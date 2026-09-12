"""tkinter 图形界面：直播间管理、SC 实时查看、调试日志。

线程模型：tkinter 主循环在主线程；现有 asyncio 核心（RoomClient 等）运行在后台
守护线程。两侧通过线程安全队列通信——GUI 线程只操作界面组件，后台线程只做网络
与文件 IO；回调里绝不直接触碰 tkinter 对象。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import queue
import re
import sys
import threading
import time
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

from .api import ApiError, BilibiliLiveAPI, describe_send_error
from .app_config import load_app_config
from .browser_cookie import get_bilibili_cookie
from .browser_rooms import is_room_being_recorded
from .cli import COOKIE_FILE_NAME, _pending_flush_loop, parse_room_id, resolve_cookie
from .client import LIVE_STATUS_TEXT, RoomClient
from .gui_config import (
    NOTIFY_SOUNDS,
    RoomEntry,
    load_room_entries,
    load_ui_prefs,
    save_room_entries,
)
from .storage import SCStorage
from .overlay import ToastOverlayManager

logger = logging.getLogger("gui")

COOKIE_FILE_PATH = Path(__file__).resolve().parent.parent / COOKIE_FILE_NAME

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

PANE_RATIO = (2.0, 3.5, 4.5)
"""三个板块（房间列表 : 醒目留言 : 弹幕）的默认高度占比 2:3.5:4.5。

PanedWindow 的初始布局由各窗格请求高度决定、weight 只影响多余空间的分配，
因此默认占比需在首次布局后用 sashpos 显式设置（见 _apply_default_pane_ratio）。
"""

PANE_WEIGHTS = (4, 7, 9)
"""与 PANE_RATIO 等比的整数 weight（2:3.5:4.5 = 4:7:9）；窗口尺寸变化时
PanedWindow 按 weight 分配增减空间，从而保持该占比。"""

DANMAKU_SEND_COOLDOWN_S = 2.0
"""同一房间两次发送弹幕的最小间隔（秒）。"""

DANMAKU_DUP_WINDOW_S = 30.0
"""相同内容在该时间窗口内（秒）禁止重复发送。"""

DANMAKU_MAX_LEN = 20
"""客户端弹幕长度上限（普通用户约 20 字；超长时服务端返回 1003212）。"""

DM_COLOR_PRESETS = (("白色", 16777215), ("红色", 16711680), ("橙色", 16744192),
                    ("黄色", 16776960), ("绿色", 65280), ("蓝色", 255),
                    ("紫色", 8388736))
"""内置弹幕颜色候选（名称 -> 十进制值）；登录后可用 get_dm_config 覆盖为服务端可用项。"""

DM_MODE_TEXTS = {"滚动": 1, "顶部": 5, "底部": 4}
"""弹幕展示模式（名称 -> mode 值）。"""

DM_META_MAX = 2000
"""dmid -> (uid, uname, text) 缓存的条目上限，超出后丢弃最早的一半。"""

EMOTICON_ICON_MAX_HEIGHT = 64
"""表情图标的显示高度**安全上限**（像素）。

正常表情（132×60、162×60、231×60 等）都按**原始大小** 1:1 显示，不做缩放；
只有超过该上限的异常大图才等比缩小，以免超出 EMOTICON_ROW_HEIGHT。
"""

EMOTICON_ICON_MAX_WIDTH = 260
"""表情图标的显示宽度**安全上限**（像素），与 EMOTICON_ICON_MAX_HEIGHT 配套。"""

EMOTICON_ROW_HEIGHT = 80
"""表情条高度（像素，**固定**）：单行横向展示，不随表情数量或图标尺寸变化。

