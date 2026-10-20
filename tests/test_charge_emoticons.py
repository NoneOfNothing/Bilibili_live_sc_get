"""充电表情包（ROADMAP 116）：解析、权益过滤、合并与两版接线的离线测试。

实测背景（2026-10-20，本机登录态只读探测）：
- 直播间接口 ``GetEmoticons`` **不返回**充电表情包（即使在该 UP 的直播间、
  账号已充电、该 UP 已满 1000 人）；
- 全站可用的充电包只在账号级面板 ``x/emote/user/panel/web?business=reply`` 下发
  （``type == 12``，包上带 ``ref_mid``）；
- 某 UP 的充电表情与「当前账号是否有使用权」在
  ``x/upowerv2/gw/rights/guide`` 的 ``rights[].right_list[]`` 里，
  ``right_type == "emote"`` 且 ``locked == false`` 才算解锁。
"""

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from blive_sc_get.api import (
    BilibiliLiveAPI,
    parse_account_charge_emoticons,
    parse_up_charge_emoticons,
)
from blive_sc_get.gui_app import (
    ScMonitorApp,
    merge_charge_emoticons,
    package_ref_mid,
)

PKG = Path(__file__).resolve().parent.parent / "blive_sc_get"

ACCOUNT_DATA = {
    "packages": [
        {
            "id": 10437,
            "text": "灰泽满Hazel（充电）",
            "url": "cover.png",
            "type": 12,
            "ref_mid": 1298779265,
            "emote": [
                {"id": 149666, "text": "[UPOWER_1298779265_品]", "url": "a.png",
                 "meta": {"size": 2, "alias": "品"},
                 "flags": {"unlocked": False, "recent_use_forbid": True}},
                {"id": 149667, "text": "[UPOWER_1298779265_阅]", "url": "b.png",
                 "meta": {"size": 1}},
            ],
        },
        {
            # 购买所得的表情包（type=3）：直播间弹幕发不出去，必须忽略
            "id": 345, "text": "装扮表情", "url": "suit.png", "type": 3,
            "ref_mid": 0,
            "emote": [{"text": "upower_[张AiNi和老E_ICU]", "url": "c.png"}],
        },
    ]
}

RIGHTS_DATA = {
    "up": {"up_mid": 1298779265, "name": "灰泽满Hazel"},
    "rights": [
        {
            "level_type": 20,
            "right_list": [
                {"right_type": "emote", "locked": True,
                 "list": [{"id": 1, "name": "未解锁", "icon": "x.png"}]},
                {"right_type": "medal", "locked": True, "list": [{"fans_name": "牌"}]},
            ],
        },
        {
            "level_type": 10,
            "right_list": [
                {"right_type": "emote", "locked": False,
                 "list": [
                     {"id": 2, "name": "品", "icon": "y.png"},
                     {"id": 3, "name": "阅", "icon": "z.png"},
                 ]},
                {"right_type": "medal", "locked": False, "list": [{"fans_name": "牌"}]},
            ],
        },
    ],
}


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


class AccountChargeEmoticonTests(unittest.TestCase):
    """账号级面板：只取 type=12 的充电包。"""

    def test_keeps_only_charge_packages(self):
        packages = parse_account_charge_emoticons(ACCOUNT_DATA)
        self.assertEqual(len(packages), 1)
        package = packages[0]
        self.assertEqual(package["name"], "灰泽满Hazel（充电）")
        self.assertEqual(package["id"], 10437)
        self.assertEqual(package["ref_mid"], 1298779265)
        self.assertEqual(package["type"], 12)
        self.assertEqual(package["cover"], "cover.png")

    def test_unique_and_display_text(self):
        package = parse_account_charge_emoticons(ACCOUNT_DATA)[0]
        first, second = package["emoticons"]
        self.assertEqual(first["unique"], "upower_[UPOWER_1298779265_品]")
        self.assertEqual(first["trigger"], "[UPOWER_1298779265_品]")
        self.assertEqual(first["text"], "[品]")
        self.assertEqual((first["width"], first["height"]), (162, 162))
        # meta.size == 1 是小表情
        self.assertEqual((second["width"], second["height"]), (40, 40))
        self.assertEqual(second["text"], "[阅]")

    def test_ignores_suit_packages(self):
        """装扮表情（type=3 / unique 形如 upower_[UP_名字]）不能当成充电包。"""
        packages = parse_account_charge_emoticons(ACCOUNT_DATA)
        uniques = [e["unique"] for p in packages for e in p["emoticons"]]
        self.assertNotIn("upower_[张AiNi和老E_ICU]", uniques)

    def test_empty_or_broken(self):
        for bad in (None, {}, {"packages": []}, {"packages": "x"}, []):
            with self.subTest(bad=bad):
                self.assertEqual(parse_account_charge_emoticons(bad), [])

    def test_drops_items_without_url_or_upower_token(self):
        data = {"packages": [{
            "id": 1, "text": "充电", "type": 12, "ref_mid": 7,
            "emote": [
                {"text": "[UPOWER_7_无图]", "url": "", "meta": {"alias": "无图"}},
                {"text": "upower_[UP_旧式]", "url": "x.png"},
                {"text": "[UPOWER_7_好]", "url": "y.png", "meta": {"alias": "好"}},
                {"text": "[UPOWER_7_好]", "url": "dup.png", "meta": {"alias": "好"}},
            ],
        }]}
        packages = parse_account_charge_emoticons(data)
        self.assertEqual(len(packages), 1)
        self.assertEqual([e["unique"] for e in packages[0]["emoticons"]],
                         ["upower_[UPOWER_7_好]"])


