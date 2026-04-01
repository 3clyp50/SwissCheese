from __future__ import annotations

from datetime import datetime, timezone
import asyncio
from difflib import SequenceMatcher
import json
import hashlib
from typing import TYPE_CHECKING, Any

from helpers.dirty_json import DirtyJson
from helpers import history as history_helper
from helpers import tokens

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import context_window, discovery, project_state, state as state_helper
from usr.plugins.swiss_cheese.helpers.constants import (
    ACTIVE_FAILURE_PATTERNS,
    AUDIT_STATUS_KEY,
    DANGEROUS_AUTONOMOUS_PATTERNS,
    KINDS,
    LATENT_CONDITION_PATTERNS,
    PLUGIN_NAME,
    SEVERITIES,
    TRANSIENT_AUDIT_TASK_KEY,
    TRANSIENT_LAST_UTILITY_INPUT_KEY,
    TRANSIENT_LAST_USER_MESSAGE_KEY,
    TRANSIENT_REASONING_KEY,
    TRANSIENT_RESPONSE_KEY,
    TRANSIENT_USER_TURN_SIGNAL_KEY,
    normalize_barrier,
)

if TYPE_CHECKING:
    from agent import Agent


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def collect_reasoning(agent: "Agent", full_text: str) -> None:
    agent.set_data(TRANSIENT_REASONING_KEY, full_text or "")


def collect_response(agent: "Agent", full_text: str) -> None:
    agent.set_data(TRANSIENT_RESPONSE_KEY, full_text or "")


def _user_turn_signal(agent: "Agent") -> dict[str, Any]:
    return dict(agent.context.get_data(TRANSIENT_USER_TURN_SIGNAL_KEY) or {})


def _references_prior_answer(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in ("previous", "earlier", "already", "as noted", "as mentioned", "prior answer", "clarify"))


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.strip().lower(), right.strip().lower()).ratio()


def _recent_history_text(agent: "Agent", limit: int) -> str:
    outputs = agent.history.output()
    if limit > 0:
        outputs = outputs[-limit:]
    return history_helper.output_text(outputs, ai_label="assistant", human_label="user")


def _build_scope_payload(agent: "Agent", plugin_config: dict[str, Any], scope: dict[str, bool]) -> dict[str, Any]:
    project_name, agent_profile = swiss_config.get_scope_from_agent(agent)
    return {
        "context_id": agent.context.id,
        "context_name": str(agent.context.name or agent.context.id),
        "project_name": project_name,
        "agent_profile": agent_profile,
        "scope": scope,
        "plugin": PLUGIN_NAME,
        "orchestration_enabled": any(scope.values()),
        "open_holes": agent.context.get_data("holes") or [],
        "open_todos": agent.context.get_data("todos") or [],
        "project_backlog_count": len(project_state.list_project_todos(agent.context, status="open")) if project_name else 0,
    }


def _build_target_catalog_snapshot(agent: "Agent", plugin_config: dict[str, Any]) -> list[dict[str, Any]]:
    scope = agent.context.get_data("cross_chat_scope") or plugin_config.get("cross_chat_scope", {})
    targets = discovery.list_targets(
        source_context=agent.context,
        scope=scope,
        project_only=False,
        include_persisted=True,
    )
    snapshot: list[dict[str, Any]] = []
    for target in targets[:12]:
        snapshot.append(
            {
                "target_key": target.get("target_key", ""),
                "kind": target.get("kind", ""),
                "context_id": target.get("context_id", ""),
                "name": target.get("name", ""),
                "project_name": target.get("project_name", ""),
                "live": bool(target.get("live", False)),
                "persisted_only": bool(target.get("persisted_only", False)),
                "scheduler": target.get("scheduler", None),
                "permissions": target.get("permissions", {}),
            }
        )
    return snapshot


