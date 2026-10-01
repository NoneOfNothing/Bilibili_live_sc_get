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
from typing import Any, Dict, Iterable, List, Optional, Tuple

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

LIGHT_UP_LIKE_CLICKS = 30
"""官方「点亮任务」的点赞次数（勋章熄灭时靠它重新点亮）。"""

LIGHT_UP_DANMAKU_COUNT = 10
"""官方「点亮任务」的发弹幕条数（同上）。"""

# ---- 亲密度机制的用户可见说明（Tk 与 Qt 两版共用同一份文案，避免两份漂移） ----
# 口径来源：2026-05-14 起生效的官方规则（用户提供并确认），替换掉此前基于未验证字段
# 语义写的「粉丝灯牌刚点亮」解释——见 ROADMAP 101。

MEDAL_RULES_HELP = (
    "· 点亮与维持：粉丝牌点亮后，3 天内没完成任何点亮任务就会熄灭"
    "（大航海生效期间不会熄灭）；本页的「点赞 / 发弹幕」就是点亮任务。\n"
    "· 熄灭后重新点亮：完成任意一个点亮任务即可——点赞、发弹幕、看播，"
    "或投喂付费礼物、开通大航海、给主播视频充电这类付费行为。\n"
    "· 熄灭的影响：不掉亲密度、也不掉等级；但熄灭并重新点亮后，免费互动拿到的"
    "亲密度会先进「储蓄」，要任一种付费行为（灯牌 / 电池礼物 / 大航海 / 充电都行，"
    "与灯牌当前是否点亮无关）才能领取。\n"
    "· 清零：删除勋章或退出粉丝团会把亲密度清零。"
)
"""粉丝牌「点亮 / 熄灭 / 清零」规则全文（两版「ⓘ 规则」弹窗共用）。

只写「会影响怎么用」的规则，不堆数值（每日额度与点赞换算等一律不写，用户要求精简）；
正文直接进弹窗（messagebox / QMessageBox），故**不写 Markdown 强调标记**——
星号会原样显示在弹窗里。
"""

MEDAL_ACQUIRE_HINT = "拿牌方式：投喂一个粉丝团灯牌 / 给该主播视频充电 1 B 币 / 开通大航海"
"""未持有粉丝牌时的取得方式（两版共用；规则弹窗末尾也附这句）。"""

SAVINGS_MODE_NOTE = (
    "免费互动（点赞 / 发弹幕 / 观看）获得的亲密度会先攒进储蓄池，"
    "攒满 100 后停止累计，需投喂付费礼物才能领取；任务仍照做以点亮并维持灯牌"
)
"""「储蓄模式」说明（接口 ``reach_free_intimacy_limit`` 为真时给用户的缘由）。

点亮后免费互动拿到的亲密度先进入**储蓄池**（接口字段 ``free_intimacy``，实测取值
0 / 12 / 100），攒满 100 即 ``reach_free_intimacy_limit=true``、免费互动不再累加，
要投喂付费礼物才能把储蓄领出来（转为亲密度）。这与「灯牌熄灭时靠免费互动只能点亮、
拿不到亲密度」是两件事：本条描述的是**已点亮**状态下的储蓄机制。任务照常执行
（长时间不做点亮任务灯牌会熄灭），故这里只解释「为什么亲密度没涨」。
"""

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


def should_auto_danmaku(live_status: int, when_live: bool) -> bool:
    """自动发弹幕本轮是否应执行：**未开播时总是执行**；开播时需 ``when_live`` 允许。

    发弹幕出现在主播弹幕区会造成刷屏，故自动任务默认只在**未开播**时执行；
    用户显式开启「开播时也自动发弹幕」后才在直播中执行（手动按钮不受此限制）。
    """
    if _int(live_status) == 1:
        return bool(when_live)
    return True


def auto_task_types(*, auto_danmaku: bool, auto_like: bool,
                    auto_danmaku_when_live: bool, live_status: int) -> List[str]:
    """本轮「自动任务」该执行哪些类型（Tk / Qt 两版共用，避免各自实现漂移）。

    - **发弹幕**：受「允许开播时自动发弹幕」约束（未开播恒执行；开播时需该开关为真）；
    - **点赞**：仅在直播中有意义（执行引擎也会跳过），未开播时不提交，省掉必然被
      跳过的请求；
    - 两项都满足时**一起返回**：由同一次执行依次完成——若分成两次提交，先提交的
      那一项会让房间进入「执行中」，后一项会被长期挡住（发弹幕可执行时点赞永远轮不到）。

    返回空列表表示该房间本轮无事可做。自动执行的整体前置条件（总开关 / 写操作 /
    监听启用 / 未持有粉丝牌等）由调用方负责。
    """
    types: List[str] = []
    if auto_danmaku and should_auto_danmaku(live_status, auto_danmaku_when_live):
        types.append(TASK_SEND_DANMAKU)
    if auto_like and _int(live_status) == 1:
        types.append(TASK_LIKE)
    return types


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


