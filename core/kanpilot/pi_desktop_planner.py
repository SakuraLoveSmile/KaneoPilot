"""PI-Desktop-Plus 规划后端执行端（protocol.md §5.24.3，v4/T6.2）。

``planner_runner`` 在请求 payload 的 ``planner_backend == "pi_desktop"`` 时把本轮
整体交给本模块；缺该字段（旧请求）或为 ``codex`` 时 codex 分支行为完全不变。

职责（只做 Plus 分支）：
1. 快照门：``runtime_config`` 五字段齐备、新规划 ``auto``，兼容旧 ``ask``
   （§5.24「v4 会话模式与权限」）、``planner_method`` 必须为 ``builtin``
   （``spec_plan`` 是 Codex 插件方式，Plus 下明确禁用，不复制技能文本冒充）；
2. 输入门：manifest 规范化指纹与请求登记一致（``planner_input_sha256``）；
3. 独立规划 worktree + 材料：git worktree（HEAD 基线）为 projectPath，绝不把
   用户主工作区当会话目录；manifest 材料逐项校验哈希后复制到该 worktree 的材料
   子目录；图片另按 Plus ``AgentPromptAttachment`` 真实形状（绝对路径 + kind）
   随 prompt 发送；缺失/哈希不符/越界/无法解析 → 明确失败
   （``attachment_unsupported``），绝不静默省略图片；
4. 会话：``pi_session_create(mode="plan", projectPath=<worktree>, providerId/
   modelId/thinkingLevel=快照)`` → ``pi_session_configure(permissionMode=快照,
   confirm=true)`` → **发送前落** ``send_state="unknown"`` → ``pi_agent_prompt``
   → 以持久 turn（``pi_turn_get`` 身份核验）取得启动证明后才登记 ``sent``；
5. ``ready`` 的**唯一**来源：本 session/turn 的 ``kind="plan"`` proposal
   （``pi_plans_pending``），严格匹配 sessionId/turnId/toolCallId/proposalId/
   version，绝不从多个旧 proposal 猜选；读取正文后 ``pi_plans_resolve(
   action="reject", confirm=true)`` 关闭该生成阶段契约，并**确认已 rejected**
   （空 pending 清单不算成功）→ 走既有 frontmatter/哈希/``publish-plan``/hold；
6. ``needs_input``：不提交 proposal，最终答复必须严格为
   ``{"status":"needs_input", ...}``（经 ``planner_runner.parse_output`` 校验）
   → 草稿落盘 + ``report needs_user``；
7. 恢复：同一请求此前轮次若已有持久 session/turn，则**只读重连**继续观察
   （绝不重复 ``pi_session_create``/``pi_agent_prompt``）；无持久 turn 的
   ``send_state="unknown"`` 保留人工阻塞。

明确不做：不发 ``approve``、不生成 Plus 执行 turn、不建 Goal 会话、不代批任何
契约、不调用 codex、不自动重试、不静默换后端或方式、不使用 ``pi_project_open``。

失败分类（terminal ``reason`` + ``report.evidence.error_category``，§5.24.3）：
``plan_proposal_missing``、``plan_proposal_ambiguous``、``plan_approval_conflict``、
``plan_reject_failed``、``attachment_unsupported``、``transcript_incomplete``；
同源明确失败另有 ``plan_proposal_unreadable``、``plan_method_unsupported``、
``plan_permission_mode_unsupported``、``plan_backend_config_incomplete``、
``plan_input_mismatch``、``plan_worktree_failed``、``plan_session_setup_failed``、
``plan_prompt_unreadable``、``plan_request_too_large``、``send_unknown``、
``startup_unproven``、``plan_round_unverifiable``、``pi_desktop_unavailable``、
``turn <state>``、``timeout``。均不自动重试。

复用（只读调用，不改共享文件）：``pi_desktop_backend`` 的 transport/脱敏，
``pi_desktop_worker`` 的 ``_Collector``（分页采集 + UiMessage→events 映射）、
``_ReadRecovery``（只读重连）、``_turn_status``/``_turn_identity_matches``。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import (
    bridge, cli, cmd_exec, codex_planner, constants, core, db, dsh_events, headless_adapter,
    pi_desktop_backend, pi_desktop_worker, planfile, planner_runner, round_store, util,
)
from . import runtime_client

# 计划会话固定 mode=plan；权限严格使用请求快照，兼容既有 ask 请求。
_PLAN_SESSION_MODE = constants.PI_DESKTOP_PLAN_MODE
_PLAN_PERMISSION_MODES = (
    constants.PI_DESKTOP_READONLY_PERMISSION_MODE,
    constants.PI_DESKTOP_PLANNER_PERMISSION_MODE,
)
# 规划 worktree 内材料子目录（projectPath 之内，Plus 附件路径校验要求）。
_MATERIAL_DIR_NAME = ".kaneopilot-plan-material"
_POLL_SECONDS = 2.0
_PROOF_WAIT_SECONDS = 30.0          # 启动证明（持久 turn 身份可读）等待上限
_PROOF_POLL_SECONDS = 0.5
_PROPOSAL_WAIT_SECONDS = 20.0       # completed 后等 pending plan 产物出现的宽限
_PROPOSAL_POLL_SECONDS = 1.0
_REJECT_CONFIRM_SECONDS = 15.0      # reject 后确认 rejected 的等待上限
_REJECT_CONFIRM_POLL_SECONDS = 1.0
_STOP_GRACE_SECONDS = 10.0
_STOP_VERIFY_SECONDS = 30.0
_RUNTIME_KEYS = constants.EXEC_PI_DESKTOP_KEYS
_RUNTIME_MAX_BYTES = constants.PI_DESKTOP_MCP_MAX_REQUEST_BYTES
# Plus host 认得的图片扩展名（prompt-attachments.ts IMAGE_EXTENSIONS）。
_IMAGE_SUFFIXES = frozenset(constants.PLAN_IMAGE_SUFFIXES) | {
    ".gif", ".bmp", ".heic", ".avif", ".tif", ".tiff", ".svg",
}
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".heic": "image/heic", ".avif": "image/avif", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".svg": "image/svg+xml",
}
# 已关闭的产物状态（reject 确认只认 rejected）。
_TERMINAL_PROPOSAL_STATUSES = ("rejected", "approved", "changes_requested",
                               "expired", "interrupted")
_PLUS_INSTRUCTION = """
## 本轮执行方式（Pi-Desktop-Plus 计划模式，protocol §5.24.3）

- 本会话固定在 **Plan 模式**、权限为 **Ask**：工具调用需要用户确认，不要假设
  自动批准。
- 你的工作目录是**独立规划 worktree**（HEAD 基线，不是用户主工作区）：除计划
  产物外不要在里面创建/修改文件，不要修改业务源码。
- 材料已复制到 `{MATERIAL_DIR}`（清单 `{MANIFEST_PATH}`）；图片作为本轮附件
  发送，绝对路径如下：
{ATTACHMENT_BLOCK}
- 计划正文必须经 **SubmitPlan** 提交（kind=plan），把完整计划 Markdown 放进
  提交内容。计划元数据（task_id/各 sha256/模型名）由调度核心按真实输入写入
  frontmatter——**不要**自己写 frontmatter，不要自报 ID/模型名/哈希。
- 缺少必要信息、无法产出可靠计划时：**不要** SubmitPlan；本轮以严格 JSON 结束：
  `{"status": "needs_input", "plan_markdown": "<草稿，可为空>",
    "questions": ["..."], "evidence": ["..."]}`
- 计划用中文、自包含、可执行（含文件级步骤与验证方式）；证据缺口如实标注，
  不伪造。
- 禁止：修改业务源码、访问网络、安装依赖、提交/推送、输出凭据或密钥、消费与
  任务无关的付费 API。
"""


# ------------------------------------------------------------ 基础辅助

def manifest_sha(manifest: dict) -> str:
    """``planner_input_sha256`` 的规范化口径（与 ``planner_inputs`` 同源）。"""
    canonical = json.dumps(
        bridge.stable(manifest), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return util.sha256_bytes(canonical)


def _safe_name(name: str) -> str:
    cleaned = "".join(
        ch if (ch.isalnum() or ch in " ._-()[]（）") else "_"
        for ch in str(name)
    ).strip()
    return cleaned or "material"


def _git(directory: Path, *args: str) -> str:
    """执行只读 git 查询；失败抛 KanpilotError（脱敏后的 stderr 摘要）。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise util.KanpilotError("plan_worktree_failed",
                                 f"git {' '.join(args)} 执行失败：{exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        raise util.KanpilotError(
            "plan_worktree_failed",
            f"git {' '.join(args)} 退出码 {result.returncode}"
            + (f"：{tail[-1][:200]}" if tail else ""))
    return result.stdout.strip()


