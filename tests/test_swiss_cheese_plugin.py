from __future__ import annotations

import copy
import json
import shutil
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import AgentContext
from api.load_webui_extensions import LoadWebuiExtensions
from helpers import files
from helpers import plugins as plugin_helpers
from initialize import initialize_agent
from usr.plugins.swiss_cheese.api.swiss_cheese import SwissCheese as SwissCheeseApi
from usr.plugins.swiss_cheese.helpers import audit, config as swiss_config
from usr.plugins.swiss_cheese.helpers import context_window, discovery, state as state_helper
from usr.plugins.swiss_cheese.helpers.config import DEFAULT_CONFIG as SWISS_DEFAULT_CONFIG
from usr.plugins.swiss_cheese.helpers.constants import CHAT_STATE_KEY, TRANSIENT_RESPONSE_KEY
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


@pytest.fixture(autouse=True)
def _disable_state_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_helper, "mark_dirty_for_context", lambda *args, **kwargs: None)


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

    monkeypatch.setattr(plugin_helpers, "get_plugin_config", _get_plugin_config)
    monkeypatch.setattr(plugin_helpers, "save_plugin_config", _save_plugin_config)
    monkeypatch.setattr(plugin_helpers, "get_default_plugin_config", _get_default_plugin_config)

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
    assert meta.per_agent_config is True
    assert meta.settings_sections == ["agent"]


