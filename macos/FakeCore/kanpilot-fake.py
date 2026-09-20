#!/usr/bin/env python3
"""KaneoPilot 假核心（开发期数据源）。

CLI 形态与真核心一致：

    python3 kanpilot-fake.py <command> [--json '<payload>']

成功输出单个 JSON 对象 exit 0；失败输出 {"error","code"} exit 1。
覆盖 protocol.md 全部 display_status / allowed_actions 分支。

可变性：动作（set-paused / set-priority / enqueue / resolve）写入
$KANPILOT_FAKE_STATE（默认 $TMPDIR/kanpilot-fake-state.json），
删除该文件即复位罐头数据。
"""
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

STATE_PATH = os.environ.get(
    "KANPILOT_FAKE_STATE",
    os.path.join(tempfile.gettempdir(), "kanpilot-fake-state.json"),
)

PROJECTS = {
    "Music": "e8magole7hbivl8103wevkss",
    "Library": "nosjbbex7hpyaiweh6cdidwr",
    "StudyTool": "jb22vodykf5ct64ng1acje5o",
    "Comic": "ms42fpv4oqdompsw3l0a7j4k",
    "Feedback": "ryryo1reph1xfemqtwkjyiiz",
}
PLAN_ROOT = "~/Documents/Code/Plan"
REPOS_ROOT = "~/Documents/Code/Project/Personal Project"
APP_SUPPORT = "~/Library/Application Support/KaneoPilot"


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def past(**kw):
    return iso(utcnow() - timedelta(**kw))


def future(**kw):
    return iso(utcnow() + timedelta(**kw))


def sha(seed, n=64):
    return (uuid.uuid5(uuid.NAMESPACE_URL, "fake:" + seed).hex * 4)[:n]


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def mutate(state, fn):
    result = fn(state)
    save_state(state)
    return result


def append_event(state, kind, task_id=None, request_id=None, actor="user", detail=None):
    events = state.setdefault("extra_events", [])
    events.append({
        "id": 9000 + len(events),
        "at": iso(utcnow()),
        "task_id": task_id,
        "request_id": request_id,
        "kind": kind,
        "actor": actor,
        "detail": detail or {},
    })


# ---------------------------------------------------------------- canned data

