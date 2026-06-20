"""Select platform for ASIC Miner integration.

One unified ``PowerLevelSelect`` drives the discrete power-level control for BOTH
miner types via a thin ``LevelProvider`` (#621), so the VNish-preset and the
generic stepped-watt paths cannot drift apart:

* VNish firmware  → ``VnishPresetProvider`` (BETA REST shim, see vnish.py)
* set_power_limit  → ``SteppedPowerProvider`` (BOS / WhatsMiner, config/heuristic)
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ENABLE_POWER_LEVELS,
    CONF_POWER_MAX,
    CONF_POWER_MIN,
    CONF_POWER_STEP,
    DEFAULT_ENABLE_POWER_LEVELS,
    DOMAIN,
)
from .coordinator import MinerCoordinator
from .entity import MinerEntity
from .level_providers import (
    LevelProvider,
    SteppedPowerProvider,
    VnishPresetProvider,
)


class PowerLevelSelect(MinerEntity, SelectEntity):
    """Discrete power-level selector (VNish presets OR stepped watts)."""

    def __init__(
        self,
        coordinator: MinerCoordinator,
        provider: LevelProvider,
        *,
        unique_suffix: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._provider = provider
        self._attr_unique_id = f"{self._device_unique_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_icon = icon

    @property
    def options(self) -> list[str]:
        return [o for o in self._provider.options() if o is not None]

    @property
    def current_option(self) -> str | None:
        return self._provider.current_option()

    async def async_select_option(self, option: str) -> None:
        await self._provider.apply(option)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]
    opts = entry.options
    enabled = opts.get(CONF_ENABLE_POWER_LEVELS, DEFAULT_ENABLE_POWER_LEVELS)

    entities: list[MinerEntity] = []
    if enabled:
        # VNish detection falls back to the cached profile when offline at startup.
        is_vnish = coordinator.is_vnish or bool(
            coordinator.profile and coordinator.profile.get("is_vnish")
        )
        if is_vnish:
            # Same unique_id as the previous VnishPresetSelect → entity preserved.
            entities.append(
                PowerLevelSelect(
                    coordinator,
                    VnishPresetProvider(coordinator),
                    unique_suffix="vnish_preset",
                    name="VNish Preset",
                    icon="mdi:speedometer",
                )
            )
        elif (
            coordinator.miner is not None
            and coordinator.miner.supports_set_power_limit
        ):
            entities.append(
                PowerLevelSelect(
                    coordinator,
                    SteppedPowerProvider(
                        coordinator,
                        min_w=opts.get(CONF_POWER_MIN),
                        max_w=opts.get(CONF_POWER_MAX),
                        step=opts.get(CONF_POWER_STEP),
                    ),
                    unique_suffix="power_level",
                    name="Leistungsstufe",
                    icon="mdi:speedometer",
                )
            )

    async_add_entities(entities)
