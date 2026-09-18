"""项目根目录的应用级配置（``config.json``）。

与界面偏好（``gui_rooms.json`` 的 ui 段）分离：这里放跨模式的**应用行为开关**
（写操作总开关、表情悬浮提示显示哪些字段等）。

安全约定：程序默认是「只读」的——发送弹幕、发评论等向 B 站提交数据的
**写操作一律默认关闭**，必须在 ``config.json`` 中显式开启才会启用。
字段缺失、文件损坏或类型非法时一律按「关闭」处理（fail-safe）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .log_categories import CATEGORY_LABELS, CATEGORY_NAMES

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
"""项目根目录：``config.json``、``cookie.txt`` 与**相对日志路径**的解析基准。"""

CONFIG_FILE_NAME = "config.json"
CONFIG_FILE_PATH = PROJECT_ROOT / CONFIG_FILE_NAME

EMOTICON_TOOLTIP_FIELDS = ("text", "unique", "id")
"""悬浮提示可显示的表情字段（元组顺序即拼接顺序）：

- ``text``：触发词（如 ``[百岁山]``）
- ``unique``：表情唯一标识 ``emoticon_unique``（如 ``room_9527_109824``）
- ``id``：数字 ``emoticon_id``
"""

DEFAULT_EMOTICON_TOOLTIP: Tuple[str, ...] = ("text",)
"""悬浮提示的默认字段：**仅触发词**（可在 config.json 的 emoticon_tooltip 段增删）。"""

DEFAULT_MEDAL_LIKE_INTERVAL: Tuple[float, float] = (15.0, 20.0)
"""自动点赞两次之间的随机间隔（秒），降低风控风险。"""

DEFAULT_MEDAL_DANMAKU_INTERVAL: Tuple[float, float] = (6.0, 8.0)
"""自动发送弹幕两次之间的随机间隔（秒），降低刷屏/风控风险。"""

DEFAULT_MEDAL_MAX_RETRY: int = 3
"""单个写任务**连续无进展/失败**多少次后停止本轮（等待下次或自动任务下一轮继续）。"""

DEFAULT_PREVIEW_QUALITY: int = 0
"""预览默认清晰度：``0`` = **自动**（取该房间可用最高档）。

直播画质越高越好且不再额外耗流量（只有一路流），所以默认自动选最高；想固定某档
（例如担心带宽）再写具体 ``qn``。与 ``api.PREVIEW_DEFAULT_QUALITY`` 保持一致（有测试
防两处漂移）。
"""

PREVIEW_QUALITY_CHOICES: Tuple[int, ...] = (0, 80, 150, 250, 400, 10000, 20000, 30000)
"""允许的清晰度取值：``0`` 自动 / 流畅 / 高清 / 超清 / 蓝光 / 原画 / 4K / 杜比。

实际能拉到哪一档由**房间与账号**决定（程序会按接口返回的可用档位收缩下拉）。
"""

DEFAULT_PREVIEW_MUTE: bool = True
"""预览是否默认静音（避免切换房间时突然出声）。"""

DEFAULT_PREVIEW_VOLUME: int = 60
"""预览音量（0-100，取消静音后生效）。"""

DEFAULT_PREVIEW_ALWAYS_ON_TOP: bool = True
"""预览浮窗是否置顶。"""

DEFAULT_PREVIEW_FOLLOW_ROOM: bool = True
"""是否让预览跟随主界面选中的直播间（**只切「主路」的声音**，不增删预览的路数）。

选中的房间已在预览中 → 把它设为有声音的那一路；不在预览中 → 什么也不做（加/减哪几路
完全由用户决定，切主界面的房间不会把预览内容换掉）。
"""

DEFAULT_PREVIEW_MAX_DRIFT: float = 3.0
"""预览允许的**播放落后**上限（秒；``preview.max_drift_sec``，ROADMAP 63 · P4 后续）。

实测（同一在播房间、经本机代理、FLV/HLS 各播 60 秒）：播放器位置增长比墙上时间慢约
**0.16 / 0.07 秒每分钟**——「越看越落后」是播放端的固有现象（音视频同步与缓冲的微调），
与代理转发无关。累计到 ``max_drift_sec`` 就自动重新拉流跳到最新（一次短暂重载），
把落后压回阈值内。写 ``0`` 关闭自动追边。
"""

DEFAULT_PREVIEW_MAX_ROOMS: int = 4
"""同时预览的路数上限（``preview.max_rooms``，ROADMAP 63 · P3）。

