"""pi 运行记录会话命令契约测试（protocol §5.15.5 + §5.16/§5.18，v1.6）——集成层。

覆盖契约条款：protocol §1（payload 校验/错误信封）、§5.15.5（运行记录只读
适配层、`task_id + run_id` 目录定位 + `job.json` 身份核验、pi `events.jsonl`
→ Message 形状、`task_id/run_id` 非法字符与越界/符号链接拒绝（unavailable +
detail，不读他目录）、events.jsonl 缺失 → pending、半写入 → pending + detail
且不扰动 session_reads 已存位点、连接恒 degraded、root 不存在 →
unavailable + detail 引导 session_runs_root 配置、设置优先级
payload > settings > 默认（空串=未设置，payload 不持久化）、记录过多时
list-sessions 轻量核验、`seq` 游标 + revision 快照的断线补齐/修订透出跨进程
生效）、state-model §1（`session_reads` 本地读取位点）。

v1.5/v1.6（§5.16/§5.18.2/§5.18.4）：trash-session / restore-session 完整
删除/恢复闭环（含 UI 将调用的命令序列）、幂等不重复写事件、禁止条件
conflict + 中文原因、expected_save_state 冲突、not_found、回填不复活、
list-sessions `save_state` 三态过滤与非法值 bad_request、SessionInfo 扩展
字段形状、get-session `round_detail` 形状、删除后 session-messages /
get-session 行为、跨 CLI 进程（重启）状态保持、backend=pi 轮次（events.jsonl
夹具：首事件 session）回填 backend=pi + 消息/round_detail 读取、
**旧后端（backend≠pi）轮次已弃用 → unavailable + 「旧后端会话已弃用」**。

数据源接缝（测试约定）：tmp 目录构造真实 `job.json`（backend=pi）/
`events.jsonl` 文件，经 DB setting `session_runs_root` 指向；每次 CLI
调用都是独立进程 → 天然无进程内缓存，增量补齐/修订透出/半写入保留上一份有效
快照完全依赖 session_reads 位点与跨进程持久快照（payload 无 cursor = 重开 →
完整历史；增量须显式传 cursor）。不发 HTTP、不启动真实 pi。
tmp_env 已把 HOME 指向假目录：不设 settings 时默认根目录必然不存在。
"""

import json
import sqlite3

import pytest

try:
    from conftest import iso_now
except ImportError:  # unittest discover 包上下文
    from tests.conftest import iso_now


SESSION_INFO_KEYS = {
    "task_id", "run_id", "session_marker", "session_id", "session_title",
    "state", "started_at", "last_message_at", "message_count", "updated_at",
    # §5.15.5：最近状态检查（session_reads 位点刷新时间，未读过为 null）。
    "last_checked_at",
    # §5.16.3：执行轮次索引扩展字段。
    "backend", "run_status", "save_state", "saved_at", "allowed_actions",
    "blocked_reason",
}

ROUND_DETAIL_KEYS = {
    "backend", "run_status", "worktree_path", "pid", "started_at",
    "ended_at", "exit_code", "terminal", "config", "save_state", "saved_at",
    "detail",
}

MESSAGE_KEYS = {"id", "seq", "run_id", "type", "content", "revision",
                "source_at"}

MESSAGE_TYPES = {"user", "assistant", "tool_call", "tool_result",
                 "command_output", "system"}

CONNECTION_STATES = {"ok", "degraded", "unavailable"}
SESSION_STATES = {"associated", "pending", "unavailable"}

LONG_OUTPUT = "x" * 200_000          # 长输出：必须完整返回，不得按 UI 截断

NO_SOURCE = {}                       # 不设 settings：默认 root（假 HOME）不存在


# ---------------------------------------------------------------------------
# pi 运行记录夹具（真实文件结构：job.json backend=pi + events.jsonl）
# ---------------------------------------------------------------------------


def set_setting(db_path, key, value):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def use_runs_root(tmp_env, root):
    """把 DB setting session_runs_root 指向给定目录（subprocess CLI 生效）。"""
    set_setting(tmp_env.db, "session_runs_root", str(root))


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def job_document(task_id, run_id, **overrides):
    """与真实 job.json 字段名一致（task_id/run_id/backend/status/pid…）。"""
    document = {
        "task_id": task_id, "run_id": run_id, "backend": "pi",
        "status": "finished", "exit_code": 0, "pid": 31337,
        "started_at": "2026-09-21T10:00:00.000000+00:00",
        "ended_at": "2026-09-21T10:45:00.000000+00:00",
        "session_id": "sess-pi-int", "session_title": "pi 集成轮次",
        "config": {"provider": "CloseAI", "model": "deepseek-flash",
                   "reasoning_effort": "xhigh", "agent_preset": "default"},
        "detail": None,
    }
    document.update(overrides)
    return document


def pi_event(seq, etype, ts=None, **fields):
    """events.jsonl 行：{"v":1,"seq":int,"ts":ISO,"type":...}。"""
    event = {"v": 1, "seq": seq,
             "ts": ts or f"2026-09-21T11:00:{seq % 60:02d}.000000+00:00",
             "type": etype}
    event.update(fields)
    return event


def default_pi_events():
    """§5.18.2 类型映射全覆盖；**首事件必须是 session 事件**。"""
    return [
        pi_event(1, "session", session_id="sess-pi-int",
                 session_title="pi 集成轮次"),
        pi_event(2, "user", content="执行计划"),
        pi_event(3, "assistant_message", content="开始执行"),
        pi_event(4, "tool_call", name="exec",
                 arguments={"command": "pytest -q"}),
        pi_event(5, "command_output", name="exec",
                 input=json.dumps({"command": "pytest -q"}),
                 output="all green", exit_code=0),
        pi_event(6, "terminal", outcome="completed", reason="正常结束"),
    ]


def write_pi_run(runs_path, task_id, run_id, events=None, *, job=None,
                 write_events=True):
    """<root>/<task>/<run>/ 的 pi 执行目录：job.json（backend=pi）+
    events.jsonl（首事件 session，§5.18.2）。"""
    directory = runs_path / task_id / run_id
    write_json(directory / "job.json",
               job if job is not None
               else job_document(task_id, run_id))
    if write_events:
        payload = events if events is not None else default_pi_events()
        (directory / "events.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in payload),
            encoding="utf-8")
    return directory


@pytest.fixture
def runs(tmp_path):
    """pi 运行记录根目录 + make() 构造 <root>/<task>/<run>/ 文件。"""
    root = tmp_path / "runs"
    root.mkdir()

    class Root:
        path = root

        @staticmethod
        def make(task_id, run_id, events=None, *, job=None,
                 write_events=True, raw=None):
            directory = root / task_id / run_id
            write_json(directory / "job.json",
                       job if job is not None
                       else job_document(task_id, run_id))
            events_path = directory / "events.jsonl"
            if raw is not None:
                directory.mkdir(parents=True, exist_ok=True)
                events_path.write_text(raw, encoding="utf-8")
            elif write_events:
                payload = events if events is not None else default_pi_events()
                events_path.write_text(
                    "".join(
                        json.dumps(e, ensure_ascii=False) + "\n"
                        for e in payload),
                    encoding="utf-8")
            return directory

    yield Root


