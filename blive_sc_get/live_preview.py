"""直播预览的业务层：**注入防盗链请求头的回环代理**（不含 Qt 依赖，便于离线测试）。

为什么要代理：B 站直播直链（``*.bilivideo.com``）校验 ``Referer`` / ``Origin``，缺了会
返回 403；而 ``QMediaPlayer`` 无法为请求自定义请求头。于是在本机回环起一个转发代理
（仅 ``127.0.0.1``、随机端口），播放器只连本地地址，由代理代注入这些头。

HLS 额外需要**改写 m3u8 里的分片地址**成代理地址：否则播放器拿到原始分片直链后会绕过
代理直连，照样 403（ROADMAP 63 的 spike 里已实测这条链路可行）。

设计要点：

- 线程内独立 asyncio 事件循环，与 GUI 的 ``AsyncHub`` 互不干扰；
- 仅监听回环、随机端口，不对外暴露；``stop()`` 可完整回收；
- FLV 是无限长流：客户端（播放器）断开即结束本次转发；
- 直链**有时效**（``expires``）：上层换源后调用 ``set_streams`` 替换即可，无需重启代理。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Dict, Optional
from urllib.parse import quote, urljoin

import aiohttp
from aiohttp import web

from .api import USER_AGENT
from .log_categories import CATEGORY_LIVE, get_logger

logger = get_logger(CATEGORY_LIVE, "live.preview")

LIVE_REFERER = "https://live.bilibili.com/"
"""防盗链校验所需的 Referer / Origin。"""

DEFAULT_START_TIMEOUT = 10.0
"""代理启动（线程 + 监听）等待上限（秒）。"""

FLV_PATH = "/live.flv"
HLS_PATH = "/live.m3u8"
SEGMENT_PATH = "/seg"
"""本地代理的三个入口：FLV 流 / HLS 播放列表 / HLS 分片。"""


class LiveStreamProxy:
    """回环代理：把带防盗链的直播直链转发给本地播放器。

    典型用法::

        proxy = LiveStreamProxy(cookie=cookie)
        proxy.set_streams(flv=urls["flv"], hls=urls["hls"])
        base = proxy.start()                 # http://127.0.0.1:PORT
        player.setSource(QUrl(base + FLV_PATH))
        ...
        proxy.stop()
    """

    def __init__(self, *, cookie: Optional[str] = None,
                 referer: str = LIVE_REFERER) -> None:
        self._cookie = cookie
        self._referer = referer
        self._lock = threading.Lock()
        self._flv: Optional[str] = None
        self._hls: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._port: Optional[int] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._requests = 0
        self._forwarded_bytes = 0

    # ---------- 生命周期 ----------

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def base_url(self) -> Optional[str]:
        return f"http://127.0.0.1:{self._port}" if self._port else None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def requests(self) -> int:
        """累计转发次数（FLV 1 次；HLS 为播放列表 + 每个分片各一次）。"""
        return self._requests

    @property
    def forwarded_bytes(self) -> int:
        return self._forwarded_bytes

    def start(self, *, timeout: float = DEFAULT_START_TIMEOUT) -> str:
        """启动代理线程并返回本地基址（``http://127.0.0.1:PORT``）。

        已在运行时直接返回原基址；启动超时或失败抛 ``RuntimeError``。
        """
        if self.running and self.base_url:
            return self.base_url
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="live-preview-proxy",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"预览代理启动超时（{timeout:.0f}s）")
        if self._error:
            raise RuntimeError(self._error)
        assert self.base_url is not None
        return self.base_url

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止代理并回收线程（未启动或已停止时静默返回）。"""
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            self._thread = None
            return
        try:
            loop.call_soon_threadsafe(self._stop_event.set)  # type: ignore[union-attr]
        except RuntimeError:  # loop 已关闭
            pass
        thread.join(timeout)
        self._thread = None
        self._loop = None
        self._port = None

    # ---------- 上游地址 ----------

    def set_streams(self, *, flv: Optional[str], hls: Optional[str]) -> None:
        """设置/替换上游直链（直链过期后由上层重新拉取再调用一次）。"""
        with self._lock:
            changed = (flv != self._flv) or (hls != self._hls)
            self._flv, self._hls = flv, hls
        if changed:
            logger.info("预览流已%s：flv=%s，hls=%s",
                        "更新" if self._flv or self._hls else "清空",
                        "有" if flv else "无", "有" if hls else "无")
            logger.debug("预览直链：flv=%s hls=%s", flv, hls)

    @property
    def play_urls(self) -> Dict[str, Optional[str]]:
        """本地播放地址（播放器用这个，而不是上游直链）。"""
        base = self.base_url
        if not base:
            return {"flv": None, "hls": None}
        with self._lock:
            return {
                "flv": base + FLV_PATH if self._flv else None,
                "hls": base + HLS_PATH if self._hls else None,
            }

    # ---------- 内部：线程与事件循环 ----------

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover - 兜底上报给 start()
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()

    async def _main(self) -> None:
        app = web.Application()
        app.router.add_get(FLV_PATH, self._handle_flv)
        app.router.add_get(HLS_PATH, self._handle_m3u8)
        app.router.add_get(SEGMENT_PATH, self._handle_segment)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        logger.info("预览代理已启动：%s（仅本机回环）", self.base_url)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self._session = session
                self._ready.set()
                await self._stop_event.wait()
        finally:
            self._session = None
            await runner.cleanup()
            logger.info("预览代理已停止（累计转发 %d 次 / %.1f MB）",
                        self._requests, self._forwarded_bytes / 1024 / 1024)

    # ---------- 内部：转发 ----------

    def _headers(self) -> Dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Referer": self._referer,
                   "Origin": self._referer.rstrip("/")}
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    async def _pipe(self, request: web.Request, url: str) -> web.StreamResponse:
        """把上游流原样转发（长连接；客户端断开即收尾）。"""
        if self._session is None:
            return web.Response(status=503, text="proxy not ready")
        self._requests += 1
        try:
            upstream = await self._session.get(url, headers=self._headers())
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("预览转发失败（%s）：%s", url.split("?")[0], exc)
            return web.Response(status=502, text=f"upstream error: {exc}")
        response = web.StreamResponse(status=upstream.status)
        response.headers["Content-Type"] = upstream.headers.get(
            "Content-Type", "application/octet-stream")
        await response.prepare(request)
        try:
            # iter_any：上游一到数据就转出去（而不是攒够 64KB 才发）。直播流对「转发延迟」
            # 敏感，攒块会让画面整体后移；这里是我们唯一自己引入的固定延迟，尽量压到最小。
            async for chunk in upstream.content.iter_any():
                self._forwarded_bytes += len(chunk)
                await response.write(chunk)
        except (ConnectionResetError, aiohttp.ClientError, asyncio.CancelledError):
            pass  # 播放器停止/换源时会直接断开，属正常
        finally:
            upstream.close()
        try:
            await response.write_eof()
        except (ConnectionResetError, RuntimeError):
            pass
        return response

    async def _handle_flv(self, request: web.Request) -> web.StreamResponse:
        with self._lock:
            url = self._flv
        if not url:
            return web.Response(status=503, text="no flv stream")
        return await self._pipe(request, url)

    async def _handle_segment(self, request: web.Request) -> web.StreamResponse:
        target = request.query.get("u")
        if not target:
            return web.Response(status=400, text="missing u")
        return await self._pipe(request, target)

    async def _handle_m3u8(self, request: web.Request) -> web.StreamResponse:
        """拉取播放列表，并把分片地址改写到本代理（否则播放器直连分片会 403）。"""
        if self._session is None:
            return web.Response(status=503, text="proxy not ready")
        with self._lock:
            url = self._hls
        if not url:
            return web.Response(status=503, text="no hls stream")
        try:
            async with self._session.get(url, headers=self._headers()) as upstream:
                status = upstream.status
                text = await upstream.text()
                base = str(upstream.url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("预览 HLS 播放列表拉取失败：%s", exc)
            return web.Response(status=502, text=f"upstream error: {exc}")
        if status != 200:
            return web.Response(status=502, text=f"m3u8 HTTP {status}")
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(line)
                continue
            absolute = urljoin(base, stripped)
            lines.append(f"{SEGMENT_PATH}?u=" + quote(absolute, safe=""))
        return web.Response(text="\n".join(lines) + "\n",
                            content_type="application/vnd.apple.mpegurl")
