const { request } = require("../../utils/request");
const { requireLogin } = require("../../utils/page");

const RUNNING_STATUSES = ["queued", "running", "retrieving", "generating", "validating"];

Page({
  data: {
    taskId: "",
    task: null,
    result: null,
    citations: [],
    validation: { passed: true, warnings: [] },
    referenceAudit: { total: 0, verified: 0, unverified: 0, citation_accuracy: null },
    reasoningChain: [],
    caseMetrics: {},
    loading: true,
    feedbackRating: "helpful",
    feedbackComment: ""
  },

  onLoad(options) {
    this.setData({ taskId: options.id || "" });
  },

  onShow() {
    if (!requireLogin() || !this.data.taskId) {
      return;
    }
    this.loadTask();
  },

  onUnload() {
    this.stopPolling();
  },

  async loadTask() {
    try {
      const data = await request(`/api/tasks/${this.data.taskId}`, { loading: !this.data.task });
      let validation = { passed: true, warnings: [] };
      if (data.result && data.result.result_json) {
        try {
          validation = JSON.parse(data.result.result_json).validation || validation;
        } catch (error) {
          // 保留默认校验状态。
        }
      }
      this.setData({
        task: data.task,
        result: data.result,
        citations: data.citations || [],
        validation,
        referenceAudit: validation.reference_audit || { total: 0, verified: 0, unverified: 0, citation_accuracy: null },
        reasoningChain: validation.reasoning_chain || [],
        caseMetrics: validation.case_retrieval_metrics || {},
        loading: false
      });
      if (RUNNING_STATUSES.includes(data.task.status) && !data.result) {
        this.startPolling();
      } else {
        this.stopPolling();
      }
    } catch (error) {
      this.setData({ loading: false });
    }
  },

  startPolling() {
    if (this.pollTimer) {
      return;
    }
    this.pollTimer = setInterval(() => this.loadTask(), 2000);
  },

  stopPolling() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  },

  goCompare() {
    wx.navigateTo({ url: `/pages/compare/index?taskId=${this.data.taskId}` });
  },

  goDocument(event) {
    const documentId = event.currentTarget.dataset.id;
    if (!documentId) {
      return;
    }
    wx.navigateTo({ url: `/pages/document-detail/index?id=${documentId}` });
  },

  copyResult() {
    if (!this.data.result) {
      return;
    }
    wx.setClipboardData({ data: this.data.result.result_markdown });
  },

  async exportResult() {
    try {
      const data = await request(`/api/tasks/${this.data.taskId}/export`);
      wx.setClipboardData({
        data: data.markdown,
        success() {
          wx.showToast({ title: "报告已复制", icon: "success" });
        }
      });
    } catch (error) {
      // 请求模块已处理提示。
    }
  },

  selectRating(event) {
    this.setData({ feedbackRating: event.currentTarget.dataset.rating });
  },

  bindFeedback(event) {
    this.setData({ feedbackComment: event.detail.value });
  },

  async submitFeedback() {
    if (!this.data.result) {
      return;
    }
    try {
      await request("/api/feedback", {
        method: "POST",
        data: {
          task_id: this.data.task.id,
          result_id: this.data.result.id,
          rating: this.data.feedbackRating,
          comment: this.data.feedbackComment
        },
        loading: true
      });
      this.setData({ feedbackComment: "" });
      wx.showToast({ title: "反馈已提交", icon: "success" });
    } catch (error) {
      // 请求模块已处理提示。
    }
  }
});