def is_light_up_task(task: Any) -> bool:
    """任务是否处于「仅点亮」态：未完成、但上限为 0（勋章已熄灭）。

    此时做任务**不产生亲密度**，唯一目的是重新点亮勋章——点亮后 3 天内没完成任何点亮
    任务就会熄灭（大航海生效期间不会熄灭），点亮之后才谈得上继续维持。点亮阶段的次数由
    官方固定（点赞 30 次 / 发弹幕 10 条），接口不下发进度，故按固定次数执行、不按
    「进度推进」判定。

    **熄灭后用免费互动（点赞 / 发弹幕 / 观看）恢复，只能点亮灯牌、拿不到亲密度**，要投喂
    付费礼物才既能点亮又加亲密度；重新点亮后任务面板会切换成**储蓄任务模式**，任务照做
    也得先投喂才能把经验领出来（口径见 ``SAVINGS_MODE_NOTE``）。
    """
    if not isinstance(task, dict):
        return False
    if is_task_complete(task):
        return False
    return _int(task.get("limit")) <= 0


def pending_write_tasks(tasks: List[dict]) -> List[dict]:
    """返回仍未完成的写任务（点赞 / 发弹幕），**含「仅点亮」态**（上限 0）。

    仅点亮态同样要执行：灯牌熄灭后只有做任务才能把它重新点亮
    （见 ``is_light_up_task``），跳过不做的代价是灯牌一直熄着。这也正是「点亮后 3 天内
    没完成任何点亮任务就会熄灭」的应对手段——这些任务的作用是**点亮并维持灯牌**，
    而不是可有可无的刷分。
    """
    return [task for task in tasks or []
            if task.get("jump_type") in WRITE_TASK_TYPES
            and (is_task_applicable(task) or is_light_up_task(task))]


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


def shows_medal_tasks(task_info: Any) -> bool:
    """该房间要不要在「粉丝牌任务」表里占一行（ROADMAP 104）。

    **未持有该主播粉丝牌**的房间（任务刷新时被标记 ``no_medal``）不显示——那一行本来就只
    三个「无粉丝牌」字，既没有任务可看、也不能执行任何任务。状态**尚未取到**时
    （``None`` / 缺该键 / 非 dict）一律照常显示：等刷新给出结论再收掉，避免启动时整表先空
    一下又长回来。
    """
    if not isinstance(task_info, dict):
        return True
    return not bool(task_info.get("no_medal"))


def medal_for_room(medals: List[dict], uid: Any = 0, room_id: Any = 0) -> Optional[dict]:
    """在粉丝牌列表里找某个直播间的粉丝牌；找不到返回 None。

    判定口径与 ``monitored_medals``、任务表「该房间是否持有粉丝牌」一致：粉丝牌的
    ``target_id`` 等于主播 uid，**或** ``room_id`` 等于该房间号（**真实房间号**，短号场景
    由调用方先映射）。uid 与房间号都为 0 时直接返回 None（无从匹配）。
    """
    wanted_uid = _int(uid)
    wanted_room = _int(room_id)
    if not wanted_uid and not wanted_room:
        return None
    for medal in medals or []:
        if not isinstance(medal, dict):
            continue
        if wanted_uid and _int(medal.get("target_id")) == wanted_uid:
            return medal
        if wanted_room and _int(medal.get("room_id")) == wanted_room:
            return medal
    return None


