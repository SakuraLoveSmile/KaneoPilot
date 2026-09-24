#!/usr/bin/env python3
"""Kaneo bridge for KaneoPilot (adapted from kaneo-scheduled kaneo_bridge.py).

The output is deliberately deterministic: ``source_sha256`` is computed from
the canonical JSON representation of the payload without that field.
Credential loading and the source/requirements hash algorithms are kept
byte-for-byte identical to the legacy bridge for migration compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
import urllib.request
from urllib.request import Request, urlopen

import ssl
def _ssl_context():
    """HTTPS CA 兜底（v1.6）：优先 certifi（嵌入/Framework Python 常缺系统
    CA 包），无 certifi 回退系统默认。返回 context 供 urlopen 使用。"""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return None
    except Exception:
        return None


MAX_RESPONSE = 10 * 1024 * 1024
MAX_CREATE_PAYLOAD = 1024 * 1024
DEFAULT_TIMEOUT = 10.0
VALID_PRIORITIES = {"no-priority", "low", "medium", "high", "urgent"}
TODO_SLUGS = ("to-do", "todo")


class BridgeError(Exception):
    pass


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def load_credentials() -> tuple[str, str]:
    path = Path(os.environ.get("KANEO_CREDENTIALS_PATH", "~/.config/kaneo-mcp/credentials.json")).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError(f"无法读取 Kaneo 登录凭据: {exc}") from exc
    if not isinstance(value, dict):
        raise BridgeError("Kaneo 凭据格式无效")
    base_url = value.get("baseUrl") or value.get("baseURL") or value.get("url")
    token = value.get("accessToken") or value.get("access_token")
    if isinstance(value.get("token"), str):
        token = token or value["token"]
    if isinstance(value.get("credentials"), dict):
        nested = value["credentials"]
        base_url = base_url or nested.get("baseUrl") or nested.get("baseURL")
        token = token or nested.get("accessToken") or nested.get("access_token")
    if not isinstance(base_url, str) or not isinstance(token, str) or not token:
        raise BridgeError("Kaneo 凭据缺少 baseUrl 或 accessToken")
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or (parsed.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}):
        raise BridgeError("Kaneo baseUrl 只允许 HTTPS 或 localhost")
    return base_url.rstrip("/") + "/", token


class Client:
    def __init__(self) -> None:
        self.base_url, self.token = load_credentials()
        try:
            self.timeout = float(os.environ.get("KANEO_TIMEOUT", DEFAULT_TIMEOUT))
        except ValueError as exc:
            raise BridgeError("KANEO_TIMEOUT 必须是数字") from exc
        if self.timeout <= 0:
            raise BridgeError("KANEO_TIMEOUT 必须大于 0")

    def request(self, path: str, method: str = "GET", body: Any = None) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        request = Request(url, method=method, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        if body is not None:
            request.data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=self.timeout,
                         context=_ssl_context()) as response:
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = response.read(min(65536, MAX_RESPONSE - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise BridgeError("Kaneo 响应超过 10 MiB 限制")
                    chunks.append(chunk)
        except HTTPError as exc:
            if exc.code == 401:
                raise BridgeError("Kaneo 登录已失效，请刷新 Kaneo 登录凭据") from exc
            raise BridgeError(f"Kaneo 请求失败: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BridgeError(f"Kaneo 请求失败: {type(exc).__name__}") from exc
        raw = b"".join(chunks)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BridgeError("Kaneo 返回了无效 JSON") from exc


def as_list(value: Any, *keys: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return []


def unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
        return value["data"]
    return value


def stable(value: Any) -> Any:
    """Normalize unordered API collections before hashing."""
    if isinstance(value, dict):
        return {str(key): stable(item) for key, item in value.items()}
    if isinstance(value, list):
        normalized = [stable(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    return value


def project_list(client: Client, workspace_id: str) -> list[dict[str, Any]]:
    value = client.request("/api/project?" + urlencode({"workspaceId": workspace_id}))
    return [
        item
        for item in as_list(value, "data", "projects")
        if isinstance(item, dict)
        and not item.get("isArchived")
        and not item.get("archived")
        and not item.get("archivedAt")
    ]


def columns(client: Client, project_id: str) -> list[dict[str, Any]]:
    project = quote(project_id, safe="")
    return [item for item in as_list(client.request(f"/api/column/{project}"), "data", "columns") if isinstance(item, dict)]


def task_context(client: Client, task_id: str) -> dict[str, Any]:
    encoded = quote(task_id, safe="")
    detail = unwrap_data(client.request(f"/api/task/{encoded}"))
    if not isinstance(detail, dict):
        raise BridgeError("Kaneo 任务响应格式无效")
    source = stable(
        {
            "task": detail,
            "comments": unwrap_data(client.request(f"/api/comment/{encoded}")),
            "relations": unwrap_data(client.request(f"/api/task-relation/{encoded}")),
            "activity": unwrap_data(client.request(f"/api/activity/{encoded}")),
        }
    )
    return {**add_hash(source), "requirements_sha256": requirements_hash(source)}


def requirements_hash(source: dict[str, Any]) -> str:
    """Hash requirement-bearing fields, excluding lifecycle and audit events."""
    task = source.get("task")
    if not isinstance(task, dict) or not task.get("id"):
        raise BridgeError("来源快照缺少 task/id，不能建立需求基线")
    fields = ("id", "projectId", "title", "description", "customFields", "attachments")
    payload = stable({
        "schema": "requirements-v1",
        "task": {key: task.get(key) for key in fields},
        "comments": source.get("comments", []),
        "relations": source.get("relations", []),
    })
    return add_hash(payload)["source_sha256"]


def tasks_for_status(
    client: Client,
    project_id: str,
    project_columns: list[dict[str, Any]],
    candidates: tuple[str, ...],
) -> list[dict[str, Any]]:
    slugs = {str(item.get("slug")) for item in project_columns}
    status_slug = next((slug for slug in candidates if slug in slugs), None)
    if status_slug is None:
        return []
    result: list[dict[str, Any]] = []
    page = 1
    while True:
        encoded_project = quote(project_id, safe="")
        board = client.request(f"/api/task/tasks/{encoded_project}?" + urlencode({"status": status_slug, "page": page, "limit": 100}))
        data = board.get("data", {}) if isinstance(board, dict) else {}
        found = []
        for column in as_list(data, "columns"):
            if isinstance(column, dict):
                found.extend(as_list(column, "tasks"))
        result.extend(item for item in found if isinstance(item, dict) and item.get("status") == status_slug)
        pagination = board.get("pagination", {}) if isinstance(board, dict) else {}
        if not isinstance(pagination, dict) or page >= int(pagination.get("totalPages", 1)):
            break
        page += 1
    return result


def project_tasks(client: Client, project_id: str) -> list[dict[str, Any]]:
    """Return every board task so create retries can find an earlier result."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while True:
        encoded_project = quote(project_id, safe="")
        board = client.request(
            f"/api/task/tasks/{encoded_project}?"
            + urlencode({"page": page, "limit": 100})
        )
        data = board.get("data", {}) if isinstance(board, dict) else {}
        buckets: list[Any] = []
        for column in as_list(data, "columns"):
            if isinstance(column, dict):
                buckets.extend(as_list(column, "tasks"))
        buckets.extend(as_list(data, "archivedTasks"))
        buckets.extend(as_list(data, "plannedTasks"))
        for task in buckets:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id", ""))
            if task_id and task_id not in seen:
                seen.add(task_id)
                result.append(task)
        pagination = board.get("pagination", {}) if isinstance(board, dict) else {}
        if not isinstance(pagination, dict) or page >= int(
            pagination.get("totalPages", 1)
        ):
            break
        page += 1
    return result