按「图标高度上限 + 按钮内边距/网格间距」预留，保证最大图标也能完整显示。
"""

EMOTICON_PANEL_WIDTH_HINT = 720
"""表情条的初始请求宽度（像素）；实际宽度由弹幕区决定。"""

EMOTICON_GRID_PAD = 4
"""表情按钮的内边距与网格间距（像素）。"""

EMOTICON_IMAGE_CACHE_MAX = 300
"""表情图片缓存上限（按 url 计），避免长时间运行后无限增长。"""

DM_COPY_HINT_MS = 1500
"""点击弹幕正文复制后「已复制」提示的保留时长（毫秒）。"""

SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(\d+)")

DM_LINE_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
"""弹幕行首的「[时间] 」前缀，用于从整行文本还原弹幕正文。"""


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

# 开播提示音播放器：键与 gui_config.NOTIFY_SOUNDS 一致，值为 winsound 播放函数
NOTIFY_SOUND_PLAYERS = {
    "上行双音": lambda ws: (ws.Beep(880, 120), ws.Beep(1318, 200)),
    "三连音": lambda ws: (ws.Beep(988, 100), ws.Beep(1175, 100), ws.Beep(1568, 180)),
    "Windows 系统提示音": lambda ws: ws.MessageBeep(ws.MB_ICONASTERISK),
    "静音": lambda ws: None,
}


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


def text_scrolled_to_bottom(yview: tuple, tolerance: float = 0.001) -> bool:
    """根据 tk.Text.yview() 返回值判断视图是否位于（接近）最底部。

    内容不足以产生滚动条时 yview() 返回 (0.0, 1.0)，同样视为在底部。
    用于决定追加新内容后是否自动跟随滚动到底部：用户向上翻阅历史时，
    新到达的消息不应把视图强制拉回底部。
    """
    try:
        bottom = float(yview[1])
    except (TypeError, ValueError, IndexError):
        return True
    return bottom >= 1.0 - tolerance


def danmaku_send_guard(text: str, *, last_text: str = "", last_time: float = 0.0,
                       now: float = 0.0, cooldown: float = DANMAKU_SEND_COOLDOWN_S,
                       dup_window: float = DANMAKU_DUP_WINDOW_S,
                       max_len: Optional[int] = DANMAKU_MAX_LEN) -> Optional[str]:
    """发送弹幕前的客户端校验（纯函数）。

    返回拦截原因（不可发送时的中文提示），可发送时返回 None。
    ``max_len`` 传 None 表示不校验长度（表情包弹幕的触发词由服务端定义）。
    """
    text = (text or "").strip()
    if not text:
        return "弹幕内容不能为空"
    if max_len is not None and len(text) > max_len:
        return f"弹幕过长：最多 {max_len} 字（当前 {len(text)} 字）"
    if last_time and now - last_time < cooldown:
        return f"发送过于频繁，请 {cooldown - (now - last_time):.1f} 秒后再试"
    if last_text and text == last_text and now - last_time < dup_window:
        return "内容与上一条相同，请勿重复发送"
    return None


def unseen_badge_text(count: int, label: str) -> str:
    """新消息浮动徽标的文案（纯函数）；无新消息时返回空串表示隐藏。"""
    try:
        value = int(count)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"{value} 条新{label} ↓"


def danmaku_content_from_line(line: str) -> str:
    """从一行弹幕文本里还原正文（纯函数）。

    行格式为 ``[HH:MM:SS] 用户名：正文``；剥离行首时间与「用户名：」前缀。
    仅在拿不到 dmid 元数据时作为兜底使用。
    """
    text = DM_LINE_PREFIX_RE.sub("", str(line or "").strip())
    _uname, sep, rest = text.partition("：")
    return (rest if sep else text).strip()


def fit_emoticon_scale(width: int, height: int, *,
                       max_w: int = EMOTICON_ICON_MAX_WIDTH,
                       max_h: int = EMOTICON_ICON_MAX_HEIGHT) -> Tuple[int, int]:
    """算出 width×height 该按什么比例缩放（纯函数）。

    返回 ``(zoom, subsample)``：``(1, 1)`` 表示**按原始大小显示、不做缩放**
    （直播间表情都是 60px 高上下的小图，这是常态）。

    只有超过 max_w×max_h 的异常大图才需要缩小。Tk 的 PhotoImage 只支持整数倍
    ``zoom`` / ``subsample``，单用 ``subsample`` 会把宽图按最长边压得极小
    （162×60 → 54×20，细节全丢），故用 ``zoom(a)`` 再 ``subsample(b)`` 近似
    a/b 这类小数比例；找不到合适的小整数比时退回整数倍 ``subsample``。
    """
    try:
        width = int(width or 0)
        height = int(height or 0)
    except (TypeError, ValueError):
        return 1, 1
    if width <= 0 or height <= 0 or max_w <= 0 or max_h <= 0:
        return 1, 1
    target = min(max_w / width, max_h / height)
    if target >= 1.0:
        return 1, 1
    best: Optional[Tuple[int, int]] = None
    best_error = None
    for zoom in range(1, 6):
        for sub in range(zoom + 1, 9):
            ratio = zoom / sub
            if ratio > target * 1.02:
                continue
            error = abs(target - ratio)
            if best_error is None or error < best_error - 1e-9:
                best, best_error = (zoom, sub), error
    if best is not None:
        return best
    return 1, max(2, int(math.ceil(1.0 / target)))


def emoticon_display_size(width: int, height: int, *,
                          max_w: int = EMOTICON_ICON_MAX_WIDTH,
                          max_h: int = EMOTICON_ICON_MAX_HEIGHT) -> Tuple[int, int]:
    """按 fit_emoticon_scale 的比例算出缩放后的显示尺寸（纯函数）。"""
    try:
        width = int(width or 0)
        height = int(height or 0)
    except (TypeError, ValueError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    zoom, sub = fit_emoticon_scale(width, height, max_w=max_w, max_h=max_h)
    if (zoom, sub) == (1, 1):
        return width, height
    return max(1, math.ceil(width * zoom / sub)), max(1, math.ceil(height * zoom / sub))


def select_dm_options(presets, offered):
    """选择弹幕颜色/模式下拉展示的可选项。

    以服务端实际可用项为准（按名称去重），**即使只有 1 项也不回退预设**：
    账号不支持的颜色/模式即使发出去也会失败，列出它们只会误导用户。仅当
    查询失败/无数据（offered 为空）时才回退内置预设，避免下拉为空。

    返回 ``[(名称, 值), ...]``。
    """
    deduped: List[Tuple[str, int]] = []
    seen = set()
    for name, value in offered:
        if name in seen:
            continue
        seen.add(name)
        deduped.append((name, value))
    return deduped or list(presets)


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
                self.storage.close_danmaku_buffers()  # 关闭弹幕句柄并刷盘
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
        self.overlay = ToastOverlayManager(root)
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.hub = AsyncHub(output_dir, self.ui_queue)

        self.entries: Dict[int, RoomEntry] = {
            e.room_id: e for e in load_room_entries(self.config_path)
        }
        self.ui_prefs = load_ui_prefs(self.config_path)  # 排序方式、直播中置顶开关
        self.app_config = load_app_config()  # 应用级配置（写操作默认关闭）
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
        self._dm_grew = False  # 窗口化时弹幕区是否已向下扩展
        self._dm_grew_delta = 0  # 弹幕区向下扩展的像素数
        self._pane_ratio_done = False  # 三板块默认占比是否已应用（仅首次布局）
        self._dm_batch: List[dict] = []  # 待渲染的当前房间弹幕（轮询周期内聚合）
        # 发送弹幕相关状态（仅主线程读写）
        self._last_dm_send: Dict[int, float] = {}   # 房间号 -> 上次发送时间(monotonic)
        self._last_dm_text: Dict[int, str] = {}     # 房间号 -> 上次发送内容
        self._room_id_map: Dict[int, int] = {}      # 输入房间号 -> 真实房间号
        self._dm_sending = False                    # 是否正在发送（防连点）
        self._dm_reply_target: Optional[dict] = None  # 回复/@ 目标 {kind,uid,uname,dmid,text}
        self._dm_send_reason = ""                   # 上次门控原因（避免重复提示）
        self._dm_meta: Dict[str, Tuple[int, str, str]] = {}  # dmid -> (uid, uname, text)
        # 新消息未读计数与弹幕复制提示（仅主线程读写）
        self._sc_unseen = 0                  # SC 区未读新消息数（滚动条不在底部时累计）
        self._dm_unseen = 0                  # 弹幕区未读新消息数
        self._dm_copy_hint_id: Optional[str] = None  # 「已复制」提示的 after id
        # 表情包（写操作）相关状态
        self._emoticons: Dict[int, List[dict]] = {}   # 房间号 -> 可用表情包（含各自表情）
        self._emoticon_visible = False                # 内嵌表情面板是否已展开
        self._emoticon_panel_room: Optional[int] = None  # 面板当前展示的房间号
        self._emoticon_packages: List[dict] = []      # 当前面板载入的表情包
        self._emoticon_page = 0                       # 当前显示的表情包序号（一页 = 一包）
        self._emoticon_button_list: List[Tuple[str, tk.Button]] = []  # 当前页按钮（按顺序）
        self._emoticon_weighted_columns = 0           # 已设置 weight 的列数（换包时清零多余的）
        self._emoticon_view_width = 0                 # 表情条视口宽度（用于铺满判断）
        self._emoticon_regrid_id: Optional[str] = None  # 宽度变化后重排的 after id
        self._emoticon_buttons: Dict[str, tk.Button] = {}   # 图片 url -> 按钮
        self._emoticon_images: Dict[str, tk.PhotoImage] = {}  # 图片 url -> 已解码图片
        self._emoticon_pending: set = set()  # 正在下载的图片 url
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
        logger.info("写操作（发送弹幕等）当前为%s（config.json 的 allow_write_operations）",
                    "启用" if self.app_config.allow_write_operations else "禁用")
        self._build_ui()
        self._refresh_dm_send_state()  # 初始化发送控件的可用状态
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
        self._default_window_size = (int(1000 * scale), int(680 * scale))
        self.root.geometry(f"{self._default_window_size[0]}x{self._default_window_size[1]}")
        self.root.minsize(int(820 * scale), int(540 * scale))

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TPanedwindow", background="#9e9e9e")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)
        self._build_rooms_tab(notebook)
        self._build_debug_tab(notebook)

    def _build_rooms_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="直播间")

        add_bar = ttk.Frame(tab)
        add_bar.pack(side="top", fill="x", padx=6, pady=(6, 4))
        ttk.Label(add_bar, text="直播间号 / 直播间地址 / 主播主页地址：").pack(side="left")
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
        self.notify_overlay_var = tk.BooleanVar(
            value=bool(self.ui_prefs.get("notify_overlay", True)))
        ttk.Checkbutton(sort_bar, text="悬浮窗通知", variable=self.notify_overlay_var,
                        command=self._on_overlay_toggled).pack(side="left", padx=(8, 0))
        self.notify_persist_var = tk.BooleanVar(
            value=bool(self.ui_prefs.get("notify_persist", False)))
        self.notify_persist_check = ttk.Checkbutton(
            sort_bar, text="弹窗常驻", variable=self.notify_persist_var,
            command=self._on_persist_toggled)
        self.notify_persist_check.pack(side="left", padx=(8, 0))
        # 悬浮窗总开关关闭时，常驻选项无意义，初始即置灰（保留取值）
        if not self.ui_prefs.get("notify_overlay", True):
            self.notify_persist_check.configure(state="disabled")
        ttk.Label(sort_bar, text="音效：").pack(side="left", padx=(8, 0))
        self.sound_var = tk.StringVar(
            value=str(self.ui_prefs.get("notify_sound", "上行双音")))
        sound_box = ttk.Combobox(sort_bar, textvariable=self.sound_var, width=12,
                                 values=list(NOTIFY_SOUNDS), state="readonly")
        sound_box.pack(side="left")
        sound_box.bind("<<ComboboxSelected>>", self._on_sound_selected)
        ttk.Button(sort_bar, text="试听",
                   command=self._play_notify_sound).pack(side="left", padx=(4, 0))
        ttk.Label(sort_bar, text="（按住行拖动可调整顺序，Ctrl 可多选）",
                  foreground="#888888").pack(side="left", padx=(8, 0))

        columns = ("room", "anchor", "status", "notify", "title", "note")
        # 三个板块（房间列表 / SC / 弹幕）放入垂直 PanedWindow：
        # 初始高度按 PANE_RATIO（2:3.5:4.5）分配，窗口变化时按 PANE_WEIGHTS 等比伸缩，
        # 拖动分隔条即可调整各板块占用空间
        self.paned = ttk.Panedwindow(tab, orient="vertical")
        # 只创建、不在此处 pack：pack 按调用顺序分配空间，带 expand 的 paned 若先
        # pack，会在窗口缩小时把「后 pack」的底部固定条（底部操作条/弹幕开关条）
        # 挤成 0 高而整条消失（取消勾选弹幕后窗口回缩即触发）。故等底部固定条都
        # pack 完、且所有窗格 add 完之后，再在本方法末尾 pack paned。
        # 首次完成布局后按默认占比设置分隔条位置（此后不再干预用户手动拖动）
        self.paned.bind("<Configure>", self._on_paned_configure, add=True)
        # 横向滚动条：用户拖宽 room/anchor 等固定列后，总宽可能超过窗口宽度，
        # 通过滚动条保证内容仍可完整查看
        tree_wrap = ttk.Frame(self.paned)
        self.tree_wrap = tree_wrap
        # 房间操作行（备注 / 启停监听 / 删除）紧贴房间列表下方。
        # 同一侧先 pack 者贴边：本行最先 pack，故位于窗格最底部，
        # 其上依次是横向滚动条与房间列表。
        self.room_actions = ttk.Frame(tree_wrap)
        self.room_actions.pack(side="bottom", fill="x", padx=4, pady=(2, 4))
        xscroll = ttk.Scrollbar(tree_wrap, orient="horizontal")
        self.xscroll = xscroll
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
        self.tree.pack(side="top", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_room_selected)
        self.paned.add(tree_wrap, weight=PANE_WEIGHTS[0])
        # 房间列表行数自适应窗格高度（拖动分隔条/窗口变化时自动调整）
        tree_wrap.bind("<Configure>", self._fit_tree_height, add=True)
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

        # 房间操作行控件：备注 / 启停监听 / 删除（启用状态由 _refresh_buttons 统一控制）
        ttk.Label(self.room_actions, text="备注:").pack(side="left")
        self.note_var = tk.StringVar()
        self.note_entry = ttk.Entry(self.room_actions, textvariable=self.note_var)
        self.note_entry.pack(side="left", fill="x", expand=True, padx=(4, 8))
        self.note_entry.bind("<Return>", lambda _e: self._flush_note())
        self.note_entry.bind("<FocusOut>", lambda _e: self._flush_note())
        self.toggle_btn = ttk.Button(self.room_actions, text="停用监听", command=self._on_toggle)
        self.toggle_btn.pack(side="left", padx=(0, 4))
        self.delete_btn = ttk.Button(self.room_actions, text="删除", command=self._on_delete)
        self.delete_btn.pack(side="left")

        # 弹幕开关条（窗口最下方，位于底部操作条之下）
        dm_bar = ttk.Frame(tab)
        self.dm_bar = dm_bar
        dm_bar.pack(side="bottom", fill="x", padx=6, pady=(0, 4))
        self.dm_var = tk.BooleanVar(value=bool(self.ui_prefs.get("dm_visible", False)))
        ttk.Checkbutton(dm_bar, text="弹幕", variable=self.dm_var,
                        command=self._on_dm_toggled).pack(side="left")
        ttk.Label(dm_bar, text="（开启后显示并保存当前选中房间的弹幕）",
                  foreground="#888888").pack(side="left", padx=(6, 0))

        # 底部全局操作条：仅保留与房间列表无关的操作（房间相关的备注/启停/删除
        # 已移到直播间列表下方的 room_actions 行）
        bottom = ttk.Frame(tab)
        bottom.pack(side="bottom", fill="x", padx=6, pady=(4, 6))
        self.refresh_btn = ttk.Button(bottom, text="刷新历史", command=self._on_refresh_history)
        self.refresh_btn.pack(side="left")
        self.cookie_btn = ttk.Button(bottom, text="获取Cookie", command=self._on_fetch_cookie)
        self.cookie_btn.pack(side="left", padx=(8, 0))

        self.sc_frame = ttk.LabelFrame(self.paned, text="醒目留言")
        self.paned.add(self.sc_frame, weight=PANE_WEIGHTS[1])
        # 底部常显当前房间的 SC 总数（区别于历史加载完成时插入文末的一次性提示）
        self.sc_total_var = tk.StringVar(value="")
        ttk.Label(self.sc_frame, textvariable=self.sc_total_var,
                  anchor="e").pack(side="bottom", fill="x")
        # height 只影响请求高度：实际高度由 PanedWindow 分配；
        # 请求过高会把底部备注/按钮行挤出窗口（Tk 控件不被父容器裁剪）
        self.sc_text = tk.Text(self.sc_frame, wrap="word", state="disabled",
                               font=("Microsoft YaHei UI", 10), padx=6, pady=4,
                               height=10)
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
        self.sc_text.bind("<Button-1>", self._on_sc_click)

        # 弹幕区（默认隐藏；开启时加入 PanedWindow 挤占其他板块空间）
        self.dm_frame = ttk.LabelFrame(self.paned, text="弹幕")
        # 发送区固定在面板最下方，先 pack 以让弹幕列表只占用其余空间
        self.dm_send_area = ttk.Frame(self.dm_frame)
        self.dm_send_area.pack(side="bottom", fill="x", padx=4, pady=(2, 2))
        self._build_dm_send_area()
        self.dm_text = tk.Text(self.dm_frame, wrap="word", state="disabled",
                               font=("Microsoft YaHei UI", 9), padx=6, pady=4,
                               height=6)
        dm_scroll = ttk.Scrollbar(self.dm_frame, command=self.dm_text.yview)
        self.dm_text.configure(yscrollcommand=dm_scroll.set)
        dm_scroll.pack(side="right", fill="y")
        self.dm_text.pack(side="left", fill="both", expand=True)
        for tag, color in (("dm_time", "#888888"), ("dm_user", "#0055cc")):
            self.dm_text.tag_configure(tag, foreground=color)
        self.dm_text.bind("<Button-1>", self._on_dm_click)
        self.dm_text.bind("<Button-3>", self._on_dm_right_click)
        self.dm_text.bind("<Escape>", lambda _e: self._hide_emoticon_panel())
        # 新消息浮动徽标：滚动条不在底部时显示未读条数，点击回到底部并恢复跟随。
        # 以 place(in_=<Text>) 叠在文本区右下角，不改变既有 pack 布局。
        self.sc_badge = ttk.Button(self.sc_frame, text="", width=16,
                                   command=self._on_sc_badge_clicked)
        self.dm_badge = ttk.Button(self.dm_frame, text="", width=16,
                                   command=self._on_dm_badge_clicked)
        if self.dm_var.get():
            self.paned.add(self.dm_frame, weight=PANE_WEIGHTS[2])
            self._dm_grew = True
            self._dm_adjust_window(True)

        # paned 最后 pack：上（添加/排序栏）下（底部操作条/弹幕开关条）两侧固定条
        # 先占位，剩余空间才归 Panedwindow；窗口缩小时底部控件不会被挤到 0 高
        self.paned.pack(side="top", fill="both", expand=True)

    def _build_dm_send_area(self) -> None:
        """构建弹幕发送区：发送行（颜色/模式/输入/计数/发送）+ 提示行 + 目标提示行。

        仅在 config.json 开启写操作、已登录且选中房间时可用（见
        _refresh_dm_send_state）；创建后由该处统一置为初始状态。
        """
        area = self.dm_send_area
        # 发送行：颜色 / 模式 / 输入框 / 字数 / 发送
        row = ttk.Frame(area)
        row.pack(side="top", fill="x")
        self.dm_send_row = row
        self.dm_colors: List[Tuple[str, int]] = list(DM_COLOR_PRESETS)
        self.dm_modes: List[Tuple[str, int]] = list(DM_MODE_TEXTS.items())
        self.dm_color_var = tk.StringVar(value=self.dm_colors[0][0])
        self.dm_color_box = ttk.Combobox(
            row, textvariable=self.dm_color_var, width=5, state="readonly",
            values=[name for name, _color in self.dm_colors])
        self.dm_color_box.pack(side="left")
        self.dm_mode_var = tk.StringVar(value=self.dm_modes[0][0])
        self.dm_mode_box = ttk.Combobox(
            row, textvariable=self.dm_mode_var, width=5, state="readonly",
            values=[name for name, _mode in self.dm_modes])
        self.dm_mode_box.pack(side="left", padx=(4, 0))
        # 表情按钮：点击在发送行上方展开/收起该直播间的专属表情面板
        # （点选即发送，写操作，受总开关约束）
        self.dm_emoji_btn = ttk.Button(row, text="表情", width=5,
                                       command=self._on_open_emoticons)
        self.dm_emoji_btn.pack(side="left", padx=(4, 0))
        self.dm_send_var = tk.StringVar()
        self.dm_send_entry = ttk.Entry(row, textvariable=self.dm_send_var)
        self.dm_send_entry.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.dm_send_entry.bind("<Return>", lambda _e: self._on_send_danmaku())
        self.dm_send_entry.bind("<KeyRelease>", self._update_dm_len_hint)
        self.dm_send_entry.bind("<Escape>", lambda _e: self._hide_emoticon_panel())
        self.dm_len_var = tk.StringVar(value=f"0/{DANMAKU_MAX_LEN}")
        self.dm_len_label = ttk.Label(row, textvariable=self.dm_len_var,
                                      foreground="#888888")
        self.dm_len_label.pack(side="left")
        self.dm_send_btn = ttk.Button(row, text="发送", command=self._on_send_danmaku)
        self.dm_send_btn.pack(side="left", padx=(4, 0))
        # 提示行：显示门控原因或发送结果
        self.dm_send_hint_var = tk.StringVar(value="")
        ttk.Label(area, textvariable=self.dm_send_hint_var,
                  foreground="#888888").pack(side="top", fill="x")
        # 复制提示行：与发送提示独立，避免点弹幕复制时覆盖发送状态文案
        self.dm_copy_hint_var = tk.StringVar(value="")
        ttk.Label(area, textvariable=self.dm_copy_hint_var,
                  foreground="#1a7f37").pack(side="top", fill="x")
        # 表情面板：内嵌在发送行上方（点「表情」展开/收起），默认隐藏。
        # 用 pack_forget 收起，不另开窗口；内容在首次展开时才向后端请求
        self.emoticon_panel = ttk.LabelFrame(area, text="发送表情包（点击即发送）")
        self._build_emoticon_panel(self.emoticon_panel)
        # 回复/@ 目标提示行：默认隐藏，显示时插入到发送行上方
        self.dm_reply_var = tk.StringVar(value="")
        self.dm_reply_bar = ttk.Frame(area)
        ttk.Label(self.dm_reply_bar, textvariable=self.dm_reply_var,
                  foreground="#0055cc").pack(side="left")
        ttk.Button(self.dm_reply_bar, text="取消", width=6,
                   command=self._clear_dm_reply_target).pack(side="left", padx=(4, 0))

    def _on_paned_configure(self, _event=None) -> None:
        """首次布局完成后应用一次默认占比；之后不再干预用户手动拖动。"""
        if self._pane_ratio_done or self.paned.winfo_height() <= 20:
            return
        self._pane_ratio_done = True
        self._apply_default_pane_ratio()

    def _apply_default_pane_ratio(self) -> None:
        """按 PANE_RATIO 设置各分隔条位置，使板块默认占比为 2:3.5:4.5。

        PanedWindow 的初始尺寸取决于各窗格请求高度，weight 只决定多余空间
        的分配，故这里显式设置 sashpos。弹幕区隐藏时只剩两个板块，取占比
        前两项（房间列表:醒目留言 = 2:3.5）。
        """
        panes = self.paned.panes()
        height = self.paned.winfo_height()
        if len(panes) < 2 or height <= 1:
            return
        ratios = PANE_RATIO[:len(panes)]
        total = sum(ratios)
        accumulated = 0.0
        for index, ratio in enumerate(ratios[:-1]):
            accumulated += ratio
            try:
                self.paned.sashpos(index, int(height * accumulated / total))
            except tk.TclError:
                return

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
            self._update_tree_xscroll()
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
        """记录按下的行：仅单元格区域可发起拖动排序（列分隔条排除）。

        房间号(#1) / 主播(#2) 列是「点击跳转」列：返回 "break" 阻止 ttk 类绑定
        改变选中行，避免跳转浏览器的同时连带切换下方 SC/弹幕面板（松手时仍由
        _on_tree_release → _open_tree_link 完成跳转）。拖动排序不受影响，仍由
        本方法记录的行在 B1-Motion 中完成移动。
        """
        self._drag_iid = None
        self._drag_moved = False
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col == "#0":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._drag_iid = iid
        if col in ("#1", "#2"):  # 房间号 / 主播：点击跳转，不切换选中直播间
            return "break"

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

    def _on_overlay_toggled(self) -> None:
        self.ui_prefs["notify_overlay"] = bool(self.notify_overlay_var.get())
        self._save_config()
        # 悬浮窗总开关关闭时，常驻选项无意义，置灰（保留其取值）
        self.notify_persist_check.configure(
            state="normal" if self.ui_prefs["notify_overlay"] else "disabled")
        logger.info("开播悬浮窗提醒已%s", "开启" if self.ui_prefs["notify_overlay"] else "关闭")

    def _on_persist_toggled(self) -> None:
        self.ui_prefs["notify_persist"] = bool(self.notify_persist_var.get())
        self._save_config()
        logger.info(
            "开播悬浮窗已设为%s",
            "常驻（需点击关闭）" if self.ui_prefs["notify_persist"] else "超时自动关闭")

    def _on_sound_selected(self, _event=None) -> None:
        self.ui_prefs["notify_sound"] = self.sound_var.get()
        self._save_config()
        self._play_notify_sound()

    # ---------- 弹幕区 ----------

    def _on_dm_toggled(self) -> None:
        visible = bool(self.dm_var.get())
        self.ui_prefs["dm_visible"] = visible
        self._save_config()
        try:
            if visible:
                self.paned.add(self.dm_frame, weight=PANE_WEIGHTS[2])
                self._clear_dm_view()
            else:
                self.paned.remove(self.dm_frame)
        except tk.TclError:
            pass  # 已处于目标状态（如启动恢复时重复 add）
        self._dm_adjust_window(visible)
        # 板块增减会打乱占比，布局完成后按默认占比重新分配
        self.root.after_idle(self._apply_default_pane_ratio)
        self._apply_dm_gate()
        self._refresh_dm_send_state()
        self._load_dm_options_for_selected()

    def _dm_adjust_window(self, visible: bool) -> None:
        """弹幕区显隐的窗口尺寸策略：最大化时挤占现有空间；窗口化时向下扩展。

        扩展量按弹幕区的实际请求高度动态计算——固定值在高 DPI 或内容较高时
        不足，会导致 PanedWindow 内容溢出覆盖底部备注/按钮行。
        注意：启动恢复时 root.geometry() 可能尚未生效（返回 1x1），
        此时回退到默认窗口尺寸计算。
        """
        try:
            if self.root.state() == "zoomed":
                return  # 最大化：弹幕区自然挤占 SC 区空间，不动窗口
        except tk.TclError:
            return
        if not visible and not self._dm_grew:
            return
        self.root.update_idletasks()
        if visible:
            delta = self.dm_frame.winfo_reqheight() + 8  # 实际请求高度 + 边距
            self._dm_grew_delta = delta
        else:
            delta = -getattr(self, "_dm_grew_delta", 0)
            self._dm_grew_delta = 0
        try:
            w, h = (int(v) for v in self.root.geometry().split("+")[0].split("x"))
        except ValueError:
            w, h = 0, 0
        if w < 200 or h < 200:  # 几何尚未生效，使用默认尺寸
            w, h = getattr(self, "_default_window_size", (1000, 680))
        if h + delta < 400:  # 防止收缩得过小
            return
        parts = self.root.geometry().split("+")
        geo = f"{w}x{h + delta}"
        if len(parts) > 1:
            geo += "+" + "+".join(parts[1:])
        self.root.geometry(geo)
        self._dm_grew = visible

    def _apply_dm_gate(self) -> None:
        """按开关与当前选中房间设置各 client 的弹幕接收门控。"""
        enabled = bool(self.dm_var.get())
        selected = self._selected_room_id
        for room_id, (client, _task) in self.room_tasks.items():
            client.set_danmaku_enabled(enabled and room_id == selected)

    def _clear_dm_view(self) -> None:
        self.dm_text.configure(state="normal")
        self.dm_text.delete("1.0", "end")
        self.dm_text.configure(state="disabled")
        self._dm_unseen = 0
        self._refresh_unseen_badges()

    # ---------- 新消息浮动徽标 ----------

    def _refresh_unseen_badges(self) -> None:
        """按未读计数显示/隐藏 SC 区与弹幕区的新消息徽标。"""
        self._sync_badge(self.sc_badge, self.sc_text,
                         unseen_badge_text(self._sc_unseen, "SC"))
        self._sync_badge(self.dm_badge, self.dm_text,
                         unseen_badge_text(self._dm_unseen, "弹幕"))

    def _sync_badge(self, badge: ttk.Button, text_widget: tk.Text, label: str) -> None:
        if not label:
            badge.place_forget()
            return
        badge.configure(text=label)
        badge.place(in_=text_widget, relx=1.0, rely=1.0, anchor="se", x=-12, y=-6)
        badge.lift()

    def _sync_unseen_from_scroll(self) -> None:
        """用户把滚动条拉回底部后，清零未读计数并隐藏徽标（复用 100ms 轮询）。"""
        changed = False
        if self._sc_unseen and text_scrolled_to_bottom(self.sc_text.yview()):
            self._sc_unseen = 0
            changed = True
        if self._dm_unseen and text_scrolled_to_bottom(self.dm_text.yview()):
            self._dm_unseen = 0
            changed = True
        if changed:
            self._refresh_unseen_badges()

    def _on_sc_badge_clicked(self) -> None:
        self._sc_unseen = 0
        self.sc_text.see("end")
        self._refresh_unseen_badges()

    def _on_dm_badge_clicked(self) -> None:
        self._dm_unseen = 0
        self.dm_text.see("end")
        self._refresh_unseen_badges()

    # ---------- 弹幕点击：用户名跳转 / 正文复制 ----------

    def _on_dm_click(self, event) -> None:
        """点击弹幕：命中用户名 → 跳转其个人空间；命中正文 → 复制该条弹幕内容。"""
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
            return  # 时间、用户名等非正文区域不触发复制
        if text.get(index, f"{index}+1c") in ("", "\n"):
            return  # 点在正文右侧的空白/行尾换行处：没有文字可复制，不触发
        content = ""
        for tag in tags:
            if tag.startswith("dmbody:"):
                meta = self._dm_meta.get(tag.split(":", 1)[1])
                if meta:
                    content = str(meta[2] or "")
                break
        if not content:
            # 无 dmid（拿不到元数据）时从整行文本兜底还原正文
            content = danmaku_content_from_line(
                text.get(f"{index} linestart", f"{index} lineend"))
        if content:
            self._copy_dm_content(content)

    def _copy_dm_content(self, content: str) -> None:
        """复制弹幕正文到剪贴板，并短暂显示「已复制」提示。"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self.root.update_idletasks()
        except tk.TclError:
            return
        snippet = content if len(content) <= 20 else content[:20] + "…"
        self.dm_copy_hint_var.set(f"已复制：{snippet}")
        if self._dm_copy_hint_id is not None:
            try:
                self.root.after_cancel(self._dm_copy_hint_id)
            except Exception:
                pass
        self._dm_copy_hint_id = self.root.after(DM_COPY_HINT_MS, self._clear_dm_copy_hint)

    def _clear_dm_copy_hint(self) -> None:
        self._dm_copy_hint_id = None
        self.dm_copy_hint_var.set("")

    # ---------- 发送弹幕 ----------

    def _dm_send_block_reason(self) -> str:
        """返回发送弹幕当前不可用的原因；可用时返回空串。"""
        if not self.app_config.allow_write_operations:
            return "已在 config.json 关闭写操作（allow_write_operations=false）"
        if self.hub.api is None:
            return "后台初始化中，请稍候…"
        if not self.hub.api.logged_in:
            return "未登录：请用「获取Cookie」获取已登录的 B 站 Cookie"
        if not self.hub.api.csrf:
            return "Cookie 缺少 bili_jct，请重新「获取Cookie」"
        if not self.dm_var.get():
            return "请先开启「弹幕」开关"
        if self._selected_room_id is None:
            return "请先选择一个直播间"
        return ""

    def _refresh_dm_send_state(self) -> None:
        """按 写操作开关/后台就绪/登录/csrf/选房 刷新发送控件状态与原因提示。"""
        reason = self._dm_send_block_reason()
        enabled = not reason and not self._dm_sending
        self.dm_send_entry.configure(state="normal" if enabled else "disabled")
        self.dm_send_btn.configure(state="normal" if enabled else "disabled")
        self.dm_color_box.configure(state="readonly" if enabled else "disabled")
        self.dm_mode_box.configure(state="readonly" if enabled else "disabled")
        self.dm_emoji_btn.configure(state="normal" if enabled else "disabled")
        if reason != self._dm_send_reason:
            previous = self._dm_send_reason
            self._dm_send_reason = reason
            if reason:
                self.dm_send_hint_var.set(reason)
                logger.info("发送弹幕不可用：%s", reason)
            elif previous:
                self.dm_send_hint_var.set("")

    def _update_dm_len_hint(self, _event=None) -> None:
        length = len(self.dm_send_var.get())
        self.dm_len_var.set(f"{length}/{DANMAKU_MAX_LEN}")
        self.dm_len_label.configure(
            foreground="#c62828" if length > DANMAKU_MAX_LEN else "#888888")

    def _set_dm_reply_target(self, target: Optional[dict]) -> None:
        """设置（或清除）回复/@ 目标；设置后聚焦输入框。"""
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
        self.dm_reply_bar.pack(side="top", fill="x", before=self.dm_send_row)
        self.root.after_idle(self.dm_send_entry.focus_set)

    def _clear_dm_reply_target(self) -> None:
        self._set_dm_reply_target(None)

    def _on_send_danmaku(self) -> None:
        """主线程发送入口：门控 → 本地校验 → 二次确认 → 提交后台协程。"""
        if self._dm_sending:
            return
        reason = self._dm_send_block_reason()
        if reason:
            self.dm_send_hint_var.set(reason)
            return
        room_id = self._selected_room_id
        text = self.dm_send_var.get().strip()
        guard = danmaku_send_guard(
            text,
            last_text=self._last_dm_text.get(room_id, ""),
            last_time=self._last_dm_send.get(room_id, 0.0),
            now=time.monotonic(),
        )
        if guard:
            self.dm_send_hint_var.set(guard)
            return
        target = self._dm_reply_target or {}
        anchor = self.anchor_names.get(room_id) or str(room_id)
        preview = text if len(text) <= 40 else text[:40] + "…"
        if target:
            who = "回复" if target.get("kind") == "reply" else "@"
            preview += f"（{who} {target.get('uname') or '未知用户'}）"
        if not messagebox.askyesno(
                "确认发送弹幕",
                f"直播间：{anchor}\n内容：{preview}\n\n发送后不可撤回，确定发送？"):
            return
        color = dict(self.dm_colors).get(self.dm_color_var.get(), self.dm_colors[0][1])
        mode = dict(self.dm_modes).get(self.dm_mode_var.get(), self.dm_modes[0][1])
        real_room = self._room_id_map.get(room_id, room_id)
        self._dm_sending = True
        self._refresh_dm_send_state()
        self.dm_send_hint_var.set("发送中…")
        self.hub.submit(self._async_send_danmaku(
            real_room, text, color=color, mode=mode,
            reply_mid=int(target.get("uid") or 0),
            reply_uname=str(target.get("uname") or ""),
            replay_dmid=str(target.get("dmid") or ""),
        ))

    async def _async_send_danmaku(self, room_id: int, text: str, *, color: int,
                                  mode: int, reply_mid: int, reply_uname: str,
                                  replay_dmid: str,
                                  emoticon: Optional[dict] = None) -> None:
        """在 asyncio 线程内发送弹幕/表情包，结果经 ui_queue 回主线程。"""
        api = self.hub.api
        if api is None:
            self.ui_queue.put(("dm_send_result",
                               {"ok": False, "error": "后台未就绪，发送取消"}))
            return
        info = {"room_id": room_id, "text": text, "emoticon": bool(emoticon)}
        try:
            await api.send_danmaku(room_id, text, color=color, mode=mode,
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

    def _on_dm_send_result(self, payload: dict) -> None:
        """发送结果回主线程：成功清空输入与目标并记录冷却，失败给出内联提示。"""
        self._dm_sending = False
        if payload.get("ok"):
            room_id = payload.get("room_id")
            text = str(payload.get("text") or "")
            if room_id is not None:
                self._last_dm_send[int(room_id)] = time.monotonic()
                self._last_dm_text[int(room_id)] = text
            if not payload.get("emoticon"):
                # 表情包发送不影响输入框内容，仅文字弹幕成功后清空
                self.dm_send_var.set("")
                self._update_dm_len_hint()
                self._set_dm_reply_target(None)
            self.dm_send_hint_var.set("已发送")
            logger.info("已发送%s：%s", "表情包" if payload.get("emoticon") else "弹幕", text)
        else:
            error = payload.get("error") or "发送失败"
            self.dm_send_hint_var.set(error)
            logger.warning("发送弹幕失败：%s", error)
        self._refresh_dm_send_state()

    def _remember_dm_meta(self, dmid: str, uid: int, uname: str, content: str) -> None:
        """缓存 dmid -> (uid, uname, text)，供右键「回复该弹幕 / @该用户」使用。"""
        meta = self._dm_meta
        meta[dmid] = (uid, uname, content)
        if len(meta) > DM_META_MAX:  # 简单上限，避免长时间运行时无限增长
            for key in list(meta)[:DM_META_MAX // 2]:
                meta.pop(key, None)

    def _on_dm_right_click(self, event) -> None:
        """右键弹幕：回复该弹幕（带 dmid）/ @该用户。"""
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
        menu = tk.Menu(self.root, tearoff=0)
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

    # ---------- 发送表情包（写操作） ----------

    def _on_open_emoticons(self) -> None:
        """展开/收起内嵌的表情面板（懒加载：首次展开时才向后端请求）。"""
        if self._emoticon_visible:
            self._hide_emoticon_panel()
            return
        reason = self._dm_send_block_reason()
        if reason:
            self.dm_send_hint_var.set(reason)
            return
        room_id = self._selected_room_id
        if room_id is None:
            return
        self._show_emoticon_panel()
        self._emoticon_panel_room = room_id
        packages = self._emoticons.get(room_id)
        if packages is None:
            self._render_emoticons(room_id, None, "正在获取该直播间的专属表情…")
            self.hub.submit(self._async_load_emoticons(room_id))
        else:
            self._render_emoticons(room_id, packages, "")

    def _build_emoticon_panel(self, parent) -> None:
        """构建内嵌表情面板：分页栏（按表情包分页）+ 可滚动的表情网格。

        与直播间内的表情面板一致——**一页 = 一个表情包**；包内表情较多时可在
        网格区拖动滚动条（或滚轮）查看。面板挂在发送区内部，点击「表情」键
        用 pack/pack_forget 展开或收起，不使用独立窗口。
        """
        # 分页栏：上一包 / 包名下拉（可直接跳页） / 下一包 / 页码说明 / 收起
        bar = ttk.Frame(parent)
        bar.pack(side="top", fill="x", padx=4, pady=(4, 2))
        self._emoticon_prev_btn = ttk.Button(bar, text="◀ 上一包", width=9,
                                            command=self._on_emoticon_prev_page)
        self._emoticon_prev_btn.pack(side="left")
        self._emoticon_pkg_var = tk.StringVar(value="")
        self._emoticon_pkg_box = ttk.Combobox(bar, textvariable=self._emoticon_pkg_var,
                                             state="readonly", width=18)
        self._emoticon_pkg_box.pack(side="left", padx=6)
        self._emoticon_pkg_box.bind("<<ComboboxSelected>>", self._on_emoticon_pkg_selected)
        self._emoticon_next_btn = ttk.Button(bar, text="下一包 ▶", width=9,
                                            command=self._on_emoticon_next_page)
        self._emoticon_next_btn.pack(side="left")
        self._emoticon_page_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self._emoticon_page_var,
                  foreground="#666666").pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="收起", width=6,
                   command=self._hide_emoticon_panel).pack(side="right")

        # 提示行：仅在「加载中 / 无可用表情」等需要说明时占位，正常浏览时整行收起，
        # 不再白占高度（正常态的操作提示写在面板标题里）
        self._emoticon_hint_var = tk.StringVar(value="")
        self._emoticon_hint_label = ttk.Label(
            parent, textvariable=self._emoticon_hint_var, foreground="#888888",
            wraplength=EMOTICON_PANEL_WIDTH_HINT, justify="left")

        # 表情条：高度固定为 EMOTICON_ROW_HEIGHT（单行），表情多时横向滚动；
        # 横向滚动条常显，保证面板高度不随表情数量变化
        body = ttk.Frame(parent)
        self._emoticon_body = body
        body.pack(side="top", fill="x", padx=4, pady=(0, 4))
        self._emoticon_canvas = tk.Canvas(
            body, highlightthickness=0, width=EMOTICON_PANEL_WIDTH_HINT,
            height=EMOTICON_ROW_HEIGHT)
        hbar = ttk.Scrollbar(body, orient="horizontal",
                             command=self._emoticon_canvas.xview)
        self._emoticon_canvas.configure(xscrollcommand=hbar.set)
        hbar.pack(side="bottom", fill="x")
        self._emoticon_canvas.pack(side="top", fill="both", expand=True)
        self._emoticon_grid = ttk.Frame(self._emoticon_canvas)
        self._emoticon_grid_id = self._emoticon_canvas.create_window(
            (0, 0), window=self._emoticon_grid, anchor="nw")
        self._emoticon_grid.bind(
            "<Configure>",
            lambda _e: self._emoticon_canvas.configure(
                scrollregion=self._emoticon_canvas.bbox("all")))
        self._emoticon_canvas.bind("<Configure>", self._on_emoticon_canvas_configure)
        for widget in (self._emoticon_canvas, self._emoticon_grid):
            widget.bind("<MouseWheel>", self._on_emoticon_wheel)
            widget.bind("<Escape>", lambda _e: self._hide_emoticon_panel())

    def _set_emoticon_hint(self, message: str) -> None:
        """设置面板底部提示；为空时把整行收起，避免白占面板高度。"""
        self._emoticon_hint_var.set(message)
        label = self._emoticon_hint_label
        try:
            if message and not label.winfo_manager():
                label.pack(side="bottom", fill="x", padx=4, pady=(0, 4),
                           before=self._emoticon_body)
            elif not message and label.winfo_manager():
                label.pack_forget()
        except tk.TclError:
            pass

    def _on_emoticon_canvas_configure(self, event) -> None:
        """画布尺寸变化：重新同步表情条宽度（内容窄时铺满、宽时交给横向滚动）。"""
        self._emoticon_view_width = event.width
        self._sync_emoticon_strip_width()
        self._schedule_emoticon_regrid()

    def _schedule_emoticon_regrid(self) -> None:
        """宽度变化后延迟重排（合并连续 resize，避免拖动窗口时反复重排）。"""
        if not self._emoticon_visible or not self._emoticon_button_list:
            return
        if self._emoticon_regrid_id is not None:
            return
        self._emoticon_regrid_id = self.root.after(80, self._run_emoticon_regrid)

    def _run_emoticon_regrid(self) -> None:
        self._emoticon_regrid_id = None
        if not self._emoticon_visible:
            return
        try:
            self._apply_emoticon_grid_layout()
        except tk.TclError:
            pass  # 窗口正在销毁

    def _apply_emoticon_grid_layout(self) -> None:
        """把本页表情排成**固定的一行**（面板高度因此恒定），并同步表情条宽度。

        一行放不下时不再换行、也不改变面板高度，而是横向滚动；宽窄不一的表情
        各自按自身宽度占一列，必要时用重量（weight）等分多余宽度铺满面板。
        """
        buttons = self._emoticon_button_list
        if not buttons:
            return
        count = len(buttons)
        if count != self._emoticon_weighted_columns:
            # 列数变化时同步各列 weight：本页各列取 1，并把上一页残留的列清零
            # （否则从 40 个表情切到 3 个时，多余宽度会被残留的 40 列平分）
            for index in range(max(count, self._emoticon_weighted_columns)):
                try:
                    self._emoticon_grid.columnconfigure(
                        index, weight=1 if index < count else 0)
                except (tk.TclError, AttributeError):
                    return
            self._emoticon_weighted_columns = count
        for position, (_url, button) in enumerate(buttons):
            try:
                button.grid_configure(row=0, column=position)
            except tk.TclError:
                return
        self._sync_emoticon_strip_width()
        try:
            self._emoticon_canvas.xview_moveto(0)
        except tk.TclError:
            pass

    def _sync_emoticon_strip_width(self) -> None:
        """同步表情条的宽度：内容窄时铺满视口（不留空白），宽时保持自然宽度滚动。

        自然宽度由各按钮宽度直接累加——刚 grid 完之后父容器的请求宽度要等 Tk
        的 idle 重排才会更新，直接读 ``winfo_reqwidth()`` 会拿到上一页的旧值，
        导致宽度同步失效、横向滚动范围不更新。
        """
        canvas = self._emoticon_canvas
        try:
            view = canvas.winfo_width()
            if view <= 1:
                view = self._emoticon_view_width or EMOTICON_PANEL_WIDTH_HINT
            canvas.itemconfigure(self._emoticon_grid_id,
                                 width=max(view, self._emoticon_strip_natural_width()))
        except tk.TclError:
            pass

    def _emoticon_strip_natural_width(self) -> int:
        """单行表情条的自然宽度：各列宽（按钮宽 + 两侧网格间距）之和。"""
        pad = 2 * EMOTICON_GRID_PAD
        try:
            return sum(btn.winfo_reqwidth() + pad
                       for _url, btn in self._emoticon_button_list)
        except tk.TclError:
            return 0

    def _show_emoticon_panel(self) -> None:
        """把表情面板展开到发送行上方。"""
        if self._emoticon_visible:
            return
        self._emoticon_visible = True
        self.emoticon_panel.pack(side="top", fill="x", before=self.dm_send_row)
        try:
            # 先让布局生效，随后渲染才能按真实宽度算出行列数（避免先按兜底列数闪一下）
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def _hide_emoticon_panel(self) -> None:
        """收起表情面板（保留已载入的表情包与图片缓存，再次展开无需重新请求）。"""
        if not self._emoticon_visible:
            return
        self._emoticon_visible = False
        self.emoticon_panel.pack_forget()

    async def _async_load_emoticons(self, room_id: int) -> None:
        api = self.hub.api
        if api is None:
            return
        packages = await api.get_room_emoticons(self._room_id_map.get(room_id, room_id))
        self.ui_queue.put(("emoticons", {"room_id": room_id, "packages": packages}))

    def _on_emoticons(self, payload: dict) -> None:
        room_id = int(payload.get("room_id") or 0)
        packages = payload.get("packages") or []
        self._emoticons[room_id] = packages
        if not self._emoticon_visible or self._emoticon_panel_room != room_id:
            return  # 面板已收起或已切到其他房间，仅入缓存
        self._render_emoticons(room_id, packages, "")

    def _render_emoticons(self, room_id: int, packages: Optional[List[dict]],
                          hint: str) -> None:
        """载入某房间的表情包并显示第一页。"""
        if not self._emoticon_visible:
            return
        for child in self._emoticon_grid.winfo_children():
            child.destroy()
        self._emoticon_buttons = {}
        self._emoticon_button_list = []
        self._emoticon_packages = list(packages or [])
        if not self._emoticon_packages:
            self._emoticon_pkg_box.configure(values=[])
            self._emoticon_pkg_var.set("")
            self._emoticon_page_var.set("")
            self._emoticon_prev_btn.configure(state="disabled")
            self._emoticon_next_btn.configure(state="disabled")
            self._set_emoticon_hint(
                hint or "该直播间暂无可用专属表情（需已登录，且账号在该房间有可用表情）")
            return
        self._set_emoticon_hint("")
        self._emoticon_pkg_box.configure(
            values=[self._emoticon_pkg_label(i, p)
                    for i, p in enumerate(self._emoticon_packages)])
        self._show_emoticon_page(0)

    @staticmethod
    def _emoticon_pkg_label(index: int, package: dict) -> str:
        """分页下拉里展示的包名（带序号，便于确认分页位置）。"""
        return f"{index + 1}. {package.get('name') or '表情'}"

    def _show_emoticon_page(self, index: int) -> None:
        """显示第 index 个表情包（一页 = 一个包，与直播间内面板一致）。"""
        packages = self._emoticon_packages
        if not self._emoticon_visible or not packages:
            return
        index = max(0, min(index, len(packages) - 1))
        self._emoticon_page = index
        package = packages[index]
        emoticons = package.get("emoticons") or []
        for child in self._emoticon_grid.winfo_children():
            child.destroy()
        self._emoticon_buttons = {}
        self._emoticon_button_list = []
        self._emoticon_pkg_var.set(self._emoticon_pkg_label(index, package))
        self._emoticon_page_var.set(
            f"第 {index + 1} / {len(packages)} 包 · 共 {len(emoticons)} 个表情")
        self._emoticon_prev_btn.configure(state="normal" if index > 0 else "disabled")
        self._emoticon_next_btn.configure(
            state="normal" if index < len(packages) - 1 else "disabled")
        try:
            self._emoticon_canvas.yview_moveto(0)
        except tk.TclError:
            return
        missing: List[str] = []
        self._emoticon_button_list = []
        for item in emoticons:
            url = str(item.get("url") or "")
            image = self._emoticon_images.get(url)
            btn = tk.Button(
                self._emoticon_grid, text=str(item.get("text") or url),
                width=8, height=3, relief="groove",
                wraplength=96,
                padx=EMOTICON_GRID_PAD, pady=EMOTICON_GRID_PAD,
                command=lambda it=item: self._send_emoticon(it))
            if image is not None:
                # 图片就绪：按钮按图片自然尺寸（+内边距）显示，图标更大更清楚
                btn.configure(image=image, text="", width=0, height=0)
            btn.grid(row=0, column=0, padx=EMOTICON_GRID_PAD, pady=EMOTICON_GRID_PAD)
            btn.bind("<MouseWheel>", self._on_emoticon_wheel)
            self._emoticon_button_list.append((url, btn))
            if url:
                self._emoticon_buttons[url] = btn
                if image is None and url not in self._emoticon_pending:
                    missing.append(url)
        # 单行横向排布
        self._apply_emoticon_grid_layout()
        if missing:
            self._emoticon_pending.update(missing)
            self.hub.submit(self._async_load_emoticon_images(missing))

    def _on_emoticon_prev_page(self) -> None:
        self._show_emoticon_page(self._emoticon_page - 1)

    def _on_emoticon_next_page(self) -> None:
        self._show_emoticon_page(self._emoticon_page + 1)

    def _on_emoticon_pkg_selected(self, _event=None) -> None:
        values = [str(v) for v in self._emoticon_pkg_box.cget("values")]
        try:
            index = values.index(self._emoticon_pkg_var.get())
        except ValueError:
            return
        self._show_emoticon_page(index)

    def _on_emoticon_wheel(self, event) -> None:
        """滚轮横向翻动表情条（单行展示，没有纵向可滚动内容）。

        Tk 不会把滚轮事件向上冒泡，故表情按钮上也各自绑定了同样的处理。
        """
        try:
            self._emoticon_canvas.xview_scroll(int(-event.delta / 120), "units")
        except (tk.TclError, AttributeError):
            pass

    async def _async_load_emoticon_images(self, urls: List[str]) -> None:
        """后台下载表情图片（限流 4 并发）；解码只能在主线程做（Tk 限制）。"""
        api = self.hub.api
        if api is None:
            return
        semaphore = self._emoticon_semaphore()
        await asyncio.gather(
            *(self._download_emoticon_image(api, url, semaphore) for url in urls),
            return_exceptions=True)

    async def _download_emoticon_image(self, api, url: str, semaphore) -> None:
        try:
            async with semaphore:
                async with api.session.get(url, headers=api.ws_headers()) as resp:
                    resp.raise_for_status()
                    raw = await resp.read()
        except Exception as exc:
            logger.debug("下载表情图片失败 %s: %s", url, exc)
            self.ui_queue.put(("emoticon_image", {"url": url, "data": ""}))
            return
        self.ui_queue.put(("emoticon_image", {
            "url": url, "data": base64.b64encode(raw).decode("ascii")}))

    def _emoticon_semaphore(self) -> asyncio.Semaphore:
        """在事件循环内惰性创建并发信号量（避免在 GUI 线程绑定到错误的 loop）。"""
        semaphore = getattr(self.hub, "_emoticon_sem", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(4)
            self.hub._emoticon_sem = semaphore
        return semaphore

    def _on_emoticon_image(self, payload: dict) -> None:
        url = str(payload.get("url") or "")
        data = str(payload.get("data") or "")
        self._emoticon_pending.discard(url)
        if not url or not data:
            return
        try:
            raw = tk.PhotoImage(master=self.root, data=data)
        except tk.TclError:
            logger.debug("表情图片格式不受 Tk 支持（如 WebP），保留文字按钮: %s", url)
            return
        image = self._scale_photo(raw)
        if len(self._emoticon_images) >= EMOTICON_IMAGE_CACHE_MAX:
            self._emoticon_images.clear()
        self._emoticon_images[url] = image
        btn = self._emoticon_buttons.get(url)
        try:
            if btn is not None and btn.winfo_exists():
                btn.configure(image=image, text="", width=0, height=0)
                # 按钮尺寸变了：重算列数与面板高度（延迟合并，避免逐张重排）
                self._schedule_emoticon_regrid()
        except tk.TclError:
            pass  # 按钮不可用（如已切页/切房）：仅保留图片缓存供后续复用

    @staticmethod
    def _scale_photo(image: tk.PhotoImage) -> tk.PhotoImage:
        """把表情图片缩放到显示尺寸上限内（保持比例，尽量少损失细节）。

        用 zoom(a)+subsample(b) 近似 a/b 的小数比例；单用 subsample 会把
        162×60 的直播「大表情」按最长边压成 54×20，细节全丢、糊成一团。
        """
        zoom, sub = fit_emoticon_scale(image.width(), image.height())
        if (zoom, sub) == (1, 1):
            return image
        try:
            return image.zoom(zoom, zoom).subsample(sub, sub)
        except tk.TclError:
            return image

    def _send_emoticon(self, emoticon: dict) -> None:
        """点击表情即发送（与网页端一致）：沿用写操作总开关、登录态与冷却/重复拦截。"""
        if self._dm_sending:
            return
        reason = self._dm_send_block_reason()
        if reason:
            self.dm_send_hint_var.set(reason)
            return
        room_id = self._selected_room_id
        trigger = str(emoticon.get("trigger") or emoticon.get("text") or "").strip()
        guard = danmaku_send_guard(
            trigger,
            last_text=self._last_dm_text.get(room_id, ""),
            last_time=self._last_dm_send.get(room_id, 0.0),
            now=time.monotonic(),
            max_len=None,  # 触发词由服务端定义，不受 20 字输入上限约束
        )
        if guard:
            self.dm_send_hint_var.set(guard)
            return
        color = dict(self.dm_colors).get(self.dm_color_var.get(), self.dm_colors[0][1])
        mode = dict(self.dm_modes).get(self.dm_mode_var.get(), self.dm_modes[0][1])
        real_room = self._room_id_map.get(room_id, room_id)
        self._dm_sending = True
        self._refresh_dm_send_state()
        self.dm_send_hint_var.set("发送表情中…")
        self.hub.submit(self._async_send_danmaku(
            real_room, trigger, color=color, mode=mode,
            reply_mid=0, reply_uname="", replay_dmid="", emoticon=emoticon,
        ))
        # 面板保持展开：可连续挑选多个表情（同一房间 2 秒冷却仍生效）

    def _load_dm_options_for_selected(self) -> None:
        """已登录且弹幕区可见时，拉取当前房间可用的弹幕颜色/模式（尽力而为）。"""
        room_id = self._selected_room_id
        if room_id is None or self.hub.api is None or not self.hub.api.logged_in:
            return
        if not self.dm_var.get():
            return
        self.hub.submit(self._async_load_dm_config(room_id))

    async def _async_load_dm_config(self, room_id: int) -> None:
        api = self.hub.api
        if api is None:
            return
        config = await api.get_dm_config(self._room_id_map.get(room_id, room_id))
        self.ui_queue.put(("dm_config", {"room_id": room_id, "config": config}))

    def _on_dm_config(self, payload: dict) -> None:
        """把服务端返回的颜色/模式可用项刷新到下拉框；无效时保留内置预设。"""
        if payload.get("room_id") != self._selected_room_id:
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
        """用服务端可用项刷新颜色/模式下拉（保留原选择，失效则回退首项）。

        候选完全以服务端可用项为准（可能只有 1 项）；仅查询失败时才用内置
        预设兜底（详见 select_dm_options）。
        """
        new_colors = select_dm_options(DM_COLOR_PRESETS, colors)
        self.dm_colors = list(new_colors)
        self.dm_color_box.configure(values=[name for name, _v in new_colors])
        if self.dm_color_var.get() not in {name for name, _v in new_colors}:
            self.dm_color_var.set(new_colors[0][0])
        new_modes = select_dm_options(tuple(DM_MODE_TEXTS.items()), modes)
        self.dm_modes = list(new_modes)
        self.dm_mode_box.configure(values=[name for name, _v in new_modes])
        if self.dm_mode_var.get() not in {name for name, _v in new_modes}:
            self.dm_mode_var.set(new_modes[0][0])

    def _append_dm_batch(self, batch: List[dict]) -> None:
        """批量插入当前房间的弹幕（本轮 poll 聚合一次插入，降低重排开销）。

        用户已向上翻阅历史（滚动条不在最底部）时保持视口不动，仅在原本位于
        底部时才跟随滚动到最新弹幕。
        """
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
            # dm:<dmid> 标记整条弹幕，供右键「回复该弹幕」定位；无 dmid 时退化为仅能 @
            dm_tag = f"dm:{dmid}" if dmid else ""
            if dmid:
                self._remember_dm_meta(dmid, uid, uname, content)
            # dmbody:<dmid> 标记正文段，供「点击正文复制」精确定位（点时间不触发）；
            # 行尾换行只带 dm:<dmid>（供右键回复），不带 dmbody——否则点击该行
            # 右侧的空白区域也会误判为"点在正文上"
            body_tag = f"dmbody:{dmid}" if dmid else "dmbody"
            text.insert("end", f"[{time_str[11:19] or time_str}] ",
                        f"dm_time {dm_tag}".strip())
            user_tag = f"dm_user dmuid:{uid}" if uid else "dm_user"
            text.insert("end", f"{uname}：", f"{user_tag} {dm_tag}".strip())
            text.insert("end", content, f"{dm_tag} {body_tag}".strip())
            text.insert("end", "\n", dm_tag or ())
        text.configure(state="disabled")
        if follow:
            text.see("end")
        else:
            # 用户正在向上翻阅：累加未读计数并显示「N 条新弹幕 ↓」徽标
            self._dm_unseen += len(batch)
            self._refresh_unseen_badges()

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

    # ---------- 获取浏览器 Cookie ----------

    def _on_fetch_cookie(self) -> None:
        """确认后从本机浏览器 Cookie 数据库获取 B 站 Cookie（后台线程）。"""
        if not messagebox.askyesno(
            "获取 Cookie",
            "将从本机浏览器的 Cookie 数据库中读取 bilibili.com 的 Cookie：\n\n"
            "· 支持 Edge / Chrome / Brave / Vivaldi / Opera / Firefox\n"
            "· 正在运行的浏览器优先，自动选择可用的 Cookie\n"
            "· 仅写本地 cookie.txt（不会上传），并立即应用到当前会话\n\n"
            "注意：\n"
            "· 新版浏览器运行时会独占锁定 Cookie 数据库，获取时可能需要"
            "**暂时完全退出对应浏览器**再重试\n"
            "· 新版浏览器的 App-Bound 加密 Cookie 可能无法解密\n\n是否继续？",
        ):
            return
        if self.hub.api is None:
            messagebox.showwarning("请稍候", "后台网络初始化中，请稍后再试")
            return
        self.cookie_btn.configure(state="disabled")
        logger.info("开始从本机浏览器获取 B 站 Cookie…")
        threading.Thread(target=self._fetch_cookie_worker,
                         name="fetch-cookie", daemon=True).start()

    def _fetch_cookie_worker(self) -> None:
        try:
            cookie, source, errors = get_bilibili_cookie()
        except Exception as exc:
            logger.exception("获取浏览器 Cookie 异常")
            cookie, source, errors = None, None, [f"获取过程异常：{exc}"]
        self.ui_queue.put(("cookie_result",
                           {"cookie": cookie, "source": source, "errors": errors}))

    def _on_cookie_result(self, payload: dict) -> None:
        self.cookie_btn.configure(state="normal")
        cookie = payload.get("cookie")
        source = payload.get("source")
        errors = payload.get("errors") or []
        for err in errors:
            logger.info("Cookie 获取提示：%s", err)
        if not cookie:
            detail = "\n".join(errors) if errors else "未找到可用 Cookie"
            messagebox.showerror(
                "获取 Cookie 失败",
                f"未能从本机浏览器获取到可用的 B 站 Cookie：\n\n{detail}\n\n"
                "可手动从浏览器复制 Cookie 后保存为项目根目录的 cookie.txt\n"
                "（浏览器 F12 → Network → 任选请求 → 复制 Cookie 请求头）")
            return
        try:
            Path(COOKIE_FILE_PATH).write_text(cookie, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("获取 Cookie 失败", f"写入 cookie.txt 失败：{exc}")
            return
        self.hub.submit(self._async_apply_cookie(cookie))
        logger.info("已获取 B 站 Cookie（来源：%s，长度 %d），已保存并应用到当前会话",
                    source, len(cookie))
        suffix = ("\n\n注意：\n" + "\n".join(errors)) if errors else ""
        messagebox.showinfo("获取 Cookie 成功",
                            f"来源：{source}\n已保存到 cookie.txt 并应用到当前会话。{suffix}")

    async def _async_apply_cookie(self, cookie: str) -> None:
        if self.hub.api is None:
            return
        self.hub.api.set_cookie(cookie)
        try:
            await self.hub.api.refresh_login()
        except Exception as exc:
            logger.debug("刷新登录 uid 失败: %s", exc)
        # 登录态变化后刷新发送弹幕控件可用状态
        self.ui_queue.put(("dm_state", None))

    # ---------- 选中房间与 SC 显示 ----------

    def _fit_tree_height(self, _event=None) -> None:
        """房间列表行数随窗格高度自适应（拖动分隔条/窗口变化时触发）。"""
        wrap_h = self.tree_wrap.winfo_height()
        if wrap_h <= 1:
            return
        self._update_tree_xscroll()
        try:
            import tkinter.font as tkfont
            row_h = tkfont.Font(font=self.tree.cget("font")).metrics("linespace") + 8
        except Exception:
            row_h = 28
        scroll_h = self.xscroll.winfo_height() if self.xscroll.winfo_manager() else 0
        actions_h = self.room_actions.winfo_reqheight()  # 房间操作行占用的高度
        # 扣除表头、横向滚动条与房间操作行后得到可见行数
        rows = max(1, int((wrap_h - row_h - scroll_h - actions_h - 8) // row_h))
        if rows != int(self.tree["height"]):
            self.tree.configure(height=rows)

    def _update_tree_xscroll(self) -> None:
        """横向滚动条自动显隐：仅当列总宽超出窗格宽度时显示。"""
        try:
            total = sum(self.tree.column(c, "width") for c in self._tree_columns)
            overflow = total > self.tree.winfo_width() + 2
        except tk.TclError:
            return
        shown = bool(self.xscroll.winfo_manager())
        if overflow and not shown:
            self.xscroll.pack(side="bottom", fill="x")
        elif not overflow and shown:
            self.xscroll.pack_forget()

    def _on_room_selected(self, _event=None) -> None:
        self._flush_note()
        room_id = self._get_selected_room_id()
        self._selected_room_id = room_id
        self._note_room_id = room_id
        self._refresh_buttons()
        self._clear_sc_view()
        self._clear_dm_view()
        self._apply_dm_gate()
        self._set_dm_reply_target(None)   # 回复/@ 目标属于具体房间，切房即清除
        self._hide_emoticon_panel()       # 表情面板展示的是具体房间的表情，切房即收起
        self._refresh_dm_send_state()
        # 可用颜色/样式随直播间变化（接口按 room_id 查询），切房即重新拉取
        self._load_dm_options_for_selected()
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
        segments = build_sc_segments(record["time_received"], sc,
                                     deleted=str(sc.get("id")) in deleted)
        mark = f"sc:{sc.get('id')}"
        last = len(segments) - 1
        for i, (chunk, tag) in enumerate(segments):
            tags = tag or ()
            if i < last:  # 结尾换行不打标记，删除标记可插在行尾
                tags = f"{tags} {mark}".strip()
            text.insert(at, chunk, tags or ())

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
            mark = f"sc:{sc.get('id')}"
            segments = build_sc_segments(record["time_received"], sc,
                                         deleted=str(sc.get("id")) in deleted)
            last = len(segments) - 1
            for i, (chunk, tag) in enumerate(segments):
                tags = tag or ()
                if i < last:  # 结尾换行不打标记，删除标记可插在行尾
                    tags = f"{tags} {mark}".strip()
                block.append((chunk, tags))
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
        self._sc_unseen = 0
        self._refresh_unseen_badges()

    def _mark_sc_deleted(self, sc_id) -> None:
        """实时标记已删除/退款的 SC：在原弹幕行尾追加说明并整条置灰。

        该条不在当前视图（如分页尚未加载到）时，退回为独立提示行。
        """
        tag = f"sc:{sc_id}"
        ranges = self.sc_text.tag_ranges(tag)
        if not ranges:
            self._append_info(f"SC {sc_id} 已被删除（退款）")
            return
        text = self.sc_text
        text.configure(state="normal")
        text.insert(str(ranges[-1]), "  （已删除，退款）", "del")
        text.tag_add("del", ranges[0], ranges[-1])  # 整条置灰，与历史行为一致
        text.configure(state="disabled")

    def _append_info(self, text: str) -> None:
        follow = text_scrolled_to_bottom(self.sc_text.yview())
        self.sc_text.configure(state="normal")
        self.sc_text.insert("end", text + "\n", "info")
        self.sc_text.configure(state="disabled")
        if follow:
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
        """实时追加一条 SC；用户已在向上翻阅历史时保持视口不跳动。"""
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
        if follow:
            self.sc_text.see("end")
        else:
            # 用户正在向上翻阅：累加未读计数并显示「N 条新SC ↓」徽标
            self._sc_unseen += 1
            self._refresh_unseen_badges()

    def _notify_live(self, room_id: int, title: str) -> None:
        """开播提醒：提示音 + 任务栏图标闪烁（悬浮窗由主开关另控）。"""
        logger.info("房间 %s 开播了：%s", room_id, title or "（无标题）")
        self._play_notify_sound()
        self._flash_taskbar()

    def _play_notify_sound(self) -> None:
        """按音效设置播放开播提示音（后台线程播放，不阻塞界面）。

        Beep 直接驱动扬声器，不依赖系统声音方案；MessageBeep 播放系统
        声音方案里的"星号"音（方案为"无"时无声，故仅作为显式选项提供）。
        """
        if winsound is None:
            return
        player = NOTIFY_SOUND_PLAYERS.get(
            str(self.ui_prefs.get("notify_sound", "上行双音")))
        if player is None:  # 静音
            return

        def _play() -> None:
            try:
                player(winsound)
            except Exception:
                try:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                except Exception:
                    pass

        threading.Thread(target=_play, name="notify-sound", daemon=True).start()

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

    def _notify_overlay(self, room_id: int, title: str) -> None:
        """弹右下角自绘悬浮提醒窗（不受勿扰模式影响，不抢占焦点）。

        「弹窗常驻」开启时不自动关闭，需点击才消失。
        """
        anchor = self.anchor_names.get(room_id)
        head = f"{anchor} 开播了" if anchor else f"直播间 {room_id} 开播了"
        message = title if len(title) <= 60 else title[:57] + "…"
        self.overlay.show(head, message,
                          persist=bool(self.ui_prefs.get("notify_persist", False)))

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
        self._apply_dm_gate()
        self._refresh_dm_send_state()
        self._load_dm_options_for_selected()

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
            input_room_id = payload.get("input_room_id")
            if input_room_id is not None and int(input_room_id) != int(room_id):
                # 短号场景：记录 输入房间号 -> 真实房间号，发弹幕时用真实号
                self._room_id_map[int(input_room_id)] = int(room_id)
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
            if prev_text and prev_text != "直播中" and status_text == "直播中":
                room_remind = entry is None or entry.notify_live
                if room_remind:
                    title = payload.get("title") or ""
                    self._notify_live(room_id, title)
                    # 悬浮窗提醒：全局主开关控制这一通道，房间级开关上面已判过
                    if self.ui_prefs.get("notify_overlay", True):
                        self._notify_overlay(room_id, title)
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
                    self._mark_sc_deleted(sc_id)
        elif event_type == "stopped":
            self.client_states[room_id] = "stopped"
            self._refresh_row(room_id)
        elif event_type == "occupied":
            self.client_states[room_id] = "occupied"
            self._refresh_row(room_id)
        elif event_type == "dm":
            # 弹幕：加入待渲染批次，由 _poll_queue 周期末统一插入当前房间视图
            self._dm_batch.append(payload)
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
        elif event_type == "reconnecting":
            # 细粒度重连轨迹（默认日志级别不显示）；INFO 级轨迹由 client 自身的
            # 「连接中断 / N 秒后重连」日志给出，避免重复刷屏
            logger.debug("房间 %s 连接中断，%.1f 秒后进行第 %d 次重连",
                         room_id, float(payload.get("delay") or 0),
                         int(payload.get("attempt") or 0))
        elif event_type == "reconnected":
            logger.info("房间 %s 断线后已自动重连成功（第 %d 次尝试）",
                        room_id, int(payload.get("attempt") or 0))

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
                elif kind == "cookie_result":
                    self._on_cookie_result(item[1])
                elif kind == "dm_send_result":
                    self._on_dm_send_result(item[1])
                elif kind == "dm_state":
                    # 登录态变化（如刚获取 Cookie）后刷新门控并补拉可用颜色/样式；
                    # 可用表情随登录身份变化，缓存一并失效
                    self._emoticons.clear()
                    self._refresh_dm_send_state()
                    self._load_dm_options_for_selected()
                elif kind == "dm_config":
                    self._on_dm_config(item[1])
                elif kind == "emoticons":
                    self._on_emoticons(item[1])
                elif kind == "emoticon_image":
                    self._on_emoticon_image(item[1])
        except queue.Empty:
            pass
        # 本轮所有日志行合并为一次插入，缓解拖动窗口时的卡顿
        self._append_logs(log_lines)
        # 弹幕仅渲染当前选中房间的，本轮聚合一次插入
        if self._dm_batch:
            batch, self._dm_batch = self._dm_batch, []
            selected = self._selected_room_id
            self._append_dm_batch(
                [d for d in batch if d.get("room_id") == selected])
        # 用户手动滚回底部后清除未读计数（复用同一轮询，不新增定时器）
        self._sync_unseen_from_scroll()
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
