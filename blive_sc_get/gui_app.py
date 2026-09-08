"""tkinter 图形界面：直播间管理、SC 实时查看、调试日志。

线程模型：tkinter 主循环在主线程；现有 asyncio 核心（RoomClient 等）运行在后台
守护线程。两侧通过线程安全队列通信——GUI 线程只操作界面组件，后台线程只做网络
与文件 IO；回调里绝不直接触碰 tkinter 对象。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

import aiohttp

try:  # 播放系统提示音用（仅 Windows）
    import winsound
except ImportError:
    winsound = None

from .api import BilibiliLiveAPI
from .browser_rooms import is_room_being_recorded
from .cli import _pending_flush_loop, parse_room_id, resolve_cookie
from .client import LIVE_STATUS_TEXT, RoomClient
from .gui_config import (
    SORT_MODES,
    RoomEntry,
    load_room_entries,
    load_ui_prefs,
    save_room_entries,
)
from .storage import SCStorage

logger = logging.getLogger("gui")

DEBUG_LOG_MAX_LINES = 4000

HISTORY_PAGE_SIZE = 100
"""SC 历史默认加载条数；滚动到顶部时再加载更早的一页。"""

SC_TITLE_MAX_LEN = 30
"""醒目留言标题栏中直播标题的最大显示长度。"""

AUTOSCROLL_TICK_MS = 30
"""中键自动滚动的轮询间隔（毫秒）。"""

AUTOSCROLL_DEADZONE = 5
"""中键锚点附近的死区（像素），此范围内不滚动。"""

AUTOSCROLL_SPEED = 0.5
"""中键滚动速度系数：每 tick 滚动像素数 = 距锚点距离 × 该系数。"""

TREE_COLUMN_MIN_WIDTHS = {"room": 60, "anchor": 60, "status": 60, "notify": 40,
                          "title": 80, "note": 60}

SORT_MODE_TEXTS = {"manual": "手动拖动", "room": "按房间号", "anchor": "按主播名",
                   "status": "按直播状态"}

SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(\d+)")


def parse_add_input(raw: str) -> Tuple[Optional[int], Optional[int]]:
    """解析「添加直播间」输入，返回 (room_id, uid)，二者必有一个为 None。

    含 space.bilibili.com 字样的输入一律按主播个人空间网址处理（返回 uid），
    必须优先于房间号解析——uid 是纯数字，先按房间号解析会被直接当成房间号。
    其余输入（纯数字、直播间链接）按房间号解析，无法解析时抛异常。
    """
    space_match = SPACE_URL_RE.search(raw)
    if space_match:
        return None, int(space_match.group(1))
    return parse_room_id(raw), None

PRICE_TAGS = ((500, "price_500"), (100, "price_100"), (50, "price_50"), (0, "price_0"))


def build_sc_segments(time_str: str, sc: dict, deleted: bool = False,
                      pending: bool = False) -> list:
    """把一条 SC 转成 [(文本, 标签), ...] 分段，供实时追加与批量渲染共用。

    用户名段附带 "uid:<n>" 标签，点击用户名可据此打开其个人空间。
    """
    price = sc.get("price") if isinstance(sc.get("price"), (int, float)) else 0
    price_tag = next(t for threshold, t in PRICE_TAGS if price >= threshold)
    user = sc.get("user_info") or {}
    uname = user.get("uname") or sc.get("uname") or "未知用户"
    uid = int(user.get("uid") or sc.get("uid") or 0)
    user_tag = f"user uid:{uid}" if uid else "user"
    message = str(sc.get("message") or "")
    received = (time_str or datetime.now().isoformat(timespec="seconds")).replace("T", " ")
    segments = [
        (f"[{received}] ", "time"),
        (f"¥{price} ", price_tag),
        (f"{uname}：", user_tag),
        (message, "del" if deleted else ""),
    ]
    if deleted:
        segments.append(("  （已删除，退款）", "del"))
    elif pending:
        segments.append(("  （文件被占用，稍后自动写入）", "time"))
    segments.append(("\n", ""))
    return segments


class _QueueLogHandler(logging.Handler):
    """把日志记录转发到 UI 队列，由主线程轮询显示。"""

    def __init__(self, ui_queue: "queue.Queue"):
        super().__init__()
        self._ui_queue = ui_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ui_queue.put(("log", self.format(record)))
        except Exception:
            pass


class AsyncHub:
    """后台线程中的 asyncio 事件循环，持有 API/存储对象供各协程使用。"""

    def __init__(self, output_dir: str, ui_queue: "queue.Queue"):
        self.output_dir = output_dir
        self.ui_queue = ui_queue
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.api: Optional[BilibiliLiveAPI] = None
        self.storage: Optional[SCStorage] = None
        self.ready = threading.Event()
        self._stop_event: Optional[asyncio.Event] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="asyncio", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            cookie = resolve_cookie(None)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                api = BilibiliLiveAPI(session, cookie)
                await api.init_session_info()
                self.api = api
                self.storage = SCStorage(self.output_dir)
                self._stop_event = asyncio.Event()
                flush_task = asyncio.create_task(
                    _pending_flush_loop(self.storage), name="pending-flush"
                )
                self.ready.set()
                self.ui_queue.put(("hub_ready", None))
                await self._stop_event.wait()
                flush_task.cancel()
                await asyncio.gather(flush_task, return_exceptions=True)
                self.storage.flush_all_pending()  # 退出前最后补写一次
        except Exception as exc:
            self.ui_queue.put(("hub_init_failed", {"error": str(exc)}))

    def submit(self, coro) -> None:
        if self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def request_shutdown(self) -> None:
        loop = self.loop

        def _set_stop() -> None:
            if self._stop_event is not None:
                self._stop_event.set()

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_set_stop)


class ScMonitorApp:
    def __init__(self, root: tk.Tk, output_dir: str = "data"):
        self.root = root
        self.output_dir = output_dir
        self.config_path = Path(output_dir) / "gui_rooms.json"
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.hub = AsyncHub(output_dir, self.ui_queue)

        self.entries: Dict[int, RoomEntry] = {
            e.room_id: e for e in load_room_entries(self.config_path)
        }
        self.ui_prefs = load_ui_prefs(self.config_path)  # 排序方式、直播中置顶开关
        self._drag_iid: Optional[str] = None   # 行拖动排序的当前行
        self._drag_moved = False
        self._loaded_count = 0    # 当前选中房间已读取的历史 SC 条数
        self._has_more = False    # 是否还有更早的历史记录未读取
        self._loading_more = False
        self.popularity: Dict[int, int] = {}  # 房间号 -> 同接（心跳人气值）
        self.guard_num: Dict[int, int] = {}   # 房间号 -> 舰长数
        self.viewers: Dict[int, int] = {}     # 房间号 -> 观众数量（同接）
        self._autoscroll_anchor_y = 0  # 中键滚动锚点（屏幕坐标）
        self._autoscroll_widget: Optional[tk.Text] = None
        # 以下状态仅主线程读写
        self.client_states: Dict[int, str] = {}  # starting/running/stopped/occupied/disabled
        self.live_state: Dict[int, str] = {}     # 最近一次的直播状态文本
        self.anchor_names: Dict[int, str] = {}   # 房间号 -> 主播昵称
        self.room_tasks: Dict[int, Tuple[RoomClient, asyncio.Task]] = {}  # asyncio 线程内访问
        self._selected_room_id: Optional[int] = None
        self._note_room_id: Optional[int] = None
        self._history_gen = 0  # 递增代号，用于丢弃切换房间后过期的历史加载结果
        self._sc_total: Dict[int, int] = {}  # 房间号 -> 当前累计 SC 条数（含历史+实时）
        self._resize_wrap_frozen = False   # 拖动窗口期间 SC 文本暂停 word 换行
        self._wrap_restore_id: Optional[str] = None

        self._setup_logging()
        self._build_ui()
        # 拖动窗口大小时暂停 SC 文本 word 换行，停止后恢复（见 _on_root_configure）
        self.root.bind("<Configure>", self._on_root_configure)
        self._populate_rows()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()
        self.hub.start()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        self.root.title("B站直播间 SC 监控")
        try:
            dpi = self.root.winfo_fpixels("1i")
            self.root.tk.call("tk", "scaling", dpi / 72.0)
            scale = max(dpi / 96.0, 1.0)
        except Exception:
            scale = 1.0
        self.root.geometry(f"{int(1000 * scale)}x{int(680 * scale)}")
        self.root.minsize(int(820 * scale), int(540 * scale))

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_rooms_tab(notebook)
        self._build_debug_tab(notebook)

    def _build_rooms_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="直播间")

        add_bar = ttk.Frame(tab)
        add_bar.pack(side="top", fill="x", padx=6, pady=(6, 4))
        ttk.Label(add_bar, text="直播间号 / 直播间地址：").pack(side="left")
        self.add_var = tk.StringVar()
        add_entry = ttk.Entry(add_bar, textvariable=self.add_var)
        add_entry.pack(side="left", fill="x", expand=True, padx=(4, 6))
        add_entry.bind("<Return>", lambda _e: self._on_add_room())
        ttk.Button(add_bar, text="添加", command=self._on_add_room).pack(side="left")

        sort_bar = ttk.Frame(tab)
        sort_bar.pack(side="top", fill="x", padx=6, pady=(0, 4))
        ttk.Label(sort_bar, text="排序：").pack(side="left")
        self.sort_mode_var = tk.StringVar(value=SORT_MODE_TEXTS[self.ui_prefs["sort_mode"]])
        sort_box = ttk.Combobox(sort_bar, textvariable=self.sort_mode_var, width=10,
                                values=list(SORT_MODE_TEXTS.values()),
                                state="readonly")
        sort_box.pack(side="left", padx=(0, 6))
        self.sort_btn = ttk.Button(sort_bar, text="排序", command=self._on_sort_clicked)
        self.sort_btn.pack(side="left", padx=(0, 8))
        self.pin_live_var = tk.BooleanVar(value=bool(self.ui_prefs["pin_live"]))
        ttk.Checkbutton(sort_bar, text="直播中置顶", variable=self.pin_live_var,
                        command=self._on_pin_live_toggled).pack(side="left")
        ttk.Label(sort_bar, text="（按住行拖动可调整顺序，Ctrl 可多选）",
                  foreground="#888888").pack(side="left", padx=(8, 0))

        columns = ("room", "anchor", "status", "notify", "title", "note")
        # 横向滚动条：用户拖宽 room/anchor 等固定列后，总宽可能超过窗口宽度，
        # 通过滚动条保证内容仍可完整查看
        tree_wrap = ttk.Frame(tab)
        tree_wrap.pack(side="top", fill="x", padx=6)
        xscroll = ttk.Scrollbar(tree_wrap, orient="horizontal")
        xscroll.pack(side="bottom", fill="x")
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings",
                                 selectmode="extended", height=7,
                                 xscrollcommand=xscroll.set)
        xscroll.configure(command=self.tree.xview)
        self.tree.heading("room", text="房间号")
        self.tree.heading("anchor", text="主播")
        self.tree.heading("status", text="状态")
        self.tree.heading("notify", text="提醒")
        self.tree.heading("title", text="直播标题（实时）")
        self.tree.heading("note", text="备注")
        self.tree.column("room", width=100, anchor="center", stretch=False)
        self.tree.column("anchor", width=130, anchor="w", stretch=False)
        self.tree.column("status", width=90, anchor="center", stretch=False)
        self.tree.column("notify", width=50, anchor="center", stretch=False)
        self.tree.column("title", width=300, anchor="w", stretch=True)
        self.tree.column("note", width=180, anchor="w", stretch=True)
        for tag, color in (("live", "#1a7f37"), ("offline", "#555555"),
                           ("stopped", "#c62828"), ("disabled", "#999999")):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.pack(side="top", fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._on_room_selected)
        # 拖动列分隔条时，把总列宽收紧到可视宽度内，防止列被拖出窗口右侧。
        # 注意：组件绑定先于 ttk 类绑定执行，此时新列宽还没生效，必须用
        # after_idle 延迟到类绑定处理完再计算；B1-Motion 让拖动过程中持续生效。
        self._tree_columns = columns
        self._clamp_pending = False
        self._tree_widths_cache: Dict[str, int] = {}  # 上次收紧后的各列宽，用于识别被拖宽的列
        # 行拖动排序 + 点击房间号/主播列跳转浏览器（与列宽收紧绑定共存）
        self.tree.bind("<ButtonPress-1>", self._on_tree_press, add=True)
        self.tree.bind("<B1-Motion>", self._on_tree_drag_motion, add=True)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_release, add=True)
        self.tree.bind("<<TreeviewColumnResize>>", self._clamp_columns_soon, add=True)

        bottom = ttk.Frame(tab)
        bottom.pack(side="bottom", fill="x", padx=6, pady=(4, 6))
        ttk.Label(bottom, text="备注:").pack(side="left")
        self.note_var = tk.StringVar()
        self.note_entry = ttk.Entry(bottom, textvariable=self.note_var)
        self.note_entry.pack(side="left", fill="x", expand=True, padx=(4, 8))
        self.note_entry.bind("<Return>", lambda _e: self._flush_note())
        self.note_entry.bind("<FocusOut>", lambda _e: self._flush_note())
        self.toggle_btn = ttk.Button(bottom, text="停用监听", command=self._on_toggle)
        self.toggle_btn.pack(side="left", padx=(0, 4))
        self.delete_btn = ttk.Button(bottom, text="删除", command=self._on_delete)
        self.delete_btn.pack(side="left", padx=(0, 4))
        self.refresh_btn = ttk.Button(bottom, text="刷新历史", command=self._on_refresh_history)
        self.refresh_btn.pack(side="left")

        self.sc_frame = ttk.LabelFrame(tab, text="醒目留言")
        self.sc_frame.pack(side="bottom", fill="both", expand=True, padx=6, pady=4)
        # 底部常显当前房间的 SC 总数（区别于历史加载完成时插入文末的一次性提示）
        self.sc_total_var = tk.StringVar(value="")
        ttk.Label(self.sc_frame, textvariable=self.sc_total_var,
                  anchor="e").pack(side="bottom", fill="x")
        self.sc_text = tk.Text(self.sc_frame, wrap="word", state="disabled",
                               font=("Microsoft YaHei UI", 10), padx=6, pady=4)
        scroll = ttk.Scrollbar(self.sc_frame, command=self._on_sc_scroll)
        self.sc_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.sc_text.pack(side="left", fill="both", expand=True)
        # 滚动条/滚轮滚到顶部时加载更早的历史 SC；中键按住可快速滚动
        self.sc_text.bind("<MouseWheel>", self._on_sc_wheel, add=True)
        self._bind_autoscroll(self.sc_text)
        for tag, color in (("time", "#888888"), ("user", "#0055cc"),
                           ("del", "#aaaaaa"), ("info", "#888888"),
                           ("price_0", "#1f1f1f"), ("price_50", "#b8860b"),
                           ("price_100", "#cc4444"), ("price_500", "#9932cc")):
            self.sc_text.tag_configure(tag, foreground=color)
        self.sc_text.tag_configure("user", underline=1)
        self.sc_text.bind("<Button-1>", self._on_sc_click)

    def _build_debug_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="调试")
        bar = ttk.Frame(tab)
        bar.pack(side="top", fill="x", padx=6, pady=(6, 2))
        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="显示 DEBUG 日志", variable=self.debug_var,
                        command=self._on_debug_toggle).pack(side="left")
        ttk.Button(bar, text="清空", command=self._clear_debug).pack(side="right")
        self.debug_text = tk.Text(tab, wrap="none", state="disabled",
                                  font=("Consolas", 9), padx=4, pady=4)
        scroll = ttk.Scrollbar(tab, command=self.debug_text.yview)
        self.debug_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.debug_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        self._bind_autoscroll(self.debug_text)

    # ---------- 日志 ----------

    def _setup_logging(self) -> None:
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                datefmt="%H:%M:%S")
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # 必须设置 Formatter，否则默认只输出 message，会丢掉 Room[房间号] 前缀
        queue_handler = _QueueLogHandler(self.ui_queue)
        queue_handler.setFormatter(fmt)
        root.addHandler(queue_handler)
        # pythonw 启动时 stdout/stderr 为 None，只保留 GUI 内的日志显示
        if sys.stdout is not None and not any(
            isinstance(h, logging.StreamHandler) for h in root.handlers
        ):
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(fmt)
            root.addHandler(stream)

    def _on_debug_toggle(self) -> None:
        logging.getLogger().setLevel(logging.DEBUG if self.debug_var.get() else logging.INFO)

    def _append_log(self, line: str) -> None:
        self._append_logs([line])

    def _append_logs(self, lines: list) -> None:
        """批量插入日志行：只做一次 state 切换与一次滚动，降低 Tk 重排开销。"""
        if not lines:
            return
        text = self.debug_text
        text.configure(state="normal")
        text.insert("end", "\n".join(lines) + "\n")
        if int(text.index("end-1c").split(".")[0]) > DEBUG_LOG_MAX_LINES:
            text.delete("1.0", f"{DEBUG_LOG_MAX_LINES // 2}.0")
        text.configure(state="disabled")
        text.see("end")

    def _clear_debug(self) -> None:
        self.debug_text.configure(state="normal")
        self.debug_text.delete("1.0", "end")
        self.debug_text.configure(state="disabled")

    # ---------- 房间列表 ----------

    def _populate_rows(self) -> None:
        # 按配置文件中的顺序显示（即上次记忆的顺序），不再启动时自动排序
        for room_id in self.entries:
            self.client_states.setdefault(
                room_id, "disabled" if not self.entries[room_id].enabled else "starting"
            )
            self._insert_row(room_id)

    def _insert_row(self, room_id: int) -> None:
        entry = self.entries[room_id]
        self.tree.insert("", "end", iid=str(room_id), tags=(),
                         values=(room_id, self.anchor_names.get(room_id, ""),
                                 self._status_text(room_id),
                                 "开" if entry.notify_live else "关", "", entry.note))

    def _status_text(self, room_id: int) -> str:
        state = self.client_states.get(room_id, "starting")
        if state == "disabled":
            return "已停用"
        if state == "starting":
            return "连接中…"
        if state == "running":
            return self.live_state.get(room_id, "连接中…")
        if state == "occupied":
            return "被占用"
        return "已停止"

    def _row_tag(self, room_id: int) -> str:
        text = self._status_text(room_id)
        if text == "直播中":
            return "live"
        if text in ("已停止", "被占用"):
            return "stopped"
        if text == "已停用":
            return "disabled"
        return "offline"

    def _refresh_row(self, room_id: int) -> None:
        iid = str(room_id)
        if not self.tree.exists(iid):
            return
        entry = self.entries[room_id]
        self.tree.item(
            iid,
            values=(room_id, self.anchor_names.get(room_id, ""),
                    self._status_text(room_id),
                    "开" if entry.notify_live else "关",
                    self.tree.set(iid, "title"), entry.note),
            tags=(self._row_tag(room_id),),
        )

    def _clamp_columns_soon(self, _event=None) -> None:
        """等 ttk 类绑定应用完新列宽后再收紧（每轮拖动只排一次队）。"""
        if self._clamp_pending:
            return
        self._clamp_pending = True
        self.root.after_idle(self._clamp_columns)

    def _clamp_columns(self) -> None:
        """把总列宽收紧到可视宽度内，防止列被拖出窗口右侧。

        牺牲顺序：未变宽的弹性列（标题/备注）先吸收超出部分 → 被拖宽的
        弹性列 → 被拖宽的固定列。这样拖宽房间号/主播时优先压缩标题列，
        备注列不会被最先挤没；直接拖宽标题/备注时二者也能互相让位。
        """
        self._clamp_pending = False
        tree = self.tree
        avail = tree.winfo_width()
        if avail <= 1:  # 尚未完成布局
            return
        widths = {c: int(tree.column(c, "width")) for c in self._tree_columns}
        overflow = sum(widths.values()) - avail
        if overflow <= 0:
            self._tree_widths_cache = widths
            return
        cache = self._tree_widths_cache
        flex = ("title", "note")
        fixed = [c for c in self._tree_columns if c not in flex]
        grown_flex = [c for c in flex if widths[c] > cache.get(c, 0)]
        grown_fixed = sorted(fixed, key=lambda c: widths[c] - cache.get(c, 0),
                             reverse=True)
        order = [c for c in flex if c not in grown_flex] + grown_flex + grown_fixed
        for col in order:
            if overflow <= 0:
                break
            width = widths[col]
            new_width = max(width - overflow, TREE_COLUMN_MIN_WIDTHS[col])
            tree.column(col, width=new_width)
            overflow -= width - new_width
        self._tree_widths_cache = {c: int(tree.column(c, "width"))
                                   for c in self._tree_columns}

    # ---------- 行拖动排序与点击跳转 ----------

    def _on_tree_press(self, event) -> None:
        """记录按下的行：仅单元格区域可发起拖动排序（列分隔条排除）。"""
        self._drag_iid = None
        self._drag_moved = False
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) == "#0":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._drag_iid = iid

    def _on_tree_drag_motion(self, event) -> None:
        # 列分隔条拖动时同步做列宽收紧
        self._clamp_columns_soon(event)
        if not self._drag_iid:
            return
        target = self.tree.identify_row(event.y)
        if target and target != self._drag_iid:
            self.tree.move(self._drag_iid, "", self.tree.index(target))
            self._drag_moved = True

    def _on_tree_release(self, event) -> None:
        self._clamp_columns_soon(event)
        if self._drag_iid is not None:
            if self._drag_moved:
                # 拖动排序完成：按当前显示顺序回写配置（记忆排序）
                order = [int(iid) for iid in self.tree.get_children()]
                self.entries = {rid: self.entries[rid] for rid in order
                                if rid in self.entries}
                self._save_config()
            elif not (event.state & 0x0005):  # Ctrl/Shift 多选点击不触发跳转
                self._open_tree_link(event)
        self._drag_iid = None
        self._drag_moved = False

    def _open_tree_link(self, event) -> None:
        """单击（未拖动）房间号列 → 打开直播间；主播列 → 打开个人空间；
        提醒列 → 切换该房间的开播提醒开关。"""
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        room_id = int(iid)
        if col == "#1":
            webbrowser.open(f"https://live.bilibili.com/{room_id}")
        elif col == "#2":
            entry = self.entries.get(room_id)
            uid = entry.uid if entry else 0
            if uid:
                webbrowser.open(f"https://space.bilibili.com/{uid}")
            elif self.hub.ready.is_set():
                # uid 未知（旧配置或刚导入）：后台查询一次，成功后再点即可跳转
                self.hub.submit(self._async_fetch_uid(room_id))
                logger.info("正在获取房间 %s 的主播信息，稍后再次点击主播名即可打开个人空间",
                            room_id)
        elif col == "#4":
            entry = self.entries.get(room_id)
            if entry is not None:
                entry.notify_live = not entry.notify_live
                self._save_config()
                self._refresh_row(room_id)

    def _on_sort_clicked(self) -> None:
        """手动触发排序：按所选方式重排，直播中置顶开关生效，并记忆新顺序。"""
        self._apply_sort()
        self._save_config()

    def _on_pin_live_toggled(self) -> None:
        self.ui_prefs["pin_live"] = bool(self.pin_live_var.get())
        self._save_config()

    def _sorted_room_ids(self) -> List[int]:
        mode = next(k for k, v in SORT_MODE_TEXTS.items() if v == self.sort_mode_var.get())
        self.ui_prefs["sort_mode"] = mode
        ids = list(self.entries)
        if mode == "room":
            ids.sort()
        elif mode == "anchor":
            ids.sort(key=lambda r: (not self.anchor_names.get(r), self.anchor_names.get(r, ""), r))
        elif mode == "status":
            rank = {"直播中": 0, "轮播中": 1}
            ids.sort(key=lambda r: (rank.get(self.live_state.get(r, ""), 2), r))
        if self.pin_live_var.get():
            live = [r for r in ids if self.live_state.get(r) == "直播中"]
            rest = [r for r in ids if r not in live]
            ids = live + rest
        return ids

    def _apply_sort(self) -> None:
        order = self._sorted_room_ids()
        for idx, room_id in enumerate(order):
            iid = str(room_id)
            if self.tree.exists(iid):
                self.tree.move(iid, "", idx)
        self.entries = {rid: self.entries[rid] for rid in order if rid in self.entries}

    def _get_selected_room_id(self) -> Optional[int]:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _get_selected_room_ids(self) -> List[int]:
        """按显示顺序返回所有选中房间号（Ctrl 多选时为多个）。"""
        return [int(iid) for iid in self.tree.selection()]

    def _save_config(self) -> None:
        save_room_entries(self.config_path, self.entries.values(), ui=self.ui_prefs)

    def _refresh_buttons(self) -> None:
        room_ids = self._get_selected_room_ids()
        valid = [r for r in room_ids if r in self.entries]
        has = bool(valid)
        for widget in (self.toggle_btn, self.delete_btn, self.refresh_btn,
                       self.note_entry):
            widget.configure(state="normal" if has else "disabled")
        if has:
            # 多选时按"全部已启用→批量停用，否则批量开启"显示按钮文案
            all_enabled = all(self.entries[r].enabled for r in valid)
            self.toggle_btn.configure(text="停用监听" if all_enabled else "开启监听")

    def _on_add_room(self) -> None:
        raw = self.add_var.get().strip()
        if not raw:
            return
        try:
            room_id, uid = parse_add_input(raw)
        except Exception:
            messagebox.showerror("添加失败", f"无法从输入中解析出房间号：{raw}")
            return
        self.add_var.set("")
        if room_id is not None and room_id in self.entries:
            messagebox.showinfo("已存在", f"房间 {room_id} 已在列表中")
            return
        if not self.hub.ready.is_set():
            messagebox.showwarning("请稍候", "后台网络初始化中，请稍后再试")
            return
        self.hub.submit(self._async_add_room(room_id, uid))

    async def _async_add_room(self, room_id: Optional[int],
                              uid: Optional[int] = None) -> None:
        try:
            if uid is not None:
                # 个人空间网址：先由 uid 换算出直播间房间号
                room_id = await self.hub.api.get_room_id_by_uid(uid)
            info = await self.hub.api.get_full_room_info(room_id)
        except Exception as exc:
            self.ui_queue.put(("add_result",
                               {"room_id": room_id or 0, "ok": False, "error": str(exc)}))
            return
        anchor_uid = int(info.get("uid") or uid or 0)
        anchor = await self.hub.api.get_anchor_name(anchor_uid)
        self.ui_queue.put(("add_result", {
            "room_id": int(info.get("room_id") or room_id),
            "ok": True,
            "title": info.get("title") or "",
            "live_status": int(info.get("live_status") or 0),
            "anchor_name": anchor,
            "uid": anchor_uid,
        }))

    def _on_add_result(self, payload: dict) -> None:
        room_id = payload["room_id"]
        if not payload.get("ok"):
            messagebox.showerror("添加失败", f"房间 {room_id} 添加失败：\n{payload.get('error')}")
            return
        if room_id in self.entries:
            messagebox.showinfo("已存在", f"房间 {room_id} 已在列表中")
            return
        self.entries[room_id] = RoomEntry(room_id=room_id, enabled=True,
                                          uid=int(payload.get("uid") or 0))
        self.client_states[room_id] = "starting"
        self.live_state[room_id] = LIVE_STATUS_TEXT.get(int(payload.get("live_status") or 0), "未知")
        if payload.get("anchor_name"):
            self.anchor_names[room_id] = payload["anchor_name"]
        self._save_config()
        self._insert_row(room_id)
        self.tree.set(str(room_id), "title", payload.get("title") or "")
        self.tree.selection_set(str(room_id))
        self.tree.see(str(room_id))
        uid = int(payload.get("uid") or 0)
        anchor = self.anchor_names.get(room_id) or "未知"
        messagebox.showinfo(
            "添加成功",
            f"房间 {room_id} 已加入监听\n主播：{anchor}（uid：{uid or '未知'}）",
        )
        self.hub.submit(self._async_start_room(room_id))

    def _on_toggle(self) -> None:
        """开启/停用监听：支持 Ctrl 多选批量操作。

        选中房间全部已启用 → 批量停用；否则 → 批量开启。
        """
        valid = [r for r in self._get_selected_room_ids() if r in self.entries]
        if not valid:
            return
        all_enabled = all(self.entries[r].enabled for r in valid)
        for room_id in valid:
            if all_enabled:
                self.entries[room_id].enabled = False
                self.client_states[room_id] = "disabled"
                self.hub.submit(self._async_stop_room(room_id))
            else:
                self.entries[room_id].enabled = True
                self.client_states[room_id] = "starting"
                if self.hub.ready.is_set():
                    self.hub.submit(self._async_start_room(room_id))
        self._save_config()
        for room_id in valid:
            self._refresh_row(room_id)
        self._refresh_buttons()

    def _on_delete(self) -> None:
        valid = [r for r in self._get_selected_room_ids() if r in self.entries]
        if not valid:
            return
        if not messagebox.askyesno(
            "删除直播间",
            f"停止监听选中的 {len(valid)} 个直播间并从列表移除？\n"
            f"（房间号：{'、'.join(str(r) for r in valid)}）\n"
            "已保存的 SC 数据会保留在磁盘",
        ):
            return
        for room_id in valid:
            if self.entries[room_id].enabled:
                self.hub.submit(self._async_stop_room(room_id))
            self.entries.pop(room_id, None)
            self.client_states.pop(room_id, None)
            self.live_state.pop(room_id, None)
        self._save_config()
        for room_id in valid:
            if self.tree.exists(str(room_id)):
                self.tree.delete(str(room_id))
        self._on_room_selected()

    def _on_refresh_history(self) -> None:
        self._on_room_selected()

    # ---------- 选中房间与 SC 显示 ----------

    def _on_room_selected(self, _event=None) -> None:
        self._flush_note()
        room_id = self._get_selected_room_id()
        self._selected_room_id = room_id
        self._note_room_id = room_id
        self._refresh_buttons()
        self._clear_sc_view()
        if room_id is None:
            self.sc_frame.configure(text="醒目留言")
            return
        entry = self.entries.get(room_id)
        self.note_var.set(entry.note if entry else "")
        self._update_sc_header(room_id)
        self._update_sc_total_label()
        self._load_history(room_id)

    def _update_sc_header(self, room_id: int) -> None:
        parts = [f"醒目留言 - 房间 {room_id}"]
        anchor = self.anchor_names.get(room_id)
        if anchor:
            parts.append(anchor)
        iid = str(room_id)
        if self.tree.exists(iid):
            title = self.tree.set(iid, "title")
            if title:
                if len(title) > SC_TITLE_MAX_LEN:
                    title = title[:SC_TITLE_MAX_LEN] + "…"
                parts.append(f"标题：{title}")
        if self.viewers.get(room_id, 0) > 0:
            parts.append(f"同接 {self.viewers[room_id]}")
        if room_id in self.guard_num:
            parts.append(f"舰长 {self.guard_num[room_id]}")
        self.sc_frame.configure(text="  |  ".join(parts))

    def _update_sc_total_label(self) -> None:
        """刷新底部常显的“（共 x 条SC记录）”标签。"""
        room_id = self._selected_room_id
        if room_id is None:
            self.sc_total_var.set("")
        else:
            self.sc_total_var.set(f"（共 {self._sc_total.get(room_id, 0)} 条SC记录）")

    def _load_history(self, room_id: int, skip: int = 0) -> None:
        """在后台线程读取一页历史 SC（读磁盘不能阻塞界面线程），完成后经队列渲染。

        首次加载取最新一页（HISTORY_PAGE_SIZE 条）；skip>0 表示向上翻页，
        取更早的一页。页内按时间正序返回。
        """
        self._history_gen += 1
        gen = self._history_gen
        if self.hub.storage is None:
            self._append_info("（后台网络初始化中，稍后会自动加载历史 SC…）")
            return
        storage = self.hub.storage
        if skip == 0:
            self._loaded_count = 0
            self._has_more = False
            self._loading_more = False
            self._append_info("（正在加载历史 SC…）")
        else:
            self._loading_more = True

        def worker() -> None:
            try:
                page = storage.load_sc_page(room_id, limit=HISTORY_PAGE_SIZE, skip=skip)
                deleted = storage.load_deleted_ids(room_id)
                total = storage.count_sc_records(room_id)
            except Exception:
                logger.exception("读取历史 SC 失败 room=%s", room_id)
                page, deleted, total = [], {}, 0
            self.ui_queue.put(("history", {
                "gen": gen, "room_id": room_id, "records": page, "deleted": deleted,
                "total": total, "skip": skip,
            }))

        threading.Thread(target=worker, name=f"history-{room_id}", daemon=True).start()

    def _history_marker(self, loaded: int, total: int) -> str:
        """顶部截断点标记。"""
        if loaded < total:
            return f"（已读取共 {loaded} 条历史记录，向上滚动加载更早记录）\n"
        return f"（已读取全部 {loaded} 条历史记录）\n"

    @staticmethod
    def _render_record(text: tk.Text, record: dict, deleted: dict, at: str) -> None:
        sc = record["sc"]
        for chunk, tag in build_sc_segments(record["time_received"], sc,
                                            deleted=str(sc.get("id")) in deleted):
            text.insert(at, chunk, tag or ())

    def _on_history_loaded(self, payload: dict) -> None:
        """渲染一页历史 SC；首次加载整页渲染，向上翻页时在顶部插入并保持视口。"""
        if payload["gen"] != self._history_gen:
            return  # 用户已切换到其他房间，丢弃过期结果
        if payload["room_id"] != self._selected_room_id:
            return
        room_id = payload["room_id"]
        records = payload["records"]
        deleted = payload["deleted"]
        total = int(payload.get("total") or 0)
        skip = int(payload.get("skip") or 0)
        loaded_before = self._loaded_count  # 触发本次加载时已读取的数量
        self._sc_total[room_id] = total
        self._update_sc_total_label()
        self._loading_more = False
        self._loaded_count = skip + len(records)
        self._has_more = self._loaded_count < total
        marker = self._history_marker(self._loaded_count, total)
        text = self.sc_text
        text.configure(state="normal")
        if skip == 0:
            self._clear_sc_view()
            if not records:
                text.configure(state="disabled")
                self._append_info("（尚无 SC 记录，收到新 SC 后会实时显示在这里）")
                return
            # 注意：_clear_sc_view 结束时会把 Text 置回 disabled，这里必须
            # 重新打开编辑状态，否则后续 insert 全部静默无效
            text.configure(state="normal")
            for record in records:
                self._render_record(text, record, deleted, "end")
            # 底部历史分割线：显示该房间历史 SC 全部数量，其下方是实时新 SC
            text.insert("end", f"（历史 {total} 条sc记录）\n", "info")
            text.insert("1.0", marker, "info")  # 截断标记置于内容最上方
            text.configure(state="disabled")
            text.see("end")
            return
        # 向上翻页：本页（更早的记录）应插在旧内容顶部——即用户滚到顶触发
        # 加载的位置；分割点插在旧内容顶部边界（本页之下）。在 "1.0" 逐段
        # 插入会把段顺序颠倒，因此把整块内容倒序后逐段插入保持正序。
        # 最终顺序：[新标记][本页记录][（已读取 x 条历史记录）][旧内容…]
        top_line = int(text.index("@0,0").split(".")[0])
        lines_before = int(text.index("end-1c").split(".")[0])
        text.delete("1.0", "2.0")  # 旧截断标记
        block: list = []
        for record in records:
            sc = record["sc"]
            for chunk, tag in build_sc_segments(record["time_received"], sc,
                                                deleted=str(sc.get("id")) in deleted):
                block.append((chunk, tag))
        # 先插分割点再插本页内容（均插在 "1.0"，倒序保证段顺序）
        text.insert("1.0", f"（已读取 {loaded_before} 条历史记录）\n", "info")
        for chunk, tag in reversed(block):
            text.insert("1.0", chunk, tag or ())
        text.insert("1.0", marker, "info")
        lines_added = int(text.index("end-1c").split(".")[0]) - lines_before
        text.configure(state="disabled")
        text.see(f"{max(top_line + lines_added, 1)}.0")

    def _on_sc_scroll(self, *args) -> None:
        self.sc_text.yview(*args)
        self.root.after_idle(self._maybe_load_more)

    def _on_sc_wheel(self, _event) -> None:
        self.root.after_idle(self._maybe_load_more)

    def _maybe_load_more(self) -> None:
        """滚动到顶部且还有更早记录时，自动加载前一页。"""
        if (self._selected_room_id is None or not self._has_more
                or self._loading_more or self.hub.storage is None):
            return
        if self.sc_text.yview()[0] > 0.001:
            return
        self._load_history(self._selected_room_id, skip=self._loaded_count)

    def _bind_autoscroll(self, text: tk.Text) -> None:
        """中键点击进入/退出快速滚动模式（同浏览器自动滚动）。

        按一下中键进入：只要鼠标偏离按下点就持续滚动，距锚点越远越快，
        鼠标移出窗口同样生效（用全局指针位置轮询）；再按一下中键或单击
        左键退出。
        """
        text.bind("<Button-2>", lambda e, t=text: self._autoscroll_toggle(e, t), add=True)
        text.bind("<Button-1>", lambda _e, t=text: self._autoscroll_cancel(t), add=True)

    def _autoscroll_toggle(self, event, text: tk.Text) -> None:
        if self._autoscroll_widget is text:
            self._autoscroll_cancel(text)
            return
        self._autoscroll_cancel()  # 关闭其他文本上的滚动模式
        self._autoscroll_widget = text
        self._autoscroll_anchor_y = event.y_root
        try:
            text.configure(cursor="sb_v_double_arrow")
        except tk.TclError:
            pass
        self.root.after(AUTOSCROLL_TICK_MS, self._autoscroll_tick)

    def _autoscroll_tick(self) -> None:
        text = self._autoscroll_widget
        if text is None:
            return
        try:
            dy = text.winfo_pointery() - self._autoscroll_anchor_y
        except tk.TclError:  # 文本组件已被销毁
            self._autoscroll_widget = None
            return
        dead = AUTOSCROLL_DEADZONE
        if dy > dead:
            text.yview_scroll(int((dy - dead) * AUTOSCROLL_SPEED), "pixels")
        elif dy < -dead:
            text.yview_scroll(int((dy + dead) * AUTOSCROLL_SPEED), "pixels")
        self.root.after(AUTOSCROLL_TICK_MS, self._autoscroll_tick)

    def _autoscroll_cancel(self, text: Optional[tk.Text] = None) -> None:
        widget = self._autoscroll_widget
        if widget is None:
            return
        if text is not None and widget is not text:
            return
        try:
            widget.configure(cursor="")
        except tk.TclError:
            pass
        self._autoscroll_widget = None

    def _clear_sc_view(self) -> None:
        self.sc_text.configure(state="normal")
        self.sc_text.delete("1.0", "end")
        self.sc_text.configure(state="disabled")

    def _append_info(self, text: str) -> None:
        self.sc_text.configure(state="normal")
        self.sc_text.insert("end", text + "\n", "info")
        self.sc_text.configure(state="disabled")
        self.sc_text.see("end")

    def _on_sc_click(self, event) -> None:
        """点击 SC 中的用户名 → 打开其个人空间。"""
        text = self.sc_text
        index = text.index(f"@{event.x},{event.y}")
        for tag in text.tag_names(index):
            tag = str(tag)
            if tag.startswith("uid:"):
                try:
                    uid = int(tag.split(":", 1)[1])
                except ValueError:
                    return
                if uid:
                    webbrowser.open(f"https://space.bilibili.com/{uid}")
                return

    def _append_sc(self, time_str: str, sc: dict, deleted: bool = False,
                   pending: bool = False) -> None:
        self.sc_text.configure(state="normal")
        for chunk, tag in build_sc_segments(time_str, sc, deleted, pending):
            self.sc_text.insert("end", chunk, tag or ())
        self.sc_text.configure(state="disabled")
        self.sc_text.see("end")

    def _notify_live(self, room_id: int, title: str) -> None:
        """开播提醒：系统提示音 + 任务栏图标闪烁（不弹窗，不干扰操作）。"""
        logger.info("房间 %s 开播了：%s", room_id, title or "（无标题）")
        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass
        self._flash_taskbar()

    def _flash_taskbar(self) -> None:
        """闪烁任务栏图标（Windows FlashWindowEx），其他平台静默跳过。"""
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                return

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", ctypes.c_uint),
                    ("uCount", ctypes.c_uint),
                    ("dwTimeout", ctypes.c_uint),
                ]

            flashw_all = 0x3  # 图标+标题闪烁
            flashw_timer_nofg = 0xC  # 持续闪烁直到窗口被激活
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                              flashw_all | flashw_timer_nofg, 5, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _flush_note(self) -> None:
        room_id = self._note_room_id
        if room_id is None or room_id not in self.entries:
            return
        text = self.note_var.get().strip()
        if text == self.entries[room_id].note:
            return
        self.entries[room_id].note = text
        self._save_config()
        if self.tree.exists(str(room_id)):
            self.tree.set(str(room_id), "note", text)

    # ---------- 后台事件 ----------

    def _client_event(self, event_type: str, payload: dict) -> None:
        """RoomClient 事件回调（asyncio 线程内执行），转发到 UI 队列。"""
        self.ui_queue.put(("client", event_type, payload))

    async def _async_start_room(self, room_id: int) -> None:
        if self.hub.api is None or self.hub.storage is None:
            return
        if is_room_being_recorded(self.output_dir, room_id):
            self.ui_queue.put(("client", "occupied", {"room_id": room_id}))
            return
        client = RoomClient(self.hub.api, room_id, self.hub.storage,
                            event_callback=self._client_event)
        task = asyncio.create_task(client.run(), name=f"room-{room_id}")
        self.room_tasks[room_id] = (client, task)
        task.add_done_callback(
            lambda _t, rid=room_id: self.ui_queue.put(("room_done", {"room_id": rid}))
        )

    async def _async_stop_room(self, room_id: int) -> None:
        pair = self.room_tasks.pop(room_id, None)
        if pair is None:
            return
        _client, task = pair
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _on_hub_ready(self) -> None:
        logger.info("后台就绪，开始启动已启用的房间")
        for room_id in self.entries:
            if self.entries[room_id].enabled:
                self.client_states[room_id] = "starting"
                self.hub.submit(self._async_start_room(room_id))
        self._refresh_all_rows()
        children = self.tree.get_children()
        if children and self._selected_room_id is None:
            self.tree.selection_set(children[0])
        elif self._selected_room_id is not None:
            self._load_history(self._selected_room_id)

    def _on_hub_failed(self, error: str) -> None:
        logger.error("后台初始化失败：%s", error)
        for room_id in list(self.client_states):
            if self.client_states[room_id] in ("starting", "running"):
                self.client_states[room_id] = "stopped"
        self._refresh_all_rows()
        self._append_info(f"后台初始化失败：{error}")

    def _on_client_event(self, event_type: str, payload: dict) -> None:
        room_id = payload.get("room_id")
        if room_id is None:
            return
        if event_type == "status":
            status_text = LIVE_STATUS_TEXT.get(int(payload.get("live_status") or 0), "未知")
            prev_text = self.live_state.get(room_id)
            self.live_state[room_id] = status_text
            if payload.get("anchor_name"):
                self.anchor_names[room_id] = payload["anchor_name"]
            entry = self.entries.get(room_id)
            if entry is not None and int(payload.get("uid") or 0) != entry.uid:
                entry.uid = int(payload.get("uid") or 0)
                self._save_config()
            # 开播提醒：仅在监听过程中从非直播中变为直播中时触发
            # （程序启动时已在直播中的房间不提醒，避免启动时连响）
            if (prev_text and prev_text != "直播中" and status_text == "直播中"
                    and (entry is None or entry.notify_live)):
                self._notify_live(room_id, payload.get("title") or "")
            if self.client_states.get(room_id) == "starting":
                self.client_states[room_id] = "running"
            if self.tree.exists(str(room_id)):
                self.tree.set(str(room_id), "title", payload.get("title") or "")
                if room_id in self.anchor_names:
                    self.tree.set(str(room_id), "anchor", self.anchor_names[room_id])
                self._refresh_row(room_id)
            if room_id == self._selected_room_id:
                self._update_sc_header(room_id)
        elif event_type == "sc":
            if room_id == self._selected_room_id:
                self._append_sc(payload.get("time_received", ""), payload.get("sc") or {},
                                pending=not payload.get("saved", True))
                self._sc_total[room_id] = self._sc_total.get(room_id, 0) + 1
                self._update_sc_total_label()
        elif event_type == "delete":
            if room_id == self._selected_room_id:
                for sc_id in payload.get("ids", []):
                    self._append_info(f"SC {sc_id} 已被删除（退款）")
        elif event_type == "stopped":
            self.client_states[room_id] = "stopped"
            self._refresh_row(room_id)
        elif event_type == "occupied":
            self.client_states[room_id] = "occupied"
            self._refresh_row(room_id)
        elif event_type == "online_count":
            # 同接：弹幕服务器推送的实时在线人数
            count = int(payload.get("count") or 0)
            if count > 0:
                self.viewers[room_id] = count
                if room_id == self._selected_room_id:
                    self._update_sc_header(room_id)
        elif event_type == "guards":
            num = int(payload.get("num") or -1)
            if num >= 0:
                self.guard_num[room_id] = num
                if room_id == self._selected_room_id:
                    self._update_sc_header(room_id)

    async def _async_fetch_uid(self, room_id: int) -> None:
        """后台查询房间对应主播的 uid（点击主播名跳转个人空间用）。"""
        try:
            info = await self.hub.api.get_room_info(room_id)
        except Exception as exc:
            logger.warning("获取房间 %s 的主播信息失败：%s", room_id, exc)
            return
        self.ui_queue.put(("uid_result", {
            "room_id": room_id,
            "uid": int(info.get("uid") or 0),
        }))

    def _on_uid_result(self, payload: dict) -> None:
        room_id = int(payload.get("room_id") or 0)
        uid = int(payload.get("uid") or 0)
        entry = self.entries.get(room_id)
        if entry is None or not uid:
            return
        entry.uid = uid
        self._save_config()
        logger.info("已获取房间 %s 的主播 uid=%s，再次点击主播名可打开个人空间",
                    room_id, uid)

    def _on_room_done(self, room_id: int) -> None:
        # 任务结束（非用户主动停用）时更新状态；已停用的保持不变
        if self.client_states.get(room_id) in ("starting", "running"):
            self.client_states[room_id] = "stopped"
            self._refresh_row(room_id)
            self._refresh_buttons()

    def _refresh_all_rows(self) -> None:
        for room_id in self.entries:
            self._refresh_row(room_id)

    # ---------- 队列轮询与关闭 ----------

    def _on_root_configure(self, event) -> None:
        """拖动窗口大小时的卡顿优化：SC 文本为 word 自动换行，每次宽度变化
        都要对全部内容重新排版，内容多时开销巨大。拖动期间临时切换为不换行，
        拖动停止 300ms 后再恢复换行并排版一次。"""
        if event.widget is not self.root:
            return
        if not self._resize_wrap_frozen:
            self._resize_wrap_frozen = True
            try:
                self.sc_text.configure(wrap="none")
            except tk.TclError:
                return
        if self._wrap_restore_id is not None:
            try:
                self.root.after_cancel(self._wrap_restore_id)
            except Exception:
                pass
        self._wrap_restore_id = self.root.after(300, self._restore_sc_wrap)
        # 窗口尺寸变化会自动伸缩弹性列，空闲后同步列宽缓存
        self.root.after_idle(self._refresh_tree_widths_cache)

    def _refresh_tree_widths_cache(self) -> None:
        try:
            self._tree_widths_cache = {
                c: int(self.tree.column(c, "width")) for c in self._tree_columns
            }
        except tk.TclError:
            pass

    def _restore_sc_wrap(self) -> None:
        self._wrap_restore_id = None
        if not self._resize_wrap_frozen:
            return
        self._resize_wrap_frozen = False
        try:
            self.sc_text.configure(wrap="word")
        except tk.TclError:
            pass

    def _poll_queue(self) -> None:
        log_lines: list = []
        try:
            for _ in range(500):
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    log_lines.append(item[1])
                elif kind == "hub_ready":
                    self._on_hub_ready()
                elif kind == "hub_init_failed":
                    self._on_hub_failed(item[1].get("error", ""))
                elif kind == "client":
                    self._on_client_event(item[1], item[2])
                elif kind == "room_done":
                    self._on_room_done(item[1]["room_id"])
                elif kind == "history":
                    self._on_history_loaded(item[1])
                elif kind == "add_result":
                    self._on_add_result(item[1])
                elif kind == "uid_result":
                    self._on_uid_result(item[1])
        except queue.Empty:
            pass
        # 本轮所有日志行合并为一次插入，缓解拖动窗口时的卡顿
        self._append_logs(log_lines)
        self.root.after(100, self._poll_queue)

    def _on_close(self) -> None:
        if not messagebox.askokcancel("退出", "确定退出？将停止所有房间的监听。"):
            return
        self.hub.submit(self._async_shutdown())
        self.root.after(1500, self._destroy)

    async def _async_shutdown(self) -> None:
        pairs = list(self.room_tasks.values())
        for _client, task in pairs:
            task.cancel()
        if pairs:
            await asyncio.gather(*[task for _client, task in pairs], return_exceptions=True)
        self.room_tasks.clear()
        self.hub.request_shutdown()

    def _destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def run_gui(output_dir: str = "data") -> int:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    ScMonitorApp(root, output_dir)
    root.mainloop()
    return 0