每路都要解码 + 一条连接 + 一个本机代理，4 路是 1080p 下普通机器的舒适区；硬上限也是
4（``qt_preview.PREVIEW_MAX_ROOMS``，配得再大也只按 4 生效）。
"""

DEFAULT_PREVIEW_WATCH_TIME: bool = False
"""是否上报「观看时长」（``preview.watch_time``，ROADMAP 63 · P2）。

**默认关闭**：这是向 B 站提交数据的**写操作**（`webHeartBeat` 心跳），用于让账号在
预览的直播间累计观看时长 / 亲密度。它模拟网页端行为，属**高风险**操作，可能触发风控；
开启还需要 ``allow_write_operations`` 与登录 Cookie 同时满足。
"""

DEFAULT_LOG_ENABLED: bool = False
"""默认是否把运行日志写入文件（``logging.enabled``）。

默认**关闭**：控制台 / GUI 调试页照常输出，只是不额外落盘。需要长期监测运行状态时
在 config.json 里改为 ``true``——GUI 常用 ``pythonw`` / ``start_gui_silent.vbs``
静默启动（没有控制台），那种场景下只有文件日志能回溯历史。
"""

DEFAULT_LOG_LEVEL: str = "INFO"
"""默认启动日志级别（``logging.level``；GUI 里勾选「显示 DEBUG 日志」可临时提升）。"""

DEFAULT_LOG_FILE: str = "logs/app.log"
"""默认日志文件（``logging.file``；相对路径按项目根目录解析）。"""

DEFAULT_LOG_MAX_BYTES: int = 2 * 1024 * 1024
"""单个日志文件大小上限（``logging.max_bytes``，默认 2 MB），超出自动轮转。"""

DEFAULT_LOG_BACKUP_COUNT: int = 3
"""保留的历史日志文件数（``logging.backup_count``，默认 3 个）。"""

LOG_LEVEL_NAMES: Tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
"""允许的日志级别名（``logging.level``，大小写不敏感，非法值回退 INFO）。"""

DEFAULT_LOG_CATEGORIES: Tuple[str, ...] = CATEGORY_NAMES
"""日志区块（``logging.categories`` 的键，定义见 ``log_categories``），**默认全部启用**。

