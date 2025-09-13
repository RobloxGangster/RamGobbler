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
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from collections import deque
from itertools import zip_longest
from typing import Dict, List, Optional, Tuple, Deque
from groups import group_totals
from metrics import Metrics
from matching import matches
from scheduler import schedule_allows_now, jittered_interval, user_idle_seconds, gate_reason, window_active

# --- Platform check & imports ----------------------------------------------
if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import winreg
    import psutil
    from PySide6.QtCore import (
        Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
        QTimer, QSize, QAbstractNativeEventFilter, QRect, QEvent
    )
    from PySide6.QtGui import QIcon, QAction, QKeySequence, QPainter, QPen
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
        QLineEdit, QTableView, QPushButton, QLabel, QHeaderView, QToolBar, QSpinBox,
        QFileDialog, QMessageBox, QAbstractItemView, QSplitter, QStyle,
        QTableWidget, QTableWidgetItem, QCheckBox, QMenu, QDialog, QFormLayout,
        QDialogButtonBox, QSystemTrayIcon, QKeySequenceEdit, QComboBox,
        QListWidget, QInputDialog, QStackedWidget,
    )
else:
    import psutil
    import types
    _dummy_class = type("_Dummy", (), {})
    Qt = types.SimpleNamespace(DisplayRole=0)
    QAbstractTableModel = QModelIndex = QSortFilterProxyModel = QTimer = QSize = QAbstractNativeEventFilter = QRect = QEvent = _dummy_class
    QIcon = QAction = QKeySequence = QPainter = QPen = _dummy_class
    QApplication = QMainWindow = QWidget = QVBoxLayout = QHBoxLayout = QGroupBox = QLineEdit = QTableView = QPushButton = QLabel = QHeaderView = QToolBar = QSpinBox = QFileDialog = QMessageBox = QAbstractItemView = QSplitter = QStyle = QTableWidget = QTableWidgetItem = QCheckBox = QMenu = QDialog = QFormLayout = QDialogButtonBox = QSystemTrayIcon = QKeySequenceEdit = QComboBox = QListWidget = QInputDialog = QStackedWidget = _dummy_class
    ctypes = types.SimpleNamespace(Structure=object, c_size_t=int)
    wintypes = types.SimpleNamespace(
        DWORD=int, BOOL=int, HANDLE=int, HWND=int, INT=int, UINT=int, LONG=int,
        LPARAM=int, WPARAM=int
    )
    winreg = types.SimpleNamespace()

APP_NAME = "Ram Gobbler"
CONFIG_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "RamGobbler")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
METRICS_PATH = os.path.join(CONFIG_DIR, "metrics.json")
DEFAULT_INTERVAL = 15  # seconds

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
    id: str
    name: str
    color: Optional[str] = None


@dataclass
class Target:
    """Configuration for an individual process to trim."""

    key: str               # identifier to match (name/path/cmdline/parent)
    match_by: str          # 'name', 'path', 'cmdline', or 'parent'
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
def list_processes(include_system: bool) -> List[psutil.Process]:
    procs = []
    for p in psutil.process_iter(attrs=["pid", "name", "exe", "memory_info"]):
        try:
            _ = p.info
            if not include_system and is_system_process(p):
                continue
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs

def match_pids(target: Target) -> List[int]:
    pids: List[int] = []
    for p in psutil.process_iter(attrs=["pid", "name", "exe", "cmdline", "ppid"]):
        try:
            parent_name = ""
            ppid = p.info.get("ppid")
            if ppid:
                try:
                    parent_name = psutil.Process(ppid).name()
                except Exception:
                    parent_name = ""
            info = {
                "name": p.info.get("name"),
                "exe": p.info.get("exe"),
                "cmdline": p.info.get("cmdline"),
                "parent": parent_name,
            }
            if matches(info, target):
                pids.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


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

