"""Protocol adapter seam (PROTOCOL.md §8 / MIGRATION_PLAN.md).

Phase 1 has exactly one implementation, LegacyDiscoveryAdapter. This
interface exists so a future manifest-based adapter (Phase 3, gated on
cross-instance coordination, not implemented here) is additive rather
than a rewrite of scheduler.py / __init__.py's wiring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import State


class ProtocolAdapter(ABC):
    """A wire-protocol generation: how this bridge speaks to its peers."""

    @abstractmethod
    def topics_to_subscribe(self) -> list[str]:
        """MQTT topic filters this adapter needs subscribed for incoming
        federation messages."""

    @abstractmethod
    async def publish_own_entity(self, entity_id: str, state: State) -> None:
        """Publish this bridge's own discovery + state for one entity."""

    @abstractmethod
    async def async_depublish_entity(self, entity_id: str) -> None:
        """Retract a previously-published own entity (empty retained
        payload) -- used when an entity is dropped from the bridged list
        (issue #7) or the whole config entry is removed. A future
        manifest-based adapter would implement this as removing the entry
        from its manifest and republishing, rather than an empty payload,
        but the intent -- "this entity is no longer ours" -- is the same."""

    @abstractmethod
    async def handle_incoming_message(self, topic: str, payload: str) -> None:
        """Handle one incoming message on a subscribed topic, per this
        adapter's protocol (e.g. LegacyDiscoveryAdapter materializes it as
        a native entity via RemoteEntityManager, §5a)."""

    @abstractmethod
    async def async_publish_metadata(self, entity_count: int) -> dict:
        """Publish this bridge's own metadata -- protocol version,
        integration version, HA version, bridged entity count, and a
        last-heartbeat timestamp -- to a dedicated topic (PROTOCOL.md §9,
        issue #12). Returns the published payload dict so callers can
        mirror the same numbers into local diagnostic entities without
        recomputing them and risking drift."""
