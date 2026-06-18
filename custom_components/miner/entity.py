"""Base entity for ASIC Miner integration."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MinerCoordinator


class MinerEntity(CoordinatorEntity[MinerCoordinator]):
    """Base class for all ASIC Miner entities."""

    # TODO(entity-ids): generated entity_ids are very long and double-prefixed
    # with the device name, e.g.
    #   sensor.antminer_3_dry2_s19k_pro_antminer_s19kpro_board_1_chip_temperature
    # because both the device name and the entity name carry the make/model.
    # Shortening would change existing entity_ids (migration risk), so it is left
    # as a follow-up rather than fixed here. See PR discussion.
    _attr_has_entity_name = True

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        data = coordinator.data
        # pyasic-rs 0.6.0: DeviceInfo no longer exposes .make/.model as
        # attributes (only model_dump()).
        di = data.device_info.model_dump()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_unique_id)},
            connections={(dr.CONNECTION_NETWORK_MAC, data.mac)} if data.mac else set(),
            name=f"{di.get('make')} {di.get('model')}",
            manufacturer=di.get("make"),
            model=di.get("model"),
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
