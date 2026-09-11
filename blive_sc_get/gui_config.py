"""GUI 直播间列表的持久化：房间号、备注、启用状态。

独立于 tkinter，便于单元测试。文件为 ``<output_dir>/gui_rooms.json``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

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
        entries.append(RoomEntry(
            room_id=room_id,
            note=str(item.get("note") or ""),
            enabled=bool(item.get("enabled", True)),
            uid=int(item.get("uid") or 0),
            notify_live=bool(item.get("notify_live", True)),
        ))
    return entries


def save_room_entries(path: Union[str, Path], entries: Iterable[RoomEntry],
                      ui: Optional[dict] = None) -> None:
    """保存房间列表（先写临时文件再替换，避免写一半被读取）。

    ui 为界面偏好（排序方式、直播中置顶开关等）；传入 None 时不写 ui 键。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rooms": [asdict(e) for e in entries]}
    if ui is not None:
        payload["ui"] = ui
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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
