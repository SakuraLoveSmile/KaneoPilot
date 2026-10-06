"""PI-Desktop-Plus Review 控制端（protocol.md §5.24.5，v4 T6.3）。

``executor_runner --stage review`` 在 ``review_config.backend == "pi_desktop"``
时改走本模块。与 dispatch/monitor 控制轮不同，Review 的登记与推进**由 runner
自己**完成（模型只产出评审 JSON）：

1. ``claim(stage=review)``（核心固定 JSON 命令）→ 以 ``get-task``/``get-request``
   核对请求 ``run_id``、任务本轮执行 ``run_id`` 与 worktree、计划哈希、执行证据
   与关联来源（**不得**只审主仓库）。
2. 建**专属 Review worktree**（不复用仍活动的执行会话），用只读 ``git status``/
   ``git diff`` 生成改动材料，并把计划/（修复周期时的）原评审报告/执行证据复制
   进该 worktree 的材料目录——它是本轮的审查输入。
3. ``pi_session_create(mode="plan", projectPath=<review worktree>)`` +
   ``permissionMode="ask"``；指令只要求审查并输出冻结 JSON，不 SubmitPlan、
   不 SubmitGoal、不代批任何契约。
4. ``completed`` + 完整转录后**严格解析** ``{conclusion, report_markdown, fix}``
   （``priority`` ∈ ``constants.REVIEW_FIX_PRIORITIES``）。意外 proposal 只
   ``reject`` 关闭并记 ``review_blocked``，**不**按其 markdown 登记 pass。
   格式错误/转录不完整/身份不符/权限等待/超时 → 绝不写 pass、不推进 done。
5. 报告写 ``<APP_SUPPORT>/reports/<task_id>/<run_id>.md``（核心重算哈希）；
   手动任务三结论动作：
   - ``pass``：``register-review(pass)`` → ``advance-task(done, in-review)`` →
     ``report succeeded``；
   - ``changes_requested``：**先** ``create-review-fix`` 取得
     ``fix_task_id``/``source_sha256``，**再** ``register-review(changes_requested,
     review_key, fix_task_id, fix_task_source_sha256)`` → ``report succeeded``；
   - ``blocked``：``register-review(blocked)`` → ``report needs_user``。
   自动任务 pass 只登记等待交付；changes_requested 在同任务内登记修复请求。
6. 登记身份：``register-review``/``create-review-fix``/``advance-task`` 都传
   ``request_id`` + ``run_id`` + ``expected_execution_run_id``（核心事务内核对）；
   ``review_key = sha256(规范 JSON(源 task_id, 本周期执行 run_id, 报告 sha))``。
7. 部分成功恢复：job 检查点 + ``get-task``/``get-request`` 事实核对；登记一致
   则跳过 ``register``，``pass`` 已 done 则跳过 ``advance``，同 key fix 已存在
   则不重复创建；``report`` 响应丢失只读核对，**不**重放非幂等调用。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import (
    constants, db, headless_adapter, pi_desktop_controller,
    util, git_delivery, review_settle,
)

from .review_settle import RESULT_SCHEMA

STAGE = "review"
EXECUTOR_ID = "kaneo-review"

# 时序（测试经模块属性加速）。
_POLL_SECONDS = 2.0
_HEARTBEAT_MAX_SECONDS = 60.0            # 心跳间隔上限（≤ min(lease/3, 60)）
_PROOF_WAIT_SECONDS = 30.0               # 复用控制端的启动证明宽限
_GIT_TIMEOUT = 30
_MAX_READ_ERRORS = 3
_TERMINAL_REQUEST_STATUSES = ("succeeded", "failed", "cancelled")

RESULT_KEYS = ("conclusion", "report_markdown", "fix")
FIX_KEYS = ("title", "description", "priority")
INSTRUCTION_MARKER = "## 本轮 Review 身份与材料（Pi-Desktop-Plus 控制端）"


def _sleep(seconds: float) -> None:  # 测试接缝
    time.sleep(seconds)


# --------------------------------------------------------------- 核心命令

def core_call(db_path, command: str, payload: dict) -> dict:
    """核心固定 JSON 命令（in-process 同一入口；状态读写全在核心内）。"""
    from . import cli  # 局部导入：避免与命令模块的导入顺序耦合

    return cli.run(command, payload, db_path)


def _identity_payload(job: dict) -> dict:
    """v4 登记身份（§5.24.5）：新自动链路必传，核心在事务内核对周期。"""
    payload = {
        "request_id": job.get("request_id"),
        "run_id": job.get("run_id"),
    }
    execution_run_id = job.get("execution_run_id")
    if isinstance(execution_run_id, str) and execution_run_id:
        payload["expected_execution_run_id"] = execution_run_id
    return {key: value for key, value in payload.items() if value}


def _facts(db_path, task_id: str, request_id: str) -> dict:
    """核心事实（get-task/get-request）；失败 → 空值（调用方保守处置）。"""
    task = request = None
    try:
        task = core_call(db_path, "get-task", {"task_id": task_id}).get("task")
    except util.KanpilotError:
        task = None
    try:
        request = core_call(
            db_path, "get-request", {"request_id": request_id}
        ).get("request")
    except util.KanpilotError:
        request = None
    return {"task": task, "request": request}



def _persisted_round(conn) -> Optional[dict]:
    """持久 review 控制轮现场（executor_runs.review + 已落盘 job.json）。

    Review 的收尾由 runner 自己做：即使控制轮进程已退出、``finished_at`` 已写，
    只要 ``job.json`` 还没收尾（status != finished），就必须走只读恢复，不能在
    同一请求上另开新轮。返回 ``{entry, job, directory, job_path}`` 或 None。
    """
    entry = pi_desktop_controller.stored_setting(conn, "executor_runs").get(STAGE)
    if not isinstance(entry, dict) or entry.get("backend") != "pi_desktop":
        return None
    invocation_id = entry.get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        return None
    directory = pi_desktop_controller.invocation_dir(STAGE, invocation_id)
    job_path = directory / "job.json"
    if not job_path.is_file():
        return None
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not isinstance(job, dict) or job.get("stage") != STAGE:
        return None
    return {
        "entry": entry, "job": job, "directory": directory,
        "job_path": job_path,
    }


def _request_state(db_path, request_id) -> Optional[dict]:
    """只读请求状态（get-request）；缺 request_id/读取失败 → None。"""
    if not isinstance(request_id, str) or not request_id:
        return None
    try:
        return core_call(db_path, "get-request", {
            "request_id": request_id,
        }).get("request")
    except util.KanpilotError:
        return None

def _lease_valid(request: Optional[dict]) -> bool:
    if not isinstance(request, dict):
        return False
    return not util.lease_lost(request.get("lease_expires_at"), util.now_dt())


def _heartbeat_interval(request: Optional[dict]) -> float:
    lease = None
    if isinstance(request, dict):
        raw = request.get("lease_seconds")
        lease = raw if isinstance(raw, (int, float)) and raw > 0 else None
        if lease is None:
            start = util.parse_ts(request.get("claimed_at"))
            expires = util.parse_ts(request.get("lease_expires_at"))
            if start is not None and expires is not None:
                lease = (expires - start).total_seconds()
    if lease is None:
        lease = float(constants.DEFAULT_LEASE_SECONDS)
    return max(1.0, min(lease / 3.0, _HEARTBEAT_MAX_SECONDS))


# --------------------------------------------------------------- 报告/键

# Preserve the historical public helpers for callers/tests.
from .review_settle import report_root, report_path, review_key, write_report


# --------------------------------------------------------------- 严格解析

def validate_result(value):
    """Legacy tuple API; new backends consume review_settle.validate_result."""
    from .review_settle import _validate_result
    return _validate_result(value)


def parse_result(messages: list) -> Tuple[Optional[dict], Optional[str]]:
    """最终 assistant 答复 → 冻结结果（严格；绝不从自由文本里猜结构）。"""
    text = pi_desktop_controller.last_assistant_text(messages)
    value, error = pi_desktop_controller.strict_json_object(text)
    if error is not None:
        return None, error
    return validate_result(value)


# --------------------------------------------------------------- proposal 拒绝

def reject_proposals(client, session_id: str, turn_id: str
                     ) -> Tuple[bool, str, int]:
    """意外 proposal 只读确认身份后 reject 关闭（绝不 approve）。

    返回 ``(ok, detail, count)``；``reject`` 后必须确认这些 proposal 已不再是
    待批准状态（空清单不单独作为证据）。
    """
    try:
        pending = client.plans_pending(session_id)
    except util.KanpilotError as exc:
        return False, f"plans_pending 读取失败：{exc.message}", 0
    rows = pending.get("plans") if isinstance(pending, dict) else None
    items = [
        item for item in (rows or [])
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    if not items:
        return True, "", 0
    rejected_ids = set()
    for item in items:
        proposal_id = item.get("id")
        if not isinstance(proposal_id, str) or not proposal_id:
            return False, "proposal 缺少 id", len(items)
        if item.get("sessionId") not in (None, session_id) or item.get(
            "turnId"
        ) not in (None, turn_id):
            return False, f"proposal {proposal_id} 身份与本控制轮不符", len(items)
        tool_call_id = item.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return False, f"proposal {proposal_id} 缺少 toolCallId", len(items)
        try:
            client.plans_resolve(
                proposal_id, session_id, item.get("turnId") or turn_id,
                tool_call_id, "reject", version=item.get("version"),
            )
        except util.KanpilotError as exc:
            return False, f"proposal {proposal_id} reject 失败：{exc.message}", len(items)
        rejected_ids.add(proposal_id)
    try:
        after = client.plans_pending(session_id)
    except util.KanpilotError as exc:
        return False, f"reject 后确认读取失败：{exc.message}", len(items)
    rows = after.get("plans") if isinstance(after, dict) else None
    still_pending = {
        item.get("id") for item in (rows or [])
        if isinstance(item, dict) and item.get("status") == "pending"
    }
    remaining = still_pending & rejected_ids
    if remaining:
        return False, f"reject 后仍是待批准：{', '.join(sorted(remaining))}", len(items)
    return True, "", len(items)


# --------------------------------------------------------------- git/材料

def _git(args: list, timeout: int = _GIT_TIMEOUT) -> Tuple[Optional[str], Optional[str]]:
    """只读 git 调用；返回 (stdout, error)。"""
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None, "git 可执行不存在"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git 执行失败：{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, (detail[-1][:300] if detail else f"git 退出码 {proc.returncode}")
    return proc.stdout, None


def prepare_worktree(task: dict, execution_worktree: Path, review_root: Path
                     ) -> Optional[str]:
    """建/复用专属 Review worktree（独立于执行会话）。返回错误原因或 None。"""
    delivery = task.get("delivery") or (task.get("extra") or {}).get("delivery") or {}
    if delivery.get("mode") == "pr":
        try:
            if not delivery.get("head_sha") or str(execution_worktree) != delivery.get("worktree_path"):
                raise git_delivery.GitDeliveryError("worktree_invalid", "工作树或提交身份缺失")
            git_delivery.verify_branch_worktree(
                dict(delivery, repository=task.get("repository")),
                require_clean=True, expected_head=delivery["head_sha"])
            snapshot = git_delivery.prepare_snapshot_worktree(
                task["repository"], task["task_id"], sha=delivery["head_sha"],
                purpose="review", worktrees_root=review_root.parent.parent)
            if Path(snapshot) != review_root:
                return "review_blocked: Review 快照路径与登记不符"
            return None
        except git_delivery.GitDeliveryError:
            return "review_blocked: 执行工作树与已提交版本不一致"
    if review_root.exists():
        top, error = _git(["-C", str(review_root), "rev-parse", "--show-toplevel"])
        if error is not None:
            return f"review_worktree_invalid: 已有目录不可核验（{error}）"
        if Path((top or "").strip()).resolve() != review_root.resolve():
            return "review_worktree_invalid: 已有目录不是独立 worktree"
        return None
    review_root.parent.mkdir(parents=True, exist_ok=True)
    commit, error = _git(["-C", str(execution_worktree), "rev-parse", "HEAD"])
    if error is not None:
        return f"review_worktree_failed: 执行 worktree 不可读（{error}）"
    repo = Path(str(task.get("repository") or "")).expanduser()
    _out, error = _git([
        "-C", str(repo), "worktree", "add", "--detach",
        str(review_root), (commit or "HEAD").strip(),
    ], timeout=60)
    if error is not None:
        _out, error = _git([
            "-C", str(execution_worktree), "worktree", "add", "--detach",
            str(review_root), (commit or "HEAD").strip(),
        ], timeout=60)
    if error is not None:
        return f"review_worktree_failed: {error}"
    return None


def _write_text(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") or not text else text + "\n",
                    encoding="utf-8")


def collect_materials(*, db_path, conn, materials: Path, task: dict,
                      execution_worktree: Path, execution_run_id: str
                      ) -> dict:
    """只读生成审查材料（改动 diff/status + 计划 + 执行证据 + 修复来源报告）。"""
    materials.mkdir(parents=True, exist_ok=True)
    files = {}
    plan_path = Path(str(task.get("plan_path") or ""))
    if plan_path.is_file():
        target = materials / "plan.md"
        shutil.copyfile(plan_path, target)
        files["plan.md"] = str(target)

    status_out, status_error = _git(
        ["-C", str(execution_worktree), "status", "--porcelain"]
    )
    _write_text(
        materials / "changes.status",
        status_out if status_error is None else f"# git status 失败：{status_error}",
    )
    files["changes.status"] = str(materials / "changes.status")

    delivery = task.get("delivery") or (task.get("extra") or {}).get("delivery") or {}
    if delivery.get("mode") == "pr":
        base, head = delivery.get("base_sha"), delivery.get("head_sha")
        if not base or not head:
            raise util.KanpilotError("review_blocked", "交付任务缺少已提交版本身份")
        def material(name, args):
            output, error = _git(["-C", str(execution_worktree), *args])
            if error:
                raise util.KanpilotError("review_blocked", f"Review 材料读取失败：{error}")
            _write_text(materials / name, output or "")
            files[name] = str(materials / name)
            return output or ""
        def diff_material(name, left):
            output, error = _git(["-C", str(execution_worktree), "diff", "--no-color", left, head])
            if error:
                raise util.KanpilotError("review_blocked", f"Review diff 读取失败：{error}")
            if len((output or "").encode("utf-8")) > 400 * 1024:
                output = material(name + ".stat", ["diff", "--stat", left, head])
                output += "\nDiff 超过 400KB，请在只读工作树中查看具体文件。\n"
            _write_text(materials / name, output or "")
            files[name] = str(materials / name)
        diff_material("changes.diff", base)
        material("commits.log", ["log", "--oneline", f"{base}..{head}"])
        history = task.get("review_history") or (task.get("extra") or {}).get("review_history") or []
        if history:
            previous = history[-1]
            if previous.get("head_sha"):
                diff_material("fix-increment.diff", previous["head_sha"])
            previous_report = Path(str(previous.get("report_path") or previous.get("review_report_path") or ""))
            if previous_report.is_file():
                target = materials / "previous-review-report.md"
                shutil.copyfile(previous_report, target)
                files[target.name] = str(target)
    else:
        diff_out, diff_error = _git(
            ["-C", str(execution_worktree), "diff", "HEAD", "--no-color"])
        _write_text(materials / "changes.diff", diff_out if diff_error is None else f"# git diff 失败：{diff_error}")
        files["changes.diff"] = str(materials / "changes.diff")

    # 执行证据（本周期执行轮次的脱敏 job/events）。
    evidence_note = None
    try:
        run_dir = headless_adapter.resolve_source(conn).run_dir(
            task["task_id"], execution_run_id
        )
    except Exception as exc:  # HeadlessError/越界 → 记原因，不静默省略
        run_dir = None
        evidence_note = f"执行证据目录不可解析：{exc}"
    copied = 0
    for name in ("events.jsonl", "job.json"):
        source = (run_dir / name) if run_dir is not None else None
        if source is not None and source.is_file():
            target = materials / f"execution-{name}"
            shutil.copyfile(source, target)
            files[target.name] = str(target)
            copied += 1
    if evidence_note is None and copied == 0:
        # 证据缺失必须显式记原因（不静默省略、不据此判成功）。
        evidence_note = (
            f"执行证据缺失：{run_dir}（本周期执行轮次的 events.jsonl/job.json "
            "不可读）——交人工核对"
        )
    if evidence_note is not None:
        _write_text(materials / "execution-evidence.note", evidence_note)
        files["execution-evidence.note"] = str(
            materials / "execution-evidence.note"
        )

    # 修复周期：带上原评审报告（供核验修复是否覆盖缺口）。
    source_task_id = task.get("review_source_task_id")
    if isinstance(source_task_id, str) and source_task_id:
        try:
            source_task = core_call(
                db_path, "get-task", {"task_id": source_task_id}
            ).get("task") or {}
        except util.KanpilotError:
            source_task = {}
        source_report = Path(str(source_task.get("review_report_path") or ""))
        if source_report.is_file():
            target = materials / "source-review-report.md"
            shutil.copyfile(source_report, target)
            files["source-review-report.md"] = str(target)

    context = {
        "task_id": task.get("task_id"),
        "project_name": task.get("project_name"),
        "title": task.get("title"),
        "repository": task.get("repository"),
        "plan_path": task.get("plan_path"),
        "plan_sha256": task.get("plan_sha256"),
        "plan_kind": task.get("plan_kind"),
        "requirements_sha256": task.get("requirements_sha256"),
        "execution_run_id": execution_run_id,
        "execution_worktree": str(execution_worktree),
        "review_source_task_id": source_task_id,
        "review_key": task.get("review_key"),
        "materials": files,
    }
    _write_text(
        materials / "context.json",
        json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2),
    )
    files["context.json"] = str(materials / "context.json")
    return {"files": files, "context": context}

# --------------------------------------------------------------- 指令


def review_instruction(prompt: str, *, job: dict, review_root: Path,
                       materials: dict, facts: dict) -> str:
    """只要求审查并输出冻结 JSON 的 Plus 指令（不 SubmitPlan、不代批契约）。"""
    task = facts.get("task") or {}
    files = materials.get("files") or {}
    lines = [prompt.rstrip(), "", INSTRUCTION_MARKER, ""]
    lines.append(
        "- 本轮由 KaneoPilot runner 已领取并核对；你只做**只读**审查：不要调用任何"
        " kanpilot/CLI 命令、不要登记结论、不要修改仓库文件、不要提交、不要跑产品构建。"
    )
    lines.append("- 评审身份（本轮固定）：")
    lines.append(
        f"  - 任务 `{job.get('task_id')}`（{task.get('project_name') or ''}）"
    )
    lines.append(
        f"  - 评审请求 `request_id={job.get('request_id')}`、`run_id={job.get('run_id')}`"
    )
    lines.append(
        f"  - 本周期任务执行 `execution_run_id={job.get('execution_run_id')}`；"
        f"执行 worktree `{job.get('execution_worktree')}`"
    )
    lines.append(
        f"  - 计划 `{task.get('plan_path')}`（sha256 `{task.get('plan_sha256')}`）；"
        f"需求指纹 `{task.get('requirements_sha256')}`"
    )
    lines.append(
        f"  - 仓库 `{task.get('repository')}`：以执行 worktree 与下面的改动材料为准，"
        "**不得**只审主仓库。"
    )
    lines.append("- 审查材料（已只读复制到专属 Review worktree）：")
    for name in sorted(files):
        lines.append(f"  - `{files[name]}`")
    lines.append(f"- 项目根（专属 Review worktree）：`{review_root}`")
    lines.append(
        "- 最终答复必须**只是**一个 JSON 对象（可整体包一层 ```json 栅栏），键恰好为"
        " `conclusion`、`report_markdown`、`fix`："
    )
    lines.append(
        '  `{"conclusion":"pass|changes_requested|blocked",'
        '"report_markdown":"<中文评审报告：结论/证据/缺口/建议>",'
        '"fix":{"title":"...","description":"...","priority":"<优先级>"}|null}`'
    )
    lines.append(
        "  - `fix` 仅在 `changes_requested` 时给出，priority ∈ "
        f"{', '.join(constants.REVIEW_FIX_PRIORITIES)}；其余结论必须为 `null`。"
    )
    lines.append(
        "  - 除该 JSON 外不要输出别的解释文字；格式不符将被判为评审受阻（交人工）。"
    )
    lines.append(
        "- 禁止 SubmitPlan/SubmitGoal、禁止代批任何契约；若被要求生成计划或目标契约，"
        "不要生成，直接按上面的结构给出评审结论。"
    )
    return "\n".join(lines) + "\n"

# --------------------------------------------------------------- 回合执行


def _create_plan_session(client, block: dict, directory: Path
                         ) -> Tuple[Optional[str], Optional[str]]:
    """``mode="plan"`` + ``permissionMode="ask"`` 审查会话（§5.24.5）。"""
    try:
        created = client.session_create(
            str(directory), mode=constants.PI_DESKTOP_PLAN_MODE,
            provider_id=block.get("provider_id"),
            model_id=block.get("model_id"),
            thinking_level=block.get("thinking_level"),
            title=f"KaneoPilot Review · {directory.name}",
        )
    except util.KanpilotError as exc:
        return None, f"pi_session_create 失败：{exc.message}"
    session_id = None
    if isinstance(created, dict):
        session = created.get("session")
        if isinstance(session, dict):
            session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            session_id = created.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None, "pi_session_create 未返回 session id"
    try:
        client.session_configure(
            session_id, mode=constants.PI_DESKTOP_PLAN_MODE,
            permission_mode=constants.PI_DESKTOP_READONLY_PERMISSION_MODE,
        )
    except util.KanpilotError as exc:
        return session_id, f"pi_session_configure 失败：{exc.message}"
    return session_id, None


def _heartbeat(db_path, job: dict, state: dict) -> dict:
    """沿当前 request 租约续租；被拒（stale_run/not_found）→ 停止续租，不写失败。"""
    if state.get("stopped"):
        return state
    state["count"] = state.get("count", 0) + 1
    try:
        core_call(db_path, "heartbeat", {
            "request_id": job["request_id"], "run_id": job["run_id"],
        })
    except util.KanpilotError as exc:
        if exc.code in ("stale_run", "not_found"):
            state["stopped"] = f"{exc.code}: {exc.message}"
        else:
            state["error"] = exc.message
    return state


def _observe_turn(*, db_path, client, job: dict, log, timeout: float,
                  request: Optional[dict]) -> dict:
    """观察模型回合（含续租与权限等待）；权限等待/超时绝不判成功。"""
    session_id, turn_id = job["session_id"], job["turn_id"]
    deadline = time.monotonic() + timeout
    interval = _heartbeat_interval(request)
    next_heartbeat = time.monotonic() + interval
    state = {"count": 0, "stopped": None, "error": None}
    read_errors = 0
    permission_seen = False
    outcome, detail = None, None
    while True:
        status, error = pi_desktop_controller.turn_status(
            client, session_id, turn_id
        )
        if status == "completed":
            outcome = "completed"
            break
        if status in ("error", "aborted"):
            outcome = "failed"
            detail = f"turn {status}"
            break
        if status is None and error:
            read_errors += 1
            if read_errors == 1:
                log.emit("system", util.now_iso(),
                         content=f"turn 读取失败（重试只读）：{error}")
            if read_errors >= _MAX_READ_ERRORS:
                outcome = "unreadable"
                detail = f"turn_unreadable: {error}"
                break
        else:
            read_errors = 0
        view = pi_desktop_controller.agent_status(client, session_id)
        if view and view.get("pendingToolConfirmations") and not permission_seen:
            permission_seen = True
            job["permission_waiting"] = True
            log.emit(
                "system", util.now_iso(),
                content=(
                    "permission_waiting: Plus 内有待处理的工具权限确认"
                    "（不代批、不重发；等待不算成功）"
                ),
            )
        now = time.monotonic()
        if now >= next_heartbeat:
            next_heartbeat = now + interval
            _heartbeat(db_path, job, state)
        if now >= deadline:
            outcome = "timeout"
            break
        _sleep(_POLL_SECONDS)
    job["heartbeat"] = dict(state)
    return {"outcome": outcome, "detail": detail}


def _proposal_blocked_result(job: dict, count: int) -> dict:
    return {
        "conclusion": "blocked",
        "report_markdown": (
            "# 评审受阻：控制轮意外生成 proposal\n\n"
            f"- 任务：`{job.get('task_id')}`\n"
            f"- 评审请求：`{job.get('request_id')}` / `{job.get('run_id')}`\n"
            f"- 被拒绝关闭的 proposal 数：{count}\n"
            "- 处置：已按 §5.24.5 只读确认身份后以 "
            "`pi_plans_resolve(action=\"reject\", confirm=true)` 关闭；"
            "**不**按其 markdown 登记 pass，交人工复核本轮审查。\n"
        ),
        "fix": None,
    }


def _attach_round(*, db_path, client, job: dict, log, timeout: float,
                  request: Optional[dict]) -> dict:
    """观察既有 session/turn → 完整转录 → proposal 拒绝 → 严格解析结果。"""
    session_id, turn_id = job["session_id"], job["turn_id"]
    observed = _observe_turn(
        db_path=db_path, client=client, job=job, log=log, timeout=timeout,
        request=request,
    )
    outcome = observed["outcome"]
    if outcome == "timeout":
        stopped = pi_desktop_controller.stop_turn(client, session_id, turn_id)
        reason = (
            "permission_waiting" if job.get("permission_waiting") else "timeout"
        )
        return {
            "kind": "blocked",
            "detail": (
                f"{reason}（{timeout}s）；停止结果 terminal="
                f"{stopped.get('terminal')} aborted={stopped.get('aborted')} "
                f"unknown={stopped.get('unknown')}"
            ),
            "stop_result": stopped,
        }
    if outcome in ("failed", "unreadable"):
        return {
            "kind": "blocked",
            "detail": observed.get("detail") or f"turn {outcome}",
        }

    messages, complete, transcript_detail = pi_desktop_controller.read_transcript(
        client, session_id
    )
    job["transcript_complete"] = bool(complete)
    pi_desktop_controller.write_json(
        Path(job["invocation_dir"]) / "transcript.json",
        {
            "complete": bool(complete),
            "detail": transcript_detail,
            "messages": messages or [],
        },
    )

    rejected_ok, reject_detail, rejected_count = reject_proposals(
        client, session_id, turn_id
    )
    if rejected_count and not rejected_ok:
        return {
            "kind": "blocked",
            "detail": f"review_reject_failed: {reject_detail}",
            "proposal_count": rejected_count,
        }
    if rejected_count:
        job["proposal_rejected"] = {
            "count": rejected_count, "detail": reject_detail,
        }
        log.emit(
            "system", util.now_iso(),
            content=(
                f"意外生成 proposal：已拒绝关闭 {rejected_count} 条"
                "（不按其 markdown 登记 pass）→ review_blocked"
            ),
        )
        return {
            "kind": "settle",
            "result": _proposal_blocked_result(job, rejected_count),
            "rejected": rejected_count,
        }
    if not complete:
        return {
            "kind": "blocked",
            "detail": (
                f"transcript_incomplete: {transcript_detail or '转录不完整'}"
            ),
        }
    result, error = parse_result(messages)
    if result is None:
        return {"kind": "blocked", "detail": f"review_result_invalid: {error}"}
    return {"kind": "settle", "result": result}


def _start_model_round(*, client, block: dict, job: dict, job_path, log,
                       record_run, instruction: str) -> Optional[str]:
    """建 plan 会话 → 发送前落 send_state=unknown → 启动证明 → 登记 sent。"""
    # 会话项目根 = 专属 Review worktree（审查输入）；控制轮产物仍在 invocation 目录。
    project_root = Path(job.get("review_root") or job["invocation_dir"])
    session_id, detail = _create_plan_session(client, block, project_root)
    if session_id is None:
        return detail or "建会话失败"
    job["session_id"] = session_id
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    if detail is not None:
        return detail
    job["send_state"] = "unknown"
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    try:
        result = client.agent_prompt(session_id, instruction)
    except util.KanpilotError as exc:
        if exc.code == "validation_failed":
            job["send_state"] = "not_sent"
        return f"prompt_response_lost: {exc.message}（不重发）"
    turn_id = result.get("turnId") if isinstance(result, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        return "prompt_response_lost: 响应未含 turnId（不重发）"
    job["turn_id"] = turn_id
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    started, proof_detail = pi_desktop_controller.prove_started(
        client, session_id, turn_id
    )
    if not started:
        job["send_state"] = "unknown"
        return proof_detail or "startup_unproven"
    job["send_state"] = "sent"
    log.emit("session", util.now_iso(), session_id=session_id, turn_id=turn_id,
             backend="pi_desktop")
    log.emit("user", util.now_iso(), content=instruction[:2000])
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    return None

# --------------------------------------------------------------- 收尾动作


def _report(db_path, job: dict, *, outcome: str, note: str,
            failure_code: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """汇报并容忍响应丢失：读 ``get-request`` 核对，**不**重放非幂等调用。"""
    job["report_intent"] = outcome
    payload = {
        "request_id": job["request_id"], "run_id": job["run_id"],
        "outcome": outcome, "note": note,
    }
    if failure_code:
        payload["failure_code"] = failure_code
    try:
        core_call(db_path, "report", payload)
    except util.KanpilotError as exc:
        request = (_facts(db_path, job["task_id"], job["request_id"])
                   .get("request") or {})
        recorded = (request.get("result") or {}).get("outcome")
        if request.get("status") in _TERMINAL_REQUEST_STATUSES and (
            recorded == outcome
        ):
            job.setdefault("checkpoints", {})["reported"] = True
            return True, None
        return False, f"report 失败：{exc.message}"
    job.setdefault("checkpoints", {})["reported"] = True
    return True, None


def _close(*, job, job_path, log, record_run, status: str, exit_code: int,
           detail: Optional[str], reason: Optional[str] = None) -> None:
    job.update({
        "status": status, "exit_code": exit_code, "ended_at": util.now_iso(),
        "detail": detail,
    })
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    log.emit("terminal", util.now_iso(),
             outcome="completed" if status == "finished" else "failed",
             reason=reason or detail or status)


def _blocked_no_pass(*, db_path, job, job_path, log, record_run,
                     detail: str) -> int:
    """受阻且不得登记任何结论（不写 pass、不推进 done）：needs_user + review_blocked。"""
    log.emit("system", util.now_iso(), content=f"评审受阻：{detail}")
    ok, error = _report(
        db_path, job, outcome="needs_user", note=detail,
        failure_code="review_blocked",
    )
    if not ok:
        detail = f"{detail}；{error}"
    _close(job=job, job_path=job_path, log=log, record_run=record_run,
           status="failed", exit_code=1, detail=detail)
    print(f"executor review Plus 控制轮受阻：{detail}", file=sys.stderr)
    return 1


def _stop_no_write(*, job, job_path, log, record_run, detail: str) -> int:
    """租约失效/被新 run 接管：停止写入（不 report、不重放），交人工恢复。"""
    _close(job=job, job_path=job_path, log=log, record_run=record_run,
           status="failed", exit_code=1, detail=detail)
    print(f"executor review 收尾受阻：{detail}", file=sys.stderr)
    return 1


def _review_summary(task: dict) -> dict:
    summary = task.get("review_summary")
    return summary if isinstance(summary, dict) else {}


def _registered_matches(task, job):
    from .review_settle import _registered_matches as matches
    return matches(task, job)


_IDENTITY_BLOCK_CODES = ("stale_run", "conflict", "not_found",
                         "validation_failed")


def _registration_failed(*, db_path, job, job_path, log, record_run,
                         label: str, exc) -> int:
    """登记/推进被核心拒绝时的分流（§5.24.5）。

    身份或评审周期失配（``stale_run``/``conflict``/``not_found``/
    ``validation_failed``）→ **停止写入**：不 report、不覆盖新周期、不登记任何
    结论，交人工恢复；其它错误（传输/远端不可用等）→ 人工阻塞。
    """
    detail = f"{label} 失败：{exc.code}: {exc.message}"
    if exc.code in _IDENTITY_BLOCK_CODES:
        return _stop_no_write(
            job=job, job_path=job_path, log=log, record_run=record_run,
            detail=f"{detail}（登记身份/评审周期失配：停止写入，交人工恢复）",
        )
    return _blocked_no_pass(
        db_path=db_path, job=job, job_path=job_path, log=log,
        record_run=record_run, detail=detail,
    )


def _settle(**kwargs) -> int:
    from . import review_settle
    return review_settle.settle_job(driver=sys.modules[__name__], **kwargs)

def _request_failed(*, db_path, job, job_path, log, record_run, detail: str) -> int:
    """连接/会话读不到等传输级失败：``report failed``（不写 pass、保留证据）。"""
    log.emit("system", util.now_iso(), content=f"评审控制轮失败：{detail}")
    ok, error = _report(db_path, job, outcome="failed", note=detail)
    if not ok:
        detail = f"{detail}；{error}"
    _close(job=job, job_path=job_path, log=log, record_run=record_run,
           status="failed", exit_code=1, detail=detail)
    print(f"executor review Plus 控制轮失败：{detail}", file=sys.stderr)
    return 1


def _identity_problem(facts: dict, job: dict) -> Optional[str]:
    """claim 后身份核对：请求 run_id、任务本轮执行 worktree、计划哈希、关联来源。"""
    task, request = facts.get("task"), facts.get("request")
    if not isinstance(task, dict):
        return "核心无该任务（身份不符）"
    if not isinstance(request, dict):
        return "核心无该请求（身份不符）"
    if request.get("stage") != STAGE:
        return f"请求 stage={request.get('stage')} 不是 review"
    if request.get("run_id") != job.get("run_id"):
        return "请求 run_id 与本轮不符"
    if request.get("status") != "claimed":
        return f"请求状态 {request.get('status')} 不是 claimed"
    if task.get("status") != "in_review":
        return f"任务状态 {task.get('status')} 不在本周期 in_review"
    if task.get("review_report_path") or task.get("review_report_sha256"):
        return "本周期已有登记报告（不覆盖新周期）"
    execution_run_id = task.get("run_id")
    if not isinstance(execution_run_id, str) or not execution_run_id:
        return "任务没有本轮执行 run_id（不得只审主仓库）"
    round_view = task.get("execution_round")
    if not isinstance(round_view, dict):
        return "缺少本轮执行轮次索引（不得只审主仓库）"
    backend = round_view.get("backend")
    if not isinstance(backend, str) or not backend:
        return "执行轮次缺少 backend"
    worktree = round_view.get("worktree_path")
    if not isinstance(worktree, str) or not worktree or not Path(worktree).is_dir():
        return f"执行 worktree 不可用：{worktree}"
    plan_path = task.get("plan_path")
    if not isinstance(plan_path, str) or not Path(plan_path).is_file():
        return "计划文件缺失"
    registered = task.get("plan_sha256")
    if not isinstance(registered, str) or util.sha256_file(
        Path(plan_path)
    ) != registered:
        return "计划哈希与登记值不符（计划已变更）"
    return None


def _review_root(conn, task_id: str, run_id: str) -> Path:
    """专属 Review worktree 根：``<worktrees_root>/<task_id>/review-<run_id>``。"""
    root = constants.worktrees_root(
        db.get_setting(conn, "worktrees_root") or None
    )
    return root / task_id / f"review-{run_id}"


def run_stage(stage: str, *, conn, db_path, prompt: str, timeout: float,
              log_path: Path, record_run: Callable, resume: bool = False) -> int:
    """跑一个 Plus Review 控制轮；返回进程退出码（0=本轮已按契约收尾）。"""
    if stage != STAGE:
        print(f"unknown stage: {stage}", file=sys.stderr)
        return 2
    if resume:
        pending = _persisted_round(conn)
        if pending is None:
            print("executor review 没有可恢复的 Plus 控制轮", file=sys.stderr)
            return 1
        if pending["job"].get("backend") != "pi_desktop":
            print("executor review 历史后端不可恢复", file=sys.stderr)
            return 1
        block = pending["job"].get("config")
        return _resume_round(
            conn=conn, db_path=db_path, block=block, prompt=prompt,
            timeout=timeout, log_path=log_path, record_run=record_run,
        )

    backend, block = pi_desktop_controller.review_round_config(conn)
    if backend != "pi_desktop":
        print(
            f"executor review 控制端后端不是 pi_desktop（{backend}）："
            "不在此分支运行",
            file=sys.stderr,
        )
        return 2
    error = pi_desktop_controller.validate_block(block)
    if error is not None:
        print(f"pi_desktop_controller_unavailable：{error}", file=sys.stderr)
        return 1
    pending = _persisted_round(conn)
    if pending is not None and pending["job"].get("status") != "finished":
        request = _request_state(db_path, pending["job"].get("request_id"))
        if (request and request.get("status") == "claimed"
                and _lease_valid(request)):
            print(
                "executor review 已有未收尾的 Plus 评审控制轮"
                f"（invocation={pending['job'].get('invocation_id')} "
                f"request={request.get('request_id')} "
                f"checkpoints={pending['job'].get('checkpoints')}）："
                "不重复启动；请走只读恢复（--resume）",
                file=sys.stderr,
            )
            return 1
    return _fresh_round(
        conn=conn, db_path=db_path, block=block, prompt=prompt,
        timeout=timeout, log_path=log_path, record_run=record_run,
    )


def _new_job(block: dict, invocation_id: str, directory: Path,
             log_path: Path) -> dict:
    job = pi_desktop_controller.base_job(STAGE, invocation_id, block)
    job.update({
        "log": str(log_path),
        "invocation_dir": str(directory),
        "task_id": None,
        "request_id": None,
        "run_id": None,
        "execution_run_id": None,
        "execution_worktree": None,
        "review_root": None,
        "materials": {},
        "report_path": None,
        "report_sha256": None,
        "review_key": None,
        "report_markdown": None,
        "conclusion": None,
        "fix": None,
        "fix_task_id": None,
        "fix_task_source_sha256": None,
        "checkpoints": {},
        "report_intent": None,
    })
    return job


def _fresh_round(*, conn, db_path, block: dict, prompt: str, timeout: float,
                 log_path: Path, record_run: Callable) -> int:
    invocation_id = util.new_uuid()
    directory = pi_desktop_controller.invocation_dir(STAGE, invocation_id)
    job_path = directory / "job.json"
    log = pi_desktop_controller.EventLog(directory / "events.jsonl")
    job = _new_job(block, invocation_id, directory, log_path)
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)

    claim = core_call(db_path, "claim", {"stage": STAGE, "executor": EXECUTOR_ID})
    if not claim.get("claimed"):
        reason = claim.get("reason")
        log.emit("system", util.now_iso(),
                 content=f"无请求：{reason}（不反复 claim）")
        _close(job=job, job_path=job_path, log=log, record_run=record_run,
               status="finished", exit_code=0, detail=f"无请求：{reason}",
               reason="no_request")
        return 0
    job["request_id"] = claim.get("request_id")
    job["run_id"] = claim.get("run_id")
    context = claim.get("context") or {}
    job["task_id"] = context.get("task_id") or (
        context.get("task") or {}
    ).get("task_id")
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    log.emit("system", util.now_iso(),
             content=f"已领取 review 请求 {job['request_id']}/{job['run_id']}")

    if not isinstance(job["task_id"], str) or not job["task_id"]:
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run, detail="review 请求缺少 task_id（身份不符）",
        )

    facts = _facts(db_path, job["task_id"], job["request_id"])
    problem = _identity_problem(facts, job)
    if problem is not None:
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run, detail=f"review 身份核对失败：{problem}",
        )
    task = facts["task"]
    job["execution_run_id"] = task.get("run_id")
    execution_worktree = Path(
        str((task.get("execution_round") or {}).get("worktree_path"))
    )
    job["execution_worktree"] = str(execution_worktree)
    delivery = task.get("delivery") or (task.get("extra") or {}).get("delivery") or {}
    suffix = str(delivery.get("head_sha") or "")[:12] if delivery.get("mode") == "pr" else job["run_id"]
    review_root = _review_root(conn, job["task_id"], suffix)
    problem = prepare_worktree(task, execution_worktree, review_root)
    if problem is not None:
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run, detail=problem,
        )
    # Keep generated inputs outside the committed snapshot so it stays clean/reusable.
    try:
        materials = collect_materials(
            db_path=db_path, conn=conn,
            materials=directory / "materials" if delivery.get("mode") == "pr" else review_root / "materials",
            task=task, execution_worktree=execution_worktree,
            execution_run_id=job["execution_run_id"],
        )
    except util.KanpilotError as exc:
        return _blocked_no_pass(db_path=db_path, job=job, job_path=job_path,
                                log=log, record_run=record_run, detail=exc.message)
    job["review_root"] = str(review_root)
    job["materials"] = materials["files"]
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    log.emit("system", util.now_iso(),
             content=f"专属 Review worktree：{review_root}")

    instruction = review_instruction(
        prompt, job=job, review_root=review_root, materials=materials,
        facts=facts,
    )
    try:
        client = pi_desktop_controller._open_client(
            block, min(timeout, 30), role="review",
            invocation_id=invocation_id, project_path=review_root,
            request_id=job.get("request_id"), run_id=job.get("run_id"),
            task_id=job.get("task_id"),
            required_tools=constants.PI_DESKTOP_PLAN_TOOLS,
            lease_seconds=timeout,
        )
        job["scope_id"] = client.scope_id
        pi_desktop_controller.write_json(job_path, job)
        pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    except util.KanpilotError as exc:
        return _request_failed(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run,
            detail=f"pi_desktop_controller_unavailable: {exc.message}",
        )
    try:
        problem = _start_model_round(
            client=client, block=block, job=job, job_path=job_path, log=log,
            record_run=record_run, instruction=instruction,
        )
    finally:
        if job.get("send_state") in ("unknown", "sent"):
            try:
                core_call(db_path, "record-cost", {
                    "task_id": job["task_id"], "request_id": job["request_id"],
                    "run_id": job["run_id"], "stage": STAGE, "source": "plus", "cost_usd": None,
                })
            except util.KanpilotError as exc:
                if exc.code not in ("stale_run", "conflict", "not_found"):
                    raise
    if problem is not None:
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run, detail=f"review 控制轮未发成立：{problem}",
        )
    return _finish_round(
        db_path=db_path, client=client, job=job, job_path=job_path, log=log,
        record_run=record_run, timeout=timeout, request=facts.get("request"),
    )


def _finish_round(*, db_path, client, job, job_path, log, record_run,
                  timeout: float, request: Optional[dict]) -> int:
    """观察 + 解析 + 登记（fresh 与恢复共用的后半段）。"""
    attached = _attach_round(
        db_path=db_path, client=client, job=job, log=log, timeout=timeout,
        request=request,
    )
    kind = attached.get("kind")
    if kind == "blocked":
        pi_desktop_controller.write_json(job_path, job)
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run, detail=attached.get("detail") or "评审受阻",
        )
    result = attached.get("result") or {}
    job["conclusion"] = result.get("conclusion")
    job["report_markdown"] = result.get("report_markdown")
    job["fix"] = result.get("fix")
    pi_desktop_controller.write_json(job_path, job)
    log.emit("system", util.now_iso(),
             content=f"评审结论：{job['conclusion']}")
    return _settle(
        db_path=db_path, job=job, job_path=job_path, log=log,
        record_run=record_run,
    )


def _resume_round(*, conn, db_path, block: dict, prompt: str, timeout: float,
                  log_path: Path, record_run: Callable) -> int:
    """只读恢复：不重复 claim/建会话/重发 prompt，按检查点收尾。"""
    pending = _persisted_round(conn)
    if pending is None:
        print("executor review 没有可恢复的 Plus 控制轮", file=sys.stderr)
        return 1
    job = pending["job"]
    if job.get("status") == "finished":
        print("executor review 控制轮已收尾：无需恢复", file=sys.stderr)
        return 0
    directory, job_path = pending["directory"], pending["job_path"]
    job["log"] = str(log_path)
    job["resumed_at"] = util.now_iso()
    job["resume"] = True
    log = pi_desktop_controller.EventLog(directory / "events.jsonl")
    log.emit("system", util.now_iso(),
             content="只读恢复：不重复 claim/建会话/重发 prompt")

    if not job.get("conclusion"):
        session_id, turn_id = job.get("session_id"), job.get("turn_id")
        if not isinstance(session_id, str) or not session_id or not isinstance(
            turn_id, str
        ) or not turn_id:
            return _stop_no_write(
                job=job, job_path=job_path, log=log, record_run=record_run,
                detail=(
                    f"send_state={job.get('send_state')} 且无已知 session/turn："
                    "不自动重发，保留人工阻塞"
                ),
            )
        facts = _facts(db_path, job["task_id"], job["request_id"])
        request = facts.get("request")
        if not isinstance(facts.get("task"), dict) or not isinstance(
            request, dict
        ):
            return _stop_no_write(
                job=job, job_path=job_path, log=log, record_run=record_run,
                detail="恢复事实核对失败：核心已无该任务/请求",
            )
        if request.get("status") in _TERMINAL_REQUEST_STATUSES:
            return _settle(
                db_path=db_path, job=job, job_path=job_path, log=log,
                record_run=record_run,
            )
        if not _lease_valid(request):
            return _stop_no_write(
                job=job, job_path=job_path, log=log, record_run=record_run,
                detail="lease_expired: 租约失效，停止写入，交人工恢复",
            )
        round_block = job.get("config")
        if not isinstance(round_block, dict) or pi_desktop_controller.validate_block(round_block) is not None:
            return _request_failed(
                db_path=db_path, job=job, job_path=job_path, log=log,
                record_run=record_run,
                detail="resume_config_unavailable: 持久五字段配置缺失或无效；不读取当前配置替代",
            )
        try:
            if job.get("scope_id"):
                from . import runtime_client
                expected_scope = runtime_client.stable_scope_id(
                    "review", invocation_id=job.get("invocation_id"),
                    task_id=job.get("task_id"), run_id=job.get("run_id"),
                )
                if expected_scope != job["scope_id"]:
                    raise util.KanpilotError("conflict", "job scope_id 与 Review 身份不匹配")
                client = pi_desktop_controller._open_client(
                    round_block, min(timeout, 30), role="review",
                    invocation_id=str(job.get("invocation_id") or ""),
                    project_path=Path(job.get("review_root") or directory),
                    request_id=job.get("request_id"), run_id=job.get("run_id"),
                    task_id=job.get("task_id"),
                    required_tools=constants.PI_DESKTOP_PLAN_TOOLS,
                    lease_seconds=timeout, read_only=True,
                )
            else:
                from . import runtime_client
                client = runtime_client.HistoricalReadClient(
                    round_block["connection_file"], timeout=min(timeout, 30))
                client.connect(required_tools=constants.PI_DESKTOP_PLAN_TOOLS)
            scope_id = getattr(client, "scope_id", None)
            if scope_id:
                job["scope_id"] = scope_id
                pi_desktop_controller.write_json(job_path, job)
                pi_desktop_controller.record_run_entry(record_run, STAGE, job)
        except util.KanpilotError as exc:
            return _request_failed(
                db_path=db_path, job=job, job_path=job_path, log=log,
                record_run=record_run,
                detail=f"pi_desktop_controller_unavailable: {exc.message}",
            )
        started, proof_detail = pi_desktop_controller.prove_started(
            client, session_id, turn_id
        )
        if not started:
            return _request_failed(
                db_path=db_path, job=job, job_path=job_path, log=log,
                record_run=record_run,
                detail=proof_detail or "startup_unproven",
            )
        if job.get("send_state") != "sent":
            job["send_state"] = "sent"
        return _finish_round(
            db_path=db_path, client=client, job=job, job_path=job_path,
            log=log, record_run=record_run, timeout=timeout, request=request,
        )
    return _settle(
        db_path=db_path, job=job, job_path=job_path, log=log,
        record_run=record_run,
    )


def main(argv=None) -> int:  # pragma: no cover - 由 executor_runner 调用
    raise SystemExit(
        "kanpilot.pi_desktop_review 由 kanpilot.executor_runner 调用"
        "（--stage review, review_config.backend=pi_desktop）"
    )
