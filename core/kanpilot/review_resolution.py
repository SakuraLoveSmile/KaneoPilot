"""Explicit, identity-bound remedies for a registered blocked Review.

Preparation never starts a model. User evidence never becomes verified evidence.
All external reads and Git/file mutations precede the final SQLite transaction.
"""
import json
import os
import shutil
import re
from types import SimpleNamespace
from pathlib import Path

from . import bridge, constants, core, db, git_delivery, planfile, remote, review_snapshot, round_store, util


IDENTITY = {"expected_execution_run_id": "execution_run_id",
            "expected_review_request_id": "review_request_id",
            "expected_report_sha256": "report_sha256",
            "expected_plan_sha256": "plan_sha256"}


def candidate(conn, task):
    """SQLite-only eligibility. Commands additionally verify files/live identity."""
    if (not db.task_is_active(task) or task["status"] != "needs_user" or task["stage"] != "review"
            or task["failure_code"] != "review_blocked" or not task["run_id"]
            or not task["plan_sha256"] or not task["review_report_path"]
            or not task["review_report_sha256"]
            or db.task_extra(task).get("review_conclusion") != "blocked"):
        return None
    if conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                    (task["task_id"],)).fetchone():
        return None
    request = conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='review' "
                           "ORDER BY created_at DESC,rowid DESC LIMIT 1", (task["task_id"],)).fetchone()
    row = round_store.get_round(conn, task["task_id"], task["run_id"])
    if (request is None or request["status"] != "failed" or not request["finished_at"]
            or not request["run_id"] or row is None or row["run_status"] != "finished"
            or row["save_state"] != "active"):
        return None
    value = {"execution_run_id": task["run_id"], "review_request_id": request["request_id"],
             "review_run_id": request["run_id"], "report_sha256": task["review_report_sha256"],
             "plan_sha256": task["plan_sha256"]}
    value["id"] = util.sha256_bytes(util.jdump(value).encode())
    value.update(summary=task["failure_reason"] or "评审需要补充材料或修复后重新验证。",
                 can_prepare_repair=True, can_add_evidence=True)
    return value


def remediation_of(task):
    value = db.task_extra(task).get("review_remediation") or {}
    return value if value.get("state") == "prepared" else {}


def _require_identity(value, payload):
    util.require(value is not None, "当前评审已变化或仍有活动请求，请刷新后处理", "conflict")
    util.require(all(payload.get(key) == value.get(field) for key, field in IDENTITY.items()),
                 "问题、报告或执行轮次已变化，请刷新后处理", "conflict")


def _snapshot(conn, task_id):
    return {"task": dict(db.require_task(conn, task_id)),
            "rounds": [dict(row) for row in conn.execute("SELECT * FROM execution_rounds WHERE task_id=? ORDER BY run_id", (task_id,))],
            "requests": [dict(row) for row in conn.execute("SELECT * FROM requests WHERE task_id=? ORDER BY rowid", (task_id,))],
            "events": [dict(row) for row in conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))]}


def _read(path, expected=None):
    try:
        util.require(not path.is_symlink() and path.is_file(), "处置材料缺失或不是普通文件", "conflict")
        with path.open("rb") as handle:
            raw = handle.read(constants.PLAN_INPUT_FILE_MAX_BYTES + 1)
        util.require(len(raw) <= constants.PLAN_INPUT_FILE_MAX_BYTES, "处置材料超过文件大小边界", "conflict")
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise util.KanpilotError("conflict", "处置材料不可读取或不是 UTF-8 文本") from exc
    sha = util.sha256_bytes(raw)
    util.require(expected is None or sha == expected, "原评审报告或计划内容已变化", "conflict")
    return raw, sha


def _remote(task, accepted=None):
    try:
        live = remote.get_task(remote.get_client(), task["task_id"])
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", str(exc)) from exc
    validate_remote(task, live, accepted=accepted)
    return live


