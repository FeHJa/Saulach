# Wire Protocol Contract (reverse-engineered from the blueprint)

Source: https://github.com/FeHJa/HA-Blueprint-MQTT-Bridge/blob/main/mqtt_bridge.yaml

This is the exact behavior Phase 1 of the integration must reproduce. Treat every detail
here as intentional/required unless explicitly marked "known limitation."

## 1. Configuration inputs (blueprint inputs → become config_entry data/options)

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
- Own state value → `{sensor_value_prefix}sensor/{object_id}` (retained)
- Forwarded remote discovery → `{local_discovery_prefix}/{component}/{object_id}/config` (retained)
  — **this is the blueprint's behavior; this integration does not do this, see §5a**
- `object_id` = `entity_id.split('.')[-1]` (domain stripped)
- `component` / `object_id` for forwarding are parsed positionally from the incoming topic,
  at the position right after the shared prefix

**Known limitation (do not fix in Phase 1):** `object_id` excludes the domain, so two
entities in different domains sharing an object_id (e.g. `sensor.garage` and
`binary_sensor.garage`) collide on the same topic — the retained message from whichever
publishes last wins. This happens upstream, on the *origin* bridge's own publish path, so
it isn't affected by §5a's change to how a *receiving* instance materializes incoming
messages — a native entity built from a colliding payload is just as last-write-wins as a
forwarded discovery message would have been.

**Known limitation (do not fix in Phase 1):** the discovery *component* segment for own
entities is hardcoded to `sensor` regardless of the source entity's actual domain. A
bridged `binary_sensor` or `input_boolean` is published as a generic MQTT `sensor`, not as
its native discovery type. Also unaffected by §5a: a receiving instance only ever sees
`component: sensor` in what it gets, native-entity or not.

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

`protocol_version` is a new field, not present in the original blueprint's
payload. It is safe to add: other instances already tolerate the
non-standard `bridge_id` key today (they ignore unknown JSON keys), so one
more integer field does not break their forwarding or loop-prevention
logic. See §8 for why it's there.

### device_class / unit_of_measurement resolution order

1. Use the source entity's actual `device_class` / `unit_of_measurement` attribute if present.
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

**Amendment (issue #13):** step 1 only uses the source entity's actual `device_class` if
the source entity's real domain is also `sensor`. Since the `component` segment is
hardcoded to `sensor` regardless of the source entity's real domain (the known limitation
just above), any other domain's `device_class` is *not* forwarded verbatim; step 1 is
skipped for it and resolution falls through to step 2/3 as if the entity had no
`device_class` at all. This is a domain check, not a device_class-name check, for two
distinct failure modes seen in practice:
- Some names (`light`, `motion`, ...) are only valid for `binary_sensor`, not `sensor` at
  all — forwarding them unfiltered made a receiver's own `mqtt` integration reject the
  entire discovery message outright (invalid enum value).
- Some names (`moisture`, `battery`, `power`, ...) are valid `SensorDeviceClass` members
  *and* valid `BinarySensorDeviceClass` members, with different value semantics — numeric
  for `sensor`, boolean `on`/`off` for `binary_sensor`. A device_class-name check alone
  lets these through since the name is legitimate; a `binary_sensor`'s `on`/`off` state
  then crashes HA's own numeric coercion on the receiving side. Only a domain check catches
  this, since there's no device_class name that's safe to forward from a non-`sensor`
  source.

Either way, omitting the field is safe — a receiver still gets a working, if less
specific, entity.

## 4. State payload

Raw state string only (no JSON wrapping), published retained to the state topic. Uses
`trigger.to_state.state` on state-triggered publishes (cheaper than re-reading `states()`).

## 5. Incoming discovery handling (federation from other instances)

- Subscribe to `{shared_discovery_prefix}+/+/config` (the blueprint hardcodes this as a
  literal string rather than substituting the configured prefix — a blueprint-engine
  limitation, not a protocol requirement; the integration should subscribe using the
  actually configured `shared_discovery_prefix`)
- On message: parse `component` / `object_id` from the topic (see §2), forward the
  **payload verbatim, unchanged bytes**, to `{local_discovery_prefix}/{component}/{object_id}/config`,
  retained
- **Loop prevention (must be preserved exactly):** skip forwarding if
  `payload.bridge_id == own slug_bridge_name`, OR `payload.unique_id` starts with
  `"{slug_bridge_name}::"` or `"{slug_bridge_name}."`. The other two instances rely on
  recognizing this bridge_id/unique_id prefix convention to avoid re-forwarding your own
  messages back to you — if this logic isn't preserved exactly, expect forwarding loops.