def base_tasks():
    """TaskDetail 形态全集；list-tasks 时投影为 TaskSummary。"""
    return [
        {   # 待规划 → enqueue(plan) + recheck
            "task_id": "task-music-001", "project_id": PROJECTS["Music"],
            "project_name": "Music", "title": "整理曲库元数据并补充缺失封面",
            "stage": "plan", "status": "needs_plan", "display_status": "待规划",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=42),
            "requirements_sha256": sha("req-music-001"),
            "source_sha256": sha("src-music-001"),
            "source_path": "~/Library/Application Support/Codex/KaneoPlanner/state/sources/task-music-001.json",
            "priority": 0, "created_at": past(days=1), "updated_at": past(hours=2),
        },
        {   # 规划中：plan 请求已被领取
            "task_id": "task-lib-002", "project_id": PROJECTS["Library"],
            "project_name": "Library", "title": "重构书库索引以支持多标签检索",
            "stage": "plan", "status": "planning", "display_status": "规划中",
            "kaneo_status": "to-do", "kaneo_checked_at": past(hours=1),
            "requirements_sha256": sha("req-lib-002"),
            "priority": 0, "created_at": past(days=2), "updated_at": past(minutes=25),
            "active_request": {
                "request_id": "req-plan-002", "kind": "plan", "status": "claimed",
                "claimed_by": "kaneo-plan", "heartbeat_at": past(minutes=2),
                "lease_expires_at": future(minutes=18),
            },
        },
        {   # 就绪：计划已发布，无活动请求
            "task_id": "task-study-003", "project_id": PROJECTS["StudyTool"],
            "project_name": "StudyTool", "title": "为复习卡片加入间隔重复调度",
            "stage": "dispatch", "status": "ready", "display_status": "就绪",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=30),
            "requirements_sha256": sha("req-study-003"),
            "plan_path": "~/Documents/Code/Plan/StudyTool/task-study-003_20250918.md",
            "plan_sha256": sha("plan-study-003"), "plan_kind": "standard",
            "plan_generated_at": past(hours=5), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "source_sha256": sha("src-study-003"),
            "source_path": "~/Library/Application Support/Codex/KaneoPlanner/state/sources/task-study-003.json",
            "priority": 0, "created_at": past(days=3), "updated_at": past(hours=5),
        },
        {   # 已排队：ready + queued dispatch 请求
            "task_id": "task-comic-004", "project_id": PROJECTS["Comic"],
            "project_name": "Comic", "title": "抓取章节列表并生成离线缓存清单",
            "stage": "dispatch", "status": "ready", "display_status": "已排队",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=18),
            "requirements_sha256": sha("req-comic-004"),
            "plan_path": "~/Documents/Code/Plan/Comic/task-comic-004_20250919.md",
            "plan_sha256": sha("plan-comic-004"), "plan_kind": "standard",
            "plan_generated_at": past(hours=20), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "source_sha256": sha("src-comic-004"),
            "source_path": "~/Library/Application Support/Codex/KaneoPlanner/state/sources/task-comic-004.json",
            "next_claim_at": future(minutes=7),
            "priority": 0, "created_at": past(days=2), "updated_at": past(minutes=40),
            "active_request": {
                "request_id": "req-dispatch-004", "kind": "dispatch",
                "status": "queued", "claimed_by": None,
                "heartbeat_at": None, "lease_expires_at": None,
            },
        },
        {   # 派发中：dispatch 请求已领取（租约新鲜）
            "task_id": "task-fb-005", "project_id": PROJECTS["Feedback"],
            "project_name": "Feedback", "title": "收集崩溃日志并归类用户反馈",
            "stage": "dispatch", "status": "dispatching", "display_status": "派发中",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=8),
            "requirements_sha256": sha("req-fb-005"),
            "plan_path": "~/Documents/Code/Plan/Feedback/task-fb-005_20250919.md",
            "plan_sha256": sha("plan-fb-005"), "plan_kind": "standard",
            "plan_generated_at": past(days=1), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-fb-005-a1",
            "session_marker": "task-fb-005/run-fb-005-a1",
            "send_state": "not_sent", "sent": 0,
            "original_todo_slug": "to-do",
            "priority": 0, "created_at": past(days=4), "updated_at": past(minutes=6),
            "active_request": {
                "request_id": "req-dispatch-005", "kind": "dispatch",
                "status": "claimed", "claimed_by": "kaneo-dispatch",
                "heartbeat_at": past(minutes=1),
                "lease_expires_at": future(minutes=15),
            },
        },
        {   # 执行中：running + 新鲜 monitor
            "task_id": "task-music-006", "project_id": PROJECTS["Music"],
            "project_name": "Music", "title": "把本地歌单同步到 Kaneo 播放列表",
            "stage": "monitor", "status": "running", "display_status": "执行中",
            "kaneo_status": "in-progress", "kaneo_checked_at": past(minutes=12),
            "requirements_sha256": sha("req-music-006"),
            "plan_path": "~/Documents/Code/Plan/Music/task-music-006_20250917.md",
            "plan_sha256": sha("plan-music-006"), "plan_kind": "standard",
            "plan_generated_at": past(days=3), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-music-006-b2", "session_id": "devin-sess-6f3a",
            "session_title": "Devin: sync playlists",
            "session_marker": "task-music-006/run-music-006-b2",
            "send_state": "sent", "sent": 1, "sent_at": past(hours=3),
            "last_progress": "已完成 12/18 个播放列表的比对",
            "priority": 0, "created_at": past(days=6), "updated_at": past(minutes=12),
            "active_request": {
                "request_id": "req-monitor-006", "kind": "monitor",
                "status": "claimed", "claimed_by": "kaneo-monitor",
                "heartbeat_at": past(minutes=2),
                "lease_expires_at": future(minutes=10),
            },
        },
        {   # 执行中但 monitor 排队等下一周期
            "task_id": "task-comic-007", "project_id": PROJECTS["Comic"],
            "project_name": "Comic", "title": "修复章节图片懒加载失败的问题",
            "stage": "monitor", "status": "running", "display_status": "执行中",
            "kaneo_status": "in-progress", "kaneo_checked_at": past(minutes=45),
            "requirements_sha256": sha("req-comic-007"),
            "plan_path": "~/Documents/Code/Plan/Comic/task-comic-007_20250915.md",
            "plan_sha256": sha("plan-comic-007"), "plan_kind": "standard",
            "plan_generated_at": past(days=5), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-comic-007-c3", "session_id": "devin-sess-91cd",
            "session_title": "Devin: lazy-load fix",
            "session_marker": "task-comic-007/run-comic-007-c3",
            "send_state": "sent", "sent": 1, "sent_at": past(days=1),
            "last_progress": "回归测试中，前 3 章图片已正常",
            "priority": 0, "created_at": past(days=7), "updated_at": past(minutes=45),
            "active_request": {
                "request_id": "req-monitor-007", "kind": "monitor",
                "status": "queued", "claimed_by": None,
                "heartbeat_at": None, "lease_expires_at": None,
            },
        },
        {   # 评审中
            "task_id": "task-lib-008", "project_id": PROJECTS["Library"],
            "project_name": "Library", "title": "导入 Zotero 书目并去重",
            "stage": "review", "status": "in_review", "display_status": "评审中",
            "kaneo_status": "in-review", "kaneo_checked_at": past(minutes=22),
            "requirements_sha256": sha("req-lib-008"),
            "plan_path": "~/Documents/Code/Plan/Library/task-lib-008_20250914.md",
            "plan_sha256": sha("plan-lib-008"), "plan_kind": "standard",
            "plan_generated_at": past(days=6), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-lib-008-d4", "session_id": "devin-sess-2e7b",
            "session_title": "Devin: zotero import",
            "session_marker": "task-lib-008/run-lib-008-d4",
            "send_state": "sent", "sent": 1, "sent_at": past(days=2),
            "last_progress": "执行端报告完成，等待评审",
            "priority": 0, "created_at": past(days=8), "updated_at": past(minutes=22),
            "active_request": {
                "request_id": "req-review-008", "kind": "review",
                "status": "queued", "claimed_by": None,
                "heartbeat_at": None, "lease_expires_at": None,
            },
        },
        {   # 需人工（send_state=not_sent）：retry/replan/dismiss/mark_done/recheck
            "task_id": "task-study-009", "project_id": PROJECTS["StudyTool"],
            "project_name": "StudyTool", "title": "把错题本导出为 Anki 牌组",
            "stage": "dispatch", "status": "needs_user", "display_status": "需人工",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=50),
            "requirements_sha256": sha("req-study-009"),
            "plan_path": "~/Documents/Code/Plan/StudyTool/task-study-009_20250919.md",
            "plan_sha256": sha("plan-study-009"), "plan_kind": "standard",
            "plan_generated_at": past(hours=10), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-study-009-e5",
            "send_state": "not_sent", "sent": 0,
            "failure_code": "validation_failed",
            "failure_reason": "远端任务列缺少 in-progress 状态，派发校验未通过",
            "failure_stage": "dispatch",
            "blocked_at": past(minutes=50),
            "blocked_reason": "远端任务列缺少 in-progress 状态",
            "blocked_summary": "dispatch claim 校验第 4 步失败：远端列集合不含 in-progress",
            "priority": 0, "created_at": past(days=3), "updated_at": past(minutes=50),
        },
        {   # 需人工（send_state=unknown）：restore/accept_duplicate/mark_sent + 证据块
            "task_id": "task-comic-010", "project_id": PROJECTS["Comic"],
            "project_name": "Comic", "title": "合并重复条目并保留最早评论",
            "stage": "dispatch", "status": "needs_user", "display_status": "需人工",
            "kaneo_status": "in-progress", "kaneo_checked_at": past(minutes=70),
            "requirements_sha256": sha("req-comic-010"),
            "plan_path": "~/Documents/Code/Plan/Comic/task-comic-010_20250918.md",
            "plan_sha256": sha("plan-comic-010"), "plan_kind": "standard",
            "plan_generated_at": past(days=2), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-comic-010-f6",
            "send_state": "unknown", "sent": None, "lock_retained": 1,
            "manual_recovery_required": 1,
            "failure_code": "evidence_invalid",
            "failure_reason": "点击发送后窗口失焦，发送证据缺失，无法确认是否已发出",
            "failure_stage": "dispatch",
            "blocked_at": past(minutes=70),
            "blocked_reason": "发送状态不明，需要人工裁决",
            "blocked_summary": "dispatch-confirm 超时：message_visible 未确认",
            "priority": 0, "created_at": past(days=4), "updated_at": past(minutes=70),
        },
        {   # 执行端失联：monitor claimed 租约过期
            "task_id": "task-fb-011", "project_id": PROJECTS["Feedback"],
            "project_name": "Feedback", "title": "按版本聚合崩溃报告",
            "stage": "monitor", "status": "running", "display_status": "执行端失联",
            "kaneo_status": "in-progress", "kaneo_checked_at": past(hours=1),
            "requirements_sha256": sha("req-fb-011"),
            "plan_path": "~/Documents/Code/Plan/Feedback/task-fb-011_20250912.md",
            "plan_sha256": sha("plan-fb-011"), "plan_kind": "standard",
            "plan_generated_at": past(days=8), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-fb-011-g7", "session_id": "devin-sess-41aa",
            "session_title": "Devin: crash aggregation",
            "session_marker": "task-fb-011/run-fb-011-g7",
            "send_state": "sent", "sent": 1, "sent_at": past(days=2),
            "last_progress": "聚类完成 80%，随后失联",
            "priority": 0, "created_at": past(days=9), "updated_at": past(hours=1),
            "active_request": {
                "request_id": "req-monitor-011", "kind": "monitor",
                "status": "claimed", "claimed_by": "kaneo-monitor",
                "heartbeat_at": past(minutes=35),
                "lease_expires_at": past(minutes=15),
            },
        },
        {   # 执行端失联：dispatch claimed 租约过期（发送不明保留占用）
            "task_id": "task-music-012", "project_id": PROJECTS["Music"],
            "project_name": "Music", "title": "清理重复音轨并统一标签",
            "stage": "dispatch", "status": "submitting", "display_status": "执行端失联",
            "kaneo_status": "in-progress", "kaneo_checked_at": past(hours=2),
            "requirements_sha256": sha("req-music-012"),
            "plan_path": "~/Documents/Code/Plan/Music/task-music-012_20250919.md",
            "plan_sha256": sha("plan-music-012"), "plan_kind": "standard",
            "plan_generated_at": past(hours=26), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-music-012-h8",
            "session_marker": "task-music-012/run-music-012-h8",
            "send_state": "unknown", "sent": None, "lock_retained": 1,
            "transition_started_at": past(minutes=90),
            "priority": 0, "created_at": past(days=2), "updated_at": past(minutes=90),
            "active_request": {
                "request_id": "req-dispatch-012", "kind": "dispatch",
                "status": "claimed", "claimed_by": "kaneo-dispatch",
                "heartbeat_at": past(minutes=80),
                "lease_expires_at": past(minutes=60),
            },
        },
        {   # 已归档
            "task_id": "task-study-013", "project_id": PROJECTS["StudyTool"],
            "project_name": "StudyTool", "title": "为每日复习生成提醒通知",
            "stage": "done", "status": "done", "display_status": "已归档",
            "kaneo_status": "done", "kaneo_checked_at": past(days=1),
            "requirements_sha256": sha("req-study-013"),
            "plan_path": "~/Documents/Code/Plan/StudyTool/task-study-013_20250910.md",
            "plan_sha256": sha("plan-study-013"), "plan_kind": "standard",
            "plan_generated_at": past(days=10), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "review_report_path": "~/Library/Application Support/Codex/KaneoReviewer/reports/task-study-013/20250915T030000.md",
            "review_report_sha256": sha("report-study-013"),
            "review_conclusion": "pass",
            "review_key": "rev-study-013",
            "send_state": "sent", "sent": 1, "sent_at": past(days=9),
            "priority": 0, "created_at": past(days=12), "updated_at": past(days=1),
        },
        {   # 长中文标题（验收 §8：40+ 字截断不撑破布局）
            "task_id": "task-fb-014", "project_id": PROJECTS["Feedback"],
            "project_name": "Feedback",
            "title": "这是一个特别特别长的任务标题用来验证列表在四十个汉字以上的截断效果而不会撑破布局",
            "stage": "dispatch", "status": "ready", "display_status": "就绪",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=15),
            "requirements_sha256": sha("req-fb-014"),
            "plan_path": "~/Documents/Code/Plan/Feedback/task-fb-014_20250919.md",
            "plan_sha256": sha("plan-fb-014"), "plan_kind": "standard",
            "plan_generated_at": past(hours=3), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "source_sha256": sha("src-fb-014"),
            "source_path": "~/Library/Application Support/Codex/KaneoPlanner/state/sources/task-fb-014.json",
            "priority": 0, "created_at": past(days=1), "updated_at": past(hours=3),
        },
    ]


