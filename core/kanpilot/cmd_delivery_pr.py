"""Leased PR publication, merge observation and optional project auto merge."""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import (bridge, constants, core, db, delivery_contract, gh_client,
               git_delivery, pr_auto_merge, remote, runtime_operations, util)


class _UnknownDelivery(Exception):
    pass


def _uncertain(exc):
    return (getattr(exc, "operation_dispatched", False)
            or isinstance(exc, bridge.BridgeError)
            or isinstance(exc, gh_client.GhError) and exc.kind in ("timeout", "transient")
            or isinstance(exc, git_delivery.GitDeliveryError) and exc.code == "push_failed")


def _receipt_data(row):
    return util.jload(row["result_json"]) or {}


def _receipt_intent(row):
    data = _receipt_data(row)
    return data.get("intent", data)


def _select_attempt(ctx, scope, root_id, intent, claim, kind):
    """Run under the writer lock: uncertain writes outrank new retry intent."""
    rows = ctx.conn.execute("SELECT * FROM runtime_operations WHERE scope_id=? ORDER BY created_at,operation_id",
                            (scope,)).fetchall()
    family = [row for row in rows if row["operation_id"] == root_id
              or _receipt_intent(row).get("retry_root") == root_id]
    pending = [row for row in rows if row["status"] in ("prepared", "unknown")]
    if pending:
        if len(pending) != 1 or pending[0] not in family:
            raise _UnknownDelivery(kind)
        selected = pending[0]
    else:
        accepted = [row for row in family if row["status"] == "accepted"]
        if accepted:
            selected = accepted[-1]
        else:
            root = next((row for row in family if row["operation_id"] == root_id), None)
            if root is None:
                return root_id, intent
            if root["status"] != "rejected" or _receipt_data(root).get("claim") == claim:
                selected = root
            else:
                retry_id = runtime_operations.compute_request_sha256({"retry_root": root_id, "claim": claim})
                selected = next((row for row in family if row["operation_id"] == retry_id), None)
                if selected is None:
                    return retry_id, dict(intent, retry_root=root_id, retry_of=root_id, retry_claim=claim)
    original = _receipt_intent(selected)
    # Legacy accepted receipts contain only the observation; their stable root
    # identity and begin_operation request hash still verify the arguments.
    if selected["operation_id"] == root_id and "arguments" not in original:
        return root_id, intent
    base = {key: value for key, value in original.items()
            if key not in ("retry_root", "retry_of", "retry_claim")}
    util.require(base == intent, "交付操作已冻结不同参数，不能复用回执", "conflict")
    return selected["operation_id"], original


def _raise_rejected(data):
    rejection = data.get("rejection") or {}
    message = rejection.get("message") or "交付操作已明确拒绝，等待下一合法领取轮次重试"
    if rejection.get("family") == "git":
        raise git_delivery.GitDeliveryError(rejection.get("code") or "delivery_commit_failed", message)
    raise gh_client.GhError(rejection.get("code") or "rejected", message)


