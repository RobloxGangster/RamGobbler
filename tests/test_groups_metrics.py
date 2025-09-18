import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass

sys.path.append(str(Path(__file__).resolve().parent.parent))

from groups import filter_targets_by_group, group_totals
from metrics import Metrics, LEGACY_COUNT_KEY, LEGACY_BY_STRATEGY_COUNT_KEY


@dataclass
class Target:
    key: str
    match_by: str
    group_id: str = ""
    session_reclaimed_mb: float = 0.0


def test_group_filter_and_totals():
    t1 = Target(key="a", match_by="name", group_id="g1", session_reclaimed_mb=10.0)
    t2 = Target(key="b", match_by="name", group_id="g2", session_reclaimed_mb=5.0)
    t3 = Target(key="c", match_by="name", group_id="", session_reclaimed_mb=1.0)
    filtered = filter_targets_by_group([t1, t2, t3], "g1")
    assert filtered == [t1]
    totals = group_totals([t1, t2, t3])
    assert totals["g1"] == 10.0
    assert totals["g2"] == 5.0
    assert totals[""] == 1.0


def test_metrics_rollup_and_export(tmp_path):
    mpath = tmp_path / "metrics.json"
    m = Metrics(str(mpath))
    now = datetime.now().timestamp()
    m.add_event(now, 5.0, "timer", "standard")
    m.add_event(now, 3.0, "manual", "reset")
    # event 2 days ago
    old = (datetime.now() - timedelta(days=2)).timestamp()
    m.add_event(old, 1.0, "timer", "standard")
    day_json = json.loads(m.export_json(1))
    assert day_json["gobbles"] == 2
    assert abs(day_json["reclaimed_mb"] - 8.0) < 0.01
    csv_lines = m.export_csv(1).splitlines()
    assert csv_lines[0] == "gobbles,reclaimed_mb"
    assert csv_lines[1].startswith("2,8.0") or csv_lines[1].startswith("2,8")
    month_json = json.loads(m.export_json(30))
    assert month_json["gobbles"] == 3
    assert abs(month_json["reclaimed_mb"] - 9.0) < 0.01


def test_metrics_legacy_load(tmp_path):
    today = datetime.now().strftime('%Y-%m-%d')
    payload = {
        "rollups": {
            today: {
                LEGACY_COUNT_KEY: 2,
                "reclaimed_mb": 4.0,
                LEGACY_BY_STRATEGY_COUNT_KEY: {"standard": 2},
                "by_strategy": {"standard": 4.0},
                "by_reason": {"timer": 4.0},
            }
        }
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload))
    metrics = Metrics(str(path))
    metrics.load()
    data = json.loads(metrics.export_json(7))
    assert data["gobbles"] == 2
    assert data["by_strategy_gobbles"]["standard"] == 2

