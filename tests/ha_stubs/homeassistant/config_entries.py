from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any

from .data_entry_flow import AbortFlow

ConfigFlowResult = dict


class ConfigEntry:
    def __init__(
        self,
        *,
        data: dict | None = None,
        options: dict | None = None,
        entry_id: str = "test_entry",
        unique_id: str | None = None,
        title: str = "",
    ) -> None:
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.unique_id = unique_id
        self.title = title
        self.runtime_data: Any = None
        self._on_unload: list = []
        self._update_listeners: list[Callable] = []

    def async_on_unload(self, func) -> None:
        self._on_unload.append(func)

    def add_update_listener(self, listener: Callable) -> Callable[[], None]:
        self._update_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)

        return _unsub

    async def async_unload(self) -> None:
        """Test helper: run registered on_unload callbacks, most-recently
        registered first (matches real HA's teardown order), then clear.

        Mirrors a real-HA quirk that bit us once already (issue #13's
        "Failed to Unload" bug): a *truthy* return value from an on_unload
        callback is treated by real HA as a job to schedule as a task, so
        a callback that accidentally returns something non-None and
        non-awaitable (e.g. because it returns a dict.pop() result) blows
        up there. Raising the same way here means a repeat of that
        mistake fails a test instead of only ever showing up in a user's
        log."""
        for func in reversed(self._on_unload):
            result = func()
            if result:
                if not hasattr(result, "__await__"):
                    raise TypeError(f"a coroutine was expected, got {result!r}")
                await result
        self._on_unload.clear()


class ConfigEntriesRegistry:
    """Test double for hass.config_entries — the unique_id collision check
    config_flow.py relies on, entity-platform forward/unload so
    custom_components.saulach.sensor's async_setup_entry can be driven
    the same way real HA drives it, and update/reload plumbing so
    options-flow changes can be tested end-to-end across a real reload
    (issue #7)."""

    def __init__(self, hass: Any = None) -> None:
        self._hass = hass
        self.entries: list[ConfigEntry] = []
        self._platform_entities: dict[tuple[str, str], list[Any]] = {}

    def async_entries(self, domain: str | None = None) -> list[ConfigEntry]:
        return list(self.entries)

    def async_get_entry(self, entry_id: str) -> ConfigEntry | None:
        return next((entry for entry in self.entries if entry.entry_id == entry_id), None)

    def async_update_entry(
        self,
        entry: ConfigEntry,
        *,
        data: dict | None = None,
        options: dict | None = None,
        title: str | None = None,
        unique_id: str | None = None,
    ) -> bool:
        changed = False
        if data is not None and data != entry.data:
            entry.data = data
            changed = True
        if options is not None and options != entry.options:
            entry.options = options
            changed = True
        if title is not None and title != entry.title:
            entry.title = title
            changed = True
        if unique_id is not None and unique_id != entry.unique_id:
            entry.unique_id = unique_id
            changed = True

        if changed:
            for listener in list(entry._update_listeners):
                self._hass.async_create_background_task(
                    listener(self._hass, entry), name="fake-update-listener"
                )
        return changed

    async def async_reload(self, entry_id: str) -> bool:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return False
        integration = importlib.import_module("custom_components.saulach")
        await integration.async_unload_entry(self._hass, entry)
        await entry.async_unload()
        return await integration.async_setup_entry(self._hass, entry)

    async def async_forward_entry_setups(self, entry: ConfigEntry, platforms: Iterable[str]) -> None:
        for platform in platforms:
            module = importlib.import_module(f"custom_components.saulach.{platform}")
            key = (entry.entry_id, platform)
            self._platform_entities.setdefault(key, [])
            await module.async_setup_entry(self._hass, entry, self._make_add_entities(key))

    async def async_unload_platforms(self, entry: ConfigEntry, platforms: Iterable[str]) -> bool:
        for platform in platforms:
            key = (entry.entry_id, platform)
            for entity in self._platform_entities.pop(key, []):
                await entity.async_will_remove_from_hass()
                if entity.entity_id is not None and self._hass is not None:
                    self._hass.states.async_remove(entity.entity_id)
        return True

    def _make_add_entities(self, key: tuple[str, str]):
        def _add_entities(new_entities: Iterable[Any], update_before_add: bool = False) -> None:
            from .helpers import device_registry as dr
            from .helpers import entity_registry as er

            for entity in new_entities:
                entity.hass = self._hass
                entity.entity_id = f"{key[1]}.{_slugify_entity_id(entity.unique_id)}"
                self._platform_entities[key].append(entity)
                device_id = None
                device_info = getattr(entity, "_attr_device_info", None) or {}
                identifiers = device_info.get("identifiers")
                if identifiers:
                    device_id = dr.async_get(self._hass).async_get_or_create(
                        config_entry_id=key[0],
                        identifiers=identifiers,
                        name=device_info.get("name"),
                    ).id
                er.async_get(self._hass)._register(entity.entity_id, device_id, entity.unique_id)

        return _add_entities


def _slugify_entity_id(value: str | None) -> str:
    if not value:
        return "unknown"
    return "".join(c.lower() if c.isalnum() else "_" for c in value).strip("_")


class ConfigFlow:
    VERSION = 1
    domain: str | None = None

    def __init_subclass__(cls, *, domain: str | None = None, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if domain is not None:
            cls.domain = domain

    def __init__(self) -> None:
        self.hass: Any = None
        self.unique_id: str | None = None
        self.context: dict[str, Any] = {}

    async def async_set_unique_id(self, unique_id: str) -> None:
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        registry = getattr(self.hass, "config_entries", None)
        if registry is None:
            return
        for entry in registry.async_entries(self.domain):
            if entry.unique_id == self.unique_id:
                raise AbortFlow("already_configured")


    def async_show_form(self, *, step_id: str, data_schema, errors: dict | None = None) -> dict:
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

    def async_create_entry(self, *, title: str, data: dict, options: dict | None = None) -> dict:
        return {"type": "create_entry", "title": title, "data": data, "options": options or {}}

    def async_abort(self, *, reason: str) -> dict:
        return {"type": "abort", "reason": reason}


class OptionsFlow:
    """Real HA provides self.config_entry on instances returned from
    async_get_options_flow automatically; setting it manually in __init__
    is deprecated (HA 2025.12+). Tests set it the same way -- after
    construction, not via a constructor arg."""

    def __init__(self) -> None:
        self.hass: Any = None
        self.config_entry: ConfigEntry | None = None

    def async_show_form(self, *, step_id: str, data_schema, errors: dict | None = None) -> dict:
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

    def async_create_entry(self, *, data: dict) -> dict:
        self.hass.config_entries.async_update_entry(self.config_entry, options=data)
        return {"type": "create_entry", "data": data}