def _write_once(ctx, task, delivery, kind, arguments, boundary, send, observe):
    """Persist intent before sending; a lost response only permits reads."""
    intent = {"task_id": task["task_id"], "execution_run_id": task["run_id"],
              "plan_sha256": task["plan_sha256"], "head_sha": delivery["head_sha"],
              "review_key": task["review_key"], "review_report_sha256": task["review_report_sha256"],
              "arguments": arguments}
    scope = "delivery-" + kind + ":" + task["task_id"]
    effect = dict(intent)
    # PR title/body are frozen arguments, not a second permission to create
    # the same PR. begin_operation rejects changed bytes under this identity.
    if kind == "github_pr_create":
        effect["arguments"] = {key: arguments[key] for key in ("repo", "base", "head")}
    root_id = runtime_operations.compute_request_sha256(effect)
    contract = (delivery.get("stage_contracts") or {}).get("publish") or {}
    claim = {key: contract.get(key) for key in ("request_id", "run_id")}
    util.require(all(claim.values()), "交付操作缺少当前领取身份", "validation_failed")
    boundary()
    with db.write_txn(ctx.conn):
        boundary()
        op_id, intent = _select_attempt(ctx, scope, root_id, intent, claim, kind)
        prepared = runtime_operations.begin_operation(ctx.conn, scope, op_id, "core_delivery", kind, intent,
                                                     now=ctx.now_iso)
        if prepared["status"] == "prepared":
            db.update_runtime_operation(ctx.conn, scope, op_id, ctx.now_iso,
                                        result_json=util.jdump({"intent": intent, "claim": claim}))
    slot = runtime_operations.reserve_downstream(ctx.conn, scope, op_id, "core_delivery", kind, intent)
    boundary()
    if ctx.conn.execute("SELECT 1 FROM runtime_operations WHERE scope_id=? "
            "AND status IN ('prepared','unknown') AND operation_id!=?", (scope, op_id)).fetchone():
        raise _UnknownDelivery(kind)
    if not slot["send"]:
        if slot["status"] == "rejected":
            _raise_rejected(slot["result"] or {})
        try:
            observed = observe(None)
        except (git_delivery.GitDeliveryError, gh_client.GhError, bridge.BridgeError) as exc:
            boundary()
            if _uncertain(exc):
                raise _UnknownDelivery(kind) from exc
            raise
        boundary()
        if observed is None:
            raise _UnknownDelivery(kind)
    else:
        try:
            sent = send()
        except (git_delivery.GitDeliveryError, gh_client.GhError, bridge.BridgeError) as exc:
            # The downstream may have accepted the request. Leave its receipt
            # unknown even when this process did not receive a usable response.
            boundary()
            if _uncertain(exc):
                raise _UnknownDelivery(kind) from exc
            with db.write_txn(ctx.conn):
                boundary()
                rejection = {"family": "git" if isinstance(exc, git_delivery.GitDeliveryError) else "github",
                             "code": exc.code if isinstance(exc, git_delivery.GitDeliveryError) else exc.kind,
                             "message": gh_client.redact(exc.message)}
                runtime_operations.fail_operation(ctx.conn, scope, op_id, now=ctx.now_iso,
                    result={"intent": intent, "claim": claim, "rejection": rejection})
            raise
        boundary()
        try:
            observed = observe(sent)
        except (git_delivery.GitDeliveryError, gh_client.GhError, bridge.BridgeError) as exc:
            boundary()
            if _uncertain(exc):
                raise _UnknownDelivery(kind) from exc
            raise
        boundary()
        if observed is None:
            raise _UnknownDelivery(kind)
    with db.write_txn(ctx.conn):
        boundary()
        runtime_operations.complete_operation(ctx.conn, scope, op_id,
            result={"intent": intent, "claim": claim, "observation": observed}, now=ctx.now_iso)
    return observed


def _reconcile_observed(ctx, task, delivery, kind, arguments, observed, boundary):
    """An ordinary identity-checked read can settle a prior lost response."""
    scope = "delivery-" + kind + ":" + task["task_id"]
    rows = ctx.conn.execute("SELECT * FROM runtime_operations WHERE scope_id=? "
        "AND status IN ('prepared','unknown')", (scope,)).fetchall()
    for row in rows:
        intent = _receipt_intent(row)
        original = intent.get("arguments") or {}
        if (row["harness_id"] == "core_delivery" and row["tool"] == kind
                and intent.get("task_id") == task["task_id"]
                and intent.get("head_sha") == delivery["head_sha"]
                and all(original.get(key) == value for key, value in arguments.items())):
            with db.write_txn(ctx.conn):
                boundary()
                runtime_operations.complete_operation(ctx.conn, scope, row["operation_id"],
                    result={"intent": intent, "claim": _receipt_data(row).get("claim"),
                            "observation": observed}, now=ctx.now_iso)


def _observed_push(git_input, repo):
    target = git_delivery._push_target(git_input["worktree_path"], repo)
    ref = "refs/heads/" + git_input["branch"]
    rows = git_delivery._value(["ls-remote", "--heads", target, ref], git_input["worktree_path"], 60).splitlines()
    if rows == [git_input["head_sha"] + "\t" + ref]:
        return {"pushed_sha": git_input["head_sha"]}
    if rows:
        raise git_delivery.GitDeliveryError("branch_diverged", "远端任务分支与冻结提交不一致")
    return None


