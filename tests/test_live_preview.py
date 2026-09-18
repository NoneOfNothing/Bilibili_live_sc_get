"""直播预览回环代理的离线测试（假上游全部在 127.0.0.1，不访问外网）。

覆盖 spike 里验证过的关键环节：转发字节一致、**注入 Referer/Origin**、
**m3u8 分片地址改写**、换源（直链过期后替换上游）、未设置流时的 503 兜底。
"""

import asyncio
import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
from aiohttp import web

from blive_sc_get.api import (
    LIVE_HEARTBEAT_DEFAULT_INTERVAL,
    LIVE_HEARTBEAT_MAX_INTERVAL,
    LIVE_HEARTBEAT_MIN_INTERVAL,
    PREVIEW_DEFAULT_QUALITY,
    ApiError,
    BilibiliLiveAPI,
    heartbeat_hb,
    parse_accept_qn,
    parse_next_interval,
    parse_room_play_info,
)
from blive_sc_get.app_config import (
    DEFAULT_PREVIEW_MAX_DRIFT,
    DEFAULT_PREVIEW_MAX_ROOMS,
    DEFAULT_PREVIEW_QUALITY,
    DEFAULT_PREVIEW_VOLUME,
    AppConfig,
    load_app_config,
)
from blive_sc_get.live_preview import (
    FLV_PATH,
    HLS_PATH,
    LIVE_REFERER,
    SEGMENT_PATH,
    LiveStreamProxy,
)

FLV_BODY = b"FLV\x01fake-flv-stream" * 32
ALT_BODY = b"ALT\x02another-stream" * 32
SEG_BODY = b"TS-segment-bytes" * 16


class _FakeUpstream:
    """本地假上游：记录收到的请求（路径 + 请求头），返回固定内容。"""

    def __init__(self) -> None:
        self.requests: list = []
        self.runner = None
        self.base = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/up/live.flv", self._flv)
        app.router.add_get("/up/alt.flv", self._alt)
        app.router.add_get("/up/index.m3u8", self._m3u8)
        app.router.add_get("/up/seg1.ts", self._segment)
        app.router.add_get("/up/seg2.ts", self._segment)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}"
        return self.base

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    def paths(self) -> list:
        return [path for path, _headers in self.requests]

    def header_of(self, path: str, name: str):
        for req_path, headers in self.requests:
            if req_path == path:
                return headers.get(name)
        return None

    async def _flv(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.path, dict(request.headers)))
        resp = web.StreamResponse(headers={"Content-Type": "video/x-flv"})
        await resp.prepare(request)
        await resp.write(FLV_BODY)
        await resp.write_eof()
        return resp

    async def _alt(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.path, dict(request.headers)))
        return web.Response(body=ALT_BODY, content_type="video/x-flv")

    async def _m3u8(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.path, dict(request.headers)))
        body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                f"{self.base}/up/seg1.ts\n"   # 绝对地址
                "seg2.ts\n")                  # 相对地址（相对播放列表所在目录）
        return web.Response(text=body, content_type="application/vnd.apple.mpegurl")

    async def _segment(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.path, dict(request.headers)))
        return web.Response(body=SEG_BODY, content_type="video/mp2t")


