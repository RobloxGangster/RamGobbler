from datetime import datetime, timedelta

from ram_gobbler_app.services.metrics_service import MetricsService


def test_metrics_record_and_export(tmp_path):
    path = tmp_path / "metrics.json"
    service = MetricsService(str(path), debounce_seconds=0.1)
    service.load()

    now = datetime.now()
    service.record(5.0, reason="timer", strategy="standard", when=now)
    service.record(3.0, reason="manual", strategy="reset", when=now - timedelta(days=1))
    service.flush()

    data = service.aggregate(7)
    assert data["gobbles"] == 2
    assert abs(data["reclaimed_mb"] - 8.0) < 0.01
    exported = service.export_json(7)
    assert "manual" in exported
    csv = service.export_csv(7)
    assert csv.splitlines()[1].startswith("2,8")

    service.close()
    assert path.exists()
