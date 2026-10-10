"""排序方案（ROADMAP 85 / 87 / 97）的离线测试：顺序合成、开播/关播时间记录与**持久化**。

两版共用同一份纯函数（``gui_app.order_room_ids`` / ``update_live_started_at`` /
``update_offline_at`` / ``merge_offline_marks`` / ``prune_live_marks``），所以这里覆盖的就是
界面最终看到的顺序——不启动任何 GUI。ROADMAP 97 那部分还顺带检查「关注列表的真实下播时刻」
在两版宿主里的接线（静态 AST）。
"""

import ast
import unittest
from pathlib import Path

from blive_sc_get.api import FOLLOWING_URL
from blive_sc_get.gui_app import (
    LIVE_MARK_MAX_AGE_S,
    NEW_ROOM_MARK_S,
    SORT_MODE_HELP,
    SORT_MODE_TEXTS,
    backfill_offline_marks,
    merge_offline_marks,
    order_room_ids,
    prune_live_marks,
    update_live_started_at,
    update_offline_at,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = [11, 22, 33, 44]


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


def _names(node) -> set:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _attr_names(node) -> set:
    return {sub.attr for sub in ast.walk(node) if isinstance(sub, ast.Attribute)}


def _called_attrs(node) -> set:
    return {sub.func.attr for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)}


def _consts(node) -> set:
    return {sub.value for sub in ast.walk(node) if isinstance(sub, ast.Constant)}


class UpdateOfflineAtTests(unittest.TestCase):
    """关播时刻的记录（「已下播的按关播先后倒序」的时间来源，真实墙钟）。

    开播时刻不在这里：它由 ``update_live_started_at`` 维护（**真实开播时间**，接口值优先、
    可校正本地观测、下播清空），排序时直接复用同一个字典。两者一起被持久化，因此重启后
    顺序不变（见 ``LiveMarkPersistenceTests``）。
    """

    def test_leave_records_time_once(self):
        offline = {}
        self.assertTrue(update_offline_at(offline, 1, False, 30.0, was_live=True))
        self.assertEqual(offline, {1: 30.0})
        # 状态没变（周期复核会反复报同一状态，此时上一轮已是未开播）：不刷新，
        # 否则关播时间被一路推后
        self.assertFalse(update_offline_at(offline, 1, False, 40.0))
        self.assertEqual(offline, {1: 30.0})

    def test_never_live_room_keeps_no_record(self):
        """没观察到「直播中 → 未开播」跳变（从未开播 / 启动时就已下播）：不留记录。

        启动时就已下播的房间，其先后顺序由**上次持久化的记录**给出（见
        ``LiveMarkPersistenceTests``），不该按本次启动时刻重新计时。
        """
        offline = {}
        self.assertFalse(update_offline_at(offline, 5, False, 10.0))
        self.assertEqual(offline, {})

    def test_live_clears_offline_time(self):
        """重新开播：关播记录清掉（它只对已下播的房间有意义）。"""
        offline = {1: 30.0}
        self.assertTrue(update_offline_at(offline, 1, True, 40.0))
        self.assertEqual(offline, {})
        self.assertFalse(update_offline_at(offline, 1, True, 50.0))

    def test_same_round_still_orderable(self):
        """同一轮里几个房间同时下播：时钟可能给出相同值，此时仍要能分出先后。

        Windows 上时钟粒度约 1~15 毫秒，``time.time()`` 在同一轮事件里可能撞值，排序就
        分不出先后；这里改用「现有最大值 + 1 毫秒」代替，偏移只有毫秒级、不偏离真实时间。
        """
        offline = {}
        for room_id in (1, 2, 3):
            update_offline_at(offline, room_id, False, 100.0, was_live=True)
        self.assertGreater(offline[2], offline[1], "后下播的时间应更大")
        self.assertGreater(offline[3], offline[2])
        self.assertLess(offline[3] - 100.0, 0.01, "偏移应只有毫秒级")


