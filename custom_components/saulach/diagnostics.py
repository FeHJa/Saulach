"""Config entry diagnostics ("Download Diagnostics" button) -- surfaces the
same metadata this bridge publishes to the wire (PROTOCOL.md §9, issue
#12), plus the bridged entity list, for support requests.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ENTITIES


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime_data = entry.runtime_data
    return {
        "entities": entry.data.get(CONF_ENTITIES, []),
        # None until the first metadata publish completes (e.g. briefly
        # after startup, before its jittered delay elapses).
        "last_metadata": None if runtime_data is None else runtime_data.scheduler.last_metadata,
    }
