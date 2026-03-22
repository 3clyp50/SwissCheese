import asyncio

from helpers.extension import Extension
from agent import LoopData

from usr.plugins.swiss_cheese.helpers import config as swiss_config
from usr.plugins.swiss_cheese.helpers import state as state_helper


class SwissCheeseProcessQueue(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent or self.agent.number != 0:
            return
        asyncio.create_task(self._bridge_when_idle(self.agent.context))

    async def _bridge_when_idle(self, context):
        plugin_config = swiss_config.get_plugin_config(context.get_agent())
        total_wait = 0.0
        while total_wait < 30.0:
            audit_task = context.get_agent().get_data("_swiss_cheese_audit_task")
            audit_running = bool(audit_task and not audit_task.done())
            if not context.is_running() and not audit_running:
                break
            await asyncio.sleep(0.1)
            total_wait += 0.1
        if not context.is_running():
            state_helper.bridge_next_followup(context, plugin_config=plugin_config, manual=False)
