"""Read-only verdict over Plus command results and the registered source tree.

Pilot never runs plan verification.  It checks trusted local Plus evidence,
source identity and delivery binding before permitting completion.
"""
from __future__ import annotations

import os
import json
import shlex
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import core, db, git_delivery, project_config, round_store, util, verify_evidence

# .pi/** is engine/plan bookkeeping; it never counts as task work.
PI_EXCLUDE_PREFIX = ".pi/"
_OUTPUT_TAIL_BYTES = 4000


def project_verify_env(conn, project_id) -> Dict[str, str]:
    """Read historical project configuration; never used by Plus verdicts."""
    _, _, items, _ = project_config.load_raw_mappings(conn)
    env = (items.get(project_id) or {}).get("verify_env")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


def effective_changes(worktree, *, exclude_prefix: str = PI_EXCLUDE_PREFIX, deadline=None, heartbeat=None) -> List[str]:
    """Changed files in the worktree, excluding the ``.pi/**`` bookkeeping tree.

    An empty list means the execution produced no countable task work.
    """
    prefix = exclude_prefix if exclude_prefix.endswith("/") else exclude_prefix + "/"
    git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
    changes = git_delivery._parse_worktree_status(git_delivery._git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree,
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)).stdout)
    return [c.get("path") or "" for c in changes
            if any(not p.startswith(prefix) and not git_delivery.is_build_artifact(p)
                   for p in (c.get("path") or "", c.get("original_path") or c.get("path") or ""))]


def has_effective_changes(worktree, **kw) -> bool:
    return bool(effective_changes(worktree, **kw))


def _command_not_found(code, tail: str) -> bool:
    """rc=127 or the shell's "command not found" → environment failure (阶梯 2)."""
    return code == 127 or "command not found" in tail.lower()


def environment_reason(check) -> Optional[str]:
    """Classify current and historical checks without changing their receipt."""
    if check.get("timed_out"):
        return None
    code = check.get("exit_code")
    if type(code) is int and code == 0:
        return None
    if type(code) is not int:
        # Old receipts use command_not_found for a failed shell launch.
        return "command_not_found" if check.get("command_not_found") else None
    # Current checks classify the raw in-memory output before redaction. A
    # short credential may obscure the signature in the persisted safe text.
    saved = check.get("environment_reason")
    if saved in ("command_not_found", "jdk_version", "android_sdk_missing", "no_connected_device"):
        return saved
    if check.get("command_not_found"):
        return "command_not_found"
    tail = (check.get("output_tail") or "").lower()
    if _command_not_found(code, tail):
        return "command_not_found"
    if (re.search(r"this build uses a java \d+ jvm", tail)
            or "requires at least jvm runtime version" in tail
            or "unsupported class file major version" in tail):
        return "jdk_version"
    if "sdk location not found" in tail:
        return "android_sdk_missing"
    if "no connected devices" in tail:
        return "no_connected_device"
    return None


def environment_reasons(checks) -> List[str]:
    return list(dict.fromkeys(reason for check in checks or []
                              for reason in [environment_reason(check)] if reason))


def environment_detail(reasons) -> str:
    hints = {"command_not_found": "Plus 验证命令不存在，请检查 Plus 执行环境的 PATH 与工具安装",
             "jdk_version": "Plus 使用的 Java 版本不符合要求，请配置 Plus 执行环境的 JAVA_HOME",
             "android_sdk_missing": "Plus 的 Android SDK 未就绪，请配置 Plus 执行环境的 ANDROID_HOME",
             "no_connected_device": "Plus 没有可用的已连接设备，请连接并授权测试设备"}
    return "验证环境未就绪：" + "；".join(hints[reason] for reason in reasons)


def run_command(*args, **kwargs):
    """Retired compatibility entry point: Pilot must never execute verification."""
    raise util.KanpilotError("local_verification_disabled", "验证由 Plus 执行；Pilot 只核对工具证据")


def run_verify_commands(*args, **kwargs):
    """Retired compatibility entry point; retained to fail closed for old callers."""
    return run_command(*args, **kwargs)


