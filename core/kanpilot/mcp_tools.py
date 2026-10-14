"""Standard MCP tool definitions and dispatching for KaneoPilot (protocol §5.25 / T7.4).

Provides tools/list metadata with JSON schemas and input/output contracts for:
- Control domain (kp_runtime_*): capabilities, catalog, session_create, session_configure,
  prompt, status, turn_get, messages, stop, abort, proposals, proposal_resolve.
- Task domain (kp_task_*): claim, get, plan, request, session, messages, heartbeat,
  report, dispatch_prepare, dispatch_begin_send, dispatch_start, dispatch_confirm,
  dispatch_fail, recheck, observe, advance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import (
    cli, cmd_dispatch, cmd_exec, cmd_queue, cmd_read, cmd_remote, cmd_session,
    constants, db, harness_plus_adapter, mcp_scopes, runtime_operations, util,
)

TOOLS_SPEC: List[Dict[str, Any]] = [
    # ------------------------------------------------------------- Control Tools
    {
        "name": "kp_runtime_capabilities",
        "description": "Check harness readiness, connected status, and supported tools (read-only).",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string", "description": "The active scope ID."},
                "harness_id": {"type": "string", "description": "Harness identifier, default 'pi_desktop'."},
            },
            "required": ["scope_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "available": {"type": "boolean"},
                "connected": {"type": "boolean"},
                "detail": {"type": "string"},
                "tools_ok": {"type": "boolean"},
                "version": {"type": ["string", "null"]},
                "supported_operations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["available", "connected", "detail", "tools_ok"],
        },
    },
    {
        "name": "kp_runtime_catalog",
        "description": "Read provider/model catalog and resolved default configuration from local cache/fallback (read-only).",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "harness_id": {"type": "string"},
                "provider_id": {"type": "string"},
            },
            "required": ["scope_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "connection_file": {"type": "string"},
                "providers": {"type": "array"},
                "resolved_default": {"type": ["object", "null"]},
                "default_detail": {"type": ["string", "null"]},
            },
            "required": ["connection_file", "providers"],
        },
    },
    {
        "name": "kp_runtime_session_create",
        "description": "Create a new session in the harness bound to the authorized project path and runtime config.",
        "domain": "runtime",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "operation_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["agent", "plan", "goal"]},
                "project_path": {"type": "string"},
                "runtime_config": {"type": "object"},
                "title": {"type": "string"},
                "project_name": {"type": "string"},
                "interaction_mode": {"type": "string", "enum": ["interactive", "unattended"]},
            },
            "required": ["scope_id", "operation_id", "mode", "project_path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "interactionMode": {"type": "string", "enum": ["interactive", "unattended"]},
                "interactionPolicyVersion": {"type": "integer"},
                "session_id": {"type": "string"},
                "title": {"type": ["string", "null"]},
                "project_name": {"type": ["string", "null"]},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "kp_runtime_session_configure",
        "description": "Configure session mode and permission mode in the harness.",
        "domain": "runtime",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "operation_id": {"type": "string"},
                "session_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["agent", "plan", "goal"]},
                "permission_mode": {"type": "string", "enum": ["ask", "accept-edits", "auto"]},
                "interaction_mode": {"type": "string", "enum": ["interactive", "unattended"]},
            },
            "required": ["scope_id", "operation_id", "session_id", "mode", "permission_mode"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "object"},
                "session_id": {"type": "string"},
                "configured": {"type": "boolean"},
            },
            "required": ["session_id", "configured"],
        },
    },
    {
        "name": "kp_runtime_attach_mcp",
        "description": "Attach session-scoped task grant bridge file to a session in the harness.",
        "domain": "runtime",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "operation_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["scope_id", "operation_id", "session_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "attached": {"type": "boolean"},
                "server_name": {"type": "string"},
                "tool_names": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["session_id", "attached", "server_name", "tool_names"],
        },
    },
    {
        "name": "kp_runtime_prompt",
        "name": "kp_runtime_prompt",
        "description": "Send a prompt to an existing session in the harness.",
        "domain": "runtime",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "expected_interaction_mode": {"type": "string", "enum": ["interactive", "unattended"]},
                "scope_id": {"type": "string"},
                "operation_id": {"type": "string"},
                "session_id": {"type": "string"},
                "content": {"type": "string"},
                "attachments": {"type": "array", "items": {
                    "type": "object", "properties": {
                        "path": {"type": "string"}, "name": {"type": "string"},
                        "kind": {"type": "string", "enum": ["image", "file"]},
                        "mimeType": {"type": "string"}, "size": {"type": "integer", "minimum": 0},
                    }, "required": ["path", "name", "kind"], "additionalProperties": False,
                }},
            },
            "required": ["scope_id", "operation_id", "session_id", "content"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "turn_id": {"type": ["string", "null"]},
                "accepted": {"type": "boolean"},
            },
            "required": ["session_id", "accepted"],
        },
    },
    {
        "name": "kp_runtime_status",
        "description": "Get current active execution status of a session in the harness (read-only).",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["scope_id", "session_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "is_running": {"type": "boolean"},
                "current_turn_id": {"type": ["string", "null"]},
                "pending_permission": {},
            },
            "required": ["session_id", "is_running"],
        },
    },
    {
        "name": "kp_runtime_turn_get",
        "description": "Get persistent turn outcome status (running, completed, error, aborted) from the harness (read-only).",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "require_identity": {"type": "boolean"},
            },
            "required": ["scope_id", "session_id", "turn_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "error_details": {"type": ["object", "null"]},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "status": {"type": "string", "enum": ["running", "completed", "error", "aborted", "unknown"]},
                "ended_at": {"type": ["string", "null"]},
                "error_code": {"type": ["string", "null"]},
            },
            "required": ["session_id", "turn_id", "status"],
        },
    },
    {
        "name": "kp_runtime_messages",
        "description": "Read paginated transcript messages from the session (read-only).",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "session_id": {"type": "string"},
                "before": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["scope_id", "session_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "interactionMode": {"type": "string", "enum": ["interactive", "unattended"]},
                "interactionPolicyVersion": {"type": "integer"},
                "session_id": {"type": "string"},
                "messages": {"type": "array"},
                "message_start": {"type": ["integer", "null"]},
                "message_end": {"type": ["integer", "null"]},
                "has_more_before": {"type": "boolean"},
                "has_more_after": {"type": "boolean"},
                "truncated": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["session_id", "messages"],
        },
    },
    {
        "name": "kp_runtime_stop",
        "description": "Request grace stop of active turn in the harness.",
        "domain": "runtime",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "operation_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["scope_id", "operation_id", "session_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "requested": {"type": "boolean"},
            },
            "required": ["session_id", "requested"],
        },
    },
    {
        "name": "kp_runtime_abort",
        "description": "Request immediate abort of active turn in the harness.",
        "domain": "runtime",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "operation_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["scope_id", "operation_id", "session_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "requested": {"type": "boolean"},
            },
            "required": ["session_id", "requested"],
        },
    },
    {
        "name": "kp_runtime_proposals",
        "description": "Read pending contract/plan proposals for a session (read-only).",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
            },
            "required": ["scope_id", "session_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "proposals": {"type": "array"},
            },
            "required": ["session_id", "proposals"],
        },
    },
    {
        "name": "kp_runtime_goal_report_list",
        "description": "Read Goal report candidates for an execution scope's session.",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["scope_id", "session_id"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object"},
    },
    {
        "name": "kp_runtime_goal_report_get",
        "description": "Read one Goal report by execution or report identity, preserving its state and hash.",
        "domain": "runtime",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "session_id": {"type": "string"},
                "execution_id": {"type": "string"},
                "report_id": {"type": "string"},
            },
            "required": ["scope_id", "session_id"],
            "anyOf": [{"required": ["execution_id"]}, {"required": ["report_id"]}],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object"},
    },
    {
        "name": "kp_runtime_proposal_resolve",
        "description": "Approve or reject a pending proposal in the harness.",
        "domain": "runtime",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope_id": {"type": "string"},
                "operation_id": {"type": "string"},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "proposal_id": {"type": "string"},
                "version": {"type": "integer"},
                "action": {"type": "string", "enum": ["accept", "reject"]},
                "goal_continuation": {"type": "boolean"},
            },
            "required": ["scope_id", "operation_id", "session_id", "turn_id", "proposal_id", "version", "action"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "proposal_id": {"type": "string"},
                "action": {"type": "string"},
                "resolved": {"type": "boolean"},
            },
            "required": ["session_id", "proposal_id", "action", "resolved"],
        },
    },

    # ------------------------------------------------------------- Task Tools
    {
        "name": "kp_task_claim",
        "description": "Claim a pending task for the authorized stage/role.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation_id": {"type": "string"},
                "request_id": {"type": "string"},
            },
            "required": ["operation_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "claimed": {"type": "boolean"},
                "context": {"type": "object"},
                "reason": {"type": "string"},
            },
            "required": ["claimed"],
        },
    },
    {
        "name": "kp_task_get",
        "description": "Get details of an authorized task (read-only).",
        "domain": "task",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "object"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "kp_task_plan",
        "description": "Read the verified plan markdown for a task (read-only).",
        "domain": "task",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "expected_plan_sha256": {"type": "string"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "plan_kind": {"type": "string"},
                "generated_at": {"type": "string"},
                "plan_sha256": {"type": "string"},
                "markdown": {"type": "string"},
            },
            "required": ["task_id", "title", "markdown"],
        },
    },
    {
        "name": "kp_task_request",
        "description": "Get request details for the active claim (read-only).",
        "domain": "task",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
            },
            "required": ["request_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "object"},
            },
            "required": ["request"],
        },
    },
    {
        "name": "kp_task_session",
        "description": "Get execution round session info for a task (read-only).",
        "domain": "task",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["task_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "object"},
            },
            "required": ["session"],
        },
    },
    {
        "name": "kp_task_messages",
        "description": "Read message history for a task session (read-only).",
        "domain": "task",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
                "source_version": {"type": "string"},
                "force_reload": {"type": "boolean"},
            },
            "required": ["task_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "messages": {"type": "array"},
                "next_cursor": {"type": ["string", "null"]},
                "source_version": {"type": ["string", "null"]},
            },
            "required": ["messages"],
        },
    },
    {
        "name": "kp_task_heartbeat",
        "description": "Send heartbeat to renew lease on an active claim.",
        "domain": "task",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "run_id": {"type": "string"},
                "progress": {"type": "object"},
            },
            "required": ["request_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
            },
            "required": ["ok"],
        },
    },
    {
        "name": "kp_task_report",
        "description": "Report final or intermediate outcome for an active claim.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "run_id": {"type": "string"},
                "outcome": {"type": "string"},
                "note": {"type": "string"},
                "evidence": {"type": "object"},
                "send_state": {"type": "string"},
                "progress": {"type": "object"},
                "failure": {"type": "object"},
            },
            "required": ["request_id", "run_id", "outcome"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
            },
            "required": ["ok"],
        },
    },
    {
        "name": "kp_task_dispatch_prepare",
        "description": "Prepare dispatch workspace and worktree for task execution.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
                "worktree_path": {"type": "string"},
            },
            "required": ["task_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "worktree_path": {"type": "string"},
            },
            "required": ["ok"],
        },
    },
    {
        "name": "kp_task_dispatch_begin_send",
        "description": "Mark beginning of sending task to executor.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["task_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
            },
            "required": ["ok"],
        },
    },
    {
        "name": "kp_task_dispatch_start",
        "description": "Start execution worker for claimed task.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["task_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "started": {"type": "boolean"},
                "pid": {"type": ["integer", "null"]},
            },
            "required": ["started"],
        },
    },
    {
        "name": "kp_task_dispatch_confirm",
        "description": "Confirm successful launch of execution worker.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["task_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
            },
            "required": ["ok"],
        },
    },
    {
        "name": "kp_task_dispatch_fail",
        "description": "Record failure during dispatch pipeline.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
                "reason": {"type": "string"},
                "send_state": {"type": "string"},
            },
            "required": ["task_id", "run_id", "reason"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
            },
            "required": ["ok"],
        },
    },
    {
        "name": "kp_task_recheck",
        "description": "Recheck task status from remote Kaneo (read-only).",
        "domain": "task",
        "readOnlyHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "offline": {"type": "boolean"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "changed": {"type": "boolean"},
            },
            "required": ["task_id", "changed"],
        },
    },
    {
        "name": "kp_task_observe",
        "description": "Observe execution progress for a running task.",
        "domain": "task",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["task_id", "run_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "finished": {"type": "boolean"},
            },
            "required": ["status", "finished"],
        },
    },
    {
        "name": "kp_task_advance",
        "description": "Advance task state on board upon verified completion.",
        "domain": "task",
        "destructiveHint": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "target": {"type": "string"},
                "expected_current": {"type": "string"},
                "evidence_note": {"type": "string"},
            },
            "required": ["task_id", "target", "expected_current"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
            },
            "required": ["ok"],
        },
    },
]

TASK_COMMANDS = {
    "kp_task_claim": "mcp-claim", "kp_task_get": "get-task",
    "kp_task_plan": "get-plan", "kp_task_request": "get-request",
    "kp_task_session": "get-session", "kp_task_messages": "session-messages",
    "kp_task_heartbeat": "heartbeat", "kp_task_report": "report",
    "kp_task_dispatch_prepare": "dispatch-prepare",
    "kp_task_dispatch_begin_send": "dispatch-begin-send",
    "kp_task_dispatch_start": "dispatch-start", "kp_task_dispatch_confirm": "dispatch-confirm",
    "kp_task_dispatch_fail": "dispatch-fail", "kp_task_recheck": "recheck",
    "kp_task_observe": "round-observe", "kp_task_advance": "advance-task",
}

# Fixed task tools share the actual core payload contract, including evidence.
for _spec in TOOLS_SPEC:
    if _spec["name"] in TASK_COMMANDS and _spec["name"] != "kp_task_claim":
        _fields = cli.COMMANDS[TASK_COMMANDS[_spec["name"]]][0]
        _types = {"str": "string", "int": "integer", "bool": "boolean", "dict": "object", "list": "array"}
        _spec["inputSchema"] = {
            "type": "object", "properties": {
                k: ({"type": _types[t]} if t in _types else {})
                for k, (_, t) in _fields.items()
            }, "required": [k for k, (required, _) in _fields.items() if required],
            "additionalProperties": False,
        }
    if _spec["name"] == "kp_task_recheck":
        _spec["readOnlyHint"] = False
    if _spec["name"] == "kp_runtime_proposal_resolve":
        _spec["inputSchema"]["properties"]["tool_call_id"] = {"type": "string"}
        _spec["inputSchema"]["properties"]["target_permission_mode"] = {"type": "string", "enum": ["ask", "accept-edits", "auto"]}
        _spec["inputSchema"]["required"].append("tool_call_id")
    _spec["outputSchema"] = {
        "type": "object", "properties": {
            "ok": {"type": "boolean"},
            "data": (_spec["outputSchema"] if _spec["name"] == "kp_runtime_messages"
                     else {"type": "object"}),
            "error": {"type": "object", "properties": {
                "code": {"type": "string"}, "message": {"type": "string"}},
                "required": ["code", "message"]},
        }, "required": ["ok"], "oneOf": [
            {"properties": {"ok": {"const": True}}, "required": ["data"]},
            {"properties": {"ok": {"const": False}}, "required": ["error"]},
        ], "additionalProperties": False,
    }

TOOLS_REGISTRY = {t["name"]: t for t in TOOLS_SPEC}


def list_tools(scope, token_type):
    return [spec for spec in TOOLS_SPEC
            if token_type == "admin" or (scope and spec["name"] in
                (mcp_scopes.RUNTIME_ROLE_TOOLS if token_type == "runtime"
                 else mcp_scopes.TASK_ROLE_TOOLS).get(scope.role, set()))]


def _validate_arguments(schema, arguments):
    if not isinstance(arguments, dict):
        raise util.bad_request("tool arguments must be an object")
    properties = schema.get("properties", {})
    unknown = set(arguments) - set(properties)
    if unknown:
        raise util.bad_request("unknown fields: " + ", ".join(sorted(unknown)))
    for key in schema.get("required", []):
        if key not in arguments:
            raise util.bad_request("missing required field: " + key)
    for key, value in arguments.items():
        spec = properties[key]
        kind = spec.get("type")
        checks = {"string": lambda: isinstance(value, str) and bool(value.strip()),
                  "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
                  "boolean": lambda: isinstance(value, bool),
                  "object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list)}
        if kind in checks and not checks[kind]():
            raise util.bad_request("invalid field: " + key)
        if "enum" in spec and value not in spec["enum"]:
            raise util.bad_request("invalid enum: " + key)
        if "minimum" in spec and value < spec["minimum"]:
            raise util.bad_request("invalid minimum: " + key)
        if kind == "array" and spec.get("items", {}).get("type") == "object":
            for item in value:
                _validate_arguments(spec["items"], item)
    if "limit" in arguments and not 1 <= arguments["limit"] <= 200:
        raise util.bad_request("limit must be between 1 and 200")
    if "before" in arguments and arguments["before"] < 0:
        raise util.bad_request("before must be nonnegative")


def _scope_valid(manager, scope):
    if not scope or scope.is_expired() or manager.get_scope(scope.scope_id) is not scope:
        raise util.KanpilotError("forbidden", "scope has expired or been revoked")


def _goal_continuation_task_guard(conn, scope, args):
    task = db.require_task(conn, scope.task_id)
    automation = db.task_extra(task).get("automation") or {}
    if (scope.role != "execution" or (scope.runtime_config or {}).get("permission_mode") != "auto"
            or args.get("action") != "accept" or args.get("target_permission_mode") != "auto"
            or task["run_id"] != scope.run_id or not db.task_schedulable(task)
            or db.paused(conn, "dispatch") or db.paused(conn, "monitor")
            or not automation or automation.get("manual_takeover")
            or task["status"] not in ("running", "needs_user")
            or (task["status"] == "needs_user" and task["failure_code"] != "execution_failed")):
        raise util.KanpilotError("forbidden", "目标自动续接已暂停或不属于当前自动执行轮次")


def _runtime_guard(conn, scope, tool, args):
    if args.get("harness_id", "pi_desktop") != "pi_desktop":
        raise util.bad_request("unsupported harness")
    if args.get("scope_id") != scope.scope_id:
        raise util.KanpilotError("forbidden", "scope_id does not match grant")
    mode = {"planner": "plan", "review": "plan", "execution": "goal"}.get(scope.role, "agent")
    if tool in ("kp_runtime_session_create", "kp_runtime_session_configure") and scope.role in ("planner", "review"):
        if args.get("interaction_mode") != "unattended":
            raise util.KanpilotError("forbidden", "planner/Review require unattended interaction policy")
    if tool == "kp_runtime_session_create":
        if not scope.project_path or Path(args["project_path"]).resolve() != Path(scope.project_path).resolve():
            raise util.KanpilotError("forbidden", "project_path does not match authorized snapshot")
        if args.get("runtime_config", scope.runtime_config) != scope.runtime_config:
            raise util.KanpilotError("forbidden", "runtime_config does not match authorized snapshot")
        if args["mode"] != mode:
            raise util.KanpilotError("forbidden", "session mode does not match role")
    session_id = args.get("session_id")
    if session_id and not runtime_operations.session_owned(conn, scope.scope_id, session_id):
        raise util.KanpilotError("forbidden", "session does not belong to scope")
    if tool == "kp_runtime_proposal_resolve" and args.get("goal_continuation"):
        _goal_continuation_task_guard(conn, scope, args)
        receipt = runtime_operations.get_operation(conn, scope.scope_id, args["operation_id"])
        if receipt is None or receipt["status"] != "accepted":
            from .pi_desktop_worker import _pending_goal_contract
            status = harness_plus_adapter.plus_status(scope.connection_file, session_id=session_id)
            raw = status.get("raw_status") or {}
            inner = raw.get("status") or raw
            proposal = _pending_goal_contract(
                {"plans": harness_plus_adapter.plus_proposals(scope.connection_file, session_id=session_id).get("proposals")},
                session_id, args["turn_id"])
            if (status.get("current_turn_id") != args["turn_id"]
                    or inner.get("planningState") not in ("planning", "awaiting_approval")
                    or status.get("pending_permission") or proposal is None
                    or any(proposal.get(source) != args.get(target) for source, target in
                           (("id", "proposal_id"), ("version", "version"), ("toolCallId", "tool_call_id")))):
                raise util.KanpilotError("conflict", "当前目标提案已变化或不再可自动批准")
    if tool == "kp_runtime_goal_report_get" and not (
        args.get("execution_id") or args.get("report_id")
    ):
        raise util.bad_request("Goal report get requires execution_id or report_id")
    if tool == "kp_runtime_session_configure":
        permission = (scope.runtime_config or {}).get("permission_mode")
        if scope.role == "review" and permission not in (
                constants.PI_DESKTOP_READONLY_PERMISSION_MODE,
                constants.PI_DESKTOP_REVIEW_PERMISSION_MODE):
            raise util.KanpilotError("forbidden", "unsupported review permission snapshot")
        elif scope.role == "planner" and permission not in (
                constants.PI_DESKTOP_READONLY_PERMISSION_MODE,
                constants.PI_DESKTOP_PLANNER_PERMISSION_MODE):
            raise util.KanpilotError("forbidden", "unsupported planner permission snapshot")
        if args["mode"] != mode or args["permission_mode"] != permission:
            raise util.KanpilotError("forbidden", "configuration exceeds authorized snapshot")
    if args.get("target_permission_mode") is not None and (scope.role != "execution" or args["target_permission_mode"] != (scope.runtime_config or {}).get("permission_mode")):
        raise util.KanpilotError("forbidden", "proposal permission exceeds authorized snapshot")
    if tool == "kp_runtime_prompt":
        for attachment in args.get("attachments", []):
            path = Path(attachment["path"])
            if not path.is_absolute() or not path.resolve().is_relative_to(Path(scope.project_path).resolve()):
                raise util.KanpilotError("forbidden", "attachment is outside the authorized project")
        if scope.role in ("planner", "review"):
            if args.get("expected_interaction_mode") != "unattended":
                raise util.KanpilotError("forbidden", "planner/Review prompt require atomic unattended precondition")
            from . import plus_interaction
            connection_file = (scope.connection_file or (scope.runtime_config or {}).get("connection_file")
                               or str(Path(constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE).expanduser()))
            policy = harness_plus_adapter.plus_messages(connection_file, session_id=session_id, limit=1)
            session = {"id": policy.get("session_id"), **{
                key: policy[key] for key in ("interactionMode", "interactionPolicyVersion") if key in policy}}
            plus_interaction.require_unattended({"session": session}, session_id)


def _task_identity(conn, scope):
    identity = scope.bound_claim or {}
    latest = None
    if not identity:
        # Claim ownership is durable and survives loss of the binding response.
        for row in conn.execute("SELECT result_json FROM runtime_operations WHERE scope_id=? AND tool='kp_task_claim' AND status='accepted' ORDER BY updated_at DESC", (scope.scope_id,)):
            result = util.jload(row["result_json"]) or {}
            if result.get("claimed"):
                if latest is None:
                    latest = {"request_id": result.get("request_id"), "run_id": result.get("run_id"), "task_id": result.get("context", {}).get("task_id")}
                request = db.get_request(conn, result.get("request_id"))
                if request and request["status"] == "claimed":
                    identity = {"request_id": request["request_id"], "run_id": request["run_id"], "task_id": request["task_id"]}
                    break
    return identity or latest or {"request_id": scope.request_id, "run_id": scope.run_id, "task_id": scope.task_id}


def _task_guard(conn, manager, scope, tool, args):
    _scope_valid(manager, scope)
    identity = _task_identity(conn, scope)
    task_id, request_id, run_id = identity.get("task_id"), identity.get("request_id"), identity.get("run_id")
    if args.get("task_id") and args["task_id"] != task_id:
        raise util.KanpilotError("forbidden", "task is outside the grant")
    if args.get("request_id") and args["request_id"] != request_id:
        raise util.KanpilotError("forbidden", "request is outside the grant")
    if not task_id and tool not in ("kp_task_request",):
        raise util.KanpilotError("forbidden", "scope has no authorized task")
    observed_run = run_id
    if scope.role == "monitor" and tool in ("kp_task_session", "kp_task_messages", "kp_task_observe"):
        observed_run = db.require_task(conn, task_id)["run_id"]
    if args.get("run_id") and args["run_id"] != observed_run:
        raise util.KanpilotError("forbidden", "run is outside the grant")
    if not TOOLS_REGISTRY[tool].get("readOnlyHint", False):
        request = db.get_request(conn, request_id) if request_id else None
        expiry = util.parse_ts(request["lease_expires_at"]) if request else None
        if (not request or request["status"] != "claimed" or request["run_id"] != run_id
                or request["stage"] != scope.role or not expiry or expiry <= util.now_dt()):
            raise util.KanpilotError("stale_run", "claim is no longer active")
    if tool == "kp_task_advance":
        if scope.role != "monitor" or (args["expected_current"], args["target"]) not in {
                ("in-progress", "in-review"), ("in-review", "in-review"), ("done", "done")}:
            raise util.KanpilotError("forbidden", "monitor transition is not allowed")


def dispatch_tool_call(db_path, scope_manager, token_type, scope, tool_name, arguments):
    spec = TOOLS_REGISTRY.get(tool_name)
    if not spec:
        raise util.KanpilotError("not_found", "unknown tool: " + tool_name)
    if not scope_manager.check_tool_permission(token_type, scope, tool_name):
        raise util.KanpilotError("forbidden", "tool is not permitted for this role")
    # Admin management is separate: runtime and task operations always need a scope.
    _scope_valid(scope_manager, scope)
    _validate_arguments(spec["inputSchema"], arguments)
    if tool_name.startswith("kp_runtime_"):
        conn_file = scope.connection_file or (scope.runtime_config or {}).get("connection_file") or str(Path(constants.DEFAULT_PI_DESKTOP_CONNECTION_FILE).expanduser())
        ctx = cli.Context(db_path)
        try:
            _runtime_guard(ctx.conn, scope, tool_name, arguments)
            def authorization_guard(conn):
                _scope_valid(scope_manager, scope)
                if tool_name == "kp_runtime_proposal_resolve" and arguments.get("goal_continuation"):
                    _goal_continuation_task_guard(conn, scope, arguments)
            ctx.conn.authorization_guard = authorization_guard
            readers = {
                "kp_runtime_capabilities": lambda: harness_plus_adapter.plus_capabilities(conn_file),
                "kp_runtime_catalog": lambda: harness_plus_adapter.plus_catalog(conn_file, provider_id=arguments.get("provider_id")),
                "kp_runtime_status": lambda: harness_plus_adapter.plus_status(conn_file, session_id=arguments["session_id"]),
                "kp_runtime_turn_get": lambda: harness_plus_adapter.plus_turn_get(conn_file, session_id=arguments["session_id"], turn_id=arguments["turn_id"], require_identity=arguments.get("require_identity", False)),
                "kp_runtime_messages": lambda: harness_plus_adapter.plus_messages(conn_file, session_id=arguments["session_id"], before=arguments.get("before"), limit=arguments.get("limit")),
                "kp_runtime_proposals": lambda: harness_plus_adapter.plus_proposals(conn_file, session_id=arguments["session_id"], turn_id=arguments.get("turn_id")),
                "kp_runtime_goal_report_list": lambda: harness_plus_adapter.plus_goal_report_list(conn_file, session_id=arguments["session_id"]),
                "kp_runtime_goal_report_get": lambda: harness_plus_adapter.plus_goal_report_get(conn_file, session_id=arguments["session_id"], execution_id=arguments.get("execution_id"), report_id=arguments.get("report_id")),
            }
            if tool_name in readers:
                return readers[tool_name]()
            if tool_name == "kp_runtime_attach_mcp":
                grant = scope_manager.get_task_grant_file(scope.scope_id)
                if not grant or not grant.is_file():
                    raise util.KanpilotError("not_found", "task grant file is missing")
            op = runtime_operations.reserve_downstream(ctx.conn, scope.scope_id, arguments["operation_id"], "pi_desktop", tool_name, {key: value for key, value in arguments.items() if key != "goal_continuation"})
            if not op["send"]:
                if op["status"] == "accepted":
                    return op["result"]
                raise util.KanpilotError("conflict", "operation outcome is unknown; do not resend")
        finally:
            ctx.close()
        # No SQLite transaction or service-wide lock spans a Plus callback.
        sid = arguments.get("session_id")
        writers = {
            "kp_runtime_session_create": lambda: harness_plus_adapter.plus_session_create(conn_file, project_path=scope.project_path, mode=arguments["mode"], runtime_config=scope.runtime_config or {}, title=arguments.get("title"), project_name=arguments.get("project_name"), interaction_mode=arguments.get("interaction_mode")),
            "kp_runtime_session_configure": lambda: harness_plus_adapter.plus_session_configure(conn_file, session_id=sid, mode=arguments["mode"], permission_mode=arguments["permission_mode"], interaction_mode=arguments.get("interaction_mode")),
            "kp_runtime_attach_mcp": lambda: harness_plus_adapter.plus_attach_mcp(conn_file, session_id=sid, bridge_file=str(grant)),
            "kp_runtime_prompt": lambda: harness_plus_adapter.plus_prompt(conn_file, session_id=sid, content=arguments["content"], attachments=arguments.get("attachments"), expected_interaction_mode=arguments.get("expected_interaction_mode")),
            "kp_runtime_stop": lambda: harness_plus_adapter.plus_stop(conn_file, session_id=sid),
            "kp_runtime_abort": lambda: harness_plus_adapter.plus_abort(conn_file, session_id=sid),
            "kp_runtime_proposal_resolve": lambda: harness_plus_adapter.plus_proposal_resolve(conn_file, session_id=sid, turn_id=arguments["turn_id"], proposal_id=arguments["proposal_id"], tool_call_id=arguments["tool_call_id"], version=arguments["version"], action=arguments["action"], target_permission_mode=arguments.get("target_permission_mode")),
        }
        result = writers[tool_name]()  # Exceptions leave the durable receipt unknown.
        if tool_name == "kp_runtime_attach_mcp":
            expected = mcp_scopes.TASK_ROLE_TOOLS.get(scope.role, set())
            if result.get("server_name") != "kaneopilot" or not expected.issubset(set(result.get("tool_names", []))):
                raise util.KanpilotError("validation_failed", "attached tool set does not match role")
        ctx = cli.Context(db_path)
        try:
            with db.write_txn(ctx.conn):
                runtime_operations.complete_operation(ctx.conn, scope.scope_id, arguments["operation_id"], result=result, session_id=result.get("session_id", sid), turn_id=result.get("turn_id"), proposal_id=result.get("proposal_id"))
        finally:
            ctx.close()
        return result

    ctx = cli.Context(db_path)
    try:
        if tool_name == "kp_task_claim":
            with scope_manager.claim_lock(scope.scope_id):
                _scope_valid(scope_manager, scope)
                if scope.request_id and arguments.get("request_id", scope.request_id) != scope.request_id:
                    raise util.KanpilotError("forbidden", "request is outside the grant")
                payload = {"stage": scope.role, "executor": "kaneo-" + scope.role,
                           "scope_id": scope.scope_id, "operation_id": arguments["operation_id"]}
                if scope.task_id:
                    payload["authorized_task_id"] = scope.task_id
                if arguments.get("request_id") or scope.request_id:
                    payload["request_id"] = arguments.get("request_id") or scope.request_id
                ctx.conn.authorization_guard = lambda conn: _scope_valid(scope_manager, scope)
                if scope.bound_claim:
                    bound = scope.bound_claim
                    prior = db.get_request(ctx.conn, bound.get("request_id"))
                    if not prior or prior["status"] != "claimed" or prior["run_id"] != bound.get("run_id"):
                        scope_manager.unbind_claim(scope.scope_id, bound["request_id"])
                result = cmd_exec.cmd_mcp_claim(ctx, payload)
                if result.get("claimed"):
                    live = db.get_request(ctx.conn, result["request_id"])
                    if live and live["status"] == "claimed" and live["run_id"] == result["run_id"]:
                        scope_manager.bind_claim(scope.scope_id, result["request_id"], result["run_id"], result["context"]["task_id"])
                return result
        _task_guard(ctx.conn, scope_manager, scope, tool_name, arguments)
        ctx.conn.authorization_guard = lambda conn: _task_guard(conn, scope_manager, scope, tool_name, arguments)
        payload = dict(arguments)
        if tool_name == "kp_task_advance":
            payload["run_id"] = _task_identity(ctx.conn, scope)["run_id"]
        command = TASK_COMMANDS[tool_name]
        cli.validate_payload(command, payload)
        result = cli.COMMANDS[command][1](ctx, payload)
        if tool_name == "kp_task_report":
            scope_manager.unbind_claim(scope.scope_id, arguments["request_id"])
        return result
    finally:
        ctx.close()