class UpChargeRightTests(unittest.TestCase):
    """权益接口：只收 locked=false；没有已解锁项 = 当前账号无使用权。"""

    def test_collects_unlocked_emoticons_only(self):
        packages = parse_up_charge_emoticons(RIGHTS_DATA, 1298779265)
        self.assertEqual(len(packages), 1)
        package = packages[0]
        self.assertEqual(package["name"], "灰泽满Hazel（充电）")
        self.assertEqual(package["id"], 1298779265)
        self.assertEqual(package["ref_mid"], 1298779265)
        uniques = [e["unique"] for e in package["emoticons"]]
        self.assertEqual(uniques, ["upower_[UPOWER_1298779265_品]",
                                   "upower_[UPOWER_1298779265_阅]"])

    def test_locked_only_means_no_right(self):
        locked = {"up": {"name": "某UP"}, "rights": [{
            "level_type": 10, "right_list": [
                {"right_type": "emote", "locked": True,
                 "list": [{"id": 1, "name": "品", "icon": "x.png"}]},
            ]}]}
        self.assertEqual(parse_up_charge_emoticons(locked, 7), [])

    def test_missing_locked_is_treated_as_locked(self):
        data = {"rights": [{"right_list": [
            {"right_type": "emote", "list": [{"id": 1, "name": "品", "icon": "x.png"}]},
        ]}]}
        self.assertEqual(parse_up_charge_emoticons(data, 7), [])

    def test_dedupes_across_levels(self):
        data = {"up": {"name": "某UP"}, "rights": [
            {"level_type": 10, "right_list": [
                {"right_type": "emote", "locked": False,
                 "list": [{"id": 1, "name": "品", "icon": "x.png"}]}]},
            {"level_type": 20, "right_list": [
                {"right_type": "emote", "locked": False,
                 "list": [{"id": 1, "name": "品", "icon": "x.png"}]}]},
        ]}
        packages = parse_up_charge_emoticons(data, 7)
        self.assertEqual(len(packages[0]["emoticons"]), 1)

    def test_broken_input(self):
        for bad in (None, [], {}, {"rights": "x"}):
            with self.subTest(bad=bad):
                self.assertEqual(parse_up_charge_emoticons(bad, 7), [])


