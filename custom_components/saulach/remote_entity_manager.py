"""Materializes federated (remote-bridge) discovery messages as native
Home Assistant entities — see PROTOCOL.md §5a.

Owns the per-entity MQTT state-topic subscription directly, rather than
via entity lifecycle hooks (`async_added_to_hass`/`async_will_remove_from_hass`),
so entity creation and its state feed are wired up atomically and don't
depend on entity-platform timing.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import mqtt_io
from .const import CONF_SHARED_DISCOVERY_PREFIX, DOMAIN, PROTOCOL_VERSION
from .discovery import domain_from_unique_id, object_id_from_entity_id
from .sensor import BridgedSensorEntity, BridgeMetadataEntities

_LOGGER = logging.getLogger(__name__)


class RemoteEntityManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._entities: dict[str, BridgedSensorEntity] = {}
        self._state_unsubs: dict[str, callable] = {}
        self._topic_to_unique_id: dict[str, str] = {}
        self._add_entities_callback: AddEntitiesCallback | None = None
        # Remote-bridge metadata (§9, issue #12 follow-up): a bridge's
        # diagnostic entities are only ever shown if we already have at
        # least one entity materialized from them -- these three dicts
        # track that membership so the metadata entities can be created
        # lazily (whenever metadata first arrives for a known bridge) and
        # torn down again once that bridge has no entities left.
        self._entity_bridge_id: dict[str, str] = {}
        self._bridge_entity_counts: dict[str, int] = {}
        self._bridge_names: dict[str, str] = {}
        self._remote_metadata_entities: dict[str, BridgeMetadataEntities] = {}

    def set_add_entities_callback(self, callback: AddEntitiesCallback) -> None:
        self._add_entities_callback = callback

    async def async_handle_discovery(self, topic: str, payload_data: dict) -> None:
        unique_id = payload_data.get("unique_id")
        state_topic = payload_data.get("state_topic")
        if not unique_id or not state_topic:
            _LOGGER.debug(
                "Ignoring federated discovery payload missing unique_id/state_topic"
            )
            return

        name = payload_data.get("name")
        device_class = payload_data.get("device_class")
        if domain_from_unique_id(unique_id) != "sensor":
            # issue #13 continued: don't blindly trust a peer's
            # device_class -- it only means what it says if it actually
            # came from a sensor-domain entity (see discovery.py's
            # domain_from_unique_id). We materialize a native SensorEntity
            # regardless of the source's real domain (§2's hardcoded
            # "sensor" component), so a mismatched value here crashes the
            # same way it did on the sending side, just locally instead.
            device_class = None
        unit_of_measurement = payload_data.get("unit_of_measurement")
        device = payload_data.get("device") or {}
        device_identifiers = {(DOMAIN, ident) for ident in device.get("identifiers", [])}
        device_name = device.get("name")
        bridge_id = next(iter(device.get("identifiers", [])), None)

        existing = self._entities.get(unique_id)
        if existing is not None:
            existing.update_from_discovery(
                name=name,
                device_class=device_class,
                unit_of_measurement=unit_of_measurement,
                device_identifiers=device_identifiers,
                device_name=device_name,
            )
            self._topic_to_unique_id[topic] = unique_id
            if bridge_id is not None:
                self._bridge_names[bridge_id] = device_name
            return

        entity = BridgedSensorEntity(
            unique_id=unique_id,
            name=name,
            device_class=device_class,
            unit_of_measurement=unit_of_measurement,
            device_identifiers=device_identifiers,
            device_name=device_name,
        )

        if self._add_entities_callback is None:
            _LOGGER.warning(
                "Discovered federated entity %s before the sensor platform was "
                "ready; dropping",
                unique_id,
            )
            return

        self._entities[unique_id] = entity
        self._topic_to_unique_id[topic] = unique_id
        if bridge_id is not None:
            self._entity_bridge_id[unique_id] = bridge_id
            self._bridge_entity_counts[bridge_id] = self._bridge_entity_counts.get(bridge_id, 0) + 1
            self._bridge_names[bridge_id] = device_name
        self._state_unsubs[unique_id] = await mqtt_io.async_subscribe(
            self._hass, state_topic, self._make_state_handler(unique_id)
        )
        self._add_entities_callback([entity])
        # Discovery alone doesn't carry a state value (§3/§4 are separate
        # messages) -- write once now so the entity is visible immediately
        # rather than absent from hass.states until its first state_topic
        # message arrives.
        entity.async_write_ha_state()

    async def async_handle_removal(self, topic: str) -> None:
        """An empty retained payload arrived on `topic` (issue #7 / §5's
        removal convention). Remove whatever entity we last associated
        with this exact topic, if any -- an empty payload carries no
        unique_id of its own, so topic is the only correlation we have."""
        unique_id = self._topic_to_unique_id.pop(topic, None)
        if unique_id is None:
            _LOGGER.debug("Ignoring removal on topic we never discovered anything from: %s", topic)
            return

        unsub = self._state_unsubs.pop(unique_id, None)
        if unsub is not None:
            unsub()

        entity = self._entities.pop(unique_id, None)
        if entity is None:
            return

        await self._async_remove_entity(entity)

        bridge_id = self._entity_bridge_id.pop(unique_id, None)
        if bridge_id is None:
            return
        remaining = self._bridge_entity_counts.get(bridge_id, 0) - 1
        if remaining > 0:
            self._bridge_entity_counts[bridge_id] = remaining
            return
        # That was the last entity from this bridge -- its diagnostic
        # entities (if any were ever created) no longer have anything to
        # attach to (issue #12 follow-up).
        self._bridge_entity_counts.pop(bridge_id, None)
        self._bridge_names.pop(bridge_id, None)
        metadata_entities = self._remote_metadata_entities.pop(bridge_id, None)
        if metadata_entities is not None:
            for metadata_entity in metadata_entities.entities:
                await self._async_remove_entity(metadata_entity)

    async def async_depublish_bridge(self, bridge_id: str, device_id: str) -> int:
        """saulach.depublish_bridge service: a human has decided
        `bridge_id` is dead (e.g. a decommissioned peer whose empty
        retained payload never reached us while we were online to see it,
        so it keeps reappearing on every restart -- MQTT retains forever
        otherwise). Driven by the *entity registry* for `device_id`, not
        our own in-memory bookkeeping -- that bookkeeping only knows
        about entities re-discovered this session, but the entities this
        service exists for are exactly the ones that show "Unavailable"
        because nothing has re-discovered them since a previous session
        (their retained discovery message is long gone). Publishes an
        empty retained payload to every real bridged entity's own
        discovery topic -- reconstructed from its unique_id
        ("{bridge_id}::{entity_id}", §3), the durable fix since the
        broker won't redeliver a cleared topic on our next restart -- and
        tears each one down immediately: through the normal
        async_handle_removal path if it's live this session, or directly
        from the registry otherwise (nothing live to call .async_remove()
        on). Diagnostic entities (§9) have no discovery topic of their
        own; anything still left on the device afterwards -- diagnostics
        that were never live this session either -- is swept up directly
        too. Returns how many discovery topics were published to."""
        entity_registry = er.async_get(self._hass)
        shared_discovery_prefix = self._entry.data[CONF_SHARED_DISCOVERY_PREFIX]
        device_entities = list(
            er.async_entries_for_device(entity_registry, device_id, include_disabled_entities=True)
        )

        published = 0
        for reg_entry in device_entities:
            unique_id = reg_entry.unique_id
            _, _, suffix = (unique_id or "").partition("::")
            if "." not in suffix:
                continue  # diagnostic entity -- no topic of its own, swept up below
            object_id = object_id_from_entity_id(suffix)
            topic = f"{shared_discovery_prefix}sensor/{object_id}/config"
            await mqtt_io.async_publish(self._hass, topic, "", retain=True)
            published += 1
            if unique_id in self._entities:
                await self.async_handle_removal(topic)

        for reg_entry in device_entities:
            if entity_registry.async_get(reg_entry.entity_id) is not None:
                entity_registry.async_remove(reg_entry.entity_id)

        self._remote_metadata_entities.pop(bridge_id, None)
        self._bridge_entity_counts.pop(bridge_id, None)
        self._bridge_names.pop(bridge_id, None)
        return published

    async def async_handle_remote_metadata(self, bridge_id: str, payload_data: dict) -> None:
        """A metadata message (§9, issue #12) arrived for `bridge_id`.
        Only shown if we already materialized at least one entity from
        that bridge -- otherwise there's no device to attach it to, and
        no reason to create one from metadata alone."""
        if bridge_id not in self._bridge_entity_counts:
            _LOGGER.debug("Ignoring metadata for unknown bridge %s", bridge_id)
            return

        metadata_entities = self._remote_metadata_entities.get(bridge_id)
        if metadata_entities is None:
            if self._add_entities_callback is None:
                return
            metadata_entities = BridgeMetadataEntities(
                bridge_name=self._bridge_names.get(bridge_id, bridge_id),
                slug_bridge_name=bridge_id,
                integration_version=payload_data.get("integration_version", "unknown"),
                protocol_version=payload_data.get("protocol_version", PROTOCOL_VERSION),
            )
            self._remote_metadata_entities[bridge_id] = metadata_entities
            self._add_entities_callback(metadata_entities.entities)

        metadata_entities.update(payload_data)

    async def _async_remove_entity(self, entity) -> None:
        entity_id = entity.entity_id
        await entity.async_remove()
        if entity_id is not None:
            registry = er.async_get(self._hass)
            if registry.async_get(entity_id) is not None:
                registry.async_remove(entity_id)

    def _make_state_handler(self, unique_id: str):
        async def _handle_state_message(msg) -> None:
            entity = self._entities.get(unique_id)
            if entity is not None:
                entity.set_native_value(msg.payload)

        return _handle_state_message

    async def async_unload(self) -> None:
        for unsub in self._state_unsubs.values():
            unsub()
        self._state_unsubs.clear()
        self._entities.clear()
        self._topic_to_unique_id.clear()
        self._entity_bridge_id.clear()
        self._bridge_entity_counts.clear()
        self._bridge_names.clear()
        self._remote_metadata_entities.clear()
