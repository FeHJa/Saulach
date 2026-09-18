# Wire Protocol Contract

Source blueprint: https://github.com/FeHJa/HA-Blueprint-MQTT-Bridge/blob/main/mqtt_bridge.yaml

This is the MQTT wire protocol Saulach Bridge speaks, reverse-engineered from the
original blueprint automation so that Saulach interoperates with instances still
running the blueprint unmodified. Treat every detail here as required behavior unless
marked a **known limitation** (kept deliberately, for compatibility) or an
**amendment** (a later, backward-compatible addition — each one explains why it needed
no coordination with the other bridge instances).

> `CLAUDE.md` in the repo root is a byte-identical copy of this file — AI coding
> assistants load `CLAUDE.md` automatically as project context, so it has to exist
> under that name too. Edit this file and copy it over `CLAUDE.md`; never edit
> `CLAUDE.md` directly.

## 1. Configuration inputs

| Input | Default | Purpose |
|---|---|---|
| `entities` | — | list of entity_ids to bridge, any domain |
| `shared_discovery_prefix` | `share/homeassistant/` | shared federation prefix on the broker |
| `local_discovery_prefix` | `homeassistant` | this instance's own discovery prefix (blueprint only — **not used by this integration**, see §5a) |
| `sensor_value_prefix` | `share/jakob/` | where this instance's own state values are published |
| `time_pattern` | `/1` | periodic full-republish interval, minutes |
| `bridge_name` | `Bridge Jakob` | human name; slugified into `bridge_id` |

`slug_bridge_name` = lowercase `bridge_name`, spaces → `_`, then strip everything not in
`[a-z0-9_]`.

## 2. Topic layout

- Own discovery config → `{shared_discovery_prefix}sensor/{object_id}/config` (retained)
- Own state value → `{sensor_value_prefix}sensor/{object_id}`
- Forwarded remote discovery → `{local_discovery_prefix}/{component}/{object_id}/config` (retained)
  — **this is the blueprint's behavior; this integration does not do this, see §5a**
- `object_id` = `entity_id.split('.')[-1]` (domain stripped)
- `component` / `object_id` for forwarding are parsed positionally from the incoming topic,
  at the position right after the shared prefix

**Known limitation:** `object_id` excludes the domain, so two entities in different
domains sharing an object_id (e.g. `sensor.garage` and `binary_sensor.garage`) collide
on the same topic — the retained message from whichever publishes last wins. This
happens upstream, on the *origin* bridge's own publish path, before a receiver ever
sees it — §5a's native materialization doesn't change that.

**Known limitation:** the discovery *component* segment for own entities is hardcoded
to `sensor` regardless of the source entity's actual domain. A bridged `binary_sensor`
or `input_boolean` is published as a generic MQTT `sensor`, not its native discovery
type. A receiving instance only ever sees `component: sensor`, native-entity or not.

## 3. Discovery payload (own entities → shared prefix)

```json
{
  "name": "<friendly_name, or title-cased object_id if missing>",
  "state_topic": "<sensor_value_prefix>sensor/<object_id>",
  "unique_id": "<slug_bridge_name>::<entity_id>",
  "device_class": "<omitted entirely if unknown, not null>",
  "unit_of_measurement": "<omitted entirely if unknown, not null>",
  "bridge_id": "<slug_bridge_name>",
  "protocol_version": 1,
  "device": {
    "identifiers": ["<slug_bridge_name>"],
    "name": "<bridge_name>",
    "sw_version": "1.0.3"
  }
}
```

`protocol_version` is not present in the original blueprint's payload, but is safe to
add: other instances already tolerate the non-standard `bridge_id` key (unknown JSON
keys are ignored), so one more integer field doesn't break forwarding or loop
prevention. See §8 for why it's there.

### device_class / unit_of_measurement resolution order

1. Use the source entity's actual `device_class` / `unit_of_measurement` attribute, but
   only if the source entity's real domain is `sensor` (see the amendment below).
2. Else, regex-match the `object_id` suffix against these 8 known patterns (first match
   wins, case-insensitive, pattern shape is `(^|_)<word>(_|$)`):

