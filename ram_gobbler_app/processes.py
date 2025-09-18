"""Process inspection helpers shared by the UI, gobbler thread and tests.

Every helper in this module wraps a ``psutil`` iterator with additional context
so that even readers new to the project can understand how we discover running
processes, filter them, and forecast how much memory a gobble might reclaim.
"""

from __future__ import annotations

import statistics
from typing import List, Tuple

from .deps import psutil
from matching import matches  # type: ignore
from .models import Target
from .system import is_system_process


def list_processes(include_system: bool) -> List[psutil.Process]:
    """Enumerate running processes, optionally filtering out system ones."""

    processes: List[psutil.Process] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "exe", "memory_info"]):
        try:
            _ = proc.info
            if not include_system and is_system_process(proc):
                continue
            processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return processes


def match_pids(target: Target) -> List[int]:
    """Return process identifiers matching ``target``'s selection rules."""

    pids: List[int] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "exe", "cmdline", "ppid"]):
        try:
            parent_name = ""
            parent_pid = proc.info.get("ppid")
            if parent_pid:
                try:
                    parent_name = psutil.Process(parent_pid).name()
                except Exception:
                    parent_name = ""
            info = {
                "name": proc.info.get("name"),
                "exe": proc.info.get("exe"),
                "cmdline": proc.info.get("cmdline"),
                "parent": parent_name,
            }
            if matches(info, target):
                pids.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def estimate_reclaim(history: List[float]) -> Tuple[float, float, float]:
    """Return ``(min, median, p95)`` reclaimed values from ``history``."""

    if not history:
        return 0.0, 0.0, 0.0
    sample = history[-10:]
    sample_sorted = sorted(sample)
    median = statistics.median(sample_sorted)
    idx = int((len(sample_sorted) - 1) * 0.95)
    p95 = sample_sorted[idx]
    return float(sample_sorted[0]), float(median), float(p95)


__all__ = ["list_processes", "match_pids", "estimate_reclaim"]
