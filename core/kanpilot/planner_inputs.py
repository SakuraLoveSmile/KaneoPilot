"""v2 手动规划链路（protocol.md §5.22）：fetch-todos / prepare-plan-input /
start-plans 的实现层。

- fetch-todos：经 bridge 分页只读枚举白名单项目的 To Do（描述/评论/关系/
  活动经 task_context 一并取回用于附件与需求指纹），不导入任务、不建请求。
- prepare-plan-input：重新获取该任务核验需求哈希 → 建不可变材料目录
  ``planner_inputs/<input_id>/``（manifest.json + source.json + files/ +
  supplement.md）→ 登记 planner_inputs 表；不入队、不调模型。
- start-plans：逐任务独立提交——材料指纹核验 → 任务导入/状态检查 →
  已授权请求去重/写回 → 建/复用 queued plan 请求（payload 含
  manual_authorized/input_id/planner_input_sha256/manifest/model_config/
  confirmed_at + v3 的 planner_method，spec_plan 再固化 plugin_id/
  plugin_version）。成功项供 planner_runner 领取。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Optional

from . import (
    bridge, codex_planner, constants, core, db, planner_config, project_config, remote, util,
)

# --------------------------------------------------------------- 附件识别

_IMAGE_SUFFIXES = constants.PLAN_IMAGE_SUFFIXES
_TEXT_SUFFIXES = constants.PLAN_TEXT_SUFFIXES
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def _safe_name(name: Any) -> str:
    """附件文件名消毒：去路径分隔与控制字符，空名给占位。"""
    text = str(name or "").strip()
    text = re.sub(r"[\\/\x00-\x1f]+", "_", text).strip(" .")
    return text[:80] or "attachment"


def _attachment_entries(task: dict) -> list:
    """task.attachments 规范化 → [{remote_id,name,url,media_type,size}]。"""
    out = []
    raw = task.get("attachments")
    if not isinstance(raw, list):
        return out
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            out.append({"remote_id": f"att-{i}", "name": None, "url": None,
                        "media_type": None, "size": None,
                        "issue": "附件结构未知"})
            continue
        url = item.get("url") or item.get("downloadUrl") or item.get("path")
        name = (
            item.get("name") or item.get("filename") or item.get("fileName")
        )
        if url and not name:
            name = Path(str(urlparse_path(url))).name or None
        out.append({
            "remote_id": str(item.get("id") or item.get("key") or f"att-{i}"),
            "name": name,
            "url": url,
            "media_type": (
                item.get("contentType") or item.get("content_type")
                or item.get("mimeType")
            ),
            "size": item.get("size") or item.get("sizeBytes"),
        })
    return out


def urlparse_path(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).path or ""
    except Exception:
        return ""


def _safe_url(url) -> str:
    """持久化/输出用的 URL 脱敏：剥掉 query/fragment（可能携带签名或
    token 参数），保留 scheme://host/path 供人工辨认（§5.22.1）。"""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(url))
        if not parsed.scheme or not parsed.netloc:
            return str(url)[:200]
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return "<url>"


def _description_links(source: dict) -> list:
    """描述/评论中的 Markdown/HTML 链接（图片与文件）→ [{url,where}]。"""
    texts = []
    task = source.get("task") or {}
    texts.append(("description", str(task.get("description") or "")))
    for i, comment in enumerate(source.get("comments") or []):
        if isinstance(comment, dict):
            texts.append((f"comment[{i}]", str(comment.get("content")
                                              or comment.get("body") or "")))
        else:
            texts.append((f"comment[{i}]", str(comment)))
    links = []
    for where, text in texts:
        for match in _URL_RE.finditer(text):
            url = match.group(0)
            # Markdown/HTML 语义粗判：![]()/<img> → image，其余按链接。
            start = match.start()
            prefix = text[max(0, start - 30):start]
            kind = (
                "image" if ("![" in prefix[-10:] or "<img" in prefix.lower())
                else "file"
            )
            links.append({"url": url, "where": where, "kind": kind})
    return links


def _attachment_kind(name: Optional[str], media_type: Optional[str]) -> str:
    suffix = Path(name or "").suffix.lower()
    mt = (media_type or "").lower()
    if suffix in _IMAGE_SUFFIXES or mt.startswith("image/"):
        return "image"
    if suffix in _TEXT_SUFFIXES or mt.startswith("text/"):
        return "text"
    return "other"


