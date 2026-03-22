from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from helpers import plugins, projects

from usr.plugins.swiss_cheese.helpers.constants import MODEL_SLOTS, PLUGIN_NAME

if TYPE_CHECKING:
    from agent import Agent


DEFAULT_CONFIG: dict[str, Any] = {
    "preferred_working_limit": 100000,
    "advisory_threshold": 128000,
    "allow_large_context_override": False,
    "utility_confirmation_warning": True,
    "max_auto_recovery_cycles": 2,
    "max_holes": 12,
    "max_near_misses": 20,
    "max_todos": 20,
    "max_audit_traces": 20,
    "max_followup_queue": 8,
    "audit_history_messages": 10,
    "cross_chat_scope": {
        "same_project_live_write": False,
        "same_project_persisted_readonly": False,
        "cross_project": False,
    },
    "confirmed_model_tuples": {
        "chat_model": [],
        "utility_model": [],
    },
}


def _merge_defaults(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_scope_from_agent(agent: "Agent | None") -> tuple[str, str]:
    if not agent:
        return "", ""
    return (
        projects.get_context_project_name(agent.context) or "",
        agent.config.profile or "",
    )


def normalize_confirmed_model_tuples(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    registry = config.get("confirmed_model_tuples", {})
    normalized: dict[str, list[dict[str, Any]]] = {slot: [] for slot in MODEL_SLOTS}

    for slot in MODEL_SLOTS:
        entries = registry.get(slot, []) if isinstance(registry, dict) else []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider", "")).strip()
            name = str(entry.get("name", "")).strip()
            try:
                ctx_length = int(entry.get("ctx_length", 0) or 0)
            except (TypeError, ValueError):
                ctx_length = 0
            if not provider or not name or ctx_length <= 0:
                continue
            normalized[slot].append(
                {
                    "provider": provider,
                    "name": name,
                    "ctx_length": ctx_length,
                    "confirmed_at": str(entry.get("confirmed_at", "")),
                }
            )
    return normalized


def normalize_plugin_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    config = _merge_defaults(DEFAULT_CONFIG, raw or {})
    config["confirmed_model_tuples"] = normalize_confirmed_model_tuples(config)
    return config


def get_plugin_config(
    agent: "Agent | None" = None,
    project_name: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, Any]:
    if project_name is None and agent is not None:
        project_name, agent_profile = get_scope_from_agent(agent)
    raw = plugins.get_plugin_config(
        PLUGIN_NAME,
        agent=agent,
        project_name=project_name,
        agent_profile=agent_profile,
    )
    return normalize_plugin_config(raw)


def save_plugin_config(project_name: str, agent_profile: str, settings: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_plugin_config(settings)
    plugins.save_plugin_config(
        PLUGIN_NAME,
        project_name=project_name,
        agent_profile=agent_profile,
        settings=normalized,
    )
    return normalized


def get_model_config(
    agent: "Agent | None" = None,
    project_name: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, Any]:
    if project_name is None and agent is not None:
        project_name, agent_profile = get_scope_from_agent(agent)
    raw = plugins.get_plugin_config(
        "_model_config",
        agent=agent,
        project_name=project_name,
        agent_profile=agent_profile,
    ) or plugins.get_default_plugin_config("_model_config") or {}
    return deepcopy(raw)


def save_model_config(project_name: str, agent_profile: str, config: dict[str, Any]) -> dict[str, Any]:
    plugins.save_plugin_config(
        "_model_config",
        project_name=project_name,
        agent_profile=agent_profile,
        settings=config,
    )
    return deepcopy(config)


def append_confirmed_tuple(
    config: dict[str, Any],
    slot: str,
    tuple_data: dict[str, Any],
) -> dict[str, Any]:
    registry = normalize_confirmed_model_tuples(config)
    entries = registry.setdefault(slot, [])
    new_key = (
        str(tuple_data.get("provider", "")),
        str(tuple_data.get("name", "")),
        int(tuple_data.get("ctx_length", 0) or 0),
    )
    if all(
        (
            entry.get("provider", ""),
            entry.get("name", ""),
            int(entry.get("ctx_length", 0) or 0),
        )
        != new_key
        for entry in entries
    ):
        entries.append(
            {
                "provider": new_key[0],
                "name": new_key[1],
                "ctx_length": new_key[2],
                "confirmed_at": str(tuple_data.get("confirmed_at", "")),
            }
        )
    config["confirmed_model_tuples"] = registry
    return config