# --- Qt Models --------------------------------------------------------------
class ProcessTableModel(QAbstractTableModel):
    COLS = ["Name", "PID", "Memory (MB)", "Path"]

    def __init__(self, include_system: bool):
        super().__init__()
        self.include_system = include_system
        self._rows: List[Tuple[str, int, float, str]] = []
        self.refresh()

    def set_include_system(self, val: bool):
        self.include_system = val
        self.refresh()

    def refresh(self):
        procs = list_processes(self.include_system)
        rows = []
        for p in procs:
            name = p.info.get("name") or ""
            pid = p.pid
            rss = (p.info.get("memory_info").rss if p.info.get("memory_info") else 0) / (1024 * 1024)
            exe = p.info.get("exe") or ""
            rows.append((name, pid, rss, exe))
        rows.sort(key=lambda r: (-r[2], r[0].lower()))
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.COLS[section]
        return section + 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        name, pid, rss, exe = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return name
            elif col == 1:
                return str(pid)
            elif col == 2:
                return f"{rss:,.1f}"
            elif col == 3:
                return exe
        return None

    def row_at(self, i: int) -> Tuple[str, int, float, str]:
        return self._rows[i]

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
            self.parent().targets_table.refresh()

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
            self.parent().targets_table.refresh()

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
            self.parent().targets_table.refresh()

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
        self.index_to_row: Dict[int, int] = {}
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.refresh()

    def set_group_filter(self, gid: str):
        self.group_filter = gid
        self.refresh()

    def _group_name(self, gid: str) -> str:
        if not gid:
            return "Ungrouped"
        for g in self.config.groups:
            if g.id == gid:
                return g.name
        return gid

    def refresh(self):
        self.setRowCount(0)
        self.index_to_row = {}
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

    def _add_group_header(self, name: str, total: float, history: List[float]):
        row = self.rowCount()
        self.insertRow(row)
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
        self.index_to_row[idx] = row

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

    def _on_start(self, index: int):
        self.gobbler.set_active(index, True)
        self.refresh()

    def _on_stop(self, index: int):
        self.gobbler.set_active(index, False)
        self.refresh()

    def _on_edit(self, index: int):
        if 0 <= index < len(self.config.targets):
            t = self.config.targets[index]
            dlg = RulesDialog(t, self)
            if dlg.exec() == QDialog.Accepted:
                new_t = dlg.apply()
                self.gobbler.update_target(index, new_t)
                self.refresh()

    def _on_remove(self, index: int):
        self.gobbler.remove_target(index)
        self.refresh()

    def _on_interval(self, index: int, val: int):
        self.gobbler.set_interval(index, val)

    def _on_autostart(self, index: int, value: bool):
        self.gobbler.set_autostart(index, value)

    def update_status(self, index: int, message: str):
        row = self.index_to_row.get(index)
        if row is None:
            return
        status_item = self.item(row, 4)
        if status_item:
            t = self.config.targets[index]
            state = "Gobbling" if t.active else "Idle"
            status_item.setText(f"{state} — {message}")
        if self.item(row, 5):
            self.item(row, 5).setText(f"{self.config.targets[index].last_delta_mb:.1f}")
        if self.item(row, 6):
            self.item(row, 6).setText(
                f"{self.config.targets[index].session_reclaimed_mb:.1f} / {self.config.targets[index].day_reclaimed_mb:.1f}"
            )
        spark: Sparkline = self.cellWidget(row, 7)
        if spark:
            spark.set_values(self.config.targets[index].history)

