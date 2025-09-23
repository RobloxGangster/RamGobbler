"""Domain models used by the refactored Ram Gobbler application.

The legacy implementation relied on mutable ``Config`` and ``Target`` classes
whose instances were shared across threads.  During the refactor we pivoted to
an immutable, dataclass driven representation so that services can reason about
state transitions explicitly.  Each mutating operation returns a brand new
object which makes it trivial to persist the resulting configuration or to
roll back to a previous snapshot when something fails.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, Iterable, Iterator, Mapping, MutableMapping, Optional, Sequence, Tuple

from ..constants import DEFAULT_INTERVAL, MIN_INTERVAL


@dataclass(frozen=True)
class GroupConfig:
    """Immutable description of a user defined target group."""

    id: str
    name: str
    color: Optional[str] = None

    def to_json(self) -> Mapping[str, object]:
        """Serialise the group to a JSON friendly dictionary."""

        payload: Dict[str, object] = {"id": self.id, "name": self.name}
        if self.color is not None:
            payload["color"] = self.color
        return payload


@dataclass(frozen=True)
class TargetSchedule:
    """Schedule configuration attached to a target."""

    start: str = "00:00"
    end: str = "00:00"
    days: Tuple[int, ...] = ()
    aggressive: bool = False
    idle_only: bool = False

    def to_json(self) -> Mapping[str, object]:
        data: Dict[str, object] = {"start": self.start, "end": self.end}
        if self.days:
            data["days"] = list(self.days)
        if self.aggressive:
            data["aggressive"] = True
        if self.idle_only:
            data["idle_only"] = True
        return data

    @classmethod
    def from_json(cls, payload: Optional[Mapping[str, object]]) -> "TargetSchedule" | None:
        if not payload:
            return None
        try:
            start = str(payload.get("start", "00:00"))
            end = str(payload.get("end", "00:00"))
            days_raw = payload.get("days") or []
            if isinstance(days_raw, Sequence):
                days = tuple(int(v) for v in days_raw)
            else:
                days = ()
            aggressive = bool(payload.get("aggressive", False))
            idle_only = bool(payload.get("idle_only", False))
        except Exception:
            return None
        return cls(start=start, end=end, days=days, aggressive=aggressive, idle_only=idle_only)


@dataclass(frozen=True)
class TargetConfig:
    """Immutable representation of a gobbling target."""

    key: str
    match_by: str
    uid: str
    group_id: str = ""
    active: bool = False
    interval_sec: float = float(DEFAULT_INTERVAL)
    last_gobble_ts: float = 0.0
    last_result: str = ""
    autostart: bool = False
    include_children: bool = False
    last_delta_mb: float = 0.0
    session_reclaimed_mb: float = 0.0
    day_reclaimed_mb: float = 0.0
    day_key: str = ""
    history: Tuple[float, ...] = ()
    adaptive: bool = True
    cpu_skip_threshold: int = 60
    background_only: bool = False
    pause_if_fullscreen: bool = True
    strategy: str = "standard"
    last_total_rss_mb: float = 0.0
    notify_threshold_mb: float = 0.0
    cooldown_s: int = 0
    reset_cap_per_min: int = 3
    schedule: TargetSchedule | None = None
    last_skip_reason: str = ""
    last_gobble_reason: str = ""
    next_allowed_ts: float = 0.0

    def normalised(self) -> "TargetConfig":
        """Return a variant with sanitised numeric fields."""

        interval = max(MIN_INTERVAL, float(self.interval_sec))
        cooldown = max(0, int(self.cooldown_s))
        reset_cap = max(0, int(self.reset_cap_per_min))
        history = tuple(float(v) for v in self.history[-40:])
        return replace(
            self,
            interval_sec=interval,
            cooldown_s=cooldown,
            reset_cap_per_min=reset_cap,
            history=history,
        )

    def touch_day_rollover(self, now: datetime) -> "TargetConfig":
        """Reset daily counters when the date changes."""

        today = now.strftime("%Y%m%d")
        if self.day_key == today:
            return self
        return replace(self, day_key=today, day_reclaimed_mb=0.0)

    def with_last_result(
        self,
        *,
        timestamp: float | None = None,
        result: str | None = None,
        delta_mb: float | None = None,
        reclaimed_delta: float | None = None,
        skip_reason: str | None = None,
        gobble_reason: str | None = None,
        history_entry: float | None = None,
    ) -> "TargetConfig":
        """Return a copy of the target with updated runtime metadata."""

        data: MutableMapping[str, object] = {}
        if timestamp is not None:
            data["last_gobble_ts"] = float(timestamp)
        if result is not None:
            data["last_result"] = result
        if delta_mb is not None:
            data["last_delta_mb"] = float(delta_mb)
        if skip_reason is not None:
            data["last_skip_reason"] = skip_reason
        if gobble_reason is not None:
            data["last_gobble_reason"] = gobble_reason
        if history_entry is not None:
            history = tuple((self.history + (float(history_entry),))[-40:])
            data["history"] = history
        reclaimed = float(self.session_reclaimed_mb)
        if reclaimed_delta is not None:
            reclaimed += float(reclaimed_delta)
            data["session_reclaimed_mb"] = reclaimed
            day_total = float(self.day_reclaimed_mb) + float(reclaimed_delta)
            data["day_reclaimed_mb"] = day_total
        if not data:
            return self
        return replace(self, **data)

    def to_json(self) -> Mapping[str, object]:
        payload: Dict[str, object] = {
            "key": self.key,
            "match_by": self.match_by,
            "uid": self.uid,
            "group_id": self.group_id,
            "active": self.active,
            "interval_sec": self.interval_sec,
            "last_gobble_ts": self.last_gobble_ts,
            "last_result": self.last_result,
            "autostart": self.autostart,
            "include_children": self.include_children,
            "last_delta_mb": self.last_delta_mb,
            "session_reclaimed_mb": self.session_reclaimed_mb,
            "day_reclaimed_mb": self.day_reclaimed_mb,
            "day_key": self.day_key,
            "history": list(self.history[-40:]),
            "adaptive": self.adaptive,
            "cpu_skip_threshold": self.cpu_skip_threshold,
            "background_only": self.background_only,
            "pause_if_fullscreen": self.pause_if_fullscreen,
            "strategy": self.strategy,
            "last_total_rss_mb": self.last_total_rss_mb,
            "notify_threshold_mb": self.notify_threshold_mb,
            "cooldown_s": self.cooldown_s,
            "reset_cap_per_min": self.reset_cap_per_min,
            "last_skip_reason": self.last_skip_reason,
            "last_gobble_reason": self.last_gobble_reason,
        }
        if self.schedule is not None:
            payload["schedule"] = self.schedule.to_json()
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> "TargetConfig":
        schedule = TargetSchedule.from_json(payload.get("schedule"))
        history = payload.get("history") or []
        if isinstance(history, Sequence):
            history_vals = tuple(float(x) for x in list(history)[-40:])
        else:
            history_vals = ()
        return cls(
            key=str(payload.get("key", "")),
            match_by=str(payload.get("match_by", "name")),
            uid=str(payload.get("uid", "")),
            group_id=str(payload.get("group_id", "")),
            active=bool(payload.get("active", False)),
            interval_sec=float(payload.get("interval_sec", DEFAULT_INTERVAL)),
            last_gobble_ts=float(payload.get("last_gobble_ts", 0.0)),
            last_result=str(payload.get("last_result", "")),
            autostart=bool(payload.get("autostart", False)),
            include_children=bool(payload.get("include_children", False)),
            last_delta_mb=float(payload.get("last_delta_mb", 0.0)),
            session_reclaimed_mb=float(payload.get("session_reclaimed_mb", 0.0)),
            day_reclaimed_mb=float(payload.get("day_reclaimed_mb", 0.0)),
            day_key=str(payload.get("day_key", "")),
            history=history_vals,
            adaptive=bool(payload.get("adaptive", True)),
            cpu_skip_threshold=int(payload.get("cpu_skip_threshold", 60)),
            background_only=bool(payload.get("background_only", False)),
            pause_if_fullscreen=bool(payload.get("pause_if_fullscreen", True)),
            strategy=str(payload.get("strategy", "standard")),
            last_total_rss_mb=float(payload.get("last_total_rss_mb", 0.0)),
            notify_threshold_mb=float(payload.get("notify_threshold_mb", 0.0)),
            cooldown_s=int(payload.get("cooldown_s", 0)),
            reset_cap_per_min=int(payload.get("reset_cap_per_min", 3)),
            schedule=schedule,
            last_skip_reason=str(payload.get("last_skip_reason", "")),
            last_gobble_reason=str(payload.get("last_gobble_reason", "")),
        ).normalised()


@dataclass(frozen=True)
class AppConfig:
    """Composite configuration for the application."""

    targets: Tuple[TargetConfig, ...] = ()
    groups: Tuple[GroupConfig, ...] = ()
    theme: str = "dark"
    theme_colors: Mapping[str, str] = field(default_factory=dict)
    custom_theme_colors: Mapping[str, str] = field(default_factory=dict)
    tray_mode: bool = True
    startup_enabled: bool = False
    hotkey: str = ""
    gobble_on_wake: bool = False
    show_system: bool = False
    protect_system: bool = True
    pressure_enabled: bool = True
    min_free_percent: int = 25
    gaming_mode: bool = True
    battery_mode: str = "slow"
    battery_slow_factor: float = 2.0
    maintenance_window: Mapping[str, object] = field(
        default_factory=lambda: {"start": "00:00", "end": "00:00", "aggressive": False}
    )
    idle_seconds: int = 0
    jitter_pct: int = 10
    metered: bool = False
    daily_recap: Mapping[str, object] = field(default_factory=lambda: {"enabled": False, "time": "00:00"})
    diag_level: int = 0
    version: int = 5

    def with_targets(self, targets: Iterable[TargetConfig]) -> "AppConfig":
        return replace(self, targets=tuple(t.normalised() for t in targets))

    def update_target(self, uid: str, target: TargetConfig) -> "AppConfig":
        updated = [t for t in self.targets if t.uid != uid]
        updated.append(target.normalised())
        updated.sort(key=lambda t: t.uid)
        return replace(self, targets=tuple(updated))

    def replace_target(self, predicate_uid: str, new_target: TargetConfig) -> "AppConfig":
        updated = []
        for t in self.targets:
            if t.uid == predicate_uid:
                updated.append(new_target.normalised())
            else:
                updated.append(t)
        return replace(self, targets=tuple(updated))

    def remove_target(self, uid: str) -> "AppConfig":
        return replace(self, targets=tuple(t for t in self.targets if t.uid != uid))

    def find_target(self, uid: str) -> TargetConfig | None:
        for target in self.targets:
            if target.uid == uid:
                return target
        return None

    def iter_targets(self) -> Iterator[TargetConfig]:
        return iter(self.targets)

    def to_json(self) -> Mapping[str, object]:
        return {
            "targets": [t.to_json() for t in self.targets],
            "groups": [g.to_json() for g in self.groups],
            "theme": self.theme,
            "theme_colors": dict(self.theme_colors),
            "custom_theme_colors": dict(self.custom_theme_colors),
            "tray_mode": self.tray_mode,
            "startup_enabled": self.startup_enabled,
            "hotkey": self.hotkey,
            "gobble_on_wake": self.gobble_on_wake,
            "show_system": self.show_system,
            "protect_system": self.protect_system,
            "pressure_enabled": self.pressure_enabled,
            "min_free_percent": self.min_free_percent,
            "gaming_mode": self.gaming_mode,
            "battery_mode": self.battery_mode,
            "battery_slow_factor": self.battery_slow_factor,
            "maintenance_window": dict(self.maintenance_window),
            "idle_seconds": self.idle_seconds,
            "jitter_pct": self.jitter_pct,
            "metered": self.metered,
            "daily_recap": dict(self.daily_recap),
            "diag_level": self.diag_level,
            "version": self.version,
        }


__all__ = ["GroupConfig", "TargetConfig", "TargetSchedule", "AppConfig"]
