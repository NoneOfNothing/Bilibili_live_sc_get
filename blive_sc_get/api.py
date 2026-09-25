"""B 站直播相关 HTTP 接口封装：房间信息、弹幕服务器信息（含 WBI 签名）、游客 buvid3。

接口与签名算法参考了开源项目 xfgryujk/blivedm 对现行 web 端协议的梳理。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .gui_config import format_live_mark
from .log_categories import CATEGORY_DATA, get_logger
from .medal_tasks import (
    dedupe_medals,
    parse_medal_panel,
    parse_task_info_list,
)

logger = get_logger(CATEGORY_DATA, __name__)

_API_LOG_PARAM_KEYS = ("room_id", "roomid", "target_id", "uid", "page", "page_size",
                       "type", "dm_type", "mode", "color", "limit", "offset")
"""接口日志里允许打印的参数键（房间号 / uid / 分页等）。

**不含 cookie、csrf（bili_jct）与 WBI 签名**——日志会落盘与长期保留，不打印凭据。
"""


def api_log_label(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    """接口请求的日志标签：路径 + 少量关键参数（见 ``_API_LOG_PARAM_KEYS``）。"""
    path = urllib.parse.urlparse(url).path or url
    picked = [f"{key}={params[key]}" for key in _API_LOG_PARAM_KEYS
              if params and params.get(key) not in (None, "")]
    return f"{path}?{'&'.join(picked)}" if picked else path

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PLAY_URL = "https://api.live.bilibili.com/room/v1/Room/playUrl"
"""直播推流地址（旧版接口，``platform=web`` 返回 http-flv、``h5`` 返回 hls）。"""

ROOM_PLAY_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
"""新版拉流信息接口：**只有它给出该房间真正可用的清晰度**。

旧接口的 ``accept_quality`` 返回的是「清晰度编号」（实测 ''4''、只一项），不是 ``qn``，
拿它当 qn 过滤会全部落空（表现为清晰度下拉列出 4K/杜比等根本拉不到的档位）。
"""

LIVE_STREAM_PLATFORMS: tuple = (("flv", "web"), ("hls", "h5"))
"""拉流格式 → ``platform`` 参数：``web`` 给 http-flv，``h5`` 给 hls（m3u8）。"""

VERIFY_ROOM_PWD_URL = "https://api.live.bilibili.com/room/v1/Room/verify_room_pwd"
"""加密（密码）直播间的密码校验（ROADMAP 63 · P4）。

密码房要先让**服务端**记住「本会话已解锁」，之后的拉流请求才会给出地址；所以拿到密码后
先调它、再带 ``pwd`` 重拉一次（两条路一起走，命中率最高）。该接口来自社区资料，官方文档
未收录，属于尽力而为的兼容实现。
"""

PREVIEW_DEFAULT_QUALITY = 0
"""预览默认清晰度：``0`` = **自动**（取该房间可用最高档）。

与 ``app_config.DEFAULT_PREVIEW_QUALITY`` 保持一致（有测试防两处漂移）。
"""

MAX_PREVIEW_QUALITY = 30000
"""请求清晰度时用的上限（杜比），交给服务端按账号权限降级到 ``accept_qn`` 里的档位。"""

LIVE_HEARTBEAT_URL = (
    "https://live-trace.bilibili.com/xlive/rdata-interface/v1/heartbeat/webHeartBeat"
)
"""网页端「观看时长」心跳（ROADMAP 63 · P2，**写操作**）。

GET，参数只有 ``hb`` 与 ``pf=web``：``hb`` 是 ``base64("<next_interval>|<真实房间号>|1|0")``，
服务端用返回的 ``data.next_interval``（文档默认 60 秒）决定下一次该隔多久上报。
"""

LIVE_HEARTBEAT_DEFAULT_INTERVAL = 60
"""首次上报（还不知道服务端间隔）用的秒数，对齐网页端默认值。"""

LIVE_HEARTBEAT_MIN_INTERVAL = 15
"""接受的服务端间隔下限：间隔过短（如 1 秒）等于脚本刷时长，夹到这里。"""

LIVE_HEARTBEAT_MAX_INTERVAL = 300
"""接受的服务端间隔上限：太长会让「已上报时长」与实际节奏偏离过大，夹到 5 分钟。"""


def parse_accept_qn(payload: Dict[str, Any]) -> Tuple[int, ...]:
    """从 ``getRoomPlayInfo`` 响应里取出可用清晰度（去重、降序）。纯函数，便于离线测试。

    结构：``data.playurl_info.playurl.stream[].format[].codec[].accept_qn``；
    同一房间各协议/编码的列表一致（flv / hls、avc / hevc 都一样），取并集即可。
    """
    playurl = (((payload.get("data") or {}).get("playurl_info") or {})
               .get("playurl") or {})
    found = set()
    for stream in playurl.get("stream") or []:
        if not isinstance(stream, dict):
            continue
        for fmt in stream.get("format") or []:
            if not isinstance(fmt, dict):
                continue
            for codec in fmt.get("codec") or []:
                if not isinstance(codec, dict):
                    continue
                for qn in codec.get("accept_qn") or []:
                    try:
                        value = int(qn)
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        found.add(value)
    return tuple(sorted(found, reverse=True))


def parse_room_play_info(payload: Dict[str, Any]
                         ) -> Tuple[Tuple[int, ...], Optional[bool], Optional[bool]]:
    """从 ``getRoomPlayInfo`` 响应取「可用档位 + 是否加密 + 密码是否已通过」（纯函数）。

    返回 ``(accept_qn, encrypted, pwd_verified)``；加密与验证状态**取不到时为 ``None``
    （未知）**，不要当成 ``False``：官方文档明确 ``pwd_verified`` 只在 ``encrypted`` 为真
    时才有意义，而且非加密房间下不同接口给的默认值并不一致（``room_init`` 给 ``false``、
    ``getRoomPlayInfo`` 给 ``true``），所以这里只做原样透传，由上层配合 ``encrypted`` 判断。
    """
    qualities = parse_accept_qn(payload)
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    encrypted = data.get("encrypted")
    verified = data.get("pwd_verified")
    return (qualities,
            encrypted if isinstance(encrypted, bool) else None,
            verified if isinstance(verified, bool) else None)


def heartbeat_hb(room_id: int, next_interval: int) -> str:
    """构造 ``webHeartBeat`` 的 ``hb`` 参数（纯函数，便于离线测试）。

    明文为 ``"{next_interval}|{真实房间号}|1|0"``——后两段网页端固定写 ``1`` 与 ``0``
    （官方文档标注「作用尚不明确」），再做 base64。
    """
    raw = f"{int(next_interval)}|{int(room_id)}|1|0"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def parse_next_interval(payload: Dict[str, Any],
                        fallback: int = LIVE_HEARTBEAT_DEFAULT_INTERVAL,
                        low: int = LIVE_HEARTBEAT_MIN_INTERVAL,
                        high: int = LIVE_HEARTBEAT_MAX_INTERVAL) -> int:
    """取 ``data.next_interval``（下次心跳间隔秒数）并夹到 ``[low, high]``（纯函数）。

    服务端异常（缺字段 / 非数字 / 0 或负数 / 超大）一律回退 ``fallback``——上报节奏宁可
    慢一点，也不要按一个奇怪的值猛刷。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    value = data.get("next_interval") if isinstance(data, dict) else None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return fallback
    if seconds <= 0:
        return fallback
    return max(low, min(high, seconds))

ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
ROOM_INFO_OLD_URL = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
ROOM_H5_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getH5InfoByRoom"
ANCHOR_INFO_URL = "https://api.live.bilibili.com/live_user/v1/Master/info"
DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
SEND_DANMAKU_URL = "https://api.live.bilibili.com/msg/send"
DM_CONFIG_URL = "https://api.live.bilibili.com/xlive/web-room/v1/dM/GetDMConfigByGroup"
ROOM_EMOTICON_URL = (
    "https://api.live.bilibili.com/xlive/web-ucenter/v2/emoticon/GetEmoticons"
)
GUARD_TOP_LIST_URL = "https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList"
MEDAL_PANEL_URL = "https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/panel"
MEDAL_TASK_URL = (
    "https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/GetActivatedMedalInfo"
)
LIKE_REPORT_URL = (
    "https://api.live.bilibili.com/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3"
)
LIKE_INTERACT_URL = (
    "https://api.live.bilibili.com/xlive/web-ucenter/v1/interact/likeInteract"
)
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
HOMEPAGE_URL = "https://www.bilibili.com/"
BUVID_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

