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
import weakref
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..constants import DEFAULT_INTERVAL, MIN_INTERVAL
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
    QColorDialog,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QButtonGroup,
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
    QColor,
    QFrame,
    QLineEdit,
    QKeySequenceEdit,
    QRadioButton,
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
    QVariantAnimation,
    QStackedWidget,
    QEvent,
    QPushButton,
    QSize,
    Signal,
    QEasingCurve,
    QT_AVAILABLE,
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
        """Apply styling that reflects the active theme palette."""

        controller = self._controller
        palette = getattr(controller, "_active_palette", None) if controller else None
        self.apply_palette(palette)

    def apply_palette(self, palette: Optional[Dict[str, str]]):
        """Update the sidebar colours using ``palette`` when available."""

        theme = getattr(self.config, "theme", "dark")
        if theme == "light":
            defaults = {
                "background": "#f1f3f6",
                "surface": "#ffffff",
                "border": "#cdd3de",
                "accent": "#dfe3eb",
                "accent_hover": "#d2d7e1",
                "accent_pressed": "#c4cad6",
                "accent_text": "#1a1c20",
                "text": "#1a1c20",
            }
        else:
            defaults = {
                "background": "#1b1f29",
                "surface": "#11141a",
                "border": "#2a2f3a",
                "accent": "#2b3a55",
                "accent_hover": "#33476a",
                "accent_pressed": "#25364f",
                "accent_text": "#eaeef8",
                "text": "#eaeef8",
            }

        def pick(key: str) -> str:
            if isinstance(palette, dict):
                value = palette.get(key)
                if isinstance(value, str) and value:
                    return value
            return defaults.get(key, "#000000")

        base_bg = pick("background")
        list_bg = pick("surface")
        border = pick("border")
        button_bg = pick("accent")
        button_hover = pick("accent_hover")
        button_press = pick("accent_pressed")
        button_fg = pick("accent_text")
        text_color = pick("text")
        if isinstance(palette, dict):
            raw_border = palette.get("accent_border") or palette.get("border")
            button_border = raw_border if isinstance(raw_border, str) and raw_border else border
        else:
            button_border = border

        self.setStyleSheet(
            """
            QWidget#groupSettingsPanel {
                background-color: %s;
                border-radius: 14px;
                color: %s;
            }
            QListWidget#groupSettingsList {
                background-color: %s;
                border: 1px solid %s;
                border-radius: 10px;
                padding: 6px;
                color: %s;
            }
            QWidget#groupSettingsButtons {
                background-color: %s;
                border-top: 1px solid %s;
                padding: 12px 0;
                color: %s;
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
                text_color,
                list_bg,
                border,
                text_color,
                base_bg,
                border,
                text_color,
                button_bg,
                button_fg,
                button_border,
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

        self.apply_palette(getattr(main_window, "_active_palette", None))

    def apply_palette(self, palette: Optional[Dict[str, str]]):
        """Propagate palette updates to the embedded sidebar."""

        sidebar = getattr(self, "sidebar", None)
        if sidebar is not None and hasattr(sidebar, "apply_palette"):
            try:
                sidebar.apply_palette(palette)
            except Exception:
                pass


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


class ToggleSwitch(QPushButton):
    """Rounded toggle styled to resemble a modern on/off slider."""

    stateChanged = Signal(int)

    def __init__(self, parent=None):
        """Prepare the button with custom styling and behaviour."""

        super().__init__("", parent)
        self._margin = 2.0
        self._preferred_width = 30
        self._preferred_height = 16
        self._checked = False
        self._block_emit = False
        self._native_signals_work = False
        self._anim_progress = 0.0
        self._animation = None
        self._track_off_color = "#0c0f19"
        self._track_on_color = "#aebff5"

        set_checkable = getattr(self, "setCheckable", None)
        if callable(set_checkable):
            try:
                set_checkable(True)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        cursor = getattr(Qt, "PointingHandCursor", None)
        if cursor is not None and hasattr(self, "setCursor"):
            try:
                self.setCursor(cursor)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        if hasattr(self, "setFocusPolicy") and hasattr(Qt, "NoFocus"):
            try:
                self.setFocusPolicy(Qt.NoFocus)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        if hasattr(self, "setMinimumHeight"):
            try:
                self.setMinimumHeight(self._preferred_height)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        toggled_signal = getattr(self, "toggled", None)
        if toggled_signal is not None and hasattr(toggled_signal, "connect"):
            try:
                toggled_signal.connect(self._on_native_toggled)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        clicked_signal = getattr(self, "clicked", None)
        if clicked_signal is not None and hasattr(clicked_signal, "connect"):
            try:
                clicked_signal.connect(self._on_clicked)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        if hasattr(self, "setToolTip"):
            try:
                self.setToolTip("Enable or disable auto-start for this target")
            except Exception:  # pragma: no cover - defensive fallback
                pass

        self._setup_animation()
        self._set_anim_progress(1.0 if self._checked else 0.0)

    # -- Basic API -----------------------------------------------------
    def sizeHint(self):  # noqa: D401 - Qt API
        """Return the preferred control dimensions."""

        if "QSize" in globals():
            try:
                return QSize(self._preferred_width, self._preferred_height)
            except Exception:  # pragma: no cover - defensive fallback
                pass
        base_hint = getattr(super(), "sizeHint", None)
        return base_hint() if callable(base_hint) else None

    def minimumSizeHint(self):  # noqa: D401 - Qt API
        """Match :meth:`sizeHint` so layouts keep the switch visible."""

        return self.sizeHint()

    def isChecked(self) -> bool:  # noqa: D401 - Qt API
        """Expose the current on/off state."""

        return bool(self._checked)

    def setChecked(self, checked: bool):  # noqa: D401 - Qt API
        """Update the toggle state and refresh the visuals."""

        new_val = bool(checked)
        if new_val == self._checked:
            setter = getattr(super(), "setChecked", None)
            if callable(setter):
                try:
                    self._block_emit = True
                    setter(new_val)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
                finally:
                    self._block_emit = False
            self._set_anim_progress(self._target_progress())
            return

        self._checked = new_val
        setter = getattr(super(), "setChecked", None)
        if callable(setter):
            try:
                self._block_emit = True
                setter(new_val)
            except Exception:  # pragma: no cover - defensive fallback
                pass
            finally:
                self._block_emit = False
        self._emit_state_changed()

    def set_track_colors(self, *, off: Optional[str] = None, on: Optional[str] = None) -> None:
        """Adjust the colours used for the track background."""

        changed = False
        if isinstance(off, str) and off:
            if off != getattr(self, "_track_off_color", "#0c0f19"):
                self._track_off_color = off
                changed = True
        if isinstance(on, str) and on:
            if on != getattr(self, "_track_on_color", "#aebff5"):
                self._track_on_color = on
                changed = True
        if changed:
            self._refresh_visuals()

    # -- Internal helpers ---------------------------------------------
    def _on_native_toggled(self, checked: bool):
        """Synchronise the cached state when Qt emits ``toggled``."""

        self._native_signals_work = True
        self._checked = bool(checked)
        if self._block_emit:
            return
        self._emit_state_changed()

    def _on_clicked(self, *args):  # noqa: D401 - Qt API
        """Fallback toggle when the native ``toggled`` signal is absent."""

        if self._native_signals_work:
            return
        self.setChecked(not self._checked)

    def _emit_state_changed(self) -> None:
        """Emit :attr:`stateChanged` and trigger a repaint."""

        checked_state = getattr(Qt, "Checked", None) if self._checked else getattr(Qt, "Unchecked", None)
        if not isinstance(checked_state, int):
            checked_state = 2 if self._checked else 0
        try:
            self.stateChanged.emit(int(checked_state))
        except Exception:  # pragma: no cover - defensive fallback
            pass
        self._start_animation()

    def _refresh_visuals(self) -> None:
        """Schedule a repaint so the slider reflects the current state."""

        update = getattr(self, "update", None)
        if callable(update):
            try:
                update()
            except Exception:  # pragma: no cover - defensive fallback
                pass

    def _setup_animation(self) -> None:
        """Prepare an easing animation when Qt is available."""

        animation_cls = globals().get("QVariantAnimation")
        if animation_cls is None:
            return
        try:
            animation = animation_cls(self)
        except Exception:  # pragma: no cover - defensive fallback
            return

        self._animation = animation
        set_duration = getattr(animation, "setDuration", None)
        if callable(set_duration):
            try:
                set_duration(180)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        easing_curve = globals().get("QEasingCurve")
        set_easing = getattr(animation, "setEasingCurve", None)
        curve_value = getattr(easing_curve, "InOutCubic", None) if easing_curve is not None else None
        if callable(set_easing) and curve_value is not None:
            try:
                set_easing(curve_value)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        value_changed = getattr(animation, "valueChanged", None)
        if value_changed is None or not hasattr(value_changed, "connect"):
            self._animation = None
            return
        try:
            value_changed.connect(self._set_anim_progress)
        except Exception:  # pragma: no cover - defensive fallback
            self._animation = None
            return

        set_start = getattr(animation, "setStartValue", None)
        if callable(set_start):
            try:
                set_start(self._anim_progress)
            except Exception:  # pragma: no cover - defensive fallback
                pass
        set_end = getattr(animation, "setEndValue", None)
        if callable(set_end):
            try:
                set_end(self._anim_progress)
            except Exception:  # pragma: no cover - defensive fallback
                pass

    def _target_progress(self) -> float:
        """Return the target knob position for the current state."""

        return 1.0 if self._checked else 0.0

    def _start_animation(self) -> None:
        """Animate the knob towards the target state if possible."""

        target = self._target_progress()
        animation = self._animation
        if animation is None:
            self._set_anim_progress(target)
            return

        try:
            animation.stop()
        except Exception:  # pragma: no cover - defensive fallback
            self._animation = None
            self._set_anim_progress(target)
            return

        start_value = getattr(self, "_anim_progress", target)
        set_start = getattr(animation, "setStartValue", None)
        if callable(set_start):
            try:
                set_start(start_value)
            except Exception:  # pragma: no cover - defensive fallback
                pass
        set_end = getattr(animation, "setEndValue", None)
        if callable(set_end):
            try:
                set_end(target)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        try:
            animation.start()
        except Exception:  # pragma: no cover - defensive fallback
            self._animation = None
            self._set_anim_progress(target)

    def _set_anim_progress(self, value) -> None:
        """Update the knob position and schedule a repaint."""

        try:
            fraction = float(value)
        except Exception:  # pragma: no cover - defensive fallback
            return
        fraction = max(0.0, min(1.0, fraction))
        self._anim_progress = fraction
        self._refresh_visuals()

    def _color(self, value: str, *, alpha: int | None = None):
        """Safely construct :class:`QColor` instances when available."""

        color_cls = globals().get("QColor")
        if color_cls is None:
            return None
        try:
            color = color_cls(value)
        except Exception:  # pragma: no cover - defensive fallback
            return None
        if alpha is not None and hasattr(color, "setAlpha"):
            try:
                color.setAlpha(alpha)
            except Exception:  # pragma: no cover - defensive fallback
                pass
        return color

    def _blend_colors(self, start_hex: str, end_hex: str, fraction: float):
        """Interpolate between two colors using the provided fraction."""

        try:
            fraction = float(fraction)
        except Exception:  # pragma: no cover - defensive fallback
            fraction = 0.0
        fraction = max(0.0, min(1.0, fraction))

        color_cls = globals().get("QColor")
        if color_cls is None:
            return self._color(end_hex if fraction >= 0.5 else start_hex)

        start_color = self._color(start_hex)
        end_color = self._color(end_hex)
        if start_color is None or end_color is None:
            return end_color if fraction >= 0.5 else start_color

        try:
            start_red = start_color.redF()
            start_green = start_color.greenF()
            start_blue = start_color.blueF()
            start_alpha = start_color.alphaF()
            end_red = end_color.redF()
            end_green = end_color.greenF()
            end_blue = end_color.blueF()
            end_alpha = end_color.alphaF()
        except Exception:  # pragma: no cover - defensive fallback
            return end_color if fraction >= 0.5 else start_color

        red = start_red + (end_red - start_red) * fraction
        green = start_green + (end_green - start_green) * fraction
        blue = start_blue + (end_blue - start_blue) * fraction
        alpha = start_alpha + (end_alpha - start_alpha) * fraction

        from_rgb = getattr(color_cls, "fromRgbF", None)
        if callable(from_rgb):
            try:
                return from_rgb(red, green, blue, alpha)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        try:
            blended = color_cls()
        except Exception:  # pragma: no cover - defensive fallback
            return end_color if fraction >= 0.5 else start_color

        for setter_name, channel_value in (
            ("setRedF", red),
            ("setGreenF", green),
            ("setBlueF", blue),
        ):
            setter = getattr(blended, setter_name, None)
            if callable(setter):
                try:
                    setter(channel_value)
                except Exception:  # pragma: no cover - defensive fallback
                    pass

        alpha_setter = getattr(blended, "setAlphaF", None)
        if callable(alpha_setter):
            try:
                alpha_setter(alpha)
            except Exception:  # pragma: no cover - defensive fallback
                pass

        return blended

    # -- Painting ------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 - Qt API
        """Render the rounded track and circular thumb."""

        painter = QPainter(self)
        try:
            set_render = getattr(painter, "setRenderHint", None)
            antialias = getattr(QPainter, "Antialiasing", None)
            if callable(set_render) and antialias is not None:
                try:
                    set_render(antialias, True)
                except Exception:  # pragma: no cover - defensive fallback
                    pass

            available_width = float(self.width() if hasattr(self, "width") else self._preferred_width)
            available_height = float(self.height() if hasattr(self, "height") else self._preferred_height)
            base_width = float(getattr(self, "_preferred_width", 0) or 0)
            base_height = float(getattr(self, "_preferred_height", 0) or 0)
            base_width = base_width if base_width > 0 else 30.0
            base_height = base_height if base_height > 0 else 16.0
            scale_candidates = [1.0]
            if available_width > 0:
                scale_candidates.append(available_width / base_width)
            if available_height > 0:
                scale_candidates.append(available_height / base_height)
            scale = min(scale_candidates)
            if scale <= 0:
                scale = 1.0
            width = max(base_width * scale, 1.0)
            height = max(base_height * scale, 1.0)
            x_offset = (available_width - width) / 2.0
            y_offset = (available_height - height) / 2.0
            radius = height / 2.0
            margin = max(self._margin * scale, height * 0.18)
            margin = min(margin, max(radius - 1.0 * scale, 1.0 * scale))
            knob_diameter = max(height - 2.0 * margin, height * 0.58)
            knob_diameter = min(knob_diameter, width - 2.0 * margin)
            travel = max(width - 2.0 * margin - knob_diameter, 0.0)
            progress = getattr(self, "_anim_progress", 1.0 if self._checked else 0.0)
            try:
                progress = float(progress)
            except Exception:  # pragma: no cover - defensive fallback
                progress = 0.0
            progress = max(0.0, min(1.0, progress))
            knob_x = x_offset + margin + travel * progress
            knob_y = y_offset + (height - knob_diameter) / 2.0

            off_hex = getattr(self, "_track_off_color", "#0c0f19") or "#0c0f19"
            on_hex = getattr(self, "_track_on_color", "#aebff5") or "#aebff5"
            track_color = self._blend_colors(off_hex, on_hex, progress)
            if track_color is None:
                fallback = on_hex if progress >= 0.5 else off_hex
                track_color = self._color(fallback)

            set_pen = getattr(painter, "setPen", None)
            set_brush = getattr(painter, "setBrush", None)
            no_pen = getattr(Qt, "NoPen", None)
            if callable(set_pen):
                try:
                    set_pen(no_pen)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            if callable(set_brush) and track_color is not None:
                try:
                    set_brush(track_color)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            draw_rounded = getattr(painter, "drawRoundedRect", None)
            if callable(draw_rounded):
                try:
                    draw_rounded(x_offset + 0.5, y_offset + 0.5, width - 1.0, height - 1.0, radius, radius)
                except Exception:  # pragma: no cover - defensive fallback
                    pass

            save = getattr(painter, "save", None)
            restore = getattr(painter, "restore", None)
            if callable(save) and callable(restore):
                try:
                    save()
                    highlight = self._color("#FFFFFF", alpha=45)
                    if callable(set_brush) and highlight is not None:
                        set_brush(highlight)
                    opacity = getattr(painter, "setOpacity", None)
                    if callable(opacity):
                        try:
                            opacity(0.3)
                        except Exception:  # pragma: no cover - defensive fallback
                            pass
                    if callable(draw_rounded):
                        try:
                            highlight_height = max((height - 2.0 * scale) * 0.45, 0.0)
                            draw_rounded(
                                x_offset + 1.0,
                                y_offset + 1.0,
                                width - 2.0,
                                highlight_height,
                                max(radius - 1.2 * scale, 0.0),
                                max(radius - 1.2 * scale, 0.0),
                            )
                        except Exception:  # pragma: no cover - defensive fallback
                            pass
                finally:
                    restore()

            shadow_color = self._color("#000000", alpha=55)
            if callable(set_pen):
                try:
                    set_pen(no_pen)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            if callable(set_brush) and shadow_color is not None:
                try:
                    set_brush(shadow_color)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            draw_ellipse = getattr(painter, "drawEllipse", None)
            if callable(draw_ellipse):
                try:
                    shadow_offset = max(height * 0.08, 1.0 * scale)
                    draw_ellipse(knob_x, knob_y + shadow_offset, knob_diameter, knob_diameter)
                except Exception:  # pragma: no cover - defensive fallback
                    pass

            knob_color = self._color("#f8f9ff")
            knob_pen = QPen()
            border_color = self._color("#c6ccdd")
            set_color = getattr(knob_pen, "setColor", None)
            if callable(set_color) and border_color is not None:
                try:
                    set_color(border_color)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            set_width = getattr(knob_pen, "setWidthF", None)
            border_width = max(height * 0.045, 0.6 * scale)
            if callable(set_width):
                try:
                    set_width(border_width)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            if callable(set_pen):
                try:
                    set_pen(knob_pen)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            if callable(set_brush) and knob_color is not None:
                try:
                    set_brush(knob_color)
                except Exception:  # pragma: no cover - defensive fallback
                    pass
            if callable(draw_ellipse):
                try:
                    draw_ellipse(knob_x, knob_y, knob_diameter, knob_diameter)
                except Exception:  # pragma: no cover - defensive fallback
                    pass

            if callable(save) and callable(restore):
                try:
                    save()
                    highlight_color = self._color("#FFFFFF", alpha=120)
                    if callable(set_pen):
                        try:
                            set_pen(no_pen)
                        except Exception:  # pragma: no cover - defensive fallback
                            pass
                    if callable(set_brush) and highlight_color is not None:
                        try:
                            set_brush(highlight_color)
                        except Exception:  # pragma: no cover - defensive fallback
                            pass
                    if callable(draw_ellipse):
                        try:
                            inset = knob_diameter * 0.24
                            draw_ellipse(
                                knob_x + inset,
                                knob_y + inset,
                                knob_diameter - 2 * inset,
                                knob_diameter - 2 * inset,
                            )
                        except Exception:  # pragma: no cover - defensive fallback
                            pass
                finally:
                    restore()
        finally:
            end = getattr(painter, "end", None)
            if callable(end):
                try:
                    end()
                except Exception:  # pragma: no cover - defensive fallback
                    pass


class _IntervalStepButton(QPushButton):
    """Small push button that always draws a bold black glyph."""

    def __init__(self, symbol: str, parent=None, *, accessible_name: Optional[str] = None):
        super().__init__("", parent)
        self._symbol = symbol
        self._glyph_active = "#000000"
        self._glyph_disabled = "#6B6B6B"
        if accessible_name:
            setter = getattr(self, "setAccessibleName", None)
            if callable(setter):
                try:
                    setter(accessible_name)
                except Exception:  # pragma: no cover - defensive
                    pass
            tooltip_setter = getattr(self, "setToolTip", None)
            if callable(tooltip_setter):
                try:
                    tooltip_setter(accessible_name)
                except Exception:  # pragma: no cover - defensive
                    pass

    def set_glyph_colors(
        self,
        *,
        active: Optional[str] = None,
        disabled: Optional[str] = None,
    ) -> None:
        """Update the glyph colours used for the active and disabled states."""

        changed = False
        if isinstance(active, str) and active.strip():
            value = active.strip()
            if value != self._glyph_active:
                self._glyph_active = value
                changed = True
        if isinstance(disabled, str) and disabled.strip():
            value = disabled.strip()
            if value != self._glyph_disabled:
                self._glyph_disabled = value
                changed = True
        if changed and hasattr(self, "update"):
            try:
                self.update()
            except Exception:  # pragma: no cover - defensive
                pass

    def paintEvent(self, event):  # noqa: N802 - Qt API
        """Paint the standard button then overlay the glyph in the configured colour."""

        super().paintEvent(event)
        painter = QPainter(self)
        try:
            set_render = getattr(painter, "setRenderHint", None)
            antialias = getattr(QPainter, "Antialiasing", None)
            if callable(set_render) and antialias is not None:
                try:
                    set_render(antialias, True)
                except Exception:  # pragma: no cover - defensive
                    pass

            pen = QPen()
            set_color = getattr(pen, "setColor", None)
            color_value = self._glyph_disabled if not self.isEnabled() else self._glyph_active
            if callable(set_color):
                try:
                    qcolor = QColor(color_value)
                except Exception:  # pragma: no cover - defensive
                    qcolor = None
                if isinstance(qcolor, QColor) and qcolor.isValid():
                    try:
                        set_color(qcolor)
                    except Exception:  # pragma: no cover - defensive
                        pass
                else:
                    fallback = getattr(Qt, "black", None)
                    if fallback is not None:
                        try:
                            set_color(fallback)
                        except Exception:  # pragma: no cover - defensive
                            pass
            set_pen = getattr(painter, "setPen", None)
            if callable(set_pen):
                try:
                    set_pen(pen)
                except Exception:  # pragma: no cover - defensive
                    pass

            font_getter = getattr(painter, "font", None)
            font = font_getter() if callable(font_getter) else None
            if font is not None:
                set_bold = getattr(font, "setBold", None)
                if callable(set_bold):
                    try:
                        set_bold(True)
                    except Exception:  # pragma: no cover - defensive
                        pass
                size = 0
                point_size = getattr(font, "pointSize", None)
                if callable(point_size):
                    try:
                        size = point_size()
                    except Exception:  # pragma: no cover - defensive
                        size = 0
                if size <= 0:
                    point_size_f = getattr(font, "pointSizeF", None)
                    if callable(point_size_f):
                        try:
                            size = int(point_size_f())
                        except Exception:  # pragma: no cover - defensive
                            size = 0
                target_size = 14 if size <= 0 else max(size, 14)
                set_point = getattr(font, "setPointSize", None)
                if callable(set_point):
                    try:
                        set_point(target_size)
                    except Exception:  # pragma: no cover - defensive
                        pass
                else:
                    set_pointf = getattr(font, "setPointSizeF", None)
                    if callable(set_pointf):
                        try:
                            set_pointf(float(target_size))
                        except Exception:  # pragma: no cover - defensive
                            pass
                set_font = getattr(painter, "setFont", None)
                if callable(set_font):
                    try:
                        set_font(font)
                    except Exception:  # pragma: no cover - defensive
                        pass

            rect_getter = getattr(self, "rect", None)
            draw_text = getattr(painter, "drawText", None)
            if callable(draw_text) and callable(rect_getter):
                rect = rect_getter()
                align = getattr(Qt, "AlignCenter", None)
                try:
                    if align is None:
                        draw_text(rect, self._symbol)
                    else:
                        draw_text(rect, align, self._symbol)
                except Exception:  # pragma: no cover - defensive
                    pass
        finally:
            end = getattr(painter, "end", None)
            if callable(end):
                try:
                    end()
                except Exception:  # pragma: no cover - defensive
                    pass


class IntervalEditor(QWidget):
    """Composite widget for editing float intervals with separate controls."""

    valueChanged = Signal(float)
    editingFinished = Signal()
    _instances = weakref.WeakSet()
    _default_palette = {
        "base": "#AEBFF5",
        "border": "#8C9FD9",
        "hover": "#9DAFDC",
        "pressed": "#8C9FD9",
        "text": "#000000",
        "disabled": "#D0DAF9",
        "disabled_text": "#6B6B6B",
    }

    def __init__(
        self,
        value: float,
        parent=None,
        *,
        minimum: float = MIN_INTERVAL,
        maximum: float = 600.0,
        step: float = 0.1,
    ):
        super().__init__(parent)
        try:
            type(self)._instances.add(self)
        except Exception:  # pragma: no cover - defensive
            pass
        self._min = float(minimum)
        self._max = float(maximum)
        self._step = float(step) if step > 0 else 0.1
        self._value = self._snap(self._clamp(value))
        base_palette = getattr(type(self), "_default_palette", {}) or {}
        self._button_palette = dict(base_palette)
        self._button_palette.setdefault("_border_explicit", False)
        if not self._button_palette:
            self._button_palette = {
                "base": "#AEBFF5",
                "border": "#8C9FD9",
                "hover": "#9DAFDC",
                "pressed": "#8C9FD9",
                "text": "#000000",
                "disabled": "#D0DAF9",
                "disabled_text": "#6B6B6B",
            }

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.line_edit = QLineEdit(self)
        name_setter = getattr(self.line_edit, "setObjectName", None)
        if callable(name_setter):
            try:
                name_setter(f"intervalEditorDisplay_{uuid.uuid4().hex}")
            except Exception:  # pragma: no cover - defensive
                pass
        if hasattr(self, "setFocusProxy"):
            try:
                self.setFocusProxy(self.line_edit)
            except Exception:  # pragma: no cover - defensive
                pass
        if hasattr(self.line_edit, "setAlignment") and hasattr(Qt, "AlignRight"):
            self.line_edit.setAlignment(Qt.AlignRight)
        if hasattr(self.line_edit, "setFixedHeight"):
            self.line_edit.setFixedHeight(24)
        layout.addWidget(self.line_edit)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(4)
        self._button_layout = btn_layout

        self.btn_down = _IntervalStepButton(
            "-",
            self,
            accessible_name="Decrease interval",
        )
        self.btn_up = _IntervalStepButton(
            "+",
            self,
            accessible_name="Increase interval",
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
                btn.setFixedWidth(28)
            if hasattr(btn, "setFixedHeight"):
                btn.setFixedHeight(24)
            if hasattr(btn, "setFont"):
                font = btn.font()
                font.setBold(True)
                if font.pointSize() <= 0:
                    font.setPointSize(12)
                else:
                    font.setPointSize(max(font.pointSize(), 12))
                btn.setFont(font)
            btn_layout.addWidget(btn)

        layout.addLayout(btn_layout)

        if hasattr(self.btn_up, "clicked"):
            self.btn_up.clicked.connect(self._on_step_up)
        if hasattr(self.btn_down, "clicked"):
            self.btn_down.clicked.connect(self._on_step_down)
        if hasattr(self.line_edit, "editingFinished"):
            self.line_edit.editingFinished.connect(self._on_edit_finished)

        self._apply_button_styles()
        self._apply_line_edit_styles()
        self._update_display()

    @classmethod
    def apply_theme_palette(cls, palette: Dict[str, str]) -> None:
        """Update all instances to match the supplied theme palette."""

        if not isinstance(palette, dict):
            return
        updates = {
            "base": palette.get("accent") or cls._default_palette.get("base", "#AEBFF5"),
            "border": palette.get("accent_border")
            or cls._default_palette.get("border", "#8C9FD9"),
            "hover": palette.get("accent_hover")
            or cls._default_palette.get("hover", "#9DAFDC"),
            "pressed": palette.get("accent_pressed")
            or cls._default_palette.get("pressed", "#8C9FD9"),
            "text": palette.get("accent_text") or cls._default_palette.get("text", "#000000"),
            "disabled": palette.get("disabled_background")
            or cls._default_palette.get("disabled", "#D0DAF9"),
            "disabled_text": palette.get("disabled_text")
            or cls._default_palette.get("disabled_text", "#6B6B6B"),
        }
        text_colour = updates.get("text")
        if isinstance(text_colour, str) and text_colour.strip():
            updates["border"] = text_colour.strip()
        cls._default_palette.update(updates)
        try:
            instances = list(cls._instances)
        except Exception:  # pragma: no cover - defensive
            instances = []
        for widget in instances:
            if isinstance(widget, IntervalEditor):
                widget.set_button_palette(**updates, link_border_to_text=True)

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
        inner_slack = 0
        if font_metrics is not None:
            advance = getattr(font_metrics, "horizontalAdvance", None)
            sample_text = "000.0"
            if callable(advance):
                try:
                    sample = advance(sample_text)
                except Exception:  # pragma: no cover - defensive
                    sample = 0
            else:
                width_fn = getattr(font_metrics, "width", None)
                sample = width_fn(sample_text) if callable(width_fn) else 0
            width = max(width, sample)
            inner_slack = max(inner_slack, 6)
        else:
            inner_slack = 6
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
            extra_padding = 12
        style_getter = getattr(self.line_edit, "style", None)
        style = style_getter() if callable(style_getter) else None
        if style and hasattr(style, "pixelMetric") and hasattr(QStyle, "PM_DefaultFrameWidth"):
            try:
                frame = style.pixelMetric(QStyle.PM_DefaultFrameWidth, None, self.line_edit)
            except Exception:  # pragma: no cover - defensive
                frame = 0
            extra_padding += frame * 2
        total_width = width + extra_padding + inner_slack
        if hasattr(self.line_edit, "setMinimumWidth"):
            self.line_edit.setMinimumWidth(total_width)
        if hasattr(self.line_edit, "setMaximumWidth"):
            self.line_edit.setMaximumWidth(total_width)
        extra = 0
        buttons: Iterable = getattr(self, "btn_down", None), getattr(self, "btn_up", None)
        for btn in buttons:
            if not btn:
                continue
            size_hint = getattr(btn, "sizeHint", None)
            if callable(size_hint):
                try:
                    extra += size_hint().width()
                    continue
                except Exception:  # pragma: no cover - defensive
                    pass
            extra += 28
        button_layout = getattr(self, "_button_layout", None)
        if button_layout is not None:
            spacing = getattr(button_layout, "spacing", None)
            if callable(spacing):
                try:
                    extra += spacing()
                except Exception:  # pragma: no cover - defensive
                    pass
        main_layout = None
        layout_getter = getattr(self, "layout", None)
        if callable(layout_getter):
            try:
                main_layout = layout_getter()
            except Exception:  # pragma: no cover - defensive
                main_layout = None
        if main_layout is not None:
            spacing = getattr(main_layout, "spacing", None)
            if callable(spacing):
                try:
                    extra += spacing()
                except Exception:  # pragma: no cover - defensive
                    pass
        extra += 6  # breathing room so buttons do not crowd the line edit
        widget_width = total_width + extra
        if hasattr(self, "setMinimumWidth"):
            self.setMinimumWidth(widget_width)
        if hasattr(self, "setMaximumWidth"):
            self.setMaximumWidth(widget_width)

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

    def set_button_palette(
        self,
        *,
        base: Optional[str] = None,
        border: Optional[str] = None,
        hover: Optional[str] = None,
        pressed: Optional[str] = None,
        text: Optional[str] = None,
        disabled: Optional[str] = None,
        disabled_text: Optional[str] = None,
        link_border_to_text: bool = False,
    ) -> None:
        """Apply per-instance overrides for the step button palette."""

        palette = getattr(self, "_button_palette", {})
        if not isinstance(palette, dict):
            palette = {}
            self._button_palette = palette
        updates = {
            "base": base,
            "border": border,
            "hover": hover,
            "pressed": pressed,
            "text": text,
            "disabled": disabled,
            "disabled_text": disabled_text,
        }
        changed = False
        text_changed = False
        border_explicit = not link_border_to_text and isinstance(border, str) and border.strip()
        for key, value in updates.items():
            if isinstance(value, str):
                candidate = value.strip()
                if candidate and candidate != palette.get(key):
                    palette[key] = candidate
                    changed = True
                    if key == "text":
                        text_changed = True
                    if key == "border" and not link_border_to_text:
                        palette["_border_explicit"] = True
        if changed:
            if text_changed and not border_explicit:
                text_colour = palette.get("text")
                if text_colour and palette.get("border") != text_colour:
                    palette["border"] = text_colour
                    palette["_border_explicit"] = False
            elif link_border_to_text and palette.get("text"):
                palette["border"] = palette["text"]
                palette["_border_explicit"] = False
            self._apply_button_styles()

    def _apply_button_styles(self) -> None:
        """Sync the step buttons with the stored palette."""

        palette = getattr(self, "_button_palette", {}) or {}
        for key, fallback in IntervalEditor._default_palette.items():
            palette.setdefault(key, fallback)
        text_color = palette.get("text", IntervalEditor._default_palette.get("text", "#000000"))
        stylesheet = (
            "QPushButton {"
            f"background-color: {palette['base']};"
            "border: none;"
            "border-radius: 4px;"
            "font-size: 16px;"
            "font-weight: bold;"
            f"color: {text_color};"
            "}"
            "QPushButton:hover {"
            f"background-color: {palette['hover']};"
            f"color: {text_color};"
            "}"
            "QPushButton:pressed {"
            f"background-color: {palette['pressed']};"
            f"color: {text_color};"
            "}"
            "QPushButton:disabled {"
            f"background-color: {palette['disabled']};"
            f"color: {palette['disabled_text']};"
            "}"
        )
        for btn in (getattr(self, "btn_down", None), getattr(self, "btn_up", None)):
            if btn is None:
                continue
            setter = getattr(btn, "setStyleSheet", None)
            if callable(setter):
                try:
                    setter(stylesheet)
                except Exception:  # pragma: no cover - defensive
                    pass
            glyph_setter = getattr(btn, "set_glyph_colors", None)
            if callable(glyph_setter):
                try:
                    glyph_setter(active=palette.get("text"), disabled=palette.get("disabled_text"))
                except Exception:  # pragma: no cover - defensive
                    pass

        self._apply_line_edit_styles()

    def _apply_line_edit_styles(self) -> None:
        """Ensure the interval display matches the themed accent colours."""

        line_edit = getattr(self, "line_edit", None)
        if line_edit is None:
            return

        palette = dict(IntervalEditor._default_palette)
        button_palette = getattr(self, "_button_palette", None)
        if isinstance(button_palette, dict):
            for key, value in button_palette.items():
                if isinstance(value, str) and value.strip():
                    palette[key] = value.strip()

        text_color = palette.get("text", "#000000")
        border_color = text_color
        hover_color = text_color
        focus_color = text_color
        disabled_color = palette.get("disabled_text", "#6B6B6B")

        padding = "2px 8px"
        border_radius = "12px"

        object_name = ""
        name_getter = getattr(line_edit, "objectName", None)
        if callable(name_getter):
            try:
                object_name = name_getter() or ""
            except Exception:  # pragma: no cover - defensive
                object_name = ""
        if not object_name:
            name_setter = getattr(line_edit, "setObjectName", None)
            if callable(name_setter):
                try:
                    object_name = f"intervalEditorDisplay_{uuid.uuid4().hex}"
                    name_setter(object_name)
                except Exception:  # pragma: no cover - defensive
                    object_name = ""
        selector = f"QLineEdit#{object_name}" if object_name else "QLineEdit"

        stylesheet = (
            f"{selector} {{"
            f"border: 1px solid {border_color};"
            f"border-radius: {border_radius};"
            f"color: {text_color};"
            f"padding: {padding};"
            "}"
            f"{selector}:hover {{"
            f"border-color: {hover_color};"
            "}"
            f"{selector}:focus {{"
            f"border-color: {focus_color};"
            "}"
            f"{selector}:disabled {{"
            f"border-color: {disabled_color};"
            f"color: {disabled_color};"
            "}"
        )

        setter = getattr(line_edit, "setStyleSheet", None)
        if callable(setter):
            try:
                setter(stylesheet)
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
        self._toggle_widgets: List[ToggleSwitch] = []
        self._accent_color: Optional[str] = None
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

    def set_accent_color(self, color: str) -> None:
        """Update the accent colour used by toggle switches."""

        if not isinstance(color, str) or not color:
            return
        if color == getattr(self, "_accent_color", None):
            return
        self._accent_color = color
        for toggle in list(self._toggle_widgets):
            if hasattr(toggle, "set_track_colors"):
                try:
                    toggle.set_track_colors(on=color)
                except Exception:
                    continue

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
            self._toggle_widgets.clear()
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

        autostart_cb = ToggleSwitch()
        autostart_cb.setChecked(bool(target.autostart))
        autostart_cb.stateChanged.connect(
            lambda _=None, i=idx, cb=autostart_cb: self._on_autostart(i, cb.isChecked())
        )
        self._register_toggle(autostart_cb)

        interval_widget = IntervalEditor(
            target.interval_sec,
            minimum=MIN_INTERVAL,
            maximum=600.0,
            step=0.1,
        )
        interval_widget.setToolTip("How often to gobble memory for this target")
        metrics = interval_widget.fontMetrics()
        try:
            sample_width = max(
                metrics.horizontalAdvance("000.0"),
                metrics.horizontalAdvance("600"),
            )
        except AttributeError:  # pragma: no cover - Qt6 preferred API
            sample_width = max(
                metrics.width("000.0"),  # type: ignore[attr-defined]
                metrics.width("600"),  # type: ignore[attr-defined]
            )
        interval_widget.set_display_width(sample_width)
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

    def _register_toggle(self, toggle: ToggleSwitch) -> None:
        """Track toggle widgets so palette updates can adjust their colours."""

        if not toggle:
            return
        if toggle not in self._toggle_widgets:
            self._toggle_widgets.append(toggle)
        accent = getattr(self, "_accent_color", None)
        if accent and hasattr(toggle, "set_track_colors"):
            try:
                toggle.set_track_colors(on=accent)
            except Exception:
                pass

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
            text = f"{state} — {message}" if message else state
            status_item.setText(text)
            status_item.setToolTip(text)

        interval_widget = self.cellWidget(row, 5)
        if isinstance(interval_widget, IntervalEditor):
            try:
                current = float(interval_widget.value())
            except Exception:
                current = float("nan")
            desired = float(getattr(target, "interval_sec", 0.0))
            if not (abs(current - desired) < 1e-6):
                try:
                    interval_widget.setValue(desired)
                except Exception:
                    pass

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

        self.interval_editor = IntervalEditor(
            DEFAULT_INTERVAL,
            minimum=MIN_INTERVAL,
            maximum=600.0,
            step=0.1,
        )
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

    def __init__(
        self,
        config: Config,
        parent=None,
        *,
        on_open_color_settings: Optional[Callable[[], None]] = None,
        on_import_settings: Optional[Callable[[], None]] = None,
        on_export_settings: Optional[Callable[[], None]] = None,
        on_open_group_settings: Optional[Callable[[], None]] = None,
        on_preview_theme: Optional[Callable[[str], None]] = None,
    ):
        """Build the settings form and seed it from ``config``."""

        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self.cfg = config
        self._on_open_color_settings = on_open_color_settings
        self._on_import_settings = on_import_settings
        self._on_export_settings = on_export_settings
        self._on_open_group_settings = on_open_group_settings
        self._on_preview_theme = on_preview_theme
        self._loading_theme = False

        form = QFormLayout(self)
        self.cmb_theme = QComboBox()
        self.cmb_theme.addItem("Dark", "dark")
        self.cmb_theme.addItem("Light", "light")
        self.cmb_theme.addItem("👑 Custom", "custom")
        self.cmb_theme.currentIndexChanged.connect(self._handle_theme_changed)
        self.kse = QKeySequenceEdit()
        self.cb_wake = QCheckBox("Gobble on wake from sleep")
        self.cb_show_system = QCheckBox("Show system processes (⚠️)")
        self.cb_protect_system = QCheckBox("Protect system processes")
        self.cb_pressure = QCheckBox("Enable pressure-aware gobbling")
        self.cb_tray_mode = QCheckBox()
        self.cb_startup = QCheckBox()
        self.sb_min_free = QSpinBox()
        self.sb_min_free.setRange(5, 80)
        self.cb_gaming = QCheckBox("Pause during fullscreen apps")
        self.cmb_batt = QComboBox()
        self.cmb_batt.addItems(["off", "slow", "pause"])
        self.sb_batt_factor = QSpinBox()
        self.sb_batt_factor.setRange(1, 4)

        self._loading_theme = True
        self._load_from_config()
        self._loading_theme = False

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

        actions = QWidget()
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(0, 12, 0, 12)
        actions_layout.setSpacing(8)

        self.btn_color_settings = QPushButton("👑 Color Settings")
        self.btn_color_settings.setAutoDefault(False)
        self.btn_color_settings.setDefault(False)
        self.btn_color_settings.clicked.connect(self._handle_color_settings_button)
        actions_layout.addWidget(self.btn_color_settings)

        self.btn_group_settings = QPushButton("Group Settings…")
        self.btn_group_settings.setAutoDefault(False)
        self.btn_group_settings.setDefault(False)
        self.btn_group_settings.clicked.connect(self._handle_group_settings)
        actions_layout.addWidget(self.btn_group_settings)

        self.btn_import_settings = QPushButton("Import Settings…")
        self.btn_import_settings.setAutoDefault(False)
        self.btn_import_settings.setDefault(False)
        self.btn_import_settings.clicked.connect(self._handle_import_settings)
        actions_layout.addWidget(self.btn_import_settings)

        self.btn_export_settings = QPushButton("Export Settings…")
        self.btn_export_settings.setAutoDefault(False)
        self.btn_export_settings.setDefault(False)
        self.btn_export_settings.clicked.connect(self._handle_export_settings)
        actions_layout.addWidget(self.btn_export_settings)

        actions_layout.addStretch(1)
        form.addRow(actions)

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
        previous_theme = getattr(self.cfg, "theme", "dark")
        
        self.cfg.tray_mode = bool(self.cb_tray_mode.isChecked())
        self.cfg.startup_enabled = bool(self.cb_startup.isChecked())
        self.cfg.min_free_percent = int(self.sb_min_free.value())
        self.cfg.gaming_mode = bool(self.cb_gaming.isChecked())
        self.cfg.battery_mode = self.cmb_batt.currentText()
        self.cfg.battery_slow_factor = int(self.sb_batt_factor.value())

    def _load_from_config(self) -> None:
        """Populate widgets from the current configuration state."""

        cfg = self.cfg
        idx = self.cmb_theme.findData(cfg.theme)
        if idx >= 0:
            self.cmb_theme.setCurrentIndex(idx)
        else:
            fallback = self.cmb_theme.findData("dark")
            if fallback >= 0:
                self.cmb_theme.setCurrentIndex(fallback)
        if cfg.hotkey:
            self.kse.setKeySequence(cfg.hotkey)
        else:
            try:
                self.kse.clear()
            except Exception:
                self.kse.setKeySequence("")
        self.cb_wake.setChecked(bool(cfg.gobble_on_wake))
        self.cb_show_system.setChecked(bool(cfg.show_system))
        self.cb_protect_system.setChecked(bool(cfg.protect_system))
        self.cb_pressure.setChecked(bool(cfg.pressure_enabled))
        self.cb_tray_mode.setChecked(bool(cfg.tray_mode))
        self.cb_startup.setChecked(bool(cfg.startup_enabled))
        self.sb_min_free.setValue(int(cfg.min_free_percent))
        self.cb_gaming.setChecked(bool(cfg.gaming_mode))
        self.cmb_batt.setCurrentText(cfg.battery_mode)
        self.sb_batt_factor.setValue(int(cfg.battery_slow_factor))

    def _handle_theme_changed(self) -> None:
        if self._loading_theme:
            return
        theme = self.cmb_theme.currentData()
        if theme not in {"light", "dark", "custom"}:
            theme = "dark"
        if theme == "custom":
            self._preview_theme("custom")
            return
        self._preview_theme(theme)

    def _preview_theme(self, theme: str) -> None:
        callback = self._on_preview_theme
        if callable(callback):
            callback(theme)

    def _ensure_custom_selected(self) -> None:
        if self.cmb_theme.currentData() == "custom":
            return
        idx = self.cmb_theme.findData("custom")
        if idx < 0:
            return
        self._loading_theme = True
        try:
            self.cmb_theme.setCurrentIndex(idx)
        finally:
            self._loading_theme = False

    def _handle_color_settings_button(self) -> None:
        self._handle_color_settings(force=True)

    def _handle_color_settings(self, *, force: bool = False) -> None:
        if not force:
            return
        self._ensure_custom_selected()
        self._preview_theme("custom")
        if self._trigger_callback(self._on_open_color_settings):
            self._preview_theme("custom")

    def _handle_group_settings(self) -> None:
        if self._trigger_callback(self._on_open_group_settings):
            self._load_from_config()

    def _handle_import_settings(self) -> None:
        if self._trigger_callback(self._on_import_settings):
            self._load_from_config()

    def _handle_export_settings(self) -> None:
        self._trigger_callback(self._on_export_settings)

    @staticmethod
    def _trigger_callback(callback: Optional[Callable[[], None]]) -> bool:
        if callable(callback):
            callback()
            return True
        return False


class ColorSettingsDialog(QDialog):
    """Dialog allowing users to customise the UI colour palette."""

    TARGETS = [
        ("text", "Text"),
        ("background", "Background"),
        ("button", "Buttons"),
    ]

    def __init__(
        self,
        colors: Dict[str, str],
        parent=None,
        theme: Optional[str] = None,
        *,
        on_preview: Optional[Callable[[Dict[str, str]], None]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Color Settings")
        self.setModal(True)
        self.setMinimumWidth(520)

        defaults = Config.default_theme_colors(theme or "dark")
        self._colors: Dict[str, str] = {
            key: self._normalise_hex(colors.get(key), defaults.get(key, "#000000"))
            for key, _ in self.TARGETS
        }
        self._swatches: Dict[str, QFrame] = {}
        self._labels: Dict[str, QLabel] = {}
        self._radios: Dict[str, QRadioButton] = {}
        self._button_group = QButtonGroup(self)
        self._current_target = "text"
        self._result: Dict[str, str] = {}
        self._updating_picker = False
        self._on_preview = on_preview
        self._preview_interval_ms = 75
        self._pending_preview: Optional[Dict[str, str]] = None
        self._preview_timer = None
        if QT_AVAILABLE:
            try:
                timer = QTimer(self)
            except Exception:
                timer = None
            self._preview_timer = timer
            if timer is not None:
                try:
                    timer.setSingleShot(True)
                except Exception:
                    pass
                try:
                    timer.setInterval(self._preview_interval_ms)
                except Exception:
                    pass
                try:
                    timer.timeout.connect(self._flush_preview)
                except Exception:
                    pass

        layout = QVBoxLayout(self)
        content = QHBoxLayout()
        content.setSpacing(16)
        layout.addLayout(content, 1)

        selector_widget = QWidget()
        selector_layout = QVBoxLayout(selector_widget)
        selector_layout.setContentsMargins(0, 0, 0, 0)
        selector_layout.setSpacing(10)

        for index, (key, label_text) in enumerate(self.TARGETS):
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            radio = QRadioButton(label_text)
            self._button_group.addButton(radio)
            row_layout.addWidget(radio, 1)
            self._radios[key] = radio

            swatch = QFrame()
            swatch.setFixedSize(40, 20)
            swatch.setFrameShape(QFrame.StyledPanel)
            swatch.setLineWidth(1)
            swatch.setCursor(Qt.PointingHandCursor)
            row_layout.addWidget(swatch)

            label = QLabel(self._colors[key].upper())
            label.setMinimumWidth(80)
            label.setCursor(Qt.PointingHandCursor)
            row_layout.addWidget(label)

            selector_layout.addWidget(row_widget)
            self._swatches[key] = swatch
            self._labels[key] = label

            radio.toggled.connect(lambda checked, target=key: self._on_target_selected(target, checked))
            swatch.mousePressEvent = lambda event, target=key: self._handle_swatch_click(target, event)
            label.mousePressEvent = lambda event, target=key: self._handle_swatch_click(target, event)
            if index == 0:
                radio.setChecked(True)

        selector_layout.addStretch(1)
        content.addWidget(selector_widget, 0)

        preview_container = QWidget()
        preview_container.setMinimumWidth(260)
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(12)

        hint = QLabel("Use the palette below to refine the selected colour")
        hint.setWordWrap(True)
        preview_layout.addWidget(hint)

        self._picker = QColorDialog(preview_container)
        try:
            size_policy = self._picker.sizePolicy()
        except Exception:
            size_policy = None
        if size_policy is not None and hasattr(self._picker, "setSizePolicy"):
            try:
                self._picker.setSizePolicy(size_policy.horizontalPolicy(), size_policy.verticalPolicy())
            except Exception:
                pass
        option_names = ["DontUseNativeDialog", "NoButtons"]
        for name in option_names:
            option = getattr(QColorDialog, name, None)
            if option is None:
                continue
            try:
                self._picker.setOption(option, True)
            except Exception:
                try:
                    current_opts = getattr(self._picker, "options", lambda: None)()
                except Exception:
                    current_opts = None
                if current_opts is None:
                    continue
                try:
                    self._picker.setOptions(current_opts | option)
                except Exception:
                    continue
        try:
            if hasattr(self._picker, "setWindowFlag") and hasattr(Qt, "Widget"):
                self._picker.setWindowFlag(Qt.Widget, True)
                if hasattr(Qt, "Dialog"):
                    self._picker.setWindowFlag(Qt.Dialog, False)
                if hasattr(Qt, "Window"):
                    self._picker.setWindowFlag(Qt.Window, False)
        except Exception:
            pass
        if hasattr(self._picker, "currentColorChanged"):
            try:
                self._picker.currentColorChanged.connect(self._on_color_changed)
            except Exception:
                pass
        preview_layout.addWidget(self._picker, 1)

        content.addWidget(preview_container, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for key in self._colors:
            self._update_preview(key)

        self._highlight_selection()
        self._sync_picker_to_target()

    @staticmethod
    def _normalise_hex(value: Optional[str], fallback: str) -> str:
        if not isinstance(value, str):
            return fallback.lower()
        candidate = value.strip()
        if not candidate:
            return fallback.lower()
        if not candidate.startswith("#"):
            candidate = f"#{candidate}"
        if len(candidate) == 4:
            candidate = "#" + "".join(ch * 2 for ch in candidate[1:])
        if len(candidate) != 7:
            return fallback.lower()
        try:
            int(candidate[1:], 16)
        except ValueError:
            return fallback.lower()
        return candidate.lower()

    def _on_target_selected(self, key: str, checked: bool) -> None:
        if not checked or key == self._current_target:
            return
        self._current_target = key
        self._sync_picker_to_target()
        self._highlight_selection()

    def _highlight_selection(self) -> None:
        for key, frame in self._swatches.items():
            if frame is None:
                continue
            is_active = key == self._current_target
            frame.setProperty("active", is_active)
            frame.style().unpolish(frame)
            frame.style().polish(frame)
            self._update_preview(key)

    def _handle_swatch_click(self, key: str, event) -> None:
        button = getattr(event, "button", None)
        if button is not None and button != Qt.LeftButton:
            return
        radio = self._radios.get(key)
        if radio is not None:
            if not radio.isChecked():
                radio.setChecked(True)
            else:
                self._sync_picker_to_target()
        else:
            self._sync_picker_to_target()
        picker = getattr(self, "_picker", None)
        if picker is not None and hasattr(picker, "setFocus"):
            try:
                picker.setFocus()
            except Exception:
                pass
        if hasattr(event, "accept"):
            try:
                event.accept()
            except Exception:
                pass

    def _sync_picker_to_target(self) -> None:
        picker = getattr(self, "_picker", None)
        if picker is None or not hasattr(picker, "setCurrentColor"):
            return
        color_hex = self._colors.get(self._current_target, "#000000")
        qcolor = QColor(color_hex)
        if not isinstance(qcolor, QColor) or not qcolor.isValid():
            return
        self._updating_picker = True
        try:
            picker.setCurrentColor(qcolor)
            if hasattr(picker, "show"):
                try:
                    picker.show()
                except Exception:
                    pass
        finally:
            self._updating_picker = False

    def _on_color_changed(self, color: QColor) -> None:
        if self._updating_picker:
            return
        if not isinstance(color, QColor) or not color.isValid():
            return
        key = self._current_target
        current_hex = self._colors.get(key, "#000000")
        try:
            name = color.name()
        except Exception:
            name = current_hex
        normalised = self._normalise_hex(name, current_hex)
        if normalised == current_hex:
            return
        self._colors[key] = normalised
        self._update_preview(key)
        self._emit_preview()
        self._highlight_selection()

    def _update_preview(self, key: str) -> None:
        color = self._colors.get(key, "#000000")
        swatch = self._swatches.get(key)
        label = self._labels.get(key)
        if swatch is not None:
            border = "2px solid #5b82ff" if swatch.property("active") else "1px solid rgba(0, 0, 0, 80)"
            swatch.setStyleSheet(
                "QFrame {"
                f"background-color: {color};"
                f"border: {border};"
                "border-radius: 4px;"
                "}"
            )
        if label is not None:
            label.setText(color.upper())

    def _emit_preview(self) -> None:
        callback = self._on_preview
        if not callable(callback):
            return
        payload = {key: value for key, value in self._colors.items()}
        timer = getattr(self, "_preview_timer", None)
        if timer is None:
            try:
                callback(payload)
            except Exception:
                pass
            return
        self._pending_preview = payload
        start = getattr(timer, "start", None)
        if callable(start):
            try:
                start(self._preview_interval_ms)
                return
            except Exception:
                pass
        self._pending_preview = None
        try:
            callback(payload)
        except Exception:
            pass

    def _flush_preview(self) -> None:
        payload = self._pending_preview
        self._pending_preview = None
        callback = self._on_preview
        if not callable(callback) or not isinstance(payload, dict):
            return
        try:
            callback(dict(payload))
        except Exception:
            pass

    def accept(self) -> None:  # noqa: D401 - Qt override
        """Persist the currently selected colours and close."""

        self._result = {key: value for key, value in self._colors.items()}
        super().accept()

    def colors(self) -> Dict[str, str]:
        """Return the colours selected by the user."""

        return dict(self._result)


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
    "ColorSettingsDialog",
    "SettingsDialog",
    "ChartWidget",
    "Dashboard",
    "_InteractionWatcher",
]