def _check_result(commands, changes, checks):
    empty_diff = not changes
    reasons = environment_reasons(checks)
    env_failure = bool(reasons)
    if empty_diff:
        passed, detail = False, "执行完成后没有可计入的改动（.pi/** 已排除），判定空转"
    elif not commands:
        passed, detail = False, "计划未声明 verify 命令，需人工接受验证结果"
    elif env_failure:
        passed = False
        detail = environment_detail(reasons)
    else:
        passed = len(checks) == len(commands) and all(
            type(c["exit_code"]) is int and c["exit_code"] == 0 and not c["timed_out"]
            for c in checks)
        detail = ("有效改动非空且验证命令逐条通过" if passed
                  else "验证未全部通过，请查看各条独立退出码与输出")
    return {"verification_ok": passed, "empty_diff": empty_diff,
            "environment_failure": env_failure, "environment_reasons": reasons, "effective_changes": changes,
            "kind": "passed" if passed else ("empty_diff" if empty_diff else
                    "environment_failure" if env_failure else "verify_failed" if commands else "unconfirmed"),
            "checks": checks, "detail": detail}


def binding(conn, task, *, allow_core_commit=False, deadline=None, heartbeat=None) -> dict:
    """Require the frozen declaration and the registered isolated task tree."""
    try:
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        def value(args, directory):
            return git_delivery._git(args, directory, git_delivery._budget_timeout(deadline, heartbeat=heartbeat)).stdout.decode().strip()
        def common(directory):
            target = Path(value(["rev-parse", "--git-common-dir"], directory))
            return (target if target.is_absolute() else directory / target).resolve()
        path = Path(task["plan_path"])
        raw = path.read_bytes()
        parsed = verify_evidence.parse_verify_block(raw.decode("utf-8"))
        declaration = db.task_extra(task).get("plan_verify") or {}
        if (util.sha256_bytes(raw) != task["plan_sha256"] or not parsed["ok"]
                or declaration.get("plan_sha256") != task["plan_sha256"]
                or declaration.get("commands") != parsed["commands"]):
            raise ValueError("计划内容或 verify 声明不符")
        row = round_store.get_round(conn, task["task_id"], task["run_id"])
        if row is None or not row["worktree_path"] or row["save_state"] != "active":
            raise ValueError("缺少登记执行工作树")
        worktree = Path(row["worktree_path"])
        repo = Path(task["repository"])
        if (worktree.is_symlink() or worktree.resolve() == repo.resolve()
                or common(worktree) != common(repo)
                or Path(value(["rev-parse", "--show-toplevel"], worktree)).resolve() != worktree.resolve()):
            raise ValueError("执行工作树归属不可确认")
        delivery = core.delivery_of(task)
        head = value(["rev-parse", "HEAD"], worktree)
        branch = value(["rev-parse", "--abbrev-ref", "HEAD"], worktree)
        if core.requires_branch_review(task):
            if row["worktree_path"] != delivery.get("worktree_path"):
                raise ValueError("执行工作树与任务不符")
            registered = delivery.get("head_sha")
            if allow_core_commit and head != registered:
                parents = value(["show", "-s", "--format=%P", "HEAD"], worktree).split()
                trailers = value(["show", "-s", "--format=%(trailers:key=KaneoPilot-Run,valueonly)", "HEAD"], worktree).splitlines()
                expected_parents = [registered]
                repair = core.review_repair_of(task)
                if repair.get("source_kind") == "merge_conflict":
                    from .. import review_repair
                    review_repair.verify_frozen(task)
                    if repair.get("head_sha") != registered or repair.get("repair_execution_run_id") != task["run_id"]:
                        raise ValueError("合并冲突处理的提交身份不符")
                    expected_parents.append(repair["target_base_sha"])
                if parents != expected_parents or trailers != [task["run_id"]]:
                    raise ValueError("未登记提交不是本轮核心提交")
            if branch != delivery.get("branch") or (not allow_core_commit and head != registered):
                raise ValueError("任务分支或登记 HEAD 不符")
        elif branch != "HEAD":
            raise ValueError("执行工作树未分离 HEAD")
        # Capture all source bytes, not just status: verification may generate
        # pyc/cache files, but must not silently edit the source being judged.
        return {"task_id": task["task_id"], "execution_run_id": task["run_id"],
                "plan_sha256": task["plan_sha256"], "commands_sha256": util.sha256_text(util.jdump(parsed["commands"])),
                "worktree_path": str(worktree), "head_sha": head, "branch": branch,
                "source_sha256": git_delivery.source_state_sha256(worktree, deadline=deadline, heartbeat=heartbeat)}
    except (OSError, ValueError, TypeError, KeyError, git_delivery.GitDeliveryError) as exc:
        raise util.KanpilotError("verification_identity_unconfirmed", "计划或执行工作树不可确认：" + util.redact(str(exc))) from exc


