"""Lifecycle-status state machine for the ASIC Miner integration.

Pure, dependency-free logic so it can be unit-tested offline (no Home Assistant,
no pyasic-rs, no network). The sensor entity (``sensor.py``) gathers the raw
inputs from the coordinator and live ``MinerData`` into a :class:`StatusInputs`
and calls :func:`compute_status`; this module never touches HA or the library.

The precedence is top-down, first match wins — see ``compute_status``. It mirrors
the design 1:1; where a state cannot be *proven* from the asic-rs 0.8.0.1 API it
is derived heuristically from hashrate / temperature / external watts, and those
spots are called out in the module docstring below and in the delivery report.

Provenance of the derivations (asic-rs 0.8.0.1, ``pyasic_rs/asic_rs.pyi``):
  * ``is_mining``           -> MinerData.is_mining            (.pyi line 597)
  * ``hashrate``            -> MinerData.hashrate             (.pyi line 591)
  * ``expected_hashrate``   -> MinerData.expected_hashrate    (.pyi line 581)
  * ``wattage``             -> MinerData.wattage              (.pyi line 634)
  * danger limit            -> TemperatureConfig.danger       (.pyi line 953)
  * throttle (VNish)        -> coordinator.vnish_throttle (vnish /summary)
  * throttle (other fw)     -> MinerData.tuning_percent       (.pyi line 628)
  * board active / chips    -> BoardData.active/working_chips (.pyi lines 17/48)

Not provable from the API, therefore intentionally NOT auto-derived:
  * ``updating`` — asic-rs exposes ``check_firmware_update`` (whether an update
    is AVAILABLE) but no "an upgrade is in progress" flag. The state stays a
    valid enum option but is never emitted automatically. See report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import (
    STATUS_FAULT,
    STATUS_MINING,
    STATUS_OFF,
    STATUS_OVERHEATING,
    STATUS_PAUSED,
    STATUS_STARTING,
    STATUS_STOPPING,
    STATUS_UNKNOWN,
    STATUS_UPDATING,
    STATUS_WARMING_UP,
)


@dataclass(frozen=True)
class StatusInputs:
    """Everything the state machine needs, already extracted from HA/lib."""

    # External power (from the configured power_sensor). ``power_present`` is
    # True/False when known, None when no power sensor is configured (then the
    # external states off/starting/stopping/fault cannot be decided).
    power_present: bool | None
    power_watts: float | None  # numeric reading if the sensor reports watts
    power_on_since: datetime | None  # last off→on edge (for boot_grace)
    now: datetime

    # Coordinator / API freshness.
    api_fresh: bool  # coordinator.last_update_success and data is not None
    shutdown_active: bool  # a power_off sequence is running (rule 2)
    boot_grace: int  # seconds
    firmware_updating: bool  # not derivable from asic-rs -> always False

    # Live MinerData-derived values (None when offline).
    is_mining: bool | None
    hashrate_th: float | None
    expected_hashrate_th: float | None
    max_temp: float | None
    danger_limit: float | None
    overheat_margin: float

    # Modifiers.
    user_throttled: bool  # a deliberate throttle is active (VNish <100 %)
    throttle_percent: int | None
    warmup_fraction: float
    mining_power_threshold_w: float | None
    failed_board_count: int
    board_failure: bool
    curtailment_source: str | None


def _under_expected(i: StatusInputs) -> bool:
    """Hashrate meaningfully below the expected hashrate (needs both known)."""
    if i.expected_hashrate_th is None or i.hashrate_th is None:
        return False
    return i.hashrate_th < 0.95 * i.expected_hashrate_th


def _under_warmup(i: StatusInputs) -> bool:
    """Hashing but below ``warmup_fraction`` × expected (needs both known).

    Unknown expected ⇒ cannot distinguish warmup from steady mining ⇒ treat as
    mining (return False). A deliberate user throttle also suppresses warming_up:
    the low hashrate is then explained by the throttle, not by ramp-up.
    """
    if i.user_throttled:
        return False
    if i.expected_hashrate_th is None or i.hashrate_th is None:
        return False
    return i.hashrate_th < i.warmup_fraction * i.expected_hashrate_th


def _overheating(i: StatusInputs) -> bool:
    """Temp within ``overheat_margin`` of danger AND hashrate under expected,
    with no deliberate user throttle explaining the shortfall."""
    if i.danger_limit is None or i.max_temp is None:
        return False
    if i.user_throttled:
        return False
    if i.max_temp < i.danger_limit - i.overheat_margin:
        return False
    return _under_expected(i)


def _state(i: StatusInputs) -> str:
    present = i.power_present
    power_on = present is True

    # 1) External power off → off.
    if present is False:
        return STATUS_OFF

    # 2) Power on and a power_off sequence is running → stopping.
    if power_on and i.shutdown_active:
        return STATUS_STOPPING

    # 3/4) Power on but API not reachable → starting (within grace) or fault.
    if power_on and not i.api_fresh:
        if i.power_on_since is not None:
            elapsed = (i.now - i.power_on_since).total_seconds()
            if elapsed < i.boot_grace:
                return STATUS_STARTING
            return STATUS_FAULT
        # Powered on, unreachable, and we never saw the on-edge (e.g. HA
        # restarted while already on): cannot time the boot → fault.
        return STATUS_FAULT

    # No external power info AND no fresh API data → nothing to go on.
    if not i.api_fresh:
        return STATUS_UNKNOWN

    # ── From here the API is fresh (live MinerData available). ──────────────
    # 5) Firmware update in progress — not derivable from asic-rs; the flag is
    #    wired but always False, so this never fires automatically (see report).
    if i.firmware_updating:
        return STATUS_UPDATING

    # 6) Overheating (heuristic: temp near danger + hashrate under expected).
    if _overheating(i):
        return STATUS_OVERHEATING

    # 7) Paused / stopped.
    if i.is_mining is False:
        return STATUS_PAUSED

    # 8) Warming up (hashing but below the warmup fraction of expected).
    if i.is_mining and _under_warmup(i):
        return STATUS_WARMING_UP

    # 9) Mining (hashing at/above threshold, or external watts over the level).
    if i.is_mining:
        return STATUS_MINING
    if (
        i.mining_power_threshold_w is not None
        and i.power_watts is not None
        and i.power_watts >= i.mining_power_threshold_w
    ):
        return STATUS_MINING

    # 10) Fallback.
    return STATUS_UNKNOWN


def compute_status(i: StatusInputs) -> tuple[str, dict]:
    """Return ``(state, modifier_attributes)``.

    ``state`` is one of ``const.MINER_STATUS_STATES``. The modifier attributes
    are surfaced as sensor attributes (never folded into the enum), per design:
    ``throttled``/``throttle_percent``, ``board_failure``/``failed_board_count``
    and ``curtailment_source``.
    """
    attrs = {
        "throttled": bool(
            i.throttle_percent is not None and i.throttle_percent < 100
        ),
        "throttle_percent": i.throttle_percent,
        "board_failure": i.board_failure,
        "failed_board_count": i.failed_board_count,
        "curtailment_source": i.curtailment_source,
    }
    return _state(i), attrs