def base_requests():
    return [
        {"request_id": "req-plan-002", "idempotency_key": "plan:task-lib-002",
         "task_id": "task-lib-002", "stage": "plan", "kind": "plan",
         "status": "claimed", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-lib-002-p1", "claimed_by": "kaneo-plan",
         "claimed_at": past(minutes=25), "heartbeat_at": past(minutes=2),
         "lease_expires_at": future(minutes=18), "attempt_no": 1,
         "created_at": past(hours=2), "updated_at": past(minutes=2)},
        {"request_id": "req-dispatch-004", "idempotency_key": "dispatch:task-comic-004",
         "task_id": "task-comic-004", "stage": "dispatch", "kind": "dispatch",
         "status": "queued", "priority": 100, "requested_by": "user",
         "attempt_no": 0, "created_at": past(minutes=40), "updated_at": past(minutes=40)},
        {"request_id": "req-dispatch-005", "idempotency_key": "dispatch:task-fb-005",
         "task_id": "task-fb-005", "stage": "dispatch", "kind": "dispatch",
         "status": "claimed", "priority": 90, "requested_by": "user",
         "run_id": "run-fb-005-a1", "claimed_by": "kaneo-dispatch",
         "claimed_at": past(minutes=6), "heartbeat_at": past(minutes=1),
         "lease_expires_at": future(minutes=15), "attempt_no": 1,
         "created_at": past(minutes=30), "updated_at": past(minutes=1)},
        {"request_id": "req-monitor-006", "idempotency_key": "monitor:task-music-006",
         "task_id": "task-music-006", "stage": "monitor", "kind": "monitor",
         "status": "claimed", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-music-006-m1", "claimed_by": "kaneo-monitor",
         "claimed_at": past(minutes=12), "heartbeat_at": past(minutes=2),
         "lease_expires_at": future(minutes=10), "attempt_no": 3,
         "created_at": past(minutes=12), "updated_at": past(minutes=2)},
        {"request_id": "req-monitor-007", "idempotency_key": "monitor:task-comic-007",
         "task_id": "task-comic-007", "stage": "monitor", "kind": "monitor",
         "status": "queued", "priority": 100, "requested_by": "scheduler",
         "not_before": past(minutes=40), "attempt_no": 0,
         "created_at": past(minutes=45), "updated_at": past(minutes=45)},
        {"request_id": "req-review-008", "idempotency_key": "review:task-lib-008",
         "task_id": "task-lib-008", "stage": "review", "kind": "review",
         "status": "queued", "priority": 100, "requested_by": "scheduler",
         "attempt_no": 0, "created_at": past(minutes=22), "updated_at": past(minutes=22)},
        {"request_id": "req-plan-scan-900", "idempotency_key": "plan-scan:workspace",
         "task_id": None, "stage": "plan", "kind": "plan-scan",
         "status": "queued", "priority": 200, "requested_by": "scheduler",
         "attempt_no": 0, "created_at": past(minutes=55), "updated_at": past(minutes=55)},
        {"request_id": "req-monitor-011", "idempotency_key": "monitor:task-fb-011",
         "task_id": "task-fb-011", "stage": "monitor", "kind": "monitor",
         "status": "claimed", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-fb-011-m9", "claimed_by": "kaneo-monitor",
         "claimed_at": past(hours=1), "heartbeat_at": past(minutes=35),
         "lease_expires_at": past(minutes=15), "attempt_no": 4,
         "created_at": past(hours=1), "updated_at": past(minutes=35)},
        {"request_id": "req-dispatch-012", "idempotency_key": "dispatch:task-music-012",
         "task_id": "task-music-012", "stage": "dispatch", "kind": "dispatch",
         "status": "claimed", "priority": 95, "requested_by": "user",
         "run_id": "run-music-012-h8", "claimed_by": "kaneo-dispatch",
         "claimed_at": past(minutes=90), "heartbeat_at": past(minutes=80),
         "lease_expires_at": past(minutes=60), "attempt_no": 1,
         "created_at": past(minutes=95), "updated_at": past(minutes=80)},
        {"request_id": "req-dispatch-009", "idempotency_key": "dispatch:task-study-009",
         "task_id": "task-study-009", "stage": "dispatch", "kind": "dispatch",
         "status": "failed", "priority": 100, "requested_by": "user",
         "run_id": "run-study-009-e5", "claimed_by": "kaneo-dispatch",
         "claimed_at": past(minutes=55),
         "result": {"outcome": "failed", "reason": "远端任务列缺少 in-progress 状态"},
         "finished_at": past(minutes=50), "attempt_no": 1,
         "created_at": past(minutes=60), "updated_at": past(minutes=50)},
        {"request_id": "req-monitor-099", "idempotency_key": "monitor:task-comic-007-old",
         "task_id": "task-comic-007", "stage": "monitor", "kind": "monitor",
         "status": "failed", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-comic-007-x0", "claimed_by": "kaneo-monitor",
         "claimed_at": past(hours=5),
         "result": {"outcome": "failed", "reason": "锁屏导致 UI 观察失败"},
         "finished_at": past(hours=5), "attempt_no": 2,
         "created_at": past(hours=5), "updated_at": past(hours=5)},
        {"request_id": "req-dispatch-006", "idempotency_key": "dispatch:task-music-006",
         "task_id": "task-music-006", "stage": "dispatch", "kind": "dispatch",
         "status": "succeeded", "priority": 100, "requested_by": "user",
         "run_id": "run-music-006-b2", "claimed_by": "kaneo-dispatch",
         "claimed_at": past(hours=4),
         "result": {"outcome": "succeeded"}, "finished_at": past(hours=3),
         "attempt_no": 1, "created_at": past(hours=4), "updated_at": past(hours=3)},
        {"request_id": "req-plan-003", "idempotency_key": "plan:task-study-003",
         "task_id": "task-study-003", "stage": "plan", "kind": "plan",
         "status": "succeeded", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-study-003-p1", "claimed_by": "kaneo-plan",
         "claimed_at": past(hours=6),
         "result": {"outcome": "succeeded"}, "finished_at": past(hours=5),
         "attempt_no": 1, "created_at": past(hours=6), "updated_at": past(hours=5)},
    ]


