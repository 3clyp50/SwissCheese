from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from agent import AgentContext
from helpers import message_queue as mq
from helpers.state_monitor_integration import mark_dirty_for_context

from usr.plugins.swiss_cheese.helpers.constants import (
    AUDIT_STATUS_KEY,
    CHAT_STATE_KEY,
    CROSS_CHAT_SCOPE_KEY,
    CTX_CONFIRMATION_KEY,
    HOLES_KEY,
    NEAR_MISSES_KEY,
    NOTIFICATION_HISTORY_LIMIT,
    RECOVERY_BUDGET_KEY,
    SEVERITIES,
    STATE_KEYS,
    TODOS_KEY,
    TRANSIENT_AUTONOMY_ORIGIN_KEY,
)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _limit(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return items[-limit:]


def _sanitize_severity(value: str) -> str:
    candidate = str(value or "medium").lower()
    return candidate if candidate in SEVERITIES else "medium"


def _sanitize_status(value: Any) -> str:
    return "completed" if str(value or "open").strip().lower() == "completed" else "open"


def _todo_title_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _todo_detail(value: Any) -> str:
    return str(value or "").strip()


def _todo_id(title: str, detail: str, hole_id: str) -> str:
    digest = hashlib.sha1(f"{title}|{detail}|{hole_id}".encode("utf-8")).hexdigest()
    return digest[:12]


def normalize_todo_record(todo: dict[str, Any]) -> dict[str, Any]:
    title = " ".join(str(todo.get("title", "")).strip().split())
    detail = _todo_detail(todo.get("detail", ""))
    hole_id = str(todo.get("hole_id", "") or "").strip()
    todo_id = str(todo.get("id", "") or "").strip() or _todo_id(title, detail, hole_id)
    return {
        "id": todo_id,
        "title": title,
        "detail": detail,
        "source": str(todo.get("source", "manual") or "manual"),
        "status": _sanitize_status(todo.get("status", "open")),
        "severity": _sanitize_severity(str(todo.get("severity", "medium"))),
        "hole_id": hole_id,
        "updated_at": str(todo.get("updated_at", iso_now()) or iso_now()),
        "origin_context_id": str(todo.get("origin_context_id", "") or "").strip(),
        "origin_context_name": str(todo.get("origin_context_name", "") or "").strip(),
        "project_name": str(todo.get("project_name", "") or "").strip(),
        "scope": str(todo.get("scope", "chat") or "chat").strip().lower() or "chat",
    }


def todo_records_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_id = str(left.get("id", "") or "").strip()
    right_id = str(right.get("id", "") or "").strip()
    if left_id and right_id and left_id == right_id:
        return True

    left_hole_id = str(left.get("hole_id", "") or "").strip()
    right_hole_id = str(right.get("hole_id", "") or "").strip()
    if left_hole_id and right_hole_id and left_hole_id == right_hole_id:
        return True

    left_title = _todo_title_key(left.get("title", ""))
    right_title = _todo_title_key(right.get("title", ""))
    if not left_title or not right_title or left_title != right_title:
        return False

    left_scope = str(left.get("scope", "chat") or "chat").strip().lower()
    right_scope = str(right.get("scope", "chat") or "chat").strip().lower()
    if left_scope != right_scope:
        return False

    left_project = str(left.get("project_name", "") or "").strip()
    right_project = str(right.get("project_name", "") or "").strip()
    if left_scope == "project" or right_scope == "project":
        return left_project == right_project

    return True


def merge_todo_records(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    current_severity = _sanitize_severity(str(current.get("severity", "medium")))
    incoming_severity = _sanitize_severity(str(incoming.get("severity", "medium")))
    current_rank = SEVERITIES.index(current_severity)
    incoming_rank = SEVERITIES.index(incoming_severity)

    current_detail = _todo_detail(current.get("detail", ""))
    incoming_detail = _todo_detail(incoming.get("detail", ""))

    preferred_detail = current_detail
    if incoming_detail and (
        not current_detail
        or incoming_detail in current_detail
        or len(incoming_detail) > len(current_detail)
    ):
        preferred_detail = incoming_detail

    current_status = _sanitize_status(current.get("status", "open"))
    incoming_status = _sanitize_status(incoming.get("status", "open"))
    merged_status = "open" if "open" in {current_status, incoming_status} else incoming_status

    current_source = str(current.get("source", "manual") or "manual")
    incoming_source = str(incoming.get("source", "manual") or "manual")
    source_priority = {
        "manual": 4,
        "tool": 3,
        "api": 3,
        "audit": 2,
        "heuristic_fallback": 1,
    }
    merged_source = (
        incoming_source
        if source_priority.get(incoming_source, 0) >= source_priority.get(current_source, 0)
        else current_source
    )

    return {
        "id": str(current.get("id", "") or incoming.get("id", "") or "").strip()
        or _todo_id(
            str(current.get("title", "") or incoming.get("title", "")).strip(),
            preferred_detail,
            str(current.get("hole_id", "") or incoming.get("hole_id", "")).strip(),
        ),
        "title": str(current.get("title", "") or incoming.get("title", "")).strip(),
        "detail": preferred_detail,
        "source": merged_source,
        "status": merged_status,
        "severity": incoming_severity if incoming_rank >= current_rank else current_severity,
        "hole_id": str(current.get("hole_id", "") or incoming.get("hole_id", "")).strip(),
        "updated_at": str(incoming.get("updated_at", "") or current.get("updated_at", "") or iso_now()),
        "origin_context_id": str(current.get("origin_context_id", "") or incoming.get("origin_context_id", "") or "").strip(),
        "origin_context_name": str(current.get("origin_context_name", "") or incoming.get("origin_context_name", "") or "").strip(),
        "project_name": str(current.get("project_name", "") or incoming.get("project_name", "") or "").strip(),
        "scope": str(current.get("scope", "") or incoming.get("scope", "") or "chat").strip().lower() or "chat",
    }


def dedupe_todos(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for todo in todos:
        normalized = normalize_todo_record(todo)
        existing_index = next(
            (idx for idx, item in enumerate(deduped) if todo_records_match(item, normalized)),
            None,
        )
        if existing_index is None:
            deduped.append(normalized)
        else:
            deduped[existing_index] = merge_todo_records(deduped[existing_index], normalized)
    return deduped


def _default_state() -> dict[str, Any]:
    return {
        "version": 2,
        "followup_queue": [],
        "followup_history": [],
        "audit_trace": [],
        "notification_history": [],
        "active_user_turn": 0,
        "recovery_cycles_used": 0,
        "last_followup_fingerprint": "",
        "last_audit_at": "",
        "updated_at": iso_now(),
    }


def _default_audit_status() -> dict[str, Any]:
    return {
        "state": "idle",
        "summary": "",
        "used_fallback": False,
        "last_error": "",
        "last_audit_at": "",
    }


def _default_recovery_budget(max_cycles: int = 2, used_cycles: int = 0) -> dict[str, Any]:
    max_cycles = max(int(max_cycles or 0), 0)
    used_cycles = max(int(used_cycles or 0), 0)
    return {
        "max_cycles": max_cycles,
        "used_cycles": used_cycles,
        "remaining_cycles": max(max_cycles - used_cycles, 0),
    }


def _state_bundle(context: AgentContext) -> dict[str, Any]:
    return {key: context.get_data(key) for key in STATE_KEYS}


def ensure_state(context: AgentContext, plugin_config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(context.get_data(CHAT_STATE_KEY), dict):
        context.set_data(CHAT_STATE_KEY, _default_state())
    for key in (HOLES_KEY, TODOS_KEY, NEAR_MISSES_KEY):
        value = context.get_data(key)
        if not isinstance(value, list):
            context.set_data(key, [])
            continue
        if key == TODOS_KEY:
            context.set_data(key, dedupe_todos(value))
    if not isinstance(context.get_data(AUDIT_STATUS_KEY), dict):
        context.set_data(AUDIT_STATUS_KEY, _default_audit_status())
    if not isinstance(context.get_data(CTX_CONFIRMATION_KEY), dict):
        context.set_data(CTX_CONFIRMATION_KEY, {})
    if not isinstance(context.get_data(CROSS_CHAT_SCOPE_KEY), dict):
        context.set_data(CROSS_CHAT_SCOPE_KEY, {})

    state = context.get_data(CHAT_STATE_KEY) or _default_state()
    state.setdefault("followup_queue", [])
    state.setdefault("followup_history", [])
    state.setdefault("audit_trace", [])
    state.setdefault("notification_history", [])
    state.setdefault("active_user_turn", 0)
    state.setdefault("recovery_cycles_used", 0)
    state.setdefault("last_followup_fingerprint", "")
    state.setdefault("last_audit_at", "")
    state.setdefault("updated_at", iso_now())
    context.set_data(CHAT_STATE_KEY, state)

    if plugin_config:
        context.set_data(
            RECOVERY_BUDGET_KEY,
            _default_recovery_budget(
                max_cycles=int(plugin_config.get("max_auto_recovery_cycles", 2) or 2),
                used_cycles=int(state.get("recovery_cycles_used", 0) or 0),
            ),
        )
        context.set_data(CROSS_CHAT_SCOPE_KEY, dict(plugin_config.get("cross_chat_scope", {}) or {}))
    if not isinstance(context.get_data(RECOVERY_BUDGET_KEY), dict):
        context.set_data(RECOVERY_BUDGET_KEY, _default_recovery_budget())

    sync_output_data(context, plugin_config=plugin_config, dirty=False)
    return _state_bundle(context)


def get_state_bundle(context: AgentContext) -> dict[str, Any]:
    if (
        not isinstance(context.get_data(CHAT_STATE_KEY), dict)
        or not isinstance(context.get_data(HOLES_KEY), list)
        or not isinstance(context.get_data(TODOS_KEY), list)
        or not isinstance(context.get_data(NEAR_MISSES_KEY), list)
        or not isinstance(context.get_data(AUDIT_STATUS_KEY), dict)
        or not isinstance(context.get_data(RECOVERY_BUDGET_KEY), dict)
        or not isinstance(context.get_data(CTX_CONFIRMATION_KEY), dict)
        or not isinstance(context.get_data(CROSS_CHAT_SCOPE_KEY), dict)
    ):
        ensure_state(context, plugin_config=None)
    return _state_bundle(context)


def list_todos(
    context: AgentContext,
    *,
    status: str = "all",
    plugin_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ensure_state(context, plugin_config=plugin_config)
    todos = dedupe_todos(list(context.get_data(TODOS_KEY) or []))
    normalized_status = str(status or "all").strip().lower()
    if normalized_status in ("open", "completed"):
        todos = [todo for todo in todos if todo.get("status") == normalized_status]
    return todos


def sync_output_data(
    context: AgentContext,
    plugin_config: dict[str, Any] | None = None,
    *,
    dirty: bool,
) -> None:
    state = context.get_data(CHAT_STATE_KEY) or _default_state()
    holes = context.get_data(HOLES_KEY) or []
    todos = context.get_data(TODOS_KEY) or []
    near_misses = context.get_data(NEAR_MISSES_KEY) or []
    audit_status = context.get_data(AUDIT_STATUS_KEY) or _default_audit_status()
    recovery_budget = context.get_data(RECOVERY_BUDGET_KEY) or _default_recovery_budget()
    ctx_confirmation = context.get_data(CTX_CONFIRMATION_KEY) or {}
    cross_chat_scope = context.get_data(CROSS_CHAT_SCOPE_KEY) or {}

    hole_limit = int((plugin_config or {}).get("max_holes", 12) or 12)
    todo_limit = int((plugin_config or {}).get("max_todos", 20) or 20)
    near_miss_limit = int((plugin_config or {}).get("max_near_misses", 20) or 20)

    output_state = {
        "version": state.get("version", 2),
        "queue_count": len(state.get("followup_queue", [])),
        "queue_preview": _limit(
            [
                {
                    "fingerprint": item.get("fingerprint", ""),
                    "reason": item.get("reason", ""),
                    "target_context_id": item.get("target_context_id", ""),
                    "status": item.get("status", "pending"),
                }
                for item in state.get("followup_queue", [])
            ],
            5,
        ),
        "audit_trace": _limit(state.get("audit_trace", []), 5),
        "notification_history": _limit(state.get("notification_history", []), 5),
        "active_user_turn": int(state.get("active_user_turn", 0) or 0),
        "last_followup_fingerprint": state.get("last_followup_fingerprint", ""),
        "last_audit_at": state.get("last_audit_at", ""),
        "updated_at": state.get("updated_at", ""),
    }

    context.set_output_data(CHAT_STATE_KEY, output_state)
    context.set_output_data(HOLES_KEY, _limit(holes, hole_limit))
    context.set_output_data(TODOS_KEY, _limit(dedupe_todos(todos), todo_limit))
    context.set_output_data(NEAR_MISSES_KEY, _limit(near_misses, near_miss_limit))
    context.set_output_data(AUDIT_STATUS_KEY, audit_status)
    context.set_output_data(RECOVERY_BUDGET_KEY, recovery_budget)
    context.set_output_data(CTX_CONFIRMATION_KEY, ctx_confirmation)
    context.set_output_data(CROSS_CHAT_SCOPE_KEY, cross_chat_scope)

    if dirty:
        mark_dirty_for_context(context.id, reason="swiss_cheese.state_sync")


def bump_user_turn(context: AgentContext, plugin_config: dict[str, Any] | None = None) -> dict[str, Any]:
    state = ensure_state(context, plugin_config=plugin_config)[CHAT_STATE_KEY]
    state["active_user_turn"] = int(state.get("active_user_turn", 0) or 0) + 1
    state["recovery_cycles_used"] = 0
    state["updated_at"] = iso_now()
    context.set_data(CHAT_STATE_KEY, state)
    context.set_data(TRANSIENT_AUTONOMY_ORIGIN_KEY, None)
    if plugin_config:
        context.set_data(
            RECOVERY_BUDGET_KEY,
            _default_recovery_budget(
                max_cycles=int(plugin_config.get("max_auto_recovery_cycles", 2) or 2),
                used_cycles=0,
            ),
        )
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return state


def set_audit_status(
    context: AgentContext,
    status: dict[str, Any],
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = _default_audit_status()
    merged.update(status or {})
    context.set_data(AUDIT_STATUS_KEY, merged)
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return merged


def set_holes(
    context: AgentContext,
    holes: list[dict[str, Any]],
    plugin_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    limit = int((plugin_config or {}).get("max_holes", 12) or 12)
    context.set_data(HOLES_KEY, _limit(holes or [], limit))
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return context.get_data(HOLES_KEY) or []


def add_or_update_todo(
    context: AgentContext,
    todo: dict[str, Any],
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    todos = list(context.get_data(TODOS_KEY) or [])
    record = normalize_todo_record({**todo, "updated_at": iso_now(), "scope": "chat"})
    existing_index = next((idx for idx, item in enumerate(todos) if todo_records_match(item, record)), None)
    if existing_index is None:
        todos.append(record)
    else:
        todos[existing_index] = merge_todo_records(normalize_todo_record(todos[existing_index]), record)
        record = todos[existing_index]
    limit = int((plugin_config or {}).get("max_todos", 20) or 20)
    todos = dedupe_todos(todos)
    context.set_data(TODOS_KEY, _limit(todos, limit))
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    current_todos = list(context.get_data(TODOS_KEY) or [])
    return next((item for item in current_todos if todo_records_match(item, record)), record)


def resolve_todo(
    context: AgentContext,
    todo_id: str,
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    todos = dedupe_todos(list(context.get_data(TODOS_KEY) or []))
    for todo in todos:
        if todo.get("id") == todo_id:
            todo["status"] = "completed"
            todo["updated_at"] = iso_now()
            context.set_data(TODOS_KEY, todos)
            sync_output_data(context, plugin_config=plugin_config, dirty=True)
            return todo
    return None


def clear_completed_todos(
    context: AgentContext,
    plugin_config: dict[str, Any] | None = None,
) -> int:
    todos = dedupe_todos(list(context.get_data(TODOS_KEY) or []))
    remaining = [todo for todo in todos if todo.get("status") != "completed"]
    context.set_data(TODOS_KEY, remaining)
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return len(remaining)


def record_near_miss(
    context: AgentContext,
    near_miss: dict[str, Any],
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    misses = list(context.get_data(NEAR_MISSES_KEY) or [])
    record = {
        "id": str(near_miss.get("id", "") or hashlib.sha1(str(near_miss).encode("utf-8")).hexdigest()[:12]),
        "title": str(near_miss.get("title", "Near miss")).strip(),
        "detail": str(near_miss.get("detail", "")).strip(),
        "barrier": str(near_miss.get("barrier", "Communicate")).strip() or "Communicate",
        "severity": _sanitize_severity(str(near_miss.get("severity", "medium"))),
        "confidence": float(near_miss.get("confidence", 1.0) or 1.0),
        "created_at": str(near_miss.get("created_at", iso_now())),
        "fingerprint": str(near_miss.get("fingerprint", "") or ""),
    }
    misses.append(record)
    limit = int((plugin_config or {}).get("max_near_misses", 20) or 20)
    context.set_data(NEAR_MISSES_KEY, _limit(misses, limit))
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return record


def append_audit_trace(
    context: AgentContext,
    entry: dict[str, Any],
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = ensure_state(context, plugin_config=plugin_config)[CHAT_STATE_KEY]
    traces = list(state.get("audit_trace", []))
    traces.append(entry)
    limit = int((plugin_config or {}).get("max_audit_traces", 20) or 20)
    state["audit_trace"] = _limit(traces, limit)
    state["last_audit_at"] = str(entry.get("created_at", iso_now()))
    state["updated_at"] = iso_now()
    context.set_data(CHAT_STATE_KEY, state)
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return entry


def has_notification_fingerprint(context: AgentContext, fingerprint: str) -> bool:
    state = ensure_state(context, plugin_config=None)[CHAT_STATE_KEY]
    notifications = list(state.get("notification_history", []))
    return any(str(item.get("fingerprint", "")) == str(fingerprint or "") for item in notifications)


def record_notification_fingerprint(
    context: AgentContext,
    fingerprint: str,
    *,
    reason: str,
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = ensure_state(context, plugin_config=plugin_config)[CHAT_STATE_KEY]
    notifications = list(state.get("notification_history", []))
    record = {
        "fingerprint": str(fingerprint or "").strip(),
        "reason": str(reason or "").strip(),
        "created_at": iso_now(),
    }
    notifications = [item for item in notifications if item.get("fingerprint") != record["fingerprint"]]
    notifications.append(record)
    state["notification_history"] = _limit(notifications, NOTIFICATION_HISTORY_LIMIT)
    state["updated_at"] = iso_now()
    context.set_data(CHAT_STATE_KEY, state)
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return record


def make_followup_fingerprint(target_context_id: str, reason: str, message: str) -> str:
    normalized = "|".join(
        [
            str(target_context_id or "").strip(),
            str(reason or "").strip().lower(),
            " ".join(str(message or "").strip().lower().split()),
        ]
    )
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def queue_followup(
    context: AgentContext,
    *,
    target_context_id: str,
    reason: str,
    message: str,
    auto_send: bool,
    source: str,
    plugin_config: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    bundle = ensure_state(context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    queue = list(state.get("followup_queue", []))
    fingerprint = make_followup_fingerprint(target_context_id, reason, message)
    max_queue = int((plugin_config or {}).get("max_followup_queue", 8) or 8)
    max_cycles = int((plugin_config or {}).get("max_auto_recovery_cycles", 2) or 2)
    used_cycles = int(state.get("recovery_cycles_used", 0) or 0)

    if any(item.get("fingerprint") == fingerprint and item.get("status") == "pending" for item in queue):
        return False, {"reason": "duplicate_pending", "fingerprint": fingerprint}
    if fingerprint == state.get("last_followup_fingerprint", ""):
        return False, {"reason": "duplicate_last_autonomous", "fingerprint": fingerprint}
    if auto_send and used_cycles >= max_cycles:
        return False, {"reason": "recovery_budget_exhausted", "fingerprint": fingerprint}
    if len(queue) >= max_queue:
        return False, {"reason": "followup_queue_full", "fingerprint": fingerprint}

    item = {
        "fingerprint": fingerprint,
        "target_context_id": target_context_id,
        "reason": str(reason or "").strip(),
        "text": str(message or "").strip(),
        "auto_send": bool(auto_send),
        "source": str(source or "manual"),
        "status": "pending",
        "created_at": iso_now(),
    }
    queue.append(item)
    state["followup_queue"] = queue
    state["updated_at"] = iso_now()
    context.set_data(CHAT_STATE_KEY, state)
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return True, item


def bridge_next_followup(
    source_context: AgentContext,
    plugin_config: dict[str, Any] | None = None,
    *,
    manual: bool = False,
) -> dict[str, Any] | None:
    bundle = ensure_state(source_context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    queue = list(state.get("followup_queue", []))
    history = list(state.get("followup_history", []))
    ctx_confirmation = source_context.get_data(CTX_CONFIRMATION_KEY) or {}

    if not manual and ctx_confirmation.get("gate_active", False):
        for item in queue:
            if item.get("status") == "pending" and item.get("auto_send"):
                item["blocked_reason"] = "chat_ctx_confirmation_gate"
                item["blocked_at"] = iso_now()
                state["followup_queue"] = queue
                state["updated_at"] = iso_now()
                source_context.set_data(CHAT_STATE_KEY, state)
                record_near_miss(
                    source_context,
                    {
                        "title": "Autonomous followup held at gate",
                        "detail": "SwissCheese kept queued autonomy idle until the active chat model context length is confirmed.",
                        "barrier": "Prepare",
                        "severity": "medium",
                        "confidence": 1.0,
                        "fingerprint": str(item.get("fingerprint", "")),
                    },
                    plugin_config=plugin_config,
                )
                sync_output_data(source_context, plugin_config=plugin_config, dirty=True)
                return {
                    "fingerprint": item.get("fingerprint", ""),
                    "status": "blocked",
                    "reason": "chat_ctx_confirmation_gate",
                }
        return None

    bridged_item: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for item in queue:
        if bridged_item is not None:
            remaining.append(item)
            continue
        if item.get("status") != "pending" or not item.get("auto_send"):
            remaining.append(item)
            continue
        target_context = AgentContext.get(str(item.get("target_context_id", "")))
        if target_context is None:
            item["status"] = "skipped"
            item["note"] = "target_not_live"
            history.append(item)
            continue
        if target_context.is_running():
            remaining.append(item)
            continue

        mq.add(target_context, str(item.get("text", "")), item_id=str(item.get("fingerprint", "")))
        target_context.set_data(
            TRANSIENT_AUTONOMY_ORIGIN_KEY,
            {
                "source_context_id": source_context.id,
                "fingerprint": item.get("fingerprint", ""),
                "reason": item.get("reason", ""),
                "queued_at": iso_now(),
            },
        )
        mq.send_next(target_context)
        bridged_item = item
        item["status"] = "sent"
        item["sent_at"] = iso_now()
        history.append(item)

    if bridged_item is None:
        return None

    state["followup_queue"] = remaining
    state["followup_history"] = _limit(history, 20)
    state["last_followup_fingerprint"] = bridged_item.get("fingerprint", "")
    state["recovery_cycles_used"] = int(state.get("recovery_cycles_used", 0) or 0) + 1
    state["updated_at"] = iso_now()
    context_recovery_budget = _default_recovery_budget(
        max_cycles=int((plugin_config or {}).get("max_auto_recovery_cycles", 2) or 2),
        used_cycles=int(state.get("recovery_cycles_used", 0) or 0),
    )
    source_context.set_data(CHAT_STATE_KEY, state)
    source_context.set_data(RECOVERY_BUDGET_KEY, context_recovery_budget)
    sync_output_data(source_context, plugin_config=plugin_config, dirty=True)
    return bridged_item


def remove_followup(
    context: AgentContext,
    fingerprint: str,
    plugin_config: dict[str, Any] | None = None,
) -> bool:
    bundle = ensure_state(context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    queue = list(state.get("followup_queue", []))
    remaining = [item for item in queue if item.get("fingerprint") != fingerprint]
    changed = len(remaining) != len(queue)
    if not changed:
        return False
    state["followup_queue"] = remaining
    state["updated_at"] = iso_now()
    context.set_data(CHAT_STATE_KEY, state)
    sync_output_data(context, plugin_config=plugin_config, dirty=True)
    return True
