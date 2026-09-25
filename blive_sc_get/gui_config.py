"""GUI 直播间列表的持久化：房间号、备注、启用状态。

独立于 tkinter，便于单元测试。文件为 ``<output_dir>/gui_rooms.json``。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from .log_categories import CATEGORY_DATA, get_logger

logger = get_logger(CATEGORY_DATA, __name__)

SORT_MODES = ("manual", "room", "anchor", "status")
"""排序方案白名单。

``manual`` 即界面上的「自定义排序」（显示用户拖动/手动调整得到的顺序）；
``status`` 即「按直播状态」——直播中的按开播时间倒序，下播的（含轮播中，不做特殊
处理）按关播时间倒序，随开播/关播信号实时重排。ROADMAP 85 起「直播中置顶」不再是
独立开关，而是并入 ``status``。
"""

# notify_overlay：开播时是否弹右下角自绘悬浮窗（全局主开关；每房间提醒列独立控制是否提醒）
# notify_persist：悬浮窗是否常驻（不自动关闭，需点击才消失；仅在 notify_overlay 开启时生效）
# notify_sound：开播提示音效（键名与 GUI 播放器映射表一致）
NOTIFY_SOUNDS = ("上行双音", "三连音", "Windows 系统提示音", "静音")
DEFAULT_NOTIFY_SOUND = "上行双音"

QUICK_DANMAKU_MAX = 12
"""每个直播间的快捷弹幕最多保存多少条（下拉过长不好用，也限制配置文件体积）。"""

QUICK_DANMAKU_LEN = 20
"""单条快捷弹幕的最大长度（与 ``gui_app.DANMAKU_MAX_LEN`` 一致：B 站弹幕长度上限）。"""


def normalize_quick_danmaku(value: Any, max_len: int = QUICK_DANMAKU_LEN) -> List[str]:
    """清洗某个直播间的快捷弹幕文本列表（纯函数，ROADMAP 90）。

    只保留**非空字符串**：去首尾空白、超长按 ``max_len`` 截断、保序去重，
    最多 ``QUICK_DANMAKU_MAX`` 条。非列表 / 含非字符串项一律丢弃而**不抛异常**
    ——配置里的坏数据不该让整个房间列表读不出来。
    """
    if not isinstance(value, (list, tuple)):
        return []
    result: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:max_len]
        if not text or text in result:
            continue
        result.append(text)
        if len(result) >= QUICK_DANMAKU_MAX:
            break
    return result


DEFAULT_UI_PREFS: Dict[str, object] = {
    "sort_mode": "manual",
    "notify_overlay": True,
    "notify_persist": False,
    "notify_sound": DEFAULT_NOTIFY_SOUND,
    "dm_visible": False,
    # dm_emoticon_image：弹幕流里是否直接显示表情图片（Qt 版专有，默认开启）。
    # 关闭后只显示「[触发词]」文字，行高更矮；悬浮提示此时仍可看原图（与 Tk 版一致）。
    "dm_emoticon_image": True,
    # window_size：上次退出时的窗口尺寸 [宽, 高]（逻辑像素）；[0, 0] 表示尚未记忆
    "window_size": [0, 0],
    # room_windows：房间独立窗口（ROADMAP 84）的位置尺寸记忆，
    # {"<房间号>": {"x": .., "y": .., "width": .., "height": ..}}；只记几何，不记「是否打开」
    "room_windows": {},
    # live_started_at / live_offline_at：「按直播状态」排序的时间基准（ROADMAP 87）——
    # 各房间的**真实开播时刻**与**最近关播时刻**（epoch 秒），{"<房间号>": 时刻}。
    # 持久化后重启仍按同样的先后排序（此前只存在内存里，重启即丢，顺序被打乱）；
    # 启动读取时会丢弃过旧 / 非法的记录（见 gui_app.prune_live_marks）。
    "live_started_at": {},
    "live_offline_at": {},
}
"""界面偏好默认值。

