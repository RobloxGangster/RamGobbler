"""Background gobbler thread implementation."""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

from .constants import MIN_INTERVAL
from .deps import psutil
from .helpers import (
    empty_working_set,
    get_foreground_pid,
    helper_gobble,
    is_fullscreen_active,
    on_battery,
    reset_working_set,
    rss_mb,
)
from .models import Config, Target
from .processes import match_pids
from scheduler import gate_reason, jittered_interval, user_idle_seconds, window_active
from .system import is_system_process


class Gobbler(threading.Thread):
    """Background worker that periodically attempts to reclaim memory.

    The class mirrors the behaviour of the original ``Ram Gobbler`` script but
    now exposes a well-defined API that the UI can interact with.  It runs as a
    daemon thread so the process exits cleanly once the main window closes.
    """

    def __init__(
        self,
        config: Config,
        metrics,
        on_result: Callable[[int, str], None],
        on_delta: Callable[[float], None],
    ) -> None:
        super().__init__(daemon=True)
        self.config = config
        self.metrics = metrics
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.on_result = on_result
        self.on_delta = on_delta
        self.helper_warned = False
        self._last_flush = time.time()
        self._reset_times: Dict[str, Deque[float]] = {}
        self._manual_requests: Deque[int] = deque()
        self._forced_uids: Set[str] = set()

    def stop(self) -> None:
        """Signal the thread to terminate and persist metrics."""

        self._stop.set()
        try:
            self.metrics.save()
        except Exception:
            pass

    def _update_day_rollover(self, target: Target) -> None:
        """Reset per-day counters when the calendar day changes."""

        today = datetime.now().strftime("%Y%m%d")
        if target.day_key != today:
            target.day_key = today
            target.day_reclaimed_mb = 0.0

    def _allow_gobbling_now(self) -> Tuple[bool, str]:
        """Check global gating rules such as memory pressure or fullscreen."""

        if self.config.pressure_enabled:
            try:
                vm = psutil.virtual_memory()
                avail_pct = 100.0 * vm.available / max(1, vm.total)
                if avail_pct > self.config.min_free_percent:
                    return False, "Pressure off"
            except Exception:
                pass
        if self.config.gaming_mode and is_fullscreen_active():
            return False, "Paused (Fullscreen)"
        if self.config.battery_mode in ("pause", "slow"):
            on_batt, _ = on_battery()
            if self.config.battery_mode == "pause" and on_batt:
                return False, "Paused (Battery)"
        return True, ""

    def _check_cooldown_and_cap(self, target: Target, now_ts: float) -> Tuple[bool, str]:
        """Enforce target-level cooldowns and reset rate limits."""

        if target.cooldown_s > 0 and now_ts < target.next_allowed_ts:
            return False, "cooldown"
        if target.strategy == "reset" and target.reset_cap_per_min > 0:
            queue = self._reset_times.setdefault(target.key, deque())
            while queue and now_ts - queue[0] > 60:
                queue.popleft()
            if len(queue) >= target.reset_cap_per_min:
                return False, "reset_capped"
        return True, ""

    def _post_gobble(self, target: Target, delta_mb: float, now_ts: float) -> None:
        """Update cooldown trackers after a successful gobble."""

        if target.cooldown_s > 0 and delta_mb >= 100:
            target.next_allowed_ts = now_ts + target.cooldown_s
        if target.strategy == "reset":
            queue = self._reset_times.setdefault(target.key, deque())
            queue.append(now_ts)

    def _record_event(self, index: int, event: dict) -> None:
        """Optionally store diagnostic events for later inspection."""

        if self.config.diag_level > 0:
            with self._lock:
                try:
                    self.config.targets[index]._events.append(event)
                except IndexError:
                    pass

    def run(self) -> None:
        """Main worker loop executed by :class:`threading.Thread`."""

        while not self._stop.is_set():
            manual_requests: List[int] = []
            with self._lock:
                while self._manual_requests:
                    manual_requests.append(self._manual_requests.popleft())
            for manual_index in manual_requests:
                self._process_manual_request(manual_index)

            now_ts = time.time()
            now_dt = datetime.now()
            with self._lock:
                targets_snapshot = list(self.config.targets)
                forced_uids = set(self._forced_uids)
            global_ok, global_reason = self._allow_gobbling_now()
            batt_on, _ = on_battery()
            batt_factor = self.config.battery_slow_factor if (self.config.battery_mode == "slow" and batt_on) else 1.0
            foreground_pid = get_foreground_pid()

            for index, target in enumerate(targets_snapshot):
                if not target.active:
                    continue
                forced = getattr(target, "uid", None) in forced_uids
                # Jitter intervals per target so their timers do not fire in lock
                # step, then slow them down when the machine is on battery.
                base_interval = jittered_interval(target.interval_sec, target.key, self.config.jitter_pct)
                eff_interval = max(MIN_INTERVAL, base_interval * batt_factor)
                in_maintenance = window_active(self.config.maintenance_window, now_dt)
                gobble_reason = "maintenance" if in_maintenance else "timer"
                if in_maintenance and self.config.maintenance_window.get("aggressive"):
                    eff_interval = min(eff_interval, 8.0)
                if now_ts - target.last_gobble_ts < eff_interval:
                    continue
                ok_cool, reason_cool = self._check_cooldown_and_cap(target, now_ts)
                if not ok_cool:
                    self.on_result(index, reason_cool)
                    with self._lock:
                        try:
                            self.config.targets[index].last_skip_reason = reason_cool
                        except IndexError:
                            pass
                    self._record_event(index, {"ts": now_ts, "type": "skip", "reason": reason_cool})
                    continue
                if not forced and not global_ok:
                    self.on_result(index, global_reason)
                    with self._lock:
                        try:
                            self.config.targets[index].last_skip_reason = global_reason
                        except IndexError:
                            pass
                    continue
                reason = gate_reason(self.config, target, now_dt)
                if reason:
                    self.on_result(index, reason)
                    with self._lock:
                        try:
                            self.config.targets[index].last_skip_reason = reason
                        except IndexError:
                            pass
                    self._record_event(index, {"ts": now_ts, "type": "skip", "reason": reason})
                    continue
                if target.background_only and foreground_pid in match_pids(target):
                    reason = "foreground"
                    self.on_result(index, reason)
                    with self._lock:
                        try:
                            self.config.targets[index].last_skip_reason = reason
                        except IndexError:
                            pass
                    continue
                if target.cpu_skip_threshold > 0:
                    skip = False
                    for pid in match_pids(target):
                        try:
                            proc = psutil.Process(pid)
                            if proc.cpu_percent(interval=None) > target.cpu_skip_threshold:
                                skip = True
                                break
                        except Exception:
                            continue
                    if skip:
                        self.on_result(index, "cpu")
                        continue
                if target.pause_if_fullscreen and is_fullscreen_active():
                    self.on_result(index, "fullscreen")
                    continue
                if self.config.idle_seconds > 0 and user_idle_seconds() < self.config.idle_seconds:
                    self.on_result(index, "idle")
                    continue
                self._perform_gobble(index, target, gobble_reason)
            if time.time() - self._last_flush > 10:
                try:
                    self.metrics.save()
                except Exception:
                    pass
                self._last_flush = time.time()
            time.sleep(1)

    def set_active(self, index: int, active: bool) -> None:
        """Toggle whether a target participates in the gobbler loop."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                uid = getattr(self.config.targets[index], "uid", None)
                self.config.targets[index].active = active
                if not active:
                    if self._manual_requests:
                        self._manual_requests = deque(i for i in self._manual_requests if i != index)
                    if uid:
                        self._forced_uids.discard(uid)
                    self.config.targets[index].last_result = "Stopped"
                self.config.save()

    def trigger_now(self, index: int, *, ensure_active: bool = True, continuous: bool = False) -> None:
        """Queue an immediate gobble for the specified target."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                if ensure_active and not self.config.targets[index].active:
                    self.config.targets[index].active = True
                    self.config.save()
                if continuous:
                    uid = getattr(self.config.targets[index], "uid", None)
                    if uid:
                        self._forced_uids.add(uid)
                self._manual_requests.append(index)

    def set_interval(self, index: int, seconds: float) -> None:
        """Change the per-target polling interval."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index].interval_sec = max(MIN_INTERVAL, float(seconds))
                self.config.save()

    def set_autostart(self, index: int, value: bool) -> None:
        """Enable or disable auto-start for the selected target."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index].autostart = bool(value)
                self.config.save()

    def set_group(self, index: int, group_id: str) -> None:
        """Assign the target to the specified group."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index].group_id = group_id or ""
                self.config.save()

    def update_target(self, index: int, new_target: Target) -> None:
        """Replace a target with ``new_target``."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                self.config.targets[index] = new_target
                self.config.save()

    def remove_target(self, index: int) -> None:
        """Delete the target at ``index`` from the configuration."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                uid = getattr(self.config.targets[index], "uid", None)
                if uid:
                    self._forced_uids.discard(uid)
                self.config.targets.pop(index)
                self.config.save()

    def _process_manual_request(self, index: int) -> None:
        """Handle manually queued gobbles triggered from the UI."""

        with self._lock:
            if not (0 <= index < len(self.config.targets)):
                return
            target = self.config.targets[index]
        self._perform_gobble(index, target, "manual")

    def _is_active(self, index: int) -> bool:
        """Check whether a target remains active."""

        with self._lock:
            if 0 <= index < len(self.config.targets):
                return self.config.targets[index].active
            return False

    def _perform_gobble(self, index: int, target: Target, reason: str) -> None:
        """Execute the gobble operation for ``target`` and update state."""

        pids = match_pids(target)
        total_delta = 0.0
        total_rss = 0.0
        gobbled = 0
        helper_needed = False
        aborted = False

        for pid in pids:
            if not self._is_active(index):
                aborted = True
                break
            try:
                if self.config.protect_system and is_system_process(psutil.Process(pid)):
                    continue
            except Exception:
                continue
            before = rss_mb(pid)
            ok = empty_working_set(pid) if target.strategy != "reset" else reset_working_set(pid)
            after = rss_mb(pid)
            delta = 0.0
            if ok:
                gobbled += 1
                if before is not None and after is not None:
                    delta = max(0.0, before - after)
            else:
                ok_helper, helper_delta = helper_gobble(pid, target.strategy)
                if ok_helper:
                    gobbled += 1
                    after = rss_mb(pid)
                    delta = helper_delta
                else:
                    helper_needed = True
                    if before is not None and after is not None:
                        delta = max(0.0, before - after)
            total_delta += delta
            if after is not None:
                total_rss += after

        event_ts = time.time()
        real_target: Optional[Target] = None
        reclaimed_text = f"{total_delta:.1f} MB reclaimed"
        result_msg = reclaimed_text
        if aborted:
            result_msg = f"Stopped — {reclaimed_text}" if total_delta > 0 else "Stopped"
        with self._lock:
            try:
                real_target = self.config.targets[index]
                if getattr(real_target, "uid", None) != getattr(target, "uid", None):
                    real_target = None
                    raise IndexError
                real_target.last_gobble_ts = event_ts
                real_target.last_delta_mb = total_delta
                real_target.last_result = result_msg
                real_target.last_gobble_reason = reason
                real_target.last_skip_reason = ""
                self._update_day_rollover(real_target)
                real_target.session_reclaimed_mb += total_delta
                real_target.day_reclaimed_mb += total_delta
                if total_delta > 0:
                    real_target.history.append(total_delta)
                    real_target.history = real_target.history[-40:]
                if self.config.diag_level > 0:
                    real_target._events.append(
                        {
                            "ts": event_ts,
                            "delta_mb": total_delta,
                            "reason": reason,
                            "ok": gobbled > 0,
                        }
                    )
                if real_target.adaptive:
                    growth = max(0.0, total_rss - real_target.last_total_rss_mb)
                    if total_delta >= 50.0 or growth >= 100.0:
                        real_target.interval_sec = max(5.0, real_target.interval_sec - 2.0)
                    elif total_delta < 5.0:
                        real_target.interval_sec = min(60.0, real_target.interval_sec + 2.0)
                    real_target.last_total_rss_mb = total_rss
            except IndexError:
                real_target = None
        if real_target is not None:
            self._post_gobble(real_target, total_delta, event_ts)
        message = result_msg
        if helper_needed and not self.helper_warned:
            message += " (helper not running)"
            self.helper_warned = True
        self.metrics.add_event(event_ts, total_delta, reason, target.strategy)
        self.on_result(index, message)
        if total_delta > 0:
            self.on_delta(total_delta)


__all__ = ["Gobbler"]