class LiveStreamProxyTests(unittest.TestCase):
    """回环代理：转发 / 改写 / 换源 / 兜底。"""

    def setUp(self):
        self.upstream = _FakeUpstream()
        self.proxy = LiveStreamProxy()

    def _run(self, scenario) -> None:
        async def wrapper():
            base = await self.upstream.start()
            try:
                await scenario(base)
            finally:
                await asyncio.to_thread(self.proxy.stop)
                await self.upstream.stop()

        asyncio.run(wrapper())

    def test_flv_forwarding_injects_referer(self):
        async def scenario(base: str):
            self.proxy.set_streams(flv=f"{base}/up/live.flv", hls=None)
            local = await asyncio.to_thread(self.proxy.start)
            assert local.startswith("http://127.0.0.1:")
            async with aiohttp.ClientSession() as session:
                async with session.get(local + FLV_PATH) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(await resp.read(), FLV_BODY)
            self.assertIn("/up/live.flv", self.upstream.paths())
            # 防盗链头由代理注入（播放器自己无法设置请求头）
            self.assertEqual(self.upstream.header_of("/up/live.flv", "Referer"),
                             LIVE_REFERER)
            self.assertEqual(self.upstream.header_of("/up/live.flv", "Origin"),
                             LIVE_REFERER.rstrip("/"))
            self.assertGreaterEqual(self.proxy.requests, 1)
            self.assertGreaterEqual(self.proxy.forwarded_bytes, len(FLV_BODY))

        self._run(scenario)

    def test_hls_playlist_rewrites_segments(self):
        async def scenario(base: str):
            self.proxy.set_streams(flv=None, hls=f"{base}/up/index.m3u8")
            local = await asyncio.to_thread(self.proxy.start)
            async with aiohttp.ClientSession() as session:
                async with session.get(local + HLS_PATH) as resp:
                    self.assertEqual(resp.status, 200)
                    text = await resp.text()
                    # 直接带 q 的分片请求也要经代理转发
                    async with session.get(
                            f"{local}{SEGMENT_PATH}?u="
                            + quote(f"{base}/up/seg1.ts", safe="")) as seg:
                        self.assertEqual(seg.status, 200)
                        self.assertEqual(await seg.read(), SEG_BODY)
            lines = [line for line in text.splitlines()
                     if line and not line.startswith("#")]
            self.assertEqual(len(lines), 2)
            for line, expected in zip(lines, (f"{base}/up/seg1.ts", f"{base}/up/seg2.ts")):
                self.assertTrue(line.startswith(f"{SEGMENT_PATH}?u="), line)
                query = parse_qs(urlsplit(line).query)
                self.assertEqual(query["u"][0], expected)  # 绝对/相对地址都被解析成绝对
            self.assertIn("/up/seg1.ts", self.upstream.paths())
            self.assertEqual(self.upstream.header_of("/up/seg1.ts", "Referer"),
                             LIVE_REFERER)

        self._run(scenario)

    def test_set_streams_switches_upstream(self):
        async def scenario(base: str):
            self.proxy.set_streams(flv=f"{base}/up/live.flv", hls=None)
            local = await asyncio.to_thread(self.proxy.start)
            async with aiohttp.ClientSession() as session:
                async with session.get(local + FLV_PATH) as resp:
                    self.assertEqual(await resp.read(), FLV_BODY)
                # 直链过期后换源：替换上游即可，无需重启代理
                self.proxy.set_streams(flv=f"{base}/up/alt.flv", hls=None)
                async with session.get(local + FLV_PATH) as resp:
                    self.assertEqual(await resp.read(), ALT_BODY)
            self.assertIn("/up/alt.flv", self.upstream.paths())

        self._run(scenario)

    def test_missing_streams_return_503(self):
        async def scenario(base: str):
            local = await asyncio.to_thread(self.proxy.start)
            async with aiohttp.ClientSession() as session:
                for path in (FLV_PATH, HLS_PATH):
                    async with session.get(local + path) as resp:
                        self.assertEqual(resp.status, 503)
                async with session.get(local + SEGMENT_PATH) as resp:
                    self.assertEqual(resp.status, 400)  # 缺 u 参数

        self._run(scenario)

    def test_play_urls_and_lifecycle(self):
        for value in self.proxy.play_urls.values():
            self.assertIsNone(value)          # 未启动时没有本地地址
        self.assertFalse(self.proxy.running)

        async def scenario(base: str):
            self.proxy.set_streams(flv=f"{base}/up/live.flv",
                                   hls=f"{base}/up/index.m3u8")
            local = await asyncio.to_thread(self.proxy.start)
            urls = self.proxy.play_urls
            self.assertEqual(urls["flv"], local + FLV_PATH)
            self.assertEqual(urls["hls"], local + HLS_PATH)
            self.assertTrue(self.proxy.running)

        self._run(scenario)
        self.assertFalse(self.proxy.running)  # _run 的 finally 已 stop


