from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import json

from agent import AgentContext, AgentContextType
from helpers import files
from helpers import persist_chat
from helpers import projects as project_helper
from helpers import task_scheduler
from initialize import initialize_agent

from usr.plugins.swiss_cheese.helpers import project_state


@dataclass
class TargetRecord:
    target_key: str
    kind: str
    context_id: str
    context_type: str
    name: str
    project_name: str
    project_title: str
    running: bool
    live: bool
    persisted_only: bool
    source: str
    path: str = ""
    scheduler: dict[str, Any] | None = None
    state_excerpt: dict[str, Any] | None = None
    context_resolvable: bool = False


def _chat_file_path(context_id: str) -> str:
    return files.get_abs_path("usr/chats", context_id, "chat.json")


def _target_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _parse_target_key(value: str) -> tuple[str, str]:
    candidate = str(value or "").strip()
    if ":" not in candidate:
        return "", candidate
    kind, raw = candidate.split(":", 1)
    return kind.strip().lower(), raw.strip()


def _serialize_datetime(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return task_scheduler.serialize_datetime(value)
    except Exception:
        try:
            return value.isoformat()
        except Exception:
            return None


def _project_info_from_payload(
    raw_data: dict[str, Any] | None,
    raw_output: dict[str, Any] | None,
    *,
    fallback_project_name: str = "",
) -> tuple[str, str]:
    data = raw_data or {}
    output = raw_output or {}
    project_name = str(data.get("project", "") or fallback_project_name or "")
    project_title = ""
    if isinstance(output.get("project"), dict):
        project_title = str(output["project"].get("title", "") or "")
    if project_name and not project_title:
        try:
            project_title = project_helper.load_basic_project_data(project_name).get("title", "") or project_name
        except Exception:
            project_title = project_name
    return project_name, project_title


def _native_queue_count(data: dict[str, Any], output: dict[str, Any]) -> int:
    queue = data.get("message_queue", output.get("message_queue", [])) or []
    return len(queue) if isinstance(queue, list) else 0


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

    followup_queue_count = int(swiss_state.get("queue_count", 0) or 0) if isinstance(swiss_state, dict) else 0

    return {
        "hole_count": len(holes) if isinstance(holes, list) else 0,
        "todo_count": len(todos) if isinstance(todos, list) else 0,
        "open_todo_count": open_todos,
        "completed_todo_count": completed_todos,
        "near_miss_count": len(near_misses) if isinstance(near_misses, list) else 0,
        "audit_state": str(audit_status.get("state", "idle")) if isinstance(audit_status, dict) else "idle",
        "followup_queue_count": followup_queue_count,
        "queue_count": followup_queue_count,
        "native_queue_count": _native_queue_count(data, output),
    }


def _load_persisted_payload(context_id: str) -> dict[str, Any] | None:
    path = _chat_file_path(context_id)
    if not files.exists(path):
        return None
    try:
        payload = json.loads(files.read_file(path))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _materialize_context_from_payload(
    payload: dict[str, Any],
    *,
    fallback_name: str,
    fallback_type: AgentContextType,
) -> AgentContext:
    raw_type = str(payload.get("type", "") or fallback_type.value)
    try:
        context_type = AgentContextType(raw_type)
    except Exception:
        context_type = fallback_type

    return AgentContext(
        config=initialize_agent(),
        id=str(payload.get("id", "") or ""),
        name=str(payload.get("name", "") or fallback_name or payload.get("id", "")),
        type=context_type,
        data=dict(payload.get("data", {}) or {}),
        output_data=dict(payload.get("output_data", {}) or {}),
        set_current=False,
    )


def _live_chat_record(context: AgentContext) -> TargetRecord:
    project_name, project_title = _project_info_from_payload(context.data, context.output_data)
    return TargetRecord(
        target_key=_target_key("chat", context.id),
        kind="chat",
        context_id=context.id,
        context_type=str(getattr(context.type, "value", AgentContextType.USER.value) or AgentContextType.USER.value),
        name=str(context.name or context.id),
        project_name=project_name,
        project_title=project_title,
        running=bool(context.is_running()),
        live=True,
        persisted_only=False,
        source="live",
        state_excerpt=_excerpt_from_payload(context.data, context.output_data),
        context_resolvable=True,
    )


def list_live_chats() -> list[TargetRecord]:
    results: list[TargetRecord] = []
    for context in AgentContext.all():
        if context.type == AgentContextType.BACKGROUND:
            continue
        results.append(_live_chat_record(context))
    return results


def list_persisted_chats() -> list[TargetRecord]:
    results: list[TargetRecord] = []
    chats_root = Path(files.get_abs_path("usr/chats"))
    if not chats_root.exists():
        return results

    live_ids = {record.context_id for record in list_live_chats()}
    for chat_file in sorted(chats_root.glob("*/chat.json")):
        payload = _load_persisted_payload(chat_file.parent.name)
        if payload is None:
            continue
        context_id = str(payload.get("id", chat_file.parent.name))
        if context_id in live_ids:
            continue

        context_type = str(payload.get("type", AgentContextType.USER.value) or AgentContextType.USER.value)
        if context_type == AgentContextType.BACKGROUND.value:
            continue

        raw_data = payload.get("data", {}) or {}
        raw_output = payload.get("output_data", {}) or {}
        project_name, project_title = _project_info_from_payload(raw_data, raw_output)
        results.append(
            TargetRecord(
                target_key=_target_key("chat", context_id),
                kind="chat",
                context_id=context_id,
                context_type=context_type if context_type in {AgentContextType.USER.value, AgentContextType.TASK.value} else AgentContextType.USER.value,
                name=str(payload.get("name", "") or context_id),
                project_name=project_name,
                project_title=project_title,
                running=False,
                live=False,
                persisted_only=True,
                source="persisted",
                path=str(chat_file),
                state_excerpt=_excerpt_from_payload(raw_data, raw_output),
                context_resolvable=True,
            )
        )
    return results


def _load_scheduler_tasks() -> list[Any]:
    path = files.get_abs_path(task_scheduler.SCHEDULER_FOLDER, "tasks.json")
    if not files.exists(path):
        return []
    try:
        payload = task_scheduler.SchedulerTaskList.model_validate_json(files.read_file(path))
    except Exception:
        return []
    return list(payload.tasks or [])


def _task_record(task: Any) -> TargetRecord | None:
    task_uuid = str(getattr(task, "uuid", "") or "").strip()
    if not task_uuid:
        return None

    context_id = str(getattr(task, "context_id", "") or "").strip()
    live_context = AgentContext.get(context_id) if context_id else None
    persisted_payload = _load_persisted_payload(context_id) if context_id and not live_context else None

    raw_data = {}
    raw_output = {}
    context_type = AgentContextType.TASK.value
    path = ""
    if live_context:
        raw_data = dict(live_context.data or {})
        raw_output = dict(live_context.output_data or {})
        if getattr(live_context.type, "value", "") in {AgentContextType.USER.value, AgentContextType.TASK.value}:
            context_type = str(live_context.type.value)
    elif persisted_payload:
        raw_data = dict(persisted_payload.get("data", {}) or {})
        raw_output = dict(persisted_payload.get("output_data", {}) or {})
        raw_type = str(persisted_payload.get("type", AgentContextType.TASK.value) or AgentContextType.TASK.value)
        if raw_type in {AgentContextType.USER.value, AgentContextType.TASK.value}:
            context_type = raw_type
        path = _chat_file_path(context_id)

    task_project_name = str(getattr(task, "project_name", "") or "")
    project_name, project_title = _project_info_from_payload(
        raw_data,
        raw_output,
        fallback_project_name=task_project_name,
    )

    task_type = getattr(task, "type", "")
    task_state = getattr(task, "state", "")
    scheduler_meta = {
        "uuid": task_uuid,
        "type": str(getattr(task_type, "value", task_type) or ""),
        "state": str(getattr(task_state, "value", task_state) or ""),
        "next_run": _serialize_datetime(task.get_next_run()),
        "dedicated_context": bool(task.is_dedicated()),
    }

    return TargetRecord(
        target_key=_target_key("task", task_uuid),
        kind="task",
        context_id=context_id,
        context_type=context_type if context_type in {AgentContextType.USER.value, AgentContextType.TASK.value} else AgentContextType.TASK.value,
        name=str(getattr(task, "name", "") or task_uuid),
        project_name=project_name,
        project_title=project_title,
        running=bool(
            str(getattr(task_state, "value", task_state) or "").lower() == "running"
            or (live_context and live_context.is_running())
        ),
        live=live_context is not None,
        persisted_only=bool(persisted_payload and live_context is None),
        source="task_live" if live_context else ("task_persisted" if persisted_payload else "task_scheduler"),
        path=path,
        scheduler=scheduler_meta,
        state_excerpt=_excerpt_from_payload(raw_data, raw_output),
        context_resolvable=bool(context_id),
    )


def list_task_targets() -> list[TargetRecord]:
    results: list[TargetRecord] = []
    for task in _load_scheduler_tasks():
        record = _task_record(task)
        if record is not None:
            results.append(record)
    return results


def _score_name_match(needle: str, candidate: str) -> float:
    if not needle or not candidate:
        return 0.0
    return SequenceMatcher(None, needle.lower(), candidate.lower()).ratio()


def _same_project(source_project: str, target_project: str) -> bool:
    return bool(source_project and target_project and source_project == target_project)


def _current_task_for_context(context_id: str) -> TargetRecord | None:
    candidates = [record for record in list_task_targets() if record.context_id == context_id]
    if not candidates:
        return None

    running = [record for record in candidates if record.running]
    if running:
        return sorted(running, key=lambda item: item.name.lower())[0]

    dedicated = [record for record in candidates if bool((record.scheduler or {}).get("dedicated_context", False))]
    if len(dedicated) == 1:
        return dedicated[0]

    if len(candidates) == 1:
        return candidates[0]

    return None


def current_target_record(source_context: AgentContext) -> TargetRecord:
    current_task = _current_task_for_context(source_context.id)
    if current_task is not None:
        return current_task
    return _live_chat_record(source_context)


def _permissions(
    *,
    source_context: AgentContext,
    target: TargetRecord,
    scope: dict[str, bool],
) -> dict[str, bool]:
    source_project = project_helper.get_context_project_name(source_context) or ""
    same_project = _same_project(source_project, target.project_name)
    cross_project = bool(scope.get("cross_project", False))
    same_context = bool(source_context.id and target.context_id and source_context.id == target.context_id)
    same_project_live_write = bool(scope.get("same_project_live_write", False))
    same_project_persisted_readonly = bool(scope.get("same_project_persisted_readonly", False))

    can_read = same_context or cross_project
    can_queue = same_context or cross_project

    if target.live and same_project:
        can_read = True
    if target.kind == "task" and same_project:
        can_read = True

    if target.live and same_project and same_project_live_write:
        can_queue = True
    if target.kind == "task" and target.context_resolvable and same_project and same_project_live_write:
        can_queue = True

    if target.persisted_only and same_project and same_project_persisted_readonly:
        can_read = True
    if target.kind == "task" and target.context_resolvable and same_project and same_project_persisted_readonly:
        can_read = True

    if target.persisted_only and target.kind != "task":
        can_queue = False
    if target.kind == "task" and not target.context_resolvable:
        can_queue = False

    return {
        "same_project": same_project,
        "can_read": can_read,
        "can_queue": can_queue,
    }


def _serialize_target(
    source_context: AgentContext,
    record: TargetRecord,
    scope: dict[str, bool],
) -> dict[str, Any]:
    permissions = _permissions(source_context=source_context, target=record, scope=scope)
    return {
        "id": record.target_key,
        "target_key": record.target_key,
        "kind": record.kind,
        "context_id": record.context_id,
        "context_type": record.context_type,
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
        "scheduler": record.scheduler if record.kind == "task" else None,
    }


def _all_records(include_persisted: bool = True) -> list[TargetRecord]:
    records = list_live_chats()
    if include_persisted:
        records.extend(list_persisted_chats())
    records.extend(list_task_targets())
    return records


def _filter_records(
    records: list[TargetRecord],
    *,
    source_context: AgentContext,
    project_only: bool,
    kind: str,
) -> list[TargetRecord]:
    source_project = project_helper.get_context_project_name(source_context) or ""
    filtered = list(records)
    if project_only and source_project:
        filtered = [
            record
            for record in filtered
            if record.context_id == source_context.id or record.project_name == source_project
        ]
    normalized_kind = str(kind or "all").strip().lower()
    if normalized_kind in {"chat", "task"}:
        filtered = [record for record in filtered if record.kind == normalized_kind]
    return filtered


def _sort_catalog(records: list[TargetRecord], source_context: AgentContext) -> list[TargetRecord]:
    source_project = project_helper.get_context_project_name(source_context) or ""
    current_target_key = current_target_record(source_context).target_key

    def _key(record: TargetRecord) -> tuple[int, int, int, int, str]:
        is_current = 0 if record.target_key == current_target_key else 1
        same_project = 0 if _same_project(source_project, record.project_name) else 1
        kind_rank = 0 if record.kind == "chat" else 1
        persisted_rank = 1 if record.persisted_only else 0
        return (is_current, same_project, kind_rank, persisted_rank, record.name.lower())

    return sorted(records, key=_key)


def list_targets(
    *,
    source_context: AgentContext,
    scope: dict[str, bool],
    project_only: bool = False,
    include_persisted: bool = True,
    kind: str = "all",
) -> list[dict[str, Any]]:
    records = _filter_records(
        _all_records(include_persisted=include_persisted),
        source_context=source_context,
        project_only=project_only,
        kind=kind,
    )
    records = _sort_catalog(records, source_context)
    return [_serialize_target(source_context, record, scope) for record in records]


def list_chat_catalog(
    *,
    source_context: AgentContext,
    scope: dict[str, bool],
    project_only: bool = False,
    include_persisted: bool = True,
) -> list[dict[str, Any]]:
    return list_targets(
        source_context=source_context,
        scope=scope,
        project_only=project_only,
        include_persisted=include_persisted,
        kind="all",
    )


def _resolve_exact_context_match(records: list[TargetRecord], context_id: str) -> TargetRecord | None:
    candidates = [record for record in records if record.context_id == context_id]
    if not candidates:
        return None
    exact_chat = next((record for record in candidates if record.kind == "chat"), None)
    return exact_chat or candidates[0]


def inspect_target(
    *,
    source_context: AgentContext,
    selector: str = "",
    target_key: str = "",
    target_context_id: str = "",
    scope: dict[str, bool],
    project_only: bool = False,
    include_persisted: bool = True,
    kind: str = "all",
) -> dict[str, Any]:
    selector = (selector or "").strip()
    target_key = (target_key or "").strip()
    target_context_id = (target_context_id or "").strip()

    records = _filter_records(
        _all_records(include_persisted=include_persisted),
        source_context=source_context,
        project_only=project_only,
        kind=kind,
    )
    records = _sort_catalog(records, source_context)

    if not selector and not target_key and not target_context_id:
        current = current_target_record(source_context)
        return {
            "selector": selector,
            "target_key": current.target_key,
            "target_context_id": current.context_id,
            "match_type": "current_target",
            "target": _serialize_target(source_context, current, scope),
            "permissions": _permissions(source_context=source_context, target=current, scope=scope),
        }

    target: TargetRecord | None = None
    match_type = "none"

    if target_key:
        target = next((record for record in records if record.target_key == target_key), None)
        if target:
            match_type = "exact_target_key"
    elif target_context_id:
        target = _resolve_exact_context_match(records, target_context_id)
        if target:
            match_type = "exact_context_id"
    elif selector:
        target = next((record for record in records if record.target_key == selector), None)
        if target:
            match_type = "exact_target_key"
        else:
            for alias_key in (_target_key("chat", selector), _target_key("task", selector)):
                target = next((record for record in records if record.target_key == alias_key), None)
                if target:
                    match_type = "exact_target_key_alias"
                    break
        if not target:
            target = _resolve_exact_context_match(records, selector)
            if target:
                match_type = "exact_context_id"
        if not target:
            exact_name = next((record for record in records if record.name == selector), None)
            if exact_name:
                target = exact_name
                match_type = "exact_name"
        if not target:
            exact_name_ci = next((record for record in records if record.name.lower() == selector.lower()), None)
            if exact_name_ci:
                target = exact_name_ci
                match_type = "exact_name_ci"
        if not target:
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
        "target_key": target_key or (target.target_key if target else ""),
        "target_context_id": target_context_id or (target.context_id if target else ""),
        "match_type": match_type,
        "target": _serialize_target(source_context, target, scope) if target else None,
        "permissions": permissions,
    }


