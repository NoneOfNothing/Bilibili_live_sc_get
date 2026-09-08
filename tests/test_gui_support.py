"""GUI 支撑逻辑的单元测试（不启动 tkinter 界面）。"""

import asyncio
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from blive_sc_get.api import ApiError, BilibiliLiveAPI
from blive_sc_get.client import RoomClient
from blive_sc_get.gui_app import build_sc_segments, parse_add_input
from blive_sc_get.gui_config import (
    RoomEntry,
    load_room_entries,
    load_ui_prefs,
    save_room_entries,
)
from blive_sc_get.storage import SCStorage


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """仅实现 get() 的鸭子类型会话，用于离线测试 api 解析。"""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return _FakeResponse(self._payload)


class AnchorNameTests(unittest.TestCase):
    def test_success(self):
        session = _FakeSession(
            {"code": 0, "data": {"info": {"uname": "某主播"}}})
        api = BilibiliLiveAPI(session)
        self.assertEqual(asyncio.run(api.get_anchor_name(123)), "某主播")
        url, params = session.calls[0]
        self.assertIn("Master/info", url)
        self.assertEqual(params, {"uid": 123})

    def test_uid_zero_skips_request(self):
        session = _FakeSession({"code": 0, "data": {"info": {"uname": "x"}}})
        api = BilibiliLiveAPI(session)
        self.assertEqual(asyncio.run(api.get_anchor_name(0)), "")
        self.assertEqual(session.calls, [])

    def test_api_error_returns_empty(self):
        session = _FakeSession({"code": -404, "message": "不存在"})
        api = BilibiliLiveAPI(session)
        self.assertEqual(asyncio.run(api.get_anchor_name(123)), "")


class UidLookupTests(unittest.TestCase):
    """通过主播 uid 查询直播间房间号（个人空间网址添加用）。"""

    def test_success(self):
        session = _FakeSession({"code": 0, "data": {"roomid": 22625025}})
        api = BilibiliLiveAPI(session)
        self.assertEqual(asyncio.run(api.get_room_id_by_uid(672328094)), 22625025)
        url, params = session.calls[0]
        self.assertIn("getRoomInfoOld", url)
        self.assertEqual(params, {"mid": 672328094})

    def test_uid_zero_raises(self):
        api = BilibiliLiveAPI(_FakeSession({"code": 0, "data": {}}))
        with self.assertRaises(ApiError):
            asyncio.run(api.get_room_id_by_uid(0))

    def test_api_error_raises(self):
        api = BilibiliLiveAPI(_FakeSession({"code": -400, "message": "请求错误"}))
        with self.assertRaises(ApiError):
            asyncio.run(api.get_room_id_by_uid(123))

    def test_missing_roomid_raises(self):
        api = BilibiliLiveAPI(_FakeSession({"code": 0, "data": {}}))
        with self.assertRaises(ApiError):
            asyncio.run(api.get_room_id_by_uid(123))


class GuiConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "gui_rooms.json"

    def test_roundtrip(self):
        entries = [RoomEntry(1727071052, "主房间", True),
                   RoomEntry(22625025, "", False)]
        save_room_entries(self.path, entries)
        loaded = load_room_entries(self.path)
        self.assertEqual(loaded, entries)

    def test_notify_live_roundtrip(self):
        entries = [RoomEntry(123, "", True, uid=1, notify_live=False),
                   RoomEntry(456)]
        save_room_entries(self.path, entries)
        self.assertEqual(load_room_entries(self.path), entries)
        # 旧配置缺省为开启提醒
        self.path.write_text('{"rooms": [{"room_id": 789}]}', encoding="utf-8")
        self.assertTrue(load_room_entries(self.path)[0].notify_live)

    def test_missing_and_corrupt_file(self):
        self.assertEqual(load_room_entries(self.tmp / "nope.json"), [])
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_room_entries(self.path), [])
        self.path.write_text('{"rooms": ["bad", {"room_id": "x"}, 5]}', encoding="utf-8")
        self.assertEqual(load_room_entries(self.path), [])

    def test_dedup_and_defaults(self):
        self.path.write_text(
            '{"rooms": [{"room_id": 123}, {"room_id": 123, "note": "dup"},'
            ' {"room_id": 456, "enabled": false, "note": "n"}]}',
            encoding="utf-8",
        )
        loaded = load_room_entries(self.path)
        self.assertEqual(loaded, [RoomEntry(123, "", True), RoomEntry(456, "n", False)])

    def test_uid_roundtrip(self):
        entries = [RoomEntry(123, "", True, uid=672328094),
                   RoomEntry(456, "", True, uid=0)]
        save_room_entries(self.path, entries)
        self.assertEqual(load_room_entries(self.path), entries)
        # 旧配置没有 uid 字段时缺省为 0
        self.path.write_text('{"rooms": [{"room_id": 789}]}', encoding="utf-8")
        self.assertEqual(load_room_entries(self.path)[0].uid, 0)

    def test_ui_prefs_roundtrip(self):
        save_room_entries(self.path, [RoomEntry(123)],
                          ui={"sort_mode": "status", "pin_live": True})
        self.assertEqual(load_ui_prefs(self.path),
                         {"sort_mode": "status", "pin_live": True})

    def test_ui_prefs_defaults(self):
        self.assertEqual(load_ui_prefs(self.tmp / "nope.json"),
                         {"sort_mode": "manual", "pin_live": False})
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_ui_prefs(self.path),
                         {"sort_mode": "manual", "pin_live": False})
        # 非法排序方式回退默认
        self.path.write_text('{"rooms": [], "ui": {"sort_mode": "bogus"}}',
                             encoding="utf-8")
        self.assertEqual(load_ui_prefs(self.path)["sort_mode"], "manual")


class StorageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.storage = SCStorage(self.tmp)
        base = datetime(2026, 9, 8, 20, 0, 0)
        for i in (1, 2):
            self.storage.save_sc(9527, {"id": 1000 + i, "price": 30 * i, "message": f"m{i}",
                                        "user_info": {"uname": f"u{i}"}},
                                 base.replace(minute=i))
        self.storage.mark_deleted(9527, [1001], base)

    def test_history_order_and_content(self):
        records = self.storage.load_sc_history(9527)
        self.assertEqual([r["sc"]["id"] for r in records], [1001, 1002])
        self.assertEqual(records[0]["sc"]["price"], 30)
        self.assertEqual(self.storage.load_sc_history(111), [])

    def test_sc_page_and_count(self):
        base = datetime(2026, 9, 8, 20, 0, 0)
        for i in range(5):
            self.storage.save_sc(8888, {"id": 2000 + i, "price": 10,
                                        "message": f"m{i}",
                                        "user_info": {"uname": f"u{i}"}},
                                 base.replace(minute=i))
        self.storage.mark_deleted(8888, [2003], base)
        self.assertEqual(self.storage.count_sc_records(8888), 5)
        # 第一页取最新 3 条，按时间正序
        page1 = self.storage.load_sc_page(8888, limit=3)
        self.assertEqual([r["sc"]["id"] for r in page1], [2002, 2003, 2004])
        # 第二页跳过 3 条取更早的 2 条
        page2 = self.storage.load_sc_page(8888, limit=3, skip=3)
        self.assertEqual([r["sc"]["id"] for r in page2], [2000, 2001])
        # 删除事件不计入，全量读取按时间正序
        all_ids = [r["sc"]["id"] for r in self.storage.load_sc_page(8888, limit=100)]
        self.assertEqual(all_ids, [2000, 2001, 2002, 2003, 2004])

    def test_deleted_ids(self):
        deleted = self.storage.load_deleted_ids(9527)
        self.assertIn("1001", deleted)
        self.assertEqual(self.storage.load_deleted_ids(111), {})


class ClientEventCallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = []
        self.client = RoomClient(api=None, room_id=9527, storage=SCStorage(self.tmp),
                                 event_callback=lambda etype, payload: self.events.append((etype, payload)))

    def test_sc_event(self):
        sample = {"cmd": "SUPER_CHAT_MESSAGE",
                  "data": {"id": 5001, "uid": 1, "price": 66, "message": "hi",
                           "user_info": {"uname": "测试"}}}
        self.client._handle_business_message(
            __import__("json").dumps(sample, ensure_ascii=False).encode("utf-8"))
        kinds = [e[0] for e in self.events]
        self.assertIn("sc", kinds)
        payload = dict(self.events[kinds.index("sc")][1])
        self.assertEqual(payload["room_id"], 9527)
        self.assertEqual(payload["sc"]["id"], 5001)
        self.assertTrue(payload["saved"])

    def test_delete_event(self):
        msg = {"cmd": "SUPER_CHAT_MESSAGE_DELETE", "data": {"ids": [5001]}}
        self.client._handle_business_message(
            __import__("json").dumps(msg).encode("utf-8"))
        kinds = [e[0] for e in self.events]
        self.assertIn("delete", kinds)
        self.assertEqual(self.events[kinds.index("delete")][1]["ids"], [5001])


class ParseAddInputTests(unittest.TestCase):
    """添加输入解析：space.bilibili.com 网址必须按 uid 处理，不能当成房间号。"""

    def test_space_url_returns_uid(self):
        for raw in ("https://space.bilibili.com/672328094",
                    "space.bilibili.com/672328094?spm_id_from=xxx",
                    "https://space.bilibili.com/672328094/"):
            room_id, uid = parse_add_input(raw)
            self.assertIsNone(room_id)
            self.assertEqual(uid, 672328094)

    def test_room_inputs_return_room_id(self):
        for raw, expected in (("22625025", 22625025),
                              ("https://live.bilibili.com/21452505", 21452505)):
            room_id, uid = parse_add_input(raw)
            self.assertEqual(room_id, expected)
            self.assertIsNone(uid)

    def test_unparseable_raises(self):
        with self.assertRaises(Exception):
            parse_add_input("不是链接")

    def test_space_url_not_treated_as_room_id(self):
        # uid 是 11 位纯数字，若先按房间号解析会被误当成房间号
        raw = "https://space.bilibili.com/12345678901"
        room_id, uid = parse_add_input(raw)
        self.assertIsNone(room_id)
        self.assertEqual(uid, 12345678901)


class BuildScSegmentsTests(unittest.TestCase):
    SC = {"id": 1, "price": 120, "message": "加油",
          "user_info": {"uname": "测试用户", "uid": 888}}

    def test_user_tag_carries_uid(self):
        segments = build_sc_segments("t", self.SC)
        user_segment = next(s for s in segments if "测试用户" in s[0])
        self.assertIn("user", user_segment[1].split())
        self.assertIn("uid:888", user_segment[1].split())

    def test_user_tag_without_uid(self):
        sc = dict(self.SC, user_info={"uname": "测试用户"})
        segments = build_sc_segments("t", sc)
        user_segment = next(s for s in segments if "测试用户" in s[0])
        self.assertEqual(user_segment[1], "user")

    def test_segments_content_and_price_tag(self):
        segments = build_sc_segments("2026-09-08T20:01:02", self.SC)
        text = "".join(s[0] for s in segments)
        self.assertIn("[2026-09-08 20:01:02]", text)
        self.assertIn("¥120", text)
        self.assertIn("测试用户：", text)
        self.assertIn("加油", text)
        self.assertIn(("¥120 ", "price_100"), segments)

    def test_deleted_and_pending_marks(self):
        deleted = build_sc_segments("t", self.SC, deleted=True)
        self.assertIn("（已删除，退款）", "".join(s[0] for s in deleted))
        pending = build_sc_segments("t", self.SC, pending=True)
        self.assertIn("（文件被占用，稍后自动写入）", "".join(s[0] for s in pending))

    def test_price_tiers(self):
        for price, tag in ((600, "price_500"), (150, "price_100"),
                           (60, "price_50"), (10, "price_0")):
            segments = build_sc_segments("t", dict(self.SC, price=price))
            self.assertIn(tag, [s[1] for s in segments], f"price={price}")


if __name__ == "__main__":
    unittest.main()