def validate_remote(task, live, accepted=None):
    item = live.get("task") or {}
    allowed = {accepted} if accepted else set(constants.REVIEW_SLUGS)
    util.require(item.get("id") == task["task_id"] and item.get("projectId") == task["project_id"]
                 and live.get("requirements_sha256") == task["requirements_sha256"]
                 and item.get("status") in allowed,
                 "远端任务、项目、需求或评审状态已变化，请先重新核验", "remote_conflict")


def _files_and_execution(ctx, task, value):
    """Read trusted completed execution plus the registered review invocation."""
    from . import cmd_queue, headless_adapter
    row, detail = cmd_queue._require_execution_terminal(ctx, task, value["execution_run_id"])
    util.require(row["run_status"] == "finished" and (detail.get("terminal") or {}).get("outcome") == "completed",
                 "原执行尚无完成终态，不能准备评审修复", "conflict")
    source = headless_adapter.resolve_source(ctx.conn)
    directory = source.run_dir(task["task_id"], value["execution_run_id"])
    files = {}
    for name in ("job.json", "events.jsonl"):
        _raw, files[str(directory / name)] = _read(directory / name)
    job = json.loads(_read(directory / "job.json")[0])
    _execution_job_identity(ctx.conn, task, value, row, job, directory, files)
    meta, plan, path = planfile.load_plan(task["plan_path"], task["task_id"])
    util.require(util.sha256_bytes(plan) == value["plan_sha256"]
                 and all(meta.get(k) == task[k] for k in ("repository", "project_id", "requirements_sha256")),
                 "原计划文件或需求身份已变化", "conflict")
    files[str(path)] = value["plan_sha256"]
    report_path = util.inside(Path(task["review_report_path"]), constants.review_reports_root(), "review report")
    report, files[str(report_path)] = _read(report_path, value["report_sha256"])
    matches = []
    request = db.get_request(ctx.conn, value["review_request_id"])
    review_backend = db.request_payload(request).get("review_backend") or "pi"
    if review_backend == "codex_desktop":
        review_round = round_store.get_round(ctx.conn, task["task_id"], value["review_run_id"])
        util.require(review_round is not None and review_round["backend"] == "codex_desktop"
                     and review_round["run_status"] == "finished" and review_round["save_state"] == "active",
                     "正式 Desktop 评审轮次登记不符", "conflict")
        # Formal Desktop's writer uses the registered runs directory, not the
        # Plus/CLI invocation directory. Resolve exactly this claimed round.
        review_paths = [source.run_dir(task["task_id"], value["review_run_id"]) / "job.json"]
    else:
        root = constants.app_support() / "executor-sessions" / "review"
        review_paths = root.glob("*/job.json")
    for review_path in review_paths:
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if review.get("request_id") != value["review_request_id"]:
            continue
        if review_backend == "codex_desktop":
            util.require(review.get("backend") == "codex_desktop" and review.get("role") == "review",
                         "正式 Desktop 评审记录角色或后端不符", "conflict")
            util.require(isinstance(review.get("execution_fingerprint"), str) and bool(review["execution_fingerprint"])
                         and review.get("execution_worktree") == row["worktree_path"]
                         and (review.get("frozen") or {}).get("plan_sha256") == value["plan_sha256"],
                         "正式 Desktop 评审缺少原执行成果或计划的冻结身份", "conflict")
        util.require(review.get("task_id") == task["task_id"] and review.get("run_id") == value["review_run_id"]
                     and review.get("execution_run_id") == value["execution_run_id"]
                     and review.get("status") in ("finished", "failed") and review.get("conclusion") == "blocked"
                     and review.get("report_path") == str(report_path) and review.get("report_sha256") == value["report_sha256"],
                     "本机评审结算记录与当前阻塞身份不符", "conflict")
        _, files[str(review_path)] = _read(review_path)
        matches.append(review)
    registration = db.task_extra(task).get("registered_review_identity") or {}
    local_registration = (registration == {"request_id": value["review_request_id"], "run_id": value["review_run_id"],
        "execution_run_id": value["execution_run_id"], "report_sha256": value["report_sha256"], "plan_sha256": value["plan_sha256"]}
        and (db.request_payload(request).get("review_backend") or "pi") == "pi")
    util.require(len(matches) == 1 or (not matches and local_registration),
                 "原阻塞评审缺少唯一的本机结算证据", "conflict")
    path = Path(row["worktree_path"] or "")
    util.require(bool(row["worktree_path"]) and path.resolve() != Path(task["repository"]).resolve()
                 and git_delivery._common_dir(path) == git_delivery._common_dir(task["repository"])
                 and Path(git_delivery._value(["rev-parse", "--show-toplevel"], path)).resolve() == path.resolve(),
                 "原执行成果不是登记仓库的独立工作树", "conflict")
    frozen = review_snapshot.freeze(path)
    for review in matches:
        fingerprint = review.get("execution_fingerprint") or review.get("manual_snapshot_fingerprint")
        if fingerprint:
            util.require(review_snapshot.fingerprint(frozen) == fingerprint,
                         "原执行工作树与阻塞评审的冻结成果已不同，请先核验成果", "conflict")
        if review.get("execution_worktree"):
            util.require(Path(review["execution_worktree"]).resolve() == path.resolve(),
                         "阻塞评审的执行工作树登记不符", "conflict")
    if core.requires_branch_review(task):
        core.require_committed_review(task)
    return {"files": files, "plan": plan, "report": report, "source": path,
            "frozen": frozen, "source_sha256": review_snapshot.fingerprint(frozen)}


