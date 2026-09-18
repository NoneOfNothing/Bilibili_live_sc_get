"""GUI 直播间列表的持久化：房间号、备注、启用状态。

独立于 tkinter，便于单元测试。文件为 ``<output_dir>/gui_rooms.json``。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from .log_categories import CATEGORY_DATA, get_logger

logger = get_logger(CATEGORY_DATA, __name__)

SORT_MODES = ("manual", "room", "anchor", "status")

# notify_overlay：开播时是否弹右下角自绘悬浮窗（全局主开关；每房间提醒列独立控制是否提醒）
# notify_persist：悬浮窗是否常驻（不自动关闭，需点击才消失；仅在 notify_overlay 开启时生效）
# notify_sound：开播提示音效（键名与 GUI 播放器映射表一致）
NOTIFY_SOUNDS = ("上行双音", "三连音", "Windows 系统提示音", "静音")
DEFAULT_NOTIFY_SOUND = "上行双音"

DEFAULT_UI_PREFS: Dict[str, object] = {
    "sort_mode": "manual",
    "pin_live": False,
    "notify_overlay": True,
    "notify_persist": False,
    "notify_sound": DEFAULT_NOTIFY_SOUND,
    "dm_visible": False,
}


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
    prefs["pin_live"] = bool(ui.get("pin_live", False))
    prefs["notify_overlay"] = bool(ui.get(
        "notify_overlay", ui.get("notify_system", True)))  # 兼容旧键名
    prefs["notify_persist"] = bool(ui.get("notify_persist", False))
    if ui.get("notify_sound") in NOTIFY_SOUNDS:
        prefs["notify_sound"] = ui["notify_sound"]
    prefs["dm_visible"] = bool(ui.get("dm_visible", False))
    return prefs
