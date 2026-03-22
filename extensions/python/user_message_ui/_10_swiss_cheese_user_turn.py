from helpers.extension import Extension

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import state as state_helper


class SwissCheeseUserTurn(Extension):
    async def execute(self, data: dict = {}, **kwargs):
        if not self.agent or self.agent.number != 0:
            return
        plugin_config = swiss_config.get_plugin_config(self.agent)
        state_helper.bump_user_turn(self.agent.context, plugin_config=plugin_config)
        self.agent.set_data("_swiss_cheese_reasoning", "")
        self.agent.set_data("_swiss_cheese_response", "")
        running_task = self.agent.get_data("_swiss_cheese_audit_task")
        if running_task and not running_task.done():
            running_task.cancel()
