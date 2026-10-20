"""Run miner control calls and surface a refusal to Home Assistant.

Since asic-rs 0.8.6 a control call the miner refuses (pause, resume, restart,
pool or fault-light writes, ...) raises ``RuntimeError`` carrying the miner's
reason, where older backends returned ``False`` without saying why. Both
outcomes end up here as a ``HomeAssistantError`` with that reason, so the UI
shows it and automations can opt in to ``continue_on_error``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError


async def async_miner_control(
    what: str, miner: Any, call: Callable[[Any], Awaitable[Any]]
) -> None:
    """Await ``call(miner)``; raise HomeAssistantError if it fails or is refused."""
    if miner is None:
        raise HomeAssistantError(f"{what}: miner not connected")
    try:
        ok = await call(miner)
    except HomeAssistantError:
        raise
    except Exception as err:  # noqa: BLE001 - the library raises RuntimeError with the reason
        raise HomeAssistantError(f"{what}: {err}") from err
    if ok is False:
        raise HomeAssistantError(f"{what}: refused by the miner")
