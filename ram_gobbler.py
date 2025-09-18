#!/usr/bin/env python3
"""Command line bootstrapper for the Ram Gobbler application.

This tiny module exists primarily for backwards compatibility with the
original single-file implementation of the app.  It simply re-exports the
objects that external tools expect and forwards execution into the new
``ram_gobbler_app`` package.  The bulk of the application now lives in the
package; keeping this wrapper means scripts that ``import ram_gobbler`` still
work exactly as before.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Sequence

from ram_gobbler_app import (
    APP_NAME,
    BACKUP_DIR,
    CONFIG_DIR,
    CONFIG_PATH,
    DEFAULT_INTERVAL,
    HELPER_PIPE,
    LEGACY_LAST_TS_KEY,
    LEGACY_PREFIX,
    LEGACY_REASON_KEY,
    LEGACY_WAKE_KEY,
    METRICS_PATH,
    Config,
    Gobbler,
    Group,
    Target,
    estimate_reclaim,
    main as _run_main,
    parse_args,
)

__all__ = [
    "APP_NAME",
    "BACKUP_DIR",
    "CONFIG_DIR",
    "CONFIG_PATH",
    "DEFAULT_INTERVAL",
    "HELPER_PIPE",
    "LEGACY_LAST_TS_KEY",
    "LEGACY_PREFIX",
    "LEGACY_REASON_KEY",
    "LEGACY_WAKE_KEY",
    "METRICS_PATH",
    "Config",
    "Group",
    "Target",
    "Gobbler",
    "estimate_reclaim",
    "parse_args",
    "main",
]


def main(argv: Sequence[str] | None = None) -> int:
    """Proxy to :func:`ram_gobbler_app.app.main`.

    ``ram_gobbler.py`` used to contain the entire application.  After the
    refactor it behaves like a launcher: command line arguments are delegated to
    :func:`ram_gobbler_app.app.main` and the resulting exit code is bubbled up to
    the caller.  Keeping the indirection means that legacy entry points such as
    ``python ram_gobbler.py`` continue to function.
    """

    return _run_main(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # We intentionally swallow *all* exceptions here so that we can provide
        # the user with a helpful crash log.  The exception is re-raised at the
        # end so automated callers still notice the failure.
        err = traceback.format_exc()

        # Persist the traceback to a log file inside the configuration
        # directory.  The ``exist_ok`` flag means first-run scenarios work
        # without special handling.
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            log_path = os.path.join(CONFIG_DIR, "error.log")
            with open(log_path, "a", encoding="utf-8") as handle:
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                handle.write(f"\n=== Crash {timestamp} ===\n{err}\n")
        except Exception:
            # Writing crash logs should never prevent the original exception
            # from bubbling up, therefore any failure here is ignored.
            pass

        # If Qt bindings are available we surface the traceback via a modal
        # message box.  This mirrors the behaviour of the legacy application and
        # gives non-technical users an explanation of what went wrong.
        try:
            from ram_gobbler_app.platform import QApplication, QMessageBox

            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, APP_NAME, f"Unexpected error:\n{err[:4000]}")
        except Exception:
            # Displaying the message box is best-effort: the Qt fallback shims
            # used in unit tests do not implement a GUI, so any failure here is
            # ignored as well.
            pass
        raise
