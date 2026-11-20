"""Private local settings, atomic durable reservations and redacted rotating logs."""
from __future__ import annotations

import copy
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import tempfile
import threading


def default_state():
    return {
        "software_attempts": [], "recovery_attempts": [], "tun_restore_pending": False,
        "recovery_tun_enabled": True, "rest_after_tun": False, "rest_until": 0, "recovery_stage": "",
        "schema": 1, "controller": "http://127.0.0.1:9097", "secret": "",
        "group": "GLOBAL", "test_url": "https://www.gstatic.com/generate_204",
        "executable": "", "config_directory": "", "automatic_restart": True,
        "automatic_launch": True, "restart_attempts": [], "launch_attempts": [],
        "unrecovered_restart_count": 0, "recovery_paused": False,
        "options": {"normal_seconds": 30, "retry_seconds": 5, "timeout_seconds": 5,
                    "failure_threshold": 5, "grace_seconds": 30,
                    "cooldown_seconds": 120, "hourly_limit": 5, "poll_seconds": 30},
    }


def _private_directory(path: Path):
    if path.is_symlink():
        raise ValueError("配置目录不能是符号链接")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        if path.stat().st_uid != os.getuid():
            raise PermissionError("配置目录不属于当前用户")
        path.chmod(0o700)


class Store:
    def __init__(self, directory=None):
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        self.directory = Path(directory) if directory else base / "clash-verge-watchdog"
        _private_directory(self.directory)
        self.path = self.directory / "settings.json"
        self.log_path = self.directory / "watchdog.log"
        self.lock = threading.RLock()
        if self.log_path.is_symlink():
            raise ValueError("日志文件不能是符号链接")
        self.logger = logging.getLogger(f"watchdog.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = RotatingFileHandler(self.log_path, maxBytes=2*1024*1024,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.logger.addHandler(handler)
        if os.name == "posix":
            self.log_path.chmod(0o600)
        self.secret = ""

    def load(self):
        with self.lock:
            state = default_state()
            if self.path.exists():
                if self.path.is_symlink():
                    raise ValueError("配置文件不能是符号链接")
                if os.name == "posix" and self.path.stat().st_uid != os.getuid():
                    raise PermissionError("配置文件不属于当前用户")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("schema") != 1:
                    raise ValueError("配置格式未知；请保留配置文件并检查，不会清空次数记录")
                for key in ("restart_attempts", "launch_attempts", "software_attempts", "recovery_attempts"):
                    entries = raw.get(key, [] if key in ("software_attempts", "recovery_attempts") else None)
                    if not isinstance(entries, list) or any(
                        isinstance(v, bool) or not isinstance(v, (int, float)) or
                        not math.isfinite(v) or v < 0 for v in entries
                    ):
                        raise ValueError("启动/重启次数记录不完整或损坏；不会自动重置")
                for key in ("automatic_restart", "automatic_launch", "recovery_paused", "tun_restore_pending", "rest_after_tun", "recovery_tun_enabled"):
                    if key in raw and type(raw[key]) is not bool:
                        raise ValueError("配置中的开关格式错误")
                rest_until = raw.get("rest_until", 0)
                if (type(rest_until) not in (int, float) or not math.isfinite(rest_until) or rest_until < 0):
                    raise ValueError("监控休息时间损坏")
                count = raw.get("unrecovered_restart_count", 0)
                if type(count) is not int or count < 0:
                    raise ValueError("重启退避记录损坏")
                options = raw.get("options", {})
                if not isinstance(options, dict):
                    raise ValueError("监控参数格式错误")
                state.update(raw)
                state["options"] = {**default_state()["options"], **options}
            self.secret = state["secret"]
            return copy.deepcopy(state)

    def save(self, state):
        with self.lock:
            if self.path.is_symlink():
                raise ValueError("配置文件不能是符号链接")
            content = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
            fd, temporary = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(content + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                if os.name == "posix":
                    self.path.chmod(0o600)
                    dirfd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dirfd)
                    finally:
                        os.close(dirfd)
                self.secret = state["secret"]
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def log(self, message):
        text = str(message)
        if self.secret:
            text = text.replace(self.secret, "[密钥已隐藏]")
        text = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [隐藏]", text)
        text = re.sub(r"(?i)(secret|password|token|authorization)\s*[=:]\s*[^\s,;&]+", r"\1=[隐藏]", text)
        text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[省略查询参数]", text)
        text = "".join(" " if ord(c) < 32 else c for c in text)[:16000]
        self.logger.info(text)
        return text

    def close(self):
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)


class InstanceLock:
    """Per-user Linux lock; do not replace a held inode or acquire a global port."""
    def __init__(self, directory):
        self.stream = None
        if os.name != "posix":
            return  # Windows is supported only for isolated development tests.
        import fcntl
        path = Path(directory) / "instance.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("监控器已经在运行，请检查现有窗口") from None
        self.stream.write(str(os.getpid()))
        self.stream.flush()

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None
