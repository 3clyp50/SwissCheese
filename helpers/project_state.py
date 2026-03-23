from __future__ import annotations

import json
from typing import Any

from agent import AgentContext
from helpers import files, plugins
from helpers import projects as project_helper
from helpers.state_monitor_integration import mark_dirty_for_context

from usr.plugins.swiss_cheese.helpers.constants import (
    NOTIFICATION_HISTORY_LIMIT,
    PLUGIN_NAME,
    PROJECT_STATE_FILENAME,
)
from usr.plugins.swiss_cheese.helpers import state as chat_state


def iso_now() -> str:
    return chat_state.iso_now()


def _project_state_path(project_name: str) -> str:
    return plugins.determine_plugin_asset_path(
        PLUGIN_NAME,
        project_name,
        "",
        PROJECT_STATE_FILENAME,
    )


def _default_project_state(project_name: str) -> dict[str, Any]:
    return {
        "version": 1,
        "project_name": str(project_name or "").strip(),
        "todos": [],
        "notification_history": [],
        "updated_at": iso_now(),
    }


def _same_project_live_contexts(project_name: str) -> list[AgentContext]:
    contexts: list[AgentContext] = []
    for context in AgentContext.all():
        if project_helper.get_context_project_name(context) == project_name:
            contexts.append(context)
    return contexts


def _mark_project_dirty(project_name: str, reason: str) -> None:
    for context in _same_project_live_contexts(project_name):
        mark_dirty_for_context(context.id, reason=reason)


def load_project_state(project_name: str) -> dict[str, Any]:
    project_name = str(project_name or "").strip()
    if not project_name:
        return _default_project_state("")

    path = _project_state_path(project_name)
    if not files.exists(path):
        return _default_project_state(project_name)

    try:
        payload = json.loads(files.read_file(path))
    except Exception:
        return _default_project_state(project_name)

    if not isinstance(payload, dict):
        return _default_project_state(project_name)

    state = _default_project_state(project_name)
    state.update(payload)
    state["project_name"] = project_name
    state["todos"] = chat_state.dedupe_todos(list(state.get("todos", []) or []))
    history = list(state.get("notification_history", []) or [])
    state["notification_history"] = history[-NOTIFICATION_HISTORY_LIMIT:]
    return state


def save_project_state(project_name: str, state: dict[str, Any]) -> dict[str, Any]:
    project_name = str(project_name or "").strip()
    payload = _default_project_state(project_name)
    payload.update(state or {})
    payload["project_name"] = project_name
    payload["todos"] = chat_state.dedupe_todos(list(payload.get("todos", []) or []))
    payload["notification_history"] = list(payload.get("notification_history", []) or [])[-NOTIFICATION_HISTORY_LIMIT:]
    payload["updated_at"] = iso_now()
    files.write_file(_project_state_path(project_name), json.dumps(payload))
    _mark_project_dirty(project_name, reason="swiss_cheese.project_state")
    return payload


def get_project_name(context: AgentContext) -> str:
    return str(project_helper.get_context_project_name(context) or "").strip()


def get_project_state_for_context(context: AgentContext) -> dict[str, Any] | None:
    project_name = get_project_name(context)
    if not project_name:
        return None
    return load_project_state(project_name)


def list_project_todos(
    context: AgentContext,
    *,
    status: str = "all",
) -> list[dict[str, Any]]:
    state = get_project_state_for_context(context)
    if state is None:
        return []
    todos = chat_state.dedupe_todos(list(state.get("todos", []) or []))
    normalized_status = str(status or "all").strip().lower()
    if normalized_status in ("open", "completed"):
        todos = [todo for todo in todos if todo.get("status") == normalized_status]
    return todos


def add_or_update_project_todo(
    context: AgentContext,
    todo: dict[str, Any],
    *,
    plugin_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    project_name = get_project_name(context)
    if not project_name:
        return None

    state = load_project_state(project_name)
    todos = list(state.get("todos", []) or [])
    record = chat_state.normalize_todo_record(
        {
            **todo,
            "updated_at": iso_now(),
            "scope": "project",
            "project_name": project_name,
            "origin_context_id": str(todo.get("origin_context_id", "") or context.id),
            "origin_context_name": str(todo.get("origin_context_name", "") or context.name or context.id),
        }
    )
    existing_index = next((idx for idx, item in enumerate(todos) if chat_state.todo_records_match(item, record)), None)
    if existing_index is None:
        todos.append(record)
    else:
        todos[existing_index] = chat_state.merge_todo_records(chat_state.normalize_todo_record(todos[existing_index]), record)
        record = todos[existing_index]

    limit = int((plugin_config or {}).get("max_todos", 20) or 20)
    state["todos"] = chat_state.dedupe_todos(todos)[-limit:]
    saved = save_project_state(project_name, state)
    current_todos = list(saved.get("todos", []) or [])
    return next((item for item in current_todos if chat_state.todo_records_match(item, record)), record)


def resolve_project_todo(context: AgentContext, todo_id: str) -> dict[str, Any] | None:
    project_name = get_project_name(context)
    if not project_name:
        return None
    state = load_project_state(project_name)
    todos = chat_state.dedupe_todos(list(state.get("todos", []) or []))
    for todo in todos:
        if todo.get("id") == todo_id:
            todo["status"] = "completed"
            todo["updated_at"] = iso_now()
            state["todos"] = todos
            save_project_state(project_name, state)
            return todo
    return None


def clear_completed_project_todos(context: AgentContext) -> int | None:
    project_name = get_project_name(context)
    if not project_name:
        return None
    state = load_project_state(project_name)
    todos = chat_state.dedupe_todos(list(state.get("todos", []) or []))
    remaining = [todo for todo in todos if todo.get("status") != "completed"]
    state["todos"] = remaining
    save_project_state(project_name, state)
    return len(remaining)


def has_notification_fingerprint(project_name: str, fingerprint: str) -> bool:
    state = load_project_state(project_name)
    notifications = list(state.get("notification_history", []) or [])
    return any(str(item.get("fingerprint", "")) == str(fingerprint or "") for item in notifications)


def record_notification_fingerprint(project_name: str, fingerprint: str, *, reason: str) -> dict[str, Any]:
    state = load_project_state(project_name)
    notifications = list(state.get("notification_history", []) or [])
    record = {
        "fingerprint": str(fingerprint or "").strip(),
        "reason": str(reason or "").strip(),
        "created_at": iso_now(),
    }
    notifications = [item for item in notifications if item.get("fingerprint") != record["fingerprint"]]
    notifications.append(record)
    state["notification_history"] = notifications[-NOTIFICATION_HISTORY_LIMIT:]
    save_project_state(project_name, state)
    return record
