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

# Severities (from asic-rs MessageSeverity) that count as an active alarm.
_PROBLEM_SEVERITIES = ("Error", "Warning")


@dataclass(frozen=True, kw_only=True)
class MinerSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[MinerData], Any]
    available_fn: Callable[[MinerData], bool] = lambda _: True


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


def _max_temperature(data: MinerData) -> float | None:
    """Hottest temperature reported across all hashboards (board/chip/intake/outlet)."""
    temps = [
        t
        for board in data.hashboards
        for t in (
            board.board_temperature,
            board.chip_temperature,
            board.intake_temperature,
            board.outlet_temperature,
        )
        if t is not None
    ]
    return max(temps) if temps else None


def _min_intake(data: MinerData) -> float | None:
    """Coolest per-board intake temperature (≈ coolant inlet on hydro miners)."""
    temps = [
        b.intake_temperature for b in data.hashboards if b.intake_temperature is not None
    ]
    return min(temps) if temps else None


def _problem_messages(data: MinerData) -> list[str]:
    """Texts of the miner's own Error/Warning messages (its self-assessment)."""
    out: list[str] = []
    for message in data.messages:
        try:
            dumped = message.model_dump()
        except Exception:  # noqa: BLE001 - defensive: never let a sensor crash setup
            continue
        if str(dumped.get("severity")) in _PROBLEM_SEVERITIES:
            text = str(dumped.get("message") or "").strip()
            if text:
                out.append(text)
    return out


def _has_problem(data: MinerData) -> bool:
    return bool(_problem_messages(data))


def _alarm_reason(data: MinerData) -> str:
    """Human-readable alarm reason, aligned to the device's own state and limits.

    ``OK`` when the miner reports nothing. Otherwise the miner's own message(s)
    plus device-aligned context (live inlet/max temps and the configured
    cold/hot limits) so it reads e.g.
    ``Miner state: idle (inlet 16 °C, max 20 °C, start ≥ 17, protect ≥ 78)``.
    No thresholds are invented here — they are read from the miner.
    """
    msgs = _problem_messages(data)
    if not msgs:
        return "OK"
    ctx: list[str] = []
    inlet = _min_intake(data)
    hottest = _max_temperature(data)
    cold = data.min_startup_temperature
    hot = data.restart_temperature
    if inlet is not None:
        ctx.append(f"inlet {inlet:.0f} °C")
    if hottest is not None:
        ctx.append(f"max {hottest:.0f} °C")
    if cold is not None:
        ctx.append(f"start ≥ {cold:.0f}")
    if hot is not None:
        ctx.append(f"protect ≥ {hot:.0f}")
    reason = "; ".join(msgs)
    if ctx:
        reason += " (" + ", ".join(ctx) + ")"
    return reason


# ── Miner-Summary: miner-wide aggregates / statistics ───────────────────────

MINER_SUMMARY_SENSORS: tuple[MinerSensorEntityDescription, ...] = (
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
        key="max_temperature",
        name="Max Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_max_temperature,
        available_fn=lambda d: _max_temperature(d) is not None,
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

# Capability-gated members of Miner-Summary (only created where structurally valid).
_SUMMARY_HYDRO_ONLY = ("fluid_temperature",)


# ── Safety / Diagnose ───────────────────────────────────────────────────────

SAFETY_REASON = MinerSensorEntityDescription(
    key="safety_alarm_reason",
    name="Safety Alarm Reason",
    entity_category=EntityCategory.DIAGNOSTIC,
    value_fn=_alarm_reason,
)
SAFETY_COLD_LIMIT = MinerSensorEntityDescription(
    key="safety_cold_limit",
    name="Safety Cold Limit",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=0,
    value_fn=lambda d: d.min_startup_temperature,
    available_fn=lambda d: d.min_startup_temperature is not None,
)
SAFETY_HOT_LIMIT = MinerSensorEntityDescription(
    key="safety_hot_limit",
    name="Safety Hot Limit",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=0,
    value_fn=lambda d: d.restart_temperature,
    available_fn=lambda d: d.restart_temperature is not None,
)


# ── Per-board sensor factories ──────────────────────────────────────────────


def _board_temp_sensors(n: int) -> list[MinerSensorEntityDescription]:
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
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.chip_temperature),
            available_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: b.chip_temperature
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

    # Structural capabilities (model + firmware), constant for the device — used
    # for capability gating (never a transient value). DeviceInfo is read via
    # model_dump() (pyasic-rs 0.6.0 exposes it that way).
    di = data.device_info.model_dump() if data and data.device_info else {}
    hardware = di.get("hardware") or {}
    expected_boards = hardware.get("boards") or []
    expected_fans = hardware.get("fans")
    is_hydro = di.get("cooling", "Air") != "Air"
    reports_chip_temp = bool(di.get("reports_chip_temperature", False))

    categories = set(
        entry.options.get(CONF_SENSOR_CATEGORIES, DEFAULT_SENSOR_CATEGORIES)
    )
    only_available = entry.options.get(CONF_ONLY_AVAILABLE, DEFAULT_ONLY_AVAILABLE)

    descriptions: list[MinerSensorEntityDescription] = []

    # Miner-wide aggregates / statistics.
    if CAT_MINER_SUMMARY in categories:
        for d in MINER_SUMMARY_SENSORS:
            # fluid temperature only exists on liquid-cooled miners.
            if (
                d.key in _SUMMARY_HYDRO_ONLY
                and only_available
                and not is_hydro
            ):
                continue
            descriptions.append(d)

    # Safety / diagnose: the alarm reason is always meaningful; the cold/hot limit
    # display sensors only where the miner actually reports the configured value.
    if CAT_SAFETY in categories:
        descriptions.append(SAFETY_REASON)
        if not only_available or data.min_startup_temperature is not None:
            descriptions.append(SAFETY_COLD_LIMIT)
        if not only_available or data.restart_temperature is not None:
            descriptions.append(SAFETY_HOT_LIMIT)

    # Per-board sensors — enumerated from the model's expected hardware shape
    # (boot-immune), falling back to live hashboards only for unknown models.
    if CAT_BOARD_TEMPS in categories or CAT_BOARD_PERF in categories:
        positions = (
            list(range(len(expected_boards)))
            if expected_boards
            else [b.position for b in data.hashboards]
        )
        for n in positions:
            if CAT_BOARD_TEMPS in categories:
                for d in _board_temp_sensors(n):
                    # chip temperature only where the firmware reports it.
                    if (
                        d.key == f"board_{n}_chip_temperature"
                        and only_available
                        and not reports_chip_temp
                    ):
                        continue
                    descriptions.append(d)
            if CAT_BOARD_PERF in categories:
                descriptions.extend(_board_perf_sensors(n))

    # Fan RPM sensors.
    if CAT_FANS in categories:
        fan_positions = (
            list(range(expected_fans))
            if expected_fans
            else [f.position for f in data.fans]
        )
        for n in fan_positions:
            descriptions.append(_fan_sensor(n, psu=False))
        for fan in data.psu_fans:
            descriptions.append(_fan_sensor(fan.position, psu=True))

    # Determine the device unique-id prefix (same scheme as MinerEntity) so we can
    # clean up entities for any category/sensor we are no longer producing.
    device_uid = (
        data.mac.replace(":", "").lower() if data and data.mac else coordinator.ip
    )
    keep = {f"{device_uid}_{d.key}" for d in descriptions}
    async_remove_stale_entities(hass, entry, "sensor", keep)

    async_add_entities(MinerSensorEntity(coordinator, desc) for desc in descriptions)
