#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ram Gobbler — Windows memory trimmer with a clean UI

New features added:
• Light theme support (toggle in toolbar; saved to config)
• System tray mode (minimize on close; tray menu for quick actions)
• Auto‑start gobbling per target (saved)
• One‑click “Trim Now” for selected processes
• System‑wide “Trim Visible Targets Now” (toolbar + tray + global hotkey)
• Start with Windows (HKCU Run registry toggle)
• Global keyboard shortcut for “Trim Visible Targets Now” (user-configurable)
• Optional “Trim on wake from sleep”
• Reclaimed MB counters + history (per target sparkline, session/day totals, global session total in status bar)
• Safe process guard (hide/protect system processes, with toggle)
• Profiles (Balanced/Aggressive/Gentle) to set intervals
• Import/Export settings + auto-backup on each save

Batch 2 — Smarter trimming & power-friendly:
• Adaptive intervals (per-target) based on recent deltas/growth
• Pressure-aware trims (only when low free RAM)
• Per-target rules: CPU threshold, background-only, pause-if-fullscreen, strategy
• Gaming Mode: pause trims while a full-screen window is active
• Battery Saver: slow or pause trims on battery

Planned next batch (optional, not in this file yet):
• Richer matching (by command line / parent)
• Helper service for elevated trims via named pipe (advanced)

Requirements (Python 3.9+ on Windows):
    pip install PySide6 psutil

Run:
    python ram_gobbler.py

Build a standalone .exe (optional):
    pip install pyinstaller
    pyinstaller -F -w -n "RamGobbler" ram_gobbler.py