def _is_image(media_type, name: str) -> bool:
    if isinstance(media_type, str) and media_type.lower().startswith("image/"):
        return True
    return Path(str(name)).suffix.lower() in _IMAGE_SUFFIXES


def _fallback_model_pair(plan_kind: str) -> dict:
    """§6：请求缺 ``model_config`` 快照时的历史回落常量。"""
    source = (constants.REVIEW_FIX_PLANNER if plan_kind == "review_fix"
              else constants.STANDARD_PLANNER)
    return dict(source)


def _published_model_pair(context: dict, manifest: dict, generated_at: str,
                          task_row, req_payload: dict, runtime: dict,
                          plan_kind: str):
    """发布用的 ``{planner_model, reasoning_effort}`` + 来源标签 + 备注。

    期望值直接取核心的 ``planfile.expected_model_snapshot``（**同一解析口径**）：
    codex ⇒ ``model_config``；Plus ⇒ ``runtime_config`` 的
    ``model_id``/``thinking_level``（§5.24.1 明确不把 codex ``reasoning_effort``
    当 Plus 思考等级）；两者都缺（旧记录）⇒ 回落 §6 历史常量。
    必须与 ``publish-plan`` 用同一口径，否则一边写 §6 常量、一边按 Plus 快照
    校验就会互相拒收（Plus 计划会被误判为 frontmatter 不符）。
    """
    expected = planfile.expected_model_snapshot(req_payload)
    runtime_pair = {
        "planner_model": runtime.get("model_id") or "",
        "reasoning_effort": runtime.get("thinking_level") or "",
    }
    candidates = []
    if expected is not None:
        preset = req_payload.get("model_config")
        source = ("request_snapshot"
                  if isinstance(preset, dict) and preset.get("planner_model")
                  else "runtime_config_snapshot")
        candidates.append((dict(expected), source))
    if expected != runtime_pair:
        candidates.append((runtime_pair, "runtime_config_snapshot"))
    candidates.append((_fallback_model_pair(plan_kind),
                       "compat_constants_fallback"))
    note = None
    manifest_task = manifest.get("task") or {}
    req_proj_snap = (context.get("payload") or {}).get("project_snapshot")
    expected_project = req_proj_snap or (
        {
            "project_id": manifest_task.get("project_id") or (context.get("task") or {}).get("project_id"),
            "project_name": manifest_task.get("project_name") or (context.get("task") or {}).get("project_name"),
            "repository": manifest_task.get("repository") or (context.get("task") or {}).get("repository"),
        } if (manifest_task.get("project_id") or (context.get("task") or {}).get("project_id")) else None
    )
    for pair, source in candidates:
        meta = planner_runner.build_plan_meta(
            context, manifest, generated_at, task_row=task_row,
            model_config=pair,
        )
        try:
            planfile.check_plan_meta(meta, plan_kind, expected_model=expected, expected_project=expected_project)
            return pair, source, note
        except planfile.PlanValidationError as exc:
            if "planner_model" not in str(exc.message):
                # 与本候选无关（缺字段/repository 等）→ 交真实发布路径报错。
                return pair, source, str(exc.message)
            note = str(exc.message)
            continue
    pair, source = candidates[-1]
    return pair, source, note


class _AnswerLog:
    """events.jsonl 追加器包装：转发落盘 + 留存助手正文候选。

    ``pi_desktop_worker._Collector`` 只依赖 ``log.emit(etype, ts, **fields)``，
    因此包装后事件形状不变，同时拿到「最终答复」候选（§5.24.3-6）。
    """

    def __init__(self, log, answers: list):
        self._log = log
        self.answers = answers

    def emit(self, etype: str, ts, **fields):
        if etype == "assistant_message":
            content = fields.get("content")
            if isinstance(content, str) and content.strip():
                self.answers.append(content)
        return self._log.emit(etype, ts, **fields)


class _Materials:
    """已校验的规划材料：材料目录 + 文件清单 + Plus 附件（图片）。"""

    def __init__(self, directory: Path, files: list, attachments: list,
                 manifest_path: Path):
        self.directory = directory
        self.files = files
        self.attachments = attachments
        self.manifest_path = manifest_path


# ------------------------------------------------------------ 轮次

