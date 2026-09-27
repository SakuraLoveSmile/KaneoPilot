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
    "Music": "example-project-01",
    "Library": "example-project-02",
    "StudyTool": "example-project-03",
    "Comic": "example-project-04",
    "Feedback": "example-project-05",
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
            "priority": 100, "created_at": past(days=1), "updated_at": past(hours=2),
        },
        {   # 规划中：plan 请求已被领取
            "task_id": "task-lib-002", "project_id": PROJECTS["Library"],
            "project_name": "Library", "title": "重构书库索引以支持多标签检索",
            "stage": "plan", "status": "planning", "display_status": "规划中",
            "kaneo_status": "to-do", "kaneo_checked_at": past(hours=1),
            "requirements_sha256": sha("req-lib-002"),
            "priority": 100, "created_at": past(days=2), "updated_at": past(minutes=25),
            "active_request": {
                "request_id": "req-plan-002", "kind": "plan", "status": "claimed",
                "claimed_by": "kaneo-plan", "heartbeat_at": past(minutes=2),
                "lease_expires_at": future(minutes=18),
            },
        },
        {   # 就绪：计划已发布，无活动请求
            "task_id": "task-study-003", "project_id": PROJECTS["StudyTool"],
            "project_name": "StudyTool", "title": "为复习卡片加入间隔重复调度",
            "stage": "dispatch", "status": "ready", "display_status": "待确认执行",
            "plan_execution_hold": True,
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=30),
            "requirements_sha256": sha("req-study-003"),
            "plan_path": "~/Documents/Code/Plan/StudyTool/task-study-003_20250918.md",
            "plan_sha256": sha("plan-study-003"), "plan_kind": "standard",
            "plan_generated_at": past(hours=5), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "source_sha256": sha("src-study-003"),
            "source_path": "~/Library/Application Support/Codex/KaneoPlanner/state/sources/task-study-003.json",
            "priority": 100, "created_at": past(days=3), "updated_at": past(hours=5),
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
            "priority": 100, "created_at": past(days=2), "updated_at": past(minutes=40),
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
            "priority": 100, "created_at": past(days=4), "updated_at": past(minutes=6),
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
            "run_id": "run-music-006-b2", "session_id": "pi-sess-6f3a",
            "session_title": "pi: sync playlists",
            "session_marker": "task-music-006/run-music-006-b2",
            "send_state": "sent", "sent": 1, "sent_at": past(hours=3),
            "last_progress": "已完成 12/18 个播放列表的比对",
            "priority": 100, "created_at": past(days=6), "updated_at": past(minutes=12),
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
            "run_id": "run-comic-007-c3", "session_id": "pi-sess-91cd",
            "session_title": "pi: lazy-load fix",
            "session_marker": "task-comic-007/run-comic-007-c3",
            "send_state": "sent", "sent": 1, "sent_at": past(days=1),
            "last_progress": "回归测试中，前 3 章图片已正常",
            "priority": 100, "created_at": past(days=7), "updated_at": past(minutes=45),
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
            "run_id": "run-lib-008-d4", "session_id": "pi-sess-2e7b",
            "session_title": "pi: zotero import",
            "session_marker": "task-lib-008/run-lib-008-d4",
            "send_state": "sent", "sent": 1, "sent_at": past(days=2),
            "last_progress": "执行端报告完成，等待评审",
            "priority": 100, "created_at": past(days=8), "updated_at": past(minutes=22),
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
            "priority": 100, "created_at": past(days=3), "updated_at": past(minutes=50),
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
            "priority": 100, "created_at": past(days=4), "updated_at": past(minutes=70),
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
            "run_id": "run-fb-011-g7", "session_id": "pi-sess-41aa",
            "session_title": "pi: crash aggregation",
            "session_marker": "task-fb-011/run-fb-011-g7",
            "send_state": "sent", "sent": 1, "sent_at": past(days=2),
            "last_progress": "聚类完成 80%，随后失联",
            "priority": 100, "created_at": past(days=9), "updated_at": past(hours=1),
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
            "priority": 100, "created_at": past(days=2), "updated_at": past(minutes=90),
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
            "priority": 100, "created_at": past(days=12), "updated_at": past(days=1),
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
            "priority": 50, "created_at": past(days=1), "updated_at": past(hours=3),
            "next_claim_at": future(minutes=12),
        },
        {   # 需人工（规划阶段受阻，非失败码）→ needs_plan 列「规划受阻」
            "task_id": "task-lib-015", "project_id": PROJECTS["Library"],
            "project_name": "Library", "title": "扫描馆藏 PDF 并重建全文检索索引",
            "stage": "plan", "status": "needs_user", "display_status": "需人工",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=95),
            "requirements_sha256": sha("req-lib-015"),
            "failure_code": "observation_blocked",
            "failure_reason": "规划执行端报告：来源快照缺少 requirements_sha256，无法起草计划",
            "failure_stage": "plan",
            "blocked_at": past(minutes=95),
            "blocked_reason": "规划执行端报告阻塞：来源快照缺少 requirements_sha256",
            "blocked_summary": "plan 请求回报 needs_user：来源快照不完整",
            "priority": 100, "created_at": past(days=2), "updated_at": past(minutes=95),
        },
        {   # 规划失败 → failed 列「规划失败」
            "task_id": "task-music-016", "project_id": PROJECTS["Music"],
            "project_name": "Music", "title": "导出播放统计周报并发送到邮箱",
            "stage": "plan", "status": "needs_user", "display_status": "需人工",
            "kaneo_status": "to-do", "kaneo_checked_at": past(hours=3),
            "requirements_sha256": sha("req-music-016"),
            "failure_code": "plan_failed",
            "failure_reason": "计划文件 frontmatter 缺少 project_name，publish-plan 校验未通过",
            "failure_stage": "plan",
            "blocked_at": past(hours=3),
            "blocked_reason": "计划文件 frontmatter 缺少 project_name",
            "blocked_summary": "publish-plan 校验失败：frontmatter.project_name 缺失",
            "priority": 100, "created_at": past(days=5), "updated_at": past(hours=3),
        },
        {   # 评审结论需修改 → in_review 列「待修复」（关联 fix_task_id）
            "task_id": "task-study-017", "project_id": PROJECTS["StudyTool"],
            "project_name": "StudyTool", "title": "为错题本增加标签过滤与批量导出",
            "stage": "review", "status": "in_review", "display_status": "评审中",
            "kaneo_status": "in-review", "kaneo_checked_at": past(minutes=30),
            "requirements_sha256": sha("req-study-017"),
            "plan_path": "~/Documents/Code/Plan/StudyTool/task-study-017_20250916.md",
            "plan_sha256": sha("plan-study-017"), "plan_kind": "standard",
            "plan_generated_at": past(days=4), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-study-017-j2", "session_id": "pi-sess-77bb",
            "session_title": "pi: tag filters",
            "session_marker": "task-study-017/run-study-017-j2",
            "send_state": "sent", "sent": 1, "sent_at": past(days=1),
            "last_progress": "评审要求补充空标签兜底处理",
            "review_conclusion": "changes_requested",
            "review_registered_at": past(minutes=30),
            "review_key": "rev-study-017",
            "review_report_path": "~/Library/Application Support/Codex/KaneoReviewer/reports/task-study-017/20250920T013000.md",
            "review_report_sha256": sha("report-study-017"),
            "fix_task_id": "task-study-021",
            "priority": 100, "created_at": past(days=6), "updated_at": past(minutes=30),
        },
        {   # 执行失败 → failed 列「执行失败」
            "task_id": "task-comic-018", "project_id": PROJECTS["Comic"],
            "project_name": "Comic", "title": "批量重下失败章节的缺失图片",
            "stage": "monitor", "status": "needs_user", "display_status": "需人工",
            "kaneo_status": "in-progress", "kaneo_checked_at": past(hours=4),
            "requirements_sha256": sha("req-comic-018"),
            "plan_path": "~/Documents/Code/Plan/Comic/task-comic-018_20250913.md",
            "plan_sha256": sha("plan-comic-018"), "plan_kind": "standard",
            "plan_generated_at": past(days=7), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-comic-018-k5", "session_id": "pi-sess-0e21",
            "session_title": "pi: retry images",
            "session_marker": "task-comic-018/run-comic-018-k5",
            "send_state": "sent", "sent": 1, "sent_at": past(days=2),
            "failure_code": "execution_failed",
            "failure_reason": "执行端报告失败：图片源站连续 403，重试 5 次仍失败",
            "failure_stage": "monitor",
            "blocked_at": past(hours=4),
            "blocked_reason": "图片源站连续 403，重试 5 次仍失败",
            "blocked_summary": "monitor report outcome=failed：源站 403",
            "priority": 100, "created_at": past(days=8), "updated_at": past(hours=4),
        },
        {   # 需人工（评审阶段阻塞，非失败码）→ in_review 列「评审受阻」
            "task_id": "task-fb-019", "project_id": PROJECTS["Feedback"],
            "project_name": "Feedback", "title": "按系统版本拆分崩溃堆栈报表",
            "stage": "review", "status": "needs_user", "display_status": "需人工",
            "kaneo_status": "in-review", "kaneo_checked_at": past(minutes=105),
            "requirements_sha256": sha("req-fb-019"),
            "failure_code": "review_blocked",
            "failure_reason": "评审执行端报告：Kaneo 附件下载失败，无法完成报告比对",
            "failure_stage": "review",
            "blocked_at": past(minutes=105),
            "blocked_reason": "评审执行端报告阻塞：Kaneo 附件下载失败",
            "blocked_summary": "review 请求回报 needs_user：附件 403",
            "priority": 100, "created_at": past(days=5), "updated_at": past(minutes=105),
        },
        {   # 执行中但尚无检查记录 → in_progress 列「执行中」+「未检查」
            "task_id": "task-fb-020", "project_id": PROJECTS["Feedback"],
            "project_name": "Feedback", "title": "把反馈看板导出为月度 PDF 摘要",
            "stage": "monitor", "status": "running", "display_status": "执行中",
            "kaneo_status": "in-progress", "kaneo_checked_at": None,
            "requirements_sha256": sha("req-fb-020"),
            "plan_path": "~/Documents/Code/Plan/Feedback/task-fb-020_20250919.md",
            "plan_sha256": sha("plan-fb-020"), "plan_kind": "standard",
            "plan_generated_at": past(hours=8), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-fb-020-m4", "session_id": "pi-sess-5c9d",
            "session_title": "pi: monthly pdf",
            "session_marker": "task-fb-020/run-fb-020-m4",
            "send_state": "sent", "sent": 1, "sent_at": past(hours=6),
            "priority": 100, "created_at": past(days=2), "updated_at": past(hours=6),
        },
        {   # 评审修复任务（task-study-017 的 fix_task）→ needs_plan 列「待规划」
            "task_id": "task-study-021", "project_id": PROJECTS["StudyTool"],
            "project_name": "StudyTool", "title": "修复：错题标签过滤的空值兜底",
            "stage": "plan", "status": "needs_plan", "display_status": "待规划",
            "kaneo_status": "to-do", "kaneo_checked_at": past(minutes=30),
            "requirements_sha256": sha("req-study-021"),
            "plan_kind": "review_fix",
            "review_source_task_id": "task-study-017",
            "review_key": "rev-study-017",
            "source_sha256": sha("src-study-021"),
            "source_path": "~/Library/Application Support/Codex/KaneoPlanner/state/sources/task-study-021.json",
            "priority": 100, "created_at": past(minutes=28), "updated_at": past(minutes=28),
        },
        {   # 已归档（save_state=archived）：移出活跃看板，可恢复
            "task_id": "task-lib-022", "project_id": PROJECTS["Library"],
            "project_name": "Library", "title": "为借阅记录增加逾期提醒模板",
            "stage": "review", "status": "done", "display_status": "已归档",
            "kaneo_status": "done", "kaneo_checked_at": past(days=2),
            "requirements_sha256": sha("req-lib-022"),
            "plan_path": "~/Documents/Code/Plan/Library/task-lib-022_20250908.md",
            "plan_sha256": sha("plan-lib-022"), "plan_kind": "standard",
            "plan_generated_at": past(days=12), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "run_id": "run-lib-022-n3", "session_id": "pi-sess-8b12",
            "session_title": "pi: overdue reminders",
            "session_marker": "task-lib-022/run-lib-022-n3",
            "send_state": "sent", "sent": 1, "sent_at": past(days=11),
            "review_conclusion": "pass",
            "review_key": "rev-lib-022",
            "save_state": "archived", "saved_at": past(days=2),
            "auto_advance_paused": 0,
            "priority": 100, "created_at": past(days=14), "updated_at": past(days=2),
        },
        {   # 回收站（save_state=trashed）：可恢复，不提供永久删除
            "task_id": "task-comic-023", "project_id": PROJECTS["Comic"],
            "project_name": "Comic", "title": "重排漫画封面比例并生成缩略图",
            "stage": "dispatch", "status": "ready", "display_status": "就绪",
            "kaneo_status": "to-do", "kaneo_checked_at": past(hours=8),
            "requirements_sha256": sha("req-comic-023"),
            "plan_path": "~/Documents/Code/Plan/Comic/task-comic-023_20250911.md",
            "plan_sha256": sha("plan-comic-023"), "plan_kind": "standard",
            "plan_generated_at": past(days=6), "planner_model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "source_sha256": sha("src-comic-023"),
            "source_path": "~/Library/Application Support/Codex/KaneoPlanner/state/sources/task-comic-023.json",
            "save_state": "trashed", "saved_at": past(hours=6),
            "auto_advance_paused": 1,
            "priority": 100, "created_at": past(days=6), "updated_at": past(hours=6),
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
        # v2 §5.22.2：旧未授权 queued plan 请求（payload 无 manual_authorized）→ 等待确认材料。
        {"request_id": "req-plan-001", "idempotency_key": "plan:task-music-001",
         "task_id": "task-music-001", "stage": "plan", "kind": "plan",
         "status": "queued", "priority": 100, "requested_by": "scheduler",
         "attempt_no": 0, "created_at": past(minutes=35), "updated_at": past(minutes=35)},
        # v2 §5.22.2：已授权 queued plan（payload.manual_authorized=true）→ 已排队等待规划。
        {"request_id": "req-plan-021", "idempotency_key": "plan:task-study-021",
         "task_id": "task-study-021", "stage": "plan", "kind": "plan",
         "status": "queued", "priority": 100, "requested_by": "user",
         "payload": {"manual_authorized": True, "input_id": "pin-study-021",
                    "planner_input_sha256": sha("input-study-021")},
         "attempt_no": 0, "created_at": past(minutes=20), "updated_at": past(minutes=20)},
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
        {"request_id": "req-dispatch-014", "idempotency_key": "dispatch:task-fb-014",
         "task_id": "task-fb-014", "stage": "dispatch", "kind": "dispatch",
         "status": "queued", "priority": 50, "requested_by": "user",
         "attempt_no": 0, "created_at": past(minutes=20), "updated_at": past(minutes=20)},
        {"request_id": "req-monitor-020", "idempotency_key": "monitor:task-fb-020",
         "task_id": "task-fb-020", "stage": "monitor", "kind": "monitor",
         "status": "queued", "priority": 100, "requested_by": "scheduler",
         "attempt_no": 0, "created_at": past(hours=6), "updated_at": past(hours=6)},
        {"request_id": "req-plan-016", "idempotency_key": "plan:task-music-016",
         "task_id": "task-music-016", "stage": "plan", "kind": "plan",
         "status": "failed", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-music-016-p1", "claimed_by": "kaneo-plan",
         "claimed_at": past(hours=3),
         "result": {"outcome": "needs_user",
                    "reason": "计划文件 frontmatter 缺少 project_name"},
         "finished_at": past(hours=3), "attempt_no": 2,
         "created_at": past(hours=4), "updated_at": past(hours=3)},
        {"request_id": "req-monitor-018", "idempotency_key": "monitor:task-comic-018",
         "task_id": "task-comic-018", "stage": "monitor", "kind": "monitor",
         "status": "failed", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-comic-018-m2", "claimed_by": "kaneo-monitor",
         "claimed_at": past(hours=4),
         "result": {"outcome": "needs_user",
                    "reason": "图片源站连续 403，重试 5 次仍失败"},
         "finished_at": past(hours=4), "attempt_no": 3,
         "created_at": past(hours=4), "updated_at": past(hours=4)},
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
        ("task-study-017", "review_registered", "kaneo-review",
         {"conclusion": "changes_requested", "fix_task_id": "task-study-021"}),
        ("task-study-021", "fix_task_created", "kaneo-review",
         {"source_task_id": "task-study-017", "review_key": "rev-study-017"}),
        ("task-music-016", "reported", "kaneo-plan",
         {"outcome": "needs_user", "reason": "frontmatter.project_name 缺失"}),
        ("task-comic-018", "reported", "kaneo-monitor",
         {"outcome": "failed", "reason": "图片源站连续 403"}),
        ("task-lib-015", "reported", "kaneo-plan",
         {"outcome": "needs_user", "reason": "来源快照缺少 requirements_sha256"}),
        ("task-fb-020", "dispatch_sent", "kaneo-dispatch",
         {"session_marker": "task-fb-020/run-fb-020-m4"}),
    ]
    # v2 §4.3：补足历史量演练游标翻页（加载更多）。
    filler_kinds = ["heartbeat", "kaneo_observed", "reported", "status_change"]
    filler_tasks = ["task-music-006", "task-comic-007", "task-lib-008",
                    "task-study-017", "task-fb-011", None]
    for i in range(110):
        rows.append((
            filler_tasks[i % len(filler_tasks)],
            filler_kinds[i % len(filler_kinds)],
            "kaneo-monitor" if i % 3 else "core",
            {"seq": i, "note": "历史事件 %d" % i},
        ))
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
    "display_status", "board_column", "lifecycle_column", "exception",
    "board_substatus", "last_checked_at",
    "review_summary", "last_progress", "blocked_reason", "next_claim_at",
    "active_request", "kaneo_status", "kaneo_checked_at", "priority",
    "plan_path", "created_at", "updated_at",
    "save_state", "saved_at", "auto_advance_paused",
    "plan_execution_hold",
    "move_out_eligibility",
]

