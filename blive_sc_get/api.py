"""B 站直播相关 HTTP 接口封装：房间信息、弹幕服务器信息（含 WBI 签名）、游客 buvid3。

接口与签名算法参考了开源项目 xfgryujk/blivedm 对现行 web 端协议的梳理。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, Optional

import aiohttp

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
GUARD_TOP_LIST_URL = "https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topList"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
HOMEPAGE_URL = "https://www.bilibili.com/"
BUVID_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

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
        super().__init__(f"{action}失败: code={code} message={message}")


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

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        async with self.session.get(url, params=params, headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if not isinstance(data, dict):
            raise ApiError(f"请求 {url}", "格式异常", f"响应不是 JSON 对象: {str(data)[:120]}")
        return data

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