def base_events():
    rows = [
        ("task-study-013", "migrated", "migration", {"from": "legacy_manifest", "status": "done"}),
        ("task-music-001", "task_created", "sync", {"source": "sync-tasks", "kaneo_status": "to-do"}),
        ("task-study-003", "plan_published", "kaneo-plan", {"plan_path": "~/Documents/Code/Plan/StudyTool/task-study-003_20250918.md"}),
        ("task-study-003", "status_change", "core", {"from": "planning", "to": "ready"}),
        ("task-comic-004", "enqueued", "user", {"kind": "dispatch", "request_id": "req-dispatch-004"}),
        ("task-fb-005", "claimed", "kaneo-dispatch", {"request_id": "req-dispatch-005", "run_id": "run-fb-005-a1"}),
        ("task-fb-005", "dispatch_prepared", "kaneo-dispatch", {"attachment_name": "task-fb-005_20250919.md"}),
        ("task-music-006", "dispatch_sent", "kaneo-dispatch", {"session_marker": "task-music-006/run-music-006-b2"}),
        ("task-music-006", "kaneo_transition", "core", {"from": "to-do", "to": "in-progress"}),
        ("task-music-006", "reported", "kaneo-monitor", {"progress": "已完成 12/18 个播放列表的比对"}),
        ("task-comic-007", "heartbeat", "kaneo-monitor", {"note": "回归测试中"}),
        ("task-lib-008", "status_change", "core", {"from": "running", "to": "in_review"}),
        ("task-study-009", "dispatch_failed", "kaneo-dispatch", {"failure_code": "validation_failed"}),
        ("task-study-009", "status_change", "core", {"from": "ready", "to": "needs_user"}),
        ("task-comic-010", "dispatch_failed", "kaneo-dispatch", {"send_state": "unknown", "reason": "发送证据缺失"}),
        ("task-fb-011", "reported", "kaneo-monitor", {"progress": "聚类完成 80%"}),
        (None, "paused", "user", {"stage": None, "paused": False}),
        ("task-comic-004", "priority_changed", "user", {"request_id": "req-dispatch-004", "priority": 100}),
        ("task-study-013", "review_registered", "kaneo-review", {"conclusion": "pass"}),
        ("task-fb-014", "rechecked", "user", {"ok": True}),
        ("task-music-006", "kaneo_observed", "kaneo-monitor", {"kaneo_status": "in-progress"}),
    ]
    events = []
    for i, (task_id, kind, actor, detail) in enumerate(rows):
        events.append({
            "id": i + 1,
            "at": past(minutes=(len(rows) - i) * 11),
            "task_id": task_id,
            "request_id": detail.get("request_id"),
            "kind": kind,
            "actor": actor,
            "detail": detail,
        })
    return events


