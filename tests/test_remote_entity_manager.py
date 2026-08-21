"""RemoteEntityManager unit tests — create-on-first-sight, update-in-place
on redelivery, per-entity state-topic subscription, removal (issue #7),
unload cleanup. Wired manually here (not through __init__.py's full setup)
to isolate the manager's own behavior; see
test_federated_entities_integration.py for the end-to-end path through
async_setup_entry.
"""

import asyncio

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.grapevine.const import DOMAIN
from custom_components.grapevine.remote_entity_manager import RemoteEntityManager

DISCOVERY_TOPIC = "share/homeassistant/sensor/garage_humidity/config"

EXAMPLE_PAYLOAD = {
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


def _make_manager(hass: HomeAssistant) -> tuple[RemoteEntityManager, list]:
    manager = RemoteEntityManager(hass, ConfigEntry())
    added: list = []

    def _add_entities(entities) -> None:
        # Mimics what the real (and fake, in ConfigEntriesRegistry) entity
        # platform does when async_add_entities runs -- assigns hass and
        # an entity_id before the entity is otherwise usable.
        for entity in entities:
            entity.hass = hass
            entity.entity_id = f"sensor.{entity.unique_id.replace('.', '_').replace(':', '_')}"
            added.append(entity)
            er.async_get(hass)._register(entity.entity_id)

    manager.set_add_entities_callback(_add_entities)
    return manager, added


def _run(coro):
    return asyncio.run(coro)


def test_creates_entity_on_first_discovery():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD)))

    assert len(added) == 1
    entity = added[0]
    assert entity.unique_id == "other_bridge::sensor.garage_humidity"
    assert entity._attr_name == "Garage Humidity"
    assert entity._attr_device_class == "humidity"
    assert entity._attr_native_unit_of_measurement == "%"
    assert entity._attr_device_info == {
        "identifiers": {(DOMAIN, "other_bridge")},
        "name": "Bridge Other",
        "sw_version": "1.0.3",
    }


def test_subscribes_to_state_topic_on_first_discovery():
    hass = HomeAssistant()
    manager, _added = _make_manager(hass)

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD)))

    assert "share/other_bridge/sensor/garage_humidity" in mqtt._state(hass).subscriptions


def test_state_message_updates_entity_native_value_and_ha_state():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await mqtt.async_fire_mqtt_message(
            hass, "share/other_bridge/sensor/garage_humidity", "55"
        )

    _run(scenario())

    entity = added[0]
    assert entity.native_value == "55"
    assert hass.states.get(entity.entity_id).state == "55"


def test_redelivery_of_same_unique_id_updates_in_place_not_duplicated():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        updated = dict(EXAMPLE_PAYLOAD)
        updated["name"] = "Garage Humidity (renamed)"
        updated["device_class"] = None
        await manager.async_handle_discovery(DISCOVERY_TOPIC, updated)

    _run(scenario())

    assert len(added) == 1  # only the first discovery triggered add_entities
    entity = added[0]
    assert entity._attr_name == "Garage Humidity (renamed)"
    assert entity._attr_device_class is None


def test_redelivery_does_not_resubscribe_state_topic():
    hass = HomeAssistant()
    manager, _added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))

    _run(scenario())

    subs = mqtt._state(hass).subscriptions["share/other_bridge/sensor/garage_humidity"]
    assert len(subs) == 1


def test_ignores_payload_missing_unique_id():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)
    payload = dict(EXAMPLE_PAYLOAD)
    del payload["unique_id"]

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, payload))

    assert added == []


def test_ignores_payload_missing_state_topic():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)
    payload = dict(EXAMPLE_PAYLOAD)
    del payload["state_topic"]

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, payload))

    assert added == []


def test_drops_discovery_when_platform_not_ready_yet():
    hass = HomeAssistant()
    manager = RemoteEntityManager(hass, ConfigEntry())
    # No set_add_entities_callback() call -- platform hasn't set up yet.

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD)))

    assert manager._entities == {}
    assert mqtt._state(hass).subscriptions == {}


def test_unload_unsubscribes_and_clears_tracked_entities():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        second = dict(EXAMPLE_PAYLOAD)
        second["unique_id"] = "other_bridge::sensor.garage_temperature"
        second["state_topic"] = "share/other_bridge/sensor/garage_temperature"
        await manager.async_handle_discovery(
            "share/homeassistant/sensor/garage_temperature/config", second
        )

        await manager.async_unload()

    _run(scenario())

    assert len(added) == 2
    assert manager._entities == {}
    assert manager._state_unsubs == {}
    assert mqtt._state(hass).subscriptions.get("share/other_bridge/sensor/garage_humidity") == []
    assert mqtt._state(hass).subscriptions.get("share/other_bridge/sensor/garage_temperature") == []


