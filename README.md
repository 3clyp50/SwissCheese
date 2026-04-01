# SwissCheese

SwissCheese is a conversation-level resilience harness for Agent Zero. It treats AI failures as system-plus-process problems and organizes defenses around five operating barriers:

1. `Readiness`
2. `Stability`
3. `Direction`
4. `Coordination`
5. `Learning`

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
- maintains holes, todos, near misses, recovery budget, and scoped cross-target permissions in persisted chat state
- unifies ordinary chats and scheduler task contexts into one target catalog
- keeps SwissCheese as the approval layer while using the native `message_queue` as the delivery layer after bridge
- keeps automatic recovery bounded, deduplicated, and idle-only

## Context Window Doctrine

- the active chat model `ctx_length` must be explicitly confirmed before SwissCheese can auto-continue on its own
- utility-model confirmation is tracked too, but missing confirmation only downgrades confidence
- if a configured context length exceeds `128000`, SwissCheese shows a best-practice advisory that the normal working envelope should stay at or below `100000` tokens unless the user deliberately overrides that guidance
- each confirmation stores a `(provider, model, ctx_length)` tuple in scoped plugin config, then live chats in that scope mirror the resolved confirmation into `ctx_confirmation`

## Config Scope

- SwissCheese settings resolve only at `global` or `project` scope
- global changes refresh all live chats
- project changes refresh all live chats in that project
- legacy profile-scoped SwissCheese configs are absorbed into the surviving global or project scope when the user saves or confirms context

## Cross-Chat Scope

Default orchestration is off.

- `same_project_live_write`: allow queueing bounded followups to same-project live chats and same-project task targets
- `same_project_persisted_readonly`: allow inspection of persisted same-project chats and task targets
- `cross_project`: allow cross-project reads and writes only when explicitly enabled

Persisted-only non-task chats stay read-only. Persisted task targets can still be queueable when scope allows and the task context is resolvable.

## Tool Surface

SwissCheese exposes one multi-method tool file with:

- `swiss_cheese:status`
- `swiss_cheese:context_window`
- `swiss_cheese:target_catalog`
- `swiss_cheese:todo_add`
- `swiss_cheese:todo_list`
- `swiss_cheese:todo_resolve`
- `swiss_cheese:todo_clear_completed`
- `swiss_cheese:inspect_target`
- `swiss_cheese:bridge_followup`
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

SwissCheese keeps its own followup queue inside `swiss_cheese_state` as the approval and policy layer. Once a followup is approved, SwissCheese bridges it into the target context's native `helpers.message_queue`.

- `auto_send: false`: bridge into the target queue and stop there
- `auto_send: true`: bridge first, then send that exact native queue item
- scheduler task targets are queue-backed contexts, not "rerun the task definition" jobs
