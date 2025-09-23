"""Helpers for evaluating schedules and cooldown windows."""
from __future__ import annotations

import ctypes
import hashlib
import os
import random
from datetime import datetime
from functools import lru_cache
from typing import Iterable, Mapping, Optional

from ..constants import MIN_INTERVAL
from .models import AppConfig, TargetConfig


def _parse_hhmm(value: str) -> int:
    """Return minutes since midnight for ``value``."""

    try:
        hours, minutes = map(int, value.split(":"))
    except Exception:
        return 0
    if 0 <= hours < 24 and 0 <= minutes < 60:
        return hours * 60 + minutes
    return 0


def _within_window(window: Mapping[str, object], now: datetime) -> bool:
    start = _parse_hhmm(str(window.get("start", "00:00")))
    end = _parse_hhmm(str(window.get("end", "00:00")))
    minutes = now.hour * 60 + now.minute
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def schedule_allows_now(
    target: TargetConfig,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Return ``True`` when ``target`` permits gobbling at ``now``."""

    schedule = target.schedule
    if schedule is None:
        return True
    now = now or datetime.now()
    days: Iterable[int] = schedule.days or ()
    if days and now.weekday() not in days:
        return False
    return _within_window({"start": schedule.start, "end": schedule.end}, now)


def window_active(window: Optional[Mapping[str, object]], now: Optional[datetime] = None) -> bool:
    if not window:
        return False
    return _within_window(window, now or datetime.now())


@lru_cache(maxsize=2048)
def _stable_span(key: str) -> float:
    digest = hashlib.sha1(key.encode("utf-8", "ignore")).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = random.Random(seed)
    return rng.uniform(-1.0, 1.0)


def jittered_interval(interval: float, key: str, pct: int) -> float:
    """Return a jittered interval using a cached random span."""

    if interval <= 0:
        return MIN_INTERVAL
    if pct <= 0:
        return max(MIN_INTERVAL, float(interval))
    base = max(MIN_INTERVAL, float(interval))
    span = _stable_span(key)
    offset = base * (pct / 100.0) * span
    return max(MIN_INTERVAL, base + offset)


def user_idle_seconds() -> int:
    """Return seconds since last user input on Windows."""

    if os.name != "nt":
        return 0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        tick = ctypes.windll.kernel32.GetTickCount()
        millis = (tick - info.dwTime) & 0xFFFFFFFF
        return int(millis / 1000)
    return 0


def gate_reason(config: AppConfig, target: TargetConfig, now: Optional[datetime] = None) -> str:
    """Return a human readable reason describing why gobbling should be skipped."""

    if not schedule_allows_now(target, now=now):
        return "Blocked (Schedule)"

    now = now or datetime.now()
    schedule = target.schedule
    if schedule and schedule.idle_only and config.idle_seconds > 0:
        if user_idle_seconds() < config.idle_seconds:
            return "Idle unmet"

    if config.metered:
        return "Metered"

    maintenance = config.maintenance_window
    if maintenance and window_active(maintenance, now):
        return "Maintenance"

    return ""


__all__ = [
    "schedule_allows_now",
    "window_active",
    "jittered_interval",
    "user_idle_seconds",
    "gate_reason",
]
