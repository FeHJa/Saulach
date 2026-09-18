# Roadmap

Source blueprint: https://github.com/FeHJa/HA-Blueprint-MQTT-Bridge/blob/main/mqtt_bridge.yaml
Wire protocol: `PROTOCOL.md` (authoritative for on-the-wire behavior)

## Status: Phase 1 baseline

Saulach Bridge is a behavior-preserving native port of the blueprint automation,
running in production across multiple independent Home Assistant installs — including
federation with at least one instance still on the unmodified blueprint. That
cross-instance compatibility, without behavior changes required on the blueprint side,
is Phase 1's acceptance test, and it's met.

"Phase 1" here includes what was originally scoped as a later Phase 3 piece — native
entity creation for incoming federated entities, pulled forward early because
MQTT-Discovery-forwarded entities had no cleanup path this integration controlled (see
`PROTOCOL.md` §5a). Everything since has been a backward-compatible amendment on top of
this baseline (tracked in `PROTOCOL.md`, one section per topic) or maintenance; none of
it required coordinating a rollout with the other bridge instances.

## Architecture

```
custom_components/saulach/
├── __init__.py                entry setup/unload, service registration, wiring
├── config_flow.py             config + options flow (single "Configure" step)
├── const.py                   domain, config keys, defaults, protocol constants
├── discovery.py               discovery payload building, device_class/unit resolution
├── protocol.py                ProtocolAdapter interface (seam for a future Phase 3 adapter)
├── adapters/legacy_discovery.py   LegacyDiscoveryAdapter — the only adapter today (PROTOCOL.md §2-9)
├── scheduler.py                publish timing: state-change, time_pattern, on-demand, jitter
├── remote_entity_manager.py    create/update/remove native entities from incoming federation messages
├── sensor.py                    BridgedSensorEntity, BridgeMetadataEntities
├── diagnostics.py               "Download Diagnostics" support
├── mqtt_io.py                    thin MQTT publish/subscribe wrapper
└── version.py                    reads this integration's own version from manifest.json
```

`discovery.py` and `mqtt_io.py` are pure/low-level building blocks, unit-tested in
isolation from Home Assistant. `ProtocolAdapter` exists so a future manifest-based
protocol generation (Phase 3, see `PROTOCOL.md` §8) is an additive second adapter
rather than a rewrite of `scheduler.py` or `__init__.py`'s wiring — only one adapter
exists today. Runtime state lives on `entry.runtime_data`, not `hass.data[DOMAIN]`; the
exception is a couple of small entry-keyed registries `__init__.py` uses to dispatch
domain-global services to the config entry they were called for.

## Open items

- **Multi-entry support.** Not implemented — Phase 1 assumes a single config entry.
  Services and internal registries are already keyed per entry rather than treated as
  global, so adding this later shouldn't require rework. Two entries sharing a
  `shared_discovery_prefix` would each independently process every retained federation
  message — wasteful but not incorrect.
- **Self-loop reappearance, under investigation.** A user's own pre-rename bridge
  identity has been seen reappearing locally even after depublishing it from a peer's
  instance. Not yet root-caused: either a peer's depublish only cleared the topics *it*
  had discovered (leaving others this instance still holds retained), or the loop
  guard's `bridge_id`/`unique_id` check no longer matches this instance's *current*
  identity after a rename — which would be a real bug in the loop guard, not just a
  depublish gap. Needs a reproduction that confirms which before it can be scoped.

## Phase 3 — manifest protocol (not started)

Full design in `PROTOCOL.md` §8. Replaces MQTT-Discovery-emulation with each bridge
publishing its own manifest, and other instances explicitly opting into the peers they
follow — removing the object_id/domain collision and hardcoded-component known
limitations along the way. **Gated on coordinating a rollout with the other bridge
instances; do not start building it unprompted.** The `protocol_version` field every
Phase 1 payload already carries exists to make that rollout gradual and per-partner
instead of a synchronized cutover.

## Engineering notes

Non-obvious constraints worth knowing before touching this codebase:

- **Blocking I/O never runs directly on the event loop.** `version.py`'s manifest.json
  read is synchronous file I/O; it's fetched once via `hass.async_add_executor_job` in
  `async_setup_entry` and threaded through `SaulachRuntimeData`, not re-read by each
  consumer. HA's blocking-call guard breaks entry setup/reload otherwise, and the fake
  test harness doesn't model that detection — this class of bug has to be caught by
  review, not by `pytest`.
- **`entry.async_on_unload` callbacks must return `None`.** A callback that returns a
  truthy, non-awaitable value (e.g. `dict.pop()`'s return value) makes real HA try to
  schedule it as a task and crash on unload. The fake harness's `ConfigEntry.async_unload()`
  does model this, so a new callback making the same mistake fails a test instead of
  only showing up in a user's log.
- **Testing strategy: the fake harness plus production use, not CI or a real-HA test
  layer.** `pytest-homeassistant-custom-component` was considered and declined — see
  `requirements_test.txt` for why. Production use across multiple federated instances
  has reliably surfaced the same class of bug that layer would have caught (blocking
  I/O, unload-callback crashes) — later than CI would have, but cheap to fix once
  found. This is the accepted long-term strategy, not an interim state.

## History

Originally shipped as "Grapevine"; renamed to Saulach Bridge (domain, package, and
later the GitHub repository) partway through development, for branding reasons rather
than a technical one. Home Assistant has no config-entry migration path across a domain
rename, so every existing install's entry had to be recreated — remove the old entry
first (so its depublish-on-removal path still ran under the old domain), upgrade, then
recreate under the new domain with the same `bridge_name` for wire continuity.
