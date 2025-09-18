"""Qt widgets and models used by the Ram Gobbler interface.

This module houses the majority of the user-facing widgets.  The classes are
deliberately verbose – both in implementation and documentation – so that
contributors with only basic Qt knowledge can understand how the dashboard,
tables and dialogs interact with the background gobbler thread.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from ..constants import DEFAULT_INTERVAL
from ..helpers import empty_working_set, rss_mb
from ..models import Config, Group, Target
from ..processes import estimate_reclaim, list_processes, match_pids
from ..system import is_system_process
from ..platform import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QModelIndex,
    QMenu,
    QObject,
    QPainter,
    QPen,
    QLineEdit,
    QKeySequenceEdit,
    QSpinBox,
    QStyle,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
    Qt,
    QAbstractItemView,
    QAbstractTableModel,
    QSortFilterProxyModel,
    QTimer,
    QStackedWidget,
    QEvent,
    QPushButton,
    Signal,
)

from groups import group_totals
from metrics import Metrics


class ProcessTableModel(QAbstractTableModel):
    """Asynchronous model listing processes for the selection table."""

    COLS = ["Name", "PID", "Memory (MB)", "Path"]
    rows_ready = Signal(object, object)

    def __init__(self, include_system: bool):
        """Initialise the model and kick off the first refresh."""

        super().__init__()
        self.include_system = include_system
        self._rows: List[Tuple[str, int, float, str]] = []
        self._refreshing = False
        self._lock = threading.Lock()
        self._pending_refresh = False
        self._pending_callbacks: List[Callable[[], None]] = []
        if hasattr(self, "rows_ready") and callable(getattr(self.rows_ready, "connect", None)):
            try:
                self.rows_ready.connect(self._apply_rows)
            except Exception:
                pass
        self.refresh()

    def set_include_system(self, val: bool, done=None):
        """Update the system-process filter and refresh the model."""

        self.include_system = val
        self.refresh(done)

    def refresh(self, done=None):
        """Trigger a background refresh of the process list."""

        callback = done if callable(done) else None
        with self._lock:
            if self._refreshing:
                if callback:
                    self._pending_callbacks.append(callback)
                self._pending_refresh = True
                return
            self._refreshing = True
        self._start_worker(callback)

    def _start_worker(self, callback: Optional[Callable[[], None]]):
        """Spawn a daemon thread to gather process data."""

        def worker():
            rows: List[Tuple[str, int, float, str]] = []
            try:
                procs = list_processes(self.include_system)
                for proc in procs:
                    name = proc.info.get("name") or ""
                    pid = proc.pid
                    rss = (proc.info.get("memory_info").rss if proc.info.get("memory_info") else 0) / (1024 * 1024)
                    exe = proc.info.get("exe") or ""
                    rows.append((name, pid, rss, exe))
            except Exception:
                rows = []
            rows.sort(key=lambda r: (-r[2], r[0].lower()))

            try:
                if hasattr(self, "rows_ready"):
                    self.rows_ready.emit(rows, callback)  # type: ignore[attr-defined]
                else:
                    self._apply_rows(rows, callback)
            except Exception:
                self._apply_rows(rows, callback)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_rows(self, rows: List[Tuple[str, int, float, str]], callback: Optional[Callable[[], None]]):
        """Swap in freshly collected rows and invoke pending callbacks."""

        callbacks: List[Callable[[], None]] = []
        if callback:
            callbacks.append(callback)
        with self._lock:
            callbacks.extend(self._pending_callbacks)
            self._pending_callbacks.clear()
            rerun = self._pending_refresh
            self._pending_refresh = False
            self._refreshing = False

        if rows != self._rows:
            self.beginResetModel()
            self._rows = rows
            self.endResetModel()
        else:
            self._rows = rows

        for cb in callbacks:
            try:
                cb()
            except Exception:
                pass

        if rerun:
            self.refresh()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        """Return the number of cached processes."""

        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        """Return the number of visible columns."""

        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802 - Qt API
        """Provide header labels for the Qt view."""

        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.COLS[section]
        return section + 1

    def data(self, index, role=Qt.DisplayRole):  # noqa: N802 - Qt API
        """Return cell data for the table view."""

        if not index.isValid():
            return None
        name, pid, rss, exe = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return name
            if col == 1:
                return str(pid)
            if col == 2:
                return f"{rss:,.1f}"
            if col == 3:
                return exe
        return None

    def row_at(self, row_index: int) -> Tuple[str, int, float, str]:
        """Convenience accessor for the row at ``row_index``."""

        return self._rows[row_index]


class Sparkline(QWidget):
    """Tiny widget drawing a sparkline of recent reclaimed values."""

    def __init__(self, values: List[float], parent=None):
        """Remember the recent history and set up a sensible baseline size."""

        super().__init__(parent)
        self.values = values[-40:]
        self.setMinimumHeight(24)

    def set_values(self, values: List[float]):
        """Update the data series and request a repaint."""

        self.values = values[-40:]
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API
        """Render the sparkline using simple line segments."""

        painter = QPainter(self)
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen())
        values = self.values or [0]
        vmax = max(values) or 1.0
        n = len(values)
        if n == 1:
            y = rect.bottom() - (values[0] / vmax) * rect.height()
            painter.drawLine(rect.left(), y, rect.right(), y)
            return
        step = rect.width() / max(1, n - 1)
        points = []
        for idx, value in enumerate(values):
            x = rect.left() + idx * step
            y = rect.bottom() - (value / vmax) * rect.height()
            points.append((x, y))
        for i in range(len(points) - 1):
            painter.drawLine(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])


class GroupsSidebar(QWidget):
    """Sidebar widget listing user-created groups."""

    def __init__(self, config: Config, parent=None, controller=None):
        """Build the list/toolbar UI and remember the surrounding objects."""

        super().__init__(parent)
        self.config = config
        self._controller = controller
        layout = QVBoxLayout(self)
        self.toolbar = QToolBar()
        self.toolbar.setIconSize(self.toolbar.iconSize())
        layout.addWidget(self.toolbar)
        self.list = QListWidget()
        layout.addWidget(self.list)
        self.toolbar.addAction("Add", self._add)
        self.toolbar.addAction("Rename", self._rename)
        self.toolbar.addAction("Delete", self._delete)
        self.list.currentRowChanged.connect(self._emit_group)
        self.refresh()
        self._suspend_emit = False
        self._current_group_id = ""

    def refresh(self):
        """Populate the list widget with current groups."""

        self.list.clear()
        self.list.addItem("All Targets")
        for grp in self.config.groups:
            label = grp.name
            if grp.color:
                label = f"{grp.name} ({grp.color})"
            self.list.addItem(label)
        if self._current_group_id:
            for idx, grp in enumerate(self.config.groups, start=1):
                if grp.id == self._current_group_id:
                    self.list.setCurrentRow(idx)
                    break
        else:
            self.list.setCurrentRow(0)

    def set_current_group(self, group_id: str, suppress_emit: bool = False):
        """Programmatically select ``group_id`` in the list."""

        self._current_group_id = group_id
        self._suspend_emit = suppress_emit
        self.refresh()
        if not suppress_emit:
            self._emit_group(self.list.currentRow())
        self._suspend_emit = False

    def current_group_id(self) -> str:
        """Return the identifier of the currently highlighted group."""

        row = self.list.currentRow()
        if row <= 0:
            return ""
        return self.config.groups[row - 1].id

    def _emit_group(self, row: int):
        """Notify the controller that a new group has been selected."""

        if row < 0:
            return
        gid = "" if row == 0 else self.config.groups[row - 1].id
        self._current_group_id = gid
        if self._suspend_emit:
            return
        if self._controller and hasattr(self._controller, "_on_group_selected"):
            self._controller._on_group_selected(gid)

    def _controller_call(self, name: str):
        """Invoke ``name`` on the controller when available."""

        if not self._controller:
            return
        func = getattr(self._controller, name, None)
        if callable(func):
            func()

    def _add(self):
        """Prompt the user for a new group and append it to the config."""

        name, ok = QInputDialog.getText(self, "Add Group", "Group name:")
        if not ok or not name:
            return
        group_id = uuid.uuid4().hex[:8]
        self.config.groups.append(Group(id=group_id, name=name))
        self.config.save()
        self._current_group_id = group_id
        self.refresh()
        if self._controller and hasattr(self._controller, "targets_table"):
            self._controller.targets_table.refresh(force=True)

    def _rename(self):
        """Allow the user to rename the selected group."""

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
        if self._controller and hasattr(self._controller, "targets_table"):
            self._controller.targets_table.refresh(force=True)

    def _delete(self):
        """Remove the selected group and reassign its targets."""

        row = self.list.currentRow()
        if row <= 0:
            return
        grp = self.config.groups.pop(row - 1)
        for target in self.config.targets:
            if target.group_id == grp.id:
                target.group_id = ""
        self.config.save()
        if self._current_group_id == grp.id:
            self._current_group_id = ""
        self.refresh()
        if self._controller and hasattr(self._controller, "targets_table"):
            self._controller.targets_table.refresh(force=True)


class GroupSettingsDialog(QDialog):
    """Simple wrapper dialog embedding :class:`GroupsSidebar`."""

    def __init__(self, main_window):
        """Embed the live sidebar so users can manage groups in-place."""

        super().__init__(main_window)
        self.setWindowTitle("Group Settings")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        self.sidebar = GroupsSidebar(main_window.config, main_window, self)
        self.sidebar.set_current_group(main_window.current_group, suppress_emit=True)
        layout.addWidget(self.sidebar)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class RulesDialog(QDialog):
    """Dialog for editing advanced gobbling rules per target."""

    def __init__(self, target: Target, parent=None):
        """Lay out the form controls for the provided ``target`` instance."""

        super().__init__(parent)
        self.setWindowTitle("Edit Target Rules")
        self.setMinimumWidth(420)
        self._orig = target
        form = QFormLayout(self)

        self.sb_cpu = QSpinBox()
        self.sb_cpu.setRange(0, 100)
        self.sb_cpu.setValue(int(target.cpu_skip_threshold))
        self.cb_adaptive = QCheckBox("Adaptive interval")
        self.cb_adaptive.setChecked(bool(target.adaptive))
        self.cb_bg = QCheckBox("Gobble only when target is in background")
        self.cb_bg.setChecked(bool(target.background_only))
        self.cb_full = QCheckBox("Pause while any app is fullscreen")
        self.cb_full.setChecked(bool(target.pause_if_fullscreen))

        self.cb_strategy = QComboBox()
        self.cb_strategy.addItems(["standard", "reset"])
        self.cb_strategy.setCurrentText(target.strategy)

        form.addRow("CPU skip threshold (%):", self.sb_cpu)
        form.addRow("Interval strategy:", self.cb_adaptive)
        form.addRow("Background-only:", self.cb_bg)
        form.addRow("Pause if fullscreen:", self.cb_full)
        form.addRow("Gobble method:", self.cb_strategy)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def apply(self) -> Target:
        """Persist values from the dialog widgets back onto the target."""

        target = self._orig
        target.cpu_skip_threshold = int(self.sb_cpu.value())
        target.adaptive = bool(self.cb_adaptive.isChecked())
        target.background_only = bool(self.cb_bg.isChecked())
        target.pause_if_fullscreen = bool(self.cb_full.isChecked())
        target.strategy = self.cb_strategy.currentText()
        return target


class _InteractionWatcher(QObject):
    """Small helper tracking whether the user recently interacted with a view."""

    def __init__(self, parent=None):
        """Initialise with a clean state so tables know they are idle."""

        super().__init__(parent)
        self.active = False

    def attach(self, widget):
        """Install the watcher as an event filter on ``widget``."""

        if widget is None:
            return
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API
        """Mark the watcher active when pointer events occur."""

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
        """Delay a reset so quick drag operations do not flip the flag."""

        if hasattr(QTimer, "singleShot"):
            QTimer.singleShot(120, self._clear_if_idle)
        else:
            self._clear_if_idle()

    def _clear_if_idle(self):
        """Reset ``active`` when no mouse buttons are pressed anymore."""

        try:
            if not QApplication.mouseButtons():
                self.active = False
        except Exception:
            self.active = False


class TargetsTable(QTableWidget):
    """Central table listing configured targets and their status."""

    HEADERS = [
        "Group",
        "Target",
        "Match",
        "Auto-start",
        "Status",
        "Last Δ (MB)",
        "Totals MB (Sess/Day)",
        "History",
        "Interval (s)",
        "Actions",
    ]

    def __init__(self, config: Config, gobbler):
        """Create the table and wire up deferred refresh helpers."""

        super().__init__(0, len(self.HEADERS))
        self.config = config
        self.gobbler = gobbler
        self.group_filter = ""
        self.uid_to_row: Dict[str, int] = {}
        self._row_targets: List[Optional[str]] = []
        self._row_config_indexes: List[Optional[int]] = []
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
        QTimer.singleShot(0, self.resizeColumnsToContents)

    def set_group_filter(self, gid: str):
        """Show only rows belonging to the specified group id."""

        self.group_filter = gid
        self.refresh(force=True)

    def _group_name(self, gid: str) -> str:
        """Look up a display name for ``gid`` falling back to the id itself."""

        if not gid:
            return "Ungrouped"
        for grp in self.config.groups:
            if grp.id == gid:
                return grp.name
        return gid

    def refresh(self, force: bool = False):
        """Rebuild the table rows, preserving scroll position and selection."""

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
            self._row_config_indexes = []
            if self.group_filter:
                for idx, target in enumerate(self.config.targets):
                    if target.group_id == self.group_filter:
                        self._add_row(idx, target)
            else:
                totals = group_totals(self.config.targets)
                if totals:
                    for group_id, total in totals.items():
                        history = []
                        for target in self.config.targets:
                            if target.group_id == group_id:
                                history.extend(target.history)
                        history = history[-40:]
                        self._add_group_header(self._group_name(group_id), total, history)
                for idx, target in enumerate(self.config.targets):
                    self._add_row(idx, target)
        finally:
            self.setUpdatesEnabled(True)

        self._restore_state(scroll_val, selected_uids)

    def _capture_state(self) -> Tuple[int, List[str]]:
        """Remember scroll position and selected targets."""

        bar = self.verticalScrollBar()
        scroll_val = bar.value() if bar else 0
        selected = []
        if self.selectionModel():
            for index in self.selectionModel().selectedRows():
                uid = self._row_targets[index.row()]
                if uid:
                    selected.append(uid)
        return scroll_val, selected

    def _restore_state(self, scroll_val: int, selected_uids: List[str]):
        """Restore the captured state after repopulating the table."""

        bar = self.verticalScrollBar()
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
        """Expose the interaction status to callers."""

        bar = self.verticalScrollBar() if hasattr(self, "verticalScrollBar") else None
        return bool(bar and bar.isSliderDown()) or self._interaction.active

    def _add_group_header(self, name: str, total: float, history: List[float]):
        """Insert a non-selectable summary row for a group."""

        row = self.rowCount()
        self.insertRow(row)
        self._row_targets.append(None)
        self._row_config_indexes.append(None)
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

    def _add_row(self, idx: int, target: Target):
        """Insert a row representing ``target`` into the table."""

        row = self.rowCount()
        self.insertRow(row)
        uid = getattr(target, "uid", None) or uuid.uuid4().hex[:10]
        target.uid = uid
        self.uid_to_row[uid] = row
        self._row_targets.append(uid)
        self._row_config_indexes.append(idx)

        group_widget = self._create_group_widget(idx, target)
        target_item = QTableWidgetItem(target.key)
        match_item = QTableWidgetItem(target.match_by)
        status_text = "Gobbling" if target.active else "Idle"
        if target.last_result:
            status_text += f" — {target.last_result}"
        status_item = QTableWidgetItem(status_text)

        autostart_cb = QCheckBox()
        autostart_cb.setChecked(target.autostart)
        autostart_cb.stateChanged.connect(lambda _=None, i=idx, cb=autostart_cb: self._on_autostart(i, cb.isChecked()))

        last_delta_item = QTableWidgetItem(f"{target.last_delta_mb:.1f}")
        totals_item = QTableWidgetItem(f"{target.session_reclaimed_mb:.1f} / {target.day_reclaimed_mb:.1f}")

        spark = Sparkline(target.history)

        interval_widget = QSpinBox()
        interval_widget.setRange(3, 600)
        interval_widget.setValue(int(target.interval_sec))
        interval_widget.setToolTip("How often to gobble memory for this target")
        interval_widget.valueChanged.connect(lambda val, i=idx: self._on_interval(i, val))

        actions_widget = QWidget()
        hbox = QHBoxLayout(actions_widget)
        hbox.setContentsMargins(0, 0, 0, 0)
        btn_start = QPushButton("Gobble RAM")
        btn_stop = QPushButton("Stop Gobbling")
        btn_edit = QPushButton("Edit Rules…")
        btn_remove = QPushButton("Remove")
        btn_start.clicked.connect(lambda _=False, i=idx: self._on_start(i))
        btn_stop.clicked.connect(lambda _=False, i=idx: self._on_stop(i))
        btn_edit.clicked.connect(lambda _=False, i=idx: self._on_edit(i))
        btn_remove.clicked.connect(lambda _=False, i=idx: self._on_remove(i))
        for button in (btn_start, btn_stop, btn_edit, btn_remove):
            button.setMinimumHeight(28)
            hbox.addWidget(button)

        self.setCellWidget(row, 0, group_widget)
        self.setItem(row, 1, target_item)
        self.setItem(row, 2, match_item)
        self.setCellWidget(row, 3, autostart_cb)
        self.setItem(row, 4, status_item)
        self.setItem(row, 5, last_delta_item)
        self.setItem(row, 6, totals_item)
        self.setCellWidget(row, 7, spark)
        self.setCellWidget(row, 8, interval_widget)
        self.setCellWidget(row, 9, actions_widget)

        for widget in (
            group_widget,
            autostart_cb,
            interval_widget,
            actions_widget,
            btn_start,
            btn_stop,
            btn_edit,
            btn_remove,
        ):
            self._register_interaction(widget)

    def contextMenuEvent(self, event):  # noqa: N802 - Qt API
        """Provide a remove action when the user right-clicks a row."""

        if not hasattr(self, "rowAt"):
            return super().contextMenuEvent(event)
        row = self.rowAt(event.pos().y())
        if row < 0 or row >= len(self._row_config_indexes):
            return super().contextMenuEvent(event)
        config_index = self._row_config_indexes[row]
        if config_index is None:
            return super().contextMenuEvent(event)
        if self.selectionModel() and not self.selectionModel().isRowSelected(row):
            self.selectionModel().clearSelection()
            self.selectRow(row)
        menu = QMenu(self)
        remove_action = menu.addAction("Remove Target")
        exec_fn = getattr(menu, "exec", None) or getattr(menu, "exec_", None)
        chosen = exec_fn(self.mapToGlobal(event.pos())) if exec_fn else None
        if chosen == remove_action and config_index is not None:
            self._on_remove(config_index)
        else:
            super().contextMenuEvent(event)

    def _register_interaction(self, widget):
        """Record widgets whose interaction should pause auto-refresh."""

        if not widget:
            return
        wid = id(widget)
        if wid in self._tracked_widgets:
            return
        self._tracked_widgets.add(wid)
        if hasattr(self._interaction, "attach"):
            self._interaction.attach(widget)

    def _create_group_widget(self, idx: int, target: Target) -> QComboBox:
        """Build the drop-down used to assign a target to a group."""

        widget = QComboBox()
        if hasattr(widget, "setEditable"):
            widget.setEditable(False)
        if hasattr(widget, "setSizeAdjustPolicy") and hasattr(QComboBox, "AdjustToContents"):
            widget.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._populate_group_widget(widget, target.group_id)
        signal = getattr(widget, "currentIndexChanged", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda _=0, i=idx, w=widget: self._on_group_changed(i, w))
        if hasattr(widget, "setToolTip"):
            widget.setToolTip("Assign this target to a group")
        return widget

    def _populate_group_widget(self, widget: QComboBox, current_gid: str):
        """Fill the group drop-down with known groups."""

        if not hasattr(widget, "clear"):
            return
        block_signals = getattr(widget, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            widget.clear()
            add_item = getattr(widget, "addItem", None)
            if callable(add_item):
                add_item("Ungrouped", "")
                for grp in self.config.groups:
                    add_item(grp.name, grp.id)
            gid = current_gid or ""
            find_data = getattr(widget, "findData", None)
            index = find_data(gid) if callable(find_data) else -1
            if index < 0 and gid and callable(add_item):
                add_item(self._group_name(gid), gid)
                index = find_data(gid) if callable(find_data) else -1
            if index < 0:
                index = 0
            set_current = getattr(widget, "setCurrentIndex", None)
            if callable(set_current):
                set_current(index)
        finally:
            if callable(block_signals):
                block_signals(False)

    def _on_group_changed(self, index: int, widget: QComboBox):
        """Apply a new group assignment after user interaction."""

        current_index = widget.currentIndex() if hasattr(widget, "currentIndex") else 0
        item_data = widget.itemData(current_index) if hasattr(widget, "itemData") else ""
        gid = item_data or ""
        self.gobbler.set_group(index, gid)
        self.refresh(force=True)

    def _on_start(self, index: int):
        """Tell the gobbler thread to start processing a target."""

        self.gobbler.set_active(index, True)
        self.refresh(force=True)

    def _on_stop(self, index: int):
        """Stop gobbling for the selected target."""

        self.gobbler.set_active(index, False)
        self.refresh(force=True)

    def _on_edit(self, index: int):
        """Open the rules dialog for ``index``."""

        if 0 <= index < len(self.config.targets):
            target = self.config.targets[index]
            dlg = RulesDialog(target, self)
            if dlg.exec() == QDialog.Accepted:
                new_target = dlg.apply()
                self.gobbler.update_target(index, new_target)
                self.refresh(force=True)

    def _on_remove(self, index: int):
        """Remove a target from the configuration."""

        self.gobbler.remove_target(index)
        self.refresh(force=True)

    def _on_interval(self, index: int, val: int):
        """Persist interval changes from the spin box."""

        self.gobbler.set_interval(index, val)

    def _on_autostart(self, index: int, value: bool):
        """Update the auto-start flag from the checkbox state."""

        self.gobbler.set_autostart(index, value)

    def update_status(self, index: int, message: str):
        """Update the status cells for a given target index."""

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

    def _flush_pending_refresh(self):
        """Execute a deferred refresh after the user stops interacting."""

        force = self._pending_refresh_force
        self._pending_refresh_force = False
        self.refresh(force=force)


class AddTargetDialog(QDialog):
    """Dialog for adding a new target using a friendly form."""

    def __init__(self, key: str, match_by: str, parent=None):
        """Pre-fill the dialog with sensible defaults for ``key`` and mode."""

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

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

        self._update_preview()

    def _update_preview(self):
        """Show how many processes currently match the entered pattern."""

        key = self.le_key.text().strip()
        match_by = self.cmb_match.currentText()
        if not key:
            self.lbl_preview.setText("Matches: 0")
            return
        preview_target = Target(key=key, match_by=match_by)
        pids = match_pids(preview_target)
        self.lbl_preview.setText(f"Matches: {len(pids)}")

    def get_values(self) -> Tuple[str, str, int, bool]:
        """Return the form values in a convenient tuple."""

        return (
            self.le_key.text().strip(),
            self.cmb_match.currentText(),
            int(self.sb_interval.value()),
            bool(self.cb_autostart.isChecked()),
        )


class SettingsDialog(QDialog):
    """Global application settings editor."""

    def __init__(self, config: Config, parent=None):
        """Build the settings form and seed it from ``config``."""

        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self.cfg = config

        form = QFormLayout(self)
        self.kse = QKeySequenceEdit()
        if config.hotkey:
            self.kse.setKeySequence(config.hotkey)
        self.cb_wake = QCheckBox("Gobble on wake from sleep")
        self.cb_wake.setChecked(bool(config.gobble_on_wake))
        self.cb_show_system = QCheckBox("Show system processes")
        self.cb_show_system.setChecked(bool(config.show_system))
        self.cb_protect_system = QCheckBox("Protect system processes")
        self.cb_protect_system.setChecked(bool(config.protect_system))
        self.cb_pressure = QCheckBox("Enable pressure-aware gobbling")
        self.cb_pressure.setChecked(bool(config.pressure_enabled))
        self.sb_min_free = QSpinBox()
        self.sb_min_free.setRange(5, 80)
        self.sb_min_free.setValue(int(config.min_free_percent))
        self.cb_gaming = QCheckBox("Pause during fullscreen apps")
        self.cb_gaming.setChecked(bool(config.gaming_mode))
        self.cmb_batt = QComboBox()
        self.cmb_batt.addItems(["off", "slow", "pause"])
        self.cmb_batt.setCurrentText(config.battery_mode)
        self.sb_batt_factor = QSpinBox()
        self.sb_batt_factor.setRange(1, 4)
        self.sb_batt_factor.setValue(int(config.battery_slow_factor))

        form.addRow("Global hotkey:", self.kse)
        form.addRow("Wake behaviour:", self.cb_wake)
        form.addRow("Show system processes:", self.cb_show_system)
        form.addRow("Protect system processes:", self.cb_protect_system)
        form.addRow("Pressure-aware gobbling:", self.cb_pressure)
        form.addRow("Min free RAM %:", self.sb_min_free)
        form.addRow("Gaming mode:", self.cb_gaming)
        form.addRow("Battery mode:", self.cmb_batt)
        form.addRow("Battery slow factor:", self.sb_batt_factor)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def apply(self):
        """Copy widget values back into the configuration object."""

        seq = self.kse.keySequence()
        self.cfg.hotkey = seq.toString() if seq else ""
        self.cfg.gobble_on_wake = bool(self.cb_wake.isChecked())
        self.cfg.show_system = bool(self.cb_show_system.isChecked())
        self.cfg.protect_system = bool(self.cb_protect_system.isChecked())
        self.cfg.pressure_enabled = bool(self.cb_pressure.isChecked())
        self.cfg.min_free_percent = int(self.sb_min_free.value())
        self.cfg.gaming_mode = bool(self.cb_gaming.isChecked())
        self.cfg.battery_mode = self.cmb_batt.currentText()
        self.cfg.battery_slow_factor = int(self.sb_batt_factor.value())


class ChartWidget(QWidget):
    """Simple bar chart used on the dashboard."""

    def __init__(self, values: List[float], parent=None):
        """Initialise the chart with an optional set of historical values."""

        super().__init__(parent)
        self.values = values
        self.setMinimumHeight(80)

    def set_values(self, values: List[float]):
        """Update the bar chart's data set."""

        self.values = values
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API
        """Draw a bar for each data point using the widget rect."""

        painter = QPainter(self)
        rect = self.rect().adjusted(2, 2, -2, -2)
        values = self.values or [0]
        vmax = max(values) or 1.0
        bar_w = rect.width() / max(len(values), 1)
        for idx, value in enumerate(values):
            x = rect.left() + int(idx * bar_w)
            height = int((value / vmax) * rect.height())
            painter.fillRect(x, rect.bottom() - height, int(bar_w * 0.8), height, Qt.darkCyan)


