from __future__ import annotations

import copy
import json
import shutil
import sys
import threading
import types
import uuid
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

TEST_FILE = Path(__file__).resolve()
PLUGIN_ROOT = TEST_FILE.parents[1]
PROJECT_ROOT = next(
    (
        candidate
        for candidate in (
            TEST_FILE.parents[2] / "agent-zero",
            TEST_FILE.parents[3] / "agent-zero",
            TEST_FILE.parents[4],
        )
        if (candidate / "agent.py").exists()
    ),
    TEST_FILE.parents[4],
)
try:
    sys.path.remove(str(PLUGIN_ROOT))
except ValueError:
    pass
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

usr_pkg = sys.modules.setdefault("usr", types.ModuleType("usr"))
usr_pkg.__path__ = [str(PROJECT_ROOT / "usr")]
plugins_pkg = sys.modules.setdefault("usr.plugins", types.ModuleType("usr.plugins"))
plugins_pkg.__path__ = [str(PROJECT_ROOT / "usr" / "plugins")]
swiss_pkg = sys.modules.setdefault("usr.plugins.swiss_cheese", types.ModuleType("usr.plugins.swiss_cheese"))
swiss_pkg.__path__ = [str(PLUGIN_ROOT)]

helpers_module = types.ModuleType("helpers")
helpers_module.__path__ = [str(PROJECT_ROOT / "helpers")]
sys.modules["helpers"] = helpers_module

from agent import AgentContext
from api.load_webui_extensions import LoadWebuiExtensions
from helpers import files
from helpers import message_queue as mq
from helpers import plugins as plugin_helpers
from helpers import task_scheduler
from helpers.api import Response
from initialize import initialize_agent
from usr.plugins.swiss_cheese.api.swiss_cheese import SwissCheese as SwissCheeseApi
from usr.plugins.swiss_cheese.helpers import audit, config as swiss_config
from usr.plugins.swiss_cheese.helpers import context_window, discovery, project_state, state as state_helper
from usr.plugins.swiss_cheese.helpers.config import DEFAULT_CONFIG as SWISS_DEFAULT_CONFIG
from usr.plugins.swiss_cheese.helpers.constants import CHAT_STATE_KEY, CTX_CONFIRMATION_KEY, NEAR_MISSES_KEY, PROJECT_STATE_FILENAME, TRANSIENT_AUTONOMY_ORIGIN_KEY, TRANSIENT_RESPONSE_KEY
from usr.plugins.swiss_cheese.tools.swiss_cheese import SwissCheese as SwissCheeseTool


MODEL_CONFIG_DEFAULT = {
    "chat_model": {
        "provider": "",
        "name": "",
        "ctx_length": 0,
    },
    "utility_model": {
        "provider": "",
        "name": "",
        "ctx_length": 0,
    },
}


@pytest.fixture(scope="session", autouse=True)
def _mount_plugin_under_host() -> None:
    host_plugin_root = Path(files.get_abs_path("usr/plugins", "swiss_cheese"))
    created = False

    if not host_plugin_root.exists():
        host_plugin_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            PLUGIN_ROOT,
            host_plugin_root,
            ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", ".specs", "tests"),
        )
        created = True

    plugin_helpers.clear_plugin_cache()

    try:
        yield
    finally:
        plugin_helpers.clear_plugin_cache()
        if created:
            shutil.rmtree(host_plugin_root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _disable_state_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_helper, "mark_dirty_for_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(project_state, "mark_dirty_for_context", lambda *args, **kwargs: None)


@pytest.fixture
def project_name_factory():
    created: list[str] = []

    def _make(prefix: str = "swiss-project") -> str:
        name = f"{prefix}-{uuid.uuid4().hex[:8]}"
        created.append(name)
        return name

    yield _make

    for name in created:
        shutil.rmtree(Path(files.get_abs_path("usr/projects", name)), ignore_errors=True)


@pytest.fixture
def context_factory():
    created: list[str] = []
    persisted_paths: list[Path] = []

    def _make(
        *,
        name: str | None = None,
        project: str = "",
        profile: str = "swiss_test",
        context_id: str | None = None,
    ) -> AgentContext:
        config = initialize_agent()
        config.profile = profile
        ctx = AgentContext(
            config=config,
            id=context_id or f"swiss-{uuid.uuid4().hex[:8]}",
            name=name,
            set_current=False,
        )
        if project:
            ctx.set_data("project", project)
            ctx.set_output_data("project", {"title": project.title()})
        created.append(ctx.id)
        return ctx

    def _persisted(
        *,
        context_id: str | None = None,
        name: str,
        project: str,
        title: str | None = None,
    ) -> Path:
        chat_id = context_id or f"persisted-{uuid.uuid4().hex[:8]}"
        chat_dir = Path(files.get_abs_path("usr/chats", chat_id))
        chat_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": chat_id,
            "name": name,
            "data": {"project": project},
            "output_data": {
                "project": {"title": title or project.title()},
                "holes": [],
                "todos": [],
                "near_misses": [],
                "audit_status": {"state": "idle"},
            },
        }
        (chat_dir / "chat.json").write_text(json.dumps(payload), encoding="utf-8")
        persisted_paths.append(chat_dir)
        return chat_dir

    yield _make, _persisted

    for context_id in created:
        AgentContext.remove(context_id)
    for chat_dir in persisted_paths:
        shutil.rmtree(chat_dir, ignore_errors=True)


