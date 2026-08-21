"""Saulach — peer-to-peer entity federation for Home Assistant.

A native integration port of the MQTT bridge blueprint. See PROTOCOL.md
for the wire-protocol contract this must reproduce, and MIGRATION_PLAN.md
for the phased rollout this is Phase 1(b) of.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import mqtt_io
from .adapters.legacy_discovery import LegacyDiscoveryAdapter
from .const import (
    ATTR_BRIDGE_DEVICE,
    ATTR_CONFIG_ENTRY_ID,
    CONF_ENTITIES,
    DOMAIN,
    SERVICE_DEPUBLISH_BRIDGE,
    SERVICE_REPUBLISH,
)
from .remote_entity_manager import RemoteEntityManager
from .scheduler import BridgeScheduler
from .version import integration_version

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

SERVICE_REPUBLISH_SCHEMA = vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string})
SERVICE_DEPUBLISH_BRIDGE_SCHEMA = vol.Schema({vol.Required(ATTR_BRIDGE_DEVICE): cv.string})


@dataclass
class SaulachRuntimeData:
    scheduler: BridgeScheduler
    remote_entity_manager: RemoteEntityManager
    protocol_adapter: LegacyDiscoveryAdapter
    integration_version: str


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await mqtt_io.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT integration is not ready")

    _async_cleanup_orphaned_devices(hass, entry)

    # integration_version() reads manifest.json off disk -- must not run
    # directly on the event loop (issue #13: this crashed/hung the entry
    # on every setup, including reload, when called synchronously from
    # LegacyDiscoveryAdapter's constructor).
    version = await hass.async_add_executor_job(integration_version)

    remote_entity_manager = RemoteEntityManager(hass, entry)
    adapter = LegacyDiscoveryAdapter(hass, entry, remote_entity_manager, version)
    scheduler = BridgeScheduler(hass, entry, adapter)
    entry.runtime_data = SaulachRuntimeData(
        scheduler=scheduler,
        remote_entity_manager=remote_entity_manager,
        protocol_adapter=adapter,
        integration_version=version,
    )
    entry.async_on_unload(remote_entity_manager.async_unload)
    # SaulachOptionsFlow's single "Configure" step (data and options
    # fields alike) goes through hass.config_entries.async_update_entry,
    # which is what fires this (issue #7; previously nothing reloaded the
    # entry after a change).
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # Must happen before scheduler.async_setup() starts the federation MQTT
    # subscription below -- the sensor platform registers the
    # async_add_entities callback RemoteEntityManager needs before it can
    # materialize anything (§5a); this ordering, not a queue, is what
    # prevents an early incoming message from being dropped.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Services are domain-global, not per-entry, so on-demand republish
    # dispatches through a small entry-keyed registry rather than a
    # singleton — this keeps the signature stable when Phase 2 multi-entry
    # support lands (MIGRATION_PLAN.md Decision 3). depublish_bridge below
    # needs to go from a device the user picked back to *its* entry's
    # RemoteEntityManager, so it gets the same treatment rather than
    # depending on hass.config_entries.async_get_entry/async_entries --
    # this stays self-contained even before a real config entry add flow
    # (or its test-harness equivalent) has run.
    handlers = hass.data.setdefault(DOMAIN, {}).setdefault("republish_handlers", {})
    handlers[entry.entry_id] = scheduler.async_republish_all
    managers = hass.data[DOMAIN].setdefault("remote_entity_managers", {})
    managers[entry.entry_id] = remote_entity_manager

    def _unregister_republish_handler() -> None:
        # Not `lambda: handlers.pop(...)` -- dict.pop() returns the
        # removed value (the scheduler method itself), and HA's real
        # on_unload processing treats a truthy callback return as a job
        # to schedule as a task, then crashes trying to wrap a plain
        # bound method as a coroutine. This wrapper discards the result
        # so the callback returns None, as every other on_unload
        # callback here does.
        handlers.pop(entry.entry_id, None)
        managers.pop(entry.entry_id, None)

    entry.async_on_unload(_unregister_republish_handler)

    if not hass.services.has_service(DOMAIN, SERVICE_REPUBLISH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REPUBLISH,
            _make_republish_service_handler(hass),
            schema=SERVICE_REPUBLISH_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DEPUBLISH_BRIDGE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DEPUBLISH_BRIDGE,
            _make_depublish_bridge_service_handler(hass),
            schema=SERVICE_DEPUBLISH_BRIDGE_SCHEMA,
        )

    await scheduler.async_setup()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Unloading the sensor platform removes the native entities this entry
    # created (§5a) -- this is what makes "remove the bridge" actually
    # clean up after itself for federated entities, unlike the old
    # forward-to-local-discovery approach where HA's own mqtt integration
    # owned that lifecycle.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # The receiving-side half of cleanup (native entities) is handled by
    # async_unload_entry above via the platform unload, which HA calls
    # before this. This is the sending-side half (issue #7): depublish
    # every entity this bridge was publishing, so other instances --
    # Saulach or blueprint -- don't keep it around as a stale entity
    # forever. Uses the same async_depublish_entity as the options flow's
    # per-entity removal.
    runtime_data: SaulachRuntimeData | None = entry.runtime_data
    if runtime_data is None:
        return
    for entity_id in entry.data.get(CONF_ENTITIES, []):
        await runtime_data.protocol_adapter.async_depublish_entity(entity_id)


def _async_cleanup_orphaned_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Startup housekeeping: remove any device this entry owns that has no
    entities left in the entity registry.

    Registry-only, so it can never touch a device that's actually in use --
    an entity registry entry persists across restarts regardless of
    whether RemoteEntityManager has re-materialized it in memory yet, so
    "zero entities" here really does mean nothing is attached anymore (e.g.
    the pre-#12-follow-up own-bridge diagnostic device, or a remote
    bridge's device after its entities were deleted by hand) -- not merely
    "nothing has arrived over MQTT since HA restarted."
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    _LOGGER.debug("Orphaned-device cleanup: %d device(s) owned by this entry", len(devices))
    for device in devices:
        entities = er.async_entries_for_device(
            entity_registry, device.id, include_disabled_entities=True
        )
        if entities:
            _LOGGER.debug(
                "Orphaned-device cleanup: keeping device %s (%s) -- %d entit(y/ies) still attached",
                device.id,
                device.name,
                len(entities),
            )
            continue
        _LOGGER.info(
            "Removing device %s (%s): no entities left in the registry", device.id, device.name
        )
        device_registry.async_remove_device(device.id)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _make_republish_service_handler(hass: HomeAssistant):
    async def _async_handle_republish(call: ServiceCall) -> None:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        handlers = hass.data.get(DOMAIN, {}).get("republish_handlers", {})
        handler = handlers.get(entry_id)
        if handler is None:
            raise ServiceValidationError(f"No Saulach instance for config entry {entry_id}")
        handler()

    return _async_handle_republish


def _find_remote_entity_manager_for_device(hass: HomeAssistant, device_id: str) -> RemoteEntityManager | None:
    device_registry = dr.async_get(hass)
    managers: dict[str, RemoteEntityManager] = hass.data.get(DOMAIN, {}).get(
        "remote_entity_managers", {}
    )
    for entry_id, manager in managers.items():
        owned_devices = dr.async_entries_for_config_entry(device_registry, entry_id)
        if any(device.id == device_id for device in owned_devices):
            return manager
    return None


def _make_depublish_bridge_service_handler(hass: HomeAssistant):
    async def _async_handle_depublish_bridge(call: ServiceCall) -> None:
        # Never automatic/heuristic -- a human has already decided, by
        # naming a specific device here, that this peer bridge is dead
        # (CLAUDE.md §5c). We only act on exactly the entities this
        # instance currently has materialized for it.
        device_id = call.data[ATTR_BRIDGE_DEVICE]
        device_registry = dr.async_get(hass)
        device = device_registry.async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"No such device: {device_id}")

        bridge_id = next((ident for (domain, ident) in device.identifiers if domain == DOMAIN), None)
        if bridge_id is None:
            raise ServiceValidationError(f"Device {device_id} is not a Saulach bridge device")

        manager = _find_remote_entity_manager_for_device(hass, device_id)
        if manager is None:
            raise ServiceValidationError(f"No running Saulach instance owns device {device_id}")

        removed = await manager.async_depublish_bridge(bridge_id, device_id)
        # Every entity async_depublish_bridge knew about for this device
        # is gone now (it swept up anything still in the registry too),
        # so the device itself is guaranteed orphaned -- no need to wait
        # for the next startup's cleanup pass (#18) to catch it.
        device_registry.async_remove_device(device_id)
        _LOGGER.info("Depublished bridge %s: %d topic(s) cleared", bridge_id, removed)

    return _async_handle_depublish_bridge
