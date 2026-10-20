"""PI-Desktop-Plus Review 控制端（protocol.md §5.24.5，v4 T6.3）。

``executor_runner --stage review`` 在 ``review_config.backend == "pi_desktop"``
时改走本模块。与 dispatch/monitor 控制轮不同，Review 的登记与推进**由 runner
自己**完成（模型只产出版本化评审文本，兼容历史 JSON）：

1. ``claim(stage=review)``（核心固定 JSON 命令）→ 以 ``get-task``/``get-request``
   核对请求 ``run_id``、任务本轮执行 ``run_id`` 与 worktree、计划哈希、执行证据
   与关联来源（**不得**只审主仓库）。
2. 建**专属 Review worktree**（不复用仍活动的执行会话），用只读 ``git status``/
   ``git diff`` 生成改动材料，并把计划/（修复周期时的）原评审报告/执行证据复制
   进该 worktree 的材料目录——它是本轮的审查输入。
3. ``pi_session_create(mode="plan", projectPath=<review worktree>)`` +
   ``permissionMode=<本轮快照，新轮次 auto>``；指令只要求审查并输出冻结结果，不 SubmitPlan、
   不 SubmitGoal、不代批任何契约。
4. ``completed`` + 完整转录后**严格解析** ``{conclusion, report_markdown, fix}``
   （``priority`` ∈ ``constants.REVIEW_FIX_PRIORITIES``）。意外 proposal 只
   ``reject`` 关闭并记 ``review_blocked``，**不**按其 markdown 登记 pass。
   格式错误/转录不完整/身份不符/权限等待/超时 → 绝不写 pass、不推进 done。
5. 报告写 ``<APP_SUPPORT>/reports/<task_id>/<run_id>.md``（核心重算哈希）；
   三结论动作：
   - ``pass``：``register-review(pass)``；自动管理任务等待既有交付流程，
     手动任务按核心交付门控 ``advance-task(done, in-review)``；最后 ``report succeeded``；
   - ``changes_requested``：``register-review(changes_requested,
     fix_mode=same_task, fix, review_key)`` → ``report succeeded``。
     不创建 Kaneo 子任务、不生成新计划；核心在原 Review 结算后按自动策略排队
     同任务独立修复会话，手动任务保留 held 等待用户确认影响与费用；
   - ``blocked``：``register-review(blocked)`` → ``report needs_user``。
6. 登记身份：``register-review``/``advance-task`` 都传
   ``request_id`` + ``run_id`` + ``expected_execution_run_id``（核心事务内核对）；
   ``review_key = sha256(规范 JSON(源 task_id, 本周期执行 run_id, 报告 sha))``。
7. 部分成功恢复：job 检查点 + ``get-task``/``get-request`` 事实核对；登记一致
   则跳过 ``register``，``pass`` 已 done 则跳过 ``advance``；同身份修复材料已
   登记时不重复启动修复会话；``report`` 响应丢失只读核对，**不**重放非幂等调用。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import (
    constants, core, db, headless_adapter, pi_desktop_controller,
    util, git_delivery, review_settle, review_snapshot, plus_interaction, task_workspace, worktree_pool,
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
REVIEW_TEXT_MARKERS = (
    "<<<KANPILOT_REVIEW_V1>>>", "<<<REPORT>>>", "<<<FIX_TITLE>>>",
    "<<<FIX_DESCRIPTION>>>", "<<<END_KANPILOT_REVIEW_V1>>>",
)


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


def _text_result(text: str, *, normalizations: Optional[list] = None
                 ) -> Tuple[Optional[dict], Optional[str]]:
    """Read one exact frame; only unstructured outer narration is removable."""
    trimmed = text.strip()
    lines = trimmed.splitlines(keepends=True)
    plain = [line.rstrip("\r\n") for line in lines]
    markers = [(i, line) for i, line in enumerate(plain) if line in REVIEW_TEXT_MARKERS]
    if ([line for _i, line in markers] != list(REVIEW_TEXT_MARKERS)
            or markers[1][0] != markers[0][0] + 3):
        return None, "review_text_v1 标记缺失、重复、乱序或混入额外文字"
    start, end = markers[0][0], markers[-1][0]
    before, after = lines[:start], lines[end + 1:]
    # One fence immediately enclosing the frame is transport decoration. Do
    # not unwrap arbitrary code blocks or search their contents for a result.
    if (before and after and before[-1].rstrip("\r\n") in ("```", "```text")
            and after[0].rstrip("\r\n") == "```"):
        before, after = before[:-1], after[1:]
    outside = "".join(before + after).strip()
    if (any(token in outside for token in ("{", "}", "[", "]", "```", "<<<", ">>>"))
            or re.search(r'''\b(?:conclusion|priority)\b["'`*_]*\s*[:=]''',
                         outside, re.IGNORECASE)):
        return None, "review_text_v1 外侧混入结构化结果或损坏标记"
    conclusion = re.fullmatch(r"conclusion: (pass|changes_requested|blocked)", plain[start + 1])
    priority = re.fullmatch(r"priority: (no-priority|low|medium|high|urgent)", plain[start + 2])
    if conclusion is None or priority is None:
        return None, "review_text_v1 conclusion/priority 缺失或非法"

    def section(start, end):
        content = "".join(lines[markers[start][0] + 1:markers[end][0]])
        # Remove just the separator newline before the next marker. Interior
        # quotes, literal backslashes, code fences and blank lines stay intact.
        return content.removesuffix("\r\n") if content.endswith("\r\n") else content.removesuffix("\n")

    report, title, description = section(1, 2), section(2, 3), section(3, 4)
    verdict, level = conclusion[1], priority[1]
    if verdict != "changes_requested" and (title.strip() or description.strip() or level != "no-priority"):
        return None, "review_text_v1 非 changes_requested 的修复段必须为空且 priority=no-priority"
    if outside and normalizations is not None:
        normalizations.append("removed_text_frame_commentary")
    return {"conclusion": verdict, "report_markdown": report,
            "fix": {"title": title, "description": description, "priority": level}
            if verdict == "changes_requested" else None}, None


def parse_result(messages: list, *, normalizations: Optional[list] = None,
                 metadata: Optional[dict] = None
                 ) -> Tuple[Optional[dict], Optional[str]]:
    """Final text frame / historical JSON → shared strict Review result."""
    text = pi_desktop_controller.last_assistant_text(messages)
    applied = []
    result_format = "json"
    value, error = pi_desktop_controller.strict_json_object(text)
    if (error is not None and isinstance(text, str)
            and (any(marker in text for marker in REVIEW_TEXT_MARKERS)
                 or "<<<KANPILOT_REVIEW_" in text
                 or "<<<END_KANPILOT_REVIEW_" in text)):
        value, error = _text_result(text, normalizations=applied)
        if error is not None:
            return None, error
        result_format = "review_text_v1"
    if error is not None and isinstance(text, str):
        # Never search for arbitrary braces or an older assistant answer. Only
        # one explicitly delimited object in the final answer is eligible.
        fences = list(re.finditer(r"^```[^\r\n]*", text, re.MULTILINE))
        block = re.search(
            r"^```(?:json)?[ \t]*\r?\n(.*?)^```[ \t]*(?=\r?$)",
            text, re.MULTILINE | re.DOTALL,
        )
        if block is not None and len(fences) == 2:
            outside = text[:block.start()] + text[block.end():]
            if not any(marker in outside for marker in ("```", "{", "}", "[", "]")):
                value, error = pi_desktop_controller.strict_json_object(block.group(1))
                if error is None:
                    applied.append("removed_fence_commentary")
    if error is not None:
        return None, error
    fix = value.get("fix")
    if (value.get("conclusion") == "changes_requested"
            and isinstance(fix, dict) and set(fix) == {"title", "description"}):
        value = dict(value, fix=dict(fix, priority="no-priority"))
        applied.append("missing_fix_priority=no-priority")
    result, error = validate_result(value)
    if error is None:
        if normalizations is not None:
            normalizations.extend(applied)
        if metadata is not None:
            metadata["result_format"] = result_format
    return result, error


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


def prepare_worktree(task: dict, execution_worktree: Path, review_root: Path, snapshot=None
                     ) -> Optional[str]:
    """建/复用专属 Review worktree（独立于执行会话）。返回错误原因或 None。"""
    delivery = core.delivery_of(task)
    if core.requires_branch_review(task):
        try:
            if not delivery.get("head_sha") or str(execution_worktree) != delivery.get("worktree_path"):
                raise git_delivery.GitDeliveryError("worktree_invalid", "工作树或提交身份缺失")
            core.require_committed_review(task)
            snapshot = git_delivery.prepare_snapshot_worktree(
                task["repository"], task["task_id"], sha=delivery["head_sha"],
                purpose="review", worktrees_root=review_root.parent.parent)
            if Path(snapshot) != review_root:
                return "review_blocked: Review 快照路径与登记不符"
            return None
        except (git_delivery.GitDeliveryError, util.KanpilotError, OSError):
            return "review_blocked: 执行工作树与已提交版本不一致"
    try:
        if execution_worktree.resolve() == review_root.resolve():
            raise util.KanpilotError("review_blocked", "Review 不得复用执行工作树")
        snapshot = snapshot if snapshot is not None else review_snapshot.freeze(execution_worktree)
        review_snapshot.verify_source(execution_worktree, snapshot)
        if review_root.exists():
            review_snapshot.verify_root(review_root, snapshot)
            review_snapshot.verify_source(execution_worktree, snapshot)
            return None
    except util.KanpilotError as exc:
        return f"review_worktree_invalid: {exc.message}"
    return "review_worktree_failed: 新评审快照必须经核心槽位租约创建"


def prepare_pooled_worktree(conn, db_path, job, task, execution_worktree, snapshot=None):
    """Freeze one leased detached checkout independently from the execution tree."""
    if core.requires_branch_review(task):
        core.require_committed_review(task)
        sha = core.delivery_of(task)["head_sha"]
    else:
        snapshot = snapshot if snapshot is not None else review_snapshot.freeze(execution_worktree)
        review_snapshot.verify_source(execution_worktree, snapshot)
        sha = snapshot["head"]
    lease = task_workspace.acquire_pool(conn, task["repository"], stage=STAGE,
                                        run_id=job["run_id"], request_id=job["request_id"], sha=sha)
    job.update(pool_lease=lease, pool_db_path=str(db_path), review_root=lease["path"], send_state="not_sent")
    if job.get("invocation_dir"):
        pi_desktop_controller.write_json(Path(job["invocation_dir"]) / "job.json", job)
    root = Path(lease["path"])
    util.require(root.resolve() != execution_worktree.resolve(), "Review 不得复用执行工作树", "review_blocked")
    if snapshot is not None:
        worktree_pool.register_snapshot(conn, **task_workspace.pool_identity(lease), snapshot=snapshot)
        review_snapshot.verify_source(execution_worktree, snapshot)
    return root


def validate_job_pool(job):
    if job.get("pool_lease"):
        conn = db.connect(job["pool_db_path"])
        try:
            task_workspace.validate_pool(conn, job["pool_lease"])
        finally:
            conn.close()


def _write_text(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") or not text else text + "\n",
                    encoding="utf-8")


def collect_materials(*, db_path, conn, materials: Path, task: dict,
                      execution_worktree: Path, execution_run_id: str, snapshot=None
                      ) -> dict:
    try:
        return _collect_materials(db_path=db_path, conn=conn, materials=materials, task=task,
                                  execution_worktree=execution_worktree,
                                  execution_run_id=execution_run_id, snapshot=snapshot)
    except OSError as exc:
        raise util.KanpilotError("review_blocked", f"Review 材料不可写入或读取：{exc}") from exc


def _collect_core_verification(conn, task, execution_run_id, materials):
    """Keep retired local checks as history; Plus events are current evidence."""
    if conn is None:
        return {}
    current = db.require_task(conn, task["task_id"])
    record = db.task_extra(current).get("verification_checks") or {}
    if (record.get("binding") or {}).get("execution_run_id") != execution_run_id:
        return {}
    # Do not re-run, validate or promote a historical local result over the
    # current execution's Plus evidence. Missing old output is not a blocker.
    target = materials / "historical-core-verification.json"
    _write_text(target, json.dumps({
        "source": "historical_pilot_verification",
        "operation_id": record.get("operation_id"),
        "state": record.get("state"),
        "verification_ok": (record.get("receipt") or {}).get("verification_ok"),
        "note": "历史 Pilot 本机验证记录，仅供审计；不代表当前 Plus 验证结果，"
                "不作为本轮完成或 Review 通过依据。当前验证以 execution-events.jsonl 的 Plus 工具结果为准。",
    }, ensure_ascii=False, sort_keys=True, indent=2))
    return {target.name: str(target)}


def _collect_materials(*, db_path, conn, materials: Path, task: dict,
                       execution_worktree: Path, execution_run_id: str, snapshot=None
                       ) -> dict:
    """只读生成审查材料（改动 diff/status + 计划 + 执行证据 + 修复来源报告）。"""
    materials.mkdir(parents=True, exist_ok=True)
    files = {}
    from . import review_resolution
    files.update(review_resolution.collect_evidence(conn, task, execution_run_id, materials))
    files.update(review_resolution.collect_remediation(task, materials))
    files.update(_collect_core_verification(conn, task, execution_run_id, materials))
    plan_path = Path(str(task.get("plan_path") or ""))
    if plan_path.is_file():
        target = materials / "plan.md"
        shutil.copyfile(plan_path, target)
        files["plan.md"] = str(target)

    delivery = core.delivery_of(task)
    if core.requires_branch_review(task):
        core.require_committed_review(task, execution_run_id)
        status_out, status_error = _git(["-C", str(execution_worktree), "status", "--porcelain"])
        _write_text(materials / "changes.status", status_out if status_error is None else f"# git status 失败：{status_error}")
        files["changes.status"] = str(materials / "changes.status")
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
        history = task.get("review_history") or (task.get("extra") or {}).get("review_history") or (
            db.task_extra(task).get("review_history") if "extra_json" in task else [])
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
        snapshot = snapshot if snapshot is not None else review_snapshot.freeze(execution_worktree)
        review_snapshot.verify_source(execution_worktree, snapshot)
        files.update(review_snapshot.write_materials(materials, snapshot))

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
    if core.requires_branch_review(task):
        context["review_branch_snapshot"] = core.branch_review_snapshot(task)
    _write_text(
        materials / "context.json",
        json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2),
    )
    files["context.json"] = str(materials / "context.json")
    if snapshot is not None:
        review_snapshot.verify_source(execution_worktree, snapshot)
    elif core.requires_branch_review(task):
        core.require_committed_review(task, execution_run_id)
    return {"files": files, "context": context}

# --------------------------------------------------------------- 指令


def review_instruction(prompt: str, *, job: dict, review_root: Path,
                       materials: dict, facts: dict) -> str:
    """Read-only Review with a raw Markdown result frame; no proposal approval."""
    task = facts.get("task") or {}
    files = materials.get("files") or {}
    lines = [prompt.rstrip(), "", INSTRUCTION_MARKER, ""]
    lines.append(
        "- 本轮由 KaneoPilot runner 已领取并核对；你只做**只读**审查：不要调用任何"
        " kanpilot/CLI 命令、不要登记结论、不要修改仓库文件、不要提交、不要跑产品构建。"
    )
    lines.append(
        "- 本轮**无人值守**：禁止调用 `asktool` 或任何在 Pi 界面等待用户作答的交互工具"
        "（Pi 计划模式「用 asktool 解决未定选择」的指引本轮不适用），这类调用只会挂起到超时；"
        "需要用户决定或补充材料时，按下方结构给出 `blocked` 结论并在报告中列出。"
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
    if core.requires_branch_review(task):
        delivery = core.delivery_of(task)
        lines.append(f"  - 指定任务分支 `{delivery.get('branch')}`；基线分支 `{delivery.get('base_branch')}`。")
        lines.append(f"  - 固定完整评审范围 `{delivery.get('base_sha')}..{delivery.get('head_sha')}`；"
                     f"在专属 Review worktree `{review_root}` 审查该提交版本，不跟随分支后续变化。")
        lines.append("  - 修复轮仍须审查基线至当前固定提交的全部功能改动；fix-increment.diff 仅用于核对修复，不能替代完整范围。")
    lines.append("- 审查材料（已冻结到本轮独立材料目录；与下方专属 Review worktree 一致）：")
    for name in sorted(files):
        lines.append(f"  - `{files[name]}`")
    lines.append(f"- 项目根（专属 Review worktree）：`{review_root}`")
    lines.append("- 最终答复使用 review_text_v1 分段文本，**不要输出 JSON**，不要手工转义报告中的引号、反斜杠或换行。按以下顺序输出，标记各独占一行、不得省略或重复：")
    lines.extend([
        "<<<KANPILOT_REVIEW_V1>>>", "conclusion: changes_requested", "priority: medium",
        "<<<REPORT>>>", "<中文评审报告：结论/证据/缺口/建议，直接写 Markdown 原文>",
        "<<<FIX_TITLE>>>", "<修复任务标题>",
        "<<<FIX_DESCRIPTION>>>", "<完整可执行修复步骤与测试/原生验收缺口，直接写原文>",
        "<<<END_KANPILOT_REVIEW_V1>>>",
    ])
    lines.append("  - 上例的 conclusion 必须替换为本轮实际结论 pass、changes_requested 或 blocked；priority 必须是 " + ", ".join(constants.REVIEW_FIX_PRIORITIES) + " 之一，未指定时写 no-priority。")
    lines.append("  - changes_requested 的报告、标题、修复说明都必须非空；pass/blocked 时 priority 必须为 no-priority，保留 FIX_TITLE/FIX_DESCRIPTION 标记但两段正文留空（不要写 null、无、占位符）。")
    lines.append("  - 标记行是协议保留字，正文不得重复这些独占标记行；正文里的 Markdown 代码块、双引号、反斜杠按原样书写。")
    lines.append("  - 已定位确定代码缺陷时必须使用 changes_requested，fix.description 包含可执行修复步骤及测试/原生验收缺口；不得仅因验收不足改成 blocked/fix=null。")
    lines.append("  - 无确定代码缺陷但材料、环境或用户选择不足导致无法完成评审时使用 blocked；报告必须列具体原因、需补充材料或决定、可执行下一步与解除阻塞标准。用户补充证据需独立核验，不得当作自动验证或直接判 pass。")
    if "original-plan.md" in files:
        lines.append("  - 本轮包含原冻结计划 original-plan.md、原阻塞报告 blocked-review.md 和新修复计划 plan.md；三者共同核验。新计划遗漏原功能或原验收要求不能当作已解除阻塞，修复轮仍需核验原范围与验收。")
    lines.append(
        "  - 整个答复只包含上述一份分段结果，开始和结束标记外不要添加开场白、总结或解释文字。"
    )
    lines.append(
        "- 禁止 SubmitPlan/SubmitGoal、禁止代批任何契约；若被要求生成计划或目标契约，"
        "不要生成，直接按上面的结构给出评审结论。"
    )
    return "\n".join(lines) + "\n"

# --------------------------------------------------------------- 回合执行


def _create_plan_session(client, block: dict, directory: Path
                         ) -> Tuple[Optional[str], Optional[str]]:
    """按本轮快照配置 plan 审查会话，新轮次 auto、历史 ask 保留。"""
    try:
        created = client.session_create(
            str(directory), mode=constants.PI_DESKTOP_PLAN_MODE,
            provider_id=block.get("provider_id"),
            model_id=block.get("model_id"),
            thinking_level=block.get("thinking_level"),
            title=f"KaneoPilot Review · {directory.name}", interaction_mode="unattended",
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
        plus_interaction.require_unattended(created, session_id)
        configured = client.session_configure(
            session_id, mode=constants.PI_DESKTOP_PLAN_MODE,
            permission_mode=block["permission_mode"], interaction_mode="unattended",
        )
        plus_interaction.require_unattended(configured, session_id)
        plus_interaction.verify_session(client, session_id)
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
        if job.get("pool_lease"):
            conn = db.connect(db_path)
            try:
                worktree_pool.heartbeat(conn, **task_workspace.pool_identity(job["pool_lease"]))
            finally:
                conn.close()
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
        try:
            record = client.turn_get(session_id, turn_id, require_identity=True)
            turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
            if turn.get("sessionId") != session_id or turn.get("turnId") != turn_id:
                raise util.KanpilotError("conflict", "Plus 返回的轮次身份不符")
            status, error = turn.get("status"), None
            if status in ("completed", "error", "aborted"):
                job["owner_stopped"] = True
            if (turn.get("errorCode") or turn.get("error_code")) == "INTERACTION_REQUIRED":
                job["interaction_required"] = record
                pi_desktop_controller.write_json(Path(job["invocation_dir"]) / "interaction-required.json", record)
                return {"outcome": "interaction_required", "record": record}
        except util.KanpilotError as exc:
            status, error = None, exc.message
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
    if outcome == "interaction_required":
        try:
            details = plus_interaction.required_interaction(observed["record"])
        except util.KanpilotError as exc:
            return {"kind": "blocked", "detail": "review_interaction_invalid: " + exc.message}
        # Collect available evidence, but the structured stop has priority over
        # missing/final assistant text and proposals. No proposal resolve call.
        messages, complete, detail = pi_desktop_controller.read_transcript(client, session_id)
        job["transcript_complete"] = bool(complete)
        pi_desktop_controller.write_json(Path(job["invocation_dir"]) / "transcript.json",
            {"complete": bool(complete), "detail": detail, "messages": messages or []})
        return {"kind": "settle", "result": plus_interaction.blocked_review(details)}
    if outcome == "timeout":
        stopped = pi_desktop_controller.stop_turn(client, session_id, turn_id)
        # The generic controller permits historical identity normalization.
        # Only a fresh strict read can prove this leased reader has stopped.
        try:
            proof = client.turn_get(session_id, turn_id, require_identity=True)
            turn = proof.get("turn") if isinstance(proof.get("turn"), dict) else proof
            if (turn.get("sessionId") == session_id and turn.get("turnId") == turn_id
                    and turn.get("status") in ("completed", "error", "aborted")):
                job["owner_stopped"] = True
        except util.KanpilotError:
            pass
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
        detail = transcript_detail or "转录不完整"
        if not detail.startswith("transcript_incomplete:"):
            detail = f"transcript_incomplete: {detail}"
        return {
            "kind": "blocked",
            "detail": detail,
        }
    normalizations, metadata = [], {}
    result, error = parse_result(messages, normalizations=normalizations, metadata=metadata)
    if result is None:
        return {"kind": "blocked", "detail": f"review_result_invalid: {error}"}
    job.update(metadata)
    log.emit("system", util.now_iso(), content="Review 结果格式：" + metadata["result_format"])
    if normalizations:
        job["result_normalizations"] = normalizations
        log.emit("system", util.now_iso(),
                 content="Review 结果格式标准化：" + ", ".join(normalizations))
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
    job["interaction_policy"] = {"interactionMode": "unattended", "interactionPolicyVersion": 1}
    job["send_state"] = "unknown"
    if job.get("pool_lease"):
        conn = db.connect(job["pool_db_path"])
        try:
            task_workspace.pool_owner(conn, job, state="running")
        finally:
            conn.close()
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    try:
        result = client.agent_prompt(session_id, instruction)
    except util.KanpilotError as exc:
        if exc.code in ("validation_failed", "interaction_policy_unverified"):
            job["send_state"] = "not_sent"
        if exc.code == "interaction_policy_unverified":
            return f"interaction_policy_unverified: {exc.message}（未发送模型请求）"
        return f"prompt_response_lost: {exc.message}（不重发）"
    turn_id = result.get("turnId") if isinstance(result, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        return "prompt_response_lost: 响应未含 turnId（不重发）"
    job["turn_id"] = turn_id
    if job.get("pool_lease"):
        conn = db.connect(job["pool_db_path"])
        try:
            task_workspace.pool_owner(conn, job, state="running")
        finally:
            conn.close()
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)
    started, proof_detail = _prove_started(
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


def _prove_started(client, session_id: str, turn_id: str):
    """A persistent hard interception proves acceptance even if it ended fast."""
    started, detail = pi_desktop_controller.prove_started(client, session_id, turn_id)
    if started:
        return True, None
    try:
        record = client.turn_get(session_id, turn_id)
        turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
        if (turn.get("sessionId") == session_id and turn.get("turnId") == turn_id
                and turn.get("status") == "error"
                and (turn.get("errorCode") or turn.get("error_code")) == "INTERACTION_REQUIRED"):
            return True, None
    except util.KanpilotError:
        pass
    return False, detail

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
    if "stage_contract" in job and "stage_result" not in job and status == "failed":
        from . import stage_contract
        # An invalid contract remains invalid; closing must not downgrade it
        # into a successful legacy result or prevent persistence of the stop.
        try:
            job["stage_result"] = stage_contract.make_result(job["stage_contract"],
                {"code": reason or "review_failed", "detail": detail or "评审未完成"},
                outcome="failed")
        except util.KanpilotError:
            job["stage_contract_invalid"] = True
    job.update({
        "status": status, "exit_code": exit_code, "ended_at": util.now_iso(),
        "detail": detail,
    })
    if job.get("pool_db_path"):
        task_workspace.release_pool(job["pool_db_path"], job, Path(job_path).parent)
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


def _freeze_manual_job(job, snapshot, materials):
    hashes = {path: util.sha256_file(Path(path)) for path in materials["files"].values()}
    for name in ("tracked-manifest.json", "untracked-manifest.json"):
        manifest = materials["files"].get(name)
        if manifest:
            entries = json.loads(Path(manifest).read_text(encoding="utf-8")).get("files") or []
            hashes.update({entry["material_path"]: entry["sha256"] for entry in entries})
    job.update(manual_snapshot_head=snapshot["head"],
               manual_snapshot_fingerprint=review_snapshot.fingerprint(snapshot),
               material_hashes=hashes)


def _freeze_branch_job(job, task, materials):
    job.update(review_branch_snapshot=core.branch_review_snapshot(task),
               material_hashes={path: util.sha256_file(Path(path))
                                for path in materials["files"].values()})


def _guard_branch_job(job, task):
    validate_job_pool(job)
    util.require(job["review_branch_snapshot"] == core.branch_review_snapshot(task),
                 "Review 固定分支版本已变化", "review_blocked")
    core.require_committed_review(task, job["execution_run_id"])
    for path, digest in job["material_hashes"].items():
        util.require(util.sha256_file(Path(path)) == digest,
                     "Review 冻结材料已变化", "review_blocked")
    root = job["review_root"]
    for args, expected in ((["rev-parse", "HEAD"], job["review_branch_snapshot"]["head_sha"]),
                           (["rev-parse", "--abbrev-ref", "HEAD"], "HEAD")):
        output, error = _git(["-C", root, *args])
        util.require(error is None and (output or "").strip() == expected,
                     "Review 提交快照已变化", "review_blocked")
    try:
        # Shared NUL-delimited parser includes both ends of renames/copies.
        # Tracked artifacts are as disposable as newly generated artifacts.
        changes = git_delivery.worktree_changes(root)
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("review_blocked", "Review 提交快照不可读取") from exc
    util.require(all(git_delivery.is_build_artifact(change["path"]) for change in changes),
                 "Review 提交快照已变化", "review_blocked")


def _restore_legacy_pr_job(job, task):
    """Recover the old PR freeze from its existing commit snapshot/materials."""
    delivery = core.delivery_of(task)
    core.require_committed_review(task, job["execution_run_id"])
    root, files = job.get("review_root"), job.get("materials") or {}
    util.require(root and files.get("changes.diff") and files.get("commits.log"),
                 "历史 PR 评审缺少固定快照或完整范围材料", "review_blocked")
    for name, args in (("changes.diff", ["diff", "--no-color", delivery["base_sha"], delivery["head_sha"]]),
                       ("commits.log", ["log", "--oneline", delivery["base_sha"] + ".." + delivery["head_sha"]])):
        output, error = _git(["-C", root, *args])
        util.require(error is None, "历史 PR 评审版本不可读取", "review_blocked")
        if name == "changes.diff" and len((output or "").encode("utf-8")) > 400 * 1024:
            output, error = _git(["-C", root, "diff", "--stat", delivery["base_sha"], delivery["head_sha"]])
            util.require(error is None, "历史 PR 评审范围不可读取", "review_blocked")
            output = (output or "") + "\nDiff 超过 400KB，请在只读工作树中查看具体文件。\n"
        expected = output if not output or output.endswith("\n") else output + "\n"
        util.require(Path(files[name]).read_text(encoding="utf-8") == expected,
                     "历史 PR 评审材料与当前固定完整范围不一致", "review_blocked")
    _freeze_branch_job(job, task, {"files": files})
    _guard_branch_job(job, task)


def _guard_manual_job(job):
    validate_job_pool(job)
    for path, digest in job["material_hashes"].items():
        if util.sha256_file(Path(path)) != digest:
            raise util.KanpilotError("review_blocked", "Review 冻结材料已变化")
    snapshot = review_snapshot.from_materials(job["materials"], job["manual_snapshot_head"])
    if review_snapshot.fingerprint(snapshot) != job["manual_snapshot_fingerprint"]:
        raise util.KanpilotError("review_blocked", "Review 冻结快照身份已变化")
    review_snapshot.verify_source(Path(job["execution_worktree"]), snapshot)
    review_snapshot.verify_root(Path(job["review_root"]), snapshot)


def _settle(**kwargs) -> int:
    from . import review_settle
    job = kwargs["job"]
    try:
        validate_job_pool(job)
    except util.KanpilotError as exc:
        return _stop_no_write(job=job, job_path=kwargs["job_path"], log=kwargs["log"],
                              record_run=kwargs["record_run"], detail=exc.message)
    module = sys.modules[__name__]
    if not job.get("manual_snapshot_head") and not job.get("review_branch_snapshot"):
        task = _facts(kwargs["db_path"], job["task_id"], job["request_id"]).get("task")
        if isinstance(task, dict) and core.is_pr_delivery(task):
            try:
                _restore_legacy_pr_job(job, task)
                pi_desktop_controller.write_json(kwargs["job_path"], job)
            except (util.KanpilotError, OSError, ValueError, KeyError) as exc:
                detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
                return _blocked_no_pass(**kwargs, detail="历史 PR 评审固定版本核验失败：" + detail)
    if not job.get("manual_snapshot_head") and not job.get("review_branch_snapshot"):
        return review_settle.settle_job(driver=module, **kwargs)

    class SnapshotGuardError(Exception):
        pass

    class GuardedDriver:
        def __getattr__(self, name):
            return getattr(module, name)

        def guard(self):
            try:
                if job.get("review_branch_snapshot"):
                    task = _facts(kwargs["db_path"], job["task_id"], job["request_id"]).get("task")
                    util.require(isinstance(task, dict), "Review 任务不可读", "review_blocked")
                    _guard_branch_job(job, task)
                else:
                    _guard_manual_job(job)
            except (util.KanpilotError, OSError, ValueError, KeyError) as exc:
                detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
                raise SnapshotGuardError(detail) from exc

    try:
        return review_settle.settle_job(driver=GuardedDriver(), **kwargs)
    except SnapshotGuardError as exc:
        return _blocked_no_pass(**kwargs, detail="Review 快照或材料核验失败：" + str(exc))

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
              log_path: Path, record_run: Callable, resume: bool = False,
              request_id=None, config_override=None) -> int:
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

    if config_override is None:
        from . import cmd_exec, review_config
        candidate = cmd_exec._select_candidate(conn, STAGE, request_id, util.now_dt())
        if candidate is not None:
            request_id = candidate["request_id"]
            config_override = review_config.for_request(conn, db.request_payload(candidate))
    backend, block = pi_desktop_controller.review_round_config(conn, config_override)
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
        request_id=request_id,
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
                 log_path: Path, record_run: Callable, request_id=None) -> int:
    invocation_id = util.new_uuid()
    directory = pi_desktop_controller.invocation_dir(STAGE, invocation_id)
    job_path = directory / "job.json"
    log = pi_desktop_controller.EventLog(directory / "events.jsonl")
    job = _new_job(block, invocation_id, directory, log_path)
    pi_desktop_controller.write_json(job_path, job)
    pi_desktop_controller.record_run_entry(record_run, STAGE, job)

    claim = core_call(db_path, "claim", {"stage": STAGE, "executor": EXECUTOR_ID,
                      **({"request_id": request_id} if request_id else {})})
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
    try:
        snapshot = review_snapshot.freeze(execution_worktree) if not core.requires_branch_review(task) else None
        review_root = prepare_pooled_worktree(conn, db_path, job, task, execution_worktree, snapshot)
    except (util.KanpilotError, OSError) as exc:
        if isinstance(exc, util.KanpilotError) and exc.code == "worktree_pool_waiting":
            _close(job=job, job_path=job_path, log=log, record_run=record_run,
                   status="finished", exit_code=0, detail=exc.message, reason=exc.code)
            return 0
        return _blocked_no_pass(db_path=db_path, job=job, job_path=job_path,
                                log=log, record_run=record_run,
                                detail=exc.message if isinstance(exc, util.KanpilotError) else str(exc))
    # Keep generated inputs outside the committed snapshot so it stays clean/reusable.
    try:
        materials = collect_materials(
            db_path=db_path, conn=conn,
            materials=directory / "materials",
            task=task, execution_worktree=execution_worktree,
            execution_run_id=job["execution_run_id"], snapshot=snapshot,
        )
        if snapshot is not None:
            review_snapshot.verify_root(review_root, snapshot)
            _freeze_manual_job(job, snapshot, materials)
        else:
            _freeze_branch_job(job, task, materials)
    except (util.KanpilotError, OSError) as exc:
        detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
        return _blocked_no_pass(db_path=db_path, job=job, job_path=job_path,
                                log=log, record_run=record_run, detail=detail)
    job["review_root"] = str(review_root)
    job["materials"] = materials["files"]
    try:
        review_settle.freeze_stage_contract(job, task)
    except util.KanpilotError as exc:
        return _blocked_no_pass(db_path=db_path, job=job, job_path=job_path,
            log=log, record_run=record_run, detail=exc.message)
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
    try:
        result = review_settle.record_stage_result(job, result,
            facts=_facts(db_path, job["task_id"], job["request_id"]), evidence={
                "mechanism": "plus_turn", "session_id": job.get("session_id"),
                "turn_id": job.get("turn_id"),
                "transcript_complete": job.get("transcript_complete"),
                "owner_stopped": job.get("owner_stopped")})
    except util.KanpilotError as exc:
        if exc.code == "stale_run":
            return _stop_no_write(job=job, job_path=job_path, log=log,
                record_run=record_run, detail=exc.message)
        return _blocked_no_pass(db_path=db_path, job=job, job_path=job_path,
            log=log, record_run=record_run, detail=exc.message)
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
    if job.get("stage_contract_required") and "stage_contract" not in job:
        print("Review 新轮次缺少冻结契约；不走历史恢复路径", file=sys.stderr)
        return 1
    if ("stage_contract" in job and job.get("status") == "finished"
            and "stage_result" not in job):
        print("Review 已结束轮次缺少结果包", file=sys.stderr)
        return 1
    if "stage_contract" in job:
        from . import stage_contract
        try:
            stage_contract.validate_contract(job["stage_contract"], expected={
                "stage": STAGE, "task_id": job.get("task_id"),
                "request_id": job.get("request_id"), "run_id": job.get("run_id"),
                "harness_id": job.get("backend") or "pi_desktop"})
            if "stage_result" in job:
                stage_contract.validate_result(job["stage_result"], job["stage_contract"])
        except util.KanpilotError as exc:
            print("Review 恢复契约无效：" + exc.message, file=sys.stderr)
            return 1
    else:
        job["legacy_contract_path"] = True
    if job.get("status") == "finished":
        print("executor review 控制轮已收尾：无需恢复", file=sys.stderr)
        return 0
    try:
        validate_job_pool(job)
    except util.KanpilotError as exc:
        print(exc.message, file=sys.stderr)
        return 1
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
        try:
            job["interaction_policy"] = plus_interaction.verify_session(client, session_id)
        except util.KanpilotError as exc:
            return _request_failed(db_path=db_path, job=job, job_path=job_path, log=log,
                                   record_run=record_run, detail=exc.message)
        started, proof_detail = _prove_started(
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
