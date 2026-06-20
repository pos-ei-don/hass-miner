"""Minimal BOS+ (Braiins OS) read-only client for power-range discovery (#621).

asic-rs exposes ``set_power_limit`` but NOT the configured power range. BOS' own
GraphQL API gives us the current power target + step (and a min, if dynamic power
scaling is configured), so the stepped power-level selector can use REAL device
values instead of guessing. Read-only, same unauth queries the BOS web UI issues;
fully defensive (any failure → empty result, never raises).

Note: BOS exposes no *maximum* power target — the max must stay user-configurable
with a safe, realistic default (never an inflated guess that could be harmful).
"""

from __future__ import annotations

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=8)
_CONFIG_QUERY = (
    "{ bosminer { config { ... on BosminerConfig { "
    "autotuning { enabled mode powerTarget } "
    "performanceScaling { powerStep minPowerTarget } } } } }"
)
_INFO_QUERY = "{ bosminer { info { modelName } } }"


def _url(ip: str) -> str:
    return f"http://{ip}/graphql"


async def detect_bos(session: aiohttp.ClientSession, ip: str) -> bool:
    """True if the miner at ip answers the BOS GraphQL API."""
    try:
        async with session.post(
            _url(ip), json={"query": _INFO_QUERY}, timeout=_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return False
            data = await resp.json()
        return bool(((data.get("data") or {}).get("bosminer") or {}).get("info"))
    except Exception:  # noqa: BLE001
        return False


async def fetch_power_config(session: aiohttp.ClientSession, ip: str) -> dict:
    """Return ``{current, step, min}`` watts (any value may be None).

    * ``current`` = autotuning.powerTarget (the configured operating point)
    * ``step``    = performanceScaling.powerStep
    * ``min``     = performanceScaling.minPowerTarget (None unless DPS configured)
    """
    out: dict = {"current": None, "step": None, "min": None}
    try:
        async with session.post(
            _url(ip), json={"query": _CONFIG_QUERY}, timeout=_TIMEOUT
        ) as resp:
            data = await resp.json()
        cfg = (((data.get("data") or {}).get("bosminer") or {}).get("config")) or {}
        at = cfg.get("autotuning") or {}
        ps = cfg.get("performanceScaling") or {}
        out["current"] = at.get("powerTarget")
        out["step"] = ps.get("powerStep")
        out["min"] = ps.get("minPowerTarget")
    except Exception:  # noqa: BLE001
        pass
    return out
