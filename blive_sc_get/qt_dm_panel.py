"""Qt 版弹幕显示、发送与表情面板模块。

复用 Tk 版（gui_app）的纯函数与业务层；本模块只承载 UI 渲染与交互，
通过宿主 ``QtScMonitorApp`` 的 ``ui_queue``/``hub`` 与后台通信。
"""

from __future__ import annotations

import base64
import logging
import time
import webbrowser
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import (
    QCursor,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .api import ApiError, describe_send_error
from .gui_app import (
    AUTOSCROLL_DEADZONE,
    AUTOSCROLL_SPEED,
    AUTOSCROLL_TICK_MS,
    DM_COLOR_PRESETS,
    DM_EMOTICON_INFO_MAX,
    DM_MODE_TEXTS,
    DM_TEXT_MAX_LINES,
    EMOTICON_ICON_MAX_HEIGHT,
    EMOTICON_ICON_MAX_WIDTH,
    EMOTICON_IMAGE_CACHE_MAX,
    danmaku_content_from_line,
    danmaku_send_guard,
    dm_trim_index,
    emoticon_from_packages,
    emoticon_packages_signature,
    emoticon_tooltip_text,
    select_dm_options,
    unseen_badge_text,
)

logger = logging.getLogger("gui_qt.dm")

DANMAKU_MAX_LEN = 20
DM_SEND_COOLDOWN = 2.0
AT_BOTTOM_TOLERANCE = 4
"""滚动条距底部多少像素以内视为「吸底」（与 qt_app.SC_AT_BOTTOM_TOLERANCE 一致）。"""


class BottomHoldTextEdit(QTextEdit):
    """吸底 + 中键快速滚动的只读文本区（供 SC 区、弹幕区、调试日志共用）。

    对齐 Tk 版两处行为：

    - ``_on_text_configure``/``_restore_bottom``：拖动窗口或分隔条改变视口高度时，
      Qt 只保留滚动条数值，原本贴着底部的视图会不再显示最新内容；这里在 resize
      时判断「变化前是否位于底部」，是则重新贴底（用户向上翻阅时不动视口）。
      是否吸底由 100ms 轮询定期记录（``refresh_bottom_state``）。
    - ``_bind_autoscroll``/``_autoscroll_toggle``/``_autoscroll_tick``：按一下中键进入
      浏览器式自动滚动——鼠标偏离锚点越远滚得越快、移出窗口同样生效；再按一下中键
      或单击左键退出。

    宿主可设置 ``click_handler``（接收控件内坐标）处理单击（如 SC 用户名跳转）。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._was_at_bottom = True
        self.click_handler = None
        self._auto_anchor_y = 0
        self._auto_timer: Optional[QTimer] = None

    # ---- 吸底 ----

    def at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.maximum() - bar.value() <= AT_BOTTOM_TOLERANCE

    def refresh_bottom_state(self) -> None:
        """由轮询调用：记录当前是否吸底（供 resize 时判断）。"""
        self._was_at_bottom = self.at_bottom()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if self._was_at_bottom:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum())

    # ---- 中键自动滚动 ----

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MiddleButton:
            self.toggle_autoscroll(int(event.globalPosition().y()))
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self.cancel_autoscroll()
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton and self.click_handler is not None:
            pos = event.position().toPoint()
            if self.viewport().rect().contains(pos):
                self.click_handler(pos)

    def toggle_autoscroll(self, anchor_y: int) -> None:
        """进入/退出自动滚动模式（锚点为全局屏幕坐标，移出窗口仍生效）。"""
        if self._auto_timer is not None:
            self.cancel_autoscroll()
            return
        self._auto_anchor_y = int(anchor_y)
        self.viewport().setCursor(Qt.SizeVerCursor)
        timer = QTimer(self)
        timer.timeout.connect(self._autoscroll_tick)
        timer.start(AUTOSCROLL_TICK_MS)
        self._auto_timer = timer

    def autoscroll_active(self) -> bool:
        return self._auto_timer is not None

    def _autoscroll_tick(self) -> None:
        dy = QCursor.pos().y() - self._auto_anchor_y
        bar = self.verticalScrollBar()
        if dy > AUTOSCROLL_DEADZONE:
            bar.setValue(bar.value() + int((dy - AUTOSCROLL_DEADZONE) * AUTOSCROLL_SPEED))
        elif dy < -AUTOSCROLL_DEADZONE:
            bar.setValue(bar.value() + int((dy + AUTOSCROLL_DEADZONE) * AUTOSCROLL_SPEED))

    def cancel_autoscroll(self) -> None:
        if self._auto_timer is None:
            return
        self._auto_timer.stop()
        self._auto_timer.deleteLater()
        self._auto_timer = None
        self.viewport().setCursor(Qt.IBeamCursor)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.cancel_autoscroll()
        super().hideEvent(event)

_DM_TIME_COLOR = "#888888"
_DM_USER_COLOR = "#0055cc"
_DM_BODY_COLOR = "#1f1f1f"

# QTextCharFormat 自定义属性键：供点击弹幕精确定位（uid 跳转 / 正文复制 / 表情悬浮）
_DM_UID_KEY = 0x301     # 用户名段用户 uid
_DM_MID_KEY = 0x302     # 正文段弹幕 dmid
_DM_EMOJI_KEY = 0x303   # 正文段表情 unique（悬浮时显示提示/原图）
DM_META_MAX = 2000      # dmid 元数据缓存上限（超限丢最早一半）
EMOJI_TOOLTIP_DELAY_MS = 300   # 表情悬浮延迟（扫过时不弹，避免乱闪）
COPY_HINT_MS = 1800     # 「已复制」提示停留时长


def _remember_meta(cache: dict, dmid: str, uid: int,
                   uname: str, content: str) -> None:
    """缓存 dmid -> (uid, uname, text)，超限丢弃最早的一半。"""
    cache[dmid] = (int(uid or 0), str(uname or ""), str(content or ""))
    if len(cache) > DM_META_MAX:
        for key in list(cache)[:DM_META_MAX // 2]:
            cache.pop(key, None)


class DmPanel(QWidget):
    """弹幕显示 + 发送区 + 表情面板。"""

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.visible = bool(host.dm_var)
        self._dm_unseen = 0
        self._dm_reply_target: Optional[dict] = None
        self._dm_sending = False
        self._last_dm_send = 0.0
        # 可用颜色/模式（登录后按房间从服务端刷新；失败时用内置预设）
        self._dm_colors: List[Tuple[str, int]] = list(DM_COLOR_PRESETS)
        self._dm_modes: List[Tuple[str, int]] = list(DM_MODE_TEXTS.items())

        self._emoticon_visible = False
        self._emoticon_packages: list = []
        self._emoticon_page = 0
        self._emoticon_panel_room: Optional[int] = None
        self._emoticon_rendered_room: Optional[int] = None
        # 表情图片：url -> 已缩放的 QPixmap（懒加载 + 缓存；面板按钮与悬浮提示共用）
        self._emoticon_images: Dict[str, QPixmap] = {}
        self._emoticon_buttons: Dict[str, QPushButton] = {}
        self._emoticon_pending: set = set()

        # 弹幕交互状态（点击复制/@、右键回复、表情悬浮）
        self._dm_meta: Dict[str, Tuple[int, str, str]] = {}
        # emoticon_unique -> {"text","unique","id","room_id","url"}（有上限保护）
        self._dm_emoticon_info: Dict[str, dict] = {}
        self._emoji_hover_key: str = ""
        self._emoji_tip_timer: Optional[QTimer] = None
        self._pending_emoji_tip: Optional[dict] = None
        self._emoji_tip_info: Optional[dict] = None  # 当前正在显示的提示所属表情
        self._emoji_tip_label: Optional[QLabel] = None
        self._copy_hint_timer: Optional[QTimer] = None

        self._build_ui()
        self.setVisible(self.visible)

    @property
    def api(self):
        return self.host.hub.api

    @property
    def hub(self):
        return self.host.hub

    @property
    def ui_queue(self):
        return self.host.ui_queue

    @property
    def selected_room(self) -> Optional[int]:
        return self.host._selected_room_id

    # ---------- UI ----------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 弹幕显示区（显隐由宿主常驻开关控制，见 qt_app.dm_toggle）
        self.dm_text = BottomHoldTextEdit()
        self.dm_text.setReadOnly(True)
        # 弹幕区字号与 Tk 版一致（9pt；SC 区为 10pt），行距更紧凑
        dm_font = QFont(self.host.sc_text.font())
        dm_font.setPointSize(9)
        self.dm_text.setFont(dm_font)
        self.dm_text.setLineWrapMode(QTextEdit.WidgetWidth)
        self.dm_text.viewport().installEventFilter(self)
        self.dm_text.installEventFilter(self)
        outer.addWidget(self.dm_text, 1)
        self.dm_badge = self.host._make_unseen_badge(self.dm_text)
        self.dm_badge.clicked.connect(self._on_dm_badge_clicked)
        self.dm_badge.hide()

        self._emoji_tip_timer = QTimer(self)
        self._emoji_tip_timer.setSingleShot(True)
        self._emoji_tip_timer.timeout.connect(self._show_emoji_tooltip)

        # 发送区
        send = QHBoxLayout()
        self.dm_modes = QComboBox()
        for name, _mode in self._dm_modes:
            self.dm_modes.addItem(name)
        send.addWidget(self.dm_modes)
        self.dm_colors = QComboBox()
        for name, _code in self._dm_colors:
            self.dm_colors.addItem(name)
        send.addWidget(self.dm_colors)
        # 表情按钮：展开/收起该直播间的专属表情面板（点选即发送，写操作）
        self.dm_emoji_btn = QPushButton("表情")
        self.dm_emoji_btn.setFixedWidth(52)
        self.dm_emoji_btn.clicked.connect(self.toggle_emoticon_panel)
        send.addWidget(self.dm_emoji_btn)
        self.dm_send_entry = QLineEdit()
        # 与 Tk 版一致：不硬截断输入，超长仅标红提示（是否接受由服务端判定）
        self.dm_send_entry.textChanged.connect(self._update_dm_len_hint)
        self.dm_send_entry.returnPressed.connect(self.on_send_danmaku)
        send.addWidget(self.dm_send_entry, 1)
        self.dm_len_label = QLabel("")
        self.dm_len_label.setStyleSheet("color:#888; font-size: 11px")
        send.addWidget(self.dm_len_label)
        self.dm_send_btn = QPushButton("发送")
        self.dm_send_btn.clicked.connect(self.on_send_danmaku)
        send.addWidget(self.dm_send_btn)

        # 回复目标条
        self.reply_widget = QWidget()
        reply_bar = QHBoxLayout(self.reply_widget)
        reply_bar.setContentsMargins(0, 0, 0, 0)
        self.dm_reply_label = QLabel("")
        self.dm_reply_label.setStyleSheet("color:#0a5; font-size: 11px")
        reply_bar.addWidget(self.dm_reply_label)
        cancel_reply = QPushButton("取消回复")
        cancel_reply.setFlat(True)
        cancel_reply.clicked.connect(self._clear_dm_reply_target)
        reply_bar.addWidget(cancel_reply)
        reply_bar.addStretch(1)
        self.reply_widget.setVisible(False)
        outer.addWidget(self.reply_widget)

        # 表情面板
        self.emoticon_panel = self._build_emoticon_panel()
        self.emoticon_panel.setVisible(False)
        outer.addWidget(self.emoticon_panel)

        # Esc 收起表情面板（与 Tk 版一致；焦点在弹幕区/输入框内均生效）
        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self.hide_emoticon_panel)

        outer.addLayout(send)

        # 提示条：发送结果与「已复制」各自独立一行，避免互相覆盖（与 Tk 版一致）
        self.dm_hint = QLabel("")
        self.dm_hint.setStyleSheet("color:#888; font-size: 11px")
        outer.addWidget(self.dm_hint)
        self.dm_copy_hint = QLabel("")
        self.dm_copy_hint.setStyleSheet("color:#1a7f37; font-size: 11px")
        outer.addWidget(self.dm_copy_hint)

        self._admin_btn_row = send

    def _build_emoticon_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        v = QVBoxLayout(panel)
        v.setContentsMargins(6, 4, 6, 4)

        bar = QHBoxLayout()
        prev = QPushButton("◀")
        prev.setFixedWidth(32)
        prev.clicked.connect(self._on_emoticon_prev_page)
        bar.addWidget(prev)
        self.emoticon_pkg_box = QComboBox()
        self.emoticon_pkg_box.currentIndexChanged.connect(
            self._on_emoticon_pkg_selected)
        bar.addWidget(self.emoticon_pkg_box, 1)
        nxt = QPushButton("▶")
        nxt.setFixedWidth(32)
        nxt.clicked.connect(self._on_emoticon_next_page)
        bar.addWidget(nxt)
        self.emoticon_page_var = QLabel("")
        self.emoticon_page_var.setStyleSheet("color:#666; font-size: 11px")
        bar.addWidget(self.emoticon_page_var)
        hide = QPushButton("收起")
        hide.setFlat(True)
        hide.clicked.connect(self.toggle_emoticon_panel)
        bar.addWidget(hide)
        v.addLayout(bar)

        self.emoticon_scroll = QScrollArea()
        self.emoticon_scroll.setWidgetResizable(True)
        self.emoticon_scroll.setFixedHeight(88)
        self.emoticon_grid = QWidget()
        self.emoticon_grid_layout = QHBoxLayout(self.emoticon_grid)
        self.emoticon_grid_layout.setContentsMargins(2, 2, 2, 2)
        self.emoticon_grid_layout.setSpacing(6)
        self.emoticon_scroll.setWidget(self.emoticon_grid)
        # 滚轮横向翻动：滚动区、视口与网格都要装过滤器（滚轮事件先到最内层控件）
        self._install_wheel_filter(self.emoticon_scroll)
        self._install_wheel_filter(self.emoticon_scroll.viewport())
        self._install_wheel_filter(self.emoticon_grid)
        v.addWidget(self.emoticon_scroll)

        self.emoticon_hint = QLabel("")
        self.emoticon_hint.setStyleSheet("color:#888; font-size: 11px")
        v.addWidget(self.emoticon_hint)
        return panel

    # ---------- 弹幕渲染 ----------

    @property
    def _dm_scrolled_to_bottom(self) -> bool:
        bar = self.dm_text.verticalScrollBar()
        return bar.maximum() - bar.value() <= 4

    def append_dm_batch(self, batch: list) -> None:
        """渲染一批弹幕（新到消息，末尾追加）。每条为客户端 ``dm`` 负载 dict。"""
        if not batch:
            return
        follow = self._dm_scrolled_to_bottom
        cursor = self.dm_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        for info in batch:
            text = str(info.get("text", "") or "").replace("\n", " ")
            dmid = str(info.get("dmid") or "")
            ts_raw = info.get("time")
            uname = str(info.get("uname", "") or "")
            uid = int(info.get("uid") or 0)
            emoticon = info.get("emoticon") or {}
            try:
                ts = float(ts_raw) if ts_raw and not isinstance(ts_raw, str) \
                    else time.time()
                tstr = time.strftime("[%H:%M:%S] ", time.localtime(ts))
            except Exception:
                tstr = "[--:--:--] "
            if dmid:
                _remember_meta(self._dm_meta, dmid, uid, uname, text)

            # 时间
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_DM_TIME_COLOR))
            cursor.insertText(tstr, fmt)
            # 用户名段：带 uid 属性 → 点击跳个人空间
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_DM_USER_COLOR))
            if uid:
                fmt.setProperty(_DM_UID_KEY, uid)
            cursor.insertText(f"{uname}：", fmt)
            # 正文段：带 dmid → 点击复制 / 右键回复；带表情 unique → 悬浮提示
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_DM_BODY_COLOR))
            if dmid:
                fmt.setProperty(_DM_MID_KEY, dmid)
            unique = str(emoticon.get("unique") or "")
            if unique:
                # 表情弹幕：打标记记录，供悬浮时看原图（弹幕流内不内嵌图片）
                fmt.setProperty(_DM_EMOJI_KEY, unique)
                self._remember_dm_emoticon(unique, info, text)
            cursor.insertText(text if text else " ", fmt)
            cursor.insertText("\n")
        if follow:
            bar = self.dm_text.verticalScrollBar()
            bar.setValue(bar.maximum())
        else:
            self._dm_unseen += len(batch)
        self._trim_dm_text()

    # ---------- 弹幕交互：点击跳转/复制 + 右键回复/@ + 表情悬浮 ----------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        if obj is self.dm_text or obj is self.dm_text.viewport():
            if event.type() == QEvent.MouseButtonPress:
                self._on_dm_press(event)
            elif event.type() == QEvent.ContextMenu:
                return self._on_dm_context_menu(event)
            elif event.type() == QEvent.MouseMove:
                self._on_dm_motion(event)
            elif event.type() == QEvent.Leave:
                self._hide_emoji_tooltip()
        elif event.type() == QEvent.Wheel and self._is_emoticon_strip_widget(obj):
            # 表情条是单行横向布局：滚轮改为横向翻动（与 Tk 版 _on_emoticon_wheel 一致）
            bar = self.emoticon_scroll.horizontalScrollBar()
            bar.setValue(bar.value() - int(event.angleDelta().y()))
            return True
        return super().eventFilter(obj, event)

    def _is_emoticon_strip_widget(self, obj) -> bool:
        if obj is self.emoticon_scroll or obj is self.emoticon_grid:
            return True
        viewport = self.emoticon_scroll.viewport()
        if obj is viewport:
            return True
        return isinstance(obj, QPushButton) and obj.parent() is self.emoticon_grid

    def _install_wheel_filter(self, widget) -> None:
        widget.installEventFilter(self)

    def _clicked_fmt(self, event) -> Optional[QTextCharFormat]:
        """取光标所在字符的格式；点在空白/行尾换行处返回 None，避免误触。"""
        pos = event.position().toPoint()
        cursor = self.dm_text.cursorForPosition(pos)
        char = self.dm_text.document().characterAt(cursor.position())
        if char in (" ", "\n", "\t", ""):
            return None
        return cursor.charFormat()

    def _on_dm_press(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        self._hide_emoji_tooltip()
        fmt = self._clicked_fmt(event)
        if fmt is None:
            return
        mid = str(fmt.property(_DM_MID_KEY) or "")
        uid = int(fmt.property(_DM_UID_KEY) or 0)
        if mid:
            meta = self._dm_meta.get(mid)
            content = str(meta[2]) if meta else ""
            if not content:
                cursor = self.dm_text.cursorForPosition(event.position().toPoint())
                content = danmaku_content_from_line(cursor.block().text())
            if content:
                self._copy_dm_content(content)
        elif uid:
            webbrowser.open(f"https://space.bilibili.com/{uid}")

    def _copy_dm_content(self, content: str) -> None:
        """复制弹幕正文到系统剪贴板，并短暂显示「已复制」提示。"""
        self.host.copy_to_clipboard(content)
        snippet = content if len(content) <= 20 else content[:20] + "…"
        self.dm_copy_hint.setText(f"已复制：{snippet}")
        if self._copy_hint_timer is None:
            self._copy_hint_timer = QTimer(self)
            self._copy_hint_timer.setSingleShot(True)
            self._copy_hint_timer.timeout.connect(self._clear_copy_hint)
        self._copy_hint_timer.stop()
        self._copy_hint_timer.start(COPY_HINT_MS)

    def _clear_copy_hint(self) -> None:
        self.dm_copy_hint.setText("")

    def _on_dm_context_menu(self, event) -> bool:
        """右键弹幕：回复该弹幕（需 dmid）/ @该用户（需 uid）。"""
        self._hide_emoji_tooltip()
        fmt = self._clicked_fmt(event)
        mid = str(fmt.property(_DM_MID_KEY) or "") if fmt else ""
        uid = int(fmt.property(_DM_UID_KEY) or 0) if fmt else 0
        meta = self._dm_meta.get(mid)
        if meta:
            uid = uid or int(meta[0] or 0)
            uname = str(meta[1] or "")
            content = str(meta[2] or "")
        else:
            uname, content = "", ""
        menu = QMenu(self)
        act_reply = menu.addAction("回复该弹幕")
        act_reply.setEnabled(bool(mid))
        act_at = menu.addAction("@ 该用户")
        act_at.setEnabled(bool(uid))
        chosen = menu.exec(event.globalPos())
        if chosen is act_reply:
            self._set_dm_reply_target(uid, uname, content)
        elif chosen is act_at:
            self._set_reply_target_at(uid, uname)
        return True

    def _remember_dm_emoticon(self, unique: str, dm: dict, shown_text: str) -> None:
        """记录弹幕里出现过的表情（供悬浮提示显示触发词/ID/原图）。

        弹幕报文可能不带图片地址：先用已加载的表情包兜底补 url 与数字 id，
        都取不到时在悬浮那一刻再补一次（见 _ensure_dm_emoticon）。
        """
        emoticon = dm.get("emoticon") or {}
        room_id = dm.get("room_id")
        package = emoticon_from_packages(self.host.emoticons_for(room_id), unique)
        self._dm_emoticon_info[unique] = {
            "text": shown_text,
            "unique": unique,
            "room_id": room_id,
            "id": package.get("id") or 0,
            "url": str(emoticon.get("url") or "") or str(package.get("url") or ""),
        }
        # 上限保护：超出后丢弃最早的记录（最旧的那几条已滚出视野）
        while len(self._dm_emoticon_info) > DM_EMOTICON_INFO_MAX:
            oldest = next(iter(self._dm_emoticon_info))
            self._dm_emoticon_info.pop(oldest, None)

    def _ensure_dm_emoticon(self, info: dict) -> str:
        """取表情图地址；缺失时用已加载的表情包补，包未加载则后台拉一次。"""
        url = str(info.get("url") or "")
        if url:
            return url
        unique = str(info.get("unique") or "")
        room_id = info.get("room_id")
        packages = self.host.emoticons_for(room_id)
        if packages is None:
            if isinstance(room_id, int):
                self.host.request_room_packages(room_id)
            return ""
        package = emoticon_from_packages(packages, unique)
        url = str(package.get("url") or "")
        if url:
            info["url"] = url
            if not info.get("id"):
                info["id"] = package.get("id") or 0
        return url

    def _request_dm_emoticon_image(self, url: str) -> None:
        """按需下载表情图（悬浮提示用；复用宿主的限流下载管线与 url 缓存）。"""
        if not url or url in self._emoticon_images or url in self._emoticon_pending:
            return
        self._emoticon_pending.add(url)
        self.host.request_emoticon_images([url])

    def _on_dm_motion(self, event) -> None:
        pos = event.position().toPoint()
        cursor = self.dm_text.cursorForPosition(pos)
        fmt = cursor.charFormat()
        key = str(fmt.property(_DM_EMOJI_KEY) or "")
        if key == self._emoji_hover_key:
            return  # 仍停留在同一表情：不重排也不闪
        self._emoji_hover_key = key
        self._hide_emoji_tooltip()
        if not key:
            return
        info = self._dm_emoticon_info.get(key)
        if info is None:
            return
        # 悬浮要看原图：缺地址先兜底，图片未缓存就先懒加载（就绪后提示窗自动补上）
        url = self._ensure_dm_emoticon(info)
        if url:
            self._request_dm_emoticon_image(url)
        self._pending_emoji_tip = info
        self._emoji_tip_timer.start(EMOJI_TOOLTIP_DELAY_MS)

    def _show_emoji_tooltip(self) -> None:
        info = self._pending_emoji_tip
        self._pending_emoji_tip = None
        if not info:
            return
        url = self._ensure_dm_emoticon(info)
        pixmap = self._emoticon_images.get(url) if url else None
        if url and pixmap is None:
            self._request_dm_emoticon_image(url)
        self._show_emoji_tip(info, pixmap)

    def _show_emoji_tip(self, info: dict, pixmap: Optional[QPixmap]) -> None:
        """显示提示：有原图就只显示图（触发词与弹幕内容重复），否则显示配置文案。"""
        label = self._emoji_tip_label
        if label is None:
            label = QLabel(None, Qt.Tool | Qt.FramelessWindowHint
                           | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
            label.setStyleSheet(
                "background:#333333; color:#ffffff; padding:4px 8px;"
                " border:1px solid #666666; font-size:11px;")
            label.hide()
            self._emoji_tip_label = label
        text = ""
        if pixmap is None:
            # 无原图时用配置字段拼文案（触发词属于 "text" 字段，勿另行重复拼接）
            try:
                text = emoticon_tooltip_text(
                    info, self.host.app_config.emoticon_tooltip) or ""
            except Exception:
                text = ""
            if not text:
                self._hide_emoji_tooltip()
                return
        self._emoji_tip_info = info
        if pixmap is not None:
            label.setText("")
            label.setPixmap(pixmap)
        else:
            label.setPixmap(QPixmap())  # 清掉旧图，避免残留上一次的原图
            label.setText(text)
        label.adjustSize()
        label.move(self._tooltip_pos(label))
        label.show()
        label.raise_()

    @staticmethod
    def _desktop_rect() -> Optional[Tuple[int, int, int, int]]:
        """整个虚拟桌面（多显示器合并）的 (left, top, right, bottom)。"""
        screens = QGuiApplication.screens()
        if not screens:
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return None
            geo = screen.availableGeometry()
            return geo.left(), geo.top(), geo.right(), geo.bottom()
        areas = [s.availableGeometry() for s in screens]
        return (min(a.left() for a in areas), min(a.top() for a in areas),
                max(a.right() for a in areas), max(a.bottom() for a in areas))

    def _tooltip_pos(self, label: QLabel) -> QPoint:
        """提示窗位置：光标右下方，越界时向内回缩（多显示器按虚拟桌面算）。"""
        cursor = QCursor.pos()
        x, y = cursor.x() + 16, cursor.y() + 20
        rect = self._desktop_rect()
        if rect is None:
            return QPoint(x, y)
        left, top, right, bottom = rect
        margin = 8
        return QPoint(min(max(x, left), max(left, right - label.width() - margin)),
                      min(max(y, top), max(top, bottom - label.height() - margin)))

    def _cancel_emoji_tooltip(self) -> None:
        self._emoji_tip_timer.stop()
        self._pending_emoji_tip = None

    def _hide_emoji_tooltip(self) -> None:
        self._cancel_emoji_tooltip()
        self._emoji_tip_info = None
        if self._emoji_tip_label is not None:
            self._emoji_tip_label.hide()

    def _trim_dm_text(self) -> None:
        """裁剪超过行数上限的最早弹幕（从顶部删）。

        ``dm_trim_index`` 返回的是 Tk 风格的行号字符串（如 ``"2000.0"``），
        这里必须转成 int 才能交给 ``findBlockByNumber``（否则 TypeError）。
        """
        doc = self.dm_text.document()
        total = doc.blockCount()
        keep_start = dm_trim_index(total, DM_TEXT_MAX_LINES)
        if keep_start is None:
            return
        try:
            keep_line = int(str(keep_start).split(".")[0])
        except (TypeError, ValueError):
            return
        # 删除到 keep_line 行首为止（保留后半）
        block_to = doc.findBlockByNumber(keep_line)
        if not block_to.isValid():
            return
        cursor = QTextCursor(doc)
        cursor.setPosition(block_to.position(), QTextCursor.KeepAnchor)
        cursor.removeSelectedText()

    # ---------- 清空 / 生命周期 ----------

    def clear_view(self) -> None:
        """清空弹幕显示区（切换直播间时调用，避免不同房间的弹幕混在一起）。"""
        self.dm_text.clear()
        self._dm_unseen = 0
        self._hide_emoji_tooltip()
        self._sync_unseen_badge()

    def destroy_popups(self) -> None:
        """退出时记住当前表情包并销毁自建的无父窗（避免残留窗口）。"""
        try:
            self._remember_page()
        except Exception:
            logger.debug("退出前记录表情包失败", exc_info=True)
        self._hide_emoji_tooltip()
        label = self._emoji_tip_label
        self._emoji_tip_label = None
        if label is not None:
            label.close()
            label.deleteLater()

    def on_login_changed(self) -> None:
        """登录态变化：可用表情随身份变化，缓存与刷新冷却一并失效。"""
        self._emoticon_packages = []
        self._emoticon_page = 0
        self._emoticon_panel_room = None
        self._emoticon_rendered_room = None
        self._emoticon_visible = False
        self.emoticon_panel.setVisible(False)
        self._refresh_dm_send_state()

    # ---------- 新建弹幕未读徽标 ----------

    def _sync_unseen_badge(self) -> None:
        """每轮轮询：滚动回底部则清零未读并隐藏徽标；顺带记录吸底状态。"""
        self.dm_text.refresh_bottom_state()
        if self._dm_unseen and self._dm_scrolled_to_bottom:
            self._dm_unseen = 0
        self.host._place_unseen_badge(
            self.dm_badge, self.dm_text, unseen_badge_text(self._dm_unseen, "弹幕"))

    def _on_dm_badge_clicked(self) -> None:
        self._dm_unseen = 0
        bar = self.dm_text.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._sync_unseen_badge()

    # ---------- 发送 ----------

    def _set_dm_reply_target(self, uid: int, uname: str, text: str = "") -> None:
        self._dm_reply_target = {"uid": uid, "uname": uname, "text": text or ""}
        self.dm_reply_label.setText(
            f"回复 {uname}（上次：{(text or '')[:20]}）" if text
            else f"回复 {uname}")
        self.reply_widget.setVisible(True)

    def _set_reply_target_at(self, uid: int, uname: str) -> None:
        self._dm_reply_target = {"uid": uid, "uname": uname, "text": ""}
        self.dm_reply_label.setText(f"@ {uname}")
        self.reply_widget.setVisible(True)

    def _clear_dm_reply_target(self) -> None:
        self._dm_reply_target = None
        self.reply_widget.setVisible(False)

    def _dm_send_block_reason(self) -> str:
        if not self.host.app_config.allow_write_operations:
            return "已在 config.json 关闭写操作（allow_write_operations=false）"
        if self.api is None:
            return "后台初始化中，请稍候…"
        if not self.api.logged_in:
            return "未登录：请用「获取Cookie」获取已登录的 B 站 Cookie"
        if not self.api.csrf:
            return "Cookie 缺少 bili_jct，请重新「获取Cookie」"
        if not self.host.dm_var:
            return "请先开启「弹幕」开关"
        if self.selected_room is None:
            return "请先选择一个直播间"
        return ""

    def _refresh_dm_send_state(self) -> None:
        reason = self._dm_send_block_reason()
        enabled = not reason and not self._dm_sending
        self.dm_send_btn.setEnabled(enabled)
        self.dm_send_entry.setEnabled(enabled)
        self.dm_emoji_btn.setEnabled(enabled)
        self.dm_colors.setEnabled(enabled)
        self.dm_modes.setEnabled(enabled)
        self._update_dm_len_hint()
        if reason:
            self.dm_hint.setText(reason)

    def _update_dm_len_hint(self) -> None:
        """字数计数（仅提示，不做客户端截断）：超长标红提醒服务端可能拒绝。"""
        text = self.dm_send_entry.text()
        over = len(text) > DANMAKU_MAX_LEN
        color = "#c62828" if over else "#888"
        self.dm_len_label.setStyleSheet(f"color:{color}; font-size:11px")
        self.dm_len_label.setText(f"{len(text)}/{DANMAKU_MAX_LEN}")

    # ---- 可用颜色 / 模式（服务端按房间下发，失败时保留内置预设） ----

    def _selected_color(self) -> int:
        return dict(self._dm_colors).get(self.dm_colors.currentText(),
                                         self._dm_colors[0][1])

    def _selected_mode(self) -> int:
        return dict(self._dm_modes).get(self.dm_modes.currentText(),
                                        self._dm_modes[0][1])

    def on_dm_config(self, payload: dict) -> None:
        """把服务端返回的颜色/模式可用项刷新到下拉框（与 Tk 版 _on_dm_config 一致）。"""
        if payload.get("room_id") != self.selected_room:
            return  # 已切换房间，丢弃过期结果
        config = payload.get("config") or {}
        colors: List[Tuple[str, int]] = []
        for group in config.get("group") or []:
            if not isinstance(group, dict):
                continue
            for item in group.get("color") or []:
                if not isinstance(item, dict) or item.get("status") != 1:
                    continue
                value = item.get("color")
                if value is None:
                    continue
                try:
                    colors.append((str(item.get("name") or item.get("color_hex") or value),
                                   int(value)))
                except (TypeError, ValueError):
                    continue
        mode_names = {value: name for name, value in DM_MODE_TEXTS.items()}
        modes: List[Tuple[str, int]] = []
        for item in config.get("mode") or []:
            if not isinstance(item, dict) or item.get("status") != 1:
                continue
            value = item.get("mode")
            if value is None:
                continue
            try:
                mode_value = int(value)
            except (TypeError, ValueError):
                continue
            name = item.get("name") or mode_names.get(mode_value, f"模式{mode_value}")
            modes.append((str(name), mode_value))
        self._set_dm_options(colors, modes)

    def _set_dm_options(self, colors: List[Tuple[str, int]],
                        modes: List[Tuple[str, int]]) -> None:
        """用服务端可用项刷新下拉（保留原选择，失效则回退首项）。"""
        new_colors = select_dm_options(DM_COLOR_PRESETS, colors)
        self._dm_colors = list(new_colors)
        current_color = self.dm_colors.currentText()
        self.dm_colors.blockSignals(True)
        self.dm_colors.clear()
        for name, _value in new_colors:
            self.dm_colors.addItem(name)
        index = self.dm_colors.findText(current_color)
        self.dm_colors.setCurrentIndex(index if index >= 0 else 0)
        self.dm_colors.blockSignals(False)
        new_modes = select_dm_options(tuple(DM_MODE_TEXTS.items()), modes)
        self._dm_modes = list(new_modes)
        current_mode = self.dm_modes.currentText()
        self.dm_modes.blockSignals(True)
        self.dm_modes.clear()
        for name, _value in new_modes:
            self.dm_modes.addItem(name)
        index = self.dm_modes.findText(current_mode)
        self.dm_modes.setCurrentIndex(index if index >= 0 else 0)
        self.dm_modes.blockSignals(False)

    # ---- 发送 ----

    def on_send_danmaku(self) -> None:
        """主线程发送入口：门控 → 本地校验 → 提交后台协程。"""
        if self._dm_sending:
            return
        block = self._dm_send_block_reason()
        if block:
            self.dm_hint.setText(block)
            return
        text = self.dm_send_entry.text().strip()
        reason = danmaku_send_guard(
            text, last_time=self._last_dm_send, now=time.time(),
            cooldown=DM_SEND_COOLDOWN)
        if reason:
            self.dm_hint.setText(str(reason))
            return
        room_id = self.selected_room
        target = self._dm_reply_target or {}
        self._dm_sending = True
        self._refresh_dm_send_state()
        self.dm_hint.setText("发送中…")
        self.hub.submit(self._async_send_danmaku(
            room_id=room_id, text=text,
            color=self._selected_color(), mode=self._selected_mode(),
            reply_mid=int(target.get("uid") or 0),
            reply_uname=str(target.get("uname") or ""),
            replay_dmid=str(target.get("dmid") or "")))

    async def _async_send_danmaku(self, *, room_id, text: str, color: int, mode: int,
                                  reply_mid: int, reply_uname: str,
                                  replay_dmid: str = "",
                                  emoticon: Optional[dict] = None) -> None:
        """在 asyncio 线程内发送弹幕/表情包，结果经 ui_queue 回主线程。"""
        api = self.api
        if api is None:
            self.ui_queue.put(("dm_send_result",
                               {"ok": False, "error": "后台未就绪，发送取消"}))
            return
        info = {"room_id": room_id, "text": text, "emoticon": bool(emoticon)}
        try:
            await api.send_danmaku(
                self.host.real_room_id(room_id), text, color=color, mode=mode,
                reply_mid=reply_mid, reply_uname=reply_uname,
                replay_dmid=replay_dmid, emoticon=emoticon)
        except ApiError as exc:
            self.ui_queue.put(("dm_send_result", dict(
                info, ok=False, error=describe_send_error(exc.code, exc.message))))
        except Exception as exc:  # 兜底：避免后台任务静默失败
            self.ui_queue.put(("dm_send_result", dict(
                info, ok=False, error=f"发送异常：{exc}")))
        else:
            self.ui_queue.put(("dm_send_result", dict(info, ok=True)))

    def on_dm_send_result(self, payload: dict) -> None:
        self._dm_sending = False
        self._last_dm_send = time.time()
        if payload.get("ok"):
            if not payload.get("emoticon"):
                # 表情包发送不影响输入框内容，仅文字弹幕成功后清空
                self.dm_send_entry.clear()
                self._update_dm_len_hint()
                self._clear_dm_reply_target()
            self.dm_hint.setText("已发送")
            logger.info("已发送%s：%s", "表情包" if payload.get("emoticon") else "弹幕",
                        payload.get("text") or "")
        else:
            error = payload.get("error") or "发送失败"
            self.dm_hint.setText(error)
            logger.warning("发送弹幕失败：%s", error)
        self._refresh_dm_send_state()

    # ---------- 表情面板 ----------

    def toggle_emoticon_panel(self) -> None:
        """展开/收起表情面板（懒加载：首次展开时才向后端请求，与 Tk 版一致）。"""
        if self._emoticon_visible:
            self.hide_emoticon_panel()
            return
        reason = self._dm_send_block_reason()
        if reason:  # 与 Tk 版一致：不具备发送条件时不展开
            self.dm_hint.setText(reason)
            return
        room_id = self.selected_room
        if room_id is None:
            return
        self._emoticon_visible = True
        self._emoticon_panel_room = room_id
        self.emoticon_panel.setVisible(True)
        self._refresh_emoticons()

    def hide_emoticon_panel(self) -> None:
        """收起表情面板（记住当前包；面板本就隐藏时也清掉悬挂的提示）。"""
        self._hide_emoji_tooltip()
        if not self._emoticon_visible:
            return
        self._emoticon_visible = False
        self._remember_page()
        self.emoticon_panel.setVisible(False)

    def _remember_page(self) -> None:
        """记住当前房间停留的表情包（写入 gui_rooms.json 的 emoticon 段）。"""
        room_id = self._emoticon_panel_room
        if room_id is None or not self._emoticon_packages:
            return
        index = max(0, min(self._emoticon_page, len(self._emoticon_packages) - 1))
        name = str(self._emoticon_packages[index].get("name") or "")
        self.host.remember_emoticon_page(room_id, index, name)

    def _refresh_emoticons(self) -> None:
        """刷新当前房间可用表情包（宿主统一拉取：面板与弹幕表情兜底共用缓存）。"""
        room_id = self.selected_room
        self._emoticon_panel_room = room_id
        if room_id is None or self.api is None:
            return
        self.host.request_room_packages(room_id)

    def on_emoticons(self, payload: dict) -> None:
        """表情包到达：仅当是本面板正在展示的房间时才重绘（否则只留在宿主缓存里）。"""
        room_id = payload.get("room_id")
        if room_id != self._emoticon_panel_room:
            return
        packages = payload.get("packages") or []
        if not packages:
            return  # 刷新失败/为空：保留上次结果
        new_sig = emoticon_packages_signature(packages)
        old_sig = emoticon_packages_signature(self._emoticon_packages)
        first_show = room_id != self._emoticon_rendered_room
        self._emoticon_packages = packages
        if new_sig != old_sig:
            self._emoticon_page = 0
        if packages and first_show:
            # 首次展示该房间：回到上次收起时停留的表情包（优先按包名找回）
            self._emoticon_page = self._memory_page(room_id, packages)
        self._emoticon_rendered_room = room_id
        self._render_emoticons()

    def _memory_page(self, room_id: int, packages: list) -> int:
        """按记忆找表情包页号：先按包名，再退回序号（越界自动收敛）。"""
        memory = self.host._emoticon_memory.get(room_id) or {}
        name = str(memory.get("name") or "")
        if name:
            for index, package in enumerate(packages):
                if str((package or {}).get("name") or "") == name:
                    return index
        try:
            return max(0, min(int(memory.get("index") or 0), len(packages) - 1))
        except (TypeError, ValueError):
            return 0

    def _render_emoticons(self) -> None:
        packages = self._emoticon_packages
        self.emoticon_pkg_box.blockSignals(True)
        self.emoticon_pkg_box.clear()
        for i, p in enumerate(packages):
            self.emoticon_pkg_box.addItem(f"{i + 1}. {p.get('name') or '表情'}", i)
        self.emoticon_pkg_box.blockSignals(False)
        self._show_emoticon_page(self._emoticon_page)

    def _show_emoticon_page(self, index: int) -> None:
        """渲染一个表情包（一页 = 一个包）：有图显图，缺图则按需后台下载。"""
        packages = self._emoticon_packages
        if not packages:
            self.emoticon_page_var.setText("（无表情包）")
            return
        index = max(0, min(index, len(packages) - 1))
        self._emoticon_page = index
        self._remember_page()  # 翻到哪一页就持久化哪一页（收起/直接退出都能记住）
        while self.emoticon_grid_layout.count():
            item = self.emoticon_grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._emoticon_buttons = {}
        pkg = packages[index]
        self.emoticon_page_var.setText(
            f"{index + 1}/{len(packages)} · {pkg.get('name') or '表情'}")
        missing: List[str] = []
        for emo in pkg.get("emoticons") or []:
            trigger = str(emo.get("trigger") or emo.get("text") or "")
            url = str(emo.get("url") or "")
            btn = QPushButton(trigger)
            btn.setFlat(True)
            btn.setToolTip(emoticon_tooltip_text(
                {"text": trigger, "trigger": trigger,
                 "unique": emo.get("unique"), "id": emo.get("id")},
                self.host.app_config.emoticon_tooltip))
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn.clicked.connect(lambda _=False, e=emo: self._send_emoticon(e))
            self._install_wheel_filter(btn)  # 在按钮上滚轮也能横向翻动表情条
            pixmap = self._emoticon_images.get(url) if url else None
            if pixmap is not None:
                self._apply_emoticon_pixmap(btn, pixmap)
            elif url and url not in self._emoticon_pending:
                missing.append(url)
            self.emoticon_grid_layout.addWidget(btn)
            if url:
                self._emoticon_buttons[url] = btn
        self.emoticon_grid_layout.addStretch(1)
        self._sync_strip_width()
        if missing:
            self._emoticon_pending.update(missing)
            self.host.request_emoticon_images(missing)

    def _emoticon_strip_width(self) -> int:
        """单行表情条的自然宽度（各按钮宽度 + 网格间距 + 边距）。"""
        layout = self.emoticon_grid_layout
        margins = layout.contentsMargins()
        total = margins.left() + margins.right()
        count = 0
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is None:
                continue
            total += widget.sizeHint().width()
            count += 1
        if count > 1:
            total += layout.spacing() * (count - 1)
        return total

    def _sync_strip_width(self) -> None:
        """把表情条最小宽度设为自然宽度。

        ``QScrollArea`` 开启 ``widgetResizable`` 后会把内部控件缩到视口大小——
        单行布局因此会被**压扁**而不是出现横向滚动条。设置最小宽度后，内容窄时
        由末尾的 stretch 铺满、宽时交给横向滚动条（对应 Tk 版 _sync_emoticon_strip_width）。
        """
        self.emoticon_grid.setMinimumWidth(self._emoticon_strip_width())

    @staticmethod
    def _apply_emoticon_pixmap(button: QPushButton, pixmap: QPixmap) -> None:
        """把表情图设为按钮图标（有图时不再显示触发词文字，与 Tk 版一致）。"""
        width = max(pixmap.width(), 1)
        height = max(pixmap.height(), 1)
        button.setIcon(QIcon(pixmap))
        button.setIconSize(QSize(width, height))
        button.setText("")
        button.setFixedSize(width + 12, height + 12)

    def on_emoticon_image(self, payload: dict) -> None:
        """后台下载完成：解码为 QPixmap，更新按钮与缓存。"""
        url = str(payload.get("url") or "")
        data = str(payload.get("data") or "")
        self._emoticon_pending.discard(url)
        if not url or not data:
            return
        try:
            raw = base64.b64decode(data)
        except Exception:
            logger.debug("表情图片 base64 解码失败: %s", url)
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            # Qt 未内置该格式的解码器（极少见）：保留触发词文字按钮
            logger.debug("表情图片无法解码: %s", url)
            return
        if (pixmap.width() > EMOTICON_ICON_MAX_WIDTH
                or pixmap.height() > EMOTICON_ICON_MAX_HEIGHT):
            pixmap = pixmap.scaled(EMOTICON_ICON_MAX_WIDTH, EMOTICON_ICON_MAX_HEIGHT,
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if len(self._emoticon_images) >= EMOTICON_IMAGE_CACHE_MAX:
            self._emoticon_images.clear()
        self._emoticon_images[url] = pixmap
        btn = self._emoticon_buttons.get(url)
        if btn is not None:
            self._apply_emoticon_pixmap(btn, pixmap)
            self._sync_strip_width()  # 图标变大后重算自然宽度（横向滚动范围）
        # 悬浮提示正展示这个表情且此前没有图：立即补上原图（懒加载的就绪回填）
        info = self._emoji_tip_info
        label = self._emoji_tip_label
        if (info is not None and label is not None and not label.isHidden()
                and self._ensure_dm_emoticon(info) == url):
            self._show_emoji_tip(info, pixmap)

    def _on_emoticon_prev_page(self) -> None:
        if self._emoticon_packages:
            self._show_emoticon_page(self._emoticon_page - 1)

    def _on_emoticon_next_page(self) -> None:
        if self._emoticon_packages:
            self._show_emoticon_page(self._emoticon_page + 1)

    def _on_emoticon_pkg_selected(self, index: int) -> None:
        self._show_emoticon_page(index)

    def _send_emoticon(self, emo: dict) -> None:
        """点击表情即发送（与网页端一致）：沿用写操作总开关、登录态与冷却拦截。

        必须把整个表情条目传给 ``send_danmaku(emoticon=...)``：接口要求以
        ``emoticon_unique`` 作为 msg 并附 ``emoticonOptions``，直接发触发词会被
        服务端拒绝。
        """
        if self._dm_sending:
            return
        block = self._dm_send_block_reason()
        if block:
            self.dm_hint.setText(block)
            return
        trigger = str(emo.get("trigger") or emo.get("text") or "").strip()
        reason = danmaku_send_guard(
            trigger, last_time=self._last_dm_send, now=time.time(),
            cooldown=DM_SEND_COOLDOWN)
        if reason:
            self.dm_hint.setText(str(reason))
            return
        room_id = self.selected_room
        self._dm_sending = True
        self._refresh_dm_send_state()
        self.dm_hint.setText("发送表情中…")
        self.hub.submit(self._async_send_danmaku(
            room_id=room_id, text=trigger,
            color=self._selected_color(), mode=self._selected_mode(),
            reply_mid=0, reply_uname="", emoticon=emo))

    # ---------- 外部联动 ----------

    def on_room_selected(self, room_id: Optional[int]) -> None:
        self._clear_dm_reply_target()
        self._refresh_dm_send_state()
        # 表情面板展示的是具体房间的表情：切房即收起（并记住停留的包）
        self.hide_emoticon_panel()