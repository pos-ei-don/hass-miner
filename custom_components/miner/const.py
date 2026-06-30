"""Constants for the ASIC Miner integration."""

DOMAIN = "miner"

# ── Stable device identity (#672) ───────────────────────────────────────────
# The miner's MAC, persisted in entry.data once known. It is the canonical
# device identifier (unique_id prefix + DeviceInfo identifiers). It must NEVER
# be derived from the IP/host: IPs are DHCP-mutable, and an IP-based identity
# orphans entities + duplicates devices when the address changes.
CONF_MAC = "mac"

# ── Options: polling ────────────────────────────────────────────────────────
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 30  # seconds
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 600

# ── Options: power-aware polling (optional) ─────────────────────────────────
# When a power switch/sensor entity is configured, the coordinator pauses
# network polling while the miner is powered off, runs a fast "boot loop" right
# after power-on until the miner answers, and latches a boot-timeout alarm if it
# never comes up in time. Default unset ⇒ exact current behavior (always-on).
CONF_POWER_ENTITY = "power_entity"
CONF_BOOT_TIMEOUT = "boot_timeout"
DEFAULT_BOOT_TIMEOUT = 120  # seconds to wait for the miner after power-on
MIN_BOOT_TIMEOUT = 30
MAX_BOOT_TIMEOUT = 600
BOOT_POLL_INTERVAL = 10  # seconds, the fast loop during boot

# ── Options: which sensors to create ────────────────────────────────────────
# The asic-rs entity model emits a large number of per-board sensors plus a few
# type-specific values. The user ticks the categories they want; gating happens
# at entity-setup time (sensor.py / binary_sensor.py), driven entirely by
# ``config_entry.options``. Unticking a category removes its entities on reload
# (deterministic / boot-safe — never based on a transient missing value).

CONF_SENSOR_CATEGORIES = "sensor_categories"
CONF_ONLY_AVAILABLE = "only_available"

# Category keys.
CAT_MINER_SUMMARY = "miner_summary"  # miner-wide aggregates/statistics
CAT_SAFETY = "safety"  # safety alarm: problem flag + reason text
CAT_BOARD_TEMPS = "board_temps"  # per-board board/chip/intake/outlet temperatures
CAT_BOARD_PERF = "board_perf"  # per-board hashrate/voltage/frequency/working-chips
CAT_FANS = "fans"  # fan RPM

SENSOR_CATEGORIES = [
    CAT_MINER_SUMMARY,
    CAT_SAFETY,
    CAT_BOARD_TEMPS,
    CAT_BOARD_PERF,
    CAT_FANS,
]

# Sensible defaults: the everyday summary plus the safety alarm.
DEFAULT_SENSOR_CATEGORIES = [CAT_MINER_SUMMARY, CAT_SAFETY]
DEFAULT_ONLY_AVAILABLE = True

# ── Options: power-level selector (#621) ────────────────────────────────────
# A discrete "Leistungsstufen-Wähler" (select). VNish miners get firmware
# presets; set_power_limit miners (BOS/WhatsMiner) get watt steps generated from
# (min, max, step). min/max unset ⇒ heuristic range from runtime data.
CONF_ENABLE_POWER_LEVELS = "enable_power_levels"
CONF_POWER_MIN = "power_min"
CONF_POWER_MAX = "power_max"
CONF_POWER_STEP = "power_step"
DEFAULT_ENABLE_POWER_LEVELS = True
DEFAULT_POWER_STEP = 200
MIN_POWER_STEP = 50
MAX_POWER_STEP = 1000

# ── Options: timezone management ─────────────────────────────────────────────
# Let HA keep the miner's timezone correct, including across DST. BraiinsOS takes
# a named IANA zone and handles DST itself; VNish takes a fixed "GMT±N" offset
# and has NO DST, so the correct offset must be (re)applied at each DST change.
# ``timezone_check`` enables the periodic + startup auto-sync; ``timezone_mode``
# picks whether a mismatch is corrected automatically ("auto") or only surfaced
# as a repair issue for the user to confirm ("repair").
CONF_TZ_CHECK = "timezone_check"
DEFAULT_TZ_CHECK = False
CONF_TZ_MODE = "timezone_mode"
TZ_MODE_AUTO = "auto"
TZ_MODE_REPAIR = "repair"
DEFAULT_TZ_MODE = TZ_MODE_AUTO

# Entity-naming (#625): when simple_naming is on, entity_ids are generated
# deterministically as "<platform>.miner_<slug>_<key>" from the config-entry
# title (slugified) instead of being derived from the (long) device name. This
# only affects NEW registrations; existing entities are migrated only via the
# explicit "Apply naming scheme" button.
CONF_SIMPLE_NAMING = "simple_naming"
DEFAULT_SIMPLE_NAMING = True
