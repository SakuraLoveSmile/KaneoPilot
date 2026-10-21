"""Actual TLS + independent stdin workers, using only loopback and a temp DB."""
import base64
import hashlib
from http import client as http_client
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from kanpilot import cli, db, mobile_access as mobile, mobile_server, util
from test_mobile_access import mobile_env, enable, pairing, pair, request, query, run, make_review


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def https_server(mobile_env):
    run(mobile_env, "set-mobile-access", {"enabled": True, "bind_host": "127.0.0.1", "port": available_port()})
    server = mobile_server.MobileServer(mobile_env)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    content = pairing(mobile_env)
    cert = ssl.DER_cert_to_PEM_cert(base64.b64decode(content["certificate_der"]))
    context = ssl.create_default_context(cadata=cert)
    try:
        yield server, content, context
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def http(server, context, method, path, body=None, token=None):
    connection = http_client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def http_pair(server, context, content):
    status, phone = http(server, context, "POST", "/mobile/v1/pair", {"pairing_token": content["pairing_token"], "device_name": "TLS 手机"})
    assert status == 200
    return phone


def test_https_listener_and_requests_do_not_require_reverse_dns(monkeypatch, request):
    def unavailable_reverse_dns(*args, **kwargs):
        raise AssertionError("mobile listener must not perform reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", unavailable_reverse_dns)
    monkeypatch.setattr(socket, "gethostbyaddr", unavailable_reverse_dns)
    # Apply resolver restrictions before the real TLS fixture binds its socket.
    server, content, context = request.getfixturevalue("https_server")
    assert server.server_name == "127.0.0.1"
    assert server.server_port == server.socket.getsockname()[1]
    phone = http_pair(server, context, content)
    status, value = http(server, context, "POST", "/mobile/v1/query",
                         {"type": "overview", "args": {}}, phone["device_token"])
    assert status == 200 and value["data"]["paused"]["global"] is False


def test_certificate_san_permissions_and_hash(https_server, mobile_env):
    server, content, context = https_server
    assert content["certificate_sha256"] == hashlib.sha256(base64.b64decode(content["certificate_der"])).hexdigest()
    conn = db.connect(mobile_env)
    config = db.get_setting(conn, "mobile.config")
    conn.close()
    for key in ("key_file", "certificate_file"):
        path = Path(config[key])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
        with context.wrap_socket(sock, server_hostname="127.0.0.1") as secure:
            assert ("IP Address", "127.0.0.1") in secure.getpeercert()["subjectAltName"]
    with pytest.raises(ssl.SSLCertVerificationError):
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            context.wrap_socket(sock, server_hostname="wrong.example")
    with pytest.raises(ssl.SSLCertVerificationError):
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            ssl.create_default_context().wrap_socket(sock, server_hostname="127.0.0.1")


def test_https_pair_query_action_receipt_and_revoke(https_server, mobile_env):
    server, content, context = https_server
    phone = http_pair(server, context, content)
    token = phone["device_token"]
    status, value = http(server, context, "POST", "/mobile/v1/query", {"type": "overview", "args": {}}, token)
    assert status == 200 and value["data"]["paused"]["global"] is False
    assert "settings" not in value["data"]
    body = {"action": "set-paused", "operation_id": "https-1", "args": {"paused": True, "expected_paused": False}}
    status, value = http(server, context, "POST", "/mobile/v1/actions", body, token)
    assert status == 200 and value["status"] == "accepted"
    assert http(server, context, "POST", "/mobile/v1/actions", body, token)[1] == value
    assert http(server, context, "GET", "/mobile/v1/operations/https-1", token=token)[1] == value
    run(mobile_env, "revoke-mobile-device", {"device_id": phone["device_id"]})
    assert http(server, context, "POST", "/mobile/v1/query", {"type": "overview", "args": {}}, token)[0] == 401


def test_http_rejects_credentials_paths_sizes_and_content_types(https_server):
    server, content, context = https_server
    phone = http_pair(server, context, content)
    token = phone["device_token"]
    assert http(server, context, "POST", "/mobile/v1/query", {"type": "overview", "args": {}})[0] == 401
    assert http(server, context, "POST", "/mobile/v1/query?type=doctor", {}, token)[0] == 404
    assert http(server, context, "POST", "/mobile/v1/query", {"type": "overview", "args": {"path": "/local"}}, token)[0] == 400
    assert http(server, context, "POST", "/mobile/v1/query", {"type": "migrate", "args": {}}, token)[0] == 403
    connection = http_client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=10)
    connection.request("POST", "/mobile/v1/query", "{}", {"Authorization": "Bearer " + token,
                        "Content-Type": "application/json", "Content-Length": str(1024 * 1024 + 1)})
    response = connection.getresponse()
    assert response.status == 413
    response.read()
    connection.close()


