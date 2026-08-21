"""SaulachOptionsFlow (issue #7), exercised against the real
async_setup_entry/async_unload_entry/async_reload path so "did the change
actually take effect" is genuinely tested, not just "did the flow return
the right dict." This flow is the sole "Configure" entry point -- entities,
both prefixes, bridge name, and the republish interval all live in one
step, since splitting them across "Configure" and a separate "Reconfigure"
action wasn't discoverable in practice (issue #7's reopening).
"""

import asyncio

import pytest

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components import saulach
from custom_components.saulach import scheduler as scheduler_module
from custom_components.saulach.config_flow import SaulachOptionsFlow
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


def _make_entry(entry_id: str, bridge_name: str, entities: list[str], minutes: int = 1) -> ConfigEntry:
    return ConfigEntry(
        entry_id=entry_id,
        unique_id=bridge_name.lower().replace(" ", "_"),
        data={
            CONF_ENTITIES: entities,
            CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant/",
            CONF_SENSOR_VALUE_PREFIX: "share/jakob/",
            CONF_BRIDGE_NAME: bridge_name,
        },
        options={CONF_TIME_PATTERN_MINUTES: minutes},
    )


def _run(coro):
    return asyncio.run(coro)


async def _drain_hass_tasks(hass: HomeAssistant) -> None:
    """Update-entry-triggered reloads (and the republishes a reload
    itself schedules) run as background tasks; drain until none remain."""
    while hass._tasks:
        await asyncio.gather(*list(hass._tasks), return_exceptions=True)


def _options_flow(hass: HomeAssistant, entry: ConfigEntry) -> SaulachOptionsFlow:
    flow = SaulachOptionsFlow()
    flow.hass = hass
    flow.config_entry = entry
    return flow


def _submit(entry: ConfigEntry, *, bridge_name=None, entities=None, minutes=None) -> dict:
    return {
        CONF_BRIDGE_NAME: bridge_name if bridge_name is not None else entry.data[CONF_BRIDGE_NAME],
        CONF_ENTITIES: entities if entities is not None else entry.data[CONF_ENTITIES],
        CONF_SHARED_DISCOVERY_PREFIX: entry.data[CONF_SHARED_DISCOVERY_PREFIX],
        CONF_SENSOR_VALUE_PREFIX: entry.data[CONF_SENSOR_VALUE_PREFIX],
        CONF_TIME_PATTERN_MINUTES: minutes if minutes is not None else entry.options[CONF_TIME_PATTERN_MINUTES],
    }


def test_options_flow_shows_form_prefilled_with_current_values():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"], minutes=5)
    flow = _options_flow(hass, entry)

    result = _run(flow.async_step_init(None))

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {}


def test_options_flow_rejects_invalid_bridge_name():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    flow = _options_flow(hass, entry)

    result = _run(flow.async_step_init(_submit(entry, bridge_name="!!!")))

    assert result["errors"] == {"base": "invalid_bridge_name"}
    assert entry.data[CONF_BRIDGE_NAME] == "Bridge Jakob"  # unchanged


def test_options_flow_rejects_empty_entities():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    flow = _options_flow(hass, entry)

    result = _run(flow.async_step_init(_submit(entry, entities=[])))

    assert result["errors"] == {"base": "no_entities"}


def test_options_flow_deduplicates_repeated_entity_selection():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    flow = _options_flow(hass, entry)

    result = _run(flow.async_step_init(_submit(entry, entities=["sensor.a", "sensor.b", "sensor.a"])))

    assert result["type"] == "create_entry"
    assert entry.data[CONF_ENTITIES] == ["sensor.a", "sensor.b"]


def test_options_flow_rejects_rename_colliding_with_another_entry():
    hass = HomeAssistant()
    entry_a = _make_entry("entry_a", "Bridge A", ["sensor.a"])
    entry_b = _make_entry("entry_b", "Bridge B", ["sensor.b"])
    hass.config_entries.entries.extend([entry_a, entry_b])
    flow = _options_flow(hass, entry_a)

    result = _run(flow.async_step_init(_submit(entry_a, bridge_name="Bridge B")))

    assert result["errors"] == {"base": "already_configured"}
    assert entry_a.data[CONF_BRIDGE_NAME] == "Bridge A"  # unchanged


def test_options_flow_keeping_same_bridge_name_does_not_self_collide():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    hass.config_entries.entries.append(entry)

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await _drain_hass_tasks(hass)

        flow = _options_flow(hass, entry)
        return await flow.async_step_init(_submit(entry))  # bridge_name unchanged

    result = _run(scenario())

    assert result["type"] == "create_entry"


def test_options_flow_updates_entities_and_reloads():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    hass.config_entries.entries.append(entry)
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.c", "3")

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await _drain_hass_tasks(hass)

        flow = _options_flow(hass, entry)
        result = await flow.async_step_init(_submit(entry, entities=["sensor.a", "sensor.c"]))
        await _drain_hass_tasks(hass)
        return result

    result = _run(scenario())

    assert result["type"] == "create_entry"
    assert entry.data[CONF_ENTITIES] == ["sensor.a", "sensor.c"]
    # The reload actually happened: the new entity got republished too.
    published_topics = {topic for topic, _, _ in mqtt._state(hass).published}
    assert "share/homeassistant/sensor/c/config" in published_topics


def test_options_flow_removing_entity_depublishes_it_before_reload():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a", "sensor.b"])
    hass.config_entries.entries.append(entry)
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await _drain_hass_tasks(hass)
        mqtt._state(hass).published.clear()

        flow = _options_flow(hass, entry)
        await flow.async_step_init(_submit(entry, entities=["sensor.a"]))  # sensor.b dropped
        await _drain_hass_tasks(hass)

    _run(scenario())

    published = {(topic, payload) for topic, payload, _retain in mqtt._state(hass).published}
    # Depublished (empty retained payload) rather than left stale.
    assert ("share/homeassistant/sensor/b/config", "") in published
    assert ("share/jakob/sensor/b", "") in published
    assert entry.data[CONF_ENTITIES] == ["sensor.a"]


def test_options_flow_change_updates_options_and_actually_reloads():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"], minutes=1)
    hass.config_entries.entries.append(entry)

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await _drain_hass_tasks(hass)
        assert entry.runtime_data.scheduler._minutes == 1

        flow = _options_flow(hass, entry)
        result = await flow.async_step_init(_submit(entry, minutes=5))
        await _drain_hass_tasks(hass)
        return result

    result = _run(scenario())

    assert result["type"] == "create_entry"
    assert entry.options[CONF_TIME_PATTERN_MINUTES] == 5
    # The core bug behind issue #7: previously this new value would sit in
    # entry.options forever without the running scheduler ever noticing.
    assert entry.runtime_data.scheduler._minutes == 5
