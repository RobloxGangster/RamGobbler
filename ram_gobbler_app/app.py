"""Command line entry point used by ``ram_gobbler.py``."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .constants import CONFIG_PATH, LEGACY_PREFIX, METRICS_PATH
from .core.config_repository import ConfigRepository
from .platform import QApplication, QT_AVAILABLE
from .services.metrics_service import MetricsService
from .services.process_matcher import ProcessMatcher
from .services.gobble_service import GobbleService
from .services.scheduler_service import SchedulerService
from .ui.main_window import MainWindow


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    legacy_visible_flag = f"--{LEGACY_PREFIX}-visible"
    legacy_group_flag = f"--{LEGACY_PREFIX}-group"

    argv = list(sys.argv[1:] if argv is None else argv)
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
    args = parse_args(argv)

    config_repo = ConfigRepository(CONFIG_PATH)
    config = config_repo.load()

    metrics = MetricsService(METRICS_PATH)
    metrics.load()

    matcher = ProcessMatcher()
    gobbler = GobbleService(matcher, metrics)
    scheduler = SchedulerService(config_repo, matcher, gobbler)

    if args.headless:
        result = {"ok": True, "reclaimed_mb": 0.0, "gobbles": 0}
        if args.export_metrics:
            try:
                metrics.flush()
                with open(args.export_metrics, "w", encoding="utf-8") as handle:
                    handle.write(metrics.export_json(30))
            except Exception:
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
    win = MainWindow(config_repo, scheduler, metrics)
    win.show()
    scheduler.start()
    rc = app.exec()
    scheduler.stop()
    return rc


__all__ = ["parse_args", "main"]
