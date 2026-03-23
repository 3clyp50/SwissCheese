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
  Prepare: "Setup, prerequisites, and readiness.",
  Aviate: "Keep the current turn stable and under control.",
  Navigate: "Direction, decisions, and next-step clarity.",
  Communicate: "Messages, handoffs, and queued followups.",
  Learn: "Lessons, patterns, and prevention for next time.",
};

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
    String(right.created_at || right.bridged_at || right.sent_at || "").localeCompare(
      String(left.created_at || left.bridged_at || left.sent_at || ""),
    ));
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
  targetKindFilter: "all",
  projectOnly: true,
  includePersisted: true,
  inspection: null,
  inspectionTargetKey: "",
  followup: {
    target_key: "",
    reason: "",
    message: "",
    auto_send: false,
  },

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

  get barrierCards() {
    const holes = this.chatState?.holes || [];
    return ["Prepare", "Aviate", "Navigate", "Communicate", "Learn"].map((barrier) => ({
      barrier,
      description: BARRIER_META[barrier] || "",
      holes: holes.filter((hole) => hole.barrier === barrier),
    }));
  },

  get filteredTargets() {
    if (this.targetKindFilter === "all") return this.targetCatalog || [];
    return (this.targetCatalog || []).filter((target) => target.kind === this.targetKindFilter);
  },

  get queueTargets() {
    return this.filteredTargets.filter((target) => target?.permissions?.can_queue);
  },

  get inspectableTargets() {
    return this.filteredTargets;
  },

  get projectChatSummaries() {
    return this.projectRollup?.chat_summaries || [];
  },

  get projectTotals() {
    return this.projectRollup?.totals || {};
  },

  get followupEntries() {
    return followupEntries(this.chatState);
  },

  get selectedFollowupTarget() {
    return (this.targetCatalog || []).find((target) => target.target_key === this.followup.target_key) || null;
  },

  targetLabel(target, options = {}) {
    return targetLabel(target, options);
  },

  setActiveView(view) {
    if (!view || !this.availableViews.includes(view)) return;
    this.activeView = view;
  },

  setTargetKindFilter(kind) {
    this.targetKindFilter = kind || "all";
    this.ensureSelections();
  },

  ensureSelections() {
    const inspectableKeys = new Set((this.targetCatalog || []).map((target) => target.target_key));
    const queueableKeys = new Set(
      (this.targetCatalog || []).filter((target) => target?.permissions?.can_queue).map((target) => target.target_key),
    );
    const fallbackTargetKey = this.currentTarget?.target_key || "";

    if (!queueableKeys.has(this.followup.target_key)) {
      this.followup.target_key = queueableKeys.has(fallbackTargetKey)
        ? fallbackTargetKey
        : [...queueableKeys][0] || fallbackTargetKey;
    }
    if (!inspectableKeys.has(this.inspectionTargetKey)) {
      this.inspectionTargetKey = inspectableKeys.has(fallbackTargetKey)
        ? fallbackTargetKey
        : [...inspectableKeys][0] || fallbackTargetKey;
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
      });
      this.targetCatalog = response?.targets || [];
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
    if (!this.contextId) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "inspect_target",
        context_id: this.contextId,
        target_key: this.inspectionTargetKey || "",
        project_only: !!this.projectOnly,
        include_persisted: !!this.includePersisted,
      });
      this.inspection = response?.inspection || null;
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to inspect target", TITLE);
    }
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

  async queueFollowup() {
    if (!this.contextId) return;
    if (!this.followup.reason.trim() || !this.followup.message.trim()) {
      void toastFrontendInfo("Reason and message are required for a followup.", TITLE);
      return;
    }
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "queue_followup",
        context_id: this.contextId,
        target_key: this.followup.target_key || this.currentTarget?.target_key || "",
        reason: this.followup.reason,
        message: this.followup.message,
        auto_send: !!this.followup.auto_send,
      });
      if (!response?.ok && !response?.queued) {
        void toastFrontendInfo(response?.result?.reason || "SwissCheese rejected the followup.", TITLE);
      } else {
        void toastFrontendSuccess("Followup queued", TITLE);
        this.followup = {
          target_key: this.currentTarget?.target_key || "",
          reason: "",
          message: "",
          auto_send: false,
        };
        await this.refresh();
      }
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to queue followup", TITLE);
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