Notes:
- For some processes, trimming requires Administrator privileges.
- Trimming too aggressively can cause a little CPU churn or re-paging. Adaptive defaults aim to avoid stutter.
- This app is Windows-only (uses Win32 APIs).
"""

import json
import os
import sys
import time
import threading
import shutil
import uuid
import argparse
import statistics
import importlib
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from collections import deque, defaultdict
from itertools import zip_longest
from types import TracebackType
from typing import Dict, List, Optional, Tuple, Deque, Callable, Set, Iterable, Type
import diagnostics
from groups import group_totals
from metrics import Metrics
from matching import matches
from scheduler import schedule_allows_now, jittered_interval, user_idle_seconds, gate_reason, window_active

APP_NAME = "Ram Gobbler"
CONFIG_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "RamGobbler")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
METRICS_PATH = os.path.join(CONFIG_DIR, "metrics.json")
ERROR_LOG_PATH = os.path.join(CONFIG_DIR, "error.log")
DEFAULT_INTERVAL = 15  # seconds


class _MissingPsutil:
    """Lightweight shim used when ``psutil`` cannot be imported."""

    class _StubPsutilError(RuntimeError):
        """Base class for placeholder psutil exceptions."""

    class AccessDenied(_StubPsutilError):
        """Placeholder for :class:`psutil.AccessDenied`."""

    class NoSuchProcess(_StubPsutilError):
        """Placeholder for :class:`psutil.NoSuchProcess`."""

    Error = _StubPsutilError

    def __init__(self, detail: Optional[str] = None) -> None:
        self.detail = detail or ""

    def _raise(self) -> None:
        raise ModuleNotFoundError(_format_missing_psutil_message(self.detail))

    def Process(self, *_args, **_kwargs):  # pragma: no cover - defensive
        self._raise()

    def process_iter(self, *_args, **_kwargs):  # pragma: no cover - defensive
        self._raise()

    def sensors_battery(self, *_args, **_kwargs):  # pragma: no cover - defensive
        self._raise()

    def virtual_memory(self, *_args, **_kwargs):  # pragma: no cover - defensive
        self._raise()

    def __getattr__(self, _name):  # pragma: no cover - defensive
        self._raise()


def _format_missing_psutil_message(detail: Optional[str]) -> str:
    base = (
        "Ram Gobbler requires the 'psutil' package, but it could not be imported "
        "and the automatic installer failed."
    )
    fix = (
        "Use `python diagnostics.py --install-psutil` or run "
        "`pip install --user psutil` manually to continue."
    )
    if detail:
        detail = detail.strip()
        if detail:
            return f"{base}\n\nInstaller output:\n{detail}\n\n{fix}"
    return f"{base}\n{fix}"


def _format_missing_pyside6_message(detail: Optional[str]) -> str:
    base = (
        "Ram Gobbler requires the 'PySide6' package for its graphical interface, "
        "but it could not be imported and the automatic installer failed."
    )
    fix = (
        "Use `python diagnostics.py --install-pyside6` or run "
        "`pip install --user PySide6` manually to continue."
    )
    if detail:
        detail = detail.strip()
        if detail:
            return f"{base}\n\nInstaller output:\n{detail}\n\n{fix}"
    return f"{base}\n{fix}"


def _log_dependency_error(tag: str, message: str) -> None:
    """Persist a dependency failure to the crash log and surface a prompt."""

    diagnostics.ensure_config_dir()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {tag} {timestamp} ===\n{message}\n")
    except Exception:
        pass

    if os.name == "nt":
        try:
            import ctypes  # local import to avoid stubbed environments

            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0)
        except Exception:
            print(message, file=sys.stderr)
    else:
        print(message, file=sys.stderr)


_PSUTIL_BOOTSTRAP_ERROR: Optional[str] = None
_PSUTIL_WARNING_EMITTED = False
_PYSIDE6_BOOTSTRAP_ERROR: Optional[str] = None
_PYSIDE6_WARNING_EMITTED = False
_ERROR_LOG_ANNOUNCED = False
_EXCEPTION_HANDLERS_INSTALLED = False
_ORIGINAL_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREAD_EXCEPTHOOK = getattr(threading, "excepthook", None)


def _log_exception(
    context: str,
    exc_info: Optional[Tuple[Type[BaseException], BaseException, Optional[TracebackType]]] = None,
) -> None:
    """Log ``context`` and the provided (or active) exception details."""

    if exc_info is None or not exc_info[0]:
        exc_info = sys.exc_info()  # type: ignore[assignment]
    if exc_info and exc_info[0]:
        err = "".join(traceback.format_exception(*exc_info))
    else:
        err = traceback.format_exc()
    err = (err or "").rstrip() or "<no traceback available>"

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    header = f"[{APP_NAME}] {context}"
    message = f"{header}\n{err}".rstrip()
    log_path = os.path.abspath(ERROR_LOG_PATH)

    try:
        print(message, file=sys.stderr, flush=True)
        print(f"[{APP_NAME}] Details were also saved to: {log_path}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _announce_error_log_location() -> None:
    """Print the crash-log path so command prompt users can find it quickly."""

    global _ERROR_LOG_ANNOUNCED
    if _ERROR_LOG_ANNOUNCED:
        return
    _ERROR_LOG_ANNOUNCED = True
    diagnostics.ensure_config_dir()
    path = os.path.abspath(ERROR_LOG_PATH)
    try:
        print(f"[{APP_NAME}] Crash reports will be saved to: {path}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _install_global_exception_handlers() -> None:
    """Route unhandled exceptions through :func:`_log_exception`."""

    global _EXCEPTION_HANDLERS_INSTALLED
    if _EXCEPTION_HANDLERS_INSTALLED:
        return
    _EXCEPTION_HANDLERS_INSTALLED = True

    original_excepthook = _ORIGINAL_EXCEPTHOOK

    def handle_exception(exc_type, exc_value, exc_tb):
        if exc_type and issubclass(exc_type, KeyboardInterrupt):
            if original_excepthook not in (None, handle_exception):
                try:
                    original_excepthook(exc_type, exc_value, exc_tb)
                except Exception:
                    pass
            return

        _log_exception("Unhandled exception", (exc_type, exc_value, exc_tb))
        if original_excepthook not in (None, handle_exception):
            try:
                original_excepthook(exc_type, exc_value, exc_tb)
            except Exception:
                pass

    sys.excepthook = handle_exception

    thread_hook = getattr(threading, "excepthook", None)
    if thread_hook is None:
        return

    original_thread_hook = _ORIGINAL_THREAD_EXCEPTHOOK

    def handle_thread_exception(args):
        exc_type = getattr(args, "exc_type", None)
        exc_value = getattr(args, "exc_value", None)
        exc_tb = getattr(args, "exc_traceback", None)
        thread = getattr(args, "thread", None)
        if exc_type and not issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            name = getattr(thread, "name", "thread") if thread else "thread"
            _log_exception(f"Unhandled exception in thread {name}", (exc_type, exc_value, exc_tb))
        if original_thread_hook not in (None, handle_thread_exception):
            try:
                original_thread_hook(args)  # type: ignore[misc]
            except Exception:
                pass

    threading.excepthook = handle_thread_exception  # type: ignore[attr-defined]

    try:
        diagnostics.ensure_config_dir()
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {context} {timestamp} ===\n{err}\n")
    except Exception:
        pass

try:
    import psutil  # type: ignore
except ModuleNotFoundError:
    diagnostics.ensure_config_dir()
    ok, output = diagnostics.install_psutil(user=True, quiet=True)
    if ok:
        try:
            importlib.invalidate_caches()
            psutil = importlib.import_module("psutil")  # type: ignore
        except Exception as exc:  # pragma: no cover - defensive
            parts = []
            output_text = (output or "").strip()
            if output_text:
                parts.append(output_text)
            exc_text = str(exc).strip()
            if exc_text:
                parts.append(exc_text)
            detail = "\n".join(parts).strip()
            if not detail:
                detail = "psutil import failed after installation attempt"
            _PSUTIL_BOOTSTRAP_ERROR = detail
            psutil = _MissingPsutil(detail)  # type: ignore[assignment]
    else:
        _PSUTIL_BOOTSTRAP_ERROR = output
        psutil = _MissingPsutil(output)  # type: ignore[assignment]


def psutil_available() -> bool:
    """Return ``True`` when a real ``psutil`` module is available."""

    return not isinstance(psutil, _MissingPsutil)


def ensure_psutil_ready() -> bool:
    """Emit a friendly error and return ``False`` if ``psutil`` is missing."""

    global _PSUTIL_WARNING_EMITTED

    if psutil_available():
        return True
    if _PSUTIL_WARNING_EMITTED:
        return False

    _PSUTIL_WARNING_EMITTED = True
    message = _format_missing_psutil_message(_PSUTIL_BOOTSTRAP_ERROR)
    _log_dependency_error("Missing psutil", message)
    return False


def ensure_pyside6_runtime_ready() -> bool:
    """Ensure the PySide6 dependency is present on Windows systems."""

    global _PYSIDE6_BOOTSTRAP_ERROR, _PYSIDE6_WARNING_EMITTED

    if os.name != "nt":
        return True

    if diagnostics.pyside6_available():
        _PYSIDE6_BOOTSTRAP_ERROR = None
        return True

    diagnostics.ensure_config_dir()
    ok, output = diagnostics.install_pyside6(user=True, quiet=True)
    if ok:
        try:
            importlib.invalidate_caches()
            importlib.import_module("PySide6")
            return True
        except Exception as exc:  # pragma: no cover - defensive
            _PYSIDE6_BOOTSTRAP_ERROR = str(exc)
    else:
        _PYSIDE6_BOOTSTRAP_ERROR = output

    if not _PYSIDE6_WARNING_EMITTED:
        _PYSIDE6_WARNING_EMITTED = True
        message = _format_missing_pyside6_message(_PYSIDE6_BOOTSTRAP_ERROR)
        _log_dependency_error("Missing PySide6", message)
    return False

# --- Platform check & imports ----------------------------------------------
if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import winreg
    try:
        from PySide6.QtCore import (
            Qt, QAbstractItemModel, QModelIndex, QSortFilterProxyModel,
            QTimer, QSize, QAbstractNativeEventFilter, QRect, QEvent, QObject,
            Signal, Slot, QItemSelectionModel, QMetaObject, QThread,
        )
        from PySide6.QtGui import QIcon, QAction, QKeySequence, QPainter, QPen
        from PySide6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
            QLineEdit, QTreeView, QPushButton, QLabel, QHeaderView, QToolBar, QSpinBox,
            QFileDialog, QMessageBox, QAbstractItemView, QSplitter, QStyle,
            QTableWidget, QTableWidgetItem, QCheckBox, QMenu, QDialog, QFormLayout,
            QDialogButtonBox, QSystemTrayIcon, QKeySequenceEdit, QComboBox,
            QListWidget, QInputDialog, QStackedWidget,
        )
    except ModuleNotFoundError as exc:
        if ensure_pyside6_runtime_ready():
            from PySide6.QtCore import (
                Qt, QAbstractItemModel, QModelIndex, QSortFilterProxyModel,
                QTimer, QSize, QAbstractNativeEventFilter, QRect, QEvent, QObject,
                Signal, Slot, QItemSelectionModel, QMetaObject, QThread,
            )
            from PySide6.QtGui import QIcon, QAction, QKeySequence, QPainter, QPen
            from PySide6.QtWidgets import (
                QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                QLineEdit, QTreeView, QPushButton, QLabel, QHeaderView, QToolBar, QSpinBox,
                QFileDialog, QMessageBox, QAbstractItemView, QSplitter, QStyle,
                QTableWidget, QTableWidgetItem, QCheckBox, QMenu, QDialog, QFormLayout,
                QDialogButtonBox, QSystemTrayIcon, QKeySequenceEdit, QComboBox,
                QListWidget, QInputDialog, QStackedWidget,
            )
        else:
            message = _format_missing_pyside6_message(_PYSIDE6_BOOTSTRAP_ERROR)
            raise ModuleNotFoundError(message) from exc
else:
    import types
    _dummy_class = type("_Dummy", (), {})
    Qt = types.SimpleNamespace(
        DisplayRole=0,
        ScrollBarAsNeeded=0,
        NoItemFlags=0,
        ItemIsEnabled=1,
        ItemIsSelectable=2,
        QueuedConnection=0,
    )
    QAbstractItemModel = QModelIndex = QSortFilterProxyModel = QTimer = QSize = QAbstractNativeEventFilter = QRect = QEvent = QItemSelectionModel = _dummy_class
    QIcon = QAction = QKeySequence = QPainter = QPen = QObject = QThread = _dummy_class
    QApplication = QMainWindow = QWidget = QVBoxLayout = QHBoxLayout = QGroupBox = QLineEdit = QTreeView = QPushButton = QLabel = QHeaderView = QToolBar = QSpinBox = QFileDialog = QMessageBox = QAbstractItemView = QSplitter = QStyle = QTableWidget = QTableWidgetItem = QCheckBox = QMenu = QDialog = QFormLayout = QDialogButtonBox = QSystemTrayIcon = QKeySequenceEdit = QComboBox = QListWidget = QInputDialog = QStackedWidget = _dummy_class
    QAbstractItemView.ScrollPerPixel = 0
    QAbstractItemView.SelectRows = 0
    QAbstractItemView.NoEditTriggers = 0
    ctypes = types.SimpleNamespace(Structure=object, c_size_t=int)
    wintypes = types.SimpleNamespace(
        DWORD=int, BOOL=int, HANDLE=int, HWND=int, INT=int, UINT=int, LONG=int,
        LPARAM=int, WPARAM=int
    )
    winreg = types.SimpleNamespace()

    class _DummyMeta:
        @staticmethod
        def invokeMethod(obj, func, connection=None):
            if callable(func):
                func()
            elif isinstance(func, str) and hasattr(obj, func):
                getattr(obj, func)()
            return True

    class _DummySignal:
        def __init__(self, *_, **__):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, *args, **kwargs):
            for slot in list(self._slots):
                slot(*args, **kwargs)

    Signal = _DummySignal
    QMetaObject = _DummyMeta

    def Slot(*types, **kwargs):  # type: ignore[override]
        def decorator(func):
            return func

        return decorator

# --- Win32: APIs ------------------------------------------------------------
if os.name == "nt":
    OpenProcess = ctypes.windll.kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    CloseHandle = ctypes.windll.kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    EmptyWorkingSet = ctypes.windll.psapi.EmptyWorkingSet
    EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    EmptyWorkingSet.restype = wintypes.BOOL

    # advanced strategy: reset working set sizes (admin more likely needed)
    SetProcessWorkingSetSizeEx = ctypes.windll.kernel32.SetProcessWorkingSetSizeEx
    SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    SetProcessWorkingSetSizeEx.restype = wintypes.BOOL
    QUOTA_FLAGS = 0x0001 | 0x0002

    RegisterHotKey = ctypes.windll.user32.RegisterHotKey
    RegisterHotKey.argtypes = [wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT]
    RegisterHotKey.restype = wintypes.BOOL

    UnregisterHotKey = ctypes.windll.user32.UnregisterHotKey
    UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.INT]
    UnregisterHotKey.restype = wintypes.BOOL

    GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow
    GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
    GetWindowRect = ctypes.windll.user32.GetWindowRect
    GetSystemMetrics = ctypes.windll.user32.GetSystemMetrics
    SM_CXSCREEN = 0
    SM_CYSCREEN = 1

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_VM_READ = 0x0010
    ACCESS = PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA | PROCESS_VM_READ

    RUN_KEY = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"
    RUN_VALUE = "RamGobbler"
else:
    def _no_win32(*a, **k):
        return 0
    OpenProcess = CloseHandle = EmptyWorkingSet = SetProcessWorkingSetSizeEx = _no_win32
    RegisterHotKey = UnregisterHotKey = _no_win32
    GetForegroundWindow = GetWindowThreadProcessId = GetWindowRect = GetSystemMetrics = _no_win32
    SM_CXSCREEN = SM_CYSCREEN = 0
    PROCESS_QUERY_INFORMATION = PROCESS_SET_QUOTA = PROCESS_VM_READ = ACCESS = 0
    RUN_KEY = ""
    RUN_VALUE = "RamGobbler"

WM_HOTKEY = 0x0312
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

HOTKEY_ID = 1

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]

# --- System process detection ----------------------------------------------
CRITICAL_NAMES = {
    "System", "Registry", "MemCompression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "RuntimeBroker.exe", "dwm.exe",
}

def is_system_exe(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        windir = os.environ.get("WINDIR", r"C:\\Windows")
        windir = os.path.normcase(windir)
        p = os.path.normcase(path)
        return p.startswith(windir)
    except Exception:
        return False

def is_system_process(p: psutil.Process) -> bool:
    try:
        name = (p.info.get("name") if hasattr(p, 'info') else None) or p.name() or ""
        if name in CRITICAL_NAMES:
            return True
        if is_system_exe((p.info.get("exe") if hasattr(p, 'info') else None) or p.exe()):
            return True
        try:
            user = p.username()
            if user.upper().endswith("\\SYSTEM") or user.upper().startswith("NT AUTHORITY\\"):
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return True  # if we can't inspect, treat as system to be safe
    return False

# --- Trimming helpers -------------------------------------------------------
def empty_working_set(pid: int) -> bool:
    h = OpenProcess(ACCESS, False, pid)
    if not h:
        return False
    try:
        return bool(EmptyWorkingSet(h))
    finally:
        CloseHandle(h)

def reset_working_set(pid: int) -> bool:
    # Set min/max to -1 (SIZE_T(-1)) to ask the OS to reset; flags allow permission adjustments
    h = OpenProcess(ACCESS, False, pid)
    if not h:
        return False
    try:
        return bool(SetProcessWorkingSetSizeEx(h, ctypes.c_size_t(-1 & 0xFFFFFFFFFFFFFFFF), ctypes.c_size_t(-1 & 0xFFFFFFFFFFFFFFFF), QUOTA_FLAGS))
    finally:
        CloseHandle(h)

def rss_mb(pid: int) -> Optional[float]:
    try:
        p = psutil.Process(pid)
        return p.memory_info().rss / (1024 * 1024)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

# optional elevated helper ----------------------------------------------------
HELPER_PIPE = r"\\.\pipe\RamGobblerHelper"

def helper_trim(pid: int, strategy: str) -> Tuple[bool, float]:
    """Request an elevated trim via helper service."""
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
        req = json.dumps({"action": "trim", "pid": int(pid), "strategy": strategy}).encode("utf-8")
        win32file.WriteFile(handle, req)
        resp = win32file.ReadFile(handle, 65536)[1]
        data = json.loads(resp.decode("utf-8"))
        return bool(data.get("ok")), float(data.get("delta_mb") or 0.0)
    except Exception:
        return False, 0.0
    finally:
        win32file.CloseHandle(handle)

# --- Foreground / fullscreen / battery -------------------------------------
def get_foreground_pid() -> Optional[int]:
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
    try:
        hwnd = GetForegroundWindow()
        if not hwnd:
            return False
        rect = RECT()
        if not GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        screen_w = GetSystemMetrics(SM_CXSCREEN)
        screen_h = GetSystemMetrics(SM_CYSCREEN)
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        # consider fullscreen if >= 95% of screen in both dimensions
        return w >= 0.95 * screen_w and h >= 0.95 * screen_h
    except Exception:
        return False

def on_battery() -> Tuple[bool, bool]:
    try:
        batt = psutil.sensors_battery()
        if not batt:
            return False, False
        return (batt.power_plugged is False), (batt.secsleft is not None and batt.secsleft < 1800)
    except Exception:
        return False, False

# --- Startup helpers --------------------------------------------------------
def _pythonw_path() -> str:
    if getattr(sys, 'frozen', False):
        return sys.executable
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pythonw = exe[:-10] + "pythonw.exe"
        if os.path.exists(pythonw):
            return pythonw
    return exe

def _startup_command() -> str:
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    script = os.path.abspath(sys.argv[0])
    return f'"{_pythonw_path()}" "{script}"'

def is_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False

def set_startup_enabled(enabled: bool) -> Tuple[bool, str]:
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _startup_command())
                return True, "Enabled Start with Windows"
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
                return True, "Disabled Start with Windows"
    except OSError as e:
        return False, f"Registry error: {e}"

# --- Data model -------------------------------------------------------------
@dataclass
class Group:
    """User-defined bucket used to color-code or organize targets."""

    id: str
    name: str
    color: Optional[str] = None


@dataclass
class ProcessEntry:
    """Description of a single running process displayed in the UI.

    The class intentionally mirrors the minimal data we need when rendering
    child rows inside the process tree.  Keeping it tiny avoids pulling in
    heavyweight ``psutil.Process`` objects once the snapshot has been taken.
    """

    pid: int
    name: str
    exe: str
    rss_mb: float


@dataclass
class ProcessGroup:
    """Aggregate of related processes shown as one expandable row.

    ``ProcessTreeModel`` treats every instance as the parent entry and renders
    ``members`` underneath it.  ``pids`` stores every process identifier that
    belongs to the group so trimming can act on the entire bundle.

    ``root_pid`` tracks the primary process for the bundle while ``root_pids``
    stores every top-level process that acts as an entry point for the group.
    Task Manager style apps often spawn multiple independent roots (for
    example two ``notepad.exe`` windows); bundling them allows the user to trim
    or add a target for the entire application with one action.
    """

    id: str
    name: str
    exe: str
    root_pid: int
    total_rss_mb: float
    pids: List[int]
    members: List[ProcessEntry]
    root_pids: List[int] = field(default_factory=list)

    @property
    def primary_pid(self) -> Optional[int]:
        """Return the most representative PID for selection convenience."""
        return self.pids[0] if self.pids else None


@dataclass
class _BundleAccumulator:
    """Internal helper that aggregates processes into a display bundle."""

    key: Tuple[str, str]
    name: str
    exe: str
    root_pids: Set[int] = field(default_factory=set)
    roots: Dict[int, ProcessEntry] = field(default_factory=dict)
    children: List[ProcessEntry] = field(default_factory=list)
    pid_set: Set[int] = field(default_factory=set)
    total_rss_mb: float = 0.0

    def add_root(self, entry: ProcessEntry) -> None:
        """Register a top-level process for the bundle."""

        if entry.pid in self.pid_set:
            # Another worker may have already added the root when iterating the
            # snapshot.  Avoid double counting.
            return
        self.root_pids.add(entry.pid)
        self.roots[entry.pid] = entry
        self.pid_set.add(entry.pid)
        self.total_rss_mb += entry.rss_mb

    def add_member(self, entry: ProcessEntry) -> None:
        """Append a non-root process to the bundle."""

        if entry.pid in self.pid_set:
            return
        self.children.append(entry)
        self.pid_set.add(entry.pid)
        self.total_rss_mb += entry.rss_mb

    def ensure_metadata(self, name: str, exe: str) -> None:
        """Upgrade the display metadata when richer details become available."""

        if name and (not self.name or self.name.startswith("PID ")):
            self.name = name
        if exe and not self.exe:
            self.exe = exe

    def build_group(self) -> ProcessGroup:
        """Produce a :class:`ProcessGroup` ready for the Qt model."""

        def _sort_key(entry: ProcessEntry) -> Tuple[float, str, int]:
            return (-entry.rss_mb, entry.name.lower(), entry.pid)

        root_entries = sorted(self.roots.values(), key=_sort_key)
        child_entries = sorted(self.children, key=_sort_key)
        members = root_entries + child_entries
        ordered_root_pids = [entry.pid for entry in root_entries]
        other_pids = [entry.pid for entry in child_entries]
        pids = ordered_root_pids + other_pids
        # ``root_pid`` is primarily used for restoring selection; pick the
        # first root when available, otherwise fall back to the first member.
        if ordered_root_pids:
            root_pid = ordered_root_pids[0]
        elif pids:
            root_pid = pids[0]
        else:
            root_pid = 0
        return ProcessGroup(
            id=f"{self.key[0]}:{self.key[1]}",
            name=self.name,
            exe=self.exe,
            root_pid=root_pid,
            total_rss_mb=self.total_rss_mb,
            pids=pids,
            members=members,
            root_pids=ordered_root_pids,
        )


def _bundle_identity(name: str, exe: str, root_pid: int) -> Tuple[Tuple[str, str], str, str]:
    """Return a stable bundle key plus display metadata.

    Parameters
    ----------
    name:
        Process name reported by ``psutil``.
    exe:
        Absolute path to the executable when available.
    root_pid:
        PID used as a fallback identifier when neither ``name`` nor ``exe``
        provide enough information.  Using the PID keeps system tasks separate
        while still allowing user applications to be merged by executable.
    """

    clean_name = (name or "").strip()
    exe_path = exe or ""
    display_name = clean_name or (os.path.basename(exe_path) if exe_path else f"PID {root_pid}")
    exe_norm = os.path.normcase(exe_path) if exe_path else ""

    if clean_name in CRITICAL_NAMES or is_system_exe(exe_path):
        key = ("pid", str(root_pid))
    elif exe_norm:
        key = ("exe", exe_norm)
    elif display_name:
        key = ("name", display_name.casefold())
    else:
        key = ("pid", str(root_pid))

    return key, display_name, exe_path


@dataclass
class Target:
    """Configuration for an individual process to trim."""

    key: str               # identifier to match (name/path/cmdline/parent)
    match_by: str          # 'name', 'path', 'cmdline', or 'parent'
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    group_id: str = ""
    active: bool = False
    interval_sec: int = DEFAULT_INTERVAL
    last_trim_ts: float = 0.0
    last_result: str = ""
    autostart: bool = False
    last_delta_mb: float = 0.0
    session_reclaimed_mb: float = 0.0
    day_reclaimed_mb: float = 0.0
    day_key: str = ""
    history: List[float] = field(default_factory=list)
    # smarter trimming
    adaptive: bool = True
    cpu_skip_threshold: int = 60  # skip trim if any pid > this CPU%
    background_only: bool = False # skip if any pid is foreground
    pause_if_fullscreen: bool = True  # skip when a fullscreen window is active
    strategy: str = "standard"   # 'standard' or 'reset'
    last_total_rss_mb: float = 0.0
    notify_threshold_mb: float = 0.0
    cooldown_s: int = 0
    reset_cap_per_min: int = 3
    # scheduling / gating
    schedule: Optional[dict] = None  # {days:[0..6], start:"HH:MM", end:"HH:MM", idle_only: bool}
    last_skip_reason: str = ""
    last_trim_reason: str = ""
    _events: Deque[dict] = field(default_factory=lambda: deque(maxlen=200), repr=False)
    next_allowed_ts: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_events", None)
        d.pop("next_allowed_ts", None)
        d["history"] = self.history[-40:]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Target":
        return Target(
            key=d.get("key", ""),
            match_by=d.get("match_by", "name"),
            uid=d.get("uid") or uuid.uuid4().hex[:10],
            active=bool(d.get("active", False)),
            interval_sec=int(d.get("interval_sec", DEFAULT_INTERVAL)),
            last_trim_ts=float(d.get("last_trim_ts", 0.0)),
            last_result=d.get("last_result", ""),
            autostart=bool(d.get("autostart", False)),
            last_delta_mb=float(d.get("last_delta_mb", 0.0)),
            session_reclaimed_mb=float(d.get("session_reclaimed_mb", 0.0)),
            day_reclaimed_mb=float(d.get("day_reclaimed_mb", 0.0)),
            day_key=d.get("day_key", ""),
            history=list(d.get("history", []))[:40],
            adaptive=bool(d.get("adaptive", True)),
            cpu_skip_threshold=int(d.get("cpu_skip_threshold", 60)),
            background_only=bool(d.get("background_only", False)),
            pause_if_fullscreen=bool(d.get("pause_if_fullscreen", True)),
            strategy=d.get("strategy", "standard"),
            last_total_rss_mb=float(d.get("last_total_rss_mb", 0.0)),
            notify_threshold_mb=float(d.get("notify_threshold_mb", 0.0)),
            cooldown_s=int(d.get("cooldown_s", 0)),
            reset_cap_per_min=int(d.get("reset_cap_per_min", 3)),
            schedule=d.get("schedule"),
            last_skip_reason=d.get("last_skip_reason", ""),
            last_trim_reason=d.get("last_trim_reason", ""),
            group_id=d.get("group_id", ""),
        )

# --- Config -----------------------------------------------------------------
class Config:
    """Application configuration persisted on disk."""

    def __init__(self, path: str):
        self.path = path
        self.targets: List[Target] = []
        self.groups: List[Group] = []
        self.theme: str = "dark"
        self.tray_mode: bool = True
        self.startup_enabled: bool = False
        self.hotkey: str = ""
        self.trim_on_wake: bool = False
        self.show_system: bool = False
        self.protect_system: bool = True
        # global smarter options
        self.pressure_enabled: bool = True
        self.min_free_percent: int = 25  # trim only if available% <= this
        self.gaming_mode: bool = True    # pause trims while fullscreen
        self.battery_mode: str = "slow"  # off | slow | pause
        self.battery_slow_factor: float = 2.0
        # scheduling / gating
        self.maintenance_window: dict = {"start": "00:00", "end": "00:00", "aggressive": False}
        self.idle_seconds: int = 0
        self.jitter_pct: int = 10
        self.metered: bool = False
        self.daily_recap: dict = {"enabled": False, "time": "00:00"}
        self.diag_level: int = 0
        self.version: int = 5

    def load(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            self.startup_enabled = is_startup_enabled()
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.targets = [Target.from_dict(t) for t in raw.get("targets", [])]
            self.groups = [Group(id=g.get("id", ""), name=g.get("name", ""), color=g.get("color"))
                            for g in raw.get("groups", [])]
            self.theme = raw.get("theme", self.theme)
            self.tray_mode = bool(raw.get("tray_mode", self.tray_mode))
            self.startup_enabled = bool(raw.get("startup_enabled", is_startup_enabled()))
            self.hotkey = raw.get("hotkey", "")
            self.trim_on_wake = bool(raw.get("trim_on_wake", False))
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
            self.version = int(raw.get("version", 1))
            if self.version < 5:
                self.version = 5
        except Exception:
            self.targets = []

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            if os.path.exists(self.path):
                os.makedirs(BACKUP_DIR, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(self.path, os.path.join(BACKUP_DIR, f"config-{ts}.json"))
        except Exception:
            pass
        raw = {
            "targets": [t.to_dict() for t in self.targets],
            "groups": [asdict(g) for g in self.groups],
            "theme": self.theme,
            "tray_mode": self.tray_mode,
            "startup_enabled": self.startup_enabled,
            "hotkey": self.hotkey,
            "trim_on_wake": self.trim_on_wake,
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
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)

# --- Process utilities ------------------------------------------------------
def list_processes(include_system: bool) -> List[ProcessGroup]:
    """Return running processes aggregated into logical groups.

    Parameters
    ----------
    include_system:
        When ``False`` we hide protected operating system tasks so the user
        cannot accidentally trim them.  ``True`` shows every process.

    The implementation is intentionally verbose and heavily commented so that
    readers can follow the grouping logic step by step:

    1. Capture a light-weight snapshot of every running process we care about.
    2. Walk the parent chain for each PID to determine its top-most ancestor.
    3. Aggregate ``ProcessEntry`` instances by that ancestor and build sorted
       ``ProcessGroup`` objects ready for the tree view.
    """

    # Snapshot structure keyed by PID.  Each entry stores the lightweight
    # ``ProcessEntry`` along with the parent PID so we can walk upwards later.
    snapshot: Dict[int, Dict[str, object]] = {}

    # ``psutil.process_iter`` gives us a fast iterator with most data already
    # cached.  ``ad_value=None`` avoids surprises when an attribute is missing.
    for proc in psutil.process_iter(attrs=["pid", "name", "exe", "memory_info", "ppid"], ad_value=None):
        try:
            # Optionally skip protected system processes when the user has
            # requested a cleaner view.
            if not include_system and is_system_process(proc):
                continue

            info = proc.info

            # ``memory_info`` may be ``None`` or raise if the process exits in
            # the meantime; guard each access carefully.
            mem_info = info.get("memory_info")
            rss_mb = 0.0
            if mem_info and hasattr(mem_info, "rss"):
                rss_mb = float(mem_info.rss) / (1024 * 1024)

            exe = info.get("exe") or ""
            name = info.get("name") or ""

            # Fall back to the executable name (or even the PID) so every row
            # has a friendly label.
            if not name and exe:
                name = os.path.basename(exe) or exe
            if not name:
                name = f"PID {proc.pid}"

            entry = ProcessEntry(pid=proc.pid, name=name, exe=exe, rss_mb=rss_mb)
            snapshot[proc.pid] = {
                "entry": entry,
                "ppid": int(info.get("ppid") or 0),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Processes vanish or deny access all the time; ignore and move on.
            continue

    if not snapshot:
        return []

    # ``root_cache`` lets us remember the computed ancestor for each PID so
    # repeated lookups are constant time.
    root_cache: Dict[int, int] = {}

    def find_root(pid: int) -> int:
        """Return the highest ancestor present in ``snapshot`` for ``pid``."""

        if pid in root_cache:
            return root_cache[pid]

        chain: List[int] = []
        current = pid
        while True:
            data = snapshot.get(current)
            if not data:
                break
            parent = int(data.get("ppid") or 0)
            # Stop when the parent is missing, points to itself, or would cause
            # a loop (which can happen with short-lived processes).
            if not parent or parent == current or parent in chain or parent not in snapshot:
                break
            chain.append(current)
            current = parent

        # Cache the discovered ancestor for the entire chain to avoid walking
        # the same path multiple times.
        root_cache[pid] = current
        for child in chain:
            root_cache[child] = current
        return current

    root_map: Dict[int, int] = {pid: find_root(pid) for pid in snapshot.keys()}

    bundles: Dict[Tuple[str, str], _BundleAccumulator] = {}

    for pid, data in snapshot.items():
        entry: ProcessEntry = data["entry"]  # type: ignore[assignment]
        root_pid = root_map.get(pid, pid)
        root_data = snapshot.get(root_pid, data)
        root_entry: ProcessEntry = root_data["entry"]  # type: ignore[assignment]
        key, display_name, exe = _bundle_identity(root_entry.name, root_entry.exe, root_pid)

        bundle = bundles.get(key)
        if not bundle:
            bundle = _BundleAccumulator(key=key, name=display_name, exe=exe)
            bundles[key] = bundle
        else:
            bundle.ensure_metadata(display_name, exe)

        if pid == root_pid:
            bundle.add_root(entry)
        else:
            bundle.add_member(entry)
            # Helpers sometimes carry richer names/paths than their root; keep
            # the nicest metadata we encounter.
            bundle.ensure_metadata(entry.name, entry.exe)

    result: List[ProcessGroup] = []
    for bundle in bundles.values():
        group = bundle.build_group()
        if group.members:
            result.append(group)

    # Present the heaviest groups at the top to mimic Task Manager.
    result.sort(key=lambda g: (-g.total_rss_mb, g.name.lower()))
    return result

def match_pids(target: Target, include_system: bool = True) -> List[int]:
    """Return PIDs matching ``target`` including entire process groups.

    The logic mirrors :func:`list_processes` so a user selecting a grouped row
    trims the exact same bundle.  We intentionally keep the snapshot
    light-weight and compute parent chains with caching so repeated calls stay
    fast even on machines with hundreds of processes.  ``include_system``
    mirrors the process list toggle so previews and manual trims avoid pulling
    in hidden Windows tasks when the UI filters them out.
    """

    snapshot: Dict[int, Dict[str, object]] = {}

    for proc in psutil.process_iter(attrs=["pid", "name", "exe", "cmdline", "ppid"], ad_value=None):
        try:
            if not include_system and is_system_process(proc):
                continue
            info = proc.info
            snapshot[proc.pid] = {
                "name": info.get("name") or "",
                "exe": info.get("exe") or "",
                "cmdline": info.get("cmdline") or [],
                "ppid": int(info.get("ppid") or 0),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not snapshot:
        return []

    # Populate parent names where possible for parent-based matching.  We avoid
    # calling into ``psutil`` unless the direct parent is missing from the
    # snapshot because fetching from disk is comparatively slow.
    for pid, data in snapshot.items():
        parent_pid = int(data.get("ppid") or 0)
        parent_name = ""
        if parent_pid and parent_pid in snapshot:
            parent_name = str(snapshot[parent_pid].get("name") or "")
        elif parent_pid:
            try:
                parent_name = psutil.Process(parent_pid).name()
            except Exception:
                parent_name = ""
        data["parent_name"] = parent_name

    root_cache: Dict[int, int] = {}

    def find_root(pid: int) -> int:
        """Return the top-most ancestor of ``pid`` captured in ``snapshot``."""

        if pid in root_cache:
            return root_cache[pid]

        chain: List[int] = []
        current = pid
        while True:
            data = snapshot.get(current)
            if not data:
                break
            parent = int(data.get("ppid") or 0)
            if not parent or parent == current or parent in chain or parent not in snapshot:
                break
            chain.append(current)
            current = parent

        root_cache[pid] = current
        for child in chain:
            root_cache[child] = current
        return current

    root_map: Dict[int, int] = {pid: find_root(pid) for pid in snapshot.keys()}

    bundle_members: Dict[Tuple[str, str], Set[int]] = defaultdict(set)
    pid_to_bundle: Dict[int, Tuple[str, str]] = {}
    root_keys: Dict[int, Tuple[str, str]] = {}

    for pid, data in snapshot.items():
        root_pid = root_map.get(pid, pid)
        if root_pid not in root_keys:
            root_info = snapshot.get(root_pid, data)
            key, _, _ = _bundle_identity(
                str(root_info.get("name", "")),
                str(root_info.get("exe", "")),
                root_pid,
            )
            root_keys[root_pid] = key
        bundle_key = root_keys[root_pid]
        bundle_members[bundle_key].add(pid)
        pid_to_bundle[pid] = bundle_key

    matched_bundles: Set[Tuple[str, str]] = set()

    for pid, data in snapshot.items():
        info = {
            "name": data.get("name", ""),
            "exe": data.get("exe", ""),
            "cmdline": data.get("cmdline", []),
            "parent": data.get("parent_name", ""),
        }
        try:
            if matches(info, target):
                key = pid_to_bundle.get(pid)
                if key:
                    matched_bundles.add(key)
        except Exception:
            continue

    if not matched_bundles:
        return []

    result: Set[int] = set()
    for key in matched_bundles:
        result.update(bundle_members.get(key, set()))

    return sorted(result)


def estimate_reclaim(history: List[float]) -> Tuple[float, float, float]:
    """Estimate reclaimable memory from past trim history.

    Returns a tuple of (min, median, p95) over the last up to 10 samples."""
    if not history:
        return (0.0, 0.0, 0.0)
    sample = history[-10:]
    sample_sorted = sorted(sample)
    med = statistics.median(sample_sorted)
    idx = int((len(sample_sorted) - 1) * 0.95)
    p95 = sample_sorted[idx]
    return (float(sample_sorted[0]), float(med), float(p95))

# --- Background trimmer thread ---------------------------------------------
class Gobbler(threading.Thread):
    def __init__(self, config: Config, metrics: Metrics, on_result, on_delta):
        super().__init__(daemon=True)
        self.config = config
        self.metrics = metrics
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.on_result = on_result
        self.on_delta = on_delta
        self.helper_warned = False
        self._last_flush = time.time()
        self._reset_times: Dict[str, Deque[float]] = {}

    def stop(self):
        self._stop.set()
        try:
            self.metrics.save()
        except Exception:
            pass

    def _update_day_rollover(self, t: Target):
        today = datetime.now().strftime("%Y%m%d")
        if t.day_key != today:
            t.day_key = today
            t.day_reclaimed_mb = 0.0

    def _allow_trimming_now(self) -> Tuple[bool, str]:
        # pressure
        if self.config.pressure_enabled:
            try:
                vm = psutil.virtual_memory()
                avail_pct = 100.0 * vm.available / max(1, vm.total)
                if avail_pct > self.config.min_free_percent:
                    return False, "Pressure off"
            except Exception:
                pass
        # gaming / fullscreen
        if self.config.gaming_mode and is_fullscreen_active():
            return False, "Paused (Fullscreen)"
        # battery
        if self.config.battery_mode in ("pause", "slow"):
            onbatt, low = on_battery()
            if self.config.battery_mode == "pause" and onbatt:
                return False, "Paused (Battery)"
        return True, ""

    def _check_cooldown_and_cap(self, t: Target, now_ts: float) -> Tuple[bool, str]:
        """Determine if target ``t`` may be trimmed at ``now_ts``.

        Returns a tuple ``(allowed, reason)`` where ``reason`` is empty when
        trimming is permitted.  The function enforces per-target cooldowns and
        limits the number of ``reset``-strategy trims per minute.
        """

        if t.cooldown_s > 0 and now_ts < t.next_allowed_ts:
            return False, "cooldown"

        if t.strategy == "reset" and t.reset_cap_per_min > 0:
            dq = self._reset_times.setdefault(t.key, deque())
            while dq and now_ts - dq[0] > 60:
                dq.popleft()
            if len(dq) >= t.reset_cap_per_min:
                return False, "reset_capped"

        return True, ""

    def _post_trim(self, t: Target, delta_mb: float, now_ts: float):
        if t.cooldown_s > 0 and delta_mb >= 100:
            t.next_allowed_ts = now_ts + t.cooldown_s
        if t.strategy == "reset":
            dq = self._reset_times.setdefault(t.key, deque())
            dq.append(now_ts)

    def _record_event(self, idx: int, event: dict):
        if self.config.diag_level > 0:
            with self._lock:
                try:
                    self.config.targets[idx]._events.append(event)
                except IndexError:
                    pass

    def run(self):
        while not self._stop.is_set():
            now_ts = time.time()
            now_dt = datetime.now()
            with self._lock:
                targets_snapshot = list(self.config.targets)
            global_ok, global_reason = self._allow_trimming_now()
            batt_on, _ = on_battery()
            batt_factor = self.config.battery_slow_factor if (self.config.battery_mode == "slow" and batt_on) else 1.0
            fg_pid = get_foreground_pid()

            for idx, t in enumerate(targets_snapshot):
                if not t.active:
                    continue
                base_interval = jittered_interval(t.interval_sec, t.key, self.config.jitter_pct)
                eff_interval = max(3, int(base_interval * batt_factor))
                in_maint = window_active(self.config.maintenance_window, now_dt)
                trim_reason = "maintenance" if in_maint else "timer"
                if in_maint and self.config.maintenance_window.get("aggressive"):
                    eff_interval = min(eff_interval, 8)
                if now_ts - t.last_trim_ts < eff_interval:
                    continue
                ok_cool, reason_cool = self._check_cooldown_and_cap(t, now_ts)
                if not ok_cool:
                    self.on_result(idx, reason_cool)
                    with self._lock:
                        try:
                            self.config.targets[idx].last_skip_reason = reason_cool
                        except IndexError:
                            pass
                    self._record_event(idx, {"ts": now_ts, "type": "skip", "reason": reason_cool})
                    continue
                if not global_ok:
                    self.on_result(idx, global_reason)
                    with self._lock:
                        try:
                            self.config.targets[idx].last_skip_reason = global_reason
                        except IndexError:
                            pass
                    continue
                if not schedule_allows_now(t.schedule, now_dt):
                    reason = "Blocked (Schedule)"
                    self.on_result(idx, reason)
                    with self._lock:
                        try:
                            self.config.targets[idx].last_skip_reason = reason
                        except IndexError:
                            pass
                    continue
                if self.config.idle_seconds > 0 and t.schedule and t.schedule.get("idle_only"):
                    if user_idle_seconds() < self.config.idle_seconds:
                        reason = "Idle unmet"
                        self.on_result(idx, reason)
                        with self._lock:
                            try:
                                self.config.targets[idx].last_skip_reason = reason
                            except IndexError:
                                pass
                        continue
                if self.config.metered:
                    reason = "Metered"
                    self.on_result(idx, reason)
                    with self._lock:
                        try:
                            self.config.targets[idx].last_skip_reason = reason
                        except IndexError:
                            pass
                    continue
                if t.background_only and fg_pid is not None:
                    try:
                        if fg_pid in match_pids(t):
                            continue
                    except Exception:
                        pass
                if t.pause_if_fullscreen and is_fullscreen_active():
                    reason = "Paused (Fullscreen)"
                    self.on_result(idx, reason)
                    with self._lock:
                        try:
                            self.config.targets[idx].last_skip_reason = reason
                        except IndexError:
                            pass
                    continue

                pids = match_pids(t)
                trimmed = 0
                total_delta = 0.0
                total_rss = 0.0
                helper_needed = False
                for pid in pids:
                    try:
                        if self.config.protect_system and is_system_process(psutil.Process(pid)):
                            continue
                    except Exception:
                        continue
                    # CPU guard
                    try:
                        if t.cpu_skip_threshold > 0:
                            cpu = psutil.Process(pid).cpu_percent(interval=0.0)
                            if cpu == 0.0:
                                cpu = psutil.Process(pid).cpu_percent(interval=0.0)
                            if cpu > t.cpu_skip_threshold:
                                continue
                    except Exception:
                        pass

                    before = rss_mb(pid)
                    ok = False
                    if t.strategy == "reset":
                        ok = reset_working_set(pid) or empty_working_set(pid)
                    else:
                        ok = empty_working_set(pid)
                    after = rss_mb(pid)
                    delta = 0.0
                    if ok:
                        trimmed += 1
                        if before is not None and after is not None:
                            delta = max(0.0, before - after)
                    else:
                        ok2, delta = helper_trim(pid, t.strategy)
                        if ok2:
                            trimmed += 1
                            after = rss_mb(pid)
                        else:
                            helper_needed = True
                            if before is not None and after is not None:
                                delta = max(0.0, before - after)
                    total_delta += delta
                    if after is not None:
                        total_rss += after

                event_ts = time.time()
                real_t = None
                with self._lock:
                    try:
                        real_t = self.config.targets[idx]
                        real_t.last_trim_ts = event_ts
                        real_t.last_delta_mb = total_delta
                        real_t.last_result = f"Trimmed {trimmed} procs, -{total_delta:.1f} MB"
                        real_t.last_trim_reason = trim_reason
                        real_t.last_skip_reason = ""
                        self._update_day_rollover(real_t)
                        real_t.session_reclaimed_mb += total_delta
                        real_t.day_reclaimed_mb += total_delta
                        if total_delta > 0:
                            real_t.history.append(total_delta)
                            real_t.history = real_t.history[-40:]
                        if self.config.diag_level > 0:
                            real_t._events.append(
                                {"ts": event_ts, "delta_mb": total_delta, "reason": trim_reason, "ok": trimmed > 0}
                            )
                        # Adaptive interval
                        if real_t.adaptive:
                            growth = max(0.0, total_rss - real_t.last_total_rss_mb)
                            if total_delta >= 50.0 or growth >= 100.0:
                                real_t.interval_sec = max(5, real_t.interval_sec - 2)
                            elif total_delta < 5.0:
                                real_t.interval_sec = min(60, real_t.interval_sec + 2)
                            real_t.last_total_rss_mb = total_rss
                    except IndexError:
                        real_t = None
                if real_t is not None:
                    self._post_trim(real_t, total_delta, event_ts)
                msg = f"Trimmed {trimmed} procs, -{total_delta:.1f} MB"
                if helper_needed and not self.helper_warned:
                    msg += " (helper not running)"
                    self.helper_warned = True
                self.metrics.add_event(event_ts, total_delta, trim_reason, t.strategy)
                self.on_result(idx, msg)
                if total_delta > 0:
                    self.on_delta(total_delta)
            if time.time() - self._last_flush > 10:
                try:
                    self.metrics.save()
                except Exception:
                    pass
                self._last_flush = time.time()
            time.sleep(1)

    # mutation helpers
    def set_active(self, index: int, active: bool):
        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index].active = active
                self.config.save()

    def set_interval(self, index: int, seconds: int):
        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index].interval_sec = max(3, int(seconds))
                self.config.save()

    def set_autostart(self, index: int, value: bool):
        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index].autostart = bool(value)
                self.config.save()

    def update_target(self, index: int, new_t: Target):
        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index] = new_t
                self.config.save()

    def remove_target(self, index: int):
        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets.pop(index)
                self.config.save()


class _ProcessSnapshotWorker(QObject):
    """Background helper that resolves process groups on a dedicated thread."""

    snapshot_ready = Signal(int, object)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._pending: Optional[Tuple[int, bool]] = None
        self._active = False
        self._stopped = False

    @Slot()
    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._pending = None
            self._active = False

    @Slot(int, bool)
    def queue_refresh(self, token: int, include_system: bool) -> None:
        with self._lock:
            if self._stopped:
                return
            # Only the most recent request matters; drop anything older.
            self._pending = (token, include_system)
            if self._active:
                return
            self._active = True
        self._drain_requests()

    def _drain_requests(self) -> None:
        while True:
            with self._lock:
                if self._stopped:
                    self._active = False
                    return
                request = self._pending
                self._pending = None
            if request is None:
                with self._lock:
                    if self._pending is None or self._stopped:
                        self._active = False
                        return
                    continue
            token, include_system = request
            try:
                groups = list_processes(include_system)
            except Exception:
                _log_exception("Process snapshot collection failed")
                groups = []
            self.snapshot_ready.emit(token, groups)


class ProcessTreeModel(QAbstractItemModel):
    """Hierarchical model backing the running-process tree view.

    A dedicated :class:`QThread` hosts :class:`_ProcessSnapshotWorker`, which
    serialises refresh requests, captures grouped process snapshots, and emits
    the results back to the GUI thread.  If a Qt worker cannot be started we
    gracefully fall back to synchronous refreshes so the process tree still
    repopulates during tests or on minimal environments.
    """

    HEADERS = ["Name", "PID(s)", "Memory (MB)", "Path"]
    _request_refresh = Signal(int, bool)
    _request_shutdown = Signal()

    def __init__(self, include_system: bool):
        super().__init__()
        self.include_system = include_system
        self._groups: List[ProcessGroup] = []
        self._group_index: Dict[str, int] = {}
        self._pid_index: Dict[int, Tuple[str, int]] = {}
        self._lock = threading.Lock()
        self._next_token = 1
        self._latest_token: Optional[int] = None
        self._pending_callbacks: Dict[int, List[Callable[[], None]]] = defaultdict(list)
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_ProcessSnapshotWorker] = None
        self._worker_enabled = False
        self._cleanup_registered = False
        self._shutting_down = False

        self._worker_enabled = self._create_worker()
        self.refresh()

    def __del__(self):
        try:
            self._shutdown_worker()
        except Exception:
            pass

    @staticmethod
    def _safe_connect(signal, slot) -> None:
        try:
            signal.connect(slot, Qt.QueuedConnection)  # type: ignore[arg-type]
        except TypeError:
            signal.connect(slot)

    def _create_worker(self) -> bool:
        if self._shutting_down:
            return False
        try:
            thread = QThread(self)
        except Exception:
            return False

        worker = _ProcessSnapshotWorker()
        try:
            worker.moveToThread(thread)
        except Exception:
            _log_exception("Moving process snapshot worker to QThread failed")
            try:
                if hasattr(thread, "deleteLater"):
                    thread.deleteLater()
            except Exception:
                pass
            return False

        try:
            self._safe_connect(worker.snapshot_ready, self._on_worker_snapshot)
            self._safe_connect(self._request_refresh, worker.queue_refresh)
            self._safe_connect(self._request_shutdown, worker.stop)
        except Exception:
            _log_exception("Connecting process snapshot worker signals failed")
            try:
                if hasattr(thread, "deleteLater"):
                    thread.deleteLater()
            except Exception:
                pass
            return False

        if hasattr(thread, "finished") and hasattr(thread.finished, "connect"):
            try:
                thread.finished.connect(self._on_worker_finished)
                thread.finished.connect(worker.deleteLater)
            except Exception:
                pass

        try:
            thread.start()
        except Exception:
            _log_exception("Starting process snapshot thread failed")
            try:
                if hasattr(thread, "deleteLater"):
                    thread.deleteLater()
            except Exception:
                pass
            return False

        self._worker_thread = thread
        self._worker = worker
        self._register_cleanup_hook()
        return True

    def _thread_running(self) -> bool:
        thread = self._worker_thread
        if thread is None:
            return False
        is_running = getattr(thread, "isRunning", None)
        if callable(is_running):
            try:
                return bool(is_running())
            except Exception:
                return False
        return False

    def _register_cleanup_hook(self) -> None:
        if self._cleanup_registered:
            return
        try:
            if 'QApplication' in globals() and hasattr(QApplication, "instance"):
                app = QApplication.instance()
            else:
                app = None
        except Exception:
            app = None
        if app and hasattr(app, "aboutToQuit"):
            try:
                app.aboutToQuit.connect(self._shutdown_worker)
                self._cleanup_registered = True
            except Exception:
                pass

    def _shutdown_worker(self) -> None:
        if self._shutting_down:
            return

        self._shutting_down = True
        thread = self._worker_thread
        worker = self._worker
        self._worker_thread = None
        self._worker = None
        self._worker_enabled = False

        if self._cleanup_registered:
            try:
                if 'QApplication' in globals() and hasattr(QApplication, "instance"):
                    app = QApplication.instance()
                else:
                    app = None
                if app and hasattr(app, "aboutToQuit"):
                    app.aboutToQuit.disconnect(self._shutdown_worker)
            except Exception:
                pass
            self._cleanup_registered = False

        if worker is not None:
            try:
                self._request_shutdown.emit()
            except Exception:
                try:
                    worker.stop()
                except Exception:
                    pass

        if thread is not None:
            quit_method = getattr(thread, "quit", None)
            if callable(quit_method):
                try:
                    quit_method()
                except Exception:
                    pass
            wait_method = getattr(thread, "wait", None)
            if callable(wait_method):
                try:
                    wait_method(1500)
                except Exception:
                    pass
            try:
                if hasattr(thread, "deleteLater"):
                    thread.deleteLater()
            except Exception:
                pass

        with self._lock:
            self._pending_callbacks.clear()
            self._latest_token = None

    @Slot()
    def _on_worker_finished(self) -> None:
        self._worker_thread = None
        self._worker = None
        self._worker_enabled = False

    @Slot(int, object)
    def _on_worker_snapshot(self, token: int, groups: object) -> None:
        if self._shutting_down:
            return
        try:
            typed_groups: List[ProcessGroup] = list(groups) if groups is not None else []
        except Exception:
            typed_groups = []
        self._apply_snapshot(token, typed_groups)

    def _run_sync_refresh(self, token: int, include_system: bool) -> None:
        try:
            groups = list_processes(include_system)
        except Exception:
            _log_exception("Process snapshot refresh failed on main thread")
            groups = []
        self._apply_snapshot(token, groups)

    def set_include_system(self, val: bool, done=None):
        self.include_system = val
        self.refresh(done)

    def refresh(self, done=None):
        if self._shutting_down:
            if callable(done):
                try:
                    done()
                except Exception:
                    _log_exception("Process refresh completion callback failed")
            return

        callback = done if callable(done) else None
        with self._lock:
            token = self._next_token
            self._next_token += 1
            bucket = self._pending_callbacks[token]
            if callback:
                bucket.append(callback)
            previous = self._latest_token
            self._latest_token = token
            if previous and previous != token:
                prev_callbacks = self._pending_callbacks.pop(previous, [])
                if prev_callbacks:
                    bucket.extend(prev_callbacks)
            include_system = self.include_system

        if self._worker_enabled and self._thread_running():
            try:
                self._request_refresh.emit(token, include_system)
                return
            except Exception:
                _log_exception("Queueing process refresh on worker failed")
                self._worker_enabled = False

        if not self._worker_enabled:
            self._worker_enabled = self._create_worker()
            if self._worker_enabled and self._thread_running():
                try:
                    self._request_refresh.emit(token, include_system)
                    return
                except Exception:
                    _log_exception("Queueing process refresh on worker failed")
                    self._worker_enabled = False

        self._run_sync_refresh(token, include_system)

    def _apply_snapshot(self, token: int, groups: List[ProcessGroup]) -> None:
        callbacks: List[Callable[[], None]] = []
        with self._lock:
            if self._shutting_down:
                self._pending_callbacks.pop(token, None)
                return
            latest = self._latest_token
            if latest is None:
                self._pending_callbacks.pop(token, None)
                return
            if token != latest:
                stale_callbacks = self._pending_callbacks.pop(token, [])
                if stale_callbacks:
                    self._pending_callbacks[latest].extend(stale_callbacks)
                return
            callbacks = self._pending_callbacks.pop(token, [])
            self._latest_token = None
        self._replace_groups(groups)
        for cb in callbacks:
            try:
                cb()
            except Exception:
                _log_exception("Process refresh callback failed")

    def _replace_groups(self, groups: List[ProcessGroup]):
        """Swap ``self._groups`` and rebuild lookup tables."""
        if groups != self._groups:
            if hasattr(self, "beginResetModel"):
                self.beginResetModel()
            self._groups = groups
            self._rebuild_indexes()
            if hasattr(self, "endResetModel"):
                self.endResetModel()
        else:
            self._groups = groups
            self._rebuild_indexes()

    def _rebuild_indexes(self):
        self._group_index.clear()
        self._pid_index.clear()
        for row, group in enumerate(self._groups):
            self._group_index[group.id] = row
            for child_row, entry in enumerate(group.members):
                self._pid_index[entry.pid] = (group.id, child_row)

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return len(self._groups)
        ptr = parent.internalPointer()
        if not ptr:
            return 0
        if isinstance(ptr, tuple) and ptr and ptr[0] == "group":
            group_idx = ptr[1]
            if 0 <= group_idx < len(self._groups):
                return len(self._groups[group_idx].members)
        return 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def index(self, row, column, parent=QModelIndex()):
        if row < 0 or column < 0 or column >= len(self.HEADERS):
            return QModelIndex()
        if not parent.isValid():
            if 0 <= row < len(self._groups):
                return self.createIndex(row, column, ("group", row))
            return QModelIndex()
        ptr = parent.internalPointer()
        if not ptr:
            return QModelIndex()
        if isinstance(ptr, tuple) and ptr and ptr[0] == "group":
            group_idx = ptr[1]
            if 0 <= group_idx < len(self._groups):
                members = self._groups[group_idx].members
                if 0 <= row < len(members):
                    return self.createIndex(row, column, ("child", group_idx, row))
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        ptr = index.internalPointer()
        if not ptr:
            return QModelIndex()
        if isinstance(ptr, tuple) and ptr:
            if ptr[0] == "child":
                group_idx = ptr[1]
                if 0 <= group_idx < len(self._groups):
                    return self.createIndex(group_idx, 0, ("group", group_idx))
        return QModelIndex()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self.HEADERS):
                return self.HEADERS[section]
        return section + 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        ptr = index.internalPointer()
        if not ptr or not isinstance(ptr, tuple):
            return None
        column = index.column()
        if ptr[0] == "group":
            group_idx = ptr[1]
            if not (0 <= group_idx < len(self._groups)):
                return None
            group = self._groups[group_idx]
            if role == Qt.DisplayRole:
                if column == 0:
                    label = group.name
                    count = len(group.pids)
                    if count > 1:
                        label = f"{label} ({count})"
                    return label
                if column == 1:
                    pid_text = ""
                    if group.pids:
                        primary = group.primary_pid
                        if primary is not None:
                            extra = len(group.pids) - 1
                            if extra > 0:
                                pid_text = f"{primary} (+{extra})"
                            else:
                                pid_text = str(primary)
                        else:
                            pid_text = ", ".join(str(p) for p in group.pids)
                    return pid_text
                if column == 2:
                    return f"{group.total_rss_mb:,.1f}"
                if column == 3:
                    return group.exe
        elif ptr[0] == "child":
            group_idx, child_row = ptr[1], ptr[2]
            if not (0 <= group_idx < len(self._groups)):
                return None
            members = self._groups[group_idx].members
            if not (0 <= child_row < len(members)):
                return None
            entry = members[child_row]
            if role == Qt.DisplayRole:
                if column == 0:
                    return entry.name
                if column == 1:
                    return str(entry.pid)
                if column == 2:
                    return f"{entry.rss_mb:,.1f}"
                if column == 3:
                    return entry.exe
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    # --- Helpers ---------------------------------------------------------
    def group_from_index(self, index: QModelIndex) -> Optional[ProcessGroup]:
        if not index.isValid():
            return None
        ptr = index.internalPointer()
        if not ptr or not isinstance(ptr, tuple):
            return None
        if ptr[0] == "group":
            group_idx = ptr[1]
        elif ptr[0] == "child":
            group_idx = ptr[1]
        else:
            return None
        if 0 <= group_idx < len(self._groups):
            return self._groups[group_idx]
        return None

    def entry_from_index(self, index: QModelIndex) -> Optional[ProcessEntry]:
        if not index.isValid():
            return None
        ptr = index.internalPointer()
        if not ptr or not isinstance(ptr, tuple):
            return None
        if ptr[0] != "child":
            return None
        group_idx, child_row = ptr[1], ptr[2]
        if 0 <= group_idx < len(self._groups):
            members = self._groups[group_idx].members
            if 0 <= child_row < len(members):
                return members[child_row]
        return None

    def is_group_index(self, index: QModelIndex) -> bool:
        if not index.isValid():
            return False
        ptr = index.internalPointer()
        return bool(ptr and isinstance(ptr, tuple) and ptr[0] == "group")

    def index_for_group(self, group_id: str) -> QModelIndex:
        row = self._group_index.get(group_id)
        if row is None:
            return QModelIndex()
        return self.index(row, 0, QModelIndex())

    def index_for_pid(self, group_id: str, pid: int) -> QModelIndex:
        row = self._group_index.get(group_id)
        if row is None:
            return QModelIndex()
        group_index = self.index(row, 0, QModelIndex())
        if not group_index.isValid():
            return QModelIndex()
        group = self._groups[row]
        for child_row, entry in enumerate(group.members):
            if entry.pid == pid:
                return self.index(child_row, 0, group_index)
        return QModelIndex()

    def group_for_pid(self, pid: int) -> Optional[ProcessGroup]:
        mapping = self._pid_index.get(pid)
        if not mapping:
            return None
        group_id, _ = mapping
        row = self._group_index.get(group_id)
        if row is None:
            return None
        if 0 <= row < len(self._groups):
            return self._groups[row]
        return None
class ProcessFilterProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self._needle = ""
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)

    def setFilterFixedString(self, pattern: str):
        self._needle = (pattern or "").casefold()
        super().setFilterFixedString(pattern)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._needle:
            return True
        model = self.sourceModel()
        if model is None:
            return True
        if self._row_matches(model, source_row, source_parent):
            return True
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return False
        for row in range(model.rowCount(index)):
            if self.filterAcceptsRow(row, index):
                return True
        return False

    def _row_matches(self, model: QAbstractItemModel, source_row: int, source_parent: QModelIndex) -> bool:
        columns = model.columnCount(source_parent)
        for col in range(columns):
            idx = model.index(source_row, col, source_parent)
            if not idx.isValid():
                continue
            text = model.data(idx, Qt.DisplayRole)
            if text is None:
                continue
            if self._needle in str(text).casefold():
                return True
        return False

# --- Sparkline widget -------------------------------------------------------
class Sparkline(QWidget):
    def __init__(self, values: List[float], parent=None):
        super().__init__(parent)
        self.values = values[-40:]
        self.setMinimumHeight(24)

    def set_values(self, values: List[float]):
        self.values = values[-40:]
        self.update()

    def paintEvent(self, event):
        qp = QPainter(self)
        rect = self.rect().adjusted(2, 2, -2, -2)
        qp.setRenderHint(QPainter.Antialiasing)
        pen = QPen()
        qp.setPen(pen)
        vals = self.values or [0]
        vmax = max(vals) or 1.0
        n = len(vals)
        if n == 1:
            y = rect.bottom() - (vals[0]/vmax) * rect.height()
            qp.drawLine(rect.left(), y, rect.right(), y)
            return
        step = rect.width() / max(1, n-1)
        pts = []
        for i, v in enumerate(vals):
            x = rect.left() + i * step
            y = rect.bottom() - (v / vmax) * rect.height()
            pts.append((x, y))
        for i in range(1, len(pts)):
            qp.drawLine(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1])

# --- Groups sidebar ---------------------------------------------------------
class GroupsSidebar(QWidget):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._emit_selection)
        layout.addWidget(self.list)
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("Add")
        self.btn_rename = QPushButton("Rename")
        self.btn_delete = QPushButton("Delete")
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        for b in (self.btn_add, self.btn_rename, self.btn_delete, self.btn_start, self.btn_stop):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)
        self.btn_add.clicked.connect(self._add)
        self.btn_rename.clicked.connect(self._rename)
        self.btn_delete.clicked.connect(self._delete)
        self.btn_start.clicked.connect(lambda: parent._gobble_all())
        self.btn_stop.clicked.connect(lambda: parent._stop_all())
        self.refresh()

    def refresh(self):
        self.list.clear()
        self.list.addItem("All")
        for g in self.config.groups:
            self.list.addItem(g.name)
        self.list.setCurrentRow(0)

    def current_group_id(self) -> str:
        row = self.list.currentRow()
        if row <= 0:
            return ""
        try:
            return self.config.groups[row - 1].id
        except IndexError:
            return ""

    def _emit_selection(self, _):
        gid = self.current_group_id()
        if hasattr(self.parent(), "_on_group_selected"):
            self.parent()._on_group_selected(gid)

    def _add(self):
        name, ok = QInputDialog.getText(self, "Add Group", "Group name:")
        if not ok or not name:
            return
        gid = uuid.uuid4().hex[:8]
        self.config.groups.append(Group(id=gid, name=name))
        self.config.save()
        self.refresh()
        if hasattr(self.parent(), "targets_table"):
            self.parent().targets_table.refresh(force=True)

    def _rename(self):
        row = self.list.currentRow()
        if row <= 0:
            return
        grp = self.config.groups[row - 1]
        name, ok = QInputDialog.getText(self, "Rename Group", "Group name:", text=grp.name)
        if not ok or not name:
            return
        grp.name = name
        self.config.save()
        self.refresh()
        if hasattr(self.parent(), "targets_table"):
            self.parent().targets_table.refresh(force=True)

    def _delete(self):
        row = self.list.currentRow()
        if row <= 0:
            return
        grp = self.config.groups.pop(row - 1)
        for t in self.config.targets:
            if t.group_id == grp.id:
                t.group_id = ""
        self.config.save()
        self.refresh()
        if hasattr(self.parent(), "targets_table"):
            self.parent().targets_table.refresh(force=True)

# --- Target rules dialog ----------------------------------------------------
class RulesDialog(QDialog):
    def __init__(self, t: Target, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Target Rules")
        self.setMinimumWidth(420)
        self._orig = t
        form = QFormLayout(self)

        self.sb_cpu = QSpinBox()
        self.sb_cpu.setRange(0, 100)
        self.sb_cpu.setValue(int(t.cpu_skip_threshold))
        self.cb_adaptive = QCheckBox("Adaptive interval")
        self.cb_adaptive.setChecked(bool(t.adaptive))
        self.cb_bg = QCheckBox("Trim only when target is in background")
        self.cb_bg.setChecked(bool(t.background_only))
        self.cb_full = QCheckBox("Pause while any app is fullscreen")
        self.cb_full.setChecked(bool(t.pause_if_fullscreen))

        self.cb_strategy = QComboBox()
        self.cb_strategy.addItems(["standard", "reset"]) 
        self.cb_strategy.setCurrentText(t.strategy)

        form.addRow("CPU skip threshold (%):", self.sb_cpu)
        form.addRow("Interval strategy:", self.cb_adaptive)
        form.addRow("Background-only:", self.cb_bg)
        form.addRow("Pause if fullscreen:", self.cb_full)
        form.addRow("Trim method:", self.cb_strategy)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def apply(self) -> Target:
        t = self._orig
        t.cpu_skip_threshold = int(self.sb_cpu.value())
        t.adaptive = bool(self.cb_adaptive.isChecked())
        t.background_only = bool(self.cb_bg.isChecked())
        t.pause_if_fullscreen = bool(self.cb_full.isChecked())
        t.strategy = self.cb_strategy.currentText()
        return t

# --- UI interaction helpers -------------------------------------------------
class _InteractionWatcher(QObject):
    """Tracks whether a widget (and its children) are actively being used."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False

    def attach(self, widget):
        if widget is None:
            return
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            self.active = True
        elif etype == QEvent.MouseButtonRelease:
            self._schedule_clear()
        elif etype == QEvent.Wheel:
            self.active = True
            self._schedule_clear()
        elif etype == QEvent.Leave:
            self._schedule_clear()
        return False

    def _schedule_clear(self):
        if hasattr(QTimer, "singleShot"):
            QTimer.singleShot(120, self._clear_if_idle)
        else:
            self._clear_if_idle()

    def _clear_if_idle(self):
        try:
            if not QApplication.mouseButtons():
                self.active = False
        except Exception:
            self.active = False


