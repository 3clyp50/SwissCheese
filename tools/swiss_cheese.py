from __future__ import annotations

import json

from helpers.tool import Tool, Response

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import context_window, discovery, state as state_helper


class SwissCheese(Tool):

    async def execute(self, **kwargs):
        if self.method == "status":
            return await self._status(**kwargs)
        if self.method == "context_window":
            return await self._context_window(**kwargs)
        if self.method == "todo_add":
            return await self._todo_add(**kwargs)
        if self.method == "todo_list":
            return await self._todo_list(**kwargs)
        if self.method == "todo_resolve":
            return await self._todo_resolve(**kwargs)
        if self.method == "todo_clear_completed":
            return await self._todo_clear_completed(**kwargs)
        if self.method == "inspect_chat":
            return await self._inspect_chat(**kwargs)
        if self.method == "queue_followup":
            return await self._queue_followup(**kwargs)
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

    async def _status(self, detail: str = "full", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        state_helper.ensure_state(self.agent.context, plugin_config=plugin_config)
        ctx_status = context_window.compute_context_window_status(self.agent, plugin_config=plugin_config)
        data = state_helper.get_state_bundle(self.agent.context)
        data["context_window"] = ctx_status
        if str(detail or "full").strip().lower() == "summary":
            data = {
                "audit_status": data.get("audit_status", {}),
                "recovery_budget": data.get("recovery_budget", {}),
                "holes": data.get("holes", []),
                "todos": [todo for todo in data.get("todos", []) if todo.get("status") != "completed"],
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

    async def _todo_add(self, title: str = "", detail: str = "", severity: str = "medium", **kwargs) -> Response:
        if not title.strip():
            return Response(
                message=self._encode("Todo title is required.", {"ok": False}),
                break_loop=False,
            )
        plugin_config = swiss_config.get_plugin_config(self.agent)
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
            message=self._encode("SwissCheese todo added.", record),
            break_loop=False,
        )

    async def _todo_list(self, status: str = "open", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        state_helper.ensure_state(self.agent.context, plugin_config=plugin_config)
        todos = self.agent.context.get_data("todos") or []
        normalized_status = str(status or "open").strip().lower()
        if normalized_status in ("open", "completed"):
            todos = [todo for todo in todos if todo.get("status") == normalized_status]
        else:
            normalized_status = "all"
        return Response(
            message=self._encode(
                "SwissCheese todos listed.",
                {"status": normalized_status, "todos": todos},
            ),
            break_loop=False,
        )

    async def _todo_resolve(self, todo_id: str = "", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        todo = state_helper.resolve_todo(
            self.agent.context,
            todo_id,
            plugin_config=plugin_config,
        )
        return Response(
            message=self._encode(
                "SwissCheese todo resolved." if todo else "SwissCheese todo not found.",
                {"todo": todo, "ok": bool(todo)},
            ),
            break_loop=False,
        )

    async def _todo_clear_completed(self, confirm: bool = False, **kwargs) -> Response:
        if not confirm:
            return Response(
                message=self._encode(
                    "Confirmation required before clearing completed SwissCheese todos.",
                    {"ok": False},
                ),
                break_loop=False,
            )
        plugin_config = swiss_config.get_plugin_config(self.agent)
        remaining = state_helper.clear_completed_todos(
            self.agent.context,
            plugin_config=plugin_config,
        )
        return Response(
            message=self._encode(
                "SwissCheese completed todos cleared.",
                {"remaining": remaining},
            ),
            break_loop=False,
        )

    async def _inspect_chat(self, selector: str = "", **kwargs) -> Response:
        plugin_config = swiss_config.get_plugin_config(self.agent)
        inspection = discovery.inspect_chat(
            source_context=self.agent.context,
            selector=selector,
            scope=plugin_config.get("cross_chat_scope", {}),
        )
        return Response(
            message=self._encode("SwissCheese chat inspection.", inspection),
            break_loop=False,
        )

    async def _queue_followup(
        self,
        selector: str = "",
        message: str = "",
        reason: str = "",
        auto_send: bool = False,
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
        target_context_id = self.agent.context.id

        if selector.strip():
            inspection = discovery.inspect_chat(
                source_context=self.agent.context,
                selector=selector,
                scope=plugin_config.get("cross_chat_scope", {}),
            )
            target = inspection.get("target") or {}
            if not inspection.get("permissions", {}).get("can_queue", False):
                return Response(
                    message=self._encode(
                        "Followup target is not queueable in the current scope.",
                        {"inspection": inspection, "ok": False},
                    ),
                    break_loop=False,
                )
            target_context_id = str(target.get("id", "") or self.agent.context.id)

        queued, payload = state_helper.queue_followup(
            self.agent.context,
            target_context_id=target_context_id,
            reason=reason,
            message=message,
            auto_send=bool(auto_send),
            source="tool",
            plugin_config=plugin_config,
        )
        return Response(
            message=self._encode(
                "SwissCheese followup queued." if queued else "SwissCheese followup rejected.",
                {"queued": queued, "result": payload},
            ),
            break_loop=False,
        )
