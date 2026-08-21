"""Minimal fake of homeassistant.components.sensor.SensorEntity — just the
attributes/methods custom_components/saulach/sensor.py uses.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from homeassistant.const import EntityCategory


class SensorDeviceClass(StrEnum):
    """Not the real, exhaustive enum -- just enough real members for tests
    to exercise the issue #13 device_class filtering against something
    real-shaped rather than a hand-invented list. Deliberately includes
    "moisture" alongside the domain-only-safe members: in real HA it's a
    legitimate SensorDeviceClass name (numeric, e.g. soil moisture %) that
    ALSO happens to be a valid BinarySensorDeviceClass name (boolean,
    "wet"/"dry") -- exactly the name-matches-but-domain-doesn't case that
    device_class-name filtering alone can't catch (see legacy_discovery.py's
    module comment). Deliberately excludes true binary_sensor-only values
    like "light"/"motion", which name-filtering alone does catch."""

    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    PRESSURE = "pressure"
    POWER = "power"
    ENERGY = "energy"
    CURRENT = "current"
    VOLTAGE = "voltage"
    ILLUMINANCE = "illuminance"
    MOISTURE = "moisture"


class SensorEntity:
    _attr_should_poll: bool = True
    _attr_unique_id: str | None = None
    _attr_name: str | None = None
    _attr_device_class: str | None = None
    _attr_native_unit_of_measurement: str | None = None
    _attr_native_value: str | None = None
    _attr_device_info: dict | None = None
    _attr_entity_category: EntityCategory | None = None

    hass: Any = None
    entity_id: str | None = None

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def native_value(self) -> str | None:
        return self._attr_native_value

    async def async_added_to_hass(self) -> None:
        pass

    async def async_will_remove_from_hass(self) -> None:
        pass

    async def async_remove(self) -> None:
        """Minimal fake of Entity.async_remove() -- unregister from
        hass.states and mark this entity as no longer attached."""
        await self.async_will_remove_from_hass()
        if self.hass is not None and self.entity_id is not None:
            self.hass.states.async_remove(self.entity_id)
        self.entity_id = None
        self.hass = None

    def async_write_ha_state(self) -> None:
        if self.hass is None or self.entity_id is None:
            return
        self.hass.states.async_set(
            self.entity_id,
            "" if self._attr_native_value is None else str(self._attr_native_value),
            {
                "friendly_name": self._attr_name,
                "device_class": self._attr_device_class,
                "unit_of_measurement": self._attr_native_unit_of_measurement,
            },
        )
