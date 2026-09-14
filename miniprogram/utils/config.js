// 当前电脑 WLAN 地址。微信开发者工具和安卓真机需与电脑连接同一个 Wi-Fi。
// 如果电脑 IP 变化，请把这里改成新的 IPv4 地址，并保持端口与后端服务一致。
// 正式发布时必须替换为已配置 request 合法域名的 HTTPS 地址。
const API_BASE_URL = "http://10.53.31.3:8003";

module.exports = {
  API_BASE_URL
};
