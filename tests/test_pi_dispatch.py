"""pi 派发状态机集成测试（protocol §5.17.3 / §5.18.3，v1.6）。

覆盖契约条款：
- §5.17.3 模型/凭据/预设不可用 → dispatch claim 确定性阻断
  （validation_failed，task needs_user，不自动换模型/agent）；
- §5.16.1 claim 创建轮次索引（backend=pi + 固化配置快照）；
- §5.18.3 pi 证据分支：prepare（config 快照全等）→ begin-send →
  dispatch-start（意图先落盘、幂等不创建第二个进程）→ confirm
  （session 事件 + config 核验）→ running；
- 未启动即 confirm / config 不符 → evidence_invalid，task 停留 submitting；
- 历史（无 backend 字段）证据分支继续可用（§5.18.3 兼容口径）。

隔离：conftest tmp_env 注入 KANPILOT_PI_HOME/BIN/AGENTS_DIR 夹具与
KANPILOT_PI_FAKE=finished（伪 worker 立即写 job.json+events.jsonl 并退出），
无真实模型调用。
"""

import json
import os
import time

try:
    from conftest import ui_evidence
except ImportError:  # unittest discover 包上下文
    from tests.conftest import ui_evidence


# ------------------------------------------------------------------ 工具

def _claim_dispatch(factory, cli, ready):
    factory.enqueue_dispatch(ready.task.task_id)
    req = factory.find_request(ready.task.task_id, "dispatch")
    return factory.claim("dispatch", request_id=req["request_id"])


def _pi_prepare_evidence(ctx, **extra):
    evidence = {
        "session_marker": ctx["session_marker"],
        "repository": ctx["repository"],
        "backend": "pi",
        "config": ctx["exec_config"],
    }
    evidence.update(extra)
    return evidence


def _pi_confirm_evidence(ctx, session_id, **extra):
    evidence = _pi_prepare_evidence(ctx)
    evidence.update({"session_id": session_id, "protocol_seen": True})
    evidence.update(extra)
    return evidence


def _to_submitting(factory, cli, ready, claim):
    """prepare(pi) + begin-send → task submitting（dispatch-start 前置）。"""
    task_id, run_id = ready.task.task_id, claim["run_id"]
    prep = cli("dispatch-prepare", {
        "task_id": task_id, "run_id": run_id,
        "evidence": _pi_prepare_evidence(claim["context"]),
    })
    assert prep["status"] == "prepared"
    sent = cli("dispatch-begin-send", {"task_id": task_id, "run_id": run_id})
    assert sent["status"] == "submitting"
    return task_id


def _round(cli, task_id, run_id):
    """经 get-task.execution_round 读轮次索引（protocol §5.18.5）。"""
    detail = cli("get-task", {"task_id": task_id})["task"]
    rnd = detail.get("execution_round")
    assert rnd is not None, "claim 后应有轮次索引行"
    assert rnd["run_id"] == run_id
    return rnd


def _wait_session_events(run_dir, timeout=5.0):
    """等伪 worker 把 session 事件刷盘（dispatch-start 也会等，这里兜底）。"""
    path = run_dir / "events.jsonl"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and '"type": "session"' in path.read_text(
            encoding="utf-8"
        ).replace('"type":"session"', '"type": "session"'):
            return
        time.sleep(0.05)
    raise AssertionError(f"超时未见 session 事件：{path}")


# ------------------------------------------------------------------ 用例

class TestPiClaimContext:
    def test_claim_creates_round_with_config_snapshot(self, factory, cli):
        ready = factory.make_ready()
        c = _claim_dispatch(factory, cli, ready)
        assert c["claimed"] is True
        ctx = c["context"]
        # §5.18.3：新 claim 恒 backend=pi + exec_config 快照 + 无附件指令。
        assert ctx["backend"] == "pi"
        assert set(ctx["exec_config"]) == {
            "provider", "model", "reasoning_effort", "agent_preset",
        }
        assert ctx["exec_config"]["provider"] == "CloseAI"
        assert ctx["exec_config"]["model"] == "deepseek-flash"
        assert ctx["exec_config"]["reasoning_effort"] == "xhigh"
        assert ctx["exec_config"]["agent_preset"] == "default"
        # 无附件形态：指令引用计划文件绝对路径（§5.18.3）。
        assert ctx["plan_path"] in ctx["instruction"]
        assert "任务标识" in ctx["instruction"]
        # §5.16.1：claim 即建轮次行，backend=pi、快照固化、save_state=active。
        rnd = _round(cli, ready.task.task_id, c["run_id"])
        assert rnd["backend"] == "pi"
        assert rnd["config"] == ctx["exec_config"]
        assert rnd["save_state"] == "active"
        assert rnd["started_at"] is None

    def test_config_change_does_not_touch_existing_round(self, factory, cli):
        ready = factory.make_ready()
        c = _claim_dispatch(factory, cli, ready)
        old = dict(c["context"]["exec_config"])
        cli("set-exec-config", {"reasoning_effort": "high"})
        rnd = _round(cli, ready.task.task_id, c["run_id"])
        assert rnd["config"] == old, "设置变更只影响新轮次（§5.17.3）"


