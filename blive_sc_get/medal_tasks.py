"""粉丝牌任务相关的**纯函数**：接口数据解析、任务完成判定、发送内容选择。

本模块不做任何网络请求、不依赖 aiohttp / tkinter，便于离线单元测试
（与 ``api.py`` 的 ``parse_room_emoticon_packages`` 风格保持一致）。
``api.py`` / ``medal_runner.py`` / GUI 共用这里的归一化结构：

任务项（``normalize_task`` 的产物）::

    {
        "jump_type": "like" | "sendDanmu" | "watchLive" | ...,
        "title": str,          # 接口给的标题（可能含次数，如「点赞 30 次」）
        "current": int,        # 已完成的次数（从 sub_title「当前/上限」解析）
        "limit": int,          # 每日上限（随粉丝牌等级提升、每日刷新，来自接口）
        "is_done": bool,       # 接口标记的任务是否已完成
        "raw": dict,           # 原始项，便于扩展
    }
"""

from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional, Tuple

# ---- jump_type 常量（接口 task_info[].jump_type） ----
TASK_LIKE = "like"
TASK_SEND_DANMAKU = "sendDanmu"
TASK_WATCH_LIVE = "watchLive"
TASK_FEED_LIGHT = "feedLight"
TASK_SEND_GIFT = "sendGift"

TASK_LABELS: Dict[str, str] = {
    TASK_LIKE: "点赞",
    TASK_SEND_DANMAKU: "发弹幕",
    TASK_WATCH_LIVE: "观看直播",
    TASK_FEED_LIGHT: "投喂粉丝灯牌",
    TASK_SEND_GIFT: "投喂礼物",
}
"""jump_type -> 中文名（未收录的类型回退为原始值）。"""

WRITE_TASK_TYPES: Tuple[str, ...] = (TASK_LIKE, TASK_SEND_DANMAKU)
"""本功能会**执行**的写任务类型（其余仅展示）。"""

ROOM_EMOTICON_PREFIX = "room_"
"""直播间专属表情的 ``emoticon_unique`` 前缀（区分公开表情）。"""

_PROGRESS_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_INT_RE = re.compile(r"\d+")


