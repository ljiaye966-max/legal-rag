const { request } = require("../../utils/request");
const { requireLogin, isAdmin } = require("../../utils/page");

Page({
  data: {
    documents: [],
    loading: false
  },

  onShow() {
    if (!requireLogin()) {
      return;
    }
    if (!isAdmin()) {
      wx.showToast({ title: "没有知识库管理权限", icon: "none" });
      wx.navigateBack();
      return;
    }
    this.loadDocuments();
  },

  async loadDocuments() {
    this.setData({ loading: true });
    try {
      const documents = await request("/api/kb/documents");
      this.setData({ documents });
    } catch (error) {
      // 请求模块已处理提示。
    } finally {
      this.setData({ loading: false });
    }
  },

  goCreate() {
    wx.navigateTo({ url: "/pages/kb-edit/index" });
  },

  goEdit(event) {
    wx.navigateTo({ url: `/pages/kb-edit/index?id=${event.currentTarget.dataset.id}` });
  },

  deleteDocument(event) {
    const docId = event.currentTarget.dataset.id;
    wx.showModal({
      title: "删除文档",
      content: "删除后文档分块和向量索引将被移除。",
      success: async (result) => {
        if (!result.confirm) {
          return;
        }
        try {
          await request(`/api/kb/documents/${docId}`, { method: "DELETE", loading: true });
          wx.showToast({ title: "已删除", icon: "success" });
          this.loadDocuments();
        } catch (error) {
          // 请求模块已处理提示。
        }
      }
    });
  }
});
