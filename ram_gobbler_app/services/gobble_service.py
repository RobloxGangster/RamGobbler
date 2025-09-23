"""Service responsible for executing gobble operations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Tuple

from ..core.models import TargetConfig
from ..helpers import empty_working_set, helper_gobble, reset_working_set, rss_mb
from .metrics_service import MetricsService
from .process_matcher import ProcessMatcher


@dataclass(frozen=True)
class GobbleSummary:
    timestamp: datetime
    reclaimed_mb: float
    gobbled: int
    helper_needed: bool
    total_rss_mb: float
    pids: Tuple[int, ...]
    reason: str
    strategy: str

    @property
    def message(self) -> str:
        if self.reclaimed_mb > 0:
            return f"{self.reclaimed_mb:.1f} MB reclaimed"
        return "No memory reclaimed"


class GobbleService:
    """Coordinate the low-level gobble operations for a target."""

    def __init__(
        self,
        matcher: ProcessMatcher,
        metrics: MetricsService,
        *,
        empty_fn: Callable[[int], bool] = empty_working_set,
        reset_fn: Callable[[int], bool] = reset_working_set,
        helper_fn: Callable[[int, str], Tuple[bool, float]] = helper_gobble,
    ) -> None:
        self._matcher = matcher
        self._metrics = metrics
        self._empty = empty_fn
        self._reset = reset_fn
        self._helper = helper_fn

    def gobble(
        self,
        target: TargetConfig,
        *,
        protect_system: bool,
        reason: str,
        when: datetime | None = None,
    ) -> GobbleSummary:
        """Execute a gobble for ``target`` and return a summary."""

        when = when or datetime.now()
        pids = self._matcher.match_pids(target)
        total_delta = 0.0
        total_rss = 0.0
        gobbled = 0
        helper_needed = False
        handled_pids: list[int] = []
        for pid in pids:
            if protect_system and self._matcher.is_system_pid(pid):
                continue
            before = rss_mb(pid)
            ok = False
            if target.strategy == "reset":
                ok = self._reset(pid)
                if not ok:
                    ok = self._empty(pid)
            else:
                ok = self._empty(pid)
            after = rss_mb(pid)
            delta = 0.0
            if ok:
                gobbled += 1
                if before is not None and after is not None:
                    delta = max(0.0, before - after)
            else:
                helper_ok, helper_delta = self._helper(pid, target.strategy)
                if helper_ok:
                    gobbled += 1
                    delta = helper_delta
                    after = rss_mb(pid)
                else:
                    helper_needed = True
                    if before is not None and after is not None:
                        delta = max(0.0, before - after)
            total_delta += delta
            if after is not None:
                total_rss += after
            handled_pids.append(pid)
        summary = GobbleSummary(
            timestamp=when,
            reclaimed_mb=total_delta,
            gobbled=gobbled,
            helper_needed=helper_needed,
            total_rss_mb=total_rss,
            pids=tuple(handled_pids),
            reason=reason,
            strategy=target.strategy,
        )
        self._metrics.record(total_delta, reason=reason, strategy=target.strategy, when=when)
        return summary


__all__ = ["GobbleService", "GobbleSummary"]