class _FakeResponse:
    """假 aiohttp 响应：只需 ``_get_json`` 用到的那几个接口。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self, content_type=None) -> dict:  # noqa: ANN001
        return self._payload


class _FakePlayUrlSession:
    """按 ``platform`` 返回预设 JSON 的最小假 session。"""

    def __init__(self, payloads: dict) -> None:
        self.payloads = payloads
        self.calls: list = []

    def get(self, url, params=None, headers=None):  # noqa: ANN001
        platform = (params or {}).get("platform")
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self.payloads.get(platform, {"code": -400, "message": "x"}))


class LiveStreamApiTests(unittest.TestCase):
    """``api.get_live_stream_urls``：两种格式各一次请求、部分失败不影响另一种。"""

    def _api(self, session):
        return BilibiliLiveAPI(session, "SESSDATA=x; bili_jct=y")

    def test_parses_both_formats(self):
        session = _FakePlayUrlSession({
            "web": {"code": 0, "data": {"durl": [{"url": "https://x/live.flv"}],
                                        "accept_quality": ["4", "3", "2"]}},
            "h5": {"code": 0, "data": {"durl": [{"url": "https://x/live.m3u8"}]}},
        })
        result = asyncio.run(self._api(session).get_live_stream_urls(5050, qn=150))
        self.assertEqual(result["flv"], "https://x/live.flv")
        self.assertEqual(result["hls"], "https://x/live.m3u8")
        self.assertEqual(result["accept_quality"], ["4", "3", "2"])
        self.assertEqual(result["errors"], {})
        platforms = [params["platform"] for _url, params in session.calls]
        self.assertEqual(platforms, ["web", "h5"])
        self.assertTrue(all(params["cid"] == 5050 and params["qn"] == 150
                            for _url, params in session.calls))

    def test_partial_failure_reported_per_format(self):
        session = _FakePlayUrlSession({
            "web": {"code": 0, "data": {"durl": [{"url": "https://x/live.flv"}]}},
            "h5": {"code": -400, "message": "参数错误"},
        })
        result = asyncio.run(self._api(session).get_live_stream_urls(5050))
        self.assertEqual(result["flv"], "https://x/live.flv")
        self.assertIsNone(result["hls"])
        self.assertIn("hls", result["errors"])
        self.assertNotIn("flv", result["errors"])

    def test_missing_durl_reported(self):
        session = _FakePlayUrlSession({
            "web": {"code": 0, "data": {}},
            "h5": {"code": 0, "data": {}},
        })
        result = asyncio.run(self._api(session).get_live_stream_urls(5050))
        self.assertIsNone(result["flv"])
        self.assertIn("durl", result["errors"]["flv"])


class PreviewConfigTests(unittest.TestCase):
    """``preview`` 配置段：默认值、合法值、非法回退、常量一致。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "config.json"

    def _write(self, data) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_defaults(self):
        cfg = AppConfig().preview
        self.assertEqual(cfg.quality, DEFAULT_PREVIEW_QUALITY)
        self.assertTrue(cfg.mute)                 # 默认静音，避免切换预览时突然出声
        self.assertEqual(cfg.volume, DEFAULT_PREVIEW_VOLUME)
        self.assertTrue(cfg.always_on_top)
        self.assertTrue(cfg.follow_room)
        self.assertFalse(cfg.watch_time)   # 写操作：上报观看时长默认必须关闭
        self.assertEqual(cfg.max_rooms, DEFAULT_PREVIEW_MAX_ROOMS)
        self.assertEqual(cfg.max_rooms, 4)   # 多路预览：默认 4 路（也是硬上限）
        self.assertEqual(cfg.max_drift_sec, DEFAULT_PREVIEW_MAX_DRIFT)
        self.assertEqual(cfg.max_drift_sec, 3.0)   # 默认落后 3 秒就自动追回
        self.assertEqual(load_app_config(self.tmp / "nope.json").preview, cfg)

    def test_parse_valid_section(self):
        self._write({"preview": {"quality": 10000, "mute": False, "volume": 30,
                                 "always_on_top": False, "follow_room": False,
                                 "watch_time": True, "max_rooms": 2,
                                 "max_drift_sec": 1.5}})
        cfg = load_app_config(self.path).preview
        self.assertEqual(cfg.quality, 10000)
        self.assertFalse(cfg.mute)
        self.assertEqual(cfg.volume, 30)
        self.assertFalse(cfg.always_on_top)
        self.assertFalse(cfg.follow_room)
        self.assertTrue(cfg.watch_time)
        self.assertEqual(cfg.max_rooms, 2)
        self.assertEqual(cfg.max_drift_sec, 1.5)

    def test_invalid_values_fall_back(self):
        self._write({"preview": {"quality": 999, "mute": "false", "volume": -5,
                                 "always_on_top": 1, "follow_room": "yes",
                                 "watch_time": "true", "max_rooms": True,
                                 "max_drift_sec": "3"}})
        cfg = load_app_config(self.path).preview
        self.assertEqual(cfg.quality, DEFAULT_PREVIEW_QUALITY)  # 白名单外的清晰度
        self.assertTrue(cfg.mute)                               # 非布尔 → 默认
        self.assertEqual(cfg.volume, DEFAULT_PREVIEW_VOLUME)    # 负数 → 默认
        self.assertTrue(cfg.always_on_top)
        self.assertTrue(cfg.follow_room)
        # 字符串 "true" 不算开启（写操作按「关闭」处理，避免误开）
        self.assertFalse(cfg.watch_time)
        self.assertEqual(cfg.max_rooms, DEFAULT_PREVIEW_MAX_ROOMS)  # bool 不算整数
        self.assertEqual(cfg.max_drift_sec, DEFAULT_PREVIEW_MAX_DRIFT)  # 字符串不算数

    def test_max_drift_clamped_and_fallback(self):
        """落后阈值（秒）：过大截断到上限、负数回退默认、0 表示关闭自动追边。"""
        self._write({"preview": {"max_drift_sec": 9999}})
        self.assertEqual(load_app_config(self.path).preview.max_drift_sec, 600.0)
        self._write({"preview": {"max_drift_sec": -1}})
        self.assertEqual(load_app_config(self.path).preview.max_drift_sec,
                         DEFAULT_PREVIEW_MAX_DRIFT)
        self._write({"preview": {"max_drift_sec": 0}})
        self.assertEqual(load_app_config(self.path).preview.max_drift_sec, 0.0)
        self._write({"preview": {"max_drift_sec": 2}})
        self.assertEqual(load_app_config(self.path).preview.max_drift_sec, 2.0)

    def test_max_rooms_clamped_and_fallback(self):
        """路数上限：过大截断到硬上限、小于 1 回退默认。"""
        self._write({"preview": {"max_rooms": 9}})
        self.assertEqual(load_app_config(self.path).preview.max_rooms,
                         DEFAULT_PREVIEW_MAX_ROOMS)
        self._write({"preview": {"max_rooms": 0}})
        self.assertEqual(load_app_config(self.path).preview.max_rooms,
                         DEFAULT_PREVIEW_MAX_ROOMS)
        self._write({"preview": {"max_rooms": 1}})
        self.assertEqual(load_app_config(self.path).preview.max_rooms, 1)

    def test_volume_clamped_above(self):
        self._write({"preview": {"volume": 500}})
        self.assertEqual(load_app_config(self.path).preview.volume, 100)

    def test_non_object_section_falls_back(self):
        self._write({"preview": "auto"})
        self.assertEqual(load_app_config(self.path).preview, AppConfig().preview)

    def test_default_quality_constants_in_sync(self):
        # api 的默认参数与配置默认值必须一致（防两处漂移）
        self.assertEqual(PREVIEW_DEFAULT_QUALITY, DEFAULT_PREVIEW_QUALITY)
        # 默认「自动」（0 = 取该房间可用最高档），不是固定某档
        self.assertEqual(DEFAULT_PREVIEW_QUALITY, 0)


