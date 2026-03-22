from helpers.extension import Extension
from agent import LoopData

from usr.plugins.swiss_cheese.helpers import config as swiss_config


class SwissCheesePrompt(Extension):
    async def execute(self, system_prompt: list[str] = [], loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent or self.agent.number != 0:
            return
        plugin_config = swiss_config.get_plugin_config(self.agent)
        prompt = self.agent.read_prompt(
            "agent.system.swiss_cheese.md",
            preferred_working_limit=str(plugin_config.get("preferred_working_limit", 100000)),
            advisory_threshold=str(plugin_config.get("advisory_threshold", 128000)),
        )
        system_prompt.append(prompt)
