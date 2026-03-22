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

export const store = createStore("swissCheese", {
  loading: false,
  contextId: "",
  state: null,
  contextWindow: null,
  inspection: null,
  inspectionSelector: "",
  followup: {
    selector: "",
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

  get barrierCards() {
    const barriers = ["Prepare", "Aviate", "Navigate", "Communicate", "Learn"];
    const holes = (this.state?.holes || []);
    return barriers.map((barrier) => ({
      barrier,
      holes: holes.filter((hole) => hole.barrier === barrier),
    }));
  },

  async refresh() {
    const contextId = chatsStore.selected || getContext();
    if (!contextId) {
      this.contextId = "";
      this.state = null;
      this.contextWindow = null;
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
      this.state = response.state || null;
      this.contextWindow = response.context_window || null;
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to load SwissCheese state", TITLE);
    } finally {
      this.loading = false;
    }
  },

  async inspectChat() {
    if (!this.contextId) return;
    try {
      const response = await api.callJsonApi(ENDPOINT, {
        action: "inspect_chat",
        context_id: this.contextId,
        selector: this.inspectionSelector || "",
      });
      this.inspection = response?.inspection || null;
      if (!this.inspection) {
        void toastFrontendInfo("No chat matched that selector.", TITLE);
      }
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to inspect chat", TITLE);
    }
  },

  async resolveTodo(todoId) {
    if (!this.contextId) return;
    try {
      await api.callJsonApi(ENDPOINT, {
        action: "todo_resolve",
        context_id: this.contextId,
        todo_id: todoId,
      });
      await this.refresh();
    } catch (error) {
      void toastFrontendError(error?.message || "Failed to resolve todo", TITLE);
    }
  },

  async clearCompletedTodos() {
    if (!this.contextId) return;
    try {
      await api.callJsonApi(ENDPOINT, {
        action: "todo_clear_completed",
        context_id: this.contextId,
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
        selector: this.followup.selector || "",
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
        this.followup = { selector: "", reason: "", message: "", auto_send: false };
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
