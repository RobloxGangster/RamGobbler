from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict

from scheduler import schedule_allows_now, jittered_interval, user_idle_seconds, gate_reason


def test_schedule_cross_midnight():
    sched = {"days": [0,1,2,3,4,5,6], "start": "23:00", "end": "01:00", "idle_only": False}
    dt_inside = datetime(2023, 1, 2, 0, 30)  # Monday 00:30
    dt_outside = datetime(2023, 1, 2, 2, 0)
    assert schedule_allows_now(sched, dt_inside)
    assert not schedule_allows_now(sched, dt_outside)


def test_jitter_deterministic():
    a = jittered_interval(100, "foo", 10)
    b = jittered_interval(100, "foo", 10)
    assert a == b
    assert 90 <= a <= 110


def test_user_idle_seconds_non_negative():
    idle = user_idle_seconds()
    assert isinstance(idle, int) and idle >= 0


def test_metered_gate():
    @dataclass
    class Cfg:
        idle_seconds: int = 0
        metered: bool = True
    @dataclass
    class Tgt:
        schedule: Optional[Dict] = None
    reason = gate_reason(Cfg(), Tgt())
    assert reason == "Metered"
