"""P0 spike：验证「拉流 + 回环代理 + QtMultimedia 播放」是否可行（ROADMAP 63）。

**只做验证，不接入主程序**；跑完即可删除本文件。

验证链路（每一项都是完整方案里的必要环节）：

1. 取一个**正在直播**的房间（``--room`` 指定，否则从 ``data/gui_rooms.json`` 里挑第一个在播的）；
2. 拉流地址：旧接口 ``room/v1/Room/playUrl``——``platform=web`` 取 http-flv、``platform=h5`` 取 hls；
3. 起**回环代理**（127.0.0.1）：直链带防盗链，必须注入 ``Referer``/``Origin``；
   HLS 还要改写 m3u8 里的分片地址（否则播放器会绕过代理直连分片 → 403）；
4. 用 ``QMediaPlayer`` + ``QVideoWidget`` 播放代理地址若干秒，观察
   ``mediaStatus`` / ``errorOccurred`` / ``position`` 推进 / ``hasVideo`` / CPU 占用；
5. 打印结论：哪种格式能播、是否建议走 QtMultimedia（否则转 libmpv）。

用法::

    python _spike_live_preview.py                 # 自动挑在播房间，flv 与 hls 各测 12 秒
    python _spike_live_preview.py --room 5050 --seconds 15 --qn 150
    python _spike_live_preview.py --format flv    # 只测 http-flv
    python _spike_live_preview.py --mute false    # 放开声音（默认静音，避免突然出声）

排错提示：若两种格式都失败，可先单独排查直链（脚本会打印直链前缀与 HTTP 状态）；
仍失败可尝试 ``set QT_MEDIA_BACKEND=ffmpeg`` 强制 FFmpeg 后端再跑一次。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # 控制台默认 GBK，直链与中文日志统一按 UTF-8 输出，避免乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from blive_sc_get.api import ROOM_INFO_URL, USER_AGENT  # noqa: E402
from blive_sc_get.cli import resolve_cookie  # noqa: E402

PLAY_URL = "https://api.live.bilibili.com/room/v1/Room/playUrl"
LIVE_REFERER = "https://live.bilibili.com/"
ROOMS_FILE = Path("data") / "gui_rooms.json"

HOT_LIST_URLS = (
    ("https://api.live.bilibili.com/room/v1/room/get_user_recommend",
     {"page": 1, "page_size": 20}),
    ("https://api.live.bilibili.com/xlive/web-interface/v1/second/getList",
     {"platform": "web", "parent_area_id": 0, "area_id": 0,
      "sort_type": "online", "page": 1}),
)
"""推荐/分区列表：配置里的房间都没开播时，用来自动挑一个在播房间做技术验证。

