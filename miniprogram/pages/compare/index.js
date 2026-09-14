const { request } = require("../../utils/request");
const { requireLogin } = require("../../utils/page");

Page({
  data: {
    taskId: "",
    candidates: [],
    selectedIds: [],
    cases: []
  },

  onLoad(options) {
    this.setData({ taskId: options.taskId || "" });
  },

  onShow() {
    if (!requireLogin()) {
      return;
    }
    this.loadCompare();
  },

  async loadCompare() {
    const { taskId, selectedIds } = this.data;
    const query = taskId ? `?task_id=${taskId}&case_ids=${selectedIds.join(",")}` : "";
    try {
      const data = await request(`/api/compare${query}`, { loading: true });
      const selectedIds = (data.selected_ids || []).map(String);
      this.setData({
        candidates: (data.candidate_cases || []).map((item) => ({
          ...item,
          checked: selectedIds.includes(String(item.document_id))
        })),
        selectedIds,
        cases: data.cases || []
      });
    } catch (error) {
      // 请求模块已处理提示。
    }
  },

  chooseCases(event) {
    const selectedIds = event.detail.value;
    if (selectedIds.length > 5) {
      wx.showToast({ title: "最多选择 5 个案例", icon: "none" });
      return;
    }
    this.setData({
      selectedIds,
      candidates: this.data.candidates.map((item) => ({
        ...item,
        checked: selectedIds.includes(String(item.document_id))
      }))
    });
  },

  compare() {
    if (this.data.selectedIds.length < 2) {
      wx.showToast({ title: "请至少选择 2 个案例", icon: "none" });
      return;
    }
    this.loadCompare();
  },

  goDocument(event) {
    wx.navigateTo({ url: `/pages/document-detail/index?id=${event.currentTarget.dataset.id}` });
  }
});
