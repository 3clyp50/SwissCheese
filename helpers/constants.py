from __future__ import annotations

PLUGIN_NAME = "swiss_cheese"
DISPLAY_TITLE = "SwissCheese"
PROJECT_STATE_FILENAME = "state.json"

CHAT_STATE_KEY = "swiss_cheese_state"
HOLES_KEY = "holes"
TODOS_KEY = "todos"
NEAR_MISSES_KEY = "near_misses"
AUDIT_STATUS_KEY = "audit_status"
RECOVERY_BUDGET_KEY = "recovery_budget"
CTX_CONFIRMATION_KEY = "ctx_confirmation"
CROSS_CHAT_SCOPE_KEY = "cross_chat_scope"

STATE_KEYS = (
    CHAT_STATE_KEY,
    HOLES_KEY,
    TODOS_KEY,
    NEAR_MISSES_KEY,
    AUDIT_STATUS_KEY,
    RECOVERY_BUDGET_KEY,
    CTX_CONFIRMATION_KEY,
    CROSS_CHAT_SCOPE_KEY,
)

TRANSIENT_REASONING_KEY = "_swiss_cheese_reasoning"
TRANSIENT_RESPONSE_KEY = "_swiss_cheese_response"
TRANSIENT_AUDIT_TASK_KEY = "_swiss_cheese_audit_task"
TRANSIENT_LAST_UTILITY_INPUT_KEY = "_swiss_cheese_last_utility_input"
TRANSIENT_AUTONOMY_ORIGIN_KEY = "_swiss_cheese_autonomy_origin"
TRANSIENT_LAST_USER_MESSAGE_KEY = "_swiss_cheese_last_user_message"
TRANSIENT_USER_TURN_SIGNAL_KEY = "_swiss_cheese_user_turn_signal"

BARRIERS = ("Readiness", "Stability", "Direction", "Coordination", "Learning")
DEFAULT_BARRIER = "Direction"
LEGACY_BARRIER_MAP = {
    "prepare": "Readiness",
    "readiness": "Readiness",
    "aviate": "Stability",
    "stability": "Stability",
    "navigate": "Direction",
    "direction": "Direction",
    "communicate": "Coordination",
    "coordination": "Coordination",
    "learn": "Learning",
    "learning": "Learning",
}
SEVERITIES = ("low", "medium", "high", "critical")
KINDS = ("active_failure", "latent_condition")
MODEL_SLOTS = ("chat_model", "utility_model")
NOTIFICATION_HISTORY_LIMIT = 40

ACTIVE_FAILURE_PATTERNS = (
    "sycophancy",
    "low_energy_effort",
    "gaming_fake_progress",
    "skipped_verification",
    "unsafe_tool_use",
    "premature_done",
)

LATENT_CONDITION_PATTERNS = (
    "wrong_ctx_limit",
    "excessive_context_occupancy",
    "missing_success_criteria",
    "stale_assumptions",
    "project_mismatch",
    "disabled_orchestration_scope",
    "chat_ctx_unconfirmed",
    "utility_ctx_unconfirmed",
)

DANGEROUS_AUTONOMOUS_PATTERNS = {"unsafe_tool_use"}


def normalize_barrier(value: object, default: str = DEFAULT_BARRIER) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return default
    return LEGACY_BARRIER_MAP.get(candidate.lower(), default)