def partition_medal_task_rooms(rooms: Iterable[Tuple[Any, Any, Any]],
                               medals: Optional[List[dict]]
                               ) -> Dict[str, List]:
    """把「要刷任务的房间」按粉丝牌分成三类（ROADMAP 105）。

    ``rooms`` 为 ``(房间号, 主播 uid, 真实房间号)`` 三元组序列（短号场景由调用方先映射）。
    返回 ``{"fetch": [(房间号, uid), …], "no_medal": [房间号, …], "no_uid": [房间号, …]}``：

    - ``fetch``：在粉丝牌列表里**找得到牌子**的房间——只有这些需要请求任务接口；
    - ``no_medal``：已有粉丝牌列表但找不到牌子的房间——直接判为「无粉丝牌」，**零请求**，
      也不参与节流（此前它们虽然不请求，却会让每个房间都白等 0.5 秒）；
    - ``no_uid``：连主播 uid 都没有的房间（接口按 uid 查询，本就查不了）。

    ``medals`` 传 ``None`` 表示**没有可用的粉丝牌列表**（未就绪，或本次是「点名就查」的
    定向刷新）：此时不做无牌判定，凡有 uid 的房间都进 ``fetch``——调用方若要「未就绪就跳过
    本轮」，应在调用前自行判断并提示，不要让它退化成「所有房间都没牌子」。传空列表 ``[]``
    是**另一种意思**：列表已就绪、账号确实一张牌子都没有 → 所有房间都判为无牌。
    """
    fetch: List[Tuple[int, int]] = []
    no_medal: List[int] = []
    no_uid: List[int] = []
    for room_id, uid, real_room in rooms or []:
        room_value = _int(room_id)
        uid_value = _int(uid)
        if not uid_value:
            no_uid.append(room_value)
            continue
        if medals is None or medal_for_room(medals, uid_value, real_room):
            fetch.append((room_value, uid_value))
        else:
            no_medal.append(room_value)
    return {"fetch": fetch, "no_medal": no_medal, "no_uid": no_uid}


def monitored_medals(medals: List[dict],
                     rooms: Iterable[Tuple[Any, Any]]) -> Tuple[List[dict], int]:
    """只保留**已加入监听列表**的直播间的粉丝牌，返回 ``(保留的, 隐藏数量)``（ROADMAP 103）。

    ``rooms`` 为各监听房间的 ``(主播 uid, 真实房间号)`` 序列（短号场景由调用方先映射成
    真实房间号）。匹配规则与「该直播间是否持有粉丝牌」的判定保持一致：粉丝牌的
    ``target_id`` 等于某个房间的主播 uid，**或**粉丝牌的 ``room_id`` 等于某个房间的房间号。

    为什么要隐藏而不是照常列出：储蓄亲密度（``free_intimacy``）是**随任务接口**一起下来
    的，而任务只对监听列表里的房间拉取，未加入列表的粉丝牌永远拿不到储蓄值、只能显示今日
    部分——留在列表里会被误读成「这个牌子的储蓄是 0」。隐藏数量由调用方在状态行说明，
    免得用户以为牌子丢了。
    """
    wanted_uids: set = set()
    wanted_rooms: set = set()
    for uid, room_id in rooms or []:
        uid_value = _int(uid)
        room_value = _int(room_id)
        if uid_value:
            wanted_uids.add(uid_value)
        if room_value:
            wanted_rooms.add(room_value)
    kept: List[dict] = []
    hidden = 0
    for medal in medals or []:
        if not isinstance(medal, dict):
            continue
        uid = _int(medal.get("target_id"))
        room_id = _int(medal.get("room_id"))
        if (uid and uid in wanted_uids) or (room_id and room_id in wanted_rooms):
            kept.append(medal)
        else:
            hidden += 1
    return kept, hidden


def format_intimacy_change(today_feed: Any, free_intimacy: Any = None) -> str:
    """粉丝牌列表「亲密度变化」列的文案（两版共用，ROADMAP 102）。

    **一格只显示一段**——该列很窄，而两者又不会同时为正：今天直接入账的亲密度与储蓄池里
    待领取的额度是互斥的（进了储蓄模式后，免费互动拿到的亲密度先进储蓄、不进今日账）。
    故：

    - 今日有值 → ``+30（今日亲密度）``；
    - 今日为 0 而储蓄有值 → ``26（储蓄亲密度）``（满池即 ``100（储蓄亲密度）``）；
    - 两者都没有 → ``+0（今日亲密度）``。

    今日已获取来自粉丝牌列表接口（``today_feed``），储蓄池内累计来自任务接口
    （``free_intimacy``，实测 0 / 12 / 100）；``free_intimacy`` 传 ``None`` 表示**该房间的
    任务信息还没取到**（两个接口到达时间不同，粉丝牌列表先到），此时按「没有储蓄」显示。
    """
    today = _int(today_feed)
    if today > 0:
        return f"+{today}（今日亲密度）"
    savings = 0 if free_intimacy is None else _int(free_intimacy)
    if savings > 0:
        return f"{savings}（储蓄亲密度）"
    return "+0（今日亲密度）"


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
