"""Helpers for working with Qt enums and compatibility fallbacks."""

from __future__ import annotations

from typing import Any, Iterable, List, Optional

from ..platform import QApplication, QT_AVAILABLE, QSizePolicy, QStyle, Qt

__all__ = [
    "_qt_enum_value",
    "_qt_enum_to_int",
    "_qt_alignment_candidates",
    "_qt_size_policy_candidates",
    "_qt_standard_icon",
    "_device_pixel_ratio",
]


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


def _qt_alignment_candidates(*names: str) -> List[Any]:
    """Return best-effort candidates for combining Qt alignment flags."""

    if not QT_AVAILABLE:
        return []

    values: List[Any] = []
    numeric_bits: List[int] = []

    for name in names:
        value = _qt_enum_value(Qt, name, ("AlignmentFlag", "Alignment"))
        if value is None:
            continue
        values.append(value)
        numeric = _qt_enum_to_int(value)
        if numeric is not None:
            numeric_bits.append(numeric)

    candidates: List[Any] = []

    if values:
        combined = values[0]
        for value in values[1:]:
            try:
                combined = combined | value
            except TypeError:
                combined = None
                break
        if combined is not None:
            candidates.append(combined)

    if numeric_bits:
        numeric = 0
        for bit in numeric_bits:
            numeric |= bit
        for alignment_scope in ("Alignment", "AlignmentFlag"):
            alignment_type = getattr(Qt, alignment_scope, None)
            if alignment_type is None:
                continue
            try:
                candidates.append(alignment_type(numeric))
            except Exception:
                continue
        candidates.append(numeric)

    if not candidates:
        fallback_map = {
            "AlignLeft": 0x0001,
            "AlignRight": 0x0002,
            "AlignHCenter": 0x0004,
            "AlignJustify": 0x0008,
            "AlignTop": 0x0020,
            "AlignBottom": 0x0040,
            "AlignVCenter": 0x0080,
            "AlignCenter": 0x0084,
        }
        numeric = 0
        for name in names:
            numeric |= fallback_map.get(name, 0)
        if numeric:
            candidates.append(numeric)

    return candidates


def _qt_size_policy_candidates(name: str) -> List[Any]:
    """Return best-effort candidates for ``QSizePolicy`` enums."""

    candidates: List[Any] = []
    for scopes in ((), ("Policy",)):
        value = _qt_enum_value(QSizePolicy, name, scopes)
        if value is not None:
            candidates.append(value)

    numeric_candidates: List[int] = []
    for value in candidates:
        numeric = _qt_enum_to_int(value)
        if numeric is not None:
            numeric_candidates.append(numeric)

    return candidates + numeric_candidates


def _qt_standard_icon(widget: Any, name: str) -> Optional[Any]:
    """Return a standard icon from ``widget``'s style, if possible."""

    if not QT_AVAILABLE or widget is None:
        return None

    style_getter = getattr(widget, "style", None)
    if not callable(style_getter):
        return None

    try:
        style = style_getter()
    except Exception:
        style = None
    if style is None:
        return None

    enum = _qt_enum_value(QStyle, name, ("StandardPixmap",))
    numeric = _qt_enum_to_int(enum)
    if enum is None and numeric is None:
        raw = getattr(QStyle, name, None)
        enum = raw
        numeric = _qt_enum_to_int(raw)

    getter = getattr(style, "standardIcon", None)
    if not callable(getter):
        return None

    if enum is not None:
        try:
            icon = getter(enum)
            if icon is not None:
                return icon
        except Exception:
            pass

    if numeric is not None:
        try:
            icon = getter(numeric)
            if icon is not None:
                return icon
        except Exception:
            pass

    return None


def _device_pixel_ratio(widget: Any) -> float:
    """Return the device-pixel ratio for ``widget`` when available."""

    if not QT_AVAILABLE or widget is None:
        return 1.0

    for attr in ("devicePixelRatioF", "devicePixelRatio"):
        getter = getattr(widget, attr, None)
        if callable(getter):
            try:
                ratio = float(getter())
            except Exception:
                continue
            if ratio > 0.0:
                return ratio

    app = QApplication.instance()
    if app is None:
        return 1.0

    try:
        screen = None
        screen_getter = getattr(widget, "screen", None)
        if callable(screen_getter):
            try:
                screen = screen_getter()
            except Exception:
                screen = None
        if screen is None:
            primary = getattr(app, "primaryScreen", None)
            if callable(primary):
                try:
                    screen = primary()
                except Exception:
                    screen = None
        if screen is not None:
            for attr in ("devicePixelRatioF", "devicePixelRatio"):
                getter = getattr(screen, attr, None)
                if callable(getter):
                    try:
                        ratio = float(getter())
                    except Exception:
                        continue
                    if ratio > 0.0:
                        return ratio
    except Exception:
        return 1.0

    return 1.0
