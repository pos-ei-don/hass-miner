"""DataUpdateCoordinator for ASIC Miner."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from pyasic_rs import MinerFactory
from pyasic_rs.data import MinerData
from pyasic_rs.miner import Miner

from . import vnish
from .const import BOOT_POLL_INTERVAL, DEFAULT_BOOT_TIMEOUT, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class MinerCoordinator(DataUpdateCoordinator[MinerData]):
    """Coordinator that polls a single ASIC miner via pyasic-rs."""

    miner: Miner | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        ip: str,
        username: str | None = None,
        password: str | None = None,
        scan_interval: int | None = None,
        power_entity: str | None = None,
        boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
    ) -> None:
        self.ip = ip
        self.username = username
        self.password = password

        # Configured (normal) scan interval — kept so we can restore it after a
        # boot fast-loop or after power returns.
        self._scan_interval = scan_interval or DEFAULT_SCAN_INTERVAL

        # BETA VNish control state (populated only for VNish miners).
        self.is_vnish: bool = False
        self.vnish_presets: list[str] = []
        self.vnish_preset: str | None = None
        self.vnish_throttle: int | None = None

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
        self.power_on = state is not None and state.state == "on"
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
        on = new is not None and new.state == "on"

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

    # ── Setup / update ─────────────────────────────────────────────────────

    async def _async_setup(self) -> None:
        if self.power_entity and not self.power_on:
            raise UpdateFailed("miner powered off")
        factory = MinerFactory()
        miner = await factory.get_miner(self.ip)
        if miner is None:
            raise UpdateFailed(f"Could not identify miner at {self.ip}")
        if self.username and self.password:
            miner.set_auth(self.username, self.password)
        self.miner = miner

        # BETA: detect VNish firmware so the preset/throttle entities get added.
        session = async_get_clientsession(self.hass)
        self.is_vnish = await vnish.detect_vnish(session, self.ip)
        if self.is_vnish and self.password:
            self.vnish_presets = await vnish.fetch_presets(
                session, self.ip, self.password
            )
        if self.is_vnish and not self.vnish_presets:
            self.vnish_presets = list(vnish.FALLBACK_PRESETS)

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
        return data

    async def _async_update_vnish(self) -> None:
        """BETA: refresh VNish preset/throttle. Never fails the main update."""
        session = async_get_clientsession(self.hass)
        try:
            self.vnish_throttle = await vnish.fetch_throttle(session, self.ip)
            if self.password:
                self.vnish_preset = await vnish.fetch_current_preset(
                    session, self.ip, self.password
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("VNish extra-poll failed for %s: %s", self.ip, err)
