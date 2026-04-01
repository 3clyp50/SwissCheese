# Workflow Handoff: swiss-cheese

> **Session**: 2026-04-01T13:28:08Z
> **Agent**: Codex (GPT-5)
> **Handoff Reason**: checkpoint

---

## Ξ State Vector

**Objective**: Validate and tune SwissCheese so runtime truth, gate state, dedupe behavior, drift detection, and modal UX match real user-reported failures.

**Phase**: Runtime Validation + Logic Tuning | Progress: 82% | Status: paused

**Current Focus**:
This session implemented the first corrective pass across backend, prompts, UI, and tests. The code now exposes gate diagnostics, tracks transient duplicate/drift user-turn signals, semantically collapses some near-duplicate archive-validation work, and promotes high-priority followups in the modal.

The remaining work is evidence-driven validation, not broad new implementation. The next session should test against the provided `/usr` runtime bundle and UI behavior in the live app, then adjust heuristics and intent taxonomy from observed false positives/false negatives.

**Blocker**:
- Full Python test execution was not possible in the current environment because `pytest` is not installed.
- Resolution path: run in the normal Agent Zero/dev environment with `pytest` available, then validate the same scenarios manually in the UI using the provided runtime bundle.

---

## Δ Context Frame

### Decisions Log
| ID | Decision | Rationale | Reversible |
|----|----------|-----------|------------|
| D1 | Add exact tuple gate diagnostics and stale mirrored-confirmation detection | “Configured model” was not enough to explain why the gate stayed active; the live tuple vs confirmed tuple mismatch had to be explicit | yes |
| D2 | Add semantic `intent_key` dedupe for todos/followups | Literal text matching missed Plugin Zipping paraphrase spam and near-duplicate archive-validation work | yes |
| D3 | Add transient user-turn signal for `exact_repeat` and `drift_suspected` | The repeated-message and drift edge cases needed runtime evidence before audit synthesis, not just post-hoc interpretation | yes |
| D4 | Keep followups as the highest-priority operational surface in the modal | Pending/blocked followups were buried below backlog and near misses in screenshots, making the operator queue hard to see | yes |
| D5 | Treat runtime `/usr` bundle and screenshots as authoritative over repo assumptions | Persisted chat/project/plugin state is the only reliable source for proving live-vs-mirrored drift and scenario mapping | no |

### Constraints Active
- Runtime `/usr` bundle and screenshots are authoritative over assumptions.
- Source and installed mirror matched textually; runtime state drift matters more than packaging drift.
- `pytest` was unavailable here; only `python -m py_compile` succeeded.
- User-turn duplicate/drift detection is currently advisory/evidence-oriented, not a hard pre-generation stop.
- `usr/` remains untracked runtime evidence and should not be discarded.

### Patterns In Use
- **Confirmation Diagnostics Pipeline**: live model tuple -> confirmation diagnostics -> mirrored `ctx_confirmation` -> API payload -> UI explanation → See `helpers/context_window.py:82-289`
- **Semantic Intent Key**: normalize near-duplicate archive-validation and duplicate-query actions into shared intent classes → See `helpers/state.py:64-79`, `helpers/state.py:230-309`, `helpers/state.py:780-829`
- **User Turn Signal**: capture `exact_repeat`/`drift_suspected` at message ingress, then feed prompt context and audit heuristics → See `extensions/python/user_message_ui/_10_swiss_cheese_user_turn.py:45-102`, `extensions/python/message_loop_prompts_after/_55_swiss_cheese_state.py:30-117`, `helpers/audit.py:63-150`
- **Priority Followup Placement**: when followups are pending/blocked, render them before backlog/near-miss sections → See `webui/main.html:458-621`, `webui/swiss-cheese-store.js:82-108`, `webui/swiss-cheese-store.js:236-248`

### Mental Models Required
Gate lineage
: `_model_config` + confirmed tuples do not equal “confirmed”; the live tuple must exactly match a confirmed tuple, then `compute_context_window_status()` mirrors that into chat state and the UI.

