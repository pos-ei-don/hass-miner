"""Firmware-update entity for ASIC miners.

Surfaces the installed firmware version plus, where the library can check it,
whether a newer firmware is available. The availability check (asic-rs
``check_firmware_update`` → e.g. BraiinsOS ``bos.checkForUpgrade``) hits the
vendor's release server, so it runs on a slow self-poll (~daily) instead of the
fast telemetry coordinator. Read-only: no install action is offered.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MinerCoordinator
from .entity import MinerEntity, async_remove_stale_entities

_LOGGER = logging.getLogger(__name__)

# The vendor check is expensive (hits the release server) — poll it ~once a day.
SCAN_INTERVAL = timedelta(hours=24)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware-update entity for miners that support the check."""
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[MinerEntity] = []
    if coordinator.supports_check_firmware_update:
        entities.append(MinerFirmwareUpdate(coordinator))

    async_remove_stale_entities(
        hass, entry, "update", [e.unique_id for e in entities]
    )
    async_add_entities(entities)


class MinerFirmwareUpdate(MinerEntity, UpdateEntity):
    """Installed firmware + (where checkable) available-update indicator."""

    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_firmware_update"
        self._latest_version: str | None = None
        self._release_url: str | None = None
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
        """Ask the library whether a newer firmware is available (slow path)."""
        miner = self.coordinator.miner
        if miner is None:
            return
        try:
            result = await miner.check_firmware_update()
        except Exception as err:  # noqa: BLE001 — never let the UI crash on this
            _LOGGER.debug("firmware update check failed for %s: %s", self.coordinator.ip, err)
            return
        if result is not None:
            self._latest_version = getattr(result, "latest_version", None)
            self._release_url = getattr(result, "release_url", None)
