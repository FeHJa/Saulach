"""Constants for Grapevine — peer-to-peer entity federation for Home
Assistant.

Values here are the wire-protocol contract reverse-engineered in
PROTOCOL.md — keep this module in sync with that document, not the other
way around.
"""

from __future__ import annotations

import re

DOMAIN = "grapevine"

# Config entry keys (PROTOCOL.md §1). local_discovery_prefix is
# deliberately absent -- it existed only to support forwarding into HA's
# local MQTT Discovery root, which Phase 1b (§5a) replaced with native
# entity creation. See MIGRATION_PLAN.md Decision 6.
CONF_ENTITIES = "entities"
CONF_SHARED_DISCOVERY_PREFIX = "shared_discovery_prefix"
CONF_SENSOR_VALUE_PREFIX = "sensor_value_prefix"
CONF_BRIDGE_NAME = "bridge_name"
CONF_TIME_PATTERN_MINUTES = "time_pattern_minutes"

DEFAULT_SHARED_DISCOVERY_PREFIX = "share/homeassistant/"
DEFAULT_SENSOR_VALUE_PREFIX = "share/jakob/"
DEFAULT_BRIDGE_NAME = "Bridge Jakob"
DEFAULT_TIME_PATTERN_MINUTES = 1

# Discovery payload (PROTOCOL.md §3)
SW_VERSION = "1.0.3"
PROTOCOL_VERSION = 1

# Jitter (PROTOCOL.md §6)
JITTER_MAX_SECONDS = 9

SERVICE_REPUBLISH = "republish"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

# Manual depublish of a dead peer bridge (issue #12/CLAUDE.md §5c) -- never
# triggered automatically, always a human naming a specific bridge device.
SERVICE_DEPUBLISH_BRIDGE = "depublish_bridge"
ATTR_BRIDGE_DEVICE = "bridge_device"


def _pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"(^|_){word}(_|$)", re.IGNORECASE)


# device_class / unit_of_measurement fallback table (PROTOCOL.md §3).
# Pattern shape "(^|_)<word>(_|$)" and match order are load-bearing for
# interop with the other bridge instances — do not rewrite or reorder.
DEVICE_CLASS_UNIT_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (_pattern("temperature"), "temperature", "°C"),
    (_pattern("humidity"), "humidity", "%"),
    (_pattern("pressure"), "pressure", "hPa"),
    (_pattern("power"), "power", "W"),
    (_pattern("energy"), "energy", "kWh"),
    (_pattern("current"), "current", "A"),
    (_pattern("voltage"), "voltage", "V"),
    (_pattern("light"), "illuminance", "lx"),
]
