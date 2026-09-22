"""排序方案（ROADMAP 85）的离线测试：显示顺序合成与开播/关播时间记录。

两版共用同一份纯函数（``gui_app.order_room_ids`` / ``gui_app.update_live_activity``），
所以这里覆盖的就是界面最终看到的顺序——不启动任何 GUI。
"""

import unittest
from pathlib import Path

from blive_sc_get.gui_app import (
    SORT_MODE_HELP,
    SORT_MODE_TEXTS,
    order_room_ids,
    update_live_activity,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = [11, 22, 33, 44]


class UpdateLiveActivityTests(unittest.TestCase):
    """开播 / 关播时刻的记录（「按直播状态」排序的时间来源）。"""

    def test_enter_and_leave_records_time_once(self):
        live, offline = {}, {}
        self.assertTrue(update_live_activity(live, offline, 1, True, 10.0))
        self.assertEqual(live, {1: 10.0})
        # 状态没变（周期复核会反复报同一状态）：不刷新时间，否则开播时间被一路推后
        self.assertFalse(update_live_activity(live, offline, 1, True, 20.0))
        self.assertEqual(live, {1: 10.0})
        self.assertTrue(update_live_activity(live, offline, 1, False, 30.0))
        self.assertEqual(live, {})
        self.assertEqual(offline, {1: 30.0})

    def test_never_live_room_keeps_no_record(self):
        live, offline = {}, {}
        self.assertFalse(update_live_activity(live, offline, 5, False, 10.0))
        self.assertEqual((live, offline), ({}, {}))

    def test_relive_clears_offline_time(self):
        live, offline = {}, {1: 30.0}
        self.assertTrue(update_live_activity(live, offline, 1, True, 40.0))
        self.assertEqual(live, {1: 40.0})
        self.assertEqual(offline, {})

    def test_stamp_auto_increases_without_clock(self):
        """先后标记自动递增：不再依赖时钟。

        实测坑：Windows 上 ``time.monotonic()`` 精度只有约 15 毫秒，同一轮事件里几次
        开播/关播会拿到**相同**的时间戳，排序就分不出先后（冒烟时踩到）。
        """
        live, offline = {}, {}
        update_live_activity(live, offline, 1, True)
        update_live_activity(live, offline, 2, True)
        self.assertGreater(live[2], live[1], "后开播的标记应更大")
        update_live_activity(live, offline, 2, False)
        self.assertGreater(offline[2], live[1], "关播标记应大于此前的开播标记")
        update_live_activity(live, offline, 3, True)
        self.assertGreater(live[3], offline[2], "再次开播应拿到更大的标记")


class OrderRoomIdsTests(unittest.TestCase):
    """按方案合成显示顺序。"""

    def test_manual_keeps_base_order(self):
        self.assertEqual(order_room_ids(BASE, "manual"), BASE)
        self.assertEqual(order_room_ids([44, 11, 33], "manual"), [44, 11, 33])

    def test_room_mode_sorts_by_room_id(self):
        self.assertEqual(order_room_ids([44, 11, 33], "room"), [11, 33, 44])

    def test_anchor_mode_puts_unknown_last(self):
        # 按主播名排序（无主播名的排最后，其次按房间号）：这里用 ASCII 名字避免中文码点歧义
        names = {11: "beta", 22: "alpha"}
        self.assertEqual(order_room_ids([11, 22, 33, 44], "anchor", anchor_names=names),
                         [22, 11, 33, 44])

    def test_status_mode_live_first_by_start_time_desc(self):
        """正在直播的：最近开播在最上，没有开播记录的按基础顺序紧随其后。"""
        states = {11: "直播中", 33: "直播中"}
        self.assertEqual(
            order_room_ids(BASE, "status", live_states=states,
                           live_since={11: 100.0, 33: 200.0}),
            [33, 11, 22, 44])
        # 33 没有开播记录（例如程序启动时就已在直播）→ 排在有记录的房间之后
        self.assertEqual(
            order_room_ids(BASE, "status", live_states=states, live_since={11: 100.0}),
            [11, 33, 22, 44])

    def test_status_mode_offline_by_offline_time_desc(self):
        """非直播中：最近关播的靠前；没有关播记录的（一直没开播）排在这一组最后。"""
        states = {room_id: "未开播" for room_id in BASE}
        self.assertEqual(
            order_room_ids(BASE, "status", live_states=states,
                           offline_at={11: 100.0, 22: 300.0, 33: 200.0}),
            [22, 33, 11, 44])

    def test_status_mode_live_group_then_offline_group(self):
        states = {11: "未开播", 22: "直播中", 33: "未开播", 44: "未开播"}
        self.assertEqual(
            order_room_ids(BASE, "status", live_states=states,
                           live_since={22: 50.0},
                           offline_at={11: 10.0, 33: 90.0}),
            [22, 33, 11, 44])

    def test_status_mode_treats_carousel_as_offline(self):
        """轮播中不做特殊处理：与下播同样归入非直播中（不再单独成段）。"""
        self.assertEqual(
            order_room_ids([11, 22], "status",
                           live_states={22: "直播中", 11: "轮播中"},
                           live_since={22: 1.0}, offline_at={11: 2.0}),
            [22, 11])
        # 轮播中的房间绝不会因为「有关播时间」而跑到正在直播的房间前面
        self.assertEqual(
            order_room_ids([22, 11], "status",
                           live_states={11: "轮播中", 22: "直播中"},
                           live_since={22: 5.0}, offline_at={11: 999.0}),
            [22, 11])

    def test_status_mode_without_any_record_falls_back_to_base_order(self):
        states = {11: "未开播", 22: "未开播"}
        self.assertEqual(order_room_ids([22, 11], "status", live_states=states), [22, 11])

    def test_unknown_rooms_are_ignored(self):
        self.assertEqual(
            order_room_ids([11, 22], "status", live_states={99: "直播中"},
                           live_since={99: 1.0}, offline_at={98: 2.0}),
            [11, 22])

    def test_full_sequence_does_not_touch_base_order(self):
        """一轮开播/关播后：显示顺序随信号变化，但自定义顺序（base_order）不被改写。"""
        base = [11, 22, 33]
        live, offline = {}, {}
        update_live_activity(live, offline, 11, True, 10.0)
        update_live_activity(live, offline, 33, True, 20.0)
        states = {11: "直播中", 33: "直播中", 22: "未开播"}
        self.assertEqual(
            order_room_ids(base, "status", live_states=states,
                           live_since=live, offline_at=offline),
            [33, 11, 22])
        update_live_activity(live, offline, 33, False, 30.0)
        states[33] = "未开播"
        self.assertEqual(
            order_room_ids(base, "status", live_states=states,
                           live_since=live, offline_at=offline),
            [11, 33, 22])
        self.assertEqual(base, [11, 22, 33], "排序过程不得改写自定义顺序")


class SortModeTextsTests(unittest.TestCase):
    """排序方案的文案：下拉里「手动拖动」已改名「自定义排序」，说明收在「ⓘ」按钮里。"""

    def test_mode_names_and_hints(self):
        self.assertEqual(SORT_MODE_TEXTS["manual"], "自定义排序")
        self.assertEqual(SORT_MODE_TEXTS["status"], "按直播状态")
        self.assertEqual(list(SORT_MODE_TEXTS), ["manual", "room", "anchor", "status"])
        for mode in SORT_MODE_TEXTS:
            self.assertTrue(SORT_MODE_HELP.get(mode), f"{mode} 缺少说明文案")
        # 非自定义排序的说明要说清「不可拖动」并指路，避免用户以为拖动失灵
        for mode in ("room", "anchor", "status"):
            self.assertIn("不可拖动", SORT_MODE_HELP[mode])
            self.assertIn("自定义排序", SORT_MODE_HELP[mode])
        # 按直播状态的说明要把规则讲全（用户点「ⓘ」看到的就是这一段）
        for keyword in ("最近开播", "最近关播", "轮播中", "自动重排"):
            self.assertIn(keyword, SORT_MODE_HELP["status"])

    def test_tk_sort_bar_keeps_only_info_button(self):
        """Tk 排序栏只留最右的「ⓘ」：长提示（含残留的静态文案）不应再铺在栏里。"""
        source = (ROOT / "blive_sc_get" / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn('self.sort_info_btn.pack(side="right")', source,
                      "「ⓘ」应固定在排序栏最右（与 Qt 版一致）")
        self.assertNotIn("sort_hint_var", source, "动态长提示应已移除")
        self.assertNotIn("（按住行拖动可调整顺序", source, "静态长提示应已移除")


if __name__ == "__main__":
    unittest.main()
