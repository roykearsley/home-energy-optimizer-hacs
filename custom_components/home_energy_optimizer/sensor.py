"""Sensor entities for Home Energy Optimizer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HomeEnergyCoordinator


@dataclass(frozen=True, kw_only=True)
class HomeEnergySensorDescription(SensorEntityDescription):
    """Describe a Home Energy sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    name_fn: Callable[[dict[str, Any]], str | None] | None = None


def _forecast_day_value(data: dict[str, Any], index: int) -> Any:
    forecast = data.get("forecast", [])
    if len(forecast) <= index:
        return None

    # v1.1.23.6: Day 1 is today's official forecast. Once the immutable
    # 05:00 baseline has been captured, keep the rolling 7-day card aligned
    # with Solar Performance Today and the hourly projected profile instead
    # of exposing a later live Open-Meteo refresh for today's row. Days 2-7
    # intentionally remain live forecasts.
    if index == 0:
        baseline = data.get("solar_forecast_baseline_today_kwh")
        if baseline is not None:
            return baseline

    return forecast[index].get("forecast_kwh")


def _forecast_day_attrs(data: dict[str, Any], index: int) -> dict[str, Any]:
    forecast = data.get("forecast", [])
    if len(forecast) <= index:
        return {}
    row = forecast[index]
    try:
        full_date = datetime.fromisoformat(row["date"]).strftime("%A %d %B %Y")
    except (KeyError, TypeError, ValueError):
        full_date = row.get("date")
    return {
        "date": row.get("date"),
        "full_date": full_date,
        "avg_cloud_pct": row.get("avg_cloud_pct"),
    }


def _forecast_day_name(data: dict[str, Any], index: int) -> str | None:
    attrs = _forecast_day_attrs(data, index)
    full_date = attrs.get("full_date")
    return f"Solar Forecast {full_date}" if full_date else None



def _ev_day_value(data: dict[str, Any], index: int) -> Any:
    rows = data.get("ev_history", [])
    return rows[index].get("kwh") if len(rows) > index else None

def _ev_day_name(data: dict[str, Any], index: int) -> str | None:
    rows = data.get("ev_history", [])
    if len(rows) <= index:
        return f"EV Charging Day {index + 1}"
    try:
        return "EV Charging " + datetime.fromisoformat(rows[index]["date"]).strftime("%A %d %B %Y")
    except (KeyError, TypeError, ValueError):
        return f"EV Charging Day {index + 1}"

def _ev_day_attrs(data: dict[str, Any], index: int) -> dict[str, Any]:
    rows = data.get("ev_history", [])
    if len(rows) <= index:
        return {}
    return {"date": rows[index].get("date"), "days_ago": index}

def _battery_day_value(data: dict[str, Any], index: int) -> Any:
    rows = data.get("battery_history", [])
    if len(rows) <= index:
        return None
    row = rows[index]
    if row.get("status") == "In progress":
        return "In progress"
    value = row.get("efficiency_pct")
    return f"{value:.1f}%" if isinstance(value, (int, float)) else None


def _battery_day_name(data: dict[str, Any], index: int) -> str | None:
    rows = data.get("battery_history", [])
    if len(rows) <= index:
        return f"Battery Performance Day {index + 1}"
    try:
        return "Battery Performance " + datetime.fromisoformat(rows[index]["date"]).strftime("%A %d %B %Y")
    except (KeyError, TypeError, ValueError):
        return f"Battery Performance Day {index + 1}"


def _battery_day_attrs(data: dict[str, Any], index: int) -> dict[str, Any]:
    rows = data.get("battery_history", [])
    if len(rows) <= index:
        return {}
    row = rows[index]
    return {
        "date": row.get("date"),
        "charged_kwh": row.get("charged_kwh"),
        "discharged_kwh": row.get("discharged_kwh"),
        "status": row.get("status"),
        "calculation": "discharged / charged x 100; daily throughput ratio, not true round-trip efficiency",
    }


def _grid_day_value(data: dict[str, Any], index: int) -> Any:
    rows = data.get("grid_history", [])
    if len(rows) <= index:
        return None
    return rows[index].get("net_kwh")

def _grid_day_name(data: dict[str, Any], index: int) -> str | None:
    rows = data.get("grid_history", [])
    if len(rows) <= index:
        return f"Grid Energy Day {index + 1}"
    try:
        return "Grid Energy " + datetime.fromisoformat(rows[index]["date"]).strftime("%A %d %B %Y")
    except (KeyError, TypeError, ValueError):
        return f"Grid Energy Day {index + 1}"

