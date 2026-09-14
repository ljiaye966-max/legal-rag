App({
  globalData: {
    auth: null
  },

  onLaunch() {
    this.globalData.auth = wx.getStorageSync("legal_rag_auth") || null;
  },

  getAuth() {
    return this.globalData.auth || wx.getStorageSync("legal_rag_auth") || null;
  },

  setAuth(auth) {
    this.globalData.auth = auth;
    wx.setStorageSync("legal_rag_auth", auth);
  },

  clearAuth() {
    this.globalData.auth = null;
    wx.removeStorageSync("legal_rag_auth");
  },

  requireLogin() {
    if (this.getAuth()) {
      return true;
    }
    wx.reLaunch({ url: "/pages/login/index" });
    return false;
  }
});
