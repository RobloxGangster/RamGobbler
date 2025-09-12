from dataclasses import dataclass
import os
from typing import List, Dict, Any

@dataclass
class TargetSpec:
    key: str
    match_by: str  # 'name', 'path', 'cmdline', 'parent'


def matches(info: Dict[str, Any], target: TargetSpec) -> bool:
    """Return True if a process info dict matches the target spec."""
    key = target.key.lower()
    mb = target.match_by
    if mb == "name":
        name = (info.get("name") or "").lower()
        return name == key
    if mb == "path":
        exe = os.path.normcase(info.get("exe") or "").lower()
        return exe == os.path.normcase(target.key).lower()
    if mb == "cmdline":
        cmd = " ".join(info.get("cmdline") or [])
        return key in cmd.lower()
    if mb == "parent":
        parent = (info.get("parent") or "").lower()
        return parent == key
    return False