class AcceptQnParseTests(unittest.TestCase):
    """``parse_accept_qn``：取该房间**真正可用**的清晰度。

    旧接口 ``playUrl`` 的 ``accept_quality`` 返回的是清晰度**编号**（实测 ``['4']``、
    只有一项），拿它当 qn 过滤会全部落空——表现为下拉列出 4K/杜比等根本拉不到的档位。
    新接口 ``getRoomPlayInfo`` 的 ``accept_qn`` 才是真实 qn。
    """

    @staticmethod
    def _payload(streams: list) -> dict:
        return {"code": 0, "data": {"playurl_info": {"playurl": {"stream": streams}}}}

    def test_union_dedup_and_order(self):
        payload = self._payload([
            {"protocol_name": "http_stream",
             "format": [{"format_name": "flv",
                         "codec": [{"accept_qn": [10000, 400, 250]}]}]},
            {"protocol_name": "http_hls",
             "format": [{"format_name": "ts",
                         "codec": [{"accept_qn": [400, 250, 150]}]}]},
        ])
        self.assertEqual(parse_accept_qn(payload), (10000, 400, 250, 150))

    def test_missing_or_broken_payload(self):
        self.assertEqual(parse_accept_qn({}), ())
        self.assertEqual(parse_accept_qn({"data": None}), ())
        self.assertEqual(parse_accept_qn({"data": {"playurl_info": {}}}), ())
        broken = self._payload([
            {"format": [{"codec": [{"accept_qn": ["x", 0, -1, None, 250]}]}]}])
        self.assertEqual(parse_accept_qn(broken), (250,))


