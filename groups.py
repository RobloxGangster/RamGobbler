from typing import List, Dict, Iterable, Any


def filter_targets_by_group(targets: Iterable[Any], group_id: str) -> List[Any]:
    """Return targets belonging to the specified group.

    Passing an empty string returns all targets.
    """
    if not group_id:
        return list(targets)
    return [t for t in targets if t.group_id == group_id]


def group_totals(targets: Iterable[Any]) -> Dict[str, float]:
    """Compute session reclaimed totals per group."""
    totals: Dict[str, float] = {}
    for t in targets:
        gid = t.group_id or ""
        totals[gid] = totals.get(gid, 0.0) + getattr(t, "session_reclaimed_mb", 0.0)
    return totals

