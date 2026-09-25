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
- **可打断**：房间级打断标记（``interrupt_auto_danmaku``）供 GUI 在**开播信号**到达时
  立即停止「自动发弹幕」任务（默认只在未开播时自动发，开播即不该继续刷屏）；
  等待间隔可被打断提前唤醒，无需等下一次发送后再退出。

引擎只负责「按房间执行一次完整任务」，是否自动/何时触发由 GUI 决定。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Iterable, Optional, Set, Union

import aiohttp

from .api import (
    ApiError,
    BilibiliLiveAPI,
    RISK_CONTROL_CODES,
    describe_like_error,
    describe_send_error,
)
from .app_config import AppConfig
from .log_categories import CATEGORY_TASK, get_logger
from .medal_tasks import (
    LIGHT_UP_DANMAKU_COUNT,
    LIGHT_UP_LIKE_CLICKS,
    TASK_LIKE,
    TASK_SEND_DANMAKU,
    WRITE_TASK_TYPES,
    compute_action_delay,
    find_task,
    is_light_up_task,
    is_task_applicable,
    is_task_complete,
    next_fallback_text,
    parse_title_count,
    pending_write_tasks,
    room_exclusive_emoticons,
    select_emoticon_cycle,
    task_label,
)

logger = get_logger(CATEGORY_TASK, __name__)

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
        # 以下三项按房间记录「本次执行」的上下文，供打断判断（见 interrupt_auto_danmaku）
        self._running_auto: Dict[int, bool] = {}      # 是否由自动任务提交
        self._running_types: Dict[int, Set[str]] = {}  # 本次执行包含的任务类型
        self._interrupts: Dict[int, asyncio.Event] = {}  # 房间级打断标记

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
                            only: Optional[Union[str, Iterable[str]]] = None,
                            auto: bool = False) -> Dict[str, Any]:
        """执行一个房间的写任务，返回结果摘要（完成即停）。

        ``only`` 指定时只执行该类型的任务：可为单个类型字符串（如 ``"like"``）
        或类型集合（如 ``["like", "sendDanmu"]``），用于界面上分离的自动开关与
        独立按钮；为 ``None`` 时执行全部可执行写任务。

        ``auto`` 标记本次提交是否来自**自动任务**（周期轮询 / 开播触发）；手动按钮
        传 ``False``。该标记只用于打断判断：``interrupt_auto_danmaku`` 只打断自动
        提交的发弹幕任务，手动按钮不受开播信号影响。

        结果 ``status``：

        - ``blocked``：未满足前置条件（写操作关闭 / 未登录 / 缺主播 uid / 已在执行）；
        - ``done``：本轮任务均已达成或无需执行；
        - ``interrupted``：自动发弹幕任务被开播信号打断（已发出的弹幕保留）；
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
        self._running_auto[room_id] = bool(auto)
        self._running_types[room_id] = self._normalize_types(only)
        self._interrupts[room_id] = asyncio.Event()
        label = room_label or str(room_id)
        started = time.monotonic()
        try:
            self._emit("medal_start", {"room_id": room_id, "room_label": label,
                                       "only": only, "auto": bool(auto)})
            logger.info("粉丝牌任务开始：房间 %s（%s），类型 %s，来源 %s",
                        room_id, label,
                        "、".join(task_label(t) for t in sorted(
                            self._running_types.get(room_id, ()))),
                        "自动" if auto else "手动")
            result = await self._complete_locked(room_id, int(anchor_uid),
                                                 int(live_status or 0), label, only)
        finally:
            self._running.discard(room_id)
            self._running_auto.pop(room_id, None)
            self._running_types.pop(room_id, None)
            self._interrupts.pop(room_id, None)
        result["elapsed"] = round(time.monotonic() - started, 1)
        logger.info("粉丝牌任务结束：房间 %s（%s），状态 %s：%s（用时 %ss）",
                    room_id, label, result.get("status"), result.get("message"),
                    result.get("elapsed"))
        return result

    @staticmethod
    def _normalize_types(only: Optional[Union[str, Iterable[str]]]) -> Set[str]:
        """把 ``only`` 归一化成类型集合（``None`` = 全部写任务类型）。"""
        if only is None:
            return set(WRITE_TASK_TYPES)
        if isinstance(only, str):
            return {only}
        return {str(item) for item in only}

    async def interrupt_auto_danmaku(self, room_id: int) -> bool:
        """打断该房间正在执行的**自动**发弹幕任务（开播信号触发，ROADMAP 64）。

        仅当三条同时满足才置位打断标记并返回 ``True``：① 该房间有任务在执行；
        ② 本次执行由自动触发（``auto=True``，手动按钮不受开播信号影响）；
        ③ 本次执行包含发弹幕。置位后执行体在**下一个检查点**立即停止（等待间隔会被
        提前唤醒，已发出的弹幕不撤回），点赞等其它任务类型不受影响。

        该方法由 GUI 提交到 asyncio 事件循环内执行（``hub.submit``），因此可以安全
        地从界面线程调用 ``asyncio.Event``。
        """
        room_id = int(room_id)
        event = self._interrupts.get(room_id)
        if event is None or not self._running_auto.get(room_id, False):
            return False
        if TASK_SEND_DANMAKU not in self._running_types.get(room_id, set()):
            return False
        if event.is_set():
            return False
        event.set()
        logger.info("已打断自动发弹幕：房间 %s（收到开播信号，已发出的弹幕保留）", room_id)
        self._emit("medal_interrupt", {
            "room_id": room_id,
            "message": "直播间已开播，已打断自动发弹幕任务（默认仅未开播时自动发弹幕）",
        })
        return True

    def _interrupted(self, room_id: int) -> bool:
        """该房间是否已被置位打断标记（执行体检查点使用）。"""
        event = self._interrupts.get(int(room_id))
        return bool(event is not None and event.is_set())

    async def _sleep_or_interrupt(self, room_id: int, seconds: float) -> bool:
        """等待间隔，被开播打断则提前返回 ``True``（不必等本次等待走完）。"""
        event = self._interrupts.get(int(room_id))
        if event is None:
            await asyncio.sleep(seconds)
            return False
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, float(seconds)))
        except asyncio.TimeoutError:
            return False
        return True

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
        # reach_free_intimacy_limit（接口下发）为真表示**该粉丝灯牌刚点亮**：服务端暂时
        # 不接受点赞 / 发弹幕这类免费任务产生的亲密度（官方规则：熄灭状态下靠点赞 30 次 /
        # 发弹幕 10 条点亮勋章时，这两种行为仅点亮勋章、不获得亲密度）。
        # 但任务**仍要照常执行**——长时间不做任务灯牌会熄灭，做任务正是为了点亮并维持它；
        # 所以这里只把「亲密度为什么没涨」说明给用户，绝不跳过任务。
        free_intimacy_paused = bool(info.get("reach_free_intimacy_limit"))
        if free_intimacy_paused:
            logger.info("房间 %s（%s）的粉丝灯牌刚点亮：免费任务暂不增加亲密度，"
                        "任务照常执行以点亮并维持灯牌", room_id, label)

        def _with_note(text: str) -> str:
            """给结果说明补上「刚点亮 → 暂不涨亲密度」的缘由（未命中时原样返回）。"""
            if not free_intimacy_paused:
                return text
            return (f"{text}（粉丝灯牌刚点亮，点赞 / 发弹幕暂不涨亲密度，"
                    "任务仅用于点亮并维持灯牌）")

        pending_all = pending_write_tasks(tasks)
        only_types: Optional[Set[str]] = None
        if only is not None:
            only_types = {only} if isinstance(only, str) else {str(x) for x in only}
            if not only_types:
                only_types = None
        if only_types is not None:
            pending = [t for t in pending_all if t.get("jump_type") in only_types]
            if not pending:
                reasons = [self._skip_reason(tasks, t) for t in sorted(only_types)]
                return self._result(room_id, "done", _with_note("；".join(reasons)))
        else:
            pending = pending_all
            if not pending:
                return self._result(room_id, "done",
                                    _with_note("今日粉丝牌任务已完成"))

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

        result = self._summarize(room_id, details)
        result["message"] = _with_note(result["message"])
        return result

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
            if is_light_up_task(task):
                # 「仅点亮」态（上限 0）：勋章已熄灭，点赞的唯一目的是重新点亮勋章
                # （长时间不做任务灯牌会熄灭）。点亮阶段不产生亲密度、接口也不下发进度，
                # 故按官方点亮次数发一次、复核一次即收尾，不做多轮「进度推进」判定。
                clicks = parse_title_count(task.get("title")) or LIGHT_UP_LIKE_CLICKS
                try:
                    await self._api.like_room(room_id, anchor_uid, click_time=clicks)
                    actions += 1
                    clicks_total += clicks
                except ApiError as exc:
                    return _done(False, describe_like_error(exc.code, exc.message),
                                 risk=exc.code in RISK_CONTROL_CODES)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    return _done(False, f"网络异常：{exc}")
                after = await self._refetch_task(anchor_uid, TASK_LIKE)
                if after is not None and not is_light_up_task(after):
                    return _done(True, f"勋章已重新点亮（点赞 {clicks} 次）")
                return _done(True, f"勋章已熄灭：已发起点赞（{clicks} 次）用于重新点亮")
            limit = int(task.get("limit") or 0)
            current = int(task.get("current") or 0)
            # 每轮复核后即上报最新进度，供界面实时更新该行（无需等整轮结束）
            self._emit("medal_task_progress", {
                "room_id": room_id, "jump_type": TASK_LIKE,
                "current": current, "limit": limit, "is_done": False})
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

        def _done(ok: bool, message: str, *, risk: bool = False,
                  interrupted: bool = False) -> Dict[str, Any]:
            return {"ok": ok, "message": message, "risk": risk,
                    "interrupted": interrupted, "sent": sent,
                    "used_emoticon": used_emoticon, "used_text": used_text,
                    "elapsed": round(time.monotonic() - started, 1)}

        max_stall = max(1, int(self._config.medal_max_retry))
        stalled = 0
        progress = -1
        rounds = 0
        while True:
            if self._interrupted(room_id):
                # 开播信号已到达（默认仅未开播时自动发弹幕）：立即停止后续发送
                return _done(False, "直播间已开播，已打断自动发弹幕任务",
                             interrupted=True)
            task = await self._refetch_task(anchor_uid, TASK_SEND_DANMAKU)
            if task is None:
                return _done(False, "接口未返回发弹幕任务")
            if is_task_complete(task):
                return _done(True, "发弹幕任务已完成")
            if is_light_up_task(task):
                # 「仅点亮」态（上限 0）：同点赞，唯一目的是重新点亮勋章；接口不下发进度，
                # 故按官方点亮条数发满后收尾（不按「进度推进」判定）。
                while sent < LIGHT_UP_DANMAKU_COUNT:
                    if self._interrupted(room_id):
                        return _done(False, "直播间已开播，已打断自动发弹幕任务",
                                     interrupted=True)
                    emoticon, index = select_emoticon_cycle(emoticons, index)
                    try:
                        if emoticon:
                            await self._api.send_danmaku(
                                room_id, str(emoticon.get("unique") or ""),
                                emoticon=emoticon)
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
                        return _done(False, last_error)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        return _done(False, f"网络异常：{exc}")
                    if sent < LIGHT_UP_DANMAKU_COUNT and await self._sleep_or_interrupt(
                            room_id,
                            compute_action_delay(*self._config.medal_danmaku_interval)):
                        return _done(False, "直播间已开播，已打断自动发弹幕任务",
                                     interrupted=True)
                return _done(True, f"勋章已熄灭：已发送 {sent} 条弹幕用于重新点亮")
            limit = int(task.get("limit") or 0)
            current = int(task.get("current") or 0)
            # 每轮复核后即上报最新进度，供界面实时更新该行（无需等整轮结束）
            self._emit("medal_task_progress", {
                "room_id": room_id, "jump_type": TASK_SEND_DANMAKU,
                "current": current, "limit": limit, "is_done": False})
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
            if self._interrupted(room_id):
                # 复核（await）期间到达的开播信号：发送前再确认一次，不再多发一条
                return _done(False, "直播间已开播，已打断自动发弹幕任务",
                             interrupted=True)
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
                if await self._sleep_or_interrupt(
                        room_id,
                        compute_action_delay(*self._config.medal_danmaku_interval)):
                    return _done(False, "直播间已开播，已打断自动发弹幕任务",
                                 interrupted=True)
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
            if await self._sleep_or_interrupt(room_id, wait):
                return _done(False, "直播间已开播，已打断自动发弹幕任务",
                             interrupted=True)

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
        has_interrupt = False
        any_ok = False
        any_fail = False
        for key, label in (("like", "点赞"), ("danmaku", "发弹幕")):
            outcome = details.get(key)
            if not outcome:
                continue
            if outcome.get("risk"):
                has_risk = True
            if outcome.get("interrupted"):
                has_interrupt = True
            if outcome.get("ok"):
                any_ok = True
            elif outcome.get("skipped") or outcome.get("interrupted"):
                # 跳过（如未开播暂无点赞）/ 被开播打断，均不算失败，也不计入完成
                pass
            else:
                any_fail = True
            parts.append(
                f"{label}：{outcome.get('message') or '完成'}"
                f"{self._format_metrics(key, outcome)}")
        message = "；".join(parts) if parts else "今日粉丝牌任务已完成"
        if has_risk:
            status = "risk"
        elif has_interrupt and not any_fail:
            # 打断是「开播后本就不该继续」的正常收尾（已发送的弹幕保留），单独状态
            status = "interrupted"
        elif any_fail and any_ok:
            status = "partial"
        elif any_fail:
            status = "error"
        else:
            status = "done"
        return self._result(room_id, status, message, details)
