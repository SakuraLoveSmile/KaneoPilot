"""观测复盘只读投影（protocol §5.32）。

`get-task` 经 `cmd_get_task` 附加 `retrospective`。本模块只读取 SQLite 行并派生汇总：
不写库、不写 runtime_operations、不调用引擎或远端、不读写工作树。时间统一向下取整到秒，
各泳道秒数合计等于 elapsed_seconds。
"""

from __future__ import annotations

import bisect
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import automation_policy, core, db, util

# §5.32.5 未汇总门控：§5.28 验收通过前为 False。关闭时等你处理、执行用时与
# 「等你处理较久」重点不生效；等待时段仍计入其所在泳道（执行或派发）。
WAIT_SOURCE_READY = False

LANES = ("plan", "dispatch", "execute", "waiting", "repair", "review", "other")
IDLE_SECONDS = 120
LONG_WAIT_SECONDS = 600
LONG_EXECUTE_SECONDS = 3600
SEGMENT_LIMIT = 200
HIGHLIGHT_LIMIT = 3
HIGHLIGHT_RANK = {"repair_exhausted": 0, "review_not_passed": 1, "long_wait": 2, "long_execute": 3}
HIGHLIGHT_LABEL = {
    "repair_exhausted": "修复轮已用完",
    "review_not_passed": "评审未通过",
    "long_wait": "等你处理较久",
    "long_execute": "单段执行过长",
}
TASK_KINDS = (
    "status_change", "enqueued", "dispatch_sent", "dispatch_failed",
    "request_cancelled", "review_registered", "requirements_changed",
)
GLOBAL_KINDS = ("paused", "unpaused")
DISPATCH_STATUSES = ("dispatching", "prepared", "submitting")


def build(conn: sqlite3.Connection, task: sqlite3.Row, as_of: datetime) -> Optional[dict]:
    """返回 `retrospective` 字典；无周期或已清除的任务返回 None。"""
    if db.task_is_purged(task):
        return None
    now = _floor(_utc(as_of))
    events = _events(conn, task["task_id"])
    start_event = _cycle_start_event(events)
    if start_event is None:
        return None
    start = start_event["at"]
    end, finished = _cycle_end(task, events, start, now)
    start_id = start_event["id"]
    in_cycle = [e for e in events if e["id"] >= start_id and e["at"] <= end]
    state = _initial_state(task, events, start_id, end)
    segments = _replay(events, start_id, start, end, state)
    extra = db.task_extra(task)
    union = _wait_union(extra, start, end, now) if WAIT_SOURCE_READY else []
    if union:
        segments = _overlay_waiting(segments, [(s, e) for s, e, _open in union])
    segments = _merge(segments)
    rounds = _round_starts(conn, task["task_id"])
    rows = _segment_rows(segments, rounds, finished, end)
    elapsed = _seconds(end - start)
    totals = _lane_totals(segments)
    distribution = _distribution(totals, elapsed)
    waiting = _waiting_summary(union, end) if WAIT_SOURCE_READY else None
    has_open_wait = any(open_ for _s, _e, open_ in union)
    return {
        "as_of": util.to_iso(now),
        "cycle_started_at": util.to_iso(start),
        "ended_at": util.to_iso(end) if finished else None,
        "state": "finished" if finished else ("waiting" if has_open_wait else "running"),
        "elapsed_seconds": elapsed,
        "exec_seconds": totals["execute"] if WAIT_SOURCE_READY else None,
        "waiting": waiting,
        "distribution": distribution,
        "segments": {"items": rows[-SEGMENT_LIMIT:], "truncated": len(rows) > SEGMENT_LIMIT},
        "highlights": _highlights(conn, task, extra, in_cycle, segments, union),
        **_verify_payload(task, extra),
    }


# ------------------------------------------------------------------ 读取


def _events(conn: sqlite3.Connection, task_id: str) -> list:
    """本任务事件与全局暂停事件，按 id 升序。

    两条查询均带 kind 过滤，心跳行不返回应用层；全局条件 `task_id IS NULL` 可用 events_task 索引。
    """
    task_marks = ",".join("?" * len(TASK_KINDS))
    rows = conn.execute(
        f"SELECT id, at, kind, request_id, detail_json FROM events "
        f"WHERE task_id=? AND kind IN ({task_marks}) ORDER BY id",
        (task_id, *TASK_KINDS),
    ).fetchall()
    global_marks = ",".join("?" * len(GLOBAL_KINDS))
    global_rows = conn.execute(
        f"SELECT id, at, kind, request_id, detail_json FROM events "
        f"WHERE task_id IS NULL AND kind IN ({global_marks}) ORDER BY id",
        GLOBAL_KINDS,
    ).fetchall()
    items = []
    for row in (*rows, *global_rows):
        at = _parse(row["at"])
        if at is None:
            continue
        items.append({
            "id": row["id"],
            "at": _floor(at),
            "kind": row["kind"],
            "request_id": row["request_id"],
            "detail": _detail(row["detail_json"]),
        })
    items.sort(key=lambda item: item["id"])
    return items