def test_response_disconnect_preserves_receipt_and_service_restart(https_server, mobile_env):
    server, content, context = https_server
    phone = http_pair(server, context, content)
    token = phone["device_token"]
    body = json.dumps({"action": "set-paused", "operation_id": "drop-1", "args": {"paused": True, "expected_paused": False}}).encode()
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
        with context.wrap_socket(sock, server_hostname="127.0.0.1") as secure:
            secure.sendall(b"POST /mobile/v1/actions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAuthorization: Bearer "
                           + token.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            secure.recv(1)
    deadline = time.monotonic() + 5
    while True:
        status, receipt = http(server, context, "GET", "/mobile/v1/operations/drop-1", token=token)
        if status == 200 and receipt["status"] == "accepted":
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert request(mobile_env, "operation", {"operation_id": "drop-1"}, token) == receipt
    connection = db.connect(mobile_env)
    assert connection.execute("SELECT COUNT(*) FROM events WHERE kind='paused'").fetchone()[0] == 1
    connection.close()
    server.shutdown()
    server.server_close()
    restarted = mobile_server.MobileServer(mobile_env)
    thread = threading.Thread(target=restarted.serve_forever, daemon=True)
    thread.start()
    try:
        assert http(restarted, context, "GET", "/mobile/v1/operations/drop-1", token=token)[1] == receipt
        assert http(restarted, context, "POST", "/mobile/v1/query", {"type": "overview", "args": {}}, token)[1]["data"]["paused"]["global"] is True
    finally:
        restarted.shutdown()
        restarted.server_close()
        thread.join(timeout=5)


def test_worker_concurrency_bound_does_not_execute_or_echo_tokens(https_server):
    server, content, context = https_server
    for _ in range(4):
        assert server.worker_slots.acquire(False)
    try:
        assert server.worker({"kind": "query", "token": "private-token", "data": {}})["code"] == "unavailable"
    finally:
        for _ in range(4):
            server.worker_slots.release()


def test_stdin_worker_uses_fixed_database_and_does_not_create_missing(mobile_env):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["KANPILOT_DB"] = str(mobile_env.parent / "absent.db")
    process = subprocess.run([sys.executable, "-m", "kanpilot.mobile_worker"],
                             input=b'{"kind":"query","token":"SECRET","data":{"type":"overview","args":{}}}',
                             capture_output=True, env=env, timeout=10)
    assert json.loads(process.stdout)["code"] == "unavailable"
    assert b"SECRET" not in process.stdout + process.stderr
    assert not Path(env["KANPILOT_DB"]).exists()


def test_server_does_not_start_against_disabled_or_missing_database(mobile_env):
    with pytest.raises(util.KanpilotError):
        mobile_server.MobileServer(mobile_env)
    with pytest.raises(util.KanpilotError):
        mobile_server.MobileServer(mobile_env.with_name("absent.db"))
    assert not mobile_env.with_name("absent.db").exists()


def test_action_worker_timeout_is_unknown_and_token_never_enters_argv(https_server, monkeypatch):
    server, content, context = https_server
    called = {}
    class TimedOut:
        def __init__(self, argv, **kwargs):
            called.update(argv=argv, kwargs=kwargs)
        def communicate(self, value, timeout):
            called.update(stdin=json.loads(value), timeout=timeout)
            raise subprocess.TimeoutExpired(called["argv"], timeout)
        def terminate(self):
            called["terminated"] = True
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(mobile_server.subprocess, "Popen", TimedOut)
    payload = {"kind": "action", "token": "SENSITIVE", "data": {"action": "submit-execution-answers", "operation_id": "timeout-1", "args": {}}}
    result = server.worker(payload)
    assert result["status"] == "unknown" and result["operation_id"] == "timeout-1"
    assert result["code"] == "timeout" and called["timeout"] == 120
    assert called["terminated"] is True and called["stdin"] == payload
    assert "SENSITIVE" not in repr(called["argv"]) + repr(called["kwargs"])
    assert called["argv"] == [sys.executable, "-m", "kanpilot.mobile_worker"]
