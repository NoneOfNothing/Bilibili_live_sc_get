"""GUI 支撑逻辑的单元测试（不启动 tkinter 界面）。"""

import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from blive_sc_get.api import (
    ApiError,
    BilibiliLiveAPI,
    describe_send_error,
    parse_room_emoticon_packages,
)
from blive_sc_get.app_config import (
    DEFAULT_EMOTICON_TOOLTIP,
    AppConfig,
    load_app_config,
)
from blive_sc_get.client import RECONNECT_MAX_DELAY, RoomClient, compute_reconnect_delay
from blive_sc_get.gui_app import (
    EMOTICON_ICON_MAX_HEIGHT,
    EMOTICON_ICON_MAX_WIDTH,
    build_sc_segments,
    danmaku_content_from_line,
    danmaku_send_guard,
    emoticon_display_size,
    emoticon_id_by_unique,
    emoticon_packages_signature,
    emoticon_tooltip_text,
    fit_emoticon_scale,
    parse_add_input,
    select_dm_options,
    text_scrolled_to_bottom,
    tooltip_position,
    unseen_badge_text,
)
from blive_sc_get.gui_config import (
    RoomEntry,
    load_emoticon_memory,
    load_room_entries,
    load_ui_prefs,
    save_room_entries,
)
from blive_sc_get.overlay import toast_geometry
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
    """实现 get()/post() 的鸭子类型会话，用于离线测试 api 解析。"""

    def __init__(self, payload, post_payload=None):
        self._payload = payload
        self._post_payload = payload if post_payload is None else post_payload
        self.calls = []
        self.post_calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return _FakeResponse(self._payload)

    def post(self, url, data=None, headers=None):
        self.post_calls.append((url, data, headers))
        return _FakeResponse(self._post_payload)


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
                          ui={"sort_mode": "status", "pin_live": True,
                              "notify_overlay": False, "notify_persist": True,
                              "notify_sound": "三连音", "dm_visible": True,
                              "dm_emoticon_image": False})
        self.assertEqual(load_ui_prefs(self.path),
                         {"sort_mode": "status", "pin_live": True,
                          "notify_overlay": False, "notify_persist": True,
                          "notify_sound": "三连音", "dm_visible": True,
                          "dm_emoticon_image": False})
        # 旧配置缺字段时的缺省值（弹幕表情图默认开启）
        save_room_entries(self.path, [RoomEntry(123)],
                          ui={"sort_mode": "manual", "pin_live": False})
        prefs = load_ui_prefs(self.path)
        self.assertTrue(prefs["notify_overlay"])
        self.assertFalse(prefs["notify_persist"])
        self.assertEqual(prefs["notify_sound"], "上行双音")
        self.assertFalse(prefs["dm_visible"])
        self.assertTrue(prefs["dm_emoticon_image"])

    def test_ui_prefs_legacy_key_migrates(self):
        # 旧键名 notify_system 迁移到 notify_overlay
        self.path.write_text(
            '{"rooms": [], "ui": {"notify_system": false}}', encoding="utf-8")
        self.assertFalse(load_ui_prefs(self.path)["notify_overlay"])

    def test_ui_prefs_defaults(self):
        expected = {"sort_mode": "manual", "pin_live": False,
                    "notify_overlay": True, "notify_persist": False,
                    "notify_sound": "上行双音", "dm_visible": False,
                    "dm_emoticon_image": True}
        self.assertEqual(load_ui_prefs(self.tmp / "nope.json"), expected)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_ui_prefs(self.path), expected)
        # 非法排序方式/音效回退默认
        self.path.write_text(
            '{"rooms": [], "ui": {"sort_mode": "bogus", "notify_sound": "bogus"}}',
            encoding="utf-8")
        prefs = load_ui_prefs(self.path)
        self.assertEqual(prefs["sort_mode"], "manual")
        self.assertEqual(prefs["notify_sound"], "上行双音")