LIVE_STARTED_AT_KEY = "live_started_at"
"""归一化后的开播时刻（epoch 秒）写在房间信息 dict 的这个键上。

各接口给的形态不同（H5 是秒级时间戳、``get_info`` 是北京时间字符串），统一由
:func:`parse_live_started_at` 归一化，上层（client / 两版 UI）只认这一个键。
"""

_LIVE_TIME_PLACEHOLDER = "0000-00-00 00:00:00"
"""``room/v1/Room/get_info`` 未开播时给的开播时间占位值。"""

_BEIJING_TZ = timezone(timedelta(hours=8))
"""B 站 ``live_time`` 字符串是**北京时间**：解析时必须显式附加时区，否则非东八区
环境算出来的「已播时长」会整体偏移 8 小时。"""

LIVE_STARTED_FUTURE_TOLERANCE_S = 120.0
"""开播时刻最多允许比本地时间晚这么多秒（容忍轻微时钟偏差），再多视为异常丢弃。"""


def parse_live_started_at(raw: object, *, now: Optional[float] = None) -> Optional[int]:
    """接口给的开播时刻 → epoch 秒；占位值 / 非法值 / 明显在未来 → ``None``（纯函数）。

    实测（2026-09-22，本地 17 个直播间）两种形态：

    - H5 ``getH5InfoByRoom`` → ``room_info.live_start_time``：**秒级时间戳**
      （直播中 ``1790066140``、未开播与轮播均为 ``0``）；
    - ``room/v1/Room/get_info`` → ``live_time``：**北京时间字符串**
      （直播中 ``"2026-09-22 16:35:40"``、未开播为 ``"0000-00-00 00:00:00"``）。
      （``getRoomInfoOld`` 实测返回 ``code=-400``，不可用，故不作回退源。）

    两者换算一致（上述两值对应同一时刻）。取不到一律 ``None``，由调用方回退
    「本地观测到的开播时刻」——**不要在这里编造时间**，否则时长会凭空开始计时。
    """
    if isinstance(raw, bool):  # bool 是 int 的子类，先挡掉
        return None
    moment: Optional[float] = None
    if isinstance(raw, (int, float)):
        moment = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or text == _LIVE_TIME_PLACEHOLDER:
            return None
        try:
            moment = float(text)  # 少数接口会把时间戳装在字符串里
        except ValueError:
            try:
                stamp = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None  # 含全零占位在内的非法日期（strptime 对 0000 年直接失败）
            moment = stamp.replace(tzinfo=_BEIJING_TZ).timestamp()
    if moment is None or moment <= 0:
        return None
    current = time.time() if now is None else float(now)
    if moment > current + LIVE_STARTED_FUTURE_TOLERANCE_S:
        return None  # 超出时钟偏差的「未来开播」必是异常数据
    return int(moment)


MEDAL_PANEL_PAGE_SIZE = 10
"""粉丝勋章列表分页大小；接口上限为 10，超出会报参数异常。"""

MEDAL_PANEL_MAX_PAGES = 50
"""分页安全上限，避免服务端异常时无限翻页。"""

MEDAL_WEB_LOCATION = "444.260"
"""粉丝牌任务接口的 web_location 埋点值（对齐网页端调用）。"""

LIKE_WEB_LOCATION = "444.8"
"""点赞接口的 web_location 埋点值。"""

DEFAULT_DANMU_HOST = "broadcastlv.chat.bilibili.com"
DEFAULT_WSS_PORT = 443

# 触发风控/需要登录态的典型错误码
RISK_CONTROL_CODES = {-352, -403, -412}

# WBI 签名用的固定置换表（公开算法）
WBI_KEY_INDEX_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
]

WBI_KEY_TTL = 12 * 3600


class ApiError(RuntimeError):
    """接口返回非 0 的 code 或响应不符合预期。"""

    def __init__(self, action: str, code: Any, message: str = ""):
        self.action = action
        self.code = code
        self.message = message  # 保留原始 message，供上层按需展示/映射
        super().__init__(f"{action}失败: code={code} message={message}")


