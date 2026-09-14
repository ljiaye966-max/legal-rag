const { API_BASE_URL } = require("./config");

function redirectToLogin() {
  const app = getApp();
  app.clearAuth();
  wx.reLaunch({ url: "/pages/login/index" });
}

function request(path, options = {}) {
  const {
    method = "GET",
    data = {},
    auth = true,
    envelope = true,
    loading = false
  } = options;
  const app = getApp();
  const session = app.getAuth();
  const header = {
    "content-type": "application/json"
  };

  if (auth && session && session.session_token) {
    header.Authorization = `Bearer ${session.session_token}`;
  }
  if (loading) {
    wx.showLoading({ title: "加载中", mask: true });
  }

  return new Promise((resolve, reject) => {
    wx.request({
      url: `${API_BASE_URL}${path}`,
      method,
      data,
      header,
      success(response) {
        const body = response.data || {};
        if (response.statusCode === 401) {
          redirectToLogin();
          reject(new Error(body.message || "登录已失效"));
          return;
        }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          wx.showToast({ title: body.message || "请求失败", icon: "none" });
          reject(new Error(body.message || "请求失败"));
          return;
        }
        if (envelope && body.code !== 0) {
          wx.showToast({ title: body.message || "操作失败", icon: "none" });
          reject(new Error(body.message || "操作失败"));
          return;
        }
        resolve(envelope ? body.data : body);
      },
      fail(error) {
        wx.showToast({ title: "无法连接后端服务", icon: "none" });
        reject(error);
      },
      complete() {
        if (loading) {
          wx.hideLoading();
        }
      }
    });
  });
}

module.exports = {
  request,
  API_BASE_URL
};
