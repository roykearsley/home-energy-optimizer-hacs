"""Regression tests for PR4 rolling seven-day readings (no HA runtime needed)."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/home_energy_optimizer/coordinator.py"


def _summary(today):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and any(isinstance(item, ast.FunctionDef)
                       and item.name == "_rolling_energy_history_summary" for item in node.body))
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_rolling_energy_history_summary")
    function = ast.FunctionDef(name=method.name, args=method.args, body=method.body,
                               decorator_list=[], returns=method.returns, type_comment=None)
    namespace = {"dt_util": Mock(now=lambda: datetime(today.year, today.month, today.day,
                                                      tzinfo=timezone.utc)),
                 "timedelta": timedelta, "Any": Any}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 str(SOURCE), "exec"), namespace)
    return namespace[method.name]


def test_rolling_dates_cross_month_boundary_without_monthly_sensors():
    today = datetime(2026, 11, 2).date()
    calc = _summary(today)
    obj = Mock()
    obj._house_recovery = {}
    obj._house_daily_history = {"2026-11-02": 6.17, "2026-11-01": 10.0,
                                "2026-10-31": 8.0}
    obj._octopus_daily_records = {
        "2026-11-02": {"peak_kwh": 1.0, "offpeak_kwh": 15.25},
        "2026-11-01": {"peak_kwh": 8.0, "offpeak_kwh": 27.923},
        "2026-10-31": {"peak_kwh": 4.0, "offpeak_kwh": 19.38},
    }
    result = calc(obj)
    assert len(result["house_consumption_7d_history"]) == 7
    assert result["house_consumption_7d_history"][2]["date"] == "2026-10-31"
    assert result["house_consumption_7d_total_kwh"] == 24.17
    assert result["electricity_combined_7d_total_kwh"] == 75.553
    assert result["electricity_tariff_7d_days_available"] == 3
    assert result["electricity_tariff_7d_history"][3]["total_kwh"] is None


def test_missing_tariff_band_not_counted_as_zero_or_partial_total():
    calc = _summary(datetime(2026, 11, 2).date())
    obj = Mock()
    obj._house_recovery = {}
    obj._house_daily_history = {}
    obj._octopus_daily_records = {
        "2026-11-02": {"peak_kwh": None, "offpeak_kwh": 3.0},
        "2026-11-01": {"peak_kwh": 2.0, "offpeak_kwh": 5.0},
    }
    result = calc(obj)
    assert result["electricity_tariff_7d_history"][0]["total_kwh"] is None
    assert result["electricity_tariff_7d_days_available"] == 1
    assert result["electricity_combined_7d_total_kwh"] == 7.0
    assert result["house_consumption_7d_days_available"] == 0

