"""Explicit discard of one unpushed one-shot AI action (KP-MG02, protocol "放弃一次性处理").

A one-shot ``check_failure`` repair can keep failing review until the repair
quota is spent. The PR was already reviewed as ``pass`` at an earlier head and
still points there, while the task is pinned to local commits that were never
pushed and has no way forward. This CLI-only command gives that task a supported
exit: the unpushed commits are kept under a preservation ref, the task branch
returns to the reviewed head, the task identity returns to the execution that
produced that head, and one fresh review is queued.

Boundaries: nothing is pushed or force-pushed, no review report or history entry
is deleted (discarded reviews are only marked), and the repair quota spent by
the discarded rounds is given back. The engine verdict of the restored
execution is re-derived through the existing ``engine-verdict`` command; it is
not needed by the review itself, so a failed derivation is reported and the same
call can simply be repeated.
"""
from . import (core, db, gh_client, git_delivery, round_store, runtime_operations, task_ai_action, util)

KIND = "check_failure"
STATE = "discarded"
STATE_TITLE = task_ai_action.STATE_TITLES[STATE]
REF_ROOT = "refs/kaneopilot/discarded/"
SCOPE_PREFIX = "task-ai-action-discard:"
_DISCARDABLE = ("needs_followup",)
# Per-action receipts of the discarded chain; everything else stays untouched.
_DROPPED_EXTRA = ("fix_request", "review_conclusion", "review_registered_at", "registered_review_identity",
                  "verification_override", "plan_execution_hold", "execution_retry", "engine_verdict")


def _entry(task, action_id):
    for item in db.task_extra(task).get("ai_action_history") or []:
        if isinstance(item, dict) and item.get("action_id") == action_id:
            return dict(item)
    return None


def _scope(task_id):
    return SCOPE_PREFIX + task_id


def _operation(action_id):
    return "discard:" + action_id


def preserved_ref(task_id, action_id):
    return REF_ROOT + task_id + "/" + action_id[:8]


def _quota_after_discard(conn, task, entry):
    """Rounds started after this action began are discarded together with it."""
    from . import review_repair
    started = None
    for row in conn.execute("SELECT id, detail_json FROM events WHERE task_id=? AND kind='task_ai_action_started' "
                            "ORDER BY id", (task["task_id"],)):
        if (util.jload(row["detail_json"]) or {}).get("action_id") == entry["action_id"]:
            started = row["id"]
    util.require(started is not None, "找不到该 AI 处理的启动记录，无法归还修复次数", "conflict")
    rounds = []
    for row in conn.execute("SELECT detail_json FROM events WHERE task_id=? AND kind='fix_round_started' AND id>? "
                            "ORDER BY id", (task["task_id"], started)):
        value = (util.jload(row["detail_json"]) or {}).get("fix_round")
        util.require(type(value) is int and value >= 1, "修复轮次记录不完整，无法归还修复次数", "conflict")
        rounds.append(value)
    return max(0, min(rounds) - 1) if rounds else review_repair._round_count(task)


def _dispatch_sent_at(conn, task_id, run_id, fallback):
    for row in conn.execute("SELECT at, detail_json FROM events WHERE task_id=? AND kind='dispatch_sent' "
                            "ORDER BY id DESC", (task_id,)):
        if (util.jload(row["detail_json"]) or {}).get("run_id") == run_id:
            return row["at"]
    return fallback


