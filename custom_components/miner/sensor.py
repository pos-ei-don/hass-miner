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
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pyasic_rs.data import BoardData, MinerData, HashRateUnit

from .const import (
    CONF_ONLY_AVAILABLE,
    CONF_SENSOR_DETAIL,
    DEFAULT_ONLY_AVAILABLE,
    DEFAULT_SENSOR_DETAIL,
    DETAIL_BOARD_INDIVIDUAL,
    DETAIL_BOARD_SUMMARY,
    DETAIL_DEBUG,
    DOMAIN,
)
from .coordinator import MinerCoordinator
from .entity import MinerEntity

UNIT_TH_S = "TH/s"
UNIT_J_TH = "J/TH"
UNIT_RPM = "RPM"


@dataclass(frozen=True, kw_only=True)
class MinerSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[MinerData], Any]
    available_fn: Callable[[MinerData], bool] = lambda _: True


# ── Top-level miner sensors ────────────────────────────────────────────────

MINER_SENSORS: tuple[MinerSensorEntityDescription, ...] = (
    MinerSensorEntityDescription(
        key="hashrate",
        name="Hashrate",
        native_unit_of_measurement=UNIT_TH_S,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: (
            round(d.hashrate.into_unit(HashRateUnit.TH).value, 4)
            if d.hashrate
            else None
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
    # Hottest temperature across all hashboards (board / chip / intake / outlet).
    # This is the safety-relevant aggregate: it is ALWAYS created — never gated by
    # detail level or capability — so an over-temperature alarm can watch a single
    # entity regardless of whether the individual per-board temperatures are shown.
    # It reads the polled board data directly, so it works even when no per-board
    # entities exist (e.g. "summary" detail).
    MinerSensorEntityDescription(
        key="max_temperature",
        name="Max Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: _max_temperature(d),
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
        value_fn=lambda d: _primary_pool_accepted(d),
        available_fn=lambda d: _primary_pool_accepted(d) is not None,
    ),
    MinerSensorEntityDescription(
        key="pool_rejected_shares",
        name="Pool Rejected Shares",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: _primary_pool_rejected(d),
        available_fn=lambda d: _primary_pool_rejected(d) is not None,
    ),
    MinerSensorEntityDescription(
        key="pool_url",
        name="Active Pool",
        value_fn=lambda d: _primary_pool_url(d),
        available_fn=lambda d: _primary_pool_url(d) is not None,
    ),
)


def _primary_pool(data: MinerData):
    """Return the first active pool across all pool groups, or the very first pool."""
    for group in data.pools:
        for pool in group.pools:
            if pool.active:
                return pool
    # Fall back to first pool if none are marked active
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


def _max_temperature(data: MinerData) -> float | None:
    """Hottest temperature reported across all hashboards.

    Considers every per-board temperature reading (board, chip, intake, outlet)
    so an over-temperature alarm can compare this single value against the
    miner's configured threshold, independent of which sensors are surfaced.
    """
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


# ── Per-board sensor factories ──────────────────────────────────────────────


# Keys (per board, with the ``board_{n}_`` prefix stripped) that make up the
# reduced "board summary" set — the values most users actually watch per board.
_BOARD_SUMMARY_KEYS = ("hashrate", "board_temperature", "chip_temperature")


def _board_sensors(
    position: int, reduced: bool = False, include_chip_temp: bool = True
) -> list[MinerSensorEntityDescription]:
    n = position
    descriptions = [
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
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.hashrate) is not None
            ),
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_board_temperature",
            name=f"Board {n} Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.board_temperature),
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.board_temperature) is not None
            ),
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_chip_temperature",
            name=f"Board {n} Chip Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.chip_temperature),
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.chip_temperature) is not None
            ),
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_intake_temperature",
            name=f"Board {n} Intake Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: b.intake_temperature
            ),
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.intake_temperature) is not None
            ),
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_outlet_temperature",
            name=f"Board {n} Outlet Temperature",
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda d, _n=n: _board_value(
                d, _n, lambda b: b.outlet_temperature
            ),
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.outlet_temperature) is not None
            ),
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_working_chips",
            name=f"Board {n} Working Chips",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.working_chips),
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.working_chips) is not None
            ),
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_frequency",
            name=f"Board {n} Frequency",
            native_unit_of_measurement=UnitOfFrequency.MEGAHERTZ,
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.frequency),
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.frequency) is not None
            ),
        ),
        MinerSensorEntityDescription(
            key=f"board_{n}_voltage",
            name=f"Board {n} Voltage",
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
            value_fn=lambda d, _n=n: _board_value(d, _n, lambda b: b.voltage),
            available_fn=lambda d, _n=n: (
                _board_value(d, _n, lambda b: b.voltage) is not None
            ),
        ),
    ]
    if reduced:
        suffixes = tuple(f"board_{n}_{k}" for k in _BOARD_SUMMARY_KEYS)
        descriptions = [d for d in descriptions if d.key in suffixes]
    if not include_chip_temp:
        descriptions = [
            d for d in descriptions if d.key != f"board_{n}_chip_temperature"
        ]
    return descriptions