def inspect_chat(
    *,
    source_context: AgentContext,
    selector: str = "",
    target_context_id: str = "",
    scope: dict[str, bool],
    project_only: bool = False,
    include_persisted: bool = True,
) -> dict[str, Any]:
    return inspect_target(
        source_context=source_context,
        selector=selector,
        target_context_id=target_context_id,
        scope=scope,
        project_only=project_only,
        include_persisted=include_persisted,
    )


def resolve_target_context(target: dict[str, Any] | TargetRecord | None) -> AgentContext | None:
    if target is None:
        return None

    if isinstance(target, TargetRecord):
        record = target
    else:
        task_uuid = str(target.get("target_task_uuid", "") or "")
        inferred_kind = str(target.get("target_kind", "") or target.get("kind", "") or "").strip().lower()
        inferred_key = str(target.get("target_key", "") or target.get("id", "") or "").strip()
        key_kind, key_value = _parse_target_key(inferred_key)
        if not inferred_kind:
            inferred_kind = key_kind
        if inferred_kind == "task" and not task_uuid:
            task_uuid = key_value

        context_id = str(target.get("target_context_id", "") or target.get("context_id", "") or "").strip()
        if not context_id and inferred_kind == "task" and task_uuid:
            task_record = next((record for record in list_task_targets() if (record.scheduler or {}).get("uuid") == task_uuid), None)
            context_id = task_record.context_id if task_record else ""
            if task_record:
                record = task_record
            else:
                record = TargetRecord(
                    target_key=_target_key("task", task_uuid),
                    kind="task",
                    context_id=context_id,
                    context_type=AgentContextType.TASK.value,
                    name=str(target.get("target_name", "") or task_uuid),
                    project_name="",
                    project_title="",
                    running=False,
                    live=False,
                    persisted_only=False,
                    source="queue",
                    scheduler={
                        "uuid": task_uuid,
                        "type": "",
                        "state": "",
                        "next_run": None,
                        "dedicated_context": False,
                    },
                    state_excerpt={},
                    context_resolvable=bool(context_id),
                )
        else:
            record = TargetRecord(
                target_key=inferred_key or _target_key(inferred_kind or "chat", context_id),
                kind=inferred_kind or "chat",
                context_id=context_id,
                context_type=str(target.get("context_type", "") or AgentContextType.USER.value),
                name=str(target.get("target_name", "") or target.get("name", "") or context_id or inferred_key),
                project_name=str(target.get("project_name", "") or ""),
                project_title=str(target.get("project_title", "") or ""),
                running=bool(target.get("running", False)),
                live=False,
                persisted_only=False,
                source="queue",
                scheduler=(target.get("scheduler", None) if isinstance(target.get("scheduler", None), dict) else None),
                state_excerpt=(target.get("state_excerpt", None) if isinstance(target.get("state_excerpt", None), dict) else {}),
                context_resolvable=bool(context_id),
            )

    if record.context_id:
        live_context = AgentContext.get(record.context_id)
        if live_context is not None:
            return live_context

        payload = _load_persisted_payload(record.context_id)
        if payload is not None:
            try:
                return persist_chat._deserialize_context(payload)
            except Exception:
                return _materialize_context_from_payload(
                    payload,
                    fallback_name=record.name,
                    fallback_type=AgentContextType.TASK if record.kind == "task" else AgentContextType.USER,
                )

    if record.kind != "task" or not record.context_id:
        return None

    config = initialize_agent()
    context = AgentContext(
        config,
        id=record.context_id,
        name=record.name,
        type=AgentContextType.TASK,
    )
    if record.project_name:
        try:
            project_helper.activate_project(context.id, record.project_name, mark_dirty=False)
        except Exception:
            context.set_data("project", record.project_name)
            context.set_output_data(
                "project",
                {"name": record.project_name, "title": record.project_title or record.project_name},
            )
    persist_chat.save_tmp_chat(context)
    return context


