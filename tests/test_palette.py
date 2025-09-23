from ram_gobbler_app.core.palette import (
    build_palette,
    blend_colors,
    darken_color,
    default_theme_colors,
    lighten_color,
    normalize_color,
)


def test_default_palette_structure():
    dark = default_theme_colors("dark")
    assert dark["text"].startswith("#")
    light = default_theme_colors("light")
    assert light["background"].startswith("#")


def test_color_normalisation_and_blending():
    assert normalize_color("fff", "#000000") == "#ffffff"
    blend = blend_colors("#000000", "#ffffff", 0.5)
    assert blend == "#808080"
    lighter = lighten_color("#202020", 150)
    darker = darken_color("#a0a0a0", 150)
    assert lighter != darker


def test_build_palette_uses_overrides():
    defaults = default_theme_colors("dark")
    palette = build_palette("dark", {"text": "#cccccc"}, defaults=defaults)
    assert palette["text"] == "#cccccc"
    assert "accent" in palette
