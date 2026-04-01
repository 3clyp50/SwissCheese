from helpers.extension import Extension

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import state as state_helper
from usr.plugins.swiss_cheese.helpers.constants import (
    TRANSIENT_LAST_USER_MESSAGE_KEY,
    TRANSIENT_RESPONSE_KEY,
    TRANSIENT_USER_TURN_SIGNAL_KEY,
)


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _excerpt(value: str, limit: int = 180) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _keywords(value: str) -> set[str]:
    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "into", "your", "about", "please",
        "chat", "project", "active", "same", "only", "need", "want", "have", "will", "would",
    }
    tokens = {
        token
        for token in _normalize_text(value).replace("/", " ").replace("-", " ").split()
        if len(token) >= 4 and token not in stopwords
    }
    return tokens


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _build_turn_signal(agent, message: str) -> dict:
    normalized = _normalize_text(message)
    previous = dict(agent.context.get_data(TRANSIENT_LAST_USER_MESSAGE_KEY) or {})
    previous_text = str(previous.get("text", "") or "")
    previous_normalized = str(previous.get("normalized", "") or "")
    previous_keywords = set(previous.get("keywords", []) or [])
    current_keywords = _keywords(message)

    project_payload = agent.context.get_output_data("project") or {}
    project_title = str(project_payload.get("title", "") or agent.context.get_data("project") or "")
    anchor_keywords = _keywords(" ".join([str(agent.context.name or ""), project_title]))
    previous_overlap = _overlap_score(current_keywords, previous_keywords)
    anchor_overlap = _overlap_score(current_keywords, anchor_keywords)
    exact_repeat = bool(normalized and normalized == previous_normalized)
    drift_suspected = bool(
        current_keywords
        and previous_keywords
        and not exact_repeat
        and previous_overlap < 0.12
        and anchor_overlap < 0.18
    )

    signal = {
        "message_excerpt": _excerpt(message),
        "previous_message_excerpt": _excerpt(previous_text),
        "previous_response_excerpt": _excerpt(str(agent.get_data(TRANSIENT_RESPONSE_KEY) or "")),
        "exact_repeat": exact_repeat,
        "drift_suspected": drift_suspected,
        "previous_overlap": round(previous_overlap, 3),
        "anchor_overlap": round(anchor_overlap, 3),
        "context_name": str(agent.context.name or agent.context.id),
        "project_title": project_title,
        "current_keywords": sorted(current_keywords),
        "anchor_keywords": sorted(anchor_keywords),
    }
    if drift_suspected:
        signal["drift_reason"] = "low_overlap_with_previous_turn_and_chat_anchors"
    return signal


class SwissCheeseUserTurn(Extension):
    async def execute(self, data: dict = {}, **kwargs):
        if not self.agent or self.agent.number != 0:
            return
        plugin_config = swiss_config.get_plugin_config(self.agent)
        message = str(data.get("message", "") or "")
        signal = _build_turn_signal(self.agent, message)
        state_helper.bump_user_turn(self.agent.context, plugin_config=plugin_config)
        self.agent.context.set_data(
            TRANSIENT_LAST_USER_MESSAGE_KEY,
            {
                "text": message,
                "normalized": _normalize_text(message),
                "keywords": signal.get("current_keywords", []),
            },
        )
        self.agent.context.set_data(TRANSIENT_USER_TURN_SIGNAL_KEY, signal)
        self.agent.context.set_output_data(TRANSIENT_USER_TURN_SIGNAL_KEY, signal)
        self.agent.set_data("_swiss_cheese_reasoning", "")
        self.agent.set_data("_swiss_cheese_response", "")
        running_task = self.agent.get_data("_swiss_cheese_audit_task")
        if running_task and not running_task.done():
            running_task.cancel()
