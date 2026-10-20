"""Tests for control.async_miner_control (refusals surface as HomeAssistantError).

Loads ``control.py`` standalone with a stub ``homeassistant.exceptions`` so the
test needs neither Home Assistant nor the miner library.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

import pytest


class _HAError(Exception):
    pass


def _load():
    ha = types.ModuleType("homeassistant")
    exc = types.ModuleType("homeassistant.exceptions")
    exc.HomeAssistantError = _HAError
    sys.modules.setdefault("homeassistant", ha)
    sys.modules["homeassistant.exceptions"] = exc
    path = (
        pathlib.Path(__file__).parents[1] / "custom_components" / "miner" / "control.py"
    )
    spec = importlib.util.spec_from_file_location("miner_control", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


control = _load()


class _Miner:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def pause(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def _run(what, miner):
    return asyncio.run(control.async_miner_control(what, miner, lambda m: m.pause()))


def test_accepted_call_passes():
    miner = _Miner(True)
    _run("Pause mining", miner)
    assert miner.calls == 1


def test_none_result_passes():
    _run("Pause mining", _Miner(None))


def test_refusal_reason_from_library_is_kept():
    reason = 'HTTP error: 400 {"err":"miner is not running"}'
    with pytest.raises(_HAError, match="Pause mining: .*miner is not running"):
        _run("Pause mining", _Miner(error=RuntimeError(reason)))


def test_false_result_becomes_error():
    with pytest.raises(_HAError, match="refused by the miner"):
        _run("Pause mining", _Miner(False))


def test_missing_miner():
    with pytest.raises(_HAError, match="miner not connected"):
        _run("Pause mining", None)