# 发送弹幕接口返回码 -> 中文提示（供 GUI 与日志复用）
SEND_ERROR_HINTS = {
    -101: "账号未登录：请在浏览器登录 B 站后用「获取Cookie」重新获取已登录的 Cookie",
    -111: "csrf 校验失败：Cookie 中的 bili_jct 与请求不一致，请重新获取 Cookie",
    -400: "请求参数错误",
    1003212: "弹幕内容超出长度限制",
    10031: "发送频率过快，请稍后再试",
    10203: "服务端未接受该内容/表情（10203）：表情包需按 emoticon_unique 发送，"
           "若为普通弹幕请稍后重试或更换内容",
    -352: "触发风控，请稍后再试或检查 Cookie",
    -403: "触发风控（无权操作）",
    -412: "触发风控（请求被拦截）",
}


def describe_send_error(code: Any, message: str = "") -> str:
    """把发送弹幕接口的返回码映射为中文可读提示（纯函数，供 API 与 GUI 复用）。"""
    try:
        key = int(code)
    except (TypeError, ValueError):
        key = None
    hint = SEND_ERROR_HINTS.get(key)
    if hint:
        return f"{hint}（code={code}）"
    if message:
        return f"发送失败：{message}（code={code}）"
    return f"发送失败（code={code}）"


# 点赞接口返回码 -> 中文提示（供 GUI 与日志复用）
LIKE_ERROR_HINTS = {
    -101: "账号未登录：请在浏览器登录 B 站后用「获取Cookie」重新获取已登录的 Cookie",
    -111: "csrf 校验失败：Cookie 中的 bili_jct 与请求不一致，请重新获取 Cookie",
    -400: "请求参数错误",
    -352: "触发风控，请稍后再试或检查 Cookie",
    -403: "触发风控（无权操作）",
    -412: "触发风控（请求被拦截）",
}


def describe_like_error(code: Any, message: str = "") -> str:
    """把点赞接口的返回码映射为中文可读提示（纯函数，供 API 与 GUI 复用）。"""
    try:
        key = int(code)
    except (TypeError, ValueError):
        key = None
    hint = LIKE_ERROR_HINTS.get(key)
    if hint:
        return f"{hint}（code={code}）"
    if message:
        return f"点赞失败：{message}（code={code}）"
    return f"点赞失败（code={code}）"


def parse_room_emoticon_packages(data: Any) -> List[Dict[str, Any]]:
    """把 GetEmoticons 的 data 字段归一化为「表情包」列表（纯函数，便于单测）。

    实测响应为 ``{"data": [{"pkg_name": ..., "emoticons": [...]}, ...]}``
    （data.data 是表情包数组，每个包里再套 ``emoticons``），**保持服务端返回的
    包顺序**，与直播间内表情面板的分页一致；同时兼容 data 直接就是表情数组的
    形态（归入单个「全部表情」包）。

    每个表情包为 ``{"name", "id", "type", "cover", "emoticons": [表情, ...]}``，
    其中表情为 ``{"unique", "id", "trigger", "text", "url", "width", "height"}``：
    - ``trigger``：接口给的触发关键词原文（发送时作为 ``msg``）；
    - ``text``：用于界面展示的 ``[触发词]`` 形式。

    仅保留可用项（``perm`` 为 1 或缺失）并按 unique 去重（跨包去重）；
    无可用表情的空包被剔除。
    """
    if isinstance(data, dict):
        items = data.get("data")
    else:
        items = data
    if not isinstance(items, list):
        items = []
    entries = [e for e in items if isinstance(e, dict)]
    # 没有任何一项带 emoticons 列表 → 视为扁平的「单个表情」数组
    if not any(isinstance(e.get("emoticons"), list) for e in entries):
        emoticons = _normalize_emoticons(entries)
        if not emoticons:
            return []
        return [{"name": "全部表情", "id": 0, "type": 0, "cover": "",
                 "emoticons": emoticons}]

    packages: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in entries:
        raw_list = entry.get("emoticons")
        emoticons = _dedup_emoticons(
            _normalize_emoticons(raw_list if isinstance(raw_list, list) else []), seen)
        if not emoticons:
            continue
        packages.append({
            "name": str(entry.get("pkg_name") or "").strip() or "表情",
            "id": _as_int(entry.get("pkg_id")),
            "type": _as_int(entry.get("pkg_type")),
            "cover": str(entry.get("current_cover") or "").strip(),
            "emoticons": emoticons,
        })
    return packages


