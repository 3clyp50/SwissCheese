from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from helpers import files, plugins, projects
from helpers import yaml as yaml_helper

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


def _normalize_project_name(project_name: str | None) -> str:
    return str(project_name or "").strip()


def _normalize_agent_profile(agent_profile: str | None) -> str:
    return str(agent_profile or "").strip()


def _load_config_payload(path: str) -> dict[str, Any] | None:
    if not path or not files.exists(path):
        return None
    try:
        payload = (json.loads if path.lower().endswith(".json") else yaml_helper.loads)(files.read_file(path))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _default_config_path(plugin_name: str) -> str:
    plugin_dir = plugins.find_plugin_dir(plugin_name)
    if not plugin_dir:
        return ""
    return files.get_abs_path(plugin_dir, plugins.CONFIG_DEFAULT_FILE_NAME)


def _exact_config_entry(plugin_name: str, project_name: str, agent_profile: str) -> dict[str, Any] | None:
    exact_lookup = getattr(plugins, "_swiss_cheese_exact_config_lookup", None)
    if callable(exact_lookup):
        entry = exact_lookup(plugin_name, project_name, agent_profile)
        if entry:
            payload = dict(entry)
            payload["project_name"] = _normalize_project_name(payload.get("project_name", project_name))
            payload["agent_profile"] = _normalize_agent_profile(payload.get("agent_profile", agent_profile))
            return payload

    path = plugins.determine_plugin_asset_path(
        plugin_name,
        _normalize_project_name(project_name),
        _normalize_agent_profile(agent_profile),
        plugins.CONFIG_FILE_NAME,
    )
    settings = _load_config_payload(path)
    if settings is None:
        return None
    return {
        "settings": settings,
        "path": path,
        "project_name": _normalize_project_name(project_name),
        "agent_profile": _normalize_agent_profile(agent_profile),
    }


def get_plugin_save_scope(project_name: str | None = None) -> tuple[str, str]:
    return (_normalize_project_name(project_name), "")


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


def resolve_plugin_config_scope(
    *,
    agent: "Agent | None" = None,
    project_name: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, Any]:
    if project_name is None and agent is not None:
        project_name, agent_profile = get_scope_from_agent(agent)

    project_name = _normalize_project_name(project_name)
    agent_profile = _normalize_agent_profile(agent_profile)
    save_project_name, save_agent_profile = get_plugin_save_scope(project_name)
    save_scope = "project" if save_project_name else "global"
    save_path = plugins.determine_plugin_asset_path(
        PLUGIN_NAME,
        save_project_name,
        save_agent_profile,
        plugins.CONFIG_FILE_NAME,
    )

    candidates: list[tuple[str, str, str, bool]] = []
    if project_name:
        candidates.append(("project", project_name, "", False))
        if agent_profile:
            candidates.append(("project", project_name, agent_profile, True))
    candidates.append(("global", "", "", False))
    if agent_profile:
        candidates.append(("global", "", agent_profile, True))

    loaded_from = {
        "scope": "default",
        "project_name": "",
        "agent_profile": "",
        "path": _default_config_path(PLUGIN_NAME),
        "legacy_profile": False,
    }
    raw_config: dict[str, Any] | None = None

    for scope_name, scope_project_name, scope_agent_profile, legacy_profile in candidates:
        entry = _exact_config_entry(PLUGIN_NAME, scope_project_name, scope_agent_profile)
        if entry is None:
            continue
        raw_config = dict(entry.get("settings", {}) or {})
        loaded_from = {
            "scope": scope_name,
            "project_name": str(entry.get("project_name", scope_project_name) or ""),
            "agent_profile": str(entry.get("agent_profile", scope_agent_profile) or ""),
            "path": str(entry.get("path", "") or ""),
            "legacy_profile": legacy_profile,
        }
        break

    if raw_config is None:
        raw_config = plugins.get_default_plugin_config(PLUGIN_NAME) or {}

    return {
        "config": normalize_plugin_config(raw_config),
        "applies_to": {
            "scope": save_scope,
            "project_name": save_project_name,
            "agent_profile": save_agent_profile,
            "path": save_path,
        },
        "loaded_from": loaded_from,
        "legacy_absorbed": bool(loaded_from.get("legacy_profile", False)),
    }


