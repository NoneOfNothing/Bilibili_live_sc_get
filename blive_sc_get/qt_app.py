"""Qt（PySide6）版图形界面渲染层。

迁移自 Tk 版的 ``gui_app.py``：业务层（``AsyncHub``、``RoomClient``、
``SCStorage``、``medal_runner``、``cookie_server``、``gui_config``、
``app_config``）与 UI 框架解耦，本模块原样复用，只替换 UI 渲染层。

线程模型：与 Tk 版一致——asyncio 核心运行在后台守护线程（``AsyncHub``），
通过线程安全队列 ``ui_queue`` 与主线程通信；主线程用 ``QTimer`` 周期轮询
队列并更新界面组件（绝不跨线程直接触碰 UI 对象）。

实现范围（与 Tk 版对等，见 QT_PORTING.md）：
- 直播间页：房间列表（添加/删除/启停/备注/排序/置顶/开播提醒）
- SC 富文本展示（历史无限加载 / 自吸底 / 删除标记 / 总数 / 未读徽标）
- 弹幕区：显示/裁剪/点击复制/@回复/表情悬浮提示/发送/表情面板（含弹幕门控）
- 粉丝牌页：持有列表 / 房间任务 / 手动+自动执行 / 顺序跟随
- 开播悬浮窗、调试日志页、获取 Cookie（浏览器数据库 + 浏览器扩展）
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import logging
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QPoint,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QPen,
    QFont,
    QFontMetrics,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProxyStyle,
    QPushButton,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .api import ApiError, BilibiliLiveAPI, describe_send_error
from .app_config import load_app_config
from .browser_cookie import get_bilibili_cookie
from .browser_rooms import is_room_being_recorded, read_room_lock_holder
from .cli import COOKIE_FILE_NAME, _pending_flush_loop, resolve_cookie
from .client import LIVE_STATUS_TEXT, RoomClient
from .cookie_server import DEFAULT_COOKIE_PORT, wait_for_extension_cookie
from .gui_app import (
    EMOTICON_REFRESH_COOLDOWN_S,
    HISTORY_PAGE_SIZE,
    MEDAL_AUTO_INTERVAL_MS,
    PANE_RATIO,
    SC_TITLE_MAX_LEN,
    build_sc_segments,
    parse_add_input,
    unseen_badge_text,
)
from .gui_config import (
    NOTIFY_SOUNDS,
    RoomEntry,
    load_emoticon_memory,
    load_room_entries,
    load_ui_prefs,
    save_room_entries,
)
from .medal_tasks import (
    TASK_LIKE,
    auto_task_types,
    find_task,
    is_task_complete,
    task_label,
)
from .medal_runner import MedalTaskRunner
from .qt_overlay import QtToastOverlayManager
from .storage import SCStorage
from .log_setup import (
    CATEGORY_APP,
    CATEGORY_DATA,
    CATEGORY_LIVE,
    CATEGORY_ROOM,
    CATEGORY_TASK,
    CATEGORY_WINDOW,
    DebouncedValueLogger,
    attach,
    configure,
    describe,
    get_logger,
    install_file_handler,
    make_formatter,
)

logger = logging.getLogger("gui_qt")

# 按区块分类的 logger：每个区块可在 config.json 的 logging.categories 里单独关闭
log_room = get_logger(CATEGORY_ROOM, "gui_qt.room")
log_window = get_logger(CATEGORY_WINDOW, "gui_qt.window")
log_data = get_logger(CATEGORY_DATA, "gui_qt.data")
log_task = get_logger(CATEGORY_TASK, "gui_qt.task")
log_live = get_logger(CATEGORY_LIVE, "gui_qt.live")
log_app = get_logger(CATEGORY_APP, "gui_qt.app")

COOKIE_FILE_PATH = Path(__file__).resolve().parent.parent / COOKIE_FILE_NAME

DEBUG_LOG_MAX_LINES = 4000

SC_PRICE_COLORS = {
    "time": "#888888",
    "user": "#0055cc",
    "del": "#aaaaaa",
    "info": "#888888",
    "price_0": "#1f1f1f",
    "price_50": "#b8860b",
    "price_100": "#cc4444",
    "price_500": "#9932cc",
}

# QTextCharFormat 自定义属性键（存到 fragment 上，供删除定位/uid 点击）
_SC_ID_KEY = 0x101
_UID_KEY = 0x102

QUEUE_POLL_MS = 100
SC_AT_BOTTOM_TOLERANCE = 4
"""滚动条距底部多少像素以内视为“吸底”。"""

TABLE_ROW_PADDING = 6
"""表格行高 = 字体行高 + 该内边距。

Qt 默认的表格行内边距约 18px（实测 30px 行高 / 12px 字体行高），中文列表看起来
很空；这里按字体行高加少量内边距（比 Tk 版 Treeview 的 +8 再紧一点）。
"""

COMPACT_STYLE = """
QPushButton { padding: 1px 8px; }
QComboBox { padding: 1px 4px; }
QLineEdit { padding: 1px 4px; }
QCheckBox { padding: 0px; }
"""
"""紧凑控件样式。

Qt 控件的默认内边距比 Tk（ttk）大一截，几行工具栏累积起来会吃掉大量垂直空间，
使同一窗口高度能显示的行数明显少于 Tk 版（实测 150% 缩放下按钮/下拉普遍比 Tk 高
约 1/3）。这里统一收紧内边距，只影响本窗口。

**注意**：不要在这里写 ``QTabBar::tab`` 规则——一旦样式表涉及页签，Qt 会放弃
Windows 原生页签绘制而改用样式表风格，选中页与未选中页几乎看不出区别（表现为
「像是当前页**左侧**那一页被按下」）。页签外观交给系统原生绘制。
"""

MAX_WIDGET_SIZE = 16777215
"""Qt 的「不限制最大尺寸」取值（即 C++ 的 ``QWIDGETSIZE_MAX``；PySide6 未导出该宏）。"""

TABLE_MAX_VISIBLE_ROWS = 7
"""房间列表**初始**最多撑到几行（再多就靠滚动条）。

高度按行数收缩（见 ``_table_preferred_height``），但**不该无限长高**：房间多时把
SC / 弹幕区挤成一条缝并不好用。实测 6~7 行足够扫一眼房间状态，剩下的空间留给
下方内容区（Tk 版的窗格高度也大致只放得下 6~7 行，两版观感一致）。

注意这只是**初始分配**用的上限：列表高度由分隔条决定，随时可以拖动改变（可压到
一两行，也可以拖大）。早期版本用 ``setFixedHeight`` 设表格高度，那会连带把面板的
**最小**高度一起钉死，用户就再也拖不小了（实测被卡在 7 行）。
"""

MIN_WINDOW_SIZE = (880, 560)
"""最小窗口尺寸（逻辑像素）。"""

DEFAULT_WINDOW_SIZE = (MIN_WINDOW_SIZE[0], 900)
"""默认窗口尺寸（逻辑像素；未记忆过尺寸时使用，并受屏幕可用区域限制）。

宽度直接取**最小许可宽度**：三区块纵向排布，宽度再大也只是让文字行更长、并不增加
信息量，窄一点反而能让 SC / 弹幕区两侧少留空白。高度取 900：1440p（150% 缩放 ≈ 912
逻辑可用高）一屏能放下，SC / 弹幕区都不用来回滚动；更小的屏由
``_apply_window_geometry`` 的限制兜底。
"""


def compact_row_height(widget) -> int:
    """按控件字体算出的紧凑行高（随 DPI 缩放自动变化）。"""
    return QFontMetrics(widget.font()).height() + TABLE_ROW_PADDING


def move_items(items, src_rows, insert_at):
    """把 ``src_rows`` 处的元素整体搬到 ``insert_at`` **之前**（插入语义，纯函数）。

    ``insert_at`` 用**移动前**的下标表示「想插到哪个位置」——被拖元素自身先被抽走，
    所以计算时要把落在它前面的源行数扣掉，否则往下拖会少挪一位。
    """
    picked = set(src_rows)
    moving = [items[row] for row in sorted(picked) if 0 <= row < len(items)]
    rest = [item for row, item in enumerate(items) if row not in picked]
    removed_before = sum(1 for row in picked if row < insert_at)
    pos = max(0, min(len(rest), insert_at - removed_before))
    return rest[:pos] + moving + rest[pos:]


DROP_EDGE_PX = 2
"""落点判定的边缘宽度（与 Qt ``dropIndicatorPosition`` 的 ``Above``/``Below`` 阈值一致）。"""


def drop_insert_row(table, y: int) -> int:
    """按落点纵坐标 ``y`` 算「插到第几行之前」（纯函数，便于离线测试）。

    与拖动提示线的位置**完全一致**：距该行上边界 ``DROP_EDGE_PX`` 内 → 插到该行之前，
    距下边界同宽度内 → 插到该行之后；落在行中间（Qt 判 ``OnItem``，提示线画在该行顶边）
    → 同样插到该行之前。落点不在任何行上（列表下方空白）→ 插到末尾。
    """
    row = table.rowAt(int(y))
    if row < 0:
        return table.rowCount()
    rect = table.visualRect(table.model().index(row, 0))
    if y <= rect.top() + DROP_EDGE_PX:
        return row
    if y >= rect.bottom() - DROP_EDGE_PX:
        return row + 1
    return row


DROP_LINE_COLOR = "#2ecc71"
"""拖动排序时「落点插入线」的颜色（与主路高亮同色系）。"""


class DropLineStyle(QProxyStyle):
    """把拖动落点提示画成**行间一条线**（Qt 默认在「落在行上」时会把整行框起来）。

    Qt 的落点判定 ``dropIndicatorPosition`` 只把「距该行上/下边界 2px 以内」算作
    ``AboveItem`` / ``BelowItem``（画线），落在行中间一律算 ``OnItem``（画方框——观感上像
    选中了那一行，用户反馈「提示应该是行之间」）。而 ``OnItem`` 的实际插入位置正是**该行
    之前**，把线画在该行顶部与之完全一致，所以这里**只改绘制、不动拖放行为**。
    """

    def drawPrimitive(self, element, option, painter, widget=None) -> None:  # noqa: N802
        if element == QStyle.PrimitiveElement.PE_IndicatorItemViewItemDrop:
            rect = option.rect
            if rect.isValid() and rect.width() > 0:
                pen = QPen(QColor(DROP_LINE_COLOR))
                pen.setWidth(2)
                painter.save()
                painter.setPen(pen)
                # 线画在矩形**顶边**：Above/Below 时 Qt 给的是零高度的线位置、OnItem 时给的是
                # 整行矩形，两种情况取 top() 的语义一致（都表示「插到这一行之前」）。
                painter.drawLine(rect.left(), rect.top(), rect.right(), rect.top())
                painter.restore()
                return
        super().drawPrimitive(element, option, painter, widget)


class ProtectedLinkTable(QTableWidget):
    """带「点击保护」的表格：点跳转列（房间号 / 主播 / 提醒）不改变选中行。

    与 Tk 版一致：跳转列**按下时不让视图改变选中行**（Tk 是返回 ``"break"``），
    松开且未拖动到其它单元格时才发出 ``linkClicked`` 完成跳转。这样点主播名打开
    个人空间时，下方 SC / 弹幕面板不会被连带切走（粉丝牌任务表也不会顺手改掉
    「要执行任务的房间」）。

    实现要点：``selectionCommand`` 对跳转列的鼠标按下返回 ``NoUpdate`` —— 既不改变
    选中行、也不会发出 ``itemSelectionChanged``，同时**不影响行拖动排序**（基类仍然
    会记录按下项，``InternalMove`` 照常启动）。
    """

    linkClicked = Signal(int, int)  # (row, column)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.link_columns: Tuple[int, ...] = ()
        self._press_pos: Optional[QPoint] = None
        self._press_cell: Tuple[int, int] = (-1, -1)
        # 「点击不要自动滚动」（ROADMAP 74）：Qt 默认（autoScroll）会在 current 项变化时
        # scrollTo 把它**完全**显示出来——点击只露出一半的行、或点击横向被裁掉的单元格时，
        # 视图会自己跳一下。这里关掉，仅在**拖动排序 / 键盘导航**期间临时打开（见下面覆写）。
        self.setAutoScroll(False)
        # 拖动落点提示：把「框住整行」画成「行间一条线」（见 DropLineStyle；只改绘制，
        # 拖放行为仍由 Qt 负责，插入位置与线的位置本来就一致）
        self._drop_style = DropLineStyle(self.style())
        self.setStyle(self._drop_style)
        self.on_reordered = None
        """拖动结束后的回调 ``(拖动前的房间号顺序, 被拖的行号, 插入行号)``（主界面注入）。

        为什么不把落数据交给 Qt：``QTableWidget`` 内部移动用的是「覆盖目标单元格 / 清空源
        单元格」语义（``dragDropOverwriteMode`` 在它身上不生效），放下后表格数据已被弄脏。
        这里只回报「拖动前的快照 + 落点」，由主界面按**插入语义**重建行（见 :meth:`startDrag`）。
        """
        self._drag_rooms: List[int] = []
        """本次拖动开始前的完整行序（房间号）——收尾重建的唯一依据。"""
        self._drag_rows: List[int] = []
        """本次拖动被拖动的行号。"""
        self._drop_row: Optional[int] = None
        """本次落点（插到第几行之前）；``None`` = 没落到本表上。"""

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.setAutoScroll(True)   # 拖到边缘要能自动滚动
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().dragLeaveEvent(event)
        self.setAutoScroll(False)

    def room_id_at(self, row: int) -> Optional[int]:
        """读**当前显示**在第 ``row`` 行的房间号（第 0 列文本；读不到给 ``None``）。"""
        item = self.item(row, 0)
        if item is None:
            return None
        try:
            return int(item.text())
        except (TypeError, ValueError):
            return None

    def startDrag(self, supported_actions) -> None:  # noqa: N802 - Qt 命名
        """记录拖动前的行序快照；等这次拖放**彻底结束**后按插入语义重建行。

        ``super().startDrag()`` 里的 ``drag->exec()`` 是**阻塞**的（一直等到松手/取消），
        返回时 Qt 已经把它该做的都做了——包括对表格数据的改动。所以收尾放在这里最稳：
        无论 Qt 把数据糊成什么样（覆盖目标单元格、清空甚至删掉源行），我们都用
        「拖动前快照 + 落点」重建出正确结果，且发生在同一帧内（用户看不到中间态）。
        """
        self._drag_rows = sorted({index.row() for index in self.selectedIndexes()})
        self._drag_rooms = [self.room_id_at(row) for row in range(self.rowCount())]
        self._drop_row = None
        try:
            super().startDrag(supported_actions)
        finally:
            self._finish_drag()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """记录落点后交给基类；数据不靠它——重建在 :meth:`startDrag` 的收尾里做。"""
        self._drop_row = drop_insert_row(self, event.position().y())
        super().dropEvent(event)
        self.setAutoScroll(False)

    def _finish_drag(self) -> None:
        """拖动收尾：把「拖动前快照 + 落点」回报给主界面重建（幂等，且与 Qt 的落数据无关）。

        单独成方法还有个好处：**可以离线直接调它验证**——合成的 ``QDropEvent`` 交给
        ``dropEvent()`` 会让 Qt 访问违例（实测 exit 0xC0000005），不能用来做自动化测试。
        """
        self.setAutoScroll(False)
        rooms, rows, insert_at = self._drag_rooms, self._drag_rows, self._drop_row
        self._drag_rooms, self._drag_rows, self._drop_row = [], [], None
        rooms = [room for room in rooms if room is not None]
        if not rooms or not callable(self.on_reordered):
            return
        if insert_at is None:
            # 没落到本表上（拖到窗口外 / ESC 取消）：顺序照旧，但**仍要刷回快照状态**——
            # Qt 可能已经把源行清空或删掉了（clearOrRemove 只认 dragDropMode）。
            rows, insert_at = [], 0
        self.on_reordered(rooms, rows, insert_at)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.setAutoScroll(True)   # 方向键移动 current 时要跟随
        try:
            super().keyPressEvent(event)
        finally:
            self.setAutoScroll(False)

    def selectionCommand(self, index, event=None):  # noqa: N802 - Qt 命名
        # 按下与松开都要拦：Qt 在「按下未改变选中」时会在**松开时补一次选中**
        # （noSelectionOnMousePress → mouseReleaseEvent 会再查一次），只拦按下
        # 会导致保护在松手瞬间失效。
        if (event is not None
                and event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease)
                and index.isValid() and index.column() in self.link_columns):
            return QItemSelectionModel.NoUpdate
        return super().selectionCommand(index, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton:
            index = self.indexAt(event.position().toPoint())
            if index.isValid() and index.column() in self.link_columns:
                self._press_pos = event.position().toPoint()
                self._press_cell = (index.row(), index.column())
            else:
                self._press_pos = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        pos, cell = self._press_pos, self._press_cell
        self._press_pos = None
        super().mouseReleaseEvent(event)
        if pos is None or event.button() != Qt.LeftButton:
            return
        # 拖动到其它位置（改列宽 / 拖动排序）不算点击，避免误开浏览器
        moved = (event.position().toPoint() - pos).manhattanLength() > 4
        index = self.indexAt(event.position().toPoint())
        if moved or not index.isValid() or (index.row(), index.column()) != cell:
            return
        self.linkClicked.emit(cell[0], cell[1])


ROOM_INFO_REFRESH_MS = 60 * 1000
"""没有弹幕连接的房间（被其它实例占用 / 已停用监听）刷新主播名与直播标题的间隔。

