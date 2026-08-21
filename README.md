# Saulach Bridge

**Peer-to-peer entity federation for Home Assistant.**

**Version: 0.1** — pre-release, not yet tested against a real broker.

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

## Status

This is Phase 1b of the migration plan: a behavior-preserving port of the
blueprint's protocol, plus native entity creation for entities received
from other bridges pulled forward from the plan's Phase 3. It has a
lightweight fake-Home-Assistant test suite (`tests/`) but has **not yet
been run against a real Home Assistant instance or a real MQTT broker**.
Don't point it at production yet.

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
| Shared discovery prefix | `share/homeassistant/` | The federation prefix all bridge instances publish to and read from |
| Sensor value prefix | `share/jakob/` | Where this instance publishes its own entities' state values |
| Full republish interval (minutes) | `1` | How often to refresh all retained messages |

## Services

`saulach.republish` — forces an immediate full discovery + state
republish for a given bridge instance, without waiting for the next
scheduled interval.

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
