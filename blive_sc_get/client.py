"""弹幕 WebSocket 客户端：认证、心跳、自动重连、SC 消息分发。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Callable, Optional, Set

import aiohttp

from .api import ApiError, BilibiliLiveAPI, RISK_CONTROL_CODES
from .protocol import (
    Operation,
    ProtocolError,
    build_packet,
    flatten_packets,
)
from .room_lock import RoomLock, RoomLockAcquireError
from .storage import SCStorage

DEFAULT_DANMU_HOST = "broadcastlv.chat.bilibili.com"
DEFAULT_WSS_PORT = 443

HEARTBEAT_INTERVAL = 30
"""心跳发送间隔（秒），与 B 站 web 端一致。"""

HEARTBEAT_TIMEOUT = 70
"""超过该秒数未收到心跳应答则主动重连。"""

AUTH_TIMEOUT = 10
"""发出认证包后等待应答的超时（秒）。"""

STATUS_REFRESH_DEBOUNCE = 2.0
"""实时刷新房间状态的最小间隔（秒），防止短时间内重复请求接口触发风控。"""

LIVE_STATUS_TEXT = {0: "未开播", 1: "直播中", 2: "轮播中"}


class NeedReconnect(Exception):
    """内部触发重连。"""


class RoomClient:
    """监听一个直播间的弹幕 WebSocket，把 SC 消息写入 storage。"""

    def __init__(self, api: Optional[BilibiliLiveAPI], room_id: int, storage: SCStorage,
                 event_callback: Optional[Callable[[str, dict], None]] = None):
        self._api = api
        self._room_id_input = int(room_id)
        self._storage = storage
        self._room_id = int(room_id)
        self._log = logging.getLogger(f"Room[{room_id}]")
        self._event_callback = event_callback
        self._seen_sc_ids: Set[int] = set()
        self._known_ids_loaded = False
        self._anchor_name = ""
        self._auth_ok = asyncio.Event()
        self._last_heartbeat_ack = 0.0
        self._connect_attempts = 0
        self._room_lock: Optional[RoomLock] = None
        self._last_status_refresh = 0.0  # monotonic 时间戳，用于状态刷新防抖

    def _emit(self, event_type: str, payload: dict) -> None:
        """向外部（如 GUI）推送事件；回调异常不影响监听本身。"""
        if self._event_callback is None:
            return
        try:
            self._event_callback(event_type, payload)
        except Exception:
            self._log.debug("事件回调执行失败: %s", event_type, exc_info=True)

    @property
    def room_id(self) -> int:
        return self._room_id

    async def run(self) -> None:
        """主循环：断线自动重连；接口级错误或房间被其他实例占用则停止本房间。"""
        backoff = 1.0
        try:
            while True:
                try:
                    await self._run_once()
                except asyncio.CancelledError:
                    raise
                except ApiError as exc:
                    hint = ""
                    if exc.code in RISK_CONTROL_CODES:
                        hint = "（疑似风控，请提供 B 站 cookie：--cookie 参数 / BILI_COOKIE 环境变量 / cookie.txt）"
                    self._log.error("接口调用失败，停止监听本房间: %s%s", exc, hint)
                    self._emit("stopped", {"room_id": self._room_id, "reason": str(exc)})
                    return
                except RoomLockAcquireError as exc:
                    self._log.error("停止监听本房间: %s", exc)
                    self._emit("stopped", {"room_id": self._room_id, "reason": str(exc)})
                    return
                except Exception as exc:
                    if isinstance(exc, NeedReconnect):
                        self._log.warning("连接中断: %s", exc)
                    elif isinstance(exc, (ProtocolError, aiohttp.ClientError, asyncio.TimeoutError)):
                        self._log.warning("连接中断: %r", exc)
                    else:
                        self._log.exception("发生未预期的异常")
                if self._auth_ok.is_set():
                    # 这次连接成功过，重置退避时间
                    self._auth_ok.clear()
                    backoff = 1.0
                    self._log.info("连接已断开")
                self._log.info("%.0f 秒后重连", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        finally:
            if self._room_lock is not None:
                self._room_lock.release()
                self._room_lock = None
                self._log.debug("已释放房间锁")

    async def _run_once(self) -> None:
        room_info = await self._api.get_full_room_info(self._room_id_input)
        self._room_id = int(room_info.get("room_id") or self._room_id_input)
        self._acquire_room_lock()
        if not self._known_ids_loaded:
            self._known_ids_loaded = True
            self._seen_sc_ids |= self._storage.load_known_sc_ids(self._room_id)
            pending_count = self._storage.load_pending(self._room_id)
            if pending_count:
                self._log.info("发现上次未写完的 %d 条 SC 记录，将自动补写", pending_count)
            self._storage.flush_pending(self._room_id)
        if self._room_id != self._room_id_input:
            self._log.info("输入的 %s 是短号，已转换为真实房间号 %s", self._room_id_input, self._room_id)
        status_text = LIVE_STATUS_TEXT.get(int(room_info.get("live_status") or 0), "未知")
        self._log.info("房间 %s（%s）标题：%s", self._room_id, status_text, room_info.get("title") or "未知")
        if not self._anchor_name:
            # 主播名基本不变，取一次即可；失败留空，下次重连再试
            self._anchor_name = await self._api.get_anchor_name(int(room_info.get("uid") or 0))
        self._emit("status", {
            "room_id": self._room_id,
            "input_room_id": self._room_id_input,
            "title": room_info.get("title") or "",
            "live_status": int(room_info.get("live_status") or 0),
            "anchor_name": self._anchor_name,
            "uid": int(room_info.get("uid") or 0),
        })

        danmu_info = await self._api.get_danmu_info(self._room_id)
        # 舰长数：非关键数据，失败不影响监听（v2 接口需要主播 uid）
        guard_num = await self._api.get_guard_count(
            self._room_id, int(room_info.get("uid") or 0)
        )
        self._emit("guards", {"room_id": self._room_id, "num": guard_num})
        host_list = danmu_info.get("host_list") or []
        self._connect_attempts += 1
        if host_list:
            conf = host_list[(self._connect_attempts - 1) % len(host_list)]
            host = conf.get("host") or DEFAULT_DANMU_HOST
            port = conf.get("wss_port") or DEFAULT_WSS_PORT
        else:
            host, port = DEFAULT_DANMU_HOST, DEFAULT_WSS_PORT
        url = f"wss://{host}:{port}/sub"
        self._log.info("连接弹幕服务器 %s（第 %d 次尝试）", url, self._connect_attempts)

        self._auth_ok.clear()
        self._last_heartbeat_ack = time.time()
        async with self._api.session.ws_connect(
            url, headers=self._api.ws_headers(), max_msg_size=8 * 1024 * 1024
        ) as ws:
            await self._send_auth(ws, danmu_info.get("token") or "")
            recv_task = asyncio.create_task(self._recv_loop(ws))
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            done = set()
            try:
                await asyncio.wait_for(self._auth_ok.wait(), AUTH_TIMEOUT)
                self._log.info("已接入 %d 直播间，开始监听 SuperChat ...", self._room_id)
                done, _pending = await asyncio.wait(
                    {recv_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (recv_task, heartbeat_task):
                    task.cancel()
                await asyncio.gather(recv_task, heartbeat_task, return_exceptions=True)
            # 把先结束任务里的异常传播出去（如心跳超时）
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()

    def _acquire_room_lock(self) -> None:
        """解析出真实房间号后立即加房间锁；被占用时抛 RoomLockAcquireError。"""
        if self._room_lock is not None:
            return
        lock = RoomLock(self._storage.base_dir / f"room_{self._room_id}")
        lock.acquire()
        self._room_lock = lock
        self._log.debug("已锁定房间目录 %s", lock.path)

    async def _send_auth(self, ws, token: str) -> None:
        auth = {
            "uid": self._api.uid,
            "roomid": self._room_id,
            "protover": 3,
            "platform": "web",
            "type": 2,
            "key": token,
        }
        if self._api.buvid3:
            auth["buvid"] = self._api.buvid3
        await ws.send_bytes(build_packet(Operation.USER_AUTH, json.dumps(auth).encode("utf-8")))
        self._log.debug("已发送认证包")

    async def _recv_loop(self, ws) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                self._process_ws_data(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.ERROR):
                raise NeedReconnect(f"WebSocket 已关闭: {msg}")
            else:
                self._log.debug("收到其他 WebSocket 消息: %s", msg.type)

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            if time.time() - self._last_heartbeat_ack > HEARTBEAT_TIMEOUT:
                raise NeedReconnect("心跳应答超时，服务器无响应")
            await ws.send_bytes(build_packet(Operation.HEARTBEAT, b"{}"))
            self._log.debug("已发送心跳")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    def _process_ws_data(self, raw: bytes) -> None:
        try:
            packets = flatten_packets(raw)
        except ProtocolError as exc:
            self._log.warning("解析报文失败: %s", exc)
            return
        for _protocol, op, body in packets:
            if op == Operation.HEARTBEAT_REPLY:
                if len(body) >= 4:
                    popularity = int.from_bytes(body[:4], "big")
                    self._last_heartbeat_ack = time.time()
                    self._log.debug("当前人气值: %d", popularity)
            elif op == Operation.AUTH_REPLY:
                self._handle_auth_reply(body)
            elif op == Operation.MESSAGE:
                self._handle_business_message(body)
            else:
                self._log.debug("忽略操作码 op=%s", op)

    def _handle_auth_reply(self, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if payload.get("code", -1) == 0:
            self._auth_ok.set()
        else:
            raise NeedReconnect(f"认证被服务器拒绝: {payload}")

    def _handle_business_message(self, body: bytes) -> None:
        try:
            command = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._log.debug("忽略非 JSON 消息: %r", body[:100])
            return
        cmd = command.get("cmd")
        if not isinstance(cmd, str):
            return
        # SUPER_CHAT_MESSAGE_DELETE 也以 SUPER_CHAT_MESSAGE 开头，先判断
        if cmd.startswith("SUPER_CHAT_MESSAGE_DELETE"):
            self._on_super_chat_delete(command)
        elif cmd.startswith("SUPER_CHAT_MESSAGE"):
            self._on_super_chat(command)
        elif cmd == "LIVE":
            self._schedule_status_refresh("开播")
        elif cmd == "PREPARING":
            self._schedule_status_refresh("下播/准备中")
        elif cmd == "ROOM_CHANGE":
            self._schedule_status_refresh("房间信息变更（标题/分区）")
        elif cmd in ("ONLINE_RANK_COUNT", "ONLINE_RANK_V2"):
            self._on_online_rank_count(command)
        else:
            self._log.debug("消息 %s", cmd)

    def _on_online_rank_count(self, command: dict) -> None:
        """直播间实时在线人数（同接），弹幕服务器随流推送。"""
        data = command.get("data") or {}
        count = data.get("count")
        if isinstance(count, (int, float)) and count > 0:
            self._emit("online_count", {"room_id": self._room_id, "count": int(count)})

    def _schedule_status_refresh(self, reason: str) -> None:
        """带防抖地安排一次房间状态刷新（LIVE/PREPARING/ROOM_CHANGE 触发）。"""
        now = time.monotonic()
        if now - self._last_status_refresh < STATUS_REFRESH_DEBOUNCE:
            self._log.debug("状态刷新过于频繁（%s），跳过本次", reason)
            return
        self._last_status_refresh = now
        asyncio.create_task(self._refresh_room_status(reason))

    async def _refresh_room_status(self, reason: str) -> None:
        """重新拉取房间信息并广播 status 事件，实现直播状态/标题实时更新。"""
        try:
            info = await self._api.get_full_room_info(self._room_id_input)
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._log.debug("刷新房间状态失败（%s）: %s", reason, exc)
            return
        live_status = int(info.get("live_status") or 0)
        status_text = LIVE_STATUS_TEXT.get(live_status, "未知")
        title = info.get("title") or ""
        self._log.info("房间状态更新（%s）：%s，标题：%s", reason, status_text, title or "未知")
        self._emit("status", {
            "room_id": self._room_id,
            "input_room_id": self._room_id_input,
            "title": title,
            "live_status": live_status,
            "anchor_name": self._anchor_name,
            "uid": int(info.get("uid") or 0),
        })

    def _on_super_chat(self, command: dict) -> None:
        data = command.get("data")
        # 兼容 data.data 双层嵌套与 data 平铺两种历史格式
        inner = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        if not isinstance(inner, dict):
            self._log.warning("SC 消息格式异常: %r", str(command)[:200])
            return
        sc_id = inner.get("id")
        if sc_id is None:
            self._log.warning("SC 消息缺少 id: %r", str(command)[:200])
            return
        if sc_id in self._seen_sc_ids:
            self._log.debug("忽略重复的 SC id=%s", sc_id)
            return
        self._seen_sc_ids.add(sc_id)
        received_at = datetime.now()
        try:
            saved = self._storage.save_sc(self._room_id, inner, received_at)
        except OSError as exc:
            self._log.error("保存 SC 失败 id=%s: %s", sc_id, exc)
            return
        user = inner.get("user_info") or {}
        uname = user.get("uname") or inner.get("uname") or "未知用户"
        message = str(inner.get("message") or "").replace("\n", " ")
        self._emit("sc", {
            "room_id": self._room_id,
            "sc": inner,
            "time_received": received_at.isoformat(timespec="seconds"),
            "saved": saved,
        })
        if saved:
            self._log.info("收到 SC ¥%s | %s | %s", inner.get("price", "?"), uname, message)
        else:
            self._log.info("收到 SC ¥%s | %s | %s（文件被占用，已进入重试队列，稍后自动补写）",
                           inner.get("price", "?"), uname, message)

    def _on_super_chat_delete(self, command: dict) -> None:
        data = command.get("data") or {}
        ids = data.get("ids") or []
        if not ids:
            return
        try:
            self._storage.mark_deleted(self._room_id, ids, datetime.now())
        except OSError as exc:
            self._log.error("记录 SC 删除事件失败: %s", exc)
            return
        self._emit("delete", {"room_id": self._room_id, "ids": list(ids)})
        self._log.info("SC 已被删除（退款）: ids=%s", ids)
