"""Data coordinator for Home Energy Optimizer."""

from __future__ import annotations

from datetime import timedelta, datetime, time
from functools import partial
import logging
import math
import asyncio
from typing import Any

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from .house_history import START as HOUSE_HISTORY_START, recover_days
from .entity_sources import discover_unique_entity, hanchu_entity, meter_derived_entity
from .energy_daily import merge_live_today

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
    DEFAULT_SOC_ENTITY,
    DEFAULT_POWER_ENTITY,
    CONF_DAILY_PV_ENTITY,
    DEFAULT_DAILY_PV_ENTITY,
    CONF_DAILY_EV_ENTITY,
    DEFAULT_DAILY_EV_ENTITY,
    CONF_ELECTRICITY_CONSUMPTION_ENTITY, DEFAULT_ELECTRICITY_CONSUMPTION_ENTITY,
    CONF_HOUSE_GRID_IMPORT_ENTITY, DEFAULT_HOUSE_GRID_IMPORT_ENTITY,
    CONF_GAS_CONSUMPTION_ENTITY, DEFAULT_GAS_CONSUMPTION_ENTITY,
    CONF_ELECTRICITY_PEAK_RATE, DEFAULT_ELECTRICITY_PEAK_RATE,
    CONF_ELECTRICITY_OFFPEAK_RATE, DEFAULT_ELECTRICITY_OFFPEAK_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE, DEFAULT_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_RATE, DEFAULT_GAS_RATE,
    CONF_GAS_STANDING_CHARGE, DEFAULT_GAS_STANDING_CHARGE,
    CONF_VAT_RATE, DEFAULT_VAT_RATE,
    CONF_OFFPEAK_START, DEFAULT_OFFPEAK_START,
    CONF_OFFPEAK_END, DEFAULT_OFFPEAK_END,
    CONF_OCTOPUS_OFFPEAK_ENTITY, DEFAULT_OCTOPUS_OFFPEAK_ENTITY,
    CONF_DAILY_GRID_EXPORT_ENTITY, DEFAULT_DAILY_GRID_EXPORT_ENTITY,
    CONF_EXPORT_RATE, DEFAULT_EXPORT_RATE,
    DEFAULT_DAILY_LOAD_ENTITY, DEFAULT_DAILY_CHARGE_ENTITY, DEFAULT_DAILY_DISCHARGE_ENTITY,
    DEFAULT_GRID_POWER_ENTITY, DEFAULT_PV_POWER_ENTITY, DEFAULT_LOAD_POWER_ENTITY,
)

_LOGGER = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(minutes=30)
OCTOPUS_CUTOFF = "2026-10-24"


# No bundled household history. Private recovered values stay in local storage.
OCTOBER_2026_CSV_DAILY_MAXIMA = {'house': {}, 'ev': {}}


# Historical export data is not bundled in public distribution.
OCTOBER_2026_EXPORT_CSV_MAXIMA = {}
# No site-specific export overrides are bundled.
OCTOBER_2026_OCTOPUS_EXPORT_OVERRIDES = {}

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "home_energy_optimizer_pv_history"


class HomeEnergyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate forecast and calculated energy data."""

    def _hanchu_entity(self, suffix: str) -> str:
        """Use the private serial from this installation's config entry."""
        return hanchu_entity(self.entry.data.get(CONF_HANCHU_SERIAL, ""), suffix)

    @property
    def _electricity_import_entity(self) -> str:
        """Resolve configured meter or one unambiguous live Octopus entity."""
        configured = self.entry.data.get(CONF_ELECTRICITY_CONSUMPTION_ENTITY, "")
        if configured:
            return str(configured)
        return discover_unique_entity(
            (st.entity_id for st in self.hass.states.async_all()),
            "sensor.octopus_energy_electricity_",
            "_current_accumulative_consumption",
        )

    @property
    def _gas_import_entity(self) -> str:
        """Resolve configured gas meter or one unambiguous live meter."""
        configured = self.entry.data.get(CONF_GAS_CONSUMPTION_ENTITY, "")
        if configured:
            return str(configured)
        return discover_unique_entity(
            (st.entity_id for st in self.hass.states.async_all()),
            "sensor.octopus_energy_gas_",
            "_current_accumulative_consumption_kwh",
        )

    def _electricity_entity(self, suffix: str) -> str:
        return meter_derived_entity(
            self._electricity_import_entity, "_current_accumulative_consumption", suffix
        )

    def _gas_entity(self, suffix: str) -> str:
        return meter_derived_entity(
            self._gas_import_entity, "_current_accumulative_consumption_kwh", suffix
        )


    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self._unsub_live = None
        # Meter updates run independently of solar forecast polling.
        self._energy_refresh_lock = asyncio.Lock()
        self._unsub_energy_timer = None
        self._unsub_energy_midnight = None
        self._unsub_energy_events = None
        self._last_energy_refresh_attempt = None
        self._last_successful_energy_update = None
        self._forecast_refresh_status = "pending"
        # Persist PV and EV rolling histories per config entry.
        # These were present in v1.1.6 and must be initialised before
        # async_load_pv_history() is called from async_setup_entry().
        self._store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}_{entry.entry_id}",
        )
        self._pv_history: list[dict[str, Any]] = []
        self._ev_history: list[dict[str, Any]] = []
        # v1.1.23.7: rolling battery charge/discharge performance history.
        self._battery_history: list[dict[str, Any]] = []
        # v1.1.23.10: rolling grid import/export history.
        self._grid_history: list[dict[str, Any]] = []
        # Validated daily Hanchu house counter, persisted across restarts/months.
        self._house_daily_history: dict[str, float] = {}
        self._house_recovery: dict[str, dict[str, Any]] = {}
        self._house_recovery_checked = None
        self._house_recovery_lock = asyncio.Lock()
        self._octopus_daily_records: dict[str, dict[str, Any]] = {}
        # Preserve complete Octopus daily meter readings beyond Recorder retention.
        self._octopus_import_days: dict[str, dict[str, float]] = {}
        self._electricity_daily_cache: dict[str, dict[str, dict[str, float]]] = {"house": {}, "ev": {}}
        self._battery_charge_sources: dict[str, dict[str, float]] = {}
        self._last_daily_charge_kwh: float | None = None
        # v1.1.21.1: live source-energy integration state.  These values are
        # intentionally not persisted so a Home Assistant restart never
        # backfills energy across the offline period.
        self._charge_tracking_last_ts: datetime | None = None
        self._charge_tracking_day: str | None = None
        self._charge_tracking_solar_w: float = 0.0
        self._charge_tracking_grid_w: float = 0.0
        # v1.1.22: frozen 05:00 hourly solar projection and measured hourly PV.
        self._hourly_pv: dict[str, Any] = {}
        # v1.1.23.3: immutable per-date 05:00 solar baselines.  A baseline is
        # only created during the 05:00 local hour; starting/restarting HA later
        # in the day must never manufacture a new baseline from a refreshed forecast.
        self._solar_baselines: dict[str, dict[str, Any]] = {}
        self._last_daily_pv_kwh: float | None = None
        # v1.1.23.9: cache the latest fresh Open-Meteo hourly rows. Live Hanchu
        # state changes must never try to repair a missing hourly baseline from
        # the persisted projected profile, because that profile may itself be empty.
        self._latest_hourly_forecast_rows: list[dict[str, Any]] = []

    async def async_load_pv_history(self) -> None:
        """Load persisted rolling PV forecast-versus-actual history."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            if isinstance(stored.get("house_recovery"), dict):
                self._house_recovery = stored["house_recovery"]
            if isinstance(stored.get("history"), list):
                self._pv_history = stored["history"][-31:]
            if isinstance(stored.get("ev_history"), list):
                self._ev_history = stored["ev_history"][-7:]
            if isinstance(stored.get("battery_history"), list):
                self._battery_history = stored["battery_history"][-8:]
            if isinstance(stored.get("house_daily_history"), dict):
                self._house_daily_history = {day: float(kwh) for day, kwh in stored["house_daily_history"].items()
                                             if isinstance(kwh, (int, float)) and kwh >= 0}
            if isinstance(stored.get("grid_history"), list):
                self._grid_history = stored["grid_history"][-8:]
            if isinstance(stored.get("octopus_daily_records"), dict):
                self._octopus_daily_records = {day: row for day, row in stored["octopus_daily_records"].items() if day >= OCTOPUS_CUTOFF and isinstance(row, dict)}
            if isinstance(stored.get("electricity_daily_cache"), dict):
                cache = stored["electricity_daily_cache"]
                self._electricity_daily_cache = {key: dict(cache.get(key, {})) for key in ("house", "ev")}
            if isinstance(stored.get("octopus_import_days"), dict):
                self._octopus_import_days = stored["octopus_import_days"]
            if isinstance(stored.get("battery_charge_sources"), dict):
                self._battery_charge_sources = stored["battery_charge_sources"]
            if isinstance(stored.get("hourly_pv"), dict):
                self._hourly_pv = stored["hourly_pv"]
            if isinstance(stored.get("solar_baselines"), dict):
                self._solar_baselines = stored["solar_baselines"]
            last_pv = stored.get("last_daily_pv_kwh")
            if isinstance(last_pv, (int, float)):
                self._last_daily_pv_kwh = float(last_pv)
            last_charge = stored.get("last_daily_charge_kwh")
            if isinstance(last_charge, (int, float)):
                self._last_daily_charge_kwh = float(last_charge)

    async def _async_save_pv_history(self) -> None:
        """Persist rolling PV history."""
        await self._store.async_save({
            "history": self._pv_history[-31:],
            "ev_history": self._ev_history[-7:],
            "battery_history": self._battery_history[-8:],
            "grid_history": self._grid_history[-8:],
            "house_daily_history": self._house_daily_history,
            "house_recovery": self._house_recovery,
            "octopus_daily_records": self._octopus_daily_records,
            "octopus_import_days": self._octopus_import_days,
            "electricity_daily_cache": self._electricity_daily_cache,
            "battery_charge_sources": self._battery_charge_sources,
            "last_daily_charge_kwh": self._last_daily_charge_kwh,
            "hourly_pv": self._hourly_pv,
            "solar_baselines": self._solar_baselines,
            "last_daily_pv_kwh": self._last_daily_pv_kwh,
        })

    async def _async_update_pv_history(self, forecast: list[dict[str, Any]]) -> None:
        """Track PV actual and capture one immutable forecast baseline in the 05:00 hour."""
        now_local = dt_util.now()
        today = now_local.date().isoformat()
        row = next((x for x in self._pv_history if x.get("date") == today), None)
        live_projected = forecast[0].get("forecast_kwh") if forecast else None
        # Keep the last usable pre-capture value available if Open-Meteo happens
        # to be unavailable during a refresh in the 05:00 capture hour.
        projected = live_projected
        if projected is None and row is not None:
            projected = row.get("projected_kwh")
        actual = self._state_float(self.entry.data.get(CONF_DAILY_PV_ENTITY, self._hanchu_entity("daily_pv_energy")))
        in_capture_hour = now_local.hour == 5
        changed = False

        # The authoritative baseline is write-once per date. Capture during
        # 05:00-05:59 whenever a usable forecast exists. If the provider was
        # unavailable for the whole capture hour, promote the first usable
        # value after 06:00 as a one-time recovery baseline. This guarantees
        # Solar Performance Today has a stable projected value from 06:00
        # whenever any forecast for the date is available, without allowing
        # later refreshes or restarts to change it.
        baseline = self._solar_baselines.get(today)
        recovery_capture = now_local.hour >= 6 and baseline is None and projected is not None
        if baseline is None and projected is not None and (in_capture_hour or recovery_capture):
            baseline = {
                "date": today,
                "projected_kwh": round(float(projected), 2),
                "captured_at": now_local.isoformat(),
                "capture_mode": "05:00" if in_capture_hour else "post_06:00_recovery",
            }
            self._solar_baselines[today] = baseline
            self._solar_baselines = dict(sorted(self._solar_baselines.items())[-31:])
            changed = True

        if row is None:
            row = {
                "date": today,
                "projected_kwh": baseline.get("projected_kwh") if baseline else projected,
                "actual_kwh": actual,
                "baseline_captured": baseline is not None,
                "baseline_captured_at": baseline.get("captured_at") if baseline else None,
            }
            self._pv_history.append(row)
            changed = True
        else:
            if baseline is not None:
                locked = baseline.get("projected_kwh")
                if row.get("projected_kwh") != locked:
                    row["projected_kwh"] = locked
                    changed = True
                if not row.get("baseline_captured", False):
                    row["baseline_captured"] = True
                    changed = True
                if row.get("baseline_captured_at") != baseline.get("captured_at"):
                    row["baseline_captured_at"] = baseline.get("captured_at")
                    changed = True
            elif now_local.hour < 5 and projected is not None:
                # Before the capture hour this is explicitly provisional.
                if row.get("projected_kwh") != projected:
                    row["projected_kwh"] = projected
                    changed = True
                row["baseline_captured"] = False
            # After 05:59 with no authoritative baseline (for example an upgrade
            # installed mid-day), freeze the existing row rather than replacing
            # it with every subsequent Open-Meteo refresh.  The next date will
            # be captured normally during 05:00..05:59.

            if actual is not None and row.get("actual_kwh") != actual:
                row["actual_kwh"] = actual
                changed = True

        self._pv_history = sorted(self._pv_history, key=lambda x: x.get("date", ""))[-31:]
        if changed:
            await self._async_save_pv_history()



    async def _async_update_hourly_pv(self, hourly_rows: list[dict[str, Any]]) -> None:
        """Persist one daily/hourly 05:00 solar baseline and accumulate actual PV by hour."""
        now = dt_util.now()
        today = now.date().isoformat()
        in_capture_hour = now.hour == 5
        changed = False

        # Keep only today's working record; completed daily history is handled elsewhere.
        if self._hourly_pv.get("date") != today:
            self._hourly_pv = {
                "date": today,
                "baseline_captured": False,
                "projected": [],
                "actual": {str(h): 0.0 for h in range(24)},
            }
            self._last_daily_pv_kwh = None
            changed = True

        todays = [r for r in hourly_rows if r.get("date") == today]
        fresh_projected = [{"hour": r["hour"], "kwh": r["kwh"]} for r in todays]
        authoritative = self._solar_baselines.get(today)

        def _scaled_profile(rows: list[dict[str, Any]], target: Any) -> list[dict[str, Any]]:
            """Scale the Open-Meteo hourly shape so it sums exactly to the locked daily baseline."""
            try:
                target_total = float(target)
            except (TypeError, ValueError):
                return []
            source_total = sum(float(x.get("kwh", 0.0) or 0.0) for x in rows)
            if target_total <= 0 or source_total <= 0.001:
                return []
            scale = target_total / source_total
            result = [
                {"hour": x["hour"], "kwh": round(float(x.get("kwh", 0.0) or 0.0) * scale, 4)}
                for x in rows
            ]
            drift = round(target_total - sum(float(x["kwh"]) for x in result), 4)
            if abs(drift) > 0 and result:
                candidates = [x for x in result if float(x.get("kwh", 0.0)) > 0]
                bucket = candidates[-1] if candidates else result[-1]
                bucket["kwh"] = round(max(0.0, float(bucket["kwh"]) + drift), 4)
            return result

        # v1.1.23.4: the daily baseline and its hourly profile are one persisted
        # record.  During 05:00-05:59 the hourly shape is scaled to the immutable
        # daily forecast and saved into solar_baselines.  Thereafter the chart
        # always reads that stored profile, so Open-Meteo refreshes and HA
        # restarts cannot alter today's orange Projected bars.
        if authoritative is not None:
            stored_profile = authoritative.get("hourly_profile")
            # v1.1.23.5: a non-empty list is not necessarily a usable profile.
            # Older same-day state can contain 24 zero-value buckets; treating
            # that as valid prevents the repair path and leaves ApexCharts with
            # no visible Projected bars even though the daily baseline is valid.
            stored_profile_total = 0.0
            if isinstance(stored_profile, list):
                try:
                    stored_profile_total = sum(
                        float(x.get("kwh", 0.0) or 0.0)
                        for x in stored_profile
                        if isinstance(x, dict)
                    )
                except (TypeError, ValueError):
                    stored_profile_total = 0.0
            stored_profile_valid = bool(stored_profile) and stored_profile_total > 0.001

            if stored_profile_valid:
                if self._hourly_pv.get("projected") != stored_profile:
                    self._hourly_pv["projected"] = stored_profile
                    changed = True
                if not self._hourly_pv.get("baseline_captured", False):
                    self._hourly_pv["baseline_captured"] = True
                    changed = True
                captured_at = authoritative.get("captured_at")
                if self._hourly_pv.get("baseline_captured_at") != captured_at:
                    self._hourly_pv["baseline_captured_at"] = captured_at
                    changed = True
            elif fresh_projected and (in_capture_hour or now.hour > 5):
                # Upgrade repair for a date whose daily baseline was already
                # captured by v1.1.23.3 without an hourly profile.  Create it
                # once from the current hourly shape, scale it to the already
                # locked daily total, then persist it.  From the next date the
                # profile is captured normally during the 05:00 hour.
                profile = _scaled_profile(fresh_projected, authoritative.get("projected_kwh"))
                if profile:
                    authoritative["hourly_profile"] = profile
                    self._hourly_pv["projected"] = profile
                    self._hourly_pv["baseline_captured"] = True
                    self._hourly_pv["baseline_captured_at"] = authoritative.get("captured_at")
                    changed = True
                    _LOGGER.info(
                        "Persisted hourly solar baseline for %s; daily total %.2f kWh",
                        today,
                        float(authoritative.get("projected_kwh", 0.0) or 0.0),
                    )
        elif now.hour < 5:
            # Before the official capture hour, show the latest forecast only as
            # a provisional profile. It is deliberately not persisted as the
            # day's official baseline.
            if fresh_projected and self._hourly_pv.get("projected") != fresh_projected:
                self._hourly_pv["projected"] = fresh_projected
                changed = True
            self._hourly_pv["baseline_captured"] = False

        # Integrate the Hanchu daily PV counter into the current clock-hour bucket.
        pv_entity = self.entry.data.get(CONF_DAILY_PV_ENTITY, self._hanchu_entity("daily_pv_energy"))
        current = self._state_float(pv_entity)
        if current is not None:
            if self._last_daily_pv_kwh is not None and current >= self._last_daily_pv_kwh:
                delta = current - self._last_daily_pv_kwh
                if 0 <= delta <= 5.0:
                    key = str(now.hour)
                    actual = self._hourly_pv.setdefault("actual", {})
                    actual[key] = round(float(actual.get(key, 0.0)) + delta, 4)
                    if delta > 0:
                        changed = True
            self._last_daily_pv_kwh = current

        if changed:
            await self._async_save_pv_history()

    def _hourly_pv_payload(self) -> list[dict[str, Any]]:
        """Return non-cumulative hourly projected/actual PV rows for today's chart."""
        projected = {int(x["hour"]): x.get("kwh") for x in self._hourly_pv.get("projected", [])}
        actual = self._hourly_pv.get("actual", {})
        hours = sorted(h for h, v in projected.items() if (v or 0) > 0.001)
        if not hours:
            hours = list(range(24))
        now = dt_util.now()
        rows = []
        for hour in hours:
            # Current hour is deliberately shown as in-progress actual; future hours are null.
            act = None if hour > now.hour else round(float(actual.get(str(hour), 0.0)), 3)
            hour_start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            rows.append({
                "hour": f"{hour:02d}:00",
                # Millisecond Unix timestamp for dashboard chart cards such as
                # ApexCharts. This preserves the friendly hour label while
                # providing a true time-axis value for each hourly bucket.
                "timestamp": int(hour_start.timestamp() * 1000),
                "projected_kwh": round(float(projected.get(hour, 0.0) or 0.0), 3),
                "actual_kwh": act,
                "complete": hour < now.hour,
            })
        return rows

    def _pv_history_payload(self) -> list[dict[str, Any]]:
        """Return newest-first rolling seven-day comparison, including today."""
        rows = []
        for item in sorted(self._pv_history, key=lambda x: x.get("date", ""), reverse=True)[:7]:
            projected = item.get("projected_kwh")
            actual = item.get("actual_kwh")
            variance = None
            accuracy = None
            if projected is not None and actual is not None:
                variance = round(float(actual) - float(projected), 2)
                if float(projected) > 0:
                    accuracy = round((float(actual) / float(projected)) * 100.0, 1)
            rows.append({
                "date": item.get("date"),
                "projected_kwh": projected,
                "actual_kwh": actual,
                "variance_kwh": variance,
                "accuracy_pct": accuracy,
                "complete": item.get("date") != dt_util.now().date().isoformat(),
            })
        return rows

    def _calculate_solar_forecast_intelligence(self) -> dict[str, Any]:
        """Calculate learned solar forecast performance and correction.

        Only completed days are used. The correction factor is based on the
        aggregate actual/projected ratio over up to 30 completed days, which
        avoids overweighting very low-generation days. It is constrained to
        0.75..1.25. Fewer than seven completed days remains in Learning and
        applies no correction; 7-13 days is Preliminary; 14+ days is Active.
        """
        today = dt_util.now().date().isoformat()
        completed = []
        for row in sorted(self._pv_history, key=lambda x: x.get("date", ""), reverse=True):
            if row.get("date") == today:
                continue
            projected = row.get("projected_kwh")
            actual = row.get("actual_kwh")
            try:
                projected = float(projected)
                actual = float(actual)
            except (TypeError, ValueError):
                continue
            if projected <= 0:
                continue
            completed.append((projected, actual))

        def aggregate_performance(rows: list[tuple[float, float]]) -> float | None:
            if not rows:
                return None
            projected_total = sum(x[0] for x in rows)
            if projected_total <= 0:
                return None
            return round(sum(x[1] for x in rows) / projected_total * 100.0, 1)

        perf7 = aggregate_performance(completed[:7])
        perf30 = aggregate_performance(completed[:30])
        count = min(len(completed), 30)

        if count < 7:
            status = "Learning"
            factor = 1.0
        else:
            status = "Preliminary" if count < 14 else "Active"
            raw_factor = (perf30 / 100.0) if perf30 is not None else 1.0
            factor = round(min(1.25, max(0.75, raw_factor)), 3)

        today_row = next((x for x in self._pv_history if x.get("date") == today), None)
        baseline = today_row.get("projected_kwh") if today_row else None
        corrected = None
        if baseline is not None:
            try:
                corrected = round(float(baseline) * factor, 2)
            except (TypeError, ValueError):
                pass

        return {
            "solar_forecast_performance_7d_pct": perf7,
            "solar_forecast_performance_30d_pct": perf30,
            "solar_forecast_correction_factor": factor,
            "corrected_solar_forecast_today_kwh": corrected,
            "solar_forecast_learning_status": status,
            "solar_forecast_completed_days": count,
            "solar_forecast_baseline_today_kwh": baseline,
        }

    async def async_backfill_ev_history(self) -> None:
        """Repair the previous six completed EV days from Recorder history.

        The configured Zappi sensor is a daily-reset energy sensor. For each of
        the previous six local calendar days, the highest recorded state for
        that day is the completed daily charging total. Completed days are
        deliberately refreshed from Recorder so a false midnight carry-over
        saved by an older integration version can be corrected. A completed
        day with valid Recorder samples but no charging is stored as 0.00 kWh.
        Recorder failures are non-fatal.
        """
        entity = self.entry.data.get(CONF_DAILY_EV_ENTITY, DEFAULT_DAILY_EV_ENTITY)
        today = dt_util.now().date()
        completed_dates = {
            (today - timedelta(days=i)).isoformat() for i in range(1, 7)
        }

        local_now = dt_util.now()
        start_local = datetime.combine(
            today - timedelta(days=6), time.min, tzinfo=local_now.tzinfo
        )
        end_local = datetime.combine(today, time.min, tzinfo=local_now.tzinfo)

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder import history as recorder_history

            job = partial(
                recorder_history.get_significant_states,
                self.hass,
                start_local,
                end_local,
                entity_ids=[entity],
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=True,
            )
            history = await get_instance(self.hass).async_add_executor_job(job)
        except Exception as err:  # Recorder history must never block integration setup.
            _LOGGER.warning("Unable to repair EV history from Recorder: %s", err)
            return

        states = history.get(entity, []) if isinstance(history, dict) else []
        maxima: dict[str, float] = {}
        seen_dates: set[str] = set()

        for state in states:
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                continue

            changed = getattr(state, "last_changed", None) or getattr(state, "last_updated", None)
            if changed is None:
                continue

            day = dt_util.as_local(changed).date().isoformat()
            if day not in completed_dates:
                continue

            seen_dates.add(day)
            maxima[day] = max(maxima.get(day, 0.0), max(value, 0.0))

        if not seen_dates:
            return

        changed = False
        for day in completed_dates:
            if day not in seen_dates:
                continue

            value = round(maxima.get(day, 0.0), 2)
            row = next((x for x in self._ev_history if x.get("date") == day), None)
            if row is None:
                self._ev_history.append({"date": day, "kwh": value})
                changed = True
            elif round(float(row.get("kwh") or 0.0), 2) != value:
                row["kwh"] = value
                changed = True

        self._ev_history = sorted(self._ev_history, key=lambda x: x.get("date", ""))[-7:]
        if changed:
            await self._async_save_pv_history()

    async def async_backfill_battery_history(self) -> None:
        """Backfill the previous seven completed battery days from Recorder.

        Hanchu daily charge/discharge entities reset each local day. The highest
        valid state recorded within a completed day is therefore that day's
        completed total. Recorder failures are deliberately non-fatal.
        """
        entities = [self._hanchu_entity("daily_charge_energy"), self._hanchu_entity("daily_discharge_energy")]
        today = dt_util.now().date()
        completed_dates = {(today - timedelta(days=i)).isoformat() for i in range(1, 8)}
        local_now = dt_util.now()
        start_local = datetime.combine(today - timedelta(days=7), time.min, tzinfo=local_now.tzinfo)
        end_local = datetime.combine(today, time.min, tzinfo=local_now.tzinfo)
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder import history as recorder_history
            job = partial(
                recorder_history.get_significant_states,
                self.hass, start_local, end_local,
                entity_ids=entities,
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=True,
            )
            history = await get_instance(self.hass).async_add_executor_job(job)
        except Exception as err:
            _LOGGER.warning("Unable to backfill battery history from Recorder: %s", err)
            return

        maxima: dict[str, dict[str, float]] = {day: {} for day in completed_dates}
        seen: dict[str, set[str]] = {day: set() for day in completed_dates}
        for entity_id, key in ((self._hanchu_entity("daily_charge_energy"), "charged_kwh"), (self._hanchu_entity("daily_discharge_energy"), "discharged_kwh")):
            states = history.get(entity_id, []) if isinstance(history, dict) else []
            for state in states:
                try:
                    value = max(float(state.state), 0.0)
                except (TypeError, ValueError):
                    continue
                changed = getattr(state, "last_changed", None) or getattr(state, "last_updated", None)
                if changed is None:
                    continue
                day = dt_util.as_local(changed).date().isoformat()
                if day not in completed_dates:
                    continue
                seen[day].add(key)
                maxima[day][key] = max(maxima[day].get(key, 0.0), value)

        changed_any = False
        for day in completed_dates:
            if not seen[day]:
                continue
            charged = round(maxima[day].get("charged_kwh", 0.0), 2)
            discharged = round(maxima[day].get("discharged_kwh", 0.0), 2)
            row = next((x for x in self._battery_history if x.get("date") == day), None)
            new = {"date": day, "charged_kwh": charged, "discharged_kwh": discharged}
            if row is None:
                self._battery_history.append(new)
                changed_any = True
            elif row != new:
                row.update(new)
                changed_any = True
        self._battery_history = sorted(self._battery_history, key=lambda x: x.get("date", ""))[-8:]
        if changed_any:
            await self._async_save_pv_history()

    async def _async_update_battery_history(self) -> None:
        """Track today's live daily-reset Hanchu battery counters."""
        charged = self._state_float(self._hanchu_entity("daily_charge_energy"))
        discharged = self._state_float(self._hanchu_entity("daily_discharge_energy"))
        if charged is None and discharged is None:
            return
        today = dt_util.now().date().isoformat()
        new = {
            "date": today,
            "charged_kwh": round(max(charged or 0.0, 0.0), 2),
            "discharged_kwh": round(max(discharged or 0.0, 0.0), 2),
        }
        row = next((x for x in self._battery_history if x.get("date") == today), None)
        changed = False
        if row is None:
            self._battery_history.append(new)
            changed = True
        elif row != new:
            row.update(new)
            changed = True
        self._battery_history = sorted(self._battery_history, key=lambda x: x.get("date", ""))[-8:]
        if changed:
            await self._async_save_pv_history()

    def _battery_history_payload(self) -> list[dict[str, Any]]:
        """Return today through six days ago with charge/discharge and throughput ratio."""
        today = dt_util.now().date()
        rows = []
        for i in range(7):
            day = (today - timedelta(days=i)).isoformat()
            source = next((x for x in self._battery_history if x.get("date") == day), None) or {}
            charged = source.get("charged_kwh")
            discharged = source.get("discharged_kwh")
            ratio = None
            if isinstance(charged, (int, float)) and charged > 0 and isinstance(discharged, (int, float)):
                ratio = round((discharged / charged) * 100.0, 1)
            rows.append({
                "date": day,
                "charged_kwh": charged,
                "discharged_kwh": discharged,
                "efficiency_pct": ratio,
                "status": "In progress" if i == 0 else "Complete",
            })
        return rows

    def _battery_history_summary(self) -> dict[str, Any]:
        # Summary deliberately excludes today's partial counters and uses the
        # previous seven completed local days. This prevents an early-morning
        # partial charge/discharge cycle from distorting the rolling figures.
        today = dt_util.now().date()
        completed_dates = [(today - timedelta(days=i)).isoformat() for i in range(1, 8)]
        rows = []
        for day in completed_dates:
            source = next((x for x in self._battery_history if x.get("date") == day), None) or {}
            rows.append({
                "date": day,
                "charged_kwh": source.get("charged_kwh"),
                "discharged_kwh": source.get("discharged_kwh"),
            })
        charged = round(sum(float(x.get("charged_kwh") or 0.0) for x in rows), 2)
        discharged = round(sum(float(x.get("discharged_kwh") or 0.0) for x in rows), 2)
        ratio = round((discharged / charged) * 100.0, 1) if charged > 0 else None
        capacity = float(self.entry.data.get(CONF_BATTERY_CAPACITY, 15.0) or 15.0)
        cycles = round(discharged / capacity, 2) if capacity > 0 else None
        return {
            "battery_history": self._battery_history_payload(),
            "battery_charge_7d_kwh": charged,
            "battery_discharge_7d_kwh": discharged,
            "battery_efficiency_7d_pct": ratio,
            "battery_equivalent_full_cycles_7d": cycles,
        }

    async def async_backfill_grid_history(self) -> None:
        """Grid history is derived from the shared Octopus daily records."""
        return

    async def _async_update_grid_history(self) -> None:
        """Refresh grid history from authoritative daily Octopus records."""
        today = dt_util.now().date()
        self._grid_history = [
            {"date": day, "import_kwh": row.get("import_kwh"), "export_kwh": row.get("export_kwh")}
            for day, row in sorted(self._octopus_daily_records.items())
            if (today - timedelta(days=7)).isoformat() <= day <= today.isoformat()
        ][-8:]


    def _grid_history_payload(self) -> list[dict[str, Any]]:
        """Today and six preceding UK days from the same Octopus records."""
        today = dt_util.now().date()
        rows = []
        for i in range(7):
            day = (today - timedelta(days=i)).isoformat()
            source = self._octopus_daily_records.get(day, {})
            imported = source.get("import_kwh")
            exported = source.get("export_kwh")
            net = round(imported - exported, 2) if imported is not None and exported is not None else None
            rows.append({"date": day, "import_kwh": imported, "export_kwh": exported,
                         "net_kwh": net, "status": "In progress" if i == 0 else "Complete"})
        return rows

    async def _async_recover_house_history(self) -> None:
        """Serialize recovery; retry unavailable or late Recorder data hourly."""
        async with self._house_recovery_lock:
            now = dt_util.now()
            if (self._house_recovery_checked is not None
                    and now - self._house_recovery_checked < timedelta(hours=1)):
                return
            self._house_recovery_checked = now
            await self._async_read_house_history()

    async def _async_read_house_history(self) -> None:
        """Read only Hanchu; storage is the durable record after Recorder purges."""
        now = dt_util.now()
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder import history as recorder_history
            start = datetime.combine(HOUSE_HISTORY_START, time.min, tzinfo=now.tzinfo)
            job = partial(
                recorder_history.get_significant_states, self.hass,
                dt_util.as_utc(start), dt_util.as_utc(now),
                entity_ids=[self._hanchu_entity("daily_load_energy")],
                include_start_time_state=False, significant_changes_only=False,
                minimal_response=False, no_attributes=True,
            )
            history = await get_instance(self.hass).async_add_executor_job(job)
            recovered = recover_days(history.get(self._hanchu_entity("daily_load_energy"), []),
                                     now.date(), now.tzinfo)
        except Exception as err:
            _LOGGER.warning("Unable to recover Hanchu Recorder history: %s", err)
            return
        previous = dict(self._house_daily_history)
        previous_recovery = dict(self._house_recovery)
        for day, row in recovered.items():
            self._house_daily_history[day] = row["kwh"]
            self._house_recovery[day] = row
        try:
            await self._async_save_pv_history()
        except Exception:
            self._house_daily_history = previous
            self._house_recovery = previous_recovery
            raise

    async def _async_update_house_daily_history(self) -> None:
        """Persist validated daily-reset Hanchu house consumption, without estimates."""
        await self._async_recover_house_history()
        now = dt_util.now()
        today = now.date().isoformat()
        state = self.hass.states.get(self._hanchu_entity("daily_load_energy"))
        if (state is None or dt_util.as_local(state.last_updated).date() != now.date()
                or (now.hour == 0 and now.minute < 10)):
            return
        value = self._state_float(self._hanchu_entity("daily_load_energy"))
        if value is None or not math.isfinite(value) or value < 0:
            return
        value = round(value, 3)
        if self._house_daily_history.get(today) != value:
            self._house_daily_history[today] = value
            await self._async_save_pv_history()

    def _rolling_energy_history_summary(self) -> dict[str, Any]:
        """Today plus six prior UK dates, from validated daily source readings."""
        today = dt_util.now().date()
        house_rows = []
        tariff_rows = []
        for offset in range(7):
            day = (today - timedelta(days=offset)).isoformat()
            house = self._house_daily_history.get(day)
            source = self._octopus_daily_records.get(day, {})
            peak = source.get("peak_kwh")
            offpeak = source.get("offpeak_kwh")
            total = round(peak + offpeak, 3) if peak is not None and offpeak is not None else None
            house_rows.append({"date": day, "kwh": house, "in_progress": offset == 0,
                               **self._house_recovery.get(day, {})})
            tariff_rows.append({"date": day, "peak_kwh": peak, "offpeak_kwh": offpeak,
                                "total_kwh": total, "in_progress": offset == 0})
        complete_tariff = [row for row in tariff_rows if row["total_kwh"] is not None]
        return {
            "house_consumption_7d_history": house_rows,
            "electricity_tariff_7d_history": tariff_rows,
            "house_consumption_7d_total_kwh": round(sum(row["kwh"] for row in house_rows
                                                         if row["kwh"] is not None), 3),
            "electricity_peak_7d_total_kwh": round(sum(row["peak_kwh"] for row in complete_tariff), 3),
            "electricity_offpeak_7d_total_kwh": round(sum(row["offpeak_kwh"] for row in complete_tariff), 3),
            "electricity_combined_7d_total_kwh": round(sum(row["total_kwh"] for row in complete_tariff), 3),
            "house_consumption_7d_days_available": sum(row["kwh"] is not None for row in house_rows),
            "electricity_tariff_7d_days_available": len(complete_tariff),
        }

    def _grid_history_summary(self) -> dict[str, Any]:
        """Previous seven completed UK days, using only observed Octopus values."""
        today = dt_util.now().date()
        # Use the exact same seven dates as the visible history (today + six days).
        rows = [self._octopus_daily_records.get((today - timedelta(days=i)).isoformat(), {})
                for i in range(7)]
        imported = round(sum(row["import_kwh"] for row in rows if row.get("import_kwh") is not None), 2)
        exported = round(sum(row["export_kwh"] for row in rows if row.get("export_kwh") is not None), 2)
        return {"grid_history": self._grid_history_payload(),
                "grid_import_7d_kwh": imported, "grid_export_7d_kwh": exported,
                "grid_net_7d_kwh": round(imported - exported, 2)}


    async def _async_update_ev_history(self) -> None:
        """Track today's Zappi daily EV value, including downward reset corrections.

        Shortly after midnight the Zappi entity can briefly retain yesterday's
        final value before resetting to zero. Older versions only allowed the
        stored value to increase, which could leave that carried-over value
        frozen against a no-charge day. Today's stored value now follows the
        live daily-reset sensor in both directions.
        """
        entity = self.entry.data.get(CONF_DAILY_EV_ENTITY, DEFAULT_DAILY_EV_ENTITY)
        actual = self._state_float(entity)
        today = dt_util.now().date().isoformat()
        if actual is None:
            return

        actual = round(max(actual, 0.0), 2)
        row = next((x for x in self._ev_history if x.get("date") == today), None)
        changed = False

        if row is None:
            self._ev_history.append({"date": today, "kwh": actual})
            changed = True
        elif round(float(row.get("kwh") or 0.0), 2) != actual:
            row["kwh"] = actual
            changed = True

        self._ev_history = sorted(self._ev_history, key=lambda x: x.get("date", ""))[-7:]
        if changed:
            await self._async_save_pv_history()

    def _ev_day_value(self, days_ago: int) -> float | None:
        """Return EV charging energy for today (0) through six days ago."""
        target = (dt_util.now().date() - timedelta(days=days_ago)).isoformat()
        row = next((x for x in self._ev_history if x.get("date") == target), None)
        return row.get("kwh") if row else None

    def _ev_history_payload(self) -> list[dict[str, Any]]:
        return [
            {"date": (dt_util.now().date() - timedelta(days=i)).isoformat(), "kwh": self._ev_day_value(i)}
            for i in range(7)
        ]

    def _calculate_daily_energy_summary(self) -> dict[str, Any]:
        """Return today's energy counters, keeping EV charging separate from house load."""
        cfg = self.entry.data
        pv_entity = cfg.get(CONF_DAILY_PV_ENTITY, self._hanchu_entity("daily_pv_energy"))
        ev_entity = cfg.get(CONF_DAILY_EV_ENTITY, DEFAULT_DAILY_EV_ENTITY)

        def value(entity_id: str) -> float | None:
            raw = self._state_float(entity_id)
            return round(raw, 3) if raw is not None else None

        charged = value(self._hanchu_entity("daily_charge_energy"))
        discharged = value(self._hanchu_entity("daily_discharge_energy"))
        battery_net = None
        if charged is not None and discharged is not None:
            battery_net = round(discharged - charged, 3)

        return {
            "daily_solar_generation_kwh": value(pv_entity),
            "daily_house_consumption_kwh": value(self._hanchu_entity("daily_load_energy")),
            "daily_grid_import_kwh": self._octopus_daily_records.get(dt_util.now().date().isoformat(), {}).get("import_kwh"),
            "daily_grid_export_kwh": self._octopus_daily_records.get(dt_util.now().date().isoformat(), {}).get("export_kwh"),
            "daily_battery_charged_kwh": charged,
            "daily_battery_discharged_kwh": discharged,
            "daily_battery_net_kwh": battery_net,
            "daily_ev_charging_kwh": value(ev_entity),
            "daily_battery_soc_pct": value(cfg.get(CONF_BATTERY_SOC_ENTITY, self._hanchu_entity("battery_soc"))),
        }


    def _battery_charge_source_live(self) -> dict[str, Any]:
        """Determine current battery charge source from validated Hanchu power flows."""
        battery = self._state_float(self.entry.data.get(CONF_BATTERY_POWER_ENTITY, self._hanchu_entity("battery_power"))) or 0.0
        pv = self._state_float(self._hanchu_entity("pv_power")) or 0.0
        load = self._state_float(self._hanchu_entity("load_power")) or 0.0
        grid = self._state_float(self._hanchu_entity("grid_power")) or 0.0
        threshold = 50.0
        if battery <= threshold:
            return {"battery_charge_source": "Not Charging", "solar_to_battery_w": 0.0, "grid_to_battery_w": 0.0}
        solar_surplus = max(pv - load, 0.0)
        solar_to_battery = min(battery, solar_surplus)
        grid_to_battery = max(battery - solar_to_battery, 0.0) if grid > threshold else 0.0
        # Reconcile any small residual to the active source.
        residual = max(battery - solar_to_battery - grid_to_battery, 0.0)
        if residual > 0:
            if grid > threshold:
                grid_to_battery += residual
            else:
                solar_to_battery += residual
        has_solar = solar_to_battery > threshold
        has_grid = grid_to_battery > threshold
        source = "Solar + Grid" if has_solar and has_grid else "Solar" if has_solar else "Grid" if has_grid else "Charging"
        return {
            "battery_charge_source": source,
            "solar_to_battery_w": round(solar_to_battery, 0),
            "grid_to_battery_w": round(grid_to_battery, 0),
        }

    async def _async_update_battery_charge_sources(self) -> None:
        """Integrate live solar/grid battery-charge power into kWh totals.

        v1.1.21 apportioned only increments of Hanchu's Daily Charge Energy
        counter.  That counter can update relatively coarsely, leaving the
        source-energy sensors at 0.00 kWh for long periods even though the
        live charge-source sensor is changing correctly.

        v1.1.21.1 instead integrates the validated live source powers on each
        relevant state change (and on the normal coordinator refresh).  The
        first sample after startup only seeds the tracker, so no energy is
        invented across a Home Assistant restart/offline gap.
        """
        now = dt_util.now()
        today = now.date().isoformat()
        live = self._battery_charge_source_live()
        current_solar_w = float(live.get("solar_to_battery_w") or 0.0)
        current_grid_w = float(live.get("grid_to_battery_w") or 0.0)

        # First sample, or first sample of a new day: seed only.  This avoids
        # attributing an unknown interval before the integration was running.
        if self._charge_tracking_last_ts is None or self._charge_tracking_day != today:
            self._charge_tracking_last_ts = now
            self._charge_tracking_day = today
            self._charge_tracking_solar_w = current_solar_w
            self._charge_tracking_grid_w = current_grid_w
            return

        elapsed_s = max((now - self._charge_tracking_last_ts).total_seconds(), 0.0)
        # A normal coordinator refresh is every 30 minutes.  Cap a single
        # interval at 35 minutes to protect against clock jumps or a stalled
        # event stream without discarding ordinary refresh intervals.
        elapsed_s = min(elapsed_s, 35.0 * 60.0)

        changed = False
        if elapsed_s > 0:
            solar_delta = self._charge_tracking_solar_w * elapsed_s / 3_600_000.0
            grid_delta = self._charge_tracking_grid_w * elapsed_s / 3_600_000.0
            if solar_delta > 0 or grid_delta > 0:
                row = self._battery_charge_sources.setdefault(
                    today, {"solar_kwh": 0.0, "grid_kwh": 0.0}
                )
                row["solar_kwh"] = round(
                    float(row.get("solar_kwh", 0.0)) + solar_delta, 6
                )
                row["grid_kwh"] = round(
                    float(row.get("grid_kwh", 0.0)) + grid_delta, 6
                )
                changed = True

        self._charge_tracking_last_ts = now
        self._charge_tracking_day = today
        self._charge_tracking_solar_w = current_solar_w
        self._charge_tracking_grid_w = current_grid_w

        # Keep roughly 62 days, enough to span current/previous months.
        cutoff = (now.date() - timedelta(days=62)).isoformat()
        trimmed = {d: v for d, v in self._battery_charge_sources.items() if d >= cutoff}
        if len(trimmed) != len(self._battery_charge_sources):
            self._battery_charge_sources = trimmed
            changed = True

        if changed:
            await self._async_save_pv_history()

    def _battery_charge_source_summary(self) -> dict[str, Any]:
        today = dt_util.now().date().isoformat()
        month = today[:7]
        row = self._battery_charge_sources.get(today, {})
        solar_today = float(row.get("solar_kwh", 0.0))
        grid_today = float(row.get("grid_kwh", 0.0))
        tracked_today = solar_today + grid_today
        solar_month = sum(float(v.get("solar_kwh", 0.0)) for d, v in self._battery_charge_sources.items() if d.startswith(month))
        grid_month = sum(float(v.get("grid_kwh", 0.0)) for d, v in self._battery_charge_sources.items() if d.startswith(month))
        live = self._battery_charge_source_live()
        return {
            **live,
            "battery_charged_from_solar_today_kwh": round(solar_today, 3),
            "battery_charged_from_grid_today_kwh": round(grid_today, 3),
            "battery_solar_charge_pct_today": round(solar_today / tracked_today * 100, 1) if tracked_today > 0 else None,
            "battery_grid_charge_pct_today": round(grid_today / tracked_today * 100, 1) if tracked_today > 0 else None,
            "battery_charged_from_solar_month_kwh": round(solar_month, 3),
            "battery_charged_from_grid_month_kwh": round(grid_month, 3),
            "battery_charge_source_tracked_kwh_today": round(tracked_today, 3),
        }

    def async_start_live_tracking(self) -> None:
        """Refresh battery-derived values immediately when Hanchu state changes."""
        if self._unsub_live is not None:
            return

        cfg = self.entry.data
        entities = [
            cfg.get(CONF_BATTERY_SOC_ENTITY, self._hanchu_entity("battery_soc")),
            cfg.get(CONF_BATTERY_POWER_ENTITY, self._hanchu_entity("battery_power")),
            cfg.get(CONF_DAILY_PV_ENTITY, self._hanchu_entity("daily_pv_energy")),
            cfg.get(CONF_DAILY_EV_ENTITY, DEFAULT_DAILY_EV_ENTITY),
            self._electricity_import_entity,
            self._electricity_entity("current_accumulative_consumption_off_peak"),
            self._electricity_entity("current_total_export"),
            self._hanchu_entity("daily_load_energy"),
            self._hanchu_entity("daily_charge_energy"),
            self._hanchu_entity("daily_discharge_energy"),
            self._hanchu_entity("grid_power"),
            self._hanchu_entity("pv_power"),
            self._hanchu_entity("load_power"),
        ]

        async def _state_changed(event) -> None:
            if self.data is None:
                return
            updated = dict(self.data)
            await self._async_update_battery_charge_sources()
            updated.update(self._calculate_live_battery_data())
            await self._async_update_pv_history(updated.get("forecast", []))
            # v1.1.23.9: use the last fresh Open-Meteo hourly rows when a live
            # Hanchu update also updates today's actual PV.  Re-feeding the
            # persisted projected profile here could leave a same-day repair
            # permanently empty if the daily baseline existed without its
            # hourly_profile.
            await self._async_update_hourly_pv(self._latest_hourly_forecast_rows)
            await self._async_update_ev_history()
            await self._async_update_battery_history()
            await self._async_update_house_daily_history()
            updated.update(self._rolling_energy_history_summary())
            updated.update(self._calculate_pv_comparison(updated.get("forecast", [])))
            updated["pv_history"] = self._pv_history_payload()
            updated["hourly_pv_today"] = self._hourly_pv_payload()
            updated["hourly_pv_baseline_captured"] = bool(self._hourly_pv.get("baseline_captured", False))
            updated.update(self._calculate_solar_forecast_intelligence())
            updated["ev_history"] = self._ev_history_payload()
            updated.update(self._battery_history_summary())
            updated.update(self._grid_history_summary())
            updated.update(self._calculate_daily_energy_summary())
            updated.update(self._battery_charge_source_summary())
            # Live pushes must not restart the coordinator's 30-minute
            # polling clock; otherwise frequent Hanchu changes can postpone
            # Open-Meteo and monthly meter refreshes indefinitely.
            self.data = updated
            self.async_update_listeners()

        self._unsub_live = async_track_state_change_event(
            self.hass, [entity for entity in entities if entity], _state_changed
        )

    def async_start_energy_tracking(self) -> None:
        """Refresh Octopus totals independently of forecast API availability."""
        if self._unsub_energy_timer is not None:
            return

        async def _refresh(_event=None) -> None:
            if self.data is None or self._energy_refresh_lock.locked():
                return
            now = dt_util.utcnow()
            if (self._last_energy_refresh_attempt is not None
                    and now - self._last_energy_refresh_attempt < timedelta(minutes=5)):
                return
            self._last_energy_refresh_attempt = now
            try:
                async with self._energy_refresh_lock:
                    monthly = await self._calculate_monthly_energy_costs()
                    self._last_successful_energy_update = dt_util.utcnow().isoformat()
                    monthly["last_successful_energy_update"] = self._last_successful_energy_update
                    monthly["forecast_refresh_status"] = self._forecast_refresh_status
                    updated = dict(self.data)
                    updated.update(monthly)
                    # Inform entities without resetting scheduled forecast polls.
                    self.data = updated
                    self.async_update_listeners()
            except Exception as err:
                _LOGGER.warning("Independent Octopus energy refresh failed: %s", err)

        # Independent backup every 15 minutes, with a day-boundary update
        # and a coalesced refresh when live Octopus counters change.
        self._unsub_energy_timer = async_track_time_interval(
            self.hass, _refresh, timedelta(minutes=15)
        )
        self._unsub_energy_midnight = async_track_time_change(
            self.hass, _refresh, hour=0, minute=5, second=0
        )
        gas_entity = self._gas_import_entity
        meter_entities = [entity for entity in (
            self._electricity_import_entity,
            self._electricity_entity("current_accumulative_consumption_off_peak"),
            gas_entity,
        ) if entity]
        if meter_entities:
            self._unsub_energy_events = async_track_state_change_event(
                self.hass, meter_entities, _refresh,
            )

    def async_stop_energy_tracking(self) -> None:
        """Remove meter refresh listeners when the entry is unloaded."""
        for name in ("_unsub_energy_timer", "_unsub_energy_midnight", "_unsub_energy_events"):
            unsubscribe = getattr(self, name)
            if unsubscribe is not None:
                unsubscribe()
                setattr(self, name, None)

    def async_stop_live_tracking(self) -> None:
        """Stop live Hanchu state tracking."""
        if self._unsub_live is not None:
            self._unsub_live()
            self._unsub_live = None

    def _calculate_live_battery_data(self) -> dict[str, Any]:
        """Calculate battery-derived values from current Home Assistant states."""
        cfg = self.entry.data
        soc_entity = cfg.get(CONF_BATTERY_SOC_ENTITY, self._hanchu_entity("battery_soc"))
        power_entity = cfg.get(CONF_BATTERY_POWER_ENTITY, self._hanchu_entity("battery_power"))

        soc = self._state_float(soc_entity)
        if soc is None and soc_entity != self._hanchu_entity("battery_soc"):
            soc_entity = self._hanchu_entity("battery_soc")
            soc = self._state_float(soc_entity)

        battery_power = self._state_float(power_entity)
        if battery_power is None and power_entity != self._hanchu_entity("battery_power"):
            power_entity = self._hanchu_entity("battery_power")
            battery_power = self._state_float(power_entity)

        exportable_kwh = None
        start_time = None
        if soc is not None:
            reserve = float(cfg[CONF_RESERVE_SOC])
            capacity = float(cfg[CONF_BATTERY_CAPACITY])
            discharge_kw = float(cfg[CONF_DISCHARGE_KW])
            exportable_kwh = max(capacity * ((soc - reserve) / 100.0), 0.0)

            if exportable_kwh > 0 and discharge_kw > 0:
                hh, mm = [int(x) for x in str(cfg[CONF_EXPORT_END]).split(":", 1)]
                local_now = dt_util.now()
                end_local = datetime.combine(
                    local_now.date(), time(hh, mm), tzinfo=local_now.tzinfo
                )
                mins = exportable_kwh / discharge_kw * 60.0
                calculated = end_local - timedelta(minutes=mins)
                start_time = calculated.isoformat()

        return {
            "soc": soc,
            "battery_power_w": battery_power,
            "exportable_kwh": round(exportable_kwh, 2)
            if exportable_kwh is not None
            else None,
            "export_start": start_time,
            "battery_soc_entity": soc_entity,
            "battery_soc_raw": self._state_raw(soc_entity),
            "battery_power_entity": power_entity,
            "battery_power_raw": self._state_raw(power_entity),
            "last_update": dt_util.utcnow().isoformat(),
        }


    def _calculate_pv_comparison(self, forecast: list[dict[str, Any]]) -> dict[str, Any]:
        """Return today's projected-versus-actual PV comparison.

        Completed-day history is retained by Home Assistant Recorder from these
        sensors, avoiding fragile custom files inside the integration.
        """
        cfg = self.entry.data
        pv_entity = cfg.get(CONF_DAILY_PV_ENTITY, self._hanchu_entity("daily_pv_energy"))
        actual = self._state_float(pv_entity)
        if actual is None and pv_entity != self._hanchu_entity("daily_pv_energy"):
            pv_entity = self._hanchu_entity("daily_pv_energy")
            actual = self._state_float(pv_entity)

        # v1.1.23.2: use the persisted 05:00 daily baseline once it has been
        # captured.  The live Open-Meteo forecast may change throughout the day,
        # but the Daily Solar Performance comparison must remain against the
        # official frozen baseline and must survive Home Assistant restarts.
        today = dt_util.now().date().isoformat()
        daily_row = next((x for x in self._pv_history if x.get("date") == today), None)
        authoritative = self._solar_baselines.get(today)
        if authoritative and authoritative.get("projected_kwh") is not None:
            projected = authoritative.get("projected_kwh")
        elif daily_row and daily_row.get("projected_kwh") is not None:
            # Legacy/mid-day-upgrade fallback: keep the existing value stable.
            projected = daily_row.get("projected_kwh")
        else:
            projected = forecast[0].get("forecast_kwh") if forecast else None
        variance = None
        progress_pct = None
        if projected is not None and actual is not None:
            variance = actual - float(projected)
            if float(projected) > 0:
                progress_pct = actual / float(projected) * 100.0

        return {
            "pv_projected_today_kwh": round(float(projected), 2) if projected is not None else None,
            "pv_actual_today_kwh": round(actual, 2) if actual is not None else None,
            "pv_variance_today_kwh": round(variance, 2) if variance is not None else None,
            "pv_forecast_progress_pct": round(progress_pct, 1) if progress_pct is not None else None,
            "daily_pv_entity": pv_entity,
            "daily_pv_raw": self._state_raw(pv_entity),
        }

    def _is_offpeak(self, local_dt: datetime) -> bool:
        """Return True when a local timestamp falls in the configured overnight window."""
        cfg = self.entry.data
        sh, sm = [int(x) for x in str(cfg.get(CONF_OFFPEAK_START, DEFAULT_OFFPEAK_START)).split(":", 1)]
        eh, em = [int(x) for x in str(cfg.get(CONF_OFFPEAK_END, DEFAULT_OFFPEAK_END)).split(":", 1)]
        start = time(sh, sm)
        end = time(eh, em)
        current = local_dt.timetz().replace(tzinfo=None)
        if start > end:  # overnight, e.g. 23:30 -> 05:30
            return current >= start or current < end
        return start <= current < end

    async def _recorder_states_current_month(self, entity_ids: list[str], attribute_entities: set[str] | None = None) -> dict[str, list[Any]]:
        """Read Recorder states from the first of the local month to now."""
        local_now = dt_util.now()
        start_local = datetime.combine(local_now.date().replace(day=1), time.min, tzinfo=local_now.tzinfo)
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder import history as recorder_history
            job = partial(
                recorder_history.get_significant_states,
                self.hass,
                start_local,
                local_now,
                entity_ids=entity_ids,
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=False if attribute_entities else True,
            )
            result = await get_instance(self.hass).async_add_executor_job(job)
            return result if isinstance(result, dict) else {}
        except Exception as err:
            _LOGGER.warning("Unable to read monthly energy history from Recorder: %s", err)
            return {}

    @staticmethod
    def _numeric_state(state: Any) -> float | None:
        try:
            return float(str(state.state).strip().replace(",", "."))
        except (AttributeError, TypeError, ValueError):
            return None

    def _without_midnight_carryover(self, states: list[Any]) -> list[Any]:
        """Remove a previous-day daily total recorded at local midnight before reset.

        Some daily-reset sensors publish yesterday's final value at exactly 00:00
        and only reset to a small value a few seconds/minutes later. Home Assistant
        timestamps that carry-over in the new calendar day. If retained, a monthly
        sum of daily maxima incorrectly counts yesterday's total again.
        """
        ordered = sorted(
            states,
            key=lambda st: getattr(st, "last_updated", None)
            or getattr(st, "last_changed", dt_util.utcnow()),
        )
        skip_ids: set[int] = set()
        by_day: dict[str, list[tuple[int, Any, datetime, float]]] = {}
        month_start = dt_util.now().date().replace(day=1)
        today = dt_util.now().date()
        for idx, st in enumerate(ordered):
            value = self._numeric_state(st)
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            if value is None or stamp is None or value < 0:
                continue
            local = dt_util.as_local(stamp)
            if local.date() < month_start or local.date() > today:
                continue
            by_day.setdefault(local.date().isoformat(), []).append((idx, st, local, value))

        for entries in by_day.values():
            if len(entries) < 2:
                continue
            first_idx, _first_st, first_local, first_value = entries[0]
            _next_idx, _next_st, next_local, next_value = entries[1]
            # Carry-over signature: first reading is at/just after midnight, followed
            # quickly by a substantial reset to a lower value.
            seconds = (next_local - first_local).total_seconds()
            near_midnight = first_local.hour == 0 and first_local.minute <= 5
            quick_reset = 0 <= seconds <= 600 and next_value < first_value * 0.5
            if near_midnight and quick_reset:
                skip_ids.add(first_idx)

        return [st for idx, st in enumerate(ordered) if idx not in skip_ids]

    def _daily_reset_total(self, states: list[Any]) -> float:
        """Sum daily maxima for a monotonically rising daily-reset energy sensor."""
        states = self._without_midnight_carryover(states)
        maxima: dict[str, float] = {}
        for st in states:
            value = self._numeric_state(st)
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            if value is None or stamp is None or value < 0:
                continue
            local = dt_util.as_local(stamp)
            # Recorder include_start_time_state can return a state whose timestamp
            # predates the requested month. Never count that carry-in state.
            month_start = dt_util.now().date().replace(day=1)
            if local.date() < month_start or local.date() > dt_util.now().date():
                continue
            day = local.date().isoformat()
            maxima[day] = max(maxima.get(day, 0.0), value)
        return round(sum(maxima.values()), 3)

    def _split_daily_reset_by_tariff(self, states: list[Any]) -> tuple[float, float]:
        """Split positive increments of a daily-reset sensor into peak/off-peak buckets."""
        peak = 0.0
        offpeak = 0.0
        previous_by_day: dict[str, float] = {}
        ordered = sorted(states, key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()))
        for st in ordered:
            value = self._numeric_state(st)
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            if value is None or stamp is None or value < 0:
                continue
            local = dt_util.as_local(stamp)
            month_start = dt_util.now().date().replace(day=1)
            if local.date() < month_start or local.date() > dt_util.now().date():
                continue
            day = local.date().isoformat()
            prev = previous_by_day.get(day)
            previous_by_day[day] = value
            if prev is None:
                # The first state may already contain usage since midnight. Attribute it to
                # its timestamp rather than losing it; subsequent states use true deltas.
                delta = value
            else:
                delta = value - prev
                if delta < 0:  # reset/correction
                    delta = value
            if delta <= 0:
                continue
            if self._is_offpeak(local):
                offpeak += delta
            else:
                peak += delta
        return round(peak, 3), round(offpeak, 3)

    def _offpeak_at(self, stamp: datetime, tariff_states: list[Any]) -> bool:
        """Return Octopus off-peak state at stamp; fall back to configured clock window."""
        chosen = None
        for st in tariff_states:
            changed = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            if changed is None or changed > stamp:
                continue
            chosen = str(getattr(st, "state", "")).lower()
        if chosen == "on":
            return True
        if chosen == "off":
            return False
        return self._is_offpeak(dt_util.as_local(stamp))

    def _split_daily_reset_by_octopus_tariff(
        self, states: list[Any], tariff_states: list[Any]
    ) -> tuple[float, float]:
        """Split positive energy increments using Octopus off_peak history."""
        peak = 0.0
        offpeak = 0.0
        previous_by_day: dict[str, float] = {}
        states = self._without_midnight_carryover(states)
        tariff_states = sorted(
            tariff_states,
            key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()),
        )
        ordered = sorted(
            states,
            key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()),
        )
        for st in ordered:
            value = self._numeric_state(st)
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            if value is None or stamp is None or value < 0:
                continue
            local = dt_util.as_local(stamp)
            month_start = dt_util.now().date().replace(day=1)
            if local.date() < month_start or local.date() > dt_util.now().date():
                continue
            day = local.date().isoformat()
            prev = previous_by_day.get(day)
            previous_by_day[day] = value
            if prev is None:
                delta = value
            else:
                delta = value - prev
                if delta < 0:
                    delta = value
            if delta <= 0:
                continue
            if self._offpeak_at(stamp, tariff_states):
                offpeak += delta
            else:
                peak += delta
        return round(peak, 3), round(offpeak, 3)

    def _daily_maxima(self, states: list[Any]) -> dict[str, float]:
        """Return the maximum daily-reset sensor value for each day this month."""
        states = self._without_midnight_carryover(states)
        maxima: dict[str, float] = {}
        month_start = dt_util.now().date().replace(day=1)
        today = dt_util.now().date()
        for st in states:
            value = self._numeric_state(st)
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            if value is None or stamp is None or value < 0:
                continue
            local = dt_util.as_local(stamp)
            if local.date() < month_start or local.date() > today:
                continue
            day = local.date().isoformat()
            maxima[day] = max(maxima.get(day, 0.0), value)
        return {day: round(value, 3) for day, value in maxima.items()}

    def _split_daily_reset_by_octopus_tariff_by_day(
        self, states: list[Any], tariff_states: list[Any]
    ) -> dict[str, dict[str, float]]:
        """Split daily-reset electricity consumption into peak/off-peak per day."""
        result: dict[str, dict[str, float]] = {}
        previous_by_day: dict[str, float] = {}
        states = self._without_midnight_carryover(states)
        tariff_states = sorted(
            tariff_states,
            key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()),
        )
        ordered = sorted(
            states,
            key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()),
        )
        month_start = dt_util.now().date().replace(day=1)
        today = dt_util.now().date()
        for st in ordered:
            value = self._numeric_state(st)
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            if value is None or stamp is None or value < 0:
                continue
            local = dt_util.as_local(stamp)
            if local.date() < month_start or local.date() > today:
                continue
            day = local.date().isoformat()
            prev = previous_by_day.get(day)
            previous_by_day[day] = value
            delta = value if prev is None else value - prev
            if delta < 0:
                delta = value
            if delta <= 0:
                continue
            buckets = result.setdefault(day, {"peak": 0.0, "offpeak": 0.0})
            if self._offpeak_at(stamp, tariff_states):
                buckets["offpeak"] += delta
            else:
                buckets["peak"] += delta

        # Reconcile each day's tariff split to the authoritative daily maximum.
        maxima = self._daily_maxima(states)
        for day, total in maxima.items():
            buckets = result.setdefault(day, {"peak": 0.0, "offpeak": 0.0})
            split_total = buckets["peak"] + buckets["offpeak"]
            if total > 0 and split_total > 0:
                scale = total / split_total
                buckets["peak"] *= scale
                buckets["offpeak"] = max(total - buckets["peak"], 0.0)
            buckets["peak"] = round(buckets["peak"], 3)
            buckets["offpeak"] = round(buckets["offpeak"], 3)
        return result

    def _octopus_half_hourly_by_day(
        self, states: list[Any], tariff_states: list[Any], dispatch_states: list[Any],
        rate_states: list[Any] | None = None,
    ) -> dict[str, dict[str, float]]:
        """Use actual dated Octopus import intervals, not HA update timestamps.

        An Octopus previous-consumption sensor can publish the same intervals
        multiple times as the meter backfills. Deduplicate by interval start.
        Only use complete, valid intervals. A missing historical day is not
        interpreted as zero consumption. Only full, contiguous local calendar
        days can override the legacy sensor; partial days must not replace
        whole-day totals.
        """
        from datetime import timezone

        month_start = dt_util.now().date().replace(day=1)
        today = dt_util.now().date()
        intervals: dict[str, tuple[datetime, datetime, float]] = {}

        def collect(obj: Any) -> None:
            if isinstance(obj, dict):
                if {"start", "end", "consumption"} <= obj.keys():
                    try:
                        start = datetime.fromisoformat(str(obj["start"]).replace("Z", "+00:00"))
                        end = datetime.fromisoformat(str(obj["end"]).replace("Z", "+00:00"))
                        kwh = float(obj["consumption"])
                        if (start.tzinfo is not None and end.tzinfo is not None
                                and 0 < (end - start).total_seconds() <= 1800
                                and 0 <= kwh < 100
                                and month_start <= dt_util.as_local(start).date() <= today
                                and end <= dt_util.utcnow()):
                            intervals[start.astimezone(timezone.utc).isoformat()] = (start, end, kwh)
                    except (TypeError, ValueError, OverflowError):
                        pass
                else:
                    for child in obj.values():
                        if isinstance(child, (list, dict)):
                            collect(child)
            elif isinstance(obj, list):
                for child in obj:
                    collect(child)

        for st in states:
            collect(getattr(st, "attributes", {}))

        def state_at(when: datetime, events: list[Any]) -> str | None:
            result = None
            for event in events:
                changed = getattr(event, "last_updated", None) or getattr(event, "last_changed", None)
                if changed is not None and changed <= when:
                    value = str(getattr(event, "state", "")).lower()
                    if value in ("on", "off"):
                        result = value
            return result

        tariff_states = sorted(
            tariff_states,
            key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()),
        )
        rate_states = sorted(
            rate_states or [],
            key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()),
        )

        def rate_at(when: datetime) -> float | None:
            latest = None
            for event in rate_states:
                stamp = getattr(event, "last_updated", None) or getattr(event, "last_changed", None)
                if stamp is not None and stamp <= when:
                    latest = self._numeric_state(event)
                elif stamp is not None and stamp > when:
                    break
            return latest

        dispatch_states = sorted(
            dispatch_states,
            key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()),
        )
        # Verify uninterrupted coverage from local midnight to the following
        # midnight. UTC boundaries also handle 23/25-hour UK DST days.
        by_day: dict[str, list[tuple[datetime, datetime, float]]] = {}
        for start, end, kwh in intervals.values():
            by_day.setdefault(dt_util.as_local(start).date().isoformat(), []).append((start, end, kwh))

        complete_days: set[str] = set()
        for day, rows in by_day.items():
            date = datetime.fromisoformat(day).date()
            if date >= today:
                continue  # Today's readings are necessarily incomplete.
            expected_start = datetime.combine(date, time.min, tzinfo=dt_util.now().tzinfo).astimezone(timezone.utc)
            expected_end = datetime.combine(date + timedelta(days=1), time.min, tzinfo=dt_util.now().tzinfo).astimezone(timezone.utc)
            cursor = expected_start
            for start, end, _ in sorted(rows, key=lambda item: item[0]):
                if start.astimezone(timezone.utc) != cursor:
                    break
                cursor = end.astimezone(timezone.utc)
            if cursor == expected_end:
                complete_days.add(day)

        totals: dict[str, dict[str, float]] = {}
        for start, end, kwh in sorted(intervals.values(), key=lambda item: item[0]):
            local = dt_util.as_local(start)
            day = local.date().isoformat()
            if day not in complete_days:
                continue
            # Recorded Octopus tariff state is authoritative when known.
            # For dates before its Recorder history, use recorded unit rates,
            # then the overnight window and dispatch signal as a fallback.
            tariff_state = state_at(start, tariff_states)
            if tariff_state in ("on", "off"):
                offpeak = tariff_state == "on"
            else:
                actual_rate = rate_at(start)
                peak_rate = float(self.entry.data.get(CONF_ELECTRICITY_PEAK_RATE, DEFAULT_ELECTRICITY_PEAK_RATE)) / 100.0
                cheap_rate = float(self.entry.data.get(CONF_ELECTRICITY_OFFPEAK_RATE, DEFAULT_ELECTRICITY_OFFPEAK_RATE)) / 100.0
                if actual_rate is not None and abs(actual_rate - cheap_rate) <= 0.015:
                    offpeak = True
                elif actual_rate is not None and abs(actual_rate - peak_rate) <= 0.015:
                    offpeak = False
                else:
                    offpeak = self._is_offpeak(local) or state_at(start, dispatch_states) == "on"
            row = totals.setdefault(day, {"peak": 0.0, "offpeak": 0.0})
            row["offpeak" if offpeak else "peak"] += kwh
        return {
            day: {"peak": round(row["peak"], 3), "offpeak": round(row["offpeak"], 3)}
            for day, row in totals.items()
        }

    def _octopus_export_half_hourly_by_day(self, states: list[Any]) -> dict[str, float]:
        """Sum dated Octopus export intervals only for fully covered days.

        Each interval is keyed by its UTC start, so repeated API publications
        and corrections do not double-count. Incomplete days are left for the
        existing Hanchu daily counter rather than being mistaken for zero.
        """
        from datetime import timezone

        now = dt_util.now()
        first = now.date().replace(day=1)
        intervals: dict[str, tuple[datetime, datetime, float]] = {}

        def collect(obj: Any) -> None:
            if isinstance(obj, dict):
                if {"start", "end", "consumption"} <= obj.keys():
                    try:
                        start = datetime.fromisoformat(str(obj["start"]).replace("Z", "+00:00"))
                        end = datetime.fromisoformat(str(obj["end"]).replace("Z", "+00:00"))
                        kwh = float(obj["consumption"])
                        if (start.tzinfo is not None and end.tzinfo is not None
                                and (end - start).total_seconds() == 1800
                                and 0 <= kwh < 100
                                and first <= dt_util.as_local(start).date() <= now.date()
                                and end <= dt_util.utcnow()):
                            intervals[start.astimezone(timezone.utc).isoformat()] = (start, end, kwh)
                    except (TypeError, ValueError, OverflowError):
                        pass
                else:
                    for child in obj.values():
                        if isinstance(child, (list, dict)):
                            collect(child)
            elif isinstance(obj, list):
                for child in obj:
                    collect(child)

        for state in states:
            collect(getattr(state, "attributes", {}))

        by_day: dict[str, list[tuple[datetime, datetime, float]]] = {}
        for start, end, kwh in intervals.values():
            by_day.setdefault(dt_util.as_local(start).date().isoformat(), []).append((start, end, kwh))

        complete: dict[str, float] = {}
        for day, rows in by_day.items():
            rows.sort(key=lambda item: item[0])
            date = datetime.fromisoformat(day).date()
            midnight = datetime.combine(date, time.min, tzinfo=now.tzinfo)
            next_midnight = datetime.combine(date + timedelta(days=1), time.min, tzinfo=now.tzinfo)
            # Check actual contiguous coverage, including UK daylight-saving days.
            expected_start = midnight.astimezone(timezone.utc)
            expected_end = next_midnight.astimezone(timezone.utc)
            if rows[0][0].astimezone(timezone.utc) != expected_start:
                continue
            cursor = expected_start
            valid = True
            for start, end, _ in rows:
                if start.astimezone(timezone.utc) != cursor:
                    valid = False
                    break
                cursor = end.astimezone(timezone.utc)
            if not valid:
                continue
            if date < now.date() and cursor == expected_end:
                complete[day] = round(sum(row[2] for row in rows), 3)
            elif date == now.date() and cursor <= dt_util.utcnow() and (dt_util.utcnow() - cursor) <= timedelta(hours=2):
                # Only accept today's complete-to-latest interval data if the
                # latest meter interval is reasonably recent.
                complete[day] = round(sum(row[2] for row in rows), 3)
        return complete

    def _octopus_gas_half_hourly_by_day(self, states: list[Any]) -> dict[str, float]:
        """Sum dated Octopus gas kWh intervals, deduplicating revised records.

        The gas API includes both consumption_m3 and consumption_kwh; only the
        latter is appropriate for the billing dashboard. Historical Recorder
        attributes may be incomplete, so this is a sum of *available* intervals.
        """
        from datetime import timezone

        first = dt_util.now().date().replace(day=1)
        today = dt_util.now().date()
        intervals: dict[str, tuple[datetime, float]] = {}

        def collect(obj: Any) -> None:
            if isinstance(obj, dict):
                if {"start", "end", "consumption_kwh"} <= obj.keys():
                    try:
                        start = datetime.fromisoformat(str(obj["start"]).replace("Z", "+00:00"))
                        end = datetime.fromisoformat(str(obj["end"]).replace("Z", "+00:00"))
                        value = float(obj["consumption_kwh"])
                        if (start.tzinfo is not None and end.tzinfo is not None
                                and 0 < (end - start).total_seconds() <= 1800
                                and 0 <= value < 100
                                and first <= dt_util.as_local(start).date() <= today
                                and end <= dt_util.utcnow()):
                            intervals[start.astimezone(timezone.utc).isoformat()] = (start, value)
                    except (TypeError, ValueError, OverflowError):
                        pass
                else:
                    for child in obj.values():
                        if isinstance(child, (dict, list)):
                            collect(child)
            elif isinstance(obj, list):
                for child in obj:
                    collect(child)

        for state in states:
            collect(getattr(state, "attributes", {}))

        totals: dict[str, float] = {}
        for start, value in intervals.values():
            day = dt_util.as_local(start).date().isoformat()
            totals[day] = totals.get(day, 0.0) + value
        return {day: round(value, 3) for day, value in totals.items()}

    def _daily_energy_usage_payload(
        self, electricity: dict[str, dict[str, float]], gas: dict[str, float]
    ) -> list[dict[str, Any]]:
        """Expose the same dated readings used for the monthly totals.

        None means no usable source record for that day; a genuine recorded
        zero remains 0. This distinction matters for charting and outages.
        """
        now_local = dt_util.now()
        first = now_local.date().replace(day=1)
        rows: list[dict[str, Any]] = []
        for day_number in range(1, now_local.day + 1):
            day_date = first.replace(day=day_number)
            day = day_date.isoformat()
            local_midnight = datetime.combine(day_date, time.min, tzinfo=now_local.tzinfo)
            buckets = electricity.get(day)
            gas_value = gas.get(day)
            rows.append({
                "date": day,
                "day": day_number,
                "timestamp": int(local_midnight.timestamp() * 1000),
                "electricity_peak_kwh": round(float(buckets["peak"]), 3) if buckets is not None else None,
                "electricity_offpeak_kwh": round(float(buckets["offpeak"]), 3) if buckets is not None else None,
                "gas_kwh": round(float(gas_value), 3) if gas_value is not None else None,
                "complete": day_date < now_local.date(),
            })
        return rows

    async def _electricity_month_days(self, source: str, states: list[Any],
                                     tariff_states: list[Any]) -> dict[str, dict[str, float]]:
        """Merge recovered CSV days, retained daily records, and live Recorder data.

        CSV daily maxima restore older October 2026 data purged by Recorder. Their
        peak/off-peak split is an *estimate* using the configured clock window,
        because the CSVs do not contain the historical Octopus tariff states.
        Current Recorder samples use the actual Off Peak binary history where
        available. Persist all observed days to survive the Recorder purge.
        """
        month = dt_util.now().strftime("%Y-%m")
        today = dt_util.now().date().isoformat()
        cache = self._electricity_daily_cache[source]
        changed = False
        if month == "2026-10":
            for day, values in OCTOBER_2026_CSV_DAILY_MAXIMA[source].items():
                if day not in cache and day < today:
                    # Historical peak/off-peak details before the binary sensor
                    # existed cannot be recovered from daily maxima alone.
                    # Use the recorded Octopus history when available below.
                    cache[day] = {**values, "source": "csv_estimate"}
                    changed = True
        # The CSVs contain daily cumulative totals, not a reliable historical
        # tariff log. In particular, sparse Zappi updates may arrive *after*
        # an Intelligent Go charging slot, incorrectly putting all EV energy
        # into the peak bucket. Treat recovered EV days as estimated off-peak
        # (approved Intelligent charging is billed at the low rate). Do not
        # override days with recorded Octopus tariff history.
        if source == "ev":
            for day, cached in cache.items():
                if (day.startswith(month) and cached.get("source") in
                        ("csv_estimate", "ev_offpeak_estimate")):
                    total = round(float(cached.get("total", 0.0)), 3)
                    updated = {"peak": 0.0, "offpeak": total,
                               "total": total, "source": "ev_offpeak_estimate"}
                    if cached != updated:
                        cache[day] = updated
                        changed = True
        observed = self._split_daily_reset_by_octopus_tariff_by_day(states, tariff_states)
        for day, row in observed.items():
            # If the tariff sensor has no historical states for this date,
            # we cannot infer the EV tariff from the timestamp of a sparse
            # cumulative Zappi update. Use the Intelligent Go low-rate
            # assumption and expose the estimate in the diagnostics.
            estimated_ev = False
            if source == "ev":
                has_tariff_history = any(
                    (getattr(st, "last_updated", None) or
                     getattr(st, "last_changed", None)) is not None and
                    dt_util.as_local(getattr(st, "last_updated", None) or
                                     getattr(st, "last_changed", None)).date().isoformat() == day and
                    str(getattr(st, "state", "")).lower() in ("on", "off")
                    for st in tariff_states
                )
                if not has_tariff_history:
                    total_observed = round(row["peak"] + row["offpeak"], 3)
                    row = {"peak": 0.0, "offpeak": total_observed}
                    estimated_ev = True
            total = round(row["peak"] + row["offpeak"], 3)
            existing = cache.get(day)
            # Never replace a full historical CSV day's maximum with a smaller
            # partial Recorder window; refresh current day from live readings.
            if existing is None or day == today or total >= float(existing.get("total", 0)) - 0.001:
                replacement = {"peak": row["peak"], "offpeak": row["offpeak"],
                               "total": total, "source": "ev_offpeak_estimate" if estimated_ev else "recorder"}
                if replacement != existing:
                    cache[day] = replacement
                    changed = True
        # Retain current and previous month only.
        cutoff = (dt_util.now() - timedelta(days=62)).strftime("%Y-%m")
        for day in list(cache):
            if day[:7] < cutoff:
                del cache[day]
                changed = True
        if changed:
            await self._async_save_pv_history()
        return {day: {"peak": float(row["peak"]), "offpeak": float(row["offpeak"])}
                for day, row in cache.items() if day.startswith(month)}

    def _octopus_home_mini_since_cutoff(self, states: list[Any], tariff_states: list[Any], cutoff: datetime) -> dict[str, dict[str, float]]:
        """Use positive Home Mini counter increments since the requested local cutoff.

        The reading immediately before the cutoff provides the baseline. A missing
        baseline is not interpreted as zero: doing so would include earlier usage.
        """
        ordered = sorted(states, key=lambda st: getattr(st, "last_updated", None) or getattr(st, "last_changed", dt_util.utcnow()))
        previous = None
        result: dict[str, dict[str, float]] = {}
        for st in ordered:
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            value = self._numeric_state(st)
            if stamp is None or value is None or value < 0:
                continue
            if stamp < cutoff:
                previous = value
                continue
            if previous is None:
                # First observation is a baseline, not an increment.
                previous = value
                continue
            delta = value - previous
            if delta < -0.001:
                # Home Mini cumulative sensor can restart at zero. Only the
                # new counter's reading belongs to the interval after reset.
                delta = value
            previous = value
            if delta <= 0:
                continue
            day = dt_util.as_local(stamp).date().isoformat()
            bucket = result.setdefault(day, {"peak": 0.0, "offpeak": 0.0})
            bucket["offpeak" if self._offpeak_at(stamp, tariff_states) else "peak"] += delta
        return result

    def _octopus_export_daily_deltas(self, states: list[Any]) -> dict[str, float]:
        """Calculate export from a non-resetting Octopus meter without estimates.

        Meter updates need not coincide with midnight. Assign each observed
        positive increment to the UK day on which the new reading arrived.
        The first observation is only a baseline; never add its lifetime total.
        """
        from zoneinfo import ZoneInfo
        cutoff = datetime(2026, 10, 24, tzinfo=ZoneInfo("Europe/London"))
        ordered = sorted(states, key=lambda st: getattr(st, "last_updated", None)
                         or getattr(st, "last_changed", dt_util.utcnow()))
        previous = None
        totals: dict[str, float] = {}
        for st in ordered:
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            value = self._numeric_state(st)
            if stamp is None or value is None or value < 0:
                continue
            if stamp < cutoff:
                previous = value
                continue
            if previous is None:
                previous = value
                continue
            delta = value - previous
            previous = value
            if delta <= 0:
                continue
            day = dt_util.as_local(stamp).date().isoformat()
            totals[day] = totals.get(day, 0.0) + delta
        return {day: round(kwh, 3) for day, kwh in totals.items()}

    @staticmethod
    def _octopus_daily_row(total: float | None, offpeak: float | None,
                           peak_cost: float | None, offpeak_cost: float | None,
                           exported: float | None) -> dict[str, Any]:
        """Keep missing readings distinct from real zero values."""
        peak = round(max(total - offpeak, 0.0), 3) if total is not None and offpeak is not None else None
        return {"import_kwh": total, "offpeak_kwh": offpeak, "peak_kwh": peak,
                "peak_cost": peak_cost, "offpeak_cost": offpeak_cost,
                "export_kwh": exported}

    def _rate_at_day(self, states: list[Any], day: str, fallback: float | None = None) -> float | None:
        """Last recorded VAT-inclusive GBP rate on or before the day."""
        latest = None
        for st in sorted(states, key=lambda x: getattr(x, "last_updated", None) or getattr(x, "last_changed", dt_util.utcnow())):
            stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
            value = self._numeric_state(st)
            if stamp is not None and value is not None and dt_util.as_local(stamp).date().isoformat() <= day:
                latest = value
        return latest if latest is not None else fallback

    async def _calculate_monthly_energy_costs(self) -> dict[str, Any]:
        """Unified daily Octopus readings, shared by daily, weekly and monthly views."""
        cfg = self.entry.data
        gas_entity = self._gas_import_entity
        ev_entity = cfg.get(CONF_DAILY_EV_ENTITY, DEFAULT_DAILY_EV_ENTITY)
        entities = [
            self._electricity_import_entity, self._electricity_entity("current_accumulative_consumption_off_peak"),
            self._electricity_entity("current_accumulative_cost_peak"), self._electricity_entity("current_accumulative_cost_off_peak"),
            self._electricity_entity("current_total_export"), self._electricity_entity("current_rate"),
            self._electricity_entity("current_standing_charge"),
            self._gas_entity("current_rate"), self._gas_entity("current_standing_charge"),
            gas_entity, ev_entity,
        ]
        entities = [entity for entity in entities if entity]
        history = await self._recorder_states_current_month(entities)
        def daily(entity: str) -> dict[str, float]:
            return {day: value for day, value in self._daily_maxima(history.get(entity, [])).items()
                    if day >= OCTOPUS_CUTOFF}

        imported = daily(self._electricity_import_entity)
        offpeak = daily(self._electricity_entity("current_accumulative_consumption_off_peak"))
        peak_cost_days = daily(self._electricity_entity("current_accumulative_cost_peak"))
        offpeak_cost_days = daily(self._electricity_entity("current_accumulative_cost_off_peak"))
        export_states = history.get(self._electricity_entity("current_total_export"), [])
        export_days = self._octopus_export_daily_deltas(export_states)

        # Recorder may not have committed any of today's daily-reset states yet
        # (especially immediately after a month rollover). Use the live Octopus
        # counters for today, but never substitute zero for an unavailable sensor.
        today_key = dt_util.now().date().isoformat()
        live_import = self._state_float(self._electricity_import_entity)
        live_offpeak = self._state_float(self._electricity_entity("current_accumulative_consumption_off_peak"))
        live_peak_cost = self._state_float(self._electricity_entity("current_accumulative_cost_peak"))
        live_offpeak_cost = self._state_float(self._electricity_entity("current_accumulative_cost_off_peak"))
        for values, live_value in (
            (imported, live_import),
            (offpeak, live_offpeak),
            (peak_cost_days, live_peak_cost),
            (offpeak_cost_days, live_offpeak_cost),
        ):
            if live_value is not None and live_value >= 0:
                values[today_key] = round(live_value, 3)

        # A cumulative export meter can remain unchanged all morning. If the
        # Recorder contains a valid baseline at or before today's start and
        # another valid reading today, that is an observed zero-export day,
        # not missing data. Never infer zero without both observations.
        if today_key not in export_days:
            valid_export = []
            for st in export_states:
                stamp = getattr(st, "last_updated", None) or getattr(st, "last_changed", None)
                reading = self._numeric_state(st)
                if stamp is not None and reading is not None and reading >= 0:
                    valid_export.append((dt_util.as_local(stamp).date().isoformat(), reading))
            if (any(day < today_key for day, _ in valid_export)
                    and any(day == today_key for day, _ in valid_export)):
                export_days[today_key] = 0.0
            else:
                # Recorder may not yet contain a state change today: a
                # lifetime-total export sensor often retains yesterday's
                # last_updated timestamp while its value stays unchanged.
                # Compare a valid *live* reading with the most recent
                # pre-today Recorder baseline before declaring zero.
                live_export = self._state_float(self._electricity_entity("current_total_export"))
                earlier = [reading for day, reading in valid_export if day < today_key]
                if (live_export is not None and live_export >= 0 and earlier
                        and abs(live_export - earlier[-1]) < 0.000001):
                    export_days[today_key] = 0.0

        days_observed = set(imported) | set(offpeak) | set(peak_cost_days) | set(offpeak_cost_days) | set(export_days)
        for day in sorted(days_observed):
            row = self._octopus_daily_row(
                imported.get(day), offpeak.get(day),
                peak_cost_days.get(day), offpeak_cost_days.get(day),
                export_days.get(day),
            )
            # Preserve a previously observed day when Recorder retention expires.
            previous = self._octopus_daily_records.get(day, {})
            self._octopus_daily_records[day] = {key: value if value is not None else previous.get(key)
                                               for key, value in row.items()}
        await self._async_save_pv_history()

        today = dt_util.now().date()
        month = today.strftime("%Y-%m")
        month_records = {day: row for day, row in self._octopus_daily_records.items()
                         if day.startswith(month) and day >= OCTOPUS_CUTOFF}
        def sum_field(field: str) -> float:
            return round(sum(float(row[field]) for row in month_records.values()
                             if row.get(field) is not None), 3)

        peak_kwh = sum_field("peak_kwh")
        offpeak_kwh = sum_field("offpeak_kwh")
        total_kwh = sum_field("import_kwh")
        peak_cost = sum_field("peak_cost")
        offpeak_cost = sum_field("offpeak_cost")
        grid_export_kwh = sum_field("export_kwh")
        gas_days = {day: value for day, value in self._daily_maxima(history.get(gas_entity, [])).items()
                    if day >= OCTOPUS_CUTOFF}
        # Live Octopus gas can be newer than Recorder, but reject a stale
        # daily-reset value carried over from the previous calendar day.
        gas_state = self.hass.states.get(gas_entity)
        merge_live_today(
            gas_days,
            today.isoformat(),
            self._state_float(gas_entity),
            gas_state.last_updated if gas_state is not None else None,
        )
        gas_kwh = round(sum(gas_days.values()), 3)
        daily_energy_usage = self._daily_energy_usage_payload(
            {day: {"peak": row["peak_kwh"], "offpeak": row["offpeak_kwh"]}
             for day, row in month_records.items()
             if row.get("peak_kwh") is not None and row.get("offpeak_kwh") is not None},
            gas_days,
        )
        ev_days = daily(ev_entity)
        ev_kwh = round(sum(ev_days.values()), 3)
        # EV energy is a subset of meter import, never added to it.
        elec_sc = self._rate_at_day(history.get(self._electricity_entity("current_standing_charge"), []),
                                    today.isoformat(), self._state_float(self._electricity_entity("current_standing_charge")))
        gas_sc = self._rate_at_day(history.get(self._gas_entity("current_standing_charge"), []),
                                   today.isoformat(), self._state_float(self._gas_entity("current_standing_charge")))
        # Derive displayed tariff rates from observed Octopus GBP/kWh states.
        # A single observed rate cannot establish both tariff bands.
        rate_samples = [self._numeric_state(st) for st in history.get(self._electricity_entity("current_rate"), [])]
        rate_samples = sorted({round(v, 7) for v in rate_samples if v is not None and v >= 0})
        live_electric_rate = self._state_float(self._electricity_entity("current_rate"))
        if live_electric_rate is not None and live_electric_rate >= 0:
            rate_samples = sorted(set(rate_samples) | {round(live_electric_rate, 7)})
        offpeak_rate_p = round(rate_samples[0] * 100, 4) if len(rate_samples) >= 2 else None
        peak_rate_p = round(rate_samples[-1] * 100, 4) if len(rate_samples) >= 2 else None
        gas_rate = self._rate_at_day(history.get(self._gas_entity("current_rate"), []),
                                     today.isoformat(), self._state_float(self._gas_entity("current_rate")))
        # Standing charges accrue once per calendar day, independently of meter updates.
        first_day = max(today.replace(day=1), datetime.fromisoformat(OCTOPUS_CUTOFF).date())
        days = max((today - first_day).days + 1, 0)
        electricity_standing = round(days * elec_sc, 4) if elec_sc is not None else 0.0
        gas_standing = round(days * gas_sc, 4) if gas_sc is not None else 0.0
        electricity_total = peak_cost + offpeak_cost + electricity_standing
        # Rates and Octopus costs are already VAT inclusive: VAT is a component,
        # not an additional charge.
        electricity_vat = electricity_total - electricity_total / 1.05
        electricity_subtotal = electricity_total - electricity_vat
        # Gas cost uses the VAT-inclusive rate applicable to each recorded day.
        gas_usage_cost = 0.0
        for day, kwh in gas_days.items():
            day_rate = self._rate_at_day(history.get(self._gas_entity("current_rate"), []), day, gas_rate)
            if day_rate is not None:
                gas_usage_cost += kwh * day_rate
        gas_total = gas_usage_cost + gas_standing
        gas_vat = gas_total - gas_total / 1.05
        gas_subtotal = gas_total - gas_vat
        export_rate = float(cfg.get(CONF_EXPORT_RATE, DEFAULT_EXPORT_RATE)) / 100.0
        export_credit = grid_export_kwh * export_rate
        net_electricity_cost = electricity_total - export_credit
        # Zappi's charging cost is informational only; a tariff-specific split
        # cannot be derived accurately from a daily EV total alone.
        return {
            "billing_month": today.strftime("%B %Y"),
            "daily_energy_usage_this_month": daily_energy_usage,
            "billing_days_elapsed": days,
            "electricity_peak_kwh": round(peak_kwh, 3),
            "electricity_offpeak_kwh": round(offpeak_kwh, 3),
            "electricity_total_kwh": round(total_kwh, 3),
            "octopus_reported_kwh": round(total_kwh, 3),
            "electricity_import_source": "Octopus Home Mini daily counters from 24 Oct 2026",
            "electricity_estimated_days": 0,
            "electricity_house_kwh": round(max(total_kwh - ev_kwh, 0), 3),
            "ev_month_kwh": ev_kwh,
            "ev_peak_kwh": None,
            "ev_offpeak_kwh": None,
            "electricity_peak_cost": round(peak_cost, 2),
            "electricity_offpeak_cost": round(offpeak_cost, 2),
            "electricity_standing_cost": round(electricity_standing, 2),
            "electricity_subtotal": round(electricity_subtotal, 2),
            "electricity_vat": round(electricity_vat, 2),
            "electricity_total_cost": round(electricity_total, 2),
            "grid_export_month_kwh": round(grid_export_kwh, 3),
            "export_credit_this_month": round(export_credit, 2),
            "net_electricity_cost": round(net_electricity_cost, 2),
            "ev_cost_inc_vat": None,
            "gas_month_kwh": gas_kwh,
            "gas_usage_cost": round(gas_usage_cost, 2),
            "gas_standing_cost": round(gas_standing, 2),
            "gas_subtotal": round(gas_subtotal, 2),
            "gas_vat": round(gas_vat, 2),
            "gas_total_cost": round(gas_total, 2),
            "combined_energy_cost": round(net_electricity_cost + gas_total, 2),
            "vat_rate_pct": 5.0,
            "peak_rate_p": peak_rate_p,
            "offpeak_rate_p": offpeak_rate_p,
            "gas_rate_p": round(gas_rate * 100, 4) if gas_rate is not None else None,
            "electricity_standing_p": round(elec_sc * 100, 5) if elec_sc is not None else None,
            "gas_standing_p": round(gas_sc * 100, 5) if gas_sc is not None else None,
            "export_rate_p": round(export_rate * 100, 3),
            "daily_grid_export_entity": self._electricity_entity("current_total_export"),
            "octopus_offpeak_entity": self._electricity_entity("current_accumulative_consumption_off_peak"),
        }


    async def _async_update_data(self) -> dict[str, Any]:
        cfg = self.entry.data
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "global_tilted_irradiance,cloud_cover",
            "forecast_days": 14,
            "timezone": "auto",
            "tilt": cfg[CONF_TILT],
            "azimuth": cfg[CONF_AZIMUTH],
        }

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params=params,
                timeout=20,
            ) as response:
                response.raise_for_status()
                weather = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            # Weather forecast is optional for meter consumption. Never
            # freeze energy summaries because Open-Meteo is unavailable.
            _LOGGER.warning(
                "Open-Meteo forecast unavailable; continuing energy updates: %s", err
            )
            forecast_failed = True
            weather = {"hourly": {}}
        else:
            forecast_failed = False

        self._forecast_refresh_status = "degraded" if forecast_failed else "ok"
        hourly = weather.get("hourly", {})
        times = hourly.get("time", [])
        irradiance = hourly.get("global_tilted_irradiance", [])
        cloud_cover = hourly.get("cloud_cover", [])

        panel_kwp = float(cfg[CONF_PANEL_KWP])
        pr = float(cfg[CONF_PERFORMANCE_RATIO])

        daily: dict[str, dict[str, float]] = {}
        hourly_rows: list[dict[str, Any]] = []
        for stamp, gti, cloud in zip(times, irradiance, cloud_cover):
            if gti is None:
                continue
            day = stamp[:10]
            row = daily.setdefault(day, {"kwh": 0.0, "cloud_sum": 0.0, "cloud_count": 0})
            # GTI is a preceding-hour mean in W/m². For a one-hour interval:
            # kWh ≈ (GTI / 1000) × array kWp × performance ratio.
            hourly_kwh = max(float(gti), 0.0) / 1000.0 * panel_kwp * pr
            row["kwh"] += hourly_kwh
            try:
                hour = int(stamp[11:13])
            except (TypeError, ValueError):
                hour = 0
            hourly_rows.append({"date": day, "hour": hour, "kwh": round(hourly_kwh, 3)})
            if cloud is not None:
                row["cloud_sum"] += float(cloud)
                row["cloud_count"] += 1

        forecast = []
        for day in sorted(daily)[:14]:
            row = daily[day]
            forecast.append(
                {
                    "date": day,
                    "forecast_kwh": round(row["kwh"], 2),
                    "avg_cloud_pct": round(row["cloud_sum"] / row["cloud_count"], 0)
                    if row["cloud_count"]
                    else None,
                }
            )

        if forecast_failed and self.data is not None:
            # Reuse only a same-day cached forecast, never yesterday's.
            previous_forecast = self.data.get("forecast", [])
            today_key = dt_util.now().date().isoformat()
            if previous_forecast and previous_forecast[0].get("date") == today_key:
                forecast = list(previous_forecast)
                hourly_rows = [
                    row for row in self._latest_hourly_forecast_rows
                    if row.get("date") == today_key
                ]

        await self._async_update_pv_history(forecast)
        # v1.1.23.9: retain the current provider hourly shape so live state
        # updates can recover/use it without depending on an already-persisted
        # projected profile.  _async_update_hourly_pv scales it to the locked
        # daily baseline before persisting today's official orange bars.
        self._latest_hourly_forecast_rows = list(hourly_rows)
        await self._async_update_hourly_pv(self._latest_hourly_forecast_rows)
        await self._async_update_ev_history()
        await self._async_update_battery_history()
        await self._async_update_house_daily_history()
        live = self._calculate_live_battery_data()
        pv = self._calculate_pv_comparison(forecast)
        dispatch_entity = cfg[CONF_OCTOPUS_DISPATCH_ENTITY]
        dispatch_on = self.hass.states.get(dispatch_entity)
        dispatching = dispatch_on is not None and dispatch_on.state == "on"
        async with self._energy_refresh_lock:
            monthly = await self._calculate_monthly_energy_costs()
            self._last_successful_energy_update = dt_util.utcnow().isoformat()
            monthly["last_successful_energy_update"] = self._last_successful_energy_update
            monthly["forecast_refresh_status"] = self._forecast_refresh_status
        await self._async_update_grid_history()
        daily_summary = self._calculate_daily_energy_summary()
        await self._async_update_battery_charge_sources()
        charge_sources = self._battery_charge_source_summary()

        return {
            "forecast": forecast,
            "today_kwh": forecast[0]["forecast_kwh"] if forecast else 0.0,
            "tomorrow_kwh": forecast[1]["forecast_kwh"] if len(forecast) > 1 else 0.0,
            "total_14d_kwh": round(sum(x["forecast_kwh"] for x in forecast), 2),
            "soc": live["soc"],
            "battery_power_w": live["battery_power_w"],
            "dispatching": dispatching,
            "exportable_kwh": live["exportable_kwh"],
            "export_start": live["export_start"],
            "hanchu_serial": cfg[CONF_HANCHU_SERIAL],
            "battery_soc_entity": live["battery_soc_entity"],
            "battery_soc_raw": live["battery_soc_raw"],
            "battery_power_entity": live["battery_power_entity"],
            "battery_power_raw": live["battery_power_raw"],
            "last_update": live["last_update"],
            **pv,
            "pv_history": self._pv_history_payload(),
            "hourly_pv_today": self._hourly_pv_payload(),
            "hourly_pv_baseline_captured": bool(self._hourly_pv.get("baseline_captured", False)),
            **self._calculate_solar_forecast_intelligence(),
            "ev_history": self._ev_history_payload(),
            **self._battery_history_summary(),
            **self._grid_history_summary(),
            **self._rolling_energy_history_summary(),
            **daily_summary,
            **charge_sources,
            **monthly,
        }

    def _state_raw(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        return None if state is None else state.state

    def _state_float(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(str(state.state).strip().replace("%", "").replace(",", "."))
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Unable to parse numeric state from %s: %r", entity_id, state.state
            )
            return None

