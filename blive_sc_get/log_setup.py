"""运行日志的安装：按 ``config.json`` 的 ``logging`` 段决定级别、区块与文件输出。

设计要点：

- **多入口共用**：Tk / Qt GUI 与命令行都走这里的同一套实现（级别、区块过滤、文件
  handler、格式化器），避免各写一份而行为漂移；
- **两级开关**：先看主开关 ``logging.enabled``——它只决定**是否落盘**（关闭时日志照常
  进控制台 / GUI 调试页）；再看**区块开关** ``logging.categories``——关闭的区块在
  **所有出口**都不输出（过滤实现见 ``log_categories.CategoryFilter``）；
- **文件日志用于监测运行状态**：GUI 常用 ``pythonw`` / ``start_gui_silent.vbs``
  静默启动（没有控制台），只有落盘的日志能回答「昨晚为什么掉线」这类问题；
- **轮转 + 失败不致命**：文件按大小轮转（``max_bytes`` × ``backup_count`` 个备份），
  目录不可写等异常只告警，不影响程序运行；
- **幂等**：同一进程里重复初始化时不会重复挂文件 handler（见 ``install_file_handler``）。

区块常量、``CategorySwitches`` / ``CategoryFilter`` / ``get_logger`` / ``category_label``
定义在 ``log_categories``（避免与配置层循环导入），这里统一再导出，方便各入口只 import 本模块。
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .app_config import PROJECT_ROOT, LogConfig
from .log_categories import (  # noqa: F401 - 再导出给各入口使用
    CATEGORY_APP,
    CATEGORY_DATA,
    CATEGORY_LIVE,
    CATEGORY_NAMES,
    CATEGORY_ROOM,
    CATEGORY_TASK,
    CATEGORY_WINDOW,
    CategoryFilter,
    CategorySwitches,
    category_label,
    get_logger,
)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
"""日志格式（与各 GUI 调试页一致；``Room[房间号]`` 前缀由 ``%(name)s`` 承载）。"""

FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
"""文件日志带完整日期：跨天回溯运行状态时需要。"""

CONSOLE_DATE_FORMAT = "%H:%M:%S"
"""控制台 / GUI 调试页只显示时分秒，节省横向空间。"""

FILE_HANDLER_FLAG = "_blive_sc_get_file_log"
"""挂在 handler 上的标记：已装过文件 handler 就不再重复安装。"""

_lock = threading.Lock()
_switches = CategorySwitches()  # 未 configure 前也是"全部启用"
_filter: Optional[CategoryFilter] = CategoryFilter(_switches)


def _apply_filter(handler: logging.Handler,
                  flt: Optional[logging.Filter] = None) -> None:
    """给 handler 挂区块过滤器（替换旧的同类过滤器，避免叠加）。"""
    if flt is None:
        with _lock:
            flt = _filter
    if flt is None:  # pragma: no cover - 正常路径不会发生
        return
    for existing in list(handler.filters):
        if isinstance(existing, CategoryFilter):
            handler.removeFilter(existing)
    handler.addFilter(flt)


def configure(config: LogConfig) -> CategorySwitches:
    """按配置建立日志状态：根级别 + 区块开关，并给**已有 handler** 补挂区块过滤器。

    幂等：重复调用只更新级别与开关（便于运行期调整）。是否**落盘**由
    ``logging.enabled`` 单独决定（见 ``install_file_handler``）。
    """
    global _switches, _filter
    root = logging.getLogger()
    root.setLevel(config.level_value)
    with _lock:
        _switches = CategorySwitches(config.categories)
        _filter = CategoryFilter(_switches)
        handler_filter = _filter
    for handler in root.handlers:
        _apply_filter(handler, handler_filter)
    return _switches


def current_switches() -> CategorySwitches:
    """当前区块开关（未初始化时为"全部启用"）。"""
    return _switches


def attach(handler: logging.Handler, *,
           formatter: Optional[logging.Formatter] = None,
           root: Optional[logging.Logger] = None) -> logging.Handler:
    """统一注册 handler：补 formatter、挂区块过滤器、加入 root logger。

    两版 GUI 与命令行都通过它注册 handler（调试页队列 / 控制台 / 文件），
    保证**每个出口**都受区块开关约束。
    """
    if formatter is not None:
        handler.setFormatter(formatter)
    _apply_filter(handler)
    (root or logging.getLogger()).addHandler(handler)
    return handler


def make_formatter(*, with_date: bool = False) -> logging.Formatter:
    """日志格式化器（文件日志带完整日期，控制台 / 调试页只带时分秒）。"""
    return logging.Formatter(
        LOG_FORMAT, datefmt=FILE_DATE_FORMAT if with_date else CONSOLE_DATE_FORMAT)


def resolve_log_path(config: LogConfig,
                     base_dir: Optional[Union[str, Path]] = None) -> Path:
    """日志文件的绝对路径：``config.file`` 为相对路径时按项目根目录解析。"""
    raw = Path(str(config.file or "").strip() or LogConfig.file)
    if raw.is_absolute():
        return raw
    base = Path(base_dir) if base_dir is not None else PROJECT_ROOT
    return base / raw


def build_file_handler(config: LogConfig, *, base_dir: Optional[Union[str, Path]] = None,
                       formatter: Optional[logging.Formatter] = None
                       ) -> Optional[logging.Handler]:
    """按配置创建**轮转文件** handler；未开启或创建失败返回 ``None``（不影响运行）。

    ``delay=True``：文件在第一条日志落地时才真正创建，避免「只启动没日志」也生成空文件。
    """
    if not config.enabled:
        return None
    path = resolve_log_path(config, base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max(0, int(config.max_bytes)),
            backupCount=max(0, int(config.backup_count)),
            encoding="utf-8", delay=True)
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning(
            "无法创建日志文件 %s（%s），本次仅输出到控制台 / 调试页", path, exc)
        return None
    handler.setFormatter(formatter or make_formatter(with_date=True))
    setattr(handler, FILE_HANDLER_FLAG, True)
    return handler


def has_file_handler(root: Optional[logging.Logger] = None) -> bool:
    """该 logger（默认 root）上是否已装过文件日志 handler。"""
    root = root or logging.getLogger()
    return any(getattr(handler, FILE_HANDLER_FLAG, False) for handler in root.handlers)


def install_file_handler(config: LogConfig, *, base_dir: Optional[Union[str, Path]] = None,
                         formatter: Optional[logging.Formatter] = None,
                         root: Optional[logging.Logger] = None
                         ) -> Optional[logging.Handler]:
    """把轮转文件 handler 装到该 logger（默认 root）；已装过则跳过。

    返回**本次实际安装**的 handler（未开启文件日志 / 创建失败 / 已装过时为 ``None``）。
    """
    root = root or logging.getLogger()
    if has_file_handler(root):
        return None
    handler = build_file_handler(config, base_dir=base_dir, formatter=formatter)
    if handler is not None:
        attach(handler, root=root)
    return handler


def describe(config: LogConfig, *, base_dir: Optional[Union[str, Path]] = None) -> str:
    """当前日志落点的一句话描述（写进启动日志，便于用户知道去哪看日志）。"""
    if not config.enabled:
        return (f"运行日志级别 {config.level}（config.json 的 logging.enabled=false："
                f"不写文件，仅控制台 / 调试页）")
    return (f"运行日志级别 {config.level}，写入 {resolve_log_path(config, base_dir)}"
            f"（单文件上限 {config.max_bytes / 1024:.0f} KB，保留 {config.backup_count} 个备份）")


class DebouncedValueLogger:
    """把高频的连续取值合并成一条日志（「首次值 → 最后值」）。

    典型场景：拖动窗口改尺寸、拖分隔条——过程中会触发几十上百次，逐次记录既吵又拖慢
    界面。这里只在**停止变化 ``delay`` 秒后**输出一条；连续变化期间最多每 ``delay`` 秒
    复检一次（不重建定时器），开销可忽略。``flush()`` 用于退出前把待写的一条补上。
    """

    def __init__(self, logger: logging.Logger,
                 *, delay: float = 0.5,
                 template: str = "{first} → {last}") -> None:
        self._logger = logger
        self._delay = max(0.05, float(delay))
        self._template = template
        self._lock = threading.Lock()
        self._first: Optional[str] = None
        self._last: Optional[str] = None
        self._dirty = False
        self._timer: Optional[threading.Timer] = None

    def note(self, value: str) -> None:
        """记录一次变化（同一段连续变化最终只输出一条日志）。"""
        with self._lock:
            if self._first is None:
                self._first = value
            self._last = value
            self._dirty = True
            if self._timer is not None:
                return
            self._timer = self._start_timer()

    def flush(self) -> None:
        """立即输出待写的一条（退出前调用，避免丢掉最后一次变化）。"""
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()
        self._emit()

    def _start_timer(self) -> threading.Timer:
        timer = threading.Timer(self._delay, self._tick)
        timer.daemon = True
        timer.start()
        return timer

    def _tick(self) -> None:
        with self._lock:
            if self._dirty:  # 仍在变化：等下一轮再看，避免频繁建定时器
                self._dirty = False
                self._timer = self._start_timer()
                return
            first, last = self._first, self._last
            self._first = self._last = None
            self._timer = None
        if first is None:
            return
        self._logger.info(self._template.format(first=first, last=last))

    def _emit(self) -> None:
        with self._lock:
            first, last = self._first, self._last
            self._first = self._last = None
            self._dirty = False
        if first is None:
            return
        self._logger.info(self._template.format(first=first, last=last))
