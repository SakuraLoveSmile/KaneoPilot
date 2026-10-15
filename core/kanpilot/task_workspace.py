"""Task branch registration and frozen planning workspace identity."""
import os
import re
from pathlib import Path

from . import constants, core, db, git_delivery, util


def payload_fields(delivery):
    fields = {"plan_cwd": delivery.get("worktree_path"),
              "delivery_mode": delivery.get("mode"),
              "delivery_head_sha": delivery.get("head_sha")}
    if delivery.get("workspace_version") == 1:
        fields.update(workspace_version=1, delivery_branch=delivery.get("branch"),
                      delivery_base_sha=delivery.get("base_sha"))
    return fields


def prepare_for_submission(conn, task_id, repository, task=None):
    """Outside a write transaction, prepare first-time local task registration.

    A historical request or execution keeps its frozen, unversioned contract.
    PR delivery and manual takeover already own their branch and are reused.
    """
    delivery = core.delivery_of(task) if task is not None else {}
    fingerprint = util.jdump(delivery)
    if delivery.get("workspace_version") == 1:
        try:
            git_delivery.verify_branch_worktree(dict(delivery, repository=repository),
                                               require_clean=False,
                                               expected_head=delivery.get("head_sha"))
            state = git_delivery.worktree_state_sha256(dict(delivery, repository=repository))
        except git_delivery.GitDeliveryError as exc:
            raise util.KanpilotError("worktree_invalid", exc.message) from exc
        return {"delivery": delivery, "fingerprint": fingerprint, "state_sha256": state}
    if delivery.get("branch") or (task is not None and
            (task["plan_path"] or task["run_id"] or conn.execute(
                "SELECT 1 FROM requests WHERE task_id=? LIMIT 1", (task_id,)).fetchone())):
        return None
    base_sha, base_branch = None, None
    if task is not None and task["plan_kind"] == "review_fix" and task["review_source_task_id"]:
        source = db.require_task(conn, task["review_source_task_id"])
        util.require(Path(source["repository"]).expanduser().resolve() == Path(repository).expanduser().resolve(),
                     "修复任务与原评审仓库不符", "worktree_invalid")
        reviews = db.task_extra(source).get("review_history") or []
        matching = [review for review in reviews if isinstance(review, dict)
                    and review.get("review_key") == task["review_key"] and review.get("head_sha")]
        util.require(bool(matching), "原评审没有固定提交，须先完成固定分支评审", "worktree_invalid")
        base_sha = matching[-1]["head_sha"]
        base_branch = core.delivery_of(source).get("branch")
        util.require(bool(base_branch), "原评审任务缺少分支登记", "worktree_invalid")
    root = constants.worktrees_root(db.get_setting(conn, "worktrees_root") or None)
    intent_key = "task_workspace_preparing:" + task_id
    intent = db.get_setting(conn, intent_key)
    try:
        if intent is None:
            git_delivery._local_task_id(task_id)
            branch = constants.DELIVERY_BRANCH_PREFIX + task_id
            path = root.expanduser().resolve() / task_id / "branch"
            refs = git_delivery._value(["for-each-ref", "--format=%(refname)",
                                       "refs/heads/" + branch, "refs/remotes/origin/" + branch], repository)
            util.require(not refs and not os.path.lexists(path),
                         "任务分支或工作树路径已存在且未登记归属，未覆盖现有成果", "worktree_invalid")
            identity = {
                "mode": "manual", "workspace_version": 1,
                "branch": branch, "worktree_path": str(path),
                "base_branch": base_branch or git_delivery._value(["rev-parse", "--abbrev-ref", "HEAD"], repository),
                "base_sha": base_sha or git_delivery._value(["rev-parse", "--verify", "HEAD^{commit}"], repository)}
            identity["head_sha"] = identity["base_sha"]
            intent = {"repository": str(Path(repository).expanduser().resolve()), "delivery": identity}
            # Record ownership before filesystem mutation. Keep this on any later
            # submission rejection or crash so recovery can identify its tree.
            with db.write_txn(conn):
                existing_intent = db.get_setting(conn, intent_key)
                if existing_intent is None:
                    db.set_setting(conn, intent_key, intent)
                else:
                    intent = existing_intent
        util.require(isinstance(intent, dict)
                     and intent.get("repository") == str(Path(repository).expanduser().resolve()),
                     "准备中的任务工作区仓库不符", "worktree_invalid")
        delivery = intent.get("delivery")
        util.require(isinstance(delivery, dict)
                     and delivery.get("branch") == constants.DELIVERY_BRANCH_PREFIX + task_id
                     and delivery.get("worktree_path") == str(root.expanduser().resolve() / task_id / "branch")
                     and delivery.get("base_sha") == delivery.get("head_sha")
                     and (base_sha is None or delivery.get("base_sha") == base_sha),
                     "准备中的任务工作区身份不符", "worktree_invalid")
        if os.path.lexists(delivery["worktree_path"]):
            git_delivery.verify_branch_worktree(dict(delivery, repository=repository),
                                               require_clean=False, expected_head=delivery["head_sha"])
        else:
            created = git_delivery.prepare_local_branch_worktree(
                repository, task_id, worktrees_root=root,
                base_sha=delivery["base_sha"], base_branch=delivery["base_branch"])
            util.require(created == delivery, "创建的任务工作区与准备登记不符", "worktree_invalid")
        state = git_delivery.worktree_state_sha256(dict(delivery, repository=repository))
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("worktree_invalid", exc.message) from exc
    return {"delivery": delivery, "fingerprint": fingerprint, "state_sha256": state,
            "intent_key": intent_key}