# protocol §2「看板列」归列映射（桌面唯一分列依据，这里与真核心同口径派生）。
STATUS_TO_COLUMN = {
    "needs_plan": "needs_plan", "planning": "needs_plan",
    "ready": "todo", "dispatching": "todo",
    "prepared": "todo", "submitting": "todo",
    "running": "in_progress", "in_review": "in_review",
    "done": "done",
}
STAGE_TO_COLUMN = {
    "needs_plan": "needs_plan", "planning": "needs_plan", "plan": "needs_plan",
    "ready": "todo", "dispatching": "todo", "prepared": "todo",
    "submitting": "todo", "dispatch": "todo",
    "running": "in_progress", "monitor": "in_progress",
    "in_review": "in_review", "review": "in_review",
    "done": "done",
}
# §2：needs_user 且 failure_code 属此类 → failed 列；其余按 failure_stage 归列。
FAILURE_CODES = {"validation_failed", "dispatch_failed",
                 "execution_failed", "plan_failed"}
FAILED_LABELS = {"plan_failed": "规划失败", "dispatch_failed": "派发失败",
                 "validation_failed": "派发失败", "execution_failed": "执行失败"}

# §5.15.2 归档/回收站禁用状态（正在规划/派发/执行/评审）。
BUSY_STATUSES = {"planning", "dispatching", "prepared", "submitting",
                 "running", "in_review"}


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


