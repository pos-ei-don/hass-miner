"""Sensor platform for ASIC Miner integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyasic_rs.data import BoardData, HashRateUnit, MinerData

from .const import (
    CAT_BOARD_PERF,
    CAT_BOARD_TEMPS,
    CAT_FANS,
    CAT_MINER_SUMMARY,
    CAT_SAFETY,
    CONF_ONLY_AVAILABLE,
    CONF_SENSOR_CATEGORIES,
    DEFAULT_ONLY_AVAILABLE,
    DEFAULT_SENSOR_CATEGORIES,
    DOMAIN,
)
from .coordinator import MinerCoordinator
from .entity import MinerEntity, async_remove_stale_entities

UNIT_TH_S = "TH/s"
UNIT_J_TH = "J/TH"
UNIT_RPM = "RPM"

# Severities (lowercased) that count as an active safety alarm.
_PROBLEM_SEVERITIES = ("error", "warning")

# Icons for sensors that would otherwise fall back to HA's generic icon.
# device_class sensors (temperature/power/…) keep their nice default icon
# unless overridden here. Per-board/fan keys are matched by suffix below.
_ICONS: dict[str, str] = {
    "hashrate": "mdi:pickaxe",
    "expected_hashrate": "mdi:pickaxe",
    "efficiency": "mdi:gauge",
    "wattage": "mdi:flash",
    "uptime": "mdi:timer-outline",
    "total_chips": "mdi:chip",
    "pool_accepted_shares": "mdi:check",
    "pool_rejected_shares": "mdi:close",
    "pool_url": "mdi:swim",
    "fluid_temperature": "mdi:thermometer-water",
    "water_inlet_min": "mdi:thermometer-water",
    "water_outlet_max": "mdi:thermometer-water",
    "safety_alarm_reason": "mdi:shield-alert",
}
_ICON_SUFFIXES: dict[str, str] = {
    "_hashrate": "mdi:pickaxe",
    "_working_chips": "mdi:chip",
    "_rpm": "mdi:fan",
}


def _icon_for(key: str) -> str | None:
    if key in _ICONS:
        return _ICONS[key]
    for suffix, icon in _ICON_SUFFIXES.items():
        if key.endswith(suffix):
            return icon
    return None


@dataclass(frozen=True, kw_only=True)
class MinerSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[MinerData], Any]
    available_fn: Callable[[MinerData], bool] = lambda _: True


# ── Defensive capability helpers (Schicht B / B1) ───────────────────────────
# These read fields that exist only on the fork wheel; on stock pyasic-rs==0.6.2
# they are absent and return None so the only_available None-gate (A4) decides.


def _cooling_is_hydro(data: MinerData) -> bool | None:
    """True/False if the lib reports a cooling type, else None (unknown)."""
    cooling = getattr(getattr(data, "device_info", None), "cooling", None)
    if cooling is None:
        return None  # unknown on this lib version -> let only_available decide
    return str(cooling).lower() in ("hydro", "water", "liquid")


def _reports_chip_temp(data: MinerData) -> bool | None:
    """Lib's reports_chip_temperature flag, or None if absent on this lib."""
    return getattr(getattr(data, "device_info", None), "reports_chip_temperature", None)


# ── shared data helpers ─────────────────────────────────────────────────────


def _primary_pool(data: MinerData):
    """Return the first active pool across all pool groups, or the very first pool."""
    for group in data.pools:
        for pool in group.pools:
            if pool.active:
                return pool
    for group in data.pools:
        if group.pools:
            return group.pools[0]
    return None


def _primary_pool_accepted(data: MinerData) -> int | None:
    pool = _primary_pool(data)
    return pool.accepted_shares if pool else None


def _primary_pool_rejected(data: MinerData) -> int | None:
    pool = _primary_pool(data)
    return pool.rejected_shares if pool else None


def _primary_pool_url(data: MinerData) -> str | None:
    pool = _primary_pool(data)
    return pool.url if pool else None


def _board_value(
    data: MinerData, position: int, getter: Callable[[BoardData], Any]
) -> Any:
    for board in data.hashboards:
        if board.position == position:
            return getter(board)
    return None


def _fan_rpm(data: MinerData, position: int, psu: bool) -> float | None:
    fans = data.psu_fans if psu else data.fans
    for fan in fans:
        if fan.position == position:
            return fan.rpm
    return None


