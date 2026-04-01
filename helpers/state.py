from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from agent import AgentContext
from helpers import message_queue as mq
from helpers import persist_chat
from helpers.state_monitor_integration import mark_dirty_for_context

from usr.plugins.swiss_cheese.helpers import discovery
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
    normalize_barrier,
)


FOLLOWUP_HISTORY_LIMIT = 40


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


def _target_key_from_fields(target_kind: str, target_key: str, target_context_id: str, target_task_uuid: str) -> str:
    explicit = str(target_key or "").strip()
    if explicit:
        return explicit
    if str(target_kind or "").strip().lower() == "task" and str(target_task_uuid or "").strip():
        return f"task:{str(target_task_uuid or '').strip()}"
    return f"chat:{str(target_context_id or '').strip()}" if str(target_context_id or "").strip() else ""


def _normalize_followup_status(value: Any) -> str:
    candidate = str(value or "pending").strip().lower()
    return candidate if candidate in {"pending", "bridged", "sent", "blocked", "removed", "skipped"} else "pending"


def _normalize_delivery_state(value: Any) -> str:
    candidate = str(value or "pending").strip().lower()
    return candidate if candidate in {"pending", "queued_in_target_queue", "sent", "blocked", "removed"} else "pending"


def _followup_target_parts(target_key: str, target_context_id: str, target_task_uuid: str) -> tuple[str, str, str]:
    key = str(target_key or "").strip()
    context_id = str(target_context_id or "").strip()
    task_uuid = str(target_task_uuid or "").strip()

    if key.startswith("task:"):
        task_uuid = task_uuid or key.split(":", 1)[1]
        return "task", context_id, task_uuid
    if key.startswith("chat:"):
        context_id = context_id or key.split(":", 1)[1]
        return "chat", context_id, task_uuid
    if task_uuid:
        return "task", context_id, task_uuid
    return "chat", context_id, task_uuid


def normalize_followup_record(item: dict[str, Any]) -> dict[str, Any]:
    raw_target_key = str(item.get("target_key", "") or item.get("id", "") or "").strip()
    raw_context_id = str(item.get("target_context_id", "") or item.get("context_id", "") or "").strip()
    raw_task_uuid = str(item.get("target_task_uuid", "") or "").strip()
    raw_kind = str(item.get("target_kind", "") or item.get("kind", "") or "").strip().lower()
    target_key = _target_key_from_fields(raw_kind, raw_target_key, raw_context_id, raw_task_uuid)
    target_kind, target_context_id, target_task_uuid = _followup_target_parts(
        target_key,
        raw_context_id,
        raw_task_uuid,
    )

    status = _normalize_followup_status(item.get("status", "pending"))
    delivery_state = _normalize_delivery_state(item.get("delivery_state", ""))
    if delivery_state == "pending":
        if status == "bridged":
            delivery_state = "queued_in_target_queue"
        elif status == "sent":
            delivery_state = "sent"
        elif status in {"blocked", "skipped"}:
            delivery_state = "blocked"
        elif status == "removed":
            delivery_state = "removed"

    fingerprint = str(item.get("fingerprint", "") or "").strip()
    bridged_item_id = str(item.get("bridged_item_id", "") or "").strip()
    if not bridged_item_id and delivery_state in {"queued_in_target_queue", "sent"} and fingerprint:
        bridged_item_id = fingerprint

    return {
        "fingerprint": fingerprint,
        "target_key": target_key,
        "target_kind": target_kind,
        "target_context_id": target_context_id,
        "target_task_uuid": target_task_uuid,
        "target_name": str(item.get("target_name", "") or item.get("name", "") or target_context_id or target_key),
        "reason": str(item.get("reason", "") or "").strip(),
        "text": str(item.get("text", item.get("message", "")) or "").strip(),
        "auto_send": bool(item.get("auto_send", False)),
        "source": str(item.get("source", "manual") or "manual"),
        "status": status,
        "delivery_state": delivery_state,
        "bridged_item_id": bridged_item_id,
        "created_at": str(item.get("created_at", "") or iso_now()),
        "bridged_at": str(item.get("bridged_at", "") or ""),
        "sent_at": str(item.get("sent_at", "") or ""),
        "blocked_reason": str(item.get("blocked_reason", item.get("note", "")) or "").strip(),
        "blocked_at": str(item.get("blocked_at", "") or ""),
        "removed_at": str(item.get("removed_at", "") or ""),
    }