def _with_contract(ctx, payload, operation, run):
    task, delivery = _snapshot(ctx, payload, publish=operation == "publish")
    contract, task, delivery = delivery_contract.begin(ctx, payload, task, delivery, operation)
    result = run(ctx, payload, task, delivery, contract)
    envelope = delivery_contract.verified_result(contract, task, payload, delivery, result)
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task["task_id"])
        current = core.delivery_of(fresh)
        util.require((current.get("stage_contracts") or {}).get(operation) == contract,
                     "交付阶段契约已被替换", "conflict")
        expected = dict(current, pr_number=delivery.get("pr_number"))
        delivery_contract.validate_current(contract, fresh, payload, expected, operation)
        core.require_stage_identity(ctx.conn, payload, fresh, util.now_dt(), stages=("delivery",))
        results = dict(current.get("stage_results") or {})
        results[operation] = envelope
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, stage_results=results)
    return dict(result, stage_contract=contract, stage_result=envelope)


def _public_text(value):
    value = gh_client.redact(value)
    # PR text is public: local file locations and host names have no useful role.
    value = re.sub(r"(?<![A-Za-z0-9])(?:~?/|[A-Za-z]:\\)[^\s`\"'<>]+", "[本机路径]", value)
    hostname = os.uname().nodename
    if hostname:
        value = value.replace(hostname, "[本机]")
    return value


def build_pr_body(task, delivery, commit_titles=None):
    extra = db.task_extra(task)
    titles = commit_titles or {}
    commits = ["- " + item["sha"][:12] + " " + _public_text(titles.get(item["sha"], task["title"]))
               for item in delivery.get("commits") or []]
    commands = (extra.get("plan_verify") or {}).get("commands") or []
    return ("任务：" + _public_text(task["title"]) + "\n\n"
            + "task_id: " + task["task_id"] + "\n"
            + "修复轮数：" + str(core.automation_of(task).get("fix_round", 0)) + "\n\n"
            + "提交：\n" + "\n".join(commits) + "\n\n"
            + "verify 命令：\n" + "\n".join("- `" + _public_text(c) + "`" for c in commands)
            + "\n\n由 KaneoPilot 自动创建\n")


def _identity(ctx, payload):
    for key in ("task_id", "request_id", "run_id"):
        util.require(isinstance(payload.get(key), str) and payload[key].strip(),
                     "缺少 " + key, "bad_request")
    task = db.require_task(ctx.conn, payload["task_id"])
    core.require_stage_identity(ctx.conn, payload, task, util.now_dt(), stages=("delivery",))
    util.require(task["status"] == "in_review" and core.is_pr_delivery(task),
                 "任务不处于 PR 交付阶段", "conflict")
    util.require(not core.automation_of(task).get("manual_takeover", False),
                 "任务已转为人工处理", "conflict")
    extra, delivery = db.task_extra(task), core.delivery_of(task)
    from . import review_delivery
    if db.request_payload(db.get_request(ctx.conn, payload["request_id"])).get("manual_review_delivery_id"):
        util.require(review_delivery.allowed(ctx.conn, task, db.get_request(ctx.conn, payload["request_id"])),
                     "手动继续交付的授权、评审或请求身份已变化", "conflict")
    history = extra.get("review_history") or []
    review = history[-1] if history and isinstance(history[-1], dict) else {}
    util.require(extra.get("review_conclusion") == "pass" and review.get("conclusion") == "pass"
                 and review.get("execution_run_id") == task["run_id"]
                 and review.get("head_sha") == delivery.get("head_sha")
                 and review_delivery.review_round_matches(task, review)
                 and review.get("review_key") and review["review_key"] == task["review_key"]
                 and review.get("report_path") and review["report_path"] == task["review_report_path"]
                 and review.get("report_sha256") and review["report_sha256"] == task["review_report_sha256"]
                 and any(c.get("execution_run_id") == task["run_id"]
                         and c.get("sha") == delivery.get("head_sha") for c in delivery.get("commits") or []),
                 "PR 交付需要当前执行轮次及提交的 Review pass 登记", "validation_failed")
    return task


