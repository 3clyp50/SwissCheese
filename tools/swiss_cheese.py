from __future__ import annotations

import json

from helpers.tool import Tool, Response

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import context_window, discovery, project_state, state as state_helper


class SwissCheese(Tool):

    async def execute(self, **kwargs):
        if self.method == "status":
            return await self._status(**kwargs)
        if self.method == "context_window":
            return await self._context_window(**kwargs)
        if self.method == "target_catalog":
            return await self._target_catalog(**kwargs)
        if self.method == "chat_catalog":
            return await self._target_catalog(**kwargs)
        if self.method == "todo_add":
            return await self._todo_add(**kwargs)
        if self.method == "todo_list":
            return await self._todo_list(**kwargs)
        if self.method == "todo_resolve":
            return await self._todo_resolve(**kwargs)
        if self.method == "todo_clear_completed":
            return await self._todo_clear_completed(**kwargs)
        if self.method == "inspect_target":
            return await self._inspect_target(**kwargs)
        if self.method == "inspect_chat":
            return await self._inspect_target(**kwargs)
        if self.method == "queue_followup":
            return await self._queue_followup(**kwargs)
        if self.method == "bridge_followup":
            return await self._bridge_followup(**kwargs)
        return Response(
            message=self._encode(
                "Unknown SwissCheese method.",
                {"method": self.method or "", "ok": False},
            ),
            break_loop=False,
        )

    def _encode(self, summary: str, data: dict) -> str:
        payload = {"summary": summary, "data": data}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _normalize_scope(self, scope: str) -> str:
        normalized = str(scope or "chat").strip().lower()
        return normalized if normalized in {"chat", "project"} else "chat"

    async def _status(self, detail: str = "full", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        state_helper.ensure_state(self.agent.context, plugin_config=plugin_config)
        ctx_status = context_window.compute_context_window_status(self.agent, plugin_config=plugin_config)
        project_rollup = discovery.build_project_rollup(
            source_context=self.agent.context,
            scope=plugin_config.get("cross_chat_scope", {}),
        )
        project_payload = project_state.get_project_state_for_context(self.agent.context)

        data = {
            "chat_state": state_helper.get_state_bundle(self.agent.context),
            "project_state": (
                {
                    "project_name": project_state.get_project_name(self.agent.context),
                    "todos": project_state.list_project_todos(self.agent.context, status="all"),
                    "notification_history": list((project_payload or {}).get("notification_history", []) or []),
                    "updated_at": str((project_payload or {}).get("updated_at", "") or ""),
                }
                if project_payload
                else None
            ),
            "project_rollup": project_rollup,
            "context_window": ctx_status,
        }
        if str(detail or "full").strip().lower() == "summary":
            data = {
                "audit_status": data["chat_state"].get("audit_status", {}),
                "recovery_budget": data["chat_state"].get("recovery_budget", {}),
                "holes": data["chat_state"].get("holes", []),
                "chat_todos": [todo for todo in data["chat_state"].get("todos", []) if todo.get("status") != "completed"],
                "project_todos": (
                    [todo for todo in (data["project_state"] or {}).get("todos", []) if todo.get("status") != "completed"]
                    if data.get("project_state")
                    else []
                ),
                "project_rollup": data.get("project_rollup"),
                "context_window": ctx_status,
            }
        return Response(
            message=self._encode(
                "SwissCheese status snapshot.",
                data,
            ),
            break_loop=False,
        )

    async def _context_window(self, slot: str = "all", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        ctx_status = context_window.compute_context_window_status(self.agent, plugin_config=plugin_config)
        normalized_slot = str(slot or "all").strip().lower()
        if normalized_slot in ("chat", "chat_model"):
            data = {
                "slot": "chat",
                "gate_active": ctx_status.get("gate_active", False),
                "model": ctx_status.get("chat_model", {}),
            }
        elif normalized_slot in ("utility", "utility_model"):
            data = {
                "slot": "utility",
                "utility_warning_active": ctx_status.get("utility_warning_active", False),
                "model": ctx_status.get("utility_model", {}),
            }
        else:
            data = ctx_status
        return Response(
            message=self._encode("SwissCheese context-window snapshot.", data),
            break_loop=False,
        )

    async def _target_catalog(
        self,
        project_only: bool = False,
        include_persisted: bool = True,
        kind: str = "all",
        **kwargs,
    ) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        targets = discovery.list_targets(
            source_context=self.agent.context,
            scope=plugin_config.get("cross_chat_scope", {}),
            project_only=bool(project_only),
            include_persisted=bool(include_persisted),
            kind=str(kind or "all"),
        )
        return Response(
            message=self._encode(
                "SwissCheese target catalog listed.",
                {
                    "project_only": bool(project_only),
                    "include_persisted": bool(include_persisted),
                    "kind": str(kind or "all"),
                    "targets": targets,
                },
            ),
            break_loop=False,
        )

    async def _todo_add(
        self,
        title: str = "",
        detail: str = "",
        severity: str = "medium",
        scope: str = "chat",
        **kwargs,
    ) -> Response:
        if not title.strip():
            return Response(
                message=self._encode("Todo title is required.", {"ok": False}),
                break_loop=False,
            )
        plugin_config = swiss_config.get_plugin_config(self.agent)
        normalized_scope = self._normalize_scope(scope)
        if normalized_scope == "project":
            record = project_state.add_or_update_project_todo(
                self.agent.context,
                {
                    "title": title,
                    "detail": detail,
                    "severity": severity,
                    "source": "tool",
                    "status": "open",
                },
                plugin_config=plugin_config,
            )
            if record is None:
                return Response(
                    message=self._encode("Project scope requires an active project.", {"ok": False}),
                    break_loop=False,
                )
        else:
            record = state_helper.add_or_update_todo(
                self.agent.context,
                {
                    "title": title,
                    "detail": detail,
                    "severity": severity,
                    "source": "tool",
                    "status": "open",
                },
                plugin_config=plugin_config,
            )
        return Response(
            message=self._encode("SwissCheese todo added.", {"scope": normalized_scope, "todo": record}),
            break_loop=False,
        )

    async def _todo_list(self, status: str = "open", scope: str = "chat", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        normalized_scope = self._normalize_scope(scope)
        normalized_status = str(status or "open").strip().lower()
        if normalized_scope == "project":
            if not project_state.get_project_name(self.agent.context):
                return Response(
                    message=self._encode("Project scope requires an active project.", {"ok": False}),
                    break_loop=False,
                )
            todos = project_state.list_project_todos(self.agent.context, status=normalized_status)
        else:
            todos = state_helper.list_todos(
                self.agent.context,
                status=normalized_status,
                plugin_config=plugin_config,
            )
        return Response(
            message=self._encode(
                "SwissCheese todos listed.",
                {"scope": normalized_scope, "status": normalized_status, "todos": todos},
            ),
            break_loop=False,
        )

    async def _todo_resolve(self, todo_id: str = "", scope: str = "chat", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        normalized_scope = self._normalize_scope(scope)
        if normalized_scope == "project":
            if not project_state.get_project_name(self.agent.context):
                return Response(
                    message=self._encode("Project scope requires an active project.", {"ok": False}),
                    break_loop=False,
                )
            todo = project_state.resolve_project_todo(self.agent.context, todo_id)
        else:
            todo = state_helper.resolve_todo(
                self.agent.context,
                todo_id,
                plugin_config=plugin_config,
            )
        return Response(
            message=self._encode(
                "SwissCheese todo resolved." if todo else "SwissCheese todo not found.",
                {"scope": normalized_scope, "todo": todo, "ok": bool(todo)},
            ),
            break_loop=False,
        )

    async def _todo_clear_completed(self, confirm: bool = False, scope: str = "chat", **kwargs) -> Response:
        if not confirm:
            return Response(
                message=self._encode(
                    "Confirmation required before clearing completed SwissCheese todos.",
                    {"ok": False},
                ),
                break_loop=False,
            )
        plugin_config = swiss_config.get_plugin_config(self.agent)
        normalized_scope = self._normalize_scope(scope)
        if normalized_scope == "project":
            if not project_state.get_project_name(self.agent.context):
                return Response(
                    message=self._encode("Project scope requires an active project.", {"ok": False}),
                    break_loop=False,
                )
            remaining = project_state.clear_completed_project_todos(self.agent.context)
        else:
            remaining = state_helper.clear_completed_todos(
                self.agent.context,
                plugin_config=plugin_config,
            )
        return Response(
            message=self._encode(
                "SwissCheese completed todos cleared.",
                {"scope": normalized_scope, "remaining": remaining},
            ),
            break_loop=False,
        )

    async def _inspect_target(
        self,
        selector: str = "",
        target_key: str = "",
        target_context_id: str = "",
        project_only: bool = False,
        include_persisted: bool = True,
        kind: str = "all",
        **kwargs,
    ) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        inspection = discovery.inspect_target(
            source_context=self.agent.context,
            selector=selector,
            target_key=target_key,
            target_context_id=target_context_id,
            scope=plugin_config.get("cross_chat_scope", {}),
            project_only=bool(project_only),
            include_persisted=bool(include_persisted),
            kind=str(kind or "all"),
        )
        return Response(
            message=self._encode("SwissCheese target inspection.", inspection),
            break_loop=False,
        )

    async def _queue_followup(
        self,
        selector: str = "",
        target_key: str = "",
        target_context_id: str = "",
        message: str = "",
        reason: str = "",
        auto_send: bool = False,
        kind: str = "all",
        **kwargs,
    ) -> Response:
        if not message.strip() or not reason.strip():
            return Response(
                message=self._encode(
                    "Followup message and reason are required.",
                    {"ok": False},
                ),
                break_loop=False,
            )

        plugin_config = swiss_config.get_plugin_config(self.agent)
        inspection = discovery.inspect_target(
            source_context=self.agent.context,
            selector=selector,
            target_key=target_key,
            target_context_id=target_context_id,
            scope=plugin_config.get("cross_chat_scope", {}),
            kind=str(kind or "all"),
        )
        target = inspection.get("target") or {}
        if not target:
            return Response(
                message=self._encode(
                    "Followup target was not found.",
                    {
                        "inspection": inspection,
                        "ok": False,
                        "result": state_helper.record_blocked_followup(
                            self.agent.context,
                            target_key=target_key,
                            target_context_id=target_context_id,
                            target_name=selector or target_key or target_context_id,
                            target_kind="chat",
                            reason=reason,
                            message=message,
                            blocked_reason="target_not_found",
                            auto_send=bool(auto_send),
                            source="tool",
                            plugin_config=plugin_config,
                        ),
                    },
                ),
                break_loop=False,
            )
        if not inspection.get("permissions", {}).get("can_queue", False):
            return Response(
                message=self._encode(
                    "Followup target is not queueable in the current scope.",
                    {
                        "inspection": inspection,
                        "ok": False,
                        "result": state_helper.record_blocked_followup(
                            self.agent.context,
                            target_key=str(target.get("target_key", "") or target_key),
                            target_context_id=str(target.get("context_id", "") or target_context_id),
                            target_kind=str(target.get("kind", "chat") or "chat"),
                            target_task_uuid=str(((target.get("scheduler") or {}) if isinstance(target.get("scheduler"), dict) else {}).get("uuid", "") or ""),
                            target_name=str(target.get("name", "") or selector or target_key or target_context_id),
                            reason=reason,
                            message=message,
                            blocked_reason="target_not_queueable_in_scope",
                            auto_send=bool(auto_send),
                            source="tool",
                            plugin_config=plugin_config,
                        ),
                    },
                ),
                break_loop=False,
            )

        queued, payload = state_helper.queue_followup(
            self.agent.context,
            target_key=str(target.get("target_key", "") or ""),
            target_kind=str(target.get("kind", "chat") or "chat"),
            target_context_id=str(target.get("context_id", "") or ""),
            target_task_uuid=str(((target.get("scheduler") or {}) if isinstance(target.get("scheduler"), dict) else {}).get("uuid", "") or ""),
            target_name=str(target.get("name", "") or ""),
            reason=reason,
            message=message,
            auto_send=bool(auto_send),
            source="tool",
            plugin_config=plugin_config,
        )
        return Response(
            message=self._encode(
                "SwissCheese followup queued." if queued else "SwissCheese followup rejected.",
                {"queued": queued, "result": payload, "inspection": inspection},
            ),
            break_loop=False,
        )

    async def _bridge_followup(
        self,
        fingerprint: str = "",
        send_now: bool | None = None,
        **kwargs,
    ) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        result = state_helper.bridge_next_followup(
            self.agent.context,
            plugin_config=plugin_config,
            manual=True,
            fingerprint=fingerprint,
            send_now=send_now,
        )
        return Response(
            message=self._encode(
                "SwissCheese followup delivery processed." if result else "No SwissCheese followup was ready.",
                {"ok": bool(result), "result": result},
            ),
            break_loop=False,
        )
