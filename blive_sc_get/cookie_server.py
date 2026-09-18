"""通过浏览器扩展接收 B 站 Cookie 的本地一次性 HTTP 服务器（GUI「从插件获取」）。

与 :mod:`browser_cookie`（读取浏览器数据库，需关闭浏览器且受新版 App-Bound 加密
影响）相比，本方式无需关闭浏览器：用户安装本项目自带的浏览器扩展后，在插件里点
按钮，即可把 ``bilibili.com`` 的 Cookie 发送到本机 ``127.0.0.1`` 的短暂端口，
程序把收到的 Cookie 写入 ``cookie.txt`` 并应用到当前会话。

安全模型（三重）：
- 仅绑定 ``127.0.0.1``（回环），不暴露局域网；
- **校验 ``Origin`` 头**必须为 ``chrome-extension://`` 或 ``moz-extension://`` 前缀
  （普通网页无法伪造该头），否则 403；
- 仅接受 ``POST /c``，且请求体须含 ``app == "blive_sc_get"`` 签名与一段合法 cookie
  （形如 ``name=value; ...``），否则 400/404。

服务器只由 GUI 点击「从插件获取」后短暂开启（约 90 秒或收到一次成功即自动关闭）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .log_categories import CATEGORY_TASK, get_logger

logger = get_logger(CATEGORY_TASK, __name__)

DEFAULT_COOKIE_PORT = 64321
"""与扩展 ``background.js`` 里约定的固定回环端口。"""

COOKIE_ENDPOINT = "/c"
"""接收 Cookie 的 POST 路径（配合空资源路径，区分于任意网页请求）。"""

ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")
"""仅接受浏览器扩展来源（Chromium / Firefox 命名空间）。"""

APP_SIGN = "blive_sc_get"
"""请求体里的应用签名，避免误收其它程序发出的请求。"""

WINDOW_TIMEOUT = 90.0
"""单次获取窗口的存活秒数；超时或收到一次成功后即关闭。"""


def origin_allowed(origin: Optional[str]) -> bool:
    """请求来源是否可信：仅浏览器扩展的 ``Origin`` 前缀。"""
    origin = (origin or "").strip()
    return origin.startswith(ALLOWED_ORIGIN_PREFIXES)


def decode_cookie_payload(data: Any) -> str:
    """校验并取出请求体里的 cookie 字符串（纯函数，便于单测）。

    返回规范化的 cookie 串（已去首尾空白）；任一条件不满足抛 ``ValueError``。
    """
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    if data.get("app") != APP_SIGN:
        raise ValueError("app 签名不匹配")
    cookies = data.get("cookies")
    if not isinstance(cookies, str) or not cookies.strip() or "=" not in cookies:
        raise ValueError("cookies 字段缺失或格式非法")
    return cookies.strip()


class CookieReceiver(ThreadingHTTPServer):
    """在回环地址上短暂监听的服务器，把第一个合法请求里的 cookie 记下来。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_COOKIE_PORT):
        # 端口被占用/不可用时由 ``OSError`` 上抛，供调用方提示
        super().__init__((host, port), _CookieHandler)
        self._done = threading.Event()
        self.received_cookie: str = ""

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def accept(self, cookies: str) -> None:
        """记录收到的 cookie 并唤醒等待方（供 handler 在收到合法请求时调用）。"""
        self.received_cookie = cookies
        self._done.set()


class _CookieHandler(BaseHTTPRequestHandler):
    """请求处理：仅 ``POST /c`` 且 Origin 为浏览器扩展时才接受。"""

    server_version = "blive-sc-get-cookie/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # 覆盖默认 stdout 日志，统一走 logging
        logger.debug("cookie server: " + fmt, *args)

    # ---- Header / 响应辅助 ----

    def _cors_headers(self):
        return [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, Origin"),
        ]

    def _reply(self, code, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        for key, value in self._cors_headers():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    # ---- HTTP 方法 ----

    def do_OPTIONS(self):
        """响应 MV3 扩展跨源 fetch 的预检。"""
        self._reply(204, b"")

    def do_POST(self):
        origin = self.headers.get("Origin") or ""
        if not origin_allowed(origin):
            logger.warning("cookie server 拒绝非扩展来源: %r", origin)
            return self._reply(403, b"forbidden origin")
        if self.path.split("?")[0] != COOKIE_ENDPOINT:
            return self._reply(404, b"not found")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, b"invalid json")
        try:
            cookies = decode_cookie_payload(data)
        except ValueError as exc:
            return self._reply(400, str(exc).encode("utf-8"))
        server = getattr(self, "server", None)
        if server is not None and isinstance(server, CookieReceiver):
            server.accept(cookies)
        logger.info("cookie server 收到浏览器的 B 站 Cookie（长度 %d）", len(cookies))
        self._reply(200, b"ok")


def wait_for_extension_cookie(timeout: float = WINDOW_TIMEOUT,
                              port: int = DEFAULT_COOKIE_PORT) -> Optional[str]:
    """启动一次性服务器，等待浏览器扩展发来 Cookie。

    返回收到的 cookie 字符串；超时或未收到返回 ``None``。无论结果 **都确保服务器
    关闭**。端口被占用等绑定失败会直接抛 ``OSError``（由调用方提示）。
    """
    receiver = CookieReceiver(host="127.0.0.1", port=port)
    thread = threading.Thread(target=receiver.serve_forever,
                              name="blive-cookie-receiver", daemon=True)
    thread.start()
    try:
        if receiver._done.wait(timeout):
            return receiver.received_cookie or None
        logger.info("cookie server 在 %.1fs 内未收到插件 Cookie，超时关闭", timeout)
        return None
    finally:
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=1.0)