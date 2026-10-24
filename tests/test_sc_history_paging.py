"""SC 历史分页状态机（ROADMAP 117）：上滑加载过一次后，重进房间仍必须能继续加载。

实测故障链（离屏探针复现）：
1. 切房/重进时 `on_history_loaded(skip=0)` 里调用 `clear_view()`；
2. `clear_view()` 先 `sc_text.clear()`，把滚动条打到 0 并触发 `valueChanged`；
3. 此时 `_has_more` 刚被置真、`_loading_more` 为假 → 误发一次 `skip>0` 请求；
4. `clear_view()` 随后 `_history_gen += 1`，把刚发出的请求代际作废；
5. 结果回来时 `gen` 不匹配被丢弃，`_loading_more` 永久保持 True；
6. 之后滚轮 / 滚动条 / 中键全部被 `_loading_more` 挡住，直到重启程序。

修复：代际只由 `load_history` 推进；清空文本时屏蔽到顶触发；结果回来（哪怕被丢弃）
按 `_pending_gen` 解除加载中；到顶滚轮可重试；切房重置分页状态。
"""

import ast
import os
import queue
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from blive_sc_get.gui_config import RoomEntry

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PKG = Path(__file__).resolve().parent.parent / "blive_sc_get"
TOTAL = 250
PAGE = 100


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


def _record(index: int) -> dict:
    return {
        "time_received": "2026-10-25 12:00:00",
        "sc": {
            "id": index, "uid": 1000 + index,
            "user": {"uname": f"user{index}"},
            "message": f"消息 {index} " * 4,
            "price": 30, "time": 30,
        },
    }


ALL = [_record(i) for i in range(TOTAL)]


class _FakeStorage:
    def __init__(self):
        self.calls = []

    def load_sc_page(self, room_id, limit=PAGE, skip=0):
        self.calls.append((room_id, skip, limit))
        end = TOTAL - skip
        start = max(0, end - limit)
        return list(ALL[start:end])

    def iter_sc_history_desc(self, room_id):
        """最新 → 更旧（与真实 storage 一致，供全量搜索使用）。"""
        return reversed(ALL)

    def load_deleted_ids(self, room_id):
        return {}

    def count_sc_records(self, room_id):
        return TOTAL


class _FakeHost:
    from blive_sc_get.qt_app import QtScMonitorApp as _App

    _make_unseen_badge = _App._make_unseen_badge
    _place_unseen_badge = _App._place_unseen_badge

    def __init__(self):
        self.storage = _FakeStorage()
        self.ui_queue = queue.Queue()
        self.hub = SimpleNamespace(storage=self.storage)
        self._sc_total = {}
        self.anchor_names = {}
        self.titles = {}
        self.viewers = {}
        self.guard_num = {}
        self.live_started_at = {}
        self.live_state = {}
        self._sc_panels = {}
        self._selected_room_id = 1
        self.entries = {1: RoomEntry(1), 2: RoomEntry(2)}

    def register_sc_panel(self, panel):
        self._sc_panels[panel.token] = panel

    def _fan_medal_for(self, room_id):
        return "", 0