def normalize_tasks(
    client: Client,
    workspace_id: str,
    status_candidates: tuple[str, ...],
) -> dict[str, Any]:
    projects_out: list[dict[str, Any]] = []
    for project in project_list(client, workspace_id):
        project_id = str(project.get("id", ""))
        project_columns = columns(client, project_id)
        tasks = tasks_for_status(client, project_id, project_columns, status_candidates)
        normalized_tasks = []
        for task in tasks:
            task_id = str(task.get("id", ""))
            if not task_id:
                continue
            normalized_tasks.append(task_context(client, task_id))
        projects_out.append({"project": project, "columns": project_columns, "tasks": normalized_tasks})
    snapshot = {"workspaceId": workspace_id, "projects": projects_out}
    canonical = json.dumps(stable(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    snapshot["snapshot_sha256"] = hashlib.sha256(canonical).hexdigest()
    return snapshot


def normalize_todos(client: Client, workspace_id: str) -> dict[str, Any]:
    return normalize_tasks(client, workspace_id, ("to-do", "todo"))


def normalize_reviews(client: Client, workspace_id: str) -> dict[str, Any]:
    return normalize_tasks(client, workspace_id, ("in-review", "review"))


def add_hash(payload: dict[str, Any]) -> dict[str, Any]:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    result = dict(payload)
    result["source_sha256"] = hashlib.sha256(canonical).hexdigest()
    return result


def command_get_task(client: Client, task_id: str) -> dict[str, Any]:
    context = task_context(client, task_id)
    task = context["task"]
    project_id = task.get("projectId")
    if not isinstance(project_id, str) or not project_id:
        raise BridgeError("任务缺少 projectId")
    return {**context, "columns": columns(client, project_id)}


def update_task_status(
    client: Client,
    task_id: str,
    project_id: str,
    status: str,
    expected_current_status: str,
) -> dict[str, Any]:
    """Library form of the legacy ``update-status`` command (same checks)."""
    encoded_task = quote(task_id, safe="")
    task = unwrap_data(client.request(f"/api/task/{encoded_task}"))
    if not isinstance(task, dict) or task.get("projectId") != project_id:
        raise BridgeError("任务不属于指定项目，拒绝更新")
    if task.get("status") != expected_current_status:
        raise BridgeError("任务当前状态与 expected-current-status 不匹配，拒绝更新")
    if not any(item.get("slug") == status for item in columns(client, project_id)):
        raise BridgeError("目标状态列不存在，拒绝更新")
    return {"task": client.request(f"/api/task/status/{encoded_task}", "PUT", {"status": status})}


def command_update_status(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    return update_task_status(
        client,
        task_id=args.task_id,
        project_id=args.project_id,
        status=args.status,
        expected_current_status=args.expected_current_status,
    )


def load_create_payload(path_value: str) -> dict[str, str]:
    path = Path(path_value).expanduser()
    try:
        if path.stat().st_size > MAX_CREATE_PAYLOAD:
            raise BridgeError("新建任务 payload 超过 1 MiB 限制")
        value = json.loads(path.read_text(encoding="utf-8"))
    except BridgeError:
        raise
    except (OSError, ValueError) as exc:
        raise BridgeError(f"无法读取新建任务 payload: {exc}") from exc
    return validate_create_payload(value)


def validate_create_payload(value: Any) -> dict[str, str]:
    """Validate an already-parsed create-todo payload (same rules as legacy)."""
    if not isinstance(value, dict):
        raise BridgeError("新建任务 payload 必须是 JSON 对象")
    allowed = {"title", "description", "priority"}
    if set(value) - allowed:
        raise BridgeError("新建任务 payload 含有不允许的字段")
    title = value.get("title")
    description = value.get("description")
    priority = value.get("priority", "high")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise BridgeError("新建任务 title 必须是 1 到 500 个字符")
    if not isinstance(description, str) or len(description) > 500_000:
        raise BridgeError("新建任务 description 必须是字符串且不超过 500000 个字符")
    if not isinstance(priority, str) or priority not in VALID_PRIORITIES:
        raise BridgeError("新建任务 priority 无效")
    return {
        "title": title.strip(),
        "description": description,
        "priority": priority,
    }


def create_todo(
    client: Client,
    project_id: str,
    dedupe_key: str,
    payload: dict[str, str],
) -> dict[str, Any]:
    """Library form of the legacy ``create-todo`` command (same dedupe semantics)."""
    if (
        not isinstance(dedupe_key, str) or not dedupe_key.strip()
        or "-->" in dedupe_key or "<!--" in dedupe_key
    ):
        raise BridgeError("dedupe-key 必须是非空且不含注释定界符的字符串")
    project_columns = columns(client, project_id)
    slugs = {str(item.get("slug")) for item in project_columns}
    todo_slug = next((slug for slug in TODO_SLUGS if slug in slugs), None)
    if todo_slug is None:
        raise BridgeError("指定项目不存在受支持的 To Do 列，拒绝新建")

    marker = f"<!-- codex-kaneo-review:{dedupe_key} -->"
    matches = [
        task
        for task in project_tasks(client, project_id)
        if marker in str(task.get("description", ""))
    ]
    if len(matches) > 1:
        raise BridgeError("同一 dedupe-key 对应多个 Kaneo 任务，需要人工处理")
    created = False
    if matches:
        task_id = str(matches[0].get("id", ""))
    else:
        description = payload["description"].rstrip()
        body = {
            **payload,
            "description": f"{description}\n\n{marker}" if description else marker,
            "status": todo_slug,
        }
        created_task = unwrap_data(
            client.request(
                f"/api/task/{quote(project_id, safe='')}", "POST", body
            )
        )
        if not isinstance(created_task, dict) or not created_task.get("id"):
            raise BridgeError("Kaneo 新建任务响应格式无效")
        task_id = str(created_task["id"])
        created = True

    context = command_get_task(client, task_id)
    task = context.get("task", {})
    if not isinstance(task, dict) or task.get("projectId") != project_id:
        raise BridgeError("新建或恢复的任务不属于指定项目")
    return {
        **context,
        "created": created,
        "dedupe_key": dedupe_key,
        "todo_slug": todo_slug,
    }


def command_create_todo(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    payload = load_create_payload(args.payload_file)
    return create_todo(client, args.project_id, args.dedupe_key, payload)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list-todos"); p.add_argument("--workspace-id", required=True)
    p = sub.add_parser("list-reviews"); p.add_argument("--workspace-id", required=True)
    p = sub.add_parser("get-task"); p.add_argument("--task-id", required=True)
    p = sub.add_parser("list-columns"); p.add_argument("--project-id", required=True)
    p = sub.add_parser("requirements-hash"); p.add_argument("--source-file", required=True)
    p = sub.add_parser("update-status"); p.add_argument("--task-id", required=True); p.add_argument("--project-id", required=True); p.add_argument("--status", required=True); p.add_argument("--expected-current-status", required=True)
    p = sub.add_parser("create-todo"); p.add_argument("--project-id", required=True); p.add_argument("--dedupe-key", required=True); p.add_argument("--payload-file", required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "requirements-hash":
            try:
                source = json.loads(Path(args.source_file).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise BridgeError("无法读取来源快照") from exc
            if not isinstance(source, dict):
                raise BridgeError("来源快照必须是 JSON 对象")
            print(json.dumps({"requirements_sha256": requirements_hash(source)}))
            return
        client = Client()
        if args.command == "list-todos":
            output = normalize_todos(client, args.workspace_id)
        elif args.command == "list-reviews":
            output = normalize_reviews(client, args.workspace_id)
        elif args.command == "get-task":
            output = command_get_task(client, args.task_id)
        elif args.command == "list-columns":
            output = {"projectId": args.project_id, "columns": columns(client, args.project_id)}
        elif args.command == "update-status":
            output = command_update_status(client, args)
        else:
            output = command_create_todo(client, args)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except BridgeError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()


# ------------------------------------------------------------------ v2 附件下载

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 交给调用方判定重定向目标是否仍同源


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def download_attachment(
    url: str,
    dest: "Path",
    max_bytes: int = 20 * 1024 * 1024,
    timeout: float = 30.0,
) -> "Path":
    """v2 §5.22.1：把 Kaneo 附件下载到材料目录。

    - 只向 **Kaneo 同源地址** 携带 Authorization；重定向逐跳检查，
      跨域/协议降级 → 拒绝（BridgeError），不泄露凭据。
    - 超过 max_bytes 拒绝（不落半成品：先写 .part 再原子改名）。
    - 返回写入路径；文件名消毒由调用方完成（dest 已是安全路径）。
    """
    import urllib.request

    base_url, token = load_credentials()
    base_origin = _origin(base_url)
    current = url
    seen = set()
    for _hop in range(8):
        if _origin(current) != base_origin:
            raise BridgeError(
                f"附件地址跨源，拒绝下载（不携带凭据）：{current}"
            )
        request = Request(
            current,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "*/*",
            },
        )
        opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                target = exc.headers.get("Location")
                if not target:
                    raise BridgeError(f"附件重定向缺少目标：HTTP {exc.code}")
                current = urljoin(current, target)
                if current in seen:
                    raise BridgeError("附件重定向循环")
                seen.add(current)
                continue
            raise BridgeError(f"附件下载失败：HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BridgeError(
                f"附件下载失败：{type(exc).__name__}"
            ) from exc
        with response:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            size = 0
            try:
                with open(tmp, "wb") as out:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise BridgeError(
                                f"附件超过 {max_bytes // (1024 * 1024)} MiB 上限"
                            )
                        out.write(chunk)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            tmp.replace(dest)
            return dest
    raise BridgeError("附件重定向跳数超限")
