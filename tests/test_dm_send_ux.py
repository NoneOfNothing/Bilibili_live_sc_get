"""弹幕发送体验四项改造的离线测试（ROADMAP 106–109），外加 107 的「上限已删」守卫。

刻意**不 import qt_app / PySide6**：本文件必须在没有 Qt 的环境下也能跑（与
``tests/test_quick_danmaku.py`` 同样的约定），Qt 侧的接线一律用 AST 静态断言。

覆盖四件事：

- 106 发送颜色 / 模式：按房间记忆的存取（``RoomEntry.dm_color`` / ``dm_mode``）、
  ``resolve_dm_choice`` 的定位与「记忆项不可用 → 回落默认」判定、三处视图的接线；
- 107 快捷弹幕上限：``normalize_quick_danmaku`` 不再截断、常量与两版对话框的拦截已删除；
- 108 发送历史：``dm_history_push`` / ``dm_history_step`` 的语义（含边界）与三处视图接线；
- 109 点击弹幕填入发送框：勾选框接线、复制 / 填入分流与「发送框不可用则回退复制」。
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

from blive_sc_get.gui_app import (
    DM_SEND_HISTORY_MAX,
    dm_history_push,
    dm_history_step,
    resolve_dm_choice,
)
from blive_sc_get.gui_config import (
    DEFAULT_UI_PREFS,
    RoomEntry,
    load_room_entries,
    load_ui_prefs,
    normalize_quick_danmaku,
    save_room_entries,
)

PKG = Path(__file__).resolve().parent.parent / "blive_sc_get"


def _src(module: str) -> str:
    return (PKG / module).read_text(encoding="utf-8")


# (模块, 方法名) -> 所属类。``__init__`` / ``_build_ui`` / ``_copy_dm_content`` 这类名字在
# 同一个文件里会撞车（辅助类也有同名方法），必须按类限定，否则 AST 会取到别的类的节点。
_OWNER = {
    ("gui_app.py", "_on_room_selected"): "ScMonitorApp",
    ("gui_app.py", "_set_dm_options"): "ScMonitorApp",
    ("gui_app.py", "_on_dm_choice_changed"): "ScMonitorApp",
    ("gui_app.py", "_build_dm_send_area"): "ScMonitorApp",
    ("gui_app.py", "_on_dm_send_result"): "ScMonitorApp",
    ("gui_app.py", "_fill_dm_send_box"): "ScMonitorApp",
    ("gui_app.py", "set_copy_dm_fill"): "ScMonitorApp",
    ("gui_app.py", "_copy_dm_content"): "ScMonitorApp",
    ("gui_app.py", "_open_quick_danmaku_manager"): "ScMonitorApp",
    ("tk_room_window.py", "_build_send_area"): "RoomChatWindow",
    ("tk_room_window.py", "_on_dm_choice_changed"): "RoomChatWindow",
    ("tk_room_window.py", "_copy_dm_content"): "RoomChatWindow",
    ("tk_room_window.py", "_fill_dm_send_box"): "RoomChatWindow",
    ("qt_dm_panel.py", "__init__"): "DmPanel",
    ("qt_dm_panel.py", "_build_ui"): "DmPanel",
    ("qt_dm_panel.py", "eventFilter"): "DmPanel",
    ("qt_dm_panel.py", "on_room_selected"): "DmPanel",
    ("qt_dm_panel.py", "_set_dm_options"): "DmPanel",
    ("qt_dm_panel.py", "_on_dm_choice_changed"): "DmPanel",
    ("qt_dm_panel.py", "_copy_dm_content"): "DmPanel",
    ("qt_dm_panel.py", "_fill_dm_send_box"): "DmPanel",
    ("qt_dm_panel.py", "_copy_dm_fill"): "DmPanel",
    ("qt_app.py", "_push_dm_history"): "QtScMonitorApp",
    ("qt_app.py", "set_copy_dm_fill"): "QtScMonitorApp",
    ("qt_app.py", "open_quick_danmaku_manager"): "QtScMonitorApp",
}


def _method(module: str, cls: str, name: str) -> ast.FunctionDef:
    """取出某模块里**指定类**的方法节点。"""
    klass = next((n for n in ast.walk(ast.parse(_src(module)))
                  if isinstance(n, ast.ClassDef) and n.name == cls), None)
    if klass is None:
        raise AssertionError(f"{module} 缺少类 {cls}")
    node = next((n for n in klass.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise AssertionError(f"{module} 的 {cls} 缺少 {name}")
    return node


def _func(module: str, name: str) -> ast.FunctionDef:
    """取出某模块里的函数节点；表里登记过的按所属类取，其余按顶层函数取。"""
    cls = _OWNER.get((module, name))
    if cls:
        return _method(module, cls, name)
    node = next((n for n in ast.parse(_src(module)).body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise AssertionError(f"{module} 缺少顶层函数 {name}")
    return node


def _attr_names(node: ast.AST) -> set:
    """节点里出现过的属性名（读 / 写都算）。"""
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _called(node: ast.AST) -> set:
    """节点里调用过的方法名（属性调用）。"""
    return {n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _called_names(node: ast.AST) -> set:
    """节点里调用过的**裸函数名**（``merge_quick_danmaku(...)`` 这类，非属性调用）。"""
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _texts(node: ast.AST) -> set:
    """节点里出现过的字符串字面量。"""
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


class ResolveDmChoiceTests(unittest.TestCase):
    """按记忆名称在可用项里定位（ROADMAP 106 的核心判定，纯函数）。"""

    NAMES = ["白色", "红色", "蓝色"]

    def test_keeps_remembered_when_available(self):
        self.assertEqual(resolve_dm_choice(self.NAMES, "红色"), ("红色", False))

    def test_falls_back_when_memory_gone(self):
        """记忆项已不可用 → 首项 + 明确告知「回落了」（调用方据此改写记忆）。"""
        self.assertEqual(resolve_dm_choice(self.NAMES, "土豪金"), ("白色", True))

    def test_defaults_without_memory(self):
        """没记过（空串）不算失效：用首项，但 ``fallback`` 为假。"""
        self.assertEqual(resolve_dm_choice(self.NAMES, ""), ("白色", False))

    def test_empty_options(self):
        """可用项为空（理论上不会，select_dm_options 保证非空）→ 返回空串并跳过。"""
        self.assertEqual(resolve_dm_choice([], "红色"), ("", False))


class RoomSendMemoryTests(unittest.TestCase):
    """发送颜色 / 模式的按房间记忆（ROADMAP 106）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "gui_rooms.json"

    def test_roundtrip(self):
        entries = [RoomEntry(111, dm_color="红色", dm_mode="顶部"),
                   RoomEntry(222)]  # 没记过 = 空串
        save_room_entries(self.path, entries)
        loaded = load_room_entries(self.path)
        self.assertEqual([(e.dm_color, e.dm_mode) for e in loaded],
                         [("红色", "顶部"), ("", "")])

    def test_legacy_config_without_keys(self):
        """旧配置没有这两个键 → 空串（＝未记忆），不会让房间列表读不出来。"""
        self.path.write_text(json.dumps({"rooms": [{"room_id": 333}]}),
                             encoding="utf-8")
        entry = load_room_entries(self.path)[0]
        self.assertEqual((entry.dm_color, entry.dm_mode), ("", ""))

    def test_bad_values_do_not_break(self):
        """键存在但值不是字符串（None / 数字）→ 空串。"""
        self.path.write_text(json.dumps(
            {"rooms": [{"room_id": 444, "dm_color": None, "dm_mode": 5}]}),
            encoding="utf-8")
        entry = load_room_entries(self.path)[0]
        self.assertEqual((entry.dm_color, entry.dm_mode), ("", ""))


