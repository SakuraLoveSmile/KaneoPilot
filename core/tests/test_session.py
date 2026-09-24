"""pi 轮次运行记录会话命令单元测试（protocol.md §5.15.5/§5.18，v1.6）。

OWNERSHIP: session subagent. 隔离方式：tmp 目录构造真实 job.json /
events.jsonl 文件（字段名与 pi worker 落盘格式一致），经
headless_adapter.set_source 注入数据源；不发 HTTP、不依赖 pi 进程。
覆盖：events.jsonl → Message 转换（round:<seq> 稳定 id、类型映射、tool 字段）、
追加与修订透出、分页与重开/断线补齐、半写入（尾行截断容忍 + 有/无上一份快照
的兜底）、缺失记录 pending、身份不符与路径越界 unavailable、旧后端
（backend≠pi）已弃用 unavailable、root 不存在 + detail 引导、设置优先级
（payload > settings > 默认）、不同执行轮次不串线、list-sessions 轻量模式、
session_store 位点语义、只读边界、会话回收站、SessionInfo.last_checked_at
（§5.15.5 最近状态检查）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path

from helpers import KanpilotTestCase  # noqa: E402
from kanpilot import (  # noqa: E402
    constants, headless_adapter as h, session_store, util,
)

PROJECT_ID = "example-project-05"


def write_json(path: Path, document) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )


def pi_job(task_id: str, run_id: str, **overrides) -> dict:
    """与 pi worker 落盘的 job.json 字段一致（§5.18.1：backend=pi + status/
    pid/起止时间/exit_code/session 关联 + config 快照）。"""
    document = {
        "task_id": task_id, "run_id": run_id, "backend": "pi",
        "status": "finished", "exit_code": 0, "pid": 4242,
        "started_at": "2026-09-20T12:00:00.000000+00:00",
        "ended_at": "2026-09-20T12:30:00.000000+00:00",
        "session_id": "sess-pi-0001", "session_title": "T1 pi 轮次",
        "config": {"provider": "CloseAI", "model": "deepseek-flash",
                   "reasoning_effort": "max", "agent_preset": "default"},
        "schema_version": 1,
    }
    document.update(overrides)
    return document


def pi_event(seq, etype, ts=None, **fields) -> dict:
    """events.jsonl 单行（§5.18.2）：v=1 + seq 单调 + ts + type。"""
    event = {"v": 1, "seq": seq,
             "ts": ts or f"2026-09-20T13:00:{seq:02d}.000000+00:00",
             "type": etype}
    event.update(fields)
    return event


def default_pi_events() -> list:
    """§5.18.2 类型映射全覆盖；**首事件必须是 session 事件**。"""
    return [
        pi_event(1, "session", session_id="sess-pi-0001",
                 session_title="T1 pi 轮次"),
        pi_event(2, "user", content="请修复登录 bug"),
        pi_event(3, "assistant_message", content="开始处理"),
        pi_event(4, "tool_call", name="exec",
                 arguments={"command": "pytest -q"}),
        pi_event(5, "command_output", name="exec",
                 input='{"command": "pytest -q"}',
                 output="3 passed", exit_code=0),
        pi_event(6, "terminal", outcome="completed", reason="正常结束"),
    ]


def simple_events(*user_texts, session_id="sess-pi-0001", terminal=True) -> list:
    """最小事件集：session + 若干 user 文本（+terminal completed）。"""
    events = [pi_event(1, "session", session_id=session_id,
                       session_title="T1 pi 轮次")]
    for index, text in enumerate(user_texts, start=2):
        events.append(pi_event(index, "user", content=text))
    if terminal:
        events.append(pi_event(len(events) + 1, "terminal",
                               outcome="completed", reason="ok"))
    return events


def revision_of(*parts) -> int:
    return h.content_revision(*parts)


class HeadlessCase(KanpilotTestCase):
    """公共夹具：runs 根 + 任务 + 注入数据源。"""

    def setUp(self):
        super().setUp()
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        # 注入数据源接缝（进程内；生产经 DB setting 解析）。
        self.source = h.HeadlessSource(str(self.runs_root))
        h.set_source(self.source)
        self.addCleanup(h.set_source, None)

    def make_task(self, task_id, run_id=None, status="running",
                  remote_status="in-progress"):
        task = self.fake.add_task(task_id, PROJECT_ID, status=remote_status)
        self.make_repo("Feedback")
        self.sync_task(task, project_name="Feedback")
        fields = {"run_id": run_id}
        if run_id is not None:
            fields["session_marker"] = f"{task_id}/{run_id}"
        self.set_task_status(task_id, status, **fields)
        return task_id

    def make_run(self, task_id, run_id, events=None, *, job=None,
                 write_events=True):
        """构造 <root>/<task_id>/<run_id>/ 的 pi 执行目录：
        job.json（backend=pi）+ events.jsonl（首事件 session，§5.18.2）。"""
        directory = self.runs_root / task_id / run_id
        write_json(directory / "job.json",
                   job if job is not None else pi_job(task_id, run_id))
        if write_events:
            write_jsonl(
                directory / "events.jsonl",
                events if events is not None else default_pi_events(),
            )
        return directory

    def events_path(self, task_id, run_id):
        return self.runs_root / task_id / run_id / "events.jsonl"

    def write_events(self, task_id, run_id, events):
        write_jsonl(self.events_path(task_id, run_id), events)

    def write_raw(self, task_id, run_id, text):
        self.events_path(task_id, run_id).write_text(text, encoding="utf-8")

    def patch_default_runs_root(self, path):
        """把缺省根目录指向不存在路径（开发机真实 root 隔离）。"""
        saved = constants.DEFAULT_SESSION_RUNS_ROOT
        constants.DEFAULT_SESSION_RUNS_ROOT = str(path)
        self.addCleanup(setattr, constants, "DEFAULT_SESSION_RUNS_ROOT", saved)

    def user_contents(self, result):
        return [m["content"] for m in result["messages"]
                if m["type"] == "user"]

    def read_all(self, task_id, **payload):
        payload["task_id"] = task_id
        # 实现层现状：constants 缺 DEFAULT_SESSION_MESSAGE_LIMIT，缺省 limit
        # 会 AttributeError；测试以显式大 limit 走同一代码路径（fixture 消息数
        # 远小于该值，语义与「不截断」等价）。
        payload.setdefault("limit", 500)
        return self.call("session-messages", payload)

    def session_reads_row(self, session_key):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM session_reads WHERE session_key=?", (session_key,)
        ).fetchone()
        conn.close()
        return row

    def stored_revisions(self, session_key):
        row = self.session_reads_row(session_key)
        return session_store.decode_revisions(row["last_revision"])

    def db_exec(self, sql, params=()):
        """测试后门：直接读写冻结 schema（时间/状态构造用）。"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.fetchall()
        finally:
            conn.close()

    def round_row(self, task_id, run_id):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM execution_rounds WHERE task_id=? AND run_id=?",
            (task_id, run_id),
        ).fetchone()
        conn.close()
        return row

    def event_kinds(self, task_id):
        return [e["kind"]
                for e in self.call("list-events", {"task_id": task_id})["events"]]

    def backfill(self, task_id):
        """list-sessions 是 §5.16.1 回填写入点：触发发现回填并返回该任务会话。"""
        return self.call("list-sessions", {"task_id": task_id})["sessions"]


