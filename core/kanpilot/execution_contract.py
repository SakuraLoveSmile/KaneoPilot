"""Bind execution specs and normalized evidence to the current stage contract.

This module never runs a task's verification commands. Native execution and
report identity checks remain with their existing adapters and core commands.
"""
from __future__ import annotations

import copy
from pathlib import Path

from . import git_delivery, stage_contract, util


def _inputs(spec, initial_source_sha256, requirements_sha256):
    from . import repair_prompt
    instruction = spec.get("instruction") or ""
    util.require(isinstance(instruction, str), "执行指令不是文本", "verification_identity_unconfirmed")
    try:
        body = repair_prompt.execution_body(spec)
    except (OSError, UnicodeError, ValueError) as exc:
        raise util.KanpilotError("verification_identity_unconfirmed", "执行正文不可核验") from exc
    return {
        "repository": spec["repository"], "plan_path": spec["plan_path"],
        "plan_sha256": spec["plan_sha256"],
        "requirements_sha256": requirements_sha256,
        "initial_source_sha256": initial_source_sha256,
        "completion_policy": spec["completion_policy"],
        "instruction_sha256": util.sha256_text(instruction),
        "execution_body_sha256": util.sha256_text(body),
        "prompt_sha256": util.sha256_text(instruction.rstrip() + "\n\n" + body),
        "repair_identity_sha256": (spec.get("repair_prompt") or {}).get("identity_sha256"),
    }


def freeze_spec(spec, *, requirements_sha256):
    """Called by dispatch after the isolated worktree and plan are verified."""
    initial_source = git_delivery.source_state_sha256(spec["cwd"])
    contract = stage_contract.make_contract(
        "execution", task_id=spec["task_id"], request_id=spec["request_id"],
        run_id=spec["run_id"], harness_id=spec["backend"],
        workspace=str(Path(spec["cwd"]).resolve()),
        inputs=_inputs(spec, initial_source, requirements_sha256),
        config=spec["config"],
        verification_commands=spec["required_verification_commands"],
        allowed_actions=("read_workspace", "write_workspace", "run_verification"))
    spec["stage_contract_required"] = True
    spec["stage_contract"] = contract
    return contract


def validate_spec(spec, *, job=None, starting=False):
    """Legacy absence is explicit; a present or required contract cannot vanish."""
    present = "stage_contract" in spec
    job_present = job is not None and "stage_contract" in job
    required = (spec.get("stage_contract_required") is True
                or (job is not None and job.get("stage_contract_required") is True))
    if not present and not job_present and not required:
        return None
    util.require(present and (job is None or job_present),
                 "执行阶段契约缺失，不能退回历史路径", "verification_identity_unconfirmed")
    contract = stage_contract.validate_contract(spec["stage_contract"])
    inputs = contract["inputs"]
    stage_contract.validate_contract(contract, expected={
        "stage": "execution", "task_id": spec["task_id"],
        "request_id": spec["request_id"], "run_id": spec["run_id"],
        "harness_id": spec["backend"], "workspace": str(Path(spec["cwd"]).resolve()),
        "inputs": _inputs(spec, inputs.get("initial_source_sha256"),
                          inputs.get("requirements_sha256")),
        "config": spec["config"],
        "verification_commands": spec["required_verification_commands"],
        "allowed_actions": ["read_workspace", "write_workspace", "run_verification"],
    })
    if job is not None:
        stage_contract.validate_contract(job["stage_contract"], expected=contract)
        util.require(job.get("request_id") == contract["request_id"],
                     "执行记录的请求身份不符", "verification_identity_unconfirmed")
    if starting:
        util.require(git_delivery.source_state_sha256(spec["cwd"])
                     == inputs["initial_source_sha256"],
                     "执行开始前源码已变化", "verification_identity_unconfirmed")
    return contract


def attach_to_job(spec, job):
    # Also used when writing a failed job after malformed input. Copying an
    # audit record grants no authority; validate_spec gates every native start.
    if "stage_contract" in spec or spec.get("stage_contract_required") is True:
        job.update(stage_contract_required=True, request_id=spec.get("request_id"))
        if "stage_contract" in spec:
            job["stage_contract"] = copy.deepcopy(spec["stage_contract"])
    return job


def verdict_result(contract, result, *, report_sha256):
    """Package only a verdict already checked by the deterministic core."""
    evidence = [{"kind": "verification_report", "sha256": report_sha256,
                 "provenance": "model_declared"},
                {"kind": "source_snapshot", "sha256": result["binding"]["source_sha256"],
                 "provenance": "core_verified"}]
    if result["verification_ok"]:
        payload = {"source_sha256": result["binding"]["source_sha256"],
                   "report_sha256": report_sha256, "checks": result["checks"]}
        outcome = "succeeded"
    else:
        payload = {"code": result["kind"], "detail": result["detail"]}
        outcome = "failed" if result["kind"] == "verify_failed" else "blocked"
    envelope = stage_contract.make_result(contract, payload, outcome=outcome, evidence=evidence)
    return stage_contract.validate_result(envelope, contract)
