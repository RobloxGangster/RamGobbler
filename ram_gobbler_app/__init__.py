"""Convenience exports for the reorganised Ram Gobbler application.

The legacy application exposed a large set of globals from ``ram_gobbler``
itself.  External scripts – including some bundled test utilities – still
import those names.  Re-exporting them from the package ``__init__`` keeps the
public API stable while allowing the implementation to be split into focused
modules.
"""
from __future__ import annotations

from .app import main, parse_args
from .constants import (
    APP_NAME,
    BACKUP_DIR,
    CONFIG_DIR,
    CONFIG_PATH,
    DEFAULT_INTERVAL,
    MIN_INTERVAL,
    HELPER_PIPE,
    LEGACY_LAST_TS_KEY,
    LEGACY_PREFIX,
    LEGACY_REASON_KEY,
    LEGACY_WAKE_KEY,
    METRICS_PATH,
)
from .gobbler import Gobbler
from .models import Config, Group, Target
from .processes import estimate_reclaim

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
    "Config",
    "Group",
    "Target",
    "Gobbler",
    "estimate_reclaim",
    "parse_args",
    "main",
]
