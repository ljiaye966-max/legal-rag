const { request } = require("../../utils/request");
const { requireLogin } = require("../../utils/page");

Page({
  data: {
    tasks: [],
    loading: false
  },

  onShow() {
    if (!requireLogin()) {
      return;
    }
    this.loadTasks();
  },

  async loadTasks() {
    this.setData({ loading: true });
    try {
      const tasks = await request("/api/tasks");
      this.setData({ tasks });
    } catch (error) {
      // 请求模块已处理提示。
    } finally {
      this.setData({ loading: false });
    }
  },

  goTask(event) {
    wx.navigateTo({ url: `/pages/task-detail/index?id=${event.currentTarget.dataset.id}` });
  },

  deleteTask(event) {
    const taskId = event.currentTarget.dataset.id;
    wx.showModal({
      title: "删除任务",
      content: "删除后将不再出现在历史记录中。",
      success: async (result) => {
        if (!result.confirm) {
          return;
        }
        try {
          await request(`/api/tasks/${taskId}`, { method: "DELETE", loading: true });
          wx.showToast({ title: "已删除", icon: "success" });
          this.loadTasks();
        } catch (error) {
          // 请求模块已处理提示。
        }
      }
    });
  }
});
