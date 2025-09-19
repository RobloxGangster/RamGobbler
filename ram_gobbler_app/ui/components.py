"""Qt widgets and models used by the Ram Gobbler interface.

This module houses the majority of the user-facing widgets.  The classes are
deliberately verbose – both in implementation and documentation – so that
contributors with only basic Qt knowledge can understand how the dashboard,
tables and dialogs interact with the background gobbler thread.
"""

from __future__ import annotations

import ntpath
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..constants import DEFAULT_INTERVAL
from ..helpers import empty_working_set, rss_mb
from ..models import Config, Group, Target
from ..processes import (
    ProcessNode,
    build_process_tree,
    estimate_reclaim,
    list_processes,
    match_pids,
)
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
    QVBoxLayout,
    QWidget,
    Qt,
    QAbstractItemModel,
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


class ProcessTreeItem:
    """Internal helper representing a node in :class:`ProcessTreeModel`."""

    def __init__(
        self,
        node: Optional[ProcessNode] = None,
        parent: Optional["ProcessTreeItem"] = None,
    ) -> None:
        self.node = node
        self.parent = parent
        self.children: List["ProcessTreeItem"] = []
        if node:
            for child in node.children:
                self.children.append(ProcessTreeItem(child, self))

    def child(self, row: int) -> Optional["ProcessTreeItem"]:
        if 0 <= row < len(self.children):
            return self.children[row]
        return None

    def child_count(self) -> int:
        return len(self.children)

    def row(self) -> int:
        if not self.parent:
            return 0
        try:
            return self.parent.children.index(self)
        except ValueError:
            return 0

    def display_name(self) -> str:
        if not self.node:
            return ""
        if self.node.name:
            return self.node.name
        if self.node.exe:
            return self.node.exe
        return f"PID {self.node.pid}"

    def all_pids(self) -> List[int]:
        if self.node:
            return list(self.node.descendant_pids)
        pids: List[int] = []
        for child in self.children:
            pids.extend(child.all_pids())
        return pids

    def iter_nodes(self) -> Iterable[ProcessNode]:
        if self.node:
            return self.node.walk()

        def descendants() -> Iterable[ProcessNode]:
            for child in self.children:
                yield from child.iter_nodes()

        return descendants()


class ProcessTreeModel(QAbstractItemModel):
    """Hierarchical process model mirroring Task Manager's grouped view."""

    COLS = ["Process", "Memory (MB)", "PID"]
    nodes_ready = Signal(object, object)

    def __init__(self, include_system: bool):
        super().__init__()
        self.include_system = include_system
        self._root = ProcessTreeItem()
        self._items_by_pid: Dict[int, ProcessTreeItem] = {}
        self._refreshing = False
        self._pending_refresh = False
        self._lock = threading.Lock()
        self._pending_callbacks: List[Callable[[], None]] = []
        if hasattr(self, "nodes_ready") and callable(getattr(self.nodes_ready, "connect", None)):
            try:
                self.nodes_ready.connect(self._apply_nodes)
            except Exception:
                pass
        self.refresh()

    def set_include_system(self, val: bool, done=None):
        self.include_system = val
        self.refresh(done)

    def refresh(self, done=None):
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
        def worker():
            try:
                nodes = build_process_tree(self.include_system)
            except Exception:
                nodes = []
            try:
                if hasattr(self, "nodes_ready"):
                    self.nodes_ready.emit(nodes, callback)  # type: ignore[attr-defined]
                else:
                    self._apply_nodes(nodes, callback)
            except Exception:
                self._apply_nodes(nodes, callback)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_nodes(self, nodes: List[ProcessNode], callback: Optional[Callable[[], None]]):
        callbacks: List[Callable[[], None]] = []
        if callback:
            callbacks.append(callback)
        with self._lock:
            callbacks.extend(self._pending_callbacks)
            self._pending_callbacks.clear()
            rerun = self._pending_refresh
            self._pending_refresh = False
            self._refreshing = False

        new_root = ProcessTreeItem()
        for node in nodes:
            new_root.children.append(ProcessTreeItem(node, new_root))

        self.beginResetModel()
        self._root = new_root
        self._rebuild_cache()
        self.endResetModel()

        for cb in callbacks:
            try:
                cb()
            except Exception:
                continue

        if rerun:
            self.refresh()

    def _rebuild_cache(self) -> None:
        items: Dict[int, ProcessTreeItem] = {}

        def visit(item: ProcessTreeItem) -> None:
            if item.node:
                items[item.node.pid] = item
            for child in item.children:
                visit(child)

        visit(self._root)
        self._items_by_pid = items

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        parent_item = self._root if not parent.isValid() else parent.internalPointer()
        if isinstance(parent_item, ProcessTreeItem):
            return parent_item.child_count()
        return 0

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(self.COLS)

    def index(self, row, column, parent=QModelIndex()):  # noqa: N802
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_item = self._root if not parent.isValid() else parent.internalPointer()
        if not isinstance(parent_item, ProcessTreeItem):
            return QModelIndex()
        child = parent_item.child(row)
        if child is None:
            return QModelIndex()
        return self.createIndex(row, column, child)

    def parent(self, index):  # noqa: N802
        if not index.isValid():
            return QModelIndex()
        item = index.internalPointer()
        if not isinstance(item, ProcessTreeItem):
            return QModelIndex()
        parent_item = item.parent
        if not parent_item or parent_item is self._root:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.COLS[section]
        return section + 1

    def data(self, index, role=Qt.DisplayRole):  # noqa: N802
        if not index.isValid():
            return None
        item = index.internalPointer()
        if not isinstance(item, ProcessTreeItem) or not item.node:
            return None
        node = item.node
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return item.display_name()
            if col == 1:
                return f"{node.aggregate_rss_mb:,.1f}"
            if col == 2:
                return str(node.pid)
        if role == getattr(Qt, "ToolTipRole", None) and node.exe:
            return node.exe
        return None

    def flags(self, index):  # noqa: N802
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def node_for_index(self, index: QModelIndex) -> Optional[ProcessTreeItem]:
        if not index.isValid():
            return None
        item = index.internalPointer()
        if isinstance(item, ProcessTreeItem):
            return item
        return None

    def index_for_pid(self, pid: int, column: int = 0) -> QModelIndex:
        item = self._items_by_pid.get(pid)
        if not item:
            return QModelIndex()
        parent_item = item.parent if item.parent else self._root
        row = item.row()
        return self.createIndex(row, column, item)