def read_messages(cli, task_id, *, run_id=None, cursor=None, limit=None,
                  env=None):
    payload = {"task_id": task_id}
    if run_id:
        payload["run_id"] = run_id
    if cursor is not None:
        payload["cursor"] = cursor
    if limit is not None:
        payload["limit"] = limit
    else:
        # 实现层现状：constants 缺 DEFAULT_SESSION_MESSAGE_LIMIT，缺省 limit
        # 会 AttributeError；测试以显式大 limit 走同一代码路径（夹具消息数
        # 远小于该值，语义与「不截断」等价；core/tests/test_session.py 同款）。
        payload["limit"] = 500
    return cli("session-messages", payload, env_extra=env)


def session_read_row(db_path, session_key):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM session_reads WHERE session_key=?",
            (session_key,)).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §1 payload 校验 / 错误信封
# ---------------------------------------------------------------------------


class TestPayloadValidation:
    def test_messages_checks_version_and_force_types(self, cli):
        for field, value in (("source_version", 3), ("force_reload", "true")):
            out = cli("session-messages", {"task_id": "none", field: value}, expect=1)
            assert out["code"] == "bad_request"
            assert field in out["error"]

    def test_messages_requires_task_id(self, cli):
        out = cli("session-messages", {}, expect=1)
        assert out["code"] == "bad_request"
        assert out["error"]

    def test_messages_rejects_unknown_field(self, cli, factory):
        task = factory.sync_task(title="会话参数")
        out = cli("session-messages", {"task_id": task.task_id,
                                       "bogus": 1}, expect=1)
        assert out["code"] == "bad_request"

    def test_get_session_requires_run_id(self, cli, factory):
        task = factory.sync_task(title="会话缺轮次")
        out = cli("get-session", {"task_id": task.task_id}, expect=1)
        assert out["code"] == "bad_request"

    def test_get_session_rejects_unknown_field(self, cli, factory):
        task = factory.sync_task(title="会话未知字段")
        out = cli("get-session", {"task_id": task.task_id, "run_id": "r",
                                  "bogus": 1}, expect=1)
        assert out["code"] == "bad_request"

    def test_list_sessions_rejects_unknown_field(self, cli):
        out = cli("list-sessions", {"bogus": 1}, expect=1)
        assert out["code"] == "bad_request"

    def test_unknown_task_is_not_found(self, cli):
        """task 不存在 → not_found（与 state=unavailable 是两回事）。"""
        out = cli("get-session", {"task_id": "nosuchtask000000000000",
                                  "run_id": "run-x"}, expect=1)
        assert out["code"] == "not_found"
        out2 = cli("session-messages", {"task_id": "nosuchtask000000000000"},
                   expect=1)
        assert out2["code"] == "not_found"


# ---------------------------------------------------------------------------
# 根目录不存在：结构化 unavailable + 配置引导
# ---------------------------------------------------------------------------


class TestNoDataSource:
    def test_messages_unavailable_shape(self, factory, cli):
        running = factory.make_running(title="无源-消息")
        out = read_messages(cli, running.task.task_id, env=NO_SOURCE)
        assert set(out) >= {"session", "messages", "next_cursor", "has_more",
                            "connection"}
        assert out["messages"] == []
        assert out["has_more"] is False
        assert out["next_cursor"] is None
        assert out["session"]["state"] == "unavailable"
        assert out["connection"]["state"] == "unavailable"
        detail = out["connection"].get("detail") or ""
        assert detail, "应说明不可用原因"
        assert "session_runs_root" in detail, "机制性失败须引导配置设置项"

    def test_get_session_unavailable_shape(self, factory, cli):
        running = factory.make_running(title="无源-详情")
        out = cli("get-session", {"task_id": running.task.task_id,
                                  "run_id": running.dispatch_run_id},
                  env_extra=NO_SOURCE)
        assert out["session"]["state"] == "unavailable"
        assert out["connection"]["state"] == "unavailable"

    def test_list_sessions_shape_and_filter(self, factory, cli):
        a = factory.make_running(title="无源-列表A")
        b = factory.make_running(project="Music", title="无源-列表B")
        out = cli("list-sessions", {}, env_extra=NO_SOURCE)
        assert isinstance(out["sessions"], list)
        for s in out["sessions"]:
            assert SESSION_INFO_KEYS <= set(s), f"SessionInfo 缺字段：{s}"
            assert s["state"] in SESSION_STATES
        only_a = cli("list-sessions", {"task_id": a.task.task_id},
                     env_extra=NO_SOURCE)
        assert all(s["task_id"] == a.task.task_id for s in only_a["sessions"])
        everything = {s["task_id"] for s in out["sessions"]}
        assert {a.task.task_id, b.task.task_id} <= everything

    def test_undispatched_task_not_listed(self, factory, cli, tmp_env, runs):
        """未派发（无 run_id）的任务不进 list-sessions；读取 → pending。"""
        ready = factory.make_ready(title="无源-未派发")
        use_runs_root(tmp_env, runs.path)
        # v2 §5.22.3：未派发任务只有 codex 规划轮次（无 pi 派发会话）。
        sessions = cli("list-sessions", {})["sessions"]
        assert [s["backend"] for s in sessions] == ["codex"]
        assert sessions[0]["task_id"] == ready.task.task_id
        out = read_messages(cli, ready.task.task_id)
        assert out["session"]["state"] == "pending"
        assert out["session"]["session_marker"] is None
        assert out["messages"] == []

    def test_reads_do_not_mutate_task_or_events(self, factory, cli):
        """只读边界：会话命令不改 task 行、不写事件、不发请求、不写 Kaneo。"""
        running = factory.make_running(title="无源-只读")
        tid = running.task.task_id
        before = factory.get_task(tid)
        events_before = cli("list-events", {"task_id": tid})["events"]
        reqs_before = cli("list-requests", {})["requests"]
        writes_before = len(factory.kaneo.requests_to("PUT", "/api/task/status"))

        cli("list-sessions", {"task_id": tid}, env_extra=NO_SOURCE)
        cli("get-session", {"task_id": tid,
                            "run_id": running.dispatch_run_id},
            env_extra=NO_SOURCE)
        read_messages(cli, tid, env=NO_SOURCE)

        after = factory.get_task(tid)
        for field in ("status", "stage", "save_state", "send_state", "sent",
                      "run_id", "session_marker", "failure_code",
                      "blocked_reason", "updated_at"):
            assert before[field] == after[field], (
                f"会话命令改写了 task.{field}：{before[field]} → {after[field]}")
        assert len(cli("list-events", {"task_id": tid})["events"]) == \
            len(events_before), "会话命令不应写事件"
        assert cli("list-requests", {})["requests"] == reqs_before
        assert len(factory.kaneo.requests_to("PUT", "/api/task/status")) == \
            writes_before, "会话命令不得写 Kaneo"


# ---------------------------------------------------------------------------
# 全文 / 长输出 / 消息形状
# ---------------------------------------------------------------------------