def build_project_rollup(
    *,
    source_context: AgentContext,
    scope: dict[str, bool],
) -> dict[str, Any] | None:
    source_project = project_helper.get_context_project_name(source_context) or ""
    if not source_project:
        return None

    include_persisted = bool(scope.get("same_project_persisted_readonly", False))
    all_targets = list_targets(
        source_context=source_context,
        scope=scope,
        project_only=True,
        include_persisted=include_persisted,
        kind="all",
    )
    chat_targets = [entry for entry in all_targets if entry.get("kind") == "chat"]

    totals = {
        "holes": sum(int((entry.get("state_excerpt") or {}).get("hole_count", 0) or 0) for entry in chat_targets),
        "chat_todos": sum(int((entry.get("state_excerpt") or {}).get("open_todo_count", 0) or 0) for entry in chat_targets),
        "near_misses": sum(int((entry.get("state_excerpt") or {}).get("near_miss_count", 0) or 0) for entry in chat_targets),
        "queued_followups": sum(int((entry.get("state_excerpt") or {}).get("queue_count", 0) or 0) for entry in chat_targets),
    }
    project_backlog = project_state.load_project_state(source_project)
    open_project_todos = [
        todo for todo in list(project_backlog.get("todos", []) or [])
        if str((todo or {}).get("status", "open")) != "completed"
    ]
    totals["project_todos"] = len(open_project_todos)

    return {
        "project_name": source_project,
        "project_title": (
            next((entry.get("project_title", "") for entry in all_targets if entry.get("project_title")), "")
            or source_project
        ),
        "target_count": len(all_targets),
        "chat_count": len(chat_targets),
        "task_count": sum(1 for entry in all_targets if entry.get("kind") == "task"),
        "live_chat_count": sum(1 for entry in chat_targets if entry.get("live")),
        "persisted_chat_count": sum(1 for entry in chat_targets if entry.get("persisted_only")),
        "totals": totals,
        "chat_summaries": chat_targets,
        "target_summaries": all_targets,
    }
