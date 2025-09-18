"""Shared dependency imports with user-friendly error messaging."""
from __future__ import annotations

import os
import sys


def _import_psutil():
    """Import :mod:`psutil`, displaying a friendly error when unavailable."""

    try:
        import psutil  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - executed only when missing
        msg = (
            "Required dependency 'psutil' is missing.\n"
            "Install it with: pip install psutil"
        )
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, msg, "Ram Gobbler", 0)
            except Exception:
                print(msg, file=sys.stderr)
        else:
            print(msg, file=sys.stderr)
        raise SystemExit(1) from exc
    return psutil


psutil = _import_psutil()

__all__ = ["psutil"]
