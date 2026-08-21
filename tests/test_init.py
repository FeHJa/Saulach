"""__init__.py tests: entry setup/unload wiring and the domain-wide
republish service (registered once, dispatched per config entry).
"""

import asyncio
import json

import pytest

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components import saulach
from custom_components.saulach import scheduler as scheduler_module
from custom_components.saulach.const import (
    ATTR_BRIDGE_DEVICE,
    ATTR_CONFIG_ENTRY_ID,
    CONF_BRIDGE_NAME,
    CONF_ENTITIES,
    CONF_SENSOR_VALUE_PREFIX,
    CONF_SHARED_DISCOVERY_PREFIX,
    CONF_TIME_PATTERN_MINUTES,
    DOMAIN,
    SERVICE_DEPUBLISH_BRIDGE,
    SERVICE_REPUBLISH,
)
from custom_components.saulach.adapters.legacy_discovery import LegacyDiscoveryAdapter
from custom_components.saulach.remote_entity_manager import RemoteEntityManager
from custom_components.saulach.scheduler import BridgeScheduler


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    # Every setup schedules a real jittered (0-9s) initial republish; these
    # tests are about service dispatch/wiring, not jitter, so keep them fast.
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda a, b: 0)


def _make_entry(entry_id: str, bridge_name: str, entities: list[str]) -> ConfigEntry:
    return ConfigEntry(
        entry_id=entry_id,
        data={
            CONF_ENTITIES: entities,
            CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant/",
            CONF_SENSOR_VALUE_PREFIX: "share/jakob/",
            CONF_BRIDGE_NAME: bridge_name,
        },
        options={CONF_TIME_PATTERN_MINUTES: 1},
    )


def _run(coro):
    return asyncio.run(coro)


def test_setup_entry_raises_config_entry_not_ready_when_mqtt_not_ready():
    hass = HomeAssistant()
    mqtt._state(hass).client_ready = False
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    with pytest.raises(ConfigEntryNotReady):
        _run(saulach.async_setup_entry(hass, entry))


def test_setup_entry_wires_scheduler_onto_runtime_data():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    result = _run(saulach.async_setup_entry(hass, entry))

    assert result is True
    assert isinstance(entry.runtime_data.scheduler, BridgeScheduler)
    assert isinstance(entry.runtime_data.remote_entity_manager, RemoteEntityManager)
    assert isinstance(entry.runtime_data.protocol_adapter, LegacyDiscoveryAdapter)
    assert entry.runtime_data.integration_version == "0.1.8"


def test_setup_entry_reads_manifest_version_off_the_event_loop(monkeypatch):
    # issue #13: integration_version() does blocking file I/O; it must be
    # fetched via hass.async_add_executor_job, never called directly on
    # the event loop (that crashed/hung every setup, including reload).
    calls: list[object] = []
    real_executor_job = HomeAssistant.async_add_executor_job

    async def recording_executor_job(self, target, *args):
        calls.append(target)
        return await real_executor_job(self, target, *args)

    monkeypatch.setattr(HomeAssistant, "async_add_executor_job", recording_executor_job)

    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    _run(saulach.async_setup_entry(hass, entry))

    assert saulach.integration_version in calls


def test_setup_entry_registers_service_once():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    _run(saulach.async_setup_entry(hass, entry))

    assert hass.services.has_service(DOMAIN, SERVICE_REPUBLISH)


