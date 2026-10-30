"""Structured AI output contract and validation, without transport concerns."""

import json

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "needs_reply": {"type": "boolean"},
        "draft": {"type": "string"},
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "evidence": {"type": "string"},
                    "message_key": {"type": "string"},
                },
                "required": ["title", "evidence", "message_key"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "needs_reply", "draft", "todos"],
    "additionalProperties": False,
}


def validate_result(text):
    result = json.loads(text)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("summary"), str)
        or type(result.get("needs_reply")) is not bool
        or not isinstance(result.get("draft"), str)
    ):
        raise ValueError("AI 未返回有效的摘要和草稿")
    if len(result["draft"]) > 10000:
        raise ValueError("AI 草稿过长，请手动审查")
    todos = result.setdefault("todos", [])
    if not isinstance(todos, list) or len(todos) > 10:
        raise ValueError("待办候选格式不正确")
    for item in todos:
        if (
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ["title", "evidence", "message_key"]
            )
            or len(item["title"]) > 200
        ):
            raise ValueError("待办候选缺少标题或消息来源")
    return result
