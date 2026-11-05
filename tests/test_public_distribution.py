"""Reject hard-coded devices and embedded household consumption history."""

from pathlib import Path
import re
import unittest

CODE = Path(__file__).resolve().parents[1] / "custom_components/home_energy_optimizer"


class PublicDistributionTests(unittest.TestCase):
    def test_no_specific_meter_or_device_identifiers(self):
        patterns = [
            re.compile(r"sensor\.hanchuess_[a-z0-9]{10,}_(?:battery|daily|grid|pv|load)", re.I),
            re.compile(r"sensor\.octopus_energy_(?:electricity|gas)_[a-z0-9]{6,}_[a-z0-9]{8,}_", re.I),
            re.compile(r"sensor\.myenergi_zappi_[0-9]{6,}"),
        ]
        for path in CODE.glob("*.py"):
            for pattern in patterns:
                with self.subTest(filename=path.name):
                    self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_historical_exports_are_not_bundled(self):
        text = (CODE / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("OCTOBER_2026_CSV_DAILY_MAXIMA = {'house': {}, 'ev': {}}", text)
        self.assertIn("OCTOBER_2026_EXPORT_CSV_MAXIMA = {}", text)
        self.assertIn("OCTOBER_2026_OCTOPUS_EXPORT_OVERRIDES = {}", text)


if __name__ == "__main__":
    unittest.main()
