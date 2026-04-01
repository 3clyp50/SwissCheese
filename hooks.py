from __future__ import annotations

import json
from typing import Any

from helpers import files, plugins

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers.constants import PLUGIN_NAME


def get_plugin_config(
    default: dict[str, Any] | None = None,
    *,
    agent=None,
    project_name: str | None = None,
    agent_profile: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    return swiss_config.resolve_plugin_config_scope(
        agent=agent,
        project_name=project_name,
        agent_profile=agent_profile,
    )["config"]


def save_plugin_config(
    default: dict[str, Any] | None = None,
    *,
    project_name: str = "",
    agent_profile: str = "",
    settings: dict[str, Any] | None = None,
    **kwargs,
) -> None:
    normalized = swiss_config.normalize_plugin_config(settings or {})
    save_project_name, save_agent_profile = swiss_config.get_plugin_save_scope(project_name)
    path = plugins.determine_plugin_asset_path(
        PLUGIN_NAME,
        save_project_name,
        save_agent_profile,
        plugins.CONFIG_FILE_NAME,
    )
    files.write_file(path, json.dumps(normalized))
    swiss_config.sync_live_scope_contexts(save_project_name)
    return None
