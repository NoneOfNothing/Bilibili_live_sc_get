"""启动时自动选中「上次退出时的直播间」的纯函数与接线（ROADMAP 98）。

此前两版启动都只是「选中一个房间」：Tk 取**显示顺序**的第一行、Qt 取 `entries` 的第一个
（＝自定义排序的第一间，两版不一致）。现两版统一为：优先恢复上次选中的房间（**仍在房间列表里**
才算数），否则回退成**显示顺序的第一行**。多选态不做恢复（只记一个房间号）。
"""

import ast
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from blive_sc_get.gui_config import load_ui_prefs, parse_selected_room

ROOT = Path(__file__).resolve().parents[1]
BOTH = (("gui_app.py", "ScMonitorApp"), ("qt_app.py", "QtScMonitorApp"))


def _module_tree(module: str) -> ast.Module:
    return ast.parse((ROOT / "blive_sc_get" / module).read_text(encoding="utf-8"))


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到类 {name}")


def _method(tree: ast.Module, class_name: str, name: str):
    for node in ast.walk(_class_node(tree, class_name)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"未找到 {class_name}.{name}")


def _called_attrs(node) -> set:
    return {sub.func.attr for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)}


def _attr_names(node) -> set:
    return {sub.attr for sub in ast.walk(node) if isinstance(sub, ast.Attribute)}


def _consts(node) -> set:
    return {sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant)}


class ParseSelectedRoomTests(unittest.TestCase):
    """`ui.selected_room`：0 = 「没记住」；脏值一律归 0（否则会去选一个不存在的房间）。"""

    def test_positive_int_is_kept(self):
        self.assertEqual(parse_selected_room(123), 123)
        self.assertEqual(parse_selected_room("456"), 456)

    def test_missing_or_bad_becomes_zero(self):
        for bad in (None, 0, -5, "", "不是数字", [], {}, True):
            with self.subTest(bad=bad):
                self.assertEqual(parse_selected_room(bad), 0)

    def test_load_ui_prefs_reads_it(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "gui_rooms.json"
        path.write_text(json.dumps({"rooms": [], "ui": {"selected_room": 27628019}}),
                        encoding="utf-8")
        self.assertEqual(load_ui_prefs(path)["selected_room"], 27628019)
        path.write_text(json.dumps({"rooms": [], "ui": {"selected_room": "坏值"}}),
                        encoding="utf-8")
        self.assertEqual(load_ui_prefs(path)["selected_room"], 0)
        path.write_text(json.dumps({"rooms": [], "ui": {}}), encoding="utf-8")
        self.assertEqual(load_ui_prefs(path)["selected_room"], 0)


class StartupSelectionWiringTests(unittest.TestCase):
    """两版都要：切房时记住、启动时优先恢复、恢复不了用显示顺序第一行。"""

    def test_room_switch_remembers_selection(self):
        for module, cls in BOTH:
            with self.subTest(module=module):
                node = _method(_module_tree(module), cls, "_on_room_selected")
                self.assertIn("_remember_selected_room", _called_attrs(node),
                              "切换直播间时没有记住选中项（重启后无法恢复）")

    def test_remember_writes_pref_and_saves(self):
        for module, cls in BOTH:
            with self.subTest(module=module):
                node = _method(_module_tree(module), cls, "_remember_selected_room")
                self.assertIn("selected_room", _consts(node), "未写 ui.selected_room")
                self.assertIn("_save_config", _called_attrs(node), "记住后没有落盘")

    def test_startup_prefers_remembered_room_only_if_still_present(self):
        for module, cls in BOTH:
            with self.subTest(module=module):
                tree = _module_tree(module)
                hub = _method(tree, cls, "_on_hub_ready")
                self.assertIn("_startup_room_id", _called_attrs(hub),
                              "启动没有尝试恢复上次选中的房间")
                startup = _method(tree, cls, "_startup_room_id")
                self.assertIn("selected_room", _consts(startup), "未读 ui.selected_room")
                self.assertIn("entries", _attr_names(startup),
                              "未核对「房间是否还存在」就恢复（可能选到已删除的房间）")

    def test_startup_fallback_is_first_row_in_display_order(self):
        """回退必须是**显示顺序**的第一行：Qt 此前取 entries 的第一个（与 Tk 不一致）。"""
        qt_hub = _method(_module_tree("qt_app.py"), "QtScMonitorApp", "_on_hub_ready")
        self.assertIn("_room_order", _attr_names(qt_hub),
                      "Qt 启动回退未用显示顺序（应与 Tk 版一致）")
        tk_hub = _method(_module_tree("gui_app.py"), "ScMonitorApp", "_on_hub_ready")
        self.assertIn("selection_set", _called_attrs(tk_hub), "Tk 启动未选中行")
        consts = _consts(tk_hub)
        self.assertNotIn("selected_room", consts,
                         "恢复逻辑应集中在 _startup_room_id 里，别散在 _on_hub_ready")


if __name__ == "__main__":
    unittest.main()