def _build_audit_message(agent: "Agent", plugin_config: dict[str, Any], ctx_status: dict[str, Any]) -> str:
    recent_history = _recent_history_text(agent, int(plugin_config.get("audit_history_messages", 10) or 10))
    reasoning = str(agent.get_data(TRANSIENT_REASONING_KEY) or "")
    response = str(agent.get_data(TRANSIENT_RESPONSE_KEY) or "")
    scope = ctx_status.get("scope", {})
    project_rollup = discovery.build_project_rollup(
        source_context=agent.context,
        scope=agent.context.get_data("cross_chat_scope") or plugin_config.get("cross_chat_scope", {}),
    )
    payload = {
        "recent_conversation_history": recent_history,
        "current_context_window_snapshot": {
            "chat_model": ctx_status.get("chat_model", {}),
            "utility_model": ctx_status.get("utility_model", {}),
            "gate_active": ctx_status.get("gate_active", False),
            "gate_diagnostics": ctx_status.get("gate_diagnostics", {}),
            "utility_warning_active": ctx_status.get("utility_warning_active", False),
            "utility_warning_diagnostics": ctx_status.get("utility_warning_diagnostics", {}),
        },
        "current_reasoning_response_evidence": {
            "reasoning": reasoning,
            "response": response,
        },
        "current_user_turn_signal": _user_turn_signal(agent),
        "current_user_message_snapshot": agent.context.get_data(TRANSIENT_LAST_USER_MESSAGE_KEY) or {},
        "open_holes": agent.context.get_data("holes") or [],
        "open_todos": agent.context.get_data("todos") or [],
        "shared_project_backlog": project_state.list_project_todos(agent.context, status="all"),
        "project_rollup_summary": project_rollup,
        "target_catalog": _build_target_catalog_snapshot(agent, plugin_config),
        "current_project_target_scope": scope,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _safe_json_load(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = DirtyJson.parse_string(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_barrier(value: Any) -> str:
    return normalize_barrier(value)


def _normalize_kind(value: Any) -> str:
    candidate = str(value or "active_failure").strip().lower()
    return candidate if candidate in KINDS else "active_failure"


def _normalize_pattern(kind: str, pattern: Any) -> str:
    candidate = str(pattern or "").strip().lower().replace(" ", "_")
    allowed = ACTIVE_FAILURE_PATTERNS if kind == "active_failure" else LATENT_CONDITION_PATTERNS
    if candidate in allowed:
        return candidate
    return allowed[0]


def _normalize_severity(value: Any) -> str:
    candidate = str(value or "medium").strip().lower()
    return candidate if candidate in SEVERITIES else "medium"


def _hole_id(kind: str, pattern: str, barrier: str, title: str) -> str:
    digest = hashlib.sha1(f"{kind}|{pattern}|{barrier}|{title}".encode("utf-8")).hexdigest()
    return digest[:12]


def _normalize_hole(raw: dict[str, Any]) -> dict[str, Any]:
    kind = _normalize_kind(raw.get("kind"))
    pattern = _normalize_pattern(kind, raw.get("pattern"))
    barrier = _normalize_barrier(raw.get("barrier"))
    title = str(raw.get("title", pattern.replace("_", " ").title())).strip()
    return {
        "id": _hole_id(kind, pattern, barrier, title),
        "kind": kind,
        "pattern": pattern,
        "barrier": barrier,
        "title": title,
        "severity": _normalize_severity(raw.get("severity")),
        "confidence": max(min(_safe_float(raw.get("confidence"), 0.5), 1.0), 0.0),
        "evidence": str(raw.get("evidence", "")).strip(),
        "trajectory": str(raw.get("trajectory", "")).strip(),
        "near_miss": bool(raw.get("near_miss", False)),
        "todo": str(raw.get("todo", "")).strip(),
    }


def _normalize_todo(raw: dict[str, Any], hole: dict[str, Any] | None = None) -> dict[str, Any]:
    title = str(raw.get("title", "")).strip() or (hole.get("todo", "") if hole else "")
    detail = str(raw.get("detail", "")).strip()
    hole_id = str(raw.get("hole_id", "")).strip() or (hole.get("id", "") if hole else "")
    digest = hashlib.sha1(f"{title}|{detail}|{hole_id}".encode("utf-8")).hexdigest()
    scope = str(raw.get("scope", "chat") or "chat").strip().lower()
    scope = scope if scope in {"chat", "project"} else "chat"
    return {
        "id": digest[:12],
        "title": title,
        "detail": detail,
        "severity": _normalize_severity(raw.get("severity") or (hole.get("severity", "medium") if hole else "medium")),
        "source": str(raw.get("source", "audit") or "audit"),
        "status": str(raw.get("status", "open") or "open"),
        "hole_id": hole_id,
        "scope": scope,
    }


def _normalize_near_miss(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title", "Near miss")).strip()
    detail = str(raw.get("detail", "")).strip()
    digest = hashlib.sha1(f"{title}|{detail}".encode("utf-8")).hexdigest()
    return {
        "id": digest[:12],
        "title": title,
        "detail": detail,
        "barrier": _normalize_barrier(raw.get("barrier")),
        "severity": _normalize_severity(raw.get("severity")),
        "confidence": max(min(_safe_float(raw.get("confidence"), 0.75), 1.0), 0.0),
        "created_at": str(raw.get("created_at", iso_now())),
        "fingerprint": str(raw.get("fingerprint", "")),
    }


def _heuristic_result(agent: "Agent", ctx_status: dict[str, Any]) -> dict[str, Any]:
    reasoning = str(agent.get_data(TRANSIENT_REASONING_KEY) or "")
    response = str(agent.get_data(TRANSIENT_RESPONSE_KEY) or "")
    turn_signal = _user_turn_signal(agent)
    holes: list[dict[str, Any]] = []
    todos: list[dict[str, Any]] = []

    chat_window = ctx_status.get("chat_model", {})
    if int(chat_window.get("current_tokens", 0) or 0) > int(chat_window.get("preferred_working_limit", 0) or 0):
        holes.append(
            {
                "kind": "latent_condition",
                "pattern": "excessive_context_occupancy",
                "barrier": "Readiness",
                "severity": "high",
                "confidence": 0.95,
                "title": "Working envelope exceeded",
                "evidence": f"current_tokens={chat_window.get('current_tokens')} preferred={chat_window.get('preferred_working_limit')}",
                "trajectory": "Large active context can hide constraints and verification evidence.",
                "todo": "Trim or summarize the active scope before continuing.",
            }
        )

    if not ctx_status.get("chat_model", {}).get("confirmed", False):
        holes.append(
            {
                "kind": "latent_condition",
                "pattern": "chat_ctx_unconfirmed",
                "barrier": "Readiness",
                "severity": "high",
                "confidence": 1.0,
                "title": "Chat context length is unconfirmed",
                "evidence": ctx_status.get("gate_diagnostics", {}).get("message")
                or "SwissCheese autonomy is gated until the active chat model context length is explicitly confirmed.",
                "trajectory": "Autonomous continuation may outrun the verified token budget.",
                "todo": "Confirm the active chat model context length before auto-recovery runs.",
            }
        )

    if turn_signal.get("drift_suspected"):
        holes.append(
            {
                "kind": "latent_condition",
                "pattern": "project_mismatch",
                "barrier": "Direction",
                "severity": "medium",
                "confidence": 0.82,
                "title": "Possible topic drift inside the current chat",
                "evidence": json.dumps(
                    {
                        "context": turn_signal.get("context_name", ""),
                        "project": turn_signal.get("project_title", ""),
                        "previous_overlap": turn_signal.get("previous_overlap", 0.0),
                        "anchor_overlap": turn_signal.get("anchor_overlap", 0.0),
                        "message_excerpt": turn_signal.get("message_excerpt", ""),
                    },
                    ensure_ascii=False,
                ),
                "trajectory": "A sharp topic pivot can hide scope drift and make same-project reachability look more relevant than it is.",
                "todo": "Acknowledge the topic drift explicitly and confirm whether the chat should switch scope.",
            }
        )

    prior_response_excerpt = str(turn_signal.get("previous_response_excerpt", "") or "")
    if (
        turn_signal.get("exact_repeat")
        and response
        and prior_response_excerpt
        and (
            _similarity(prior_response_excerpt, response) >= 0.55
            or not _references_prior_answer(response)
        )
    ):
        holes.append(
            {
                "kind": "active_failure",
                "pattern": "low_energy_effort",
                "barrier": "Direction",
                "severity": "medium",
                "confidence": 0.88,
                "title": "Repeated request produced overlapping work",
                "evidence": json.dumps(
                    {
                        "message_excerpt": turn_signal.get("message_excerpt", ""),
                        "previous_message_excerpt": turn_signal.get("previous_message_excerpt", ""),
                        "previous_response_excerpt": prior_response_excerpt,
                    },
                    ensure_ascii=False,
                ),
                "trajectory": "Exact-repeat requests should reference the prior answer or ask whether refinement is needed instead of regenerating the same work.",
                "todo": "Reference the prior answer instead of regenerating the full response.",
            }
        )

    lower_response = response.lower()
    if any(term in lower_response for term in ("done", "completed", "finished")) and not any(
        marker in lower_response for marker in ("test", "verified", "pytest", "read", "diff")
    ):
        holes.append(
            {
                "kind": "active_failure",
                "pattern": "premature_done",
                "barrier": "Stability",
                "severity": "medium",
                "confidence": 0.8,
                "title": "Premature completion language",
                "evidence": response[:240],
                "trajectory": "The response claims completion without visible verification evidence.",
                "todo": "Verify the claimed outcome before closing the task.",
            }
        )

    if any(term in lower_response for term in ("fixed", "implemented", "resolved")) and not any(
        marker in (reasoning + "\n" + response).lower()
        for marker in ("pytest", "verified", "checked", "read", "inspection", "test")
    ):
        holes.append(
            {
                "kind": "active_failure",
                "pattern": "skipped_verification",
                "barrier": "Direction",
                "severity": "medium",
                "confidence": 0.85,
                "title": "Verification likely skipped",
                "evidence": response[:240],
                "trajectory": "Unverified success claims can hide regressions or partial work.",
                "todo": "Run an explicit verification step or call out that verification was not performed.",
            }
        )

    if agent.context.get_data("_swiss_cheese_autonomy_origin"):
        auto_origin = agent.context.get_data("_swiss_cheese_autonomy_origin") or {}
        if str(auto_origin.get("fingerprint", "")) == str((agent.context.get_data("swiss_cheese_state") or {}).get("last_followup_fingerprint", "")):
            holes.append(
                {
                "kind": "active_failure",
                "pattern": "gaming_fake_progress",
                "barrier": "Coordination",
                "severity": "medium",
                "confidence": 0.9,
                    "title": "Redundant autonomous followup loop",
                    "evidence": json.dumps(auto_origin, ensure_ascii=False),
                    "trajectory": "Repeating the same autonomous followup can create self-loops instead of progress.",
                    "todo": "Stop repeating the same followup and request clarification or a new plan.",
                }
            )

    for hole in holes:
        if hole.get("todo"):
            todos.append(
                {
                    "title": hole["todo"],
                    "detail": hole.get("trajectory", ""),
                    "severity": hole.get("severity", "medium"),
                    "source": "heuristic_fallback",
                }
            )

    return {
        "summary": "Heuristic fallback audit completed.",
        "confidence": 0.75,
        "holes": holes,
        "todos": todos,
        "near_misses": [],
        "followups": [],
    }


def _parse_or_fallback(agent: "Agent", response: str, ctx_status: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    parsed = _safe_json_load(response)
    if parsed is not None:
        return parsed, False
    return _heuristic_result(agent, ctx_status), True


def _make_current_chat_nudge_fingerprint(
    context_id: str,
    hole_ids: list[str],
    project_todo_ids: list[str],
) -> str:
    payload = json.dumps(
        {
            "context_id": context_id,
            "hole_ids": sorted(set(hole_ids)),
            "project_todo_ids": sorted(set(project_todo_ids)),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _build_current_chat_nudge_message(
    new_high_confidence_holes: list[dict[str, Any]],
    new_project_todo_ids: list[str],
) -> str:
    lines = ["Review the latest SwissCheese findings for this chat before continuing."]
    if new_high_confidence_holes:
        titles = ", ".join(hole.get("title", hole.get("pattern", "finding")) for hole in new_high_confidence_holes[:3])
        lines.append(f"New high-confidence findings: {titles}.")
    if new_project_todo_ids:
        lines.append("The shared project backlog now has new SwissCheese actions.")
    lines.append("Check SwissCheese status, address the highest-priority open todo, and continue only after an explicit verification step.")
    return " ".join(lines)


def _maybe_queue_current_chat_nudge(
    agent: "Agent",
    *,
    plugin_config: dict[str, Any],
    new_high_confidence_holes: list[dict[str, Any]],
    new_project_todo_ids: list[str],
) -> None:
    if not new_high_confidence_holes and not new_project_todo_ids:
        return

    project_name = project_state.get_project_name(agent.context)
    fingerprint = _make_current_chat_nudge_fingerprint(
        agent.context.id,
        [str(hole.get("id", "")) for hole in new_high_confidence_holes],
        new_project_todo_ids,
    )
    if state_helper.has_notification_fingerprint(agent.context, fingerprint):
        return
    if project_name and project_state.has_notification_fingerprint(project_name, fingerprint):
        return

    current_target = discovery.inspect_target(
        source_context=agent.context,
        scope=agent.context.get_data("cross_chat_scope") or plugin_config.get("cross_chat_scope", {}),
    ).get("target") or {}
    queued, _info = state_helper.queue_followup(
        agent.context,
        target_key=str(current_target.get("target_key", "") or f"chat:{agent.context.id}"),
        target_kind=str(current_target.get("kind", "chat") or "chat"),
        target_context_id=str(current_target.get("context_id", "") or agent.context.id),
        target_task_uuid=str(((current_target.get("scheduler") or {}) if isinstance(current_target.get("scheduler"), dict) else {}).get("uuid", "") or ""),
        target_name=str(current_target.get("name", "") or agent.context.name or agent.context.id),
        reason="swiss_cheese_review",
        message=_build_current_chat_nudge_message(new_high_confidence_holes, new_project_todo_ids),
        auto_send=True,
        source="audit",
        plugin_config=plugin_config,
    )
    if not queued:
        return

    state_helper.record_notification_fingerprint(
        agent.context,
        fingerprint,
        reason="current_chat_nudge",
        plugin_config=plugin_config,
    )
    if project_name:
        project_state.record_notification_fingerprint(
            project_name,
            fingerprint,
            reason="current_chat_nudge",
        )


def _apply_audit_result(
    agent: "Agent",
    payload: dict[str, Any],
    *,
    ctx_status: dict[str, Any],
    used_fallback: bool,
    plugin_config: dict[str, Any],
) -> None:
    existing_hole_ids = {
        str(item.get("id", ""))
        for item in list(agent.context.get_data("holes") or [])
        if isinstance(item, dict)
    }
    existing_project_todo_ids = {
        str(item.get("id", ""))
        for item in project_state.list_project_todos(agent.context, status="all")
    }
    normalized_holes = [_normalize_hole(item) for item in payload.get("holes", []) if isinstance(item, dict)]
    state_helper.set_holes(agent.context, normalized_holes, plugin_config=plugin_config)

    for hole in normalized_holes:
        if hole.get("todo"):
            state_helper.add_or_update_todo(
                agent.context,
                _normalize_todo({"title": hole["todo"], "detail": hole.get("trajectory", ""), "severity": hole.get("severity")}, hole=hole),
                plugin_config=plugin_config,
            )

    new_project_todo_ids: list[str] = []
    for raw_todo in payload.get("todos", []):
        if isinstance(raw_todo, dict):
            normalized_todo = _normalize_todo(raw_todo)
            if normalized_todo.get("scope") == "project" and project_state.get_project_name(agent.context):
                project_todo = project_state.add_or_update_project_todo(
                    agent.context,
                    normalized_todo,
                    plugin_config=plugin_config,
                )
                if project_todo and project_todo.get("id") not in existing_project_todo_ids:
                    new_project_todo_ids.append(str(project_todo.get("id", "")))
            else:
                state_helper.add_or_update_todo(
                    agent.context,
                    normalized_todo,
                    plugin_config=plugin_config,
                )

    for raw_near_miss in payload.get("near_misses", []):
        if isinstance(raw_near_miss, dict):
            state_helper.record_near_miss(
                agent.context,
                _normalize_near_miss(raw_near_miss),
                plugin_config=plugin_config,
            )

    audit_trace_entry = {
        "created_at": iso_now(),
        "summary": str(payload.get("summary", "SwissCheese audit completed.")).strip(),
        "used_fallback": used_fallback,
        "hole_patterns": [hole.get("pattern", "") for hole in normalized_holes],
    }
    state_helper.append_audit_trace(agent.context, audit_trace_entry, plugin_config=plugin_config)

    state_helper.set_audit_status(
        agent.context,
        {
            "state": "complete",
            "summary": audit_trace_entry["summary"],
            "used_fallback": used_fallback,
            "last_error": "",
            "last_audit_at": audit_trace_entry["created_at"],
        },
        plugin_config=plugin_config,
    )

    new_high_confidence_holes = [
        hole
        for hole in normalized_holes
        if str(hole.get("id", "")) not in existing_hole_ids and float(hole.get("confidence", 0.0) or 0.0) >= 0.85
    ]
    _maybe_queue_current_chat_nudge(
        agent,
        plugin_config=plugin_config,
        new_high_confidence_holes=new_high_confidence_holes,
        new_project_todo_ids=new_project_todo_ids,
    )

    for raw_followup in payload.get("followups", []):
        if not isinstance(raw_followup, dict):
            continue
        reason = str(raw_followup.get("reason", "")).strip()
        message = str(raw_followup.get("message", "")).strip()
        if not reason or not message:
            continue
        target = str(raw_followup.get("target", "current_target") or "current_target")
        target_key_raw = str(raw_followup.get("target_key", "") or "").strip()
        target_context_id_raw = str(raw_followup.get("target_context_id", "") or "").strip()
        auto_send = bool(raw_followup.get("auto_send", False))
        inspection = discovery.inspect_target(
            source_context=agent.context,
            selector="" if target in {"current_chat", "current_target"} else target,
            target_key=target_key_raw,
            target_context_id=target_context_id_raw,
            scope=agent.context.get_data("cross_chat_scope") or {},
        )
        target_meta = inspection.get("target") or {}
        if not target_meta or not inspection.get("permissions", {}).get("can_queue", False):
            selector_label = target_key_raw or target_context_id_raw or target
            blocked_reason = "target_not_found" if not target_meta else "target_not_queueable_in_scope"
            state_helper.record_blocked_followup(
                agent.context,
                target_key=target_key_raw,
                target_context_id=target_context_id_raw,
                target_kind=str((target_meta or {}).get("kind", "chat") or "chat"),
                target_task_uuid=str((((target_meta or {}).get("scheduler") or {}) if isinstance((target_meta or {}).get("scheduler"), dict) else {}).get("uuid", "") or ""),
                target_name=str((target_meta or {}).get("name", "") or selector_label),
                reason=reason,
                message=message,
                blocked_reason=blocked_reason,
                auto_send=auto_send,
                source="audit",
                plugin_config=plugin_config,
            )
            state_helper.record_near_miss(
                agent.context,
                {
                    "title": "Followup queue blocked",
                    "detail": f"SwissCheese rejected queued followup for target '{selector_label}'.",
                    "barrier": "Coordination",
                    "severity": "medium",
                    "confidence": 1.0,
                },
                plugin_config=plugin_config,
            )
            continue
        queued, info = state_helper.queue_followup(
            agent.context,
            target_key=str(target_meta.get("target_key", "") or ""),
            target_kind=str(target_meta.get("kind", "chat") or "chat"),
            target_context_id=str(target_meta.get("context_id", "") or agent.context.id),
            target_task_uuid=str(((target_meta.get("scheduler") or {}) if isinstance(target_meta.get("scheduler"), dict) else {}).get("uuid", "") or ""),
            target_name=str(target_meta.get("name", "") or ""),
            reason=reason,
            message=message,
            auto_send=auto_send,
            source="audit",
            plugin_config=plugin_config,
        )
        if not queued:
            state_helper.record_near_miss(
                agent.context,
                {
                    "title": "Followup deduplicated",
                    "detail": f"SwissCheese rejected followup '{reason}' because {info.get('reason', 'it was not allowed')}.",
                    "barrier": "Coordination",
                    "severity": "low",
                    "confidence": 1.0,
                    "fingerprint": info.get("fingerprint", ""),
                },
                plugin_config=plugin_config,
            )


async def run_background_audit(agent: "Agent") -> None:
    if agent.number != 0:
        return

    plugin_config = swiss_config.get_plugin_config(agent)
    state_helper.set_audit_status(
        agent.context,
        {"state": "pending", "summary": "SwissCheese audit running...", "used_fallback": False},
        plugin_config=plugin_config,
    )

    system_prompt = agent.read_prompt("swiss_cheese.audit.sys.md")
    ctx_status = context_window.compute_context_window_status(agent, plugin_config=plugin_config)
    ctx_status["scope"] = _build_scope_payload(
        agent,
        plugin_config,
        agent.context.get_data("cross_chat_scope") or plugin_config.get("cross_chat_scope", {}),
    )
    provisional_user_message = _build_audit_message(agent, plugin_config, ctx_status)
    agent.context.set_data(
        TRANSIENT_LAST_UTILITY_INPUT_KEY,
        {
            "tokens": tokens.approximate_tokens(system_prompt + "\n" + provisional_user_message),
            "captured_at": iso_now(),
        },
    )
    ctx_status = context_window.compute_context_window_status(agent, plugin_config=plugin_config)
    ctx_status["scope"] = _build_scope_payload(
        agent,
        plugin_config,
        agent.context.get_data("cross_chat_scope") or plugin_config.get("cross_chat_scope", {}),
    )
    user_message = _build_audit_message(agent, plugin_config, ctx_status)
    agent.context.set_data(
        TRANSIENT_LAST_UTILITY_INPUT_KEY,
        {
            "tokens": tokens.approximate_tokens(system_prompt + "\n" + user_message),
            "captured_at": iso_now(),
        },
    )

    try:
        response = await agent.call_utility_model(
            system=system_prompt,
            message=user_message,
            background=True,
        )
        parsed, used_fallback = _parse_or_fallback(agent, response, ctx_status)
        _apply_audit_result(
            agent,
            parsed,
            ctx_status=ctx_status,
            used_fallback=used_fallback,
            plugin_config=plugin_config,
        )
    except Exception as exc:
        fallback_payload = _heuristic_result(agent, ctx_status)
        _apply_audit_result(
            agent,
            fallback_payload,
            ctx_status=ctx_status,
            used_fallback=True,
            plugin_config=plugin_config,
        )
        state_helper.set_audit_status(
            agent.context,
            {
                "state": "fallback",
                "summary": "SwissCheese audit used heuristic fallback.",
                "used_fallback": True,
                "last_error": str(exc),
                "last_audit_at": iso_now(),
            },
            plugin_config=plugin_config,
        )
    finally:
        agent.set_data(TRANSIENT_AUDIT_TASK_KEY, None)


def schedule_background_audit(agent: "Agent") -> asyncio.Task | None:
    if agent.number != 0:
        return None
    existing = agent.get_data(TRANSIENT_AUDIT_TASK_KEY)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(run_background_audit(agent))
    agent.set_data(TRANSIENT_AUDIT_TASK_KEY, task)
    return task


def should_block_autonomous_tool(agent: "Agent", tool_name: str) -> tuple[bool, str]:
    origin = agent.context.get_data("_swiss_cheese_autonomy_origin") or {}
    if not origin:
        return False, ""
    ctx_confirmation = agent.context.get_data("ctx_confirmation") or {}
    if ctx_confirmation.get("gate_active", False):
        return True, "chat_ctx_confirmation_gate"

    holes = agent.context.get_data("holes") or []
    if any(
        hole.get("pattern") in DANGEROUS_AUTONOMOUS_PATTERNS
        and hole.get("severity") in ("high", "critical")
        for hole in holes
        if isinstance(hole, dict)
    ):
        return True, "unsafe_autonomous_followup"

    return False, ""
