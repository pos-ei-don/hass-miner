"""Offline tests for the lifecycle-status state machine (custom_components/miner/status.py).

Dependency-free and read-only: no Home Assistant, no pyasic-rs, no network. Runs
standalone (``python3 tests/test_status.py``) or under pytest. ``status.py`` only
depends on ``const.py``; both are loaded by file path into a synthetic package so
importing ``custom_components/miner/__init__.py`` (which pulls in HA) is avoided.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone

_MINER = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "miner"
)


def _load():
    pkg = types.ModuleType("miner_pure")
    pkg.__path__ = [str(_MINER)]
    sys.modules["miner_pure"] = pkg

    def _mod(name):
        spec = importlib.util.spec_from_file_location(
            f"miner_pure.{name}", _MINER / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"miner_pure.{name}"] = mod
        spec.loader.exec_module(mod)
        return mod

    return _mod("const"), _mod("status")


const, status = _load()
StatusInputs = status.StatusInputs
compute_status = status.compute_status

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _mk(**kw) -> "StatusInputs":
    base = dict(
        power_present=True,
        power_watts=3000.0,
        power_on_since=NOW - timedelta(seconds=300),
        now=NOW,
        api_fresh=True,
        shutdown_active=False,
        boot_grace=120,
        firmware_updating=False,
        is_mining=True,
        hashrate_th=100.0,
        expected_hashrate_th=100.0,
        max_temp=60.0,
        danger_limit=90.0,
        overheat_margin=3.0,
        user_throttled=False,
        throttle_percent=100,
        warmup_fraction=0.8,
        mining_power_threshold_w=None,
        failed_board_count=0,
        board_failure=False,
        curtailment_source=None,
    )
    base.update(kw)
    return StatusInputs(**base)


def _state(**kw) -> str:
    return compute_status(_mk(**kw))[0]


# ── Precedence (top-down, first match wins) ─────────────────────────────────


def test_off_when_power_absent():
    assert _state(power_present=False) == "off"


def test_stopping_when_sequence_active():
    assert _state(shutdown_active=True) == "stopping"


def test_starting_within_boot_grace():
    assert (
        _state(api_fresh=False, power_on_since=NOW - timedelta(seconds=30))
        == "starting"
    )


def test_fault_after_boot_grace():
    assert (
        _state(api_fresh=False, power_on_since=NOW - timedelta(seconds=300))
        == "fault"
    )


def test_fault_when_on_but_no_edge_known():
    assert _state(api_fresh=False, power_on_since=None) == "fault"


def test_unknown_without_power_sensor_and_no_api():
    assert (
        _state(power_present=None, api_fresh=False, power_on_since=None)
        == "unknown"
    )


def test_overheating():
    assert (
        _state(max_temp=89.0, hashrate_th=50.0, expected_hashrate_th=100.0)
        == "overheating"
    )


def test_user_throttle_suppresses_overheating_and_warmup():
    # A deliberate throttle explains the low hashrate → not overheating, and not
    # warming_up either → plain mining.
    assert (
        _state(
            max_temp=89.0,
            hashrate_th=50.0,
            expected_hashrate_th=100.0,
            user_throttled=True,
            throttle_percent=70,
        )
        == "mining"
    )


def test_paused_when_not_mining():
    assert _state(is_mining=False) == "paused"


def test_warming_up_below_fraction():
    assert _state(hashrate_th=50.0, expected_hashrate_th=100.0) == "warming_up"


def test_mining_at_or_above_fraction():
    assert _state(hashrate_th=95.0, expected_hashrate_th=100.0) == "mining"


def test_watt_threshold_does_not_override_paused():
    assert _state(is_mining=False, mining_power_threshold_w=1000.0) == "paused"


def test_mining_via_watt_threshold_when_hashrate_unknown():
    assert (
        _state(
            is_mining=None,
            hashrate_th=None,
            expected_hashrate_th=None,
            mining_power_threshold_w=1000.0,
            power_watts=3000.0,
        )
        == "mining"
    )


def test_unknown_fallback():
    assert (
        _state(is_mining=None, hashrate_th=None, expected_hashrate_th=None)
        == "unknown"
    )


def test_internal_states_work_without_power_sensor():
    assert _state(power_present=None) == "mining"


# ── Modifier attributes ─────────────────────────────────────────────────────


def test_modifier_attributes():
    _, attrs = compute_status(
        _mk(
            throttle_percent=70,
            board_failure=True,
            failed_board_count=2,
            curtailment_source="integration_power_off",
        )
    )
    assert attrs["throttled"] is True
    assert attrs["throttle_percent"] == 70
    assert attrs["board_failure"] is True
    assert attrs["failed_board_count"] == 2
    assert attrs["curtailment_source"] == "integration_power_off"


def test_every_state_is_a_valid_enum_option():
    # Whatever the machine emits must be in the sensor's enum options.
    for name, fn in list(globals().items()):
        if name.startswith("test_") and name != "test_every_state_is_a_valid_enum_option":
            pass
    # Spot-check a spread of inputs.
    for inp in (
        _mk(power_present=False),
        _mk(shutdown_active=True),
        _mk(api_fresh=False, power_on_since=None),
        _mk(is_mining=False),
        _mk(hashrate_th=10.0),
        _mk(),
    ):
        assert compute_status(inp)[0] in const.MINER_STATUS_STATES


if __name__ == "__main__":
    passed = 0
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                passed += 1
                print(f"[OK ] {_name}")
            except AssertionError as err:
                failed += 1
                print(f"[FAIL] {_name}: {err}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
