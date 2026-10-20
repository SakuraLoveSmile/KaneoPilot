"""Software-owned clarification drafts and atomic continuation requests."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from . import core, db, plan_questions, planner_inputs, project_config, remote, util


def _digest(value):
    return util.sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"), allow_nan=False).encode())


def _identity(request, questions):
    return "clarification-" + _digest({"request_id": request["request_id"],
                                      "run_id": request["run_id"], "questions": questions})[:32]


def _latest_request(conn, task_id):
    return conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='plan' ORDER BY rowid DESC LIMIT 1",
                        (task_id,)).fetchone()


def _base(request, questions, evidence, task):
    payload = db.request_payload(request)
    return {"id": _identity(request, questions), "request_id": request["request_id"],
            "run_id": request["run_id"], "questions": questions, "saved_answers": [],
            "supplement": "", "saved_at": None, "blocked_at": task["blocked_at"],
            "task_material_version": {key: task[key] for key in ("requirements_sha256", "source_sha256")},
            "input_id": payload.get("input_id"), "input_sha256": payload.get("planner_input_sha256"),
            "plan_markdown": evidence.get("plan_markdown"), "draft_path": evidence.get("draft_path")}


def register(conn, task, request, now, evidence):
    """Called in report's transaction only for a planner needs_input result."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("questions"), list):
        return
    questions = plan_questions.normalize(evidence["questions"])
    if not questions:
        return
    db.set_extra(conn, task, now, plan_clarification=_base(request, questions, evidence, task))


def candidate(conn, task):
    if not db.task_is_active(task) or task["status"] != "needs_user" or task["failure_code"] != "reported" or task["failure_stage"] not in ("plan", "planning", "needs_plan"):
        return None
    request = _latest_request(conn, task["task_id"])
    if request is None or request["status"] != "failed" or not request["run_id"]:
        return None
    # Any newer queued/claimed workflow invalidates the old questions.
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed') LIMIT 1", (task["task_id"],)).fetchone():
        return None
    attempt = conn.execute("SELECT * FROM attempts WHERE request_id=? AND run_id=? ORDER BY id DESC LIMIT 1",
                           (request["request_id"], request["run_id"])).fetchone()
    if attempt is None or attempt["outcome"] != "needs_user":
        return None
    saved = db.task_extra(task).get("plan_clarification")
    if isinstance(saved, dict) and saved.get("request_id") == request["request_id"] and saved.get("run_id") == request["run_id"]:
        if saved.get("blocked_at") != task["blocked_at"]:
            return None
        return copy.deepcopy(saved)
    evidence = util.jload(attempt["evidence_json"]) or {}
    if not isinstance(evidence, dict) or not isinstance(evidence.get("questions"), list):
        return None
    try:
        questions = plan_questions.normalize(evidence["questions"])
    except util.KanpilotError:
        return None
    return _base(request, questions, evidence, task) if questions else None


def view(conn, task):
    item = candidate(conn, task)
    if item is None:
        return None
    return {key: item[key] for key in ("id", "request_id", "run_id", "questions", "saved_answers", "supplement", "saved_at")}


def _current(conn, payload):
    task = db.require_task(conn, payload["task_id"])
    item = candidate(conn, task)
    if item is None or item["id"] != payload["clarification_id"]:
        raise util.KanpilotError("conflict", "规划问题已失效，请刷新任务后重新回答")
    return task, item


def _answers(item, raw):
    if not isinstance(raw, list):
        raise util.bad_request("answers 必须是列表")
    questions = {q["id"]: q for q in item["questions"]}
    answers, seen = {}, set()
    for answer in raw:
        if not isinstance(answer, dict) or set(answer) - {"question_id", "option_id", "text"}:
            raise util.bad_request("答案字段无效")
        qid = answer.get("question_id")
        if not isinstance(qid, str) or qid not in questions or qid in seen:
            raise util.bad_request("答案包含重复或未知问题")
        seen.add(qid)
        q = questions[qid]
        opt, text = answer.get("option_id"), answer.get("text", "")
        if not isinstance(text, str) or (opt is not None and not isinstance(opt, str)):
            raise util.bad_request("答案选项与正文必须是文本")
        text = text.strip()
        normalized = {"question_id": qid}
        if q["options"]:
            valid = {o["id"] for o in q["options"]}
            if opt == "custom":
                if not q["allow_custom"]:
                    raise util.bad_request("此问题不允许自定义答案")
            elif opt is not None and opt not in valid:
                raise util.bad_request("答案选项已失效")
            if opt is not None:
                normalized["option_id"] = opt
        elif opt is not None:
            raise util.bad_request("文本题不能提交选项")
        if text:
            normalized["text"] = text
        answers[qid] = normalized
    return [answers[q["id"]] for q in item["questions"] if q["id"] in answers]


