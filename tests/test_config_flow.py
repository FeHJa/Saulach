"""config_flow.py tests against the fake homeassistant.config_entries.

Real Home Assistant's flow manager normally validates user_input against
the step's voluptuous schema before calling async_step_user; the fake
ConfigFlow base class here doesn't reproduce that, so tests pass
already-schema-conformant values (matching what the real form would
coerce user_input into) directly.
"""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow

from custom_components.saulach.config_flow import SaulachConfigFlow
from custom_components.saulach.const import (
    CONF_BRIDGE_NAME,
    CONF_ENTITIES,
    CONF_SENSOR_VALUE_PREFIX,
    CONF_SHARED_DISCOVERY_PREFIX,
    CONF_TIME_PATTERN_MINUTES,
)

VALID_INPUT = {
    CONF_BRIDGE_NAME: "Bridge Jakob",
    CONF_ENTITIES: ["sensor.garage_temperature"],
    CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant",
    CONF_SENSOR_VALUE_PREFIX: "share/jakob",
    CONF_TIME_PATTERN_MINUTES: 1,
}


def _make_flow() -> SaulachConfigFlow:
    flow = SaulachConfigFlow()
    flow.hass = HomeAssistant()
    return flow


def _run(coro):
    return asyncio.run(coro)


def test_initial_form_has_no_errors():
    flow = _make_flow()
    result = _run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {}


def test_valid_input_creates_entry_with_normalized_prefixes():
    flow = _make_flow()
    result = _run(flow.async_step_user(dict(VALID_INPUT)))

    assert result["type"] == "create_entry"
    assert result["title"] == "Bridge Jakob"
    assert result["data"] == {
        CONF_ENTITIES: ["sensor.garage_temperature"],
        CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant/",
        CONF_SENSOR_VALUE_PREFIX: "share/jakob/",
        CONF_BRIDGE_NAME: "Bridge Jakob",
    }
    assert result["options"] == {CONF_TIME_PATTERN_MINUTES: 1}


def test_valid_input_sets_unique_id_to_slug():
    flow = _make_flow()
    _run(flow.async_step_user(dict(VALID_INPUT)))
    assert flow.unique_id == "bridge_jakob"


def test_bridge_name_with_no_valid_chars_is_rejected():
    flow = _make_flow()
    bad_input = dict(VALID_INPUT)
    bad_input[CONF_BRIDGE_NAME] = "!!!"

    result = _run(flow.async_step_user(bad_input))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_bridge_name"}


def test_empty_entities_is_rejected():
    flow = _make_flow()
    bad_input = dict(VALID_INPUT)
    bad_input[CONF_ENTITIES] = []

    result = _run(flow.async_step_user(bad_input))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "no_entities"}


def test_duplicate_entity_in_selection_is_deduped():
    flow = _make_flow()
    dup_input = dict(VALID_INPUT)
    dup_input[CONF_ENTITIES] = ["sensor.garage_temperature", "sensor.garage_temperature"]

    result = _run(flow.async_step_user(dup_input))

    assert result["type"] == "create_entry"
    assert result["data"][CONF_ENTITIES] == ["sensor.garage_temperature"]


def test_duplicate_bridge_name_aborts():
    flow = _make_flow()
    flow.hass.config_entries.entries.append(
        ConfigEntry(unique_id="bridge_jakob")
    )

    try:
        _run(flow.async_step_user(dict(VALID_INPUT)))
        raised = False
    except AbortFlow as exc:
        raised = True
        assert exc.reason == "already_configured"

    assert raised
