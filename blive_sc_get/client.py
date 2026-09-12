"""弹幕 WebSocket 客户端：认证、心跳、自动重连、SC 消息分发。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
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

HEARTBEAT_INTERVAL = 20
"""心跳发送间隔（秒）。比 web 端略密，以便更快发现静默断网（如拔网线）。"""

HEARTBEAT_TIMEOUT = 45
"""超过该秒数未收到任何心跳应答则主动重连（约 2 个心跳周期）。

原为 70 秒，静默断网（连接未关闭但不再有数据）时最长要等 70 秒才会发现；
收紧后短暂断网可更快进入重连流程。
"""

HEARTBEAT_CHECK_INTERVAL = 5
"""心跳循环的轮询间隔（秒）：把「超时判定」与「发包节拍」解耦，缩短静默断网
（连接未关闭但不再有数据，如拔网线）的发现延迟。"""

AUTH_TIMEOUT = 10
"""发出认证包后等待应答的超时（秒）。"""

RECONNECT_BASE_DELAY = 1.0
"""首次重连前的等待秒数；之后按指数增长。"""

RECONNECT_MAX_DELAY = 30.0
"""单次重连等待的上限（秒），避免长时间断网后退避过久。"""

RECONNECT_JITTER = 0.3
"""退避抖动比例（±30%），避免多个房间在同一时刻同步重连。"""

STATUS_REFRESH_DEBOUNCE = 2.0
"""实时刷新房间状态的最小间隔（秒），防止短时间内重复请求接口触发风控。"""

OFFLINE_CONFIRM_DELAY = 3.0
"""收到关播信号后，延迟多少秒重拉接口确认（避开接口 live_status 的缓存窗口）。"""

OFFLINE_CONFIRM_RETRY_DELAY = 5.0
"""关播确认刷新失败后的重试间隔（秒）。"""

LIVE_STATUS_TEXT = {0: "未开播", 1: "直播中", 2: "轮播中"}


def compute_reconnect_delay(attempt: int, *, base: float = RECONNECT_BASE_DELAY,
                            cap: float = RECONNECT_MAX_DELAY,
                            jitter: float = RECONNECT_JITTER,
                            rng: Optional[Callable[[], float]] = None) -> float:
    """第 ``attempt`` 次重连前的等待秒数（纯函数，便于单测）。

    ``attempt`` 从 0 起：0 → base、1 → 2×base……直到 cap 封顶，再叠加
    ±jitter 比例的随机抖动（默认 ±30%），避免多房间同步重连形成尖峰。
    ``rng`` 可注入（返回 [0,1) 的可调用对象）以便测试确定化。
    """
    if rng is None:
        rng = random.random
    index = max(int(attempt), 0)
    delay = min(base * (2 ** index), cap)
    spread = delay * max(0.0, float(jitter))
    if spread > 0:
        delay += (rng() * 2.0 - 1.0) * spread
    return round(max(0.1, min(delay, cap)), 3)


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
        self._status_refresh_pending = False  # 防抖窗口内已有待发的合并刷新
        self._offline_signal = False  # 收到 PREPARING/STOP_LIVE_ROOM_LIST 等确定性关播信号
        self._dm_enabled = False  # 弹幕接收开关（GUI 按当前选中房间设置）
        self._title = ""  # 最近一次已知的直播标题（离线兜底 emit 用）
        self._uid = 0  # 最近一次已知的主播 uid
        self._offline_confirm_task: Optional[asyncio.Task] = None

    def set_danmaku_enabled(self, enabled: bool) -> None:
        """开关弹幕接收（GUI 按当前选中房间设置；bool 赋值线程安全）。

        开启后收到的弹幕会落盘并广播 dm 事件；关闭则解析后直接丢弃。
        """
        self._dm_enabled = bool(enabled)

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
        attempt = 0  # 连续失败次数（握手成功过就清零），用于指数退避
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
                    attempt = 0
                    self._log.info("连接已断开")
                delay = compute_reconnect_delay(attempt)
                attempt += 1
                self._log.info("%.1f 秒后重连（第 %d 次）", delay, attempt)
                self._emit("reconnecting", {
                    "room_id": self._room_id, "attempt": attempt, "delay": delay,
                })
                await asyncio.sleep(delay)
        finally:
            if self._offline_confirm_task is not None:
                self._offline_confirm_task.cancel()
            if self._room_lock is not None:
                self._room_lock.release()
                self._room_lock = None
                self._log.debug("已释放房间锁")
            self._storage.close_danmaku_buffer(self._room_id)  # 刷盘并关闭弹幕句柄

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
        live_status = int(room_info.get("live_status") or 0)
        if live_status == 1:
            # 连接时拉到的是新鲜数据，可清除可能残留的陈旧关播信号
            self._offline_signal = False
        if not self._anchor_name:
            # 主播名基本不变，取一次即可；失败留空，下次重连再试
            self._anchor_name = await self._api.get_anchor_name(int(room_info.get("uid") or 0))
        self._emit_status(live_status, room_info.get("title") or "",
                          int(room_info.get("uid") or 0))

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
                if self._connect_attempts > 1:
                    # 断线后的重连握手成功：上报可观测事件，便于确认短时断网已恢复
                    self._emit("reconnected", {
                        "room_id": self._room_id,
                        "attempt": self._connect_attempts,
                        "url": url,
                    })
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
        """定时发心跳，并按 HEARTBEAT_CHECK_INTERVAL 高频检查应答超时。

        发送仍严格按 HEARTBEAT_INTERVAL 的节拍（与 web 端一致），但超时判定
        独立轮询——否则静默断网时最长要等「超时阈值 + 一个发送周期」才发现。
        """
        next_send = time.time()
        while True:
            if time.time() - self._last_heartbeat_ack > HEARTBEAT_TIMEOUT:
                raise NeedReconnect("心跳应答超时，服务器无响应")
            now = time.time()
            if now >= next_send:
                await ws.send_bytes(build_packet(Operation.HEARTBEAT, b"{}"))
                self._log.debug("已发送心跳")
                next_send = now + HEARTBEAT_INTERVAL
            await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL)

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
            self._offline_signal = False  # 开播信号同样权威，清除关播信号
            self._schedule_status_refresh("开播")
        elif cmd == "PREPARING":
            self._on_offline_signal("下播/准备中")
        elif cmd == "ROOM_CHANGE":
            self._schedule_status_refresh("房间信息变更（标题/分区）")
        elif cmd == "STOP_LIVE_ROOM_LIST":
            self._on_stop_live_room_list(command)
        elif cmd in ("ONLINE_RANK_COUNT", "ONLINE_RANK_V2"):
            self._on_online_rank_count(command)
        elif cmd.startswith("DANMU_MSG"):
            self._on_danmu_msg(command)
        else:
            self._log.debug("消息 %s", cmd)

    def _on_danmu_msg(self, command: dict) -> None:
        """普通弹幕：开关开启时落盘并广播 dm 事件，关闭则直接丢弃。

        表情包弹幕（dm_type=1，见 info[0][13]）的 info[1] 为表情触发词，
        统一以 [名称] 形式显示/落盘。
        """
        if not self._dm_enabled:
            return
        # DANMU_MSG 的 info 在 cmd 同级（不在 data 下）：info[1]=内容、info[2]=用户信息
        info = command.get("info")
        if not isinstance(info, list) or len(info) < 3:
            return
        text = info[1] if isinstance(info[1], str) else ""
        user = info[2] if isinstance(info[2], list) else []
        uname = user[1] if len(user) > 1 and isinstance(user[1], str) else "未知用户"
        try:
            uid = int(user[0]) if user and isinstance(user[0], (int, str)) else 0
        except (TypeError, ValueError):
            uid = 0
        if self._is_emote_danmu(info):
            word = text.strip() or str(self._parse_dm_extra(info).get("content", "")).strip()
            text = word if (word.startswith("[") and word.endswith("]") and len(word) > 2) \
                else f"[{word or '表情包'}]"
        if not text:
            return
        received_at = datetime.now()
        try:
            self._storage.save_danmaku(self._room_id, uname, text, received_at)
        except OSError as exc:
            # 弹幕非关键数据：落盘失败（如文件被占用）直接丢弃，不影响显示
            self._log.debug("弹幕落盘失败: %s", exc)
        self._emit("dm", {
            "room_id": self._room_id,
            "time": received_at.isoformat(timespec="seconds"),
            "uname": uname,
            "uid": uid,
            "text": text,
            "dmid": self._extract_dmid(info),
        })

    def _on_online_rank_count(self, command: dict) -> None:
        """直播间实时在线人数（同接），弹幕服务器随流推送。"""
        data = command.get("data") or {}
        count = data.get("count")
        if isinstance(count, (int, float)) and count > 0:
            self._emit("online_count", {"room_id": self._room_id, "count": int(count)})

    def _on_stop_live_room_list(self, command: dict) -> None:
        """批量下播通知：名单包含本房间时视为确定性关播信号。"""
        data = command.get("data") or {}
        raw_list = data.get("room_id_list") or []
        try:
            ids = {int(r) for r in raw_list}
        except (TypeError, ValueError):
            ids = set()
        if self._room_id in ids or self._room_id_input in ids:
            self._on_offline_signal("下播(STOP_LIVE_ROOM_LIST)")
        else:
            self._log.debug("下播名单不含本房间: %s", raw_list)

    def _on_offline_signal(self, reason: str) -> None:
        """确定性关播信号处理：立即本地兜底置为未开播，并安排延迟确认刷新。

        关播瞬间 room/v1/Room/get_info 常返回缓存的 live_status=1，因此本地状态
        以弹幕推送为准，随后延迟重拉接口复核并刷新标题等字段。
        """
        if not self._offline_signal:
            self._offline_signal = True
            self._log.info("收到关播信号（%s），状态置为未开播", reason)
            self._emit_status(0)
        self._schedule_offline_confirm()

    def _schedule_offline_confirm(self) -> None:
        if self._offline_confirm_task is not None and not self._offline_confirm_task.done():
            return  # 已有待执行的确认任务
        self._offline_confirm_task = asyncio.create_task(self._confirm_offline())

    async def _confirm_offline(self) -> None:
        await asyncio.sleep(OFFLINE_CONFIRM_DELAY)
        for delay in (0.0, OFFLINE_CONFIRM_RETRY_DELAY):
            if delay:
                await asyncio.sleep(delay)
            if await self._refresh_room_status("关播确认"):
                return

    def _schedule_status_refresh(self, reason: str) -> None:
        """带防抖地安排一次房间状态刷新；节流窗口内的请求合并为窗口结束后补发。"""
        now = time.monotonic()
        elapsed = now - self._last_status_refresh
        if elapsed < STATUS_REFRESH_DEBOUNCE:
            if self._status_refresh_pending:
                self._log.debug("状态刷新合并（%s）", reason)
                return
            self._status_refresh_pending = True
            delay = STATUS_REFRESH_DEBOUNCE - elapsed

            async def _trailing() -> None:
                await asyncio.sleep(delay)
                self._status_refresh_pending = False
                self._last_status_refresh = time.monotonic()
                await self._refresh_room_status(f"{reason}（合并补发）")

            asyncio.create_task(_trailing())
            self._log.debug("状态刷新合并到 %.1f 秒后（%s）", delay, reason)
            return
        self._last_status_refresh = now
        asyncio.create_task(self._refresh_room_status(reason))

    async def _refresh_room_status(self, reason: str) -> bool:
        """重新拉取房间信息并广播 status 事件，返回是否成功。"""
        try:
            info = await self._api.get_full_room_info(self._room_id_input)
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._log.debug("刷新房间状态失败（%s）: %s", reason, exc)
            return False
        live_status = int(info.get("live_status") or 0)
        if self._offline_signal and live_status == 1:
            # 关播瞬间接口常返回缓存的旧状态，以弹幕推送的关播信号为准
            self._log.info("接口返回直播中但近期收到过关播信号，按未开播处理")
            live_status = 0
        status_text = LIVE_STATUS_TEXT.get(live_status, "未知")
        title = info.get("title") or ""
        self._log.info("房间状态更新（%s）：%s，标题：%s", reason, status_text, title or "未知")
        self._emit_status(live_status, title, int(info.get("uid") or 0))
        return True

    def _emit_status(self, live_status: int, title: Optional[str] = None,
                     uid: Optional[int] = None) -> None:
        """统一构造并广播 status 事件，同时维护本地缓存（离线兜底 emit 依赖）。"""
        if title is not None:
            self._title = title
        if uid is not None:
            self._uid = uid
        self._emit("status", {
            "room_id": self._room_id,
            "input_room_id": self._room_id_input,
            "title": self._title,
            "live_status": live_status,
            "anchor_name": self._anchor_name,
            "uid": self._uid,
        })

    @staticmethod
    def _parse_dm_extra(info: list) -> dict:
        """解析 DANMU_MSG 的 extra（info[0][15]）。

        实测结构为 {"extra": "<JSON 字符串>"}（dm_type/content 在内层），
        旧版为平铺对象或 JSON 字符串，三种形态都兼容。
        """
        meta = info[0] if isinstance(info[0], list) else []
        extra = meta[15] if len(meta) > 15 else {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except ValueError:
                return {}
        if isinstance(extra, dict) and isinstance(extra.get("extra"), str):
            try:
                inner = json.loads(extra["extra"])
            except ValueError:
                return {}
            if isinstance(inner, dict):
                return inner
        return extra if isinstance(extra, dict) else {}

    @staticmethod
    def _extract_dmid(info: list) -> str:
        """从 DANMU_MSG 的 info[0] 中取弹幕 dmid（供「回复该弹幕」使用）。

        现行协议中 dmid 通常位于 info[0][6]（部分实现为 info[0][7]）；仓库
        内样本为占位数据无法确定，故按候选下标取「最长的纯数字串」容错：
        dmid 是较长的数字 id，行号等短数字不会被误选。取不到返回 ""（此时
        GUI 的「回复该弹幕」不可用，仍可 @ 用户）。
        """
        meta = info[0] if isinstance(info, list) and info and isinstance(info[0], list) else []
        best = ""
        for index in (6, 7, 5):
            if index >= len(meta):
                continue
            value = str(meta[index]).strip()
            if value.isdigit() and int(value) > 0 and len(value) > len(best):
                best = value
        return best

    @staticmethod
    def _is_emote_danmu(info: list) -> bool:
        """判断是否表情包弹幕：dm_type 位于 info[0][15] 的 extra JSON 内。

        注意实测中 info[0][13] 是表情图片信息对象，不是 dm_type。
        """
        return RoomClient._parse_dm_extra(info).get("dm_type") == 1

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