判定顺序：先看主开关 ``logging.enabled``（只决定是否**落盘**——关闭时日志照常进
控制台 / GUI 调试页），再看区块开关（关闭的区块在**所有出口**都不输出）。
"""

LOG_CATEGORY_LABELS: Dict[str, str] = CATEGORY_LABELS
"""各区块的中文说明（写入配置模板的 ``_说明``，也用于启动日志）。"""

DEFAULT_CONFIG_TEMPLATE = """{
  "_说明": "应用级配置（首次运行自动生成，可随时删除，下次运行会按需重建）。allow_write_operations 是写操作总开关（发送弹幕、自动点赞等会向 B 站提交数据的操作），出于安全考虑默认关闭；确认了解风险后改为 true 才会启用。emoticon_tooltip 控制鼠标悬浮表情时提示哪些字段：text=触发词、unique=表情唯一标识、id=数字 id，默认仅 text，写 [] 或全部 false 表示不显示提示。medal_tasks 控制粉丝牌自动任务：auto 为「全自动」总开关（默认 false，同时约束点赞与发弹幕两项；仍需在界面里对具体房间分别开启「自动点赞」/「自动发弹幕」，且受 allow_write_operations 约束）；like_interval_sec / danmaku_interval_sec 为两次点赞/发弹幕之间的随机间隔秒数（数组 [最小, 最大]）；max_retry 为单任务连续无进展/失败上限（达到后停止本轮，等下轮继续，不做失败重试风暴）。自动任务属于违反平台常规使用方式的高风险操作，可能触发风控，请自行评估后再开启。logging 控制运行日志（GUI 与命令行共用）：enabled 为是否写入文件（默认 false，静默启动时如需事后排查改为 true）；level 为启动级别 DEBUG/INFO/WARNING/ERROR/CRITICAL（默认 INFO，GUI 里勾选「显示 DEBUG 日志」可临时提升）；file 为日志文件路径（相对项目根目录，默认 logs/app.log）；max_bytes / backup_count 为单文件大小上限（字节，默认 2097152 = 2MB）与保留的历史文件数（超出自动轮转）；categories 为分区块日志开关（默认全部 true，可关闭不关心的区块以减少噪音）：room=房间与选择（切换直播间、增删、启停、排序、拖动、备注）、window=界面布局（窗口尺寸、分隔条、页签、面板显隐）、data=数据读写（接口请求、历史读取、落盘、表情包与粉丝牌刷新）、task=任务与写操作（发弹幕/表情、粉丝牌任务、Cookie 获取、开播提醒）、live=直播连接与状态（WS 连接、重连、开播下播、SC 与弹幕接收）、app=应用生命周期（启动退出、配置加载、房间锁、异常）。判定顺序：先看主开关 enabled（只决定是否落盘，关闭时日志照常进控制台/GUI 调试页），再看区块开关（关闭的区块在所有出口都不输出）。修改后需重启程序生效。preview 控制直播预览（浮窗播放直播流，ROADMAP 63）：quality 为清晰度，0=自动（默认，取该房间可用最高档），也可写 80 流畅/150 高清/250 超清/400 蓝光/10000 原画/20000 4K/30000 杜比（高清晰度需登录 Cookie，且以房间实际可用档位为准）；mute 为是否默认静音；volume 为音量 0-100；always_on_top 为预览窗口是否置顶；follow_room 为是否让预览跟随主界面选中的直播间（只把该房间设为「主路」= 有声音的那一路，**不会增删预览的路数**）；watch_time 为是否在预览播放时上报「观看时长」（默认 false）——这是模拟网页端心跳（webHeartBeat）的写操作，用于让账号在预览的直播间累计观看时长，需 allow_write_operations 也为 true 且已登录 Cookie，属高风险操作、可能触发风控，请自行评估；max_rooms 为同时预览的路数上限（1-4，默认 4，多路时只有主路出声、副路卡顿或 CPU 偏高会被自动停掉）；max_drift_sec 为允许的播放落后秒数（默认 3：直播播放会以每分钟约 0.1~0.2 秒的速度越看越落后于网页端，累计超过这个值就自动重新拉流跳到最新，写 0 关闭自动追边）。字段缺失/文件损坏/类型非法一律按默认值处理。",
  "allow_write_operations": false,
  "emoticon_tooltip": {
    "text": true,
    "unique": false,
    "id": false
  },
  "medal_tasks": {
    "auto": false,
    "like_interval_sec": [15, 20],
    "danmaku_interval_sec": [6, 8],
    "max_retry": 3
  },
  "logging": {
    "enabled": false,
    "level": "INFO",
    "file": "logs/app.log",
    "max_bytes": 2097152,
    "backup_count": 3,
    "categories": {
      "room": true,
      "window": true,
      "data": true,
      "task": true,
      "live": true,
      "app": true
    }
  },
  "preview": {
    "quality": 0,
    "mute": true,
    "volume": 60,
    "always_on_top": true,
    "follow_room": true,
    "watch_time": false,
    "max_rooms": 4,
    "max_drift_sec": 3.0
  }
}
"""
"""首次运行自动生成的默认 ``config.json`` 模板（带字段说明）。"""


@dataclass(frozen=True)
class LogConfig:
    """运行日志配置（``config.json`` 的 ``logging`` 段）。

    作用于 **GUI（Tk / Qt）与命令行两套入口**：启动级别与文件输出都由这里决定，
    两版共用 ``log_setup`` 里的同一实现，避免各写一份而行为漂移。
    """

    enabled: bool = DEFAULT_LOG_ENABLED
    """是否把日志写入文件（默认关；开启后写入 ``file`` 指定的轮转文件）。"""

    level: str = DEFAULT_LOG_LEVEL
    """启动日志级别（``LOG_LEVEL_NAMES`` 之一，解析时已规范化为大写）。"""

    file: str = DEFAULT_LOG_FILE
    """日志文件路径；相对路径按项目根目录解析。"""

    max_bytes: int = DEFAULT_LOG_MAX_BYTES
    """单个日志文件大小上限（字节），超出即轮转。"""

    backup_count: int = DEFAULT_LOG_BACKUP_COUNT
    """保留的历史日志文件数。"""

    categories: Dict[str, bool] = field(
        default_factory=lambda: {name: True for name in DEFAULT_LOG_CATEGORIES})
    """区块日志开关（``DEFAULT_LOG_CATEGORIES`` 的键，**默认全部启用**）。

    关闭某区块后，该类日志在控制台 / GUI 调试页 / 文件里**都不再输出**；未标记区块的
    第三方库日志不受影响（照常输出）。
    """

    @property
    def level_value(self) -> int:
        """``logging`` 模块用的级别数值（级别名已在解析时校验）。"""
        return getattr(logging, self.level, logging.INFO)


@dataclass(frozen=True)
class PreviewConfig:
    """直播预览配置（``config.json`` 的 ``preview`` 段，ROADMAP 63）。

    只控制**拉流与播放**的本地行为；「观看时长上报」属写操作，另行加开关约束（P2）。
    """

    quality: int = DEFAULT_PREVIEW_QUALITY
    """拉流清晰度 ``qn``（见 ``PREVIEW_QUALITY_CHOICES``）。

    ``0`` 表示自动（该房间可用最高档）；高清晰度还需要登录 Cookie，最终以房间实际
    可用档位为准（界面会按接口返回收缩下拉）。
    """

    mute: bool = DEFAULT_PREVIEW_MUTE
    """是否默认静音。"""

    volume: int = DEFAULT_PREVIEW_VOLUME
    """音量 0-100。"""

    always_on_top: bool = DEFAULT_PREVIEW_ALWAYS_ON_TOP
    """预览浮窗是否置顶。"""

    follow_room: bool = DEFAULT_PREVIEW_FOLLOW_ROOM
    """是否让预览跟随主界面选中的直播间（**只切主路声音**，不改变预览的路数）。"""

    watch_time: bool = DEFAULT_PREVIEW_WATCH_TIME
    """是否上报「观看时长」（写操作，默认关闭；见 ``DEFAULT_PREVIEW_WATCH_TIME``）。

    还需 ``allow_write_operations`` 开启、并已登录，浮窗里那个开关才能真正生效。
    """

    max_rooms: int = DEFAULT_PREVIEW_MAX_ROOMS
    """同时预览的路数上限（1~4，默认 4；见 ``DEFAULT_PREVIEW_MAX_ROOMS``）。"""

    max_drift_sec: float = DEFAULT_PREVIEW_MAX_DRIFT
    """允许的播放落后上限（秒，默认 3；``0`` 关闭自动追边；见 ``DEFAULT_PREVIEW_MAX_DRIFT``）。"""


@dataclass(frozen=True)
class AppConfig:
    """应用级配置。默认值即「只读」：一切写操作关闭。"""

    allow_write_operations: bool = False
    """写操作总开关（发送弹幕等）。默认 False，需在 config.json 中显式开启。"""

    emoticon_tooltip: Tuple[str, ...] = DEFAULT_EMOTICON_TOOLTIP
    """鼠标悬浮在表情上时提示哪些字段（``EMOTICON_TOOLTIP_FIELDS`` 的子集）。

    默认仅 ``text``（触发词）；给空元组表示不显示悬浮提示。
    """

    auto_medal_tasks: bool = False
    """粉丝牌自动任务的**总开关**（默认 False，同时约束点赞与发弹幕两项）。

    仅当此开关、``allow_write_operations``、以及**具体房间**的「自动点赞」/
    「自动发弹幕」开关（各自独立）同时满足时，该房间才会后台自动执行对应任务。
    """

    medal_like_interval: Tuple[float, float] = DEFAULT_MEDAL_LIKE_INTERVAL
    """自动点赞的随机间隔区间（秒）。"""

    medal_danmaku_interval: Tuple[float, float] = DEFAULT_MEDAL_DANMAKU_INTERVAL
    """自动发弹幕的随机间隔区间（秒）。"""

    medal_max_retry: int = DEFAULT_MEDAL_MAX_RETRY
    """单个写任务**连续无进展/失败**多少次后停止本轮。"""

    log: LogConfig = field(default_factory=LogConfig)
    """运行日志（对应 config.json 的 ``logging`` 段，默认不写文件、区块全开）。"""

    preview: PreviewConfig = field(default_factory=PreviewConfig)
    """直播预览（对应 config.json 的 ``preview`` 段，ROADMAP 63）。"""


def _as_bool(value: Any, default: bool = False) -> bool:
    """严格取布尔值：只有真正的 JSON 布尔才认，字符串 \"false\"/数字一律回退默认。

    避免误把 "false" 这类字符串当真值而意外开启写操作。
    """
    return value if isinstance(value, bool) else default


def _parse_tooltip_fields(value: Any) -> Tuple[str, ...]:
    """解析 ``emoticon_tooltip``（纯函数）。

    支持两种写法，直接编辑 config.json 即可：

    - 列表：``["text", "unique", "id"]``
    - 开关对象：``{"text": true, "unique": false, "id": false}``（只认 JSON 布尔）

    - 字段缺失、类型非法（含字符串 "true" 这类非布尔值）→ 回退默认（仅触发词）；
    - **显式**给空列表或全 false 对象 → 返回空元组（即不显示悬浮提示）；
    - 名单里全是拼错的字段名 → 视为写错，回退默认。
    """
    if isinstance(value, list):
        entries = [str(item).strip().lower() for item in value if isinstance(item, str)]
    elif isinstance(value, dict):
        # 只认真正的 JSON 布尔：出现 "true"/1 这类写法视为写错，整体回退默认
        if not all(isinstance(on, bool) for on in value.values()):
            return DEFAULT_EMOTICON_TOOLTIP
        entries = [str(name).strip().lower() for name, on in value.items() if on]
    else:
        return DEFAULT_EMOTICON_TOOLTIP
    if not entries:
        return ()
    picked = set(entries)
    matched = tuple(name for name in EMOTICON_TOOLTIP_FIELDS if name in picked)
    return matched or DEFAULT_EMOTICON_TOOLTIP


def _parse_interval(value: Any, default: Tuple[float, float]) -> Tuple[float, float]:
    """解析 ``[最小, 最大]`` 间隔区间；非法值回退默认（负值/类型错/长度不为 2）。

    最大值小于最小值时自动交换，保证返回 ``(low <= high)``。
    """
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return default
    try:
        low = float(value[0])
        high = float(value[1])
    except (TypeError, ValueError):
        return default
    if low < 0 or high < 0:
        return default
    if high < low:
        low, high = high, low
    return (low, high)


def _parse_retry(value: Any, default: int, *, upper: int = 10) -> int:
    """解析重试上限：仅接受非负整数，超过上限则截断到 ``upper``。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < 0:
        return default
    return min(value, upper)