Duplication lineage
: Repeated work can appear at multiple layers: repeated user input, repeated response, repeated todo, repeated followup, repeated project notification; each layer uses different data and dedupe rules.

Runtime truth vs mirrored state
: Persisted `chat.json` and project plugin state may lag until recomputation; screenshots can reflect a newer live state than archived runtime files.

Reach vs drift
: Discovery/reachability is permission/project-based; drift is semantic/topic-based and is not solved by target visibility alone.

---

## Φ Code Map

### Modified This Session
| File | Lines | Change Summary |
|------|-------|----------------|
| `api/swiss_cheese.py` | 63-131 | Exposed `gate_diagnostics`, `followup_diagnostics`, and `drift_diagnostics` from the state payload |
| `extensions/python/message_loop_prompts_after/_55_swiss_cheese_state.py` | 30-117 | Injected duplicate/drift summaries and confirmation reasons into the SwissCheese prompt context |
| `extensions/python/user_message_ui/_10_swiss_cheese_user_turn.py` | 5-102 | Added transient per-turn signal generation for exact-repeat and drift suspicion |
| `helpers/audit.py` | 63-150, 254-339 | Fed turn signal and gate diagnostics into audit payloads and heuristic holes |
| `helpers/constants.py` | 27-32 | Added transient keys for last user message and user-turn signal |
| `helpers/context_window.py` | 53-289 | Added confirmation diagnostics, stale mirrored snapshot detection, gate/utility warning explanations, richer mirrored output |
| `helpers/state.py` | 64-79, 123-161, 230-309, 780-829 | Added semantic `intent_key` inference and used it in todo/followup dedupe/fingerprints |
| `prompts/agent.context.swiss_cheese.md` | 8-9 | Added duplicate-user and drift signal prompt slots |
| `tests/test_swiss_cheese_plugin.py` | 384-453, 891-1030, 1423-1560 | Expanded regressions for stale gate state, repeated work, semantic followup/todo dedupe, optional diagnostics |
| `webui/main.html` | 20-28, 95-172, 291-408, 458-621, 734-1089 | Compact summary toolbars, gate explanation row, promoted followups, project target dropdown, accordion detail |
| `webui/swiss-cheese-store.js` | 82-108, 236-248 | Prioritized/sorted followups and exposed `hasPriorityFollowups` + project target options |

### Reference Anchors
| File | Lines | Relevance |
|------|-------|-----------|
| `helpers/config.py` | 243-377 | Model config scope resolution, confirmed tuple append, and live-scope sync are the root of gate lineage |
| `helpers/discovery.py` | 458-715 | Target catalog, target inspection, and project rollup define reachability and project view behavior |
| `helpers/project_state.py` | 55-153 | Project backlog persistence and notification-history behavior |
| `extensions/python/tool_execute_before/_60_swiss_cheese_guard.py` | 1-25 | Guard consumes mirrored gate state and blocks autonomous tool flow |
| `usr/chats/M0IcqC0U/chat.json` | runtime artifact | Drift/laptop-buying scenario inside same-project Helios context |
| `usr/chats/xL1yiPWr/chat.json` | runtime artifact | Repeated-message / overlapping-work scenario |
| `usr/chats/XXmv3e1p/chat.json` | runtime artifact | Plugin Zipping followup/backlog spam scenario |
| `usr/projects/project_1/.a0proj/plugins/swiss_cheese/state.json` | runtime artifact | Project notification history and project-level persisted state |
| `usr/plugins/_model_config/config.json` | runtime artifact | Live model-config scope used by the gate lineage |
| `usr/plugins/swiss_cheese/config.json` | runtime artifact | Confirmed tuple registry and plugin config scope |

