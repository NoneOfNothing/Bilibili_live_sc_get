"""右下角自绘悬浮提醒窗：开播提醒的悬浮窗通道。

与系统 toast 相比的优势：
- 完全不受 Windows 勿扰模式 / 专注助手影响，无需任何系统设置；
- 通过 ``WS_EX_NOACTIVATE`` 扩展风格保证显示与点击都**不抢占**用户
  当前窗口的焦点；
- 名称、图标、样式完全由本程序控制。

悬浮窗由 :class:`ToastOverlayManager` 统一管理：右下角自下而上堆叠、
超时自动消失（可切换为常驻，仅点击关闭）、点击立即关闭、超出同时
显示上限时关闭最旧一条。所有操作都在 tkinter 主线程执行。
"""

from __future__ import annotations

import ctypes
import tkinter as tk
from typing import List

SHOW_DURATION_MS = 6000  # 单条悬浮窗显示时长
WIDTH = 320              # 悬浮窗宽度（像素）
MARGIN = 12              # 距屏幕右/下边缘的间距
SPACING = 8              # 多条悬浮窗之间的间距
TASKBAR_HEIGHT = 48      # 预留任务栏高度
MAX_VISIBLE = 4          # 同时显示上限，超出时关闭最旧一条

_BG = "#1e1f22"          # 卡片底色
_ACCENT = "#2ea043"      # 左侧直播中主题色条
_FG_TITLE = "#ffffff"
_FG_BODY = "#c9d1d9"

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOPMOST = 0x00000008


def toast_geometry(index: int, screen_w: int, screen_h: int, height: int,
                   width: int = WIDTH, margin: int = MARGIN,
                   spacing: int = SPACING, taskbar: int = TASKBAR_HEIGHT) -> str:
    """计算第 index 条（0 起，最新的贴任务栏）悬浮窗的 geometry 字符串。

    自下而上堆叠：index 每加 1，位置向上挪一个窗高加一个间距。
    """
    x = screen_w - width - margin
    y = screen_h - taskbar - margin - (index + 1) * height - index * spacing
    return f"{width}x{height}+{x}+{y}"


def apply_noactivate(win: tk.Toplevel) -> None:
    """给悬浮窗设置 WS_EX_NOACTIVATE | WS_EX_TOPMOST，显示/点击不抢焦点。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        if not hwnd:
            return
        get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        ex = get_long(hwnd, _GWL_EXSTYLE)
        set_long(hwnd, _GWL_EXSTYLE, ex | _WS_EX_NOACTIVATE | _WS_EX_TOPMOST)
    except Exception:  # 非 Windows 或句柄获取失败时静默跳过
        pass


class ToastWindow:
    """单条悬浮提醒窗。由 :class:`ToastOverlayManager` 创建与回收。"""

    def __init__(self, manager: "ToastOverlayManager", root: tk.Misc,
                 title: str, body: str, persist: bool = False):
        self._manager = manager
        self._persist = persist
        self._after_id: Optional[str] = None
        win = self._win = tk.Toplevel(root)
        win.overrideredirect(True)   # 无边框
        win.attributes("-topmost", True)
        win.configure(bg=_BG)
        accent = tk.Frame(win, bg=_ACCENT, width=4)
        accent.pack(side="left", fill="y")
        inner = tk.Frame(win, bg=_BG, padx=12, pady=9)
        inner.pack(side="left", fill="both", expand=True)
        tk.Label(inner, text=title, bg=_BG, fg=_FG_TITLE, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x")
        if body:
            tk.Label(inner, text=body, bg=_BG, fg=_FG_BODY, anchor="w",
                     justify="left", wraplength=WIDTH - 46,
                     font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(2, 0))
        if persist:
            tk.Label(inner, text="（点击关闭）", bg=_BG, fg=_FG_BODY, anchor="w",
                     font=("Microsoft YaHei UI", 8)).pack(fill="x", pady=(4, 0))
        # 点击任意位置立即关闭；NOACTIVATE 风格下点击不会夺走焦点
        for widget in (win, accent, inner, *inner.winfo_children()):
            widget.bind("<Button-1>", lambda _e: self.close())

    def height(self) -> int:
        return max(self._win.winfo_reqheight(), 60)

    def set_geometry(self, geom: str) -> None:
        self._win.geometry(geom)

    def reveal(self) -> None:
        """完成布局、应用不抢焦点风格；非常驻时启动超时自动关闭。"""
        self._win.update_idletasks()
        apply_noactivate(self._win)
        if not self._persist:
            self._after_id = self._win.after(SHOW_DURATION_MS, self.close)

    def close(self) -> None:
        if self._after_id is not None:
            try:
                self._win.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self._manager._remove(self)
        try:
            self._win.destroy()
        except tk.TclError:
            pass


class ToastOverlayManager:
    """管理右下角悬浮窗的堆叠布局与生命周期（主线程使用）。"""

    def __init__(self, root: tk.Misc):
        self._root = root
        self._toasts: List[ToastWindow] = []

    def show(self, title: str, body: str = "", persist: bool = False) -> None:
        """弹出一悬浮提醒；超过同时显示上限时关闭最旧一条。

        persist=True 时该条常驻显示（不自动关闭），需点击才消失。
        """
        while len(self._toasts) >= MAX_VISIBLE:
            self._toasts[0].close()
        toast = ToastWindow(self, self._root, title, body, persist=persist)
        self._toasts.append(toast)
        toast.reveal()
        self._relayout()

    def _remove(self, toast: ToastWindow) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
            self._relayout()

    def _relayout(self) -> None:
        """右下角自下而上重新排布（最新的贴任务栏）。"""
        if not self._toasts:
            return
        self._root.update_idletasks()
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        for index, toast in enumerate(reversed(self._toasts)):
            toast.set_geometry(
                toast_geometry(index, screen_w, screen_h, toast.height()))
