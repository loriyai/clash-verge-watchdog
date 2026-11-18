"""Controller integration tests: fake processes/API, memory state, no desktop I/O."""
import copy
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from clash_watchdog.api import (ApiError, Cancelled, DiagnosticRound, HistoryRecord,
                                Probe, ProxyNode, ProxySnapshot)
from clash_watchdog.controller import Monitor
from clash_watchdog.engine import HealthSample, Options, WatchdogEngine
from clash_watchdog.platform_linux import AppInspection, RestartResult
from clash_watchdog.store import default_state


def snapshot(leaf="leaf"):
    return ProxySnapshot({
        "group": ProxyNode("group", "Selector", leaf, ("leaf", "other"), (), True),
        "leaf": ProxyNode("leaf", "Shadowsocks", history=(HistoryRecord("2026-11-14T00:00:00Z", 1000, 123),)),
        "other": ProxyNode("other", "Shadowsocks"),
    })


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MemoryStore:
    def __init__(self):
        self.saved = []
        self.messages = []
        self.fail_save = False
        self.on_save = None

    def save(self, state):
        if self.fail_save:
            raise OSError("fixture disk unavailable")
        self.saved.append(copy.deepcopy(state))
        if self.on_save:
            self.on_save(state)

    def log(self, message):
        self.messages.append(message)
        return message


class FakeAPI:
    def __init__(self, clock):
        self.clock = clock
        self.snapshots = [snapshot()]
        self.outcome = "failure"
        self.error = None
        self.elapsed = 0
        self.diagnoses = 0
        self.proxy_reads = 0
        self.on_diagnose = None

    def tun_enabled(self, cancel=None):
        return True

    def get_version(self, cancel=None):
        return "Mihomo-fixture"

    def get_proxies(self, cancel=None):
        if cancel and cancel.is_set():
            raise Cancelled("cancelled")
        self.proxy_reads += 1
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def diagnose(self, leaf, primary, timeout_ms, cancel, on_probe):
        self.diagnoses += 1
        if self.error:
            raise self.error
        started = self.clock()
        self.clock.advance(self.elapsed)
        if self.on_diagnose:
            self.on_diagnose(cancel)
        success = self.outcome in ("success", "partial")
        probe = Probe(success, 125 if success else None, started, self.clock(),
                      f"request:{self.diagnoses}", "fixture", 200, None, None, int(self.elapsed*1000))
        on_probe(probe, 0)
        return DiagnosticRound(f"round:{self.diagnoses}", (probe,), success,
                               self.outcome == "partial", probe.delay_ms, started, self.clock())

    def route_snapshot(self, *args, **kwargs):
        raise AssertionError("Optional route observation was disabled in this fixture")


class FakePlatform:
    def __init__(self):
        self.presence = "running"
        self.inspections = self.launches = self.restarts = self.preflights = 0
        self.inspect_error = None
        self.on_inspect = self.on_restart = self.on_launch = self.after_launch = None
        self.ready = True
        self.restart_result = RestartResult(True, "core requested", True)
        self.launch_result = RestartResult(True, "app requested", True)

    def set_tun(self, enabled, cancel=None):
        pass

    def restart_application(self, cancel=None):
        pass

    def inspect_app(self):
        self.inspections += 1
        if self.on_inspect:
            self.on_inspect()
        if self.inspect_error:
            raise self.inspect_error
        return AppInspection(self.presence, "fixture "+self.presence)

    def probe_restart(self, cancel=None):
        self.preflights += 1
        return RestartResult(self.ready, "fixture readiness", target="fixture-target")

    def restart_core(self, expected=None, cancel=None):
        if cancel and cancel.is_set():
            return RestartResult(False, "cancelled")
        if self.on_restart:
            self.on_restart()
        self.restarts += 1
        return self.restart_result

    def launch_app(self, executable, cancel=None):
        if self.on_launch:
            self.on_launch(cancel)
        if cancel and cancel.is_set():
            return RestartResult(False, "cancelled")
        self.launches += 1
        if self.after_launch:
            self.after_launch()
        return self.launch_result