def verify_plan_workspace(task, payload):
    """Return registered read-only project root; None means historical contract.

    New requests never accept a missing/changed identity or fall back to primary.
    Dirty content is allowed only if its frozen planning state still matches.
    """
    if "workspace_version" not in payload:
        return None
    delivery = core.delivery_of(task)
    util.require(payload.get("workspace_version") == 1
                 and delivery.get("workspace_version") == 1,
                 "规划工作区版本与登记不符", "worktree_invalid")
    expected = payload_fields(delivery)
    util.require(all(payload.get(key) == value and value is not None
                     for key, value in expected.items()),
                 "规划工作树、分支或提交与任务登记不符", "worktree_invalid")
    try:
        identity = dict(delivery, repository=task["repository"])
        git_delivery.verify_branch_worktree(identity, require_clean=False,
                                           expected_head=payload["delivery_head_sha"])
        state = payload.get("worktree_state_sha256")
        util.require(bool(state) and git_delivery.worktree_state_sha256(identity) == state,
                     "规划排队后任务工作树内容已变化，请重新准备规划", "worktree_invalid")
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("worktree_invalid", exc.message) from exc
    return Path(delivery["worktree_path"])


def prepare_dirty_plan_snapshot(task, payload, *, worktrees_root, run_id, conn=None, request_id=None):
    """Freeze task inputs into a leased slot; never create a per-run directory."""
    from . import review_snapshot, worktree_pool
    source = verify_plan_workspace(task, payload)
    util.require(source is not None and conn is not None and request_id,
                 "规划快照需要核心请求与槽位租约", "worktree_invalid")
    frozen = review_snapshot.freeze(source)
    util.require(frozen["head"] == payload["delivery_head_sha"],
                 "规划快照 HEAD 与登记不符", "worktree_invalid")
    verify_plan_workspace(task, payload)
    lease = acquire_pool(conn, task["repository"], stage="plan", run_id=run_id,
                         request_id=request_id, sha=frozen["head"])
    worktree_pool.register_snapshot(conn, **pool_identity(lease), snapshot=frozen)
    review_snapshot.verify_source(source, frozen)
    verify_plan_workspace(task, payload)
    return Path(lease["path"])


def pool_identity(lease):
    return {key: lease[key] for key in ("slot_id", "holder_id", "lease_token")}


def validate_pool(conn, lease):
    from . import worktree_pool
    row = worktree_pool.validate_lease(conn, **pool_identity(lease))
    util.require(row["path"] == lease["path"] and row["state"] == "leased",
                 "槽位路径或租约状态已变化", "conflict")
    return row


def pool_owner(conn, job, *, state, pid=None):
    from . import worktree_pool
    lease = job.get("pool_lease")
    if lease:
        validate_pool(conn, lease)
        worktree_pool.mark_owner(conn, **pool_identity(lease), state=state, pid=pid,
                                 session_id=job.get("session_id"), turn_id=job.get("turn_id"))