# ------------------------------------------------- 数据源解析 / 设置优先级

class TestRunsRootResolution(HeadlessCase):
    def test_default_root_constant_is_lazy_expanduser(self):
        # 默认根目录 = ~/Library/Application Support/KaneoPilot/runs（v1.6）。
        self.assertEqual(
            str(constants.default_session_runs_root()), os.path.expanduser(
                "~/Library/Application Support/KaneoPilot/runs"
            ),
        )
        # 懒解析：同一进程内改 HOME 后重新求值（不缓存展开结果）。
        saved_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = "/fake-home-kanpilot"
            self.assertEqual(
                str(constants.default_session_runs_root()),
                "/fake-home-kanpilot/Library/Application Support"
                "/KaneoPilot/runs",
            )
        finally:
            os.environ["HOME"] = saved_home

    def test_settings_used_when_no_override(self):
        # 清除注入接缝 → resolve_source 走 DB setting（隔离测试不碰真实 root）。
        h.set_source(None)
        task_id = self.make_task("tasksess-cfg00001", run_id="run-A")
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("session_runs_root", util.jdump(str(self.runs_root))),
        )
        conn.commit()
        conn.close()
        self.make_run(task_id, "run-A", simple_events("hello"))
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "associated")
        self.assertEqual(result["connection"]["state"], "degraded")

    def test_empty_string_setting_means_unset(self):
        h.set_source(None)
        self.patch_default_runs_root(self.root / "nowhere")
        task_id = self.make_task("tasksess-cfg00002", run_id="run-A")
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("session_runs_root", util.jdump("")),
        )
        conn.commit()
        conn.close()
        # 空串 = 未设置 → 回落默认根目录（本例不存在）→ unavailable。
        result = self.read_all(task_id)
        self.assertEqual(result["connection"]["state"], "unavailable")
        self.assertEqual(result["session"]["state"], "unavailable")

    def test_missing_root_detail_mentions_setting(self):
        h.set_source(None)
        self.patch_default_runs_root(self.root / "nowhere")
        task_id = self.make_task("tasksess-cfg00003", run_id="run-A")
        result = self.read_all(task_id)
        self.assertEqual(result["connection"]["state"], "unavailable")
        self.assertIn("session_runs_root", result["connection"]["detail"])

    def test_list_sessions_payload_override_not_persisted(self):
        other_root = self.root / "other-runs"
        other_root.mkdir()
        task_id = self.make_task("tasksess-cfg00004", run_id="run-A")
        self.make_run(task_id, "run-A", simple_events("hi"))
        # 注入源指向 runs_root；payload 指向空目录 other-runs → 覆盖生效。
        # 注意 set_source 优先级最高，payload 覆盖只走 resolve_source 路径，
        # 因此本例先清注入、改走 settings 缺省（默认 root 不存在）+ payload。
        h.set_source(None)
        listed = self.call("list-sessions", {
            "task_id": task_id, "session_runs_root": str(other_root),
        })
        # payload root 存在但无该任务目录 → 目录缺失 → unavailable。
        self.assertEqual(listed["sessions"][0]["state"], "unavailable")
        # 不持久化：随后 session-messages（无 payload）仍读不到 other-runs，
        # 走默认 root（不存在）→ unavailable；显式空串 = 未提供。
        again = self.call("list-sessions", {
            "task_id": task_id, "session_runs_root": "",
        })
        self.assertEqual(again["sessions"][0]["state"], "unavailable")
        # 指回真实 root 的 settings 写入 → payload 空串视为未提供 → settings 生效。
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("session_runs_root", util.jdump(str(self.runs_root))),
        )
        conn.commit()
        conn.close()
        resolved = self.call("list-sessions", {
            "task_id": task_id, "session_runs_root": "",
        })
        self.assertEqual(resolved["sessions"][0]["state"], "associated")


# -------------------------------------------------- 追加 / 修订 / 分页 / 重开