class WatchHeartbeatTests(unittest.TestCase):
    """观看时长心跳（ROADMAP 63 · P2）：``hb`` 构造与间隔解析（均为纯函数）。"""

    def test_hb_is_base64_of_documented_format(self):
        hb = heartbeat_hb(26863308, 60)
        self.assertEqual(base64.b64decode(hb).decode("utf-8"), "60|26863308|1|0")

    def test_hb_tolerates_string_inputs(self):
        self.assertEqual(base64.b64decode(heartbeat_hb("5050", "30")).decode("utf-8"),
                         "30|5050|1|0")

    def test_next_interval_parsed_and_clamped(self):
        self.assertEqual(parse_next_interval({"data": {"next_interval": 60}}), 60)
        # 服务端给得太短（像脚本刷时长）或太长（统计失真）都夹到范围内
        self.assertEqual(parse_next_interval({"data": {"next_interval": 1}}),
                         LIVE_HEARTBEAT_MIN_INTERVAL)
        self.assertEqual(parse_next_interval({"data": {"next_interval": 99999}}),
                         LIVE_HEARTBEAT_MAX_INTERVAL)

    def test_next_interval_falls_back_on_broken_payload(self):
        for payload in ({}, {"data": None}, {"data": {}}, "not-a-dict",
                        {"data": {"next_interval": "x"}},
                        {"data": {"next_interval": 0}},
                        {"data": {"next_interval": None}},
                        {"data": {"next_interval": -5}}):
            self.assertEqual(parse_next_interval(payload), LIVE_HEARTBEAT_DEFAULT_INTERVAL,
                             f"异常响应应回退默认间隔：{payload!r}")


class _FakeRouteSession:
    """按 URL 片段路由到不同预设 JSON 的假 session（记录每次请求的参数）。"""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list = []

    def get(self, url, params=None, headers=None):  # noqa: ANN001
        self.calls.append((url, dict(params or {})))
        for needle, payload in self.routes.items():
            if needle in url:
                return _FakeResponse(payload)
        return _FakeResponse({"code": -400, "message": "no route"})


