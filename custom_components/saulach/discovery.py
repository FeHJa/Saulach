"""Pure wire-protocol logic (PROTOCOL.md §1-§5), no Home Assistant imports.

Kept framework-free on purpose so the exact behavior ported from the
blueprint — regex table, payload shape, topic parsing, loop guard — is
unit-testable without a running HA instance or MQTT broker.
"""

from __future__ import annotations

import re

from .const import DEVICE_CLASS_UNIT_PATTERNS, PROTOCOL_VERSION, SW_VERSION

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9_]")


def slugify_bridge_name(bridge_name: str) -> str:
    """PROTOCOL.md §1: lowercase, spaces -> '_', strip anything not [a-z0-9_]."""
    slug = bridge_name.lower().replace(" ", "_")
    return _SLUG_INVALID_CHARS.sub("", slug)


def object_id_from_entity_id(entity_id: str) -> str:
    """PROTOCOL.md §2: entity_id.split('.')[-1] (domain stripped)."""
    return entity_id.split(".")[-1]


def domain_from_entity_id(entity_id: str) -> str:
    """entity_id.split('.')[0] -- the source entity's real HA domain
    (e.g. "binary_sensor"), independent of the "sensor" component this
    bridge always publishes as (§2's known limitation)."""
    return entity_id.split(".")[0]


def normalize_prefix(prefix: str) -> str:
    return prefix if prefix.endswith("/") else f"{prefix}/"


def domain_from_unique_id(unique_id: str) -> str | None:
    """Recovers the source entity's real HA domain from our own unique_id
    convention (`{slug_bridge_name}::{entity_id}`, §3) -- lets a
    *receiving* Saulach instance apply the same device_class safety
    check as the sending side (issue #13 continued) without trusting an
    incoming payload's device_class on its own. Every Saulach sender
    follows this convention, but nothing enforces it on the wire, so
    returns None -- "unknown, treat as unsafe" -- for anything that
    doesn't match it, rather than guessing."""
    if "::" not in unique_id:
        return None
    _, entity_id = unique_id.split("::", 1)
    return domain_from_entity_id(entity_id)


def _title_case_object_id(object_id: str) -> str:
    return object_id.replace("_", " ").title()


def resolve_device_class_and_unit(
    object_id: str,
    device_class: str | None,
    unit_of_measurement: str | None,
) -> tuple[str | None, str | None]:
    """PROTOCOL.md §3 resolution order, applied independently per field.

    Entity-provided values win; a missing field is filled in from the first
    regex pattern (of the 8) whose word appears in object_id. Neither
    field is ever set to an explicit null — a still-unresolved field stays
    None, which callers must omit from the payload entirely.
    """
    if device_class is None or unit_of_measurement is None:
        for pattern, pattern_device_class, pattern_unit in DEVICE_CLASS_UNIT_PATTERNS:
            if pattern.search(object_id):
                if device_class is None:
                    device_class = pattern_device_class
                if unit_of_measurement is None:
                    unit_of_measurement = pattern_unit
                break

    return device_class, unit_of_measurement


def build_discovery_payload(
    *,
    entity_id: str,
    friendly_name: str | None,
    device_class: str | None,
    unit_of_measurement: str | None,
    bridge_name: str,
    slug_bridge_name: str,
    sensor_value_prefix: str,
) -> dict:
    """PROTOCOL.md §3: own-entity discovery payload, shared-prefix bound."""
    object_id = object_id_from_entity_id(entity_id)
    name = friendly_name or _title_case_object_id(object_id)
    resolved_device_class, resolved_unit = resolve_device_class_and_unit(
        object_id, device_class, unit_of_measurement
    )

    payload: dict = {
        "name": name,
        "state_topic": f"{sensor_value_prefix}sensor/{object_id}",
        "unique_id": f"{slug_bridge_name}::{entity_id}",
    }
    if resolved_device_class is not None:
        payload["device_class"] = resolved_device_class
    if resolved_unit is not None:
        payload["unit_of_measurement"] = resolved_unit
    payload["bridge_id"] = slug_bridge_name
    payload["protocol_version"] = PROTOCOL_VERSION
    payload["device"] = {
        "identifiers": [slug_bridge_name],
        "name": bridge_name,
        "sw_version": SW_VERSION,
    }
    return payload


def build_metadata_payload(
    *,
    slug_bridge_name: str,
    integration_version: str,
    ha_version: str,
    entity_count: int,
    last_heartbeat: str,
) -> dict:
    """PROTOCOL.md §9: own-bridge metadata payload (issue #12) -- a
    separate, additive side-channel from the §3 discovery payload, not a
    replacement for it."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "integration_version": integration_version,
        "bridge_id": slug_bridge_name,
        "ha_version": ha_version,
        "entity_count": entity_count,
        "last_heartbeat": last_heartbeat,
    }


def parse_metadata_topic(topic: str, shared_discovery_prefix: str) -> str | None:
    """PROTOCOL.md §9: {prefix}bridge/{slug_bridge_name}/metadata. Returns
    the slug_bridge_name if the topic matches this shape, else None --
    used to route incoming metadata messages separately from §2 discovery
    messages (issue #12 follow-up: showing other bridges' metadata)."""
    if not topic.startswith(shared_discovery_prefix):
        return None
    remainder = topic[len(shared_discovery_prefix) :]
    parts = remainder.split("/")
    if len(parts) != 3 or parts[0] != "bridge" or parts[2] != "metadata":
        return None
    return parts[1]


def parse_federation_topic(topic: str, shared_discovery_prefix: str) -> tuple[str, str] | None:
    """PROTOCOL.md §2/§5: component/object_id parsed positionally, right
    after the shared prefix. Returns None if topic doesn't match the
    expected shape (defensive; the broker shouldn't deliver anything else
    to a `{prefix}+/+/config` subscription, but don't assume it)."""
    if not topic.startswith(shared_discovery_prefix):
        return None
    remainder = topic[len(shared_discovery_prefix) :]
    parts = remainder.split("/")
    if len(parts) < 3:
        return None
    return parts[0], parts[1]


def is_own_message(payload_data: dict, slug_bridge_name: str) -> bool:
    """PROTOCOL.md §5 loop guard — preserve exactly. Other instances rely
    on this bridge_id/unique_id prefix convention to avoid re-forwarding
    your own messages back to you; this instance relies on it too, to
    avoid materializing its own echoed messages as entities (§5a)."""
    if payload_data.get("bridge_id") == slug_bridge_name:
        return True
    unique_id = payload_data.get("unique_id") or ""
    return unique_id.startswith(f"{slug_bridge_name}::") or unique_id.startswith(f"{slug_bridge_name}.")
