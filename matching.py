"""Helpers for matching running processes against user specifications."""

from dataclasses import dataclass
import os
from typing import Any, Dict


@dataclass
class TargetSpec:
    """Description of a process to match.

    Attributes
    ----------
    key:
        Value to match against.  Interpretation depends on ``match_by``.
    match_by:
        Strategy used for matching: one of ``"name"``, ``"path"``,
        ``"cmdline"`` or ``"parent"``.
    """

    key: str
    match_by: str  # 'name', 'path', 'cmdline', 'parent'


def matches(info: Dict[str, Any], target: TargetSpec) -> bool:
    """Return ``True`` if ``info`` matches ``target``.

    Parameters
    ----------
    info:
        Dictionary describing a process, typically gathered from ``psutil``.
    target:
        ``TargetSpec`` describing the desired matching attributes.
    """

    key = target.key.lower()
    mb = target.match_by

    if mb == "name":
        # Compare executable names case-insensitively.
        name = (info.get("name") or "").lower()
        return name == key

    if mb == "path":
        # Normalise paths so case and separator differences do not matter.
        exe = os.path.normcase(info.get("exe") or "").lower()
        return exe == os.path.normcase(target.key).lower()

    if mb == "cmdline":
        # ``cmdline`` is a list; join and perform a substring search.
        cmd = " ".join(info.get("cmdline") or [])
        return key in cmd.lower()

    if mb == "parent":
        parent = (info.get("parent") or "").lower()
        return parent == key

    # Unknown strategy -> no match.
    return False
