from helpers.extension import Extension
from agent import LoopData

from usr.plugins.swiss_cheese.helpers import audit


class SwissCheeseCollectReasoning(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), stream_data=None, **kwargs):
        if not self.agent or self.agent.number != 0 or stream_data is None:
            return
        audit.collect_reasoning(self.agent, str(stream_data.get("full", "") or ""))
