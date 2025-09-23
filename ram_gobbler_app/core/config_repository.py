"""Configuration repository providing validated dataclass objects."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, MutableMapping, Sequence

from ..constants import BACKUP_DIR, CONFIG_PATH
from ..startup import is_startup_enabled
from .models import AppConfig, GroupConfig, TargetConfig

ConfigMutator = Callable[[AppConfig], AppConfig]


class ConfigRepository:
    """Load and persist :class:`AppConfig` instances with validation."""

    def __init__(
        self,
        path: str = CONFIG_PATH,
        backup_dir: str = BACKUP_DIR,
        *,
        retention: int = 5,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)
        self.retention = max(1, retention)
        self._last_loaded: AppConfig | None = None

    # ------------------------------------------------------------------ loading
    def load(self) -> AppConfig:
        """Load configuration from disk, building defaults on failure."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            config = self._default_config()
            self._last_loaded = config
            return config
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            # Corrupt files are renamed with a timestamp suffix so the user can
            # inspect them later.  A fresh default configuration is returned.
            corrupt = self.backup_dir / f"config-corrupt-{int(time.time())}.json"
            corrupt.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(self.path, corrupt)
            except Exception:
                pass
            config = self._default_config()
            self._last_loaded = config
            return config
        config = self._parse_config(payload)
        self._last_loaded = config
        return config

    def _parse_config(self, payload: Mapping[str, object]) -> AppConfig:
        """Build an :class:`AppConfig` from raw JSON payload."""

        targets: Iterable[TargetConfig]
        raw_targets = payload.get("targets") or []
        if isinstance(raw_targets, Sequence):
            targets = tuple(
                TargetConfig.from_json(t).normalised()
                for t in raw_targets
                if isinstance(t, Mapping)
            )
        else:
            targets = ()

        groups: Iterable[GroupConfig]
        raw_groups = payload.get("groups") or []
        if isinstance(raw_groups, Sequence):
            groups = tuple(
                GroupConfig(
                    id=str(g.get("id", "")),
                    name=str(g.get("name", "")),
                    color=(None if g.get("color") in {None, ""} else str(g.get("color"))),
                )
                for g in raw_groups
                if isinstance(g, Mapping)
            )
        else:
            groups = ()

        theme = str(payload.get("theme", "dark"))
        theme_colors = self._filter_palette(payload.get("theme_colors"), default={})
        custom_theme_colors = self._filter_palette(
            payload.get("custom_theme_colors"), default=theme_colors
        )

        maintenance = payload.get("maintenance_window")
        if isinstance(maintenance, Mapping):
            maintenance_window: MutableMapping[str, object] = {
                "start": str(maintenance.get("start", "00:00")),
                "end": str(maintenance.get("end", "00:00")),
                "aggressive": bool(maintenance.get("aggressive", False)),
            }
        else:
            maintenance_window = {"start": "00:00", "end": "00:00", "aggressive": False}

        daily_recap = payload.get("daily_recap")
        if isinstance(daily_recap, Mapping):
            recap = {
                "enabled": bool(daily_recap.get("enabled", False)),
                "time": str(daily_recap.get("time", "00:00")),
            }
        else:
            recap = {"enabled": False, "time": "00:00"}

        config = AppConfig(
            targets=tuple(targets),
            groups=tuple(groups),
            theme=theme,
            theme_colors=theme_colors or {},
            custom_theme_colors=custom_theme_colors or {},
            tray_mode=bool(payload.get("tray_mode", True)),
            startup_enabled=bool(payload.get("startup_enabled", is_startup_enabled())),
            hotkey=str(payload.get("hotkey", "")),
            gobble_on_wake=bool(payload.get("gobble_on_wake", False)),
            show_system=bool(payload.get("show_system", False)),
            protect_system=bool(payload.get("protect_system", True)),
            pressure_enabled=bool(payload.get("pressure_enabled", True)),
            min_free_percent=int(payload.get("min_free_percent", 25)),
            gaming_mode=bool(payload.get("gaming_mode", True)),
            battery_mode=str(payload.get("battery_mode", "slow")),
            battery_slow_factor=float(payload.get("battery_slow_factor", 2.0)),
            maintenance_window=maintenance_window,
            idle_seconds=int(payload.get("idle_seconds", 0)),
            jitter_pct=int(payload.get("jitter_pct", 10)),
            metered=bool(payload.get("metered", False)),
            daily_recap=recap,
            diag_level=int(payload.get("diag_level", 0)),
            version=max(int(payload.get("version", 5)), 5),
        )
        return config

    def _filter_palette(self, raw: object, *, default: Mapping[str, str]) -> Mapping[str, str]:
        if not isinstance(raw, Mapping):
            return dict(default)
        result: dict[str, str] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str):
                result[key] = value
        return result or dict(default)

    def _default_config(self) -> AppConfig:
        return AppConfig(startup_enabled=is_startup_enabled())

    # ------------------------------------------------------------------ saving
    def save(self, config: AppConfig) -> None:
        """Persist ``config`` to disk with backups."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config.to_json(), indent=2, sort_keys=True)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="ram-gobbler", suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            self._rotate_backups()
            if self.path.exists():
                timestamp = int(time.time())
                backup_path = self.backup_dir / f"config-{timestamp}.json"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(self.path, backup_path)
                except Exception:
                    pass
            os.replace(tmp_path, self.path)
            self._last_loaded = config
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)

    def mutate(self, mutator: ConfigMutator) -> AppConfig:
        """Apply ``mutator`` to the last loaded configuration and persist."""

        base = self._last_loaded or self.load()
        updated = mutator(base)
        if updated is base:
            return updated
        self.save(updated)
        return updated

    def _rotate_backups(self) -> None:
        if not self.backup_dir.exists():
            return
        backups = sorted(
            [p for p in self.backup_dir.glob("config-*.json") if p.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[self.retention :]:
            with contextlib.suppress(Exception):
                stale.unlink()


__all__ = ["ConfigRepository", "ConfigMutator"]
