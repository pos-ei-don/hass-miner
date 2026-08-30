"""ASIC Miner integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import (
    CONF_BOOT_TIMEOUT,
    CONF_MAC,
    CONF_POWER_ENTITY,
    CONF_SCAN_INTERVAL,
    DEFAULT_BOOT_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_POWER_OFF,
    SERVICE_POWER_ON,
)
from .coordinator import MinerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ASIC Miner from a config entry."""
    # Options override entry data (e.g. a VNish web password added after setup).
    password = entry.options.get(CONF_PASSWORD) or entry.data.get(CONF_PASSWORD)
    coordinator = MinerCoordinator(
        hass,
        ip=entry.data[CONF_HOST],
        entry_id=entry.entry_id,
        username=entry.data.get(CONF_USERNAME),
        password=password,
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        power_entity=entry.options.get(CONF_POWER_ENTITY) or None,
        boot_timeout=entry.options.get(CONF_BOOT_TIMEOUT, DEFAULT_BOOT_TIMEOUT),
    )
    await coordinator.async_setup_power_tracking()
    entry.async_on_unload(coordinator._stop_power_tracking)
    # Lifecycle-status feature: track the external power sensor's on/off edges
    # and register the power_on/power_off services (once, integration-wide).
    await coordinator.async_setup_status_tracking()
    entry.async_on_unload(coordinator._stop_status_tracking)
    _async_register_services(hass)

    # Offline resilience: load the cached device profile, then do a NON-raising
    # refresh. If the miner is reachable we get live data; if not, we may still
    # have a cached profile and can load entities (showing unavailable).
    await coordinator.async_load_profile()
    await coordinator.async_refresh()

    # Only bail (and let HA retry) when we truly know nothing about the miner:
    # the refresh failed AND we have no cached profile AND no data. A never-seen
    # miner that is offline at first setup still behaves as before.
    if (
        not coordinator.last_update_success
        and coordinator.profile is None
        and coordinator.data is None
    ):
        raise ConfigEntryNotReady(
            f"Miner at {entry.data[CONF_HOST]} is unreachable and no cached "
            "profile exists yet"
        )

    # Stabilize the device identity (#672) BEFORE platforms create entities:
    # once the MAC is known, persist it and migrate any registry rows that still
    # carry a stale (IP/entry_id) prefix to the MAC. Doing this first means the
    # platforms re-attach to the migrated rows (same entity_id, no orphan) and
    # land on the canonical MAC device.
    _async_stabilize_identity(hass, entry, coordinator)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


def _async_stabilize_identity(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MinerCoordinator
) -> None:
    """Pin device identity to the MAC and migrate stale prefixes (#672).

    No-op until a MAC is known (online once, or cached profile). Until then the
    entities use the entry_id placeholder; this runs again on the next setup and
    migrates them as soon as the MAC appears. Idempotent.
    """
    mac = coordinator.device_mac
    if not mac:
        return
    canonical = mac.replace(":", "").lower()

    # 1) Persist the MAC so the identity is stable across IP changes / offline
    #    restarts and independent of live availability.
    if entry.data.get(CONF_MAC) != canonical:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MAC: canonical}
        )

    # 2) Migrate registry rows whose unique_id still carries an old prefix.
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    for row in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        uid = row.unique_id
        if uid == canonical or uid.startswith(f"{canonical}_"):
            continue
        new_uid = _remap_unique_id(uid, canonical, row, dev_reg, coordinator)
        if not new_uid or new_uid == uid:
            continue
        # Collision guard: if the canonical row already exists (e.g. a partial
        # earlier migration), drop the stale duplicate instead of colliding.
        existing = ent_reg.async_get_entity_id(row.domain, DOMAIN, new_uid)
        if existing and existing != row.entity_id:
            _LOGGER.info(
                "miner identity migration: removing stale duplicate %s (%s); "
                "canonical %s already exists",
                row.entity_id, uid, new_uid,
            )
            ent_reg.async_remove(row.entity_id)
        else:
            _LOGGER.info(
                "miner identity migration: %s unique_id %s -> %s",
                row.entity_id, uid, new_uid,
            )
            ent_reg.async_update_entity(row.entity_id, new_unique_id=new_uid)


def _remap_unique_id(uid, canonical, row, dev_reg, coordinator):
    """Return the MAC-prefixed unique_id for a row carrying an old prefix.

    The old prefix is the entity's former _device_unique_id value. Candidates,
    in order: the (DOMAIN, x) identifiers of the entity's device, then the IP and
    the entry_id. The first candidate that the unique_id starts with wins; its
    leading segment is swapped for the canonical MAC. Returns None if no
    candidate matches (then the row is left untouched).
    """
    candidates: list[str] = []
    if row.device_id:
        device = dev_reg.async_get(row.device_id)
        if device:
            candidates.extend(
                ident for domain, ident in device.identifiers if domain == DOMAIN
            )
    candidates.append(coordinator.ip)
    candidates.append(coordinator.entry_id)
    for prefix in candidates:
        if prefix and prefix != canonical and uid.startswith(f"{prefix}_"):
            return f"{canonical}_{uid[len(prefix) + 1:]}"
    return None


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration-orchestrated power services (idempotent).

    These are domain services (not entity services): the target (device/entity/
    area of this integration) is resolved to the owning config entries, and each
    entry's coordinator runs the sequence. Registered once; left in place for the
    lifetime of HA (harmless if all entries are later removed).
    """
    if hass.services.has_service(DOMAIN, SERVICE_POWER_ON):
        return

    async def _coordinators_for(call: ServiceCall) -> list[MinerCoordinator]:
        entry_ids = await async_extract_config_entry_ids(hass, call)
        store = hass.data.get(DOMAIN, {})
        found = [store[eid] for eid in entry_ids if eid in store]
        if not found:
            raise HomeAssistantError(
                "No miner integration targets in the service call. Point the "
                "service at a miner device or one of its entities."
            )
        return found

    async def _handle_power_on(call: ServiceCall) -> None:
        for coordinator in await _coordinators_for(call):
            await coordinator.async_power_on()

    async def _handle_power_off(call: ServiceCall) -> None:
        for coordinator in await _coordinators_for(call):
            await coordinator.async_power_off()

    hass.services.async_register(DOMAIN, SERVICE_POWER_ON, _handle_power_on)
    hass.services.async_register(DOMAIN, SERVICE_POWER_OFF, _handle_power_off)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change (e.g. category toggles, scan interval, password)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