def _supplement(payload):
    value = payload.get("supplement", "")
    if not isinstance(value, str):
        raise util.bad_request("supplement 必须是文本")
    return value.strip()


def _complete(item, answers):
    by_id = {a["question_id"]: a for a in answers}
    for q in item["questions"]:
        answer = by_id.get(q["id"], {})
        option, text = answer.get("option_id"), answer.get("text", "")
        present = bool(option) if q["options"] else bool(text)
        if q["required"] and not present:
            raise util.bad_request("请回答必答问题：" + q["title"])
        if option == "custom" and not text:
            raise util.bad_request("请填写自定义方案：" + q["title"])
        if q["options"] and text and not option:
            raise util.bad_request("请明确选择选项：" + q["title"])


def _save(conn, task, item, answers, supplement, now):
    item.update(saved_answers=answers, supplement=supplement, saved_at=now)
    db.set_extra(conn, task, now, plan_clarification=item)


def cmd_save_plan_answers(ctx, payload):
    with db.write_txn(ctx.conn):
        task, item = _current(ctx.conn, payload)
        answers = _answers(item, payload["answers"])
        _save(ctx.conn, task, item, answers, _supplement(payload), ctx.now_iso)
    return {"ok": True, "saved_at": ctx.now_iso}


def _receipt(conn, task_id, operation_id, fingerprint):
    task = db.require_task(conn, task_id)
    record = (db.task_extra(task).get("plan_answer_submissions") or {}).get(operation_id)
    if not record:
        return None
    if record["fingerprint"] != fingerprint:
        raise util.KanpilotError("conflict", "同一 operation_id 已用于不同的规划答案")
    return {"ok": True, "request_id": record["request_id"], "deduplicated": True}


def _material(conn, item, request):
    payload = db.request_payload(request)
    row = db.get_planner_input(conn, item.get("input_id"))
    if row is None or row["task_id"] != request["task_id"]:
        raise util.KanpilotError("conflict", "原规划材料缺失，请重新准备材料")
    manifest = db.planner_input_manifest(row)
    if row["input_sha256"] != item.get("input_sha256") or planner_inputs._manifest_sha(manifest) != row["input_sha256"] or payload.get("manifest") != manifest:
        raise util.KanpilotError("conflict", "原规划材料版本已变化，请重新准备材料")
    root = planner_inputs._inputs_root(conn)
    old_dir = util.inside(root / item["input_id"], root, "planner_input")
    raw, reason = planner_inputs._read_worktree_material(str(old_dir), "manifest.json", 20 * 1024 * 1024)
    try:
        disk_manifest = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        disk_manifest = None
    if disk_manifest != manifest:
        raise util.KanpilotError("conflict", "规划材料清单已变化或不可读取，请重新准备材料")
    copies, total = {}, 0
    from . import constants
    for entry in manifest.get("files", []):
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel or rel == "manifest.json":
            raise util.KanpilotError("conflict", "原规划材料文件路径无效")
        content, reason = planner_inputs._read_worktree_material(str(old_dir), rel, constants.PLAN_INPUT_TOTAL_MAX_BYTES - total)
        if content is None or util.sha256_bytes(content) != entry.get("sha256"):
            raise util.KanpilotError("conflict", "原规划材料已变化或不可读取：" + rel)
        copies[rel] = content
        total += len(content)
    return payload, manifest, copies


def _draft(conn, item):
    if isinstance(item.get("plan_markdown"), str):
        return item["plan_markdown"]
    # Legacy report evidence only recorded the draft path. Read the exact
    # registered run directory without following symlinks; never arbitrary paths.
    if not item.get("draft_path"):
        return ""
    from . import constants, round_store
    request = db.get_request(conn, item["request_id"])
    task_id = request["task_id"]
    registered = round_store.get_round(conn, task_id, item["run_id"])
    draft = Path(item["draft_path"])
    expected_tail = (task_id, item["run_id"], "plan-draft.md")
    if registered is None or not draft.is_absolute() or draft.parts[-3:] != expected_tail or any(part == ".." for part in draft.parts):
        raise util.KanpilotError("conflict", "历史规划草稿路径与登记轮次不符")
    # The exact draft path is durable report evidence from this registered run.
    # Its original root remains authoritative if Settings now point elsewhere.
    old_root = draft.parents[2]
    raw, reason = planner_inputs._read_worktree_material(str(old_root), "/".join(expected_tail), constants.PLAN_INPUT_FILE_MAX_BYTES)
    if raw is None:
        # Empty plans historically did not write a draft file.
        if reason == "deleted":
            return ""
        raise util.KanpilotError("conflict", "历史规划草稿不可读取")
    return raw.decode("utf-8")


