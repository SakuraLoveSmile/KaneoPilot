"""Shared strict Review result and deterministic settlement (protocol §5.27.11)."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Optional, Tuple
from . import constants, core, pi_desktop_controller, util

STAGE = "review"
RESULT_KEYS = ("conclusion", "report_markdown", "fix")
FIX_KEYS = ("title", "description", "priority")
_TERMINAL_REQUEST_STATUSES = ("succeeded", "failed", "cancelled")
RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": list(RESULT_KEYS),
    "properties": {
        "conclusion": {"type": "string", "enum": list(constants.REVIEW_CONCLUSIONS)},
        "report_markdown": {"type": "string"},
        "fix": {"type": ["object", "null"], "additionalProperties": False,
                "required": list(FIX_KEYS), "properties": {
                    "title": {"type": "string"}, "description": {"type": "string"},
                    "priority": {"type": "string", "enum": list(constants.REVIEW_FIX_PRIORITIES)}}}
    }
}

def raw_task(task):
    """Core helpers consume rows; get-task exposes the same extra as a DTO."""
    if "extra_json" in task:
        return task
    return dict(task, extra_json=json.dumps(task.get("extra") or {}))

def is_managed(task):
    return core.is_automation_managed(raw_task(task))

def report_root() -> Path:
    """报告根（protocol §5.24.5：``<APP_SUPPORT>/reports``；核心按此校验）。"""
    return constants.review_reports_root()


def report_path(task_id: str, run_id: str) -> Path:
    return report_root() / task_id / f"{run_id}.md"


def review_key(source_task_id: str, execution_run_id: str,
               report_sha256: str) -> str:
    """``sha256(规范 JSON(源 task_id, 本周期执行 run_id, 报告 sha))``。"""
    payload = json.dumps(
        [source_task_id, execution_run_id, report_sha256],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_report(path: Path, markdown: str) -> str:
    """写评审报告并返回其 sha256（核心登记时会重算核对）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return util.sha256_file(path)


