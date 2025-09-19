"""Utilities for building Ram Gobbler's runtime Qt stylesheet."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from textwrap import dedent


def _clamp(value: float, minimum: float = 0.0, maximum: float = 255.0) -> int:
    return int(max(minimum, min(maximum, round(value))))


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    clean = value.lstrip("#")
    if len(clean) != 6:
        clean = "000000"
    return tuple(int(clean[idx : idx + 2], 16) for idx in (0, 2, 4))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _blend(color: str, other: str, factor: float) -> str:
    factor = max(0.0, min(1.0, factor))
    r1, g1, b1 = _hex_to_rgb(color)
    r2, g2, b2 = _hex_to_rgb(other)
    r = _clamp(r1 + (r2 - r1) * factor)
    g = _clamp(g1 + (g2 - g1) * factor)
    b = _clamp(b1 + (b2 - b1) * factor)
    return _rgb_to_hex((r, g, b))


def _lighten(color: str, factor: float) -> str:
    return _blend(color, "#FFFFFF", factor)


def _darken(color: str, factor: float) -> str:
    return _blend(color, "#000000", factor)


def _relative_luminance(color: str) -> float:
    r, g, b = [_hex_to_rgb(color)[idx] / 255.0 for idx in range(3)]
    channels = []
    for channel in (r, g, b):
        if channel <= 0.03928:
            channels.append(channel / 12.92)
        else:
            channels.append(((channel + 0.055) / 1.055) ** 2.4)
    r_l, g_l, b_l = channels
    return 0.2126 * r_l + 0.7152 * g_l + 0.0722 * b_l


def _contrast_color(color: str) -> str:
    return "#0B1220" if _relative_luminance(color) > 0.6 else "#F5F8FF"


def _with_alpha(color: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(color)
    return f"rgba({r}, {g}, {b}, {max(0.0, min(alpha, 1.0)):.3f})"


@dataclass(frozen=True)
class ThemePalette:
    """Container producing derived colours for the application stylesheet."""

    primary: str
    secondary: str
    text: str
    button: str

    @classmethod
    def from_dict(cls, palette: dict) -> "ThemePalette":
        def _normalise(value: str, fallback: str) -> str:
            try:
                raw = (value or "").strip()
            except Exception:
                raw = ""
            if not raw:
                return fallback
            if not raw.startswith("#"):
                raw = f"#{raw}"
            raw = raw[:7]
            try:
                int(raw[1:], 16)
            except Exception:
                return fallback
            return raw.upper()

        defaults = {
            "primary": "#5A7BFF",
            "secondary": "#283142",
            "text": "#E8ECF7",
            "button": "#5A7BFF",
        }
        data = {**defaults, **(palette or {})}
        return cls(
            primary=_normalise(data.get("primary"), defaults["primary"]),
            secondary=_normalise(data.get("secondary"), defaults["secondary"]),
            text=_normalise(data.get("text"), defaults["text"]),
            button=_normalise(data.get("button"), defaults["button"]),
        )

    @property
    def dark_mode(self) -> bool:
        return _relative_luminance(self.text) >= 0.55

    @property
    @lru_cache(maxsize=1)
    def background(self) -> str:
        base = _darken(self.secondary, 0.6) if self.dark_mode else _lighten(self.secondary, 0.78)
        anchor = "#090B12" if self.dark_mode else "#FFFFFF"
        return _blend(base, anchor, 0.35 if self.dark_mode else 0.12)

    @property
    @lru_cache(maxsize=1)
    def surface(self) -> str:
        return _lighten(self.background, 0.09) if self.dark_mode else _darken(self.background, 0.06)

    @property
    @lru_cache(maxsize=1)
    def surface_alt(self) -> str:
        return _lighten(self.background, 0.14) if self.dark_mode else _darken(self.background, 0.1)

    @property
    def border(self) -> str:
        anchor = "#000000" if self.dark_mode else "#1C2233"
        return _blend(self.secondary, anchor, 0.45 if self.dark_mode else 0.3)

    @property
    def border_soft(self) -> str:
        return _blend(self.surface, self.border, 0.35 if self.dark_mode else 0.2)

    @property
    def muted_text(self) -> str:
        anchor = self.background if self.dark_mode else "#6A748A"
        return _blend(self.text, anchor, 0.45 if self.dark_mode else 0.55)

    @property
    def button_text(self) -> str:
        return _contrast_color(self.button)

    @property
    def button_hover(self) -> str:
        return _lighten(self.button, 0.08)

    @property
    def button_pressed(self) -> str:
        return _darken(self.button, 0.12)

    @property
    def selection(self) -> str:
        return _blend(self.primary, "#FFFFFF", 0.18 if self.dark_mode else 0.32)

    @property
    def selection_text(self) -> str:
        return _contrast_color(self.selection)

    @property
    def alternate_surface(self) -> str:
        return _blend(self.surface, self.background, 0.18 if self.dark_mode else 0.08)

    @property
    def toolbar_top(self) -> str:
        return _lighten(self.surface, 0.04) if self.dark_mode else _lighten(self.surface, 0.02)

    @property
    def toolbar_bottom(self) -> str:
        return _darken(self.toolbar_top, 0.09)

    @property
    def focus_ring(self) -> str:
        return _blend(self.primary, "#FFFFFF", 0.35 if self.dark_mode else 0.2)

    @property
    def shadow(self) -> str:
        base = _darken(self.secondary, 0.75) if self.dark_mode else _darken(self.secondary, 0.35)
        return _with_alpha(base, 0.25 if self.dark_mode else 0.18)

    def as_dict(self) -> dict:
        return {
            "primary": self.primary,
            "secondary": self.secondary,
            "text": self.text,
            "button": self.button,
        }


def build_stylesheet(palette: ThemePalette, font_family: str, font_size: int) -> str:
    radius = 12
    spacing = 12
    base = dedent(
        f"""
        QWidget {{
            font-family: "{font_family}";
            font-size: {font_size}pt;
            color: {palette.text};
            background-color: {palette.background};
        }}

        QMainWindow {{
            background-color: {palette.background};
        }}

        QToolTip {{
            background-color: {palette.surface};
            color: {palette.text};
            border: 1px solid {palette.border};
            padding: 6px 8px;
            border-radius: {radius - 4}px;
        }}

        QLabel[heading="true"] {{
            font-size: {font_size + 2}pt;
            font-weight: 600;
        }}
        """
    )

    containers = dedent(
        f"""
        QGroupBox {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {palette.surface}, stop:1 {palette.surface_alt});
            border: 1px solid {palette.border};
            border-radius: {radius}px;
            margin-top: 20px;
            padding: {spacing + 4}px;
        }}

        QGroupBox::title {{
            subcontrol-origin: margin;
            left: {spacing}px;
            padding: 0px 6px;
            background-color: transparent;
            color: {palette.muted_text};
        }}

        QFrame[frameShape="4"] {{
            border: 1px solid {palette.border};
            border-radius: {radius - 6}px;
        }}

        QStatusBar {{
            background-color: {palette.surface};
            border-top: 1px solid {palette.border};
            padding: 4px {spacing}px;
        }}
        """
    )

    toolbar = dedent(
        f"""
        QToolBar {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {palette.toolbar_top}, stop:1 {palette.toolbar_bottom});
            border-bottom: 1px solid {palette.border};
            padding: 8px {spacing + 4}px;
            spacing: {spacing}px;
        }}

        QToolBar QToolButton {{
            border-radius: {radius - 6}px;
            padding: 6px 10px;
            background-color: transparent;
        }}

        QToolBar QToolButton:hover {{
            background-color: {palette.alternate_surface};
        }}
        """
    )

    inputs = dedent(
        f"""
        QLineEdit,
        QSpinBox,
        QComboBox,
        QFontComboBox,
        QKeySequenceEdit {{
            background-color: {palette.surface};
            border: 1px solid {palette.border_soft};
            border-radius: {radius - 4}px;
            padding: 6px 10px;
            selection-background-color: {palette.selection};
            selection-color: {palette.selection_text};
        }}

        QLineEdit:focus,
        QSpinBox:focus,
        QComboBox:focus,
        QFontComboBox:focus,
        QKeySequenceEdit:focus {{
            border: 1px solid {palette.focus_ring};
        }}

        QSpinBox::up-button,
        QSpinBox::down-button {{
            border: none;
            background: transparent;
        }}

        QComboBox QAbstractItemView {{
            background-color: {palette.surface};
            border: 1px solid {palette.border};
            selection-background-color: {palette.selection};
            selection-color: {palette.selection_text};
        }}
        """
    )

    tables = dedent(
        f"""
        QTreeView,
        QTableView,
        QTableWidget {{
            background-color: {palette.surface};
            alternate-background-color: {palette.alternate_surface};
            border: 1px solid {palette.border};
            border-radius: {radius}px;
            padding: 4px;
            selection-background-color: {palette.selection};
            selection-color: {palette.selection_text};
            gridline-color: {palette.border_soft};
        }}

        QTreeView::item:hover,
        QTableView::item:hover {{
            background-color: {palette.alternate_surface};
        }}

        QHeaderView::section {{
            background-color: {palette.surface_alt};
            color: {palette.text};
            border: none;
            border-bottom: 1px solid {palette.border};
            padding: 6px 10px;
        }}

        QTableCornerButton::section {{
            background-color: {palette.surface_alt};
            border: none;
        }}
        """
    )

    buttons = dedent(
        f"""
        QPushButton {{
            background-color: {palette.button};
            color: {palette.button_text};
            border-radius: {radius - 2}px;
            padding: 8px 18px;
            border: none;
        }}

        QPushButton:hover {{
            background-color: {palette.button_hover};
        }}

        QPushButton:pressed {{
            background-color: {palette.button_pressed};
        }}

        QPushButton:disabled {{
            background-color: {palette.alternate_surface};
            color: {palette.muted_text};
        }}
        """
    )

    menus = dedent(
        f"""
        QMenu {{
            background-color: {palette.surface};
            border: 1px solid {palette.border};
            padding: 6px;
        }}

        QMenu::item:selected {{
            background-color: {palette.selection};
            color: {palette.selection_text};
            border-radius: {radius - 6}px;
        }}
        """
    )

    scrollbars = dedent(
        f"""
        QScrollBar:vertical {{
            background: {palette.surface};
            width: 12px;
            margin: 12px 0 12px 0;
            border: none;
        }}

        QScrollBar::handle:vertical {{
            background: {palette.border};
            border-radius: 6px;
        }}

        QScrollBar:horizontal {{
            background: {palette.surface};
            height: 12px;
            margin: 0 12px 0 12px;
            border: none;
        }}

        QScrollBar::handle:horizontal {{
            background: {palette.border};
            border-radius: 6px;
        }}

        QScrollBar::add-line,
        QScrollBar::sub-line {{
            background: transparent;
            border: none;
        }}
        """
    )

    misc = dedent(
        f"""
        QListWidget,
        QTreeWidget {{
            border: 1px solid {palette.border};
            border-radius: {radius - 2}px;
            padding: 4px;
        }}

        QListWidget::item:selected,
        QTreeWidget::item:selected {{
            background-color: {palette.selection};
            color: {palette.selection_text};
        }}
        """
    )

    return "\n".join(
        section.strip() for section in (
            base,
            containers,
            toolbar,
            inputs,
            tables,
            buttons,
            menus,
            scrollbars,
            misc,
        )
    )


__all__ = ["ThemePalette", "build_stylesheet"]
