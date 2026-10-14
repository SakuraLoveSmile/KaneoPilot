"""Plus unattended policy and structured asktool interception contract."""
from __future__ import annotations

import hashlib
import json
from . import plan_questions, util


def require_unattended(receipt: dict, session_id: str) -> dict:
    session = receipt.get("session") if isinstance(receipt, dict) else None
    if (not isinstance(session, dict) or session.get("id") != session_id
            or session.get("interactionMode") != "unattended"
            or type(session.get("interactionPolicyVersion")) is not int
            or session["interactionPolicyVersion"] != 1):
        raise util.KanpilotError(
            "interaction_policy_unverified",
            "Plus 未确认 unattended 硬交互策略 v1；禁止发送或结算该轮次")
    return {"interactionMode": "unattended", "interactionPolicyVersion": 1}


def verify_session(client, session_id: str) -> dict:
    return require_unattended(client.session_get(session_id, message_limit=1), session_id)


def required_interaction(record: dict):
    turn = record.get("turn") if isinstance(record.get("turn"), dict) else record
    if (turn.get("errorCode") or turn.get("error_code")) != "INTERACTION_REQUIRED":
        return None
    details = turn.get("errorDetails") or turn.get("error_details")
    if (turn.get("status") != "error" or not isinstance(details, dict)
            or details.get("kind") != "interaction_required"
            or details.get("toolName") != "asktool"
            or not isinstance(details.get("toolCallId"), str)
            or not details["toolCallId"].strip()
            or ("invalidArguments" in details and details["invalidArguments"] is not False)
            or not isinstance(details.get("questions"), list)
            or not 1 <= len(details["questions"]) <= 20):
        raise util.KanpilotError("plan_interaction_invalid", "Plus asktool 拦截问题无效；未生成用户问题")
    for question in details["questions"]:
        if (not isinstance(question, dict)
                or not isinstance(question.get("question"), str)
                or not question["question"].strip()
                or not isinstance(question.get("options"), list)
                or not 1 <= len(question["options"]) <= 100
                or ("multiSelect" in question and type(question["multiSelect"]) is not bool)):
            raise util.KanpilotError("plan_interaction_invalid", "Plus asktool 拦截问题结构无效")
        for option in question["options"]:
            if isinstance(option, str) and option.strip():
                continue
            if (isinstance(option, dict) and isinstance(option.get("label"), str)
                    and option["label"].strip()
                    and isinstance(option.get("description", ""), str)):
                continue
            raise util.KanpilotError("plan_interaction_invalid", "Plus asktool 拦截选项无效")
    try:
        call_id_bytes = len(details["toolCallId"].encode("utf-8"))
        question_bytes = len(json.dumps(details, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8"))
    except (UnicodeError, TypeError, ValueError):
        raise util.KanpilotError("plan_interaction_invalid", "Plus asktool 拦截问题编码无效")
    if call_id_bytes > 512 or question_bytes > 64 * 1024:
        raise util.KanpilotError("plan_interaction_invalid", "Plus asktool 拦截问题或调用 ID 超限")
    return details


def planner_questions(details: dict) -> list:
    result = []
    for index, question in enumerate(details["questions"]):
        digest = hashlib.sha256((details["toolCallId"] + ":" + str(index)).encode()).hexdigest()[:20]
        options = [{"id": "option-" + str(i),
                    "label": opt if isinstance(opt, str) else opt["label"],
                    "description": "" if isinstance(opt, str) else opt.get("description", "")}
                   for i, opt in enumerate(question["options"])]
        description = ""
        if question.get("multiSelect"):
            description = "此问题可多选，请在文本框填写所选选项，可填写多个。\n" + "\n".join(
                opt["label"] + ("：" + opt["description"] if opt["description"] else "") for opt in options)
            options = []
        result.append({"id": "plus-" + digest, "title": question["question"],
                       "description": description, "options": options,
                       "recommended_option_id": None, "allow_custom": True, "required": True})
    return plan_questions.normalize(result)


def blocked_review(details: dict) -> dict:
    lines = ["# 评审受阻：需要用户决定", "", "Plus 已硬拦截 asktool，本轮未继续执行工具或模型请求。", ""]
    for question in details["questions"]:
        lines.append("- " + question["question"])
        for option in question["options"]:
            label = option if isinstance(option, str) else option["label"]
            description = "" if isinstance(option, str) else option.get("description", "")
            lines.append("  - " + label + ("：" + description if description else ""))
        if question.get("multiSelect"):
            lines.append("  - 本问题允许多选。")
    lines.extend(["", "下一步：补充上述决定或材料后重新安排 Review。解除阻塞前不登记通过、不推进交付。"])
    return {"conclusion": "blocked", "report_markdown": "\n".join(lines) + "\n", "fix": None}