def _verify_review_report(task):
    report = util.inside(Path(task["review_report_path"]), constants.review_reports_root(), "report_path")
    util.require(report.is_file() and util.sha256_file(report) == task["review_report_sha256"],
                 "Review 报告缺失或已变化", "validation_failed")


def _snapshot(ctx, payload, *, publish):
    with db.write_txn(ctx.conn):
        task = _identity(ctx, payload)
        delivery = core.delivery_of(task)
        keys = ("branch", "head_sha", "base_branch", "worktree_path") if publish else ("pr_number",)
        util.require(all(delivery.get(k) for k in keys), "交付登记不完整", "validation_failed")
        return task, delivery


def _fresh(ctx, payload, task, delivery):
    fresh = db.require_task(ctx.conn, payload["task_id"])
    util.require(fresh["run_id"] == task["run_id"] and fresh["repository"] == task["repository"]
                 and fresh["plan_sha256"] == task["plan_sha256"]
                 and all(fresh[key] == task[key] for key in
                         ("review_key", "review_report_path", "review_report_sha256",
                          "project_id", "requirements_sha256"))
                 and core.delivery_of(fresh) == delivery,
                 "交付期间执行轮次或交付登记已变更", "conflict")
    return _identity(ctx, payload)


def _failure(ctx, payload, task, delivery, failure):
    with db.write_txn(ctx.conn):
        fresh = _fresh(ctx, payload, task, delivery)
        message = gh_client.redact(failure.message)
        code = None
        probe_timeout = False
        if isinstance(failure, git_delivery.GitDeliveryError):
            if failure.code == "branch_diverged":
                code = "branch_diverged"
            elif failure.code in ("git_probe_timeout", "git_probe_failed"):
                probe_timeout = failure.code == "git_probe_timeout"
            elif failure.code in ("origin_missing", "origin_not_github", "git_unavailable"):
                code = "gh_unavailable"
            elif failure.code != "push_failed":
                code = "delivery_commit_failed"
        elif failure.kind == "auth":
            code = "gh_not_logged_in"   # ladder 2 environmental
        elif failure.kind == "missing":
            code = "gh_missing"          # ladder 2 environmental
        elif failure.kind == "timeout":
            code = "gh_timeout"          # ladder 2 environmental
        elif failure.kind != "transient":
            code = "gh_unavailable"
        failures = int(delivery.get("publish_failures") or 0)
        if code is None:
            failures += 1
            core.update_delivery(ctx.conn, fresh, ctx.now_iso, publish_failures=failures,
                                 next_sync_at=util.plus_seconds(300, ctx.now))
            if failures >= 3:
                code = "gh_timeout" if probe_timeout else "gh_unavailable"
        if code:
            core.block_task(ctx.conn, fresh, ctx.now_iso, message, code,
                            "kaneo-delivery", stage="delivery")
            if code == "pr_closed":
                db.add_event(ctx.conn, ctx.now_iso, "pr_closed", task_id=task["task_id"],
                             request_id=payload["request_id"], actor="kaneo-delivery")
            return {"outcome": "blocked", "failure_code": code, "message": message}
        return {"outcome": "retry", "publish_failures": failures, "message": message}


def _closed(ctx, payload, task, delivery, pr=None):
    with db.write_txn(ctx.conn):
        fresh = _fresh(ctx, payload, task, delivery)
        if pr:
            core.update_delivery(ctx.conn, fresh, ctx.now_iso, pr_state="CLOSED", last_sync_at=ctx.now_iso)
        core.block_task(ctx.conn, fresh, ctx.now_iso, "PR 已关闭且未合并", "pr_closed",
                        "kaneo-delivery", stage="delivery")
        db.add_event(ctx.conn, ctx.now_iso, "pr_closed", task_id=task["task_id"],
                     request_id=payload["request_id"], actor="kaneo-delivery")
    return {"outcome": "blocked", "failure_code": "pr_closed"}


