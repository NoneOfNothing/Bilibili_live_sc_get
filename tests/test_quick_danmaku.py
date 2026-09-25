"""快捷弹幕（ROADMAP 90）的离线测试：清洗规则、填入规则、按房间持久化。

刻意**不 import qt_app / PySide6**——本文件必须在没有 Qt 的环境下也能跑
（``tests/test_gui_support.py`` 因导入 qt_app 而依赖 PySide6）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from blive_sc_get.gui_app import merge_quick_danmaku
from blive_sc_get.gui_config import (
    QUICK_DANMAKU_LEN,
    QUICK_DANMAKU_MAX,
    RoomEntry,
    load_room_entries,
    normalize_quick_danmaku,
    save_room_entries,
)


class NormalizeQuickDanmakuTests(unittest.TestCase):
    """清洗：只留非空字符串、去首尾空白、超长截断、保序去重、限制条数。"""

    def test_non_list_returns_empty(self):
        for value in (None, "", "打卡", 3, {"a": 1}):
            self.assertEqual(normalize_quick_danmaku(value), [])

    def test_strips_and_keeps_valid_items(self):
        self.assertEqual(normalize_quick_danmaku([" 打卡 ", "好耶"]), ["打卡", "好耶"])

    def test_drops_non_string_and_blank_items(self):
        self.assertEqual(
            normalize_quick_danmaku(["打卡", 5, None, "   ", ""]), ["打卡"])

    def test_dedupes_keeping_order(self):
        self.assertEqual(normalize_quick_danmaku(["b", "a", "b", "a"]), ["b", "a"])

    def test_truncates_overlong_text(self):
        self.assertEqual(normalize_quick_danmaku(["字" * 30]),
                         ["字" * QUICK_DANMAKU_LEN])

    def test_caps_item_count(self):
        items = [f"第{i}条" for i in range(QUICK_DANMAKU_MAX + 5)]
        self.assertEqual(len(normalize_quick_danmaku(items)), QUICK_DANMAKU_MAX)

    def test_custom_max_len(self):
        self.assertEqual(normalize_quick_danmaku(["abcdef"], max_len=3), ["abc"])


class MergeQuickDanmakuTests(unittest.TestCase):
    """点选后填入输入框的规则：不覆盖用户已输入的内容。"""

    def test_fills_empty_box(self):
        self.assertEqual(merge_quick_danmaku("", "打卡"), "打卡")

    def test_blank_current_is_treated_as_empty(self):
        self.assertEqual(merge_quick_danmaku("   ", "打卡"), "打卡")

    def test_appends_without_overwriting(self):
        self.assertEqual(merge_quick_danmaku("半截", "打卡"), "半截 打卡")

    def test_same_text_is_not_repeated(self):
        self.assertEqual(merge_quick_danmaku("打卡", "打卡"), "打卡")

    def test_empty_text_keeps_current(self):
        self.assertEqual(merge_quick_danmaku("半截", ""), "半截")
        self.assertEqual(merge_quick_danmaku("半截", None), "半截")


class QuickDanmakuStorageTests(unittest.TestCase):
    """按房间独立持久化：读回清洗、旧配置缺字段兼容、写盘往返。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "gui_rooms.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_reads_list_per_room(self):
        self._write({"rooms": [
            {"room_id": 1, "quick_danmaku": ["打卡", "好耶"]},
            {"room_id": 2, "quick_danmaku": ["另一个房间"]},
        ]})
        entries = {e.room_id: e for e in load_room_entries(self.path)}
        self.assertEqual(entries[1].quick_danmaku, ["打卡", "好耶"])
        self.assertEqual(entries[2].quick_danmaku, ["另一个房间"])

    def test_missing_field_defaults_to_empty(self):
        self._write({"rooms": [{"room_id": 1}]})
        self.assertEqual(load_room_entries(self.path)[0].quick_danmaku, [])

    def test_invalid_field_is_cleaned(self):
        self._write({"rooms": [{"room_id": 1, "quick_danmaku": "打卡"}]})
        self.assertEqual(load_room_entries(self.path)[0].quick_danmaku, [])
        self._write({"rooms": [{"room_id": 1, "quick_danmaku": ["打卡", 7, "  "]} ]})
        self.assertEqual(load_room_entries(self.path)[0].quick_danmaku, ["打卡"])

    def test_save_round_trip(self):
        save_room_entries(self.path, [RoomEntry(room_id=9, quick_danmaku=["打卡", "好耶"])])
        self.assertEqual(load_room_entries(self.path)[0].quick_danmaku, ["打卡", "好耶"])


if __name__ == "__main__":
    unittest.main()