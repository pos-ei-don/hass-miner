"""Power-level providers for the unified ``PowerLevelSelect`` (#621).

A ``LevelProvider`` supplies the discrete power levels for one miner plus the
apply / current operations. Two implementations keep the VNish (firmware
presets) and the generic ``set_power_limit`` (stepped watts) paths on ONE select
entity so they cannot drift apart:

* ``VnishPresetProvider``  — firmware autotune presets via the VNish REST shim.
* ``SteppedPowerProvider`` — watt steps generated from (min, max, step) for any
  ``supports_set_power_limit`` miner (BOS / WhatsMiner).

Alpha A scaffold: labels carry a ``· —`` placeholder for the (not-yet-learned)
efficiency; the learned efficiency overlay arrives in a later step.
"""

from __future__ import annotations

import math
import re

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from pyasic_rs.data import HashRateUnit

from . import vnish

# Placeholder shown where a learned efficiency value is not (yet) available.
PLACEHOLDER = "—"


def _leading_int(label: str) -> int | None:
    """Pull the leading integer (watt / preset number) out of a label."""
    m = re.match(r"\s*(\d+)", label or "")
    return int(m.group(1)) if m else None


class LevelProvider:
    """Base interface. ``options``/``current_option`` are sync (cached state)."""

    def options(self) -> list[str]:
        return []

    def current_option(self) -> str | None:
        return None

    async def apply(self, option: str) -> None:
        raise NotImplementedError


class VnishPresetProvider(LevelProvider):
    """[BETA] Firmware autotune presets via the VNish REST API (see vnish.py)."""

    def __init__(self, coordinator) -> None:
        self.c = coordinator

    def _label_for(self, name: str | None) -> str | None:
        if name is None:
            return None
        eff = getattr(self.c, "efficiency", None)
        if eff is not None and eff.get(name) is not None:
            sk = getattr(self.c, "sampling_key", None)
            return f"{name} W · {eff.label_suffix(name, sampling=(sk == name))}"
        return self.c.vnish_preset_labels.get(name, name)

    def _name_for(self, label: str) -> str:
        """Map a displayed label back to the canonical preset name VNish wants."""
        for name, lbl in self.c.vnish_preset_labels.items():
            if lbl == label:
                return name
        m = re.match(r"\s*(\d+)", label)
        if m:
            return m.group(1)
        return label.strip().lower()

    def options(self) -> list[str]:
        full = self.c.vnish_presets or list(vnish.FALLBACK_PRESETS)
        names = self._safety_cap(full)
        return [self._label_for(n) for n in names]

    def _safety_cap(self, names: list[str]) -> list[str]:
        """Drop presets above the GUI-set power limit (safety, #621).

        With the limit known+enabled: keep presets <= limit plus the first one
        ABOVE it (the boundary step, offered last). Without a readable limit:
        fall back to the curated safe set (FALLBACK_PRESETS, <= 5560 W = the
        non-modded-PSU ceiling) so unsafe untuned presets (>7000 W) never show.
        """
        limit = getattr(self.c, "vnish_power_limit", None)
        enabled = getattr(self.c, "vnish_power_limit_enabled", False)
        if enabled and limit:
            out: list[str] = []
            above_added = False
            for n in names:
                if not str(n).isdigit():
                    out.append(n)
                    continue
                w = int(n)
                if w <= limit:
                    out.append(n)
                elif not above_added:
                    out.append(n)  # first step above the limit = boundary, last
                    above_added = True
            return out
        # No readable limit → conservative curated ceiling.
        safe = set(vnish.FALLBACK_PRESETS)
        capped = [n for n in names if n in safe]
        return capped or list(vnish.FALLBACK_PRESETS)

    def current_option(self) -> str | None:
        return self._label_for(self.c.vnish_preset)

    async def apply(self, option: str) -> None:
        name = self._name_for(option)
        session = async_get_clientsession(self.c.hass)
        ok, msg = await vnish.apply_preset(
            session, self.c.ip, self.c.password, name
        )
        if not ok:
            raise RuntimeError(f"VNish preset '{name}' failed: {msg}")
        self.c.vnish_preset = name


