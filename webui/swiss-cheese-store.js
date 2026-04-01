import { createStore } from "/js/AlpineStore.js";
import * as api from "/js/api.js";
import { openModal } from "/js/modals.js";
import { getContext } from "/index.js";
import { store as chatsStore } from "/components/sidebar/chats/chats-store.js";
import {
  toastFrontendError,
  toastFrontendSuccess,
  toastFrontendInfo,
} from "/components/notifications/notification-store.js";

const TITLE = "SwissCheese";
const ENDPOINT = "/plugins/swiss_cheese/swiss_cheese";
const BARRIER_META = {
  Readiness: "Setup, prerequisites, and operating readiness.",
  Stability: "Execution stability and control in the current turn.",
  Direction: "Direction, decision quality, and next-step clarity.",
  Coordination: "Handoffs, followups, and cross-context alignment.",
  Learning: "Retained lessons, patterns, and prevention work.",
};
const LEGACY_BARRIERS = {
  Prepare: "Readiness",
  Aviate: "Stability",
  Navigate: "Direction",
  Communicate: "Coordination",
  Learn: "Learning",
  Readiness: "Readiness",
  Stability: "Stability",
  Direction: "Direction",
  Coordination: "Coordination",
  Learning: "Learning",
};
const SEVERITY_RANK = {
  low: 0,
  medium: 1,
  high: 2,
  critical: 3,
};
const CHAT_MAP_POSITIONS = [
  { x: 50, y: 7 },
  { x: 84, y: 28 },
  { x: 78, y: 72 },
  { x: 22, y: 72 },
  { x: 16, y: 28 },
];

function normalizeBarrier(barrier) {
  return LEGACY_BARRIERS[barrier] || "Direction";
}

function barrierList() {
  return ["Readiness", "Stability", "Direction", "Coordination", "Learning"];
}

function severityRank(severity) {
  return SEVERITY_RANK[String(severity || "low").toLowerCase()] ?? 0;
}

function maxSeverity(items) {
  const winner = (items || []).reduce((best, item) =>
    severityRank(item?.severity) > severityRank(best) ? String(item?.severity || "low").toLowerCase() : best,
  "low");
  return items?.length ? winner : "";
}

function targetLabel(target, { includeQueueability = false } = {}) {
  if (!target) return "Current target";
  const parts = [target.name || target.target_key || target.id];
  parts.push(target.kind === "task" ? "task" : "chat");
  if (target?.scheduler?.type) parts.push(target.scheduler.type);
  if (target?.scheduler?.state) parts.push(target.scheduler.state);
  if (target.kind === "task") {
    parts.push(target?.scheduler?.dedicated_context ? "dedicated context" : "shared context");
  }
  if (target.project_title) parts.push(target.project_title);
  if (target.persisted_only && target.kind !== "task") parts.push("read-only");
  else if (!target.live) parts.push("offline");
  if (includeQueueability && !target?.permissions?.can_queue) parts.push("not queueable");
  return parts.join(" | ");
}

function followupEntries(chatState) {
  const swiss = chatState?.swiss_cheese_state || {};
  const pending = (swiss.followup_queue || []).map((item) => ({ ...item, entry_source: "pending" }));
  const history = (swiss.followup_history || []).map((item) => ({ ...item, entry_source: "history" }));
  return [...pending, ...history].sort((left, right) =>
    String(right.sent_at || right.blocked_at || right.bridged_at || right.created_at || "").localeCompare(
      String(left.sent_at || left.blocked_at || left.bridged_at || left.created_at || ""),
    ));
}

function summarizeSeverity(items) {
  const severity = maxSeverity(items);
  return severity ? severity.toUpperCase() : "CLEAR";
}

