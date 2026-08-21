"""Unit tests for discovery.py — the pure wire-protocol logic.

These encode PROTOCOL.md as an executable spec. No Home Assistant
dependency required: discovery.py has none by design.
"""

from custom_components.saulach.const import PROTOCOL_VERSION, SW_VERSION
from custom_components.saulach.discovery import (
    build_discovery_payload,
    build_metadata_payload,
    domain_from_entity_id,
    domain_from_unique_id,
    is_own_message,
    normalize_prefix,
    object_id_from_entity_id,
    parse_federation_topic,
    parse_metadata_topic,
    resolve_device_class_and_unit,
    slugify_bridge_name,
)


# --- slugify_bridge_name (§1) ---


def test_slugify_bridge_name_lowercases_and_replaces_spaces():
    assert slugify_bridge_name("Bridge Jakob") == "bridge_jakob"


def test_slugify_bridge_name_strips_invalid_chars():
    assert slugify_bridge_name("Bridge! Jakob (Attic)") == "bridge_jakob_attic"


def test_slugify_bridge_name_empty_when_no_valid_chars():
    assert slugify_bridge_name("!!!") == ""


# --- object_id_from_entity_id (§2) ---


def test_object_id_strips_domain():
    assert object_id_from_entity_id("sensor.garage_temperature") == "garage_temperature"


def test_object_id_handles_dots_in_object_id():
    # split('.')[-1] ported verbatim: only the last segment is used.
    assert object_id_from_entity_id("sensor.weird.id") == "id"


def test_domain_from_entity_id():
    assert domain_from_entity_id("binary_sensor.pv_sunny") == "binary_sensor"
    assert domain_from_entity_id("sensor.garage_temperature") == "sensor"


def test_domain_from_unique_id_recovers_source_domain():
    assert domain_from_unique_id("bridge_jakob::binary_sensor.dwd_rain_prediction") == "binary_sensor"
    assert domain_from_unique_id("bridge_jakob::sensor.garage_temperature") == "sensor"


def test_domain_from_unique_id_returns_none_without_bridge_prefix():
    assert domain_from_unique_id("not_our_convention") is None


# --- normalize_prefix ---


def test_normalize_prefix_adds_trailing_slash():
    assert normalize_prefix("share/homeassistant") == "share/homeassistant/"


def test_normalize_prefix_leaves_existing_slash():
    assert normalize_prefix("share/homeassistant/") == "share/homeassistant/"


# --- resolve_device_class_and_unit (§3) ---


def test_resolve_prefers_entity_attributes():
    device_class, unit = resolve_device_class_and_unit(
        "garage_temperature", device_class="carbon_dioxide", unit_of_measurement="ppm"
    )
    assert (device_class, unit) == ("carbon_dioxide", "ppm")


def test_resolve_falls_back_to_regex_table():
    device_class, unit = resolve_device_class_and_unit(
        "garage_temperature", device_class=None, unit_of_measurement=None
    )
    assert (device_class, unit) == ("temperature", "°C")


def test_resolve_regex_table_all_eight_patterns():
    expected = {
        "temperature": ("temperature", "°C"),
        "humidity": ("humidity", "%"),
        "pressure": ("pressure", "hPa"),
        "power": ("power", "W"),
        "energy": ("energy", "kWh"),
        "current": ("current", "A"),
        "voltage": ("voltage", "V"),
        "light": ("illuminance", "lx"),
    }
    for word, expected_pair in expected.items():
        object_id = f"garage_{word}_sensor"
        assert resolve_device_class_and_unit(object_id, None, None) == expected_pair


def test_resolve_pattern_requires_word_boundary():
    # "temperatures" must NOT match "temperature" per (^|_)word(_|$).
    device_class, unit = resolve_device_class_and_unit("outdoor_temperatures", None, None)
    assert device_class is None
    assert unit is None


def test_resolve_first_match_wins_when_multiple_words_present():
    # temperature listed before humidity in the table.
    device_class, unit = resolve_device_class_and_unit("temperature_and_humidity", None, None)
    assert (device_class, unit) == ("temperature", "°C")


def test_resolve_no_match_returns_none_none():
    device_class, unit = resolve_device_class_and_unit("front_door", None, None)
    assert (device_class, unit) == (None, None)


def test_resolve_independent_per_field():
    # device_class present, unit missing -> unit filled from regex table,
    # entity-provided device_class is preserved (not overwritten).
    device_class, unit = resolve_device_class_and_unit(
        "garage_temperature", device_class="custom_class", unit_of_measurement=None
    )
    assert (device_class, unit) == ("custom_class", "°C")


# --- build_discovery_payload (§3) ---


def _payload(**overrides):
    kwargs = {
        "entity_id": "sensor.garage_temperature",
        "friendly_name": None,
        "device_class": None,
        "unit_of_measurement": None,
        "bridge_name": "Bridge Jakob",
        "slug_bridge_name": "bridge_jakob",
        "sensor_value_prefix": "share/jakob/",
    }
    kwargs.update(overrides)
    return build_discovery_payload(**kwargs)