# ---------------------------------------------------------------- derive / merge

SUMMARY_KEYS = [
    "task_id", "project_id", "project_name", "title", "stage", "status",
    "display_status", "last_progress", "blocked_reason", "next_claim_at",
    "active_request", "kaneo_status", "kaneo_checked_at", "priority",
    "plan_path", "updated_at",
]


def request_is_lost(req):
    if req.get("status") != "claimed":
        return False
    lease = req.get("lease_expires_at")
    if not lease:
        return False
    try:
        dt = datetime.strptime(lease, "%Y-%m-%dT%H:%M:%S.%f+00:00").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return dt < utcnow()


def merged_tasks(state):
    tasks = {t["task_id"]: dict(t) for t in base_tasks()}
    for tid, patch in state.get("task_overrides", {}).items():
        if tid in tasks:
            tasks[tid].update(patch)
    requests = merged_requests(state)
    for task in tasks.values():
        active = [r for r in requests
                  if r.get("task_id") == task["task_id"] and r["status"] in ("queued", "claimed")]
        if active:
            req = active[0]
            task["active_request"] = {
                "request_id": req["request_id"], "kind": req["kind"],
                "status": req["status"], "claimed_by": req.get("claimed_by"),
                "heartbeat_at": req.get("heartbeat_at"),
                "lease_expires_at": req.get("lease_expires_at"),
            }
            if any(request_is_lost(r) for r in active):
                task["display_status"] = "执行端失联"
            elif task["status"] == "ready" and req["kind"] == "dispatch" and req["status"] == "queued":
                task["display_status"] = "已排队"
        else:
            task.pop("active_request", None)
            if task.get("display_status") in ("已排队", "执行端失联"):
                task["display_status"] = {
                    "ready": "就绪", "running": "执行中", "in_review": "评审中",
                    "dispatching": "派发中", "prepared": "派发中", "submitting": "派发中",
                    "planning": "规划中", "needs_plan": "待规划",
                    "needs_user": "需人工", "done": "已归档",
                }.get(task["status"], task["status"])
    return list(tasks.values())


def merged_requests(state):
    requests = {r["request_id"]: dict(r) for r in base_requests()}
    for rid, patch in state.get("request_overrides", {}).items():
        if rid in requests:
            requests[rid].update(patch)
    for req in state.get("extra_requests", []):
        requests[req["request_id"]] = dict(req)
    return list(requests.values())


def merged_events(state):
    return base_events() + state.get("extra_events", [])


def allowed_actions(task, requests):
    """按 §4.10 前置条件近似推导 allowed_actions。"""
    status = task["status"]
    if status == "done":
        return ["recheck"]
    task_reqs = [r for r in requests if r.get("task_id") == task["task_id"]]
    if any(request_is_lost(r) for r in task_reqs):
        return ["release_lost", "recheck"]
    if status == "needs_user":
        if task.get("send_state") == "unknown":
            return ["restore", "accept_duplicate", "mark_sent", "recheck"]
        return ["retry", "replan", "dismiss", "mark_done", "recheck"]
    if status in ("needs_plan",):
        return ["enqueue", "recheck"]
    if status == "ready":
        actions = ["enqueue", "recheck"]
        if any(r["status"] == "queued" for r in task_reqs):
            actions.insert(1, "set-priority")
            actions.append("set-paused")
        return actions
    if status in ("running", "in_review"):
        actions = ["enqueue", "mark_done", "recheck"]
        if any(r["status"] == "queued" for r in task_reqs):
            actions.insert(2, "set-priority")
        return actions
    return ["recheck"]