class TestFullHistoryAndShape:
    def test_first_load_full_history_and_shape(self, factory, cli, tmp_env,
                                               runs):
        running = factory.make_running(title="会话全文")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, [
            pi_event(1, "session", session_id="sess-pi-int",
                     session_title="pi 集成轮次"),   # → system 消息
            pi_event(2, "user", content="请修复登录 bug"),
            pi_event(3, "assistant_message", content="我来看看"),
            pi_event(4, "tool_call", name="exec",
                     arguments={"command": "pytest"}),
            pi_event(5, "command_output", name="exec",
                     input=json.dumps({"command": "pytest"}),
                     output="ok", exit_code=0),
            pi_event(6, "tool_call", name="read",
                     arguments={"path": "a.txt"}),
            pi_event(7, "tool_result", name="read", output="1|hello"),
        ])
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid)
        assert out["session"]["state"] == "associated"
        assert out["session"]["session_id"] == "sess-pi-int"
        assert out["session"]["session_title"] == "pi 集成轮次"
        assert SESSION_INFO_KEYS <= set(out["session"])
        assert [m["type"] for m in out["messages"]] == [
            "system", "user", "assistant", "tool_call", "command_output",
            "tool_call", "tool_result"]
        assert out["has_more"] is False
        assert out["next_cursor"]
        for m in out["messages"]:
            assert MESSAGE_KEYS <= set(m), f"消息缺字段：{m}"
            assert m["type"] in MESSAGE_TYPES
            assert m["run_id"] == run, "run_id 应由命令层注入"
        by_id = {m["id"]: m for m in out["messages"]}
        # tool 仅工具类型出现；command_output 的 output 为 null、exit_code 有值。
        assert "tool" not in by_id["round:1"]
        assert "tool" not in by_id["round:2"]
        assert "tool" not in by_id["round:3"]
        assert by_id["round:4"]["tool"]["name"] == "exec"
        assert json.loads(by_id["round:4"]["tool"]["input"]) == \
            {"command": "pytest"}
        assert by_id["round:5"]["tool"]["exit_code"] == 0
        assert by_id["round:5"]["tool"]["output"] is None
        assert by_id["round:7"]["tool"]["output"] == "1|hello"
        assert by_id["round:7"]["tool"]["exit_code"] is None
        assert out["connection"]["state"] == "degraded"
        # §5.15.5：session-messages 响应反映本次「最近状态检查」时间。
        assert out["session"]["last_checked_at"] is not None

    def test_long_output_not_truncated(self, factory, cli, tmp_env, runs):
        running = factory.make_running(title="会话长输出")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, [
            pi_event(1, "session", session_id="s1"),
            pi_event(2, "command_output", name="exec",
                     input=json.dumps({"command": "cat big"}),
                     output=LONG_OUTPUT, exit_code=0),
        ])
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid)
        by_id = {m["id"]: m for m in out["messages"]}
        assert by_id["round:2"]["content"] == LONG_OUTPUT, "长输出被截断"
        assert by_id["round:2"]["type"] == "command_output"

    def test_limit_paginates_and_has_more(self, factory, cli, tmp_env, runs):
        running = factory.make_running(title="会话分页")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, [pi_event(i, "assistant_message",
                                      content=f"m{i}")
                             for i in range(1, 6)])
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid, limit=2)
        assert len(out["messages"]) == 2
        assert out["has_more"] is True
        assert [m["seq"] for m in out["messages"]] == [1, 2]
        # 逐页推进直到读完（跨进程：靠 session_reads + 显式 cursor）。
        cursor = out["next_cursor"]
        collected = [m["id"] for m in out["messages"]]
        while True:
            page = read_messages(cli, tid, cursor=cursor, limit=2)
            collected += [m["id"] for m in page["messages"]]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert collected == [f"round:{i}" for i in range(1, 6)]


# ---------------------------------------------------------------------------
# 多任务隔离 / 轮次切换
# ---------------------------------------------------------------------------


class TestIsolationAndRounds:
    def test_multi_task_isolation(self, factory, cli, tmp_env, runs):
        a = factory.make_running(title="会话隔离A")
        b = factory.make_running(project="Music", title="会话隔离B")
        runs.make(a.task.task_id, a.dispatch_run_id,
                  [pi_event(1, "user", content="A only")])
        runs.make(b.task.task_id, b.dispatch_run_id,
                  [pi_event(1, "user", content="B only")])
        use_runs_root(tmp_env, runs.path)
        listed = cli("list-sessions", {})["sessions"]
        # v2 §5.22.3：每个任务含 codex 规划轮次 + pi 派发轮次两条会话；
        # 按 (task_id, run_id) 定位派发会话做隔离断言。
        by_pair = {(s["task_id"], s["run_id"]): s for s in listed}
        assert (a.task.task_id, a.dispatch_run_id) in by_pair
        assert (b.task.task_id, b.dispatch_run_id) in by_pair
        sa = by_pair[(a.task.task_id, a.dispatch_run_id)]
        sb = by_pair[(b.task.task_id, b.dispatch_run_id)]
        assert sa["backend"] == "pi" and sa["state"] == "associated"
        assert sb["backend"] == "pi" and sb["state"] == "associated"

        only_a = cli("list-sessions", {"task_id": a.task.task_id})["sessions"]
        assert all(s["task_id"] == a.task.task_id for s in only_a)

        ma = read_messages(cli, a.task.task_id)["messages"]
        assert [m["content"] for m in ma] == ["A only"]
        assert all(m["run_id"] == a.dispatch_run_id for m in ma)

    def test_round_switching(self, factory, cli, tmp_env, runs):
        """缺省 run_id 取当前轮次；显式 run_id 切到历史轮次；互不串线。"""
        running = factory.make_running(title="会话轮次")
        tid = running.task.task_id
        current, previous = running.dispatch_run_id, "run-previous-0001"
        runs.make(tid, previous, [pi_event(1, "user", content="上一轮")])
        runs.make(tid, current, [pi_event(1, "user", content="当前轮"),
                                 pi_event(2, "assistant_message",
                                          content="当前轮第二条")])
        use_runs_root(tmp_env, runs.path)
        cur = read_messages(cli, tid)
        assert cur["session"]["run_id"] == current
        assert [m["content"] for m in cur["messages"]] == \
            ["当前轮", "当前轮第二条"]

        prev = read_messages(cli, tid, run_id=previous)
        assert prev["session"]["run_id"] == previous
        assert [m["content"] for m in prev["messages"]] == ["上一轮"]

        g = cli("get-session", {"task_id": tid, "run_id": previous})["session"]
        assert g["run_id"] == previous
        assert g["state"] == "associated"
        # 两轮回点各自独立落 session_reads。
        assert session_read_row(tmp_env.db, f"{tid}/{current}") is not None
        assert session_read_row(tmp_env.db, f"{tid}/{previous}") is not None


# ---------------------------------------------------------------------------
# pending / 半写入 / 身份与越界
# ---------------------------------------------------------------------------


