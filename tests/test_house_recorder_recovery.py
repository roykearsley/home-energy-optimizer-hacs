"""Recorder recovery tests, including the UK DST change on 25 October."""
import importlib.util
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
import ast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "house_history", ROOT / "custom_components/home_energy_optimizer/house_history.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
UK = ZoneInfo("Europe/London")


def run_immediate(coroutine):
    """Drive storage mocks, which complete without event-loop IO."""
    try:
        coroutine.send(None)
    except StopIteration:
        return
    raise AssertionError("Unexpected suspended IO")


def sample(stamp, value):
    return SimpleNamespace(last_updated=datetime.fromisoformat(stamp), state=str(value))


class RecoveryTests(unittest.TestCase):
    def test_storage_roundtrip_keeps_recovery_and_octopus(self):
        source = ROOT / "custom_components/home_energy_optimizer/coordinator.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        methods = [n for n in cls.body if getattr(n, "name", "") in
                   {"async_load_pv_history", "_async_save_pv_history"}]
        scope = {"Any": object, "OCTOPUS_CUTOFF": "2026-10-24"}
        exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                     str(source), "exec"), scope)
        obj = SimpleNamespace(_store=SimpleNamespace(async_save=AsyncMock()))
        for name in ["pv_history", "ev_history", "battery_history", "grid_history"]:
            setattr(obj, "_" + name, [])
        for name in ["house_daily_history", "house_recovery", "octopus_daily_records",
                     "octopus_import_days", "electricity_daily_cache", "battery_charge_sources",
                     "hourly_pv", "solar_baselines"]:
            setattr(obj, "_" + name, {})
        obj._last_daily_pv_kwh = None
        obj._last_daily_charge_kwh = None
        obj._house_daily_history = {"2026-10-24": 12.3}
        obj._house_recovery = {"2026-10-24": {"kwh": 12.3, "source": "recorder"}}
        obj._octopus_daily_records = {"2026-10-24": {"peak_kwh": 7.0}}
        run_immediate(scope["_async_save_pv_history"](obj))
        payload = obj._store.async_save.call_args.args[0]
        obj._store.async_load = AsyncMock(return_value=payload)
        obj._house_daily_history = {}
        obj._house_recovery = {}
        run_immediate(scope["async_load_pv_history"](obj))
        self.assertEqual(obj._house_daily_history, {"2026-10-24": 12.3})
        self.assertEqual(obj._house_recovery, payload["house_recovery"])
        self.assertEqual(obj._octopus_daily_records, payload["octopus_daily_records"])

    def recover(self, rows):
        return module.recover_days(rows, date(2026, 11, 2), UK)

    def test_uk_midnight_and_dst(self):
        rows = [sample("2026-10-24T22:59:00+00:00", 12.3),
                sample("2026-10-24T23:00:00+00:00", 0),
                sample("2026-10-25T23:59:00+00:00", 19.4),
                sample("2026-10-26T00:00:00+00:00", 0)]
        result = self.recover(rows)
        self.assertEqual(result["2026-10-24"]["kwh"], 12.3)
        self.assertEqual(result["2026-10-25"]["kwh"], 19.4)
        self.assertEqual(self.recover(list(reversed(rows))), result)

    def test_source_corrections_and_positive_midnight_reset(self):
        rows = [sample("2026-11-01T12:00:00+00:00", 12.0),
                sample("2026-11-01T12:05:00+00:00", 11.89),
                sample("2026-11-01T23:55:00+00:00", 18.01),
                sample("2026-11-01T23:58:59.774+00:00", 18.0),
                sample("2026-11-02T00:03:59.696+00:00", 0.02)]
        result = self.recover(rows)
        self.assertEqual(result["2026-11-01"]["kwh"], 18.0)
        self.assertEqual(result["2026-11-01"]["observed_at"],
                         "2026-11-01T23:58:59.774000+00:00")

    def test_cumulative_decline_and_non_reset_are_rejected(self):
        for middle, reset in [(11.88, 0), (12, 0.03), (12, 12)]:
            rows = [sample("2026-11-01T12:00:00+00:00", 12),
                    sample("2026-11-01T12:05:00+00:00", middle),
                    sample("2026-11-01T23:59:00+00:00", 18),
                    sample("2026-11-02T00:04:00+00:00", reset)]
            self.assertEqual(self.recover(rows), {})

    def test_missing_reset_or_long_gap_is_unavailable(self):
        for stamp, value in [("2026-10-27T00:00:00+00:00", 12),
                             ("2026-10-27T00:20:00+00:00", 0)]:
            self.assertEqual(self.recover([
                sample("2026-10-26T23:59:00+00:00", 12), sample(stamp, value)]), {})

    def test_invalid_and_intraday_reset_are_rejected(self):
        for value in ["unknown", "unavailable", "nan", "inf", -1, 20]:
            self.assertEqual(self.recover([
                sample("2026-10-26T12:00:00+00:00", value),
                sample("2026-10-26T23:59:00+00:00", 12),
                sample("2026-10-27T00:00:00+00:00", 0)]), {})

    def test_cutoff_today_and_zero(self):
        rows = [sample("2026-10-23T22:59:00+00:00", 4),
                sample("2026-10-23T23:00:00+00:00", 0),
                sample("2026-10-24T22:59:00+00:00", 0),
                sample("2026-10-24T23:00:00+00:00", 0),
                sample("2026-11-02T12:00:00+00:00", 2)]
        self.assertEqual(set(self.recover(rows)), {"2026-10-24"})
        self.assertEqual(self.recover(rows)["2026-10-24"]["kwh"], 0)

    def test_existing_rolling_regressions(self):
        from test_rolling_seven_day_history import (
            test_missing_tariff_band_not_counted_as_zero_or_partial_total,
            test_rolling_dates_cross_month_boundary_without_monthly_sensors)
        test_missing_tariff_band_not_counted_as_zero_or_partial_total()
        test_rolling_dates_cross_month_boundary_without_monthly_sensors()
