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
import hashlib
import time
import copy
import os
import sys
import tempfile
import uuid
import signal
from datetime import datetime, timedelta, timezone

STATE_PATH = os.environ.get(
    "KANPILOT_FAKE_STATE",
    os.path.join(tempfile.gettempdir(), "kanpilot-fake-state.json"),
)

PROJECTS = {
    "Music": "fixture-project-music",
    "Library": "fixture-project-library",
    "StudyTool": "fixture-project-studytool",
    "Comic": "fixture-project-comic",
    "Feedback": "fixture-project-feedback",
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


# 同一阻塞和 failed monitor 的精确时间关联；跨 CLI 调用保持稳定。
VERIFICATION_FIXTURE_BLOCKED_AT = "2026-10-09T07:56:27.000000+00:00"
VERIFICATION_FIXTURE_COMMANDS = [
    "dart format --output=none --set-exit-if-changed lib/core/providers.dart",
    "flutter analyze --no-pub lib/core/providers.dart",
    "flutter test --no-pub test/daily_recommend_shuffle_test.dart",
]
VERIFICATION_FIXTURE_PLAN = (
    "# 自动处理示例 · verification_unconfirmed\n\n"
    "> 隔离假数据：不会运行真实命令、模型或修改真实仓库。\n\n"
    "## 验证声明\n\n```verify\n" + "\n".join(VERIFICATION_FIXTURE_COMMANDS) + "\n```\n"
)
VERIFICATION_FIXTURE_PLAN_SHA = hashlib.sha256(VERIFICATION_FIXTURE_PLAN.encode("utf-8")).hexdigest()


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
        {"request_id": "req-monitor-auto-verification-unconfirmed",
         "idempotency_key": "monitor:auto-verification_unconfirmed:run-auto-verification-unconfirmed",
         "task_id": "auto-verification_unconfirmed", "stage": "monitor", "kind": "monitor",
         "status": "failed", "priority": 100, "requested_by": "scheduler",
         "run_id": "run-auto-verification-monitor", "claimed_by": "kaneo-monitor",
         "claimed_at": "2026-10-09T07:55:27.000000+00:00",
         "finished_at": VERIFICATION_FIXTURE_BLOCKED_AT, "attempt_no": 1,
         "payload": {"task_status_at_claim": "running"},
         "result": {"outcome": "needs_user",
                    "reason": "验证退出码尚未识别，等待重新核验现有记录"},
         "created_at": "2026-10-09T07:54:27.000000+00:00",
         "updated_at": VERIFICATION_FIXTURE_BLOCKED_AT},
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
    events.append({
        "id": len(events) + 1, "at": VERIFICATION_FIXTURE_BLOCKED_AT,
        "task_id": "auto-verification_unconfirmed",
        "request_id": "req-monitor-auto-verification-unconfirmed",
        "kind": "reported", "actor": "kaneo-monitor",
        "detail": {"outcome": "needs_user", "note": "验证证据尚未确认"},
    })
    return events


# ---------------------------------------------------------------- derive / merge

SUMMARY_KEYS = [
    "task_id", "project_id", "project_name", "title", "stage", "status",
    "phase", "condition", "attention",
    "display_status", "board_column", "lifecycle_column", "exception",
    "board_substatus", "last_checked_at",
    "review_summary", "last_progress", "blocked_reason", "next_claim_at",
    "active_request", "kaneo_status", "kaneo_checked_at", "priority",
    "plan_path", "created_at", "updated_at",
    "save_state", "saved_at", "auto_advance_paused",
    "plan_execution_hold", "execution_question",
    "move_out_eligibility", "automation", "delivery", "review_history", "parent_closeout", "task_relations", "release",
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
            or "评审结论为需修改，等待启动修复会话"
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
    tasks = {t["task_id"]: dict(t) for t in base_tasks() + automation_tasks()}
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
        task.setdefault("display_status", {
            "needs_plan": "待规划", "planning": "规划中", "ready": "就绪",
            "dispatching": "派发中", "prepared": "派发中", "submitting": "派发中",
            "running": "执行中", "in_review": "评审中", "needs_user": "需人工", "done": "已归档",
        }.get(task["status"]))
        derive_board(task, requests)
        task["phase"], task["condition"] = {
            "needs_plan": ("plan", "wanted"), "planning": ("plan", "drafting"),
            "ready": ("plan", "ready"), "dispatching": ("dispatch", "confirmed"),
            "prepared": ("dispatch", "submitting"), "submitting": ("dispatch", "submitting"),
            "running": ("observe", "running"), "in_review": ("review", "reviewing"),
            "needs_user": ("needs-you", "blocked"), "done": ("done", "merged"),
        }.get(task["status"], (None, None))
        delivery = task.get("delivery") or {}
        if task["status"] == "in_review" and (task.get("review_conclusion") == "pass" or delivery.get("pr_number") or delivery.get("pushed_sha")):
            task["phase"], task["condition"] = "delivery", "syncing"
        task["attention"] = None if state.get("attention_empty") else fake_attention_item(task, requests)
        repair = task.get("review_repair") or {}
        if task["status"] not in ("needs_user", "done") and repair.get("state") in ("held", "queued", "running", "awaiting_review"):
            task["lifecycle_column"] = "in_review"
            if repair["state"] != "held": task["board_column"] = "in_review"
            task["display_status"] = task["board_substatus"] = {
                "held": "待修复", "queued": "修复会话待启动", "running": "修复中", "awaiting_review": "修复后待复审"}[repair["state"]]
            task["phase"], task["condition"] = "review", "reviewing" if repair["state"] == "awaiting_review" else "repairing"

    for task in tasks.values():
        if task["status"] == "done":
            task["display_status"] = "已归档" if task.get("save_state") == "archived" else "已完成"
            delivery = task.get("delivery") or {}
            notice = delivery.get("auto_merge") or {}
            if (delivery.get("pr_state") == "MERGED" and delivery.get("merged_at")
                    and notice.get("state") == "merged" and delivery.get("head_sha")
                    and notice.get("head_sha") == delivery["head_sha"]):
                task["delivery"] = dict(delivery, auto_merge=dict(notice, reason="PR 已合并，任务已完成"))
    build_ai = (state.get("task_ai_action_fixture") or {}).get("kind") == "build_failure"
    if not build_ai:
        apply_task_ai_action_fixture(state, tasks)
    apply_parent_closeout_fixture(state, tasks)
    apply_release_fixture(state, tasks)
    if build_ai:
        apply_task_ai_action_fixture(state, tasks)
    apply_external_build_repair_fixture(state, tasks)
    for task in tasks.values():
        if task.get("_delivery_continuation"):
            task.update(display_status="待 PR 交付", phase="delivery", condition="syncing",
                        board_column="in_review", lifecycle_column="in_review",
                        exception=None, blocked_reason=None, attention=None)
            if task.get("release"):
                task["release"] = dict(task["release"], title="待 PR 交付",
                    reason="评审已通过；继续交付会复用已有成果，创建 PR，并按项目策略推进合并及构建发布。")
    return list(tasks.values())

def merged_requests(state):
    requests = {r["request_id"]: dict(r) for r in base_requests()}
    for code in ("branch_exists", "delivery_base_unknown"):
        rid = "req-plan-auto-" + code
        requests[rid] = {"request_id": rid, "task_id": "auto-" + code, "kind": "plan", "stage": "plan",
                         "status": "failed", "priority": 100, "attempt_no": 1, "requested_by": "scheduler",
                         "created_at": past(minutes=2), "updated_at": past(minutes=1)}
    for req in state.get("extra_requests", []):
        requests[req["request_id"]] = dict(req)
    for rid, patch in state.get("request_overrides", {}).items():
        if rid in requests:
            requests[rid].update(patch)
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


def fake_engine_fix_revert(task, requests):
    candidate = task.get("engine_fix_revert")
    if (not candidate or not task.get("automation") or task.get("status") != "needs_user"
            or task.get("failure_stage") != "plan"
            or task.get("failure_code") not in ("branch_exists", "delivery_base_unknown")
            or candidate.get("execution_run_id") != task.get("run_id")
            or candidate.get("plan_sha256") != task.get("plan_sha256")
            or any(r.get("task_id") == task["task_id"] and r.get("status") in ("queued", "claimed") for r in requests)):
        return None
    return dict(candidate)


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
    status = task.get("status")
    if task.get("_delivery_continuation"):
        return ["continue_delivery", "recheck"]
    closeout = task.get("parent_closeout")
    if closeout and status != "done":
        return (["closeout_parent"] if closeout.get("can_complete") else []) + ["recheck"]
    release = task.get("release")
    if release and release.get("required") and release.get("state") != "published":
        repair = release.get("external_repair") or {}
        if repair.get("can_sync_verification") and (repair.get("verification") or {}).get("verification_id"):
            return ["sync_release_verification", "recheck"]
        if repair.get("can_restart_verification"):
            return ["restart_release_verification", "recheck"]
        return ["recheck"]
    current_ai_action = (task.get("task_ai_action") or {}).get("current")
    if current_ai_action:
        if (current_ai_action.get("kind") == "merge_conflict" and status == "needs_user"
                and task.get("failure_code") == "verification_unconfirmed"
                and not any(r.get("task_id") == task["task_id"] and r.get("status") in ("queued", "claimed") for r in requests)):
            return ["retry_verification", "reverify", "recheck"]
        return ["recheck"]  # 一次性处理不从通用恢复入口新增执行轮次。
    if task.get("_automation_allowed") and status == "needs_user":
        return list(task["_automation_allowed"]) + (["revert_engine_fix"] if fake_engine_fix_revert(task, requests) else [])
    if status == "done":
        actions = ["recheck"]
        if move_out_blocked_reason(task, requests) is None:
            actions += ["archive", "trash"]
        return actions
    task_reqs = [r for r in requests if r.get("task_id") == task["task_id"]]
    if any(request_is_lost(r) for r in task_reqs):
        return ["release_lost", "recheck"]
    # protocol §5.3（2026-10-17）：同一任务有 queued/claimed 活动请求时不再提供 enqueue。
    has_active_request = any(r.get("status") in ("queued", "claimed") for r in task_reqs)
    if status == "needs_user":
        if task.get("failure_code") == "execution_input_required":
            return ["answer_execution_questions", "skip_execution_questions", "stop_execution"]
        if task.get("plan_clarification"):
            return ["answer_plan_questions", "replan", "dismiss", "recheck"]
        if task.get("failure_code") == "verification_unconfirmed":
            actions = ["retry_verification", "reverify", "replan", "dismiss", "recheck"]
            if (task.get("execution_round") or {}).get("backend") != "codex":
                actions.insert(2, "accept_verification")
            return actions
        if task.get("failure_code") == "verification_environment_failure":
            return ["retry_verification", "reverify", "resume", "replan", "dismiss", "recheck"]
        if task.get("failure_code") == "verification_identity_unconfirmed":
            return ["reverify", "replan", "dismiss", "recheck"]
        if task.get("review_resolution"):
            return ["resolve_review_blocker", "retry", "replan", "dismiss", "recheck"]
        if task.get("send_state") == "unknown":
            return ["restore", "accept_duplicate", "mark_sent", "recheck"]
        if task.get("failure_code") == "execution_failed":
            return ["resume", "replan", "dismiss", "mark_done", "recheck"]
        return ["retry", "replan", "dismiss", "mark_done", "recheck"]
    if status in ("needs_plan",):
        # v2 §5.22：plan 不再经 enqueue，打开规划面板（prepare_plan）。
        return ["prepare_plan", "recheck"]
    if status == "ready":
        # v2 §5.22.4：plan_execution_hold → 仅确认执行（enqueue confirm_plan）。
        if task.get("plan_execution_hold"):
            return ["confirm_dispatch", "recheck"]
        actions = ["recheck"] if has_active_request else ["enqueue", "recheck"]
        if any(r["status"] == "queued" for r in task_reqs):
            actions.insert(1 if "enqueue" in actions else 0, "set-priority")
            actions.append("set-paused")
        if move_out_blocked_reason(task, requests) is None:
            actions += ["archive", "trash"]
        return actions
    if status in ("running", "in_review"):
        has_held_repair = (task.get("review_repair") or {}).get("state") == "held"
        actions = ["mark_done", "recheck"] if has_active_request or has_held_repair else ["enqueue", "mark_done", "recheck"]
        if status == "running" and (task.get("execution_round") or {}).get("backend") == "codex":
            actions.append("stop_execution")
        if any(r["status"] == "queued" for r in task_reqs):
            actions.insert(2 if "enqueue" in actions else 1, "set-priority")
        return actions
    return ["recheck"]


ENQUEUE_INFER = {"needs_plan": "plan", "ready": "dispatch", "running": "monitor", "in_review": "review"}


def enqueue_kind(task, actions):
    """protocol §5.2：下一次 enqueue 的 kind；仅当 allowed_actions 含 enqueue。"""
    if "enqueue" not in actions:
        return None
    return ENQUEUE_INFER.get(task.get("status"))


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
    detail["engine_fix_summary"] = task.get("engine_fix_summary", {"used": 1, "max": 2}) if task.get("automation") else None
    detail["engine_fix_revert"] = fake_engine_fix_revert(task, requests)
    detail["allowed_actions"] = allowed_actions(task, requests)
    detail["enqueue_kind"] = enqueue_kind(task, detail["allowed_actions"])
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
            scenario = fixture_scenario(state, "%s/%s" % (tid, task.get("run_id")))
            if scenario.get("task_read_error"):
                return fail(str(scenario["task_read_error"]), "remote_unavailable")
            return {"task": task_detail(task, merged_requests(state))}
    return fail("任务不存在：%s" % tid, "not_found")


def cmd_plan_answers(state, payload, submit=False):
    """Isolated UI fixture; never contact a model, harness or real Kaneo."""
    tid = payload.get("task_id")
    operation = payload.get("operation_id")
    record = state.get("plan_answer_operations", {}).get(operation) if submit else None
    if record:
        if record["payload"] != payload:
            return fail("同一提交标识的答案已变化", "conflict")
        return {"ok": True, "request_id": record["request_id"], "deduplicated": True}
    task = next((t for t in merged_tasks(state) if t["task_id"] == tid), None)
    clarification = (task or {}).get("plan_clarification")
    if not clarification or clarification["id"] != payload.get("clarification_id"):
        return fail("待确认问题已变化，请刷新后重新回答", "conflict")
    failure = state.get("plan_answer_fixture_failure")
    if failure:
        return fail(failure, "remote_unavailable")
    answers = payload.get("answers", [])
    if submit:
        by_id = {a["question_id"]: a for a in answers}
        for q in clarification["questions"]:
            answer = by_id.get(q["id"], {})
            valid = bool(answer.get("text", "").strip()) if not q["options"] else (
                answer.get("option_id") in {o["id"] for o in q["options"]} or
                q["allow_custom"] and answer.get("option_id") == "custom" and bool(answer.get("text", "").strip()))
            if q["required"] and not valid:
                return fail("请完成必答问题", "bad_request")
        if not operation:
            return fail("缺少提交标识", "bad_request")
    def update(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        saved = copy.deepcopy(clarification)
        saved.update(saved_answers=answers, supplement=payload.get("supplement", ""), saved_at=iso(utcnow()))
        ov["plan_clarification"] = saved
        if not submit:
            return {"ok": True}
        rid = "req-plan-answers-" + uuid.uuid4().hex[:8]
        s.setdefault("extra_requests", []).append({
            "request_id": rid, "task_id": tid, "kind": "plan", "stage": "plan", "status": "queued",
            "priority": 100, "created_at": iso(utcnow()), "updated_at": iso(utcnow()), "requested_by": "user"})
        ov.update(plan_clarification=None, status="planning", display_status="已排队", blocked_reason=None,
                  blocked_summary=None, failure_code=None, failure_reason=None, failure_stage=None)
        s.setdefault("plan_answer_operations", {})[operation] = {"payload": payload, "request_id": rid}
        decisions = []
        for question in clarification["questions"]:
            answer = next((a for a in answers if a["question_id"] == question["id"]), {})
            label = next((o["label"] for o in question["options"] if o["id"] == answer.get("option_id")), "")
            decisions.append(question["title"] + "：" + "；".join(v for v in (label, answer.get("text")) if v))
        append_event(s, "plan_answers_submitted", tid, rid, detail={
            "questions": clarification["questions"], "answers": answers, "supplement": payload.get("supplement", ""),
            "note": "；".join(decisions)})
        return {"ok": True, "request_id": rid, "deduplicated": False}
    return mutate(state, update)


def cmd_verification_checks(state, payload, status_only=False):
    tid, op = payload.get("task_id"), payload.get("operation_id")
    previous = state.get("verification_check_operations", {}).get(op)
    if status_only:
        if not previous or previous["payload"].get("task_id") != tid:
            return {"ok": True, "state": "not_found"}
        return dict(previous["response"], state="completed", deduplicated=True)
    if previous:
        if previous["payload"] != payload:
            return fail("同一操作编号的检查内容已变化", "conflict")
        if previous["response"].get("recovery_pending") and not state.get("verification_checks_fixture_recovery_pending"):
            def recover(s):
                response = s["verification_check_operations"][op]["response"]
                response.update(recovery_pending=False, detail="检查已通过，已恢复推进")
                ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
                ov["verification_checks"].update(recovery_pending=False, detail=response["detail"])
                ov.update(status="running", stage="monitor", display_status="执行中", failure_code=None,
                          failure_reason=None, failure_stage=None, blocked_reason=None, blocked_summary=None)
                enqueue_request(s, tid, "monitor")
                return dict(response, deduplicated=True)
            return mutate(state, recover)
        return dict(previous["response"], deduplicated=True)
    task = next((t for t in merged_tasks(state) if t["task_id"] == tid), None)
    if not task or task.get("failure_code") not in ("verification_unconfirmed", "verification_environment_failure", "verification_identity_unconfirmed"):
        return fail("当前任务不允许重新运行验证检查", "conflict")
    if (payload.get("expected_execution_run_id") != task.get("run_id")
            or payload.get("expected_plan_sha256") != task.get("plan_sha256")):
        return fail("执行轮次或计划已变化", "conflict")
    code = state.get("verification_checks_fixture_exit_code", 0)
    checks = [{"command": command, "exit_code": code if i == 0 else 0,
               "timed_out": False, "detail": "隔离模拟检查失败" if code and i == 0 else None,
               "output_tail": "隔离 FakeCore 输出：" + ("检查失败" if code and i == 0 else "检查通过")}
              for i, command in enumerate(VERIFICATION_FIXTURE_COMMANDS)]
    pending = code == 0 and state.get("verification_checks_fixture_recovery_pending", False)
    result = {"ok": True, "verification_ok": code == 0, "recovery_pending": pending, "deduplicated": False, "checks": checks,
              "detail": "所有检查独立通过，已排队监控" if code == 0 else "检查未通过，原成果与阻塞保留"}
    if pending:
        result["detail"] = "检查已通过，恢复推进暂未确认，请复用原回执继续恢复"
    def update(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        ov["verification_checks"] = dict(result, operation_id=op, state="completed",
                                          execution_run_id=task["run_id"], plan_sha256=task["plan_sha256"])
        s.setdefault("verification_check_operations", {})[op] = {"payload": payload, "response": result}
        append_event(s, "verification_checks_started", tid, detail={"operation_id": op})
        append_event(s, "verification_checks_completed", tid, detail={"operation_id": op, "verification_ok": code == 0})
        if code == 0 and not pending:
            ov.update(status="running", stage="monitor", display_status="执行中", failure_code=None,
                      failure_reason=None, failure_stage=None, blocked_reason=None, blocked_summary=None)
            enqueue_request(s, tid, "monitor")
        return result
    return mutate(state, update)


def cmd_review_resolution(state, payload, evidence=False):
    """Native acceptance fixture, never model/Kaneo/Git operations."""
    tid = payload.get("task_id")
    op = payload.get("operation_id")
    receipt = state.get("review_resolution_operations", {}).get(op) if evidence else None
    if receipt:
        if receipt["payload"] != payload:
            return fail("提交标识的内容已变化", "conflict")
        return {"ok": True, "request_id": receipt["request_id"], "deduplicated": True}
    if not evidence and tid in state.get("review_remediations", {}):
        return {"ok": True, "task_id": tid, "deduplicated": True, "detail": "修复材料已准备"}
    task = next((t for t in merged_tasks(state) if t["task_id"] == tid), None)
    resolution = (task or {}).get("review_resolution")
    fields = {"expected_execution_run_id": "execution_run_id", "expected_review_request_id": "review_request_id",
              "expected_report_sha256": "report_sha256", "expected_plan_sha256": "plan_sha256"}
    if not resolution or any(payload.get(key) != resolution.get(value) for key, value in fields.items()):
        return fail("评审版本已变化，请刷新后处理", "conflict")
    if state.get("review_resolution_fixture_failure"):
        return fail(state["review_resolution_fixture_failure"], "remote_unavailable")
    if evidence and not (payload.get("evidence_text", "").strip() or payload.get("local_paths")):
        return fail("请填写说明或选择证据文件", "bad_request")
    def update(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        ov.update(review_resolution=None, blocked_reason=None, failure_code=None, failure_reason=None,
                  blocked_summary=None, stage="review" if evidence else "plan")
        if not evidence:
            s.setdefault("review_remediations", {})[tid] = dict(resolution)
            ov.update(status="needs_plan", display_status="待规划")
            append_event(s, "review_repair_prepared", tid, detail={"note": "原计划、评审报告和执行成果已带入修复材料"})
            return {"ok": True, "task_id": tid, "deduplicated": False, "detail": "修复材料已准备，请核对后开始规划"}
        rid = "req-evidence-" + uuid.uuid4().hex[:8]
        s.setdefault("extra_requests", []).append({"request_id": rid, "task_id": tid, "stage": "review", "kind": "review",
                                                 "status": "queued", "created_at": iso(utcnow()), "updated_at": iso(utcnow()), "priority": 100})
        s.setdefault("review_resolution_operations", {})[op] = {"payload": payload, "request_id": rid}
        ov.update(status="in_review", display_status="已排队")
        append_event(s, "review_supplemented", tid, rid, detail={"note": "用户补充证据已保存，等待评审核验"})
        return {"ok": True, "request_id": rid, "deduplicated": False}
    return mutate(state, update)


def cmd_get_plan(state, payload):
    """Static verified-response fixture; never read a user's real plan files."""
    task = next((t for t in merged_tasks(state) if t["task_id"] == payload.get("task_id")), None)
    if not task or not task.get("plan_path"):
        return fail("该任务尚未生成计划", "not_found")
    if state.get("plan_read_error"):
        return fail("隔离演练：计划自发布后已变更", "conflict")
    expected = payload.get("expected_plan_sha256")
    if expected and expected != task.get("plan_sha256"):
        return fail("计划哈希不匹配", "conflict")
    markdown = ("# " + task["title"] + "\n\n"
                "> 隔离预览材料，不会调用真实模型或修改真实仓库。\n\n"
                "## 本次目标\n\n让用户明确当前阶段、需要决定的事项和最终交付结果。\n\n"
                "## 实施与验证\n\n1. 核对需求和材料。\n2. 在隔离环境执行与验证。\n3. 保留记录并提交评审。\n\n"
                "| 检查 | 预期 |\n| --- | --- |\n| 正常操作 | 状态清晰 |\n| 失败状态 | 保留原因和恢复入口 |\n")
    markdown = task.get("_verification_plan_markdown", markdown)
    return {"task_id": task["task_id"], "title": task["title"],
            "plan_kind": task.get("plan_kind", "standard"),
            "generated_at": task.get("plan_generated_at"),
            "plan_sha256": task.get("plan_sha256"), "plan_path": task["plan_path"],
            "markdown": markdown}


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
        frozen = r.get("payload") or {}
        r["backend"] = r.get("backend") or frozen.get("planner_backend") or frozen.get("review_backend")
    requests.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    limit = payload.get("limit", 200)
    return {"requests": requests[: int(limit)]}


EVENT_KIND_CN = {
    "verification_checks_started": "验证检查已开始", "verification_checks_completed": "验证检查已完成",
    "review_repair_prepared": "评审修复材料已准备", "review_supplemented": "评审证据已补充",
    "task_created": "任务创建", "task_updated": "任务更新",
    "requirements_changed": "需求变更", "kaneo_observed": "远端观察",
    "plan_published": "计划发布", "enqueued": "已入队", "claimed": "已领取",
    "heartbeat": "心跳", "status_change": "状态变更",
    "dispatch_prepared": "派发就绪", "kaneo_transition": "远端迁移",
    "dispatch_sent": "派发已发送", "dispatch_failed": "派发失败",
    "reported": "已回报", "review_registered": "评审登记",
    "fix_task_created": "修复任务创建", "resolved": "人工解决",
    "engine_fix_reverted": "自动补救已撤回", "branch_worktree_adopted": "已收养任务分支工作树",
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
    "plan_answers_submitted": "规划答案已提交",
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
    elif kind in ("reported", "plan_answers_submitted"):
        note = detail.get("note") or detail.get("reason") or detail.get("progress")
        summary = "%s：%s" % (cn, note) if note else cn
        if kind == "plan_answers_submitted":
            stage, result = "plan", "succeeded"
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
    scenario = fixture_scenario(state, "%s/%s" % (tid, task.get("run_id")))
    if scenario.get("resolve_error"):
        return fail(str(scenario["resolve_error"]), "conflict")
    if action == "continue_delivery":
        if (not task.get("_delivery_continuation")
                or payload.get("expected_execution_run_id") != task.get("run_id")
                or payload.get("expected_review_key") != task.get("review_key")):
            return fail("交付候选已变化，请刷新后重试", "conflict")
    if action == "revert_engine_fix":
        if not payload.get("expected_execution_run_id") or not payload.get("expected_plan_sha256"):
            return fail("撤回需要执行轮次与计划摘要", "bad_request")
        candidate = fake_engine_fix_revert(task, requests)
        if (not candidate or payload["expected_execution_run_id"] != candidate["execution_run_id"]
                or payload["expected_plan_sha256"] != candidate["plan_sha256"]):
            return fail("自动补救撤回候选已变化，请刷新后重试", "conflict")
        if scenario.get("revert_error"):
            return fail(str(scenario["revert_error"]), "conflict")
    if action == "reverify":
        if action not in allowed_actions(task, requests) or task.get("failure_code") not in ("verification_unconfirmed", "verification_environment_failure", "verification_identity_unconfirmed"):
            return fail("当前状态不允许重新核验验证证据", "conflict")
        if not payload.get("expected_execution_run_id") or payload["expected_execution_run_id"] != task.get("run_id"):
            return fail("执行轮次已变化，请刷新后重新核验", "conflict")
        if any(r.get("task_id") == tid and r.get("status") in ("queued", "claimed") for r in requests):
            return fail("任务仍有活动请求，请刷新后重新核验", "conflict")
        if scenario.get("verification_passed", task.get("_verification_passed")) is not True:
            return fail("验证未通过或证据仍无法确认，请查看执行记录", "conflict")

    def fail_action(msg):
        return {"__fail__": True, "error": msg, "code": "bad_request"}

    def apply(s):
        ov = s.setdefault("task_overrides", {}).setdefault(tid, {})
        clear = ["failure_code", "failure_reason", "failure_stage",
                 "blocked_at", "blocked_reason", "blocked_summary",
                 "manual_recovery_required", "lock_retained"]
        if action == "continue_delivery":
            for k in clear:
                ov[k] = None
            ov.update(status="in_review", stage="delivery", display_status="交付中",
                      _delivery_continuation=False, _automation_allowed=None,
                      delivery=dict(task.get("delivery") or {}, mode="pr"))
            enqueue_request(s, tid, "delivery")
            append_event(s, "delivery_continued", task_id=tid, actor="user",
                         detail={"execution_run_id": payload["expected_execution_run_id"],
                                 "review_key": payload["expected_review_key"]})
        elif action == "revert_engine_fix":
            ov.update({"status": "needs_user", "stage": "monitor", "failure_stage": "monitor",
                       "failure_code": "verification_environment_failure", "display_status": "需人工",
                       "failure_reason": "验证所需 JDK 版本不符：请核对 Plus 原轮次证据或让 Plus 重新执行并验证",
                       "send_state": "sent", "sent": 1, "sent_at": task.get("sent_at"),
                       "engine_fix_revert": None,
                       "engine_fix_summary": {"used": max(0, (task.get("engine_fix_summary") or {"used": 1})["used"] - 1), "max": 2},
                       "_automation_allowed": ["resume", "replan", "mark_done", "dismiss"],
                       "delivery": {"mode": "manual"}})
            append_event(s, "engine_fix_reverted", task_id=tid, actor="user",
                         detail={"execution_run_id": candidate["execution_run_id"], "plan_sha256": candidate["plan_sha256"],
                                 "failure_code": "verification_environment_failure", "environment_reasons": ["jdk_version"]})
        elif action == "reverify":
            for k in clear:
                ov[k] = None
            ov.update({"status": "running", "display_status": "执行中", "stage": "monitor"})
            enqueue_request(s, tid, "monitor")
        elif action in ("resume", "retry_verification", "accept_verification"):
            if action not in allowed_actions(task, requests):
                return fail_action("核心未允许此动作")
            failed_execution = (action == "resume" and task.get("failure_code") == "execution_failed") or action == "retry_verification"
            if "expected_execution_run_id" in payload and not failed_execution:
                return fail_action("失败执行状态已变化，请刷新后重新操作")
            if failed_execution and (not payload.get("expected_execution_run_id") or
                                     payload["expected_execution_run_id"] != task.get("run_id")):
                return fail_action("失败执行轮次已变化，请刷新后重新操作")
            for k in clear:
                ov[k] = None
            if failed_execution:
                if (task.get("task_ai_action") or {}).get("current", {}).get("kind") == "merge_conflict":
                    s["task_ai_action_fixture"]["scenario"] = "queued"
                ov.update({"status": "ready", "display_status": "已排队", "stage": "dispatch",
                           "run_id": None, "session_id": None, "session_marker": None,
                           "send_state": "not_sent", "sent": 0})
                enqueue_request(s, tid, "dispatch")
            else:
                ov.update({"status": "running", "display_status": "执行中", "stage": "monitor"})
                enqueue_request(s, tid, "monitor")
        elif action == "retry":
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
            if task.get("automation"):
                ov["automation"] = dict(task["automation"], manual_takeover=True)
                ov["delivery"] = dict(task.get("delivery") or {}, mode="manual")
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
                  "monitor": "monitor", "review": "review", "delivery": "delivery"}[kind],
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
LONG_SESSION_SHAPE = os.environ.get("KANPILOT_FAKE_LONG_SESSION_SHAPE", "lines")


def long_session_messages(count=None, output_bytes=None):
    """T1 合成长会话：count 条消息，部分为固定字节数的长工具输出。

    长输出逐行填充到接近 output_bytes（ASCII，字节数与字符数一致），
    消息类型循环覆盖 assistant/tool_call/tool_result/command_output，
    保证折叠、搜索、逐条展开与本项目既有夹具形状一致。
    """
    count = count or LONG_SESSION_COUNT
    output_bytes = output_bytes or LONG_SESSION_BYTES
    line = "0123456789abcdef tool output chunk payload\n"
    long_output = (line * (output_bytes // len(line) + 1))[:output_bytes]
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
            if LONG_SESSION_SHAPE == "wrapped_json":
                content = json.dumps({"content": [{"type": "text", "text": json.dumps({
                    "command": "python3 scripts/step_%04d.py" % seq,
                    "commandShellId": "shell-%04d" % seq,
                    "stdout": long_output, "stderr": "", "exitCode": 0})}]})
            tool = {"name": "Bash",
                    "input": "python3 scripts/step_%04d.py" % seq,
                    "output": content, "exit_code": 0}
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

    sessions = {
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
    failed = entry("auto-execution_failed", "run-auto-execution-failed", "associated", "ok", [])
    failed["session"].update(backend="pi_desktop", run_status="failed")
    sessions["auto-execution_failed/run-auto-execution-failed"] = failed
    previous = entry("auto-execution_failed", "run-auto-execution-previous", "associated", "ok", [])
    previous["session"].update(backend="pi_desktop", run_status="failed")
    sessions["auto-execution_failed/run-auto-execution-previous"] = previous
    return sessions


def seeded_sessions(state):
    """Freeze source metadata/content once; only explicit scenario mutations change it."""
    seeded = state.get("session_fixture_seed")
    shape = [LONG_SESSION_TASK, LONG_SESSION_COUNT, LONG_SESSION_BYTES, LONG_SESSION_SHAPE]
    if seeded is None or state.get("session_fixture_shape") != shape:
        seeded = base_sessions()
        if LONG_SESSION_TASK:
            marker = LONG_SESSION_TASK + "/run-music-006-b2"
            if marker in seeded:
                seeded[marker]["messages"] = long_session_messages()
                seeded[marker]["session"]["message_count"] = LONG_SESSION_COUNT
                seeded[marker]["session"]["run_status"] = "running"
        for marker, entry in seeded.items():
            for message in entry["messages"]:
                message["run_id"] = entry["session"]["run_id"]
            if entry["connection"]["state"] != "unavailable":
                entry["connection"] = {"state": "degraded", "detail": None,
                                       "poll_seconds": SESSION_POLL_SECONDS}
        state["session_fixture_seed"] = seeded
        state["session_fixture_shape"] = shape
        save_state(state)
    if isinstance(state.get("task_ai_action_fixture"), dict):
        seeded = dict(seeded)
        task = next((t for t in merged_tasks(state) if t["task_id"] == "auto-long-pr"), {})
        for action in (task.get("task_ai_action") or {}).get("history", []):
            run_id = action["execution_run_id"]
            marker = "auto-long-pr/" + run_id
            queued = action["state"] == "queued"
            messages = [] if queued else [{"id": marker + "-message", "seq": 1, "run_id": run_id,
                "type": "assistant", "revision": 1, "source_at": action["created_at"],
                "content": "隔离 AI 处理会话：直接解决 PR 冲突，不重新规划。"}]
            seeded[marker] = {"session": {"task_id": "auto-long-pr", "run_id": run_id,
                "session_marker": marker, "session_id": action.get("session_id"),
                "session_title": "AI 处理 · 合并冲突（隔离示例）", "state": "pending" if queued else "associated",
                "started_at": None if queued else action["created_at"], "updated_at": action["created_at"],
                "last_checked_at": action["created_at"], "message_count": len(messages), "save_state": "active",
                "backend": "pi_desktop", "run_status": "running" if action["state"] == "running" else "finished",
                "allowed_actions": [], "read_only": False},
                "connection": {"state": "ok", "detail": "隔离假会话，无真实模型调用", "poll_seconds": SESSION_POLL_SECONDS},
                "messages": messages}
    return seeded


def fixture_scenario(state, marker):
    return (state.get("session_scenarios") or {}).get(marker) or {}


def fixture_version(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


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
    runs = [k[len(prefix):] for k in seeded_sessions(state) if k.startswith(prefix)]
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
    task = next((t for t in merged_tasks(state) if t["task_id"] == session.get("task_id")), {})
    current_active = (session.get("run_id") == task_run_id(state, session.get("task_id"))
                      and (task.get("status") in ("planning", "dispatching", "prepared", "submitting", "running", "in_review")
                           or task.get("send_state") == "unknown"))
    if session.get("run_status") == "running" or current_active:
        session["allowed_actions"] = ["restore-session"] if save_state == "trashed" else []
        session["blocked_reason"] = "仍在执行，不能移入回收站"
    return session


def long_session_override(task_id, messages):
    """T1 度量夹具：`KANPILOT_FAKE_LONG_SESSION` 命中时替换该任务消息。"""
    if LONG_SESSION_TASK and task_id == LONG_SESSION_TASK:
        return long_session_messages()
    return messages


def resolve_session(state, task_id, run_id=None):
    """返回 (SessionInfo, connection, messages)；无记录 → pending。"""
    run_id = run_id or task_run_id(state, task_id) or latest_session_run(state, task_id)
    entry = seeded_sessions(state).get("%s/%s" % (task_id, run_id)) if run_id else None
    if entry is None:
        return (apply_session_save_state(state, pending_session(task_id, run_id)),
                {"state": "ok", "detail": None, "poll_seconds": SESSION_POLL_SECONDS},
                [])
    messages = entry["messages"]
    marker = "%s/%s" % (task_id, run_id)
    scenario = fixture_scenario(state, marker)
    if scenario.get("append") or scenario.get("revisions") or scenario.get("large_unknown"):
        messages = copy.deepcopy(messages)
        messages.extend(scenario.get("append") or [])
        for message in messages:
            change = (scenario.get("revisions") or {}).get(message["id"])
            if change:
                message.update(change)
        if scenario.get("large_unknown"):
            messages.append({"id": "unknown-large", "seq": max((m.get("seq", 0) for m in messages), default=0)+1,
                             "run_id": run_id, "type": "tool_result", "revision": 1,
                             "source_at": entry["session"]["updated_at"],
                             "content": json.dumps({"unknown": "x" * (1024*1024)}),
                             "tool": {"name": "unknown", "input": None,
                                      "output": None, "exit_code": None}})
    session = dict(entry["session"], message_count=len(messages), last_checked_at=iso(utcnow()))
    if scenario.get("backend"):
        session["backend"] = scenario["backend"]
    if scenario.get("pending"):
        session.update(state="pending", message_count=0)
        messages = []
    if scenario.get("updated_at"):
        session["updated_at"] = scenario["updated_at"]
    if scenario.get("round_status"):
        session["run_status"] = scenario["round_status"]
    if "session_id" in scenario:
        session["session_id"] = scenario["session_id"]
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
        runs = sorted(k[len(prefix):] for k in seeded_sessions(state) if k.startswith(prefix))
        if runs:
            for run in runs:
                sessions.append(apply_session_save_state(
                    state, seeded_sessions(state)["%s/%s" % (task_id, run)]["session"]))
        else:
            run = task_run_id(state, task_id)
            if run:
                sessions.append(apply_session_save_state(
                    state, pending_session(task_id, run)))
        return {"sessions": [s for s in sessions if _match(s)]}
    # 缺省：全部可关联会话，active 任务优先
    rows = []
    for key, entry in seeded_sessions(state).items():
        session = apply_session_save_state(state, entry["session"])
        task_id = session["task_id"]
        task = next((t for t in merged_tasks(state) if t["task_id"] == task_id), None)
        rows.append((1 if (task or {}).get("save_state") == "active" else 0,
                     session.get("updated_at") or "", session))
    for task in merged_tasks(state):
        if task.get("save_state") != "active":
            continue
        marker = task.get("session_marker")
        if not marker or marker in seeded_sessions(state):
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
    scenario = fixture_scenario(state, "%s/%s" % (task_id, run_id))
    if scenario.get("status_delay_ms"):
        time.sleep(float(scenario["status_delay_ms"]) / 1000)
    if scenario.get("status_read_error"):
        return fail(str(scenario["status_read_error"]), "remote_unavailable")
    status = scenario.get("round_status") or session.get("run_status") or "unknown"
    detail = {"backend": session.get("backend") or "pi", "run_status": status, "turn_id": "turn-round",
              "worktree_path": os.path.join(os.environ.get("KANPILOT_REPOS_ROOT", "/tmp"), run_id),
              "started_at": session.get("started_at"), "exit_code": 1 if status == "failed" else None,
              "terminal": {"outcome": "failed" if status == "failed" else "completed",
                           "reason": "model_unavailable" if status == "failed" else "completed"},
              "detail": "隔离模型不可用" if status == "failed" else None,
              "config": {"provider": "fixture", "model": "fixture-model", "reasoning_effort": "medium",
                         "agent_preset": "default"}, "save_state": session.get("save_state"),
              "turn_adopted": [{"from": "turn-old", "to": "turn-current",
                                "code": "EMPTY_MODEL_RESPONSE", "at": session.get("updated_at")}]}
    if status == "running":
        detail["terminal"] = None
    detail.update(scenario.get("round_detail") or {})
    return {"session": session, "connection": connection, "round_detail": detail}


def cmd_session_messages(state, payload):
    request_log = os.environ.get("KANPILOT_FAKE_REQUEST_LOG")
    if request_log:
        with open(request_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    task_id = payload.get("task_id")
    if not task_id:
        return fail("缺少必填字段 task_id", "bad_request")
    if session_read_fails(state, task_id):
        return fail("执行会话读取失败：连接中断（fake 模拟）", "remote_unavailable")
    run_id = payload.get("run_id") or task_run_id(state, task_id) \
        or latest_session_run(state, task_id)
    session, connection, messages = resolve_session(state, task_id, run_id)
    marker = "%s/%s" % (task_id, run_id)
    scenario = fixture_scenario(state, marker)
    delay = scenario.get("delay_ms", 0)
    if delay:
        time.sleep(min(float(delay)/1000, 5))
    if "fallback_messages" in scenario:
        fallback = scenario["fallback_messages"]
        return {"session": session, "connection": {"state": "degraded", "detail": "隔离来源读取失败，返回旧快照", "poll_seconds": SESSION_POLL_SECONDS},
                "messages": fallback, "next_cursor": "seq:4", "has_more": False, "source_version": None}
    if scenario.get("read_error"):
        return fail("隔离会话读取失败：" + str(scenario["read_error"]), "remote_unavailable")
    ordered = sorted(messages, key=lambda m: m.get("seq") or 0)
    cursor = payload.get("cursor")
    try:
        after = int(str(cursor).removeprefix("seq:")) if cursor not in (None, "") else 0
    except (TypeError, ValueError):
        return fail("cursor 必须是 seq:<n>", "bad_request")
    limit = max(1, min(int(payload.get("limit") or 100), 1000))
    version = hashlib.sha256(json.dumps({"messages": ordered, "run_status": session.get("run_status"),
                                            "updated_at": session.get("updated_at")},
                                           ensure_ascii=False, sort_keys=True).encode()).hexdigest() if session.get("state") == "associated" and connection.get("state") != "unavailable" and not connection.get("detail") else None
    maximum = max((m.get("seq") or 0 for m in ordered), default=0)
    unchanged = (cursor is not None and after >= maximum and version is not None
                 and payload.get("source_version") == version and not payload.get("force_reload"))
    if unchanged:
        page, has_more = [], False
    else:
        # Explicit revision scenarios resend revised old IDs once per acknowledged version.
        candidates = [m for m in ordered if (m.get("seq") or 0) > after]
        if after and payload.get("source_version") != version:
            changed = scenario.get("revisions") or {}
            candidates = [m for m in ordered if m["id"] in changed or (m.get("seq") or 0) > after]
        page = candidates[:limit]
        has_more = len(candidates) > len(page)
    next_cursor = "seq:%d" % max(after, max((m.get("seq") or 0 for m in page), default=0)) if page else cursor
    return {"session": session, "messages": page,
            "next_cursor": next_cursor, "has_more": has_more,
            "source_version": version, "connection": connection}


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


def cmd_reconnect_task(state, payload):
    tid = payload.get("task_id")
    expected = payload.get("expected_save_state")
    if not tid or expected not in ("archived", "trashed", "purged"):
        return fail("需要 task_id 和有效 expected_save_state", "bad_request")
    task = next((t for t in merged_tasks(state) if t["task_id"] == tid), None)
    if task is None:
        return fail("任务不存在", "not_found")
    candidate = next((t for t in cmd_fetch_todos(state, {})["items"] if t["task_id"] == tid), None)
    if task.get("save_state") != expected or not candidate or not candidate.get("reconnectable"):
        return fail("任务状态变化或当前不能重新接入，请刷新后重试", "conflict")
    def apply(s):
        s.setdefault("task_overrides", {}).setdefault(tid, {}).update(
            save_state="active", saved_at=None, status="needs_plan", stage="plan",
            auto_advance_paused=1, run_id=None, failure_code=None, failure_reason=None,
            plan_path=None, plan_sha256=None, plan_kind=None, plan_generated_at=None)
        append_event(s, "task_reconnected", task_id=tid, actor="user",
                     detail={"from": expected, "to": "active",
                             "previous": {key: task.get(key) for key in ("plan_path", "plan_sha256", "source_path", "run_id")}})
    mutate(state, apply)
    return {"ok": True}


# ------------------------------------------------- §5.17/§5.19 exec config + executor（v1.6，pi）

EXEC_CONFIG_DEFAULTS = {
    "provider": "CloseAI",
    "model": "deepseek-v4-flash",
    "reasoning_effort": "max",
    "agent_preset": "default",
    "backend": "pi",
    "codex": {"model": "gpt-6.1-sol", "reasoning_effort": "high"},
}


def cmd_exec_capabilities(state, payload):
    """exec-capabilities {}：v1.6 pi 形态（pi/providers/presets/checks，
    不再有 dsh/permission 字段）。"""
    saved = state.get("exec_config") or {}
    codex_saved = isinstance(saved.get("codex"), dict)
    codex = dict({"available": codex_saved,
                  "path": "/tmp/fake-codex", "version": "fake",
                  "detail": "隔离 FakeCore 就绪" if codex_saved else "Codex 执行配置未保存"},
                 **(state.get("codex_exec_capability_fixture") or {}))
    return {
        "ok": codex["available"] if saved.get("backend") == "codex" else True,
        "codex": codex,
        "pi_desktop": {"available": True, "detail": "隔离 FakeCore 就绪"},
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
    fields = [k for k in (*EXEC_CONFIG_DEFAULTS, "pi_desktop") if k in payload]
    if not fields:
        return fail("set-exec-config 至少提供一个字段"
                    "（provider / model / reasoning_effort / agent_preset）",
                    "bad_request")
    caps = cmd_exec_capabilities(state, payload)
    candidate = dict(state.get("exec_config") or EXEC_CONFIG_DEFAULTS)
    if not isinstance((state.get("exec_config") or {}).get("codex"), dict):
        candidate.pop("codex", None)
    for k in fields:
        if k in ("codex", "pi_desktop"):
            if not isinstance(payload[k], dict):
                return fail(k + " 必须是对象", "bad_request")
            candidate[k] = dict(candidate.get(k) or {}, **payload[k])
        else:
            candidate[k] = payload[k]
    if candidate.get("backend") not in ("pi", "pi_desktop", "codex"):
        return fail("不支持的执行后端", "bad_request")
    if "codex" in fields or candidate.get("backend") == "codex":
        block = candidate.get("codex") or {}
        if set(block) != {"model", "reasoning_effort"}:
            return fail("codex 仅接受 model 和 reasoning_effort 完整配置", "bad_request")
        models = planner_capabilities(state)["models"]
        entry = next((m for m in models if m["id"] == block.get("model")), None)
        if not entry or block.get("reasoning_effort") not in entry["reasoning_efforts"]:
            return fail("Codex 模型或思考等级无效", "bad_request")
    if candidate.get("backend", "pi") == "pi":
        error = validate_fake_pi_exec(candidate, caps)
        if error:
            return error
    elif candidate.get("backend") == "pi_desktop" and not candidate.get("pi_desktop"):
        return fail("pi_desktop 配置未保存", "bad_request")
    candidate["updated_at"] = iso(utcnow())

    def apply(s):
        s["exec_config"] = dict(candidate)
        append_event(s, "exec_config_updated", detail=dict(candidate))
    mutate(state, apply)
    return {"config": dict(candidate)}


def validate_fake_pi_exec(candidate, caps):
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
    return None


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
    return {"owned_workers": [], "paused": state.get("paused", {"global": False, "stages": []}), "stages": {
        stage: {"running": None, "last": runs.get(stage)}
        for stage in ("plan", "dispatch", "monitor", "review", "delivery", "intake")},
        "automation": {"enabled_projects": 1, "active_tasks": 2,
                       "max_active_tasks": state.get("automation_global", {}).get("max_active_tasks", 2),
                       "waiting_merge": 1, "pending_tasks": 16,
                       "last_intake": {"started_at": past(minutes=5), "finished_at": past(minutes=4),
                           "projects": [{"project_id": PROJECTS["Music"], "ready": True,
                                         "adopted": ["auto-waiting-plan"], "preflight": [],
                           "skipped": [{"task_id": "auto-long-pr", "reason": "active_task_exists"}]}], "errors": []}}}


def cmd_executor_instance(state, payload):
    instance = payload.get("instance_id")
    try:
        uuid.UUID(instance)
    except (ValueError, TypeError, AttributeError):
        return fail("实例标识必须是 UUID", "bad_request")
    action = payload.get("action")
    owners = state.setdefault("executor_instances", {})
    current = owners.get(instance)
    if action == "start":
        if current == "stopped": return fail("实例已停止", "conflict")
        owners[instance] = "active"
        save_state(state)
    elif action == "stop":
        owners[instance] = "stopped"
        save_state(state)
    elif action != "status":
        return fail("此界面夹具不执行 worker 登记", "bad_request")
    return {"instance_id": instance, "state": owners.get(instance) or "stopped", "owned_workers": []}


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
    action = {"active": "restore-session", "trashed": "trash-session", "purged": "purge-session"}[target]
    if action not in (session.get("allowed_actions") or []):
        return fail(session.get("blocked_reason") or "该会话操作不可用", "conflict")
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
        task_requests = [r for r in merged_requests(state) if r.get("task_id") == req.get("task_id")]
        monitors = [r for r in task_requests if r.get("kind") == "monitor"]
        latest_monitor = max(enumerate(monitors), key=lambda item: (item[1].get("created_at") or "", item[0]))[1] if monitors else None
        if (task is not None and req.get("kind") == "monitor"
                and task.get("failure_code") == "verification_unconfirmed"
                and "reverify" in allowed_actions(task, merged_requests(state))
                and req.get("finished_at") == task.get("blocked_at")
                and latest_monitor is not None and latest_monitor["request_id"] == rid
                and req.get("claimed_at") and task.get("_execution_started_at")
                and req["claimed_at"] >= task["_execution_started_at"]
                and any(e.get("kind") == "reported" and e.get("task_id") == task["task_id"]
                        and e.get("request_id") == rid and e.get("at") == req.get("finished_at")
                        and (e.get("detail") or {}).get("outcome") == "needs_user"
                        for e in merged_events(state))
                and not any(r.get("status") in ("queued", "claimed") for r in task_requests)):
            actions.insert(0, "reverify")
    if status == "failed" and task is not None and fake_engine_fix_revert(task, merged_requests(state)):
        actions.insert(0, "revert_engine_fix")
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


def fake_project_mappings(state):
    mappings = state.get("project_mappings")
    if mappings:
        return mappings
    return {
        "version": 1,
        "revision": 0,
        "workspace_id": "ws-fake-001",
        "items": {
            pid: {
                "project_name": name,
                "repository": os.path.join(REPOS_ROOT, name),
                "enabled": True,
                "updated_at": past(days=10),
            }
            for name, pid in PROJECTS.items()
        },
    }


def effective_fake_mapping(mappings, project_id):
    item = (mappings.get("items") or {}).get(project_id)
    if not item:
        return {"project_name": "", "repository": None, "enabled": False,
                "mapping_state": "unmapped", "repository_kind": None,
                "blocked_reason": "尚未选择仓库"}
    repository = item.get("repository") or None
    enabled = bool(item.get("enabled", True))
    if not repository:
        state, kind, reason = "unmapped", None, "尚未选择仓库"
    elif not enabled:
        state, kind, reason = "disabled", "git", "已停用新规划"
    else:
        # FakeCore does not inspect real filesystem paths.
        state, kind, reason = "available", "git", None
    return {"project_name": item.get("project_name") or "", "repository": repository,
            "enabled": enabled, "mapping_state": state,
            "repository_kind": kind, "blocked_reason": reason}


def cmd_fetch_todos(state, payload):
    """§5.22.1：只读枚举 To Do（含一个 selectable=false 与聚焦项）。"""
    items = []
    mappings = fake_project_mappings(state)
    for t in merged_tasks(state):
        if t.get("kaneo_status") != "to-do" and t["task_id"] not in state.get("review_remediations", {}):
            continue
        mapping = effective_fake_mapping(mappings, t.get("project_id"))
        removed = t.get("save_state") in ("archived", "trashed", "purged")
        reconnectable = (removed and mapping["mapping_state"] == "available"
                         and t["status"] not in ("planning", "prepared", "dispatching", "submitting", "running", "in_review")
                         and t.get("send_state") != "unknown"
                         and not (t.get("sent") or t.get("send_state") == "sent")
                         and not any(r.get("task_id") == t["task_id"] and r.get("status") in ("queued", "claimed")
                                     for r in merged_requests(state)))
        selectable = not removed and mapping["mapping_state"] == "available" and t["status"] == "needs_plan"
        if mapping["mapping_state"] != "available":
            blocked_reason = mapping["blocked_reason"]
        elif removed:
            label = {"archived": "已归档", "trashed": "已移入回收站", "purged": "已永久移除"}[t["save_state"]]
            blocked_reason = "本地任务%s，不重复规划" % label
        elif t["status"] == "needs_plan":
            blocked_reason = None
        else:
            blocked_reason = "本地已有任务（状态 %s），如需重新规划请先在任务详情确认" % t["status"]
        items.append({
            "task_id": t["task_id"], "title": t.get("title"),
            "project_id": t.get("project_id"), "project_name": t.get("project_name"),
            "requirements_sha256": t.get("requirements_sha256"),
            "source_sha256": t.get("source_sha256"),
            "kaneo_status": t.get("kaneo_status"),
            "repository": mapping["repository"],
            "mapping_state": mapping["mapping_state"],
            "repository_kind": mapping["repository_kind"],
            "mapping_project_id": t.get("project_id"),
            "local_status": {
                "needs_plan": "待规划", "planning": "规划中", "ready": "已有计划",
            }.get(t["status"], t["status"]),
            "local_save_state": t.get("save_state"),
            "attachments": [
                {"remote_id": "att-%s-1" % t["task_id"][-3:],
                 "name": "requirements.txt", "url": "https://kaneo.example.com/a.txt",
                 "media_type": "text/plain", "size": 1280},
            ] if t["task_id"] == "task-music-016" else [],
            "selectable": selectable,
            "reconnectable": reconnectable,
            "blocked_reason": blocked_reason,
        })
    unmapped_id = "proj-unmapped"
    unmapped_mapping = effective_fake_mapping(mappings, unmapped_id)
    unmapped_available = unmapped_mapping["mapping_state"] == "available"
    items.append({
        "task_id": "todo-unmapped-001", "title": "未映射项目的 To Do（fake）",
        "project_id": unmapped_id,
        "project_name": unmapped_mapping["project_name"] or "Unmapped",
        "requirements_sha256": sha("req-unmapped"), "source_sha256": sha("src-unmapped"),
        "kaneo_status": "to-do", "repository": unmapped_mapping["repository"],
        "mapping_state": unmapped_mapping["mapping_state"],
        "repository_kind": unmapped_mapping["repository_kind"],
        "mapping_project_id": unmapped_id, "local_status": None,
        "local_save_state": None, "attachments": [],
        "selectable": unmapped_available,
        "blocked_reason": unmapped_mapping["blocked_reason"],
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
    input_sha = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    input_id = "pin-" + (tid or "x") + "-" + input_sha[:12]
    files = [{
        "name": "source-snapshot.json", "source": "snapshot",
        "media_type": "application/json",
        "sha256": sha("snap-" + tid), "size": 2048,
    }]
    remediation = state.get("review_remediations", {}).get(tid)
    if remediation:
        files.extend([
            {"name": "original-plan.md", "source": "review_remediation", "media_type": "text/markdown",
             "sha256": remediation["plan_sha256"], "size": 1024},
            {"name": "review-report.md", "source": "review_remediation", "media_type": "text/markdown",
             "sha256": remediation["report_sha256"], "size": 2048},
        ])
    for path in payload.get("local_paths") or []:
        files.append({"name": path.split("/")[-1], "source": "local",
                      "media_type": "text/plain",
                      "sha256": sha("local-" + path), "size": 512})
    if payload.get("supplement"):
        files.append({"name": "supplement.md", "source": "supplement",
                      "media_type": "text/markdown",
                      "sha256": hashlib.sha256(payload["supplement"].encode("utf-8")).hexdigest(), "size": 256})
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
            "input_sha256": input_sha,
        }
        append_event(s, "plan_input_prepared", task_id=tid, actor="user",
                     detail={"input_id": input_id, "can_start": can_start})
    mutate(state, apply)
    return {"input_id": input_id,
            "input_sha256": input_sha,
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
                                 "confirmed_at": iso(utcnow()),
                                 "planner_backend": item.get("planner_backend", "codex"),
                                 "planner_method": item.get("planner_method", "builtin")}
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
                        "confirmed_at": iso(utcnow()),
                                 "planner_backend": item.get("planner_backend", "codex"),
                                 "planner_method": item.get("planner_method", "builtin")},
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


def cmd_get_project_mappings(state, payload):
    mappings = fake_project_mappings(state)
    items_list = []
    for pid, item in (mappings.get("items") or {}).items():
        effective = effective_fake_mapping(mappings, pid)
        items_list.append({
            "project_id": pid,
            "project_name": item.get("project_name", ""),
            "repository": effective["repository"] or "",
            "enabled": item.get("enabled", True),
            "updated_at": item.get("updated_at", iso(utcnow())),
            "engine_mode": item.get("engine_mode", "legacy"),
            "origin_default_branch": item.get("origin_default_branch", "master"),
            "delivery_base_exists": None if item.get("delivery_base_unverified") else ((item.get("automation") or {}).get("delivery_base") in (None, "", "master", "develop")),
            "verify_env": item.get("verify_env", {}),
            "fallback_model": item.get("fallback_model"),
            "automation": item.get("automation") or automation_policy(pid == PROJECTS["Music"]),
            "automation_preflight": ([{"code": "origin_not_github", "message": "origin 不是 GitHub 仓库"},
                                      {"code": "review_backend_unsupported", "message": "Review 后端需为 Plus/Claude/Codex"}]
                                     if pid == PROJECTS["Feedback"] else []),
            "mapping_state": effective["mapping_state"],
            "repository_kind": effective["repository_kind"],
            "blocked_reason": effective["blocked_reason"],
            "active_tasks_count": 0,
            "active_requests_count": 0,
        })
    return {
        "revision": mappings.get("revision", 0),
        "workspace_id": mappings.get("workspace_id", "ws-fake-001"),
        "items": items_list,
        "remote_projects_status": "not_requested",
    }


def cmd_set_project_mapping(state, payload):
    pid = payload.get("project_id")
    if not pid:
        return {"__error__": "缺少 project_id", "__code__": "bad_request"}
    mappings = state.setdefault("project_mappings", {
        "version": 1,
        "revision": 0,
        "workspace_id": "ws-fake-001",
        "items": {value: {"project_name": name, "repository": os.path.join(REPOS_ROOT, name), "enabled": True}
                  for name, value in PROJECTS.items()},
    })
    expected_rev = payload.get("expected_revision")
    if expected_rev is not None and expected_rev != mappings.get("revision", 0):
        return {"__error__": "revision 冲突", "__code__": "conflict"}
    items = mappings.setdefault("items", {})
    if payload.get("unmap"):
        items.pop(pid, None)
    else:
        automation = (items.get(pid) or {}).get("automation")
        items[pid] = {
            "automation": automation,
            "project_name": payload.get("project_name") or next((name for name, value in PROJECTS.items() if value == pid), pid),
            "repository": payload.get("repository", ""),
            "enabled": payload.get("enabled", True),
            "updated_at": iso(utcnow()),
        }
    mappings["revision"] = mappings.get("revision", 0) + 1
    save_state(state)
    return {
        "revision": mappings["revision"],
        "project_id": pid,
        "mapping_state": "unmapped" if payload.get("unmap") else
            ("available" if payload.get("repository") and payload.get("enabled", True) else
             "disabled" if payload.get("repository") else "unmapped"),
        "deduplicated": False,
    }



# 自动流水线 UI 全场景夹具；所有写入仍只进入隔离 fake state。
def automation_policy(enabled=False):
    return {"enabled": enabled, "label_filter": ["feedback", "bug"], "auto_confirm_plan": True,
            "max_fix_rounds": 2, "max_task_minutes": 240, "max_task_cost_usd": 10.0,
            "delivery_base": None, "policy_revision": 0, "updated_at": past(days=1),
            "release": {"enabled": False, "channel": "prerelease", "workflow_path": ".github/workflows/pilot-release.yml",
                        "workflow_ref": "main", "version_base": "0.1.0", "required_assets": [], "required_jobs": []}}


def automation_tasks():
    failures = ["verification_unconfirmed", "verification_environment_failure", "fix_branch_unavailable",
                "execution_failed", "fix_rounds_exhausted",
                "auto_budget_exceeded", "automation_stopped", "review_blocked", "review_backend_unsupported",
                "branch_exists", "delivery_base_unknown", "delivery_blocked_files", "delivery_commit_failed",
                "no_changes", "branch_diverged", "gh_unavailable", "pr_closed", "remote_conflict",
                "planner_failed", "needs_input", "dispatch_failed"]
    result = []
    for i, code in enumerate(failures + [None, None, None]):
        tid = "auto-" + (code or ["waiting-plan", "planning", "long-pr"][i - len(failures)])
        status = "needs_user" if code else ["needs_plan", "planning", "in_review"][i - len(failures)]
        actions = ["resume", "replan", "mark_done", "dismiss", "recheck"]
        if code == "verification_unconfirmed": actions = ["retry_verification", "reverify", "accept_verification", "replan", "mark_done", "dismiss"]
        if code == "verification_environment_failure": actions = ["retry_verification", "reverify", "resume", "replan", "dismiss"]
        if code == "no_changes": actions.remove("resume")
        if code in ("planner_failed", "needs_input"): actions = ["replan", "dismiss", "prepare_plan"]
        if code == "dispatch_failed": actions = ["retry", "replan", "dismiss"]
        if code in ("branch_exists", "delivery_base_unknown"): actions = ["resume", "dismiss"]
        if code in ("review_backend_unsupported", "branch_diverged", "gh_unavailable", "pr_closed"): actions = ["resume", "mark_done", "dismiss"]
        stage = ("plan" if code in ("branch_exists", "delivery_base_unknown", "planner_failed", "needs_input") or status in ("needs_plan", "planning")
                 else "delivery" if code in ("delivery_blocked_files", "delivery_commit_failed", "no_changes", "branch_diverged", "gh_unavailable", "pr_closed")
                 else "monitor" if code in ("verification_unconfirmed", "verification_environment_failure", "fix_branch_unavailable", "execution_failed") else "review")
        result.append({"task_id": tid, "title": "自动处理示例 · " + (code or status),
                       "project_id": PROJECTS["Music"], "project_name": "Music", "status": status,
                       "stage": stage, "kaneo_status": "to-do" if stage == "plan" else "in-review",
                       "failure_code": code, "failure_stage": stage if code else None,
                       "failure_reason": "隔离示例：请检查失败证据后选择操作" if code else None,
                       "created_at": past(days=1), "updated_at": past(minutes=i),
                       "requirements_sha256": sha(tid), "_automation_allowed": actions,
                       "automation": {"adopted_at": past(hours=1), "segment_started_at": past(minutes=35),
                                      "fix_round": 1, "max_fix_rounds": 2, "max_task_minutes": 240,
                                      "max_task_cost_usd": 10, "cost_usd": 2.35, "cost_unknown_steps": 1,
                                      "plan_wanted": status == "needs_plan", "manual_takeover": False,
                                      "last_skip": {"reason": "active_task_exists", "at": past(minutes=5)}},
                       "delivery": {"mode": "pr", "branch": "kanpilot/feedback-with-a-long-task-identifier-and-descriptive-branch-name-1234567890",
                                    "base_branch": "main", "worktree_path": "/tmp/kaneopilot-fake-worktrees/" + tid,
                                    "pr_url": "https://github.com/example/long-repository-name-for-responsive-ui-validation/pull/12345",
                                    "pr_state": "open", "pr_number": 12345, "kaneo_comment_at": past(minutes=10),
                                    "last_sync_at": past(minutes=1), "next_sync_at": future(minutes=4),
                                    "commits": [{"sha": sha(tid + "commit", 40), "at": past(minutes=15), "fix_round": 1, "files": 3}]},
                       "review_conclusion": "pass" if not code and status == "in_review" else "changes_requested",
                       "review_history": [{"review_key": sha(tid, 12), "conclusion": "changes_requested",
                                           "registered_at": past(minutes=25), "report_path": "/tmp/fake-review.md",
                                           "head_sha": sha(tid, 40), "execution_run_id": "run-fake-1"}]})
        if code in ("verification_unconfirmed", "verification_environment_failure"):
            run_id = "run-" + tid
            session_id = "session-auto-verification-unconfirmed"
            plan_path = os.path.join(os.environ.get("KANPILOT_PLAN_ROOT", tempfile.gettempdir()), "auto-verification_unconfirmed.plan.md")
            result[-1].update(run_id=run_id, _verification_passed=True,
                              plan_path=plan_path, plan_sha256=VERIFICATION_FIXTURE_PLAN_SHA, plan_kind="standard",
                              plan_generated_at="2026-10-09T07:50:27.000000+00:00",
                              _verification_plan_markdown=VERIFICATION_FIXTURE_PLAN,
                              plan_verify={"plan_sha256": VERIFICATION_FIXTURE_PLAN_SHA, "commands": list(VERIFICATION_FIXTURE_COMMANDS)},
                              session_id=session_id,
                              execution_round={"task_id": tid, "run_id": run_id, "backend": "pi_desktop",
                                               "session_id": session_id, "turn_id": "turn-auto-verification-unconfirmed",
                                               "run_status": "finished", "send_state": "sent", "save_state": "active",
                                               "worktree_path": result[-1]["delivery"]["worktree_path"],
                                               "started_at": "2026-10-09T07:53:27.000000+00:00"},
                              blocked_at=VERIFICATION_FIXTURE_BLOCKED_AT,
                              _execution_started_at="2026-10-09T07:53:27.000000+00:00",
                              failure_reason="隔离示例：验证退出码尚未识别，等待重新核验现有记录",
                              send_state="sent", sent=1,
                              session_marker=tid + "/" + run_id)
        if code in ("branch_exists", "delivery_base_unknown"):
            result[-1].update(run_id="run-" + tid, plan_sha256=sha(tid + "plan", 64), sent=1,
                              sent_at=past(hours=1), send_state="sent", engine_fix_summary={"used": 1, "max": 2},
                              engine_fix_revert={"execution_run_id": "run-" + tid,
                                                 "plan_sha256": sha(tid + "plan", 64), "expected_failure_code": code})
        if code == "verification_environment_failure":
            result[-1]["failure_reason"] = "验证所需 JDK 版本不符，请核对 Plus 原轮次证据或让 Plus 重新执行并验证"
        if code == "fix_branch_unavailable":
            result[-1]["failure_reason"] = "本轮改动在独立工作树，无法同分支修复"
        if code == "execution_failed":
            result[-1].update(run_id="run-auto-execution-failed", plan_path="/tmp/fake-confirmed-plan.md",
                              plan_sha256=sha("confirmed-plan", 64), plan_kind="standard",
                              send_state="sent", sent=1,
                              session_marker="auto-execution_failed/run-auto-execution-failed")
    review = copy.deepcopy(next(t for t in result if t.get("failure_code") == "fix_branch_unavailable"))
    review.update(task_id="auto-fix_branch_unavailable-review", title="评审示例 · 无法同分支修复",
                  stage="review", failure_stage="review", _automation_allowed=["replan", "mark_done", "dismiss"])
    result.append(review)
    manual = copy.deepcopy(next(t for t in result if t.get("failure_code") == "verification_environment_failure"))
    manual.update(task_id="manual-verification-environment", title="人工处理示例 · 验证环境未就绪", automation=None)
    result.append(manual)
    return result


def cmd_set_project_automation(state, payload):
    pid = payload.get("project_id")
    if pid not in PROJECTS.values(): return fail("项目不存在", "not_found")
    mappings = state.setdefault("project_mappings", {"revision": 0, "items": {value: {
        "project_name": name, "repository": os.path.join(REPOS_ROOT, name), "enabled": True
    } for name, value in PROJECTS.items()}})
    item = mappings.setdefault("items", {}).setdefault(pid, {
        "project_name": next((name for name, value in PROJECTS.items() if value == pid), pid),
        "repository": "", "enabled": True})
    current = item.get("automation") or automation_policy(pid == PROJECTS["Music"])
    expected = payload.get("expected_policy_revision")
    if expected is not None and expected != current["policy_revision"]:
        return fail("策略版本冲突，请重新载入后保存", "conflict")
    policy = dict(payload.get("automation") or {})
    base = policy.get("delivery_base")
    check = "unverified" if item.get("delivery_base_unverified") else "verified"
    if base and check != "unverified" and base not in ("master", "develop"):
        return fail("origin 上不存在该分支 %s；可用默认分支为 master" % base, "bad_request")
    policy.update({"policy_revision": current["policy_revision"] + 1, "updated_at": iso(utcnow())})
    item["automation"] = policy
    save_state(state)
    return {"ok": True, "automation": policy, "delivery_base_check": check}


PLANNER_CONFIG = {"backend": "codex", "default_method": "builtin",
                  "standard": {"planner_model": "gpt-6.1-sol", "reasoning_effort": "high"},
                  "review_fix": {"planner_model": "gpt-6.1-sol", "reasoning_effort": "high"},
                  "claude": {"model": "sonnet", "effort": "high"},
                  "qoder": {"model": "auto", "reasoning_effort": "inherit"}}
REVIEW_CONFIG = {"backend": "claude", "mode": "inherit",
                 "claude": {"model": "sonnet", "effort": "high"},
                 "codex": {"model": "gpt-6.1-sol", "reasoning_effort": "high"},
                 "qoder": {"model": "auto", "reasoning_effort": "inherit"}}


def fake_config(state, payload, role, save=False):
    defaults = PLANNER_CONFIG if role == "planner" else REVIEW_CONFIG
    config = dict(defaults, **(state.get(role + "_config") or {}))
    if save:
        config.update(payload)
        if role == "planner" and config.get("backend") == "qoder" and config.get("default_method") != "builtin":
            return fail("Qoder 仅支持内置规划，请先把默认规划方式改为 builtin", "invalid_config")
        state[role + "_config"] = config
        save_state(state)
    return {"config": config, "defaults": dict(defaults), "updated_at": past(minutes=1)}


def desktop_queue_status(state, payload, save=False):
    if save:
        state["desktop_queue_enabled"] = bool(payload.get("enabled"))
        save_state(state)
    config = state.get("desktop_queue_fixture") or {}
    requests = merged_requests(state)
    counts = {role: sum(1 for request in requests
                       if request.get("status") == "queued"
                       and (request.get("backend") or (request.get("payload") or {}).get("planner_backend" if role == "plan" else "review_backend")) == "codex_desktop"
                       and request.get("stage", request.get("kind")) == role)
              for role in ("plan", "review")}
    return {"enabled": state.get("desktop_queue_enabled", False),
            "poll_minutes": config.get("poll_minutes", 0),
            "auto_configured": config.get("auto_configured", False),
            "planner_thread_id": config.get("planner_thread_id"),
            "review_thread_id": config.get("review_thread_id"),
            "queue": config.get("queue", counts),
            "detail": config.get("detail", "隔离 FakeCore 状态；尚未绑定原生自动任务")}


def desktop_automation_status(state, payload, action=None):
    roles = {}
    for role in ("plan", "review"):
        roles[role] = dict({"automation_id": None, "name": "隔离 " + role,
            "status": "MISSING", "enabled": None, "controllable": False,
            "detail": "隔离 FakeCore；没有真实自动任务",
            "rrule": None, "schedule": None, "schedule_summary": "未绑定",
            "schedule_editable": False, "time_zone": "Asia/Taipei"},
            **((state.get("desktop_automation_fixture") or {}).get(role) or {}))
    if action:
        role = payload.get("role")
        current = roles.get(role)
        if not current or not current["controllable"] or current["status"] != payload.get("expected_status"):
            return fail("隔离自动任务状态已变化，请刷新", "conflict")
        if action == "enabled":
            current.update(enabled=bool(payload.get("enabled")),
                           status="ACTIVE" if payload.get("enabled") else "PAUSED")
        else:
            if current["rrule"] != payload.get("expected_rrule"):
                return fail("隔离时间规则已变化，请刷新", "conflict")
            schedule = payload.get("schedule") or {}
            current.update(schedule=schedule, rrule="fixture-rule", schedule_summary="隔离规则已更新")
        state["desktop_automation_fixture"] = roles
        save_state(state)
    return {"supported": True, "detail": "隔离 FakeCore 自动任务状态", "roles": roles}


def qoder_capability(state):
    return dict({"available": True, "version": "fake",
                 "detail": "隔离 FakeCore 接口探测"},
                **(state.get("qoder_capability_fixture") or {}))


def qoder_model_catalog(state, payload):
    if state.get("qoder_catalog_delay_ms"):
        time.sleep(min(float(state["qoder_catalog_delay_ms"]) / 1000, 5))
    if state.get("qoder_catalog_error"):
        return fail(str(state["qoder_catalog_error"]), "validation_failed")
    return state.get("qoder_catalog_fixture", {"multiplier_source": "qoder_cli_live", "models": [
        {"id": "Auto", "name": "Auto", "billing_type": "qoder_credits",
         "credit_multiplier": 0.5, "original_credit_multiplier": None},
        {"id": "Ultimate", "name": "Ultimate", "billing_type": "qoder_credits",
         "credit_multiplier": 0.4, "original_credit_multiplier": 2},
        {"id": "DeepSeek-Flash", "name": "DeepSeek-Flash", "billing_type": "qoder_credits",
         "credit_multiplier": 0, "original_credit_multiplier": 0.1},
        {"id": "Unknown-Rate", "name": "Unknown Rate", "billing_type": "qoder_credits",
         "credit_multiplier": None, "original_credit_multiplier": None},
        {"id": "qoder-custom-fixture/deepseek/deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash",
         "billing_type": "external", "credit_multiplier": None, "original_credit_multiplier": None},
    ]})


def review_capabilities(state):
    result = {key: {"available": True, "detail": "隔离 FakeCore 就绪"}
              for key in ("pi", "pi_desktop", "claude", "codex")}
    result["qoder"] = qoder_capability(state)
    return result


def planner_capabilities(state):
    ready = {"available": True, "tools_ok": True, "version": "fake", "detail": "隔离 FakeCore 就绪"}
    return {"codex": {"path": "/tmp/fake-codex", "version": "fake"},
            "auth": {"ready": True, "detail": "隔离夹具"},
            "models_verified": True, "models": [{"id": "gpt-6.1-sol", "name": "6.1 Sol", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]}],
            "planner_methods": {"builtin": ready, "spec_plan": ready},
            "backends": {"codex": ready, "codex_desktop": ready, "claude": ready, "pi_desktop": ready,
                         "qoder": qoder_capability(state)}}

def cmd_session_control_status(state, payload):
    marker = "%s/%s" % (payload.get("task_id"), payload.get("run_id"))
    scenario = fixture_scenario(state, marker)
    configured = scenario.get("control") or {}
    if scenario.get("control_delay_ms"):
        time.sleep(min(float(scenario["control_delay_ms"]) / 1000, 5))
    if scenario.get("control_error"):
        return fail(str(scenario["control_error"]), "remote_unavailable")
    supported = scenario.get("backend") == "pi_desktop"
    return dict({"task_id": payload.get("task_id"), "run_id": payload.get("run_id"),
                 "request_id": "fixture-plan-request", "backend": scenario.get("backend") or "pi",
                 "state": "executor_lost" if supported else "unsupported",
                 "detail": "规划进程已退出，尚未创建 Plus 会话" if supported else "该轮次不使用 Plus",
                 "connected": supported, "session_id": None, "turn_id": None,
                 "can_start": supported, "blocked_reason": None if supported else "非 Plus 轮次",
                 "start_label": "请求开始"}, **configured)


def cmd_session_request_start(state, payload):
    status = cmd_session_control_status(state, payload)
    if not status["can_start"] or payload.get("request_id") != status["request_id"]:
        return fail("该轮次不能请求开始", "conflict")
    marker = "%s/%s" % (payload.get("task_id"), payload.get("run_id"))
    scenario = state.setdefault("session_scenarios", {}).setdefault(marker, {})
    repeated = bool(scenario.get("control", {}).get("queued"))
    scenario.setdefault("control", {}).update(state="not_started", detail="开始请求已排队，等待规划器领取", queued=True)
    save_state(state)
    return {"request_id": status["request_id"], "stage": "plan", "queued": True, "deduplicated": repeated}


def cmd_recover_execution_goals_fixture(state, payload):
    """Isolated owned recovery: preserve normal goals and opt-in release progress."""
    delay = state.get("recovery_fixture_delay_seconds", 0)
    if delay:
        time.sleep(min(max(float(delay), 0), 60))
    results = []
    config = state.get("external_build_repair_fixture") or {}
    if (config.get("auto_progress_fixture") and config.get("permission_mode") == "auto"
            and config.get("accepted") and config.get("verification_starts")):
        task = release_fixture_task(state)
        verification = external_verification_view(state)
        # Only a confirmed, current attempt progresses. Unknowns are observed;
        # this fixture never creates a prompt, attempt or approval to recover one.
        if (task and task.get("save_state") == "active"
                and config.get("confirmed_verification_id") == verification["verification_id"]
                and verification["state"] in ("planning", "running", "unknown")):
            result = cmd_sync_release_verification_fixture(state, {"task_id": task["task_id"],
                "verification_id": verification["verification_id"]})
            results.append(dict(result, task_id=task["task_id"]))
    return {"goal_recoveries": [], "release_verifications": results}


def cmd_execution_answers(state, payload, mode):
    """Native form fixture only: no real core, SQLite, Plus, model or service."""
    tid = payload.get("task_id")
    task = next((t for t in merged_tasks(state) if t["task_id"] == tid), None)
    question = copy.deepcopy((task or {}).get("execution_question"))
    if not question:
        return fail("执行提问不存在", "not_found")
    if mode in ("save", "submit") and (question["request_id"] != payload.get("request_id")
            or question["questions_sha256"] != payload.get("questions_sha256")):
        return fail("执行提问身份已变化", "conflict")
    if mode != "save" and question["run_id"] != payload.get("run_id"):
        return fail("执行轮次已变化", "stale_run")
    status = question["delivery"]
    if mode == "save":
        question["draft"] = copy.deepcopy(payload["answers"])
    elif mode == "submit":
        if question["delivery"] == "submitting": return fail("正在提交", "conflict")
        values = ([{"question_id": item["id"], "option_ids": [], "text": "", "skipped": True}
                   for item in question["questions"]] if payload.get("skip_all") else payload["answers"])
        if question["delivery"] == "unknown" and values != question["submitted"]["answers"]:
            return fail("送达未知期间答案已冻结", "conflict")
        question["draft"] = copy.deepcopy(values)
        question["submitted"] = {"answers": copy.deepcopy(values), "operation_id": payload["operation_id"],
                                 "at": iso(utcnow()), "by": "native_ui_fixture"}
        status = state.get("execution_fixture_submit_result", "accepted")
        question["delivery"] = status
        if status == "accepted": question.update(state="answered", answered_by="kaneopilot", closed_at=iso(utcnow()))
    elif mode == "stop":
        status = state.get("execution_fixture_stop_result", "accepted")
        if status != "unknown": question.update(state="stopped", closed_at=iso(utcnow()), detail="用户停止了本轮（隔离夹具）")
    elif mode == "observe":
        question["observer_alive"] = True
        status = "observing"
    patch = state.setdefault("task_overrides", {}).setdefault(tid, {})
    patch["execution_question"] = question
    if question["state"] == "answered":
        queued = patch.get("execution_question_queue") or []
        if queued:
            question = queued.pop(0)
            patch["execution_question"] = question
            patch["blocked_summary"] = "执行中提问：%s（共 %s 题）" % (question["questions"][0]["title"], len(question["questions"]))
        else: patch.update(status="running", display_status="执行中", failure_code=None, blocked_summary=None, blocked_reason=None)
    elif question["state"] in ("expired", "stopped"):
        patch.update(failure_code="execution_failed", blocked_summary=question.get("detail") or "本轮已结束")
    save_state(state)
    return {"task_id": tid, "run_id": question["run_id"], "execution_question": question, "status": status}


def cmd_start_review_repair_fixture(state, payload):
    task = next((t for t in merged_tasks(state) if t["task_id"] == payload.get("task_id")), None)
    repair = (task or {}).get("review_repair") or {}
    expected = {"expected_execution_run_id": "source_execution_run_id",
                "expected_review_key": "review_key", "expected_report_sha256": "report_sha256",
                "expected_plan_sha256": "plan_sha256"}
    if not repair.get("can_start") or any(payload.get(k) != repair.get(v) for k, v in expected.items()):
        return fail("隔离修复身份已变化", "conflict")
    patch = state.setdefault("task_overrides", {}).setdefault(task["task_id"], {})
    patch.update(status="ready", stage="dispatch", exception=None, review_conclusion=None,
                 review_repair=dict(repair, state="queued", can_start=False, round=repair["round"] + 1,
                                    reason="修复会话待启动"))
    state.setdefault("extra_requests", []).append(dict(request_id="fixture-repair-dispatch", task_id=task["task_id"],
        kind="dispatch", stage="dispatch", status="queued", priority=100, attempt_no=1, requested_by="user",
        created_at=past(), updated_at=past()))
    save_state(state)
    return {"ok": True, "request_id": "fixture-repair-dispatch"}


# Opt-in release scenarios; all commands only read/write this isolated fake state.
# {"release_fixture":{"scenario":"ready|waiting_group|dispatching|unknown|queued|building|publishing|blocked|failed|published",
#   "historical_done":false,"start_result":"queued","sync_result":"published",
#   "not_sent":false,"can_start":true,"can_abandon":false}}
def apply_release_fixture(state, tasks):
    config = state.get("release_fixture")
    if not isinstance(config, dict):
        return
    task = tasks.get(config.get("task_id", "auto-long-pr"))
    if not task:
        return
    scenario = config.get("scenario", "ready")
    titles = {"ready": "待构建发布", "waiting_group": "等待关联任务交付", "dispatching": "正在安排构建",
              "unknown": "发布结果待确认", "queued": "构建已排队", "building": "构建中",
              "publishing": "发布中", "blocked": "发布受阻", "failed": "构建失败", "published": "版本已发布"}
    if scenario not in titles:
        scenario = "ready"
    reasons = {"ready": "已有验证、评审及合并证据，可以准备构建发布。",
               "waiting_group": "等待同一批次的二级修复具备完整交付证据。",
               "dispatching": "已保存构建意图，等待 GitHub Actions 回执。",
               "unknown": "发送回执不明，仅核对已有运行，不会重复发送。",
               "queued": "GitHub Actions 正在等待执行资源。", "building": "正在构建 Android、macOS 和 iOS unsigned 安装包。",
               "publishing": "构建已通过，正在核对版本与安装包。", "blocked": "必需安装包缺少 SHA256 digest，不能确认发布。",
               "failed": "iOS 构建任务失败，请在 Actions 查看日志或重跑后同步。",
               "published": "已核验工作流、版本标签及全部必需安装包。"}
    historical = bool(config.get("historical_done"))
    not_sent = bool(config.get("not_sent")) and scenario != "unknown"
    batch = None if scenario in ("ready", "waiting_group") and not not_sent else "fixture-release-batch"
    tag = "v0.1.0" if config.get("channel") == "stable" else "v0.1.0-pilot.1792396800"
    url = "https://github.com/example/music/releases/download/" + tag + "/"
    assets = [{"name": name, "url": url + name, "size": 42123456, "sha256": sha(name)}
              for name in ["Music-android-universal.apk", "Music-macOS-universal-desktop-very-long-artifact-name-for-wrapping-validation.zip", "Music-iOS-unsigned.ipa"]] if scenario == "published" else []
    task["release"] = {"state": scenario, "title": titles[scenario], "reason": reasons[scenario], "required": not historical,
        "channel": config.get("channel", "prerelease"), "context_sha256": sha("release-context-%s" % config.get("revision", 0)),
        "batch_id": batch, "root_task_id": task["task_id"], "member_task_ids": [task["task_id"]],
        "target_sha": sha("release-target", 40), "version": "0.1.0", "tag": tag, "build_number": 1792396800,
        "workflow_run_id": 12345 if batch and not not_sent else None,
        "workflow_url": "https://github.com/example/music/actions/runs/12345" if batch and not not_sent else None,
        "release_url": "https://github.com/example/music/releases/tag/" + tag if scenario == "published" else None,
        "checked_at": past(minutes=1) if batch else None, "assets": assets,
        "blockers": [{"code": "asset_digest_missing", "message": "Music-iOS-unsigned.ipa 缺少 SHA256 digest"}] if scenario == "blocked" else [],
        "can_start": scenario != "unknown" and bool(config.get("can_start", scenario == "ready" or not_sent)),
        "can_reconcile": batch is not None and not not_sent,
        "can_abandon": not_sent and bool(config.get("can_abandon"))}
    done = historical or bool(config.get("task_done"))
    task.update(title="构建发布示例 · 合并后的安装包交付", review_repair=None, review_resolution=None, parent_closeout=None,
                status="done" if done else "in_review", stage="done" if done else "delivery",
                phase="done" if done else "delivery", condition="merged" if done else "syncing",
                display_status="已完成" if done else titles[scenario], board_substatus="已完成" if done else titles[scenario],
                board_column="done" if done else "in_review", lifecycle_column="done" if done else "in_review",
                kaneo_status="done" if done else "in-review", active_request=None, attention=None,
                review_conclusion="pass", review_summary={"conclusion": "pass"}, task_ai_action=None,
                blocked_reason=reasons[scenario] if scenario in ("blocked", "unknown", "failed") else None,
                exception=None if done or scenario not in ("blocked", "unknown", "failed") else {
                    "kind": "release_" + scenario, "label": titles[scenario], "severity": "failed" if scenario == "failed" else "blocked",
                    "detail": reasons[scenario]}, last_progress=reasons[scenario])
    delivery = dict(task.get("delivery") or {}, mode="pr", pr_state="MERGED", base_branch="main", auto_merge=None)
    task["delivery"] = delivery


def release_fixture_task(state):
    config = state.get("release_fixture") or {}
    return next((task for task in merged_tasks(state) if task["task_id"] == config.get("task_id", "auto-long-pr")), None)


# Opt-in external repair fixture. State is isolated by KANPILOT_FAKE_STATE.
# Seed release_fixture.scenario=failed plus external_build_repair_fixture={}.
def external_repair_candidate(state, task_id):
    config = state.get("external_build_repair_fixture") or {}
    context = sha("external-repair-%s" % config.get("revision", 0))
    return {"task_id": task_id, "pr_number": config.get("pr_number", 11),
            "pr_url": "https://github.com/example/music/pull/%s" % config.get("pr_number", 11),
            "head_sha": sha("external-head", 40), "base_branch": "main", "target_sha": sha("external-target", 40),
            "target_source_sha256": sha("external-source"), "source_batch_id": "fixture-release-batch",
            "source_tag": "v0.1.0-pilot.1792396800", "source_target_sha": sha("release-target", 40),
            "source_workflow_run_id": 12345, "source_workflow_attempt": 1,
            "preservation_kind": config.get("preservation_kind", "ancestor"), "context_sha256": context}


def external_verification_view(state):
    config = state["external_build_repair_fixture"]
    status = config.get("verification_state", "prepared")
    attempt = config.get("verification_restarts", 0)
    context_seed = "external-verification-%s" % config.get("revision", 0)
    if attempt:
        context_seed += "-attempt-%s" % attempt
    verification_id = "fixture-external-verification" + ("-%s" % (attempt + 1) if attempt else "")
    reason = {"prepared": "尚未调用模型，等待确认完整验证。", "planning": "独立 Plus 验证已安排。",
              "running": "Pilot 已批准本轮 Goal，Plus 正在执行，报告将持续自动核对。", "executing": "正在执行完整验证。", "verified": "原需求、计划验收与全部命令均已核对通过。",
              "blocked": "完整验证尚未通过，不能准备发布。"}.get(status, "等待核对验证结果。")
    failure_code = "release_verification_report" if status == "blocked" and config.get("report_failure") else None
    if config.get("continuation_scenario") == "completed_goal_missing_receipt" and not attempt:
        if config.get("goal_observed"):
            reason = "验证证据不足：四条检查未确认，13 条引用未解析；原报告保留，需显式重新完整验证。"
        else:
            reason = "Plus 中的原 Goal 已完成，Pilot 尚未核对原会话与报告。"
    return {"state": status, "target_sha": sha("external-target", 40), "target_source_sha256": sha("external-source"),
            "context_sha256": sha(context_seed),
            "verification_id": verification_id if config.get("verification_starts") else None,
            "commands": ["python3 -m pytest tests -q", "./gradlew :app:assembleDebug"],
            "model_summary": {"provider_id": "fixture", "model_id": "fixture-model", "thinking_level": "high", "permission_mode": config.get("permission_mode", "default")},
            "reason": reason, "failure_code": failure_code,
            "report_goal_status": "met" if config.get("goal_observed") else None}


def apply_external_build_repair_fixture(state, tasks):
    config = state.get("external_build_repair_fixture")
    if not isinstance(config, dict):
        return
    task = tasks.get(config.get("task_id", "auto-long-pr"))
    if not task or not task.get("release"):
        return
    release = task["release"]
    release["can_link_external_repair"] = not config.get("accepted", False) and release["state"] == "failed"
    if not config.get("accepted"):
        return
    candidate = external_repair_candidate(state, task["task_id"])
    verified = config.get("verification_state") == "verified"
    repair = {key: candidate[key] for key in ("pr_number", "pr_url", "target_sha", "source_batch_id", "source_tag", "context_sha256")}
    repair.update(can_prepare_verification=not config.get("verification_starts"),
                  can_sync_verification=bool(config.get("verification_starts")) and (config.get("verification_state") not in ("verified", "blocked", "failed") or bool(config.get("recovery_pending"))),
                  can_restart_verification=config.get("verification_state") == "blocked" and bool(config.get("report_failure")) and not config.get("recovery_pending"),
                  verification=external_verification_view(state) if config.get("verification_starts") else None)
    release.update(external_repair=repair, can_reconcile=bool(config.get("batch_started")), can_abandon=False,
                   can_start=verified and not config.get("batch_started"), is_historical=False,
                   target_sha=candidate["target_sha"], context_sha256=sha("external-release-%s" % config.get("revision", 0)))
    if not config.get("batch_started"):
        verification = repair.get("verification") or {}
        status = verification.get("state")
        title = {"planning": "核对原验证" if config.get("continuation_scenario") and not config.get("goal_observed") else "验证规划中",
                 "running": "验证执行中", "executing": "完整验证中", "unknown": "验证结果待确认",
                 "blocked": "验证证据不足" if config.get("report_failure") else "完整验证受阻",
                 "failed": "完整验证失败"}.get(status, "待完整验证")
        release.update(state="ready" if verified else "blocked", title="待新批次发布" if verified else title,
                       reason="修复目标完整验证已通过，可以准备新批次。" if verified else verification.get("reason") or "已关联外部修复，需独立完整验证后再准备新批次。",
                       batch_id=None, tag=None, workflow_run_id=None, workflow_url=None)
    else:
        release.update(batch_id="fixture-external-new-batch", tag="v0.1.0-pilot.1792396900")
    task.update(task_ai_action=None, display_status=release["title"], board_substatus=release["title"],
                last_progress=release["reason"], exception=None, blocked_reason=None)


def cmd_prepare_external_build_repair_fixture(state, payload):
    config = state.get("external_build_repair_fixture")
    task = release_fixture_task(state)
    if not isinstance(config, dict) or not task or task["task_id"] != payload.get("task_id"):
        return fail("外部修复任务不匹配", "conflict")
    if config.get("prepare_error"):
        return fail(config["prepare_error"], "conflict")
    candidate = external_repair_candidate(state, task["task_id"])
    if payload.get("pr_number") != candidate["pr_number"]:
        return fail("修复 PR 未合并或不属于同一仓库", "conflict")
    if not config.get("accepted") and not task["release"].get("can_link_external_repair"):
        return fail("当前失败批次不允许关联", "conflict")
    return {"task_id": task["task_id"], "context_sha256": candidate["context_sha256"], "candidate": candidate}


def cmd_accept_external_build_repair_fixture(state, payload):
    prepared = cmd_prepare_external_build_repair_fixture(state, payload)
    if payload.get("confirm") is not True or payload.get("expected_context_sha256") != prepared["context_sha256"]:
        return fail("修复候选已变化，请重新核对", "conflict")
    config = state["external_build_repair_fixture"]
    if not config.get("accepted"):
        config["accepted"] = True
        config["accepts"] = config.get("accepts", 0) + 1
        save_state(state)
    return {"ok": True, "repair": release_fixture_task(state)["release"]["external_repair"]}


def cmd_prepare_release_verification_fixture(state, payload):
    task = release_fixture_task(state)
    repair = ((task or {}).get("release") or {}).get("external_repair") or {}
    if not task or task["task_id"] != payload.get("task_id") or not repair.get("can_prepare_verification"):
        return fail("当前不可准备完整验证", "conflict")
    verification = external_verification_view(state)
    return {"task_id": task["task_id"], "context_sha256": verification["context_sha256"], "verification": verification}


def cmd_start_release_verification_fixture(state, payload):
    config = state.get("external_build_repair_fixture") or {}
    if not config.get("accepted"):
        return fail("尚未关联修复，不能启动完整验证", "conflict")
    task = release_fixture_task(state)
    verification = external_verification_view(state)
    if not task or task["task_id"] != payload.get("task_id") or not config.get("accepted") or payload.get("expected_context_sha256") != verification["context_sha256"]:
        return fail("验证候选已变化，请重新准备", "conflict")
    if not config.get("verification_starts"):
        config.update(verification_starts=1, verification_state="planning")
        if config.get("auto_progress_fixture"):
            config["confirmed_verification_id"] = external_verification_view(state)["verification_id"]
            config["prompts"] = config.get("prompts", 0) + 1
        save_state(state)
    return {"verification_id": external_verification_view(state)["verification_id"], "verification": external_verification_view(state)}


def cmd_sync_release_verification_fixture(state, payload):
    config = state.get("external_build_repair_fixture") or {}
    task = release_fixture_task(state)
    if not task or task["task_id"] != payload.get("task_id") or not config.get("verification_starts") or payload.get("verification_id") != external_verification_view(state)["verification_id"]:
        return fail("验证会话身份不匹配", "conflict")
    config["verification_syncs"] = config.get("verification_syncs", 0) + 1
    if config.get("auto_progress_fixture") and config.get("permission_mode") == "auto":
        if config.get("verification_state") == "planning":
            config["goal_approvals"] = config.get("goal_approvals", 0) + 1
            config["verification_state"] = "running"
        elif config.get("verification_state") == "running":
            config["verification_state"] = config.get("sync_result", "verified")
        # unknown is a read-only observation, never another approval or prompt.
    elif config.get("continuation_scenario") == "completed_goal_missing_receipt" and not config.get("verification_restarts"):
        config.update(verification_state="blocked", report_failure=True, goal_observed=True)
    else:
        config["verification_state"] = config.get("sync_result", "verified")
    save_state(state)
    return {"verification_id": external_verification_view(state)["verification_id"], "verification": external_verification_view(state)}


def cmd_prepare_restart_release_verification_fixture(state, payload):
    config = state.get("external_build_repair_fixture") or {}
    task = release_fixture_task(state)
    repair = ((task or {}).get("release") or {}).get("external_repair") or {}
    if not task or task["task_id"] != payload.get("task_id") or not repair.get("can_restart_verification"):
        return fail("当前原报告不允许重启，请先核对已有验证结果", "conflict")
    old = external_verification_view(state)
    attempt = config.get("verification_restarts", 0) + 1
    context = sha("external-verification-%s-attempt-%s" % (config.get("revision", 0), attempt))
    verification = dict(old, state="prepared", context_sha256=context, verification_id=None,
                        reason="仅准备新的独立尝试，原报告保留；确认后可能产生模型费用。")
    return {"task_id": task["task_id"], "context_sha256": context, "verification": verification,
            "expected_verification_id": old["verification_id"], "expected_context_sha256": old["context_sha256"],
            "expected_report_sha256": sha("external-report-%s-%s" % (config.get("revision", 0), attempt - 1))}


def cmd_restart_release_verification_fixture(state, payload):
    config = state.get("external_build_repair_fixture") or {}
    identity = {key: payload.get(key) for key in ("task_id", "expected_verification_id", "expected_context_sha256", "expected_report_sha256")}
    if config.get("restart_receipt") == identity:
        verification = external_verification_view(state)
        return {"verification_id": verification["verification_id"], "verification": verification}
    prepared = cmd_prepare_restart_release_verification_fixture(state, payload)
    if any(payload.get(key) != prepared[key] for key in ("expected_verification_id", "expected_context_sha256", "expected_report_sha256")):
        return fail("原验证或报告已变化，请重新准备", "conflict")
    config["verification_restarts"] = config.get("verification_restarts", 0) + 1
    config["verification_starts"] = config.get("verification_starts", 0) + 1
    config.update(verification_state="planning", restart_receipt=identity)
    if config.get("auto_progress_fixture"):
        config["confirmed_verification_id"] = external_verification_view(state)["verification_id"]
        config["prompts"] = config.get("prompts", 0) + 1
    save_state(state)
    verification = external_verification_view(state)
    return {"verification_id": verification["verification_id"], "verification": verification}


def cmd_prepare_task_release_fixture(state, payload):
    task = release_fixture_task(state)
    if not task or task["task_id"] != payload.get("task_id"):
        return fail("当前发布候选不可启动，请刷新核对", "conflict")
    release = copy.deepcopy(task["release"])
    if not release["can_start"]:
        return {"task_id": task["task_id"], "context_sha256": None, "release": release}
    return {"task_id": task["task_id"], "context_sha256": release["context_sha256"], "release": release}


def cmd_start_task_release_fixture(state, payload):
    task = release_fixture_task(state)
    release = (task or {}).get("release") or {}
    if not task or task["task_id"] != payload.get("task_id") or payload.get("expected_context_sha256") != release.get("context_sha256"):
        return fail("发布候选身份已变化，请重新准备", "conflict")
    if not release.get("can_start"):
        return {"outcome": release["state"], "release": release}
    if state.get("external_build_repair_fixture", {}).get("accepted"):
        state["external_build_repair_fixture"]["batch_started"] = True
    config = state["release_fixture"]
    config["dispatches"] = config.get("dispatches", 0) + 1
    config["not_sent"] = False
    config.pop("can_start", None)
    config["scenario"] = config.get("start_result", "queued")
    save_state(state)
    release = release_fixture_task(state)["release"]
    return {"outcome": release["state"], "release": release}


def cmd_abandon_unsent_release_fixture(state, payload):
    task = release_fixture_task(state)
    release = (task or {}).get("release") or {}
    if (not release.get("can_abandon") or release.get("state") == "unknown"
            or release.get("batch_id") != payload.get("batch_id")
            or release.get("context_sha256") != payload.get("expected_context_sha256")):
        return fail("批次不可放弃或身份已变化", "conflict")
    config = state["release_fixture"]
    config.update(scenario="ready", not_sent=False, can_abandon=False, revision=config.get("revision", 0) + 1)
    config.pop("can_start", None)
    config["abandoned"] = config.get("abandoned", 0) + 1
    save_state(state)
    abandoned = dict(release, required=False, can_start=False, can_abandon=False, can_reconcile=False,
                     reason="已放弃未发送批次，原任务和历史记录保留。")
    return {"outcome": "abandoned", "release": abandoned}


def cmd_sync_release_fixture(state, payload):
    task = release_fixture_task(state)
    release = (task or {}).get("release") or {}
    if not release.get("batch_id") or release["batch_id"] != payload.get("batch_id"):
        return fail("发布批次不匹配", "conflict")
    config = state["release_fixture"]
    config["syncs"] = config.get("syncs", 0) + 1
    config["scenario"] = config.get("sync_result", release["state"])
    save_state(state)
    release = release_fixture_task(state)["release"]
    return {"outcome": release["state"], "release": release}


def cmd_sync_releases_fixture(state, payload):
    task = release_fixture_task(state)
    release = (task or {}).get("release") or {}
    if not release.get("batch_id"):
        return {"batches": []}
    return {"batches": [cmd_sync_release_fixture(state, {"batch_id": release["batch_id"]})]}


# Opt-in isolated closeout fixture; no network, real models or repository writes.
# {"parent_closeout_fixture":{"scenario":"ready|blocked|unknown|completed|waiting_children"}}
def apply_parent_closeout_fixture(state, tasks):
    config = state.get("parent_closeout_fixture")
    if not isinstance(config, dict):
        return
    task = tasks.get(config.get("task_id", "task-study-017"))
    if not task:
        return
    scenario = config.get("scenario", "ready")
    if scenario not in ("ready", "blocked", "unknown", "completed", "waiting_children"):
        scenario = "ready"
    titles = {"ready": "待收尾校验", "blocked": "收尾校验受阻", "unknown": "完成同步待确认",
              "completed": "已完成", "waiting_children": "等待二级任务完成"}
    reasons = {"ready": "二级修复任务已完成，可核对已有验证、评审与合并证据后完成原任务。",
               "blocked": "尚缺最终整合检查点，保留原任务与历史评审。",
               "unknown": "远端完成结果尚未确认，请只读核对；不会重复提交。",
               "completed": "已复用最终通过检查点，原功能与修复均已进入目标分支。",
               "waiting_children": "仍有二级修复任务未完成，暂不能进行收尾校验。"}
    closeout = {"state": scenario, "title": titles[scenario], "reason": reasons[scenario],
                "context_sha256": sha("parent-closeout-%s" % config.get("context_revision", 0)),
                "can_complete": scenario in ("ready", "blocked", "unknown"), "required_children": 2,
                "completed_children": 1 if scenario == "waiting_children" else 2,
                "checked_at": None if scenario in ("ready", "waiting_children") else past(minutes=1),
                "blockers": [{"code": "final_checkpoint_missing", "message": "缺少覆盖全部修复的最终通过检查点"}] if scenario == "blocked" else [],
                "evidence_task_id": "task-study-021" if scenario == "completed" else None,
                "target_branch": "main" if scenario == "completed" else None,
                "target_sha": sha("closeout-target", 40) if scenario == "completed" else None,
                "acceptance_note": "本次仅确认软件交付与已有自动验证；原计划的人工、真机及真实服务体验仍未核验。"}
    task.update(parent_closeout=closeout, review_repair=None, review_resolution=None,
                status="done" if scenario == "completed" else "in_review", stage="review",
                phase="done" if scenario == "completed" else "review" if scenario == "waiting_children" else "delivery",
                condition="merged" if scenario == "completed" else "reviewing" if scenario == "waiting_children" else "syncing",
                display_status=titles[scenario], board_substatus=titles[scenario],
                board_column="done" if scenario == "completed" else "in_review",
                lifecycle_column="done" if scenario == "completed" else "in_review",
                blocked_reason=reasons[scenario] if scenario in ("blocked", "unknown") else None,
                attention=None, exception=({"kind": "parent_closeout_blocked", "label": titles[scenario],
                    "severity": "blocked", "detail": reasons[scenario]} if scenario in ("blocked", "unknown") else None),
                kaneo_status="done" if scenario == "completed" else "in-review", active_request=None,
                last_progress=reasons[scenario], task_relations={"parent": None, "children": [
                    {"task_id": "task-study-021", "title": "修复标签缓存及目录切换", "display_status": "已完成", "available": True},
                    {"task_id": "fixture-closeout-child-2", "title": "补齐有界读取与验证", "display_status": "执行中" if scenario == "waiting_children" else "已完成", "available": True}]})
    for child in task["task_relations"]["children"]:
        target = tasks.setdefault(child["task_id"], {"task_id": child["task_id"], "project_id": task["project_id"],
                    "project_name": task["project_name"], "save_state": "active", "title": child["title"]})
        target.update(status="running" if child["display_status"] == "执行中" else "done",
                      display_status=child["display_status"], board_column="in_progress" if child["display_status"] == "执行中" else "done",
                      exception=None, task_relations={"parent": {"task_id": task["task_id"], "title": task["title"],
                          "display_status": titles[scenario], "available": True}, "children": []})


def cmd_closeout_parent_fixture(state, payload):
    task = next((item for item in merged_tasks(state) if item["task_id"] == payload.get("task_id")), None)
    closeout = (task or {}).get("parent_closeout")
    if not closeout or payload.get("expected_context_sha256") != closeout["context_sha256"]:
        return fail("隔离收尾身份已变化，请刷新后重试", "conflict")
    if closeout["state"] == "waiting_children":
        return fail("二级任务尚未完成", "conflict")
    config = state["parent_closeout_fixture"]
    config["calls"] = config.get("calls", 0) + 1
    scenario = config.get("result", "completed")
    config["scenario"] = scenario if scenario in ("blocked", "unknown", "completed") else "completed"
    save_state(state)
    result = next(item for item in merged_tasks(state) if item["task_id"] == task["task_id"])["parent_closeout"]
    return {"outcome": result["state"], "parent_closeout": result}


# Opt-in isolated native UI scenarios. State: {"task_ai_action_fixture":
# {"scenario":"candidate|queued|running|awaiting_review|history|blocked|failed|readonly|verification_unconfirmed",
#  "prepare_delay":0, "context_revision":0, "config_revision":0}}.
# Scenario can be changed in the state file while the app runs; all commands are fake.
def apply_task_ai_action_fixture(state, tasks):
    fixture = state.get("task_ai_action_fixture")
    if not isinstance(fixture, dict):
        return
    task = tasks.get("auto-long-pr")
    if not task:
        return
    scenario = fixture.get("scenario", "candidate")
    kind = fixture.get("kind", "merge_conflict")
    build = kind == "build_failure"
    delivery = dict(task["delivery"], pr_state="MERGED" if build else "OPEN", head_sha=sha("ai-fixture-head", 40))
    delivery["auto_merge"] = None if build else {"state": "blocked", "reason": "PR 存在合并冲突，需处理后重新校验",
                                               "head_sha": delivery["head_sha"], "updated_at": past(minutes=1)}
    task.update(title="一次性 AI 处理 · 构建失败（隔离示例）" if build else "一次性 AI 处理 · PR 合并冲突（隔离示例）", delivery=delivery,
                run_id="run-ai-source", review_repair=None, review_conclusion="pass",
                review_summary={"conclusion": "pass"}, phase="delivery", condition="syncing",
                status="in_review", display_status="构建失败" if build else "评审通过 · 待合并",
                board_substatus="构建失败" if build else "评审通过 · 待合并")
    saved = fixture.get("started")
    current = copy.deepcopy(saved.get("entry")) if saved else None
    titles = {"queued": "AI 处理待启动", "running": "AI 正在处理", "awaiting_review": "处理后待复审",
              "completed": "已完成", "needs_followup": "需要后续处理", "blocked": "受阻", "failed": "失败"}

    def entry(key, action_state):
        return {"action_id": key, "kind": kind, "title": "修复构建失败" if build else "解决 PR 合并冲突",
                "state": action_state, "state_title": titles[action_state], "created_at": "2026-10-19T02:00:00+00:00",
                "execution_run_id": "run-" + key, "session_id": "session-" + key if action_state != "queued" else None,
                "model_summary": "隔离通用执行配置 · fake-model / high（无模型调用）", "pr_number": 12345,
                "source_pr_number": 12345 if build else None,
                "source_pr_url": "https://github.com/example/music/pull/12345" if build else None,
                "head_sha": delivery["head_sha"], "base_sha": sha("ai-fixture-base", 40), "reason": None}

    if scenario in titles:
        current = entry("ai-fixture-current", scenario)
        if scenario in ("blocked", "failed", "needs_followup"):
            current["reason"] = ("隔离示例：构建 attempt 已变化，请重新核对失败日志。" if build else
                                 "隔离示例：目标分支已变化，请核对保留的冲突现场与执行记录后再处理。")
    if scenario == "verification_unconfirmed":
        current = entry("ai-fixture-current", "blocked")
        current["reason"] = "正式报告缺少全部 5 条冻结验证命令的检查记录（checks 为空）；请补齐验证及正式报告。"
    if current:
        task["run_id"] = current["execution_run_id"]
        task["session_id"] = current.get("session_id")
        action_state = current["state"]
        if action_state in ("queued", "running", "awaiting_review"):
            status, stage, phase, condition = {
                "queued": ("ready", "dispatch", "dispatch", "confirmed"),
                "running": ("running", "monitor", "observe", "running"),
                "awaiting_review": ("in_review", "review", "review", "reviewing"),
            }[action_state]
            task.update(status=status, stage=stage, phase=phase, condition=condition,
                        display_status=titles[action_state], board_substatus=titles[action_state],
                        board_column="in_review", lifecycle_column="in_review",
                        review_conclusion=None, review_summary=None, exception=None, attention=None,
                        blocked_reason=None, failure_code=None, failure_reason=None, failure_stage=None)
        elif action_state in ("blocked", "failed"):
            task.update(status="needs_user", stage="monitor", phase="needs-you", condition="blocked",
                        display_status="需人工", review_conclusion=None, review_summary=None,
                        failure_stage="running", failure_code="verification_unconfirmed" if scenario == "verification_unconfirmed" else
                        "execution_failed" if action_state == "failed" else "merge_conflict_base_changed",
                        failure_reason=current["reason"], blocked_summary=current["reason"])
            derive_board(task, merged_requests(state))
            task["attention"] = None if state.get("attention_empty") else fake_attention_item(task, merged_requests(state))
    history = [entry("ai-fixture-history", "completed")] if scenario == "history" or fixture.get("with_history") else []
    if current:
        history.append(current)
    if build and current and task.get("release"):
        task["release"] = dict(task["release"], can_reconcile=False, is_historical=True)
    readonly = scenario == "readonly"
    if readonly:
        task["save_state"] = "purged"
    can_prepare = current is None and not readonly
    task["task_ai_action"] = {"kind": kind, "can_prepare": can_prepare,
                              "reason": "只读任务不可启动 AI 处理" if readonly else
                                        "已有处理记录，请先查看当前状态" if current else
                                        "读取失败构建日志并安排一次修复" if build else "使用通用执行配置处理 PR 合并冲突",
                              "current": current, "history": history}


def cmd_task_ai_action_fixture(state, payload, start=False):
    fixture = state.get("task_ai_action_fixture")
    task = next((t for t in merged_tasks(state) if t["task_id"] == payload.get("task_id")), None)
    if not isinstance(fixture, dict) or not task or not task.get("task_ai_action"):
        return fail("未启用隔离 AI 处理场景", "conflict")
    kind = fixture.get("kind", "merge_conflict")
    build = kind == "build_failure"
    if payload.get("kind") != kind:
        return fail("处理类型不支持", "bad_request")
    if fixture.get("start_error" if start else "prepare_error"):
        return fail(str(fixture["start_error" if start else "prepare_error"]), "conflict")
    context = sha("ai-context-%s" % fixture.get("context_revision", 0))
    config = sha("ai-config-%s" % fixture.get("config_revision", 0))
    if start:
        if payload.get("confirm") is not True:
            return fail("需要明确确认模型费用", "bad_request")
        if payload.get("expected_context_sha256") != context or payload.get("expected_config_sha256") != config:
            return fail("PR、任务或通用配置已变化，请重新核对并确认", "conflict")
        if fixture.get("started"):
            return {"ok": True, "request_id": fixture["started"]["request_id"], "already_started": True}
    if not task["task_ai_action"]["can_prepare"]:
        return fail(task["task_ai_action"]["reason"], "conflict")
    if not start:
        time.sleep(min(3, max(0, float(fixture.get("prepare_delay", 0)))))
        return {"candidate": {"task_id": task["task_id"], "kind": kind,
                              "title": "修复构建失败" if build else "解决 PR 合并冲突",
                              "context_sha256": context, "config_sha256": config, "pr_number": None if build else 12345,
                              "branch": "kaneo/auto-long-pr/build-fix-12345-1" if build else task["delivery"]["branch"], "base_branch": "main",
                              "head_sha": task["delivery"]["head_sha"], "base_sha": sha("ai-fixture-base", 40),
                              "source_pr_number": 12345 if build else None,
                              "source_pr_url": "https://github.com/example/music/pull/12345" if build else None,
                              "release_batch_id": "fixture-release-batch" if build else None,
                              "release_tag": (task.get("release") or {}).get("tag") if build else None,
                              "workflow_run_id": 12345 if build else None, "workflow_attempt": 1 if build else None,
                              "workflow_url": "https://github.com/example/music/actions/runs/12345" if build else None,
                              "model_summary": "隔离通用执行配置 · fake-model / high（无模型调用）"}}
    rid = "req-ai-fixture-dispatch"
    fixture["started"] = {"request_id": rid, "entry": {
        "action_id": "ai-fixture-started", "kind": kind, "title": "修复构建失败" if build else "解决 PR 合并冲突",
        "state": "queued", "state_title": "AI 处理待启动", "created_at": iso(utcnow()),
        "execution_run_id": "run-ai-fixture-started", "session_id": None,
        "model_summary": "隔离通用执行配置 · fake-model / high（无模型调用）", "pr_number": 12345,
        "source_pr_number": 12345 if build else None,
        "source_pr_url": "https://github.com/example/music/pull/12345" if build else None,
        "head_sha": task["delivery"]["head_sha"], "base_sha": sha("ai-fixture-base", 40), "reason": None}}
    state.setdefault("extra_requests", []).append({"request_id": rid, "task_id": task["task_id"],
        "kind": "dispatch", "stage": "dispatch", "status": "queued", "priority": 100, "attempt_no": 1,
        "requested_by": "user", "created_at": iso(utcnow()), "updated_at": iso(utcnow())})
    save_state(state)
    return {"ok": True, "request_id": rid, "already_started": False}


# Engine/inbox fixtures never import real core or contact network services.
def fake_attention_item(task, requests):
    if task["status"] != "needs_user" or task.get("save_state", "active") != "active":
        return None
    code = task.get("failure_code")
    reasons = {
        "execution_input_required": "execution_question",
        "verification_unconfirmed": "verify_unconfirmed", "empty_diff": "verify_unconfirmed",
        "send_state_unknown": "plus_send_unknown", "plus_unreachable": "plus_unreachable",
        "fix_rounds_exhausted": "review_changes_exhausted", "review_blocked": "review_changes_exhausted",
        "review_backend_unsupported": "review_changes_exhausted", "review_fix_exhausted": "review_changes_exhausted",
        "planner_failed": "needs_plan_input", "plan_missing": "needs_plan_input", "plan_failed": "needs_plan_input",
        "planner_timeout": "needs_plan_input", "planner_output_invalid": "needs_plan_input", "planner_input_invalid": "needs_plan_input",
        "needs_input": "needs_plan_input", "branch_exists": "needs_plan_input", "delivery_base_unknown": "needs_plan_input",
        "auto_budget_exceeded": "budget_exceeded", "remote_unavailable": "remote_unavailable", "remote_task_missing": "remote_unavailable",
        "remote_conflict": "remote_unavailable", "delivery_commit_failed": "delivery_blocked", "delivery_blocked_files": "delivery_blocked",
        "branch_diverged": "delivery_blocked", "no_changes": "delivery_blocked", "gh_unavailable": "delivery_blocked",
        "push_rejected": "push_rejected", "pr_closed_unmerged": "pr_closed_unmerged", "pr_closed": "pr_closed_unmerged",
        "gh_missing": "gh_environment", "gh_not_logged_in": "gh_environment", "gh_timeout": "gh_environment",
        "lease_expired": "concurrency_stalled", "request_lost": "concurrency_stalled", "plus_timeout": "plus_unreachable",
    }
    stage_reasons = {"plan": "needs_plan_input", "monitor": "verify_unconfirmed", "review": "review_changes_exhausted", "delivery": "delivery_blocked", "dispatch": "delivery_blocked"}
    reason = reasons.get(code, "needs_user_other") if code else stage_reasons.get(task.get("failure_stage"), "needs_user_other")
    actions = allowed_actions(task, requests)
    since = task.get("blocked_at") or task.get("updated_at")
    return {"attention_id": "attn_%s_%s" % (task["task_id"], reason), "task_id": task["task_id"],
            "project_name": task.get("project_name"), "title": task.get("title"), "reason_code": reason,
            "failure_code": code, "failure_stage": task.get("failure_stage"), "failure_reason": task.get("failure_reason"),
            "blocked_summary": task.get("blocked_summary"), "since": since,
            "evidence": [{"kind": "event", "id": "fake-" + task["task_id"], "at": since, "detail": "隔离假数据证据"}],
            "recommended": actions[0] if actions else None, "alternatives": actions[1:], "allowed_actions": actions,
            "question_preview": {"request_id": task["execution_question"]["request_id"], "count": len(task["execution_question"]["questions"]),
                "first_title": task["execution_question"]["questions"][0]["title"] if task["execution_question"]["questions"] else "请在 Plus 回答"}
                if task.get("execution_question") and task.get("failure_code") == "execution_input_required" else None,
            "attention_state": "actionable"}


def cmd_attention_list(state, payload):
    if state.get("attention_error"): return fail("隔离收件箱读取失败", "fixture_failure")
    tasks = merged_tasks(state)
    items = [t["attention"] for t in tasks if t.get("attention")]
    # Frozen ladder precedence; stable input order within equivalent tiers.
    fourth = {"auto_budget_exceeded", "fix_rounds_exhausted", "review_fix_exhausted"}
    second = {"send_state_unknown", "plus_unreachable", "plus_timeout", "gh_missing", "gh_not_logged_in", "gh_timeout"}
    def rank(item):
        return 4 if item["failure_code"] in fourth else 2 if item["failure_code"] in second else 3
    items.sort(key=lambda item: (-rank(item), item.get("since") or ""))
    return {"items": items}


def cmd_engine_status(state, payload):
    try:
        with open(STATE_PATH + ".engine.json", encoding="utf-8") as handle:
            entry = json.load(handle)
        try: os.kill(entry["pid"], 0); alive = True
        except ProcessLookupError: alive = False
        entry["holder_alive"] = alive
        try:
            expires = datetime.fromisoformat(entry.get("lease_expires_at", "").replace("Z", "+00:00"))
            unexpired = expires > utcnow()
        except (ValueError, TypeError):
            unexpired = False
        entry["active"] = alive and unexpired and entry["state"] == "active" and not state.get("engine_heartbeat_lost")
        return {"instances": [entry]}
    except FileNotFoundError:
        return {"instances": []}


def cmd_engine_run(state, payload):
    if os.environ.get("KANPILOT_FAKE_ENGINE_FAIL") == "1":
        return fail("隔离引擎启动失败", "fake_failure")
    stopped = [False]
    def stop(signum, frame): stopped[0] = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    entry = {"instance_id": "fake-engine-%s" % os.getpid(), "pid": os.getpid(), "state": "active", "active": True,
             "holder_alive": True, "stage": None, "started_at": iso(utcnow()), "lease_expires_at": future(minutes=2), "owned_workers": []}
    marker = STATE_PATH + ".engine.json"
    def publish():
        # Readers always observe a complete heartbeat snapshot.
        pending = marker + ".%s.tmp" % os.getpid()
        with open(pending, "w", encoding="utf-8") as handle: json.dump(entry, handle)
        os.replace(pending, marker)
    publish()
    next_heartbeat = time.monotonic() + 1.0
    while not stopped[0]:
        time.sleep(0.1)
        if time.monotonic() >= next_heartbeat:
            if not load_state().get("engine_heartbeat_lost"):
                entry["lease_expires_at"] = future(minutes=2)
                publish()
            next_heartbeat = time.monotonic() + 1.0
    entry["state"] = "stopped"; entry["active"] = False
    publish()
    return {"instance_id": entry["instance_id"], "ticks": 1, "failures": 0}


def cmd_engine_project_setting(state, payload, key):
    mappings = copy.deepcopy(fake_project_mappings(state))
    if payload.get("expected_revision", mappings.get("revision", 0)) != mappings.get("revision", 0):
        return fail("项目映射版本已变化", "conflict")
    pid = payload.get("project_id")
    if pid not in mappings.get("items", {}): return fail("项目不存在", "not_found")
    value = payload.get(key)
    if key == "engine_mode" and value not in ("legacy", "shadow", "live"):
        return fail("引擎模式无效", "bad_request")
    if key == "verify_env":
        forbidden = {"TOKEN", "ACCESS_TOKEN", "ACCESSTOKEN", "REFRESH_TOKEN", "API_KEY", "APIKEY", "SECRET", "PASSWORD", "CREDENTIALS", "AUTHORIZATION", "KANEO_TOKEN", "KANEO_ACCESS_TOKEN", "PI_DESKTOP_TOKEN"}
        if not isinstance(value, dict) or len(value) > 64 or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or k.strip().upper() in forbidden for k, v in value.items()):
            return fail("verify_env 必须为无凭据的字符串字典", "bad_request")
    if key == "fallback_model" and value is not None:
        if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k].strip() for k in ("provider_id", "model_id")):
            return fail("备用模型必须含 provider_id/model_id", "bad_request")
    mappings["items"][pid][key] = value
    mappings["revision"] = mappings.get("revision", 0) + 1
    return mutate(state, lambda s: s.update(project_mappings=mappings) or {"project_id": pid, key: value, "revision": mappings["revision"]})


# Mobile settings fixture: no socket, certificate generation, or network traffic.
def cmd_mobile_status(state, payload):
    config = state.get("mobile_access", {})
    host = config.get("bind_host")
    port = config.get("port", 47831)
    return {"api_version": 1, "server_id": "isolated-mobile-fixture",
            "enabled": config.get("enabled", False), "bind_host": host, "port": port,
            "endpoint": "https://%s:%s" % (host, port) if host else None,
            "certificate_sha256": "0" * 64 if host else None,
            "addresses": [{"address": "192.0.2.10", "name": "en0 · 隔离示例"},
                          {"address": "198.51.100.2", "name": "utun3 · VPN 示例"}],
            "devices": config.get("devices", [])}


def cmd_set_mobile_access(state, payload):
    config = state.setdefault("mobile_access", {})
    if payload.get("enabled"):
        if payload.get("bind_host") not in {"192.0.2.10", "198.51.100.2"}:
            return fail("所选本机地址当前不可用", "bad_request")
        if not isinstance(payload.get("port"), int) or not 1024 <= payload["port"] <= 65535:
            return fail("端口必须为 1024–65535", "bad_request")
        if payload.get("bind_host") != config.get("bind_host"):
            for device in config.get("devices", []):
                device["revoked_at"] = iso(utcnow())
        config.update({"bind_host": payload["bind_host"], "port": payload["port"]})
    config["enabled"] = payload.get("enabled") is True
    save_state(state)
    return cmd_mobile_status(state, {})


def cmd_create_mobile_pairing(state, payload):
    import base64
    status = cmd_mobile_status(state, {})
    if not status["enabled"]:
        return fail("请先启用手机连接", "conflict")
    expires = future(minutes=5)
    body = {"api_version": 1, "server_id": status["server_id"],
            "endpoint": status["endpoint"], "certificate_der": "Zml4dHVyZQ==",
            "certificate_sha256": status["certificate_sha256"],
            "pairing_token": "isolated-fixture-only-" + uuid.uuid4().hex,
            "expires_at": expires}
    encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return {"pairing_uri": "kaneopilot://pair?data=" + encoded, "expires_at": expires}


def cmd_revoke_mobile_device(state, payload):
    for device in state.get("mobile_access", {}).get("devices", []):
        if device["device_id"] == payload.get("device_id"):
            device["revoked_at"] = iso(utcnow())
            save_state(state)
            return {"ok": True}
    return fail("设备不存在", "not_found")


COMMANDS = {
    "mobile-status": cmd_mobile_status,
    "set-mobile-access": cmd_set_mobile_access,
    "create-mobile-pairing": cmd_create_mobile_pairing,
    "revoke-mobile-device": cmd_revoke_mobile_device,
    "mobile-serve": lambda s, p: fail("FakeCore 禁止启动真实手机监听服务", "forbidden"),
    "prepare-task-ai-action": lambda s, p: cmd_task_ai_action_fixture(s, p),
    "start-task-ai-action": lambda s, p: cmd_task_ai_action_fixture(s, p, start=True),
    "engine-status": cmd_engine_status,
    "engine-run": cmd_engine_run,
    "attention-list": cmd_attention_list,
    "session-view": lambda s, p: {"session": {"task_id": p.get("task_id"), "run_id": p.get("run_id"), "state": "associated", "backend": "pi_desktop", "run_status": "running", "connection": {"state": "ok", "detail": "隔离假会话"}, "allowed_actions": []}},
    "set-project-engine-mode": lambda s, p: cmd_engine_project_setting(s, p, "engine_mode"),
    "set-project-verify-env": lambda s, p: cmd_engine_project_setting(s, p, "verify_env"),
    "set-project-fallback-model": lambda s, p: cmd_engine_project_setting(s, p, "fallback_model"),
    "desktop-queue-status": lambda s, p: desktop_queue_status(s, p),
    "set-desktop-queue-config": lambda s, p: desktop_queue_status(s, p, True),
    "desktop-automation-status": lambda s, p: desktop_automation_status(s, p),
    "set-desktop-automation": lambda s, p: desktop_automation_status(s, p, "enabled"),
    "set-desktop-automation-schedule": lambda s, p: desktop_automation_status(s, p, "schedule"),
    "session-control-status": cmd_session_control_status,
    "session-request-start": cmd_session_request_start,
    "get-planner-config": lambda s, p: fake_config(s, p, "planner"),
    "set-planner-config": lambda s, p: fake_config(s, p, "planner", True),
    "planner-capabilities": lambda s, p: planner_capabilities(s),
    "get-review-config": lambda s, p: fake_config(s, p, "review"),
    "set-review-config": lambda s, p: fake_config(s, p, "review", True),
    "review-capabilities": lambda s, p: review_capabilities(s),
    "qoder-model-catalog": qoder_model_catalog,
    "set-project-automation": lambda s, p: cmd_set_project_automation(s, p),
    "set-automation-global": lambda s, p: mutate(s, lambda state: state.update({"automation_global": p}) or {"ok": True}),
    "doctor": lambda s, p: cmd_doctor(p),
    "tick": lambda s, p: {"created": [], "skipped": [{"reason": "fake 不建请求"}]},
    "overview": lambda s, p: cmd_overview(s),
    "list-tasks": lambda s, p: cmd_list_tasks(s, p),
    "get-task": lambda s, p: cmd_get_task(s, p),
    "recover-execution-goals": cmd_recover_execution_goals_fixture,
    "save-execution-answers": lambda s, p: cmd_execution_answers(s, p, "save"),
    "submit-execution-answers": lambda s, p: cmd_execution_answers(s, p, "submit"),
    "reconcile-execution-question": lambda s, p: cmd_execution_answers(s, p, "reconcile"),
    "stop-execution": lambda s, p: cmd_execution_answers(s, p, "stop"),
    "round-observe": lambda s, p: cmd_execution_answers(s, p, "observe"),
    "save-plan-answers": lambda s, p: cmd_plan_answers(s, p),
    "submit-plan-answers": lambda s, p: cmd_plan_answers(s, p, submit=True),
    "abandon-unsent-release": cmd_abandon_unsent_release_fixture,
    "prepare-external-build-repair": cmd_prepare_external_build_repair_fixture,
    "accept-external-build-repair": cmd_accept_external_build_repair_fixture,
    "prepare-restart-release-verification": cmd_prepare_restart_release_verification_fixture,
    "restart-release-verification": cmd_restart_release_verification_fixture,
    "prepare-release-verification": cmd_prepare_release_verification_fixture,
    "start-release-verification": cmd_start_release_verification_fixture,
    "sync-release-verification": cmd_sync_release_verification_fixture,
    "prepare-task-release": cmd_prepare_task_release_fixture,
    "start-task-release": cmd_start_task_release_fixture,
    "sync-release-batch": cmd_sync_release_fixture,
    "sync-release-batches": cmd_sync_releases_fixture,
    "closeout-parent-task": cmd_closeout_parent_fixture,
    "start-review-repair": cmd_start_review_repair_fixture,
    "prepare-review-repair": lambda s, p: cmd_review_resolution(s, p),
    "run-verification-checks": lambda s, p: fail("Pilot 不再运行本机验证；请让 Plus 重新执行并验证，或重新核验已有证据", "conflict"),
    "get-verification-check-status": lambda s, p: cmd_verification_checks(s, p, status_only=True),
    "supplement-review": lambda s, p: cmd_review_resolution(s, p, evidence=True),
    "get-plan": lambda s, p: cmd_get_plan(s, p),
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
    "reconnect-task": lambda s, p: cmd_reconnect_task(s, p),
    "list-sessions": lambda s, p: cmd_list_sessions(s, p),
    "get-session": lambda s, p: cmd_get_session(s, p),
    "session-messages": lambda s, p: cmd_session_messages(s, p),
    "get-exec-config": lambda s, p: cmd_get_exec_config(s, p),
    "set-exec-config": lambda s, p: cmd_set_exec_config(s, p),
    "exec-capabilities": lambda s, p: cmd_exec_capabilities(s, p),
    "executor-status": lambda s, p: cmd_executor_status(s, p),
    "executor-instance": lambda s, p: cmd_executor_instance(s, p),
    "purge-task": lambda s, p: cmd_purge_task(s, p),
    "purge-session": lambda s, p: cmd_purge_session(s, p),
    "trash-session": lambda s, p: cmd_trash_session(s, p),
    "restore-session": lambda s, p: cmd_restore_session(s, p),
    "get-request": lambda s, p: cmd_get_request(s, p),
    "fetch-todos": lambda s, p: cmd_fetch_todos(s, p),
    "prepare-plan-input": lambda s, p: cmd_prepare_plan_input(s, p),
    "start-plans": lambda s, p: cmd_start_plans(s, p),
    "get-project-mappings": lambda s, p: cmd_get_project_mappings(s, p),
    "set-project-mapping": lambda s, p: cmd_set_project_mapping(s, p),
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

    command_log = os.environ.get("KANPILOT_FAKE_COMMAND_LOG")
    if command_log:
        with open(command_log, "a", encoding="utf-8") as log:
            log.write(json.dumps({"command": command, "payload": payload}) + "\n")
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
