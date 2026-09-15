// Web、微信小程序和 Android 共用服务器上的同一个 FastAPI 后端。
// 当前服务器通过 Nginx 使用 80 端口对外提供服务。
// 微信小程序正式发布时必须替换为已配置 request 合法域名的 HTTPS 地址。
const API_BASE_URL = "http://193.112.28.40";

module.exports = {
  API_BASE_URL
};