def _read_plus_evidence(conn, task, *, deadline=None, heartbeat=None):
    from .. import verification_recovery
    try:
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        evidence = verification_recovery._read_evidence(conn, task, require_pass=False)
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        # A finished local job remains historical once its Plus session has a
        # new turn. Query only status/turn; never send a prompt or run a model.
        verification_recovery._require_live_terminal(evidence[1])
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        return evidence
    except util.KanpilotError as exc:
        raise util.KanpilotError("verification_identity_unconfirmed", exc.message) from exc


def _evidence_unchanged(hashes):
    try:
        return bool(hashes) and all(util.sha256_file(Path(path)) == digest for path, digest in hashes.items())
    except OSError:
        return False


def _require_snapshot(job, source_sha256):
    if (type(job.get("verification_evidence_version")) is not int
            or job["verification_evidence_version"] != 1
            or not job.get("verification_source_sha256")):
        raise util.KanpilotError("verification_unconfirmed",
            "Plus 验证缺少源码快照；请通过新的执行轮次补齐验证证据")
    if not source_sha256 or job["verification_source_sha256"] != source_sha256:
        raise util.KanpilotError("verification_identity_unconfirmed", "Plus 验证完成后源码已变化，不能沿用旧验证")


def receipt_evidence_current(conn, task, receipt, *, deadline=None, heartbeat=None):
    """Recheck receipt evidence on retries; historical local receipts never pass."""
    if receipt.get("evidence_source") == "goal_report":
        return _goal_receipt_current(conn, task, receipt, deadline=deadline, heartbeat=heartbeat)
    if receipt.get("evidence_source") != "plus_tool_results":
        return False
    try:
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        hashes, job, result = _read_plus_evidence(conn, task, deadline=deadline, heartbeat=heartbeat)
        _require_snapshot(job, (receipt.get("binding") or {}).get("source_sha256"))
        return (hashes == receipt.get("evidence_sha256")
                and job.get("turn_id") == receipt.get("execution_turn_id")
                and _evidence_unchanged(hashes)
                and (receipt.get("verification_ok") is not True or result.get("ok") is True))
    except (util.KanpilotError, OSError, ValueError, TypeError, git_delivery.GitDeliveryError):
        return False


def _inside_worktree(value, cwd, root):
    if not isinstance(value, str) or not value or any(char in value for char in "$`*?[]~"):
        raise ValueError("工具工作目录不是可核验的固定路径")
    target = Path(value)
    target = (target if target.is_absolute() else cwd / target).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Plus 验证工具工作目录越出本轮执行工作树") from exc
    return target


def _command_context(message, root):
    tool = message.get("tool") or {}
    arguments = tool.get("input")
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("Plus 验证工具参数无法核验")
    cwd, explicit_cwd = root, None
    for key in ("cwd", "workdir"):
        if key in arguments:
            selected = _inside_worktree(arguments[key], root, root)
            if explicit_cwd is not None and explicit_cwd != selected:
                raise ValueError("Plus 工具工作目录参数互相冲突")
            cwd = explicit_cwd = selected
    segments, _ = verify_evidence._command_segments(arguments.get("command", ""), allow_single=True)
    for segment in segments:
        words = shlex.split(segment)
        if words and words[0] == "cd":
            arguments = words[1:]
            if arguments and arguments[0] == "--":
                arguments = arguments[1:]
            if len(arguments) != 1:
                raise ValueError("Plus 验证命令的 cd 目标无法核验")
            cwd = _inside_worktree(arguments[0], cwd, root)


def _plus_checks(commands, result, messages, root):
    # The shared matcher has already limited these to the current execution
    # window and invalidated checks preceding a later writable tool.
    results = result.get("command_results") or []
    from .. import verification_redaction
    secrets = verification_redaction.values(os.environ)
    checks = []
    for command in commands:
        candidates = [row for row in results
                      if not verify_evidence.match_verification([command], [row])["missing"]]
        if not candidates:
            continue
        row = candidates[-1]
        message = next((item for item in messages if item.get("id") == row.get("message_id")), None)
        if message is None:
            raise ValueError("Plus 验证工具消息身份无法核验")
        _command_context(message, root)
        single = verify_evidence.match_verification([command], [row])
        code = 0 if single["ok"] else single["failed"][0]["exit_code"]
        output = (message.get("tool") or {}).get("output") or message.get("content") or ""
        if not isinstance(output, str):
            output = util.jdump(output)
        raw = {"exit_code": code, "output_tail": output, "timed_out": False}
        checks.append({"command": verification_redaction.redact(command, secrets), "exit_code": code,
                       "timed_out": False, "command_not_found": environment_reason(raw) == "command_not_found",
                       "environment_reason": environment_reason(raw), "message_id": row.get("message_id"),
                       "output_tail": verification_redaction.redact(output, secrets)[-_OUTPUT_TAIL_BYTES:]})
    return checks


