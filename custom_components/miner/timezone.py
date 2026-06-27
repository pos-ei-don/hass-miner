"""Timezone management for ASIC miners.

Lets Home Assistant set a miner's timezone and keep it correct across DST
automatically — the miners do not handle DST changes by themselves. The desired
value is always derived from HA's own configured zone (``hass.config.time_zone``,
an IANA name), so the miner simply tracks the Home Assistant instance.

Two firmware flavours, two representations of "the right timezone":

* **BraiinsOS** — takes a named IANA zone (e.g. ``"Europe/Vienna"``) and handles
  DST itself once set. We hand it the IANA name unchanged; a twice-daily re-check
  only ever has to (re)assert the name.
* **VNish** — takes a fixed UTC offset string like ``"GMT+1"`` / ``"GMT+2"`` and
  has NO DST. The correct whole-hour offset therefore has to be re-applied at
  each DST change, which is exactly what the periodic sync does.

Surfaced as a single ``select`` entity (current zone + the firmware's available
list) plus a ``miner.sync_timezone`` service and an optional periodic/startup
auto-sync. A mismatch that cannot be (or is not, in "repair" mode) corrected
automatically raises a fixable repair issue.

Fully defensive: every library call is guarded, and an older wheel without
``get_timezone_config`` / ``TimezoneConfig`` simply yields no entity and a no-op
sync — the integration never crashes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_platform
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
)

from .const import (
    CONF_TZ_CHECK,
    CONF_TZ_MODE,
    DEFAULT_TZ_CHECK,
    DEFAULT_TZ_MODE,
    DOMAIN,
    TZ_MODE_REPAIR,
)
from .coordinator import MinerCoordinator
from .entity import MinerEntity

# Older wheels may not ship TimezoneConfig — import-guard so the module always
# loads and we degrade to "no entity / no-op sync".
try:
    from pyasic_rs.config import TimezoneConfig
except Exception:  # noqa: BLE001
    TimezoneConfig = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

# Re-check twice a day. DST is handled regardless of cadence (any check after a
# changeover corrects the offset); a short interval just bounds the worst-case
# lag between the DST flip and the miner being fixed.
SCAN_INTERVAL = timedelta(hours=12)

# Run the first sync shortly after setup, not synchronously during it.
_STARTUP_SYNC_DELAY = timedelta(seconds=30)

# VNish has no DST and no useful "available" list of its own — offer a sensible
# spread of whole-hour offsets so the manual select is usable when empty.
_VNISH_DEFAULT_OFFSETS = [
    "GMT+0" if h == 0 else f"GMT{h:+d}" for h in range(-12, 15)
]


def _target_timezone(coordinator: MinerCoordinator) -> str | None:
    """Compute the desired timezone value for this miner from HA's own zone.

    HA's ``time_zone`` is an IANA name. For BraiinsOS we return it unchanged (the
    firmware tracks DST itself). For VNish we convert it to the *current* whole-
    hour ``"GMT±N"`` offset (re-evaluated on every call, so a DST flip yields the
    new offset). Returns None on any error or if the offset is not a whole hour.
    """
    try:
        tz = coordinator.hass.config.time_zone
        if not tz:
            return None
        if not coordinator.is_vnish:
            # BraiinsOS: named IANA zone, DST handled by the firmware.
            return tz
        offset = datetime.now(ZoneInfo(tz)).utcoffset()
        if offset is None:
            return None
        total_seconds = int(offset.total_seconds())
        if total_seconds % 3600 != 0:
            # VNish only supports whole-hour offsets.
            return None
        hours = total_seconds // 3600
        if hours == 0:
            return "GMT+0"
        return f"GMT{hours:+d}"
    except Exception:  # noqa: BLE001
        return None


async def _read_timezone(coordinator: MinerCoordinator) -> str | None:
    """Read the miner's current timezone via the library, or None."""
    miner = coordinator.miner
    if miner is None or TimezoneConfig is None:
        return None
    try:
        config = await miner.get_timezone_config()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("get_timezone_config failed for %s: %s", coordinator.ip, err)
        return None
    if config is None:
        return None
    return getattr(config, "timezone", None)


async def _set_timezone(coordinator: MinerCoordinator, value: str) -> None:
    """Set the miner's timezone via the library (best effort, never raises)."""
    miner = coordinator.miner
    if miner is None or TimezoneConfig is None:
        return
    try:
        await miner.set_timezone_config(TimezoneConfig(timezone=value, available=[]))
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("set_timezone_config failed for %s: %s", coordinator.ip, err)


def _issue_id(coordinator: MinerCoordinator) -> str:
    return f"timezone_{coordinator.ip}"


def _entry_title(coordinator: MinerCoordinator) -> str:
    entry = coordinator.hass.config_entries.async_get_entry(coordinator.entry_id)
    return (entry.title if entry else None) or coordinator.ip


def _delete_issue(hass: HomeAssistant, coordinator: MinerCoordinator) -> None:
    try:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(coordinator))
    except Exception:  # noqa: BLE001
        pass


def _create_issue(
    hass: HomeAssistant,
    coordinator: MinerCoordinator,
    *,
    current: str | None,
    target: str,
) -> None:
    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(coordinator),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="timezone_mismatch",
            translation_placeholders={
                "current": current or "unknown",
                "target": target,
                "name": _entry_title(coordinator),
            },
        )
    except Exception:  # noqa: BLE001
        pass


