"""Bounded, sequential recovery. All waits are cancellable."""
import time
import uuid
from .api import ApiError, Cancelled
from .engine import HealthSample
from .platform_linux import PlatformError


class Recovery:
    def __init__(self, monitor, token, selected):
        self.m = monitor
        self.token = token
        self.selected = selected

    def stage(self, text):
        self.m.state["recovery_stage"] = text
        self.m.store.save(self.m.state)
        self.m.emit("recovery", text)
        self.m.log("recovery_stage " + text)

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.m._ensure_active(self.token)
            self.m.cancel.wait(min(.1, end-time.monotonic()))

    def ready(self, tun=True):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            self.m._ensure_active(self.token)
            try:
                self.m.api.get_version(self.token)
                if tun:
                    self.m.platform.set_tun(True, self.token)
                    if not self.m.api.tun_enabled(self.token):
                        raise PlatformError("TUN 尚未开启")
                return True
            except (ApiError, PlatformError):
                self.wait(1)
        return False

    def diagnose(self):
        for attempt in range(2):
            self.stage(f"恢复诊断 {attempt+1}/2")
            self.wait(2)
            fresh = self.m.api.get_proxies(self.token).resolve(self.m.state["group"])
            if fresh.target_key != self.selected.target_key:
                self.m._follow(fresh)
                raise Cancelled("节点发生变化，终止旧恢复流程")
            if not self.m.api.tun_enabled(self.token):
                raise PlatformError("测速前 TUN 未开启")
            result = self.m.api.diagnose(fresh.leaf, self.m.state["test_url"],
                                        self.m.state["options"]["timeout_seconds"]*1000,
                                        self.token, self.m._on_probe)
            after = self.m.api.get_proxies(self.token).resolve(self.m.state["group"])
            if after.target_key != fresh.target_key:
                self.m._follow(after)
                raise Cancelled("测速期间节点变化")
            if not self.m.api.tun_enabled(self.token):
                raise PlatformError("测速期间 TUN 状态变化")
            kind = "partial" if result.partial else "success" if result.success else "failure"
            self.m.rounds.append((result.completed_at, kind))
            self.m.emit("probe", {"delay": result.delay_ms, "kind": kind, "time": result.completed_at})
            self.m.log(f"recovery_probe outcome={kind}")
            if result.success:
                with self.m.lock:
                    self.m.engine.start(self.m.clock(), fresh.target_key)
                    self.m.engine.grace_until = self.m.clock()
                    self.m.engine.observe(HealthSample(uuid.uuid4().hex, fresh.target_key,
                        self.m.engine.generation, result.completed_at, kind, result.delay_ms), self.m.clock())
                    self.m._persist_engine()
                return True
        return False

    def tun_cycles(self):
        for number in range(2):
            fresh = self.m.api.get_proxies(self.token).resolve(self.m.state["group"])
            if fresh.target_key != self.selected.target_key:
                self.m._follow(fresh)
                raise Cancelled("节点已变化，终止 TUN 恢复")
            self.stage(f"TUN 恢复 {number+1}/2")
            self.m.state["tun_restore_pending"] = True
            self.m.store.save(self.m.state)
            try:
                self.m.platform.set_tun(False, self.token)
                self.wait(1)
            finally:
                # Finish restoring our own temporary change even when Stop was pressed.
                self.m.platform.set_tun(True, None)
                self.m.state["tun_restore_pending"] = False
                self.m.store.save(self.m.state)
            if not self.ready():
                return False
            if self.diagnose():
                return True
        return False

    def run(self):
        try:
            if self.m.api.tun_enabled(self.token) and self.tun_cycles():
                return
            self.stage("重启内核")
            now = self.m.clock()
            self.m.engine._prune_attempts(now)
            if len(self.m.engine._attempts) >= self.m.engine.options.hourly_limit:
                return
            self.m.engine._attempts.append(now)
            self.m.engine._last_attempt = now
            self.m.engine.unrecovered_restart_count += 1
            self.m.engine.mark_unknown(now, "内核恢复期间等待诊断")
            self.m._persist_engine()
            self.m._ensure_active(self.token)
            result = self.m.platform.restart_core(cancel=self.token)
            if result.uncertain:
                self.m.state["recovery_paused"] = True
                self.m.store.save(self.m.state)
            if result.uncertain or not result.dispatched:
                raise PlatformError(result.message)
            self.wait(2)
            if self.ready():
                if self.diagnose() or self.tun_cycles():
                    return
            attempts = [t for t in self.m.state.get("software_attempts", []) if t > self.m.clock()-3600]
            if len(attempts) >= 2:
                self.stage("软件重启额度已用完，继续诊断")
                return
            self.stage("重启 Clash 软件")
            self.m.state["software_attempts"] = attempts + [self.m.clock()]
            self.m.store.save(self.m.state)
            old_processes = self.m.platform.inspect_app().processes
            self.m.platform.restart_application(self.token)
            deadline = time.monotonic()+30
            while old_processes and self.m.platform.inspect_app().processes == old_processes:
                if time.monotonic() >= deadline:
                    raise PlatformError("软件重启尚未确认，终止本轮自动操作")
                self.wait(.2)
            self.wait(2)
            if not self.ready():
                self.m.state["tun_restore_pending"] = True
                self.m.state["rest_after_tun"] = True
                self.m.store.save(self.m.state)
                self.stage("等待 TUN 恢复")
                return
            if not self.diagnose():
                self.m.state["rest_until"] = self.m.clock()+60
                self.m.store.save(self.m.state)
                self.m.rest_deadline = time.monotonic()+60
                self.stage("监控休息 60 秒")
        finally:
            if not self.m.state.get("tun_restore_pending") and not self.m.state.get("rest_until"):
                self.m.state["recovery_stage"] = ""
                self.m.store.save(self.m.state)
            with self.m.lock:
                self.m.engine.consecutive_failures = 0
                self.m.next_poll = 0