def _min_intake(data: MinerData) -> float | None:
    """Coolest per-board intake temperature (≈ coolant inlet on hydro miners)."""
    temps = [
        b.intake_temperature for b in data.hashboards if b.intake_temperature is not None
    ]
    return min(temps) if temps else None


def _max_outlet(data: MinerData) -> float | None:
    """Warmest per-board outlet temperature (≈ coolant return on hydro miners)."""
    temps = [
        b.outlet_temperature for b in data.hashboards if b.outlet_temperature is not None
    ]
    return max(temps) if temps else None


def _max_temperature(data: MinerData) -> float | None:
    """Hottest temperature reported across boards plus miner-wide temps.

    ``chip_temperature`` is read defensively (absent on stock 0.6.2).
    """
    temps: list[float] = []
    for board in data.hashboards:
        for t in (
            board.board_temperature,
            getattr(board, "chip_temperature", None),
            board.intake_temperature,
            board.outlet_temperature,
        ):
            if t is not None:
                temps.append(t)
    for t in (data.average_temperature, data.fluid_temperature):
        if t is not None:
            temps.append(t)
    return max(temps) if temps else None


# ── Safety helpers (Schicht B / B2) ─────────────────────────────────────────
# All lib-dependent reads are defensive: with no messages and no thermal limits
# on stock 0.6.2 these reduce to _has_problem -> False and _alarm_reason -> "OK".


def _problem_messages(data: MinerData) -> list[str]:
    """Texts of the miner's own Error/Warning messages (its self-assessment).

    Defensive about both the presence of ``data.messages`` and the message
    object's shape: severity via ``.severity``, text via ``.message``/``.text``.
    """
    out: list[str] = []
    messages = getattr(data, "messages", None) or []
    for message in messages:
        try:
            severity = str(getattr(message, "severity", "") or "").lower()
            if severity not in _PROBLEM_SEVERITIES:
                continue
            text = getattr(message, "message", None)
            if text is None:
                text = getattr(message, "text", None)
            text = str(text or "").strip()
            if text:
                out.append(text)
            elif severity:
                out.append(severity)
        except Exception:  # noqa: BLE001 - never let a sensor crash setup/update
            continue
    return out


def _has_problem(data: MinerData) -> bool:
    return bool(_problem_messages(data))


def _alarm_reason(data: MinerData) -> str:
    """Human-readable alarm reason, aligned to device messages and thermal limits.

    ``OK`` when the miner reports nothing actionable. Device-read limits
    (``min_startup_temperature`` / ``restart_temperature``) and live temperatures
    are read defensively, so on stock 0.6.2 (no messages, no limits) this is "OK".
    """
    reasons: list[str] = list(_problem_messages(data))

    hot = getattr(data, "restart_temperature", None)
    cold = getattr(data, "min_startup_temperature", None)
    maxtemp = _max_temperature(data)
    inlet = _min_intake(data)

    if hot is not None and maxtemp is not None and maxtemp >= hot:
        reasons.append(f"too hot ({maxtemp:.0f} °C ≥ {hot:.0f} °C)")
    if cold is not None and inlet is not None and inlet < cold:
        reasons.append(
            f"inlet water too cold (mining won't start) "
            f"({inlet:.0f} °C < {cold:.0f} °C)"
        )

    if not reasons:
        return "OK"
    return "; ".join(reasons)


# ── Miner-Summary: miner-wide aggregates / statistics (CAT_MINER_SUMMARY) ────

