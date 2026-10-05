"""KaneoPilot MCP stdio transport bridge (protocol §5.25 / T7.4).

Reads line-delimited JSON-RPC from stdin, forwards to KaneoPilot local HTTP MCP server
with grant authentication, and writes JSON-RPC responses to stdout.
"""

from __future__ import annotations

import json
import ipaddress
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from . import constants, util


def _log_err(msg: str) -> None:
    sys.stderr.write(f"[kanpilot-mcp-stdio] {msg}\n")
    sys.stderr.flush()


def _valid_loopback_mcp_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "http" or not host or parsed.path != "/mcp":
            return False
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            return False
        if host.lower() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_stdio_bridge(
    connection_file: Optional[Path] = None,
    scope_file: Optional[Path] = None,
) -> int:
    """Run stdio loop forwarding to running local HTTP MCP server."""
    token: Optional[str] = None
    server_url: Optional[str] = None

    if scope_file and scope_file.is_file():
        try:
            grant_data = json.loads(scope_file.read_text(encoding="utf-8"))
            token = grant_data.get("token")
            server_url = grant_data.get("url")
        except Exception as exc:
            _log_err(f"无法读取 scope-file {scope_file}: {exc}")
            return 1

    if not server_url and connection_file and connection_file.is_file():
        try:
            srv_data = json.loads(connection_file.read_text(encoding="utf-8"))
            server_url = srv_data.get("url")
            token_file_str = srv_data.get("token_file")
            if token_file_str and not token:
                token_file = Path(token_file_str)
                if token_file.is_file():
                    token = token_file.read_text(encoding="utf-8").strip()
        except Exception as exc:
            _log_err(f"无法读取 connection-file {connection_file}: {exc}")
            return 1

    if not server_url:
        # Default server.json location
        default_srv = constants.app_support() / "mcp" / "server.json"
        if default_srv.is_file():
            try:
                srv_data = json.loads(default_srv.read_text(encoding="utf-8"))
                server_url = srv_data.get("url")
                token_file_str = srv_data.get("token_file")
                if token_file_str and not token:
                    token_file = Path(token_file_str)
                    if token_file.is_file():
                        token = token_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass

    if not server_url:
        _log_err("MCP 服务未运行：无法找到有效的 server.json 或 scope grant 文件")
        return 1
    if not _valid_loopback_mcp_url(server_url):
        _log_err("MCP URL 必须是本机 loopback HTTP /mcp 端点")
        return 1
    if not isinstance(token, str) or not token:
        _log_err("MCP grant 中缺少 Bearer token")
        return 1

    mcp_session_id: Optional[str] = None

    for line in sys.stdin:
        line_str = line.strip()
        if not line_str:
            continue
        try:
            req = json.loads(line_str)
        except Exception as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if mcp_session_id:
            headers["Mcp-Session-Id"] = mcp_session_id

        req_bytes = json.dumps(req, ensure_ascii=False).encode("utf-8")
        http_req = urllib.request.Request(
            server_url,
            data=req_bytes,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(http_req, timeout=60.0) as resp:
                resp_sess_id = resp.headers.get("Mcp-Session-Id")
                if resp_sess_id:
                    mcp_session_id = resp_sess_id
                resp_bytes = resp.read()
                if resp_bytes:
                    sys.stdout.write(resp_bytes.decode("utf-8") + "\n")
                    sys.stdout.flush()
                elif resp.status == 202:
                    # Notification accepted, no stdout response required
                    pass
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8") if exc.fp else ""
            try:
                parsed_err = json.loads(body)
                sys.stdout.write(json.dumps(parsed_err, ensure_ascii=False) + "\n")
            except Exception:
                req_id = req.get("id") if isinstance(req, dict) else None
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"HTTP {exc.code}: {body or exc.reason}"},
                }
                sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except urllib.error.URLError as exc:
            req_id = req.get("id") if isinstance(req, dict) else None
            err_resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"MCP 服务连接失败: {exc.reason}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            req_id = req.get("id") if isinstance(req, dict) else None
            err_resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    return 0