export const store = createStore("swissCheese", {
  loading: false,
  contextId: "",
  chatState: null,
  projectState: null,
  projectRollup: null,
  contextWindow: null,
  scope: {},
  currentTarget: null,
  availableViews: ["chat"],
  activeView: "chat",
  targetCatalog: [],
  targetCatalogCounts: { all: 0, chat: 0, task: 0 },
  targetKindFilter: "all",
  projectOnly: true,
  includePersisted: true,
  inspection: null,
  inspectionTargetKey: "",
  projectMapTargetKey: "",
  selectedBarrierKey: "",
  previewBarrierKey: "",

  async openModal() {
    await openModal("/plugins/swiss_cheese/webui/main.html");
  },

  init() {},

  async onOpen() {
    await this.refresh();
  },

  get hasProjectView() {
    return Array.isArray(this.availableViews) && this.availableViews.includes("project");
  },

  get isChatView() {
    return this.activeView === "chat";
  },

  get isProjectView() {
    return this.activeView === "project";
  },

  get hasTaskTargets() {
    return (this.targetCatalogCounts?.task || 0) > 0;
  },

  get barrierCards() {
    const holes = (this.chatState?.holes || []).map((hole) => ({
      ...hole,
      barrier: normalizeBarrier(hole?.barrier),
      severity: String(hole?.severity || "low").toLowerCase(),
    }));
    return barrierList().map((barrier) => {
      const items = holes.filter((hole) => hole.barrier === barrier);
      return {
        barrier,
        description: BARRIER_META[barrier] || "",
        holes: items,
        issueCount: items.length,
        severity: maxSeverity(items),
      };
    });
  },

  get issues() {
    return [...(this.chatState?.holes || [])]
      .map((hole) => ({
        ...hole,
        barrier: normalizeBarrier(hole?.barrier),
        severity: String(hole?.severity || "low").toLowerCase(),
      }))
      .sort((left, right) => severityRank(right.severity) - severityRank(left.severity));
  },

  get activeBarrierCard() {
    const key = this.previewBarrierKey || this.selectedBarrierKey;
    if (key) return this.barrierCards.find((card) => card.barrier === key) || null;
    return this.barrierCards.find((card) => card.issueCount > 0) || this.barrierCards[0] || null;
  },

  get chatMapNodes() {
    return this.barrierCards.map((card, index) => {
      const position = CHAT_MAP_POSITIONS[index] || { x: 50, y: 50 };
      return {
        ...card,
        x: position.x,
        y: position.y,
        badge: card.issueCount,
        severityLabel: summarizeSeverity(card.holes),
        style: `left:${position.x}%; top:${position.y}%;`,
      };
    });
  },

  get chatCenterLabel() {
    return this.currentTarget?.name || this.chatState?.context_name || "Active chat";
  },

  get filteredTargets() {
    return this.targetCatalog || [];
  },

  get inspectableTargets() {
    return this.filteredTargets;
  },

  get actionableTargets() {
    return this.filteredTargets.filter((target) => target?.permissions?.can_queue);
  },

  get readOnlyTargets() {
    return this.filteredTargets.filter((target) => !target?.permissions?.can_queue);
  },

  get followupEntries() {
    return followupEntries(this.chatState);
  },

  get projectTargets() {
    return this.filteredTargets;
  },

  get selectedProjectTarget() {
    return this.projectTargets.find((target) => target.target_key === (this.projectMapTargetKey || this.inspectionTargetKey)) || null;
  },

  get projectGraphNodes() {
    const targets = this.projectTargets || [];
    const count = Math.max(targets.length, 1);
    return targets.map((target, index) => {
      const angle = ((Math.PI * 2) / count) * index - (Math.PI / 2);
      const radius = count <= 4 ? 30 : 36;
      const x = 50 + (Math.cos(angle) * radius);
      const y = 50 + (Math.sin(angle) * radius);
      const severity = deriveTargetSeverity(target);
      return {
        ...target,
        x,
        y,
        issueCount: intValue(target?.state_excerpt?.hole_count),
        todoCount: intValue(target?.state_excerpt?.open_todo_count),
        nearMissCount: intValue(target?.state_excerpt?.near_miss_count),
        followupCount: intValue(target?.state_excerpt?.queue_count || target?.state_excerpt?.followup_queue_count),
        severity,
        severityLabel: summarizeSeverity([{ severity }].filter((item) => item.severity)),
      };
    });
  },

  targetLabel(target, options = {}) {
    return targetLabel(target, options);
  },

  targetSeverity(target) {
    return deriveTargetSeverity(target);
  },

  setBarrierSelection(barrier) {
    this.selectedBarrierKey = barrier || "";
  },

  previewBarrier(barrier) {
    this.previewBarrierKey = barrier || "";
  },

  clearBarrierPreview() {
    this.previewBarrierKey = "";
  },

  async setActiveView(view) {
    if (!view || !this.availableViews.includes(view)) return;
    this.activeView = view;
    if (this.isChatView && this.targetKindFilter !== "all") {
      this.targetKindFilter = "all";
      await this.updateCatalogFilters();
      return;
    }
    if (this.isProjectView) {
      await this.updateCatalogFilters();
    }
  },

  targetFilterDisabled(kind) {
    if (this.isChatView) return true;
    if (kind === "task") return !this.hasTaskTargets;
    return false;
  },

  async setTargetKindFilter(kind) {
    if (this.targetFilterDisabled(kind)) return;
    this.targetKindFilter = kind || "all";
    await this.updateCatalogFilters();
  },

  ensureSelections() {
    const inspectableKeys = new Set((this.targetCatalog || []).map((target) => target.target_key));
    const fallbackTargetKey = this.currentTarget?.target_key || [...inspectableKeys][0] || "";

    if (!inspectableKeys.has(this.inspectionTargetKey)) {
      this.inspectionTargetKey = inspectableKeys.has(fallbackTargetKey)
        ? fallbackTargetKey
        : [...inspectableKeys][0] || "";
    }
    if (!inspectableKeys.has(this.projectMapTargetKey)) {
      this.projectMapTargetKey = this.inspectionTargetKey || fallbackTargetKey;
    }
  },

  async refresh() {
    const contextId = chatsStore.selected || getContext();
    if (!contextId) {
      this.contextId = "";
      this.chatState = null;
      this.projectState = null;
      this.projectRollup = null;
      this.contextWindow = null;
      this.targetCatalog = [];
      this.targetCatalogCounts = { all: 0, chat: 0, task: 0 };
      this.inspection = null;
      this.currentTarget = null;
      return;
    }

    this.loading = true;
    this.contextId = contextId;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "get_state",
        context_id: contextId,
      });
      if (!response?.ok) throw new Error("SwissCheese state request failed");

      this.chatState = response.chat_state || response.state || null;
      this.projectState = response.project_state || null;
      this.projectRollup = response.project_rollup || null;
      this.contextWindow = response.context_window || null;
      this.scope = response.scope || {};
      this.availableViews = response.available_views || ["chat"];
      this.currentTarget = response.current_target || null;

      if (!this.availableViews.includes(this.activeView)) {
        this.activeView = response.default_view || "chat";
      }
      if (!this.projectState) this.activeView = "chat";

      if (!this.targetCatalog.length) {
        this.projectOnly = !!response?.catalog_defaults?.project_only;
        this.includePersisted = !!response?.catalog_defaults?.include_persisted;
        this.targetKindFilter = response?.catalog_defaults?.kind || "all";
      }

      if (!this.selectedBarrierKey) {
        this.selectedBarrierKey = (this.barrierCards.find((card) => card.issueCount > 0) || this.barrierCards[0] || {}).barrier || "";
      }

      await this.refreshCatalog();
      await this.inspectTarget();
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to load SwissCheese state", TITLE);
    } finally {
      this.loading = false;
    }
  },

  async refreshCatalog() {
    if (!this.contextId) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "list_targets",
        context_id: this.contextId,
        project_only: !!this.projectOnly,
        include_persisted: !!this.includePersisted,
        kind: this.targetKindFilter,
      });
      this.targetCatalog = response?.targets || [];
      this.targetCatalogCounts = response?.counts || { all: this.targetCatalog.length, chat: 0, task: 0 };
      if (this.isProjectView && this.targetKindFilter === "task" && !this.hasTaskTargets) {
        this.targetKindFilter = "all";
        await this.refreshCatalog();
        return;
      }
      this.ensureSelections();
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to load targets", TITLE);
    }
  },

  async updateCatalogFilters() {
    await this.refreshCatalog();
    await this.inspectTarget();
  },

  async inspectTarget() {
    if (!this.contextId || !this.inspectionTargetKey) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "inspect_target",
        context_id: this.contextId,
        target_key: this.inspectionTargetKey || "",
        project_only: !!this.projectOnly,
        include_persisted: !!this.includePersisted,
        kind: this.targetKindFilter,
      });
      this.inspection = response?.inspection || null;
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to inspect target", TITLE);
    }
  },

  async selectProjectTarget(targetKey) {
    this.projectMapTargetKey = targetKey || "";
    this.inspectionTargetKey = targetKey || "";
    await this.inspectTarget();
  },

  async resolveTodo(todoId, scope = "chat") {
    if (!this.contextId) return;
    try {
      await api.callJsonApi(ENDPOINT, {
        action: "todo_resolve",
        context_id: this.contextId,
        todo_id: todoId,
        scope,
      });
      await this.refresh();
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to resolve todo", TITLE);
    }
  },

  async clearCompletedTodos(scope = "chat") {
    if (!this.contextId) return;
    try {
      await api.callJsonApi(ENDPOINT, {
        action: "todo_clear_completed",
        context_id: this.contextId,
        scope,
      });
      await this.refresh();
      void toastFrontendSuccess("Completed todos cleared", TITLE);
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to clear completed todos", TITLE);
    }
  },

  async retryFollowup(fingerprint) {
    if (!this.contextId) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "retry_followup",
        context_id: this.contextId,
        fingerprint,
      });
      if (!response?.queued) {
        void toastFrontendInfo(response?.result?.reason || "Followup could not be retried.", TITLE);
        return;
      }
      await this.refresh();
      void toastFrontendSuccess("Followup retried", TITLE);
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to retry followup", TITLE);
    }
  },

  async removeFollowup(fingerprint) {
    if (!this.contextId) return;
    try {
      await api.callJsonApi(ENDPOINT, {
        action: "remove_followup",
        context_id: this.contextId,
        fingerprint,
      });
      await this.refresh();
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to remove followup", TITLE);
    }
  },

  async bridgeFollowup(fingerprint, sendNow = false) {
    if (!this.contextId) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "bridge_followup",
        context_id: this.contextId,
        fingerprint,
        send_now: !!sendNow,
      });
      await this.refresh();
      const state = response?.result?.delivery_state || response?.result?.status || "";
      if (state === "queued_in_target_queue") {
        void toastFrontendSuccess("Followup bridged to target queue", TITLE);
      } else if (state === "sent") {
        void toastFrontendSuccess("Followup sent", TITLE);
      } else if (state === "blocked") {
        void toastFrontendInfo(response?.result?.reason || "Followup is blocked.", TITLE);
      } else {
        void toastFrontendInfo("No followup was ready.", TITLE);
      }
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to deliver followup", TITLE);
    }
  },
});

function intValue(value) {
  return Number.parseInt(value || "0", 10) || 0;
}

function deriveTargetSeverity(target) {
  const holes = intValue(target?.state_excerpt?.hole_count);
  const nearMisses = intValue(target?.state_excerpt?.near_miss_count);
  if (holes > 0) return "high";
  if (nearMisses > 0) return "medium";
  return "";
}
