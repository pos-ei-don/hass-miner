"""Switch platform for ASIC Miner integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .control import async_miner_control
from .coordinator import MinerCoordinator
from .entity import MinerEntity

_LOGGER = logging.getLogger(__name__)


class FaultLightSwitch(MinerEntity, SwitchEntity):
    """Controls the miner's fault/locate LED."""

    _attr_name = "Fault Light"
    _attr_icon = "mdi:led-on"

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_fault_light"
        self._apply_naming("switch")

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.light_flashing

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.light_flashing is not None
        )

    async def async_turn_on(self, **kwargs) -> None:
        await async_miner_control(
            "Fault light on", self.coordinator.miner, lambda m: m.set_fault_light(True)
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await async_miner_control(
            "Fault light off",
            self.coordinator.miner,
            lambda m: m.set_fault_light(False),
        )
        await self.coordinator.async_request_refresh()


class MiningSwitch(MinerEntity, SwitchEntity):
    """Pause or resume mining on the miner."""

    _attr_name = "Mining"
    _attr_icon = "mdi:pickaxe"

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_mining"
        self._apply_naming("switch")

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.is_mining

    async def async_turn_on(self, **kwargs) -> None:
        await self._switch_mining(
            "Resume mining", True, lambda m: m.resume(timedelta(0))
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self._switch_mining(
            "Pause mining", False, lambda m: m.pause(timedelta(0))
        )

    async def _switch_mining(self, what: str, want: bool, call) -> None:
        """Pause/resume; a refusal is only an error if the miner is not there yet.

        Since asic-rs 0.8.6 a refused call raises. Braiins OS refuses a pause on
        a miner that is not mining yet (HTTP 500 right after boot) - that is the
        wanted state, so it must not abort the caller (automation, script).
        """
        try:
            await async_miner_control(what, self.coordinator.miner, call)
        except HomeAssistantError as err:
            await self.coordinator.async_refresh()
            data = self.coordinator.data
            if (
                not self.coordinator.last_update_success
                or data is None
                or data.is_mining is not want
            ):
                raise
            _LOGGER.warning(
                "%s on %s refused, miner already in that state: %s",
                what,
                self.coordinator.ip,
                err,
            )
            return
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[MinerEntity] = []

    # Gated on CACHED capabilities (coordinator.supports_*), so these native
    # entities also appear when the miner is offline at startup (unavailable,
    # no reload) and recover when it returns.
    if coordinator.supports_set_fault_light:
        entities.append(FaultLightSwitch(coordinator))

    if coordinator.supports_pause and coordinator.supports_resume:
        entities.append(MiningSwitch(coordinator))

    async_add_entities(entities)
