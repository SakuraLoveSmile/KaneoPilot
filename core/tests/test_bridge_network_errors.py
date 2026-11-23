"""Kaneo 请求传输失败必须保留底层原因（不再只写异常类名）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from kanpilot import bridge


def transport_client():
    """只构造传输层字段；不读凭据文件，也不真正联网。"""
    client = bridge.Client.__new__(bridge.Client)
    client.base_url = "http://127.0.0.1:1/"
    client.token = "test-token"
    client.timeout = 1
    return client


class TestTransportFailureMessages(unittest.TestCase):
    def message_for(self, exc):
        with patch.object(bridge, "urlopen", side_effect=exc):
            with self.assertRaises(bridge.BridgeError) as caught:
                transport_client().request("/api/task/t1")
        return str(caught.exception)

    def test_connection_refused_keeps_errno(self):
        text = self.message_for(URLError(OSError(61, "Connection refused")))
        self.assertEqual(text, "Kaneo 请求失败: URLError: [Errno 61] Connection refused")

    def test_tls_failure_keeps_reason(self):
        reason = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)")
        text = self.message_for(URLError(reason))
        self.assertEqual(text, "Kaneo 请求失败: URLError: " + str(reason))

    def test_proxy_or_connect_timeout_text(self):
        text = self.message_for(URLError("timed out"))
        self.assertEqual(text, "Kaneo 请求失败: URLError: timed out")

    def test_bare_timeout_and_empty_reason_add_no_suffix(self):
        self.assertEqual(self.message_for(TimeoutError()), "Kaneo 请求失败: TimeoutError")
        self.assertEqual(self.message_for(URLError("")), "Kaneo 请求失败: URLError")

    def test_detail_is_bounded_to_200_chars(self):
        text = self.message_for(URLError(OSError("x" * 500)))
        self.assertEqual(text, "Kaneo 请求失败: URLError: " + "x" * 200)

    def test_message_carries_no_credential(self):
        text = self.message_for(URLError(OSError(61, "Connection refused")))
        self.assertNotIn("test-token", text)

    def test_http_status_messages_unchanged(self):
        for code, expected in ((503, "Kaneo 请求失败: HTTP 503"),
                               (401, "Kaneo 登录已失效，请刷新 Kaneo 登录凭据")):
            with self.subTest(code=code):
                error = HTTPError("http://127.0.0.1:1/api/task/t1", code, "x", {}, None)
                self.assertEqual(self.message_for(error), expected)


if __name__ == "__main__":
    unittest.main()
