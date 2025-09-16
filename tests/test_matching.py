import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import matching
from matching import TargetSpec


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