def _int(value: Any) -> int:
    """宽松取整：非法/缺失一律回退 0。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def task_label(jump_type: str) -> str:
    """jump_type 的中文名；未知类型原样返回。"""
    key = str(jump_type or "").strip()
    return TASK_LABELS.get(key, key)


def parse_task_progress(sub_title: Any) -> Tuple[int, int]:
    """从 ``sub_title`` 里解析「当前/上限」进度，如 ``"3/30"`` → ``(3, 30)``。

    解析不到返回 ``(0, 0)``。上限由服务端按粉丝牌等级动态下发、每日刷新，
    故一律以接口为准，不在此硬编码。
    """
    if not isinstance(sub_title, str):
        return (0, 0)
    match = _PROGRESS_RE.search(sub_title)
    if not match:
        return (0, 0)
    return (_int(match.group(1)), _int(match.group(2)))


def parse_title_count(title: Any) -> Optional[int]:
    """从任务标题里取第一个数字（如「点赞 30 次」→ 30）；没有返回 None。"""
    if not isinstance(title, str):
        return None
    match = _INT_RE.search(title)
    if not match:
        return None
    return _int(match.group(0))


def normalize_task(raw: Any) -> Optional[dict]:
    """把接口返回的单个 task_info 项归一化为统一结构；非法项返回 None。"""
    if not isinstance(raw, dict):
        return None
    current, limit = parse_task_progress(raw.get("sub_title"))
    return {
        "jump_type": str(raw.get("jump_type") or "").strip(),
        "title": str(raw.get("title") or "").strip(),
        "current": current,
        "limit": limit,
        "is_done": bool(raw.get("is_done")),
        "raw": raw,
    }


def parse_task_info_list(info: Any) -> List[dict]:
    """从 ``GetActivatedMedalInfo`` 的 ``data`` 字段解析任务列表。"""
    if not isinstance(info, dict):
        return []
    seq = info.get("task_info")
    if not isinstance(seq, list):
        return []
    tasks = [normalize_task(item) for item in seq]
    return [task for task in tasks if task]


def find_task(tasks: List[dict], jump_type: str) -> Optional[dict]:
    """在任务列表中按 jump_type 查找第一项。"""
    target = str(jump_type or "")
    for task in tasks or []:
        if task.get("jump_type") == target:
            return task
    return None


def is_task_complete(task: Any) -> bool:
    """任务是否已完成：接口 ``is_done`` 为真，或进度已达上限（完成即停判据）。"""
    if not isinstance(task, dict):
        return True
    if task.get("is_done"):
        return True
    limit = _int(task.get("limit"))
    current = _int(task.get("current"))
    return limit > 0 and current >= limit


def is_task_applicable(task: Any) -> bool:
    """任务当前是否**可执行**：有正数上限且尚未完成。

    ``sub_title`` 为「仅点亮」这类非进度文案时解析出的 ``limit`` 为 0，表示该任务
    当前不适用（例如**粉丝牌未点亮**），不应执行——否则会一直空发请求直到重试耗尽。
    """
    if not isinstance(task, dict):
        return False
    if is_task_complete(task):
        return False
    return _int(task.get("limit")) > 0


def pending_write_tasks(tasks: List[dict]) -> List[dict]:
    """返回仍未完成、且当前可执行的写任务（点赞 / 发弹幕）。"""
    return [task for task in tasks or []
            if task.get("jump_type") in WRITE_TASK_TYPES and is_task_applicable(task)]


def normalize_medal(raw: Any, is_special: bool = False) -> Optional[dict]:
    """归一化单个粉丝勋章项。

    兼容两种来源形态：``panel`` 的嵌套结构（``medal`` / ``room_info`` /
    ``anchor_info``）与 ``GetMyMedals`` 的扁平结构。缺少 ``medal_id`` 的项丢弃。
    """
    if not isinstance(raw, dict):
        return None
    medal = raw.get("medal") if isinstance(raw.get("medal"), dict) else raw
    room_info = raw.get("room_info") if isinstance(raw.get("room_info"), dict) else {}
    anchor_info = raw.get("anchor_info") if isinstance(raw.get("anchor_info"), dict) else {}
    medal_id = _int(medal.get("medal_id") or raw.get("medal_id"))
    if not medal_id:
        return None
    living_raw = room_info.get("living_status")
    living_status = _int(living_raw) if living_raw is not None else _int(raw.get("living_status"))
    return {
        "medal_id": medal_id,
        "medal_name": str(medal.get("medal_name") or raw.get("medal_name") or "").strip(),
        "level": _int(medal.get("level") or raw.get("level")),
        "target_id": _int(medal.get("target_id") or raw.get("target_id")),
        "room_id": _int(room_info.get("room_id") or raw.get("roomid") or medal.get("roomid")),
        "living_status": living_status,
        "is_lighted": _int(medal.get("is_lighted") or raw.get("is_lighted")),
        "anchor_name": str(anchor_info.get("nick_name") or raw.get("uname")
                           or raw.get("target_name") or "").strip(),
        "intimacy": _int(medal.get("intimacy") or raw.get("intimacy")),
        "today_feed": _int(medal.get("today_feed") or raw.get("today_feed")),
        "next_intimacy": _int(medal.get("next_intimacy") or raw.get("next_intimacy")),
        "is_special": bool(is_special),
        "raw": raw,
    }


def parse_medal_panel(data: Any) -> List[dict]:
    """解析 ``panel`` 接口单页的 ``data``：``special_list`` + ``list`` 合并归一化。"""
    if not isinstance(data, dict):
        return []
    medals: List[dict] = []
    for key in ("special_list", "list"):
        seq = data.get(key)
        if not isinstance(seq, list):
            continue
        for item in seq:
            medal = normalize_medal(item, is_special=(key == "special_list"))
            if medal:
                medals.append(medal)
    return medals


def dedupe_medals(medals: List[dict]) -> List[dict]:
    """按 ``medal_id`` 跨页去重（保留首次出现顺序，special 在前）。"""
    result: List[dict] = []
    seen: set = set()
    for medal in medals or []:
        medal_id = _int(medal.get("medal_id")) if isinstance(medal, dict) else 0
        if not medal_id or medal_id in seen:
            continue
        seen.add(medal_id)
        result.append(medal)
    return result


def medal_level_for_room(medals: List[dict], room_id: int = 0, target_id: int = 0) -> int:
    """按房间号或主播 uid 查找粉丝牌等级；找不到返回 0。"""
    for medal in medals or []:
        if not isinstance(medal, dict):
            continue
        if room_id and _int(medal.get("room_id")) == _int(room_id):
            return _int(medal.get("level"))
        if target_id and _int(medal.get("target_id")) == _int(target_id):
            return _int(medal.get("level"))
    return 0


def room_exclusive_emoticons(packages: Any) -> List[dict]:
    """从 ``get_room_emoticons`` 的分组结果里挑出**直播间专属**表情（按 unique 去重）。

    专属表情的 ``emoticon_unique`` 以 ``room_`` 开头；公开表情不带该前缀，故被排除。
    """
    result: List[dict] = []
    seen: set = set()
    for pkg in packages or []:
        if not isinstance(pkg, dict):
            continue
        for emoticon in pkg.get("emoticons") or []:
            if not isinstance(emoticon, dict):
                continue
            unique = str(emoticon.get("unique") or "").strip()
            if not unique.startswith(ROOM_EMOTICON_PREFIX) or unique in seen:
                continue
            seen.add(unique)
            result.append(emoticon)
    return result


def select_emoticon_cycle(emoticons: List[dict], index: int) -> Tuple[Optional[dict], int]:
    """按序轮次选择表情：返回 ``(表情, 下一个序号)``；列表为空时返回 ``(None, 0)``。"""
    items = [e for e in (emoticons or [])
             if isinstance(e, dict) and str(e.get("unique") or "").strip()]
    if not items:
        return (None, 0)
    position = int(index) % len(items)
    return (items[position], (position + 1) % len(items))


def next_fallback_text(current: Any) -> str:
    """无专属表情时的纯数字回退序列：``""→"1"``、``"1"→"2"``，非数字→``"1"``。"""
    text = str(current or "").strip()
    if text.isdigit():
        return str(int(text) + 1)
    return "1"


def compute_action_delay(base_min: float, base_max: float,
                         rng: Optional[Any] = None) -> float:
    """在 ``[base_min, base_max]`` 内取随机间隔（秒），用于节流；``rng`` 可注入便于测试。"""
    if rng is None:
        rng = random.random
    low = max(0.0, float(base_min))
    high = max(low, float(base_max))
    return round(low + (high - low) * float(rng()), 3)
