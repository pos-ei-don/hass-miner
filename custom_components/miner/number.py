"""Number platform for ASIC Miner integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import vnish
from .const import DOMAIN
from .coordinator import MinerCoordinator
from .entity import MinerEntity


class PowerLimitNumber(MinerEntity, NumberEntity):
    """Set the miner's power limit in watts."""

    _attr_name = "Power Limit"
    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 1.0
    _attr_native_max_value = 10_000.0
    _attr_native_step = 10.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_power_limit"
        self._apply_naming("number")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        if data.tuning_target is not None:
            watts = data.tuning_target.watts
            if watts is not None:
                return watts
        return data.wattage

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.miner.set_power_limit(value)
        await self.coordinator.async_request_refresh()


class VnishThrottleNumber(MinerEntity, NumberEntity):
    """Set the VNish throttle (percent of full power, 100 = unthrottled).

    Native since asic-rs 0.7.0.1: reads ``MinerData.throttle_percent`` and writes
    via ``miner.set_throttle()`` (the firmware accepts 20..100). No REST shim.
    """

    _attr_name = "VNish Throttle"
    _attr_icon = "mdi:speedometer-slow"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = float(vnish.THROTTLE_MIN)
    _attr_native_max_value = float(vnish.THROTTLE_MAX)
    _attr_native_step = 1.0
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_vnish_throttle"
        self._apply_naming("number")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.throttle_percent if data is not None else None

    async def async_set_native_value(self, value: float) -> None:
        if self.coordinator.miner is None:
            raise HomeAssistantError("miner not connected")
        ok = await self.coordinator.miner.set_throttle(int(value))
        if not ok:
            raise HomeAssistantError(f"VNish throttle {int(value)}% failed")
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[MinerEntity] = []

    # Native PowerLimit gated on a CACHED capability (coordinator.supports_*),
    # so the entity also appears when the miner is offline at startup (shown
    # unavailable, no reload needed) and recovers when it returns.
    if coordinator.supports_set_power_limit:
        entities.append(PowerLimitNumber(coordinator))

    # BETA: VNish throttle for VNish-firmware miners (asic-rs read-only here).
    # VNish detection falls back to the cached profile when offline at startup.
    is_vnish = coordinator.is_vnish or bool(
        coordinator.profile and coordinator.profile.get("is_vnish")
    )
    if is_vnish:
        entities.append(VnishThrottleNumber(coordinator))

    async_add_entities(entities)