| object_id contains | device_class | unit |
|---|---|---|
| `temperature` | temperature | °C |
| `humidity` | humidity | % |
| `pressure` | pressure | hPa |
| `power` | power | W |
| `energy` | energy | kWh |
| `current` | current | A |
| `voltage` | voltage | V |
| `light` | illuminance | lx |

3. If neither matches, the key is omitted from the payload entirely (not sent as `null`).

Port these regexes verbatim — do not rewrite or "simplify" them.

**Amendment — domain check on `device_class` (issue #13):** the `component` segment is
hardcoded to `sensor` regardless of the source entity's real domain (the known
limitation above), so forwarding a non-`sensor` source's `device_class` verbatim is
unsafe. Two failure modes, both needing a domain check rather than a device_class-name
check:
- Some names (`light`, `motion`, ...) are only valid for `binary_sensor`, never
  `sensor` — forwarding them made a receiver's own `mqtt` integration reject the whole
  discovery message outright.
- Some names (`moisture`, `battery`, `power`, ...) are valid members of *both*
  `SensorDeviceClass` and `BinarySensorDeviceClass`, with different value semantics —
  numeric for `sensor`, boolean `on`/`off` for `binary_sensor`. A name-only check lets
  these through; a `binary_sensor`'s `on`/`off` state then crashes numeric coercion on
  the receiving side.

Omitting the field is always safe — a receiver still gets a working, if less specific,
entity.

## 4. State payload

Raw state string only (no JSON wrapping), published to the state topic. Uses
`trigger.to_state.state` on state-triggered publishes (cheaper than re-reading
`states()`). Not retained — see the issue #29 amendment below.