class CopyDmFillPrefTests(unittest.TestCase):
    """「点击弹幕填入发送框」是全局偏好（ROADMAP 109），默认关闭且读得回来。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "gui_rooms.json"

    def _write(self, ui: dict) -> None:
        self.path.write_text(json.dumps({"rooms": [], "ui": ui}, ensure_ascii=False),
                             encoding="utf-8")

    def test_default_off_and_read_back(self):
        self.assertFalse(DEFAULT_UI_PREFS["copy_dm_fill"], "默认应为关闭（保持复制）")
        self._write({})
        self.assertFalse(load_ui_prefs(self.path)["copy_dm_fill"])
        self._write({"copy_dm_fill": True})
        self.assertTrue(load_ui_prefs(self.path)["copy_dm_fill"])

    def test_toggle_survives_restart(self):
        prefs = load_ui_prefs(self.path)
        prefs["copy_dm_fill"] = True          # 宿主统一入口改的就是这个键
        save_room_entries(self.path, [], ui=prefs)
        self.assertTrue(load_ui_prefs(self.path)["copy_dm_fill"], "重启后丢了勾选")


class DmHistoryPushTests(unittest.TestCase):
    """发送历史的入栈规则（ROADMAP 108，纯函数）。"""

    def test_newest_first(self):
        self.assertEqual(dm_history_push(["a"], "b"), ["b", "a"])

    def test_dedupe_moves_to_front(self):
        """重复发送同一条：提到最前，而不是留两份。"""
        self.assertEqual(dm_history_push(["a", "b", "c"], "c"), ["c", "a", "b"])

    def test_blank_ignored(self):
        for value in ("", "   ", None):
            self.assertEqual(dm_history_push(["a"], value), ["a"], repr(value))

    def test_limit_drops_oldest(self):
        items = [f"第{i}条" for i in range(5)]
        got = dm_history_push(items, "新", limit=3)
        self.assertEqual(got, ["新", "第0条", "第1条"])
        self.assertEqual(len(dm_history_push(items, "新", limit=0)), 5, "非正上限不改动")

    def test_default_limit_matches_constant(self):
        items = [f"第{i}条" for i in range(DM_SEND_HISTORY_MAX + 10)]
        self.assertEqual(len(dm_history_push(items, "新")), DM_SEND_HISTORY_MAX)


class DmHistoryStepTests(unittest.TestCase):
    """上下键取用历史的规则（ROADMAP 108，含全部边界）。"""

    HISTORY = ["最新", "中间", "最早"]

    def test_empty_history_keeps_draft(self):
        """没有历史时上下键都停在草稿（与未实现历史前行为一致）。"""
        self.assertEqual(dm_history_step([], -1, "草稿", backwards=True), ("草稿", -1))
        self.assertEqual(dm_history_step([], 0, "草稿", backwards=False), ("草稿", -1))

    def test_up_from_draft_goes_to_newest(self):
        self.assertEqual(dm_history_step(self.HISTORY, -1, "草稿", backwards=True),
                         ("最新", 0))

    def test_up_walks_to_earliest_then_stops(self):
        text, index = dm_history_step(self.HISTORY, 0, "草稿", backwards=True)
        self.assertEqual((text, index), ("中间", 1))
        text, index = dm_history_step(self.HISTORY, index, "草稿", backwards=True)
        self.assertEqual((text, index), ("最早", 2))
        # 已到最早：再按 ↑ 停在最早那条（不越界、不循环）
        self.assertEqual(dm_history_step(self.HISTORY, index, "草稿", backwards=True),
                         ("最早", 2))

    def test_down_walks_back_to_draft(self):
        text, index = dm_history_step(self.HISTORY, 2, "草稿", backwards=False)
        self.assertEqual((text, index), ("中间", 1))
        text, index = dm_history_step(self.HISTORY, index, "草稿", backwards=False)
        self.assertEqual((text, index), ("最新", 0))
        # 越过最新一条 → 回到按键前的草稿
        self.assertEqual(dm_history_step(self.HISTORY, index, "草稿", backwards=False),
                         ("草稿", -1))

    def test_out_of_range_index_treated_as_draft(self):
        """历史被截断后残留的旧游标：按「不在历史里」处理，不会 IndexError。"""
        self.assertEqual(dm_history_step(self.HISTORY, 99, "草稿", backwards=True),
                         ("最新", 0))
        self.assertEqual(dm_history_step(self.HISTORY, 99, "草稿", backwards=False),
                         ("草稿", -1))


class QuickDanmakuUncappedTests(unittest.TestCase):
    """ROADMAP 107：条数上限彻底取消（常量、清洗截断与两版对话框拦截都要没了）。"""

    def test_no_cap_constant_anywhere(self):
        """常量本身没了：全仓库不再**引用**该标识符（注释里提历史不算）。

        用 AST 判而不是文本匹配：``normalize_quick_danmaku`` 的 docstring 里刻意留了
        「此处曾用 ``QUICK_DANMAKU_MAX``（12 条）截断」这句历史说明。
        """
        for module in ("gui_config.py", "gui_app.py", "qt_app.py"):
            tree = ast.parse(_src(module))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            self.assertNotIn("QUICK_DANMAKU_MAX", names | attrs,
                             f"{module} 里还引用着 QUICK_DANMAKU_MAX")

    def test_normalize_keeps_every_item(self):
        items = [f"第{i}条" for i in range(120)]
        self.assertEqual(normalize_quick_danmaku(items), items)

    def test_manage_dialogs_do_not_block_on_count(self):
        """两版管理对话框都不再按条数拦截（只拦重复）。"""
        for module, name in (("gui_app.py", "_open_quick_danmaku_manager"),
                             ("qt_app.py", "open_quick_danmaku_manager")):
            node = _func(module, name)
            for call in ast.walk(node):
                if not isinstance(call, ast.If):
                    continue
                names = {t.id for t in ast.walk(call) if isinstance(t, ast.Name)}
                self.assertNotIn("QUICK_DANMAKU_MAX", names,
                                 f"{module} 的 {name} 里仍有条数拦截")


class SendUxWiringTests(unittest.TestCase):
    """三处视图的接线（AST 静态检查）：Tk 主界面 / Tk 房间独立窗口 / Qt 弹幕面板。"""

    # -------- 106 记忆 --------

    def test_tk_main_applies_and_remembers(self):
        self.assertIn("_apply_dm_memory",
                      _called(_func("gui_app.py", "_on_room_selected")),
                      "Tk 主界面切房时未套用记忆")
        self.assertIn("_apply_memory_to_combo",
                      _called(_func("gui_app.py", "_set_dm_options")),
                      "Tk 主界面拿到可用项后未按记忆收口")
        handler = _func("gui_app.py", "_on_dm_choice_changed")
        self.assertIn("_save_config", _called(handler), "Tk 改动下拉后未落盘")
        self.assertIn("dm_color", _attr_names(handler), "Tk 未写颜色记忆")
        # 两个下拉都要绑用户改动事件（程序 set() 不触发 <<ComboboxSelected>>）；
        # 快捷弹幕下拉也绑同一个事件，故这里只数挂到 _on_dm_choice_changed 的那两个
        setup = _func("gui_app.py", "_build_dm_send_area")
        binds = [n for n in ast.walk(setup)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "bind" and n.args
                 and isinstance(n.args[0], ast.Constant)
                 and n.args[0].value == "<<ComboboxSelected>>"
                 and "_on_dm_choice_changed" in ast.unparse(n)]
        self.assertEqual(len(binds), 2, "Tk 主界面的颜色 / 模式下拉未都绑改动事件")

    def test_tk_room_window_shares_room_memory(self):
        self.assertIn("_apply_dm_memory",
                      _called(_func("tk_room_window.py", "_build_send_area")),
                      "Tk 独立窗口建好后未套用记忆")
        handler = _func("tk_room_window.py", "_on_dm_choice_changed")
        self.assertIn("entries", _attr_names(handler), "独立窗口未写房间条目")
        self.assertIn("_save_config", _called(handler), "独立窗口改动下拉后未落盘")

    def test_qt_panel_applies_and_remembers(self):
        self.assertIn("_apply_dm_memory",
                      _called(_func("qt_dm_panel.py", "on_room_selected")),
                      "Qt 面板切房时未套用记忆")
        self.assertIn("_apply_memory_to_combo",
                      _called(_func("qt_dm_panel.py", "_set_dm_options")),
                      "Qt 面板拿到可用项后未按记忆收口")
        handler = _func("qt_dm_panel.py", "_on_dm_choice_changed")
        self.assertIn("_save_config", _called(handler), "Qt 改动下拉后未落盘")
        self.assertIn("entries", _attr_names(handler), "Qt 未写房间条目")

    # -------- 108 历史 --------

    def test_history_bound_to_entry_in_all_views(self):
        """三处输入框都要接上下键：Tk 两处绑 <Up>/<Down>，Qt 走 eventFilter。"""
        for module in ("gui_app.py", "tk_room_window.py"):
            setup = (_func(module, "_build_dm_send_area")
                     if module == "gui_app.py" else
                     _func(module, "_build_send_area"))
            keys = {n.args[0].value for n in ast.walk(setup)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "bind" and n.args
                    and isinstance(n.args[0], ast.Constant)}
            self.assertIn("<Up>", keys, f"{module} 未绑上键")
            self.assertIn("<Down>", keys, f"{module} 未绑下键")
        filt = _func("qt_dm_panel.py", "eventFilter")
        self.assertIn("Key_Up", ast.unparse(filt), "Qt 未处理上键")
        self.assertIn("Key_Down", ast.unparse(filt), "Qt 未处理下键")

    def test_qt_event_filter_survives_early_events(self):
        """`eventFilter` 会装到多个控件上，且构建期就可能被调到：不能直接访问还没建出来的输入框。

        探针实测踩到过——`dm_text` 先于 `dm_send_entry` 创建，构建期来一个事件就会
        在 `eventFilter` 里抛 ``AttributeError: 'DmPanel' object has no attribute
        'dm_send_entry'``，面板直接建不出来。
        """
        filt = _func("qt_dm_panel.py", "eventFilter")
        self.assertIn("_key_entry", _attr_names(filt),
                      "eventFilter 未用 _key_entry 这一「可能尚未创建」的安全字段")
        init = _func("qt_dm_panel.py", "__init__")
        assigned = [n for n in ast.walk(init)
                    if isinstance(n, ast.AnnAssign)
                    and isinstance(n.target, ast.Attribute)
                    and n.target.attr == "_key_entry"]
        self.assertTrue(assigned, "_key_entry 未在 __init__ 里初始化（None）")

    def test_history_recorded_on_successful_text_send(self):
        """历史只在**发送成功**且是文字弹幕时入栈：两版记录点都要判 emoticon。"""
        tk_result = _func("gui_app.py", "_on_dm_send_result")
        self.assertIn("_push_dm_history", _called(tk_result), "Tk 未记录发送历史")
        self.assertIn("emoticon", _texts(tk_result) | _attr_names(tk_result),
                      "Tk 未区分表情包发送")
        qt_host = _func("qt_app.py", "_push_dm_history")
        self.assertIn("emoticon", _texts(qt_host) | _attr_names(qt_host),
                      "Qt 未区分表情包发送")
        self.assertIn("ok", _texts(qt_host) | _attr_names(qt_host), "Qt 未判发送成功")

    def test_history_is_memory_only(self):
        """历史只存在内存：不写进 ui 偏好（否则就成了「落盘记忆」，与确认的方案不符）。"""
        self.assertNotIn("dm_send_history", DEFAULT_UI_PREFS)

    # -------- 109 点击填入 --------

    def test_copy_path_branches_in_all_views(self):
        for module, name in (("gui_app.py", "_copy_dm_content"),
                             ("tk_room_window.py", "_copy_dm_content"),
                             ("qt_dm_panel.py", "_copy_dm_content")):
            node = _func(module, name)
            unparsed = ast.unparse(node)
            self.assertIn("copy_dm_fill", unparsed, f"{module} 未按偏好分流")
            self.assertIn("_fill_dm_send_box", unparsed, f"{module} 未实现「填入发送框」")
            self.assertIn("_dm_send_block_reason", unparsed,
                          f"{module} 未处理「发送框不可用」的回退")

    def test_fill_is_overwrite_not_merge(self):
        """填入是**覆盖式**（用户明确要求）：不能走 merge_quick_danmaku 那套追加。"""
        tk_fill = _func("gui_app.py", "_fill_dm_send_box")
        tk_calls = _called(tk_fill) | _called_names(tk_fill)
        self.assertNotIn("merge_quick_danmaku", tk_calls, "Tk 填入误用了追加语义")
        self.assertIn("set", _called(tk_fill), "Tk 未覆盖式写入输入框")
        qt_fill = _func("qt_dm_panel.py", "_fill_dm_send_box")
        qt_calls = _called(qt_fill) | _called_names(qt_fill)
        self.assertNotIn("merge_quick_danmaku", qt_calls, "Qt 填入误用了追加语义")
        self.assertIn("setText", _called(qt_fill), "Qt 未覆盖式写入输入框")

    def test_toggle_entry_is_shared_in_all_views(self):
        """勾选框是全局偏好：宿主统一入口落盘 + 广播，视图只回写。"""
        # 广播必须是「遍历各视图 + 调各自的 set_copy_dm_fill」这个循环，
        # 只看属性名会被日志里的 len(self._dm_panels) 蒙混过去（故障植入验证时踩到过）
        for module, attr in (("gui_app.py", "_room_windows"),
                             ("qt_app.py", "_dm_panels")):
            host = _func(module, "set_copy_dm_fill")
            self.assertIn("_save_config", _called(host), f"{module} 宿主入口未落盘")
            self.assertIn("copy_dm_fill", _texts(host), f"{module} 宿主入口未写偏好键")
            loops = [n for n in ast.walk(host)
                     if isinstance(n, ast.For)
                     and attr in ast.unparse(n.iter)
                     and "set_copy_dm_fill" in ast.unparse(n)]
            self.assertTrue(loops, f"{module} 宿主未把开关广播给各视图")

    def test_checkbox_initial_value_reads_global_pref(self):
        """三处勾选框的**初值**都要来自全局偏好（写死 False 的话记忆同样失效）。"""
        for module, build in (("gui_app.py", "_build_dm_send_area"),
                              ("tk_room_window.py", "_build_send_area")):
            node = _func(module, build)
            wired = any(isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Attribute)
                                and t.attr == "copy_dm_fill_var" for t in n.targets)
                        and "copy_dm_fill" in _texts(n)
                        for n in ast.walk(node))
            self.assertTrue(wired, f"{module} 的勾选框初值未读全局偏好")
        qt_checker = _func("qt_dm_panel.py", "_copy_dm_fill")
        self.assertIn("copy_dm_fill", _texts(qt_checker),
                      "Qt 的 _copy_dm_fill 未读全局偏好")
        checked = [n for n in ast.walk(_func("qt_dm_panel.py", "_build_ui"))
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "setChecked"]
        self.assertTrue(any("_copy_dm_fill" in ast.unparse(n) for n in checked),
                        "Qt 勾选框初值未调用 _copy_dm_fill（可能写死了）")


if __name__ == "__main__":
    unittest.main()
