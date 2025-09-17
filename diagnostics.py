#!/usr/bin/env python3
"""Utility helpers for Ram Gobbler diagnostics and dependency checks.

This module deliberately avoids importing heavy third-party dependencies so it
can run even when the main application fails to start.  It exposes a tiny CLI
that lets users inspect the crash log or bootstrap required packages such as
``psutil``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

APP_NAME = "Ram Gobbler"
CONFIG_DIR = Path(os.getenv("APPDATA", Path.home())) / "RamGobbler"
ERROR_LOG_PATH = CONFIG_DIR / "error.log"


def ensure_config_dir() -> None:
    """Make sure the Ram Gobbler configuration directory exists."""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _package_available(name: str) -> bool:
    """Return ``True`` when the requested package can be imported."""

    return importlib.util.find_spec(name) is not None


def psutil_available() -> bool:
    """Return ``True`` when ``psutil`` can be imported."""

    return _package_available("psutil")


def pyside6_available() -> bool:
    """Return ``True`` when ``PySide6`` can be imported."""

    return _package_available("PySide6")


def _install_package(package: str, user: bool = True, quiet: bool = True) -> Tuple[bool, str]:
    """Attempt to install ``package`` using ``pip``.

    Parameters
    ----------
    user:
        Install into the invoking user's site-packages directory instead of the
        system interpreter.  This mirrors the recommendation for Ram Gobbler and
        avoids needing administrative privileges.
    quiet:
        When ``True`` the command's output is only returned as part of the
        result instead of being printed directly.

    Returns
    -------
    (success, output): tuple
        ``success`` is ``True`` when installation completed. ``output`` contains
        any captured output or an error message if the invocation failed.
    """

    python = sys.executable or "python"
    cmd: List[str] = [python, "-m", "pip", "install"]
    if user:
        cmd.append("--user")
    cmd.append(package)

    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        text = output.decode("utf-8", errors="replace")
        if not quiet:
            print(text)
        return True, text
    except subprocess.CalledProcessError as exc:
        return False, exc.output.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)


def install_psutil(user: bool = True, quiet: bool = True) -> Tuple[bool, str]:
    """Attempt to install ``psutil`` using ``pip``."""

    return _install_package("psutil", user=user, quiet=quiet)


def install_pyside6(user: bool = True, quiet: bool = True) -> Tuple[bool, str]:
    """Attempt to install ``PySide6`` using ``pip``."""

    return _install_package("PySide6", user=user, quiet=quiet)


def tail_error_log(lines: Optional[int] = 50) -> str:
    """Return the contents of the crash log, optionally truncated to ``lines``."""

    if not ERROR_LOG_PATH.exists():
        return ""

    text = ERROR_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    if lines is None or lines <= 0:
        return text
    chunks = [chunk for chunk in text.strip().splitlines() if chunk]
    return "\n".join(chunks[-lines:])


def clear_error_log() -> None:
    """Remove the stored error log if it exists."""

    if ERROR_LOG_PATH.exists():
        ERROR_LOG_PATH.unlink()


def print_dependency_status() -> None:
    """Write dependency information to stdout."""

    print("Diagnostics for", APP_NAME)
    print("Configuration directory:", CONFIG_DIR)
    for name in ("psutil", "PySide6"):
        available = _package_available(name)
        if available:
            spec = importlib.util.find_spec(name)
            path = getattr(spec, "origin", "unknown") if spec else "unknown"
            print(f"{name}: installed ({path})")
        else:
            print(f"{name}: missing")


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Entry point for the diagnostics helper CLI."""

    parser = argparse.ArgumentParser(description="Ram Gobbler diagnostics")
    parser.add_argument(
        "--show-error-log",
        action="store_true",
        help="print the saved crash log if available",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=50,
        help="number of lines from the end of the error log to show (default: 50)",
    )
    parser.add_argument(
        "--clear-error-log",
        action="store_true",
        help="delete the stored crash log",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="report dependency availability",
    )
    parser.add_argument(
        "--install-psutil",
        action="store_true",
        help="attempt to install psutil using pip",
    )
    parser.add_argument(
        "--install-pyside6",
        action="store_true",
        help="attempt to install PySide6 using pip",
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help="install psutil into the system interpreter instead of --user",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    ensure_config_dir()

    exit_code = 0

    if args.check_deps:
        print_dependency_status()

    if args.install_psutil:
        success, output = install_psutil(user=not args.system, quiet=True)
        print(output.strip())
        if not success:
            exit_code = 1
        else:
            print("psutil installation succeeded.")

    if args.install_pyside6:
        success, output = install_pyside6(user=not args.system, quiet=True)
        print(output.strip())
        if not success:
            exit_code = 1
        else:
            print("PySide6 installation succeeded.")

    if args.clear_error_log:
        clear_error_log()
        print("Cleared", ERROR_LOG_PATH)

    if args.show_error_log:
        content = tail_error_log(args.lines)
        if content:
            print(content)
        else:
            print("No error log has been recorded yet.")

    if (
        not args.check_deps
        and not args.install_psutil
        and not args.clear_error_log
        and not args.show_error_log
    ):
        parser.print_help()

    return exit_code


if __name__ == "__main__":  # pragma: no cover - manual CLI testing
    sys.exit(main())
