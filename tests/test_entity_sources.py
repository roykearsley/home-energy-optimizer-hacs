"""Entity discovery and ID construction tests (without Home Assistant)."""

import importlib.util
from pathlib import Path
import unittest

MODULE = Path(__file__).resolve().parents[1] / "custom_components/home_energy_optimizer/entity_sources.py"
spec = importlib.util.spec_from_file_location("heo_entity_sources", MODULE)
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


class TestEntitySources(unittest.TestCase):
    def test_single_matching_electricity_meter(self):
        meter = "sensor.octopus_energy_electricity_example_current_accumulative_consumption"
        selected = sources.discover_unique_entity([meter], "sensor.octopus_energy_electricity_",
                                                  "_current_accumulative_consumption")
        self.assertEqual(selected, meter)
        self.assertEqual(sources.meter_derived_entity(
            selected, "_current_accumulative_consumption", "current_accumulative_cost_peak"),
            "sensor.octopus_energy_electricity_example_current_accumulative_cost_peak")

    def test_multiple_meters_are_not_guessed(self):
        items = ["sensor.octopus_energy_electricity_one_current_accumulative_consumption",
                 "sensor.octopus_energy_electricity_two_current_accumulative_consumption"]
        self.assertEqual(sources.discover_unique_entity(
            items, "sensor.octopus_energy_electricity_", "_current_accumulative_consumption"), "")

    def test_hanchu_id_is_derived_from_configured_serial(self):
        self.assertEqual(sources.hanchu_entity("", "battery_soc"), "")
        self.assertEqual(sources.hanchu_entity("EXAMPLE", "battery_soc"),
                         "sensor.hanchuess_example_battery_soc")

    def test_no_config_is_not_an_invented_id(self):
        self.assertEqual(sources.meter_derived_entity("", "_current_accumulative_consumption", "current_rate"), "")


if __name__ == "__main__":
    unittest.main()