def _execution_job_identity(conn, task, value, row, job, directory, files):
    util.require(job.get("task_id") == task["task_id"] and job.get("run_id") == value["execution_run_id"]
                 and job.get("backend") == row["backend"] and job.get("status") == "finished",
                 "本机执行记录身份已变化", "conflict")
    if row["backend"] not in ("pi", "pi_desktop"):
        util.require(job.get("plan_sha256") == value["plan_sha256"]
                     and job.get("worktree_path") == row["worktree_path"],
                     "本机执行记录的计划或工作树身份不符", "conflict")
        return
    # Both real workers' _base_job omit the working directory. The immutable
    # core startup spec owns cwd/repository/plan_path; only Plus stores plan SHA
    # in its job. Bind that actual format to the registered round, not a made-up
    # common job schema, and freeze the spec along with the other evidence.
    raw, files[str(directory / "spec.json")] = _read(directory / "spec.json")
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        raise util.KanpilotError("conflict", "原执行启动记录不是有效 JSON") from exc
    config = util.jload(row["config_json"])
    util.require(isinstance(spec, dict) and type(spec.get("v")) is int and spec["v"] == 1
                 and spec.get("task_id") == task["task_id"] and spec.get("run_id") == value["execution_run_id"]
                 and isinstance(config, dict) and bool(config)
                 and (config.get("backend") == "pi_desktop" if row["backend"] == "pi_desktop"
                      else config.get("backend", "pi") == "pi")
                 and spec.get("config") == config and job.get("config") == config
                 and (spec.get("backend") == "pi_desktop" if row["backend"] == "pi_desktop"
                      else spec.get("backend", "pi") == "pi")
                 and spec.get("repository") == task["repository"] and spec.get("plan_path") == task["plan_path"]
                 and bool(row["worktree_path"]) and spec.get("cwd") == row["worktree_path"]
                 and spec.get("runs_dir") == str(directory),
                 "原执行启动记录与已登记的工作树、计划或后端配置不符", "conflict")
    for key, expected in (("plan_sha256", value["plan_sha256"]), ("worktree_path", row["worktree_path"])):
        util.require(key not in job or job[key] == expected,
                     "本机执行记录的计划或工作树身份不符", "conflict")
    util.require("plan_sha256" not in spec or spec["plan_sha256"] == value["plan_sha256"],
                 "原执行启动记录的计划指纹不符", "conflict")
    if row["backend"] == "pi_desktop":
        util.require(spec.get("plan_sha256") == value["plan_sha256"]
                     and job.get("plan_sha256") == value["plan_sha256"]
                     and bool(row["session_id"]) and job.get("session_id") == row["session_id"]
                     and isinstance(job.get("turn_id"), str) and bool(job["turn_id"]),
                     "原 Plus 执行的计划或会话身份不符", "conflict")
        util.require(isinstance(spec.get("request_id"), str) and bool(spec["request_id"]),
                     "原 Plus 执行启动记录缺少派发请求身份", "conflict")
    if spec.get("request_id"):
        request = db.get_request(conn, spec["request_id"])
        util.require(request is not None and request["task_id"] == task["task_id"]
                     and request["run_id"] == value["execution_run_id"]
                     and request["kind"] == "dispatch" and request["status"] == "succeeded",
                     "原执行启动记录与派发请求登记不符", "conflict")


