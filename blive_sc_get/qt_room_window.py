"""Qt 版「房间独立窗口」（ROADMAP 84）：只显示某个直播间的 SC 区 + 弹幕区。

用途：同时盯多个直播间而不用在主界面来回切换。窗口里两区的能力与主程序相同
（SC 历史 / 未读徽标 / 点击跳主页、弹幕显示 / 复制 / 回复 / 表情 / 发送），但**不含**
房间列表、粉丝牌、调试页与工具栏。

约定（与用户确认过）：

- **一房一窗**：宿主用 ``room_id -> 窗口`` 映射，重复打开只把已有窗口前置；
- **不抢焦点**：``WA_ShowWithoutActivating``（与预览浮窗同款做法），打开时主界面焦点不变；
- **不置顶**；
- **按房间记忆几何**（``gui_rooms.json`` 的 ``ui.room_windows``），重启只恢复几何、不自动开窗；
- 主窗口关闭时统一回收（宿主 ``closeEvent`` 调 :meth:`shutdown`）。

数据链路复用宿主：窗口里的两个面板都以主窗口为 host（api / hub / ui_queue / 表情缓存 /
配置），只把「当前房间」换成固定房间；事件由宿主按 room_id 分发（见 ``qt_app`` 的视图注册表）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QSplitter, QVBoxLayout, QWidget

from .log_categories import CATEGORY_WINDOW, get_logger
from .qt_dm_panel import DmPanel
from .qt_sc_panel import ScPanel

logger = logging.getLogger("gui_qt.roomwin")
log_window = get_logger(CATEGORY_WINDOW, "gui_qt.roomwin")

GEOMETRY_SAVE_DELAY_MS = 1000
"""移动 / 缩放后多久把几何写盘（去抖，避免拖拽时写盘风暴）。"""

DEFAULT_SIZE = (600, 800)
"""没有几何记忆时的默认窗口尺寸（逻辑像素）。"""

MIN_SIZE = (360, 300)
"""最小窗口尺寸：再小就 SC / 弹幕都看不清了。"""


class RoomChatWindow(QWidget):
    """某个直播间的独立窗口：上 SC 区、下弹幕区（可拖动分隔条调比例）。"""

    def __init__(self, host, room_id: int):
        super().__init__(None)
        self.host = host
        self._room_id = int(room_id)
        self._closing = False

        # 独立顶层窗口：显示时不抢主窗口焦点（预览浮窗同款做法），也不置顶
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumSize(*MIN_SIZE)
        self.setWindowTitle(self._title_text())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter, 1)
        # 两个面板都绑定固定房间（注入 provider；不再跟随宿主选中房间）
        self.sc_panel = ScPanel(host, lambda: self._room_id)
        self.dm_panel = DmPanel(host, lambda: self._room_id, always_visible=True)
        splitter.addWidget(self.sc_panel)
        splitter.addWidget(self.dm_panel)
        splitter.setSizes([1, 1])  # SC 与弹幕对半（可拖动调整）

        # 底部一行：与主界面底部同一个「弹幕表情图」开关（全局偏好，三处同步）。
        # 子窗口的弹幕区恒显示，故不提供「显示弹幕区」开关。
        options = QHBoxLayout()
        options.setSpacing(6)
        self.dm_emoticon_check = QCheckBox("弹幕表情图")
        self.dm_emoticon_check.setChecked(
            bool(host.ui_prefs.get("dm_emoticon_image", True)))
        self.dm_emoticon_check.setToolTip(
            "开启：表情弹幕在弹幕流里直接显示图片（更直观，但行更高、占更多高度）。\n"
            "关闭：只显示「[触发词]」文字（行更紧凑），鼠标悬浮仍可看原图。\n"
            "与主界面底部是同一个开关：任一处切换都会立即作用于主界面与所有独立窗口。")
        self.dm_emoticon_check.toggled.connect(self._on_emoticon_image_toggled)
        options.addWidget(self.dm_emoticon_check)
        options.addStretch(1)
        layout.addLayout(options)

        # 几何记忆：移动 / 缩放去抖后写盘
        self._geom_timer = QTimer(self)
        self._geom_timer.setSingleShot(True)
        self._geom_timer.timeout.connect(self._save_geometry)

        self.sc_panel.apply_room(self._room_id)
        host.register_room_window(self)
        log_window.info("打开房间独立窗口：房间 %s（%s）", self._room_id, self._title_text())

    # ---------- 弹幕表情图开关（与主界面同一偏好） ----------

    def _on_emoticon_image_toggled(self, checked: bool) -> None:
        """勾选框 → 宿主的统一入口（同时同步主界面、本窗口与其它窗口的勾选框）。"""
        self.host.set_dm_emoticon_image(bool(checked))

    def set_emoticon_check(self, enabled: bool) -> None:
        """宿主广播时回写本窗口勾选框（`blockSignals` 防与 toggled 回环）。"""
        check = getattr(self, "dm_emoticon_check", None)
        if check is None or check.isChecked() == bool(enabled):
            return
        check.blockSignals(True)
        check.setChecked(bool(enabled))
        check.blockSignals(False)

    # ---------- 房间 / 标题 ----------

    def room_id(self) -> int:
        return self._room_id

    def _title_text(self) -> str:
        anchor = self.host.anchor_names.get(self._room_id) or "未知主播"
        return f"房间 {self._room_id} · {anchor}"

    def update_title(self) -> None:
        """直播状态 / 主播名变化后刷新标题（宿主在 status 事件里调用）。"""
        self.setWindowTitle(self._title_text())

    # ---------- 几何记忆 ----------

    def restore_geometry(self) -> None:
        """按记忆的几何摆放（无记忆时用默认尺寸；越界由宿主收拢到屏幕内）。"""
        rect = self.host.load_room_window_geometry(self._room_id)
        if rect is not None:
            self.setGeometry(rect[0], rect[1], rect[2], rect[3])
            log_window.debug("恢复房间 %s 的窗口几何：%sx%s+%s+%s", self._room_id,
                             rect[2], rect[3], rect[0], rect[1])
            return
        # 无记忆：默认尺寸 + 相对主窗口偏移一点，避免与主界面完全重叠
        self.resize(*DEFAULT_SIZE)
        origin = self.host.pos()
        self.move(origin.x() + 80, origin.y() + 60)

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().moveEvent(event)
        if self.isVisible():
            self._geom_timer.start(GEOMETRY_SAVE_DELAY_MS)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if self.isVisible():
            self._geom_timer.start(GEOMETRY_SAVE_DELAY_MS)

    def _save_geometry(self) -> None:
        self.host.save_room_window_geometry(self._room_id, self.geometry())

    # ---------- 关闭 ----------

    def shutdown(self) -> None:
        """收窗：保存几何、释放弹窗资源、注销视图（可重复调用）。"""
        if self._closing:
            return
        self._closing = True
        self._save_geometry()
        self._geom_timer.stop()
        self.dm_panel.destroy_popups()
        self.host.unregister_sc_panel(self.sc_panel)
        self.host.unregister_dm_panel(self.dm_panel)
        self.host.unregister_room_window(self)
        # 该房间可能不再需要接收弹幕（除非主界面正选中它）
        self.host._apply_dm_gate()
        log_window.info("关闭房间独立窗口：房间 %s", self._room_id)
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 用户点标题栏「✕」与宿主统一回收都走同一条路（shutdown 自带重入保护）
        self.shutdown()
        super().closeEvent(event)
