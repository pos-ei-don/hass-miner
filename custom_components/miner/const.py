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

# ── Options: lifecycle status sensor + power services ───────────────────────
# A dedicated status feature, independent of the power-aware polling above:
#   * ``power_sensor``  — an external power reading (W sensor preferred, or a
#     switch/binary_sensor) that tells whether the miner is drawing power. This
#     is what separates ``off`` from an unreachable-but-powered miner. When
#     unset it falls back to ``power_entity`` (the polling gate) if that is set.
#   * ``power_switch``  — the controllable outlet (e.g. a Tasmota switch) the
#     ``miner.power_on`` / ``miner.power_off`` services operate.
#   * ``boot_grace_seconds``     — how long after a power-on edge an unreachable
#     miner is still ``starting`` rather than ``fault``.
#   * ``shutdown_delay_seconds`` — the pause→power-off delay in ``power_off``.
#   * ``mining_power_threshold_w`` — optional external-watt level at/above which
#     the miner counts as ``mining`` even without live hashrate.
#   * ``warmup_hashrate_fraction`` — hashrate/expected ratio below which a
#     hashing miner is ``warming_up`` rather than ``mining``.
# All additive with defaults; existing options are untouched.
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_SWITCH = "power_switch"
CONF_BOOT_GRACE = "boot_grace_seconds"
DEFAULT_BOOT_GRACE = 120  # seconds; separates ``starting`` from ``fault``
CONF_SHUTDOWN_DELAY = "shutdown_delay_seconds"
DEFAULT_SHUTDOWN_DELAY = 30  # seconds; pause→power-off delay
CONF_MINING_POWER_THRESHOLD_W = "mining_power_threshold_w"  # optional
CONF_WARMUP_HASHRATE_FRACTION = "warmup_hashrate_fraction"
DEFAULT_WARMUP_HASHRATE_FRACTION = 0.8

# Status-sensor tuning constants (not user-facing).
# A W reading strictly above this counts as "power present" (a bare standby /
# vampire draw of a switched-off outlet is typically 0 W).
STATUS_POWER_EPSILON_W = 5.0
# Overheating is flagged when the hottest temperature is within this many °C of
# the device's danger limit (TemperatureConfig.danger).
STATUS_OVERHEAT_MARGIN_C = 3.0

# Lifecycle status states (sensor.<miner>_status, device_class enum). Order is
# the top-down precedence used by status.compute_status (first match wins).
STATUS_OFF = "off"
STATUS_STARTING = "starting"
STATUS_WARMING_UP = "warming_up"
STATUS_MINING = "mining"
STATUS_PAUSED = "paused"
STATUS_STOPPING = "stopping"
STATUS_OVERHEATING = "overheating"
STATUS_FAULT = "fault"
STATUS_UPDATING = "updating"
STATUS_UNKNOWN = "unknown"

MINER_STATUS_STATES = [
    STATUS_OFF,
    STATUS_STARTING,
    STATUS_WARMING_UP,
    STATUS_MINING,
    STATUS_PAUSED,
    STATUS_STOPPING,
    STATUS_OVERHEATING,
    STATUS_FAULT,
    STATUS_UPDATING,
    STATUS_UNKNOWN,
]

# Service names (domain services, registered in __init__.py).
SERVICE_POWER_ON = "power_on"
SERVICE_POWER_OFF = "power_off"

# Entity-naming (#625): when simple_naming is on, entity_ids are generated
# deterministically as "<platform>.miner_<slug>_<key>" from the config-entry
# title (slugified) instead of being derived from the (long) device name. This
# only affects NEW registrations; existing entities are migrated only via the
# explicit "Apply naming scheme" button.
CONF_SIMPLE_NAMING = "simple_naming"
DEFAULT_SIMPLE_NAMING = True
