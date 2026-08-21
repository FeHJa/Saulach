"""LegacyDiscoveryAdapter tests against the fake homeassistant.components.mqtt
in tests/ha_stubs — exercises publish/loop-guard through the real mqtt_io.py
wrapper, not just discovery.py's pure functions directly. Incoming-message
handling (§5a) is tested here only up to "did the loop guard let it through
and get handed to the entity manager" — RemoteEntityManager's own behavior
(entity creation/update) is covered in test_remote_entity_manager.py.
"""

import asyncio
import json

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State

from custom_components.saulach import mqtt_io
from custom_components.saulach.adapters.legacy_discovery import LegacyDiscoveryAdapter
from custom_components.saulach.const import (
    CONF_BRIDGE_NAME,
    CONF_SENSOR_VALUE_PREFIX,
    CONF_SHARED_DISCOVERY_PREFIX,
)


class RecordingEntityManager:
    def __init__(self) -> None:
        self.handled: list[dict] = []
        self.handled_topics: list[str] = []
        self.removed_topics: list[str] = []
        self.handled_metadata: list[tuple[str, dict]] = []

    async def async_handle_discovery(self, topic: str, payload_data: dict) -> None:
        self.handled.append(payload_data)
        self.handled_topics.append(topic)

    async def async_handle_removal(self, topic: str) -> None:
        self.removed_topics.append(topic)

    async def async_handle_remote_metadata(self, bridge_id: str, payload_data: dict) -> None:
        self.handled_metadata.append((bridge_id, payload_data))


def _make_adapter(hass: HomeAssistant, manager: RecordingEntityManager) -> LegacyDiscoveryAdapter:
    entry = ConfigEntry(
        data={
            CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant/",
            CONF_SENSOR_VALUE_PREFIX: "share/jakob/",
            CONF_BRIDGE_NAME: "Bridge Jakob",
        }
    )
    return LegacyDiscoveryAdapter(hass, entry, manager, "0.1.3")


def _run(coro):
    return asyncio.run(coro)


def _published(hass: HomeAssistant) -> list[tuple[str, str, bool]]:
    return mqtt._state(hass).published


# --- publish_own_entity ---


def test_publish_own_entity_publishes_discovery_and_state_retained():
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())
    state = State("sensor.garage_temperature", "21.5", {"friendly_name": "Garage Temperature"})

    _run(adapter.publish_own_entity("sensor.garage_temperature", state))

    published = _published(hass)
    assert len(published) == 2

    discovery_topic, discovery_payload, discovery_retain = published[0]
    assert discovery_topic == "share/homeassistant/sensor/garage_temperature/config"
    assert discovery_retain is True
    payload = json.loads(discovery_payload)
    assert payload["name"] == "Garage Temperature"
    assert payload["state_topic"] == "share/jakob/sensor/garage_temperature"
    assert payload["unique_id"] == "bridge_jakob::sensor.garage_temperature"
    assert payload["device_class"] == "temperature"
    assert payload["unit_of_measurement"] == "°C"
    assert payload["bridge_id"] == "bridge_jakob"
    assert payload["protocol_version"] == 1
    assert payload["device"] == {
        "identifiers": ["bridge_jakob"],
        "name": "Bridge Jakob",
        "sw_version": "1.0.3",
    }

    state_topic, state_payload, state_retain = published[1]
    assert state_topic == "share/jakob/sensor/garage_temperature"
    assert state_payload == "21.5"  # raw string, no JSON wrapping (§4)
    assert state_retain is True


def test_publish_own_entity_drops_device_class_invalid_for_sensor_platform():
    # issue #13: a binary_sensor's real device_class (e.g. "light",
    # "motion") isn't a valid SensorDeviceClass, but this bridge always
    # publishes component "sensor" (§2) -- forwarding it verbatim made a
    # blueprint-based receiver's own mqtt integration reject the whole
    # discovery message outright.
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())
    state = State(
        "binary_sensor.pv_sunny", "on", {"friendly_name": "PV Sunny", "device_class": "light"}
    )

    _run(adapter.publish_own_entity("binary_sensor.pv_sunny", state))

    discovery_topic, discovery_payload, _ = _published(hass)[0]
    payload = json.loads(discovery_payload)
    assert "device_class" not in payload


