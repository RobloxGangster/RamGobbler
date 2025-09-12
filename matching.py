"""Helpers for matching running processes against user specifications."""

from dataclasses import dataclass
import os
from typing import Any, Dict, Callable


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

    # Normalize the key once so individual strategies can assume lowercase.
    key = target.key.lower()

    def match_name() -> bool:
        # Compare the process name directly against the key.
        return (info.get("name") or "").lower() == key

    def match_path() -> bool:
        # Normalize paths for case-insensitive matching across platforms.
        exe = os.path.normcase(info.get("exe") or "").lower()
        tgt = os.path.normcase(target.key).lower()
        return exe == tgt

    def match_cmdline() -> bool:
        # Flatten command line arguments and search for the key substring.
        cmd = " ".join(info.get("cmdline") or []).lower()
        return key in cmd

    def match_parent() -> bool:
        # Parent names are stored separately; compare against the key.
        return (info.get("parent") or "").lower() == key

    # Dispatch table mapping the ``match_by`` attribute to the appropriate
    # strategy function.  Each callable performs the necessary comparison
    # using the ``info`` dictionary.
    strategies: Dict[str, Callable[[], bool]] = {
        "name": match_name,
        "path": match_path,
        "cmdline": match_cmdline,
        "parent": match_parent,
    }

    # Retrieve and execute the desired strategy; fall back to ``False`` when
    # an unknown strategy is requested.
    func = strategies.get(target.match_by)
    return func() if func else False