def _round_starts(conn: sqlite3.Connection, task_id: str) -> list:
    """执行轮次起点（1 起序号），按 started_at 升序。"""
    rows = conn.execute(
        "SELECT started_at FROM execution_rounds WHERE task_id=? AND started_at IS NOT NULL "
        "ORDER BY started_at, run_id",
        (task_id,),
    ).fetchall()
    starts = []
    for row in rows:
        at = _parse(row["started_at"])
        if at is not None:
            starts.append(_floor(at))
    starts.sort()
    return starts


# ------------------------------------------------------------------ 周期


def _cycle_start_event(events: list) -> Optional[dict]:
    """最近一次 requirements_changed 之后（无则自创建起算）的首个 enqueued（§5.32.2）。"""
    anchor = max((e["id"] for e in events if e["kind"] == "requirements_changed"), default=0)
    for event in events:
        if event["kind"] == "enqueued" and event["id"] > anchor:
            return event
    return None


def _cycle_end(task: sqlite3.Row, events: list, start: datetime, now: datetime) -> tuple:
    if task["status"] != "done":
        return max(now, start), False
    done = [
        e for e in events
        if e["kind"] == "status_change" and e["at"] >= start and e["at"] <= now
        and e["detail"].get("to") == "done"
    ]
    if done:
        return max(done[-1]["at"], start), True
    # 迁移导入等无登记的 done：取 updated_at，限制在 [周期起点, as_of] 内（§5.32.2，近似）。
    updated = _parse(task["updated_at"])
    fallback = _floor(updated) if updated is not None else now
    return min(max(fallback, start), now), True


# ------------------------------------------------------------------ 重放


def _initial_state(task: sqlite3.Row, events: list, start_id: int, end: datetime) -> dict:
    status = task["status"]
    for event in events:
        if event["id"] >= start_id and event["kind"] == "status_change" and event["at"] <= end:
            status = event["detail"].get("from") or task["status"]
            break
    return {
        "status": status, "queued": False, "request_id": None,
        "review_pending": False, "repair": False,
        "paused_global": False, "paused_dispatch": False,
    }


def _replay(events: list, start_id: int, start: datetime, end: datetime, state: dict) -> list:
    """按事件推进状态，产出 [(泳道, 起点, 终点)]，覆盖 [start, end) 且首尾相接。"""
    segments = []
    cursor = start
    for event in events:
        if event["id"] < start_id:
            # 周期前的暂停窗口仍有效；派生标志不在周期前累积。
            if event["kind"] in GLOBAL_KINDS:
                _apply_pause(state, event)
            continue
        at = min(max(event["at"], start), end)
        if at > cursor:
            segments.append((_lane(state), cursor, at))
            cursor = at
        _apply(state, event)
    if end > cursor:
        segments.append((_lane(state), cursor, end))
    return segments


def _apply(state: dict, event: dict) -> None:
    kind, detail = event["kind"], event["detail"]
    if kind == "status_change":
        state["status"] = detail.get("to") or state["status"]
        if state["status"] == "in_review":
            state["review_pending"] = True
        if state["status"] == "running":
            state["repair"] = False
    elif kind == "enqueued":
        if detail.get("kind") == "dispatch":
            state["queued"] = True
            state["request_id"] = event["request_id"]
    elif kind in ("dispatch_sent", "dispatch_failed"):
        state["queued"] = False
    elif kind == "request_cancelled":
        if event["request_id"] is not None and event["request_id"] == state["request_id"]:
            state["queued"] = False
    elif kind == "review_registered":
        state["review_pending"] = False
        if detail.get("conclusion") == "changes_requested":
            state["repair"] = True
    elif kind in GLOBAL_KINDS:
        _apply_pause(state, event)


def _apply_pause(state: dict, event: dict) -> None:
    stage = event["detail"].get("stage")
    if stage in (None, ""):
        key = "paused_global"
    elif stage == "dispatch":
        key = "paused_dispatch"
    else:
        return
    state[key] = event["kind"] == "paused"


def _lane(state: dict) -> str:
    """§5.32.3 第 2–4 步：修复优先，其次按状态；暂停窗口内的派发改归其他。"""
    if state["repair"]:
        return "repair"
    status = state["status"]
    if status == "planning":
        lane = "plan"
    elif status in DISPATCH_STATUSES or (status == "ready" and state["queued"]):
        lane = "dispatch"
    elif status == "running":
        lane = "execute"
    elif status == "in_review" and state["review_pending"]:
        lane = "review"
    else:
        lane = "other"
    if lane == "dispatch" and (state["paused_global"] or state["paused_dispatch"]):
        return "other"
    return lane


