# Migration Plan: Blueprint → Grapevine

Source blueprint: https://github.com/FeHJa/HA-Blueprint-MQTT-Bridge/blob/main/mqtt_bridge.yaml
Wire protocol contract: `PROTOCOL.md` (authoritative for Phase 1 behavior)

## Goal

Replace the YAML automation blueprint with a native Home Assistant custom
integration (`custom_components/grapevine/`) that is functionally
identical on the wire — any other instance still running the blueprint, or
another migrated instance, must interoperate without changes on its end.

## Guiding principle

Phase 1 is a **behavior-preserving port**, not a redesign. Every detail in
`PROTOCOL.md`, including both documented "known limitations," must be
reproduced exactly. Anything that looks like an improvement (fixing the
object_id collision, per-domain discovery components, availability/LWT)
is explicitly deferred to a later phase so the two known limitations can be
verified against real behavior before anyone decides whether to keep them.

**Forward-compatibility seam (Phase 1 scope, not a wire change):** a target
design for a future protocol generation has been identified — see
`PROTOCOL.md` §8 — that replaces MQTT-Discovery-emulation with a
manifest-publish-and-diff model and native entity creation. It is **not**
being implemented now (it's gated on coordinating a rollout with the other
two bridge instances). Phase 1 is built so reaching it later doesn't
require a rewrite: every outgoing own-payload JSON gets a `protocol_version`
field (§3, value `1` for Phase 1), and the protocol-specific
publish/subscribe/parse logic lives behind a small internal
`ProtocolAdapter` interface rather than being hardcoded into the MQTT I/O
layer. See "Target architecture" and "Phase 3" below.

## Target architecture

```
custom_components/grapevine/
├── __init__.py          # async_setup_entry / async_unload_entry, wires everything together
├── manifest.json         # domain, dependencies: [mqtt], config_flow: true, iot_class
├── const.py               # DOMAIN, CONF_* keys, defaults, sw_version, PROTOCOL_VERSION, the 8 regex patterns
├── config_flow.py         # ConfigFlow + OptionsFlow — maps blueprint inputs (§1), sets unique_id
├── scheduler.py            # entity-tracking, time_pattern trigger, on-demand republish, jitter
├── discovery.py           # payload building (§3), device_class/unit resolution (§3 table)
├── protocol.py             # ProtocolAdapter interface (seam for PROTOCOL.md §8's future manifest protocol)
├── mqtt_io.py              # thin, protocol-agnostic MQTT client wrapper (subscribe/publish, retained)
├── adapters/
│   └── legacy_discovery.py # LegacyDiscoveryAdapter(ProtocolAdapter) — Phase 1's protocol (§2-5), the only adapter that exists right now
├── remote_entity_manager.py # Phase 1b (§5a): create-or-update native entities from incoming federation messages, keyed by unique_id
├── sensor.py                # Phase 1b: BridgedSensorEntity(SensorEntity); issue #12: BridgeMetadataEntities, per-remote-bridge diagnostic entities
├── diagnostics.py           # issue #12: "Download Diagnostics" -- entity list + last published metadata
├── version.py                # issue #12: reads this integration's own release version out of manifest.json
└── strings.json / translations/en.json
tests/
├── test_config_flow.py
├── test_discovery.py       # regex table, name fallback, device_class/unit precedence
├── test_legacy_discovery_adapter.py  # topic construction, publish payload, loop prevention, protocol_version
├── test_remote_entity_manager.py     # create-on-first-sight, update-in-place, state-topic subscription, unload cleanup
├── test_scheduler.py       # time_pattern trigger, jitter bounds, on-demand republish
└── test_integration.py     # entry setup/reload/unload against pytest-homeassistant-custom-component
```

