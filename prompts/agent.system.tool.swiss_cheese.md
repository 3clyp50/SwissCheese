### swiss_cheese
conversation resilience situational awareness todo hygiene bounded followups
always send explicit non-empty tool_args for swiss_cheese tools
never send an empty object for swiss_cheese tool_args
when targeting another chat, prefer exact `target_context_id` values from `swiss_cheese:chat_catalog`
use `scope: "project"` for shared project backlog actions and `scope: "chat"` for chat-local actions

#### swiss_cheese:status
full swisscheese state snapshot for current chat
args detail
- detail: full or summary
usage:
~~~json
{
    "thoughts": [
        "I need the full SwissCheese state before deciding the next step."
    ],
    "headline": "Checking SwissCheese state",
    "tool_name": "swiss_cheese:status",
    "tool_args": {
        "detail": "full"
    }
}
~~~

#### swiss_cheese:context_window
context window status for current chat
args slot
- slot: all chat utility
usage:
~~~json
{
    "thoughts": [
        "I should inspect the active chat model budget before continuing autonomously."
    ],
    "headline": "Inspecting context window",
    "tool_name": "swiss_cheese:context_window",
    "tool_args": {
        "slot": "all"
    }
}
~~~

#### swiss_cheese:chat_catalog
list discoverable chat targets with exact ids and permissions
args project_only include_persisted
- project_only: true or false
- include_persisted: true or false
usage:
~~~json
{
    "thoughts": [
        "I should list the exact chat catalog before queueing a same-project followup."
    ],
    "headline": "Listing chat catalog",
    "tool_name": "swiss_cheese:chat_catalog",
    "tool_args": {
        "project_only": true,
        "include_persisted": true
    }
}
~~~

#### swiss_cheese:todo_add
add or update a swisscheese todo
args title detail severity scope
usage:
~~~json
{
    "thoughts": [
        "I need to capture the corrective action as a SwissCheese todo."
    ],
    "headline": "Adding SwissCheese todo",
    "tool_name": "swiss_cheese:todo_add",
    "tool_args": {
        "title": "Verify tool request schema",
        "detail": "Ensure the next tool call uses the canonical SwissCheese argument shape.",
        "severity": "high",
        "scope": "chat"
    }
}
~~~

#### swiss_cheese:todo_list
list swisscheese todos for current chat or shared project backlog
args status scope
- status: open completed all
- scope: chat project
usage:
~~~json
{
    "thoughts": [
        "The user asked for the SwissCheese todo list, so I should list open todos explicitly."
    ],
    "headline": "Listing SwissCheese todos",
    "tool_name": "swiss_cheese:todo_list",
    "tool_args": {
        "status": "open",
        "scope": "project"
    }
}
~~~

#### swiss_cheese:todo_resolve
mark a swisscheese todo completed
args todo_id scope
usage:
~~~json
{
    "thoughts": [
        "The corrective step is done, so I should resolve that todo."
    ],
    "headline": "Resolving SwissCheese todo",
    "tool_name": "swiss_cheese:todo_resolve",
    "tool_args": {
        "todo_id": "abc123def456",
        "scope": "chat"
    }
}
~~~

#### swiss_cheese:todo_clear_completed
remove completed swisscheese todos
args confirm scope
- confirm: must be true
usage:
~~~json
{
    "thoughts": [
        "Completed SwissCheese todos are cluttering the state, so I should clear them explicitly."
    ],
    "headline": "Clearing completed SwissCheese todos",
    "tool_name": "swiss_cheese:todo_clear_completed",
    "tool_args": {
        "confirm": true,
        "scope": "project"
    }
}
~~~

#### swiss_cheese:inspect_chat
inspect a chat target within swisscheese scope
args selector target_context_id
usage:
~~~json
{
    "thoughts": [
        "I should inspect the target chat before queuing a bounded followup."
    ],
    "headline": "Inspecting target chat",
    "tool_name": "swiss_cheese:inspect_chat",
    "tool_args": {
        "target_context_id": "ctx_beta_1234"
    }
}
~~~

#### swiss_cheese:queue_followup
queue a bounded followup for current or selected chat
args selector target_context_id reason message auto_send
usage:
~~~json
{
    "thoughts": [
        "A bounded followup is appropriate and should be queued with an explicit reason."
    ],
    "headline": "Queueing SwissCheese followup",
    "tool_name": "swiss_cheese:queue_followup",
    "tool_args": {
        "target_context_id": "ctx_beta_1234",
        "reason": "schema_check",
        "message": "Use the canonical SwissCheese tool arguments and continue with the user task.",
        "auto_send": false
    }
}
~~~