def _wait_union(extra: dict, start: datetime, end: datetime, now: datetime) -> list:
    """执行问题等待区间裁剪到周期后取并集；返回 [(起点, 终点, 是否开放)]。"""
    raw = extra.get("execution_input_wait_intervals") or []
    spans = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        begin = _parse(item.get("start"))
        open_ = item.get("end") in (None, "")
        finish = now if open_ else _parse(item.get("end"))
        if begin is None or finish is None:
            continue
        lo, hi = max(_floor(begin), start), min(_floor(finish), end)
        if hi > lo:
            spans.append((lo, hi, open_))
    spans.sort(key=lambda span: (span[0], span[1]))
    merged = []
    for lo, hi, open_ in spans:
        if merged and lo <= merged[-1][1]:
            prev_lo, prev_hi, prev_open = merged[-1]
            merged[-1] = (prev_lo, max(prev_hi, hi), prev_open or open_)
        else:
            merged.append((lo, hi, open_))
    return merged


def _overlay_waiting(segments: list, union: list) -> list:
    """等待区间优先于其余判定（§5.32.3 第 1 步）。"""
    out = []
    for lane, a, b in segments:
        cursor = a
        for lo, hi in union:
            if hi <= cursor or lo >= b:
                continue
            left, right = max(lo, a), min(hi, b)
            if left > cursor:
                out.append((lane, cursor, left))
            out.append(("waiting", left, right))
            cursor = right
        if cursor < b:
            out.append((lane, cursor, b))
    return out


def _merge(segments: list) -> list:
    merged = []
    for lane, a, b in segments:
        if b <= a:
            continue
        if merged and merged[-1][0] == lane and merged[-1][2] == a:
            merged[-1] = (lane, merged[-1][1], b)
        else:
            merged.append((lane, a, b))
    return merged


# ------------------------------------------------------------------ 输出


def _segment_rows(segments: list, rounds: list, finished: bool, end: datetime) -> list:
    rows = []
    last = len(segments) - 1
    for index, (lane, a, b) in enumerate(segments):
        seconds = _seconds(b - a)
        is_open = not finished and index == last
        rows.append({
            "lane": lane,
            "round": _round_of(rounds, a),
            "started_at": util.to_iso(a),
            "ended_at": None if is_open else util.to_iso(b),
            "seconds": seconds,
            "idle": lane in ("dispatch", "other") and seconds >= IDLE_SECONDS,
        })
    return rows


def _round_of(rounds: list, at: datetime) -> Optional[int]:
    position = bisect.bisect_right(rounds, at)
    return position if position else None


def _lane_totals(segments: list) -> dict:
    totals = {lane: 0 for lane in LANES}
    for lane, a, b in segments:
        totals[lane] += _seconds(b - a)
    return totals


def _distribution(totals: dict, elapsed: int) -> list:
    """最大余数法取整；门控关闭时等你处理项为 null，其余六项合计等于 elapsed。"""
    measured = [lane for lane in LANES if WAIT_SOURCE_READY or lane != "waiting"]
    percent = _percent({lane: totals[lane] for lane in measured}, elapsed)
    rows = []
    for lane in LANES:
        if lane in measured:
            rows.append({"lane": lane, "seconds": totals[lane], "percent": percent[lane]})
        else:
            rows.append({"lane": lane, "seconds": None, "percent": None})
    return rows


def _percent(values: dict, total: int) -> dict:
    if total <= 0:
        return {lane: 0 for lane in values}
    raw = {lane: value * 100 / total for lane, value in values.items()}
    floors = {lane: int(share) for lane, share in raw.items()}
    remainder = 100 - sum(floors.values())
    order = sorted(values, key=lambda lane: -(raw[lane] - floors[lane]))
    for lane in order[:remainder]:
        floors[lane] += 1
    return floors


def _waiting_summary(union: list, end: datetime) -> dict:
    lengths = [_seconds(hi - lo) for lo, hi, _open in union]
    opened = [lo for lo, _hi, open_ in union if open_]
    return {
        "count": len(union),
        "total_seconds": sum(lengths),
        "longest_seconds": max(lengths, default=0),
        "open_since": util.to_iso(min(opened)) if opened else None,
    }