# --- Targets table ----------------------------------------------------------
class TargetsTable(QTableWidget):
    HEADERS = [
        "Group", "Target", "Match", "Auto-start", "Status",
        "Last Δ (MB)", "Totals MB (Sess/Day)", "History", "Interval (s)", "Actions"
    ]

    def __init__(self, config: Config, gobbler: Gobbler):
        super().__init__(0, len(self.HEADERS))
        self.config = config
        self.gobbler = gobbler
        self.group_filter = ""
        self.uid_to_row: Dict[str, int] = {}
        self._row_targets: List[Optional[str]] = []
        self._interaction = _InteractionWatcher(self)
        self._tracked_widgets: set[int] = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._flush_pending_refresh)
        self._pending_refresh_force = False
        self.setHorizontalHeaderLabels(self.HEADERS)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self._register_interaction(self)
        if hasattr(self, "viewport"):
            self._register_interaction(self.viewport())
        bar = self.verticalScrollBar() if hasattr(self, "verticalScrollBar") else None
        if bar:
            self._register_interaction(bar)
        hbar = self.horizontalScrollBar() if hasattr(self, "horizontalScrollBar") else None
        if hbar:
            self._register_interaction(hbar)
        self.refresh(force=True)
        # Size columns once the table has initial data. Doing this on every
        # refresh caused the scroll position to reset, so defer it to a queued
        # event after the first population.
        QTimer.singleShot(0, self.resizeColumnsToContents)

    def set_group_filter(self, gid: str):
        self.group_filter = gid
        self.refresh(force=True)

    def _group_name(self, gid: str) -> str:
        if not gid:
            return "Ungrouped"
        for g in self.config.groups:
            if g.id == gid:
                return g.name
        return gid

    def refresh(self, force: bool = False):
        if self.is_busy():
            self._pending_refresh_force = self._pending_refresh_force or force
            self._refresh_timer.start(200)
            return
        if self._refresh_timer.isActive():
            self._refresh_timer.stop()
        self._pending_refresh_force = False

        scroll_val, selected_uids = self._capture_state()

        self.setUpdatesEnabled(False)
        try:
            self.setRowCount(0)
            self.uid_to_row.clear()
            self._row_targets = []
            if self.group_filter:
                for idx, t in enumerate(self.config.targets):
                    if t.group_id == self.group_filter:
                        self._add_row(idx, t)
            else:
                groups: Dict[str, List[Tuple[int, Target]]] = {}
                for idx, t in enumerate(self.config.targets):
                    groups.setdefault(t.group_id or "", []).append((idx, t))
                order = [""] + [g.id for g in self.config.groups if g.id]
                for gid in order:
                    members = groups.get(gid, [])
                    if not members:
                        continue
                    total = sum(t.session_reclaimed_mb for _, t in members)
                    histories = [t.history for _, t in members]
                    agg_hist = [sum(vals) for vals in zip_longest(*histories, fillvalue=0.0)]
                    self._add_group_header(self._group_name(gid), total, agg_hist)
                    for idx, t in members:
                        self._add_row(idx, t)
        finally:
            self.setUpdatesEnabled(True)

        self._restore_state(scroll_val, selected_uids)

    def _flush_pending_refresh(self):
        force = self._pending_refresh_force
        self._pending_refresh_force = False
        self.refresh(force=force)

    def _register_interaction(self, widget):
        if widget is None:
            return
        wid = id(widget)
        if wid in self._tracked_widgets:
            return
        self._tracked_widgets.add(wid)
        self._interaction.attach(widget)

    def _capture_state(self) -> Tuple[int, List[str]]:
        bar = self.verticalScrollBar() if hasattr(self, "verticalScrollBar") else None
        scroll_val = bar.value() if bar else 0
        selected: List[str] = []
        if self.selectionModel():
            for index in self.selectionModel().selectedRows():
                row = index.row()
                if 0 <= row < len(self._row_targets):
                    uid = self._row_targets[row]
                    if uid:
                        selected.append(uid)
        return scroll_val, selected

    def _restore_state(self, scroll_val: int, selected_uids: List[str]):
        bar = self.verticalScrollBar() if hasattr(self, "verticalScrollBar") else None

        def apply_scroll():
            if not bar:
                return
            bar.blockSignals(True)
            try:
                bar.setValue(max(bar.minimum(), min(scroll_val, bar.maximum())))
            finally:
                bar.blockSignals(False)

        apply_scroll()
        if self.selectionModel():
            self.selectionModel().clearSelection()
            for uid in selected_uids:
                row = self.uid_to_row.get(uid)
                if row is not None:
                    self.selectRow(row)

        if bar and hasattr(QTimer, "singleShot"):
            captured = list(selected_uids)
            QTimer.singleShot(0, lambda b=bar, v=scroll_val, u=captured: self._finalize_restore(b, v, u))
            QTimer.singleShot(120, lambda b=bar, v=scroll_val, u=captured: self._finalize_restore(b, v, u))

    def _finalize_restore(self, bar, scroll_val: int, selected_uids: List[str]):
        if bar:
            bar.blockSignals(True)
            try:
                bar.setValue(max(bar.minimum(), min(scroll_val, bar.maximum())))
            finally:
                bar.blockSignals(False)
        if not self.selectionModel():
            return
        for uid in selected_uids:
            row = self.uid_to_row.get(uid)
            if row is not None:
                self.selectRow(row)

    def is_busy(self) -> bool:
        bar = self.verticalScrollBar() if hasattr(self, "verticalScrollBar") else None
        return bool(bar and bar.isSliderDown()) or self._interaction.active

    def _add_group_header(self, name: str, total: float, history: List[float]):
        row = self.rowCount()
        self.insertRow(row)
        self._row_targets.append(None)
        item = QTableWidgetItem(name)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setFlags(Qt.ItemIsEnabled)
        self.setItem(row, 0, item)
        total_item = QTableWidgetItem(f"{total:.1f}")
        total_item.setFlags(Qt.ItemIsEnabled)
        self.setItem(row, 6, total_item)
        spark = Sparkline(history)
        self.setCellWidget(row, 7, spark)

    def _add_row(self, idx: int, t: Target):
        row = self.rowCount()
        self.insertRow(row)
        uid = getattr(t, "uid", None) or uuid.uuid4().hex[:10]
        t.uid = uid
        self.uid_to_row[uid] = row
        self._row_targets.append(uid)

        group_item = QTableWidgetItem(self._group_name(t.group_id))
        target_item = QTableWidgetItem(t.key)
        match_item = QTableWidgetItem(t.match_by)
        status_text = ("Gobbling" if t.active else "Idle")
        if t.last_result:
            status_text += f" — {t.last_result}"
        status_item = QTableWidgetItem(status_text)

        autostart_cb = QCheckBox()
        autostart_cb.setChecked(t.autostart)
        autostart_cb.stateChanged.connect(lambda _=None, i=idx, cb=autostart_cb: self._on_autostart(i, cb.isChecked()))

        last_delta_item = QTableWidgetItem(f"{t.last_delta_mb:.1f}")
        totals_item = QTableWidgetItem(f"{t.session_reclaimed_mb:.1f} / {t.day_reclaimed_mb:.1f}")

        spark = Sparkline(t.history)

        interval_widget = QSpinBox()
        interval_widget.setRange(3, 600)
        interval_widget.setValue(int(t.interval_sec))
        interval_widget.setToolTip("How often to trim memory for this target")
        interval_widget.valueChanged.connect(lambda val, i=idx: self._on_interval(i, val))

        actions_widget = QWidget()
        h = QHBoxLayout(actions_widget)
        h.setContentsMargins(0, 0, 0, 0)
        btn_start = QPushButton("Gobble RAM")
        btn_stop = QPushButton("Stop Gobbling")
        btn_edit = QPushButton("Edit Rules…")
        btn_remove = QPushButton("Remove")
        btn_start.clicked.connect(lambda _=False, i=idx: self._on_start(i))
        btn_stop.clicked.connect(lambda _=False, i=idx: self._on_stop(i))
        btn_edit.clicked.connect(lambda _=False, i=idx: self._on_edit(i))
        btn_remove.clicked.connect(lambda _=False, i=idx: self._on_remove(i))
        for b in (btn_start, btn_stop, btn_edit, btn_remove):
            b.setMinimumHeight(28)
        h.addWidget(btn_start)
        h.addWidget(btn_stop)
        h.addWidget(btn_edit)
        h.addWidget(btn_remove)

        self.setItem(row, 0, group_item)
        self.setItem(row, 1, target_item)
        self.setItem(row, 2, match_item)
        self.setCellWidget(row, 3, autostart_cb)
        self.setItem(row, 4, status_item)
        self.setItem(row, 5, last_delta_item)
        self.setItem(row, 6, totals_item)
        self.setCellWidget(row, 7, spark)
        self.setCellWidget(row, 8, interval_widget)
        self.setCellWidget(row, 9, actions_widget)

        self._register_interaction(autostart_cb)
        self._register_interaction(interval_widget)
        self._register_interaction(actions_widget)
        self._register_interaction(btn_start)
        self._register_interaction(btn_stop)
        self._register_interaction(btn_edit)
        self._register_interaction(btn_remove)

    def _on_start(self, index: int):
        self.gobbler.set_active(index, True)
        self.refresh(force=True)

    def _on_stop(self, index: int):
        self.gobbler.set_active(index, False)
        self.refresh(force=True)

    def _on_edit(self, index: int):
        if 0 <= index < len(self.config.targets):
            t = self.config.targets[index]
            dlg = RulesDialog(t, self)
            if dlg.exec() == QDialog.Accepted:
                new_t = dlg.apply()
                self.gobbler.update_target(index, new_t)
                self.refresh(force=True)

    def _on_remove(self, index: int):
        self.gobbler.remove_target(index)
        self.refresh(force=True)

    def _on_interval(self, index: int, val: int):
        self.gobbler.set_interval(index, val)

    def _on_autostart(self, index: int, value: bool):
        self.gobbler.set_autostart(index, value)

    def update_status(self, index: int, message: str):
        if not (0 <= index < len(self.config.targets)):
            return
        target = self.config.targets[index]
        uid = getattr(target, "uid", None)
        if not uid:
            return
        row = self.uid_to_row.get(uid)
        if row is None:
            return
        status_item = self.item(row, 4)
        if status_item:
            state = "Gobbling" if target.active else "Idle"
            status_item.setText(f"{state} — {message}")
        delta_item = self.item(row, 5)
        if delta_item:
            delta_item.setText(f"{target.last_delta_mb:.1f}")
        totals_item = self.item(row, 6)
        if totals_item:
            totals_item.setText(f"{target.session_reclaimed_mb:.1f} / {target.day_reclaimed_mb:.1f}")
        spark = self.cellWidget(row, 7)
        if isinstance(spark, Sparkline):
            spark.set_values(target.history)