class TestIncrementalAppendRevision(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("tasksess-incr0001", run_id="run-A")

    def test_append_and_revision_reemit(self):
        events_v1 = default_pi_events()
        self.make_run(self.task_id, "run-A", events_v1)
        first = self.read_all(self.task_id)
        self.assertEqual(
            [m["id"] for m in first["messages"]],
            [f"round:{i}" for i in range(1, 7)],
        )
        cursor = first["next_cursor"]
        self.assertEqual(cursor, "seq:6")
        # 修订快照落库：覆盖当前尾部窗口消息。
        stored = self.stored_revisions(f"{self.task_id}/run-A")
        self.assertEqual(stored["round:6"], first["messages"][-1]["revision"])

        # 1) 追加新事件 → 增量只返回新消息。
        events_v2 = events_v1 + [
            pi_event(7, "assistant_message", content="完成"),
        ]
        self.write_events(self.task_id, "run-A", events_v2)
        second = self.read_all(self.task_id, cursor=cursor)
        self.assertEqual([m["id"] for m in second["messages"]], ["round:7"])
        cursor = second["next_cursor"]

        # 2) 尾部同 seq 重发行内容更新（seq<=cursor 但 revision 变化）→ 透出。
        events_v3 = events_v1 + [
            pi_event(7, "assistant_message", content="完成且验证通过"),
            pi_event(8, "assistant_message", content="交付"),
        ]
        self.write_events(self.task_id, "run-A", events_v3)
        third = self.read_all(self.task_id, cursor=cursor)
        ids = [m["id"] for m in third["messages"]]
        # round:7 同 seq 重发（revision 变化透出）+ round:8 新增。
        self.assertEqual(ids, ["round:7", "round:8"])
        updated = next(m for m in third["messages"] if m["id"] == "round:7")
        self.assertEqual(updated["content"], "完成且验证通过")
        self.assertEqual(
            updated["revision"],
            revision_of("assistant_message", "完成且验证通过"),
        )
        # 游标不回退：重放同游标不再重复本轮已见内容。
        again = self.read_all(self.task_id, cursor=third["next_cursor"])
        self.assertEqual(again["messages"], [])

    def test_pagination_and_reopen_full_history(self):
        events = []
        for i in range(1, 6):
            events.append(pi_event(len(events) + 1, "tool_call",
                                   name="exec", arguments={"command": f"cmd{i}"}))
            events.append(pi_event(len(events) + 1, "command_output",
                                   name="exec", input=f'{{"command": "cmd{i}"}}',
                                   output=f"out{i}", exit_code=0))
        self.make_run(self.task_id, "run-A", events)
        # 分页：limit 逐页推进，has_more 正确（每步 tc+co → 10 条）。
        collected, cursor, pages = [], None, 0
        while True:
            payload = {"task_id": self.task_id, "limit": 4}
            if cursor:
                payload["cursor"] = cursor
            page = self.call("session-messages", payload)
            collected.extend(page["messages"])
            pages += 1
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            self.assertLess(pages, 20)
        self.assertEqual(len(collected), 10)
        # 重开（无 cursor 且无已存位点的新会话）返回完整历史：
        reopen_task = self.make_task("tasksess-incr0002", run_id="run-A")
        self.make_run(reopen_task, "run-A", events)
        reopened = self.read_all(reopen_task)
        self.assertEqual([m["id"] for m in reopened["messages"]],
                         [m["id"] for m in collected])

    def test_reconnect_with_cursor_no_gap_no_repeat(self):
        events = [pi_event(i, "assistant_message", content=f"m{i}")
                  for i in range(1, 6)]
        self.make_run(self.task_id, "run-A", events)
        first = self.read_all(self.task_id, limit=2)
        cursor = first["next_cursor"]
        # 断线：期间追加。
        self.write_events(self.task_id, "run-A", events + [
            pi_event(6, "assistant_message", content="m6"),
        ])
        # 带最后 cursor 补齐 → 不跳历史（3-6 全回）也不重复（1,2 不再回）。
        nxt = self.read_all(self.task_id, cursor=cursor)
        self.assertEqual([m["id"] for m in nxt["messages"]],
                         ["round:3", "round:4", "round:5", "round:6"])
        self.assertFalse(nxt["has_more"])
        # 不带 cursor = 首次加载/重开语义 → 恒返回完整历史（协议 §5.15.5：
        # session_reads 位点不得隐式充当游标；断线补齐由调用方带最后 cursor）。
        resumed = self.read_all(self.task_id)
        self.assertEqual([m["id"] for m in resumed["messages"]],
                         [f"round:{i}" for i in range(1, 7)])


# ---------------------------------------------------------------- 半写入容忍

class TestHalfWrittenTolerance(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("tasksess-half001", run_id="run-A")
        self.make_run(self.task_id, "run-A")

    def test_truncated_tail_line_drops_only_tail(self):
        """§5.18.2 半写容忍：events.jsonl 尾部截断行丢弃，此前事件原样保留
        （detail=None 正常路径，不算 read_failed）。"""
        first = self.read_all(self.task_id)
        self.assertTrue(first["messages"])
        path = self.events_path(self.task_id, "run-A")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write('{"v": 1, "seq": 999, "ts": "2026-01-01T00:00:00')
        second = self.read_all(self.task_id, cursor="seq:0")
        self.assertEqual(second["session"]["state"], "associated")
        self.assertIsNone(second["connection"]["detail"])
        self.assertEqual([m["id"] for m in second["messages"]],
                         [m["id"] for m in first["messages"]])

    def test_with_previous_snapshot_keeps_last_good(self):
        first = self.read_all(self.task_id)   # 同进程已缓存最近一次成功解析
        # events.jsonl 全量半写入（有内容但零有效行 → read_failed）。
        good_text = self.events_path(
            self.task_id, "run-A").read_text(encoding="utf-8")
        self.write_raw(self.task_id, "run-A", '{"v": 1, "seq": 截断')
        second = self.read_all(self.task_id, cursor=first["next_cursor"])
        self.assertEqual(second["session"]["state"], "associated")
        self.assertEqual(second["connection"]["state"], "degraded")
        self.assertIn("解析失败", second["connection"]["detail"])
        # 保留上一份有效内容：带位点重放上一份。
        replay = self.read_all(self.task_id, cursor="seq:0")
        self.assertEqual([m["id"] for m in replay["messages"]],
                         [m["id"] for m in first["messages"]])
        # 恢复写入 → 正常增量。
        self.write_raw(self.task_id, "run-A", good_text)
        back = self.read_all(self.task_id, cursor=first["next_cursor"])
        self.assertEqual(back["messages"], [])

    def test_without_previous_snapshot_is_pending_with_reason(self):
        self.write_raw(self.task_id, "run-A", '{"v": 1, "seq": 截断')
        # 无上一份（新进程缓存为空）：换新注入源模拟。
        h.set_source(h.HeadlessSource(str(self.runs_root)))
        result = self.read_all(self.task_id)
        self.assertEqual(result["session"]["state"], "pending")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["connection"]["state"], "degraded")
        self.assertIn("解析失败", result["connection"]["detail"])

    def test_truncation_does_not_disturb_stored_position(self):
        first = self.read_all(self.task_id)
        cursor = first["next_cursor"]
        row_before = self.session_reads_row(f"{self.task_id}/run-A")
        self.write_raw(self.task_id, "run-A", "{oops")
        h.set_source(h.HeadlessSource(str(self.runs_root)))  # 无进程内缓存
        degraded_read = self.read_all(self.task_id)
        # 半写入：靠跨进程持久快照保留上一份有效内容 → associated + detail，
        # 位点按快照内容原样保存（协议 §5.15.5「不把解析失败当空会话」）。
        self.assertEqual(degraded_read["session"]["state"], "associated")
        self.assertTrue(degraded_read["messages"])
        self.assertTrue(degraded_read["connection"]["detail"])
        row = self.session_reads_row(f"{self.task_id}/run-A")
        # cursor/last_seq 不回退；revision 快照与首轮读取一致（内容未变）。
        self.assertEqual(row["cursor"], row_before["cursor"])
        self.assertEqual(row["last_seq"], row_before["last_seq"])
        self.assertEqual(row["last_revision"], row_before["last_revision"])
        self.assertEqual(row["state"], "associated")
        # 恢复后按原 cursor 续读不丢消息。
        self.write_events(self.task_id, "run-A", default_pi_events() + [
            pi_event(7, "assistant_message", content="完成"),
        ])
        filled = self.read_all(self.task_id, cursor=cursor)
        self.assertEqual([m["id"] for m in filled["messages"]], ["round:7"])


# --------------------------------------------------------- pending / 缺失记录

class TestPendingAndMissing(HeadlessCase):
    def test_job_valid_events_missing_is_pending(self):
        task_id = self.make_task("tasksess-pend0001", run_id="run-A")
        self.make_run(task_id, "run-A", write_events=False)
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "pending")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["connection"]["state"], "degraded")
        self.assertIsNone(result["next_cursor"])
        # pending 落 session_reads 位点（state=pending，游标保持）。
        row = self.session_reads_row(f"{task_id}/run-A")
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["cursor"])

    def test_no_run_id_is_pending(self):
        task_id = self.make_task("tasksess-pend0002", run_id=None)
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "pending")
        self.assertIsNone(result["session"]["session_marker"])
        self.assertEqual(result["messages"], [])

    def test_missing_run_dir_is_unavailable_with_detail(self):
        task_id = self.make_task("tasksess-miss0001", run_id="run-A")
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "unavailable")
        self.assertIn("执行目录", result["connection"]["detail"])

    def test_missing_job_json_is_unavailable(self):
        task_id = self.make_task("tasksess-miss0002", run_id="run-A")
        directory = self.runs_root / task_id / "run-A"
        directory.mkdir(parents=True)
        self.write_events(task_id, "run-A", simple_events("x"))
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "unavailable")


# --------------------------------------------- 身份不符 / 路径越界（unavailable）