def _highlights(conn, task, extra, in_cycle, segments, union) -> list:
    candidates = []
    automation = core.automation_of(task)
    policy = automation_policy.policy_for(conn, task["project_id"]) or {}
    fix_round, max_rounds = automation.get("fix_round"), policy.get("max_fix_rounds")
    if (type(fix_round) is int and type(max_rounds) is int and fix_round >= max_rounds):
        candidates.append(("repair_exhausted", None, "repair", None))
    reviews = [e for e in in_cycle if e["kind"] == "review_registered"]
    if reviews and reviews[-1]["detail"].get("conclusion") != "pass":
        candidates.append(("review_not_passed", reviews[-1]["at"], "review", None))
    if WAIT_SOURCE_READY:
        for lo, hi, _open in union:
            length = _seconds(hi - lo)
            if length >= LONG_WAIT_SECONDS:
                candidates.append(("long_wait", lo, "waiting", length))
    for lane, a, b in segments:
        seconds = _seconds(b - a)
        if lane == "execute" and seconds >= LONG_EXECUTE_SECONDS:
            candidates.append(("long_execute", a, "execute", seconds))
    candidates.sort(key=lambda item: (HIGHLIGHT_RANK[item[0]], -(item[1].timestamp() if item[1] else 0)))
    return [
        {
            "kind": kind,
            "label": HIGHLIGHT_LABEL[kind],
            "lane": lane,
            "at": util.to_iso(at) if at is not None else None,
            "seconds": seconds,
        }
        for kind, at, lane, seconds in candidates[:HIGHLIGHT_LIMIT]
    ]


def _verify_payload(task: sqlite3.Row, extra: dict) -> dict:
    declaration = extra.get("plan_verify")
    commands = declaration.get("commands") if isinstance(declaration, dict) else None
    if not isinstance(declaration, dict) or "error" in declaration or not isinstance(commands, list):
        return {"verify": None, "verify_runs": None}
    receipt = extra.get("engine_verdict") if isinstance(extra.get("engine_verdict"), dict) else {}
    verify, runs = _verify_counts(task, commands, receipt)
    return {"verify": verify, "verify_runs": runs}


def _verify_counts(task: sqlite3.Row, commands: list, receipt: dict) -> tuple:
    """§5.32.6：仅当前回执计数；旧回执一律记为未核验。"""
    total = len(commands)
    source = receipt.get("evidence_source")
    if not _receipt_current(task, commands, receipt):
        return ({"total": total, "passed": 0, "failed": 0, "unconfirmed": total,
                 "current": False, "evidence_source": source}, None)
    checks = [item for item in receipt.get("checks") or [] if isinstance(item, dict)]
    if source == "goal_report":
        coverage = receipt.get("goal_check_coverage") if isinstance(receipt.get("goal_check_coverage"), dict) else {}
        matched = set(coverage.get("matched") or [])
        passed = len(coverage.get("matched") or [])
        failed = sum(
            1 for command in commands
            if command not in matched and any(
                item.get("command") == command and _nonzero_int(item.get("exit_code")) for item in checks)
        )
    else:
        native = [item for item in checks if item.get("provenance") == "harness_observed"]
        passed_set = {
            command for command in commands
            if any(item.get("command") == command and _exit_code(item) == 0 for item in native)
        }
        passed = len(passed_set)
        failed = sum(
            1 for command in commands
            if command not in passed_set and any(
                item.get("command") == command and _nonzero_int(item.get("exit_code")) for item in native)
        )
    runs = {
        "runs": len(checks),
        "failed": sum(1 for item in checks if _nonzero_int(item.get("exit_code"))),
        "timed_out": sum(1 for item in checks if item.get("timed_out") is True),
        "command_not_found": sum(1 for item in checks if item.get("command_not_found") is True),
    }
    return ({"total": total, "passed": passed, "failed": failed,
             "unconfirmed": total - passed - failed, "current": True,
             "evidence_source": source}, runs)


def _receipt_current(task: sqlite3.Row, commands: list, receipt: dict) -> bool:
    if receipt.get("evidence_source") not in ("goal_report", "codex_report"):
        return False
    binding = receipt.get("binding") if isinstance(receipt.get("binding"), dict) else {}
    return (
        binding.get("plan_sha256") == task["plan_sha256"]
        and binding.get("commands_sha256") == util.sha256_text(util.jdump(commands))
        and binding.get("execution_run_id") == task["run_id"]
    )


# ------------------------------------------------------------------ 工具


def _exit_code(item: dict) -> Optional[int]:
    value = item.get("exit_code")
    return value if type(value) is int else None


def _nonzero_int(value: Any) -> bool:
    return type(value) is int and value != 0


def _detail(text: Any) -> dict:
    value = util.jload(text)
    return value if isinstance(value, dict) else {}


def _parse(value: Any) -> Optional[datetime]:
    return util.parse_ts(value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _seconds(delta: timedelta) -> int:
    return int(delta.total_seconds())