def _pump(panel, host, timeout=0.8):
    """把宿主队列里的 history 结果回投给面板（模拟 _poll_queue 的 token 路由）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kind, payload = host.ui_queue.get(timeout=0.03)
        except queue.Empty:
            continue
        if kind == "history":
            panel.on_history_loaded(payload)


class _QtPanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from blive_sc_get.qt_sc_panel import ScPanel

        self.host = _FakeHost()
        self.panel = ScPanel(self.host, lambda: self.host._selected_room_id)
        self.panel.resize(600, 380)
        self.panel.show()
        self.app.processEvents()
        self.addCleanup(self.panel.deleteLater)

    def _step(self, room_id):
        self.host._selected_room_id = room_id
        self.panel.apply_room(room_id)
        time.sleep(0.15)
        _pump(self.panel, self.host)
        self.app.processEvents()


class ReenterPagingTests(_QtPanelCase):
    """用户报告的核心路径：加载过一次后，重进房间仍能上滑。"""

    def test_reenter_still_loads(self):
        self._step(1)
        self.assertEqual(self.panel._loaded_count, 100)
        self.assertTrue(self.panel._has_more)

        self.panel.sc_text.verticalScrollBar().setValue(0)
        time.sleep(0.15)
        _pump(self.panel, self.host)
        self.assertEqual(self.panel._loaded_count, 200)

        self._step(2)   # 切走
        self._step(1)   # 重进
        self.assertFalse(self.panel._loading_more,
                         "重进房间后 _loading_more 卡在 True，上滑会被永久挡掉")
        self.assertTrue(self.panel._has_more)
        self.assertEqual(self.panel._loaded_count, 100)

        self.panel.sc_text.verticalScrollBar().setValue(0)
        time.sleep(0.15)
        _pump(self.panel, self.host)
        self.assertEqual(self.panel._loaded_count, 200,
                         "重进后上滑没有加载更早的一页")
        self.assertFalse(self.panel._loading_more)

    def test_clear_does_not_fire_extra_page(self):
        """清空旧房间内容不能顺带发一次翻页请求（随后会被代际作废并卡死）。"""
        self._step(1)
        self._step(2)
        skips = [(room, skip) for room, skip, _limit in self.host.storage.calls]
        self.assertIn((2, 0), skips)
        self.assertNotIn((2, 100), skips,
                         "切换房间时自动发了一次 skip>0 请求（清空触发的误请求）")

    def test_last_page_stops(self):
        self._step(1)
        for _ in range(2):
            self.panel.sc_text.verticalScrollBar().setValue(0)
            time.sleep(0.15)
            _pump(self.panel, self.host)
        self.assertEqual(self.panel._loaded_count, TOTAL)
        self.assertFalse(self.panel._has_more)

        before = len(self.host.storage.calls)
        self.panel.sc_text.verticalScrollBar().setValue(0)
        time.sleep(0.1)
        _pump(self.panel, self.host)
        self.assertEqual(len(self.host.storage.calls), before,
                         "已读完全部历史后仍在请求")


class StaleResultTests(_QtPanelCase):
    """过期结果回来时必须解除加载中，不能把状态永久卡死。"""

    def test_stale_result_clears_loading(self):
        self.panel._loading_more = True
        self.panel._pending_gen = 7
        self.panel._history_gen = 9  # 已被新请求取代
        self.panel.on_history_loaded({
            "gen": 7, "room_id": 1, "records": [], "deleted": {},
            "total": TOTAL, "skip": 100, "token": self.panel.token,
        })
        self.assertFalse(self.panel._loading_more,
                         "被丢弃的过期结果没有解除 _loading_more")

    def test_wrong_room_result_clears_loading(self):
        self.panel._loading_more = True
        self.panel._pending_gen = 3
        self.panel._history_gen = 3
        self.host._selected_room_id = 2
        self.panel.on_history_loaded({
            "gen": 3, "room_id": 1, "records": [], "deleted": {},
            "total": TOTAL, "skip": 100, "token": self.panel.token,
        })
        self.assertFalse(self.panel._loading_more)

    def test_apply_room_resets_loading(self):
        self.panel._loading_more = True
        self.panel.apply_room(2)
        self.assertFalse(self.panel._loading_more,
                         "切房没有重置加载中状态")
        time.sleep(0.15)
        _pump(self.panel, self.host)


class TriggerTests(_QtPanelCase):
    """到顶触发的守护条件。"""

    def test_suspend_blocks_trigger(self):
        self.panel._has_more = True
        self.panel._loaded_count = 100
        self.panel._suspend_history_trigger = True
        self.panel._maybe_load_more()
        self.assertEqual(self.host.storage.calls, [],
                         "清空/首次渲染期间仍触发了翻页请求")

    def test_trigger_at_top_requests(self):
        self.panel._has_more = True
        self.panel._loaded_count = 100
        self.panel.sc_text.verticalScrollBar().setValue(0)
        self.panel._maybe_load_more()
        self.assertEqual([(r, s) for r, s, _l in self.host.storage.calls],
                         [(1, 100)])
        time.sleep(0.15)
        _pump(self.panel, self.host)

    def test_loading_blocks_duplicate_request(self):
        self.panel._has_more = True
        self.panel._loaded_count = 100
        self.panel._loading_more = True
        self.panel._maybe_load_more()
        self.assertEqual(self.host.storage.calls, [])


class HistoryPagingWiringTests(unittest.TestCase):
    """两版接线（AST）：代际、加载中复位与到顶重试都不能被改回去。"""

    def test_qt_clear_view_does_not_touch_gen(self):
        clear = _method(_tree("qt_sc_panel.py"), "ScPanel", "clear_view")
        self.assertNotIn("_history_gen", _attrs(clear),
                         "clear_view 又去推进代际了（会把刚发的请求作废）")
        self.assertIn("_reset_history_state", _attrs(clear))

    def test_qt_result_resets_loading_by_pending_gen(self):
        loaded = _method(_tree("qt_sc_panel.py"), "ScPanel", "on_history_loaded")
        self.assertIn("_pending_gen", _attrs(loaded))
        self.assertIn("_loading_more", _attrs(loaded))
        apply = _method(_tree("qt_sc_panel.py"), "ScPanel", "apply_room")
        self.assertIn("_reset_history_state", _attrs(apply),
                      "切房没有重置分页状态")

    def test_qt_suspend_and_wheel_retry(self):
        maybe = _method(_tree("qt_sc_panel.py"), "ScPanel", "_maybe_load_more")
        self.assertIn("_suspend_history_trigger", _attrs(maybe))
        panel = _class(_tree("qt_sc_panel.py"), "ScPanel")
        filters = [n for n in ast.walk(panel)
                   if isinstance(n, ast.FunctionDef) and n.name == "eventFilter"]
        self.assertTrue(filters, "缺少滚轮到顶重试的 eventFilter")
        self.assertIn("QEvent", _names(filters[0]))

    def test_tk_pending_gen_and_autoscroll_retry(self):
        tree = _tree("gui_app.py")
        load = _method(tree, "ScMonitorApp", "_load_history")
        self.assertIn("_pending_gen", _attrs(load))
        loaded = _method(tree, "ScMonitorApp", "_on_history_loaded")
        self.assertIn("_pending_gen", _attrs(loaded))
        tick = _method(tree, "ScMonitorApp", "_autoscroll_tick")
        self.assertIn("_maybe_load_more", _attrs(tick),
                      "中键自动滚动到顶后没有重试加载")

        room = _tree("tk_room_window.py")
        window_load = _method(room, "RoomChatWindow", "_load_history")
        self.assertIn("_pending_gen", _attrs(window_load))
        window_loaded = _method(room, "RoomChatWindow", "_on_history_loaded")
        self.assertIn("_pending_gen", _attrs(window_loaded))


if __name__ == "__main__":
    unittest.main()
