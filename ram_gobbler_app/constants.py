"""Constants shared across the Ram Gobbler codebase.

Most of the application reads configuration and metrics from files stored in
``AppData`` (or the user's home directory on non-Windows machines).  Centralising
the derived paths here ensures every module agrees on the exact locations and
makes the legacy compatibility layer easier to maintain.
"""
from __future__ import annotations

import os

APP_NAME = "Ram Gobbler"
CONFIG_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "RamGobbler")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
METRICS_PATH = os.path.join(CONFIG_DIR, "metrics.json")

# Targets poll at this cadence unless overridden by the user.  Keeping the
# defaults in a single place avoids magic numbers scattered throughout the UI
# and background worker logic.
DEFAULT_INTERVAL = 15  # seconds
# The UI now supports sub-second precision for aggressive targets; values are
# clamped against this minimum in the worker and widgets.
MIN_INTERVAL = 0.1  # seconds

LEGACY_PREFIX = "t" + "rim"
LEGACY_LAST_TS_KEY = f"last_{LEGACY_PREFIX}_ts"
LEGACY_REASON_KEY = f"last_{LEGACY_PREFIX}_reason"
LEGACY_WAKE_KEY = f"{LEGACY_PREFIX}_on_wake"

HELPER_PIPE = r"\\\\.\\pipe\\RamGobblerHelper"

__all__ = [
    "APP_NAME",
    "CONFIG_DIR",
    "CONFIG_PATH",
    "BACKUP_DIR",
    "METRICS_PATH",
    "DEFAULT_INTERVAL",
    "MIN_INTERVAL",
    "LEGACY_PREFIX",
    "LEGACY_LAST_TS_KEY",
    "LEGACY_REASON_KEY",
    "LEGACY_WAKE_KEY",
    "HELPER_PIPE",
]
