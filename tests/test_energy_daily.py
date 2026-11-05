"""Regression tests for live Octopus daily counter reconciliation."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "custom_components" / "home_energy_optimizer" / "energy_daily.py"
spec = importlib.util.spec_from_file_location("heo_energy_daily", MODULE)
energy_daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(energy_daily)
merge_live_today = energy_daily.merge_live_today


class TestLiveDailyGas(unittest.TestCase):
    def test_live_today_overrides_lagging_recorder(self):
        days = {"2026-11-05": 22.55, "2026-11-04": 82.1}
        merge_live_today(days, "2026-11-05", 26.72,
                         datetime(2026, 11, 5, 7, 5, tzinfo=timezone.utc))
        self.assertEqual(days["2026-11-05"], 26.72)
        self.assertEqual(days["2026-11-04"], 82.1)

    def test_does_not_reduce_more_recent_recorder_value(self):
        days = {"2026-11-05": 26.72}
        merge_live_today(days, "2026-11-05", 22.55,
                         datetime(2026, 11, 5, 7, 5, tzinfo=timezone.utc))
        self.assertEqual(days["2026-11-05"], 26.72)

    def test_stale_previous_day_counter_is_rejected(self):
        days = {}
        merge_live_today(days, "2026-11-05", 86.3,
                         datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc))
        self.assertNotIn("2026-11-05", days)

    def test_uk_day_boundary_applies_during_bst(self):
        days = {}
        merge_live_today(days, "2026-06-02", 4.2,
                         datetime(2026, 6, 1, 23, 10, tzinfo=timezone.utc))
        self.assertEqual(days["2026-06-02"], 4.2)

    def test_unavailable_readings_are_not_created_as_zero(self):
        days = {}
        for value in (None, -1.0, float("nan"), float("inf")):
            merge_live_today(days, "2026-11-05", value,
                             datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(days, {})

    def test_missing_last_updated_is_not_assumed_fresh(self):
        days = {}
        merge_live_today(days, "2026-11-05", 26.72, None)
        self.assertEqual(days, {})


if __name__ == "__main__":
    unittest.main()
