"""Dependency-free regression tests for the energy calculation helpers.

Extract actual methods from coordinator.py via AST to avoid importing Home
Assistant in GitHub's lightweight syntax/regression CI environment.
"""
import ast
from datetime import datetime, timedelta, timezone, time
from pathlib import Path
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components/home_energy_optimizer/coordinator.py"
CONFIG = ROOT / "custom_components/home_energy_optimizer/config_flow.py"
UK = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 31, 12, tzinfo=UK)


class DateTools:
    @staticmethod
    def now():
        return NOW

    @staticmethod
    def utcnow():
        return NOW.astimezone(timezone.utc)

    @staticmethod
    def as_local(stamp):
        return stamp.astimezone(UK)


def load_methods(*names):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and any(getattr(item, "name", None) in names for item in node.body))
    selected = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names]
    assert len(selected) == len(names), f"Missing methods: {set(names) - {n.name for n in selected}}"
    # Strip decorators (including staticmethod); tests call the helpers explicitly.
    for node in selected:
        node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    scope = {"datetime": datetime, "timedelta": timedelta, "time": time, "dt_util": DateTools,
             "OCTOPUS_CUTOFF": "2026-10-24", "Any": object}
    exec(compile(module, str(SOURCE), "exec"), scope)
    return SimpleNamespace(**{name: scope[name] for name in names})


METHODS = load_methods("_numeric_state", "_octopus_export_daily_deltas",
                       "_octopus_daily_row", "_grid_history_payload", "_grid_history_summary",
                       "_daily_energy_usage_payload")


class MeterState:
    def __init__(self, value, day, hour=12, minute=0):
        self.state = str(value)
        self.last_updated = datetime.fromisoformat(day).replace(
            hour=hour, minute=minute, tzinfo=UK)


class EnergyTests(unittest.TestCase):
    def setUp(self):
        self.obj = SimpleNamespace(
            _numeric_state=METHODS._numeric_state,
            _octopus_daily_records={},
            _grid_history_payload=lambda: METHODS._grid_history_payload(self.obj),
        )

    def test_export_lifetime_baseline_not_counted(self):
        states = [MeterState(3700, "2026-10-24"), MeterState(3702.5, "2026-10-24", 18),
                  MeterState(3704, "2026-10-25")]
        result = METHODS._octopus_export_daily_deltas(self.obj, states)
        self.assertEqual(result, {"2026-10-24": 2.5, "2026-10-25": 1.5})

    def test_export_does_not_backfill_before_cutoff(self):
        states = [MeterState(3690, "2026-10-23"), MeterState(3700, "2026-10-24")]
        result = METHODS._octopus_export_daily_deltas(self.obj, states)
        self.assertEqual(result, {"2026-10-24": 10.0})

    def test_export_ignores_negative_reset_and_bad_readings(self):
        states = [MeterState(100, "2026-10-24"), MeterState("unknown", "2026-10-24", 13),
                  MeterState(98, "2026-10-24", 14), MeterState(101, "2026-10-24", 15)]
        result = METHODS._octopus_export_daily_deltas(self.obj, states)
        self.assertEqual(result, {"2026-10-24": 3.0})

    def test_daily_row_preserves_missing_not_zero(self):
        missing = METHODS._octopus_daily_row(None, None, None, None, None)
        self.assertIsNone(missing["peak_kwh"])
        zero = METHODS._octopus_daily_row(0.0, 0.0, 0.0, 0.0, 0.0)
        self.assertEqual(zero["peak_kwh"], 0.0)

    def test_seven_day_summary_matches_displayed_dates(self):
        for days_ago in range(8):
            day = (NOW.date() - timedelta(days=days_ago)).isoformat()
            self.obj._octopus_daily_records[day] = {"import_kwh": float(days_ago + 1),
                                                      "export_kwh": float(days_ago) / 2}
        displayed = METHODS._grid_history_payload(self.obj)
        summary = METHODS._grid_history_summary(self.obj)
        self.assertEqual(summary["grid_import_7d_kwh"],
                         round(sum(row["import_kwh"] for row in displayed), 2))
        self.assertEqual(summary["grid_export_7d_kwh"],
                         round(sum(row["export_kwh"] for row in displayed), 2))

    def test_daily_usage_unknown_not_zero(self):
        payload = METHODS._daily_energy_usage_payload(
            self.obj, {"2026-10-24": {"peak": 1.0, "offpeak": 2.0}}, {})
        day = next(row for row in payload if row["date"] == "2026-10-24")
        self.assertEqual(day["electricity_peak_kwh"], 1.0)
        self.assertIsNone(day["gas_kwh"])
        absent = next(row for row in payload if row["date"] == "2026-10-23")
        self.assertIsNone(absent["electricity_peak_kwh"])

    def test_configuration_files_parse(self):
        for path in (SOURCE, CONFIG):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


if __name__ == "__main__":
    unittest.main()
