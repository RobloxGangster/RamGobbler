import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict


@dataclass
class Metrics:
    """Lightweight rollup metrics store."""

    path: str
    rollups: Dict[str, Dict] = field(default_factory=dict)

    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.rollups = json.load(f).get("rollups", {})
            except Exception:
                self.rollups = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"rollups": self.rollups}, f, indent=2)

    def add_event(self, ts: float, delta_mb: float, reason: str, strategy: str) -> None:
        date_key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        r = self.rollups.setdefault(
            date_key,
            {
                "trims": 0,
                "reclaimed_mb": 0.0,
                "by_strategy": {},
                "by_strategy_trims": {},
                "by_reason": {},
            },
        )
        r["trims"] += 1
        r["reclaimed_mb"] += delta_mb
        r["by_strategy"][strategy] = r["by_strategy"].get(strategy, 0.0) + delta_mb
        r["by_strategy_trims"][strategy] = r["by_strategy_trims"].get(strategy, 0) + 1
        r["by_reason"][reason] = r["by_reason"].get(reason, 0.0) + delta_mb

    def _aggregate(self, days: int) -> Dict[str, Dict]:
        cutoff = datetime.now().date() - timedelta(days=days - 1)
        agg = {
            "trims": 0,
            "reclaimed_mb": 0.0,
            "by_strategy": {},
            "by_strategy_trims": {},
            "by_reason": {},
        }
        for date_str, r in self.rollups.items():
            date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if date < cutoff:
                continue
            agg["trims"] += r.get("trims", 0)
            agg["reclaimed_mb"] += r.get("reclaimed_mb", 0.0)
            for k, v in r.get("by_strategy", {}).items():
                agg["by_strategy"][k] = agg["by_strategy"].get(k, 0.0) + v
            for k, v in r.get("by_strategy_trims", {}).items():
                agg["by_strategy_trims"][k] = agg["by_strategy_trims"].get(k, 0) + v
            for k, v in r.get("by_reason", {}).items():
                agg["by_reason"][k] = agg["by_reason"].get(k, 0.0) + v
        return agg

    def export_json(self, days: int) -> str:
        return json.dumps(self._aggregate(days), sort_keys=True)

    def export_csv(self, days: int) -> str:
        agg = self._aggregate(days)
        lines = ["trims,reclaimed_mb"]
        lines.append(f"{agg['trims']},{agg['reclaimed_mb']:.1f}")
        return "\n".join(lines)