class EncryptedRoomTests(unittest.TestCase):
    """加密（密码）直播间（ROADMAP 63 · P4）：状态透传、带密码拉流、密码校验。"""

    def _api(self, session):
        return BilibiliLiveAPI(session, "SESSDATA=x; bili_jct=y")

    def test_parse_room_play_info_passes_flags_through(self):
        payload = {"data": {"encrypted": True, "pwd_verified": False,
                            "playurl_info": {"playurl": {"stream": [
                                {"format": [{"codec": [{"accept_qn": [250, 150]}]}]}]}}}}
        self.assertEqual(parse_room_play_info(payload), ((250, 150), True, False))
        # 缺字段 / 类型不对 → 状态为「未知」（None），不能当成 False
        self.assertEqual(parse_room_play_info({}), ((), None, None))
        self.assertEqual(parse_room_play_info({"data": {"encrypted": "yes"}}), ((), None, None))

    def test_get_stream_info_sends_pwd_and_reports_flags(self):
        session = _FakeRouteSession({"getRoomPlayInfo": {"code": 0, "data": {
            "encrypted": True, "pwd_verified": True,
            "playurl_info": {"playurl": {"stream": [
                {"format": [{"codec": [{"accept_qn": [10000]}]}]}]}}}}})
        info = asyncio.run(self._api(session).get_stream_info(5050, pwd="s3cret"))
        self.assertEqual(info["qualities"], (10000,))
        self.assertTrue(info["encrypted"])
        self.assertTrue(info["pwd_verified"])
        self.assertEqual(info["error"], "")
        self.assertEqual(session.calls[0][1]["pwd"], "s3cret")

    def test_get_stream_info_omits_pwd_when_empty(self):
        session = _FakeRouteSession({"getRoomPlayInfo": {"code": 0, "data": {}}})
        asyncio.run(self._api(session).get_stream_info(5050))
        self.assertNotIn("pwd", session.calls[0][1])

    def test_get_live_stream_urls_carries_pwd(self):
        session = _FakeRouteSession({"playUrl": {"code": 0, "data": {
            "durl": [{"url": "https://x/live.flv"}]}}})
        result = asyncio.run(self._api(session).get_live_stream_urls(5050, qn=150, pwd="p"))
        self.assertEqual(result["flv"], "https://x/live.flv")
        self.assertTrue(session.calls)
        for _url, params in session.calls:
            self.assertEqual(params["pwd"], "p")

    def test_verify_room_pwd(self):
        ok = _FakeRouteSession({"verify_room_pwd": {"code": 0}})
        self.assertTrue(asyncio.run(self._api(ok).verify_room_pwd(5050, "p")))
        bad = _FakeRouteSession({"verify_room_pwd": {"code": -400, "message": "密码错误"}})
        self.assertFalse(asyncio.run(self._api(bad).verify_room_pwd(5050, "p")))
        empty = _FakeRouteSession({})
        self.assertFalse(asyncio.run(self._api(empty).verify_room_pwd(5050, " ")))
        self.assertEqual(empty.calls, [], "空密码不应发请求")


class _FakeHeartbeatSession:
    """只服务 ``webHeartBeat`` 的假 session：记录请求参数并返回预设 JSON。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list = []

    def get(self, url, params=None, headers=None):  # noqa: ANN001
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self.payload)


class WatchHeartbeatApiTests(unittest.TestCase):
    """``api.report_watch_heartbeat``：参数构造、需登录、错误码上抛。"""

    def test_sends_hb_and_pf_web(self):
        session = _FakeHeartbeatSession({"code": 0, "data": {"next_interval": 60}})
        api = BilibiliLiveAPI(session, "SESSDATA=x; bili_jct=y")
        seconds = asyncio.run(api.report_watch_heartbeat(5050, 60))
        self.assertEqual(seconds, 60)
        url, params = session.calls[0]
        self.assertTrue(url.endswith("/xlive/rdata-interface/v1/heartbeat/webHeartBeat"),
                        url)
        self.assertEqual(params["pf"], "web")
        self.assertEqual(base64.b64decode(params["hb"]).decode("utf-8"), "60|5050|1|0")

    def test_requires_cookie(self):
        session = _FakeHeartbeatSession({"code": 0, "data": {}})
        api = BilibiliLiveAPI(session, "")
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(api.report_watch_heartbeat(5050, 60))
        self.assertEqual(ctx.exception.code, -101)
        self.assertEqual(session.calls, [], "未登录时不应发起请求")

    def test_error_code_raises(self):
        session = _FakeHeartbeatSession({"code": -352, "message": "风控"})
        api = BilibiliLiveAPI(session, "SESSDATA=x")
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(api.report_watch_heartbeat(5050, 60))
        self.assertEqual(ctx.exception.code, -352)


if __name__ == "__main__":
    unittest.main()
