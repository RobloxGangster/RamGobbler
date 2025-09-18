"""Windows startup integration helpers."""
from __future__ import annotations

import os
import sys

from .platform import RUN_KEY, RUN_VALUE, winreg

IS_WINDOWS = os.name == "nt"


def _pythonw_path() -> str:
    """Return the path to ``pythonw.exe`` when available."""

    if getattr(sys, "frozen", False):
        return sys.executable
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pythonw = exe[:-10] + "pythonw.exe"
        if os.path.exists(pythonw):
            return pythonw
    return exe


def _startup_command() -> str:
    """Build the command string stored in the registry."""

    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.abspath(sys.argv[0])
    return f'"{_pythonw_path()}" "{script}"'


def is_startup_enabled() -> bool:
    """Return ``True`` if the registry contains our Run key."""

    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:  # type: ignore[attr-defined]
            val, _ = winreg.QueryValueEx(key, RUN_VALUE)  # type: ignore[attr-defined]
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> tuple[bool, str]:
    """Create or remove the Run key depending on ``enabled``."""

    if not IS_WINDOWS:
        return False, "Start with Windows is only supported on Windows"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:  # type: ignore[attr-defined]
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _startup_command())  # type: ignore[attr-defined]
                return True, "Enabled Start with Windows"
            try:
                winreg.DeleteValue(key, RUN_VALUE)  # type: ignore[attr-defined]
            except FileNotFoundError:
                pass
            return True, "Disabled Start with Windows"
    except OSError as exc:
        return False, f"Registry error: {exc}"


__all__ = ["is_startup_enabled", "set_startup_enabled"]
