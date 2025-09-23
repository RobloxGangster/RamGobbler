"""Thread-safe metrics aggregation and persistence."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Mapping, MutableMapping, Optional

LEGACY_PREFIX = "t" + "rim"
LEGACY_COUNT_KEY = f"{LEGACY_PREFIX}s"
LEGACY_BY_STRATEGY_COUNT_KEY = f"by_strategy_{LEGACY_PREFIX}s"


@dataclass(frozen=True)
class Rollup:
    gobbles: int = 0
    reclaimed_mb: float = 0.0
    by_strategy: Mapping[str, float] = field(default_factory=dict)
    by_strategy_gobbles: Mapping[str, int] = field(default_factory=dict)
    by_reason: Mapping[str, float] = field(default_factory=dict)

    def record(self, delta_mb: float, reason: str, strategy: str) -> "Rollup":
        def _update(mapping: Mapping[str, float], key: str, amount: float) -> Mapping[str, float]:
            updated = dict(mapping)
            updated[key] = updated.get(key, 0.0) + amount
            return updated

        def _update_int(mapping: Mapping[str, int], key: str, amount: int) -> Mapping[str, int]:
            updated = dict(mapping)
            updated[key] = updated.get(key, 0) + amount
            return updated

        return Rollup(
            gobbles=self.gobbles + 1,
            reclaimed_mb=self.reclaimed_mb + float(delta_mb),
            by_strategy=_update(self.by_strategy, strategy, float(delta_mb)),
            by_strategy_gobbles=_update_int(self.by_strategy_gobbles, strategy, 1),
            by_reason=_update(self.by_reason, reason, float(delta_mb)),
        )

    def to_json(self) -> Mapping[str, object]:
        return {
            "gobbles": self.gobbles,
            "reclaimed_mb": self.reclaimed_mb,
            "by_strategy": dict(self.by_strategy),
            "by_strategy_gobbles": dict(self.by_strategy_gobbles),
            "by_reason": dict(self.by_reason),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> "Rollup":
        gobbles = int(payload.get("gobbles", payload.get(LEGACY_COUNT_KEY, 0)))
        reclaimed = float(payload.get("reclaimed_mb", 0.0))
        by_strategy = payload.get("by_strategy", {}) or {}
        by_strategy_counts = payload.get("by_strategy_gobbles", payload.get(LEGACY_BY_STRATEGY_COUNT_KEY, {})) or {}
        by_reason = payload.get("by_reason", {}) or {}
        return cls(
            gobbles=gobbles,
            reclaimed_mb=reclaimed,
            by_strategy={k: float(v) for k, v in by_strategy.items() if isinstance(k, str)},
            by_strategy_gobbles={k: int(v) for k, v in by_strategy_counts.items() if isinstance(k, str)},
            by_reason={k: float(v) for k, v in by_reason.items() if isinstance(k, str)},
        )


@dataclass
class MetricsState:
    rollups: Dict[str, Rollup] = field(default_factory=dict)

    def record(self, when: datetime, delta_mb: float, reason: str, strategy: str) -> "MetricsState":
        key = when.strftime("%Y-%m-%d")
        current = self.rollups.get(key, Rollup())
        updated = dict(self.rollups)
        updated[key] = current.record(delta_mb, reason, strategy)
        return replace(self, rollups=updated)

    def to_json(self) -> Mapping[str, object]:
        return {"rollups": {day: rollup.to_json() for day, rollup in self.rollups.items()}}

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> "MetricsState":
        raw_rollups = payload.get("rollups", {})
        if not isinstance(raw_rollups, Mapping):
            return cls()
        rollups: Dict[str, Rollup] = {}
        for day, raw in raw_rollups.items():
            if not isinstance(day, str) or not isinstance(raw, Mapping):
                continue
            rollups[day] = Rollup.from_json(raw)
        return cls(rollups=rollups)

    def aggregate(self, days: int) -> Mapping[str, object]:
        cutoff = datetime.now().date() - timedelta(days=days - 1)
        agg: MutableMapping[str, object] = {
            "gobbles": 0,
            "reclaimed_mb": 0.0,
            "by_strategy": {},
            "by_strategy_gobbles": {},
            "by_reason": {},
        }
        for day, rollup in self.rollups.items():
            try:
                day_dt = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day_dt < cutoff:
                continue
            agg["gobbles"] = int(agg["gobbles"]) + rollup.gobbles
            agg["reclaimed_mb"] = float(agg["reclaimed_mb"]) + rollup.reclaimed_mb
            by_strategy: MutableMapping[str, float] = agg["by_strategy"]  # type: ignore[assignment]
            by_strategy_gobbles: MutableMapping[str, int] = agg["by_strategy_gobbles"]  # type: ignore[assignment]
            by_reason: MutableMapping[str, float] = agg["by_reason"]  # type: ignore[assignment]
            for key, value in rollup.by_strategy.items():
                by_strategy[key] = by_strategy.get(key, 0.0) + value
            for key, value in rollup.by_strategy_gobbles.items():
                by_strategy_gobbles[key] = by_strategy_gobbles.get(key, 0) + value
            for key, value in rollup.by_reason.items():
                by_reason[key] = by_reason.get(key, 0.0) + value
        return agg


class MetricsService:
    """Aggregate metrics and persist them with debounced writes."""

    def __init__(self, path: str, *, debounce_seconds: float = 2.0) -> None:
        self.path = Path(path)
        self.debounce_seconds = max(0.1, float(debounce_seconds))
        self._state = MetricsState()
        self._lock = threading.Lock()
        self._flush_timer: threading.Timer | None = None

    # ------------------------------------------------------------- lifecycle
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return
        with self._lock:
            self._state = MetricsState.from_json(payload)

    def close(self) -> None:
        with self._lock:
            timer = self._flush_timer
            self._flush_timer = None
        if timer is not None:
            timer.cancel()
        self.flush()

    # --------------------------------------------------------------- updates
    def record(self, delta_mb: float, *, reason: str, strategy: str, when: Optional[datetime] = None) -> None:
        when = when or datetime.now()
        with self._lock:
            self._state = self._state.record(when, delta_mb, reason, strategy)
            self._ensure_flush_timer_locked()

    def _ensure_flush_timer_locked(self) -> None:
        if self._flush_timer and self._flush_timer.is_alive():
            return
        def _flush_wrapper() -> None:
            try:
                self.flush()
            finally:
                with self._lock:
                    self._flush_timer = None
        timer = threading.Timer(self.debounce_seconds, _flush_wrapper)
        timer.daemon = True
        self._flush_timer = timer
        timer.start()

    # -------------------------------------------------------------- exports
    def aggregate(self, days: int) -> Mapping[str, object]:
        with self._lock:
            state = self._state
        return state.aggregate(days)

    def export_json(self, days: int) -> str:
        return json.dumps(self.aggregate(days), separators=(",", ":"), sort_keys=True)

    def export_csv(self, days: int) -> str:
        agg = self.aggregate(days)
        lines = ["gobbles,reclaimed_mb"]
        lines.append(f"{agg['gobbles']},{float(agg['reclaimed_mb']):.1f}")
        return "\n".join(lines)

    # --------------------------------------------------------------- persistence
    def flush(self) -> None:
        with self._lock:
            state = self._state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(state.to_json(), handle, indent=2, sort_keys=True)
        temp_path.replace(self.path)


__all__ = ["MetricsService", "Rollup", "MetricsState", "LEGACY_COUNT_KEY", "LEGACY_BY_STRATEGY_COUNT_KEY"]
