"""Constants for the ASIC Miner integration."""

DOMAIN = "miner"

DEFAULT_SCAN_INTERVAL = 30  # seconds

# ── Options: sensor verbosity ───────────────────────────────────────────────
# The asic-rs entity model emits a large number of per-board sensors plus a few
# type-specific values (e.g. fluid/water temps that only exist on hydro miners).
# These options let the user dial the entity count down to what is useful for
# their hardware. Gating happens at entity-setup time in sensor.py (and the
# other platforms), driven entirely by ``config_entry.options``.

CONF_SENSOR_DETAIL = "sensor_detail_level"
CONF_ONLY_AVAILABLE = "only_available_sensors"

# Sensor detail levels (ordered least → most verbose).
DETAIL_SUMMARY = "summary"           # miner-wide aggregates only (max/min etc.)
DETAIL_BOARD_SUMMARY = "board_summary"  # + reduced per-board set (hashrate + temps)
DETAIL_BOARD_INDIVIDUAL = "board_individual"  # + full per-board set + fans
DETAIL_DEBUG = "debug"               # everything, ignore the availability gate

SENSOR_DETAIL_LEVELS = (
    DETAIL_SUMMARY,
    DETAIL_BOARD_SUMMARY,
    DETAIL_BOARD_INDIVIDUAL,
    DETAIL_DEBUG,
)

DEFAULT_SENSOR_DETAIL = DETAIL_SUMMARY
DEFAULT_ONLY_AVAILABLE = True
