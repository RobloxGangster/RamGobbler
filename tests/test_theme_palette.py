from ram_gobbler_app.models import Config
from ram_gobbler_app.ui.main_window import (
    blend_colors,
    build_palette,
    contrast_color,
    darken_color,
    lighten_color,
    normalize_color,
)


def test_config_theme_palette_defaults():
    cfg = Config("dummy")
    palette = cfg.theme_palette()
    assert palette == {
        "text": "#e5e9f5",
        "background": "#0c0f19",
        "button": "#5b82ff",
    }


def test_config_theme_palette_customisation():
    cfg = Config("dummy")
    cfg.theme_colors = {"text": "#101010", "background": "#202020", "button": "#303030"}
    palette = cfg.theme_palette()
    assert palette == {"text": "#101010", "background": "#202020", "button": "#303030"}


def test_custom_theme_round_trip():
    cfg = Config("dummy")
    custom = {"text": "#0f0f0f", "background": "#202123", "button": "#9966ff"}
    cfg.custom_theme_colors = dict(custom)
    cfg.theme = "custom"
    assert cfg.theme_palette() == custom
    cfg.reset_theme_colors("dark")
    assert cfg.custom_theme_colors == custom
    cfg.theme = "custom"
    assert cfg.theme_palette_for("custom") == custom


def test_color_helpers_adjustment():
    assert lighten_color("#101010", 120) == "#131313"
    assert darken_color("#202020", 150) == "#151515"
    assert blend_colors("#000000", "#ffffff", 0.5) == "#808080"
    assert contrast_color("#fefefe", dark="#010101", threshold=0.8) == "#010101"
    assert normalize_color("abc", "#123456") == "#aabbcc"


def test_build_palette_uses_base_colors():
    base = {"text": "#111111", "background": "#eeeeee", "button": "#3366ff"}
    palette = build_palette("light", base)
    assert palette["text"] == "#111111"
    assert palette["background"] == "#eeeeee"
    assert palette["accent"] == "#3366ff"
    assert palette["table_selected_bg"] == palette["accent"]
    assert palette["table_selected_text"] in {palette["accent_text"], palette["text"]}