@pytest.fixture
def scheduler_tasks_file():
    path = Path(files.get_abs_path(task_scheduler.SCHEDULER_FOLDER, "tasks.json"))
    original = path.read_text(encoding="utf-8") if path.exists() else None

    def _write(tasks: list[Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = task_scheduler.SchedulerTaskList(tasks=tasks)
        path.write_text(payload.model_dump_json(), encoding="utf-8")
        return path

    yield _write

    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(original, encoding="utf-8")


@pytest.fixture
def in_memory_plugin_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store: dict[tuple[str, str, str], dict[str, Any]] = {}
    defaults = {
        "swiss_cheese": copy.deepcopy(SWISS_DEFAULT_CONFIG),
        "_model_config": copy.deepcopy(MODEL_CONFIG_DEFAULT),
    }

    def _resolve(plugin_name: str, project_name: str, agent_profile: str) -> dict[str, Any]:
        candidates = []
        if project_name and agent_profile:
            candidates.append((plugin_name, project_name, agent_profile))
        if project_name:
            candidates.append((plugin_name, project_name, ""))
        if agent_profile:
            candidates.append((plugin_name, "", agent_profile))
        candidates.append((plugin_name, "", ""))
        for key in candidates:
            if key in store:
                return copy.deepcopy(store[key])
        return copy.deepcopy(defaults.get(plugin_name, {}))

    def _get_plugin_config(
        plugin_name: str,
        agent=None,
        project_name: str | None = None,
        agent_profile: str | None = None,
    ):
        if project_name is None and agent is not None:
            project_name = agent.context.get_data("project") or ""
        if agent_profile is None and agent is not None:
            agent_profile = agent.config.profile or ""
        return _resolve(plugin_name, project_name or "", agent_profile or "")

    def _save_plugin_config(
        plugin_name: str,
        project_name: str,
        agent_profile: str,
        settings: dict[str, Any],
    ) -> None:
        store[(plugin_name, project_name or "", agent_profile or "")] = copy.deepcopy(settings)

    def _get_default_plugin_config(plugin_name: str):
        return copy.deepcopy(defaults.get(plugin_name, {}))

    def _exact_lookup(plugin_name: str, project_name: str, agent_profile: str):
        key = (plugin_name, project_name or "", agent_profile or "")
        if key not in store:
            return None
        return {
            "settings": copy.deepcopy(store[key]),
            "path": f"memory://{plugin_name}/{project_name or '_global'}/{agent_profile or '_default'}/config.json",
            "project_name": project_name or "",
            "agent_profile": agent_profile or "",
        }

    monkeypatch.setattr(plugin_helpers, "get_plugin_config", _get_plugin_config)
    monkeypatch.setattr(plugin_helpers, "save_plugin_config", _save_plugin_config)
    monkeypatch.setattr(plugin_helpers, "get_default_plugin_config", _get_default_plugin_config)
    monkeypatch.setattr(plugin_helpers, "_swiss_cheese_exact_config_lookup", _exact_lookup, raising=False)

    return {"store": store, "defaults": defaults}


def _parse_tool_payload(response) -> dict[str, Any]:
    return json.loads(response.message)


def _new_api() -> SwissCheeseApi:
    return SwissCheeseApi(Flask("swiss-cheese-test"), threading.RLock())


@pytest.mark.asyncio
async def test_manifest_metadata_exposes_scoped_config() -> None:
    meta = plugin_helpers.get_plugin_meta("swiss_cheese")
    assert meta is not None
    assert meta.name == "swiss_cheese"
    assert meta.title == "SwissCheese"
    assert meta.per_project_config is True
    assert meta.per_agent_config is False
    assert meta.settings_sections == ["agent"]


def test_scoped_config_precedence_and_hybrid_confirmation_resolution(
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    project_name = project_name_factory("apollo")
    store = in_memory_plugin_backend["store"]
    store[("swiss_cheese", "", "")] = {
        "preferred_working_limit": 90000,
        "max_auto_recovery_cycles": 1,
    }
    store[("swiss_cheese", project_name, "")] = {
        "preferred_working_limit": 95000,
    }
    store[("swiss_cheese", "", "pilot")] = {
        "preferred_working_limit": 98000,
    }
    store[("swiss_cheese", project_name, "pilot")] = {
        "preferred_working_limit": 99000,
        "confirmed_model_tuples": {
            "chat_model": [
                {
                    "provider": "openai",
                    "name": "gpt-chat",
                    "ctx_length": 131072,
                    "confirmed_at": "2026-03-22T00:00:00+00:00",
                }
            ],
            "utility_model": [],
        },
    }

    resolved = swiss_config.resolve_plugin_config_scope(project_name=project_name, agent_profile="pilot")
    assert resolved["config"]["preferred_working_limit"] == 95000
    assert resolved["config"]["max_auto_recovery_cycles"] == 2
    assert resolved["loaded_from"]["scope"] == "project"
    assert resolved["loaded_from"]["legacy_profile"] is False
    assert resolved["applies_to"]["scope"] == "project"
    assert resolved["applies_to"]["agent_profile"] == ""

    project_only = swiss_config.resolve_plugin_config_scope(project_name=project_name, agent_profile="")
    assert project_only["config"]["preferred_working_limit"] == 95000

    agent_only = swiss_config.resolve_plugin_config_scope(project_name="", agent_profile="pilot")
    assert agent_only["config"]["preferred_working_limit"] == 90000
    assert agent_only["loaded_from"]["scope"] == "global"
    assert agent_only["loaded_from"]["legacy_profile"] is False


def test_legacy_profile_scoped_config_absorbs_into_surviving_scope(
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    project_name = project_name_factory("legacy")
    store = in_memory_plugin_backend["store"]
    store[("swiss_cheese", "", "pilot")] = {
        "preferred_working_limit": 97000,
    }
    store[("swiss_cheese", project_name, "pilot")] = {
        "preferred_working_limit": 99000,
        "confirmed_model_tuples": {
            "chat_model": [
                {
                    "provider": "openai",
                    "name": "gpt-chat",
                    "ctx_length": 131072,
                    "confirmed_at": "2026-03-22T00:00:00+00:00",
                }
            ],
            "utility_model": [],
        },
    }

    project_scope = swiss_config.resolve_plugin_config_scope(project_name=project_name, agent_profile="pilot")
    assert project_scope["config"]["preferred_working_limit"] == 99000
    assert project_scope["loaded_from"]["scope"] == "project"
    assert project_scope["loaded_from"]["legacy_profile"] is True
    assert project_scope["legacy_absorbed"] is True
    assert project_scope["applies_to"]["scope"] == "project"
    assert project_scope["applies_to"]["agent_profile"] == ""
    assert project_scope["config"]["confirmed_model_tuples"]["chat_model"][0]["ctx_length"] == 131072

    saved_project = swiss_config.save_plugin_config(project_name, "pilot", project_scope["config"])
    assert saved_project["preferred_working_limit"] == 99000
    assert ("swiss_cheese", project_name, "") in store
    assert store[("swiss_cheese", project_name, "")]["preferred_working_limit"] == 99000

    global_scope = swiss_config.resolve_plugin_config_scope(project_name="", agent_profile="pilot")
    assert global_scope["config"]["preferred_working_limit"] == 97000
    assert global_scope["loaded_from"]["scope"] == "global"
    assert global_scope["loaded_from"]["legacy_profile"] is True
    assert global_scope["legacy_absorbed"] is True
    assert global_scope["applies_to"]["scope"] == "global"

    swiss_config.save_plugin_config("", "pilot", global_scope["config"])
    assert ("swiss_cheese", "", "") in store
    assert store[("swiss_cheese", "", "")]["preferred_working_limit"] == 97000


@pytest.mark.asyncio
async def test_context_window_gate_warning_advisory_and_invalidation(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("apollo")
    store = in_memory_plugin_backend["store"]
    store[("_model_config", project_name, "pilot")] = {
        "chat_model": {
            "provider": "openai",
            "name": "gpt-chat",
            "ctx_length": 200000,
        },
        "utility_model": {
            "provider": "openai",
            "name": "gpt-util",
            "ctx_length": 32000,
        },
    }

    ctx = make_context(name="Primary", project=project_name, profile="pilot")
    agent = ctx.get_agent()
    agent.set_data(agent.DATA_NAME_CTX_WINDOW, {"tokens": 110000})
    ctx.set_data("_swiss_cheese_last_utility_input", {"tokens": 1200})

    status = context_window.compute_context_window_status(agent)
    assert status["gate_active"] is True
    assert status["utility_warning_active"] is True
    assert status["utility_confidence"] == "reduced"
    assert status["chat_model"]["advisory_active"] is True
    assert status["chat_model"]["preferred_working_limit"] == 100000
    assert status["chat_model"]["remaining_budget"] == 90000

    scoped_cfg = swiss_config.get_plugin_config(
        agent=None,
        project_name=project_name,
        agent_profile="pilot",
    )
    swiss_config.append_confirmed_tuple(
        scoped_cfg,
        "chat_model",
        {
            "provider": "openai",
            "name": "gpt-chat",
            "ctx_length": 200000,
            "confirmed_at": "2026-03-22T00:00:00+00:00",
        },
    )
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = scoped_cfg

    status = context_window.compute_context_window_status(agent)
    assert status["gate_active"] is False
    assert status["utility_warning_active"] is True

    store[("_model_config", project_name, "pilot")]["chat_model"]["name"] = "gpt-chat-next"
    invalidated = context_window.compute_context_window_status(agent)
    assert invalidated["gate_active"] is True
    assert invalidated["chat_model"]["confirmed"] is False

    api = _new_api()
    state_payload = await api.process({"action": "get_state", "context_id": ctx.id}, None)
    assert state_payload["ok"] is True
    assert state_payload["context_window"]["gate_active"] is True
    assert state_payload["context_window"]["chat_model"]["confirmed"] is False
    assert state_payload["model_snapshot"]["chat_model"]["confirmed"] is False
    assert state_payload["state"][CTX_CONFIRMATION_KEY]["gate_active"] is True
    assert state_payload["state"][CTX_CONFIRMATION_KEY]["chat_model"]["confirmed"] is False
    assert state_payload["state"][CTX_CONFIRMATION_KEY]["chat_model"]["name"] == "gpt-chat-next"


@pytest.mark.asyncio
async def test_confirm_ctx_window_clears_gate_writes_model_scope_and_resyncs_live_project_chats(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("confirm-sync")
    other_project = project_name_factory("confirm-other")
    store = in_memory_plugin_backend["store"]
    store[("_model_config", project_name, "pilot")] = {
        "chat_model": {
            "provider": "openai",
            "name": "gpt-chat",
            "ctx_length": 200000,
        },
        "utility_model": {
            "provider": "openai",
            "name": "gpt-util",
            "ctx_length": 32000,
        },
    }
    store[("_model_config", other_project, "pilot")] = copy.deepcopy(store[("_model_config", project_name, "pilot")])

    primary = make_context(name="Primary", project=project_name, profile="pilot")
    sibling = make_context(name="Sibling", project=project_name, profile="pilot")
    foreign = make_context(name="Foreign", project=other_project, profile="pilot")

    primary.get_agent().set_data(primary.get_agent().DATA_NAME_CTX_WINDOW, {"tokens": 110000})
    sibling.get_agent().set_data(sibling.get_agent().DATA_NAME_CTX_WINDOW, {"tokens": 90000})
    foreign.get_agent().set_data(foreign.get_agent().DATA_NAME_CTX_WINDOW, {"tokens": 70000})

    api = _new_api()
    response = await api.process(
        {
            "action": "confirm_ctx_window",
            "context_id": primary.id,
            "project_name": project_name,
            "slot": "chat_model",
            "provider": "openai",
            "name": "gpt-chat",
            "ctx_length": 200000,
        },
        None,
    )

    assert response["ok"] is True
    assert response["project_name"] == project_name
    assert response["model_writeback_to"]["scope"] == "project_agent"
    assert response["model_writeback_to"]["project_name"] == project_name
    assert response["model_writeback_to"]["agent_profile"] == "pilot"
    assert ("swiss_cheese", project_name, "") in store
    assert store[("_model_config", project_name, "pilot")]["chat_model"]["ctx_length"] == 200000
    assert primary.get_output_data(CTX_CONFIRMATION_KEY)["gate_active"] is False
    assert sibling.get_output_data(CTX_CONFIRMATION_KEY)["gate_active"] is False
    assert foreign.get_output_data(CTX_CONFIRMATION_KEY) is None


def test_project_state_persists_backlog_and_chat_todos_remain_local(
    context_factory,
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("backlog")
    ctx = make_context(name="Mission", project=project_name, profile="pilot")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    chat_todo = state_helper.add_or_update_todo(
        ctx,
        {
            "title": "Verify local change",
            "detail": "Check only the active chat work.",
            "severity": "medium",
            "source": "manual",
            "status": "open",
        },
        plugin_config=plugin_config,
    )
    project_todo = project_state.add_or_update_project_todo(
        ctx,
        {
            "title": "Align project checklist",
            "detail": "Shared action across same-project chats.",
            "severity": "high",
            "source": "manual",
            "status": "open",
        },
        plugin_config=plugin_config,
    )

    assert chat_todo["scope"] == "chat"
    assert project_todo is not None
    assert project_todo["scope"] == "project"
    assert project_todo["project_name"] == project_name
    assert project_todo["origin_context_id"] == ctx.id
    assert project_todo["origin_context_name"] == "Mission"
    assert [todo["title"] for todo in ctx.get_data("todos")] == ["Verify local change"]
    assert [todo["title"] for todo in project_state.list_project_todos(ctx, status="all")] == ["Align project checklist"]

    state_path = Path(files.get_abs_path("usr/projects", project_name, ".a0proj", "plugins", "swiss_cheese", PROJECT_STATE_FILENAME))
    assert state_path.exists()
    loaded_state = project_state.load_project_state(project_name)
    assert loaded_state["todos"][0]["title"] == "Align project checklist"


@pytest.mark.asyncio
async def test_api_get_state_returns_project_view_defaults_and_backlog(
    context_factory,
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("project-view")
    ctx = make_context(name="Project Chat", project=project_name, profile="pilot")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)
    project_state.add_or_update_project_todo(
        ctx,
        {
            "title": "Shared backlog item",
            "detail": "Tracked at project scope.",
            "severity": "high",
            "source": "manual",
            "status": "open",
        },
        plugin_config=plugin_config,
    )

    api = _new_api()
    response = await api.process({"action": "get_state", "context_id": ctx.id}, None)
    assert response["ok"] is True
    assert response["default_view"] == "project"
    assert set(response["available_views"]) == {"chat", "project"}
    assert response["catalog_defaults"]["project_only"] is True
    assert response["project_state"]["todos"][0]["title"] == "Shared backlog item"
    assert response["project_rollup"]["project_name"] == project_name


@pytest.mark.asyncio
async def test_no_project_chat_keeps_chat_local_todo_workflow(context_factory) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="Solo Chat")
    api = _new_api()

    add_response = await api.process(
        {
            "action": "todo_add",
            "context_id": ctx.id,
            "title": "Solo todo",
            "detail": "No project required.",
            "scope": "chat",
        },
        None,
    )
    assert add_response["ok"] is True
    assert add_response["scope"] == "chat"

    state_response = await api.process({"action": "get_state", "context_id": ctx.id}, None)
    assert state_response["ok"] is True
    assert state_response["default_view"] == "chat"
    assert state_response["project_state"] is None
    assert state_response["chat_state"]["todos"][0]["title"] == "Solo todo"

    project_list_response = await api.process(
        {
            "action": "todo_list",
            "context_id": ctx.id,
            "scope": "project",
        },
        None,
    )
    assert isinstance(project_list_response, Response)
    assert project_list_response.status_code == 400


def test_target_catalog_includes_tasks_and_persisted_task_permissions(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
    scheduler_tasks_file,
) -> None:
    make_context, persist_chat = context_factory
    project_name = project_name_factory("catalog")
    other_project = project_name_factory("other")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Beta Mission", project=project_name, profile="pilot")
    cross_project = make_context(name="Foreign Mission", project=other_project, profile="pilot")
    persist_chat(name="Gamma Mission", project=project_name)
    persist_chat(context_id="task-persisted-ctx", name="Task Context", project=project_name)

    shared_context_id = "task-shared-ctx"
    shared_one = task_scheduler.AdHocTask.create(
        name="Shared Task A",
        system_prompt="sys",
        prompt="prompt",
        token="1111111111111111111",
        context_id=shared_context_id,
        project_name=project_name,
    )
    shared_two = task_scheduler.AdHocTask.create(
        name="Shared Task B",
        system_prompt="sys",
        prompt="prompt",
        token="2222222222222222222",
        context_id=shared_context_id,
        project_name=project_name,
    )
    persisted_task = task_scheduler.ScheduledTask.create(
        name="Persisted Task",
        system_prompt="sys",
        prompt="prompt",
        schedule=task_scheduler.TaskSchedule(minute="0", hour="1", day="*", month="*", weekday="*"),
        context_id="task-persisted-ctx",
        project_name=project_name,
    )
    scheduler_tasks_file([shared_one, shared_two, persisted_task])

    plugin_config = swiss_config.get_plugin_config(source.get_agent())
    catalog = discovery.list_targets(
        source_context=source,
        scope=plugin_config.get("cross_chat_scope", {}),
        project_only=True,
        include_persisted=True,
    )
    catalog_keys = {entry["target_key"] for entry in catalog}
    assert f"chat:{source.id}" in catalog_keys
    assert f"chat:{target.id}" in catalog_keys
    assert f"chat:{cross_project.id}" not in catalog_keys
    assert f"task:{shared_one.uuid}" in catalog_keys
    assert f"task:{shared_two.uuid}" in catalog_keys

    persisted_chat_entry = next(
        entry for entry in catalog
        if entry["kind"] == "chat" and entry["persisted_only"]
    )
    assert persisted_chat_entry["permissions"]["can_read"] is True
    assert persisted_chat_entry["permissions"]["can_queue"] is False

    persisted_task_entry = next(
        entry for entry in catalog
        if entry["kind"] == "task" and entry["target_key"] == f"task:{persisted_task.uuid}"
    )
    assert persisted_task_entry["permissions"]["can_read"] is True
    assert persisted_task_entry["permissions"]["can_queue"] is True

    inspection = discovery.inspect_target(
        source_context=source,
        target_key=f"task:{shared_one.uuid}",
        scope=plugin_config.get("cross_chat_scope", {}),
    )
    assert inspection["match_type"] == "exact_target_key"
    assert inspection["target"]["target_key"] == f"task:{shared_one.uuid}"
    assert inspection["permissions"]["can_queue"] is True


@pytest.mark.asyncio
async def test_api_queue_followup_accepts_exact_target_context_id(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("handoff")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": False,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    api = _new_api()

    response = await api.process(
        {
            "action": "queue_followup",
            "context_id": source.id,
            "target_context_id": target.id,
            "reason": "handoff",
            "message": "Continue the bounded recovery sequence.",
            "auto_send": False,
        },
        None,
    )
    assert response["ok"] is True
    assert response["queued"] is True
    assert response["result"]["target_key"] == f"chat:{target.id}"
    assert response["result"]["target_context_id"] == target.id


@pytest.mark.asyncio
async def test_api_target_actions_and_legacy_aliases_match(context_factory) -> None:
    make_context, _persisted = context_factory
    source = make_context(name="Source Chat")
    target = make_context(name="Target Chat")
    api = _new_api()

    targets_new = await api.process({"action": "list_targets", "context_id": source.id}, None)
    targets_old = await api.process({"action": "list_chat_targets", "context_id": source.id}, None)
    assert targets_new["targets"] == targets_old["targets"]

    inspect_new = await api.process(
        {"action": "inspect_target", "context_id": source.id, "target_key": f"chat:{target.id}"},
        None,
    )
    inspect_old = await api.process(
        {"action": "inspect_chat", "context_id": source.id, "target_context_id": target.id},
        None,
    )
    assert inspect_new["inspection"]["target"]["target_key"] == inspect_old["inspection"]["target"]["target_key"]
    assert inspect_new["inspection"]["permissions"] == inspect_old["inspection"]["permissions"]


@pytest.mark.asyncio
async def test_api_list_targets_and_inspect_target_honor_kind_filter(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
    scheduler_tasks_file,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("kind-filter")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    task = task_scheduler.AdHocTask.create(
        name="Project Task",
        system_prompt="sys",
        prompt="prompt",
        token="4444444444444444444",
        context_id=target.id,
        project_name=project_name,
    )
    scheduler_tasks_file([task])
    api = _new_api()

    chat_targets = await api.process(
        {"action": "list_targets", "context_id": source.id, "project_only": True, "kind": "chat"},
        None,
    )
    assert chat_targets["ok"] is True
    assert chat_targets["counts"]["all"] >= 3
    assert chat_targets["counts"]["chat"] >= 2
    assert chat_targets["counts"]["task"] == 1
    assert chat_targets["targets"]
    assert all(entry["kind"] == "chat" for entry in chat_targets["targets"])

    task_targets = await api.process(
        {"action": "list_targets", "context_id": source.id, "project_only": True, "kind": "task"},
        None,
    )
    assert task_targets["ok"] is True
    assert len(task_targets["targets"]) == 1
    assert task_targets["targets"][0]["target_key"] == f"task:{task.uuid}"
    assert task_targets["targets"][0]["kind"] == "task"

    hidden_task = await api.process(
        {
            "action": "inspect_target",
            "context_id": source.id,
            "target_key": f"task:{task.uuid}",
            "project_only": True,
            "kind": "chat",
        },
        None,
    )
    assert hidden_task["ok"] is True
    assert hidden_task["inspection"]["target"] is None

    visible_task = await api.process(
        {
            "action": "inspect_target",
            "context_id": source.id,
            "target_key": f"task:{task.uuid}",
            "project_only": True,
            "kind": "task",
        },
        None,
    )
    assert visible_task["inspection"]["target"]["target_key"] == f"task:{task.uuid}"
    assert visible_task["inspection"]["target"]["kind"] == "task"

def test_followup_fingerprint_uses_target_key_for_shared_task_contexts(context_factory) -> None:
    make_context, _persisted = context_factory
    source = make_context(name="Source Chat")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)

    first_queued, first = state_helper.queue_followup(
        source,
        target_key="task:task-one",
        target_kind="task",
        target_context_id="shared-task-context",
        target_task_uuid="task-one",
        target_name="Task One",
        reason="handoff",
        message="Continue the bounded recovery sequence.",
        auto_send=False,
        source="test",
        plugin_config=plugin_config,
    )
    second_queued, second = state_helper.queue_followup(
        source,
        target_key="task:task-two",
        target_kind="task",
        target_context_id="shared-task-context",
        target_task_uuid="task-two",
        target_name="Task Two",
        reason="handoff",
        message="Continue the bounded recovery sequence.",
        auto_send=False,
        source="test",
        plugin_config=plugin_config,
    )

    assert first_queued is True
    assert second_queued is True
    assert first["fingerprint"] != second["fingerprint"]


def test_duplicate_followup_fingerprint_blocks_repeated_work_across_pending_and_last_autonomous(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("repeat-work")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    plugin_config = swiss_config.get_plugin_config(source.get_agent())
    source.set_data(CTX_CONFIRMATION_KEY, {"gate_active": False})

    queued, item = state_helper.queue_followup(
        source,
        target_key=f"chat:{target.id}",
        target_kind="chat",
        target_context_id=target.id,
        target_name=target.name or target.id,
        reason="repeat_request",
        message="Please run `unzip -t /a0/usr/projects/project_1/usr.zip` and report the exit status.",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    duplicate_pending, pending_info = state_helper.queue_followup(
        source,
        target_key=f"chat:{target.id}",
        target_kind="chat",
        target_context_id=target.id,
        target_name=target.name or target.id,
        reason="repeat_request",
        message=" please   RUN `UNZIP -t /a0/usr/projects/project_1/usr.zip` and report the exit status. ",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert duplicate_pending is False
    assert pending_info["reason"] == "duplicate_pending"
    assert pending_info["fingerprint"] == item["fingerprint"]

    sent = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=False)
    assert sent is not None
    assert sent["delivery_state"] == "sent"

    duplicate_last, last_info = state_helper.queue_followup(
        source,
        target_key=f"chat:{target.id}",
        target_kind="chat",
        target_context_id=target.id,
        target_name=target.name or target.id,
        reason="repeat_request",
        message=" please   RUN `UNZIP -t /a0/usr/projects/project_1/usr.zip` and report the exit status. ",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert duplicate_last is False
    assert last_info["reason"] == "duplicate_last_autonomous"


def test_semantic_archive_validation_followups_collapse_even_with_paraphrase(
    context_factory,
) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="Plugin Zipping")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)

    queued, first = state_helper.queue_followup(
        ctx,
        target_key=f"chat:{ctx.id}",
        target_kind="chat",
        target_context_id=ctx.id,
        target_name=ctx.name or ctx.id,
        reason="archive_validation",
        message="Run `unzip -t /a0/usr/projects/project_1/usr.zip`, capture the exit code, and report the exact archive path and size.",
        auto_send=False,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    duplicate, info = state_helper.queue_followup(
        ctx,
        target_key=f"chat:{ctx.id}",
        target_kind="chat",
        target_context_id=ctx.id,
        target_name=ctx.name or ctx.id,
        reason="zip_success_evidence",
        message="Confirm the archive exists, is readable, and spot-check `plugin.yaml` via `zipinfo` before reporting success.",
        auto_send=False,
        source="test",
        plugin_config=plugin_config,
    )
    assert duplicate is False
    assert info["reason"] == "duplicate_pending"
    assert info["fingerprint"] == first["fingerprint"]


def test_bump_user_turn_resets_autonomy_origin_and_recovery_budget(
    context_factory,
) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="User Turn Chat")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    chat_state = ctx.get_data(CHAT_STATE_KEY)
    chat_state["active_user_turn"] = 4
    chat_state["recovery_cycles_used"] = 2
    chat_state["last_followup_fingerprint"] = "abc123"
    ctx.set_data(TRANSIENT_AUTONOMY_ORIGIN_KEY, {"fingerprint": "abc123", "reason": "repeat_request"})

    bumped = state_helper.bump_user_turn(ctx, plugin_config=plugin_config)

    assert bumped["active_user_turn"] == 5
    assert bumped["recovery_cycles_used"] == 0
    assert ctx.get_data(TRANSIENT_AUTONOMY_ORIGIN_KEY) is None
    assert ctx.get_output_data(CHAT_STATE_KEY)["active_user_turn"] == 5


def test_manual_bridge_of_non_auto_send_item_queues_native_message_without_spending_budget(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("bridge")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    plugin_config = swiss_config.get_plugin_config(source.get_agent())

    queued, item = state_helper.queue_followup(
        source,
        target_key=f"chat:{target.id}",
        target_kind="chat",
        target_context_id=target.id,
        target_name=target.name or target.id,
        reason="manual_bridge",
        message="Bridge only.",
        auto_send=False,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    bridged = state_helper.bridge_next_followup(
        source,
        plugin_config=plugin_config,
        manual=True,
        fingerprint=item["fingerprint"],
        send_now=False,
    )

    assert bridged is not None
    assert bridged["delivery_state"] == "queued_in_target_queue"
    assert len(source.get_data(CHAT_STATE_KEY)["followup_queue"]) == 0
    assert source.get_data(CHAT_STATE_KEY)["followup_history"][0]["delivery_state"] == "queued_in_target_queue"
    assert len(mq.get_queue(target)) == 1
    assert source.get_data("recovery_budget")["used_cycles"] == 0


def test_auto_send_bridges_then_sends_and_spends_one_cycle(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("autosend")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    plugin_config = swiss_config.get_plugin_config(source.get_agent())
    sent_messages: list[tuple[str, str]] = []

    def _fake_communicate(self, msg, broadcast_level: int = 1):
        sent_messages.append((self.id, msg.message))
        return None

    monkeypatch.setattr(AgentContext, "communicate", _fake_communicate)
    source.set_data(CTX_CONFIRMATION_KEY, {"gate_active": False})

    queued, item = state_helper.queue_followup(
        source,
        target_key=f"chat:{target.id}",
        target_kind="chat",
        target_context_id=target.id,
        target_name=target.name or target.id,
        reason="auto_send",
        message="Send immediately.",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    sent = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=False)

    assert sent is not None
    assert sent["fingerprint"] == item["fingerprint"]
    assert sent["delivery_state"] == "sent"
    assert sent_messages == [(target.id, "Send immediately.")]
    assert len(mq.get_queue(target)) == 0
    assert source.get_data("recovery_budget")["used_cycles"] == 1
    assert source.get_data(CHAT_STATE_KEY)["followup_history"][0]["delivery_state"] == "sent"


def test_task_target_auto_send_loads_persisted_task_context_and_sends(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
    scheduler_tasks_file,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_context, persist_chat = context_factory
    project_name = project_name_factory("tasksend")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    persist_chat(context_id="task-context-id", name="Persisted Task Context", project=project_name)
    task = task_scheduler.AdHocTask.create(
        name="Persisted Task",
        system_prompt="sys",
        prompt="prompt",
        token="3333333333333333333",
        context_id="task-context-id",
        project_name=project_name,
    )
    scheduler_tasks_file([task])
    plugin_config = swiss_config.get_plugin_config(source.get_agent())
    sent_messages: list[tuple[str, str]] = []

    def _fake_communicate(self, msg, broadcast_level: int = 1):
        sent_messages.append((self.id, msg.message))
        return None

    monkeypatch.setattr(AgentContext, "communicate", _fake_communicate)
    source.set_data(CTX_CONFIRMATION_KEY, {"gate_active": False})

    queued, item = state_helper.queue_followup(
        source,
        target_key=f"task:{task.uuid}",
        target_kind="task",
        target_context_id="task-context-id",
        target_task_uuid=task.uuid,
        target_name=task.name,
        reason="task_followup",
        message="Continue inside the task context queue.",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    sent = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=False)

    assert sent is not None
    assert sent["fingerprint"] == item["fingerprint"]
    assert sent["target_key"] == f"task:{task.uuid}"
    assert sent["delivery_state"] == "sent"
    assert sent_messages == [("task-context-id", "Continue inside the task context queue.")]
    assert AgentContext.get("task-context-id") is not None


def test_blocked_followup_records_gate_reason_and_moves_to_history(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("gate-block")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    plugin_config = swiss_config.get_plugin_config(source.get_agent())
    source.set_data(CTX_CONFIRMATION_KEY, {"gate_active": True})

    queued, item = state_helper.queue_followup(
        source,
        target_key=f"chat:{target.id}",
        target_kind="chat",
        target_context_id=target.id,
        target_name=target.name or target.id,
        reason="auto_send_gate",
        message="Wait for explicit confirmation.",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    blocked = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=False)

    assert blocked is not None
    assert blocked["delivery_state"] == "blocked"
    assert blocked["reason"] == "chat_ctx_confirmation_gate"
    assert blocked["item"]["blocked_reason"] == "chat_ctx_confirmation_gate"
    assert source.get_data(CHAT_STATE_KEY)["followup_queue"] == []
    assert source.get_data(CHAT_STATE_KEY)["followup_history"][0]["fingerprint"] == item["fingerprint"]
    assert source.get_data(CHAT_STATE_KEY)["followup_history"][0]["blocked_reason"] == "chat_ctx_confirmation_gate"


@pytest.mark.asyncio
async def test_api_queue_followup_records_not_queueable_and_not_found_reasons(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("queue-blocked")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "")] = {
        "cross_chat_scope": {
            "same_project_live_write": False,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    api = _new_api()

    blocked_scope = await api.process(
        {
            "action": "queue_followup",
            "context_id": source.id,
            "target_context_id": target.id,
            "reason": "handoff",
            "message": "This should stay blocked by scope.",
            "auto_send": True,
        },
        None,
    )
    assert blocked_scope["ok"] is False
    assert blocked_scope["queued"] is False
    assert blocked_scope["result"]["delivery_state"] == "blocked"
    assert blocked_scope["result"]["blocked_reason"] == "target_not_queueable_in_scope"

    blocked_missing = await api.process(
        {
            "action": "queue_followup",
            "context_id": source.id,
            "target_context_id": "missing-context",
            "reason": "handoff",
            "message": "This should fail because the target is gone.",
            "auto_send": True,
        },
        None,
    )
    assert blocked_missing["ok"] is False
    assert blocked_missing["queued"] is False
    assert blocked_missing["result"]["blocked_reason"] == "target_not_found"


def test_bridge_followup_records_target_context_unavailable_reason(context_factory) -> None:
    make_context, _persisted = context_factory
    source = make_context(name="Source Chat")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    source.set_data(CTX_CONFIRMATION_KEY, {"gate_active": False})

    queued, item = state_helper.queue_followup(
        source,
        target_key="chat:missing-context",
        target_kind="chat",
        target_context_id="missing-context",
        target_name="Missing Context",
        reason="handoff",
        message="Try to continue later.",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    blocked = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=False)

    assert blocked is not None
    assert blocked["delivery_state"] == "blocked"
    assert blocked["reason"] == "target_context_unavailable"
    assert blocked["item"]["blocked_reason"] == "target_context_unavailable"
    assert source.get_data(CHAT_STATE_KEY)["followup_history"][0]["fingerprint"] == item["fingerprint"]


@pytest.mark.asyncio
async def test_api_retry_followup_requeues_blocked_entry(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("retry")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    plugin_config = swiss_config.get_plugin_config(source.get_agent())
    source.set_data(CTX_CONFIRMATION_KEY, {"gate_active": True})
    api = _new_api()

    queued, item = state_helper.queue_followup(
        source,
        target_key=f"chat:{target.id}",
        target_kind="chat",
        target_context_id=target.id,
        target_name=target.name or target.id,
        reason="retry_me",
        message="Retry after confirmation.",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    blocked = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=False)
    assert blocked is not None
    assert blocked["delivery_state"] == "blocked"

    retried = await api.process(
        {
            "action": "retry_followup",
            "context_id": source.id,
            "fingerprint": item["fingerprint"],
        },
        None,
    )
    assert retried["ok"] is True
    assert retried["queued"] is True
    assert retried["result"]["delivery_state"] == "pending"
    assert source.get_data(CHAT_STATE_KEY)["followup_queue"]


def test_build_project_rollup_counts_same_project_live_and_allowed_persisted(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, persist_chat = context_factory
    project_name = project_name_factory("rollup")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    peer = make_context(name="Peer Chat", project=project_name, profile="pilot")
    persist_chat(name="Archived Chat", project=project_name)
    plugin_config = swiss_config.get_plugin_config(source.get_agent())

    state_helper.ensure_state(source, plugin_config=plugin_config)
    state_helper.ensure_state(peer, plugin_config=plugin_config)
    state_helper.add_or_update_todo(
        peer,
        {
            "title": "Peer todo",
            "detail": "Counts in rollup.",
            "severity": "medium",
            "source": "manual",
            "status": "open",
        },
        plugin_config=plugin_config,
    )
    project_state.add_or_update_project_todo(
        source,
        {
            "title": "Project backlog",
            "detail": "Shared action.",
            "severity": "high",
            "source": "manual",
            "status": "open",
        },
        plugin_config=plugin_config,
    )

    rollup = discovery.build_project_rollup(
        source_context=source,
        scope=plugin_config.get("cross_chat_scope", {}),
    )
    assert rollup is not None
    assert rollup["chat_count"] == 3
    assert rollup["live_chat_count"] == 2
    assert rollup["persisted_chat_count"] == 1
    assert rollup["totals"]["chat_todos"] == 1
    assert rollup["totals"]["project_todos"] == 1


def test_near_duplicate_plugin_zipping_todos_merge_by_hole_id_in_chat_and_project_scopes(
    context_factory,
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("zip-dedupe")
    ctx = make_context(name="Plugin Zipping", project=project_name, profile="pilot")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    first_chat = state_helper.add_or_update_todo(
        ctx,
        {
            "title": "Verify destination directory exists and is writable before zipping",
            "detail": "Check mkdir -p + test -w before archive creation.",
            "severity": "medium",
            "source": "audit",
            "status": "open",
            "hole_id": "zip-preflight",
        },
        plugin_config=plugin_config,
    )
    second_chat = state_helper.add_or_update_todo(
        ctx,
        {
            "title": "Preflight: verify source and destination readiness for zip",
            "detail": "Confirm /a0/usr/projects/project_1 is writable before creating the archive.",
            "severity": "high",
            "source": "audit",
            "status": "open",
            "hole_id": "zip-preflight",
        },
        plugin_config=plugin_config,
    )

    assert len(ctx.get_data("todos")) == 1
    assert first_chat["id"] == second_chat["id"]
    assert second_chat["severity"] == "high"
    assert second_chat["title"] == "Verify destination directory exists and is writable before zipping"

    first_project = project_state.add_or_update_project_todo(
        ctx,
        {
            "title": "Before zipping, verify destination writability",
            "detail": "Confirm the destination can be created and written to.",
            "severity": "medium",
            "source": "audit",
            "status": "open",
            "hole_id": "zip-preflight",
        },
        plugin_config=plugin_config,
    )
    second_project = project_state.add_or_update_project_todo(
        ctx,
        {
            "title": "Preflight destination writability before archiving",
            "detail": "Use mkdir -p and test -w before any archive run.",
            "severity": "high",
            "source": "audit",
            "status": "open",
            "hole_id": "zip-preflight",
        },
        plugin_config=plugin_config,
    )

    assert len(project_state.list_project_todos(ctx, status="all")) == 1
    assert first_project["id"] == second_project["id"]
    assert second_project["severity"] == "high"


def test_semantic_archive_validation_todos_merge_without_shared_hole_id(
    context_factory,
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("zip-semantic-dedupe")
    ctx = make_context(name="Plugin Zipping", project=project_name, profile="pilot")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    first = state_helper.add_or_update_todo(
        ctx,
        {
            "title": "Run lightweight integrity test on the archive",
            "detail": "Use `unzip -t` and report the exact archive path plus file size.",
            "severity": "medium",
            "source": "audit",
            "status": "open",
        },
        plugin_config=plugin_config,
    )
    second = state_helper.add_or_update_todo(
        ctx,
        {
            "title": "Validate key archive entries before calling the zip done",
            "detail": "Use `zipinfo` to confirm `plugin.yaml` or `state.json` exists and treat that as success evidence.",
            "severity": "high",
            "source": "audit",
            "status": "open",
        },
        plugin_config=plugin_config,
    )

    assert len(ctx.get_data("todos")) == 1
    assert first["id"] == second["id"]
    assert second["severity"] == "high"


@pytest.mark.asyncio
async def test_optional_gate_drift_and_semantic_followup_diagnostics_are_exposed_when_present(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("optional-diagnostics")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    ctx = make_context(name="Helios Hardware", project=project_name, profile="pilot")
    api = _new_api()
    payload = await api.process({"action": "get_state", "context_id": ctx.id}, None)

    optional_payloads = {
        "gate_diagnostics": payload.get("gate_diagnostics"),
        "context_gate_diagnostics": payload.get("context_window", {}).get("gate_diagnostics"),
        "confirmation_diagnostics": payload.get("state", {}).get(CTX_CONFIRMATION_KEY, {}).get("diagnostics"),
        "drift_diagnostics": payload.get("drift_diagnostics"),
        "outlier_diagnostics": payload.get("outlier_diagnostics"),
        "followup_diagnostics": payload.get("followup_diagnostics"),
    }
    present = {key: value for key, value in optional_payloads.items() if value is not None}
    if not present:
        pytest.skip("No optional diagnostics payload is exposed in this backend revision")

    gate_diagnostics = present.get("gate_diagnostics") or present.get("context_gate_diagnostics")
    if gate_diagnostics is not None:
        assert isinstance(gate_diagnostics, dict)
        assert gate_diagnostics

    confirmation_diagnostics = present.get("confirmation_diagnostics")
    if confirmation_diagnostics is not None:
        assert isinstance(confirmation_diagnostics, dict)
        assert confirmation_diagnostics

    drift_diagnostics = present.get("drift_diagnostics") or present.get("outlier_diagnostics")
    if drift_diagnostics is not None:
        assert isinstance(drift_diagnostics, dict)
        assert drift_diagnostics

    followup_diagnostics = present.get("followup_diagnostics")
    if followup_diagnostics is not None:
        assert isinstance(followup_diagnostics, dict)
        assert followup_diagnostics


def test_apply_audit_result_adds_project_backlog_and_deduped_current_chat_nudge(
    context_factory,
    project_name_factory,
) -> None:
    make_context, _persisted = context_factory
    project_name = project_name_factory("audit")
    ctx = make_context(name="Audit Chat", project=project_name, profile="pilot")
    agent = ctx.get_agent()
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    payload = {
        "summary": "Audit completed.",
        "holes": [
            {
                "kind": "active_failure",
                "pattern": "skipped_verification",
                "barrier": "Navigate",
                "severity": "high",
                "confidence": 0.92,
                "title": "Verification skipped",
                "trajectory": "Claims could be wrong without a verification pass.",
                "todo": "Run a concrete verification step.",
            }
        ],
        "todos": [
            {
                "title": "Align shared checklist",
                "detail": "Project-level action from the audit.",
                "severity": "high",
                "source": "audit",
                "status": "open",
                "scope": "project",
            }
        ],
        "near_misses": [],
        "followups": [],
    }

    audit._apply_audit_result(
        agent,
        payload,
        ctx_status={"scope": {}},
        used_fallback=False,
        plugin_config=plugin_config,
    )

    queue = (ctx.get_data(CHAT_STATE_KEY) or {}).get("followup_queue", [])
    assert len(queue) == 1
    assert queue[0]["target_key"] == f"chat:{ctx.id}"
    assert queue[0]["target_context_id"] == ctx.id
    assert project_state.list_project_todos(ctx, status="all")[0]["title"] == "Align shared checklist"
    assert (ctx.get_data(CHAT_STATE_KEY) or {}).get("notification_history")
    assert project_state.load_project_state(project_name).get("notification_history")

    audit._apply_audit_result(
        agent,
        payload,
        ctx_status={"scope": {}},
        used_fallback=False,
        plugin_config=plugin_config,
    )
    queue_again = (ctx.get_data(CHAT_STATE_KEY) or {}).get("followup_queue", [])
    assert len(queue_again) == 1


def test_legacy_barrier_names_map_to_new_labels(context_factory) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="Legacy Barrier Chat")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    ctx.set_data(
        CHAT_STATE_KEY,
        {
            "holes": [
                {
                    "id": "legacy-hole",
                    "kind": "active_failure",
                    "barrier": "Navigate",
                    "severity": "high",
                    "title": "Legacy direction gap",
                }
            ],
            "near_misses": [
                {
                    "id": "legacy-near-miss",
                    "barrier": "Communicate",
                    "severity": "medium",
                    "title": "Legacy coordination catch",
                }
            ],
        },
    )

    bundle = state_helper.ensure_state(ctx, plugin_config=plugin_config)

    assert bundle[CHAT_STATE_KEY]["holes"][0]["barrier"] == "Direction"
    assert bundle[CHAT_STATE_KEY]["near_misses"][0]["barrier"] == "Coordination"

    state_helper.record_near_miss(
        ctx,
        {
            "title": "Another legacy item",
            "detail": "Mapped on write.",
            "barrier": "Prepare",
            "severity": "low",
        },
        plugin_config=plugin_config,
    )
    assert any(item["barrier"] == "Readiness" for item in ctx.get_data(NEAR_MISSES_KEY))


@pytest.mark.asyncio
async def test_tool_chat_catalog_and_scoped_project_todo_lifecycle(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
    project_name_factory,
) -> None:
    make_context, persist_chat = context_factory
    project_name = project_name_factory("tooling")
    in_memory_plugin_backend["store"][("swiss_cheese", project_name, "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }
    source = make_context(name="Source Chat", project=project_name, profile="pilot")
    target = make_context(name="Target Chat", project=project_name, profile="pilot")
    persist_chat(name="Archive", project=project_name)
    agent = source.get_agent()

    catalog_response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="chat_catalog",
        args={},
        message="",
        loop_data=None,
    ).execute(project_only=True, include_persisted=True)
    catalog_payload = _parse_tool_payload(catalog_response)
    target_ids = {item["id"] for item in catalog_payload["data"]["targets"]}
    assert f"chat:{source.id}" in target_ids
    assert f"chat:{target.id}" in target_ids

    add_response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_add",
        args={},
        message="",
        loop_data=None,
    ).execute(
        title="Project todo",
        detail="Shared backlog item.",
        severity="high",
        scope="project",
    )
    added = _parse_tool_payload(add_response)
    todo_id = added["data"]["todo"]["id"]

    list_response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_list",
        args={},
        message="",
        loop_data=None,
    ).execute(status="open", scope="project")
    listed = _parse_tool_payload(list_response)
    assert any(todo["id"] == todo_id for todo in listed["data"]["todos"])

    resolve_response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_resolve",
        args={},
        message="",
        loop_data=None,
    ).execute(todo_id=todo_id, scope="project")
    resolved = _parse_tool_payload(resolve_response)
    assert resolved["data"]["ok"] is True
    assert resolved["data"]["todo"]["status"] == "completed"


@pytest.mark.asyncio
async def test_webui_sidebar_extension_surface_includes_swiss_cheese_entry() -> None:
    payload = await LoadWebuiExtensions(Flask("swiss-cheese-webui"), threading.RLock()).process(
        {
            "extension_point": "sidebar-quick-actions-main-start",
            "filters": ["*.html"],
        },
        None,
    )
    extensions = payload["extensions"]
    expected_suffix = "usr/plugins/swiss_cheese/extensions/webui/sidebar-quick-actions-main-start/swiss-cheese-entry.html"
    assert any(str(entry).replace("\\", "/").endswith(expected_suffix) for entry in extensions)

    extension_file = PROJECT_ROOT / expected_suffix
    html = extension_file.read_text(encoding="utf-8")
    assert "openModal('/plugins/swiss_cheese/webui/main.html')" in html