Rationale for the split: `discovery.py` is pure functions (entity/state in,
payload dict out) so the device_class/unit regex table and name-fallback
logic can be unit-tested without a running HA instance or MQTT broker.
`mqtt_io.py` owns raw `homeassistant.components.mqtt` interaction
(`async_subscribe`/`async_publish`) and knows nothing about discovery
payloads or forwarding rules — it's the layer any future adapter reuses
unchanged. `protocol.py` defines the `ProtocolAdapter` interface
(`publish_own_entities()`, `handle_incoming_message(topic, payload)`,
`topics_to_subscribe()`); `adapters/legacy_discovery.py` is Phase 1's (and
today, the only) implementation — it owns everything protocol-specific:
building discovery/state payloads via `discovery.py`, the loop guard (§5),
and stamping `protocol_version` on outgoing payloads. As of Phase 1b
(§5a), it no longer forwards incoming messages anywhere itself — it hands
the parsed payload to `remote_entity_manager.py`, which creates or updates
a native `sensor.py` entity keyed by the payload's `unique_id` and owns
that entity's MQTT state-topic subscription directly (not via entity
lifecycle hooks, so entity creation and its state feed are wired up
atomically). `scheduler.py` drives timing (the `time_pattern` trigger, the on-demand
republish service, jitter) by calling the active adapter's
`publish_own_entities()` — it never talks to MQTT or builds a payload
directly, so a future manifest-based adapter slots in without touching
`scheduler.py` or `__init__.py`'s wiring. Phase 1 has exactly one adapter
registered; the interface exists now so a second one is additive later,
not a rewrite.

**Naming note:** this module is deliberately *not* called `coordinator.py`
and does not subclass HA's `DataUpdateCoordinator` — Phase 1 has no
"pull data periodically into entities" use case that pattern is for, and
naming it `coordinator.py` would invite someone to bolt that base class on
incorrectly later. Wired-up runtime objects (the MQTT bridge instance, the
scheduler, unsub callbacks) live on `entry.runtime_data`
(the standard 2026.x-core pattern), not an ad-hoc `hass.data[DOMAIN]` dict.

**Lifecycle/cleanup (must be explicit in `__init__.py`):** every
subscription and background trigger registered during `async_setup_entry`
must be unregistered via `entry.async_on_unload`, specifically: the
federation `async_subscribe` unsub callback, the `time_pattern` trigger's
cancel callback, and any in-flight jittered publish tasks. This matters
concretely for Phase 2's options-flow reload of `time_pattern` — without
it, a reload leaks the old MQTT subscription and every future incoming
discovery message gets forwarded twice.

**MQTT readiness:** `manifest.json` must declare `"dependencies": ["mqtt"]`,
and setup must wait for the MQTT client to be connected (e.g.
`mqtt.async_wait_for_mqtt_client`) before the first publish/subscribe —
otherwise a publish attempted during HA startup, before the broker
connection is up, is silently lost.

## Config entry mapping (§1)

| Blueprint input | Config entry field | Notes |
|---|---|---|
| `entities` | `data[CONF_ENTITIES]` | list of entity_ids, any domain |
| `shared_discovery_prefix` | `data[CONF_SHARED_DISCOVERY_PREFIX]` | default `share/homeassistant/` |
| `sensor_value_prefix` | `data[CONF_SENSOR_VALUE_PREFIX]` | default `share/jakob/` |
| `time_pattern` | `options[CONF_TIME_PATTERN_MINUTES]` | default 1; options flow so it's editable without reauth |
| `bridge_name` | `data[CONF_BRIDGE_NAME]` | slugified once at setup into `bridge_id`, stored alongside |

`entities` and prefixes go in `data` (identity of the bridge instance);
`time_pattern` goes in `options` (safely reconfigurable, triggers an entry
update listener that resets the interval timer). Config flow validates
`bridge_name` slugifies to a non-empty string and `entities` is non-empty,
and sets `config_entry.unique_id = slug_bridge_name` — this both blocks
accidentally creating two entries with the same bridge identity and gives
Phase 2 multi-entry support a cheap collision guard when
`shared_discovery_prefix` overlaps between entries.

**`local_discovery_prefix` was dropped as of Phase 1b** (see below) — it
existed only to support forwarding incoming federation messages into HA's
local MQTT Discovery root, and that forwarding no longer happens (§5a).
`PROTOCOL.md` §1 keeps it listed as a historical note about what the
*blueprint* does; it's not part of this integration's config schema.

**Resolved (issue #7):** the split above still applies to *where fields are
stored* (`entities`/prefixes/`bridge_name` in `data`, `time_pattern` in
`options`), but not to *how they're edited*. The original plan put the
`data` fields behind a separate "Reconfigure" flow, following the
conventional HA identity-vs-safely-reconfigurable split — in practice that
second entry point wasn't discoverable (users only ever found the gear-icon
"Configure" button and concluded entities/prefixes/bridge_name couldn't be
changed at all). `GrapevineOptionsFlow` was widened to edit every field —
`data` and `options` alike — in one single "Configure" step, updating both
via one `hass.config_entries.async_update_entry(...)` call. There is no
longer a separate `async_step_reconfigure`.

