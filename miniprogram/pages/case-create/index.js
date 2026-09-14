const { request } = require("../../utils/request");
const { requireLogin } = require("../../utils/page");

const CAUSES = ["全部", "合同纠纷", "民间借贷", "劳动争议", "交通事故", "婚姻家事"];
const REGIONS = ["全部", "全国", "江苏", "浙江", "广东", "四川", "山东"];
const YEARS = ["全部", "2026", "2025", "2024", "2023", "2022", "2021", "2020"];

Page({
  data: {
    caseText: "",
    causes: CAUSES,
    regions: REGIONS,
    years: YEARS,
    causeIndex: 0,
    regionIndex: 0,
    yearIndex: 0,
    submitting: false
  },

  onShow() {
    requireLogin();
  },

  bindText(event) {
    this.setData({ caseText: event.detail.value });
  },

  bindPicker(event) {
    this.setData({ [event.currentTarget.dataset.field]: Number(event.detail.value) });
  },

  async submit() {
    const caseText = this.data.caseText.trim();
    if (caseText.length < 10) {
      wx.showToast({ title: "请至少输入 10 个字的案情", icon: "none" });
      return;
    }
    this.setData({ submitting: true });
    try {
      const task = await request("/api/tasks", {
        method: "POST",
        data: {
          case_text: caseText,
          case_cause: CAUSES[this.data.causeIndex],
          region: REGIONS[this.data.regionIndex],
          year: YEARS[this.data.yearIndex]
        }
      });
      await request(`/api/tasks/${task.task_id}/reason`, {
        method: "POST",
        data: {
          case_text: caseText,
          case_cause: CAUSES[this.data.causeIndex],
          region: REGIONS[this.data.regionIndex],
          year: YEARS[this.data.yearIndex]
        },
        loading: true
      });
      wx.navigateTo({ url: `/pages/task-detail/index?id=${task.task_id}` });
    } catch (error) {
      // 请求模块已处理提示。
    } finally {
      this.setData({ submitting: false });
    }
  }
});
