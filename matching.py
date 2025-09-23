"""Helpers for matching running processes against user specifications."""

from dataclasses import dataclass
import os
from typing import Any, Dict, Callable, Protocol


class TargetLike(Protocol):
    key: str
    match_by: str


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


def matches(info: Dict[str, Any], target: TargetLike) -> bool:
    """Return ``True`` if ``info`` matches ``target``.

    Parameters
    ----------
    info:
        Dictionary describing a process, typically gathered from ``psutil``.
    target:
        ``TargetSpec`` describing the desired matching attributes.
    """

    # Case-normalize the target once so individual strategies operate on a
    # consistent representation.
    key = str(target.key).casefold()

    def match_name() -> bool:
        """Case-insensitive equality on process name."""
        return (info.get("name", "").casefold()) == key

    def match_path() -> bool:
        """Match using a normalized executable path."""
        raw_exe = info.get("exe")
        if raw_exe is None:
            return False
        try:
            exe = os.path.normcase(os.fsdecode(raw_exe)).casefold()
        except (TypeError, ValueError, AttributeError):
            return False
        tgt = os.path.normcase(target.key).casefold()
        return exe == tgt

    def match_cmdline() -> bool:
        """Search for the key as a substring in the command line."""
        cmd = " ".join(info.get("cmdline") or []).casefold()
        return key in cmd

    def match_parent() -> bool:
        """Compare the parent process name case-insensitively."""
        return (info.get("parent", "").casefold()) == key

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
    func = strategies.get(str(target.match_by))
    return func() if func else False