def _recheck_files(prepared):
    for path, sha in prepared["files"].items():
        _read(Path(path), sha)
    review_snapshot.verify_source(prepared["source"], prepared["frozen"])


def _owned_workspace(ctx, task, value, prepared):
    delivery = core.delivery_of(task)
    if delivery.get("branch") and delivery.get("worktree_path"):
        util.require(all(delivery.get(key) for key in ("base_branch", "base_sha", "head_sha")),
                     "原任务分支缺少固定基线，未解除阻塞或覆盖成果，请先核验分支登记", "conflict")
        util.require(Path(delivery["worktree_path"]).resolve() == prepared["source"].resolve(),
                     "任务分支与原执行成果登记不一致", "conflict")
        git_delivery.verify_branch_worktree(dict(delivery, repository=task["repository"]),
                                           require_clean=False, expected_head=prepared["frozen"]["head"])
        return dict(delivery, workspace_version=1, head_sha=prepared["frozen"]["head"])
    util.require(git_delivery._value(["rev-parse", "--abbrev-ref", "HEAD"], prepared["source"]) == "HEAD",
                 "历史成果不是 detached 工作树，须先人工核验归属", "conflict")
    root = constants.worktrees_root(db.get_setting(ctx.conn, "worktrees_root") or None).expanduser().resolve()
    task_id = task["task_id"]
    git_delivery._local_task_id(task_id)
    key = "review_repair_preparing:" + task_id
    branch = constants.DELIVERY_BRANCH_PREFIX + task_id
    target = root / task_id / "branch"
    identity = {"mode": "manual", "workspace_version": 1, "branch": branch,
                "worktree_path": str(target), "base_branch": git_delivery._value(["rev-parse", "--abbrev-ref", "HEAD"], task["repository"]),
                "base_sha": prepared["frozen"]["head"], "head_sha": prepared["frozen"]["head"]}
    expected = {"repository": task["repository"], "resolution_id": value["id"],
                "source_sha256": prepared["source_sha256"], "delivery": identity}
    intent = db.get_setting(ctx.conn, key)
    if intent is None:
        refs = git_delivery._value(["for-each-ref", "--format=%(refname)",
                                   "refs/heads/" + branch, "refs/remotes/origin/" + branch], task["repository"])
        util.require(not refs and not os.path.lexists(target), "目标分支或工作树已存在且未登记，未覆盖成果", "conflict")
        with db.write_txn(ctx.conn):
            util.require(_snapshot(ctx.conn, task_id) == prepared["db_snapshot"], "处置准备期间任务身份已变化", "conflict")
            intent = db.get_setting(ctx.conn, key)
            if intent is None:
                db.set_setting(ctx.conn, key, expected)
                intent = expected
    util.require(intent == expected, "恢复中的工作区意图与当前评审不符", "conflict")
    if not os.path.lexists(target):
        refs = git_delivery._value(["for-each-ref", "--format=%(refname)", "refs/heads/" + branch], task["repository"])
        if refs:
            util.require(git_delivery._value(["rev-parse", branch], task["repository"]) == identity["head_sha"],
                         "恢复中的分支已变化", "conflict")
            target.parent.mkdir(parents=True, exist_ok=True)
            git_delivery._git(["worktree", "add", str(target), branch], task["repository"], 60)
        else:
            created = git_delivery.prepare_local_branch_worktree(task["repository"], task_id, worktrees_root=root,
                base_sha=identity["head_sha"], base_branch=identity["base_branch"])
            util.require(created == identity, "恢复工作区登记不符", "conflict")
    git_delivery.verify_branch_worktree(dict(identity, repository=task["repository"]), require_clean=False,
                                       expected_head=identity["head_sha"])
    review_snapshot.populate(target, prepared["frozen"], allow_branch=True, resume=True)
    review_snapshot.verify_source(prepared["source"], prepared["frozen"])
    return identity


