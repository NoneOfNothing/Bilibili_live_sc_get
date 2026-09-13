"""B 站直播相关 HTTP 接口封装：房间信息、弹幕服务器信息（含 WBI 签名）、游客 buvid3。

接口与签名算法参考了开源项目 xfgryujk/blivedm 对现行 web 端协议的梳理。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import aiohttp

from .medal_tasks import (
    dedupe_medals,
    parse_medal_panel,
    parse_task_info_list,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        async with self.session.get(url, params=params, headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if not isinstance(data, dict):
            raise ApiError(f"请求 {url}", "格式异常", f"响应不是 JSON 对象: {str(data)[:120]}")
        return data

    async def _post_form_json(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """表单（application/x-www-form-urlencoded）POST，返回 JSON 对象。

        aiohttp 在 data 传 dict 时会自动设置表单 Content-Type。
        """
        async with self.session.post(url, data=data, headers=self._headers()) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise ApiError(f"请求 {url}", "格式异常", f"响应不是 JSON 对象: {str(payload)[:120]}")
        return payload

    async def _post_query_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """POST 请求（参数放 query、body 为空）返回 JSON 对象。

        点赞 likeReportV3 等接口把业务参数（含 WBI 签名）放在 query、body 为空，
        与 :meth:`_post_form_json`（表单 body）区分开。
        """
        async with self.session.post(url, params=params, headers=self._headers()) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise ApiError(f"请求 {url}", "格式异常", f"响应不是 JSON 对象: {str(payload)[:120]}")
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
        return info

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
