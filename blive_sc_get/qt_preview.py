"""Qt 版直播预览浮窗（ROADMAP 63 · P3 多路宫格，最多 4 路）。

为什么只有 Qt 版：Tk 没有可用的视频组件（这也是本项目的双版方针——Tk 做基础功能，Qt 在
其之上做「需求更高」的功能）。业务层 `live_preview.py` 的回环代理与本模块无关，纯 Python。

链路（P0 spike 已验证可行）::

    api.get_live_stream_urls(真实房间号, qn)      # 带 Cookie 拉直链（web→flv / h5→hls）
        ↓
    LiveStreamProxy（回环代理：注入 Referer/Origin/Cookie，HLS 还要改写分片地址）
        ↓
    QMediaPlayer 播放 http://127.0.0.1:PORT/...   # 播放器只连本机，不碰防盗链

结构：:class:`PreviewTile` 是**一路**（自己的播放器、代理与运行状态），:class:`PreviewWindow`
是**编排者**（宫格布局、主路、控制条、资源保护）——多路时每路一个独立代理实例（随机端口、
只监听回环），互不影响。

设计要点：

- **宫格布局**：1 路全屏，2 路左右分栏，3~4 路 2×2；**点某一格即把它设为主路**（只有主路
  出声，其余强制静音），主路边框高亮。窗口大小可自由缩放、置顶、拖动。
- **最多 4 路**（``preview.max_rooms``，1~4）：到达上限时加入新房间会被拒绝并提示。
- **HLS 优先**：m3u8 由代理逐次转发，直链过期只需 `set_streams` 换源即可无缝续播；FLV 是
  单条长连接，换源必须重连，故只作回退（播放出错时自动换另一种格式重试一次）。
- **线程模型**：拉流与起代理在后台（`hub.submit`），UI 只在主线程碰（`setSource` 必须主线程），
  结果经 `ui_host.ui_queue` 回投（与项目其它子模块一致）。
- **未登录只有 720P**：B 站对匿名请求限制清晰度，未登录时下拉只保留 ≤150 档位并给出提示，
  登录后（「获取Cookie」）自动放开全部档位。
- **资源保护**：主路卡顿走断流自愈（自动重拉，上限 3 次）；**副路卡顿**只重试 1 次，仍不行
  就停掉该路（不为了看不过来的画面拖垮整机）；进程 CPU 持续偏高时同样自动停掉最后加入的
  副路（见 ``CPU_LIMIT``）。
- 默认**静音**（避免加入房间突然出声），音量 / 静音 / 置顶 / 清晰度都即时生效；清晰度是
  **全局**的（下拉作用于所有路，各房间实际可用档位不同时按该房间可用最高档播放）。
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .api import LIVE_HEARTBEAT_DEFAULT_INTERVAL
from .app_config import DEFAULT_PREVIEW_MAX_DRIFT, PREVIEW_QUALITY_CHOICES
from .live_preview import LiveStreamProxy
from .log_categories import CATEGORY_LIVE, CATEGORY_WINDOW, get_logger

log_live = get_logger(CATEGORY_LIVE, "gui_qt.preview")
log_window = get_logger(CATEGORY_WINDOW, "gui_qt.preview")

AUTO_QUALITY = 0
"""清晰度「自动」：取该房间可用最高档（``config.json`` 的 ``preview.quality`` 默认值）。"""

MAX_QUALITY = 30000
"""请求清晰度时用的上限（杜比）；服务端会按房间与账号权限降到可用档位。"""

QUALITY_TEXTS: Dict[int, str] = {
    AUTO_QUALITY: "自动（最高）",
    80: "流畅",
    150: "高清",
    250: "超清",
    400: "蓝光",
    10000: "原画",
    20000: "4K",
    30000: "杜比",
}
"""清晰度（``qn``）→ 名称；``0`` 为自动，未知取值回退成 ``qn=xxx``。

名称与 B 站官方一致、**不带分辨率**：同一 qn 在不同房间标出的分辨率并不固定（例如
「超清」有的房间是 720P），写死分辨率反而误导。
"""

ANONYMOUS_MAX_QUALITY = 150
"""未登录（匿名请求）能拿到的最高清晰度：720P。登录后才放开更高档位。"""

PREVIEW_REFRESH_MS = 10 * 60 * 1000
"""直链有效期有限：定时（10 分钟）重新拉流并换源。HLS 由代理转发播放列表，换源无缝。"""

PREVIEW_MAX_ROOMS = 4
"""同时预览的硬上限（``preview.max_rooms`` 只能配到这么多）。

每路都要解码 + 一条独立连接 + 一个本机代理，4 路已是 1080p 下普通机器的舒适区。
"""

DEFAULT_WINDOW_SIZE = (900, 560)
"""浮窗默认尺寸（逻辑像素；可自由缩放）。多路宫格需要比单路更大的初始面积。"""

MAIN_BORDER_COLOR = "#2ecc71"
"""主路边框色（有声音的那一路）。"""

SUB_BORDER_COLOR = "#444444"
"""副路边框色。"""

FORMATS = ("hls", "flv")
"""优先顺序：HLS 可无缝换源，FLV 作为回退。"""

PAUSE_RESYNC_S = 30.0
"""暂停超过这么久再继续时，自动重新拉流跳到最新画面。

直播流没有「seek 到直播点」的通用手段：暂停后继续只会从暂停处接着播，反复暂停会
越拖越后（观众看到的画面比真实直播晚）。所以恢复时若暂停较久，干脆重新装载一次；
短暂停（几秒）仍走 `play()`，避免每次都重连。
"""

HEALTH_CHECK_MS = 5 * 1000
"""播放健康检查周期：**正在直播却没在播放**时自动重新拉流（断流自愈）。

`errorOccurred` 只在播放器明确报错时才触发；直播流「静默卡死」（连接被服务端断开、
直链过期，播放器停在 StoppedState 却不报错）它兜不住，画面就那么定格着，必须手动点
「刷新」——参考项目 DD_Monitor 的做法（`VideoWidget_vlc.py`：定时器里
`not is_playing() and liveStatus != 0 and not userPause → mediaReload()`）正是补这个洞。
"""

HEALTH_GRACE_S = 15.0
"""刚把源交给播放器后的宽限期：这段时间内不判「异常」。

换档 / 换源 / 起播都有一段起播与缓冲时间（http-flv 尤其慢），期间播放器可能还没进入
PlayingState；没有宽限期就会把正常起播误判成断流，反复重载反而永远播不起来。这个间隔
同时充当两次自动重连之间的最小间隔。
"""

HEALTH_MAX_AUTO_RELOADS = 3
"""主路连续自动重连次数上限：地址确实不可播时不再反复重试。

到上限后状态提示一次「自动重连未成功，请点刷新」，并把恢复交给用户——继续以 5 秒一次的
节奏重试只会在日志里堆垃圾，也不会让死链活过来。
"""

HEALTH_MAX_RELOADS_SUB = 1
"""**副路**的自动重连次数上限（比主路更少）。

多路预览的定位是「扫一眼」，不是为了看不到的画面占满 CPU 与带宽：副路卡住只自动重连一次，
仍不行就把它停掉（提示原因），把资源让给主路。
"""

WATCH_START_DELAY_MS = 3 * 1000
"""开启上报（或换主路）后多久首次尝试上报观看时长。

给起播留一点时间：上报判定本身要求「播放器正在播放」，刚 setSource 时还没进 PlayingState。
"""

WATCH_IDLE_RETRY_MS = 15 * 1000
"""「此刻不该上报」（暂停 / 缓冲 / 未开播 / 后台未就绪）时的重查间隔。"""

DRIFT_SYNC_GRACE_S = 20.0
"""装载后多久才开始测「播放落后」：起播阶段 ``position`` 还没稳定，测不准。"""

DRIFT_MIN_INTERVAL_S = 60.0
"""两次自动追边之间的最小间隔（防抖）：避免落后在阈值附近抖动时反复重载。"""

CPU_CHECK_MS = 30 * 1000
"""进程 CPU 采样周期（资源保护）。"""

CPU_LIMIT = 0.70
"""进程 CPU 占用率告警线（1.0 = 一个核心跑满，多核可超过 1）。

