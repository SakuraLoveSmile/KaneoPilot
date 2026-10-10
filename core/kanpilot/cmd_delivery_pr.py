"""Three-phase PR publication and passive merge observation."""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import bridge, constants, core, db, gh_client, git_delivery, remote, util


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
    history = extra.get("review_history") or []
    review = history[-1] if history and isinstance(history[-1], dict) else {}
    util.require(extra.get("review_conclusion") == "pass" and review.get("conclusion") == "pass"
                 and review.get("execution_run_id") == task["run_id"]
                 and review.get("head_sha") == delivery.get("head_sha")
                 and review.get("fix_round") == core.automation_of(task).get("fix_round", 0)
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
                         ("review_key", "review_report_path", "review_report_sha256"))
                 and core.delivery_of(fresh) == delivery,
                 "交付期间执行轮次或交付登记已变更", "conflict")
    return _identity(ctx, payload)


def _failure(ctx, payload, task, delivery, failure):
    with db.write_txn(ctx.conn):
        fresh = _fresh(ctx, payload, task, delivery)
        message = gh_client.redact(failure.message)
        code = None
        if isinstance(failure, git_delivery.GitDeliveryError):
            if failure.code == "branch_diverged":
                code = "branch_diverged"
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
                code = "gh_unavailable"
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
    repo = git_delivery.origin_github_repo(task["repository"])
    if not repo:
        raise gh_client.GhError("rejected", "origin 不是有效 GitHub 仓库")
    if delivery.get("pr_url"):
        url = urlsplit(delivery["pr_url"])
        expected = "/" + repo + "/pull/" + str(delivery.get("pr_number"))
        if (url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
                or url.path.lower() != expected.lower()):
            raise git_delivery.GitDeliveryError("branch_diverged", "origin 与已登记 PR 仓库不一致")
    return repo


def cmd_delivery_publish(ctx, payload) -> dict:
    task, delivery = _snapshot(ctx, payload, publish=True)
    def boundary():
        fresh = _fresh(ctx, payload, task, delivery)
        _verify_review_report(fresh)
        _fresh(ctx, payload, task, delivery)
    try:
        boundary()
        git_input = dict(delivery, repository=task["repository"])
        repo = _repository(task, delivery)
        boundary()
        pushed = git_delivery.push_branch(git_input, expected_repo=repo, before_push=boundary)
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
            pr = gh_client.pr_create(repo, base=delivery["base_branch"], head=delivery["branch"],
                                     title=title, body=build_pr_body(task, delivery, titles))
        boundary()
        _verify_pr_identity(pr, repo, delivery, pushed["pushed_sha"])
        if pr["state"] == "CLOSED":
            return _closed(ctx, payload, task, delivery, pr)
        boundary()
        git_delivery.verify_branch_worktree(git_input, require_clean=True, expected_head=delivery["head_sha"])
        with db.write_txn(ctx.conn):
            _fresh(ctx, payload, task, delivery)
        boundary()
        client = remote.get_client()
        marker = "[kaneopilot:delivery:" + task["task_id"] + "]"
        boundary()
        if not remote.find_marker_comment(client, task["task_id"], marker):
            boundary()
            remote.post_comment(client, task["task_id"], marker + " 已创建 PR：" + pr["url"])
    except (git_delivery.GitDeliveryError, gh_client.GhError) as exc:
        return _failure(ctx, payload, task, delivery, exc)
    except bridge.BridgeError as exc:
        return _failure(ctx, payload, task, delivery, gh_client.GhError("transient", str(exc)))
    boundary()
    with db.write_txn(ctx.conn):
        fresh = _fresh(ctx, payload, task, delivery)
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, pushed_sha=pushed["pushed_sha"],
                             pr_number=pr["number"], pr_url=pr["url"], pr_state=pr["state"],
                             kaneo_comment_at=ctx.now_iso, publish_failures=0,
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
    task, delivery = _snapshot(ctx, payload, publish=False)
    try:
        _verify_review_report(task)
        _fresh(ctx, payload, task, delivery)
        client = remote.get_client()
        _fresh(ctx, payload, task, delivery)
        live = remote.get_task(client, task["task_id"])
        _fresh(ctx, payload, task, delivery)
        if (live.get("task") or {}).get("status") == "done":
            with db.write_txn(ctx.conn):
                _fresh(ctx, payload, task, delivery)
            return {"outcome": "kaneo_done"}
        repo = _repository(task, delivery)
        _fresh(ctx, payload, task, delivery)
        pr = gh_client.pr_view(repo, delivery["pr_number"])
        _fresh(ctx, payload, task, delivery)
        if not delivery.get("pushed_sha") or delivery["pushed_sha"] != delivery.get("head_sha"):
            raise git_delivery.GitDeliveryError("branch_diverged", "同步交付的提交与已推送快照不一致")
        _verify_pr_identity(pr, repo, delivery, delivery["pushed_sha"])
    except (git_delivery.GitDeliveryError, gh_client.GhError) as exc:
        return _failure(ctx, payload, task, delivery, exc)
    except bridge.BridgeError as exc:
        raise util.KanpilotError("remote_unavailable", gh_client.redact(str(exc))) from exc
    if pr["state"] == "CLOSED":
        return _closed(ctx, payload, task, delivery, pr)
    _verify_review_report(task)
    with db.write_txn(ctx.conn):
        fresh = _fresh(ctx, payload, task, delivery)
        if pr["state"] == "MERGED":
            core.update_delivery(ctx.conn, fresh, ctx.now_iso, merged_at=pr["mergedAt"],
                                 pr_state="MERGED", last_sync_at=ctx.now_iso, publish_failures=0)
            if not delivery.get("merged_at"):
                db.add_event(ctx.conn, ctx.now_iso, "pr_merged", task_id=task["task_id"],
                             request_id=payload["request_id"], actor="kaneo-delivery",
                             detail={"pr_number": delivery["pr_number"], "merged_at": pr["mergedAt"]})
            return {"outcome": "merged"}
        registered = util.parse_ts(db.task_extra(fresh).get("review_registered_at"))
        delay = 300 if registered and (ctx.now - registered).total_seconds() < 3600 else 1800
        next_sync = util.plus_seconds(delay, ctx.now)
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, pr_state="OPEN", last_sync_at=ctx.now_iso,
                             next_sync_at=next_sync, publish_failures=0)
    return {"outcome": "waiting", "next_sync_at": next_sync}
