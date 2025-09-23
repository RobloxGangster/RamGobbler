import json
from pathlib import Path

import ram_gobbler_app.app as app_module
from ram_gobbler_app.core.models import AppConfig


class DummyRepo:
    def __init__(self):
        self.config = AppConfig()

    def load(self):
        return self.config

    def mutate(self, fn):
        self.config = fn(self.config)
        return self.config


class DummyMetrics:
    def __init__(self, *_args, **_kwargs):
        self._data = {}

    def load(self):
        return None

    def record(self, *args, **kwargs):
        return None

    def flush(self):
        return None

    def export_json(self, days):
        return json.dumps({"gobbles": 0, "reclaimed_mb": 0.0})


class DummyScheduler:
    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        return None

    def stop(self):
        return None


class DummyWindow:
    def __init__(self, *_args, **_kwargs):
        self.shown = False

    def show(self):
        self.shown = True


def test_headless_main(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(app_module, "ConfigRepository", lambda *_: DummyRepo())
    monkeypatch.setattr(app_module, "MetricsService", lambda *_a, **_kw: DummyMetrics())
    monkeypatch.setattr(app_module, "ProcessMatcher", lambda: None)
    monkeypatch.setattr(app_module, "GobbleService", lambda *_: None)
    monkeypatch.setattr(app_module, "SchedulerService", lambda *_: DummyScheduler())
    monkeypatch.setattr(app_module, "QT_AVAILABLE", False)

    rc = app_module.main(["--headless"])
    captured = capsys.readouterr()
    assert rc == 0
    data = json.loads(captured.out)
    assert data["ok"]


def test_ui_smoke(monkeypatch):
    class DummyApp:
        def __init__(self, *_):
            self.exec_calls = 0

        def exec(self):
            self.exec_calls += 1
            return 0

    # Ensure the Qt path uses fallbacks and the window can be constructed.
    monkeypatch.setattr(app_module, "ConfigRepository", lambda *_: DummyRepo())
    monkeypatch.setattr(app_module, "MetricsService", lambda *_a, **_kw: DummyMetrics())
    monkeypatch.setattr(app_module, "ProcessMatcher", lambda: None)
    monkeypatch.setattr(app_module, "GobbleService", lambda *_: None)
    monkeypatch.setattr(app_module, "SchedulerService", lambda *_: DummyScheduler())
    monkeypatch.setattr(app_module, "MainWindow", DummyWindow)
    monkeypatch.setattr(app_module, "QT_AVAILABLE", True)
    monkeypatch.setattr(app_module, "QApplication", lambda *_: DummyApp())

    rc = app_module.main([])
    assert rc == 0