def _inspect(ctx, task, entry, payload, *, resumed):
    """Read-only preconditions. Any failure leaves the database, refs and worktree untouched."""
    conn = ctx.conn
    task_id = task["task_id"]
    head, restore, pr_head = (payload["expected_head_sha"], payload["expected_restore_head_sha"],
                              payload["expected_pr_head_sha"])
    util.require(entry.get("kind") == KIND, "首版只支持放弃检查修复", "conflict")
    util.require(entry.get("state") in _DISCARDABLE, "该 AI 处理尚未以「需要后续处理」结束，不能放弃", "conflict")
    util.require(db.task_is_active(task) and not db.task_auto_paused(task)
                 and task["status"] == "in_review" and task["stage"] == "review",
                 "仅处于评审阶段、未暂停的任务可放弃 AI 处理", "conflict")
    util.require(not core.automation_of(task),
                 "自动处理任务的放弃另行设计，当前仅支持手动任务", "conflict")
    util.require(not db.paused(conn, "review") and not db.paused(conn, "delivery"), "评审或交付阶段已暂停", "conflict")
    util.require(not conn.execute("SELECT 1 FROM requests WHERE task_id=? AND status IN ('queued','claimed')",
                                  (task_id,)).fetchone(), "当前任务仍有未结束请求，请稍后重试", "conflict")
    util.require(not conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? AND status IN ('prepared','unknown')",
                                  ("pr-merge:" + task_id,)).fetchone(),
                 "已有 PR 合并请求结果待确认，请先重新核验", "conflict")
    extra = db.task_extra(task)
    util.require(extra.get("review_conclusion") != "pass", "当前评审已通过，无需放弃", "conflict")
    delivery = core.delivery_of(task)
    util.require(delivery.get("mode") == "pr" and type(delivery.get("pr_number")) is int
                 and delivery.get("worktree_path") and delivery.get("branch"),
                 "任务不处于 PR 交付阶段", "conflict")
    util.require(delivery.get("head_sha") == head, "本地提交已变化，请刷新后重试", "conflict")
    util.require(entry.get("head_sha") == restore and pr_head == restore and delivery.get("pushed_sha") == restore
                 and not delivery.get("merge_head_sha"),
                 "放弃目标不是该处理冻结时已推送、已评审的提交", "conflict")
    source_run = entry.get("source_execution_run_id")
    commits = [row for row in delivery.get("commits") or [] if isinstance(row, dict)]
    shas = [row.get("sha") for row in commits]
    util.require(source_run and restore in shas and shas[-1] == head and head != restore
                 and shas.count(restore) == 1,
                 "交付提交记录与放弃范围不符", "conflict")
    cut = shas.index(restore)
    kept, dropped = commits[:cut + 1], commits[cut + 1:]
    util.require(kept[-1].get("execution_run_id") == source_run
                 and all(row.get("execution_run_id") != source_run for row in dropped),
                 "被放弃的提交不是该处理之后产生的", "conflict")
    for row in dropped:
        later = round_store.get_round(conn, task_id, row.get("execution_run_id"))
        util.require(later is not None and later["created_at"] >= entry["created_at"],
                     "被放弃的提交早于该 AI 处理，不能放弃", "conflict")
    source_round = round_store.get_round(conn, task_id, source_run)
    util.require(source_round is not None and source_round["run_status"] == "finished"
                 and source_round["save_state"] == "active"
                 and source_round["worktree_path"] == delivery["worktree_path"],
                 "原执行轮次已不可用，无法恢复", "conflict")
    history = [item for item in extra.get("review_history") or []]
    dropped_runs = {row["execution_run_id"] for row in dropped}
    passed = [index for index, item in enumerate(history)
              if isinstance(item, dict) and item.get("conclusion") == "pass"
              and item.get("execution_run_id") == source_run and item.get("head_sha") == restore]
    util.require(len(passed) == 1, "找不到原执行精确对应的评审通过记录", "conflict")
    later_reviews = history[passed[0] + 1:]
    util.require(later_reviews and all(isinstance(item, dict) and item.get("conclusion") != "pass"
                                       and item.get("execution_run_id") in dropped_runs
                                       for item in later_reviews),
                 "评审记录与放弃范围不符", "conflict")
    worktree = delivery["worktree_path"]
    try:
        # A resumed call may find the branch already rolled back by the first attempt.
        current = git_delivery._value(["rev-parse", "HEAD"], worktree) if resumed else head
        git_delivery.verify_branch_worktree(dict(delivery, repository=task["repository"]), require_clean=True,
                                            expected_head=current if current in (head, restore) else head)
        util.require(git_delivery._merge_marker(worktree, "MERGE_HEAD") is None, "工作树有未完成合并", "conflict")
        git_delivery._git(["merge-base", "--is-ancestor", restore, head], worktree, 30)
        lines = git_delivery._value(["rev-list", "--parents", "--reverse", restore + ".." + head],
                                    worktree, 30).splitlines()
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", exc.message) from exc
    chain = [line.split() for line in lines]
    util.require(chain and all(len(item) == 2 for item in chain) and chain[0][1] == restore
                 and all(chain[i][1] == chain[i - 1][0] for i in range(1, len(chain)))
                 and [item[0] for item in chain] == [row["sha"] for row in dropped],
                 "本地提交不是只属于该处理的线性单父提交链", "conflict")
    repo = git_delivery.require_github_origin(task["repository"])
    try:
        pr = gh_client.pr_view(repo, delivery["pr_number"])
    except gh_client.GhError as exc:
        raise util.KanpilotError("remote_unavailable", exc.message) from exc
    util.require(pr.get("state") == "OPEN" and pr.get("url") == f"https://github.com/{repo}/pull/{delivery['pr_number']}"
                 and pr.get("headRefName") == delivery["branch"]
                 and pr.get("baseRefName") == delivery.get("base_branch")
                 and pr.get("isCrossRepository") is False and pr.get("headRefOid") == pr_head,
                 "PR 身份、分支、仓库或 head 已变化，不能放弃", "conflict")
    return dict(head=head, restore=restore, kept=kept, dropped=dropped, review_index=passed[0],
                source_run=source_run, source_round=dict(source_round), worktree=worktree,
                quota=_quota_after_discard(conn, task, entry),
                sent_at=_dispatch_sent_at(conn, task_id, source_run, source_round["started_at"] or task["sent_at"]))