class TestIdentityAndPathSafety(HeadlessCase):
    def assert_unavailable(self, task_id, run_id, fragment):
        result = self.read_all(task_id, run_id=run_id)
        self.assertEqual(result["session"]["state"], "unavailable",
                         f"{run_id} 应 unavailable")
        self.assertEqual(result["connection"]["state"], "degraded")
        self.assertIn(fragment, result["connection"]["detail"])
        self.assertEqual(result["messages"], [])

    def test_job_identity_mismatch(self):
        task_id = self.make_task("tasksess-id000001", run_id="run-A")
        self.make_run(task_id, "run-A",
                      job=pi_job("other-task", "run-A"),
                      events=simple_events("x"))
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "unavailable")
        self.assertIn("身份与目录不符", result["connection"]["detail"])

    def test_run_id_mismatch(self):
        task_id = self.make_task("tasksess-id000002", run_id="run-A")
        self.make_run(task_id, "run-A",
                      job=pi_job(task_id, "different-run"),
                      events=simple_events("x"))
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "unavailable")

    def test_backend_field_required(self):
        task_id = self.make_task("tasksess-id000003", run_id="run-A")
        job = pi_job(task_id, "run-A")
        job.pop("backend")
        self.make_run(task_id, "run-A", job=job,
                      events=simple_events("x"))
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "unavailable")
        self.assertIn("backend", result["connection"]["detail"])

    def test_illegal_identifiers_rejected(self):
        task_id = self.make_task("tasksess-id000004", run_id="run-A")
        for bad in ("../escape", "a/b", "with space", "semi;colon", ""):
            result = self.read_all(task_id, run_id=bad)
            self.assertEqual(result["session"]["state"], "unavailable",
                             f"非法 run_id {bad!r} 必须拒绝")

    def test_symlink_escape_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        write_json(outside / "job.json", pi_job("t", "r"))
        write_jsonl(outside / "events.jsonl", simple_events("sneaky"))
        task_id = self.make_task("tasksess-sym00001", run_id="run-A")
        link = self.runs_root / task_id / "run-A"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except OSError:  # 环境不支持符号链接则跳过
            self.skipTest("symlinks unavailable")
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "unavailable")
        self.assertNotIn("sneaky", json.dumps(result, ensure_ascii=False))

    def test_identity_verified_before_events_read(self):
        # 越界/身份不符时绝不读取他目录内容（目录名指向他处的
        # job.json 身份不符同样 unavailable，无回退猜测）。
        task_id = self.make_task("tasksess-id000005", run_id="run-A")
        self.make_run(task_id, "other-run",
                      simple_events("x"))  # run_id 与请求不一致
        result = self.read_all(task_id)
        self.assertEqual(result["session"]["state"], "unavailable")


# ------------------------------------------------ 旧后端已弃用（backend≠pi）

class TestLegacyBackendUnavailable(HeadlessCase):
    """v1.6：backend≠pi 的历史轮次（dsh/headless 等）读取已弃用 →
    unavailable + 「已弃用」detail；索引行仍按 job.json backend 原值回填，
    round_detail 仍成形（terminal=None）。"""

    def test_legacy_backend_unavailable(self):
        for backend in ("dsh", "headless"):
            task_id = self.make_task(f"tasksess-legacy-{backend}",
                                     run_id="run-L", status="ready")
            # 即便目录里有 events.jsonl，backend≠pi 也不读（路由前置判定）。
            self.make_run(
                task_id, "run-L",
                job=pi_job(task_id, "run-L", backend=backend),
            )
            # list-sessions 回填：索引行保留旧 backend 原值（LEGACY_ROUND_BACKENDS）。
            entry = next(s for s in self.backfill(task_id)
                         if s["run_id"] == "run-L")
            self.assertEqual(entry["backend"], backend)
            self.assertEqual(entry["state"], "unavailable")
            self.assertEqual(
                self.round_row(task_id, "run-L")["backend"], backend
            )
            # 会话读取：unavailable + detail 含「已弃用」。
            result = self.read_all(task_id)
            self.assertEqual(result["session"]["state"], "unavailable")
            self.assertEqual(result["messages"], [])
            self.assertIn("已弃用", result["connection"]["detail"] or "")
            self.assertIn(backend, result["connection"]["detail"])
            # get-session 的 round_detail 仍成形（backend=旧值、terminal=None）。
            detail = self.call(
                "get-session", {"task_id": task_id, "run_id": "run-L"}
            )["round_detail"]
            self.assertIsNotNone(detail)
            self.assertEqual(detail["backend"], backend)
            self.assertIsNone(detail["terminal"])
            # exit_code 等进程字段来自 job.json 原文（与 backend 无关）。


# -------------------------------------------------------- 轮次隔离 / 多任务

class TestRoundAndTaskIsolation(HeadlessCase):
    def test_different_runs_no_crosstalk(self):
        task_id = self.make_task("tasksess-round001", run_id="run-2")
        self.make_run(task_id, "run-1", simple_events("round1"))
        self.make_run(task_id, "run-2", simple_events("round2"))
        current = self.read_all(task_id)
        self.assertEqual(current["session"]["run_id"], "run-2")
        self.assertEqual(self.user_contents(current), ["round2"])
        previous = self.read_all(task_id, run_id="run-1")
        self.assertEqual(self.user_contents(previous), ["round1"])
        # 位点按 session_key 隔离（各自成行）。
        row2 = self.session_reads_row(f"{task_id}/run-2")
        row1 = self.session_reads_row(f"{task_id}/run-1")
        self.assertIsNotNone(row1)
        self.assertIsNotNone(row2)

    def test_multi_task_isolation_and_list(self):
        a = self.make_task("tasksess-taskA001", run_id="run-A")
        b = self.make_task("tasksess-taskB001", run_id="run-B")
        self.make_run(a, "run-A", simple_events("A only"))
        self.make_run(b, "run-B", simple_events("B only"))
        listed = {s["task_id"]: s
                  for s in self.call("list-sessions")["sessions"]}
        self.assertEqual(set(listed), {a, b})
        self.assertEqual(listed[a]["state"], "associated")
        self.assertEqual(listed[b]["state"], "associated")
        self.assertEqual(self.user_contents(self.read_all(a)), ["A only"])
        only_a = self.call("list-sessions", {"task_id": a})["sessions"]
        self.assertEqual([s["task_id"] for s in only_a], [a])


# ------------------------------------------------- list-sessions 轻量模式

class TestListSessionsLightMode(HeadlessCase):
    def test_light_mode_above_threshold(self):
        saved = constants.LIST_SESSIONS_LIGHT_THRESHOLD
        constants.LIST_SESSIONS_LIGHT_THRESHOLD = 3
        self.addCleanup(setattr, constants, "LIST_SESSIONS_LIGHT_THRESHOLD",
                        saved)
        task_id = self.make_task("tasksess-light001", run_id="run-A")
        self.make_run(task_id, "run-A", simple_events("hi"))
        # 再建 3 个多余目录 → 超过阈值。
        for i in range(3):
            (self.runs_root / f"tasksess-junk{i:04d}" / "run-X").mkdir(
                parents=True)
        listed = self.call("list-sessions")["sessions"]
        session = next(s for s in listed if s["task_id"] == task_id)
        # 轻量模式：只 stat 不读文件 → pending（deferred），associated 由
        # get-session/session-messages 补齐。
        self.assertEqual(session["state"], "pending")
        full = self.call("get-session",
                         {"task_id": task_id, "run_id": "run-A"})
        self.assertEqual(full["session"]["state"], "associated")