def _parse_float(value: Any, default: float, *, low: float = 0.0,
                 high: float = 1e9) -> float:
    """解析浮点配置：只认真正的 int/float（``true``/``"3"`` 一律回退默认）。

    低于 ``low`` 视为写错 → 回退默认；高于 ``high`` → 截断到上限（与 ``_parse_int`` 一致）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if number < low:
        return default
    return min(number, high)


def _parse_int(value: Any, default: int, *, low: int = 0,
               high: int = 2 ** 31 - 1) -> int:
    """解析整数配置：只认真正的 int（``true``/``"1"`` 这类一律回退默认）。

    低于 ``low``（含负数）视为写错 → 回退默认；高于 ``high``（过大）→ 截断到上限。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < low:
        return default
    return min(value, high)


def _parse_log_categories(value: Any) -> Dict[str, bool]:
    """解析 ``logging.categories``：只认 JSON 布尔，缺失/非法 → 该区块按默认（启用）。

    未知区块名忽略并告警（避免把拼错的键当成"关闭某个区块"）；整段类型非法 → 全默认。
    """
    result = {name: True for name in DEFAULT_LOG_CATEGORIES}
    if value is None:
        return result
    if not isinstance(value, dict):
        logger.warning("配置 logging.categories 应为对象，使用默认（区块全部启用）")
        return result
    for name in DEFAULT_LOG_CATEGORIES:
        raw = value.get(name)
        if raw is None:
            continue
        if isinstance(raw, bool):
            result[name] = raw
        else:
            logger.warning("配置 logging.categories.%s 应为 true/false（收到 %r），按默认启用",
                           name, raw)
    unknown = [str(key) for key in value if key not in DEFAULT_LOG_CATEGORIES]
    if unknown:
        logger.warning("配置 logging.categories 含未知区块 %s（可选：%s），已忽略",
                       "、".join(unknown), "、".join(DEFAULT_LOG_CATEGORIES))
    return result


