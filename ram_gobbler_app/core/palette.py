"""Colour palette utilities shared by the UI and tests."""
from __future__ import annotations

from typing import Dict, Tuple

from ..platform import QT_AVAILABLE, QColor


_THEME_DEFAULTS: Dict[str, Dict[str, str]] = {
    "light": {"text": "#111827", "background": "#e7ecf6", "button": "#4c6ef5"},
    "dark": {"text": "#e5e9f5", "background": "#0c0f19", "button": "#5b82ff"},
}


def default_theme_colors(theme: str) -> Dict[str, str]:
    """Return the base palette for ``theme``."""

    key = "light" if theme == "light" else "dark"
    return dict(_THEME_DEFAULTS[key])


def _parse_hex(color: str) -> tuple[str, tuple[int, int, int]]:
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
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def normalize_color(value: str, fallback: str) -> str:
    try:
        normalised, _ = _parse_hex(value)
        return normalised
    except Exception:
        normalised_fallback, _ = _parse_hex(fallback)
        return normalised_fallback


def lighten_color(color: str, factor: int) -> str:
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
    _, (r1, g1, b1) = _parse_hex(color_a)
    _, (r2, g2, b2) = _parse_hex(color_b)
    ratio = max(0.0, min(1.0, ratio))
    inv = 1.0 - ratio
    r = int(round(r1 * inv + r2 * ratio))
    g = int(round(g1 * inv + g2 * ratio))
    b = int(round(b1 * inv + b2 * ratio))
    return _hex_from_rgb(r, g, b)


def relative_luminance(color: str) -> float:
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


def contrast_color(color: str, *, light: str = "#ffffff", dark: str = "#0c0f19", threshold: float = 0.25) -> str:
    luminance = relative_luminance(color)
    return dark if luminance >= threshold else light


def build_palette(theme: str, base_colors: Dict[str, str], *, defaults: Dict[str, str]) -> Dict[str, str]:
    text = normalize_color(base_colors.get("text", defaults["text"]), defaults["text"])
    background = normalize_color(base_colors.get("background", defaults["background"]), defaults["background"])
    button = normalize_color(base_colors.get("button", defaults["button"]), defaults["button"])

    is_light = theme == "light" or relative_luminance(background) >= 0.45

    surface = lighten_color(background, 115 if is_light else 140)
    surface_alt = lighten_color(background, 108 if is_light else 170)
    table_background = lighten_color(background, 120 if is_light else 150)
    table_alternate = lighten_color(background, 112 if is_light else 175)
    border = darken_color(surface, 122) if is_light else darken_color(lighten_color(background, 200), 120)
    border_strong = darken_color(surface, 140) if is_light else darken_color(lighten_color(background, 220), 130)

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
    scroll_handle = darken_color(surface, 140) if is_light else lighten_color(background, 220)
    scroll_hover = darken_color(surface, 155) if is_light else lighten_color(background, 260)

    input_background = lighten_color(background, 110 if is_light else 170)
    input_border = darken_color(surface, 135) if is_light else darken_color(lighten_color(background, 210), 120)
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


__all__ = [
    "default_theme_colors",
    "normalize_color",
    "lighten_color",
    "darken_color",
    "blend_colors",
    "relative_luminance",
    "contrast_color",
    "build_palette",
]
