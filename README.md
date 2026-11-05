# Home Energy Optimizer

Home Assistant custom integration for Hanchu battery monitoring, Octopus electricity and gas metering, solar forecasting and EV charging.

This clean HACS distribution contains **no private meter IDs, device serials, household data or old development commit history**.

## Install through HACS / Marketplace

Once this repository is public, add `https://github.com/roykearsley/home-energy-optimizer-hacs` as a custom **Integration** repository. Install from HACS / Marketplace and restart Home Assistant.

### Moving from a manually installed integration

1. Make a complete Home Assistant backup, including `custom_components/home_energy_optimizer`.
2. Keep your existing Home Energy Optimizer entry in Settings > Devices & services. **Do not delete it.**
3. Install this HACS distribution over the existing integration files and restart Home Assistant.
4. Verify your Hanchu serial, SOC, power, daily PV and EV entity settings. These remain stored privately in Home Assistant.
5. If your installation lacks a configured Octopus electricity meter entity, it will discover one only if exactly one matching entity exists. Otherwise use **Reconfigure** and enter the cumulative Octopus electricity and gas consumption entity IDs.
6. Confirm `sensor.daily_energy_usage_this_month` updates and the displayed Peak, Off-Peak and Gas values agree with Octopus.

## Configuration

On a fresh installation, supply the Hanchu device serial, battery SOC/power entities, daily PV and EV sensors, Octopus electricity and gas daily cumulative consumption sensors, and Octopus Intelligent dispatch sensor. Other Hanchu entity IDs are derived from the configured serial. Electricity tariff, rate and standing charge entity names are derived from the configured Octopus meter entities.

## Reliability

Energy consumption refreshes independently of solar forecast availability. Check the `last_successful_energy_update` and `forecast_refresh_status` attributes on the monthly summary sensor to troubleshoot stale readings.

The integration uses Home Assistant Recorder and state history for observations and Open-Meteo for solar forecast estimates. Missing readings are not filled with invented zeroes.

## Development

The repository runs Python compilation, unit regression tests and privacy checks in GitHub Actions. The original private development history and private recovery datasets are not included.
