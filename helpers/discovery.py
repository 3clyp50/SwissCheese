from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import json

from agent import AgentContext, AgentContextType
from helpers import files
from helpers import projects as project_helper


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
    return {
        "hole_count": len(holes) if isinstance(holes, list) else 0,
        "todo_count": len(todos) if isinstance(todos, list) else 0,
        "near_miss_count": len(near_misses) if isinstance(near_misses, list) else 0,
        "audit_state": str(audit_status.get("state", "idle")) if isinstance(audit_status, dict) else "idle",
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


def inspect_chat(
    *,
    source_context: AgentContext,
    selector: str,
    scope: dict[str, bool],
) -> dict[str, Any]:
    selector = (selector or "").strip()
    live = list_live_chats()
    persisted = list_persisted_chats()
    all_records = live + persisted

    if not selector:
        return {
            "selector": selector,
            "match_type": "none",
            "target": None,
            "permissions": _permissions(
                source_context=source_context,
                target=ChatRecord(
                    id=source_context.id,
                    name=str(source_context.name or source_context.id),
                    project_name=project_helper.get_context_project_name(source_context) or "",
                    project_title=str((source_context.get_output_data("project") or {}).get("title", "")),
                    running=bool(source_context.is_running()),
                    live=True,
                    persisted_only=False,
                    source="live",
                ),
                scope=scope,
            ),
        }

    exact_id = next((record for record in all_records if record.id == selector), None)
    if exact_id:
        target = exact_id
        match_type = "exact_context_id"
    else:
        exact_name = next((record for record in all_records if record.name == selector), None)
        if exact_name:
            target = exact_name
            match_type = "exact_name"
        else:
            exact_name_ci = next((record for record in all_records if record.name.lower() == selector.lower()), None)
            if exact_name_ci:
                target = exact_name_ci
                match_type = "exact_name_ci"
            else:
                scored = sorted(
                    ((record, _score_name_match(selector, record.name)) for record in all_records),
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
        "match_type": match_type,
        "target": (
            {
                "id": target.id,
                "name": target.name,
                "project_name": target.project_name,
                "project_title": target.project_title,
                "running": target.running,
                "live": target.live,
                "persisted_only": target.persisted_only,
                "source": target.source,
                "path": target.path,
                "state_excerpt": target.state_excerpt or {},
            }
            if target
            else None
        ),
        "permissions": permissions,
    }