def cmd_prepare_review_repair(ctx, payload):
    task = db.require_task(ctx.conn, payload["task_id"])
    existing = remediation_of(task)
    if existing and all(payload.get(key) == existing.get(field) for key, field in IDENTITY.items()):
        verify_remediation(ctx.conn, task)
        return {"ok": True, "task_id": task["task_id"], "deduplicated": True, "detail": "修复规划材料已准备，原成果保留。"}
    value = candidate(ctx.conn, task)
    _require_identity(value, payload)
    prepared = _files_and_execution(ctx, task, value)
    prepared["db_snapshot"] = _snapshot(ctx.conn, task["task_id"])
    live = _remote(task)
    util.require(_snapshot(ctx.conn, task["task_id"]) == prepared["db_snapshot"], "处置期间任务身份已变化", "conflict")
    delivery = _owned_workspace(ctx, task, value, prepared)
    _recheck_files(prepared)
    state = git_delivery.worktree_state_sha256(dict(delivery, repository=task["repository"]))
    # Keep immutable original plan/report even after the new plan is published.
    directory = constants.app_support() / "review-remediation" / task["task_id"] / value["id"]
    directory.mkdir(parents=True, exist_ok=True)
    frozen_files = {}
    for name, content in (("original-plan.md", prepared["plan"]), ("blocked-review.md", prepared["report"])):
        path = directory / name
        if os.path.lexists(path):
            _read(path, util.sha256_bytes(content))
        else:
            with path.open("xb") as handle:
                handle.write(content)
        frozen_files[name] = {"path": str(path), "sha256": util.sha256_bytes(content)}
    record = {**{key: value[key] for key in ("id", *IDENTITY.values(), "review_run_id")},
              "state": "prepared", "created_at": ctx.now_iso, "files": frozen_files,
              "repository": task["repository"], "project_id": task["project_id"],
              "requirements_sha256": task["requirements_sha256"], "remote_status": live["task"]["status"],
              "source_worktree": str(prepared["source"]), "source_fingerprint": prepared["source_sha256"],
              "execution_files": {path: sha for path, sha in prepared["files"].items()
                                  if Path(path).name in ("job.json", "events.jsonl", "spec.json")},
              "delivery": delivery, "worktree_state_sha256": state}
    _recheck_files(prepared)
    util.require(git_delivery.worktree_state_sha256(dict(delivery, repository=task["repository"])) == state,
                 "成果工作区在材料冻结期间已变化", "conflict")
    with db.write_txn(ctx.conn):
        util.require(_snapshot(ctx.conn, task["task_id"]) == prepared["db_snapshot"], "处置期间任务或轮次已变化", "conflict")
        fresh = db.require_task(ctx.conn, task["task_id"])
        _require_identity(candidate(ctx.conn, fresh), payload)
        from . import cmd_queue
        fresh = cmd_queue.clear_review_result_inline(ctx.conn, fresh, ctx.now_iso)
        core.update_automation(ctx.conn, fresh, ctx.now_iso, manual_takeover=True, plan_wanted=False)
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, **delivery)
        fresh = db.require_task(ctx.conn, task["task_id"])
        history = list(db.task_extra(fresh).get("review_history") or [])
        if not any(h.get("report_sha256") == value["report_sha256"] and h.get("execution_run_id") == value["execution_run_id"] for h in history):
            history.append(dict(value, conclusion="blocked", report_path=task["review_report_path"]))
        db.set_extra(ctx.conn, fresh, ctx.now_iso, review_remediation=record, review_history=history,
                     plan_execution_hold=True)
        core.transition_task(ctx.conn, db.require_task(ctx.conn, task["task_id"]), "needs_plan", ctx.now_iso, "user",
            detail={"via": "prepare-review-repair", "resolution_id": value["id"]},
            stage="plan", plan_kind="standard", auto_advance_paused=1, **core.clear_block_fields())
        db.set_setting(ctx.conn, "review_repair_preparing:" + task["task_id"], None)
        db.add_event(ctx.conn, ctx.now_iso, "review_repair_prepared", task_id=task["task_id"], actor="user", detail=record)
    return {"ok": True, "task_id": task["task_id"], "deduplicated": False,
            "detail": "原成果与评审材料已保留，请确认修复规划材料；尚未运行模型或执行。"}