def _normalize_emoticons(raw_items: Any) -> List[Dict[str, Any]]:
    """把原始表情项逐条归一化，丢弃不可用（``perm`` 非 1）或字段缺失的项。"""
    result: List[Dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return result
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        perm = item.get("perm")
        if perm is not None:
            try:
                if int(perm) != 1:
                    continue
            except (TypeError, ValueError):
                continue
        unique = str(item.get("emoticon_unique") or "").strip()
        url = str(item.get("url") or "").strip()
        if not unique or not url:
            continue
        trigger = str(item.get("emoji") or item.get("descript") or "").strip() or unique
        display = trigger if (trigger.startswith("[") and trigger.endswith("]")
                              and len(trigger) > 2) else f"[{trigger}]"
        result.append({
            "unique": unique,
            "id": _as_int(item.get("emoticon_id") or item.get("id")),
            "trigger": trigger,
            "text": display,
            "url": url,
            "width": _as_int(item.get("width")),
            "height": _as_int(item.get("height")),
            "bulge_display": _as_int(item.get("bulge_display")),
        })
    return result


def _dedup_emoticons(items: List[Dict[str, Any]], seen: set) -> List[Dict[str, Any]]:
    """按 ``unique`` 跨包去重（同一表情可能出现在多个包里）。"""
    result: List[Dict[str, Any]] = []
    for item in items:
        unique = item["unique"]
        if unique in seen:
            continue
        seen.add(unique)
        result.append(item)
    return result


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class BilibiliLiveAPI:
    """封装匿名抓取所需的少量公开接口，所有方法都应在事件循环内调用。"""

    def __init__(self, session: aiohttp.ClientSession, cookie: Optional[str] = None):
        self.session = session
        self._cookie = (cookie or "").strip()
        self.buvid3 = ""
        self.uid = 0
        self._wbi_key = ""
        self._wbi_key_time = 0.0
        match = re.search(r"buvid3=([^;]+)", self._cookie)
        if match:
            self.buvid3 = match.group(1).strip()

    @property
    def has_cookie(self) -> bool:
        return bool(self._cookie)

    @property
    def csrf(self) -> str:
        """Cookie 中的 bili_jct（发弹幕等写操作鉴权用）；缺失返回空串。"""
        match = re.search(r"bili_jct=([^;]+)", self._cookie)
        return match.group(1).strip() if match else ""

    @property
    def logged_in(self) -> bool:
        """是否已登录（据 nav 接口拿到的 uid 判断）。"""
        return self.uid > 0

    async def refresh_login(self) -> None:
        """登录状态变化后（如 GUI 获取到新 cookie）刷新登录 uid。"""
        if self._cookie:
            await self._fetch_uid()

    def set_cookie(self, cookie: str) -> None:
        """运行时更新 cookie（GUI 获取浏览器 Cookie 后调用），并刷新 buvid3。"""
        self._cookie = (cookie or "").strip()
        match = re.search(r"buvid3=([^;]+)", self._cookie)
        if match:
            self.buvid3 = match.group(1).strip()
        logger.info("会话 cookie 已更新（长度 %d，含 buvid3=%s）",
                    len(self._cookie), "是" if self.buvid3 else "否")

    def _headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://live.bilibili.com/",
            "Origin": "https://live.bilibili.com",
        }
        cookie = self._cookie
        if self.buvid3 and "buvid3=" not in cookie:
            cookie = f"{cookie}; " if cookie else ""
            cookie += f"buvid3={self.buvid3}"
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def ws_headers(self) -> Dict[str, str]:
        """WebSocket 握手用的请求头。"""
        return {"User-Agent": USER_AGENT}

    def image_headers(self) -> Dict[str, str]:
        """下载图片资源用的请求头。

        带 Referer/Origin：表情等 CDN 资源有防盗链校验，缺 Referer 时部分
        资源会返回 403（表现为对应直播间的表情全部显示为文字）。
        """
        return {"User-Agent": USER_AGENT,
                "Referer": "https://live.bilibili.com/",
                "Origin": "https://live.bilibili.com"}

    @staticmethod
    def _log_api_result(label: str, started: float, payload: Dict[str, Any]) -> None:
        """成功拿到 JSON 后记一条（含 B 站业务 code 与耗时，便于监测接口是否变慢/变错）。"""
        code = payload.get("code")
        elapsed = (time.monotonic() - started) * 1000
        if code in (None, 0):
            logger.info("接口 %s 返回（code=%s，%.0fms）", label, code, elapsed)
        else:
            logger.warning("接口 %s 返回异常（code=%s，message=%s，%.0fms）",
                           label, code, payload.get("message"), elapsed)

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        label = api_log_label(url, params)
        started = time.monotonic()
        try:
            async with self.session.get(url, params=params,
                                        headers=self._headers()) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("接口 %s 请求失败（%.0fms）：%s", label,
                           (time.monotonic() - started) * 1000, exc)
            raise
        if not isinstance(data, dict):
            raise ApiError(f"请求 {url}", "格式异常", f"响应不是 JSON 对象: {str(data)[:120]}")
        self._log_api_result(label, started, data)
        return data

    async def _post_form_json(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """表单（application/x-www-form-urlencoded）POST，返回 JSON 对象。

        aiohttp 在 data 传 dict 时会自动设置表单 Content-Type。
        """
        label = api_log_label(url, data)
        started = time.monotonic()
        try:
            async with self.session.post(url, data=data,
                                         headers=self._headers()) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("接口 %s 请求失败（%.0fms）：%s", label,
                           (time.monotonic() - started) * 1000, exc)
            raise
        if not isinstance(payload, dict):
            raise ApiError(f"请求 {url}", "格式异常", f"响应不是 JSON 对象: {str(payload)[:120]}")
        self._log_api_result(label, started, payload)
        return payload

    async def _post_query_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """POST 请求（参数放 query、body 为空）返回 JSON 对象。

        点赞 likeReportV3 等接口把业务参数（含 WBI 签名）放在 query、body 为空，
        与 :meth:`_post_form_json`（表单 body）区分开。
        """
        label = api_log_label(url, params)
        started = time.monotonic()
        try:
            async with self.session.post(url, params=params,
                                         headers=self._headers()) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("接口 %s 请求失败（%.0fms）：%s", label,
                           (time.monotonic() - started) * 1000, exc)
            raise
        if not isinstance(payload, dict):
            raise ApiError(f"请求 {url}", "格式异常", f"响应不是 JSON 对象: {str(payload)[:120]}")
        self._log_api_result(label, started, payload)
        return payload

    async def init_session_info(self) -> None:
        """尽量拿到 buvid3（弹幕接口风控需要）和登录 uid；失败不致命，只打日志。"""
        if not self.buvid3:
            self.buvid3 = await self._fetch_buvid_from_homepage() or await self._fetch_buvid_from_spi()
            if self.buvid3:
                logger.info("已获取游客 buvid3=%s...", self.buvid3[:8])
            else:
                logger.warning("未能自动获取 buvid3，弹幕接口可能被风控拦截")
        if self._cookie:
            await self._fetch_uid()

    async def _fetch_buvid_from_homepage(self) -> str:
        try:
            async with self.session.get(HOMEPAGE_URL, headers={"User-Agent": USER_AGENT}) as resp:
                buvid = resp.cookies.get("buvid3")
                if buvid and buvid.value:
                    return buvid.value
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("从主站首页获取 buvid3 失败: %s", exc)
        return ""

    async def _fetch_buvid_from_spi(self) -> str:
        try:
            data = await self._get_json(BUVID_SPI_URL, {})
            if data.get("code") == 0:
                return (data.get("data") or {}).get("b_3") or ""
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError) as exc:
            logger.debug("通过 spi 接口获取 buvid3 失败: %s", exc)
        return ""

    async def _fetch_uid(self) -> None:
        try:
            data = await self._get_json(NAV_URL, {})
            if data.get("code") == 0:
                info = data.get("data") or {}
                self.uid = int(info.get("mid") or 0) if info.get("isLogin") else 0
                logger.info("登录身份 uid=%d", self.uid)
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError, ValueError) as exc:
            logger.debug("获取登录 uid 失败，按未登录处理: %s", exc)
            self.uid = 0

    async def _ensure_wbi_key(self) -> None:
        if self._wbi_key and time.time() - self._wbi_key_time < WBI_KEY_TTL:
            return
        data = await self._get_json(NAV_URL, {})
        wbi_img = (data.get("data") or {}).get("wbi_img") or {}
        img_url = wbi_img.get("img_url") or ""
        sub_url = wbi_img.get("sub_url") or ""
        if not img_url or not sub_url:
            raise ApiError("获取 wbi 密钥", data.get("code"), data.get("message", ""))
        raw = img_url.rpartition("/")[2].partition(".")[0] + sub_url.rpartition("/")[2].partition(".")[0]
        self._wbi_key = "".join(raw[i] for i in WBI_KEY_INDEX_TABLE if i < len(raw))
        self._wbi_key_time = time.time()

    def _add_wbi_sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._wbi_key:
            return params
        wts = str(int(time.time()))
        to_sign = {**params, "wts": wts}
        to_sign = dict(sorted(to_sign.items()))
        to_sign = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in to_sign.items()}
        sign_str = urllib.parse.urlencode(to_sign) + self._wbi_key
        w_rid = hashlib.md5(sign_str.encode("utf-8")).hexdigest()
        return {**params, "wts": wts, "w_rid": w_rid}

    async def get_room_info(self, room_id: int) -> Dict[str, Any]:
        """查询房间信息（支持短号），返回 data 字段：room_id / uid / title / live_status 等。"""
        data = await self._get_json(ROOM_INFO_URL, {"room_id": room_id})
        if data.get("code") != 0:
            raise ApiError("获取房间信息", data.get("code"), data.get("message", ""))
        info = data.get("data") or {}
        if not info.get("room_id"):
            raise ApiError("获取房间信息", data.get("code"), "响应缺少 room_id")
        # 开播时刻归一化成 epoch 秒（本接口给的是北京时间字符串，未开播为全零占位）
        info[LIVE_STARTED_AT_KEY] = parse_live_started_at(info.get("live_time"))
        logger.debug("房间 %s 开播时刻（get_info 的 live_time=%r）→ %s", info.get("room_id"),
                     info.get("live_time"), format_live_mark(info.get(LIVE_STARTED_AT_KEY)))
        return info

    async def get_live_stream_urls(self, room_id: int, *,
                                   qn: int = PREVIEW_DEFAULT_QUALITY,
                                   pwd: str = ""
                                   ) -> Dict[str, Any]:
        """取直播推流直链（http-flv 与 hls 各一条，供本地预览播放）。

        - ``room_id`` 需为**真实房间号**（短号先经 :meth:`get_room_info` 换算）；
        - ``qn`` 为清晰度（80 流畅 / 150 高清 / 250 超清 / 400 蓝光 / 10000 原画；高清晰度需登录）；
        - ``pwd`` 为加密（密码）房间的密码，非空时随请求一起带上（服务端已解锁时也无害）；
        - 直链**有时效**（``expires`` 参数），过期后需重新调用换源；
        - 两种格式互不影响：某一格式失败只写进 ``errors``，预览可在两者间回退。

        返回 ``{"flv", "hls", "qn", "accept_quality", "errors"}``。

        **注意**：这里返回的 ``accept_quality`` 是该接口的「清晰度编号」（实测 ``['4']``），
        **不是 qn**，不要拿它过滤清晰度；要判断某房间能用哪些档位请用
        :meth:`get_stream_qualities`（新接口的 ``accept_qn``）。
        """
        result: Dict[str, Any] = {"flv": None, "hls": None, "qn": int(qn),
                                  "accept_quality": [], "errors": {}}
        for key, platform in LIVE_STREAM_PLATFORMS:
            params = {"cid": int(room_id), "platform": platform, "qn": int(qn)}
            if pwd:
                params["pwd"] = str(pwd)   # 加密房间：带着密码请求（已解锁时也无害）
            try:
                payload = await self._get_json(PLAY_URL, params)
            except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                result["errors"][key] = str(exc)
                continue
            if payload.get("code") != 0:
                result["errors"][key] = (f"code={payload.get('code')} "
                                         f"{payload.get('message')}")
                continue
            data = payload.get("data") or {}
            durl = data.get("durl") or []
            url = durl[0].get("url") if durl and isinstance(durl[0], dict) else None
            if not url:
                result["errors"][key] = "响应缺少 durl[0].url"
                continue
            result[key] = str(url)
            accept = data.get("accept_quality")
            if not result["accept_quality"] and isinstance(accept, list) and accept:
                result["accept_quality"] = accept
        return result

    async def get_stream_info(self, room_id: int, *, pwd: str = "") -> Dict[str, Any]:
        """取拉流前置信息：可用档位 + 是否加密房间 + 密码是否已通过。

        返回 ``{"qualities", "encrypted", "pwd_verified", "error"}``；失败（未开播、网络
        异常、接口报错）时 ``qualities`` 为空、``error`` 给出原因，由调用方按兜底档位处理，
        **不抛异常**。``pwd`` 非空时一并带上（密码房在服务端解锁前后都可能需要它）。
        """
        params: Dict[str, Any] = {"room_id": int(room_id), "protocol": "0,1",
                                  "format": "0,1,2", "codec": "0,1",
                                  "qn": MAX_PREVIEW_QUALITY, "platform": "web",
                                  "ptype": 8}
        if pwd:
            params["pwd"] = str(pwd)
        try:
            payload = await self._get_json(ROOM_PLAY_INFO_URL, params)
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("获取房间 %s 拉流信息失败：%s", room_id, exc)
            return {"qualities": (), "encrypted": None, "pwd_verified": None,
                    "error": str(exc)}
        if payload.get("code") != 0:
            logger.debug("获取房间 %s 拉流信息返回 code=%s", room_id, payload.get("code"))
            return {"qualities": (), "encrypted": None, "pwd_verified": None,
                    "error": f"code={payload.get('code')} {payload.get('message')}"}
        qualities, encrypted, verified = parse_room_play_info(payload)
        return {"qualities": qualities, "encrypted": encrypted,
                "pwd_verified": verified, "error": ""}

    async def get_stream_qualities(self, room_id: int) -> Tuple[int, ...]:
        """该房间**可用**的清晰度（``accept_qn``，降序）；拿不到时返回空元组。

        等价于 :meth:`get_stream_info` 的 ``qualities``（保留这个入口便于既有调用与测试）。
        """
        info = await self.get_stream_info(room_id)
        return tuple(info.get("qualities") or ())

    async def verify_room_pwd(self, room_id: int, pwd: str) -> bool:
        """在服务端校验加密（密码）直播间的密码；成功返回 True。

        密码房要先让服务端记住「本会话已解锁」，之后拉流才给地址。该接口来自社区资料
        （官方文档未收录），所以**失败不抛异常**：返回 False 由上层提示密码可能不对，
        但**仍会带 ``pwd`` 重试拉流**——两条路一起走，命中率最高。
        """
        pwd = str(pwd or "").strip()
        if not pwd:
            return False
        params = {"room_id": int(room_id), "pwd": pwd}
        try:
            payload = await self._get_json(VERIFY_ROOM_PWD_URL, params)
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("校验房间 %s 密码失败：%s", room_id, exc)
            return False
        ok = payload.get("code") == 0
        if not ok:
            logger.info("房间 %s 密码未通过（code=%s）", room_id, payload.get("code"))
        return ok

    async def report_watch_heartbeat(
            self, room_id: int,
            next_interval: int = LIVE_HEARTBEAT_DEFAULT_INTERVAL) -> int:
        """上报一次直播**观看时长**心跳（**写操作**），返回下次该隔多少秒再上报。

        对应网页端 ``webHeartBeat``：只有 ``hb``（明文 ``间隔|真实房间号|1|0`` 的 base64）
        与 ``pf=web`` 两个参数，服务端用 ``data.next_interval`` 告知下一次的时间。
        需要已登录 Cookie——未登录没有账号可计时长，这个请求也就没有意义。

        这是**模拟网页端行为**的写操作，可能触发风控，仅供 ``preview.watch_time``
        显式开启后使用。
        """
        if not self.has_cookie:
            raise ApiError("观看时长上报", -101,
                           "未提供 cookie，请先用「获取Cookie」获取已登录的 Cookie")
        params = {"hb": heartbeat_hb(room_id, next_interval), "pf": "web"}
        payload = await self._get_json(LIVE_HEARTBEAT_URL, params)
        code = payload.get("code")
        if code != 0:
            raise ApiError("观看时长上报", code,
                           payload.get("message") or payload.get("msg") or "")
        return parse_next_interval(payload)

    async def get_full_room_info(self, room_id: int) -> Dict[str, Any]:
        """查询房间信息，直播标题取 get_info 与 getH5InfoByRoom 中更完整者。

        room/v1/Room/get_info 在部分直播间会返回被截断的标题，H5 接口返回
        的标题是完整的；以更长的那个为准，其余字段仍来自 get_info。
        H5 接口失败不影响主流程，回退为 get_info 的结果。
        """
        info = await self.get_room_info(room_id)
        try:
            data = await self._get_json(
                ROOM_H5_INFO_URL, {"room_id": info.get("room_id") or room_id}
            )
            if data.get("code") == 0:
                h5_data = data.get("data") or {}
                # 新版 H5 结构标题在 room_info 下，旧版在 data 下，两处都试
                h5_title = str(h5_data.get("title")
                               or (h5_data.get("room_info") or {}).get("title") or "").strip()
                current = str(info.get("title") or "").strip()
                if h5_title and len(h5_title) > len(current):
                    info["title"] = h5_title
                # 开播时刻：H5 给的是**秒级时间戳**（无时区歧义），优先于 get_info 的
                # 北京时间字符串；未开播/轮播两处分别给 0 与全零占位，都解析为 None，
                # 此时保留 get_info 的结果（同样是 None）。
                h5_room = h5_data.get("room_info") or {}
                h5_started = parse_live_started_at(
                    h5_room.get("live_start_time") or h5_data.get("live_start_time"))
                if h5_started is not None:
                    info[LIVE_STARTED_AT_KEY] = h5_started
                    logger.debug("房间 %s 开播时刻改用 H5 时间戳：%s",
                                 info.get("room_id"), format_live_mark(h5_started))
                else:
                    logger.debug("房间 %s 的 H5 未给出开播时刻，沿用 get_info 的结果：%s",
                                 info.get("room_id"),
                                 format_live_mark(info.get(LIVE_STARTED_AT_KEY)))
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError) as exc:
            logger.debug("获取 H5 房间信息失败，沿用 get_info 标题: %s", exc)
        return info

    async def get_guard_count(self, room_id: int, anchor_uid: int = 0) -> int:
        """查询直播间舰长总数；失败返回 -1 表示未知（不影响监听）。

        v2 接口需要主播 uid（ruid），uid 缺失或错误时 num 可能为 0。
        """
        params: Dict[str, Any] = {"roomid": room_id, "page": 1, "page_size": 29}
        if anchor_uid:
            params["ruid"] = anchor_uid
        try:
            data = await self._get_json(GUARD_TOP_LIST_URL, params)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("获取舰长数失败 room=%s: %s", room_id, exc)
            return -1
        if data.get("code") != 0:
            logger.debug("获取舰长数失败 room=%s: code=%s", room_id, data.get("code"))
            return -1
        info = (data.get("data") or {}).get("info") or {}
        try:
            return int(info.get("num") or 0)
        except (TypeError, ValueError):
            return -1

    async def get_medals(self) -> List[Dict[str, Any]]:
        """分页拉取账号持有的粉丝勋章（等级 / 亲密度 / 主播 / 房间等）。

        需已登录（Cookie 含 SESSDATA）；未提供 Cookie 时抛 ApiError(-101)，
        接口返回非 0 抛 ApiError。跨页按 ``medal_id`` 去重，``special_list``
        （当前佩戴）排在前面。
        """
        if not self.has_cookie:
            raise ApiError("获取粉丝牌", -101,
                           "未提供 cookie，请先用「获取Cookie」获取已登录的 Cookie")
        medals: List[Dict[str, Any]] = []
        page = 1
        while page <= MEDAL_PANEL_MAX_PAGES:
            data = await self._get_json(
                MEDAL_PANEL_URL,
                {"page": page, "page_size": MEDAL_PANEL_PAGE_SIZE},
            )
            if data.get("code") != 0:
                raise ApiError("获取粉丝牌", data.get("code"), data.get("message", ""))
            info = data.get("data") or {}
            page_medals = parse_medal_panel(info)
            medals.extend(page_medals)
            page_info = info.get("page_info") or {}
            total_page = _as_int(page_info.get("total_page"))
            has_raw = bool(info.get("list") or info.get("special_list"))
            if not has_raw or not page_medals:
                break
            if total_page and page >= total_page:
                break
            page += 1
        return dedupe_medals(medals)

    async def get_medal_task_info(self, target_id: int) -> Dict[str, Any]:
        """查询指定主播（``target_id`` = 主播 uid）的粉丝牌任务信息。

        返回 ``{"target_id", "tasks", "free_intimacy", "reach_free_intimacy_limit"}``，
        其中 ``tasks`` 为 ``medal_tasks.normalize_task`` 归一化后的任务列表
        （``jump_type`` / ``title`` / ``current`` / ``limit`` / ``is_done``），
        任务上限随粉丝牌等级变化、每日刷新，一律以接口为准。

        ``reach_free_intimacy_limit`` 为真表示**该粉丝灯牌刚点亮**：服务端暂时不接受
        点赞 / 发弹幕这类免费任务产生的亲密度（官方规则：熄灭状态下靠「点赞 30 次 /
        发弹幕 10 条」点亮勋章时，这两种行为仅点亮勋章、不获得亲密度）。但它**不代表
        任务不该做**——长时间不做任务灯牌会熄灭，做任务正是为了点亮并维持它，故调用方
        应照常执行任务，只把原因说明给用户（见 ``MedalTaskRunner._complete_locked``）。

        需已登录且 Cookie 含 ``bili_jct``（csrf）；失败抛 ApiError。
        """
        if not self.has_cookie:
            raise ApiError("获取粉丝牌任务", -101,
                           "未提供 cookie，请先用「获取Cookie」获取已登录的 Cookie")
        if not self.csrf:
            raise ApiError("获取粉丝牌任务", -111,
                           "Cookie 中缺少 bili_jct，请重新「获取Cookie」")
        if not target_id:
            raise ApiError("获取粉丝牌任务", -400, "缺少主播 uid")
        params = {
            "csrf": self.csrf,
            "target_id": int(target_id),
            "web_location": MEDAL_WEB_LOCATION,
        }
        data = await self._get_json(MEDAL_TASK_URL, params)
        if data.get("code") != 0:
            raise ApiError("获取粉丝牌任务", data.get("code"), data.get("message", ""))
        info = data.get("data") or {}
        return {
            "target_id": int(target_id),
            "tasks": parse_task_info_list(info),
            "free_intimacy": _as_int(info.get("free_intimacy")),
            "reach_free_intimacy_limit": bool(info.get("reach_free_intimacy_limit")),
        }

    async def like_room(self, room_id: int, anchor_uid: int, *,
                        click_time: int = 1) -> Dict[str, Any]:
        """给直播间点赞（写操作，需已登录且 Cookie 含 bili_jct）。

        优先 ``likeReportV3``（WBI 签名、参数在 query、body 为空）；仅在**非风控类**
        失败（含网络异常）时回退免签名的 ``likeInteract``。风控码
        （``-352/-403/-412``）与未登录/缺 csrf 直接抛出，不再重试，避免加剧风控。
        """
        if not self.has_cookie:
            raise ApiError("点赞", -101,
                           "未提供 cookie，请先用「获取Cookie」获取已登录的 Cookie")
        csrf = self.csrf
        if not csrf:
            raise ApiError("点赞", -111, "Cookie 中缺少 bili_jct，请重新「获取Cookie」")
        clicks = max(1, int(click_time))
        try:
            return await self._like_via_report(room_id, anchor_uid, clicks, csrf)
        except ApiError as exc:
            if exc.code in RISK_CONTROL_CODES or exc.code in (-101, -111):
                raise
            logger.debug("likeReportV3 失败（code=%s），回退 likeInteract", exc.code)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("likeReportV3 网络异常（%s），回退 likeInteract", exc)
        return await self._like_via_interact(room_id, csrf)

    async def _like_via_report(self, room_id: int, anchor_uid: int,
                               click_time: int, csrf: str) -> Dict[str, Any]:
        """likeReportV3：参数（含 WBI 签名）放 query，body 为空。"""
        try:
            await self._ensure_wbi_key()
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError) as exc:
            logger.debug("获取 wbi 密钥失败，点赞将不带签名: %s", exc)
        params: Dict[str, Any] = {
            "click_time": click_time,
            "room_id": int(room_id),
            "uid": int(self.uid),
            "anchor_id": int(anchor_uid),
            "web_location": LIKE_WEB_LOCATION,
            "csrf": csrf,
        }
        params = self._add_wbi_sign(params)
        payload = await self._post_query_json(LIKE_REPORT_URL, params)
        code = payload.get("code")
        if code != 0:
            raise ApiError("点赞", code, payload.get("message") or payload.get("msg") or "")
        return payload

    async def _like_via_interact(self, room_id: int, csrf: str) -> Dict[str, Any]:
        """likeInteract：免 WBI 的网页端点赞备选（单次点击）。"""
        data = {
            "platform": "pc",
            "roomid": int(room_id),
            "csrf": csrf,
            "csrf_token": csrf,
        }
        payload = await self._post_form_json(LIKE_INTERACT_URL, data)
        code = payload.get("code")
        if code != 0:
            raise ApiError("点赞", code, payload.get("message") or payload.get("msg") or "")
        return payload

    async def get_room_id_by_uid(self, mid: int) -> int:
        """通过主播 uid 查询其直播间房间号（用于个人空间网址添加直播间）。

        用户可能没有开通直播间，此时抛 ApiError。
        """
        if not mid:
            raise ApiError("通过 uid 查询直播间", "参数错误", "uid 为空")
        data = await self._get_json(ROOM_INFO_OLD_URL, {"mid": mid})
        if data.get("code") != 0:
            raise ApiError("通过 uid 查询直播间", data.get("code"), data.get("message", ""))
        roomid = int((data.get("data") or {}).get("roomid") or 0)
        if not roomid:
            raise ApiError("通过 uid 查询直播间", data.get("code"), "该用户没有开通直播间")
        return roomid

    async def get_anchor_name(self, uid: int) -> str:
        """查询主播昵称（失败返回空串，不影响监听）。"""
        if not uid:
            return ""
        try:
            data = await self._get_json(ANCHOR_INFO_URL, {"uid": uid})
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError):
            return ""
        if data.get("code") != 0:
            return ""
        return ((data.get("data") or {}).get("info") or {}).get("uname") or ""

    async def get_danmu_info(self, room_id: int) -> Dict[str, Any]:
        """获取弹幕服务器列表与连接 token，wbi 签名失败时降级为不带签名尝试。"""
        try:
            await self._ensure_wbi_key()
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError) as exc:
            logger.warning("获取 wbi 密钥失败，将不带签名请求弹幕服务器信息: %s", exc)
        params = self._add_wbi_sign({"id": room_id, "type": 0})
        data = await self._get_json(DANMU_INFO_URL, params)
        code = data.get("code")
        if code != 0:
            raise ApiError("获取弹幕服务器信息", code, data.get("message", ""))
        info = data.get("data") or {}
        if not info.get("token") or not info.get("host_list"):
            raise ApiError("获取弹幕服务器信息", code, "响应缺少 token 或 host_list")
        return info

    async def get_dm_config(self, room_id: int) -> Dict[str, Any]:
        """查询当前用户在指定直播间可用的弹幕颜色/模式（尽力而为，失败返回 {}）。

        未登录也可查询，但仅「白色 + 滚动」可用，故 GUI 需在登录后再刷新。
        """
        try:
            data = await self._get_json(DM_CONFIG_URL, {"room_id": int(room_id)})
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError) as exc:
            logger.debug("获取弹幕配置失败 room=%s: %s", room_id, exc)
            return {}
        if data.get("code") != 0:
            logger.debug("获取弹幕配置失败 room=%s: code=%s", room_id, data.get("code"))
            return {}
        info = data.get("data")
        return info if isinstance(info, dict) else {}

    async def get_room_emoticons(self, room_id: int) -> List[Dict[str, Any]]:
        """查询当前用户在指定直播间可用的表情包（含直播间专属），失败返回 []。

        对应网页端弹幕输入框旁的「表情」面板：返回按**表情包**分组的列表
        （``parse_room_emoticon_packages`` 的结构），供 GUI 按包分页展示。
        未登录也能拿到公开表情，但专属表情需账号满足解锁条件（``perm`` 为 1），
        故 GUI 应登录后再取。
        """
        try:
            data = await self._get_json(
                ROOM_EMOTICON_URL, {"platform": "pc", "room_id": int(room_id)})
        except (aiohttp.ClientError, asyncio.TimeoutError, ApiError) as exc:
            logger.debug("获取直播间表情失败 room=%s: %s", room_id, exc)
            return []
        if data.get("code") != 0:
            logger.debug("获取直播间表情失败 room=%s: code=%s", room_id, data.get("code"))
            return []
        return parse_room_emoticon_packages(data.get("data"))

    async def send_danmaku(self, room_id: int, msg: str, *, mode: int = 1,
                           color: int = 16777215, fontsize: int = 25,
                           reply_mid: int = 0, reply_uname: str = "",
                           replay_dmid: str = "",
                           emoticon: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送直播弹幕（写操作，需已登录且 Cookie 含 bili_jct）。

        未登录 / 缺少 csrf / 接口返回非 0 均抛 ApiError（code 为接口返回码，
        本地前置校验用 -101 未登录、-111 缺 csrf 表示）。

        传入 ``emoticon``（``get_room_emoticons`` 返回的条目）时按表情包弹幕
        发送：``dm_type=1``，``msg`` 用表情唯一标识 ``emoticon_unique``
        （即网页端 DOM 的 ``data-file-id``；传触发词会被服务端拒绝），并附
        ``emoticonOptions``（``emoticon_unique`` / ``url`` / ``width`` /
        ``height`` / ``in_player_area`` / ``bulge_display``）。不传时行为完全不变。
        """
        if not self._cookie:
            raise ApiError("发送弹幕", -101, "未提供 cookie，无法发送弹幕")
        csrf = self.csrf
        if not csrf:
            raise ApiError("发送弹幕", -111,
                           "Cookie 中缺少 bili_jct，请用「获取Cookie」重新获取已登录的 Cookie")
        emoticon_unique = ""
        if emoticon:
            emoticon_unique = str(emoticon.get("unique") or "").strip()
            if not emoticon_unique:
                raise ApiError("发送表情包", -400, "缺少表情标识（emoticon_unique）")
        data: Dict[str, Any] = {
            "csrf": csrf,
            "csrf_token": csrf,
            "roomid": int(room_id),
            "msg": msg,
            "rnd": int(time.time()),
            "fontsize": int(fontsize),
            "color": int(color),
            "mode": int(mode),
            "bubble": 0,
            "room_type": 0,
            "jumpfrom": 0,
            "statistics": '{"appId":100,"platform":5}',
        }
        if reply_mid:
            data["reply_mid"] = int(reply_mid)
        if reply_uname:
            data["reply_uname"] = reply_uname
        if replay_dmid:
            data["replay_dmid"] = str(replay_dmid)
        if emoticon_unique:
            # 表情包弹幕：dm_type=1 + emoticonOptions。
            # 关键：msg 必须传 emoticon_unique（网页端 DOM 里的 data-file-id）；
            # 传表情触发词会被服务端拒绝（实测返回 code=10203），故此处覆盖 msg。
            data["msg"] = emoticon_unique
            data["dm_type"] = 1
            data["emoticonOptions"] = json.dumps({
                "bulge_display": _as_int(emoticon.get("bulge_display")),
                "emoticon_unique": emoticon_unique,
                "height": _as_int(emoticon.get("height")) or 40,
                "width": _as_int(emoticon.get("width")) or 40,
                "in_player_area": 1,
                "url": str(emoticon.get("url") or ""),
            }, ensure_ascii=False, separators=(",", ":"))
        payload = await self._post_form_json(SEND_DANMAKU_URL, data)
        code = payload.get("code")
        if code != 0:
            raw = payload.get("message") or payload.get("msg") or ""
            raise ApiError("发送弹幕", code, raw)
        return payload