## 5a. Amendment: local materialization via native entities

**Status: implemented, supersedes the local-forwarding requirement in §5.**
Everything else in §5 — subscribing with the configured `shared_discovery_prefix`,
parsing `component`/`object_id`, and the loop-prevention guard — is unchanged and still
required exactly as written. What changes is only the last step: instead of forwarding
the verbatim payload to `{local_discovery_prefix}/{component}/{object_id}/config` for
Home Assistant's built-in `mqtt` integration to discover, this integration parses the
payload itself and creates or updates a native entity directly, through its own entity
platform, keyed by the payload's `unique_id`.

**Why this is safe to do without coordinating with the other two bridge instances**
(unlike the §8 Phase 3 redesign): this is purely a receiving-side, local decision. What a
bridge does with a message *after* the loop-guard check is never observable by the
instance that sent it — nothing about it is re-published onto the shared prefix. The
wire protocol, and every other instance's view of this bridge, is byte-for-byte
identical to before.

**What this fixes:** entities created this way are owned by this integration's config
entry, so removing the integration removes them automatically via Home Assistant's
standard config-entry cleanup — no separate depublish step needed for the receiving
side. It also means this integration no longer writes anything into
`local_discovery_prefix` (`homeassistant/` by default) for federated entities, removing
the collision risk with Zigbee2MQTT/ESPHome/Tasmota discovery that motivated the §8
redesign in the first place — for the receiving side, today, without waiting for Phase 3.

**What this does *not* fix:** neither of the two §2 known limitations (object_id/domain
collision, hardcoded `sensor` component) — both originate on the far side, in what the
*sending* bridge publishes, before this instance ever sees the message.