# ------------------------------------------------------------ fetch-todos

_SELECTABLE_STATUSES = {"needs_plan", "ready", "needs_user"}


def _local_status(conn: sqlite3.Connection, task_id: str) -> dict:
    task = db.get_task(conn, task_id)
    if task is None:
        return {"local_status": None, "local_save_state": None}
    return {
        "local_status": task["status"],
        "local_save_state": db.task_save_state(task),
    }


def _selectable(conn, task_id: str, mapping: Optional[dict]) -> tuple[bool, Optional[str]]:
    """(selectable, blocked_reason)。只读判定，UI 灰显依据。"""
    if not mapping or mapping.get("mapping_state") != "available":
        reason = mapping.get("blocked_reason") if mapping else "项目未映射仓库，不能规划"
        return False, reason
    task = db.get_task(conn, task_id)
    if task is None:
        return True, None
    save_state = db.task_save_state(task)
    if save_state != "active":
        label = {"archived": "归档", "trashed": "回收站",
                 "purged": "已永久移除"}.get(save_state, save_state)
        return False, f"本地任务已{label}，不重复规划"
    if task["send_state"] == "unknown":
        return False, "发送结果不明，请先处理派发状态"
    if task["status"] not in _SELECTABLE_STATUSES:
        label = {
            "planning": "正在规划中", "dispatching": "正在派发",
            "prepared": "正在派发", "submitting": "正在派发",
            "running": "正在执行", "in_review": "正在评审",
            "done": "已完成",
        }.get(task["status"], task["status"])
        return False, f"本地任务{label}，不重复规划"
    return True, None


def fetch_todos(conn: sqlite3.Connection) -> dict:
    """fetch-todos {} → {fetched_at, items[]}（§5.22.1 只读枚举）。"""
    client = remote.get_client()
    projects = bridge.project_list(client, constants.WORKSPACE_ID)
    items = []
    for project in projects:
        project_id = str(project.get("id", ""))
        mapping = project_config.get_effective_mapping(conn, project_id)
        project_name = (mapping.get("project_name") if mapping and mapping.get("project_name") else None) or project.get("name")
        mapping_state = mapping.get("mapping_state", "unmapped") if mapping else "unmapped"
        repository_kind = mapping.get("repository_kind") if mapping else None
        repository = mapping.get("repository") if mapping else None

        cols = bridge.columns(client, project_id)
        todos = bridge.tasks_for_status(
            client, project_id, cols, tuple(constants.TODO_SLUGS)
        )
        for todo in todos:
            task_id = str(todo.get("id", ""))
            if not task_id:
                continue
            try:
                context = bridge.task_context(client, task_id)
            except Exception as exc:
                items.append({
                    "task_id": task_id,
                    "title": todo.get("title") or todo.get("name"),
                    "project_id": project_id, "project_name": project_name,
                    "requirements_sha256": None, "source_sha256": None,
                    "kaneo_status": todo.get("status"), "repository": repository,
                    "mapping_state": mapping_state, "repository_kind": repository_kind,
                    "mapping_project_id": project_id,
                    "local_status": None, "local_save_state": None,
                    "attachments": [], "selectable": False,
                    "blocked_reason": f"任务详情读取失败：{exc}",
                })
                continue
            attachments = _attachment_entries(context.get("task") or {})
            local = _local_status(conn, task_id)
            selectable, blocked = _selectable(conn, task_id, mapping)
            # 输出脱敏：附件 url 去 query/fragment（下载经 prepare 重新取
            # 完整地址；列表项只用于展示与排除勾选）。
            attachments = [
                {**a, "url": _safe_url(a["url"]) if a.get("url") else None}
                for a in attachments
            ]
            items.append({
                "task_id": task_id,
                "title": (context["task"].get("title")
                          or context["task"].get("name")),
                "project_id": project_id, "project_name": project_name,
                "requirements_sha256": context["requirements_sha256"],
                "source_sha256": context["source_sha256"],
                "kaneo_status": (context["task"].get("status")
                                 or todo.get("status")),
                "repository": repository,
                "mapping_state": mapping_state,
                "repository_kind": repository_kind,
                "mapping_project_id": project_id,
                "local_status": local["local_status"],
                "local_save_state": local["local_save_state"],
                "attachments": attachments,
                "selectable": selectable,
                "blocked_reason": blocked,
            })
    return {"fetched_at": util.now_iso(), "items": items}