def _board_value(
    data: MinerData, position: int, getter: Callable[[BoardData], Any]
) -> Any:
    for board in data.hashboards:
        if board.position == position:
            return getter(board)
    return None


# ── Per-fan sensor factories ────────────────────────────────────────────────


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


def _fan_rpm(data: MinerData, position: int, psu: bool) -> float | None:
    fans = data.psu_fans if psu else data.fans
    for fan in fans:
        if fan.position == position:
            return fan.rpm
    return None


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

    # Capability gating is driven purely by what the device *structurally*
    # supports (model + firmware), exposed on DeviceInfo and constant for a given
    # device. This is deterministic and boot-immune: the create/don't-create
    # decision never depends on a (possibly transient) live value, so a sensor
    # that is momentarily ``None`` right after the miner boots still gets its
    # entity — it simply reports ``unavailable`` until the value arrives. There is
    # no value-snapshot fallback.
    # pyasic-rs 0.6.0: DeviceInfo exposes fields via model_dump(), not attrs.
    di = data.device_info.model_dump() if data and data.device_info else {}
    hardware = di.get("hardware") or {}
    expected_boards = hardware.get("boards") or []
    expected_fans = hardware.get("fans")
    # Air vs Hydro/Immersion — only liquid-cooled miners have a fluid temperature.
    is_hydro = di.get("cooling", "Air") != "Air"
    # Firmware-dependent: only some firmwares (e.g. VNish) report chip temps.
    reports_chip_temp = bool(di.get("reports_chip_temperature", False))

    detail = entry.options.get(CONF_SENSOR_DETAIL, DEFAULT_SENSOR_DETAIL)
    only_available = entry.options.get(CONF_ONLY_AVAILABLE, DEFAULT_ONLY_AVAILABLE)
    # Debug shows everything regardless of capability; otherwise the "only
    # type-relevant sensors" toggle (default on) applies the structural gates.
    apply_caps = only_available and detail != DETAIL_DEBUG

    # Top-level sensors. The only capability-gated one is the fluid temperature.
    descriptions: list[MinerSensorEntityDescription] = [
        d
        for d in MINER_SENSORS
        if d.key != "fluid_temperature" or is_hydro or not apply_caps
    ]

    # Per-board sensors. ``summary`` keeps only the miner-wide aggregates above;
    # the more verbose levels add per-board entities. Boards are enumerated from
    # the model's *expected* hardware shape (boot-immune), falling back to the
    # live hashboards only for genuinely-unknown models with no static shape.
    if detail in (DETAIL_BOARD_SUMMARY, DETAIL_BOARD_INDIVIDUAL, DETAIL_DEBUG):
        reduced = detail == DETAIL_BOARD_SUMMARY
        include_chip_temp = reports_chip_temp or not apply_caps
        positions = (
            range(len(expected_boards))
            if expected_boards
            else [b.position for b in data.hashboards]
        )
        for position in positions:
            descriptions.extend(
                _board_sensors(
                    position, reduced=reduced, include_chip_temp=include_chip_temp
                )
            )

    # Fan RPM sensors are per-board-individual / debug only. Enumerated from the
    # expected fan count (boot-immune), falling back to live fans if unknown.
    if detail in (DETAIL_BOARD_INDIVIDUAL, DETAIL_DEBUG):
        fan_positions = (
            range(expected_fans)
            if expected_fans
            else [f.position for f in data.fans]
        )
        for position in fan_positions:
            descriptions.append(_fan_sensor(position, psu=False))
        for fan in data.psu_fans:
            descriptions.append(_fan_sensor(fan.position, psu=True))

    async_add_entities(MinerSensorEntity(coordinator, desc) for desc in descriptions)
