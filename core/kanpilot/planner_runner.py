"""规划 runner（protocol.md §5.22.3 v2/T5；v4/T6.2 后端分流）。

CLI：``python -m kanpilot.planner_runner [--request-id <id>] [--once]``
（App 侧经 ExecutorScheduler 以独立子进程拉起；stdout/stderr 重定向到
``<app_support>/executor-logs/planner-<ts>.log``）。

**后端分流（v4 §5.24.3）**：request payload ``planner_backend`` 缺省/``codex``
→ 本模块的 codex 分支（行为与 v2 一致，一字未改）；``pi_desktop`` → 整轮交
``pi_desktop_planner``（独立规划 worktree + plan 会话 + plan proposal 读取/关闭）；
非法值 → 明确失败（``_fail_unknown_backend``），**不**静默改走后端默认值。

职责（手动规划链执行端）：
1. ``claim`` stage=plan executor=``kaneo-planner``（--request-id 定点领取
   或自动取单；未授权/无任务 → 结束）；
2. 在 ``<session_runs_root>/<task_id>/<run_id>/`` 写 ``job.json``
   （backend=codex + planner_method；spec_plan 附 plugin_id/
   plugin_version）+ ``events.jsonl``（session 首事件；codex JSONL
   进度经 codex_planner.map_event 翻译）；原始 JSONL 落 ``codex.jsonl``；
3. 拉起 ``codex exec``（只读 sandbox；prompt 经 stdin；``--output-schema``
   约束最终输出；``-o`` 落最后消息）；心跳 ``heartbeat`` 间隔
   ``min(lease_seconds/3, PLANNER_HEARTBEAT_MAX_SECONDS)``；
   租约失效/心跳返回 stale_run → 终止子进程组、禁止发布；
4. 终态：schema 校验 ``-o`` 输出——ready → 生成带 frontmatter 的计划文件
   （元数据由本进程按真实输入生成，不信模型自报）→ ``publish-plan``；
   needs_input → 草稿落 ``plan-draft.md`` + ``report needs_user``；
   超时/崩溃/输出不合规 → ``report failed``（不自动重试）；子进程非零
   退出按 §5.22.6 分类 failure_reason（model_unavailable/auth_failed/
   request_rejected/process_exit/crash）并只显示一条主要原因。
   publish-plan 自身在同事务结束请求（§5.6），成功路径不再 report。

超时：settings ``codex_timeout_seconds`` > ``executor_timeout_seconds`` >
1800s。单飞由调用方保证（同 executor_runner 约定）。

KANPILOT_CODEX_FAKE（隔离测试接缝，不拉起 codex）：
- ``ready``（默认 fake 值）：写假事件 + 合成 ready 输出 → publish-plan；
- ``needs_input``：写 questions 输出 → report needs_user；
- ``invalid``：写不合规输出 → report failed；
- ``fail``/``crash``：写 job(running) 后以非零退出/不写 terminal →
  report failed（crash 模拟子进程异常）。
"""

from __future__ import annotations

from . import plan_questions

import argparse
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import cli, codex_planner, constants, db, headless_adapter, util
from . import dsh_events, task_workspace

DEFAULT_PLANNER_TIMEOUT_SECONDS = 1800


# ------------------------------------------------------------ 轮次记录（同 pi_worker 口径）

def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


