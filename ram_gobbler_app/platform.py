"""Platform abstraction layer wrapping Qt and Win32 APIs.

The original monolithic script imported PySide/Win32 modules directly which
made testing difficult.  This module centralises the imports and exposes a
consistent surface area to the rest of the application.  When Qt is not
available (for example inside automated tests) lightweight Python fallbacks are
installed so imports succeed and the CLI can keep working.
"""

from __future__ import annotations

import ctypes
import os
import sys
import types
from ctypes import wintypes  # type: ignore

QT_AVAILABLE = True


def _install_qt_fallback() -> None:
    """Install lightweight stand-ins for Qt classes when bindings are missing."""

    global QT_AVAILABLE, Qt, QAbstractItemModel, QAbstractTableModel, QModelIndex, QSortFilterProxyModel
    global QTimer, QSize, QAbstractNativeEventFilter, QRect, QEvent, QObject, Signal
    global QIcon, QAction, QKeySequence, QPainter, QPen, QApplication, QMainWindow
    global QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLineEdit, QTableView, QTreeView
    global QPushButton, QLabel, QHeaderView, QToolBar, QSpinBox, QFileDialog
    global QMessageBox, QAbstractItemView, QSplitter, QStyle, QTableWidget
    global QTableWidgetItem, QCheckBox, QMenu, QDialog, QFormLayout
    global QDialogButtonBox, QSystemTrayIcon, QKeySequenceEdit, QComboBox
    global QListWidget, QInputDialog, QStackedWidget, QColorDialog, QColor
    global QFontComboBox, QFont, QToolButton, QSizePolicy, QFrame

    QT_AVAILABLE = False
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    class _Signal:
        def __init__(self, *_, **__):
            self._slots = []

        def connect(self, slot):  # pragma: no cover - trivial fallback behaviour
            self._slots.append(slot)

        def emit(self, *args, **kwargs):  # pragma: no cover - trivial fallback behaviour
            for slot in list(self._slots):
                try:
                    slot(*args, **kwargs)
                except Exception:
                    continue

    signal_names = {
        "triggered",
        "toggled",
        "clicked",
        "stateChanged",
        "valueChanged",
        "textChanged",
        "editingFinished",
        "currentIndexChanged",
        "currentTextChanged",
        "timeout",
        "doubleClicked",
        "accepted",
        "rejected",
        "pressed",
        "released",
        "selectionChanged",
        "returnPressed",
    }

    class _DummyObject:
        _signal_names = signal_names

        def __init__(self, *_, **__):
            self._signals: dict[str, _Signal] = {}

        def __getattr__(self, name):
            if name in self._signal_names:
                sig = self._signals.setdefault(name, _Signal())
                return sig

            def _noop(*_, **__):
                return None

            return _noop

        def exec(self):  # pragma: no cover - fallback only
            return 0

    class _DummyApplication(_DummyObject):
        _instance: "_DummyApplication | None" = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            type(self)._instance = self

        @classmethod
        def instance(cls):  # pragma: no cover - fallback only
            return cls._instance

    class _DummyTimer(_DummyObject):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.timeout = _Signal()

    class _DummyRect(_DummyObject):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.left = self.top = 0
            self.right = self.bottom = 0

    class _DummyMessageBox(_DummyObject):
        @staticmethod
        def critical(*_, **__):
            return None

        @staticmethod
        def information(*_, **__):
            return None

    class _DummyFileDialog(_DummyObject):
        @staticmethod
        def getSaveFileName(*_, **__):
            return "", ""

        @staticmethod
        def getOpenFileName(*_, **__):
            return "", ""

    class _DummyInputDialog(_DummyObject):
        @staticmethod
        def getText(*_, **__):
            return "", False

    class _DummySystemTrayIcon(_DummyObject):
        Trigger = 1

        def isSystemTrayAvailable(self):  # pragma: no cover - fallback only
            return False

    class _DummyHeaderView(_DummyObject):
        Stretch = 0
        ResizeToContents = 1

    class _DummyItemView(_DummyObject):
        ScrollPerPixel = 0
        SelectRows = 0
        NoEditTriggers = 0

    class _DummyColor:
        def __init__(self, value: str = "#000000"):
            self._value = value

        def name(self):  # pragma: no cover - fallback only
            return self._value

        def isValid(self):  # pragma: no cover - fallback only
            return False

    class _DummyColorDialog(_DummyObject):
        @staticmethod
        def getColor(*_, **__):  # pragma: no cover - fallback only
            return _DummyColor()

    class _DummySizePolicy:
        Fixed = 0
        Preferred = 1
        Expanding = 2

        def __init__(self, *_, **__):
            pass

    Qt = types.SimpleNamespace(
        DisplayRole=0,
        ScrollBarAsNeeded=0,
        ItemIsEnabled=1,
        ItemIsSelectable=2,
        NoItemFlags=0,
    )
    QObject = type("QObject", (_DummyObject,), {})
    QAbstractItemModel = type("QAbstractItemModel", (_DummyObject,), {})
    QAbstractTableModel = type("QAbstractTableModel", (_DummyObject,), {})
    QModelIndex = type("QModelIndex", (_DummyObject,), {})
    QSortFilterProxyModel = type("QSortFilterProxyModel", (_DummyObject,), {})
    QTimer = _DummyTimer
    QSize = type("QSize", (_DummyObject,), {})
    QAbstractNativeEventFilter = type("QAbstractNativeEventFilter", (_DummyObject,), {})
    QRect = _DummyRect
    QEvent = type("QEvent", (_DummyObject,), {})
    Signal = _Signal
    QIcon = type("QIcon", (_DummyObject,), {})
    QAction = type("QAction", (_DummyObject,), {})
    QKeySequence = type("QKeySequence", (_DummyObject,), {})
    QPainter = type("QPainter", (_DummyObject,), {})
    QPen = type("QPen", (_DummyObject,), {})
    QApplication = _DummyApplication
    QMainWindow = type("QMainWindow", (_DummyObject,), {})
    QWidget = type("QWidget", (_DummyObject,), {})
    QVBoxLayout = type("QVBoxLayout", (_DummyObject,), {})
    QHBoxLayout = type("QHBoxLayout", (_DummyObject,), {})
    QGroupBox = type("QGroupBox", (_DummyObject,), {})
    QLineEdit = type("QLineEdit", (_DummyObject,), {})
    QTableView = type("QTableView", (_DummyObject,), {})
    QTreeView = type("QTreeView", (_DummyObject,), {})
    QPushButton = type("QPushButton", (_DummyObject,), {})
    QLabel = type("QLabel", (_DummyObject,), {})
    QHeaderView = _DummyHeaderView
    QToolBar = type("QToolBar", (_DummyObject,), {})
    QSpinBox = type("QSpinBox", (_DummyObject,), {})
    QFileDialog = _DummyFileDialog
    QMessageBox = _DummyMessageBox
    QAbstractItemView = _DummyItemView
    QSplitter = type("QSplitter", (_DummyObject,), {})
    QStyle = types.SimpleNamespace(SP_ComputerIcon=0, SP_BrowserReload=1)
    QTableWidget = type("QTableWidget", (_DummyObject,), {})
    QTableWidgetItem = type("QTableWidgetItem", (_DummyObject,), {})
    QCheckBox = type("QCheckBox", (_DummyObject,), {})
    QMenu = type("QMenu", (_DummyObject,), {})
    QDialog = type("QDialog", (_DummyObject,), {})
    QFormLayout = type("QFormLayout", (_DummyObject,), {})
    QDialogButtonBox = type("QDialogButtonBox", (_DummyObject,), {})
    QSystemTrayIcon = _DummySystemTrayIcon
    QKeySequenceEdit = type("QKeySequenceEdit", (_DummyObject,), {})
    QComboBox = type("QComboBox", (_DummyObject,), {})
    QListWidget = type("QListWidget", (_DummyObject,), {})
    QInputDialog = _DummyInputDialog
    QStackedWidget = type("QStackedWidget", (_DummyObject,), {})
    QColorDialog = _DummyColorDialog
    QColor = _DummyColor
    QFontComboBox = type("QFontComboBox", (_DummyObject,), {})
    QFont = type("QFont", (_DummyObject,), {})
    QToolButton = type("QToolButton", (_DummyObject,), {})
    QSizePolicy = _DummySizePolicy
    QFrame = type("QFrame", (_DummyObject,), {})



