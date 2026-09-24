"""Codex 规划适配层（protocol.md §5.22.3，v2/T5）。

规划执行从 pi 分流到 Codex CLI；dispatch/monitor/review 保留 pi。本模块只
负责「拉起参数 + 输出契约 + 事件映射」，进程生命周期与心跳/汇报由
``planner_runner`` 承担。

- ``check_available()``：codex 可执行 + 本机登录态（``CODEX_HOME/auth.json``
  存在）探测；只查存在性，不读凭据值、不发起登录。
- ``output_schema()``：``--output-schema`` 的 JSON Schema（冻结输出契约）。
- ``launcher_argv(job, schema_path, out_path)``：``codex exec`` argv——
  ``-s read-only``、``--json``、``-i``（图片）、``--output-schema``、
  ``-o``（最后消息落盘）、``-C <repository>``、``--ephemeral``、
  ``--ignore-user-config``、``--skip-git-repo-check``、``-c model=``/
  ``-c model_reasoning_effort=``。不带第三方插件/MCP/用户规则。
- ``map_event(message)``：``codex exec --json`` 的 JSONL 行 →
  ``(etype, ts, fields)``（与 pi_backend.map_event 同协议）；未知行 → None。

输出契约（§5.22.3）：``{"status": "ready|needs_input",
"plan_markdown": str, "questions": [str], "evidence": [str]}``。
ready 必须 plan_markdown 非空且 questions 为空；needs_input 由 runner 落为
needs_user。模型自报的 ID/模型名/哈希一律不信（核心按真实输入登记）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple

from . import constants

# codex exec --json 顶层 type → 轮次事件 type（None = 不落盘）。
_CODEX_TYPE_MAP = {
    "thread.started": "session",
    "turn.started": None,
    "item.started": None,
    "item.updated": None,
    "item.completed": None,   # 按 item.type 分流（见 map_event）
    "turn.completed": "turn_end",
    "turn.failed": None,      # 由 runner 依退出码/输出判定，不写独立事件
    "error": None,
}

# 只读 sandbox 下允许的图片扩展名（沿用 constants.PLAN_IMAGE_SUFFIXES）。


def check_available() -> Tuple[bool, str]:
    """(ok, detail)：codex 可执行且本机登录态就绪。不读凭据值。"""
    binary = constants.codex_bin()
    if not binary:
        return False, "codex 可执行未找到（KANPILOT_CODEX_BIN 或 PATH）"
    home = constants.codex_home()
    if not (home / "auth.json").is_file():
        return False, f"codex 未登录（缺少 {home}/auth.json）"
    return True, f"codex={binary}；auth={home}"


def output_schema() -> dict:
    """--output-schema 冻结契约（§5.22.3）。附加属性拒绝，字段全必填。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "plan_markdown", "questions", "evidence"],
        "properties": {
            "status": {"type": "string", "enum": ["ready", "needs_input"]},
            "plan_markdown": {"type": "string"},
            "questions": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
    }


def image_files(manifest: dict, input_dir: Optional[Path]) -> list:
    """从材料 manifest 挑出可经 -i 传给 codex 的图片绝对路径。

    只取已落盘、扩展名在白名单内、source 为 attachment/link/local 的文件；
    不存在或越出 input_dir 的条目静默跳过（材料目录不可变，理论不缺）。
    """
    images: list = []
    base = Path(input_dir) if input_dir else None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not base or not isinstance(files, list):
        return images
    try:
        base_real = base.resolve()
    except OSError:
        return images
    for entry in files:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path") or entry.get("name")
        if not isinstance(rel, str) or not rel:
            continue
        if Path(rel).suffix.lower() not in constants.PLAN_IMAGE_SUFFIXES:
            continue
        candidate = base / rel
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if real != base_real and base_real not in real.parents:
            continue
        if real.is_file():
            images.append(str(real))
    return images


def build_prompt(task: dict, manifest: dict, input_dir: Optional[Path]) -> str:
    """渲染 executor_prompts/planner_prompt.md 为本次规划 prompt。

    材料正文不写进 argv（过长且含用户内容）；runner 把 prompt 经 stdin
    传入（codex exec 以 "-" 从 stdin 读 PROMPT）。
    """
    manifest_task = manifest.get("task") or {}
    template_path = (
        Path(__file__).resolve().parent.parent
        / "executor_prompts" / "planner_prompt.md"
    )
    template = template_path.read_text(encoding="utf-8")
    files = manifest.get("files") if isinstance(manifest, dict) else []
    names = [
        str(e.get("path") or e.get("name"))
        for e in files if isinstance(e, dict)
    ]
    files_block = (
        "材料文件：" + "、".join(names) if names else ""
    )
    supplement = (manifest.get("supplement") or "").strip()
    supplement_block = (
        "## 用户补充说明\n\n" + supplement if supplement else ""
    )
    return (template
            .replace("{TASK_TITLE}", str(
                task.get("title") or manifest_task.get("title") or ""))
            .replace("{TASK_ID}", str(
                task.get("task_id") or manifest_task.get("task_id") or ""))
            .replace("{REPOSITORY}", str(
                task.get("repository")
                or manifest_task.get("repository") or "."))
            .replace("{INPUT_DIR}", str(input_dir) if input_dir else "")
            .replace("{FILES_BLOCK}", files_block)
            .replace("{SUPPLEMENT_BLOCK}", supplement_block))