# ------------------------------------------------------- prepare-plan-input

def _inputs_root(conn) -> Path:
    return constants.planner_inputs_root(
        db.get_setting(conn, "planner_inputs_root")
    )


def _manifest_sha(manifest: dict) -> str:
    """planner_input_sha256 = 规范化 manifest 的 sha256（§5.22.1）。"""
    canonical = json.dumps(
        bridge.stable(manifest), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return util.sha256_bytes(canonical)


def _unique_path(directory: Path, name: str) -> Path:
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = directory / f"{stem}{suffix}"
    i = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{i}{suffix}"
        i += 1
    return candidate


def prepare_plan_input(ctx, payload: dict) -> dict:
    """prepare-plan-input {task_id, expected_requirements_sha256, local_paths[],
    supplement, excluded_remote_ids[]}（§5.22.1）。

    重新获取远端任务核验需求哈希 → 建不可变材料目录 → 登记 planner_inputs。
    不入队、不调模型。issues 非空 → can_start=false（须补齐或显式排除）。
    """
    conn, now = ctx.conn, ctx.now_iso
    task_id = payload["task_id"]
    # expected_requirements_sha256 可选：提供则核验（UI 总是携带自
    # fetch-todos）；缺省按远端现状登记。
    expected_req = payload.get("expected_requirements_sha256")
    local_paths = payload.get("local_paths") or []
    supplement = payload.get("supplement") or ""
    if not isinstance(local_paths, list):
        raise util.bad_request("local_paths must be a list")
    if not isinstance(supplement, str):
        raise util.bad_request("supplement must be a string")
    raw_excluded = payload.get("excluded_remote_ids") or []
    if not isinstance(raw_excluded, list):
        raise util.bad_request("excluded_remote_ids must be a list")
    excluded = {str(x) for x in raw_excluded}

    # 1) 远端重新获取 + 需求哈希核验。
    try:
        context = remote.get_task(remote.get_client(), task_id)
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", str(exc)) from exc
    remote_req = context.get("requirements_sha256")
    if expected_req and remote_req and remote_req != expected_req:
        raise util.KanpilotError(
            "conflict",
            "远端需求已变化（requirements_sha256 不符），请重新获取后再准备",
        )
    local_task = db.get_task(conn, task_id)
    task_obj = context.get("task") or {}
    project_id = task_obj.get("projectId") or (local_task["project_id"] if local_task else None)
    mapping = project_config.get_effective_mapping(conn, project_id)
    project_name = (mapping.get("project_name") if mapping else None) or (local_task["project_name"] if local_task else None)
    repository = mapping.get("repository") if mapping else (local_task["repository"] if local_task else None)
    mapping_revision = mapping.get("revision", 0) if mapping else 0

    issues: list = []
    files: list = []
    total_bytes = 0

    if not mapping or mapping.get("mapping_state") != "available":
        issues.append({"issue": f"项目未映射仓库或仓库不可用：{mapping.get('blocked_reason') if mapping else '未配置'}"})

    # 2) 材料目录（不可变产物；input_id 即目录名）。
    input_id = util.new_uuid()
    root = _inputs_root(conn) / input_id
    files_dir = root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    def register(path: Path, source: str, media_type=None) -> dict:
        nonlocal total_bytes
        size = path.stat().st_size
        total_bytes += size
        entry = {
            "name": path.name, "source": source, "media_type": media_type,
            "sha256": util.sha256_file(path), "size": size,
            "path": str(path.relative_to(root)),
        }
        files.append(entry)
        return entry

    # 2a) 源快照（规范化落盘，canonical 口径与 bridge 一致）。
    source = bridge.stable({
        "task": task_obj,
        "comments": bridge.unwrap_data(context.get("comments")) or [],
        "relations": bridge.unwrap_data(context.get("relations")) or [],
        "activity": bridge.unwrap_data(context.get("activity")) or [],
    })
    source = bridge.add_hash(source)
    source_path = root / "source.json"
    source_path.write_text(
        json.dumps(source, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    register(source_path, "source_snapshot", "application/json")

    # 2b) 补充说明。
    if supplement.strip():
        sup_path = root / "supplement.md"
        sup_path.write_text(supplement, encoding="utf-8")
        register(sup_path, "supplement", "text/markdown")

    # 2c) 附件（同源下载；跨源/失效/超限 → issue，can_start=false）。
    for entry in _attachment_entries(task_obj):
        rid = entry["remote_id"]
        if rid in excluded:
            continue
        url, name = entry.get("url"), entry.get("name")
        if not url:
            issues.append({"remote_id": rid, "issue": entry.get("issue")
                           or "附件缺少下载地址"})
            continue
        kind = _attachment_kind(name, entry.get("media_type"))
        if kind == "other":
            issues.append({"remote_id": rid,
                           "issue": f"附件类型不支持（{name or rid}）："
                                    "仅图片 PNG/JPEG/WebP 与 UTF-8 文本"})
            continue
        dest = _unique_path(files_dir, _safe_name(name or rid))
        try:
            bridge.download_attachment(
                url, dest, max_bytes=constants.PLAN_INPUT_FILE_MAX_BYTES
            )
        except bridge.BridgeError as exc:
            issues.append({"remote_id": rid, "issue": str(exc)})
            continue
        if kind == "text":
            try:
                dest.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                dest.unlink(missing_ok=True)
                issues.append({"remote_id": rid,
                               "issue": f"文本附件不是 UTF-8：{name or rid}"})
                continue
        register(dest, "attachment", entry.get("media_type") or (
            "image/*" if kind == "image" else "text/*"))

    # 2d) 描述/评论中的链接（图片/文件）——同源下载，跨源记 issue。
    for link in _description_links(source):
        url = link["url"]
        if url in excluded:
            continue
        kind = link["kind"]
        dest_name = _safe_name(Path(urlparse_path(url)).name or "link-file")
        dest = _unique_path(files_dir, dest_name)
        try:
            bridge.download_attachment(
                url, dest, max_bytes=constants.PLAN_INPUT_FILE_MAX_BYTES
            )
        except bridge.BridgeError as exc:
            issues.append({"url": _safe_url(url), "issue": str(exc)})
            continue
        register(dest, f"{link['where']}:{kind}", None)

    # 2e) 本地路径（只读复制进材料目录；缺文件 → issue）。
    for raw in local_paths:
        if not isinstance(raw, str) or not raw.strip():
            issues.append({"local_path": str(raw), "issue": "本地路径无效"})
            continue
        src = Path(raw).expanduser()
        if not src.is_file():
            issues.append({"local_path": raw, "issue": "本地文件不存在"})
            continue
        if src.stat().st_size > constants.PLAN_INPUT_FILE_MAX_BYTES:
            issues.append({"local_path": raw,
                           "issue": "本地文件超过 20 MiB 上限"})
            continue
        dest = _unique_path(files_dir, _safe_name(src.name))
        try:
            shutil.copyfile(src, dest)
        except OSError as exc:
            issues.append({"local_path": raw, "issue": f"复制失败：{exc}"})
            continue
        register(dest, "local", None)

    if total_bytes > constants.PLAN_INPUT_TOTAL_MAX_BYTES:
        issues.append({"issue": "材料合计超过 100 MiB 上限"})

    # 3) manifest + 登记（不可变：manifest.json 落盘后即哈希基线）。
    manifest = {
        "input_id": input_id,
        "task": {
            "task_id": task_id,
            "title": task_obj.get("title") or task_obj.get("name"),
            "project_id": project_id,
            "project_name": project_name,
            "repository": repository,
            "project_mapping_revision": mapping_revision,
            "kaneo_status": task_obj.get("status"),
            "plan_kind": (
                local_task["plan_kind"]
                if local_task is not None and local_task["plan_kind"]
                else "standard"
            ),
        },
        "requirements_sha256": remote_req,
        "source_sha256": source["source_sha256"],
        "supplement": supplement,
        "excluded_remote_ids": sorted(str(x) for x in excluded),
        "files": files,
        "issues": issues,
        "created_at": now,
    }
    input_sha = _manifest_sha(manifest)
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )

    with db.write_txn(conn):
        db.insert_planner_input(
            conn, input_id, task_id, input_sha, remote_req,
            source["source_sha256"], manifest, now,
        )
        db.add_event(
            conn, now, "plan_input_prepared", task_id=task_id, actor="user",
            detail={"input_id": input_id, "files": len(files),
                    "issues": len(issues)},
        )
    return {
        "input_id": input_id, "input_sha256": input_sha,
        "files": files, "issues": issues,
        "can_start": not issues,
        "requirements_sha256": remote_req,
        "source_sha256": source["source_sha256"],
        "input_dir": str(root),
    }


# -------------------------------------------------------------- start-plans

def _import_task(conn, now: str, manifest: dict):
    """本地无任务行时从 manifest 快照导入（needs_plan；幂等键 plan:<id>）。"""
    meta = manifest["task"]
    task_id = meta["task_id"]
    input_dir = _inputs_root(conn) / manifest["input_id"]
    source_path = input_dir / "source.json"
    fields = {
        "task_id": task_id,
        "project_id": meta["project_id"],
        "project_name": meta["project_name"] or "",
        "title": meta.get("title"),
        "repository": meta.get("repository"),
        "stage": "plan", "status": "needs_plan",
        "kaneo_status": meta.get("kaneo_status"), "kaneo_checked_at": now,
        "requirements_sha256": manifest.get("requirements_sha256"),
        "source_sha256": manifest.get("source_sha256"),
        "source_path": str(source_path) if source_path.is_file() else None,
        "plan_kind": meta.get("plan_kind") or "standard",
        "created_at": now, "updated_at": now,
    }
    columns = ", ".join(fields)
    conn.execute(
        f"INSERT INTO tasks({columns}) VALUES "
        f"({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )
    db.add_event(
        conn, now, "task_created", task_id=task_id, actor="user",
        detail={"via": "start-plans import"},
    )
    return db.require_task(conn, task_id)


def _item_task_id(conn, item: dict):
    """错误结果里的 task_id 回落：planner_inputs 行 > None（不拿 input_id
    冒充 task_id）。"""
    input_id = item.get("input_id") if isinstance(item, dict) else None
    row = db.get_planner_input(conn, input_id) if input_id else None
    return row["task_id"] if row is not None else None


def _submit_one(conn, now: str, item: dict, prepared=None) -> dict:
    """start-plans 单项提交（§5.22.2/§5.23.2/§5.24.3）：逐项独立事务语义由
    调用方保证；返回 {task_id, request_id?, deduplicated?, reused?, code?,
    error?, waiting_reason?}。

    item 可带 ``planner_method``（缺省 builtin）与 ``planner_backend``
    （缺省 codex）。payload 按后端固化：codex → ``model_config``；
    pi_desktop → ``runtime_config``（五字段脱敏）+ ``planner_backend``。
    幂等判定包含材料哈希、方式与后端三者。"""
    input_id = item.get("input_id")
    supplied_sha = item.get("input_sha256")
    # §5.23.2：planner_method 缺省 builtin；非法值 → 该项 bad_request。
    method = item.get("planner_method") or "builtin"
    if method not in planner_config.DEFAULT_METHODS:
        return {"task_id": _item_task_id(conn, item),
                "code": "bad_request",
                "error": f"planner_method 非法：{item.get('planner_method')!r}"
                         f"（仅 { '/'.join(planner_config.DEFAULT_METHODS) }）"}
    # §5.24.3：planner_backend 缺省 codex（UI 总是提交明确值）；非法值 →
    # 该项 bad_request，不改设置、不猜后端。
    planner_backend = (
        item.get("planner_backend") or planner_config.DEFAULT_BACKEND
    )
    if planner_backend not in planner_config.BACKENDS:
        return {"task_id": _item_task_id(conn, item),
                "code": "bad_request",
                "error": f"planner_backend 非法：{item.get('planner_backend')!r}"
                         f"（仅 {'/'.join(planner_config.BACKENDS)}）"}
    row = db.get_planner_input(conn, input_id) if input_id else None
    if row is None:
        return {"task_id": None, "code": "not_found",
                "error": f"规划材料不存在：{input_id}"}
    if supplied_sha and supplied_sha != row["input_sha256"]:
        return {"task_id": row["task_id"], "code": "conflict",
                "error": "材料指纹与登记不符（可能被重新准备）"}
    manifest = db.planner_input_manifest(row)
    task_id = row["task_id"]
    task = db.get_task(conn, task_id)

    # 项目映射核验（§5.22.1 / §5.26）：未映射或已停用项目不可规划。
    manifest_task = manifest.get("task") or {}
    task_keys = task.keys() if task else []
    manifest_project_id = (
        manifest_task.get("project_id")
        or (task["project_id"] if task and "project_id" in task_keys and task["project_id"] else None)
        or constants.PROJECTS.get(manifest_task.get("project_name") or (task["project_name"] if task and "project_name" in task_keys else None) or "")
    )
    effective_mapping = project_config.get_effective_mapping(conn, manifest_project_id)
    if not effective_mapping or not effective_mapping.get("enabled"):
        return {
            "task_id": task_id, "code": "conflict",
            "error": f"项目未映射仓库或已停用（{manifest_task.get('project_name') or (task['project_name'] if task and 'project_name' in task_keys else None) or '未知'}），不能规划",
        }
    if manifest_task.get("repository") and effective_mapping.get("repository"):
        m_repo = Path(manifest_task.get("repository")).expanduser().resolve()
        e_repo = Path(effective_mapping.get("repository")).expanduser().resolve()
        if m_repo != e_repo:
            return {
                "task_id": task_id, "code": "conflict",
                "error": f"项目仓库配置已变更（{m_repo} vs {e_repo}），请重新准备规划材料",
            }
    if db.task_is_purged(task):
        return {"task_id": task_id, "code": "conflict",
                "error": "任务已永久从列表移除"}
    if task is not None and not db.task_is_active(task):
        return {"task_id": task_id, "code": "conflict",
                "error": "任务不在活跃看板（归档/回收站），请先恢复"}
    if task is not None and task["send_state"] == "unknown":
        return {"task_id": task_id, "code": "conflict",
                "error": "发送结果不明，请先处理派发状态"}

    # 非规划活动请求 → 等待（waiting_reason）。
    if task is not None:
        active = conn.execute(
            "SELECT kind, status FROM requests WHERE task_id=? "
            "AND status IN ('queued','claimed')",
            (task_id,),
        ).fetchall()
        others = [r for r in active if r["kind"] != "plan"]
        if others:
            return {
                "task_id": task_id, "code": "conflict",
                "error": "存在进行中的其他请求，暂不能排队规划",
                "waiting_reason": f"等待 {others[0]['kind']} 请求结束",
            }

    # 任务导入/状态推进。
    if task is None:
        task = _import_task(conn, now, manifest)
    elif task["status"] == "ready":
        expected = item.get("expected_plan_sha256")
        if expected and expected != task["plan_sha256"]:
            return {"task_id": task_id, "code": "conflict",
                    "error": "现行计划哈希与预期不符，请先查看后再重新规划"}
        # ready → needs_plan 重新规划（等 claim 时转 planning）。
        core.transition_task(
            conn, task, "needs_plan", now, "user",
            detail={"via": "start-plans replan"},
        )
        task = db.require_task(conn, task_id)
    elif task["status"] not in ("needs_plan",):
        # needs_user 等：允许规划重试（材料已确认）；其余状态由前面拦截。
        if task["status"] == "needs_user":
            core.transition_task(
                conn, task, "needs_plan", now, "user",
                detail={"via": "start-plans replan"},
            )
            task = db.require_task(conn, task_id)
        else:
            return {"task_id": task_id, "code": "conflict",
                    "error": f"任务状态 {task['status']} 不能排队规划"}

    # 幂等键 plan:<task_id>：已授权且同材料 + 同方式 + 同后端 → deduplicated；
    # 材料/方式/后端任一不同 → conflict（不暗换材料、方式或后端）；未授权旧
    # 请求 → 写回已确认输入（复用请求，§5.22.2/§5.23.2/§5.24.3）。
    key = f"plan:{task_id}"
    existing = db.active_request_for_key(conn, key)
    if existing is not None:
        if existing["status"] == "claimed":
            return {"task_id": task_id, "code": "conflict",
                    "error": "规划请求已被领取，正在执行中"}
        old_payload = db.request_payload(existing)
        if old_payload.get("manual_authorized"):
            # 缺 planner_method / planner_backend 的旧 payload 按
            # builtin/codex 解释（§5.23.2/§5.24.3）。
            old_method = old_payload.get("planner_method") or "builtin"
            old_backend = (
                old_payload.get("planner_backend")
                or planner_config.DEFAULT_BACKEND
            )
            same_input = (
                old_payload.get("planner_input_sha256")
                == row["input_sha256"])
            if same_input and old_backend != planner_backend:
                return {"task_id": task_id, "code": "conflict",
                        "error": f"已有同材料但后端不同的规划请求"
                                 f"（{old_backend} → {planner_backend}），请先处理"}
            if same_input and old_method == method:
                return {"task_id": task_id,
                        "request_id": existing["request_id"],
                        "deduplicated": True}
            if same_input:
                return {"task_id": task_id, "code": "conflict",
                        "error": f"已有同材料但方式不同的规划请求"
                                 f"（{old_method} → {method}），请先处理"}
            return {"task_id": task_id, "code": "conflict",
                    "error": "已有不同材料的规划请求在队列中，请先处理"}

    # §5.24.3：Plus 分支只支持 builtin——spec_plan 是 Codex 插件运行方式，
    # 在 Plus 下明确失败并给原因（不把技能文本复制给 Plus、不静默换方式）。
    # dedup/conflict 已短路，此处只对真正创建/复用的请求判定。
    if planner_backend == "pi_desktop" and method != "builtin":
        reason = planner_config.spec_plan_blocked_reason(planner_backend)
        return {"task_id": task_id, "code": "validation_failed",
                "error": f"Plus 计划请求不支持 planner_method={method}：{reason}"}
    # spec_plan：提交时经 codex plugin list 固化插件身份与版本（不可用 →
    # validation_failed，该项失败、其他项不受影响）。
    prepared = prepared or {}
    if prepared.get("config_fingerprint") != util.jdump(planner_config.merged(conn)):
        return {"task_id": task_id, "code": "conflict", "error": "规划配置在解析期间变化，请重新提交"}
    if prepared.get("input_sha256") != row["input_sha256"]:
        return {"task_id": task_id, "code": "conflict", "error": "规划材料在解析期间变化，请重新提交"}
    plugin = None
    if method == "spec_plan":
        plugin = prepared.get("plugin") or {"available": False, "reason": "未解析插件"}
        if not plugin.get("available"):
            return {"task_id": task_id, "code": "validation_failed",
                    "error": f"spec_plan 不可用：{plugin.get('reason')}"}

    # §5.24.3：按后端固化对应运行快照——codex → model_config（codex 模型组）；
    # pi_desktop → runtime_config（五字段脱敏，无 token）。Plus 分支要求设置里
    # 已保存完整五字段（结构校验在此复核，避免 hand-edited 库产生半残请求）。
    runtime_config = None
    if planner_backend == "pi_desktop":
        block = planner_config.pi_desktop_block(conn)
        error = planner_config.validate_pi_desktop_block(block)
        if error is not None:
            return {"task_id": task_id, "code": "validation_failed",
                    "error": f"Plus 计划分支配置无效：{error}"}
        resolved, resolve_err = prepared.get("runtime_config"), prepared.get("runtime_error")
        if resolve_err is not None or resolved is None:
            return {"task_id": task_id, "code": "validation_failed",
                    "error": f"Plus 计划分支配置无效：{resolve_err}"}
        runtime_config = resolved
    payload = {
        "manual_authorized": True,
        "input_id": input_id,
        "planner_input_sha256": row["input_sha256"],
        "manifest": manifest,
        "project_snapshot": {
            "project_id": manifest_project_id,
            "project_name": manifest_task.get("project_name"),
            "repository": manifest_task.get("repository"),
            "project_mapping_revision": manifest_task.get("project_mapping_revision", 0),
        },
        "planner_backend": planner_backend,
        "confirmed_at": now,
        "planner_method": method,
    }
    if runtime_config is not None:
        payload["runtime_config"] = runtime_config
    else:
        payload["model_config"] = planner_config.model_snapshot(
            conn, planner_config.group_for(task["plan_kind"])
        )
    if plugin is not None:
        payload["plugin_id"] = plugin["plugin_id"]
        payload["plugin_version"] = plugin["version"]
    if task["plan_kind"] == "review_fix":
        payload.update({
            "plan_kind": "review_fix",
            "review_key": task["review_key"],
            "review_source_task_id": task["review_source_task_id"],
            "fix_task_id": task_id,
        })
    if existing is not None:
        # 未授权旧请求 → 写回已确认输入（复用请求，不新建）。
        old_payload = db.request_payload(existing)
        old_payload.update(payload)
        db.update_request(
            conn, existing["request_id"], now, payload_json=old_payload
        )
        db.add_event(
            conn, now, "plan_started", task_id=task_id,
            request_id=existing["request_id"], actor="user",
            detail={"input_id": input_id, "reused": True,
                    "planner_method": method,
                    "planner_backend": planner_backend},
        )
        return {"task_id": task_id, "request_id": existing["request_id"],
                "deduplicated": False, "reused": True}

    request, _created = db.create_request(
        conn, now, "plan", key, task_id=task_id, payload=payload,
        requested_by="user",
    )
    db.add_event(
        conn, now, "plan_started", task_id=task_id,
        request_id=request["request_id"], actor="user",
        detail={"input_id": input_id, "reused": False,
                "planner_method": method,
                "planner_backend": planner_backend},
    )
    return {"task_id": task_id, "request_id": request["request_id"],
            "deduplicated": False}


class _SubmissionRejected(Exception):
    def __init__(self, result):
        self.result = result


def _prepare_submission(conn, item):
    """Only reads local inputs before resolving network/plugin dependencies."""
    row = db.get_planner_input(conn, item.get("input_id"))
    prepared = {"config_fingerprint": util.jdump(planner_config.merged(conn)),
                "input_sha256": row["input_sha256"] if row else None}
    method = item.get("planner_method") or "builtin"
    backend = item.get("planner_backend") or planner_config.DEFAULT_BACKEND
    if row is None or method not in planner_config.DEFAULT_METHODS or backend not in planner_config.BACKENDS:
        return prepared
    if item.get("input_sha256") and item["input_sha256"] != row["input_sha256"]:
        return prepared
    existing = db.active_request_for_key(conn, "plan:" + row["task_id"])
    if existing is not None and (existing["status"] == "claimed" or db.request_payload(existing).get("manual_authorized")):
        return prepared  # Existing authorization wins, including while offline.
    if backend == "pi_desktop" and method == "builtin":
        prepared["runtime_config"], prepared["runtime_error"] = planner_config.resolve_runtime_config(conn)
    elif backend == "codex" and method == "spec_plan":
        prepared["plugin"] = codex_planner.probe_spec_plugin()
    return prepared


def start_plans(ctx, payload: dict) -> dict:
    """start-plans {items:[{input_id,input_sha256,expected_plan_sha256?,
    planner_method?, planner_backend?}]} → {results:[...]}。每项独立事务，
    不回滚已成功项（§5.22.2/§5.23.2/§5.24.3：planner_backend 缺省 codex，
    非法值→bad_request；Plus 项只支持 builtin 且要求已保存五字段）。"""
    conn, now = ctx.conn, ctx.now_iso
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise util.bad_request("start-plans 需要非空 items 列表")
    results = []
    started = 0
    for item in items:
        if not isinstance(item, dict):
            results.append({"task_id": None, "code": "bad_request",
                            "error": "item 必须是对象"})
            continue
        try:
            prepared = _prepare_submission(conn, item)
            with db.write_txn(conn):
                result = _submit_one(conn, now, item, prepared)
                if result.get("error"):
                    raise _SubmissionRejected(result)
        except _SubmissionRejected as exc:
            result = exc.result
        except util.KanpilotError as exc:
            result = {"task_id": _item_task_id(conn, item),
                      "code": exc.code, "error": exc.message}
        except Exception as exc:  # pragma: no cover - defensive
            result = {"task_id": _item_task_id(conn, item),
                      "code": "internal", "error": str(exc)}
        if result.get("request_id"):
            started += 1
        results.append(result)
    return {"results": results, "started": started}
