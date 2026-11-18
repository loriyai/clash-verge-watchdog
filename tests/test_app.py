"""Hidden desktop wiring tests; fake platform only, no Clash or desktop input."""
from __future__ import annotations

from contextlib import contextmanager
import tempfile
import threading
import time
import unittest

from clash_watchdog.app import Window
from clash_watchdog.api import Selection, RouteSnapshot
from clash_watchdog.platform_linux import RestartResult
from clash_watchdog.store import Store


class WindowTests(unittest.TestCase):
    @contextmanager
    def window(self):
        try:
            import tkinter as tk
            root = tk.Tk(className="Clash-verge-watchdog")
        except Exception as exc:
            self.skipTest(f"Tk/display unavailable: {exc}")
        root.withdraw()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            app = Window(root, store, store.load())
            try:
                yield app
            finally:
                if not app.closing:
                    app.close()
                store.close()

    def test_initial_controls_default_on_and_no_monitor_worker(self):
        with self.window() as app:
            self.assertTrue(app.auto_launch.get())
            self.assertFalse(app.monitor.running)
            self.assertIsNone(app.monitor.thread)
            self.assertEqual(app.header.cget("background"), app.NAVY)
            self.assertEqual(str(app.start_button.cget("state")), "disabled")

    def test_api_dataclasses_reach_widgets_without_thread_widget_calls(self):
        with self.window() as app:
            selected = Selection("group", "leaf", ("group", "leaf"), '["group","leaf"]')
            route = RouteSnapshot("rule", "matching", 1, 1, 0, 0, 0, True, time.time(), 5)
            app.events.put(("connected", {"version": "fixture", "groups": ["group"]}))
            app.events.put(("selection", selected))
            app.events.put(("routes", route))
            app.events.put(("history", "100 ms · fixture time"))
            app.drain()
            self.assertEqual(app.vars["group"].get(), "group")
            self.assertIn("group → leaf", app.labels["selection"].cget("text"))
            self.assertIn(route.summary, app.labels["routes"].cget("text"))
            self.assertIn("非界面读数", app.labels["history"].cget("text"))

    def test_header_does_not_hide_partial_or_missing_recovery_capability(self):
        with self.window() as app:
            app.auto_launch.set(False)
            app.monitor.running = True
            app.health = {"state": "healthy", "grace": 0, "paused": False}
            app.monitor.last_readiness = RestartResult(False, "fixture unavailable")
            app.ready_at = time.time()
            app._header()
            self.assertEqual(app.header.cget("background"), app.AMBER)
            app.monitor.last_readiness = RestartResult(True, "fixture available")
            app._header()
            self.assertEqual(app.header.cget("background"), app.GREEN)
            app.auto_launch.set(True)
            app.monitor.last_app_state = "confirming"
            app._header()
            self.assertEqual(app.header.cget("background"), app.AMBER)
            app.auto_launch.set(False)
            app.health["state"] = "partial"
            app._header()
            self.assertEqual(app.header.cget("background"), app.AMBER)
            app.health["state"] = "failed"
            app._header()
            self.assertEqual(app.header.cget("background"), app.RED)

    def test_stopped_event_cannot_leave_running_header(self):
        with self.window() as app:
            app.monitor.running = True
            app.health = {"state": "failed"}
            app.monitor.stop()
            app.drain()
            self.assertEqual(app.labels["status"].cget("text"), "已停止")
            self.assertEqual(app.header.cget("background"), app.NAVY)

    def test_local_http_round_reaches_desktop_as_healthy_without_restart(self):
        from test_api import server, proxies
        from clash_watchdog.api import ClashAPI
        from clash_watchdog.engine import Options, WatchdogEngine
        with self.window() as app, server({"body": proxies()}, {"body": {"delay": 123}}, {"body": proxies()}) as (url, requests):
            state = app.monitor.state
            state.update(controller=url, group="组", automatic_restart=False)
            state["options"]["grace_seconds"] = 0
            app.auto_restart.set(False)
            app.auto_launch.set(False)
            app.monitor.api = ClashAPI(url)
            app.monitor.engine = WatchdogEngine(Options(**state["options"]))
            app.monitor.running = True
            app.monitor.next_aux = float("inf")
            app.monitor._network_tick(time.time())
            app.drain()
            self.assertEqual(len(requests), 3)
            self.assertEqual(app.monitor.engine.health_state, "healthy")
            self.assertIn("123 ms", app.labels["probe"].cget("text"))
            self.assertIn("节点", app.labels["selection"].cget("text"))
            self.assertEqual(app.header.cget("background"), app.GREEN)
            self.assertEqual(app.monitor.state["restart_attempts"], [])

    def test_close_cancels_pending_manual_launch(self):
        with self.window() as app:
            entered, release, completed = threading.Event(), threading.Event(), threading.Event()
            dispatched = []
            def launch(path, cancel):
                entered.set()
                release.wait(2)
                if not cancel.is_set():
                    dispatched.append(path)
                completed.set()
                return RestartResult(False, "cancelled")
            app.monitor.platform.launch_app = launch
            app.launch_clash()
            self.assertTrue(entered.wait(1))
            app.close()
            release.set()
            self.assertTrue(completed.wait(1))
            self.assertFalse(dispatched)
            self.assertTrue(app.lifetime_cancel.is_set())


if __name__ == "__main__":
    unittest.main()
