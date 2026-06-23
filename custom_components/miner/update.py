"""Firmware-update entity for ASIC miners.

Surfaces the installed firmware version plus, where determinable, whether a newer
firmware is available. Two sources, both slow (~daily self-poll, not the fast
telemetry coordinator):

* **BraiinsOS** — the library does a local-API check (`asic-rs
  check_firmware_update` → `bos.checkForUpgrade`), returning the latest release.
* **VNish** — there is no local update-availability field; VNish's own GUI
  compares the installed version against the vendor's cloud changelog. We do the
  same **consumer-side** (the lib stays cloud-free): fetch the release list from
  the vendor and take the latest stable, then let Home Assistant compare it
  against the installed version.

Read-only: no install action is offered.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import aiohttp
from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MinerCoordinator
from .entity import MinerEntity, async_remove_stale_entities

_LOGGER = logging.getLogger(__name__)

# The vendor check is expensive (hits a release server) — poll it ~once a day.
SCAN_INTERVAL = timedelta(hours=24)

# VNish/Hashcore release changelog (consumer-side cloud check). Same source the
# VNish web UI uses to mark "are you up to date?".
_VNISH_RELEASES_URL = "https://partner.anthill.farm/api/client/releases-notes"
_VNISH_RELEASE_NOTES_URL = "https://docs.hashcore.com/firmware/about"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable int tuple ('1.3.4' -> (1,3,4))."""
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


async def _fetch_vnish_latest_stable(
    session: aiohttp.ClientSession,
) -> str | None:
    """Latest stable VNish firmware version from the vendor changelog, or None.

    The version numbers are unified across miner series, so we take the global
    max stable version. Fully defensive: any failure returns None.
    """
    try:
        async with session.get(_VNISH_RELEASES_URL, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            releases = await resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(releases, list):
        return None
    stable = [
        str(r.get("version"))
        for r in releases
        if isinstance(r, dict)
        and str(r.get("stage", "")).lower() == "stable"
        and r.get("version")
    ]
    if not stable:
        return None
    return max(stable, key=_version_tuple)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware-update entity for miners where we can determine it."""
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]

    is_vnish = coordinator.is_vnish or bool(
        coordinator.profile and coordinator.profile.get("is_vnish")
    )

    entities: list[MinerEntity] = []
    if coordinator.supports_check_firmware_update or is_vnish:
        entities.append(MinerFirmwareUpdate(coordinator, vnish_cloud=is_vnish))

    async_remove_stale_entities(
        hass, entry, "update", [e.unique_id for e in entities]
    )
    async_add_entities(entities)


class MinerFirmwareUpdate(MinerEntity, UpdateEntity):
    """Installed firmware + (where determinable) available-update indicator."""

    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MinerCoordinator, *, vnish_cloud: bool = False) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_firmware_update"
        # VNish has no local update check; fall back to the vendor cloud changelog
        # only when the lib can't do it locally.
        self._vnish_cloud = vnish_cloud and not coordinator.supports_check_firmware_update
        self._latest_version: str | None = None
        self._release_url: str | None = (
            _VNISH_RELEASE_NOTES_URL if self._vnish_cloud else None
        )
        self._apply_naming("update")

    @property
    def should_poll(self) -> bool:
        # Self-poll on SCAN_INTERVAL for the slow vendor check (the base
        # CoordinatorEntity is otherwise push-only).
        return True

    @property
    def installed_version(self) -> str | None:
        data = self.coordinator.data
        return getattr(data, "firmware_version", None) if data else None

    @property
    def latest_version(self) -> str | None:
        # Fall back to the installed version so the entity reads "up to date"
        # rather than "unknown" until/unless a newer one is reported.
        return self._latest_version or self.installed_version

    @property
    def release_url(self) -> str | None:
        return self._release_url

    async def async_update(self) -> None:
        """Determine the latest available firmware (slow path)."""
        if self._vnish_cloud:
            session = async_get_clientsession(self.coordinator.hass)
            latest = await _fetch_vnish_latest_stable(session)
            if latest:
                self._latest_version = latest
            return

        miner = self.coordinator.miner
        if miner is None:
            return
        try:
            result = await miner.check_firmware_update()
        except Exception as err:  # noqa: BLE001 — never let the UI crash on this
            _LOGGER.debug(
                "firmware update check failed for %s: %s", self.coordinator.ip, err
            )
            return
        if result is not None:
            self._latest_version = getattr(result, "latest_version", None)
            self._release_url = getattr(result, "release_url", None)
