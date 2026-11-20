"""Pure monitoring policy for the Ubuntu watchdog.

All timestamps are injected UTC Unix seconds (``time.time()``), including the
durable restart ledger. The caller serializes engine access and allows only one
diagnostic round in flight. A successful reservation MUST be saved durably before
the caller sends a restart command; a failed dispatch still spends its attempt.
No desktop observation, cached history entry or GUI latency is failure evidence.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit
import uuid


DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
BACKUP_TEST_URL = "https://cp.cloudflare.com/generate_204"


def _timestamp(value: float, label: str = "timestamp") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite UTC Unix seconds")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite UTC Unix seconds")
    return result


def _valid_delay(value: object) -> bool:
    # bool is an int subclass; neither bool nor 12.0 proves integer API latency.
    return type(value) is int and 0 < value <= 2**31 - 1


@dataclass(frozen=True)
class Options:
    normal_seconds: int = 30
    retry_seconds: int = 5
    timeout_seconds: int = 5
    failure_threshold: int = 5
    grace_seconds: int = 30
    cooldown_seconds: int = 120
    hourly_limit: int = 5
    poll_seconds: int = 30

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            minimum = 0 if name == "grace_seconds" else 1
            if type(value) is not int or not minimum <= value <= 2**31 - 1:
                raise ValueError(f"Invalid {name}")
        if self.normal_seconds < self.timeout_seconds:
            raise ValueError("Normal interval must be at least the probe timeout")


WatchdogOptions = Options


@dataclass(frozen=True)
class HealthSample:
    sample_id: str
    target_key: str
    generation: int
    completed_at: float
    kind: str
    delay_ms: int | None = None
    message: str = ""


class WatchdogEngine:
    """Deterministic state machine; call from one serialized monitor worker."""

    def __init__(self, options: Options | None = None,
                 attempts: Iterable[float] = (),
                 unrecovered_restart_count: int = 0) -> None:
        self.options = options or Options()
        if not isinstance(self.options, Options):
            raise TypeError("options must be Options")
        if type(unrecovered_restart_count) is not int or unrecovered_restart_count < 0:
            raise ValueError("Invalid unrecovered restart count")
        self.unrecovered_restart_count = unrecovered_restart_count
        self._attempts = sorted(_timestamp(at, "restart attempt") for at in attempts)
        self._last_attempt = self._attempts[-1] if self._attempts else None
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self.running = False
        self.generation = 0
        self.target_key = ""
        self.consecutive_failures = 0
        self.setup_fault: str | None = None
        self.recovery_paused = False  # An expiring quota hold, never a manual latch.
        self.health_unknown = False
        self.unknown_since: float | None = None
        self.unknown_reason: str | None = None
        self.grace_until = 0.0
        self.health_state = "waiting"
        self.last_sample: HealthSample | None = None
        self.last_completed_at: float | None = None
        self._last_probe_started: float | None = None
        self._last_unknown: float | None = None
        self._context_started = 0.0

    @property
    def evidence_max_age(self) -> int:
        return max(60, self.options.retry_seconds + 2 * self.options.timeout_seconds + 10)

    @property
    def effective_cooldown(self) -> int:
        backoff = (0 if self.unrecovered_restart_count <= 0 else
                   120 if self.unrecovered_restart_count == 1 else
                   300 if self.unrecovered_restart_count == 2 else 600)
        return max(self.options.cooldown_seconds, backoff)

    @property
    def next_probe_at(self) -> float | None:
        if not self.running or self.setup_fault:
            return None
        observations = [at for at in (self.last_completed_at, self._last_probe_started)
                        if at is not None]
        if self.health_unknown and self._last_unknown is not None:
            observations.append(self._last_unknown)
        interval = (self.options.retry_seconds if self.consecutive_failures or self.health_unknown
                    else self.options.normal_seconds)
        return max(self.grace_until, max(observations) + interval) if observations else self.grace_until

    @property
    def next_restart_allowed_at(self) -> float | None:
        """Temporal eligibility only; fresh failures and restart capability still required."""
        if not self.running or self.setup_fault or self.health_unknown:
            return None
        earliest = self.grace_until
        if self._last_attempt is not None:
            earliest = max(earliest, self._last_attempt + self.effective_cooldown)
        if len(self._attempts) >= self.options.hourly_limit:
            earliest = max(earliest, self._attempts[-self.options.hourly_limit] + 3600)
        return earliest

    def start(self, now: float, target_key: str) -> None:
        now = _timestamp(now)
        self._check_target(target_key)
        self.running = True
        self.target_key = target_key
        self._reset_context(now)
        self._prune_attempts(now)

    def stop(self) -> None:
        self.running = False
        self.generation += 1
        self.consecutive_failures = 0
        # Attempt records and unrecovered backoff deliberately survive stop/start.

    def select_target(self, target_key: str, now: float) -> None:
        now = _timestamp(now)
        self._check_target(target_key)
        if target_key != self.target_key:
            self.target_key = target_key
            self._reset_context(now)

    def mark_unknown(self, now: float, reason: str = "等待新的完整诊断") -> None:
        now = _timestamp(now)
        if not self.running:
            return
        self.generation += 1
        if not self.health_unknown:
            self.unknown_since = now
        self.health_unknown = True
        self.unknown_reason = reason or "等待新的完整诊断"
        self._last_unknown = max(now, self._last_unknown) if self._last_unknown is not None else now
        self.last_sample = None
        self.health_state = "unknown"

    def clear_setup_fault(self, now: float) -> None:
        self.setup_fault = None
        self.mark_unknown(now, "配置已修正，等待新的完整诊断")

    def observe(self, sample: HealthSample, now: float) -> str:
        now = _timestamp(now)
        if not self.running:
            return "not_running"
        if (not isinstance(sample, HealthSample) or not isinstance(sample.sample_id, str)
                or not sample.sample_id.strip() or type(sample.generation) is not int
                or not isinstance(sample.kind, str) or sample.kind not in
                {"success", "partial", "failure", "unknown", "setup_fault"}):
            return "invalid_sample"
        try:
            completed = _timestamp(sample.completed_at)
        except ValueError:
            return "invalid_sample"
        if sample.generation != self.generation or sample.target_key != self.target_key:
            return "wrong_context"
        if completed > now:
            return "future_timestamp"
        if (completed < self._context_started or
                (self._last_unknown is not None and completed < self._last_unknown) or
                (self.last_completed_at is not None and completed < self.last_completed_at)):
            return "stale"
        if sample.sample_id in self._seen:
            return "duplicate"
        accepted = sample
        if sample.kind in {"success", "partial"} and not _valid_delay(sample.delay_ms):
            accepted = replace(sample, kind="failure", message="成功结果没有有效的整数延迟")
        if accepted.kind == "setup_fault":
            self.setup_fault = accepted.message or "监控配置错误"
            self.mark_unknown(now, self.setup_fault)
            self.last_sample = accepted
            return "setup_fault"
        if accepted.kind == "unknown":
            self.mark_unknown(now, accepted.message)
            self.last_sample = accepted
            return "unknown"
        if self.setup_fault:
            return "setup_fault"
        if (self.last_completed_at is not None and
                completed - self.last_completed_at > self.evidence_max_age):
            self.consecutive_failures = 0
        self.health_unknown = False
        self.unknown_since = None
        self.unknown_reason = None
        self._last_unknown = None
        self.last_sample = accepted
        self.last_completed_at = completed
        self.health_state = {"success": "healthy", "partial": "partial", "failure": "failed"}[accepted.kind]
        self._remember(accepted.sample_id)
        success = accepted.kind in {"success", "partial"}
        if success:
            self.unrecovered_restart_count = 0
        if now < self.grace_until or completed < self.grace_until:
            self.consecutive_failures = 0
            return "during_grace"
        self.consecutive_failures = 0 if success else self.consecutive_failures + 1
        return "accepted"

    def need_probe(self, now: float) -> bool:
        now = _timestamp(now)
        self._prune_attempts(now)
        due = self.next_probe_at
        return due is not None and now >= due

    def mark_probe_started(self, now: float) -> None:
        now = _timestamp(now)
        if not self.running:
            raise RuntimeError("Monitoring is stopped")
        self._last_probe_started = now

    def restart_decision(self, now: float) -> str:
        now = _timestamp(now)
        self._prune_attempts(now)
        if not self.running:
            return "not_running"
        if self.setup_fault:
            return "setup_fault"
        if self.recovery_paused:
            return "hourly_limit"
        if self.health_unknown:
            return "health_unknown"
        if now < self.grace_until:
            return "during_grace"
        if self.consecutive_failures < self.options.failure_threshold:
            return "below_threshold"
        if self._last_attempt is not None and now < self._last_attempt + self.effective_cooldown:
            return "cooldown"
        if (self.last_completed_at is None or self.last_completed_at > now or
                now - self.last_completed_at > self.evidence_max_age):
            return "stale_evidence"
        return "ready"

    def reserve_restart(self, now: float) -> str:
        """Reserve once; caller MUST persist export_ledger before any external action."""
        now = _timestamp(now)
        decision = self.restart_decision(now)
        if decision != "ready":
            return decision
        self._attempts.append(now)
        self._attempts.sort()
        self._last_attempt = now
        self.unrecovered_restart_count += 1
        self._reset_context(now)
        self.recovery_paused = len(self._attempts) >= self.options.hourly_limit
        return "ready"

    def record_recovery_restart(self, now: float) -> str:
        """Reserve a restart inside an already authorized recovery cycle."""
        now = _timestamp(now)
        self._prune_attempts(now)
        if self.recovery_paused:
            return "hourly_limit"
        if self._last_attempt is not None and now < self._last_attempt + self.effective_cooldown:
            return "cooldown"
        self._attempts.append(now)
        self._last_attempt = now
        self.unrecovered_restart_count += 1
        self.mark_unknown(now, "内核恢复期间等待诊断")
        return "ready"

    def reset_diagnostics(self, now: float) -> None:
        self._reset_context(_timestamp(now))
        self.grace_until = now

    def finish_recovery(self) -> None:
        self.consecutive_failures = 0

    def resume_recovery(self, now: float) -> bool:
        """Quota expiry is automatic. This never bypasses cooldown or the hourly cap."""
        self._prune_attempts(_timestamp(now))
        return not self.recovery_paused

    def export_ledger(self, now: float) -> dict:
        now = _timestamp(now)
        self._prune_attempts(now)
        attempts = list(self._attempts)
        # Retain a longer-than-one-hour custom cooldown across process restarts.
        if (self._last_attempt is not None and self._last_attempt not in attempts and
                now < self._last_attempt + self.effective_cooldown):
            attempts.append(self._last_attempt)
        return {"attempts": sorted(attempts),
                "unrecovered_restart_count": self.unrecovered_restart_count}

    def _reset_context(self, now: float) -> None:
        self.generation += 1
        self._context_started = now
        self.grace_until = now + self.options.grace_seconds
        self.consecutive_failures = 0
        self.setup_fault = None
        self.last_completed_at = None
        self._last_probe_started = None
        self.last_sample = None
        self._last_unknown = None
        self.health_unknown = False
        self.unknown_since = None
        self.unknown_reason = None
        self.health_state = "waiting"
        self._seen.clear()
        self._seen_order.clear()

    def _prune_attempts(self, now: float) -> None:
        # Future entries after a clock rollback continue to spend quota.
        self._attempts = [at for at in self._attempts if at > now - 3600]
        self.recovery_paused = len(self._attempts) >= self.options.hourly_limit

    def _remember(self, sample_id: str) -> None:
        self._seen.add(sample_id)
        self._seen_order.append(sample_id)
        while len(self._seen_order) > 512:
            self._seen.remove(self._seen_order.popleft())

    @staticmethod
    def _check_target(target_key: str) -> None:
        if not isinstance(target_key, str) or not target_key.strip():
            raise ValueError("A nonempty target key is required")


@dataclass(frozen=True)
class ProbeResult:
    success: bool
    delay_ms: int | None = None
    message: str = ""


@dataclass(frozen=True)
class DiagnosticRoundResult:
    round_id: str
    started_at: float
    completed_at: float
    success: bool
    backup_recovered: bool
    delay_ms: int | None
    probes: tuple[ProbeResult, ...]

    def as_sample(self, target_key: str, generation: int) -> HealthSample:
        kind = "partial" if self.backup_recovered else "success" if self.success else "failure"
        return HealthSample(self.round_id, target_key, generation, self.completed_at, kind, self.delay_ms)


def backup_for(primary: str) -> str:
    parsed = urlsplit(primary)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Diagnostic endpoint must be an HTTPS URL")
    return DEFAULT_TEST_URL if parsed.hostname.lower() == urlsplit(BACKUP_TEST_URL).hostname else BACKUP_TEST_URL


def run_diagnostic_round(probe: Callable[[str], ProbeResult], primary: str = DEFAULT_TEST_URL, *,
                         clock: Callable[[], float] = time.time,
                         cancel_check: Callable[[], None] | None = None,
                         round_id: str | None = None,
                         on_probe: Callable[[ProbeResult, int], None] | None = None) -> DiagnosticRoundResult:
    """Run primary then optional alternate. API/configuration exceptions propagate.

    A failed HTTP test is a ProbeResult(False); inability to ask the local API is
    an exception/unknown. Only a completed round with BOTH results failing is one
    failure. on_probe runs before cancellation checks so completed work is logged.
    """
    alternate = backup_for(primary)
    identity = "round:" + uuid.uuid4().hex if round_id is None else round_id
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("Round ID is required")
    check = cancel_check or (lambda: None)
    started = _timestamp(clock())
    results: list[ProbeResult] = []
    success = False
    delay = None
    for endpoint in (primary, alternate):
        check()
        item = probe(endpoint)
        if not isinstance(item, ProbeResult):
            raise TypeError("Probe must return ProbeResult or raise a controller error")
        results.append(item)
        if on_probe is not None:
            on_probe(item, len(results) - 1)
        check()
        if item.success and _valid_delay(item.delay_ms):
            success = True
            delay = item.delay_ms
            break
    completed = _timestamp(clock())
    if completed < started:
        raise ValueError("Clock moved backward during the diagnostic round")
    return DiagnosticRoundResult(identity, started, completed, success,
                                 success and len(results) == 2, delay, tuple(results))


class HealthStatistics:
    """Presentation-only rolling counters; never input to restart decisions."""

    def __init__(self) -> None:
        self._entries: deque[DiagnosticRoundResult] = deque()
        self.last_partial_at: float | None = None

    def record(self, result: DiagnosticRoundResult) -> None:
        self._entries.append(result)
        if result.backup_recovered:
            self.last_partial_at = result.completed_at
        self._prune(result.completed_at)

    def summary(self, now: float) -> dict:
        self._prune(_timestamp(now))
        return {"rounds": len(self._entries),
                "partial": sum(item.backup_recovered for item in self._entries),
                "failed": sum(not item.success for item in self._entries),
                "last_partial_at": self.last_partial_at}

    def _prune(self, now: float) -> None:
        while self._entries and self._entries[0].completed_at < now - 900:
            self._entries.popleft()