这些房间收不到弹幕推送的 status 事件，只能靠只读 HTTP 查询填充列表信息。
"""

LIVE_TAG_COLORS = {
    "live": QColor("#1a7f37"),
    "offline": QColor("#555555"),
    "stopped": QColor("#c62828"),
    "disabled": QColor("#999999"),
}

SORT_MODE_TEXTS = {"manual": "手动拖动", "room": "按房间号", "anchor": "按主播名",
                   "status": "按直播状态"}


def _notify_sound_play(sound: str) -> None:
    """后台线程播放提示音（Beep/MessageBeep），静音或非 Windows 时跳过。"""
    try:  # winsound 仅 Windows 提供；缺失时静默跳过（与 Tk 版一致）
        import winsound
    except ImportError:
        return

    players = {
        "上行双音": lambda ws: (ws.Beep(880, 120), ws.Beep(1318, 200)),
        "三连音": lambda ws: (ws.Beep(988, 100), ws.Beep(1175, 100), ws.Beep(1568, 180)),
        "Windows 系统提示音": lambda ws: ws.MessageBeep(ws.MB_ICONASTERISK),
        "静音": lambda ws: None,
    }
    player = players.get(sound)
    if player is None:
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


def _flash_taskbar(hwnd: int) -> None:
    """闪烁任务栏图标（Windows FlashWindowEx），其他平台静默跳过。"""
    try:
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", wintypes.HWND),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]

        flashw_all = 0x3
        flashw_timer_nofg = 0xC
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                          flashw_all | flashw_timer_nofg, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


class _QueueLogHandler(logging.Handler):
    """把日志记录转发到 UI 队列，由主线程轮询显示（与 Tk 版同款）。"""

    def __init__(self, ui_queue: "queue.Queue"):
        super().__init__()
        self._ui_queue = ui_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ui_queue.put(("log", self.format(record)))
        except Exception:
            pass


class AsyncHub:
    """后台线程中的 asyncio 事件循环（与 Tk 版一致的复用实现）。"""

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
                self.storage.flush_all_pending()
                self.storage.close_danmaku_buffers()
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


class QtScMonitorApp(QMainWindow):
    """Qt 版主界面框架。"""

    def __init__(self, output_dir: str = "data"):
        super().__init__()
        self.output_dir = output_dir
        self.config_path = Path(output_dir) / "gui_rooms.json"
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.hub = AsyncHub(output_dir, self.ui_queue)

        self.entries: Dict[int, RoomEntry] = {
            e.room_id: e for e in load_room_entries(self.config_path)
        }
        self.ui_prefs: dict = load_ui_prefs(self.config_path)
        self.app_config = load_app_config()

        # 房间列表显示顺序（按显示顺序的房间号）
        self._room_order: List[int] = list(self.entries.keys())

        self.client_states: Dict[int, str] = {}
        self.live_state: Dict[int, str] = {}
        self.titles: Dict[int, str] = {}  # 房间号 -> 最近一次的直播标题
        self.occupiers: Dict[int, str] = {}  # 房间号 -> 占用该房间的实例信息
        self._note_room_id: Optional[int] = None  # 备注输入框当前所属房间
        self.anchor_names: Dict[int, str] = {}
        self.room_tasks: Dict[int, Tuple[RoomClient, asyncio.Task]] = {}
        self._room_id_map: Dict[int, int] = {}  # 输入房间号 -> 真实房间号（短号）
        self._selected_room_id: Optional[int] = None
        self._history_gen = 0
        self._loaded_count = 0
        self._has_more = False
        self._loading_more = False
        self._sc_total: Dict[int, int] = {}
        self.popularity: Dict[int, int] = {}
        self.guard_num: Dict[int, int] = {}
        self.viewers: Dict[int, int] = {}

        # ---- 批次2：弹幕 / 表情 / 粉丝牌 ----
        # 与 gui_config.DEFAULT_UI_PREFS 保持一致（Tk 版键名为 dm_visible）
        self.dm_var = bool(self.ui_prefs.get("dm_visible", False))
        # 弹幕/表情的具体状态都在 DmPanel 内（宿主只持有开关与轮询聚合缓冲）
        self._dm_pending: list = []  # 轮询周期内聚合的当前房间弹幕负载
        self._sc_unseen = 0
        # 房间号 -> {"index": 收起的表情包序号, "name": 包名}（持久化到 gui_rooms.json）
        self._emoticon_memory: Dict[int, dict] = load_emoticon_memory(self.config_path)
        # 房间号 -> 可用表情包（表情面板与「弹幕表情悬浮看原图」共用，见 Tk 版 self._emoticons）
        self._emoticons: Dict[int, list] = {}
        self._emoticon_fetched_at: Dict[int, float] = {}

        self._medal_running: set = set()
        self._runner: Optional[MedalTaskRunner] = None
        self._base_log_level = logging.INFO  # 由 _setup_logging 按 config.json 覆盖
        self._log_switches = None            # 同上：区块开关（configure 的返回值）
        # 高频布局事件合并成一条日志（含首末值），避免拖动时刷屏
        self._window_size_log = DebouncedValueLogger(
            log_window, delay=0.6, template="窗口尺寸 {first} → {last}")
        self._splitter_size_log = DebouncedValueLogger(
            log_window, delay=0.6, template="分隔条位置 {first} → {last}")
        self._medal_tab_refresh_ms = 90 * 1000
        self._pane_ratio_done = False  # 三板块默认占比是否已按窗口高度应用
        self.overlay = QtToastOverlayManager()

        self._setup_logging()
        log_app.info("写操作（发送弹幕等）当前为%s（config.json 的 allow_write_operations）",
                     "启用" if self.app_config.allow_write_operations else "禁用")
        log_app.info("程序启动：数据目录 %s，直播间列表 %s（%d 个房间）",
                     self.output_dir, self.config_path, len(self.entries))
        self._build_ui()
        # 窗口尺寸放在界面**建完**之后设置：早先在 _build_ui 开头调用时，后面继续添加
        # 控件会把刚设好的尺寸顶回布局 sizeHint（实测只剩 880×600），默认值与记忆值
        # 都看不出效果。
        self._apply_window_geometry()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_queue)
        self._poll_timer.start(QUEUE_POLL_MS)
        # 没有弹幕连接的房间靠只读查询刷新主播/标题（被占用、已停用时也能显示）
        self._room_info_timer = QTimer(self)
        self._room_info_timer.timeout.connect(self._room_info_tick)
        self._room_info_timer.start(ROOM_INFO_REFRESH_MS)
        self._populate_rows()
        self.hub.start()

    # ---------- 日志 ----------

    def _setup_logging(self) -> None:
        """把日志接到 UI 队列（调试页）、控制台与（可选）**轮转文件**，与 Tk 版同款。

        级别与文件输出由 ``config.json`` 的 ``logging`` 段决定（``app_config.LogConfig``）：
        文件日志用于事后监测运行状态——``pythonw`` / ``start_gui_silent.vbs`` 静默启动
        时没有控制台，只有落盘的日志可查。
        """
        fmt = make_formatter()
        root = logging.getLogger()
        self._base_log_level = self.app_config.log.level_value
        # 已有 handler（如 logging.basicConfig 建的）补上 Formatter，避免只显示 message
        for handler in root.handlers:
            if handler.formatter is None:
                handler.setFormatter(fmt)
        attach(_QueueLogHandler(self.ui_queue), formatter=fmt)
        # pythonw 启动时 stdout/stderr 为 None，只保留 GUI 内的日志显示
        if sys.stdout is not None and not any(
                isinstance(h, logging.StreamHandler) for h in root.handlers):
            attach(logging.StreamHandler(sys.stdout), formatter=fmt)
        # 根级别 + 区块开关（会给上面所有 handler 补挂区块过滤器）
        self._log_switches = configure(self.app_config.log)
        # 文件日志（logging.enabled 为真时）：带完整日期并按大小轮转，方便事后回溯
        install_file_handler(self.app_config.log, formatter=make_formatter(with_date=True))
        log_app.info("%s", describe(self.app_config.log))
        log_app.info("%s", self._log_switches.describe())

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        self.setWindowTitle("B站直播间 SC 监控（Qt）")
        self.setStyleSheet(COMPACT_STYLE)  # 收紧控件内边距，一屏多显示内容

        tabs = QTabWidget(self)
        self.setCentralWidget(tabs)
        self._tabs = tabs
        self._build_rooms_tab(tabs)
        self._build_medal_tab(tabs)
        self._build_debug_tab(tabs)

    def _apply_window_geometry(self) -> None:
        """窗口初始尺寸：优先用上次记忆的，否则取默认值并受屏幕可用区域限制。

        Tk 版按 DPI 放大默认尺寸（1000×760 × scale）；Qt 的尺寸是**逻辑像素**（高 DPI
        下会自动放大渲染），所以这里直接用逻辑像素，并按屏幕可用区域做上限，避免在
        小屏上一开窗就超出屏幕。
        """
        self.setMinimumSize(*MIN_WINDOW_SIZE)
        saved = self.ui_prefs.get("window_size")
        if (isinstance(saved, (list, tuple)) and len(saved) == 2
                and all(isinstance(value, int) and value > 200 for value in saved)):
            self.resize(int(saved[0]), int(saved[1]))
            return
        width, height = DEFAULT_WINDOW_SIZE
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, max(MIN_WINDOW_SIZE[0], int(available.width() * 0.88)))
            # 高度尽量用满可用区域（只留 12px 余量）：窗口矮会让 SC / 弹幕区频繁滚动
            height = min(height, max(MIN_WINDOW_SIZE[1], available.height() - 12))
        self.resize(width, height)

    def _build_rooms_tab(self, tabs: QTabWidget) -> None:
        from .qt_dm_panel import BottomHoldTextEdit, DmPanel

        tab = QWidget()
        tabs.addTab(tab, "直播间")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # 添加栏
        add_bar = QHBoxLayout()
        add_bar.setSpacing(4)
        add_bar.addWidget(QLabel("直播间号 / 直播间地址 / 主播主页地址："))
        self.add_edit = QLineEdit()
        self.add_edit.returnPressed.connect(self._on_add_room)
        add_bar.addWidget(self.add_edit)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._on_add_room)
        add_bar.addWidget(add_btn)
        outer.addLayout(add_bar)

        # 排序 / 通知设置栏
        sort_bar = QHBoxLayout()
        sort_bar.setSpacing(4)
        sort_bar.addWidget(QLabel("排序："))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(list(SORT_MODE_TEXTS.values()))
        self.sort_combo.setCurrentText(SORT_MODE_TEXTS.get(self.ui_prefs["sort_mode"], "手动拖动"))
        sort_bar.addWidget(self.sort_combo)
        self.sort_btn = QPushButton("排序")
        self.sort_btn.clicked.connect(self._on_sort_clicked)
        sort_bar.addWidget(self.sort_btn)
        self.pin_live_check = QCheckBox("直播中置顶")
        self.pin_live_check.setChecked(bool(self.ui_prefs.get("pin_live", False)))
        self.pin_live_check.toggled.connect(self._on_pin_live_toggled)
        sort_bar.addWidget(self.pin_live_check)
        self.overlay_check = QCheckBox("悬浮窗通知")
        self.overlay_check.setChecked(bool(self.ui_prefs.get("notify_overlay", True)))
        self.overlay_check.toggled.connect(self._on_overlay_toggled)
        sort_bar.addWidget(self.overlay_check)
        self.persist_check = QCheckBox("弹窗常驻")
        self.persist_check.setChecked(bool(self.ui_prefs.get("notify_persist", False)))
        self.persist_check.toggled.connect(self._on_persist_toggled)
        # 悬浮窗总开关关闭时常驻置灰
        self.persist_check.setEnabled(bool(self.ui_prefs.get("notify_overlay", True)))
        sort_bar.addWidget(self.persist_check)
        sort_bar.addWidget(QLabel("音效："))
        self.sound_combo = QComboBox()
        self.sound_combo.addItems(list(NOTIFY_SOUNDS))
        self.sound_combo.setCurrentText(str(self.ui_prefs.get("notify_sound", "上行双音")))
        # 选择即保存并试听（与 Tk 版 <<ComboboxSelected>> 行为一致）
        self.sound_combo.currentTextChanged.connect(self._on_sound_selected)
        sort_bar.addWidget(self.sound_combo)
        sound_preview = QPushButton("试听")
        sound_preview.clicked.connect(self._on_preview_sound)
        sort_bar.addWidget(sound_preview)
        # 直播预览（ROADMAP 63 · P1，仅 Qt 版：Tk 没有可用的视频组件）
        self.preview_btn = QPushButton("预览")
        self.preview_btn.setToolTip(
            "在独立浮窗里预览所选直播间的直播流（可缩放 / 置顶 / 拖动）。\n"
            "可同时预览多路：Ctrl 多选后点这里批量加入（上限见 config.json 的 preview.max_rooms，默认 4）。\n"
            "多路时点某一格把它设为主路——只有主路出声，其余静音；每格右下「✕」停掉该路。\n"
            "未登录只能看 720P：点底部「获取Cookie」登录后可看更高清晰度。\n"
            "默认静音；清晰度等默认值见 config.json 的 preview 段。")
        self.preview_btn.clicked.connect(self._on_preview_clicked)
        sort_bar.addWidget(self.preview_btn)
        self.refresh_preview_button()  # 初始：未选中房间时置灰
        sort_bar.addStretch(1)  # 提示靠右：窗口变窄时先压缩提示，而不是让控件挤成一团
        hint = QLabel("（拖动行排序，Ctrl 多选；双击行加入/停止预览）")
        hint.setStyleSheet("color:#888888;")
        sort_bar.addWidget(hint)
        outer.addLayout(sort_bar)

        # 三板块垂直分割（房间列表 / SC / 弹幕占位）
        splitter = QSplitter(Qt.Vertical)
        self.splitter = splitter
        splitter.splitterMoved.connect(self._on_splitter_moved)
        outer.addWidget(splitter, 1)

        # ---- 房间列表 ----
        room_panel = QWidget()
        room_layout = QVBoxLayout(room_panel)
        room_layout.setContentsMargins(0, 0, 0, 0)
        # 房间号 / 主播 / 提醒列为「跳转列」：点击不改变选中行（点击保护）
        self.table = ProtectedLinkTable(0, 6)
        self.table.link_columns = (0, 1, 3)
        # 双击房间行 = 加入 / 停止该房间的预览（工具栏按钮之外的快捷入口）
        self.table.cellDoubleClicked.connect(self._on_room_double_clicked)
        self.table.setHorizontalHeaderLabels(["房间号", "主播", "状态", "提醒", "直播标题", "备注"])
        # 列宽策略与 Tk 版一致：直播标题为弹性列（吸收多余空间、被拖宽时先让位），
        # 其余列可拖动；总列宽由 Qt 保证不超出可视宽度（弹性列让位，不会把列挤出窗口）
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(40)
        header.setStretchLastSection(False)
        # 直播标题与备注都弹性（与 Tk 版一致）：两者分摊多余宽度，避免标题独吞、
        # 备注列被挤到只剩几个字
        for col in (4, 5):
            header.setSectionResizeMode(col, QHeaderView.Stretch)
        for col, width in ((0, 96), (1, 120), (2, 84), (3, 46)):
            self.table.setColumnWidth(col, width)
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        self.table.verticalHeader().setDefaultSectionSize(compact_row_height(self.table))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._on_room_selected)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QTableWidget.InternalMove)
        # Qt 自己的落数据语义**不能用**（ROADMAP 75 的真正根因）：QTableView 上
        # dragDropOverwriteMode **默认为 true**，放下鼠标时把被拖行的数据从落点那一列开始
        # **覆盖到目标行**上（拖到行中间就从中间那列往后覆盖；只覆盖前几列、目标行其余列仍是
        # 原内容——「拖动的行盖在那一格上、连带串了后面的内容」）。即使关掉它，QTableWidget 的
        # 内部移动仍走「插入/删除 + 清空源单元格」那套，落点语义也不受我们控制。所以这里只借用
        # Qt 的**拖动交互与落点提示**，落数据一律由 ProtectedLinkTable 在拖动收尾时按「拖动前的
        # 快照 + 落点」重建（真正的插入语义）。
        self.table.setDragDropOverwriteMode(False)
        self.table.linkClicked.connect(self._on_tree_cell_clicked)
        # 右键房间行：预览该房间 / 停止预览（工具栏「预览」按钮之外的第二入口）
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_room_context_menu)
        # 拖动排序：表格只回报「拖动前的行序快照 + 落点」，由主界面按**插入语义**重建行
        # （Qt 自己的落数据是「覆盖目标单元格 / 清空源单元格」语义，不能用，见 ProtectedLinkTable）
        self.table.on_reordered = self._on_rows_reordered
        room_layout.addWidget(self.table, 1)

        # 房间操作行：备注 / 启停监听 / 删除
        actions = QHBoxLayout()
        actions.addWidget(QLabel("备注："))
        self.note_edit = QLineEdit()
        self.note_edit.editingFinished.connect(self._flush_note)
        actions.addWidget(self.note_edit, 1)
        self.toggle_btn = QPushButton("停用监听")
        self.toggle_btn.clicked.connect(self._on_toggle)
        actions.addWidget(self.toggle_btn)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.clicked.connect(self._on_delete)
        actions.addWidget(self.delete_btn)
        room_layout.addLayout(actions)
        splitter.addWidget(room_panel)

        # ---- SC 区 ----
        sc_panel = QWidget()
        sc_layout = QVBoxLayout(sc_panel)
        sc_layout.setContentsMargins(0, 0, 0, 0)
        # 头部：左侧直播标题 + 同接/舰长/粉丝牌，右侧累计 SC（同一行，省一行高度）
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        self.sc_header = QLabel("")
        self.sc_header.setWordWrap(False)  # 单行显示；过长与 Tk 版一样裁掉，完整内容见悬浮提示
        header_row.addWidget(self.sc_header, 1)
        self.sc_total_label = QLabel("")
        self.sc_total_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header_row.addWidget(self.sc_total_label, 0)
        sc_layout.addLayout(header_row)
        self.sc_text = BottomHoldTextEdit()
        self.sc_text.setReadOnly(True)
        self.sc_text.setFont(QFont("Microsoft YaHei UI", 10))
        self.sc_text.setLineWrapMode(QTextEdit.WidgetWidth)
        self.sc_text.verticalScrollBar().valueChanged.connect(self._on_sc_wheel)
        # 点击 SC 里的用户名 → 打开其个人空间（与 Tk 版 _on_sc_click 一致）
        self.sc_text.click_handler = self._on_sc_clicked
        sc_layout.addWidget(self.sc_text, 1)
        self.sc_badge = self._make_unseen_badge(self.sc_text)
        self.sc_badge.clicked.connect(self._on_sc_badge_clicked)
        self.sc_badge.hide()
        splitter.addWidget(sc_panel)

        # 弹幕区（显示 + 发送 + 表情面板）
        self.dm_panel = DmPanel(self)
        splitter.addWidget(self.dm_panel)

        # 初始占比按 Tk 的 PANE_RATIO（2 : 3.5 : 4.5）
        self._apply_pane_ratio(initial=True)

        # 底部全局操作条（弹幕区开关也放这里：与排序栏共用一行会太挤，合并到此处省一整行）
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.dm_toggle = QCheckBox("显示弹幕区")
        self.dm_toggle.setChecked(bool(self.ui_prefs.get("dm_visible", False)))
        self.dm_toggle.toggled.connect(self._on_dm_toggled)
        self.dm_toggle.setToolTip("开启后显示并保存当前选中房间的弹幕（与 Tk 版一致）")
        bottom.addWidget(self.dm_toggle)
        # 弹幕流里是否直接显示表情图片（Qt 版专有：内嵌图片更直观但更占行高）
        self.dm_emoticon_check = QCheckBox("弹幕表情图")
        self.dm_emoticon_check.setChecked(
            bool(self.ui_prefs.get("dm_emoticon_image", True)))
        self.dm_emoticon_check.toggled.connect(self._on_dm_emoticon_image_toggled)
        self.dm_emoticon_check.setToolTip(
            "开启：表情弹幕在弹幕流里直接显示图片（更直观，但行更高、占更多高度）。\n"
            "关闭：只显示「[触发词]」文字（行更紧凑），鼠标悬浮仍可看原图。")
        bottom.addWidget(self.dm_emoticon_check)
        bottom.addSpacing(10)
        refresh_btn = QPushButton("刷新历史")
        refresh_btn.clicked.connect(self._on_refresh_history)
        bottom.addWidget(refresh_btn)
        self.cookie_btn = QPushButton("获取Cookie")
        self.cookie_btn.clicked.connect(self._on_fetch_cookie)
        bottom.addWidget(self.cookie_btn)
        self.cookie_plugin_btn = QPushButton("从插件获取")
        self.cookie_plugin_btn.clicked.connect(self._on_fetch_cookie_plugin)
        bottom.addWidget(self.cookie_plugin_btn)
        bottom.addStretch(1)
        outer.addLayout(bottom)

    # ---------- 粉丝牌页 ----------

    def _build_medal_tab(self, tabs: QTabWidget) -> None:
        from .qt_medal_tab import MedalTab

        self.medal_tab = MedalTab(self)
        self._tab_medal_index = tabs.count()
        tabs.addTab(self.medal_tab, "粉丝牌")
        # 周期自动任务（Tk 版 MEDAL_AUTO_INTERVAL_MS = 5 分钟）
        self._medal_tick_timer = QTimer(self)
        self._medal_tick_timer.timeout.connect(self._medal_auto_tick)
        self._medal_tick_timer.start(MEDAL_AUTO_INTERVAL_MS)
        # 停留在「粉丝牌」页签时的自动刷新（Tk 版 MEDAL_TAB_REFRESH_MS）
        self._medal_refresh_timer = QTimer(self)
        self._medal_refresh_timer.timeout.connect(self._medal_tab_refresh_tick)
        self._medal_refresh_timer.start(self._medal_tab_refresh_ms)
        tabs.currentChanged.connect(self._on_tab_changed)

    def _medal_event(self, event_type: str, payload: dict) -> None:
        self.ui_queue.put(("medal_event", event_type, payload))

    def _on_tab_changed(self, index: int) -> None:
        """切到「粉丝牌」页签时：同步任务行、刷新自动开关，必要时自动拉一次。"""
        is_medal = index == self._tab_medal_index
        log_window.info("页签切换：%s", "「粉丝牌」页" if is_medal else f"索引 {index}")
        if not is_medal:
            return
        self.medal_tab.sync_task_rows()
        if (not self.medal_tab._medals and self.hub.ready.is_set()
                and self.hub.api is not None and self.hub.api.logged_in):
            self.medal_tab._on_refresh()

    def _medal_tab_refresh_tick(self) -> None:
        """停留期间定时刷新粉丝牌列表与各房间任务（静默，不覆盖用户可见提示）。"""
        if self._tabs.currentIndex() != self._tab_medal_index:
            return
        if not self.hub.ready.is_set() or not self.entries:
            return
        if self.hub.api is None or not self.hub.api.logged_in:
            return
        self.hub.submit(self.medal_tab._async_refresh_medals())
        self.hub.submit(self.medal_tab._async_refresh_tasks(announce=False))

    def _interrupt_auto_danmaku(self, room_id: int) -> None:
        """开播信号打断该房间正在执行的「自动发弹幕」任务（与 Tk 版一致，ROADMAP 64）。

        自动发弹幕**默认只在未开播时**执行；轮次发送期间主播开播，就不该继续刷屏，
        故收到开播信号即打断（执行器在下一个检查点停止，已发出的弹幕保留）。

        仅在**未**勾选「允许开播时自动发弹幕」时打断：勾选表示用户明确允许开播时也
        自动发（此时任务本就该继续）；**手动**「发弹幕」按钮触发的任务同样不受影响
        （执行器只打断 ``auto=True`` 的提交）。
        """
        entry = self.entries.get(room_id)
        if entry is None or entry.auto_danmaku_when_live:
            return
        try:
            runner = self.medal_tab.runner()
        except Exception:  # 执行器构造异常不应连带打断开播流程（调试页可见）
            log_task.warning("查询房间 %s 的粉丝牌执行器失败", room_id, exc_info=True)
            return
        if runner is None:
            return
        # 执行器记录的是真实房间号（短号场景与输入房间号不同）
        self.hub.submit(runner.interrupt_auto_danmaku(self.real_room_id(room_id)))

    def _try_start_auto_room(self, room_id: int) -> None:
        """粉丝牌自动开关触发：仅点赞走开播/周期，发弹幕留给 medal_auto_tick。"""
        entry = self.entries.get(room_id)
        if entry is None or not entry.enabled:
            return
        if not (self.app_config.auto_medal_tasks and self.app_config.allow_write_operations):
            return
        if self.hub.api is None or not self.hub.api.logged_in:
            return
        if not entry.auto_like:
            return
        if (self.medal_tab._medal_tasks.get(room_id) or {}).get("no_medal"):
            return  # 未持有该主播粉丝牌：无任务可做（与 Tk 版 _try_auto_like 一致）
        runner = self.medal_tab.runner()
        if runner is None or runner.is_running(room_id):
            return
        anchor_uid = entry.uid or 0
        live_status = 1 if self.live_state.get(room_id) == "直播中" else 0
        self.hub.submit(self.medal_tab._start_medal_room(
            room_id, anchor_uid, live_status, only=TASK_LIKE, manual=False))

    def _medal_auto_tick(self) -> None:
        """周期检查粉丝牌自动任务（与 Tk 版共用一个判定函数，避免两版行为漂移）。

        发弹幕 / 点赞的自动开关各自独立，但同一房间本轮该执行的项由
        ``auto_task_types`` 汇总成**一次提交**——一项一提交时，先提交的那一项会让
        房间进入「执行中」，后一项被长期挡住（发弹幕可执行时点赞永远轮不到）。
        发弹幕受「允许开播时自动发弹幕」约束（默认仅未开播时发）；点赞只在直播中
        提交，开播瞬间另有 ``_try_start_auto_room`` 即时触发。
        """
        if self.hub.api is None or not self.hub.ready.is_set():
            return
        # 与 Tk 版一致：必须同时开启「全自动总开关」与「写操作总开关」
        if not (self.app_config.auto_medal_tasks and self.app_config.allow_write_operations):
            return
        if not self.hub.api.logged_in:
            return
        runner = self.medal_tab.runner()
        if runner is None:
            return
        for room_id, entry in list(self.entries.items()):
            if not entry.enabled or runner.is_running(room_id):
                continue
            if (self.medal_tab._medal_tasks.get(room_id) or {}).get("no_medal"):
                continue  # 未持有该主播粉丝牌：无任务可做（与 _try_start_auto_room 一致）
            live_status = 1 if self.live_state.get(room_id) == "直播中" else 0
            only = auto_task_types(
                auto_danmaku=entry.auto_danmaku, auto_like=entry.auto_like,
                auto_danmaku_when_live=entry.auto_danmaku_when_live,
                live_status=live_status)
            if not only:
                log_task.debug(
                    "房间 %s 本轮无自动任务可执行（直播状态=%s；自动发弹幕=%s，"
                    "允许开播时发=%s；自动点赞=%s）",
                    room_id, live_status, entry.auto_danmaku,
                    entry.auto_danmaku_when_live, entry.auto_like)
                continue
            self.hub.submit(self.medal_tab._start_medal_room(
                room_id, entry.uid or 0, live_status, only=only, manual=False))

    def _build_debug_tab(self, tabs: QTabWidget) -> None:
        from .qt_dm_panel import BottomHoldTextEdit

        tab = QWidget()
        tabs.addTab(tab, "调试")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        # 调试日志同样支持中键快速滚动（与 Tk 版 _bind_autoscroll(debug_text) 一致）
        self.log_view = BottomHoldTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)
        self.log_level_check = QCheckBox("调试日志（DEBUG）")
        self.log_level_check.setToolTip(
            "临时把日志级别提升为 DEBUG（含所有弹幕消息类型与人气值）；"
            "取消勾选回到 config.json 里 logging.level 配置的级别。"
            "同一份日志还会按 logging 段写入文件（默认 logs/app.log）。")
        # 初始勾选状态跟随 config.json 的 logging.level（DEBUG 时即为勾选）
        self.log_level_check.setChecked(self._base_log_level <= logging.DEBUG)
        self.log_level_check.toggled.connect(self._on_debug_toggle)
        layout.addWidget(self.log_level_check)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(lambda: self.log_view.clear())
        layout.addWidget(clear_btn, 0, Qt.AlignRight)

    # ---------- 房间列表 ----------

    def _table_preferred_height(self) -> int:
        """列表期望高度：表头 + 行数 × 行高（最多 ``TABLE_MAX_VISIBLE_ROWS`` 行）+ 边框余量。

        只算「想要多高」，不直接设到控件上——高度最终由分隔条分配（见
        ``_apply_pane_ratio``），这样用户随时能拖动改变，不会被钉死。
        """
        table = getattr(self, "table", None)
        if table is None:
            return 0
        row_h = table.verticalHeader().defaultSectionSize()
        rows = max(1, min(table.rowCount(), TABLE_MAX_VISIBLE_ROWS))
        # 表头 + 行 + 边框 + 4px 余量：余量不足时最后一行会被挤出视口（出现半行）
        return (table.horizontalHeader().height() + rows * row_h
                + 2 * table.frameWidth() + 4)

    def _fit_table_height(self) -> None:
        """行数变化后重排板块高度（列表按行数收缩，多余空间留给 SC / 弹幕区）。

        仅重排**分配**，不锁死表格高度：早期用 ``setFixedHeight`` 会让面板的最小
        高度也被固定，分隔条就再也拖不动了（表现为「列表被锁定至少 7 行」）。
        """
        table = getattr(self, "table", None)
        if table is not None and (table.minimumHeight() != 0
                                  or table.maximumHeight() != MAX_WIDGET_SIZE):
            # 清掉可能残留的固定高度（旧版本设过），恢复可伸缩
            table.setMinimumHeight(0)
            table.setMaximumHeight(MAX_WIDGET_SIZE)
        self._apply_pane_ratio()

    def _populate_rows(self, *, fit_height: bool = True) -> None:
        """按 ``_room_order`` 重建全部行。

        ``fit_height=False``：只重建行、**不重排板块高度**。拖动排序后重建时必须用它——
        ``_fit_table_height`` 会走 ``_apply_pane_ratio`` 按默认占比重新分配三个板块，
        把用户拖好的分隔条位置**重置**，而拖动一行并没有改变行数，不该动布局。
        """
        self.table.setRowCount(0)
        for room_id in self._room_order:
            # 与 Tk 版一致：未启用的房间默认落到 disabled，避免配色/状态误判
            self.client_states.setdefault(
                room_id,
                "disabled" if not self.entries[room_id].enabled else "starting")
            self._insert_row(room_id)
        # 粉丝牌页任务列表跟随直播间列表顺序
        self.medal_tab.sync_task_rows()
        if fit_height:
            self._fit_table_height()

    def _insert_row(self, room_id: int) -> None:
        """插入一行。**只负责行本身**——板块高度由调用方按需重排（见 ``_populate_rows``）。

        这里不能再自己调 ``_fit_table_height()``：``_populate_rows(fit_height=False)``（拖动排序
        后重建）会逐行调它，那样等于每插一行就把用户拖好的分隔条比例重置一次。
        """
        if room_id not in self._room_order:
            self._room_order.append(room_id)
        row = self._room_order.index(room_id)
        self.table.insertRow(row)
        self._refresh_row(room_id)

    def _refresh_row(self, room_id: int) -> None:
        if room_id not in self._room_order:
            return
        row = self._room_order.index(room_id)
        if row >= self.table.rowCount():
            return
        entry = self.entries.get(room_id)
        status = self._status_text(room_id)
        color = self._row_color(room_id)
        values = [
            str(room_id),
            self.anchor_names.get(room_id) or "",
            status,
            "开" if (entry is not None and entry.notify_live) else "关",
            self.live_state_title(room_id),
            entry.note if entry else "",
        ]
        for col, text in enumerate(values):
            item = self.table.item(row, col)
            if item is None:
                item = QTableWidgetItem(text)
                self.table.setItem(row, col, item)
            else:
                item.setText(text)
            item.setForeground(color)
        # 被占用时把「谁在占用」挂在状态格提示上，避免用户只看到「被占用」无从下手
        status_item = self.table.item(row, 2)
        if status_item is not None:
            holder = self.occupiers.get(room_id)
            status_item.setToolTip(
                f"该房间已被其它实例监听：{holder}\n"
                "（同一房间不会被两个实例重复监听；关闭对应窗口/进程后重启本程序即可恢复）"
                if holder else "")
        # 提醒列（第 4 列）点击可切换该房间的开播提醒开关，见 _on_tree_cell_clicked

    def live_state_title(self, room_id: int) -> str:
        """最近一次的直播标题（与 Tk 版 tree 的 title 列一致，不是直播状态）。"""
        return self.titles.get(room_id) or ""

    def _status_text(self, room_id: int) -> str:
        """与 Tk 版 _status_text 一致：连接态优先，再取直播状态。"""
        state = self.client_states.get(room_id, "starting")
        if state == "disabled":
            return "已停用"
        if state == "starting":
            return "连接中…"
        if state == "running":
            return self.live_state.get(room_id) or "连接中…"
        if state == "occupied":
            return "被占用"
        return "已停止"

    def real_room_id(self, room_id: int) -> int:
        """输入房间号 → 真实房间号（短号场景见 _on_client_event status）。"""
        return self._room_id_map.get(room_id, room_id)

    def _row_color(self, room_id: int) -> QColor:
        state = self.client_states.get(room_id)
        if state == "disabled":
            return LIVE_TAG_COLORS["disabled"]
        if state in ("stopped", "occupied"):
            return LIVE_TAG_COLORS["stopped"]
        live = self.live_state.get(room_id)
        if live == "直播中":
            return LIVE_TAG_COLORS["live"]
        if live == "未开播":
            return LIVE_TAG_COLORS["offline"]
        return QColor("#000000")

    def _get_selected_room_ids(self) -> List[int]:
        """当前选中的房间号（按行序）。

        表格**可能还没建出来**：``_build_rooms_tab`` 创建「预览」按钮后会立刻刷新一次按钮
        状态，而那时 ``self.table`` 尚未创建（P3 多路改造时这里改成按「选中的房间」决定
        文案，于是启动阶段抛 ``AttributeError: 'QtScMonitorApp' object has no attribute
        'table'``，表现为程序一闪就退出）。这里对表格做存在性判断，没有就按「无选中」处理。
        """
        table = getattr(self, "table", None)
        if table is None:
            return []
        rows = sorted({i.row() for i in table.selectedItems()})
        return [self._room_order[r] for r in rows if 0 <= r < len(self._room_order)]

    def _get_selected_room_id(self) -> Optional[int]:
        selected = self._get_selected_room_ids()
        return selected[0] if selected else None

    def _refresh_buttons(self) -> None:
        valid = [r for r in self._get_selected_room_ids() if r in self.entries]
        has = bool(valid)
        for w in (self.toggle_btn, self.delete_btn, self.note_edit):
            w.setEnabled(has)
        if has:
            all_enabled = all(self.entries[r].enabled for r in valid)
            self.toggle_btn.setText("停用监听" if all_enabled else "开启监听")

    def _on_room_selected(self) -> None:
        self._flush_note()  # 先把备注写回它所属的房间，再切换
        room_id = self._get_selected_room_id()
        if room_id != self._selected_room_id:
            log_room.info("切换直播间：%s（%s，%s）",
                          room_id if room_id is not None else "未选择",
                          self.anchor_names.get(room_id, "未知主播") if room_id else "—",
                          self.live_state.get(room_id, "未知") if room_id else "—")
        self._refresh_buttons()
        if room_id is None:
            self._selected_room_id = None
            self._clear_sc_view()
            self._clear_dm_view()
            self._apply_dm_gate()
            self._refresh_dm_send_state()
            return
        if room_id == self._selected_room_id:
            return
        self._selected_room_id = room_id
        self._sync_note_display(room_id)
        self._update_sc_header(room_id)
        self._update_sc_total_label()
        self._load_history(room_id)
        # 弹幕区展示的是具体房间的弹幕：切房即清空并重设接收门控
        self._clear_dm_view()
        self._apply_dm_gate()
        if hasattr(self, "dm_panel"):
            self.dm_panel.on_room_selected(room_id)
        self._refresh_dm_send_state()
        # 可用颜色/样式随直播间变化（接口按 room_id 查询），切房即重新拉取
        self._load_dm_options_for_selected()
        self._follow_preview(room_id)  # 预览开着且开启「跟随选中房间」时切流

    def _clear_dm_view(self) -> None:
        if hasattr(self, "dm_panel"):
            self.dm_panel.clear_view()

    def _refresh_dm_send_state(self) -> None:
        if hasattr(self, "dm_panel"):
            self.dm_panel._refresh_dm_send_state()

    def _apply_dm_gate(self) -> None:
        """按弹幕开关与当前选中房间设置各 client 的弹幕接收门控。

        与 Tk 版 _apply_dm_gate 一致：只有「开关开启 + 该房间被选中」才会接收
        弹幕（落盘 + 广播 dm 事件），其余房间解析后直接丢弃。
        未设置时 RoomClient._dm_enabled 默认 False，弹幕会全部被丢弃。
        """
        enabled = bool(self.dm_var)
        selected = self._selected_room_id
        for room_id, (client, _task) in self.room_tasks.items():
            client.set_danmaku_enabled(enabled and room_id == selected)

    def _load_dm_options_for_selected(self) -> None:
        """已登录且弹幕区可见时，拉取当前房间可用的弹幕颜色/模式（尽力而为）。"""
        room_id = self._selected_room_id
        if room_id is None or self.hub.api is None or not self.hub.api.logged_in:
            return
        if not self.dm_var:
            return
        self.hub.submit(self._async_load_dm_config(room_id))

    async def _async_load_dm_config(self, room_id: int) -> None:
        api = self.hub.api
        if api is None:
            return
        config = await api.get_dm_config(self.real_room_id(room_id))
        self.ui_queue.put(("dm_config", {"room_id": room_id, "config": config}))

    def _sync_note_display(self, room_id: int) -> None:
        entry = self.entries.get(room_id)
        self._note_room_id = room_id
        self.note_edit.setText(entry.note if entry else "")
        self.note_edit.setCursorPosition(len(self.note_edit.text()))

    # ---- 房间行交互：点击跳转 / 拖拽排序 ----

    def _room_id_at_row(self, row: int) -> Optional[int]:
        if 0 <= row < len(self._room_order):
            return self._room_order[row]
        return None

    def _on_rows_reordered(self, rooms: List[int], src_rows: List[int],
                           insert_at: int) -> None:
        """拖动排序收尾（自管拖放）：按「拖动前的行序 + 落点」重建行并持久化。

        ``rooms``：**拖动前**的完整行序（房间号）；``src_rows``：被拖动的行号；``insert_at``：
        插到第几行之前——三者由 ``ProtectedLinkTable`` 在拖动结束时回报。之所以要这么绕，是
        因为 Qt 对 ``QTableWidget`` 的内部移动用的是「覆盖目标单元格 / 清空源单元格」语义
        （``dragDropOverwriteMode`` 在它身上不起作用），放下后表格数据已被弄脏，无法再从表格
        读顺序，只能按拖动前的快照重建。

        重建时**不重排板块高度**（``fit_height=False``）：``_fit_table_height`` 会走
        ``_apply_pane_ratio`` 按默认占比重新分配三个区块，把用户拖好的分隔条比例重置——而拖动
        一行并没有改变行数（用户反馈「拖动还会导致界面比例重置」）。
        """
        new_order = move_items(rooms, src_rows, insert_at)
        dragged = rooms[src_rows[0]] if src_rows and src_rows[0] < len(rooms) else None
        self._room_order = new_order
        self._save_config()
        log_room.info("拖动排序完成，新顺序：%s", "、".join(str(r) for r in new_order))
        selected = dragged if dragged is not None else self._get_selected_room_id()
        # 重建行期间屏蔽选中信号：清空 → 重填 → 重新选中会连着触发几次「切换直播间」，把
        # SC / 弹幕面板整段刷掉（拖动一行不该换房间）。
        self.table.blockSignals(True)
        try:
            self._populate_rows(fit_height=False)
        finally:
            self.table.blockSignals(False)
        if selected is not None and selected in new_order:
            self.table.selectRow(new_order.index(selected))
        if self._get_selected_room_id() != self._selected_room_id:
            # 选中确实变了（信号被屏蔽时丢掉了触发）才补一次；没变说明面板本来就对，
            # 不必重复拉弹幕选项、重复切预览
            self._on_room_selected()

    def _open_room_link(self, room_id: int, col: int) -> None:
        """房间列→直播间地址；主播列→主播主页。"""
        url = f"https://live.bilibili.com/{room_id}"
        if col == 1:  # 主播列 → 主播主页
            entry = self.entries.get(room_id)
            uid = entry.uid if entry else 0
            if uid and int(uid) > 0:
                url = f"https://space.bilibili.com/{uid}"
            else:
                self._async_fetch_uid(room_id)
                return
        log_room.info("点击跳转：房间 %s 的%s", room_id, "主播主页" if col == 1 else "直播间")
        webbrowser.open(url)

    def _async_fetch_uid(self, room_id: int) -> None:
        """补齐某房间的主播/标题信息（点击「主播」列或没有连接时使用）。"""
        if not self.hub.ready.is_set():
            return
        self.hub.submit(self._async_fetch_room_info(room_id))

    async def _async_fetch_room_info(self, room_id: int) -> None:
        """只读查询房间信息（主播 uid/昵称/直播标题/直播状态）。

        被其它实例占用、或已停用监听的房间不会建立弹幕连接，因此收不到 status
        事件；这里补一次 HTTP 查询，让列表仍能显示主播名与直播标题。
        """
        api = self.hub.api
        if api is None:
            return
        try:
            info = await api.get_full_room_info(room_id)
            uid = int(info.get("uid") or 0)
            anchor = await api.get_anchor_name(uid) if uid else ""
        except Exception as exc:
            log_data.debug("获取房间 %s 信息失败: %s", room_id, exc)
            return
        self.ui_queue.put(("room_info", {
            "room_id": room_id,
            "uid": uid,
            "anchor_name": anchor,
            "title": info.get("title") or "",
            "live_status": int(info.get("live_status") or 0),
        }))

    def _room_info_tick(self) -> None:
        """周期为「没有弹幕连接」的房间刷新主播名与直播标题。"""
        if not self.hub.ready.is_set() or self.hub.api is None:
            return
        for room_id in self._rooms_without_client():
            self.hub.submit(self._async_fetch_room_info(room_id))

    def _rooms_without_client(self) -> List[int]:
        """当前没有本程序弹幕连接的房间号（被占用 / 已停用 / 已停止）。"""
        active = {rid for rid, (_client, task) in self.room_tasks.items()
                  if not task.done()}
        return [rid for rid in self.entries if rid not in active]

    def _on_room_info(self, payload: dict) -> None:
        """只读房间信息到达：补主播名/直播标题，并在没有连接时采用其直播状态。

        有弹幕连接时直播状态以弹幕推送为准（客户端对「关播瞬间接口缓存
        旧状态」有专门处理），故此时不覆盖。
        """
        room_id = int(payload.get("room_id") or 0)
        entry = self.entries.get(room_id)
        if entry is None:
            return
        uid = int(payload.get("uid") or 0)
        if uid and entry.uid != uid:
            entry.uid = uid
            self._save_config()
        anchor = str(payload.get("anchor_name") or "")
        if anchor:
            self.anchor_names[room_id] = anchor
        self.titles[room_id] = str(payload.get("title") or "")
        if self.client_states.get(room_id) not in ("running", "starting"):
            self.live_state[room_id] = LIVE_STATUS_TEXT.get(
                int(payload.get("live_status") or 0), "未知")
        self._refresh_row(room_id)
        if room_id == self._selected_room_id:
            self._update_sc_header(room_id)

    def _on_tree_cell_clicked(self, row: int, col: int) -> None:
        """房间号/主播列 → 跳浏览器；提醒列 → 切换该房间的开播提醒开关。

        只由 ``ProtectedLinkTable.linkClicked`` 触发：按下时不改变选中行（点击保护），
        松开且未拖动才走到这里，因此不会连带切换下方 SC / 弹幕面板。
        """
        room_id = self._room_id_at_row(row)
        if room_id is None:
            return
        if col in (0, 1):
            self._open_room_link(room_id, col)
        elif col == 3:
            entry = self.entries.get(room_id)
            if entry is not None:
                entry.notify_live = not entry.notify_live
                self._save_config()
                self._refresh_row(room_id)

    def _on_add_room(self) -> None:
        raw = self.add_edit.text().strip()
        if not raw:
            return
        try:
            room_id, uid = parse_add_input(raw)
        except Exception:
            log_room.warning("添加直播间失败：无法从 %r 解析出房间号", raw)
            QMessageBox.critical(self, "添加失败", f"无法从输入中解析出房间号：{raw}")
            return
        self.add_edit.clear()
        log_room.info("添加直播间：输入 %r → 房间 %s，uid %s", raw, room_id, uid or "未知")
        if room_id is not None and room_id in self.entries:
            QMessageBox.information(self, "已存在", f"房间 {room_id} 已在列表中")
            return
        if not self.hub.ready.is_set():
            QMessageBox.warning(self, "请稍候", "后台网络初始化中，请稍后再试")
            return
        self.hub.submit(self._async_add_room(room_id, uid))

    async def _async_add_room(self, room_id: Optional[int],
                              uid: Optional[int] = None) -> None:
        try:
            if uid is not None:
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
            log_room.warning("添加直播间 %s 失败：%s", room_id, payload.get("error"))
            QMessageBox.critical(self, "添加失败",
                                 f"房间 {room_id} 添加失败：\n{payload.get('error')}")
            return
        if room_id in self.entries:
            QMessageBox.information(self, "已存在", f"房间 {room_id} 已在列表中")
            return
        self.entries[room_id] = RoomEntry(room_id=room_id, enabled=True,
                                          uid=int(payload.get("uid") or 0))
        self.client_states[room_id] = "starting"
        self.live_state[room_id] = LIVE_STATUS_TEXT.get(int(payload.get("live_status") or 0), "未知")
        self.titles[room_id] = payload.get("title") or ""
        if payload.get("anchor_name"):
            self.anchor_names[room_id] = payload["anchor_name"]
        self._save_config()
        self._insert_row(room_id)
        self._fit_table_height()         # 多了一行：重排板块高度（_insert_row 本身不再做）
        self.medal_tab.sync_task_rows()  # 粉丝牌页新增行并保持顺序
        self._select_room(room_id)
        uid = int(payload.get("uid") or 0)
        anchor = self.anchor_names.get(room_id) or "未知"
        log_room.info("已添加直播间 %s（%s，uid=%s）", room_id, anchor, uid or "未知")
        QMessageBox.information(self, "添加成功",
                                f"房间 {room_id} 已加入监听\n主播：{anchor}（uid：{uid or '未知'}）")
        self.hub.submit(self._start_room(room_id))

    def _select_room(self, room_id: int) -> None:
        if room_id in self._room_order:
            row = self._room_order.index(room_id)
            self.table.selectRow(row)
            self._on_room_selected()

    def _on_toggle(self) -> None:
        valid = [r for r in self._get_selected_room_ids() if r in self.entries]
        if not valid:
            return
        all_enabled = all(self.entries[r].enabled for r in valid)
        for room_id in valid:
            if all_enabled:
                self.entries[room_id].enabled = False
                self.client_states[room_id] = "disabled"
                self.hub.submit(self._stop_room(room_id))
            else:
                self.entries[room_id].enabled = True
                self.client_states[room_id] = "starting"
                self.occupiers.pop(room_id, None)  # 重新尝试监听，清掉旧的占用提示
                if self.hub.ready.is_set():
                    self.hub.submit(self._start_room(room_id))
        self._save_config()
        for room_id in valid:
            self._refresh_row(room_id)
        self._refresh_buttons()
        self._apply_dm_gate()
        log_room.info("%s监听 %d 个房间：%s", "停用" if all_enabled else "启用",
                      len(valid), "、".join(str(r) for r in valid))

    def _on_delete(self) -> None:
        valid = [r for r in self._get_selected_room_ids() if r in self.entries]
        if not valid:
            return
        if not QMessageBox.question(
                self, "删除直播间",
                f"停止监听选中的 {len(valid)} 个直播间并从列表移除？\n"
                f"（房间号：{'、'.join(str(r) for r in valid)}）\n"
                "已保存的 SC 数据会保留在磁盘",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            return
        log_room.info("删除直播间 %d 个：%s", len(valid), "、".join(str(r) for r in valid))
        for room_id in valid:
            if self.entries[room_id].enabled:
                self.hub.submit(self._stop_room(room_id))
            self.entries.pop(room_id, None)
            self.client_states.pop(room_id, None)
            self.live_state.pop(room_id, None)
            self.titles.pop(room_id, None)
            self.occupiers.pop(room_id, None)
            self.anchor_names.pop(room_id, None)
            self._sc_total.pop(room_id, None)
            self.viewers.pop(room_id, None)
            self.guard_num.pop(room_id, None)
            self._room_id_map.pop(room_id, None)
            # 表情包记忆与缓存随房间一并清理
            self._emoticon_memory.pop(room_id, None)
            self._emoticons.pop(room_id, None)
            self._emoticon_fetched_at.pop(room_id, None)
            if room_id in self._room_order:
                self.table.removeRow(self._room_order.index(room_id))
                self._room_order.remove(room_id)
        self._save_config()
        self.medal_tab.sync_task_rows()  # 粉丝牌页同步移除行
        self._fit_table_height()         # 行数减少：列表高度随之收缩
        self._on_room_selected()
        self._apply_dm_gate()

    def _on_refresh_history(self) -> None:
        room_id = self._get_selected_room_id()
        if room_id is not None:
            log_data.info("手动刷新历史 SC（房间 %s）", room_id)
            self._load_history(room_id)

    def _flush_note(self) -> None:
        """把备注写回它所属的房间。

        必须用「备注输入框当前展示的房间」而不是「当前选中房间」——失焦/切换行时
        选中项可能已经变了，否则会把 A 房间的备注写到 B 房间（同 Tk 版 _note_room_id）。
        """
        room_id = self._note_room_id
        if room_id is None or room_id not in self.entries:
            return
        text = self.note_edit.text().strip()
        if text == self.entries[room_id].note:
            return
        self.entries[room_id].note = text
        self._save_config()
        log_room.info("房间 %s 备注已保存：%s", room_id, text or "（清空）")
        self._refresh_row(room_id)

    def _on_sort_clicked(self) -> None:
        text = self.sort_combo.currentText()
        mode = next((k for k, v in SORT_MODE_TEXTS.items() if v == text), "manual")
        self.ui_prefs["sort_mode"] = mode
        self._save_config()
        self._apply_sort()
        log_room.info("手动排序：方式 %s（显示 %s）", mode, text)

    def _apply_sort(self) -> None:
        mode = self.ui_prefs.get("sort_mode", "manual")
        ids = [r for r in self._room_order if r in self.entries]
        if mode == "room":
            ids.sort()
        elif mode == "anchor":
            ids.sort(key=lambda r: self.anchor_names.get(r, ""))
        elif mode == "status":
            # 与 Tk 版一致：直播中 → 轮播中 → 其余（同档内按房间号）
            rank = {"直播中": 0, "轮播中": 1}
            ids.sort(key=lambda r: (rank.get(self.live_state.get(r) or "", 2), r))
        # manual 保持当前顺序
        self._room_order = ids
        self._populate_rows()
        if self._selected_room_id and self._selected_room_id in self._room_order:
            self._select_room(self._selected_room_id)

    def _on_pin_live_toggled(self, checked: bool) -> None:
        self.ui_prefs["pin_live"] = checked
        self._save_config()
        log_room.info("「直播中置顶」：%s", "开" if checked else "关")
        if checked:
            ids = [r for r in self._room_order if r in self.entries]
            current = self._selected_room_id
            ids.sort(key=lambda r: (self.live_state.get(r) != "直播中",
                                    self._room_order.index(r)))
            self._room_order = ids
            self._populate_rows()
            if current and current in self._room_order:
                self._select_room(current)

    def _on_overlay_toggled(self, checked: bool) -> None:
        self.ui_prefs["notify_overlay"] = checked
        self._save_config()
        self.persist_check.setEnabled(checked)

    def _on_persist_toggled(self, checked: bool) -> None:
        self.ui_prefs["notify_persist"] = checked
        self._save_config()

    def _on_dm_toggled(self, checked: bool) -> None:
        """常驻弹幕开关：显隐弹幕面板、重设接收门控并落盘偏好。"""
        self.dm_var = bool(checked)
        self.ui_prefs["dm_visible"] = self.dm_var
        if hasattr(self, "dm_panel"):
            self.dm_panel.setVisible(self.dm_var)
        self._save_config()
        log_window.info("弹幕区%s", "显示" if self.dm_var else "隐藏")
        self._apply_dm_gate()

    def _on_dm_emoticon_image_toggled(self, checked: bool) -> None:
        """弹幕表情图开关：内嵌图片／只显示触发词（立即生效并落盘偏好）。

        关闭时会把弹幕里**已经显示**的表情图还原成「[触发词]」文字，所以行高立刻降下来，
        不必等新弹幕；重新开启只影响后续新弹幕（历史行保持文字）。
        """
        self.ui_prefs["dm_emoticon_image"] = bool(checked)
        if hasattr(self, "dm_panel"):
            self.dm_panel.set_emoticon_image_enabled(bool(checked))
        self._save_config()
        log_window.info("弹幕表情图：%s", "显示" if checked else "只显示触发词")
        self._refresh_dm_send_state()
        self._load_dm_options_for_selected()
        # 板块增减会打乱占比，重新按默认占比分配（与 Tk 版 _on_dm_toggled 一致）
        self._apply_pane_ratio()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """首次显示后按 PANE_RATIO 分配板块高度（此前分割器还没有真实高度）。"""
        super().showEvent(event)
        if not self._pane_ratio_done:
            self._pane_ratio_done = True
            self._apply_pane_ratio()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """窗口尺寸变化：由节流器合并成一条日志（与 Tk 版 _on_root_configure 一致）。"""
        super().resizeEvent(event)
        self._fit_table_height()  # 窗口变矮/变高时重新约束列表高度
        debounced = getattr(self, "_window_size_log", None)
        if debounced is not None:
            size = self.size()
            debounced.note(f"{size.width()}x{size.height()}")

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        """拖动分隔条：合并成一条日志（含首末各板块尺寸）。"""
        sizes = getattr(self, "splitter", None)
        if sizes is None:
            return
        self._splitter_size_log.note("、".join(str(s) for s in sizes.sizes()))

    def _room_pane_preferred_height(self, panel) -> int:
        """房间面板的期望高度 = 列表期望高度 + 面板内其它控件（备注框 / 按钮行）。

        表格以外的部分直接用 ``sizeHint`` 差值推出来，免得再维护一份间距常量。
        """
        table = getattr(self, "table", None)
        if table is None:
            return panel.sizeHint().height()
        others = panel.sizeHint().height() - table.sizeHint().height()
        return self._table_preferred_height() + max(0, others)

    def _apply_pane_ratio(self, *, initial: bool = False) -> None:
        """分配三个板块的高度。

        房间列表按**内容高度**（表格 + 操作行）收缩，剩余空间再按 Tk 的 PANE_RATIO
        余项（3.5 : 4.5）分给 SC / 弹幕区——房间少时列表区不会白出一大片；弹幕区
        隐藏时只剩两块，剩余空间全部给 SC 区。

        这里只是**初始分配**：列表高度由分隔条决定，用户随时可以拖动改变（含压到
        一两行），不会被锁死。
        """
        splitter = getattr(self, "splitter", None)
        if splitter is None:
            return
        panes = [splitter.widget(i) for i in range(splitter.count())]
        visible = [w for w in panes if not w.isHidden()]
        if len(visible) < 2:
            return
        total = max(self.height(), 400) if initial else splitter.height()
        if total <= 1:
            total = 680
        ratios = PANE_RATIO[:len(visible)]
        span = sum(ratios)
        room_h = min(self._room_pane_preferred_height(visible[0]),
                     max(120, int(total * ratios[0] / span)))
        rest = max(0, total - room_h)
        rest_ratios = ratios[1:]
        rest_span = sum(rest_ratios) or 1
        sizes: List[int] = []
        index = 0
        for widget in panes:
            if widget.isHidden():
                sizes.append(0)
                continue
            if index == 0:
                sizes.append(room_h)
            else:
                sizes.append(max(1, int(rest * rest_ratios[index - 1] / rest_span)))
            index += 1
        splitter.setSizes(sizes)

    def _on_preview_sound(self) -> None:
        """试听当前下拉选中的音效（未保存时也应播放所选的那个）。"""
        _notify_sound_play(self.sound_combo.currentText())

    def _on_sound_selected(self, text: str) -> None:
        """音效选择即保存并试听（与 Tk 版 _on_sound_selected 一致）。"""
        if not text:
            return
        self.ui_prefs["notify_sound"] = str(text)
        self._save_config()
        _notify_sound_play(str(text))

    # ---------- 后台房间任务 ----------

    async def _start_room(self, room_id: int) -> None:
        if self.hub.api is None or self.hub.storage is None:
            return
        if is_room_being_recorded(self.output_dir, room_id):
            # 其它实例/CLI 已占用该房间：不重复监听（与 Tk 版一致）。
            # 一并读出锁文件里的持有者信息，便于用户判断是哪个实例在监听
            holder = read_room_lock_holder(self.output_dir, room_id)
            log_live.warning("房间 %s 已被其它实例监听%s，本程序不重复监听"
                           "（关闭对应窗口/进程后重启即可恢复）",
                           room_id, f"（持有者 {holder}）" if holder else "")
            self.ui_queue.put(("client", "occupied",
                               {"room_id": room_id, "holder": holder}))
            # 不建立连接就收不到 status 事件：补一次只读查询，列表仍显示主播与标题
            self.hub.submit(self._async_fetch_room_info(room_id))
            return
        client = RoomClient(self.hub.api, room_id, self.hub.storage,
                            event_callback=self._client_event)
        # 弹幕门控：只有「开关开启 + 当前选中房间」才接收/落盘弹幕
        # （RoomClient 默认不接收，必须显式设置）
        client.set_danmaku_enabled(bool(self.dm_var) and self._selected_room_id == room_id)
        task = asyncio.create_task(client.run(), name=f"room-{room_id}")
        self.room_tasks[room_id] = (client, task)
        task.add_done_callback(
            lambda _t, rid=room_id: self.ui_queue.put(("room_done", {"room_id": rid}))
        )

    async def _stop_room(self, room_id: int) -> None:
        pair = self.room_tasks.pop(room_id, None)
        if pair is None:
            return
        _client, task = pair
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _client_event(self, event_type: str, payload: dict) -> None:
        self.ui_queue.put(("client", event_type, payload))

    # ---------- 表情图片下载（面板按钮用，与 Tk 版同款管线） ----------

    def request_emoticon_images(self, urls: List[str]) -> None:
        """请求下载若干表情图片（后台限流下载，结果经 ui_queue 回主线程解码）。"""
        if not urls:
            return
        self.hub.submit(self._async_load_emoticon_images(list(urls)))

    async def _async_load_emoticon_images(self, urls: List[str]) -> None:
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
                async with api.session.get(url, headers=api.image_headers()) as resp:
                    resp.raise_for_status()
                    raw = await resp.read()
        except Exception as exc:
            log_data.debug("下载表情图片失败 %s: %s", url, exc)
            self.ui_queue.put(("emoticon_image", {"url": url, "data": ""}))
            return
        self.ui_queue.put(("emoticon_image", {
            "url": url, "data": base64.b64encode(raw).decode("ascii")}))

    def request_room_packages(self, room_id: int) -> None:
        """按房间拉取可用表情包（表情面板与弹幕表情兜底共用）。

        表情包会随粉丝灯牌升级等条件解锁，故允许重复拉取；同一房间在
        ``EMOTICON_REFRESH_COOLDOWN_S`` 内只请求一次，避免连点触发风控。
        """
        now = time.monotonic()
        if now - self._emoticon_fetched_at.get(room_id, 0.0) < EMOTICON_REFRESH_COOLDOWN_S:
            return
        self._emoticon_fetched_at[room_id] = now
        self.hub.submit(self._async_load_emoticons(room_id))

    async def _async_load_emoticons(self, room_id: int) -> None:
        api = self.hub.api
        if api is None:
            return
        packages = await api.get_room_emoticons(self.real_room_id(room_id))
        self.ui_queue.put(("emoticons", {"room_id": room_id, "packages": packages}))

    def emoticons_for(self, room_id) -> Optional[list]:
        """已缓存的该房间表情包；尚未加载过返回 None（区别于「加载过但为空」）。"""
        return self._emoticons.get(room_id)

    def _emoticon_semaphore(self) -> asyncio.Semaphore:
        """在事件循环内惰性创建并发信号量（避免绑定到错误的 loop）。"""
        semaphore = getattr(self.hub, "_emoticon_sem", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(4)
            self.hub._emoticon_sem = semaphore
        return semaphore

    # ---------- SC 实时渲染与历史 ----------

    def _clear_sc_view(self) -> None:
        self.sc_text.clear()
        self._sc_unseen = 0
        if hasattr(self, "sc_badge"):
            self.sc_badge.hide()
        self._history_gen += 1

    def _load_history(self, room_id: int, skip: int = 0) -> None:
        self._history_gen += 1
        gen = self._history_gen
        log_data.debug("读取历史 SC：房间 %s，skip=%d", room_id, skip)
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
                log_data.exception("读取历史 SC 失败 room=%s", room_id)
                page, deleted, total = [], {}, 0
            self.ui_queue.put(("history", {
                "gen": gen, "room_id": room_id, "records": page, "deleted": deleted,
                "total": total, "skip": skip,
            }))

        threading.Thread(target=worker, name=f"history-{room_id}", daemon=True).start()

    def _on_history_loaded(self, payload: dict) -> None:
        if payload["gen"] != self._history_gen:
            return
        if payload["room_id"] != self._selected_room_id:
            return
        room_id = payload["room_id"]
        records = payload["records"]
        deleted = payload["deleted"]
        total = int(payload.get("total") or 0)
        skip = int(payload.get("skip") or 0)
        loaded_before = self._loaded_count
        self._sc_total[room_id] = total
        self._update_sc_total_label()
        self._loading_more = False
        self._loaded_count = skip + len(records)
        self._has_more = self._loaded_count < total
        marker = self._history_marker(self._loaded_count, total)
        if skip == 0:
            self._clear_sc_view()
            if not records:
                self._append_info("（尚无 SC 记录，收到新 SC 后会实时显示在这里）")
                return
            for record in records:
                self._render_record(record, deleted)
            self._append_info(f"（历史 {total} 条sc记录）")
            self._prepend_info(marker)
            self._scroll_to_bottom()
            return
        # 向上翻页：更早记录插在顶部
        block = self._pack_records(records, deleted)
        self._prepend_block(block)
        self._prepend_info(marker)

    def _pack_records(self, records: list, deleted: dict) -> list:
        block: list = []
        for record in records:
            sc = record["sc"]
            block.append((record["time_received"], sc,
                          str(sc.get("id")) in deleted))
        return block

    def _render_record(self, record: dict, deleted: dict) -> None:
        sc = record["sc"]
        self._append_sc_formatted(record["time_received"], sc,
                                  deleted=str(sc.get("id")) in deleted)

    def _append_sc_formatted(self, time_str: str, sc: dict, *,
                             deleted: bool = False, pending: bool = False,
                             count_unseen: bool = False) -> None:
        follow = self._sc_at_bottom()
        segments = build_sc_segments(time_str, sc, deleted=deleted, pending=pending)
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        sc_id = sc.get("id")
        for idx, (chunk, tag) in enumerate(segments):
            fmt = QTextCharFormat()
            tag = (tag or "").strip()
            seg_tags = set(tag.split()) if tag else set()
            color = None
            for name, color_hex in SC_PRICE_COLORS.items():
                if name in seg_tags:
                    color = QColor(color_hex)
                    break
            if color is not None:
                fmt.setForeground(color)
            if idx < len(segments) - 1 and sc_id is not None:
                fmt.setProperty(_SC_ID_KEY, str(sc_id))
            user_tag = next((t for t in seg_tags if t.startswith("uid:")), None)
            if user_tag:
                fmt.setProperty(_UID_KEY, int(user_tag.split(":", 1)[1]))
            cursor.insertText(chunk, fmt)
        # segments 末尾已含 "\n"（Qt 里会另起一个文本块），不能再 insertBlock，
        # 否则每条 SC 后面会多出一整行空行（行距翻倍）
        if follow:
            self._scroll_to_bottom()
        elif count_unseen:
            # 用户正在向上翻阅：累加未读计数并显示「N 条新SC ↓」徽标
            self._sc_unseen += 1
            self._sync_unseen_badges()

    def _append_info(self, text: str) -> None:
        follow = self._sc_at_bottom()
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(SC_PRICE_COLORS["info"]))
        cursor.insertText(text + "\n", fmt)
        if follow:
            self._scroll_to_bottom()

    def _prepend_info(self, text: str) -> None:
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.Start)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(SC_PRICE_COLORS["info"]))
        cursor.insertText(text + "\n", fmt)

    def _prepend_block(self, records: list) -> None:
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for time_str, sc, deleted in records:
            segments = build_sc_segments(time_str, sc, deleted=deleted)
            sc_id = sc.get("id")
            for idx, (chunk, tag) in enumerate(segments):
                fmt = QTextCharFormat()
                tag = (tag or "").strip()
                seg_tags = set(tag.split()) if tag else set()
                for name, color_hex in SC_PRICE_COLORS.items():
                    if name in seg_tags:
                        fmt.setForeground(QColor(color_hex))
                        break
                if idx < len(segments) - 1 and sc_id is not None:
                    fmt.setProperty(_SC_ID_KEY, str(sc_id))
                cursor.insertText(chunk, fmt)
            # 同上：末尾 "\n" 已换行，勿再 insertBlock（避免空行）

    def _on_sc_clicked(self, pos) -> None:
        """点击 SC 中的用户名 → 打开其个人空间（与 Tk 版 _on_sc_click 一致）。"""
        cursor = self.sc_text.cursorForPosition(pos)
        uid = int(cursor.charFormat().property(_UID_KEY) or 0)
        if uid:
            webbrowser.open(f"https://space.bilibili.com/{uid}")

    def _on_sc_wheel(self, _value: int) -> None:
        if self.sc_text.verticalScrollBar().value() <= 0:
            self._maybe_load_more()

    def _maybe_load_more(self) -> None:
        if (self._selected_room_id is None or not self._has_more
                or self._loading_more or self.hub.storage is None):
            return
        if self.sc_text.verticalScrollBar().value() > 0:
            return
        self._load_history(self._selected_room_id,
                           skip=self._loaded_count)

    def _sc_at_bottom(self) -> bool:
        bar = self.sc_text.verticalScrollBar()
        return bar.maximum() - bar.value() <= SC_AT_BOTTOM_TOLERANCE

    def _scroll_to_bottom(self) -> None:
        bar = self.sc_text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _history_marker(self, loaded: int, total: int) -> str:
        if loaded < total:
            return f"（已读取共 {loaded} 条历史记录，向上滚动加载更早记录）"
        return f"（已读取全部 {loaded} 条历史记录）"

    def _update_sc_header(self, room_id: int) -> None:
        anchor = self.anchor_names.get(room_id) or "未知主播"
        title = self.titles.get(room_id) or ""
        parts = [f"房间 {room_id} · {anchor}"]
        if title:
            if len(title) > SC_TITLE_MAX_LEN:
                title = title[:SC_TITLE_MAX_LEN] + "…"
            parts.append(f"标题：{title}")
        viewers = self.viewers.get(room_id)
        guards = self.guard_num.get(room_id)
        if viewers:
            parts.append(f"同接 {viewers}")
        if guards is not None and guards >= 0:
            parts.append(f"舰长 {guards}")
        medal_name, medal_level = self._fan_medal_for(room_id)
        if medal_name:
            parts.append(f"粉丝牌 {medal_name} Lv{medal_level}")
        text = " · ".join(parts)
        self.sc_header.setText(text)
        self.sc_header.setToolTip(text)  # 单行显示：过长时靠悬浮查看完整内容

    def _update_sc_total_label(self) -> None:
        room_id = self._selected_room_id
        total = self._sc_total.get(room_id, 0)
        self.sc_total_label.setText(f"累计 SC：{total}" if room_id else "")

    def _fan_medal_for(self, room_id: int) -> tuple:
        """当前房间的粉丝牌名/等级（取自粉丝牌页缓存；无则返回空）。"""
        tab = getattr(self, "medal_tab", None)
        if tab is None:
            return "", 0
        if room_id in tab._medal_levels_room:
            return tab._medal_names_room.get(room_id, ""), \
                tab._medal_levels_room[room_id]
        entry = self.entries.get(room_id)
        uid = int(entry.uid) if entry else 0
        if uid and uid in tab._medal_levels_uid:
            return tab._medal_names_uid.get(uid, ""), \
                tab._medal_levels_uid[uid]
        return "", 0

    # ---------- 新消息未读徽标 ----------

    def _make_unseen_badge(self, parent: QWidget):
        """在文本区内部创建一个「N 条新消息 ↓」点击徽标（覆盖层）。"""
        badge = QPushButton("", parent)
        badge.setFlat(True)
        badge.setCursor(Qt.PointingHandCursor)
        badge.setStyleSheet(
            "QPushButton{background:#fff3cd; color:#c77700; border:1px solid #e0b35d;"
            " border-radius:4px; padding:2px 10px; font-size:11px; font-weight:bold;}"
            " QPushButton:hover{background:#ffe9a8;}")
        return badge

    def _place_unseen_badge(self, badge, widget: QTextEdit, label: str) -> None:
        """把徽标吸附到文本区右下角（避开垂直滚动条），文案为空则隐藏。"""
        if not label:
            badge.hide()
            return
        badge.setText(label)
        badge.adjustSize()
        bar = widget.verticalScrollBar()
        sbw = bar.width() if bar.isVisible() and bar.maximum() > bar.minimum() else 0
        badge.move(widget.width() - sbw - badge.width() - 12,
                   widget.height() - badge.height() - 10)
        badge.raise_()
        badge.show()

    def _sync_unseen_badges(self) -> None:
        """每轮轮询：同步 SC 与弹幕区的未读徽标；滚动回底部后清零隐藏。"""
        if self._sc_unseen and self._sc_at_bottom():
            self._sc_unseen = 0
        self._place_unseen_badge(
            self.sc_badge, self.sc_text, unseen_badge_text(self._sc_unseen, "SC"))
        if hasattr(self, "dm_panel"):
            self.dm_panel._sync_unseen_badge()

    def _on_sc_badge_clicked(self) -> None:
        self._sc_unseen = 0
        self._scroll_to_bottom()
        self._sync_unseen_badges()

    # ---------- 队列轮询 ----------

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
                elif kind == "room_info":
                    self._on_room_info(item[1])
                elif kind == "cookie_result":
                    self._on_cookie_result(item[1])
                elif kind == "cookie_plugin_result":
                    self._on_cookie_from_plugin_result(item[1])
                elif kind == "dm_state":
                    self._on_dm_state(item[1])
                elif kind == "dm_config":
                    if hasattr(self, "dm_panel"):
                        self.dm_panel.on_dm_config(item[1])
                elif kind == "dm_send_result":
                    if hasattr(self, "dm_panel"):
                        self.dm_panel.on_dm_send_result(item[1])
                elif kind == "emoticons":
                    payload = item[1]
                    packages = payload.get("packages") or []
                    # 刷新失败（或确实为空）时保留上次结果，不把已有表情包清空
                    if packages:
                        self._emoticons[int(payload.get("room_id") or 0)] = packages
                    if hasattr(self, "dm_panel"):
                        self.dm_panel.on_emoticons(payload)
                elif kind == "emoticon_image":
                    if hasattr(self, "dm_panel"):
                        self.dm_panel.on_emoticon_image(item[1])
                elif kind == "preview_ready":
                    if hasattr(self, "preview"):
                        self.preview.on_streams_ready(item[1])
                elif kind == "preview_error":
                    if hasattr(self, "preview"):
                        self.preview.on_stream_error(item[1])
                elif kind == "watch_reported":
                    if hasattr(self, "preview"):
                        self.preview.on_watch_reported(item[1])
                elif kind == "watch_error":
                    if hasattr(self, "preview"):
                        self.preview.on_watch_error(item[1])
                elif kind == "preview_unlocked":
                    if hasattr(self, "preview"):
                        self.preview.on_preview_unlocked(item[1])
                elif kind == "medal_list":
                    self.medal_tab.on_medal_list(item[1])
                elif kind == "medal_task_info":
                    self.medal_tab.on_medal_task_info(item[1])
                elif kind == "medal_tasks_done":
                    self.medal_tab.on_medal_tasks_done(item[1])
                elif kind == "medal_result":
                    self._on_medal_result(item[1])
                elif kind == "medal_event":
                    self._on_medal_event(item[1], item[2])
                else:
                    log_app.debug("未处理的队列事件: %s", item[0])
        except queue.Empty:
            pass
        self._append_logs(log_lines)
        if self._selected_room_id is not None and self._sc_total.get(self._selected_room_id):
            self._update_sc_total_label()
        # 记录 SC 区是否吸底（尺寸变化后据此保持贴底）
        self.sc_text.refresh_bottom_state()
        # 本轮累积的弹幕聚合一次渲染（仅当前选中房间）
        if self._dm_pending and hasattr(self, "dm_panel"):
            batch, self._dm_pending = self._dm_pending, []
            selected = self._selected_room_id
            batch = [d for d in batch if d.get("room_id") == selected]
            if batch:
                self.dm_panel.append_dm_batch(batch)
        self._sync_unseen_badges()

    def _append_log(self, line: str) -> None:
        self._append_logs([line])

    def _append_logs(self, lines: List[str]) -> None:
        """批量插入日志行：只做一次插入与一次滚动，降低重排开销（同 Tk 版）。"""
        if not lines:
            return
        doc = self.log_view.document()
        # 超过行数上限时裁掉最旧的一半，避免日志视图无限增长
        if doc.blockCount() > DEBUG_LOG_MAX_LINES:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor,
                                DEBUG_LOG_MAX_LINES // 2)
            cursor.removeSelectedText()
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText("\n".join(lines) + "\n")
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_debug_toggle(self, checked: bool) -> None:
        # 与 Tk 版一致：改根 logger 级别，各模块子 logger 才会跟着放行 DEBUG；
        # 取消勾选时回到 config.json 配置的级别（而不是硬编码 INFO）
        level = logging.DEBUG if checked else self._base_log_level
        logging.getLogger().setLevel(level)
        log_app.info("调试日志开关：%s（当前级别 %s）",
                     "开" if checked else "关", logging.getLevelName(level))

    # ---------- 弹幕 / 粉丝牌状态 ----------

    def copy_to_clipboard(self, text: str) -> None:
        QApplication.clipboard().setText(text)

    # ---------- 直播预览（ROADMAP 63 · P1，仅 Qt 版） ----------

    def _preview_window(self):
        """惰性创建预览浮窗（首次点「预览」才加载 QtMultimedia，不拖慢启动）。"""
        window = getattr(self, "preview", None)
        if window is None:
            from .qt_preview import PreviewWindow

            window = PreviewWindow(self)
            self.preview = window
            self.refresh_preview_button()
        return window

    def _on_preview_clicked(self) -> None:
        """工具栏「预览」按钮：把选中的直播间加入预览（都已加入则停止它们）。

        支持 Ctrl 多选**批量加入**（受 ``preview.max_rooms`` 限制，默认最多 4 路）；再次点击
        时若选中的房间都已在预览中，就把它们一起停掉——按钮文案会跟着变（「预览」/「停止预览」）。
        """
        window = self._preview_window()
        selected = [r for r in self._get_selected_room_ids() if r in self.entries]
        if not selected:
            QMessageBox.information(self, "直播预览", "请先在房间列表里选中一个直播间。")
            return
        active = set(window.room_ids())
        if all(room_id in active for room_id in selected):
            for room_id in selected:
                window.remove_room(room_id)
            self.refresh_preview_button()
            return
        for room_id in selected:
            if room_id not in active:
                window.start(room_id)
        self.refresh_preview_button()

    def _on_room_context_menu(self, pos) -> None:
        """房间列表右键：预览该房间 / 停止预览该房间 / 设为主路（工具栏之外的入口）。

        菜单只作用在**右键所在的那一行**：多路预览时「停止预览」只停这一路（不是全部），
        「设为主路」把有声音的那一路切过去。预览一个非选中房间时会顺手把它选中——先切流再
        选中：这样「跟随选中房间」随后发现已是同一房间就会跳过，不会把预览又切走。
        """
        index = self.table.indexAt(pos)
        row = index.row() if index.isValid() else -1
        if not (0 <= row < len(self._room_order)):
            return
        room_id = self._room_order[row]
        window = self._preview_window()
        active_here = int(room_id) in set(window.room_ids())
        menu = QMenu(self)
        anchor = self.anchor_names.get(room_id) or ""
        header = menu.addAction(f"房间 {room_id}" + (f"（{anchor}）" if anchor else ""))
        header.setEnabled(False)  # 仅作标题
        menu.addSeparator()
        action = menu.addAction("停止预览该房间" if active_here else "预览该房间")
        main_action = None
        if active_here and window.main_room_id != int(room_id):
            main_action = menu.addAction("设为主路（有声音）")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if main_action is not None and chosen is main_action:
            window.set_main_room(room_id)
            self.refresh_preview_button()
            return
        if chosen is not action:
            return
        if active_here:
            window.remove_room(room_id)
            self.refresh_preview_button()
            return
        window.start(room_id)
        self._select_room(room_id)

    def refresh_preview_button(self) -> None:
        """按预览状态更新按钮文案（浮窗在加入 / 移除 / 关闭时回调）。

        文案看**当前选中的房间**是否都已在预览中：都在 → 「停止预览」（点了会把它们停掉），
        否则 → 「预览」（点了把还没加入的加进去）。
        """
        if not hasattr(self, "preview_btn"):
            return
        window = getattr(self, "preview", None)
        active = set(window.room_ids()) if window is not None else set()
        selected = [r for r in self._get_selected_room_ids() if r in self.entries]
        all_active = bool(selected) and all(r in active for r in selected)
        self.preview_btn.setText("停止预览" if all_active else "预览")
        self.preview_btn.setEnabled(bool(selected))

    def _follow_preview(self, room_id: Optional[int]) -> None:
        """预览跟随选中房间（``config.json`` 的 ``preview.follow_room``，默认开）。

        **只切声音，绝不动预览内容**：选中的房间已在预览中 → 把它设为主路（有声音）；
        不在预览中 → 什么也不做。加减路请用「预览」按钮 / 双击房间行 / 右键菜单。
        （P3 早期版本在「只有一路」时会把这一路**换成**选中房间，于是在主界面点几下就把
        预览换掉了、也没法逐个把房间加进来——那个「抢走预览」的行为已去掉。）
        """
        window = getattr(self, "preview", None)
        if window is None or not window.is_active():
            self.refresh_preview_button()  # 选中变化也会影响按钮可用状态
            return
        if not self.app_config.preview.follow_room or room_id is None:
            return
        selected = int(room_id)
        if selected in window.room_ids() and window.main_room_id != selected:
            window.set_main_room(selected)

    def _on_room_double_clicked(self, row: int, column: int) -> None:
        """双击房间行：加入 / 停止该房间的预览（比 Ctrl 多选逐个加入顺手）。

        跳转列（房间号 / 主播 / 提醒）不参与：那里单击就会打开浏览器或切换提醒，双击会先
        触发一次单击，再叠加预览操作会打架。双击「状态 / 直播标题 / 备注」列即可。
        """
        if column in self.table.link_columns:
            return
        if not (0 <= row < len(self._room_order)):
            return
        room_id = self._room_order[row]
        window = self._preview_window()
        if int(room_id) in set(window.room_ids()):
            window.remove_room(room_id)
        else:
            window.start(room_id)
        self.refresh_preview_button()

    def save_preview_quality(self, qn: int) -> None:
        """记住预览清晰度（``gui_rooms.json`` 的 ``ui.preview_quality``），下次启动沿用。

        浮窗里改清晰度会回调这里——此前只在本次运行内有效，重启又回到 config.json 的默认值。
        """
        try:
            value = int(qn)
        except (TypeError, ValueError):
            return
        if self.ui_prefs.get("preview_quality") == value:
            return
        self.ui_prefs["preview_quality"] = value
        self._save_config()

    def _on_dm_state(self, _payload) -> None:
        """登录态 / Cookie 变化后刷新门控与可用颜色/模式。"""
        # 可用表情随登录身份变化：缓存与刷新冷却一并失效
        self._emoticons.clear()
        self._emoticon_fetched_at.clear()
        if hasattr(self, "dm_panel"):
            self.dm_panel.on_login_changed()
        self._refresh_dm_send_state()
        self._load_dm_options_for_selected()
        # 预览清晰度档位随登录态收缩/放开（未登录只有 720P）
        window = getattr(self, "preview", None)
        if window is not None:
            window.refresh_qualities()
        if hasattr(self, "medal_tab"):
            self.medal_tab._on_refresh()

    def _on_medal_result(self, payload: dict) -> None:
        room_id = payload.get("room_id")
        if room_id in self._medal_running:
            self._medal_running.discard(room_id)
        status = payload.get("status") or "error"
        message = payload.get("message") or ""
        elapsed = payload.get("elapsed")
        suffix = f"（总耗时 {elapsed}s）" if elapsed is not None else ""
        prefix = {"done": "完成", "partial": "部分完成", "risk": "风控中止",
                  "interrupted": "已打断", "blocked": "未执行",
                  "error": "失败"}.get(status, status)
        # 自动执行「无事可做」时静默（仅记日志），避免每轮覆盖用户可见提示
        if bool(payload.get("manual", True)) or status != "done":
            self.medal_tab.set_hint(f"{prefix}：{message}{suffix}")
        if status in ("risk", "error"):
            log_task.warning("粉丝牌任务%s：%s%s", prefix, message, suffix)
        else:
            log_task.info("粉丝牌任务%s：%s%s", prefix, message, suffix)
        self.medal_tab._on_task_selected()
        # 任务完成会改变亲密度/经验：顺带刷新粉丝牌与该房间任务，无需手动点刷新
        if (room_id is not None and self.hub.ready.is_set()
                and self.hub.api is not None and self.hub.api.logged_in):
            self.hub.submit(self.medal_tab._async_refresh_medals())
            self.hub.submit(self.medal_tab._async_refresh_tasks(
                room_ids=[room_id], announce=False))

    def _input_room_for(self, real_room_id) -> Optional[int]:
        """真实房间号 → 界面里的输入房间号（短号场景 runner 回报的是真实号）。"""
        try:
            room_id = int(real_room_id)
        except (TypeError, ValueError):
            return None
        if room_id in self.entries:
            return room_id
        for input_id, real_id in self._room_id_map.items():
            if real_id == room_id and input_id in self.entries:
                return input_id
        return None

    def _on_medal_event(self, event_type: str, payload: dict) -> None:
        """粉丝牌执行器的过程事件（与 Tk 版 _on_medal_event 一致）。"""
        room_id = payload.get("room_id")
        label = payload.get("room_label") or room_id
        if event_type == "medal_note":
            message = str(payload.get("message") or "")
            self.medal_tab.set_hint(f"房间 {label}：{message}")
            log_task.info("粉丝牌任务（房间 %s）：%s", label, message)
        elif event_type == "medal_interrupt":
            message = str(payload.get("message") or "已打断自动发弹幕任务")
            self.medal_tab.set_hint(f"房间 {label}：{message}")
            log_task.info("粉丝牌任务（房间 %s）：%s", label, message)
        elif event_type == "medal_start":
            log_task.info("房间 %s 开始执行粉丝牌任务", label)
        elif event_type == "medal_progress":
            outcome = payload.get("outcome") or {}
            elapsed = outcome.get("elapsed")
            tail = f"（用时 {elapsed}s）" if elapsed is not None else ""
            log_task.info("房间 %s %s：%s%s", label,
                        task_label(str(payload.get("jump_type") or "")),
                        outcome.get("message") or "", tail)
        elif event_type == "medal_task_progress":
            # 执行期间的实时进度：就地更新该房间任务行（room_id 需映射回输入房间号）
            mapped = dict(payload)
            mapped["room_id"] = self._input_room_for(room_id)
            if mapped["room_id"] is not None:
                self.medal_tab.apply_task_progress(mapped)

    # ---------- Cookie ----------

    def _on_hub_ready(self) -> None:
        log_app.info("后台就绪，开始启动已启用的房间")
        for room_id in self.entries:
            if self.entries[room_id].enabled:
                self.client_states[room_id] = "starting"
                self.hub.submit(self._start_room(room_id))
            else:
                # 停用监听的房间不建立连接：补一次只读查询填主播名与直播标题
                self.hub.submit(self._async_fetch_room_info(room_id))
        self._refresh_all_rows()
        if self.entries and self._selected_room_id is None:
            self._select_room(next(iter(self.entries)))
        elif self._selected_room_id is not None:
            self._load_history(self._selected_room_id)
        self._apply_dm_gate()
        self._refresh_dm_send_state()
        self._load_dm_options_for_selected()
        # 启动即拉取粉丝牌与各房间任务：无需先点开「粉丝牌」页签，
        # SC 标题栏也能直接显示粉丝牌名称与等级（与 Tk 版一致）。
        if self.hub.api is not None and self.hub.api.logged_in:
            self.medal_tab._on_refresh()

    def _refresh_all_rows(self) -> None:
        for room_id in self._room_order:
            self._refresh_row(room_id)

    def _on_hub_failed(self, error: str) -> None:
        log_app.error("后台初始化失败：%s", error)
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
            # 直播标题单独存：状态列显示直播状态，标题列显示标题（勿混用）
            self.titles[room_id] = payload.get("title") or ""
            if payload.get("anchor_name"):
                self.anchor_names[room_id] = payload["anchor_name"]
            entry = self.entries.get(room_id)
            if entry is not None and int(payload.get("uid") or 0) != entry.uid:
                entry.uid = int(payload.get("uid") or 0)
                self._save_config()
            if prev_text and prev_text != "直播中" and status_text == "直播中":
                if entry is None or entry.notify_live:
                    self._notify_live(room_id, payload.get("title") or "")
                # 开播信号打断正在执行的「自动发弹幕」（与 Tk 版一致，ROADMAP 64）：
                # 默认只在未开播时自动发弹幕，开播后不该继续刷屏
                self._interrupt_auto_danmaku(room_id)
                # 开播信号直接触发自动点赞（与 Tk 版一致，与「提醒」开关无关）
                self._try_start_auto_room(room_id)
            if self.client_states.get(room_id) == "starting":
                self.client_states[room_id] = "running"
            self._refresh_row(room_id)
            if room_id == self._selected_room_id:
                self._update_sc_header(room_id)
        elif event_type == "sc":
            if room_id == self._selected_room_id:
                self._append_sc_formatted(payload.get("time_received", ""),
                                          payload.get("sc") or {},
                                          pending=not payload.get("saved", True),
                                          count_unseen=True)
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
            holder = str(payload.get("holder") or "")
            if holder:
                self.occupiers[room_id] = holder
            self._refresh_row(room_id)
        elif event_type == "dm":
            if hasattr(self, "dm_panel"):
                self._dm_pending.append(payload)
        elif event_type == "online_count":
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
            log_live.debug("房间 %s 连接中断，%.1f 秒后进行第 %d 次重连",
                         room_id, float(payload.get("delay") or 0),
                         int(payload.get("attempt") or 0))
        elif event_type == "reconnected":
            log_live.info("房间 %s 断线后已自动重连成功（第 %d 次尝试）",
                        room_id, int(payload.get("attempt") or 0))

    def _on_room_done(self, room_id: int) -> None:
        if self.client_states.get(room_id) in ("starting", "running"):
            self.client_states[room_id] = "stopped"
            self._refresh_row(room_id)
            self._refresh_buttons()

    def _mark_sc_deleted(self, sc_id) -> None:
        """实时标记已删除/退款的 SC：整条置灰并在行尾追加说明。"""
        doc = self.sc_text.document()
        target = str(sc_id)
        block = doc.begin()
        while block.isValid():
            if block.textLayout():
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid() and frag.charFormat().property(_SC_ID_KEY) == target:
                        # 整块置灰 + 行尾追加说明
                        cursor = self.sc_text.textCursor()
                        cursor.setPosition(block.position())
                        cursor.select(QTextCursor.BlockUnderCursor)
                        del_fmt = QTextCharFormat()
                        del_fmt.setForeground(QColor(SC_PRICE_COLORS["del"]))
                        cursor.mergeCharFormat(del_fmt)
                        cursor.movePosition(QTextCursor.EndOfBlock)
                        cursor.insertText("  （已删除，退款）", del_fmt)
                        return
                    it += 1
            block = block.next()
        self._append_info(f"SC {sc_id} 已被删除（退款）")

    # ---------- Cookie 获取 ----------

    def _on_fetch_cookie(self) -> None:
        if not QMessageBox.question(
                self, "获取 Cookie",
                "将从本机浏览器的 Cookie 数据库中读取 bilibili.com 的 Cookie：\n\n"
                "· 支持 Edge / Chrome / Brave / Vivaldi / Opera / Firefox\n"
                "· 正在运行的浏览器优先，自动选择可用的 Cookie\n"
                "· 仅写本地 cookie.txt（不会上传），并立即应用到当前会话\n\n"
                "注意：\n"
                "· 新版浏览器运行时会独占锁定 Cookie 数据库，获取时可能需要"
                "**暂时完全退出对应浏览器**再重试",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            return
        if self.hub.api is None:
            QMessageBox.warning(self, "请稍候", "后台网络初始化中，请稍后再试")
            return
        self.cookie_btn.setEnabled(False)
        log_task.info("开始从本机浏览器获取 B 站 Cookie…")
        threading.Thread(target=self._fetch_cookie_worker,
                         name="fetch-cookie", daemon=True).start()

    def _fetch_cookie_worker(self) -> None:
        try:
            cookie, source, errors = get_bilibili_cookie()
        except Exception as exc:
            log_task.exception("获取浏览器 Cookie 异常")
            cookie, source, errors = None, None, [f"获取过程异常：{exc}"]
        self.ui_queue.put(("cookie_result",
                           {"cookie": cookie, "source": source, "errors": errors}))

    def _on_cookie_result(self, payload: dict) -> None:
        self.cookie_btn.setEnabled(True)
        cookie = payload.get("cookie")
        source = payload.get("source")
        errors = payload.get("errors") or []
        for err in errors:
            log_task.info("Cookie 获取提示：%s", err)
        if not cookie:
            detail = "\n".join(errors) if errors else "未找到可用 Cookie"
            QMessageBox.critical(self, "获取 Cookie 失败",
                                 f"未能从本机浏览器获取到可用的 B 站 Cookie：\n\n{detail}\n\n"
                                 "可手动从浏览器复制 Cookie 后保存为项目根目录的 cookie.txt\n"
                                 "（浏览器 F12 → Network → 任选请求 → 复制 Cookie 请求头）")
            return
        try:
            Path(COOKIE_FILE_PATH).write_text(cookie, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "获取 Cookie 失败", f"写入 cookie.txt 失败：{exc}")
            return
        self.hub.submit(self._async_apply_cookie(cookie))
        log_task.info("已获取 B 站 Cookie（来源：%s，长度 %d），已保存并应用到当前会话",
                    source, len(cookie))
        QMessageBox.information(self, "获取 Cookie 成功",
                                f"来源：{source}\n已保存到 cookie.txt 并应用到当前会话。")

    def _on_fetch_cookie_plugin(self) -> None:
        if not QMessageBox.question(
                self, "从插件获取 Cookie",
                "将通过自带的浏览器扩展接收登录 Cookie：\n\n"
                f"· 程序将在本机 127.0.0.1:{DEFAULT_COOKIE_PORT} 临时开启一个端口等待\n"
                "· 请在浏览器（Edge/Chrome）中先加载本项目 extension/ 目录的扩展，"
                "再点扩展里的「获取并发送」按钮\n"
                "· 仅写入本地 cookie.txt 并立即应用到当前会话，不会上传\n"
                "· 若约 90 秒内未收到将自动关闭并停止等待\n\n"
                "注意：\n"
                "· Cookie 等同你的登录凭证，仅向本机端口发送，请勿在不可信环境使用\n"
                "· 需先安装并启用扩展（edge://extensions → 开发者模式 → 加载已解压的扩展）",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            return
        if self.hub.api is None:
            QMessageBox.warning(self, "请稍候", "后台网络初始化中，请稍后再试")
            return
        self.cookie_plugin_btn.setEnabled(False)
        log_task.info("开始等待浏览器扩展发送 B 站 Cookie（端口 %s）…", DEFAULT_COOKIE_PORT)
        threading.Thread(target=self._fetch_cookie_plugin_worker,
                         name="fetch-cookie-plugin", daemon=True).start()

    def _fetch_cookie_plugin_worker(self) -> None:
        try:
            cookie = wait_for_extension_cookie(port=DEFAULT_COOKIE_PORT)
        except OSError as exc:
            log_task.warning("cookie server 无法启动: %s", exc)
            self.ui_queue.put(("cookie_plugin_result",
                               {"cookie": None, "error": f"本地端口无法开启：{exc}"}))
            return
        except Exception as exc:
            log_task.exception("等待扩展 Cookie 异常")
            self.ui_queue.put(("cookie_plugin_result",
                               {"cookie": None, "error": f"获取过程异常：{exc}"}))
            return
        self.ui_queue.put(("cookie_plugin_result", {"cookie": cookie, "error": None}))

    def _on_cookie_from_plugin_result(self, payload: dict) -> None:
        self.cookie_plugin_btn.setEnabled(True)
        cookie = payload.get("cookie")
        error = payload.get("error")
        if not cookie:
            detail = error or "约 90 秒内未收到插件发送的 Cookie（可能插件未安装/未点发送，或浏览器未打开）"
            QMessageBox.critical(
                self, "从插件获取失败",
                f"未获取到 Cookie：\n\n{detail}\n\n"
                "请确认：\n"
                "· 已在 edge://extensions 开启开发者模式并加载本项目的 extension/ 目录\n"
                "· 打开了插件弹窗并勾选确认后点击「获取并发送」\n"
                "· 也可改用「获取Cookie」按钮从浏览器数据库读取（需完全退出浏览器）")
            return
        try:
            Path(COOKIE_FILE_PATH).write_text(cookie, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "从插件获取失败", f"写入 cookie.txt 失败：{exc}")
            return
        self.hub.submit(self._async_apply_cookie(cookie))
        log_task.info("已通过浏览器扩展获取 B 站 Cookie（长度 %d），已保存并应用到当前会话",
                    len(cookie))
        QMessageBox.information(self, "从插件获取成功",
                                "已通过浏览器扩展获取到 B 站登录 Cookie\n"
                                "已保存到 cookie.txt 并应用到当前会话。")

    async def _async_apply_cookie(self, cookie: str) -> None:
        if self.hub.api is None:
            return
        self.hub.api.set_cookie(cookie)
        try:
            await self.hub.api.refresh_login()
        except Exception as exc:
            log_data.debug("刷新登录 uid 失败: %s", exc)
        self.ui_queue.put(("dm_state", None))

    # ---------- 开播提醒 ----------

    def _notify_live(self, room_id: int, title: str) -> None:
        log_task.info("房间 %s 开播了：%s", room_id, title or "（无标题）")
        _notify_sound_play(str(self.ui_prefs.get("notify_sound", "上行双音")))
        try:
            hwnd = int(self.winId())
            _flash_taskbar(hwnd)
        except Exception:
            pass
        if self.ui_prefs.get("notify_overlay", True):
            self._notify_overlay(room_id, title)

    def _notify_overlay(self, room_id: int, title: str) -> None:
        """弹右下角自绘悬浮提醒窗（不受勿扰模式影响，不抢占焦点）。"""
        anchor = self.anchor_names.get(room_id)
        head = f"{anchor} 开播了" if anchor else f"直播间 {room_id} 开播了"
        message = title if len(title) <= 60 else title[:57] + "…"
        self.overlay.show(head, message,
                          persist=bool(self.ui_prefs.get("notify_persist", False)))

    # ---------- 保存 ----------

    def _save_config(self) -> None:
        save_room_entries(self.config_path, self.entries.values(), ui=self.ui_prefs,
                          emoticon=self._emoticon_memory)

    def remember_emoticon_page(self, room_id: int, index: int, name: str) -> None:
        """记住某房间停留的表情包（翻到哪一页即写入，与 Tk 版一致）。"""
        memory = {"index": max(0, int(index)), "name": str(name or "")}
        if self._emoticon_memory.get(room_id) == memory:
            return
        self._emoticon_memory[room_id] = memory
        self._save_config()

    def closeEvent(self, event) -> None:
        if not QMessageBox.question(self, "退出", "确定退出？将停止所有房间的监听。",
                                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            event.ignore()
            return
        # 记住窗口尺寸：下次启动恢复（Tk 版不使用该键，只原样写回）
        self.ui_prefs["window_size"] = [self.width(), self.height()]
        self._save_config()
        log_app.info("用户确认退出：正在停止 %d 个房间的监听", len(self._room_order))
        self._window_size_log.flush()    # 退出前把待写的窗口尺寸变化补上
        self._splitter_size_log.flush()
        if hasattr(self, "dm_panel"):
            self.dm_panel.destroy_popups()
        window = getattr(self, "preview", None)
        if window is not None:  # 预览浮窗：停播放、回收回环代理，并把浮窗一起关掉
            window.stop(reason="程序退出", close=True)
        self.hub.submit(self._async_shutdown())
        # 等后台收尾（flush_all_pending / close_danmaku_buffers）落盘后再退出，
        # 否则守护线程会随主进程一起消失，未刷盘的 SC 有丢失风险（Tk 版等 1500ms）。
        thread = getattr(self.hub, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        super().closeEvent(event)

    async def _async_shutdown(self) -> None:
        pairs = list(self.room_tasks.values())
        for _client, task in pairs:
            task.cancel()
        if pairs:
            await asyncio.gather(*[task for _client, task in pairs], return_exceptions=True)
        self.room_tasks.clear()
        self.hub.request_shutdown()


def run_gui_qt(output_dir: str = "data") -> int:
    """启动 Qt 版图形界面。"""
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("blive_sc_get_qt")
    window = QtScMonitorApp(output_dir)
    window.show()
    app.exec()
    return 0