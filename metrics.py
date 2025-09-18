"""Persistence layer for usage metrics.

The :class:`Metrics` class stores daily roll‑ups of gobbling activity and can
export aggregates in JSON or CSV format.  The on‑disk format is intentionally
simple so that tools other than the main application can read or manipulate it
easily.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict

LEGACY_PREFIX = "t" + "rim"
LEGACY_COUNT_KEY = f"{LEGACY_PREFIX}s"
LEGACY_BY_STRATEGY_COUNT_KEY = f"by_strategy_{LEGACY_PREFIX}s"


@dataclass
class Metrics:
    """Lightweight rollup metrics store."""

    path: str
    rollups: Dict[str, Dict] = field(default_factory=dict)

    # ------------------------------------------------------------------ I/O --
    def load(self) -> None:
        """Load metrics from ``self.path`` if it exists."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f).get("rollups", {})
                migrated: Dict[str, Dict] = {}
                for day, payload in raw.items():
                    record = dict(payload)
                    if LEGACY_COUNT_KEY in record and "gobbles" not in record:
                        record["gobbles"] = record.pop(LEGACY_COUNT_KEY)
                    if LEGACY_BY_STRATEGY_COUNT_KEY in record and "by_strategy_gobbles" not in record:
                        record["by_strategy_gobbles"] = record.pop(LEGACY_BY_STRATEGY_COUNT_KEY)
                    migrated[day] = record
                self.rollups = migrated
            except Exception:
                # Corrupted files are ignored; start with a clean slate.
                self.rollups = {}

    def save(self) -> None:
        """Persist current metrics to ``self.path``."""
        # ``os.path.dirname`` returns an empty string when ``path`` has no
        # directory component; default to ``.`` so ``makedirs`` succeeds.
        dir_path = os.path.dirname(self.path) or "."
        os.makedirs(dir_path, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {"rollups": self.rollups},
                f,
                separators=(",", ":"),
                sort_keys=True,
            )

    # --------------------------------------------------------------- Updates --
    def add_event(self, ts: float, delta_mb: float, reason: str, strategy: str) -> None:
        """Record a gobbling event."""
        date_key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        r = self.rollups.setdefault(
            date_key,
            {
                "gobbles": 0,
                "reclaimed_mb": 0.0,
                "by_strategy": {},
                "by_strategy_gobbles": {},
                "by_reason": {},
            },
        )
        r["gobbles"] = r.get("gobbles", 0) + 1
        r["reclaimed_mb"] += delta_mb
        r["by_strategy"][strategy] = r["by_strategy"].get(strategy, 0.0) + delta_mb
        r["by_strategy_gobbles"][strategy] = r["by_strategy_gobbles"].get(strategy, 0) + 1
        r["by_reason"][reason] = r["by_reason"].get(reason, 0.0) + delta_mb

    # --------------------------------------------------------------- Helpers --
    def _aggregate(self, days: int) -> Dict[str, Dict]:
        """Aggregate metrics for the last ``days`` days."""
        cutoff = datetime.now().date() - timedelta(days=days - 1)
        agg = {
            "gobbles": 0,
            "reclaimed_mb": 0.0,
            "by_strategy": {},
            "by_strategy_gobbles": {},
            "by_reason": {},
        }
        for date_str, r in self.rollups.items():
            date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if date < cutoff:
                continue
            agg["gobbles"] += r.get("gobbles", r.get(LEGACY_COUNT_KEY, 0))
            agg["reclaimed_mb"] += r.get("reclaimed_mb", 0.0)
            for k, v in r.get("by_strategy", {}).items():
                agg["by_strategy"][k] = agg["by_strategy"].get(k, 0.0) + v
            strat_counts = r.get("by_strategy_gobbles", r.get(LEGACY_BY_STRATEGY_COUNT_KEY, {}))
            for k, v in strat_counts.items():
                agg["by_strategy_gobbles"][k] = agg["by_strategy_gobbles"].get(k, 0) + v
            for k, v in r.get("by_reason", {}).items():
                agg["by_reason"][k] = agg["by_reason"].get(k, 0.0) + v
        return agg

    # --------------------------------------------------------------- Exports --
    def export_json(self, days: int) -> str:
        """Return metrics as a JSON string for the last ``days`` days."""
        return json.dumps(
            self._aggregate(days), separators=(",", ":"), sort_keys=True
        )

    def export_csv(self, days: int) -> str:
        """Return metrics as a small CSV block for the last ``days`` days."""
        agg = self._aggregate(days)
        lines = ["gobbles,reclaimed_mb"]
        lines.append(f"{agg['gobbles']},{agg['reclaimed_mb']:.1f}")
        return "\n".join(lines)

