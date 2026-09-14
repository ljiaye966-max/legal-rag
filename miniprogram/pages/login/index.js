const { request } = require("../../utils/request");

Page({
  data: {
    mode: "login",
    account: "user01",
    password: "123456",
    phone: "",
    verificationCode: "",
    displayName: "",
    demoCode: "",
    codeSeconds: 0,
    loading: false
  },

  onShow() {
    if (getApp().getAuth()) {
      wx.switchTab({ url: "/pages/home/index" });
    }
  },

  bindInput(event) {
    this.setData({ [event.currentTarget.dataset.field]: event.detail.value });
  },

  switchMode(event) {
    this.setData({
      mode: event.currentTarget.dataset.mode,
      demoCode: ""
    });
  },

  onUnload() {
    this.stopCodeTimer();
  },

  startCodeTimer() {
    this.stopCodeTimer();
    this.setData({ codeSeconds: 60 });
    this.codeTimer = setInterval(() => {
      const codeSeconds = this.data.codeSeconds - 1;
      if (codeSeconds <= 0) {
        this.stopCodeTimer();
        this.setData({ codeSeconds: 0 });
        return;
      }
      this.setData({ codeSeconds });
    }, 1000);
  },

  stopCodeTimer() {
    if (this.codeTimer) {
      clearInterval(this.codeTimer);
      this.codeTimer = null;
    }
  },

  async sendCode() {
    const phone = String(this.data.phone || "").replace(/[\s-]/g, "");
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      wx.showToast({ title: "请输入有效手机号", icon: "none" });
      return;
    }
    try {
      const data = await request("/api/auth/send-code", {
        method: "POST",
        auth: false,
        data: { phone, purpose: "register" },
        loading: true
      });
      this.setData({ phone, demoCode: data.demo_code || "" });
      this.startCodeTimer();
      wx.showToast({ title: data.demo_code ? `演示码 ${data.demo_code}` : "验证码已发送", icon: "none", duration: 3500 });
    } catch (error) {
      // 请求模块已展示错误提示。
    }
  },

  async submit() {
    const { mode, account, password, phone, verificationCode, displayName } = this.data;
    if (!password) {
      wx.showToast({ title: "请输入密码", icon: "none" });
      return;
    }
    if (mode === "login" && !account) {
      wx.showToast({ title: "请输入用户名或手机号", icon: "none" });
      return;
    }
    if (mode === "register" && (!/^1[3-9]\d{9}$/.test(String(phone).replace(/[\s-]/g, "")) || !/^\d{6}$/.test(verificationCode))) {
      wx.showToast({ title: "请填写手机号和 6 位验证码", icon: "none" });
      return;
    }
    this.setData({ loading: true });
    try {
      const auth = await request(mode === "login" ? "/api/login" : "/api/register", {
        method: "POST",
        auth: false,
        data: mode === "login"
          ? { account, password }
          : { phone: String(phone).replace(/[\s-]/g, ""), verification_code: verificationCode, password, display_name: displayName }
      });
      getApp().setAuth(auth);
      wx.showToast({ title: mode === "login" ? "登录成功" : "注册成功", icon: "success" });
      wx.switchTab({ url: "/pages/home/index" });
    } catch (error) {
      // 请求模块已展示错误提示。
    } finally {
      this.setData({ loading: false });
    }
  }
});
