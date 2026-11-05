"""Home Energy Optimizer integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import HomeEnergyCoordinator

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Home Energy Optimizer from a config entry."""
    coordinator = HomeEnergyCoordinator(hass, entry)
    await coordinator.async_load_pv_history()
    await coordinator.async_backfill_ev_history()
    await coordinator.async_backfill_battery_history()
    await coordinator.async_backfill_grid_history()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_live_tracking()
    coordinator.async_start_energy_tracking()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry."""
    coordinator = hass.data[DOMAIN].get(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        if coordinator is not None:
            coordinator.async_stop_live_tracking()
            coordinator.async_stop_energy_tracking()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