def evaluate_task(conn, task, *, verify_env=None, timeout=None, deadline=None, heartbeat=None, isolated=False) -> dict:
    """Read Plus evidence in every mode; never execute or copy verification."""
    from .. import dsh_events
    frozen = binding(conn, task, deadline=deadline, heartbeat=heartbeat)
    hashes, job, verification = _read_plus_evidence(conn, task, deadline=deadline, heartbeat=heartbeat)
    _require_snapshot(job, frozen["source_sha256"])
    raw = Path(task["plan_path"]).read_bytes()
    commands = verify_evidence.parse_verify_block(raw.decode("utf-8"))["commands"]
    try:
        # Re-read only hashed files; a concurrent append/replacement cannot be
        # accepted because all evidence hashes are checked again below.
        event_path = next(Path(path) for path in hashes if Path(path).name == dsh_events.EVENT_LOG_NAME)
        events = [json.loads(line) for line in event_path.read_text().splitlines() if line.strip()]
        messages = dsh_events.to_messages(events)
        checks = _plus_checks(commands, verification, messages, Path(frozen["worktree_path"]).resolve())
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise util.KanpilotError("verification_identity_unconfirmed", "Plus 验证证据无法确认：" + util.redact(str(exc))) from exc
    changes = effective_changes(frozen["worktree_path"], deadline=deadline, heartbeat=heartbeat)
    result = _check_result(commands, changes, checks)
    if not verification.get("ok") and not result["empty_diff"]:
        # Missing/ambiguous results cannot trigger a quality repair. Only a
        # real independent nonzero exit is a verification failure.
        failed = verification.get("failed") or []
        real_failure = any(type(item.get("exit_code")) is int and item["exit_code"] != 0 for item in failed)
        result["verification_ok"] = False
        if not real_failure:
            result.update(kind="unconfirmed", environment_failure=False, environment_reasons=[],
                          detail="当前 Plus 轮次的验证证据缺失、已过期或退出码不明确；请通过新的执行轮次补齐验证")
    if not _evidence_unchanged(hashes):
        raise util.KanpilotError("verification_identity_unconfirmed", "核验期间 Plus 执行证据或计划文件已变化")
    if binding(conn, db.require_task(conn, task["task_id"]), deadline=deadline, heartbeat=heartbeat) != frozen:
        raise util.KanpilotError("verification_identity_unconfirmed", "核验期间计划、轮次或源文件已变化")
    result.update(binding=frozen, evidence_source="plus_tool_results", evidence_sha256=hashes,
                  execution_turn_id=job["turn_id"], verification_evidence_version=1)
    if result["verification_ok"] and not isolated:
        result["index_source_sha256"] = git_delivery.expected_index_source_sha256(frozen["worktree_path"], deadline=deadline, heartbeat=heartbeat)
        if (binding(conn, db.require_task(conn, task["task_id"]), deadline=deadline, heartbeat=heartbeat) != frozen
                or not _evidence_unchanged(hashes)):
            raise util.KanpilotError("verification_identity_unconfirmed", "冻结 Git 提交内容期间源码或 Plus 证据已变化")
    return result