# --- Add Target Dialog ------------------------------------------------------
class AddTargetDialog(QDialog):
    def __init__(self, key: str, match_by: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Target")
        self.setMinimumWidth(520)
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
            count = len(match_pids(t))
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

        self.proc_model = ProcessTableModel(include_system=self.config.show_system)
        self.proxy = QSortFilterProxyModel(); self.proxy.setSourceModel(self.proc_model); self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive); self.proxy.setFilterKeyColumn(-1)
        self.table = QTableView(); self.table.setModel(self.proxy); self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.table.verticalHeader().setVisible(False); self.table.setAlternatingRowColors(True)
        bl.addWidget(self.table)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)

        # Right: Targets with groups sidebar
        rv = QVBoxLayout(right)
        box_right = QGroupBox("Targets")
        rv.addWidget(box_right)
        br = QHBoxLayout(box_right)
        self.groups_sidebar = GroupsSidebar(self.config, self)
        br.addWidget(self.groups_sidebar)
        self.targets_table = TargetsTable(self.config, self.gobbler)
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
        self.refresh_timer = QTimer(self); self.refresh_timer.timeout.connect(self._refresh_periodic); self.refresh_timer.start(2000)

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
        self.config.show_system = bool(self.cb_show_system.isChecked()); self.config.save(); self.proc_model.set_include_system(self.config.show_system)

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
                QLabel, QLineEdit, QTableView, QTableWidget, QHeaderView::section { color: #0b1220; background: #ffffff; }
                QLineEdit { border: 1px solid #cbd2e1; border-radius: 8px; padding: 8px; }
                QTableView, QTableWidget { gridline-color: #e5e9f2; selection-background-color: #d9e6ff; selection-color: #0b1220; }
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
                QLabel, QLineEdit, QTableView, QTableWidget, QHeaderView::section { color: #d9deea; background: #161a22; }
                QLineEdit { border: 1px solid #2a2f3a; border-radius: 8px; padding: 8px; }
                QTableView, QTableWidget { gridline-color: #2a2f3a; selection-background-color: #2b3a55; selection-color: #ffffff; }
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
        self.proc_model.refresh(); self.targets_table.refresh()
        if self.stack.currentWidget() == self.dashboard:
            self.dashboard.refresh(self.session_reclaimed_total)

    def _refresh_processes(self):
        self.proc_model.refresh()

    def _gobble_all(self):
        for i, t in enumerate(self.config.targets):
            if not self.current_group or t.group_id == self.current_group:
                self.gobbler.set_active(i, True)
        self.targets_table.refresh()
        scope = self._group_name(self.current_group) if self.current_group else "all targets"
        self.statusBar().showMessage(f"Gobbling started for {scope}", 3000)

    def _stop_all(self):
        for i, t in enumerate(self.config.targets):
            if not self.current_group or t.group_id == self.current_group:
                self.gobbler.set_active(i, False)
        self.targets_table.refresh()
        scope = self._group_name(self.current_group) if self.current_group else "all targets"
        self.statusBar().showMessage(f"Gobbling stopped for {scope}", 3000)

    def _apply_profile(self, interval_sec: int):
        for i in range(len(self.config.targets)): self.gobbler.set_interval(i, interval_sec)
        self.targets_table.refresh(); self.statusBar().showMessage(f"Applied profile: interval {interval_sec}s to all targets", 3000)

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
            self.proc_model.set_include_system(self.config.show_system); self.targets_table.refresh(); self._apply_styles(self.config.theme)
            self.act_light.setChecked(self.config.theme == "light"); self.act_startup.setChecked(self.config.startup_enabled or is_startup_enabled()); self.cb_show_system.setChecked(self.config.show_system)
            self._apply_hotkey(self.config.hotkey); self.statusBar().showMessage("Settings imported", 3000)
        except Exception as e: QMessageBox.warning(self, APP_NAME, f"Import failed: {e}")

    def _add_selected(self):
        sel = self.table.selectionModel().selectedRows()
        if not sel: QMessageBox.information(self, APP_NAME, "Select a process row first."); return
        row = sel[0].row(); src_row = self.proxy.mapToSource(self.proxy.index(row, 0)).row()
        name, pid, rss, exe = self.proc_model.row_at(src_row)
        try:
            if is_system_process(psutil.Process(pid)) and self.config.protect_system:
                QMessageBox.warning(self, APP_NAME, "This looks like a system process. It's hidden/protected by default."); return
        except Exception: pass
        match_by = "path" if exe else "name"; key = exe or name
        dlg = AddTargetDialog(key, match_by, self)
        if dlg.exec() == QDialog.Accepted:
            key, match_by, interval, autostart = dlg.get_values()
            if not key: return
            self.config.targets.append(Target(key=key, match_by=match_by, interval_sec=interval, autostart=autostart))
            self.config.save(); self.targets_table.refresh();
            if autostart: self.gobbler.set_active(len(self.config.targets)-1, True)

    def _trim_selected(self):
        sel = self.table.selectionModel().selectedRows()
        if not sel: QMessageBox.information(self, APP_NAME, "Select a process row first."); return
        row = sel[0].row(); src_row = self.proxy.mapToSource(self.proxy.index(row, 0)).row()
        name, pid, rss, exe = self.proc_model.row_at(src_row)
        try:
            if self.config.protect_system and is_system_process(psutil.Process(pid)):
                QMessageBox.warning(self, APP_NAME, "Skipping protected system process."); return
        except Exception: pass
        before = rss_mb(pid); ok = empty_working_set(pid); after = rss_mb(pid)
        delta = 0.0
        if before is not None and after is not None:
            delta = max(0.0, before - after); self.session_reclaimed_total += delta; self._update_total_label()
        if ok: self.statusBar().showMessage(f"Trimmed {name} (PID {pid}) −{delta:.1f} MB", 3000)
        else: QMessageBox.warning(self, APP_NAME, "Could not trim this process. Try running as Administrator.")

    def _trim_visible_targets(self):
        total_pids = 0; trimmed_ok = 0; total_delta = 0.0
        for t in self.config.targets:
            if self.current_group and t.group_id != self.current_group:
                continue
            pids = match_pids(t); total_pids += len(pids)
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
        self.targets_table.refresh()

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

# --- Entry point ------------------------------------------------------------
def main():
    args = parse_args(sys.argv[1:])
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
    app = QApplication(sys.argv)
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
    return parser.parse_args(argv)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback, os
        err = traceback.format_exc()
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(os.path.join(CONFIG_DIR, "error.log"), "a", encoding="utf-8") as f:
                f.write(f"
=== Crash {time.strftime('%Y-%m-%d %H:%M:%S')} ===
{err}
")
        except Exception:
            pass
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, APP_NAME, f"Unexpected error:
{err[:4000]}")
        except Exception:
            pass
        raise