def verify_remediation(conn, task, *, check_workspace=True):
    record = remediation_of(task)
    if not record:
        return None
    util.require(record.get("repository") == task["repository"] and record.get("project_id") == task["project_id"]
                 and record.get("requirements_sha256") == task["requirements_sha256"]
                 and record.get("delivery") == core.delivery_of(task), "修复处置登记或需求已变化", "conflict")
    row = round_store.get_round(conn, task["task_id"], record["execution_run_id"])
    util.require(row is not None and row["run_status"] == "finished" and row["save_state"] == "active"
                 and row["worktree_path"] == record["source_worktree"], "原成果轮次身份已变化", "conflict")
    for file in record["files"].values():
        _read(Path(file["path"]), file["sha256"])
    for path, sha in record.get("execution_files", {}).items():
        _read(Path(path), sha)
    from . import cmd_queue
    cmd_queue._require_execution_terminal(SimpleNamespace(conn=conn, now=util.now_dt(), now_iso=util.now_iso()),
                                          task, record["execution_run_id"])
    if check_workspace:
        actual = git_delivery.worktree_state_sha256(dict(core.delivery_of(task), repository=task["repository"]))
        util.require(actual == record["worktree_state_sha256"], "修复规划前原成果已变化，请重新核验", "conflict")
        frozen = review_snapshot.freeze(Path(record["source_worktree"]))
        util.require(review_snapshot.fingerprint(frozen) == record["source_fingerprint"], "原执行成果已变化", "conflict")
    return record


def prompt_block(manifest):
    value = manifest.get("review_remediation") or {}
    if not value:
        return ""
    return ("\n## 用户确认的评审阻塞修复规划\n\n这是同任务的普通修复规划，原成果已在登记工作树中完整保留。"
            "必须读取材料中的 original-plan.md 和 blocked-review.md 全文，继承原计划范围与验收要求，"
            "明确报告的阻塞原因、修复步骤和解除阻塞标准；保留原成果，不从主检出或空目录重新实现。"
            "不得把 blocked 改写成已确认缺陷或评审通过。输出 standard 计划，必要验证覆盖新修复和原验收缺口。"
            "本轮仅规划；执行仍需用户确认。\n")


def report_summary(raw):
    for paragraph in re.split(r"\n\s*\n", raw.decode("utf-8")):
        lines = [line.strip() for line in paragraph.splitlines()
                 if line.strip() and not line.lstrip().startswith(("#", "---", "```"))
                 and not re.match(r"^[-*\s`]*(task_id|request_id|run_id|execution_run_id|sha256|report_path)\s*[:=]", line.strip())]
        if lines:
            return "\n".join(lines)[:600]
    return "请查看完整评审报告，确认所需材料或修复步骤。"


def verify_plan_cycle(conn, task, payload):
    record = verify_remediation(conn, task)
    if not record:
        return
    from . import planner_inputs
    manifest = payload.get("manifest") or {}
    util.require(manifest.get("review_remediation") == record,
                 "规划请求未携带当前评审处置来源", "conflict")
    root = planner_inputs._inputs_root(conn) / payload["input_id"]
    for entry in manifest.get("files") or []:
        if entry.get("source") != "review_remediation":
            continue
        content, _reason = planner_inputs._read_worktree_material(str(root), entry.get("path", ""),
                                                                constants.PLAN_INPUT_FILE_MAX_BYTES)
        util.require(content is not None and util.sha256_bytes(content) == entry.get("sha256"),
                     "原计划或阻塞报告的规划副本已变化", "conflict")
    _remote(task, accepted=record["remote_status"])


