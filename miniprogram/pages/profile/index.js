const { requireLogin, isAdmin, isSystemAdmin } = require("../../utils/page");

Page({
  data: {
    auth: null,
    canManageKnowledge: false,
    canManageSystem: false
  },

  onShow() {
    if (!requireLogin()) {
      return;
    }
    this.setData({
      auth: getApp().getAuth(),
      canManageKnowledge: isAdmin(),
      canManageSystem: isSystemAdmin()
    });
  },

  goKnowledgeBase() {
    wx.navigateTo({ url: "/pages/kb/index" });
  },

  goSystem() {
    wx.navigateTo({ url: "/pages/system/index" });
  },

  logout() {
    wx.showModal({
      title: "退出登录",
      content: "确定退出当前账号吗？",
      success(result) {
        if (!result.confirm) {
          return;
        }
        getApp().clearAuth();
        wx.reLaunch({ url: "/pages/login/index" });
      }
    });
  }
});
