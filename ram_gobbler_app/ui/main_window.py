"""Main application window implementation."""
from __future__ import annotations

import json
import shutil
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..constants import (
    APP_NAME,
    CONFIG_PATH,
    METRICS_PATH,
    DEFAULT_INTERVAL,
    MIN_INTERVAL,
)
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
    QPixmap,
    QSplitter,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTreeView,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QSize,
    QSizePolicy,
    Qt,
    QAbstractItemView,
    QHeaderView,
    QTimer,
    QSortFilterProxyModel,
    QPushButton,
    RegisterHotKey,
    UnregisterHotKey,
    HOTKEY_ID,
    QColor,
    QT_AVAILABLE,
)
from ..processes import ProcessNode, match_pids
from ..startup import is_startup_enabled, set_startup_enabled
from ..system import is_system_process
from .components import (
    AddTargetDialog,
    ColorSettingsDialog,
    Dashboard,
    GroupSettingsDialog,
    ProcessFilterProxyModel,
    ProcessTreeModel,
    SettingsDialog,
    TargetsTable,
    _InteractionWatcher,
    IntervalEditor,
)
from .logo_asset import get_rg_logo_png_bytes
from metrics import Metrics


def _parse_hex(color: str) -> tuple[str, tuple[int, int, int]]:
    """Return normalised ``#rrggbb`` string and RGB tuple."""

    if not isinstance(color, str):
        raise ValueError("color must be a string")
    value = color.strip()
    if not value:
        raise ValueError("empty color")
    if not value.startswith("#"):
        value = f"#{value}"
    if len(value) == 4:
        value = "#" + "".join(ch * 2 for ch in value[1:])
    if len(value) != 7:
        raise ValueError(f"invalid color: {color}")
    try:
        r = int(value[1:3], 16)
        g = int(value[3:5], 16)
        b = int(value[5:7], 16)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid color: {color}") from exc
    return value.lower(), (r, g, b)


def _hex_from_rgb(r: int, g: int, b: int) -> str:
    """Convert RGB components into a ``#rrggbb`` string."""

    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def normalize_color(value: str, fallback: str) -> str:
    """Normalise ``value`` or fall back to ``fallback`` on failure."""

    try:
        normalised, _ = _parse_hex(value)
        return normalised
    except Exception:
        normalised_fallback, _ = _parse_hex(fallback)
        return normalised_fallback


def lighten_color(color: str, factor: int) -> str:
    """Lighten ``color`` by ``factor`` percent."""

    normalised, (r, g, b) = _parse_hex(color)
    factor = max(factor, 0)
    if QT_AVAILABLE and hasattr(QColor, "lighter"):
        try:
            return QColor(normalised).lighter(factor).name().lower()
        except Exception:  # pragma: no cover - fallback to manual path
            pass
    scale = factor / 100.0
    r = min(255, int(round(r * scale)))
    g = min(255, int(round(g * scale)))
    b = min(255, int(round(b * scale)))
    return _hex_from_rgb(r, g, b)


def darken_color(color: str, factor: int) -> str:
    """Darken ``color`` by ``factor`` percent."""

    normalised, (r, g, b) = _parse_hex(color)
    factor = max(factor, 1)
    if QT_AVAILABLE and hasattr(QColor, "darker"):
        try:
            return QColor(normalised).darker(factor).name().lower()
        except Exception:  # pragma: no cover - fallback to manual path
            pass
    scale = 100.0 / factor
    r = max(0, int(round(r * scale)))
    g = max(0, int(round(g * scale)))
    b = max(0, int(round(b * scale)))
    return _hex_from_rgb(r, g, b)


def blend_colors(color_a: str, color_b: str, ratio: float) -> str:
    """Blend two colours using ``ratio`` of ``color_b``."""

    _, (r1, g1, b1) = _parse_hex(color_a)
    _, (r2, g2, b2) = _parse_hex(color_b)
    ratio = max(0.0, min(1.0, ratio))
    inv = 1.0 - ratio
    r = int(round(r1 * inv + r2 * ratio))
    g = int(round(g1 * inv + g2 * ratio))
    b = int(round(b1 * inv + b2 * ratio))
    return _hex_from_rgb(r, g, b)