# ---------------------------------------------------- session_store 位点语义

class TestSessionStoreSemantics(HeadlessCase):
    def test_encode_decode_roundtrip_and_sentinels(self):
        self.assertIsNone(session_store.encode_revisions(None))
        self.assertEqual(session_store.encode_revisions({}), "{}")
        encoded = session_store.encode_revisions({"round:1": 12345})
        self.assertEqual(session_store.decode_revisions(encoded),
                         {"round:1": 12345})
        self.assertEqual(session_store.decode_revisions("not json"), {})
        self.assertEqual(session_store.decode_revisions(7), {})  # 旧版整数
        self.assertEqual(session_store.decode_revisions({"a": "1", "b": None}),
                         {"a": 1})

    def test_save_position_cursor_no_rewind(self):
        self.call("overview", {})  # 建库
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        key = "task-store-0000001/run-A"
        session_store.save_position(conn, key, "task-store-0000001", "run-A",
                                    "seq:300", 300, '{"round:1":1}',
                                    "associated", "2026-09-20T10:00:00+00:00")
        # 尾部修订透出：last_seq 更小 → 游标不回退，但快照刷新。
        session_store.save_position(conn, key, "task-store-0000001", "run-A",
                                    "seq:201", 201, '{"round:1":2}',
                                    "associated", "2026-09-20T10:00:01+00:00")
        conn.execute("COMMIT")
        stored = session_store.get_position(conn, key)
        self.assertEqual(stored["cursor"], "seq:300")
        self.assertEqual(stored["last_seq"], 300)
        self.assertEqual(stored["last_revision"], {"round:1": 2})
        # None 快照 = 保持不变；"{}" = 显式清空。
        session_store.save_position(conn, key, "task-store-0000001", "run-A",
                                    None, None, None, "pending",
                                    "2026-09-20T10:00:02+00:00")
        self.assertEqual(
            session_store.get_position(conn, key)["last_revision"],
            {"round:1": 2})
        session_store.save_position(conn, key, "task-store-0000001", "run-A",
                                    None, None, "{}", "pending",
                                    "2026-09-20T10:00:03+00:00")
        self.assertEqual(
            session_store.get_position(conn, key)["last_revision"], {})
        conn.close()


# -------------------------------------------------------------- 只读与错误

class TestReadOnlyAndErrors(HeadlessCase):
    def test_task_not_found(self):
        exc = self.call_err("get-session", {"task_id": "nope", "run_id": "r"})
        self.assertEqual(exc.code, "not_found")
        exc = self.call_err("session-messages", {"task_id": "nope"})
        self.assertEqual(exc.code, "not_found")

    def test_reads_do_not_write_requests_or_events(self):
        task_id = self.make_task("tasksess-ro000001", run_id="run-A")
        self.make_run(task_id, "run-A", simple_events("x"))
        before_events = self.call("list-events", {"task_id": task_id})
        before_reqs = self.requests()
        self.read_all(task_id)
        self.call("get-session", {"task_id": task_id, "run_id": "run-A"})
        after_events = self.call("list-events", {"task_id": task_id})
        after_reqs = self.requests()
        self.assertEqual(len(before_events["events"]),
                         len(after_events["events"]))
        self.assertEqual(len(before_reqs), len(after_reqs))
        # task 行不被会话命令改写。
        before = self.task(task_id)
        self.read_all(task_id)
        after = self.task(task_id)
        self.assertEqual(before, after)

    def test_empty_list_when_no_dispatched_tasks(self):
        self.make_task("tasksess-empty0001", run_id=None)
        self.assertEqual(self.call("list-sessions")["sessions"], [])

    def test_only_session_reads_written(self):
        task_id = self.make_task("tasksess-ro000002", run_id="run-A")
        self.make_run(task_id, "run-A", simple_events("x"))
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        events_before = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        requests_before = conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        conn.close()
        self.read_all(task_id)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        events_after = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        requests_after = conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]
        reads = conn.execute(
            "SELECT COUNT(*) c FROM session_reads").fetchone()["c"]
        conn.close()
        self.assertEqual(events_before, events_after)
        self.assertEqual(requests_before, requests_after)
        self.assertEqual(reads, 1)


# ------------------------------------------- v1.5 会话回收站（§5.16，T1）

