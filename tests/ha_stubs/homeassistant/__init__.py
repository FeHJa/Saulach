"""A minimal, hand-written fake of Home Assistant's Python surface.

This is NOT Home Assistant, and it is only ever imported from tests/ (see
conftest.py) via a sys.path prepend. It exists because this project's dev
sandbox cannot install a real `homeassistant` package matching the 2026.7
core floor (see requirements_test.txt for details/history). It implements
just enough of the state machine, event bus, service registry, config
entries and MQTT client surface for custom_components/saulach to
run against in tests, with test-only helpers to drive it
(async_fire_time_changed, async_fire_mqtt_message, etc).

Treat tests that pass against this stub as "our code's control flow does
what we intended", not as proof it behaves correctly against real Home
Assistant core — that's covered by production use across multiple
federated instances instead of a pytest-homeassistant-custom-component
layer, a deliberate choice (see MIGRATION_PLAN.md's "Engineering notes").

Known, named gap (issue #13): this stub's `async_add_executor_job` just
calls the target inline -- it does not model real HA's event-loop
blocking-call detection, which will crash/hang a config entry if any
synchronous file/network I/O runs directly on the loop (e.g. in a
constructor called from `async_setup_entry`). A test suite passing here
does not prove the absence of that bug; it must be caught by code review
(any blocking call belongs behind `hass.async_add_executor_job`).
"""