_DELIVERY_IDENTITY = ("mode", "branch", "worktree_path", "base_branch", "base_sha", "head_sha", "pushed_sha",
                      "pr_number", "merge_head_sha", "commits")


def _snapshot(task):
    """What the plan was computed from. Delivery sync observations may move meanwhile; identity may not."""
    extra, delivery = db.task_extra(task), core.delivery_of(task)
    return (task["status"], task["stage"], task["run_id"], task["review_key"], extra.get("review_conclusion"),
            util.jdump({key: delivery.get(key) for key in _DELIVERY_IDENTITY}),
            util.jdump(extra.get("review_history") or []), util.jdump(extra.get("ai_action_history") or []),
            util.jdump(extra.get("fix_request") or {}))


def _preserve(plan, task_id, action_id):
    """Keep the discarded head reachable before anything is moved."""
    ref, worktree = preserved_ref(task_id, action_id), plan["worktree"]
    try:
        current = (git_delivery._value(["rev-parse", "--verify", ref + "^{commit}"], worktree)
                   if _has_ref(worktree, ref) else None)
        util.require(current in (None, plan["head"]), "保全引用已指向别的提交，不能覆盖", "conflict")
        if current is None:
            git_delivery._git(["update-ref", ref, plan["head"]], worktree, 10)
        util.require(git_delivery._value(["rev-parse", "--verify", ref + "^{commit}"], worktree) == plan["head"],
                     "保全引用未能解析到被放弃的提交", "conflict")
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", exc.message) from exc
    return ref


def _has_ref(worktree, ref):
    try:
        git_delivery._git(["show-ref", "--verify", "--quiet", ref], worktree, 10)
        return True
    except git_delivery.GitDeliveryError:
        return False


def _rollback(plan, delivery):
    """Move the task branch back inside its own clean worktree; the old head is already preserved."""
    worktree = plan["worktree"]
    try:
        head = git_delivery._value(["rev-parse", "HEAD"], worktree)
        util.require(head in (plan["head"], plan["restore"]), "任务分支已被改动，保留现场", "conflict")
        if head != plan["restore"]:
            git_delivery._git(["reset", "--hard", plan["restore"]], worktree, 60)
        git_delivery.verify_branch_worktree(delivery, require_clean=True, expected_head=plan["restore"])
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("conflict", exc.message) from exc