## Phase breakdown

### Phase 1 — Protocol-faithful core (this migration's primary deliverable)

1. Scaffold: `manifest.json`, `const.py`, minimal `config_flow.py` (single
   step form for the 6 inputs above), `__init__.py` with
   `async_setup_entry`/`async_unload_entry`.
2. `discovery.py`: build the discovery payload (§3) exactly — field
   presence/omission rules, the 8-pattern regex table ported **verbatim**,
   friendly_name → title-cased object_id fallback, plus the new
   `protocol_version: PROTOCOL_VERSION` field (const, value `1`) per
   `PROTOCOL.md` §3/§8.
3. `protocol.py` + `adapters/legacy_discovery.py` (the seam — see
   "Target architecture" above), using `mqtt_io.py` for all actual broker
   I/O:
   - Own publish path: discovery → `{shared_discovery_prefix}sensor/{object_id}/config`,
     state → `{sensor_value_prefix}sensor/{object_id}`, both retained (§2, §4).
   - Federation subscribe: `{shared_discovery_prefix}+/+/config` using the
     *configured* prefix (not the blueprint's hardcoded literal — §5).
   - Loop guard ported exactly: skip on `bridge_id` match or `unique_id`
     prefix match (`::` or `.` separator) (§5).
   - What happens to a message that passes the loop guard: originally,
     verbatim byte forwarding to `{local_discovery_prefix}/{component}/{object_id}/config`
     (§5 as written). **Superseded by Phase 1b (§5a)** — see below — before
     this integration was ever pointed at a real broker, so Phase 1 as
     actually built creates a native entity instead. §5's forwarding
     behavior is preserved here only as the historical baseline Phase 1b
     amended.
4. `scheduler.py`:
   - State-change listener on bridged entities → publish discovery+state
     for that one entity, using `trigger`/event `to_state.state` directly
     (§4), not a fresh `states()` read.
   - `time_pattern` trigger → full republish loop over all entities. Use
     a clock-aligned trigger (`async_track_time_change`/the same
     primitive HA's own `time_pattern` automation trigger is built on),
     **not** `async_track_time_interval` anchored to setup time — the §6
     jitter rationale ("three instances firing on the same minute mark")
     only holds if all instances actually fire on the same wall-clock
     minute, which a setup-time-anchored interval does not guarantee.
   - On-demand republish: a domain service call,
     `grapevine.republish`, targeting a config entry (via a
     `config_entry_id`/device selector in `services.yaml`), doing the
     same full republish loop as the time_pattern trigger. This is the
     Phase 1 replacement for the blueprint's `force_republish_sensors`
     event. A `button` entity is deferred to Phase 2 but will be a thin
     wrapper that calls this same service. Services are registered once
     per domain, not per entry, so the handler must dispatch on the
     targeted entry via an entry-keyed registry (small dict of
     `entry_id -> handler`), not a singleton — this keeps multi-entry
     (Phase 2) from requiring a signature change later.
   - Jitter: random 0–9s delay before each discovery/state publish,
     applied per-publish via `hass.async_create_background_task` (tracked
     and auto-cancelled on unload) rather than raw `asyncio.create_task`,
     so a full republish burst can't leak untracked tasks past entry
     unload (equivalent in spirit to the blueprint's `mode: parallel, max: 50`).
5. Unit tests for `discovery.py` (regex table, fallbacks, `protocol_version`
   presence) and `adapters/legacy_discovery.py` (topic strings, publish
   payload shape, loop guard) — these encode `PROTOCOL.md` as executable
   spec. In addition, integration-level tests
   using `pytest-homeassistant-custom-component` (`hass` + `mqtt_mock`
   fixtures) covering config-entry setup/reload/unload: subscription is
   created on setup, torn down on unload/reload (no duplicate forwarding
   after a reload), and the `time_pattern` trigger is cancelled on unload.
   Pure-function tests alone won't catch this class of plumbing bug.
6. Manual interop test: run this integration alongside a real instance of
   the blueprint (or a second migrated instance) against a shared broker,
   confirm the blueprint instance still sees this bridge's own entities via
   MQTT Discovery exactly as before, and that no message-processing loop
   occurs (loop guard still applies regardless of what a passing message
   turns into locally — see Phase 1b/§5a).

**Acceptance criteria:** outbound topic layout, payload shape (including
the new `protocol_version: 1` field), regex table, and loop prevention
match `PROTOCOL.md` exactly; both documented known limitations are present
and unfixed; a blueprint instance and this integration interoperate over
the same broker without behavior changes on the blueprint side; the
`ProtocolAdapter` interface exists and `LegacyDiscoveryAdapter` is its only
implementation — no manifest-based adapter is built or wired up. (Inbound
message handling is covered by Phase 1b's acceptance criteria below, since
it landed as an amendment before real-broker testing.)

### Phase 1b — Native entity creation for federated entities (pulled forward from Phase 3)

Full rationale in `PROTOCOL.md` §5a. Landed before any real-broker testing,
in response to a concrete concern: MQTT-Discovery-forwarded entities are
owned and cleaned up by Home Assistant's own `mqtt` integration, not by
this one, so there was no way for this integration to guarantee they get
removed when you remove the bridge — a real "flooded with orphaned
entities" risk. Unlike the full Phase 3 redesign, this piece only changes
what a *receiving* instance does with an already-loop-guarded incoming
message — nothing about it is observable by the bridge that sent it, so
**it does not require coordinating a rollout with the other two bridge
instances.** Safe to do unilaterally, on this instance alone, at any time.

1. `sensor.py`: new entity platform, `BridgedSensorEntity(SensorEntity)`.
   `_attr_should_poll = False`; attributes (`name`, `device_class`,
   `unit_of_measurement`, `device_info`) set from the incoming discovery
   payload's fields directly — no regex re-derivation, unlike our own
   outbound payload construction, since the incoming payload already
   carries resolved values. `device_info.identifiers` maps the payload's
   `device.identifiers` (bare bridge-id strings) into HA's
   `{(DOMAIN, ident)}` tuple form, namespaced under this integration's own
   domain.
2. `remote_entity_manager.py`: `RemoteEntityManager`, keyed by the
   payload's `unique_id`:
   - First sighting of a `unique_id` → construct a `BridgedSensorEntity`,
     hand it to the platform's `async_add_entities` callback, and
     subscribe to the payload's `state_topic` via `mqtt_io.py` — owned by
     the manager directly (not via `async_added_to_hass`/
     `async_will_remove_from_hass` entity lifecycle hooks), so entity
     creation and its state feed are wired up atomically and don't depend
     on entity-platform timing.
   - Repeat sighting of a known `unique_id` (redelivery on time_pattern
     resync, or the origin bridge changed the entity's name/device_class)
     → update the existing entity's attributes in place and
     `async_write_ha_state()`, not a duplicate entity.
   - Unload (`entry.async_on_unload`): unsubscribe every tracked
     state-topic subscription. Entity removal itself is handled by HA's
     standard platform-unload machinery once `__init__.py` calls
     `async_unload_platforms` — this is the mechanism that actually closes
     the "flooded with orphaned entities" gap: removing the config entry
     removes these entities automatically, no separate depublish code
     needed for the receiving side.
3. `adapters/legacy_discovery.py`: `handle_incoming_message` drops the
   forward-to-`local_discovery_prefix` step entirely and instead calls
   `remote_entity_manager.async_handle_discovery(payload_data)` after the
   (unchanged) loop guard. `local_discovery_prefix` is removed from
   `const.py`/`config_flow.py` — it had no remaining purpose. Topic-shape
   validation (`parse_federation_topic`) is kept as a defensive check, but
   its `component`/`object_id` return values are no longer needed
   downstream — the manager keys everything off the payload's own
   `unique_id`.
4. `__init__.py`: `PLATFORMS = ["sensor"]`; forward/unload platform setups
   around the existing scheduler wiring; `entry.runtime_data` becomes a
   small dataclass holding both the `BridgeScheduler` and the
   `RemoteEntityManager` (previously just the scheduler alone). Platform
   setup (which registers the `async_add_entities` callback) must
   complete *before* `scheduler.async_setup()` starts the federation MQTT
   subscription, or an early incoming message could arrive before
   anything can materialize it — ordering, not a queue, is what
   prevents this race.

**Acceptance criteria:** a federation message that passes the loop guard
results in exactly one native entity per `unique_id`, correctly attributed
(name/device_class/unit/device), whose state updates when its `state_topic`
receives a new retained message; redelivery of the same `unique_id`
updates in place rather than duplicating; removing the config entry
removes the entities; nothing is written to `local_discovery_prefix`
(the field no longer exists in config). Both §2 known limitations remain
present and unfixed, per §5a — this phase doesn't touch them.

### Phase 2 — Integration polish

- ~~Options flow for `time_pattern`~~ **Done** (issue #7): `GrapevineOptionsFlow`
  is the single "Configure" step covering every editable field —
  `time_pattern_minutes` plus the `data` fields (entities/prefixes/
  bridge_name) — wired to an `entry.add_update_listener` that actually
  reloads the entry on any change — previously nothing did. An earlier
  version of this split entities/prefixes/bridge_name into a separate
  `async_step_reconfigure` flow, but that second entry point wasn't
  discoverable in practice (issue #7's reopening) and was folded back into
  `GrapevineOptionsFlow`. Entities dropped from the list, or the whole
  entry on removal, are now depublished (empty retained payload) rather
  than left as stale entities on other instances; see `PROTOCOL.md`
  §5b/`async_depublish_entity`.
- `strings.json`/translations, `manifest.json` metadata for HACS
  (`hacs.json`, versioning).
- ~~Diagnostics platform for support requests~~ **Done** (issue #12): closed
  together with a new metadata message (`PROTOCOL.md` §9) -- each bridge
  publishes its own protocol/integration/HA version, bridged entity count,
  and a last-heartbeat timestamp to a dedicated topic every `time_pattern`
  tick, plus the full payload through `diagnostics.py` for "Download
  Diagnostics". An earlier version of this also surfaced this bridge's
  own metadata locally as a device with three `entity_category:
  diagnostic` entities, same as remote bridges get (`sensor.py`'s
  `BridgeMetadataEntities`) -- reverted per user feedback (added a
  sensor-less device cluttering the device list, see §9's "Local
  surfacing (own bridge): reverted" note). Remote bridges still get their
  device with those three entities; only the own-bridge one was removed.
- ~~Clean up devices orphaned by removing local surfacing~~ **Done**: the
  own-bridge device change above left existing installs with a stranded
  device (its three diagnostic entities no longer recreated, but still
  sitting in the registry) that HA won't offer a UI way to delete while
  the config entry is loaded. `__init__.py`'s `_async_cleanup_orphaned_devices`
  runs on every `async_setup_entry` and removes any device owned by this
  entry with zero entities left in the entity registry. Deliberately
  registry-only and startup-only, not an "ignore this bridge" or
  forget/unforget mechanism -- a device that still has entities is by
  definition in use and is never touched, whether or not the remote
  bridge is currently publishing, so this can't delete anything active
  and can't affect entity history (which is already gone once the
  entities themselves are gone).
- ~~`grapevine.depublish_bridge` service~~ **Done** (issue #12 follow-up):
  the startup cleanup above only catches a device once it has zero
  entities, which never happens for a peer whose empty-retained removal
  signal (§5b) was never actually delivered to us live -- MQTT retains
  forever, so a merely-cleared-on-the-broker or never-cleared topic just
  gets redelivered on our next restart, looking exactly like a live
  bridge. There's no reliable local signal to tell a dead peer from a
  quiet one, so this isn't automatic: the service takes a bridge device
  the user names explicitly (device selector in `services.yaml`,
  Developer Tools → Actions only -- no button entity), and for every
  entity currently materialized from it, publishes an empty retained
  payload to its discovery topic (durably clearing the broker, unlike a
  plain local entity delete) and tears it down locally right away rather
  than waiting on its own publish looping back. See `PROTOCOL.md` §5c.
  `RemoteEntityManager`/`__init__.py` resolve the device back to a
  `RemoteEntityManager` via a small `entry_id -> manager` dict in
  `hass.data[DOMAIN]`, the same pattern `republish_handlers` already
  uses, rather than `hass.config_entries.async_get_entry` -- keeps the
  lookup self-contained the same way the existing republish dispatch is.
- Broaden test coverage (config flow, scheduler timing) toward CI.
- `button` entity per config entry that calls `grapevine.republish`.
- **Multi-entry support** (lower priority — not currently needed, but
  worth designing for): allow multiple config entries so one HA install
  can run several bridge instances (e.g. against different brokers or
  with different `bridge_name`/prefixes). Phase 1 targets a single entry;
  as long as `unique_id`/service registration in Phase 1 are keyed per
  config entry rather than assumed global, this should not require
  rework later. Note: two entries sharing the same `shared_discovery_prefix`
  will each independently process every retained federation message
  (wasteful, not incorrect) — acceptable for Phase 2, worth a mention in
  docs/options-flow help text.

### Phase 3 — Manifest-based protocol (named target, not started)

Full design in `PROTOCOL.md` §8 — summarized here for the phase-plan view.
**Gated on coordinating a rollout with the other two bridge instances; do
not start implementation without that coordination happening first.**
Narrower than originally scoped, now that Phase 1b exists: the *local
materialization* half (native entities instead of MQTT Discovery) is
already done. What's left here is entirely the *outbound* half plus the
subscription model:

- Replace MQTT-Discovery-emulation with each bridge publishing a retained
  JSON manifest (`{bridge_id, object_id, domain, name, device_class, unit,
  state_topic}` per entity) to `ha_bridge/{bridge_id}/manifest`.
- Config flow gains a "follow list" — which peer bridges' manifests to
  subscribe to (opt-in, not blanket-subscribe like today's shared prefix).
- New adapter, `adapters/manifest.py` implementing `ProtocolAdapter`,
  diffs a followed peer's manifest and feeds the *same*
  `remote_entity_manager.py` Phase 1b already built — expect this to be
  close to a drop-in reuse (a manifest entry maps to the same
  create-or-update-by-unique_id shape `RemoteEntityManager` already
  expects), not a second entity-creation mechanism. No loop-guard logic
  needed — the whole §5/§5a message-processing-loop concern goes away for
  bridges that have moved to this protocol, since there's no
  echo-of-your-own-message case when you only ever read peers you
  explicitly follow.
- `protocol_version` (§3/§8) on incoming messages lets a bridge decide,
  per followed peer, whether to speak legacy-discovery or manifest
  protocol — this is what makes a gradual per-partner rollout possible
  instead of a synchronized cutover.
- As a side effect, resolves both §2 known limitations (object_id/domain
  collision, hardcoded `sensor` component) for bridges that have migrated
  — the manifest carries the real domain per entity, unlike the legacy
  discovery payload's hardcoded `sensor` component.
- Investigate blueprint commit history for why availability/LWT tracking
  was removed (§7) before considering reintroducing it — the direct
  entity-platform model (already in place since Phase 1b) makes
  availability tracking straightforward to add if wanted (no more
  piggybacking on MQTT Discovery's own `availability_topic` convention).

## Decisions

1. **On-demand full republish trigger**: `services.yaml` service call
   `grapevine.republish` in Phase 1. The Phase 2 `button` entity
   calls this same service rather than duplicating the republish logic.
2. **Minimum HA core version**: `2026.7`. `manifest.json`
   `"homeassistant"` requirement pinned accordingly; target the
   `mqtt` component's `async_subscribe`/`async_publish` API surface as of
   that release.
3. **Multiple config entries**: not needed for Phase 1 (single entry is
   sufficient), but plausible later. Phase 1 must not hardcode
   assumptions that only one entry exists (e.g. service registration and
   any module-level state must be keyed per config entry). Full
   multi-entry support (e.g. per-entry MQTT client considerations) is
   tracked as a lower-priority Phase 2 item.
4. **`protocol_version` field**: added to every outgoing own-payload JSON
   starting in Phase 1 (value `1`), even though nothing reads it yet.
   Cost is one constant and one dict key; the payoff is that Phase 3's
   manifest protocol can be rolled out per-partner instead of requiring a
   synchronized cutover across all three bridge instances. See
   `PROTOCOL.md` §8.
5. **`ProtocolAdapter` abstraction**: Phase 1's discovery/forwarding/loop-
   guard logic is implemented as `LegacyDiscoveryAdapter`, behind a
   `ProtocolAdapter` interface, rather than inlined into the MQTT I/O
   layer. Only one adapter exists right now — this is purely a seam so a
   future `adapters/manifest.py` (Phase 3, not started) is additive rather
   than a rewrite of `scheduler.py`/`__init__.py`'s wiring. Do not build
   the manifest adapter now; it's gated on cross-instance coordination.
6. **Pull Phase 3's local entity-materialization forward as Phase 1b**:
   done before any real-broker testing, in direct response to the risk
   that MQTT-Discovery-forwarded entities have no cleanup path this
   integration controls. Confirmed safe to do without partner
   coordination, since it only changes what a *receiving* instance does
   with an already-loop-guarded message — see `PROTOCOL.md` §5a. As a
   consequence, `local_discovery_prefix` is removed from the config
   schema entirely (dead field, nothing left to write there) rather than
   kept around unused.
7. **No blocking I/O directly in `async_setup_entry`/adapter constructors**
   (learned the hard way, issue #13): `version.py`'s manifest.json read is
   synchronous file I/O; calling it straight from `LegacyDiscoveryAdapter.
   __init__`/`BridgeMetadataEntities.__init__` ran it on the event loop on
   every setup, including every reload triggered by the Configure flow —
   HA's blocking-call guard caught it and broke the entry until a full
   delete+recreate. The fake HA test harness doesn't model this detection
   at all, so the test suite passed the whole time. Fixed by fetching it
   once via `hass.async_add_executor_job` in `async_setup_entry` and
   threading the value through `GrapevineRuntimeData` instead of letting
   each consumer read the file itself. Any future blocking call (file I/O,
   network, subprocess) must go through `hass.async_add_executor_job` the
   same way — this class of bug is invisible to the test harness, so it
   has to be caught by code review, not by `pytest`.
8. **`entry.async_on_unload` callbacks must return `None`** (learned the
   hard way, issue #13 continued): `lambda: handlers.pop(entry.entry_id,
   None)` in `__init__.py` looked like a harmless cleanup callback, but
   `dict.pop()` returns the *removed value* — here, the
   `scheduler.async_republish_all` bound method that was stored at that
   key. Real HA's on-unload processing treats a truthy callback return as
   a job to schedule as a task, tries to wrap that bound method as a
   coroutine, and crashes with "Failed to Unload" on every unload
   (including every reload the Configure flow triggers). Fixed by wrapping
   the cleanup in a named function that discards the pop's result. Unlike
   decision 7, this one *is* now caught by the test harness — `ConfigEntry
   .async_unload()`'s fake now mirrors this exact real-HA behavior
   (raises if a callback returns something truthy and non-awaitable), so
   any future on_unload callback making the same mistake fails a test
   instead of only showing up in a user's log.

Phase 1 work can start directly from the file layout above, using
`PROTOCOL.md` as the acceptance spec for each module.

## Architecture review (pre-implementation gate)

Before starting Phase 1 coding, this plan was reviewed by a dedicated
software-architecture pass. Findings already folded into the sections
above:

- Renamed `coordinator.py` → `scheduler.py` to avoid implying HA's
  `DataUpdateCoordinator` pattern, which doesn't fit this use case.
- Runtime state lives on `entry.runtime_data`, not `hass.data[DOMAIN]`.
- Explicit unload/reload cleanup requirement (subscriptions, trigger
  cancellation, in-flight jittered tasks) called out in Phase 1 step 4.
- `time_pattern` must be a clock-aligned trigger, not a setup-time-anchored
  interval, or the §6 jitter rationale doesn't hold.
- `config_entry.unique_id = slug_bridge_name` added to the config-flow
  mapping section.
- `manifest.json` MQTT dependency + wait-for-client requirement added.
- Jittered publishes must use `hass.async_create_background_task`, not
  raw `asyncio.create_task`, so they're cancelled on unload.
- Service registration must dispatch per targeted config entry (small
  entry-keyed registry), since HA services are domain-global — flagged in
  Phase 1 step 4 to avoid a signature change when Phase 2 multi-entry
  lands.
- Testing strategy extended to include `pytest-homeassistant-custom-component`
  integration tests for entry setup/reload/unload, not just pure-function
  unit tests.
- Two open tradeoffs flagged but deliberately left as-is for now: the
  data/options split may need revisiting if `entities`/prefixes turn out
  to need frequent edits (see Config entry mapping section), and
  same-prefix multi-entry federation overlap is accepted as a documented
  Phase 2 wrinkle rather than solved now.
