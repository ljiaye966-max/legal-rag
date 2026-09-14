const { request } = require("../../utils/request");
const { requireLogin } = require("../../utils/page");

const SUGGESTIONS = [
  "对方拖欠合同尾款，我需要准备哪些证据？",
  "借条和转账记录都有，对方到期不还款怎么办？",
  "公司以绩效不达标为由辞退员工，是否合法？",
  "《中华人民共和国民法典》第9999条规定了什么？"
];

Page({
  data: {
    question: "",
    suggestions: SUGGESTIONS,
    messages: [
      {
        id: "welcome",
        role: "assistant",
        text: "你好，我是法律智能小助手。你可以描述案件情况、已有证据或想了解的法律问题，我会从知识库中整理参考依据。"
      }
    ],
    sources: [],
    warnings: [],
    asking: false
  },

  onShow() {
    if (!requireLogin()) {
      return;
    }
    wx.setNavigationBarTitle({ title: "智能法律小助手" });
  },

  bindQuestion(event) {
    this.setData({ question: event.detail.value });
  },

  askSuggestion(event) {
    this.ask(event.currentTarget.dataset.question || "");
  },

  submit() {
    this.ask(this.data.question);
  },

  async ask(rawQuestion) {
    const question = String(rawQuestion || "").trim();
    if (!question || this.data.asking) {
      if (!question) {
        wx.showToast({ title: "请输入想咨询的问题", icon: "none" });
      }
      return;
    }

    const userMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      text: question
    };
    const history = [...this.data.messages, userMessage]
      .filter((item) => item.role === "user" || item.role === "assistant")
      .slice(-6)
      .map((item) => ({
        role: item.role === "user" ? "用户" : "助手",
        content: item.text
      }));

    this.setData({
      messages: [...this.data.messages, userMessage],
      question: "",
      sources: [],
      warnings: [],
      asking: true
    });

    try {
      const reply = await request("/api/assistant", {
        method: "POST",
        data: { question, history },
        loading: true
      });
      const assistantMessage = {
        id: `assistant-${Date.now()}`,
        role: "assistant",
        text: reply.answer
      };
      this.setData({
        messages: [...this.data.messages, assistantMessage],
        sources: reply.sources || [],
        warnings: reply.warnings || []
      });
    } catch (error) {
      const assistantMessage = {
        id: `error-${Date.now()}`,
        role: "assistant",
        text: "暂时无法完成分析，请检查网络连接后再试。"
      };
      this.setData({ messages: [...this.data.messages, assistantMessage] });
    } finally {
      this.setData({ asking: false });
      wx.pageScrollTo({ scrollTop: 100000, duration: 250 });
    }
  },

  goDocument(event) {
    wx.navigateTo({ url: `/pages/document-detail/index?id=${event.currentTarget.dataset.id}` });
  }
});
