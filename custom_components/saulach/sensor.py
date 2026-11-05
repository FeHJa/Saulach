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
from homeassistant.const import EntityCategory, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PROTOCOL_VERSION


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entry.runtime_data.remote_entity_manager.set_add_entities_callback(async_add_entities)


class BridgedSensorEntity(SensorEntity):
    """Deliberately doesn't set device_info["sw_version"] from the incoming
    payload's device.sw_version -- that field is always the wire-protocol's
    fixed legacy constant (PROTOCOL.md §3's SW_VERSION, "1.0.3", inherited
    from the original blueprint automation), not this bridge's actual
    release version, and setting it here would fight BridgeMetadataEntities
    for the same device's sw_version field: ordinary discovery messages
    fire far more often (every state change) than metadata (once per
    time_pattern tick), so the meaningless "1.0.3" kept winning and hiding
    the real version. Purely a receiving-side display choice -- the
    outgoing wire payload (discovery.py) is unchanged.

    device_info["model"] is set to bridge_id, not the human-chosen
    bridge_name -- two peers can easily end up with the same bridge_name
    (e.g. both left it at its default), but bridge_id is, in practice,
    always unique (loop prevention, PROTOCOL.md §5, depends on it). Model
    is secondary/detail info, unlike name, so this doesn't change what a
    device is called anywhere it's listed -- only what's shown when you
    look closer (e.g. the saulach.depublish_bridge device picker)."""

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
        bridge_id: str | None,
    ) -> None:
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_info = {
            "identifiers": device_identifiers,
            "name": device_name,
            "model": bridge_id,
        }

    def set_native_value(self, value: str) -> None:
        # issue #27: a bridged entity's own source can go unavailable on
        # the *other* end -- the raw MQTT payload is then the literal
        # string "unavailable" (or "unknown"), not a real reading. Writing
        # that straight into native_value crashed HA core's numeric
        # coercion for any sensor with a numeric device_class (temperature,
        # humidity, ...): a non-None string value on such a sensor is
        # always assumed to be a real number. HA's own convention is to
        # signal "no value" via native_value=None and "not available" via
        # the `available` property, never via a literal sentinel string --
        # translate the wire's sentinel into that before writing state.
        if value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_native_value = None
            self._attr_available = value != STATE_UNAVAILABLE
        else:
            self._attr_native_value = value
            self._attr_available = True
        self.async_write_ha_state()

    def update_from_discovery(
        self,
        *,
        name: str | None,
        device_class: str | None,
        unit_of_measurement: str | None,
        device_identifiers: set[tuple[str, str]],
        device_name: str | None,
        bridge_id: str | None,
    ) -> None:
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_info = {
            "identifiers": device_identifiers,
            "name": device_name,
            "model": bridge_id,
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
            "model": slug_bridge_name,
            "sw_version": f"{integration_version} (protocol v{protocol_version})",
        }
        self._device_info = device_info
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
        # Bug: this used to only refresh the three visible sensor values.
        # device_info["sw_version"] is set once, in __init__, the first
        # time this bridge's metadata is ever seen -- every later
        # redelivery (e.g. the peer actually upgrading) went through this
        # method instead of __init__, so the displayed firmware stayed
        # frozen at whatever it was the first time, forever, regardless
        # of how many times the peer actually upgraded. All three
        # diagnostic entities share this one device_info dict by
        # reference (see __init__), so mutating it here updates all of
        # them; set_native_value's async_write_ha_state() below is what
        # actually pushes the refreshed value to the device registry.
        integration_version = metadata.get("integration_version", "unknown")
        protocol_version = metadata.get("protocol_version", PROTOCOL_VERSION)
        self._device_info["sw_version"] = f"{integration_version} (protocol v{protocol_version})"
        self.entity_count.set_native_value(str(metadata["entity_count"]))
        self.last_heartbeat.set_native_value(metadata["last_heartbeat"])
        self.ha_version.set_native_value(metadata["ha_version"])