def _evidence_files(paths):
    from . import planner_inputs
    util.require(isinstance(paths, list), "local_paths 必须为数组", "bad_request")
    values, total = [], 0
    for raw in paths:
        util.require(isinstance(raw, str) and raw.strip(), "补充证据路径无效", "bad_request")
        path = Path(raw).expanduser().absolute()
        content, reason = planner_inputs._read_worktree_material(str(path.parent), path.name,
            max(0, constants.PLAN_INPUT_TOTAL_MAX_BYTES - total))
        util.require(content is not None, "补充证据文件不安全或不可读取：" + str(reason), "validation_failed")
        total += len(content)
        values.append({"path": str(path), "name": path.name, "content": content,
                       "sha256": util.sha256_bytes(content), "size": len(content)})
    return values


def cmd_supplement_review(ctx, payload):
    text = payload.get("evidence_text") or ""
    util.require(isinstance(text, str), "evidence_text 必须为文字", "bad_request")
    util.require(len(text.encode("utf-8")) <= constants.PLAN_INPUT_FILE_MAX_BYTES, "文字证据过大", "bad_request")
    paths = payload.get("local_paths") or []
    util.require(isinstance(paths, list) and all(isinstance(path, str) and path.strip() for path in paths),
                 "补充证据路径无效", "bad_request")
    util.require(bool(text.strip()) or bool(paths), "请填写说明或选择本地证据文件", "bad_request")
    task = db.require_task(ctx.conn, payload["task_id"])
    submission = {"identity": {key: payload[key] for key in IDENTITY}, "evidence_text": text,
                  "paths": [str(Path(path).expanduser().absolute()) for path in paths]}
    operation = payload["operation_id"]
    util.require(bool(operation.strip()) and len(operation) <= 128, "operation_id 无效", "bad_request")
    key = "review_supplement:" + task["task_id"] + ":" + util.sha256_bytes(operation.encode())
    previous = db.get_setting(ctx.conn, key)
    if previous:
        util.require(previous.get("submission") == submission, "同一操作标识已有不同证据", "conflict")
        for entry in previous.get("files", []):
            path = util.inside(Path(entry["path"]), constants.app_support() / "review-evidence", "review evidence")
            util.require(path.is_file() and not path.is_symlink() and util.sha256_file(path) == entry["sha256"],
                         "已登记的冻结证据不可核验", "conflict")
        request = db.get_request(ctx.conn, previous["request_id"])
        util.require(request is not None and request["task_id"] == task["task_id"], "补充证据请求不存在", "conflict")
        return {"ok": True, "request_id": request["request_id"], "deduplicated": True}
    values = _evidence_files(paths)
    canonical = {"identity": submission["identity"], "evidence_text": text,
                 "files": [{key: value[key] for key in ("path", "name", "sha256", "size")} for value in values]}
    digest = util.sha256_bytes(util.jdump(canonical).encode())
    value = candidate(ctx.conn, task)
    _require_identity(value, payload)
    prepared = _files_and_execution(ctx, task, value)
    prepared["db_snapshot"] = _snapshot(ctx.conn, task["task_id"])
    _remote(task)
    _recheck_files(prepared)
    directory = constants.app_support() / "review-evidence" / task["task_id"] / digest
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for index, item in enumerate(values):
        path = directory / (str(index) + Path(item["name"]).suffix)
        if os.path.lexists(path):
            util.require(path.is_file() and not path.is_symlink() and util.sha256_file(path) == item["sha256"],
                         "已有证据材料与本次提交不符", "conflict")
        else:
            with path.open("xb") as handle:
                handle.write(item["content"])
        records.append({"path": str(path), "name": item["name"], "sha256": item["sha256"], "size": item["size"]})
    record = {**value, "evidence_text": text, "files": records, "sha256": digest,
              "operation_id": operation, "created_at": ctx.now_iso,
              "provenance": "用户提供，待评审核验；不替代执行验证证据"}
    _recheck_files(prepared)
    with db.write_txn(ctx.conn):
        existing = db.get_setting(ctx.conn, key)
        if existing:
            util.require(existing["submission"] == submission, "同一操作标识已有不同证据", "conflict")
            return {"ok": True, "request_id": existing["request_id"], "deduplicated": True}
        util.require(_snapshot(ctx.conn, task["task_id"]) == prepared["db_snapshot"], "证据准备期间任务身份已变化", "conflict")
        fresh = db.require_task(ctx.conn, task["task_id"])
        _require_identity(candidate(ctx.conn, fresh), payload)
        from . import cmd_queue
        fresh = cmd_queue.clear_review_result_inline(ctx.conn, fresh, ctx.now_iso)
        history = list(db.task_extra(fresh).get("review_history") or [])
        if not any(h.get("report_sha256") == value["report_sha256"] and h.get("execution_run_id") == value["execution_run_id"] for h in history):
            history.append(dict(value, conclusion="blocked", report_path=task["review_report_path"]))
        db.set_extra(ctx.conn, fresh, ctx.now_iso, review_supplements=record, review_history=history)
        core.transition_task(ctx.conn, db.require_task(ctx.conn, task["task_id"]), "in_review", ctx.now_iso, "user",
                             detail={"via": "supplement-review", "resolution_id": value["id"]}, stage="review", auto_advance_paused=0, **core.clear_block_fields())
        request, _ = db.create_request(ctx.conn, ctx.now_iso, "review", "review:" + task["task_id"],
                                      task_id=task["task_id"], requested_by="user", payload={"review_supplement_sha256": digest})
        db.set_setting(ctx.conn, key, {"sha256": digest, "request_id": request["request_id"],
                                      "submission": submission, "files": records})
        db.add_event(ctx.conn, ctx.now_iso, "review_supplemented", task_id=task["task_id"], request_id=request["request_id"], actor="user", detail=record)
    return {"ok": True, "request_id": request["request_id"], "deduplicated": False}


