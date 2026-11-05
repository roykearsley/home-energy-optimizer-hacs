"""Conservative recovery of observed daily Hanchu counter readings."""
from datetime import date, datetime, time, timedelta, timezone
import math
from decimal import Decimal

START = date(2026, 10, 24)
# Observed maximum drawdown in the supplied Hanchu export, in kWh.
MAX_CORRECTION = Decimal("0.11")
MAX_RESET_READING = 0.02


def recover_days(states, today, tz):
    """Return last observed readings only when a midnight reset is witnessed.

    These are recorded readings, not interpolated end-of-day totals. Require
    samples within ten minutes of both sides of midnight; otherwise omit the
    day. Never move a post-midnight stale counter to the preceding day.
    """
    samples = []
    for state in states:
        stamp = state.last_updated
        if stamp is None or stamp.tzinfo is None:
            continue
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            value = None
        if value is not None and (not math.isfinite(value) or value < 0):
            value = None
        samples.append((stamp.astimezone(timezone.utc), value))
    samples.sort(key=lambda row: row[0])
    grouped = {}
    for stamp, value in samples:
        grouped.setdefault(stamp.astimezone(tz).date(), []).append((stamp, value))
    result = {}
    for day, rows in grouped.items():
        if not START <= day < today:
            continue
        values = [value for _, value in rows]
        # Small source corrections are not counter resets. Retain the final
        # actual sample, never sum increments or substitute a daily maximum.
        if any(value is None for value in values):
            continue
        peak = Decimal("0")
        ambiguous = False
        for observed in values:
            numeric = Decimal(str(observed))
            peak = max(peak, numeric)
            if peak - numeric > MAX_CORRECTION:
                ambiguous = True
        if ambiguous:
            continue
        boundary = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        boundary = boundary.astimezone(timezone.utc)
        stamp, value = rows[-1]
        following = grouped.get(day + timedelta(days=1), [])
        if not following or boundary - stamp > timedelta(minutes=10):
            continue
        reset_stamp, reset_value = following[0]
        if (reset_stamp - boundary > timedelta(minutes=10)
                or reset_value is None or reset_value > MAX_RESET_READING
                or (reset_value != 0 and value - reset_value <= float(MAX_CORRECTION))):
            continue
        result[day.isoformat()] = {
            "kwh": value, "observed_at": stamp.isoformat(),
            "reset_at": reset_stamp.isoformat(), "source": "recorder",
            "status": "last_observed_before_reset",
        }
    return result
