"""Scope and grant management for KaneoPilot MCP Server (protocol §5.25 / T7.4).

Scope hierarchy and access control:
- Admin token: App / runner only. Allows registering/renewing/revoking scopes on /admin/scopes.
- Runtime grant (control domain): allows calling kp_runtime_* tools to control harness.
- Task grant (task domain): allows calling kp_task_* tools within authorized role/task/claim context.
- Task domain clients cannot call control domain tools.
- Grant files are written with 0600 permissions, containing server url, protocol_version, epoch, token, and scope_id.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
import uuid
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from . import constants, util

# Role to allowed kp_runtime_* tools
RUNTIME_ROLE_TOOLS: Dict[str, Set[str]] = {
    "catalog": {
        "kp_runtime_capabilities",
        "kp_runtime_catalog",
    },
    "planner": {
        "kp_runtime_capabilities",
        "kp_runtime_catalog",
        "kp_runtime_session_create",
        "kp_runtime_session_configure",
        "kp_runtime_attach_mcp",
        "kp_runtime_prompt",
        "kp_runtime_status",
        "kp_runtime_turn_get",
        "kp_runtime_messages",
        "kp_runtime_stop",
        "kp_runtime_abort",
        "kp_runtime_proposals",
        "kp_runtime_proposal_resolve",
    },
    "dispatch": {
        "kp_runtime_capabilities",
        "kp_runtime_catalog",
        "kp_runtime_session_create",
        "kp_runtime_session_configure",
        "kp_runtime_attach_mcp",
        "kp_runtime_prompt",
        "kp_runtime_status",
        "kp_runtime_turn_get",
        "kp_runtime_messages",
        "kp_runtime_stop",
        "kp_runtime_abort",
        "kp_runtime_proposals",
        "kp_runtime_proposal_resolve",
    },
    "monitor": {
        "kp_runtime_capabilities",
        "kp_runtime_catalog",
        "kp_runtime_session_create",
        "kp_runtime_session_configure",
        "kp_runtime_attach_mcp",
        "kp_runtime_prompt",
        "kp_runtime_status",
        "kp_runtime_turn_get",
        "kp_runtime_messages",
        "kp_runtime_stop",
        "kp_runtime_abort",
        "kp_runtime_proposals",
        "kp_runtime_proposal_resolve",
    },
    "review": {
        "kp_runtime_capabilities",
        "kp_runtime_catalog",
        "kp_runtime_session_create",
        "kp_runtime_session_configure",
        "kp_runtime_attach_mcp",
        "kp_runtime_prompt",
        "kp_runtime_status",
        "kp_runtime_turn_get",
        "kp_runtime_messages",
        "kp_runtime_stop",
        "kp_runtime_abort",
        "kp_runtime_proposals",
        "kp_runtime_proposal_resolve",
    },
    "execution": {
        "kp_runtime_capabilities",
        "kp_runtime_catalog",
        "kp_runtime_session_create",
        "kp_runtime_session_configure",
        "kp_runtime_attach_mcp",
        "kp_runtime_prompt",
        "kp_runtime_status",
        "kp_runtime_turn_get",
        "kp_runtime_messages",
        "kp_runtime_stop",
        "kp_runtime_abort",
        "kp_runtime_proposals",
        "kp_runtime_proposal_resolve",
    },
}

# Role to allowed kp_task_* tools
TASK_ROLE_TOOLS: Dict[str, Set[str]] = {
    "catalog": set(),
    "planner": {
        "kp_task_get",
        "kp_task_plan",
        "kp_task_messages",
    },
    "execution": {
        "kp_task_get",
        "kp_task_plan",
        "kp_task_messages",
    },
    "review": {
        "kp_task_get",
        "kp_task_plan",
        "kp_task_messages",
    },
    "dispatch": {
        "kp_task_claim",
        "kp_task_get",
        "kp_task_plan",
        "kp_task_request",
        "kp_task_session",
        "kp_task_messages",
        "kp_task_heartbeat",
        "kp_task_report",
        "kp_task_dispatch_prepare",
        "kp_task_dispatch_begin_send",
        "kp_task_dispatch_start",
        "kp_task_dispatch_confirm",
        "kp_task_dispatch_fail",
    },
    "monitor": {
        "kp_task_claim",
        "kp_task_get",
        "kp_task_plan",
        "kp_task_request",
        "kp_task_session",
        "kp_task_messages",
        "kp_task_heartbeat",
        "kp_task_report",
        "kp_task_recheck",
        "kp_task_observe",
        "kp_task_advance",
    },
}

VALID_ROLES = set(RUNTIME_ROLE_TOOLS.keys())


class Scope:
    def __init__(
        self,
        scope_id: str,
        invocation_id: str,
        role: str,
        expires_at: str,
        request_id: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        project_path: Optional[str] = None,
        runtime_config: Optional[dict] = None,
        connection_file: Optional[str] = None,
    ):
        self.scope_id = scope_id
        self.invocation_id = invocation_id
        self.role = role
        self.expires_at = expires_at
        self.initial_expires_at = expires_at
        self.request_id = request_id
        self.run_id = run_id
        self.task_id = task_id
        self.project_path = project_path
        self.runtime_config = dict(runtime_config) if isinstance(runtime_config, dict) else None
        self.connection_file = connection_file
        self.bound_claim: Optional[dict] = None
        self.created_at = util.to_iso(util.now_dt())

    def is_expired(self) -> bool:
        exp = util.parse_ts(self.expires_at)
        return exp is None or util.now_dt() >= exp

    def to_dict(self) -> dict:
        return {
            "scope_id": self.scope_id,
            "invocation_id": self.invocation_id,
            "role": self.role,
            "expires_at": self.expires_at,
            "initial_expires_at": self.initial_expires_at,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "project_path": self.project_path,
            "runtime_config": self.runtime_config,
            "connection_file": self.connection_file,
            "bound_claim": self.bound_claim,
            "created_at": self.created_at,
        }


class ScopeManager:
    def __init__(
        self,
        app_support_dir: Optional[Path] = None,
        admin_token: Optional[str] = None,
        epoch: Optional[str] = None,
    ):
        self.app_support = (
            Path(app_support_dir).expanduser()
            if app_support_dir
            else constants.app_support()
        )
        self.mcp_dir = self.app_support / "mcp"
        self.grants_dir = self.mcp_dir / "grants"
        self._ensure_secure_dirs()

        self.epoch = epoch or uuid.uuid4().hex
        self.admin_token = admin_token or self._load_or_create_admin_token()

        self.scopes: Dict[str, Scope] = {}
        # token -> (token_type, scope_id) where token_type in ('admin', 'runtime', 'task')
        self.tokens: Dict[str, Tuple[str, str]] = {}
        # scope_id -> {"runtime_token": str, "task_token": str, "runtime_grant_file": Path, "task_grant_file": Path}
        self.grant_records: Dict[str, dict] = {}
        self._lock = threading.RLock()
        self._claim_locks: Dict[str, threading.RLock] = {}

    def _ensure_secure_dirs(self) -> None:
        owner = os.geteuid()
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory_flag is None:
            raise OSError("O_NOFOLLOW and O_DIRECTORY are required for MCP state directories")
        for path in (self.mcp_dir, self.grants_dir):
            path.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_RDONLY | directory_flag | nofollow)
            try:
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode):
                    raise OSError(f"MCP state path must be a real directory: {path}")
                if info.st_uid != owner:
                    raise PermissionError(f"MCP state directory has a different owner: {path}")
                os.fchmod(fd, 0o700)
                info = os.fstat(fd)
                if info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700:
                    raise PermissionError(f"MCP state directory is not owner-only: {path}")
            finally:
                os.close(fd)

    def _load_or_create_admin_token(self) -> str:
        token_file = self.mcp_dir / "server.token"
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("O_NOFOLLOW is required for the MCP admin token")

        def read_existing() -> str:
            fd = os.open(str(token_file), os.O_RDONLY | nofollow)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                mode = stat.S_IMODE(info.st_mode)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or mode not in (0o400, 0o600):
                    raise PermissionError("MCP admin token file must be owner-owned and mode 0400 or 0600")
                token = stream.read().strip()
            if not re.fullmatch(r"[0-9a-f]{64}", token):
                raise ValueError("MCP admin token file is invalid")
            return token

        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
            fd = os.open(str(token_file), flags, 0o600)
        except FileExistsError:
            return read_existing()
        token = secrets.token_hex(32)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    raise PermissionError("MCP admin token file is not a regular file owned by this user")
                os.fchmod(stream.fileno(), 0o600)
                stream.write(token)
        except Exception:
            try:
                token_file.unlink()
            except OSError:
                pass
            raise
        return token

    def register_scope(self, payload: dict, server_url: str) -> dict:
        with self._lock:
            return self._register_scope_locked(payload, server_url)

    def _register_scope_locked(self, payload: dict, server_url: str) -> dict:
        """Register a scope and generate grant files.

        Payload fields:
          scope_id: str (required)
          invocation_id: str (required)
          role: str (required, in VALID_ROLES)
          expires_at: str (required, ISO 8601)
          request_id: Optional[str]
          run_id: Optional[str]
          task_id: Optional[str]
          project_path: Optional[str]
          runtime_config: Optional[dict]
          connection_file: Optional[str]
        """
        scope_id = payload.get("scope_id")
        if not isinstance(scope_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope_id):
            raise util.bad_request("scope_id 只允许 1-128 位字母、数字、下划线或连字符")
        invocation_id = payload.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise util.bad_request("invocation_id 必须为非空字符串")
        role = payload.get("role")
        if role not in VALID_ROLES:
            raise util.bad_request(
                f"role 不合法：{role}（可用：{', '.join(sorted(VALID_ROLES))}）"
            )
        expires_at = payload.get("expires_at")
        expires = util.parse_ts(expires_at)
        if expires is None or expires <= util.now_dt():
            raise util.bad_request("expires_at 必须是未来的 ISO 8601 时间")

        # Check existing scope with same ID
        with self._lock:
            if scope_id in self.scopes:
                existing = self.scopes[scope_id]
                expected = {
                    "invocation_id": invocation_id,
                    "role": role,
                    "expires_at": expires_at,
                    "request_id": payload.get("request_id"),
                    "run_id": payload.get("run_id"),
                    "task_id": payload.get("task_id"),
                    "project_path": payload.get("project_path"),
                    "runtime_config": payload.get("runtime_config"),
                    "connection_file": payload.get("connection_file"),
                }
                same_identity = all(
                    getattr(existing, key) == value
                    for key, value in expected.items()
                    if key != "expires_at"
                )
                if same_identity and not existing.is_expired() and existing.expires_at == expires_at:
                    records = self.grant_records.get(scope_id, {})
                    return {
                        "scope_id": scope_id,
                        "runtime_grant_file": str(records.get("runtime_grant_file")) if records.get("runtime_grant_file") else None,
                        "task_grant_file": str(records.get("task_grant_file")) if records.get("task_grant_file") else None,
                    }
                if same_identity and existing.is_expired():
                    self.revoke_scope(scope_id)
                else:
                    raise util.KanpilotError("conflict", f"scope_id {scope_id} 已存在但授权配置不同")

        scope = Scope(
            scope_id=scope_id,
            invocation_id=invocation_id,
            role=role,
            expires_at=expires_at,
            request_id=payload.get("request_id"),
            run_id=payload.get("run_id"),
            task_id=payload.get("task_id"),
            project_path=payload.get("project_path"),
            runtime_config=payload.get("runtime_config"),
            connection_file=payload.get("connection_file"),
        )
        records: dict = {}

        # 1. Runtime grant (control domain)
        runtime_token = secrets.token_hex(24)
        records["runtime_token"] = runtime_token
        runtime_grant_file = self.grants_dir / f"{scope_id}_runtime.json"
        runtime_grant_data = {
            "server_name": constants.MCP_SERVER_NAME,
            "url": server_url,
            "protocol_version": constants.MCP_PROTOCOL_VERSION,
            "epoch": self.epoch,
            "token": runtime_token,
            "scope_id": scope_id,
            "role": role,
            "domain": "runtime",
            "expires_at": expires_at,
        }
        records["runtime_grant_file"] = runtime_grant_file

        # 2. Task grant (task domain) - if role has task tools
        task_grant_file = None
        if TASK_ROLE_TOOLS.get(role):
            task_token = secrets.token_hex(24)
            records["task_token"] = task_token
            task_grant_file = self.grants_dir / f"{scope_id}_task.json"
            task_grant_data = {
                "server_name": constants.MCP_SERVER_NAME,
                "url": server_url,
                "protocol_version": constants.MCP_PROTOCOL_VERSION,
                "epoch": self.epoch,
                "token": task_token,
                "scope_id": scope_id,
                "role": role,
                "domain": "task",
                "expires_at": expires_at,
            }
            records["task_grant_file"] = task_grant_file

        try:
            self._write_secure_json(runtime_grant_file, runtime_grant_data)
            if task_grant_file is not None:
                self._write_secure_json(task_grant_file, task_grant_data)
        except Exception:
            for grant_path in (runtime_grant_file, task_grant_file):
                if grant_path is not None and grant_path.exists() and not grant_path.is_symlink():
                    grant_path.unlink()
            raise

        self.scopes[scope_id] = scope
        self.tokens[runtime_token] = ("runtime", scope_id)
        if records.get("task_token"):
            self.tokens[records["task_token"]] = ("task", scope_id)
        self.grant_records[scope_id] = records

        return {
            "scope_id": scope_id,
            "runtime_grant_file": str(runtime_grant_file) if runtime_grant_file else None,
            "task_grant_file": str(task_grant_file) if task_grant_file else None,
        }

    def _write_secure_json(self, path: Path, data: dict) -> None:
        blob = json.dumps(data, indent=2, ensure_ascii=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        elif path.is_symlink():
            raise OSError(f"拒绝写入符号链接 grant 文件：{path}")
        fd = os.open(str(path), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"grant 文件必须是普通文件：{path}")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = -1
                f.write(blob)
        finally:
            if fd >= 0:
                os.close(fd)

    def revoke_scope(self, scope_id: str) -> bool:
        with self._lock:
            scope = self.scopes.pop(scope_id, None)
            if scope is None:
                return False
            records = self.grant_records.pop(scope_id, {})
            for token_key in ("runtime_token", "task_token"):
                tok = records.get(token_key)
                if tok and tok in self.tokens:
                    self.tokens.pop(tok, None)
        for path_key in ("runtime_grant_file", "task_grant_file"):
            p = records.get(path_key)
            if p and isinstance(p, Path) and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        return True

    def renew_scope(self, scope_id: str, expires_at: str) -> dict:
        scope = self.scopes.get(scope_id)
        if scope is None:
            raise util.KanpilotError("not_found", f"scope_id {scope_id} 不存在")
        expires = util.parse_ts(expires_at)
        if expires is None or expires <= util.now_dt():
            raise util.bad_request("expires_at 必须是未来的 ISO 8601 时间")
        with self._lock:
            scope = self.scopes.get(scope_id)
            if scope is None:
                raise util.KanpilotError("not_found", f"scope_id {scope_id} 不存在")
            initial_expiry = util.parse_ts(scope.initial_expires_at)
            if initial_expiry is None or expires > initial_expiry:
                raise util.bad_request("续期不得超过 scope 初始 expires_at")

            records = self.grant_records.get(scope_id, {})
            grant_updates = []
            for domain in ("runtime", "task"):
                grant_path = records.get(f"{domain}_grant_file")
                expected_token = records.get(f"{domain}_token")
                if grant_path is None:
                    continue
                grant_path = Path(grant_path)
                if not hasattr(os, "O_NOFOLLOW") and grant_path.is_symlink():
                    raise OSError(f"拒绝读取符号链接 grant 文件：{grant_path}")
                fd = os.open(
                    str(grant_path),
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                )
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd)
                    raise OSError(f"grant 文件必须是普通文件：{grant_path}")
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    grant_data = json.load(stream)
                if (
                    grant_data.get("scope_id") != scope_id
                    or grant_data.get("domain") != domain
                    or grant_data.get("token") != expected_token
                ):
                    raise util.KanpilotError("conflict", f"{domain} grant 与 scope 授权记录不匹配")
                grant_data["expires_at"] = expires_at
                grant_updates.append((grant_path, grant_data))
            if not grant_updates:
                raise util.KanpilotError("conflict", f"scope {scope_id} 没有有效 grant 文件")
            for grant_path, grant_data in grant_updates:
                self._write_secure_json(grant_path, grant_data)
            scope.expires_at = expires_at
            return scope.to_dict()

    def get_scope(self, scope_id: Optional[str]) -> Optional[Scope]:
        if not scope_id:
            return None
        with self._lock:
            return self.scopes.get(scope_id)

    def get_task_grant_file(self, scope_id: str) -> Optional[Path]:
        with self._lock:
            records = self.grant_records.get(scope_id, {})
            p = records.get("task_grant_file")
            return Path(p) if p else None

    def claim_lock(self, scope_id: str) -> threading.RLock:
        """Return the process-local lock used to serialize a scope's claim flow."""
        with self._lock:
            return self._claim_locks.setdefault(scope_id, threading.RLock())

    def authenticate(self, token: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
        """Authenticate raw token string.

        Returns:
          ("admin", None) if admin token
          ("runtime", scope_id) if runtime grant
          ("task", scope_id) if task grant
          None if invalid or expired
        """
        if not token or not isinstance(token, str):
            return None
        if token == self.admin_token:
            return "admin", None
        with self._lock:
            entry = self.tokens.get(token)
            if not entry:
                return None
            token_type, scope_id = entry
            scope = self.scopes.get(scope_id)
            if not scope or scope.is_expired():
                return None
            return token_type, scope_id

    def authenticate_bearer(
        self, header_val: Optional[str]
    ) -> Tuple[Optional[str], Optional[Scope]]:
        """Verify Authorization Bearer header.

        Returns:
          ("admin", None) if admin token
          ("runtime", scope) if runtime grant
          ("task", scope) if task grant
          (None, None) if invalid or expired
        """
        if not header_val or not isinstance(header_val, str):
            return None, None
        parts = header_val.strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None, None
        token = parts[1]

        if token == self.admin_token:
            return "admin", None

        with self._lock:
            token_entry = self.tokens.get(token)
            if token_entry is None:
                return None, None
            token_type, scope_id = token_entry
            scope = self.scopes.get(scope_id)
            if scope is None or scope.is_expired():
                return None, None
            return token_type, scope

    def check_tool_permission(
        self, token_type: str, scope: Optional[Scope], tool_name: str
    ) -> bool:
        """Check if caller has permission to call tool_name."""
        if token_type == "admin":
            # Admin can call all tools if needed
            return True

        if scope is None:
            return False

        if token_type == "runtime":
            allowed = RUNTIME_ROLE_TOOLS.get(scope.role, set())
            return tool_name in allowed

        if token_type == "task":
            allowed = TASK_ROLE_TOOLS.get(scope.role, set())
            return tool_name in allowed

        return False

    def bind_claim(
        self, scope_id: str, request_id: str, run_id: str, task_id: str
    ) -> None:
        with self._lock:
            scope = self.scopes.get(scope_id)
            if scope is None:
                raise util.KanpilotError("not_found", f"scope {scope_id} 不存在")
            if scope.bound_claim is not None:
                if scope.bound_claim.get("request_id") == request_id and scope.bound_claim.get("run_id") == run_id:
                    return
                raise util.KanpilotError("conflict", f"scope {scope_id} 当前已有有效领取绑定：{scope.bound_claim.get('request_id')}，无法同时领取多项")
            scope.bound_claim = {"request_id": request_id, "run_id": run_id, "task_id": task_id, "claimed_at": util.to_iso(util.now_dt())}

    def unbind_claim(self, scope_id: str, request_id: str) -> None:
        with self._lock:
            scope = self.scopes.get(scope_id)
            if scope is not None and scope.bound_claim is not None and scope.bound_claim.get("request_id") == request_id:
                scope.bound_claim = None
