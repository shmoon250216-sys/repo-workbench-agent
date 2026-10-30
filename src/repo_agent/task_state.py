"""Lossless, bounded user requirements; never inferred from model working notes."""

import json

MAX_TASK_STATE_CHARS = 12000


def requirement_list(values):
    if values is None:
        return []
    if (
        not isinstance(values, list)
        or len(values) > 8
        or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values)
    ):
        raise ValueError(
            "Provide at most eight non-empty requirements, each <= 500 characters"
        )
    return list(values)


def task_state(session):
    """Rebuild from authoritative user messages, retaining explicitly supplied fields.

    Old sessions require no migration. Source indices address the stored message list,
    which is never trimmed. A changed source text invalidates its old annotations.
    """
    previous = {
        r["source"]: r for r in session.get("task_state", {}).get("requests", [])
    }
    sources = [("initial", session["task"])] + [
        (f"message:{i}", m["content"])
        for i, m in enumerate(session["messages"])
        if m["role"] == "user" and m.get("origin") != "approval_notice"
    ]
    requests = []
    for source, text in sources:
        old = previous.get(source, {})
        matching = old.get("text") == text
        requests.append(
            {
                "source": source,
                "text": text,
                "constraints": requirement_list(old.get("constraints"))
                if matching
                else [],
                "acceptance_criteria": requirement_list(old.get("acceptance_criteria"))
                if matching
                else [],
            }
        )
    latest = requests[-1]
    return {
        "version": 1,
        "current_goal": {"source": latest["source"], "text": latest["text"]},
        "requests": requests,
    }


def protect_requirements(session, constraints=None, acceptance_criteria=None):
    state = task_state(session)
    latest = state["requests"][-1]
    if constraints is not None:
        latest["constraints"] = requirement_list(constraints)
    if acceptance_criteria is not None:
        latest["acceptance_criteria"] = requirement_list(acceptance_criteria)
    limit = (
        200000 if session.get("semantic_requirements_enabled") else MAX_TASK_STATE_CHARS
    )
    if len(json.dumps(state, ensure_ascii=False)) > limit:
        raise ValueError(
            f"受保护的用户要求超出 {limit} 字符容量，未删除任何旧要求；"
            "请整理完整目标、约束和验收条件后开启新会话。"
        )
    session["task_state"] = state
    return state
