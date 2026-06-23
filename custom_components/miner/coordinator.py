"""DataUpdateCoordinator for ASIC Miner."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from pyasic_rs import MinerFactory
from pyasic_rs.data import HashRateUnit, MinerData
from pyasic_rs.miner import Miner

from . import bos, vnish
from .const import BOOT_POLL_INTERVAL, DEFAULT_BOOT_TIMEOUT, DEFAULT_SCAN_INTERVAL, DOMAIN
from .efficiency import EfficiencySampler, EfficiencyStore

_LOGGER = logging.getLogger(__name__)

# VNish self-reported states that are NOT steady mining → never sample these.
_VNISH_TUNING_STATES = {
    "tuning",
    "autotuning",
    "initializing",
    "preheating",
    "starting",
    "stopped",
}


class MinerCoordinator(DataUpdateCoordinator[MinerData]):
    """Coordinator that polls a single ASIC miner via pyasic-rs."""

    miner: Miner | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        ip: str,
        entry_id: str,
        username: str | None = None,
        password: str | None = None,
        scan_interval: int | None = None,
        power_entity: str | None = None,
        boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
    ) -> None:
        self.ip = ip
        self.username = username
        self.password = password
        self.entry_id = entry_id

        # ── Offline resilience: cached device profile ──────────────────────
        # Persisted via Store (NOT entry.data — writing entry.data would trigger
        # the options update listener and reload-loop). Lets the entry LOAD with
        # entities (showing unavailable) even when the miner is unreachable at
        # HA startup. Populated from a successful poll; read as a fallback when
        # live ``data`` is None.
        self._store: Store = Store(hass, 1, f"{DOMAIN}_profile_{entry_id}")
        self.profile: dict | None = None

        # ── Self-learning power-level efficiency map (#621 B) ───────────────
        self.efficiency = EfficiencyStore(hass, entry_id)
        self._eff_sampler = EfficiencySampler(self.efficiency)
        self._eff_loaded = False

        # Configured (normal) scan interval — kept so we can restore it after a
        # boot fast-loop or after power returns.
        self._scan_interval = scan_interval or DEFAULT_SCAN_INTERVAL

        # BETA VNish control state (populated only for VNish miners).
        self.is_vnish: bool = False
        self.vnish_presets: list[str] = []
        # name -> human Select label (tuned hashrate / "(untuned)" marker).
        self.vnish_preset_labels: dict[str, str] = {}
        self.vnish_preset: str | None = None
        self.vnish_throttle: int | None = None
        # VNish's own state verdict (mining / tuning / initializing / stopped …),
        # polled from /summary alongside the throttle. Lets the safety-reason
        # sensor say "tuning in progress" instead of a bare "OK".
        self.vnish_state: str | None = None
        # GUI-set power limit (misc.power_limit) → caps the offered presets.
        self.vnish_power_limit: int | None = None
        self.vnish_power_limit_enabled: bool = False
        # BOS power config (current/step/min) for set_power_limit miners.
        self.bos_power_config: dict = {}

        # ── Power-aware polling state ──────────────────────────────────────
        # When no power_entity is configured, power_on stays True forever and
        # none of the power logic ever fires ⇒ exact legacy behavior.
        self.power_entity = power_entity or None
        self.boot_timeout = boot_timeout
        self.power_on: bool = True
        self.booting: bool = False
        self.boot_failed: bool = False
        self._power_on_since = None
        self._power_unsub = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{ip}",
            update_interval=timedelta(seconds=self._scan_interval),
        )

    # ── Power tracking ─────────────────────────────────────────────────────

    async def async_setup_power_tracking(self) -> None:
        """Read the power entity's current state and subscribe to changes.

        Called from __init__.py after the coordinator is created but before the
        first refresh. No-op when no power_entity is configured.
        """
        if not self.power_entity:
            return
        state = self.hass.states.get(self.power_entity)
        # Only an explicit "off" suppresses polling. At HA startup the power
        # entity's integration may not have loaded yet (state None / "unknown" /
        # "unavailable"); treating that as off would wrongly suppress a running
        # miner for the whole session. Default to powered-on in the ambiguous
        # case — it self-corrects on the next state change.
        self.power_on = state is None or state.state != "off"
        self._power_unsub = async_track_state_change_event(
            self.hass, [self.power_entity], self._handle_power_event
        )

    @callback
    def _stop_power_tracking(self) -> None:
        """Unsubscribe from the power entity (registered on entry unload)."""
        if self._power_unsub is not None:
            self._power_unsub()
            self._power_unsub = None

    @callback
    def _handle_power_event(self, event) -> None:
        new = event.data.get("new_state")
        # Ignore transient/unknown states: only explicit on/off flips the power
        # state. A power entity briefly going "unavailable" (its integration
        # reloading) must not be read as powered-off and stop a running miner.
        if new is None or new.state in ("unavailable", "unknown"):
            return
        on = new.state == "on"

        if on and not self.power_on:
            # OFF → ON: begin the fast boot loop and poll immediately.
            self.power_on = True
            self.booting = True
            self.boot_failed = False
            self._power_on_since = dt_util.utcnow()
            self.update_interval = timedelta(seconds=BOOT_POLL_INTERVAL)
            self.hass.async_create_task(self.async_request_refresh())
        elif not on and self.power_on:
            # ON → OFF: stop polling the network, restore normal cadence.
            self.power_on = False
            self.booting = False
            self.boot_failed = False
            self._power_on_since = None
            self.update_interval = timedelta(seconds=self._scan_interval)
            # Push state so entities re-evaluate availability. _async_update_data
            # will now short-circuit (powered off), so entities go unavailable.
            self.async_update_listeners()

    # ── Cached device profile (offline resilience) ─────────────────────────

    async def async_load_profile(self) -> None:
        """Load the persisted device profile (None if no file exists yet).

        Called from __init__.py before the first refresh so that platforms can
        enumerate per-board / per-fan entities from cache when the miner is
        offline at startup.
        """
        self.profile = await self._store.async_load()

    async def _async_store_profile(self, data: MinerData) -> None:
        """Persist a fresh profile derived from a successful poll, if changed."""
        profile = {
            "mac": data.mac,
            "make": data.device_info.make,
            "model": data.device_info.model,
            "fw": data.firmware_version,
            "board_positions": [b.position for b in data.hashboards],
            "fan_positions": [f.position for f in data.fans],
            "psu_fan_positions": [f.position for f in data.psu_fans],
            "is_vnish": self.is_vnish,
            # Cache control capabilities so native entities can be created even
            # when the miner is offline at next startup (no reload needed, #634).
            "supports_set_power_limit": bool(
                getattr(self.miner, "supports_set_power_limit", False)
            ),
            "supports_pause": bool(getattr(self.miner, "supports_pause", False)),
            "supports_resume": bool(getattr(self.miner, "supports_resume", False)),
            "supports_restart": bool(getattr(self.miner, "supports_restart", False)),
            "supports_set_fault_light": bool(
                getattr(self.miner, "supports_set_fault_light", False)
            ),
            "supports_presets": bool(getattr(self.miner, "supports_presets", False)),
            "supports_check_firmware_update": bool(
                getattr(self.miner, "supports_check_firmware_update", False)
            ),
        }
        if profile != self.profile:
            self.profile = profile
            await self._store.async_save(profile)

    # Helper properties: prefer live ``data``, fall back to the cached profile,
    # finally a safe default. Used by entity.py and the platform setups so they
    # work identically online and offline-with-cache.

    @property
    def device_mac(self) -> str | None:
        data = self.data
        if data is not None and data.mac:
            return data.mac
        if self.profile:
            return self.profile.get("mac")
        return None

    @property
    def device_make(self) -> str | None:
        data = self.data
        if data is not None and data.device_info.make:
            return data.device_info.make
        if self.profile:
            return self.profile.get("make")
        return None

    @property
    def device_model(self) -> str | None:
        data = self.data
        if data is not None and data.device_info.model:
            return data.device_info.model
        if self.profile:
            return self.profile.get("model")
        return None

    @property
    def fw_version(self) -> str | None:
        data = self.data
        if data is not None and data.firmware_version:
            return data.firmware_version
        if self.profile:
            return self.profile.get("fw")
        return None

    @property
    def board_positions(self) -> list[int]:
        data = self.data
        if data is not None:
            return [b.position for b in data.hashboards]
        if self.profile:
            return list(self.profile.get("board_positions") or [])
        return []

    @property
    def fan_positions(self) -> list[int]:
        data = self.data
        if data is not None:
            return [f.position for f in data.fans]
        if self.profile:
            return list(self.profile.get("fan_positions") or [])
        return []

    @property
    def psu_fan_positions(self) -> list[int]:
        data = self.data
        if data is not None:
            return [f.position for f in data.psu_fans]
        if self.profile:
            return list(self.profile.get("psu_fan_positions") or [])
        return []

    # Control-capability flags: live miner when connected, else the cached
    # profile (#634). Lets platform setups create native control entities even
    # when the miner is offline at startup — they show unavailable and recover
    # when it returns, without needing a reload.
    def _supports(self, cap: str) -> bool:
        if self.miner is not None:
            return bool(getattr(self.miner, cap, False))
        return bool(self.profile and self.profile.get(cap))

    @property
    def supports_set_power_limit(self) -> bool:
        return self._supports("supports_set_power_limit")

    @property
    def supports_pause(self) -> bool:
        return self._supports("supports_pause")

    @property
    def supports_resume(self) -> bool:
        return self._supports("supports_resume")

    @property
    def supports_restart(self) -> bool:
        return self._supports("supports_restart")

    @property
    def supports_set_fault_light(self) -> bool:
        return self._supports("supports_set_fault_light")

    @property
    def supports_presets(self) -> bool:
        return self._supports("supports_presets")

    @property
    def supports_check_firmware_update(self) -> bool:
        return self._supports("supports_check_firmware_update")

    # ── Setup / update ─────────────────────────────────────────────────────

    async def _async_setup(self) -> None:
        if self.power_entity and not self.power_on:
            raise UpdateFailed("miner powered off")
        factory = MinerFactory()
        miner = await factory.get_miner(self.ip)
        if miner is None:
            raise UpdateFailed(f"Could not identify miner at {self.ip}")
        # Auth on password alone: VNish is password-only (no username), and the
        # native control paths (get_presets/set_preset/set_throttle) authenticate
        # via this set_auth — without it they would have no token.
        if self.password:
            miner.set_auth(self.username or "", self.password)
        self.miner = miner

        # Detect VNish firmware so the preset/throttle entities get added.
        session = async_get_clientsession(self.hass)
        self.is_vnish = await vnish.detect_vnish(session, self.ip)
        if self.is_vnish:
            # Native (asic-rs 0.7.0.2+): preset list/labels come from the library
            # (miner.get_presets), authenticated via the set_auth above — no REST
            # shim. Each PresetInfo carries name/pretty/status for the label.
            presets: list = []
            try:
                presets = await self.miner.get_presets()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("native get_presets failed for %s: %s", self.ip, err)
            if presets:
                self.vnish_presets = [p.name for p in presets]
                self.vnish_preset_labels = {
                    p.name: vnish.preset_label(p.name, p.pretty, p.status)
                    for p in presets
                }
            else:
                # No live list (no password / unreachable): bare fallback names.
                self.vnish_presets = list(vnish.FALLBACK_PRESETS)
                self.vnish_preset_labels = {n: n for n in vnish.FALLBACK_PRESETS}
            if self.password:
                (
                    self.vnish_power_limit,
                    self.vnish_power_limit_enabled,
                ) = await vnish.fetch_power_limit(session, self.ip, self.password)
        elif self.miner is not None and getattr(
            self.miner, "supports_set_power_limit", False
        ):
            # Non-VNish set_power_limit miner (BOS/Braiins): read the real power
            # range (current/step/min) from the device. Harmless no-op if not BOS.
            self.bos_power_config = await bos.fetch_power_config(session, self.ip)

        if not self._eff_loaded:
            await self.efficiency.async_load()
            self._eff_loaded = True

    async def _async_update_data(self) -> MinerData:
        if self.power_entity and not self.power_on:
            # Benign: no network call while powered off. Entities go unavailable.
            raise UpdateFailed("miner powered off")
        if self.miner is None:
            await self._async_setup()
        try:
            data = await self.miner.get_data()
        except Exception as err:
            # While booting, latch the alarm once the boot timeout has elapsed.
            if (
                self.booting
                and not self.boot_failed
                and self._power_on_since is not None
                and (dt_util.utcnow() - self._power_on_since).total_seconds()
                > self.boot_timeout
            ):
                # Boot timed out: latch the alarm and stop hammering at the fast
                # cadence — fall back to the normal interval. booting stays True
                # so a later success still clears the alarm and recovers.
                self.boot_failed = True
                self.update_interval = timedelta(seconds=self._scan_interval)
            raise UpdateFailed(
                f"Error communicating with miner at {self.ip}: {err}"
            ) from err

        # Success: if we were booting, the miner is up — clear boot state and
        # restore the normal polling cadence.
        if self.booting:
            self.booting = False
            self.boot_failed = False
            self.update_interval = timedelta(seconds=self._scan_interval)

        if self.is_vnish:
            await self._async_update_vnish()

        # Feed the self-learning efficiency map (defensive: never raises).
        self._observe_efficiency(data)

        # Persist a fresh device profile so the entry can load offline next time.
        await self._async_store_profile(data)
        return data

    @property
    def sampling_key(self):
        """Level key currently accumulating a dwell window (for 'lernt…' label)."""
        return self._eff_sampler.active_key if self._eff_sampler else None

    def _observe_efficiency(self, data: MinerData) -> None:
        """Extract (level, hashrate, efficiency, mining, tuning) → sampler."""
        try:
            if self._eff_sampler is None:
                return
            if self.is_vnish:
                key = self.vnish_preset
                tuning = (self.vnish_state or "").lower() in _VNISH_TUNING_STATES
                # Throttled VNish: hashrate/efficiency reflect the throttle, not
                # the preset → would contaminate the per-preset value. Only learn
                # at full throttle (100); otherwise skip (reuse the tuning path).
                if self.vnish_throttle is not None and self.vnish_throttle < 100:
                    tuning = True
            else:
                tt = getattr(data, "tuning_target", None)
                watts = getattr(tt, "watts", None) if tt else None
                if watts is None:
                    watts = getattr(data, "wattage", None)
                key = str(int(round(watts))) if watts else None
                tuning = False
            try:
                hashrate = (
                    data.hashrate.into_unit(HashRateUnit.TH).value
                    if data.hashrate
                    else None
                )
            except Exception:  # noqa: BLE001
                hashrate = None
            self._eff_sampler.observe(
                key=key,
                hashrate=hashrate,
                efficiency=getattr(data, "efficiency", None),
                mining=bool(getattr(data, "is_mining", False)),
                tuning=tuning,
            )
        except Exception:  # noqa: BLE001
            return

    async def _async_update_vnish(self) -> None:
        """BETA: refresh VNish preset/throttle. Never fails the main update."""
        session = async_get_clientsession(self.hass)
        try:
            self.vnish_throttle, self.vnish_state = await vnish.fetch_status(
                session, self.ip
            )
            if self.password:
                # Native: current preset from the library (auth via set_auth).
                self.vnish_preset = await self.miner.get_current_preset()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("VNish extra-poll failed for %s: %s", self.ip, err)