def _qt_enum_value(container: Any, name: str, enum_scopes: Iterable[str] = ()) -> Any:
    """Return ``name`` from ``container`` or nested enum scopes if present."""

    value = getattr(container, name, None)
    if value is not None:
        return value

    for scope_name in enum_scopes:
        scope = getattr(container, scope_name, None)
        if scope is None:
            continue
        value = getattr(scope, name, None)
        if value is not None:
            return value
    return None


def _qt_enum_to_int(value: Any) -> Optional[int]:
    """Best-effort conversion of Qt enum objects to integer bitmasks."""

    if value is None:
        return None
    if isinstance(value, int):
        return value

    numeric = getattr(value, "value", None)
    if isinstance(numeric, int):
        return numeric

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def relative_luminance(color: str) -> float:
    """Return the W3C relative luminance for ``color``."""

    _, (r, g, b) = _parse_hex(color)

    def _linear(component: int) -> float:
        value = component / 255.0
        if value <= 0.03928:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    r_lin = _linear(r)
    g_lin = _linear(g)
    b_lin = _linear(b)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_color(
    color: str,
    *,
    light: str = "#ffffff",
    dark: str = "#0c0f19",
    threshold: float = 0.25,
) -> str:
    """Pick ``light`` or ``dark`` for contrast against ``color``."""

    luminance = relative_luminance(color)
    return dark if luminance >= threshold else light


