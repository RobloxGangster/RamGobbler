from datetime import datetime
from types import SimpleNamespace
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ram_gobbler_app import gobbler as gobbler_module
from ram_gobbler_app.models import Config, Target

Gobbler = gobbler_module.Gobbler


def _base_config_and_target():
    config = Config(path="test-config.json")
    config.pressure_enabled = False
    config.gaming_mode = False
    config.battery_mode = "off"
    config.battery_slow_factor = 1.0
    config.maintenance_window = {"start": "00:00", "end": "00:00", "aggressive": False}
    config.idle_seconds = 0
    config.jitter_pct = 0
    config.metered = False
    config.diag_level = 0
    config.targets = []

    target = Target(key="dummy", match_by="name")
    target.active = True
    target.interval_sec = 0.0
    target.last_gobble_ts = 0.0

    config.targets.append(target)
    return config, target


def _run_once(gobbler: Gobbler):
    gobbler.run()


def test_gobbler_reports_schedule_block(monkeypatch):
    config, target = _base_config_and_target()
    target.schedule = {"start": "00:00", "end": "00:00"}

    fake_now = datetime(2023, 1, 2, 12, 0, 0)
    reasons = []

    gobbler = Gobbler(config, SimpleNamespace(save=lambda: None), lambda *_: None, lambda *_: None)

    def record_result(index, reason):
        reasons.append(reason)
        gobbler._stop.set()

    gobbler.on_result = record_result

    monkeypatch.setattr(gobbler_module, "datetime", SimpleNamespace(now=lambda: fake_now))
    monkeypatch.setattr(gobbler_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(gobbler_module, "match_pids", lambda target: [])
    monkeypatch.setattr(gobbler_module, "get_foreground_pid", lambda: 0)
    monkeypatch.setattr(gobbler_module, "on_battery", lambda: (False, 0))
    monkeypatch.setattr(gobbler_module, "is_fullscreen_active", lambda: False)

    _run_once(gobbler)

    assert reasons == ["Blocked (Schedule)"]
    assert config.targets[0].last_skip_reason == "Blocked (Schedule)"


def test_gobbler_skips_when_metered(monkeypatch):
    config, target = _base_config_and_target()
    config.metered = True
    reasons = []

    gobbler = Gobbler(config, SimpleNamespace(save=lambda: None), lambda *_: None, lambda *_: None)

    def record_result(index, reason):
        reasons.append(reason)
        gobbler._stop.set()

    gobbler.on_result = record_result

    monkeypatch.setattr(gobbler_module, "datetime", SimpleNamespace(now=lambda: datetime(2023, 1, 2, 12, 0, 0)))
    monkeypatch.setattr(gobbler_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(gobbler_module, "match_pids", lambda target: [])
    monkeypatch.setattr(gobbler_module, "get_foreground_pid", lambda: 0)
    monkeypatch.setattr(gobbler_module, "on_battery", lambda: (False, 0))
    monkeypatch.setattr(gobbler_module, "is_fullscreen_active", lambda: False)
    monkeypatch.setattr(
        Gobbler,
        "_perform_gobble",
        lambda *args, **kwargs: pytest.fail("should not gobble when metered"),
    )

    _run_once(gobbler)

    assert reasons == ["Metered"]
    assert config.targets[0].last_skip_reason == "Metered"
