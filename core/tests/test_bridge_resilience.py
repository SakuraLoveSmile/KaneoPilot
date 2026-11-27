"""Kaneo bridge 韧性（kp-nx01）：默认直连、幂等 GET 重试、连接层失败文案。"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from kanpilot import bridge


class _Response:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, size):
        chunk, self._raw = self._raw[:size], self._raw[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _Opener:
    """按顺序回放结果：异常则抛出，字典则当响应体返回。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.timeouts = []

    def open(self, request, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)


def _client(opener, timeout=10.0):
    client = bridge.Client.__new__(bridge.Client)
    client.base_url = "https://kaneo.example.test/"
    client.token = "stub-token"
    client.timeout = timeout
    client._opener = opener
    return client


class BridgeResilienceTest(unittest.TestCase):
    def setUp(self):
        # 退避只影响时序判断，不把测试打满真实秒数。
        sleep = patch.object(bridge.time, "sleep", lambda *_: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_get_retries_transport_blips_then_succeeds(self):
        opener = _Opener([URLError("timed out"), URLError("timed out"),
                          {"ok": True}])
        client = _client(opener)
        self.assertEqual(client.request("/api/task/x"), {"ok": True})
        self.assertEqual(opener.calls, 3)

    def test_mixed_read_and_write_are_not_retried(self):
        opener = _Opener([URLError("Connection reset by peer")])
        client = _client(opener)
        with self.assertRaises(bridge.BridgeError) as raised:
            client.request("/api/task/status/x", "PUT", {"status": "in-progress"})
        self.assertEqual(opener.calls, 1)
        self.assertIn("PUT /api/task/status/x", str(raised.exception))

    def test_exhausted_retries_report_reason_and_attempts(self):
        opener = _Opener([TimeoutError("timed out")])
        client = _client(opener)
        with self.assertRaises(bridge.BridgeError) as raised:
            client.request("/api/column/p?page=2")
        self.assertEqual(opener.calls, 3)
        message = str(raised.exception)
        self.assertIn("Kaneo 请求失败: TimeoutError(timed out)", message)
        self.assertIn("GET /api/column/p", message)
        self.assertIn("尝试 3 次", message)
        self.assertNotIn("page=2", message)

    def test_retry_can_be_disabled_per_call(self):
        opener = _Opener([URLError("down")])
        client = _client(opener)
        with self.assertRaises(bridge.BridgeError):
            client.request("/api/task/x", retry=0)
        self.assertEqual(opener.calls, 1)

    def test_get_uses_shorter_timeout_than_writes(self):
        opener = _Opener([{"ok": 1}])
        client = _client(opener, timeout=30.0)
        client.request("/api/task/x")
        client.request("/api/task/status/x", "PUT", {"status": "in-progress"})
        self.assertEqual(opener.timeouts[0], bridge.GET_TIMEOUT_CAP)
        self.assertEqual(opener.timeouts[1], 30.0)

    def test_lowered_timeout_after_construction_is_used(self):
        # doctor 探针会临时把 client.timeout 调小；不能再用构造时算好的旧值，
        # 否则「1 秒探针 × 3 次重试」会把超时语义放大（集成用例抓到的回归）。
        opener = _Opener([{"ok": 1}])
        client = _client(opener, timeout=10.0)
        client.timeout = 1.0
        client.request("/api/project?workspaceId=w")
        self.assertEqual(opener.timeouts[0], 1.0)

    def test_http_error_is_never_retried(self):
        opener = _Opener([HTTPError("u", 503, "busy", {}, None)])
        client = _client(opener)
        with self.assertRaisesRegex(bridge.BridgeError, "HTTP 503"):
            client.request("/api/task/x")
        self.assertEqual(opener.calls, 1)

    def _proxy_handler(self, env):
        with patch.dict(os.environ, env, clear=False), \
                patch.object(bridge.urllib.request, "build_opener",
                             return_value=None) as build:
            bridge._build_opener()
        return build.call_args.args[0]

    def test_inherited_proxy_env_is_ignored_by_default(self):
        handler = self._proxy_handler({
            "http_proxy": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890", "KANEO_PROXY": ""})
        self.assertIsInstance(handler, bridge.urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies, {})

    def test_explicit_proxy_is_used(self):
        handler = self._proxy_handler({"KANEO_PROXY": "http://127.0.0.1:7890"})
        self.assertEqual(handler.proxies, {"http": "http://127.0.0.1:7890",
                                           "https": "http://127.0.0.1:7890"})

    def test_invalid_proxy_is_rejected(self):
        with patch.dict(os.environ, {"KANEO_PROXY": "socks5://127.0.0.1:7890"}, clear=False):
            with self.assertRaisesRegex(bridge.BridgeError, "KANEO_PROXY"):
                bridge._build_opener()


if __name__ == "__main__":
    unittest.main()
