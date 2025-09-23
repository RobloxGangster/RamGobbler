from dataclasses import replace

import pytest

from ram_gobbler_app.core.models import TargetConfig
from ram_gobbler_app.services.process_matcher import ProcessMatcher


class FakeMemory:
    def __init__(self, rss):
        self.rss = rss


class FakeProcess:
    def __init__(self, pid, name, *, exe="", rss=0, ppid=None, cmdline=None, username="user"):
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": name,
            "exe": exe,
            "memory_info": FakeMemory(rss),
            "ppid": ppid,
            "cmdline": list(cmdline or ()),
            "username": username,
        }


class FakePsutil:
    def __init__(self, processes):
        self.processes = list(processes)
        self.calls = 0

    def process_iter(self, attrs=None):
        self.calls += 1
        return list(self.processes)


def test_process_matcher_caches_snapshot(monkeypatch):
    parent = FakeProcess(100, "Parent.exe", exe="C:/Parent.exe", rss=200 * 1024 * 1024)
    child = FakeProcess(101, "Child.exe", exe="C:/Child.exe", rss=100 * 1024 * 1024, ppid=100)
    grandchild = FakeProcess(102, "Worker.exe", exe="C:/Worker.exe", rss=50 * 1024 * 1024, ppid=101)
    system_proc = FakeProcess(1, "System", exe="C:/Windows/system32/system.exe", rss=10 * 1024 * 1024)
    fake = FakePsutil([parent, child, grandchild, system_proc])
    monkeypatch.setattr("ram_gobbler_app.services.process_matcher.psutil", fake)

    matcher = ProcessMatcher(ttl=10.0)
    target = TargetConfig(key="Parent.exe", match_by="name", uid="t1", active=True)

    pids = matcher.match_pids(target)
    assert pids == [100]

    pids_children = matcher.match_pids(replace(target, include_children=True))
    assert pids_children == [100, 101, 102]

    # Second call should reuse cached iterator
    matcher.match_pids(target)
    assert fake.calls == 1

    roots = matcher.build_process_tree(include_system=False)
    assert [node.pid for node in roots] == [100]
    assert roots[0].aggregate_rss_mb == pytest.approx(350.0)
    assert matcher.is_system_pid(1)
