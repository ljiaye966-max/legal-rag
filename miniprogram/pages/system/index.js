const { request } = require("../../utils/request");
const { requireLogin, isSystemAdmin } = require("../../utils/page");

Page({
  data: {
    configs: [],
    health: null,
    metrics: null,
    saving: false
  },

  onShow() {
    if (!requireLogin()) {
      return;
    }
    if (!isSystemAdmin()) {
      wx.showToast({ title: "没有系统管理权限", icon: "none" });
      wx.navigateBack();
      return;
    }
    this.loadData();
  },

  async loadData() {
    try {
      const [configs, health, metrics] = await Promise.all([
        request("/api/configs"),
        request("/api/health", { auth: false, envelope: false }),
        request("/api/evaluations/metrics")
      ]);
      this.setData({ configs, health, metrics });
    } catch (error) {
      // 请求模块已处理提示。
    }
  },

  bindConfig(event) {
    const index = event.currentTarget.dataset.index;
    const configs = this.data.configs.slice();
    configs[index].value = event.detail.value;
    this.setData({ configs });
  },

  async save() {
    const payload = {};
    this.data.configs.forEach((item) => {
      payload[item.key] = item.value;
    });
    this.setData({ saving: true });
    try {
      await request("/api/configs", { method: "POST", data: payload, loading: true });
      wx.showToast({ title: "配置已保存", icon: "success" });
      this.loadData();
    } catch (error) {
      // 请求模块已处理提示。
    } finally {
      this.setData({ saving: false });
    }
  }
});
