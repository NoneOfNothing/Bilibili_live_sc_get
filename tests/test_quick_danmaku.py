"""快捷弹幕（ROADMAP 90）的离线测试：清洗规则、填入规则、按房间持久化。

刻意**不 import qt_app / PySide6**——本文件必须在没有 Qt 的环境下也能跑
（``tests/test_gui_support.py`` 因导入 qt_app 而依赖 PySide6）。
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

from blive_sc_get.gui_app import merge_quick_danmaku
from blive_sc_get.gui_config import (
    DEFAULT_UI_PREFS,
    NOTIFY_SOUNDS,
    QUICK_DANMAKU_MAX,
    SORT_MODES,
    RoomEntry,
    load_room_entries,
    load_ui_prefs,
    normalize_quick_danmaku,
    save_room_entries,
)

PKG = Path(__file__).resolve().parent.parent / "blive_sc_get"


class NormalizeQuickDanmakuTests(unittest.TestCase):
    """清洗：只留非空字符串、去首尾空白、保序去重、限制条数（**不限单条长度**，ROADMAP 91）。"""

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

    def test_keeps_long_text(self):
        """不截断单条长度（ROADMAP 91）：预设只是「待填入输入框的文本」。

        此前按 20 字截断，长句会被悄悄砍短；更糟的是打开一次管理对话框再关闭，就会把
        截断后的文本写回配置（用户反馈「快捷弹幕预设也不能超过 20 字」）。
        """
        self.assertEqual(normalize_quick_danmaku(["字" * 200]), ["字" * 200])

    def test_caps_item_count(self):
        items = [f"第{i}条" for i in range(QUICK_DANMAKU_MAX + 5)]
        self.assertEqual(len(normalize_quick_danmaku(items)), QUICK_DANMAKU_MAX)


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

    def test_long_preset_round_trip(self):
        """长预设存取往返**不被截断**（用户反馈的现场：读一次就被砍成 20 字）。

        这条同时锁住「管理对话框打开即保存」的路径——它会把工作副本写回配置，此前等于
        把截断后的文本固化下来。
        """
        long_text = "这是一条超过二十字的快捷弹幕预设，用来确认不会被悄悄截断"
        self.assertGreater(len(long_text), 20)
        save_room_entries(self.path, [RoomEntry(room_id=9, quick_danmaku=[long_text])])
        self.assertEqual(load_room_entries(self.path)[0].quick_danmaku, [long_text])


class DanmakuLenHintTests(unittest.TestCase):
    """字数计数只显示**已输入字数**，不再出现「x/20」那种上限写法（ROADMAP 91）。

    发送侧早已没有客户端截断（见 ``gui_app.danmaku_send_guard``：只拦空内容与发送过快），
    但 Qt 的计数标签一直写成 ``x/20``、Tk 独立窗口超长时也是，看起来像「超过 20 字就发不
    出去」。超长时的**标红提醒**保留（服务端可能拒绝）。
    """

    def test_no_slash_limit_writing(self):
        """三个计数位置（Qt 面板 / Tk 独立窗口 / Tk 主界面）都不再有「/上限」写法。"""
        for module in ("qt_dm_panel.py", "tk_room_window.py", "gui_app.py"):
            with self.subTest(module=module):
                source = (PKG / module).read_text(encoding="utf-8")
                self.assertNotIn("/{DANMAKU_MAX_LEN}", source,
                                 f"{module} 仍显示「x/20」上限写法")

    def test_overlong_warning_is_kept(self):
        """只去掉上限写法，超长标红提醒必须留着。"""
        for module in ("qt_dm_panel.py", "tk_room_window.py", "gui_app.py"):
            with self.subTest(module=module):
                source = (PKG / module).read_text(encoding="utf-8")
                self.assertIn("DANMAKU_MAX_LEN", source, f"{module} 丢失超长提醒")


class QuickDanmakuSendNowTests(unittest.TestCase):
    """「点选即发送」（ROADMAP 92）：勾选框、点选分支与统一入口（三处视图一致）。

    关键约束：直接发送必须复用**同一条**发送链路（写操作门控 / 登录态 / 冷却 / 回复目标
    与手动发送完全一致），不能因为多了一个入口而绕过任何限制。
    """

    def test_checkbox_in_all_three_views(self):
        """勾选框要在三处都出现：主界面、Tk 房间独立窗口、Qt 弹幕面板。"""
        for module in ("gui_app.py", "tk_room_window.py", "qt_dm_panel.py"):
            with self.subTest(module=module):
                source = (PKG / module).read_text(encoding="utf-8")
                self.assertIn("quick_dm_now_check", source,
                              f"{module} 缺少「点选即发送」勾选框")

    def test_pick_branches_on_preference(self):
        """点选处理必须按开关分支，且「直接发送」复用统一发送入口。"""
        for module in ("gui_app.py", "tk_room_window.py", "qt_dm_panel.py"):
            with self.subTest(module=module):
                source = (PKG / module).read_text(encoding="utf-8")
                self.assertIn("quick_dm_send_now", source, f"{module} 点选未按开关分支")
                self.assertIn("send_danmaku_text", source,
                              f"{module} 未复用统一发送入口（可能另开捷径）")

    def test_send_text_helper_is_used_by_both_entries(self):
        """发送入口与点选都走同一个方法：手动发送也要经由它，避免两条路径分叉。"""
        for module, wrapper, helper in (("gui_app.py", "_on_send_danmaku",
                                         "_send_danmaku_text"),
                                        ("tk_room_window.py", "_on_send_danmaku",
                                         "_send_danmaku_text"),
                                        ("qt_dm_panel.py", "on_send_danmaku",
                                         "send_danmaku_text")):
            with self.subTest(module=module):
                tree = ast.parse((PKG / module).read_text(encoding="utf-8"))
                node = next((n for n in ast.walk(tree)
                             if isinstance(n, ast.FunctionDef) and n.name == wrapper), None)
                self.assertIsNotNone(node, f"{module}.{wrapper} 不存在")
                called = {sub.func.attr for sub in ast.walk(node)
                          if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)}
                self.assertIn(helper, called,
                              f"{module}.{wrapper} 未委托给 {helper}（两条路径会分叉）")

    def test_host_entry_saves_and_broadcasts(self):
        """两版宿主都有统一入口：落盘偏好 + 广播同步所有视图。"""
        for module in ("gui_app.py", "qt_app.py"):
            with self.subTest(module=module):
                tree = ast.parse((PKG / module).read_text(encoding="utf-8"))
                node = next((n for n in ast.walk(tree)
                             if isinstance(n, ast.FunctionDef)
                             and n.name == "set_quick_dm_send_now"), None)
                self.assertIsNotNone(node, f"{module} 缺少统一入口 set_quick_dm_send_now")
                called = {sub.func.attr for sub in ast.walk(node)
                          if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)}
                self.assertIn("_save_config", called, f"{module} 切换后未落盘")
        # Qt 还要广播到每个弹幕面板（含独立窗口）；Tk 广播到各独立窗口
        qt = (PKG / "qt_app.py").read_text(encoding="utf-8")
        self.assertIn("panel.set_quick_dm_send_now", qt, "Qt 未广播到所有弹幕面板")
        tk = (PKG / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn("window.set_quick_dm_send_now", tk, "Tk 未同步各独立窗口")


class QuickSendNowPersistenceTests(unittest.TestCase):
    """「点选即发送」勾选框的落盘记忆（ROADMAP 99）。

    这个开关的**写入侧本来就通**（切换即 ``_save_config``，ui 段是整段落盘，磁盘上确实
    躺着 ``quick_dm_send_now: true``），坏在**读取侧**：``load_ui_prefs`` 是「逐键列出要读回
    的偏好」这种写法，唯独漏了这一个键，于是重启后永远是默认的关闭 —— 用户看到的就是
    「勾选框记不住」。所以这里既锁住这一个键，也在末尾加一道**通用守卫**：``DEFAULT_UI_PREFS``
    里的每个键都必须能从配置读回来，以后新增偏好键忘写读取（本 BUG 的成因）会直接失败。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "gui_rooms.json"

    def _write(self, ui: dict) -> None:
        self.path.write_text(json.dumps({"rooms": [], "ui": ui}, ensure_ascii=False),
                             encoding="utf-8")

    def test_reads_saved_flag(self):
        """磁盘上是 true / false / 缺键 → 分别读回 True / False / False（默认关闭）。"""
        self.assertEqual(DEFAULT_UI_PREFS["quick_dm_send_now"], False, "默认应为关闭")
        self._write({"quick_dm_send_now": True})
        self.assertTrue(load_ui_prefs(self.path)["quick_dm_send_now"])
        self._write({"quick_dm_send_now": False})
        self.assertFalse(load_ui_prefs(self.path)["quick_dm_send_now"])
        self._write({})
        self.assertFalse(load_ui_prefs(self.path)["quick_dm_send_now"])

    def test_toggle_survives_restart(self):
        """真实落盘回路：勾选（写 ui 段）→ 重启（重新读配置）→ 仍是勾选；取消同理。"""
        prefs = load_ui_prefs(self.path)  # 首次启动（配置文件还不存在）→ 默认关闭
        self.assertFalse(prefs["quick_dm_send_now"])
        prefs["quick_dm_send_now"] = True  # 宿主统一入口改的就是这个键
        save_room_entries(self.path, [], ui=prefs)
        self.assertTrue(load_ui_prefs(self.path)["quick_dm_send_now"], "重启后丢了勾选")

        again = load_ui_prefs(self.path)
        again["quick_dm_send_now"] = False
        save_room_entries(self.path, [], ui=again)
        self.assertFalse(load_ui_prefs(self.path)["quick_dm_send_now"], "重启后丢了取消")

    def test_every_default_pref_key_is_read_back(self):
        """通用守卫：每个偏好键写入「非默认值」后都必须读得回来（防再出现「写了没读」）。"""
        samples = {
            "sort_mode": next(m for m in SORT_MODES
                              if m != DEFAULT_UI_PREFS["sort_mode"]),
            "notify_overlay": not DEFAULT_UI_PREFS["notify_overlay"],
            "notify_persist": not DEFAULT_UI_PREFS["notify_persist"],
            "notify_sound": next(s for s in NOTIFY_SOUNDS
                                 if s != DEFAULT_UI_PREFS["notify_sound"]),
            "dm_visible": not DEFAULT_UI_PREFS["dm_visible"],
            "dm_emoticon_image": not DEFAULT_UI_PREFS["dm_emoticon_image"],
            "quick_dm_send_now": not DEFAULT_UI_PREFS["quick_dm_send_now"],
            "window_size": [900, 800],
            "selected_room": 12345,
            "room_windows": {"12345": {"x": 10, "y": 20, "width": 640, "height": 720}},
            "live_started_at": {"12345": 1790491373.0},
            "live_offline_at": {"12345": 1790482140.0},
        }
        self.assertEqual(set(samples), set(DEFAULT_UI_PREFS),
                         "DEFAULT_UI_PREFS 增减了键：请同步样本表，并确认它被 load_ui_prefs 读回")
        self._write(samples)
        prefs = load_ui_prefs(self.path)
        for key, sample in samples.items():
            with self.subTest(key=key):
                self.assertNotEqual(prefs[key], DEFAULT_UI_PREFS[key],
                                    f"{key} 写进配置却读不回来（写了没读）")

    def test_checkbox_starts_from_global_pref(self):
        """三处勾选框的**初值**都要来自全局偏好，而不是自己写死一个 False。

        只锁「读得回来」还不够：视图若改成硬编码初值，记忆同样失效（本 BUG 的另一半）。
        """
        for module in ("gui_app.py", "tk_room_window.py"):
            with self.subTest(module=module):
                tree = ast.parse((PKG / module).read_text(encoding="utf-8"))
                wired = False
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign) or not any(
                            isinstance(t, ast.Attribute) and t.attr == "quick_dm_now_var"
                            for t in node.targets):
                        continue
                    texts = {c.value for c in ast.walk(node)
                             if isinstance(c, ast.Constant) and isinstance(c.value, str)}
                    wired = wired or "quick_dm_send_now" in texts
                self.assertTrue(wired, f"{module} 的「点选即发送」勾选框初值未读全局偏好")

        # Qt 弹幕面板：勾选框初值走 self._quick_send_now()，那个函数必须读全局偏好
        tree = ast.parse((PKG / "qt_dm_panel.py").read_text(encoding="utf-8"))
        checker = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                        and n.name == "_quick_send_now"), None)
        self.assertIsNotNone(checker, "qt_dm_panel 缺少 _quick_send_now")
        texts = {c.value for c in ast.walk(checker)
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        self.assertIn("quick_dm_send_now", texts, "_quick_send_now 未读全局偏好")
        set_checked = [n for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "setChecked" and n.args
                       and any(isinstance(a, ast.Attribute) and a.attr == "_quick_send_now"
                               for a in ast.walk(n.args[0]))]
        self.assertTrue(set_checked, "Qt 勾选框初值未调用 _quick_send_now（可能写死了）")


if __name__ == "__main__":
    unittest.main()