def evaluate_goal_task(conn, task, *, deadline=None, heartbeat=None, isolated=False) -> dict:
    """Live verdict from Plus's structured Goal report; transcript tool results are never read."""
    from .. import verification_recovery
    frozen = binding(conn, task, deadline=deadline, heartbeat=heartbeat)
    try:
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        evidence = verification_recovery.read_goal_evidence(conn, task)
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
    except util.KanpilotError as exc:
        if exc.code == "verification_unconfirmed":
            raise
        raise util.KanpilotError("verification_identity_unconfirmed", util.redact(exc.message)) from exc
    _require_snapshot(evidence["job"], frozen["source_sha256"])
    commands, coverage, report = evidence["commands"], evidence["coverage"], evidence["report"]
    checks = [{"command": row["command"], "exit_code": row["exit_code"], "timed_out": False,
               "command_not_found": False, "message_id": row["report_check_id"],
               "output_tail": util.redact(row["detail"])[-_OUTPUT_TAIL_BYTES:]}
              for row in coverage["rows"]]
    changes = effective_changes(frozen["worktree_path"], deadline=deadline, heartbeat=heartbeat)
    result = _check_result(commands, changes, checks)
    if not coverage["ok"] and not result["empty_diff"]:
        # Missing or unconfirmed report records stay unconfirmed; only a real nonzero exit fails.
        real_failure = any(type(item.get("exit_code")) is int and item["exit_code"] != 0
                           for item in coverage["failed"])
        result["verification_ok"] = False
        if not real_failure:
            result.update(kind="unconfirmed", environment_failure=False, environment_reasons=[],
                          detail=verification_recovery.goal_coverage_detail(coverage))
    try:
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        recheck = verification_recovery.read_goal_evidence(conn, db.require_task(conn, task["task_id"]))
    except util.KanpilotError as exc:
        raise util.KanpilotError("verification_identity_unconfirmed", util.redact(exc.message)) from exc
    if (not _evidence_unchanged(evidence["hashes"]) or recheck["hashes"] != evidence["hashes"]
            or recheck["report"]["report_sha256"] != report["report_sha256"]):
        raise util.KanpilotError("verification_identity_unconfirmed", "核验期间 Plus 报告或执行证据已变化")
    if binding(conn, db.require_task(conn, task["task_id"]), deadline=deadline, heartbeat=heartbeat) != frozen:
        raise util.KanpilotError("verification_identity_unconfirmed", "核验期间计划、轮次或源文件已变化")
    result.update(binding=frozen, evidence_source="goal_report", evidence_sha256=evidence["hashes"],
                  execution_turn_id=report["turn_id"], report_id=report.get("report_id"),
                  report_sha256=report["report_sha256"], verification_evidence_version=1,
                  goal_check_coverage={"matched": coverage["matched"], "missing": coverage["missing"],
                                       "total": len(commands)})
    if result["verification_ok"] and not isolated:
        result["index_source_sha256"] = git_delivery.expected_index_source_sha256(
            frozen["worktree_path"], deadline=deadline, heartbeat=heartbeat)
        if (binding(conn, db.require_task(conn, task["task_id"]), deadline=deadline, heartbeat=heartbeat) != frozen
                or not _evidence_unchanged(evidence["hashes"])):
            raise util.KanpilotError("verification_identity_unconfirmed", "冻结 Git 提交内容期间源码或 Plus 证据已变化")
    return result


def _goal_receipt_current(conn, task, receipt, *, deadline=None, heartbeat=None):
    """Recheck a goal_report receipt: same run files, same report bytes, same coverage result."""
    from .. import verification_recovery
    try:
        git_delivery._budget_timeout(deadline, heartbeat=heartbeat)
        evidence = verification_recovery.read_goal_evidence(conn, task)
        _require_snapshot(evidence["job"], (receipt.get("binding") or {}).get("source_sha256"))
        report = evidence["report"]
        return (evidence["hashes"] == receipt.get("evidence_sha256")
                and report["report_sha256"] == receipt.get("report_sha256")
                and report["turn_id"] == receipt.get("execution_turn_id")
                and _evidence_unchanged(evidence["hashes"])
                and (receipt.get("verification_ok") is not True or evidence["coverage"]["ok"] is True))
    except (util.KanpilotError, OSError, ValueError, TypeError, git_delivery.GitDeliveryError):
        return False


def valid_committed_receipt(conn, task, receipt, *, deadline=None, heartbeat=None):
    """A core commit changes HEAD/status, but not the verified source bytes."""
    if (receipt.get("evidence_source") != "goal_report"
            or receipt.get("verification_ok") is not True
            or receipt.get("execution_run_id") != task["run_id"]
            or receipt.get("plan_sha256") != task["plan_sha256"]):
        return False
    if not receipt_evidence_current(conn, task, receipt, deadline=deadline, heartbeat=heartbeat):
        return False
    frozen = receipt.get("binding") or {}
    actual = binding(conn, task, allow_core_commit=True, deadline=deadline, heartbeat=heartbeat)
    delivery = core.delivery_of(task)
    return (dict(actual, head_sha=frozen.get("head_sha")) == frozen
            and (actual["head_sha"] != frozen.get("head_sha")
                 or any(row.get("execution_run_id") == task["run_id"] and row.get("sha") == actual["head_sha"]
                        for row in delivery.get("commits") or []))
            and git_delivery.tree_source_sha256(actual["worktree_path"], actual["head_sha"], deadline=deadline, heartbeat=heartbeat) == receipt.get("index_source_sha256"))