def _verify_pr_identity(pr, repo, delivery, pushed_sha):
    # CLI filters are discovery only. Reuse and merge observation require the
    # actual PR to describe this repository, branch pair and pushed commit.
    gh_client._pr(pr)
    path = urlsplit(pr["url"]).path
    expected_path = "/" + repo + "/pull/" + str(pr["number"])
    if (path.lower() != expected_path.lower()
            or (delivery.get("pr_number") is not None and pr["number"] != delivery["pr_number"])
            or pr["isCrossRepository"]
            or pr["headRefName"] != delivery.get("branch")
            or pr["baseRefName"] != delivery.get("base_branch")
            or not pushed_sha or pr["headRefOid"] != pushed_sha):
        raise git_delivery.GitDeliveryError("branch_diverged", "PR 身份、分支或提交与交付快照不一致")


def _repository(task, delivery):
    repo = git_delivery.require_github_origin(task["repository"])
    if delivery.get("pr_url"):
        url = urlsplit(delivery["pr_url"])
        expected = "/" + repo + "/pull/" + str(delivery.get("pr_number"))
        if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
                or url.path.lower() != expected.lower()):
            raise git_delivery.GitDeliveryError("branch_diverged", "origin 与已登记 PR 仓库不一致")
    return repo


def cmd_delivery_publish(ctx, payload) -> dict:
    return _with_contract(ctx, payload, "publish", _publish)


