"""Select platform for ASIC Miner integration.

One unified ``PowerLevelSelect`` drives the discrete power-level control for BOTH
miner types via a thin ``LevelProvider`` (#621), so the VNish-preset and the
generic stepped-watt paths cannot drift apart:

* VNish firmware  → ``VnishPresetProvider`` (BETA REST shim, see vnish.py)
* set_power_limit  → ``SteppedPowerProvider`` (BOS / WhatsMiner, config/heuristic)

Entity services (#621 B):
* ``miner.reset_efficiency``  — clear learned efficiency (one level or all)
* ``miner.set_efficiency``    — pin a known hashrate/efficiency for a level
* ``miner.set_power_range``   — set min/max/step for the stepped levels
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
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
from . import timezone as tz_platform


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
        self._apply_naming("select")
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

    # ── entity services (#621 B) ─────────────────────────────────────────
    async def async_reset_efficiency(self, level: str | None = None) -> None:
        self.coordinator.efficiency.reset(level)
        self.async_write_ha_state()

    async def async_set_efficiency(
        self,
        level: str,
        hashrate: float | None = None,
        efficiency: float | None = None,
        pin: bool = True,
    ) -> None:
        self.coordinator.efficiency.set_manual(
            level, hashrate=hashrate, efficiency=efficiency, pin=pin
        )
        self.async_write_ha_state()

    async def async_set_power_range(
        self,
        min: float | None = None,  # noqa: A002 — HA service field name
        max: float | None = None,  # noqa: A002
        step: int | None = None,
    ) -> None:
        entry = self.hass.config_entries.async_get_entry(self.coordinator.entry_id)
        if entry is None:
            return
        new_options = dict(entry.options)
        if min is not None:
            new_options[CONF_POWER_MIN] = min
        if max is not None:
            new_options[CONF_POWER_MAX] = max
        if step is not None:
            new_options[CONF_POWER_STEP] = int(step)
        # Validate before persisting (effective values incl. existing config).
        eff_min = new_options.get(CONF_POWER_MIN)
        eff_max = new_options.get(CONF_POWER_MAX)
        eff_step = new_options.get(CONF_POWER_STEP)
        if eff_step is not None and eff_step <= 0:
            raise HomeAssistantError(f"step must be > 0 (got {eff_step})")
        if eff_min is not None and eff_max is not None and eff_min >= eff_max:
            raise HomeAssistantError(
                f"min ({eff_min}) must be below max ({eff_max})"
            )
        # Triggers the options update listener → entry reload → levels regenerate.
        self.hass.config_entries.async_update_entry(entry, options=new_options)


def _register_services() -> None:
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "reset_efficiency",
        {vol.Optional("level"): cv.string},
        "async_reset_efficiency",
    )
    platform.async_register_entity_service(
        "set_efficiency",
        {
            vol.Required("level"): cv.string,
            vol.Optional("hashrate"): vol.Coerce(float),
            vol.Optional("efficiency"): vol.Coerce(float),
            vol.Optional("pin", default=True): cv.boolean,
        },
        "async_set_efficiency",
    )
    platform.async_register_entity_service(
        "set_power_range",
        {
            vol.Optional("min"): vol.Coerce(float),
            vol.Optional("max"): vol.Coerce(float),
            vol.Optional("step"): vol.Coerce(int),
        },
        "async_set_power_range",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]
    opts = entry.options
    enabled = opts.get(CONF_ENABLE_POWER_LEVELS, DEFAULT_ENABLE_POWER_LEVELS)

    _register_services()

    entities: list[MinerEntity] = []
    if enabled:
        # VNish detection falls back to the cached profile when offline at startup.
        is_vnish = coordinator.is_vnish or bool(
            coordinator.profile and coordinator.profile.get("is_vnish")
        )
        # Only offer the preset select if the lib actually supports presets.
        # Guards against a wheel without preset support showing a half-working
        # select ("unknown" current + fallback options) — the 0.7.0.4 regression.
        if is_vnish and coordinator.supports_presets:
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

    # Timezone management is also a `select` entity — wire it through here so it
    # lives under the already-registered Platform.SELECT (no new platform). This
    # also registers the `miner.sync_timezone` service and the optional auto-sync.
    await tz_platform.async_setup_entry(hass, entry, async_add_entities)
