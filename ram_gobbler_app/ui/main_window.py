"""Main application window implementation."""
from __future__ import annotations

import json
import shutil
import time
from typing import List, Optional, Set, Tuple

from ..constants import APP_NAME, CONFIG_PATH, METRICS_PATH
from ..deps import psutil
from ..gobbler import Gobbler
from ..helpers import empty_working_set, rss_mb
from ..hotkeys import WinEventFilter, parse_hotkey_string
from ..models import Config, Target
from ..platform import (
    QAction,
    QApplication,
    QCheckBox,
    QDialog,
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
        box_left = QGroupBox("Running Processes")
        lv.addWidget(box_left)
        bl = QVBoxLayout(box_left)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by name or path…")
        self.btn_gobble_now = QPushButton("Gobble Now")
        self.btn_gobble_now.setToolTip("Gobble the selected process immediately")
        self.btn_gobble_now.clicked.connect(self._gobble_selected)
        self.btn_add_selected = QPushButton("Add Selected as Target")
        self.btn_add_selected.clicked.connect(self._add_selected)
        self.cb_show_system = QCheckBox("Show system processes (⚠️)")
        self.cb_show_system.setChecked(self.config.show_system)
        self.cb_show_system.stateChanged.connect(self._toggle_show_system)
        search_row.addWidget(self.search)
        search_row.addWidget(self.btn_gobble_now)
        search_row.addWidget(self.btn_add_selected)
        search_row.addWidget(self.cb_show_system)
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
            header.setSectionResizeMode(0, QHeaderView.Stretch)
            for col in range(1, self.proc_model.columnCount()):
                header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        except Exception:
            self.table.header().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        bl.addWidget(self.table)
        self._process_interaction = _InteractionWatcher(self.table)
        self._process_interaction.attach(self.table)
        self._process_interaction.attach(self.table.viewport())
        self._process_interaction.attach(self.table.verticalScrollBar())
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
        box_right = QGroupBox("Targets")
        rv.addWidget(box_right)
        br = QVBoxLayout(box_right)
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

    def _toggle_show_system(self, _):
        """Refresh the process table when the system filter changes."""

        pid = self._current_pid()
        bar = self.table.verticalScrollBar()
        scroll = bar.value() if bar else 0
        self.config.show_system = bool(self.cb_show_system.isChecked())
        self.config.save()

        def done():
            self._restore_expanded()
            self._select_pid(pid)
            self._restore_scrollbar(bar, scroll)

        self.proc_model.set_include_system(self.config.show_system, done)

    def _on_group_selected(self, gid: str):
        """Filter the targets table to the newly selected group."""

        self.current_group = gid
        self.targets_table.set_group_filter(gid)
        self._update_toolbar_scope()

    def _group_name(self, gid: str) -> str:
        """Return a displayable name for ``gid``."""

        if not gid:
            return "Ungrouped"
        for group in self.config.groups:
            if group.id == gid:
                return group.name
        return gid

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
                """
            )
        else:
            self.setStyleSheet(
                """
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
                self.gobbler.set_active(index, True)
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
            self.config.load()
            self.proc_model.set_include_system(self.config.show_system)
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
            self.cb_show_system.setChecked(self.config.show_system)
            self._apply_hotkey(self.config.hotkey)
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
        if not item or not item.node:
            return
        node = item.node
        try:
            if is_system_process(psutil.Process(node.pid)) and self.config.protect_system:
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    "This looks like a system process. It's hidden/protected by default.",
                )
                return
        except Exception:
            pass
        subtree_nodes = list(item.iter_nodes())
        if not subtree_nodes:
            return
        seen = {(t.key.casefold(), t.match_by) for t in self.config.targets}
        additions: List[Tuple[ProcessNode, str, str]] = []
        for candidate in subtree_nodes:
            if self.config.protect_system:
                try:
                    if is_system_process(psutil.Process(candidate.pid)):
                        continue
                except Exception:
                    pass
            key = candidate.exe or candidate.name or ""
            if not key:
                continue
            match_by = "path" if candidate.exe else "name"
            canonical = (key.casefold(), match_by)
            if canonical in seen:
                continue
            additions.append((candidate, key, match_by))
            seen.add(canonical)
        if not additions:
            QMessageBox.information(
                self,
                APP_NAME,
                "Every process in this group is already configured as a target.",
            )
            return
        primary = None
        for entry in additions:
            if entry[0].pid == node.pid:
                primary = entry
                break
        if primary is None:
            primary = additions[0]
        primary_node = primary[0]
        summary_lines = [
            f"{(n.name or n.exe or f'PID {n.pid}')} (PID {n.pid})"
            for n, _key, _match in additions
            if n.pid != primary_node.pid
        ]
        dialog = AddTargetDialog(
            primary[1],
            primary[2],
            self,
            summary=summary_lines or None,
            include_children=bool(primary_node.children),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        key, match_by, interval, autostart, include_children = dialog.get_values()
        if not key:
            return
        additions_to_create: List[Tuple[ProcessNode, str, str, bool]] = []
        for candidate, default_key, default_match in additions:
            if candidate.pid == primary_node.pid:
                additions_to_create.append(
                    (candidate, key, match_by, bool(include_children or candidate.children))
                )
            else:
                additions_to_create.append(
                    (candidate, default_key, default_match, bool(candidate.children))
                )
        existing = {(t.key.casefold(), t.match_by) for t in self.config.targets}
        new_indices: List[int] = []
        added_count = 0
        for candidate, key_val, match_val, include_flag in additions_to_create:
            canonical = (key_val.casefold(), match_val)
            if canonical in existing or not key_val:
                continue
            target = Target(
                key=key_val,
                match_by=match_val,
                interval_sec=interval,
                autostart=autostart,
                include_children=include_flag,
            )
            self.config.targets.append(target)
            new_index = len(self.config.targets) - 1
            new_indices.append(new_index)
            existing.add(canonical)
            added_count += 1
        if added_count == 0:
            QMessageBox.information(self, APP_NAME, "No new targets were added.")
            return
        self.config.save()
        self.targets_table.refresh(force=True)
        if autostart:
            for idx in new_indices:
                self.gobbler.set_active(idx, True)
        label = primary_node.name or primary_node.exe or f"PID {primary_node.pid}"
        plural = "targets" if added_count > 1 else "target"
        self.statusBar().showMessage(
            f"Added {added_count} {plural} for {label}",
            3000,
        )

    def _gobble_selected(self):
        """Immediately gobble the highlighted process."""

        selection = self.table.selectionModel().selectedRows()
        if not selection:
            QMessageBox.information(self, APP_NAME, "Select a process row first.")
            return
        proxy_index = selection[0]
        src_index = self.proxy.mapToSource(proxy_index)
        item = self.proc_model.node_for_index(src_index)
        if not item or not item.node:
            return
        node = item.node
        pids = []
        seen: Set[int] = set()
        for pid in item.all_pids():
            if pid not in seen:
                seen.add(pid)
                pids.append(pid)
        if not pids:
            return
        display = item.display_name()
        reclaimed = 0.0
        processed = 0
        skipped = 0
        for pid in pids:
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
        if reclaimed > 0:
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

    def _open_settings(self):
        """Open the settings dialog and persist any accepted changes."""

        prev_theme = self.config.theme
        prev_tray_mode = self.config.tray_mode
        prev_startup = self.config.startup_enabled
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            dialog.apply()
            theme_changed = self.config.theme != prev_theme
            tray_changed = self.config.tray_mode != prev_tray_mode
            startup_changed = self.config.startup_enabled != prev_startup

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

            self.proc_model.set_include_system(self.config.show_system)
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
