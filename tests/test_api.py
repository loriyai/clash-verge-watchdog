"""Loopback-only HTTP fixtures; no Clash process, remote website or secret store."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from clash_watchdog.api import (ApiError, BACKUP_TEST_URL, Cancelled, ClashAPI,
                                DEFAULT_TEST_URL, ProxyNode, ProxySnapshot)


@contextmanager
def server(*replies):
    records = []
    response_queue = list(replies)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            records.append((self.path, dict(self.headers)))
            response = response_queue.pop(0) if response_queue else {"status": 500, "body": {"error": "unexpected request"}}
            try:
                if response.get("header_delay"):
                    time.sleep(response["header_delay"])
                self.send_response(response.get("status", 200))
                body = response.get("body", {})
                body = json.dumps(body).encode("utf-8") if not isinstance(body, (bytes, str)) else body.encode("utf-8") if isinstance(body, str) else body
                for key, value in response.get("headers", {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", response.get("content_length", str(len(body))))
                self.end_headers()
                if response.get("drip"):
                    for char in body:
                        self.wfile.write(bytes([char]))
                        self.wfile.flush()
                        time.sleep(response["drip"])
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    host = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host.daemon_threads = True
    worker = threading.Thread(target=lambda: host.serve_forever(poll_interval=0.02), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{host.server_port}", records
    finally:
        host.shutdown()
        host.server_close()
        worker.join(1)


def proxies(**changes):
    data = {"proxies": {
        "组": {"name": "组", "type": "Selector", "now": "child", "all": ["child"]},
        "child": {"name": "child", "type": "URLTest", "now": "节点", "all": ["节点"]},
        "节点": {"name": "节点", "type": "Shadowsocks", "history": []},
    }}
    for name, value in changes.items():
        data["proxies"][name] = value
    return data


class ControllerValidationTests(unittest.TestCase):
    def test_exact_loopback_only(self):
        for address in ["http://192.168.1.1:9090", "http://127.0.0.2:9090", "http://2130706433:9090",
                        "http://127.1:9090", "http://localhost.evil:9090", "http://127.0.0.1.:9090",
                        "file:///tmp/socket", "http://[::1%eth0]:9090"]:
            with self.subTest(address=address), self.assertRaises(ApiError) as context:
                ClashAPI(address)
            self.assertEqual(context.exception.kind, "setup")

    def test_credentials_path_fragment_query_and_bad_ports_rejected(self):
        for address in ["http://x@127.0.0.1:9090", "http://127.0.0.1:9090/api", "http://127.0.0.1:9090?x=y",
                        "http://127.0.0.1/#token", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:\n9090"]:
            with self.subTest(address=address), self.assertRaises(ApiError):
                ClashAPI(address)

    def test_localhost_is_pinned_and_ipv6_supported(self):
        self.assertEqual(ClashAPI("http://localhost:9097").controller_url, "http://127.0.0.1:9097")
        self.assertEqual(ClashAPI("http://[::1]:9097").controller_url, "http://[::1]:9097")
        self.assertEqual(ClashAPI("https://127.0.0.1").controller_url, "https://127.0.0.1:443")

    def test_header_injection_is_rejected_without_echoing_secret(self):
        for token in ["private\r\nX: 1", "private\0data", "private\tdata", "private\x7fdata"]:
            with self.subTest(token=repr(token)), self.assertRaises(ApiError) as context:
                ClashAPI("http://127.0.0.1", token)
            self.assertNotIn("private", str(context.exception))

    def test_no_environment_proxy_and_auth_is_local_only(self):
        with server({"body": {"version": "1.19.0"}}) as (url, records):
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                self.assertEqual(ClashAPI(url, "fixture-token").get_version(), "1.19.0")
            self.assertEqual(records[0][1]["Authorization"], "Bearer fixture-token")
            self.assertEqual(records[0][0], "/version")

    def test_redirect_never_follows_or_forwards_secret(self):
        with server({"body": {"version": "wrong"}}) as (destination, destination_records):
            with server({"status": 302, "headers": {"Location": destination + "/version"}}) as (url, records):
                with self.assertRaises(ApiError) as context:
                    ClashAPI(url, "fixture-token").get_version()
                self.assertEqual(context.exception.kind, "setup")
                self.assertEqual(len(records), 1)
                self.assertEqual(destination_records, [])


class ResponseTests(unittest.TestCase):
    def test_malformed_json_is_unknown_not_node_failure(self):
        for body in [b"{broken", b"[]", b"\xff", b'{"delay":NaN}', b'{"delay":1,"delay":2}']:
            with self.subTest(body=body), server({"body": body}) as (url, _):
                with self.assertRaises(ApiError) as context:
                    ClashAPI(url).probe("node")
                self.assertEqual(context.exception.kind, "response")

    def test_oversized_reply_rejected_before_reading_body(self):
        with server({"body": {}, "content_length": str(9 * 1024 * 1024)}) as (url, _):
            with self.assertRaises(ApiError) as context:
                ClashAPI(url).get_version()
            self.assertEqual(context.exception.kind, "response")

    def test_transport_disconnect_is_controller_error(self):
        with server({"body": b'{"version":', "content_length": "200"}) as (url, _):
            with self.assertRaises(ApiError) as context:
                ClashAPI(url).get_version()
            self.assertIn(context.exception.kind, {"controller", "response"})

    def test_truncated_transfer_cannot_pass_as_healthy_even_if_json_is_complete(self):
        with server({"body": {"delay": 25}, "content_length": "200"}) as (url, _):
            with self.assertRaises(ApiError) as context:
                ClashAPI(url).probe("node")
            self.assertEqual(context.exception.kind, "controller")

    def test_auth_and_missing_node_are_configuration_faults(self):
        for status, kind in [(401, "setup"), (403, "setup"), (404, "selection"), (400, "setup"), (500, "controller")]:
            with self.subTest(status=status), server({"status": status, "body": {"error": "private-secret"}}) as (url, _):
                with self.assertRaises(ApiError) as context:
                    ClashAPI(url, "private-secret").probe("node")
                self.assertEqual(context.exception.kind, kind)
                self.assertEqual(context.exception.status, status)
                self.assertNotIn("private-secret", str(context.exception))
                self.assertTrue(context.exception.request_id.startswith("probe:"))

    def test_timeout_status_only_counts_on_delay_endpoint(self):
        for status in [408, 503, 504]:
            with self.subTest(status=status), server({"status": status}, {"status": status}) as (url, _):
                api = ClashAPI(url)
                result = api.probe("node")
                self.assertFalse(result.success)
                self.assertEqual(result.error_kind, "health")
                self.assertEqual(result.http_status, status)
                with self.assertRaises(ApiError) as context:
                    api.get_proxies()
                self.assertNotEqual(context.exception.kind, "health")

    def test_cancellation_before_request_sends_nothing(self):
        cancel = threading.Event()
        cancel.set()
        with server({"body": {"version": "x"}}) as (url, records):
            with self.assertRaises(Cancelled):
                ClashAPI(url).get_version(cancel)
            self.assertEqual(records, [])

    def test_cancellation_interrupts_blocked_response(self):
        with server({"body": {"version": "x"}, "header_delay": 1.0}) as (url, _):
            cancel = threading.Event()
            timer = threading.Timer(0.12, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(Cancelled):
                    ClashAPI(url).get_version(cancel)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 0.8)

    def test_total_budget_interrupts_slow_drip(self):
        with server({"body": {"version": "slow"}, "drip": 0.07}) as (url, _):
            started = time.monotonic()
            with self.assertRaises(ApiError) as context:
                ClashAPI(url)._request("/version", timeout=0.18)
            self.assertEqual(context.exception.kind, "controller")
            self.assertLess(time.monotonic() - started, 0.7)


class SelectionTests(unittest.TestCase):
    def test_nested_selected_chain_and_history_remain_reference_only(self):
        data = proxies()
        data["proxies"]["节点"]["history"] = [{"time": "2026-11-14T10:20:30.123456789Z", "delay": 0}]
        with server({"body": data}) as (url, _):
            snapshot = ClashAPI(url).get_proxies()
        selection = snapshot.resolve("组")
        self.assertEqual(selection.chain, ("组", "child", "节点"))
        self.assertEqual(selection.leaf, "节点")
        self.assertEqual(json.loads(selection.target_key), list(selection.chain))
        self.assertEqual(snapshot.nodes["节点"].latest_history.delay_ms, 0)
        self.assertTrue(snapshot.nodes["节点"].latest_history.timestamp.endswith("123456789Z"))

    def test_unknown_current_member_and_cycles_rejected(self):
        for data in [proxies(child={"type": "Selector", "now": "missing", "all": ["missing"]}),
                     proxies(child={"type": "Selector", "now": "组", "all": ["组"]}),
                     proxies(child={"type": "Selector", "now": "节点", "all": ["different"]}),
                     proxies(child={"type": "Selector", "all": []})]:
            with self.subTest(data=data), server({"body": data}) as (url, _):
                with self.assertRaises(ApiError) as context:
                    ClashAPI(url).get_proxies().resolve("组")
                self.assertEqual(context.exception.kind, "selection")

    def test_groups_without_unique_exit_and_direct_rejected(self):
        for kind in ["LoadBalance", "load-balance", "Relay", "Direct", "Reject", "RejectDrop"]:
            with self.subTest(kind=kind):
                node = ProxyNode("node", kind)
                with self.assertRaises(ApiError):
                    ProxySnapshot({"node": node}).resolve("node")

    def test_json_target_key_does_not_collide_on_separator_names(self):
        one = ProxySnapshot({"a→b": ProxyNode("a→b", "Selector", "c", ("c",), is_group=True), "c": ProxyNode("c", "ss")}).resolve("a→b")
        two = ProxySnapshot({"a": ProxyNode("a", "Selector", "b→c", ("b→c",), is_group=True), "b→c": ProxyNode("b→c", "ss")}).resolve("a")
        self.assertNotEqual(one.target_key, two.target_key)

    def test_invalid_history_and_mismatched_node_names_rejected(self):
        invalid = [dict(type="ss", name="different"), dict(type="ss", history=False),
                   dict(type="ss", history=[{"time": "today", "delay": 1}]),
                   dict(type="ss", history=[{"time": "2026-01-01Z", "delay": True}])]
        for node in invalid:
            with self.subTest(node=node), server({"body": {"proxies": {"node": node}}}) as (url, _):
                with self.assertRaises(ApiError) as context:
                    ClashAPI(url).get_proxies()
                self.assertEqual(context.exception.kind, "response")


class ProbeTests(unittest.TestCase):
    def test_positive_integer_only_and_zero_is_failed_reading(self):
        for value, expected in [(1, True), (65535, True), (0, False), (65536, False), (-1, False),
                                (True, False), (1.5, False), ("12", False), ("err", False), ("timeout", False), (None, False)]:
            with self.subTest(value=value), server({"body": {"delay": value}}) as (url, _):
                result = ClashAPI(url).probe("node")
                self.assertEqual(result.success, expected)
                self.assertEqual(result.http_status, 200)

    def test_node_err_and_timeout_are_health_failures(self):
        for status in (503, 504):
            with self.subTest(status=status), server({"status": status, "body": {"message": "err"}}) as (url, _):
                result = ClashAPI(url).probe("node")
                self.assertFalse(result.success)
                self.assertEqual("health", result.error_kind)

    def test_missing_delay_is_completed_failed_reading(self):
        with server({"body": {"message": "timeout"}}) as (url, _):
            result = ClashAPI(url).probe("node")
        self.assertFalse(result.success)
        self.assertEqual(result.error_kind, "health")

    def test_encoded_leaf_and_test_query_do_not_change_route(self):
        leaf = "香港 / A? #🚀"
        target = "https://www.gstatic.com/generate_204?token=private&x=2"
        with server({"body": {"delay": 37}}) as (url, records):
            result = ClashAPI(url).probe(leaf, target)
        path = urlsplit(records[0][0])
        self.assertEqual(path.path, "/proxies/" + quote(leaf, safe="") + "/delay")
        self.assertEqual(parse_qs(path.query), {"url": [target], "timeout": ["5000"]})
        self.assertNotIn("private", result.endpoint)
        self.assertNotIn("x=2", result.endpoint)
        self.assertTrue(result.endpoint.endswith("?[query omitted]"))

    def test_invalid_test_urls_and_timeout_rejected_before_network(self):
        api = ClashAPI("http://127.0.0.1:1")
        for target in ["http://example.com/", "https://user:pass@example.com/", "https://example.com/#x", "https://example.com/\n"]:
            with self.subTest(target=target), self.assertRaises(ApiError) as context:
                api.probe("node", target)
            self.assertEqual(context.exception.kind, "setup")
        for budget in [True, 99, 30001, 100.5]:
            with self.subTest(budget=budget), self.assertRaises(ApiError):
                api.probe("node", timeout_ms=budget)

    def test_primary_success_does_not_test_backup(self):
        with server({"body": {"delay": 43}}) as (url, records):
            result = ClashAPI(url).diagnose("node")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(result.probes), 1)
        self.assertTrue(result.success)
        self.assertFalse(result.partial)

    def test_primary_failed_backup_success_is_partial_one_round(self):
        with server({"status": 504}, {"body": {"delay": 85}}) as (url, records):
            observed = []
            result = ClashAPI(url).diagnose("node", on_probe=lambda probe, index: observed.append((probe.success, index)))
        self.assertTrue(result.success)
        self.assertTrue(result.partial)
        self.assertEqual(result.delay_ms, 85)
        self.assertEqual(observed, [(False, 0), (True, 1)])
        self.assertEqual(parse_qs(urlsplit(records[1][0]).query)["url"], [BACKUP_TEST_URL])

    def test_both_fail_one_round_and_backup_is_independent_host(self):
        with server({"status": 504}, {"status": 504}) as (url, records):
            result = ClashAPI(url).diagnose("node", primary=BACKUP_TEST_URL)
        self.assertFalse(result.success)
        self.assertFalse(result.partial)
        self.assertEqual(len(result.probes), 2)
        self.assertEqual(parse_qs(urlsplit(records[1][0]).query)["url"], [DEFAULT_TEST_URL])

    def test_backup_controller_failure_does_not_fabricate_failed_round(self):
        with server({"status": 504}, {"body": "not-json"}) as (url, _):
            observed = []
            with self.assertRaises(ApiError) as context:
                ClashAPI(url).diagnose("node", on_probe=lambda probe, index: observed.append(index))
        self.assertEqual(context.exception.kind, "response")
        self.assertEqual(observed, [0])

    def test_cancel_after_primary_prevents_backup(self):
        with server({"status": 504}) as (url, records):
            cancel = threading.Event()
            with self.assertRaises(Cancelled):
                ClashAPI(url).diagnose("node", cancel=cancel, on_probe=lambda probe, index: cancel.set())
            self.assertEqual(len(records), 1)


class RouteTests(unittest.TestCase):
    def test_route_compares_effective_first_chain_entry(self):
        data = {"connections": [
            {"metadata": {"host": "chatgpt.com"}, "chains": ["other", "node"], "rule": "Domain"},
            {"metadata": {"processPath": "/opt/Codex/codex"}, "chains": ["node", "group"]},
            {"metadata": {"host": "evilchatgpt.com"}, "chains": ["node"]},
        ]}
        with server({"body": {"mode": "rule"}}, {"body": data}) as (url, _):
            result = ClashAPI(url).route_snapshot(("group", "node"))
        self.assertEqual(result.relationship, "mixed")
        self.assertEqual((result.relevant, result.matching, result.different), (2, 1, 1))
        self.assertIn("瞬时", result.summary)

    def test_null_connections_means_empty_not_fault(self):
        with server({"body": {"mode": "global"}}, {"body": {"connections": None}}) as (url, _):
            result = ClashAPI(url).route_snapshot(("group", "node"))
        self.assertTrue(result.complete)
        self.assertEqual(result.relationship, "none")

    def test_incomplete_connection_data_never_claims_matching(self):
        with server({"body": {}}, {"body": {"connections": [{"metadata": {"host": "api.openai.com"}, "chains": []}, {}]}}) as (url, _):
            result = ClashAPI(url).route_snapshot(("node",))
        self.assertEqual(result.relationship, "unknown")
        self.assertFalse(result.complete)
        self.assertEqual(result.unknown, 1)
        self.assertEqual(result.unclassified, 1)


if __name__ == "__main__":
    unittest.main()
