"""Codex 规划适配层（protocol.md §5.22.3 / §5.23.2，v2/T5 + v3/T2）。

规划执行从 pi 分流到 Codex CLI；dispatch/monitor/review 保留 pi。本模块只
负责「拉起参数 + 输出契约 + 事件映射 + 插件探测」，进程生命周期与心跳/
汇报由 ``planner_runner`` 承担。

- ``check_available()``：codex 可执行 + 本机登录态/服务商解析
  （``codex_provider.resolve_provider``）探测；只查存在性，不读凭据值、
  不发起登录；解析失败明确报错。
- ``probe_spec_plugin()``：``codex plugin list --json --marketplace
  personal`` 结构化结果确认 ``spec-planner@personal`` 已安装、启用，
  并在安装源核对 ``.codex-plugin/plugin.json`` 与
  ``skills/spec-plan/SKILL.md``（§5.23.2 能力门；命令失败/输出非法 →
  available:false，不猜）。
- ``output_schema()``：``--output-schema`` 的 JSON Schema（冻结输出契约）。
- ``launcher_argv(job, schema_path, out_path, images, provider=None)``：
  ``codex exec`` argv——``-s read-only``、``--json``、``-i``（图片）、
  ``--output-schema``、``-o``（最后消息落盘）、``-C <repository>``、
  ``--ephemeral``、``--skip-git-repo-check``、provider ``config_args``
  逐项 ``-c``（在 ``-c model=``/``-c model_reasoning_effort=`` 之前）。
  planner_method=builtin（默认）：加 ``--ignore-user-config`` 并回传
  provider 覆写；spec_plan：**不带** ``--ignore-user-config``（该标志
  隔离插件），也不加 provider ``-c``（user config 未被忽略，服务商由
  codex 自身解析）；模型/思考等级 ``-c`` 覆写两方式共用。
- ``build_prompt(task, manifest, input_dir, method="builtin")``：
  spec_plan 时在模板结果前加 ``$spec-plan`` 前缀与本轮「不写文件、完整
  Markdown 放 plan_markdown」指令。
- ``map_event(message)``：``codex exec --json`` 的 JSONL 行 →
  ``(etype, ts, fields)``（与 pi_backend.map_event 同协议）；未知行 → None。
  ``error``/``turn.failed``/错误 item → ``system`` 事件（§5.22.6）。

输出契约（§5.22.3）：``{"status": "ready|needs_input",
"plan_markdown": str, "questions": [str], "evidence": [str]}``。
ready 必须 plan_markdown 非空且 questions 为空；needs_input 由 runner 落为
needs_user。模型自报的 ID/模型名/哈希一律不信（核心按真实输入登记）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from . import constants, util
from . import codex_provider

# codex exec --json 顶层 type → 轮次事件 type（None = 不落盘）。
# ``error``/``turn.failed`` 不进本表：map_event 单独分流为 system 事件
#（§5.22.6 错误链）；``item.completed`` 按 item.type 在 map_event 内分流。
_CODEX_TYPE_MAP = {
    "thread.started": "session",
    "turn.started": None,
    "item.started": None,
    "item.updated": None,
    "item.completed": None,   # 按 item.type 分流（见 map_event）
    "turn.completed": "turn_end",
}

# 只读 sandbox 下允许的图片扩展名（沿用 constants.PLAN_IMAGE_SUFFIXES）。


def check_available() -> Tuple[bool, str]:
    """(ok, detail)：codex 可执行 + 本机认证态 + 服务商配置就绪。

    只查存在性/解析，不读凭据值、不发起登录；「配置就绪」≠「真实服务
    验证成功」。配置损坏 → 明确失败（禁止静默回落官方服务商）。
    """
    binary = constants.codex_bin()
    if not binary:
        return False, "codex 可执行未找到（KANPILOT_CODEX_BIN 或 PATH）"
    home = constants.codex_home()
    try:
        resolution = codex_provider.resolve_provider(home)
    except util.KanpilotError as exc:
        return False, f"codex 配置错误：{exc.message}"
    if resolution.provider_id is None:
        if not (home / "auth.json").is_file():
            return False, f"codex 未登录（缺少 {home}/auth.json）"
        return True, f"codex={binary}；auth={home}；provider=默认路径"
    if not resolution.auth_ready:
        return (
            False,
            f"codex={binary}；provider={resolution.provider_id}/"
            f"{resolution.name}（认证未就绪：{resolution.auth_detail}）",
        )
    has_auth_json = (home / "auth.json").is_file()
    auth_desc = str(home) if has_auth_json else "服务商凭据"
    return (
        True,
        f"codex={binary}；auth={auth_desc}；provider="
        f"{resolution.provider_id}/{resolution.name}（认证就绪）",
    )

# ------------------------------------------------------------ spec_plan 插件（§5.23.2）

SPEC_PLUGIN_ID = "spec-planner@personal"
SPEC_PLUGIN_NAME = "spec-planner"
SPEC_PLUGIN_MARKETPLACE = "personal"
SPEC_MANIFEST_REL = ".codex-plugin/plugin.json"
SPEC_SKILL_REL = "skills/spec-plan/SKILL.md"
SPEC_PROMPT_PREFIX = (
    "$spec-plan\n\n"
    "本轮指令：使用 spec-plan 技能完成本次规划。**本次不写任何文件**；"
    "把完整计划 Markdown 放入输出 JSON 的 plan_markdown 字段，\n"
    "不要在仓库或材料目录创建/修改文件。\n\n"
)


def _spec_unavailable(reason: str, **extra) -> dict:
    result = {
        "available": False,
        "reason": reason,
        "plugin_id": SPEC_PLUGIN_ID,
    }
    result.update(extra)
    return result


def probe_spec_plugin(timeout: int = 15) -> dict:
    """探测 spec-planner 插件是否可用（§5.23.2 能力门）。

    返回 ``{available, reason, plugin_id?, version?, source_path?}``。
    判定全须成立：``codex plugin list --json --marketplace personal``
    可执行且输出合法 JSON → 目标插件 ``installed``/``enabled`` 为真 →
    安装源 ``source.path`` 下 ``SPEC_MANIFEST_REL`` 与 ``SPEC_SKILL_REL``
    均存在 → ``version`` 非空字符串。任一不满足 → available:false +
    中文 reason；命令失败/输出非法同样 false（不猜）。
    """
    binary = constants.codex_bin()
    if not binary:
        return _spec_unavailable(
            "codex 可执行未找到（KANPILOT_CODEX_BIN 或 PATH）")
    try:
        proc = subprocess.run(
            [binary, "plugin", "list", "--json",
             "--marketplace", SPEC_PLUGIN_MARKETPLACE],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return _spec_unavailable(f"codex 可执行不存在：{binary}")
    except subprocess.TimeoutExpired:
        return _spec_unavailable(f"codex plugin list 超时（{timeout}s）")
    except OSError as exc:
        return _spec_unavailable(f"codex plugin list 执行失败：{exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        suffix = f"（{tail[-1][:120]}）" if tail else ""
        return _spec_unavailable(
            f"codex plugin list 退出码 {proc.returncode}{suffix}")
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return _spec_unavailable("codex plugin list 输出不是 JSON")
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, list):
        return _spec_unavailable("codex plugin list 输出缺少 installed 列表")
    entry = next(
        (e for e in installed
         if isinstance(e, dict) and e.get("pluginId") == SPEC_PLUGIN_ID),
        None,
    )
    if entry is None:
        return _spec_unavailable(
            f"插件 {SPEC_PLUGIN_ID} 未安装（personal 市场无记录）")
    if entry.get("installed") is not True:
        return _spec_unavailable(f"插件 {SPEC_PLUGIN_ID} 未处于已安装状态")
    version = entry.get("version")
    version = version.strip() if isinstance(version, str) else None
    if entry.get("enabled") is not True:
        return _spec_unavailable(
            f"插件 {SPEC_PLUGIN_ID} 已安装但被禁用",
            version=version or None)
    source = entry.get("source")
    source_path = (
        source.get("path") if isinstance(source, dict) else None
    )
    if not isinstance(source_path, str) or not source_path.strip():
        return _spec_unavailable(
            f"插件 {SPEC_PLUGIN_ID} 缺少本地安装源路径",
            version=version or None)
    base = Path(source_path).expanduser()
    if not (base / SPEC_MANIFEST_REL).is_file():
        return _spec_unavailable(
            f"插件安装源缺少 manifest（{base / SPEC_MANIFEST_REL}）",
            version=version or None)
    if not (base / SPEC_SKILL_REL).is_file():
        return _spec_unavailable(
            f"插件安装源缺少技能文件（{base / SPEC_SKILL_REL}）",
            version=version or None)
    if not version:
        return _spec_unavailable(
            f"插件 {SPEC_PLUGIN_ID} 未报告版本号，无法固化请求")
    return {
        "available": True,
        "reason": None,
        "plugin_id": SPEC_PLUGIN_ID,
        "version": version,
        "source_path": str(base),
    }


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


def build_prompt(task: dict, manifest: dict, input_dir: Optional[Path],
                 method: str = "builtin") -> str:
    """渲染 executor_prompts/planner_prompt.md 为本次规划 prompt。

    材料正文不写进 argv（过长且含用户内容）；runner 把 prompt 经 stdin
    传入（codex exec 以 "-" 从 stdin 读 PROMPT）。``method="spec_plan"``
    时在渲染结果前加 ``$spec-plan`` 技能引用与本轮「不写文件」指令
    （§5.23.2）；builtin 不变。
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
    prompt = (template
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
    if method == "spec_plan":
        prompt = SPEC_PROMPT_PREFIX + prompt
    return prompt


def launcher_argv(
    job: dict, schema_path: Path, out_path: Path, images: list,
    provider=None,
) -> list:
    """组装 codex exec argv（protocol §5.22.3 / §5.23.2）。

    ``job`` 取自 claim context / job 行：model_config（planner_model/
    reasoning_effort）、repository（cwd）、planner_method。prompt 经 stdin
    （末尾追加 "-"，stdin 作为 PROMPT）。``--ephemeral`` 不写会话文件；
    ``-c model=``/``-c model_reasoning_effort=`` 以只读本次配置指定模型与
    思考档（不可用即报告，不替换）。

    planner_method（job["planner_method"]，缺字段按 builtin）：
    - builtin：``--ignore-user-config`` 隔离用户规则/插件/MCP，并把
      ``provider``（codex_provider.ProviderResolution，可选）的连接配置经
      ``provider.config_args`` 逐项 ``-c`` 传回（在 model/effort 覆写
      之前）；凭据类字段绝不进入 argv（内联 token 只走 env）。
    - spec_plan：**去掉** ``--ignore-user-config``（该标志隔离插件），
      且不加 provider ``-c`` 覆写——user config 未被忽略，服务商由
      codex 自身按 config.toml 解析（§5.23.2）。
    """
    model_config = job.get("model_config") or {}
    model = model_config.get("planner_model") or ""
    effort = model_config.get("reasoning_effort") or ""
    cwd = job.get("repository") or "."
    spec_mode = job.get("planner_method") == "spec_plan"

    argv = [
        constants.codex_bin(),
        "exec",
        "-s", "read-only",            # 只读 sandbox
        "--json",                     # stdout JSONL 进度流
        "--ephemeral",                # 不落会话文件
    ]
    if not spec_mode:
        argv.append("--ignore-user-config")  # builtin：隔离用户配置/插件/MCP
    argv += [
        "--skip-git-repo-check",
        "--color", "never",
        "-C", str(cwd),               # 工作根 = 任务仓库（可读）
        "--output-schema", str(schema_path),
        "-o", str(out_path),          # 最后消息落盘（终态判定旁证）
    ]
    if provider is not None and not spec_mode:
        for config_arg in provider.config_args:
            argv += ["-c", config_arg]
    for image in images:
        argv += ["-i", str(image)]
    if model:
        argv += ["-c", f'model="{model}"']
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv.append("-")  # prompt 从 stdin 读
    return argv


def child_env(provider=None) -> dict:
    """子进程环境：透传父进程 + CODEX_HOME 指向本机 codex 家目录。

    不注入凭据值——登录态由 ~/.codex/auth.json 管理。``provider``（可选）
    的 ``env`` 并入子进程环境：这是内联 bearer token 的唯一落点（内存，
    不进 argv/日志）。
    """
    env = dict(os.environ)
    env["CODEX_HOME"] = str(constants.codex_home())
    env.setdefault("KANPILOT_DB", str(constants.default_db_path()))
    if provider is not None:
        env.update(provider.env)
    return env


def map_event(message: dict) -> Optional[Tuple[str, str, dict]]:
    """codex exec --json JSONL 行 → (etype, ts, fields)；未知 → None。

    ``ts`` 取事件时间戳（毫秒 epoch/ISO）或空串由调用方补当前时刻。
    item.completed 按 item.type 分流：reasoning→assistant_message 草稿、
    agent_message→assistant_message、command_execution→command_output、
    file_change/web_search 等→tool_call/tool_result 近似；其余不落盘。

    §5.22.6 错误链：顶层 ``error``、``turn.failed`` 与 ``item.completed``
    内的错误项（item.type=error/turn_failed 或 item 带 ``error`` 字段）
    都映射为 ``system`` 事件（可读 ``content`` + ``code`` 字段），供
    runner 归类失败原因并在历史轮次透出。
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

    if etype == "error":
        # 顶层错误帧：error 字段（dict/str）优先，回落顶层 message；
        # 可读文本（不再包整帧 JSON），由 runner 汇总归类。
        error = message.get("error")
        if error is not None:
            text = error_text(error)
        else:
            text = error_text(message.get("message"))
        return ("system", ts, {
            "content": text or "codex 报错（未知内容）",
            "code": _error_code(message),
        })

    if etype == "turn.failed":
        # 回合失败帧：error.message + 附注（codex_error_info/http_status）。
        error = message.get("error")
        if isinstance(error, dict):
            text = error_text(error)
        elif isinstance(error, str):
            text = error
        else:
            text = error_text(message.get("message"))
        if not text:
            text = "codex 回合失败（未附错误内容）"
        extras = []
        if isinstance(error, dict):
            info = error.get("codex_error_info")
            if info:
                extras.append(f"codex_error_info={info}")
            status = http_status_of(error)
            if status is not None:
                extras.append(f"http_status={status}")
        return ("system", ts, {
            "content": text + (" [ " + ", ".join(extras) + " ]"
                               if extras else ""),
            "code": _error_code(message),
        })

    if etype == "item.completed":
        item = message.get("item") or {}
        kind = item.get("type") or item.get("item_type")
        if kind in ("error", "turn_failed") or item.get("error") is not None:
            # 错误 item 优先于常规分流 → system 事件（§5.22.6）。
            content = (
                error_text(item.get("error"))
                or item.get("text") or item.get("message") or "未知错误"
            )
            return ("system", ts, {
                "content": content,
                "code": _error_code(item),
            })
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

    mapped = _CODEX_TYPE_MAP.get(etype)
    if mapped is None:
        return None
    return (mapped, ts, {})


# ------------------------------------------------------------ 辅助

_ERROR_TEXT_KEYS = ("message", "msg", "detail", "code")
_HTTP_STATUS_KEYS = ("http_status", "status")


def error_text(value) -> str:
    """错误对象（dict/str/其他）→ 可读文本；提取不出 → ""。

    dict 依次尝试 message/msg/detail/code；均无时退化为 JSON 摘要
    （截断 500 字符，保证事件单行可读）。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in _ERROR_TEXT_KEYS:
            field = value.get(key)
            if isinstance(field, str) and field.strip():
                return field
        for key in _ERROR_TEXT_KEYS:
            field = value.get(key)
            if field is not None and not isinstance(field, (dict, list)):
                return str(field)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:500]
    if value is None:
        return ""
    return str(value)


def http_status_of(value) -> Optional[int]:
    """dict 中的 HTTP 状态码（http_status/status，int）→ int 或 None。"""
    if not isinstance(value, dict):
        return None
    for key in _HTTP_STATUS_KEYS:
        field = value.get(key)
        if isinstance(field, int) and not isinstance(field, bool):
            return field
    return None


def _error_code(message: dict):
    """system 事件的 code 字段：error.code > error http_status >
    顶层 code > 顶层 http_status；没有 → None（emit 省略）。"""
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code not in (None, ""):
            return code
        status = http_status_of(error)
        if status is not None:
            return status
    code = message.get("code")
    if code not in (None, ""):
        return code
    return http_status_of(message)


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