def test_republish_service_dispatches_to_correct_entry(monkeypatch):
    hass = HomeAssistant()
    entry_a = _make_entry("entry_a", "Bridge A", ["sensor.a"])
    entry_b = _make_entry("entry_b", "Bridge B", ["sensor.b"])
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")

    async def scenario():
        await saulach.async_setup_entry(hass, entry_a)
        await saulach.async_setup_entry(hass, entry_b)

        # Both entries schedule an initial resync republish on setup; drain
        # those before exercising the service call so they don't muddy the
        # published-messages assertion below.
        for entry in (entry_a, entry_b):
            pending = list(entry.runtime_data.scheduler._tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        mqtt._state(hass).published.clear()

        await hass.services.async_call(
            DOMAIN, SERVICE_REPUBLISH, {ATTR_CONFIG_ENTRY_ID: "entry_a"}
        )
        pending = list(entry_a.runtime_data.scheduler._tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    _run(scenario())

    published_topics = {topic for topic, _, _ in mqtt._state(hass).published}
    # entry_a's sensor ("sensor.a" -> object_id "a") was republished...
    assert "share/homeassistant/sensor/a/config" in published_topics
    assert "share/jakob/sensor/a" in published_topics
    # ...entry_b's was not, since the service call only targeted entry_a.
    assert "share/homeassistant/sensor/b/config" not in published_topics
    assert "share/jakob/sensor/b" not in published_topics


def test_republish_service_raises_for_unknown_entry_id():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    _run(saulach.async_setup_entry(hass, entry))

    async def scenario():
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, SERVICE_REPUBLISH, {ATTR_CONFIG_ENTRY_ID: "does_not_exist"}
            )

    _run(scenario())


def test_unload_entry_removes_its_republish_handler():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        assert "entry1" in hass.data[DOMAIN]["republish_handlers"]

        await saulach.async_unload_entry(hass, entry)
        await entry.async_unload()

        assert "entry1" not in hass.data[DOMAIN]["republish_handlers"]
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, SERVICE_REPUBLISH, {ATTR_CONFIG_ENTRY_ID: "entry1"}
            )

    _run(scenario())


def test_remove_entry_depublishes_every_bridged_entity():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a", "sensor.b"])

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        # async_unload_entry always runs before async_remove_entry in real
        # HA; the adapter/protocol_adapter on runtime_data must still work
        # for depublishing after that unload.
        await saulach.async_unload_entry(hass, entry)
        await entry.async_unload()

        mqtt._state(hass).published.clear()
        await saulach.async_remove_entry(hass, entry)

    _run(scenario())

    published = {(topic, payload) for topic, payload, _retain in mqtt._state(hass).published}
    assert ("share/homeassistant/sensor/a/config", "") in published
    assert ("share/jakob/sensor/a", "") in published
    assert ("share/homeassistant/sensor/b/config", "") in published
    assert ("share/jakob/sensor/b", "") in published


def test_remove_entry_is_a_noop_when_setup_never_completed():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    # entry.runtime_data is None -- setup never ran (e.g. it failed).

    _run(saulach.async_remove_entry(hass, entry))  # must not raise

    assert mqtt._state(hass).published == []


# --- saulach.depublish_bridge (manual cleanup of a confirmed-dead peer) ---

_REMOTE_PAYLOAD = {
    "name": "Garage Humidity",
    "state_topic": "share/other_bridge/sensor/garage_humidity",
    "unique_id": "other_bridge::sensor.garage_humidity",
    "bridge_id": "other_bridge",
    "protocol_version": 1,
    "device": {"identifiers": ["other_bridge"], "name": "Bridge Other", "sw_version": "1.0.3"},
}


def test_setup_entry_registers_depublish_bridge_service_once():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    _run(saulach.async_setup_entry(hass, entry))

    assert hass.services.has_service(DOMAIN, SERVICE_DEPUBLISH_BRIDGE)


def test_depublish_bridge_service_clears_remote_bridge_topics_and_entity():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await mqtt.async_fire_mqtt_message(
            hass,
            "share/homeassistant/sensor/garage_humidity/config",
            json.dumps(_REMOTE_PAYLOAD),
        )
        manager = entry.runtime_data.remote_entity_manager
        entity_id = manager._entities["other_bridge::sensor.garage_humidity"].entity_id

        device = next(
            device
            for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            if (DOMAIN, "other_bridge") in device.identifiers
        )
        mqtt._state(hass).published.clear()

        await hass.services.async_call(
            DOMAIN, SERVICE_DEPUBLISH_BRIDGE, {ATTR_BRIDGE_DEVICE: device.id}
        )
        return entity_id

    entity_id = _run(scenario())

    assert hass.states.get(entity_id) is None
    published = {(topic, payload, retain) for topic, payload, retain in mqtt._state(hass).published}
    assert ("share/homeassistant/sensor/garage_humidity/config", "", True) in published


