// popup.js —— 插件弹窗逻辑：勾选确认后可点击「获取并发送」。
const agree = document.getElementById("agree");
const sendBtn = document.getElementById("sendBtn");
const statusEl = document.getElementById("status");

agree.addEventListener("change", () => {
  sendBtn.disabled = !agree.checked;
});

sendBtn.addEventListener("click", () => {
  sendBtn.disabled = true;
  statusEl.textContent = "正在发送…";
  chrome.runtime.sendMessage({ type: "send_cookie" }, (resp) => {
    if (chrome.runtime.lastError) {
      statusEl.textContent = "失败：" + chrome.runtime.lastError.message;
    } else if (resp && resp.ok) {
      statusEl.textContent = "已发送到本机程序。请回到程序查看结果。";
    } else if (resp && resp.status === 403) {
      statusEl.textContent = "被拒绝（来源不符）。请检查程序已用「从插件获取」开启等待。";
    } else if (resp && resp.status === 400) {
      statusEl.textContent = "请求格式不被接受。请确认已在 bilibili.com 登录。";
    } else {
      statusEl.textContent = "失败：未能连接本机程序。请确认程序已打开「从插件获取」等待窗口。";
    }
    sendBtn.disabled = false;
  });
});