def preserve_pool_artifacts(job, evidence_dir):
    """Copy allowed slot evidence before release or returned-owner recovery."""
    import shutil
    import stat
    root = Path(job["pool_lease"]["path"])
    material = root / ".kaneopilot-plan-material"
    if material.exists():
        for entry in (material, *material.rglob("*")):
            mode = entry.lstat().st_mode
            util.require(stat.S_ISDIR(mode) or stat.S_ISREG(mode),
                         "规划材料包含不安全类型，未释放槽位", "worktree_invalid")
        destination = Path(evidence_dir) / "plan-materials"
        shutil.copytree(material, destination, dirs_exist_ok=True)
        if job.get("material_dir"):
            previous = Path(job["material_dir"])
            # A prior copy can succeed before release is blocked or crashes.
            relative = (previous.relative_to(destination) if previous.is_relative_to(destination)
                        else previous.relative_to(material))
            job["material_dir"] = str(destination / relative)
    plans = root / ".pi" / "plan"
    if plans.is_dir() and not plans.is_symlink() and not plans.parent.is_symlink():
        target = Path(evidence_dir) / "plus-plan-evidence"
        for source in plans.glob("*.md"):
            if source.is_file() and not source.is_symlink():
                target.mkdir(parents=True, exist_ok=True)
                (target / source.name).write_bytes(source.read_bytes())


def release_pool(db_path, job, evidence_dir, *, unexposed=False):
    """Persist allowed plan artifacts before returning a proven stopped owner."""
    from . import worktree_pool
    lease = job.get("pool_lease")
    if not lease or job.get("pool_released"):
        return
    conn = db.connect(db_path)
    try:
        try:
            current = validate_pool(conn, lease)
        except util.KanpilotError as exc:
            job["pool_release_error"] = exc.message
            # A changed generation is untouched; a damaged checkout owned by
            # this generation is retained as quarantine, never cleaned.
            try:
                worktree_pool.quarantine(conn, **pool_identity(lease), reason=exc.message)
            except util.KanpilotError:
                pass
            return
        request = db.get_request(conn, job.get("request_id"))
        terminal = request is not None and request["status"] in ("succeeded", "failed", "cancelled")
        owner_request = conn.execute("SELECT * FROM requests WHERE run_id=? AND stage=? LIMIT 1",
                                     (current["holder_id"], current["holder_stage"])).fetchone()
        owner_payload = db.request_payload(owner_request) if owner_request is not None else {}
        backend_key = "planner_backend" if current["holder_stage"] == "plan" else "review_backend"
        desktop_owner = (owner_payload.get(backend_key) == "codex_desktop"
                         or job.get("backend") == "codex_desktop")
        never_exposed = (unexposed and job.get("send_state") == "not_sent"
                         and current["owner_state"] == "not_started")
        stopped = (never_exposed if unexposed else
                   not desktop_owner and terminal
                   and (job.get("owner_stopped") is True or job.get("send_state") == "not_sent"))
        returned = (not unexposed and terminal and job.get("backend") == "codex_desktop"
                    and worktree_pool.consumer_returned(conn, current))
        if not stopped and not returned:
            if unexposed:
                return
            state = ("unknown" if desktop_owner else
                     "not_started" if job.get("send_state") == "not_sent" else
                     "stopped" if job.get("owner_stopped") is True else "unknown")
            pool_owner(conn, job, state=state)
            return
        preserve_pool_artifacts(job, evidence_dir)
        result = worktree_pool.release(conn, **pool_identity(lease), owner_stopped=stopped,
                                       only_not_started=unexposed)
        job["pool_release_state"] = result["state"]
        job["pool_released"] = result["state"] == "free"
    except (OSError, ValueError, util.KanpilotError) as exc:
        job["pool_release_error"] = str(exc)
        try:
            worktree_pool.quarantine(conn, **pool_identity(lease), reason=str(exc))
        except util.KanpilotError:
            pass
    finally:
        conn.close()


def pool_wait(conn, request_id, run_id, waiting):
    """Capacity waiting remains a retryable request, never a business failure."""
    now = util.now_iso()
    with db.write_txn(conn):
        request = db.get_request(conn, request_id)
        util.require(request is not None and request["status"] == "claimed"
                     and request["run_id"] == run_id, "等待请求身份已变化", "conflict")
        payload = db.request_payload(request)
        payload["worktree_pool_wait"] = dict(waiting, waiting_at=now)
        task = db.get_task(conn, request["task_id"]) if request["task_id"] else None
        db.set_setting(conn, "worktree_pool_waiting:" + request_id,
                       dict(request_id=request_id, stage=request["stage"], repo_key=waiting.get("repo_key"),
                            repository=task["repository"] if task else None, since=now))
        receipts = db.get_setting(conn, "desktop_claim_receipts", {}) or {}
        for receipt in receipts.values():
            response = receipt.get("response") or {}
            if response.get("request_id") == request_id and response.get("run_id") == run_id:
                receipt["response"] = {"claimed": False, "reason": "worktree_pool_waiting", "request_id": request_id}
        db.set_setting(conn, "desktop_claim_receipts", receipts)
        db.update_request(conn, request_id, now, status="queued", run_id=None,
                          claimed_by=None, claimed_at=None, heartbeat_at=None,
                          lease_expires_at=None, not_before=util.plus_seconds(20), payload_json=payload)
        db.close_attempt(conn, request_id, run_id, now, "waiting", send_state="not_sent",
                         evidence=waiting, note="worktree_pool_full")
        if request["kind"] == "plan" and request["task_id"]:
            task = db.require_task(conn, request["task_id"])
            if task["status"] == "planning":
                db.update_task(conn, task["task_id"], now, status=payload.get("task_status_at_claim") or "needs_plan")
        db.add_event(conn, now, "worktree_pool_waiting", task_id=request["task_id"],
                     request_id=request_id, detail=dict(waiting, run_id=run_id))