def _submit_plan_answers(ctx, payload):
    conn, now = ctx.conn, ctx.now_iso
    operation_id = payload["operation_id"].strip()
    if not operation_id:
        raise util.bad_request("operation_id 不能为空")
    # Normalize ordering for retries before checking current question state.
    fingerprint = _digest({"clarification_id": payload["clarification_id"],
                           "answers": sorted(payload["answers"], key=lambda a: str(a.get("question_id", "")) if isinstance(a, dict) else str(a)),
                           "supplement": _supplement(payload)})
    cached = _receipt(conn, payload["task_id"], operation_id, fingerprint)
    if cached:
        return cached
    # Save a valid partial draft before completeness, file or remote validation.
    # Failed continuation remains needs_user with the user's answers intact.
    with db.write_txn(conn):
        cached = _receipt(conn, payload["task_id"], operation_id, fingerprint)
        if cached:
            return cached
        task, item = _current(conn, payload)
        answers = _answers(item, payload["answers"])
        _save(conn, task, item, answers, _supplement(payload), now)
    _complete(item, answers)
    task, item = _current(conn, payload)
    request = db.get_request(conn, item["request_id"])
    old_payload, manifest, copies = _material(conn, item, request)
    _require_continuation_workspace(conn, task, old_payload)
    if item.get("task_material_version") != {key: task[key] for key in ("requirements_sha256", "source_sha256")}:
        raise util.KanpilotError("conflict", "任务材料版本已变化，请重新准备材料")
    try:
        live = remote.get_task(remote.get_client(), task["task_id"])
    except Exception as exc:
        raise util.KanpilotError("remote_unavailable", "远端需求核验失败，答案已保存：" + str(exc)) from exc
    if (live.get("requirements_sha256") != manifest.get("requirements_sha256")
            or (live.get("task") or {}).get("status") not in core.expected_remote_slugs(task)):
        raise util.KanpilotError("conflict", "远端需求或任务状态已变化，请重新准备材料；答案已保存")
    meta = manifest.get("task") or {}
    mapping = project_config.get_effective_mapping(conn, meta.get("project_id") or task["project_id"])
    if not mapping or mapping.get("mapping_state") != "available" or Path(mapping.get("repository", "")).resolve() != Path(meta.get("repository", "")).resolve():
        raise util.KanpilotError("conflict", "项目仓库映射已变化，请重新准备材料")
    if core.is_user_replan(task) and manifest.get("execution_worktree"):
        if planner_inputs._replan_worktree_state(task) != manifest["execution_worktree"].get("state_sha256"):
            raise util.KanpilotError("conflict", "保留的执行工作树已变化，请重新准备材料")
    if core.is_automation_managed(task) and (db.task_extra(task).get("fix_request") or {}).get("state") == "open":
        planner_inputs._fix_report_material(task)
    input_id = util.new_uuid()
    new_manifest = copy.deepcopy(manifest)
    new_manifest.update(input_id=input_id, created_at=now, plan_clarification={
        "id": item["id"], "request_id": item["request_id"], "run_id": item["run_id"],
        "questions": item["questions"], "answers": answers, "supplement": item["supplement"],
        "answered_at": now, "plan_markdown": _draft(conn, item)})
    history = copy.deepcopy(manifest.get("plan_clarification_history") or [])
    if manifest.get("plan_clarification"):
        history.append(copy.deepcopy(manifest["plan_clarification"]))
    if history:
        new_manifest["plan_clarification_history"] = history
    input_sha = planner_inputs._manifest_sha(new_manifest)
    root = planner_inputs._inputs_root(conn) / input_id
    root.mkdir(parents=True, exist_ok=False)
    try:
        for rel, content in copies.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (root / "manifest.json").write_text(json.dumps(new_manifest, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":"), allow_nan=False), encoding="utf-8")
        with db.write_txn(conn):
            cached = _receipt(conn, payload["task_id"], operation_id, fingerprint)
            if cached:
                shutil.rmtree(root)
                return cached
            fresh_task, fresh_item = _current(conn, payload)
            _require_continuation_workspace(conn, fresh_task, old_payload)
            stable_item = {k: v for k, v in item.items() if k != "saved_at"}
            stable_fresh = {k: v for k, v in fresh_item.items() if k != "saved_at"}
            if stable_fresh != stable_item or task["requirements_sha256"] != fresh_task["requirements_sha256"] or task["source_sha256"] != fresh_task["source_sha256"]:
                raise util.KanpilotError("conflict", "问题或答案在提交期间变化，请刷新后重试")
            if project_config.get_effective_mapping(conn, meta.get("project_id") or fresh_task["project_id"]) != mapping:
                raise util.KanpilotError("conflict", "项目映射在提交期间变化")
            # Recheck original input registration/files before queue commit.
            _material(conn, fresh_item, db.get_request(conn, item["request_id"]))
            db.insert_planner_input(conn, input_id, task["task_id"], input_sha,
                                    new_manifest.get("requirements_sha256"), new_manifest.get("source_sha256"), new_manifest, now)
            next_payload = copy.deepcopy(old_payload)
            next_payload.pop("desktop_claim", None)
            next_payload.update(input_id=input_id, planner_input_sha256=input_sha, manifest=new_manifest,
                                confirmed_at=now, manual_authorized=True,
                                clarification_submission={"operation_id": operation_id, "fingerprint": fingerprint,
                                                          "clarification_id": item["id"]})
            new_request, created = db.create_request(conn, now, "plan", "plan:" + task["task_id"],
                                                    task_id=task["task_id"], payload=next_payload, requested_by="user")
            if not created:
                raise util.KanpilotError("conflict", "任务已有规划请求，请刷新后查看")
            submissions = dict(db.task_extra(fresh_task).get("plan_answer_submissions") or {})
            submissions[operation_id] = {"fingerprint": fingerprint, "request_id": new_request["request_id"],
                                         "clarification_id": item["id"], "submitted_at": now}
            db.set_extra(conn, fresh_task, now, plan_answer_submissions=submissions,
                         plan_clarification={**fresh_item, "submitted_at": now, "next_request_id": new_request["request_id"]},
                         plan_execution_hold=True)
            core.transition_task(conn, db.require_task(conn, task["task_id"]), "needs_plan", now, "user",
                                 detail={"via": "submit-plan-answers"}, **core.clear_block_fields())
            labels = []
            by_id = {a["question_id"]: a for a in answers}
            for question in item["questions"]:
                answer = by_id.get(question["id"])
                if answer is None:
                    continue
                option = next((o["label"] for o in question["options"] if o["id"] == answer.get("option_id")), "自定义方案" if answer.get("option_id") == "custom" else "")
                value = "；".join(part for part in (option, answer.get("text", "")) if part) or "未作答"
                labels.append(question["title"] + "：" + value)
            event_detail = dict(new_manifest["plan_clarification"], note="；".join(labels))
            db.add_event(conn, now, "plan_answers_submitted", task_id=task["task_id"],
                         request_id=new_request["request_id"], actor="user", detail=event_detail)
            return {"ok": True, "request_id": new_request["request_id"], "deduplicated": False}
    except BaseException:
        # This directory is created exclusively by this call; failed SQLite
        # transactions leave the original material and draft untouched.
        shutil.rmtree(root)
        raise


def _require_continuation_workspace(conn, task, original):
    from . import task_workspace
    task_workspace.require_no_workspace_repair(conn, task["task_id"])
    if original.get("delivery_head_sha"):
        current = core.delivery_of(task)
        util.require(original["delivery_head_sha"] == current.get("head_sha")
                     and original.get("plan_cwd") == current.get("worktree_path"),
                     "修复工作树基线或路径已更新，请重新准备规划材料；答案已保存", "conflict")


def cmd_submit_plan_answers(ctx, payload):
    try:
        return _submit_plan_answers(ctx, payload)
    except util.KanpilotError:
        # A competing identical submission can commit between any read-only
        # validation and the next guard. Its durable receipt takes precedence.
        fingerprint = _digest({"clarification_id": payload["clarification_id"],
                               "answers": sorted(payload["answers"], key=lambda a: str(a.get("question_id", "")) if isinstance(a, dict) else str(a)),
                               "supplement": _supplement(payload)})
        cached = _receipt(ctx.conn, payload["task_id"], payload["operation_id"].strip(), fingerprint)
        if cached:
            return cached
        raise
