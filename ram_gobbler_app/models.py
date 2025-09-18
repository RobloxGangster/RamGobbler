"""Data models for Ram Gobbler."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Deque, List, Optional
from collections import deque

from .constants import (
    BACKUP_DIR,
    CONFIG_PATH,
    DEFAULT_INTERVAL,
    LEGACY_LAST_TS_KEY,
    LEGACY_PREFIX,
    LEGACY_REASON_KEY,
    LEGACY_WAKE_KEY,
)
from .startup import is_startup_enabled


@dataclass
class Group:
    """User-defined collection used to organise targets in the UI."""

    id: str
    name: str
    color: Optional[str] = None


@dataclass
class Target:
    """Persisted configuration describing a process to gobble."""

    key: str
    match_by: str
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    group_id: str = ""
    active: bool = False
    interval_sec: int = DEFAULT_INTERVAL
    last_gobble_ts: float = 0.0
    last_result: str = ""
    autostart: bool = False
    last_delta_mb: float = 0.0
    session_reclaimed_mb: float = 0.0
    day_reclaimed_mb: float = 0.0
    day_key: str = ""
    history: List[float] = field(default_factory=list)
    adaptive: bool = True
    cpu_skip_threshold: int = 60
    background_only: bool = False
    pause_if_fullscreen: bool = True
    strategy: str = "standard"
    last_total_rss_mb: float = 0.0
    notify_threshold_mb: float = 0.0
    cooldown_s: int = 0
    reset_cap_per_min: int = 3
    schedule: Optional[dict] = None
    last_skip_reason: str = ""
    last_gobble_reason: str = ""
    _events: Deque[dict] = field(default_factory=lambda: deque(maxlen=200), repr=False)
    next_allowed_ts: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict:
        """Serialise the target for JSON persistence."""

        payload = asdict(self)
        payload.pop("_events", None)
        payload.pop("next_allowed_ts", None)
        payload["history"] = self.history[-40:]
        return payload

    @staticmethod
    def from_dict(raw: dict) -> "Target":
        """Construct a :class:`Target` from a raw dictionary."""

        return Target(
            key=raw.get("key", ""),
            match_by=raw.get("match_by", "name"),
            uid=raw.get("uid") or uuid.uuid4().hex[:10],
            active=bool(raw.get("active", False)),
            interval_sec=int(raw.get("interval_sec", DEFAULT_INTERVAL)),
            last_gobble_ts=float(raw.get("last_gobble_ts", raw.get(LEGACY_LAST_TS_KEY, 0.0))),
            last_result=raw.get("last_result", ""),
            autostart=bool(raw.get("autostart", False)),
            last_delta_mb=float(raw.get("last_delta_mb", 0.0)),
            session_reclaimed_mb=float(raw.get("session_reclaimed_mb", 0.0)),
            day_reclaimed_mb=float(raw.get("day_reclaimed_mb", 0.0)),
            day_key=raw.get("day_key", ""),
            history=list(raw.get("history", []))[:40],
            adaptive=bool(raw.get("adaptive", True)),
            cpu_skip_threshold=int(raw.get("cpu_skip_threshold", 60)),
            background_only=bool(raw.get("background_only", False)),
            pause_if_fullscreen=bool(raw.get("pause_if_fullscreen", True)),
            strategy=raw.get("strategy", "standard"),
            last_total_rss_mb=float(raw.get("last_total_rss_mb", 0.0)),
            notify_threshold_mb=float(raw.get("notify_threshold_mb", 0.0)),
            cooldown_s=int(raw.get("cooldown_s", 0)),
            reset_cap_per_min=int(raw.get("reset_cap_per_min", 3)),
            schedule=raw.get("schedule"),
            last_skip_reason=raw.get("last_skip_reason", ""),
            last_gobble_reason=raw.get("last_gobble_reason", raw.get(LEGACY_REASON_KEY, "")),
            group_id=raw.get("group_id", ""),
        )


class Config:
    """High-level configuration manager backed by a JSON file."""

    def __init__(self, path: str):
        self.path = path
        self.targets: List[Target] = []
        self.groups: List[Group] = []
        self.theme: str = "dark"
        self.tray_mode: bool = True
        self.startup_enabled: bool = False
        self.hotkey: str = ""
        self.gobble_on_wake: bool = False
        self.show_system: bool = False
        self.protect_system: bool = True
        self.pressure_enabled: bool = True
        self.min_free_percent: int = 25
        self.gaming_mode: bool = True
        self.battery_mode: str = "slow"
        self.battery_slow_factor: float = 2.0
        self.maintenance_window: dict = {"start": "00:00", "end": "00:00", "aggressive": False}
        self.idle_seconds: int = 0
        self.jitter_pct: int = 10
        self.metered: bool = False
        self.daily_recap: dict = {"enabled": False, "time": "00:00"}
        self.diag_level: int = 0
        self.version: int = 5

    def load(self) -> None:
        """Load configuration from disk, creating defaults when necessary."""

        dir_path = os.path.dirname(self.path) or "."
        os.makedirs(dir_path, exist_ok=True)
        if not os.path.exists(self.path):
            self.startup_enabled = is_startup_enabled()
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self.targets = [Target.from_dict(t) for t in raw.get("targets", [])]
            self.groups = [
                Group(id=g.get("id", ""), name=g.get("name", ""), color=g.get("color"))
                for g in raw.get("groups", [])
            ]
            self.theme = raw.get("theme", self.theme)
            self.tray_mode = bool(raw.get("tray_mode", self.tray_mode))
            self.startup_enabled = bool(raw.get("startup_enabled", is_startup_enabled()))
            self.hotkey = raw.get("hotkey", "")
            wake_val = raw.get("gobble_on_wake", raw.get(LEGACY_WAKE_KEY, False))
            self.gobble_on_wake = bool(wake_val)
            self.show_system = bool(raw.get("show_system", False))
            self.protect_system = bool(raw.get("protect_system", True))
            self.pressure_enabled = bool(raw.get("pressure_enabled", True))
            self.min_free_percent = int(raw.get("min_free_percent", 25))
            self.gaming_mode = bool(raw.get("gaming_mode", True))
            self.battery_mode = raw.get("battery_mode", "slow")
            self.battery_slow_factor = float(raw.get("battery_slow_factor", 2.0))
            self.maintenance_window = raw.get("maintenance_window", self.maintenance_window)
            self.idle_seconds = int(raw.get("idle_seconds", 0))
            self.jitter_pct = int(raw.get("jitter_pct", 10))
            self.metered = bool(raw.get("metered", False))
            self.daily_recap = raw.get("daily_recap", self.daily_recap)
            self.diag_level = int(raw.get("diag_level", 0))
            self.version = max(int(raw.get("version", 1)), 5)
        except Exception:
            # A corrupted configuration should not prevent the UI from loading;
            # we simply start with an empty list of targets.
            self.targets = []

    def save(self) -> None:
        """Persist the current configuration, keeping timestamped backups."""

        dir_path = os.path.dirname(self.path) or "."
        os.makedirs(dir_path, exist_ok=True)
        try:
            if os.path.exists(self.path):
                os.makedirs(BACKUP_DIR, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(self.path, os.path.join(BACKUP_DIR, f"config-{timestamp}.json"))
        except Exception:
            pass
        payload = {
            "targets": [t.to_dict() for t in self.targets],
            "groups": [asdict(g) for g in self.groups],
            "theme": self.theme,
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
            "maintenance_window": self.maintenance_window,
            "idle_seconds": self.idle_seconds,
            "jitter_pct": self.jitter_pct,
            "metered": self.metered,
            "daily_recap": self.daily_recap,
            "diag_level": self.diag_level,
            "version": self.version,
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)


__all__ = ["Group", "Target", "Config", "CONFIG_PATH"]
