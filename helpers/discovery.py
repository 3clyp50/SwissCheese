from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import json

from agent import AgentContext, AgentContextType
from helpers import files
from helpers import projects as project_helper

from usr.plugins.swiss_cheese.helpers import project_state


@dataclass
class ChatRecord:
    id: str
    name: str
    project_name: str
    project_title: str
    running: bool
    live: bool
    persisted_only: bool
    source: str
    path: str = ""
    state_excerpt: dict[str, Any] | None = None


def _project_info_from_payload(
    raw_data: dict[str, Any] | None,
    raw_output: dict[str, Any] | None,
) -> tuple[str, str]:
    data = raw_data or {}
    output = raw_output or {}
    project_name = str(data.get("project", "") or "")
    project_title = ""
    if isinstance(output.get("project"), dict):
        project_title = str(output["project"].get("title", "") or "")
    if project_name and not project_title:
        try:
            project_title = project_helper.load_basic_project_data(project_name).get("title", "") or project_name
        except Exception:
            project_title = project_name
    return project_name, project_title


def _excerpt_from_payload(data: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    holes = output.get("holes", data.get("holes", [])) or []
    todos = output.get("todos", data.get("todos", [])) or []
    near_misses = output.get("near_misses", data.get("near_misses", [])) or []
    audit_status = output.get("audit_status", data.get("audit_status", {})) or {}
    swiss_state = output.get("swiss_cheese_state", data.get("swiss_cheese_state", {})) or {}

    if isinstance(todos, list):
        open_todos = sum(1 for todo in todos if str((todo or {}).get("status", "open")) != "completed")
        completed_todos = max(len(todos) - open_todos, 0)
    else:
        open_todos = 0
        completed_todos = 0

    return {
        "hole_count": len(holes) if isinstance(holes, list) else 0,
        "todo_count": len(todos) if isinstance(todos, list) else 0,
        "open_todo_count": open_todos,
        "completed_todo_count": completed_todos,
        "near_miss_count": len(near_misses) if isinstance(near_misses, list) else 0,
        "audit_state": str(audit_status.get("state", "idle")) if isinstance(audit_status, dict) else "idle",
        "queue_count": int(swiss_state.get("queue_count", 0) or 0) if isinstance(swiss_state, dict) else 0,
    }


def list_live_chats() -> list[ChatRecord]:
    results: list[ChatRecord] = []
    for context in AgentContext.all():
        if context.type == AgentContextType.BACKGROUND:
            continue
        project_name, project_title = _project_info_from_payload(context.data, context.output_data)
        results.append(
            ChatRecord(
                id=context.id,
                name=str(context.name or context.id),
                project_name=project_name,
                project_title=project_title,
                running=bool(context.is_running()),
                live=True,
                persisted_only=False,
                source="live",
                state_excerpt=_excerpt_from_payload(context.data, context.output_data),
            )
        )
    return results


def list_persisted_chats() -> list[ChatRecord]:
    results: list[ChatRecord] = []
    chats_root = Path(files.get_abs_path("usr/chats"))
    if not chats_root.exists():
        return results

    live_ids = {record.id for record in list_live_chats()}
    for chat_file in sorted(chats_root.glob("*/chat.json")):
        try:
            payload = json.loads(chat_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        chat_id = str(payload.get("id", chat_file.parent.name))
        if chat_id in live_ids:
            continue
        raw_data = payload.get("data", {}) or {}
        raw_output = payload.get("output_data", {}) or {}
        project_name, project_title = _project_info_from_payload(raw_data, raw_output)
        results.append(
            ChatRecord(
                id=chat_id,
                name=str(payload.get("name", "") or chat_id),
                project_name=project_name,
                project_title=project_title,
                running=False,
                live=False,
                persisted_only=True,
                source="persisted",
                path=str(chat_file),
                state_excerpt=_excerpt_from_payload(raw_data, raw_output),
            )
        )
    return results


def _score_name_match(needle: str, candidate: str) -> float:
    if not needle or not candidate:
        return 0.0
    return SequenceMatcher(None, needle.lower(), candidate.lower()).ratio()


def _same_project(source_project: str, target_project: str) -> bool:
    return bool(source_project and target_project and source_project == target_project)


def _permissions(
    *,
    source_context: AgentContext,
    target: ChatRecord,
    scope: dict[str, bool],
) -> dict[str, bool]:
    source_project = project_helper.get_context_project_name(source_context) or ""
    same_project = _same_project(source_project, target.project_name)
    cross_project = bool(scope.get("cross_project", False))
    is_current = source_context.id == target.id

    can_read = is_current or cross_project
    can_queue = is_current or cross_project

    if target.live and same_project and scope.get("same_project_live_write", False):
        can_read = True
        can_queue = True

    if target.persisted_only and same_project and scope.get("same_project_persisted_readonly", False):
        can_read = True

    if target.live and same_project:
        can_read = True

    if target.persisted_only:
        can_queue = False

    return {
        "same_project": same_project,
        "can_read": can_read,
        "can_queue": can_queue,
    }


def _serialize_target(
    source_context: AgentContext,
    record: ChatRecord,
    scope: dict[str, bool],
) -> dict[str, Any]:
    permissions = _permissions(source_context=source_context, target=record, scope=scope)
    return {
        "id": record.id,
        "name": record.name,
        "project_name": record.project_name,
        "project_title": record.project_title,
        "running": record.running,
        "live": record.live,
        "persisted_only": record.persisted_only,
        "source": record.source,
        "path": record.path,
        "state_excerpt": record.state_excerpt or {},
        "permissions": permissions,
    }


def _all_records(include_persisted: bool = True) -> list[ChatRecord]:
    live = list_live_chats()
    if not include_persisted:
        return live
    return live + list_persisted_chats()


def _sort_catalog(records: list[ChatRecord], source_context: AgentContext) -> list[ChatRecord]:
    source_project = project_helper.get_context_project_name(source_context) or ""

    def _key(record: ChatRecord) -> tuple[int, int, int, str]:
        is_current = 0 if record.id == source_context.id else 1
        same_project = 0 if _same_project(source_project, record.project_name) else 1
        persisted = 1 if record.persisted_only else 0
        return (is_current, same_project, persisted, record.name.lower())

    return sorted(records, key=_key)


def list_chat_catalog(
    *,
    source_context: AgentContext,
    scope: dict[str, bool],
    project_only: bool = False,
    include_persisted: bool = True,
) -> list[dict[str, Any]]:
    records = _all_records(include_persisted=include_persisted)
    source_project = project_helper.get_context_project_name(source_context) or ""
    if project_only and source_project:
        records = [record for record in records if record.id == source_context.id or record.project_name == source_project]
    records = _sort_catalog(records, source_context)
    return [_serialize_target(source_context, record, scope) for record in records]


def inspect_chat(
    *,
    source_context: AgentContext,
    selector: str = "",
    target_context_id: str = "",
    scope: dict[str, bool],
    project_only: bool = False,
    include_persisted: bool = True,
) -> dict[str, Any]:
    selector = (selector or "").strip()
    target_context_id = (target_context_id or "").strip()
    records = _all_records(include_persisted=include_persisted)
    source_project = project_helper.get_context_project_name(source_context) or ""
    if project_only and source_project:
        records = [record for record in records if record.id == source_context.id or record.project_name == source_project]

    if not selector and not target_context_id:
        current_record = ChatRecord(
            id=source_context.id,
            name=str(source_context.name or source_context.id),
            project_name=source_project,
            project_title=str((source_context.get_output_data("project") or {}).get("title", "")),
            running=bool(source_context.is_running()),
            live=True,
            persisted_only=False,
            source="live",
            state_excerpt=_excerpt_from_payload(source_context.data, source_context.output_data),
        )
        return {
            "selector": selector,
            "target_context_id": target_context_id,
            "match_type": "current_chat",
            "target": _serialize_target(source_context, current_record, scope),
            "permissions": _permissions(source_context=source_context, target=current_record, scope=scope),
        }

    target: ChatRecord | None = None
    match_type = "none"

    if target_context_id:
        target = next((record for record in records if record.id == target_context_id), None)
        if target:
            match_type = "exact_context_id"
    elif selector:
        exact_id = next((record for record in records if record.id == selector), None)
        if exact_id:
            target = exact_id
            match_type = "exact_context_id"
        else:
            exact_name = next((record for record in records if record.name == selector), None)
            if exact_name:
                target = exact_name
                match_type = "exact_name"
            else:
                exact_name_ci = next((record for record in records if record.name.lower() == selector.lower()), None)
                if exact_name_ci:
                    target = exact_name_ci
                    match_type = "exact_name_ci"
                else:
                    scored = sorted(
                        ((record, _score_name_match(selector, record.name)) for record in records),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                    target = scored[0][0] if scored and scored[0][1] >= 0.55 else None
                    match_type = "fuzzy_name" if target else "none"

    permissions = _permissions(source_context=source_context, target=target, scope=scope) if target else {
        "same_project": False,
        "can_read": False,
        "can_queue": False,
    }

    return {
        "selector": selector,
        "target_context_id": target_context_id,
        "match_type": match_type,
        "target": _serialize_target(source_context, target, scope) if target else None,
        "permissions": permissions,
    }


def build_project_rollup(
    *,
    source_context: AgentContext,
    scope: dict[str, bool],
) -> dict[str, Any] | None:
    source_project = project_helper.get_context_project_name(source_context) or ""
    if not source_project:
        return None

    include_persisted = bool(scope.get("same_project_persisted_readonly", False))
    records = _all_records(include_persisted=include_persisted)
    project_records = [record for record in records if record.project_name == source_project]
    project_records = _sort_catalog(project_records, source_context)

    serialized = [_serialize_target(source_context, record, scope) for record in project_records]
    totals = {
        "holes": sum(int((entry.get("state_excerpt") or {}).get("hole_count", 0) or 0) for entry in serialized),
        "chat_todos": sum(int((entry.get("state_excerpt") or {}).get("open_todo_count", 0) or 0) for entry in serialized),
        "near_misses": sum(int((entry.get("state_excerpt") or {}).get("near_miss_count", 0) or 0) for entry in serialized),
        "queued_followups": sum(int((entry.get("state_excerpt") or {}).get("queue_count", 0) or 0) for entry in serialized),
    }
    project_backlog = project_state.load_project_state(source_project)
    open_project_todos = [
        todo for todo in list(project_backlog.get("todos", []) or [])
        if str((todo or {}).get("status", "open")) != "completed"
    ]
    totals["project_todos"] = len(open_project_todos)

    return {
        "project_name": source_project,
        "project_title": serialized[0]["project_title"] if serialized else source_project,
        "chat_count": len(serialized),
        "live_chat_count": sum(1 for entry in serialized if entry.get("live")),
        "persisted_chat_count": sum(1 for entry in serialized if entry.get("persisted_only")),
        "totals": totals,
        "chat_summaries": serialized,
    }
