"""Read-only local Mihomo API; synchronous methods belong on a worker thread.

No system/environment HTTP proxy, redirects, remote controller, node switching,
core configuration writes or process operations are supported by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import http.client
import io
import json
import re
from pathlib import Path
import socket
import ssl
import threading
import time
from typing import Callable
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4


DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
BACKUP_TEST_URL = "https://cp.cloudflare.com/generate_204"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
GROUP_TYPES = {"selector", "urltest", "url-test", "fallback", "loadbalance", "load-balance", "relay"}


class ApiError(Exception):
    """kind is setup/selection/controller/response; none is node-failure evidence."""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.request_id = ""
        self.endpoint = ""
        self.request_ms = 0


class Cancelled(Exception):
    pass


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("操作已取消。")


def _valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(name.strip()) and len(name) <= 4096 and not any(ord(c) < 32 for c in name)


def _delay(value: object) -> bool:
    return type(value) is int and 0 <= value <= 65535


@dataclass(frozen=True)
class HistoryRecord:
    timestamp: str
    time_utc: float
    delay_ms: int


@dataclass(frozen=True)
class ProxyNode:
    name: str
    type: str
    now: str | None = None
    all: tuple[str, ...] = ()
    history: tuple[HistoryRecord, ...] = ()
    is_group: bool = False

    @property
    def latest_history(self) -> HistoryRecord | None:
        return self.history[-1] if self.history else None


@dataclass(frozen=True)
class Selection:
    root: str
    leaf: str
    chain: tuple[str, ...]
    target_key: str


@dataclass(frozen=True)
class ProxySnapshot:
    nodes: dict[str, ProxyNode]

    def resolve(self, root: str) -> Selection:
        if not _valid_name(root):
            raise ApiError("selection", "请选择要监控的代理组或节点。")
        seen: set[str] = set()
        chain: list[str] = []
        name = root
        for _ in range(64):
            if name not in self.nodes:
                raise ApiError("selection", "选中的代理组或节点不存在，请重新加载列表。")
            if name in seen:
                raise ApiError("selection", "代理组选择形成循环，无法确定当前节点。")
            seen.add(name)
            chain.append(name)
            node = self.nodes[name]
            if node.type.lower() in {"loadbalance", "load-balance", "relay"}:
                raise ApiError("selection", "此代理组没有唯一当前节点，请选择具体节点或普通选择组。")
            if not node.is_group:
                if node.type.lower() in {"direct", "reject", "rejectdrop", "reject-drop", "pass"} or name.upper() in {"DIRECT", "REJECT", "REJECT-DROP"}:
                    raise ApiError("selection", "当前选择为直连或拒绝策略，不能用作代理节点故障判断。")
                return Selection(root, name, tuple(chain), json.dumps(chain, ensure_ascii=False, separators=(",", ":")))
            if not node.now:
                raise ApiError("selection", "代理组没有当前选择，请先在 Clash Verge 中选择节点。")
            if node.all and node.now not in node.all:
                raise ApiError("selection", "代理组选择与成员列表不一致，请稍后重试。")
            name = node.now
        raise ApiError("selection", "代理组嵌套层数过多。")


@dataclass(frozen=True)
class Probe:
    success: bool
    delay_ms: int | None
    started_at: float
    completed_at: float
    request_id: str
    endpoint: str
    http_status: int | None
    error_kind: str | None
    error: str | None
    request_ms: int


@dataclass(frozen=True)
class DiagnosticRound:
    id: str
    probes: tuple[Probe, ...]
    success: bool
    partial: bool
    delay_ms: int | None
    started_at: float
    completed_at: float


@dataclass(frozen=True)
class RouteSnapshot:
    mode: str
    relationship: str
    relevant: int
    matching: int
    different: int
    unknown: int
    unclassified: int
    complete: bool
    observed_at: float
    request_ms: int

    @property
    def summary(self) -> str:
        text = {
            "none": "本次未观察到相关连接，不能据此判断故障",
            "matching": "观察到相关连接经过监控节点",
            "different": "相关连接出口与监控节点不同",
            "mixed": "相关连接使用多个出口，其中部分与监控节点不同",
            "unknown": "连接资料不足，暂不能比较出口",
        }[self.relationship]
        return f"模式={self.mode}；相关连接={self.relevant}；{text}。仅为瞬时路径观察，不代表服务健康。"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON value")


class _DeadlineReader(io.RawIOBase):
    """Poll recv with a short timeout without losing partially buffered HTTP data.

    Retain the original makefile as an ownership reference: HTTPConnection closes
    its socket after a Connection:close header while HTTPResponse still reads it.
    Do not read through that makefile, whose timeout state cannot be safely retried.
    """

    def __init__(self, sock, ownership_reference, cancel, deadline):
        super().__init__()
        self._sock = sock
        self._ownership_reference = ownership_reference
        self._cancel = cancel
        self._deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            _check_cancel(self._cancel)
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise ApiError("controller", "本机控制接口响应超时；不计为节点故障。")
            self._sock.settimeout(min(0.1, remaining))
            try:
                count = self._sock.recv_into(buffer)
                _check_cancel(self._cancel)
                return count
            except socket.timeout:
                continue

    def close(self):
        try:
            self._ownership_reference.close()
        finally:
            super().close()


class _DeadlineResponse(http.client.HTTPResponse):
    def __init__(self, sock, *, cancel=None, deadline=0.0, **kwargs):
        super().__init__(sock, **kwargs)
        self.fp = io.BufferedReader(_DeadlineReader(sock, self.fp, cancel, deadline))


def _parse_node(data: object, name: str) -> ProxyNode:
    if not isinstance(data, dict) or data.get("name", name) != name or not _valid_name(name):
        raise ApiError("response", "节点名称或节点资料无效。")
    kind = data.get("type")
    if not isinstance(kind, str) or not kind:
        raise ApiError("response", "节点类型字段无效。")
    now = data.get("now")
    if now is not None and not isinstance(now, str):
        raise ApiError("response", "代理组当前选择字段无效。")
    members = data.get("all", [])
    if not isinstance(members, list) or not all(_valid_name(x) for x in members):
        raise ApiError("response", "代理组成员列表无效。")
    history = data.get("history")
    if history is None:
        history = []
    if not isinstance(history, list):
        raise ApiError("response", "延迟历史字段无效。")
    records = []
    previous = float("-inf")
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get("time"), str) or not _delay(entry.get("delay")):
            raise ApiError("response", "延迟历史记录无效。")
        try:
            parsed = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            stamp = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            raise ApiError("response", "延迟历史时间无效。") from None
        if stamp < previous:
            raise ApiError("response", "延迟历史记录顺序无效。")
        previous = stamp
        records.append(HistoryRecord(entry["time"], stamp, entry["delay"]))
    return ProxyNode(name, kind, now, tuple(members), tuple(records), "all" in data or kind.lower() in GROUP_TYPES)


class ClashAPI:
    def __init__(self, controller_url: str, secret: str = ""):
        if not isinstance(controller_url, str) or any(ord(c) < 32 for c in controller_url):
            raise ApiError("setup", "控制地址无效。")
        self._socket_path = None
        if controller_url.startswith("unix://"):
            path = Path(controller_url[7:])
            if not path.is_absolute() or path.name != "verge-mihomo.sock":
                raise ApiError("setup", "请选择 Clash Verge 的本机 Unix 套接字。")
            self._socket_path = str(path)
            controller_url = "http://localhost"
            secret = ""
        try:
            parsed = urlsplit(controller_url.strip())
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ApiError("setup", "控制地址或端口无效。") from None
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ApiError("setup", "控制地址应为本机 HTTP 地址，例如 http://127.0.0.1:9097。")
        if host not in {"127.0.0.1", "::1", "localhost"} or port == 0:
            raise ApiError("setup", "只允许 127.0.0.1、localhost 或 [::1] 上的本机控制接口。")
        if not isinstance(secret, str) or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            raise ApiError("setup", "密钥不能含有控制字符。")
        try:
            secret.encode("latin-1")
        except UnicodeEncodeError:
            raise ApiError("setup", "密钥包含 HTTP 请求头不支持的字符，请使用随机英文字母和数字密钥。") from None
        self._scheme = parsed.scheme
        # Do not resolve localhost via hosts/DNS: the Bearer token stays on loopback.
        self._host = "127.0.0.1" if host == "localhost" else host
        self._port = port or (443 if self._scheme == "https" else 80)
        bracket_host = "[::1]" if self._host == "::1" else self._host
        self.controller_url = f"{self._scheme}://{bracket_host}:{self._port}"
        if self._socket_path:
            self.controller_url = "unix://" + self._socket_path
        self._secret = secret

    @staticmethod
    def validate_test_url(value: str) -> str:
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise ApiError("setup", "测速地址必须为 HTTPS 地址。")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ApiError("setup", "测速地址或端口无效。") from None
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or port == 0:
            raise ApiError("setup", "测速地址必须为不含登录信息的 HTTPS 地址。")
        return value

    def _endpoint_label(self, value: str) -> str:
        parts = urlsplit(value)
        label = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if parts.query:
            label += "?[query omitted]"
        if self._secret:
            for representation in (self._secret, quote(self._secret, safe=""), base64.b64encode(self._secret.encode("utf-8")).decode("ascii")):
                label = label.replace(representation, "[redacted]")
        return label[:400]

    @staticmethod
    def _status_error(status: int, is_delay: bool) -> ApiError:
        if status in {401, 403}:
            return ApiError("setup", "控制接口拒绝访问，请检查密钥。", status)
        if status == 404:
            return ApiError("selection", "接口或代理节点不存在，请重新加载。", status)
        if is_delay and status in {408, 503, 504}:
            return ApiError("health", "节点延迟测试失败或超时。", status)
        if 300 <= status < 400:
            return ApiError("setup", "控制接口返回重定向，已拒绝跟随以保护密钥。", status)
        if 400 <= status < 500:
            return ApiError("setup", f"控制接口拒绝请求（HTTP {status}）。", status)
        return ApiError("controller", f"控制接口异常（HTTP {status}）。", status)

    def _request(self, path: str, *, timeout: float = 5.0, cancel: threading.Event | None = None,
                 is_delay: bool = False, max_bytes: int = MAX_RESPONSE_BYTES,
                 request_id: str = "", endpoint: str = "") -> tuple[dict, int, int]:
        _check_cancel(cancel)
        started = time.monotonic()
        deadline = started + timeout
        request_id = request_id or "api:" + uuid4().hex
        endpoint = endpoint or (path if path in {"/version", "/configs", "/connections", "/proxies"} else "/proxies/[node]")
        connection = (http.client.HTTPSConnection(self._host, self._port, timeout=timeout, context=ssl.create_default_context())
                      if self._scheme == "https" else http.client.HTTPConnection(self._host, self._port, timeout=timeout))
        connection.response_class = lambda sock, **kwargs: _DeadlineResponse(sock, cancel=cancel, deadline=deadline, **kwargs)
        status = None
        response = None
        try:
            if self._socket_path:
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.settimeout(timeout)
                connection.sock.connect(self._socket_path)
            else:
                connection.connect()
            _check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise ApiError("controller", "本机控制接口响应超时；不计为节点故障。")
            headers = {"Accept": "application/json", "Connection": "close", "User-Agent": "ClashVergeWatchdog/1.3.4-Ubuntu"}
            if self._secret:
                headers["Authorization"] = "Bearer " + self._secret
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            status = response.status
            _check_cancel(cancel)
            if not 200 <= status < 300:
                # Never copy arbitrary error/config/connection bodies into diagnostics.
                raise self._status_error(status, is_delay)
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    length = int(content_length)
                except ValueError:
                    raise ApiError("response", "控制接口响应长度无效。") from None
                if length < 0 or length > max_bytes:
                    raise ApiError("response", "控制接口响应过大或长度无效。")
            chunks = bytearray()
            while True:
                _check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise ApiError("controller", "本机控制接口响应超时；不计为节点故障。")
                chunk = response.read1(min(8192, max_bytes + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise ApiError("response", "控制接口响应过大。")
            if response.length is not None and response.length > 0:
                raise ApiError("controller", "本机控制接口响应未完整传输；不计为节点故障。")
            _check_cancel(cancel)
            if time.monotonic() >= deadline:
                raise ApiError("controller", "本机控制接口响应超时；不计为节点故障。")
            try:
                data = json.loads(chunks.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            except (UnicodeError, ValueError, RecursionError):
                raise ApiError("response", "控制接口返回了无法解析的 JSON 数据。") from None
            if not isinstance(data, dict):
                raise ApiError("response", "控制接口响应不是 JSON 对象。")
            return data, status, round((time.monotonic() - started) * 1000)
        except ApiError as error:
            _check_cancel(cancel)
            error.status = error.status if error.status is not None else status
            error.request_id = request_id
            error.endpoint = endpoint
            error.request_ms = round((time.monotonic() - started) * 1000)
            raise
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            _check_cancel(cancel)
            error = ApiError("controller", "本机控制接口连接失败或响应超时；不计为节点故障。", status)
            error.request_id, error.endpoint = request_id, endpoint
            error.request_ms = round((time.monotonic() - started) * 1000)
            raise error from None
        finally:
            if response is not None:
                response.close()
            connection.close()

    def tun_enabled(self, cancel=None):
        data, _, _ = self._request("/configs", cancel=cancel)
        enabled = data.get("tun", {}).get("enable")
        if type(enabled) is not bool:
            raise ApiError("controller", "无法核实内核 TUN 状态")
        return enabled

    def get_version(self, cancel: threading.Event | None = None) -> str:
        data, _, _ = self._request("/version", cancel=cancel)
        value = data.get("version")
        if not isinstance(value, str) or not value.strip():
            raise ApiError("response", "版本响应无效。")
        return value

    def get_proxies(self, cancel: threading.Event | None = None) -> ProxySnapshot:
        data, _, _ = self._request("/proxies", cancel=cancel)
        raw = data.get("proxies")
        if not isinstance(raw, dict):
            raise ApiError("response", "代理列表响应缺少有效的 proxies 字段。")
        nodes = {}
        for name, node in raw.items():
            _check_cancel(cancel)
            nodes[name] = _parse_node(node, name)
        return ProxySnapshot(nodes)

    def get_proxy(self, name: str, cancel: threading.Event | None = None) -> ProxyNode:
        if not _valid_name(name):
            raise ApiError("selection", "节点名称无效。")
        data, _, _ = self._request("/proxies/" + quote(name, safe=""), cancel=cancel)
        return _parse_node(data, name)

    def probe(self, leaf: str, test_url: str = DEFAULT_TEST_URL, timeout_ms: int = 5000,
              cancel: threading.Event | None = None) -> Probe:
        if not _valid_name(leaf):
            raise ApiError("selection", "节点名称无效。")
        self.validate_test_url(test_url)
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
            raise ApiError("setup", "测速超时必须在 100 至 30000 毫秒之间。")
        started = time.time()
        request_id = "probe:" + uuid4().hex
        endpoint = self._endpoint_label(test_url)
        path = "/proxies/" + quote(leaf, safe="") + "/delay?" + urlencode({"url": test_url, "timeout": timeout_ms})
        try:
            data, status, elapsed = self._request(path, timeout=(timeout_ms + 3000) / 1000,
                                                  cancel=cancel, is_delay=True, request_id=request_id, endpoint=endpoint)
        except ApiError as error:
            if error.kind != "health":
                raise
            return Probe(False, None, started, time.time(), request_id, endpoint, error.status, "health", str(error), error.request_ms)
        value = data.get("delay")
        valid = _delay(value)
        success = valid and value > 0
        return Probe(success, value if valid else None, started, time.time(), request_id, endpoint, status,
                     None if success else "health", None if success else "测速未返回有效的正整数延迟。", elapsed)

    def diagnose(self, leaf: str, primary: str = DEFAULT_TEST_URL, timeout_ms: int = 5000,
                 cancel: threading.Event | None = None,
                 on_probe: Callable[[Probe, int], None] | None = None) -> DiagnosticRound:
        self.validate_test_url(primary)
        started = time.time()
        round_id = "round:" + uuid4().hex
        first = self.probe(leaf, primary, timeout_ms, cancel)
        if on_probe:
            on_probe(first, 0)
        _check_cancel(cancel)
        probes = [first]
        if not first.success:
            backup = DEFAULT_TEST_URL if urlsplit(primary).hostname == urlsplit(BACKUP_TEST_URL).hostname else BACKUP_TEST_URL
            second = self.probe(leaf, backup, timeout_ms, cancel)
            probes.append(second)
            if on_probe:
                on_probe(second, 1)
            _check_cancel(cancel)
        result = probes[-1]
        return DiagnosticRound(round_id, tuple(probes), result.success, len(probes) == 2 and result.success,
                               result.delay_ms if result.success else None, started, time.time())

    def route_snapshot(self, chain: tuple[str, ...] | list[str], cancel: threading.Event | None = None,
                       timeout: float = 2.0) -> RouteSnapshot:
        if not chain or len(chain) > 64 or not all(_valid_name(x) for x in chain):
            raise ApiError("selection", "路由观察需要明确的监控选择链。")
        if not 0.1 <= timeout <= 10:
            raise ApiError("setup", "路由观察时间预算必须在 0.1 至 10 秒之间。")
        started = time.monotonic()
        config, _, _ = self._request("/configs", timeout=timeout, cancel=cancel, max_bytes=1024 * 1024)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ApiError("controller", "路由观察已达到时间预算。")
        data, _, _ = self._request("/connections", timeout=remaining, cancel=cancel, max_bytes=1024 * 1024)
        mode = config.get("mode", "unknown")
        mode = mode.lower() if isinstance(mode, str) and mode.lower() in {"rule", "global", "direct"} else "unknown"
        entries = data.get("connections")
        if "connections" in data and entries is None:
            entries = []
        complete = isinstance(entries, list)
        relevant = matching = different = unknown = unclassified = 0
        for entry in entries if complete else []:
            _check_cancel(cancel)
            metadata = entry.get("metadata") if isinstance(entry, dict) else None
            if not isinstance(metadata, dict):
                unclassified += 1
                complete = False
                continue
            process = metadata.get("process") or metadata.get("processPath") or ""
            process = process.replace("\\", "/").rsplit("/", 1)[-1].lower() if isinstance(process, str) else ""
            relevant_host = any(_relevant_host(metadata.get(key)) for key in ("host", "sniffHost"))
            if not relevant_host and process not in {"codex", "codex.exe", "chatgpt", "chatgpt.exe"}:
                continue
            relevant += 1
            route = entry.get("chains")
            if not isinstance(route, list) or not route or len(route) > 64 or not all(_valid_name(x) for x in route):
                unknown += 1
            elif route[0] == chain[-1]:
                matching += 1
            else:
                different += 1
        relationship = ("mixed" if matching and different else "unknown" if not complete or unknown else
                        "different" if different else "matching" if matching else "none")
        return RouteSnapshot(mode, relationship, relevant, matching, different, unknown, unclassified, complete,
                             time.time(), round((time.monotonic() - started) * 1000))


def _relevant_host(value: object) -> bool:
    if not isinstance(value, str):
        return False
    host = value.rstrip(".").lower()
    if len(host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or ".." in host:
        return False
    return any(host == domain or host.endswith("." + domain)
               for domain in ("openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com"))


# Alternative conventional spelling, useful to embedding callers.
ClashApi = ClashAPI
