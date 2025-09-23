"""Public exports for the refactored Ram Gobbler package."""
from __future__ import annotations

from .constants import (
    APP_NAME,
    BACKUP_DIR,
    CONFIG_DIR,
    CONFIG_PATH,
    DEFAULT_INTERVAL,
    HELPER_PIPE,
    LEGACY_LAST_TS_KEY,
    LEGACY_PREFIX,
    LEGACY_REASON_KEY,
    LEGACY_WAKE_KEY,
    METRICS_PATH,
    MIN_INTERVAL,
)
from .app import main, parse_args
from .core.config_repository import ConfigRepository
from .core.models import AppConfig, GroupConfig, TargetConfig
from .core.schedule import gate_reason, jittered_interval, schedule_allows_now, window_active
from .core.palette import blend_colors, darken_color, lighten_color, normalize_color
from .gobbler import Gobbler
from .models import Config, Group, Target
from .processes import estimate_reclaim
from .services.metrics_service import MetricsService
from .services.process_matcher import ProcessMatcher, ProcessNode
from .services.gobble_service import GobbleService
from .services.scheduler_service import SchedulerService

__all__ = [
    "APP_NAME",
    "BACKUP_DIR",
    "CONFIG_DIR",
    "CONFIG_PATH",
    "DEFAULT_INTERVAL",
    "MIN_INTERVAL",
    "HELPER_PIPE",
    "LEGACY_LAST_TS_KEY",
    "LEGACY_PREFIX",
    "LEGACY_REASON_KEY",
    "LEGACY_WAKE_KEY",
    "METRICS_PATH",
    "AppConfig",
    "GroupConfig",
    "TargetConfig",
    "ConfigRepository",
    "Config",
    "Group",
    "Target",
    "Gobbler",
    "estimate_reclaim",
    "MetricsService",
    "ProcessMatcher",
    "ProcessNode",
    "GobbleService",
    "SchedulerService",
    "gate_reason",
    "jittered_interval",
    "schedule_allows_now",
    "window_active",
    "blend_colors",
    "darken_color",
    "lighten_color",
    "normalize_color",
    "parse_args",
    "main",
]
