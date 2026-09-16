# Saulach Bridge

![Saulach Bridge](custom_components/saulach/brand/logo.png)

**Peer-to-peer entity federation for Home Assistant.**

**Version: 0.1.10** — in active production use, federating multiple real
Home Assistant instances over a real MQTT broker.

A native Home Assistant custom integration that bridges entities between
independent Home Assistant instances over MQTT. It's a port of the
[HA-Blueprint-MQTT-Bridge](https://github.com/FeHJa/HA-Blueprint-MQTT-Bridge)
YAML automation blueprint, reproducing its wire behavior exactly so it
interoperates with instances still running the original blueprint.

## What it does

- Publishes your chosen local entities' state onto a shared MQTT prefix,
  in an MQTT-Discovery-style JSON payload, so other Home Assistant
  instances watching that prefix can pick them up.
- Subscribes to that same shared prefix and, for entities published by
  *other* Saulach instances, creates and maintains native sensor
  entities directly in this Home Assistant install — no writes into the
  local MQTT Discovery root, and no orphaned entities left behind:
  removing the integration removes them too.
- Refreshes everything periodically (configurable interval) so retained
  messages stay current after a restart, plus an on-demand `republish`
  service for the same thing on demand.

See [`PROTOCOL.md`](PROTOCOL.md) for the exact wire contract this
implements, and [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) for the phased
rollout this integration is being built against.

## Prerequisites

This integration only talks to *its own* Home Assistant instance's MQTT
broker — it has no way to reach another instance's broker directly. For
federation to actually work, every participating instance's broker needs
to be bridged/federated with the others at the broker level, so that
publishes under the shared topic prefix (e.g. `share/`) actually replicate
between brokers. With Mosquitto, for example, that means a
[bridge connection](https://mosquitto.org/documentation/mosquitto-conf/)
forwarding both directions on that prefix (e.g. `topic share/# both 0`) —
consult your broker's documentation for the equivalent. If every
participating instance already publishes to the same single physical
broker, there's nothing to set up here.

## Status

Phase 1 + 1b (behavior-preserving blueprint port, plus native entity
creation for received entities pulled forward from Phase 3) are complete
and validated in real-world use: multiple independent Home Assistant
installs, some still running the original blueprint automation, federate
over a shared broker today without behavior changes on the blueprint side.
Several real issues surfaced this way and were fixed (blocking I/O on
setup, `device_class` mismatches crashing receivers, entity/device cleanup
gaps, crashes on a bridged entity's source going unavailable, stale
retained state values replayed on reconnect) — see `MIGRATION_PLAN.md`'s
Decisions section and `PROTOCOL.md`'s §4/§5b/§5c/§9 amendments for the
details.

Acceptance testing happens by running this in production and fixing what
breaks, rather than through a `pytest-homeassistant-custom-component`-based
integration-test layer or a CI pipeline — both considered and declined,
see `MIGRATION_PLAN.md` Decision 9.

## Installation

### Via HACS (custom repository)

Not in HACS's default store yet, so it has to be added manually as a
custom repository:

1. HACS → the "⋮" menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/FeHJa/Saulach`, category:
   **Integration**.
3. Install "Saulach Bridge" from HACS, then restart Home Assistant.
4. Settings → Devices & Services → Add Integration → search for
   "Saulach Bridge".

### Manually

1. Copy `custom_components/saulach/` into your Home Assistant config's
   `custom_components/` directory.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → search for
   "Saulach Bridge".

Requires Home Assistant 2026.7 or newer and a configured MQTT integration.

The integration ships its own icon (`custom_components/saulach/brand/`),
picked up automatically by Home Assistant's local brand-icon mechanism
(2026.3+) — no separate submission to the `home-assistant/brands` repo
needed.

## Configuration

Set up via the UI config flow:

| Field | Default | Purpose |
|---|---|---|
| Bridge name | `Bridge Jakob` | Human-readable name; slugified into this bridge's identifier |
| Entities to bridge | — | The entities to publish, any domain |
| Shared discovery prefix | `share/homeassistant/` | The federation prefix all bridge instances publish to and read from — must be bridged between brokers, see [Prerequisites](#prerequisites) |
| Sensor value prefix | `share/jakob/` | Where this instance publishes its own entities' state values |
| Full republish interval (minutes) | `1` | How often to refresh all retained messages |

## Services

- `saulach.republish` — forces an immediate full discovery + state
  republish for a given bridge instance, without waiting for the next
  scheduled interval.
- `saulach.depublish_bridge` — permanently retracts every entity shown for
  a peer bridge you've confirmed is dead (decommissioned, migrated, etc.),
  so it stops reappearing on every restart. Manual and explicit only
  (Developer Tools → Actions, device selector) — there's no automatic way
  to tell a dead peer from a merely quiet one. See `PROTOCOL.md` §5c.

## Development

```
pip install -r requirements_test.txt
pytest
```

Tests run against a small hand-written fake of Home Assistant
(`tests/ha_stubs/`), not the real `homeassistant` package — see that
package's docstring and `requirements_test.txt` for why, and what that
does and doesn't validate.

## License

[MIT](LICENSE)