async def async_sync_timezone(
    hass: HomeAssistant,
    coordinator: MinerCoordinator,
    *,
    force_auto: bool = False,
) -> dict:
    """Reconcile the miner's timezone with HA's configured zone.

    Returns a small status dict (never raises). Behaviour depends on the per-entry
    ``timezone_mode`` option (overridden to "auto" when ``force_auto`` is set, e.g.
    from the repair fix flow):

    * **auto** — set the target, read it back, and only report success once the
      read-back confirms it. A confirmed correction clears any repair issue; a
      failed one raises a repair issue.
    * **repair** — never set automatically; raise a fixable repair issue offering
      the correction.
    """
    if coordinator.miner is None or TimezoneConfig is None:
        return {"status": "unsupported"}

    current = await _read_timezone(coordinator)
    target = _target_timezone(coordinator)
    if target is None:
        return {"status": "no_target"}
    if current is None:
        # We couldn't read the miner's timezone (e.g. it was briefly offline /
        # the config call failed). That is "unknown", NOT a mismatch — never
        # raise a repair on a failed read, or we'd flag a false "timezone is
        # unknown" whenever the device blips. Leave any existing issue as-is; a
        # later sync with the miner online resolves it via the current==target
        # branch. (Mirrors the offline-robustness of the other entities.)
        return {"status": "unread"}
    if current == target:
        # Already in sync — make sure no stale repair issue lingers.
        _delete_issue(hass, coordinator)
        return {"status": "ok", "timezone": current}

    entry = hass.config_entries.async_get_entry(coordinator.entry_id)
    mode = (
        entry.options.get(CONF_TZ_MODE, DEFAULT_TZ_MODE) if entry else DEFAULT_TZ_MODE
    )
    if force_auto:
        mode = DEFAULT_TZ_MODE

    if mode == TZ_MODE_REPAIR:
        _create_issue(hass, coordinator, current=current, target=target)
        return {"status": "repair_raised", "current": current, "target": target}

    # auto: set, then read back to verify (never trust the return alone).
    await _set_timezone(coordinator, target)
    readback = await _read_timezone(coordinator)
    if readback == target:
        _delete_issue(hass, coordinator)
        return {"status": "corrected", "from": current, "to": target}

    _create_issue(hass, coordinator, current=current, target=target)
    return {"status": "failed", "tried": target, "still": readback}


class MinerTimezoneSelect(MinerEntity, SelectEntity):
    """Current miner timezone + the firmware's available zones."""

    _attr_name = "Timezone"
    _attr_icon = "mdi:earth"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_timezone"
        self._apply_naming("select")
        self._current: str | None = None
        self._available: list[str] = []

    @property
    def should_poll(self) -> bool:
        # Self-poll on SCAN_INTERVAL: the timezone config is not part of the fast
        # telemetry coordinator and changes very rarely.
        return True

    @property
    def options(self) -> list[str]:
        if self._available:
            return [o for o in self._available if o is not None]
        if self._current:
            return [self._current]
        if self.coordinator.is_vnish:
            return list(_VNISH_DEFAULT_OFFSETS)
        return []

    @property
    def current_option(self) -> str | None:
        return self._current

    async def async_update(self) -> None:
        """Read the current timezone + available list (slow path)."""
        miner = self.coordinator.miner
        if miner is None or TimezoneConfig is None:
            return
        try:
            config = await miner.get_timezone_config()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "get_timezone_config failed for %s: %s", self.coordinator.ip, err
            )
            return
        if config is None:
            return
        self._current = getattr(config, "timezone", None)
        available = getattr(config, "available", None)
        if available:
            self._available = list(available)

    async def async_select_option(self, option: str) -> None:
        await _set_timezone(self.coordinator, option)
        # Read back to confirm rather than trusting the set return alone.
        await self.async_update()
        self.async_write_ha_state()

    async def async_sync_timezone_service(self) -> dict:
        """Service `miner.sync_timezone`: reconcile now, return the status dict."""
        result = await async_sync_timezone(self.hass, self.coordinator)
        await self.async_update()
        self.async_write_ha_state()
        return result


def _register_services() -> None:
    """Register the manual timezone-sync service (idempotent)."""
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "sync_timezone",
        {},
        "async_sync_timezone_service",
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the timezone select + service + optional auto-sync.

    Called from ``select.py``'s ``async_setup_entry`` so the entity is created
    under the already-registered ``Platform.SELECT`` — no new platform.
    """
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]

    _register_services()

    if not coordinator.supports_timezone_config or TimezoneConfig is None:
        # Unsupported (or old wheel): no entity, and the sync below is a no-op.
        return

    async_add_entities([MinerTimezoneSelect(coordinator)])

    if entry.options.get(CONF_TZ_CHECK, DEFAULT_TZ_CHECK):

        async def _periodic_sync(_now=None) -> None:
            await async_sync_timezone(hass, coordinator)

        # Periodic re-check (handles DST) ...
        entry.async_on_unload(
            async_track_time_interval(hass, _periodic_sync, SCAN_INTERVAL)
        )
        # ... plus one run shortly after setup so a mismatch surfaces promptly
        # (deferred so it never blocks/raises during platform setup).
        entry.async_on_unload(
            async_call_later(
                hass, _STARTUP_SYNC_DELAY.total_seconds(), _periodic_sync
            )
        )