class AppConfigTests(unittest.TestCase):
    """应用级配置：写操作默认关闭，非法/损坏输入一律 fail-safe 为关闭。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "config.json"

    def test_default_is_write_disabled(self):
        self.assertFalse(AppConfig().allow_write_operations)
        self.assertFalse(load_app_config(self.tmp / "nope.json").allow_write_operations)

    def test_explicit_true_enables_write(self):
        self.path.write_text('{"allow_write_operations": true}', encoding="utf-8")
        self.assertTrue(load_app_config(self.path).allow_write_operations)

    def test_explicit_false_disables_write(self):
        self.path.write_text('{"allow_write_operations": false}', encoding="utf-8")
        self.assertFalse(load_app_config(self.path).allow_write_operations)

    def test_non_bool_and_corrupt_fall_back_to_disabled(self):
        # 字符串 "true"/"false"、数字、损坏 JSON 等一律按关闭处理，避免误开启
        for raw in ('{"allow_write_operations": "true"}',
                    '{"allow_write_operations": "false"}',
                    '{"allow_write_operations": 1}',
                    '["not an object"]',
                    "{not json"):
            self.path.write_text(raw, encoding="utf-8")
            self.assertFalse(load_app_config(self.path).allow_write_operations, raw)

    def test_extra_keys_ignored(self):
        self.path.write_text('{"_说明": "x", "other": 1}', encoding="utf-8")
        self.assertFalse(load_app_config(self.path).allow_write_operations)

    # ---- 表情悬浮提示显示哪些字段（默认仅触发词） ----

    def test_tooltip_default_is_trigger_text_only(self):
        self.assertEqual(AppConfig().emoticon_tooltip, ("text",))
        self.assertEqual(load_app_config(self.tmp / "nope.json").emoticon_tooltip,
                         DEFAULT_EMOTICON_TOOLTIP)
        self.path.write_text('{"allow_write_operations": true}', encoding="utf-8")
        self.assertEqual(load_app_config(self.path).emoticon_tooltip, ("text",))

    def test_tooltip_list_form_sorted_canonically(self):
        self.path.write_text('{"emoticon_tooltip": ["id", "text"]}', encoding="utf-8")
        self.assertEqual(load_app_config(self.path).emoticon_tooltip, ("text", "id"))

    def test_tooltip_object_form(self):
        self.path.write_text(
            '{"emoticon_tooltip": {"text": true, "id": true, "unique": false}}',
            encoding="utf-8")
        self.assertEqual(load_app_config(self.path).emoticon_tooltip, ("text", "id"))

    def test_tooltip_can_be_explicitly_disabled(self):
        for raw in ('{"emoticon_tooltip": []}', '{"emoticon_tooltip": {}}',
                    '{"emoticon_tooltip": {"text": false}}'):
            self.path.write_text(raw, encoding="utf-8")
            self.assertEqual(load_app_config(self.path).emoticon_tooltip, (), raw)

    def test_tooltip_invalid_falls_back_to_default(self):
        for raw in ('{"emoticon_tooltip": "text"}',
                    '{"emoticon_tooltip": 5}',
                    '{"emoticon_tooltip": ["typo"]}',
                    '{"emoticon_tooltip": {"text": "true"}}',
                    '{"emoticon_tooltip": {"id": 1}}'):
            self.path.write_text(raw, encoding="utf-8")
            self.assertEqual(load_app_config(self.path).emoticon_tooltip,
                             DEFAULT_EMOTICON_TOOLTIP, raw)
    # 注：不断言仓库里的 config.json 本体——它是供用户编辑的开关文件，
    # 用户开启写操作后该断言会误报。代码层默认值由 test_default_is_write_disabled 保证。


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


class BrowserCookieHelperTests(unittest.TestCase):
    """浏览器 Cookie 提取的纯函数（不读真实浏览器数据）。"""

    def test_is_bilibili_host(self):
        from blive_sc_get.browser_cookie import is_bilibili_host
        self.assertTrue(is_bilibili_host(".bilibili.com"))
        self.assertTrue(is_bilibili_host("bilibili.com"))
        self.assertTrue(is_bilibili_host("live.bilibili.com"))
        self.assertFalse(is_bilibili_host("evil-bilibili.com"))
        self.assertFalse(is_bilibili_host("bilibili.com.evil.com"))
        self.assertFalse(is_bilibili_host(""))

    def test_build_cookie_string(self):
        from blive_sc_get.browser_cookie import build_cookie_string
        self.assertEqual(build_cookie_string([("a", "1"), ("b", "2")]), "a=1; b=2")
        self.assertEqual(build_cookie_string([]), "")

    def test_decrypt_chromium_v10_roundtrip(self):
        import os
        from blive_sc_get.browser_cookie import decrypt_chromium_value
        from Crypto.Cipher import AES
        key = os.urandom(32)
        nonce = os.urandom(12)
        plain = "SESSDATA=abc%2Cdef"
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plain.encode("utf-8"))
        encrypted = b"v10" + nonce + ciphertext + tag
        self.assertEqual(decrypt_chromium_value(encrypted, key), plain)

    def test_decrypt_chromium_app_bound_unsupported(self):
        from blive_sc_get.browser_cookie import decrypt_chromium_value
        self.assertIsNone(decrypt_chromium_value(b"v20" + b"\x00" * 40, b"\x00" * 32))
        self.assertIsNone(decrypt_chromium_value(b"app_bound" + b"\x00" * 40, b"\x00" * 32))
    """悬浮窗堆叠位置的纯函数计算（不创建窗口）。"""

class ToastGeometryTests(unittest.TestCase):
    """悬浮窗堆叠位置的纯函数计算（不创建窗口）。"""

    def _parse(self, geom: str):
        size, _, pos = geom.partition("+")
        w, h = size.split("x")
        x, y = pos.split("+")
        return int(w), int(h), int(x), int(y)

    def test_first_toast_sits_above_taskbar(self):
        w, h, x, y = self._parse(toast_geometry(0, 1920, 1080, 60))
        self.assertEqual(x, 1920 - 320 - 12)          # 右缘留边距
        self.assertEqual(y, 1080 - 48 - 12 - 60)      # 任务栏之上

    def test_stack_goes_upward_with_spacing(self):
        _, _, _, y0 = self._parse(toast_geometry(0, 1920, 1080, 60))
        _, _, _, y1 = self._parse(toast_geometry(1, 1920, 1080, 60))
        self.assertEqual(y0 - y1, 60 + 8)             # 一个窗高 + 一个间距

    def test_geometry_uses_fixed_width(self):
        w, _h, _x, _y = self._parse(toast_geometry(0, 1920, 1080, 60))
        self.assertEqual(w, 320)


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


class TextScrolledToBottomTests(unittest.TestCase):
    """根据 yview() 判断是否位于底部：决定追加新内容后是否跟随滚动。"""

    def test_at_bottom(self):
        self.assertTrue(text_scrolled_to_bottom((0.5, 1.0)))

    def test_scrolled_up(self):
        self.assertFalse(text_scrolled_to_bottom((0.0, 0.4)))
        self.assertFalse(text_scrolled_to_bottom((0.9, 0.99)))

    def test_short_content_without_scrollbar(self):
        # 内容不足一屏时 yview() 为 (0.0, 1.0)，应视为在底部
        self.assertTrue(text_scrolled_to_bottom((0.0, 1.0)))

    def test_within_tolerance(self):
        # 浮点误差内（距底 0.05%）仍视为在底部
        self.assertTrue(text_scrolled_to_bottom((0.9, 0.9995)))

    def test_malformed_input_defaults_to_follow(self):
        self.assertTrue(text_scrolled_to_bottom(None))
        self.assertTrue(text_scrolled_to_bottom(()))


class SendDanmakuApiTests(unittest.TestCase):
    """发送弹幕接口：csrf 提取、表单参数、前置拦截与错误映射（离线）。"""

    COOKIE = "SESSDATA=abc; bili_jct=csrf123; DedeUserID=1"

    def _api(self, payload=None, cookie=COOKIE):
        session = _FakeSession({"code": 0} if payload is None else payload)
        return BilibiliLiveAPI(session, cookie=cookie), session

    def test_csrf_extracted_from_cookie(self):
        api, _ = self._api()
        self.assertEqual(api.csrf, "csrf123")
        self.assertEqual(BilibiliLiveAPI(_FakeSession({}), cookie="SESSDATA=x").csrf, "")
        self.assertEqual(BilibiliLiveAPI(_FakeSession({})).csrf, "")

    def test_form_params_include_required_and_reply(self):
        api, session = self._api()
        asyncio.run(api.send_danmaku(22625025, "加油", mode=5, color=16711680,
                                     fontsize=30, reply_mid=888, reply_uname="张三",
                                     replay_dmid="627348750013235456"))
        url, data, _headers = session.post_calls[0]
        self.assertIn("/msg/send", url)
        self.assertEqual(data["roomid"], 22625025)
        self.assertEqual(data["msg"], "加油")
        self.assertEqual(data["csrf"], "csrf123")
        self.assertEqual(data["csrf_token"], "csrf123")
        self.assertEqual(data["fontsize"], 30)
        self.assertEqual(data["color"], 16711680)
        self.assertEqual(data["mode"], 5)
        self.assertIsInstance(data["rnd"], int)
        self.assertEqual(data["reply_mid"], 888)
        self.assertEqual(data["reply_uname"], "张三")
        self.assertEqual(data["replay_dmid"], "627348750013235456")

    def test_reply_fields_omitted_by_default(self):
        api, session = self._api()
        asyncio.run(api.send_danmaku(1, "hi"))
        _url, data, _headers = session.post_calls[0]
        for key in ("reply_mid", "reply_uname", "replay_dmid"):
            self.assertNotIn(key, data)

    def test_local_precheck_without_cookie(self):
        api = BilibiliLiveAPI(_FakeSession({}), cookie="")
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(api.send_danmaku(1, "hi"))
        self.assertEqual(ctx.exception.code, -101)
        self.assertEqual(len(api.session.post_calls), 0)  # 前置拦截，不发请求

    def test_local_precheck_without_csrf(self):
        api = BilibiliLiveAPI(_FakeSession({}), cookie="SESSDATA=x")
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(api.send_danmaku(1, "hi"))
        self.assertEqual(ctx.exception.code, -111)

    def test_server_error_code_raises_with_hint(self):
        api, _ = self._api(payload={"code": 10031, "message": "太快了"})
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(api.send_danmaku(1, "hi"))
        self.assertEqual(ctx.exception.code, 10031)
        self.assertIn("频率", describe_send_error(ctx.exception.code, ctx.exception.message))

    def test_describe_send_error_mapping_and_fallback(self):
        self.assertIn("未登录", describe_send_error(-101))
        self.assertIn("csrf", describe_send_error(-111))
        self.assertIn("长度", describe_send_error(1003212))
        self.assertIn("风控", describe_send_error(-352))
        self.assertIn("boom", describe_send_error(9999, "boom"))
        self.assertEqual(describe_send_error("weird"), "发送失败（code=weird）")

    def test_get_dm_config_failure_returns_empty(self):
        api, _ = self._api(payload={"code": -101, "message": "未登录"})
        self.assertEqual(asyncio.run(api.get_dm_config(1)), {})


class DanmakuSendGuardTests(unittest.TestCase):
    """发送弹幕的客户端校验（纯函数）：长度 / 冷却 / 重复内容。"""

    def test_allows_normal_text(self):
        self.assertIsNone(danmaku_send_guard("你好", last_text="", last_time=0.0,
                                             now=10.0))

    def test_blocks_empty(self):
        self.assertIn("不能为空", danmaku_send_guard("   "))

    def test_blocks_too_long(self):
        self.assertIn("过长", danmaku_send_guard("啊" * 21))

    def test_max_len_none_skips_length_check(self):
        # 表情包触发词由服务端定义，不受 20 字输入上限约束
        self.assertIsNone(danmaku_send_guard("啊" * 60, max_len=None))
        self.assertIn("过长", danmaku_send_guard("啊" * 60))

    def test_blocks_within_cooldown(self):
        msg = danmaku_send_guard("新内容", last_text="旧内容", last_time=10.0, now=11.0)
        self.assertIn("频繁", msg)
        self.assertIsNone(danmaku_send_guard("新内容", last_text="旧内容",
                                             last_time=10.0, now=12.5))

    def test_blocks_duplicate_within_window(self):
        msg = danmaku_send_guard("一样", last_text="一样", last_time=10.0, now=20.0)
        self.assertIn("相同", msg)
        # 超过重复窗口后可再次发送
        self.assertIsNone(danmaku_send_guard("一样", last_text="一样",
                                             last_time=10.0, now=45.0))


class SelectDmOptionsTests(unittest.TestCase):
    """弹幕颜色/模式候选：以服务端可用项为准，仅查询失败时回退内置预设。"""

    PRESETS = (("白色", 16777215), ("红色", 16711680), ("蓝色", 255))

    def test_uses_offered_as_is(self):
        offered = [("白色", 16777215), ("蓝色", 255)]
        self.assertEqual(select_dm_options(self.PRESETS, offered), offered)

    def test_single_offered_item_is_kept(self):
        # 账号只支持 1 项时必须如实展示：给出不支持的颜色只会发送失败
        self.assertEqual(select_dm_options(self.PRESETS, [("白色", 16777215)]),
                         [("白色", 16777215)])

    def test_falls_back_to_presets_when_empty(self):
        # 查询失败/无数据时回退预设，避免下拉为空
        self.assertEqual(select_dm_options(self.PRESETS, []), list(self.PRESETS))

    def test_dedups_offered_by_name(self):
        offered = [("白色", 16777215), ("白色", 16777215), ("蓝色", 255)]
        self.assertEqual(select_dm_options(self.PRESETS, offered),
                         [("白色", 16777215), ("蓝色", 255)])


class UnseenBadgeTests(unittest.TestCase):
    """新消息浮动徽标文案（纯函数）：无新消息时返回空串表示隐藏。"""

    def test_no_unseen_hides_badge(self):
        self.assertEqual(unseen_badge_text(0, "弹幕"), "")
        self.assertEqual(unseen_badge_text(-3, "SC"), "")
        self.assertEqual(unseen_badge_text(None, "SC"), "")
        self.assertEqual(unseen_badge_text("abc", "SC"), "")

    def test_text_for_unseen(self):
        self.assertEqual(unseen_badge_text(5, "弹幕"), "5 条新弹幕 ↓")
        self.assertEqual(unseen_badge_text("7", "SC"), "7 条新SC ↓")


class DanmakuContentFromLineTests(unittest.TestCase):
    """从弹幕整行文本还原正文：剥离「[时间] 」与「用户名：」前缀。"""

    def test_strips_time_and_username(self):
        self.assertEqual(danmaku_content_from_line("[20:15:30] 弹幕哥：你好世界"), "你好世界")

    def test_content_containing_colon_is_kept(self):
        self.assertEqual(danmaku_content_from_line("[20:15:30] 张三：a：b"), "a：b")

    def test_emoticon_display_text(self):
        self.assertEqual(danmaku_content_from_line("[20:15:30] 弹幕哥：[百岁山]"), "[百岁山]")

    def test_without_prefix_returns_as_is(self):
        self.assertEqual(danmaku_content_from_line("裸文本"), "裸文本")

    def test_empty_and_none(self):
        self.assertEqual(danmaku_content_from_line(""), "")
        self.assertEqual(danmaku_content_from_line(None), "")


class ReconnectDelayTests(unittest.TestCase):
    """断线重连退避：指数增长、封顶、随机抖动上下界。"""

    def test_grows_exponentially_and_caps(self):
        delays = [compute_reconnect_delay(i, jitter=0.0) for i in range(7)]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0])

    def test_negative_attempt_treated_as_first(self):
        self.assertEqual(compute_reconnect_delay(-5, jitter=0.0), 1.0)

    def test_jitter_stays_within_bounds(self):
        for ratio in (0.0, 0.5, 1.0):
            delay = compute_reconnect_delay(1, rng=lambda r=ratio: r)
            self.assertGreaterEqual(delay, 1.4)
            self.assertLessEqual(delay, 2.6)

    def test_never_below_floor_or_above_cap(self):
        for attempt in range(12):
            lowest = compute_reconnect_delay(attempt, rng=lambda: 0.0)
            highest = compute_reconnect_delay(attempt, rng=lambda: 1.0)
            self.assertGreaterEqual(lowest, 0.1, attempt)
            self.assertLessEqual(highest, RECONNECT_MAX_DELAY, attempt)


EMOTICON_PAYLOAD = {
    "code": 0,
    "data": {
        "data": [
            {"pkg_name": "官方表情", "pkg_id": 1, "pkg_type": 1, "emoticons": [
                {"emoticon_unique": "official_331", "emoticon_id": 331, "emoji": "妙",
                 "url": "https://i0.hdslb.com/a.png", "width": 132, "height": 60,
                 "bulge_display": 1, "perm": 1},
                {"emoticon_unique": "official_332", "emoticon_id": 332, "emoji": "冲",
                 "url": "https://i0.hdslb.com/b.png", "perm": 0},
            ]},
            {"pkg_name": "房间专属", "pkg_id": 99, "pkg_type": 2, "emoticons": [
                {"emoticon_unique": "room_9527_1", "emoticon_id": 109824, "emoji": "百岁山",
                 "url": "https://i0.hdslb.com/c.png", "width": 162, "height": 60},
            ]},
            {"pkg_name": "空表情包", "pkg_id": 100, "emoticons": []},
        ]
    },
}


class ParseRoomEmoticonPackagesTests(unittest.TestCase):
    """GetEmoticons 归一化为表情包：保持服务端分页顺序、过滤不可用项、剔除空包。"""

    def test_packages_keep_server_order_and_drop_empty(self):
        packages = parse_room_emoticon_packages(EMOTICON_PAYLOAD["data"])
        # 顺序与服务端一致（与直播间内表情面板的分页一致）；无可用表情的包被剔除
        self.assertEqual([p["name"] for p in packages], ["官方表情", "房间专属"])
        self.assertEqual(packages[0]["id"], 1)
        self.assertEqual(packages[1]["type"], 2)

    def test_unavailable_emoticon_filtered_out(self):
        packages = parse_room_emoticon_packages(EMOTICON_PAYLOAD["data"])
        first = packages[0]["emoticons"]
        self.assertEqual([e["unique"] for e in first], ["official_331"])
        self.assertEqual(first[0]["trigger"], "妙")
        self.assertEqual(first[0]["text"], "[妙]")
        self.assertEqual(first[0]["url"], "https://i0.hdslb.com/a.png")
        self.assertEqual(first[0]["width"], 132)
        self.assertEqual(first[0]["bulge_display"], 1)

    def test_room_exclusive_emoticon_normalized(self):
        packages = parse_room_emoticon_packages(EMOTICON_PAYLOAD["data"])
        emo = packages[1]["emoticons"][0]
        self.assertEqual(emo["unique"], "room_9527_1")
        self.assertEqual(emo["trigger"], "百岁山")
        self.assertEqual(emo["text"], "[百岁山]")
        self.assertEqual(emo["id"], 109824)

    def test_flat_emoticon_entries_become_single_package(self):
        # 兼容 data 下直接就是表情项（未套 emoticons 列表）的形态
        packages = parse_room_emoticon_packages({"data": [
            {"emoticon_unique": "official_1", "emoji": "[已带括号]", "url": "u"},
            {"emoticon_unique": "official_2", "emoji": "打call", "url": "v"}]})
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["name"], "全部表情")
        self.assertEqual([e["text"] for e in packages[0]["emoticons"]],
                         ["[已带括号]", "[打call]"])

    def test_dedup_across_packages_and_missing_fields_skipped(self):
        packages = parse_room_emoticon_packages({"data": [
            {"pkg_name": "A", "emoticons": [
                {"emoticon_unique": "x", "emoji": "a", "url": "u"},
                {"emoticon_unique": "x", "emoji": "a", "url": "u"},
                {"emoticon_unique": "no-url", "emoji": "b"},
                "not-a-dict"]},
            {"pkg_name": "B", "emoticons": [
                {"emoticon_unique": "x", "emoji": "a", "url": "u"},
                {"emoticon_unique": "y", "emoji": "c", "url": "w"}]},
        ]})
        self.assertEqual([p["name"] for p in packages], ["A", "B"])
        self.assertEqual([e["unique"] for e in packages[0]["emoticons"]], ["x"])
        self.assertEqual([e["unique"] for e in packages[1]["emoticons"]], ["y"])

    def test_malformed_input_returns_empty(self):
        for bad in (None, [], {}, {"data": None}, {"data": "x"}, {"data": [1, 2]}):
            self.assertEqual(parse_room_emoticon_packages(bad), [])


class GetRoomEmoticonsApiTests(unittest.TestCase):
    """表情接口：请求参数与响应解析；失败一律返回空列表（不阻塞界面）。"""

    def test_success(self):
        session = _FakeSession(EMOTICON_PAYLOAD)
        api = BilibiliLiveAPI(session)
        packages = asyncio.run(api.get_room_emoticons(9527))
        self.assertEqual([p["name"] for p in packages], ["官方表情", "房间专属"])
        url, params = session.calls[0]
        self.assertIn("GetEmoticons", url)
        self.assertEqual(params, {"platform": "pc", "room_id": 9527})

    def test_api_error_returns_empty(self):
        api = BilibiliLiveAPI(_FakeSession({"code": -101, "message": "未登录"}))
        self.assertEqual(asyncio.run(api.get_room_emoticons(1)), [])


class SendEmoticonTests(unittest.TestCase):
    """发送表情包弹幕：msg 必须传 emoticon_unique（传触发词会被服务端拒绝）。"""

    COOKIE = "SESSDATA=abc; bili_jct=csrf123"
    EMOTICON = {"unique": "room_9527_1", "id": 109824, "trigger": "百岁山",
                "text": "[百岁山]", "url": "https://i0.hdslb.com/c.png",
                "width": 162, "height": 60, "bulge_display": 1}

    def test_emoticon_uses_unique_as_msg(self):
        session = _FakeSession({"code": 0})
        api = BilibiliLiveAPI(session, cookie=self.COOKIE)
        asyncio.run(api.send_danmaku(9527, "百岁山", emoticon=self.EMOTICON))
        _url, data, _headers = session.post_calls[0]
        # 关键回归：msg 传触发词会导致服务端返回 10203，必须传 emoticon_unique
        self.assertEqual(data["msg"], "room_9527_1")
        self.assertEqual(data["dm_type"], 1)
        options = __import__("json").loads(data["emoticonOptions"])
        self.assertEqual(options["emoticon_unique"], "room_9527_1")
        self.assertEqual(options["url"], "https://i0.hdslb.com/c.png")
        self.assertEqual(options["width"], 162)
        self.assertEqual(options["height"], 60)
        self.assertEqual(options["in_player_area"], 1)
        self.assertEqual(options["bulge_display"], 1)

    def test_emoticon_options_fall_back_to_default_size(self):
        emoticon = dict(self.EMOTICON, width=0, height=0, bulge_display=0)
        session = _FakeSession({"code": 0})
        api = BilibiliLiveAPI(session, cookie=self.COOKIE)
        asyncio.run(api.send_danmaku(9527, "百岁山", emoticon=emoticon))
        _url, data, _headers = session.post_calls[0]
        options = __import__("json").loads(data["emoticonOptions"])
        self.assertEqual(options["width"], 40)
        self.assertEqual(options["height"], 40)
        self.assertEqual(options["bulge_display"], 0)

    def test_plain_danmaku_keeps_text_and_has_no_emoticon_fields(self):
        session = _FakeSession({"code": 0})
        api = BilibiliLiveAPI(session, cookie=self.COOKIE)
        asyncio.run(api.send_danmaku(9527, "普通弹幕"))
        _url, data, _headers = session.post_calls[0]
        self.assertEqual(data["msg"], "普通弹幕")
        self.assertNotIn("dm_type", data)
        self.assertNotIn("emoticonOptions", data)

    def test_emoticon_without_unique_raises(self):
        api = BilibiliLiveAPI(_FakeSession({"code": 0}), cookie=self.COOKIE)
        with self.assertRaises(ApiError):
            asyncio.run(api.send_danmaku(1, "x", emoticon={"text": "[x]"}))
        self.assertEqual(len(api.session.post_calls), 0)

    def test_error_code_10203_has_hint(self):
        self.assertIn("表情包", describe_send_error(10203))


class FitEmoticonScaleTests(unittest.TestCase):
    """表情缩放：正常表情按**原始大小 1:1** 显示，只有超大图才等比缩小。"""

    def test_real_emoticons_keep_natural_size(self):
        # 直播间表情（含 132/162/231 宽的「大表情」）都是 60px 高的小图
        for size in ((132, 60), (162, 60), (231, 60), (60, 60), (40, 40)):
            self.assertEqual(fit_emoticon_scale(*size), (1, 1), size)
            self.assertEqual(emoticon_display_size(*size), size, size)

    def test_taller_than_row_is_shrunk(self):
        # 高于行高上限的图片才缩小，保证能完整放进固定高度的表情条
        width, height = emoticon_display_size(66, 66)
        self.assertLessEqual(width, EMOTICON_ICON_MAX_WIDTH)
        self.assertLessEqual(height, EMOTICON_ICON_MAX_HEIGHT)
        self.assertGreater(height, 40)  # 不能压得过小

    def test_oversized_image_is_shrunk_not_crushed(self):
        width, height = emoticon_display_size(300, 300)
        self.assertLessEqual(width, EMOTICON_ICON_MAX_WIDTH * 1.05)
        self.assertLessEqual(height, EMOTICON_ICON_MAX_HEIGHT * 1.05)
        self.assertGreater(width, 40)  # 不能像旧实现那样压得过小

    def test_huge_image_falls_back_to_integer_subsample(self):
        width, height = emoticon_display_size(1000, 1000)
        self.assertLessEqual(width, EMOTICON_ICON_MAX_WIDTH * 1.05)
        self.assertLessEqual(height, EMOTICON_ICON_MAX_HEIGHT * 1.05)
        self.assertGreater(width, 40)

    def test_result_never_exceeds_limits_or_upscales(self):
        for size in ((132, 60), (162, 60), (231, 60), (300, 300), (1000, 1000),
                     (600, 100), (50, 400)):
            out_w, out_h = emoticon_display_size(*size)
            self.assertLessEqual(out_w, EMOTICON_ICON_MAX_WIDTH * 1.05, size)
            self.assertLessEqual(out_h, EMOTICON_ICON_MAX_HEIGHT * 1.05, size)
            self.assertLessEqual(out_w, size[0], size)
            self.assertLessEqual(out_h, size[1], size)

    def test_invalid_metadata(self):
        for bad in ((0, 0), (None, None), ("x", "y"), (-10, 60)):
            self.assertEqual(fit_emoticon_scale(*bad), (1, 1))
        self.assertEqual(emoticon_display_size(0, 60), (0, 0))


class EmoticonTooltipTextTests(unittest.TestCase):
    """悬浮提示文案：按 config.json 的字段开关拼接，默认仅触发词。"""

    INFO = {"text": "[百岁山]", "trigger": "百岁山",
            "unique": "room_9527_109824", "id": 109824}

    def test_default_only_trigger_text(self):
        self.assertEqual(emoticon_tooltip_text(self.INFO, DEFAULT_EMOTICON_TOOLTIP),
                         "[百岁山]")

    def test_all_fields_joined_in_order(self):
        self.assertEqual(
            emoticon_tooltip_text(self.INFO, ("text", "unique", "id")),
            "[百岁山] · unique=room_9527_109824 · id=109824")

    def test_subset_respects_given_order(self):
        # 顺序由 fields 决定；config.json 里写乱顺序会在解析阶段归正（见 AppConfigTests）
        self.assertEqual(emoticon_tooltip_text(self.INFO, ("unique", "id")),
                         "unique=room_9527_109824 · id=109824")
        self.assertEqual(emoticon_tooltip_text(self.INFO, ("id",)),
                         "id=109824")

    def test_missing_values_are_skipped(self):
        self.assertEqual(emoticon_tooltip_text({"text": "[x]"}, ("text", "unique", "id")),
                         "[x]")
        self.assertEqual(emoticon_tooltip_text({"unique": "room_1"}, ("text", "unique")),
                         "unique=room_1")

    def test_trigger_fallback_and_zero_id(self):
        self.assertEqual(emoticon_tooltip_text({"trigger": "打call"}, ("text",)), "打call")
        self.assertEqual(emoticon_tooltip_text({"id": 0}, ("id",)), "")

    def test_empty_fields_or_info_yields_empty(self):
        self.assertEqual(emoticon_tooltip_text(self.INFO, ()), "")
        self.assertEqual(emoticon_tooltip_text(self.INFO, None), "")
        self.assertEqual(emoticon_tooltip_text({}, DEFAULT_EMOTICON_TOOLTIP), "")


class EmoticonIdByUniqueTests(unittest.TestCase):
    """按 unique 从已加载的表情包里补数字 id（弹幕报文只有 unique）。"""

    PACKAGES = [{"name": "官方", "id": 1, "emoticons": [
        {"unique": "official_331", "id": 331},
        {"unique": "room_9527_1", "id": 109824}]}]

    def test_found(self):
        self.assertEqual(emoticon_id_by_unique(self.PACKAGES, "room_9527_1"), 109824)
        self.assertEqual(emoticon_id_by_unique(self.PACKAGES, "official_331"), 331)

    def test_not_found_or_bad_input(self):
        self.assertEqual(emoticon_id_by_unique(self.PACKAGES, "nope"), 0)
        self.assertEqual(emoticon_id_by_unique(self.PACKAGES, ""), 0)
        self.assertEqual(emoticon_id_by_unique(None, "room_9527_1"), 0)
        self.assertEqual(emoticon_id_by_unique([{"emoticons": "bad"}, 5], "x"), 0)

    def test_bad_id_value(self):
        self.assertEqual(emoticon_id_by_unique(
            [{"emoticons": [{"unique": "u", "id": "x"}]}], "u"), 0)


class TooltipPositionTests(unittest.TestCase):
    """悬浮提示定位（纯函数）：光标右下方，越界回缩且不出屏。"""

    def test_offset_from_cursor(self):
        self.assertEqual(tooltip_position(100, 200, 80, 20, 1920, 1080), "+116+220")

    def test_clamped_to_screen(self):
        self.assertEqual(tooltip_position(1900, 1070, 200, 40, 1920, 1080),
                         "+1712+1032")

    def test_never_negative(self):
        self.assertEqual(tooltip_position(-50, -50, 100, 20, 1920, 1080), "+0+0")


class EmoticonMemoryTests(unittest.TestCase):
    """表情包记忆持久化到 gui_rooms.json：重启后仍回到上次浏览的表情包。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "gui_rooms.json"

    def test_roundtrip_with_rooms_and_ui(self):
        memory = {1: {"index": 2, "name": "房间专属"}, 2: {"index": 0, "name": ""}}
        save_room_entries(self.path, [RoomEntry(1), RoomEntry(2)],
                          ui={"sort_mode": "manual", "pin_live": True},
                          emoticon=memory)
        self.assertEqual(load_emoticon_memory(self.path), memory)
        # 房间列表与界面偏好不受影响
        self.assertEqual(load_room_entries(self.path), [RoomEntry(1), RoomEntry(2)])
        prefs = load_ui_prefs(self.path)
        self.assertEqual(prefs["sort_mode"], "manual")
        self.assertTrue(prefs["pin_live"])

    def test_other_saves_do_not_drop_memory(self):
        memory = {7: {"index": 3, "name": "粉丝团"}}
        save_room_entries(self.path, [RoomEntry(7)], ui={}, emoticon=memory)
        # 不传 emoticon 的其他保存动作（改备注、排序等）不应抹掉记忆
        save_room_entries(self.path, [RoomEntry(7, note="备注")],
                          ui={"pin_live": True})
        self.assertEqual(load_emoticon_memory(self.path), memory)
        self.assertEqual(load_room_entries(self.path)[0].note, "备注")

    def test_empty_memory_writes_no_key(self):
        save_room_entries(self.path, [RoomEntry(1)], ui={}, emoticon={})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("emoticon", data)
        self.assertEqual(load_emoticon_memory(self.path), {})

    def test_missing_and_malformed_input(self):
        self.assertEqual(load_emoticon_memory(self.tmp / "nope.json"), {})
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_emoticon_memory(self.path), {})
        self.path.write_text('{"emoticon": "x"}', encoding="utf-8")
        self.assertEqual(load_emoticon_memory(self.path), {})
        self.path.write_text(
            '{"emoticon": {"1": {"index": "2"}, "bad": {"index": 1},'
            ' "3": 5, "4": {"index": -1}, "5": {"name": "只有名字"}}}',
            encoding="utf-8")
        self.assertEqual(load_emoticon_memory(self.path), {
            1: {"index": 2, "name": ""},
            4: {"index": 0, "name": ""},
            5: {"index": 0, "name": "只有名字"},
        })


