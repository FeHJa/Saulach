"""Native sensor entity platform.

Federated (remote-bridge) entities (PROTOCOL.md §5a) -- platform setup
just hands its async_add_entities callback to the config entry's
RemoteEntityManager; entity creation itself happens there, driven by
incoming federation messages, not a static list. RemoteEntityManager also
uses BridgeMetadataEntities (below) for each remote bridge's diagnostic
entities (§9, issue #12 follow-up) -- this bridge's *own* metadata is only
published to the wire, not surfaced locally (deliberately reverted, see
PROTOCOL.md §9's "Local surfacing" note).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entry.runtime_data.remote_entity_manager.set_add_entities_callback(async_add_entities)


class BridgedSensorEntity(SensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        *,
        unique_id: str,
        name: str | None,
        device_class: str | None,
        unit_of_measurement: str | None,
        device_identifiers: set[tuple[str, str]],
        device_name: str | None,
        device_sw_version: str | None,
    ) -> None:
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_info = {
            "identifiers": device_identifiers,
            "name": device_name,
            "sw_version": device_sw_version,
        }

    def set_native_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()

    def update_from_discovery(
        self,
        *,
        name: str | None,
        device_class: str | None,
        unit_of_measurement: str | None,
        device_identifiers: set[tuple[str, str]],
        device_name: str | None,
        device_sw_version: str | None,
    ) -> None:
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_info = {
            "identifiers": device_identifiers,
            "name": device_name,
            "sw_version": device_sw_version,
        }
        self.async_write_ha_state()


class _BridgeDiagnosticSensor(SensorEntity):
    """One field of this bridge's own metadata (PROTOCOL.md §9), shown as
    a plain-text diagnostic entity. Deliberately no device_class -- values
    like last_heartbeat are ISO8601 strings straight off the wire, not
    Python datetimes, and forcing e.g. device_class=timestamp without a
    real datetime object risks HA rejecting the state."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, *, unique_id: str, name: str, device_info: dict) -> None:
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_device_info = device_info

    def set_native_value(self, value: str) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class BridgeMetadataEntities:
    """The fixed set of diagnostic entities for one remote bridge's device
    (§9, issue #12 follow-up) -- entity count, last heartbeat, HA version.
    Created by RemoteEntityManager the first time that bridge's metadata
    is seen. This bridge's own metadata is deliberately not surfaced this
    way (see PROTOCOL.md §9) -- only published to the wire."""

    def __init__(
        self,
        *,
        bridge_name: str,
        slug_bridge_name: str,
        integration_version: str,
        protocol_version: int,
    ) -> None:
        # protocol_version is a parameter, not the module constant --
        # this same class is used for remote bridges too (issue #12
        # follow-up), whose protocol_version may differ from ours.
        device_info = {
            "identifiers": {(DOMAIN, slug_bridge_name)},
            "name": bridge_name,
            "sw_version": f"{integration_version} (protocol v{protocol_version})",
        }
        self.entity_count = _BridgeDiagnosticSensor(
            unique_id=f"{slug_bridge_name}::entity_count",
            name="Bridged entity count",
            device_info=device_info,
        )
        self.last_heartbeat = _BridgeDiagnosticSensor(
            unique_id=f"{slug_bridge_name}::last_heartbeat",
            name="Last heartbeat",
            device_info=device_info,
        )
        self.ha_version = _BridgeDiagnosticSensor(
            unique_id=f"{slug_bridge_name}::ha_version",
            name="Home Assistant version",
            device_info=device_info,
        )
        self.entities = [self.entity_count, self.last_heartbeat, self.ha_version]

    def update(self, metadata: dict) -> None:
        self.entity_count.set_native_value(str(metadata["entity_count"]))
        self.last_heartbeat.set_native_value(metadata["last_heartbeat"])
        self.ha_version.set_native_value(metadata["ha_version"])
