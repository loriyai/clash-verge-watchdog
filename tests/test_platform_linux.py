from pathlib import Path
import os
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from clash_watchdog.platform_linux import (
    AppInspection, Cancelled, GioSessionBus, LinuxPlatform, PlatformError,
    ProcessIdentity, default_data_directory, inspect_processes, normalize_controller,
    read_connection, restart_menu_item, validate_executable,
)


def menu(core_label="重启 Clash 内核"):
    return (0, {}, [(1, {"label": "更多", "children-display": "submenu"}, [
        (2, {"label": core_label}, []), (3, {"label": "重启应用"}, [])])])


class FakeBus:
    def __init__(self):
        self.items = ((":1.17", "/item"),)
        self.identity = (500, 1000)
        self.properties = {"Id": "tray-icon tray app clash-verge-rev-tray", "Menu": "/menu"}
        self.layout = menu()
        self.revision = 9
        self.events = []
        self.on_click = None

    def discover_items(self, pid, uid, cancel=None):
        return self.items

    def owner_identity(self, owner):
        return self.identity

    def item_properties(self, owner, path):
        return self.properties

    def menu_layout(self, owner, path):
        return self.revision, self.layout

    def click(self, owner, path, item_id, cancel=None):
        if self.on_click:
            self.on_click()
        self.events.append((owner, path, item_id))


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.identity = ProcessIdentity(500, 1000, "1234", "/usr/bin/clash-verge")
        self.presence = AppInspection("running", "running", (self.identity,))
        self.bus = FakeBus()
        self.platform = LinuxPlatform(lambda: self.presence, self.bus)

    def test_exact_core_menu_only(self):
        self.assertEqual(restart_menu_item(menu()), (2, "重启 Clash 内核", 1))
        english = (0, {}, [(10, {"label": "_More"}, [(20, {"label": "Restart Clash Core"}, [])])])
        self.assertEqual(restart_menu_item(english)[0], 20)

    def test_application_restart_never_selected(self):
        with self.assertRaises(PlatformError):
            restart_menu_item(menu("重启应用"))

    def test_proxy_named_restart_does_not_become_action(self):
        value = (0, {}, [(5, {"label": "代理"}, [(6, {"label": "更多"}, [(7, {"label": "重启 Clash 内核"}, [])])])])
        with self.assertRaises(PlatformError):
            restart_menu_item(value)

    def test_disabled_parent_blocks_restart(self):
        value = menu()
        value[2][0][1]["enabled"] = False
        with self.assertRaises(PlatformError):
            restart_menu_item(value)

    def test_invisible_child_blocks_restart(self):
        value = menu()
        value[2][0][2][0][1]["visible"] = False
        with self.assertRaises(PlatformError):
            restart_menu_item(value)

    def test_duplicate_core_actions_are_rejected(self):
        value = menu()
        value[2][0][2].append((4, {"label": "重启 Clash 内核"}, []))
        with self.assertRaises(PlatformError):
            restart_menu_item(value)

    def test_duplicate_ids_are_rejected(self):
        value = menu()
        value[2][0][2].append((2, {"label": "other"}, []))
        with self.assertRaises(PlatformError):
            restart_menu_item(value)

    def test_readiness_never_sends_event(self):
        result = self.platform.probe_restart()
        self.assertTrue(result.available)
        self.assertFalse(result.dispatched)
        self.assertEqual(self.bus.events, [])

    def test_restart_dispatches_one_core_action(self):
        target = self.platform.probe_restart().target
        result = self.platform.restart_core(target)
        self.assertTrue(result.dispatched)
        self.assertFalse(result.uncertain)
        self.assertEqual(self.bus.events, [(":1.17", "/menu", 2)])

    def test_unrelated_revision_change_uses_new_verified_layout(self):
        target = self.platform.probe_restart().target
        self.bus.revision += 1
        result = self.platform.restart_core(target)
        self.assertTrue(result.dispatched)
        self.assertFalse(result.uncertain)
        self.assertEqual(result.target.revision, self.bus.revision)
        self.assertEqual(self.bus.events, [(":1.17", "/menu", 2)])

    def test_revision_and_core_item_id_change_prevents_dispatch(self):
        target = self.platform.probe_restart().target
        self.bus.revision += 1
        self.bus.layout[2][0][2][0] = (20, {"label": "重启 Clash 内核"}, [])
        self.assertFalse(self.platform.restart_core(target).dispatched)
        self.assertEqual(self.bus.events, [])

    def test_parent_id_change_prevents_dispatch(self):
        target = self.platform.probe_restart().target
        parent = self.bus.layout[2][0]
        self.bus.layout[2][0] = (10, parent[1], parent[2])
        self.assertFalse(self.platform.restart_core(target).dispatched)

    def test_core_label_change_prevents_dispatch_even_when_both_supported(self):
        target = self.platform.probe_restart().target
        self.bus.layout[2][0][2][0][1]["label"] = "Restart Clash Core"
        self.assertFalse(self.platform.restart_core(target).dispatched)

    def test_menu_path_change_prevents_dispatch(self):
        target = self.platform.probe_restart().target
        self.bus.properties["Menu"] = "/new_menu"
        self.assertFalse(self.platform.restart_core(target).dispatched)

    def test_owner_is_rechecked_immediately_before_dispatch(self):
        target = self.platform.probe_restart().target
        identities = iter(((500, 1000), (500, 1000), (600, 1000)))
        self.bus.owner_identity = lambda owner: next(identities)
        self.assertFalse(self.platform.restart_core(target).dispatched)
        self.assertEqual(self.bus.events, [])

    def test_process_is_rechecked_immediately_before_dispatch(self):
        target = self.platform.probe_restart().target
        states = iter((self.presence, self.presence, AppInspection("missing", "gone")))
        self.platform._inspector = lambda: next(states)
        self.assertFalse(self.platform.restart_core(target).dispatched)
        self.assertEqual(self.bus.events, [])

    def test_owner_change_prevents_dispatch(self):
        target = self.platform.probe_restart().target
        self.bus.items = ((":1.18", "/item"),)
        result = self.platform.restart_core(target)
        self.assertFalse(result.dispatched)

    def test_pid_reuse_prevents_dispatch(self):
        target = self.platform.probe_restart().target
        self.presence = AppInspection("running", "running", (ProcessIdentity(500, 1000, "1235", "/usr/bin/clash-verge"),))
        self.assertFalse(self.platform.restart_core(target).dispatched)

    def test_wrong_uid_is_not_trusted(self):
        self.bus.identity = (500, 1001)
        self.assertFalse(self.platform.probe_restart().available)

    def test_wrong_pid_is_not_trusted(self):
        self.bus.identity = (501, 1000)
        self.assertFalse(self.platform.probe_restart().available)

    def test_nonunique_bus_name_is_not_trusted(self):
        self.bus.items = (("org.fake", "/item"),)
        self.assertFalse(self.platform.probe_restart().available)

    def test_unrelated_tray_id_is_not_trusted(self):
        self.bus.properties["Id"] = "another-tray"
        self.assertFalse(self.platform.probe_restart().available)

    def test_two_distinct_clash_tray_objects_are_rejected(self):
        self.bus.items = ((":1.17", "/first"), (":1.17", "/second"))
        self.assertFalse(self.platform.probe_restart().available)

    def test_same_registered_object_alias_is_deduplicated(self):
        self.bus.items = ((":1.17", "/item"), (":1.17", "/item"))
        self.assertTrue(self.platform.probe_restart().available)

    def test_cancelled_restart_never_dispatches(self):
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(self.platform.restart_core(cancel=cancel).dispatched)
        self.assertEqual(self.bus.events, [])

    def test_event_failure_is_uncertain_and_not_retried(self):
        def fail():
            raise TimeoutError()
        self.bus.on_click = fail
        result = self.platform.restart_core()
        self.assertTrue(result.dispatched)
        self.assertTrue(result.uncertain)
        self.assertEqual(self.bus.events, [])

    def test_inspector_failure_blocks_both_actions(self):
        platform = LinuxPlatform(lambda: (_ for _ in ()).throw(OSError()), self.bus)
        self.assertEqual(platform.inspect_app().state, "unknown")
        self.assertFalse(platform.probe_restart().available)

    def test_process_changes_while_reading_block_readiness(self):
        sequence = iter((self.presence, AppInspection("missing", "gone")))
        platform = LinuxPlatform(lambda: next(sequence), self.bus)
        self.assertFalse(platform.probe_restart().available)

    def test_parallel_action_is_not_dispatched(self):
        self.platform._action_lock.acquire()
        try:
            self.assertFalse(self.platform.restart_core().dispatched)
        finally:
            self.platform._action_lock.release()

    def test_launch_uses_argument_list_and_existing_process_blocks_it(self):
        calls = []
        with patch("clash_watchdog.platform_linux.validate_executable", return_value="/opt/Clash Verge/clash-verge"):
            platform = LinuxPlatform(lambda: self.presence, self.bus, lambda *a, **k: calls.append((a, k)))
            self.assertFalse(platform.launch_app("/ignored").dispatched)
            self.presence = AppInspection("missing", "missing")
            self.assertTrue(platform.launch_app("/ignored").dispatched)
        self.assertEqual(calls[0][0][0], ["/opt/Clash Verge/clash-verge"])
        self.assertNotIn("shell", calls[0][1])
        self.assertTrue(calls[0][1]["start_new_session"])

    def test_launch_cancelled_before_validation(self):
        cancel = threading.Event()
        cancel.set()
        with patch("clash_watchdog.platform_linux.validate_executable") as validate:
            self.assertFalse(self.platform.launch_app("/any", cancel).dispatched)
            validate.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_only_loopback_controller_addresses(self):
        self.assertEqual(normalize_controller("127.0.0.1:9097"), "http://127.0.0.1:9097")
        self.assertEqual(normalize_controller("http://localhost:9090"), "http://127.0.0.1:9090")
        self.assertEqual(normalize_controller("[::1]:9090"), "http://[::1]:9090")
        for address in ("0.0.0.0:9097", "example.com:9097", "http://user:pass@localhost:9097", "http://localhost:9097/path", "http://localhost:9097#evil"):
            with self.subTest(address=address), self.assertRaises(PlatformError):
                normalize_controller(address)

    def test_runtime_connection_preferred_without_rewriting_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clash-verge.yaml"
            content = 'external-controller: 127.0.0.1:9097\nsecret: "abc#quoted"\n'
            path.write_text(content, encoding="utf-8")
            result = read_connection(directory)
            self.assertEqual(result["secret"], "abc#quoted")
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_disabled_runtime_does_not_fall_back_to_old_enabled_config(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "clash-verge.yaml").write_text('external-controller: ""\n', encoding="utf-8")
            (Path(directory) / "config.yaml").write_text('external-controller: 127.0.0.1:9097\n', encoding="utf-8")
            with self.assertRaises(PlatformError):
                read_connection(directory)

    def test_duplicate_yaml_controller_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.yaml").write_text('external-controller: 127.0.0.1:9090\nexternal-controller: 127.0.0.1:9097\n', encoding="utf-8")
            with self.assertRaises(PlatformError):
                read_connection(directory)

    def test_missing_directory_reports_setup_problem(self):
        with self.assertRaises(PlatformError):
            read_connection("/definitely-not-real-clash-config")

    def test_relative_xdg_data_home_is_ignored(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
            self.assertTrue(Path(default_data_directory()).is_absolute())


class BusDiscoveryTests(unittest.TestCase):
    def test_discovery_without_watcher_walks_only_owned_process_objects(self):
        adapter = object.__new__(GioSessionBus)
        queried = []
        def call(owner, path, interface, method, signature=None, arguments=(), timeout=1200):
            queried.append((owner, path, method))
            if owner in ("org.kde.StatusNotifierWatcher", "org.freedesktop.StatusNotifierWatcher"):
                raise OSError("no watcher")
            if method == "ListNames":
                return ((":1.17", ":1.18", "org.example"),)
            if method == "GetConnectionUnixProcessID":
                return (500 if arguments[0] == ":1.17" else 600,)
            if method == "GetConnectionUnixUser":
                return (1000,)
            if method == "Introspect":
                if path == "/":
                    return ('<node><node name="custom"/></node>',)
                return ('<node><interface name="org.kde.StatusNotifierItem"/></node>',)
            raise AssertionError(method)
        adapter.call = call
        self.assertEqual(adapter.discover_items(500, 1000), ((":1.17", "/custom"),))
        self.assertNotIn((":1.18", "/", "Introspect"), queried)

    def test_cancelled_discovery_has_no_dbus_calls(self):
        adapter = object.__new__(GioSessionBus)
        adapter.call = lambda *a, **k: self.fail("unexpected bus query")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            adapter.discover_items(500, 1000, cancel)


class BusConnectionTests(unittest.TestCase):
    def fake_modules(self, callback):
        cancellation = Mock()
        gio = SimpleNamespace(Cancellable=lambda: cancellation,
                              BusType=SimpleNamespace(SESSION="session"), bus_get_sync=callback)
        modules = {"gi": SimpleNamespace(require_version=Mock()),
                   "gi.repository": SimpleNamespace(Gio=gio, GLib=object())}
        return modules, cancellation

    def test_connection_uses_cancellable_and_cancels_timer_after_success(self):
        callback = Mock(return_value=object())
        modules, cancellation = self.fake_modules(callback)
        timer = Mock()
        with patch.dict("sys.modules", modules), patch("clash_watchdog.platform_linux.threading.Timer", return_value=timer) as timer_factory:
            adapter = GioSessionBus()
        timer_factory.assert_called_once_with(2.0, cancellation.cancel)
        self.assertIs(timer.daemon, True)
        timer.start.assert_called_once_with()
        timer.cancel.assert_called_once_with()
        callback.assert_called_once_with("session", cancellation)
        self.assertIs(adapter.connection, callback.return_value)

    def test_connection_failure_still_cancels_timer(self):
        callback = Mock(side_effect=OSError("session bus failed"))
        modules, cancellation = self.fake_modules(callback)
        timer = Mock()
        with patch.dict("sys.modules", modules), patch("clash_watchdog.platform_linux.threading.Timer", return_value=timer):
            with self.assertRaises(PlatformError):
                GioSessionBus()
        timer.cancel.assert_called_once_with()

    def test_connection_deadline_cancels_gio_operation(self):
        modules, cancellation = self.fake_modules(Mock(side_effect=OSError("cancelled")))
        timer = Mock()
        def make_timer(seconds, callback):
            timer.start.side_effect = callback
            return timer
        with patch.dict("sys.modules", modules), patch("clash_watchdog.platform_linux.threading.Timer", side_effect=make_timer):
            with self.assertRaises(PlatformError):
                GioSessionBus()
        cancellation.cancel.assert_called_once_with()
        timer.cancel.assert_called_once_with()


class ProcessTests(unittest.TestCase):
    class Entry:
        def __init__(self, pid="500", uid=1000, comm="clash-verge", started="9876", fail=False):
            self.name, self.uid, self.comm, self.started, self.fail = pid, uid, comm, started, fail

        def stat(self):
            return SimpleNamespace(st_uid=self.uid)

        def __truediv__(self, name):
            parent = self
            class File:
                def read_text(self, **kwargs):
                    if parent.fail:
                        raise PermissionError("fixture")
                    if name == "comm":
                        return parent.comm + "\n"
                    if name == "stat":
                        return parent.name + " (clash-verge) " + " ".join(["S"] + ["0"] * 18 + [parent.started])
                    raise AssertionError(name)
            return File()

    def inspect(self, entries, link="/usr/bin/clash-verge"):
        root = SimpleNamespace(iterdir=lambda: entries)
        with patch("clash_watchdog.platform_linux.os.readlink", return_value=link):
            return inspect_processes(root, uid=1000)

    def test_current_user_identity_includes_proc_start_time(self):
        result = self.inspect([self.Entry()])
        self.assertEqual(result.state, "running")
        self.assertEqual(result.processes[0].start_time, "9876")

    def test_two_gui_processes_are_ambiguous(self):
        self.assertEqual(self.inspect([self.Entry(), self.Entry("501")]).state, "ambiguous")

    def test_foreign_user_clash_prevents_duplicate_launch(self):
        self.assertEqual(self.inspect([self.Entry(uid=0)]).state, "unknown")

    def test_unrelated_processes_do_not_count_as_clash(self):
        self.assertEqual(self.inspect([self.Entry(comm="python")]).state, "missing")

    def test_unreadable_current_user_process_fails_closed(self):
        self.assertEqual(self.inspect([self.Entry(fail=True)]).state, "unknown")

    def test_deleted_binary_cannot_authorize_restart(self):
        self.assertEqual(self.inspect([self.Entry()], "/usr/bin/clash-verge (deleted)").state, "unknown")

    def test_spoofed_comm_with_other_executable_is_rejected(self):
        self.assertEqual(self.inspect([self.Entry()], "/usr/bin/something-else").state, "unknown")


if __name__ == "__main__":
    unittest.main()
