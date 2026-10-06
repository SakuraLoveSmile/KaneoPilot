"""KaneoPilot Standard MCP Server (protocol §5.25 / T7.4).

Provides standard MCP 2025-06-18 loopback HTTP service at 127.0.0.1:<port>/mcp
and administrative scope management at /admin/scopes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from . import constants, db, mcp_scopes, mcp_tools, util

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB body limit


class MCPRequestHandler(BaseHTTPRequestHandler):
    server: "MCPHTTPServer"

    # Suppress default log messages to stdout; keep stdout clean for protocol
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, status: int, data: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            data = {"jsonrpc": "2.0", "id": data.get("id") if isinstance(data, dict) else None,
                    "error": {"code": -32000, "message": "MCP_RESULT_LIMIT: response exceeds 2 MiB"}}
            body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_jsonrpc(self, code: int, message: str, req_id: Any = None, status: int = 200) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message,
            },
        }
        self._send_json(status, payload)

    def _validate_host_origin(self) -> bool:
        expected = self.server.authority.lower()
        host = self.headers.get("Host", "").strip().lower()
        if host != expected:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid Host header", "code": "forbidden"})
            return False

        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            try:
                origin_authority = parsed.netloc.lower()
                valid_origin = (
                    parsed.scheme == "http"
                    and origin_authority == expected
                    and parsed.path == ""
                    and not parsed.query
                    and not parsed.fragment
                    and parsed.username is None
                    and parsed.password is None
                )
            except ValueError:
                valid_origin = False
            if not valid_origin:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid Origin header", "code": "forbidden"})
                return False
        return True

    def _extract_token(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def do_GET(self) -> None:
        if not self._validate_host_origin():
            return
        # GET on /mcp is 405 Method Not Allowed per MCP 2025-06-18 spec
        if self.path.startswith("/mcp"):
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "POST, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_DELETE(self) -> None:
        if not self._validate_host_origin():
            return
        parsed_path = urlsplit(self.path).path

        if parsed_path.startswith("/admin/scopes/"):
            # Revoke scope
            scope_id = parsed_path[len("/admin/scopes/"):].strip()
            token = self._extract_token()
            if not token or token != self.server.scope_manager.admin_token:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Admin token required", "code": "unauthorized"})
                return
            revoked = self.server.scope_manager.revoke_scope(scope_id)
            self._send_json(HTTPStatus.OK, {"ok": True, "data": {"revoked": revoked, "scope_id": scope_id}})
            return

        if parsed_path == "/mcp":
            token = self._extract_token()
            session_id = self.headers.get("Mcp-Session-Id")
            auth_res = self.server.scope_manager.authenticate(token)
            with self.server.transport_sessions_lock:
                sess = self.server.transport_sessions.get(session_id) if session_id else None
                if not auth_res or not sess or sess["token"] != token:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Valid Bearer token and matching session required", "code": "unauthorized"})
                    return
                self.server.transport_sessions.pop(session_id, None)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        if not self._validate_host_origin():
            return

        content_len_header = self.headers.get("Content-Length")
        try:
            content_len = int(content_len_header or 0)
        except ValueError:
            content_len = 0

        if content_len > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Payload too large (>2MiB)", "code": "too_large"})
            return

        raw_body = self.rfile.read(content_len) if content_len > 0 else b""

        parsed_path = urlsplit(self.path).path

        # ---------------------------------------------------------------------
        # Admin scope management
        # ---------------------------------------------------------------------
        if parsed_path == "/admin/scopes":
            self._handle_admin_create_scope(raw_body)
            return

        if parsed_path.startswith("/admin/scopes/") and parsed_path.endswith("/renew"):
            scope_id = parsed_path[len("/admin/scopes/"): -len("/renew")].strip()
            self._handle_admin_renew_scope(scope_id, raw_body)
            return

        # ---------------------------------------------------------------------
        # MCP Protocol Endpoint
        # ---------------------------------------------------------------------
        if parsed_path == "/mcp":
            self._handle_mcp_rpc(raw_body)
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def _handle_admin_create_scope(self, raw_body: bytes) -> None:
        token = self._extract_token()
        if not token or token != self.server.scope_manager.admin_token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Admin token required", "code": "unauthorized"})
            return
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {exc}", "code": "bad_request"})
            return

        try:
            server_url = self.server.server_url
            self._validate_scope_database_identity(payload)
            res = self.server.scope_manager.register_scope(payload, server_url)
            self._send_json(HTTPStatus.OK, {"ok": True, "data": res})
        except util.KanpilotError as exc:
            status = HTTPStatus.CONFLICT if exc.code == "conflict" else HTTPStatus.BAD_REQUEST
            self._send_json(status, {"error": exc.message, "code": exc.code})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "code": "internal"})

    def _validate_scope_database_identity(self, payload: dict) -> None:
        request_id = payload.get("request_id")
        task_id = payload.get("task_id")
        run_id = payload.get("run_id")
        if not any((request_id, task_id, run_id)):
            return
        conn = db.connect(self.server.db_path)
        try:
            task = None
            if task_id:
                task = conn.execute("SELECT task_id, run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if task is None:
                    raise util.bad_request("task_id 不存在")
            request = None
            if request_id:
                request = conn.execute(
                    "SELECT request_id, task_id, run_id FROM requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if request is None:
                    raise util.bad_request("request_id 不存在")
                if task_id and request["task_id"] != task_id:
                    raise util.bad_request("request_id 与 task_id 不匹配")
                if run_id and request["run_id"] != run_id:
                    raise util.bad_request("request_id 与 run_id 不匹配")
                if not task_id and request["task_id"]:
                    task_id = request["task_id"]
            if run_id and not request_id:
                query = "SELECT 1 FROM execution_rounds WHERE run_id=?"
                params = [run_id]
                if task_id:
                    query += " AND task_id=?"
                    params.append(task_id)
                if conn.execute(query, params).fetchone() is None and not (
                    task is not None and task["run_id"] == run_id
                ):
                    raise util.bad_request("run_id 与 task_id 不存在对应执行轮次")
        finally:
            conn.close()

    def _handle_admin_renew_scope(self, scope_id: str, raw_body: bytes) -> None:
        token = self._extract_token()
        if not token or token != self.server.scope_manager.admin_token:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Admin token required", "code": "unauthorized"})
            return
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON: {exc}", "code": "bad_request"})
            return

        new_expires_at = payload.get("expires_at")
        if not isinstance(new_expires_at, str) or not new_expires_at:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "expires_at 必填", "code": "bad_request"})
            return

        try:
            res = self.server.scope_manager.renew_scope(scope_id, new_expires_at)
            self._send_json(HTTPStatus.OK, {"ok": True, "data": res})
        except util.KanpilotError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": exc.message, "code": exc.code})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "code": "internal"})

    def _handle_mcp_rpc(self, raw_body: bytes) -> None:
        try:
            req = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except Exception as exc:
            self._send_error_jsonrpc(-32700, f"Parse error: {exc}", req_id=None)
            return

        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or "method" not in req:
            self._send_error_jsonrpc(-32600, "Invalid Request: expected jsonrpc 2.0 object with method", req_id=req.get("id") if isinstance(req, dict) else None)
            return

        method = req["method"]
        req_id = req.get("id")
        if not isinstance(method, str) or not method or isinstance(req_id, bool) or (req_id is not None and not isinstance(req_id, (str, int, float))):
            self._send_error_jsonrpc(-32600, "Invalid Request: invalid method or id", req_id=None)
            return
        params = req.get("params", {})
        if not isinstance(params, dict):
            self._send_error_jsonrpc(-32602, "params must be an object", req_id=req_id)
            return

        # 1. Initialize method
        if method == "initialize":
            token = self._extract_token()
            auth_res = self.server.scope_manager.authenticate(token)
            if not auth_res:
                self._send_error_jsonrpc(-32000, "Unauthorized: valid scope Bearer token required", req_id=req_id, status=HTTPStatus.UNAUTHORIZED)
                return
            token_type, scope_id = auth_res

            # Create new transport session
            mcp_session_id = secrets.token_hex(16)
            session = {
                "created_at": util.to_iso(util.now_dt()),
                "token": token,
                "token_type": token_type,
                "scope_id": scope_id,
                "host": self.headers.get("Host", "").lower(),
                "origin": self.headers.get("Origin"),
            }
            with self.server.transport_sessions_lock:
                self.server.transport_sessions[mcp_session_id] = session

            resp_data = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": constants.MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {},
                    },
                    "serverInfo": {
                        "name": constants.MCP_SERVER_NAME,
                        "version": "1.0.0",
                    },
                },
            }
            self._send_json(HTTPStatus.OK, resp_data, extra_headers={"Mcp-Session-Id": mcp_session_id})
            return

        # Revalidate the grant on every request, and bind a session to that same grant and origin.
        token = self._extract_token()
        session_id = self.headers.get("Mcp-Session-Id")
        auth_res = self.server.scope_manager.authenticate(token)
        if not auth_res:
            self._send_error_jsonrpc(-32000, "Unauthorized: valid scope Bearer token required", req_id=req_id, status=HTTPStatus.UNAUTHORIZED)
            return
        token_type, scope_id = auth_res
        if session_id:
            with self.server.transport_sessions_lock:
                sess = self.server.transport_sessions.get(session_id)
                if (
                    not sess
                    or sess["token"] != token
                    or sess["token_type"] != token_type
                    or sess["scope_id"] != scope_id
                    or sess["host"] != self.headers.get("Host", "").lower()
                    or sess["origin"] != self.headers.get("Origin")
                ):
                    self._send_error_jsonrpc(-32000, "Unauthorized: session does not match this grant and origin", req_id=req_id, status=HTTPStatus.UNAUTHORIZED)
                    return

        # Notifications still authenticate and validate their transport session.
        if req_id is None and method.startswith("notifications/"):
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        scope = self.server.scope_manager.get_scope(scope_id) if scope_id else None

        # 2. Ping method
        if method == "ping":
            self._send_json(HTTPStatus.OK, {"jsonrpc": "2.0", "id": req_id, "result": {}})
            return

        if token_type == "admin" and method in ("tools/list", "tools/call"):
            self._send_error_jsonrpc(-32000, "Forbidden: admin credential is limited to transport health", req_id=req_id, status=HTTPStatus.FORBIDDEN)
            return

        # 3. tools/list method
        if method == "tools/list":
            tools_list = mcp_tools.list_tools(scope, token_type)
            self._send_json(HTTPStatus.OK, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": tools_list,
                },
            })
            return

        # 4. tools/call method
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(tool_name, str) or not tool_name:
                self._send_error_jsonrpc(-32602, "params.name 必填且须为字符串", req_id=req_id)
                return
            if not isinstance(arguments, dict):
                self._send_error_jsonrpc(-32602, "params.arguments 须为 JSON 对象", req_id=req_id)
                return

            try:
                data = mcp_tools.dispatch_tool_call(
                    db_path=self.server.db_path,
                    scope_manager=self.server.scope_manager,
                    token_type=token_type,
                    scope=scope,
                    tool_name=tool_name,
                    arguments=arguments,
                )
                self._send_json(HTTPStatus.OK, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"ok": True, "data": data}, ensure_ascii=False),
                            }
                        ],
                        "structuredContent": {
                            "ok": True,
                            "data": data,
                        },
                        "isError": False,
                    },
                })
            except util.KanpilotError as exc:
                err_dict = {"code": exc.code, "message": exc.message}
                self._send_json(HTTPStatus.OK, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"ok": False, "error": err_dict}, ensure_ascii=False),
                            }
                        ],
                        "structuredContent": {
                            "ok": False,
                            "error": err_dict,
                        },
                        "isError": True,
                    },
                })
            except Exception as exc:
                err_dict = {"code": "internal", "message": str(exc)}
                self._send_json(HTTPStatus.OK, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"ok": False, "error": err_dict}, ensure_ascii=False),
                            }
                        ],
                        "structuredContent": {
                            "ok": False,
                            "error": err_dict,
                        },
                        "isError": True,
                    },
                })
            return

        # Unknown method
        self._send_error_jsonrpc(-32601, f"Method not found: {method}", req_id=req_id)


class MCPHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: Tuple[str, int],
        RequestHandlerClass: Any,
        db_path: Path,
        scope_manager: mcp_scopes.ScopeManager,
        connection_file: Optional[Path] = None,
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.db_path = db_path
        self.scope_manager = scope_manager
        self.connection_file = connection_file
        self.transport_sessions: Dict[str, dict] = {}
        self.transport_sessions_lock = threading.RLock()
        self.authority = f"{server_address[0]}:{self.server_port}".lower()
        self.server_url = f"http://127.0.0.1:{self.server_port}/mcp"


class MCPServer:
    """Manages MCP Server lifecycle, scope manager, and connection manifest."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        app_support_dir: Optional[Path] = None,
        connection_file: Optional[Path] = None,
        host: str = "127.0.0.1",
        port: int = 0,
        epoch: Optional[str] = None,
    ):
        self.db_path = db_path or constants.default_db_path()
        self.app_support = (
            Path(app_support_dir).expanduser()
            if app_support_dir
            else constants.app_support()
        )
        self.connection_file = (
            Path(connection_file).expanduser()
            if connection_file
            else self.app_support / "mcp" / "server.json"
        )
        self.host = host
        if self.host not in ("127.0.0.1", "localhost"):
            raise ValueError("MCP server must bind to 127.0.0.1 or localhost")
        self.port = port
        self.epoch = epoch or uuid.uuid4().hex
        self.scope_manager = mcp_scopes.ScopeManager(
            app_support_dir=self.app_support,
            epoch=self.epoch,
        )
        self.http_server: Optional[MCPHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the HTTP server on a background thread and write server.json."""
        # A healthy transport must not conceal an outdated task database.
        checked = db.connect(self.db_path)
        checked.close()
        self.http_server = MCPHTTPServer(
            (self.host, self.port),
            MCPRequestHandler,
            db_path=self.db_path,
            scope_manager=self.scope_manager,
            connection_file=self.connection_file,
        )
        actual_port = self.http_server.server_port
        self.port = actual_port
        self.server_url = f"http://{self.host}:{actual_port}/mcp"
        self.http_server.server_url = self.server_url

        # Write server.json
        self._write_server_manifest(active=True)

        self.thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the HTTP server and update server.json active=false."""
        if self.http_server:
            try:
                self._write_server_manifest(active=False)
            except Exception:
                pass
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
            self.thread = None

    def _write_server_manifest(self, active: bool) -> None:
        self.connection_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.connection_file.parent, 0o700)
        except OSError:
            pass

        token_file = self.app_support / "mcp" / "server.token"
        data = {
            "server_name": constants.MCP_SERVER_NAME,
            "url": f"http://{self.host}:{self.port}/mcp",
            "admin_url": f"http://{self.host}:{self.port}/admin/scopes",
            "pid": os.getpid(),
            "epoch": self.epoch,
            "protocol_version": constants.MCP_PROTOCOL_VERSION,
            "active": active,
            "token_file": str(token_file),
        }
        raw = json.dumps(data, indent=2, ensure_ascii=False)
        tmp_file = self.connection_file.parent / f".server.{os.getpid()}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(str(tmp_file), flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
            tmp_file.replace(self.connection_file)
        except Exception:
            self.connection_file.write_text(raw, encoding="utf-8")
            try:
                os.chmod(self.connection_file, 0o600)
            except OSError:
                pass