MINER_SENSORS: tuple[MinerSensorEntityDescription, ...] = (
    MinerSensorEntityDescription(
        key="hashrate",
        name="Hashrate",
        native_unit_of_measurement=UNIT_TH_S,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: (
            round(d.hashrate.into_unit(HashRateUnit.TH).value, 4) if d.hashrate else None
        ),
        available_fn=lambda d: d.hashrate is not None,
    ),
    MinerSensorEntityDescription(
        key="expected_hashrate",
        name="Expected Hashrate",
        native_unit_of_measurement=UNIT_TH_S,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: (
            round(d.expected_hashrate.into_unit(HashRateUnit.TH).value, 4)
            if d.expected_hashrate
            else None
        ),
        available_fn=lambda d: d.expected_hashrate is not None,
    ),
    MinerSensorEntityDescription(
        key="average_temperature",
        name="Average Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.average_temperature,
        available_fn=lambda d: d.average_temperature is not None,
    ),
    MinerSensorEntityDescription(
        key="fluid_temperature",
        name="Fluid Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.fluid_temperature,
        available_fn=lambda d: d.fluid_temperature is not None,
    ),
    # Coolant in/out aggregates across all boards (hydro only): coldest inlet and
    # hottest outlet — the safety-relevant water extremes, without per-board entities.
    MinerSensorEntityDescription(
        key="water_inlet_min",
        name="Water Inlet (min)",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_min_intake,
        available_fn=lambda d: _min_intake(d) is not None,
    ),
    MinerSensorEntityDescription(
        key="water_outlet_max",
        name="Water Outlet (max)",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_max_outlet,
        available_fn=lambda d: _max_outlet(d) is not None,
    ),
    MinerSensorEntityDescription(
        key="wattage",
        name="Power Consumption",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.wattage,
        available_fn=lambda d: d.wattage is not None,
    ),
    MinerSensorEntityDescription(
        key="efficiency",
        name="Efficiency",
        native_unit_of_measurement=UNIT_J_TH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.efficiency,
        available_fn=lambda d: d.efficiency is not None,
    ),
    MinerSensorEntityDescription(
        key="uptime",
        name="Uptime",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=lambda d: int(d.uptime.total_seconds()) if d.uptime else None,
        available_fn=lambda d: d.uptime is not None,
    ),
    MinerSensorEntityDescription(
        key="total_chips",
        name="Total Active Chips",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.total_chips,
        available_fn=lambda d: d.total_chips is not None,
    ),
    MinerSensorEntityDescription(
        key="pool_accepted_shares",
        name="Pool Accepted Shares",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_primary_pool_accepted,
        available_fn=lambda d: _primary_pool_accepted(d) is not None,
    ),
    MinerSensorEntityDescription(
        key="pool_rejected_shares",
        name="Pool Rejected Shares",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_primary_pool_rejected,
        available_fn=lambda d: _primary_pool_rejected(d) is not None,
    ),
    MinerSensorEntityDescription(
        key="pool_url",
        name="Active Pool",
        value_fn=_primary_pool_url,
        available_fn=lambda d: _primary_pool_url(d) is not None,
    ),
)

# Members of Miner-Summary that only make sense on liquid-cooled miners. When the
# lib reports cooling we trust it; otherwise the only_available None-gate decides.
_SUMMARY_HYDRO_ONLY = ("fluid_temperature", "water_inlet_min", "water_outlet_max")


# ── Safety / Diagnose (CAT_SAFETY) ──────────────────────────────────────────

SAFETY_REASON = MinerSensorEntityDescription(
    key="safety_alarm_reason",
    name="Safety Alarm Reason",
    entity_category=EntityCategory.DIAGNOSTIC,
    icon="mdi:shield-alert",
    value_fn=_alarm_reason,
    available_fn=lambda _: True,
)


# ── Per-board sensor factories ──────────────────────────────────────────────


def _board_temp_sensors(n: int) -> list[MinerSensorEntityDescription]:
    """Per-board temperature sensors (CAT_BOARD_TEMPS).

    ``board_{n}_chip_temperature`` (B3) reads ``chip_temperature`` defensively;
    on stock 0.6.2 the field is absent so its value is None and only_available
    drops it.
    """
    return [
        MinerSensorEntityDescription(
            key=f"board_{n}_board_temperature",
            name=f"Board {n} Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.board_temperature),
            available_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: b.board_temperature
            )
            is not None,
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_chip_temperature",
            name=f"Board {n} Chip Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: getattr(b, "chip_temperature", None)
            ),
            available_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: getattr(b, "chip_temperature", None)
            )
            is not None,
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_intake_temperature",
            name=f"Board {n} Intake Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.intake_temperature),
            available_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: b.intake_temperature
            )
            is not None,
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_outlet_temperature",
            name=f"Board {n} Outlet Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.outlet_temperature),
            available_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: b.outlet_temperature
            )
            is not None,
        ),
    ]


