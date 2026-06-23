"""Button platform for ASIC Miner integration."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import MinerCoordinator
from .entity import MinerEntity, naming_object_id

_LOGGER = logging.getLogger(__name__)


class RestartButton(MinerEntity, ButtonEntity):
    """Restart the miner."""

    _attr_name = "Restart"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_restart"
        self._apply_naming("button")

    async def async_press(self) -> None:
        await self.coordinator.miner.restart()


class ApplyNamingButton(MinerEntity, ButtonEntity):
    """Apply the simple_naming scheme to this miner's EXISTING entities (#625).

    `simple_naming` only governs entity_ids at first registration; already
    registered entities keep their id. Pressing this renames them to
    "<platform>.miner_<slug>_<key>" deterministically. Existing references
    (automations/dashboards) may need updating afterwards — hence a manual,
    config-category action, never automatic."""

    _attr_name = "Apply naming scheme"
    _attr_icon = "mdi:rename-box"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MinerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_apply_naming"
        self._apply_naming("button")

    async def async_press(self) -> None:
        registry = er.async_get(self.hass)
        entry_id = self.coordinator.entry_id
        device_uid = self._device_unique_id
        slug = self._name_slug
        renamed = 0
        for ent in list(registry.entities.values()):  # snapshot; we mutate below
            if ent.config_entry_id != entry_id or ent.platform != DOMAIN:
                continue
            target = f"{ent.domain}.{naming_object_id(slug, device_uid, ent.unique_id)}"
            if ent.entity_id == target:
                continue
            if registry.async_get(target) is not None:
                _LOGGER.warning(
                    "Apply naming: target %s exists, skipping %s",
                    target, ent.entity_id,
                )
                continue
            registry.async_update_entity(ent.entity_id, new_entity_id=target)
            renamed += 1
        _LOGGER.info("Apply naming scheme: renamed %d entities (slug=%s)", renamed, slug)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[MinerEntity] = []

    # Gated on a CACHED capability (coordinator.supports_restart), so the button
    # also appears when the miner is offline at startup (unavailable, no reload)
    # and recovers when it returns.
    if coordinator.supports_restart:
        entities.append(RestartButton(coordinator))

    # Naming-migration button (#625): always available, independent of miner
    # capabilities (operates on the entity registry, not the device).
    entities.append(ApplyNamingButton(coordinator))

    async_add_entities(entities)
