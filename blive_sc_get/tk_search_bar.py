"""Tk 版通用搜索条（ROADMAP 118，SC / 弹幕共用）。

行为与 Qt 版一致：输入去抖后搜索、回车 = 下一个、Shift+回车 = 上一个、Esc = 关闭。
默认不占位（`pack_forget`），`reveal()` 时才 `pack` 到文本控件上方。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

SEARCH_DEBOUNCE_MS = 180
"""输入停顿多久后开始搜索（毫秒）。"""


class SearchBar(ttk.Frame):
    """输入框 + 计数 + 上一个 / 下一个 / 关闭。"""

    def __init__(self, parent, *, on_query: Callable[[str], None],
                 on_next: Callable[[], None], on_prev: Callable[[], None],
                 on_close: Callable[[], None], before: Optional[tk.Widget] = None,
                 placeholder: str = "搜索…"):
        super().__init__(parent)
        self._on_query = on_query
        self._on_next = on_next
        self._on_prev = on_prev
        self._on_close = on_close
        self._before = before
        self._after_id: Optional[str] = None
        self._silent = False

        self.var = tk.StringVar(value="")
        self.entry = ttk.Entry(self, textvariable=self.var)
        self.entry.pack(side="left", fill="x", expand=True, padx=(4, 4), pady=2)
        self.counter = ttk.Label(self, text="", foreground="#666666", width=12,
                                 anchor="e")
        self.counter.pack(side="left")
        self.prev_btn = ttk.Button(self, text="↑", width=3, command=self._emit_prev)
        self.prev_btn.pack(side="left")
        self.next_btn = ttk.Button(self, text="↓", width=3, command=self._emit_next)
        self.next_btn.pack(side="left")
        self.close_btn = ttk.Button(self, text="✕", width=3, command=self._emit_close)
        self.close_btn.pack(side="left", padx=(0, 2))

        self.var.trace_add("write", self._schedule)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Shift-Return>", self._on_shift_return)
        self.entry.bind("<Escape>", self._on_escape)
        self._placeholder = placeholder
        self.set_navigation_enabled(False)

    # ---------- 外部 ----------

    def query(self) -> str:
        return self.var.get()

    def set_query(self, text: str) -> None:
        self._silent = True
        try:
            self.var.set(text)
        finally:
            self._silent = False
        self._cancel()

    def set_counter(self, text: str) -> None:
        self.counter.configure(text=text)

    def set_navigation_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.prev_btn.configure(state=state)
        self.next_btn.configure(state=state)

    def reveal(self) -> None:
        """显示到文本区上方并聚焦输入框。"""
        if self._before is not None:
            try:
                self.pack(side="top", fill="x", before=self._before)
            except tk.TclError:
                self.pack(side="top", fill="x")
        else:
            self.pack(side="top", fill="x")
        self.entry.focus_set()
        try:
            self.entry.selection_range(0, "end")
        except tk.TclError:
            pass

    def hide_bar(self) -> None:
        self._cancel()
        self.pack_forget()
        self.counter.configure(text="")
        self.set_navigation_enabled(False)

    # ---------- 内部 ----------

    def _schedule(self, *_args) -> None:
        if self._silent:
            return
        self._cancel()
        try:
            self._after_id = self.after(SEARCH_DEBOUNCE_MS, self._emit_query)
        except tk.TclError:
            self._after_id = None

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _emit_query(self) -> None:
        self._after_id = None
        self._on_query(self.var.get())

    def _emit_next(self) -> None:
        self._on_next()
        self.entry.focus_set()

    def _emit_prev(self) -> None:
        self._on_prev()
        self.entry.focus_set()

    def _emit_close(self) -> None:
        self._on_close()

    def _on_return(self, _event=None):
        self._on_next()
        return "break"

    def _on_shift_return(self, _event=None):
        self._on_prev()
        return "break"

    def _on_escape(self, _event=None):
        self._on_close()
        return "break"
