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


def _tuple_mismatch_reasons(entry: dict[str, Any], tuple_data: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if str(entry.get("provider", "")) != str(tuple_data.get("provider", "")):
        reasons.append("provider_mismatch")
    if str(entry.get("name", "")) != str(tuple_data.get("name", "")):
        reasons.append("name_mismatch")
    if int(entry.get("ctx_length", 0) or 0) != int(tuple_data.get("ctx_length", 0) or 0):
        reasons.append("ctx_length_mismatch")
    return reasons


def _best_registry_entry(entries: list[dict[str, Any]], tuple_data: dict[str, Any]) -> dict[str, Any] | None:
    if not entries:
        return None

    def _score(entry: dict[str, Any]) -> tuple[int, int]:
        exact_fields = 0
        if str(entry.get("provider", "")) == str(tuple_data.get("provider", "")):
            exact_fields += 1
        if str(entry.get("name", "")) == str(tuple_data.get("name", "")):
            exact_fields += 1
        if int(entry.get("ctx_length", 0) or 0) == int(tuple_data.get("ctx_length", 0) or 0):
            exact_fields += 1
        ctx_delta = abs(int(entry.get("ctx_length", 0) or 0) - int(tuple_data.get("ctx_length", 0) or 0))
        return (exact_fields, -ctx_delta)

    return max(entries, key=_score)


def _build_confirmation_diagnostics(
    plugin_config: dict[str, Any],
    slot: str,
    tuple_data: dict[str, Any],
    mirrored_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = swiss_config.normalize_confirmed_model_tuples(plugin_config).get(slot, [])
    confirmed = bool(
        tuple_data.get("provider")
        and tuple_data.get("name")
        and int(tuple_data.get("ctx_length", 0) or 0) > 0
        and any(tuple_matches(entry, tuple_data) for entry in registry)
    )

    mirrored = dict(mirrored_snapshot or {})
    mirrored_tuple = {
        "provider": str(mirrored.get("provider", "") or ""),
        "name": str(mirrored.get("name", "") or ""),
        "ctx_length": int(mirrored.get("ctx_length", 0) or 0),
    }
    mirrored_snapshot_stale = bool(
        any(mirrored_tuple.values()) and not tuple_matches(mirrored_tuple, tuple_data)
    )

    reasons: list[str] = []
    closest = None
    if not str(tuple_data.get("provider", "")).strip():
        reasons.append("provider_unconfigured")
    if not str(tuple_data.get("name", "")).strip():
        reasons.append("name_unconfigured")
    if int(tuple_data.get("ctx_length", 0) or 0) <= 0:
        reasons.append("ctx_length_unconfigured")
    if not registry:
        reasons.append("no_confirmed_tuples")

    if not confirmed and registry:
        closest = _best_registry_entry(registry, tuple_data)
        if closest:
            reasons.extend(_tuple_mismatch_reasons(closest, tuple_data))
        else:
            reasons.append("no_matching_confirmed_tuple")

    if mirrored_snapshot_stale:
        reasons.append("mirrored_snapshot_stale")

    reasons = list(dict.fromkeys(reasons))
    reason = "confirmed" if confirmed else (reasons[0] if reasons else "unconfirmed")
    return {
        "confirmed": confirmed,
        "reason": reason,
        "reasons": reasons,
        "confirmed_tuple_count": len(registry),
        "closest_confirmed_tuple": (
            {
                "provider": str((closest or {}).get("provider", "") or ""),
                "name": str((closest or {}).get("name", "") or ""),
                "ctx_length": int((closest or {}).get("ctx_length", 0) or 0),
                "confirmed_at": str((closest or {}).get("confirmed_at", "") or ""),
            }
            if closest
            else None
        ),
        "mirrored_snapshot_stale": mirrored_snapshot_stale,
        "mirrored_snapshot": mirrored if mirrored else None,
    }


def _diagnostic_message(slot: str, diagnostics: dict[str, Any]) -> str:
    if diagnostics.get("confirmed"):
        return ""
    label = "chat" if slot == "chat_model" else "utility"
    reasons = list(diagnostics.get("reasons", []) or [])
    closest = diagnostics.get("closest_confirmed_tuple") or {}
    if "mirrored_snapshot_stale" in reasons:
        return f"The mirrored {label} model snapshot is stale relative to the live model tuple."
    if "no_confirmed_tuples" in reasons:
        return f"No confirmed {label} model tuple exists for the current scope."
    if closest:
        return (
            f"The live {label} model tuple does not exactly match the closest confirmed tuple "
            f"({closest.get('provider', '')}/{closest.get('name', '')}, ctx={closest.get('ctx_length', 0)})."
        )
    return f"The live {label} model tuple is not confirmed for the current scope."


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
    mirrored_confirmation = agent.context.get_data(CTX_CONFIRMATION_KEY) or {}

    for slot, payload in slot_payloads.items():
        tuple_data = build_model_tuple(slot, payload["settings"])
        hard_limit = int(tuple_data.get("ctx_length", 0) or 0)
        effective_preferred = min(preferred_limit, hard_limit) if hard_limit > 0 else preferred_limit
        current_tokens = max(int(payload.get("current_tokens", 0) or 0), 0)
        remaining_budget = max(hard_limit - current_tokens, 0) if hard_limit > 0 else 0
        working_remaining = max(effective_preferred - current_tokens, 0)
        diagnostics = _build_confirmation_diagnostics(
            plugin_config,
            slot,
            tuple_data,
            mirrored_snapshot=mirrored_confirmation.get(slot, {}),
        )
        confirmed = bool(diagnostics.get("confirmed", False))
        advisory_active = hard_limit > advisory_threshold and not allow_large_override
        snapshots[slot] = {
            **tuple_data,
            "hard_limit": hard_limit,
            "preferred_working_limit": effective_preferred,
            "current_tokens": current_tokens,
            "remaining_budget": remaining_budget,
            "working_remaining_budget": working_remaining,
            "confirmed": confirmed,
            "confirmation_reason": diagnostics.get("reason", ""),
            "confirmation_reasons": diagnostics.get("reasons", []),
            "confirmation_diagnostics": diagnostics,
            "status_message": _diagnostic_message(slot, diagnostics),
            "advisory_active": advisory_active,
            "manual_search_query": build_manual_search_query(tuple_data["provider"], tuple_data["name"]),
            "captured_at": iso_now(),
        }

    gate_active = not snapshots["chat_model"]["confirmed"]
    confidence = "reduced" if not snapshots["utility_model"]["confirmed"] else "normal"
    gate_diagnostics = {
        "active": gate_active,
        "reason": snapshots["chat_model"].get("confirmation_reason", ""),
        "reasons": snapshots["chat_model"].get("confirmation_reasons", []),
        "message": snapshots["chat_model"].get("status_message", ""),
        "slot": "chat_model",
    }
    utility_warning_diagnostics = {
        "active": not snapshots["utility_model"]["confirmed"],
        "reason": snapshots["utility_model"].get("confirmation_reason", ""),
        "reasons": snapshots["utility_model"].get("confirmation_reasons", []),
        "message": snapshots["utility_model"].get("status_message", ""),
        "slot": "utility_model",
    }

    return {
        "chat_model": snapshots["chat_model"],
        "utility_model": snapshots["utility_model"],
        "gate_active": gate_active,
        "gate_diagnostics": gate_diagnostics,
        "utility_warning_active": not snapshots["utility_model"]["confirmed"],
        "utility_warning_diagnostics": utility_warning_diagnostics,
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
        "gate_diagnostics": dict(status.get("gate_diagnostics", {}) or {}),
        "utility_warning_active": bool(status.get("utility_warning_active", False)),
        "utility_warning_diagnostics": dict(status.get("utility_warning_diagnostics", {}) or {}),
        "utility_confidence": status.get("utility_confidence", "normal"),
        "diagnostics": {
            "gate": dict(status.get("gate_diagnostics", {}) or {}),
            "utility": dict(status.get("utility_warning_diagnostics", {}) or {}),
            "chat_model": dict((status.get("chat_model", {}) or {}).get("confirmation_diagnostics", {}) or {}),
            "utility_model": dict((status.get("utility_model", {}) or {}).get("confirmation_diagnostics", {}) or {}),
        },
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
