"""Bounded HTTPS transport. Every request delegates to a fresh core process."""
from __future__ import annotations
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

from . import cli, db, util
from .mobile_access import _config, require_existing_database
from .mobile_worker import MAX_REQUEST, MAX_RESPONSE

HTTP_STATUS = {"bad_request": 400, "unauthorized": 401, "forbidden": 403, "not_found": 404,
               "conflict": 409, "too_large": 413, "unavailable": 503, "timeout": 503}


class MobileServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, db_path):
        require_existing_database(db_path)
        ctx = cli.Context(Path(db_path))
        try:
            config = _config(ctx.conn)
        finally:
            ctx.close()
        if not config.get("enabled"):
            raise util.KanpilotError("unavailable", "手机连接尚未启用")
        self.db_path = Path(db_path).absolute()
        self.endpoint = config["endpoint"]
        self.worker_slots = threading.BoundedSemaphore(4)
        self.connection_slots = threading.BoundedSemaphore(8)
        self.children = set()
        self.children_lock = threading.Lock()
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            for key in ("certificate_file", "key_file"):
                path = Path(config[key])
                if path.is_symlink() or not path.is_file():
                    raise OSError("unsafe identity")
            self.tls.load_cert_chain(config["certificate_file"], config["key_file"])
            super().__init__((config["bind_host"], config["port"]), MobileHandler)
        except (OSError, KeyError):
            raise util.KanpilotError("unavailable", "手机连接启动失败：地址不可用、端口占用或证书不可读") from None

    def server_bind(self):
        TCPServer.server_bind(self)
        # The configured address is numeric IPv4; startup needs no reverse DNS.
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            # TLS handshake is bounded and happens in a worker thread so a slow
            # peer cannot block the accept loop or allocate unlimited threads.
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except (OSError, ssl.SSLError):
                self.shutdown_request(request)
                return
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def handle_error(self, request, client_address):
        # Default HTTPServer logs tracebacks (possibly containing request data).
        pass

    def server_close(self):
        super().server_close()
        with self.children_lock:
            children = list(self.children)
        for process in children:
            try:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def worker(self, payload):
        if not self.worker_slots.acquire(blocking=False):
            return {"error": "手机请求繁忙，请稍后重试查询", "code": "unavailable"}
        process = None
        action = payload.get("kind") == "action"
        timeout = 120 if action and payload.get("data", {}).get("action") == "submit-execution-answers" else 60
        try:
            require_existing_database(self.db_path)
            env = os.environ.copy()
            env["KANPILOT_DB"] = str(self.db_path)
            core_root = str(Path(__file__).resolve().parent.parent)
            # Fixed module, root, DB and argv. No bearer or payload in argv/env.
            env["PYTHONPATH"] = core_root
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen([sys.executable, "-m", "kanpilot.mobile_worker"],
                                           cwd=core_root, env=env, stdin=subprocess.PIPE,
                                           stdout=output, stderr=subprocess.DEVNULL)
                with self.children_lock:
                    self.children.add(process)
                try:
                    process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    return self._unknown(payload, "timeout") if action else {"error": "查询超时", "code": "timeout"}
                output.seek(0)
                raw = output.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    return self._unknown(payload, "too_large") if action else {"error": "响应超过大小限制", "code": "too_large"}
                try:
                    result = json.loads(raw.decode("utf-8"))
                    if not isinstance(result, dict):
                        raise ValueError()
                    return result
                except (ValueError, UnicodeDecodeError):
                    return self._unknown(payload, "unavailable") if action else {"error": "核心响应不可用", "code": "unavailable"}
        except (OSError, util.KanpilotError):
            return self._unknown(payload, "unavailable") if action else {"error": "手机服务不可用", "code": "unavailable"}
        finally:
            if process:
                with self.children_lock:
                    self.children.discard(process)
            self.worker_slots.release()

    @staticmethod
    def _unknown(payload, code):
        return {"api_version": 1, "operation_id": payload.get("data", {}).get("operation_id"),
                "status": "unknown", "error": "结果待核实，请查询原操作回执", "code": code}


class MobileHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KaneoPilot"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _send(self, value):
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        code = HTTP_STATUS.get(value.get("code"), 503) if "error" in value and "status" not in value else 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # Receipt persists; a disconnected client must query it.

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def _handle(self, method):
        try:
            paths = {"/mobile/v1/pair": "pair", "/mobile/v1/query": "query", "/mobile/v1/actions": "action"}
            if method == "POST" and self.path in paths:
                kind = paths[self.path]
            elif method == "GET" and re.fullmatch(r"/mobile/v1/operations/[A-Za-z0-9._:-]{1,128}", self.path):
                kind = "operation"
            else:
                return self._send({"error": "接口不存在", "code": "not_found"})
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                return self._send({"error": "不支持此请求编码", "code": "bad_request"})
            payload = {"kind": kind}
            if kind != "pair":
                auth = self.headers.get_all("Authorization", [])
                if len(auth) != 1 or not re.fullmatch(r"Bearer [A-Za-z0-9_-]{32,512}", auth[0]):
                    return self._send({"error": "缺少设备授权", "code": "unauthorized"})
                payload["token"] = auth[0][7:]
            if method == "POST":
                if self.headers.get_content_type() != "application/json":
                    return self._send({"error": "请使用 JSON 请求", "code": "bad_request"})
                length = self.headers.get("Content-Length", "")
                if not length.isdigit():
                    return self._send({"error": "缺少请求长度", "code": "bad_request"})
                length = int(length)
                if length > MAX_REQUEST:
                    return self._send({"error": "请求超过大小限制", "code": "too_large"})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self._send({"error": "请求不完整", "code": "bad_request"})
                payload["data"] = json.loads(raw.decode("utf-8"))
                if not isinstance(payload["data"], dict):
                    raise ValueError()
            else:
                if self.headers.get("Content-Length", "0") != "0":
                    raise ValueError()
                payload["data"] = {"operation_id": self.path.rsplit("/", 1)[1]}
            # A syntactically invalid operation ID must never be echoed as an
            # unknown receipt. Only identifiers representable in GET are valid.
            if kind == "action":
                operation = payload["data"].get("operation_id")
                if not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", operation):
                    raise ValueError()
            self._send(self.server.worker(payload))
        except (ValueError, UnicodeDecodeError):
            self._send({"error": "请求不是有效 JSON 或字段无效", "code": "bad_request"})
        except (OSError, TimeoutError):
            self.close_connection = True


def serve(db_path):
    server = MobileServer(db_path)
    stopped = threading.Event()
    previous = {}

    def stop(signum, frame):
        stopped.set()
    if threading.current_thread() is threading.main_thread():
        for name in (signal.SIGTERM, signal.SIGINT):
            previous[name] = signal.signal(name, stop)
    server.timeout = 0.5
    try:
        print(json.dumps({"ready": True, "endpoint": server.endpoint}), flush=True)
        while not stopped.is_set():
            server.handle_request()
    finally:
        server.server_close()
        for name, handler in previous.items():
            signal.signal(name, handler)
    return {"ok": True}
