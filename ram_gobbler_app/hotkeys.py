"""Global hotkey handling helpers."""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from .platform import (
    HOTKEY_ID,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    MSG,
    PBT_APMRESUMEAUTOMATIC,
    PBT_APMRESUMESUSPEND,
    QAbstractNativeEventFilter,
    WM_HOTKEY,
    WM_POWERBROADCAST,
)

VK_SPECIAL = {
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "SPACE": 0x20,
    "TAB": 0x09,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "BACKSPACE": 0x08,
    "RETURN": 0x0D,
    "ENTER": 0x0D,
}


def parse_hotkey_string(hotkey: str) -> Tuple[Optional[int], Optional[int]]:
    """Translate a human readable hotkey into modifier and virtual-key codes."""

    if not hotkey:
        return None, None
    parts = [segment.strip() for segment in hotkey.split("+") if segment.strip()]
    mods = 0
    vk = None
    for part in parts:
        upper = part.upper()
        if upper in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif upper == "ALT":
            mods |= MOD_ALT
        elif upper == "SHIFT":
            mods |= MOD_SHIFT
        elif upper in ("WIN", "META", "CMD", "SUPER"):
            mods |= MOD_WIN
        elif upper.startswith("F") and upper[1:].isdigit():
            number = int(upper[1:])
            if 1 <= number <= 24:
                vk = 0x70 + (number - 1)
        elif len(upper) == 1 and upper.isalnum():
            vk = ord(upper)
        else:
            vk = VK_SPECIAL.get(upper)
    return mods, vk


class WinEventFilter(QAbstractNativeEventFilter):
    """Bridge Windows native events into Qt callbacks."""

    def __init__(self, on_hotkey: Callable[[int], None], on_resume: Callable[[], None]):
        super().__init__()
        self.on_hotkey = on_hotkey
        self.on_resume = on_resume

    def nativeEventFilter(self, event_type, message):
        """Dispatch hotkey and resume events to the provided callbacks."""

        if event_type != "windows_generic_MSG":
            return False, 0
        msg = MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:
            self.on_hotkey(msg.wParam)
        elif msg.message == WM_POWERBROADCAST:
            wp = int(msg.wParam)
            if wp in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                self.on_resume()
        return False, 0


__all__ = ["parse_hotkey_string", "WinEventFilter", "VK_SPECIAL"]