def test_depublish_bridge_service_removes_the_device_too():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await mqtt.async_fire_mqtt_message(
            hass,
            "share/homeassistant/sensor/garage_humidity/config",
            json.dumps(_REMOTE_PAYLOAD),
        )
        device = next(
            device
            for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            if (DOMAIN, "other_bridge") in device.identifiers
        )
        await hass.services.async_call(
            DOMAIN, SERVICE_DEPUBLISH_BRIDGE, {ATTR_BRIDGE_DEVICE: device.id}
        )
        return device.id

    device_id = _run(scenario())

    assert dr.async_get(hass).async_get(device_id) is None


def test_depublish_bridge_service_clears_a_bridge_never_rediscovered_this_session():
    # Bug report this covers: an old bridge whose sensor shows
    # "Unavailable" -- it survived a restart via the entity registry, but
    # nothing rediscovered it live this session (its retained discovery
    # message is long gone from the broker). Calling the service on it
    # must still depublish and remove it -- not silently no-op.
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        # Never fires an MQTT message -- simulates an entity that
        # survived purely via registry persistence, unknown to
        # RemoteEntityManager's in-memory bookkeeping this session.
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, "stale_bridge")},
            name="Bridge Test Jakob",
        )
        er.async_get(hass)._register(
            "sensor.stale_bridge_garage_humidity",
            device.id,
            "stale_bridge::sensor.garage_humidity",
        )

        await hass.services.async_call(
            DOMAIN, SERVICE_DEPUBLISH_BRIDGE, {ATTR_BRIDGE_DEVICE: device.id}
        )
        return device.id

    device_id = _run(scenario())

    published = {(topic, payload, retain) for topic, payload, retain in mqtt._state(hass).published}
    assert ("share/homeassistant/sensor/garage_humidity/config", "", True) in published
    assert er.async_get(hass).async_get("sensor.stale_bridge_garage_humidity") is None
    assert dr.async_get(hass).async_get(device_id) is None


def test_depublish_bridge_service_raises_for_unknown_device():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    _run(saulach.async_setup_entry(hass, entry))

    async def scenario():
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, SERVICE_DEPUBLISH_BRIDGE, {ATTR_BRIDGE_DEVICE: "does_not_exist"}
            )

    _run(scenario())


def test_depublish_bridge_service_raises_for_device_without_bridge_identifier():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        # A device owned by this entry but without a (DOMAIN, bridge_id)
        # identifier -- shouldn't happen in practice, but the service must
        # fail closed rather than guess.
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("unrelated_domain", "x")},
            name="Not a bridge",
        )
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, SERVICE_DEPUBLISH_BRIDGE, {ATTR_BRIDGE_DEVICE: device.id}
            )

    _run(scenario())


def test_setup_entry_removes_orphaned_device_with_no_entities():
    # Simulates the #12-follow-up regression: an own-bridge (or remote
    # bridge) device left behind in the registry with zero entities --
    # either because the code stopped creating them, or the user deleted
    # them by hand. HA won't offer a UI way to delete the device itself
    # while the config entry is loaded, so this must happen automatically.
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    orphan = dr.async_get(hass).async_get_or_create(
        config_entry_id="entry1",
        identifiers={(DOMAIN, "bridge_jakob")},
        name="Bridge Jakob",
    )

    _run(saulach.async_setup_entry(hass, entry))

    assert dr.async_get(hass).async_get(orphan.id) is None


def test_setup_entry_keeps_device_that_still_has_entities():
    # The safety property the cleanup relies on: a device with at least
    # one entity is never touched, so an active bridge can never be
    # deleted by this pass regardless of whether it's currently publishing.
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    active = dr.async_get(hass).async_get_or_create(
        config_entry_id="entry1",
        identifiers={(DOMAIN, "some_remote_bridge")},
        name="Some Remote Bridge",
    )
    er.async_get(hass)._register("sensor.some_remote_bridge_temp", active.id)

    _run(saulach.async_setup_entry(hass, entry))

    assert dr.async_get(hass).async_get(active.id) is not None


def test_setup_entry_does_not_touch_orphaned_devices_from_other_entries():
    hass = HomeAssistant()
    entry = _make_entry("entry1", "Bridge Jakob", ["sensor.a"])
    other_entrys_orphan = dr.async_get(hass).async_get_or_create(
        config_entry_id="entry2",
        identifiers={(DOMAIN, "other_bridge")},
        name="Other Bridge",
    )

    _run(saulach.async_setup_entry(hass, entry))

    assert dr.async_get(hass).async_get(other_entrys_orphan.id) is not None