# --- Add Target Dialog ------------------------------------------------------
class AddTargetDialog(QDialog):
    def __init__(self, key: str, match_by: str, include_system: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Target")
        self.setMinimumWidth(520)
        self.include_system = include_system
        form = QFormLayout(self)
        self.le_key = QLineEdit(key)
        self.cmb_match = QComboBox()
        self.cmb_match.addItems(["name", "path", "cmdline", "parent"])
        if match_by in ["name", "path", "cmdline", "parent"]:
            self.cmb_match.setCurrentText(match_by)
        self.sb_interval = QSpinBox()
        self.sb_interval.setRange(3, 600)
        self.sb_interval.setValue(DEFAULT_INTERVAL)
        self.cb_autostart = QCheckBox("Auto-start gobbling for this target")
        self.lbl_preview = QLabel("Matches: 0")
        self.le_key.textChanged.connect(self._update_preview)
        self.cmb_match.currentTextChanged.connect(self._update_preview)
        form.addRow("Target pattern:", self.le_key)
        form.addRow("Match by:", self.cmb_match)
        form.addRow("Preview:", self.lbl_preview)
        form.addRow("Interval (s):", self.sb_interval)
        form.addRow("", self.cb_autostart)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)
        QTimer.singleShot(0, self._update_preview)

    def _update_preview(self):
        key = self.le_key.text().strip()
        match_by = self.cmb_match.currentText()
        if not key:
            self.lbl_preview.setText("Matches: 0")
            return
        t = Target(key=key, match_by=match_by)
        try:
            count = len(match_pids(t, include_system=self.include_system))
        except Exception:
            count = 0
        self.lbl_preview.setText(f"Matches: {count}")

    def get_values(self) -> Tuple[str, str, int, bool]:
        return (
            self.le_key.text().strip(),
            self.cmb_match.currentText(),
            int(self.sb_interval.value()),
            bool(self.cb_autostart.isChecked()),
        )