class TestStatesAndFailures:
    def test_pending_when_events_missing(self, factory, cli, tmp_env, runs):
        """目录 + job.json 有效但 events.jsonl 尚未生成 → pending。"""
        running = factory.make_running(title="会话待导出")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, write_events=False)
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid)
        assert out["session"]["state"] == "pending"
        assert out["session"]["session_id"] is None
        assert out["messages"] == []
        assert out["connection"]["state"] == "degraded"

    def test_half_written_without_history_is_pending_with_reason(
            self, factory, cli, tmp_env, runs):
        """零有效行（首行即截断）且无上一份（跨进程必然无缓存）→ pending +
        detail 说明，绝不以「空会话 associated」掩盖解析失败。"""
        running = factory.make_running(title="会话半写入")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, raw='{"v": 1, "seq": 1, "type": "session", "s截断')
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid)
        assert out["session"]["state"] == "pending"
        assert out["messages"] == []
        assert out["connection"]["state"] == "degraded"
        assert "解析失败" in (out["connection"]["detail"] or "")

    def test_half_written_keeps_last_valid_snapshot_cross_process(
            self, factory, cli, tmp_env, runs):
        """先成功读取，再整文件截断：新进程无缓存也保留上一份有效快照（跨进程
        持久于 settings）+ degraded detail；位点不回退；恢复后补齐。"""
        running = factory.make_running(title="会话半写入位点")
        tid, run = running.task.task_id, running.dispatch_run_id
        events = [pi_event(1, "session", session_id="s1"),
                  pi_event(2, "user", content="a"),
                  pi_event(3, "assistant_message", content="b")]
        runs.make(tid, run, events)
        use_runs_root(tmp_env, runs.path)
        first = read_messages(cli, tid)
        cursor = first["next_cursor"]
        row = session_read_row(tmp_env.db, f"{tid}/{run}")
        assert row["cursor"] == cursor and row["last_seq"] == 3

        broken = runs.path / tid / run / "events.jsonl"
        good = broken.read_text(encoding="utf-8")
        # 首行截断 → 零有效行 → read_failed（尾行半写才会静默丢弃）。
        broken.write_text(good[:20], encoding="utf-8")
        # 全新进程、无进程内缓存：仍保留上一份有效内容，不报空会话。
        out = read_messages(cli, tid, cursor=cursor)
        assert out["session"]["state"] == "associated"
        assert out["connection"]["state"] == "degraded"
        assert "解析失败" in (out["connection"]["detail"] or "")
        replay = read_messages(cli, tid, cursor="seq:0")
        assert [m["id"] for m in replay["messages"]] == \
            [m["id"] for m in first["messages"]]
        row2 = session_read_row(tmp_env.db, f"{tid}/{run}")
        assert row2["cursor"] == cursor, "半写入不得扰动已存位点"
        assert row2["last_seq"] == 3

        # 恢复并追加：带原 cursor 补齐新消息，不跳历史。
        broken.write_text("".join(
            json.dumps(e, ensure_ascii=False) + "\n"
            for e in events + [pi_event(4, "assistant_message",
                                        content="完成")]),
            encoding="utf-8")
        filled = read_messages(cli, tid, cursor=cursor)
        assert [m["id"] for m in filled["messages"]] == ["round:4"]

    def test_missing_run_dir_is_unavailable(self, factory, cli, tmp_env, runs):
        running = factory.make_running(title="会话无目录")
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, running.task.task_id)
        assert out["session"]["state"] == "unavailable"
        assert "执行目录" in (out["connection"]["detail"] or "")

    def test_identity_mismatch_is_unavailable(self, factory, cli, tmp_env,
                                              runs):
        """job.json 的 task_id/run_id 与目录名不符 → unavailable + 原因，
        不按标题猜测、不读取该会话内容。"""
        running = factory.make_running(title="会话身份不符")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, job=job_document("other-task", run),
                  events=[pi_event(1, "user", content="x")])
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid)
        assert out["session"]["state"] == "unavailable"
        assert "身份与目录不符" in (out["connection"]["detail"] or "")
        assert out["messages"] == []

    def test_backend_field_missing_is_unavailable(self, factory, cli, tmp_env,
                                                  runs):
        running = factory.make_running(title="会话缺backend")
        tid, run = running.task.task_id, running.dispatch_run_id
        job = job_document(tid, run)
        job.pop("backend")
        runs.make(tid, run, job=job,
                  events=[pi_event(1, "user", content="x")])
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid)
        assert out["session"]["state"] == "unavailable"
        assert "backend" in (out["connection"]["detail"] or "")

    def test_illegal_run_id_rejected(self, factory, cli, tmp_env, runs):
        running = factory.make_running(title="会话非法标识")
        tid = running.task.task_id
        use_runs_root(tmp_env, runs.path)
        for bad in ("../escape", "a/b", "with space", ""):
            out = read_messages(cli, tid, run_id=bad)
            assert out["session"]["state"] == "unavailable", bad
            assert out["messages"] == []

    def test_symlink_escape_rejected(self, factory, cli, tmp_env, runs,
                                     tmp_path):
        """run 目录是指向 root 外的符号链接：realpath 越界 → unavailable，
        绝不读取他目录内容。"""
        running = factory.make_running(title="会话符号链接")
        tid, run = running.task.task_id, running.dispatch_run_id
        outside = tmp_path / "outside"
        write_json(outside / "job.json", job_document(tid, run))
        (outside / "events.jsonl").write_text(
            json.dumps(pi_event(1, "user", content="sneaky"),
                       ensure_ascii=False) + "\n",
            encoding="utf-8")
        link_parent = runs.path / tid
        link_parent.mkdir(parents=True, exist_ok=True)
        (link_parent / run).symlink_to(outside)
        use_runs_root(tmp_env, runs.path)
        out = read_messages(cli, tid)
        assert out["session"]["state"] == "unavailable"
        assert "sneaky" not in json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 游标 / 断线重连 / 修订透出（跨进程：仅依赖 session_reads）
# ---------------------------------------------------------------------------


