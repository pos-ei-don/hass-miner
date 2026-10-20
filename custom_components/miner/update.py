"""Firmware-update entity for ASIC miners.

Surfaces the installed firmware version plus, where determinable, whether a newer
firmware is available. Two sources, both slow (~daily self-poll, not the fast
telemetry coordinator):

* **BraiinsOS** — the library does a local-API check (`asic-rs
  check_firmware_update` → `bos.checkForUpgrade`), returning the latest release.
* **VNish** — there is no local update-availability field; VNish's own GUI
  compares the installed version against the vendor's cloud changelog. We do the
  same **consumer-side** (the lib stays cloud-free): fetch the release list from
  the vendor and take the latest stable, then let Home Assistant compare it
  against the installed version.

Install: where the library can flash the firmware (``supports_upgrade_firmware``,
e.g. BraiinsOS 26.x) and the check reported a downloadable image
(``FirmwareUpdate.Remote``), the entity offers Install: the image is downloaded
to a temporary file and handed to ``miner.upgrade_firmware``; the miner reboots.

Timing: the first check runs as soon as the miner answers after HA start (not
only after SCAN_INTERVAL, which restarts with every HA restart), then daily.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .control import async_miner_control
from .coordinator import MinerCoordinator
from .entity import MinerEntity, async_remove_stale_entities

_LOGGER = logging.getLogger(__name__)

# The vendor check is expensive (hits a release server) — poll it ~once a day.
SCAN_INTERVAL = timedelta(hours=24)

# VNish/Hashcore release changelog (consumer-side cloud check). Same source the
# VNish web UI uses to mark "are you up to date?".
_VNISH_RELEASES_URL = "https://partner.anthill.farm/api/client/releases-notes"
_VNISH_RELEASE_NOTES_URL = "https://docs.hashcore.com/firmware/about"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Firmware images are ~100 MB from the vendor feed.
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=900, sock_read=120)
_DOWNLOAD_CHUNK = 1024 * 1024
# A failed automatic check (miner/release server down) is retried after this.
_RETRY_AFTER = 600
# Install + reboot; Install stays blocked until the firmware changed or this passed.
_UPGRADE_TIMEOUT = 30 * 60


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable int tuple ('1.3.4' -> (1,3,4))."""
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