def review_summary(task):
    """§5.2 review_summary：当前周期评审结论快照，无则 None。"""
    rs = task.get("review_summary")
    if rs:
        return rs
    if task.get("review_conclusion"):
        return {
            "conclusion": task["review_conclusion"],
            "registered_at": task.get("review_registered_at"),
            "fix_task_id": task.get("fix_task_id"),
            "review_key": task.get("review_key"),
            "report_path": task.get("review_report_path"),
        }
    return None


def derive_board(task, requests):
    """派生 board_column / lifecycle_column / exception / board_substatus /
    blocked_reason / last_checked_at / review_summary（protocol §5.15.4 v1.3）。
    异常（needs_user、评审未通过、失联）一律进第六列 failed；lifecycle_column
    保留纯生命周期位置；exception 给结构化异常信息。"""
    status = task.get("status")
    task_reqs = [r for r in requests if r.get("task_id") == task["task_id"]]
    active = [r for r in task_reqs if r.get("status") in ("queued", "claimed")]
    lost = [r for r in active if request_is_lost(r)]

    # --- lifecycle_column（纯生命周期位置，无异常逻辑）
    if status == "needs_user":
        lifecycle = STAGE_TO_COLUMN.get(task.get("failure_stage")) \
            or STAGE_TO_COLUMN.get(task.get("stage")) or "todo"
    else:
        lifecycle = STATUS_TO_COLUMN.get(status, "todo")

    # --- exception（§5.15.4 结构化异常；无异常为 None）
    summary = review_summary(task)
    exception = None
    if lost:
        kind_cn = {"plan": "规划", "dispatch": "派发",
                   "monitor": "监控", "review": "评审"}
        kinds = "、".join(sorted(
            {kind_cn.get(r.get("kind"), r.get("kind") or "request")
             for r in lost}))
        blocked_reason = "执行端失联（%s请求租约过期）" % kinds
        exception = {"kind": "executor_lost", "label": "执行端失联",
                     "severity": "blocked", "detail": blocked_reason,
                     "failure_code": None, "fix_task_id": None}
    elif status == "in_review" and (summary or {}).get("conclusion") == "changes_requested":
        blocked_reason = task.get("blocked_reason") or task.get("failure_reason") \
            or "评审结论为需修改，等待修复任务完成"
        exception = {"kind": "review_rejected", "label": "评审未通过·待修复",
                     "severity": "blocked", "detail": blocked_reason,
                     "failure_code": None,
                     "fix_task_id": summary.get("fix_task_id")}
    elif status == "needs_user":
        code = task.get("failure_code")
        if task.get("send_state") == "unknown":
            kind, label, severity = "send_unknown", "发送不明", "blocked"
        elif code == "observation_blocked":
            kind, label, severity = "observation_blocked", "监控异常", "blocked"
        elif code == "review_blocked":
            kind, label, severity = "review_blocked", "评审受阻", "blocked"
        elif code in FAILURE_CODES:
            kind, severity = "failed", "failed"
            label = FAILED_LABELS.get(code, "失败")
        else:
            kind, label, severity = "manual_block", "需人工处理", "blocked"
        blocked_reason = task.get("blocked_reason") \
            or task.get("failure_reason") or "需人工处理"
        exception = {"kind": kind, "label": label, "severity": severity,
                     "detail": blocked_reason, "failure_code": code,
                     "fix_task_id": task.get("fix_task_id")}
    else:
        blocked_reason = None

    # --- board_column：异常一律进第六列 failed（§5.15.4）
    column = "failed" if exception is not None else lifecycle

    # --- board_substatus：第六列等于 exception.label；前五列维持 v1.2 词表
    if exception is not None:
        substatus = exception["label"]
    elif column == "needs_plan":
        substatus = "规划中" if status == "planning" else "待规划"
    elif column == "todo":
        if status in ("dispatching", "prepared", "submitting"):
            substatus = "派发中"
        elif any(r.get("kind") == "dispatch" and r.get("status") == "queued"
                 for r in active):
            substatus = "已排队"
        else:
            substatus = "就绪"
    elif column == "in_progress":
        substatus = "执行中"
    elif column == "in_review":
        substatus = "待修复" if (summary or {}).get("conclusion") == "changes_requested" \
            else "评审中"
    else:
        substatus = "已完成"

    # --- last_checked_at = max(kaneo_checked_at, 活动请求 heartbeat_at, 罐头值)
    candidates = [task.get("kaneo_checked_at"), task.get("last_checked_at")]
    candidates += [r.get("heartbeat_at") for r in active]
    last_checked = max([c for c in candidates if c], default=None)

    task["board_column"] = column
    task["lifecycle_column"] = lifecycle
    task["exception"] = exception
    task["board_substatus"] = substatus
    task["blocked_reason"] = blocked_reason
    task["last_checked_at"] = last_checked
    reason = move_out_blocked_reason(task, requests)
    task["move_out_eligibility"] = {"allowed": reason is None, "reason": reason}
    task["review_summary"] = summary