class LiveMarkPersistenceTests(unittest.TestCase):
    """排序依据必须**持久化**：重启后仍按上次的开播 / 关播先后排序（用户反馈的核心问题）。"""

    NOW = 100000.0

    def test_restart_keeps_order(self):
        """模拟「运行 → 落盘 → 重启」：重启后的显示顺序与重启前一致。"""
        base = [11, 22, 33, 44]
        live, offline = {}, {}
        # 运行期：33 先开播、11 后开播（最近开播的在最上）；22 刚下播
        update_live_started_at(live, 33, True, self.NOW - 7200, now=self.NOW)
        update_live_started_at(live, 11, True, self.NOW - 600, now=self.NOW)
        update_offline_at(offline, 22, False, self.NOW - 120, was_live=True)
        states = {33: "直播中", 11: "直播中", 22: "未开播"}
        before = order_room_ids(base, "status", live_states=states,
                                live_since=live, offline_at=offline)
        self.assertEqual(before, [11, 33, 22, 44])

        # 落盘（gui_rooms.json 的 ui 段用字符串键）→ 重启读取
        saved_live = {str(k): v for k, v in live.items()}
        saved_offline = {str(k): v for k, v in offline.items()}
        live2 = prune_live_marks(saved_live, now=self.NOW)
        offline2 = prune_live_marks(saved_offline, now=self.NOW)
        self.assertEqual(live2, live)
        self.assertEqual(offline2, offline)

        after = order_room_ids(base, "status", live_states=states,
                               live_since=live2, offline_at=offline2)
        self.assertEqual(after, before, "重启后顺序被打乱")

    def test_startup_unknown_state_then_live_reorders(self):
        """启动时状态未知 → 先按上次关播时间排；状态到达后在播房间要排到最前。

        这正是「状态跳变（哪怕时间基准没变）也要重排」的价值：启动那一刻没人知道谁在播，
        只能先按历史记录排，等状态陆续到达再纠正。
        """
        base = [11, 22, 33]
        live = {11: 1000.0}                 # 持久化恢复：11 上次的开播时刻
        offline = {22: 900.0, 33: 800.0}    # 22 最近关播、33 更早
        self.assertEqual(
            order_room_ids(base, "status", live_states={},
                           live_since=live, offline_at=offline),
            [22, 33, 11], "状态未知时应按上次关播先后排，无记录的落最后")
        self.assertEqual(
            order_room_ids(base, "status", live_states={11: "直播中"},
                           live_since=live, offline_at=offline),
            [11, 22, 33], "状态到达后在播房间应排到最前")

    def test_stale_and_invalid_marks_are_dropped(self):
        """过旧 / 未来 / 非法的记录一律丢弃：上次会话的陈旧时间不能把排序带偏。"""
        now = 1000000.0
        marks = {
            "1": now - 60,                        # 有效
            "2": now - LIVE_MARK_MAX_AGE_S - 1,   # 过旧（不可能是同一场直播）
            "3": now + 3600,                      # 未来（时钟回拨或脏数据）
            "4": 0,                               # 无意义
            "5": -5,                              # 负数
            "abc": now,                           # 键不是房间号
            "6": "not-a-time",                    # 值不是数字
        }
        self.assertEqual(prune_live_marks(marks, now=now), {1: now - 60})

    def test_empty_and_none_are_safe(self):
        self.assertEqual(prune_live_marks({}), {})
        self.assertEqual(prune_live_marks(None), {})

    def test_offline_marks_never_expire(self):
        """关播时刻**永久保留**、开播时刻只保留 24 小时（ROADMAP 110）。

        数天未开播的房间的关播记录依然是「它最近一次下播」的准确描述——此前统一按
        24 小时裁掉，会让它掉进「无记录」档、按自定义顺序乱排（用户反馈「数天未开播的
        直播间排在其他直播间上面」）。开播时刻没有这个豁免：跨场次沿用会把「已播」算错。
        """
        now = 1900000000.0  # 真实量级（2030 年），比 NEW_ROOM_MARK_S（2000 年）晚
        year_old = now - 365 * 86400
        # 关播时刻：一年前也保留（max_age_s=None = 不限龄）；非法 / 未来值仍会丢
        self.assertEqual(
            prune_live_marks({"1": year_old, "2": now + 3600, "3": 0},
                             now=now, max_age_s=None),
            {1: year_old})
        # 开播时刻仍按 24 小时裁（LIVE_MARK_MAX_AGE_S 默认值）
        self.assertEqual(prune_live_marks({"1": year_old}, now=now), {})

    def test_backfill_offline_marks_seeds_missing_only(self):
        """补默认标记：只补「没有记录」的房间，已有记录（含真实时间）一律不动。"""
        offline = {11: 1790000000.0}          # 已有真实记录
        filled = backfill_offline_marks(offline, [11, 22, "33", "abc"])
        self.assertEqual(filled, 2)
        self.assertEqual(offline[11], 1790000000.0, "已有记录不许被默认值覆盖")
        self.assertEqual(offline[22], NEW_ROOM_MARK_S)
        self.assertEqual(offline[33], NEW_ROOM_MARK_S)
        # 再次补：没有可补的，返回 0（幂等）
        self.assertEqual(backfill_offline_marks(offline, [11, 22, 33]), 0)

    def test_days_offline_room_orders_by_its_real_offline_time(self):
        """修复目标场景：3 天前下播的房间按**真实关播时间**排在「昨天关播」的后面，
        而不是掉进「无记录」档、凭自定义顺序压过别人。"""
        base = [11, 22]
        now = 4000000.0
        offline = {11: now - 3 * 86400, 22: now - 86400}
        self.assertEqual(
            order_room_ids(base, "status", live_states={}, offline_at=offline),
            [22, 11], "最近关播的应排在前，与自定义顺序无关")
        # 对照「修前」行为：关播记录若被 24 小时规则裁掉，就只剩自定义顺序（BUG 的样子）
        self.assertEqual(
            order_room_ids(base, "status", live_states={}, offline_at={}),
            [11, 22])

    def test_new_room_seed_sinks_to_the_bottom(self):
        """新监控的直播间默认「很久之前开播」：默认标记让它排在所有有真实关播记录的
        房间后面，直到它真的下播（拿到真实关播时刻）为止。"""
        base = [11, 22, 33]
        now = 1900000000.0  # 真实量级，NEW_ROOM_MARK_S（2000 年）比所有真实记录都早
        offline = {11: now - 86400,          # 昨天关播
                   22: NEW_ROOM_MARK_S,      # 新监控（默认标记）
                   33: now - 3 * 86400}      # 3 天前关播
        self.assertEqual(
            order_room_ids(base, "status", live_states={}, offline_at=offline),
            [11, 33, 22], "新房间应沉底，老房间按关播时间倒序")

    def test_observation_fallback_is_logged(self):
        """没有接口开播时刻、回退「本地观测」时要留痕。

        这条日志能让用户知道：该房间的「已播」时长与排序时间是**估算**的，接口一旦
        返回就会校正。
        """
        tree = ast.parse((ROOT / "blive_sc_get" / "gui_app.py").read_text(encoding="utf-8"))
        node = next((item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef)
                     and item.name == "update_live_started_at"), None)
        self.assertIsNotNone(node, "未找到 update_live_started_at")
        self.assertIn("log_room", {sub.id for sub in ast.walk(node)
                                   if isinstance(sub, ast.Name)},
                      "回退本地观测未记日志")


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
        update_live_started_at(live, 11, True, 10.0)
        update_live_started_at(live, 33, True, 20.0)
        states = {11: "直播中", 33: "直播中", 22: "未开播"}
        self.assertEqual(
            order_room_ids(base, "status", live_states=states,
                           live_since=live, offline_at=offline),
            [33, 11, 22])
        update_live_started_at(live, 33, False)
        update_offline_at(offline, 33, False, 30.0, was_live=True)
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


