"""Qt 版右下角自绘悬浮提醒窗：开播提醒的悬浮窗通道。

与 Tk 版（``overlay.py``）行为一致：右下角自下而上堆叠、超时自动消失
（可切换为常驻，仅点击关闭）、点击立即关闭、超出同时显示上限时关闭最旧。
通过 ``WS_EX_NOACTIVATE`` 保证显示与点击都不抢占当前焦点窗口。
"""

from __future__ import annotations

import ctypes
import logging
from typing import List

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from .log_categories import CATEGORY_WINDOW, get_logger

logger = get_logger(CATEGORY_WINDOW, "gui_qt.overlay")

SHOW_DURATION_MS = 6000
WIDTH = 320
MARGIN = 12
SPACING = 8
TASKBAR_HEIGHT = 48
MAX_VISIBLE = 4

_BG = "#1e1f22"
_ACCENT = "#2ea043"
_FG_TITLE = "#ffffff"
_FG_BODY = "#c9d1d9"

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOPMOST = 0x00000008


def apply_noactivate(hwnd: int) -> None:
    """给窗口设置 WS_EX_NOACTIVATE | WS_EX_TOPMOST，显示/点击不抢焦点。"""
    try:
        user32 = ctypes.windll.user32
        if not hwnd:
            return
        get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        ex = get_long(hwnd, _GWL_EXSTYLE)
        set_long(hwnd, _GWL_EXSTYLE, ex | _WS_EX_NOACTIVATE | _WS_EX_TOPMOST)
    except Exception:  # 非 Windows 或句柄获取失败时静默跳过
        pass


class QtToastWindow(QWidget):
    """单条悬浮提醒窗。由 :class:`QtToastOverlayManager` 创建与回收。"""

    def __init__(self, manager: "QtToastOverlayManager", title: str,
                 body: str, persist: bool = False):
        super().__init__(None,
                         Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self._manager = manager
        self._persist = persist
        self._closed = False
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(WIDTH)
        self.setStyleSheet(f"background:{_BG};")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # 左侧主题色条
        accent_label = QLabel("")
        accent_label.setFixedWidth(4)
        accent_label.setStyleSheet(f"background:{_ACCENT};")
        accent_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        root.addWidget(accent_label)

        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(12, 9, 12, 9)
        inner_layout.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            f"color:{_FG_TITLE}; font-weight:bold;")
        self.title_label.setWordWrap(True)
        inner_layout.addWidget(self.title_label)
        if body:
            self.body_label = QLabel(body)
            self.body_label.setStyleSheet(f"color:{_FG_BODY};")
            self.body_label.setWordWrap(True)
            self.body_label.setMaximumWidth(WIDTH - 46)
            inner_layout.addWidget(self.body_label)
        if persist:
            hint = QLabel("（点击关闭）")
            hint.setStyleSheet(f"color:{_FG_BODY};")
            inner_layout.addWidget(hint)
        root.addWidget(inner, 1)

        self.setCursor(Qt.PointingHandCursor)

    def showEvent(self, event):  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if self.isVisible():
            apply_noactivate(int(self.winId()))
            if not self._persist:
                QTimer.singleShot(SHOW_DURATION_MS, self.close)

    def mousePressEvent(self, event):  # noqa: N802 - Qt 命名
        self.close()

    def close(self):  # 覆盖 QWidget.close：同步移出管理列表并释放窗口
        if self._closed:
            return
        self._closed = True
        self._manager._remove(self)
        QWidget.close(self)
        self.deleteLater()


class QtToastOverlayManager:
    """管理右下角悬浮窗的堆叠布局与生命周期（Qt 主线程使用）。"""

    def __init__(self):
        self._toasts: List[QtToastWindow] = []

    def show(self, title: str, body: str = "", persist: bool = False) -> None:
        """弹出一悬浮提醒；超过同时显示上限时关闭最旧一条。"""
        while len(self._toasts) >= MAX_VISIBLE:
            self._toasts[0].close()
        toast = QtToastWindow(self, title, body, persist=persist)
        self._toasts.append(toast)
        toast.show()
        self._relayout()

    def _remove(self, toast: QtToastWindow) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
            self._relayout()

    def _relayout(self) -> None:
        """右下角自下而上重新排布（最新的贴任务栏右下方）。"""
        if not self._toasts:
            return
        active = [t for t in self._toasts if t.isVisible()]
        if not active:
            return
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is None:
            return
        width = max(t.width() for t in active)
        latest_at_bottom = list(reversed(active))
        for index, toast in enumerate(latest_at_bottom):
            height = toast.height()
            x = area.right() - width - MARGIN
            y = area.bottom() - TASKBAR_HEIGHT - MARGIN \
                - (index + 1) * height - index * SPACING
            toast.setGeometry(x, y, width, height)
            toast.raise_()
            toast.show()