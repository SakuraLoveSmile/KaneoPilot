"""Trusted delivery envelopes; remote observations remain the business gate."""

import os

from . import core, db, stage_contract, util


def freeze(task, payload, delivery, operation):
    util.require(operation in ("publish", "sync"), "交付操作无效", "validation_failed")
    inputs = {key: task[key] for key in (
        "repository", "plan_sha256", "requirements_sha256", "review_key",
        "review_report_path", "review_report_sha256")}
    inputs.update(operation=operation, execution_run_id=task["run_id"],
                  head_sha=delivery.get("head_sha"), base_sha=delivery.get("base_sha"),
                  branch=delivery.get("branch"), base_branch=delivery.get("base_branch"),
                  pr_number=delivery.get("pr_number"))
    return stage_contract.make_contract("delivery", task_id=task["task_id"],
        request_id=payload["request_id"], run_id=payload["run_id"],
        harness_id="core_delivery", workspace=os.path.normpath(delivery["worktree_path"]),
        inputs=inputs, config={}, allowed_actions=[operation])


def validate_current(contract, task, payload, delivery, operation):
    """Compare against trusted current state, not identities from a result."""
    return stage_contract.validate_contract(contract, expected=freeze(task, payload, delivery, operation))


def begin(ctx, payload, task, delivery, operation):
    contracts = delivery.get("stage_contracts", {})
    results = delivery.get("stage_results", {})
    util.require(type(contracts) is dict and type(results) is dict
                 and set(contracts) <= {"publish", "sync"} and set(results) <= set(contracts),
                 "交付契约或结果登记损坏", "validation_failed")
    contracts = dict(contracts)
    for name, previous in contracts.items():
        stage_contract.validate_contract(previous, expected={"stage": "delivery",
            "task_id": task["task_id"], "harness_id": "core_delivery"})
        util.require(previous["inputs"].get("operation") == name,
                     "交付契约操作身份不符", "validation_failed")
        if name in results:
            stage_contract.validate_result(results[name], previous)
    contract = freeze(task, payload, delivery, operation)
    contracts[operation] = contract
    with db.write_txn(ctx.conn):
        fresh = db.require_task(ctx.conn, task["task_id"])
        util.require(core.delivery_of(fresh) == delivery, "交付快照已变化", "conflict")
        core.require_stage_identity(ctx.conn, payload, fresh, util.now_dt(), stages=("delivery",))
        # The prior result belongs to the prior operation snapshot only.
        results = dict(results)
        results.pop(operation, None)
        core.update_delivery(ctx.conn, fresh, ctx.now_iso, stage_contracts=contracts, stage_results=results)
    return contract, db.require_task(ctx.conn, task["task_id"]), core.delivery_of(db.require_task(ctx.conn, task["task_id"]))


def verified_result(contract, task, payload, delivery, result):
    validate_current(contract, task, payload, delivery, contract["inputs"]["operation"])
    outcome = result.get("outcome")
    util.require(outcome in (("published", "waiting", "retry", "blocked")
                 if contract["inputs"]["operation"] == "publish"
                 else ("merged", "kaneo_done", "waiting", "retry", "blocked")),
                 "交付结果不属于当前操作", "validation_failed")
    terminal = outcome in ("published", "merged", "kaneo_done")
    if terminal:
        def check(value):
            util.require(value.get("outcome") == outcome, "交付结果不一致", "validation_failed")
            if outcome == "published":
                util.require(type(value.get("pr_number")) is int and value.get("pr_url"),
                             "PR 交付缺少已核验身份", "validation_failed")
        envelope = stage_contract.make_result(contract, result,
            evidence=[{"source": "core_independent", "kind": "delivery_observation", "outcome": outcome}])
        return stage_contract.validate_result(envelope, contract, payload_validator=check)
    code = result.get("failure_code") or ("delivery_unknown" if result.get("operation_state") == "unknown" else "delivery_waiting")
    envelope = stage_contract.make_result(contract,
        {"code": code, "detail": result.get("message") or "交付尚未确认完成"}, outcome="blocked",
        evidence=[{"source": "core_independent", "kind": "delivery_observation", "result": result}])
    return stage_contract.validate_result(envelope, contract)


def consume(result, task, payload, operation):
    """Runner gate: no status advance from an unbound or stale CLI outcome."""
    util.require(isinstance(result.get("stage_contract"), dict) and isinstance(result.get("stage_result"), dict),
                 "交付结果缺少阶段契约", "validation_failed")
    contract = result["stage_contract"]
    delivery = core.delivery_of(task)
    # The operation freezes its pre-update target; the response is checked
    # against the runner's pre-call snapshot, including the old PR state.
    validate_current(contract, task, payload, delivery, operation)
    envelope = stage_contract.validate_result(result["stage_result"], contract)
    util.require(result.get("outcome") in (("published", "waiting", "retry", "blocked")
                 if operation == "publish" else ("merged", "kaneo_done", "waiting", "retry", "blocked")),
                 "交付结果不属于当前操作", "validation_failed")
    if result.get("outcome") in ("published", "merged", "kaneo_done"):
        util.require(envelope["outcome"] == "succeeded" and envelope["payload"] == {
            key: value for key, value in result.items() if key not in ("stage_contract", "stage_result")},
            "交付结果与核证信封不一致", "validation_failed")
    else:
        util.require(envelope["outcome"] != "succeeded", "未完成的交付不能标记成功", "validation_failed")
    return envelope
