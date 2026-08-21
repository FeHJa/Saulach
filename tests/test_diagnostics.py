"""diagnostics.py tests (issue #12) -- the "Download Diagnostics" data,
exercised through the real async_setup_entry so last_metadata reflects
what BridgeScheduler actually published, not a hand-built double.
"""

import asyncio

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components import saulach
from custom_components.saulach import scheduler as scheduler_module
from custom_components.saulach.const import (
    CONF_BRIDGE_NAME,
    CONF_ENTITIES,
    CONF_SENSOR_VALUE_PREFIX,
    CONF_SHARED_DISCOVERY_PREFIX,
    CONF_TIME_PATTERN_MINUTES,
)
from custom_components.saulach.diagnostics import async_get_config_entry_diagnostics


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch):
    monkeypatch.setattr(scheduler_module.random, "uniform", lambda a, b: 0)


def _run(coro):
    return asyncio.run(coro)


async def _drain_hass_tasks(hass: HomeAssistant) -> None:
    while hass._tasks:
        await asyncio.gather(*list(hass._tasks), return_exceptions=True)


def _make_entry(entities: list[str]) -> ConfigEntry:
    return ConfigEntry(
        entry_id="entry1",
        data={
            CONF_ENTITIES: entities,
            CONF_SHARED_DISCOVERY_PREFIX: "share/homeassistant/",
            CONF_SENSOR_VALUE_PREFIX: "share/jakob/",
            CONF_BRIDGE_NAME: "Bridge Jakob",
        },
        options={CONF_TIME_PATTERN_MINUTES: 1},
    )


def test_diagnostics_includes_entities_and_last_metadata():
    hass = HomeAssistant()
    entry = _make_entry(["sensor.a", "sensor.b"])
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")

    async def scenario():
        await saulach.async_setup_entry(hass, entry)
        await _drain_hass_tasks(hass)
        return await async_get_config_entry_diagnostics(hass, entry)

    diagnostics = _run(scenario())

    assert diagnostics["entities"] == ["sensor.a", "sensor.b"]
    assert diagnostics["last_metadata"]["entity_count"] == 2
    assert diagnostics["last_metadata"]["bridge_id"] == "bridge_jakob"


def test_diagnostics_is_none_safe_before_setup():
    entry = _make_entry(["sensor.a"])  # entry.runtime_data never set
    hass = HomeAssistant()

    diagnostics = _run(async_get_config_entry_diagnostics(hass, entry))

    assert diagnostics == {"entities": ["sensor.a"], "last_metadata": None}