class FollowingOfflineMarksTests(unittest.TestCase):
    """「上次直播结束时刻」的来源、合并与接线（ROADMAP 97）。

    程序关闭期间下播的房间收不到关播信号：以前重启后既无开播也无下播记录，在「按直播状态」
    排序里掉进「无记录」档（排最后）。现从关注列表接口取**真实下播时刻**
    （`record_live_time`），并叠加「退出时给仍在直播房间记的下播估算」，两者取较晚的那个。
    """

    def test_merge_takes_later_mark(self):
        merged = merge_offline_marks({1: 1000.0, 2: 500.0},
                                     {1: 2000, 3: 1500, 4: "坏值"})
        self.assertEqual(merged, {1: 2000.0, 2: 500.0, 3: 1500.0})

    def test_merge_normalizes_and_tolerates_bad_input(self):
        self.assertEqual(merge_offline_marks({"7": "300"}, None), {7: 300.0})
        self.assertEqual(merge_offline_marks(None, {}), {})
        self.assertEqual(merge_offline_marks({"x": 1, "9": None}, {"bad": object()}), {})

    def test_closed_period_offline_room_gets_ordered_by_real_time(self):
        """关闭期间下播的房间：修前落「无记录」档，修后按真实下播时间就位。"""
        now = 1_000_000.0
        states = {11: "直播中", 22: "未开播", 33: "未开播", 44: "未开播"}
        live_since = {11: now - 3600}
        local = {22: now - 1800, 44: now - 300}  # 观测到的关播 + 退出时的估算
        remote = {33: now - 600}                 # 关闭期间下播：只有接口能给
        before = order_room_ids(BASE, "status", live_states=states,
                                live_since=live_since, offline_at=local)
        after = order_room_ids(BASE, "status", live_states=states, live_since=live_since,
                               offline_at=merge_offline_marks(local, remote))
        self.assertEqual(before, [11, 44, 22, 33], "33 无记录时应排最后（修前行为）")
        self.assertEqual(after, [11, 44, 33, 22], "33 应按真实下播时间插到 22 之前")

    def test_exit_writes_offline_estimate(self):
        """退出时给「此刻仍在直播」的房间写估算下播时刻，并落盘（两版都要）。"""
        for module, cls in (("gui_app.py", "ScMonitorApp"),
                            ("qt_app.py", "QtScMonitorApp")):
            with self.subTest(module=module):
                node = _method(_module_tree(module), cls, "_record_shutdown_offline_estimate")
                self.assertIn("_offline_at", _attr_names(node), "未写入下播时刻")
                self.assertIn("直播中", _consts(node), "未限定「仍在直播中」的房间")
                self.assertIn("_save_config", _called_attrs(node), "估算值未落盘")

    def test_following_fetch_wired_into_both_hosts(self):
        """启动与切到「按直播状态」时都要抓；拿到值要触发重排（两版）。"""
        for module, cls in (("gui_app.py", "ScMonitorApp"),
                            ("qt_app.py", "QtScMonitorApp")):
            with self.subTest(module=module):
                tree = _module_tree(module)
                for trigger in ("_on_hub_ready", "_on_sort_mode_changed"):
                    node = _method(tree, cls, trigger)
                    self.assertIn("_refresh_following_marks", _called_attrs(node),
                                  f"{trigger} 未抓取真实下播时刻")
                fetch = _method(tree, cls, "_refresh_following_marks")
                self.assertIn("FOLLOWING_REFRESH_COOLDOWN_S", _names(fetch), "未做冷却节流")
                self.assertIn("status", _consts(fetch), "未限定只在「按直播状态」下抓")
                self.assertIn("logged_in", _consts(fetch), "未检查登录态（接口需要）")
                loader = _method(tree, cls, "_async_load_following_marks")
                self.assertIn("get_following_live_marks", _called_attrs(loader))
                handler = _method(tree, cls, "_on_following_marks")
                self.assertIn("_remote_offline_at", _attr_names(handler))
                self.assertIn("_sort_dirty", _attr_names(handler), "拿到值后未触发重排")

    def test_sorting_uses_merged_marks(self):
        for module, cls, method in (("gui_app.py", "ScMonitorApp", "_sorted_room_ids"),
                                    ("qt_app.py", "QtScMonitorApp", "_refresh_display_order")):
            with self.subTest(module=module):
                node = _method(_module_tree(module), cls, method)
                self.assertIn("merge_offline_marks", _names(node), "排序未用合并后的下播时刻")
                self.assertIn("_remote_offline_at", _attr_names(node))

    def test_api_endpoint_and_paging(self):
        tree = _module_tree("api.py")
        self.assertIn(FOLLOWING_URL, _consts(tree), "关注列表接口地址缺失")
        fetch = _method(tree, "BilibiliLiveAPI", "get_following_live_marks")
        self.assertIn("parse_following_live_marks", _names(fetch), "未复用解析函数")
        self.assertIn("FOLLOWING_PAGE_SIZE", _names(fetch), "未按实测页大小翻页")
        self.assertIn("FOLLOWING_MAX_PAGES", _names(fetch), "未限制最多翻多少页")