def attempts_for(task):
    """按任务状态造 attempts 历史（含发送不明证据块）。"""
    tid = task["task_id"]
    base = {
        "request_id": "req-dispatch-%s" % tid[-3:],
        "run_id": task.get("run_id") or "run-" + tid,
        "stage": task.get("stage"), "kind": "dispatch",
        "started_at": past(hours=2), "finished_at": past(hours=2),
        "outcome": "succeeded", "send_state": task.get("send_state"),
        "note": None, "evidence": None,
    }
    if task.get("send_state") == "unknown":
        base.update({
            "outcome": "needs_user",
            "note": "点击发送后窗口失焦，未能观察到已发送气泡",
            "evidence": {
                "session_marker": task.get("session_marker") or "%s/%s" % (tid, base["run_id"]),
                "repository": "~/Documents/Code/Project/Personal Project/%s" % task.get("project_name", ""),
                "mode": "Agent", "model": "SWE-2 Max", "execution_mode": "Local",
                "attachment_name": (task.get("plan_path") or "plan.md").split("/")[-1],
                "attachment_visible": True, "instruction_visible": True,
                "message_visible": False,
                "session_title": task.get("session_title"),
                "session_id": task.get("session_id"),
                "screen_locked_at": past(minutes=70),
            },
        })
    elif task["status"] == "needs_user":
        base.update({
            "outcome": "needs_user",
            "note": task.get("blocked_summary") or task.get("failure_reason"),
            "evidence": None,
        })
    elif task["status"] in ("running", "in_review", "done"):
        base.update({
            "note": "派发确认完成",
            "evidence": {
                "session_marker": task.get("session_marker") or "%s/%s" % (tid, base["run_id"]),
                "attachment_name": (task.get("plan_path") or "plan.md").split("/")[-1],
                "message_visible": True,
            },
        })
    else:
        return []
    return [base]


def task_detail(task, requests):
    detail = {k: task.get(k) for k in SUMMARY_KEYS}
    for key, value in task.items():
        detail.setdefault(key, value)
    detail["allowed_actions"] = allowed_actions(task, requests)
    detail["attempts"] = attempts_for(task)
    detail["links"] = {
        "kaneo_task_url": "https://kaneo.example.com/task/%s" % task["task_id"]
            if task.get("kaneo_status") else None,
        "plan_file": task.get("plan_path"),
        "report_file": task.get("review_report_path"),
    }
    return detail


# ---------------------------------------------------------------- commands

def cmd_doctor(payload):
    online = bool(payload.get("online"))
    return {
        "ok": True,
        "checks": [
            {"name": "python", "ok": True, "detail": "3.10.10"},
            {"name": "db_writable", "ok": True,
             "detail": os.path.join(tempfile.gettempdir(), "kanpilot-fake.db")},
            {"name": "credentials", "ok": True,
             "detail": "present, baseUrl https://kaneo.example.com (fake)"},
            {"name": "plan_root", "ok": True, "detail": PLAN_ROOT},
            {"name": "repos_root", "ok": True, "detail": REPOS_ROOT},
            {"name": "state_root", "ok": True, "detail": APP_SUPPORT},
            {"name": "kaneo_ping", "ok": True, "detail": "HTTP 200 (fake)",
             "skipped": not online},
        ],
    }


def cmd_overview(state):
    tasks = merged_tasks(state)
    requests = merged_requests(state)
    paused = state.get("paused", {"global": False, "stages": []})
    task_counts = {}
    for t in tasks:
        task_counts[t["status"]] = task_counts.get(t["status"], 0) + 1
    req_counts = {"queued": 0, "claimed": 0, "lost": 0}
    for r in requests:
        if request_is_lost(r):
            req_counts["lost"] += 1
        elif r["status"] in req_counts:
            req_counts[r["status"]] += 1
    return {
        "paused": paused,
        "counts": {"tasks": task_counts, "requests": req_counts},
        "last_tick_at": past(minutes=3),
        "schema_version": 1,
        # 协议外可选字段：settings 值与迁移状态（真实核心 v1 不提供则 UI 隐藏）
        "settings": {
            "lease_seconds": 1200,
            "monitor_interval_seconds": 900,
            "plan_scan_interval_hours": 24,
            "plan_scan_anchor": "每日 09:00 Asia/Shanghai",
            "last_plan_scan_at": past(hours=2),
            "migration": {
                "at": past(days=2),
                "backup_path": "~/Library/Application Support/KaneoPilot/migration-20250918T020000",
                "manifests_sha256": [sha("manifests")],
            },
        },
    }


def cmd_list_tasks(state, payload):
    tasks = [t for t in merged_tasks(state)]
    stage = payload.get("stage")
    status = payload.get("status")
    if stage:
        tasks = [t for t in tasks if t.get("stage") == stage]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if payload.get("needs_attention"):
        tasks = [t for t in tasks
                 if t.get("display_status") in ("需人工", "执行端失联")]
    return {"tasks": [{k: t.get(k) for k in SUMMARY_KEYS} for t in tasks]}


def cmd_get_task(state, payload):
    tid = payload.get("task_id")
    if not tid:
        return fail("缺少必填字段 task_id", "bad_request")
    for task in merged_tasks(state):
        if task["task_id"] == tid:
            return {"task": task_detail(task, merged_requests(state))}
    return fail("任务不存在：%s" % tid, "not_found")


def cmd_list_requests(state, payload):
    requests = merged_requests(state)
    status = payload.get("status")
    if status == "active":
        requests = [r for r in requests if r["status"] in ("queued", "claimed")]
    elif status == "lost":
        requests = [r for r in requests if request_is_lost(r)]
    elif status:
        requests = [r for r in requests if r["status"] == status]
    for r in requests:
        r["display_status"] = "lost" if request_is_lost(r) else r["status"]
    requests.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    limit = payload.get("limit", 200)
    return {"requests": requests[: int(limit)]}


