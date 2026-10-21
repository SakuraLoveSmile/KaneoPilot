package com.sakurasep.kaneopilot

/** Shared Chinese wording follows macOS Labels and SessionReaderStore; unknown keys stay visible. */
object Labels {
    fun boardColumn(key: String): String = when (key) {
        "needs_plan" -> "待规划"
        "todo" -> "待办"
        "in_progress" -> "进行中"
        "in_review" -> "待评审"
        "done" -> "已完成"
        "failed" -> "阻塞 / 失败"
        else -> key
    }
    fun taskStatus(key: String): String = when (key) {
        "needs_plan" -> "待规划"
        "planning" -> "规划中"
        "ready" -> "就绪"
        "queued" -> "已排队"
        "dispatching", "prepared", "submitting" -> "派发中"
        "running" -> "执行中"
        "in_review" -> "评审中"
        "needs_user" -> "需人工"
        "blocked" -> "阻塞"
        "failed" -> "失败"
        "lost" -> "执行端失联"
        "done", "completed" -> "已完成"
        else -> key
    }
    fun requestStatus(key: String): String = when (key) {
        "queued" -> "已排队"
        "claimed" -> "进行中"
        "succeeded" -> "成功"
        "failed" -> "失败"
        "cancelled" -> "已取消"
        "lost" -> "失联"
        else -> key
    }
    fun count(kind: String, key: String): String = when (kind) {
        "board" -> boardColumn(key)
        "tasks" -> taskStatus(key)
        "requests" -> requestStatus(key)
        else -> key
    }
    fun sessionStatus(key: String): String = when (key) {
        "pending" -> "等待开始"
        "running" -> "运行中"
        "finished" -> "已结束"
        "failed" -> "失败"
        "aborted" -> "已中止"
        "waiting_permission" -> "等待权限确认"
        "idle" -> "当前空闲"
        "not_started" -> "等待启动"
        "executor_lost" -> "启动中断"
        "disconnected" -> "连接不可用"
        "associated" -> "已关联"
        "unavailable" -> "不可用"
        else -> key
    }
    fun reviewConclusion(key: String): String = when (key) {
        "pass" -> "通过"
        "changes_requested" -> "需修改"
        "blocked" -> "受阻"
        else -> key
    }
    fun outcome(key: String): String = when (key) {
        "claimed" -> "已领取"
        "succeeded" -> "成功"
        "failed" -> "失败"
        "needs_user" -> "需人工"
        "cancelled" -> "已取消"
        else -> key
    }
    fun messageType(key: String): String = when (key) {
        "user" -> "用户"
        "assistant" -> "助手"
        "tool_call" -> "工具调用"
        "tool_result" -> "工具结果"
        "command_output" -> "命令输出"
        "system" -> "系统"
        else -> key
    }
    fun eventKind(key: String): String = when (key) {
        "task_created" -> "任务创建"
        "task_updated" -> "任务信息更新"
        "requirements_changed" -> "需求指纹变化"
        "kaneo_observed" -> "远端状态观测"
        "plan_published" -> "计划发布"
        "enqueued" -> "请求入队"
        "claimed" -> "请求领取"
        "heartbeat" -> "执行心跳"
        "status_change" -> "任务状态变化"
        "dispatch_prepared" -> "派发准备"
        "kaneo_transition" -> "远端状态迁移"
        "dispatch_sent" -> "派发已发送"
        "dispatch_failed" -> "派发失败"
        "reported" -> "执行端汇报"
        "review_registered" -> "评审登记"
        "review_repair_prepared" -> "评审修复材料已准备"
        "review_supplemented" -> "评审证据已补充"
        "verification_checks_started" -> "验证检查已开始"
        "verification_checks_completed" -> "验证检查已完成"
        "fix_task_created" -> "修复任务创建"
        "resolved" -> "处置决议"
        "paused" -> "自动推进暂停"
        "unpaused" -> "自动推进恢复"
        "priority_changed" -> "优先级调整"
        "migrated" -> "数据迁移"
        "rechecked" -> "重新校验"
        "request_cancelled" -> "请求取消"
        "failed_request_dismissed" -> "失败记录已移除"
        "task_archived" -> "任务归档"
        "task_trashed" -> "任务移入回收站"
        "task_restored" -> "任务恢复"
        "review_stage_recovered" -> "评审阶段已恢复"
        "review_result_cleared" -> "旧评审结果已留存"
        "task_reconnected" -> "任务重新接入待规划"
        "session_trashed" -> "会话移入回收站"
        "session_restored" -> "会话恢复"
        "task_purged" -> "任务永久从列表移除"
        "session_purged" -> "会话永久从列表移除"
        "plan_input_prepared" -> "规划材料就绪"
        "execution_question_opened" -> "执行中提问"
        "execution_question_answered" -> "执行问题已回答"
        "execution_question_closed" -> "执行问题已关闭"
        "plan_started" -> "规划已排队"
        "plan_ready" -> "规划产出就绪"
        "plan_needs_input" -> "规划需要补充输入"
        "plan_answers_submitted" -> "规划答案已提交"
        "execution_retry_requested" -> "已确认沿用计划重新执行"
        "engine_fix_started" -> "自动补救轮开始"
        "engine_fix_reverted" -> "自动补救已撤回"
        "branch_worktree_adopted" -> "已收养任务分支工作树"
        "model" -> "执行模型选择"
        "model_hint" -> "执行模型配置建议"
        "round_started" -> "轮次启动"
        "round_observe" -> "观察者续接"
        "round_start_failed" -> "轮次启动失败"
        "exec_config_updated" -> "执行配置更新"
        "review_config_updated" -> "评审配置更新"
        "planner_config_updated" -> "规划配置更新"
        "goal_report_evaluated" -> "Goal 报告完成判定"
        "lost_detected" -> "执行端失联"
        "auto_confirm_skipped" -> "跳过自动确认"
        "automation_adopted" -> "自动接收任务"
        "automation_stopped" -> "自动处理已停止"
        "cost_recorded" -> "运行成本登记"
        "delivery_committed" -> "分支改动已提交"
        "delivery_pushed" -> "交付分支已推送"
        "fix_round_started" -> "同任务修复轮开始"
        "kaneo_comment_posted" -> "PR 链接已回写"
        "lost_request_released" -> "失联观察请求已释放"
        "pr_closed" -> "PR 已关闭"
        "pr_merged" -> "PR 已合并"
        "pr_opened" -> "PR 已创建"
        "requirements_accepted" -> "已接受远端需求"
        else -> key
    }
}
