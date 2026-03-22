from helpers.extension import Extension
from helpers.errors import HandledException

from usr.plugins.swiss_cheese.helpers import audit
from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import state as state_helper


class SwissCheeseGuard(Extension):
    async def execute(self, tool_name: str = "", tool_args: dict = {}, **kwargs):
        if not self.agent or self.agent.number != 0:
            return
        blocked, reason = audit.should_block_autonomous_tool(self.agent, tool_name)
        if not blocked:
            return
        plugin_config = swiss_config.get_plugin_config(self.agent)
        state_helper.record_near_miss(
            self.agent.context,
            {
                "title": "Autonomous followup blocked",
                "detail": f"SwissCheese blocked tool '{tool_name}' because {reason}.",
                "barrier": "Communicate",
                "severity": "medium",
                "confidence": 1.0,
                "fingerprint": str((self.agent.context.get_data("_swiss_cheese_autonomy_origin") or {}).get("fingerprint", "")),
            },
            plugin_config=plugin_config,
        )
        self.agent.context.set_data("_swiss_cheese_autonomy_origin", None)
        raise HandledException(Exception(f"SwissCheese blocked autonomous tool execution: {reason}"))
