"""Self-learning power-level efficiency map (#621, part B).

Per config entry we persist a map ``{level_key: entry}`` in a Store (JSON file),
where ``level_key`` is the VNish preset name or the watt setpoint (as str) and
``entry`` holds the learned hashrate (TH/s) + efficiency (W/TH) as an EMA of the
recent stable samples, plus sample count / pin / timestamp.

Sampling is fed by the coordinator only when a level has run *stably* for a
dwell window (mining, not tuning, hashrate within tolerance) — see
``EfficiencySampler``. Manual values can be pinned (the learner won't overwrite
them) and reset.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

STORAGE_VERSION = 1

# EMA weight for the newest stable sample (recent-weighted → tracks drift).
EMA_ALPHA = 0.3
# A level must run stably this long before an EMA-refining sample is recorded.
DWELL_SECONDS = 900  # 15 min
# First *provisional* value is seeded after this short stable window (so labels
# aren't empty at the start; the device's own efficiency is taken over and then
# refined by EMA as BOS keeps fine-tuning).
SEED_SECONDS = 120  # 2 min
# Stability is judged over a SLIDING recent window (not since window-start) so an
# old ramp transient can't block seeding forever. Mining hashrate fluctuates, so
# the tolerance is generous (3 % was far too strict — nothing ever seeded).
STABLE_TOLERANCE = 0.10  # ±10 %
STABLE_WINDOW_SECONDS = 180  # 3 min sliding window for the stability check

PLACEHOLDER = "—"
LEARNING = "lernt…"


def _round1(x):
    return round(float(x), 1) if x is not None else None


class EfficiencyStore:
    """Persistent learned-efficiency map for one miner."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, f"miner_efficiency_{entry_id}")
        self._map: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._map = (await self._store.async_load()) or {}

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._map, 5)

    # ── reads ────────────────────────────────────────────────────────────
    def get(self, key) -> dict | None:
        return self._map.get(str(key))

    def as_map(self) -> dict[str, dict]:
        return dict(self._map)

    def label_suffix(self, key, *, sampling: bool = False) -> str:
        """Display suffix for an option: learned values, else placeholder."""
        e = self._map.get(str(key))
        if e and e.get("eff") is not None:
            hr = e.get("hashrate")
            hr_s = f"{hr:.1f} TH · " if hr else ""
            pin = " \U0001f7e2" if e.get("pinned") else ""
            return f"{hr_s}{e['eff']:.1f} W/TH{pin}"
        return LEARNING if sampling else PLACEHOLDER

    # ── writes ───────────────────────────────────────────────────────────
    def set_manual(self, key, *, hashrate=None, efficiency=None, pin=True) -> None:
        e = self._map.setdefault(str(key), {"samples": 0})
        if hashrate is not None:
            e["hashrate"] = _round1(hashrate)
        if efficiency is not None:
            e["eff"] = _round1(efficiency)
        e["pinned"] = bool(pin)
        e["manual"] = True
        e["ts"] = dt_util.utcnow().isoformat()
        self._save()

    def reset(self, key=None) -> None:
        if key is None:
            self._map.clear()
        else:
            self._map.pop(str(key), None)
        self._save()

    def add_sample(self, key, *, hashrate, efficiency) -> None:
        """Fold one stable sample into the EMA (skips pinned/manual entries)."""
        if not hashrate or not efficiency or hashrate <= 0 or efficiency <= 0:
            return
        e = self._map.get(str(key))
        if e and e.get("pinned"):
            return
        if not e:
            e = {"samples": 0}
            self._map[str(key)] = e
        if e.get("eff") is None or not e.get("samples"):
            e["eff"] = _round1(efficiency)
            e["hashrate"] = _round1(hashrate)
        else:
            e["eff"] = _round1(EMA_ALPHA * efficiency + (1 - EMA_ALPHA) * e["eff"])
            e["hashrate"] = _round1(
                EMA_ALPHA * hashrate + (1 - EMA_ALPHA) * e["hashrate"]
            )
        e["samples"] = int(e.get("samples", 0)) + 1
        e["manual"] = False
        e["ts"] = dt_util.utcnow().isoformat()
        self._save()


class EfficiencySampler:
    """Tracks dwell + stability to decide when to record a sample.

    Fed once per successful coordinator update via ``observe``. Fully defensive:
    any bad/missing signal just skips sampling, never raises.
    """

    def __init__(self, store: EfficiencyStore) -> None:
        self.store = store
        self._key = None
        self._since = None
        self._recent: list[tuple] = []  # (timestamp, hashrate) within the window
        self._seeded = False

    @property
    def active_key(self):
        """The level currently accumulating a (not-yet-recorded) dwell, if any."""
        return self._key

    def _stable(self, now) -> bool:
        """Stable over the SLIDING recent window (old transients age out)."""
        cutoff = now - timedelta(seconds=STABLE_WINDOW_SECONDS)
        self._recent = [(t, h) for (t, h) in self._recent if t >= cutoff]
        hrs = [h for _, h in self._recent]
        if len(hrs) < 2:
            return False
        hi, lo = max(hrs), min(hrs)
        return bool(hi) and (hi - lo) / hi <= STABLE_TOLERANCE

    def observe(self, *, key, hashrate, efficiency, mining, tuning) -> None:
        try:
            now = dt_util.utcnow()
            if key is None or not mining or tuning or not hashrate or not efficiency:
                self._key = None
                self._since = None
                self._recent = []
                self._seeded = False
                return
            if key != self._key:
                # level changed → start a fresh window
                self._key = key
                self._since = now
                self._recent = [(now, hashrate)]
                self._seeded = False
                return
            self._recent.append((now, hashrate))
            elapsed = (now - self._since).total_seconds()
            stable = self._stable(now)  # prunes to the sliding window
            existing = self.store.get(key)
            has_value = bool(existing and existing.get("eff") is not None)

            # Seed: take over the device's own efficiency after a short stable
            # window so the label isn't empty — only if this step has no value yet
            # (pinned/manual + already-learned are left untouched by add_sample).
            if not has_value and not self._seeded and stable and elapsed >= SEED_SECONDS:
                self.store.add_sample(key, hashrate=hashrate, efficiency=efficiency)
                self._seeded = True
                return

            # Refine: full stable dwell → EMA update (tracks BOS fine-tune drift).
            if elapsed >= DWELL_SECONDS and stable:
                self.store.add_sample(key, hashrate=hashrate, efficiency=efficiency)
                self._since = now
                self._seeded = False
        except Exception:  # noqa: BLE001 — never break the coordinator loop
            return
