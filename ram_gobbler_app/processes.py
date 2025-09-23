"""Process inspection helpers shared by the UI, gobbler thread and tests.

Every helper in this module wraps a ``psutil`` iterator with additional context
so that even readers new to the project can understand how we discover running
processes, filter them, and forecast how much memory a gobble might reclaim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import statistics
from typing import Dict, Iterable, List, Optional, Tuple

from .deps import psutil
from matching import matches  # type: ignore
from .models import Target
from .system import is_system_process


@dataclass
class ProcessNode:
    """Lightweight structure describing a process and its descendants."""

    pid: int
    name: str
    exe: str
    rss_mb: float
    ppid: Optional[int] = None
    cmdline: Tuple[str, ...] = field(default_factory=tuple)
    children: List["ProcessNode"] = field(default_factory=list)
    aggregate_rss_mb: float = 0.0
    descendant_pids: Tuple[int, ...] = field(default_factory=tuple)

    def walk(self) -> Iterable["ProcessNode"]:
        """Yield this node and every descendant depth-first."""

        yield self
        for child in self.children:
            yield from child.walk()


def build_process_tree(include_system: bool) -> List[ProcessNode]:
    """Return a parent/child tree of running processes.

    The resulting nodes mirror Windows Task Manager's grouped view: parents are
    listed alphabetically with their children directly beneath them and memory
    totals include the full subtree.
    """

    processes: Dict[int, ProcessNode] = {}
    try:
        iterator = psutil.process_iter(
            attrs=["pid", "name", "exe", "memory_info", "ppid", "cmdline"]
        )
    except Exception:
        iterator = []  # pragma: no cover - defensive fallback
    for proc in iterator:
        try:
            if not include_system and is_system_process(proc):
                continue
            info = proc.info
            rss = 0
            if info.get("memory_info"):
                rss = info["memory_info"].rss  # type: ignore[index]
            node = ProcessNode(
                pid=proc.pid,
                name=info.get("name") or "",
                exe=info.get("exe") or "",
                rss_mb=rss / (1024 * 1024),
                ppid=info.get("ppid"),
                cmdline=tuple(info.get("cmdline") or ()),
            )
            processes[proc.pid] = node
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    roots: List[ProcessNode] = []
    for node in processes.values():
        parent = processes.get(node.ppid or -1)
        if parent is None:
            roots.append(node)
            continue
        parent.children.append(node)

    def sort_nodes(items: List[ProcessNode]) -> None:
        items.sort(key=lambda n: (n.name.lower(), n.pid))
        for item in items:
            sort_nodes(item.children)

    sort_nodes(roots)

    def accumulate(node: ProcessNode) -> Tuple[float, Tuple[int, ...]]:
        total = node.rss_mb
        pids: List[int] = [node.pid]
        for child in node.children:
            child_total, child_pids = accumulate(child)
            total += child_total
            pids.extend(child_pids)
        node.aggregate_rss_mb = total
        node.descendant_pids = tuple(pids)
        return total, node.descendant_pids

    for root in roots:
        accumulate(root)

    return roots


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

    # ``dict`` used as an ordered set so repeated matches collapse naturally.
    pids: Dict[int, None] = {}

    try:
        iterator = psutil.process_iter(
            attrs=["pid", "name", "exe", "cmdline", "ppid"]
        )
    except Exception:
        iterator = []  # pragma: no cover - defensive fallback

    # Cache basic metadata so that expensive ``psutil.Process`` lookups are
    # avoided when resolving parent names.  Building the cache up-front keeps
    # the matching loop lean and drastically reduces the number of system calls
    # performed when many processes share the same parent.
    processes: List[Tuple[psutil.Process, Dict[str, object]]] = []
    name_cache: Dict[int, str] = {}

    for proc in iterator:
        try:
            info = proc.info
            processes.append((proc, info))
            name_cache[proc.pid] = str(info.get("name") or "")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    parent_cache: Dict[int, str] = {}

    for proc, info in processes:
        try:
            parent_name = ""
            parent_raw = info.get("ppid")
            parent_pid = parent_raw if isinstance(parent_raw, int) else None
            if parent_pid:
                cached = parent_cache.get(parent_pid)
                if cached is None:
                    cached = name_cache.get(parent_pid, "")
                    if not cached:
                        try:
                            cached = psutil.Process(parent_pid).name()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            cached = ""
                    parent_cache[parent_pid] = cached or ""
                parent_name = cached

            info_payload = {
                "name": info.get("name"),
                "exe": info.get("exe"),
                "cmdline": info.get("cmdline"),
                "parent": parent_name,
            }
            if matches(info_payload, target):
                pids[proc.pid] = None
                if target.include_children:
                    try:
                        for child in proc.children(recursive=True):
                            pids[child.pid] = None
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(pids)


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


__all__ = [
    "ProcessNode",
    "build_process_tree",
    "list_processes",
    "match_pids",
    "estimate_reclaim",
]