def _apply(ctx, task, entry, plan, payload, ref):
    conn, now = ctx.conn, ctx.now_iso
    task_id, action_id = task["task_id"], entry["action_id"]
    extra = db.task_extra(task)
    delivery = core.delivery_of(task)
    delivery.update(head_sha=plan["restore"], commits=plan["kept"])
    extra["delivery"] = delivery
    marker = {"action_id": action_id, "at": now}
    reviews = list(extra.get("review_history") or [])
    for index in range(plan["review_index"] + 1, len(reviews)):
        reviews[index] = dict(reviews[index], discarded=marker)
    extra["review_history"] = reviews
    extra["ai_action_history"] = [
        dict(item, state=STATE, state_title=STATE_TITLE, discarded_at=now, discarded_head_sha=plan["head"],
             restore_head_sha=plan["restore"], preserved_ref=ref, discard_note=payload.get("note"))
        if isinstance(item, dict) and item.get("action_id") == action_id else item
        for item in extra.get("ai_action_history") or []]
    for key in _DROPPED_EXTRA:
        extra.pop(key, None)
    extra["review_repair_round"] = plan["quota"]
    source = plan["source_round"]
    core.transition_task(
        conn, task, "in_review", now, "user",
        detail={"via": "discard_task_ai_action", "action_id": action_id, "restore_head_sha": plan["restore"],
                "discarded_head_sha": plan["head"]},
        stage="review", run_id=plan["source_run"], session_id=source["session_id"],
        session_marker=task_id + "/" + plan["source_run"], session_title=source["session_title"],
        sent=1, send_state="sent", sent_at=plan["sent_at"], lock_retained=0,
        review_report_path=None, review_report_sha256=None, review_key=None,
        last_progress="已放弃 AI 处理，回到已评审提交重新评审", extra_json=extra, **core.clear_block_fields())
    request, _ = db.create_request(conn, now, "review", "review:" + task_id, task_id=task_id, requested_by="user")
    db.add_event(conn, now, "task_ai_action_discarded", task_id=task_id, request_id=request["request_id"], actor="user",
                 detail={"action_id": action_id, "kind": entry.get("kind"), "discarded_head_sha": plan["head"],
                         "restore_head_sha": plan["restore"], "preserved_ref": ref, "fix_round": plan["quota"],
                         "dropped_commits": [row.get("sha") for row in plan["dropped"]], "note": payload.get("note")})
    return request


def rederive_verdict(ctx, task_id, action_id):
    """Ask the existing ``engine-verdict`` for the restored run and check what it wrote."""
    from .engine import commands, verdict
    task = db.require_task(ctx.conn, task_id)
    try:
        commands.cmd_engine_verdict(ctx, {"task_id": task_id, "expected_execution_run_id": task["run_id"],
                                          "expected_plan_sha256": task["plan_sha256"],
                                          "operation_id": "discard-verdict:" + action_id})
        task = db.require_task(ctx.conn, task_id)
        receipt = db.task_extra(task).get("engine_verdict") or {}
        if receipt.get("verification_ok") is not True:
            return {"state": "unconfirmed", "detail": str(receipt.get("detail") or "验证回执未确认通过")}
        if not verdict.valid_committed_receipt(ctx.conn, task, receipt):
            return {"state": "unconfirmed", "detail": "重新算出的验证回执与已提交源码或执行证据不符"}
    except (util.KanpilotError, OSError, git_delivery.GitDeliveryError) as exc:
        return {"state": "failed", "detail": util.redact(getattr(exc, "message", str(exc)))}
    except Exception as exc:  # noqa: BLE001 - the discard is already committed: report, never turn it into an error
        return {"state": "failed", "detail": util.redact("%s: %s" % (type(exc).__name__, exc))}
    return {"state": "confirmed", "detail": None}