def _board_perf_sensors(n: int) -> list[MinerSensorEntityDescription]:
    """Per-board performance sensors (CAT_BOARD_PERF)."""
    return [
        MinerSensorEntityDescription(
            key=f"board_{n}_hashrate",
            name=f"Board {n} Hashrate",
            native_unit_of_measurement=UNIT_TH_S,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
            value_fn=lambda d, _n=n: _board_value(
                d,
                _n,
                lambda b: (
                    round(b.hashrate.into_unit(HashRateUnit.TH).value, 4)
                    if b.hashrate
                    else None
                ),
            ),
            available_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.hashrate)
            is not None,
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_working_chips",
            name=f"Board {n} Working Chips",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.working_chips),
            available_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.working_chips)
            is not None,
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_frequency",
            name=f"Board {n} Frequency",
            native_unit_of_measurement=UnitOfFrequency.MEGAHERTZ,
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.frequency),
            available_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.frequency)
            is not None,
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_voltage",
            name=f"Board {n} Voltage",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.voltage),
            available_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.voltage)
            is not None,
        ),
    ]


def _fan_sensor(position: int, psu: bool = False) -> MinerSensorEntityDescription:
    prefix = "PSU Fan" if psu else "Fan"
    key_prefix = "psu_fan" if psu else "fan"
    return MinerSensorEntityDescription(
        key=f"{key_prefix}_{position}_rpm",
        name=f"{prefix} {position} RPM",
        native_unit_of_measurement=UNIT_RPM,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d, _p=position, _psu=psu: _fan_rpm(d, _p, _psu),
        available_fn=lambda d, _p=position, _psu=psu: _fan_rpm(d, _p, _psu) is not None,
    )


# ── Entity class ────────────────────────────────────────────────────────────


class MinerSensorEntity(MinerEntity, SensorEntity):
    entity_description: MinerSensorEntityDescription

    def __init__(
        self,
        coordinator: MinerCoordinator,
        description: MinerSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._device_unique_id}_{description.key}"
        if description.icon is None:
            icon = _icon_for(description.key)
            if icon is not None:
                self._attr_icon = icon

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success or self.coordinator.data is None:
            return False
        return self.entity_description.available_fn(self.coordinator.data)


# ── Platform setup ──────────────────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MinerCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data

    categories = set(
        entry.options.get(CONF_SENSOR_CATEGORIES, DEFAULT_SENSOR_CATEGORIES)
    )
    only_available = entry.options.get(CONF_ONLY_AVAILABLE, DEFAULT_ONLY_AVAILABLE)

    # Capability gates (Schicht B / B1), defensive: True/False when the lib knows,
    # None on stock 0.6.2 so the only_available None-gate decides.
    is_hydro = _cooling_is_hydro(data)
    reports_chip = _reports_chip_temp(data)

    descriptions: list[MinerSensorEntityDescription] = []

    # Miner-wide aggregates / statistics.
    if CAT_MINER_SUMMARY in categories:
        for d in MINER_SENSORS:
            # Hydro-only members: drop only when the lib positively says "not hydro".
            if d.key in _SUMMARY_HYDRO_ONLY and is_hydro is False:
                continue
            descriptions.append(d)

    # Safety / diagnose: the alarm reason text (always meaningful, "OK" by default).
    if CAT_SAFETY in categories:
        descriptions.append(SAFETY_REASON)

    # Per-board sensors, matched to the live hashboards by position.
    if CAT_BOARD_TEMPS in categories or CAT_BOARD_PERF in categories:
        positions = [b.position for b in data.hashboards]
        for n in positions:
            if CAT_BOARD_TEMPS in categories:
                for d in _board_temp_sensors(n):
                    # Chip temp: drop only when the lib positively says it isn't reported.
                    if (
                        d.key == f"board_{n}_chip_temperature"
                        and reports_chip is False
                    ):
                        continue
                    descriptions.append(d)
            if CAT_BOARD_PERF in categories:
                descriptions.extend(_board_perf_sensors(n))

    # Fan RPM sensors.
    if CAT_FANS in categories:
        for fan in data.fans:
            descriptions.append(_fan_sensor(fan.position, psu=False))
        for fan in data.psu_fans:
            descriptions.append(_fan_sensor(fan.position, psu=True))

    # only_available gate (A4): drop descriptions whose value is unavailable now.
    if only_available:
        descriptions = [d for d in descriptions if d.available_fn(data)]

    # Clean up entities of any category/sensor we are no longer producing
    # (same unique-id scheme as MinerEntity).
    device_uid = (
        data.mac.replace(":", "").lower() if data and data.mac else coordinator.ip
    )
    keep = {f"{device_uid}_{d.key}" for d in descriptions}
    async_remove_stale_entities(hass, entry, "sensor", keep)

    async_add_entities(MinerSensorEntity(coordinator, desc) for desc in descriptions)
