"""Constants for the ASIC Miner integration."""

DOMAIN = "miner"

# ── Options: polling ────────────────────────────────────────────────────────
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 30  # seconds
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 3600

# ── Options: which sensors to create ────────────────────────────────────────
# The asic-rs entity model emits a large number of per-board sensors plus a few
# type-specific values. The user ticks the categories they want; gating happens
# at entity-setup time (sensor.py / binary_sensor.py), driven entirely by
# ``config_entry.options``. Unticking a category removes its entities on reload
# (deterministic / boot-safe — never based on a transient missing value).

CONF_SENSOR_CATEGORIES = "sensor_categories"
CONF_ONLY_AVAILABLE = "only_available_sensors"

# Category keys.
CAT_MINER_SUMMARY = "miner_summary"  # miner-wide aggregates/statistics
CAT_SAFETY = "safety"  # safety alarm: problem flag, reason text, device limits
CAT_BOARD_TEMPS = "board_temps"  # per-board board/chip/intake/outlet temperatures
CAT_BOARD_PERF = "board_perf"  # per-board hashrate/voltage/frequency/working-chips
CAT_FANS = "fans"  # fan RPM

SENSOR_CATEGORIES = (
    CAT_MINER_SUMMARY,
    CAT_SAFETY,
    CAT_BOARD_TEMPS,
    CAT_BOARD_PERF,
    CAT_FANS,
)

# Sensible defaults: the everyday summary plus the safety alarm.
DEFAULT_SENSOR_CATEGORIES = [CAT_MINER_SUMMARY, CAT_SAFETY]
DEFAULT_ONLY_AVAILABLE = True