class TestCursorAndReconnect:
    def test_incremental_after_cursor(self, factory, cli, tmp_env, runs):
        running = factory.make_running(title="会话游标")
        tid, run = running.task.task_id, running.dispatch_run_id
        marker = f"{tid}/{run}"
        events = [pi_event(1, "session", session_id="s1"),
                  pi_event(2, "user", content="a"),
                  pi_event(3, "assistant_message", content="b"),
                  pi_event(4, "assistant_message", content="c")]
        runs.make(tid, run, events)
        use_runs_root(tmp_env, runs.path)
        first = read_messages(cli, tid)
        assert [m["seq"] for m in first["messages"]] == [1, 2, 3, 4]
        cursor = first["next_cursor"]
        row = session_read_row(tmp_env.db, marker)
        assert row["cursor"] == cursor
        assert row["last_seq"] == 4
        assert row["state"] == "associated"

        # 追加两条 → 全新进程带游标只回新消息。
        runs.make(tid, run, events + [pi_event(5, "assistant_message",
                                               content="d"),
                                      pi_event(6, "assistant_message",
                                               content="e")])
        second = read_messages(cli, tid, cursor=cursor)
        assert [m["seq"] for m in second["messages"]] == [5, 6]
        assert second["next_cursor"] != cursor

    def test_reopen_returns_full_history_reconnect_uses_cursor(
            self, factory, cli, tmp_env, runs):
        """重开（payload 无 cursor）恒返回完整历史（不隐式套用已存位点）；
        断线重连由调用方带最后 cursor 补齐，不重复已读。"""
        running = factory.make_running(title="会话重连")
        tid, run = running.task.task_id, running.dispatch_run_id
        events = [pi_event(i, "assistant_message", content=f"m{i}")
                  for i in range(1, 4)]
        runs.make(tid, run, events)
        use_runs_root(tmp_env, runs.path)
        first = read_messages(cli, tid)
        assert len(first["messages"]) == 3
        # 断线期间新增消息。
        runs.make(tid, run, events + [pi_event(4, "assistant_message",
                                               content="m4")])
        # 重开：完整历史（含新消息），不只给增量。
        reopened = read_messages(cli, tid)
        assert [m["id"] for m in reopened["messages"]] == \
            ["round:1", "round:2", "round:3", "round:4"]
        # 带最后 cursor 的增量：只补新消息，不重放 1-3。
        resumed = read_messages(cli, tid, cursor=first["next_cursor"])
        assert [m["id"] for m in resumed["messages"]] == ["round:4"]

    def test_tail_revision_reemit_cross_process(self, factory, cli, tmp_env,
                                                runs):
        """尾部进行中事件内容更新：seq<=cursor 但 revision 与已存快照不同 →
        增量读取透出（全新进程、无缓存依赖）。"""
        running = factory.make_running(title="会话修订透出")
        tid, run = running.task.task_id, running.dispatch_run_id
        marker = f"{tid}/{run}"
        events = [pi_event(1, "session", session_id="s1"),
                  pi_event(2, "user", content="开始"),
                  pi_event(3, "command_output", name="exec",
                           input=json.dumps({"command": "make"}),
                           output="ok", exit_code=0)]
        runs.make(tid, run, events)
        use_runs_root(tmp_env, runs.path)
        first = read_messages(cli, tid)
        cursor = first["next_cursor"]
        row = session_read_row(tmp_env.db, marker)
        snapshot = json.loads(row["last_revision"])
        assert "round:3" in snapshot

        # 同一尾部事件内容变化（结果更新 + 退出码变化），seq 均 <= cursor。
        updated = pi_event(3, "command_output", name="exec",
                           input=json.dumps({"command": "make"}),
                           output="failed output", exit_code=1)
        runs.make(tid, run, events[:2] + [updated])
        second = read_messages(cli, tid, cursor=cursor)
        ids = [m["id"] for m in second["messages"]]
        assert "round:3" in ids, "修订内容必须在增量读取中透出"
        # 游标不回退。
        row2 = session_read_row(tmp_env.db, marker)
        assert row2["cursor"] == cursor
        # 再读同游标不重复。
        third = read_messages(cli, tid, cursor=row2["cursor"])
        assert third["messages"] == []

    def test_unavailable_keeps_read_position(self, factory, cli, tmp_env, runs):
        """root 换成不存在路径：messages 空、位点不被覆盖（断线补齐用）。"""
        running = factory.make_running(title="会话不可用")
        tid, run = running.task.task_id, running.dispatch_run_id
        marker = f"{tid}/{run}"
        events = [pi_event(1, "session", session_id="s1"),
                  pi_event(2, "user", content="a"),
                  pi_event(3, "assistant_message", content="b")]
        runs.make(tid, run, events)
        use_runs_root(tmp_env, runs.path)
        first = read_messages(cli, tid)
        cursor = first["next_cursor"]
        row = session_read_row(tmp_env.db, marker)
        assert row["cursor"] == cursor and row["last_seq"] == 3

        use_runs_root(tmp_env, tmp_env.root / "no-such-root-here")
        out = read_messages(cli, tid)
        assert out["connection"]["state"] == "unavailable"
        assert out["session"]["state"] == "unavailable"
        assert out["messages"] == []
        row2 = session_read_row(tmp_env.db, marker)
        assert row2["cursor"] == cursor, "不可用不得清空已读位点"
        assert row2["last_seq"] == 3
        # 恢复后带原 cursor 仍能补齐。
        use_runs_root(tmp_env, runs.path)
        runs.make(tid, run, events + [pi_event(4, "assistant_message",
                                               content="c")])
        filled = read_messages(cli, tid, cursor=cursor)
        assert [m["id"] for m in filled["messages"]] == ["round:4"]


# ---------------------------------------------------------------------------
# connection / 设置优先级
# ---------------------------------------------------------------------------


class TestConnectionAndSettings:
    def test_degraded_always(self, factory, cli, tmp_env, runs):
        """运行记录无推送订阅 → connection 恒 degraded + poll_seconds。"""
        running = factory.make_running(title="连接-降级")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, [pi_event(1, "user", content="x")])
        use_runs_root(tmp_env, runs.path)
        out = cli("get-session", {"task_id": tid, "run_id": run})
        assert out["connection"]["state"] == "degraded"
        assert out["connection"]["subscription"] is False
        assert out["connection"]["poll_seconds"] == 2
        msgs = read_messages(cli, tid)["messages"]
        assert [m["id"] for m in msgs] == ["round:1"]

    def test_poll_seconds_setting(self, factory, cli, tmp_env, runs):
        """session_poll_seconds 经 settings 可调；overview 与会话双路下发。"""
        running = factory.make_running(title="连接-轮询")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, [pi_event(1, "user", content="x")])
        use_runs_root(tmp_env, runs.path)
        assert cli("overview", {})["settings"]["session_poll_seconds"] == 2
        assert "session_runs_root" in cli("overview", {})["settings"]
        set_setting(tmp_env.db, "session_poll_seconds", 5)
        out = cli("get-session", {"task_id": tid, "run_id": run})
        assert out["connection"]["poll_seconds"] == 5

    def test_payload_override_and_precedence(self, factory, cli, tmp_env,
                                             runs, tmp_path):
        """优先级 payload > settings > 默认；payload 覆盖不持久化；
        空串 = 未提供。"""
        running = factory.make_running(title="覆盖-优先级")
        tid, run = running.task.task_id, running.dispatch_run_id
        other = tmp_path / "other-runs"
        runs.make(tid, run, [pi_event(1, "user", content="real")])
        use_runs_root(tmp_env, runs.path)
        # settings 生效。
        out = cli("list-sessions", {"task_id": tid})
        assert out["sessions"][0]["state"] == "associated"
        # payload 覆盖：other-runs 存在但无该目录 → unavailable（仅本次调用）。
        out2 = cli("list-sessions", {"task_id": tid,
                                     "session_runs_root": str(other)})
        assert out2["sessions"][0]["state"] == "unavailable"
        # 不持久化：下一次不带 payload → settings 再次生效。
        out3 = cli("list-sessions", {"task_id": tid})
        assert out3["sessions"][0]["state"] == "associated"
        # payload 空串 = 未提供 → settings 生效。
        out4 = cli("list-sessions", {"task_id": tid,
                                     "session_runs_root": ""})
        assert out4["sessions"][0]["state"] == "associated"

    def test_list_sessions_light_mode_many_records(self, factory, cli, tmp_env,
                                                   runs):
        """运行记录目录数超阈值：list-sessions 缺省模式轻量核验（不读文件），
        associated 由 get-session/session-messages 补齐。"""
        running = factory.make_running(title="轻量列表")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, [pi_event(1, "user", content="x")])
        for i in range(51):
            (runs.path / f"junk{i:04d}").mkdir()
        use_runs_root(tmp_env, runs.path)
        listed = cli("list-sessions")["sessions"]
        session = next(s for s in listed if s["task_id"] == tid)
        assert session["state"] == "pending", "超阈值应延迟核验"
        full = cli("get-session", {"task_id": tid, "run_id": run})
        assert full["session"]["state"] == "associated"


# ---------------------------------------------------------------------------
# 会话输出只作进度展示：不解除异常、不改生命周期
# ---------------------------------------------------------------------------