def merged_tasks(state):
    tasks = {t["task_id"]: dict(t) for t in base_tasks()}
    for tid, patch in state.get("task_overrides", {}).items():
        if tid in tasks:
            tasks[tid].update(patch)
    requests = merged_requests(state)
    for task in tasks.values():
        # §5.15.1 保存状态：与生命周期正交，缺省 active
        task.setdefault("save_state", "active")
        task.setdefault("auto_advance_paused", 0)
        active = [r for r in requests
                  if r.get("task_id") == task["task_id"] and r["status"] in ("queued", "claimed")]
        if active:
            req = active[0]
            task["active_request"] = {
                "request_id": req["request_id"], "kind": req["kind"],
                "status": req["status"], "priority": req.get("priority"),
                "claimed_by": req.get("claimed_by"),
                "heartbeat_at": req.get("heartbeat_at"),
                "lease_expires_at": req.get("lease_expires_at"),
                "not_before": req.get("not_before"),
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
        # v2 §5.22.4：计划已发布未确认 → display_status「待确认执行」。
        if task["status"] == "ready" and task.get("plan_execution_hold"):
            task["display_status"] = "待确认执行"
        derive_board(task, requests)
    return list(tasks.values())

def merged_requests(state):
    requests = {r["request_id"]: dict(r) for r in base_requests()}
    for rid, patch in state.get("request_overrides", {}).items():
        if rid in requests:
            requests[rid].update(patch)
    for req in state.get("extra_requests", []):
        requests[req["request_id"]] = dict(req)
    # v2 §5.21/§5.22：manual_authorized 由 payload_json.manual_authorized 派生到行级字段。
    for req in requests.values():
        payload = req.get("payload") or {}
        req["manual_authorized"] = payload.get("manual_authorized")
    return list(requests.values())


def merged_events(state):
    return base_events() + state.get("extra_events", [])


def move_out_blocked_reason(task, requests):
    """§5.15.2 归档/回收站禁用条件；可移出返回 None。"""
    status = task["status"]
    if status in BUSY_STATUSES:
        cn = {"planning": "正在规划", "dispatching": "正在派发",
              "prepared": "正在派发", "submitting": "正在派发",
              "running": "正在执行", "in_review": "正在评审"}[status]
        return cn + "，暂不可移出看板"
    if task.get("send_state") == "unknown":
        return "发送不明，需先人工裁决后再移出看板"
    task_reqs = [r for r in requests if r.get("task_id") == task["task_id"]]
    if any(request_is_lost(r) for r in task_reqs):
        return "存在未解除的失联请求，暂不可移出看板"
    if any(r.get("status") == "claimed" for r in task_reqs):
        return "任务已被执行端领取，暂不能移出看板"
    return None


def allowed_actions(task, requests):
    """按 §4.10 / §5.15.6 前置条件近似推导 allowed_actions。"""
    if task.get("save_state") == "purged":
        return []
    if task.get("save_state") != "active":
        actions = ["restore", "recheck"]
        if task.get("save_state") == "trashed":
            actions.append("purge")
        if task.get("auto_advance_paused"):
            actions.insert(1, "resume-auto")
        return actions
    if status == "done":
        actions = ["recheck"]
        if move_out_blocked_reason(task, requests) is None:
            actions += ["archive", "trash"]
        return actions
    task_reqs = [r for r in requests if r.get("task_id") == task["task_id"]]
    if any(request_is_lost(r) for r in task_reqs):
        return ["release_lost", "recheck"]
    if status == "needs_user":
        if task.get("send_state") == "unknown":
            return ["restore", "accept_duplicate", "mark_sent", "recheck"]
        return ["retry", "replan", "dismiss", "mark_done", "recheck"]
    if status in ("needs_plan",):
        # v2 §5.22：plan 不再经 enqueue，打开规划面板（prepare_plan）。
        return ["prepare_plan", "recheck"]
    if status == "ready":
        # v2 §5.22.4：plan_execution_hold → 仅确认执行（enqueue confirm_plan）。
        if task.get("plan_execution_hold"):
            return ["confirm_dispatch", "recheck"]
        actions = ["enqueue", "recheck"]
        if any(r["status"] == "queued" for r in task_reqs):
            actions.insert(1, "set-priority")
            actions.append("set-paused")
        if move_out_blocked_reason(task, requests) is None:
            actions += ["archive", "trash"]
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
    # v2 §5.20：purged 任务详情为只读审计视图。
    detail["read_only"] = task.get("save_state") == "purged"
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
    tasks = [t for t in merged_tasks(state) if t.get("save_state") == "active"]
    requests = merged_requests(state)
    paused = state.get("paused", {"global": False, "stages": []})
    task_counts = {}
    for t in tasks:
        task_counts[t["status"]] = task_counts.get(t["status"], 0) + 1
    board_counts = {c: 0 for c in
                    ("needs_plan", "todo", "in_progress", "in_review", "done", "failed")}
    for t in tasks:
        col = t.get("board_column")
        if col in board_counts:
            board_counts[col] += 1
    req_counts = {"queued": 0, "claimed": 0, "lost": 0}
    for r in requests:
        if request_is_lost(r):
            req_counts["lost"] += 1
        elif r["status"] in req_counts:
            req_counts[r["status"]] += 1
    return {
        "paused": paused,
        "counts": {"tasks": task_counts, "board": board_counts,
                   "requests": req_counts},
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
    # §5.15.3：save_state 缺省 active；非法值 bad_request
    save_state = payload.get("save_state") or "active"
    # v2 §5.20：purged 终态；all 覆盖 active+archived+trashed，purged 仅按名查询。
    if save_state not in ("active", "archived", "trashed", "purged", "all"):
        return fail("save_state 只能是 active|archived|trashed|purged|all", "bad_request")
    if save_state == "all":
        tasks = [t for t in tasks if t.get("save_state") != "purged"]
    else:
        tasks = [t for t in tasks if t.get("save_state") == save_state]
    stage = payload.get("stage")
    status = payload.get("status")
    if stage:
        tasks = [t for t in tasks if t.get("stage") == stage]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    board_column = payload.get("board_column")
    if board_column:
        tasks = [t for t in tasks if t.get("board_column") == board_column]
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


EVENT_KIND_CN = {
    "task_created": "任务创建", "task_updated": "任务更新",
    "requirements_changed": "需求变更", "kaneo_observed": "远端观察",
    "plan_published": "计划发布", "enqueued": "已入队", "claimed": "已领取",
    "heartbeat": "心跳", "status_change": "状态变更",
    "dispatch_prepared": "派发就绪", "kaneo_transition": "远端迁移",
    "dispatch_sent": "派发已发送", "dispatch_failed": "派发失败",
    "reported": "已回报", "review_registered": "评审登记",
    "fix_task_created": "修复任务创建", "resolved": "人工解决",
    "paused": "已暂停", "unpaused": "已恢复",
    "priority_changed": "优先级变更", "migrated": "已迁移",
    "rechecked": "已重新校验", "request_cancelled": "请求取消",
    "lost_detected": "执行端失联", "task_archived": "任务归档",
    "task_trashed": "移入回收站", "task_restored": "任务恢复",
    "session_trashed": "会话移入回收站", "session_restored": "会话恢复",
    "round_started": "执行轮次启动", "exec_config_updated": "执行配置已保存",
    "task_purged": "任务永久从列表移除",
    "session_purged": "会话永久从列表移除",
    "plan_input_prepared": "规划材料就绪", "plan_started": "规划已排队",
    "plan_ready": "规划产出就绪", "plan_needs_input": "规划需要补充输入",
}


def _event_enrichment(state, event):
    """v2 §5.21.2：task_title/project_name/stage/result/summary 回填。"""
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    requests = {r["request_id"]: r for r in merged_requests(state)}
    task = tasks.get(event.get("task_id") or "")
    req = requests.get(event.get("request_id") or "")
    kind = event.get("kind")
    detail = event.get("detail") or {}
    stage = (req or {}).get("stage") or (task or {}).get("stage")
    result = (req or {}).get("result")
    if isinstance(result, dict):
        result = result.get("outcome")
    cn = EVENT_KIND_CN.get(kind) or "未识别事件（%s）" % kind
    summary = cn
    if kind == "status_change":
        summary = "任务状态：%s → %s" % (detail.get("from"), detail.get("to"))
    elif kind in ("enqueued", "claimed"):
        summary = "%s（%s）" % (cn, detail.get("kind") or (req or {}).get("kind") or "请求")
    elif kind == "reported":
        note = detail.get("note") or detail.get("reason") or detail.get("progress")
        summary = "%s：%s" % (cn, note) if note else cn
    return {
        "task_title": (task or {}).get("title")
            or ("未保留标题 · %s" % (event.get("task_id") or "")[:8]
                if event.get("task_id") else None),
        "project_name": (task or {}).get("project_name"),
        "stage": stage, "result": result, "summary": summary,
    }


def cmd_list_events(state, payload):
    """v2 §5.21.2：query/stage/result 先过滤后按 id DESC 分页（before_id 游标）。"""
    events = merged_events(state)
    tid = payload.get("task_id")
    if tid:
        events = [e for e in events if e.get("task_id") == tid]
    stage = payload.get("stage")
    result = payload.get("result")
    query = (payload.get("query") or "").strip().lower()
    enriched = []
    for e in events:
        row = dict(e)
        row.update(_event_enrichment(state, e))
        enriched.append(row)
    if stage:
        enriched = [e for e in enriched if e.get("stage") == stage]
    if result:
        enriched = [e for e in enriched if e.get("result") == result]
    if query:
        def _hit(e):
            hay = " ".join(str(x or "") for x in (
                e.get("task_title"), e.get("project_name"),
                e.get("task_id"), e.get("request_id"),
                e.get("summary"), json.dumps(e.get("detail") or {},
                                            ensure_ascii=False)))
            return query in hay.lower()
        enriched = [e for e in enriched if _hit(e)]
    enriched.sort(key=lambda e: e.get("id") or 0, reverse=True)
    before = payload.get("before_id")
    if before is not None:
        try:
            enriched = [e for e in enriched if (e.get("id") or 0) < int(before)]
        except (TypeError, ValueError):
            return fail("before_id 必须是整数", "bad_request")
    limit = payload.get("limit") or 100
    limit = int(limit)
    if limit > 200:
        limit = 200
    page = enriched[:limit]
    next_before = page[-1].get("id") if len(enriched) > limit and page else None
    return {"events": page, "next_before_id": next_before}


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
    # v2 §5.22：plan 请求只能经 start-plans 创建；enqueue 拒绝 kind=plan。
    if kind == "plan":
        return fail("规划请求请经规划面板提交（start-plans）", "bad_request")
    # v2 §5.22.4：confirm_plan 门控——ready+hold 任务确认计划哈希后才建
    # dispatch 请求并解除 hold；哈希不符 → conflict 原文上抛。
    if payload.get("confirm_plan"):
        if kind != "dispatch":
            return fail("confirm_plan 仅适用于 dispatch 请求", "bad_request")
        if not task.get("plan_execution_hold"):
            return fail("任务当前无待确认的计划", "conflict")
        expected = payload.get("expected_plan_sha256")
        if expected and expected != task.get("plan_sha256"):
            return fail("计划哈希冲突：计划已更新，请重新查看最新计划后再确认",
                        "conflict")
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
            ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
            ov["display_status"] = "已排队"
            if payload.get("confirm_plan"):
                ov["plan_execution_hold"] = False
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


# ---------------------------------------------------------------- §5.15.5 sessions

# 开发期开关：对该任务的会话读取失败一次（演练「连接中断」横幅与旧内容保留）
FLAKY_SESSION_TASK = os.environ.get("KANPILOT_FAKE_SESSION_FLAKY")

SESSION_POLL_SECONDS = 2

# T1（docs/plans/task-detail-readability-and-execution-evidence.md 性能门）：
# `KANPILOT_FAKE_LONG_SESSION=<task_id>` 时把该任务最新轮次的消息换成一条
# **固定消息数 / 固定长输出字节数**的合成长会话，供执行页阅读流畅性在同一
# 数据量、同一窗口、同一 2 秒轮询下做改前/改后度量（docs/ui-spec §4.5
# 「无变化轮询不重排（T1）」）。不设置该变量时夹具行为与既有完全一致。
LONG_SESSION_TASK = os.environ.get("KANPILOT_FAKE_LONG_SESSION")
LONG_SESSION_COUNT = int(os.environ.get("KANPILOT_FAKE_LONG_SESSION_COUNT") or 400)
LONG_SESSION_BYTES = int(os.environ.get("KANPILOT_FAKE_LONG_SESSION_BYTES") or 4096)


def long_session_messages(count=None, output_bytes=None):
    """T1 合成长会话：count 条消息，部分为固定字节数的长工具输出。

    长输出逐行填充到接近 output_bytes（ASCII，字节数与字符数一致），
    消息类型循环覆盖 assistant/tool_call/tool_result/command_output，
    保证折叠、搜索、逐条展开与本项目既有夹具形状一致。
    """
    count = count or LONG_SESSION_COUNT
    output_bytes = output_bytes or LONG_SESSION_BYTES
    line = "0123456789abcdef tool output chunk payload\n"
    long_output = line * max(1, output_bytes // len(line))
    types = ["assistant", "tool_call", "tool_result", "command_output"]
    messages = []
    for seq in range(1, count + 1):
        mtype = "system" if seq == 1 else types[seq % len(types)]
        tool = None
        if seq == 1:
            content = "执行会话已创建（fake long session）"
        elif mtype == "tool_call":
            content = "run_command"
            tool = {"name": "Bash",
                    "input": "python3 scripts/step_%04d.py" % seq,
                    "output": None, "exit_code": None}
        elif mtype in ("tool_result", "command_output"):
            content = long_output
            tool = {"name": "Bash",
                    "input": "python3 scripts/step_%04d.py" % seq,
                    "output": long_output, "exit_code": 0}
        else:
            content = "步骤 %04d：已完成本段处理，继续下一段。" % seq
        messages.append({"id": "m%05d" % seq, "seq": seq, "type": mtype,
                         "content": content, "revision": 1,
                         "source_at": past(minutes=max(0, count - seq)),
                         "tool": tool})
    return messages


def _msg(seq, mtype, content, tool=None, minutes=3, revision=1):
    return {"id": "m%03d" % seq, "seq": seq, "type": mtype,
            "content": content, "revision": revision,
            "source_at": past(minutes=minutes), "tool": tool}


def base_sessions():
    """§5.15.5 会话夹具：{"<task_id>/<run_id>": {"session", "connection", "messages"}}。

    覆盖：多执行轮次（历史轮次切换）、user/assistant/tool_call/tool_result/
    command_output/system 六类消息、工具折叠、增量游标、ok/degraded/unavailable
    三种连接状态、消息修订（id+revision 更新）。
    """
    music_current = [
        _msg(1, "system", "执行会话已创建（pi · CloseAI/deepseek-v4-flash · max）", minutes=200),
        _msg(2, "user",
             "任务标识：task-music-006/run-music-006-b2。读取并完整执行所附计划，"
             "保留现有改动，完成验证并报告结果。", minutes=198),
        _msg(3, "assistant", "我先读取计划文件，并列出当前播放列表。", minutes=190),
        _msg(4, "tool_call", "read_file", tool={"name": "read_file",
             "input": "~/Documents/Code/Plan/Music/task-music-006_20250917.md",
             "output": None, "exit_code": None}, minutes=188),
        _msg(5, "tool_result", "计划读取成功：共 18 个播放列表待比对，含 3 个自定义排序。",
             tool={"name": "read_file", "input": "plan.md",
                   "output": "# 计划：把本地歌单同步到 Kaneo 播放列表\n## 步骤\n1. 拉取本地歌单\n2. 拉取 Kaneo 播放列表\n3. 按标题匹配并保留自定义排序\n4. 回报结果",
                   "exit_code": 0}, minutes=187),
        _msg(6, "command_output",
             "$ python3 scripts/sync_playlists.py --dry-run\n"
             "[1/18] 通勤歌单      matched\n[2/18] 深夜钢琴      matched\n"
             "[3/18] 健身节奏      renamed -> 健身节奏 2025\n[4/18] 收藏红心      matched\n"
             "dry-run 完成：18 个中 16 个可直接同步，2 个需确认",
             minutes=120),
        _msg(7, "assistant", "已完成 12/18 个播放列表的比对，继续处理剩余 6 个。", minutes=45),
        _msg(8, "user", "继续，注意保留用户自定义排序。", minutes=30),
        _msg(9, "assistant", "好的，合并时我会保留现有顺序，只追加缺失条目。", minutes=12),
    ]
    music_history = [
        _msg(1, "system", "执行会话已创建（上一轮派发，已结束）", minutes=3000),
        _msg(2, "user", "任务标识：task-music-006/run-music-006-a1。", minutes=2998),
        _msg(3, "assistant",
             "上一轮因 Kaneo 列状态不符中断，未做任何写操作。", minutes=2990),
    ]
    comic = [
        _msg(1, "system", "执行会话已创建（pi · CloseAI/deepseek-v4-flash）", minutes=600),
        _msg(2, "user", "任务标识：task-comic-007/run-comic-007-c3。修复章节图片懒加载失败。", minutes=598),
        _msg(3, "tool_call", "run_tests", tool={"name": "run_tests",
             "input": "pytest tests/test_lazy_load.py -q", "output": None,
             "exit_code": None}, minutes=90),
        _msg(4, "tool_result", "前 3 章图片已正常；第 4 章 IntersectionObserver 未触发。",
             tool={"name": "run_tests", "input": "pytest tests/test_lazy_load.py -q",
                   "output": "3 passed, 1 failed\nFAILED test_lazy_load.py::test_chapter4_observer",
                   "exit_code": 1}, minutes=88),
        _msg(5, "assistant", "回归测试中，前 3 章图片已正常，正在排查第 4 章观察器。", minutes=40),
    ]
    study = [
        _msg(1, "system", "执行会话已创建（pi · CloseAI/deepseek-v4-flash）", minutes=2000),
        _msg(2, "user", "任务标识：task-study-017/run-study-017-j2。为错题本增加标签过滤。", minutes=1998),
        _msg(3, "tool_call", "grep", tool={"name": "grep",
             "input": "src/ -n \"tagFilter\"", "output": None, "exit_code": None}, minutes=1980),
        _msg(4, "tool_result", "找到 2 处过滤逻辑，均未处理空标签。",
             tool={"name": "grep", "input": "src/ -n \"tagFilter\"",
                   "output": "src/TagFilter.tsx:12\nsrc/TagFilter.tsx:47",
                   "exit_code": 0}, minutes=1979),
        _msg(5, "command_output",
             "$ npm run build\nvite v6 build ✓ 42 modules transformed\ndist/assets/index-4f2a.js  112.4 kB", minutes=1970),
        _msg(6, "assistant", "实现完成，空标签走全部题目兜底。", minutes=1960),
    ]
    fb_lost = [
        _msg(1, "system", "执行会话已创建（pi · CloseAI/deepseek-v4-flash）", minutes=3000),
        _msg(2, "user", "任务标识：task-fb-011/run-fb-011-g7。按版本聚合崩溃报告。", minutes=2998),
        _msg(3, "command_output", "$ python3 aggregate.py --group-by version\n处理 1,284 条崩溃记录", minutes=2900),
        _msg(4, "assistant", "聚类完成 80%，随后监控端失联。", minutes=2800),
    ]
    lib_archived = [
        _msg(1, "system", "执行会话已创建（pi · CloseAI/deepseek-v4-flash）", minutes=20000),
        _msg(2, "user", "任务标识：task-lib-022/run-lib-022-n3。为借阅记录增加逾期提醒模板。", minutes=19998),
        _msg(3, "assistant", "模板与邮件样式已按评审结论调整完成。", minutes=19900),
    ]
    lib_review = [
        _msg(1, "system", "执行会话已创建（pi · CloseAI/deepseek-v4-flash）", minutes=5000),
        _msg(2, "user", "任务标识：task-lib-008/run-lib-008-d4。导入 Zotero 书目并去重。", minutes=4998),
        _msg(3, "command_output", "$ python3 import_zotero.py --dedupe\n导入 1,204 条，去重 87 条", minutes=4900),
        _msg(4, "assistant", "执行端报告完成，等待评审。", minutes=4800),
    ]
    fb_pdf = [
        _msg(1, "system", "执行会话已创建（pi · CloseAI/deepseek-v4-flash）", minutes=800),
        _msg(2, "user", "任务标识：task-fb-020/run-fb-020-m4。导出月度 PDF 摘要。", minutes=798),
        _msg(3, "assistant", "正在汇总 9 月反馈并渲染 PDF。", minutes=700),
    ]

    def entry(task_id, run_id, state, connection_state, messages, detail=None,
              save_state="active"):
        session = {
            "task_id": task_id, "run_id": run_id,
            "session_marker": "%s/%s" % (task_id, run_id),
            "session_id": "pi-sess-%s" % run_id[-4:],
            "session_title": "pi: %s" % task_id,
            "state": state,
            "started_at": past(minutes=2400),
            "updated_at": past(minutes=5),
            "last_checked_at": past(minutes=2),
            "message_count": len(messages),
            # §5.16.3 + v2 §5.20：会话保存状态与可用动作。
            "save_state": save_state,
            "saved_at": past(hours=3) if save_state != "active" else None,
            "backend": "pi",
            "run_status": "finished",
            "allowed_actions": {
                "active": ["trash-session"],
                "trashed": ["restore-session", "purge-session"],
                "purged": [],
            }.get(save_state, ["trash-session"]),
            "read_only": save_state == "purged",
        }
        connection = {"state": connection_state, "detail": detail,
                      "poll_seconds": SESSION_POLL_SECONDS}
        return {"session": session, "connection": connection, "messages": messages}

    return {
        "task-music-006/run-music-006-b2": entry(
            "task-music-006", "run-music-006-b2", "associated", "ok", music_current),
        "task-music-006/run-music-006-a1": entry(
            "task-music-006", "run-music-006-a1", "associated", "ok", music_history),
        "task-comic-007/run-comic-007-c3": entry(
            "task-comic-007", "run-comic-007-c3", "associated", "degraded", comic,
            detail="订阅接口不可用，按 2 秒增量读取"),
        "task-study-017/run-study-017-j2": entry(
            "task-study-017", "run-study-017-j2", "associated", "ok", study),
        "task-fb-011/run-fb-011-g7": entry(
            "task-fb-011", "run-fb-011-g7", "associated", "unavailable", fb_lost,
            detail="当前 pi 版本接口不兼容，仅展示已读内容"),
        "task-lib-022/run-lib-022-n3": entry(
            "task-lib-022", "run-lib-022-n3", "associated", "ok", lib_archived),
        "task-lib-008/run-lib-008-d4": entry(
            "task-lib-008", "run-lib-008-d4", "associated", "ok", lib_review),
        "task-fb-020/run-fb-020-m4": entry(
            "task-fb-020", "run-fb-020-m4", "associated", "degraded", fb_pdf,
            detail="订阅接口不可用，按 2 秒增量读取", save_state="trashed"),
    }


def session_read_fails(state, task_id):
    """开发期开关：KANPILOT_FAKE_SESSION_FLAKY 命中时首次读取失败一次。"""
    if not FLAKY_SESSION_TASK or FLAKY_SESSION_TASK != task_id:
        return False
    failed = state.setdefault("session_failed", [])
    if task_id in failed:
        return False
    failed.append(task_id)
    save_state(state)
    return True


def task_run_id(state, task_id):
    """任务当前/最近一个带 session_marker 的执行轮次。"""
    for task in merged_tasks(state):
        if task["task_id"] == task_id:
            marker = task.get("session_marker")
            if marker and "/" in marker:
                return marker.split("/", 1)[1]
            return task.get("run_id")
    return None


def latest_session_run(state, task_id):
    prefix = task_id + "/"
    runs = [k[len(prefix):] for k in base_sessions() if k.startswith(prefix)]
    if not runs:
        return None
    return sorted(runs)[-1]


def pending_session(task_id, run_id):
    return {
        "task_id": task_id, "run_id": run_id,
        "session_marker": "%s/%s" % (task_id, run_id) if run_id else None,
        "session_id": None, "session_title": None, "state": "pending",
        "started_at": None, "updated_at": None,
        "last_checked_at": past(minutes=2), "message_count": 0,
        "save_state": "active", "saved_at": None,
        "allowed_actions": [], "read_only": False,
    }


def apply_session_save_state(state, session):
    """§5.16.3 / v2 §5.20：session_save_states[marker] 覆盖 save_state，
    并同步派生 allowed_actions 与 read_only（purged=只读审计）。"""
    if not session:
        return session
    session = dict(session)
    marker = session.get("session_marker") or (
        "%s/%s" % (session.get("task_id"), session.get("run_id")))
    saved = (state.get("session_save_states") or {}).get(marker) or {}
    save_state = saved.get("save_state") or session.get("save_state") or "active"
    session["save_state"] = save_state
    if saved.get("saved_at"):
        session["saved_at"] = saved["saved_at"]
    session["allowed_actions"] = {
        "active": ["trash-session"],
        "trashed": ["restore-session", "purge-session"],
        "purged": [],
    }.get(save_state, ["trash-session"])
    session["read_only"] = save_state == "purged"
    return session


def long_session_override(task_id, messages):
    """T1 度量夹具：`KANPILOT_FAKE_LONG_SESSION` 命中时替换该任务消息。"""
    if LONG_SESSION_TASK and task_id == LONG_SESSION_TASK:
        return long_session_messages()
    return messages


def resolve_session(state, task_id, run_id=None):
    """返回 (SessionInfo, connection, messages)；无记录 → pending。"""
    run_id = run_id or task_run_id(state, task_id) or latest_session_run(state, task_id)
    entry = base_sessions().get("%s/%s" % (task_id, run_id)) if run_id else None
    if entry is None:
        return (apply_session_save_state(state, pending_session(task_id, run_id)),
                {"state": "ok", "detail": None, "poll_seconds": SESSION_POLL_SECONDS},
                [])
    messages = long_session_override(task_id, entry["messages"])
    session = dict(entry["session"], message_count=len(messages))
    return (apply_session_save_state(state, session), entry["connection"], messages)


def cmd_list_sessions(state, payload):
    task_id = payload.get("task_id")
    # v2 §5.16.1：save_state ∈ active|trashed|all|purged（缺省 active）。
    save_filter = payload.get("save_state") or "active"
    if save_filter not in ("active", "trashed", "all", "purged"):
        return fail("save_state 只能是 active|trashed|all|purged", "bad_request")

    def _match(s):
        sv = s.get("save_state") or "active"
        if save_filter == "all":
            return sv in ("active", "trashed")
        return sv == save_filter

    sessions = []
    if task_id:
        tasks = {t["task_id"]: t for t in merged_tasks(state)}
        if task_id not in tasks:
            return fail("任务不存在：%s" % task_id, "not_found")
        prefix = task_id + "/"
        runs = sorted(k[len(prefix):] for k in base_sessions() if k.startswith(prefix))
        if runs:
            for run in runs:
                sessions.append(apply_session_save_state(
                    state, base_sessions()["%s/%s" % (task_id, run)]["session"]))
        else:
            run = task_run_id(state, task_id)
            if run:
                sessions.append(apply_session_save_state(
                    state, pending_session(task_id, run)))
        return {"sessions": [s for s in sessions if _match(s)]}
    # 缺省：全部可关联会话，active 任务优先
    rows = []
    for key, entry in base_sessions().items():
        session = apply_session_save_state(state, entry["session"])
        task_id = session["task_id"]
        task = next((t for t in merged_tasks(state) if t["task_id"] == task_id), None)
        rows.append((1 if (task or {}).get("save_state") == "active" else 0,
                     session.get("updated_at") or "", session))
    for task in merged_tasks(state):
        if task.get("save_state") != "active":
            continue
        marker = task.get("session_marker")
        if not marker or marker in base_sessions():
            continue
        rows.append((1, "", apply_session_save_state(
            state, pending_session(task["task_id"], marker.split("/", 1)[1]))))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return {"sessions": [r[2] for r in rows if _match(r[2])]}


def cmd_get_session(state, payload):
    task_id = payload.get("task_id")
    run_id = payload.get("run_id")
    if not task_id or not run_id:
        return fail("缺少必填字段 task_id/run_id", "bad_request")
    session, connection, _ = resolve_session(state, task_id, run_id)
    return {"session": session, "connection": connection}


def cmd_session_messages(state, payload):
    task_id = payload.get("task_id")
    if not task_id:
        return fail("缺少必填字段 task_id", "bad_request")
    if session_read_fails(state, task_id):
        return fail("执行会话读取失败：连接中断（fake 模拟）", "remote_unavailable")
    run_id = payload.get("run_id") or task_run_id(state, task_id) \
        or latest_session_run(state, task_id)
    session, connection, messages = resolve_session(state, task_id, run_id)
    ordered = sorted(messages, key=lambda m: m.get("seq") or 0)
    cursor = payload.get("cursor")
    try:
        after = int(cursor) if cursor not in (None, "") else 0
    except (TypeError, ValueError):
        return fail("cursor 必须是消息 seq 整数", "bad_request")
    limit = int(payload.get("limit") or 100)
    page = [m for m in ordered if (m.get("seq") or 0) > after]
    incremental = after > 0
    if incremental and page and page[-1] is ordered[-1]:
        # 增量读取时把最新消息升版一次：演练 id+revision 去重/更新
        newest = dict(page[-1])
        newest["revision"] = (newest.get("revision") or 1) + 1
        newest["content"] = (newest.get("content") or "") + "（已修订）"
        page = page[:-1] + [newest]
    page = page[:limit]
    has_more = len([m for m in ordered if (m.get("seq") or 0) > after]) > len(page)
    next_cursor = str((page[-1].get("seq") or 0)) if page else (cursor or None)
    return {"session": session, "messages": page,
            "next_cursor": next_cursor, "has_more": has_more,
            "connection": connection}


# ---------------------------------------------------------------- §5.15.2 save state

def _move_out(state, payload, target, event_kind):
    tid = payload.get("task_id")
    if not tid:
        return fail("缺少必填字段 task_id", "bad_request")
    expected_save_state = payload.get("expected_save_state")
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    task = tasks.get(tid)
    if not task:
        return fail("任务不存在：%s" % tid, "not_found")
    current = task.get("save_state") or "active"
    if current == target:
        return {"ok": True, "deduplicated": True, "task_id": tid,
                "save_state": target, "task_status": task["status"]}
    if expected_save_state is not None and current != expected_save_state:
        return fail("任务保存状态冲突（期望 %s，实际为 %s）" % (expected_save_state, current), "conflict")
    reason = move_out_blocked_reason(task, merged_requests(state))
    if reason:
        return fail(reason, "conflict")

    def apply(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        ov["save_state"] = target
        ov["saved_at"] = iso(utcnow())
        # §5.15.2：同事务取消该任务 queued 请求
        for req in merged_requests(s):
            if req.get("task_id") == tid and req.get("status") == "queued":
                s.setdefault("request_overrides", {}).setdefault(
                    req["request_id"], {})["status"] = "cancelled"
        append_event(s, event_kind, task_id=tid, actor="user",
                     detail={"save_state": target})

    mutate(state, apply)
    return {"ok": True, "task_id": tid, "save_state": target,
            "task_status": task["status"]}


def cmd_archive_task(state, payload):
    return _move_out(state, payload, "archived", "task_archived")


def cmd_trash_task(state, payload):
    return _move_out(state, payload, "trashed", "task_trashed")


def cmd_restore_task(state, payload):
    tid = payload.get("task_id")
    if not tid:
        return fail("缺少必填字段 task_id", "bad_request")
    resume_auto = bool(payload.get("resume_auto"))
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    task = tasks.get(tid)
    if not task:
        return fail("任务不存在：%s" % tid, "not_found")

    def apply(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        ov["save_state"] = "active"
        ov["saved_at"] = None
        ov["auto_advance_paused"] = 0 if resume_auto else 1
        append_event(s, "task_restored", task_id=tid, actor="user",
                     detail={"resume_auto": resume_auto})

    mutate(state, apply)
    warnings = [] if resume_auto else \
        ["已恢复并默认暂停自动推进，需手动「恢复自动推进」"]
    return {"ok": True, "task_id": tid, "save_state": "active",
            "auto_advance_paused": 0 if resume_auto else 1,
            "task_status": task["status"], "warnings": warnings}


# ------------------------------------------------- §5.17/§5.19 exec config + executor（v1.6，pi）

EXEC_CONFIG_DEFAULTS = {
    "provider": "CloseAI",
    "model": "deepseek-v4-flash",
    "reasoning_effort": "max",
    "agent_preset": "default",
}


def cmd_exec_capabilities(state, payload):
    """exec-capabilities {}：v1.6 pi 形态（pi/providers/presets/checks，
    不再有 dsh/permission 字段）。"""
    return {
        "ok": True,
        "pi": {"path": os.path.expanduser("~/.npm-global/bin/pi"),
               "version": "0.85.1",
               "home": os.path.expanduser("~/.pi")},
        "providers": [
            {"id": "CloseAI", "display_name": "CloseAI", "models": [
                {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash",
                 "reasoning_efforts": ["low", "medium", "high", "max"]},
                {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro",
                 "reasoning_efforts": ["medium", "high", "max"]},
            ]},
        ],
        "presets": [
            {"id": "default", "name": "default", "description": None,
             "broken": None, "path": APP_SUPPORT + "/agents/default.md"},
            {"id": "strict", "name": "strict", "description": "严格评审预设",
             "broken": None, "path": APP_SUPPORT + "/agents/strict.md"},
            {"id": "legacy", "name": "legacy", "description": None,
             "broken": "agent.md 引用了不存在的工具", "path": None},
        ],
        "checks": [
            {"name": "pi_binary", "ok": True, "detail": "0.85.1"},
            {"name": "models", "ok": True,
             "detail": "~/.pi/agent/models.json，1 个 provider"},
            {"name": "agent_preset", "ok": True,
             "detail": "2 个 agent.md（" + APP_SUPPORT + "/agents）"},
            {"name": "credential", "ok": True, "detail": "凭据就绪"},
        ],
    }


def cmd_get_exec_config(state, payload):
    return {"config": state.get("exec_config"),
            "defaults": dict(EXEC_CONFIG_DEFAULTS)}


def cmd_set_exec_config(state, payload):
    fields = [k for k in EXEC_CONFIG_DEFAULTS if k in payload]
    if not fields:
        return fail("set-exec-config 至少提供一个字段"
                    "（provider / model / reasoning_effort / agent_preset）",
                    "bad_request")
    caps = cmd_exec_capabilities(state, payload)
    candidate = dict(state.get("exec_config") or EXEC_CONFIG_DEFAULTS)
    for k in fields:
        candidate[k] = payload[k]
    provider = next(
        (p for p in caps["providers"] if p["id"] == candidate["provider"]),
        None)
    if provider is None:
        return fail("执行配置校验失败：provider 不存在：%s"
                    % candidate["provider"], "bad_request")
    model = next(
        (m for m in provider["models"] if m["id"] == candidate["model"]),
        None)
    if model is None:
        return fail("执行配置校验失败：模型不存在：%s" % candidate["model"],
                    "bad_request")
    if candidate["reasoning_effort"] not in model["reasoning_efforts"]:
        return fail("执行配置校验失败：模型 %s 不支持思考等级 %s"
                    % (model["id"], candidate["reasoning_effort"]),
                    "bad_request")
    preset = next(
        (p for p in caps["presets"] if p["id"] == candidate["agent_preset"]),
        None)
    if preset is None:
        return fail("执行配置校验失败：预设不存在：%s"
                    % candidate["agent_preset"], "bad_request")
    if preset.get("broken"):
        return fail("执行配置校验失败：预设不可用：%s（%s）"
                    % (preset["id"], preset["broken"]), "bad_request")
    candidate["updated_at"] = iso(utcnow())

    def apply(s):
        s["exec_config"] = dict(candidate)
        append_event(s, "exec_config_updated", detail={
            k: candidate[k] for k in EXEC_CONFIG_DEFAULTS})
    mutate(state, apply)
    return {"config": dict(candidate)}


def cmd_executor_status(state, payload):
    """executor-status {}：罐头运行记录（不探测 pid，running 恒 None）。"""
    runs = state.get("executor_runs")
    if not isinstance(runs, dict):
        runs = {
            "plan": {"pid": 81234, "started_at": past(minutes=90),
                     "log": APP_SUPPORT + "/executor-logs/plan-fake.log",
                     "exit_code": 0, "finished_at": past(minutes=80)},
            "dispatch": {"pid": 82345, "started_at": past(minutes=40),
                         "log": APP_SUPPORT + "/executor-logs/dispatch-fake.log",
                         "exit_code": 1, "finished_at": past(minutes=35)},
        }
    return {"stages": {
        stage: {"running": None, "last": runs.get(stage)}
        for stage in ("plan", "dispatch", "monitor", "review")}}


# ------------------------------------------------- v2 §5.20 purge / §5.21 get-request / §5.22 规划链路

def _session_state_change(state, payload, target, event_kind):
    """trash-session / restore-session / purge-session 共用：CAS + 状态写回。"""
    tid = payload.get("task_id")
    run_id = payload.get("run_id")
    if not tid or not run_id:
        return fail("缺少必填字段 task_id/run_id", "bad_request")
    marker = "%s/%s" % (tid, run_id)
    session, _, _ = resolve_session(state, tid, run_id)
    if session is None:
        return fail("会话不存在：%s" % marker, "not_found")
    current = session.get("save_state") or "active"
    if current == target:
        return {"ok": True, "deduplicated": True, "task_id": tid,
                "run_id": run_id, "save_state": target, "session": session}
    expected = payload.get("expected_save_state")
    if expected is not None and current != expected:
        return fail("会话保存状态冲突（期望 %s，实际为 %s）" % (expected, current),
                    "conflict")
    if target in ("trashed", "purged") and current == "active" \
            and target == "purged":
        return fail("仅回收站中的会话可永久移除", "conflict")

    def apply(s):
        s.setdefault("session_save_states", {})[marker] = {
            "save_state": target, "saved_at": iso(utcnow())}
        append_event(s, event_kind, task_id=tid, actor="user",
                     detail={"run_id": run_id, "save_state": target})
    mutate(state, apply)
    session["save_state"] = target
    session["read_only"] = target == "purged"
    return {"ok": True, "task_id": tid, "run_id": run_id,
            "save_state": target, "session": session}


def cmd_trash_session(state, payload):
    return _session_state_change(state, payload, "trashed", "session_trashed")


def cmd_restore_session(state, payload):
    return _session_state_change(state, payload, "active", "session_restored")


def cmd_purge_session(state, payload):
    return _session_state_change(state, payload, "purged", "session_purged")


def cmd_purge_task(state, payload):
    tid = payload.get("task_id")
    if not tid:
        return fail("缺少必填字段 task_id", "bad_request")
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    task = tasks.get(tid)
    if not task:
        return fail("任务不存在：%s" % tid, "not_found")
    current = task.get("save_state") or "active"
    if current == "purged":
        return {"ok": True, "deduplicated": True, "task_id": tid,
                "save_state": "purged"}
    if current != "trashed":
        return fail("仅回收站中的任务可永久移除（当前为 %s）" % current,
                    "conflict")
    expected = payload.get("expected_save_state")
    if expected is not None and expected != "trashed":
        return fail("任务保存状态冲突（期望 %s，实际为 %s）" % (expected, current),
                    "conflict")

    def apply(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        ov["save_state"] = "purged"
        ov["saved_at"] = iso(utcnow())
        append_event(s, "task_purged", task_id=tid, actor="user",
                     detail={"save_state": "purged"})
    mutate(state, apply)
    return {"ok": True, "task_id": tid, "save_state": "purged"}


_REQUEST_ACTIONS = {
    "queued": ["view", "set-priority"],
    "claimed": ["view"],
    "succeeded": ["view"],
    "cancelled": ["view"],
}


def cmd_get_request(state, payload):
    """§5.21.1：请求字段 + 关联任务摘要 + attempts + 动作词表 + 关注态。"""
    rid = payload.get("request_id")
    if not rid:
        return fail("缺少必填字段 request_id", "bad_request")
    req = next((r for r in merged_requests(state) if r["request_id"] == rid), None)
    if req is None:
        return fail("请求不存在：%s" % rid, "not_found")
    req = dict(req)
    req["display_status"] = "lost" if request_is_lost(req) else req["status"]
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    task = tasks.get(req.get("task_id") or "")
    task_summary = None
    if task is not None and task.get("save_state") != "purged":
        task_summary = {k: task.get(k) for k in SUMMARY_KEYS}

    status = req["status"]
    actions = list(_REQUEST_ACTIONS.get(status, []))
    if status == "claimed" and request_is_lost(req):
        actions.append("release_lost")
    if status == "failed":
        actions += ["retry", "replan"]
    if req.get("kind") == "plan-scan" and req.get("task_id") is None:
        actions = ["view", "fetch_todos"]
    if status == "queued" and req.get("kind") == "plan" \
            and not req.get("manual_authorized"):
        actions.append("prepare_plan")
    if status == "queued" and req.get("kind") == "plan" \
            and req.get("manual_authorized"):
        pass  # 已授权排队中：仅 view/set-priority

    if status in ("queued", "claimed") or request_is_lost(req):
        attention = "actionable"
    elif status == "failed":
        attention = "historical" if task is None or task.get("save_state") != "active" \
            else "actionable"
    else:
        attention = "informational"
    read_only = status in ("succeeded", "cancelled") \
        or (task is not None and task.get("save_state") == "purged") \
        or (status == "failed" and not actions)

    attempts = []
    if req.get("run_id"):
        result = req.get("result") or {}
        attempts.append({
            "request_id": rid, "run_id": req.get("run_id"),
            "stage": req.get("stage"), "kind": req.get("kind"),
            "started_at": req.get("claimed_at"),
            "finished_at": req.get("finished_at"),
            "outcome": result.get("outcome") if isinstance(result, dict) else None,
            "send_state": (task or {}).get("send_state"),
            "note": (result.get("reason") if isinstance(result, dict) else None),
            "evidence": {"note": "fake 证据"},
        })
    log_path = (APP_SUPPORT + "/executor-logs/%s.log" % rid) if req.get("run_id") else None
    return {"request": req, "task": task_summary, "attempts": attempts,
            "allowed_actions": actions,
            "blocked_reason": (task or {}).get("blocked_reason"),
            "read_only": read_only, "log_path": log_path,
            "attention_state": attention, "payload": req.get("payload")}


def cmd_fetch_todos(state, payload):
    """§5.22.1：只读枚举 To Do（含一个 selectable=false 与聚焦项）。"""
    items = []
    for t in merged_tasks(state):
        if t.get("kaneo_status") != "to-do" or t.get("save_state") != "active":
            continue
        items.append({
            "task_id": t["task_id"], "title": t.get("title"),
            "project_id": t.get("project_id"), "project_name": t.get("project_name"),
            "requirements_sha256": t.get("requirements_sha256"),
            "source_sha256": t.get("source_sha256"),
            "kaneo_status": t.get("kaneo_status"),
            "repository": "~/Documents/Code/Project/Personal Project/%s"
                % (t.get("project_name") or ""),
            "local_status": {
                "needs_plan": "待规划", "planning": "规划中", "ready": "已有计划",
            }.get(t["status"], t["status"]),
            "local_save_state": t.get("save_state"),
            "attachments": [
                {"remote_id": "att-%s-1" % t["task_id"][-3:],
                 "name": "requirements.txt", "url": "https://kaneo.example.com/a.txt",
                 "media_type": "text/plain", "size": 1280},
            ] if t["task_id"] == "task-music-016" else [],
            "selectable": t["status"] in ("needs_plan",),
            "blocked_reason": None if t["status"] == "needs_plan"
                else "本地已有任务（状态 %s），如需重新规划请先在任务详情确认" % t["status"],
        })
    items.append({
        "task_id": "todo-unmapped-001", "title": "未映射项目的 To Do（fake）",
        "project_id": "proj-unmapped", "project_name": "Unmapped",
        "requirements_sha256": sha("req-unmapped"), "source_sha256": sha("src-unmapped"),
        "kaneo_status": "to-do", "repository": None, "local_status": None,
        "local_save_state": None, "attachments": [],
        "selectable": False,
        "blocked_reason": "项目未在白名单/仓库映射中，无法规划",
    })
    return {"fetched_at": iso(utcnow()), "items": items}


def cmd_prepare_plan_input(state, payload):
    """§5.22.1：核验需求哈希 → 材料目录 manifest；task-music-016 附件
    默认带 issue（可经 excluded_remote_ids 排除）。"""
    tid = payload.get("task_id")
    if not tid:
        return fail("缺少必填字段 task_id", "bad_request")
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    todos = {i["task_id"]: i
             for i in cmd_fetch_todos(state, {}).get("items", [])}
    todo = todos.get(tid)
    if todo is None and tid not in tasks:
        return fail("任务不存在：%s" % tid, "not_found")
    req_sha = (todo or {}).get("requirements_sha256") \
        or (tasks.get(tid) or {}).get("requirements_sha256")
    expected = payload.get("expected_requirements_sha256")
    if expected and req_sha and expected != req_sha:
        return fail("需求已变更（requirements_sha256 不符），请重新获取 To Do",
                    "conflict")
    excluded = set(payload.get("excluded_remote_ids") or [])
    input_id = "pin-" + (tid or "x")
    files = [{
        "name": "source-snapshot.json", "source": "snapshot",
        "media_type": "application/json",
        "sha256": sha("snap-" + tid), "size": 2048,
    }]
    for path in payload.get("local_paths") or []:
        files.append({"name": path.split("/")[-1], "source": "local",
                      "media_type": "text/plain",
                      "sha256": sha("local-" + path), "size": 512})
    if payload.get("supplement"):
        files.append({"name": "supplement.md", "source": "supplement",
                      "media_type": "text/markdown",
                      "sha256": sha("supp-" + tid), "size": 256})
    issues = []
    for att in (todo or {}).get("attachments") or []:
        rid_ = att.get("remote_id") or att.get("url")
        if rid_ and rid_ not in excluded and att.get("url") not in excluded:
            issues.append({"remote_id": att.get("remote_id"),
                           "url": att.get("url"),
                           "issue": "附件下载失败（fake 模拟 403）"})
    can_start = not issues

    def apply(s):
        s.setdefault("plan_inputs", {})[input_id] = {
            "task_id": tid, "files": files,
            "input_sha256": sha("input-" + tid + str(len(files))),
        }
        append_event(s, "plan_input_prepared", task_id=tid, actor="user",
                     detail={"input_id": input_id, "can_start": can_start})
    mutate(state, apply)
    return {"input_id": input_id,
            "input_sha256": sha("input-" + tid + str(len(files))),
            "files": files, "issues": issues, "can_start": can_start,
            "requirements_sha256": req_sha,
            "source_sha256": (todo or {}).get("source_sha256")
                or (tasks.get(tid) or {}).get("source_sha256"),
            "input_dir": APP_SUPPORT + "/planner_inputs/" + input_id}


def cmd_start_plans(state, payload):
    """§5.22.2：逐项提交；已授权 queued 幂等、未授权复用写回、新任务新建。"""
    items = payload.get("items") or []
    if not items:
        return fail("缺少必填字段 items", "bad_request")
    tasks = {t["task_id"]: t for t in merged_tasks(state)}
    requests = {r["request_id"]: r for r in merged_requests(state)}
    inputs = state.get("plan_inputs") or {}
    results = []
    for item in items:
        input_id = item.get("input_id")
        entry = inputs.get(input_id)
        if entry is None:
            results.append({"task_id": None, "code": "bad_request",
                            "error": "材料不存在：%s" % input_id})
            continue
        tid = entry["task_id"]
        if item.get("input_sha256") and item["input_sha256"] != entry["input_sha256"]:
            results.append({"task_id": tid, "code": "conflict",
                            "error": "材料哈希不符，请重新准备"})
            continue
        task = tasks.get(tid)
        if task is None or task.get("save_state") not in ("active",):
            results.append({"task_id": tid, "code": "conflict",
                            "error": "任务不处于活跃状态"})
            continue
        existing = next((r for r in requests.values()
                         if r.get("idempotency_key") == "plan:%s" % tid
                         and r["status"] in ("queued", "claimed")), None)
        if existing is not None:
            if existing["status"] == "claimed":
                results.append({"task_id": tid, "code": "conflict",
                                "error": "规划请求已被执行端领取，不可覆盖"})
                continue
            if existing.get("manual_authorized"):
                same = (existing.get("payload") or {}).get("planner_input_sha256") \
                    == entry["input_sha256"]
                if same:
                    results.append({"task_id": tid,
                                    "request_id": existing["request_id"],
                                    "deduplicated": True, "reused": True})
                else:
                    results.append({"task_id": tid, "code": "conflict",
                                    "error": "已有授权请求但材料不同（不偷偷替换）"})
                continue
            # 旧未授权请求：写回已确认材料，复用同一 request。
            def apply_reuse(s, rid=existing["request_id"], tid=tid, entry=entry):
                ov = s.setdefault("request_overrides", {}).setdefault(rid, {})
                ov["payload"] = {"manual_authorized": True,
                                 "input_id": entry and input_id,
                                 "planner_input_sha256": entry["input_sha256"],
                                 "confirmed_at": iso(utcnow())}
                ov["updated_at"] = iso(utcnow())
                append_event(s, "plan_started", task_id=tid, request_id=rid,
                             actor="user", detail={"input_id": input_id})
            mutate(state, apply_reuse)
            results.append({"task_id": tid,
                            "request_id": existing["request_id"],
                            "deduplicated": False, "reused": True})
            continue
        # 新建授权 plan 请求。
        req = {
            "request_id": "req-" + uuid.uuid4().hex[:8],
            "idempotency_key": "plan:%s" % tid, "task_id": tid,
            "stage": "plan", "kind": "plan", "status": "queued",
            "priority": 100, "requested_by": "user", "attempt_no": 0,
            "payload": {"manual_authorized": True, "input_id": input_id,
                        "planner_input_sha256": entry["input_sha256"],
                        "confirmed_at": iso(utcnow())},
            "created_at": iso(utcnow()), "updated_at": iso(utcnow()),
        }

        def apply_new(s, req=req, tid=tid):
            s.setdefault("extra_requests", []).append(req)
            append_event(s, "plan_started", task_id=tid,
                         request_id=req["request_id"], actor="user",
                         detail={"input_id": input_id})
        mutate(state, apply_new)
        requests[req["request_id"]] = req
        results.append({"task_id": tid, "request_id": req["request_id"],
                        "deduplicated": False})
    return {"results": results,
            "started": len([r for r in results if r.get("request_id")])}


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
    "archive-task": lambda s, p: cmd_archive_task(s, p),
    "trash-task": lambda s, p: cmd_trash_task(s, p),
    "restore-task": lambda s, p: cmd_restore_task(s, p),
    "list-sessions": lambda s, p: cmd_list_sessions(s, p),
    "get-session": lambda s, p: cmd_get_session(s, p),
    "session-messages": lambda s, p: cmd_session_messages(s, p),
    "get-exec-config": lambda s, p: cmd_get_exec_config(s, p),
    "set-exec-config": lambda s, p: cmd_set_exec_config(s, p),
    "exec-capabilities": lambda s, p: cmd_exec_capabilities(s, p),
    "executor-status": lambda s, p: cmd_executor_status(s, p),
    "purge-task": lambda s, p: cmd_purge_task(s, p),
    "purge-session": lambda s, p: cmd_purge_session(s, p),
    "trash-session": lambda s, p: cmd_trash_session(s, p),
    "restore-session": lambda s, p: cmd_restore_session(s, p),
    "get-request": lambda s, p: cmd_get_request(s, p),
    "fetch-todos": lambda s, p: cmd_fetch_todos(s, p),
    "prepare-plan-input": lambda s, p: cmd_prepare_plan_input(s, p),
    "start-plans": lambda s, p: cmd_start_plans(s, p),
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
