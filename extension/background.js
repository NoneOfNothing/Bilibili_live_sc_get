// background.js —— MV3 service worker：读取 bilibili.com 的 Cookie 并发送到本机程序。
//
// 仅在本弹窗里显式点「获取并发送」时触发；也只读取 .bilibili.com 域的 Cookie，
// 之后 POST 到本机回环 127.0.0.1:64321（程序「从插件获取」的等待窗口）。
const SERVER_URL = "http://127.0.0.1:64321/c";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "send_cookie") {
    chrome.cookies.getAll({ domain: ".bilibili.com" }, (cookies) => {
      if (chrome.runtime.lastError || !cookies || cookies.length === 0) {
        sendResponse({ ok: false, status: 0, error: "未获取到 bilibili.com 的 Cookie，请确认已登录" });
        return;
      }
      const cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
      fetch(SERVER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app: "blive_sc_get", cookies: cookieStr }),
      })
        .then((resp) => sendResponse({ ok: resp.ok, status: resp.status }))
        .catch(() => sendResponse({ ok: false, status: 0, error: "连接本机程序失败（端口未开启）" }));
    });
    return true; // 保持消息通道等待异步结果
  }
  return false;
});