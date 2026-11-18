"""Policy scenarios use injected clocks and fake endpoints; no real networking."""
from dataclasses import FrozenInstanceError
import unittest

from clash_watchdog.engine import (
    BACKUP_TEST_URL, DEFAULT_TEST_URL, DiagnosticRoundResult, HealthSample,
    HealthStatistics, Options, ProbeResult, WatchdogEngine, backup_for,
    run_diagnostic_round,
)


class EngineTests(unittest.TestCase):
    def engine(self, **overrides):
        overrides.setdefault("grace_seconds", 0)
        engine = WatchdogEngine(Options(**overrides))
        engine.start(1000, "group/leaf")
        return engine

    def sample(self, engine, at, kind="failure", sample_id=None, **overrides):
        values = dict(sample_id=sample_id or f"round:{at}", target_key=engine.target_key,
                      generation=engine.generation, completed_at=at, kind=kind,
                      delay_ms=125 if kind in {"success", "partial"} else None)
        values.update(overrides)
        return HealthSample(**values)

    def failures(self, engine, last_at, count=5):
        for i in range(count):
            at = last_at - (count - 1 - i) * 5
            self.assertEqual("accepted", engine.observe(self.sample(engine, at), at))

    def test_requested_defaults_and_five_complete_rounds(self):
        options = Options()
        self.assertEqual((30, 5, 5, 30, 120, 5), (options.normal_seconds, options.retry_seconds,
                         options.failure_threshold, options.grace_seconds,
                         options.cooldown_seconds, options.hourly_limit))
        engine = self.engine()
        self.failures(engine, 1015, 4)
        self.assertEqual("below_threshold", engine.restart_decision(1015))
        engine.observe(self.sample(engine, 1020), 1020)
        self.assertEqual("ready", engine.restart_decision(1020))

    def test_normal_and_retry_schedule_start_after_round_completion(self):
        engine = self.engine()
        engine.mark_probe_started(1000)
        engine.observe(self.sample(engine, 1008, "success"), 1008)
        self.assertFalse(engine.need_probe(1037))
        self.assertTrue(engine.need_probe(1038))
        engine.mark_probe_started(1038)
        engine.observe(self.sample(engine, 1048), 1048)
        self.assertEqual(1053, engine.next_probe_at)

    def test_grace_blocks_probes_and_never_counts_a_grace_failure(self):
        engine = self.engine(grace_seconds=30)
        self.assertFalse(engine.need_probe(1029))
        self.assertEqual("during_grace", engine.observe(self.sample(engine, 1010), 1010))
        self.assertEqual(0, engine.consecutive_failures)
        # A completed reference supplied during grace does not create a failure
        # streak; the next normal round remains measured from its completion.
        self.assertFalse(engine.need_probe(1039))
        self.assertTrue(engine.need_probe(1040))

    def test_partial_success_clears_failure_but_stays_partial(self):
        engine = self.engine()
        self.failures(engine, 1015, 4)
        engine.observe(self.sample(engine, 1020, "partial"), 1020)
        self.assertEqual(0, engine.consecutive_failures)
        self.assertEqual("partial", engine.health_state)
        self.assertEqual(1050, engine.next_probe_at)
        self.assertEqual("below_threshold", engine.restart_decision(1020))

    def test_duplicate_round_cannot_accumulate_failures(self):
        engine = self.engine()
        original = self.sample(engine, 1000)
        self.assertEqual("accepted", engine.observe(original, 1000))
        for _ in range(20):
            self.assertEqual("duplicate", engine.observe(original, 1001))
        self.assertEqual(1, engine.consecutive_failures)

    def test_unknown_keeps_failure_count_and_grace_but_invalidates_requests(self):
        engine = self.engine()
        self.failures(engine, 1020)
        old_generation = engine.generation
        original_grace = engine.grace_until
        engine.mark_unknown(1021, "local API unreachable")
        self.assertEqual(5, engine.consecutive_failures)
        self.assertEqual("health_unknown", engine.restart_decision(1021))
        self.assertEqual(1026, engine.next_probe_at)
        self.assertEqual(original_grace, engine.grace_until)
        self.assertIsNone(engine.last_sample)
        stale_worker = self.sample(engine, 1022, "success", generation=old_generation)
        self.assertEqual("wrong_context", engine.observe(stale_worker, 1022))

    def test_repeated_unknown_does_not_reset_startup_grace(self):
        engine = self.engine(grace_seconds=30)
        for at in (1005, 1010, 1020, 1030):
            engine.mark_unknown(at)
        self.assertEqual(1030, engine.grace_until)
        self.assertEqual(1005, engine.unknown_since)
        self.assertEqual(1035, engine.next_probe_at)

    def test_short_unknown_can_resume_but_long_outage_starts_new_streak(self):
        engine = self.engine()
        self.failures(engine, 1015, 4)
        engine.mark_unknown(1020)
        engine.observe(self.sample(engine, 1025), 1025)
        self.assertEqual(5, engine.consecutive_failures)
        engine.mark_unknown(1030)
        engine.observe(self.sample(engine, 1100), 1100)
        self.assertEqual(1, engine.consecutive_failures)

    def test_old_or_future_failure_evidence_cannot_restart(self):
        engine = self.engine()
        self.failures(engine, 1020)
        self.assertEqual("stale_evidence", engine.restart_decision(1081))
        self.assertEqual("stale_evidence", engine.restart_decision(1019))
        self.assertEqual("future_timestamp", engine.observe(self.sample(engine, 1200), 1100))

    def test_slow_retry_and_timeout_extend_evidence_window(self):
        engine = self.engine(normal_seconds=60, retry_seconds=60, timeout_seconds=30)
        self.assertEqual(130, engine.evidence_max_age)
        self.failures(engine, 1020)
        self.assertEqual("ready", engine.restart_decision(1100))

    def test_switch_away_and_back_rejects_earlier_request(self):
        engine = self.engine()
        before = self.sample(engine, 1005, "success")
        engine.select_target("group/other", 1001)
        engine.select_target("group/leaf", 1002)
        self.assertEqual("wrong_context", engine.observe(before, 1005))
        self.assertEqual(0, engine.consecutive_failures)

    def test_selecting_same_chain_does_not_grant_repeated_grace(self):
        engine = self.engine()
        engine.observe(self.sample(engine, 1000), 1000)
        generation = engine.generation
        engine.select_target("group/leaf", 1001)
        self.assertEqual(generation, engine.generation)
        self.assertEqual(1, engine.consecutive_failures)

    def test_pre_context_or_out_of_order_result_is_stale(self):
        engine = self.engine()
        self.assertEqual("stale", engine.observe(self.sample(engine, 999), 1000))
        engine.observe(self.sample(engine, 1010), 1010)
        self.assertEqual("stale", engine.observe(self.sample(engine, 1005), 1010))

    def test_invalid_success_integer_values_are_not_health_success(self):
        for value in (None, 0, -1, True, 5.5, 12.0, "17", 2**31):
            with self.subTest(value=value):
                engine = self.engine()
                engine.observe(self.sample(engine, 1000, "success", delay_ms=value), 1000)
                self.assertEqual(1, engine.consecutive_failures)
                self.assertEqual("failed", engine.health_state)

    def test_setup_fault_requires_explicit_correction(self):
        engine = self.engine()
        engine.observe(self.sample(engine, 1000, "setup_fault", message="invalid secret"), 1000)
        self.assertIsNone(engine.next_probe_at)
        self.assertEqual("setup_fault", engine.restart_decision(1030))
        self.assertEqual("setup_fault", engine.observe(self.sample(engine, 1001, "success"), 1001))
        engine.clear_setup_fault(1002)
        self.assertEqual("health_unknown", engine.restart_decision(1002))
        self.assertEqual("accepted", engine.observe(self.sample(engine, 1003, "success"), 1003))

    def test_reservation_invalidates_old_context_before_dispatch(self):
        engine = self.engine()
        self.failures(engine, 1020)
        prior = self.sample(engine, 1021, "success")
        self.assertEqual("ready", engine.reserve_restart(1020))
        self.assertEqual([1020.0], engine.export_ledger(1020)["attempts"])
        self.assertEqual(0, engine.consecutive_failures)
        self.assertEqual("wrong_context", engine.observe(prior, 1021))
        self.assertEqual("below_threshold", engine.reserve_restart(1021))

    def test_unrecovered_attempts_back_off_and_only_fresh_success_resets(self):
        engine = self.engine(failure_threshold=1)
        for at, expected in ((1000, 120), (1120, 300), (1420, 600), (2020, 600)):
            engine.observe(self.sample(engine, at), at)
            self.assertEqual("ready", engine.reserve_restart(at))
            self.assertEqual(expected, engine.effective_cooldown)
        engine.observe(self.sample(engine, 2025, "partial"), 2025)
        self.assertEqual(0, engine.unrecovered_restart_count)
        self.assertEqual(120, engine.effective_cooldown)
        self.assertEqual(4, len(engine.export_ledger(2025)["attempts"]))

    def test_cooldown_requires_new_evidence_in_addition_to_elapsed_time(self):
        engine = self.engine(failure_threshold=1)
        engine.observe(self.sample(engine, 1000), 1000)
        engine.reserve_restart(1000)
        engine.observe(self.sample(engine, 1001), 1001)
        self.assertEqual("cooldown", engine.restart_decision(1119))
        self.assertEqual("stale_evidence", engine.restart_decision(1120))
        engine.observe(self.sample(engine, 1120), 1120)
        self.assertEqual("ready", engine.restart_decision(1120))

    def test_default_five_attempt_hourly_limit_expires_automatically(self):
        engine = self.engine(failure_threshold=1)
        for at in (1000, 1120, 1420, 2020, 2620):
            engine.observe(self.sample(engine, at), at)
            self.assertEqual("ready", engine.reserve_restart(at))
        engine.observe(self.sample(engine, 3220), 3220)
        self.assertEqual("hourly_limit", engine.restart_decision(3220))
        engine.observe(self.sample(engine, 4600), 4600)
        self.assertEqual("ready", engine.restart_decision(4600))
        self.assertFalse(engine.recovery_paused)
        self.assertEqual([1120.0, 1420.0, 2020.0, 2620.0], engine.export_ledger(4600)["attempts"])

    def test_stop_restart_and_manual_resume_cannot_erase_quota(self):
        engine = self.engine(hourly_limit=1, failure_threshold=1)
        engine.observe(self.sample(engine, 1000), 1000)
        engine.reserve_restart(1000)
        engine.stop()
        engine.start(1100, "other")
        self.assertFalse(engine.resume_recovery(1100))
        engine.observe(self.sample(engine, 1100), 1100)
        self.assertEqual("hourly_limit", engine.restart_decision(1100))
        self.assertEqual(1, engine.unrecovered_restart_count)

    def test_persisted_attempts_backoff_and_future_records_survive_reload(self):
        engine = WatchdogEngine(Options(grace_seconds=0, hourly_limit=2), [1200, 1500], 2)
        engine.start(1000, "target")
        self.assertEqual("hourly_limit", engine.restart_decision(1000))
        self.assertEqual(300, engine.effective_cooldown)
        self.assertEqual([1200.0, 1500.0], engine.export_ledger(1000)["attempts"])
        restored = WatchdogEngine(engine.options, **engine.export_ledger(1000))
        restored.start(1000, "target")
        self.assertEqual("hourly_limit", restored.restart_decision(1000))

    def test_long_cooldown_survives_hour_expiry_and_reload(self):
        options = Options(grace_seconds=0, cooldown_seconds=7200, failure_threshold=1)
        engine = WatchdogEngine(options, [1000], 1)
        engine.start(5000, "target")
        ledger = engine.export_ledger(5000)
        self.assertEqual([1000.0], ledger["attempts"])
        reloaded = WatchdogEngine(options, **ledger)
        reloaded.start(5000, "target")
        reloaded.observe(self.sample(reloaded, 5000), 5000)
        self.assertEqual("cooldown", reloaded.restart_decision(5000))

    def test_next_temporal_eligibility_includes_quota_and_backoff(self):
        engine = WatchdogEngine(Options(grace_seconds=0, hourly_limit=2), [1000, 2000], 2)
        engine.start(2100, "target")
        self.assertEqual(4600, engine.next_restart_allowed_at)
        engine.mark_unknown(2101)
        self.assertIsNone(engine.next_restart_allowed_at)

    def test_exported_ledger_is_independent_and_options_are_immutable(self):
        engine = WatchdogEngine(Options(grace_seconds=0), [1000])
        ledger = engine.export_ledger(1001)
        ledger["attempts"].clear()
        self.assertEqual([1000.0], engine.export_ledger(1001)["attempts"])
        with self.assertRaises(FrozenInstanceError):
            engine.options.hourly_limit = 999

    def test_invalid_config_and_nonfinite_timestamps_are_rejected(self):
        for options in (dict(retry_seconds=0), dict(grace_seconds=-1), dict(hourly_limit=True),
                        dict(normal_seconds=4, timeout_seconds=5), dict(timeout_seconds=1.0)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Options(**options)
        for at in (float("nan"), float("inf"), True, "1000"):
            with self.subTest(at=at), self.assertRaises(ValueError):
                WatchdogEngine(attempts=[at])
        with self.assertRaises(ValueError):
            self.engine().start(1000, " ")

    def test_stopped_engine_rejects_results_and_does_not_schedule(self):
        engine = self.engine()
        sample = self.sample(engine, 1001)
        engine.stop()
        self.assertEqual("not_running", engine.observe(sample, 1001))
        self.assertFalse(engine.need_probe(10000))
        with self.assertRaises(RuntimeError):
            engine.mark_probe_started(10000)


class RoundTests(unittest.TestCase):
    def round(self, responses, **kwargs):
        calls = []
        items = iter(responses)
        ticks = iter((1000, 1008))
        def probe(endpoint):
            calls.append(endpoint)
            item = next(items)
            if isinstance(item, Exception):
                raise item
            return item
        result = run_diagnostic_round(probe, clock=lambda: next(ticks), **kwargs)
        return result, calls

    def test_primary_success_uses_one_request_only(self):
        result, calls = self.round([ProbeResult(True, 123)])
        self.assertEqual([DEFAULT_TEST_URL], calls)
        self.assertTrue(result.success)
        self.assertFalse(result.backup_recovered)
        self.assertEqual("success", result.as_sample("group", 7).kind)

    def test_backup_success_is_partial_and_not_a_failed_round(self):
        result, calls = self.round([ProbeResult(False), ProbeResult(True, 124)])
        self.assertEqual([DEFAULT_TEST_URL, BACKUP_TEST_URL], calls)
        self.assertTrue(result.success)
        self.assertTrue(result.backup_recovered)
        self.assertEqual("partial", result.as_sample("group", 7).kind)

    def test_two_failed_addresses_produce_exactly_one_failure_sample(self):
        result, calls = self.round([ProbeResult(False), ProbeResult(False)])
        engine = WatchdogEngine(Options(grace_seconds=0))
        engine.start(1000, "group")
        sample = result.as_sample("group", engine.generation)
        self.assertEqual("accepted", engine.observe(sample, result.completed_at))
        self.assertEqual(1, engine.consecutive_failures)
        self.assertEqual(2, len(calls))

    def test_invalid_primary_delay_gets_independent_backup(self):
        result, calls = self.round([ProbeResult(True, 3.5), ProbeResult(True, 200)])
        self.assertEqual(2, len(calls))
        self.assertTrue(result.backup_recovered)

    def test_controller_exception_aborts_without_a_health_failure_result(self):
        seen = []
        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
            self.round([ProbeResult(False), RuntimeError("API unavailable")],
                       on_probe=lambda result, index: seen.append(index))
        self.assertEqual([0], seen)

    def test_cancellation_after_primary_is_logged_and_prevents_backup(self):
        calls = []
        events = []
        def probe(endpoint):
            calls.append(endpoint)
            return ProbeResult(False)
        def check():
            if events:
                raise InterruptedError("cancelled")
        with self.assertRaises(InterruptedError):
            run_diagnostic_round(probe, cancel_check=check,
                                 on_probe=lambda result, index: events.append(index))
        self.assertEqual([DEFAULT_TEST_URL], calls)
        self.assertEqual([0], events)

    def test_primary_cloudflare_uses_a_different_backup(self):
        self.assertEqual(DEFAULT_TEST_URL, backup_for(BACKUP_TEST_URL))
        self.assertEqual(DEFAULT_TEST_URL, backup_for("https://CP.CLOUDFLARE.COM/anything"))
        with self.assertRaises(ValueError):
            backup_for("http://example.com")

    def test_logging_failure_cannot_fabricate_a_completed_round(self):
        def broken_log(result, index):
            raise OSError("disk unavailable")
        with self.assertRaises(OSError):
            self.round([ProbeResult(True, 100)], on_probe=broken_log)

    def test_wall_clock_rollback_aborts_round_as_unknown(self):
        times = iter((1000, 999))
        with self.assertRaises(ValueError):
            run_diagnostic_round(lambda endpoint: ProbeResult(True, 100), clock=lambda: next(times))

    def test_statistics_expire_without_erasing_last_partial_time(self):
        stats = HealthStatistics()
        stats.record(DiagnosticRoundResult("a", 1000, 1002, True, True, 10, ()))
        stats.record(DiagnosticRoundResult("b", 1010, 1012, False, False, None, ()))
        self.assertEqual({"rounds": 2, "partial": 1, "failed": 1, "last_partial_at": 1002}, stats.summary(1100))
        self.assertEqual({"rounds": 0, "partial": 0, "failed": 0, "last_partial_at": 1002}, stats.summary(2000))


if __name__ == "__main__":
    unittest.main()
