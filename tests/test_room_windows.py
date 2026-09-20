"""房间独立窗口（ROADMAP 84）的离线测试：几何记忆、按房间路由、窗口生命周期的接线。

不启动任何界面：几何收拢与配置解析直接调纯函数；宿主的「按房间分发 / 一房一窗 /
退出回收 / 门控」等接线用 AST + 源码文本做静态检查（这类断链只在运行期以「窗口收不到
消息」的形式出现，离线检查比人工点界面可靠）。
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

from blive_sc_get.gui_config import (
    DEFAULT_UI_PREFS,
    load_ui_prefs,
    parse_room_window_rects,
)
from blive_sc_get.gui_app import clamp_window_rect as tk_clamp_window_rect

PKG = Path(__file__).resolve().parent.parent / "blive_sc_get"


def _source(module: str) -> str:
    return (PKG / module).read_text(encoding="utf-8")


def _method(tree: ast.Module, class_name: str, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return sub
    raise AssertionError(f"未找到 {class_name}.{name}")


def _attr_names(node) -> set:
    return {sub.attr for sub in ast.walk(node) if isinstance(sub, ast.Attribute)}


def _called_attrs(node) -> set:
    return {sub.func.attr for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)}


def _names(node) -> set:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _consts(node) -> set:
    return {sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant)}


class RoomWindowRectTests(unittest.TestCase):
    """几何记忆的解析与屏幕收拢（纯函数，两版各一份）。"""

    def test_parse_room_window_rects_filters_bad_values(self):
        ui = {"room_windows": {
            "111": {"x": 10, "y": 20, "width": 600, "height": 700},
            "abc": {"x": 0, "y": 0, "width": 600, "height": 700},       # 键不是房间号
            "222": {"x": 0, "y": 0, "width": 100, "height": 700},       # 宽度过小
            "333": {"x": "?", "y": 0, "width": 600, "height": 700},     # 字段类型不对
            "444": "not-a-dict",
        }}
        self.assertEqual(parse_room_window_rects(ui),
                         {"111": {"x": 10, "y": 20, "width": 600, "height": 700}})
        self.assertEqual(parse_room_window_rects(None), {})
        self.assertEqual(parse_room_window_rects({"room_windows": []}), {})

    def test_load_ui_prefs_reads_room_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gui_rooms.json"
            path.write_text(json.dumps({
                "rooms": [],
                "ui": {"room_windows": {"111": {"x": 1, "y": 2, "width": 640, "height": 720}}},
            }), encoding="utf-8")
            prefs = load_ui_prefs(path)
        self.assertEqual(prefs["room_windows"]["111"],
                         {"x": 1, "y": 2, "width": 640, "height": 720})
        self.assertEqual(DEFAULT_UI_PREFS["room_windows"], {})

    def test_tk_clamp_keeps_visible_and_centers_offscreen(self):
        """窗口仍有可点区域就原样保留；完全跑出屏幕则居中并收缩。"""
        self.assertEqual(tk_clamp_window_rect((100, 100, 600, 700), 1920, 1080),
                         (100, 100, 600, 700))
        x, y, w, h = tk_clamp_window_rect((5000, 3000, 600, 700), 1920, 1080)
        self.assertTrue(0 <= x <= 1920 and 0 <= y <= 1080, (x, y))
        self.assertLessEqual(w, 1920)
        self.assertLessEqual(h, 1080)

    def test_qt_clamp_keeps_visible_and_centers_offscreen(self):
        try:
            from PySide6.QtCore import QRect

            from blive_sc_get.qt_app import clamp_window_rect
        except Exception as exc:  # pragma: no cover - 环境缺 PySide6 时跳过
            raise unittest.SkipTest(f"无法导入 Qt 宿主模块：{exc}")
        area = QRect(0, 0, 1920, 1080)
        self.assertEqual(clamp_window_rect((100, 100, 600, 720), [area]),
                         (100, 100, 600, 720))
        self.assertEqual(clamp_window_rect((100, 100, 600, 720), []),
                         (100, 100, 600, 720))
        x, y, w, h = clamp_window_rect((5000, 3000, 600, 720), [area])
        self.assertTrue(0 <= x <= 1920 and 0 <= y <= 1080, (x, y))


class QtRoomWindowWiringTests(unittest.TestCase):
    """Qt 宿主与房间独立窗口的接线（静态检查）。"""

    def setUp(self):
        self.host = ast.parse(_source("qt_app.py"))
        self.window_source = _source("qt_room_window.py")

    def test_context_menu_opens_window_without_changing_selection(self):
        menu = _method(self.host, "QtScMonitorApp", "_on_room_context_menu")
        self.assertIn("open_room_window", _called_attrs(menu), "右键菜单未提供开窗入口")
        self.assertIn("window_action", _names(menu), "未区分开窗菜单项")
        self.assertNotIn("_select_room", _called_attrs(menu),
                         "开窗不该切换选中房间（SC / 弹幕面板会被切走）")

    def test_window_map_is_one_per_room_and_recycled(self):
        open_window = _method(self.host, "QtScMonitorApp", "open_room_window")
        self.assertIn("_room_windows", _attr_names(open_window))
        self.assertIn("raise_", _called_attrs(open_window), "重复开窗应前置已有窗口")
        register = _method(self.host, "QtScMonitorApp", "register_room_window")
        self.assertIn("room_id", _called_attrs(register), "未按房间号登记（一房一窗）")
        close_event = _method(self.host, "QtScMonitorApp", "closeEvent")
        self.assertIn("_close_room_windows", _called_attrs(close_event),
                      "退出时未回收房间独立窗口")
        delete = _method(self.host, "QtScMonitorApp", "_on_delete")
        self.assertIn("shutdown", _called_attrs(delete), "删除房间时未关闭对应窗口")

    def test_events_are_routed_by_room(self):
        client_event = _method(self.host, "QtScMonitorApp", "_on_client_event")
        called = _called_attrs(client_event)
        self.assertIn("sc_panels_for", called, "SC 事件未按房间分发")
        self.assertIn("dm_panels_for", called, "弹幕事件未按房间入桶")
        poll = _method(self.host, "QtScMonitorApp", "_poll_queue")
        self.assertIn("dm_panels_for", _called_attrs(poll), "弹幕批次未按房间分发")
        self.assertIn("token", _consts(poll), "历史结果未按 token 回投发起面板")
        self.assertIn("_dm_panels", _attr_names(poll), "表情图片未广播给各弹幕面板")

    def test_dm_gate_covers_window_rooms(self):
        gate = _method(self.host, "QtScMonitorApp", "_apply_dm_gate")
        self.assertIn("_dm_rooms", _called_attrs(gate), "弹幕门控未覆盖独立窗口房间")
        rooms = _method(self.host, "QtScMonitorApp", "_dm_rooms")
        self.assertIn("always_visible", _consts(rooms),
                      "独立窗口的弹幕区应恒显示（不随主界面开关关闭）")

    def test_window_module_binds_and_unbinds_views(self):
        tree = ast.parse(self.window_source)
        init = _method(tree, "RoomChatWindow", "__init__")
        self.assertIn("register_room_window", _called_attrs(init), "窗口未登记到宿主")
        shutdown = _method(tree, "RoomChatWindow", "shutdown")
        for attr in ("unregister_sc_panel", "unregister_dm_panel",
                     "unregister_room_window", "_apply_dm_gate"):
            self.assertIn(attr, _called_attrs(shutdown), f"收窗未调用 {attr}")
        restore = _method(tree, "RoomChatWindow", "restore_geometry")
        self.assertIn("load_room_window_geometry", _called_attrs(restore),
                      "未恢复记忆的几何")
        save = _method(tree, "RoomChatWindow", "_save_geometry")
        self.assertIn("save_room_window_geometry", _called_attrs(save), "几何未写回配置")

    def test_emoticon_image_switch_is_shared_across_windows(self):
        """「弹幕表情图」开关：主界面与各独立窗口**双向同步**，且都是同一个统一入口。

        独立窗口里必须**有自己的勾选框**（用户反馈过：只在主界面能切换，窗口里看不到
        也改不了），任一处切换都要落到 `set_dm_emoticon_image`：广播给每个弹幕面板 +
        回写主界面与所有窗口的勾选框（回写要 `blockSignals`，否则与 `toggled` 回环）。
        """
        toggle = _method(self.host, "QtScMonitorApp", "_on_dm_emoticon_image_toggled")
        self.assertIn("set_dm_emoticon_image", _called_attrs(toggle),
                      "主界面勾选框未走统一入口")
        setter = _method(self.host, "QtScMonitorApp", "set_dm_emoticon_image")
        called = _called_attrs(setter)
        self.assertIn("set_emoticon_image_enabled", called, "未广播给各弹幕面板")
        self.assertIn("set_emoticon_check", called, "未回写各独立窗口的勾选框")
        self.assertIn("blockSignals", called, "回写勾选框时未屏蔽信号（会与 toggled 回环）")

        tree = ast.parse(self.window_source)
        init = _method(tree, "RoomChatWindow", "__init__")
        self.assertIn("dm_emoticon_check", _attr_names(init),
                      "独立窗口里缺少「弹幕表情图」勾选框")
        self.assertIn("_on_emoticon_image_toggled", _attr_names(init),
                      "独立窗口的勾选框未接处理函数")
        handler = _method(tree, "RoomChatWindow", "_on_emoticon_image_toggled")
        self.assertIn("set_dm_emoticon_image", _called_attrs(handler),
                      "窗口勾选框未走宿主统一入口（会与主界面状态不一致）")
        back = _method(tree, "RoomChatWindow", "set_emoticon_check")
        self.assertIn("blockSignals", _called_attrs(back),
                      "宿主回写窗口勾选框时未屏蔽信号")

    def test_window_does_not_steal_focus_but_not_topmost(self):
        tree = ast.parse(self.window_source)
        init = _method(tree, "RoomChatWindow", "__init__")
        attrs = _attr_names(init)
        self.assertIn("WA_ShowWithoutActivating", attrs, "打开时会抢主窗口焦点")
        self.assertNotIn("WindowStaysOnTopHint", attrs, "子窗口不该置顶")


class TkRoomWindowWiringTests(unittest.TestCase):
    """Tk 宿主与房间独立窗口的接线（静态检查）。"""

    def setUp(self):
        self.host = ast.parse(_source("gui_app.py"))

    def test_right_click_opens_window(self):
        build = _method(self.host, "ScMonitorApp", "_build_rooms_tab")
        self.assertIn("_on_tree_right_click", _attr_names(build), "房间列表未接右键菜单")
        menu = _method(self.host, "ScMonitorApp", "_on_tree_right_click")
        self.assertIn("open_room_window", _called_attrs(menu), "右键菜单未提供开窗入口")
        self.assertIn("identify_row", _called_attrs(menu), "未按右键位置定位房间")

    def test_window_lifecycle_and_geometry(self):
        open_window = _method(self.host, "ScMonitorApp", "open_room_window")
        self.assertIn("_room_windows", _attr_names(open_window))
        self.assertIn("lift", _called_attrs(open_window), "重复开窗应前置已有窗口")
        destroy = _method(self.host, "ScMonitorApp", "_destroy")
        self.assertIn("_close_room_windows", _called_attrs(destroy),
                      "退出时未回收房间独立窗口")
        delete = _method(self.host, "ScMonitorApp", "_on_delete")
        self.assertIn("shutdown", _called_attrs(delete), "删除房间时未关闭对应窗口")
        save = _method(self.host, "ScMonitorApp", "save_room_window_geometry")
        self.assertIn("_save_config", _called_attrs(save), "几何未写回配置")
        load = _method(self.host, "ScMonitorApp", "load_room_window_geometry")
        self.assertIn("clamp_window_rect", _names(load), "记忆的位置未做屏幕收拢")

    def test_tk_events_are_broadcast_by_room(self):
        client_event = _method(self.host, "ScMonitorApp", "_on_client_event")
        called = _called_attrs(client_event)
        for attr in ("on_sc", "on_delete", "on_meta_changed"):
            self.assertIn(attr, called, f"{attr} 未广播给独立窗口")
        self.assertIn("room_windows_for", called, "未按房间找窗口")
        poll = _method(self.host, "ScMonitorApp", "_poll_queue")
        self.assertIn("on_dm_batch", _called_attrs(poll), "弹幕批次未分发给窗口")
        self.assertIn("on_emoticon_image", _called_attrs(poll), "表情图片未广播给窗口")
        gate = _method(self.host, "ScMonitorApp", "_apply_dm_gate")
        self.assertIn("_dm_rooms", _called_attrs(gate), "弹幕门控未覆盖独立窗口房间")

    def test_window_uses_no_activate_and_own_queue(self):
        tree = ast.parse(_source("tk_room_window.py"))
        init = _method(tree, "RoomChatWindow", "__init__")
        self.assertIn("apply_noactivate", _names(init), "打开时会抢主窗口焦点")
        self.assertIn("register_room_window", _called_attrs(init), "窗口未登记到宿主")
        self.assertIn("_apply_dm_gate", _called_attrs(init), "开窗后未重设弹幕门控")
        shutdown = _method(tree, "RoomChatWindow", "shutdown")
        for attr in ("unregister_room_window", "_apply_dm_gate", "destroy"):
            self.assertIn(attr, _called_attrs(shutdown), f"收窗未调用 {attr}")
        # 子窗口不消费宿主的 ui_queue（否则会抢主界面的事件）
        self.assertNotIn("ui_queue", _attr_names(tree))


if __name__ == "__main__":
    unittest.main()
