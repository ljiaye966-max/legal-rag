const { request } = require("../../utils/request");
const { requireLogin } = require("../../utils/page");

Page({
  data: {
    health: null,
    tasks: []
  },

  onShow() {
    if (!requireLogin()) {
      return;
    }
    this.loadData();
  },

  async loadData() {
    try {
      const [health, tasks] = await Promise.all([
        request("/api/health", { auth: false, envelope: false }),
        request("/api/tasks")
      ]);
      this.setData({ health, tasks: tasks.slice(0, 5) });
    } catch (error) {
      // 请求模块已处理提示。
    }
  },

  goCaseCreate() {
    wx.navigateTo({ url: "/pages/case-create/index" });
  },

  goAssistant() {
    wx.switchTab({ url: "/pages/search/index" });
  },

  goTask(event) {
    wx.navigateTo({ url: `/pages/task-detail/index?id=${event.currentTarget.dataset.id}` });
  }
});
