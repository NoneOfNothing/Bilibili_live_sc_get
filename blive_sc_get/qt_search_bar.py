"""Qt 版通用搜索条（ROADMAP 118，SC / 弹幕共用）。

只负责输入、去抖、计数与上一个/下一个/关闭信号；匹配与高亮由各面板实现
（SC 搜索全量历史、弹幕只搜当前文档，二者语义不同）。

键盘约定与浏览器一致：回车 = 下一个、Shift+回车 = 上一个、Esc = 关闭。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

SEARCH_DEBOUNCE_MS = 180
"""输入停顿多久后开始搜索（毫秒）：边打字边搜会反复读盘/扫文档。"""


class SearchBar(QFrame):
    """紧凑的一行搜索条：输入框 + 计数 + 上一个 / 下一个 / 关闭。"""

    queryChanged = Signal(str)
    nextRequested = Signal()
    prevRequested = Signal()
    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None,
                 placeholder: str = "搜索…（回车下一个，Shift+回车上一个，Esc 关闭）"):
        super().__init__(parent)
        self.setObjectName("SearchBar")
        self.setStyleSheet(
            "QFrame#SearchBar{background:#f7f7f7;border-top:1px solid #e2e2e2;}")
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(6)

        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        row.addWidget(self.edit, 1)

        self.counter = QLabel("", self)
        self.counter.setMinimumWidth(96)
        self.counter.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.counter)

        self.prev_btn = QPushButton("↑", self)
        self.prev_btn.setFixedWidth(28)
        self.prev_btn.setToolTip("上一个（Shift+Enter）")
        row.addWidget(self.prev_btn)

        self.next_btn = QPushButton("↓", self)
        self.next_btn.setFixedWidth(28)
        self.next_btn.setToolTip("下一个（Enter）")
        row.addWidget(self.next_btn)

        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedWidth(28)
        self.close_btn.setToolTip("关闭搜索（Esc）")
        row.addWidget(self.close_btn)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._timer.timeout.connect(
            lambda: self.queryChanged.emit(self.edit.text()))
        self._restore_focus: Optional[QWidget] = None

        self.edit.textChanged.connect(self._restart)
        self.edit.installEventFilter(self)
        self.prev_btn.clicked.connect(self._emit_prev)
        self.next_btn.clicked.connect(self._emit_next)
        self.close_btn.clicked.connect(self._emit_closed)
        self.set_navigation_enabled(False)
        self.hide()

    # ---------- 外部 ----------

    def query(self) -> str:
        return self.edit.text()

    def set_query(self, text: str) -> None:
        """外部回填查询词（不触发去抖，由调用方决定是否立即搜索）。"""
        self.edit.blockSignals(True)
        self.edit.setText(text)
        self.edit.blockSignals(False)

    def set_counter(self, text: str) -> None:
        self.counter.setText(text)

    def set_navigation_enabled(self, enabled: bool) -> None:
        self.prev_btn.setEnabled(bool(enabled))
        self.next_btn.setEnabled(bool(enabled))

    def reveal(self) -> None:
        """显示并聚焦输入框；记住唤起前的焦点，关闭后还回去。"""
        if not self.isVisible():
            self._restore_focus = QApplication.focusWidget()
        self.show()
        self.edit.setFocus(Qt.ShortcutFocusReason)
        self.edit.selectAll()

    def hide_bar(self, *, restore_focus: bool = True) -> None:
        self._timer.stop()
        self.hide()
        self.counter.clear()
        self.set_navigation_enabled(False)
        target = self._restore_focus
        self._restore_focus = None
        if restore_focus and target is not None:
            try:
                target.setFocus(Qt.OtherFocusReason)
            except RuntimeError:  # 目标控件已销毁
                pass

    # ---------- 内部 ----------

    def _restart(self) -> None:
        self._timer.start()

    def _emit_prev(self) -> None:
        self.prevRequested.emit()
        self.edit.setFocus(Qt.ShortcutFocusReason)

    def _emit_next(self) -> None:
        self.nextRequested.emit()
        self.edit.setFocus(Qt.ShortcutFocusReason)

    def _emit_closed(self) -> None:
        self.closed.emit()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        if obj is self.edit and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    self.prevRequested.emit()
                else:
                    self.nextRequested.emit()
                return True
            if key == Qt.Key_Escape:
                self.closed.emit()
                return True
        return super().eventFilter(obj, event)
