"""Utilities for dealing with target groups.

This module intentionally keeps the logic small and dependency free so that it
can be reused in both the GUI application and the test-suite.  Targets are
expected to be simple objects (``dataclass`` instances are ideal) exposing a
``group_id`` attribute and, optionally, ``session_reclaimed_mb`` indicating how
much memory has been reclaimed from that target during the current session.
"""

from typing import Iterable, Any, Dict, List
from collections import defaultdict


def filter_targets_by_group(targets: Iterable[Any], group_id: str) -> List[Any]:
    """Return only the targets that belong to ``group_id``.

    ``targets`` may be any iterable of objects with a ``group_id`` attribute.
    If ``group_id`` is an empty string all targets are returned.  The function
    returns a concrete ``list`` so the caller can freely re-use or modify the
    result without consuming the original iterable.
    """

    if not group_id:
        # Short‑circuit to avoid iterating when the caller wants everything.
        return list(targets)

    # Using a list comprehension keeps the logic concise and efficient.
    return [t for t in targets if getattr(t, "group_id", "") == group_id]


def group_totals(targets: Iterable[Any]) -> Dict[str, float]:
    """Aggregate reclaimed memory totals per group.

    Each target may expose ``session_reclaimed_mb``.  Targets without a group
    identifier are aggregated under the empty string key so that callers can
    still account for them.
    """

    # ``defaultdict`` simplifies accumulation by avoiding missing-key checks.
    totals: Dict[str, float] = defaultdict(float)

    for t in targets:
        # Normalize group id to an empty string when missing or ``None``.
        gid = getattr(t, "group_id", "") or ""

        # Add reclaimed memory; missing metrics count as ``0``.
        totals[gid] += getattr(t, "session_reclaimed_mb", 0.0)

    # Convert back to a regular ``dict`` so callers don't depend on defaultdict.
    return dict(totals)