def launcher_argv(
    job: dict, schema_path: Path, out_path: Path, images: list
) -> list:
    """组装 codex exec argv（protocol §5.22.3）。

    ``job`` 取自 claim context / job 行：model_config（planner_model/
    reasoning_effort）、repository（cwd）。prompt 经 stdin（末尾追加 "-"，
    stdin 作为 PROMPT）。``--ephemeral`` 不写会话文件；``--ignore-user-config``
    隔离用户规则/插件/MCP；``-c model=``/``-c model_reasoning_effort=``
    以只读本次配置指定模型与思考档（不可用即报告，不替换）。
    """
    model_config = job.get("model_config") or {}
    model = model_config.get("planner_model") or ""
    effort = model_config.get("reasoning_effort") or ""
    cwd = job.get("repository") or "."

    argv = [
        constants.codex_bin(),
        "exec",
        "-s", "read-only",            # 只读 sandbox
        "--json",                     # stdout JSONL 进度流
        "--ephemeral",                # 不落会话文件
        "--ignore-user-config",       # 不继承用户配置/插件/MCP/规则
        "--skip-git-repo-check",
        "--color", "never",
        "-C", str(cwd),               # 工作根 = 任务仓库（可读）
        "--output-schema", str(schema_path),
        "-o", str(out_path),          # 最后消息落盘（终态判定旁证）
    ]
    for image in images:
        argv += ["-i", str(image)]
    if model:
        argv += ["-c", f'model="{model}"']
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv.append("-")  # prompt 从 stdin 读
    return argv


def child_env() -> dict:
    """子进程环境：透传父进程 + CODEX_HOME 指向本机 codex 家目录。

    不注入凭据值——登录态由 ~/.codex/auth.json 管理。
    """
    env = dict(os.environ)
    env["CODEX_HOME"] = str(constants.codex_home())
    env.setdefault("KANPILOT_DB", str(constants.default_db_path()))
    return env


def map_event(message: dict) -> Optional[Tuple[str, str, dict]]:
    """codex exec --json JSONL 行 → (etype, ts, fields)；未知 → None。

    ``ts`` 取事件时间戳（毫秒 epoch/ISO）或空串由调用方补当前时刻。
    item.completed 按 item.type 分流：reasoning→assistant_message 草稿、
    agent_message→assistant_message、command_execution→command_output、
    file_change/web_search 等→tool_call/tool_result 近似；其余不落盘。
    """
    if not isinstance(message, dict):
        return None
    etype = message.get("type")
    ts = _iso_ms(message.get("timestamp") or message.get("ts"))

    if etype == "thread.started":
        return ("session", ts, {
            "session_id": message.get("thread_id") or message.get("id"),
            "session_title": message.get("cwd") or "codex exec",
        })

    if etype == "turn.completed":
        # 回合结束（worker 终态判定辅助）。
        return ("turn_end", ts, {
            "will_retry": False,
            "usage": message.get("usage"),
        })

    if etype == "item.completed":
        item = message.get("item") or {}
        kind = item.get("type") or item.get("item_type")
        if kind in ("reasoning", "agent_message", "assistant_message"):
            return ("assistant_message", ts, {
                "content": item.get("text") or item.get("content") or "",
            })
        if kind == "command_execution":
            return ("command_output", ts, {
                "name": "exec",
                "input": item.get("command") or item.get("input") or "",
                "output": item.get("aggregated_output")
                or item.get("output") or "",
                "exit_code": _exit_code(item.get("exit_code")),
            })
        if kind in ("file_change", "patch_apply", "web_search", "tool_call"):
            return ("tool_result", ts, {
                "name": kind,
                "output": json.dumps(item, ensure_ascii=False,
                                   sort_keys=True)[:4000],
            })
        return None

    if etype == "error":
        # 顶层错误帧：保留为 system 事件便于 UI 展示（不视为 terminal）。
        return ("system", ts, {
            "content": json.dumps(
                {"error": message.get("message") or message.get("error")},
                ensure_ascii=False,
            ),
        })

    mapped = _CODEX_TYPE_MAP.get(etype)
    if mapped is None:
        return None
    return (mapped, ts, {})


# ------------------------------------------------------------ 辅助

def _iso_ms(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ).isoformat()
        except (ValueError, OSError):
            return ""
    if isinstance(value, str) and value:
        return value
    return ""


def _exit_code(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None