class OfflineMarkRetentionWiringTests(unittest.TestCase):
    """ROADMAP 110 的宿主接线：关播时刻永久保留、新房间 / 老房间都有默认标记。"""

    def test_restore_uses_permanent_retention_for_offline_marks(self):
        """启动恢复：开播时刻按 24 小时裁、关播时刻不限龄（max_age_s=None）。"""
        restore = next((node for node in ast.walk(_module_tree("gui_app.py"))
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "restore_live_marks"), None)
        self.assertIsNotNone(restore, "未找到 restore_live_marks")
        source = ast.unparse(restore)
        self.assertIn("max_age_s=None", source, "关播时刻未改为永久保留")
        self.assertIn("LIVE_MARK_MAX_AGE_S", source, "开播时刻的 24 小时规则丢失")

    def test_hosts_backfill_missing_marks(self):
        """两版启动恢复后都要给没有关播记录的房间补默认标记（很久之前，沉底）。"""
        for module, cls in (("gui_app.py", "ScMonitorApp"),
                            ("qt_app.py", "QtScMonitorApp")):
            with self.subTest(module=module):
                init = _method(_module_tree(module), cls, "__init__")
                self.assertIn("backfill_offline_marks", _names(init),
                              f"{module} 启动时未补默认关播标记")

    def test_add_result_seeds_new_room_mark(self):
        """新监控的直播间默认「很久之前开播」：两版加房成功都要写入默认关播标记。"""
        for module, cls in (("gui_app.py", "ScMonitorApp"),
                            ("qt_app.py", "QtScMonitorApp")):
            with self.subTest(module=module):
                node = _method(_module_tree(module), cls, "_on_add_result")
                source = ast.unparse(node)
                self.assertIn("_offline_at[room_id] = NEW_ROOM_MARK_S", source,
                              f"{module} 新加房间未写入默认关播标记")
                self.assertIn("NEW_ROOM_MARK_S", _names(node),
                              f"{module} 未使用共享常量 NEW_ROOM_MARK_S")


if __name__ == "__main__":
    unittest.main()
