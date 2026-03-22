# SwissCheese

SwissCheese is a conversation-level resilience harness for Agent Zero inspired by aviation mental models. It treats AI failures as system-plus-process problems and keeps `Aviate, Navigate, Communicate` inside a fuller barrier stack:

1. `Prepare`
2. `Aviate`
3. `Navigate`
4. `Communicate`
5. `Learn`

## Why This Exists

A $400 million aircraft has a context window too, it's called the flight
crew's working memory. Aviation solved the "humans forget things under
cognitive load" problem decades ago with checklists, annunciator panels,
and a culture that treats forgetting as a system failure, not a personal
one.

AI chats and coding sessions took a different approach: they gave you a 200k-token
context window with no gauges, no warnings, and an assistant that will
always agree it remembers everything while silently dropping your
requirements behind your back. Yes, you're absolutely right.

The author has ADHD. This means that somewhere around the 100k token mark, both
participants in the conversation, the human and the LLM, are operating
on vibes and pattern-matching, and neither one is going to be the adult in
the room. Projects drift. Code patterns contradict themselves. The model
starts solving problems it already solved, differently. It's the Swiss
cheese model of failure in real time, except nobody built the annunciator
panel.

Until now.

## Vocabulary

- `slice` or `barrier`: a defense layer
- `hole`: a current weakness in that layer
- `trajectory`: the path from user intent to a harmful or low-quality outcome
- `near_miss`: a trajectory trapped before damage
- `active_failure`: a visible model behavior in the current turn
- `latent_condition`: an upstream setup or process weakness that makes failure more likely

SwissCheese never relies on the metaphor being intuitive. The README, system prompt, tool outputs, and dashboard all use the same terms explicitly.

## What It Does

- injects SwissCheese doctrine into the main system prompt
- mirrors chat and utility-model context-window status into per-chat state
- hard-gates SwissCheese-generated autonomy until the chat model `ctx_length` has been explicitly confirmed
- audits every assistant turn with the utility model using structured JSON
- falls back to deterministic heuristics when the audit JSON is malformed
- maintains holes, todos, near misses, recovery budget, and scoped cross-chat permissions in persisted chat state
- keeps automatic recovery bounded, deduplicated, and idle-only

## Context Window Doctrine

- the active chat model `ctx_length` must be explicitly confirmed before SwissCheese can auto-continue on its own
- utility-model confirmation is tracked too, but missing confirmation only downgrades confidence
- if a configured context length exceeds `128000`, SwissCheese shows a best-practice advisory that the normal working envelope should stay at or below `100000` tokens unless the user deliberately overrides that guidance
- each confirmation stores a `(provider, model, ctx_length)` tuple in scoped plugin config, then live chats in that scope mirror the resolved confirmation into `ctx_confirmation`

## Cross-Chat Scope

Default orchestration is off.

- `same_project_live_write`: allow queueing bounded followups to other live chats in the same project
- `same_project_persisted_readonly`: allow inspection of persisted same-project chats without queueing into them
- `cross_project`: allow cross-project reads and live writes only when explicitly enabled

Persisted-only chats are always read-only until they are reopened.

## Tool Surface

SwissCheese exposes one multi-method tool file with:

- `swiss_cheese:status`
- `swiss_cheese:context_window`
- `swiss_cheese:todo_add`
- `swiss_cheese:todo_list`
- `swiss_cheese:todo_resolve`
- `swiss_cheese:todo_clear_completed`
- `swiss_cheese:inspect_chat`
- `swiss_cheese:queue_followup`

## Extension Points Used

- `system_prompt`
- `message_loop_prompts_after`
- `reasoning_stream_chunk`
- `response_stream_chunk`
- `response_stream_end`
- `tool_execute_before`
- `process_chain_end`
- `user_message_ui`

## Queue Design

SwissCheese keeps its own followup queue inside `swiss_cheese_state`. It only bridges one approved item into `helpers.message_queue` after the relevant chat is idle. This avoids the core `_50_process_queue.py` auto-drainer bypassing SwissCheese policy.
