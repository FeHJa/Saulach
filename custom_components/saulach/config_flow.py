"""Config flow — maps the blueprint's inputs (PROTOCOL.md §1) onto a config
entry. Two flows: initial setup (async_step_user) and a single consolidated
"Configure" flow for everything editable afterwards -- entities, both
prefixes, bridge name, and the republish interval (SaulachOptionsFlow,
issue #7). These used to be split across "Configure" (options-only) and a
separate "Reconfigure" action, but that split wasn't discoverable in
practice -- see issue #7's reopening."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BRIDGE_NAME,
    CONF_ENTITIES,
    CONF_SENSOR_VALUE_PREFIX,
    CONF_SHARED_DISCOVERY_PREFIX,
    CONF_TIME_PATTERN_MINUTES,
    DEFAULT_BRIDGE_NAME,
    DEFAULT_SENSOR_VALUE_PREFIX,
    DEFAULT_SHARED_DISCOVERY_PREFIX,
    DEFAULT_TIME_PATTERN_MINUTES,
    DOMAIN,
)
from .discovery import normalize_prefix, slugify_bridge_name

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BRIDGE_NAME, default=DEFAULT_BRIDGE_NAME): str,
        vol.Required(CONF_ENTITIES): selector.EntitySelector(
            selector.EntitySelectorConfig(multiple=True)
        ),
        vol.Required(
            CONF_SHARED_DISCOVERY_PREFIX, default=DEFAULT_SHARED_DISCOVERY_PREFIX
        ): str,
        vol.Required(CONF_SENSOR_VALUE_PREFIX, default=DEFAULT_SENSOR_VALUE_PREFIX): str,
        vol.Required(
            CONF_TIME_PATTERN_MINUTES, default=DEFAULT_TIME_PATTERN_MINUTES
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
    }
)

TIME_PATTERN_SELECTOR = vol.All(vol.Coerce(int), vol.Range(min=1, max=60))


def _options_schema(current_data: dict[str, Any], current_options: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BRIDGE_NAME, default=current_data[CONF_BRIDGE_NAME]): str,
            vol.Required(
                CONF_ENTITIES, default=current_data[CONF_ENTITIES]
            ): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
            vol.Required(
                CONF_SHARED_DISCOVERY_PREFIX, default=current_data[CONF_SHARED_DISCOVERY_PREFIX]
            ): str,
            vol.Required(
                CONF_SENSOR_VALUE_PREFIX, default=current_data[CONF_SENSOR_VALUE_PREFIX]
            ): str,
            vol.Required(
                CONF_TIME_PATTERN_MINUTES,
                default=current_options.get(
                    CONF_TIME_PATTERN_MINUTES, DEFAULT_TIME_PATTERN_MINUTES
                ),
            ): TIME_PATTERN_SELECTOR,
        }
    )


class SaulachConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            slug_bridge_name = slugify_bridge_name(user_input[CONF_BRIDGE_NAME])
            # dict.fromkeys dedupes while preserving selection order -- the
            # entity picker shouldn't produce duplicates, but nothing stops
            # one from reaching here, and a duplicate means
            # BridgeScheduler.async_republish_all publishes the same
            # retained topic twice per tick.
            entities = list(dict.fromkeys(user_input[CONF_ENTITIES]))

            if not slug_bridge_name:
                errors["base"] = "invalid_bridge_name"
            elif not entities:
                errors["base"] = "no_entities"
            else:
                await self.async_set_unique_id(slug_bridge_name)
                self._abort_if_unique_id_configured()

                data = {
                    CONF_ENTITIES: entities,
                    CONF_SHARED_DISCOVERY_PREFIX: normalize_prefix(
                        user_input[CONF_SHARED_DISCOVERY_PREFIX]
                    ),
                    CONF_SENSOR_VALUE_PREFIX: normalize_prefix(
                        user_input[CONF_SENSOR_VALUE_PREFIX]
                    ),
                    CONF_BRIDGE_NAME: user_input[CONF_BRIDGE_NAME],
                }
                options = {CONF_TIME_PATTERN_MINUTES: user_input[CONF_TIME_PATTERN_MINUTES]}

                return self.async_create_entry(
                    title=user_input[CONF_BRIDGE_NAME], data=data, options=options
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SaulachOptionsFlow:
        return SaulachOptionsFlow()


class SaulachOptionsFlow(config_entries.OptionsFlow):
    """The single "Configure" flow for an existing entry -- entities, both
    prefixes, bridge name, and the republish interval all live here. Does
    not set self.config_entry in __init__: that's deprecated as of HA
    2025.12 in favor of the base class providing it automatically."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}

        if user_input is not None:
            slug_bridge_name = slugify_bridge_name(user_input[CONF_BRIDGE_NAME])
            # See async_step_user's comment: dedupe defensively, since a
            # duplicate entity_id makes async_republish_all double-publish
            # the same retained topic every tick.
            entities = list(dict.fromkeys(user_input[CONF_ENTITIES]))

            if not slug_bridge_name:
                errors["base"] = "invalid_bridge_name"
            elif not entities:
                errors["base"] = "no_entities"
            elif any(
                other.entry_id != entry.entry_id and other.unique_id == slug_bridge_name
                for other in self.hass.config_entries.async_entries(DOMAIN)
            ):
                # Renaming the bridge is allowed (it's the same case as
                # any other identity field changing) -- this only rejects
                # renaming *onto* a slug some other entry already owns.
                errors["base"] = "already_configured"
            else:
                # Depublish entities dropped from the list *before* the
                # update below triggers a reload, while we can still reach
                # the live adapter -- see PROTOCOL.md §5b and issue #7.
                # Renaming the bridge itself (slug change) is not handled
                # here: that would orphan every entity under the old slug
                # too, which is a separate, not-yet-decided piece of scope.
                removed_entities = set(entry.data[CONF_ENTITIES]) - set(entities)
                runtime_data = entry.runtime_data
                if runtime_data is not None:
                    for entity_id in removed_entities:
                        await runtime_data.protocol_adapter.async_depublish_entity(entity_id)

                new_data = {
                    CONF_ENTITIES: entities,
                    CONF_SHARED_DISCOVERY_PREFIX: normalize_prefix(
                        user_input[CONF_SHARED_DISCOVERY_PREFIX]
                    ),
                    CONF_SENSOR_VALUE_PREFIX: normalize_prefix(
                        user_input[CONF_SENSOR_VALUE_PREFIX]
                    ),
                    CONF_BRIDGE_NAME: user_input[CONF_BRIDGE_NAME],
                }
                new_options = {CONF_TIME_PATTERN_MINUTES: user_input[CONF_TIME_PATTERN_MINUTES]}
                # One update call covering data/title/unique_id/options --
                # triggers the update listener registered in __init__.py
                # once, which reloads the entry.
                self.hass.config_entries.async_update_entry(
                    entry,
                    title=user_input[CONF_BRIDGE_NAME],
                    data=new_data,
                    options=new_options,
                    unique_id=slug_bridge_name,
                )
                return self.async_create_entry(data=new_options)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(entry.data, entry.options),
            errors=errors,
        )
