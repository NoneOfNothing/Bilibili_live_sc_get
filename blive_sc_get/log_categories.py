"""日志区块（分类）定义与「按区块过滤」的实现。

独立于 ``app_config`` 与 ``log_setup``，避免循环导入（配置层要用区块名清单、
日志层要用配置对象，而这里两者都不依赖）：

- 区块常量与中文说明：``CATEGORY_*`` / ``CATEGORY_NAMES`` / ``CATEGORY_LABELS``；
- 开关：``CategorySwitches``（进程内可变，便于将来在界面上切换而无需重启）；
- 过滤：``CategoryFilter``（挂在**每个 handler** 上，对所有出口生效）；
- 取带类别标记的 logger：``get_logger``。

判定顺序：先看主开关 ``logging.enabled``（只决定是否**落盘**——关闭时日志照常进
控制台 / GUI 调试页），再看区块开关（关闭的区块在**所有出口**都不输出）。
未标记区块的记录（第三方库日志等）一律放行。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

CATEGORY_ROOM = "room"
CATEGORY_WINDOW = "window"
CATEGORY_DATA = "data"
CATEGORY_TASK = "task"
CATEGORY_LIVE = "live"
CATEGORY_APP = "app"

CATEGORY_NAMES: Tuple[str, ...] = (
    CATEGORY_ROOM, CATEGORY_WINDOW, CATEGORY_DATA,
    CATEGORY_TASK, CATEGORY_LIVE, CATEGORY_APP,
)
"""区块名（即 config.json 里 ``logging.categories`` 的键）。"""

CATEGORY_LABELS: Dict[str, str] = {
    CATEGORY_ROOM: "房间与选择（切换直播间、增删、启停、排序、拖动、备注）",
    CATEGORY_WINDOW: "界面布局（窗口尺寸、分隔条、页签、面板显隐）",
    CATEGORY_DATA: "数据读写（接口请求、历史读取、落盘、表情包与粉丝牌刷新）",
    CATEGORY_TASK: "任务与写操作（发弹幕/表情、粉丝牌任务、Cookie 获取、开播提醒）",
    CATEGORY_LIVE: "直播连接与状态（WS 连接、重连、开播下播、SC 与弹幕接收）",
    CATEGORY_APP: "应用生命周期（启动退出、配置加载、房间锁、异常）",
}
"""各区块的中文说明（写入配置模板的 ``_说明``，也用于启动日志与文档）。"""


class CategorySwitches:
    """区块日志开关（默认全部启用；进程内可变）。"""

    def __init__(self, switches: Optional[Dict[str, bool]] = None) -> None:
        self._switches: Dict[str, bool] = {name: True for name in CATEGORY_NAMES}
        self.update(switches or {})

    def update(self, switches: Optional[Dict[str, Any]]) -> None:
        """更新开关（只认已知区块 + 真正的布尔，其余忽略）。"""
        for name, value in (switches or {}).items():
            if name in self._switches and isinstance(value, bool):
                self._switches[name] = value

    def is_enabled(self, category: str) -> bool:
        """该区块是否启用（未知/空区块一律放行）。"""
        return self._switches.get(category, True)

    def as_dict(self) -> Dict[str, bool]:
        return dict(self._switches)

    def disabled(self) -> Tuple[str, ...]:
        return tuple(name for name, on in self._switches.items() if not on)

    def describe(self) -> str:
        """启动日志用的一句话描述（列出被关闭的区块）。"""
        off = self.disabled()
        if not off:
            return f"区块日志全部启用（{len(self._switches)} 个）"
        return "区块日志已关闭：" + "、".join(off)


class CategoryFilter(logging.Filter):
    """按区块开关过滤日志记录。

    挂在**每个 handler** 上（而不是 root logger）——logger 上的 filter 只作用于该
    logger 直接处理的记录，不拦子 logger 传播上来的记录，挂 handler 才能覆盖所有出口。
    """

    def __init__(self, switches: CategorySwitches) -> None:
        super().__init__()
        self._switches = switches

    def filter(self, record: logging.LogRecord) -> bool:
        category = getattr(record, "category", None)
        if not category:
            return True
        return self._switches.is_enabled(str(category))


def get_logger(category: str, name: str) -> logging.LoggerAdapter:
    """取带区块标记的 logger（类别由 handler 上的 ``CategoryFilter`` 读取）。

    用法::

        log_room = get_logger(CATEGORY_ROOM, "gui.room")
        log_room.info("切换到直播间 %s", room_id)

    每条记录都会带 ``category=room``；该区块被关闭时在控制台 / 调试页 / 文件里都不输出，
    其它区块照常。``category`` 建议用本模块的 ``CATEGORY_*`` 常量。
    """
    return logging.LoggerAdapter(logging.getLogger(name), {"category": category})


def category_label(category: str) -> str:
    """区块的中文说明（用于提示与文档）。"""
    return CATEGORY_LABELS.get(category, category)