def _normalize_followup_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            normalized.append(normalize_followup_record(item))
    return normalized


def _normalize_hole_record(hole: dict[str, Any]) -> dict[str, Any]:
    return {
        **dict(hole or {}),
        "barrier": normalize_barrier((hole or {}).get("barrier")),
        "severity": _sanitize_severity(str((hole or {}).get("severity", "medium"))),
    }


def _normalize_near_miss_record(near_miss: dict[str, Any]) -> dict[str, Any]:
    return {
        **dict(near_miss or {}),
        "barrier": normalize_barrier((near_miss or {}).get("barrier"), default="Coordination"),
        "severity": _sanitize_severity(str((near_miss or {}).get("severity", "medium"))),
        "confidence": float((near_miss or {}).get("confidence", 1.0) or 1.0),
        "created_at": str((near_miss or {}).get("created_at", iso_now()) or iso_now()),
        "fingerprint": str((near_miss or {}).get("fingerprint", "") or ""),
    }


def _upsert_followup_record(records: list[dict[str, Any]], record: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    normalized = normalize_followup_record(record)
    fingerprint = normalized.get("fingerprint", "")
    merged = [
        normalize_followup_record(item)
        for item in records
        if str(item.get("fingerprint", "") or "") != fingerprint
    ]
    merged.append(normalized)
    return _limit(merged, limit)


def _find_followup(records: list[dict[str, Any]], fingerprint: str) -> dict[str, Any] | None:
    needle = str(fingerprint or "").strip()
    if not needle:
        return None
    for item in records:
        if str(item.get("fingerprint", "") or "") == needle:
            return item
    return None


def _persist_context_snapshot(context: AgentContext, reason: str) -> None:
    persist_chat.save_tmp_chat(context)
    mark_dirty_for_context(context.id, reason=reason)


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
        "version": 3,
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


def _commit_state(
    context: AgentContext,
    state: dict[str, Any],
    *,
    plugin_config: dict[str, Any] | None = None,
    dirty: bool,
    persist_reason: str | None = None,
) -> dict[str, Any]:
    state["followup_queue"] = _normalize_followup_items(list(state.get("followup_queue", []) or []))
    state["followup_history"] = _limit(
        _normalize_followup_items(list(state.get("followup_history", []) or [])),
        FOLLOWUP_HISTORY_LIMIT,
    )
    state["updated_at"] = iso_now()
    context.set_data(CHAT_STATE_KEY, state)
    context.set_data(
        RECOVERY_BUDGET_KEY,
        _default_recovery_budget(
            max_cycles=int((plugin_config or {}).get("max_auto_recovery_cycles", 2) or 2),
            used_cycles=int(state.get("recovery_cycles_used", 0) or 0),
        ),
    )
    sync_output_data(context, plugin_config=plugin_config, dirty=dirty)
    if persist_reason:
        _persist_context_snapshot(context, persist_reason)
    return state


def ensure_state(context: AgentContext, plugin_config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(context.get_data(CHAT_STATE_KEY), dict):
        context.set_data(CHAT_STATE_KEY, _default_state())
    for key in (HOLES_KEY, TODOS_KEY, NEAR_MISSES_KEY):
        value = context.get_data(key)
        if not isinstance(value, list):
            context.set_data(key, [])
            continue
        if key == HOLES_KEY:
            context.set_data(key, [_normalize_hole_record(item) for item in value if isinstance(item, dict)])
            continue
        if key == TODOS_KEY:
            context.set_data(key, dedupe_todos(value))
            continue
        if key == NEAR_MISSES_KEY:
            context.set_data(key, [_normalize_near_miss_record(item) for item in value if isinstance(item, dict)])
    if not isinstance(context.get_data(AUDIT_STATUS_KEY), dict):
        context.set_data(AUDIT_STATUS_KEY, _default_audit_status())
    if not isinstance(context.get_data(CTX_CONFIRMATION_KEY), dict):
        context.set_data(CTX_CONFIRMATION_KEY, {})
    if not isinstance(context.get_data(CROSS_CHAT_SCOPE_KEY), dict):
        context.set_data(CROSS_CHAT_SCOPE_KEY, {})

    raw_state = context.get_data(CHAT_STATE_KEY) or _default_state()
    state = _default_state()
    state.update(raw_state)
    state["holes"] = [_normalize_hole_record(item) for item in list(raw_state.get("holes", []) or []) if isinstance(item, dict)]
    state["todos"] = dedupe_todos(list(raw_state.get("todos", []) or []))
    state["near_misses"] = [
        _normalize_near_miss_record(item)
        for item in list(raw_state.get("near_misses", []) or [])
        if isinstance(item, dict)
    ]
    state["followup_queue"] = [
        item
        for item in _normalize_followup_items(list(raw_state.get("followup_queue", []) or []))
        if str(item.get("status", "pending")) == "pending"
    ]
    state["followup_history"] = _limit(
        _normalize_followup_items(list(raw_state.get("followup_history", []) or [])),
        FOLLOWUP_HISTORY_LIMIT,
    )
    context.set_data(CHAT_STATE_KEY, state)

    if plugin_config:
        context.set_data(CROSS_CHAT_SCOPE_KEY, dict(plugin_config.get("cross_chat_scope", {}) or {}))
    if not isinstance(context.get_data(RECOVERY_BUDGET_KEY), dict):
        context.set_data(RECOVERY_BUDGET_KEY, _default_recovery_budget())

    _commit_state(context, state, plugin_config=plugin_config, dirty=False)
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
        "version": state.get("version", 3),
        "queue_count": len(state.get("followup_queue", [])),
        "history_count": len(state.get("followup_history", [])),
        "queue_preview": _limit(
            [
                {
                    "fingerprint": item.get("fingerprint", ""),
                    "reason": item.get("reason", ""),
                    "target_key": item.get("target_key", ""),
                    "target_name": item.get("target_name", ""),
                    "delivery_state": item.get("delivery_state", "pending"),
                    "status": item.get("status", "pending"),
                }
                for item in state.get("followup_queue", [])
            ],
            5,
        ),
        "history_preview": _limit(
            [
                {
                    "fingerprint": item.get("fingerprint", ""),
                    "reason": item.get("reason", ""),
                    "target_key": item.get("target_key", ""),
                    "target_name": item.get("target_name", ""),
                    "delivery_state": item.get("delivery_state", "pending"),
                    "status": item.get("status", "pending"),
                }
                for item in state.get("followup_history", [])
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
    context.set_data(TRANSIENT_AUTONOMY_ORIGIN_KEY, None)
    _commit_state(context, state, plugin_config=plugin_config, dirty=True)
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
    normalized = [_normalize_hole_record(hole) for hole in holes or [] if isinstance(hole, dict)]
    context.set_data(HOLES_KEY, _limit(normalized, limit))
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
    normalized = _normalize_near_miss_record(near_miss)
    record = {
        "id": str(near_miss.get("id", "") or hashlib.sha1(str(near_miss).encode("utf-8")).hexdigest()[:12]),
        "title": str(near_miss.get("title", "Near miss")).strip(),
        "detail": str(near_miss.get("detail", "")).strip(),
        "barrier": normalized["barrier"],
        "severity": normalized["severity"],
        "confidence": normalized["confidence"],
        "created_at": normalized["created_at"],
        "fingerprint": normalized["fingerprint"],
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
    _commit_state(context, state, plugin_config=plugin_config, dirty=True)
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
    _commit_state(context, state, plugin_config=plugin_config, dirty=True)
    return record


def record_blocked_followup(
    context: AgentContext,
    *,
    target_context_id: str = "",
    target_key: str = "",
    target_kind: str = "chat",
    target_task_uuid: str = "",
    target_name: str = "",
    reason: str,
    message: str,
    blocked_reason: str,
    auto_send: bool,
    source: str,
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = ensure_state(context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    resolved_target_key = _target_key_from_fields(target_kind, target_key, target_context_id, target_task_uuid)
    record = normalize_followup_record(
        {
            "fingerprint": make_followup_fingerprint(resolved_target_key, reason, message),
            "target_key": resolved_target_key,
            "target_kind": target_kind,
            "target_context_id": target_context_id,
            "target_task_uuid": target_task_uuid,
            "target_name": target_name,
            "reason": str(reason or "").strip(),
            "text": str(message or "").strip(),
            "auto_send": bool(auto_send),
            "source": str(source or "manual"),
            "status": "blocked",
            "delivery_state": "blocked",
            "created_at": iso_now(),
            "blocked_reason": str(blocked_reason or "").strip(),
            "blocked_at": iso_now(),
        }
    )
    _record_history(state, record)
    _commit_state(
        context,
        state,
        plugin_config=plugin_config,
        dirty=True,
        persist_reason="swiss_cheese.followup_blocked",
    )
    return record


def make_followup_fingerprint(target_key: str, reason: str, message: str) -> str:
    normalized = "|".join(
        [
            str(target_key or "").strip(),
            str(reason or "").strip().lower(),
            " ".join(str(message or "").strip().lower().split()),
        ]
    )
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def queue_followup(
    context: AgentContext,
    *,
    target_context_id: str = "",
    target_key: str = "",
    target_kind: str = "chat",
    target_task_uuid: str = "",
    target_name: str = "",
    reason: str,
    message: str,
    auto_send: bool,
    source: str,
    plugin_config: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    bundle = ensure_state(context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    queue = list(state.get("followup_queue", []))
    resolved_target_key = _target_key_from_fields(target_kind, target_key, target_context_id, target_task_uuid)
    fingerprint = make_followup_fingerprint(resolved_target_key, reason, message)
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

    item = normalize_followup_record(
        {
            "fingerprint": fingerprint,
            "target_key": resolved_target_key,
            "target_kind": target_kind,
            "target_context_id": target_context_id,
            "target_task_uuid": target_task_uuid,
            "target_name": target_name,
            "reason": str(reason or "").strip(),
            "text": str(message or "").strip(),
            "auto_send": bool(auto_send),
            "source": str(source or "manual"),
            "status": "pending",
            "delivery_state": "pending",
            "created_at": iso_now(),
        }
    )
    queue.append(item)
    state["followup_queue"] = queue
    _commit_state(
        context,
        state,
        plugin_config=plugin_config,
        dirty=True,
        persist_reason="swiss_cheese.queue_followup",
    )
    return True, item


def _update_pending_item(
    state: dict[str, Any],
    fingerprint: str,
    updater,
) -> dict[str, Any] | None:
    queue = list(state.get("followup_queue", []))
    for idx, item in enumerate(queue):
        if str(item.get("fingerprint", "") or "") == str(fingerprint or ""):
            updated = normalize_followup_record(updater(dict(item)))
            queue[idx] = updated
            state["followup_queue"] = queue
            return updated
    return None


def _mark_pending_blocked(
    source_context: AgentContext,
    state: dict[str, Any],
    fingerprint: str,
    *,
    reason: str,
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocked = _update_pending_item(
        state,
        fingerprint,
        lambda item: {
            **item,
            "status": "blocked",
            "delivery_state": "blocked",
            "blocked_reason": reason,
            "blocked_at": iso_now(),
        },
    )
    state["followup_queue"] = [
        item
        for item in list(state.get("followup_queue", []))
        if str(item.get("fingerprint", "") or "") != str(fingerprint or "")
    ]
    if blocked is not None:
        _record_history(state, blocked)
    _commit_state(
        source_context,
        state,
        plugin_config=plugin_config,
        dirty=True,
        persist_reason="swiss_cheese.followup_blocked",
    )
    return {
        "fingerprint": str(fingerprint or ""),
        "status": "blocked",
        "delivery_state": "blocked",
        "reason": reason,
        "item": blocked,
    }


def _select_pending_followup(
    queue: list[dict[str, Any]],
    *,
    fingerprint: str = "",
    auto_send_only: bool = False,
) -> dict[str, Any] | None:
    needle = str(fingerprint or "").strip()
    if needle:
        return next(
            (
                item
                for item in queue
                if str(item.get("fingerprint", "") or "") == needle
                and str(item.get("status", "pending")) == "pending"
            ),
            None,
        )

    for item in queue:
        if str(item.get("status", "pending")) != "pending":
            continue
        if auto_send_only and not bool(item.get("auto_send", False)):
            continue
        return item
    return None


def _bridge_native_queue_item(target_context: AgentContext, item: dict[str, Any]) -> dict[str, Any]:
    item_id = str(item.get("bridged_item_id", "") or item.get("fingerprint", "") or "")
    existing = next(
        (queued for queued in mq.get_queue(target_context) if str(queued.get("id", "") or "") == item_id),
        None,
    )
    if existing is not None:
        return existing
    return mq.add(target_context, str(item.get("text", "")), item_id=item_id)


def _send_native_queue_item(
    target_context: AgentContext,
    item_id: str,
    *,
    source_context: AgentContext,
    followup: dict[str, Any],
    manual: bool,
) -> dict[str, Any] | None:
    queued_item = mq.pop_item(target_context, item_id)
    if queued_item is None:
        return None
    if not manual and bool(followup.get("auto_send", False)):
        target_context.set_data(
            TRANSIENT_AUTONOMY_ORIGIN_KEY,
            {
                "source_context_id": source_context.id,
                "fingerprint": followup.get("fingerprint", ""),
                "reason": followup.get("reason", ""),
                "queued_at": iso_now(),
            },
        )
    mq.send_message(target_context, queued_item)
    return queued_item


def _record_history(state: dict[str, Any], record: dict[str, Any]) -> None:
    state["followup_history"] = _upsert_followup_record(
        list(state.get("followup_history", [])),
        record,
        limit=FOLLOWUP_HISTORY_LIMIT,
    )


def _consume_recovery_cycle(
    source_context: AgentContext,
    state: dict[str, Any],
    item: dict[str, Any],
    *,
    plugin_config: dict[str, Any] | None = None,
) -> None:
    state["last_followup_fingerprint"] = item.get("fingerprint", "")
    state["recovery_cycles_used"] = int(state.get("recovery_cycles_used", 0) or 0) + 1
    _commit_state(
        source_context,
        state,
        plugin_config=plugin_config,
        dirty=True,
        persist_reason="swiss_cheese.followup_sent",
    )


def _bridge_pending_followup(
    source_context: AgentContext,
    state: dict[str, Any],
    pending: dict[str, Any],
    *,
    plugin_config: dict[str, Any] | None = None,
    manual: bool,
    send_now: bool,
) -> dict[str, Any]:
    ctx_confirmation = source_context.get_data(CTX_CONFIRMATION_KEY) or {}
    if not manual and bool(pending.get("auto_send", False)) and ctx_confirmation.get("gate_active", False):
        record_near_miss(
            source_context,
            {
                "title": "Autonomous followup held at gate",
                "detail": "SwissCheese kept queued autonomy idle until the active chat model context length is confirmed.",
                "barrier": "Readiness",
                "severity": "medium",
                "confidence": 1.0,
                "fingerprint": str(pending.get("fingerprint", "")),
            },
            plugin_config=plugin_config,
        )
        return _mark_pending_blocked(
            source_context,
            state,
            str(pending.get("fingerprint", "")),
            reason="chat_ctx_confirmation_gate",
            plugin_config=plugin_config,
        )

    if send_now and not manual and bool(pending.get("auto_send", False)):
        max_cycles = int((plugin_config or {}).get("max_auto_recovery_cycles", 2) or 2)
        used_cycles = int(state.get("recovery_cycles_used", 0) or 0)
        if used_cycles >= max_cycles:
            return _mark_pending_blocked(
                source_context,
                state,
                str(pending.get("fingerprint", "")),
                reason="recovery_budget_exhausted",
                plugin_config=plugin_config,
            )

    target_context = discovery.resolve_target_context(pending)
    if target_context is None:
        return _mark_pending_blocked(
            source_context,
            state,
            str(pending.get("fingerprint", "")),
            reason="target_context_unavailable",
            plugin_config=plugin_config,
        )

    native_item = _bridge_native_queue_item(target_context, pending)
    _persist_context_snapshot(target_context, "swiss_cheese.bridge_target_queue")

    queue = list(state.get("followup_queue", []))
    state["followup_queue"] = [
        item
        for item in queue
        if str(item.get("fingerprint", "") or "") != str(pending.get("fingerprint", "") or "")
    ]

    bridged_record = normalize_followup_record(
        {
            **pending,
            "status": "bridged",
            "delivery_state": "queued_in_target_queue",
            "bridged_item_id": str(native_item.get("id", "") or pending.get("fingerprint", "")),
            "bridged_at": iso_now(),
            "blocked_reason": "",
            "blocked_at": "",
        }
    )

    if not send_now:
        _record_history(state, bridged_record)
        _commit_state(
            source_context,
            state,
            plugin_config=plugin_config,
            dirty=True,
            persist_reason="swiss_cheese.followup_bridged",
        )
        return bridged_record

    sent_item = _send_native_queue_item(
        target_context,
        str(bridged_record.get("bridged_item_id", "") or ""),
        source_context=source_context,
        followup=bridged_record,
        manual=manual,
    )
    if sent_item is None:
        state["followup_queue"].append(normalize_followup_record(pending))
        _commit_state(
            source_context,
            state,
            plugin_config=plugin_config,
            dirty=True,
            persist_reason="swiss_cheese.followup_send_failed",
        )
        return {
            "fingerprint": str(pending.get("fingerprint", "")),
            "status": "blocked",
            "delivery_state": "blocked",
            "reason": "bridged_item_missing",
        }

    _persist_context_snapshot(target_context, "swiss_cheese.send_target_queue")
    sent_record = normalize_followup_record(
        {
            **bridged_record,
            "status": "sent",
            "delivery_state": "sent",
            "sent_at": iso_now(),
        }
    )
    _record_history(state, sent_record)

    if not manual and bool(sent_record.get("auto_send", False)):
        _consume_recovery_cycle(source_context, state, sent_record, plugin_config=plugin_config)
    else:
        _commit_state(
            source_context,
            state,
            plugin_config=plugin_config,
            dirty=True,
            persist_reason="swiss_cheese.followup_sent_manual",
        )
    return sent_record


def _send_bridged_followup(
    source_context: AgentContext,
    state: dict[str, Any],
    history_item: dict[str, Any],
    *,
    plugin_config: dict[str, Any] | None = None,
    manual: bool,
) -> dict[str, Any]:
    target_context = discovery.resolve_target_context(history_item)
    if target_context is None:
        blocked_record = normalize_followup_record(
            {
                **history_item,
                "status": "blocked",
                "delivery_state": "blocked",
                "blocked_reason": "target_context_unavailable",
                "blocked_at": iso_now(),
            }
        )
        _record_history(state, blocked_record)
        _commit_state(
            source_context,
            state,
            plugin_config=plugin_config,
            dirty=True,
            persist_reason="swiss_cheese.followup_send_blocked",
        )
        return blocked_record

    sent_item = _send_native_queue_item(
        target_context,
        str(history_item.get("bridged_item_id", "") or ""),
        source_context=source_context,
        followup=history_item,
        manual=manual,
    )
    if sent_item is None:
        blocked_record = normalize_followup_record(
            {
                **history_item,
                "status": "blocked",
                "delivery_state": "blocked",
                "blocked_reason": "bridged_item_missing",
                "blocked_at": iso_now(),
            }
        )
        _record_history(state, blocked_record)
        _commit_state(
            source_context,
            state,
            plugin_config=plugin_config,
            dirty=True,
            persist_reason="swiss_cheese.followup_send_missing",
        )
        return blocked_record

    _persist_context_snapshot(target_context, "swiss_cheese.send_target_queue")
    sent_record = normalize_followup_record(
        {
            **history_item,
            "status": "sent",
            "delivery_state": "sent",
            "sent_at": iso_now(),
            "blocked_reason": "",
            "blocked_at": "",
        }
    )
    _record_history(state, sent_record)
    if not manual and bool(sent_record.get("auto_send", False)):
        _consume_recovery_cycle(source_context, state, sent_record, plugin_config=plugin_config)
    else:
        _commit_state(
            source_context,
            state,
            plugin_config=plugin_config,
            dirty=True,
            persist_reason="swiss_cheese.followup_sent_manual",
        )
    return sent_record


def bridge_next_followup(
    source_context: AgentContext,
    plugin_config: dict[str, Any] | None = None,
    *,
    manual: bool = False,
    fingerprint: str = "",
    send_now: bool | None = None,
) -> dict[str, Any] | None:
    bundle = ensure_state(source_context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    queue = list(state.get("followup_queue", []))
    history = list(state.get("followup_history", []))

    requested_send_now = bool(send_now) if send_now is not None else False

    pending = _select_pending_followup(
        queue,
        fingerprint=fingerprint,
        auto_send_only=not manual,
    )
    if pending is not None:
        return _bridge_pending_followup(
            source_context,
            state,
            pending,
            plugin_config=plugin_config,
            manual=manual,
            send_now=(requested_send_now if manual else True),
        )

    if manual and requested_send_now:
        history_item = _find_followup(history, fingerprint) if fingerprint else None
        if history_item is None:
            history_item = next(
                (
                    item
                    for item in reversed(history)
                    if str(item.get("delivery_state", "")) == "queued_in_target_queue"
                ),
                None,
            )
        if history_item is not None and str(history_item.get("delivery_state", "")) == "queued_in_target_queue":
            return _send_bridged_followup(
                source_context,
                state,
                history_item,
                plugin_config=plugin_config,
                manual=True,
            )

    return None


def retry_followup(
    context: AgentContext,
    fingerprint: str,
    plugin_config: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    bundle = ensure_state(context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    history_item = _find_followup(list(state.get("followup_history", [])), fingerprint)
    if history_item is None:
        return False, {"reason": "followup_not_found", "fingerprint": str(fingerprint or "")}

    delivery_state = str(history_item.get("delivery_state", "") or "")
    if delivery_state not in {"blocked", "removed"}:
        return False, {"reason": "followup_not_retryable", "fingerprint": str(fingerprint or "")}

    return queue_followup(
        context,
        target_key=str(history_item.get("target_key", "") or ""),
        target_kind=str(history_item.get("target_kind", "chat") or "chat"),
        target_context_id=str(history_item.get("target_context_id", "") or ""),
        target_task_uuid=str(history_item.get("target_task_uuid", "") or ""),
        target_name=str(history_item.get("target_name", "") or ""),
        reason=str(history_item.get("reason", "") or ""),
        message=str(history_item.get("text", "") or ""),
        auto_send=bool(history_item.get("auto_send", False)),
        source="retry",
        plugin_config=plugin_config,
    )


def remove_followup(
    context: AgentContext,
    fingerprint: str,
    plugin_config: dict[str, Any] | None = None,
) -> bool:
    bundle = ensure_state(context, plugin_config=plugin_config)
    state = bundle[CHAT_STATE_KEY]
    queue = list(state.get("followup_queue", []))
    remaining = [item for item in queue if item.get("fingerprint") != fingerprint]
    if len(remaining) != len(queue):
        state["followup_queue"] = remaining
        _commit_state(
            context,
            state,
            plugin_config=plugin_config,
            dirty=True,
            persist_reason="swiss_cheese.remove_followup",
        )
        return True

    history_item = _find_followup(list(state.get("followup_history", [])), fingerprint)
    if history_item is None or str(history_item.get("delivery_state", "")) != "queued_in_target_queue":
        return False

    target_context = discovery.resolve_target_context(history_item)
    if target_context is None:
        return False

    mq.remove(target_context, str(history_item.get("bridged_item_id", "") or ""))
    _persist_context_snapshot(target_context, "swiss_cheese.remove_target_queue_item")
    removed_record = normalize_followup_record(
        {
            **history_item,
            "status": "removed",
            "delivery_state": "removed",
            "removed_at": iso_now(),
        }
    )
    _record_history(state, removed_record)
    _commit_state(
        context,
        state,
        plugin_config=plugin_config,
        dirty=True,
        persist_reason="swiss_cheese.remove_followup",
    )
    return True
