from dataclasses import replace
from datetime import datetime

from ram_gobbler_app.core.models import AppConfig, TargetConfig
from ram_gobbler_app.services.gobble_service import GobbleSummary
from ram_gobbler_app.services.scheduler_service import SchedulerEvent, SchedulerService


class MemoryRepo:
    def __init__(self, config):
        self.config = config

    def load(self):
        return self.config

    def mutate(self, fn):
        self.config = fn(self.config)
        return self.config


class StubMatcher:
    def __init__(self, pids):
        self._pids = list(pids)

    def match_pids(self, target):
        return list(self._pids)

    def is_system_pid(self, pid):
        return False


class StubGobbler:
    def __init__(self, summary):
        self.summary = summary

    def gobble(self, target, *, protect_system, reason):
        return replace(self.summary, reason=reason)


def test_scheduler_executes_gobble(monkeypatch):
    target = TargetConfig(key="demo", match_by="name", uid="t1", active=True)
    config = AppConfig(targets=(target,))
    repo = MemoryRepo(config)
    matcher = StubMatcher([123])
    summary = GobbleSummary(
        timestamp=datetime.now(),
        reclaimed_mb=42.0,
        gobbled=1,
        helper_needed=False,
        total_rss_mb=100.0,
        pids=(123,),
        reason="timer",
        strategy="standard",
    )
    gobbler = StubGobbler(summary)
    scheduler = SchedulerService(repo, matcher, gobbler)
    events: list[SchedulerEvent] = []
    scheduler._notify = events.append  # type: ignore[attr-defined]

    runtime = scheduler._runtime_for(target)
    scheduler._handle_target(repo.config, target, runtime, datetime.now(), datetime.now().timestamp(), 0, "timer", True, "")

    assert events and events[0].status == "gobble"
    updated_target = repo.config.targets[0]
    assert updated_target.last_result.startswith("42.0")


def test_scheduler_reports_skip():
    target = TargetConfig(key="demo", match_by="name", uid="t1", active=True)
    config = AppConfig(targets=(target,), metered=True)
    repo = MemoryRepo(config)
    matcher = StubMatcher([123])
    summary = GobbleSummary(
        timestamp=datetime.now(),
        reclaimed_mb=0.0,
        gobbled=0,
        helper_needed=False,
        total_rss_mb=0.0,
        pids=(),
        reason="timer",
        strategy="standard",
    )
    gobbler = StubGobbler(summary)
    scheduler = SchedulerService(repo, matcher, gobbler)
    events: list[SchedulerEvent] = []
    scheduler._notify = events.append  # type: ignore[attr-defined]

    runtime = scheduler._runtime_for(target)
    scheduler._handle_target(repo.config, target, runtime, datetime.now(), datetime.now().timestamp(), 0, "timer", True, "")

    assert events and events[0].status == "skip"
    assert events[0].reason == "Metered"