class ProcessFilterProxyModel(QSortFilterProxyModel):
    """Proxy model that keeps parents visible when descendants match."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filter_text = ""

    def setFilterFixedString(self, pattern: str):  # noqa: N802
        self._filter_text = pattern or ""
        super().setFilterFixedString(pattern)

    def filterAcceptsRow(self, source_row, source_parent):  # noqa: N802
        model = self.sourceModel()
        if model is None:
            return False
        index = model.index(source_row, 0, source_parent)
        if not index.isValid():
            return False
        if not self._filter_text:
            return True
        if self._matches_index(index):
            return True
        for row in range(model.rowCount(index)):
            if self.filterAcceptsRow(row, index):
                return True
        return False

    def _matches_index(self, index) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        text = self._filter_text.casefold()
        parent = index.parent()
        columns = model.columnCount(parent) if hasattr(model, "columnCount") else 1
        for col in range(columns):
            idx = model.index(index.row(), col, parent)
            data = model.data(idx, Qt.DisplayRole)
            if data is None:
                continue
            if text in str(data).casefold():
                return True
        return False

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
        self._suspend_emit = False
        self._current_group_id = ""

        self.setObjectName("groupSettingsPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.list = QListWidget()
        self.list.setObjectName("groupSettingsList")
        self.list.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self.list)

        buttons_bar = QWidget()
        buttons_bar.setObjectName("groupSettingsButtons")
        buttons_layout = QHBoxLayout(buttons_bar)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(10)

        self.btn_add = QPushButton("Add")
        self.btn_add.clicked.connect(self._add)
        buttons_layout.addWidget(self.btn_add)

        self.btn_rename = QPushButton("Rename")
        self.btn_rename.clicked.connect(self._rename)
        buttons_layout.addWidget(self.btn_rename)

        self.btn_delete = QPushButton("Delete")
        self.btn_delete.clicked.connect(self._delete)
        buttons_layout.addWidget(self.btn_delete)

        buttons_layout.addStretch(1)

        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(self._start_group)
        buttons_layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self._stop_group)
        buttons_layout.addWidget(self.btn_stop)

        layout.addWidget(buttons_bar)

        self.list.currentRowChanged.connect(self._emit_group)
        self.refresh()

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
        self._update_button_states()
        self._apply_style()

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
        self._update_button_states()
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

    def _start_group(self):
        """Activate gobbling for targets within the selected scope."""

        controller = self._controller
        if not controller or not hasattr(controller, "gobbler"):
            return
        gid = self.current_group_id()
        for index, target in enumerate(controller.config.targets):
            if not gid or target.group_id == gid:
                controller.gobbler.trigger_now(index, ensure_active=True, continuous=True)
        targets_table = getattr(controller, "targets_table", None)
        if targets_table and hasattr(targets_table, "refresh"):
            targets_table.refresh(force=True)
        if gid and hasattr(controller, "_group_name"):
            scope = controller._group_name(gid)
        else:
            scope = "all targets" if not gid else gid
        status_bar = controller.statusBar() if hasattr(controller, "statusBar") else None
        if status_bar is not None:
            status_bar.showMessage(f"Gobbling started for {scope}", 3000)

    def _stop_group(self):
        """Pause gobbling for targets within the selected scope."""

        controller = self._controller
        if not controller or not hasattr(controller, "gobbler"):
            return
        gid = self.current_group_id()
        for index, target in enumerate(controller.config.targets):
            if not gid or target.group_id == gid:
                controller.gobbler.set_active(index, False)
        targets_table = getattr(controller, "targets_table", None)
        if targets_table and hasattr(targets_table, "refresh"):
            targets_table.refresh(force=True)
        if gid and hasattr(controller, "_group_name"):
            scope = controller._group_name(gid)
        else:
            scope = "all targets" if not gid else gid
        status_bar = controller.statusBar() if hasattr(controller, "statusBar") else None
        if status_bar is not None:
            status_bar.showMessage(f"Gobbling stopped for {scope}", 3000)

    def _update_button_states(self):
        """Enable or disable buttons based on the current selection."""

        has_group = self.list.currentRow() > 0
        self.btn_rename.setEnabled(has_group)
        self.btn_delete.setEnabled(has_group)

    def _apply_style(self):
        """Apply the grey modal styling requested for the mock-up."""

        theme = getattr(self.config, "theme", "dark")
        if theme == "light":
            base_bg = "#f1f3f6"
            list_bg = "#ffffff"
            border = "#cdd3de"
            button_bg = "#dfe3eb"
            button_hover = "#d2d7e1"
            button_press = "#c4cad6"
            button_fg = "#1a1c20"
        else:
            base_bg = "#1b1f29"
            list_bg = "#11141a"
            border = "#2a2f3a"
            button_bg = "#2b3a55"
            button_hover = "#33476a"
            button_press = "#25364f"
            button_fg = "#eaeef8"

        self.setStyleSheet(
            """
            QWidget#groupSettingsPanel {
                background-color: %s;
                border-radius: 14px;
            }
            QListWidget#groupSettingsList {
                background-color: %s;
                border: 1px solid %s;
                border-radius: 10px;
                padding: 6px;
            }
            QWidget#groupSettingsButtons {
                background-color: %s;
                border-top: 1px solid %s;
                padding: 12px 0;
            }
            QWidget#groupSettingsButtons QPushButton {
                background-color: %s;
                color: %s;
                border: 1px solid %s;
                border-radius: 8px;
                padding: 6px 16px;
            }
            QWidget#groupSettingsButtons QPushButton:hover {
                background-color: %s;
            }
            QWidget#groupSettingsButtons QPushButton:pressed {
                background-color: %s;
            }
            """
            % (
                base_bg,
                list_bg,
                border,
                base_bg,
                border,
                button_bg,
                button_fg,
                border,
                button_hover,
                button_press,
            )
        )

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
        layout.setContentsMargins(24, 24, 24, 16)
        layout.setSpacing(16)

        self.sidebar = GroupsSidebar(main_window.config, self, main_window)
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


class IntervalEditor(QWidget):
    """Composite widget for editing float intervals with separate controls."""

    valueChanged = Signal(float)
    editingFinished = Signal()

    def __init__(
        self,
        value: float,
        parent=None,
        *,
        minimum: float = 0.5,
        maximum: float = 600.0,
        step: float = 0.5,
    ):
        super().__init__(parent)
        self._min = float(minimum)
        self._max = float(maximum)
        self._step = float(step) if step > 0 else 0.5
        self._value = self._snap(self._clamp(value))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.line_edit = QLineEdit(self)
        if hasattr(self, "setFocusProxy"):
            try:
                self.setFocusProxy(self.line_edit)
            except Exception:  # pragma: no cover - defensive
                pass
        if hasattr(self.line_edit, "setAlignment") and hasattr(Qt, "AlignRight"):
            self.line_edit.setAlignment(Qt.AlignRight)
        layout.addWidget(self.line_edit)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)

        self.btn_down = QPushButton("-", self)
        self.btn_up = QPushButton("+", self)
        button_stylesheet = (
            """
            QPushButton {
                background-color: #AEBFF5;
                border: 1px solid #8C9FD9;
                border-radius: 4px;
                font-size: 16px;
                font-weight: bold;
                color: #000000;
            }
            QPushButton:hover {
                background-color: #9DAFDC;
                color: #000000;
            }
            QPushButton:pressed {
                background-color: #8C9FD9;
                color: #000000;
            }
            QPushButton:disabled {
                background-color: #D0DAF9;
                color: #6B6B6B;
            }
            """
        )
        for btn in (self.btn_down, self.btn_up):
            if hasattr(btn, "setAutoDefault"):
                btn.setAutoDefault(False)
            if hasattr(btn, "setDefault"):
                btn.setDefault(False)
            if hasattr(btn, "setFocusPolicy") and hasattr(Qt, "NoFocus"):
                btn.setFocusPolicy(Qt.NoFocus)
            if hasattr(btn, "setFlat"):
                btn.setFlat(True)
            if hasattr(btn, "setFixedWidth"):
                btn.setFixedWidth(34)
            if hasattr(btn, "setFixedHeight"):
                btn.setFixedHeight(28)
            if hasattr(btn, "setFont"):
                font = btn.font()
                font.setBold(True)
                if font.pointSize() <= 0:
                    font.setPointSize(12)
                else:
                    font.setPointSize(max(font.pointSize(), 12))
                btn.setFont(font)
            if hasattr(btn, "setStyleSheet"):
                btn.setStyleSheet(button_stylesheet)
            btn_layout.addWidget(btn)

        layout.addLayout(btn_layout)

        if hasattr(self.btn_up, "clicked"):
            self.btn_up.clicked.connect(self._on_step_up)
        if hasattr(self.btn_down, "clicked"):
            self.btn_down.clicked.connect(self._on_step_down)
        if hasattr(self.line_edit, "editingFinished"):
            self.line_edit.editingFinished.connect(self._on_edit_finished)

        self._update_display()

    def _clamp(self, value: float) -> float:
        return max(self._min, min(self._max, float(value)))

    def _snap(self, value: float) -> float:
        if self._step <= 0:
            return float(value)
        steps = round(float(value) / self._step)
        return steps * self._step

    def _format(self, value: float) -> str:
        rounded = round(value, 2)
        if abs(rounded - round(rounded)) < 1e-6:
            return str(int(round(rounded)))
        text = f"{rounded:.2f}".rstrip("0").rstrip(".")
        if text.startswith("."):
            return f"0{text}"
        if text.startswith("-."):
            return text.replace("-.", "-0.", 1)
        return text

    def _update_display(self) -> None:
        text = self._format(self._value)
        if hasattr(self.line_edit, "setText"):
            self.line_edit.setText(text)

    def set_display_width(self, width: int) -> None:
        width = max(0, width)
        font_metrics = None
        getter = getattr(self.line_edit, "fontMetrics", None)
        if callable(getter):
            try:
                font_metrics = getter()
            except Exception:  # pragma: no cover - defensive
                font_metrics = None
        if font_metrics is not None:
            advance = getattr(font_metrics, "horizontalAdvance", None)
            sample_text = "0000.00"
            if callable(advance):
                try:
                    sample = advance(sample_text)
                except Exception:  # pragma: no cover - defensive
                    sample = 0
            else:
                width_fn = getattr(font_metrics, "width", None)
                sample = width_fn(sample_text) if callable(width_fn) else 0
            width = max(width, sample)
        extra_padding = 0
        get_margins = getattr(self.line_edit, "textMargins", None)
        if callable(get_margins):
            try:
                margins = get_margins()
            except Exception:  # pragma: no cover - defensive
                margins = None
            if margins is not None:
                left = getattr(margins, "left", None)
                right = getattr(margins, "right", None)
                if callable(left) and callable(right):
                    extra_padding += left() + right()
                elif isinstance(margins, tuple) and len(margins) == 4:
                    extra_padding += margins[0] + margins[2]
        if extra_padding <= 0:
            # Account for padding applied by the global stylesheet.
            extra_padding = 24
        style_getter = getattr(self.line_edit, "style", None)
        style = style_getter() if callable(style_getter) else None
        if style and hasattr(style, "pixelMetric") and hasattr(QStyle, "PM_DefaultFrameWidth"):
            try:
                frame = style.pixelMetric(QStyle.PM_DefaultFrameWidth, None, self.line_edit)
            except Exception:  # pragma: no cover - defensive
                frame = 0
            extra_padding += frame * 2
        total_width = width + extra_padding
        if hasattr(self.line_edit, "setMinimumWidth"):
            self.line_edit.setMinimumWidth(total_width)
        if hasattr(self.line_edit, "setMaximumWidth"):
            self.line_edit.setMaximumWidth(total_width)
        extra = 80  # space for buttons and layout spacing
        if hasattr(self, "setMinimumWidth"):
            self.setMinimumWidth(total_width + extra)
        if hasattr(self, "setMaximumWidth"):
            self.setMaximumWidth(total_width + extra + 12)

    def value(self) -> float:
        return self._value

    def setValue(self, value: float, *, emit: bool = False) -> None:
        clean = self._snap(self._clamp(value))
        changed = abs(clean - self._value) > 1e-6
        self._value = clean
        self._update_display()
        if emit and changed and hasattr(self, "valueChanged"):
            try:
                self.valueChanged.emit(self._value)
            except Exception:  # pragma: no cover - defensive
                pass

    def interaction_widgets(self) -> List[QWidget]:
        """Return widgets that should pause auto-refresh when interacted with."""

        return [w for w in (self, self.line_edit, self.btn_up, self.btn_down) if w]

    def _on_step_up(self) -> None:
        self.setValue(self._value + self._step, emit=True)

    def _on_step_down(self) -> None:
        self.setValue(self._value - self._step, emit=True)

    def _on_edit_finished(self) -> None:
        getter = getattr(self.line_edit, "text", None)
        raw = getter() if callable(getter) else ""
        if not isinstance(raw, str):
            raw = ""
        text = raw.strip()
        try:
            new_val = float(text)
        except Exception:
            self._update_display()
        else:
            self.setValue(new_val, emit=True)
        if hasattr(self, "editingFinished"):
            try:
                self.editingFinished.emit()
            except Exception:  # pragma: no cover - defensive
                pass


class TargetStatsDialog(QDialog):
    """Modal dialog presenting detailed statistics for a target."""

    def __init__(self, target: Target, parent=None):
        """Capture the target reference and build the stats layout."""

        super().__init__(parent)
        self.setWindowTitle("Target Stats")
        self.setMinimumWidth(360)
        self._target = target

        layout = QVBoxLayout(self)

        self.last_box = QGroupBox("Last Δ (MB)")
        last_layout = QVBoxLayout(self.last_box)
        self.lbl_last = QLabel()
        if hasattr(self.lbl_last, "setAlignment") and hasattr(Qt, "AlignCenter"):
            self.lbl_last.setAlignment(Qt.AlignCenter)
        last_layout.addWidget(self.lbl_last)
        layout.addWidget(self.last_box)

        self.totals_box = QGroupBox("Totals MB (Sess/Day)")
        totals_layout = QVBoxLayout(self.totals_box)
        self.lbl_session_total = QLabel()
        self.lbl_day_total = QLabel()
        totals_layout.addWidget(self.lbl_session_total)
        totals_layout.addWidget(self.lbl_day_total)
        layout.addWidget(self.totals_box)

        self.history_box = QGroupBox("History")
        history_layout = QVBoxLayout(self.history_box)
        self.sparkline = Sparkline(target.history, self.history_box)
        history_layout.addWidget(self.sparkline)
        self.lbl_history = QLabel()
        if hasattr(self.lbl_history, "setWordWrap"):
            self.lbl_history.setWordWrap(True)
        history_layout.addWidget(self.lbl_history)
        layout.addWidget(self.history_box)

        self.button_box = QDialogButtonBox(QDialogButtonBox.Close)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._refresh()
        self._timer.start()

    def closeEvent(self, event):  # noqa: N802 - Qt API
        """Stop the refresh timer when the dialog is closed."""

        if hasattr(self, "_timer") and self._timer.isActive():
            self._timer.stop()
        super().closeEvent(event)

    def _refresh(self):
        """Update all statistic fields from the current target state."""

        target = self._target
        self.lbl_last.setText(f"{target.last_delta_mb:.1f}")
        self.lbl_session_total.setText(f"Session reclaimed: {target.session_reclaimed_mb:.1f} MB")
        self.lbl_day_total.setText(f"Day reclaimed: {target.day_reclaimed_mb:.1f} MB")
        history = target.history[-12:]
        if history:
            values = ", ".join(f"{val:.1f}" for val in history)
            self.lbl_history.setText(values)
        else:
            self.lbl_history.setText("No history recorded yet.")
        if isinstance(self.sparkline, Sparkline):
            self.sparkline.set_values(target.history)


class TargetsTable(QTableWidget):
    """Central table listing configured targets and their status."""

    HEADERS = [
        "Group",
        "Target",
        "Match",
        "Auto-start",
        "Status",
        "Interval (s)",
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
        header.setStretchLastSection(True)
        self.setWordWrap(False)

        metrics = self.fontMetrics()
        sample_status = "Gobbling — 8888.8 MB reclaimed"
        try:
            status_width = metrics.horizontalAdvance(sample_status)
        except AttributeError:  # pragma: no cover - Qt6 preferred API
            status_width = metrics.width(sample_status)  # type: ignore[attr-defined]
        status_width += 32
        self._status_column_width = status_width
        try:
            header.setSectionResizeMode(4, QHeaderView.Fixed)
            self.setColumnWidth(4, self._status_column_width)
        except Exception:
            pass
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
        QTimer.singleShot(0, self._apply_status_width)

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

    def _display_target_label(self, target: Target) -> str:
        """Return a compact label for the target's process column."""

        raw_key = getattr(target, "key", "") or ""
        key = raw_key.strip()
        if not key:
            return ""

        candidate = key.strip("\"'")
        looks_like_path = target.match_by in {"path", "parent"} or any(
            sep in candidate for sep in ("/", "\\")
        )

        if looks_like_path:
            lower = candidate.lower()
            exe_idx = lower.rfind(".exe")
            if exe_idx >= 0:
                candidate = candidate[: exe_idx + 4]
            candidate = candidate.strip("\"'")
            base = ntpath.basename(candidate)
            if base:
                return base
            if candidate:
                return candidate

        return candidate or raw_key

    def refresh(self, force: bool = False):
        """Rebuild the table rows, preserving scroll position and selection."""

        if self.is_busy():
            self._pending_refresh_force = self._pending_refresh_force or force
            self._refresh_timer.start(200)
            return
        if self._refresh_timer.isActive():
            self._refresh_timer.stop()
        self._pending_refresh_force = False

        scroll_state, selected_uids = self._capture_state()

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

        self._restore_state(scroll_state, selected_uids)
        QTimer.singleShot(0, self.resizeColumnsToContents)
        QTimer.singleShot(0, self._apply_status_width)

    def _capture_state(self) -> Tuple[Tuple[int, int], List[str]]:
        """Remember scroll position and selected targets."""

        vbar = self.verticalScrollBar()
        hbar = self.horizontalScrollBar()
        scroll_vals = (
            vbar.value() if vbar else 0,
            hbar.value() if hbar else 0,
        )
        selected = []
        if self.selectionModel():
            for index in self.selectionModel().selectedRows():
                uid = self._row_targets[index.row()]
                if uid:
                    selected.append(uid)
        return scroll_vals, selected

    def _restore_state(
        self, scroll_vals: Tuple[int, int], selected_uids: List[str]
    ):
        """Restore the captured state after repopulating the table."""

        v_scroll, h_scroll = scroll_vals
        vbar = self.verticalScrollBar()
        if vbar:
            vbar.blockSignals(True)
            try:
                vbar.setValue(max(vbar.minimum(), min(v_scroll, vbar.maximum())))
            finally:
                vbar.blockSignals(False)
        hbar = self.horizontalScrollBar()
        if hbar:
            hbar.blockSignals(True)
            try:
                hbar.setValue(max(hbar.minimum(), min(h_scroll, hbar.maximum())))
            finally:
                hbar.blockSignals(False)
        if not self.selectionModel():
            return
        for uid in selected_uids:
            row = self.uid_to_row.get(uid)
            if row is not None:
                self.selectRow(row)

    def is_busy(self) -> bool:
        """Expose the interaction status to callers."""

        vbar = self.verticalScrollBar() if hasattr(self, "verticalScrollBar") else None
        hbar = self.horizontalScrollBar() if hasattr(self, "horizontalScrollBar") else None
        sliding = bool(
            (vbar and vbar.isSliderDown()) or (hbar and hbar.isSliderDown())
        )
        return sliding or self._interaction.active

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
        target_label = self._display_target_label(target)
        target_item = QTableWidgetItem(target_label)
        if target.key:
            target_item.setToolTip(target.key)
        match_item = QTableWidgetItem(target.match_by)
        status_text = "Gobbling" if target.active else "Idle"
        if target.last_result:
            status_text += f" — {target.last_result}"
        status_item = QTableWidgetItem(status_text)
        status_item.setToolTip(status_text)

        autostart_cb = QCheckBox()
        autostart_cb.setChecked(target.autostart)
        autostart_cb.stateChanged.connect(lambda _=None, i=idx, cb=autostart_cb: self._on_autostart(i, cb.isChecked()))

        interval_widget = IntervalEditor(target.interval_sec, minimum=0.5, maximum=600.0, step=0.5)
        interval_widget.setToolTip("How often to gobble memory for this target")
        metrics = interval_widget.fontMetrics()
        try:
            digits_width = metrics.horizontalAdvance("0000.0")
        except AttributeError:  # pragma: no cover - Qt6 preferred API
            digits_width = metrics.width("0000.0")  # type: ignore[attr-defined]
        interval_widget.set_display_width(digits_width + 24)
        if hasattr(interval_widget, "setFocusPolicy") and hasattr(Qt, "StrongFocus"):
            interval_widget.setFocusPolicy(Qt.StrongFocus)
        interval_widget.valueChanged.connect(lambda val, i=idx: self._on_interval(i, val))
        interval_widget.editingFinished.connect(
            lambda i=idx, widget=interval_widget: self._commit_interval_widget(i, widget)
        )

        self.setCellWidget(row, 0, group_widget)
        self.setItem(row, 1, target_item)
        self.setItem(row, 2, match_item)
        self.setCellWidget(row, 3, autostart_cb)
        self.setItem(row, 4, status_item)
        self.setCellWidget(row, 5, interval_widget)

        for widget in (
            group_widget,
            autostart_cb,
            interval_widget,
        ):
            self._register_interaction(widget)
        if hasattr(interval_widget, "interaction_widgets"):
            for child in interval_widget.interaction_widgets():
                self._register_interaction(child)

    def _apply_status_width(self) -> None:
        """Keep the status column width fixed to avoid layout shifts."""

        width = getattr(self, "_status_column_width", 0)
        if width <= 0 or not hasattr(self, "setColumnWidth"):
            return
        header = self.horizontalHeader() if hasattr(self, "horizontalHeader") else None
        if header and hasattr(header, "setSectionResizeMode"):
            try:
                header.setSectionResizeMode(4, QHeaderView.Fixed)
            except Exception:
                pass
        try:
            self.setColumnWidth(4, width)
        except Exception:
            pass

    def contextMenuEvent(self, event):  # noqa: N802 - Qt API
        """Provide context actions when the user right-clicks a row."""

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

        target = None
        if 0 <= config_index < len(self.config.targets):
            target = self.config.targets[config_index]

        menu = QMenu(self)
        start_action = menu.addAction("Gobble RAM")
        stop_action = menu.addAction("Stop Gobbling")
        stats_action = menu.addAction("Stats")
        edit_action = menu.addAction("Edit Rules…")
        remove_action = menu.addAction("Remove Target")

        if target is not None:
            start_action.setEnabled(not target.active)
            stop_action.setEnabled(target.active)

        exec_fn = getattr(menu, "exec", None) or getattr(menu, "exec_", None)
        if not exec_fn:
            return super().contextMenuEvent(event)

        chosen = exec_fn(self.mapToGlobal(event.pos()))
        if chosen == start_action and target is not None:
            self._on_start(config_index)
        elif chosen == stop_action and target is not None:
            self._on_stop(config_index)
        elif chosen == stats_action and target is not None:
            self._on_stats(config_index)
        elif chosen == edit_action:
            self._on_edit(config_index)
        elif chosen == remove_action:
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

        self.gobbler.trigger_now(index, ensure_active=True, continuous=True)
        self.refresh(force=True)

    def _on_stop(self, index: int):
        """Stop gobbling for the selected target."""

        self.gobbler.set_active(index, False)
        self.refresh(force=True)

    def _on_stats(self, index: int):
        """Open the statistics dialog for the specified target."""

        if not (0 <= index < len(self.config.targets)):
            return
        target = self.config.targets[index]
        dialog = TargetStatsDialog(target, self)
        dialog.exec()

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

    def _on_interval(self, index: int, val: float):
        """Persist interval changes from the spin box."""

        self.gobbler.set_interval(index, val)

    def _commit_interval_widget(self, index: int, widget):
        """Store manual edits that do not emit a valueChanged signal."""

        if not (0 <= index < len(self.config.targets)):
            return
        try:
            if not hasattr(widget, "value"):
                return
            value_obj = getattr(widget, "value")
            new_value = float(value_obj()) if callable(value_obj) else float(value_obj)
        except Exception:  # pragma: no cover - defensive
            return
        if abs(self.config.targets[index].interval_sec - new_value) < 1e-6:
            return
        self._on_interval(index, new_value)

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

    def _flush_pending_refresh(self):
        """Execute a deferred refresh after the user stops interacting."""

        force = self._pending_refresh_force
        self._pending_refresh_force = False
        self.refresh(force=force)