class Dashboard(QWidget):
    """Summary view showing recent gobbling activity."""

    def __init__(self, config: Config, metrics: Metrics, parent=None):
        """Lay out summary cards, tables and export buttons."""

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
            box_layout = QVBoxLayout(box)
            box_layout.addWidget(widget)
            card_row.addWidget(box)
        layout.addLayout(card_row)

        self.top_table = QTableWidget(0, 2)
        self.top_table.setHorizontalHeaderLabels(["Top Saver", "MB"])
        self.top_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.top_table)

        self.eff_table = QTableWidget(0, 2)
        self.eff_table.setHorizontalHeaderLabels(["Strategy", "Avg MB/Gobble"])
        self.eff_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.eff_table)

        self.chart = ChartWidget([])
        layout.addWidget(self.chart)

        btn_row = QHBoxLayout()
        self.btn_day_json = QPushButton("Export Day JSON")
        self.btn_day_csv = QPushButton("Export Day CSV")
        self.btn_month_json = QPushButton("Export 30-day JSON")
        self.btn_month_csv = QPushButton("Export 30-day CSV")
        for button in (self.btn_day_json, self.btn_day_csv, self.btn_month_json, self.btn_month_csv):
            btn_row.addWidget(button)
        layout.addLayout(btn_row)
        self.btn_day_json.clicked.connect(lambda: self._export(1, "json"))
        self.btn_day_csv.clicked.connect(lambda: self._export(1, "csv"))
        self.btn_month_json.clicked.connect(lambda: self._export(30, "json"))
        self.btn_month_csv.clicked.connect(lambda: self._export(30, "csv"))

    def _export(self, days: int, fmt: str):
        """Persist metrics to a file chosen by the user."""

        data = self.metrics.export_json(days) if fmt == "json" else self.metrics.export_csv(days)
        file_filter = "JSON Files (*.json)" if fmt == "json" else "CSV Files (*.csv)"
        path, _ = QFileDialog.getSaveFileName(self, "Export Metrics", "", file_filter)
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data)

    def refresh(self, session_total: float):
        """Refresh dashboard cards and tables from stored metrics."""

        day = self.metrics._aggregate(1)
        week = self.metrics._aggregate(7)
        month = self.metrics._aggregate(30)
        self.lbl_session.setText(f"{session_total:.1f} MB")
        self.lbl_day.setText(f"{day['reclaimed_mb']:.1f} MB")
        self.lbl_week.setText(f"{week['reclaimed_mb']:.1f} MB")
        self.lbl_month.setText(f"{month['reclaimed_mb']:.1f} MB")

        self.top_table.setRowCount(0)
        top_targets = sorted(self.config.targets, key=lambda t: t.session_reclaimed_mb, reverse=True)[:5]
        totals = group_totals(self.config.targets)
        top_groups = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
        for target in top_targets:
            row = self.top_table.rowCount()
            self.top_table.insertRow(row)
            self.top_table.setItem(row, 0, QTableWidgetItem(f"Target: {target.key}"))
            self.top_table.setItem(row, 1, QTableWidgetItem(f"{target.session_reclaimed_mb:.1f}"))
        for gid, value in top_groups:
            name = next((g.name for g in self.config.groups if g.id == gid), "Ungrouped" if not gid else gid)
            row = self.top_table.rowCount()
            self.top_table.insertRow(row)
            self.top_table.setItem(row, 0, QTableWidgetItem(f"Group: {name}"))
            self.top_table.setItem(row, 1, QTableWidgetItem(f"{value:.1f}"))

        self.eff_table.setRowCount(0)
        agg = self.metrics._aggregate(30)
        for strat, mb in agg.get("by_strategy", {}).items():
            gobbles = agg.get("by_strategy_gobbles", {}).get(strat, 0)
            avg = mb / gobbles if gobbles else 0.0
            row = self.eff_table.rowCount()
            self.eff_table.insertRow(row)
            self.eff_table.setItem(row, 0, QTableWidgetItem(strat))
            self.eff_table.setItem(row, 1, QTableWidgetItem(f"{avg:.1f}"))

        today = datetime.now().date()
        values = []
        for i in range(30):
            date = today - timedelta(days=29 - i)
            values.append(self.metrics.rollups.get(date.strftime("%Y-%m-%d"), {}).get("reclaimed_mb", 0.0))
        self.chart.set_values(values)


__all__ = [
    "ProcessTableModel",
    "Sparkline",
    "GroupsSidebar",
    "GroupSettingsDialog",
    "RulesDialog",
    "TargetsTable",
    "AddTargetDialog",
    "SettingsDialog",
    "ChartWidget",
    "Dashboard",
    "_InteractionWatcher",
]