class _Round:
    """一个 Plus 规划轮次（claim 之后由本模块接管）。"""

    def __init__(self, db_path, request_id: str, run_id: str, *,
                 context: dict, task: dict, task_id: str, run_dir: Path,
                 timeout, heartbeat_every):
        self.db_path = Path(db_path)
        self.request_id = request_id
        self.run_id = run_id
        self.context = context or {}
        self.task = task or {}
        self.task_id = task_id
        self.run_dir = Path(run_dir)
        self.timeout = float(timeout)
        self.heartbeat_every = max(1.0, float(heartbeat_every))
        self.req_payload = self.context.get("payload") or {}
        self.manifest = self.req_payload.get("manifest") or {}
        raw_runtime = self.req_payload.get("runtime_config")
        self.runtime = raw_runtime if isinstance(raw_runtime, dict) else None
        self.plan_kind = (
            self.task.get("plan_kind")
            or (self.manifest.get("task") or {}).get("plan_kind")
            or "standard"
        )
        raw_input_dir = self.context.get("input_dir")
        self.input_dir = Path(raw_input_dir) if raw_input_dir else None
        self.job = {
            "task_id": task_id, "run_id": run_id,
            "request_id": request_id, "backend": "pi_desktop",
            "session_id": None, "session_title": None, "turn_id": None,
            "config": self.runtime,
            "input_id": self.context.get("input_id"),
            "planner_input_sha256": self.req_payload.get("planner_input_sha256"),
            "planner_method": self.req_payload.get("planner_method") or "builtin",
            "plan_kind": self.plan_kind,
            "pid": os.getpid(), "status": "running",
            "started_at": util.now_iso(), "ended_at": None, "exit_code": None,
            "detail": None, "send_state": None,
        }
        self._secrets: list = []
        self.answers: list = []
        self.client = None
        self.collector = None
        self.observer = None
        self.raw_log = planner_runner._EventLog(
            self.run_dir / dsh_events.EVENT_LOG_NAME,
            redact=lambda text: util.redact(text, self._secrets),
        )
        self.log = _AnswerLog(self.raw_log, self.answers)
        self._heartbeat_next = time.monotonic() + self.heartbeat_every
        self._heartbeat_failures = 0
        self._lease_lost = False
        self._lease_detail = None

    # ---------------------------------------------------------- 记录与汇报

    def write_job(self) -> None:
        planner_runner._write_json(self.run_dir / dsh_events.JOB_NAME, self.job)

    def emit(self, etype: str, **fields) -> None:
        self.log.emit(etype, util.now_iso(), **fields)

    def finish(self, status: str, exit_code, detail, outcome: str,
               reason: str) -> None:
        self.job.update({
            "status": status, "exit_code": exit_code,
            "ended_at": util.now_iso(), "detail": detail,
        })
        self.write_job()
        self.log.emit("terminal", util.now_iso(), outcome=outcome, reason=reason)
        conn = db.connect(self.db_path)
        try:
            planner_runner._mark_round_finished(
                conn, self.task_id, self.run_id,
                "finished" if status == "finished" else "failed",
                util.now_iso(),
            )
        finally:
            conn.close()

    def fail(self, detail, reason: str, *, failure_code: str = "planner_failed",
             evidence: Optional[dict] = None) -> bool:
        """明确失败：写终态 + 一条 failed 汇报（§5.22.3/§5.24.3）。"""
        detail = str(detail)
        self.finish("failed", 1, detail, "failed", reason)
        payload_evidence = {"error_category": reason}
        payload_evidence.update({
            key: value for key, value in (evidence or {}).items()
            if value is not None
        })
        planner_runner._report(
            self.db_path, self.request_id, self.run_id, "failed", detail,
            failure_code=failure_code, evidence=payload_evidence,
        )
        return True

    def abandon(self, detail, reason: str) -> bool:
        """租约丢失（stale_run）：只写终态，不汇报、不发布（所有权已失）。"""
        self.finish("failed", 1, str(detail), "failed", reason)
        return True

    def defer(self, detail: str) -> bool:
        """恢复观察未成立：不改既有轮次终态、不汇报，留给下一次领取再试。"""
        self.job.update({
            "resume_detail": detail,
            "resume_deferred_at": util.now_iso(),
            "resume_attempts": int(self.job.get("resume_attempts") or 0) + 1,
            "status": "running",
        })
        self.write_job()
        self.log.emit("system", util.now_iso(),
                      content=f"plan_resume_deferred: {detail}")
        return True

    def heartbeat(self, progress: str = "planning") -> bool:
        """按 ≤ min(lease/3, 60)s 续租；被拒 → 停止本轮（不重发、不发布）。

        瞬时错误三次熔断（与 codex 分支同口径）。
        """
        if time.monotonic() < self._heartbeat_next:
            return True
        try:
            planner_runner._heartbeat(
                self.db_path, self.request_id, self.run_id, progress)
            self._heartbeat_failures = 0
        except util.KanpilotError as exc:
            if exc.code in ("stale_run", "conflict", "not_found"):
                self._lease_lost = True
                self._lease_detail = f"lease lost: {exc}"
                return False
            self._heartbeat_failures += 1
            if self._heartbeat_failures >= 3:
                self._lease_lost = True
                self._lease_detail = f"heartbeat failed x3: {exc}"
                return False
        except Exception as exc:  # noqa: BLE001 - 与 codex 分支同口径
            self._heartbeat_failures += 1
            if self._heartbeat_failures >= 3:
                self._lease_lost = True
                self._lease_detail = f"heartbeat failed x3: {exc}"
                return False
        self._heartbeat_next = time.monotonic() + self.heartbeat_every
        return True

    def _evidence(self) -> dict:
        proposal = self.job.get("plan_proposal") or {}
        return {
            "plan_session_id": self.job.get("session_id"),
            "plan_turn_id": self.job.get("turn_id"),
            "send_state": self.job.get("send_state"),
            "plan_proposal_id": proposal.get("id"),
            "worktree_path": self.job.get("worktree_path"),
        }

    def _token(self):
        return self._secrets[-1] if self._secrets else None

    def _refresh_round(self, *, config=None, session_id=None,
                       session_title=None, worktree_path=None) -> None:
        """把真实后端/快照/会话身份刷进轮次索引（claim 固定写了 codex）。"""
        conn = db.connect(self.db_path)
        try:
            if config is not None:
                round_store.upsert_on_claim(
                    conn, self.task_id, self.run_id, config,
                    util.now_iso(), backend="pi_desktop")
            if session_id or session_title:
                round_store.upsert_discovered(
                    conn, self.task_id, self.run_id, util.now_iso(),
                    backend="pi_desktop", session_id=session_id,
                    session_title=session_title)
            if worktree_path:
                round_store.mark_started(
                    conn, self.task_id, self.run_id, util.now_iso(),
                    worktree_path=str(worktree_path))
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------- 主体

    def run(self) -> bool:
        try:
            return self._run()
        except Exception as exc:  # noqa: BLE001 - 本轮意外退出必须收束 claim
            return self._fail_unexpected(exc)

    def _fail_unexpected(self, exc: Exception) -> bool:
        # 异常文本可能包含连接凭据或整个请求，故只记录异常类型。
        reason = "planner_internal_error"
        detail = (f"{reason}: 规划进程异常退出（{type(exc).__name__}）；"
                  "本轮已停止，未自动重试")
        ctx = cli.Context(self.db_path)
        try:
            def guard(conn):
                request = cmd_exec._claimed_request(
                    conn, self.request_id, self.run_id)
                if (self._lease_lost
                        or request["task_id"] != self.task_id
                        or request["stage"] != "plan"
                        or request["kind"] != "plan"
                        or util.lease_lost(request["lease_expires_at"], util.now_dt())):
                    raise util.KanpilotError("stale_run", "规划轮次归属已失效")

            # 与 report 的写事务共用归属守卫，避免检查后被新 claim 接管。
            # 先成功汇报再写本 run 的终态，失去归属时不覆盖任何现场。
            ctx.conn.authorization_guard = guard
            cmd_exec.cmd_report(ctx, {
                "request_id": self.request_id, "run_id": self.run_id,
                "outcome": "failed", "note": detail,
                "failure_code": "planner_failed", "failure_reason": detail,
                "evidence": {"error_category": reason, **self._evidence()},
            })
        except util.KanpilotError as error:
            if error.code not in ("stale_run", "conflict", "not_found"):
                raise
            return True
        finally:
            ctx.close()
        self.finish("failed", 1, detail, "failed", reason)
        return True

    def _run(self) -> bool:
        self.write_job()
        # §5.24.3：claim 写的是 codex 轮次行；这里刷新为真实后端 + 脱敏快照，
        # 使「每阶段 backend/型号」与运行记录显示真实值。
        self._refresh_round(
            config={key: (self.runtime or {}).get(key) for key in _RUNTIME_KEYS})
        problem = self._check_snapshot()
        if problem is None:
            problem = self._check_input()
        if problem is not None:
            detail, reason = problem
            return self.fail(detail, reason, evidence=self._evidence())
        previous = self._previous_round()
        if previous is not None:
            return self._resume(previous)
        return self._fresh()

    def _check_snapshot(self):
        if self.runtime is None:
            return ("缺少 pi_desktop 运行快照（runtime_config），无法确定本轮"
                    "规划模型与连接", "plan_backend_config_incomplete")
        missing = [key for key in _RUNTIME_KEYS if not self.runtime.get(key)]
        if missing:
            return (f"pi_desktop 运行快照缺字段：{'、'.join(missing)}",
                    "plan_backend_config_incomplete")
        if self.runtime.get("permission_mode") not in _PLAN_PERMISSION_MODES:
            return (f"Plus 计划会话权限必须为 {' 或 '.join(_PLAN_PERMISSION_MODES)}"
                    f"（快照为 {self.runtime.get('permission_mode')!r}）",
                    "plan_permission_mode_unsupported")
        if self.job["planner_method"] != "builtin":
            return (f"Plus 规划只支持 planner_method=builtin（本请求为 "
                    f"{self.job['planner_method']!r}）：spec_plan 是 Codex 插件"
                    "运行方式，Plus 下明确禁用", "plan_method_unsupported")
        return None

    def _check_input(self):
        manifest = self.manifest
        if not isinstance(manifest, dict) or not manifest:
            return ("请求缺少规划材料 manifest", "plan_input_mismatch")
        expected_input = self.req_payload.get("input_id")
        if expected_input and manifest.get("input_id") != expected_input:
            return ("manifest.input_id 与请求登记不符（材料可能被替换）",
                    "plan_input_mismatch")
        expected_sha = self.req_payload.get("planner_input_sha256")
        if expected_sha:
            actual = manifest_sha(manifest)
            if actual != expected_sha:
                return (f"规划材料指纹不符（登记 {str(expected_sha)[:12]}…，"
                        f"实算 {actual[:12]}…）", "plan_input_mismatch")
        files = manifest.get("files")
        if files and not (self.input_dir and self.input_dir.is_dir()):
            return (f"规划材料目录不可用：{self.input_dir}", "plan_input_mismatch")
        return None

    def _previous_round(self) -> Optional[dict]:
        """同一请求此前轮次的现场（job.json）；无则 None。

        ``claim`` 每次都发放新 ``run_id``，故恢复身份必须跨 run 查找：沿
        ``attempts`` 表里本请求的历史 run_id 回溯，读各自的 job.json，只认本模块
        自己写下、带持久 ``session_id`` 的现场。
        """
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT run_id FROM attempts WHERE request_id=? "
                "AND run_id IS NOT NULL ORDER BY id DESC",
                (self.request_id,),
            ).fetchall()
        finally:
            conn.close()
        seen = set()
        for row in rows:
            candidate = row["run_id"]
            if not candidate or candidate == self.run_id or candidate in seen:
                continue
            seen.add(candidate)
            conn = db.connect(self.db_path)
            try:
                source = headless_adapter.resolve_source(conn)
                run_dir = source.run_dir(self.task_id, candidate)
            finally:
                conn.close()
            try:
                job = json.loads(
                    (Path(run_dir) / dsh_events.JOB_NAME).read_text(
                        encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or job.get("backend") != "pi_desktop":
                continue
            if job.get("task_id") != self.task_id:
                continue
            if job.get("request_id") not in (None, self.request_id):
                continue
            if not job.get("session_id"):
                continue
            return job
        return None

    # ---------------------------------------------------------- 恢复（不重发）

    def _resume(self, previous: dict) -> bool:
        prev_run_id = previous.get("run_id")
        session_id = previous.get("session_id")
        turn_id = previous.get("turn_id")
        send_state = previous.get("send_state")
        self.job["resume_from"] = {
            "run_id": prev_run_id, "session_id": session_id,
            "turn_id": turn_id, "send_state": send_state,
            "status": previous.get("status"),
            "started_at": previous.get("started_at"),
        }
        previous_worktree = previous.get("worktree_path")
        previous_config = previous.get("config")
        if turn_id and (
                not isinstance(previous_worktree, str)
                or not previous_worktree
                or not isinstance(previous_config, dict)
                or any(not previous_config.get(key)
                       for key in constants.EXEC_PI_DESKTOP_KEYS)
                or previous_config.get("permission_mode") not in _PLAN_PERMISSION_MODES):
            return self.fail(
                "plan_round_unverifiable: 上一轮缺少持久 worktree 或五字段配置；"
                "不读取当前配置重建连接，也不重发 prompt",
                "plan_round_unverifiable",
                evidence={"resume_from_run_id": prev_run_id},
            )
        if turn_id:
            self.runtime = {
                key: previous_config[key]
                for key in constants.EXEC_PI_DESKTOP_KEYS
            }
            self.job["config"] = dict(self.runtime)
            self.job["worktree_path"] = previous_worktree
            self.scope_run_id = prev_run_id
            old_scope = previous.get("scope_id")
            expected_scope = runtime_client.stable_scope_id(
                "planner", task_id=self.task_id, run_id=prev_run_id,
            )
            if old_scope is not None and old_scope != expected_scope:
                return self.fail(
                    "plan_round_unverifiable: 上一轮 scope 身份不符；未重发 prompt",
                    "plan_round_unverifiable",
                    evidence={"resume_from_run_id": prev_run_id},
                )
            self.historical_read_only = old_scope is None
        if not turn_id:
            if send_state == "unknown":
                self.write_job()
                return self.fail(
                    "send_unknown: 上一轮发送状态不明且没有持久 turn 身份；"
                    "禁止重复 pi_agent_prompt（保留人工阻塞）",
                    "send_unknown",
                    evidence={"resume_from_run_id": prev_run_id,
                              "plan_session_id": session_id})
            # 从未发出（无持久 turn 且非 unknown）→ 本轮可正常发送一次。
            self.job["resume_from"]["resend_allowed"] = True
            self.write_job()
            return self._fresh()
        # 有持久 turn → 只读重连，绝不重发 prompt / 不重建会话。
        remaining = pi_desktop_worker._round_remaining_seconds(
            previous, self.timeout)
        if remaining <= 0:
            return self.defer(
                "round_timeout_elapsed: 上一轮已超出时限，需人工判定终态")
        self.job.update({
            "session_id": session_id,
            "session_title": previous.get("session_title"),
            "turn_id": turn_id,
            "resumed_at": util.now_iso(),
            "send_state": send_state or "unknown",
        })
        self.write_job()
        problem = self._connect()
        if problem:
            return self.defer(f"connection_unavailable: {problem}")
        self._refresh_round(session_id=session_id,
                           session_title=self.job.get("session_title"))
        self.collector = pi_desktop_worker._Collector(
            self.client, session_id, self.log, "")
        self.observer = pi_desktop_worker._ReadRecovery(
            self.client, self.runtime["connection_file"], session_id, turn_id)
        self.collector.client = self.observer
        try:
            record = self.observer.turn_get(session_id, turn_id)
        except util.KanpilotError as exc:
            return self.defer(f"turn_unreadable: {exc.message}")
        if not pi_desktop_worker._turn_identity_matches(
                record, session_id, turn_id):
            return self.fail(
                "plan_round_unverifiable: 持久轮次身份不符（session/turn 与"
                "上一轮记录不一致），未重发 prompt", "plan_round_unverifiable",
                evidence=self._evidence())
        # 持久 turn 即「已发送」的硬证据（原进程的启动证明已完成）。
        self.job["send_state"] = "sent"
        self.job["send_state_proven_by"] = "persistent_turn"
        self.write_job()
        self.emit("session", session_id=session_id,
                  session_title=self.job.get("session_title")
                  or "pi_desktop 计划会话",
                  turn_id=turn_id, resumed=True)
        self.collector.drain()
        return self._observe_until_settled(
            session_id, turn_id, pi_desktop_worker._turn_status(record), record,
            time.monotonic() + remaining)

    # ---------------------------------------------------------- 新轮

    def _fresh(self) -> bool:
        try:
            worktree, mode, materials = self._prepare_materials()
            prompt = self._render_prompt(materials)
        except util.KanpilotError as exc:
            return self.fail(str(exc.message), exc.code or "plan_worktree_failed",
                             evidence=self._evidence())
        self.job.update({
            "worktree_path": str(worktree),
            "worktree_mode": mode,
            "material_dir": str(materials.directory),
            "attachment_count": len(materials.attachments),
        })
        self.write_job()
        problem = self._connect()
        if problem:
            return self.fail(f"pi_desktop_unavailable: {problem}",
                             "pi_desktop_unavailable", evidence=self._evidence())
        try:
            created = self.client.session_create(
                str(worktree), mode=_PLAN_SESSION_MODE,
                provider_id=self.runtime.get("provider_id"),
                model_id=self.runtime.get("model_id"),
                thinking_level=self.runtime.get("thinking_level"),
                title=f"kanpilot plan {self.task_id}")
            session = created.get("session") if isinstance(created, dict) else None
            session = session if isinstance(session, dict) else {}
            session_id = (session.get("id")
                          or (created or {}).get("sessionId")
                          or (created or {}).get("id"))
            if not session_id:
                raise util.KanpilotError(
                    "internal", "pi_session_create 未返回 session.id")
            self.job["session_id"] = str(session_id)
            self.job["session_title"] = (
                session.get("title") or session.get("name"))
            self.write_job()
            # 保留该请求的权限快照；新请求 auto，旧 ask 请求不能被提升。
            self.client.session_configure(
                self.job["session_id"], mode=_PLAN_SESSION_MODE,
                permission_mode=self.runtime["permission_mode"])
        except util.KanpilotError as exc:
            return self.fail(f"plan_session_setup_failed: {exc.message}",
                             "plan_session_setup_failed",
                             evidence=self._evidence())
        self._refresh_round(session_id=self.job["session_id"],
                            session_title=self.job.get("session_title"),
                            worktree_path=worktree)
        self.collector = pi_desktop_worker._Collector(
            self.client, self.job["session_id"], self.log, "")
        # 发送前先落 send_state="unknown"（响应不明不得重发）。
        attachments = list(materials.attachments)
        body = json.dumps(
            {"sessionId": self.job["session_id"], "content": prompt,
             "attachments": attachments},
            ensure_ascii=False,
        ).encode("utf-8")
        if len(body) > _RUNTIME_MAX_BYTES:
            self.job["send_state"] = "not_sent"
            return self.fail(
                f"plan_request_too_large: 请求体 {len(body)} 字节超过 "
                f"{_RUNTIME_MAX_BYTES} 字节上限，发送前拒绝（不截断正文或图片）",
                "plan_request_too_large", evidence=self._evidence())
        self.job["send_state"] = "unknown"
        self.write_job()
        try:
            if attachments:
                result = self.client.agent_prompt(
                    self.job["session_id"], prompt, attachments=attachments)
            else:
                result = self.client.agent_prompt(
                    self.job["session_id"], prompt)
        except TypeError as exc:
            # 后端未提供可选 attachments 参数（本地拒绝，未发出）→ 明确阻塞，
            # 绝不静默省略图片。
            self.job["send_state"] = "not_sent"
            self.write_job()
            return self.fail(
                f"attachment_unsupported: PI-Desktop 后端不支持附件参数（{exc}）",
                "attachment_unsupported", evidence=self._evidence())
        except util.KanpilotError as exc:
            return self._send_lost(exc)
        turn_id = None
        if isinstance(result, dict):
            turn_id = result.get("turnId") or result.get("turn_id")
        if not turn_id:
            return self._send_lost(util.KanpilotError(
                "internal", "pi_agent_prompt 未返回 turnId"))
        self.job["turn_id"] = str(turn_id)
        # 仍保持 send_state=unknown：取得启动证明后才登记 sent（§5.24.3-2）。
        self.write_job()
        self.observer = pi_desktop_worker._ReadRecovery(
            self.client, self.runtime["connection_file"],
            self.job["session_id"], self.job["turn_id"])
        self.collector.client = self.observer
        record, error = self._await_proof(
            self.job["session_id"], self.job["turn_id"])
        if error is not None:
            self.collector.poll()
            self.collector.preserve_unverified(self.run_dir, self._token())
            self.job["send_state"] = "unknown"
            self.write_job()
            return self.fail(
                f"startup_unproven: {error.message}（未重发 prompt）",
                "startup_unproven", evidence=self._evidence())
        self.job["send_state"] = "sent"
        self.job["startup_proven_at"] = util.now_iso()
        self.write_job()
        self.emit("session", session_id=self.job["session_id"],
                  session_title=self.job.get("session_title")
                  or "pi_desktop 计划会话",
                  turn_id=self.job["turn_id"])
        self.emit("user", content=prompt)
        self.collector.drain()
        return self._observe_until_settled(
            self.job["session_id"], self.job["turn_id"],
            pi_desktop_worker._turn_status(record), record,
            time.monotonic() + self.timeout)

    # ---------------------------------------------------------- 连接 / 会话

    def _connect(self) -> Optional[str]:
        """注册 project MCP scope 并核对 Plus 工具门；失败返回中文原因。"""
        config = self.runtime or {}
        try:
            if getattr(self, "historical_read_only", False):
                client = runtime_client.HistoricalReadClient(
                    config.get("connection_file"), timeout=10)
                client.connect(required_tools=constants.PI_DESKTOP_PLAN_TOOLS)
            else:
                client = runtime_client.RuntimeClient(
                    role="planner", connection_file=config.get("connection_file"),
                    project_path=str(self.job.get("worktree_path") or ""),
                    runtime_config={key: config.get(key)
                                    for key in constants.EXEC_PI_DESKTOP_KEYS},
                    task_id=self.task_id, request_id=self.request_id,
                    run_id=getattr(self, "scope_run_id", self.run_id),
                    timeout=10, lease_seconds=self.timeout,
                    read_only=hasattr(self, "scope_run_id"),
                )
                client.connect(required_tools=constants.PI_DESKTOP_PLAN_TOOLS)
        except util.KanpilotError as exc:
            return f"项目 MCP/Plus 工具检查失败：{exc.message}"
        self.client = client
        scope_id = getattr(client, "scope_id", None)
        if scope_id:
            self.job["scope_id"] = scope_id
        self.write_job()
        return None

    def _await_proof(self, session_id: str, turn_id: str):
        """启动证明：持久 turn 身份 + 状态可读（§5.24.3-2）→ (record, None)。"""
        deadline = time.monotonic() + _PROOF_WAIT_SECONDS
        while True:
            try:
                record = self.observer.turn_get(session_id, turn_id)
                break
            except util.KanpilotError as exc:
                # 已受理的 prompt 可能短暂先于持久 turn 行出现。
                if exc.code != "not_found" or time.monotonic() >= deadline:
                    return None, exc
                time.sleep(_PROOF_POLL_SECONDS)
        if not pi_desktop_worker._turn_identity_matches(record, session_id,
                                                       turn_id):
            return None, util.KanpilotError(
                "conflict", "turn_identity_mismatch: 持久轮次身份不符")
        state = pi_desktop_worker._turn_status(record)
        if state not in ("running", "completed", "error", "aborted"):
            return None, util.KanpilotError(
                "conflict", f"turn 状态未知，无法归因：{state!r}")
        return record, None

    def _send_lost(self, exc) -> bool:
        """发送响应不明：``currentTurnId`` 只作候选证据，绝不重发。"""
        recovered = None
        try:
            view = self.client.agent_status(self.job["session_id"])
            inner = view.get("status") if isinstance(view.get("status"), dict) \
                else view
            if isinstance(inner, dict):
                recovered = inner.get("currentTurnId")
        except util.KanpilotError:
            recovered = None
        if self.collector is not None:
            self.collector.poll()
            self.collector.preserve_unverified(self.run_dir, self._token())
        self.job["send_state"] = "unknown"
        if recovered:
            self.job["turn_id"] = str(recovered)
        detail = (f"send_unknown: prompt 响应不明（{exc.message}）；"
                  + (f"currentTurnId 仅作候选证据：{recovered}" if recovered
                     else "无候选 turn"))
        self.write_job()
        return self.fail(detail, "send_unknown", evidence=self._evidence())

    # ---------------------------------------------------------- worktree / 材料

    def _prepare_materials(self):
        """独立规划 worktree + 材料复制；失败抛明确 KanpilotError。"""
        repository = (self.task.get("repository")
                      or (self.manifest.get("task") or {}).get("repository")
                      or "")
        if not repository:
            raise util.KanpilotError("plan_worktree_failed",
                                     "任务缺少 repository，无法建独立规划 worktree")
        repo = Path(str(repository)).expanduser()
        if not repo.is_dir():
            raise util.KanpilotError("plan_worktree_failed",
                                     f"任务仓库不可用：{repo}")
        conn = db.connect(self.db_path)
        try:
            root = constants.worktrees_root(
                db.get_setting(conn, "worktrees_root") or None)
            manual_branch = (self.req_payload.get("delivery_mode") == "manual"
                             and self.req_payload.get("plan_cwd"))
            if manual_branch:
                task = db.require_task(conn, self.task_id)
                delivery = core.delivery_of(task)
                if (core.automation_of(task).get("manual_takeover") is not True
                        or delivery.get("mode") != "manual"
                        or not delivery.get("worktree_path")
                        or Path(self.req_payload["plan_cwd"]).resolve()
                        != Path(delivery["worktree_path"]).resolve()):
                    raise util.KanpilotError(
                        "worktree_invalid", "手动接管规划工作树与任务登记不符")
        finally:
            conn.close()
        from . import git_delivery
        try:
            if manual_branch:
                git_delivery.verify_branch_worktree(
                    dict(delivery, repository=str(repo)),
                    require_clean=False, expected_head=None)
                worktree = Path(delivery["worktree_path"])
                mode = "manual"
            elif self.req_payload.get("delivery_head_sha"):
                worktree = Path(git_delivery.prepare_snapshot_worktree(
                    repo, self.task_id, sha=self.req_payload["delivery_head_sha"],
                    purpose="plan", worktrees_root=root))
                mode = "git"
            else:
                worktree = root / self.task_id / self.run_id
                mode = self._ensure_worktree(repo, worktree)
        except git_delivery.GitDeliveryError as exc:
            # Git 已脱敏 URL/认证形态；再去掉本轮凭据的裸值。
            # 准备工作树先于 MCP 连接，读取连接记录只为脱敏，不发请求。
            secrets = list(self._secrets)
            try:
                connection = pi_desktop_backend.load_connection(
                    (self.runtime or {}).get("connection_file"))
            except (util.KanpilotError, OSError, ValueError):
                pass
            else:
                secrets.append(connection["token"])
            raise util.KanpilotError(
                exc.code, util.redact(exc.message, secrets)) from exc
        materials_dir = util.inside(worktree / _MATERIAL_DIR_NAME / self.run_id,
                                   worktree, "plan_materials")
        try:
            materials_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise util.KanpilotError("plan_worktree_failed",
                                     f"材料目录创建失败：{exc}") from exc
        files, attachments = self._copy_materials(materials_dir)
        manifest_path = materials_dir / "manifest.json"
        util.inside(manifest_path, worktree, "plan_manifest")
        try:
            manifest_path.write_text(
                json.dumps(self.manifest, ensure_ascii=False, sort_keys=True,
                           indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            raise util.KanpilotError("plan_worktree_failed",
                                     f"材料清单写入失败：{exc}") from exc
        return worktree, mode, _Materials(materials_dir, files, attachments,
                                          manifest_path)

    def _ensure_worktree(self, repo: Path, worktree: Path) -> str:
        """git worktree（HEAD 基线）；失败/非 git → 明确失败。

        绝不退回用户主工作区：SubmitPlan 的产物落在 projectPath，用户仓库不得被
        写入（§5.24.3-1）。
        """
        if worktree.exists():
            top = _git(worktree, "rev-parse", "--show-toplevel")
            common = _git(worktree, "rev-parse", "--path-format=absolute",
                          "--git-common-dir")
            repo_common = _git(repo, "rev-parse", "--path-format=absolute",
                               "--git-common-dir")
            if (Path(top).resolve() != worktree.resolve()
                    or Path(common) != Path(repo_common)):
                raise util.KanpilotError(
                    "plan_worktree_failed",
                    f"已有规划目录不是本仓库独立 worktree：{worktree}")
            return "reuse"
        if not (repo / ".git").exists():
            raise util.KanpilotError(
                "plan_worktree_failed",
                "Pi-Desktop-Plus 规划要求 Git 仓库与独立 worktree"
                "（避免把计划产物写进用户主工作区）")
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach",
                 str(worktree), "HEAD"],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise util.KanpilotError(
                "plan_worktree_failed", f"git worktree add 执行失败：{exc}") from exc
        if result.returncode != 0:
            tail = util.redact(
                (result.stderr or result.stdout or "").strip(),
                self._secrets)[:300]
            raise util.KanpilotError(
                "plan_worktree_failed",
                f"无法创建独立规划 worktree（git 退出码 "
                f"{result.returncode}）：{tail}")
        return "git"

    def _copy_materials(self, materials_dir: Path):
        """逐项校验 manifest 材料哈希 → 复制到材料目录；图片生成 Plus 附件。"""
        files: list = []
        attachments: list = []
        entries = self.manifest.get("files")
        if not isinstance(entries, list):
            return files, attachments
        base_real = None
        if self.input_dir is not None:
            try:
                base_real = self.input_dir.resolve()
            except OSError:
                base_real = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rel = entry.get("path")
            if not isinstance(rel, str) or not rel.strip():
                continue
            name = str(entry.get("name") or Path(rel).name)
            if base_real is None:
                raise util.KanpilotError(
                    "attachment_unsupported",
                    f"材料目录不可用，无法校验材料：{name}")
            candidate = Path(rel)
            if candidate.is_absolute():
                raise util.KanpilotError(
                    "attachment_unsupported",
                    f"材料路径必须是材料目录内的相对路径：{rel}")
            try:
                src_real = (self.input_dir / candidate).resolve()
            except OSError as exc:
                raise util.KanpilotError(
                    "attachment_unsupported",
                    f"材料不可解析：{name}（{exc}）") from exc
            if src_real != base_real and base_real not in src_real.parents:
                raise util.KanpilotError(
                    "attachment_unsupported", f"材料越出材料目录：{rel}")
            if not src_real.is_file():
                raise util.KanpilotError(
                    "attachment_unsupported", f"材料文件缺失：{name}")
            actual_sha = util.sha256_file(src_real)
            expected_sha = entry.get("sha256")
            if (isinstance(expected_sha, str) and expected_sha
                    and actual_sha != expected_sha):
                raise util.KanpilotError(
                    "attachment_unsupported",
                    f"材料哈希不符：{name}（登记 {expected_sha[:12]}…，"
                    f"实算 {actual_sha[:12]}…）")
            dest = materials_dir / _safe_name(name)
            counter = 1
            while dest.exists():
                stem = Path(_safe_name(name)).stem
                suffix = Path(name).suffix
                dest = materials_dir / f"{stem}-{counter}{suffix}"
                counter += 1
            util.inside(dest, materials_dir, "plan_material")
            try:
                shutil.copyfile(src_real, dest)
            except OSError as exc:
                raise util.KanpilotError(
                    "attachment_unsupported",
                    f"材料复制失败：{name}（{exc}）") from exc
            record = {
                "name": name, "path": str(dest),
                "source": str(entry.get("source") or ""),
                "media_type": entry.get("media_type"),
                "sha256": actual_sha, "size": dest.stat().st_size,
            }
            if entry.get("repository_path"):
                record["repository_path"] = entry["repository_path"]
            files.append(record)
            if _is_image(entry.get("media_type"), name):
                suffix = Path(name).suffix.lower()
                mime = entry.get("media_type")
                if not (isinstance(mime, str)
                        and mime.lower().startswith("image/")):
                    mime = _IMAGE_MIME.get(suffix, "application/octet-stream")
                # Plus AgentPromptAttachment 真实形状（绝对路径 + kind）。
                attachments.append({
                    "path": str(dest), "name": name, "kind": "image",
                    "mimeType": mime, "size": record["size"],
                })
        return files, attachments

    def _render_prompt(self, materials: _Materials) -> str:
        """渲染 ``planner_prompt.md`` + Plus 计划模式附加指令（§5.24.3）。"""
        template = (constants.core_root() / "executor_prompts"
                    / "planner_prompt.md")
        try:
            text = template.read_text(encoding="utf-8")
        except OSError as exc:
            raise util.KanpilotError(
                "plan_prompt_unreadable",
                f"规划指令模板不可读：{template}（{exc}）") from exc
        manifest_task = self.manifest.get("task") or {}
        files_block = ("材料文件：\n" + "\n".join(
            f"- {record['name']} → {record['path']}"
            + (f"（工作树路径：{record['repository_path']}）"
               if record.get("repository_path") else "")
            for record in materials.files)
            if materials.files else "")
        supplement = (self.manifest.get("supplement") or "").strip()
        supplement_block = ("## 用户补充说明\n\n" + supplement
                            if supplement else "")
        prompt = (text
                  .replace("{TASK_TITLE}", str(
                      self.task.get("title") or manifest_task.get("title")
                      or ""))
                  .replace("{TASK_ID}", str(self.task_id or ""))
                  .replace("{REPOSITORY}", str(
                      self.task.get("repository")
                      or manifest_task.get("repository") or "."))
                  .replace("{INPUT_DIR}", str(materials.directory))
                  .replace("{FILES_BLOCK}", files_block)
                  .replace("{EXECUTION_WORKTREE_BLOCK}", codex_planner._execution_worktree_block(self.manifest))
                  .replace("{SUPPLEMENT_BLOCK}", supplement_block))
        attachment_block = "\n".join(
            f"  - {item['name']} → {item['path']}"
            for item in materials.attachments
        ) or "  - （本轮无图片附件）"
        if self.req_payload.get("fix_mode") == "same_task":
            prompt += "\n## 同任务修复轮\n\n工作树已含前几轮提交，只修 Review 报告列出的问题，verify 要覆盖修复点。\n"
        instruction = _PLUS_INSTRUCTION
        if self.req_payload.get("delivery_mode") == "manual" and self.req_payload.get("plan_cwd"):
            instruction = instruction.replace(
                "独立规划 worktree**（HEAD 基线，不是用户主工作区）",
                "已登记的任务分支 worktree**（手动接管后的当前状态，含未提交改动）")
        return prompt + instruction.replace(
            "{MATERIAL_DIR}", str(materials.directory)
        ).replace(
            "{MANIFEST_PATH}", str(materials.manifest_path)
        ).replace(
            "{ATTACHMENT_BLOCK}", attachment_block)

    # ---------------------------------------------------------- 观察

    def _observe_until_settled(self, session_id: str, turn_id: str, state: str,
                               record: dict, deadline: float) -> bool:
        while state not in ("completed", "error", "aborted"):
            if time.monotonic() >= deadline:
                return self._timeout(session_id, turn_id)
            if not self.heartbeat():
                return self.abandon(self._lease_detail or "heartbeat failed",
                                    "stale_run")
            try:
                record = self.observer.turn_get(session_id, turn_id)
            except util.KanpilotError as exc:
                if self.collector is not None:
                    self.collector.poll()
                return self.fail(
                    f"plan_round_unverifiable: turn 终态无法核实：{exc.message}"
                    f"；{self._collector_detail()}（未重发 prompt）",
                    "plan_round_unverifiable", evidence=self._evidence())
            state = pi_desktop_worker._turn_status(record)
            if state in ("completed", "error", "aborted"):
                break
            if self.collector is not None:
                self.collector.poll()
            time.sleep(_POLL_SECONDS)
        return self._settle(session_id, turn_id, state, record)

    def _collector_detail(self) -> str:
        if self.collector is None:
            return "无转录采集"
        bits = []
        if self.collector.truncated:
            bits.append("分页链断裂/超限")
        if self.collector.read_error:
            bits.append(f"转录读取失败：{self.collector.read_error}")
        return "；".join(bits) or "转录采集正常"

    def _timeout(self, session_id: str, turn_id: str) -> bool:
        detail = f"timeout after {self.timeout:g}s"
        stop_detail = self._stop_turn(session_id, turn_id)
        if self.collector is not None:
            self.collector.poll()
        return self.fail(
            f"{detail}；{stop_detail}", "timeout",
            failure_code="planner_timeout",
            evidence={**self._evidence(), "stop": stop_detail})

    def _stop_turn(self, session_id: str, turn_id: str) -> str:
        """超时有界停止：stop → 宽限 → abort → 核验；不证实就如实报 unknown。"""
        detail = []
        try:
            self.observer.client.agent_stop(session_id)
            detail.append("stop 请求已接受")
        except util.KanpilotError as exc:
            detail.append(f"stop 响应未证实：{exc.message}")
        terminal = self._wait_terminal(session_id, turn_id,
                                      _STOP_GRACE_SECONDS)
        if terminal:
            detail.append(f"turn 终态 {terminal}")
            self.emit("system", content="plan_timeout_stop：" + "；".join(detail))
            return "；".join(detail)
        try:
            self.observer.client.agent_abort(session_id)
            detail.append("abort 请求已接受")
        except util.KanpilotError as exc:
            detail.append(f"abort 响应未证实：{exc.message}")
        terminal = self._wait_terminal(session_id, turn_id,
                                      _STOP_VERIFY_SECONDS)
        detail.append(f"turn 终态 {terminal}" if terminal
                      else "停止未证实，Plus 侧可能仍在运行（unknown）")
        self.emit("system", content="plan_timeout_stop：" + "；".join(detail))
        return "；".join(detail)

    def _wait_terminal(self, session_id: str, turn_id: str,
                       seconds: float) -> Optional[str]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                record = self.observer.turn_get(session_id, turn_id)
                state = pi_desktop_worker._turn_status(record)
                if state in ("completed", "error", "aborted"):
                    return state
            except util.KanpilotError:
                pass
            time.sleep(_POLL_SECONDS)
        return None

    # ---------------------------------------------------------- 终态判定

    def _settle(self, session_id: str, turn_id: str, state: str,
                record: dict) -> bool:
        if self.collector is not None:
            self.collector.poll()
        if self.collector is not None and (self.collector.truncated
                                           or self.collector.read_error):
            parts = []
            if self.collector.truncated:
                parts.append("transcript_incomplete: 分页链断裂/超限")
            if self.collector.read_error:
                parts.append("transcript_incomplete: 转录读取失败"
                             f"（{self.collector.read_error}）")
            return self.fail("；".join(parts), "transcript_incomplete",
                             evidence=self._evidence())
        if self.collector is None or not self.collector.seen:
            return self.fail("transcript_incomplete: 空转录",
                             "transcript_incomplete",
                             evidence=self._evidence())
        if state != "completed":
            turn = record.get("turn") if isinstance(record.get("turn"), dict) \
                else record
            code = str(turn.get("errorCode") or turn.get("error_code")
                       or state)
            return self.fail(f"turn {state}: {code}", code or "turn_error",
                             evidence=self._evidence())
        try:
            proposal, reason, detail = self._matching_proposal(session_id,
                                                              turn_id)
        except util.KanpilotError as exc:
            return self.fail(f"plan_proposal_unreadable: {exc.message}",
                             "plan_proposal_unreadable",
                             evidence=self._evidence())
        if reason == "plan_proposal_missing":
            return self._needs_input_or_missing(detail)
        if reason is not None:
            return self.fail(detail, reason, evidence=self._evidence())
        markdown = str(proposal.get("markdown") or proposal.get("plan") or "")
        self.collector.poll()
        parsed, _detail = self._final_answer()
        if parsed is not None and parsed.get("status") == "needs_input":
            # §5.24.3-6：needs_input 与 proposal 混入 → 明确失败并保留证据
            # （不擅自变更 Plus 侧状态，也不发布）。
            return self.fail(
                "plan_proposal_ambiguous: 同一轮既有待批准 plan 产物又有 "
                f"needs_input 最终答复（proposal {proposal.get('id')}）",
                "plan_proposal_ambiguous", evidence=self._evidence())
        if not markdown.strip():
            return self.fail(
                "plan_proposal_missing: plan 产物正文为空"
                f"（proposal {proposal.get('id')}）",
                "plan_proposal_missing", evidence=self._evidence())
        ok, reject_reason, reject_detail = self._reject_proposal(
            session_id, turn_id, proposal)
        if not ok:
            return self.fail(reject_detail, reject_reason,
                             evidence=self._evidence())
        return self._publish(markdown, session_id, turn_id, proposal)

    def _matching_proposal(self, session_id: str, turn_id: str):
        """返回 ``(proposal|None, reason|None, detail)``；严格匹配、不猜选。"""
        deadline = time.monotonic() + _PROPOSAL_WAIT_SECONDS
        while True:
            view = self.client.plans_pending(session_id)
            raw = view.get("plans") if isinstance(view, dict) else None
            history = view.get("history") if isinstance(view, dict) else None
            if raw is None:
                raw = []
            if not isinstance(raw, list):
                raise util.KanpilotError(
                    "internal", "pi_plans_pending.plans 不是数组")
            # history 是「近期不可变检查点」：已批准/已关闭的产物只在这里可见，
            # 因此审批冲突必须同时看两处；pending 身份匹配只看待批准清单。
            checkpoints = [
                item for item in (list(raw) + list(history or []))
                if isinstance(item, dict)
                and str(item.get("kind") or "") == "plan"
            ]
            pending = [item for item in raw if isinstance(item, dict)
                       and str(item.get("kind") or "") == "plan"
                       and str(item.get("status") or "") == "pending"]
            matching = [item for item in pending
                        if self._proposal_identity_ok(item, session_id,
                                                      turn_id)]
            if len(matching) == 1:
                return matching[0], None, None
            if len(matching) > 1:
                return None, "plan_proposal_ambiguous", (
                    "plan_proposal_ambiguous: 多个 pending plan 产物同时匹配本 "
                    f"session/turn 身份：{sorted(str(i.get('id')) for i in matching)}"
                    "（不猜选）")
            approved = [item for item in checkpoints
                        if str(item.get("status") or "")
                        in ("approved", "changes_requested")]
            execution = self._execution_turn(session_id, turn_id)
            if approved or execution:
                return None, "plan_approval_conflict", (
                    "plan_approval_conflict: 本轮 plan 产物已被 Plus 侧批准或已"
                    "出现执行轮次（approved="
                    f"{sorted(str(i.get('id')) for i in approved)}，"
                    f"execution_turn={execution}）：禁止本地发布与二次派发，"
                    "也不声称已阻止 Plus 侧执行")
            if time.monotonic() >= deadline:
                if pending:
                    return None, "plan_proposal_ambiguous", (
                        "plan_proposal_ambiguous: 存在 pending plan 产物但身份与"
                        f"本 session/turn 不符："
                        f"{sorted(str(i.get('id')) for i in pending)}")
                return None, "plan_proposal_missing", (
                    "plan_proposal_missing: 本 session/turn 未观察到 kind=plan "
                    "的待批准产物（已 completed 且转录完整；未重发 prompt）")
            time.sleep(_PROPOSAL_POLL_SECONDS)

    def _proposal_identity_ok(self, proposal: dict, session_id: str,
                              turn_id: str) -> bool:
        """§5.24.3-3：严格匹配 sessionId/turnId/toolCallId/proposalId/version。"""
        if str(proposal.get("sessionId") or "") != str(session_id):
            return False
        if str(proposal.get("turnId") or "") != str(turn_id):
            return False
        proposal_id = proposal.get("id")
        tool_call_id = proposal.get("toolCallId")
        version = proposal.get("version")
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            return False
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            return False
        if not isinstance(version, int) or isinstance(version, bool) \
                or version <= 0:
            return False
        return True

    def _execution_turn(self, session_id: str,
                        turn_id: str) -> Optional[str]:
        """同会话中不同于本轮规划 turn 的轮次 = Plus 侧执行轮次（只读证据）。"""
        try:
            view = self.client.agent_status(session_id)
        except util.KanpilotError:
            return None
        inner = view.get("status") if isinstance(view.get("status"), dict) \
            else view
        if not isinstance(inner, dict):
            return None
        current = inner.get("currentTurnId")
        if not current or str(current) == str(turn_id):
            return None
        candidate = str(current)
        try:
            record = self.client.turn_get(session_id, candidate)
        except util.KanpilotError:
            return None
        if pi_desktop_worker._turn_identity_matches(record, session_id,
                                                   candidate):
            return candidate
        return None

    # ---------------------------------------------------------- reject / 发布

    def _reject_proposal(self, session_id: str, turn_id: str, proposal: dict):
        """reject 关闭生成阶段契约并确认 rejected；返回 (ok, reason, detail)。"""
        proposal_id = str(proposal.get("id") or "")
        tool_call_id = str(proposal.get("toolCallId") or "")
        raw_version = proposal.get("version")
        version = (raw_version if isinstance(raw_version, int)
                   and not isinstance(raw_version, bool) else None)
        resolve_error = None
        result = None
        try:
            result = self.client.plans_resolve(
                proposal_id, session_id, turn_id, tool_call_id, "reject",
                version=version)
        except util.KanpilotError as exc:
            resolve_error = exc
        try:
            status, where = self._proposal_status(session_id, proposal_id,
                                                 result)
        except util.KanpilotError as exc:
            return (False, "plan_reject_failed",
                    f"plan_reject_failed: reject 后状态不可核实：{exc.message}")
        if status == "rejected":
            self.job["plan_proposal"] = {
                "id": proposal_id, "session_id": session_id,
                "turn_id": turn_id, "tool_call_id": tool_call_id,
                "version": version, "status": "rejected",
                "title": proposal.get("title"),
                "rejected_at": util.now_iso(),
                "concurrent": bool(resolve_error),
            }
            self.write_job()
            self.emit("system", content=(
                f"plan_proposal_rejected: {proposal_id} → 已导入 KaneoPilot；"
                "此会话不用于执行（未发 approve、未生成执行轮次）"))
            return (True, None, None)
        if status in ("approved", "changes_requested"):
            return (False, "plan_approval_conflict", (
                f"plan_approval_conflict: 生成阶段产物状态为 {status}"
                f"（{where}）：禁止本地发布与二次派发"))
        if resolve_error is not None:
            return (False, "plan_reject_failed", (
                f"plan_reject_failed: reject 调用失败（{resolve_error.message}）；"
                f"{where}；保留产物与身份，不发布"))
        return (False, "plan_reject_failed", (
            f"plan_reject_failed: reject 已提交但未确认 rejected（{where}；"
            "空 pending 清单不算成功），保留产物与身份，不发布"))

    def _proposal_status(self, session_id: str, proposal_id: str, result):
        """确认该 proposal 的关闭状态；返回 ``(status|None, 来源说明)``。"""
        if isinstance(result, dict):
            proposal = result.get("proposal")
            if isinstance(proposal, dict):
                status = str(proposal.get("status") or "")
                if status in _TERMINAL_PROPOSAL_STATUSES:
                    return status, "plans_resolve 返回的 proposal 状态"
        deadline = time.monotonic() + _REJECT_CONFIRM_SECONDS
        while True:
            view = self.client.plans_pending(session_id)
            for bucket in ("plans", "history"):
                items = view.get(bucket) if isinstance(view, dict) else None
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("id") or "") != str(proposal_id):
                        continue
                    status = str(item.get("status") or "")
                    if status in _TERMINAL_PROPOSAL_STATUSES:
                        return status, f"{bucket} 中该产物的状态"
            if time.monotonic() >= deadline:
                return None, (f"{_REJECT_CONFIRM_SECONDS:g}s 内未在 pending/"
                              "history 中观察到该产物的关闭状态")
            time.sleep(_REJECT_CONFIRM_POLL_SECONDS)

    def _publish(self, markdown: str, session_id: str, turn_id: str,
                 proposal: dict) -> bool:
        generated_at = util.now_iso()
        task_row = None
        if self.plan_kind == "review_fix" and self.task_id:
            conn = db.connect(self.db_path)
            try:
                task_row = db.get_task(conn, self.task_id)
            finally:
                conn.close()
        pair, pair_source, note = _published_model_pair(
            self.context, self.manifest, generated_at, task_row,
            self.req_payload, self.runtime or {}, self.plan_kind)
        try:
            plan_path = planner_runner.write_plan_file(
                self.context, self.manifest, markdown, generated_at,
                task_row=task_row, model_config=pair)
        except OSError as exc:
            return self.fail(f"计划文件写入失败：{exc}", "plan_write_failed",
                             evidence=self._evidence())
        self.job["plan_path"] = str(plan_path)
        self.job["model_pair"] = dict(pair)
        self.job["model_pair_source"] = pair_source
        self.job["plan_sha256"] = util.sha256_file(plan_path)
        self.write_job()
        if pair_source == "compat_constants_fallback":
            # 核心当前按 §6 历史常量校验 frontmatter（payload 无 model_config
            # 快照）；降级不静默：运行记录保留 Plus 快照真值与降级原因。
            self.emit("system", content=(
                "plan_model_pair_fallback: frontmatter 按 §6 历史常量 "
                f"{pair.get('planner_model')}/{pair.get('reasoning_effort')} 写入"
                "（核心尚未按 Plus 快照校验）；Plus 快照真值为 "
                f"{self.runtime.get('model_id')}/"
                f"{self.runtime.get('thinking_level')}"
                + (f"（{note}）" if note else "")))
        publish = {
            "task_id": self.task_id,
            "plan_path": str(plan_path),
            "plan_kind": self.plan_kind,
            "source_sha256": self.manifest.get("source_sha256")
            or self.task.get("source_sha256") or "",
            "requirements_sha256": self.manifest.get("requirements_sha256")
            or self.task.get("requirements_sha256") or "",
            "generated_at": generated_at,
            "planner_model": pair.get("planner_model") or "",
            "reasoning_effort": pair.get("reasoning_effort") or "",
            "request_id": self.request_id, "run_id": self.run_id,
            "planner_input_sha256": self.req_payload.get(
                "planner_input_sha256") or "",
        }
        try:
            cli.run("publish-plan", publish, self.db_path)
        except util.KanpilotError as exc:
            return self.fail(f"计划发布被拒：{exc.message}", "publish_failed",
                             evidence=self._evidence())
        self.finish("finished", 0, None, "completed", "plan_published")
        return True

    # ---------------------------------------------------------- 最终答复

    def _final_answer(self):
        """严格解析最终答复（最后一个 assistant_message）→ (parsed, detail)。"""
        if not self.answers:
            return None, "转录中没有助手正文"
        path = self.run_dir / "last-message.txt"
        try:
            path.write_text(self.answers[-1], encoding="utf-8")
        except OSError:
            pass
        try:
            return planner_runner.parse_output(path), None
        except util.KanpilotError as exc:
            return None, exc.message

    def _needs_input_or_missing(self, missing_detail: str) -> bool:
        """无有效 plan 产物：needs_input（按 §5.22.3 处理）或明确失败。"""
        if self.collector is not None:
            self.collector.poll()
        parsed, detail = self._final_answer()
        if parsed is not None and parsed.get("status") == "needs_input":
            draft_path = self.run_dir / "plan-draft.md"
            if parsed["plan_markdown"].strip():
                draft_path.write_text(parsed["plan_markdown"],
                                      encoding="utf-8")
            self.finish("finished", 0, "needs_input", "completed", "needs_input")
            planner_runner._report(
                self.db_path, self.request_id, self.run_id, "needs_user",
                "规划需要补充材料："
                + "；".join(parsed["questions"] or ["见草稿"]),
                failure_code="reported",
                evidence={
                    "questions": parsed["questions"],
                    "draft_path": str(draft_path),
                    "evidence": parsed["evidence"],
                    "plan_session_id": self.job.get("session_id"),
                    "plan_turn_id": self.job.get("turn_id"),
                },
            )
            return True
        if parsed is not None and parsed.get("status") == "ready":
            return self.fail(
                "plan_proposal_missing: 最终答复自称 ready，但本 session/turn "
                "没有有效的 kind=plan 待批准产物（未发布）",
                "plan_proposal_missing", evidence=self._evidence())
        return self.fail(
            f"{missing_detail}；最终答复不是有效的 needs_input JSON：{detail}",
            "plan_proposal_missing", evidence=self._evidence())


def run_one(db_path, request_id: str, run_id: str, *, context: dict,
            task: dict, task_id: str, run_dir, timeout,
            heartbeat_every) -> bool:
    """执行一个 Plus 规划轮次（由 ``planner_runner.run_one`` 分流调用）。"""
    round_ = _Round(
        db_path, request_id, run_id, context=context, task=task,
        task_id=task_id, run_dir=Path(run_dir), timeout=timeout,
        heartbeat_every=heartbeat_every,
    )
    return round_.run()


if __name__ == "__main__":  # pragma: no cover - 无独立 CLI（由 planner_runner 分流）
    raise SystemExit("pi_desktop_planner 由 kanpilot.planner_runner 调用")
