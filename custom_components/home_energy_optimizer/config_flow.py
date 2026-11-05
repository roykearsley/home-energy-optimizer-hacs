"""Config flow for Home Energy Optimizer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries

from .const import (
    DOMAIN,
    CONF_PANEL_KWP,
    CONF_TILT,
    CONF_AZIMUTH,
    CONF_PERFORMANCE_RATIO,
    CONF_BATTERY_CAPACITY,
    CONF_DISCHARGE_KW,
    CONF_RESERVE_SOC,
    CONF_EXPORT_END,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_POWER_ENTITY,
    CONF_OCTOPUS_DISPATCH_ENTITY,
    CONF_HANCHU_SERIAL,
    CONF_DAILY_PV_ENTITY,
    DEFAULT_PANEL_KWP,
    DEFAULT_TILT,
    DEFAULT_AZIMUTH,
    DEFAULT_PERFORMANCE_RATIO,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_DISCHARGE_KW,
    DEFAULT_RESERVE_SOC,
    DEFAULT_EXPORT_END,
    DEFAULT_SOC_ENTITY,
    DEFAULT_POWER_ENTITY,
    DEFAULT_DISPATCH_ENTITY,
    DEFAULT_HANCHU_SERIAL,
    DEFAULT_DAILY_PV_ENTITY,
    CONF_DAILY_EV_ENTITY,
    DEFAULT_DAILY_EV_ENTITY,
    CONF_ELECTRICITY_CONSUMPTION_ENTITY, DEFAULT_ELECTRICITY_CONSUMPTION_ENTITY,
    CONF_GAS_CONSUMPTION_ENTITY, DEFAULT_GAS_CONSUMPTION_ENTITY,
    CONF_EXPORT_RATE, DEFAULT_EXPORT_RATE,
)

# Only active settings are retained when an existing entry is reconfigured.
_CONFIG_KEYS = {
    CONF_PANEL_KWP, CONF_TILT, CONF_AZIMUTH, CONF_PERFORMANCE_RATIO,
    CONF_BATTERY_CAPACITY, CONF_DISCHARGE_KW, CONF_RESERVE_SOC,
    CONF_EXPORT_END, CONF_BATTERY_SOC_ENTITY, CONF_BATTERY_POWER_ENTITY,
    CONF_OCTOPUS_DISPATCH_ENTITY, CONF_HANCHU_SERIAL,
    CONF_DAILY_PV_ENTITY, CONF_DAILY_EV_ENTITY,
    CONF_GAS_CONSUMPTION_ENTITY, CONF_ELECTRICITY_CONSUMPTION_ENTITY, CONF_EXPORT_RATE,
}


def _schema(current: dict[str, Any] | None = None) -> vol.Schema:
    """Build config/reconfigure schema using current values when available."""
    current = current or {}

    return vol.Schema(
        {
            vol.Required(
                CONF_PANEL_KWP,
                default=current.get(CONF_PANEL_KWP, DEFAULT_PANEL_KWP),
            ): vol.Coerce(float),
            vol.Required(
                CONF_TILT,
                default=current.get(CONF_TILT, DEFAULT_TILT),
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
            vol.Required(
                CONF_AZIMUTH,
                default=current.get(CONF_AZIMUTH, DEFAULT_AZIMUTH),
            ): vol.All(vol.Coerce(float), vol.Range(min=-180, max=180)),
            vol.Required(
                CONF_PERFORMANCE_RATIO,
                default=current.get(
                    CONF_PERFORMANCE_RATIO, DEFAULT_PERFORMANCE_RATIO
                ),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
            vol.Required(
                CONF_BATTERY_CAPACITY,
                default=current.get(
                    CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY
                ),
            ): vol.All(vol.Coerce(float), vol.Range(min=1)),
            vol.Required(
                CONF_DISCHARGE_KW,
                default=current.get(CONF_DISCHARGE_KW, DEFAULT_DISCHARGE_KW),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
            vol.Required(
                CONF_RESERVE_SOC,
                default=current.get(CONF_RESERVE_SOC, DEFAULT_RESERVE_SOC),
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
            vol.Required(
                CONF_EXPORT_END,
                default=current.get(CONF_EXPORT_END, DEFAULT_EXPORT_END),
            ): str,
            vol.Required(
                CONF_BATTERY_SOC_ENTITY,
                default=current.get(CONF_BATTERY_SOC_ENTITY, DEFAULT_SOC_ENTITY),
            ): str,
            vol.Required(
                CONF_BATTERY_POWER_ENTITY,
                default=current.get(CONF_BATTERY_POWER_ENTITY, DEFAULT_POWER_ENTITY),
            ): str,
            vol.Required(
                CONF_OCTOPUS_DISPATCH_ENTITY,
                default=current.get(
                    CONF_OCTOPUS_DISPATCH_ENTITY, DEFAULT_DISPATCH_ENTITY
                ),
            ): str,
            vol.Required(
                CONF_HANCHU_SERIAL,
                default=current.get(CONF_HANCHU_SERIAL, DEFAULT_HANCHU_SERIAL),
            ): str,
            vol.Required(
                CONF_DAILY_PV_ENTITY,
                default=current.get(CONF_DAILY_PV_ENTITY, DEFAULT_DAILY_PV_ENTITY),
            ): str,
            vol.Required(
                CONF_DAILY_EV_ENTITY,
                default=current.get(CONF_DAILY_EV_ENTITY, DEFAULT_DAILY_EV_ENTITY),
            ): str,
            vol.Required(CONF_ELECTRICITY_CONSUMPTION_ENTITY, default=current.get(CONF_ELECTRICITY_CONSUMPTION_ENTITY, DEFAULT_ELECTRICITY_CONSUMPTION_ENTITY)): vol.All(str, vol.Length(min=1)),
            vol.Required(CONF_GAS_CONSUMPTION_ENTITY, default=current.get(CONF_GAS_CONSUMPTION_ENTITY, DEFAULT_GAS_CONSUMPTION_ENTITY)): vol.All(str, vol.Length(min=1)),
            vol.Required(CONF_EXPORT_RATE, default=current.get(CONF_EXPORT_RATE, DEFAULT_EXPORT_RATE)): vol.All(vol.Coerce(float), vol.Range(min=0)),
        }
    )


class HomeEnergyOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Home Energy Optimizer configuration."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle initial configuration."""
        if user_input is not None:
            await self.async_set_unique_id("home_energy_optimizer")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Home Energy Optimizer",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ):
        """Allow an existing Home Energy Optimizer entry to be reconfigured."""
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            await self.async_set_unique_id("home_energy_optimizer")
            self._abort_if_unique_id_mismatch()

            return self.async_update_reload_and_abort(
                entry,
                data_updates={**{k: v for k, v in entry.data.items() if k in _CONFIG_KEYS}, **user_input},
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(dict(entry.data)),
        )