def test_scoped_config_precedence_and_hybrid_confirmation_resolution(
    in_memory_plugin_backend: dict[str, Any],
) -> None:
    store = in_memory_plugin_backend["store"]
    store[("swiss_cheese", "", "")] = {
        "preferred_working_limit": 90000,
        "max_auto_recovery_cycles": 1,
    }
    store[("swiss_cheese", "apollo", "")] = {
        "preferred_working_limit": 95000,
    }
    store[("swiss_cheese", "", "pilot")] = {
        "preferred_working_limit": 98000,
    }
    store[("swiss_cheese", "apollo", "pilot")] = {
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

    resolved = swiss_config.get_plugin_config(
        agent=None,
        project_name="apollo",
        agent_profile="pilot",
    )
    assert resolved["preferred_working_limit"] == 99000
    assert resolved["max_auto_recovery_cycles"] == 2
    assert resolved["confirmed_model_tuples"]["chat_model"][0]["ctx_length"] == 131072

    project_only = swiss_config.get_plugin_config(
        agent=None,
        project_name="apollo",
        agent_profile="",
    )
    assert project_only["preferred_working_limit"] == 95000

    agent_only = swiss_config.get_plugin_config(
        agent=None,
        project_name="",
        agent_profile="pilot",
    )
    assert agent_only["preferred_working_limit"] == 98000


def test_context_window_gate_warning_advisory_and_invalidation(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
) -> None:
    make_context, _persisted = context_factory
    store = in_memory_plugin_backend["store"]
    store[("_model_config", "apollo", "pilot")] = {
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

    ctx = make_context(name="Primary", project="apollo", profile="pilot")
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
        project_name="apollo",
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
    in_memory_plugin_backend["store"][("swiss_cheese", "apollo", "pilot")] = scoped_cfg

    status = context_window.compute_context_window_status(agent)
    assert status["gate_active"] is False
    assert status["utility_warning_active"] is True
    assert status["utility_confidence"] == "reduced"

    store[("_model_config", "apollo", "pilot")]["chat_model"]["name"] = "gpt-chat-next"
    invalidated = context_window.compute_context_window_status(agent)
    assert invalidated["gate_active"] is True
    assert invalidated["chat_model"]["confirmed"] is False


def test_queue_followup_dedup_budget_and_loop_prevention(context_factory) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="Queue Source")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    queued, first = state_helper.queue_followup(
        ctx,
        target_context_id=ctx.id,
        reason="verify",
        message="Run verification",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert queued is True

    duplicate_pending, duplicate_pending_info = state_helper.queue_followup(
        ctx,
        target_context_id=ctx.id,
        reason="verify",
        message="Run verification",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert duplicate_pending is False
    assert duplicate_pending_info["reason"] == "duplicate_pending"

    state = ctx.get_data(CHAT_STATE_KEY)
    state["followup_queue"] = []
    state["last_followup_fingerprint"] = first["fingerprint"]
    ctx.set_data(CHAT_STATE_KEY, state)
    duplicate_last, duplicate_last_info = state_helper.queue_followup(
        ctx,
        target_context_id=ctx.id,
        reason="verify",
        message="Run verification",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert duplicate_last is False
    assert duplicate_last_info["reason"] == "duplicate_last_autonomous"

    state = ctx.get_data(CHAT_STATE_KEY)
    state["followup_queue"] = []
    state["recovery_cycles_used"] = plugin_config["max_auto_recovery_cycles"]
    ctx.set_data(CHAT_STATE_KEY, state)
    over_budget, over_budget_info = state_helper.queue_followup(
        ctx,
        target_context_id=ctx.id,
        reason="second-pass",
        message="Take another autonomous pass",
        auto_send=True,
        source="test",
        plugin_config=plugin_config,
    )
    assert over_budget is False
    assert over_budget_info["reason"] == "recovery_budget_exhausted"


def test_bridge_next_followup_respects_ctx_gate_and_manual_override(
    context_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_context, _persisted = context_factory
    source = make_context(name="Source", project="apollo", profile="pilot")
    target = make_context(name="Target", project="apollo", profile="pilot")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(source, plugin_config=plugin_config)
    source.set_data(
        "ctx_confirmation",
        {
            "gate_active": True,
            "chat_model": {},
            "utility_model": {},
        },
    )

    sent_to: list[str] = []
    monkeypatch.setattr(state_helper.mq, "send_next", lambda context: sent_to.append(context.id) or True)

    queued, item = state_helper.queue_followup(
        source,
        target_context_id=target.id,
        reason="recover",
        message="Continue with a bounded recovery pass.",
        auto_send=True,
        source="audit",
        plugin_config=plugin_config,
    )
    assert queued is True

    blocked = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=False)
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "chat_ctx_confirmation_gate"
    assert sent_to == []
    assert any(miss["fingerprint"] == item["fingerprint"] for miss in source.get_data("near_misses"))

    manual = state_helper.bridge_next_followup(source, plugin_config=plugin_config, manual=True)
    assert manual is not None
    assert manual["fingerprint"] == item["fingerprint"]
    assert sent_to == [target.id]
    assert target.get_data("_swiss_cheese_autonomy_origin")["fingerprint"] == item["fingerprint"]


def test_audit_parse_dirty_json_and_heuristic_fallback(context_factory) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="Audit Chat", project="apollo", profile="pilot")
    agent = ctx.get_agent()
    agent.set_data(TRANSIENT_RESPONSE_KEY, "Done. Implemented the fix.")

    ctx_status = {
        "chat_model": {
            "confirmed": False,
            "current_tokens": 120000,
            "preferred_working_limit": 100000,
        },
        "utility_model": {
            "confirmed": False,
        },
        "gate_active": True,
    }

    parsed, used_fallback = audit._parse_or_fallback(
        agent,
        "{'summary':'ok','holes':[{'kind':'active_failure','pattern':'premature_done','barrier':'Aviate','severity':'medium','confidence':0.9,'title':'Premature done'}],'todos':[],'near_misses':[],'followups':[]}",
        ctx_status,
    )
    assert used_fallback is False
    assert parsed["holes"][0]["pattern"] == "premature_done"

    fallback, used_fallback = audit._parse_or_fallback(agent, "not-json", ctx_status)
    patterns = {hole["pattern"] for hole in fallback["holes"]}
    assert used_fallback is True
    assert "chat_ctx_unconfirmed" in patterns
    assert "excessive_context_occupancy" in patterns


def test_apply_audit_result_normalizes_findings_and_dedupes_followups(
    context_factory,
) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="Audit Apply", project="apollo", profile="pilot")
    agent = ctx.get_agent()
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    duplicate_fp = state_helper.make_followup_fingerprint(ctx.id, "repeat", "Repeat the same step.")
    state = ctx.get_data(CHAT_STATE_KEY)
    state["last_followup_fingerprint"] = duplicate_fp
    ctx.set_data(CHAT_STATE_KEY, state)

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
        "near_misses": [
            {
                "title": "Unsafe continuation trapped",
                "detail": "The bad trajectory was trapped before damage.",
                "barrier": "Communicate",
                "severity": "medium",
                "confidence": 0.8,
            }
        ],
        "followups": [
            {
                "reason": "repeat",
                "message": "Repeat the same step.",
                "auto_send": True,
                "target": "current_chat",
            }
        ],
    }

    audit._apply_audit_result(
        agent,
        payload,
        ctx_status={"scope": {}},
        used_fallback=False,
        plugin_config=plugin_config,
    )

    holes = ctx.get_data("holes")
    todos = ctx.get_data("todos")
    near_misses = ctx.get_data("near_misses")
    assert holes[0]["pattern"] == "skipped_verification"
    assert holes[0]["severity"] == "high"
    assert any(todo["title"] == "Run a concrete verification step." for todo in todos)
    assert any(miss["title"] == "Unsafe continuation trapped" for miss in near_misses)
    assert any(miss["title"] == "Followup deduplicated" for miss in near_misses)


def test_add_or_update_todo_merges_duplicate_titles(context_factory) -> None:
    make_context, _persisted = context_factory
    ctx = make_context(name="Todo Merge")
    plugin_config = copy.deepcopy(SWISS_DEFAULT_CONFIG)
    state_helper.ensure_state(ctx, plugin_config=plugin_config)

    first = state_helper.add_or_update_todo(
        ctx,
        {
            "title": "Verify tool request schema",
            "detail": "Ensure the next SwissCheese call uses the documented argument shape.",
            "severity": "medium",
            "source": "audit",
            "status": "open",
        },
        plugin_config=plugin_config,
    )
    second = state_helper.add_or_update_todo(
        ctx,
        {
            "title": "Verify tool request schema",
            "detail": "Ensure the next SwissCheese call uses the documented argument shape before submission.",
            "severity": "high",
            "source": "heuristic_fallback",
            "status": "open",
        },
        plugin_config=plugin_config,
    )

    todos = ctx.get_data("todos") or []
    assert len(todos) == 1
    assert todos[0]["id"] == first["id"] == second["id"]
    assert todos[0]["severity"] == "high"
    assert "before submission" in todos[0]["detail"]


@pytest.mark.asyncio
async def test_swiss_cheese_tool_todo_lifecycle_and_status(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
) -> None:
    make_context, _persisted = context_factory
    store = in_memory_plugin_backend["store"]
    store[("_model_config", "apollo", "pilot")] = {
        "chat_model": {"provider": "openai", "name": "gpt-chat", "ctx_length": 65536},
        "utility_model": {"provider": "openai", "name": "gpt-util", "ctx_length": 32768},
    }

    ctx = make_context(name="Tool Chat", project="apollo", profile="pilot")
    agent = ctx.get_agent()
    agent.set_data(agent.DATA_NAME_CTX_WINDOW, {"tokens": 1234})
    ctx.set_data("_swiss_cheese_last_utility_input", {"tokens": 256})

    response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_add",
        args={},
        message="",
        loop_data=None,
    ).execute(title="Verify patch", detail="Run targeted pytest.", severity="high")
    todo_add = _parse_tool_payload(response)
    todo_id = todo_add["data"]["id"]
    assert todo_add["summary"] == "SwissCheese todo added."

    response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_list",
        args={},
        message="",
        loop_data=None,
    ).execute(status="open")
    todo_list = _parse_tool_payload(response)
    assert any(todo["id"] == todo_id for todo in todo_list["data"]["todos"])

    response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_resolve",
        args={},
        message="",
        loop_data=None,
    ).execute(todo_id=todo_id)
    todo_resolve = _parse_tool_payload(response)
    assert todo_resolve["data"]["ok"] is True
    assert todo_resolve["data"]["todo"]["status"] == "completed"

    response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_list",
        args={},
        message="",
        loop_data=None,
    ).execute(status="completed")
    completed_list = _parse_tool_payload(response)
    assert any(todo["id"] == todo_id for todo in completed_list["data"]["todos"])

    response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="todo_clear_completed",
        args={},
        message="",
        loop_data=None,
    ).execute(confirm=True)
    cleared = _parse_tool_payload(response)
    assert cleared["data"]["remaining"] == 0

    response = await SwissCheeseTool(
        agent=agent,
        name="swiss_cheese",
        method="status",
        args={},
        message="",
        loop_data=None,
    ).execute(detail="full")
    status = _parse_tool_payload(response)
    assert "context_window" in status["data"]
    assert status["data"]["context_window"]["chat_model"]["current_tokens"] == 1234


