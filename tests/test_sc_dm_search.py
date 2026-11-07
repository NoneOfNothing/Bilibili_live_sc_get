"""SC / 弹幕 Ctrl+F 搜索（ROADMAP 118）的行为与接线测试。

- Qt：offscreen + 假宿主，验证高亮/计数/循环跳转/Esc 清理，以及 SC 全量搜索
  命中未加载记录时的补页跳转；
- Tk：AST 接线（Ctrl+F 焦点判断、搜索条创建、切房清理）。
"""

import ast
import os
import queue
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PKG = Path(__file__).resolve().parent.parent / "blive_sc_get"


def _tree(module: str) -> ast.Module:
    return ast.parse((PKG / module).read_text(encoding="utf-8"))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到类 {name}")


def _method(tree: ast.Module, class_name: str, name: str):
    for node in ast.walk(_class(tree, class_name)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"未找到方法 {class_name}.{name}")


def _names(node: ast.AST) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _attrs(node: ast.AST) -> set:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _called_attrs(node: ast.AST) -> set:
    return {n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _pump(panel, host, timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kind, payload = host.ui_queue.get(timeout=0.03)
        except queue.Empty:
            continue
        if kind == "history":
            panel.on_history_loaded(payload)
        elif kind == "sc_search":
            panel.on_sc_search_result(payload)


class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])


class DmSearchTests(_QtCase):
    def _panel(self):
        from blive_sc_get.qt_dm_panel import DmPanel
        from tests.test_dm_rooms_ux import _FakeQtHost

        host = _FakeQtHost()
        host.hub.submit = lambda _job: None
        panel = DmPanel(host, lambda: 111)
        panel.on_room_selected(111)
        panel.append_dm_batch([
            {"text": "今天天气不错", "uname": "甲", "uid": 1, "dmid": "a", "time": 1},
            {"text": "无关内容", "uname": "乙", "uid": 2, "dmid": "b", "time": 2},
            {"text": "天气真好", "uname": "丙", "uid": 3, "dmid": "c", "time": 3},
        ])
        self.addCleanup(panel.deleteLater)
        return panel

    def test_find_highlight_and_cycle(self):
        panel = self._panel()
        panel._open_search()
        panel.search_bar.set_query("天气")
        panel._run_dm_search("天气")
        self.assertEqual(len(panel._dm_search_matches), 2)
        self.assertEqual(panel.search_bar.counter.text(), "第 1/2 个")
        self.assertTrue(panel.dm_text.extraSelections(), "没有高亮")
        # 优先最新：第 1 个是文档末尾那条
        first = panel._dm_search_matches[0].text()
        self.assertIn("天气真好", first)

        panel._step_dm_search(1)
        self.assertEqual(panel._dm_search_index, 1)
        self.assertEqual(panel.search_bar.counter.text(), "第 2/2 个")
        panel._step_dm_search(1)  # 循环回最新
        self.assertEqual(panel._dm_search_index, 0)

    def test_escape_and_close_clear_highlight(self):
        panel = self._panel()
        panel._open_search()
        panel._run_dm_search("天气")
        panel._on_escape()
        self.assertFalse(panel.search_bar.isVisible())
        self.assertEqual(panel.dm_text.extraSelections(), [])
        self.assertEqual(panel._dm_search_matches, [])

    def test_clear_view_closes_search(self):
        panel = self._panel()
        panel._open_search()
        panel._run_dm_search("天气")
        panel.clear_view()
        self.assertFalse(panel.search_bar.isVisible())
        self.assertEqual(panel.dm_text.extraSelections(), [])

    def test_empty_query(self):
        panel = self._panel()
        panel._open_search()
        panel._run_dm_search("")
        self.assertEqual(panel.search_bar.counter.text(), "无匹配")
        self.assertFalse(panel.search_bar.next_btn.isEnabled())

    def test_new_danmaku_keeps_current_hit(self):
        """新弹幕到达时不能把当前命中重置为第 1 个（用户报告的 BUG）。"""
        panel = self._panel()
        panel._open_search()
        panel.search_bar.set_query("天气")
        panel._run_dm_search("天气")
        panel._step_dm_search(1)
        self.assertEqual(panel._dm_search_index, 1)
        current = panel._dm_search_matches[1].text()

        panel.append_dm_batch([
            {"text": "新弹幕一条", "uname": "丁", "uid": 4, "dmid": "d", "time": 4},
        ])
        # 走真实接线：文档变化后由 200ms 定时器重算（必须带 keep_current）
        panel._dm_search_refresh.stop()
        panel._dm_search_refresh.timeout.emit()

        self.assertEqual(panel._dm_search_index, 1, "当前命中被重置为第一个")
        self.assertEqual(panel.search_bar.counter.text(), "第 2/2 个")
        self.assertEqual(panel._dm_search_matches[1].text(), current)

    def test_new_matching_danmaku_keeps_anchor(self):
        """新增一条也命中的弹幕：命中数变化，但当前选中仍是原来那条。"""
        panel = self._panel()
        panel._open_search()
        panel.search_bar.set_query("天气")
        panel._run_dm_search("天气")
        panel._step_dm_search(1)
        current = panel._dm_search_matches[1].text()

        panel.append_dm_batch([
            {"text": "天气 又一条", "uname": "丁", "uid": 4, "dmid": "d", "time": 4},
        ])
        panel._dm_search_refresh.stop()
        panel._dm_search_refresh.timeout.emit()

        self.assertEqual(len(panel._dm_search_matches), 3)
        self.assertEqual(panel._dm_search_matches[panel._dm_search_index].text(),
                         current, "新增命中后锚点跑到别的记录了")
        self.assertEqual(panel.search_bar.counter.text(), "第 3/3 个")