超过后**连续两次**（约一分钟）仍偏高、且当前不止一路时，自动停掉最后加入的副路，
避免多路硬解把机器拖垮（画面反而全都卡）。可在 ``preview.max_rooms`` 里减路规避。
"""

CPU_LIMIT_HITS = 2
"""连续多少次采样超限才动手（避免起播瞬间的抖动被误判）。"""


def quality_text(qn: int) -> str:
    """清晰度中文名（纯函数，便于离线测试）。"""
    try:
        value = int(qn)
    except (TypeError, ValueError):
        return str(qn)
    return QUALITY_TEXTS.get(value, f"qn={value}")


def available_qualities(logged_in: bool) -> Tuple[int, ...]:
    """**兜底**档位（拿不到房间可用列表时才用）：未登录只到 720P（纯函数）。

    B 站对匿名请求限制清晰度上限，档位给多了也拉不到；登录（Cookie）后才把全部档位
    放出来。正常情况下界面会用接口返回的 ``accept_qn``（该房间真正可用的档位）收缩下拉。
    """
    choices = [qn for qn in PREVIEW_QUALITY_CHOICES if qn > AUTO_QUALITY]
    if logged_in:
        return tuple(choices)
    return tuple(qn for qn in choices if qn <= ANONYMOUS_MAX_QUALITY)


def pick_quality(preferred: int, accept) -> int:
    """决定实际请求的清晰度（纯函数）。

    - ``preferred`` 为 ``0``（自动）→ 取 ``accept`` 里最高的；
    - ``preferred`` 在 ``accept`` 中 → 就用它；
    - ``preferred`` 不在（该房间没有这一档）→ 取可用最高；
    - ``accept`` 为空（接口没给）→ 用 ``preferred``；仍为自动时用 ``MAX_QUALITY``
      请求，由服务端按权限降级（新接口会回 ``accept_qn`` 告知真实档位）。
    """
    values = []
    for item in accept or ():
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    best = max(values) if values else MAX_QUALITY
    try:
        wanted = int(preferred or 0)
    except (TypeError, ValueError):
        wanted = AUTO_QUALITY
    if wanted <= AUTO_QUALITY:
        return best
    if not values:
        return wanted
    return wanted if wanted in values else best


def needs_auto_reload(*, active: bool, paused: bool, visible: bool, playing: bool,
                      live: bool, since_load: float, auto_reloads: int,
                      max_reloads: int = HEALTH_MAX_AUTO_RELOADS) -> bool:
    """播放中断是否需要**自动重新拉流**（纯函数，便于离线测试）。

    条件与参考项目一致（「在直播 + 没暂停 + 却不在播放」），另补三点工程约束：

    - ``live``：主播**未开播**时不重连——流本来就不存在，重试只是白费（状态由主界面的
      直播状态提供，取不到时按「在播」处理，宁可多试一次也别漏自愈）；
    - ``since_load``：刚装载完有 ``HEALTH_GRACE_S`` 宽限期，避免换档 / 换源起播阶段被误判；
    - ``auto_reloads`` / ``max_reloads``：连续重试有上限（**主路 3 次、副路 1 次**，见
      ``HEALTH_MAX_RELOADS_SUB``），超过就交给上层处理（主路等用户点刷新、副路直接停路）。
    """
    if not active or paused or not visible or playing or not live:
        return False
    if since_load < HEALTH_GRACE_S:
        return False
    return auto_reloads < max_reloads


def needs_watch_report(*, enabled: bool, active: bool, paused: bool, live: bool,
                       playing: bool) -> bool:
    """此刻是否该上报一次「观看时长」（纯函数，便于离线测试）。

    只在**真正在看**的时候上报：开关开着、该路在预览、没暂停、主播在播、播放器在播——
    暂停（画面定格）、缓冲/断流（没画面）、未开播都不计，避免「没看也算时长」。
    多路时只对**主路**判定（同一时间只报 1 个房间，见 ROADMAP 63 · P2）。
    """
    return bool(enabled and active and not paused and live and playing)


def needs_catch_up(*, playing: bool, paused: bool, drift_s: float, limit_s: float,
                   since_sync_s: float, since_catch_up_s: float) -> bool:
    """是否需要自动「追到最新画面」（纯函数，便于离线测试）。

    为什么要追：实测同一直播间经本机代理播放，播放器位置增长比墙上时间慢约
    **0.16 秒/分钟**（FLV）/ 0.07（HLS）——「越看越落后」是播放端固有的（音视频同步与
    缓冲微调），跟代理转发无关；累计几分钟就会明显落后于网页端。累计落后达到
    ``limit_s`` 时重新拉流一次（一次短暂重载）把它压回阈值内。

    约束：正在播放、未暂停、距离装载够久（``DRIFT_SYNC_GRACE_S``）、距离上次追边够久
    （``DRIFT_MIN_INTERVAL_S``）；``limit_s <= 0`` 表示用户关掉了自动追边。
    """
    if limit_s <= 0 or not playing or paused:
        return False
    if since_sync_s < DRIFT_SYNC_GRACE_S or since_catch_up_s < DRIFT_MIN_INTERVAL_S:
        return False
    return drift_s >= limit_s


def format_duration(seconds: int) -> str:
    """秒 → ``"12:34"`` / ``"1:02:03"``（纯函数，便于离线测试）。"""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


ERROR_HINTS: Tuple[Tuple[str, str], ...] = (
    ("未开播", "主播当前未开播（开播后点「刷新」即可）"),
    ("不存在", "直播间不存在（房间号可能有误）"),
    ("-101", "账号未登录：点主界面「获取Cookie」登录后重试"),
    ("-400", "请求被拒绝（房间号或清晰度不被接受）"),
    ("-352", "触发风控，请稍后再试或重新获取 Cookie"),
    ("-403", "触发风控（无权访问该房间）"),
    ("-412", "触发风控（请求被拦截）"),
    ("Timeout", "网络超时，请稍后重试"),
    ("timeout", "网络超时，请稍后重试"),
    ("ClientError", "网络请求失败，请检查网络后重试"),
)
"""拉流失败的「原因片段 → 中文提示」，按顺序匹配（纯数据，便于离线测试）。"""


def describe_preview_error(errors, *, encrypted: Optional[bool] = None,
                           pwd_verified: Optional[bool] = None,
                           pwd_used: bool = False) -> str:
    """把拉流失败信息翻译成**一句可操作的中文**（纯函数，便于离线测试）。

    ``errors`` 是 ``{格式: 原因}``（可能为空）；``encrypted`` / ``pwd_verified`` 来自
    ``getRoomPlayInfo``（``None`` = 未知）。优先级：**加密未解锁** > 已知错误码 > 原始信息。
    只把 ``pwd_verified is False`` 当作「密码不对」——官方文档说明该字段仅在加密房间下有
    意义，非加密房间下不同接口给的默认值都不一致，拿它当判断依据会误报。
    """
    if encrypted and pwd_verified is False:
        if pwd_used:
            return "密码未通过，请右键该格「输入密码…」重新输入"
        return "该房间是加密直播间，请右键该格「输入密码…」解锁后观看"
    text = "；".join(f"{key}: {value}" for key, value in (errors or {}).items())
    if not text:
        return "服务端未返回可播放地址（可能未开播、为付费直播间或需要登录）"
    for needle, hint in ERROR_HINTS:
        if needle in text:
            return hint
    return text


def grid_shape(count: int) -> Tuple[int, int]:
    """宫格行列数（纯函数）：0→(0,0)、1→(1,1)、2→(1,2)、3~4→(2,2)。

    2 路左右分栏比上下分栏更符合直觉（宽屏），3 路也用 2×2 留一格空位（比 1×3 更均称）。
    """
    if count <= 0:
        return (0, 0)
    if count == 1:
        return (1, 1)
    columns = 2 if count <= 4 else int(math.ceil(math.sqrt(count)))
    return (int(math.ceil(count / columns)), columns)


def pick_victim_index(count: int, main_index: int) -> Optional[int]:
    """资源超限时要停掉**哪一路**（纯函数）：最后一个非主路。

    主路是用户正在看的那一路（有声音），最后加入的副路信息量最低——先停它。
    只有一路（或索引越界）时返回 ``None``：不值得为了省资源把主路也停掉。
    """
    if count < 2:
        return None
    for index in range(count - 1, -1, -1):
        if index != main_index:
            return index
    return None


def process_cpu_ratio(delta_cpu: float, delta_wall: float) -> float:
    """进程 CPU 占用率 = CPU 时间增量 / 墙上时间增量（纯函数；多核可 > 1）。"""
    if delta_wall <= 0:
        return 0.0
    return max(0.0, float(delta_cpu) / float(delta_wall))


class PreviewTile(QFrame):
    """宫格里的**一路**：自己的播放器、代理与运行状态。

    状态（优先格式、格式回退是否用过、重连预算、宽限期起点）全部收在这里，窗口只负责
    编排：加入 / 移除、主路与声音、资源保护。点击格子会发出 :attr:`clicked`，由窗口
    把它设为主路。
    """

    clicked = Signal(int)
    """被点击（参数为 tile 序号）：用于切换主路。"""

    dblclicked = Signal(int)
    """被双击（参数为 tile 序号）：该路独占窗口 / 还原。"""

    def __init__(self, index: int, window: "PreviewWindow") -> None:
        super().__init__(window)
        self.index = index
        self.window = window
        self.host = window.host
        self.room_id: Optional[int] = None
        self.proxy: Optional[LiveStreamProxy] = None
        self.prefer = FORMATS[0]      # 当前优先格式；播放出错时换另一种重试一次
        self.tried_fallback = False
        self.paused = False
        """是否处于「暂停」（画面定格，但预览仍在进行、代理未释放）。"""
        self.paused_at = 0.0
        self.loaded_at = 0.0
        """最近一次把源交给播放器的时刻（monotonic）：宽限期与重连节流都用它。"""
        self.auto_reloads = 0
        self.health_gave_up = False
        """是否已就「自动重连用尽」提示过（只提示一次，避免每 5 秒刷屏）。"""
        self.server_qualities: Tuple[int, ...] = ()
        """该房间**实际可用**的清晰度（接口的 ``accept_qn``）：非空时后续换源不再重复请求。"""
        self.pwd = ""
        """加密房间的密码：**只存在内存里**——切房、停止预览或退出程序即丢弃，不写入配置。"""
        self.encrypted: Optional[bool] = None
        """该房间是否为加密（密码）直播间；``None`` = 还不知道。"""
        self.need_info = True
        """下次拉流是否重新问一次「可用档位 + 加密状态」（首次 / 切房 / 输入密码后为真）。"""
        self._sync_at = 0.0
        """落后测量的基线时刻（monotonic）；0 = 尚无基线（下次检查时重新建立）。"""
        self._sync_pos = 0
        """基线时刻的播放器位置（毫秒）。"""
        self._caught_up_at = 0.0
        """上次自动追边的时刻（monotonic），用于限制追边频率。"""
        self.actual_qn = 0
        """最近一次实际请求到的档位（「自动」时用于在标题栏显示真实值）。"""
        self.is_main = False
        self._status = ""
        self._status_error = False
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.video = QVideoWidget()
        self.video.setMinimumSize(200, 112)
        self.video.setStyleSheet("background:#000000;")
        outer.addWidget(self.video, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 2, 0)
        bar.setSpacing(2)
        self.label = QLabel("")
        self.label.setStyleSheet("color:#b8b8b8; font-size:11px;")
        bar.addWidget(self.label, 1)
        self.close_btn = QToolButton()
        self.close_btn.setText("✕")
        self.close_btn.setToolTip("停止这一路（不影响其它路）")
        self.close_btn.clicked.connect(self._on_close_clicked)
        bar.addWidget(self.close_btn, 0)
        outer.addLayout(bar)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(self._on_player_error)
        self.audio.setMuted(True)   # 非主路一律静音；主路由窗口统一下发音量
        self._apply_frame_style()

    def _apply_frame_style(self) -> None:
        color = MAIN_BORDER_COLOR if self.is_main else SUB_BORDER_COLOR
        width = 2 if self.is_main else 1
        self.setStyleSheet(f"PreviewTile {{ border: {width}px solid {color}; }}")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton and self.room_id is not None:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton and self.room_id is not None:
            self.dblclicked.emit(self.index)
        super().mouseDoubleClickEvent(event)

    def _on_close_clicked(self) -> None:
        if self.room_id is not None:
            self.window.remove_room(self.room_id)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """右键格子菜单：输入密码 / 设为主路 / 放大还原 / 停止这一路。

        密码与「这一路」绑定（多路时各房间各用各的），也是加密房间的唯一入口。
        """
        if self.room_id is None:
            return
        menu = QMenu(self)
        main_action = None
        if not self.is_main:
            main_action = menu.addAction("设为主路（有声音）")
        pwd_action = menu.addAction("输入密码…" + ("（本房间已加密）" if self.encrypted else ""))
        zoom_action = menu.addAction("还原分格" if self.window.zoomed_index == self.index
                                     else "放大这一路")
        menu.addSeparator()
        stop_action = menu.addAction("停止这一路")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is main_action:
            self.window.set_main_room(self.room_id)
        elif chosen is pwd_action:
            self.prompt_password()
        elif chosen is zoom_action:
            self.window.toggle_zoom(self.index)
        elif chosen is stop_action:
            self.window.remove_room(self.room_id)

    def prompt_password(self) -> None:
        """弹输入框要密码（**只存在内存里**，不写入任何配置文件），交给窗口校验并重拉。"""
        if self.room_id is None:
            return
        text, ok = QInputDialog.getText(
            self.window, "加密直播间",
            f"房间 {self.room_id} 需要密码：\n（仅本次运行记住，不会写入配置文件）",
            QLineEdit.Password)
        if not ok or not str(text).strip():
            return
        self.window.submit_password(self, str(text).strip())

    # ---------- 对外状态 ----------

    @property
    def active(self) -> bool:
        return self.room_id is not None

    @property
    def playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def set_main(self, is_main: bool) -> None:
        """标记是否为主路（边框高亮；声音由窗口统一设置）。"""
        self.is_main = bool(is_main)
        self._apply_frame_style()
        self._refresh_label()

    def apply_audio(self, *, muted: bool, volume: int) -> None:
        """只有主路出声：副路一律静音。"""
        if self.is_main:
            self.audio.setMuted(bool(muted))
            self.audio.setVolume(max(0, min(100, int(volume))) / 100.0)
        else:
            self.audio.setMuted(True)

    def set_status(self, text: str, *, error: bool = False) -> None:
        self._status = text
        self._status_error = bool(error)
        self._refresh_label()
        if error:
            log_live.warning("预览状态（第 %s 路）：%s", self.index + 1, text)
        else:
            log_live.debug("预览状态（第 %s 路）：%s", self.index + 1, text)

    def _refresh_label(self) -> None:
        if self.room_id is None:
            self.label.setText("")
            self.label.setStyleSheet("color:#b8b8b8; font-size:11px;")
            return
        room_id = self.room_id
        anchor = (getattr(self.host, "anchor_names", {}) or {}).get(room_id) or "未知主播"
        parts = [f"{'● ' if self.is_main else ''}房间 {room_id} · {anchor}"]
        if self.actual_qn:
            parts.append(quality_text(self.actual_qn))
        if self._status:
            parts.append(self._status)
        self.label.setText(" · ".join(parts))
        color = "#e74c3c" if self._status_error else "#b8b8b8"
        self.label.setStyleSheet(f"color:{color}; font-size:11px;")

    # ---------- 生命周期 ----------

    def start(self, room_id: int) -> None:
        """开始（或重开）这一路的播放。"""
        self.room_id = int(room_id)
        self.paused = False
        self.paused_at = 0.0
        self.prefer = FORMATS[0]
        self.tried_fallback = False
        self.auto_reloads = 0
        self.health_gave_up = False
        self.loaded_at = time.monotonic()
        self.server_qualities = ()
        self.actual_qn = 0
        self.pwd = ""            # 切房即忘记上一间的密码（也不落盘）
        self.encrypted = None
        self.need_info = True
        self.set_status("正在获取直播流…")
        self.request_prepare(restart=True)

    def stop(self) -> None:
        """停止这一路：停播放、回收代理（代理停止会 join 线程，丢到后台做）。"""
        was_active = self.room_id is not None
        room_id = self.room_id
        self.room_id = None
        self.paused = False
        self.paused_at = 0.0
        self.auto_reloads = 0
        self.health_gave_up = False
        self.server_qualities = ()
        self.actual_qn = 0
        try:
            self.player.stop()
            self.player.setSource(QUrl())
        except Exception:  # 播放器异常不应阻断收尾
            log_live.debug("停止第 %s 路播放器时出错", self.index + 1, exc_info=True)
        proxy, self.proxy = self.proxy, None
        if proxy is not None:
            self.host.hub.submit(self._async_stop_proxy(proxy))
        self._status = ""
        self._status_error = False
        self._refresh_label()
        if was_active:
            log_live.info("停止预览第 %s 路（房间 %s）", self.index + 1, room_id)

    def toggle_pause(self) -> None:
        """暂停 / 继续（画面定格，预览不结束、代理不释放）。"""
        if self.room_id is None:
            return
        if self.paused:
            self._resume()
            return
        self.player.pause()
        self.paused = True
        self.paused_at = time.monotonic()
        self.set_status("已暂停（点「继续」恢复）")
        log_window.info("预览已暂停（房间 %s）", self.room_id)

    def _resume(self) -> None:
        """继续播放：暂停久了（或期间断流）就重新拉流跳到最新画面。"""
        self.paused = False
        lag = (time.monotonic() - self.paused_at) if self.paused_at else 0.0
        self.paused_at = 0.0
        if self.player.mediaStatus() in (QMediaPlayer.NoMedia,
                                         QMediaPlayer.InvalidMedia,
                                         QMediaPlayer.StalledMedia):
            self.set_status("暂停期间流已断开，正在重新拉流…")
            self.request_prepare(restart=True)
            return
        if lag > PAUSE_RESYNC_S:
            self.set_status(f"暂停了 {lag:.0f} 秒，正在跳到最新画面…")
            log_window.info("预览暂停 %.0f 秒后继续：重新拉流跳最新（房间 %s）", lag,
                            self.room_id)
            self.request_prepare(restart=True)
            return
        self.player.play()
        self.set_status("播放中")
        log_window.info("预览已继续播放（房间 %s）", self.room_id)

    def reload(self) -> None:
        """重新拉流并重新装载，跳到最新画面（追回暂停 / 卡顿造成的滞后）。"""
        if self.room_id is None:
            return
        self.paused = False
        self.paused_at = 0.0
        self.tried_fallback = False
        self.prefer = FORMATS[0]
        self.auto_reloads = 0        # 手动刷新 = 重新给自动重连预算
        self.health_gave_up = False
        self.loaded_at = time.monotonic()
        self._sync_at = 0.0     # 重新装载：落后基线作废，播放稳定后重新建立
        self.set_status("正在刷新到最新画面…")
        log_window.info("预览手动刷新（第 %s 路，房间 %s）", self.index + 1, self.room_id)
        self.request_prepare(restart=True)

    def request_prepare(self, *, restart: bool) -> None:
        """提交后台拉流任务（失败/就绪都经队列回投主线程）。"""
        room_id = self.room_id
        if room_id is None:
            return
        self.host.hub.submit(self._async_prepare(room_id, restart=restart))

    # ---------- 队列回调（主线程） ----------

    def on_streams_ready(self, payload: dict) -> None:
        """后台准备好流：记录可用档位，再把本地代理地址交给播放器。"""
        if self.room_id != int(payload.get("room_id") or 0):
            return  # 已换房 / 已停止：丢弃过期结果
        qualities = tuple(payload.get("accept_quality") or ())
        if qualities:
            self.server_qualities = qualities
            if self.is_main:
                self.window.on_main_qualities(self)  # 主路的可用档位决定下拉内容
        play = payload.get("play") or {}
        url = play.get(self.prefer) or play.get("hls") or play.get("flv")
        if not url:
            self.set_status("无法预览：" + describe_preview_error(
                payload.get("errors"), encrypted=self.encrypted), error=True)
            return
        kind = "HLS" if url.endswith("m3u8") else "FLV"
        self.actual_qn = int(payload.get("qn") or self.window.quality)
        self.set_status(f"播放中（{kind}）")
        restart = bool(payload.get("restart")) or not self.playing
        if not restart:
            return  # 定时换源：上游已替换，HLS 播放器不用动（无缝续播）
        # 本地代理地址是固定的：直接 setSource 同一个 URL **不会重新加载**（表现就是黑屏），
        # 必须先清空再设置，强制播放器重新拉流——换清晰度 / 换格式都走这条路径
        self.paused = False  # 重新装载后即回到播放状态（若之前是暂停）
        self.loaded_at = time.monotonic()  # 装载即重置宽限期（起播/缓冲不计为断流）
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setSource(QUrl(url))
        self.player.play()
        log_live.info("预览第 %s 路开始播放（房间 %s，%s，清晰度 %s）",
                      self.index + 1, self.room_id, kind, quality_text(self.actual_qn))
        log_live.debug("预览第 %s 路流就绪（房间 %s，档位 %s，%s）", self.index + 1,
                       self.room_id, quality_text(self.actual_qn),
                       "重新装载" if restart else "仅换上游（不重载）")

    def on_stream_error(self, payload: dict) -> None:
        """后台拉流失败：翻成一句可操作的中文（未开播 / 加密 / 风控 / 网络…）。"""
        if self.room_id != int(payload.get("room_id") or 0):
            return
        if payload.get("encrypted"):
            self.encrypted = True
            self.set_status("该房间是加密直播间——右键此格「输入密码…」解锁", error=True)
            log_live.warning("预览失败（房间 %s）：加密直播间，需要密码", self.room_id)
            return
        message = str(payload.get("message") or "未知错误")
        self.set_status("无法预览：" + describe_preview_error({"stream": message}), error=True)
        log_live.warning("预览失败（房间 %s）：%s", self.room_id, message)

    # ---------- 定时任务（由窗口驱动） ----------

    def refresh_stream(self) -> None:
        """定时换源：直链过期前替换上游，播放器（HLS）无需重连。"""
        if self.room_id is None:
            return
        self.request_prepare(restart=False)

    def health_tick(self) -> None:
        """断流自愈：在播、未暂停、却停住了 → 自动重新拉流。

        主路最多重连 ``HEALTH_MAX_AUTO_RELOADS`` 次（用尽后提示手动刷新）；副路只重试
        ``HEALTH_MAX_RELOADS_SUB`` 次，仍不行就**停掉该路**（把资源让给主路）。
        """
        if self.room_id is None:
            return
        if self.playing:
            self.auto_reloads = 0  # 播放正常：重试预算恢复（下次断了还能自动救）
            self.health_gave_up = False
            self._check_drift()    # 播放中才需要测「越看越落后」
            return
        self._sync_at = 0.0        # 没在播放：落后基线作废（下次播放时重新建立）
        if self.player.mediaStatus() == QMediaPlayer.LoadingMedia:
            return  # 正在装载（换源 / 手动刷新）：还不算断流
        since_load = time.monotonic() - self.loaded_at
        limit = HEALTH_MAX_AUTO_RELOADS if self.is_main else HEALTH_MAX_RELOADS_SUB
        if needs_auto_reload(active=True, paused=self.paused,
                             visible=self.window.isVisible(), playing=False,
                             live=self.window.is_live(self.room_id),
                             since_load=since_load, auto_reloads=self.auto_reloads,
                             max_reloads=limit):
            self.auto_reloads += 1
            self.loaded_at = time.monotonic()  # 节流：下次判定至少隔一个宽限期
            log_live.warning("预览播放中断（房间 %s），自动重新拉流（第 %s 次）",
                             self.room_id, self.auto_reloads)
            self.set_status(f"播放中断，正在自动重连（第 {self.auto_reloads} 次）…")
            self.request_prepare(restart=True)
            return
        if (self.auto_reloads < limit or since_load < HEALTH_GRACE_S
                or self.paused or not self.window.isVisible()):
            return
        if not self.is_main:
            self.window.drop_tile_for_failure(self, "持续无法播放")
            return
        if not self.health_gave_up:  # 主路：只提示一次，等用户手动刷新
            self.health_gave_up = True
            log_live.warning("预览自动重连 %s 次仍未恢复（房间 %s），等待手动刷新",
                             self.auto_reloads, self.room_id)
            self.set_status("播放已中断，自动重连未成功——请点「刷新」重试", error=True)

    def _check_drift(self) -> None:
        """播放落后检查：累计落后超过阈值就重新拉流跳到最新（见 :func:`needs_catch_up`）。

        实测落后约 0.1~0.2 秒/分钟，累积到阈值（默认 3 秒）约需十几分钟——一次短暂重载即可
        追回，随后重建基线继续测。把 ``preview.max_drift_sec`` 设为 ``0`` 可关闭自动追边。
        """
        limit = self.window.max_drift
        if limit <= 0:
            return
        now = time.monotonic()
        pos = self.player.position()
        if self._sync_at == 0.0:
            self._sync_at, self._sync_pos = now, pos
            return
        elapsed = now - self._sync_at
        drift = elapsed - (pos - self._sync_pos) / 1000.0
        if not needs_catch_up(playing=self.playing, paused=self.paused, drift_s=drift,
                              limit_s=limit, since_sync_s=elapsed,
                              since_catch_up_s=now - self._caught_up_at):
            return
        self._caught_up_at = now
        self._sync_at, self._sync_pos = now, pos   # 重建基线，避免立刻再次触发
        log_live.info("预览落后 %.1f 秒，自动追到最新画面（房间 %s）", drift, self.room_id)
        self.set_status(f"已落后 {drift:.1f} 秒，正在追到最新画面…")
        self.reload()

    def _on_player_error(self, error, error_string: str) -> None:
        if self.room_id is None:
            return  # 主动 stop() 产生的空源报错：忽略
        log_live.warning("预览播放出错（房间 %s）：%s（%s）", self.room_id, error_string, error)
        if not self.tried_fallback:
            # 只回退一次：换另一种格式重试（HLS ⇄ FLV）
            self.tried_fallback = True
            self.prefer = "flv" if self.prefer == "hls" else "hls"
            self.set_status(f"播放失败（{error_string}），改用 {self.prefer.upper()} 重试…",
                            error=True)
            self.request_prepare(restart=True)
            return
        self.set_status(f"播放失败：{error_string}", error=True)

    # ---------- 后台任务（hub 线程） ----------

    async def _async_prepare(self, room_id: int, *, restart: bool) -> None:
        """后台：拉直链 + 起/更新代理，再把可播放的本地地址投回主线程。"""
        host = self.host
        queue = host.ui_queue
        api = host.hub.api
        if api is None:
            queue.put(("preview_error", {"room_id": room_id, "message": "后台尚未就绪"}))
            return
        wanted = self.window.quality  # 全局清晰度；后台协程不读会被主线程改动的状态
        pwd = self.pwd                # 加密房间的密码（只在内存里）
        try:
            real_room = int(host.real_room_id(room_id))
            # 「可用档位 + 是否加密」：首次 / 切房 / 输入密码后问一次，其余换源沿用缓存
            # （省掉每次换源都多打一次接口；房间档位不会频繁变化）
            qualities = self.server_qualities
            encrypted = self.encrypted
            verified: Optional[bool] = None
            if self.need_info or not qualities:
                info = await api.get_stream_info(real_room, pwd=pwd)
                self.need_info = False
                if info.get("qualities"):
                    qualities = tuple(info["qualities"])
                    self.server_qualities = qualities
                encrypted = info.get("encrypted")
                verified = info.get("pwd_verified")
                self.encrypted = encrypted
            # 加密房间且**明确**没通过密码：不再白拉一次流，直接把原因带回界面
            if encrypted and verified is False:
                queue.put(("preview_error", {
                    "room_id": room_id,
                    "encrypted": True,
                    "pwd_used": bool(pwd),
                    "message": "encrypted"}))
                return
            qn = pick_quality(wanted, qualities)
            result = await api.get_live_stream_urls(real_room, qn=qn, pwd=pwd)
            proxy = self.proxy
            if proxy is None:
                proxy = LiveStreamProxy(cookie=getattr(api, "_cookie", "") or "")
                self.proxy = proxy
                proxy.start()  # 阻塞等监听就绪（≤10s）；已经在后台线程里
            proxy.set_streams(flv=result.get("flv"), hls=result.get("hls"))
            play = proxy.play_urls
        except Exception as exc:  # 网络/接口/代理启动异常：交给界面提示
            log_live.warning("预览拉流失败（房间 %s）：%s", room_id, exc, exc_info=True)
            queue.put(("preview_error", {
                "room_id": room_id,
                "message": f"{type(exc).__name__}: {exc}"}))
            return
        queue.put(("preview_ready", {
            "room_id": room_id,
            "play": play,
            "errors": result.get("errors") or {},
            "restart": bool(restart),
            "qn": qn,
            "accept_quality": list(qualities),
        }))

    async def _async_stop_proxy(self, proxy: LiveStreamProxy) -> None:
        try:
            proxy.stop()
        except Exception:
            log_live.debug("停止预览代理时出错", exc_info=True)


class PreviewWindow(QWidget):
    """多路预览浮窗：宫格布局 + 主路声音 + 资源保护（可缩放 / 置顶 / 拖动）。"""

    def __init__(self, host) -> None:
        super().__init__(None, Qt.Window)
        self.host = host
        self.app_config = host.app_config
        config = host.app_config.preview
        self.quality = int(config.quality)
        """全局清晰度（下拉作用于所有路；每路按自己房间的可用档位落到实处）。"""
        remembered = (getattr(host, "ui_prefs", None) or {}).get("preview_quality")
        if (isinstance(remembered, int) and not isinstance(remembered, bool)
                and remembered in QUALITY_TEXTS):
            self.quality = remembered   # 上次在浮窗里选过的档位优先（P4 清晰度记忆）
        self.max_rooms = max(1, min(PREVIEW_MAX_ROOMS, int(getattr(config, "max_rooms",
                                                                  PREVIEW_MAX_ROOMS))))
        self.max_drift = max(0.0, float(getattr(config, "max_drift_sec",
                                                DEFAULT_PREVIEW_MAX_DRIFT)))
        """允许的播放落后上限（秒）：累计超过就自动追到最新画面；``0`` = 关闭自动追边。"""
        self._tiles: List[PreviewTile] = []
        self._main_index = 0
        self._zoomed_index: Optional[int] = None
        """被「放大」（双击格子）独占窗口的那一路；``None`` = 正常分格显示。"""
        self._mute = bool(config.mute)
        self._volume = max(0, min(100, int(config.volume)))
        self._refresh_timer: Optional[QTimer] = None
        self._health_timer: Optional[QTimer] = None
        self._cpu_timer: Optional[QTimer] = None
        self._cpu_hits = 0
        self._cpu_sample = (time.monotonic(), time.process_time())
        self._watch_timer: Optional[QTimer] = None
        self._watch_enabled = False
        self._watch_reported = 0
        self._watch_next = LIVE_HEARTBEAT_DEFAULT_INTERVAL
        self._watch_room: Optional[int] = None

        self._build_ui()
        self._apply_config(config)
        self.refresh_qualities()
        self.setWindowTitle("直播预览")
        self.resize(*DEFAULT_WINDOW_SIZE)
        log_live.info("预览浮窗已创建（最多 %s 路，清晰度 %s，静音 %s，置顶 %s）",
                      self.max_rooms, quality_text(self.quality), self._mute,
                      config.always_on_top)

    # ---------- UI ----------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        head = QHBoxLayout()
        self.title_label = QLabel("直播预览")
        head.addWidget(self.title_label, 1)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#888888;")
        head.addWidget(self.status_label, 0)
        outer.addLayout(head)

        grid_host = QWidget()
        self.grid = QGridLayout(grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        outer.addWidget(grid_host, 1)
        for index in range(self.max_rooms):
            tile = PreviewTile(index, self)
            tile.clicked.connect(self._set_main)
            tile.dblclicked.connect(self.toggle_zoom)
            tile.setVisible(False)
            self._tiles.append(tile)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.reload_btn = QPushButton("刷新")
        self.reload_btn.setToolTip(
            "重新拉流并跳到最新画面（作用于**主路**）：直播暂停后继续只从暂停处接着播、\n"
            "画面会比真实直播晚，点这里立刻追回。")
        self.reload_btn.clicked.connect(self._on_reload_clicked)
        self.play_btn = QPushButton("播放")
        self.play_btn.setToolTip("暂停 / 继续**主路**（暂停只定格画面，预览继续、代理不释放）。\n"
                                "停止某一路请点该格的「✕」，停止全部请关窗或用工具栏「停止预览」。")
        self.play_btn.clicked.connect(self._on_pause_clicked)
        controls.addWidget(self.reload_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(QLabel("清晰度："))
        self.quality_combo = QComboBox()
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        controls.addWidget(self.quality_combo)
        self.mute_check = QCheckBox("静音")
        self.mute_check.setToolTip("只影响主路；副路始终静音（避免多路声音混在一起）。")
        self.mute_check.toggled.connect(self._on_mute_toggled)
        controls.addWidget(self.mute_check)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        controls.addWidget(self.volume_slider)
        self.top_check = QCheckBox("置顶")
        self.top_check.toggled.connect(self._on_top_toggled)
        controls.addWidget(self.top_check)
        # 观看时长上报（ROADMAP 63 · P2，写操作，默认关闭；只对主路上报）
        self.watch_check = QCheckBox("上报观看时长")
        self.watch_check.setToolTip(
            "开启后，预览播放期间每隔约 1 分钟向 B 站上报一次「观看时长」心跳（只对**主路**），\n"
            "让账号在该直播间累计观看时长。需要满足：config.json 的 allow_write_operations\n"
            "与 preview.watch_time 均为 true，且已登录。\n"
            "注意：这是模拟网页端行为的高风险写操作，可能触发风控，请自行评估。")
        self.watch_check.toggled.connect(self._on_watch_toggled)
        controls.addWidget(self.watch_check)
        self.watch_label = QLabel("")
        self.watch_label.setStyleSheet("color:#888888;")
        controls.addWidget(self.watch_label)
        controls.addStretch(1)
        outer.addLayout(controls)

        note = QHBoxLayout()
        self.quality_note = QLabel("")
        self.quality_note.setStyleSheet("color:#b06000; font-size:11px;")
        note.addWidget(self.quality_note, 1)
        self.hint_label = QLabel("点格子设为主路（有声音）；双击格子放大/还原；每格「✕」停该路")
        self.hint_label.setStyleSheet("color:#888888; font-size:11px;")
        note.addWidget(self.hint_label, 0)
        outer.addLayout(note)

    def _apply_config(self, config) -> None:
        """按 config.json 的 ``preview`` 段设置默认值（静音 / 音量 / 置顶）。"""
        self.mute_check.setChecked(self._mute)
        self.volume_slider.setValue(self._volume)
        self.volume_slider.setEnabled(not self._mute)
        self.top_check.setChecked(bool(config.always_on_top))
        if config.always_on_top:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        # 观看时长上报：config 想开、写操作总开关允许、且已登录才默认勾上；
        # 否则把原因写在旁边（用户至少知道为什么没生效，不必去翻配置）
        if config.watch_time:
            reason = self._watch_block_reason()
            if reason is None:
                self.watch_check.setChecked(True)  # 触发 toggled → 开启并排首次上报
            else:
                self.watch_label.setText(f"（未开启上报：{reason}）")

    # ---------- 对外接口（主界面用） ----------

    def is_active(self) -> bool:
        """是否正在预览（至少有一路）。"""
        return bool(self.room_ids())

    def room_ids(self) -> List[int]:
        """当前预览的房间号（按格子顺序）。"""
        return [tile.room_id for tile in self._tiles if tile.room_id is not None]

    @property
    def room_id(self) -> Optional[int]:
        """**主路**房间号；未预览时为 ``None``（主界面沿用这个属性判断状态）。"""
        tile = self._main_tile()
        return tile.room_id if tile is not None else None

    @property
    def main_room_id(self) -> Optional[int]:
        return self.room_id

    def start(self, room_id: int) -> bool:
        """加入一路预览；已在该路则只把它设为主路。满路时拒绝并提示。"""
        room_id = int(room_id)
        existing = self._tile_of(room_id)
        if existing is not None:
            self._set_main(existing.index)
            return True
        tile = self._free_tile()
        if tile is None:
            self._set_status(f"最多同时预览 {self.max_rooms} 路，请先关掉一路", error=True)
            log_live.warning("预览已达上限 %s 路，忽略房间 %s", self.max_rooms, room_id)
            return False
        self._show_window()
        tile.start(room_id)
        self._relayout()
        self._set_main(tile.index)
        self._start_timers()
        self._update_title()
        log_live.info("加入预览第 %s 路（房间 %s，共 %s 路）",
                      tile.index + 1, room_id, len(self.room_ids()))
        self.host.refresh_preview_button()
        return True

    def remove_room(self, room_id: int) -> bool:
        """停止某一路（不影响其它路）；最后一路被停掉时关窗。"""
        tile = self._tile_of(int(room_id))
        if tile is None:
            return False
        was_main = tile.is_main
        tile.stop()
        if not self.is_active():
            self.stop(reason="已停止")
            return True
        # 无论停的是主路还是副路都要重排：被停掉的那格必须隐藏、其它格重新分布
        # （`_zoomed_index` 指向它时 `_relayout` 会自动退出放大）
        self._relayout()
        if was_main:
            self._set_main(self._active_tiles()[0].index)
        else:
            self._apply_audio()
            self._update_title()
            self._sync_controls()
        self.host.refresh_preview_button()
        return True

    def set_main_room(self, room_id: int) -> bool:
        """把某一路设为主路（有声音）。"""
        tile = self._tile_of(int(room_id))
        if tile is None:
            return False
        self._set_main(tile.index)
        return True

    def stop(self, *, reason: str = "", close: bool = True) -> None:
        """**停止全部预览**：停所有路的播放与代理，并关闭浮窗（默认）。

        「暂停」是另一回事（浮窗内的按钮，只定格画面）；这里的 stop 对应工具栏 / 右键的
        「停止预览」与关窗。``close=False`` 只给 ``closeEvent`` 自己用，避免「关闭 → 停止
        → 再关闭」递归；窗口关掉后对象仍可复用（再次 ``start`` 会重新 show）。
        """
        was_active = self.is_active()
        self._stop_timers()
        self._watch_room = None
        for tile in self._tiles:
            tile.stop()
        self._relayout()
        self.title_label.setText("直播预览")
        self._set_status(reason or ("已停止" if was_active else ""))
        if was_active:
            log_live.info("停止预览%s", f"（{reason}）" if reason else "")
        if close and self.isVisible():
            self.close()  # 停止即收窗：不再留一个空浮窗在屏幕上

    def refresh_qualities(self) -> None:
        """按登录态 + 主路房间可用档位刷新下拉（未登录只有 ≤720P）。"""
        self._update_quality_choices()

    def on_main_qualities(self, tile: PreviewTile) -> None:
        """主路拿到了该房间的可用档位 → 收缩下拉（只列这个房间真有的档位）。"""
        if tile is self._main_tile():
            self._update_quality_choices()

    # ---------- 队列回调（主线程） ----------

    def on_streams_ready(self, payload: dict) -> None:
        """把后台结果分发给对应的一路（按房间号匹配，避免串路）。"""
        room_id = int(payload.get("room_id") or 0)
        tile = self._tile_of(room_id)
        if tile is not None:
            tile.on_streams_ready(payload)

    def on_stream_error(self, payload: dict) -> None:
        room_id = int(payload.get("room_id") or 0)
        tile = self._tile_of(room_id)
        if tile is not None:
            tile.on_stream_error(payload)
        else:
            log_live.debug("忽略过期预览错误（房间 %s）", room_id)

    def drop_tile_for_failure(self, tile: PreviewTile, reason: str) -> None:
        """资源保护：停掉某一路（副路重连失败 / CPU 超限）并提示。"""
        room_id = tile.room_id
        if room_id is None:
            return
        log_live.warning("自动停掉预览第 %s 路（房间 %s）：%s",
                         tile.index + 1, room_id, reason)
        self._set_status(f"已停掉第 {tile.index + 1} 路（房间 {room_id}）：{reason}", error=True)
        self.remove_room(room_id)

    @property
    def zoomed_index(self) -> Optional[int]:
        """当前被「放大」独占窗口的那一路序号（``None`` = 正常分格）。"""
        return self._zoomed_index

    def toggle_zoom(self, index: int) -> None:
        """放大 / 还原某一路（双击格子与右键菜单都走它）——多路时快速看细节。

        放大只影响显示（主路与声音都不变）；被放大的那一格若被停掉，会自动退出放大
        （见 :meth:`_relayout`）。
        """
        tile = self._tile_at(index)
        if tile is None or not tile.active:
            return
        self._zoomed_index = None if self._zoomed_index == index else index
        self._relayout()
        self._update_title()
        if self._zoomed_index is None:
            self._set_status("已还原分格显示")
        else:
            self._set_status(f"第 {index + 1} 路已放大（双击该格还原分格）")

    # ---------- 主路 / 声音 ----------

    def _set_main(self, index: int) -> None:
        """把第 ``index`` 路设为主路（有声音、下拉与按钮作用于它）。"""
        tile = self._tile_at(index)
        if tile is None or not tile.active:
            return
        self._main_index = index
        for item in self._tiles:
            item.set_main(item.index == index)
        self._apply_audio()
        self._reset_watch_counters()
        self._update_quality_choices()
        self._sync_controls()
        self._update_title()
        log_window.info("预览主路切换为第 %s 路（房间 %s）", index + 1, tile.room_id)

    def _apply_audio(self) -> None:
        for tile in self._tiles:
            tile.apply_audio(muted=self._mute, volume=self._volume)

    def _reset_watch_counters(self) -> None:
        """主路变化：观看时长按新房间重新累计（同一时间只上报一个房间）。"""
        self._watch_reported = 0
        self._watch_next = LIVE_HEARTBEAT_DEFAULT_INTERVAL
        self._watch_room = None
        if self._watch_enabled:
            self.watch_label.setText(f"已上报 {format_duration(0)}")

    # ---------- 工具 ----------

    def _tile_at(self, index: int) -> Optional[PreviewTile]:
        return self._tiles[index] if 0 <= index < len(self._tiles) else None

    def _tile_of(self, room_id: int) -> Optional[PreviewTile]:
        for tile in self._tiles:
            if tile.room_id is not None and int(tile.room_id) == int(room_id):
                return tile
        return None

    def _free_tile(self) -> Optional[PreviewTile]:
        for tile in self._tiles:
            if tile.room_id is None:
                return tile
        return None

    def _active_tiles(self) -> List[PreviewTile]:
        return [tile for tile in self._tiles if tile.room_id is not None]

    def _main_tile(self) -> Optional[PreviewTile]:
        tile = self._tile_at(self._main_index)
        if tile is not None and tile.active:
            return tile
        active = self._active_tiles()
        return active[0] if active else None

    def is_live(self, room_id: Optional[int]) -> bool:
        """主播是否在播（轮播中、状态未知都按「在播」处理——宁可多试一次也别漏自愈）。"""
        if room_id is None:
            return False
        states = getattr(self.host, "live_state", None) or {}
        return states.get(room_id) != "未开播"

    def _relayout(self) -> None:
        """按当前路数重排宫格（1 路全屏 / 2 路左右 / 3~4 路 2×2；放大时只显示那一路）。

        注意**所有**会改变「哪些格子在用」的路径都要调用它：漏掉的话被停掉的那一格不会被
        隐藏，会在窗口里留一块黑框、剩余格子也不会重排（P3 早期版本停主路时就漏了）。
        """
        zoomed = self._tile_at(self._zoomed_index) if self._zoomed_index is not None else None
        if zoomed is None or not zoomed.active:
            self._zoomed_index = None   # 放大的那一路被停掉了：自动退出放大
            zoomed = None
        if zoomed is not None:
            self.grid.addWidget(zoomed, 0, 0)
            for tile in self._tiles:
                tile.setVisible(tile is zoomed)
            return
        active = self._active_tiles()
        _rows, columns = grid_shape(len(active))
        for position, tile in enumerate(active):
            self.grid.addWidget(tile, position // columns if columns else 0,
                                position % columns if columns else 0)
            tile.setVisible(True)
        for tile in self._tiles:
            if not tile.active:
                tile.setVisible(False)

    def _show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        log_window.debug("预览浮窗显示并置前（当前 %s 路）", len(self.room_ids()))

    # ---------- 交互 ----------

    def _on_pause_clicked(self) -> None:
        """暂停 / 继续**主路**（未预览时用选中房间开始）。"""
        tile = self._main_tile()
        if tile is None:
            room_id = getattr(self.host, "_selected_room_id", None)
            if room_id is None:
                self._set_status("请先在主界面选中一个直播间", error=True)
                return
            self.start(room_id)
            return
        tile.toggle_pause()
        self._sync_controls()

    def _on_reload_clicked(self) -> None:
        """「刷新」：重新拉流并重新装载主路，跳到最新画面。"""
        tile = self._main_tile()
        if tile is None:
            self._set_status("尚未开始预览", error=True)
            return
        tile.reload()

    def _on_quality_changed(self, _index: int) -> None:
        data = self.quality_combo.currentData()
        if data is None:
            return
        qn = int(data)  # 0 = 自动（取各房间可用最高）
        if qn == self.quality:
            return
        self.quality = qn
        save = getattr(self.host, "save_preview_quality", None)
        if callable(save):
            save(qn)   # 记住这次选择（gui_rooms.json 的 ui.preview_quality），下次启动沿用
        active = self._active_tiles()
        log_window.info("预览清晰度切换为 %s（%s 路）", quality_text(qn), len(active))
        for tile in active:
            tile.tried_fallback = False
            tile.prefer = FORMATS[0]
            tile.request_prepare(restart=True)
        if active:
            self._set_status(f"切换清晰度到 {quality_text(qn)}…")
        self._update_quality_choices()

    def _on_mute_toggled(self, checked: bool) -> None:
        self._mute = bool(checked)
        self.volume_slider.setEnabled(not checked)
        self._apply_audio()
        log_window.debug("预览静音：%s", "开" if checked else "关")

    def _on_volume_changed(self, value: int) -> None:
        self._volume = max(0, min(100, int(value)))
        self._apply_audio()

    def _on_top_toggled(self, checked: bool) -> None:
        log_window.info("预览窗口置顶：%s", "开" if checked else "关")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(checked))
        if self.isVisible():
            self.show()  # 改窗口标志后需要重新 show 才生效

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        """关闭浮窗：停所有路、回收代理，并让主界面复位按钮。"""
        self.stop(reason="窗口已关闭", close=False)  # 已在关闭流程里，别再 close
        log_live.info("预览浮窗已关闭")
        self.host.refresh_preview_button()
        super().closeEvent(event)

    # ---------- 清晰度下拉 ----------

    def _update_quality_choices(self) -> None:
        """刷新清晰度下拉：只列**主路房间**真正可用的档位（拿不到时按登录态兜底）。

        用户已选的档位会一直保留在列表里（哪怕这个房间没有）——格子标题会显示该路**实际**
        播放的档位，若两者不一致还会在下方提示，避免「下拉显示 4K、其实放的是蓝光」的误解。
        """
        api = self.host.hub.api
        logged_in = bool(api is not None and api.logged_in)
        allowed = available_qualities(logged_in)
        tile = self._main_tile()
        server: List[int] = []
        if tile is not None:
            for item in tile.server_qualities:
                try:
                    qn = int(item)
                except (TypeError, ValueError):
                    continue
                if qn in allowed and qn not in server:
                    server.append(qn)
        values = sorted(set(server or allowed))
        if self.quality > AUTO_QUALITY and self.quality not in values:
            values.append(self.quality)  # 保留用户选择（下面用提示说明实际档位）
            values.sort()

        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        self.quality_combo.addItem(quality_text(AUTO_QUALITY), AUTO_QUALITY)
        for qn in values:
            self.quality_combo.addItem(quality_text(qn), qn)
        index = self.quality_combo.findData(self.quality)
        if index >= 0:
            self.quality_combo.setCurrentIndex(index)
        self.quality_combo.blockSignals(False)

        notes = []
        if not logged_in:
            notes.append("未登录：仅 720P（点主界面「获取Cookie」登录后可看更高清晰度）")
        if (self.quality > AUTO_QUALITY and server and self.quality not in server):
            notes.append(f"该房间没有「{quality_text(self.quality)}」，"
                         f"将按可用最高档播放")
        self.quality_note.setText("；".join(notes))

    def _sync_controls(self) -> None:
        """控制条状态：暂停按钮文案、格子里程碑提示。"""
        tile = self._main_tile()
        if tile is None:
            self.play_btn.setText("播放")
        else:
            self.play_btn.setText("继续" if tile.paused else "暂停")
        self.reload_btn.setEnabled(tile is not None)
        self.play_btn.setEnabled(True)

    def _update_title(self) -> None:
        count = len(self.room_ids())
        if not count:
            self.title_label.setText("直播预览")
            return
        text = f"直播预览（{count}/{self.max_rooms} 路）"
        if self._zoomed_index is not None:
            text += "（放大）"
        main = self._main_tile()
        if main is not None:
            anchor = (getattr(self.host, "anchor_names", {}) or {}).get(main.room_id) or "未知主播"
            text += f" · 主路：房间 {main.room_id}（{anchor}）"
        self.title_label.setText(text)

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:#c0392b;" if error else "color:#888888;")
        self._sync_controls()
        if error:
            log_live.warning("预览状态：%s", text)
        else:
            log_live.debug("预览状态：%s", text)

    # ---------- 定时任务 ----------

    def _start_timers(self) -> None:
        if self._refresh_timer is None:
            self._refresh_timer = QTimer(self)
            self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start(PREVIEW_REFRESH_MS)
        if self._health_timer is None:
            self._health_timer = QTimer(self)
            self._health_timer.timeout.connect(self._on_health_tick)
        self._health_timer.start(HEALTH_CHECK_MS)
        if self._cpu_timer is None:
            self._cpu_timer = QTimer(self)
            self._cpu_timer.timeout.connect(self._on_cpu_tick)
        self._cpu_timer.start(CPU_CHECK_MS)
        self._cpu_sample = (time.monotonic(), time.process_time())

    def _stop_timers(self) -> None:
        for timer in (self._refresh_timer, self._health_timer, self._cpu_timer,
                      self._watch_timer):
            if timer is not None:
                timer.stop()

    def _on_refresh_tick(self) -> None:
        """定时重新拉流换源（所有路）：直链过期前替换上游，HLS 播放器无需重连。"""
        active = self._active_tiles()
        if not active:
            return
        log_live.debug("预览定时换源（%s 路）", len(active))
        for tile in active:
            tile.refresh_stream()

    def _on_health_tick(self) -> None:
        """断流自愈（所有路）：主路自动重连，副路重试 1 次后停路。"""
        for tile in self._active_tiles():
            tile.health_tick()

    def _on_cpu_tick(self) -> None:
        """资源保护：进程 CPU 持续偏高时停掉最后加入的副路。"""
        active = self._active_tiles()
        if len(active) < 2:
            self._cpu_hits = 0
            self._cpu_sample = (time.monotonic(), time.process_time())
            return
        now, cpu = time.monotonic(), time.process_time()
        last_wall, last_cpu = self._cpu_sample
        self._cpu_sample = (now, cpu)
        ratio = process_cpu_ratio(cpu - last_cpu, now - last_wall)
        if ratio <= CPU_LIMIT:
            self._cpu_hits = 0
            return
        self._cpu_hits += 1
        log_live.warning("预览进程 CPU 偏高（%.0f%%，%s 路），连续第 %s 次",
                         ratio * 100, len(active), self._cpu_hits)
        if self._cpu_hits < CPU_LIMIT_HITS:
            return
        self._cpu_hits = 0
        main = self._main_tile()
        position = active.index(main) if main in active else 0
        victim = pick_victim_index(len(active), position)
        if victim is None:
            return
        self.drop_tile_for_failure(active[victim],
                                   f"CPU 占用偏高（{ratio * 100:.0f}%）")

    # ---------- 加密（密码）直播间（ROADMAP 63 · P4） ----------

    def submit_password(self, tile: PreviewTile, pwd: str) -> None:
        """某一路输入了密码：先让服务端解锁（尽力而为），再带密码重拉这一路。

        密码只存在这一路的内存里（`tile.pwd`），切房 / 停止 / 退出即丢弃，不写入配置。
        """
        room_id = tile.room_id
        pwd = str(pwd or "").strip()
        if not pwd or room_id is None:
            return
        tile.pwd = pwd
        tile.need_info = True
        tile.set_status("正在校验密码…")
        log_live.info("预览第 %s 路（房间 %s）提交密码", tile.index + 1, room_id)
        self.host.hub.submit(self._async_unlock(room_id, pwd))

    async def _async_unlock(self, room_id: int, pwd: str) -> None:
        """后台：调密码校验接口（官方文档未收录，失败不致命），结果回投主线程。"""
        host = self.host
        queue = host.ui_queue
        api = host.hub.api
        if api is None:
            queue.put(("preview_unlocked", {"room_id": room_id, "ok": False}))
            return
        try:
            ok = await api.verify_room_pwd(int(host.real_room_id(room_id)), pwd)
        except Exception:
            log_live.warning("密码校验异常（房间 %s）", room_id, exc_info=True)
            ok = False
        queue.put(("preview_unlocked", {"room_id": room_id, "ok": bool(ok)}))

    def on_preview_unlocked(self, payload: dict) -> None:
        """密码校验回来：**无论是否通过都带密码重拉一次**（服务端可能已记住解锁状态）。"""
        tile = self._tile_of(int(payload.get("room_id") or 0))
        if tile is None:
            return
        log_live.info("密码校验结果：%s（房间 %s）",
                      "通过" if payload.get("ok") else "未通过", tile.room_id)
        if payload.get("ok"):
            tile.set_status("密码已通过，正在拉流…")
        else:
            tile.set_status("密码可能不正确，正在带密码重试…", error=True)
        tile.need_info = True
        tile.request_prepare(restart=True)

    # ---------- 观看时长上报（ROADMAP 63 · P2，写操作） ----------

    def _watch_block_reason(self) -> Optional[str]:
        """不能开启观看时长上报的原因（``None`` = 可以开启）。"""
        if not bool(getattr(self.app_config, "allow_write_operations", False)):
            return "需先把 config.json 的 allow_write_operations 改为 true"
        api = self.host.hub.api
        if api is None or not bool(getattr(api, "logged_in", False)):
            return "需先登录（点主界面「获取Cookie」）"
        return None

    def _on_watch_toggled(self, checked: bool) -> None:
        """复选框：开启前先校验写操作总开关与登录态，通过后立刻上报一次。"""
        if not checked:
            self._watch_enabled = False
            self._stop_watch_timer()
            self.watch_label.setText("")
            log_live.info("观看时长上报：已关闭")
            return
        reason = self._watch_block_reason()
        if reason is not None:
            self.watch_check.blockSignals(True)   # 校验不过：把勾去掉并说明原因
            self.watch_check.setChecked(False)
            self.watch_check.blockSignals(False)
            self.watch_label.setText(f"（未开启上报：{reason}）")
            log_live.warning("观看时长上报未开启：%s", reason)
            self._set_status(f"无法开启观看时长上报：{reason}", error=True)
            return
        self._watch_enabled = True
        self._reset_watch_counters()
        log_live.info("观看时长上报：已开启（只对主路；当前主路 %s）", self.room_id)
        self._set_status("观看时长上报已开启（模拟网页端心跳，注意风控风险）")
        self._schedule_watch(WATCH_START_DELAY_MS)

    def _schedule_watch(self, delay_ms: int) -> None:
        """安排下一次「该不该上报」的检查（单次定时；每次上报或跳过后再排）。"""
        if not self._watch_enabled:
            return
        if self._watch_timer is None:
            self._watch_timer = QTimer(self)
            self._watch_timer.setSingleShot(True)
            self._watch_timer.timeout.connect(self._on_watch_tick)
        self._watch_timer.start(max(0, int(delay_ms)))

    def _stop_watch_timer(self) -> None:
        if self._watch_timer is not None:
            self._watch_timer.stop()

    def _on_watch_tick(self) -> None:
        """到点：只有主路「真正在看」才上报，否则过一会儿再看。"""
        tile = self._main_tile()
        playing = bool(tile is not None and tile.playing)
        if tile is None or not needs_watch_report(
                enabled=self._watch_enabled, active=True, paused=tile.paused,
                live=self.is_live(tile.room_id), playing=playing):
            self._schedule_watch(WATCH_IDLE_RETRY_MS)
            return
        self._watch_room = tile.room_id
        self.host.hub.submit(self._async_report_watch(tile.room_id, self._watch_next))

    def on_watch_reported(self, payload: dict) -> None:
        """后台心跳成功：累计已上报时长，并按服务端给的下次间隔继续。"""
        if not self._watch_enabled:
            return
        room_id = int(payload.get("room_id") or 0)
        if room_id != int(self._watch_room or 0):
            return  # 主路已换 / 已停止：丢弃过期结果
        seconds = int(payload.get("next_interval") or LIVE_HEARTBEAT_DEFAULT_INTERVAL)
        self._watch_next = seconds
        self._watch_reported += seconds
        self.watch_label.setText(f"已上报 {format_duration(self._watch_reported)}")
        log_live.info("观看时长已上报（房间 %s，本次 +%d 秒，累计 %s）",
                      room_id, seconds, format_duration(self._watch_reported))
        self._schedule_watch(seconds * 1000)

    def on_watch_error(self, payload: dict) -> None:
        """后台心跳失败：**停下**并提示（不自动重试，避免风控与无谓刷请求）。"""
        if not self._watch_enabled:
            return
        message = str(payload.get("message") or "未知错误")
        self._watch_enabled = False
        self._watch_room = None
        self._stop_watch_timer()
        self.watch_check.blockSignals(True)
        self.watch_check.setChecked(False)
        self.watch_check.blockSignals(False)
        self.watch_label.setText("上报已停止")
        log_live.warning("观看时长上报已停止（房间 %s）：%s", self.room_id, message)
        self._set_status("观看时长上报失败：" + message, error=True)

    async def _async_report_watch(self, room_id: int, next_interval: int) -> None:
        """后台：上报一次观看时长心跳，结果（或错误）回投主线程。"""
        host = self.host
        queue = host.ui_queue
        api = host.hub.api
        if api is None:
            queue.put(("watch_error", {"room_id": room_id, "message": "后台尚未就绪"}))
            return
        try:
            real_room = int(host.real_room_id(room_id))
            seconds = await api.report_watch_heartbeat(real_room, next_interval)
        except Exception as exc:  # 未登录 / 网络 / 风控：交给界面提示并停表
            log_live.warning("观看时长上报失败（房间 %s）：%s", room_id, exc, exc_info=True)
            queue.put(("watch_error", {
                "room_id": room_id,
                "message": f"{type(exc).__name__}: {exc}"}))
            return
        queue.put(("watch_reported", {"room_id": room_id, "next_interval": seconds}))
