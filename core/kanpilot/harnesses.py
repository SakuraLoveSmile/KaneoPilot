"""Static harness contracts; discovery never starts a process or probes a service.

Only the Plus adapter implements the project MCP runtime control contract. CLI
descriptions describe existing runners, and do not grant runtime permissions.
"""
from __future__ import annotations

from . import util

CONTRACT_VERSION = "kp-harness-v1"
CAPABILITY_NAMES = (
    "structured_result", "durable_status", "verification_report",
    "verification_command_binding", "observed_command_results", "input_requests",
    "stop_confirmation", "read_only_workspace",
)


def capability(state: str, guarantee: str, detail: str) -> dict:
    if state not in ("supported", "unsupported", "unknown"):
        raise ValueError("invalid capability state")
    if guarantee not in ("host", "adapter", "model"):
        raise ValueError("invalid capability guarantee")
    return {"state": state, "guarantee": guarantee, "detail": detail}


def require_capabilities(description: dict, required) -> None:
    """Fail closed on disconnected, unknown, missing, or unsupported capabilities."""
    if not isinstance(required, (list, tuple, set, frozenset)) or any(
            not isinstance(name, str) or not name for name in required):
        raise util.bad_request("required capabilities must be a collection")
    required = tuple(required)
    if not isinstance(description, dict) or description.get("contract_version") != CONTRACT_VERSION:
        raise util.KanpilotError("unsupported_capabilities", "不支持的 harness 能力契约版本")
    capabilities = description.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    missing = [name for name in required
               if not isinstance(capabilities.get(name), dict)
               or capabilities[name].get("state") != "supported"
               or capabilities[name].get("guarantee") not in ("host", "adapter", "model")
               or not isinstance(capabilities[name].get("detail"), str)]
    if description.get("connected") is not True or missing:
        detail = "、".join(sorted(set(missing))) if missing else "连接状态未确认"
        raise util.KanpilotError("unsupported_capabilities", "Harness 不满足必需能力：" + detail)


class _CLIHarness:
    runtime_control = False
    adapter_version = "1"

    def __init__(self, harness_id: str):
        self.harness_id = harness_id

    def describe(self, *, role: str, connected=None) -> dict:
        if role not in ("plan", "planner", "review"):
            raise util.KanpilotError("unsupported_role", "CLI harness 仅支持现有规划和评审 runner")
        if connected is not None and not isinstance(connected, bool):
            raise util.bad_request("connected must be true, false, or unknown")
        caps = {name: capability("unsupported", "adapter", "现有 CLI runner 未实现此机制")
                for name in CAPABILITY_NAMES}
        caps["structured_result"] = capability(
            "supported", "adapter", "现有 runner 请求 schema 输出并校验阶段结果；不证明内容真实")
        caps["read_only_workspace"] = capability(
            "supported", "host", "Codex read-only sandbox" if self.harness_id == "codex"
            else "Claude restricted Read/Grep/Glob 工具集及严格 MCP 配置")
        return {
            "contract_version": CONTRACT_VERSION, "harness_id": self.harness_id,
            "adapter_version": self.adapter_version, "connected": connected,
            "capabilities": caps, "role": role,
        }


_CLI_ADAPTERS = {name: _CLIHarness(name) for name in ("codex", "claude")}


def get_adapter(harness_id: str):
    if not isinstance(harness_id, str):
        raise util.KanpilotError("unsupported_harness", "不支持的 harness")
    if harness_id == "pi_desktop":
        from .harness_plus_adapter import ADAPTER
        return ADAPTER
    if harness_id in _CLI_ADAPTERS:
        return _CLI_ADAPTERS[harness_id]
    raise util.KanpilotError("unsupported_harness", "不支持的 harness")


def get_runtime_adapter(harness_id: str):
    adapter = get_adapter(harness_id)
    if not adapter.runtime_control:
        raise util.KanpilotError("unsupported_harness", "该 harness 未实现 MCP runtime 控制契约")
    return adapter


def describe_cli_harness(harness_id: str, *, role: str, connected=None) -> dict:
    adapter = get_adapter(harness_id)
    if adapter.runtime_control:
        raise util.KanpilotError("unsupported_harness", "该 harness 不是 CLI runner")
    return adapter.describe(role=role, connected=connected)


def supports_role(harness_id: str, role: str) -> bool:
    """Implemented phase mechanisms, independent of connectivity or readiness."""
    roles = {
        "pi_desktop": ("plan", "planner", "review", "execution"),
        "codex": ("plan", "planner", "review"),
        "claude": ("plan", "planner", "review"),
    }
    return role in roles.get(harness_id, ())


def runtime_adapter_for_scope(scope, arguments: dict):
    """Resolve only the trusted scope identity; call arguments cannot select it.

    Existing persisted scopes have no harness identity and authorize Plus only.
    Runtime configuration is not an authorization source for a different harness.
    """
    harness_id = "pi_desktop"
    if arguments.get("harness_id", harness_id) != harness_id:
        raise util.KanpilotError("forbidden", "harness_id does not match authorized scope")
    return get_runtime_adapter(harness_id)