def _validate_result(value: dict) -> Tuple[Optional[dict], Optional[str]]:
    """严格校验冻结结果结构（三键齐全、fix 仅在 changes_requested 出现）。"""
    if not isinstance(value, dict):
        return None, "最终答复不是 JSON 对象"
    if set(value) != set(RESULT_KEYS):
        return None, (
            "最终 JSON 字段不是冻结集合 "
            f"{sorted(RESULT_KEYS)}：{sorted(value)}"
        )
    conclusion = value.get("conclusion")
    if conclusion not in constants.REVIEW_CONCLUSIONS:
        return None, (
            f"conclusion 非法：{conclusion}"
            f"（可用：{', '.join(constants.REVIEW_CONCLUSIONS)}）"
        )
    markdown = value.get("report_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return None, "report_markdown 必须是非空字符串"
    fix = value.get("fix")
    if conclusion == "changes_requested":
        if not isinstance(fix, dict) or set(fix) != set(FIX_KEYS):
            return None, (
                f"changes_requested 必须携带 fix（字段 {sorted(FIX_KEYS)}）"
            )
        for key in ("title", "description"):
            if not isinstance(fix.get(key), str) or not fix[key].strip():
                return None, f"fix.{key} 必须是非空字符串"
        if fix.get("priority") not in constants.REVIEW_FIX_PRIORITIES:
            return None, (
                f"fix.priority 非法：{fix.get('priority')}"
                f"（可用：{', '.join(constants.REVIEW_FIX_PRIORITIES)}）"
            )
        fix = {key: fix[key].strip() for key in FIX_KEYS}
    elif fix is not None:
        return None, "非 changes_requested 结论不得携带 fix"
    return {
        "conclusion": conclusion,
        "report_markdown": markdown,
        "fix": fix,
    }, None

def _registered_matches(task, job):
    key = job.get("review_key")
    extra = task.get("extra") or {}
    fix = extra.get("fix_request") or {}
    history = task.get("review_history") or extra.get("review_history") or []
    if key and (fix.get("review_key") == key or any(
            entry.get("review_key") == key for entry in history if isinstance(entry, dict))):
        return True
    summary = task.get("review_summary") or {}
    if task.get("review_report_sha256") != job.get("report_sha256"):
        return False
    if summary.get("conclusion") != job.get("conclusion"):
        return False
    if key and summary.get("review_key") == key:
        return True
    if job.get("conclusion") == "changes_requested":
        return summary.get("review_key") == key and summary.get("fix_task_id") == job.get("fix_task_id")
    return not is_managed(task)


def validate_result(value) -> dict:
    result, error = _validate_result(value)
    if error:
        raise util.KanpilotError("validation_failed", error)
    return result

def settle_job(*, driver, db_path, job, job_path, log, record_run) -> int:
    """登记与推进（含部分成功恢复）：pass / changes_requested / blocked。"""
    # The driver keeps the existing Plus persistence/transport seams and recovery.
    core_call = driver.core_call
    _facts = driver._facts
    _blocked_no_pass = driver._blocked_no_pass
    _stop_no_write = driver._stop_no_write
    _lease_valid = driver._lease_valid
    _identity_payload = driver._identity_payload
    _registration_failed = driver._registration_failed
    _review_summary = driver._review_summary
    _report = driver._report
    _close = driver._close
    task_id, request_id = job["task_id"], job["request_id"]
    checkpoints = job.setdefault("checkpoints", {})
    conclusion = job.get("conclusion")
    backend_label = job.get("backend") or "Plus"
    facts = _facts(db_path, task_id, request_id)
    task, request = facts.get("task"), facts.get("request")
    if not isinstance(task, dict) or not isinstance(request, dict):
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run,
            detail="收尾事实核对失败：核心已无该任务/请求",
        )
    if request.get("stage") != STAGE or request.get("run_id") != job.get("run_id"):
        return _stop_no_write(
            job=job, job_path=job_path, log=log, record_run=record_run,
            detail="请求已被新 run 接管（不覆盖新周期）",
        )
    automated = is_managed(task)
    status = request.get("status")
    if status in _TERMINAL_REQUEST_STATUSES:
        recorded = (request.get("result") or {}).get("outcome")
        if job.get("report_intent") and recorded == job["report_intent"]:
            checkpoints["reported"] = True
            _close(job=job, job_path=job_path, log=log, record_run=record_run,
                   status="finished", exit_code=0,
                   detail="汇报已完成（响应丢失只读核对，不重放）",
                   reason=f"reported_{recorded}")
            return 0
        return _stop_no_write(
            job=job, job_path=job_path, log=log, record_run=record_run,
            detail=f"请求已以 {status}/{recorded} 结束（不覆盖）",
        )
    if status != "claimed":
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run,
            detail=f"请求状态不是 claimed（{status}），不登记结论",
        )
    if not _lease_valid(request):
        return _stop_no_write(
            job=job, job_path=job_path, log=log, record_run=record_run,
            detail="lease_expired: 租约失效，停止写入，交人工恢复",
        )

    # 报告落盘（恢复时按既有 sha 复用，不写第二份不同内容）。
    markdown = job.get("report_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run, detail="缺报告正文：不登记结论",
        )
    target = report_path(task_id, job["run_id"])
    existing = Path(str(job.get("report_path") or ""))
    if (job.get("report_sha256") and existing == target and target.is_file()
            and util.sha256_file(target) == job["report_sha256"]):
        pass
    else:
        job["report_path"] = str(target)
        job["report_sha256"] = write_report(target, markdown)
    job["review_key"] = review_key(
        task_id, job["execution_run_id"], job["report_sha256"]
    )
    if (task.get("review_report_sha256")
            and task["review_report_sha256"] != job["report_sha256"]):
        return _stop_no_write(
            job=job, job_path=job_path, log=log, record_run=record_run,
            detail="本周期已登记其他报告（不覆盖新周期）",
        )

    if _registered_matches(task, job):
        checkpoints["registered"] = True
        if conclusion == "changes_requested":
            job["fix_task_id"] = (
                _review_summary(task).get("fix_task_id") or job.get("fix_task_id")
            )
            checkpoints["fix_created"] = True
        log.emit("system", util.now_iso(),
                 content="登记已存在且与同一报告 sha/结论/key/fix 一致：跳过 register")
    else:
        if conclusion == "changes_requested" and not automated:
            fix = job.get("fix") or {}
            try:
                created = core_call(db_path, "create-review-fix", {
                    "source_task_id": task_id,
                    "title": fix.get("title"),
                    "description": fix.get("description"),
                    "priority": fix.get("priority"),
                    "dedupe_key": job["review_key"],
                    **_identity_payload(job),
                })
            except util.KanpilotError as exc:
                return _registration_failed(
                    db_path=db_path, job=job, job_path=job_path, log=log,
                    record_run=record_run, label="create-review-fix", exc=exc,
                )
            job["fix_task_id"] = created.get("fix_task_id")
            job["fix_task_source_sha256"] = created.get("source_sha256")
            checkpoints["fix_created"] = True
            pi_desktop_controller.write_json(job_path, job)
        payload = {
            "task_id": task_id, "report_path": job["report_path"],
            "conclusion": conclusion, **_identity_payload(job),
        }
        if automated:
            payload["review_key"] = job["review_key"]
            if conclusion == "changes_requested":
                payload.update({"fix_mode": "same_task", "fix": job.get("fix")})
        elif conclusion == "changes_requested":
            payload.update({
                "review_key": job["review_key"],
                "fix_task_id": job["fix_task_id"],
                "fix_task_source_sha256": job.get("fix_task_source_sha256"),
            })
        try:
            core_call(db_path, "register-review", payload)
        except util.KanpilotError as exc:
            return _registration_failed(
                db_path=db_path, job=job, job_path=job_path, log=log,
                record_run=record_run, label="register-review", exc=exc,
            )
        checkpoints["registered"] = True

    if conclusion == "pass" and not automated:
        if task.get("status") == "done":
            if _review_summary(task).get("conclusion") != "pass":
                return _stop_no_write(
                    job=job, job_path=job_path, log=log, record_run=record_run,
                    detail="任务已 done 但当前周期无 pass 登记（不推进）",
                )
            checkpoints["advanced"] = True
            log.emit("system", util.now_iso(),
                     content="任务已 done 且 pass 登记一致：跳过 advance-task")
        else:
            try:
                core_call(db_path, "advance-task", {
                    "task_id": task_id, "target": "done",
                    "expected_current": "in-review",
                    **_identity_payload(job),
                    "evidence_note": f"{backend_label} Review pass；报告 {job['report_path']}",
                })
            except util.KanpilotError as exc:
                return _registration_failed(
                    db_path=db_path, job=job, job_path=job_path, log=log,
                    record_run=record_run, label="advance-task", exc=exc,
                )
            checkpoints["advanced"] = True

    outcome = (
        "succeeded" if conclusion in ("pass", "changes_requested")
        else "needs_user"
    )
    # Persist the intent before report so a crash after the core commits can
    # recover by reading the request, without replaying the report command.
    job["report_intent"] = outcome
    pi_desktop_controller.write_json(job_path, job)
    note = f"{backend_label} Review {conclusion}；报告 {job['report_path']}"
    if conclusion == "blocked":
        note = f"{note}（review_blocked，交人工）"
    ok, error = _report(db_path, job, outcome=outcome, note=note,
                        failure_code="review_blocked" if conclusion == "blocked" else None)
    if not ok:
        return _blocked_no_pass(
            db_path=db_path, job=job, job_path=job_path, log=log,
            record_run=record_run, detail=error or "report 失败",
        )
    job["review_outcome"] = conclusion
    _close(job=job, job_path=job_path, log=log, record_run=record_run,
           status="finished", exit_code=0, detail=None,
           reason=f"review_{conclusion}")
    return 0

