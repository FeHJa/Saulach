"""Owns timing: state-change publishes, the time_pattern trigger, on-demand
republish, and publish jitter (PROTOCOL.md §6).

Talks to MQTT and builds payloads only through the active ProtocolAdapter,
so swapping the adapter later (Phase 3) doesn't require touching this
class. Deliberately not called "coordinator.py" / built on
DataUpdateCoordinator — nothing here pulls data into entities, it's pure
publish-side timing.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change

from . import mqtt_io
from .const import CONF_ENTITIES, CONF_TIME_PATTERN_MINUTES, JITTER_MAX_SECONDS
from .protocol import ProtocolAdapter

_LOGGER = logging.getLogger(__name__)


class BridgeScheduler:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, adapter: ProtocolAdapter) -> None:
        self._hass = hass
        self._entry = entry
        self._adapter = adapter
        # dict.fromkeys dedupes while preserving order -- entries written
        # by a version predating config_flow.py's own dedup guard could
        # already carry a duplicate entity_id, which would otherwise make
        # async_republish_all publish the same retained topic twice per
        # tick, every tick.
        self._entities: list[str] = list(dict.fromkeys(entry.data[CONF_ENTITIES]))
        self._minutes: int = entry.options.get(CONF_TIME_PATTERN_MINUTES, 1)
        self._tasks: set[asyncio.Task] = set()
        # Last metadata payload this bridge published (§9) -- kept for
        # diagnostics.py's "Download Diagnostics". Not surfaced as local
        # entities; own metadata is wire-only (see PROTOCOL.md §9).
        self.last_metadata: dict | None = None

    async def async_setup(self) -> None:
        hass = self._hass
        entry = self._entry

        entry.async_on_unload(
            async_track_state_change_event(hass, self._entities, self._handle_state_change)
        )
        entry.async_on_unload(async_track_time_change(hass, self._handle_clock_tick, second=0))

        for topic in self._adapter.topics_to_subscribe():
            unsub = await mqtt_io.async_subscribe(hass, topic, self._adapter.async_handle_mqtt_message)
            entry.async_on_unload(unsub)

        entry.async_on_unload(self._cancel_pending_tasks)

        # Resync retained messages right after (re)start, before the first
        # time_pattern tick — same role as the blueprint's startup behavior.
        self.async_republish_all()

    def async_republish_all(self) -> None:
        for entity_id in self._entities:
            state = self._hass.states.get(entity_id)
            if state is not None:
                self._schedule_publish(entity_id, state)
        self._schedule_metadata_publish()

    @callback
    def _handle_state_change(self, event: Event) -> None:
        new_state = event.data["new_state"]
        if new_state is None:
            return
        self._schedule_publish(new_state.entity_id, new_state)

    @callback
    def _handle_clock_tick(self, now: datetime) -> None:
        # Clock-aligned equivalent of the blueprint's `time_pattern:
        # minutes: '/N'` trigger — fires every minute, only acts on the
        # Nth. Wall-clock anchoring (not setup time) is what makes the §6
        # jitter rationale ("instances collide on the same minute mark")
        # hold across independently-started instances.
        if now.minute % self._minutes == 0:
            self.async_republish_all()

    def _schedule_publish(self, entity_id: str, state: State) -> None:
        task = self._hass.async_create_background_task(
            self._jittered_publish(entity_id, state),
            name=f"saulach publish {entity_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _jittered_publish(self, entity_id: str, state: State) -> None:
        await asyncio.sleep(random.uniform(0, JITTER_MAX_SECONDS))
        try:
            await self._adapter.publish_own_entity(entity_id, state)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to publish bridged entity %s", entity_id)

    def _schedule_metadata_publish(self) -> None:
        task = self._hass.async_create_background_task(
            self._jittered_metadata_publish(),
            name="saulach publish metadata",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _jittered_metadata_publish(self) -> None:
        await asyncio.sleep(random.uniform(0, JITTER_MAX_SECONDS))
        try:
            metadata = await self._adapter.async_publish_metadata(len(self._entities))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to publish bridge metadata")
            return
        self.last_metadata = metadata

    @callback
    def _cancel_pending_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()