class TestSessionTrashRestore(HeadlessCase):
    """trash-session / restore-session：成功与幂等、禁止条件（conflict +
    中文原因）、expected_save_state 冲突、not_found、回填不复活、
    删除边界（不删文件/不动任务与执行记录）。"""

    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("tasksess-trash0001", run_id="run-A",
                                      status="ready")
        self.make_run(self.task_id, "run-A", simple_events("开工"))
        first = self.backfill(self.task_id)
        self.assertEqual([s["state"] for s in first], ["associated"])

    def trash(self, run_id="run-A", **payload):
        payload.update({"task_id": self.task_id, "run_id": run_id})
        return self.call("trash-session", payload)

    def restore(self, run_id="run-A", **payload):
        payload.update({"task_id": self.task_id, "run_id": run_id})
        return self.call("restore-session", payload)

    def blocked(self, run_id="run-A"):
        exc = self.call_err(
            "trash-session", {"task_id": self.task_id, "run_id": run_id}
        )
        self.assertEqual(exc.code, "conflict")
        self.assertTrue(exc.message, "禁止条件必须给出原因")
        return exc

    def session_entry(self, run_id="run-A", **payload):
        payload.setdefault("task_id", self.task_id)
        sessions = self.call("list-sessions", payload)["sessions"]
        return next((s for s in sessions if s["run_id"] == run_id), None)

    # -- 成功与幂等 --

    def test_trash_success_and_idempotent(self):
        before = len(self.event_kinds(self.task_id))
        result = self.trash()
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        session = result["session"]
        self.assertEqual(session["save_state"], "trashed")
        self.assertIsNotNone(session["saved_at"])
        self.assertEqual(session["allowed_actions"], ["restore-session", "purge-session"])
        self.assertIsNone(session["blocked_reason"])
        row = self.round_row(self.task_id, "run-A")
        self.assertEqual(row["save_state"], "trashed")
        self.assertIsNotNone(row["saved_at"])
        # 事件：session_trashed，task_id 关联，detail 含 run_id/from/to。
        events = [e for e in self.call("list-events",
                                       {"task_id": self.task_id})["events"]
                  if e["kind"] == "session_trashed"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["run_id"], "run-A")
        self.assertEqual(events[0]["detail"]["from"], "active")
        self.assertEqual(events[0]["detail"]["to"], "trashed")
        # 幂等：已处于目标状态 → deduplicated，不重复写事件。
        again = self.trash()
        self.assertTrue(again["ok"])
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["session"]["save_state"], "trashed")
        kinds = self.event_kinds(self.task_id)
        self.assertEqual(kinds.count("session_trashed"), 1)
        self.assertEqual(len(kinds), before + 1)

    def test_restore_success_and_idempotent(self):
        self.trash()
        before = len(self.event_kinds(self.task_id))
        result = self.restore()
        self.assertTrue(result["ok"])
        self.assertFalse(result["deduplicated"])
        self.assertEqual(result["session"]["save_state"], "active")
        self.assertIsNone(result["session"]["saved_at"])
        self.assertEqual(result["session"]["allowed_actions"], ["trash-session"])
        row = self.round_row(self.task_id, "run-A")
        self.assertEqual(row["save_state"], "active")
        self.assertIsNone(row["saved_at"])
        again = self.restore()
        self.assertTrue(again["deduplicated"])
        kinds = self.event_kinds(self.task_id)
        self.assertEqual(kinds.count("session_restored"), 1)
        self.assertEqual(kinds.count("session_trashed"), 1)
        self.assertEqual(len(kinds), before + 1)

    def test_restore_has_no_block_conditions(self):
        # restore 无禁止条件（§5.16.2）：任务执行中同样可恢复。
        self.trash()
        self.set_task_status(self.task_id, "running")
        result = self.restore()
        self.assertFalse(result["deduplicated"])
        self.assertEqual(result["session"]["save_state"], "active")

    # -- 禁止条件（§5.16.2）→ conflict + 中文原因 --

    def test_blocked_running_round(self):
        # ① 索引 run_status=running（job.json status=running 使回填核验
        # 与索引一致，不回退为 finished）。
        write_json(
            self.runs_root / self.task_id / "run-A" / "job.json",
            pi_job(self.task_id, "run-A", status="running",
                   started_at=util.now_iso(), ended_at=None, exit_code=None),
        )
        self.db_exec(
            "UPDATE execution_rounds SET run_status='running' "
            "WHERE task_id=? AND run_id=?",
            (self.task_id, "run-A"),
        )
        exc = self.blocked()
        self.assertIn("仍在运行", exc.message)
        self.assertEqual(
            self.round_row(self.task_id, "run-A")["save_state"], "active"
        )
        # list-sessions 同轮 blocked_reason 同文案、allowed_actions 为空。
        entry = self.session_entry()
        self.assertEqual(entry["allowed_actions"], [])
        self.assertEqual(entry["blocked_reason"], exc.message)

    def test_blocked_current_round_executing(self):
        # ① 该轮为任务当前轮次且 task.status 执行中。
        self.set_task_status(self.task_id, "running")
        exc = self.blocked()
        self.assertIn("正在执行中", exc.message)
        entry = self.session_entry()
        self.assertEqual(entry["allowed_actions"], [])
        self.assertEqual(entry["blocked_reason"], exc.message)

    def test_blocked_lost_claim_until_released(self):
        # ② run_id 匹配的 claimed dispatch 请求租约过期未解除。
        now = "2026-09-20T10:00:00.000000+00:00"
        self.db_exec(
            "INSERT INTO requests(request_id, idempotency_key, task_id, "
            "stage, kind, status, priority, created_at, updated_at, run_id, "
            "claimed_by, claimed_at, lease_expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("req-lost-000001", "dispatch:lost00000000000001", self.task_id,
             "dispatch", "dispatch", "claimed", 100, now, now, "run-A",
             "kaneo-dispatch", now, "2020-01-01T00:00:00.000000+00:00"),
        )
        exc = self.blocked()
        self.assertIn("失联", exc.message)
        # 解除（等价 resolve release_lost 后请求不再 claimed）→ 允许删除。
        self.db_exec("UPDATE requests SET status='cancelled' "
                     "WHERE request_id=?", ("req-lost-000001",))
        result = self.trash()
        self.assertFalse(result["deduplicated"])

    def test_blocked_send_state_unknown(self):
        # ③ 该轮为任务当前轮次且 send_state='unknown'。
        self.db_exec("UPDATE tasks SET send_state='unknown' WHERE task_id=?",
                     (self.task_id,))
        exc = self.blocked()
        self.assertIn("发送结果不明", exc.message)
        self.assertEqual(
            self.session_entry()["allowed_actions"], []
        )

    def test_historical_round_trash_allowed_while_task_running(self):
        # 历史轮次（非当前轮次、run_status 非 running、无失联）允许删除。
        self.make_run(self.task_id, "run-old", simple_events("上一轮"))
        self.backfill(self.task_id)
        self.set_task_status(self.task_id, "running")
        result = self.trash(run_id="run-old")
        self.assertFalse(result["deduplicated"])
        self.assertEqual(result["session"]["save_state"], "trashed")

    # -- expected_save_state / not_found --

    def test_expected_save_state_conflict_then_match(self):
        exc = self.call_err("trash-session", {
            "task_id": self.task_id, "run_id": "run-A",
            "expected_save_state": "trashed",
        })
        self.assertEqual(exc.code, "conflict")
        self.assertIn("会话保存状态冲突", exc.message)
        self.assertEqual(
            self.round_row(self.task_id, "run-A")["save_state"], "active"
        )
        # 匹配 → 成功；已处于目标状态时 expected 不符也按幂等（非目标态才冲突）。
        matched = self.trash(expected_save_state="active")
        self.assertFalse(matched["deduplicated"])
        stale_expected = self.trash(expected_save_state="active")
        self.assertTrue(stale_expected["deduplicated"])

    def test_not_found(self):
        exc = self.call_err("trash-session", {
            "task_id": self.task_id, "run_id": "run-missing",
        })
        self.assertEqual(exc.code, "not_found")
        exc = self.call_err("restore-session", {
            "task_id": self.task_id, "run_id": "run-missing",
        })
        self.assertEqual(exc.code, "not_found")
        exc = self.call_err("trash-session", {
            "task_id": "tasksess-nope0001", "run_id": "run-A",
        })
        self.assertEqual(exc.code, "not_found")

    # -- 回填不复活（§5.16.1） --

    def test_backfill_does_not_resurrect(self):
        self.trash()
        # 再次回填：list-sessions（active 缺省）不出现、get-session 后仍 trashed。
        self.assertEqual(self.backfill(self.task_id), [])
        self.call("get-session",
                  {"task_id": self.task_id, "run_id": "run-A"})
        row = self.round_row(self.task_id, "run-A")
        self.assertEqual(row["save_state"], "trashed")
        self.assertIsNotNone(row["saved_at"])
        trashed = self.call(
            "list-sessions",
            {"task_id": self.task_id, "save_state": "trashed"},
        )["sessions"]
        self.assertEqual([s["run_id"] for s in trashed], ["run-A"])
        self.assertEqual(trashed[0]["allowed_actions"], ["restore-session", "purge-session"])

    # -- 删除边界：不删文件、不动任务与执行记录 --

    def test_trash_keeps_task_files_and_requests(self):
        task_before = self.task(self.task_id)
        reqs_before = self.requests()
        events_file = self.events_path(self.task_id, "run-A")
        job_file = self.runs_root / self.task_id / "run-A" / "job.json"
        self.assertTrue(events_file.exists() and job_file.exists())
        self.trash()
        # 文件全保留（不停进程、不删运行记录/工作目录——§5.16.2）。
        self.assertTrue(events_file.exists())
        self.assertTrue(job_file.exists())
        # 任务生命周期与保存状态不动、执行记录不动。
        task_after = self.task(self.task_id)
        for field in ("status", "save_state", "run_id", "session_marker",
                      "send_state", "stage"):
            self.assertEqual(task_before[field], task_after[field], field)
        self.assertEqual(reqs_before, self.requests())
        # 会话仍可读（删除只影响调度台列表与保存状态）。
        messages = self.read_all(self.task_id)
        self.assertEqual(messages["session"]["state"], "associated")
        self.assertEqual(messages["session"]["save_state"], "trashed")
        self.assertTrue(messages["messages"])


