"""Main application window implementation."""
from __future__ import annotations

import json
import shutil
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from ..constants import APP_NAME, CONFIG_PATH, METRICS_PATH, DEFAULT_INTERVAL
from ..deps import psutil
from ..gobbler import Gobbler
from ..helpers import empty_working_set, rss_mb
from ..hotkeys import WinEventFilter, parse_hotkey_string
from ..models import Config
from ..platform import (
    QAction,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTreeView,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QSize,
    Qt,
    QAbstractItemView,
    QHeaderView,
    QTimer,
    QSortFilterProxyModel,
    QPushButton,
    RegisterHotKey,
    UnregisterHotKey,
    HOTKEY_ID,
)
from ..processes import ProcessNode, match_pids
from ..startup import is_startup_enabled, set_startup_enabled
from ..system import is_system_process
from .components import (
    AddTargetDialog,
    Dashboard,
    GroupSettingsDialog,
    ProcessFilterProxyModel,
    ProcessTreeModel,
    SettingsDialog,
    TargetsTable,
    _InteractionWatcher,
    IntervalEditor,
)
from metrics import Metrics


class MainWindow(QMainWindow):
    """Primary Qt window orchestrating the entire application."""

    def __init__(self, config: Config):
        """Construct the UI, background threads and supporting widgets."""

        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1240, 780)
        self.setWindowIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))

        self.config = config
        self.session_reclaimed_total = 0.0
        self.metrics = Metrics(METRICS_PATH)
        self.metrics.load()
        self.gobbler = Gobbler(self.config, self.metrics, self._on_gobble_result, self._on_delta)
        self.gobbler.start()
        self._hotkey_registered = False
        self.current_group = ""
        self._expanded_pids: Set[int] = set()
        self._process_gobble_tasks: Dict[Tuple[int, ...], Dict[str, object]] = {}
        self._group_menu_actions: List[QAction] = []

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

        self.act_gobble_visible = QAction("Gobble Visible Targets Now", self)
        self.act_gobble_visible.triggered.connect(self._gobble_visible_targets)
        tb.addAction(self.act_gobble_visible)

        self.act_view = QAction("Dashboard", self)
        self.act_view.triggered.connect(self._toggle_view)
        tb.addAction(self.act_view)

        m_profiles = QMenu("Profiles", self)
        for label, secs in [("Balanced (15s)", 15), ("Aggressive (8s)", 8), ("Gentle (30s)", 30)]:
            action = QAction(label, self)
            action.triggered.connect(lambda _, s=secs: self._apply_profile(s))
            m_profiles.addAction(action)
        btn_profiles = QAction("Profiles", self)
        btn_profiles.setMenu(m_profiles)
        tb.addAction(btn_profiles)

        tb.addSeparator()

        self.act_export = QAction("Export Settings…", self)
        self.act_export.triggered.connect(self._export_settings)
        tb.addAction(self.act_export)
        self.act_import = QAction("Import Settings…", self)
        self.act_import.triggered.connect(self._import_settings)
        tb.addAction(self.act_import)

        self.act_settings = QAction("Settings…", self)
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

        self.act_group_settings = QAction("Group Settings…", self)
        self.act_group_settings.triggered.connect(self._open_group_settings)
        tb.addAction(self.act_group_settings)

        self.group_menu = QMenu("Select Group", self)
        try:
            self.group_menu.triggered.connect(self._handle_group_menu_triggered)
        except Exception:
            pass
        self.act_group_selector = QAction("Group: All Targets", self)
        try:
            self.act_group_selector.setMenu(self.group_menu)
        except Exception:
            pass
        tb.addAction(self.act_group_selector)
        self._rebuild_group_menu()
        self._update_group_selector_label()

        splitter = QSplitter()
        left = QWidget()
        right = QWidget()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([650, 560])
        splitter.setChildrenCollapsible(False)
        for idx in range(splitter.count()):
            splitter.setCollapsible(idx, False)
        splitter.splitterMoved.connect(self._prevent_splitter_collapse)
        self.splitter = splitter

        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        box_left = QGroupBox("Running Processes")
        lv.addWidget(box_left, 1)
        bl = QVBoxLayout(box_left)
        bl.setContentsMargins(12, 12, 12, 12)
        bl.setSpacing(8)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by name or path…")
        self.btn_gobble_now = QPushButton("Gobble Now")
        self.btn_gobble_now.setToolTip("Gobble the selected process immediately")
        self.btn_gobble_now.clicked.connect(self._gobble_selected)
        self.btn_add_selected = QPushButton("Add Selected as Target")
        self.btn_add_selected.clicked.connect(self._add_selected)
        search_row.addWidget(self.search)
        search_row.addWidget(self.btn_gobble_now)
        search_row.addWidget(self.btn_add_selected)
        search_row.addStretch(1)
        bl.addLayout(search_row)

        self.proc_model = ProcessTreeModel(include_system=self.config.show_system)
        self.proxy = ProcessFilterProxyModel()
        self.proxy.setSourceModel(self.proc_model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.table = QTreeView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setUniformRowHeights(True)
        header = self.table.header()
        try:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.Stretch)
            for col in range(1, self.proc_model.columnCount()):
                header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        except Exception:
            self.table.header().setSectionResizeMode(QHeaderView.Stretch)
        if hasattr(self.table, "verticalHeader"):
            try:
                self.table.verticalHeader().setVisible(False)
            except Exception:
                pass
        self.table.setAlternatingRowColors(True)
        if hasattr(self.table, "setContextMenuPolicy") and hasattr(Qt, "CustomContextMenu"):
            try:
                self.table.setContextMenuPolicy(Qt.CustomContextMenu)
                self.table.customContextMenuRequested.connect(self._show_process_context_menu)
            except Exception:
                pass
        bl.addWidget(self.table, 1)
        self._process_interaction = _InteractionWatcher(self.table)
        self._process_interaction.attach(self.table)
        self._process_interaction.attach(self.table.viewport())
        self._process_interaction.attach(self.table.verticalScrollBar())
        self._process_interaction.attach(self.table.horizontalScrollBar())
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        if hasattr(self.table, "expanded"):
            try:
                self.table.expanded.connect(self._on_tree_expanded)
            except Exception:
                pass
        if hasattr(self.table, "collapsed"):
            try:
                self.table.collapsed.connect(self._on_tree_collapsed)
            except Exception:
                pass

        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        box_right = QGroupBox("Targets")
        rv.addWidget(box_right, 1)
        br = QVBoxLayout(box_right)
        br.setContentsMargins(12, 12, 12, 12)
        br.setSpacing(8)
        self.targets_table = TargetsTable(self.config, self.gobbler)
        br.addWidget(self.targets_table)

        self.dashboard = Dashboard(self.config, self.metrics)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.splitter)
        self.stack.addWidget(self.dashboard)
        self.setCentralWidget(self.stack)

        self.lbl_total = QLabel("Session reclaimed: 0.0 MB")
        self.statusBar().addPermanentWidget(self.lbl_total)
        self.statusBar().showMessage("Ready")

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_periodic)
        self.refresh_timer.start(5000)
        self._refresh_debounce = QTimer(self)
        self._refresh_debounce.setSingleShot(True)
        self._refresh_debounce.timeout.connect(self._refresh_periodic)

        self._init_tray()

        self._apply_styles(self.config.theme)

        QTimer.singleShot(0, self._autostart_targets)

        self._event_filter = WinEventFilter(self._on_hotkey, self._on_resume)
        QApplication.instance().installNativeEventFilter(self._event_filter)

        QTimer.singleShot(0, lambda: self._apply_hotkey(self.config.hotkey))

        self._update_toolbar_scope()

    def _prevent_splitter_collapse(self, pos: int, _index: int) -> None:
        """Ensure splitter handles cannot collapse panels entirely."""

        splitter = getattr(self, "splitter", None)
        if splitter is None:
            return

        sizes = splitter.sizes()
        if not sizes:
            return

        total = sum(sizes)
        handle_width = splitter.handleWidth()
        if splitter.orientation() == Qt.Horizontal:
            available = splitter.width()
        else:
            available = splitter.height()
        max_pos = max(0, available - handle_width)
        edge_slop = min(max(1, handle_width // 2), max_pos)

        if len(sizes) == 2 and total > 0:
            if pos <= edge_slop:
                self._set_splitter_sizes([0, total])
                return
            if pos >= max_pos - edge_slop:
                self._set_splitter_sizes([total, 0])
                return

        min_size = 1
        if all(size >= min_size for size in sizes):
            return

        self._set_splitter_sizes([max(size, min_size) for size in sizes])

    def _set_splitter_sizes(self, sizes: List[int]) -> None:
        """Update splitter sizes while temporarily allowing collapse."""

        splitter = getattr(self, "splitter", None)
        if splitter is None:
            return

        splitter.blockSignals(True)
        try:
            collapsible_states = [splitter.isCollapsible(idx) for idx in range(splitter.count())]
            for idx in range(splitter.count()):
                splitter.setCollapsible(idx, True)
            splitter.setSizes(sizes)
        finally:
            for idx, was_collapsible in enumerate(collapsible_states):
                splitter.setCollapsible(idx, was_collapsible)
            splitter.blockSignals(False)

    def _apply_theme_choice(
        self, theme: str, *, persist: bool = True, previous: Optional[str] = None
    ) -> None:
        """Apply the requested theme and optionally persist it."""

        selected = "light" if theme == "light" else "dark"
        prior = previous if previous is not None else self.config.theme
        changed = prior != selected
        self.config.theme = selected
        if changed:
            self._apply_styles(selected)
        if persist:
            self.config.save()

    def _toggle_theme(self, on: bool):
        """Switch between dark and light stylesheets."""

        self._apply_theme_choice("light" if on else "dark", persist=True)

    def _apply_tray_mode(
        self, enabled: bool, *, persist: bool = True, previous: Optional[bool] = None
    ) -> None:
        """Persist the minimise-to-tray preference."""

        val = bool(enabled)
        prior = previous if previous is not None else self.config.tray_mode
        changed = prior != val
        self.config.tray_mode = val
        if persist:
            self.config.save()
        if persist and changed:
            msg = "Minimize to tray on close enabled" if val else "Minimize to tray on close disabled"
            self.statusBar().showMessage(msg, 3000)

    def _toggle_tray_mode(self, on: bool):
        """Persist the minimise-to-tray preference."""

        self._apply_tray_mode(on, persist=True)

    def _apply_startup_preference(
        self, enabled: bool, *, persist: bool = True, previous: Optional[bool] = None
    ) -> bool:
        """Enable or disable the Windows startup registration."""

        target = bool(enabled)
        ok, msg = set_startup_enabled(target)
        if not ok:
            QMessageBox.warning(self, APP_NAME, msg)
            if previous is not None:
                self.config.startup_enabled = bool(previous)
            else:
                self.config.startup_enabled = is_startup_enabled()
            return False
        self.config.startup_enabled = target
        if persist:
            self.config.save()
        self.statusBar().showMessage(msg, 3000)
        return True

    def _toggle_startup(self, on: bool):
        """Enable or disable the Windows startup registration."""

        self._apply_startup_preference(on, persist=True)

    def _refresh_show_system_filter(self, include_system: bool):
        """Refresh the process table when the system filter changes."""

        pid = self._current_pid()
        bar = self.table.verticalScrollBar()
        scroll = bar.value() if bar else 0

        def done():
            self._restore_expanded()
            self._select_pid(pid)
            self._restore_scrollbar(bar, scroll)

        self.proc_model.set_include_system(include_system, done)

    def _on_group_selected(self, gid: str):
        """Filter the targets table to the newly selected group."""

        gid = gid or ""
        if gid == self.current_group:
            self._update_toolbar_scope()
            self._update_group_selector_label()
            self._update_group_menu_checks()
            return

        self.current_group = gid
        self.targets_table.set_group_filter(gid)
        self._update_toolbar_scope()
        self._update_group_selector_label()
        self._update_group_menu_checks()

    def _group_name(self, gid: str) -> str:
        """Return a displayable name for ``gid``."""

        if not gid:
            return "Ungrouped"
        for group in self.config.groups:
            if group.id == gid:
                return group.name
        return gid

    def _update_group_selector_label(self) -> None:
        """Reflect the current group selection in the toolbar button."""

        label = "All Targets" if not self.current_group else self._group_name(self.current_group)
        try:
            self.act_group_selector.setText(f"Group: {label}")
        except Exception:
            pass

    def _handle_group_menu_triggered(self, action) -> None:
        """Handle selection changes from the group dropdown menu."""

        if action is None:
            return
        data_method = getattr(action, "data", None)
        gid = ""
        if callable(data_method):
            try:
                gid = data_method() or ""
            except Exception:
                gid = ""
        self._on_group_selected(gid)

    def _rebuild_group_menu(self) -> None:
        """Populate the group dropdown with the latest configuration."""

        if not hasattr(self, "group_menu"):
            return

        try:
            self.group_menu.clear()
        except Exception:
            return

        self._group_menu_actions = []

        action_all = self.group_menu.addAction("All Targets")
        try:
            action_all.setData("")
            action_all.setCheckable(True)
        except Exception:
            pass
        self._group_menu_actions.append(action_all)

        if getattr(self.config, "groups", None):
            try:
                self.group_menu.addSeparator()
            except Exception:
                pass
        for group in getattr(self.config, "groups", []):
            action = self.group_menu.addAction(group.name)
            try:
                action.setData(group.id)
                action.setCheckable(True)
            except Exception:
                pass
            self._group_menu_actions.append(action)

        self._update_group_menu_checks()

    def _update_group_menu_checks(self) -> None:
        """Ensure the dropdown menu highlights the active group."""

        target = self.current_group or ""
        for action in getattr(self, "_group_menu_actions", []) or []:
            set_checked = getattr(action, "setChecked", None)
            data_method = getattr(action, "data", None)
            if callable(set_checked):
                gid = ""
                if callable(data_method):
                    try:
                        gid = data_method() or ""
                    except Exception:
                        gid = ""
                try:
                    set_checked(gid == target)
                except Exception:
                    continue

    def _update_toolbar_scope(self):
        """Adjust toolbar labels to reflect the active group filter."""

        if self.current_group:
            name = self._group_name(self.current_group)
            self.act_gobble_all.setText(f"Gobble Group {name}")
            self.act_stop_all.setText(f"Stop Group {name}")
            self.act_gobble_visible.setText(f"Gobble {name} Targets Now")
        else:
            self.act_gobble_all.setText("Gobble All Targets")
            self.act_stop_all.setText("Stop All")
            self.act_gobble_visible.setText("Gobble Visible Targets Now")

    def _toggle_view(self):
        """Switch between the targets table and the metrics dashboard."""

        if self.stack.currentWidget() == self.dashboard:
            self.stack.setCurrentWidget(self.splitter)
            self.act_view.setText("Dashboard")
        else:
            self.dashboard.refresh(self.session_reclaimed_total)
            self.stack.setCurrentWidget(self.dashboard)
            self.act_view.setText("Targets")

    def _apply_styles(self, theme: str):
        """Apply a stylesheet matching the chosen theme."""

        if theme == "light":
            self.setStyleSheet(
                """
                * {
                    font-family: "Segoe UI", "Helvetica Neue", sans-serif;
                    font-size: 12px;
                    color: #111827;
                }
                QMainWindow {
                    background-color: #e7ecf6;
                }
                QToolBar {
                    background: #ffffff;
                    border: none;
                    padding: 6px 12px;
                    spacing: 8px;
                }
                QToolBar QToolButton {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f6f8ff, stop:1 #e6ebfa);
                    border: 1px solid #d1d9ec;
                    border-radius: 10px;
                    padding: 6px 14px;
                    color: #0f172a;
                }
                QToolBar QToolButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #edf2ff, stop:1 #dce6ff);
                    border-color: #b8c4e4;
                }
                QToolBar QToolButton:pressed {
                    background: #cdd8fa;
                    border-color: #94a3da;
                }
                QStatusBar {
                    background: #ffffff;
                    border-top: 1px solid #d1d9ec;
                }
                QStatusBar::item {
                    border: none;
                }
                QGroupBox {
                    background: #ffffff;
                    border: 1px solid #d9e1f5;
                    border-radius: 18px;
                    margin-top: 22px;
                    padding-top: 16px;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    subcontrol-position: top left;
                    padding: 0px 12px;
                    margin-top: 4px;
                    margin-left: 20px;
                    font-weight: 600;
                    color: #1f2937;
                }
                QLabel {
                    background: transparent;
                }
                QLineEdit, QSpinBox, QComboBox, QKeySequenceEdit {
                    background: #f9fbff;
                    border: 1px solid #c7d0e5;
                    border-radius: 10px;
                    padding: 8px 12px;
                    selection-background-color: #4c6ef5;
                    selection-color: #ffffff;
                }
                QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QKeySequenceEdit:focus {
                    border: 1px solid #4c6ef5;
                }
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4660ff, stop:1 #6b7bff);
                    color: #ffffff;
                    border: none;
                    border-radius: 12px;
                    padding: 8px 18px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #516cff, stop:1 #8090ff);
                }
                QPushButton:pressed {
                    background: #3e55dc;
                }
                QPushButton:disabled {
                    background: #e3e8f5;
                    color: #94a3b8;
                }
                QTreeView, QTableView, QTableWidget {
                    background: #fdfdff;
                    alternate-background-color: #f1f4fb;
                    border: 1px solid #d9e1f5;
                    border-radius: 14px;
                    padding: 6px;
                    gridline-color: #dfe6f5;
                }
                QTreeView::item:hover, QTableView::item:hover, QTableWidget::item:hover {
                    background: #e4ecff;
                }
                QTreeView::item:selected, QTreeView::branch:selected,
                QTableView::item:selected, QTableWidget::item:selected {
                    background: #4c6ef5;
                    color: #ffffff;
                }
                QHeaderView::section {
                    background: #eef2ff;
                    color: #1f2937;
                    padding: 6px 10px;
                    border: none;
                    border-right: 1px solid #d9e1f5;
                    font-weight: 600;
                }
                QHeaderView::section:last {
                    border-right: none;
                }
                QScrollBar:vertical {
                    background: transparent;
                    width: 12px;
                    margin: 4px;
                }
                QScrollBar::handle:vertical {
                    background: #c7d0e5;
                    border-radius: 6px;
                }
                QScrollBar::handle:vertical:hover {
                    background: #aab8dc;
                }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                    height: 0px;
                }
                QScrollBar:horizontal {
                    background: transparent;
                    height: 12px;
                    margin: 4px;
                }
                QScrollBar::handle:horizontal {
                    background: #c7d0e5;
                    border-radius: 6px;
                }
                QScrollBar::handle:horizontal:hover {
                    background: #aab8dc;
                }
                QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                    width: 0px;
                }
                QMenu {
                    background: #ffffff;
                    border: 1px solid #d1d9ec;
                    border-radius: 10px;
                    padding: 6px 0;
                }
                QMenu::item {
                    padding: 6px 16px;
                    border-radius: 6px;
                }
                QMenu::item:selected {
                    background: #e4ecff;
                }
                QSplitter::handle {
                    background: #d1d9ec;
                    margin: 0 6px;
                }
                QSplitter::handle:pressed {
                    background: #4c6ef5;
                }
                """
            )
        else:
            self.setStyleSheet(
                """
                * {
                    font-family: "Segoe UI", "Helvetica Neue", sans-serif;
                    font-size: 12px;
                    color: #e5e9f5;
                }
                QMainWindow {
                    background-color: #0c0f19;
                }
                QToolBar {
                    background: #111627;
                    border: none;
                    padding: 6px 12px;
                    spacing: 8px;
                }
                QToolBar QToolButton {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1a223a, stop:1 #11182c);
                    border: 1px solid #1f2a46;
                    border-radius: 10px;
                    padding: 6px 14px;
                    color: #f3f5ff;
                }
                QToolBar QToolButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #253255, stop:1 #1a2440);
                    border-color: #2f3f68;
                }
                QToolBar QToolButton:pressed {
                    background: #233053;
                    border-color: #4a64aa;
                }
                QStatusBar {
                    background: #111627;
                    border-top: 1px solid #1f2a46;
                }
                QStatusBar::item {
                    border: none;
                }
                QGroupBox {
                    background: #111627;
                    border: 1px solid #1f2a46;
                    border-radius: 18px;
                    margin-top: 22px;
                    padding-top: 16px;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    subcontrol-position: top left;
                    padding: 0px 12px;
                    margin-top: 4px;
                    margin-left: 20px;
                    font-weight: 600;
                    color: #f3f5ff;
                }
                QLabel {
                    background: transparent;
                }
                QLineEdit, QSpinBox, QComboBox, QKeySequenceEdit {
                    background: #161d33;
                    border: 1px solid #253255;
                    border-radius: 10px;
                    padding: 8px 12px;
                    selection-background-color: #5b82ff;
                    selection-color: #0c0f19;
                }
                QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QKeySequenceEdit:focus {
                    border: 1px solid #5b82ff;
                }
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #5b82ff, stop:1 #8a6bff);
                    color: #0c0f19;
                    border: none;
                    border-radius: 12px;
                    padding: 8px 18px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6a8fff, stop:1 #9b7aff);
                }
                QPushButton:pressed {
                    background: #4c6ce0;
                }
                QPushButton:disabled {
                    background: #1e253f;
                    color: #4c5675;
                }
                QTreeView, QTableView, QTableWidget {
                    background: #0f1527;
                    alternate-background-color: #151d33;
                    border: 1px solid #1f2a46;
                    border-radius: 14px;
                    padding: 6px;
                    gridline-color: #1f2a46;
                }
                QTreeView::item:hover, QTableView::item:hover, QTableWidget::item:hover {
                    background: #1e2b4a;
                }
                QTreeView::item:selected, QTreeView::branch:selected,
                QTableView::item:selected, QTableWidget::item:selected {
                    background: #5b82ff;
                    color: #0c0f19;
                }
                QHeaderView::section {
                    background: #1a223b;
                    color: #f3f5ff;
                    padding: 6px 10px;
                    border: none;
                    border-right: 1px solid #253255;
                    font-weight: 600;
                }
                QHeaderView::section:last {
                    border-right: none;
                }
                QScrollBar:vertical {
                    background: transparent;
                    width: 12px;
                    margin: 4px;
                }
                QScrollBar::handle:vertical {
                    background: #2a365a;
                    border-radius: 6px;
                }
                QScrollBar::handle:vertical:hover {
                    background: #384a78;
                }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                    height: 0px;
                }
                QScrollBar:horizontal {
                    background: transparent;
                    height: 12px;
                    margin: 4px;
                }
                QScrollBar::handle:horizontal {
                    background: #2a365a;
                    border-radius: 6px;
                }
                QScrollBar::handle:horizontal:hover {
                    background: #384a78;
                }
                QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                    width: 0px;
                }
                QMenu {
                    background: #111627;
                    border: 1px solid #1f2a46;
                    border-radius: 10px;
                    padding: 6px 0;
                }
                QMenu::item {
                    padding: 6px 16px;
                    border-radius: 6px;
                }
                QMenu::item:selected {
                    background: #1f2d4d;
                }
                QSplitter::handle {
                    background: #1f2a46;
                    margin: 0 6px;
                }
                QSplitter::handle:pressed {
                    background: #5b82ff;
                }
                """
            )

    def _init_tray(self):
        """Initialise the optional system-tray icon and context menu."""

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        menu = QMenu()
        act_show = QAction("Show/Hide", self)
        act_show.triggered.connect(self._toggle_window)
        act_gobble_visible = QAction("Gobble Visible Targets Now", self)
        act_gobble_visible.triggered.connect(self._gobble_visible_targets)
        act_gobble_all = QAction("Gobble All Targets", self)
        act_gobble_all.triggered.connect(self._gobble_all)
        act_stop_all = QAction("Stop All", self)
        act_stop_all.triggered.connect(self._stop_all)
        act_settings = QAction("Settings…", self)
        act_settings.triggered.connect(self._open_settings)
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_gobble_visible)
        menu.addAction(act_gobble_all)
        menu.addAction(act_stop_all)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _toggle_window(self):
        """Show or hide the window in response to tray interactions."""

        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _quit(self):
        """Terminate background work and exit the application."""

        self._unregister_hotkey()
        try:
            self.gobbler.stop()
        except Exception:
            pass
        self.tray.hide()
        QApplication.instance().quit()

    def _tray_activated(self, reason):
        """Toggle the window when the tray icon is activated."""

        if reason == QSystemTrayIcon.Trigger:
            self._toggle_window()

    def _refresh_periodic(self):
        """Periodically refresh process data unless the user is interacting."""

        interaction_busy = getattr(self, "_process_interaction", None)
        if self.targets_table.is_busy() or (interaction_busy and interaction_busy.active):
            self._refresh_debounce.start(250)
            return

        bar = self.table.verticalScrollBar()
        pid = self._current_pid()
        proc_scroll = bar.value() if bar else 0

        def done():
            self._restore_expanded()
            self._select_pid(pid)
            self._restore_scrollbar(bar, proc_scroll)

        self.proc_model.refresh(done)
        self.targets_table.refresh()

        if self.stack.currentWidget() == self.dashboard:
            self.dashboard.refresh(self.session_reclaimed_total)

    def _refresh_processes(self):
        """Explicit refresh triggered by toolbar or timer events."""

        pid = self._current_pid()
        bar = self.table.verticalScrollBar()
        scroll = bar.value() if bar else 0

        def done():
            self._restore_expanded()
            self._select_pid(pid)
            self._restore_scrollbar(bar, scroll)

        self.proc_model.refresh(done)

    def _current_pid(self) -> Optional[int]:
        """Return the PID of the currently selected process, if any."""

        if not self.table.selectionModel():
            return None
        idx = self.table.selectionModel().currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        if not src.isValid():
            return None
        item = self.proc_model.node_for_index(src)
        if not item or not item.node:
            return None
        return item.node.pid

    def _select_pid(self, pid: Optional[int]):
        """Highlight ``pid`` in the process table if it exists."""

        if pid is None:
            return
        src_index = self.proc_model.index_for_pid(pid)
        if not src_index.isValid():
            return
        proxy_index = self.proxy.mapFromSource(src_index)
        if not proxy_index.isValid():
            return
        self._expand_ancestors(proxy_index)
        self.table.setCurrentIndex(proxy_index)
        try:
            self.table.scrollTo(proxy_index)
        except Exception:
            pass

    def _restore_scrollbar(self, bar, value: int):
        """Restore ``bar`` to ``value`` without firing change handlers."""

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

    def _expand_ancestors(self, proxy_index):
        """Ensure ancestors of ``proxy_index`` are expanded."""

        parent = proxy_index.parent()
        stack = []
        while parent.isValid():
            stack.append(parent)
            parent = parent.parent()
        while stack:
            idx = stack.pop()
            src_parent = self.proxy.mapToSource(idx)
            item = self.proc_model.node_for_index(src_parent)
            if item and item.node:
                try:
                    self.table.expand(idx)
                except Exception:
                    pass
                self._expanded_pids.add(item.node.pid)

    def _restore_expanded(self):
        """Re-expand tree nodes that were previously expanded."""

        if not self._expanded_pids:
            return
        keep: Set[int] = set()
        for pid in list(self._expanded_pids):
            src_index = self.proc_model.index_for_pid(pid)
            if not src_index.isValid():
                continue
            proxy_index = self.proxy.mapFromSource(src_index)
            if not proxy_index.isValid():
                continue
            self._expand_ancestors(proxy_index)
            try:
                self.table.expand(proxy_index)
            except Exception:
                continue
            item = self.proc_model.node_for_index(src_index)
            if item and item.node:
                keep.add(item.node.pid)
        self._expanded_pids = keep

    def _on_tree_expanded(self, index):
        """Track expanded nodes so state can be restored after refreshes."""

        src = self.proxy.mapToSource(index)
        item = self.proc_model.node_for_index(src)
        if item and item.node:
            self._expanded_pids.add(item.node.pid)

    def _on_tree_collapsed(self, index):
        """Remove collapsed nodes (and descendants) from the remembered set."""

        src = self.proxy.mapToSource(index)
        item = self.proc_model.node_for_index(src)
        if not item or not item.node:
            return
        for pid in item.all_pids():
            self._expanded_pids.discard(pid)

    def _gobble_all(self):
        """Activate gobbling for every target within scope."""

        for index, target in enumerate(self.config.targets):
            if not self.current_group or target.group_id == self.current_group:
                self.gobbler.trigger_now(index, ensure_active=True, continuous=True)
        self.targets_table.refresh(force=True)
        scope = self._group_name(self.current_group) if self.current_group else "all targets"
        self.statusBar().showMessage(f"Gobbling started for {scope}", 3000)

    def _stop_all(self):
        """Pause gobbling for every target within scope."""

        for index, target in enumerate(self.config.targets):
            if not self.current_group or target.group_id == self.current_group:
                self.gobbler.set_active(index, False)
        self.targets_table.refresh(force=True)
        scope = self._group_name(self.current_group) if self.current_group else "all targets"
        self.statusBar().showMessage(f"Gobbling stopped for {scope}", 3000)

    def _apply_profile(self, interval_sec: int):
        """Apply a preset interval to every configured target."""

        for index in range(len(self.config.targets)):
            self.gobbler.set_interval(index, interval_sec)
        self.targets_table.refresh(force=True)
        self.statusBar().showMessage(f"Applied profile: interval {interval_sec}s to all targets", 3000)

    def _export_settings(self):
        """Write the current configuration to a user-selected JSON file."""

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Settings",
            "ramgobbler-config.json",
            "JSON Files (*.json)",
        )
        if not path:
            return
        try:
            self.config.save()
            shutil.copy2(CONFIG_PATH, path)
            self.statusBar().showMessage("Settings exported", 3000)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Export failed: {exc}")

    def _import_settings(self):
        """Load configuration from disk and refresh the UI."""

        path, _ = QFileDialog.getOpenFileName(self, "Import Settings", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict) or "targets" not in raw:
                raise ValueError("Invalid config file")
            self.config.save()
            shutil.copy2(path, CONFIG_PATH)
            prev_theme = self.config.theme
            prev_tray = self.config.tray_mode
            prev_startup = self.config.startup_enabled
            prev_show_system = self.config.show_system
            self.config.load()
            if self.config.show_system != prev_show_system:
                self._refresh_show_system_filter(self.config.show_system)
            selected_group = self.current_group
            if selected_group and not any(
                group.id == selected_group for group in self.config.groups
            ):
                selected_group = ""
            self._rebuild_group_menu()
            self.targets_table.refresh(force=True)
            if self.config.theme != prev_theme:
                self._apply_theme_choice(
                    self.config.theme, persist=False, previous=prev_theme
                )
            if self.config.tray_mode != prev_tray:
                self._apply_tray_mode(
                    self.config.tray_mode, persist=False, previous=prev_tray
                )
            startup_msg_shown = False
            if self.config.startup_enabled != prev_startup:
                startup_msg_shown = self._apply_startup_preference(
                    self.config.startup_enabled, persist=False, previous=prev_startup
                )
            self._apply_hotkey(self.config.hotkey)
            self._on_group_selected(selected_group)
            if not startup_msg_shown:
                self.statusBar().showMessage("Settings imported", 3000)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Import failed: {exc}")

    def _add_selected(self):
        """Create a new target based on the selected process row."""

        selection = self.table.selectionModel().selectedRows()
        if not selection:
            QMessageBox.information(self, APP_NAME, "Select a process row first.")
            return

        proxy_index = selection[0]
        src_index = self.proxy.mapToSource(proxy_index)
        item = self.proc_model.node_for_index(src_index)
        if not item:
            return

        self._add_process_item_as_target(item)

    def _add_process_item_as_target(self, item, *, warn: bool = True) -> None:
        """Open the add-target dialog for the provided process tree item."""

        if item is None:
            return

        nodes = list(item.iter_nodes()) if hasattr(item, "iter_nodes") else []
        if not nodes:
            if warn:
                QMessageBox.information(
                    self,
                    APP_NAME,
                    "No processes were found for that selection.",
                )
            return

        if warn and item.node and self.config.protect_system:
            try:
                if is_system_process(psutil.Process(item.node.pid)):
                    QMessageBox.warning(
                        self,
                        APP_NAME,
                        "This looks like a system process. It's hidden/protected by default.",
                    )
                    return
            except Exception:
                pass

        def _describe(node: ProcessNode) -> str:
            return node.name or node.exe or f"PID {node.pid}"

        def _pattern_for(node: ProcessNode) -> Tuple[str, str]:
            if node.exe:
                return node.exe, "path"
            if node.name:
                return node.name, "name"
            if node.cmdline:
                joined = " ".join(part for part in node.cmdline if part).strip()
                if joined:
                    return joined, "cmdline"
            return "", "name"

        def _common_attr(items: List[ProcessNode], attr: str) -> str:
            values = [getattr(node, attr) for node in items if getattr(node, attr)]
            if not values:
                return ""
            first = values[0]
            first_key = first.casefold() if isinstance(first, str) else first
            for value in values[1:]:
                comp = value.casefold() if isinstance(value, str) else value
                if comp != first_key:
                    return ""
            return first

        primary_node = item.node or max(
            nodes,
            key=lambda n: (n.aggregate_rss_mb, n.rss_mb, -n.pid),
        )

        include_children_default = bool(primary_node.children)
        key_default: str
        match_default: str

        if item.node is None:
            shared_exe = _common_attr(nodes, "exe")
            shared_name = _common_attr(nodes, "name")
            if shared_exe:
                key_default, match_default = shared_exe, "path"
            elif shared_name:
                key_default, match_default = shared_name, "name"
            else:
                key_default, match_default = _pattern_for(primary_node)
            if len(nodes) > 1:
                include_children_default = True
        else:
            key_default, match_default = _pattern_for(primary_node)

        if not key_default:
            if warn:
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    "Couldn't determine how to match that process. Try selecting a different row.",
                )
            return

        summary_nodes = [node for node in nodes if node.pid != primary_node.pid]
        summary_lines: List[str] = []
        for node in summary_nodes[:12]:
            summary_lines.append(f"{_describe(node)} (PID {node.pid})")
        remaining = len(summary_nodes) - len(summary_lines)
        if remaining > 0:
            summary_lines.append(f"…and {remaining} more")

        dialog = AddTargetDialog(
            self.config,
            key_default,
            match_default,
            self,
            summary=summary_lines or None,
            include_children=include_children_default,
            group_id=self.current_group or "",
        )
        if dialog.exec() != QDialog.Accepted:
            return

        target = dialog.get_target()
        key = target.key.strip()
        if not key:
            return

        target.key = key
        match_by = target.match_by
        canonical = (key.casefold(), match_by)
        existing = {(t.key.casefold(), t.match_by) for t in self.config.targets}
        if canonical in existing:
            QMessageBox.information(
                self,
                APP_NAME,
                "That target already exists. Adjust the pattern or remove the previous entry.",
            )
            return

        self.config.targets.append(target)
        new_index = len(self.config.targets) - 1
        self.config.save()
        self.targets_table.refresh(force=True)

        if target.autostart:
            self.gobbler.set_active(new_index, True)

        label = item.display_name() or _describe(primary_node)
        if summary_nodes:
            count = len(summary_nodes)
            suffix = f" with {count} related process{'es' if count != 1 else ''}"
        else:
            suffix = ""
        self.statusBar().showMessage(
            f"Added target for {label}{suffix}",
            3000,
        )

    def _process_item_summary(self, item, warn: bool) -> Optional[Tuple[List[int], str, ProcessNode, List[ProcessNode]]]:
        """Return PID list, display label and primary node for ``item``."""

        if item is None:
            return None

        nodes = list(item.iter_nodes()) if hasattr(item, "iter_nodes") else []
        if not nodes:
            if warn:
                QMessageBox.information(
                    self,
                    APP_NAME,
                    "No processes were found for that selection.",
                )
            return None

        if warn and getattr(item, "node", None) and self.config.protect_system:
            try:
                if is_system_process(psutil.Process(item.node.pid)):
                    QMessageBox.warning(
                        self,
                        APP_NAME,
                        "This looks like a system process. It's hidden/protected by default.",
                    )
                    return None
            except Exception:
                pass

        seen: Set[int] = set()
        pids: List[int] = []
        if hasattr(item, "all_pids"):
            for pid in item.all_pids():
                if pid not in seen:
                    seen.add(pid)
                    pids.append(pid)

        if not pids:
            if warn:
                QMessageBox.information(
                    self,
                    APP_NAME,
                    "No processes were found for that selection.",
                )
            return None

        display = item.display_name() if hasattr(item, "display_name") else ""
        if not display:
            first_node = nodes[0]
            base_label = first_node.name or first_node.exe or f"PID {first_node.pid}"
            if len(nodes) > 1:
                display = f"{base_label} and {len(nodes) - 1} more"
            else:
                display = base_label

        primary = (
            item.node
            if getattr(item, "node", None)
            else max(nodes, key=lambda n: (n.aggregate_rss_mb, n.rss_mb, -n.pid))
        )

        return pids, display, primary, nodes

    def _resolve_process_path(self, primary: ProcessNode, nodes: List[ProcessNode]) -> str:
        """Return the most relevant executable path for a selection."""

        if getattr(primary, "exe", ""):
            return primary.exe
        for node in nodes:
            if getattr(node, "exe", ""):
                return node.exe
        return ""

    def _format_interval(self, seconds: float) -> str:
        """Format ``seconds`` for human-friendly status messages."""

        rounded = round(float(seconds), 2)
        if abs(rounded - round(rounded)) < 1e-6:
            return str(int(round(rounded)))
        text = f"{rounded:.1f}"
        return text.rstrip("0").rstrip(".")

    def _current_process_interval(self, key: Tuple[int, ...]) -> Optional[float]:
        """Return the active interval for ``key`` if one is configured."""

        task = self._process_gobble_tasks.get(key)
        if not task:
            return None
        value = task.get("interval")
        if isinstance(value, (int, float)):
            return max(0.5, float(value))
        return None

    def _prompt_process_interval(self, current: Optional[float] = None) -> Optional[float]:
        """Show a dialog requesting a gobble interval in seconds."""

        dialog = QDialog(self)
        dialog.setWindowTitle("Process Gobble Interval")
        layout = QVBoxLayout(dialog)
        label = QLabel("Gobble this process every (seconds):", dialog)
        layout.addWidget(label)
        editor = IntervalEditor(
            current if current is not None else DEFAULT_INTERVAL,
            minimum=0.5,
            maximum=600.0,
            step=0.5,
        )
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            return float(editor.value())
        return None

    def _perform_manual_gobble(
        self, pids: List[int], stop_event: Optional[threading.Event] = None
    ) -> Tuple[int, int, float, bool]:
        """Gobble ``pids`` and return processed/skipped/MB/cancelled."""

        reclaimed = 0.0
        processed = 0
        skipped = 0
        cancelled = False

        for pid in pids:
            if stop_event and stop_event.is_set():
                cancelled = True
                break
            try:
                if self.config.protect_system and is_system_process(psutil.Process(pid)):
                    skipped += 1
                    continue
            except Exception:
                pass
            before = rss_mb(pid)
            ok = empty_working_set(pid)
            after = rss_mb(pid)
            if ok:
                processed += 1
            if before is not None and after is not None:
                reclaimed += max(0.0, before - after)

        if stop_event and stop_event.is_set():
            cancelled = True

        return processed, skipped, reclaimed, cancelled

    def _handle_manual_gobble_result(
        self,
        display: str,
        processed: int,
        skipped: int,
        reclaimed: float,
        *,
        aborted: bool = False,
        update_totals: bool = True,
    ) -> None:
        """Update UI elements after a manual gobble completes."""

        if aborted:
            details: List[str] = []
            if processed > 0:
                plural = "processes" if processed != 1 else "process"
                details.append(f"{processed} {plural}")
            if reclaimed > 0 and update_totals:
                self.session_reclaimed_total += reclaimed
                self._update_total_label()
                details.append(f"−{reclaimed:.1f} MB")
            message = f"Stopped gobbling {display}"
            if details:
                message += f" ({', '.join(details)})"
            if skipped:
                message += f" ({skipped} protected skipped)"
            self.statusBar().showMessage(message, 3000)
            return

        if reclaimed > 0 and update_totals:
            self.session_reclaimed_total += reclaimed
            self._update_total_label()

        if processed == 0:
            if skipped:
                QMessageBox.information(
                    self,
                    APP_NAME,
                    "All processes in this group are protected and were skipped.",
                )
            else:
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    "Could not gobble any process in this group. Try running as Administrator.",
                )
            return

        plural = "processes" if processed > 1 else "process"
        msg = f"Gobbled {display} ({processed} {plural}) −{reclaimed:.1f} MB"
        if skipped:
            msg += f" ({skipped} protected skipped)"
        self.statusBar().showMessage(msg, 3000)

    def _gobble_selected(self):
        """Immediately gobble the highlighted process."""

        selection = self.table.selectionModel().selectedRows()
        if not selection:
            QMessageBox.information(self, APP_NAME, "Select a process row first.")
            return
        proxy_index = selection[0]
        src_index = self.proxy.mapToSource(proxy_index)
        item = self.proc_model.node_for_index(src_index)
        summary = self._process_item_summary(item, warn=True)
        if not summary:
            return
        pids, display, _primary, _nodes = summary
        processed, skipped, reclaimed, _cancelled = self._perform_manual_gobble(pids)
        self._handle_manual_gobble_result(display, processed, skipped, reclaimed)

    def _show_process_context_menu(self, pos) -> None:
        """Display a context menu for process tree items."""

        index_at = getattr(self.table, "indexAt", None)
        if index_at is None:
            return
        index = index_at(pos)
        if not index.isValid():
            return
        src_index = self.proxy.mapToSource(index)
        item = self.proc_model.node_for_index(src_index)
        summary = self._process_item_summary(item, warn=False)
        if not summary:
            return
        pids, display, primary, nodes = summary
        if not pids:
            return
        menu = QMenu(self)
        act_add_target = menu.addAction("Add Process as Target")
        path = self._resolve_process_path(primary, nodes)
        act_copy = None
        if path:
            menu.addSeparator()
            act_copy = menu.addAction("Copy File Path")
        exec_fn = getattr(menu, "exec", None) or getattr(menu, "exec_", None)
        viewport = getattr(self.table, "viewport", None)
        global_pos = viewport().mapToGlobal(pos) if callable(viewport) else self.mapToGlobal(pos)
        chosen = exec_fn(global_pos) if exec_fn else None
        if chosen == act_add_target:
            self._add_process_item_as_target(item)
        elif act_copy is not None and chosen == act_copy and path:
            self._copy_process_path(path, display)

    def _start_process_gobble(self, item, *, interval: Optional[float] = None) -> None:
        """Kick off a background gobble for ``item``."""

        summary = self._process_item_summary(item, warn=True)
        if not summary:
            return
        pids, display, _primary, _nodes = summary
        if not pids:
            return
        key = tuple(sorted(pids))
        existing = self._process_gobble_tasks.get(key)
        if existing and interval is not None:
            existing["interval"] = max(0.5, float(interval))
            formatted = self._format_interval(interval)
            display_name = str(existing.get("display") or display)
            self.statusBar().showMessage(
                f"Updated interval for {display_name} to {formatted} seconds",
                3000,
            )
            return
        if existing:
            return

        stop_event = threading.Event()
        task: Dict[str, object] = {"event": stop_event, "display": display}
        if interval is not None:
            task["interval"] = max(0.5, float(interval))
        self._process_gobble_tasks[key] = task

        def worker():
            processed_total = skipped_total = 0
            reclaimed_total = 0.0
            aborted = False
            try:
                while not stop_event.is_set():
                    processed, skipped, reclaimed, cancelled = self._perform_manual_gobble(
                        pids, stop_event
                    )
                    processed_total += processed
                    skipped_total += skipped
                    reclaimed_total += reclaimed
                    if cancelled and stop_event.is_set():
                        aborted = True
                        break

                    def deliver(p=processed, s=skipped, r=reclaimed, name=display):
                        self._handle_manual_gobble_result(name, p, s, r)

                    QTimer.singleShot(0, deliver)

                    wait_interval = self._current_process_interval(key)
                    if wait_interval is None:
                        break
                    if stop_event.wait(wait_interval):
                        aborted = True
                        break
            finally:
                def finish():
                    self._process_gobble_tasks.pop(key, None)
                    if aborted:
                        self._handle_manual_gobble_result(
                            display,
                            processed_total,
                            skipped_total,
                            reclaimed_total,
                            aborted=True,
                            update_totals=False,
                        )

                QTimer.singleShot(0, finish)

        threading.Thread(target=worker, daemon=True).start()
        if interval is not None:
            formatted = self._format_interval(interval)
            self.statusBar().showMessage(
                f"Gobbling {display} every {formatted} seconds", 3000
            )

    def _stop_process_gobble(self, key: Tuple[int, ...]) -> None:
        """Request cancellation of an in-flight manual gobble."""

        task = self._process_gobble_tasks.get(key)
        if not task:
            return
        event = task.get("event")
        if isinstance(event, threading.Event):
            event.set()

    def _copy_process_path(self, path: str, display: str) -> None:
        """Copy ``path`` to the clipboard and notify the user."""

        if not path:
            QMessageBox.information(
                self,
                APP_NAME,
                "That process does not expose a file path to copy.",
            )
            return

        getter = getattr(QApplication, "clipboard", None)
        clipboard = None
        if callable(getter):
            try:
                clipboard = getter()
            except Exception:
                clipboard = None
        copied = False
        if clipboard is not None and hasattr(clipboard, "setText"):
            try:
                clipboard.setText(path)
                copied = True
            except Exception:
                copied = False
        if copied:
            self.statusBar().showMessage(
                f"Copied path for {display} to the clipboard",
                3000,
            )
        else:
            QMessageBox.information(
                self,
                APP_NAME,
                "Unable to access the clipboard on this platform.",
            )

    def _gobble_visible_targets(self):
        """Manually gobble all targets that currently match their rules."""

        total_pids = 0
        gobbled_ok = 0
        total_delta = 0.0
        seen_pids: Set[int] = set()
        for target in self.config.targets:
            if self.current_group and target.group_id != self.current_group:
                continue
            raw_pids = match_pids(target)
            pids = []
            for pid in raw_pids:
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
                pids.append(pid)
            total_pids += len(pids)
            delta_t = 0.0
            if pids:
                target.last_gobble_reason = "manual"
                target.last_skip_reason = ""
            for pid in pids:
                try:
                    if self.config.protect_system and is_system_process(psutil.Process(pid)):
                        continue
                except Exception:
                    continue
                before = rss_mb(pid)
                ok = empty_working_set(pid)
                after = rss_mb(pid)
                if ok:
                    gobbled_ok += 1
                if before is not None and after is not None:
                    delta = max(0.0, before - after)
                    total_delta += delta
                    delta_t += delta
            if delta_t > 0:
                target.session_reclaimed_mb += delta_t
                target.day_reclaimed_mb += delta_t
                target.last_delta_mb = delta_t
                target.history.append(delta_t)
                target.history = target.history[-40:]
                ts = time.time()
                target._events.append({"ts": ts, "delta_mb": delta_t, "reason": "manual", "ok": True})
                self.metrics.add_event(ts, delta_t, "manual", target.strategy)
        self.session_reclaimed_total += total_delta
        self._update_total_label()
        scope = self._group_name(self.current_group) if self.current_group else "visible targets"
        msg = f"Gobbled {gobbled_ok}/{total_pids} processes (−{total_delta:.1f} MB) for {scope}"
        self.statusBar().showMessage(msg, 4000)
        if getattr(self, "tray", None):
            self.tray.showMessage(APP_NAME, msg)
        if self.stack.currentWidget() == self.dashboard:
            self.dashboard.refresh(self.session_reclaimed_total)

    def _update_total_label(self):
        """Update the UI label tracking reclaimed RAM this session."""

        self.lbl_total.setText(f"Session reclaimed: {self.session_reclaimed_total:.1f} MB")

    def _open_group_settings(self):
        """Open the group management dialog."""

        dialog = GroupSettingsDialog(self)
        dialog.exec()
        gid = self.current_group
        if gid and not any(group.id == gid for group in self.config.groups):
            gid = ""
        self._rebuild_group_menu()
        self._on_group_selected(gid)

    def _open_settings(self):
        """Open the settings dialog and persist any accepted changes."""

        prev_theme = self.config.theme
        prev_tray_mode = self.config.tray_mode
        prev_startup = self.config.startup_enabled
        prev_show_system = self.config.show_system
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            dialog.apply()
            theme_changed = self.config.theme != prev_theme
            tray_changed = self.config.tray_mode != prev_tray_mode
            startup_changed = self.config.startup_enabled != prev_startup
            show_system_changed = self.config.show_system != prev_show_system

            if theme_changed:
                self._apply_theme_choice(
                    self.config.theme, persist=False, previous=prev_theme
                )
            if tray_changed:
                self._apply_tray_mode(
                    self.config.tray_mode, persist=False, previous=prev_tray_mode
                )

            startup_msg_shown = False
            if startup_changed:
                startup_msg_shown = self._apply_startup_preference(
                    self.config.startup_enabled, persist=False, previous=prev_startup
                )

            if show_system_changed:
                self._refresh_show_system_filter(self.config.show_system)
            self._apply_hotkey(self.config.hotkey)
            self.config.save()
            if not startup_msg_shown:
                self.statusBar().showMessage("Settings saved", 3000)

    def _apply_hotkey(self, hotkey_str: str):
        """Register a global hotkey from the configuration string."""

        self._unregister_hotkey()
        if not hotkey_str:
            return
        mods, vk = parse_hotkey_string(hotkey_str)
        if not vk:
            QMessageBox.warning(
                self,
                APP_NAME,
                f"Can't use hotkey '{hotkey_str}'. Try letters/digits or F-keys with modifiers.",
            )
            return
        hwnd = int(self.winId())
        ok = bool(RegisterHotKey(hwnd, HOTKEY_ID, mods, vk))
        if not ok:
            QMessageBox.warning(
                self,
                APP_NAME,
                f"Couldn't register hotkey '{hotkey_str}'. It may be in use.",
            )
            return
        self._hotkey_registered = True

    def _unregister_hotkey(self):
        """Release the previously registered global hotkey."""

        if self._hotkey_registered:
            try:
                UnregisterHotKey(int(self.winId()), HOTKEY_ID)
            except Exception:
                pass
            self._hotkey_registered = False

    def _on_hotkey(self, hotkey_id):
        """Invoke manual gobbling when the global hotkey fires."""

        if int(hotkey_id) == HOTKEY_ID:
            self._gobble_visible_targets()

    def _on_resume(self):
        """Kick off a gobble shortly after the machine resumes from sleep."""

        if self.config.gobble_on_wake:
            QTimer.singleShot(1500, self._gobble_visible_targets)

    def _on_gobble_result(self, index: int, message: str):
        """Update the UI with the latest status string from the worker."""

        def update():
            self.targets_table.update_status(index, message)

        QTimer.singleShot(0, update)

    def _on_delta(self, delta_mb: float):
        """Accumulate reclaimed memory totals and refresh dashboards."""

        if delta_mb > 0:
            self.session_reclaimed_total += delta_mb
            self._update_total_label()
            if self.stack.currentWidget() == self.dashboard:
                self.dashboard.refresh(self.session_reclaimed_total)

    def _autostart_targets(self):
        """Activate targets marked with the auto-start flag."""

        for index, target in enumerate(self.config.targets):
            if target.autostart:
                self.gobbler.set_active(index, True)
        self.targets_table.refresh(force=True)

    def closeEvent(self, event):
        """Handle window closing, optionally minimising to the tray."""

        if self.config.tray_mode:
            event.ignore()
            self.hide()
            if getattr(self, "tray", None):
                self.tray.showMessage(APP_NAME, "Still running in the system tray.")
            return
        self._unregister_hotkey()
        try:
            self.gobbler.stop()
        except Exception:
            pass
        try:
            self.metrics.save()
        except Exception:
            pass
        return super().closeEvent(event)


__all__ = ["MainWindow"]
