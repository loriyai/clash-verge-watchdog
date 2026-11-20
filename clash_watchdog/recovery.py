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
        self.tun_required = monitor.state.get("recovery_tun_enabled", True)

    def stage(self, text):
        self.m.set_recovery_stage(text)

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.m._ensure_active(self.token)
            self.m.cancel.wait(min(.1, end-time.monotonic()))

    def ready(self):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            self.m._ensure_active(self.token)
            try:
                self.m.api.get_version(self.token)
                self.m.platform.set_tun(self.tun_required, self.token)
                if self.m.api.tun_enabled(self.token) != self.tun_required:
                    raise PlatformError("TUN 状态尚未恢复")
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
            if self.m.api.tun_enabled(self.token) != self.tun_required:
                raise PlatformError("测速前 TUN 未开启")
            result = self.m.api.diagnose(fresh.leaf, self.m.state["test_url"],
                                        self.m.state["options"]["timeout_seconds"]*1000,
                                        self.token, self.m._on_probe)
            after = self.m.api.get_proxies(self.token).resolve(self.m.state["group"])
            if after.target_key != fresh.target_key:
                self.m._follow(after)
                raise Cancelled("测速期间节点变化")
            if self.m.api.tun_enabled(self.token) != self.tun_required:
                raise PlatformError("测速期间 TUN 状态变化")
            kind = "partial" if result.partial else "success" if result.success else "failure"
            self.m.rounds.append((result.completed_at, kind))
            self.m.emit("probe", {"delay": result.delay_ms, "kind": kind, "time": result.completed_at})
            self.m.log(f"recovery_probe outcome={kind}")
            if result.success:
                with self.m.lock:
                    self.m._ensure_active(self.token)
                    self.m.engine.reset_diagnostics(self.m.clock())
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
            if not self.m.api.tun_enabled(self.token):
                raise Cancelled("TUN 已被关闭，终止旧恢复流程")
            self.stage(f"TUN 恢复 {number+1}/2")
            self.m.update_recovery(tun_restore_pending=True)
            try:
                self.m.platform.set_tun(False, self.token)
                self.wait(1)
            finally:
                # Finish restoring our own temporary change even when Stop was pressed.
                self.m.platform.set_tun(True, None)
                self.m.update_recovery(tun_restore_pending=False)
            if not self.ready():
                return False
            if self.diagnose():
                return True
        return False

    def run(self):
        try:
            self.tun_required = self.m.api.tun_enabled(self.token)
            self.m.update_recovery(recovery_tun_enabled=self.tun_required)
            if self.tun_required and self.tun_cycles():
                return
            self.stage("重启内核")
            with self.m.lock:
                decision = self.m.engine.record_recovery_restart(self.m.clock())
                if decision != "ready":
                    self.m.emit("notice", "内核恢复等待额度 / 冷却：" + decision)
                    return
                self.m._persist_engine()
            self.m._ensure_active(self.token)
            result = self.m.platform.restart_core(cancel=self.token)
            if result.uncertain:
                self.m.update_recovery(recovery_paused=True)
            if result.uncertain or not result.dispatched:
                raise PlatformError(result.message)
            self.wait(2)
            if self.ready():
                if self.diagnose() or (self.tun_required and self.tun_cycles()):
                    return
            if not self.m.reserve_software_restart():
                return
            self.stage("重启 Clash 软件")
            old_processes = self.m.platform.inspect_app().processes
            self.m.platform.restart_application(self.token)
            deadline = time.monotonic()+30
            while old_processes and self.m.platform.inspect_app().processes == old_processes:
                if time.monotonic() >= deadline:
                    raise PlatformError("软件重启尚未确认，终止本轮自动操作")
                self.wait(.2)
            self.wait(2)
            if not self.ready():
                self.m.update_recovery(tun_restore_pending=True, rest_after_tun=True)
                self.stage("等待 TUN 恢复")
                return
            if not self.diagnose():
                self.m.enter_rest()
        finally:
            with self.m.lock:
                if not self.m.state.get("tun_restore_pending") and not self.m.state.get("rest_until"):
                    self.m.update_recovery(recovery_stage="")
                self.m.engine.finish_recovery()
                self.m.next_poll = 0
