"""Durability and privacy checks; POSIX permissions require an actual POSIX host."""
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from clash_watchdog.store import Store, default_state


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "private-watchdog"
        self.store = Store(self.directory)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_first_run_defaults_are_30_5_5_and_launch_enabled(self):
        state = self.store.load()
        self.assertIs(state["automatic_launch"], True)
        self.assertEqual((state["options"]["normal_seconds"],
                          state["options"]["retry_seconds"],
                          state["options"]["failure_threshold"]), (30, 5, 5))
        self.assertEqual(state["restart_attempts"], [])
        self.assertEqual(state["launch_attempts"], [])

    def test_default_and_loaded_state_are_independent_copies(self):
        one = default_state()
        one["options"]["normal_seconds"] = 77
        one["launch_attempts"].append(1)
        self.assertEqual(default_state()["options"]["normal_seconds"], 30)
        self.assertEqual(self.store.load()["launch_attempts"], [])

    def test_launch_option_and_limits_survive_save_reload(self):
        state = default_state()
        state["automatic_launch"] = True
        state["restart_attempts"] = [12.5, 99]
        state["launch_attempts"] = [20]
        state["unrecovered_restart_count"] = 2
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_future_timestamps_are_preserved_for_clock_rollback_safety(self):
        state = default_state()
        state["restart_attempts"] = [1, 10**12]
        state["launch_attempts"] = [10**12 + 1]
        self.store.save(state)
        loaded = self.store.load()
        self.assertEqual(loaded["restart_attempts"], [1, 10**12])
        self.assertEqual(loaded["launch_attempts"], [10**12 + 1])

    def test_invalid_ledger_is_refused_without_overwriting_evidence(self):
        for key in ("restart_attempts", "launch_attempts"):
            for value in (None, "12", {}, [True], [-1], ["12"], [float("nan")], [float("inf")]):
                with self.subTest(key=key, value=value):
                    state = default_state()
                    state[key] = value
                    original = json.dumps(state)
                    self.store.path.write_text(original, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.store.load()
                    self.assertEqual(self.store.path.read_text(encoding="utf-8"), original)

    def test_missing_ledger_is_refused_without_reset(self):
        for key in ("restart_attempts", "launch_attempts"):
            with self.subTest(key=key):
                state = default_state()
                del state[key]
                self.store.path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.load()

    def test_corrupt_json_and_unknown_schema_are_refused(self):
        for original in ('{"restart_attempts":', '{"schema": 999}', '[]'):
            with self.subTest(original=original):
                self.store.path.write_text(original, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.load()
                self.assertEqual(self.store.path.read_text(encoding="utf-8"), original)

    def test_invalid_switch_or_backoff_count_is_refused(self):
        for key, value in (("automatic_launch", "false"), ("automatic_restart", 1),
                           ("recovery_paused", None), ("unrecovered_restart_count", -1),
                           ("unrecovered_restart_count", True)):
            with self.subTest(key=key, value=value):
                state = default_state()
                state[key] = value
                self.store.path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.load()

    def _assert_failed_save_preserves_old(self, failure_target):
        old = default_state()
        old["secret"] = "old-secret"
        old["restart_attempts"] = [100]
        self.store.save(old)
        original = self.store.path.read_bytes()
        newer = copy.deepcopy(old)
        newer["secret"] = "new-secret"
        newer["restart_attempts"].append(200)
        with patch(failure_target, side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.store.save(newer)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.secret, "old-secret")
        self.assertEqual(list(self.directory.glob("settings-*.tmp")), [])

    def test_failed_atomic_replace_preserves_old_file_and_cleans_temp(self):
        self._assert_failed_save_preserves_old("clash_watchdog.store.os.replace")

    def test_failed_file_fsync_preserves_old_file_and_cleans_temp(self):
        self._assert_failed_save_preserves_old("clash_watchdog.store.os.fsync")

    def test_known_secret_never_reaches_returned_or_file_log(self):
        state = default_state()
        state["secret"] = "密钥-abc def #123"
        self.store.save(state)
        returned = self.store.log("failed request value=" + state["secret"])
        self.assertNotIn(state["secret"], returned)
        self.assertIn("[密钥已隐藏]", returned)
        for handler in self.store.logger.handlers:
            handler.flush()
        self.assertNotIn(state["secret"], self.store.log_path.read_text(encoding="utf-8"))

    def test_bearer_token_and_url_query_are_redacted(self):
        text = self.store.log("Authorization: Bearer bearer-value; token=other-token "
                              "https://example.test/path?credential=query-value")
        for sensitive in ("bearer-value", "other-token", "query-value"):
            self.assertNotIn(sensitive, text)
        self.assertIn("https://example.test/path?[省略查询参数]", text)

    def test_log_control_characters_do_not_forge_lines(self):
        text = self.store.log("a\r\nfake_success\x00")
        self.assertNotIn("\n", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\x00", text)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode checks require a real POSIX host")
    def test_private_directory_and_created_files_have_private_modes(self):
        self.store.save(default_state())
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.log_path.stat().st_mode), 0o600)
        self.assertEqual(self.directory.stat().st_uid, os.getuid())
        self.assertEqual(self.store.path.stat().st_uid, os.getuid())

    @unittest.skipUnless(os.name == "posix", "POSIX chmod checks require a real POSIX host")
    def test_existing_directory_is_hardened_to_700(self):
        self.directory.chmod(0o755)
        other = Store(self.directory)
        try:
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        finally:
            other.close()

    @unittest.skipUnless(os.name == "posix", "POSIX symlink checks require a real POSIX host")
    def test_symlink_directory_and_settings_are_rejected(self):
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            Store(alias)
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text(json.dumps(default_state()), encoding="utf-8")
        self.store.path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.store.load()
        with self.assertRaises(ValueError):
            self.store.save(default_state())


if __name__ == "__main__":
    unittest.main()
