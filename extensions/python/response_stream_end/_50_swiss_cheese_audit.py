from helpers.extension import Extension
from agent import LoopData

from usr.plugins.swiss_cheese.helpers import audit


class SwissCheeseAudit(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent or self.agent.number != 0:
            return
        audit.schedule_background_audit(self.agent)