def settle(*, db_path, task, request, run_id, result, backend, cost_usd) -> dict:
    """Settle an already validated result; all writes use fixed core commands."""
    from . import pi_desktop_review as driver
    result = validate_result(result)
    directory = pi_desktop_controller.invocation_dir(STAGE, run_id)
    job = {"task_id": task["task_id"], "request_id": request["request_id"],
           "run_id": run_id, "execution_run_id": task.get("run_id"),
           "backend": backend, "cost_usd": cost_usd, "checkpoints": {}, **result}
    saved = directory / "job.json"
    if saved.is_file():
        previous = json.loads(saved.read_text(encoding="utf-8"))
        identity = ("task_id", "request_id", "run_id", "execution_run_id")
        if any(previous.get(key) != job.get(key) for key in identity):
            raise util.KanpilotError("conflict", "Review 结算检查点身份不一致")
        if any(previous.get(key) != result[key] for key in RESULT_KEYS):
            raise util.KanpilotError("conflict", "Review 结算检查点已有其他结果")
        job = previous
    code = settle_job(driver=driver, db_path=db_path, job=job,
                      job_path=directory / "job.json",
                      log=pi_desktop_controller.EventLog(directory / "events.jsonl"),
                      record_run=lambda *args, **kwargs: None)
    return {"exit_code": code, "job": job}
