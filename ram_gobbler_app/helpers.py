"""Low level Windows helpers used by the gobbler and UI layers.

The original monolithic script in this project mingled direct Win32 calls with
UI code, which made it hard for newcomers to follow the flow.  Collecting the
helpers in a dedicated module – and documenting each one thoroughly – keeps the
"what" and "why" in one place so readers can understand the memory-reclamation
pipeline without prior Windows API knowledge.
"""

from __future__ import annotations

import ctypes
import json
from typing import Optional, Tuple

from .constants import HELPER_PIPE, LEGACY_PREFIX
from .deps import psutil
from .platform import (
    ACCESS,
    CloseHandle,
    EmptyWorkingSet,
    GetForegroundWindow,
    GetSystemMetrics,
    GetWindowRect,
    GetWindowThreadProcessId,
    OpenProcess,
    RECT,
    SM_CXSCREEN,
    SM_CYSCREEN,
    SetProcessWorkingSetSizeEx,
    QUOTA_FLAGS,
    wintypes,
)


def empty_working_set(pid: int) -> bool:
    """Invoke ``EmptyWorkingSet`` for ``pid``.

    The helper mirrors the original script which used the Win32 API directly.
    ``ACCESS`` is the desired permission mask exported by ``platform``.
    """

    handle = OpenProcess(ACCESS, False, pid)
    if not handle:
        return False
    try:
        return bool(EmptyWorkingSet(handle))
    finally:
        CloseHandle(handle)


def reset_working_set(pid: int) -> bool:
    """Use ``SetProcessWorkingSetSizeEx`` to force a working-set rebuild."""

    handle = OpenProcess(ACCESS, False, pid)
    if not handle:
        return False
    try:
        return bool(
            SetProcessWorkingSetSizeEx(
                handle,
                ctypes.c_size_t(-1 & 0xFFFFFFFFFFFFFFFF),
                ctypes.c_size_t(-1 & 0xFFFFFFFFFFFFFFFF),
                QUOTA_FLAGS,
            )
        )
    finally:
        CloseHandle(handle)


def rss_mb(pid: int) -> Optional[float]:
    """Return the RSS value for ``pid`` in megabytes."""

    try:
        proc = psutil.Process(pid)
        return proc.memory_info().rss / (1024 * 1024)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def helper_gobble(pid: int, strategy: str) -> Tuple[bool, float]:
    """Request an elevated gobble via the optional helper service."""

    def _request(action: str) -> Tuple[bool, float]:
        try:
            import win32file  # type: ignore
        except Exception:
            return False, 0.0
        try:
            handle = win32file.CreateFile(
                HELPER_PIPE,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
        except Exception:
            return False, 0.0
        try:
            payload = json.dumps({"action": action, "pid": int(pid), "strategy": strategy}).encode("utf-8")
            win32file.WriteFile(handle, payload)
            response = win32file.ReadFile(handle, 65536)[1]
            data = json.loads(response.decode("utf-8"))
            return bool(data.get("ok")), float(data.get("delta_mb") or 0.0)
        except Exception:
            return False, 0.0
        finally:
            win32file.CloseHandle(handle)

    ok, delta = _request("gobble")
    if not ok:
        legacy_ok, legacy_delta = _request(LEGACY_PREFIX)
        if legacy_ok:
            return legacy_ok, legacy_delta
    return ok, delta


def get_foreground_pid() -> Optional[int]:
    """Best-effort lookup of the PID owning the active window."""

    try:
        hwnd = GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) if pid.value else None
    except Exception:
        return None


def is_fullscreen_active() -> bool:
    """Return ``True`` when the foreground window likely covers the screen."""

    try:
        hwnd = GetForegroundWindow()
        if not hwnd:
            return False
        rect = RECT()
        if not GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        screen_w = GetSystemMetrics(SM_CXSCREEN)
        screen_h = GetSystemMetrics(SM_CYSCREEN)
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        return width >= 0.95 * screen_w and height >= 0.95 * screen_h
    except Exception:
        return False


def on_battery() -> Tuple[bool, bool]:
    """Return ``(on_battery, critically_low)`` using :mod:`psutil`."""

    try:
        batt = psutil.sensors_battery()
        if not batt:
            return False, False
        return (batt.power_plugged is False), (batt.secsleft is not None and batt.secsleft < 1800)
    except Exception:
        return False, False


__all__ = [
    "empty_working_set",
    "reset_working_set",
    "rss_mb",
    "helper_gobble",
    "get_foreground_pid",
    "is_fullscreen_active",
    "on_battery",
]