def _parse_preview_config(value: Any) -> PreviewConfig:
    """解析 ``preview`` 段（逐项 fail-safe；清晰度需在白名单内）。"""
    if value is None:
        return PreviewConfig()
    if not isinstance(value, dict):
        logger.warning("配置 preview 应为对象，使用默认（自动最高画质 / 静音 / 跟随选中房间）")
        return PreviewConfig()
    raw_quality = value.get("quality")
    quality = raw_quality if isinstance(raw_quality, int) and not isinstance(
        raw_quality, bool) else None
    if quality not in PREVIEW_QUALITY_CHOICES:
        if raw_quality is not None:
            logger.warning("配置 preview.quality 非法（%r），使用 %s",
                           raw_quality, DEFAULT_PREVIEW_QUALITY)
        quality = DEFAULT_PREVIEW_QUALITY
    return PreviewConfig(
        quality=quality,
        mute=_as_bool(value.get("mute"), DEFAULT_PREVIEW_MUTE),
        volume=_parse_int(value.get("volume"), DEFAULT_PREVIEW_VOLUME, low=0, high=100),
        always_on_top=_as_bool(value.get("always_on_top"),
                               DEFAULT_PREVIEW_ALWAYS_ON_TOP),
        follow_room=_as_bool(value.get("follow_room"), DEFAULT_PREVIEW_FOLLOW_ROOM),
        watch_time=_as_bool(value.get("watch_time"), DEFAULT_PREVIEW_WATCH_TIME),
        max_rooms=_parse_int(value.get("max_rooms"), DEFAULT_PREVIEW_MAX_ROOMS,
                             low=1, high=DEFAULT_PREVIEW_MAX_ROOMS),
        max_drift_sec=_parse_float(value.get("max_drift_sec"),
                                   DEFAULT_PREVIEW_MAX_DRIFT, low=0.0, high=600.0),
    )