class SteppedPowerProvider(LevelProvider):
    """Generic stepped watt levels for ``set_power_limit`` miners."""

    DEFAULT_STEP = 200
    FALLBACK_MAX = 3000.0
    # Rated-class efficiency (W/TH) used to estimate the nominal-rated max when
    # the device exposes no rated power (BOS doesn't). Stable constant × the
    # model's nominal hashrate → a sensible default ≈ rated; the owner can
    # configure ANY higher value (their responsibility, no clamp).
    NOMINAL_EFF_W_PER_TH = 27.0

    def __init__(
        self, coordinator, *, min_w=None, max_w=None, step=None
    ) -> None:
        self.c = coordinator
        self._cfg_min = float(min_w) if min_w else None
        self._cfg_max = float(max_w) if max_w else None
        self._cfg_step = int(step) if step else None

    def _current_watts(self) -> float | None:
        try:
            data = self.c.data
            if data is None:
                return None
            tt = getattr(data, "tuning_target", None)
            watts = getattr(tt, "watts", None) if tt else None
            if watts is None:
                watts = getattr(data, "wattage", None)
            return float(watts) if watts is not None else None
        except Exception:  # noqa: BLE001 — never let the UI crash on data shape
            return None

    def _default_max(self) -> float | None:
        """STABLE default max when none is configured (BOS exposes no rated max).

        = model nominal hashrate × a rated-class efficiency constant → a sensible
        ≈ rated default (so the list reaches the nominal point, not just what's
        been run so far). STABLE: expected_hashrate is a constant model value, so
        no jitter (unlike the live efficiency). The owner can configure ANY higher
        value — that's their business; no clamp is applied to a configured max."""
        try:
            th = _as_th(getattr(self.c.data, "expected_hashrate", None))
            if th and th > 0:
                return round(th * self.NOMINAL_EFF_W_PER_TH / 50.0) * 50.0
        except Exception:  # noqa: BLE001
            pass
        cur = self._current_watts()
        return (cur * 1.5) if cur else self.FALLBACK_MAX

    def _range(self) -> tuple[float, float, int]:
        """Return (min_w, max_w, step). Priority: config > device (BOS) > safe.

        - step: config → BOS powerStep → default 200
        - min:  config → BOS minPowerTarget → BOS current target → current watts
        - max:  config → realistic nominal (hashrate×eff). BOS exposes NO max, so
          we never guess high — the max stays user-configurable for safety.
        """
        bos = getattr(self.c, "bos_power_config", None) or {}
        cur = self._current_watts()

        step = int(self._cfg_step or bos.get("step") or self.DEFAULT_STEP)
        min_w = self._cfg_min or bos.get("min") or bos.get("current") or cur
        max_w = self._cfg_max or self._default_max()

        if max_w is None:
            max_w = (cur * 1.2) if cur else self.FALLBACK_MAX
        if min_w is None:
            min_w = max(step, max_w * 0.3)
        min_w, max_w = float(min_w), float(max_w)
        if min_w >= max_w:
            max_w = min_w + step
        return min_w, max_w, step

    def options(self) -> list[str]:
        try:
            mn, mx, step = self._range()
            levels: set[int] = {int(round(mn))}
            start = int(math.ceil(mn / 1000.0)) * 1000
            w = max(start, step)
            while w <= mx + 1:
                levels.add(int(w))
                w += step
            eff = getattr(self.c, "efficiency", None)
            sk = getattr(self.c, "sampling_key", None)
            out = []
            for lvl in sorted(levels):
                if eff is not None:
                    suffix = eff.label_suffix(str(lvl), sampling=(sk == str(lvl)))
                else:
                    suffix = PLACEHOLDER
                out.append(f"{lvl} W · {suffix}")
            return out
        except Exception:  # noqa: BLE001
            return []

    def current_option(self) -> str | None:
        cur = self._current_watts()
        opts = self.options()
        if cur is None or not opts:
            return None
        return min(opts, key=lambda o: abs((_leading_int(o) or 0) - cur))

    async def apply(self, option: str) -> None:
        watts = _leading_int(option)
        if watts is None:
            raise RuntimeError(f"Invalid power level '{option}'")
        await self.c.miner.set_power_limit(watts)


def _as_th(eh) -> float | None:
    """Extract a TH/s float from an expected-hashrate value (proven method)."""
    if eh is None:
        return None
    try:
        return eh.into_unit(HashRateUnit.TH).value
    except Exception:  # noqa: BLE001
        pass
    try:
        v = float(eh)
        return v if v > 0 else None
    except Exception:  # noqa: BLE001
        return None
