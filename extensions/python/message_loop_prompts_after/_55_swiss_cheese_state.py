from helpers.extension import Extension
from agent import LoopData

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import context_window, state as state_helper


def _format_holes(holes: list[dict]) -> str:
    if not holes:
        return "- none"
    lines = []
    for hole in holes[:4]:
        lines.append(
            f"- [{hole.get('barrier', 'Navigate')}] {hole.get('pattern', 'unknown')} ({hole.get('severity', 'medium')})"
        )
    return "\n".join(lines)


def _format_todos(todos: list[dict]) -> str:
    if not todos:
        return "- none"
    lines = []
    for todo in todos[:4]:
        status = todo.get("status", "open")
        lines.append(f"- [{status}] {todo.get('title', '')}")
    return "\n".join(lines)


class SwissCheesePromptState(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent or self.agent.number != 0:
            return

        plugin_config = swiss_config.get_plugin_config(self.agent)
        state_helper.ensure_state(self.agent.context, plugin_config=plugin_config)
        ctx_status = context_window.compute_context_window_status(self.agent, plugin_config=plugin_config)
        recovery_budget = self.agent.context.get_data("recovery_budget") or {"max_cycles": 0, "used_cycles": 0, "remaining_cycles": 0}
        scope = plugin_config.get("cross_chat_scope", {})

        context_window.mirror_context_window_status(
            self.agent.context,
            ctx_status,
            recovery_budget,
            scope,
        )
        state_helper.sync_output_data(self.agent.context, plugin_config=plugin_config, dirty=True)

        chat = ctx_status.get("chat_model", {})
        utility = ctx_status.get("utility_model", {})
        prompt = self.agent.read_prompt(
            "agent.context.swiss_cheese.md",
            chat_gate_active="yes" if ctx_status.get("gate_active", False) else "no",
            chat_ctx_summary=(
                f"{chat.get('provider', '')}/{chat.get('name', '')} "
                f"confirmed={chat.get('confirmed', False)} "
                f"tokens={chat.get('current_tokens', 0)} "
                f"remaining={chat.get('remaining_budget', 0)}"
            ).strip(),
            utility_ctx_summary=(
                f"{utility.get('provider', '')}/{utility.get('name', '')} "
                f"confirmed={utility.get('confirmed', False)} "
                f"tokens={utility.get('current_tokens', 0)} "
                f"remaining={utility.get('remaining_budget', 0)}"
            ).strip(),
            scope_summary=(
                f"same_project_live_write={scope.get('same_project_live_write', False)}, "
                f"same_project_persisted_readonly={scope.get('same_project_persisted_readonly', False)}, "
                f"cross_project={scope.get('cross_project', False)}"
            ),
            recovery_summary=(
                f"used={recovery_budget.get('used_cycles', 0)}/"
                f"{recovery_budget.get('max_cycles', 0)} "
                f"remaining={recovery_budget.get('remaining_cycles', 0)}"
            ),
            holes_text=_format_holes(self.agent.context.get_data("holes") or []),
            todos_text=_format_todos(self.agent.context.get_data("todos") or []),
        )
        loop_data.extras_persistent["swiss_cheese"] = prompt
