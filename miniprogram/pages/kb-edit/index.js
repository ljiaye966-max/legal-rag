const { request } = require("../../utils/request");
const { requireLogin, isAdmin } = require("../../utils/page");

const TYPES = ["case", "statute"];
const CAUSES = ["合同纠纷", "民间借贷", "劳动争议", "交通事故", "婚姻家事"];

Page({
  data: {
    docId: "",
    title: "",
    content: "",
    documentType: "case",
    caseCause: "合同纠纷",
    region: "全国",
    year: "2026",
    types: TYPES,
    causes: CAUSES,
    typeIndex: 0,
    causeIndex: 0,
    saving: false
  },

  onLoad(options) {
    this.setData({ docId: options.id || "" });
  },

  onShow() {
    if (!requireLogin() || !isAdmin()) {
      return;
    }
    if (this.data.docId && !this.loaded) {
      this.loadDocument();
    }
  },

  async loadDocument() {
    try {
      const data = await request(`/api/kb/documents/${this.data.docId}`, { loading: true });
      const document = data.document;
      const typeIndex = TYPES.indexOf(document.document_type);
      const causeIndex = CAUSES.indexOf(document.case_cause);
      this.setData({
        title: document.title,
        content: data.chunks.map((item) => item.content).join("\n\n"),
        documentType: document.document_type,
        caseCause: document.case_cause,
        region: document.region || "全国",
        year: String(document.year || "2026"),
        typeIndex: typeIndex < 0 ? 0 : typeIndex,
        causeIndex: causeIndex < 0 ? 0 : causeIndex
      });
      this.loaded = true;
    } catch (error) {
      // 请求模块已处理提示。
    }
  },

  bindText(event) {
    this.setData({ [event.currentTarget.dataset.field]: event.detail.value });
  },

  bindPicker(event) {
    const field = event.currentTarget.dataset.field;
    const index = Number(event.detail.value);
    if (field === "typeIndex") {
      this.setData({ typeIndex: index, documentType: TYPES[index] });
      return;
    }
    this.setData({ causeIndex: index, caseCause: CAUSES[index] });
  },

  async save() {
    const { docId, title, content, documentType, caseCause, region, year } = this.data;
    if (!title.trim() || !content.trim()) {
      wx.showToast({ title: "标题和正文不能为空", icon: "none" });
      return;
    }
    this.setData({ saving: true });
    const payload = {
      title: title.trim(),
      content: content.trim(),
      document_type: documentType,
      case_cause: caseCause,
      region: region.trim() || "全国",
      year: Number(year) || 2026
    };
    try {
      await request(docId ? `/api/kb/documents/${docId}` : "/api/kb/documents", {
        method: docId ? "PUT" : "POST",
        data: payload,
        loading: true
      });
      wx.showToast({ title: "保存成功", icon: "success" });
      wx.navigateBack();
    } catch (error) {
      // 请求模块已处理提示。
    } finally {
      this.setData({ saving: false });
    }
  }
});
