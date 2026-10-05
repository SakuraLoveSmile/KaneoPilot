"""Transport and credential-file checks for MCPClient."""

import json
from pathlib import Path
import sys
import urllib.error
from unittest.mock import patch

import pytest

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from kanpilot import constants, mcp_client, util


class Response:
    def __init__(self, payload=b"", status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_protocol_version_header_added_after_initialize():
    client = mcp_client.MCPClient("http://127.0.0.1:8765/mcp", "token")
    captured = []

    def open_request(request, timeout):
        captured.append(request.headers)
        if len(captured) == 1:
            return Response(json.dumps({"result": {
                "protocolVersion": constants.MCP_PROTOCOL_VERSION,
                "serverInfo": {"name": "test", "version": "1"},
            }}).encode())
        return Response(b"", status=202)

    with patch("kanpilot.mcp_client.urllib.request.urlopen", side_effect=open_request):
        client.initialize()

    assert "Mcp-session-id" not in captured[0]
    assert "Mcp-session-id" not in captured[1]
    assert "Mcp-protocol-version" not in captured[0]
    assert captured[1]["Mcp-protocol-version"] == constants.MCP_PROTOCOL_VERSION


def test_notification_http_error_is_not_swallowed():
    client = mcp_client.MCPClient("http://127.0.0.1:8765/mcp", "token")
    client.protocol_version = constants.MCP_PROTOCOL_VERSION
    failure = urllib.error.HTTPError(
        client.server_url, 503, "unavailable", {}, None
    )
    with patch("kanpilot.mcp_client.urllib.request.urlopen", side_effect=failure):
        with pytest.raises(util.KanpilotError, match="通知 HTTP 503"):
            client._notify("notifications/initialized")


def test_rpc_http_json_error_is_preserved():
    client = mcp_client.MCPClient("http://127.0.0.1:8765/mcp", "token")
    error_body = json.dumps({"error": {"code": -32602, "message": "bad params"}}).encode()
    failure = urllib.error.HTTPError(
        client.server_url, 400, "bad request", {}, __import__("io").BytesIO(error_body)
    )
    with patch("kanpilot.mcp_client.urllib.request.urlopen", side_effect=failure):
        with pytest.raises(util.KanpilotError) as error:
            client.ping()
    assert error.value.code == "-32602"
    assert "bad params" in error.value.message


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_grant_file_requires_owner_only_permissions(tmp_path, mode):
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps({"url": "http://127.0.0.1:8765/mcp", "token": "secret"}))
    grant.chmod(mode)
    with pytest.raises(util.KanpilotError, match="权限"):
        mcp_client.MCPClient.from_grant_file(grant)


def test_grant_file_rejects_loopback_lookalike(tmp_path):
    grant = tmp_path / "grant.json"
    grant.write_text(json.dumps({"url": "http://127.0.0.1.attacker.example/mcp", "token": "secret"}))
    grant.chmod(0o600)
    with pytest.raises(util.KanpilotError, match="loopback"):
        mcp_client.MCPClient.from_grant_file(grant)