class ControllerTests(unittest.TestCase):
    def setUp(self):
        waiter = patch("clash_watchdog.recovery.Recovery.wait", lambda *_: None)
        waiter.start()
        self.addCleanup(waiter.stop)

    def make(self, active=True, **changes):
        clock = Clock()
        store, platform, events = MemoryStore(), FakePlatform(), []
        api = FakeAPI(clock)
        state = default_state()
        state.update(changes)
        state["group"] = "group"
        monitor = Monitor(store, state, lambda kind, value: events.append((kind, value)),
                          platform=platform, api_factory=lambda url, secret: api, clock=clock)
        monitor.api = api
        if active:
            monitor.running = True
            monitor.monitor_id = "fixture-monitor"
            monitor.engine = WatchdogEngine(Options(**state["options"]), state["restart_attempts"],
                                            state["unrecovered_restart_count"])
            monitor.engine.start(clock()-120, snapshot().resolve("group").target_key)
        monitor.next_aux = float("inf")
        return monitor, store, platform, api, clock, events

    def seed(self, monitor, count=5):
        for i in range(count):
            at = monitor.clock() - (count-i)*5
            monitor.engine.observe(HealthSample(f"seed:{i}", monitor.engine.target_key,
                monitor.engine.generation, at, "failure"), at)

    def app_tick(self, monitor, clock, at):
        clock.now = at
        monitor._app_tick(at)

    def test_connect_and_history_use_actual_api_dataclasses(self):
        monitor, store, platform, api, clock, events = self.make(active=False)
        monitor.connect()
        connected = next(value for kind, value in events if kind == "connected")
        self.assertEqual(["group"], connected["groups"])
        monitor._history(snapshot(), snapshot().resolve("group"))
        self.assertIn("123 ms", events[-1][1])
        self.assertEqual(0, platform.restarts)

    def test_configure_validates_options_without_missing_validate_method(self):
        monitor, store, platform, api, clock, events = self.make(active=False, restart_attempts=[900])
        candidate = default_state()
        monitor.configure(candidate)
        self.assertEqual([900], monitor.state["restart_attempts"])
        self.assertEqual([], candidate["restart_attempts"])
        candidate["options"]["retry_seconds"] = 0
        with self.assertRaises(ValueError):
            monitor.configure(candidate)

    def test_start_initializes_engine_and_workers_without_validate_attribute_error(self):
        monitor, store, platform, api, clock, events = self.make(active=False)
        with patch("clash_watchdog.controller.threading.Thread") as thread:
            monitor.start()
            self.assertEqual(2, thread.call_count)
            self.assertTrue(monitor.running)
            self.assertEqual(5, monitor.engine.options.failure_threshold)
        monitor.stop()

    def test_five_failed_rounds_reserve_durably_before_one_restart(self):
        monitor, store, platform, api, clock, events = self.make()
        def verify_saved():
            self.assertEqual([clock()], store.saved[-1]["restart_attempts"])
            self.assertTrue(monitor.core_busy)
        platform.on_restart = verify_saved
        for index in range(5):
            clock.now = 1000+index*5
            monitor._network_tick(clock())
            self.assertEqual(0 if index < 4 else 1, platform.restarts)
        self.assertEqual(17, api.diagnoses)
        self.assertEqual(0, monitor.engine.consecutive_failures)
        self.assertEqual(clock()+60, monitor.state["rest_until"])

    def test_partial_success_clears_failures_without_restart(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor, 4)
        api.outcome = "partial"
        monitor._network_tick(clock())
        self.assertEqual("partial", monitor.engine.health_state)
        self.assertEqual(0, monitor.engine.consecutive_failures)
        self.assertEqual(0, platform.restarts)

    def test_selection_changed_during_round_discards_old_failure(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor, 4)
        api.snapshots = [snapshot(), snapshot("other")]
        monitor._network_tick(clock())
        self.assertEqual(snapshot("other").resolve("group").target_key, monitor.engine.target_key)
        self.assertEqual(0, monitor.engine.consecutive_failures)
        self.assertFalse(any(kind == "probe" for kind, value in events))
        self.assertTrue(any("discarded_selection_changed" in line for line in store.messages))
        self.assertEqual(0, platform.restarts)

    def test_controller_unknown_preserves_failures_and_cannot_restart(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor, 4)
        api.error = ApiError("controller", "unreachable")
        monitor._network_tick(clock())
        self.assertEqual(4, monitor.engine.consecutive_failures)
        self.assertTrue(monitor.engine.health_unknown)
        self.assertEqual(0, platform.restarts)
        self.assertEqual(1005, monitor.engine.next_probe_at)

    def test_api_error_racing_app_request_cancellation_does_not_end_network_worker(self):
        monitor, store, platform, api, clock, events = self.make()
        original = api.diagnose
        def fail_after_cancel(*args, **kwargs):
            monitor.request_cancel.set()
            monitor.request_cancel = threading.Event()
            raise ApiError("controller", "old request ended")
        api.diagnose = fail_after_cancel
        monitor._network_tick(clock())
        self.assertTrue(monitor.running)
        api.diagnose = original
        clock.advance(30)
        monitor._network_tick(clock())
        self.assertEqual(1, api.diagnoses)
        self.assertEqual(1, monitor.engine.consecutive_failures)

    def test_cancelled_round_has_terminal_log_and_no_late_action(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor, 4)
        api.on_diagnose = lambda token: monitor.stop()
        with self.assertRaises(Cancelled):
            monitor._network_tick(clock())
        self.assertEqual(0, platform.restarts)
        self.assertTrue(any("outcome=cancelled" in line for line in store.messages))
        self.assertFalse(any(kind == "probe" for kind, value in events))

    def test_stop_during_restart_reservation_prevents_dispatch(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor)
        store.on_save = lambda state: monitor.stop() if state["restart_attempts"] else None
        with self.assertRaises(Cancelled):
            monitor._try_restart(snapshot().resolve("group"))
        self.assertEqual(0, platform.restarts)
        self.assertEqual(1, len(store.saved[-1]["restart_attempts"]))

    def test_failed_restart_reservation_never_dispatches(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor)
        store.fail_save = True
        with self.assertRaises(OSError):
            monitor._try_restart(snapshot().resolve("group"))
        self.assertEqual(0, platform.restarts)
        self.assertFalse(monitor.core_busy)
        self.assertEqual([], monitor.engine.export_ledger(1000)["attempts"])

    def test_uncertain_restart_result_persists_manual_pause(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor)
        platform.restart_result = RestartResult(False, "uncertain", True, True)
        monitor._try_restart(snapshot().resolve("group"))
        self.assertTrue(store.saved[-1]["recovery_paused"])
        self.assertFalse(monitor.core_busy)

    def test_failed_resume_save_keeps_recovery_paused(self):
        monitor, store, platform, api, clock, events = self.make(recovery_paused=True)
        store.fail_save = True
        with self.assertRaises(OSError):
            monitor.resume_recovery()
        self.assertTrue(monitor.state["recovery_paused"])

    def test_unavailable_restart_preflight_is_not_requeried_every_fast_round(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor)
        platform.ready = False
        monitor._try_restart(snapshot().resolve("group"))
        clock.advance(5)
        monitor._try_restart(snapshot().resolve("group"))
        self.assertEqual(1, platform.preflights)
        self.assertEqual(0, platform.restarts)

    def test_long_dual_probe_is_not_mistaken_for_idle_scheduler_gap(self):
        monitor, store, platform, api, clock, events = self.make()
        api.elapsed = 20
        waits = []
        def wait(interval):
            waits.append(interval)
            clock.advance(interval)
            if len(waits) >= 2:
                monitor.stop()
            return monitor.cancel.is_set()
        monitor.cancel.wait = wait
        monitor._loop()
        self.assertEqual(1, api.diagnoses)
        self.assertFalse(any("scheduler_gap" in line for line in store.messages))

    def test_real_idle_gap_invalidates_previous_evidence(self):
        monitor, store, platform, api, clock, events = self.make()
        self.seed(monitor, 4)
        monitor.next_poll = float("inf")
        monitor.engine.mark_probe_started(clock())
        waits = []
        def wait(interval):
            waits.append(interval)
            if len(waits) == 1:
                clock.advance(20)
            else:
                monitor.stop()
            return monitor.cancel.is_set()
        monitor.cancel.wait = wait
        monitor._loop()
        self.assertTrue(any("scheduler_gap" in line for line in store.messages))
        self.assertEqual(0, platform.restarts)

    def test_disabled_application_supervisor_never_queries_processes(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=False)
        for at in (1000, 1005, 1010):
            self.app_tick(monitor, clock, at)
        self.assertEqual((0, 0), (platform.inspections, platform.launches))

    def test_app_launch_waits_ten_seconds_and_saves_before_dispatch(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        platform.on_launch = lambda token: self.assertEqual([clock()], store.saved[-1]["launch_attempts"])
        self.app_tick(monitor, clock, 1000)
        self.app_tick(monitor, clock, 1005)
        self.assertEqual(0, platform.launches)
        self.app_tick(monitor, clock, 1010)
        self.assertEqual(1, platform.launches)

    def test_launch_failure_waits_120_seconds_before_next_attempt(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        platform.launch_result = RestartResult(False, "failed")
        for at in range(1000, 1130, 5):
            self.app_tick(monitor, clock, at)
        self.assertEqual(1, platform.launches)
        self.app_tick(monitor, clock, 1130)
        self.assertEqual(2, platform.launches)
        self.assertEqual([1010, 1130], store.saved[-1]["launch_attempts"])

    def test_hourly_app_quota_survives_reload_then_expires_automatically(self):
        monitor, store, platform, api, clock, events = self.make(
            automatic_launch=True, launch_attempts=[1000, 1120, 1240])
        platform.presence = "missing"
        for at in (1500, 1505, 1510):
            self.app_tick(monitor, clock, at)
        self.assertEqual(0, platform.launches)
        for at in (4600, 4605, 4610):
            self.app_tick(monitor, clock, at)
        self.assertEqual(1, platform.launches)
        self.assertEqual([1120, 1240, 4610], store.saved[-1]["launch_attempts"])

    def test_clock_rollback_does_not_discard_future_app_attempt(self):
        monitor, store, platform, api, clock, events = self.make(
            automatic_launch=True, launch_attempts=[1100])
        platform.presence = "missing"
        for at in (1000, 1005, 1010):
            self.app_tick(monitor, clock, at)
        self.assertEqual(0, platform.launches)

    def test_missing_confirmation_blocks_core_and_core_dispatch_blocks_app_query(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        self.seed(monitor)
        platform.presence = "missing"
        self.app_tick(monitor, clock, 1000)
        monitor._try_restart(snapshot().resolve("group"))
        self.assertEqual(0, platform.preflights)
        monitor.core_busy = True
        self.app_tick(monitor, clock, 1005)
        self.assertEqual(1, platform.inspections)
        self.assertEqual((0, 0), (platform.restarts, platform.launches))

    def test_app_launch_cancels_old_network_generation_and_restarts_grace(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        self.seed(monitor, 4)
        platform.presence = "missing"
        old_token, generation = monitor.request_cancel, monitor.engine.generation
        for at in (1000, 1005, 1010):
            self.app_tick(monitor, clock, at)
        self.assertTrue(old_token.is_set())
        self.assertNotEqual(generation, monitor.engine.generation)
        self.assertEqual(0, monitor.engine.consecutive_failures)
        self.assertTrue(monitor.engine.health_unknown)
        self.assertEqual(1040, monitor.engine.grace_until)

    def test_app_launch_save_failure_prevents_dispatch_and_preserves_reservation(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        self.app_tick(monitor, clock, 1000)
        self.app_tick(monitor, clock, 1005)
        store.fail_save = True
        with self.assertRaises(OSError):
            self.app_tick(monitor, clock, 1010)
        self.assertEqual(0, platform.launches)
        self.assertFalse(monitor.app_busy)
        self.app_tick(monitor, clock, 1015)  # No rapid retry despite failed persistence.
        self.assertEqual([1010], monitor.state["launch_attempts"])

    def test_stop_during_app_reservation_prevents_late_launch(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        store.on_save = lambda state: monitor.stop() if state["launch_attempts"] else None
        self.app_tick(monitor, clock, 1000)
        self.app_tick(monitor, clock, 1005)
        with self.assertRaises(Cancelled):
            self.app_tick(monitor, clock, 1010)
        self.assertEqual(0, platform.launches)
        self.assertFalse(monitor.app_busy)

    def test_disable_during_inspection_cancels_without_stopping_monitor(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        platform.on_inspect = lambda: monitor.set_automatic_launch(False)
        with self.assertRaises(Cancelled):
            self.app_tick(monitor, clock, 1000)
        self.assertTrue(monitor.running)
        self.assertFalse(monitor.state["automatic_launch"])
        self.assertEqual(0, platform.launches)

    def test_disable_can_reach_in_flight_launcher_without_waiting_for_monitor_lock(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        observed = []
        def before_launch(token):
            worker = threading.Thread(target=lambda: monitor.set_automatic_launch(False))
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive(), "Disable was blocked behind launcher I/O")
            observed.append(token.is_set())
        platform.on_launch = before_launch
        for at in (1000, 1005, 1010):
            self.app_tick(monitor, clock, at)
        self.assertEqual([True], observed)
        self.assertEqual(0, platform.launches)
        self.assertTrue(monitor.running)

    def test_disabled_after_actual_launch_preserves_off_status_and_clears_old_failures(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        self.seed(monitor, 4)
        old_token = monitor.request_cancel
        platform.after_launch = lambda: monitor.set_automatic_launch(False)
        for at in (1000, 1005, 1010):
            self.app_tick(monitor, clock, at)
        self.assertEqual(1, platform.launches)
        self.assertTrue(old_token.is_set())
        self.assertEqual(0, monitor.engine.consecutive_failures)
        self.assertFalse(monitor.state["automatic_launch"])
        self.assertEqual("已关闭", [value for kind, value in events if kind == "app_status"][-1])

    def test_app_unknown_resets_missing_timer_and_never_launches(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.presence = "missing"
        self.app_tick(monitor, clock, 1000)
        platform.presence = "unknown"
        self.app_tick(monitor, clock, 1005)
        platform.presence = "missing"
        self.app_tick(monitor, clock, 1010)
        self.assertEqual(1010, monitor.missing_since)
        self.assertEqual(0, platform.launches)

    def test_repeated_app_inspection_errors_log_once_not_every_five_seconds(self):
        monitor, store, platform, api, clock, events = self.make(automatic_launch=True)
        platform.inspect_error = OSError("fixture /proc unavailable")
        waits = []
        def wait(interval):
            waits.append(interval)
            clock.advance(interval)
            if len(waits) == 3:
                monitor.stop()
            return monitor.cancel.is_set()
        monitor.cancel.wait = wait
        monitor._app_loop()
        self.assertEqual(3, platform.inspections)
        self.assertEqual(1, sum("app_supervision state=error" in line for line in store.messages))
        self.assertEqual(0, platform.launches)


if __name__ == "__main__":
    unittest.main()
