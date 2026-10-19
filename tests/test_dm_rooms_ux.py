"""ROADMAP 112 / 114 与全局快捷弹幕（113）的接线测试。

112：Qt 房间列表整表只读（保留整行选择与 Ctrl 多选）；
113：三处下拉都合并「全局 + 房间」候选，两版管理对话框都有两个列表并在关闭时分别落盘；
114：Qt 独立窗口创建面板后必须调用 ``on_room_selected``，否则快捷弹幕下拉永远是灰的
（顺带让 106 的颜色 / 模式记忆在独立窗口生效）。

Qt 控件一律在 offscreen 下构造；DmPanel 用最小假宿主（只提供它构造与选房时真正会碰到的属性）。
"""

import ast
import os
import queue
import unittest
from pathlib import Path

from blive_sc_get.gui_config import RoomEntry
from blive_sc_get.qt_app import QtScMonitorApp

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "blive_sc_get"


def _tree(module: str) -> ast.Module:
    return ast.parse((PKG / module).read_text(encoding="utf-8"))


def _method(module: str, cls: str, name: str) -> ast.FunctionDef:
    klass = next((n for n in ast.walk(_tree(module))
                  if isinstance(n, ast.ClassDef) and n.name == cls), None)
    if klass is None:
        raise AssertionError(f"{module} 缺少类 {cls}")
    node = next((n for n in klass.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise AssertionError(f"{module} 的 {cls} 缺少 {name}")
    return node


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class RoomTableReadOnlyTests(unittest.TestCase):
    """112：房间表双击 / F2 都不能再改单元格（改的只是界面文本，与真实数据不一致）。"""

    def test_edit_triggers_disabled(self):
        src = ast.unparse(_method("qt_app.py", "QtScMonitorApp", "_build_rooms_tab"))
        self.assertIn("setEditTriggers", src, "房间表未关闭编辑触发器")
        self.assertIn("NoEditTriggers", src)

    def test_items_strip_editable_flag(self):
        src = ast.unparse(_method("qt_app.py", "QtScMonitorApp", "_refresh_row"))
        self.assertIn("setFlags", src, "未逐项设置 flags")
        self.assertIn("~Qt.ItemIsEditable", src,
                      "只关触发器不够：单元格项仍带 ItemIsEditable")

    def test_row_selection_and_ctrl_multi_kept(self):
        """整行选择与 Ctrl 多选不能动（批量加入预览在用）。"""
        src = ast.unparse(_method("qt_app.py", "QtScMonitorApp", "_build_rooms_tab"))
        self.assertIn("SelectRows", src)
        self.assertIn("ExtendedSelection", src)


class QuickDanmakuGlobalWiringTests(unittest.TestCase):
    """113：三处下拉合并全局项；两版对话框两个列表、关闭时分别落盘。"""

    def test_three_dropdowns_merge_global(self):
        for module, cls in (("gui_app.py", "ScMonitorApp"),
                            ("tk_room_window.py", "RoomChatWindow"),
                            ("qt_dm_panel.py", "DmPanel")):
            with self.subTest(module=module):
                src = ast.unparse(_method(module, cls, "_refresh_quick_danmaku"))
                self.assertIn("merge_quick_presets", src, f"{module} 未合并全局项")
                self.assertIn("global_quick_danmaku", src, f"{module} 未读全局列表")

    def test_right_click_transfer_wired(self):
        """右键「转移」（ROADMAP 115）：两版都要有右键菜单，走共享移动函数、一次写两档。"""
        for module, cls, func in (
                ("gui_app.py", "ScMonitorApp", "_open_quick_danmaku_manager"),
                ("qt_app.py", "QtScMonitorApp", "open_quick_danmaku_manager")):
            with self.subTest(module=module):
                node = _method(module, cls, func)
                src = ast.unparse(node)
                self.assertIn("move_quick_preset", src, f"{module} 未走共享的移动函数")
                transfer = [n for n in ast.walk(node)
                            if isinstance(n, ast.FunctionDef) and n.name == "transfer"]
                self.assertTrue(transfer, f"{module} 缺少 transfer")
                transfer_src = ast.unparse(transfer[0])
                # 转移同时改动两档：persist 要一次收到源与目标两个范围（只写一次盘）
                self.assertRegex(transfer_src, r"persist\((?:\w+),\s*target",
                                 "转移后未同时落盘两档")
                if module == "gui_app.py":
                    self.assertIn("tk.Menu(", src, "Tk 未创建右键菜单")
                    self.assertIn("<Button-3>", src, "Tk 未绑定右键事件")
                    self.assertIn("tk_popup", src, "Tk 未弹出菜单")
                else:
                    self.assertIn("CustomContextMenu", src, "Qt 未启用自定义右键菜单")
                    self.assertIn("QMenu", src, "Qt 未创建菜单")
                    self.assertIn("setWindowFlag", src, "Qt 菜单未置顶（会被置顶浮窗盖住）")
                    # 只看真实调用（docstring 里那句「show() 而非 exec()」不算）
                    calls = {n.func.attr for n in ast.walk(node)
                             if isinstance(n, ast.Call)
                             and isinstance(n.func, ast.Attribute)}
                    self.assertNotIn("exec", calls, "对话框/菜单不该用 exec 阻塞")

    def test_tk_dialog_has_two_lists(self):
        src = ast.unparse(
            _method("gui_app.py", "ScMonitorApp", "_open_quick_danmaku_manager"))
        # 列表由内部 make_section() 建（一次定义、两处调用），故按调用点判断
        self.assertIn("tk.Listbox(", src, "Tk 管理对话框没有列表控件")
        self.assertIn("global_box = make_section(", src, "Tk 对话框缺少全局列表")
        self.assertIn("room_box = make_section(", src, "Tk 对话框缺少本房间列表")
        self.assertIn("global_quick_danmaku", src, "Tk 管理对话框未处理全局列表")
        self.assertIn("_save_config", src, "关闭时未落盘")
        self.assertIn("_refresh_quick_danmaku", src, "保存后未刷新下拉")

    def test_dialog_has_explicit_add_target(self):
        """添加目标用**显式单选**（全局 / 本房间，默认本房间）：空列表也能加第一条。

        （此前靠「有选中项的列表、否则最后点过的」推断：全局列表为空时 Qt 侧根本无处可点，
        第一条全局项加不进去。）
        """
        tk_src = ast.unparse(
            _method("gui_app.py", "ScMonitorApp", "_open_quick_danmaku_manager"))
        self.assertEqual(tk_src.count("ttk.Radiobutton("), 1, "Tk 缺少「添加到」单选")
        self.assertIn("StringVar(value='room')", tk_src, "Tk 默认目标应是本房间")
        qt_src = ast.unparse(
            _method("qt_app.py", "QtScMonitorApp", "open_quick_danmaku_manager"))
        self.assertIn("QRadioButton", qt_src, "Qt 未提供「添加到」单选")
        self.assertIn("room_radio.setChecked(True)", qt_src, "Qt 默认目标应是本房间")

    def test_dialog_saves_immediately(self):
        """每次添加 / 删除 / 移动都**立即**落盘（用户要求），不再等关闭时统一保存。"""
        for module, cls, name in (
                ("gui_app.py", "ScMonitorApp", "_open_quick_danmaku_manager"),
                ("qt_app.py", "QtScMonitorApp", "open_quick_danmaku_manager")):
            with self.subTest(module=module):
                src = ast.unparse(_method(module, cls, name))
                self.assertIn("def persist(", src, "缺少「立即落盘」的帮助函数")
                persist = [n for n in ast.walk(
                    _method(module, cls, name))
                    if isinstance(n, ast.FunctionDef) and n.name == "persist"]
                self.assertTrue(persist, "未找到 persist 定义")
                self.assertIn("_save_config", ast.unparse(persist[0]),
                              "persist 未写盘")
                # 增删移三处都要调 persist
                for operation in ("add", "remove", "move"):
                    node = [n for n in ast.walk(_method(module, cls, name))
                            if isinstance(n, ast.FunctionDef) and n.name == operation]
                    self.assertTrue(node, f"缺少 {operation}")
                    self.assertIn("persist(", ast.unparse(node[0]),
                                  f"{operation} 后未立即落盘")

    def test_qt_dialog_has_two_lists(self):
        src = ast.unparse(
            _method("qt_app.py", "QtScMonitorApp", "open_quick_danmaku_manager"))
        self.assertEqual(src.count("QListWidget()"), 2, "Qt 管理对话框应有两个列表")
        self.assertIn("global_quick_danmaku", src, "Qt 管理对话框未处理全局列表")
        self.assertIn("_save_config", src, "关闭时未落盘")
        self.assertIn("_refresh_quick_danmaku", src, "保存后未刷新下拉")


class _FakeQtHost:
    """DmPanel 的最小假宿主：只提供它构造与选房路径真正会碰到的属性。"""

    _make_unseen_badge = QtScMonitorApp._make_unseen_badge
    _place_unseen_badge = QtScMonitorApp._place_unseen_badge

    def __init__(self):
        from PySide6.QtWidgets import QTextEdit

        self.sc_text = QTextEdit()            # 面板取它来对齐字体
        self.ui_prefs = {"copy_dm_fill": False, "quick_dm_send_now": False,
                         "global_quick_danmaku": ["打卡"]}
        self.entries = {111: RoomEntry(111, quick_danmaku=["打卡", "房间专属"],
                                       dm_color="红色")}
        self._selected_room_id = 111
        self.dm_var = True
        self.ui_queue = queue.Queue()
        self._dm_send_history = {}
        self._emoticon_memory = {}
        self._dm_panels = []
        self.app_config = type("Cfg", (), {"allow_write_operations": True,
                                           "emoticon_tooltip": ""})()
        self.hub = type("Hub", (), {
            "api": type("Api", (), {"logged_in": True, "csrf": "x"})()})()

    def register_dm_panel(self, panel):
        self._dm_panels.append(panel)

    def unregister_dm_panel(self, panel):
        if panel in self._dm_panels:
            self._dm_panels.remove(panel)

    def copy_to_clipboard(self, _text):
        pass

    def real_room_id(self, room_id):
        return room_id

    def _save_config(self):
        pass

    def _dm_history_of(self, _room_id):
        return []

    # 表情相关：本测试不涉及，给空实现
    def emoticons_for(self, _room_id):
        return []

    def request_room_packages(self, _room_id):
        pass

    def request_emoticon_images(self, _urls):
        pass

    def remember_emoticon_page(self, *_args, **_kwargs):
        pass

    def open_quick_danmaku_manager(self, _room_id):
        pass

    def set_quick_dm_send_now(self, _enabled):
        pass

    def set_copy_dm_fill(self, _enabled):
        pass


class QtRoomWindowQuickDanmakuTests(unittest.TestCase):
    """114：独立窗口创建面板后要初始化发送状态，否则快捷弹幕下拉永远是灰的。"""

    def test_room_window_calls_on_room_selected(self):
        src = ast.unparse(_method("qt_room_window.py", "RoomChatWindow", "__init__"))
        self.assertIn("on_room_selected", src,
                      "独立窗口创建面板后未初始化（快捷弹幕下拉会一直灰着）")

    def test_panel_combo_enabled_after_room_selected(self):
        """复现 114：构造期下拉是灰的 → 选房后必须可用，且候选 = 占位 + 全局 + 房间。"""
        from blive_sc_get.qt_dm_panel import DmPanel

        app = _qapp()
        host = _FakeQtHost()
        panel = DmPanel(host, lambda: 111)
        try:
            self.assertFalse(panel.quick_dm_combo.isEnabled(),
                             "构造期本来就该是灰的（等选房后再亮）")
            panel.on_room_selected(111)
            self.assertTrue(panel.quick_dm_combo.isEnabled(),
                            "选了房间后快捷弹幕下拉仍不可用（114 未修好）")
            texts = [panel.quick_dm_combo.itemText(i)
                     for i in range(panel.quick_dm_combo.count())]
            self.assertEqual(texts, ["快捷弹幕", "打卡", "房间专属"],
                             "候选应为「占位 + 全局在前 + 房间在后」，同名只出现一次")
            # 106 的附带修正：颜色 / 模式记忆在独立窗口也要套用
            self.assertEqual(panel.dm_colors.currentText(), "红色",
                             "独立窗口未套用颜色记忆")
        finally:
            panel.deleteLater()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
