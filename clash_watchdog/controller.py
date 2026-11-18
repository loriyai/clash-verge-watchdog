"""Background orchestration. emit must enqueue data; workers never call Tk."""
from __future__ import annotations

import copy
import threading
import time
import uuid

from .api import ClashAPI, ApiError, Cancelled
from .store import default_state
from .engine import Options, WatchdogEngine, HealthSample
from .platform_linux import LinuxPlatform, Cancelled as PlatformCancelled


class _Cancellation:
    """The API/platform require only Event.is_set, not ownership of the event."""
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)


class Monitor:
    def __init__(self, store, state, emit, platform=None, api_factory=ClashAPI, clock=time.time):
        self.store, self.state, self.emit = store, copy.deepcopy(state), emit
        self.platform = platform or LinuxPlatform()
        self.api_factory, self.clock = api_factory, clock
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.request_cancel = threading.Event()
        self.app_cancel = threading.Event()
        self.app_generation = 0
        self.settings_generation = 0
        self.running = False
        self.engine = None
        self.api = None
        self.rest_tun_check = 0.0
        self.rest_deadline = 0.0
        self.session = uuid.uuid4().hex
        self.monitor_id = "none"
        self.last_readiness = None
        self.app_busy = self.core_busy = self.app_suppress_recovery = False
        self.missing_since = self.last_app_observation = None
        self.last_app_state = ""
        self.rounds = []
        self.thread = self.app_thread = None
        self.next_poll = self.next_aux = self.next_heartbeat = self.next_restart_check = 0.0

    def log(self, message):
        text = self.store.log(f"session={self.session} monitor={self.monitor_id} {message}")
        self.emit("log", text)

    def configure(self, state):
        candidate = copy.deepcopy(state)
        Options(**candidate["options"])  # validation is in the frozen dataclass constructor
        ClashAPI(candidate["controller"], candidate["secret"])
        ClashAPI.validate_test_url(candidate["test_url"])
        if not 1 <= candidate["options"]["timeout_seconds"] <= 30:
            raise ValueError("测速超时必须为 1 至 30 秒")
        with self.lock:
            if self.running:
                raise RuntimeError("请先停止监控再修改连接或参数")
            for key in ("restart_attempts", "launch_attempts", "unrecovered_restart_count", "recovery_paused", "software_attempts", "recovery_attempts", "tun_restore_pending", "rest_until", "rest_after_tun", "recovery_stage"):
                candidate[key] = copy.deepcopy(self.state.get(key, default_state().get(key)))
            self.store.save(candidate)
            self.state = candidate
            self.settings_generation += 1

    def connect(self):
        with self.lock:
            state = copy.deepcopy(self.state)
            settings_generation = self.settings_generation
        api = self.api_factory(state["controller"], state["secret"])
        version = api.get_version()
        snapshot = api.get_proxies()
        groups = [name for name, node in snapshot.nodes.items() if node.is_group]
        readiness = self.platform.probe_restart()
        with self.lock:
            if settings_generation != self.settings_generation:
                raise Cancelled("连接设置已变化，请重新连接")
            self.emit("connected", {"version": version, "groups": groups})
            self._readiness(readiness, force=True)
            self.log(f"connected version={version}; groups={len(groups)}")

    def _readiness(self, result, force=False):
        previous = self.last_readiness
        self.last_readiness = result
        self.emit("readiness", result.message)
        if force or previous != result:
            self.log(f"restart_readiness available={result.available}; {result.message}")

    def check_restart(self):
        result = self.platform.probe_restart()
        with self.lock:
            self._readiness(result, force=True)
        return result

    def start(self):
        with self.lock:
            if self.running:
                return
            if any(t and t.is_alive() for t in (self.thread, self.app_thread)):
                raise RuntimeError("上一轮请求正在取消，请稍候再开始")
            options = Options(**self.state["options"])
            self.engine = WatchdogEngine(options, self.state["restart_attempts"],
                                         self.state["unrecovered_restart_count"])
            self.api = self.api_factory(self.state["controller"], self.state["secret"])
            self.cancel = threading.Event()
            self.request_cancel = threading.Event()
            self.app_cancel = threading.Event()
            self.app_generation += 1
            self.monitor_id = uuid.uuid4().hex
            self.running = True
            self.rounds = []
            self.app_busy = self.core_busy = self.app_suppress_recovery = False
            self.missing_since = self.last_app_observation = None
            self.last_app_state = ""
            self.next_poll = self.next_aux = self.next_heartbeat = self.next_restart_check = 0.0
            self.log("monitor_start normal_s=%s retry_s=%s threshold=%s" % (
                options.normal_seconds, options.retry_seconds, options.failure_threshold))
            self.thread = threading.Thread(target=self._loop, name="watchdog-network", daemon=True)
            self.app_thread = threading.Thread(target=self._app_loop, name="watchdog-app", daemon=True)
            self.thread.start()
            self.app_thread.start()

    def stop(self):
        # Invalidate dispatch immediately, even while another worker saves a ledger.
        self.cancel.set()
        self.request_cancel.set()
        self.app_cancel.set()
        with self.lock:
            was_running = self.running
            self.running = False
            self.app_generation += 1
            if self.engine:
                self.engine.stop()
            self.missing_since = self.last_app_observation = None
            self.app_suppress_recovery = False
            if was_running:
                self.log("monitor_stop; 已停止后续自动恢复，保留尝试记录")
            self.emit("stopped", None)

    def set_automatic_launch(self, enabled):
        self.app_cancel.set()  # A blocked inspection/launch can see disable before acquiring our lock.
        with self.lock:
            prior = self.state["automatic_launch"]
            self.app_cancel = threading.Event()
            self.app_generation += 1
            self.state["automatic_launch"] = bool(enabled)
            self.missing_since = self.last_app_observation = None
            self.app_suppress_recovery = False
            try:
                self.store.save(self.state)
            except Exception:
                self.state["automatic_launch"] = False
                self.app_cancel.set()
                self._app_status("disabled", "保存开关失败，自动启动在本次运行中关闭")
                raise
            self.log(f"app_launch_option enabled={enabled} previous={prior}")
            self._app_status("enabled" if enabled else "disabled", "已开启，监控时生效" if enabled else "已关闭")

    def resume_recovery(self):
        with self.lock:
            if self.core_busy:
                raise RuntimeError("重启请求尚未结束，请等待结果")
            prior = self.state["recovery_paused"]
            self.state["recovery_paused"] = False
            try:
                self.store.save(self.state)
            except Exception:
                self.state["recovery_paused"] = prior
                raise
            self.log("recovery_resume; 次数与冷却限制继续保留")

    def _persist_engine(self):
        ledger = self.engine.export_ledger(self.clock())
        self.state["restart_attempts"] = ledger["attempts"]
        self.state["unrecovered_restart_count"] = ledger["unrecovered_restart_count"]
        self.store.save(self.state)

    def _snapshot(self, message=""):
        with self.lock:
            if not self.engine or not self.running:
                return
            now = self.clock()
            self.rounds = [r for r in self.rounds if now-r[0] <= 900]
            self.emit("health", {
                "state": self.engine.health_state,
                "delay_ms": (self.engine.last_sample.delay_ms if self.engine.last_sample and
                             self.engine.health_state in ("healthy", "partial") else None),
                "failures": self.engine.consecutive_failures,
                "threshold": self.engine.options.failure_threshold,
                "next_probe": self.engine.next_probe_at,
                "grace": self.engine.grace_until,
                "decision": self.engine.restart_decision(now),
                "paused": self.state["recovery_paused"],
                "rounds": len(self.rounds),
                "partial": sum(r[1] == "partial" for r in self.rounds),
                "failed": sum(r[1] == "failure" for r in self.rounds),
                "message": message,
            })

    def _on_probe(self, probe, index):
        self.log(f"probe request={probe.request_id} role={'primary' if index == 0 else 'backup'} "
                 f"success={probe.success} delay_ms={probe.delay_ms} request_ms={probe.request_ms} "
                 f"http={probe.http_status} kind={probe.error_kind}; {probe.error}")

    def _loop(self):
        previous_idle = self.clock()
        try:
            while not self.cancel.is_set():
                now = self.clock()
                # Compare to AFTER the previous work, not before synchronous I/O.
                # A normal 16-second dual-address round is not a scheduler gap.
                if now-previous_idle > 15 or now < previous_idle:
                    with self.lock:
                        if self.engine.target_key:
                            self.engine.mark_unknown(now, "休眠或调度中断后等待新诊断")
                        self.missing_since = self.last_app_observation = None
                        self.last_readiness = None
                        self.next_aux = 0
                    self.log("scheduler_gap; 原故障证据不用于立即重启")
                self._network_tick(now)
                previous_idle = self.clock()
                self.cancel.wait(0.3)
        except (Cancelled, PlatformCancelled):
            pass
        except Exception as exc:
            self.log(f"monitor_error {type(exc).__name__}; {exc}")
            self.emit("notice", f"监控已停止：{exc}")
            self.stop()

    def _network_tick(self, now):
        if self.state.get("tun_restore_pending"):
            self.emit("recovery", "等待 TUN 恢复")
            try:
                self.platform.set_tun(True, self.cancel)
                if not self.api.tun_enabled(self.cancel):
                    return
                self.state["tun_restore_pending"] = False
                if self.state.pop("rest_after_tun", False):
                    self.state["rest_until"] = self.clock()+60
                    self.rest_deadline = time.monotonic()+60
                self.store.save(self.state)
            except Exception:
                self.cancel.wait(2)
                return
        if self.state.get("rest_until", 0):
            if not self.rest_deadline:
                self.rest_deadline = time.monotonic()+max(0, min(60, self.state["rest_until"]-now))
            remaining = self.rest_deadline-time.monotonic()
            if remaining > 0:
                if time.monotonic() >= self.rest_tun_check:
                    self.rest_tun_check = time.monotonic()+5
                    try:
                        tun_on = self.api.tun_enabled(self.cancel)
                    except ApiError:
                        tun_on = False
                    if not tun_on:
                        self.state.update(tun_restore_pending=True, rest_after_tun=True, rest_until=0)
                        self.rest_deadline = 0
                        self.store.save(self.state)
                        return
                self.emit("recovery", f"监控休息 {int(remaining)+1} 秒")
                return
            self.state["rest_until"] = 0
            self.store.save(self.state)
            self.engine.consecutive_failures = 0
            self.engine.grace_until = now
            self.engine._last_probe_started = None
            self.engine.last_completed_at = None
            self.engine._last_unknown = None
            self.next_poll = 0
            self.rest_deadline = 0
            self.emit("recovery", "")
        with self.lock:
            self._ensure_active()
            token = _Cancellation(self.cancel, self.request_cancel)
            if now >= self.next_heartbeat:
                self.log("heartbeat running=true")
                self.next_heartbeat = now+60
            due = now >= self.next_poll or self.engine.need_probe(now)
        if not due:
            self._snapshot()
            return
        try:
            self._poll_once(token)
        except (Cancelled, PlatformCancelled):
            if self.cancel.is_set() or not self.running:
                raise Cancelled("操作已取消")
            # App launch invalidates the old request, not the monitor itself.
        except ApiError as exc:
            with self.lock:
                self._ensure_active()
                # An app launch may invalidate this request at the same moment
                # an HTTP error returns. It is not a reason to end the network
                # worker or overwrite the new launch's diagnostic generation.
                if token.is_set():
                    return
                if self.engine.target_key:
                    self.engine.mark_unknown(self.clock(), str(exc))
                self.next_poll = self.clock()+self.state["options"]["retry_seconds"]
            self.log(f"api_error kind={exc.kind} request={exc.request_id} http={exc.status}; {exc}")
            self.emit("notice", str(exc))
            self._snapshot(str(exc))
            if exc.kind in ("setup", "selection"):
                self.stop()

    def _poll_once(self, token):
        snapshot = self.api.get_proxies(token)
        selected = snapshot.resolve(self.state["group"])
        self._ensure_active(token)
        with self.lock:
            self._follow(selected)
            due_probe = self.engine.need_probe(self.clock())
            generation = self.engine.generation
            if due_probe:
                self.engine.mark_probe_started(self.clock())
        self.emit("selection", selected)
        self._history(snapshot, selected)
        self._ensure_active(token)
        if due_probe:
            started = self.clock()
            rid = uuid.uuid4().hex
            self.log(f"round_start id={rid} generation={generation}")
            terminal = "unknown"
            try:
                result = self.api.diagnose(selected.leaf, self.state["test_url"],
                    self.state["options"]["timeout_seconds"]*1000, token, self._on_probe)
                if result.completed_at < started:
                    raise ApiError("controller", "系统时间发生变化，等待新的完整诊断")
                fresh = self.api.get_proxies(token).resolve(self.state["group"])
                self._ensure_active(token)
                with self.lock:
                    if fresh.target_key != selected.target_key:
                        self._follow(fresh)
                        self.emit("selection", fresh)
                        terminal = "discarded_selection_changed"
                    else:
                        kind = "partial" if result.partial else "success" if result.success else "failure"
                        disposition = self.engine.observe(HealthSample(rid, selected.target_key,
                            generation, result.completed_at, kind, result.delay_ms), self.clock())
                        terminal = disposition
                        if disposition in ("accepted", "during_grace"):
                            terminal = kind
                            self.rounds.append((result.completed_at, kind))
                            self.emit("probe", {"delay": result.delay_ms, "kind": kind,
                                                "time": result.completed_at})
                            self._persist_engine()
            except (Cancelled, PlatformCancelled):
                terminal = "cancelled"
                raise
            finally:
                self.log(f"round_end id={rid} outcome={terminal} elapsed_ms={int((self.clock()-started)*1000)}")
        self._ensure_active(token)
        self._try_restart(selected, token)
        now = self.clock()
        with self.lock:
            self._ensure_active(token)
            self.next_poll = now+self.engine.options.poll_seconds
            next_probe = self.engine.next_probe_at
            auxiliary = (not self.engine.consecutive_failures and not self.engine.health_unknown and
                         now >= self.next_aux and next_probe is not None and next_probe-now >= 10)
            if auxiliary:
                self.next_aux = now+60
        if auxiliary:
            self._readiness(self.platform.probe_restart(token))
            try:
                route = self.api.route_snapshot(selected.chain, token, timeout=2)
                self._ensure_active(token)
                self.emit("routes", route)
                self.log(f"route_snapshot {route.summary}")
            except ApiError as exc:
                self.log(f"route_snapshot unavailable; {exc}")
        self._snapshot()

    def _follow(self, selected):
        if not self.engine.target_key:
            self.engine.start(self.clock(), selected.target_key)
        elif selected.target_key != self.engine.target_key:
            self.engine.select_target(selected.target_key, self.clock())
            self.log("selection_changed; 连续失败清零，重新进入宽限期")

    def _history(self, snapshot, selected):
        node = snapshot.nodes.get(selected.leaf)
        latest = node.latest_history if node else None
        text = "尚无记录"
        if latest:
            latency = f"{latest.delay_ms} ms" if latest.delay_ms > 0 else "Timeout / 未获得有效延迟"
            text = f"{latency} · {latest.timestamp}（内核历史参考）"
        self.emit("history", text)

    def _core_allowed(self):
        return (self.running and not self.cancel.is_set() and not self.app_busy and
                not self.core_busy and not self.app_suppress_recovery and
                self.state["automatic_restart"] and not self.state["recovery_paused"] and
                self.clock() >= self.next_restart_check and
                self.engine.restart_decision(self.clock()) == "ready")

    def _try_restart(self, selected, token=None):
        token = token or _Cancellation(self.cancel, self.request_cancel)
        with self.lock:
            if not self._core_allowed() or token.is_set():
                return
        readiness = self.platform.probe_restart(token)
        if not readiness.available:
            self.next_restart_check = self.clock()+30
            return
        attempts = [t for t in self.state.get("recovery_attempts", []) if t > self.clock()-3600]
        if len(attempts) >= 5 or (attempts and self.clock()-attempts[-1] < 120):
            return
        self.state["recovery_attempts"] = attempts+[self.clock()]
        self.store.save(self.state)
        from .recovery import Recovery
        with self.lock:
            self.core_busy = True
        try:
            Recovery(self, token, selected).run()
        except (ApiError, PlatformCancelled) as exc:
            self.engine.mark_unknown(self.clock(), str(exc))
            self.log(f"recovery_unavailable {exc}")
        except Cancelled:
            if self.cancel.is_set():
                raise
        except Exception as exc:
            self.engine.mark_unknown(self.clock(), str(exc))
            self.log(f"recovery_unavailable {exc}")
        finally:
            with self.lock:
                self.core_busy = False
            self.emit("recovery", "")

    def _ensure_active(self, token=None):
        if self.cancel.is_set() or not self.running or (token is not None and token.is_set()):
            raise Cancelled("操作已取消")

    def _app_status(self, state, message):
        self.emit("app_status", message)
        if self.last_app_state != state:
            self.last_app_state = state
            self.log(f"app_supervision state={state}; {message}")

    def _app_loop(self):
        while not self.cancel.is_set():
            try:
                self._app_tick(self.clock())
            except (Cancelled, PlatformCancelled):
                if self.cancel.is_set():
                    break
            except Exception as exc:
                # State-based logging keeps a persistent /proc or path error from
                # producing a new log entry every five seconds.
                self._app_status("error", f"自动启动暂缓：{exc}")
            self.cancel.wait(5)

    def _app_tick(self, now):
        with self.lock:
            if not self.running or self.cancel.is_set() or not self.state["automatic_launch"]:
                self.missing_since = self.last_app_observation = None
                self.app_suppress_recovery = False
                self._app_status("disabled", "已关闭" if not self.state["automatic_launch"] else "已停止")
                return
            if self.core_busy or self.app_busy or self.state.get("rest_until", 0) or self.state.get("tun_restore_pending"):
                self.missing_since = self.last_app_observation = None
                return
            token = _Cancellation(self.cancel, self.app_cancel)
            generation = self.app_generation
            monitor_id = self.monitor_id
        try:
            presence = self.platform.inspect_app()
        except Exception:
            with self.lock:
                if generation == self.app_generation:
                    self.missing_since = self.last_app_observation = None
                    self.app_suppress_recovery = True
            raise
        with self.lock:
            self._ensure_active(token)
            if generation != self.app_generation or not self.state["automatic_launch"]:
                return
            self.app_suppress_recovery = presence.state != "running"
            if presence.state != "missing":
                self.missing_since = self.last_app_observation = None
                self._app_status(presence.state, presence.message)
                return
            if (self.missing_since is None or self.last_app_observation is None or
                    now < self.last_app_observation or now-self.last_app_observation > 15):
                self.missing_since = now
            self.last_app_observation = now
            if now-self.missing_since < 10:
                self._app_status("confirming", "主程序缺失，持续确认满 10 秒再启动")
                return
            attempts = sorted(t for t in self.state["launch_attempts"] if t > now-3600)
            if len(attempts) >= 3 or (attempts and now < attempts[-1]+120):
                self._app_status("cooldown", "自动启动等待额度 / 2 分钟冷却；仍持续监控")
                return
            if self.core_busy or self.app_busy or self.state.get("rest_until", 0) or self.state.get("tun_restore_pending"):
                return
            self.app_busy = True
            self.state["launch_attempts"] = attempts+[now]
            try:
                self.store.save(self.state)
            except Exception:
                self.app_busy = False
                raise
            executable = self.state["executable"]
        try:
            self._ensure_active(token)
            # Never hold the UI/configuration lock during a process or D-Bus call.
            # The runtime checks the token and process absence immediately before spawn.
            result = self.platform.launch_app(executable, token)
            self.log(f"app_launch dispatched={result.dispatched} uncertain={result.uncertain}; {result.message}")
            with self.lock:
                if result.dispatched and self.running and monitor_id == self.monitor_id:
                    # Even an immediately disabled option must not leave old failures
                    # attached to an app whose launch was already dispatched.
                    self.request_cancel.set()
                    self.request_cancel = threading.Event()
                    self.missing_since = self.last_app_observation = None
                    self.next_poll = self.next_aux = 0
                    self.last_readiness = None
                    if self.engine and self.engine.target_key:
                        self.engine.start(self.clock(), self.engine.target_key)
                        self.engine.mark_unknown(self.clock(), "主程序启动后等待新诊断")
                    self._snapshot("主程序启动后等待新诊断")
                if (self.running and monitor_id == self.monitor_id and
                        generation == self.app_generation and not token.is_set()):
                    self._app_status("requested" if result.dispatched else "failed", result.message)
        finally:
            with self.lock:
                self.app_busy = False
