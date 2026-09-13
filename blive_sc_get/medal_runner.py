"""粉丝牌任务的异步执行引擎：完成即停、节流退避、房间级互斥。

设计要点：

- **完成即停**：以接口返回的 ``is_done`` 或进度「当前 >= 上限」为唯一停止判据；
  每次执行后重新拉取任务复核，达标立即返回，不做补齐式重发。
- **节流/退避**：点赞与发弹幕按房间串行，动作之间随机间隔（默认点赞 15~20s、
  弹幕 6~8s）；连续失败达上限即停止本轮，避免失败重试风暴。
- **风控即止**：命中 ``RISK_CONTROL_CODES`` 立即中止该任务并上报，不再重试。
- **写操作门控**：仅当 ``allow_write_operations`` 为真且已登录（csrf 就绪）才执行。
- **发弹幕内容**：优先轮次循环该直播间**专属表情**；无专属表情包时回退发送
  **纯数字文本**（1、2、3……递增）。

引擎只负责「按房间执行一次完整任务」，是否自动/何时触发由 GUI 决定。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional, Set

import aiohttp

from .api import (
    ApiError,
    BilibiliLiveAPI,
    RISK_CONTROL_CODES,
    describe_like_error,
    describe_send_error,
)
from .app_config import AppConfig
from .medal_tasks import (
    TASK_LIKE,
    TASK_SEND_DANMAKU,
    WRITE_TASK_TYPES,
    compute_action_delay,
    find_task,
    is_task_applicable,
    is_task_complete,
    next_fallback_text,
    parse_title_count,
    pending_write_tasks,
    room_exclusive_emoticons,
    select_emoticon_cycle,
    task_label,
)

logger = logging.getLogger(__name__)

DEFAULT_LIKE_CLICK_CAP = 30
"""单次点赞请求的最大 ``click_time``（接口未给出次数时的兜底）。"""


class MedalTaskRunner:
    """按房间执行粉丝牌写任务（点赞 / 发弹幕）的执行器。"""

    def __init__(self, api: Optional[BilibiliLiveAPI], config: AppConfig, *,
                 emit: Optional[Callable[[str, dict], None]] = None):
        self._api = api
        self._config = config
        self._emit_fn = emit
        self._running: Set[int] = set()

    def is_running(self, room_id: int) -> bool:
        """该房间是否正在执行（供 GUI 禁用重复触发）。"""
        return int(room_id) in self._running

    def _emit(self, event_type: str, payload: dict) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(event_type, payload)
        except Exception:  # 事件回调异常不影响执行本身
            logger.debug("medal 事件回调失败: %s", event_type, exc_info=True)

    async def complete_room(self, room_id: int, anchor_uid: int,
                            live_status: int, *, room_label: str = "",
                            only: Optional[str] = None) -> Dict[str, Any]:
        """执行一个房间的写任务，返回结果摘要（完成即停）。

        ``only`` 指定时只执行该类型的任务（如 ``"like"`` / ``"sendDanmu"``），
        用于界面上的「点赞」「发弹幕」独立按钮；为 ``None`` 时执行全部可执行写任务。

        结果 ``status``：

        - ``blocked``：未满足前置条件（写操作关闭 / 未登录 / 缺主播 uid / 已在执行）；
        - ``done``：本轮任务均已达成或无需执行；
        - ``partial``：部分任务完成、部分未完成；
        - ``risk``：命中风控中止；
        - ``error``：全部失败。
        """
        room_id = int(room_id)
        if room_id in self._running:
            return self._result(room_id, "blocked", "该房间任务正在执行中")
        if not self._config.allow_write_operations:
            return self._result(room_id, "blocked",
                                "已在 config.json 关闭写操作（allow_write_operations=false）")
        if self._api is None or not self._api.logged_in:
            return self._result(room_id, "blocked",
                                "未登录：请用「获取Cookie」获取已登录的 Cookie")
        if not self._api.csrf:
            return self._result(room_id, "blocked",
                                "Cookie 缺少 bili_jct，请重新「获取Cookie」")
        if not anchor_uid:
            return self._result(room_id, "blocked", "缺少主播 uid，无法查询粉丝牌任务")

        self._running.add(room_id)
        label = room_label or str(room_id)
        started = time.monotonic()
        try:
            self._emit("medal_start", {"room_id": room_id, "room_label": label,
                                       "only": only})
            result = await self._complete_locked(room_id, int(anchor_uid),
                                                 int(live_status or 0), label, only)
        finally:
            self._running.discard(room_id)
        result["elapsed"] = round(time.monotonic() - started, 1)
        return result

    @staticmethod
    def _skip_reason(tasks: List[dict], jump_type: str) -> str:
        """指定任务无待办时给出原因（不存在 / 已完成 / 未点亮）。"""
        label = task_label(jump_type)
        task = find_task(tasks, jump_type)
        if task is None:
            return f"该直播间没有「{label}」任务"
        if is_task_complete(task):
            return f"「{label}」任务已完成"
        if not is_task_applicable(task):
            return f"「{label}」任务当前不适用（未点亮）"
        return f"「{label}」任务暂无可执行内容"

    async def _complete_locked(self, room_id: int, anchor_uid: int,
                               live_status: int, label: str,
                               only: Optional[str] = None) -> Dict[str, Any]:
        info = await self._api.get_medal_task_info(anchor_uid)
        tasks = info.get("tasks") or []
        if info.get("reach_free_intimacy_limit"):
            return self._result(room_id, "done",
                                "已达储蓄亲密度上限：投喂一个粉丝灯牌即可领取，暂不执行")
        pending_all = pending_write_tasks(tasks)
        if only is not None:
            pending = [t for t in pending_all if t.get("jump_type") == only]
            if not pending:
                return self._result(room_id, "done", self._skip_reason(tasks, only))
        else:
            pending = pending_all
            if not pending:
                # 上限为 0 且未完成 = 「仅点亮」等不适用任务（如粉丝牌未点亮），明确说明
                not_applicable = any(
                    t.get("jump_type") in WRITE_TASK_TYPES
                    and not is_task_complete(t) and not is_task_applicable(t)
                    for t in tasks)
                if not_applicable:
                    return self._result(
                        room_id, "done",
                        "粉丝牌未点亮（任务显示「仅点亮」），暂无可执行任务")
                return self._result(room_id, "done", "今日粉丝牌任务已完成")

        details: Dict[str, Dict[str, Any]] = {}
        for task in pending:
            jump_type = task.get("jump_type")
            try:
                if jump_type == TASK_LIKE:
                    details["like"] = await self._run_like(
                        room_id, anchor_uid, live_status)
                elif jump_type == TASK_SEND_DANMAKU:
                    details["danmaku"] = await self._run_danmaku(room_id, anchor_uid)
                else:
                    continue
            except ApiError as exc:
                details["like" if jump_type == TASK_LIKE else "danmaku"] = {
                    "ok": False, "message": str(exc)}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                details["like" if jump_type == TASK_LIKE else "danmaku"] = {
                    "ok": False, "message": f"网络异常：{exc}"}
            self._emit("medal_progress", {
                "room_id": room_id, "room_label": label,
                "jump_type": jump_type, "outcome": details.get(
                    "like" if jump_type == TASK_LIKE else "danmaku"),
            })

        return self._summarize(room_id, details)

    async def _run_like(self, room_id: int, anchor_uid: int,
                        live_status: int) -> Dict[str, Any]:
        """点赞任务：直播中方可执行；**按间隔多轮请求直到完成**。

        实测要点：服务端对点赞计数**有节流/延迟**，单次请求通常只推进约 1 点进度，
        且 ``click_time`` 大小不保证等量计入（发送间隔过密会被丢弃）。因此这里按
        随机间隔多轮发送，每轮后重新拉取任务复核（完成即停）；连续若干轮进度不推进
        则提前结束，留待下次（或自动任务下一轮）继续，不无限空发。

        返回结果附带效率统计：``actions``（请求次数）、``clicks``（累计点赞次数）、
        ``elapsed``（耗时秒）。
        """
        if live_status != 1:
            return {"ok": False, "skipped": True,
                    "message": "直播间未开播，跳过点赞（开播后重试）",
                    "actions": 0, "clicks": 0, "elapsed": 0.0}
        started = time.monotonic()
        actions = 0
        clicks_total = 0
        last_error = ""
        first_progress = -1
        progress = -1

        def _done(ok: bool, message: str, *, risk: bool = False) -> Dict[str, Any]:
            return {"ok": ok, "message": message, "risk": risk,
                    "actions": actions, "clicks": clicks_total,
                    "progress": (max(first_progress, 0), max(progress, 0)),
                    "elapsed": round(time.monotonic() - started, 1)}

        max_stall = max(1, int(self._config.medal_max_retry))
        stalled = 0
        rounds = 0
        while True:
            task = await self._refetch_task(anchor_uid, TASK_LIKE)
            if task is None:
                return _done(False, "接口未返回点赞任务")
            if is_task_complete(task):
                return _done(True, "点赞任务已完成")
            if not is_task_applicable(task):
                # 上限为 0（「仅点亮」等）→ 不执行，避免空发请求
                return _done(True, "任务不适用（未点亮），跳过点赞")
            limit = int(task.get("limit") or 0)
            current = int(task.get("current") or 0)
            if first_progress < 0:
                first_progress = current
            if progress >= 0 and current <= progress:
                stalled += 1
                if stalled > max_stall:
                    return _done(False, f"点赞进度未推进（{current}/{limit}），稍后重试")
            else:
                stalled = 0
            progress = current
            rounds += 1
            if rounds > limit * 3 + 5:
                return _done(False, f"点赞任务未完成（{current}/{limit}），稍后重试")
            # click_time 取任务标题里的次数（如「点赞30次」→30），对齐网页端行为
            clicks = parse_title_count(task.get("title")) or DEFAULT_LIKE_CLICK_CAP
            try:
                await self._api.like_room(room_id, anchor_uid, click_time=clicks)
                actions += 1
                clicks_total += clicks
            except ApiError as exc:
                last_error = describe_like_error(exc.code, exc.message)
                if exc.code in RISK_CONTROL_CODES:
                    return _done(False, last_error, risk=True)
                stalled += 1
                if stalled > max_stall:
                    return _done(False, last_error)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"网络异常：{exc}"
                stalled += 1
                if stalled > max_stall:
                    return _done(False, last_error)
            wait = compute_action_delay(*self._config.medal_like_interval)
            logger.info(
                "房间 %s 点赞任务：已发 %d 次请求（累计 %d 次点赞），当前 %d/%d，"
                "耗时 %.1fs，%.1fs 后复核",
                room_id, actions, clicks_total, current, limit,
                time.monotonic() - started, wait)
            await asyncio.sleep(wait)

    async def _run_danmaku(self, room_id: int, anchor_uid: int) -> Dict[str, Any]:
        """发弹幕任务：轮次发专属表情，无专属表情时回退纯数字文本；完成即停。

        返回结果附带效率统计：``sent``（发送条数）、``used_emoticon`` /
        ``used_text``（各发送条数）、``elapsed``（耗时秒）。
        """
        started = time.monotonic()
        sent = 0
        used_emoticon = 0
        used_text = 0
        emoticons = []
        try:
            packages = await self._api.get_room_emoticons(room_id)
            emoticons = room_exclusive_emoticons(packages)
        except (ApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("获取直播间专属表情失败 room=%s: %s", room_id, exc)
        if not emoticons:
            self._emit("medal_note", {
                "room_id": room_id,
                "message": "该直播间无专属表情包，改用纯数字文本发送",
            })

        index = 0
        fallback_text = ""
        last_error = ""

        def _done(ok: bool, message: str, *, risk: bool = False) -> Dict[str, Any]:
            return {"ok": ok, "message": message, "risk": risk, "sent": sent,
                    "used_emoticon": used_emoticon, "used_text": used_text,
                    "elapsed": round(time.monotonic() - started, 1)}

        max_stall = max(1, int(self._config.medal_max_retry))
        stalled = 0
        progress = -1
        rounds = 0
        while True:
            task = await self._refetch_task(anchor_uid, TASK_SEND_DANMAKU)
            if task is None:
                return _done(False, "接口未返回发弹幕任务")
            if is_task_complete(task):
                return _done(True, "发弹幕任务已完成")
            if not is_task_applicable(task):
                # 上限为 0（「仅点亮」等）→ 不执行，避免空发弹幕
                return _done(True, "任务不适用（未点亮），跳过发弹幕")
            limit = int(task.get("limit") or 0)
            current = int(task.get("current") or 0)
            if progress >= 0 and current <= progress:
                stalled += 1
                if stalled > max_stall:
                    return _done(False, f"发弹幕进度未推进（{current}/{limit}），稍后重试")
            else:
                stalled = 0
            progress = current
            rounds += 1
            if rounds > limit * 3 + 5:
                return _done(False, f"发弹幕任务未完成（{current}/{limit}），稍后重试")
            emoticon, index = select_emoticon_cycle(emoticons, index)
            try:
                if emoticon:
                    await self._api.send_danmaku(
                        room_id, str(emoticon.get("unique") or ""), emoticon=emoticon)
                    used_emoticon += 1
                else:
                    fallback_text = next_fallback_text(fallback_text)
                    await self._api.send_danmaku(room_id, fallback_text)
                    used_text += 1
                sent += 1
            except ApiError as exc:
                last_error = describe_send_error(exc.code, exc.message)
                if exc.code in RISK_CONTROL_CODES:
                    return _done(False, last_error, risk=True)
                stalled += 1
                if stalled > max_stall:
                    return _done(False, last_error)
                # 频率过快等可恢复错误：等待一个更长的间隔再试
                await asyncio.sleep(compute_action_delay(*self._config.medal_danmaku_interval))
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = f"网络异常：{exc}"
                stalled += 1
                if stalled > max_stall:
                    return _done(False, last_error)
            wait = compute_action_delay(*self._config.medal_danmaku_interval)
            logger.info(
                "房间 %s 发弹幕任务：已发 %d 条（表情 %d / 文本 %d），当前 %d/%d，"
                "耗时 %.1fs，%.1fs 后复核",
                room_id, sent, used_emoticon, used_text, current, limit,
                time.monotonic() - started, wait)
            await asyncio.sleep(wait)

    async def _refetch_task(self, anchor_uid: int, jump_type: str) -> Optional[dict]:
        """重新拉取任务信息并按 jump_type 取任务（完成即停的复核来源）。"""
        info = await self._api.get_medal_task_info(anchor_uid)
        return find_task(info.get("tasks") or [], jump_type)

    @staticmethod
    def _result(room_id: int, status: str, message: str,
                details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"room_id": int(room_id), "status": status,
                "message": message, "details": details or {}}

    @staticmethod
    def _format_metrics(key: str, outcome: Dict[str, Any]) -> str:
        """把单个任务的执行效率拼成可读后缀（无实际动作时不显示）。"""
        elapsed = outcome.get("elapsed")
        if key == "like":
            actions = int(outcome.get("actions") or 0)
            if not actions:
                return ""
            low, high = (outcome.get("progress") or (0, 0))[:2]
            return f"（{actions} 次请求，进度 {low}→{high}，用时 {elapsed}s）"
        sent = int(outcome.get("sent") or 0)
        if not sent:
            return ""
        return (f"（{sent} 条：表情 {int(outcome.get('used_emoticon') or 0)}"
                f" / 文本 {int(outcome.get('used_text') or 0)}，用时 {elapsed}s）")

    def _summarize(self, room_id: int, details: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        parts = []
        has_risk = False
        any_ok = False
        any_fail = False
        for key, label in (("like", "点赞"), ("danmaku", "发弹幕")):
            outcome = details.get(key)
            if not outcome:
                continue
            if outcome.get("risk"):
                has_risk = True
            if outcome.get("ok"):
                any_ok = True
            elif outcome.get("skipped"):
                # 跳过（如未开播暂无点赞）不算失败，也不计入完成
                pass
            else:
                any_fail = True
            parts.append(
                f"{label}：{outcome.get('message') or '完成'}"
                f"{self._format_metrics(key, outcome)}")
        message = "；".join(parts) if parts else "今日粉丝牌任务已完成"
        if has_risk:
            status = "risk"
        elif any_fail and any_ok:
            status = "partial"
        elif any_fail:
            status = "error"
        else:
            status = "done"
        return self._result(room_id, status, message, details)
