"""Scheduler service orchestrating gobble execution."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Deque, Dict, List, Tuple

from ..constants import MIN_INTERVAL
from ..core.config_repository import ConfigRepository
from ..core.models import AppConfig, TargetConfig
from ..core.schedule import gate_reason, jittered_interval, window_active
from ..deps import psutil
from ..helpers import get_foreground_pid, is_fullscreen_active, on_battery
from .gobble_service import GobbleService, GobbleSummary
from .process_matcher import ProcessMatcher


@dataclass(frozen=True)
class SchedulerEvent:
    """Notification emitted after a scheduler decision."""

    uid: str
    status: str  # "gobble" or "skip"
    message: str
    timestamp: datetime
    delta_mb: float = 0.0
    reason: str = ""


@dataclass
class TargetRuntime:
    reset_times: Deque[float] = field(default_factory=lambda: deque(maxlen=20))
    next_allowed_ts: float = 0.0
    forced: bool = False


class SchedulerService:
    """Background coordinator that replaces the legacy gobbler thread."""

    def __init__(
        self,
        config_repo: ConfigRepository,
        matcher: ProcessMatcher,
        gobbler: GobbleService,
    ) -> None:
        self._config_repo = config_repo
        self._matcher = matcher
        self._gobbler = gobbler
        self._config = self._config_repo.load()
        self._runtime: Dict[str, TargetRuntime] = {}
        self._listeners: List[Callable[[SchedulerEvent], None]] = []
        self._listeners_lock = threading.Lock()
        self._manual_queue: Deque[Tuple[str, str]] = deque()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.set()
        self._thread = threading.Thread(target=self._run_loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    # --------------------------------------------------------------- listeners
    def subscribe(self, callback: Callable[[SchedulerEvent], None]) -> Callable[[], None]:
        with self._listeners_lock:
            self._listeners.append(callback)

        def _unsubscribe() -> None:
            with self._listeners_lock:
                try:
                    self._listeners.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    def _notify(self, event: SchedulerEvent) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            threading.Thread(target=listener, args=(event,), daemon=True).start()

    # ---------------------------------------------------------- public actions
    def trigger_manual(self, uid: str, *, reason: str = "manual", continuous: bool = False) -> None:
        runtime = self._runtime.setdefault(uid, TargetRuntime())
        if continuous:
            runtime.forced = True
        self._manual_queue.append((uid, reason))
        self._wake.set()

    def clear_forced(self, uid: str) -> None:
        runtime = self._runtime.setdefault(uid, TargetRuntime())
        runtime.forced = False

    # --------------------------------------------------------------- internals
    def _runtime_for(self, target: TargetConfig) -> TargetRuntime:
        return self._runtime.setdefault(target.uid, TargetRuntime())

    def _global_gate(self, config: AppConfig) -> Tuple[bool, str, float]:
        batt_factor = 1.0
        if config.pressure_enabled:
            try:
                vm = psutil.virtual_memory()
                avail_pct = 100.0 * vm.available / max(1, vm.total)
                if avail_pct > config.min_free_percent:
                    return False, "Pressure off", 1.0
            except Exception:
                pass
        if config.gaming_mode and is_fullscreen_active():
            return False, "Paused (Fullscreen)", 1.0
        if config.battery_mode in ("pause", "slow"):
            on_batt, _ = on_battery()
            if config.battery_mode == "pause" and on_batt:
                return False, "Paused (Battery)", 1.0
            if config.battery_mode == "slow" and on_batt:
                batt_factor = max(1.0, config.battery_slow_factor)
        return True, "", batt_factor

    def _check_cooldown(self, target: TargetConfig, runtime: TargetRuntime, now_ts: float) -> Tuple[bool, str]:
        if target.cooldown_s > 0 and now_ts < runtime.next_allowed_ts:
            return False, "cooldown"
        if target.strategy == "reset" and target.reset_cap_per_min > 0:
            while runtime.reset_times and now_ts - runtime.reset_times[0] > 60:
                runtime.reset_times.popleft()
            if len(runtime.reset_times) >= target.reset_cap_per_min:
                return False, "reset_capped"
        return True, ""

    def _apply_summary(self, target: TargetConfig, runtime: TargetRuntime, summary: GobbleSummary) -> TargetConfig:
        now_ts = summary.timestamp.timestamp()
        if target.cooldown_s > 0 and summary.reclaimed_mb >= 100.0:
            runtime.next_allowed_ts = now_ts + target.cooldown_s
        if target.strategy == "reset" and summary.gobbled > 0:
            runtime.reset_times.append(now_ts)
        updated = target.touch_day_rollover(summary.timestamp)
        updated = updated.with_last_result(
            timestamp=now_ts,
            result=summary.message,
            delta_mb=summary.reclaimed_mb,
            reclaimed_delta=summary.reclaimed_mb,
            gobble_reason=summary.reason,
            skip_reason="",
            history_entry=summary.reclaimed_mb if summary.reclaimed_mb > 0 else None,
        )
        interval = updated.interval_sec
        if target.adaptive:
            growth = max(0.0, summary.total_rss_mb - target.last_total_rss_mb)
            if summary.reclaimed_mb >= 50.0 or growth >= 100.0:
                interval = max(5.0, interval - 2.0)
            elif summary.reclaimed_mb < 5.0:
                interval = min(60.0, interval + 2.0)
        updated = replace(updated, interval_sec=interval, last_total_rss_mb=summary.total_rss_mb)
        return updated

    def _persist_target(self, updated: TargetConfig) -> None:
        def _mutator(config: AppConfig) -> AppConfig:
            return config.replace_target(updated.uid, updated)

        self._config = self._config_repo.mutate(_mutator)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            config = self._config
            ok_global, global_reason, batt_factor = self._global_gate(config)
            now_dt = datetime.now()
            now_ts = time.time()
            foreground_pid = get_foreground_pid() or 0
            manual_queue: List[Tuple[str, str]] = []
            while self._manual_queue:
                manual_queue.append(self._manual_queue.popleft())
            for uid, reason in manual_queue:
                target = config.find_target(uid)
                if not target:
                    continue
                runtime = self._runtime_for(target)
                self._handle_target(config, target, runtime, now_dt, now_ts, foreground_pid, reason, ok_global, global_reason)
            for target in config.targets:
                runtime = self._runtime_for(target)
                if not target.active and not runtime.forced:
                    continue
                interval = jittered_interval(target.interval_sec, target.uid or target.key, config.jitter_pct)
                if ok_global and config.battery_mode == "slow":
                    interval *= batt_factor
                if window_active(config.maintenance_window, now_dt) and config.maintenance_window.get("aggressive"):
                    interval = min(interval, 8.0)
                if runtime.forced or now_ts - target.last_gobble_ts >= max(MIN_INTERVAL, interval):
                    self._handle_target(
                        config,
                        target,
                        runtime,
                        now_dt,
                        now_ts,
                        foreground_pid,
                        "timer",
                        ok_global,
                        global_reason,
                    )
            self._wake.wait(timeout=1.0)

    def _handle_target(
        self,
        config: AppConfig,
        target: TargetConfig,
        runtime: TargetRuntime,
        now_dt: datetime,
        now_ts: float,
        foreground_pid: int,
        reason: str,
        ok_global: bool,
        global_reason: str,
    ) -> None:
        allowed, cooldown_reason = self._check_cooldown(target, runtime, now_ts)
        if not allowed:
            self._notify(SchedulerEvent(target.uid, "skip", cooldown_reason, datetime.now(), reason=cooldown_reason))
            return
        if not ok_global:
            self._notify(SchedulerEvent(target.uid, "skip", global_reason, datetime.now(), reason=global_reason))
            return
        gate = gate_reason(config, target, now_dt)
        if gate:
            self._notify(SchedulerEvent(target.uid, "skip", gate, datetime.now(), reason=gate))
            return
        matches = self._matcher.match_pids(target)
        if target.background_only and foreground_pid and foreground_pid in matches:
            self._notify(SchedulerEvent(target.uid, "skip", "foreground", datetime.now(), reason="foreground"))
            return
        if target.cpu_skip_threshold > 0:
            for pid in matches:
                try:
                    if psutil.Process(pid).cpu_percent(interval=None) > target.cpu_skip_threshold:
                        self._notify(SchedulerEvent(target.uid, "skip", "cpu", datetime.now(), reason="cpu"))
                        return
                except Exception:
                    continue
        if target.pause_if_fullscreen and is_fullscreen_active():
            self._notify(SchedulerEvent(target.uid, "skip", "fullscreen", datetime.now(), reason="fullscreen"))
            return
        summary = self._gobbler.gobble(target, protect_system=config.protect_system, reason=reason)
        runtime.forced = False
        updated = self._apply_summary(target, runtime, summary)
        self._persist_target(updated)
        self._notify(
            SchedulerEvent(
                target.uid,
                "gobble",
                summary.message,
                summary.timestamp,
                delta_mb=summary.reclaimed_mb,
                reason=summary.reason,
            )
        )


__all__ = ["SchedulerService", "SchedulerEvent"]