class ScFullSearchTests(_QtCase):
    def setUp(self):
        from blive_sc_get.qt_sc_panel import ScPanel
        from tests.test_sc_history_paging import _FakeHost

        self.host = _FakeHost()
        self.panel = ScPanel(self.host, lambda: self.host._selected_room_id)
        self.panel.resize(600, 380)
        self.panel.show()
        self.app.processEvents()
        self.panel.apply_room(1)
        time.sleep(0.15)
        _pump(self.panel, self.host)
        self.app.processEvents()
        self.addCleanup(self.panel.deleteLater)

    def test_full_history_hits(self):
        self.panel._open_search()
        self.panel.search_bar.set_query("消息")
        self.panel._run_sc_search("消息")
        _pump(self.panel, self.host)
        self.assertEqual(len(self.panel._search_hits), 250)
        self.assertEqual(self.panel.search_bar.counter.text(), "第 1/250 个")
        self.assertTrue(self.panel.sc_text.extraSelections())

    def test_jump_to_unloaded_hit_loads_page(self):
        self.panel._open_search()
        self.panel._run_sc_search("消息")
        _pump(self.panel, self.host)
        hit = self.panel._search_hits[150]  # 第 151 条：尚在未加载的第二页
        self.panel._search_index = 150
        self.panel._jump_to_search_hit(hit)
        self.assertTrue(self.panel._loading_more or self.panel._loaded_count > 150)
        _pump(self.panel, self.host, timeout=1.5)
        self.assertGreaterEqual(self.panel._loaded_count, 151)
        self.assertIsNone(self.panel._search_pending_hit,
                          "补加载完成后仍未定位到命中记录")
        self.assertIn((1, 100), [(room, skip) for room, skip, _l
                                 in self.host.storage.calls])

    def test_close_clears_search_state(self):
        self.panel._open_search()
        self.panel._run_sc_search("消息")
        _pump(self.panel, self.host)
        self.panel._close_search()
        self.assertFalse(self.panel.search_bar.isVisible())
        self.assertEqual(self.panel.sc_text.extraSelections(), [])
        self.assertEqual(self.panel._search_hits, [])

    def test_new_sc_keeps_search_index(self):
        """新 SC 到达只重建高亮，不重置当前命中的序号。"""
        self.panel._open_search()
        self.panel._run_sc_search("消息")
        _pump(self.panel, self.host)
        self.panel._search_index = 10
        self.panel.append_sc("2026-10-25 12:00:00", {
            "id": 99999, "uid": 1, "user": {"uname": "新观众"},
            "message": "消息 新内容", "price": 30, "time": 30,
        })
        self.panel._refresh_sc_search_highlight()
        self.assertEqual(self.panel._search_index, 10,
                         "新 SC 到达后搜索序号被重置")
        self.assertEqual(len(self.panel._search_hits), 250)