class TestDispatchConfigGate:
    def test_pi_home_unavailable_blocks_claim(self, factory, cli, tmp_env):
        """§5.17.3：能力检查失败 → claim 确定性阻断、task needs_user
        （validation_failed），不自动换模型/agent。"""
        ready = factory.make_ready()
        # 指向空目录 → models.json/agent preset 检查失败。
        empty_home = tmp_env.root / "pi-home-empty"
        empty_home.mkdir(exist_ok=True)
        factory.enqueue_dispatch(ready.task.task_id)
        req = factory.find_request(ready.task.task_id, "dispatch")
        c = _claim_with_env(
            factory, cli, req["request_id"],
            {"KANPILOT_PI_HOME": str(empty_home)},
        )
        assert c["claimed"] is False, c
        assert c["reason"] == "guard_failed", c
        assert c["code"] == "validation_failed", c
        detail = (c.get("detail") or "")
        assert "pi 执行环境不可用" in detail, detail
        t = factory.get_task(ready.task.task_id)
        assert t["status"] == "needs_user"
        assert t["failure_code"] == "validation_failed"


class TestPiDispatchFlow:
    def test_full_flow_prepare_start_confirm(self, factory, cli):
        ready = factory.make_ready()
        c = _claim_dispatch(factory, cli, ready)
        assert c["claimed"] is True
        task_id = _to_submitting(factory, cli, ready, c)

        started = cli("dispatch-start",
                      {"task_id": task_id, "run_id": c["run_id"]})
        assert started["deduplicated"] is False
        assert started["pid"] and started["pid"] > 0
        assert started["worktree_path"]
        # KANPILOT_PI_FAKE=finished：伪 worker 立即写 session 事件。
        assert started["status"] == "started"
        assert started["session_id"], "finished 模式应已产出 session_id"

        # §5.18.3 幂等：同一轮次重复 dispatch-start 绝不创建第二个进程。
        again = cli("dispatch-start",
                    {"task_id": task_id, "run_id": c["run_id"]})
        assert again["deduplicated"] is True
        assert again["pid"] == started["pid"]

        confirm = cli("dispatch-confirm", {
            "task_id": task_id, "run_id": c["run_id"],
            "evidence": _pi_confirm_evidence(
                c["context"], started["session_id"]
            ),
        })
        assert confirm["status"] == "running"

        t = factory.get_task(task_id)
        assert t["status"] == "running"
        assert t["send_state"] == "sent" and t["sent"] == 1
        assert t["session_id"] == started["session_id"]

        rnd = _round(cli, task_id, c["run_id"])
        assert rnd["started_at"] is not None
        assert rnd["pid"] == started["pid"]
        assert rnd["worktree_path"] == started["worktree_path"]
        # confirm 后会话关联回填到轮次行（§5.16.1 发现类字段刷新）。
        assert rnd["session_id"] == started["session_id"]

        # monitor 请求已建（既有流程不变，§5.18.3）。
        reqs = cli("list-requests", {"status": "queued"})
        kinds = {r["kind"] for r in reqs["requests"] if r["task_id"] == task_id}
        assert "monitor" in kinds

        # 运行记录现场：spec.json + job.json + events.jsonl 落盘。
        runs_root = cli("overview", {})["paths"]["session_runs_root"]
        run_dir = (pathlib_path(runs_root) / task_id / c["run_id"])
        spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
        assert spec["v"] == 1
        assert spec["task_id"] == task_id and spec["run_id"] == c["run_id"]
        assert spec["config"] == c["context"]["exec_config"]
        assert spec["pi_home"], "spec 必须携带隔离 pi_home"
        assert spec["agent_md"], "spec 必须携带 agent.md 路径"
        assert spec["plan_path"] in spec["instruction"]
        job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
        assert job["backend"] == "pi"
        assert job["task_id"] == task_id and job["run_id"] == c["run_id"]
        assert job["status"] == "finished" and job["exit_code"] == 0
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        assert events[0]["type"] == "session"  # 首事件必须是 session
        assert events[0]["session_id"] == started["session_id"]
        assert events[-1]["type"] == "terminal"
        assert events[-1]["outcome"] == "completed"
        # seq 单调递增
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        # get-session round_detail（§5.18.4）：terminal + 进程结果可见。
        sess = cli("get-session",
                   {"task_id": task_id, "run_id": c["run_id"]})
        detail = sess.get("round_detail")
        assert detail is not None, "get-session 应返回 round_detail"
        assert detail["backend"] == "pi"
        assert detail["terminal"] == {"outcome": "completed", "reason": "ok"}
        assert detail["exit_code"] == 0

    def test_prepare_rejects_config_mismatch(self, factory, cli):
        """§5.18.3：prepare 的 config 必须全等于该轮快照。"""
        ready = factory.make_ready()
        c = _claim_dispatch(factory, cli, ready)
        bad = _pi_prepare_evidence(c["context"])
        bad["config"] = dict(bad["config"], model="deepseek-v3-pro")
        err = _expect_error(cli, "dispatch-prepare", {
            "task_id": ready.task.task_id, "run_id": c["run_id"],
            "evidence": bad,
        })
        assert err["code"] == "evidence_invalid"

    def test_confirm_requires_start_intent(self, factory, cli):
        """未 dispatch-start 即 confirm → evidence_invalid，task 停留 submitting。"""
        ready = factory.make_ready()
        c = _claim_dispatch(factory, cli, ready)
        task_id = ready.task.task_id
        cli("dispatch-prepare", {
            "task_id": task_id, "run_id": c["run_id"],
            "evidence": _pi_prepare_evidence(c["context"]),
        })
        cli("dispatch-begin-send",
            {"task_id": task_id, "run_id": c["run_id"]})
        err = _expect_error(cli, "dispatch-confirm", {
            "task_id": task_id, "run_id": c["run_id"],
            "evidence": _pi_confirm_evidence(
                c["context"], "sess-whatever"
            ),
        })
        assert err["code"] == "evidence_invalid"
        assert factory.get_task(task_id)["status"] == "submitting"

    def test_confirm_rejects_unknown_session(self, factory, cli):
        """session_id 与协议会话事件不符 → evidence_invalid（§5.18.3）。"""
        ready = factory.make_ready()
        c = _claim_dispatch(factory, cli, ready)
        task_id = _to_submitting(factory, cli, ready, c)
        cli("dispatch-start", {"task_id": task_id, "run_id": c["run_id"]})
        err = _expect_error(cli, "dispatch-confirm", {
            "task_id": task_id, "run_id": c["run_id"],
            "evidence": _pi_confirm_evidence(
                c["context"], "sess-not-the-real-one"
            ),
        })
        assert err["code"] == "evidence_invalid"
        assert factory.get_task(task_id)["status"] == "submitting"

    def test_legacy_evidence_branch_still_works(self, factory, cli):
        """历史（无 backend 字段）证据分支保留：ATIF 兼容路径不回归（§5.18.3）。"""
        ready = factory.make_ready()
        c = _claim_dispatch(factory, cli, ready)
        task_id = ready.task.task_id
        ev = ui_evidence(
            session_marker=c["context"]["session_marker"],
            repository=c["context"]["repository"],
            attachment_name=c["context"]["attachment_name"],
        )
        prep = cli("dispatch-prepare", {
            "task_id": task_id, "run_id": c["run_id"], "evidence": ev,
        })
        assert prep["status"] == "prepared"
        cli("dispatch-begin-send",
            {"task_id": task_id, "run_id": c["run_id"]})
        ev2 = dict(ev, message_visible=True, session_id="legacy-sess",
                   session_title="legacy")
        confirm = cli("dispatch-confirm", {
            "task_id": task_id, "run_id": c["run_id"], "evidence": ev2,
        })
        assert confirm["status"] == "running"


# ------------------------------------------------------------------ 工具实现

def _claim_with_env(factory, cli, request_id, env_overlay):
    """在临时 env 覆盖下执行 dispatch claim（CLI 进程级隔离）。"""
    saved = {k: cli.env.get(k) for k in env_overlay}
    cli.env.update(env_overlay)
    try:
        return factory.claim("dispatch", request_id=request_id)
    finally:
        for key, value in saved.items():
            if value is None:
                cli.env.pop(key, None)
            else:
                cli.env[key] = value


def pathlib_path(text):
    from pathlib import Path

    return Path(text)


def _expect_error(cli, command, payload):
    """执行期望失败的命令，返回 {"error","code"} 错误信封（§1）。"""
    import pytest

    try:
        result = cli(command, payload, expect=1)
    except AssertionError as exc:
        pytest.fail(f"{command} 应以 exit 1 失败：{exc}")
    return result


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
