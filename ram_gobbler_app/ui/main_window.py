"""Lightweight Qt window that wires the new services together."""
from __future__ import annotations

from typing import Callable

from ..constants import APP_NAME
from ..core.config_repository import ConfigRepository
from ..core.models import AppConfig
from ..core.palette import build_palette, default_theme_colors
from ..services.metrics_service import MetricsService
from ..services.scheduler_service import SchedulerEvent, SchedulerService
from ..platform import QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget


class MainWindow(QMainWindow):
    """Minimal main window that reacts to scheduler events."""

    def __init__(
        self,
        config_repo: ConfigRepository,
        scheduler: SchedulerService,
        metrics: MetricsService,
    ) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self._config_repo = config_repo
        self._scheduler = scheduler
        self._metrics = metrics
        self._unsubscribe: Callable[[], None] | None = scheduler.subscribe(self._handle_event)
        self._init_ui()
        self._apply_theme(self._config_repo.load())

    def _init_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.status_label = QLabel("Scheduler idle")
        layout.addWidget(self.status_label)

        trigger = QPushButton("Trigger Active Targets")
        trigger.clicked.connect(self._trigger_active)
        layout.addWidget(trigger)

        export = QPushButton("Export Metrics")
        export.clicked.connect(self._export_metrics)
        layout.addWidget(export)

        layout.addStretch(1)
        self.setCentralWidget(central)

    def _apply_theme(self, config: AppConfig) -> None:
        theme = config.theme if config.theme in {"light", "dark", "custom"} else "dark"
        base = default_theme_colors("light" if theme == "light" else "dark")
        overrides = config.custom_theme_colors if theme == "custom" else config.theme_colors
        palette = build_palette(theme, dict(overrides or base), defaults=base)
        stylesheet = (
            "QWidget { background: %(background)s; color: %(text)s; } "
            "QPushButton { background: %(button)s; color: %(accent_text)s; border-radius: 4px; padding: 6px 12px; } "
            "QPushButton:hover { background: %(accent_hover)s; } "
            "QPushButton:pressed { background: %(accent_pressed)s; }"
        ) % palette
        try:
            self.setStyleSheet(stylesheet)
        except Exception:
            pass

    # ----------------------------------------------------------- interactions
    def _trigger_active(self) -> None:
        config = self._config_repo.load()
        for target in config.targets:
            if target.active:
                self._scheduler.trigger_manual(target.uid, reason="manual")
        self.status_label.setText("Manual trigger requested")

    def _export_metrics(self) -> None:
        data = self._metrics.export_json(7)
        self.status_label.setText(f"Metrics (7d): {data}")

    # ----------------------------------------------------------- scheduler API
    def _handle_event(self, event: SchedulerEvent) -> None:
        msg = f"{event.status.upper()}: {event.message}"
        self.status_label.setText(msg)

    # ------------------------------------------------------------- lifecycle
    def closeEvent(self, event) -> None:  # pragma: no cover - Qt runtime behaviour
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        super().closeEvent(event)


__all__ = ["MainWindow"]
