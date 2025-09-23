"""Efficient process discovery used by gobbling services."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from ..deps import psutil
from ..system import CRITICAL_NAMES, is_system_exe
from ..core.models import TargetConfig
from matching import matches  # type: ignore


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    name: str
    exe: str
    rss_mb: float
    ppid: Optional[int]
    cmdline: Tuple[str, ...]
    username: str = ""

    @property
    def key(self) -> Tuple[int, str]:
        return self.pid, self.name.lower()


@dataclass
class ProcessNode:
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
        yield self
        for child in self.children:
            yield from child.walk()


def _is_system_entry(entry: ProcessEntry) -> bool:
    if entry.name in CRITICAL_NAMES:
        return True
    if is_system_exe(entry.exe):
        return True
    user = entry.username.upper()
    if user.endswith("\\SYSTEM") or user.startswith("NT AUTHORITY\\"):
        return True
    return False


class ProcessMatcher:
    """Cache psutil snapshots for repeated matching operations."""

    def __init__(self, *, ttl: float = 1.0) -> None:
        self.ttl = max(0.1, float(ttl))
        self._lock = threading.Lock()
        self._entries: Dict[int, ProcessEntry] = {}
        self._children: Dict[int, Tuple[int, ...]] = {}
        self._system_flags: Dict[int, bool] = {}
        self._timestamp = 0.0

    # ------------------------------------------------------------ snapshots
    def _refresh_locked(self) -> None:
        entries: Dict[int, ProcessEntry] = {}
        children: Dict[int, List[int]] = defaultdict(list)
        system_flags: Dict[int, bool] = {}
        try:
            iterator = psutil.process_iter(
                attrs=["pid", "name", "exe", "memory_info", "ppid", "cmdline", "username"]
            )
        except Exception:
            iterator = []  # pragma: no cover
        for proc in iterator:
            try:
                info = proc.info
                rss = 0.0
                if info.get("memory_info"):
                    rss = info["memory_info"].rss / (1024 * 1024)
                entry = ProcessEntry(
                    pid=proc.pid,
                    name=info.get("name") or "",
                    exe=info.get("exe") or "",
                    rss_mb=float(rss),
                    ppid=info.get("ppid"),
                    cmdline=tuple(info.get("cmdline") or ()),
                    username=str(info.get("username", "")),
                )
                entries[proc.pid] = entry
                if entry.ppid is not None:
                    children[entry.ppid].append(entry.pid)
                system_flags[entry.pid] = _is_system_entry(entry)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self._entries = entries
        self._children = {pid: tuple(sorted(child_ids)) for pid, child_ids in children.items()}
        self._system_flags = system_flags
        self._timestamp = time.monotonic()

    def _ensure_snapshot(self) -> None:
        with self._lock:
            if time.monotonic() - self._timestamp > self.ttl:
                self._refresh_locked()

    # --------------------------------------------------------------- matching
    def is_system_pid(self, pid: int) -> bool:
        self._ensure_snapshot()
        return self._system_flags.get(pid, False)

    def match_pids(self, target: TargetConfig) -> List[int]:
        self._ensure_snapshot()
        entries = self._entries
        children = self._children
        pids: Dict[int, None] = {}
        for entry in entries.values():
            info = {
                "name": entry.name,
                "exe": entry.exe,
                "cmdline": list(entry.cmdline),
                "parent": entries.get(
                    entry.ppid or -1,
                    ProcessEntry(-1, "", "", 0.0, None, (), ""),
                ).name
                if entry.ppid
                else "",
            }
            if matches(info, target):  # type: ignore[arg-type]
                pids[entry.pid] = None
                if target.include_children:
                    stack = list(children.get(entry.pid, ()))
                    seen = set()
                    while stack:
                        child_pid = stack.pop()
                        if child_pid in seen:
                            continue
                        seen.add(child_pid)
                        pids[child_pid] = None
                        stack.extend(children.get(child_pid, ()))
        return sorted(pids)

    # ----------------------------------------------------------- process tree
    def build_process_tree(self, *, include_system: bool) -> List[ProcessNode]:
        self._ensure_snapshot()
        entries = self._entries
        children = self._children
        nodes: Dict[int, ProcessNode] = {}
        for entry in entries.values():
            if not include_system and self._system_flags.get(entry.pid, False):
                continue
            nodes[entry.pid] = ProcessNode(
                pid=entry.pid,
                name=entry.name,
                exe=entry.exe,
                rss_mb=entry.rss_mb,
                ppid=entry.ppid,
                cmdline=entry.cmdline,
            )
        for node in list(nodes.values()):
            parent = node.ppid
            if parent in nodes:
                nodes[parent].children.append(node)
        roots = [node for node in nodes.values() if node.ppid not in nodes]
        for node in nodes.values():
            node.children.sort(key=lambda n: (n.name.lower(), n.pid))
        roots.sort(key=lambda n: (n.name.lower(), n.pid))

        def accumulate(node: ProcessNode) -> Tuple[float, Tuple[int, ...]]:
            total = node.rss_mb
            descendant: List[int] = [node.pid]
            for child in node.children:
                subtotal, subpids = accumulate(child)
                total += subtotal
                descendant.extend(subpids)
            node.aggregate_rss_mb = total
            node.descendant_pids = tuple(descendant)
            return total, node.descendant_pids

        for root in roots:
            accumulate(root)
        return roots


__all__ = ["ProcessMatcher", "ProcessNode", "ProcessEntry"]