async def _fetch_vnish_latest_stable(
    session: aiohttp.ClientSession,
) -> str | None:
    """Latest stable VNish firmware version from the vendor changelog, or None.

    The version numbers are unified across miner series, so we take the global
    max stable version. Fully defensive: any failure returns None.
    """
    try:
        async with session.get(_VNISH_RELEASES_URL, timeout=_HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            releases = await resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(releases, list):
        return None
    stable = [
        str(r.get("version"))
        for r in releases
        if isinstance(r, dict)
        and str(r.get("stage", "")).lower() == "stable"
        and r.get("version")
    ]
    if not stable:
        return None
    return max(stable, key=_version_tuple)


def _register_services() -> None:
    """Register the manual firmware-update-check service (idempotent)."""
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "check_firmware_update",
        {},
        "async_check_firmware_update",
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware-update entity for miners where we can determine it."""
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]

    _register_services()

    is_vnish = coordinator.is_vnish or bool(
        coordinator.profile and coordinator.profile.get("is_vnish")
    )

    # Capability-gated (orglib branch): the native local update check
    # (miner.check_firmware_update) exists only on the fork wheel, so on upstream
    # PyPI pyasic-rs==0.8.0 supports_check_firmware_update is False. VNish miners
    # still get the entity because the update-availability check for them is the
    # vendor-cloud changelog path (_vnish_cloud, see async_update) — that path has
    # NO library dependency and works on the upstream lib. A non-VNish miner on the
    # upstream lib therefore shows no firmware-update entity (no native check, no
    # crash). TODO(orglib): a BOS-local shim could restore the non-VNish check.
    entities: list[MinerEntity] = []
    if coordinator.supports_check_firmware_update or is_vnish:
        entities.append(MinerFirmwareUpdate(coordinator, vnish_cloud=is_vnish))

    async_remove_stale_entities(
        hass, entry, "update", [e.unique_id for e in entities]
    )
    async_add_entities(entities)


class MinerFirmwareUpdate(MinerEntity, UpdateEntity):
    """Installed firmware + (where determinable) available-update indicator."""

    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MinerCoordinator, *, vnish_cloud: bool = False) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device_unique_id}_firmware_update"
        # VNish has no local update check; fall back to the vendor cloud changelog
        # only when the lib can't do it locally.
        self._vnish_cloud = vnish_cloud and not coordinator.supports_check_firmware_update
        self._latest_version: str | None = None
        self._checked: tuple[str | None, str | None] = (None, None)
        # Download URL of the latest image (FirmwareUpdate.Remote), if any.
        self._firmware_url: str | None = None
        # Auto check: right after start (as soon as the miner answers) and after
        # a firmware change, instead of only every SCAN_INTERVAL. A failed auto
        # check waits _RETRY_AFTER before the next try.
        self._checked_once = False
        self._retry_at = 0.0
        self._check_lock = asyncio.Lock()
        self._check_task: asyncio.Task | None = None
        # Running upgrade: (raw firmware string before, monotonic deadline).
        self._upgrade: tuple[str | None, float] | None = None
        self._release_url: str | None = (
            _VNISH_RELEASE_NOTES_URL if self._vnish_cloud else None
        )
        self._apply_naming("update")

    @property
    def supported_features(self) -> UpdateEntityFeature:
        if self.coordinator.supports_upgrade_firmware and self._firmware_url:
            return UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
        return UpdateEntityFeature(0)

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        if not self.coordinator.last_update_success or self.coordinator.data is None:
            return
        raw = self._raw_installed()
        if self._upgrade is not None:
            before, deadline = self._upgrade
            if raw == before and time.monotonic() < deadline:
                return  # still installing / rebooting
            if raw == before:
                _LOGGER.warning(
                    "firmware upgrade on %s: version unchanged after %d min",
                    self.coordinator.ip,
                    _UPGRADE_TIMEOUT // 60,
                )
            self._upgrade = None
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self._checked_once = False
            self._retry_at = 0.0
            self.async_write_ha_state()
        stale = self._checked[0] is not None and self._checked[0] != raw
        if (
            (not self._checked_once or stale)
            and time.monotonic() >= self._retry_at
            and (self._check_task is None or self._check_task.done())
        ):
            self._check_task = self.hass.async_create_task(self._async_auto_check())

    async def _async_auto_check(self) -> None:
        if not await self._async_check():
            self._retry_at = time.monotonic() + _RETRY_AFTER
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._check_task is not None and not self._check_task.done():
            self._check_task.cancel()
        await super().async_will_remove_from_hass()

    @property
    def should_poll(self) -> bool:
        # Self-poll on SCAN_INTERVAL for the slow vendor check (the base
        # CoordinatorEntity is otherwise push-only).
        return True

    def _raw_installed(self) -> str | None:
        data = self.coordinator.data
        return getattr(data, "firmware_version", None) if data else None

    @property
    def installed_version(self) -> str | None:
        # Prefer the lib-normalised version from the last check, as long as the
        # raw firmware string has not changed since (else it is stale).
        raw = self._raw_installed()
        checked_raw, checked = self._checked
        if checked and checked_raw == raw:
            return checked
        return raw

    @property
    def latest_version(self) -> str | None:
        # Fall back to the installed version so the entity reads "up to date"
        # rather than "unknown" until/unless a newer one is reported.
        # A firmware change since the last check makes its result stale.
        checked_raw = self._checked[0]
        if checked_raw is not None and checked_raw != self._raw_installed():
            return self.installed_version
        return self._latest_version or self.installed_version

    @property
    def release_url(self) -> str | None:
        return self._release_url

    async def async_update(self) -> None:
        """Determine the latest available firmware (slow path, SCAN_INTERVAL)."""
        await self._async_check()

    async def _async_check(self) -> bool:
        """Run one check; concurrent callers share it. True if it got a result."""
        if self._upgrade is not None:
            return False  # miner is installing/rebooting
        if self._check_lock.locked():
            async with self._check_lock:
                return self._checked_once
        async with self._check_lock:
            return await self._async_check_locked()

    async def _async_check_locked(self) -> bool:
        if self._vnish_cloud:
            session = async_get_clientsession(self.coordinator.hass)
            latest = await _fetch_vnish_latest_stable(session)
            if not latest:
                return False
            self._latest_version = latest
            self._checked_once = True
            return True

        miner = self.coordinator.miner
        if miner is None:
            return False
        raw = self._raw_installed()  # the version this result belongs to
        try:
            result = await miner.check_firmware_update()
        except Exception as err:  # noqa: BLE001 — never let the UI crash on this
            _LOGGER.debug(
                "firmware update check failed for %s: %s", self.coordinator.ip, err
            )
            return False
        if result is None:
            return False
        self._checked_once = True
        # asic-rs 0.8.6 FirmwareStats: both versions normalised by the lib
        # (BOS "2026-09-11-…-26.09-plus" -> "26.9"), plus update_available.
        current = getattr(result, "current_version", None)
        latest = getattr(result, "latest_version", None)
        self._checked = (raw, current)
        if getattr(result, "update_available", None) is False:
            latest = current
        self._latest_version = latest
        self._release_url = getattr(result, "release_url", None)
        firmware = getattr(result, "firmware", None)
        self._firmware_url = (
            getattr(firmware, "url", None)
            if getattr(firmware, "variant", None) == "remote"
            else None
        )
        return True

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Download the latest image and flash it via the library.

        Stays "in progress" after the library accepted the image until the
        coordinator reports a different firmware (reboot done) or
        _UPGRADE_TIMEOUT passes - Install is not offered again meanwhile.
        """
        url = self._firmware_url
        if not url:
            raise HomeAssistantError(
                "No firmware image known - run the update check first"
            )
        if self._upgrade is not None:
            raise HomeAssistantError("A firmware upgrade is already running")
        name = os.path.basename(url.split("?", 1)[0]) or "firmware.tar"
        started = False
        self._attr_in_progress = True
        self._attr_update_percentage = 0
        self.async_write_ha_state()
        try:
            tmpdir = await self.hass.async_add_executor_job(
                tempfile.mkdtemp, None, "miner-fw-"
            )
            try:
                path = os.path.join(tmpdir, name)
                await self._async_download(url, path)
                self._attr_update_percentage = None  # flashing: no progress
                self.async_write_ha_state()
                raw_before = self._raw_installed()
                await async_miner_control(
                    "Firmware upgrade",
                    self.coordinator.miner,
                    lambda m: m.upgrade_firmware(path),
                )
                started = True
                self._upgrade = (raw_before, time.monotonic() + _UPGRADE_TIMEOUT)
                _LOGGER.info(
                    "firmware upgrade to %s started on %s, miner reboots",
                    self._latest_version,
                    self.coordinator.ip,
                )
            finally:
                await self.hass.async_add_executor_job(shutil.rmtree, tmpdir, True)
        finally:
            if not started:
                self._attr_in_progress = False
                self._attr_update_percentage = None
            self.async_write_ha_state()

    async def _async_download(self, url: str, path: str) -> None:
        """Stream the image to ``path``, reporting progress."""
        session = async_get_clientsession(self.hass)
        handle = await self.hass.async_add_executor_job(open, path, "wb")
        try:
            async with session.get(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
                if resp.status != 200:
                    raise HomeAssistantError(
                        f"Firmware download failed: HTTP {resp.status} for {url}"
                    )
                total = resp.content_length or 0
                done = 0
                async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
                    await self.hass.async_add_executor_job(handle.write, chunk)
                    done += len(chunk)
                    if total:
                        pct = min(99, int(done * 100 / total))
                        if pct != self._attr_update_percentage:
                            self._attr_update_percentage = pct
                            self.async_write_ha_state()
                if total and done != total:
                    raise HomeAssistantError(
                        f"Firmware download incomplete: {done} of {total} bytes"
                    )
        except aiohttp.ClientError as err:
            raise HomeAssistantError(f"Firmware download failed: {err}") from err
        finally:
            # the caller removes the whole temp directory
            await self.hass.async_add_executor_job(handle.close)

    async def async_check_firmware_update(self) -> dict:
        """Service `miner.check_firmware_update`: force a check now, return result.

        Triggers the (otherwise ~daily) check immediately and writes state, so the
        user doesn't have to wait for the next poll. Returns installed/latest and
        whether an update is available.
        """
        await self.async_update()
        self.async_write_ha_state()
        installed = self.installed_version
        return {
            "installed_version": installed,
            "latest_version": self.latest_version,
            "update_available": bool(
                self._latest_version and self._latest_version != installed
            ),
            "release_url": self._release_url,
        }