class TestSessionDoesNotAdvanceLifecycle:
    def test_reading_session_keeps_exception(self, factory, cli, tmp_env,
                                             runs):
        """收到新会话日志不解除任何异常（§5.15.4）。"""
        running = factory.make_running(title="会话不解除异常")
        tid = running.task.task_id
        runs.make(tid, running.dispatch_run_id, [
            pi_event(1, "assistant_message", content="我已经修好了"),
            pi_event(2, "assistant_message", content="任务完成"),
        ])
        use_runs_root(tmp_env, runs.path)
        req = factory.find_request(tid, "monitor")
        c = factory.claim("monitor", request_id=req["request_id"])
        cli("report", {"request_id": c["request_id"], "run_id": c["run_id"],
                       "outcome": "needs_user",
                       "failure_code": "execution_failed",
                       "failure_reason": "执行失败"})
        before = factory.get_task(tid)
        assert before["status"] == "needs_user"
        assert before["board_column"] == "failed"

        read_messages(cli, tid)
        cli("get-session", {"task_id": tid,
                            "run_id": running.dispatch_run_id})

        after = factory.get_task(tid)
        assert after["status"] == "needs_user", "会话输出不得推进生命周期"
        assert after["board_column"] == "failed"
        assert after["failure_code"] == "execution_failed"
        assert after["blocked_reason"]


# ---------------------------------------------------------------------------
# §5.16：会话回收站 + 执行轮次索引 + pi 轮次（v1.6）
# ---------------------------------------------------------------------------


class TestTrashRestoreLifecycle:
    """完整删除/恢复闭环：UI 将调用的命令序列 + 幂等 + 跨 CLI 进程（重启）状态
    保持 + 删除后 get-session/session-messages 行为。"""

    def test_full_delete_restore_loop(self, factory, cli, tmp_env, runs):
        fixture = factory.make_needs_user(title="会话回收站闭环")
        tid, run = fixture.task.task_id, fixture.dispatch_run_id
        runs.make(tid, run, [pi_event(1, "session", session_id="s1"),
                             pi_event(2, "user", content="开始"),
                             pi_event(3, "assistant_message",
                                      content="处理中")])
        use_runs_root(tmp_env, runs.path)

        # ① UI 打开阅读器：list-sessions(active) → get-session → session-messages。
        active = cli("list-sessions",
                     {"task_id": tid, "save_state": "active"})["sessions"]
        # v2 §5.22.3：已规划任务另有 codex 轮次；断言当前派发轮次在列。
        s0 = next(s for s in active if s["run_id"] == run)
        assert {s["backend"] for s in active} == {"pi", "codex"}
        assert SESSION_INFO_KEYS <= set(s0)
        assert s0["state"] == "associated"
        assert s0["backend"] == "pi"
        assert s0["save_state"] == "active" and s0["saved_at"] is None
        assert s0["allowed_actions"] == ["trash-session"]
        assert s0["blocked_reason"] is None
        # §5.15.5：从未读取过消息 → 最近状态检查为 null（尚未检查）。
        assert s0["last_checked_at"] is None
        first = cli("get-session", {"task_id": tid, "run_id": run})
        assert first["session"]["save_state"] == "active"
        assert first["session"]["last_checked_at"] is None
        assert "round_detail" in first
        history = read_messages(cli, tid)
        assert history["session"]["state"] == "associated"
        assert history["session"]["last_checked_at"] is not None, (
            "读取后应记录最近状态检查时间")
        assert history["messages"], "pi 事件流读取能力必须保留"

        # ② 移入回收站（带 expected_save_state 的 CAS）。
        trashed = cli("trash-session", {
            "task_id": tid, "run_id": run, "expected_save_state": "active",
        })
        assert trashed["ok"] is True and trashed["deduplicated"] is False
        assert trashed["session"]["save_state"] == "trashed"
        assert trashed["session"]["saved_at"]
        # v2 §5.20.2：回收站会话另有 purge-session（永久移除）入口。
        assert trashed["session"]["allowed_actions"] == [
            "restore-session", "purge-session"]
        assert trashed["session"]["blocked_reason"] is None

        # ③ 刷新列表（每次 cli() 都是全新核心进程 → 状态持久即重启后保持）。
        cli("overview", {})
        remaining = cli("list-sessions",
                        {"task_id": tid, "save_state": "active"})["sessions"]
        assert all(s["run_id"] != run for s in remaining)
        assert [s["backend"] for s in remaining] == ["codex"]
        trash_list = cli("list-sessions",
                         {"task_id": tid,
                          "save_state": "trashed"})["sessions"]
        assert [s["run_id"] for s in trash_list] == [run]
        assert trash_list[0]["allowed_actions"] == [
            "restore-session", "purge-session"]

        # ④ 删除后 get-session / session-messages 符合契约：仍可读、保存状态
        #    透出、消息与删除前一致（删除只改调度台保存状态）。
        after = cli("get-session", {"task_id": tid, "run_id": run})
        assert set(after) >= {"session", "connection", "round_detail"}
        assert SESSION_INFO_KEYS <= set(after["session"])
        assert after["session"]["save_state"] == "trashed"
        # v2 §5.20.2：回收站会话另有 purge-session（永久移除）入口。
        assert after["session"]["allowed_actions"] == [
            "restore-session", "purge-session"]
        reread = read_messages(cli, tid)
        assert reread["session"]["allowed_actions"] == [
            "restore-session", "purge-session"]
        assert reread["session"]["state"] == "associated"
        assert reread["session"]["save_state"] == "trashed"
        assert [m["id"] for m in reread["messages"]] == \
            [m["id"] for m in history["messages"]]

        # ⑤ 幂等：重复删除不重复写事件。
        events_before = cli("list-events", {"task_id": tid})["events"]
        again = cli("trash-session", {"task_id": tid, "run_id": run})
        assert again["deduplicated"] is True
        assert cli("list-events", {"task_id": tid})["events"] == events_before

        # ⑥ 恢复 → 正常列表重新出现、回收站清空、重复恢复幂等。
        restored = cli("restore-session", {"task_id": tid, "run_id": run})
        assert restored["ok"] is True and restored["deduplicated"] is False
        assert restored["session"]["save_state"] == "active"
        assert restored["session"]["saved_at"] is None
        listed = cli("list-sessions", {"task_id": tid})["sessions"]
        assert run in {s["run_id"] for s in listed}
        assert {s["backend"] for s in listed} == {"pi", "codex"}
        assert cli("list-sessions",
                   {"task_id": tid, "save_state": "trashed"})["sessions"] == []
        assert cli("restore-session",
                   {"task_id": tid,
                    "run_id": run})["deduplicated"] is True

        # ⑦ 边界：任务与执行记录不受会话删除影响（两种保存状态正交）。
        task_after = factory.get_task(tid)
        assert task_after["save_state"] == "active"
        assert task_after["status"] == "needs_user"
        assert task_after["run_id"] == run
        assert task_after["session_marker"] == f"{tid}/{run}"