def test_publish_own_entity_drops_device_class_that_names_match_but_wrong_domain():
    # issue #13 continued: "moisture" IS a valid SensorDeviceClass name
    # (unlike "light"), so the name-only check alone doesn't drop it --
    # but it's a numeric device class in the sensor domain while this
    # source is a binary_sensor with an "on"/"off" state. Forwarding it
    # made HA's own numeric coercion crash on the receiving side
    # ("...has device class 'moisture'... thus indicating it has a
    # numeric value; however, it has the non-numeric value: 'off'").
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())
    state = State(
        "binary_sensor.dwd_rain_prediction",
        "off",
        {"friendly_name": "DWD Rain Prediction", "device_class": "moisture"},
    )

    _run(adapter.publish_own_entity("binary_sensor.dwd_rain_prediction", state))

    _, discovery_payload, _ = _published(hass)[0]
    payload = json.loads(discovery_payload)
    assert "device_class" not in payload


def test_publish_own_entity_keeps_device_class_valid_for_sensor_platform():
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())
    state = State(
        "sensor.garage_humidity", "50", {"friendly_name": "Garage Humidity", "device_class": "humidity"}
    )

    _run(adapter.publish_own_entity("sensor.garage_humidity", state))

    _, discovery_payload, _ = _published(hass)[0]
    payload = json.loads(discovery_payload)
    assert payload["device_class"] == "humidity"


def test_publish_own_entity_uses_shared_discovery_prefix_for_topic():
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())
    state = State("sensor.x", "1", {})

    _run(adapter.publish_own_entity("sensor.x", state))

    discovery_topic, _, _ = _published(hass)[0]
    assert discovery_topic.startswith("share/homeassistant/")


# --- async_depublish_entity (issue #7) ---


def test_depublish_publishes_empty_retained_payload_to_both_topics():
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())

    _run(adapter.async_depublish_entity("sensor.garage_temperature"))

    published = _published(hass)
    assert published == [
        ("share/homeassistant/sensor/garage_temperature/config", "", True),
        ("share/jakob/sensor/garage_temperature", "", True),
    ]


# --- async_publish_metadata (§9, issue #12) ---


def test_publish_metadata_publishes_retained_payload_with_entity_count():
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())

    payload = _run(adapter.async_publish_metadata(5))

    published = _published(hass)
    assert len(published) == 1
    topic, raw_payload, retain = published[0]
    assert topic == "share/homeassistant/bridge/bridge_jakob/metadata"
    assert retain is True
    assert json.loads(raw_payload) == payload
    assert payload["bridge_id"] == "bridge_jakob"
    assert payload["entity_count"] == 5
    assert "last_heartbeat" in payload
    assert "ha_version" in payload
    assert "integration_version" in payload
    assert payload["protocol_version"] == 1


def test_publish_metadata_topic_does_not_match_discovery_subscription():
    # Load-bearing: the metadata topic must never be mistaken for a §2
    # discovery message by anything (this bridge included) subscribed to
    # {shared_discovery_prefix}+/+/config.
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())

    _run(adapter.async_publish_metadata(1))

    topic, _, _ = _published(hass)[0]
    prefix = "share/homeassistant/"
    assert topic.startswith(prefix)
    remainder = topic[len(prefix) :].split("/")
    assert not (len(remainder) == 3 and remainder[-1] == "config")


# --- topics_to_subscribe ---


def test_topics_to_subscribe_uses_configured_shared_prefix():
    hass = HomeAssistant()
    adapter = _make_adapter(hass, RecordingEntityManager())
    assert adapter.topics_to_subscribe() == [
        "share/homeassistant/+/+/config",
        "share/homeassistant/bridge/+/metadata",
    ]


# --- handle_incoming_message: loop guard + dispatch to entity manager (§5/§5a) ---