# Qt / PySide imports ---------------------------------------------------------
if os.name == "nt":  # pragma: no cover - Qt bindings not available in tests
    try:
        from PySide6.QtCore import (
            Qt,
            QAbstractItemModel,
            QAbstractTableModel,
            QModelIndex,
            QSortFilterProxyModel,
            QTimer,
            QSize,
            QAbstractNativeEventFilter,
            QRect,
            QEvent,
            QObject,
            Signal,
        )
        from PySide6.QtGui import QIcon, QAction, QKeySequence, QPainter, QPen, QColor, QFont
        from PySide6.QtWidgets import (
            QApplication,
            QMainWindow,
            QWidget,
            QVBoxLayout,
            QHBoxLayout,
            QGroupBox,
            QLineEdit,
            QTableView,
            QTreeView,
            QPushButton,
            QLabel,
            QHeaderView,
            QToolBar,
            QSpinBox,
            QFileDialog,
            QMessageBox,
            QAbstractItemView,
            QSplitter,
            QStyle,
            QTableWidget,
            QTableWidgetItem,
            QCheckBox,
            QMenu,
            QDialog,
            QFormLayout,
            QDialogButtonBox,
            QSystemTrayIcon,
            QKeySequenceEdit,
            QComboBox,
            QListWidget,
            QInputDialog,
            QStackedWidget,
            QColorDialog,
            QFontComboBox,
            QToolButton,
            QSizePolicy,
            QFrame,
        )
    except Exception:  # pragma: no cover - allows tests without Qt
        _install_qt_fallback()
