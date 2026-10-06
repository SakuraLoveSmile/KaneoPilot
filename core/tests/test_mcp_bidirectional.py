"""Forwarder for bidirectional MCP tests to satisfy test runners invoking core/tests/test_mcp_bidirectional.py."""

from pathlib import Path
import sys

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO / "core") not in sys.path:
    sys.path.insert(0, str(_REPO / "core"))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests.test_mcp_bidirectional import *  # noqa: F401, F403
