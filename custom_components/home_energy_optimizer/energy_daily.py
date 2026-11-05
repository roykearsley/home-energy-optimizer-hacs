"""Pure helpers for reconciling live daily-reset Octopus counters."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

UK_TIMEZONE = ZoneInfo("Europe/London")


def merge_live_today(
    readings: dict[str, float],
    today: str,
    live_value: float | None,
    last_updated: datetime | None,
) -> None:
    """Use a fresh live daily counter if Recorder is behind.

    The daily-reset counter must have been updated on *today's UK date*.
    A stale state from yesterday must never be attributed to today.
    Keep the larger observed value so Recorder timing cannot move a day
    backwards. Missing data is not interpreted as zero.
    """
    if live_value is None or last_updated is None or last_updated.tzinfo is None:
        return
    try:
        value = float(live_value)
    except (TypeError, ValueError):
        return
    if not isfinite(value) or value < 0:
        return
    if last_updated.astimezone(UK_TIMEZONE).date().isoformat() != today:
        return

    previous = readings.get(today)
    if previous is None or value > previous:
        readings[today] = round(value, 3)
