"""Base entity for ASIC Miner integration."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MinerCoordinator


@callback
def async_remove_stale_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform_domain: str,
    keep_unique_ids: Iterable[str],
) -> None:
    """Remove this entry's entities (of one platform) that are no longer produced.

    This is the "cleanup" behind the sensor-category toggles: it is driven purely
    by the (deterministic) options, so unticking a category removes exactly its
    entities on the next reload. It is never keyed on a transient/missing value,
    so it cannot delete an entity just because a miner is briefly unreachable.
    """
    keep = set(keep_unique_ids)
    registry = er.async_get(hass)
    for ent in list(registry.entities.values()):
        if (
            ent.config_entry_id == entry.entry_id
            and ent.platform == DOMAIN
            and ent.domain == platform_domain
            and ent.unique_id not in keep
        ):
            registry.async_remove(ent.entity_id)


class MinerEntity(CoordinatorEntity[MinerCoordinator]):
    """Base class for all ASIC Miner entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        data = coordinator.data
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_unique_id)},
            connections={(dr.CONNECTION_NETWORK_MAC, data.mac)} if data.mac else set(),
            name=f"{data.device_info.make} {data.device_info.model}",
            manufacturer=data.device_info.make,
            model=data.device_info.model,
            sw_version=data.firmware_version,
            configuration_url=f"http://{coordinator.ip}",
        )

    @property
    def _device_unique_id(self) -> str:
        """Stable device identifier: prefer MAC over IP."""
        data = self.coordinator.data
        if data and data.mac:
            return data.mac.replace(":", "").lower()
        return self.coordinator.ip