def _verdict_current(task):
    receipt = db.task_extra(task).get("engine_verdict") or {}
    return (receipt.get("verification_ok") is True and receipt.get("execution_run_id") == task["run_id"]
            and receipt.get("plan_sha256") == task["plan_sha256"])


def _result(task, entry, request, verdict, *, already):
    delivery = core.delivery_of(task)
    return {"ok": True, "task_id": task["task_id"], "action_id": entry["action_id"], "already_discarded": already,
            "restore_head_sha": delivery.get("head_sha"), "discarded_head_sha": entry.get("discarded_head_sha"),
            "preserved_ref": entry.get("preserved_ref"), "fix_round": db.task_extra(task).get("review_repair_round", 0),
            "request_id": request["request_id"] if request is not None else None, "verdict": verdict}


def _resume(ctx, task, entry, payload):
    """The discard itself is committed; only an unfinished verdict derivation can still be pending."""
    delivery = core.delivery_of(task)
    util.require(task["run_id"] == entry.get("source_execution_run_id")
                 and delivery.get("head_sha") == entry.get("restore_head_sha") == payload["expected_restore_head_sha"]
                 and entry.get("discarded_head_sha") == payload["expected_head_sha"],
                 "该 AI 处理已放弃，但任务已离开恢复后的提交；请核对任务状态", "conflict")
    verdict = ({"state": "confirmed", "detail": None} if _verdict_current(task)
               else rederive_verdict(ctx, task["task_id"], entry["action_id"]))
    request = ctx.conn.execute("SELECT * FROM requests WHERE task_id=? AND kind='review' ORDER BY rowid DESC LIMIT 1",
                               (task["task_id"],)).fetchone()
    return _result(db.require_task(ctx.conn, task["task_id"]), entry, request, verdict, already=True)


def cmd_discard_task_ai_action(ctx, payload):
    util.require(payload.get("confirm") is True, "需要明确确认放弃本次 AI 处理", "bad_request")
    conn, task_id, action_id = ctx.conn, payload["task_id"], payload["action_id"]
    task = db.require_task(conn, task_id)
    entry = _entry(task, action_id)
    util.require(entry is not None, "没有这次 AI 处理的记录", "conflict")
    if entry.get("state") == STATE:
        return _resume(ctx, task, entry, payload)
    scope, operation = _scope(task_id), _operation(action_id)
    identity = {key: payload[key] for key in ("task_id", "action_id", "expected_head_sha",
                                              "expected_restore_head_sha", "expected_pr_head_sha")}
    existing = db.get_runtime_operation(conn, scope, operation)
    plan = _inspect(ctx, task, entry, payload, resumed=existing is not None)
    snapshot = _snapshot(task)
    with db.write_txn(conn):
        util.require(_snapshot(db.require_task(conn, task_id)) == snapshot, "准备期间任务状态已变化，请刷新后重试", "conflict")
        receipt = runtime_operations.begin_operation(conn, scope, operation, "git", "discard_ai_action", identity,
                                                     ctx.now_iso)
        util.require(receipt["status"] in ("prepared", "unknown"), "放弃操作已有不同结果，保留现场等待核对", "conflict")
        if existing is None:
            runtime_operations.mark_operation_unknown(conn, scope, operation, ctx.now_iso)
    delivery = dict(core.delivery_of(task), repository=task["repository"])
    ref = _preserve(plan, task_id, action_id)
    _rollback(plan, delivery)
    with db.write_txn(conn):
        fresh = db.require_task(conn, task_id)
        util.require(_snapshot(fresh) == snapshot, "回退期间任务状态已变化，保留现场等待核对", "conflict")
        request = _apply(ctx, fresh, entry, plan, payload, ref)
        runtime_operations.complete_operation(conn, scope, operation, result={
            "restore_head_sha": plan["restore"], "discarded_head_sha": plan["head"], "preserved_ref": ref})
    done = db.require_task(conn, task_id)
    return _result(done, _entry(done, action_id), request, rederive_verdict(ctx, task_id, action_id), already=False)
