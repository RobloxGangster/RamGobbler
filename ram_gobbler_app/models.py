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
    MIN_INTERVAL,
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
    interval_sec: float = float(DEFAULT_INTERVAL)
    last_gobble_ts: float = 0.0
    last_result: str = ""
    autostart: bool = False
    include_children: bool = False
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

    def __post_init__(self) -> None:
        """Normalise interval bounds eagerly so background workers stay safe."""

        try:
            self.interval_sec = max(MIN_INTERVAL, float(self.interval_sec))
        except Exception:
            self.interval_sec = float(DEFAULT_INTERVAL)

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
            interval_sec=max(MIN_INTERVAL, float(raw.get("interval_sec", DEFAULT_INTERVAL))),
            last_gobble_ts=float(raw.get("last_gobble_ts", raw.get(LEGACY_LAST_TS_KEY, 0.0))),
            last_result=raw.get("last_result", ""),
            autostart=bool(raw.get("autostart", False)),
            include_children=bool(raw.get("include_children", False)),
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
        self.theme_colors: dict[str, str] = self.default_theme_colors(self.theme)
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
            raw_theme_colors = raw.get("theme_colors", {})
            if isinstance(raw_theme_colors, dict):
                self.theme_colors = {
                    key: value
                    for key, value in raw_theme_colors.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
            else:
                self.theme_colors = {}
        except Exception:
            # A corrupted configuration should not prevent the UI from loading;
            # we simply start with an empty list of targets.
            self.targets = []
        finally:
            # Ensure theme palette always has sensible defaults.
            self.theme_colors = self.theme_palette_overrides()

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
            "theme_colors": self.theme_palette_overrides(),
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    @classmethod
    def default_theme_colors(cls, theme: str) -> dict[str, str]:
        """Return the baseline palette for ``theme``."""

        defaults = {
            "light": {
                "text": "#111827",
                "background": "#e7ecf6",
                "button": "#4c6ef5",
            },
            "dark": {
                "text": "#e5e9f5",
                "background": "#0c0f19",
                "button": "#5b82ff",
            },
        }
        key = "light" if theme == "light" else "dark"
        return dict(defaults[key])

    def theme_palette(self) -> dict[str, str]:
        """Combine theme defaults with any persisted overrides."""

        palette = self.default_theme_colors(self.theme)
        overrides = self.theme_palette_overrides()
        palette.update(
            {
                key: value
                for key, value in overrides.items()
                if key in {"text", "background", "button"} and isinstance(value, str)
            }
        )
        return palette

    def theme_palette_overrides(self) -> dict[str, str]:
        """Return validated overrides, falling back to defaults when empty."""

        if not isinstance(self.theme_colors, dict):
            return self.default_theme_colors(self.theme)
        merged: dict[str, str] = {}
        for key in ("text", "background", "button"):
            value = self.theme_colors.get(key)
            if isinstance(value, str) and value:
                merged[key] = value
        if len(merged) != 3:
            defaults = self.default_theme_colors(self.theme)
            for key, value in defaults.items():
                merged.setdefault(key, value)
        return merged

    def reset_theme_colors(self, theme: Optional[str] = None) -> None:
        """Reset colour overrides to the defaults for ``theme``."""

        target_theme = theme if theme is not None else self.theme
        self.theme_colors = self.default_theme_colors(target_theme)


__all__ = ["Group", "Target", "Config", "CONFIG_PATH"]
