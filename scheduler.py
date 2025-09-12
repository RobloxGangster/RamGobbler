"""Time and schedule helpers used by the trimming logic."""

import hashlib
import ctypes
import os
import time
from datetime import datetime
from typing import Dict, Optional


def _parse_hhmm(s: str) -> int:
    """Convert ``HH:MM`` strings to minutes since midnight.

    Invalid values return ``0`` which effectively disables time checks.
    """

    try:
        hours, minutes = map(int, s.split(":"))
        return hours * 60 + minutes
    except Exception:
        return 0


def schedule_allows_now(schedule: Optional[Dict], now: Optional[datetime] = None) -> bool:
    """Return ``True`` if ``schedule`` permits running at ``now``.

    ``schedule`` is a dictionary containing ``days`` (list of weekday indexes)
    and ``start``/``end`` times expressed as ``HH:MM`` strings.  Cross-midnight
    intervals are handled correctly.
    """

    if not schedule:
        return True
    now = now or datetime.now()
    day = now.weekday()
    days = schedule.get("days") or []
    if days and day not in days:
        return False
    start = _parse_hhmm(schedule.get("start", "00:00"))
    end = _parse_hhmm(schedule.get("end", "00:00"))
    minutes = now.hour * 60 + now.minute
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def window_active(window: Optional[Dict], now: Optional[datetime] = None) -> bool:
    """Check if the current time falls inside ``window``.

    This is similar to :func:`schedule_allows_now` but returns ``False`` when
    ``window`` is ``None``.
    """

    if not window:
        return False
    now = now or datetime.now()
    start = _parse_hhmm(window.get("start", "00:00"))
    end = _parse_hhmm(window.get("end", "00:00"))
    minutes = now.hour * 60 + now.minute
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def jittered_interval(interval: int, key: str, pct: int) -> int:
    """Deterministically jitter ``interval`` by ``pct`` percent.

    The jitter is stable for a given ``key`` so that intervals remain consistent
    across process restarts.
    """

    if pct <= 0 or interval <= 0:
        return interval
    rng = int(interval * pct / 100)
    if rng <= 0:
        return interval
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    offset = (h % (2 * rng + 1)) - rng
    return max(1, interval + offset)


def user_idle_seconds() -> int:
    """Return the number of seconds since the last user input.

    On non-Windows platforms the value is always ``0``.
    """

    if os.name != "nt":
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
    """Return a string explaining why trimming should be skipped.

    An empty string means trimming is allowed.
    """

    if not schedule_allows_now(getattr(target, "schedule", None), now):
        return "Blocked (Schedule)"

    idle_sec = getattr(config, "idle_seconds", 0)
    schedule = getattr(target, "schedule", None) or {}
    if idle_sec > 0 and schedule.get("idle_only"):
        if user_idle_seconds() < idle_sec:
            return "Idle unmet"

    if getattr(config, "metered", False):
        return "Metered"

    return ""