def get_plugin_config(
    agent: "Agent | None" = None,
    project_name: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, Any]:
    return resolve_plugin_config_scope(
        agent=agent,
        project_name=project_name,
        agent_profile=agent_profile,
    )["config"]


def save_plugin_config(project_name: str, agent_profile: str, settings: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_plugin_config(settings)
    save_project_name, save_agent_profile = get_plugin_save_scope(project_name)
    plugins.save_plugin_config(
        PLUGIN_NAME,
        project_name=save_project_name,
        agent_profile=save_agent_profile,
        settings=normalized,
    )
    return normalized


def resolve_model_config_scope(
    *,
    agent: "Agent | None" = None,
    project_name: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, Any]:
    if project_name is None and agent is not None:
        project_name, agent_profile = get_scope_from_agent(agent)

    project_name = _normalize_project_name(project_name)
    agent_profile = _normalize_agent_profile(agent_profile)

    candidates: list[tuple[str, str, str]] = []
    if project_name and agent_profile:
        candidates.append(("project_agent", project_name, agent_profile))
    if project_name:
        candidates.append(("project", project_name, ""))
    if agent_profile:
        candidates.append(("agent", "", agent_profile))
    candidates.append(("global", "", ""))

    loaded_from = {
        "scope": "default",
        "project_name": "",
        "agent_profile": "",
        "path": _default_config_path("_model_config"),
    }
    raw_config: dict[str, Any] | None = None

    for scope_name, scope_project_name, scope_agent_profile in candidates:
        entry = _exact_config_entry("_model_config", scope_project_name, scope_agent_profile)
        if entry is None:
            continue
        raw_config = dict(entry.get("settings", {}) or {})
        loaded_from = {
            "scope": scope_name,
            "project_name": str(entry.get("project_name", scope_project_name) or ""),
            "agent_profile": str(entry.get("agent_profile", scope_agent_profile) or ""),
            "path": str(entry.get("path", "") or ""),
        }
        break

    if raw_config is None:
        raw_config = plugins.get_default_plugin_config("_model_config") or {}

    writeback_to = dict(loaded_from)
    if writeback_to["scope"] == "default":
        writeback_to = {
            "scope": "project_agent" if project_name and agent_profile else ("project" if project_name else ("agent" if agent_profile else "global")),
            "project_name": project_name,
            "agent_profile": agent_profile if not project_name or agent_profile else "",
            "path": plugins.determine_plugin_asset_path(
                "_model_config",
                project_name,
                agent_profile if not project_name or agent_profile else "",
                plugins.CONFIG_FILE_NAME,
            ),
        }

    return {
        "config": deepcopy(raw_config),
        "loaded_from": loaded_from,
        "writeback_to": writeback_to,
    }


def get_model_config(
    agent: "Agent | None" = None,
    project_name: str | None = None,
    agent_profile: str | None = None,
) -> dict[str, Any]:
    return resolve_model_config_scope(
        agent=agent,
        project_name=project_name,
        agent_profile=agent_profile,
    )["config"]


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


def matching_live_scope_contexts(project_name: str | None = None) -> list["AgentContext"]:
    from agent import AgentContext

    project_name = _normalize_project_name(project_name)
    if project_name:
        return [
            context
            for context in AgentContext.all()
            if str(context.get_data("project") or "") == project_name
        ]
    return list(AgentContext.all())


def sync_live_scope_contexts(project_name: str | None = None) -> None:
    from usr.plugins.swiss_cheese.helpers import context_window, state as state_helper

    for context in matching_live_scope_contexts(project_name):
        agent = context.get_agent()
        plugin_config = get_plugin_config(agent)
        state_helper.ensure_state(context, plugin_config=plugin_config)
        ctx_status = context_window.compute_context_window_status(agent, plugin_config=plugin_config)
        recovery_budget = context.get_data("recovery_budget") or {
            "max_cycles": 0,
            "used_cycles": 0,
            "remaining_cycles": 0,
        }
        cross_chat_scope = plugin_config.get("cross_chat_scope", {})
        context_window.mirror_context_window_status(context, ctx_status, recovery_budget, cross_chat_scope)
        state_helper.sync_output_data(context, plugin_config=plugin_config, dirty=True)