def _publish(ctx, payload, task, delivery, contract):
    def boundary():
        fresh = _fresh(ctx, payload, task, delivery)
        _verify_review_report(fresh)
        _fresh(ctx, payload, task, delivery)
    try:
        boundary()
        git_input = dict(delivery, repository=task["repository"])
        repo = _repository(task, delivery)
        boundary()
        pushed = _write_once(ctx, task, delivery, "git_push",
            {"repo": repo, "branch": delivery["branch"], "sha": delivery["head_sha"]}, boundary,
            lambda: git_delivery.push_branch(git_input, expected_repo=repo, before_push=boundary),
            lambda _: _observed_push(git_input, repo))
        boundary()
        prs = gh_client.pr_list(repo, delivery["branch"])
        boundary()
        if delivery.get("pr_number") is not None:
            pr = next((p for p in prs if p.get("number") == delivery["pr_number"]), None)
            if pr is None:
                raise git_delivery.GitDeliveryError("branch_diverged", "已登记 PR 与当前候选不一致")
        elif prs:
            pr = next((p for p in prs if p["state"] == "OPEN"), None)
            pr = pr or next((p for p in prs if p["state"] == "MERGED"), prs[0])
        else:
            titles = {}
            for item in delivery.get("commits") or []:
                sha = item.get("sha", "")
                util.require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha),
                             "交付提交 SHA 无效", "validation_failed")
                boundary()
                titles[sha] = git_delivery._value(["show", "-s", "--format=%s", sha], delivery["worktree_path"])
            title = _public_text("[Kaneo] " + " ".join(str(task["title"] or "").split()))[:100]
            boundary()
            body = build_pr_body(task, delivery, titles)
            arguments = {"repo": repo, "base": delivery["base_branch"], "head": delivery["branch"],
                         "title": title, "body": body}
            def observe_pr(sent):
                # gh_client.pr_create returns a structured pr_view, not the
                # create command's textual acceptance response.
                candidates = [sent] if isinstance(sent, dict) else gh_client.pr_list(repo, delivery["branch"])
                boundary()
                if not candidates:
                    return None
                candidate = next((p for p in candidates if p.get("state") == "OPEN"), candidates[0])
                _verify_pr_identity(candidate, repo, delivery, pushed["pushed_sha"])
                return candidate
            pr = _write_once(ctx, task, delivery, "github_pr_create", arguments, boundary,
                lambda: gh_client.pr_create(repo, base=arguments["base"], head=arguments["head"],
                                            title=title, body=body), observe_pr)
        boundary()
        _verify_pr_identity(pr, repo, delivery, pushed["pushed_sha"])
        _reconcile_observed(ctx, task, delivery, "github_pr_create",
            {"repo": repo, "base": delivery["base_branch"], "head": delivery["branch"]}, pr, boundary)
        if pr["state"] == "CLOSED":
            return _closed(ctx, payload, task, delivery, pr)
        boundary()
        git_delivery.verify_branch_worktree(git_input, require_clean=True, expected_head=delivery["head_sha"])
        with db.write_txn(ctx.conn):
            _fresh(ctx, payload, task, delivery)
        boundary()
        client = remote.get_client()
        action_id = delivery.get("build_failure_action_id")
        marker = ("[kaneopilot:delivery:" + task["task_id"]
                  + (":build:" + action_id + ":pr:" + str(pr["number"]) if action_id else "") + "]")
        boundary()
        comment = remote.find_marker_comment(client, task["task_id"], marker)
        content = marker + " 已创建 PR：" + pr["url"]
        def verify_comment(value):
            if value is not None and action_id and (not value.get("id") or value.get("content") != content):
                raise gh_client.GhError("rejected", "构建修复 PR 回写评论与冻结 PR 地址不符")
            return value
        verify_comment(comment)
        def observe_comment(_):
            return verify_comment(remote.find_marker_comment(client, task["task_id"], marker))
        if not comment:
            boundary()
            _write_once(ctx, task, delivery, "kaneo_comment", {"content": content}, boundary,
                lambda: remote.post_comment(client, task["task_id"], content),
                observe_comment)
        else:
            _reconcile_observed(ctx, task, delivery, "kaneo_comment", {"content": content}, comment, boundary)
    except _UnknownDelivery as exc:
        boundary()
        return {"outcome": "waiting", "operation_state": "unknown", "failure_code": "delivery_unknown",
                "message": "交付操作 " + str(exc) + " 结果未知，保留回执并只读对账，不重复发送"}
    except (git_delivery.GitDeliveryError, gh_client.GhError) as exc:
        return _failure(ctx, payload, task, delivery, exc)
    except bridge.BridgeError as exc:
        return _failure(ctx, payload, task, delivery, gh_client.GhError("transient", str(exc)))
    boundary()
    with db.write_txn(ctx.conn):
        fresh = _fresh(ctx, payload, task, delivery)
        envelope = delivery_contract.verified_result(contract, fresh, payload, delivery,
            {"outcome": "published", "pr_number": pr["number"], "pr_url": pr["url"]})
        results = dict(delivery.get("stage_results") or {}, publish=envelope)
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, pushed_sha=pushed["pushed_sha"],
                             pr_number=pr["number"], pr_url=pr["url"], pr_state=pr["state"],
                             kaneo_comment_at=ctx.now_iso, publish_failures=0,
                             stage_results=results,
                             next_sync_at=util.plus_seconds(300, ctx.now))
        for kind in ("delivery_pushed", "pr_opened", "kaneo_comment_posted"):
            # Local duplicate invocation shouldn't duplicate successful event records.
            if ((kind == "delivery_pushed" and delivery.get("pushed_sha") == pushed["pushed_sha"])
                    or (kind == "pr_opened" and delivery.get("pr_number") == pr["number"])
                    or (kind == "kaneo_comment_posted" and delivery.get("kaneo_comment_at"))):
                continue
            db.add_event(ctx.conn, ctx.now_iso, kind, task_id=task["task_id"],
                         request_id=payload["request_id"], actor="kaneo-delivery",
                         detail={"sha": pushed["pushed_sha"], "pr_number": pr["number"], "pr_url": pr["url"]})
    return {"outcome": "published", "pr_number": pr["number"], "pr_url": pr["url"]}


def cmd_delivery_sync(ctx, payload) -> dict:
    return _with_contract(ctx, payload, "sync", _sync)