# --- Settings Dialog (global opts + hotkey & wake) --------------------------
class SettingsDialog(QDialog):
    def __init__(self, cfg: 'Config', parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        form = QFormLayout(self)

        self.kse = QKeySequenceEdit()
        if cfg.hotkey:
            self.kse.setKeySequence(QKeySequence(cfg.hotkey))
        self.cb_wake = QCheckBox("Trim visible targets automatically after waking from sleep")
        self.cb_wake.setChecked(bool(cfg.trim_on_wake))
        self.cb_show_system = QCheckBox("Show system processes (⚠️ not recommended)")
        self.cb_show_system.setChecked(bool(cfg.show_system))
        self.cb_protect_system = QCheckBox("Protect system processes from trimming (recommended)")
        self.cb_protect_system.setChecked(bool(cfg.protect_system))

        self.cb_pressure = QCheckBox("Only auto-trim under memory pressure")
        self.cb_pressure.setChecked(bool(cfg.pressure_enabled))
        self.sb_min_free = QSpinBox(); self.sb_min_free.setRange(5, 90); self.sb_min_free.setValue(int(cfg.min_free_percent))

        self.cb_gaming = QCheckBox("Pause trims while any app is fullscreen")
        self.cb_gaming.setChecked(bool(cfg.gaming_mode))

        self.cmb_batt = QComboBox(); self.cmb_batt.addItems(["off", "slow", "pause"]); self.cmb_batt.setCurrentText(cfg.battery_mode)
        self.sb_batt_factor = QSpinBox(); self.sb_batt_factor.setRange(1, 5); self.sb_batt_factor.setValue(int(cfg.battery_slow_factor))

        form.addRow("Global hotkey:", self.kse)
        form.addRow("", QLabel("(Leave empty for no hotkey)"))
        form.addRow("On wake:", self.cb_wake)
        form.addRow("System visibility:", self.cb_show_system)
        form.addRow("System protection:", self.cb_protect_system)
        form.addRow("Under pressure:", self.cb_pressure)
        form.addRow("Min free RAM %:", self.sb_min_free)
        form.addRow("Gaming mode:", self.cb_gaming)
        form.addRow("Battery mode:", self.cmb_batt)
        form.addRow("Battery slow×:", self.sb_batt_factor)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

        self.cfg = cfg

    def apply(self):
        seq = self.kse.keySequence()
        self.cfg.hotkey = seq.toString(QKeySequence.NativeText)
        self.cfg.trim_on_wake = bool(self.cb_wake.isChecked())
        self.cfg.show_system = bool(self.cb_show_system.isChecked())
        self.cfg.protect_system = bool(self.cb_protect_system.isChecked())
        self.cfg.pressure_enabled = bool(self.cb_pressure.isChecked())
        self.cfg.min_free_percent = int(self.sb_min_free.value())
        self.cfg.gaming_mode = bool(self.cb_gaming.isChecked())
        self.cfg.battery_mode = self.cmb_batt.currentText()
        self.cfg.battery_slow_factor = int(self.sb_batt_factor.value())

# --- Hotkey helpers ---------------------------------------------------------
VK_SPECIAL = {
    "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "UP": 0x26, "DOWN": 0x28,
    "LEFT": 0x25, "RIGHT": 0x27, "SPACE": 0x20, "TAB": 0x09,
    "ESC": 0x1B, "ESCAPE": 0x1B, "BACKSPACE": 0x08, "RETURN": 0x0D,
    "ENTER": 0x0D,
}

def parse_hotkey_string(hotkey: str) -> Tuple[Optional[int], Optional[int]]:
    if not hotkey:
        return None, None
    parts = [p.strip() for p in hotkey.split("+") if p.strip()]
    mods = 0
    vk = None
    for p in parts:
        up = p.upper()
        if up in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif up == "ALT":
            mods |= MOD_ALT
        elif up == "SHIFT":
            mods |= MOD_SHIFT
        elif up in ("WIN", "META", "CMD", "SUPER"):
            mods |= MOD_WIN
        elif up.startswith("F") and up[1:].isdigit():
            n = int(up[1:])
            if 1 <= n <= 24:
                vk = 0x70 + (n - 1)
        elif len(up) == 1 and up.isalnum():
            vk = ord(up)
        else:
            vk = VK_SPECIAL.get(up)
    return mods, vk

class WinEventFilter(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey, on_resume):
        super().__init__()
        self.on_hotkey = on_hotkey
        self.on_resume = on_resume

    def nativeEventFilter(self, eventType, message):
        if eventType != "windows_generic_MSG":
            return False, 0
        msg = MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:
            self.on_hotkey(msg.wParam)
        elif msg.message == WM_POWERBROADCAST:
            wp = int(msg.wParam)
            if wp in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                self.on_resume()
        return False, 0

# --- Dashboard widgets ------------------------------------------------------
class ChartWidget(QWidget):
    def __init__(self, values: List[float], parent=None):
        super().__init__(parent)
        self.values = values
        self.setMinimumHeight(80)

    def set_values(self, values: List[float]):
        self.values = values
        self.update()

    def paintEvent(self, event):
        qp = QPainter(self)
        rect = self.rect().adjusted(2, 2, -2, -2)
        vals = self.values or [0]
        vmax = max(vals) or 1.0
        bar_w = rect.width() / max(len(vals), 1)
        for i, v in enumerate(vals):
            x = rect.left() + int(i * bar_w)
            h = int((v / vmax) * rect.height())
            qp.fillRect(x, rect.bottom() - h, int(bar_w * 0.8), h, Qt.darkCyan)


class Dashboard(QWidget):
    def __init__(self, config: Config, metrics: Metrics, parent=None):
        super().__init__(parent)
        self.config = config
        self.metrics = metrics
        layout = QVBoxLayout(self)

        card_row = QHBoxLayout()
        self.lbl_session = QLabel("0.0")
        self.lbl_day = QLabel("0.0")
        self.lbl_week = QLabel("0.0")
        self.lbl_month = QLabel("0.0")
        for label, widget in [
            ("Session", self.lbl_session),
            ("Day", self.lbl_day),
            ("7-day", self.lbl_week),
            ("30-day", self.lbl_month),
        ]:
            box = QGroupBox(label)
            vb = QVBoxLayout(box)
            vb.addWidget(widget)
            card_row.addWidget(box)
        layout.addLayout(card_row)

        self.top_table = QTableWidget(0, 2)
        self.top_table.setHorizontalHeaderLabels(["Top Saver", "MB"])
        self.top_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.top_table)

        self.eff_table = QTableWidget(0, 2)
        self.eff_table.setHorizontalHeaderLabels(["Strategy", "Avg MB/Trim"])
        self.eff_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.eff_table)

        self.chart = ChartWidget([])
        layout.addWidget(self.chart)

        btn_row = QHBoxLayout()
        self.btn_day_json = QPushButton("Export Day JSON")
        self.btn_day_csv = QPushButton("Export Day CSV")
        self.btn_month_json = QPushButton("Export 30-day JSON")
        self.btn_month_csv = QPushButton("Export 30-day CSV")
        for b in (self.btn_day_json, self.btn_day_csv, self.btn_month_json, self.btn_month_csv):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)
        self.btn_day_json.clicked.connect(lambda: self._export(1, "json"))
        self.btn_day_csv.clicked.connect(lambda: self._export(1, "csv"))
        self.btn_month_json.clicked.connect(lambda: self._export(30, "json"))
        self.btn_month_csv.clicked.connect(lambda: self._export(30, "csv"))

    def _export(self, days: int, fmt: str):
        data = self.metrics.export_json(days) if fmt == "json" else self.metrics.export_csv(days)
        filt = "JSON Files (*.json)" if fmt == "json" else "CSV Files (*.csv)"
        path, _ = QFileDialog.getSaveFileName(self, "Export Metrics", "", filt)
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)

    def refresh(self, session_total: float):
        day = self.metrics._aggregate(1)
        week = self.metrics._aggregate(7)
        month = self.metrics._aggregate(30)
        self.lbl_session.setText(f"{session_total:.1f} MB")
        self.lbl_day.setText(f"{day['reclaimed_mb']:.1f} MB")
        self.lbl_week.setText(f"{week['reclaimed_mb']:.1f} MB")
        self.lbl_month.setText(f"{month['reclaimed_mb']:.1f} MB")

        self.top_table.setRowCount(0)
        top_targets = sorted(self.config.targets, key=lambda t: t.session_reclaimed_mb, reverse=True)[:5]
        groups = group_totals(self.config.targets)
        top_groups = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:5]
        for t in top_targets:
            r = self.top_table.rowCount(); self.top_table.insertRow(r)
            self.top_table.setItem(r, 0, QTableWidgetItem(f"Target: {t.key}"))
            self.top_table.setItem(r, 1, QTableWidgetItem(f"{t.session_reclaimed_mb:.1f}"))
        for gid, val in top_groups:
            name = next((g.name for g in self.config.groups if g.id == gid), "Ungrouped" if not gid else gid)
            r = self.top_table.rowCount(); self.top_table.insertRow(r)
            self.top_table.setItem(r, 0, QTableWidgetItem(f"Group: {name}"))
            self.top_table.setItem(r, 1, QTableWidgetItem(f"{val:.1f}"))

        self.eff_table.setRowCount(0)
        agg = self.metrics._aggregate(30)
        for strat, mb in agg.get("by_strategy", {}).items():
            trims = agg.get("by_strategy_trims", {}).get(strat, 0)
            avg = mb / trims if trims else 0.0
            r = self.eff_table.rowCount(); self.eff_table.insertRow(r)
            self.eff_table.setItem(r, 0, QTableWidgetItem(strat))
            self.eff_table.setItem(r, 1, QTableWidgetItem(f"{avg:.1f}"))

        today = datetime.now().date()
        vals = []
        for i in range(30):
            d = today - timedelta(days=29 - i)
            vals.append(self.metrics.rollups.get(d.strftime("%Y-%m-%d"), {}).get("reclaimed_mb", 0.0))
        self.chart.set_values(vals)