def cmd_list_events(state, payload):
    events = merged_events(state)
    tid = payload.get("task_id")
    if tid:
        events = [e for e in events if e.get("task_id") == tid]
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    limit = payload.get("limit", 200)
    return {"events": events[: int(limit)]}


def cmd_enqueue(state, payload):
    tid = payload.get("task_id")
    if not tid:
        return fail("缺少必填字段 task_id", "bad_request")
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    task = tasks.get(tid)
    if not task:
        return fail("任务不存在：%s" % tid, "not_found")
    kind = payload.get("kind") or {
        "needs_plan": "plan", "ready": "dispatch",
        "running": "monitor", "in_review": "review",
    }.get(task["status"])
    if not kind:
        return fail("无法按当前状态推断请求类型", "bad_request")
    key = "%s:%s" % ("plan-scan" if kind == "plan-scan" else kind, tid)
    for req in merged_requests(state):
        if req.get("idempotency_key") == key and req["status"] in ("queued", "claimed"):
            return {"request": req, "deduplicated": True}
    stage = {"plan-scan": "plan", "plan": "plan", "dispatch": "dispatch",
             "monitor": "monitor", "review": "review"}[kind]
    req = {
        "request_id": "req-" + uuid.uuid4().hex[:8],
        "idempotency_key": key, "task_id": tid, "stage": stage,
        "kind": kind, "status": "queued", "priority": 100,
        "requested_by": "user", "attempt_no": 0,
        "created_at": iso(utcnow()), "updated_at": iso(utcnow()),
    }

    def apply(s):
        s.setdefault("extra_requests", []).append(req)
        if kind == "dispatch" and task["status"] == "ready":
            s.setdefault("task_overrides", {}).setdefault(tid, {})["display_status"] = "已排队"
        append_event(s, "enqueued", task_id=tid, request_id=req["request_id"],
                     actor="user", detail={"kind": kind})
    mutate(state, apply)
    return {"request": req, "deduplicated": False}


def cmd_set_priority(state, payload):
    priority = payload.get("priority")
    rid = payload.get("request_id")
    tid = payload.get("task_id")
    if priority is None:
        return fail("缺少必填字段 priority", "bad_request")
    requests = merged_requests(state)
    target = None
    if rid:
        target = next((r for r in requests if r["request_id"] == rid), None)
    elif tid:
        target = next((r for r in requests
                       if r.get("task_id") == tid and r["status"] == "queued"), None)
    if not target:
        return fail("找不到可调整的排队请求", "not_found")

    def apply(s):
        s.setdefault("request_overrides", {}).setdefault(
            target["request_id"], {})["priority"] = int(priority)
        append_event(s, "priority_changed", task_id=target.get("task_id"),
                     request_id=target["request_id"], actor="user",
                     detail={"priority": int(priority)})
    mutate(state, apply)
    return {"ok": True, "request_id": target["request_id"], "priority": int(priority)}


def cmd_set_paused(state, payload):
    paused_flag = bool(payload.get("paused"))
    stage = payload.get("stage")

    def apply(s):
        cur = s.setdefault("paused", {"global": False, "stages": []})
        if stage is None:
            cur["global"] = paused_flag
        else:
            stages = set(cur.get("stages", []))
            if paused_flag:
                stages.add(stage)
            else:
                stages.discard(stage)
            cur["stages"] = sorted(stages)
        append_event(s, "paused" if paused_flag else "unpaused",
                     actor="user", detail={"stage": stage, "paused": paused_flag})
    mutate(state, apply)
    return {"paused": state.get("paused", {"global": False, "stages": []})}


