"""Thin, protocol-agnostic MQTT client wrapper.

Knows nothing about discovery payloads, forwarding, or loop prevention —
just talks to the broker via homeassistant.components.mqtt. Any current or
future ProtocolAdapter reuses this unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant


async def async_wait_for_mqtt_client(hass: HomeAssistant) -> bool:
    return await mqtt.async_wait_for_mqtt_client(hass)


async def async_publish(hass: HomeAssistant, topic: str, payload: str, *, retain: bool = True) -> None:
    await mqtt.async_publish(hass, topic, payload, qos=0, retain=retain)


async def async_subscribe(
    hass: HomeAssistant,
    topic: str,
    msg_callback: Callable[[mqtt.ReceiveMessage], Awaitable[None] | None],
) -> Callable[[], None]:
    return await mqtt.async_subscribe(hass, topic, msg_callback, qos=0)