def acquire_pool(conn, repository, *, stage, run_id, request_id, sha):
    from . import worktree_pool
    lease = worktree_pool.acquire(conn, repository, stage=stage, holder_id=run_id, sha=sha)
    if lease["state"] == "waiting":
        pool_wait(conn, request_id, run_id, lease)
        raise util.KanpilotError("worktree_pool_waiting", "仓库槽位已满，请求保持排队")
    try:
        with db.write_txn(conn):
            request = db.get_request(conn, request_id)
            util.require(request is not None and request["status"] == "claimed" and request["run_id"] == run_id,
                         "槽位请求身份已变化", "conflict")
            db.set_setting(conn, "worktree_pool_waiting:" + request_id, None)
            db.merge_request_payload(conn, request_id, util.now_iso(), pool_lease=lease,
                                     worktree_pool_wait=None)
    except Exception:
        # Git preparation happened outside SQL. Losing this request before the
        # binding cannot strand a checkout that was never exposed to a reader.
        worktree_pool.release(conn, **pool_identity(lease), owner_stopped=True,
                              only_not_started=True)
        raise
    return lease


def prepare_plan_pool(conn, db_path, task, payload, job, job_path):
    """Choose the frozen planning root shared by all real planner backends."""
    try:
        return _prepare_plan_pool(conn, db_path, task, payload, job, job_path)
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError(exc.code, exc.message) from exc


def _prepare_plan_pool(conn, db_path, task, payload, job, job_path):
    from . import review_snapshot, worktree_pool
    source = verify_plan_workspace(task, payload)
    repository = task["repository"]
    manual = (payload.get("delivery_mode") == "manual" and payload.get("plan_cwd")
              and payload.get("workspace_version") != 1)
    job.update(request_id=job["request_id"], send_state="not_sent")
    if manual:
        delivery = core.delivery_of(task)
        util.require(core.automation_of(task).get("manual_takeover") is True
                     and delivery.get("mode") == "manual"
                     and Path(payload["plan_cwd"]).resolve() == Path(delivery.get("worktree_path") or "").resolve(),
                     "手动接管规划工作树与任务登记不符", "worktree_invalid")
        git_delivery.verify_branch_worktree(dict(delivery, repository=repository),
                                           require_clean=False, expected_head=None)
        job["worktree_path"] = delivery["worktree_path"]
        return Path(delivery["worktree_path"])
    snapshot = review_snapshot.freeze(source) if source is not None and git_delivery.worktree_changes(source) else None
    sha = (snapshot["head"] if snapshot else payload.get("delivery_head_sha")
           or git_delivery._value(["rev-parse", "--verify", "HEAD^{commit}"], repository))
    lease = acquire_pool(conn, repository, stage="plan", run_id=job["run_id"],
                         request_id=job["request_id"], sha=sha)
    job.update(pool_lease=lease, pool_db_path=str(db_path), worktree_path=lease["path"])
    Path(job_path).parent.mkdir(parents=True, exist_ok=True)
    Path(job_path).write_text(util.jdump(job) + "\n", encoding="utf-8")
    if snapshot is not None:
        worktree_pool.register_snapshot(conn, **pool_identity(lease), snapshot=snapshot)
        review_snapshot.verify_source(source, snapshot)
    verify_plan_workspace(task, payload)
    return Path(lease["path"])


def heartbeat_pool(db_path, job):
    if not job.get("pool_lease"):
        return
    from . import worktree_pool
    conn = db.connect(db_path)
    try:
        worktree_pool.heartbeat(conn, **pool_identity(job["pool_lease"]))
    finally:
        conn.close()