class TkSearchWiringTests(unittest.TestCase):
    def test_main_ctrl_f_follows_focus(self):
        tree = _tree("gui_app.py")
        handler = _method(tree, "ScMonitorApp", "_on_ctrl_f")
        self.assertIn("_focus_in", _called_attrs(handler))
        self.assertIn("_open_dm_search", _called_attrs(handler))
        self.assertIn("_open_sc_search", _called_attrs(handler))
        method = _method(tree, "ScMonitorApp", "_run_sc_search")
        self.assertIn("iter_sc_hits", _names(method))
        loaded = _method(tree, "ScMonitorApp", "_on_history_loaded")
        self.assertIn("_resolve_sc_search_pending", _called_attrs(loaded))
        clear = _method(tree, "ScMonitorApp", "_clear_sc_view")
        self.assertIn("_close_sc_search", _called_attrs(clear))
        self.assertIn("<Control-f>",
                      (PKG / "gui_app.py").read_text(encoding="utf-8"))

    def test_room_window_has_search(self):
        tree = _tree("tk_room_window.py")
        handler = _method(tree, "RoomChatWindow", "_on_ctrl_f")
        self.assertIn("_focus_in", _called_attrs(handler))
        self.assertIn("_open_dm_search", _called_attrs(handler))
        self.assertIn("_open_sc_search", _called_attrs(handler))
        loaded = _method(tree, "RoomChatWindow", "_on_history_loaded")
        self.assertIn("_resolve_sc_search_pending", _called_attrs(loaded))
        source = (PKG / "tk_room_window.py").read_text(encoding="utf-8")
        self.assertIn("SearchBar(", source)
        self.assertIn("<Control-f>", source)

    def test_qt_shortcuts_and_search_bars(self):
        for module, cls in (("qt_sc_panel.py", "ScPanel"), ("qt_dm_panel.py", "DmPanel")):
            with self.subTest(module=module):
                tree = _tree(module)
                klass = _class(tree, cls)
                init = _method(tree, cls, "_build_ui")
                self.assertIn("SearchBar", _names(init) | _called_attrs(init),
                              f"{cls} 没有创建搜索条")
                source = (PKG / module).read_text(encoding="utf-8")
                self.assertIn("QKeySequence.Find", source)
                self.assertIn("WidgetWithChildrenShortcut", source)

    def test_tk_new_danmaku_keeps_hit(self):
        """新弹幕到达后的重算必须带 keep_current（否则跳回第一个）。"""
        for module, cls in (("gui_app.py", "ScMonitorApp"),
                            ("tk_room_window.py", "RoomChatWindow")):
            with self.subTest(module=module):
                tree = _tree(module)
                schedule = _method(tree, cls, "_schedule_dm_search_tags")
                self.assertIn("keep_current=True", ast.unparse(schedule),
                              f"{cls} 的新弹幕重算会重置搜索选中")
                run = _method(tree, cls, "_run_dm_search")
                args = {a.arg for a in run.args.args}
                args |= {a.arg for a in run.args.kwonlyargs}
                self.assertIn("keep_current", args)

    def test_qt_highlight_cleared_on_close(self):
        tree = _tree("qt_sc_panel.py")
        close = _method(tree, "ScPanel", "_close_search")
        self.assertIn("_clear_sc_search_highlight", _called_attrs(close))
        clear = _method(tree, "ScPanel", "_clear_sc_search_highlight")
        self.assertIn("setExtraSelections", _called_attrs(clear))


if __name__ == "__main__":
    unittest.main()