def test_payload_shape_matches_protocol_contract():
    payload = _payload(friendly_name="Garage Temperature")
    assert payload == {
        "name": "Garage Temperature",
        "state_topic": "share/jakob/sensor/garage_temperature",
        "unique_id": "bridge_jakob::sensor.garage_temperature",
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "bridge_id": "bridge_jakob",
        "protocol_version": PROTOCOL_VERSION,
        "device": {
            "identifiers": ["bridge_jakob"],
            "name": "Bridge Jakob",
            "sw_version": SW_VERSION,
        },
    }


def test_payload_name_falls_back_to_title_cased_object_id():
    payload = _payload(entity_id="sensor.front_door", friendly_name=None)
    assert payload["name"] == "Front Door"


def test_payload_omits_device_class_and_unit_entirely_when_unknown():
    payload = _payload(entity_id="binary_sensor.front_door", friendly_name="Front Door")
    assert "device_class" not in payload
    assert "unit_of_measurement" not in payload


def test_payload_protocol_version_is_one_in_phase_1():
    payload = _payload()
    assert payload["protocol_version"] == 1


# --- build_metadata_payload (§9, issue #12) ---


def test_metadata_payload_shape_matches_protocol_contract():
    payload = build_metadata_payload(
        slug_bridge_name="bridge_jakob",
        integration_version="0.1.3",
        ha_version="2026.8.0",
        entity_count=3,
        last_heartbeat="2026-08-06T08:14:00+00:00",
    )
    assert payload == {
        "protocol_version": PROTOCOL_VERSION,
        "integration_version": "0.1.3",
        "bridge_id": "bridge_jakob",
        "ha_version": "2026.8.0",
        "entity_count": 3,
        "last_heartbeat": "2026-08-06T08:14:00+00:00",
    }


# --- parse_federation_topic (§2/§5) ---


def test_parse_federation_topic_extracts_component_and_object_id():
    result = parse_federation_topic(
        "share/homeassistant/sensor/garage_temperature/config", "share/homeassistant/"
    )
    assert result == ("sensor", "garage_temperature")


def test_parse_federation_topic_returns_none_for_mismatched_prefix():
    assert parse_federation_topic("other/prefix/sensor/x/config", "share/homeassistant/") is None


def test_parse_federation_topic_returns_none_for_too_short_remainder():
    assert parse_federation_topic("share/homeassistant/sensor", "share/homeassistant/") is None


# --- parse_metadata_topic (§9, issue #12 follow-up) ---


def test_parse_metadata_topic_extracts_bridge_id():
    result = parse_metadata_topic(
        "share/homeassistant/bridge/other_bridge/metadata", "share/homeassistant/"
    )
    assert result == "other_bridge"


def test_parse_metadata_topic_returns_none_for_mismatched_prefix():
    assert parse_metadata_topic("other/prefix/bridge/x/metadata", "share/homeassistant/") is None


def test_parse_metadata_topic_returns_none_for_discovery_shaped_topic():
    # Must not collide with the §2 discovery pattern (2 segments ending
    # in "config") -- this is the whole point of the topic shape (§9).
    assert (
        parse_metadata_topic("share/homeassistant/sensor/garage_temperature/config", "share/homeassistant/")
        is None
    )


def test_parse_metadata_topic_returns_none_for_wrong_middle_segment():
    assert parse_metadata_topic("share/homeassistant/other/x/metadata", "share/homeassistant/") is None


# --- is_own_message (§5 loop guard) ---


def test_is_own_message_true_on_bridge_id_match():
    assert is_own_message({"bridge_id": "bridge_jakob"}, "bridge_jakob") is True


def test_is_own_message_true_on_unique_id_double_colon_prefix():
    assert is_own_message({"unique_id": "bridge_jakob::sensor.x"}, "bridge_jakob") is True


def test_is_own_message_true_on_unique_id_dot_prefix():
    assert is_own_message({"unique_id": "bridge_jakob.sensor.x"}, "bridge_jakob") is True


def test_is_own_message_false_for_other_bridge():
    payload = {"bridge_id": "other_bridge", "unique_id": "other_bridge::sensor.x"}
    assert is_own_message(payload, "bridge_jakob") is False


def test_is_own_message_false_when_neither_key_present():
    assert is_own_message({}, "bridge_jakob") is False


def test_is_own_message_does_not_false_positive_on_substring():
    # "bridge_jakob_2" must not be treated as own just because it starts
    # with "bridge_jakob" without the required "::" or "." separator.
    payload = {"unique_id": "bridge_jakob_2::sensor.x", "bridge_id": "bridge_jakob_2"}
    assert is_own_message(payload, "bridge_jakob") is False