class _EventLog:
    """events.jsonl 追加器（seq 单调；flush 每行）。

    - 字段值为 ``None`` 的一律省略（§5.22.6：如 system 事件缺 ``code``）。
    - ``redact``（可选）逐字段清洗字符串（含嵌套 dict/list），凭据值
      不落盘（provider.env 密钥 / util._SECRET_PATTERNS 形态）。
    """

    def __init__(self, path: Path, redact=None):
        self.path = Path(path)
        self._redact = redact
        self.seq = 0
        if self.path.is_file():
            events, _ = dsh_events.iter_events(self.path)
            if events:
                self.seq = max(e.get("seq", 0) for e in events)

    def _clean(self, value):
        if isinstance(value, str) and self._redact is not None:
            return self._redact(value)
        if isinstance(value, dict):
            return {k: self._clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        return value

    def emit(self, etype: str, ts: str, **fields) -> dict:
        self.seq += 1
        event = {"v": 1, "seq": self.seq, "ts": ts, "type": etype}
        for key, value in fields.items():
            if value is None:
                continue  # None 字段省略（如缺 code 的 system 事件）
            event[key] = self._clean(value)
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
        return event

# ------------------------------------------------------------ 计划文件生成

def _yaml_scalar(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _plan_file_path(project_id: Optional[str], title: str) -> Path:
    root = constants.plan_root()
    if project_id and re.fullmatch(r"[A-Za-z0-9_-]+", str(project_id)):
        directory = root / "projects" / str(project_id)
    else:
        directory = root
    directory = util.inside(directory, root, "plan_directory")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe = "".join(
        ch if (ch.isalnum() or ch in " _-[]【】（）()") else "_"
        for ch in (title or "plan")
    ).strip()[:80] or "plan"
    path = directory / f"{safe}_{stamp}.md"
    path = util.inside(path, root, "plan_path")
    counter = 1
    while path.exists():
        path = directory / f"{safe}_{stamp}-{counter}.md"
        path = util.inside(path, root, "plan_path")
        counter += 1
    return path


def _strip_model_frontmatter(markdown: str) -> str:
    """模型若在正文开头自带 frontmatter，剥掉（元数据由本进程生成）。"""
    text = markdown.lstrip("\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            return text[end + 4:].lstrip("\n")
    return markdown


def review_fix_plan_task(conn, task_id: str, req_payload: dict) -> dict:
    """Resolve an independent fix's registered report; never infer paths or write DB.

    Older create-review-fix rows lack report fields. Only a registered parent
    delivery (current or historical) for this exact child/key can fill them.
    Already complete historical child rows remain usable when their parent
    predates report delivery history, with any available authority still checked.
    """
    task = dict(db.require_task(conn, task_id))
    if task["plan_kind"] != "review_fix" or req_payload.get("fix_mode") == "same_task":
        return task
    source_id, key = task["review_source_task_id"], task["review_key"]
    util.require(source_id and key, "review fix binding missing source/review_key")
    for field, expected in (("review_source_task_id", source_id), ("review_key", key)):
        util.require(not req_payload.get(field) or req_payload[field] == expected,
                     "review fix " + field + " mismatch")
    source = db.get_task(conn, source_id)
    util.require(source is not None and not db.task_is_purged(source),
                 "review fix binding source unavailable")
    util.require(source["project_id"] == task["project_id"]
                 and source["repository"] == task["repository"],
                 "review fix binding project/repository mismatch")
    extra = db.task_extra(source)
    records = list(extra.get("review_history") or [])
    if source["review_key"] == key:
        util.require(not source["fix_task_id"] or source["fix_task_id"] == task_id,
                     "review fix binding child mismatch")
        # create-review-fix changes the parent link before register-review and
        # leaves previous report columns intact. A receipt must bind those
        # columns to this cycle before they can supply a missing child report.
        registered = any(
            isinstance(detail, dict)
            and detail.get("conclusion") == "changes_requested"
            and detail.get("review_key") == key
            and detail.get("fix_task_id") == task_id
            and detail.get("report_sha256") == source["review_report_sha256"]
            for detail in (util.jload(row["detail_json"]) for row in conn.execute(
                "SELECT detail_json FROM events WHERE task_id=? AND kind='review_registered'",
                (source_id,))))
        if source["fix_task_id"] == task_id and registered:
            records.append({"review_key": key, "fix_task_id": task_id,
                            "conclusion": extra.get("review_conclusion"),
                            "report_path": source["review_report_path"],
                            "report_sha256": source["review_report_sha256"]})
    candidates = []
    for record in records:
        if not isinstance(record, dict) or record.get("review_key") != key:
            continue
        util.require(not record.get("fix_task_id") or record["fix_task_id"] == task_id,
                     "review fix binding history child mismatch")
        if record.get("fix_task_id") != task_id:
            continue
        if not any(record.get(field) for field in ("conclusion", "report_path", "report_sha256")):
            continue  # Older delivery links have no authoritative report receipt.
        util.require(record.get("conclusion") == "changes_requested",
                     "review fix binding conclusion mismatch")
        if record.get("report_path") and record.get("report_sha256"):
            candidates.append((record["report_path"], record["report_sha256"]))
    path, digest = task["review_report_path"], task["review_report_sha256"]
    if not path or not digest:
        util.require(bool(candidates), "review fix report binding not registered")
        recovered_path, recovered_digest = candidates[0]
        util.require((not path or path == recovered_path)
                     and (not digest or digest == recovered_digest),
                     "review fix report binding mismatch")
        path, digest = recovered_path, recovered_digest
    util.require(all(pair == (path, digest) for pair in candidates),
                 "review fix registered report binding mismatch")
    report = util.inside(Path(path), constants.review_reports_root(), "review_report_path")
    util.require(report.is_file(), "review report missing")
    util.require(util.sha256_file(report) == digest, "review report sha256 mismatch")
    task.update(review_report_path=str(report), review_report_sha256=digest)
    return task


def build_plan_meta(
    context: dict, manifest: dict, generated_at: str, task_row=None,
    model_config=None,
) -> dict:
    """按本轮真实输入生成计划 frontmatter 元数据（§6）。

    ``task_row``：DB 原始任务行（review_fix 必填字段 review_report_* 不在
    claim context 的 TaskSummary 里，由调用方按 task_id 拉取）。
    ``model_config``：发布用的 ``{planner_model, reasoning_effort}``；``None``
    时取请求 payload 的 ``model_config`` 快照（codex 调用方行为不变）。Plus
    分支传入按 §5.24.1 解析出的发布模型对（见 ``pi_desktop_planner``）——
    模型字段恒由本进程按真实输入写入，不信模型自报。
    """
    task = context.get("task") or {}
    req_payload = context.get("payload") or {}
    manifest_task = manifest.get("task") or {}
    proj_snap = req_payload.get("project_snapshot") or {}
    task_id = context.get("task_id") or manifest_task.get("task_id")
    project_id = (
        proj_snap.get("project_id")
        or manifest_task.get("project_id")
        or task.get("project_id")
        or constants.PROJECTS.get(manifest_task.get("project_name") or task.get("project_name") or "")
    )
    project_name = (
        proj_snap.get("project_name")
        or manifest_task.get("project_name")
        or task.get("project_name")
    )
    repository = (
        proj_snap.get("repository")
        or manifest_task.get("repository")
        or task.get("repository")
        or ""
    )
    if model_config is None:
        model_config = req_payload.get("model_config") or {}
    plan_kind = task.get("plan_kind") or manifest_task.get("plan_kind") \
        or "standard"
    meta = {
        "task_id": task_id,
        "project_id": project_id,
        "project_name": project_name,
        "repository": repository,
        "source_sha256": manifest.get("source_sha256")
        or (task.get("source_sha256") or ""),
        "requirements_sha256": manifest.get("requirements_sha256")
        or (task.get("requirements_sha256") or ""),
        "planner_model": model_config.get("planner_model") or "",
        "reasoning_effort": model_config.get("reasoning_effort") or "",
        "implementation_status": "未开始",
        "generated_at": generated_at,
        "plan_kind": plan_kind,
        "planner_input_sha256": context.get("payload", {}).get(
            "planner_input_sha256") or "",
    }
    if req_payload.get("fix_mode") == "same_task":
        meta.update({key: req_payload.get(key) or "" for key in (
            "review_report_path", "review_report_sha256", "review_key")})
        meta.update(fix_mode="same_task", fix_round=req_payload.get("fix_round"),
                    review_source_task_id=task_id)
    elif plan_kind == "review_fix":
        row = task_row or {}
        meta.update({
            # 优先 DB 任务行（含 review_report_*）；回落请求 payload
            # （_submit_one 写入 review_key/review_source_task_id）。
            "review_report_path": (
                row["review_report_path"]
                if row and row["review_report_path"]
                else ""
            ),
            "review_report_sha256": (
                row["review_report_sha256"]
                if row and row["review_report_sha256"]
                else ""
            ),
            "review_source_task_id": (
                (row["review_source_task_id"] if row else None)
                or req_payload.get("review_source_task_id") or ""
            ),
            "review_key": (
                (row["review_key"] if row else None)
                or req_payload.get("review_key") or ""
            ),
        })
    return meta


def write_plan_file(
    context: dict, manifest: dict, plan_markdown: str, generated_at: str,
    task_row=None, model_config=None, target_path=None,
) -> Path:
    """``build_plan_meta`` 的 frontmatter + 模型 plan_markdown 正文 → PLAN_ROOT。"""
    meta = build_plan_meta(
        context, manifest, generated_at, task_row=task_row,
        model_config=model_config,
    )
    task = context.get("task") or {}
    manifest_task = manifest.get("task") or {}
    task_id = context.get("task_id") or manifest_task.get("task_id")
    lines = ["---"]
    for key, value in meta.items():
        scalar = str(value) if key == "fix_round" and isinstance(value, int) else _yaml_scalar(value)
        lines.append(f"{key}: {scalar}")
    lines.append("---")
    body = _strip_model_frontmatter(plan_markdown or "")
    path = (util.inside(Path(target_path), constants.plan_root(), "plan_path")
            if target_path is not None else
            _plan_file_path(meta.get("project_id"), task.get("title") or task_id or "plan"))
    path.write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")
    return path


# ------------------------------------------------------------ 输出契约校验

def parse_output(path: Path) -> dict:
    """读取 -o 落盘的最终消息并校验输出契约（§5.22.3）。

    返回规范化 dict；不合规 → util.KanpilotError(validation_failed)。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise util.KanpilotError(
            "validation_failed", f"规划输出不可读：{exc}"
        ) from exc
    text = text.strip()
    if not text:
        raise util.KanpilotError("validation_failed", "规划输出为空")
    try:
        data = json.loads(text)
    except ValueError:
        # 宽容：模型可能把 JSON 包在 markdown 围栏里。
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except ValueError as exc:
                raise util.KanpilotError(
                    "validation_failed", f"规划输出不是 JSON：{exc}"
                ) from exc
        else:
            raise util.KanpilotError(
                "validation_failed", "规划输出不是 JSON"
            )
    from . import stage_contract
    return stage_contract.validate_plan_result(data)


def _prepare_stage_inputs(context, task_id, request_id, run_id, backend):
    """Check shared claimed materials before selecting an adapter; no workspace freeze."""
    from . import pi_desktop_planner, stage_contract
    payload = context.get("payload") or {}
    manifest = payload.get("manifest")
    util.require(isinstance(manifest, dict) and bool(manifest),
                 "请求缺少规划材料 manifest", "validation_failed")
    input_id = payload.get("input_id")
    util.require(isinstance(input_id, str) and bool(input_id)
                 and manifest.get("input_id") == input_id
                 and context.get("input_id") == input_id,
                 "规划材料 input_id 与领取身份不符", "validation_failed")
    digest = payload.get("planner_input_sha256")
    util.require(isinstance(digest, str) and bool(digest)
                 and pi_desktop_planner.manifest_sha(manifest) == digest,
                 "规划材料指纹不符", "validation_failed")
    manifest_task = manifest.get("task") or {}
    util.require(isinstance(manifest_task, dict), "规划材料 task 不是对象", "validation_failed")
    util.require(manifest_task.get("task_id") == task_id,
                 "规划材料 task_id 与领取身份不符", "validation_failed")
    project = payload.get("project_snapshot") or {}
    util.require(isinstance(project, dict), "规划项目快照不是对象", "validation_failed")
    repository = project.get("repository") or manifest_task.get("repository")
    util.require(isinstance(repository, str) and Path(repository).is_absolute(),
                 "规划材料缺少绝对项目路径", "validation_failed")
    runtime = payload.get("runtime_config") or {}
    model = payload.get("model_config") or {}
    util.require(isinstance(runtime, dict) and isinstance(model, dict),
                 "规划模型配置不是对象", "validation_failed")
    config = {"backend": backend,
              "planner_method": payload.get("planner_method") or "builtin",
              "runtime": {key: runtime[key] for key in constants.EXEC_PI_DESKTOP_KEYS if key in runtime},
              "model": {key: model[key] for key in ("planner_model", "reasoning_effort") if key in model}}
    for key in ("plugin_id", "plugin_version", "planner_skill"):
        if key in payload:
            config[key] = payload[key]
    prepared = {
        "task_id": task_id, "request_id": request_id, "run_id": run_id,
        "harness_id": backend, "repository": str(Path(repository).resolve()),
        "inputs": {"input_id": input_id, "planner_input_sha256": digest,
                   "manifest": manifest, "project_snapshot": project,
                   "input_dir": context.get("input_dir")}, "config": config,
    }
    util.require(len(util.jdump(prepared).encode("utf-8")) <= stage_contract.MAX_BYTES,
                 "plan_request_too_large: 规划材料超过 2 MiB", "validation_failed")
    if "stage_contract" in context:
        stage_contract.validate_contract(context["stage_contract"], expected={
            "stage": "plan", "task_id": task_id, "request_id": request_id,
            "run_id": run_id, "harness_id": backend})
    return prepared


def _plan_workspace_identity(job, workspace):
    from . import git_delivery
    root = str(Path(workspace).resolve())
    try:
        util.require(str(Path(git_delivery._value(["rev-parse", "--show-toplevel"], root)).resolve()) == root,
                     "规划目录不是独立工作树", "validation_failed")
        head = git_delivery._value(["rev-parse", "HEAD"], root)
        managed = set()
        for path in job.get("managed_material_paths") or []:
            relative = Path(path).relative_to(root)
            util.require(relative.parts[:1] == (".kaneopilot-plan-material",)
                         and ".." not in relative.parts,
                         "规划材料路径不在受管目录", "validation_failed")
            managed.add(relative.as_posix())
        # Materials may be ignored by Git. Read every registered file through
        # the same no-follow descriptor reader used for source fingerprints.
        material_records = []
        for name in sorted(managed):
            state = git_delivery._candidate_state(root, name, deadline=time.monotonic() + 30)
            util.require(state.get("kind") == "file", "规划受管材料不是普通文件", "validation_failed")
            material_records.append({"path": name, **state})
        all_records = git_delivery.source_state(root)
        records = [entry for entry in all_records if entry["path"] not in managed]
    except git_delivery.GitDeliveryError as exc:
        raise util.KanpilotError("validation_failed", exc.message) from exc
    except (OSError, ValueError) as exc:
        raise util.KanpilotError("validation_failed", "规划工作树源码身份不可读取") from exc
    lease = job.get("pool_lease")
    pool = None if not lease else {
        "slot_id": lease["slot_id"], "holder_id": lease["holder_id"],
        "lease_sha256": util.sha256_text(lease["lease_token"])}
    return {"head_sha": head, "source_sha256": util.sha256_text(util.jdump(records)),
            "pool": pool, "managed_material_sha256": util.sha256_text(util.jdump(material_records))}


def _freeze_plan_contract(job, prepared, workspace, *, resume_from=None, fake=False):
    """Bind the actual prepared root/configuration before connecting or sending."""
    from . import stage_contract
    root = str(Path(workspace).resolve())
    inputs = dict(prepared["inputs"])
    config = dict(prepared["config"])
    if fake:
        inputs["execution_mode"] = "isolated_fake"
    else:
        inputs["worktree_identity"] = _plan_workspace_identity(job, root)
    if resume_from is not None:
        inputs["resume_from"] = resume_from
    contract = stage_contract.make_contract(
        "plan", task_id=job["task_id"], request_id=job["request_id"], run_id=job["run_id"],
        harness_id=job["backend"], workspace=root, inputs=inputs, config=config,
        allowed_actions=("read_workspace", "produce_plan"))
    if "stage_contract" in job:
        stage_contract.validate_contract(job["stage_contract"], expected=contract)
    job.update(stage_contract=contract, stage_contract_required=True, worktree_path=root)
    return contract


def _verify_plan_contract_workspace(job, contract):
    util.require(str(Path(job.get("worktree_path") or "").resolve()) == contract["workspace"],
                 "规划契约工作树与实际目录不符", "validation_failed")
    if contract["inputs"].get("execution_mode") != "isolated_fake":
        util.require(contract["inputs"].get("worktree_identity") ==
                     _plan_workspace_identity(job, contract["workspace"]),
                     "规划契约源码或槽位身份已变化", "validation_failed")


def _plan_stage_result(job, output, *, evidence=None):
    from . import stage_contract
    output = stage_contract.validate_plan_result(output)
    util.require(not job.get("stage_contract_required") or "stage_contract" in job,
                 "规划新轮次缺少冻结契约", "validation_failed")
    if "stage_contract" in job:
        contract = stage_contract.validate_contract(job["stage_contract"], expected={
            "stage": "plan", "task_id": job["task_id"],
            "request_id": job["request_id"], "run_id": job["run_id"],
            "harness_id": job["backend"]})
        _verify_plan_contract_workspace(job, contract)
        result = stage_contract.make_result(contract, output,
            evidence=[evidence] if isinstance(evidence, dict) else evidence)
        stage_contract.validate_result(result, contract,
                                       payload_validator=stage_contract.validate_plan_result)
        job["stage_result"] = result
    return output


def _failed_stage_result(job, detail, code):
    if "stage_contract" in job and "stage_result" not in job:
        from . import stage_contract
        job["stage_result"] = stage_contract.make_result(
            job["stage_contract"], {"code": code or "planner_failed",
                                    "detail": detail or "规划未完成"}, outcome="failed")


# ------------------------------------------------------------ 子进程

def _terminate(proc, grace: float = 5.0) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _has_data(fd) -> bool:
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        return bool(ready)
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------ 单轮执行

def _timeout_seconds(conn) -> int:
    for key in ("codex_timeout_seconds", "executor_timeout_seconds"):
        value = db.get_setting(conn, key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return DEFAULT_PLANNER_TIMEOUT_SECONDS


def _heartbeat(db_path: Path, request_id: str, run_id: str,
               progress: Optional[str]) -> None:
    cli.run("heartbeat", {
        "request_id": request_id, "run_id": run_id,
        "progress": progress or "planning",
    }, db_path)


def _report(db_path: Path, request_id: str, run_id: str, outcome: str,
            note: str, failure_code: Optional[str] = None,
            evidence: Optional[dict] = None) -> None:
    payload = {
        "request_id": request_id, "run_id": run_id,
        "outcome": outcome, "note": note,
    }
    if failure_code:
        payload["failure_code"] = failure_code
        payload["failure_reason"] = note
    if evidence is not None:
        payload["evidence"] = evidence
    cli.run("report", payload, db_path)


def _mark_round_finished(conn, task_id: str, run_id: str, status: str,
                         now: str) -> None:
    conn.execute(
        "UPDATE execution_rounds SET run_status=?, updated_at=? "
        "WHERE task_id=? AND run_id=?",
        (status, now, task_id, run_id),
    )
    conn.commit()


def _fail_unknown_backend(db_path: Path, run_dir: Path, request_id: str,
                          run_id: str, task_id: str, backend) -> None:
    """非法 ``planner_backend`` → 明确失败（§5.24.1「不暗换后端」）。

    只写本轮的 job/终态事件与一条 failed 汇报，绝不改走后端默认值执行。
    """
    detail = (f"planner_backend 非法：{backend!r}"
              f"（允许值 {constants.PLANNER_BACKENDS}）")
    _write_json(run_dir / dsh_events.JOB_NAME, {
        "task_id": task_id, "run_id": run_id, "backend": str(backend),
        "session_id": None, "turn_id": None, "status": "failed",
        "send_state": "not_sent",
        "pid": os.getpid(), "started_at": util.now_iso(),
        "ended_at": util.now_iso(), "exit_code": 1, "detail": detail,
    })
    log = _EventLog(run_dir / dsh_events.EVENT_LOG_NAME)
    log.emit("terminal", util.now_iso(), outcome="failed",
             reason="plan_backend_invalid")
    conn = db.connect(db_path)
    try:
        _mark_round_finished(conn, task_id, run_id, "failed", util.now_iso())
    finally:
        conn.close()
    _report(db_path, request_id, run_id, "failed", detail,
            failure_code="planner_failed",
            evidence={"error_category": "plan_backend_invalid"})


def _verify_partial_materials(context: dict) -> None:
    """Verify frozen partial file copies before any backend or fake dispatch."""
    from . import planner_inputs

    manifest = (context.get("payload") or {}).get("manifest") or {}
    if not isinstance(manifest, dict):
        return
    files = manifest.get("files") or []
    if not isinstance(files, list):
        if manifest.get("execution_worktree") is not None:
            raise util.KanpilotError("validation_failed", "重新规划材料文件清单不合法")
        return
    for entry in files:
        if not isinstance(entry, dict) or entry.get("source") != "execution_worktree":
            continue
        relative = entry.get("path")
        input_dir = context.get("input_dir")
        if not isinstance(relative, str) or not relative or not input_dir:
            raise util.KanpilotError("validation_failed", "重新规划材料路径缺失，请重新准备材料")
        content, reason = planner_inputs._read_worktree_material(
            str(input_dir), relative, constants.PLAN_INPUT_FILE_MAX_BYTES)
        if content is None or util.sha256_bytes(content) != entry.get("sha256"):
            raise util.KanpilotError(
                "validation_failed", "重新规划材料已变化或不可读取，请重新准备材料："
                + relative + "（" + (reason or "sha256_mismatch") + "）")


def _fail_planner_input(db_path, run_dir, request_id, run_id, task_id, backend, detail):
    now = util.now_iso()
    _write_json(run_dir / dsh_events.JOB_NAME, {
        "task_id": task_id, "run_id": run_id, "backend": str(backend),
        "session_id": None, "turn_id": None, "status": "failed",
        "send_state": "not_sent",
        "pid": os.getpid(), "started_at": now, "ended_at": now,
        "exit_code": 1, "detail": detail,
    })
    _EventLog(run_dir / dsh_events.EVENT_LOG_NAME).emit(
        "terminal", now, outcome="failed", reason="plan_input_mismatch")
    conn = db.connect(db_path)
    try:
        _mark_round_finished(conn, task_id, run_id, "failed", now)
    finally:
        conn.close()
    _report(db_path, request_id, run_id, "failed", detail,
            failure_code="planner_failed",
            evidence={"error_category": "plan_input_mismatch"})

# ------------------------------------------------------------ 失败分类（§5.22.6）
_AUTH_RE = re.compile(
    r"(?i)(?<!\d)(401|403)(?!\d)|unauthorized|forbidden|"
    r"authentication|认证|未授权|登录态"
)
_MODEL_RE = re.compile(
    r"(?i)model is not supported|not supported|unsupported model|"
    r"unknown model|model.{0,40}not (supported|available|found)|"
    r"模型.{0,10}(不支持|不可用|未知)"
)
_HTTP4XX_RE = re.compile(
    r"(?i)(?:http|status(?:es)?|响应|code)[\s_:：=]*4\d{2}\b|"
    r"\b4\d{2}\b[^。\n]{0,24}(?:error|bad request|rejected|被拒)"
)

_FAILURE_LABELS = {
    "auth_failed": "codex 认证失败",
    "model_unavailable": "规划模型请求被拒绝",
    "request_rejected": "规划请求被拒绝",
    "process_exit": "codex 异常退出",
    "crash": "codex 进程崩溃",
}


def _classify_failure(returncode, errors: list, tail: list,
                      http_status=None):
    """非零退出 → (reason, label)。reason 为 §5.22.6 分类词，label 是
    report note 的中文前缀（``<分类名>：<主要原因>`` 的前半段）。

    判定序：信号/>128 → crash；错误文本命中认证 → auth_failed；模型不被
    支持 → model_unavailable；HTTP 4xx → request_rejected；其余 →
    process_exit（注明退出码）。错误文本与 stdout/stderr 尾部共同作据。
    """
    haystack = "\n".join(
        [str(e) for e in errors] + [str(t) for t in tail]
    )[-8000:]
    suffix = f"（HTTP {http_status}）" if http_status else ""
    if isinstance(returncode, int) and (returncode < 0 or returncode > 128):
        label = _FAILURE_LABELS["crash"] + (
            f"（信号 {-returncode}）" if returncode < 0
            else f"（退出码 {returncode}）"
        )
        return "crash", label
    if _AUTH_RE.search(haystack):
        return "auth_failed", _FAILURE_LABELS["auth_failed"] + suffix
    if _MODEL_RE.search(haystack):
        return "model_unavailable", _FAILURE_LABELS["model_unavailable"] + suffix
    if (isinstance(http_status, int) and 400 <= http_status < 500) \
            or _HTTP4XX_RE.search(haystack):
        return "request_rejected", _FAILURE_LABELS["request_rejected"] + suffix
    label = _FAILURE_LABELS["process_exit"] + (
        f"（退出码 {returncode}）" if returncode is not None else ""
    )
    return "process_exit", label


def run_one(db_path: Path, request_id: Optional[str] = None) -> bool:
    lifecycle = {}
    try:
        return _run_one(db_path, request_id, lifecycle=lifecycle)
    finally:
        job = lifecycle.get("job")
        if job and job.get("pool_lease"):
            task_workspace.release_pool(db_path, job, lifecycle["run_dir"])
            _write_json(lifecycle["run_dir"] / dsh_events.JOB_NAME, job)


def _run_one(db_path: Path, request_id: Optional[str] = None, *, lifecycle) -> bool:
    """领取并执行一个规划轮次。返回 True=处理了一个请求（可继续领取）。"""
    claim = cli.run("claim", {
        "stage": "plan", "executor": constants.PLANNER_EXECUTOR_ID,
        **({"request_id": request_id} if request_id else {}),
    }, db_path)
    if not claim.get("claimed"):
        return False

    req_id, run_id = claim["request_id"], claim["run_id"]
    context = claim.get("context") or {}
    req_payload = context.get("payload") or {}
    task = context.get("task") or {}
    task_id = context.get("task_id") or task.get("task_id")
    lease_seconds = int(context.get("lease_seconds")
                        or constants.DEFAULT_LEASE_SECONDS)
    # §5.22.3：心跳 ≤ min(lease/3, 60)（不再设下限，短租约须更密）。
    heartbeat_every = min(
        lease_seconds / 3.0,
        float(constants.PLANNER_HEARTBEAT_MAX_SECONDS))


    conn = db.connect(db_path)
    try:
        source = headless_adapter.resolve_source(conn)
        run_dir = source.run_dir(task_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        timeout = _timeout_seconds(conn)
        # 轮次索引行已由 claim 写入（backend=codex）；此处标记进程启动。
        conn.execute(
            "UPDATE execution_rounds SET run_status='running', pid=?, "
            "started_at=COALESCE(started_at, ?), updated_at=? "
            "WHERE task_id=? AND run_id=?",
            (os.getpid(), util.now_iso(), util.now_iso(), task_id, run_id),
        )
        conn.commit()
    finally:
        conn.close()

    # v4 §5.24.3：规划后端分流。payload 缺 planner_backend 一律按 codex 解释
    #（旧请求语义不变）；以下 codex 分支一字未改，Plus 分支整体交
    # ``pi_desktop_planner``（独立规划 worktree + plan 会话 + plan proposal 读取）。
    backend = req_payload.get("planner_backend") or constants.PLANNER_BACKEND
    try:
        if backend in ("codex", "claude", "pi_desktop"):
            _prepare_stage_inputs(context, task_id, req_id, run_id, backend)
        _verify_partial_materials(context)
        from . import task_workspace
        conn = db.connect(db_path)
        try:
            workspace_task = db.require_task(conn, task_id)
            task_workspace.verify_plan_workspace(workspace_task, req_payload)
            from . import review_resolution
            review_resolution.verify_plan_cycle(conn, workspace_task, req_payload)
        finally:
            conn.close()
    except util.KanpilotError as exc:
        _fail_planner_input(db_path, run_dir, req_id, run_id, task_id, backend, exc.message)
        return True
    if backend == "pi_desktop":
        from . import pi_desktop_planner

        return pi_desktop_planner.run_one(
            db_path, req_id, run_id, context=context, task=task,
            task_id=task_id, run_dir=run_dir, timeout=timeout,
            heartbeat_every=heartbeat_every,
        )
    if backend == "claude":
        from . import claude_planner
        return claude_planner.run_one(
            db_path, req_id, run_id, context=context, task=task,
            task_id=task_id, run_dir=run_dir, timeout=timeout,
            heartbeat_every=heartbeat_every,
        )
    if backend != constants.PLANNER_BACKEND:
        # 非法/未知值 → 明确失败，不静默改走 codex（§5.24.1「不暗换后端」）。
        _fail_unknown_backend(db_path, run_dir, req_id, run_id, task_id,
                              backend)
        return True

    method = req_payload.get("planner_method") or "builtin"
    job = {
        "task_id": task_id, "request_id": req_id, "run_id": run_id, "backend": "codex",
        "session_id": None, "session_title": None,
        "config": req_payload.get("model_config"),
        "input_id": context.get("input_id"),
        "planner_input_sha256": req_payload.get("planner_input_sha256"),
        "planner_method": method,
        "pid": os.getpid(), "status": "running",
        "started_at": util.now_iso(), "ended_at": None,
        "exit_code": None, "detail": None, "send_state": "not_sent",
    }
    if "stage_contract" in context:
        job["stage_contract"] = context["stage_contract"]
    lifecycle.update(job=job, run_dir=run_dir)
    # §5.23.2：spec_plan 记录固化的插件身份/版本（有则）；不写凭据。
    if method == "spec_plan":
        if req_payload.get("plugin_id"):
            job["plugin_id"] = req_payload["plugin_id"]
        if req_payload.get("plugin_version"):
            job["plugin_version"] = req_payload["plugin_version"]
    # §5.22.3/T1：启动前解析 config.toml 的 model_provider——
    # --ignore-user-config 会跳过服务商定义，必须经 -c 覆写显式传回。
    # 解析失败（配置损坏/引用缺失/认证缺方式）→ 明确失败，禁止静默回落。
    provider = None
    provider_error = None
    try:
        provider = codex_planner.codex_provider.resolve_provider(
            constants.codex_home())
    except util.KanpilotError as exc:
        provider_error = exc.message
    # 运行记录只存脱敏摘要（§7：无凭据值）。
    if provider is not None and provider.provider_id:
        job["provider"] = provider.summary
    else:
        job["provider"] = {
            "provider_id": None, "detail": "codex 默认认证路径"}
    if provider_error:
        job["detail"] = f"codex 配置错误：{provider_error}"
    _write_json(run_dir / dsh_events.JOB_NAME, job)
    # 脱敏凭据集：provider.env 注入值（内联 token）+ env_key 指向的环境
    # 变量值（redact 只替换长度 ≥8 的值；HTTP 状态/模型名不受影响）。
    secrets = [
        value for value in (provider.env if provider else {}).values()
        if value
    ]
    if provider is not None and provider.env_key:
        env_secret = os.environ.get(provider.env_key)
        if env_secret:
            secrets.append(env_secret)
    log = _EventLog(
        run_dir / dsh_events.EVENT_LOG_NAME,
        redact=lambda text: util.redact(text, secrets),
    )
    codex_log_path = run_dir / constants.PLANNER_CODEX_LOG_NAME
    manifest = req_payload.get("manifest") or {}
    input_dir = context.get("input_dir")

    def finalize(status: str, exit_code, detail: str, outcome: str,
                 reason: str) -> None:
        if status == "failed":
            _failed_stage_result(job, detail, reason)
        job.update({
            "status": status, "exit_code": exit_code,
            "ended_at": util.now_iso(), "detail": detail,
        })
        _write_json(run_dir / dsh_events.JOB_NAME, job)
        log.emit("terminal", util.now_iso(), outcome=outcome, reason=reason)
        conn2 = db.connect(db_path)
        try:
            _mark_round_finished(
                conn2, task_id, run_id,
                "finished" if status == "finished" else "failed",
                util.now_iso(),
            )
        finally:
            conn2.close()

    fake = os.environ.get("KANPILOT_CODEX_FAKE")
    if fake:
        prepared = _prepare_stage_inputs(context, task_id, req_id, run_id, backend)
        _freeze_plan_contract(job, prepared, prepared["repository"], fake=True)
        # fake 下服务商解析失败只记 detail、不阻断（运行记录完整性）；fake
        # 各分支若再写 detail，把解析错误合并保留。
        if provider_error:
            inner_finalize = finalize
            def finalize(status, exit_code, detail, outcome, reason,
                         _inner=inner_finalize):
                merged = (
                    f"{detail or ''} | codex 配置错误：{provider_error}"
                    if detail else f"codex 配置错误：{provider_error}"
                )
                _inner(status, exit_code, merged, outcome, reason)
        return _run_fake(db_path, run_dir, job, log, context, manifest,
                         req_id, run_id, fake, finalize)

    # ---- 真实 codex exec ----
    if provider_error:
        # 服务商配置损坏/缺失 → 明确失败（不静默回落官方服务商）。
        detail = f"codex 配置错误：{provider_error}"
        finalize("failed", 1, detail, "failed", "config_error")
        _report(db_path, req_id, run_id, "failed", detail,
                failure_code="planner_failed",
                evidence={"codex_log": str(codex_log_path),
                          "error_category": "config_error"})
        return True

    if method == "spec_plan":
        # §5.23.2：领取时复检插件仍启用；被移除/禁用 → 明确失败
        #（report failed，材料保留），**不静默降级 builtin**。
        plugin = codex_planner.probe_spec_plugin()
        if not plugin.get("available"):
            detail = (f"spec-planner 插件不可用（plugin_unavailable）："
                      f"{plugin.get('reason')}")
            finalize("failed", 1, detail, "failed", "plugin_unavailable")
            _report(db_path, req_id, run_id, "failed", detail,
                    failure_code="plan_failed",
                    evidence={"codex_log": str(codex_log_path),
                              "error_category": "plugin_unavailable"})
            return True
        # user config 未被忽略：服务商由 codex 自身解析，但认证就绪
        # （auth.json / env_key 环境值）仍需在拉起前确认（§5.23.2）。
        if provider_error or not provider.auth_ready:
            detail = (
                f"codex 配置错误：{provider_error}"
                if provider_error
                else f"codex 认证未就绪：{provider.auth_detail}")
            finalize("failed", 1, detail, "failed", "config_error")
            _report(db_path, req_id, run_id, "failed", detail,
                    failure_code="planner_failed",
                    evidence={"codex_log": str(codex_log_path),
                              "error_category": "auth_not_ready"})
            return True

    conn = db.connect(db_path)
    try:
        plan_cwd = task_workspace.prepare_plan_pool(conn, db_path, db.require_task(conn, task_id),
            req_payload, job, run_dir / dsh_events.JOB_NAME)
        _freeze_plan_contract(job, _prepare_stage_inputs(context, task_id, req_id, run_id, backend), plan_cwd)
        _write_json(run_dir / dsh_events.JOB_NAME, job)
    except (util.KanpilotError, OSError) as exc:
        if isinstance(exc, util.KanpilotError) and exc.code == "worktree_pool_waiting":
            finalize("finished", 0, exc.message, "completed", exc.code)
            return True
        detail = exc.message if isinstance(exc, util.KanpilotError) else str(exc)
        finalize("failed", 1, detail, "failed", "plan_worktree_failed")
        _report(db_path, req_id, run_id, "failed", detail, failure_code="planner_failed")
        return True
    finally:
        conn.close()

    schema_path = run_dir / "output-schema.json"
    out_path = run_dir / "last-message.txt"
    _write_json(schema_path, codex_planner.output_schema())
    images = codex_planner.image_files(
        manifest, Path(input_dir) if input_dir else None
    )
    prompt = codex_planner.build_prompt(
        dict(task, fix_mode=req_payload.get("fix_mode"), plan_cwd=str(plan_cwd)), manifest, Path(input_dir) if input_dir else None,
        method=method,
    )
    job_for_argv = {
        "plan_cwd": str(plan_cwd),
        "model_config": req_payload.get("model_config"),
        "repository": task.get("repository")
        or (manifest.get("task") or {}).get("repository"),
        "planner_method": method,
    }
    argv = codex_planner.launcher_argv(
        job_for_argv, schema_path, out_path, images, provider=provider)
    env = codex_planner.child_env(provider)

    log.emit("session", util.now_iso(),
             session_id=None, session_title=f"codex plan {task_id}")
    log.emit("user", util.now_iso(), content=prompt)

    deadline = time.monotonic() + timeout
    next_heartbeat = time.monotonic() + heartbeat_every
    try:
        job["send_state"] = "unknown"
        conn = db.connect(db_path)
        try:
            task_workspace.pool_owner(conn, job, state="running", pid=os.getpid())
        finally:
            conn.close()
        _write_json(run_dir / dsh_events.JOB_NAME, job)
        proc = subprocess.Popen(
            argv, cwd=str(plan_cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        job["send_state"] = "not_sent"
        finalize("failed", 1, f"launch failed: {exc}", "failed",
                 "launch_failed")
        _report(db_path, req_id, run_id, "failed",
                f"codex 拉起失败：{exc}", failure_code="planner_failed")
        return True
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    except (OSError, BrokenPipeError):
        pass

    state = {
        "eof": False, "turn_end": False, "stale": False,
        # §5.22.6 错误证据：system/error 帧的可读文本（按内容去重）与
        # stdout/stderr 尾部（非 JSON 行也算入），供非零退出分类。
        "errors": [], "errors_set": set(), "tail": [],
        "emitted": set(),  # system 事件 content 去重（相同错误只显示一次）
        "http_status": None,
    }
    buffer = bytearray()
    fd = proc.stdout.fileno()
    raw_log = open(codex_log_path, "ab")

    def _collect_error(text) -> None:
        """错误帧/系统事件文本 → state['errors']（脱敏 + 按内容去重）。"""
        text = util.redact(str(text), secrets).strip()
        if text and text not in state["errors_set"]:
            state["errors_set"].add(text)
            state["errors"].append(text)

    def _note_http_status(frame) -> None:
        """原始 JSONL 帧（顶层 error/turn.failed/item）→ http_status 提取。"""
        if not isinstance(frame, dict):
            return
        for holder in (frame.get("error"), frame):
            status = codex_planner.http_status_of(holder)
            if isinstance(status, int) and 400 <= status <= 599:
                state["http_status"] = status
                return

    def handle_line(raw: bytes) -> None:
        # codex.jsonl 保留完整行但先脱敏（凭据值不落盘）。
        text = raw.decode("utf-8", errors="replace")
        line = util.redact(text, secrets).strip()
        raw_log.write(line.encode("utf-8") + b"\n")
        raw_log.flush()
        if not line:
            return
        state["tail"].append(line[:500])
        del state["tail"][:-40]
        try:
            message = json.loads(line)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        # 原始 error/turn.failed/item.completed(error) 帧计入错误证据。
        if message.get("type") in ("error", "turn.failed"):
            err = message.get("error")
            _collect_error(
                codex_planner.error_text(err) if err is not None
                else codex_planner.error_text(message.get("message")))
            _note_http_status(message)
        elif message.get("type") == "item.completed":
            item = message.get("item") or {}
            if isinstance(item, dict) and item.get("error") is not None:
                _collect_error(codex_planner.error_text(item.get("error")))
            _note_http_status(item)
        parsed = codex_planner.map_event(message)
        if parsed is None:
            return
        etype, ts, fields = parsed
        if etype == "session":
            job["session_id"] = fields.get("session_id")
            job["session_title"] = fields.get("session_title")
        elif etype == "turn_end":
            state["turn_end"] = True
        elif etype == "system":
            _collect_error(fields.get("content") or "")
            code = fields.get("code")
            if (state["http_status"] is None
                    and isinstance(code, int) and not isinstance(code, bool)
                    and 400 <= code <= 599):
                state["http_status"] = code
            content = fields.get("content")
            if content is not None:
                if content in state["emitted"]:
                    return  # 相同错误已展示，不重复发事件
                state["emitted"].add(content)
        log.emit(etype, ts or util.now_iso(), **fields)

    def drain() -> None:
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                return
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            handle_line(line)

    timed_out = False
    heartbeat_failures = 0
    while not state["eof"]:
        now_m = time.monotonic()
        if now_m >= deadline:
            timed_out = True
            break
        if now_m >= next_heartbeat:
            try:
                _heartbeat(db_path, req_id, run_id, "planning")
                task_workspace.heartbeat_pool(db_path, job)
                heartbeat_failures = 0
            except util.KanpilotError as exc:
                # stale_run / 失联 → 终止并禁止发布；瞬时错误（busy/锁）
                # 连续三次才放弃，避免一次抖动杀掉整轮。
                if exc.code in ("stale_run", "conflict", "not_found"):
                    state["stale"] = True
                    job["detail"] = f"heartbeat failed: {exc}"
                    break
                heartbeat_failures += 1
                if heartbeat_failures >= 3:
                    state["stale"] = True
                    job["detail"] = f"heartbeat failed x3: {exc}"
                    break
            except Exception as exc:  # 网络/IO 级错误同样三次熔断
                heartbeat_failures += 1
                if heartbeat_failures >= 3:
                    state["stale"] = True
                    job["detail"] = f"heartbeat failed x3: {exc}"
                    break
            next_heartbeat = now_m + heartbeat_every
        if proc.poll() is not None and not _has_data(fd):
            state["eof"] = True
            break
        ready, _, _ = select.select(
            [fd], [], [], min(0.5, max(0.05, deadline - now_m))
        )
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                state["eof"] = True
                continue
            buffer.extend(chunk)
            drain()
    if buffer:
        handle_line(bytes(buffer))
    raw_log.close()

    if state["stale"]:
        _terminate(proc)
        job["owner_stopped"] = proc.poll() is not None
        finalize("failed", proc.returncode, "lease lost (stale_run)",
                 "failed", "stale_run")
        return True  # 不汇报：请求所有权已失
    if timed_out:
        _terminate(proc)
        job["owner_stopped"] = proc.poll() is not None
        finalize("failed", proc.returncode, f"timeout after {timeout:g}s",
                 "failed", "timeout")
        _report(db_path, req_id, run_id, "failed",
                f"规划超时（{timeout:g}s）", failure_code="planner_timeout",
                evidence={"codex_log": str(codex_log_path),
                          "error_category": "timeout"})
        return True

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate(proc)
    job["owner_stopped"] = proc.poll() is not None
    returncode = proc.returncode
    try:
        proc.stdout.close()
    except OSError:
        pass

    if returncode not in (0, None):
        # §5.22.6 失败分类：依据收集的错误帧 + stdout/stderr 尾部归类。
        reason, label = _classify_failure(
            returncode, state["errors"], state["tail"],
            http_status=state["http_status"])
        primary = state["errors"][0] if state["errors"] else "无错误输出"
        note = f"{label}：{primary}"[:800]
        finalize("failed", returncode, note, "failed", reason)
        _report(db_path, req_id, run_id, "failed", note,
                failure_code="planner_failed",
                evidence={"codex_log": str(codex_log_path),
                          "error_category": reason})
        return True

    # exit 0 不等于成功：须通过输出契约校验（§5.22.3）。
    try:
        output = _plan_stage_result(job, parse_output(out_path), evidence={
            "mechanism": "cli_process", "exit_code": returncode,
            "output_path": str(out_path)})
    except util.KanpilotError as exc:
        finalize("failed", returncode, str(exc), "failed",
                 "invalid_output")
        _report(db_path, req_id, run_id, "failed",
                f"规划输出不合规：{exc.message}",
                failure_code="planner_output_invalid",
                evidence={"codex_log": str(codex_log_path)})
        return True

    if output["status"] == "needs_input":
        draft_path = run_dir / "plan-draft.md"
        if output["plan_markdown"].strip():
            draft_path.write_text(output["plan_markdown"], encoding="utf-8")
        finalize("finished", 0, "needs_input", "completed", "needs_input")
        _report(db_path, req_id, run_id, "needs_user",
                "规划需要补充材料：" + plan_questions.summary(output["questions"]),
                failure_code="reported",
                evidence={
                    "questions": output["questions"], "plan_markdown": output["plan_markdown"],
                    "draft_path": str(draft_path),
                    "evidence": output["evidence"],
                })
        return True

    # ready → 生成计划文件 → publish-plan（同事务结束请求）。
    generated_at = util.now_iso()
    task_row = None
    plan_kind = (task.get("plan_kind")
                 or (manifest.get("task") or {}).get("plan_kind")
                 or "standard")
    if plan_kind == "review_fix" and task_id:
        conn = db.connect(db_path)
        try:
            task_row = review_fix_plan_task(conn, task_id, req_payload)
        except util.KanpilotError as exc:
            finalize("failed", 0, f"review report invalid: {exc.message}", "failed",
                     "publish_failed")
            _report(db_path, req_id, run_id, "failed",
                    f"计划发布被拒：{exc.message}", failure_code="planner_failed")
            return True
        finally:
            conn.close()
    try:
        plan_path = write_plan_file(context, manifest,
                                    output["plan_markdown"], generated_at,
                                    task_row=task_row)
    except OSError as exc:
        finalize("failed", 0, f"plan write failed: {exc}", "failed",
                 "plan_write_failed")
        _report(db_path, req_id, run_id, "failed",
                f"计划文件写入失败：{exc}", failure_code="planner_failed")
        return True

    model_config = req_payload.get("model_config") or {}
    try:
        cli.run("publish-plan", {
            "task_id": task_id,
            "plan_path": str(plan_path),
            "plan_kind": plan_kind,
            "source_sha256": manifest.get("source_sha256")
            or task.get("source_sha256") or "",
            "requirements_sha256": manifest.get("requirements_sha256")
            or task.get("requirements_sha256") or "",
            "generated_at": generated_at,
            "planner_model": model_config.get("planner_model") or "",
            "reasoning_effort": model_config.get("reasoning_effort") or "",
            "request_id": req_id,
            "run_id": run_id,
            "planner_input_sha256": req_payload.get("planner_input_sha256")
            or "",
        }, db_path)
    except util.KanpilotError as exc:
        finalize("failed", 0, f"publish failed: {exc.message}", "failed",
                 "publish_failed")
        _report(db_path, req_id, run_id, "failed",
                f"计划发布被拒：{exc.message}", failure_code="planner_failed")
        return True
    finalize("finished", 0, None, "completed", "plan_published")
    return True


# ------------------------------------------------------------ fake 接缝

def _run_fake(db_path: Path, run_dir: Path, job: dict, log: _EventLog,
              context: dict, manifest: dict, req_id: str, run_id: str,
              mode: str, finalize) -> bool:
    task = context.get("task") or {}
    task_id = context.get("task_id") or task.get("task_id")
    now = util.now_iso()
    log.emit("session", now, session_id=f"fake-codex-{run_id[:8]}",
             session_title="Fake codex · 规划")
    log.emit("user", now, content="fake plan prompt")
    job["session_id"] = f"fake-codex-{run_id[:8]}"
    out_path = run_dir / "last-message.txt"

    if mode == "crash":
        job.update({"status": "failed", "exit_code": 3,
                    "ended_at": util.now_iso(), "detail": "fake crash"})
        _write_json(run_dir / dsh_events.JOB_NAME, job)
        _report(db_path, req_id, run_id, "failed", "fake crash",
                failure_code="planner_failed")
        return True

    if mode == "needs_input":
        output = {
            "status": "needs_input", "plan_markdown": "# 草稿\n待定",
            "questions": ["需要确认目标版本范围"], "evidence": [],
        }
    elif mode == "invalid":
        out_path.write_text("not a json", encoding="utf-8")
        finalize("failed", 0, "invalid output", "failed", "invalid_output")
        _report(db_path, req_id, run_id, "failed",
                "规划输出不合规：fake invalid",
                failure_code="planner_output_invalid")
        return True
    elif mode == "fail":
        finalize("failed", 1, "fake failure", "failed", "crash")
        _report(db_path, req_id, run_id, "failed", "fake failure",
                failure_code="planner_failed")
        return True
    else:  # ready（默认）
        output = {
            "status": "ready",
            "plan_markdown": "# 实施计划\n\n1. 步骤一\n2. 步骤二\n\n```verify\npython3 -m unittest\n```\n",
            "questions": [], "evidence": ["manifest.json"],
        }

    out_path.write_text(json.dumps(output, ensure_ascii=False),
                        encoding="utf-8")
    log.emit("assistant_message", util.now_iso(),
             content=json.dumps(output, ensure_ascii=False))
    log.emit("turn_end", util.now_iso(), will_retry=False)
    parsed = _plan_stage_result(job, parse_output(out_path), evidence={"mechanism": "fake"})

    if parsed["status"] == "needs_input":
        finalize("finished", 0, "needs_input", "completed", "needs_input")
        _report(db_path, req_id, run_id, "needs_user",
                "规划需要补充材料：" + plan_questions.summary(parsed["questions"]),
                failure_code="reported",
                evidence={"questions": parsed["questions"], "plan_markdown": parsed["plan_markdown"]})
        return True

    generated_at = util.now_iso()
    plan_path = write_plan_file(context, manifest,
                                parsed["plan_markdown"], generated_at)
    req_payload = context.get("payload") or {}
    model_config = req_payload.get("model_config") or {}
    cli.run("publish-plan", {
        "task_id": task_id,
        "plan_path": str(plan_path),
        "plan_kind": task.get("plan_kind")
        or (manifest.get("task") or {}).get("plan_kind") or "standard",
        "source_sha256": manifest.get("source_sha256")
        or task.get("source_sha256") or "",
        "requirements_sha256": manifest.get("requirements_sha256")
        or task.get("requirements_sha256") or "",
        "generated_at": generated_at,
        "planner_model": model_config.get("planner_model") or "",
        "reasoning_effort": model_config.get("reasoning_effort") or "",
        "request_id": req_id,
        "run_id": run_id,
        "planner_input_sha256": req_payload.get("planner_input_sha256") or "",
    }, db_path)
    finalize("finished", 0, None, "completed", "plan_published")
    return True


# ------------------------------------------------------------ 入口

def _record_run(conn, **fields) -> None:
    """把本次运行合并进 settings ``executor_runs["plan"]``（executor-status
    单飞依据：App 侧 ExecutorScheduler 先查 running pid 再拉起）。"""
    runs = db.get_setting(conn, "executor_runs")
    if not isinstance(runs, dict):
        runs = {}
    entry = dict(runs.get("plan") or {})
    entry.update(fields)
    entry["stage"] = "plan"
    runs["plan"] = entry
    db.set_setting(conn, "executor_runs", runs)
    conn.commit()


def run_loop(db_path: Path, request_id: Optional[str], once: bool) -> int:
    conn = db.connect(db_path)
    try:
        log_dir = constants.app_support() / "executor-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / ("plan-%s.log" % util.now_iso()
                         .replace(":", "").replace("-", ""))
        _record_run(conn, pid=os.getpid(), started_at=util.now_iso(),
                    log=str(log), exit_code=None, finished_at=None)
    finally:
        conn.close()
    handled = 0
    rc = 0
    try:
        while True:
            try:
                progressed = run_one(db_path, request_id=request_id)
            except util.KanpilotError as exc:
                print(f"planner_runner: {exc.code}: {exc.message}",
                      file=sys.stderr)
                rc = 1
                break
            except Exception as exc:  # pragma: no cover - defensive
                print(f"planner_runner: internal: {exc}", file=sys.stderr)
                rc = 1
                break
            if not progressed:
                break
            handled += 1
            request_id = None  # 定点领取只生效一次
            if once:
                break
    finally:
        conn = db.connect(db_path)
        try:
            _record_run(conn, exit_code=rc, finished_at=util.now_iso())
        finally:
            conn.close()
    return rc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kanpilot.planner_runner")
    parser.add_argument("--request-id", dest="request_id")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", dest="db_path")
    args = parser.parse_args(argv)
    db_path = Path(args.db_path).expanduser() if args.db_path \
        else constants.default_db_path()
    return run_loop(db_path, args.request_id, args.once)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
