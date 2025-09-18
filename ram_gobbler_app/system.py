"""Heuristics that keep Ram Gobbler away from critical system processes.

The gobbler aims to be aggressive yet safe.  These helpers collect the safety
checks in one place and explain *why* each rule exists so that readers without
deep Windows knowledge can trust the behaviour and tweak it confidently.
"""

from __future__ import annotations

import os
from typing import Optional

from .deps import psutil

CRITICAL_NAMES = {
    "System",
    "Registry",
    "MemCompression",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "RuntimeBroker.exe",
    "dwm.exe",
}


def is_system_exe(path: Optional[str]) -> bool:
    """Return ``True`` if ``path`` lives inside the Windows directory."""

    if not path:
        return False
    try:
        windir = os.environ.get("WINDIR", r"C:\\Windows")
        windir = os.path.normcase(windir)
        p = os.path.normcase(path)
        return p.startswith(windir)
    except Exception:
        return False


def is_system_process(proc: psutil.Process) -> bool:
    """Best-effort detection of processes that should not be touched."""

    try:
        name = (proc.info.get("name") if hasattr(proc, "info") else None) or proc.name() or ""
        if name in CRITICAL_NAMES:
            return True
        if is_system_exe((proc.info.get("exe") if hasattr(proc, "info") else None) or proc.exe()):
            return True
        try:
            user = proc.username()
            if user.upper().endswith("\\SYSTEM") or user.upper().startswith("NT AUTHORITY\\"):
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return True
    return False


__all__ = ["is_system_exe", "is_system_process", "CRITICAL_NAMES"]
