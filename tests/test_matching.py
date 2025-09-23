import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import matching
import pytest
from matching import TargetSpec

from ram_gobbler_app import processes as process_helpers
from ram_gobbler_app.models import Target


class FakeMemoryInfo:
    def __init__(self, rss_bytes):
        self.rss = rss_bytes


class FakeProcess:
    def __init__(
        self,
        pid,
        name="",
        *,
        exe="",
        rss_bytes=0,
        ppid=None,
        cmdline=None,
    ):
        self.pid = pid
        self._name = name
        self._exe = exe
        self._rss = rss_bytes
        self._ppid = ppid
        self._cmdline = tuple(cmdline or ())
        self._children = []

    def add_child(self, child):
        child._ppid = self.pid
        self._children.append(child)

    @property
    def info(self):
        return {
            "pid": self.pid,
            "name": self._name,
            "exe": self._exe,
            "memory_info": FakeMemoryInfo(self._rss),
            "ppid": self._ppid,
            "cmdline": list(self._cmdline),
        }

    def name(self):
        return self._name

    def children(self, recursive=False):
        if not recursive:
            return list(self._children)
        result = list(self._children)
        for child in self._children:
            result.extend(child.children(True))
        return result


class FakePsutil:
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def __init__(self, processes):
        self._by_pid = {proc.pid: proc for proc in processes}

    def process_iter(self, attrs=None):
        return list(self._by_pid.values())

    def Process(self, pid):
        proc = self._by_pid.get(pid)
        if proc is None:
            raise self.NoSuchProcess()
        return proc


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


def test_build_process_tree_aggregates_and_orders(monkeypatch):
    parent = FakeProcess(100, "Parent.exe", exe=r"C:\\Parent.exe", rss_bytes=200 * 1024 * 1024)
    child = FakeProcess(101, "child.exe", exe=r"C:\\child.exe", rss_bytes=100 * 1024 * 1024, ppid=100)
    grandchild = FakeProcess(102, "Worker.exe", exe=r"C:\\Worker.exe", rss_bytes=50 * 1024 * 1024, ppid=101)
    child.add_child(grandchild)
    parent.add_child(child)
    sibling_root = FakeProcess(10, "Alpha.exe", exe=r"C:\\Alpha.exe", rss_bytes=25 * 1024 * 1024)
    system_proc = FakeProcess(999, "System.exe", exe=r"C:\\Windows\\system32\\system.exe", rss_bytes=5 * 1024 * 1024)

    fake = FakePsutil([parent, child, grandchild, sibling_root, system_proc])
    monkeypatch.setattr(process_helpers, "psutil", fake)
    monkeypatch.setattr(process_helpers, "is_system_process", lambda proc: proc is system_proc)

    roots = process_helpers.build_process_tree(include_system=False)
    assert [node.pid for node in roots] == [10, 100]

    parent_node = next(node for node in roots if node.pid == 100)
    assert parent_node.aggregate_rss_mb == pytest.approx(350.0)
    assert parent_node.descendant_pids == (100, 101, 102)

    child_node = next(node for node in parent_node.children if node.pid == 101)
    assert child_node.aggregate_rss_mb == pytest.approx(150.0)
    assert child_node.descendant_pids == (101, 102)
    assert [leaf.pid for leaf in child_node.children] == [102]


def test_match_pids_include_children(monkeypatch):
    parent = FakeProcess(200, "Main.exe", exe=r"C:\\Main.exe")
    child_a = FakeProcess(201, "Helper.exe", exe=r"C:\\Helper.exe", ppid=200)
    child_b = FakeProcess(202, "Worker.exe", exe=r"C:\\Worker.exe", ppid=200)
    grandchild = FakeProcess(203, "Leaf.exe", exe=r"C:\\Leaf.exe", ppid=201)
    child_a.add_child(grandchild)
    parent.add_child(child_a)
    parent.add_child(child_b)
    unrelated = FakeProcess(300, "Other.exe", exe=r"C:\\Other.exe")

    fake = FakePsutil([parent, child_a, child_b, grandchild, unrelated])
    monkeypatch.setattr(process_helpers, "psutil", fake)
    monkeypatch.setattr(process_helpers, "is_system_process", lambda proc: False)

    target = Target(key="Main.exe", match_by="name")
    target.include_children = False
    assert process_helpers.match_pids(target) == [200]

    target.include_children = True
    assert process_helpers.match_pids(target) == [200, 201, 202, 203]
