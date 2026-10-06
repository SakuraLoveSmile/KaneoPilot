"""Project and repository mapping configuration (protocol §5.26 / T1).

Local project mapping management:
- Stored in SQLite settings key ``project_mappings``.
- Resolves effective repository paths, Git repository kind, and mapping states.
- Authoritative project identity is ``project_id``.
- Provides safe legacy defaults when not configured (revision=0).
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Optional

from . import bridge, constants, db, remote, util

SETTINGS_KEY = "project_mappings"
CURRENT_VERSION = 1


def _legacy_default_items() -> dict[str, dict]:
    """旧五项目只读默认映射字典（project_id -> dict）。"""
    items = {}
    for name, pid in constants.PROJECTS.items():
        repo = str(constants.repo_path_for(name))
        items[pid] = {
            "project_name": name,
            "repository": repo,
            "enabled": True,
            "updated_at": None,
        }
    return items


def load_raw_mappings(conn: sqlite3.Connection) -> tuple[int, str, dict[str, dict], bool]:
    """读取已存储的 project_mappings 设置。

    返回 (revision, workspace_id, items_dict, is_saved)。
    未保存时返回 (0, constants.WORKSPACE_ID, legacy_items, False)。
    """
    stored = db.get_setting(conn, SETTINGS_KEY)
    if isinstance(stored, dict) and isinstance(stored.get("items"), dict):
        revision = int(stored.get("revision", 0))
        workspace_id = str(stored.get("workspace_id") or constants.WORKSPACE_ID)
        items = dict(stored["items"])
        return revision, workspace_id, items, True
    return 0, constants.WORKSPACE_ID, _legacy_default_items(), False


def inspect_repository_path(raw_path: Optional[str]) -> tuple[Optional[str], Optional[str], str, Optional[str]]:
    """规范化并检查本地仓库路径。

    返回 (canonical_path, repository_kind, mapping_state, blocked_reason)。
    mapping_state: "unmapped" | "available" | "missing" | "unreadable"
    repository_kind: "git" | "directory" | None
    """
    if not raw_path or not str(raw_path).strip():
        return None, None, "unmapped", "尚未选择仓库"

    expanded = os.path.expanduser(str(raw_path).strip())
    path = Path(expanded)

    try:
        resolved = path.resolve()
    except Exception as exc:
        return str(path.absolute()), None, "unreadable", f"仓库路径不可读取：{exc}"

    canonical_str = str(resolved)

    if not resolved.exists():
        return canonical_str, None, "missing", f"仓库目录不存在：{canonical_str}"

    if not resolved.is_dir():
        return canonical_str, None, "missing", f"仓库路径不是有效目录：{canonical_str}"

    # 检查是否可读
    try:
        _ = os.listdir(resolved)
    except PermissionError:
        return canonical_str, None, "unreadable", f"仓库目录无读取权限：{canonical_str}"
    except Exception as exc:
        return canonical_str, None, "unreadable", f"仓库目录读取异常：{exc}"

    # 检查是否为 Git 仓库或 Git 子目录
    git_root = _find_git_root(resolved)
    if git_root is not None:
        return str(git_root), "git", "available", None

    return canonical_str, "directory", "available", None


def _find_git_root(dir_path: Path) -> Optional[Path]:
    """寻找 Git 仓库根目录。"""
    try:
        res = subprocess.run(
            ["git", "-C", str(dir_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            candidate = Path(res.stdout.strip()).resolve()
            if candidate.is_dir():
                return candidate
    except Exception:
        pass

    # 回退：向上检查 .git 目录/文件
    current = dir_path
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def _get_active_counts(conn: sqlite3.Connection, project_id: str) -> tuple[int, int]:
    """统计指定 project_id 的未完成任务数与活跃请求数。"""
    task_count = 0
    request_count = 0
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id=? AND save_state='active' AND status NOT IN ('done')",
            (project_id,),
        )
        row = cur.fetchone()
        if row:
            task_count = int(row[0])

        cur = conn.execute(
            """SELECT COUNT(r.request_id) FROM requests r
               JOIN tasks t ON r.task_id = t.task_id
               WHERE t.project_id=? AND r.status IN ('queued', 'claimed')""",
            (project_id,),
        )
        row = cur.fetchone()
        if row:
            request_count = int(row[0])
    except Exception:
        pass
    return task_count, request_count


def get_effective_mapping(conn: sqlite3.Connection, project_id: Optional[str]) -> Optional[dict]:
    """获取单个 project_id 的当前有效映射。"""
    if not project_id:
        return None
    revision, workspace_id, items, is_saved = load_raw_mappings(conn)
    entry = items.get(str(project_id))
    if not entry:
        return {
            "project_id": str(project_id),
            "project_name": "",
            "repository": None,
            "enabled": False,
            "revision": revision,
            "workspace_id": workspace_id,
            "mapping_state": "unmapped",
            "repository_kind": None,
            "blocked_reason": "尚未选择仓库",
            "updated_at": None,
        }

    raw_repo = entry.get("repository")
    enabled = bool(entry.get("enabled", True))
    canonical_path, repo_kind, state, blocked = inspect_repository_path(raw_repo)

    if not enabled and state == "available":
        state = "disabled"
        blocked = "已停用新规划"

    return {
        "project_id": str(project_id),
        "project_name": entry.get("project_name") or "",
        "repository": canonical_path if state == "available" or not enabled else raw_repo,
        "enabled": enabled,
        "revision": revision,
        "workspace_id": workspace_id,
        "mapping_state": state,
        "repository_kind": repo_kind,
        "blocked_reason": blocked,
        "updated_at": entry.get("updated_at"),
    }


def get_project_mappings(conn: sqlite3.Connection, *, refresh_remote: bool = False) -> dict:
    """实现 get-project-mappings 命令。"""
    revision, workspace_id, saved_items, is_saved = load_raw_mappings(conn)

    remote_status = "not_requested"
    remote_fetched_at = None
    remote_error = None
    remote_projects: dict[str, str] = {}  # project_id -> project_name

    if refresh_remote:
        try:
            client = remote.get_client()
            project_list = bridge.project_list(client, workspace_id)
            remote_status = "ok"
            remote_fetched_at = util.now_iso()
            for p in project_list:
                pid = str(p.get("id") or "")
                pname = str(p.get("name") or "")
                if pid:
                    remote_projects[pid] = pname
        except Exception as exc:
            remote_status = "error"
            remote_error = str(exc)

    all_pids = list(saved_items.keys())
    for rpid in remote_projects:
        if rpid not in all_pids:
            all_pids.append(rpid)

    items_output = []
    for pid in all_pids:
        in_saved = pid in saved_items
        in_remote = pid in remote_projects

        if in_saved:
            entry = saved_items[pid]
            pname = remote_projects.get(pid) or entry.get("project_name") or ""
            raw_repo = entry.get("repository")
            enabled = bool(entry.get("enabled", True))
            origin = "saved" if is_saved else "legacy"
            updated_at = entry.get("updated_at")
        else:
            pname = remote_projects.get(pid, "")
            raw_repo = None
            enabled = False
            origin = "remote_only"
            updated_at = None

        canonical_path, repo_kind, state, blocked = inspect_repository_path(raw_repo)
        if raw_repo and not enabled and state == "available":
            state = "disabled"
            blocked = "已停用新规划"

        task_count, req_count = _get_active_counts(conn, pid)

        items_output.append({
            "project_id": pid,
            "project_name": pname,
            "repository": canonical_path if state == "available" else raw_repo,
            "enabled": enabled,
            "origin": origin,
            "mapping_state": state,
            "repository_kind": repo_kind,
            "blocked_reason": blocked,
            "active_task_count": task_count,
            "active_request_count": req_count,
            "updated_at": updated_at,
        })

    return {
        "version": CURRENT_VERSION,
        "revision": revision,
        "workspace_id": workspace_id,
        "items": items_output,
        "remote": {
            "status": remote_status,
            "fetched_at": remote_fetched_at,
            "error": remote_error,
        },
    }


def set_project_mapping(
    conn: sqlite3.Connection,
    payload: dict,
    now_iso: str,
) -> dict:
    """实现 set-project-mapping 命令。"""
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise util.bad_request("project_id 必须为非空字符串")
    project_id = project_id.strip()

    raw_repo = payload.get("repository")
    if raw_repo is not None and (not isinstance(raw_repo, str) or not raw_repo.strip()):
        raise util.bad_request("repository 必须为非空字符串或 null（解除映射）")

    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise util.bad_request("enabled 必须为布尔值")

    expected_revision = payload.get("expected_revision")
    if not isinstance(expected_revision, int) or expected_revision < 0 or isinstance(expected_revision, bool):
        raise util.bad_request("expected_revision 必须为非负整数")

    # 规范化路径与状态
    canonical_repo: Optional[str] = None
    if raw_repo is not None:
        c_path, r_kind, state, blocked = inspect_repository_path(raw_repo)
        if state in ("missing", "unreadable") and enabled:
            raise util.KanpilotError("validation_failed", blocked or f"仓库路径无效：{raw_repo}")
        canonical_repo = c_path or str(Path(os.path.expanduser(raw_repo)).resolve())

    with db.write_txn(conn):
        current_revision, workspace_id, items, is_saved = load_raw_mappings(conn)

        # 检查现有条目
        existing_entry = items.get(project_id)
        existing_repo = existing_entry.get("repository") if existing_entry else None
        if existing_repo is not None:
            existing_repo = str(Path(os.path.expanduser(existing_repo)).resolve())
        existing_enabled = bool(existing_entry.get("enabled", True)) if existing_entry else False

        # 幂等性判定：如果与已保存的一致
        is_dedup = (
            existing_entry is not None
            and canonical_repo == existing_repo
            and enabled == existing_enabled
        )

        if expected_revision != current_revision:
            if is_dedup:
                # 幂等返回
                task_count, req_count = _get_active_counts(conn, project_id)
                canonical_path, repo_kind, state, blocked = inspect_repository_path(canonical_repo)
                if canonical_repo and not enabled and state == "available":
                    state = "disabled"
                    blocked = "已停用新规划"
                return {
                    "revision": current_revision,
                    "deduplicated": True,
                    "item": {
                        "project_id": project_id,
                        "project_name": existing_entry.get("project_name") or "",
                        "repository": canonical_repo,
                        "enabled": enabled,
                        "origin": "saved",
                        "mapping_state": state,
                        "repository_kind": repo_kind,
                        "blocked_reason": blocked,
                        "active_task_count": task_count,
                        "active_request_count": req_count,
                        "updated_at": existing_entry.get("updated_at"),
                    },
                }
            raise util.KanpilotError(
                "conflict",
                f"project_mappings revision mismatch: expected {expected_revision}, current is {current_revision}",
            )

        if is_dedup:
            task_count, req_count = _get_active_counts(conn, project_id)
            canonical_path, repo_kind, state, blocked = inspect_repository_path(canonical_repo)
            if canonical_repo and not enabled and state == "available":
                state = "disabled"
                blocked = "已停用新规划"
            return {
                "revision": current_revision,
                "deduplicated": True,
                "item": {
                    "project_id": project_id,
                    "project_name": existing_entry.get("project_name") or "",
                    "repository": canonical_repo,
                    "enabled": enabled,
                    "origin": "saved",
                    "mapping_state": state,
                    "repository_kind": repo_kind,
                    "blocked_reason": blocked,
                    "active_task_count": task_count,
                    "active_request_count": req_count,
                    "updated_at": existing_entry.get("updated_at"),
                },
            }

        # 确定 project_name
        project_name = existing_entry.get("project_name") if existing_entry else None
        if not project_name:
            # 尝试从远端或本地任务获取
            for name, pid in constants.PROJECTS.items():
                if pid == project_id:
                    project_name = name
                    break
            if not project_name:
                t_row = conn.execute(
                    "SELECT project_name FROM tasks WHERE project_id=? LIMIT 1",
                    (project_id,),
                ).fetchone()
                if t_row and t_row[0]:
                    project_name = t_row[0]

        if not project_name:
            # 尝试调用 remote 获取
            try:
                client = remote.get_client()
                plist = bridge.project_list(client, workspace_id)
                for p in plist:
                    if str(p.get("id")) == project_id:
                        project_name = str(p.get("name") or "")
                        break
            except Exception:
                pass

        if not project_name:
            project_name = project_id

        # 更新条目
        items[project_id] = {
            "project_name": project_name,
            "repository": canonical_repo,
            "enabled": enabled,
            "updated_at": now_iso,
        }

        new_revision = current_revision + 1
        new_payload = {
            "version": CURRENT_VERSION,
            "revision": new_revision,
            "workspace_id": workspace_id,
            "items": items,
        }

        db.set_setting(conn, SETTINGS_KEY, new_payload)

        # 记录事件
        db.add_event(
            conn,
            now_iso,
            "project_mapping_updated",
            actor="user",
            detail={
                "project_id": project_id,
                "project_name": project_name,
                "repository": canonical_repo,
                "enabled": enabled,
                "revision": new_revision,
            },
        )

        canonical_path, repo_kind, state, blocked = inspect_repository_path(canonical_repo)
        if canonical_repo and not enabled and state == "available":
            state = "disabled"
            blocked = "已停用新规划"
        task_count, req_count = _get_active_counts(conn, project_id)

        return {
            "revision": new_revision,
            "deduplicated": False,
            "item": {
                "project_id": project_id,
                "project_name": project_name,
                "repository": canonical_repo,
                "enabled": enabled,
                "origin": "saved",
                "mapping_state": state,
                "repository_kind": repo_kind,
                "blocked_reason": blocked,
                "active_task_count": task_count,
                "active_request_count": req_count,
                "updated_at": now_iso,
            },
        }


def cmd_get_project_mappings(ctx, payload: dict) -> dict:
    refresh_remote = bool((payload or {}).get("refresh_remote", False))
    return get_project_mappings(ctx.conn, refresh_remote=refresh_remote)


def cmd_set_project_mapping(ctx, payload: dict) -> dict:
    return set_project_mapping(ctx.conn, payload, ctx.now_iso)