class MergeChargeEmoticonTests(unittest.TestCase):
    """合并：房间包在前，当前 UP 充电包其次，其它全站包最后；unique 去重。"""

    def _room(self):
        return [{"id": 1, "name": "房间专属", "emoticons": [
            {"unique": "room_1_1", "text": "[嗨]"}]}]

    def _global(self):
        return [{
            "id": 10437, "name": "灰泽满Hazel（充电）", "cover": "cover.png",
            "ref_mid": 1298779265,
            "emoticons": [
                {"unique": "upower_[UPOWER_1298779265_品]", "text": "[品]"},
                {"unique": "upower_[UPOWER_1298779265_阅]", "text": "[阅]"},
            ],
        }, {
            "id": 200, "name": "别的UP（充电）", "ref_mid": 99,
            "emoticons": [{"unique": "upower_[UPOWER_99_哦]", "text": "[哦]"}],
        }]

    def _up(self):
        return [{
            "id": 1298779265, "name": "灰泽满Hazel（充电）",
            "ref_mid": 1298779265,
            "emoticons": [
                {"unique": "upower_[UPOWER_1298779265_品]", "text": "[品]"},
                {"unique": "upower_[UPOWER_1298779265_新]", "text": "[新]"},
            ],
        }]

    def test_no_charge_packages_keeps_room_packages(self):
        room = self._room()
        self.assertEqual(merge_charge_emoticons(room, [], [], 0), room)

    def test_order_and_same_up_merge(self):
        merged = merge_charge_emoticons(self._room(), self._global(), self._up(),
                                        1298779265)
        self.assertEqual([p["name"] for p in merged],
                         ["房间专属", "灰泽满Hazel（充电）", "别的UP（充电）"])
        current = merged[1]
        self.assertEqual(current["id"], 10437)  # 保留账号面板的真实包 id
        self.assertEqual([e["unique"] for e in current["emoticons"]], [
            "upower_[UPOWER_1298779265_品]",
            "upower_[UPOWER_1298779265_阅]",
            "upower_[UPOWER_1298779265_新]",  # 权益接口独有的表情补进来
        ])

    def test_room_package_wins_duplicate_unique(self):
        room = [{"id": 1, "name": "房间专属", "emoticons": [
            {"unique": "upower_[UPOWER_1298779265_品]", "text": "[房间版]"}]}]
        merged = merge_charge_emoticons(
            room, [{"id": 10437, "ref_mid": 1298779265, "name": "充电",
                    "emoticons": [{"unique": "upower_[UPOWER_1298779265_品]"},
                                  {"unique": "upower_[UPOWER_1298779265_阅]"}]}],
            [], 1298779265)
        self.assertEqual(merged[0]["emoticons"][0]["text"], "[房间版]")
        self.assertEqual([e["unique"] for e in merged[1]["emoticons"]],
                         ["upower_[UPOWER_1298779265_阅]"])

    def test_packages_without_ref_mid_are_dropped(self):
        merged = merge_charge_emoticons(
            self._room(), [{"id": 9, "name": "无归属", "emoticons": [
                {"unique": "upower_[UPOWER_1_x]"}]}], [], 0)
        self.assertEqual(len(merged), 1)

    def test_empty_packages_are_dropped(self):
        merged = merge_charge_emoticons(
            self._room(), [{"id": 9, "ref_mid": 5, "name": "空包", "emoticons": []}],
            [], 0)
        self.assertEqual(len(merged), 1)

    def test_package_ref_mid(self):
        self.assertEqual(package_ref_mid({"ref_mid": "123"}), 123)
        self.assertEqual(package_ref_mid({"ref_mid": None}), 0)
        self.assertEqual(package_ref_mid("bad"), 0)


class HostEntitlementFilterTests(unittest.TestCase):
    """宿主展示层：只有**已确认有权益**的 UP 的全站包才进入面板。"""

    def _host(self, up_right):
        host = SimpleNamespace()
        host.entries = {1: SimpleNamespace(uid=1298779265)}
        host._emoticons = {1: [{"id": 1, "name": "房间专属", "emoticons": [
            {"unique": "room_1_1"}]}]}
        host._charge_global = [{
            "id": 10437, "name": "有权益（充电）", "ref_mid": 1298779265,
            "emoticons": [{"unique": "upower_[UPOWER_1298779265_品]"}],
        }, {
            "id": 200, "name": "无权益（充电）", "ref_mid": 99,
            "emoticons": [{"unique": "upower_[UPOWER_99_哦]"}],
        }]
        host._charge_up = {1298779265: up_right}  # 99 没查过 / 无权益
        host._emoticon_display = {}
        host._room_uid = ScMonitorApp._room_uid.__get__(host)
        host._rebuild_display_emoticons = ScMonitorApp._rebuild_display_emoticons.__get__(host)
        host._display_emoticons = ScMonitorApp._display_emoticons.__get__(host)
        return host

    def test_only_entitled_packages_are_shown(self):
        host = self._host([{"id": 1298779265, "name": "有权益（充电）",
                            "ref_mid": 1298779265,
                            "emoticons": [{"unique": "upower_[UPOWER_1298779265_品]"}]}])
        merged = host._display_emoticons(1)
        names = [p["name"] for p in merged]
        self.assertIn("有权益（充电）", names)
        self.assertNotIn("无权益（充电）", names)

    def test_no_entitlement_hides_even_global_package(self):
        host = self._host([])  # 查过了：当前账号没有使用权
        merged = host._display_emoticons(1)
        self.assertEqual([p["name"] for p in merged], ["房间专属"])


