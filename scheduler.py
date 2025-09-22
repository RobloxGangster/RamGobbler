"""Time and schedule helpers used by the gobbling logic."""

import hashlib
import ctypes
import os
import time
from datetime import datetime
from typing import Dict, Optional

from ram_gobbler_app.constants import MIN_INTERVAL


def _parse_hhmm(s: str) -> int:
    """Convert ``HH:MM`` strings to minutes since midnight.

    Invalid values return ``0`` which effectively disables time checks.
    """

    try:
        # Split the string and convert the components to integers.
        hours, minutes = map(int, s.split(":"))
        # Reject out-of-range values; ``24:00`` and beyond are invalid.
        if 0 <= hours < 24 and 0 <= minutes < 60:
            return hours * 60 + minutes
    except Exception:
        pass
    # Any parsing failure or invalid value results in ``0`` minutes.
    return 0


def _within_window(window: Dict, now: datetime) -> bool:
    """Return ``True`` if ``now`` falls within ``window``'s start/end.

    The window may wrap across midnight when the start time is later than the
    end time.
    """
    # Convert configured times to absolute minutes since midnight.
    start = _parse_hhmm(window.get("start", "00:00"))
    end = _parse_hhmm(window.get("end", "00:00"))

    # Current time expressed as minutes for easy comparison.
    minutes = now.hour * 60 + now.minute

    if start <= end:
        # Normal case where end is later in the day.
        return start <= minutes < end

    # Handle windows that span midnight.
    return minutes >= start or minutes < end


def schedule_allows_now(
    schedule: Optional[Dict],
    now: Optional[datetime] = None,
) -> bool:
    """Return ``True`` if ``schedule`` permits running at ``now``.

    ``schedule`` is a dictionary containing ``days`` (list of weekday indexes)
    and ``start``/``end`` times expressed as ``HH:MM`` strings.  Cross-midnight
    intervals are handled correctly.
    """

    if not schedule:
        # No schedule provided: always allowed.
        return True

    now = now or datetime.now()

    # Check day-of-week restrictions first.
    days = schedule.get("days") or []
    if days and now.weekday() not in days:
        return False

    # Delegate exact time comparisons to the helper.
    return _within_window(schedule, now)


def window_active(
    window: Optional[Dict],
    now: Optional[datetime] = None,
) -> bool:
    """Check if the current time falls inside ``window``.

    This is similar to :func:`schedule_allows_now` but returns ``False`` when
    ``window`` is ``None``.
    """

    if not window:
        # Absence of a window means gobbling is inactive.
        return False

    return _within_window(window, now or datetime.now())


def jittered_interval(interval: float, key: str, pct: int) -> float:
    """Deterministically jitter ``interval`` by ``pct`` percent.

    The jitter is stable for a given ``key`` so that intervals remain consistent
    across process restarts.
    """

    if pct <= 0 or interval <= 0:
        # Nothing to jitter; clamp to the minimum supported interval.
        return max(MIN_INTERVAL, float(interval))

    # Calculate the maximum absolute jitter based on the percentage.
    rng = float(interval) * pct / 100.0
    if rng <= 0:
        return max(MIN_INTERVAL, float(interval))

    # Derive a deterministic offset from the provided key.
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    span = h % 10001 / 10000.0  # Stable pseudo-random value in [0, 1].
    offset = (span * 2.0 - 1.0) * rng

    # Ensure the final interval stays above the supported minimum.
    return max(MIN_INTERVAL, float(interval) + offset)


def user_idle_seconds() -> int:
    """Return the number of seconds since the last user input.

    On non-Windows platforms the value is always ``0``.
    """

    if os.name != "nt":
        # The Windows API is required for idle time queries.
        return 0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        # ``GetTickCount`` wraps roughly every 49 days; mask the subtraction to
        # treat values as unsigned and avoid negative results.
        tick = ctypes.windll.kernel32.GetTickCount()
        millis = (tick - info.dwTime) & 0xFFFFFFFF
        return int(millis / 1000)
    return 0


def gate_reason(config, target, now: Optional[datetime] = None) -> str:
    """Return a string explaining why gobbling should be skipped.

    An empty string means gobbling is allowed.
    """

    if not schedule_allows_now(getattr(target, "schedule", None), now):
        # Target's schedule does not permit gobbling at this time.
        return "Blocked (Schedule)"

    idle_sec = getattr(config, "idle_seconds", 0)
    schedule = getattr(target, "schedule", None) or {}
    if idle_sec > 0 and schedule.get("idle_only"):
        # If the target requires the user to be idle, enforce it here.
        if user_idle_seconds() < idle_sec:
            return "Idle unmet"

    if getattr(config, "metered", False):
        # Skip gobbling on metered connections unless explicitly allowed.
        return "Metered"

    return ""