def _grid_day_attrs(data: dict[str, Any], index: int) -> dict[str, Any]:
    rows = data.get("grid_history", [])
    if len(rows) <= index:
        return {}
    row = rows[index]
    return {
        "date": row.get("date"),
        "full_date": datetime.fromisoformat(row["date"]).strftime("%A %d %B %Y") if row.get("date") else None,
        "days_ago": index,
        # Explicit dashboard-friendly attribute names. Keep the original short
        # names as compatibility aliases for any existing cards/templates.
        "grid_import_kwh": row.get("import_kwh"),
        "grid_export_kwh": row.get("export_kwh"),
        "net_grid_kwh": row.get("net_kwh"),
        "import_kwh": row.get("import_kwh"),
        "export_kwh": row.get("export_kwh"),
        "net_kwh": row.get("net_kwh"),
        "status": row.get("status"),
        "definition": "net = grid import minus grid export; positive means net import",
    }


SENSORS = (
    HomeEnergySensorDescription(
        key="solar_forecast_today",
        name="Solar Forecast Today",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d["today_kwh"],
        attrs_fn=lambda d: {"daily_forecast": d["forecast"]},
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_tomorrow",
        name="Solar Forecast Tomorrow",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d["tomorrow_kwh"],
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_14_day_total",
        name="Solar Forecast 14 Day Total",
        icon="mdi:calendar-range",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d["total_14d_kwh"],
        attrs_fn=lambda d: {"daily_forecast": d["forecast"]},
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_day_1",
        name="Solar Forecast Day 1",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=0: _forecast_day_value(d, i),
        attrs_fn=lambda d, i=0: _forecast_day_attrs(d, i),
        name_fn=lambda d, i=0: _forecast_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_day_2",
        name="Solar Forecast Day 2",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=1: _forecast_day_value(d, i),
        attrs_fn=lambda d, i=1: _forecast_day_attrs(d, i),
        name_fn=lambda d, i=1: _forecast_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_day_3",
        name="Solar Forecast Day 3",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=2: _forecast_day_value(d, i),
        attrs_fn=lambda d, i=2: _forecast_day_attrs(d, i),
        name_fn=lambda d, i=2: _forecast_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_day_4",
        name="Solar Forecast Day 4",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=3: _forecast_day_value(d, i),
        attrs_fn=lambda d, i=3: _forecast_day_attrs(d, i),
        name_fn=lambda d, i=3: _forecast_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_day_5",
        name="Solar Forecast Day 5",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=4: _forecast_day_value(d, i),
        attrs_fn=lambda d, i=4: _forecast_day_attrs(d, i),
        name_fn=lambda d, i=4: _forecast_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_day_6",
        name="Solar Forecast Day 6",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=5: _forecast_day_value(d, i),
        attrs_fn=lambda d, i=5: _forecast_day_attrs(d, i),
        name_fn=lambda d, i=5: _forecast_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_day_7",
        name="Solar Forecast Day 7",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=6: _forecast_day_value(d, i),
        attrs_fn=lambda d, i=6: _forecast_day_attrs(d, i),
        name_fn=lambda d, i=6: _forecast_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="pv_projected_today",
        name="PV Projected Today",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d.get("pv_projected_today_kwh"),
    ),
    HomeEnergySensorDescription(
        key="pv_actual_today",
        name="PV Actual Today",
        icon="mdi:solar-panel",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d.get("pv_actual_today_kwh"),
        attrs_fn=lambda d: {
            "source_entity": d.get("daily_pv_entity"),
            "source_raw": d.get("daily_pv_raw"),
        },
    ),
    HomeEnergySensorDescription(
        key="pv_variance_today",
        name="PV Variance Today",
        icon="mdi:delta",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.get("pv_variance_today_kwh"),
    ),
    HomeEnergySensorDescription(
        key="pv_forecast_progress",
        name="PV Forecast Progress",
        icon="mdi:percent",
        native_unit_of_measurement="%",
        value_fn=lambda d: d.get("pv_forecast_progress_pct"),
    ),
    HomeEnergySensorDescription(
        key="hourly_solar_forecast_today",
        name="Hourly Solar Forecast Today",
        icon="mdi:chart-bar",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d.get("pv_actual_today_kwh"),
        attrs_fn=lambda d: {
            "hours": d.get("hourly_pv_today", []),
            "baseline": "05:00",
            "baseline_captured": d.get("hourly_pv_baseline_captured", False),
            "projected_is_cumulative": False,
            "actual_is_cumulative": False,
        },
    ),
    HomeEnergySensorDescription(
        key="pv_7_day_comparison",
        name="PV 7 Day Forecast vs Actual",
        icon="mdi:chart-line",
        value_fn=lambda d: len(d.get("pv_history", [])),
        attrs_fn=lambda d: {"days": d.get("pv_history", [])},
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_performance_7_days",
        name="Solar Forecast Performance - 7 Days",
        icon="mdi:chart-line",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("solar_forecast_performance_7d_pct"),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_performance_30_days",
        name="Solar Forecast Performance - 30 Days",
        icon="mdi:chart-timeline-variant",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("solar_forecast_performance_30d_pct"),
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_correction_factor",
        name="Solar Forecast Correction Factor",
        icon="mdi:tune-variant",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("solar_forecast_correction_factor"),
        attrs_fn=lambda d: {
            "status": d.get("solar_forecast_learning_status"),
            "completed_days": d.get("solar_forecast_completed_days"),
            "minimum_factor": 0.75,
            "maximum_factor": 1.25,
        },
    ),
    HomeEnergySensorDescription(
        key="corrected_solar_forecast_today",
        name="Corrected Solar Forecast Today",
        icon="mdi:weather-sunny-alert",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d.get("corrected_solar_forecast_today_kwh"),
        attrs_fn=lambda d: {
            "raw_05_00_baseline_kwh": d.get("solar_forecast_baseline_today_kwh"),
            "correction_factor": d.get("solar_forecast_correction_factor"),
            "status": d.get("solar_forecast_learning_status"),
            "completed_days": d.get("solar_forecast_completed_days"),
        },
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_learning_status",
        name="Solar Forecast Learning Status",
        icon="mdi:brain",
        value_fn=lambda d: d.get("solar_forecast_learning_status"),
        attrs_fn=lambda d: {"completed_days": d.get("solar_forecast_completed_days")},
    ),
    HomeEnergySensorDescription(
        key="solar_forecast_completed_days",
        name="Solar Forecast Completed Days Analysed",
        icon="mdi:calendar-check",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.get("solar_forecast_completed_days"),
    ),
    HomeEnergySensorDescription(
        key="ev_charging_day_1",
        name="EV Charging Day 1",
        icon="mdi:ev-station",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=0: _ev_day_value(d, i),
        attrs_fn=lambda d, i=0: _ev_day_attrs(d, i),
        name_fn=lambda d, i=0: _ev_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="ev_charging_day_2",
        name="EV Charging Day 2",
        icon="mdi:ev-station",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=1: _ev_day_value(d, i),
        attrs_fn=lambda d, i=1: _ev_day_attrs(d, i),
        name_fn=lambda d, i=1: _ev_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="ev_charging_day_3",
        name="EV Charging Day 3",
        icon="mdi:ev-station",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=2: _ev_day_value(d, i),
        attrs_fn=lambda d, i=2: _ev_day_attrs(d, i),
        name_fn=lambda d, i=2: _ev_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="ev_charging_day_4",
        name="EV Charging Day 4",
        icon="mdi:ev-station",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=3: _ev_day_value(d, i),
        attrs_fn=lambda d, i=3: _ev_day_attrs(d, i),
        name_fn=lambda d, i=3: _ev_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="ev_charging_day_5",
        name="EV Charging Day 5",
        icon="mdi:ev-station",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=4: _ev_day_value(d, i),
        attrs_fn=lambda d, i=4: _ev_day_attrs(d, i),
        name_fn=lambda d, i=4: _ev_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="ev_charging_day_6",
        name="EV Charging Day 6",
        icon="mdi:ev-station",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=5: _ev_day_value(d, i),
        attrs_fn=lambda d, i=5: _ev_day_attrs(d, i),
        name_fn=lambda d, i=5: _ev_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="ev_charging_day_7",
        name="EV Charging Day 7",
        icon="mdi:ev-station",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d, i=6: _ev_day_value(d, i),
        attrs_fn=lambda d, i=6: _ev_day_attrs(d, i),
        name_fn=lambda d, i=6: _ev_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="battery_performance_day_1",
        name="Battery Performance Day 1",
        icon="mdi:battery-sync",
        value_fn=lambda d, i=0: _battery_day_value(d, i),
        attrs_fn=lambda d, i=0: _battery_day_attrs(d, i),
        name_fn=lambda d, i=0: _battery_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="battery_performance_day_2",
        name="Battery Performance Day 2",
        icon="mdi:battery-sync",
        value_fn=lambda d, i=1: _battery_day_value(d, i),
        attrs_fn=lambda d, i=1: _battery_day_attrs(d, i),
        name_fn=lambda d, i=1: _battery_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="battery_performance_day_3",
        name="Battery Performance Day 3",
        icon="mdi:battery-sync",
        value_fn=lambda d, i=2: _battery_day_value(d, i),
        attrs_fn=lambda d, i=2: _battery_day_attrs(d, i),
        name_fn=lambda d, i=2: _battery_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="battery_performance_day_4",
        name="Battery Performance Day 4",
        icon="mdi:battery-sync",
        value_fn=lambda d, i=3: _battery_day_value(d, i),
        attrs_fn=lambda d, i=3: _battery_day_attrs(d, i),
        name_fn=lambda d, i=3: _battery_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="battery_performance_day_5",
        name="Battery Performance Day 5",
        icon="mdi:battery-sync",
        value_fn=lambda d, i=4: _battery_day_value(d, i),
        attrs_fn=lambda d, i=4: _battery_day_attrs(d, i),
        name_fn=lambda d, i=4: _battery_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="battery_performance_day_6",
        name="Battery Performance Day 6",
        icon="mdi:battery-sync",
        value_fn=lambda d, i=5: _battery_day_value(d, i),
        attrs_fn=lambda d, i=5: _battery_day_attrs(d, i),
        name_fn=lambda d, i=5: _battery_day_name(d, i),
    ),
    HomeEnergySensorDescription(
        key="battery_performance_day_7",
        name="Battery Performance Day 7",
        icon="mdi:battery-sync",
        value_fn=lambda d, i=6: _battery_day_value(d, i),
        attrs_fn=lambda d, i=6: _battery_day_attrs(d, i),
        name_fn=lambda d, i=6: _battery_day_name(d, i),
    ),
    HomeEnergySensorDescription(key="battery_charge_7_days", name="Battery Charged - Last 7 Days", icon="mdi:battery-arrow-up", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("battery_charge_7d_kwh")),
    HomeEnergySensorDescription(key="battery_discharge_7_days", name="Battery Discharged - Last 7 Days", icon="mdi:battery-arrow-down", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("battery_discharge_7d_kwh")),
    HomeEnergySensorDescription(key="battery_efficiency_7_days", name="Battery Discharge/Charge Ratio - Last 7 Days", icon="mdi:percent", native_unit_of_measurement="%", state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: d.get("battery_efficiency_7d_pct"), attrs_fn=lambda d: {"calculation": "total discharged / total charged x 100; throughput ratio, not true round-trip efficiency"}),
    HomeEnergySensorDescription(key="battery_equivalent_full_cycles_7_days", name="Battery Equivalent Full Cycles - Last 7 Days", icon="mdi:battery-sync", state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: d.get("battery_equivalent_full_cycles_7d"), attrs_fn=lambda d: {"basis": "total discharged energy divided by configured battery capacity"}),
    HomeEnergySensorDescription(key="grid_energy_day_1", name="Grid Energy Day 1", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=0: _grid_day_value(d, i), attrs_fn=lambda d, i=0: _grid_day_attrs(d, i), name_fn=lambda d, i=0: _grid_day_name(d, i)),
    HomeEnergySensorDescription(key="grid_energy_day_2", name="Grid Energy Day 2", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=1: _grid_day_value(d, i), attrs_fn=lambda d, i=1: _grid_day_attrs(d, i), name_fn=lambda d, i=1: _grid_day_name(d, i)),
    HomeEnergySensorDescription(key="grid_energy_day_3", name="Grid Energy Day 3", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=2: _grid_day_value(d, i), attrs_fn=lambda d, i=2: _grid_day_attrs(d, i), name_fn=lambda d, i=2: _grid_day_name(d, i)),
    HomeEnergySensorDescription(key="grid_energy_day_4", name="Grid Energy Day 4", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=3: _grid_day_value(d, i), attrs_fn=lambda d, i=3: _grid_day_attrs(d, i), name_fn=lambda d, i=3: _grid_day_name(d, i)),
    HomeEnergySensorDescription(key="grid_energy_day_5", name="Grid Energy Day 5", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=4: _grid_day_value(d, i), attrs_fn=lambda d, i=4: _grid_day_attrs(d, i), name_fn=lambda d, i=4: _grid_day_name(d, i)),
    HomeEnergySensorDescription(key="grid_energy_day_6", name="Grid Energy Day 6", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=5: _grid_day_value(d, i), attrs_fn=lambda d, i=5: _grid_day_attrs(d, i), name_fn=lambda d, i=5: _grid_day_name(d, i)),
    HomeEnergySensorDescription(key="grid_energy_day_7", name="Grid Energy Day 7", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=6: _grid_day_value(d, i), attrs_fn=lambda d, i=6: _grid_day_attrs(d, i), name_fn=lambda d, i=6: _grid_day_name(d, i)),
    # Permanent rolling Grid Energy sensors (v1.1.23.13).  These use fixed
    # unique IDs/object IDs while their friendly names and data roll forward daily.
    HomeEnergySensorDescription(key="grid_energy_today", name="Grid Energy Today", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=0: _grid_day_value(d, i), attrs_fn=lambda d, i=0: _grid_day_attrs(d, i)),
    HomeEnergySensorDescription(key="grid_energy_1_day_ago", name="Grid Energy 1 Day Ago", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=1: _grid_day_value(d, i), attrs_fn=lambda d, i=1: _grid_day_attrs(d, i)),
    HomeEnergySensorDescription(key="grid_energy_2_days_ago", name="Grid Energy 2 Days Ago", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=2: _grid_day_value(d, i), attrs_fn=lambda d, i=2: _grid_day_attrs(d, i)),
    HomeEnergySensorDescription(key="grid_energy_3_days_ago", name="Grid Energy 3 Days Ago", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=3: _grid_day_value(d, i), attrs_fn=lambda d, i=3: _grid_day_attrs(d, i)),
    HomeEnergySensorDescription(key="grid_energy_4_days_ago", name="Grid Energy 4 Days Ago", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=4: _grid_day_value(d, i), attrs_fn=lambda d, i=4: _grid_day_attrs(d, i)),
    HomeEnergySensorDescription(key="grid_energy_5_days_ago", name="Grid Energy 5 Days Ago", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=5: _grid_day_value(d, i), attrs_fn=lambda d, i=5: _grid_day_attrs(d, i)),
    HomeEnergySensorDescription(key="grid_energy_6_days_ago", name="Grid Energy 6 Days Ago", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d, i=6: _grid_day_value(d, i), attrs_fn=lambda d, i=6: _grid_day_attrs(d, i)),
    HomeEnergySensorDescription(key="grid_import_7_days", name="Grid Imported - Last 7 Days", icon="mdi:transmission-tower-import", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("grid_import_7d_kwh")),
    HomeEnergySensorDescription(key="grid_export_7_days", name="Grid Exported - Last 7 Days", icon="mdi:transmission-tower-export", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("grid_export_7d_kwh")),
    HomeEnergySensorDescription(key="grid_net_7_days", name="Net Grid Energy - Last 7 Days", icon="mdi:transmission-tower", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d: d.get("grid_net_7d_kwh"), attrs_fn=lambda d: {"definition": "grid imported minus grid exported; positive means net import"}),
    # Daily Energy Summary (v1.1.19). EV charging intentionally remains separate
    # from Hanchu house load because the Zappi is outside that measurement.
    HomeEnergySensorDescription(key="solar_generation_today", name="Solar Generation Today", icon="mdi:solar-power", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("daily_solar_generation_kwh")),
    HomeEnergySensorDescription(key="house_consumption_today", name="House Consumption Today", icon="mdi:home-lightning-bolt", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("daily_house_consumption_kwh")),
    HomeEnergySensorDescription(key="grid_import_today", name="Grid Import Today", icon="mdi:transmission-tower-import", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("daily_grid_import_kwh")),
    HomeEnergySensorDescription(key="grid_export_today", name="Grid Export Today", icon="mdi:transmission-tower-export", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("daily_grid_export_kwh")),
    HomeEnergySensorDescription(key="battery_charged_today", name="Battery Charged Today", icon="mdi:battery-arrow-up", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("daily_battery_charged_kwh")),
    HomeEnergySensorDescription(key="battery_discharged_today", name="Battery Discharged Today", icon="mdi:battery-arrow-down", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("daily_battery_discharged_kwh")),
    HomeEnergySensorDescription(key="battery_net_energy_today", name="Battery Net Energy Today", icon="mdi:battery-sync", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, value_fn=lambda d: d.get("daily_battery_net_kwh"), attrs_fn=lambda d: {"definition": "battery discharged minus battery charged; positive means net discharge"}),
    HomeEnergySensorDescription(key="ev_charging_today", name="EV Charging Today", icon="mdi:ev-station", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("daily_ev_charging_kwh")),
    HomeEnergySensorDescription(key="battery_soc_today", name="Battery SOC", icon="mdi:battery", native_unit_of_measurement="%", device_class=SensorDeviceClass.BATTERY, state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: d.get("daily_battery_soc_pct")),
    # Battery charge-source tracking (v1.1.21). Attribution is reconciled to
    # increments of the Hanchu Daily Charge Energy counter.
    HomeEnergySensorDescription(key="battery_charge_source", name="Battery Charge Source", icon="mdi:battery-sync", value_fn=lambda d: d.get("battery_charge_source"), attrs_fn=lambda d: {"solar_to_battery_w": d.get("solar_to_battery_w"), "grid_to_battery_w": d.get("grid_to_battery_w")}),
    HomeEnergySensorDescription(key="battery_charged_from_solar_today", name="Battery Charged From Solar Today", icon="mdi:solar-power", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("battery_charged_from_solar_today_kwh")),
    HomeEnergySensorDescription(key="battery_charged_from_grid_today", name="Battery Charged From Grid Today", icon="mdi:transmission-tower-import", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("battery_charged_from_grid_today_kwh")),
    HomeEnergySensorDescription(key="battery_solar_charge_pct_today", name="Battery Solar Charge % Today", icon="mdi:percent", native_unit_of_measurement="%", state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: d.get("battery_solar_charge_pct_today")),
    HomeEnergySensorDescription(key="battery_grid_charge_pct_today", name="Battery Grid Charge % Today", icon="mdi:percent", native_unit_of_measurement="%", state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: d.get("battery_grid_charge_pct_today")),
    HomeEnergySensorDescription(key="battery_charged_from_solar_this_month", name="Battery Charged From Solar This Month", icon="mdi:solar-power", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("battery_charged_from_solar_month_kwh")),
    HomeEnergySensorDescription(key="battery_charged_from_grid_this_month", name="Battery Charged From Grid This Month", icon="mdi:transmission-tower-import", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("battery_charged_from_grid_month_kwh")),
        HomeEnergySensorDescription(
        key="daily_energy_usage_this_month",
        name="Daily Energy Usage This Month",
        icon="mdi:chart-bar",
        value_fn=lambda d: len(d.get("daily_energy_usage_this_month", [])),
        attrs_fn=lambda d: {
            "month": d.get("billing_month"),
            "days": d.get("daily_energy_usage_this_month", []),
            "electricity_source": "Octopus current accumulative consumption",
            "tariff_source": "Octopus off-peak status",
            "gas_source": "Octopus gas consumption",
            "last_successful_energy_update": d.get("last_successful_energy_update"),
            "forecast_refresh_status": d.get("forecast_refresh_status"),
        },
    ),
    HomeEnergySensorDescription(
        key="house_consumption_last_7_days",
        name="House Consumption Last 7 Days",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d.get("house_consumption_7d_total_kwh"),
        attrs_fn=lambda d: {"days": d.get("house_consumption_7d_history", []),
                            "days_available": d.get("house_consumption_7d_days_available", 0),
                            "source": "Hanchu daily load energy; observed live or recovered from Recorder; no estimates"},
    ),
    HomeEnergySensorDescription(
        key="electricity_peak_offpeak_last_7_days",
        name="Electricity Peak Offpeak Last 7 Days",
        icon="mdi:flash",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d.get("electricity_combined_7d_total_kwh"),
        attrs_fn=lambda d: {"days": d.get("electricity_tariff_7d_history", []),
                            "days_available": d.get("electricity_tariff_7d_days_available", 0),
                            "peak_kwh": d.get("electricity_peak_7d_total_kwh"),
                            "offpeak_kwh": d.get("electricity_offpeak_7d_total_kwh"),
                            "source": "Validated Octopus daily import and off-peak counters; no monthly sensors"},
    ),
    HomeEnergySensorDescription(key="electricity_peak_this_month", name="Electricity Peak This Month", icon="mdi:transmission-tower-import", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("electricity_peak_kwh")),
    HomeEnergySensorDescription(key="electricity_offpeak_this_month", name="Electricity Off-Peak This Month", icon="mdi:weather-night", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("electricity_offpeak_kwh")),
    HomeEnergySensorDescription(key="electricity_total_this_month", name="Electricity Total This Month", icon="mdi:flash", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("electricity_total_kwh")),
    HomeEnergySensorDescription(key="ev_charging_this_month", name="EV Charging This Month", icon="mdi:ev-station", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("ev_month_kwh")),
    HomeEnergySensorDescription(key="electricity_peak_cost_this_month", name="Electricity Peak Cost This Month", icon="mdi:currency-gbp", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("electricity_peak_cost")),
    HomeEnergySensorDescription(key="electricity_offpeak_cost_this_month", name="Electricity Off-Peak Cost This Month", icon="mdi:currency-gbp", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("electricity_offpeak_cost")),
    HomeEnergySensorDescription(key="electricity_standing_charge_this_month", name="Electricity Standing Charge This Month", icon="mdi:currency-gbp", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("electricity_standing_cost")),
    HomeEnergySensorDescription(key="electricity_vat_this_month", name="Electricity VAT This Month", icon="mdi:percent", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("electricity_vat")),
    HomeEnergySensorDescription(key="electricity_total_cost_this_month", name="Electricity Total Cost This Month", icon="mdi:cash-multiple", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("electricity_total_cost"), attrs_fn=lambda d: {"billing_month": d.get("billing_month"), "days_elapsed": d.get("billing_days_elapsed"), "vat_rate_pct": d.get("vat_rate_pct")} ),
    HomeEnergySensorDescription(key="grid_export_this_month", name="Grid Export This Month", icon="mdi:transmission-tower-export", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("grid_export_month_kwh")),
    HomeEnergySensorDescription(key="export_credit_this_month", name="Export Credit This Month", icon="mdi:cash-plus", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("export_credit_this_month"), attrs_fn=lambda d: {"export_rate_p": d.get("export_rate_p"), "source_entity": d.get("daily_grid_export_entity")} ),
    HomeEnergySensorDescription(key="net_electricity_cost_this_month", name="Net Electricity Cost This Month", icon="mdi:cash-minus", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("net_electricity_cost")),
    HomeEnergySensorDescription(key="ev_charging_cost_this_month", name="EV Charging Cost This Month", icon="mdi:ev-station", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("ev_cost_inc_vat")),
    HomeEnergySensorDescription(key="gas_consumption_this_month", name="Gas Consumption This Month", icon="mdi:fire", native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY, value_fn=lambda d: d.get("gas_month_kwh")),
    HomeEnergySensorDescription(key="gas_usage_cost_this_month", name="Gas Usage Cost This Month", icon="mdi:currency-gbp", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("gas_usage_cost")),
    HomeEnergySensorDescription(key="gas_standing_charge_this_month", name="Gas Standing Charge This Month", icon="mdi:currency-gbp", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("gas_standing_cost")),
    HomeEnergySensorDescription(key="gas_vat_this_month", name="Gas VAT This Month", icon="mdi:percent", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("gas_vat")),
    HomeEnergySensorDescription(key="gas_total_cost_this_month", name="Gas Total Cost This Month", icon="mdi:cash-multiple", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("gas_total_cost")),
    HomeEnergySensorDescription(key="combined_energy_cost_this_month", name="Combined Energy Cost This Month", icon="mdi:cash", native_unit_of_measurement="GBP", device_class=SensorDeviceClass.MONETARY, value_fn=lambda d: d.get("combined_energy_cost"), attrs_fn=lambda d: {"billing_month": d.get("billing_month"), "vat_rate_pct": d.get("vat_rate_pct"), "peak_rate_p": d.get("peak_rate_p"), "offpeak_rate_p": d.get("offpeak_rate_p"), "gas_rate_p": d.get("gas_rate_p"), "electricity_standing_p": d.get("electricity_standing_p"), "gas_standing_p": d.get("gas_standing_p")} ),

    HomeEnergySensorDescription(
        key="battery_exportable_energy",
        name="Battery Exportable Energy",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_fn=lambda d: d["exportable_kwh"],
        attrs_fn=lambda d: {
            "battery_soc": d.get("soc"),
            "battery_soc_entity": d.get("battery_soc_entity"),
            "battery_soc_raw": d.get("battery_soc_raw"),
            "reserve_soc": None,
        },
    ),
    HomeEnergySensorDescription(
        key="calculated_export_start",
        name="Calculated Export Start",
        icon="mdi:clock-start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: datetime.fromisoformat(d["export_start"]) if d["export_start"] else None,
    ),
    HomeEnergySensorDescription(
        key="battery_power_mirror",
        name="Battery Power Mirror",
        icon="mdi:battery-sync",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d["battery_power_w"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomeEnergyCoordinator = hass.data[DOMAIN][entry.entry_id]

    # v1.1.23.15: migrate the permanent rolling Grid Energy sensors to their
    # intended stable entity IDs. Home Assistant's entity registry preserves an
    # entity_id once a unique_id has been registered, so changing entity naming
    # hints alone cannot repair IDs allocated by earlier releases. Migrate the
    # registry entries by unique_id before the entities are added to the platform.
    registry = er.async_get(hass)
    rolling_grid_keys = (
        "grid_energy_today",
        "grid_energy_1_day_ago",
        "grid_energy_2_days_ago",
        "grid_energy_3_days_ago",
        "grid_energy_4_days_ago",
        "grid_energy_5_days_ago",
        "grid_energy_6_days_ago",
    )
    for key in rolling_grid_keys:
        unique_id = f"{entry.entry_id}_{key}"
        current_entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        desired_entity_id = f"sensor.garage_home_energy_optimizer_{key}"
        if current_entity_id and current_entity_id != desired_entity_id:
            desired_entry = registry.async_get(desired_entity_id)
            if desired_entry is None:
                registry.async_update_entity(
                    current_entity_id, new_entity_id=desired_entity_id
                )

    async_add_entities(
        HomeEnergySensor(coordinator, entry, description) for description in SENSORS
    )


class HomeEnergySensor(CoordinatorEntity[HomeEnergyCoordinator], SensorEntity):
    """Representation of a Home Energy Optimizer sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HomeEnergyCoordinator,
        entry: ConfigEntry,
        description: HomeEnergySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        # Rolling forecast/EV sensors provide their complete rolling display name.
        # Keep them independent of the parent device naming so Home Assistant cannot
        # prepend "Home Energy Optimizer" to the friendly name. Their unique IDs are
        # unchanged, so existing entities/dashboard references remain intact.
        rolling_named = (
            description.key.startswith("solar_forecast_day_")
            or description.key.startswith("ev_charging_day_")
            or description.key.startswith("battery_performance_day_")
            or description.key.startswith("grid_energy_day_")
        )
        monthly_named = (
            description.key.endswith("_this_month")
            or description.key == "combined_energy_cost_this_month"
        )
        if rolling_named or monthly_named:
            self._attr_has_entity_name = False
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # New v1.1.23.13 rolling Grid Energy entities get deterministic, permanent
        # entity IDs so Lovelace cards never need date-based entity references.
        if description.key == "grid_energy_today" or (
            description.key.startswith("grid_energy_") and description.key.endswith("_ago")
        ):
            self._attr_suggested_object_id = f"garage_home_energy_optimizer_{description.key}"
            # v1.1.23.14: force a stable object ID. In v1.1.23.13 the dynamic
            # friendly name was used by HA when the entity was first registered,
            # so the intended *_today/*_days_ago entity IDs were not created.
            self.entity_id = f"sensor.garage_home_energy_optimizer_{description.key}"
        if rolling_named or monthly_named:
            self._attr_device_info = None
        else:
            self._attr_device_info = {
                "identifiers": {(DOMAIN, entry.entry_id)},
                "name": "Home Energy Optimizer",
                "manufacturer": "Custom",
                "model": "Solar + Hanchu + Octopus",
            }

    @property
    def name(self) -> str | None:
        """Return a rolling date-based name for forecast-day sensors."""
        if self.entity_description.name_fn:
            dynamic_name = self.entity_description.name_fn(self.coordinator.data)
            if dynamic_name:
                return dynamic_name
        return self.entity_description.name

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        if self.entity_description.attrs_fn:
            return self.entity_description.attrs_fn(self.coordinator.data)
        return None

