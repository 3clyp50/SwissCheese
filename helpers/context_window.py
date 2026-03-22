from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from plugins._model_config.helpers import model_config as model_config_helper

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers.constants import (
    CTX_CONFIRMATION_KEY,
    CROSS_CHAT_SCOPE_KEY,
    MODEL_SLOTS,
    RECOVERY_BUDGET_KEY,
    TRANSIENT_LAST_UTILITY_INPUT_KEY,
)

if TYPE_CHECKING:
    from agent import Agent, AgentContext


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_model_tuple(slot: str, model_settings: dict[str, Any]) -> dict[str, Any]:
    provider = str(model_settings.get("provider", "")).strip()
    name = str(model_settings.get("name", "")).strip()
    try:
        ctx_length = int(model_settings.get("ctx_length", 0) or 0)
    except (TypeError, ValueError):
        ctx_length = 0
    return {
        "slot": slot,
        "provider": provider,
        "name": name,
        "ctx_length": ctx_length,
    }


def tuple_matches(entry: dict[str, Any], tuple_data: dict[str, Any]) -> bool:
    return (
        str(entry.get("provider", "")) == str(tuple_data.get("provider", ""))
        and str(entry.get("name", "")) == str(tuple_data.get("name", ""))
        and int(entry.get("ctx_length", 0) or 0) == int(tuple_data.get("ctx_length", 0) or 0)
    )


def is_confirmed(plugin_config: dict[str, Any], slot: str, tuple_data: dict[str, Any]) -> bool:
    registry = swiss_config.normalize_confirmed_model_tuples(plugin_config)
    return any(tuple_matches(entry, tuple_data) for entry in registry.get(slot, []))


def build_manual_search_query(provider: str, name: str) -> str:
    terms = " ".join(part for part in (provider.strip(), name.strip(), "context window tokens") if part)
    return f"\"{terms}\"".strip()


def _normalize_scope(plugin_config: dict[str, Any]) -> dict[str, bool]:
    scope = dict((plugin_config or {}).get("cross_chat_scope", {}) or {})
    return {
        "same_project_live_write": bool(scope.get("same_project_live_write", False)),
        "same_project_persisted_readonly": bool(scope.get("same_project_persisted_readonly", False)),
        "cross_project": bool(scope.get("cross_project", False)),
    }


def compute_context_window_status(agent: "Agent", plugin_config: dict[str, Any] | None = None) -> dict[str, Any]:
    plugin_config = plugin_config or swiss_config.get_plugin_config(agent)

    chat_model = model_config_helper.get_chat_model_config(agent)
    utility_model = model_config_helper.get_utility_model_config(agent)
    live_ctx_window = agent.get_data(agent.DATA_NAME_CTX_WINDOW) or {}
    utility_input = agent.context.get_data(TRANSIENT_LAST_UTILITY_INPUT_KEY) or {}

    snapshots: dict[str, dict[str, Any]] = {}

    slot_payloads = {
        "chat_model": {
            "settings": chat_model,
            "current_tokens": int(live_ctx_window.get("tokens", 0) or 0),
        },
        "utility_model": {
            "settings": utility_model,
            "current_tokens": int(utility_input.get("tokens", 0) or 0),
        },
    }

    preferred_limit = int(plugin_config.get("preferred_working_limit", 100000) or 100000)
    advisory_threshold = int(plugin_config.get("advisory_threshold", 128000) or 128000)
    allow_large_override = bool(plugin_config.get("allow_large_context_override", False))

    for slot, payload in slot_payloads.items():
        tuple_data = build_model_tuple(slot, payload["settings"])
        hard_limit = int(tuple_data.get("ctx_length", 0) or 0)
        effective_preferred = min(preferred_limit, hard_limit) if hard_limit > 0 else preferred_limit
        current_tokens = max(int(payload.get("current_tokens", 0) or 0), 0)
        remaining_budget = max(hard_limit - current_tokens, 0) if hard_limit > 0 else 0
        working_remaining = max(effective_preferred - current_tokens, 0)
        confirmed = bool(tuple_data["provider"] and tuple_data["name"] and hard_limit > 0 and is_confirmed(plugin_config, slot, tuple_data))
        advisory_active = hard_limit > advisory_threshold and not allow_large_override
        snapshots[slot] = {
            **tuple_data,
            "hard_limit": hard_limit,
            "preferred_working_limit": effective_preferred,
            "current_tokens": current_tokens,
            "remaining_budget": remaining_budget,
            "working_remaining_budget": working_remaining,
            "confirmed": confirmed,
            "advisory_active": advisory_active,
            "manual_search_query": build_manual_search_query(tuple_data["provider"], tuple_data["name"]),
            "captured_at": iso_now(),
        }

    gate_active = not snapshots["chat_model"]["confirmed"]
    confidence = "reduced" if not snapshots["utility_model"]["confirmed"] else "normal"

    return {
        "chat_model": snapshots["chat_model"],
        "utility_model": snapshots["utility_model"],
        "gate_active": gate_active,
        "utility_warning_active": not snapshots["utility_model"]["confirmed"],
        "utility_confidence": confidence,
        "preferred_working_limit": preferred_limit,
        "advisory_threshold": advisory_threshold,
        "allow_large_context_override": allow_large_override,
        "computed_at": iso_now(),
    }


def mirror_context_window_status(
    context: "AgentContext",
    status: dict[str, Any],
    recovery_budget: dict[str, Any],
    cross_chat_scope: dict[str, Any],
) -> None:
    confirmation = {
        "chat_model": status.get("chat_model", {}),
        "utility_model": status.get("utility_model", {}),
        "gate_active": bool(status.get("gate_active", False)),
        "utility_warning_active": bool(status.get("utility_warning_active", False)),
        "utility_confidence": status.get("utility_confidence", "normal"),
        "computed_at": status.get("computed_at", iso_now()),
    }
    context.set_data(CTX_CONFIRMATION_KEY, confirmation)
    context.set_data(RECOVERY_BUDGET_KEY, recovery_budget)
    context.set_data(CROSS_CHAT_SCOPE_KEY, cross_chat_scope or _normalize_scope({}))
    context.set_output_data(CTX_CONFIRMATION_KEY, confirmation)
    context.set_output_data(RECOVERY_BUDGET_KEY, recovery_budget)
    context.set_output_data(CROSS_CHAT_SCOPE_KEY, cross_chat_scope or _normalize_scope({}))


def resolve_scope_status(agent: "Agent") -> tuple[dict[str, Any], dict[str, bool]]:
    plugin_config = swiss_config.get_plugin_config(agent)
    return plugin_config, _normalize_scope(plugin_config)