# --- device_class safety on the receiving side (issue #13 continued) ---


def test_discovery_drops_device_class_from_non_sensor_source():
    # A peer's binary_sensor.dwd_rain_prediction (device_class "moisture",
    # state "off") crashed HA's own numeric coercion when materialized as
    # a native sensor entity here -- the receiving side must not trust an
    # incoming device_class any more than the sending side does.
    hass = HomeAssistant()
    manager, added = _make_manager(hass)
    payload = dict(EXAMPLE_PAYLOAD)
    payload["unique_id"] = "other_bridge::binary_sensor.dwd_rain_prediction"
    payload["device_class"] = "moisture"

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, payload))

    assert added[0]._attr_device_class is None


def test_discovery_keeps_device_class_from_sensor_source():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD)))

    assert added[0]._attr_device_class == "humidity"


def test_discovery_drops_device_class_when_unique_id_has_no_bridge_prefix():
    # Doesn't match the "{slug}::{entity_id}" convention at all -- fail
    # closed (treat as unsafe) rather than guess.
    hass = HomeAssistant()
    manager, added = _make_manager(hass)
    payload = dict(EXAMPLE_PAYLOAD)
    payload["unique_id"] = "not_our_convention"

    _run(manager.async_handle_discovery(DISCOVERY_TOPIC, payload))

    assert added[0]._attr_device_class is None


def test_redelivery_drops_device_class_on_update_path_too():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        payload = dict(EXAMPLE_PAYLOAD)
        payload["unique_id"] = "other_bridge::binary_sensor.dwd_rain_prediction"
        payload["device_class"] = "humidity"  # starts safe (sensor-shaped)
        await manager.async_handle_discovery(DISCOVERY_TOPIC, payload)

        updated = dict(payload)
        updated["device_class"] = "moisture"
        await manager.async_handle_discovery(DISCOVERY_TOPIC, updated)

    _run(scenario())

    assert len(added) == 1  # update, not a second entity
    assert added[0]._attr_device_class is None


# --- async_handle_removal (issue #7) ---


def test_removal_removes_entity_state_and_subscription():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        entity = added[0]
        entity.set_native_value("42")
        assert hass.states.get(entity.entity_id) is not None

        await manager.async_handle_removal(DISCOVERY_TOPIC)
        return entity

    entity = _run(scenario())

    assert hass.states.get(entity.entity_id) is None
    assert manager._entities == {}
    assert mqtt._state(hass).subscriptions.get("share/other_bridge/sensor/garage_humidity") == []


def test_removal_purges_entity_registry_entry():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        entity_id = added[0].entity_id
        assert er.async_get(hass).async_get(entity_id) is not None

        await manager.async_handle_removal(DISCOVERY_TOPIC)
        return entity_id

    entity_id = _run(scenario())

    assert er.async_get(hass).async_get(entity_id) is None


def test_removal_on_unknown_topic_is_a_noop():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await manager.async_handle_removal("share/homeassistant/sensor/never_seen/config")

    _run(scenario())

    # The unrelated topic's removal didn't touch the entity we do know about.
    assert len(added) == 1
    assert added[0].unique_id in manager._entities


def test_rediscovery_after_removal_creates_a_fresh_entity():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await manager.async_handle_removal(DISCOVERY_TOPIC)
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))

    _run(scenario())

    assert len(added) == 2
    assert "other_bridge::sensor.garage_humidity" in manager._entities


# --- async_handle_remote_metadata (§9, issue #12 follow-up) ---

METADATA_PAYLOAD = {
    "protocol_version": 1,
    "integration_version": "0.1.3",
    "bridge_id": "other_bridge",
    "ha_version": "2026.8.0",
    "entity_count": 3,
    "last_heartbeat": "2026-08-06T08:14:00+00:00",
}


def test_metadata_ignored_for_bridge_with_no_known_entities():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    _run(manager.async_handle_remote_metadata("other_bridge", dict(METADATA_PAYLOAD)))

    assert added == []


def test_metadata_creates_diagnostic_entities_for_known_bridge():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await manager.async_handle_remote_metadata("other_bridge", dict(METADATA_PAYLOAD))

    _run(scenario())

    # 1 federated entity + 3 diagnostic entities.
    assert len(added) == 4
    diagnostic_entities = added[1:]
    assert {e.unique_id for e in diagnostic_entities} == {
        "other_bridge::entity_count",
        "other_bridge::last_heartbeat",
        "other_bridge::ha_version",
    }
    for entity in diagnostic_entities:
        assert entity._attr_device_info["identifiers"] == {(DOMAIN, "other_bridge")}
        assert entity._attr_device_info["name"] == "Bridge Other"  # from the discovery payload
        assert "0.1.3" in entity._attr_device_info["sw_version"]
        assert "protocol v1" in entity._attr_device_info["sw_version"]