### Entry Points
- **Primary**: `api/swiss_cheese.py:63` — assembles the full SwissCheese state payload consumed by the modal
- **User-Turn Hook**: `extensions/python/user_message_ui/_10_swiss_cheese_user_turn.py:85` — first place duplicate/drift signals are captured
- **Test Suite**: `tests/test_swiss_cheese_plugin.py` — covers gate invalidation, repeated-work followup suppression, semantic dedupe, optional diagnostics

---

## Ψ Knowledge Prerequisites

### Documentation Sections
- [ ] `prompts/agent.context.swiss_cheese.md` § signal fields — confirm how duplicate/drift/gate explanations reach the model

### Modules to Explore
- [ ] `helpers/config.py` — understand scope resolution and why confirmed tuples can drift from mirrored chat state
- [ ] `helpers/discovery.py` — understand why reachability is not the same as semantic drift detection
- [ ] `helpers/project_state.py` — understand project backlog and notification persistence
- [ ] `helpers/state.py` — understand where semantic dedupe now happens and where it still does not

### External References *(optional)*
- Attached UI screenshots from the prior session: use them as UX evidence when comparing live modal behavior to persisted runtime state

---

## Ω Forward Vector

### Next Actions *(priority order)*
1. **Run**: scenario-driven manual regression against the provided runtime bundle and live modal → `api/swiss_cheese.py:63`, `webui/main.html:95-621`
2. **Execute**: `tests/test_swiss_cheese_plugin.py` in an environment with `pytest` available → `tests/test_swiss_cheese_plugin.py:384-1560`
3. **Tune**: duplicate/drift thresholds and `intent_key` taxonomy from observed false positives/false negatives → `extensions/python/user_message_ui/_10_swiss_cheese_user_turn.py:45-80`, `helpers/state.py:64-79`
4. **Decide**: whether repeated-request handling should remain prompt/audit-guided or become a hard pre-response intervention → `extensions/python/user_message_ui/_10_swiss_cheese_user_turn.py:85-102`, `helpers/audit.py:317-339`

### Open Questions
- [ ] Are the current `drift_suspected` thresholds (`previous_overlap < 0.12`, `anchor_overlap < 0.18`) too aggressive or too weak on real chats?
- [ ] Should archive-validation semantic dedupe remain broad, or should post-condition validation be split into multiple preserved sub-intents?
- [ ] Should utility-model mismatch remain advisory only, or should it become a stronger operator-visible warning when the main gate is open?
- [ ] Should repeated-user-message handling rewrite/short-circuit the outgoing message before model generation, instead of only shaping prompt context and audit evidence?

### Success Criteria
- [ ] `M0IcqC0U` produces a clearer drift signal than before, or the heuristic gap is precisely identified from runtime evidence
- [ ] `xL1yiPWr` no longer encourages overlapping repeated work without referencing the prior answer
- [ ] `XXmv3e1p` shows materially less near-duplicate backlog/followup spam in both persisted state and modal rendering
- [ ] Gate UI shows exact cause/explanation when user believes contexts are set, and stale mirrored gate state rewrites correctly through `get_state`
- [ ] Full `pytest` run passes in the normal development environment

### Hazards / Watch Points
- ⚠️ Current repeated-message handling is still soft guidance; it may reduce but not completely prevent duplicate model output until a harder interception path is implemented
- ⚠️ Semantic dedupe may collapse actions that advanced users sometimes want separated; verify carefully on Plugin Zipping before broadening the taxonomy
- ⚠️ Persisted `chat.json` can lag behind live modal state; always compare API payload + runtime artifact + screenshot/live UI before concluding behavior

---

## Glossary *(session-specific terms)*
| Term | Definition |
|------|------------|
| Gate lineage | The path from model config + confirmed tuples to `compute_context_window_status()` to mirrored `ctx_confirmation` to guard/UI behavior |
| Intent key | A normalized semantic label used to collapse paraphrased todos/followups that represent the same operational action |
| User-turn signal | Transient metadata derived from the new user message, currently used for exact-repeat and drift suspicion |
| Runtime truth | The `/usr` bundle and screenshots, treated as more authoritative than repo assumptions or stale mirrors |