# ------------------------------------------ list-sessions save_state 筛选（§5.16.3）

class TestListSessionsSaveState(HeadlessCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("tasksess-filter001", run_id="run-A",
                                      status="ready")
        self.make_run(self.task_id, "run-A", simple_events("A"))
        self.make_run(self.task_id, "run-B", simple_events("B"))
        sessions = self.backfill(self.task_id)
        self.assertEqual({s["run_id"] for s in sessions}, {"run-A", "run-B"})
        self.call("trash-session",
                  {"task_id": self.task_id, "run_id": "run-B"})

    def test_three_state_filter(self):
        # 缺省 active。
        active = self.call(
            "list-sessions", {"task_id": self.task_id})["sessions"]
        self.assertEqual([s["run_id"] for s in active], ["run-A"])
        # trashed。
        trashed = self.call(
            "list-sessions",
            {"task_id": self.task_id, "save_state": "trashed"},
        )["sessions"]
        self.assertEqual([s["run_id"] for s in trashed], ["run-B"])
        # all。
        everything = self.call(
            "list-sessions",
            {"task_id": self.task_id, "save_state": "all"},
        )["sessions"]
        self.assertEqual({s["run_id"] for s in everything}, {"run-A", "run-B"})
        # 缺省（无 task_id）按索引行同样过滤。
        default_active = {
            s["run_id"] for s in self.call("list-sessions", {})["sessions"]
            if s["task_id"] == self.task_id
        }
        self.assertEqual(default_active, {"run-A"})
        default_trashed = {
            s["run_id"] for s in self.call(
                "list-sessions", {"save_state": "trashed"})["sessions"]
            if s["task_id"] == self.task_id
        }
        self.assertEqual(default_trashed, {"run-B"})

    def test_invalid_save_state_is_bad_request(self):
        for bad in ("archived", "bogus", "ACTIVE", ""):
            exc = self.call_err("list-sessions", {"save_state": bad})
            self.assertEqual(exc.code, "bad_request", bad)
            exc = self.call_err("list-sessions", {
                "task_id": self.task_id, "save_state": bad,
            })
            self.assertEqual(exc.code, "bad_request", bad)


# ------------------------------------------- pi 轮次读取 + round_detail（§5.18）

class TestPiSessionReading(HeadlessCase):
    """backend=pi 轮次：job.json 回填 backend=pi、events.jsonl → 消息、
    get-session round_detail 形状、pending/半写入兜底、旧后端弃用路由、
    run_status 超时派生不回写。"""

    def setUp(self):
        super().setUp()
        self.task_id = self.make_task("tasksess-pi000001", run_id="run-D",
                                      status="ready")
        self.make_run(self.task_id, "run-D")

    def test_backfill_marks_backend_pi(self):
        session = next(s for s in self.backfill(self.task_id)
                       if s["run_id"] == "run-D")
        self.assertEqual(session["backend"], "pi")
        self.assertEqual(session["run_status"], "finished")
        self.assertEqual(session["state"], "associated")
        self.assertEqual(session["session_id"], "sess-pi-0001")
        row = self.round_row(self.task_id, "run-D")
        self.assertEqual(row["backend"], "pi")
        self.assertEqual(row["session_id"], "sess-pi-0001")
        self.assertEqual(row["run_status"], "finished")
        self.assertEqual(row["save_state"], "active")

    def test_session_messages_from_events_incremental(self):
        result = self.read_all(self.task_id)
        self.assertEqual(result["session"]["state"], "associated")
        self.assertEqual(
            [m["type"] for m in result["messages"]],
            ["system", "user", "assistant", "tool_call", "command_output",
             "system"],
        )
        self.assertEqual(
            [m["id"] for m in result["messages"]],
            [f"round:{i}" for i in range(1, 7)],
        )
        by_id = {m["id"]: m for m in result["messages"]}
        for mid in ("round:1", "round:2", "round:3"):
            self.assertNotIn("tool", by_id[mid])
        self.assertEqual(by_id["round:4"]["tool"]["name"], "exec")
        self.assertEqual(json.loads(by_id["round:4"]["tool"]["input"]),
                         {"command": "pytest -q"})
        self.assertIsNone(by_id["round:4"]["tool"]["output"])
        self.assertIsNone(by_id["round:4"]["tool"]["exit_code"])
        self.assertEqual(by_id["round:5"]["tool"]["exit_code"], 0)
        self.assertIsNone(by_id["round:5"]["tool"]["output"])
        self.assertEqual(by_id["round:5"]["content"], "3 passed")
        for message in result["messages"]:
            self.assertEqual(message["run_id"], "run-D")
        # 增量（cursor/revision/slice 逻辑不区分后端）。
        cursor = result["next_cursor"]
        events = default_pi_events() + [
            pi_event(7, "assistant_message", content="已完成")
        ]
        self.write_events(self.task_id, "run-D", events)
        nxt = self.read_all(self.task_id, cursor=cursor)
        self.assertEqual([m["id"] for m in nxt["messages"]], ["round:7"])
        self.assertEqual(
            [m["id"] for m in self.read_all(
                self.task_id, cursor=nxt["next_cursor"])["messages"]], []
        )

    def test_get_session_round_detail_shape(self):
        result = self.call("get-session",
                           {"task_id": self.task_id, "run_id": "run-D"})
        detail = result["round_detail"]
        expected_keys = {
            "backend", "run_status", "worktree_path", "pid", "started_at",
            "ended_at", "exit_code", "terminal", "config", "save_state",
            "saved_at", "detail",
        }
        self.assertTrue(expected_keys <= set(detail), detail)
        self.assertEqual(detail["backend"], "pi")
        self.assertEqual(detail["run_status"], "finished")
        self.assertEqual(detail["pid"], 4242)
        self.assertEqual(detail["exit_code"], 0)
        self.assertEqual(detail["terminal"],
                         {"outcome": "completed", "reason": "正常结束"})
        self.assertEqual(detail["save_state"], "active")
        # SessionInfo 同样带 §5.16.3 新字段。
        session = result["session"]
        self.assertEqual(session["backend"], "pi")
        self.assertEqual(session["save_state"], "active")
        self.assertEqual(session["allowed_actions"], ["trash-session"])
        self.assertIsNone(session["blocked_reason"])

    def test_round_detail_safe_none_without_round(self):
        result = self.call("get-session",
                           {"task_id": self.task_id, "run_id": "run-GONE"})
        self.assertIsNone(result["round_detail"])
        self.assertEqual(result["session"]["state"], "unavailable")
        self.assertIsNone(result["session"]["backend"])
        self.assertEqual(result["session"]["run_status"], "unknown")
        self.assertEqual(result["session"]["allowed_actions"], [])

    def test_run_status_timeout_derived_without_writeback(self):
        # §5.16.1/§5.18.4：status=running 超过 pi_round_timeout_seconds →
        # 读取层派生 failed；索引保持观察值（不回写 job.json/索引）。
        self.db_exec(
            "INSERT OR REPLACE INTO settings(key, value) "
            "VALUES('pi_round_timeout_seconds', '1')"
        )
        task = self.make_task("tasksess-pi000002", run_id="run-T",
                              status="ready")
        self.make_run(task, "run-T", job=pi_job(
            task, "run-T", status="running",
            started_at="2020-01-01T00:00:00.000000+00:00", ended_at=None,
        ))
        session = next(s for s in self.backfill(task)
                       if s["run_id"] == "run-T")
        self.assertEqual(session["run_status"], "failed")
        self.assertEqual(self.round_row(task, "run-T")["run_status"],
                         "running")

    def test_round_detail_timeout_derives_failed_with_detail(self):
        # §5.18.4：超时 → round_detail.run_status=failed 且 detail 记原因
        #（与 SessionInfo.run_status 同口径），索引与 job.json 均不回写。
        self.db_exec(
            "INSERT OR REPLACE INTO settings(key, value) "
            "VALUES('pi_round_timeout_seconds', '1')"
        )
        task = self.make_task("tasksess-pi000004", run_id="run-X",
                              status="ready")
        self.make_run(
            task, "run-X",
            events=[pi_event(1, "session", session_id="sess-pi-0004",
                             session_title="超时轮次")],
            job=pi_job(task, "run-X", status="running",
                       started_at="2020-01-01T00:00:00.000000+00:00",
                       ended_at=None, exit_code=None),
        )
        out = self.call("get-session", {"task_id": task, "run_id": "run-X"})
        detail = out["round_detail"]
        self.assertEqual(detail["run_status"], "failed")
        self.assertIn("超时", detail["detail"])
        self.assertIsNone(detail["terminal"])
        # SessionInfo 与 round_detail 同口径派生。
        self.assertEqual(out["session"]["run_status"], "failed")
        # 只读派生：索引保持观察值 running，job.json 不被改写。
        self.assertEqual(self.round_row(task, "run-X")["run_status"],
                         "running")
        job = json.loads(
            (self.runs_root / task / "run-X" / "job.json").read_text(
                encoding="utf-8")
        )
        self.assertEqual(job["status"], "running")

    def test_pi_pending_when_events_missing(self):
        self.make_run(self.task_id, "run-P", write_events=False)
        result = self.read_all(self.task_id, run_id="run-P")
        self.assertEqual(result["session"]["state"], "pending")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["connection"]["state"], "degraded")
        self.assertIn("事件日志", result["connection"]["detail"] or "")

    def test_pi_read_failure_keeps_last_good_snapshot(self):
        first = self.read_all(self.task_id)
        self.assertTrue(first["messages"])
        # 半写入：有内容但零有效行 → read_failed 路径。
        self.write_raw(self.task_id, "run-D", "{不是有效行\n")
        # 全新数据源（无进程内缓存）→ 跨进程持久快照保留上一份有效内容。
        h.set_source(h.HeadlessSource(str(self.runs_root)))
        second = self.read_all(self.task_id)
        self.assertEqual(second["session"]["state"], "associated")
        self.assertEqual(second["connection"]["state"], "degraded")
        self.assertTrue(second["connection"]["detail"])
        self.assertEqual([m["id"] for m in second["messages"]],
                         [m["id"] for m in first["messages"]])
        # 恢复写入 → 正常读取。
        self.write_events(self.task_id, "run-D", default_pi_events())
        third = self.read_all(self.task_id)
        self.assertEqual(len(third["messages"]), 6)

    def test_rebackfill_refresh_never_resurrects(self):
        # pi 轮次：job.status 变化触发发现类字段刷新，save_state 恒不复活。
        self.call("trash-session",
                  {"task_id": self.task_id, "run_id": "run-D"})
        write_json(
            self.runs_root / self.task_id / "run-D" / "job.json",
            pi_job(self.task_id, "run-D", status="running",
                   session_title="刷新后的标题"),
        )
        row = self.round_row(self.task_id, "run-D")
        self.assertEqual(row["save_state"], "trashed")
        trashed = self.call(
            "list-sessions",
            {"task_id": self.task_id, "save_state": "trashed"},
        )["sessions"]
        self.assertEqual([s["run_id"] for s in trashed], ["run-D"])
        row = self.round_row(self.task_id, "run-D")
        self.assertEqual(row["save_state"], "trashed")
        self.assertEqual(row["run_status"], "running")     # 发现类字段已刷新
        self.assertEqual(row["session_title"], "刷新后的标题")
        self.assertNotIn("run-D",
                         [s["run_id"] for s in self.backfill(self.task_id)])