**Amendment — translate `unavailable`/`unknown` on receipt (issue #27):** a bridged
entity's source can legitimately go unavailable, in which case the state payload is the
literal string `"unavailable"` or `"unknown"` (HA's own sentinels). This isn't a wire
change — a compliant sender always publishes whatever `to_state.state` is. But a
receiving instance's native materialization (§5a) must not write that literal string
into an entity's value: HA recognizes "no value" only via `native_value = None` and
"not available" only via the `available` property, never via a sentinel string, and a
sensor with a numeric `device_class` crashes trying to coerce `"unavailable"` to a
number. `BridgedSensorEntity.set_native_value` maps `"unavailable"` →
`native_value = None, available = False`, and `"unknown"` →
`native_value = None, available = True` (still available, just no current reading).

**Amendment — state is no longer retained (issue #29):** the original blueprint, and
this integration up to this point, published the state topic retained, same as
discovery. A retained message is redelivered verbatim to any fresh subscriber (broker
reconnect, receiver restart) — indistinguishable from a live publish. Harmless for a
plain last-value display (the only thing this protocol's own receivers ever did with
it), but it corrupts a receiver that treats incoming state as a delta or accumulates
it, since there's no way to tell a stale replay from a fresh publish. State-topic
publishes now use `retain=False`; the discovery topic is untouched and stays retained.
A receiver that (re)subscribes between state publishes now sees no value until the next
one arrives — up to one `time_pattern` interval in the worst case — an accepted
tradeoff since nothing about a plain last-value display depends on retention.

This needed no coordination with the other bridge instances: `retain` is a
broker-delivery flag, not something either side parses out of the payload or topic, so
dropping it only changes *when* a subscriber sees a value, never what it reads once one
arrives.

Because a broker never clears a retained message just because a later publish on the
same topic isn't retained, every state value published before this fix is still
retained on the broker. `LegacyDiscoveryAdapter.async_clear_retained_state` publishes
an empty retained payload to each bridged entity's state topic on every startup, before
the scheduler's own startup republish — unconditional and idempotent (a no-op once
already cleared) rather than a one-shot migration flag, so it's self-healing if a stale
retained value ever reappears.

## 5. Incoming discovery handling (federation from other instances)

- Subscribe to `{shared_discovery_prefix}+/+/config`, using the actually configured
  `shared_discovery_prefix` (the blueprint hardcodes this as a literal string — a
  blueprint-engine limitation, not a protocol requirement)
- On message: parse `component` / `object_id` from the topic (see §2), forward the
  **payload verbatim, unchanged bytes**, to
  `{local_discovery_prefix}/{component}/{object_id}/config`, retained
- **Loop prevention (must be preserved exactly):** skip forwarding if
  `payload.bridge_id == own slug_bridge_name`, OR `payload.unique_id` starts with
  `"{slug_bridge_name}::"` or `"{slug_bridge_name}."`. The other instances rely on
  recognizing this bridge_id/unique_id prefix convention to avoid re-forwarding your own
  messages back to you — deviate from this exactly and expect forwarding loops.

## 5a. Amendment: local materialization via native entities

**Supersedes the local-forwarding step in §5.** Everything else in §5 — subscribing
with the configured prefix, parsing `component`/`object_id`, the loop guard — is
unchanged. What changes: instead of forwarding the verbatim payload into HA's built-in
`mqtt` integration via `local_discovery_prefix`, this integration parses the payload
itself and creates or updates a native entity directly, keyed by the payload's
`unique_id`.

Safe without coordinating with the other instances because it's purely receiving-side:
what a bridge does with a message *after* the loop guard is never observable by whoever
sent it. The wire protocol is unchanged.

This is what makes config-entry removal clean up federated entities automatically (no
separate depublish step needed on the receiving side), and stops this integration from
writing into `local_discovery_prefix` at all — removing the collision risk with
Zigbee2MQTT/ESPHome/Tasmota discovery that motivated §8's redesign in the first place,
for the receiving side, without waiting on Phase 3. It does **not** fix either §2 known
limitation — both originate on the sending side, before this instance ever sees the
message.

**Amendment (issue #13 continued):** a native entity materialized here is just as
exposed to §3's device_class failure mode as the sending side, and nothing about the
wire format stops a peer from sending an unsafe payload. Rather than trust the payload,
`RemoteEntityManager` recovers the source entity's real domain from the payload's own
`unique_id` (`{slug_bridge_name}::{entity_id}`, §3) and applies the same domain check —
for both entity creation and update-in-place on redelivery. A `unique_id` that doesn't
match this convention is treated as unsafe (device_class dropped).

`local_discovery_prefix` remains listed in §1 as a historical note about what the
*blueprint* does; it is not part of this integration's config.

## 5b. Amendment: depublishing own entities (removal signal)

Closes the gap §5a leaves open: cleanup of *this* bridge's own entities as seen by
*other* instances. When an entity stops being bridged — dropped from the config entry,
or the whole entry removed — this instance publishes an **empty retained payload** to
that entity's own discovery topic and state topic. An empty retained payload on a
discovery config topic is the standard MQTT Discovery removal convention, so
blueprint-based receivers get this for free through their existing forwarding step.

Saulach-based receivers materialize entities natively (§5a) instead of going through
the `mqtt` integration, so `RemoteEntityManager` recognizes an empty payload on a topic
it previously saw a real payload on — correlated by *topic*, since an empty payload
carries no JSON `unique_id` to read — and removes the native entity it created for it.

## 5c. Amendment: manual depublish of a confirmed-dead peer bridge

§5b's removal signal only reaches a receiver that's online and subscribed at the exact
moment it's published; MQTT retains a topic's last payload forever otherwise, so a
peer's entities can keep reappearing on every restart even after the peer itself is
long gone, with nothing left to send the missing removal signal.

There's no automatic detection for this — MQTT gives no reliable signal that a peer is
dead rather than merely quiet. Deciding a bridge is dead is deliberately left to a
human. `saulach.depublish_bridge` (a service, not automatic) takes a bridge device the
user names explicitly, and reads the *entity registry* rather than
`RemoteEntityManager`'s in-memory state — the whole reason this service exists is for
peers that were never rediscovered this session, so they have no in-memory footprint at
all and show as "Unavailable" while still sitting in the registry. For each entity it
finds, it:
- publishes an empty retained payload to that entity's own discovery topic,
  reconstructed from its `unique_id` (`{bridge_id}::{entity_id}`, §3) — the same topic
  and removal convention as §5b, indistinguishable to any other receiver from the
  origin bridge's own depublish, and
- tears it down immediately, through the normal §5b path if `RemoteEntityManager` does
  have it live this session, or directly from the registry otherwise, rather than
  waiting on its own publish to loop back over MQTT.

Diagnostic entities (§9) have no discovery topic of their own — they're removed as a
side effect once every real entity for the bridge is gone, same as an organic removal.

Publishing to a topic this instance didn't originate is unusual but not a protocol
violation — the shared prefix has no per-topic ownership model, and §5b's convention
only cares that an empty retained payload arrived, not who sent it. Because the
broker's retained store is actually cleared this time, the peer stops reappearing on
future restarts too — unlike a plain local entity deletion, which only hides it until
the next restart redelivers the same stale retained messages.

## 6. Publish triggers and timing

- State-change on any bridged entity → publish discovery + state for that one entity
- Time-pattern trigger (every `time_pattern` minutes) → full republish loop over all
  bridged entities (discovery + state) — the resync-after-restart /
  retained-message-refresh mechanism
- On-demand full republish (`saulach.republish` service; was a custom HA event
  `force_republish_sensors` in the blueprint) → same as above
- Incoming MQTT discovery on the shared prefix → forwarding logic (§5)
- **Jitter:** before any discovery/state publish, a random 0–9 second delay is applied,
  to desync near-simultaneous publishes from multiple instances hitting the broker at
  the same moment (e.g. three instances all firing on the same time-pattern minute
  mark). Preserve this or a functionally equivalent spread mechanism.
- Original automation ran with `mode: parallel, max: 50` — relevant because bursts of
  state changes across many bridged entities can produce many simultaneous publishes.

## 7. Deliberately dropped feature

The blueprint's name is "...(stable, no availability)" — availability/LWT tracking
existed at some point and was removed for stability. Check the source repo's commit
history for why before reintroducing this.

## 8. Forward compatibility: Phase 3 target design (named, not implemented)

This integration reproduces the blueprint's MQTT-Discovery-emulation protocol, with one
deliberate, backward-compatible deviation (§4's issue #29 amendment dropping `retain`
on state-topic publishes). Otherwise, no wire changes. A target design for a future
protocol generation has been identified and is documented here so it doesn't need to be
rediscovered later. **Not implemented, and gated on coordinating a rollout with the
other two bridge instances — do not build it unprompted.**

**Problem it solves:** the current protocol emulates MQTT Discovery by writing into
`local_discovery_prefix` (`homeassistant/` by default) — a namespace shared with
Zigbee2MQTT/ESPHome/Tasmota discovery. Combined with §2's object_id/domain collision,
this is a real risk of a bridged entity colliding with an unrelated device's discovery
message.

**Target design:** each bridge instance publishes its own retained JSON "manifest" — a
list of `{bridge_id, object_id, domain, name, device_class, unit, state_topic}` per
bridged entity — under a dedicated, bridge-only topic tree:
`ha_bridge/{bridge_id}/manifest`. Other instances subscribe only to the manifests of
bridges they explicitly opt into (a config-flow "follow list", not a blanket subscribe
to the shared prefix), diff the manifest against the native entities already
instantiated for that peer, and create/remove entities directly. No MQTT Discovery
emulation, no writes to the local discovery root, no forwarding/echo-prevention logic
(§5). As a side effect, this eliminates both §2 known limitations — entities carry
their real domain and no longer collide on a shared `sensor/{object_id}` topic.

**Migration path — `protocol_version`:** every own-payload JSON carries a
`protocol_version` integer field (§3; `1` today). A future manifest-based payload will
carry its own (`2+`), letting any instance decide per bridge partner whether to speak
legacy discovery or manifest — a gradual, partner-by-partner rollout instead of a
synchronized cutover.

`MIGRATION_PLAN.md` documents the internal `ProtocolAdapter` seam that keeps this
implementation swappable when Phase 3 is eventually built.

**Relationship to §5a:** §5a already brought the *local materialization* half of this
design forward. What Phase 3 still owns exclusively is the *outbound* half (own
entities as a manifest instead of MQTT Discovery emulation) and the follow-list
subscription model — both wire-protocol changes requiring cross-instance coordination.
Its manifest-diffing logic is expected to feed the same entity materialization layer
§5a introduced, not a second one.

## 9. Metadata message (issue #12)

A small, additive side-channel alongside §2-§5: each bridge periodically publishes a
retained JSON message describing itself — protocol version, this integration's own
release version, the local HA version, how many entities it's currently bridging, and a
last-heartbeat timestamp.

**Topic:** `{shared_discovery_prefix}bridge/{slug_bridge_name}/metadata`, retained.

**Payload:**

```json
{
  "protocol_version": 1,
  "integration_version": "0.1.10",
  "bridge_id": "bridge_jakob",
  "ha_version": "2026.8.0",
  "entity_count": 7,
  "last_heartbeat": "2026-08-06T08:14:00+00:00"
}
```

`last_heartbeat` is a plain "as of this publish" UTC timestamp, refreshed every
publish — it carries no online/offline or staleness inference. Availability tracking is
the feature §7 says was deliberately dropped from the blueprint for stability, and
stays out of scope here.

Needs no coordination with the other bridge instances: the topic has three segments
ending in `metadata`, so it never matches `{shared_discovery_prefix}+/+/config`, the
pattern every receiver subscribes to today (§5). Nobody sees this message unless they
deliberately opt in, so publishing it unilaterally changes nothing about how any
existing receiver behaves.

**Timing:** published on the same `time_pattern` tick as the full discovery/state
republish (§6) — at startup, on the periodic trigger, and on-demand via the
`republish` service — rather than a second, separately configurable interval. Subject
to the same jitter as any other publish.

**Local surfacing (own bridge): wire-only.** An earlier version also surfaced this
bridge's own metadata locally, as a device with three `entity_category: diagnostic`
entities (entity count, last heartbeat, HA version). This just added a device with no
sensors on it to every install, without adding information the user didn't already have
some other way, so it was removed — this bridge's own metadata is published every tick
as documented above and available via "Download Diagnostics"
(`diagnostics.py` reads the same `last_metadata` the scheduler tracks), but not
materialized as a local device or entities. **Remote** bridges' metadata is unaffected —
a remote bridge's device is something the user has no other local visibility into.

**Consuming other bridges' metadata:** `LegacyDiscoveryAdapter` also subscribes to
`{shared_discovery_prefix}bridge/+/metadata`. Deliberately not a follow-list/opt-in
mechanism (§8's Phase 3 manifest is where that belongs) — eligibility falls out of
state this integration already has: a remote bridge's metadata is only shown if
`RemoteEntityManager` has already materialized at least one entity from that
`bridge_id` via §5a. A metadata message for a bridge with no entities is dropped —
there's no device to attach it to. The same three diagnostic entities used for this
bridge's own device are created on *their* device the first time metadata is seen, then
updated in place on every redelivery, and removed once that bridge's last entity is
removed (§5b). Own metadata arriving back via the broker (any subscriber to
`bridge/+/metadata` receives its own retained publish) is dropped by a loop guard
comparing the topic's `bridge_id` against this instance's own slug.

**Remote device's displayed firmware:** a remote bridge's native device (§5a) gets its
`sw_version` from exactly one place — the diagnostic entities above, set to
`"{integration_version} (protocol v{protocol_version})"`. `BridgedSensorEntity` does
**not** set `sw_version`, even though the incoming discovery payload's
`device.sw_version` field is present — that field is always the wire protocol's fixed
legacy constant (§3's `SW_VERSION`, `"1.0.3"`), not a peer's actual release version.
HA's device registry keeps whichever entity most recently supplied `sw_version`, and
ordinary discovery fires far more often than metadata, so if `BridgedSensorEntity` also
set it, `"1.0.3"` would win almost every time and permanently hide the real version. A
bridge with no metadata yet (a blueprint-based peer, which never sends §9 at all, or a
Saulach peer whose first `time_pattern` tick hasn't landed) simply shows no firmware
version rather than the misleading constant.
