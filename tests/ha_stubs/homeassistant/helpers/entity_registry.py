from __future__ import annotations

from typing import Any


class RegistryEntry:
    def __init__(
        self, entity_id: str, device_id: str | None = None, unique_id: str | None = None
    ) -> None:
        self.entity_id = entity_id
        self.device_id = device_id
        self.unique_id = unique_id


class EntityRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}

    def async_get(self, entity_id: str) -> RegistryEntry | None:
        return self._entries.get(entity_id)

    def async_remove(self, entity_id: str) -> None:
        self._entries.pop(entity_id, None)

    def _register(
        self, entity_id: str, device_id: str | None = None, unique_id: str | None = None
    ) -> None:
        self._entries[entity_id] = RegistryEntry(entity_id, device_id, unique_id)


def async_get(hass: Any) -> EntityRegistry:
    return hass.data.setdefault("_fake_entity_registry", EntityRegistry())


def async_entries_for_device(
    registry: EntityRegistry, device_id: str, include_disabled_entities: bool = False
) -> list[RegistryEntry]:
    return [entry for entry in registry._entries.values() if entry.device_id == device_id]
