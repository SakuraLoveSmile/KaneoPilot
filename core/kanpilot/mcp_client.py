"""KaneoPilot MCP Client (protocol §5.25 / T7.4 & T7.5).

Communicates with standard MCP 2025-06-18 servers (including KaneoPilot MCP server
and Pi-Desktop-Plus MCP server) via HTTP JSON-RPC 2.0.
"""

from __future__ import annotations

import json
import ipaddress
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import constants, util


def pi_desktop_backend_is_loopback(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class MCPClient:
    """Client for KaneoPilot or standard MCP server."""

    def __init__(
        self,
        server_url: str,
        token: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.mcp_session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self.server_info: Optional[dict] = None
        self.capabilities: Optional[dict] = None
        self._req_id_counter = 0

    @classmethod
    def from_grant_file(cls, grant_file: Path, timeout: float = 60.0) -> "MCPClient":
        """Create MCPClient from a grant JSON file."""
        data = cls._read_secure_json(grant_file, "Grant")
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise util.bad_request(f"Grant 文件缺少 url: {grant_file}")
        cls._validate_loopback_url(url, "Grant")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise util.bad_request(f"Grant 文件缺少 token: {grant_file}")
        return cls(server_url=url, token=token, timeout=timeout)

    @classmethod
    def from_server_file(cls, server_file: Path, timeout: float = 60.0) -> "MCPClient":
        """Create MCPClient from a server.json file."""
        data = cls._read_secure_json(server_file, "Server")
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise util.bad_request(f"Server 文件缺少 url: {server_file}")
        cls._validate_loopback_url(url, "Server")
        token = None
        token_file_str = data.get("token_file")
        if token_file_str:
            tok_path = Path(token_file_str).expanduser()
            token_data = cls._read_secure_file(tok_path, "Server token")
            token = token_data.decode("utf-8").strip()
            if not token:
                raise util.KanpilotError("validation_failed", "Server token 文件为空")
        return cls(server_url=url, token=token, timeout=timeout)

    @staticmethod
    def _read_secure_file(path: Path, label: str) -> bytes:
        path = Path(path).expanduser()
        if path.is_symlink():
            raise util.KanpilotError("validation_failed", f"{label} 文件不能是符号链接：{path}")
        try:
            st = path.stat()
        except FileNotFoundError:
            raise util.KanpilotError("not_found", f"{label} 文件不存在：{path}")
        if not stat.S_ISREG(st.st_mode):
            raise util.KanpilotError("validation_failed", f"{label} 路径必须是普通文件：{path}")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise util.KanpilotError("validation_failed", f"{label} 文件所有者不符：{path}")
        if stat.S_IMODE(st.st_mode) not in (0o600, 0o400):
            raise util.KanpilotError("validation_failed", f"{label} 文件权限必须为 0600/0400：{path}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise util.KanpilotError("validation_failed", f"{label} 文件不可读：{exc}") from exc

    @classmethod
    def _read_secure_json(cls, path: Path, label: str) -> dict:
        raw = cls._read_secure_file(path, label)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise util.KanpilotError("validation_failed", f"{label} 文件不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise util.KanpilotError("validation_failed", f"{label} 文件必须是 JSON 对象")
        return data

    @staticmethod
    def _validate_loopback_url(url: str, label: str) -> None:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "http" or not host or parsed.username or parsed.password
                or not pi_desktop_backend_is_loopback(host)):
            raise util.KanpilotError("validation_failed", f"{label} URL 必须是无凭据的 HTTP loopback 地址")

    def _next_id(self) -> int:
        self._req_id_counter += 1
        return self._req_id_counter

    def _rpc_call(self, method: str, params: Optional[dict] = None) -> Any:
        req_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.mcp_session_id:
            headers["Mcp-Session-Id"] = self.mcp_session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version

        req_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_req = urllib.request.Request(
            self.server_url,
            data=req_bytes,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(http_req, timeout=self.timeout) as resp:
                resp_sess_id = resp.headers.get("Mcp-Session-Id")
                if resp_sess_id:
                    self.mcp_session_id = resp_sess_id
                body = resp.read().decode("utf-8")
                if not body and resp.status == 202:
                    return None
                resp_json = json.loads(body)
        except urllib.error.HTTPError as exc:
            raw_error = exc.read() if exc.fp else b""
            err_body = raw_error.decode("utf-8", errors="replace") if isinstance(raw_error, bytes) else str(raw_error)
            parsed = None
            try:
                parsed = json.loads(err_body)
            except json.JSONDecodeError:
                pass
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                err_info = parsed["error"]
                code = err_info.get("code", "http_error")
                msg = err_info.get("message", str(exc))
                raise util.KanpilotError(str(code), msg) from exc
            raise util.KanpilotError("http_error", f"HTTP {exc.code}: {exc.reason}")
        except urllib.error.URLError as exc:
            raise util.KanpilotError("network_error", f"无法连接到 MCP 服务器 {self.server_url}: {exc.reason}")
        except Exception as exc:
            raise util.KanpilotError("client_error", f"MCP RPC 调用失败: {exc}")

        if "error" in resp_json:
            err_info = resp_json["error"]
            code = err_info.get("code", "mcp_error")
            msg = err_info.get("message", "Unknown MCP error")
            raise util.KanpilotError(str(code), msg)

        return resp_json.get("result")

    def initialize(self, client_name: str = "kaneopilot-client", client_version: str = "1.0.0") -> dict:
        """Perform initialize and notifications/initialized handshake."""
        res = self._rpc_call(
            "initialize",
            {
                "protocolVersion": constants.MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": client_name,
                    "version": client_version,
                },
            },
        )
        self.server_info = res.get("serverInfo") if isinstance(res, dict) else None
        self.capabilities = res.get("capabilities") if isinstance(res, dict) else None
        if isinstance(res, dict) and "protocolVersion" in res:
            self.protocol_version = res["protocolVersion"]
        if self.protocol_version not in constants.MCP_COMPATIBLE_VERSIONS:
            raise util.KanpilotError("protocol_error", f"MCP 协议版本不受支持：{self.protocol_version}")

        # Send notifications/initialized
        self._notify("notifications/initialized", {})
        return res

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.mcp_session_id:
            headers["Mcp-Session-Id"] = self.mcp_session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version

        req_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_req = urllib.request.Request(
            self.server_url,
            data=req_bytes,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                if resp.status == 202 or not body:
                    return
                result = json.loads(body)
                if isinstance(result, dict) and isinstance(result.get("error"), dict):
                    error = result["error"]
                    raise util.KanpilotError(str(error.get("code", "mcp_error")), error.get("message", "MCP notification failed"))
                raise util.KanpilotError("protocol_error", f"MCP 通知返回了非预期响应：{result}")
        except urllib.error.HTTPError as exc:
            raw_error = exc.read() if exc.fp else b""
            body = raw_error.decode("utf-8", errors="replace") if isinstance(raw_error, bytes) else str(raw_error)
            raise util.KanpilotError("http_error", f"MCP 通知 HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise util.KanpilotError("network_error", f"无法连接到 MCP 服务器：{exc.reason}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise util.KanpilotError("protocol_error", "MCP 通知返回非 JSON 响应") from exc

    def ping(self) -> dict:
        """Send ping request."""
        return self._rpc_call("ping", {})

    def list_tools(self) -> List[dict]:
        """List available tools on the server."""
        res = self._rpc_call("tools/list", {})
        if isinstance(res, dict) and "tools" in res:
            return res["tools"]
        return []

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a tool and return structured data or raise KanpilotError."""
        res = self._rpc_call("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(res, dict):
            raise util.KanpilotError("protocol_error", f"tool/call 返回非法结果：{res}")

        is_error = res.get("isError", False)
        structured = res.get("structuredContent")

        if structured and isinstance(structured, dict):
            if not structured.get("ok", True) or is_error:
                err = structured.get("error", {})
                code = err.get("code", "tool_error")
                msg = err.get("message", "Tool execution failed")
                raise util.KanpilotError(str(code), msg)
            return structured.get("data", {})

        # Fallback to content parsing if structuredContent not present
        if is_error:
            content_list = res.get("content", [])
            err_msg = "Tool execution failed"
            err_code = "tool_error"
            if content_list and isinstance(content_list, list) and len(content_list) > 0:
                txt = content_list[0].get("text", "")
                try:
                    parsed_txt = json.loads(txt)
                    if isinstance(parsed_txt, dict) and "error" in parsed_txt:
                        err_code = parsed_txt["error"].get("code", err_code)
                        err_msg = parsed_txt["error"].get("message", err_msg)
                except Exception:
                    if txt:
                        err_msg = txt
            raise util.KanpilotError(err_code, err_msg)

        content_list = res.get("content", [])
        if content_list and isinstance(content_list, list) and len(content_list) > 0:
            txt = content_list[0].get("text", "")
            try:
                parsed_txt = json.loads(txt)
                if isinstance(parsed_txt, dict) and "data" in parsed_txt:
                    return parsed_txt["data"]
                return parsed_txt
            except Exception:
                return {"text": txt}

        return {}
