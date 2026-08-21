"""BridgeScheduler tests against the fake homeassistant in tests/ha_stubs.

Uses a recording ProtocolAdapter double so these tests are about
scheduler.py's own timing/lifecycle logic, not the legacy discovery
protocol (that's covered in test_legacy_discovery_adapter.py).

Jittered publishes run as real background asyncio Tasks (by design --
that's what makes the §6 jitter behavior real). Rather than guessing how
many bare `await asyncio.sleep(0)` ticks are enough to drain them (fragile
and Python-version-sensitive), tests explicitly `asyncio.gather()` the
scheduler's own tracked task set after triggering an action.
"""

import asyncio
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.event import async_fire_time_changed

from custom_components.saulach import scheduler as scheduler_module
from custom_components.saulach.const import CONF_ENTITIES, CONF_TIME_PATTERN_MINUTES
from custom_components.saulach.protocol import ProtocolAdapter
from custom_components.saulach.scheduler import BridgeScheduler


class RecordingAdapter(ProtocolAdapter):
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.published_metadata: list[int] = []

    def topics_to_subscribe(self) -> list[str]:
        return ["fake/+/+/config"]

    async def publish_own_entity(self, entity_id, state) -> None:
        self.published.append((entity_id, state.state))

    async def async_depublish_entity(self, entity_id) -> None:
        pass

    async def handle_incoming_message(self, topic, payload) -> None:
        pass

    async def async_handle_mqtt_message(self, msg) -> None:
        pass

    async def async_publish_metadata(self, entity_count: int) -> dict:
        self.published_metadata.append(entity_count)
        return {"entity_count": entity_count}


def _make_hass_entry(entities: list[str], minutes: int = 1) -> tuple[HomeAssistant, ConfigEntry]:
    hass = HomeAssistant()
    entry = ConfigEntry(
        data={CONF_ENTITIES: entities},
        options={CONF_TIME_PATTERN_MINUTES: minutes},
    )
    return hass, entry


def _no_jitter(monkeypatch) -> None:
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda a, b: 0)


async def _drain(sched: BridgeScheduler) -> None:
    """Wait for every currently-pending jittered publish task to finish."""
    pending = list(sched._tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _run(coro):
    return asyncio.run(coro)


def test_setup_does_initial_republish_for_known_entities(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a", "sensor.b"])
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)

    _run(scenario())

    assert sorted(adapter.published) == [("sensor.a", "1"), ("sensor.b", "2")]


def test_duplicate_entity_id_in_entry_data_is_published_only_once(monkeypatch):
    # Guards against a config entry written by a version predating
    # config_flow.py's own dedup guard: a duplicate entity_id in
    # entry.data[CONF_ENTITIES] previously made every republish tick
    # publish the same retained topic twice.
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a", "sensor.a"])
    hass.states.async_set("sensor.a", "1")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)

    _run(scenario())

    assert adapter.published == [("sensor.a", "1")]


def test_setup_skips_entities_without_state(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a", "sensor.missing"])
    hass.states.async_set("sensor.a", "1")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)

    _run(scenario())

    assert adapter.published == [("sensor.a", "1")]


def test_state_change_event_publishes_single_entity(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a"])
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)  # nothing to publish yet -- sensor.a has no state
        adapter.published.clear()

        new_state = State("sensor.a", "42")
        hass.bus.async_fire(
            "state_changed",
            {"entity_id": "sensor.a", "new_state": new_state, "old_state": None},
        )
        await _drain(sched)

    _run(scenario())

    assert adapter.published == [("sensor.a", "42")]


def test_state_change_ignores_entity_removal(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a"])
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)
        adapter.published.clear()

        hass.bus.async_fire(
            "state_changed",
            {"entity_id": "sensor.a", "new_state": None, "old_state": None},
        )
        await _drain(sched)

    _run(scenario())

    assert adapter.published == []


def test_state_change_ignores_untracked_entity(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a"])
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)
        adapter.published.clear()

        hass.bus.async_fire(
            "state_changed",
            {
                "entity_id": "sensor.not_bridged",
                "new_state": State("sensor.not_bridged", "x"),
                "old_state": None,
            },
        )
        await _drain(sched)

    _run(scenario())

    assert adapter.published == []


def test_clock_tick_only_republishes_on_matching_minute(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a"], minutes=5)
    hass.states.async_set("sensor.a", "x")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)  # drain the initial resync-on-setup republish
        adapter.published.clear()

        async_fire_time_changed(hass, datetime(2026, 1, 1, 12, 3, 0))
        await _drain(sched)
        assert adapter.published == []

        async_fire_time_changed(hass, datetime(2026, 1, 1, 12, 5, 0))
        await _drain(sched)
        assert adapter.published == [("sensor.a", "x")]

    _run(scenario())


def test_republish_all_publishes_metadata_with_current_entity_count(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a", "sensor.b"])
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)

    _run(scenario())

    assert adapter.published_metadata == [2]


def test_clock_tick_republishes_metadata_alongside_entities(monkeypatch):
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a"], minutes=1)
    hass.states.async_set("sensor.a", "x")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)
        adapter.published_metadata.clear()

        async_fire_time_changed(hass, datetime(2026, 1, 1, 12, 1, 0))
        await _drain(sched)

    _run(scenario())

    assert adapter.published_metadata == [1]


def test_last_metadata_is_recorded_after_publish(monkeypatch):
    # last_metadata is kept for diagnostics.py's "Download Diagnostics"
    # only -- own metadata is no longer surfaced as local entities (issue
    # #12 follow-up, reverted per user feedback).
    _no_jitter(monkeypatch)
    hass, entry = _make_hass_entry(["sensor.a"])
    hass.states.async_set("sensor.a", "x")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)

    _run(scenario())

    assert sched.last_metadata == {"entity_count": 1}


def test_republish_is_jittered_within_documented_bounds(monkeypatch):
    calls: list[tuple[float, float]] = []
    monkeypatch.setattr(
        scheduler_module.random, "uniform", lambda a, b: calls.append((a, b)) or 0
    )
    hass, entry = _make_hass_entry(["sensor.a"])
    hass.states.async_set("sensor.a", "x")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        await _drain(sched)

    _run(scenario())

    # One entity publish + one metadata publish, each independently
    # jittered within the same documented bounds.
    assert calls == [(0, scheduler_module.JITTER_MAX_SECONDS)] * 2


def test_unload_cancels_pending_jittered_tasks_and_unsubscribes():
    hass, entry = _make_hass_entry(["sensor.a"])
    hass.states.async_set("sensor.a", "x")
    adapter = RecordingAdapter()
    sched = BridgeScheduler(hass, entry, adapter)

    async def scenario():
        await sched.async_setup()
        # Real (non-zero) jitter means the initial-republish task hasn't
        # run yet -- it's still pending when we unload.
        pending = list(sched._tasks)
        assert pending

        await entry.async_unload()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in pending:
            assert task.cancelled()
        assert not sched._tasks
        assert not hass.bus._listeners.get("state_changed")
        assert not getattr(hass, "_time_change_listeners", [])
        assert adapter.published == []  # cancelled before it could publish

    _run(scenario())
