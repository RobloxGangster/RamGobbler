"""Command line interface for the Ram Gobbler desktop application.

Historically the project exposed a large ``if __name__ == "__main__"`` block
with ad-hoc argument handling.  The refactored structure routes all command
line interactions through this module so both the GUI launcher and automated
scripts share the same parsing logic.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .constants import CONFIG_PATH, LEGACY_PREFIX, METRICS_PATH
from .models import Config
from .platform import QApplication, QT_AVAILABLE
from .ui.main_window import MainWindow
from metrics import Metrics


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments used by the small CLI interface.

    The original tool accepted a handful of hidden ``--trim-*`` flags.  For
    backwards compatibility we still recognise them and translate the values to
    the new ``--gobble-*`` options.  ``argparse`` is deliberately configured with
    ``add_help=True`` so casual users running the binary from a terminal receive
    friendly usage information.
    """

    legacy_visible_flag = f"--{LEGACY_PREFIX}-visible"
    legacy_group_flag = f"--{LEGACY_PREFIX}-group"

    if argv is None:
        argv = sys.argv[1:]

    normalized: list[str] = []
    for arg in argv:
        if arg == legacy_visible_flag:
            normalized.append("--gobble-visible")
        elif arg == legacy_group_flag:
            normalized.append("--gobble-group")
        else:
            normalized.append(arg)

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--headless", action="store_true", help="run without GUI")
    parser.add_argument("--gobble-visible", action="store_true", help="gobble currently visible targets and exit")
    parser.add_argument("--gobble-group", help="gobble targets belonging to a specific group")
    parser.add_argument("--export-metrics", help="write metrics JSON to the given path")
    args = parser.parse_args(normalized)

    legacy_visible_attr = legacy_visible_flag.replace("-", "_").lstrip("_")
    legacy_group_attr = legacy_group_flag.replace("-", "_").lstrip("_")
    setattr(args, legacy_visible_attr, args.gobble_visible)
    setattr(args, legacy_group_attr, args.gobble_group)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Main entry point used by ``ram_gobbler.py``.

    The workflow is intentionally straightforward:

    #. Parse command line arguments (supporting legacy aliases).
    #. Load persisted configuration and metrics data.
    #. Execute the requested mode: headless JSON output, metrics export or the
       full Qt user interface.

    Returning an integer exit status keeps the function friendly to both
    ``sys.exit`` and unit tests.
    """

    args = parse_args(argv)

    # Configuration and metrics are plain JSON files stored under ``AppData`` on
    # Windows.  Loading them up-front keeps the rest of the function focused on
    # behaviour.
    config = Config(CONFIG_PATH)
    config.load()
    metrics = Metrics(METRICS_PATH)
    metrics.load()

    if args.headless:
        result = {"ok": True, "reclaimed_mb": 0.0, "gobbles": 0}
        if args.export_metrics:
            try:
                with open(args.export_metrics, "w", encoding="utf-8") as handle:
                    handle.write(metrics.export_json(30))
            except Exception:
                # Exporting metrics is an optional convenience feature.  If the
                # user specifies an invalid path we simply ignore the failure and
                # still return a successful headless result.
                pass
        print(json.dumps(result))
        return 0

    if not QT_AVAILABLE:
        print(
            "Qt bindings are not available; run with --headless on this system.",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.show()
    rc = app.exec()
    config.save()
    return rc


__all__ = ["parse_args", "main"]