def test_metadata_redelivery_updates_values_without_readding():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await manager.async_handle_remote_metadata("other_bridge", dict(METADATA_PAYLOAD))
        updated = dict(METADATA_PAYLOAD)
        updated["entity_count"] = 5
        await manager.async_handle_remote_metadata("other_bridge", updated)

    _run(scenario())

    assert len(added) == 4  # no duplicate diagnostic entities on redelivery
    holder = manager._remote_metadata_entities["other_bridge"]
    assert holder.entity_count.native_value == "5"


def test_metadata_diagnostic_entities_removed_when_last_entity_removed():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await manager.async_handle_remote_metadata("other_bridge", dict(METADATA_PAYLOAD))
        await manager.async_handle_removal(DISCOVERY_TOPIC)

    _run(scenario())

    assert "other_bridge" not in manager._remote_metadata_entities
    assert hass.states.get("sensor.other_bridge__entity_count") is None


# --- async_depublish_bridge (grapevine.depublish_bridge service) ---


def test_depublish_bridge_publishes_empty_retained_to_every_tracked_topic():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)
    second_topic = "share/homeassistant/sensor/garage_temperature/config"

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        second = dict(EXAMPLE_PAYLOAD)
        second["unique_id"] = "other_bridge::sensor.garage_temperature"
        second["state_topic"] = "share/other_bridge/sensor/garage_temperature"
        await manager.async_handle_discovery(second_topic, second)

        return await manager.async_depublish_bridge("other_bridge")

    removed = _run(scenario())

    assert removed == 2
    published = {(topic, payload, retain) for topic, payload, retain in mqtt._state(hass).published}
    assert (DISCOVERY_TOPIC, "", True) in published
    assert (second_topic, "", True) in published


def test_depublish_bridge_removes_entities_locally_immediately():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        entity_id = added[0].entity_id
        await manager.async_depublish_bridge("other_bridge")
        return entity_id

    entity_id = _run(scenario())

    assert manager._entities == {}
    assert hass.states.get(entity_id) is None
    assert er.async_get(hass).async_get(entity_id) is None
    assert mqtt._state(hass).subscriptions.get("share/other_bridge/sensor/garage_humidity") == []


def test_depublish_bridge_also_removes_its_diagnostic_entities():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        await manager.async_handle_remote_metadata("other_bridge", dict(METADATA_PAYLOAD))
        await manager.async_depublish_bridge("other_bridge")

    _run(scenario())

    assert "other_bridge" not in manager._remote_metadata_entities
    assert hass.states.get("sensor.other_bridge__entity_count") is None


def test_depublish_bridge_is_a_noop_for_unknown_bridge_id():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        return await manager.async_depublish_bridge("some_other_bridge")

    removed = _run(scenario())

    assert removed == 0
    assert mqtt._state(hass).published == []
    # The unrelated bridge's entity is untouched.
    assert len(added) == 1
    assert added[0].unique_id in manager._entities


def test_depublish_bridge_does_not_touch_a_different_bridges_entity():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)
    other_topic = "share/homeassistant/sensor/attic_humidity/config"

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        third = dict(EXAMPLE_PAYLOAD)
        third["unique_id"] = "third_bridge::sensor.attic_humidity"
        third["state_topic"] = "share/third_bridge/sensor/attic_humidity"
        third["bridge_id"] = "third_bridge"
        third["device"] = {"identifiers": ["third_bridge"], "name": "Bridge Third", "sw_version": "1.0.3"}
        await manager.async_handle_discovery(other_topic, third)

        return await manager.async_depublish_bridge("other_bridge")

    removed = _run(scenario())

    assert removed == 1
    assert len(added) == 2
    assert "third_bridge::sensor.attic_humidity" in manager._entities
    assert "other_bridge::sensor.garage_humidity" not in manager._entities


def test_metadata_diagnostic_entities_survive_partial_entity_removal():
    hass = HomeAssistant()
    manager, added = _make_manager(hass)
    second_topic = "share/homeassistant/sensor/garage_temperature/config"

    async def scenario():
        await manager.async_handle_discovery(DISCOVERY_TOPIC, dict(EXAMPLE_PAYLOAD))
        second = dict(EXAMPLE_PAYLOAD)
        second["unique_id"] = "other_bridge::sensor.garage_temperature"
        second["state_topic"] = "share/other_bridge/sensor/garage_temperature"
        await manager.async_handle_discovery(second_topic, second)
        await manager.async_handle_remote_metadata("other_bridge", dict(METADATA_PAYLOAD))

        await manager.async_handle_removal(DISCOVERY_TOPIC)  # only one of the two

    _run(scenario())

    assert "other_bridge" in manager._remote_metadata_entities
    assert hass.states.get("sensor.other_bridge__entity_count") is not None