def _parse_log_config(value: Any) -> LogConfig:
    """解析 ``logging`` 段（纯函数，便于离线测试）。

    fail-safe：整段类型非法→全默认；单项非法（级别名拼错、路径非字符串、大小非整数）
    →该项回退默认并告警，其余项照常生效——日志是辅助功能，不该因配置错误影响运行。
    """
    if value is None:
        return LogConfig()
    if not isinstance(value, dict):
        logger.warning("配置 logging 应为对象，使用默认（日志写入 %s）", DEFAULT_LOG_FILE)
        return LogConfig()
    level_raw = value.get("level")
    level = str(level_raw).strip().upper() if isinstance(level_raw, str) else ""
    if level not in LOG_LEVEL_NAMES:
        if level_raw is not None:
            logger.warning("配置 logging.level 非法（%r），使用 %s",
                           level_raw, DEFAULT_LOG_LEVEL)
        level = DEFAULT_LOG_LEVEL
    file_raw = value.get("file")
    file_name = str(file_raw).strip() if isinstance(file_raw, str) else ""
    if not file_name:
        if file_raw is not None:
            logger.warning("配置 logging.file 非法（%r），使用 %s",
                           file_raw, DEFAULT_LOG_FILE)
        file_name = DEFAULT_LOG_FILE
    return LogConfig(
        enabled=_as_bool(value.get("enabled"), DEFAULT_LOG_ENABLED),
        level=level,
        file=file_name,
        max_bytes=_parse_int(value.get("max_bytes"), DEFAULT_LOG_MAX_BYTES,
                             low=1024, high=64 * 1024 * 1024),
        backup_count=_parse_int(value.get("backup_count"), DEFAULT_LOG_BACKUP_COUNT,
                                low=0, high=20),
        categories=_parse_log_categories(value.get("categories")),
    )


def _template_defaults() -> Dict[str, Any]:
    """默认模板解析出的结构（去掉 ``_`` 开头的说明字段），用于补全缺失配置项。

    直接解析 ``DEFAULT_CONFIG_TEMPLATE``，避免模板与补全逻辑两处维护而走偏。
    """
    try:
        data = json.loads(DEFAULT_CONFIG_TEMPLATE)
    except ValueError:  # 模板自身损坏：不做补全（不影响按默认值运行）
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if not key.startswith("_")}


def _merge_missing(target: Dict[str, Any], defaults: Dict[str, Any]) -> bool:
    """把 ``defaults`` 中**缺失**的键补进 ``target``（嵌套对象递归），返回是否有改动。

    - 只补缺失键，**绝不覆盖已有值**（用户改过的值一律保留，含显式 ``false``）；
    - 已有值类型不对（如把 ``medal_tasks`` 写成字符串）时保留原样，交由解析层
      按默认值处理并告警（不静默"修好"用户的错误配置）；
    - **空对象不递归填充**：空对象往往是「显式声明什么都不用」的写法
      （如 ``emoticon_tooltip: {}`` 表示不显示悬浮提示），按模板补上
      ``text: true`` 会把用户关掉的功能又打开；
    - 用户自己加的额外键保留。
    """
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
            changed = True
        elif (isinstance(value, dict) and isinstance(target.get(key), dict)
                and target[key]):
            changed = _merge_missing(target[key], value) or changed
    return changed


