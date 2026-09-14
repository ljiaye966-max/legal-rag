const { request } = require("../../utils/request");
const { requireLogin } = require("../../utils/page");

Page({
  data: {
    document: null,
    chunks: []
  },

  onLoad(options) {
    this.docId = options.id;
  },

  onShow() {
    if (!requireLogin() || !this.docId) {
      return;
    }
    this.loadDocument();
  },

  async loadDocument() {
    try {
      const data = await request(`/api/documents/${this.docId}`, { loading: true });
      this.setData(data);
      wx.setNavigationBarTitle({ title: "文档详情" });
    } catch (error) {
      // 请求模块已处理提示。
    }
  }
});
