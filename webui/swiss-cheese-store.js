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
  if (!target) return "Current chat";
  const parts = [target.name || target.id];
  if (target.project_title) parts.push(target.project_title);
  if (target.persisted_only) parts.push("read-only");
  else if (!target.live) parts.push("offline");
  if (includeQueueability && !target?.permissions?.can_queue) parts.push("not queueable");
  return parts.join(" | ");
}

export const store = createStore("swissCheese", {
  loading: false,
  contextId: "",
  chatState: null,
  projectState: null,
  projectRollup: null,
  contextWindow: null,
  scope: {},
  availableViews: ["chat"],
  activeView: "chat",
  targetCatalog: [],
  projectOnly: true,
  includePersisted: true,
  inspection: null,
  inspectionTargetId: "",
  followup: {
    target_context_id: "",
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
    const barriers = ["Prepare", "Aviate", "Navigate", "Communicate", "Learn"];
    const holes = this.chatState?.holes || [];
    return barriers.map((barrier) => ({
      barrier,
      description: BARRIER_META[barrier] || "",
      holes: holes.filter((hole) => hole.barrier === barrier),
    }));
  },

  get queueTargets() {
    return (this.targetCatalog || []).filter((target) => target?.permissions?.can_queue);
  },

  get inspectableTargets() {
    return this.targetCatalog || [];
  },

  get projectChatSummaries() {
    return this.projectRollup?.chat_summaries || [];
  },

  get projectTotals() {
    return this.projectRollup?.totals || {};
  },

  targetLabel(target, options = {}) {
    return targetLabel(target, options);
  },

  setActiveView(view) {
    if (!view || !this.availableViews.includes(view)) return;
    this.activeView = view;
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
      return;
    }

    this.loading = true;
    this.contextId = contextId;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "get_state",
        context_id: contextId,
      });
      if (!response?.ok) {
        throw new Error("SwissCheese state request failed");
      }

      this.chatState = response.chat_state || response.state || null;
      this.projectState = response.project_state || null;
      this.projectRollup = response.project_rollup || null;
      this.contextWindow = response.context_window || null;
      this.scope = response.scope || {};
      this.availableViews = response.available_views || ["chat"];

      if (!this.availableViews.includes(this.activeView)) {
        this.activeView = response.default_view || "chat";
      }
      if (!this.projectState) {
        this.activeView = "chat";
      }

      if (!this.targetCatalog.length) {
        this.projectOnly = !!response?.catalog_defaults?.project_only;
        this.includePersisted = !!response?.catalog_defaults?.include_persisted;
      }
      await this.refreshCatalog();
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
        action: "list_chat_targets",
        context_id: this.contextId,
        project_only: !!this.projectOnly,
        include_persisted: !!this.includePersisted,
      });
      this.targetCatalog = response?.targets || [];
      const queueableIds = new Set(this.queueTargets.map((target) => target.id));
      const inspectableIds = new Set(this.inspectableTargets.map((target) => target.id));

      if (!queueableIds.has(this.followup.target_context_id)) {
        this.followup.target_context_id = this.contextId;
      }
      if (!inspectableIds.has(this.inspectionTargetId)) {
        this.inspectionTargetId = this.contextId;
      }
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to load chat targets", TITLE);
    }
  },

  async updateCatalogFilters() {
    await this.refreshCatalog();
    await this.inspectChat();
  },

  async inspectChat() {
    if (!this.contextId) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "inspect_chat",
        context_id: this.contextId,
        target_context_id: this.inspectionTargetId || "",
        project_only: !!this.projectOnly,
        include_persisted: !!this.includePersisted,
      });
      this.inspection = response?.inspection || null;
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to inspect chat", TITLE);
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
        target_context_id: this.followup.target_context_id || this.contextId,
        reason: this.followup.reason,
        message: this.followup.message,
        auto_send: !!this.followup.auto_send,
      });
      if (!response?.ok && !response?.queued) {
        void toastFrontendInfo(
          response?.result?.reason || "SwissCheese rejected the followup.",
          TITLE,
        );
      } else {
        void toastFrontendSuccess("Followup queued", TITLE);
        this.followup = {
          target_context_id: this.contextId,
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

  async bridgeFollowup() {
    if (!this.contextId) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "bridge_followup",
        context_id: this.contextId,
      });
      await this.refresh();
      if (response?.bridged) {
        void toastFrontendSuccess("Followup sent", TITLE);
      } else {
        void toastFrontendInfo("No auto-send followup was ready.", TITLE);
      }
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to bridge followup", TITLE);
    }
  },
});