else:  # pragma: no cover - Linux/macOS CI fallback
    _install_qt_fallback()
    winreg = types.SimpleNamespace()

# Win32 API wrappers ----------------------------------------------------------
if os.name == "nt":  # pragma: no cover - not executed in CI
    import winreg  # type: ignore

    OpenProcess = ctypes.windll.kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    CloseHandle = ctypes.windll.kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    EmptyWorkingSet = ctypes.windll.psapi.EmptyWorkingSet
    EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    EmptyWorkingSet.restype = wintypes.BOOL

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
else:  # pragma: no cover - fallback for non-Windows testing environments
    winreg = types.SimpleNamespace()

    def _no_win32(*_, **__):
        return 0

    OpenProcess = CloseHandle = EmptyWorkingSet = SetProcessWorkingSetSizeEx = _no_win32
    RegisterHotKey = UnregisterHotKey = _no_win32
    GetForegroundWindow = GetWindowThreadProcessId = GetWindowRect = GetSystemMetrics = _no_win32
    SM_CXSCREEN = SM_CYSCREEN = 0
    PROCESS_QUERY_INFORMATION = PROCESS_SET_QUOTA = PROCESS_VM_READ = ACCESS = 0
    RUN_KEY = ""
    RUN_VALUE = "RamGobbler"
    QUOTA_FLAGS = 0

WM_HOTKEY = 0x0312
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

HOTKEY_ID = 1


class RECT(ctypes.Structure):  # type: ignore[misc]
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class POINT(ctypes.Structure):  # type: ignore[misc]
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):  # type: ignore[misc]
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


__all__ = [
    "Qt",
    "QAbstractItemModel",
    "QAbstractTableModel",
    "QModelIndex",
    "QSortFilterProxyModel",
    "QTimer",
    "QSize",
    "QAbstractNativeEventFilter",
    "QRect",
    "QEvent",
    "QObject",
    "Signal",
    "QIcon",
    "QAction",
    "QKeySequence",
    "QPainter",
    "QPen",
    "QApplication",
    "QMainWindow",
    "QWidget",
    "QVBoxLayout",
    "QHBoxLayout",
    "QGroupBox",
    "QLineEdit",
    "QTableView",
    "QTreeView",
    "QPushButton",
    "QLabel",
    "QHeaderView",
    "QToolBar",
    "QSpinBox",
    "QFileDialog",
    "QMessageBox",
    "QAbstractItemView",
    "QSplitter",
    "QStyle",
    "QTableWidget",
    "QTableWidgetItem",
    "QCheckBox",
    "QMenu",
    "QDialog",
    "QFormLayout",
    "QDialogButtonBox",
    "QSystemTrayIcon",
    "QKeySequenceEdit",
    "QComboBox",
    "QListWidget",
    "QInputDialog",
    "QStackedWidget",
    "QColorDialog",
    "QColor",
    "QFontComboBox",
    "QFont",
    "QToolButton",
    "QSizePolicy",
    "QFrame",
    "QT_AVAILABLE",
    "winreg",
    "wintypes",
    "OpenProcess",
    "CloseHandle",
    "EmptyWorkingSet",
    "SetProcessWorkingSetSizeEx",
    "QUOTA_FLAGS",
    "RegisterHotKey",
    "UnregisterHotKey",
    "GetForegroundWindow",
    "GetWindowThreadProcessId",
    "GetWindowRect",
    "GetSystemMetrics",
    "SM_CXSCREEN",
    "SM_CYSCREEN",
    "PROCESS_QUERY_INFORMATION",
    "PROCESS_SET_QUOTA",
    "PROCESS_VM_READ",
    "ACCESS",
    "RUN_KEY",
    "RUN_VALUE",
    "WM_HOTKEY",
    "WM_POWERBROADCAST",
    "PBT_APMRESUMESUSPEND",
    "PBT_APMRESUMEAUTOMATIC",
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_SHIFT",
    "MOD_WIN",
    "HOTKEY_ID",
    "RECT",
    "POINT",
    "MSG",
]