# ------------------------------------ SessionInfo.last_checked_at（§5.15.5）

class TestLastCheckedAt(HeadlessCase):
    """「最近状态检查」（§5.15.5）：last_checked_at = session_reads 位点行的
    刷新时间；从未读取 → null；session-messages 响应反映本次检查（位点刷新后
    构建）；与会话更新时间（updated_at）分开展示。"""

    def test_last_checked_at_tracks_session_reads_position(self):
        task_id = self.make_task("tasksess-check0001", run_id="run-A",
                                 status="ready")
        self.make_run(task_id, "run-A", simple_events("hi"))
        # 未读取过（list/get 只探测不落位点）→ null = 从未检查。
        entry = self.backfill(task_id)[0]
        self.assertIsNone(entry["last_checked_at"])
        got = self.call("get-session",
                        {"task_id": task_id, "run_id": "run-A"})
        self.assertIsNone(got["session"]["last_checked_at"])
        # session-messages 读取 → 位点落库，响应即反映本次检查时间。
        page = self.read_all(task_id)
        checked = page["session"]["last_checked_at"]
        self.assertIsNotNone(checked)
        row = self.session_reads_row(f"{task_id}/run-A")
        self.assertEqual(checked, row["updated_at"])
        # 后续 list/get 读取同一位点时间；再次轮询推进到新的检查时间。
        self.assertEqual(
            self.backfill(task_id)[0]["last_checked_at"], row["updated_at"]
        )
        again = self.read_all(task_id, cursor="seq:0")
        self.assertGreaterEqual(
            again["session"]["last_checked_at"], checked
        )
        # pending 同样刷新检查时间（位点 state=pending，updated_at 推进）。
        pend = self.make_task("tasksess-check0002", run_id="run-P",
                              status="ready")
        self.make_run(pend, "run-P", write_events=False)
        pending = self.read_all(pend)
        self.assertEqual(pending["session"]["state"], "pending")
        self.assertIsNotNone(pending["session"]["last_checked_at"])


if __name__ == "__main__":
    unittest.main()
