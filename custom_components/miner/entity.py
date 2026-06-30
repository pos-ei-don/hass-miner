"""Base entity for ASIC Miner integration."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_MAC, CONF_SIMPLE_NAMING, DEFAULT_SIMPLE_NAMING, DOMAIN
from .coordinator import MinerCoordinator


def naming_object_id(slug: str, device_unique_id: str, unique_id: str) -> str:
    """Deterministic object_id for an entity under simple_naming (#625).

    "miner_<slug>_<key>", where <key> is the entity's unique_id with the
    per-device prefix stripped (every entity sets unique_id = "<device>_<key>").
    Used both for fresh registrations (MinerEntity._apply_naming) and the
    explicit migration button (ApplyNamingButton).
    """
    prefix = f"{device_unique_id}_"
    key = unique_id[len(prefix):] if unique_id.startswith(prefix) else unique_id
    return f"{DOMAIN}_{slug}_{key}"


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
        # Naming (#625): read simple_naming + a stable slug from the config entry
        # title. The slug drives deterministic entity_ids; falls back to the IP
        # when there is no entry/title. Tolerates a missing entry (never raises).
        entry = coordinator.hass.config_entries.async_get_entry(coordinator.entry_id)
        self._simple_naming = bool(
            entry.options.get(CONF_SIMPLE_NAMING, DEFAULT_SIMPLE_NAMING)
        ) if entry else DEFAULT_SIMPLE_NAMING
        self._name_slug = slugify((entry.title if entry else None) or coordinator.ip)
        # Build device_info from the coordinator helpers, which prefer live data
        # and fall back to the cached profile. They tolerate the fully-offline,
        # never-seen case (everything None) — we then use the IP-based identifier
        # and a generic name. Never raise on None.
        mac = coordinator.device_mac
        make = coordinator.device_make
        model = coordinator.device_model
        if make or model:
            name = " ".join(p for p in (make, model) if p)
        else:
            name = f"ASIC Miner ({coordinator.ip})"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_unique_id)},
            connections={(dr.CONNECTION_NETWORK_MAC, mac)} if mac else set(),
            name=name,
            manufacturer=make,
            model=model,
            sw_version=coordinator.fw_version,
            configuration_url=f"http://{coordinator.ip}",
        )

    @property
    def _device_unique_id(self) -> str:
        """Stable device identity (#672).

        Order: persisted MAC (entry.data) > live/cached MAC > IP.

        The persisted MAC is the key improvement: once the MAC is ever seen it is
        written to entry.data and used forever, so the identity no longer flips
        between MAC and IP depending on whether the miner happened to be online at
        setup (the bug that orphaned entities and spawned a duplicate device). On
        first MAC sighting, async_setup_entry also migrates any IP-prefixed
        registry rows to the MAC.

        The IP remains the *last* resort, only for miners whose firmware/library
        exposes no MAC at all (e.g. some stock Antminer firmware via asic-rs,
        where ``data.mac is None``). It is intentionally NOT changed to something
        like entry_id, because re-keying an existing MAC-less miner would orphan
        its entities. For those miners the correct long-term fix is to obtain a
        real hardware id (MAC/serial) from the library; until then the IP (pinned
        by a static DHCP reservation) is the only stable handle available.
        """
        entry = self.coordinator.hass.config_entries.async_get_entry(
            self.coordinator.entry_id
        )
        persisted = entry.data.get(CONF_MAC) if entry else None
        if persisted:
            return persisted
        mac = self.coordinator.device_mac
        if mac:
            return mac.replace(":", "").lower()
        return self.coordinator.ip

    def _apply_naming(self, platform_domain: str) -> None:
        """Suggest a deterministic entity_id (#625), called by each platform
        after unique_id is set. Only honored at FIRST registration — existing
        entities keep their entity_id (use the "Apply naming scheme" button to
        migrate). No-op when simple_naming is off or unique_id is unset."""
        if not self._simple_naming or not self._attr_unique_id:
            return
        object_id = naming_object_id(
            self._name_slug, self._device_unique_id, self._attr_unique_id
        )
        self.entity_id = f"{platform_domain}.{object_id}"