def cmd_resolve(state, payload):
    tid = payload.get("task_id")
    action = payload.get("action")
    note = payload.get("note")
    if not tid or not action:
        return fail("缺少必填字段 task_id/action", "bad_request")
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    task = tasks.get(tid)
    if not task:
        return fail("任务不存在：%s" % tid, "not_found")
    requests = merged_requests(state)

    def fail_action(msg):
        return {"__fail__": True, "error": msg, "code": "bad_request"}

    def apply(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        clear = ["failure_code", "failure_reason", "failure_stage",
                 "blocked_at", "blocked_reason", "blocked_summary",
                 "manual_recovery_required", "lock_retained"]
        if action == "retry":
            if task["status"] != "needs_user" or task.get("send_state") == "unknown":
                return fail_action("当前状态不允许 retry")
            for k in clear:
                ov[k] = None
            ov.update({"status": "ready", "display_status": "已排队",
                       "send_state": "not_sent", "sent": 0})
            enqueue_request(s, tid, "dispatch")
        elif action == "restore":
            if task["status"] != "needs_user" or task.get("send_state") != "unknown":
                return fail_action("restore 仅适用于发送不明的阻塞任务")
            for k in clear:
                ov[k] = None
            ov.update({"status": "ready", "display_status": "已排队",
                       "send_state": "not_sent", "sent": 0})
            enqueue_request(s, tid, "dispatch")
        elif action == "accept_duplicate":
            if task["status"] != "needs_user" or task.get("send_state") != "unknown":
                return fail_action("accept_duplicate 仅适用于发送不明的阻塞任务")
            for k in clear:
                ov[k] = None
            ov.update({"status": "ready", "display_status": "已排队",
                       "send_state": "not_sent", "sent": 0,
                       "resolution": {"action": action, "note": note,
                                      "at": iso(utcnow())}})
            enqueue_request(s, tid, "dispatch")
        elif action == "mark_sent":
            if task["status"] != "needs_user" or task.get("send_state") != "unknown":
                return fail_action("mark_sent 仅适用于发送不明的阻塞任务")
            for k in clear:
                ov[k] = None
            ov.update({"status": "running", "display_status": "执行中",
                       "send_state": "sent", "sent": 1, "sent_at": iso(utcnow()),
                       "stage": "monitor"})
            enqueue_request(s, tid, "monitor")
        elif action == "replan":
            if task["status"] != "needs_user":
                return fail_action("replan 仅适用于需人工任务")
            for k in clear:
                ov[k] = None
            ov.update({"status": "needs_plan", "display_status": "待规划",
                       "stage": "plan", "send_state": None, "sent": None})
            enqueue_request(s, tid, "plan")
        elif action == "release_lost":
            lost = [r for r in requests
                    if r.get("task_id") == tid and request_is_lost(r)]
            if not lost:
                return fail_action("没有失联请求")
            req = lost[0]
            s.setdefault("request_overrides", {}).setdefault(
                req["request_id"], {})["status"] = "cancelled"
            if task["status"] in ("dispatching", "prepared", "submitting"):
                ov.update({"status": "needs_user", "display_status": "需人工",
                           "failure_code": "interrupted_dispatch",
                           "failure_reason": "派发执行端失联，已释放请求",
                           "blocked_at": iso(utcnow())})
            else:
                back = {"running": ("running", "执行中", "monitor"),
                        "in_review": ("in_review", "评审中", "review"),
                        "planning": ("needs_plan", "待规划", "plan")}.get(task["status"])
                if back:
                    ov.update({"status": back[0], "display_status": back[1]})
                    enqueue_request(s, tid, back[2])
        elif action == "mark_done":
            if task["status"] in ("dispatching", "prepared", "submitting"):
                return fail_action("派发中途不可登记完成")
            ov.update({"status": "done", "display_status": "已归档", "stage": "done"})
        elif action == "dismiss":
            if task["status"] != "needs_user":
                return fail_action("dismiss 仅适用于需人工任务")
            for k in clear:
                ov[k] = None
            ov.update({"status": "ready", "display_status": "就绪"})
        else:
            return fail_action("未知动作：%s" % action)
        append_event(s, "resolved", task_id=tid, actor="user",
                     detail={"action": action, "note": note})
        return None

    result = mutate(state, apply)
    if isinstance(result, dict) and result.get("__fail__"):
        return fail(result["error"], result["code"])
    return {"ok": True, "task_status": next(
        t["status"] for t in merged_tasks(load_state()) if t["task_id"] == tid)}


def enqueue_request(state, tid, kind):
    req = {
        "request_id": "req-" + uuid.uuid4().hex[:8],
        "idempotency_key": "%s:%s" % (kind, tid), "task_id": tid,
        "stage": {"plan": "plan", "dispatch": "dispatch",
                  "monitor": "monitor", "review": "review"}[kind],
        "kind": kind, "status": "queued", "priority": 100,
        "requested_by": "user", "attempt_no": 0,
        "created_at": iso(utcnow()), "updated_at": iso(utcnow()),
    }
    state.setdefault("extra_requests", []).append(req)
    return req


def cmd_recheck(state, payload):
    tid = payload.get("task_id")
    if not tid:
        return fail("缺少必填字段 task_id", "bad_request")
    task = next((t for t in merged_tasks(state) if t["task_id"] == tid), None)
    if not task:
        return fail("任务不存在：%s" % tid, "not_found")
    offline = bool(payload.get("offline"))
    checks = [
        {"name": "plan_file", "ok": task.get("plan_path") is not None,
         "detail": task.get("plan_path") or "无计划文件"},
        {"name": "plan_sha256", "ok": True, "detail": "重算一致 (fake)"},
        {"name": "source_sha256", "ok": task.get("source_sha256") is not None,
         "detail": "快照一致" if task.get("source_sha256") else "无来源快照"},
    ]
    if not offline:
        checks.append({"name": "kaneo_get_task", "ok": True,
                       "detail": "远端 %s (fake)" % (task.get("kaneo_status") or "unknown")})
    mutate(state, lambda s: append_event(
        s, "rechecked", task_id=tid, actor="user",
        detail={"ok": True, "offline": offline}))
    return {
        "task_id": tid, "checks": checks,
        "kaneo_status": task.get("kaneo_status"),
        "requirements_sha256": task.get("requirements_sha256"),
        "ok": all(c.get("ok") for c in checks),
    }


def fail(message, code):
    return {"__error__": message, "__code__": code}


COMMANDS = {
    "doctor": lambda s, p: cmd_doctor(p),
    "tick": lambda s, p: {"created": [], "skipped": [{"reason": "fake 不建请求"}]},
    "overview": lambda s, p: cmd_overview(s),
    "list-tasks": lambda s, p: cmd_list_tasks(s, p),
    "get-task": lambda s, p: cmd_get_task(s, p),
    "list-requests": lambda s, p: cmd_list_requests(s, p),
    "list-events": lambda s, p: cmd_list_events(s, p),
    "enqueue": lambda s, p: cmd_enqueue(s, p),
    "set-priority": lambda s, p: cmd_set_priority(s, p),
    "set-paused": lambda s, p: cmd_set_paused(s, p),
    "resolve": lambda s, p: cmd_resolve(s, p),
    "recheck": lambda s, p: cmd_recheck(s, p),
    "migrate": lambda s, p: {"ok": True, "backup_path": "(fake)",
                             "imported": {"tasks": 0, "requests": 0, "events": 0},
                             "warnings": ["fake 核心不执行迁移"]},
}


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: kanpilot-fake <command> [--json '<payload>']\n")
        return 2
    command = argv[1]
    payload = {}
    if "--json" in argv:
        idx = argv.index("--json")
        if idx + 1 < len(argv):
            try:
                payload = json.loads(argv[idx + 1])
            except json.JSONDecodeError:
                print(json.dumps({"error": "--json 不是合法 JSON", "code": "bad_request"}))
                return 1
    elif not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                print(json.dumps({"error": "stdin 不是合法 JSON", "code": "bad_request"}))
                return 1

    handler = COMMANDS.get(command)
    if not handler:
        print(json.dumps({"error": "未知命令：%s" % command, "code": "bad_request"},
                         ensure_ascii=False))
        return 1

    state = load_state()
    result = handler(state, payload)
    if isinstance(result, dict) and "__error__" in result:
        print(json.dumps({"error": result["__error__"], "code": result["__code__"]},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