class EmoticonPackagesSignatureTests(unittest.TestCase):
    """表情包指纹：刷新结果与已有内容一致时不重绘（保住横向滚动位置与当前包）。"""

    BASE = [{"id": 1, "name": "官方表情", "emoticons": [{"unique": "a"}, {"unique": "b"}]}]

    def test_same_content_same_signature(self):
        clone = [{"id": 1, "name": "官方表情",
                  "emoticons": [{"unique": "a"}, {"unique": "b"}]}]
        self.assertEqual(emoticon_packages_signature(self.BASE),
                         emoticon_packages_signature(clone))

    def test_detects_newly_unlocked_package(self):
        # 粉丝灯牌升级解锁新表情包 → 包数量变化
        grown = self.BASE + [{"id": 2, "name": "粉丝团", "emoticons": [{"unique": "c"}]}]
        self.assertNotEqual(emoticon_packages_signature(self.BASE),
                            emoticon_packages_signature(grown))

    def test_detects_new_emoticon_inside_package(self):
        grown = [{"id": 1, "name": "官方表情",
                  "emoticons": [{"unique": "a"}, {"unique": "b"}, {"unique": "c"}]}]
        self.assertNotEqual(emoticon_packages_signature(self.BASE),
                            emoticon_packages_signature(grown))

    def test_malformed_input(self):
        empty = emoticon_packages_signature([])
        self.assertEqual(emoticon_packages_signature(None), empty)
        self.assertEqual(emoticon_packages_signature(["bad", 1]), empty)
        self.assertEqual(emoticon_packages_signature([{"name": "x"}]),
                         ((None, "x", ()),))


if __name__ == "__main__":
    unittest.main()