def build_palette(theme: str, base_colors: Dict[str, str]) -> Dict[str, str]:
    """Derive a palette for ``theme`` using ``base_colors``."""

    defaults = Config.default_theme_colors(theme)
    text = normalize_color(base_colors.get("text", defaults["text"]), defaults["text"])
    background = normalize_color(
        base_colors.get("background", defaults["background"]), defaults["background"]
    )
    button = normalize_color(base_colors.get("button", defaults["button"]), defaults["button"])

    is_light = theme == "light" or relative_luminance(background) >= 0.45

    surface = lighten_color(background, 115 if is_light else 140)
    surface_alt = lighten_color(background, 108 if is_light else 170)
    table_background = lighten_color(background, 120 if is_light else 150)
    table_alternate = lighten_color(background, 112 if is_light else 175)
    border = (
        darken_color(surface, 122)
        if is_light
        else darken_color(lighten_color(background, 200), 120)
    )
    border_strong = (
        darken_color(surface, 140)
        if is_light
        else darken_color(lighten_color(background, 220), 130)
    )

    accent = button
    accent_hover = lighten_color(accent, 115 if is_light else 120)
    accent_pressed = darken_color(accent, 120 if is_light else 130)
    accent_border = darken_color(accent, 135 if is_light else 140)
    accent_text = contrast_color(accent)
    accent_disabled = blend_colors(accent, background, 0.7 if is_light else 0.4)
    accent_disabled_text = blend_colors(accent_text, text, 0.5)

    table_hover_ratio = 0.22 if is_light else 0.35
    table_hover = blend_colors(surface, accent, table_hover_ratio)

    header_background = lighten_color(background, 118 if is_light else 160)
    scroll_handle = (
        darken_color(surface, 140)
        if is_light
        else lighten_color(background, 220)
    )
    scroll_hover = (
        darken_color(surface, 155)
        if is_light
        else lighten_color(background, 260)
    )

    input_background = lighten_color(background, 110 if is_light else 170)
    input_border = (
        darken_color(surface, 135)
        if is_light
        else darken_color(lighten_color(background, 210), 120)
    )
    disabled_background = blend_colors(surface, background, 0.6 if is_light else 0.35)
    disabled_text = blend_colors(text, surface, 0.6 if is_light else 0.5)

    palette = {
        "text": text,
        "background": background,
        "surface": surface,
        "surface_alt": surface_alt,
        "table_background": table_background,
        "table_alternate": table_alternate,
        "border": border,
        "border_strong": border_strong,
        "accent": accent,
        "accent_hover": accent_hover,
        "accent_pressed": accent_pressed,
        "accent_border": accent_border,
        "accent_text": accent_text,
        "accent_disabled": accent_disabled,
        "accent_disabled_text": accent_disabled_text,
        "table_hover": table_hover,
        "table_selected_bg": accent,
        "table_selected_text": accent_text,
        "header_background": header_background,
        "header_text": text,
        "scrollbar_handle": scroll_handle,
        "scrollbar_hover": scroll_hover,
        "menu_background": surface,
        "menu_border": border,
        "menu_item_hover": table_hover,
        "splitter_handle": border,
        "splitter_pressed": accent,
        "input_background": input_background,
        "input_border": input_border,
        "input_focus_border": accent,
        "input_selection_bg": accent,
        "input_selection_text": accent_text,
        "disabled_background": disabled_background,
        "disabled_text": disabled_text,
        "toolbar_background": surface,
        "toolbar_border": border,
        "tool_button_background": accent,
        "tool_button_border": accent_border,
        "tool_button_hover": accent_hover,
        "tool_button_pressed": accent_pressed,
        "tool_button_text": text,
        "statusbar_background": surface,
        "is_light": is_light,
        "flat_button_background": accent,
        "flat_button_border": accent_border,
        "flat_button_hover": accent_hover,
        "flat_button_pressed": accent_pressed,
        "flat_button_text": accent_text,
        "table_grid": border_strong,
    }
    return palette


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
        if hasattr(tb, "setContentsMargins"):
            tb.setContentsMargins(6, 4, 6, 4)
        layout = tb.layout() if hasattr(tb, "layout") else None
        if layout is not None:
            try:
                layout.setSpacing(2)
                layout.setContentsMargins(6, 4, 6, 4)
            except Exception:
                pass
        if hasattr(tb, "setStyleSheet"):
            tb.setStyleSheet(
                "QToolBar {spacing: 2px;} "
                "QToolButton {padding: 4px 8px; margin-right: 2px;}"
            )

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

        self.act_view = QAction("Dashboard", self)
        self.act_view.triggered.connect(self._toggle_view)
        tb.addAction(self.act_view)

        self.act_settings = QAction("Settings…", self)
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

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

        tb.addSeparator()
        spacer = QWidget()
        if hasattr(spacer, "setSizePolicy"):
            try:
                spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            except Exception:
                pass
        tb.addWidget(spacer)

        self.logo_label = QLabel()
        self.logo_label.setObjectName("toolbarLogo")
        if hasattr(self.logo_label, "setToolTip"):
            try:
                self.logo_label.setToolTip("RamGobbler")
            except Exception:
                pass
        if QT_AVAILABLE:
            align_right = _qt_enum_value(Qt, "AlignRight", ("AlignmentFlag",))
            align_vcenter = _qt_enum_value(Qt, "AlignVCenter", ("AlignmentFlag",))
            aligned_candidates: List[Any] = []

            combined = None
            for flag in (align_right, align_vcenter):
                if flag is None:
                    continue
                if combined is None:
                    combined = flag
                    continue
                try:
                    combined = combined | flag
                except TypeError:
                    combined = None
                    break
            if combined is not None:
                aligned_candidates.append(combined)

            numeric = 0
            for flag in (align_right, align_vcenter):
                bit = _qt_enum_to_int(flag)
                if bit is not None:
                    numeric |= bit
            if numeric:
                alignment_flag = getattr(Qt, "AlignmentFlag", None)
                if alignment_flag is not None:
                    try:
                        aligned_candidates.append(alignment_flag(numeric))
                    except Exception:
                        pass
                aligned_candidates.append(numeric)

            for alignment in aligned_candidates:
                try:
                    self.logo_label.setAlignment(alignment)
                    break
                except Exception:
                    continue
        self._configure_logo_label()
        self._set_logo_pixmap()
        tb.addWidget(self.logo_label)


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

    def _set_logo_pixmap(self) -> None:
        """Populate the toolbar logo label with the embedded RG artwork."""

        label = getattr(self, "logo_label", None)
        if label is None:
            return

        self._configure_logo_label()

        if not QT_AVAILABLE:
            if hasattr(label, "setText"):
                try:
                    label.setText("RG")
                except Exception:
                    pass
            return

        try:
            pixmap = QPixmap()
        except Exception:
            pixmap = None

        data = get_rg_logo_png_bytes()
        if not data or pixmap is None:
            if hasattr(label, "setText"):
                try:
                    label.setText("RG")
                except Exception:
                    pass
            return

        loader = getattr(pixmap, "loadFromData", None)
        if not callable(loader) or not loader(data):
            if hasattr(label, "setText"):
                try:
                    label.setText("RG")
                except Exception:
                    pass
            return

        scaler = getattr(pixmap, "scaled", None)
        if callable(scaler):
            aspect_ratio = _qt_enum_value(Qt, "KeepAspectRatio", ("AspectRatioMode",))
            smooth = _qt_enum_value(Qt, "SmoothTransformation", ("TransformationMode",))
            fast = _qt_enum_value(Qt, "FastTransformation", ("TransformationMode",))
            transform_mode = smooth or fast

            scaled = False
            if aspect_ratio is not None and transform_mode is not None:
                try:
                    pixmap = scaler(44, 44, aspect_ratio, transform_mode)
                    scaled = True
                except TypeError:
                    pass
                except Exception:
                    pass

            if not scaled and aspect_ratio is not None:
                try:
                    pixmap = scaler(44, 44, aspect_ratio)
                    scaled = True
                except TypeError:
                    pass
                except Exception:
                    pass

            if not scaled:
                keep_int = _qt_enum_to_int(aspect_ratio)
                transform_int = _qt_enum_to_int(transform_mode)
                args: List[Any] = [44, 44]
                if keep_int is not None:
                    args.append(keep_int)
                    if transform_int is not None:
                        args.append(transform_int)
                try:
                    pixmap = scaler(*args)
                    scaled = True
                except Exception:
                    pass

            if not scaled:
                try:
                    pixmap = scaler(44, 44)
                except Exception:
                    pass

        if hasattr(label, "setPixmap"):
            try:
                label.setPixmap(pixmap)
                if hasattr(label, "setMinimumSize"):
                    label.setMinimumSize(36, 36)
                if hasattr(label, "setMaximumHeight"):
                    label.setMaximumHeight(48)
            except Exception:
                if hasattr(label, "setText"):
                    try:
                        label.setText("RG")
                    except Exception:
                        pass

    def _configure_logo_label(self) -> None:
        """Apply consistent styling and transparency to the logo label."""

        label = getattr(self, "logo_label", None)
        if label is None:
            return

        stylesheet_bits = [
            "padding: 2px",
            "border-radius: 16px",
            "background: transparent",
            "background-color: transparent",
        ]
        stylesheet = "; ".join(stylesheet_bits) + ";"

        if hasattr(label, "setStyleSheet"):
            try:
                label.setStyleSheet(stylesheet)
            except Exception:
                pass

        if hasattr(label, "setContentsMargins"):
            try:
                label.setContentsMargins(0, 0, 0, 0)
            except Exception:
                pass

        if hasattr(label, "setMargin"):
            try:
                label.setMargin(0)
            except Exception:
                pass

        if hasattr(label, "setAutoFillBackground"):
            try:
                label.setAutoFillBackground(False)
            except Exception:
                pass

        if hasattr(label, "setSizePolicy") and QSizePolicy is not None:
            fixed = _qt_enum_value(QSizePolicy, "Fixed", ("Policy",))
            if fixed is None:
                fixed = getattr(QSizePolicy, "Fixed", None)
            if fixed is not None:
                try:
                    label.setSizePolicy(fixed, fixed)
                except TypeError:
                    fixed_int = _qt_enum_to_int(fixed)
                    if fixed_int is not None:
                        try:
                            label.setSizePolicy(fixed_int, fixed_int)
                        except Exception:
                            pass
                except Exception:
                    pass

        if QT_AVAILABLE and hasattr(label, "setAttribute"):
            attributes = [
                _qt_enum_value(Qt, "WA_TranslucentBackground", ("WidgetAttribute",)),
                _qt_enum_value(Qt, "WA_NoSystemBackground", ("WidgetAttribute",)),
                _qt_enum_value(Qt, "WA_StyledBackground", ("WidgetAttribute",)),
            ]
            for attribute in attributes:
                if attribute is None:
                    continue
                try:
                    label.setAttribute(attribute, True)
                except Exception:
                    continue

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
            self.config.reset_theme_colors(selected)
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
        else:
            self.act_gobble_all.setText("Gobble All Targets")
            self.act_stop_all.setText("Stop All")

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

        palette = build_palette(theme, self.config.theme_palette())
        self._apply_palette(palette)

    def _apply_palette(self, palette: Dict[str, str]):
        """Apply ``palette`` to the main window and supporting widgets."""

        self._active_palette = palette
        IntervalEditor.apply_theme_palette(palette)
        targets_table = getattr(self, "targets_table", None)
        if targets_table is not None and hasattr(targets_table, "set_accent_color"):
            try:
                targets_table.set_accent_color(palette.get("accent", ""))
            except Exception:
                pass
        stylesheet = f"""
            * {{
                font-family: "Segoe UI", "Helvetica Neue", sans-serif;
                font-size: 12px;
                color: {palette['text']};
            }}
            QMainWindow {{
                background-color: {palette['background']};
            }}
            QToolBar {{
                background: {palette['toolbar_background']};
                border: none;
                padding: 6px 12px;
                spacing: 8px;
            }}
            QToolBar QToolButton {{
                background: {palette['tool_button_background']};
                border: 1px solid {palette['tool_button_border']};
                border-radius: 10px;
                padding: 6px 14px;
                color: {palette['tool_button_text']};
            }}
            QToolBar QToolButton:hover {{
                background: {palette['tool_button_hover']};
            }}
            QToolBar QToolButton:pressed {{
                background: {palette['tool_button_pressed']};
            }}
            QStatusBar {{
                background: {palette['statusbar_background']};
                border-top: 1px solid {palette['toolbar_border']};
            }}
            QStatusBar::item {{
                border: none;
            }}
            QGroupBox {{
                background: {palette['surface']};
                border: 1px solid {palette['border']};
                border-radius: 18px;
                margin-top: 22px;
                padding-top: 16px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0px 12px;
                margin-top: 4px;
                margin-left: 20px;
                font-weight: 600;
                color: {palette['text']};
            }}
            QLabel {{
                background: transparent;
            }}
            QLineEdit, QSpinBox, QComboBox, QKeySequenceEdit {{
                background: {palette['input_background']};
                border: 1px solid {palette['input_border']};
                border-radius: 10px;
                padding: 8px 12px;
                selection-background-color: {palette['input_selection_bg']};
                selection-color: {palette['input_selection_text']};
            }}
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QKeySequenceEdit:focus {{
                border: 1px solid {palette['input_focus_border']};
            }}
            QPushButton {{
                background: {palette['accent']};
                color: {palette['accent_text']};
                border: none;
                border-radius: 12px;
                padding: 8px 18px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {palette['accent_hover']};
            }}
            QPushButton:pressed {{
                background: {palette['accent_pressed']};
            }}
            QPushButton:disabled {{
                background: {palette['disabled_background']};
                color: {palette['disabled_text']};
            }}
            QTreeView, QTableView, QTableWidget {{
                background: {palette['table_background']};
                alternate-background-color: {palette['table_alternate']};
                border: 1px solid {palette['border']};
                border-radius: 14px;
                padding: 6px;
                gridline-color: {palette['table_grid']};
            }}
            QTreeView::item:hover, QTableView::item:hover, QTableWidget::item:hover {{
                background: {palette['table_hover']};
            }}
            QTreeView::item:selected, QTreeView::branch:selected,
            QTableView::item:selected, QTableWidget::item:selected {{
                background: {palette['table_selected_bg']};
                color: {palette['table_selected_text']};
            }}
            QHeaderView::section {{
                background: {palette['header_background']};
                color: {palette['header_text']};
                padding: 6px 10px;
                border: none;
                border-right: 1px solid {palette['border']};
                font-weight: 600;
            }}
            QHeaderView::section:last {{
                border-right: none;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 12px;
                margin: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {palette['scrollbar_handle']};
                border-radius: 6px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {palette['scrollbar_hover']};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 12px;
                margin: 4px;
            }}
            QScrollBar::handle:horizontal {{
                background: {palette['scrollbar_handle']};
                border-radius: 6px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {palette['scrollbar_hover']};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
            }}
            QMenu {{
                background: {palette['menu_background']};
                border: 1px solid {palette['menu_border']};
                border-radius: 10px;
                padding: 6px 0;
            }}
            QMenu::item {{
                padding: 6px 16px;
                border-radius: 6px;
            }}
            QMenu::item:selected {{
                background: {palette['menu_item_hover']};
            }}
            QSplitter::handle {{
                background: {palette['splitter_handle']};
                margin: 0 6px;
            }}
            QSplitter::handle:pressed {{
                background: {palette['splitter_pressed']};
            }}
        """
        self.setStyleSheet(stylesheet)

        flat_button_style = (
            "QPushButton {"
            f"background-color: {palette['flat_button_background']};"
            f"border: 1px solid {palette['flat_button_border']};"
            "border-radius: 4px;"
            "padding: 6px 12px;"
            f"color: {palette['flat_button_text']};"
            "}\n"
            "QPushButton:hover {"
            f"background-color: {palette['flat_button_hover']};"
            "}\n"
            "QPushButton:pressed {"
            f"background-color: {palette['flat_button_pressed']};"
            "}\n"
            "QPushButton:disabled {"
            f"background-color: {palette['disabled_background']};"
            f"color: {palette['disabled_text']};"
            "}"
        )
        for btn in (getattr(self, "btn_gobble_now", None), getattr(self, "btn_add_selected", None)):
            if btn is not None and hasattr(btn, "setStyleSheet"):
                btn.setStyleSheet(flat_button_style)
        for dialog in self.findChildren(GroupSettingsDialog):
            if hasattr(dialog, "apply_palette"):
                try:
                    dialog.apply_palette(palette)
                except Exception:
                    continue

        self._configure_logo_label()

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
            return max(MIN_INTERVAL, float(value))
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
            minimum=MIN_INTERVAL,
            maximum=600.0,
            step=0.1,
        )
        metrics = editor.fontMetrics()
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
        editor.set_display_width(sample_width)
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
            if reclaimed > 0 and update_totals:
                self.session_reclaimed_total += reclaimed
                self._update_total_label()
            message = f"Stopped gobbling {display}"
            if reclaimed > 0:
                message += f" (−{reclaimed:.1f} MB)"
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

        msg = f"Gobbled {display} (−{reclaimed:.1f} MB)"
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
            existing["interval"] = max(MIN_INTERVAL, float(interval))
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
            task["interval"] = max(MIN_INTERVAL, float(interval))
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
        msg = f"Gobbled {scope} (−{total_delta:.1f} MB)"
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
        dialog = SettingsDialog(
            self.config,
            self,
            on_open_color_settings=self._open_color_settings,
            on_import_settings=self._import_settings,
            on_export_settings=self._export_settings,
            on_open_group_settings=self._open_group_settings,
        )
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

    def _open_color_settings(self):
        """Allow users to tweak the active palette colours."""

        current = self.config.theme_palette_overrides()
        original_palette = getattr(self, "_active_palette", None)
        if not isinstance(original_palette, dict):
            original_palette = build_palette(self.config.theme, self.config.theme_palette())
        else:
            original_palette = dict(original_palette)

        preview_applied = False

        def handle_preview(updated: Dict[str, str]):
            nonlocal preview_applied
            preview_applied = True
            palette = build_palette(self.config.theme, dict(updated))
            self._apply_palette(palette)

        dialog = ColorSettingsDialog(
            current,
            self,
            self.config.theme,
            on_preview=handle_preview,
        )
        if dialog.exec() != QDialog.Accepted:
            if preview_applied:
                self._apply_palette(original_palette)
            return

        chosen = dialog.colors() or {}
        changed = chosen != current
        if changed:
            self.config.theme_colors = dict(chosen)

        self._apply_styles(self.config.theme)

        if changed:
            self.config.save()
            self.statusBar().showMessage("Color settings saved", 3000)

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