def _sync(ctx, payload, task, delivery, contract):
    from . import release_delivery
    policy = pr_auto_merge.snapshot(ctx.conn, task)
    def boundary():
        fresh = _fresh(ctx, payload, task, delivery)
        _verify_review_report(fresh)
        if policy:
            pr_auto_merge.check_policy(ctx.conn, fresh, policy)
        return _fresh(ctx, payload, task, delivery)
    try:
        boundary()
        client = remote.get_client()
        boundary()
        live = remote.get_task(client, task["task_id"])
        boundary()
        remote_done = (live.get("task") or {}).get("status") == "done"
        if remote_done and release_delivery.completion_gate(ctx.conn, task) is None:
            with db.write_txn(ctx.conn):
                boundary()
                delivery_contract.verified_result(contract, task, payload, delivery, {"outcome": "kaneo_done"})
            return {"outcome": "kaneo_done"}
        repo = _repository(task, delivery)
        boundary()
        pr = (gh_client.pr_view(repo, delivery["pr_number"], merge_checks=True) if policy
              else gh_client.pr_view(repo, delivery["pr_number"]))
        boundary()
        if not delivery.get("pushed_sha") or delivery["pushed_sha"] != delivery.get("head_sha"):
            raise git_delivery.GitDeliveryError("branch_diverged", "同步交付的提交与已推送快照不一致")
        _verify_pr_identity(pr, repo, delivery, delivery["pushed_sha"])
    except (git_delivery.GitDeliveryError, gh_client.GhError) as exc:
        boundary()
        return _failure(ctx, payload, task, delivery, exc)
    except bridge.BridgeError as exc:
        boundary()
        raise util.KanpilotError("remote_unavailable", gh_client.redact(str(exc))) from exc
    if pr["state"] == "CLOSED":
        return _closed(ctx, payload, task, delivery, pr)
    def verify_repository():
        boundary()
        if _repository(task, delivery) != repo:
            raise git_delivery.GitDeliveryError("branch_diverged", "交付期间 GitHub 仓库身份已变化，未提交自动合并")
        boundary()
    try:
        pr, auto_merge = pr_auto_merge.reconcile(ctx, task, delivery, repo, pr, policy, boundary,
            lambda observed: _verify_pr_identity(observed, repo, delivery, delivery["pushed_sha"]),
            verify_repository, lambda: pr_auto_merge.source_eligibility(task, live))
    except (git_delivery.GitDeliveryError, gh_client.GhError) as exc:
        boundary()
        return _failure(ctx, payload, task, delivery, exc)
    boundary()
    with db.write_txn(ctx.conn):
        fresh = boundary()
        display = {"auto_merge": auto_merge} if auto_merge else {}
        if pr["state"] == "MERGED":
            delivery_contract.verified_result(contract, fresh, payload, delivery, {"outcome": "merged"})
            # The PR merge is confirmed, while release policy may still hold
            # task completion. Persist an honest intermediate result with the
            # merge receipt; the final envelope is stored after that gate.
            intermediate = delivery_contract.verified_result(contract, fresh, payload, delivery,
                {"outcome": "waiting", "message": "PR 已合并，等待交付收尾"})
            display["stage_results"] = dict(delivery.get("stage_results") or {}, sync=intermediate)
            core.update_delivery(ctx.conn, fresh, ctx.now_iso, merged_at=pr["mergedAt"],
                                 pr_state="MERGED", last_sync_at=ctx.now_iso, publish_failures=0,
                                 next_sync_at=util.plus_seconds(300, ctx.now), **display)
            if not delivery.get("merged_at"):
                db.add_event(ctx.conn, ctx.now_iso, "pr_merged", task_id=task["task_id"],
                             request_id=payload["request_id"], actor="kaneo-delivery",
                             detail={"pr_number": delivery["pr_number"], "merged_at": pr["mergedAt"]})
        else:
            registered = util.parse_ts(db.task_extra(fresh).get("review_registered_at"))
            delay = 300 if policy or (registered and (ctx.now - registered).total_seconds() < 3600) else 1800
            next_sync = util.plus_seconds(delay, ctx.now)
            core.update_delivery(ctx.conn, fresh, ctx.now_iso, pr_state="OPEN", last_sync_at=ctx.now_iso,
                                 next_sync_at=next_sync, publish_failures=0, **display)
    if pr["state"] == "MERGED":
        # The merge receipt is local before Actions I/O. Release commands own
        # their own intent/dispatch receipts and never hold this writer lock.
        released = release_delivery.after_merge(ctx, db.require_task(ctx.conn, task["task_id"]), payload)
        if released["outcome"] in ("disabled", "published"):
            return {"outcome": "kaneo_done" if remote_done else "merged"}
        return {"outcome": "waiting", "next_sync_at": util.plus_seconds(300, ctx.now),
                "release": released.get("release")}
    return {"outcome": "waiting", "next_sync_at": next_sync}