@pytest.mark.asyncio
async def test_tool_inspect_chat_and_live_chat_only_queueing(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
) -> None:
    make_context, persist_chat = context_factory
    in_memory_plugin_backend["store"][("swiss_cheese", "apollo", "pilot")] = {
        "cross_chat_scope": {
            "same_project_live_write": True,
            "same_project_persisted_readonly": True,
            "cross_project": False,
        }
    }

    source = make_context(name="Source Chat", project="apollo", profile="pilot")
    target = make_context(name="Beta Mission", project="apollo", profile="pilot")
    persist_chat(name="Gamma Mission", project="apollo")

    inspect_live = await SwissCheeseTool(
        agent=source.get_agent(),
        name="swiss_cheese",
        method="inspect_chat",
        args={},
        message="",
        loop_data=None,
    ).execute(selector="Beta Mission")
    inspect_live_payload = _parse_tool_payload(inspect_live)
    assert inspect_live_payload["data"]["permissions"]["can_queue"] is True

    queue_live = await SwissCheeseTool(
        agent=source.get_agent(),
        name="swiss_cheese",
        method="queue_followup",
        args={},
        message="",
        loop_data=None,
    ).execute(
        selector="Beta Mission",
        reason="handoff",
        message="Continue the bounded recovery sequence.",
        auto_send=False,
    )
    queue_live_payload = _parse_tool_payload(queue_live)
    assert queue_live_payload["data"]["queued"] is True

    inspect_persisted = await SwissCheeseTool(
        agent=source.get_agent(),
        name="swiss_cheese",
        method="inspect_chat",
        args={},
        message="",
        loop_data=None,
    ).execute(selector="Gamma Mission")
    inspect_persisted_payload = _parse_tool_payload(inspect_persisted)
    assert inspect_persisted_payload["data"]["permissions"]["can_read"] is True
    assert inspect_persisted_payload["data"]["permissions"]["can_queue"] is False