class ChargeEmoticonApiTests(unittest.TestCase):
    """接口方法：未登录/缺 csrf 短路、203010 视为空、失败返回 None。"""

    def _api(self, cookie="SESSDATA=x; bili_jct=abc"):
        api = BilibiliLiveAPI(None, cookie)
        api.uid = 1
        api.calls = []

        async def fake_get_json(url, params):
            api.calls.append((url, params))
            return api.payload

        api._get_json = fake_get_json
        return api

    def test_account_requires_login(self):
        api = self._api(cookie="")
        self.assertIsNone(asyncio.run(api.get_account_charge_emoticons()))
        self.assertEqual(api.calls, [])

    def test_account_parses_packages(self):
        api = self._api()
        api.payload = {"code": 0, "data": ACCOUNT_DATA}
        packages = asyncio.run(api.get_account_charge_emoticons())
        self.assertEqual(len(packages), 1)
        self.assertEqual(api.calls[0][1], {"business": "reply"})
        self.assertIn("emote/user/panel/web", api.calls[0][0])

    def test_account_failure_is_none(self):
        api = self._api()
        api.payload = {"code": -101, "message": "未登录"}
        self.assertIsNone(asyncio.run(api.get_account_charge_emoticons()))

    def test_up_requires_csrf(self):
        api = self._api(cookie="SESSDATA=only")
        self.assertIsNone(asyncio.run(api.get_up_charge_emoticons(7)))
        self.assertEqual(api.calls, [])

    def test_up_203010_is_empty(self):
        api = self._api()
        api.payload = {"code": 203010, "message": "203010"}
        self.assertEqual(asyncio.run(api.get_up_charge_emoticons(7)), [])

    def test_up_parses_unlocked(self):
        api = self._api()
        api.payload = {"code": 0, "data": RIGHTS_DATA}
        packages = asyncio.run(api.get_up_charge_emoticons(1298779265))
        self.assertEqual(len(packages), 1)
        self.assertEqual(api.calls[0][1], {"csrf": "abc", "up_mid": 1298779265})

    def test_up_failure_is_none(self):
        api = self._api()
        api.payload = {"code": -352, "message": "风控"}
        self.assertIsNone(asyncio.run(api.get_up_charge_emoticons(7)))


class ChargeEmoticonWiringTests(unittest.TestCase):
    """两版接线（静态 AST）：请求入口、权益过滤、队列回投与缓存失效。"""

    def test_tk_opens_panel_then_requests_charge(self):
        tree = _tree("gui_app.py")
        open_panel = _method(tree, "ScMonitorApp", "_on_open_emoticons")
        self.assertIn("_ensure_charge_emoticons", _called_attrs(open_panel))

    def test_tk_async_loader_uses_both_sources(self):
        tree = _tree("gui_app.py")
        loader = _method(tree, "ScMonitorApp", "_async_load_charge_emoticons")
        self.assertIn("get_account_charge_emoticons", _called_attrs(loader))
        self.assertIn("get_up_charge_emoticons", _called_attrs(loader))

    def test_tk_display_filters_by_entitlement(self):
        tree = _tree("gui_app.py")
        rebuild = _method(tree, "ScMonitorApp", "_rebuild_display_emoticons")
        self.assertIn("merge_charge_emoticons", _names(rebuild))
        self.assertIn("_charge_up", _attrs(rebuild), "展示层未按权益结论过滤")

    def test_tk_queue_and_login_reset(self):
        source = (PKG / "gui_app.py").read_text(encoding="utf-8")
        self.assertIn('elif kind == "charge_emoticons":', source)
        self.assertIn("self._charge_global = None", source)
        self.assertIn("self._charge_up.clear()", source)

    def test_tk_room_window_uses_merged_cache(self):
        """Tk 独立窗口展开时也要用合并列表并触发充电表情刷新（ROADMAP 116）。"""
        tree = _tree("tk_room_window.py")
        open_panel = _method(tree, "RoomChatWindow", "_on_open_emoticons")
        self.assertIn("_display_emoticons", _attrs(open_panel))
        self.assertIn("_ensure_charge_emoticons", _attrs(open_panel))
        self.assertNotIn("_emoticons", _attrs(open_panel),
                         "独立窗口仍在直接读原始包缓存（会漏掉充电包）")

    def test_qt_panel_requests_charge(self):
        tree = _tree("qt_dm_panel.py")
        refresh = _method(tree, "DmPanel", "_refresh_emoticons")
        self.assertIn("request_charge_emoticons", _called_attrs(refresh))

    def test_qt_host_merges_and_handles_queue(self):
        tree = _tree("qt_app.py")
        host = _class(tree, "QtScMonitorApp")
        names = {n.name for n in ast.walk(host)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for required in ("_rebuild_display_emoticons", "_on_charge_emoticons",
                         "request_charge_emoticons"):
            self.assertIn(required, names)
        rebuild = _method(tree, "QtScMonitorApp", "_rebuild_display_emoticons")
        self.assertIn("merge_charge_emoticons", _names(rebuild))
        self.assertIn("_charge_up", _attrs(rebuild), "Qt 展示层未按权益结论过滤")


class ChargeEmoticonDoesNotLeakIntoMedalTaskTests(unittest.TestCase):
    """粉丝牌自动发弹幕只挑 ``room_`` 前缀的专属表情，充电表情不能被误选。"""

    def test_charge_unique_prefix_is_not_room(self):
        package = parse_up_charge_emoticons(RIGHTS_DATA, 1298779265)[0]
        uniques = [e["unique"] for e in package["emoticons"]]
        self.assertTrue(uniques)
        for unique in uniques:
            self.assertFalse(unique.startswith("room_"))


if __name__ == "__main__":
    unittest.main()
