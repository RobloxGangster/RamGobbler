"""Regression tests for the process matching helpers.

The tests double as executable documentation: each scenario is heavily
commented so contributors can understand how grouped matching should behave.
"""

import os
import sys

# Add the project root to ``sys.path`` so tests can import the application
# modules without requiring installation.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import matching
from matching import TargetSpec
import pytest
import ram_gobbler


def test_match_by_name():
    info = {"name": "Notepad.exe"}
    t = TargetSpec(key="notepad.exe", match_by="name")
    assert matching.matches(info, t)
    t2 = TargetSpec(key="calc.exe", match_by="name")
    assert not matching.matches(info, t2)


def test_match_by_path():
    info = {"exe": r"C:\Program Files\App\app.exe"}
    t = TargetSpec(key=r"c:\program files\app\app.exe", match_by="path")
    assert matching.matches(info, t)


def test_match_by_cmdline():
    info = {"cmdline": ["python", "script.py", "--opt"]}
    t = TargetSpec(key="script.py", match_by="cmdline")
    assert matching.matches(info, t)
    t2 = TargetSpec(key="other.py", match_by="cmdline")
    assert not matching.matches(info, t2)


def test_match_by_parent():
    info = {"parent": "launcher.exe"}
    t = TargetSpec(key="launcher.exe", match_by="parent")
    assert matching.matches(info, t)
    t2 = TargetSpec(key="other.exe", match_by="parent")
    assert not matching.matches(info, t2)


def test_match_pids_returns_entire_group(monkeypatch):
    """Selecting one process should return every PID in the bundle."""

    # ``FakeProc`` mimics the attributes ``psutil`` exposes through
    # ``process_iter`` so our helper sees deterministic data.
    class FakeProc:
        def __init__(self, pid, info):
            self.pid = pid
            self.info = info

    procs = [
        FakeProc(100, {"pid": 100, "name": "App.exe", "exe": r"C:\\App\\app.exe", "cmdline": ["app.exe"], "ppid": 0}),
        FakeProc(101, {"pid": 101, "name": "Helper.exe", "exe": r"C:\\App\\helper.exe", "cmdline": ["helper.exe"], "ppid": 100}),
        FakeProc(200, {"pid": 200, "name": "Other.exe", "exe": r"C:\\Other\\other.exe", "cmdline": ["other.exe"], "ppid": 0}),
    ]

    def fake_iter(attrs=None, ad_value=None):
        # ``attrs`` and ``ad_value`` are accepted for API compatibility.
        return iter(procs)

    monkeypatch.setattr(ram_gobbler.psutil, "process_iter", fake_iter)
    monkeypatch.setattr(ram_gobbler, "is_system_process", lambda proc: False)

    class DummyProc:
        def name(self):
            return ""

    monkeypatch.setattr(ram_gobbler.psutil, "Process", lambda pid: DummyProc())

    target = type("T", (), {"key": "app.exe", "match_by": "name"})()
    assert ram_gobbler.match_pids(target) == [100, 101]


def test_match_pids_respects_include_system(monkeypatch):
    class FakeProc:
        def __init__(self, pid, info):
            self.pid = pid
            self.info = info

    procs = [
        FakeProc(100, {"pid": 100, "name": "App.exe", "exe": r"C:\\App\\app.exe", "cmdline": ["app.exe"], "ppid": 0}),
        FakeProc(101, {"pid": 101, "name": "Helper.exe", "exe": r"C:\\App\\helper.exe", "cmdline": ["helper.exe"], "ppid": 100}),
    ]

    def fake_iter(attrs=None, ad_value=None):
        return iter(procs)

    monkeypatch.setattr(ram_gobbler.psutil, "process_iter", fake_iter)
    class DummyProc:
        def name(self):
            return ""

    monkeypatch.setattr(ram_gobbler.psutil, "Process", lambda pid: DummyProc())
    monkeypatch.setattr(ram_gobbler, "is_system_process", lambda proc: proc.pid == 101)

    target = type("T", (), {"key": "app.exe", "match_by": "name"})()
    assert ram_gobbler.match_pids(target, include_system=True) == [100, 101]
    assert ram_gobbler.match_pids(target, include_system=False) == [100]


def test_list_processes_groups_children(monkeypatch):
    """The grouped snapshot should fold helpers underneath their parent."""

    class Mem:
        def __init__(self, rss):
            self.rss = rss

    class FakeProc:
        def __init__(self, pid, info):
            self.pid = pid
            self.info = info

    procs = [
        FakeProc(100, {"pid": 100, "name": "App.exe", "exe": r"C:\\App\\app.exe", "memory_info": Mem(120 * 1024 * 1024), "ppid": 0}),
        FakeProc(101, {"pid": 101, "name": "Helper.exe", "exe": r"C:\\App\\helper.exe", "memory_info": Mem(30 * 1024 * 1024), "ppid": 100}),
    ]

    def fake_iter(attrs=None, ad_value=None):
        return iter(procs)

    monkeypatch.setattr(ram_gobbler.psutil, "process_iter", fake_iter)
    monkeypatch.setattr(ram_gobbler, "is_system_process", lambda proc: False)

    groups = ram_gobbler.list_processes(include_system=True)
    assert len(groups) == 1
    group = groups[0]
    assert group.pids == [100, 101]
    assert pytest.approx(group.total_rss_mb, rel=1e-3) == 150.0