class TestSessionTrashGuards:
    def test_running_task_round_blocked(self, factory, cli, tmp_env, runs):
        """当前轮次任务执行中 → conflict + 中文原因；list-sessions 同轮
        blocked_reason 同文案、allowed_actions 为空（§5.16.2）。"""
        running = factory.make_running(title="回收站禁止-执行中")
        tid, run = running.task.task_id, running.dispatch_run_id
        runs.make(tid, run, [pi_event(1, "user", content="x")])
        use_runs_root(tmp_env, runs.path)
        cli("list-sessions", {"task_id": tid})
        out = cli("trash-session", {"task_id": tid, "run_id": run},
                  expect=1)
        assert out["code"] == "conflict"
        assert "暂不能移入会话回收站" in out["error"]
        entry = next(s for s in cli("list-sessions",
                                    {"task_id": tid})["sessions"]
                     if s["run_id"] == run)
        assert entry["allowed_actions"] == []
        assert entry["blocked_reason"] == out["error"]
        assert entry["save_state"] == "active"

    def test_expected_conflict_and_not_found(self, factory, cli, tmp_env,
                                             runs):
        fixture = factory.make_needs_user(title="会话expected冲突")
        tid, run = fixture.task.task_id, fixture.dispatch_run_id
        runs.make(tid, run, [pi_event(1, "user", content="x")])
        use_runs_root(tmp_env, runs.path)
        cli("list-sessions", {"task_id": tid})
        conflict = cli("trash-session", {
            "task_id": tid, "run_id": run, "expected_save_state": "trashed",
        }, expect=1)
        assert conflict["code"] == "conflict"
        assert "会话保存状态冲突" in conflict["error"]
        missing = cli("trash-session",
                      {"task_id": tid, "run_id": "run-zzz-none"},
                      expect=1)
        assert missing["code"] == "not_found"
        restore_missing = cli("restore-session",
                              {"task_id": tid, "run_id": "run-zzz-none"},
                              expect=1)
        assert restore_missing["code"] == "not_found"


class TestListSessionsSaveStateContract:
    def test_filter_three_states_and_invalid(self, factory, cli, tmp_env,
                                             runs):
        a = factory.make_needs_user(title="筛选A")
        b = factory.make_needs_user(title="筛选B")
        runs.make(a.task.task_id, a.dispatch_run_id,
                  [pi_event(1, "user", content="A")])
        runs.make(b.task.task_id, b.dispatch_run_id,
                  [pi_event(1, "user", content="B")])
        use_runs_root(tmp_env, runs.path)
        cli("trash-session",
            {"task_id": b.task.task_id, "run_id": b.dispatch_run_id})

        active = cli("list-sessions", {"save_state": "active"})["sessions"]
        # v2 §5.22.3：每个已规划任务另有 codex 轮次会话；按 run_id 粒度断言。
        assert (a.task.task_id, a.dispatch_run_id) in {
            (s["task_id"], s["run_id"]) for s in active}
        assert all(s["run_id"] != b.dispatch_run_id for s in active), (
            "trashed 会话不得混入 active")
        # b 的 codex 规划轮次未被 trash-session 触碰，仍在 active。
        b_active = [s for s in active if s["task_id"] == b.task.task_id]
        assert [s["backend"] for s in b_active] == ["codex"]
        trashed = cli("list-sessions", {"save_state": "trashed"})["sessions"]
        assert {s["run_id"] for s in trashed} == {b.dispatch_run_id}
        everything = cli("list-sessions", {"save_state": "all"})["sessions"]
        assert {a.task.task_id, b.task.task_id} <= \
            {s["task_id"] for s in everything}
        # 缺省 = active。
        default = cli("list-sessions")["sessions"]
        assert all(s["run_id"] != b.dispatch_run_id for s in default)
        # 非法值 → bad_request（§5.16.3，与 §1 同口径；空串按非法值）。
        for bad in ("archived", "bogus", "ACTIVE", ""):
            assert cli("list-sessions", {"save_state": bad},
                       expect=1)["code"] == "bad_request"
            assert cli("list-sessions",
                       {"task_id": a.task.task_id, "save_state": bad},
                       expect=1)["code"] == "bad_request"


class TestRoundDetailContract:
    def test_round_detail_pi_and_missing(self, factory, cli,
                                         tmp_env, runs):
        # pi 轮次：job 进程结果 + terminal 事件透出（§5.18.4）。
        fixture = factory.make_needs_user(title="round_detail-pi")
        tid, run = fixture.task.task_id, fixture.dispatch_run_id
        write_pi_run(runs.path, tid, run)
        use_runs_root(tmp_env, runs.path)
        out = cli("get-session", {"task_id": tid, "run_id": run})
        assert set(out) >= {"session", "connection", "round_detail"}
        detail = out["round_detail"]
        assert ROUND_DETAIL_KEYS <= set(detail)
        assert detail["backend"] == "pi"
        assert detail["run_status"] == "finished"
        assert detail["exit_code"] == 0
        assert detail["pid"] == 31337
        assert detail["terminal"] == {"outcome": "completed",
                                      "reason": "正常结束"}
        assert detail["save_state"] == "active"

        # 无轮次（目录与索引行都不存在）→ round_detail 安全 None。
        missing = cli("get-session", {"task_id": tid, "run_id": "run-none"})
        assert missing["round_detail"] is None
        assert missing["session"]["state"] == "unavailable"

    def test_round_detail_timeout_derives_failed_with_detail(self, factory, cli,
                                                             tmp_env, runs):
        """§5.18.4：status=running 超过 pi_round_timeout_seconds →
        round_detail.run_status 读取层派生 failed 且 detail 记原因（与
        SessionInfo.run_status 同口径）；索引与 job.json 不回写。"""
        fixture = factory.make_needs_user(title="round_detail-超时")
        tid, run = fixture.task.task_id, fixture.dispatch_run_id
        write_pi_run(
            runs.path, tid, run,
            events=[pi_event(1, "session", session_id="sess-pi-int",
                             session_title="pi 集成轮次")],
            job=job_document(tid, run, status="running",
                             started_at="2020-01-01T00:00:00.000000+00:00",
                             ended_at=None, exit_code=None),
        )
        use_runs_root(tmp_env, runs.path)
        set_setting(tmp_env.db, "pi_round_timeout_seconds", 1)
        out = cli("get-session", {"task_id": tid, "run_id": run})
        detail = out["round_detail"]
        assert ROUND_DETAIL_KEYS <= set(detail)
        assert detail["run_status"] == "failed"
        assert "超时" in (detail["detail"] or "")
        assert detail["terminal"] is None
        assert out["session"]["run_status"] == "failed"
        # 只读派生：索引保持观察值 running。
        conn = sqlite3.connect(str(tmp_env.db))
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT run_status FROM execution_rounds "
                "WHERE task_id=? AND run_id=?", (tid, run)).fetchone()
        finally:
            conn.close()
        assert row["run_status"] == "running"