def test_chat_discovery_resolves_exact_and_fuzzy_matches_with_scope(context_factory) -> None:
    make_context, persist_chat = context_factory
    source = make_context(name="Source Chat", project="apollo", profile="pilot")
    live = make_context(name="Beta Mission", project="apollo", profile="pilot")
    persist_chat(name="Gamma Mission", project="apollo")
    scope = {
        "same_project_live_write": True,
        "same_project_persisted_readonly": True,
        "cross_project": False,
    }

    exact = discovery.inspect_chat(source_context=source, selector=live.id, scope=scope)
    assert exact["match_type"] == "exact_context_id"
    assert exact["permissions"]["can_queue"] is True

    exact_name = discovery.inspect_chat(source_context=source, selector="Beta Mission", scope=scope)
    assert exact_name["match_type"] == "exact_name"

    fuzzy = discovery.inspect_chat(source_context=source, selector="Gmma Mision", scope=scope)
    assert fuzzy["match_type"] == "fuzzy_name"
    assert fuzzy["target"]["persisted_only"] is True
    assert fuzzy["permissions"]["can_read"] is True
    assert fuzzy["permissions"]["can_queue"] is False


@pytest.mark.asyncio
async def test_api_confirm_ctx_window_updates_model_config_and_live_state(
    context_factory,
    in_memory_plugin_backend: dict[str, Any],
) -> None:
    make_context, _persisted = context_factory
    store = in_memory_plugin_backend["store"]
    store[("_model_config", "apollo", "pilot")] = {
        "chat_model": {
            "provider": "openai",
            "name": "gpt-chat",
            "ctx_length": 0,
        },
        "utility_model": {
            "provider": "openai",
            "name": "gpt-util",
            "ctx_length": 32000,
        },
    }

    ctx_one = make_context(name="One", project="apollo", profile="pilot")
    ctx_two = make_context(name="Two", project="apollo", profile="pilot")
    api = _new_api()

    response = await api.process(
        {
            "action": "confirm_ctx_window",
            "project_name": "apollo",
            "agent_profile": "pilot",
            "slot": "chat_model",
            "provider": "openai",
            "name": "gpt-chat",
            "ctx_length": 131072,
            "update_model_config": True,
        },
        None,
    )
    assert response["ok"] is True
    assert store[("_model_config", "apollo", "pilot")]["chat_model"]["ctx_length"] == 131072

    state_payload = await api.process(
        {
            "action": "get_state",
            "context_id": ctx_one.id,
        },
        None,
    )
    assert state_payload["ok"] is True
    assert state_payload["context_window"]["chat_model"]["confirmed"] is True
    assert ctx_two.get_data("ctx_confirmation")["chat_model"]["confirmed"] is True


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