``get_user_recommend`` 实测可用（``data`` 为房间数组）；``second/getList`` 目前常被风控
拦（code=-352），仅作备选。
"""


def _live_headers(cookie: Optional[str]) -> dict:
    """直链与接口请求头：防盗链要求 Referer/Origin，其余尽量贴近网页端。"""
    headers = {"User-Agent": USER_AGENT, "Referer": LIVE_REFERER,
               "Origin": LIVE_REFERER.rstrip("/")}
    if cookie:
        headers["Cookie"] = cookie
    return headers


class LiveProxy(threading.Thread):
    """后台线程里的 aiohttp 事件循环：解析直链 + 回环代理转发。"""

    def __init__(self, cookie: Optional[str], *, room: Optional[int], qn: int,
                 auto_hot: bool = False) -> None:
        super().__init__(daemon=True)
        self._cookie = cookie
        self._room = room
        self._qn = qn
        self._auto_hot = auto_hot
        self.ready = threading.Event()
        self.error: Optional[str] = None
        self.port: Optional[int] = None
        self.room_info: dict = {}
        self.flv_url: Optional[str] = None
        self.hls_url: Optional[str] = None
        self.flv_error: Optional[str] = None
        self.hls_error: Optional[str] = None
        self.requests = 0  # 代理转发次数（含分片）
        self._session: Optional[aiohttp.ClientSession] = None

    # ---------- 线程主体 ----------

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # pragma: no cover - 兜底上报
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()

    async def _main(self) -> None:
        app = web.Application()
        app.router.add_get("/live.flv", self._handle_flv)
        app.router.add_get("/live.m3u8", self._handle_m3u8)
        app.router.add_get("/seg", self._handle_seg)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        timeout = aiohttp.ClientTimeout(total=20, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            try:
                if self._room:
                    room_id = self._room
                elif self._auto_hot:
                    room_id = await self._pick_hot_room()
                else:
                    room_id = await self._pick_live_room()
                self.room_info = await self._get_room_info(room_id)
                real_room = int(self.room_info["room_id"])
                self.flv_url, self.flv_error = await self._fetch_play_url(
                    real_room, platform="web")
                self.hls_url, self.hls_error = await self._fetch_play_url(
                    real_room, platform="h5")
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.ready.set()
                return
            self.ready.set()
            await asyncio.Event().wait()  # 代理常驻，直到进程退出

    # ---------- 直链解析 ----------

    async def _pick_live_room(self) -> int:
        from blive_sc_get.gui_config import load_room_entries

        entries = load_room_entries(ROOMS_FILE) if ROOMS_FILE.exists() else []
        for entry in entries:
            try:
                info = await self._get_room_info(entry.room_id)
            except Exception:
                continue
            if int(info.get("live_status") or 0) == 1:
                return entry.room_id
        raise RuntimeError("没有正在直播的房间：请用 --room 指定一个正在直播的房间号")

    async def _pick_hot_room(self) -> int:
        """从推荐/分区列表挑一个在播房间（配置房间都没开播时的技术验证兜底）。"""
        assert self._session is not None
        candidates: list[int] = []
        for url, params in HOT_LIST_URLS:
            try:
                async with self._session.get(
                        url, params=params, headers=_live_headers(self._cookie)) as resp:
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            items: list = []
            if isinstance(data, list):          # get_user_recommend：直接是房间数组
                items = data
            elif isinstance(data, dict):
                for key in ("list", "room_list", "recommend_room_list"):
                    if isinstance(data.get(key), list):
                        items = data[key]
                        break
            for item in items:
                if not isinstance(item, dict):
                    continue
                room_id = item.get("roomid") or item.get("room_id")
                if room_id:
                    candidates.append(int(room_id))
            if candidates:
                break
        if not candidates:
            raise RuntimeError("无法从推荐列表取到在播房间：请用 --room 手动指定")
        for room_id in candidates[:5]:
            try:
                info = await self._get_room_info(room_id)
            except Exception:
                continue
            if int(info.get("live_status") or 0) == 1:
                print(f"（未指定房间：自动选用热门在播房间 {room_id} 做技术验证）")
                return room_id
        raise RuntimeError("热门列表里的房间当前都不在播：请用 --room 手动指定")

    async def _get_room_info(self, room_id: int) -> dict:
        assert self._session is not None
        async with self._session.get(ROOM_INFO_URL, params={"room_id": room_id},
                                     headers=_live_headers(self._cookie)) as resp:
            payload = await resp.json(content_type=None)
        if payload.get("code") != 0:
            raise RuntimeError(f"get_info 失败（code={payload.get('code')} "
                               f"{payload.get('message')}）")
        return payload.get("data") or {}

    async def _fetch_play_url(self, real_room: int, *,
                              platform: str) -> tuple[Optional[str], Optional[str]]:
        """取一条直链：``platform=web`` → http-flv，``h5`` → hls。"""
        assert self._session is not None
        params = {"cid": real_room, "platform": platform, "qn": self._qn}
        async with self._session.get(PLAY_URL, params=params,
                                     headers=_live_headers(self._cookie)) as resp:
            status = resp.status
            payload = await resp.json(content_type=None)
        if status != 200 or payload.get("code") != 0:
            return None, (f"HTTP {status}，code={payload.get('code')} "
                          f"{payload.get('message')}")
        durl = ((payload.get("data") or {}).get("durl") or [])
        if not durl or not durl[0].get("url"):
            return None, "响应没有 durl[0].url"
        return str(durl[0]["url"]), None

    # ---------- 代理 ----------

    async def _pipe(self, request: web.Request, url: str) -> web.StreamResponse:
        """把上游流原样转发给本地播放器（长连接，客户端断开即结束）。"""
        assert self._session is not None
        self.requests += 1
        try:
            upstream = await self._session.get(url, headers=_live_headers(self._cookie))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.Response(status=502, text=f"upstream error: {exc}")
        response = web.StreamResponse(status=upstream.status)
        response.headers["Content-Type"] = upstream.headers.get(
            "Content-Type", "application/octet-stream")
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(32 * 1024):
                await response.write(chunk)
        except (ConnectionResetError, aiohttp.ClientError, asyncio.CancelledError):
            pass  # 播放器停止/换源时会直接断开
        finally:
            upstream.close()
        try:
            await response.write_eof()
        except (ConnectionResetError, RuntimeError):
            pass
        return response

    async def _handle_flv(self, request: web.Request) -> web.StreamResponse:
        if not self.flv_url:
            return web.Response(status=503, text=f"flv 不可用: {self.flv_error}")
        return await self._pipe(request, self.flv_url)

    async def _handle_seg(self, request: web.Request) -> web.StreamResponse:
        target = request.query.get("u")
        if not target:
            return web.Response(status=400, text="missing u")
        return await self._pipe(request, target)

    async def _handle_m3u8(self, request: web.Request) -> web.StreamResponse:
        """拉取播放列表并把分片地址改写到本代理（否则播放器直连分片会 403）。"""
        assert self._session is not None
        if not self.hls_url:
            return web.Response(status=503, text=f"hls 不可用: {self.hls_error}")
        async with self._session.get(self.hls_url,
                                     headers=_live_headers(self._cookie)) as resp:
            status = resp.status
            text = await resp.text()
            base = str(resp.url)
        if status != 200:
            return web.Response(status=502, text=f"m3u8 HTTP {status}")
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(line)
                continue
            lines.append("/seg?u=" + quote(urljoin(base, stripped), safe=""))
        return web.Response(text="\n".join(lines) + "\n",
                            content_type="application/vnd.apple.mpegurl")


def _enum_name(value) -> str:
    """Qt 6 的枚举在 PySide6 里不能直接 int()，统一取枚举名做展示。"""
    name = getattr(value, "name", None)
    return str(name) if name else str(value)


def probe(label: str, url: str, *, seconds: float, mute: bool) -> dict:
    """用 QMediaPlayer 播放 url 一段时间，返回观测结果（在主线程调用）。"""
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    player = QMediaPlayer()
    audio = QAudioOutput()
    audio.setMuted(mute)
    audio.setVolume(0.6)
    player.setAudioOutput(audio)
    widget = QVideoWidget()
    widget.setWindowTitle(f"spike {label} — {seconds:.0f}s 后自动关闭")
    widget.resize(640, 360)
    widget.show()
    player.setVideoOutput(widget)

    stats: dict = {"label": label, "errors": [], "statuses": [], "max_position": 0,
                   "has_video": False, "has_audio": False, "cpu_ratio": 0.0,
                   "state": None}

    def on_error(err, message) -> None:  # noqa: ANN001
        stats["errors"].append(f"{_enum_name(err)}: {message}")

    player.errorOccurred.connect(on_error)

    def sample() -> None:
        stats["max_position"] = max(stats["max_position"], player.position())
        stats["statuses"].append(_enum_name(player.mediaStatus()))
        stats["has_video"] = stats["has_video"] or player.hasVideo()
        stats["has_audio"] = stats["has_audio"] or player.hasAudio()

    timer = QTimer()
    timer.timeout.connect(sample)
    timer.start(500)

    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    cpu_before, wall_before = time.process_time(), time.monotonic()
    player.setSource(QUrl(url))
    player.play()
    loop.exec()
    wall = max(0.001, time.monotonic() - wall_before)
    stats["cpu_ratio"] = (time.process_time() - cpu_before) / wall
    stats["state"] = _enum_name(player.playbackState())
    stats["status_final"] = _enum_name(player.mediaStatus())
    timer.stop()
    player.stop()
    player.setSource(QUrl())
    widget.hide()
    widget.deleteLater()
    app.processEvents()
    return stats


def report(results: list[dict]) -> bool:
    print("\n=== spike 结论 ===")
    ok_any = False
    for stats in results:
        played = stats["max_position"] > 1000 and not stats["errors"]
        ok_any = ok_any or played
        verdict = "可播放" if played else "失败"
        detail = "；".join(stats["errors"]) if stats["errors"] else "无错误"
        print(f"[{stats['label']}] {verdict}：位置推进 {stats['max_position'] / 1000:.1f}s，"
              f"有画面={stats['has_video']}，有音轨={stats['has_audio']}，"
              f"进程 CPU≈{stats['cpu_ratio'] * 100:.0f}%，终态 mediaStatus="
              f"{stats.get('status_final')}，playbackState={stats['state']}（{detail}）")
    print("\n建议：" + ("QtMultimedia 可用 → 走路线 A（QtMultimedia + 本地代理）"
                      if ok_any else
                      "QtMultimedia 播不动 → 转路线 B（libmpv 内嵌），"
                      "或先试 set QT_MEDIA_BACKEND=ffmpeg 复测"))
    return ok_any


def main() -> int:
    parser = argparse.ArgumentParser(description="ROADMAP 63 拉流/播放可行性 spike")
    parser.add_argument("--room", type=int, default=None,
                        help="直播间号（默认从 data/gui_rooms.json 挑第一个在播的）")
    parser.add_argument("--seconds", type=float, default=12.0, help="每种格式试播秒数")
    parser.add_argument("--qn", type=int, default=150, help="清晰度（80 流畅 / 150 高清 / 10000 原画）")
    parser.add_argument("--format", choices=("both", "flv", "hls"), default="both")
    parser.add_argument("--mute", default="true", help="true/false：是否静音试播")
    parser.add_argument("--auto-hot", action="store_true",
                        help="配置房间都没开播时，从热门列表挑一个在播房间")
    args = parser.parse_args()

    try:
        import PySide6  # noqa: F401
        from PySide6.QtCore import qVersion
    except Exception as exc:
        print(f"缺少 PySide6（{exc}）：本 spike 需要 Qt 版环境")
        return 2
    print(f"PySide6 {PySide6.__version__} / Qt {qVersion()}，"
          f"QT_MEDIA_BACKEND={os.environ.get('QT_MEDIA_BACKEND', '(默认)')}")

    cookie = resolve_cookie(None)
    print(f"Cookie：{'已加载（长度 %d）' % len(cookie) if cookie else '未提供（可能拿不到高清晰度）'}")

    proxy = LiveProxy(cookie, room=args.room, qn=args.qn, auto_hot=args.auto_hot)
    proxy.start()
    if not proxy.ready.wait(timeout=30):
        print("解析直链超时（30s）——检查网络/接口可用性")
        return 2
    if proxy.error:
        print(f"准备阶段失败：{proxy.error}")
        return 2

    info = proxy.room_info
    print(f"房间 {info.get('room_id')}（{info.get('title') or '无标题'}）"
          f"live_status={info.get('live_status')}，代理端口 {proxy.port}")
    for label, url, err in (("http-flv", proxy.flv_url, proxy.flv_error),
                            ("hls", proxy.hls_url, proxy.hls_error)):
        if url:
            print(f"{label} 直链：{url[:110]}…")
        else:
            print(f"{label} 直链获取失败：{err}")

    targets = []
    if args.format in ("both", "flv"):
        targets.append(("http-flv", f"http://127.0.0.1:{proxy.port}/live.flv"))
    if args.format in ("both", "hls"):
        targets.append(("hls(m3u8)", f"http://127.0.0.1:{proxy.port}/live.m3u8"))

    results = [probe(label, url, seconds=args.seconds, mute=args.mute.lower() != "false")
               for label, url in targets]
    print(f"\n代理累计转发请求 {proxy.requests} 次")
    report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