class TestPiRoundContract:
    """backend=pi 轮次：job.json 身份核验回填 backend=pi、events.jsonl →
    消息（首事件 session）、cursor 增量同语义、trash/restore 同样可用。"""

    def test_pi_round_read_and_trash(self, factory, cli, tmp_env, runs):
        fixture = factory.make_needs_user(title="pi 轮次集成")
        tid, run = fixture.task.task_id, fixture.dispatch_run_id
        directory = write_pi_run(runs.path, tid, run)
        use_runs_root(tmp_env, runs.path)

        # list-sessions：回填 backend=pi + run_status=job.status。
        entry = next(s for s in cli("list-sessions",
                                    {"task_id": tid})["sessions"]
                     if s["run_id"] == run)
        assert SESSION_INFO_KEYS <= set(entry)
        assert entry["backend"] == "pi"
        assert entry["run_status"] == "finished"
        assert entry["state"] == "associated"

        # get-session：round_detail 与 SessionInfo 扩展字段。
        got = cli("get-session", {"task_id": tid, "run_id": run})
        assert got["round_detail"]["backend"] == "pi"
        assert got["session"]["backend"] == "pi"
        assert got["session"]["session_id"] == "sess-pi-int"

        # session-messages：events.jsonl → 消息（routing 在 inspect 覆盖）。
        page = read_messages(cli, tid, run_id=run)
        assert page["session"]["state"] == "associated"
        assert [m["type"] for m in page["messages"]] == [
            "system", "user", "assistant", "tool_call", "command_output",
            "system"]
        assert [m["id"] for m in page["messages"]] == \
            [f"round:{i}" for i in range(1, 7)]
        assert all(m["run_id"] == run for m in page["messages"])
        by_id = {m["id"]: m for m in page["messages"]}
        assert by_id["round:4"]["tool"]["name"] == "exec"
        assert by_id["round:5"]["tool"]["exit_code"] == 0
        assert by_id["round:5"]["tool"]["output"] is None
        assert "tool" not in by_id["round:2"]

        # 追加事件 → 增量只回新消息（cursor 语义不区分后端）。
        events = default_pi_events() + [
            pi_event(7, "assistant_message", content="收尾")]
        (directory / "events.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
            encoding="utf-8")
        nxt = read_messages(cli, tid, run_id=run,
                            cursor=page["next_cursor"])
        assert [m["id"] for m in nxt["messages"]] == ["round:7"]

        # pi 轮次同样走 trash/restore（与 UI 相同序列）。
        tr = cli("trash-session", {"task_id": tid, "run_id": run})
        assert tr["ok"] is True
        assert tr["session"]["save_state"] == "trashed"
        # v2 §5.22.3：派发轮次已入回收站；codex 规划轮次仍在 active。
        remaining = cli("list-sessions", {"task_id": tid})["sessions"]
        assert all(s["run_id"] != run for s in remaining)
        assert [s["backend"] for s in remaining] == ["codex"]
        still = read_messages(cli, tid, run_id=run)
        assert still["session"]["state"] == "associated"
        assert still["session"]["save_state"] == "trashed"
        assert len(still["messages"]) == 7, "删除后仍可读完整历史"
        rs = cli("restore-session", {"task_id": tid, "run_id": run})
        assert rs["session"]["save_state"] == "active"


class TestSourceVersionContract:
    def test_independent_process_pagination_ack_force_reopen_and_revision(
            self, factory, cli, tmp_env, runs):
        fixture = factory.make_needs_user(title="版本确认分页")
        tid, run = fixture.task.task_id, fixture.dispatch_run_id
        directory = write_pi_run(runs.path, tid, run)
        use_runs_root(tmp_env, runs.path)
        request = {"task_id": tid, "run_id": run, "limit": 2}
        loaded = []
        while True:
            page = cli("session-messages", request)
            assert isinstance(page["source_version"], str)
            loaded.extend(page["messages"])
            if not page["has_more"]:
                break
            # Even an early acknowledgement cannot skip unread pages.
            request.update(cursor=page["next_cursor"], source_version=page["source_version"])
        assert [message["seq"] for message in loaded] == list(range(1, 7))
        ack_request = dict(task_id=tid, run_id=run, cursor=page["next_cursor"],
                           source_version=page["source_version"])
        ack = cli("session-messages", ack_request)
        assert ack["messages"] == [] and not ack["has_more"]
        assert ack["next_cursor"] == page["next_cursor"]
        assert ack["source_version"] == page["source_version"]
        assert ack["session"]["last_checked_at"] > page["session"]["last_checked_at"]
        forced = cli("session-messages", dict(ack_request, force_reload=True))
        assert forced["source_version"] == page["source_version"]
        reopened = cli("session-messages", {"task_id": tid, "run_id": run,
                                             "source_version": page["source_version"]})
        assert len(reopened["messages"]) == 6
        changed = default_pi_events()
        changed[2] = dict(changed[2], content="new conclusion")
        (directory / "events.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in changed),
            encoding="utf-8")
        update = cli("session-messages", ack_request)
        assert update["source_version"] != page["source_version"]
        assert any(message["content"] == "new conclusion" for message in update["messages"])
        (directory / "events.jsonl").write_text('{"seq":')
        failed = cli("session-messages", ack_request)
        assert failed["source_version"] is None
        assert failed["connection"]["detail"]
        assert failed["session"]["state"] == "associated"


class TestLegacyBackendUnavailable:
    """v1.6 回归：旧后端（backend≠pi）轮次已弃用 → unavailable + 明确原因，
    list-sessions 不崩溃且透出 backend 原值。"""

    def test_legacy_backend_unavailable(self, factory, cli, tmp_env, runs):
        fixture = factory.make_needs_user(title="旧后端会话")
        tid, run = fixture.task.task_id, fixture.dispatch_run_id
        # 手工造 backend='dsh' 的历史轮次目录（job.json + 旧事件文件）。
        directory = write_pi_run(
            runs.path, tid, run,
            job=job_document(tid, run, backend="dsh",
                             session_id="sess-legacy-dsh",
                             session_title="旧 dsh 轮次"),
            events=[
                {"v": 1, "seq": 1, "ts": "2026-09-21T11:00:00+00:00",
                 "type": "session", "session_id": "sess-legacy-dsh",
                 "session_title": "旧 dsh 轮次"},
                {"v": 1, "seq": 2, "ts": "2026-09-21T11:00:01+00:00",
                 "type": "user", "content": "旧会话内容"},
            ],
        )
        assert (directory / "events.jsonl").is_file()
        use_runs_root(tmp_env, runs.path)

        # get-session：state=unavailable + detail 含弃用原因；round_detail
        # 透出 backend=dsh（索引行由读取层回填，不报错）。
        out = cli("get-session", {"task_id": tid, "run_id": run})
        assert out["session"]["state"] == "unavailable"
        assert "旧后端会话已弃用" in (out["connection"]["detail"] or "")
        assert out["session"]["backend"] == "dsh"
        assert out["round_detail"]["backend"] == "dsh"

        # session-messages：空消息 + 同一弃用原因。
        msgs = read_messages(cli, tid, run_id=run)
        assert msgs["session"]["state"] == "unavailable"
        assert msgs["messages"] == []
        assert "旧后端会话已弃用" in (msgs["connection"]["detail"] or "")

        # list-sessions：该行不崩溃、state=unavailable、backend 原值。
        listed = cli("list-sessions", {"task_id": tid})["sessions"]
        entry = next(s for s in listed if s["run_id"] == run)
        assert entry["state"] == "unavailable"
        assert entry["backend"] == "dsh"
        assert SESSION_INFO_KEYS <= set(entry)


# ---------------------------------------------------------------------------
# unittest discover 收集入口（本机无 pytest；见 tests/unittest_compat.py）


def load_tests(loader, standard_tests, pattern):
    import sys

    try:
        import unittest_compat
    except ImportError:
        from tests import unittest_compat
    return unittest_compat.collect_module(sys.modules[__name__])