def test_hands_foreign_bridge_discovery_to_entity_manager():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)
    raw_payload = '{"bridge_id": "other_bridge", "unique_id": "other_bridge::sensor.y", "extra": 1}'

    _run(
        adapter.handle_incoming_message(
            "share/homeassistant/sensor/garage_humidity/config", raw_payload
        )
    )

    assert manager.handled == [
        {"bridge_id": "other_bridge", "unique_id": "other_bridge::sensor.y", "extra": 1}
    ]
    # §5a: nothing is forwarded/published locally anymore.
    assert _published(hass) == []


def test_does_not_hand_own_message_by_bridge_id_to_entity_manager():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)
    own_payload = json.dumps({"bridge_id": "bridge_jakob", "unique_id": "bridge_jakob::sensor.x"})

    _run(adapter.handle_incoming_message("share/homeassistant/sensor/x/config", own_payload))

    assert manager.handled == []


def test_does_not_hand_own_message_by_unique_id_prefix_to_entity_manager():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)
    own_payload = json.dumps({"unique_id": "bridge_jakob.sensor.x"})

    _run(adapter.handle_incoming_message("share/homeassistant/sensor/x/config", own_payload))

    assert manager.handled == []


def test_ignores_non_json_payload_without_raising():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    _run(adapter.handle_incoming_message("share/homeassistant/sensor/x/config", "not json"))

    assert manager.handled == []


def test_ignores_message_on_unmatched_topic_shape():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    _run(adapter.handle_incoming_message("some/other/topic", '{"bridge_id": "x"}'))

    assert manager.handled == []


# --- handle_incoming_message: routing remote metadata (§9, issue #12 follow-up) ---


def test_incoming_metadata_routes_to_entity_manager():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)
    raw_payload = '{"bridge_id": "other_bridge", "entity_count": 3}'

    _run(
        adapter.handle_incoming_message(
            "share/homeassistant/bridge/other_bridge/metadata", raw_payload
        )
    )

    assert manager.handled_metadata == [("other_bridge", {"bridge_id": "other_bridge", "entity_count": 3})]
    assert manager.handled == []


def test_incoming_metadata_for_own_bridge_is_ignored():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    _run(
        adapter.handle_incoming_message(
            "share/homeassistant/bridge/bridge_jakob/metadata", '{"bridge_id": "bridge_jakob"}'
        )
    )

    assert manager.handled_metadata == []


def test_incoming_metadata_non_json_payload_is_ignored():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    _run(
        adapter.handle_incoming_message(
            "share/homeassistant/bridge/other_bridge/metadata", "not json"
        )
    )

    assert manager.handled_metadata == []


def test_incoming_metadata_empty_payload_is_ignored():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    _run(adapter.handle_incoming_message("share/homeassistant/bridge/other_bridge/metadata", ""))

    assert manager.handled_metadata == []
    assert manager.removed_topics == []


# --- empty payload = removal signal (issue #7) ---


def test_empty_payload_routes_to_removal_by_topic():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    _run(
        adapter.handle_incoming_message(
            "share/homeassistant/sensor/garage_humidity/config", ""
        )
    )

    assert manager.removed_topics == ["share/homeassistant/sensor/garage_humidity/config"]
    assert manager.handled == []


def test_empty_payload_on_unmatched_topic_shape_is_not_routed_to_removal():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    _run(adapter.handle_incoming_message("some/other/topic", ""))

    assert manager.removed_topics == []


# --- end-to-end through mqtt_io subscribe + fake broker delivery ---


def test_subscribed_topic_delivers_to_handler_and_reaches_entity_manager():
    hass = HomeAssistant()
    manager = RecordingEntityManager()
    adapter = _make_adapter(hass, manager)

    async def scenario():
        for topic in adapter.topics_to_subscribe():
            await mqtt_io.async_subscribe(hass, topic, adapter.async_handle_mqtt_message)

        payload = json.dumps({"bridge_id": "other_bridge", "unique_id": "other_bridge::sensor.y"})
        await mqtt.async_fire_mqtt_message(
            hass, "share/homeassistant/sensor/garage_humidity/config", payload
        )

    _run(scenario())

    assert manager.handled == [
        {"bridge_id": "other_bridge", "unique_id": "other_bridge::sensor.y"}
    ]
