"""End-to-end test of Phase 1b (§5a) through the real async_setup_entry
path: entry setup -> sensor platform registers its add_entities callback
-> a federation message arrives on the shared prefix -> a native entity
appears in hass.states -> removing the entry removes it again.
"""

import asyncio
import json

import pytest

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components import saulach
from custom_components.saulach import scheduler as scheduler_module
from custom_components.saulach.const import (
    CONF_BRIDGE_NAME,
    CONF_ENTITIES,
    CONF_SENSOR_VALUE_PREFIX,
    CONF_SHARED_DISCOVERY_PREFIX,
    CONF_TIME_PATTERN_MINUTES,
)


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda a, b: 0)


def _make_entry() -> ConfigEntry:
    return ConfigEntry(
        entry_id="entry1",
        data={
            CONF_ENTITIES: [],
            CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant/",
            CONF_SENSOR_VALUE_PREFIX: "share/jakob/",
            CONF_BRIDGE_NAME: "Bridge Jakob",
        },
        options={CONF_TIME_PATTERN_MINUTES: 1},
    )


def _run(coro):
    return asyncio.run(coro)


def test_federation_message_materializes_native_entity():
    hass = HomeAssistant()
    entry = _make_entry()
    remote_payload = {
        "name": "Garage Humidity",
        "state_topic": "share/other_bridge/sensor/garage_humidity",
        "unique_id": "other_bridge::sensor.garage_humidity",
        "device_class": "humidity",
        "unit_of_measurement": "%",
        "bridge_id": "other_bridge",
        "protocol_version": 1,
        "device": {
            "identifiers": ["other_bridge"],
            "name": "Bridge Other",
            "sw_version": "1.0.3",
        },
    }

    async def scenario():
        await saulach.async_setup_entry(hass, entry)

        await mqtt.async_fire_mqtt_message(
            hass,
            "share/homeassistant/sensor/garage_humidity/config",
            json.dumps(remote_payload),
        )
        await mqtt.async_fire_mqtt_message(
            hass, "share/other_bridge/sensor/garage_humidity", "63"
        )

    _run(scenario())

    manager = entry.runtime_data.remote_entity_manager
    entity = manager._entities["other_bridge::sensor.garage_humidity"]
    assert entity.entity_id is not None
    assert entity.entity_id.startswith("sensor.")

    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.state == "63"
    assert state.attributes["device_class"] == "humidity"
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["friendly_name"] == "Garage Humidity"


def test_removing_entry_removes_the_native_entity():
    hass = HomeAssistant()
    entry = _make_entry()
    remote_payload = {
        "name": "Garage Humidity",
        "state_topic": "share/other_bridge/sensor/garage_humidity",
        "unique_id": "other_bridge::sensor.garage_humidity",
        "bridge_id": "other_bridge",
        "protocol_version": 1,
        "device": {"identifiers": ["other_bridge"], "name": "Bridge Other", "sw_version": "1.0.3"},
    }

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await mqtt.async_fire_mqtt_message(
            hass,
            "share/homeassistant/sensor/garage_humidity/config",
            json.dumps(remote_payload),
        )

        manager = entry.runtime_data.remote_entity_manager
        entity_id = manager._entities["other_bridge::sensor.garage_humidity"].entity_id
        assert hass.states.get(entity_id) is not None

        await saulach.async_unload_entry(hass, entry)
        await entry.async_unload()

        assert hass.states.get(entity_id) is None
        # The manager's own bookkeeping is cleared too (§5a unload cleanup).
        assert manager._entities == {}

    _run(scenario())


def test_does_not_materialize_own_echoed_message():
    """Loop guard still applies -- our own discovery message, if somehow
    delivered back to us (broker echo, or federation misconfiguration),
    must not create a self-referential entity."""
    hass = HomeAssistant()
    entry = _make_entry()

    async def scenario():
        await saulach.async_setup_entry(hass, entry)

        own_payload = {
            "name": "Should Not Appear",
            "state_topic": "share/jakob/sensor/should_not_appear",
            "unique_id": "bridge_jakob::sensor.should_not_appear",
            "bridge_id": "bridge_jakob",
            "protocol_version": 1,
            "device": {"identifiers": ["bridge_jakob"], "name": "Bridge Jakob", "sw_version": "1.0.3"},
        }
        await mqtt.async_fire_mqtt_message(
            hass,
            "share/homeassistant/sensor/should_not_appear/config",
            json.dumps(own_payload),
        )

    _run(scenario())

    manager = entry.runtime_data.remote_entity_manager
    assert manager._entities == {}