class AddTargetDialog(QDialog):
    """Dialog for adding a new target using a friendly form."""

    def __init__(
        self,
        config: Config,
        key: str,
        match_by: str,
        parent=None,
        *,
        summary: Optional[List[str]] = None,
        include_children: bool = False,
        group_id: str = "",
    ):
        """Pre-fill the dialog with sensible defaults for ``key`` and mode."""

        super().__init__(parent)
        self.setWindowTitle("Add Target")
        self.setMinimumWidth(520)
        self._config = config
        self._include_children = bool(include_children)
        self._initial_group = group_id or ""

        form = QFormLayout(self)
        self.le_key = QLineEdit(key)
        self.cmb_match = QComboBox()
        self.cmb_match.addItems(["name", "path", "cmdline", "parent"])
        if match_by in ["name", "path", "cmdline", "parent"]:
            self.cmb_match.setCurrentText(match_by)

        self.cmb_group = QComboBox()
        if hasattr(self.cmb_group, "setEditable"):
            self.cmb_group.setEditable(False)
        if hasattr(self.cmb_group, "setToolTip"):
            self.cmb_group.setToolTip("Assign the new target to a group")
        if hasattr(self.cmb_group, "setSizeAdjustPolicy") and hasattr(QComboBox, "AdjustToContents"):
            self.cmb_group.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._populate_groups(self._initial_group)

        self.interval_editor = IntervalEditor(DEFAULT_INTERVAL, minimum=0.5, maximum=600.0, step=0.5)
        self.cb_autostart = QCheckBox("Auto-start gobbling for this target")

        self.sb_cpu = QSpinBox()
        self.sb_cpu.setRange(0, 100)
        self.sb_cpu.setValue(60)
        self.cb_adaptive = QCheckBox("Adaptive interval")
        self.cb_adaptive.setChecked(True)
        self.cb_bg = QCheckBox("Gobble only when target is in background")
        self.cb_bg.setChecked(False)
        self.cb_full = QCheckBox("Pause while any app is fullscreen")
        self.cb_full.setChecked(True)
        self.cb_strategy = QComboBox()
        self.cb_strategy.addItems(["standard", "reset"])
        self.cb_strategy.setCurrentText("standard")

        self.lbl_preview = QLabel("Matches: 0")
        self.le_key.textChanged.connect(self._update_preview)
        self.cmb_match.currentTextChanged.connect(self._update_preview)

        form.addRow("Target pattern:", self.le_key)
        form.addRow("Match by:", self.cmb_match)
        form.addRow("Group:", self.cmb_group)
        form.addRow("Interval (s):", self.interval_editor)
        form.addRow("Auto-start:", self.cb_autostart)
        form.addRow("CPU skip threshold (%):", self.sb_cpu)
        form.addRow("Interval strategy:", self.cb_adaptive)
        form.addRow("Background-only:", self.cb_bg)
        form.addRow("Pause if fullscreen:", self.cb_full)
        form.addRow("Gobble method:", self.cb_strategy)
        form.addRow("Preview:", self.lbl_preview)
        if summary:
            summary_label = QLabel("\n".join(summary))
            summary_label.setWordWrap(True)
            form.addRow("Subprocesses:", summary_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

        self._update_preview()

    def _populate_groups(self, current_gid: str):
        """Populate the group combo-box with available groups."""

        widget = self.cmb_group
        if widget is None:
            return
        block_signals = getattr(widget, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            if hasattr(widget, "clear"):
                widget.clear()
            add_item = getattr(widget, "addItem", None)
            if callable(add_item):
                add_item("Ungrouped", "")
                for grp in getattr(self._config, "groups", []):
                    add_item(grp.name, grp.id)
            gid = current_gid or ""
            find_data = getattr(widget, "findData", None)
            index = find_data(gid) if callable(find_data) else -1
            if index < 0 and gid:
                if callable(add_item):
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

    def _group_name(self, gid: str) -> str:
        """Return the friendly name for ``gid`` if known."""

        for grp in getattr(self._config, "groups", []):
            if grp.id == gid:
                return grp.name
        return gid or "Ungrouped"

    def _update_preview(self):
        """Show how many processes currently match the entered pattern."""

        key = self.le_key.text().strip()
        match_by = self.cmb_match.currentText()
        if not key:
            self.lbl_preview.setText("Matches: 0")
            return
        preview_target = Target(key=key, match_by=match_by, include_children=self._include_children)
        pids = match_pids(preview_target)
        self.lbl_preview.setText(f"Matches: {len(pids)}")

    def get_target(self) -> Target:
        """Construct a new :class:`Target` instance from the dialog fields."""

        key = self.le_key.text().strip()
        match_by = self.cmb_match.currentText()
        target = Target(key=key, match_by=match_by, include_children=bool(self._include_children))
        target.interval_sec = float(self.interval_editor.value())
        target.autostart = bool(self.cb_autostart.isChecked())
        target.cpu_skip_threshold = int(self.sb_cpu.value())
        target.adaptive = bool(self.cb_adaptive.isChecked())
        target.background_only = bool(self.cb_bg.isChecked())
        target.pause_if_fullscreen = bool(self.cb_full.isChecked())
        target.strategy = self.cb_strategy.currentText()
        current_index = self.cmb_group.currentIndex() if hasattr(self.cmb_group, "currentIndex") else 0
        gid = self.cmb_group.itemData(current_index) if hasattr(self.cmb_group, "itemData") else ""
        target.group_id = gid or ""
        return target

    def get_values(self) -> Tuple[str, str, float, bool, bool]:
        """Return legacy tuple values for compatibility with older callers."""

        target = self.get_target()
        return (
            target.key,
            target.match_by,
            float(target.interval_sec),
            bool(target.autostart),
            bool(target.include_children),
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
        self.cmb_theme = QComboBox()
        self.cmb_theme.addItem("Dark", "dark")
        self.cmb_theme.addItem("Light", "light")
        idx = self.cmb_theme.findData(config.theme)
        if idx >= 0:
            self.cmb_theme.setCurrentIndex(idx)
        else:
            fallback = self.cmb_theme.findData("dark")
            if fallback >= 0:
                self.cmb_theme.setCurrentIndex(fallback)
        self.kse = QKeySequenceEdit()
        if config.hotkey:
            self.kse.setKeySequence(config.hotkey)
        self.cb_wake = QCheckBox("Gobble on wake from sleep")
        self.cb_wake.setChecked(bool(config.gobble_on_wake))
        self.cb_show_system = QCheckBox("Show system processes (⚠️)")
        self.cb_show_system.setChecked(bool(config.show_system))
        self.cb_protect_system = QCheckBox("Protect system processes")
        self.cb_protect_system.setChecked(bool(config.protect_system))
        self.cb_pressure = QCheckBox("Enable pressure-aware gobbling")
        self.cb_pressure.setChecked(bool(config.pressure_enabled))
        self.cb_tray_mode = QCheckBox()
        self.cb_tray_mode.setChecked(bool(config.tray_mode))
        self.cb_startup = QCheckBox()
        self.cb_startup.setChecked(bool(config.startup_enabled))
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

        form.addRow("Theme:", self.cmb_theme)
        form.addRow("Global hotkey:", self.kse)
        form.addRow("Wake behaviour:", self.cb_wake)
        form.addRow("Show system processes:", self.cb_show_system)
        form.addRow("Protect system processes:", self.cb_protect_system)
        form.addRow("Pressure-aware gobbling:", self.cb_pressure)
        form.addRow("Minimize to tray on close:", self.cb_tray_mode)
        form.addRow("Start with Windows:", self.cb_startup)
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
        theme = self.cmb_theme.currentData()
        self.cfg.theme = theme if theme in {"light", "dark"} else "dark"
        self.cfg.tray_mode = bool(self.cb_tray_mode.isChecked())
        self.cfg.startup_enabled = bool(self.cb_startup.isChecked())
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
    "ProcessTreeModel",
    "ProcessFilterProxyModel",
    "Sparkline",
    "GroupsSidebar",
    "GroupSettingsDialog",
    "RulesDialog",
    "IntervalEditor",
    "TargetsTable",
    "AddTargetDialog",
    "SettingsDialog",
    "ChartWidget",
    "Dashboard",
    "_InteractionWatcher",
]