def collect_evidence(conn, task, execution_run_id, materials):
    raw = task if "extra_json" in task else dict(task, extra_json=util.jdump(task.get("extra") or {}))
    record = db.task_extra(raw).get("review_supplements") or {}
    if not record or record.get("execution_run_id") != execution_run_id or record.get("plan_sha256") != task.get("plan_sha256"):
        return {}
    paths = {}
    note = materials / "user-review-evidence.md"
    note.write_text("# 用户补充，待评审核验\n\n这些材料未自动验证，不替代执行验证记录，不构成通过结论。\n\n"
                    + record.get("evidence_text", "") + "\n", encoding="utf-8")
    paths[note.name] = str(note)
    for index, entry in enumerate(record.get("files") or []):
        source = util.inside(Path(entry["path"]), constants.app_support() / "review-evidence", "review evidence")
        util.require(not source.is_symlink() and source.is_file() and util.sha256_file(source) == entry["sha256"],
                     "登记的用户补充证据已变化", "review_blocked")
        target = materials / ("user-evidence-" + str(index) + Path(entry["name"]).suffix)
        shutil.copyfile(source, target)
        util.require(util.sha256_file(target) == entry["sha256"], "用户证据在复制期间已变化", "review_blocked")
        paths[target.name] = str(target)
    manifest = materials / "user-review-evidence.json"
    manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    paths[manifest.name] = str(manifest)
    return paths


def collect_remediation(task, materials):
    raw = task if "extra_json" in task else dict(task, extra_json=util.jdump(task.get("extra") or {}))
    record = db.task_extra(raw).get("review_remediation") or {}
    if record.get("state") not in ("prepared", "executing"):
        return {}
    util.require(record.get("repository") == task.get("repository")
                 and record.get("project_id") == task.get("project_id"),
                 "原评审处置来源与任务身份不符", "review_blocked")
    files = {}
    for name in ("original-plan.md", "blocked-review.md"):
        entry = record.get("files", {}).get(name) or {}
        source = util.inside(Path(entry.get("path") or ""), constants.app_support() / "review-remediation", "review remediation")
        content, _sha = _read(source, entry.get("sha256"))
        target = materials / name
        target.write_bytes(content)
        files[name] = str(target)
    return files