``sort_mode`` 记忆上次选的排序方案（两版共用；ROADMAP 85 起「直播中置顶」并入
「按直播状态」，旧配置的 ``pin_live`` 会在读取时迁移过去）。
``window_size`` 由 Qt 版在退出时写入、启动时恢复（Tk 版不使用，只原样写回）；
``dm_emoticon_image`` 同理只作用于 Qt 版（Tk 版不做内嵌，恒为文字）；
``room_windows`` 两版共用（按房间记忆独立窗口几何）。
"""


@dataclass
class RoomEntry:
    room_id: int
    note: str = ""
    enabled: bool = True
    uid: int = 0
    """主播 uid，用于跳转个人空间；旧配置缺省为 0（未知）。"""

    notify_live: bool = True
    """该直播间开播时是否提醒（提示音 + 任务栏闪烁）。"""

    auto_like: bool = False
    """该直播间是否加入「点赞」任务的全自动执行（默认关闭）。"""

    auto_danmaku: bool = False
    """该直播间是否加入「发弹幕」任务的全自动执行（默认关闭）。

    两项均仅在 ``config.json`` 的 ``medal_tasks.auto`` 与
    ``allow_write_operations`` 同时开启时生效；关闭时仍可用对应按钮手动执行。
    """

    auto_danmaku_when_live: bool = False
    """开播时是否也执行「自动发弹幕」（默认关闭 = **仅未开播时**自动发弹幕）。

    发弹幕会出现在直播弹幕区，默认避免在开播时自动刷屏；显式开启后才在开播时执行。
    仅约束**自动**任务，手动「发弹幕」按钮不受影响。
    """

    quick_danmaku: List[str] = field(default_factory=list)
    """该直播间的快捷弹幕文本（**按房间独立**，ROADMAP 90）。

    界面上点选后**只填入发送输入框、不直接发送**（可先改后发，避免误触）；
    列表顺序即下拉的显示顺序。读入时由 ``normalize_quick_danmaku`` 清洗。
    """


def load_room_entries(path: Union[str, Path]) -> List[RoomEntry]:
    """读取房间列表；文件缺失或损坏时返回空列表。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("rooms") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    entries: List[RoomEntry] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            room_id = int(item["room_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if room_id in seen:
            continue
        seen.add(room_id)
        # 旧配置只有一个 auto_medal_tasks（两项合在一起）→ 迁移为两项都按原值
        legacy_auto = item.get("auto_medal_tasks")
        auto_default = bool(legacy_auto) if legacy_auto is not None else False
        entries.append(RoomEntry(
            room_id=room_id,
            note=str(item.get("note") or ""),
            enabled=bool(item.get("enabled", True)),
            uid=int(item.get("uid") or 0),
            notify_live=bool(item.get("notify_live", True)),
            auto_like=bool(item.get("auto_like", auto_default)),
            auto_danmaku=bool(item.get("auto_danmaku", auto_default)),
            auto_danmaku_when_live=bool(item.get("auto_danmaku_when_live", False)),
            quick_danmaku=normalize_quick_danmaku(item.get("quick_danmaku")),
        ))
    logger.debug("读取直播间列表 %s：%d 个房间（其中 %d 个启用）",
                 path, len(entries), sum(1 for e in entries if e.enabled))
    return entries


def save_room_entries(path: Union[str, Path], entries: Iterable[RoomEntry],
                      ui: Optional[dict] = None,
                      emoticon: Optional[Dict[int, Dict[str, object]]] = None) -> None:
    """保存房间列表（先写临时文件再替换，避免写一半被读取）。

    ui 为界面偏好（排序方式、直播中置顶开关等）；传入 None 时不写 ui 键。
    emoticon 为每个直播间上次浏览的表情包记忆；传入 None 时保留磁盘上已有的
    （该记忆由表情面板收起时单独写入，其他保存动作不应把它抹掉）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rooms": [asdict(e) for e in entries]}
    if ui is not None:
        payload["ui"] = ui
    memory = load_emoticon_memory(path) if emoticon is None else emoticon
    if memory:
        payload["emoticon"] = {str(room_id): dict(item)
                               for room_id, item in memory.items()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info("已保存直播间列表 %s：%d 个房间%s", path, len(payload["rooms"]),
                "（含界面偏好）" if ui is not None else "")


def load_emoticon_memory(path: Union[str, Path]) -> Dict[int, Dict[str, object]]:
    """读取每个直播间上次浏览的表情包记忆。

    结构为 ``{"emoticon": {"<房间号>": {"index": 序号, "name": 包名}}}``，返回
    ``{房间号: {"index": int, "name": str}}``（序号非负）；文件缺失、损坏或字段
    非法时按空处理，不抛异常。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("emoticon") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    memory: Dict[int, Dict[str, object]] = {}
    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        try:
            room_id = int(key)
            index = int(item.get("index") or 0)
        except (TypeError, ValueError):
            continue
        memory[room_id] = {"index": max(0, index), "name": str(item.get("name") or "")}
    return memory


def parse_room_window_rects(ui: object) -> Dict[str, Dict[str, int]]:
    """解析 ``ui.room_windows``（``{"<房间号>": {x, y, width, height}}``）。

    非法项（键不是房间号、宽高过小、字段类型不对）直接丢弃——几何记忆只是「便利」，
    不值得为一条坏数据让窗口打不开。
    """
    if not isinstance(ui, dict):
        return {}
    raw = ui.get("room_windows")
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, Dict[str, int]] = {}
    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        try:
            room_id = int(key)
            x = int(item.get("x", 0))
            y = int(item.get("y", 0))
            width = int(item.get("width", 0))
            height = int(item.get("height", 0))
        except (TypeError, ValueError):
            continue
        if width < 200 or height < 150:
            continue
        result[str(room_id)] = {"x": x, "y": y, "width": width, "height": height}
    return result


def parse_room_time_map(raw: object) -> Dict[int, float]:
    """解析「房间号 → 时刻（epoch 秒）」映射（纯函数，便于离线测试）。

    供 ``ui.live_started_at`` / ``ui.live_offline_at`` 使用——它们记录「按直播状态」排序
    所需的时间基准，**持久化后重启仍能按同样的先后排序**。配置文件是用户可编辑的，所以
    这里只做宽松解析：房间号须能转 ``int``、时刻须能转成正的 ``float``，其余（非 dict、
    非数字、0、负数）一律丢弃。**过旧记录的过滤不在这里**——那是业务规则，见
    ``gui_app.prune_live_marks``（读取后立即调用）。
    """
    if not isinstance(raw, dict):
        return {}
    result: Dict[int, float] = {}
    for key, value in raw.items():
        try:
            room_id = int(key)
            moment = float(value)
        except (TypeError, ValueError):
            continue
        if moment <= 0:
            continue
        result[room_id] = moment
    return result


def format_live_mark(moment: Optional[float]) -> str:
    """时间标记 → 日志里的本地时间串（如 ``2026-09-22 16:35:40``）；无效给 ``—``（纯函数）。

    放在本模块而不是 ``gui_app``：``client`` 也要用它记状态日志，而 client 不能导入
    gui_app（gui_app 反过来导入 client，会形成循环导入）。``gui_app.format_live_mark``
    仍然可用（那里把它重新导出了）。
    """
    if moment is None:
        return "—"
    try:
        return datetime.fromtimestamp(float(moment)).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


def load_ui_prefs(path: Union[str, Path]) -> Dict[str, object]:
    """读取界面偏好；文件缺失/损坏/缺少字段时返回默认值。"""
    prefs = dict(DEFAULT_UI_PREFS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prefs
    ui = data.get("ui") if isinstance(data, dict) else None
    if not isinstance(ui, dict):
        return prefs
    if ui.get("sort_mode") in SORT_MODES:
        prefs["sort_mode"] = ui["sort_mode"]
    if ui.get("pin_live"):
        # 旧版「直播中置顶」勾选框：ROADMAP 85 起并入「按直播状态」排序方案（一次性迁移）
        prefs["sort_mode"] = "status"
        logger.info("旧配置的「直播中置顶」已并入「按直播状态」排序方案")
    prefs["notify_overlay"] = bool(ui.get(
        "notify_overlay", ui.get("notify_system", True)))  # 兼容旧键名
    prefs["notify_persist"] = bool(ui.get("notify_persist", False))
    if ui.get("notify_sound") in NOTIFY_SOUNDS:
        prefs["notify_sound"] = ui["notify_sound"]
    prefs["dm_visible"] = bool(ui.get("dm_visible", False))
    prefs["dm_emoticon_image"] = bool(ui.get("dm_emoticon_image", True))
    size = ui.get("window_size")
    if (isinstance(size, list) and len(size) == 2
            and all(isinstance(value, int) and value > 200 for value in size)):
        prefs["window_size"] = [int(size[0]), int(size[1])]
    prefs["room_windows"] = parse_room_window_rects(ui)
    # 「按直播状态」排序的时间基准（ROADMAP 87）：重启后据此恢复上次的先后顺序
    prefs["live_started_at"] = parse_room_time_map(ui.get("live_started_at"))
    prefs["live_offline_at"] = parse_room_time_map(ui.get("live_offline_at"))
    return prefs
