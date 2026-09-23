"""Tk 版「房间独立窗口」（ROADMAP 84）：只显示某个直播间的 SC 区 + 弹幕区。

用途：同时盯多个直播间而不用在主界面来回切换。窗口里两区的能力与主程序相同
（SC 历史分页 / 吸底 / 未读徽标 / 点击用户名跳主页 / 删除置灰、弹幕显示 / 复制 /
回复 / 表情面板 / 发送弹幕 / 表情悬浮看原图），但不含房间列表、粉丝牌、调试页
与工具栏。

与 Qt 版的差异（实现方式，见 QT_PORTING.md 的「已知差异」）：Qt 版把 SC / 弹幕
做成可复用的面板组件（主界面与子窗口共用同一份渲染代码）；Tk 版的主界面渲染
逻辑与宿主控件强耦合（历史久、无自动化 UI 测试兜底），为**不触碰这条最久经考验
的路径**，本窗口自己实现一份精简渲染，只复用业务层（``hub`` / ``storage`` /
``api``）与框架无关的纯函数（``build_sc_segments`` 等）。数据来源为宿主广播
（``gui_app`` 在事件分发处按房间调用本窗口的 ``on_*`` 方法），历史读盘走本窗口
自己的队列与轮询，绝不消费宿主的 ``ui_queue``。

约定（与用户确认过）：一房一窗、不抢焦点（``overlay.apply_noactivate``）、不置顶、
按房间记忆几何（``gui_rooms.json`` 的 ``ui.room_windows``）、主窗口关闭时统一回收。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Dict, List, Optional, Tuple

from .gui_app import (
    BOTTOM_HOLD_DEBOUNCE_MS,
    DM_COPY_HINT_MS,
    DM_COLOR_PRESETS,
    DM_EMOTICON_TAG_PREFIX,
    DM_META_MAX,
    DM_MODE_TEXTS,
    EMOTICON_GRID_PAD,
    EMOTICON_PANEL_WIDTH_HINT,
    EMOTICON_ROW_HEIGHT,
    EMOTICON_TOOLTIP_DELAY_MS,
    HISTORY_PAGE_SIZE,
    SC_TITLE_MAX_LEN,
    build_sc_segments,
    danmaku_content_from_line,
    danmaku_send_guard,
    dm_trim_index,
    emoticon_from_packages,
    emoticon_tooltip_text,
    live_duration_text,
    text_scrolled_to_bottom,
    unseen_badge_text,
)
from .log_categories import CATEGORY_WINDOW, get_logger
from .overlay import apply_noactivate

logger = logging.getLogger("gui.tk_roomwin")
log_window = get_logger(CATEGORY_WINDOW, "gui.tk_roomwin")
log_data = get_logger(CATEGORY_WINDOW, "gui.tk_roomwin.data")

GEOMETRY_SAVE_DELAY_MS = 1000
"""移动 / 缩放后多久把几何写盘（去抖，避免拖拽时写盘风暴）。"""

LOCAL_POLL_MS = 250
"""本窗口自己的小队列轮询间隔（历史读盘结果回投用）。"""

DEFAULT_SIZE = (620, 800)
MIN_SIZE = (380, 320)
BADGE_WIDTH = 16


class RoomChatWindow(tk.Toplevel):
    """某个直播间的独立窗口：上 SC 区、下弹幕区（含发送与表情）。"""

    def __init__(self, host, room_id: int):
        super().__init__(host.root)
        self.host = host
        self._room_id = int(room_id)
        self._closing = False
        # 视图级状态（与主界面互不影响）
        self._history_gen = 0
        self._loaded_count = 0
        self._has_more = False
        self._loading_more = False
        self._sc_unseen = 0
        self._dm_unseen = 0
        self._bottom_at = {"sc": True, "dm": True}
        self._bottom_hold: Dict[str, Optional[str]] = {"sc": None, "dm": None}
        self._bottom_hold_was = {"sc": True, "dm": True}
        self._dm_meta: Dict[str, Tuple[int, str, str]] = {}
        self._dm_reply_target: Optional[dict] = None
        self._dm_sending = False
        self._queue: "queue.Queue" = queue.Queue()
        self._pending_images: set = set()
        self._emoticon_urls: Dict[str, str] = {}
        self._emoticon_info: Dict[str, dict] = {}
        self._emoticon_visible = False
        self._emoticon_packages: List[dict] = []
        self._emoticon_page = 0
        self._emoticon_buttons: Dict[str, tk.Button] = {}
        self._emoticon_button_list: List[Tuple[str, tk.Button]] = []
        self._emoticon_tip_id: Optional[str] = None
        self._emoticon_tip_key = ""
        self._emoticon_tip_pair: Optional[Tuple[dict, bool]] = None  # (info, 是否显示原图)
        self._emoticon_tip_with_image = False
        self._copy_hint_id: Optional[str] = None
        self._geom_after: Optional[str] = None
        self._poll_after: Optional[str] = None

        self.title(self._title_text())
        self.minsize(*MIN_SIZE)
        # 打开时不抢主窗口焦点（与开播悬浮窗同款做法，见 overlay.apply_noactivate）
        apply_noactivate(self)
        self._build_ui()
        self.bind("<Configure>", self._on_configure)
        self.protocol("WM_DELETE_WINDOW", self.shutdown)
        host.register_room_window(self)
        # 该房间要开始接收弹幕（独立窗口弹幕区恒显示）
        host._apply_dm_gate()
        self._update_sc_header()
        self._update_sc_total_label()
        self._append_info("（正在加载该直播间的历史 SC…）")
        self._load_history(self._room_id)
        self._poll_after = self.after(LOCAL_POLL_MS, self._poll_local_queue)
        log_window.info("打开房间独立窗口：房间 %s（%s）", self._room_id, self._title_text())

    # ---------- UI ----------

    def room_id(self) -> int:
        return self._room_id

    def _title_text(self) -> str:
        anchor = self.host.anchor_names.get(self._room_id) or "未知主播"
        return f"房间 {self._room_id} · {anchor}"

    def _build_ui(self) -> None:
        paned = ttk.Panedwindow(self, orient="vertical")
        paned.pack(side="top", fill="both", expand=True, padx=4, pady=4)
        self._paned = paned

        # ---- SC 区 ----
        self.sc_frame = ttk.LabelFrame(paned, text="醒目留言")
        paned.add(self.sc_frame, weight=3)
        self.sc_total_var = tk.StringVar(value="")
        ttk.Label(self.sc_frame, textvariable=self.sc_total_var,
                  anchor="e").pack(side="bottom", fill="x")
        self.sc_text = tk.Text(self.sc_frame, wrap="word", state="disabled",
                               font=("Microsoft YaHei UI", 10), padx=6, pady=4,
                               height=10)
        self.sc_text.bind("<Configure>", lambda e: self._on_text_configure(e, "sc"))
        sc_scroll = ttk.Scrollbar(self.sc_frame, command=self._on_sc_scroll)
        self.sc_text.configure(yscrollcommand=sc_scroll.set)
        sc_scroll.pack(side="right", fill="y")
        self.sc_text.pack(side="left", fill="both", expand=True)
        self.sc_text.bind("<MouseWheel>", self._on_sc_wheel, add=True)
        for tag, color in (("time", "#888888"), ("user", "#0055cc"),
                           ("del", "#aaaaaa"), ("info", "#888888"),
                           ("price_0", "#1f1f1f"), ("price_50", "#b8860b"),
                           ("price_100", "#cc4444"), ("price_500", "#9932cc")):
            self.sc_text.tag_configure(tag, foreground=color)
        self.sc_text.bind("<Button-1>", self._on_sc_click)
        self.sc_badge = ttk.Button(self.sc_frame, text="", width=BADGE_WIDTH,
                                   command=self._on_sc_badge_clicked)

        # ---- 弹幕区 ----
        self.dm_frame = ttk.LabelFrame(paned, text="弹幕")
        paned.add(self.dm_frame, weight=4)
        send_area = ttk.Frame(self.dm_frame)
        send_area.pack(side="bottom", fill="x", padx=4, pady=(2, 2))
        self._send_area = send_area
        self._build_send_area(send_area)
        self.dm_text = tk.Text(self.dm_frame, wrap="word", state="disabled",
                               font=("Microsoft YaHei UI", 9), padx=6, pady=4,
                               height=8)
        dm_scroll = ttk.Scrollbar(self.dm_frame, command=self.dm_text.yview)
        self.dm_text.configure(yscrollcommand=dm_scroll.set)
        dm_scroll.pack(side="right", fill="y")
        self.dm_text.pack(side="left", fill="both", expand=True)
        for tag, color in (("dm_time", "#888888"), ("dm_user", "#0055cc")):
            self.dm_text.tag_configure(tag, foreground=color)
        self.dm_text.bind("<Button-1>", self._on_dm_click)
        self.dm_text.bind("<Button-3>", self._on_dm_right_click)
        self.dm_text.bind("<Motion>", self._on_dm_motion)
        self.dm_text.bind("<Leave>", lambda _e: self._hide_emoticon_tooltip())
        self.dm_text.bind("<Escape>", lambda _e: self._hide_emoticon_panel())
        self.dm_text.bind("<Configure>", lambda e: self._on_text_configure(e, "dm"))
        self.dm_badge = ttk.Button(self.dm_frame, text="", width=BADGE_WIDTH,
                                   command=self._on_dm_badge_clicked)
        self._refresh_dm_send_state()
        self._refresh_badges()

    def _build_send_area(self, area: ttk.Frame) -> None:
        row = ttk.Frame(area)
        row.pack(side="top", fill="x")
        self._send_row = row
        self.dm_colors: List[Tuple[str, int]] = list(DM_COLOR_PRESETS)
        self.dm_modes: List[Tuple[str, int]] = list(DM_MODE_TEXTS.items())
        self.dm_color_var = tk.StringVar(value=self.dm_colors[0][0])
        self.dm_color_box = ttk.Combobox(row, textvariable=self.dm_color_var, width=5,
                                         state="readonly",
                                         values=[name for name, _v in self.dm_colors])
        self.dm_color_box.pack(side="left")
        self.dm_mode_var = tk.StringVar(value=self.dm_modes[0][0])
        self.dm_mode_box = ttk.Combobox(row, textvariable=self.dm_mode_var, width=5,
                                        state="readonly",
                                        values=[name for name, _v in self.dm_modes])
        self.dm_mode_box.pack(side="left", padx=(4, 0))
        self.dm_emoji_btn = ttk.Button(row, text="表情", width=5,
                                       command=self._on_open_emoticons)
        self.dm_emoji_btn.pack(side="left", padx=(4, 0))
        self.dm_send_var = tk.StringVar()
        self.dm_send_entry = ttk.Entry(row, textvariable=self.dm_send_var)
        self.dm_send_entry.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.dm_send_entry.bind("<Return>", lambda _e: self._on_send_danmaku())
        self.dm_send_entry.bind("<KeyRelease>", self._update_dm_len_hint)
        self.dm_len_var = tk.StringVar(value="0")
        ttk.Label(row, textvariable=self.dm_len_var,
                  foreground="#888888").pack(side="left")
        self.dm_send_btn = ttk.Button(row, text="发送", command=self._on_send_danmaku)
        self.dm_send_btn.pack(side="left", padx=(4, 0))
        self.dm_send_hint_var = tk.StringVar(value="")
        ttk.Label(area, textvariable=self.dm_send_hint_var,
                  foreground="#888888").pack(side="top", fill="x")
        self.dm_copy_hint_var = tk.StringVar(value="")
        ttk.Label(area, textvariable=self.dm_copy_hint_var,
                  foreground="#1a7f37").pack(side="top", fill="x")
        # 回复 / @ 目标条（默认隐藏）
        self.dm_reply_var = tk.StringVar(value="")
        self.dm_reply_bar = ttk.Frame(area)
        ttk.Label(self.dm_reply_bar, textvariable=self.dm_reply_var,
                  foreground="#0055cc").pack(side="left")
        ttk.Button(self.dm_reply_bar, text="取消", width=6,
                   command=self._clear_dm_reply_target).pack(side="left", padx=(4, 0))
        # 表情面板（内嵌，默认隐藏）
        self.emoticon_panel = ttk.LabelFrame(area, text="发送表情包（点击即发送）")
        self._build_emoticon_panel(self.emoticon_panel)

    def _build_emoticon_panel(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(side="top", fill="x", padx=4, pady=(4, 2))
        self._emoticon_prev_btn = ttk.Button(bar, text="◀ 上一包", width=9,
                                             command=lambda: self._show_emoticon_page(
                                                 self._emoticon_page - 1))
        self._emoticon_prev_btn.pack(side="left")
        self._emoticon_pkg_var = tk.StringVar(value="")
        self._emoticon_pkg_box = ttk.Combobox(bar, textvariable=self._emoticon_pkg_var,
                                              state="readonly", width=18)
        self._emoticon_pkg_box.pack(side="left", padx=6)
        self._emoticon_pkg_box.bind("<<ComboboxSelected>>", self._on_emoticon_pkg_selected)
        self._emoticon_next_btn = ttk.Button(bar, text="下一包 ▶", width=9,
                                             command=lambda: self._show_emoticon_page(
                                                 self._emoticon_page + 1))
        self._emoticon_next_btn.pack(side="left")
        self._emoticon_page_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._emoticon_page_var,
                  foreground="#666666").pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="收起", width=6,
                   command=self._hide_emoticon_panel).pack(side="right")
        self._emoticon_hint_var = tk.StringVar(value="")
        self._emoticon_hint_label = ttk.Label(
            parent, textvariable=self._emoticon_hint_var, foreground="#888888",
            wraplength=EMOTICON_PANEL_WIDTH_HINT, justify="left")
        body = ttk.Frame(parent)
        body.pack(side="top", fill="x", padx=4, pady=(0, 4))
        self._emoticon_canvas = tk.Canvas(body, highlightthickness=0,
                                          width=EMOTICON_PANEL_WIDTH_HINT,
                                          height=EMOTICON_ROW_HEIGHT)
        hbar = ttk.Scrollbar(body, orient="horizontal",
                             command=self._emoticon_canvas.xview)
        self._emoticon_canvas.configure(xscrollcommand=hbar.set)
        hbar.pack(side="bottom", fill="x")
        self._emoticon_canvas.pack(side="top", fill="both", expand=True)
        self._emoticon_grid = ttk.Frame(self._emoticon_canvas)
        self._emoticon_canvas.create_window((0, 0), window=self._emoticon_grid,
                                            anchor="nw")

    # ---------- 宿主广播入口（由 gui_app 按房间调用） ----------

    def on_sc(self, payload: dict) -> None:
        self._append_sc(payload.get("time_received", ""), payload.get("sc") or {},
                        pending=not payload.get("saved", True))

    def on_delete(self, sc_ids) -> None:
        for sc_id in sc_ids:
            self._mark_sc_deleted(sc_id)

    def on_dm_batch(self, batch: List[dict]) -> None:
        self._append_dm_batch(batch)

    def on_meta_changed(self) -> None:
        """直播状态 / 标题 / 同接 / 舰长变化：刷新头部与窗口标题。"""
        self.title(self._title_text())
        self._update_sc_header()

    def refresh_header(self) -> None:
        """只刷头部信息行（宿主每秒调用，让「已播」的秒数往前跳）。

        与 :meth:`on_meta_changed` 分开：那个还会重设窗口标题，没必要每秒做一次。
        """
        self._update_sc_header()

    def on_dm_send_result(self, payload: dict) -> None:
        # 只认自己发起的那一次（payload 的房间号是真实房间号，短号场景需映射）
        expected = self.host._room_id_map.get(self._room_id, self._room_id)
        if payload.get("room_id") is not None and int(payload["room_id"]) != int(expected):
            return
        self._dm_sending = False
        if payload.get("ok"):
            room_id = payload.get("room_id")
            if room_id is not None:
                self.host._last_dm_send[int(room_id)] = time.monotonic()
            if not payload.get("emoticon"):
                self.dm_send_var.set("")
                self._update_dm_len_hint()
                self._set_dm_reply_target(None)
            self.dm_send_hint_var.set("已发送")
        else:
            self.dm_send_hint_var.set(payload.get("error") or "发送失败")
        self._refresh_dm_send_state()

    def on_emoticons(self, payload: dict) -> None:
        """宿主拉到的表情包（宿主已入缓存）；本窗口只在展示该房间时重绘。"""
        if int(payload.get("room_id") or 0) != self._room_id:
            return
        packages = payload.get("packages") or []
        if not packages or not self._emoticon_visible:
            return
        self._emoticon_packages = list(packages)
        self._show_emoticon_page(self._emoticon_page)

    def on_emoticon_image(self, payload: dict) -> None:
        """图片下载完成：更新按钮与悬浮提示（解码只能在主线程做）。"""
        url = str(payload.get("url") or "")
        data = str(payload.get("data") or "")
        self._pending_images.discard(url)
        if not url or not data:
            return
        try:
            raw = tk.PhotoImage(master=self.root, data=data)
        except tk.TclError:
            converted = self.host._convert_image_data(data)
            if converted is None:
                return
            try:
                raw = tk.PhotoImage(master=self.root, data=converted)
            except tk.TclError:
                return
        image = self.host._scale_photo(raw)
        self.host._emoticon_images[url] = image
        btn = self._emoticon_buttons.get(url)
        try:
            if btn is not None and btn.winfo_exists():
                btn.configure(image=image, text="", width=0, height=0)
                self._apply_emoticon_layout()
        except tk.TclError:
            return
        # 悬浮提示正显示这个表情且此前只有文字：补上原图
        if self._emoticon_tip_key and self._emoticon_tip_pair is not None:
            info, with_image = self._emoticon_tip_pair
            if with_image and self._emoticon_urls.get(self._emoticon_tip_key) == url:
                self.host.emoticon_tooltip.show(
                    "", self.root.winfo_pointerx(), self.root.winfo_pointery(),
                    image=image)

    # ---------- 本地队列（历史读盘回投） ----------

    def _poll_local_queue(self) -> None:
        self._poll_after = None
        if self._closing:
            return
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == "history":
                    self._on_history_loaded(item[1])
        except queue.Empty:
            pass
        # 与主界面一致：同一轮轮询里更新吸底状态并清除已读的未读计数
        self._sync_unseen_from_scroll()
        self._poll_after = self.after(LOCAL_POLL_MS, self._poll_local_queue)

    # ---------- SC 渲染 ----------

    def _append_sc(self, time_str: str, sc: dict, *, deleted: bool = False,
                   pending: bool = False) -> None:
        follow = text_scrolled_to_bottom(self.sc_text.yview())
        self.sc_text.configure(state="normal")
        segments = build_sc_segments(time_str, sc, deleted, pending)
        mark = f"sc:{sc.get('id')}"  # 打标记便于删除事件实时定位该条
        last = len(segments) - 1
        for i, (chunk, tag) in enumerate(segments):
            tags = tag or ()
            if i < last:  # 结尾换行不打标记，删除标记可插在行尾
                tags = f"{tags} {mark}".strip()
            self.sc_text.insert("end", chunk, tags or ())
        self.sc_text.configure(state="disabled")
        # 累计数由宿主统一维护（本窗口只负责显示，避免主界面与窗口重复计数）
        self._update_sc_total_label()
        if follow:
            self.sc_text.see("end")
        else:
            self._sc_unseen += 1
            self._refresh_badges()

    def _append_info(self, text: str) -> None:
        follow = text_scrolled_to_bottom(self.sc_text.yview())
        self.sc_text.configure(state="normal")
        self.sc_text.insert("end", text + "\n", "info")
        self.sc_text.configure(state="disabled")
        if follow:
            self.sc_text.see("end")

    def _clear_sc_view(self) -> None:
        self.sc_text.configure(state="normal")
        self.sc_text.delete("1.0", "end")
        self.sc_text.configure(state="disabled")
        self._sc_unseen = 0
        self._refresh_badges()

    def _mark_sc_deleted(self, sc_id) -> None:
        tag = f"sc:{sc_id}"
        ranges = self.sc_text.tag_ranges(tag)
        if not ranges:
            self._append_info(f"SC {sc_id} 已被删除（退款）")
            return
        self.sc_text.configure(state="normal")
        self.sc_text.insert(str(ranges[-1]), "  （已删除，退款）", "del")
        self.sc_text.tag_add("del", ranges[0], ranges[-1])  # 整条置灰
        self.sc_text.configure(state="disabled")

    def _update_sc_header(self) -> None:
        parts = [f"醒目留言 - 房间 {self._room_id}"]
        anchor = self.host.anchor_names.get(self._room_id)
        if anchor:
            parts.append(anchor)
        title = self._room_title()
        if title:
            if len(title) > SC_TITLE_MAX_LEN:
                title = title[:SC_TITLE_MAX_LEN] + "…"
            parts.append(f"标题：{title}")
        viewers = self.host.viewers.get(self._room_id, 0)
        if viewers > 0:
            parts.append(f"同接 {viewers}")
        if self._room_id in self.host.guard_num:
            parts.append(f"舰长 {self.host.guard_num[self._room_id]}")
        # 「已播 01:23:45」：仅直播中且有起点时出现（与主界面头部同一套判断）
        duration = live_duration_text(
            self.host.live_started_at.get(self._room_id),
            self.host.live_state.get(self._room_id) == "直播中")
        if duration:
            parts.append(duration)
        entry = self.host.entries.get(self._room_id)
        medal_name, level = self.host._medal_info_for(
            self._room_id, int(entry.uid) if entry else 0)
        if level:
            parts.append(f"{medal_name} Lv{level}" if medal_name else f"粉丝牌 Lv{level}")
        self.sc_frame.configure(text="  |  ".join(parts))

    def _room_title(self) -> str:
        try:
            iid = str(self._room_id)
            if self.host.tree.exists(iid):
                return str(self.host.tree.set(iid, "title") or "")
        except tk.TclError:
            return ""
        return ""

    def _update_sc_total_label(self) -> None:
        total = self.host._sc_total.get(self._room_id, 0)
        self.sc_total_var.set(f"（共 {total} 条SC记录）")

    def _on_sc_click(self, event) -> None:
        index = self.sc_text.index(f"@{event.x},{event.y}")
        for tag in self.sc_text.tag_names(index):
            tag = str(tag)
            if tag.startswith("uid:"):
                try:
                    uid = int(tag.split(":", 1)[1])
                except ValueError:
                    return
                if uid:
                    webbrowser.open(f"https://space.bilibili.com/{uid}")
                return

    def _on_sc_scroll(self, *args) -> None:
        self.sc_text.yview(*args)
        self.after_idle(self._maybe_load_more)

    def _on_sc_wheel(self, _event) -> None:
        self.after_idle(self._maybe_load_more)

    # ---------- 历史分页 ----------

    def _load_history(self, room_id: int, skip: int = 0) -> None:
        self._history_gen += 1
        gen = self._history_gen
        storage = self.host.hub.storage
        if storage is None:
            self._append_info("（后台网络初始化中，稍后会自动加载历史 SC…）")
            return
        if skip == 0:
            self._loaded_count = 0
            self._has_more = False
            self._loading_more = False
            self._append_info("（正在加载历史 SC…）")
        else:
            self._loading_more = True
        queue_ = self._queue

        def worker() -> None:
            try:
                page = storage.load_sc_page(room_id, limit=HISTORY_PAGE_SIZE, skip=skip)
                deleted = storage.load_deleted_ids(room_id)
                total = storage.count_sc_records(room_id)
            except Exception:
                log_data.exception("读取历史 SC 失败 room=%s", room_id)
                page, deleted, total = [], {}, 0
            queue_.put(("history", {
                "gen": gen, "room_id": room_id, "records": page, "deleted": deleted,
                "total": total, "skip": skip,
            }))

        threading.Thread(target=worker, name=f"roomwin-history-{room_id}",
                         daemon=True).start()

    def _on_history_loaded(self, payload: dict) -> None:
        if self._closing:
            return
        if payload["gen"] != self._history_gen or payload["room_id"] != self._room_id:
            return
        room_id = payload["room_id"]
        records = payload["records"]
        deleted = payload["deleted"]
        total = int(payload.get("total") or 0)
        skip = int(payload.get("skip") or 0)
        loaded_before = self._loaded_count
        self.host._sc_total[room_id] = total
        self._update_sc_total_label()
        self._loading_more = False
        self._loaded_count = skip + len(records)
        self._has_more = self._loaded_count < total
        log_data.debug("独立窗口历史 SC 加载完成：房间 %s，本次 %d 条，累计 %d/%d",
                       room_id, len(records), self._loaded_count, total)
        marker = self._history_marker(self._loaded_count, total)
        text = self.sc_text
        text.configure(state="normal")
        if skip == 0:
            self._clear_sc_view()
            text.configure(state="normal")
            if not records:
                text.configure(state="disabled")
                self._append_info("（尚无 SC 记录，收到新 SC 后会实时显示在这里）")
                return
            for record in records:
                self._render_record(text, record, deleted, "end")
            text.insert("end", f"（历史 {total} 条sc记录）\n", "info")
            text.insert("1.0", marker, "info")
            text.configure(state="disabled")
            text.see("end")
            return
        # 向上翻页：整块插到顶部
        top_line = int(text.index("@0,0").split(".")[0])
        lines_before = int(text.index("end-1c").split(".")[0])
        text.delete("1.0", "2.0")  # 旧截断标记
        block: list = []
        for record in records:
            sc = record["sc"]
            mark = f"sc:{sc.get('id')}"
            segments = build_sc_segments(record["time_received"], sc,
                                        deleted=str(sc.get("id")) in deleted)
            last = len(segments) - 1
            for i, (chunk, tag) in enumerate(segments):
                tags = tag or ()
                if i < last:
                    tags = f"{tags} {mark}".strip()
                block.append((chunk, tags))
        text.insert("1.0", f"（已读取 {loaded_before} 条历史记录）\n", "info")
        for chunk, tag in reversed(block):
            text.insert("1.0", chunk, tag or ())
        text.insert("1.0", marker, "info")
        lines_added = int(text.index("end-1c").split(".")[0]) - lines_before
        text.configure(state="disabled")
        text.see(f"{max(top_line + lines_added, 1)}.0")

    @staticmethod
    def _render_record(text: tk.Text, record: dict, deleted: dict, at: str) -> None:
        sc = record["sc"]
        segments = build_sc_segments(record["time_received"], sc,
                                     deleted=str(sc.get("id")) in deleted)
        mark = f"sc:{sc.get('id')}"
        last = len(segments) - 1
        for i, (chunk, tag) in enumerate(segments):
            tags = tag or ()
            if i < last:
                tags = f"{tags} {mark}".strip()
            text.insert(at, chunk, tags or ())

    @staticmethod
    def _history_marker(loaded: int, total: int) -> str:
        if loaded < total:
            return f"（已读取共 {loaded} 条历史记录，向上滚动加载更早记录）\n"
        return f"（已读取全部 {loaded} 条历史记录）\n"

    def _maybe_load_more(self) -> None:
        if not self._has_more or self._loading_more or self.host.hub.storage is None:
            return
        if self.sc_text.yview()[0] > 0.001:
            return
        self._load_history(self._room_id, skip=self._loaded_count)

    # ---------- 未读徽标 / 吸底 ----------

    def _refresh_badges(self) -> None:
        self._sync_badge(self.sc_badge, self.sc_text,
                         unseen_badge_text(self._sc_unseen, "SC"))
        self._sync_badge(self.dm_badge, self.dm_text,
                         unseen_badge_text(self._dm_unseen, "弹幕"))

    @staticmethod
    def _sync_badge(badge: ttk.Button, text_widget: tk.Text, label: str) -> None:
        if not label:
            badge.place_forget()
            return
        badge.configure(text=label)
        badge.place(in_=text_widget, relx=1.0, rely=1.0, anchor="se", x=-12, y=-6)
        badge.lift()

    def _on_sc_badge_clicked(self) -> None:
        self._sc_unseen = 0
        self.sc_text.see("end")
        self._refresh_badges()

    def _on_dm_badge_clicked(self) -> None:
        self._dm_unseen = 0
        self.dm_text.see("end")
        self._refresh_badges()

    def _on_text_configure(self, _event, key: str) -> None:
        if self._bottom_hold[key] is None:
            self._bottom_hold_was[key] = self._bottom_at[key]
        pending = self._bottom_hold[key]
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        self._bottom_hold[key] = self.after(
            BOTTOM_HOLD_DEBOUNCE_MS, lambda: self._restore_bottom(key))

    def _restore_bottom(self, key: str) -> None:
        self._bottom_hold[key] = None
        if not self._bottom_hold_was[key]:
            return
        text = self.sc_text if key == "sc" else self.dm_text
        try:
            text.see("end")
        except tk.TclError:
            pass

    def _sync_unseen_from_scroll(self) -> None:
        """每轮本地轮询：记录两区是否吸底；用户拉回底部后清零未读并隐藏徽标。"""
        if self._closing:
            return
        changed = False
        for key, unseen_attr, text in (("sc", "_sc_unseen", self.sc_text),
                                       ("dm", "_dm_unseen", self.dm_text)):
            at_bottom = text_scrolled_to_bottom(text.yview())
            self._bottom_at[key] = at_bottom
            if getattr(self, unseen_attr) and at_bottom:
                setattr(self, unseen_attr, 0)
                changed = True
        if changed:
            self._refresh_badges()

    # ---------- 弹幕渲染与交互 ----------

    def _append_dm_batch(self, batch: List[dict]) -> None:
        if not batch:
            return
        text = self.dm_text
        follow = text_scrolled_to_bottom(text.yview())
        text.configure(state="normal")
        for dm in batch:
            time_str = str(dm.get("time", ""))
            uid = int(dm.get("uid") or 0)
            dmid = str(dm.get("dmid") or "")
            uname = str(dm.get("uname", ""))
            content = str(dm.get("text", ""))
            dm_tag = f"dm:{dmid}" if dmid else ""
            if dmid:
                self._remember_dm_meta(dmid, uid, uname, content)
            body_tag = f"dmbody:{dmid}" if dmid else "dmbody"
            text.insert("end", f"[{time_str[11:19] or time_str}] ",
                        f"dm_time {dm_tag}".strip())
            user_tag = f"dm_user dmuid:{uid}" if uid else "dm_user"
            text.insert("end", f"{uname}：", f"{user_tag} {dm_tag}".strip())
            tags = f"{dm_tag} {body_tag}".strip()
            unique = str((dm.get("emoticon") or {}).get("unique") or "")
            if unique:
                self._remember_dm_emoticon(unique, dm, content)
                text.insert("end", content,
                            f"{tags} {DM_EMOTICON_TAG_PREFIX}{unique}".strip())
            else:
                text.insert("end", content, tags)
            text.insert("end", "\n", dm_tag or ())
        self._trim_dm_text()
        text.configure(state="disabled")
        if follow:
            text.see("end")
        else:
            self._dm_unseen += len(batch)
            self._refresh_badges()

    def _trim_dm_text(self) -> None:
        text = self.dm_text
        try:
            total = int(text.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError, AttributeError):
            return
        cut = dm_trim_index(total)
        if not cut:
            return
        try:
            text.delete("1.0", cut)
        except tk.TclError:
            return

    def _remember_dm_meta(self, dmid: str, uid: int, uname: str, content: str) -> None:
        meta = self._dm_meta
        meta[dmid] = (uid, uname, content)
        if len(meta) > DM_META_MAX:
            for key in list(meta)[:DM_META_MAX // 2]:
                meta.pop(key, None)

    def _on_dm_click(self, event) -> None:
        self._hide_emoticon_tooltip()
        text = self.dm_text
        index = text.index(f"@{event.x},{event.y}")
        tags = [str(t) for t in text.tag_names(index)]
        for tag in tags:
            if tag.startswith("dmuid:"):
                try:
                    uid = int(tag.split(":", 1)[1])
                except ValueError:
                    return
                if uid:
                    webbrowser.open(f"https://space.bilibili.com/{uid}")
                return
        if not any(t == "dmbody" or t.startswith("dmbody:") for t in tags):
            return
        if text.get(index, f"{index}+1c") in ("", "\n"):
            return
        content = ""
        for tag in tags:
            if tag.startswith("dmbody:"):
                meta = self._dm_meta.get(tag.split(":", 1)[1])
                if meta:
                    content = str(meta[2] or "")
                break
        if not content:
            content = danmaku_content_from_line(
                text.get(f"{index} linestart", f"{index} lineend"))
        if content:
            self._copy_dm_content(content)

    def _copy_dm_content(self, content: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update_idletasks()
        except tk.TclError:
            return
        snippet = content if len(content) <= 20 else content[:20] + "…"
        self.dm_copy_hint_var.set(f"已复制：{snippet}")
        if self._copy_hint_id is not None:
            try:
                self.after_cancel(self._copy_hint_id)
            except Exception:
                pass
        self._copy_hint_id = self.after(DM_COPY_HINT_MS, self._clear_copy_hint)

    def _clear_copy_hint(self) -> None:
        self._copy_hint_id = None
        self.dm_copy_hint_var.set("")

    def _on_dm_right_click(self, event) -> None:
        self._hide_emoticon_tooltip()
        index = self.dm_text.index(f"@{event.x},{event.y}")
        dmid = ""
        uid = 0
        for tag in self.dm_text.tag_names(index):
            tag = str(tag)
            if tag.startswith("dm:"):
                dmid = tag.split(":", 1)[1]
            elif tag.startswith("dmuid:"):
                try:
                    uid = int(tag.split(":", 1)[1])
                except ValueError:
                    uid = 0
        meta = self._dm_meta.get(dmid, (0, "", ""))
        if not uid:
            uid = int(meta[0] or 0)
        uname = str(meta[1] or "")
        content = str(meta[2] or "")
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(
            label="回复该弹幕", state="normal" if dmid else "disabled",
            command=lambda: self._set_dm_reply_target(
                {"kind": "reply", "uid": uid, "uname": uname,
                 "dmid": dmid, "text": content}))
        menu.add_command(
            label="@ 该用户", state="normal" if uid else "disabled",
            command=lambda: self._set_dm_reply_target(
                {"kind": "at", "uid": uid, "uname": uname, "dmid": "", "text": ""}))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_dm_motion(self, event) -> None:
        info = self._dm_emoticon_under(event)
        key = str(info.get("unique") or "") if info else ""
        if key == self._emoticon_tip_key:
            return
        self._emoticon_tip_key = key
        self._emoticon_tip_with_image = True
        self._cancel_emoticon_tip()
        self.host.emoticon_tooltip.hide()
        if info:
            self._ensure_emoticon_url(info)
            url = str(info.get("url") or "") or self._emoticon_urls.get(key, "")
            if url:
                self.host._request_dm_emoticon_image(url)
            self._schedule_emoticon_tip(info)

    def _dm_emoticon_under(self, event) -> Optional[dict]:
        try:
            index = self.dm_text.index(f"@{event.x},{event.y}")
            names = [str(name) for name in self.dm_text.tag_names(index)]
        except tk.TclError:
            return None
        for name in names:
            if name.startswith(DM_EMOTICON_TAG_PREFIX):
                return self._emoticon_info.get(name[len(DM_EMOTICON_TAG_PREFIX):])
        return None

    def _remember_dm_emoticon(self, unique: str, dm: dict, shown_text: str) -> None:
        emoticon = dm.get("emoticon") or {}
        packages = self.host._emoticons.get(self._room_id)
        pkg = emoticon_from_packages(packages, unique)
        self._emoticon_info[unique] = {
            "text": shown_text, "unique": unique, "room_id": self._room_id,
            "id": int(pkg.get("id") or 0),
        }
        url = str(emoticon.get("url") or "") or str(pkg.get("url") or "")
        if url:
            self._emoticon_urls[unique] = url
        while len(self._emoticon_info) > DM_META_MAX:
            oldest = next(iter(self._emoticon_info))
            self._emoticon_info.pop(oldest, None)
            self._emoticon_urls.pop(oldest, None)

    def _ensure_emoticon_url(self, info: dict) -> None:
        if info.get("url"):
            return
        unique = str(info.get("unique") or "")
        packages = self.host._emoticons.get(self._room_id)
        if packages is None:
            self.host._request_room_packages(self._room_id)
            return
        url = str(emoticon_from_packages(packages, unique).get("url") or "")
        if url:
            info["url"] = url
            self._emoticon_urls[unique] = url

    def _schedule_emoticon_tip(self, info: dict) -> None:
        text = emoticon_tooltip_text(info, self.host.app_config.emoticon_tooltip)
        unique = str(info.get("unique") or "")
        if not text and not (info.get("url") or self._emoticon_urls.get(unique)):
            return
        self._cancel_emoticon_tip()
        self._emoticon_tip_pair = (dict(info), self._emoticon_tip_with_image)
        self._emoticon_tip_id = self.after(
            EMOTICON_TOOLTIP_DELAY_MS, self._show_emoticon_tip)

    def _show_emoticon_tip(self) -> None:
        self._emoticon_tip_id = None
        if self._emoticon_tip_pair is None:
            return
        info, with_image = self._emoticon_tip_pair
        text = emoticon_tooltip_text(info, self.host.app_config.emoticon_tooltip)
        unique = str(info.get("unique") or "")
        url = str(info.get("url") or "") or self._emoticon_urls.get(unique, "")
        image = self.host._emoticon_images.get(url) if with_image else None
        if with_image and url and image is None:
            self.host._request_dm_emoticon_image(url)
        if image is not None:
            text = ""  # 弹幕区：原图即可，触发词与弹幕内容重复
        self.host.emoticon_tooltip.show(
            text, self.winfo_pointerx(), self.winfo_pointery(), image=image)

    def _cancel_emoticon_tip(self) -> None:
        if self._emoticon_tip_id is None:
            return
        try:
            self.after_cancel(self._emoticon_tip_id)
        except Exception:
            pass
        self._emoticon_tip_id = None

    def _hide_emoticon_tooltip(self) -> None:
        self._cancel_emoticon_tip()
        self._emoticon_tip_key = ""
        self._emoticon_tip_pair = None
        self.host.emoticon_tooltip.hide()

    # ---------- 发送弹幕 ----------

    def _dm_send_block_reason(self) -> str:
        app_config = self.host.app_config
        if not app_config.allow_write_operations:
            return "已在 config.json 关闭写操作（allow_write_operations=false）"
        if self.host.hub.api is None:
            return "后台初始化中，请稍候…"
        if not self.host.hub.api.logged_in:
            return "未登录：请用「获取Cookie」获取已登录的 B 站 Cookie"
        if not self.host.hub.api.csrf:
            return "Cookie 缺少 bili_jct，请重新「获取Cookie」"
        return ""

    def _refresh_dm_send_state(self) -> None:
        reason = self._dm_send_block_reason()
        enabled = not reason and not self._dm_sending
        state = "normal" if enabled else "disabled"
        self.dm_send_entry.configure(state=state)
        self.dm_send_btn.configure(state=state)
        self.dm_color_box.configure(state="readonly" if enabled else "disabled")
        self.dm_mode_box.configure(state="readonly" if enabled else "disabled")
        self.dm_emoji_btn.configure(state=state)
        if reason:
            self.dm_send_hint_var.set(reason)

    def _update_dm_len_hint(self, _event=None) -> None:
        """字数计数（仅提示，不做客户端截断）：超过上限时在提示里标注。"""
        length = len(self.dm_send_var.get())
        self.dm_len_var.set(f"{length}/{DANMAKU_MAX_LEN}" if length > DANMAKU_MAX_LEN
                            else str(length))

    def _set_dm_reply_target(self, target: Optional[dict]) -> None:
        self._dm_reply_target = target
        if not target:
            self.dm_reply_var.set("")
            self.dm_reply_bar.pack_forget()
            return
        prefix = "回复" if target.get("kind") == "reply" else "@"
        label = f"{prefix} {target.get('uname') or '未知用户'}"
        if target.get("kind") == "reply" and target.get("text"):
            snippet = str(target["text"])
            if len(snippet) > 20:
                snippet = snippet[:20] + "…"
            label += f"：{snippet}"
        self.dm_reply_var.set(label)
        self.dm_reply_bar.pack(side="top", fill="x", before=self._send_row)
        self.after_idle(self.dm_send_entry.focus_set)

    def _clear_dm_reply_target(self) -> None:
        self._set_dm_reply_target(None)

    def _on_send_danmaku(self) -> None:
        if self._dm_sending:
            return
        reason = self._dm_send_block_reason()
        if reason:
            self.dm_send_hint_var.set(reason)
            return
        text = self.dm_send_var.get().strip()
        room_id = self._room_id
        guard = danmaku_send_guard(
            text, last_time=self.host._last_dm_send.get(room_id, 0.0),
            now=time.monotonic())
        if guard:
            self.dm_send_hint_var.set(guard)
            return
        target = self._dm_reply_target or {}
        color = dict(self.dm_colors).get(self.dm_color_var.get(), self.dm_colors[0][1])
        mode = dict(self.dm_modes).get(self.dm_mode_var.get(), self.dm_modes[0][1])
        real_room = self.host._room_id_map.get(room_id, room_id)
        self._dm_sending = True
        self._refresh_dm_send_state()
        self.dm_send_hint_var.set("发送中…")
        log_window.info("独立窗口发送弹幕：房间 %s，%d 字", room_id, len(text))
        self.host.hub.submit(self.host._async_send_danmaku(
            real_room, text, color=color, mode=mode,
            reply_mid=int(target.get("uid") or 0),
            reply_uname=str(target.get("uname") or ""),
            replay_dmid=str(target.get("dmid") or "")))

    def _send_emoticon(self, emoticon: dict) -> None:
        if self._dm_sending:
            return
        reason = self._dm_send_block_reason()
        if reason:
            self.dm_send_hint_var.set(reason)
            return
        room_id = self._room_id
        trigger = str(emoticon.get("trigger") or emoticon.get("text") or "").strip()
        guard = danmaku_send_guard(
            trigger, last_time=self.host._last_dm_send.get(room_id, 0.0),
            now=time.monotonic())
        if guard:
            self.dm_send_hint_var.set(guard)
            return
        real_room = self.host._room_id_map.get(room_id, room_id)
        self._dm_sending = True
        self._refresh_dm_send_state()
        self.dm_send_hint_var.set("发送中…")
        log_window.info("独立窗口发送表情：房间 %s，表情 %s（unique=%s）", room_id,
                        trigger or "无触发词", emoticon.get("unique") or "")
        self.host.hub.submit(self.host._async_send_danmaku(
            real_room, trigger, color=self.dm_colors[0][1], mode=self.dm_modes[0][1],
            reply_mid=0, reply_uname="", replay_dmid="", emoticon=emoticon))

    # ---------- 表情面板 ----------

    def _on_open_emoticons(self) -> None:
        if self._emoticon_visible:
            self._hide_emoticon_panel()
            return
        reason = self._dm_send_block_reason()
        if reason:
            self.dm_send_hint_var.set(reason)
            return
        self.emoticon_panel.pack(side="top", fill="x", before=self._send_row)
        self._emoticon_visible = True
        packages = self.host._emoticons.get(self._room_id)
        if packages:
            self._emoticon_packages = list(packages)
            self._show_emoticon_page(0)
            self._emoticon_hint_var.set("")
            self._emoticon_hint_label.pack_forget()
        else:
            self._emoticon_hint_var.set("正在获取该直播间的专属表情…")
            self._emoticon_hint_label.pack(side="top", fill="x", padx=4)
        # 表情包会随粉丝灯牌升级解锁：每次展开都后台刷新一次（宿主带冷却）
        self.host._request_room_packages(self._room_id)

    def _hide_emoticon_panel(self) -> None:
        self._hide_emoticon_tooltip()
        self._emoticon_visible = False
        self.emoticon_panel.pack_forget()

    def _show_emoticon_page(self, index: int) -> None:
        if not self._emoticon_visible or not self._emoticon_packages:
            return
        self._hide_emoticon_tooltip()
        index = max(0, min(index, len(self._emoticon_packages) - 1))
        self._emoticon_page = index
        package = self._emoticon_packages[index]
        emoticons = package.get("emoticons") or []
        for child in self._emoticon_grid.winfo_children():
            child.destroy()
        self._emoticon_buttons = {}
        self._emoticon_button_list = []
        self._emoticon_pkg_var.set(f"{index + 1}. {package.get('name') or '表情'}")
        self._emoticon_pkg_box.configure(
            values=[f"{i + 1}. {p.get('name') or '表情'}"
                    for i, p in enumerate(self._emoticon_packages)])
        self._emoticon_page_var.set(
            f"第 {index + 1} / {len(self._emoticon_packages)} 包 · 共 {len(emoticons)} 个表情")
        self._emoticon_prev_btn.configure(state="normal" if index > 0 else "disabled")
        self._emoticon_next_btn.configure(
            state="normal" if index < len(self._emoticon_packages) - 1 else "disabled")
        missing: List[str] = []
        for item in emoticons:
            url = str(item.get("url") or "")
            image = self.host._emoticon_images.get(url)
            btn = tk.Button(self._emoticon_grid, text=str(item.get("text") or url),
                            width=8, height=3, relief="groove", wraplength=96,
                            padx=EMOTICON_GRID_PAD, pady=EMOTICON_GRID_PAD,
                            command=lambda it=item: self._send_emoticon(it))
            if image is not None:
                btn.configure(image=image, text="", width=0, height=0)
            btn.grid(row=0, column=len(self._emoticon_button_list),
                     padx=EMOTICON_GRID_PAD, pady=EMOTICON_GRID_PAD)
            btn.bind("<MouseWheel>", self._on_emoticon_wheel)
            btn.bind("<Enter>", lambda e, it=item: self._schedule_tooltip_for_item(it))
            btn.bind("<Leave>", lambda _e: self._hide_emoticon_tooltip())
            self._emoticon_button_list.append((url, btn))
            if url:
                self._emoticon_buttons[url] = btn
                if image is None and url not in self.host._emoticon_pending:
                    missing.append(url)
        self._apply_emoticon_layout()
        if missing:
            self.host._emoticon_pending.update(missing)
            self.host.hub.submit(self.host._async_load_emoticon_images(missing))

    def _schedule_tooltip_for_item(self, item: dict) -> None:
        """表情面板按钮悬浮：只显示触发词（按钮本身已是图）。"""
        info = {"text": str(item.get("text") or ""),
                "unique": str(item.get("unique") or ""),
                "id": item.get("id"),
                "room_id": self._room_id}
        url = str(item.get("url") or "")
        if url:
            self._emoticon_urls[info["unique"]] = url
        self._emoticon_tip_key = info["unique"]
        self._emoticon_tip_with_image = False
        self._cancel_emoticon_tip()
        self._emoticon_tip_pair = (info, False)
        self._emoticon_tip_id = self.after(
            EMOTICON_TOOLTIP_DELAY_MS, self._show_emoticon_tip)

    def _on_emoticon_pkg_selected(self, _event=None) -> None:
        values = [str(v) for v in self._emoticon_pkg_box.cget("values")]
        try:
            index = values.index(self._emoticon_pkg_var.get())
        except ValueError:
            return
        self._show_emoticon_page(index)

    def _on_emoticon_wheel(self, event) -> None:
        try:
            self._emoticon_canvas.xview_scroll(int(-event.delta / 120), "units")
        except (tk.TclError, AttributeError):
            pass

    def _apply_emoticon_layout(self) -> None:
        """按按钮实际尺寸设置画布滚动区域（单行横向排布）。"""
        try:
            self._emoticon_grid.update_idletasks()
            self._emoticon_canvas.configure(
                scrollregion=self._emoticon_canvas.bbox("all"))
        except tk.TclError:
            return

    # ---------- 几何记忆 / 关闭 ----------

    def restore_geometry(self) -> None:
        rect = self.host.load_room_window_geometry(self._room_id)
        if rect is not None:
            self.geometry(f"{rect[2]}x{rect[3]}+{rect[0]}+{rect[1]}")
            log_window.debug("恢复房间 %s 的窗口几何：%sx%s+%s+%s", self._room_id,
                             rect[2], rect[3], rect[0], rect[1])
            return
        width, height = DEFAULT_SIZE
        origin_x, origin_y = 60, 60
        try:
            origin_x += self.host.root.winfo_rootx()
            origin_y += self.host.root.winfo_rooty()
        except tk.TclError:
            pass
        self.geometry(f"{width}x{height}+{origin_x}+{origin_y}")

    def _on_configure(self, event) -> None:
        if self._closing or event.widget is not self:
            return
        if self._geom_after is not None:
            try:
                self.after_cancel(self._geom_after)
            except Exception:
                pass
        self._geom_after = self.after(GEOMETRY_SAVE_DELAY_MS, self._save_geometry)

    def _save_geometry(self) -> None:
        self._geom_after = None
        if self._closing:
            return
        try:
            self.host.save_room_window_geometry(self._room_id, self.geometry())
        except tk.TclError:
            return

    def shutdown(self) -> None:
        """收窗：保存几何、停掉定时回调、注销视图（可重复调用）。"""
        if self._closing:
            return
        self._closing = True
        self._save_geometry()
        for pending in (self._geom_after, self._poll_after):
            if pending is not None:
                try:
                    self.after_cancel(pending)
                except Exception:
                    pass
        self._geom_after = None
        self._poll_after = None
        self._cancel_emoticon_tip()
        self.host.emoticon_tooltip.hide()
        self.host.unregister_room_window(self)
        # 该房间可能不再需要接收弹幕（除非主界面正选中它）
        self.host._apply_dm_gate()
        log_window.info("关闭房间独立窗口：房间 %s", self._room_id)
        try:
            self.destroy()
        except tk.TclError:
            pass
