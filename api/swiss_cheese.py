from __future__ import annotations

from datetime import datetime, timezone

from agent import AgentContext
from helpers.api import ApiHandler, Request, Response

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import context_window, discovery, project_state, state as state_helper


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SwissCheese(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        action = str(input.get("action", "") or "")

        if action == "get_state":
            return self._get_state(input)
        if action == "list_chat_targets":
            return self._list_chat_targets(input)
        if action == "inspect_chat":
            return self._inspect_chat(input)
        if action == "confirm_ctx_window":
            return self._confirm_ctx_window(input)
        if action == "todo_add":
            return self._todo_add(input)
        if action == "todo_list":
            return self._todo_list(input)
        if action == "todo_resolve":
            return self._todo_resolve(input)
        if action == "todo_clear_completed":
            return self._todo_clear_completed(input)
        if action == "queue_followup":
            return self._queue_followup(input)
        if action == "remove_followup":
            return self._remove_followup(input)
        if action == "bridge_followup":
            return self._bridge_followup(input)

        return Response(status=400, response=f"Unknown action: {action}")

    def _get_context(self, input: dict) -> AgentContext | None:
        context_id = str(input.get("context_id", "") or input.get("context", "") or "")
        if not context_id:
            return AgentContext.current() or AgentContext.first()
        return AgentContext.get(context_id)

    def _matching_live_scope_contexts(self, project_name: str, agent_profile: str) -> list[AgentContext]:
        matches: list[AgentContext] = []
        for context in AgentContext.all():
            project = context.get_data("project") or ""
            profile = context.agent0.config.profile or ""
            if project == project_name and profile == agent_profile:
                matches.append(context)
        return matches

    def _sync_live_scope_contexts(self, project_name: str, agent_profile: str) -> None:
        for context in self._matching_live_scope_contexts(project_name, agent_profile):
            agent = context.get_agent()
            plugin_config = swiss_config.get_plugin_config(agent)
            state_helper.ensure_state(context, plugin_config=plugin_config)
            ctx_status = context_window.compute_context_window_status(agent, plugin_config=plugin_config)
            recovery_budget = context.get_data("recovery_budget") or {"max_cycles": 0, "used_cycles": 0, "remaining_cycles": 0}
            cross_chat_scope = plugin_config.get("cross_chat_scope", {})
            context_window.mirror_context_window_status(context, ctx_status, recovery_budget, cross_chat_scope)
            state_helper.sync_output_data(context, plugin_config=plugin_config, dirty=True)

    def _build_state_payload(self, context: AgentContext) -> dict[str, object]:
        agent = context.get_agent()
        plugin_config = swiss_config.get_plugin_config(agent)
        state_helper.ensure_state(context, plugin_config=plugin_config)
        ctx_status = context_window.compute_context_window_status(agent, plugin_config=plugin_config)
        context_window.mirror_context_window_status(
            context,
            ctx_status,
            context.get_data("recovery_budget") or {"max_cycles": 0, "used_cycles": 0, "remaining_cycles": 0},
            plugin_config.get("cross_chat_scope", {}),
        )
        state_helper.sync_output_data(context, plugin_config=plugin_config, dirty=True)

        project_name = project_state.get_project_name(context)
        project_state_payload = None
        project_rollup = None
        available_views = ["chat"]
        default_view = "chat"

        if project_name:
            project_payload = project_state.get_project_state_for_context(context) or {}
            project_state_payload = {
                "project_name": project_name,
                "project_title": str((context.get_output_data("project") or {}).get("title", "") or project_name),
                "todos": project_state.list_project_todos(context, status="all"),
                "notification_history": list(project_payload.get("notification_history", []) or []),
                "updated_at": str(project_payload.get("updated_at", "") or ""),
            }
            project_rollup = discovery.build_project_rollup(
                source_context=context,
                scope=plugin_config.get("cross_chat_scope", {}),
            )
            available_views.append("project")
            default_view = "project"

        return {
            "ok": True,
            "context_id": context.id,
            "state": state_helper.get_state_bundle(context),
            "chat_state": state_helper.get_state_bundle(context),
            "project_state": project_state_payload,
            "project_rollup": project_rollup,
            "available_views": available_views,
            "default_view": default_view,
            "context_window": ctx_status,
            "model_snapshot": {
                "chat_model": ctx_status.get("chat_model", {}),
                "utility_model": ctx_status.get("utility_model", {}),
            },
            "scope": plugin_config.get("cross_chat_scope", {}),
            "catalog_defaults": {
                "project_only": bool(project_name),
                "include_persisted": True,
            },
        }

    def _normalize_scope(self, input: dict) -> str:
        scope = str(input.get("scope", "chat") or "chat").strip().lower()
        return scope if scope in {"chat", "project"} else "chat"

    def _get_state(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        return self._build_state_payload(context)

    def _list_chat_targets(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        targets = discovery.list_chat_catalog(
            source_context=context,
            scope=plugin_config.get("cross_chat_scope", {}),
            project_only=bool(input.get("project_only", False)),
            include_persisted=bool(input.get("include_persisted", True)),
        )
        return {"ok": True, "targets": targets}

    def _inspect_chat(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        agent = context.get_agent()
        plugin_config = swiss_config.get_plugin_config(agent)
        inspection = discovery.inspect_chat(
            source_context=context,
            selector=str(input.get("selector", "") or ""),
            target_context_id=str(input.get("target_context_id", "") or ""),
            scope=plugin_config.get("cross_chat_scope", {}),
            project_only=bool(input.get("project_only", False)),
            include_persisted=bool(input.get("include_persisted", True)),
        )
        return {"ok": True, "inspection": inspection}

    def _confirm_ctx_window(self, input: dict) -> dict | Response:
        slot = str(input.get("slot", "") or "").strip()
        if slot not in ("chat_model", "utility_model"):
            return Response(status=400, response="slot must be chat_model or utility_model")

        project_name = str(input.get("project_name", "") or "")
        agent_profile = str(input.get("agent_profile", "") or "")
        provider = str(input.get("provider", "") or "")
        name = str(input.get("name", "") or "")
        raw_ctx_length = input.get("ctx_length", None)
        update_model_config = bool(input.get("update_model_config", True))

        swiss_plugin_config = swiss_config.get_plugin_config(
            agent=None,
            project_name=project_name,
            agent_profile=agent_profile,
        )
        model_config = swiss_config.get_model_config(
            agent=None,
            project_name=project_name,
            agent_profile=agent_profile,
        )
        section = dict(model_config.get(slot, {}) or {})
        if provider:
            section["provider"] = provider
        if name:
            section["name"] = name
        if raw_ctx_length is not None:
            try:
                section["ctx_length"] = int(raw_ctx_length)
            except (TypeError, ValueError):
                return Response(status=400, response="ctx_length must be an integer")
        if update_model_config:
            model_config[slot] = section
            swiss_config.save_model_config(project_name, agent_profile, model_config)

        tuple_data = context_window.build_model_tuple(slot, section)
        if not tuple_data["provider"] or not tuple_data["name"] or int(tuple_data["ctx_length"] or 0) <= 0:
            return Response(status=400, response="provider, name, and ctx_length must all be set before confirmation")

        tuple_data["confirmed_at"] = iso_now()
        swiss_config.append_confirmed_tuple(swiss_plugin_config, slot, tuple_data)
        swiss_config.save_plugin_config(project_name, agent_profile, swiss_plugin_config)
        self._sync_live_scope_contexts(project_name, agent_profile)

        return {
            "ok": True,
            "confirmed_tuple": tuple_data,
            "project_name": project_name,
            "agent_profile": agent_profile,
        }

    def _todo_add(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        title = str(input.get("title", "") or "").strip()
        if not title:
            return Response(status=400, response="title is required")
        scope = self._normalize_scope(input)
        plugin_config = swiss_config.get_plugin_config(context.get_agent())

        if scope == "project":
            todo = project_state.add_or_update_project_todo(
                context,
                {
                    "title": title,
                    "detail": str(input.get("detail", "") or ""),
                    "severity": str(input.get("severity", "medium") or "medium"),
                    "source": "api",
                    "status": "open",
                },
                plugin_config=plugin_config,
            )
            if todo is None:
                return Response(status=400, response="Project scope requires an active project")
        else:
            todo = state_helper.add_or_update_todo(
                context,
                {
                    "title": title,
                    "detail": str(input.get("detail", "") or ""),
                    "severity": str(input.get("severity", "medium") or "medium"),
                    "source": "api",
                    "status": "open",
                },
                plugin_config=plugin_config,
            )
        return {"ok": True, "scope": scope, "todo": todo}

    def _todo_list(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        scope = self._normalize_scope(input)
        status = str(input.get("status", "open") or "open")
        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        if scope == "project":
            todos = project_state.list_project_todos(context, status=status)
            if not project_state.get_project_name(context):
                return Response(status=400, response="Project scope requires an active project")
        else:
            todos = state_helper.list_todos(context, status=status, plugin_config=plugin_config)
        return {"ok": True, "scope": scope, "status": status, "todos": todos}

    def _todo_resolve(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        todo_id = str(input.get("todo_id", "") or "")
        scope = self._normalize_scope(input)
        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        if scope == "project":
            todo = project_state.resolve_project_todo(context, todo_id)
            if not project_state.get_project_name(context):
                return Response(status=400, response="Project scope requires an active project")
        else:
            todo = state_helper.resolve_todo(context, todo_id, plugin_config=plugin_config)
        if todo is None:
            return Response(status=404, response="Todo not found")
        return {"ok": True, "scope": scope, "todo": todo}

    def _todo_clear_completed(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        scope = self._normalize_scope(input)
        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        if scope == "project":
            remaining = project_state.clear_completed_project_todos(context)
            if remaining is None:
                return Response(status=400, response="Project scope requires an active project")
        else:
            remaining = state_helper.clear_completed_todos(context, plugin_config=plugin_config)
        return {"ok": True, "scope": scope, "remaining": remaining}

    def _queue_followup(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        message = str(input.get("message", "") or "").strip()
        reason = str(input.get("reason", "") or "").strip()
        if not message or not reason:
            return Response(status=400, response="message and reason are required")

        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        selector = str(input.get("selector", "") or "").strip()
        requested_target_context_id = str(input.get("target_context_id", "") or "").strip()
        target_context_id = context.id
        inspection = None

        if selector or requested_target_context_id:
            inspection = discovery.inspect_chat(
                source_context=context,
                selector=selector,
                target_context_id=requested_target_context_id,
                scope=plugin_config.get("cross_chat_scope", {}),
            )
            target = inspection.get("target") or {}
            if not inspection.get("permissions", {}).get("can_queue", False):
                return Response(status=403, response="Target chat is not queueable in the current scope")
            target_context_id = str(target.get("id", "") or context.id)

        queued, payload = state_helper.queue_followup(
            context,
            target_context_id=target_context_id,
            reason=reason,
            message=message,
            auto_send=bool(input.get("auto_send", False)),
            source="api",
            plugin_config=plugin_config,
        )
        if not queued:
            return {
                "ok": False,
                "queued": False,
                "result": payload,
                "inspection": inspection,
            }
        return {
            "ok": True,
            "queued": True,
            "result": payload,
            "inspection": inspection,
        }

    def _remove_followup(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        fingerprint = str(input.get("fingerprint", "") or "")
        if not fingerprint:
            return Response(status=400, response="fingerprint is required")
        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        removed = state_helper.remove_followup(context, fingerprint, plugin_config=plugin_config)
        return {"ok": removed}

    def _bridge_followup(self, input: dict) -> dict | Response:
        context = self._get_context(input)
        if context is None:
            return Response(status=404, response="Context not found")
        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        bridged = state_helper.bridge_next_followup(context, plugin_config=plugin_config, manual=True)
        return {"ok": bool(bridged), "bridged": bridged}