def _complete_existing_config(config_path: Path, raw: Dict[str, Any]) -> None:
    """把后来新增的配置项补进**已存在**的 ``config.json``。

    只在首次运行（文件不存在）时写模板，会漏掉老版本文件：用户升级后
    ``medal_tasks`` 之类的段根本不出现在文件里，既看不到也无从开启。这里在读取时
    就地补全缺失键，并顺带把自动生成的 ``_说明`` 刷新为最新版本（旧说明会漏讲新项）。
    """
    defaults = _template_defaults()
    if not defaults:
        return
    if not _merge_missing(raw, defaults):
        return  # 没有缺失项：不动文件（也不刷新说明），避免每次启动都重写
    try:
        note = json.loads(DEFAULT_CONFIG_TEMPLATE).get("_说明")
        if isinstance(note, str) and note:
            raw["_说明"] = note
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("补全应用配置失败（%s）: %s", config_path, exc)
        return
    except ValueError as exc:  # 模板异常：仅跳过说明刷新
        logger.debug("刷新配置说明失败（%s）: %s", config_path, exc)
        return
    logger.info("已补全应用配置 %s 中缺失的字段（已有设置保持不变），"
                "新增项见文件内的 _说明", config_path)


def _write_default_config(config_path: Path) -> None:
    """首次运行（或文件不存在）时生成带字段说明的默认配置文件。

    生成失败（如目录只读）仅记录日志，不影响本次运行使用默认值。
    """
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        logger.info("已生成默认应用配置 %s（写操作默认关闭）", config_path)
    except OSError as exc:
        logger.debug("生成默认应用配置失败（%s）: %s", config_path, exc)


def load_app_config(path: Optional[Union[str, Path]] = None) -> AppConfig:
    """读取应用配置；文件损坏/字段非法时回退默认（写操作关闭）。

    文件不存在时视为首次运行：自动生成带字段说明的默认配置文件；
    文件已存在但缺少后来新增的字段（如老版本留下的 ``config.json`` 没有
    ``medal_tasks`` 段）时，**就地补全缺失字段**后再解析（已有值不变）。
    生成/补全失败不影响本次运行。
    """
    config_path = Path(path) if path is not None else CONFIG_FILE_PATH
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("未找到应用配置 %s，生成默认配置（写操作关闭）", config_path)
        _write_default_config(config_path)
        return AppConfig()
    except (OSError, ValueError) as exc:
        logger.warning("读取应用配置失败（%s），使用默认（写操作关闭）: %s",
                       config_path, exc)
        return AppConfig()
    raw: Dict[str, Any] = data if isinstance(data, dict) else {}
    if not isinstance(data, dict):
        logger.warning("应用配置格式异常（应为 JSON 对象），使用默认（写操作关闭）")
    else:
        # 老版本文件不会自动获得新字段：读取时就地补全（用户已有值与自定义键保留）
        _complete_existing_config(config_path, raw)
    medal_raw = raw.get("medal_tasks")
    medal = medal_raw if isinstance(medal_raw, dict) else {}
    if medal_raw is not None and not isinstance(medal_raw, dict):
        logger.warning("配置 medal_tasks 应为对象，使用默认（自动任务关闭）")
    return AppConfig(
        allow_write_operations=_as_bool(raw.get("allow_write_operations"), False),
        emoticon_tooltip=_parse_tooltip_fields(raw.get("emoticon_tooltip")),
        auto_medal_tasks=_as_bool(medal.get("auto"), False),
        medal_like_interval=_parse_interval(
            medal.get("like_interval_sec"), DEFAULT_MEDAL_LIKE_INTERVAL),
        medal_danmaku_interval=_parse_interval(
            medal.get("danmaku_interval_sec"), DEFAULT_MEDAL_DANMAKU_INTERVAL),
        medal_max_retry=_parse_retry(medal.get("max_retry"), DEFAULT_MEDAL_MAX_RETRY),
        log=_parse_log_config(raw.get("logging")),
        preview=_parse_preview_config(raw.get("preview")),
    )
