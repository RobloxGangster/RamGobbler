import os
import sys
import time
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
if ROOT not in sys.path:
    sys.path.append(ROOT)
import ram_gobbler
from ram_gobbler import estimate_reclaim, Target, Config, Gobbler, parse_args
from metrics import Metrics


def test_estimate_reclaim():
    history = [10, 20, 30, 40]
    mn, med, p95 = estimate_reclaim(history)
    assert mn == 10
    assert med == 25
    assert p95 == 30


def test_cooldown_and_reset_cap():
    config = Config('dummy')
    metrics = Metrics('dummy')
    g = Gobbler(config, metrics, lambda *args, **kwargs: None, lambda *args, **kwargs: None)
    now = time.time()
    t = Target(key='a', match_by='name', cooldown_s=5, strategy='reset', reset_cap_per_min=2)
    ok, reason = g._check_cooldown_and_cap(t, now)
    assert ok
    g._post_gobble(t, 150, now)
    ok, reason = g._check_cooldown_and_cap(t, now + 1)
    assert not ok and reason == 'cooldown'
    ok, reason = g._check_cooldown_and_cap(t, now + 6)
    assert ok

    t2 = Target(key='b', match_by='name', strategy='reset', reset_cap_per_min=1)
    ok, reason = g._check_cooldown_and_cap(t2, now)
    assert ok
    g._post_gobble(t2, 10, now)
    ok, reason = g._check_cooldown_and_cap(t2, now + 1)
    assert not ok and reason == 'reset_capped'


def test_parse_args():
    args = parse_args(['--headless', '--gobble-group', 'foo', '--export-metrics', 'out.json'])
    assert args.headless
    assert args.gobble_group == 'foo'
    assert args.export_metrics == 'out.json'
    assert not args.gobble_visible


def test_parse_args_legacy_aliases():
    legacy_visible = f"--{ram_gobbler.LEGACY_PREFIX}-visible"
    legacy_group = f"--{ram_gobbler.LEGACY_PREFIX}-group"
    args = parse_args([legacy_visible, legacy_group, 'foo'])
    assert args.gobble_visible
    assert args.gobble_group == 'foo'
    legacy_visible_attr = legacy_visible.replace('-', '_').lstrip('_')
    legacy_group_attr = legacy_group.replace('-', '_').lstrip('_')
    assert getattr(args, legacy_visible_attr)
    assert getattr(args, legacy_group_attr) == 'foo'


def test_continuous_trigger_bypasses_pressure(tmp_path):
    config = Config(str(tmp_path / 'config.json'))
    config.targets = [Target(key='x', match_by='name', active=True, interval_sec=1)]
    config.jitter_pct = 0
    metrics = Metrics(str(tmp_path / 'metrics.json'))
    results = []

    g = Gobbler(config, metrics, lambda *args, **kwargs: None, lambda *args, **kwargs: None)

    def fake_allow(self):
        return False, 'Pressure off'

    def fake_perform(self, index, target, reason):
        target.last_gobble_ts = time.time()
        results.append(reason)

    g._allow_gobbling_now = types.MethodType(fake_allow, g)
    g._perform_gobble = types.MethodType(fake_perform, g)

    g.start()
    try:
        time.sleep(1.2)
        assert results == []

        g.trigger_now(0, ensure_active=True, continuous=True)
        deadline = time.time() + 6
        while time.time() < deadline and not results:
            time.sleep(0.05)
        assert results and results[0] == 'manual'

        deadline = time.time() + 6
        while time.time() < deadline and not any(reason == 'timer' for reason in results[1:]):
            time.sleep(0.05)

        g.set_active(0, False)
    finally:
        g.stop()
        time.sleep(0.2)

    assert results and results[0] == 'manual'
    assert any(reason == 'timer' for reason in results[1:])
