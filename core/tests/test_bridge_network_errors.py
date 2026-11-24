"""Kaneo 传输失败必须保留底层原因：请求与附件下载都不再只写异常类名。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
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


class FailingOpener:
    """build_opener 的替身：open() 直接抛出给定的传输异常。"""

    def __init__(self, exc):
        self.exc = exc

    def open(self, request, timeout=None):
        raise self.exc


class TestAttachmentFailureMessages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        credentials = Path(self.tmp.name) / "credentials.json"
        credentials.write_text(json.dumps({
            "baseUrl": "http://127.0.0.1:1",
            "accessToken": "test-token",
        }), encoding="utf-8")
        env = patch.dict(os.environ, {"KANEO_CREDENTIALS_PATH": str(credentials)})
        env.start()
        self.addCleanup(env.stop)
        self.dest = Path(self.tmp.name) / "files" / "a.bin"

    def message_for(self, exc):
        with patch.object(urllib.request, "build_opener",
                          lambda *args, **kwargs: FailingOpener(exc)):
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.download_attachment(
                    "http://127.0.0.1:1/api/attachment/a", self.dest)
        return str(caught.exception)

    def test_connection_refused_keeps_errno_and_writes_nothing(self):
        text = self.message_for(URLError(OSError(61, "Connection refused")))
        self.assertEqual(text, "附件下载失败：URLError: [Errno 61] Connection refused")
        self.assertFalse(self.dest.exists())

    def test_empty_reason_adds_no_suffix(self):
        self.assertEqual(self.message_for(URLError("")), "附件下载失败：URLError")

    def test_timeout_name_without_reason(self):
        self.assertEqual(self.message_for(TimeoutError()),
                         "附件下载失败：TimeoutError")

    def test_http_status_message_unchanged(self):
        error = HTTPError("http://127.0.0.1:1/api/attachment/a", 403, "forbidden", {}, None)
        self.assertEqual(self.message_for(error), "附件下载失败：HTTP 403")

    def test_cross_origin_still_refused_without_network(self):
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.download_attachment("https://evil.invalid/a", self.dest)
        self.assertIn("附件地址跨源", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
