"""Shared planner question contract; legacy prose stays a text question."""
from __future__ import annotations

import hashlib
import json
from . import util

QUESTION_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        {"type": "object", "additionalProperties": False,
         "required": ["id", "title", "description", "options", "recommended_option_id", "allow_custom", "required"],
         "properties": {
             "id": {"type": "string"}, "title": {"type": "string"},
             "description": {"type": "string"},
             "options": {"type": "array", "items": {
                 "type": "object", "additionalProperties": False,
                 "required": ["id", "label", "description"],
                 "properties": {k: {"type": "string"} for k in ("id", "label", "description")}}},
             "recommended_option_id": {"type": ["string", "null"]},
             "allow_custom": {"type": "boolean"}, "required": {"type": "boolean"}}},
    ]
}


def normalize(questions):
    if not isinstance(questions, list):
        raise util.KanpilotError("validation_failed", "questions 必须是列表")
    result, ids = [], set()
    for index, raw in enumerate(questions):
        if isinstance(raw, str):
            if not raw.strip():
                continue
            q = {"id": "legacy-" + hashlib.sha256((str(index) + ":" + raw).encode()).hexdigest()[:20],
                 "title": raw.strip(), "description": "", "options": [],
                 "recommended_option_id": None, "allow_custom": False, "required": True}
        elif isinstance(raw, dict):
            q = dict(raw)
            if set(q) - {"id", "title", "description", "options", "recommended_option_id", "allow_custom", "required"}:
                raise util.KanpilotError("validation_failed", "问题包含不支持的字段")
            for key in ("id", "title"):
                if not isinstance(q.get(key), str) or not q[key].strip():
                    raise util.KanpilotError("validation_failed", "问题缺少有效 " + key)
                q[key] = q[key].strip()
            q.setdefault("description", "")
            q.setdefault("options", [])
            q.setdefault("recommended_option_id", None)
            q.setdefault("allow_custom", False)
            q.setdefault("required", True)
            if not isinstance(q["description"], str) or not isinstance(q["options"], list) or any(type(q[key]) is not bool for key in ("allow_custom", "required")):
                raise util.KanpilotError("validation_failed", "问题字段类型无效")
            options, option_ids = [], set()
            for opt in q["options"]:
                if not isinstance(opt, dict) or set(opt) - {"id", "label", "description"}:
                    raise util.KanpilotError("validation_failed", "问题选项结构无效")
                opt = dict(opt)
                opt.setdefault("description", "")
                if any(not isinstance(opt.get(key), str) for key in ("id", "label", "description")) or not opt["id"].strip() or not opt["label"].strip():
                    raise util.KanpilotError("validation_failed", "问题选项缺少有效 ID 或标签")
                opt["id"], opt["label"] = opt["id"].strip(), opt["label"].strip()
                if opt["id"] == "custom" or opt["id"] in option_ids:
                    raise util.KanpilotError("validation_failed", "选项 ID 重复或使用保留值 custom")
                option_ids.add(opt["id"])
                options.append(opt)
            q["options"] = options
            rec = q["recommended_option_id"]
            if rec is not None and (not isinstance(rec, str) or rec not in option_ids):
                raise util.KanpilotError("validation_failed", "推荐选项不存在")
        else:
            raise util.KanpilotError("validation_failed", "问题必须是文本或结构化对象")
        if q["id"] in ids:
            raise util.KanpilotError("validation_failed", "问题 ID 重复")
        ids.add(q["id"])
        result.append(q)
    return result


def summary(questions):
    return "；".join(q["title"] for q in normalize(questions)) or "见草稿"


def prompt_block(manifest):
    clarification = manifest.get("plan_clarification")
    if not clarification:
        return ""
    return ("\n## 用户已回答的规划问题\n\n以下问题、用户明确选择、补充和上一轮草稿是本轮输入。"
            "遵守用户答案继续完善计划；不要再次询问已经回答的问题。用户作答不授权执行。\n"
            + json.dumps({"previous_decisions": manifest.get("plan_clarification_history", []), "current_decision": clarification}, ensure_ascii=False, indent=2) + "\n")
