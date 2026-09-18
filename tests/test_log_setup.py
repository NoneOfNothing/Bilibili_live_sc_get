"""运行日志配置与安装的离线测试（不启动界面、不写项目目录）。

覆盖两件事：

- ``config.json`` 的 ``logging`` 段解析（默认值 / 合法值 / 非法值回退 / 老配置补全）；
- 文件 handler 的安装（相对路径解析、真正写盘、超限轮转、幂等、关闭时不建文件、
  创建失败不影响运行）。
"""

import json
import logging
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from blive_sc_get.app_config import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_CATEGORIES,
    DEFAULT_LOG_ENABLED,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
    AppConfig,
    LogConfig,
    load_app_config,
)
from blive_sc_get.log_categories import (
    CATEGORY_DATA,
    CATEGORY_NAMES,
    CATEGORY_ROOM,
    CategoryFilter,
    CategorySwitches,
    get_logger,
)
from blive_sc_get.log_setup import (
    DebouncedValueLogger,
    build_file_handler,
    configure,
    describe,
    has_file_handler,
    install_file_handler,
    make_formatter,
    resolve_log_path,
)


class LogConfigParsingTests(unittest.TestCase):
    """``config.json`` 的 ``logging`` 段解析（fail-safe，不影响程序运行）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "config.json"

    def _write(self, data) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_defaults(self):
        # 默认：不写文件（只看控制台 / 调试页），级别 INFO，路径预置 logs/app.log
        cfg = AppConfig().log
        self.assertIs(cfg.enabled, DEFAULT_LOG_ENABLED)
        self.assertIs(cfg.enabled, False)
        self.assertEqual(cfg.level, DEFAULT_LOG_LEVEL)
        self.assertEqual(cfg.file, DEFAULT_LOG_FILE)
        self.assertEqual(cfg.max_bytes, DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(cfg.backup_count, DEFAULT_LOG_BACKUP_COUNT)
        self.assertEqual(cfg.level_value, logging.INFO)
        # 配置文件不存在（首次运行）时同样是默认值
        self.assertIs(load_app_config(self.tmp / "nope.json").log.enabled, False)

    def test_parse_valid_section(self):
        self._write({"logging": {"enabled": False, "level": "debug",
                                 "file": "logs/custom.log", "max_bytes": 4096,
                                 "backup_count": 1}})
        cfg = load_app_config(self.path).log
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.level, "DEBUG")  # 级别名规范化为大写
        self.assertEqual(cfg.level_value, logging.DEBUG)
        self.assertEqual(cfg.file, "logs/custom.log")
        self.assertEqual(cfg.max_bytes, 4096)
        self.assertEqual(cfg.backup_count, 1)

    def test_invalid_values_fall_back(self):
        # 字符串 "false" / 拼错的级别名 / 非字符串路径 / 非整数大小 → 逐项回退默认
        self._write({"logging": {"enabled": "false", "level": "TRACE", "file": 5,
                                 "max_bytes": "big", "backup_count": -1}})
        cfg = load_app_config(self.path).log
        self.assertIs(cfg.enabled, False)  # 字符串 "false" 不算布尔 → 回退默认（关）
        self.assertEqual(cfg.level, DEFAULT_LOG_LEVEL)
        self.assertEqual(cfg.file, DEFAULT_LOG_FILE)
        self.assertEqual(cfg.max_bytes, DEFAULT_LOG_MAX_BYTES)
        self.assertEqual(cfg.backup_count, DEFAULT_LOG_BACKUP_COUNT)

    def test_non_object_section_falls_back(self):
        self._write({"logging": "on"})
        cfg = load_app_config(self.path).log
        self.assertIs(cfg.enabled, False)
        self.assertEqual(cfg.level, DEFAULT_LOG_LEVEL)

    def test_out_of_range_values(self):
        self._write({"logging": {"max_bytes": 1, "backup_count": 999}})
        cfg = load_app_config(self.path).log
        self.assertEqual(cfg.max_bytes, DEFAULT_LOG_MAX_BYTES)  # 过小视为写错 → 默认
        self.assertEqual(cfg.backup_count, 20)                  # 过大 → 截断到上限

    def test_old_config_gets_logging_section(self):
        # 老版本文件没有 logging 段：读取时就地补全（与 medal_tasks 同一机制）
        self._write({"allow_write_operations": True})
        load_app_config(self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("logging", data)
        self.assertIs(data["logging"]["enabled"], False)
        self.assertEqual(data["logging"]["file"], DEFAULT_LOG_FILE)
        self.assertEqual(data["logging"]["level"], DEFAULT_LOG_LEVEL)


class LogFileHandlerTests(unittest.TestCase):
    """文件 handler：路径解析、写盘、轮转、幂等、关闭与失败兜底。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def _fresh_logger(name: str) -> logging.Logger:
        """互不干扰的独立 logger（不向 root 传播，避免污染其它测试）。"""
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        return logger

    def test_resolve_relative_path(self):
        cfg = LogConfig(file="logs/app.log")
        self.assertEqual(resolve_log_path(cfg, self.tmp), self.tmp / "logs" / "app.log")
        self.assertEqual(resolve_log_path(cfg, str(self.tmp)),
                         self.tmp / "logs" / "app.log")

    def test_resolve_absolute_path_kept(self):
        target = self.tmp / "elsewhere" / "x.log"
        self.assertEqual(resolve_log_path(LogConfig(file=str(target)), self.tmp), target)

    def test_disabled_creates_nothing(self):
        logger = self._fresh_logger("test.log_setup.off")
        self.assertIsNone(build_file_handler(LogConfig(enabled=False), base_dir=self.tmp))
        self.assertIsNone(
            install_file_handler(LogConfig(enabled=False), base_dir=self.tmp, root=logger))
        self.assertFalse(has_file_handler(logger))
        self.assertFalse((self.tmp / "logs").exists())

    def test_install_is_idempotent(self):
        cfg = LogConfig(enabled=True, file="logs/app.log")
        logger = self._fresh_logger("test.log_setup.idempotent")
        first = install_file_handler(cfg, base_dir=self.tmp, root=logger)
        second = install_file_handler(cfg, base_dir=self.tmp, root=logger)
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # 已装过：不重复挂 handler
        self.assertTrue(has_file_handler(logger))
        self.assertEqual(len(logger.handlers), 1)

    def test_writes_and_rotates(self):
        cfg = LogConfig(enabled=True, file="logs/app.log", max_bytes=512,
                        backup_count=1)
        logger = self._fresh_logger("test.log_setup.rotate")
        handler = build_file_handler(cfg, base_dir=self.tmp,
                                     formatter=make_formatter(with_date=True))
        self.assertIsNotNone(handler)
        logger.addHandler(handler)
        try:
            for index in range(200):
                logger.info("运行状态 %d %s", index, "x" * 60)
        finally:
            logger.removeHandler(handler)
            handler.close()
        path = self.tmp / "logs" / "app.log"
        self.assertTrue(path.exists(), "应真正写出日志文件")
        self.assertTrue(path.with_name("app.log.1").exists(), "超出上限应轮转出备份")
        content = path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("运行状态", content)
        # 文件日志带完整日期（跨天回溯需要），而非控制台 / 调试页的时分秒
        self.assertRegex(content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_creation_failure_returns_none(self):
        # 目录位置被同名文件占用 → mkdir 失败：只告警，不影响程序运行
        (self.tmp / "logs").write_text("占位文件", encoding="utf-8")
        self.assertIsNone(
            build_file_handler(LogConfig(enabled=True, file="logs/app.log"),
                               base_dir=self.tmp))

    def test_describe_mentions_path_and_switch(self):
        text = describe(LogConfig(enabled=True, level="DEBUG", file="logs/app.log"),
                        base_dir=self.tmp)
        self.assertIn("DEBUG", text)
        self.assertIn("app.log", text)
        off = describe(LogConfig(enabled=False), base_dir=self.tmp)
        self.assertIn("logging.enabled=false", off)


class LogCategoryTests(unittest.TestCase):
    """区块开关：常量一致、过滤器行为、带类别 logger、节流合并器。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def _fresh_logger(name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        return logger

    def test_category_names_match_config_defaults(self):
        # 配置层（解析/补全用的键清单）与日志层（区块常量）必须一致，防两处漂移
        self.assertEqual(tuple(DEFAULT_LOG_CATEGORIES), CATEGORY_NAMES)

    def test_categories_default_all_enabled(self):
        self.assertEqual(AppConfig().log.categories,
                         {name: True for name in CATEGORY_NAMES})

    def test_parse_categories_section(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"logging": {"categories": {
            "room": False, "data": "no", "window": True, "unknown": False}}}),
            encoding="utf-8")
        cfg = load_app_config(path).log
        self.assertFalse(cfg.categories["room"])       # 显式关闭生效
        self.assertTrue(cfg.categories["data"])        # 非布尔 → 回退默认（启用）
        self.assertTrue(cfg.categories["window"])
        self.assertEqual(sorted(cfg.categories), sorted(CATEGORY_NAMES))
        self.assertNotIn("unknown", cfg.categories)    # 未知区块忽略

    def test_filter_blocks_disabled_category(self):
        switches = CategorySwitches({CATEGORY_ROOM: False})
        flt = CategoryFilter(switches)
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        self.assertTrue(flt.filter(record))       # 未标记（第三方库日志）放行
        record.category = CATEGORY_ROOM
        self.assertFalse(flt.filter(record))      # 关闭的区块被拦下
        record.category = CATEGORY_DATA
        self.assertTrue(flt.filter(record))
        record.category = "unknown-category"
        self.assertTrue(flt.filter(record))       # 未知区块放行，不静默丢弃

    def test_get_logger_marks_category_and_respects_switch(self):
        name = "test.log_setup.category"
        logger = self._fresh_logger(name)
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        handler.addFilter(CategoryFilter(CategorySwitches({CATEGORY_ROOM: False})))
        logger.addHandler(handler)
        get_logger(CATEGORY_ROOM, name).info("房间动作")
        get_logger(CATEGORY_DATA, name).info("数据动作")
        self.assertEqual([r.getMessage() for r in records], ["数据动作"])
        self.assertEqual(getattr(records[0], "category", None), CATEGORY_DATA)

    def test_configure_sets_level_switches_and_attaches_filter(self):
        root = logging.getLogger()
        before_handlers, before_level = list(root.handlers), root.level

        def _restore():
            root.handlers[:] = before_handlers
            root.setLevel(before_level)
            configure(LogConfig())  # 区块开关恢复默认（全开），不影响其它测试

        self.addCleanup(_restore)

        class _Capture(logging.Handler):
            def emit(self, record):
                pass

        probe = _Capture()
        root.addHandler(probe)
        switches = configure(LogConfig(level="WARNING",
                                       categories={CATEGORY_ROOM: False}))
        self.assertFalse(switches.is_enabled(CATEGORY_ROOM))
        self.assertTrue(switches.is_enabled(CATEGORY_DATA))
        self.assertEqual(root.level, logging.WARNING)
        # 已有 handler 会被补挂区块过滤器（这就是「分块启用」的执行点）
        self.assertTrue(any(isinstance(f, CategoryFilter) for f in probe.filters))
        root.removeHandler(probe)

    def test_debounced_value_logger_merges_and_flushes(self):
        logger = self._fresh_logger("test.log_setup.debounce")
        lines = []

        class _Capture(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        logger.addHandler(_Capture())
        debounced = DebouncedValueLogger(logger, delay=0.05,
                                         template="{first} → {last}")
        for size in ("800x600", "1024x768", "1280x900"):
            debounced.note(size)
        deadline = time.monotonic() + 2.0
        while not lines and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(lines, ["800x600 → 1280x900"])  # 连续变化合并成一条
        # flush：立即输出待写值（退出前调用，避免丢掉最后一次变化）
        debounced.note("1500x1000")
        debounced.flush()
        self.assertEqual(lines[-1], "1500x1000 → 1500x1000")


if __name__ == "__main__":
    unittest.main()