# --- Main Window ------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1240, 780)
        self.setWindowIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))

        self.config = config
        self.session_reclaimed_total = 0.0
        self.metrics = Metrics(METRICS_PATH)
        self.metrics.load()
        self.gobbler = Gobbler(self.config, self.metrics, self._on_trim_result, self._on_delta)
        self.gobbler.start()
        self._hotkey_registered = False
        self.current_group = ""

        # Toolbar
        tb = QToolBar()
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)

        act_refresh = QAction(self.style().standardIcon(QStyle.SP_BrowserReload), "Refresh Processes", self)
        act_refresh.triggered.connect(self._refresh_processes)
        tb.addAction(act_refresh)

        tb.addSeparator()

        self.act_gobble_all = QAction("Gobble All Targets", self)
        self.act_gobble_all.triggered.connect(self._gobble_all)
        tb.addAction(self.act_gobble_all)

        self.act_stop_all = QAction("Stop All", self)
        self.act_stop_all.triggered.connect(self._stop_all)
        tb.addAction(self.act_stop_all)

        self.act_trim_visible = QAction("Trim Visible Targets Now", self)
        self.act_trim_visible.triggered.connect(self._trim_visible_targets)
        tb.addAction(self.act_trim_visible)

        self.act_view = QAction("Dashboard", self)
        self.act_view.triggered.connect(self._toggle_view)
        tb.addAction(self.act_view)

        # Profiles
        m_profiles = QMenu("Profiles", self)
        for label, secs in [("Balanced (15s)",15),("Aggressive (8s)",8),("Gentle (30s)",30)]:
            a = QAction(label, self); a.triggered.connect(lambda _, s=secs: self._apply_profile(s)); m_profiles.addAction(a)
        btn_profiles = QAction("Profiles", self); btn_profiles.setMenu(m_profiles); tb.addAction(btn_profiles)

        tb.addSeparator()

        # Theme / Tray / Startup
        self.act_light = QAction("Light theme", self, checkable=True)
        self.act_light.setChecked(self.config.theme == "light")
        self.act_light.toggled.connect(self._toggle_theme)
        tb.addAction(self.act_light)

        self.act_traymode = QAction("Minimize to tray on close", self, checkable=True)
        self.act_traymode.setChecked(self.config.tray_mode)
        self.act_traymode.toggled.connect(self._toggle_tray_mode)
        tb.addAction(self.act_traymode)

        self.act_startup = QAction("Start with Windows", self, checkable=True)
        self.act_startup.setChecked(self.config.startup_enabled or is_startup_enabled())
        self.act_startup.toggled.connect(self._toggle_startup)
        tb.addAction(self.act_startup)

        # Import / Export
        self.act_export = QAction("Export Settings…", self)
        self.act_export.triggered.connect(self._export_settings)
        tb.addAction(self.act_export)
        self.act_import = QAction("Import Settings…", self)
        self.act_import.triggered.connect(self._import_settings)
        tb.addAction(self.act_import)

        # Settings
        self.act_settings = QAction("Settings…", self)
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

        # Central UI
        splitter = QSplitter()
        left = QWidget(); right = QWidget()
        splitter.addWidget(left); splitter.addWidget(right)
        splitter.setSizes([650, 560])
        self.splitter = splitter

        # Left: Running processes
        lv = QVBoxLayout(left)
        box_left = QGroupBox("Running Processes")
        lv.addWidget(box_left)
        bl = QVBoxLayout(box_left)
        search_row = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search by name or path…")
        self.btn_trim_now = QPushButton("Trim Now"); self.btn_trim_now.setToolTip("Trim the selected process immediately"); self.btn_trim_now.clicked.connect(self._trim_selected)
        self.btn_add_selected = QPushButton("Add Selected as Target"); self.btn_add_selected.clicked.connect(self._add_selected)
        self.cb_show_system = QCheckBox("Show system processes (⚠️)"); self.cb_show_system.setChecked(self.config.show_system); self.cb_show_system.stateChanged.connect(self._toggle_show_system)
        search_row.addWidget(self.search); search_row.addWidget(self.btn_trim_now); search_row.addWidget(self.btn_add_selected); search_row.addWidget(self.cb_show_system)
        bl.addLayout(search_row)

        self.proc_model = ProcessTreeModel(include_system=self.config.show_system)
        self.proxy = ProcessFilterProxy(); self.proxy.setSourceModel(self.proc_model)
        self.table = QTreeView(); self.table.setModel(self.proxy); self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setRootIsDecorated(True); self.table.setItemsExpandable(True); self.table.setExpandsOnDoubleClick(True); self.table.setAllColumnsShowFocus(True)
        self.table.setUniformRowHeights(True); self.table.setAlternatingRowColors(True); self.table.header().setSectionResizeMode(QHeaderView.Stretch)
        bl.addWidget(self.table)
        self._process_interaction = _InteractionWatcher(self.table)
        self._process_interaction.attach(self.table)
        self._process_interaction.attach(self.table.viewport())
        self._process_interaction.attach(self.table.verticalScrollBar())
        self.search.textChanged.connect(self.proxy.setFilterFixedString)

        # Right: Targets with groups sidebar
        rv = QVBoxLayout(right)
        box_right = QGroupBox("Targets")
        rv.addWidget(box_right)
        br = QHBoxLayout(box_right)
        # Instantiate the targets table first so any sidebar signals fired during
        # construction (such as the default "All" selection) can safely access
        # it on the parent window without tripping attribute errors.
        self.targets_table = TargetsTable(self.config, self.gobbler)
        self.groups_sidebar = GroupsSidebar(self.config, self)
        br.addWidget(self.groups_sidebar)
        br.addWidget(self.targets_table, 1)

        self.dashboard = Dashboard(self.config, self.metrics)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.splitter)
        self.stack.addWidget(self.dashboard)
        self.setCentralWidget(self.stack)

        # Status bar: global reclaimed total
        self.lbl_total = QLabel("Session reclaimed: 0.0 MB"); self.statusBar().addPermanentWidget(self.lbl_total)
        self.statusBar().showMessage("Ready")

        # Timers
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_periodic)
        self.refresh_timer.start(5000)
        self._refresh_debounce = QTimer(self)
        self._refresh_debounce.setSingleShot(True)
        self._refresh_debounce.timeout.connect(self._refresh_periodic)

        # System tray
        self._init_tray()

        # Style
        self._apply_styles(self.config.theme)

        # Auto-start targets
        QTimer.singleShot(0, self._autostart_targets)

        # Native event filter (hotkey + wake)
        self._event_filter = WinEventFilter(self._on_hotkey, self._on_resume)
        QApplication.instance().installNativeEventFilter(self._event_filter)

        # Apply hotkey from config
        QTimer.singleShot(0, lambda: self._apply_hotkey(self.config.hotkey))

        self._update_toolbar_scope()

    # --- Theme & styles ---
    def _toggle_theme(self, on: bool):
        self.config.theme = "light" if on else "dark"; self.config.save(); self._apply_styles(self.config.theme)

    def _toggle_tray_mode(self, on: bool):
        self.config.tray_mode = bool(on); self.config.save()

    def _toggle_startup(self, on: bool):
        ok, msg = set_startup_enabled(bool(on))
        if not ok:
            QMessageBox.warning(self, APP_NAME, msg)
            self.act_startup.blockSignals(True); self.act_startup.setChecked(is_startup_enabled()); self.act_startup.blockSignals(False); return
        self.config.startup_enabled = bool(on); self.config.save(); self.statusBar().showMessage(msg, 3000)

    def _toggle_show_system(self, _):
        # Preserve current selection and scroll position when toggling the
        # visibility of system processes.  The underlying model refreshes which
        # previously caused the view to jump back to the top and lose the
        # selected process.
        sel_group, sel_pid, sel_is_child = self._current_selection()
        bar = self.table.verticalScrollBar()
        scroll = bar.value() if bar else 0
        self.config.show_system = bool(self.cb_show_system.isChecked()); self.config.save()

        def done():
            self._select_group(sel_group, sel_pid, sel_is_child)
            self._restore_scrollbar(bar, scroll)

        self.proc_model.set_include_system(self.config.show_system, done)

    def _on_group_selected(self, gid: str):
        self.current_group = gid
        self.targets_table.set_group_filter(gid)
        self._update_toolbar_scope()

    def _group_name(self, gid: str) -> str:
        if not gid:
            return "Ungrouped"
        for g in self.config.groups:
            if g.id == gid:
                return g.name
        return gid

    def _update_toolbar_scope(self):
        if self.current_group:
            name = self._group_name(self.current_group)
            self.act_gobble_all.setText(f"Gobble Group {name}")
            self.act_stop_all.setText(f"Stop Group {name}")
            self.act_trim_visible.setText(f"Trim {name} Targets Now")
        else:
            self.act_gobble_all.setText("Gobble All Targets")
            self.act_stop_all.setText("Stop All")
            self.act_trim_visible.setText("Trim Visible Targets Now")

    def _toggle_view(self):
        if self.stack.currentWidget() == self.dashboard:
            self.stack.setCurrentWidget(self.splitter)
            self.act_view.setText("Dashboard")
        else:
            self.dashboard.refresh(self.session_reclaimed_total)
            self.stack.setCurrentWidget(self.dashboard)
            self.act_view.setText("Targets")

    def _apply_styles(self, theme: str):
        if theme == "light":
            self.setStyleSheet("""
                QMainWindow { background: #f5f7fb; }
                QGroupBox { color: #0b1220; border: 1px solid #d8dce6; border-radius: 12px; margin-top: 10px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0px 4px; }
                QLabel, QLineEdit, QTreeView, QTableView, QTableWidget, QHeaderView::section { color: #0b1220; background: #ffffff; }
                QLineEdit { border: 1px solid #cbd2e1; border-radius: 8px; padding: 8px; }
                QTreeView, QTableView, QTableWidget { gridline-color: #e5e9f2; selection-background-color: #d9e6ff; selection-color: #0b1220; }
                QToolBar { background: #f5f7fb; border-bottom: 1px solid #e5e9f2; }
                QPushButton { background: #ecf1fb; color: #0b1220; border: 1px solid #d8dce6; border-radius: 10px; padding: 6px 12px; }
                QPushButton:hover { background: #e3ebfb; }
                QPushButton:pressed { background: #d6e1fb; }
                QSpinBox { background: #ffffff; color: #0b1220; border: 1px solid #d8dce6; border-radius: 6px; padding: 4px; }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow { background: #0f1115; }
                QGroupBox { color: #eaeef8; border: 1px solid #2a2f3a; border-radius: 12px; margin-top: 10px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0px 4px; }
                QLabel, QLineEdit, QTreeView, QTableView, QTableWidget, QHeaderView::section { color: #d9deea; background: #161a22; }
                QLineEdit { border: 1px solid #2a2f3a; border-radius: 8px; padding: 8px; }
                QTreeView, QTableView, QTableWidget { gridline-color: #2a2f3a; selection-background-color: #2b3a55; selection-color: #ffffff; }
                QToolBar { background: #0f1115; border-bottom: 1px solid #2a2f3a; }
                QPushButton { background: #253042; color: #eaeef8; border: 1px solid #2a2f3a; border-radius: 10px; padding: 6px 12px; }
                QPushButton:hover { background: #2b3a55; }
                QPushButton:pressed { background: #1e2a3b; }
                QSpinBox { background: #161a22; color: #eaeef8; border: 1px solid #2a2f3a; border-radius: 6px; padding: 4px; }
            """)

    # --- System tray ---
    def _init_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        menu = QMenu()
        act_show = QAction("Show/Hide", self); act_show.triggered.connect(self._toggle_window)
        act_trim_visible = QAction("Trim Visible Targets Now", self); act_trim_visible.triggered.connect(self._trim_visible_targets)
        act_gobble_all = QAction("Gobble All Targets", self); act_gobble_all.triggered.connect(self._gobble_all)
        act_stop_all = QAction("Stop All", self); act_stop_all.triggered.connect(self._stop_all)
        act_settings = QAction("Settings…", self); act_settings.triggered.connect(self._open_settings)
        act_quit = QAction("Quit", self); act_quit.triggered.connect(self._quit)
        menu.addAction(act_show); menu.addSeparator(); menu.addAction(act_trim_visible); menu.addAction(act_gobble_all); menu.addAction(act_stop_all); menu.addSeparator(); menu.addAction(act_settings); menu.addSeparator(); menu.addAction(act_quit)
        self.tray.setContextMenu(menu); self.tray.activated.connect(self._tray_activated); self.tray.show()

    def _toggle_window(self):
        if self.isVisible(): self.hide()
        else: self.show(); self.raise_(); self.activateWindow()

    def _quit(self):
        self._unregister_hotkey()
        try: self.gobbler.stop()
        except Exception: pass
        self.tray.hide(); QApplication.instance().quit()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger: self._toggle_window()

    # --- Periodic refresh ---
    def _refresh_periodic(self):
        interaction_busy = getattr(self, "_process_interaction", None)
        if (self.targets_table.is_busy() or (interaction_busy and interaction_busy.active)):
            self._refresh_debounce.start(250)
            return

        bar = self.table.verticalScrollBar()
        sel_group, sel_pid, sel_is_child = self._current_selection()
        proc_scroll = bar.value() if bar else 0

        def done():
            self._select_group(sel_group, sel_pid, sel_is_child)
            self._restore_scrollbar(bar, proc_scroll)

        self.proc_model.refresh(done)

        # Targets table handles preserving its own scroll/selection state.
        self.targets_table.refresh()

        if self.stack.currentWidget() == self.dashboard:
            self.dashboard.refresh(self.session_reclaimed_total)

    def _refresh_processes(self):
        sel_group, sel_pid, sel_is_child = self._current_selection()
        bar = self.table.verticalScrollBar()
        scroll = bar.value() if bar else 0

        def done():
            self._select_group(sel_group, sel_pid, sel_is_child)
            self._restore_scrollbar(bar, scroll)

        self.proc_model.refresh(done)

    def _current_selection(self) -> Tuple[Optional[str], Optional[int], bool]:
        if not self.table.selectionModel():
            return (None, None, False)
        idx = self.table.selectionModel().currentIndex()
        if not idx.isValid():
            return (None, None, False)
        src = self.proxy.mapToSource(idx)
        if not src.isValid():
            return (None, None, False)
        group = self.proc_model.group_from_index(src)
        if not group:
            return (None, None, False)
        is_child = not self.proc_model.is_group_index(src)
        pid = None
        if is_child:
            entry = self.proc_model.entry_from_index(src)
            pid = entry.pid if entry else None
        return (group.id, pid, is_child)

    def _select_group(self, group_id: Optional[str], pid: Optional[int], is_child: bool):
        if not group_id:
            return
        group_index = self.proc_model.index_for_group(group_id)
        if not group_index.isValid():
            return
        proxy_group_index = self.proxy.mapFromSource(group_index)
        if not proxy_group_index.isValid():
            return
        target_index = proxy_group_index
        if is_child and pid is not None:
            child_index = self.proc_model.index_for_pid(group_id, pid)
            if child_index.isValid():
                proxy_child_index = self.proxy.mapFromSource(child_index)
                if proxy_child_index.isValid():
                    self.table.expand(proxy_group_index)
                    target_index = proxy_child_index
        self.table.setCurrentIndex(target_index)

    def _selected_group_entry(self) -> Tuple[Optional[ProcessGroup], Optional[ProcessEntry]]:
        if not self.table.selectionModel():
            return (None, None)
        idx = self.table.selectionModel().currentIndex()
        if not idx.isValid():
            return (None, None)
        src = self.proxy.mapToSource(idx)
        if not src.isValid():
            return (None, None)
        group = self.proc_model.group_from_index(src)
        entry = self.proc_model.entry_from_index(src)
        return group, entry

    def _restore_scrollbar(self, bar, value: int):
        if bar is None:
            return

        def apply():
            bar.blockSignals(True)
            try:
                bar.setValue(max(bar.minimum(), min(value, bar.maximum())))
            finally:
                bar.blockSignals(False)

        apply()
        if hasattr(QTimer, "singleShot"):
            QTimer.singleShot(0, apply)
            QTimer.singleShot(120, apply)

    def _gobble_all(self):
        for i, t in enumerate(self.config.targets):
            if not self.current_group or t.group_id == self.current_group:
                self.gobbler.set_active(i, True)
        self.targets_table.refresh(force=True)
        scope = self._group_name(self.current_group) if self.current_group else "all targets"
        self.statusBar().showMessage(f"Gobbling started for {scope}", 3000)

    def _stop_all(self):
        for i, t in enumerate(self.config.targets):
            if not self.current_group or t.group_id == self.current_group:
                self.gobbler.set_active(i, False)
        self.targets_table.refresh(force=True)
        scope = self._group_name(self.current_group) if self.current_group else "all targets"
        self.statusBar().showMessage(f"Gobbling stopped for {scope}", 3000)

    def _apply_profile(self, interval_sec: int):
        for i in range(len(self.config.targets)): self.gobbler.set_interval(i, interval_sec)
        self.targets_table.refresh(force=True); self.statusBar().showMessage(f"Applied profile: interval {interval_sec}s to all targets", 3000)

    def _export_settings(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Settings", "ramgobbler-config.json", "JSON Files (*.json)")
        if not path: return
        try: self.config.save(); shutil.copy2(CONFIG_PATH, path); self.statusBar().showMessage("Settings exported", 3000)
        except Exception as e: QMessageBox.warning(self, APP_NAME, f"Export failed: {e}")

    def _import_settings(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Settings", "", "JSON Files (*.json)")
        if not path: return
        try:
            with open(path, "r", encoding="utf-8") as f: raw = json.load(f)
            if not isinstance(raw, dict) or "targets" not in raw: raise ValueError("Invalid config file")
            self.config.save(); shutil.copy2(path, CONFIG_PATH); self.config.load()
            self.proc_model.set_include_system(self.config.show_system); self.targets_table.refresh(force=True); self._apply_styles(self.config.theme)
            self.act_light.setChecked(self.config.theme == "light"); self.act_startup.setChecked(self.config.startup_enabled or is_startup_enabled()); self.cb_show_system.setChecked(self.config.show_system)
            self._apply_hotkey(self.config.hotkey); self.statusBar().showMessage("Settings imported", 3000)
        except Exception as e: QMessageBox.warning(self, APP_NAME, f"Import failed: {e}")

    def _add_selected(self):
        group, entry = self._selected_group_entry()
        if not group:
            QMessageBox.information(self, APP_NAME, "Select a process group first.")
            return
        if not group.pids:
            QMessageBox.information(self, APP_NAME, "This group has no running processes right now.")
            return
        if self.config.protect_system:
            for pid in group.pids:
                try:
                    if is_system_process(psutil.Process(pid)):
                        QMessageBox.warning(self, APP_NAME, "This group includes a protected system process. It's hidden/protected by default.")
                        return
                except Exception:
                    continue
        exe = group.exe or (entry.exe if entry else "")
        key = exe or group.name
        match_by = "path" if exe else "name"
        dlg = AddTargetDialog(key, match_by, self.config.show_system, self)
        if dlg.exec() == QDialog.Accepted:
            key, match_by, interval, autostart = dlg.get_values()
            if not key: return
            self.config.targets.append(Target(key=key, match_by=match_by, interval_sec=interval, autostart=autostart))
            self.config.save(); self.targets_table.refresh(force=True);
            if autostart: self.gobbler.set_active(len(self.config.targets)-1, True)

    def _trim_selected(self):
        group, _ = self._selected_group_entry()
        if not group:
            QMessageBox.information(self, APP_NAME, "Select a process group first.")
            return
        if not group.pids:
            QMessageBox.information(self, APP_NAME, "This group has no running processes right now.")
            return

        acted = 0
        trimmed_ok = 0
        total_delta = 0.0
        for pid in group.pids:
            try:
                if self.config.protect_system and is_system_process(psutil.Process(pid)):
                    continue
            except Exception:
                continue
            acted += 1
            before = rss_mb(pid)
            ok = empty_working_set(pid)
            after = rss_mb(pid)
            if ok:
                trimmed_ok += 1
            if before is not None and after is not None:
                total_delta += max(0.0, before - after)

        if acted == 0:
            QMessageBox.warning(self, APP_NAME, "Could not trim this group. The processes may be protected or inaccessible.")
            return

        if total_delta > 0:
            self.session_reclaimed_total += total_delta
            self._update_total_label()

        if trimmed_ok > 0:
            self.statusBar().showMessage(
                f"Trimmed {group.name} ({trimmed_ok}/{acted} processes) −{total_delta:.1f} MB",
                3000,
            )
        else:
            QMessageBox.warning(self, APP_NAME, "Could not trim the selected group. Try running as Administrator.")

    def _trim_visible_targets(self):
        total_pids = 0; trimmed_ok = 0; total_delta = 0.0
        for t in self.config.targets:
            if self.current_group and t.group_id != self.current_group:
                continue
            pids = match_pids(t, include_system=self.config.show_system); total_pids += len(pids)
            delta_t = 0.0
            if pids:
                t.last_trim_reason = "manual"
                t.last_skip_reason = ""
            for pid in pids:
                try:
                    if self.config.protect_system and is_system_process(psutil.Process(pid)):
                        continue
                except Exception:
                    continue
                before = rss_mb(pid); ok = empty_working_set(pid); after = rss_mb(pid)
                if ok:
                    trimmed_ok += 1
                if before is not None and after is not None:
                    d = max(0.0, before - after)
                    total_delta += d
                    delta_t += d
            if delta_t > 0:
                t.session_reclaimed_mb += delta_t
                t.day_reclaimed_mb += delta_t
                t.last_delta_mb = delta_t
                t.history.append(delta_t)
                t.history = t.history[-40:]
                ts = time.time()
                t._events.append({"ts": ts, "delta_mb": delta_t, "reason": "manual", "ok": True})
                self.metrics.add_event(ts, delta_t, "manual", t.strategy)
        self.session_reclaimed_total += total_delta
        self._update_total_label()
        scope = self._group_name(self.current_group) if self.current_group else "visible targets"
        msg = f"Trimmed {trimmed_ok}/{total_pids} processes (−{total_delta:.1f} MB) for {scope}"; self.statusBar().showMessage(msg, 4000)
        if self.tray:
            self.tray.showMessage(APP_NAME, msg)
        if self.stack.currentWidget() == self.dashboard:
            self.dashboard.refresh(self.session_reclaimed_total)

    def _update_total_label(self):
        self.lbl_total.setText(f"Session reclaimed: {self.session_reclaimed_total:.1f} MB")

    # --- Settings / Hotkey / Wake ---
    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply(); self.config.save(); self.proc_model.set_include_system(self.config.show_system); self._apply_hotkey(self.config.hotkey); self.statusBar().showMessage("Settings saved", 3000)

    def _apply_hotkey(self, hotkey_str: str):
        self._unregister_hotkey()
        if not hotkey_str: return
        mods, vk = parse_hotkey_string(hotkey_str)
        if not vk: QMessageBox.warning(self, APP_NAME, f"Can't use hotkey '{hotkey_str}'. Try letters/digits or F-keys with modifiers."); return
        hwnd = int(self.winId()); ok = bool(RegisterHotKey(hwnd, HOTKEY_ID, mods, vk))
        if not ok: QMessageBox.warning(self, APP_NAME, f"Couldn't register hotkey '{hotkey_str}'. It may be in use."); return
        self._hotkey_registered = True

    def _unregister_hotkey(self):
        if self._hotkey_registered:
            try: UnregisterHotKey(int(self.winId()), HOTKEY_ID)
            except Exception: pass
            self._hotkey_registered = False

    def _on_hotkey(self, hotkey_id):
        if int(hotkey_id) == HOTKEY_ID: self._trim_visible_targets()

    def _on_resume(self):
        if self.config.trim_on_wake: QTimer.singleShot(1500, self._trim_visible_targets)

    def _on_trim_result(self, index: int, message: str):
        def update(): self.targets_table.update_status(index, message)
        QTimer.singleShot(0, update)

    def _on_delta(self, delta_mb: float):
        if delta_mb > 0:
            self.session_reclaimed_total += delta_mb
            self._update_total_label()
            if self.stack.currentWidget() == self.dashboard:
                self.dashboard.refresh(self.session_reclaimed_total)

    def _autostart_targets(self):
        for i, t in enumerate(self.config.targets):
            if t.autostart: self.gobbler.set_active(i, True)
        self.targets_table.refresh(force=True)

    def closeEvent(self, event):
        if self.config.tray_mode:
            event.ignore(); self.hide();
            if self.tray: self.tray.showMessage(APP_NAME, "Still running in the system tray."); return
        self._unregister_hotkey()
        try: self.gobbler.stop()
        except Exception: pass
        try: self.metrics.save()
        except Exception: pass
        return super().closeEvent(event)

# --- Qt Application wrapper -------------------------------------------------
class RamGobblerApplication(QApplication):
    """QApplication subclass that logs uncaught event-loop exceptions."""

    def notify(self, receiver, event):  # pragma: no cover - Qt specific
        try:
            return super().notify(receiver, event)
        except AttributeError:
            # Dummy fallback used during tests when Qt isn't available.
            return True
        except Exception:
            _log_exception("Unhandled exception in Qt event dispatch")
            raise

# --- Entry point ------------------------------------------------------------
def main():
    args = parse_args(sys.argv[1:])
    _install_global_exception_handlers()
    if args.check_deps:
        diagnostics.print_dependency_status()
        return 0

    if args.install_psutil:
        diagnostics.ensure_config_dir()
        ok, output = diagnostics.install_psutil(user=True, quiet=True)
        print(output.strip())
        if not ok:
            print("psutil installation failed.", file=sys.stderr)
            return 1
        try:
            globals()["psutil"] = importlib.import_module("psutil")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"psutil installed but could not be imported: {exc}", file=sys.stderr)
            return 1
        print("psutil installation succeeded.")
        return 0

    if args.install_pyside6:
        diagnostics.ensure_config_dir()
        ok, output = diagnostics.install_pyside6(user=True, quiet=True)
        print(output.strip())
        if not ok:
            print("PySide6 installation failed.", file=sys.stderr)
            return 1
        try:
            importlib.invalidate_caches()
            importlib.import_module("PySide6")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"PySide6 installed but could not be imported: {exc}", file=sys.stderr)
            return 1
        print("PySide6 installation succeeded.")
        return 0

    if args.show_error_log:
        content = diagnostics.tail_error_log(args.error_lines)
        if content:
            print(content)
        else:
            print("No error log has been recorded yet.")
        return 0

    diagnostics.ensure_config_dir()
    _announce_error_log_location()

    if not ensure_psutil_ready():
        return 1
    if os.name == "nt" and not diagnostics.pyside6_available():
        if not ensure_pyside6_runtime_ready():
            return 1

    config = Config(CONFIG_PATH)
    config.load()
    metrics = Metrics(METRICS_PATH)
    metrics.load()
    if args.headless:
        result = {"ok": True, "reclaimed_mb": 0.0, "trims": 0}
        if args.export_metrics:
            try:
                with open(args.export_metrics, "w", encoding="utf-8") as f:
                    f.write(metrics.export_json(30))
            except Exception:
                pass
        print(json.dumps(result))
        return 0
    app = RamGobblerApplication(sys.argv)
    win = MainWindow(config)
    win.show()
    rc = app.exec()
    config.save()
    return rc


def parse_args(argv=None):
    """Parse command-line arguments used by the small CLI interface."""

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--headless", action="store_true", help="run without GUI")
    parser.add_argument("--trim-visible", action="store_true", help="trim currently visible targets and exit")
    parser.add_argument("--trim-group", help="trim targets belonging to a specific group")
    parser.add_argument("--export-metrics", help="write metrics JSON to the given path")
    parser.add_argument("--check-deps", action="store_true", help="show whether required packages are installed")
    parser.add_argument("--install-psutil", action="store_true", help="install the psutil dependency and exit")
    parser.add_argument("--install-pyside6", action="store_true", help="install the PySide6 dependency and exit")
    parser.add_argument("--show-error-log", action="store_true", help="print the crash log and exit")
    parser.add_argument(
        "--error-lines",
        type=int,
        default=50,
        help="number of lines from the end of the crash log to display",
    )
    return parser.parse_args(argv)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        _log_exception("Fatal error in main", sys.exc_info())
        try:
            app = QApplication.instance() or RamGobblerApplication(sys.argv)
            err = traceback.format_exc()
            QMessageBox.critical(None, APP_NAME, f"Unexpected error:\n{err[:4000]}")
        except Exception:
            pass
        raise