**Amendment (issue #13 continued):** materializing a native entity here means this
instance is just as exposed to the §3 device_class amendment's failure mode as the
sending side is — a payload's `device_class` is only safe to apply if it actually came
from a `sensor`-domain entity, and nothing about the wire format stops a peer (a
not-yet-updated Grapevine instance, or any other implementation of this protocol) from
sending an unsafe one. Rather than trust the payload, `RemoteEntityManager` recovers the
source entity's real domain from the payload's own `unique_id` (`{slug_bridge_name}::
{entity_id}`, §3) and applies the same domain check before setting it on the native
entity — for both entity creation and update-in-place on redelivery. A `unique_id` that
doesn't match this convention at all is treated as unsafe (device_class dropped) rather
than guessed at.

`local_discovery_prefix` remains listed in §1 as a historical note (it's still what the
*blueprint* does, and still relevant if you're comparing against another instance running
the blueprint unmodified) but is no longer part of this integration's config — see
MIGRATION_PLAN.md's Phase 1b for the implementation.

## 5b. Amendment: depublishing own entities (removal signal)

**Status: implemented (issue #7).** Closes the gap §5a left open: cleanup of *this*
bridge's own entities as seen by *other* instances (§3's outbound side).

When an entity stops being bridged — dropped from the config entry's entity list via
reconfigure, or the whole config entry removed — this instance publishes an **empty
retained payload** to that entity's own discovery topic
(`{shared_discovery_prefix}sensor/{object_id}/config`) and state topic
(`{sensor_value_prefix}sensor/{object_id}`). An empty retained payload on a discovery
config topic is the standard MQTT Discovery removal convention, so this requires no
protocol negotiation: Home Assistant's own `mqtt` integration already treats it as
"remove this entity," which means **blueprint-based receivers get this for free** —
their forwarding step relays the empty payload into their local discovery root exactly
like any other payload, and their local `mqtt` integration does the rest.

Grapevine-based receivers materialize entities natively instead (§5a), so they don't go
through the `mqtt` integration's removal handling — `RemoteEntityManager` is taught to
recognize an empty payload on a topic it previously saw a real payload on (correlated by
*topic*, not `unique_id`, since an empty payload carries no JSON to read) and remove the
native entity it created for it.

## 5c. Amendment: manual depublish of a confirmed-dead peer bridge

**Status: implemented (issue #12 follow-up).** §5b's removal signal only reaches a
receiver if it's online and subscribed at the exact moment the empty retained payload is
published. MQTT retains a topic's last payload forever otherwise — a fresh subscribe (e.g.
every Grapevine restart) redelivers whatever was last retained, indistinguishable from a
live republish. If the empty payload was never delivered live (receiver offline, or
whoever decommissioned the peer only cleared the broker's retained store without a client
actually online to publish-and-be-delivered), the peer's entities keep reappearing on
every restart even though the source is long gone — no amount of waiting fixes this, since
nothing is left to eventually send the missing removal signal.

There is no automatic detection for this: MQTT gives a receiver no reliable signal that a
given peer is dead rather than merely quiet between publishes (unlike a state-change or
time-pattern republish, silence isn't itself an event). Deciding a specific bridge is dead
is deliberately left to a human, the same way the person clearing this conversation's six
example bridges did — by external knowledge (migrated, decommissioned, "yes that's still
running blueprint"), not by any timeout Grapevine could apply on its own.

Given that human decision, `grapevine.depublish_bridge` (a service, not automatic) takes a
Grapevine bridge device the user names explicitly. For every entity this instance
currently has materialized from that device's `bridge_id`, it:
- publishes an empty retained payload to that entity's own discovery topic (the *same*
  topic, and the *same* removal convention, as §5b — a receiving Grapevine or
  blueprint-based instance elsewhere on the shared prefix cannot tell this apart from the
  origin bridge's own depublish), and
- immediately tears the entity down locally via the existing §5b removal path, rather
  than waiting for its own publish to loop back over MQTT.

Publishing to a topic this instance didn't originate is unusual but not a protocol
violation — the shared prefix has no per-topic ownership model, and §5b's removal
convention only cares that an empty retained payload arrived, not who sent it. Because the
broker's retained store is actually cleared this time (not merely cleared-without-anyone-
subscribed, or never cleared at all), the peer stops reappearing on future restarts too —
unlike a plain local entity deletion, which would only hide it until the next restart
redelivers the same stale retained messages.

- State-change on any bridged entity → publish discovery + state for that one entity
- Time-pattern trigger (every `time_pattern` minutes) → full republish loop over all bridged
  entities (discovery + state) — this is the resync-after-restart / retained-message-refresh
  mechanism
- On-demand full republish (was a custom HA event `force_republish_sensors` in the
  blueprint) → same as above
- Incoming MQTT discovery on the shared prefix → forwarding logic (§5)
- **Jitter:** before any discovery/state publish, a random 0–9 second delay is applied.
  This desyncs near-simultaneous publishes from multiple instances hitting the broker at
  the same moment (three instances all firing on the same time-pattern minute mark would
  otherwise collide). Preserve this or a functionally equivalent spread mechanism.
- Original automation ran with `mode: parallel, max: 50` — relevant because bursts of state
  changes across many bridged entities can produce many simultaneous publishes.

## 7. Deliberately dropped feature

The blueprint name is "...(stable, no availability)" — availability/LWT tracking existed at
some point and was removed for stability. Check the source repo's commit history for why
before reintroducing this in Phase 3.

## 8. Forward compatibility: Phase 3 target design (named, not implemented)

Phase 1 reproduces the blueprint's MQTT-Discovery-emulation protocol exactly, as specified
above, with no wire changes. However, a target design for a future protocol generation has
been identified and is documented here so it doesn't need to be rediscovered later. It is
**not implemented in Phase 1 or Phase 2**, and is gated on coordinating a rollout with the
other two bridge instances — do not build it unprompted.

**Problem it solves:** the current protocol emulates MQTT Discovery by writing into
`local_discovery_prefix` (`homeassistant/` by default) — a namespace shared with
Zigbee2MQTT/ESPHome/Tasmota discovery. Combined with the §2 object_id/domain collision
known limitation, this is a real risk of a bridged entity colliding with, or being
overwritten by, an unrelated device's discovery message.

**Target design:** each bridge instance publishes its own retained JSON "manifest" —
a list of `{bridge_id, object_id, domain, name, device_class, unit, state_topic}` per
bridged entity — under a dedicated, bridge-only topic tree:
`ha_bridge/{bridge_id}/manifest`. Other instances subscribe only to the manifests of
bridges they explicitly opt into (a config-flow "follow list", not a blanket subscribe to
everything on the shared prefix). Each instance diffs the manifest against the native HA
entities it has already instantiated for that remote bridge, and creates/removes native
entities directly through its own entity platform. There is no MQTT Discovery emulation
in this design, no writes to the local discovery root, and no forwarding/echo-prevention
logic (§5) — a bridge just reads its followed peers' manifests and reconciles entities
against them. As a side effect, this eliminates both §2 known limitations: entities carry
their real domain and no longer collide on a shared `sensor/{object_id}` topic.

**Migration path — `protocol_version`:** every own-payload JSON this integration
publishes carries a `protocol_version` integer field (§3; Phase 1 sets it to `1`). Once a
future manifest-based payload exists, it will carry its own `protocol_version` (2+). This
lets any instance inspect `protocol_version` per bridge partner on incoming messages and
decide, per partner, whether to speak the legacy discovery protocol or the manifest
protocol — enabling a gradual, partner-by-partner rollout instead of a synchronized
cutover across all three instances on one day.

See `MIGRATION_PLAN.md` for the internal `ProtocolAdapter` abstraction that keeps Phase 1's
implementation swappable when this design is eventually built.

**Relationship to §5a:** §5a already brought the *local materialization* half of this
design forward — incoming messages become native entities, not forwarded discovery. What
Phase 3 still owns exclusively is the *outbound* half (own entities as a manifest instead
of MQTT Discovery emulation) and the follow-list/opt-in subscription model — both
wire-protocol changes requiring the cross-instance coordination described above. When
Phase 3 lands, its manifest-diffing logic is expected to feed the same entity
materialization layer §5a introduced, rather than building a second one.

## 9. Metadata message (issue #12)

**Status: implemented.** A small, additive side-channel alongside the §2-§5 protocol —
each bridge periodically publishes a retained JSON message describing itself: protocol
version, this integration's own release version, the local Home Assistant version, how
many entities it's currently bridging, and a last-heartbeat timestamp.

**Topic:** `{shared_discovery_prefix}bridge/{slug_bridge_name}/metadata`, retained.

**Payload:**

```json
{
  "protocol_version": 1,
  "integration_version": "0.1.3",
  "bridge_id": "bridge_jakob",
  "ha_version": "2026.8.0",
  "entity_count": 7,
  "last_heartbeat": "2026-08-06T08:14:00+00:00"
}
```

`last_heartbeat` is a plain "as of this publish" UTC timestamp, refreshed every
publish — it carries no online/offline or staleness inference. Availability tracking is
the feature §7 says was deliberately dropped from the original blueprint for stability,
and stays out of scope here; a future change that wants it needs its own design pass.

**Why this needs no coordination with the other two bridge instances** (unlike §8's
Phase 3 redesign): the topic has three segments ending in `metadata`, so it never matches
`{shared_discovery_prefix}+/+/config` — the two-segment, `config`-suffixed pattern every
receiver (blueprint or Grapevine) subscribes to today (§5). Nobody's subscription sees
this message unless they deliberately opt into it in the future, so publishing it
unilaterally changes nothing about how any existing receiver behaves — the same safety
argument §5a used for native materialization.

**Timing:** published on the same `time_pattern` tick as the full discovery/state
republish (§6) — at startup, on the periodic clock-aligned trigger, and on-demand via the
`republish` service — rather than a second, separately configurable interval. Subject to
the same 0-9s jitter as any other publish.

**Local surfacing (own bridge): reverted.** An earlier version of this amendment also
surfaced this bridge's own metadata locally — a device representing "this bridge
instance" with three `entity_category: diagnostic` entities (entity count, last
heartbeat, HA version). In practice this just added a device with no sensors on it to
every Grapevine install, cluttering the integration's device list without adding
information the user didn't already have some other way (they're the one running this
instance). Removed per user feedback; this bridge's own metadata is now wire-only —
still published every tick as documented above, still available via Home Assistant's
"Download Diagnostics" for support requests (`diagnostics.py` reads the same
`last_metadata` the scheduler already tracks), just not materialized as a local device or
entities. **Remote** bridges' metadata (next amendment) is unaffected by this — that's a
materially different situation, since a remote bridge's device is something the user
doesn't otherwise have visibility into locally.

**Amendment: consuming other bridges' metadata (issue #12 follow-up).** Status:
implemented. `LegacyDiscoveryAdapter` also subscribes to
`{shared_discovery_prefix}bridge/+/metadata`. Deliberately *not* a follow-list/opt-in
mechanism (§8's Phase 3 manifest is where that belongs) — eligibility instead falls out of
state this integration already has: a remote bridge's metadata is only ever shown if
`RemoteEntityManager` has already materialized at least one entity from that `bridge_id`
via §5a. If a metadata message arrives for a bridge we have no entities from, it's dropped
— there's no device to attach it to and no reason to create one from metadata alone. The
same three `entity_category: diagnostic` entities used for this bridge's own device are
created on *their* device the first time metadata is seen, then updated in place on every
redelivery. If that bridge's last entity is later removed (§5b), its diagnostic entities
are removed too, for the same reason they were created: nothing left to attach them to.
Own metadata arriving back via the broker (any client subscribed to
`bridge/+/metadata` receives its own retained publish) is dropped by a loop guard
comparing the topic's `bridge_id` against this instance's own slug — same shape as §5's
loop guard, simpler since there's no JSON `unique_id`/`bridge_id` prefix convention to
check, just an exact match.
