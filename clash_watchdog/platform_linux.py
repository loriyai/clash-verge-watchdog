"""Ubuntu integration using Clash's own session D-Bus menu, never process killing.

Protocol sources (reviewed against Clash Verge Rev v2.5.10):
https://github.com/clash-verge-rev/clash-verge-rev/blob/v2.5.10/src-tauri/src/core/tray/mod.rs
https://github.com/AyatanaIndicators/libayatana-appindicator/blob/main/src/notification-item.xml
https://github.com/gnustep/libs-dbuskit/blob/master/Bundles/DBusMenu/com.canonical.dbusmenu.xml
The privileged service has GUI-owned session credentials. Do not impersonate its
owner or send stop/start requests independently of the GUI's lifecycle manager.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

APP_ID = "io.github.clash-verge-rev.clash-verge-rev"
APP_NAMES = frozenset(("clash-verge", "clash-verge-rev"))
RESTART_LABELS = frozenset(("重启 Clash 内核", "重啟 Clash 核心", "Restart Clash Core"))
MORE_LABELS = frozenset(("更多", "More"))
SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"


class PlatformError(RuntimeError):
    pass


class Cancelled(PlatformError):
    pass


def check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("操作已取消，未继续派发。")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    start_time: str
    executable: str


@dataclass(frozen=True)
class AppInspection:
    state: str
    message: str
    processes: tuple[ProcessIdentity, ...] = ()


@dataclass(frozen=True)
class RestartTarget:
    owner: str
    item_path: str
    menu_path: str
    revision: int
    menu_id: int
    pid: int
    start_time: str
    label: str
    parent_id: int


@dataclass(frozen=True)
class RestartResult:
    available: bool
    message: str
    dispatched: bool = False
    uncertain: bool = False
    target: RestartTarget | None = None


def default_data_directory() -> str:
    base = os.environ.get("XDG_DATA_HOME", "")
    if not base or not Path(base).is_absolute():
        base = str(Path.home() / ".local" / "share")
    return str(Path(base) / APP_ID)


def _yaml_scalar(value: str) -> str:
    """Read simple top-level scalars emitted by Clash; refuse complex YAML."""
    value = value.strip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise PlatformError("连接配置中的引号值无法安全解析，请手动填写。") from exc
        if not isinstance(parsed, str):
            raise PlatformError("连接配置不是字符串。")
        return parsed
    if value.startswith("'"):
        if not value.endswith("'"):
            raise PlatformError("连接配置中的引号未闭合。")
        return value[1:-1].replace("''", "'")
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if value in ("null", "~", ""):
        return ""
    if value[:1] in "[{}&*!>|" or "\n" in value or "\r" in value:
        raise PlatformError("连接配置包含复杂 YAML，请在监控器中手动填写。")
    return value


def _read_top_level_connection(path: Path) -> dict[str, str]:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise PlatformError("配置文件过大，未读取。")
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^(external-controller|external-controller-unix|secret)\s*:\s*(.*)$", line)
        if not match:
            continue
        key, raw = match.groups()
        if key in found:
            raise PlatformError("配置中存在重复连接字段，请手动确认。")
        found[key] = _yaml_scalar(raw)
    return found


def normalize_controller(address: str) -> str:
    address = address.strip()
    if "://" not in address:
        address = "http://" + address
    try:
        parsed = urlsplit(address)
        port = parsed.port
    except ValueError as exc:
        raise PlatformError("控制地址格式无效。") from exc
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "::1", "localhost")
            or not port or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise PlatformError("仅支持明确端口的本机回环控制地址，例如 http://127.0.0.1:9097。")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"{parsed.scheme}://{host}:{port}"


def read_connection(data_directory: str = "") -> dict[str, str]:
    """Read existing runtime/config connection; never rewrite Clash YAML."""
    directory = Path(data_directory or default_data_directory()).expanduser()
    if not directory.is_absolute() or not directory.is_dir():
        raise PlatformError("请选择现有 Clash Verge 配置目录。")
    if directory == Path(default_data_directory()) and hasattr(os, "getuid"):
        service_socket = Path("/run/clash-verge-service/users") / str(os.getuid()) / "verge-mihomo.sock"
        if service_socket.is_socket():
            return {"controller_url": "unix://" + str(service_socket), "secret": "", "data_directory": str(directory)}
    # Generated runtime is authoritative: external-controller may be disabled there.
    for name in ("clash-verge.yaml", "config.yaml"):
        path = directory / name
        if not path.is_file():
            continue
        fields = _read_top_level_connection(path)
        socket_path = Path(fields.get("external-controller-unix", ""))
        if socket_path == directory / "verge-mihomo.sock" and socket_path.is_socket():
            return {"controller_url": "unix://" + str(socket_path), "secret": "", "data_directory": str(directory)}
        if "external-controller" not in fields:
            continue
        if not fields["external-controller"]:
            raise PlatformError("Clash 外部控制接口未启用，请在 Clash 设置中开启本机接口。")
        return {"controller_url": normalize_controller(fields["external-controller"]),
                "secret": fields.get("secret", ""), "data_directory": str(directory)}
    raise PlatformError("未找到已启用的外部控制接口，请在 Clash 设置中启用后再读取。")


def inspect_processes(proc_root: Path = Path("/proc"), uid: int | None = None) -> AppInspection:
    if uid is None:
        if not hasattr(os, "getuid"):
            return AppInspection("unknown", "主程序进程检测仅在 Linux 上可用。")
        uid = os.getuid()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return AppInspection("unknown", "无法枚举本机进程，暂不启动或重启。")
    matches: list[ProcessIdentity] = []
    uncertain = False
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            process_uid = entry.stat().st_uid
            if process_uid != uid:
                # A second user/root-owned GUI can hold the same controller/TUN.
                # Do not start another GUI just because it is outside this session.
                try:
                    if (entry / "comm").read_text(encoding="utf-8").strip() in APP_NAMES:
                        uncertain = True
                except OSError:
                    pass
                continue
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            if comm not in APP_NAMES:
                continue
            executable = os.readlink(entry / "exe")
            if Path(executable).name not in APP_NAMES or executable.endswith(" (deleted)"):
                uncertain = True
                continue
            # /proc/<pid>/stat field 22, preserving parenthesized comm with spaces.
            raw_stat = (entry / "stat").read_text(encoding="ascii")
            tail = raw_stat[raw_stat.rindex(")") + 2:].split()
            start_time = tail[19]
            if not start_time.isdecimal():
                raise ValueError("invalid start time")
            matches.append(ProcessIdentity(int(entry.name), uid, start_time, executable))
        except FileNotFoundError:
            continue  # process exited during observation
        except (OSError, ValueError, IndexError):
            uncertain = True
    if uncertain:
        return AppInspection("unknown", "部分当前用户进程无法核实，暂不自动操作。", tuple(matches))
    if len(matches) > 1:
        return AppInspection("ambiguous", "检测到多个 Clash 主程序，无法确定操作目标。", tuple(matches))
    if matches:
        return AppInspection("running", "Clash 主程序进程存在。", tuple(matches))
    return AppInspection("missing", "未发现当前用户的 Clash 主程序进程。")


def validate_executable(executable: str) -> str:
    path = Path(executable).expanduser()
    if not path.is_absolute():
        raise PlatformError("请选择 Clash 主程序的绝对路径。")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise PlatformError("Clash 主程序路径不存在。") from exc
    known = resolved.name in APP_NAMES or bool(re.fullmatch(r"(?i)clash[. _-]?verge[^/]*\.appimage", resolved.name))
    if not known or not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise PlatformError("请选择可执行的 clash-verge 或 Clash Verge AppImage。")
    if info.st_mode & (stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID):
        raise PlatformError("Clash 可执行文件权限异常，未自动启动。")
    if hasattr(os, "getuid") and info.st_uid not in (0, os.getuid()):
        raise PlatformError("Clash 可执行文件不属于当前用户或系统，未自动启动。")
    return str(resolved)


def find_executable() -> str:
    candidate = shutil.which("clash-verge") or shutil.which("clash-verge-rev")
    for value in (candidate, "/usr/bin/clash-verge", "/usr/local/bin/clash-verge"):
        if value:
            try:
                return validate_executable(value)
            except PlatformError:
                pass
    return ""


def _unpack(value: Any) -> Any:
    if hasattr(value, "unpack"):
        return _unpack(value.unpack())
    if isinstance(value, dict):
        return {key: _unpack(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_unpack(item) for item in value)
    return value


def normalize_label(label: str) -> str:
    # GTK underscores mark mnemonics, while a double underscore is literal.
    return " ".join(label.replace("__", "\0").replace("_", "").replace("\0", "_").split())


def restart_menu_item(layout: Any) -> tuple[int, str, int]:
    """Require one enabled restart-core leaf under an enabled top-level More menu."""
    layout = _unpack(layout)
    seen: set[int] = set()
    candidates: list[tuple[int, str, int]] = []
    count = 0

    def visit(node: Any, ancestors: tuple[tuple[int, str], ...], active: bool) -> None:
        nonlocal count
        count += 1
        if count > 5000 or len(ancestors) > 16 or not isinstance(node, (tuple, list)) or len(node) != 3:
            raise PlatformError("Clash 菜单结构无法安全解析。")
        item_id, properties, children = node
        if type(item_id) is not int or item_id in seen or not isinstance(properties, dict) or not isinstance(children, (tuple, list)):
            raise PlatformError("Clash 菜单包含重复标识或无效结构。")
        seen.add(item_id)
        label = normalize_label(str(properties.get("label", "")))
        active = active and properties.get("enabled", True) is True and properties.get("visible", True) is True
        standard = properties.get("type", "standard") == "standard"
        if active and standard and not children and label in RESTART_LABELS:
            # Root(0) -> More -> Restart Core. Never act on a proxy/profile with a matching name.
            if len(ancestors) == 2 and ancestors[-1][1] in MORE_LABELS:
                candidates.append((item_id, label, ancestors[-1][0]))
        for child in children:
            visit(child, ancestors + ((item_id, label),), active)

    visit(layout, (), True)
    if len(candidates) != 1:
        raise PlatformError("未找到唯一可用的“更多 → 重启 Clash 内核”菜单（支持中文/英文）。")
    return candidates[0]


class GioSessionBus:
    """GDBus calls use the unique name, not a reusable service name, for dispatch."""
    def __init__(self) -> None:
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
            self.Gio, self.GLib = Gio, GLib
            # bus_get_sync has no timeout parameter. A broken session-bus
            # handshake must not leave the setup worker waiting indefinitely.
            cancellable = Gio.Cancellable()
            deadline = threading.Timer(2.0, cancellable.cancel)
            deadline.daemon = True
            deadline.start()
            try:
                self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, cancellable)
            finally:
                deadline.cancel()
        except Exception as exc:
            raise PlatformError("无法连接桌面 D-Bus；需要图形登录会话以及 python3-gi。") from exc

    def call(self, owner: str, path: str, interface: str, method: str, signature: str | None = None,
             arguments: tuple = (), timeout: int = 1200) -> Any:
        params = self.GLib.Variant(signature, arguments) if signature else None
        value = self.connection.call_sync(owner, path, interface, method, params, None,
                                          self.Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
        return _unpack(value)

    def owner_identity(self, owner: str) -> tuple[int, int]:
        pid = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                        "GetConnectionUnixProcessID", "(s)", (owner,))[0]
        uid = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                        "GetConnectionUnixUser", "(s)", (owner,))[0]
        return int(pid), int(uid)

    def unique_owner(self, name: str) -> str:
        owner = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "GetNameOwner", "(s)", (name,))[0]
        if not isinstance(owner, str) or not owner.startswith(":"):
            raise PlatformError("D-Bus 没有提供唯一进程身份。")
        return owner

    def item_properties(self, owner: str, path: str) -> dict:
        result = self.call(owner, path, "org.freedesktop.DBus.Properties", "GetAll", "(s)", (SNI_IFACE,))[0]
        if not isinstance(result, dict):
            raise PlatformError("Clash 托盘接口属性无效。")
        return result

    def menu_layout(self, owner: str, path: str) -> tuple[int, Any]:
        result = self.call(owner, path, MENU_IFACE, "GetLayout", "(iias)",
                           (0, -1, ("label", "enabled", "visible", "type", "children-display", "toggle-state", "toggle-type")))
        if len(result) != 2 or type(result[0]) is not int:
            raise PlatformError("Clash 菜单版本无效。")
        return result[0], result[1]

    def discover_items(self, pid: int, uid: int, cancel: threading.Event | None = None) -> tuple[tuple[str, str], ...]:
        deadline = time.monotonic() + 8
        candidates: set[tuple[str, str]] = set()
        owners: set[str] = set()

        def budget() -> None:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise PlatformError("D-Bus 托盘发现超时，稍后复查。")

        for watcher in ("org.kde.StatusNotifierWatcher", "org.freedesktop.StatusNotifierWatcher"):
            budget()
            try:
                values = self.call(watcher, "/StatusNotifierWatcher", "org.freedesktop.DBus.Properties",
                                   "Get", "(ss)", ("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems"))[0]
            except Exception:
                continue
            for item in values:
                budget()
                service, slash, suffix = item.partition("/")
                path = "/" + suffix if slash else "/StatusNotifierItem"
                try:
                    owner = self.unique_owner(service)
                    if self.owner_identity(owner) == (pid, uid):
                        candidates.add((owner, path))
                        owners.add(owner)
                except Exception:
                    continue
        # Discover the application's own object tree when the desktop does not run
        # a StatusNotifierWatcher. No unrelated process object trees are inspected.
        names = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames")[0]
        for name in names:
            budget()
            if not name.startswith(":"):
                continue
            try:
                if self.owner_identity(name) == (pid, uid):
                    owners.add(name)
            except Exception:
                continue
        for owner in owners:
            pending, visited = [("/", 0)], set()
            while pending:
                budget()
                path, depth = pending.pop()
                if path in visited:
                    continue
                visited.add(path)
                if len(visited) > 64 or depth > 7:
                    raise PlatformError("Clash D-Bus 对象树超出安全读取范围。")
                try:
                    xml = self.call(owner, path, "org.freedesktop.DBus.Introspectable", "Introspect")[0]
                except Exception:
                    continue
                if not isinstance(xml, str) or len(xml) > 131072 or "<!ENTITY" in xml:
                    raise PlatformError("Clash D-Bus 对象描述无效。")
                tree = ET.fromstring(xml)
                if any(child.get("name") == SNI_IFACE for child in tree.findall("interface")):
                    candidates.add((owner, path))
                for child in tree.findall("node"):
                    segment = child.get("name", "")
                    if re.fullmatch(r"[A-Za-z0-9_]+", segment):
                        pending.append((path.rstrip("/") + "/" + segment, depth + 1))
        return tuple(sorted(candidates))

    def click(self, owner: str, path: str, menu_id: int, cancel: threading.Event | None = None) -> None:
        check_cancel(cancel)
        self.call(owner, path, MENU_IFACE, "Event", "(isvu)",
                  (menu_id, "clicked", self.GLib.Variant("i", 0), int(time.monotonic() * 1000) & 0xFFFFFFFF), 2000)


class LinuxPlatform:
    def __init__(self, inspector: Callable[[], AppInspection] | None = None, bus: Any = None,
                 popen: Callable[..., Any] = subprocess.Popen):
        self._inspector = inspector or inspect_processes
        self._bus = bus
        self._popen = popen
        self._action_lock = threading.Lock()

    def inspect_app(self) -> AppInspection:
        try:
            return self._inspector()
        except Exception:
            return AppInspection("unknown", "无法可靠确认 Clash 主程序进程。")

    @staticmethod
    def find_executable() -> str:
        return find_executable()

    @property
    def bus(self) -> Any:
        if self._bus is None:
            self._bus = GioSessionBus()
        return self._bus

    def probe_restart(self, cancel: threading.Event | None = None) -> RestartResult:
        try:
            check_cancel(cancel)
            observed = self.inspect_app()
            if observed.state != "running" or len(observed.processes) != 1:
                return RestartResult(False, observed.message)
            process = observed.processes[0]
            items = self.bus.discover_items(process.pid, process.uid, cancel)
            targets = []
            for owner, item_path in items:
                check_cancel(cancel)
                if not owner.startswith(":") or self.bus.owner_identity(owner) != (process.pid, process.uid):
                    raise PlatformError("托盘菜单所属进程已变化。")
                check_cancel(cancel)
                properties = self.bus.item_properties(owner, item_path)
                indicator_id = str(properties.get("Id", ""))
                # Tauri prefixes the application-provided tray ID for AppIndicator.
                if indicator_id not in ("clash-verge-rev-tray", "tray-icon tray app clash-verge-rev-tray"):
                    continue
                menu = properties.get("Menu")
                if not isinstance(menu, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_]+/?)*", menu) or menu == "/":
                    raise PlatformError("Clash 未公开可验证的托盘菜单。")
                check_cancel(cancel)
                revision, layout = self.bus.menu_layout(owner, menu)
                menu_id, label, parent_id = restart_menu_item(layout)
                targets.append(RestartTarget(owner, item_path, menu, revision, menu_id,
                                             process.pid, process.start_time, label, parent_id))
            unique = set(targets)
            if len(unique) != 1:
                return RestartResult(False, "未找到唯一的 Clash 托盘重启入口；请确认 Clash 托盘已启用且使用中文或英文界面。")
            target = unique.pop()
            if self.inspect_app() != observed or self.bus.owner_identity(target.owner) != (process.pid, process.uid):
                return RestartResult(False, "读取期间 Clash 进程身份变化，等待重新检查。")
            check_cancel(cancel)
            return RestartResult(True, "已验证当前用户 Clash 的 D-Bus 内核重启菜单；派发前再次核对。", target=target)
        except Cancelled as exc:
            return RestartResult(False, str(exc))
        except Exception as exc:
            # Do not include arbitrary D-Bus payloads / process command lines in logs.
            reason = str(exc) if isinstance(exc, PlatformError) else type(exc).__name__
            return RestartResult(False, "重启入口暂不可用：" + reason)

    def restart_core(self, expected: RestartTarget | None = None, cancel: threading.Event | None = None) -> RestartResult:
        if not self._action_lock.acquire(blocking=False):
            return RestartResult(False, "另一个 Clash 操作正在进行。")
        entered = False
        try:
            current = self.probe_restart(cancel)
            if not current.available or current.target is None:
                return current
            # Revision covers the whole menu, including unrelated proxy updates.
            # The newly read full layout must retain every target identity field;
            # its newer revision alone does not invalidate a verified core action.
            if expected is not None and replace(expected, revision=current.target.revision) != current.target:
                return RestartResult(False, "Clash 重启菜单或进程身份已变化，未发送重启。")
            check_cancel(cancel)
            target = current.target
            presence = self.inspect_app()
            if presence.state != "running" or len(presence.processes) != 1:
                return RestartResult(False, "派发前 Clash 进程状态已变化，未发送重启。")
            process = presence.processes[0]
            if ((process.pid, process.start_time) != (target.pid, target.start_time) or
                    self.bus.owner_identity(target.owner) != (process.pid, process.uid)):
                return RestartResult(False, "派发前 Clash 进程身份已变化，未发送重启。")
            check_cancel(cancel)
            entered = True
            self.bus.click(target.owner, target.menu_path, target.menu_id, cancel)
            return RestartResult(True, "已通过 Clash 自身菜单请求重启内核，等待新诊断确认恢复。", True, False, target)
        except Cancelled as exc:
            return RestartResult(False, str(exc), entered, entered)
        except Exception:
            return RestartResult(False, "D-Bus 重启请求结果不明，请检查 Clash 后恢复自动重启。", entered, entered)
        finally:
            self._action_lock.release()

    def _menu_action(self, labels, cancel=None, desired=None):
        check_cancel(cancel)
        ready = self.probe_restart(cancel)
        if not ready.available:
            raise PlatformError(ready.message)
        target = ready.target
        _, layout = self.bus.menu_layout(target.owner, target.menu_path)
        matches = []
        def visit(node, depth=0):
            item, props, children = node
            if (normalize_label(props.get("label", "")) in labels and
                    props.get("enabled", True) and props.get("visible", True) and
                    depth == (1 if desired is not None else 2)):
                matches.append((item, props))
            for child in children:
                visit(child, depth+1)
        visit(layout)
        if len(matches) != 1:
            raise PlatformError("无法核实唯一菜单入口")
        item, props = matches[0]
        if desired is not None:
            current = props.get("toggle-state")
            if current not in (0, 1):
                raise PlatformError("无法核实 TUN 开关状态")
            if bool(current) == desired:
                return True
        presence = self.inspect_app()
        if (presence.state != "running" or len(presence.processes) != 1 or
                presence.processes[0].pid != target.pid or
                presence.processes[0].start_time != target.start_time or
                self.bus.owner_identity(target.owner) != (target.pid, presence.processes[0].uid)):
            raise PlatformError("菜单进程身份已变化")
        check_cancel(cancel)
        self.bus.click(target.owner, target.menu_path, item, cancel)
        return False

    def set_tun(self, enabled, cancel=None):
        labels = {"TUN Mode", "TUN 模式", "虚拟网卡模式", "虛擬網卡模式"}
        with self._action_lock:
            if self._menu_action(labels, cancel, enabled):
                return
            # Read state after dispatch without blindly toggling again.
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                check_cancel(cancel)
                ready = self.probe_restart(cancel)
                if ready.available:
                    _, layout = self.bus.menu_layout(ready.target.owner, ready.target.menu_path)
                    states = [props.get("toggle-state") for _, props, _ in layout[2]
                              if normalize_label(props.get("label", "")) in labels]
                    if states == [int(enabled)]:
                        return
                time.sleep(.1)
            raise PlatformError("TUN 开关状态确认超时")

    def restart_application(self, cancel=None):
        with self._action_lock:
            self._menu_action({"Restart Application", "重启应用", "重启软件", "重啟應用程式"}, cancel)

    def launch_app(self, executable: str, cancel: threading.Event | None = None) -> RestartResult:
        if not self._action_lock.acquire(blocking=False):
            return RestartResult(False, "另一个 Clash 操作正在进行。")
        entered = False
        try:
            check_cancel(cancel)
            path = validate_executable(executable)
            presence = self.inspect_app()
            if presence.state != "missing":
                return RestartResult(False, presence.message)
            check_cancel(cancel)
            entered = True
            self._popen([path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True, close_fds=True)
            return RestartResult(True, "已请求启动 Clash 主程序，等待进程和接口恢复。", True)
        except Cancelled as exc:
            return RestartResult(False, str(exc), entered, entered)
        except Exception as exc:
            return RestartResult(False, "主程序启动失败：" + (str(exc) if isinstance(exc, PlatformError) else type(exc).__name__),
                                 False, False)
        finally:
            self._action_lock.release()
