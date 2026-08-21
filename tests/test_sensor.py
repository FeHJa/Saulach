"""Tests for sensor.py's BridgeMetadataEntities holder (PROTOCOL.md §9) --
used for remote bridges' diagnostic entities (test_remote_entity_manager.py
covers that wiring) and, historically, this bridge's own diagnostic
entities too, until that local surfacing was reverted per user feedback
(kept out of the entity list; own metadata is wire-only, see PROTOCOL.md
§9's "Local surfacing" note). test_setup_entry_creates_no_own_bridge_
diagnostic_entities below guards against that regressing.
"""

import asyncio

import pytest

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
from custom_components.saulach.sensor import BridgeMetadataEntities


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda a, b: 0)


def _run(coro):
    return asyncio.run(coro)


# --- BridgeMetadataEntities in isolation ---


def test_metadata_entities_share_one_device():
    entities = BridgeMetadataEntities(
        bridge_name="Bridge Jakob",
        slug_bridge_name="bridge_jakob",
        integration_version="0.1.3",
        protocol_version=1,
    )

    for entity in entities.entities:
        assert entity._attr_device_info["identifiers"] == {("saulach", "bridge_jakob")}
        assert entity._attr_device_info["name"] == "Bridge Jakob"


def test_metadata_entities_are_diagnostic_category():
    entities = BridgeMetadataEntities(
        bridge_name="Bridge Jakob",
        slug_bridge_name="bridge_jakob",
        integration_version="0.1.3",
        protocol_version=1,
    )
    assert all(e._attr_entity_category == EntityCategory.DIAGNOSTIC for e in entities.entities)


def test_metadata_entities_update_sets_native_values():
    hass = HomeAssistant()
    entities = BridgeMetadataEntities(
        bridge_name="Bridge Jakob",
        slug_bridge_name="bridge_jakob",
        integration_version="0.1.3",
        protocol_version=1,
    )
    for entity, suffix in zip(entities.entities, ["count", "heartbeat", "haversion"]):
        entity.hass = hass
        entity.entity_id = f"sensor.{suffix}"

    entities.update(
        {"entity_count": 3, "last_heartbeat": "2026-08-06T08:14:00+00:00", "ha_version": "2026.8.0"}
    )

    assert entities.entity_count.native_value == "3"
    assert entities.last_heartbeat.native_value == "2026-08-06T08:14:00+00:00"
    assert entities.ha_version.native_value == "2026.8.0"


# --- end-to-end through async_setup_entry: own bridge stays undiagnosed ---


def _make_entry(entities: list[str]) -> ConfigEntry:
    return ConfigEntry(
        entry_id="entry1",
        data={
            CONF_ENTITIES: entities,
            CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant/",
            CONF_SENSOR_VALUE_PREFIX: "share/jakob/",
            CONF_BRIDGE_NAME: "Bridge Jakob",
        },
        options={CONF_TIME_PATTERN_MINUTES: 1},
    )


async def _drain_hass_tasks(hass: HomeAssistant) -> None:
    while hass._tasks:
        await asyncio.gather(*list(hass._tasks), return_exceptions=True)


def test_setup_entry_creates_no_own_bridge_diagnostic_entities():
    hass = HomeAssistant()
    entry = _make_entry(["sensor.a"])
    hass.states.async_set("sensor.a", "1")

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await _drain_hass_tasks(hass)

    _run(scenario())

    # unique_id "bridge_jakob::entity_count" -> the fake entity-id slugifier
    # turns "::" into a double underscore.
    assert hass.states.get("sensor.bridge_jakob__entity_count") is None
    assert hass.states.get("sensor.bridge_jakob__last_heartbeat") is None
    assert hass.states.get("sensor.bridge_jakob__ha_version") is None
    # The bridged entity itself is still published as normal.
    published_topics = {topic for topic, _, _ in mqtt._state(hass).published}
    assert "share/homeassistant/sensor/a/config" in published_topics
    # ...and metadata still goes out on the wire, just not as local entities.
    assert entry.runtime_data.scheduler.last_metadata is not None
