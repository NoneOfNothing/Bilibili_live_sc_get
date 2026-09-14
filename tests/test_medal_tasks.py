"""粉丝牌任务：纯函数解析、接口封装与配置解析的单元测试（离线，不依赖网络）。"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from blive_sc_get.api import (
    LIKE_INTERACT_URL,
    LIKE_REPORT_URL,
    MEDAL_TASK_URL,
    ApiError,
    BilibiliLiveAPI,
    describe_like_error,
)
from blive_sc_get.app_config import (
    DEFAULT_MEDAL_DANMAKU_INTERVAL,
    DEFAULT_MEDAL_LIKE_INTERVAL,
    DEFAULT_MEDAL_MAX_RETRY,
    AppConfig,
    load_app_config,
)
from blive_sc_get.gui_config import RoomEntry, load_room_entries, save_room_entries
from blive_sc_get.medal_runner import MedalTaskRunner
from blive_sc_get.medal_tasks import (
    TASK_LIKE,
    TASK_SEND_DANMAKU,
    TASK_WATCH_LIVE,
    compute_action_delay,
    dedupe_medals,
    find_task,
    is_task_applicable,
    is_task_complete,
    medal_level_for_room,
    next_fallback_text,
    normalize_medal,
    normalize_task,
    parse_medal_panel,
    parse_task_info_list,
    parse_task_progress,
    parse_title_count,
    pending_write_tasks,
    room_exclusive_emoticons,
    select_emoticon_cycle,
    should_auto_danmaku,
    task_label,
)

COOKIE = "SESSDATA=abc; bili_jct=csrf123; DedeUserID=1"

# 对齐真实响应（实测）：sub_title 形如「每日上限 1/10」。
TASK_INFO_DATA = {
    "free_intimacy": 0,
    "reach_free_intimacy_limit": False,
    "task_info": [
        {"jump_type": "feedLight", "title": "投喂粉丝灯牌",
         "sub_title": "每日上限 0/1", "is_done": False},
        {"jump_type": "watchLive", "title": "观看直播满15分钟",
         "sub_title": "每日上限 1/10", "is_done": False},
        {"jump_type": "sendGift", "title": "投喂礼物",
         "sub_title": "+1亲密度/电池", "is_done": False},
        {"jump_type": "sendDanmu", "title": "发弹幕",
         "sub_title": "每日上限 10/10", "is_done": True},
        {"jump_type": "like", "title": "点赞30次",
         "sub_title": "每日上限 10/10", "is_done": True},
    ],
}

MEDAL_PANEL_DATA = {
    "special_list": [
        {"medal": {"medal_id": 1, "medal_name": "枼绿素", "level": 13,
                   "target_id": 1891335475},
         "room_info": {"room_id": 1727071052, "living_status": 1},
         "anchor_info": {"nick_name": "枼绿素"}},
    ],
    "list": [
        {"medal": {"medal_id": 2, "medal_name": "大母鹅", "level": 28,
                   "target_id": 433351, "intimacy": 25, "next_intimacy": 99,
                   "today_feed": 22},
         "room_info": {"room_id": 5050, "living_status": 0},
         "anchor_info": {"nick_name": "大母鹅"}},
    ],
    "page_info": {"total_page": 1, "cur_page": 1},
}


class _FakeResp:
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
    """鸭子类型会话：按调用顺序返回预设的 GET/POST 响应。"""

    def __init__(self, get_payloads=None, post_payloads=None):
        self._get = list(get_payloads or [])
        self._post = list(post_payloads or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params))
        payload = self._get.pop(0) if self._get else {"code": 0}
        return _FakeResp(payload)

    def post(self, url, params=None, data=None, headers=None):
        self.post_calls.append((url, params, data))
        payload = self._post.pop(0) if self._post else {"code": 0}
        return _FakeResp(payload)


class TaskParsingTests(unittest.TestCase):
    def test_parse_task_progress(self):
        self.assertEqual(parse_task_progress("每日上限 1/10"), (1, 10))
        self.assertEqual(parse_task_progress("3/30"), (3, 30))
        self.assertEqual(parse_task_progress("0 / 1"), (0, 1))
        for bad in ("+1亲密度/电池", "", None, 5, "无进度"):
            self.assertEqual(parse_task_progress(bad), (0, 0))

    def test_parse_title_count(self):
        self.assertEqual(parse_title_count("点赞30次"), 30)
        self.assertEqual(parse_title_count("点赞 30 次"), 30)
        self.assertIsNone(parse_title_count("发弹幕"))
        self.assertIsNone(parse_title_count(None))

    def test_task_label(self):
        self.assertEqual(task_label(TASK_LIKE), "点赞")
        self.assertEqual(task_label(TASK_SEND_DANMAKU), "发弹幕")
        self.assertEqual(task_label("unknown"), "unknown")

    def test_parse_task_info_list(self):
        tasks = parse_task_info_list(TASK_INFO_DATA)
        self.assertEqual(len(tasks), 5)
        watch = find_task(tasks, TASK_WATCH_LIVE)
        self.assertEqual((watch["current"], watch["limit"], watch["is_done"]),
                         (1, 10, False))
        like = find_task(tasks, TASK_LIKE)
        self.assertEqual(like["title"], "点赞30次")
        self.assertTrue(is_task_complete(like))

    def test_parse_task_info_list_bad_input(self):
        for bad in (None, [], {}, {"task_info": None}, {"task_info": "x"}):
            self.assertEqual(parse_task_info_list(bad), [])

    def test_normalize_task_bad_item(self):
        self.assertIsNone(normalize_task("x"))
        self.assertEqual(normalize_task({"jump_type": "like"})["limit"], 0)

    def test_is_task_complete(self):
        self.assertTrue(is_task_complete({"is_done": True, "limit": 0, "current": 0}))
        self.assertTrue(is_task_complete({"is_done": False, "limit": 10, "current": 10}))
        self.assertFalse(is_task_complete({"is_done": False, "limit": 10, "current": 9}))
        self.assertFalse(is_task_complete({"is_done": False, "limit": 0, "current": 0}))
        self.assertTrue(is_task_complete(None))  # 非任务项视为无需执行

    def test_pending_write_tasks_only_incomplete_writes(self):
        tasks = parse_task_info_list(TASK_INFO_DATA)
        # like/sendDanmu 均已完成 → 无待办；把 like 改为未完成再看
        self.assertEqual(pending_write_tasks(tasks), [])
        tasks[4]["is_done"] = False
        tasks[4]["current"] = 3
        pending = pending_write_tasks(tasks)
        self.assertEqual([t["jump_type"] for t in pending], [TASK_LIKE])
        # watchLive / feedLight / sendGift 不属于写任务，永不出现在待办里
        self.assertNotIn(TASK_WATCH_LIVE, [t["jump_type"] for t in pending])

    def test_is_task_applicable(self):
        self.assertTrue(is_task_applicable(
            {"limit": 10, "current": 0, "is_done": False}))
        self.assertFalse(is_task_applicable(
            {"limit": 10, "current": 10, "is_done": False}))
        # limit 为 0 且未完成 = 「仅点亮」等不适用任务
        self.assertFalse(is_task_applicable(
            {"limit": 0, "current": 0, "is_done": False}))
        self.assertFalse(is_task_applicable(
            {"limit": 0, "current": 0, "is_done": True}))
        self.assertFalse(is_task_applicable(None))

    def test_should_auto_danmaku(self):
        # 未开播：总是执行
        self.assertTrue(should_auto_danmaku(0, False))
        self.assertTrue(should_auto_danmaku(0, True))
        # 开播：默认不执行，需显式开启「开播时也自动发弹幕」
        self.assertFalse(should_auto_danmaku(1, False))
        self.assertTrue(should_auto_danmaku(1, True))

    def test_pending_write_tasks_skips_not_lit(self):
        # 未点亮（sub_title「仅点亮」）解析出的 limit 为 0，不应计入待办
        tasks = [{"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
                  "limit": 0, "is_done": False, "raw": {}},
                 {"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕10次",
                  "current": 0, "limit": 0, "is_done": False, "raw": {}}]
        self.assertEqual(pending_write_tasks(tasks), [])


class MedalParsingTests(unittest.TestCase):
    def test_normalize_medal_nested(self):
        medal = normalize_medal(MEDAL_PANEL_DATA["list"][0])
        self.assertEqual(medal["medal_id"], 2)
        self.assertEqual(medal["level"], 28)
        self.assertEqual(medal["room_id"], 5050)
        self.assertEqual(medal["target_id"], 433351)
        self.assertEqual(medal["anchor_name"], "大母鹅")
        self.assertEqual(medal["living_status"], 0)
        # 当前经验 / 升级所需 / 今日已获取
        self.assertEqual(medal["intimacy"], 25)
        self.assertEqual(medal["next_intimacy"], 99)
        self.assertEqual(medal["today_feed"], 22)

    def test_normalize_medal_flat_shape(self):
        medal = normalize_medal({"medal_id": 9, "medal_name": "y", "level": 3,
                                 "target_id": 8, "roomid": 200, "uname": "B",
                                 "today_feed": 5})
        self.assertEqual((medal["level"], medal["room_id"], medal["anchor_name"]),
                         (3, 200, "B"))
        self.assertEqual(medal["today_feed"], 5)

    def test_normalize_medal_requires_id(self):
        self.assertIsNone(normalize_medal({"level": 3}))
        self.assertIsNone(normalize_medal("x"))

    def test_parse_medal_panel_merges_special_and_list(self):
        medals = parse_medal_panel(MEDAL_PANEL_DATA)
        self.assertEqual([m["medal_id"] for m in medals], [1, 2])
        self.assertTrue(medals[0]["is_special"])
        self.assertFalse(medals[1]["is_special"])

    def test_dedupe_medals_keeps_first(self):
        medals = [{"medal_id": 1, "level": 1}, {"medal_id": 1, "level": 9},
                  {"medal_id": 2, "level": 2}, {"medal_id": 0}]
        self.assertEqual([m["medal_id"] for m in dedupe_medals(medals)], [1, 2])

    def test_medal_level_for_room(self):
        medals = parse_medal_panel(MEDAL_PANEL_DATA)
        self.assertEqual(medal_level_for_room(medals, room_id=5050), 28)
        self.assertEqual(medal_level_for_room(medals, target_id=1891335475), 13)
        self.assertEqual(medal_level_for_room(medals, room_id=999), 0)


class EmoticonSelectionTests(unittest.TestCase):
    PACKAGES = [
        {"name": "官方", "emoticons": [
            {"unique": "official_1", "url": "u1"},
            {"unique": "official_2", "url": "u2"}]},
        {"name": "房间专属", "emoticons": [
            {"unique": "room_9527_1", "url": "r1"},
            {"unique": "room_9527_2", "url": "r2"}]},
    ]

    def test_room_exclusive_only(self):
        emoticons = room_exclusive_emoticons(self.PACKAGES)
        self.assertEqual([e["unique"] for e in emoticons],
                         ["room_9527_1", "room_9527_2"])
        self.assertEqual(room_exclusive_emoticons(None), [])

    def test_select_emoticon_cycle_wraps(self):
        emoticons = room_exclusive_emoticons(self.PACKAGES)
        first, idx = select_emoticon_cycle(emoticons, 0)
        self.assertEqual(first["unique"], "room_9527_1")
        second, idx = select_emoticon_cycle(emoticons, idx)
        self.assertEqual(second["unique"], "room_9527_2")
        third, idx = select_emoticon_cycle(emoticons, idx)
        self.assertEqual(third["unique"], "room_9527_1")
        self.assertEqual(idx, 1)

    def test_select_emoticon_cycle_empty(self):
        self.assertEqual(select_emoticon_cycle([], 3), (None, 0))
        self.assertEqual(select_emoticon_cycle(None, 0), (None, 0))

    def test_next_fallback_text(self):
        self.assertEqual(next_fallback_text(""), "1")
        self.assertEqual(next_fallback_text("1"), "2")
        self.assertEqual(next_fallback_text("9"), "10")
        self.assertEqual(next_fallback_text("x"), "1")
        self.assertEqual(next_fallback_text(None), "1")

    def test_compute_action_delay_bounds_and_rng(self):
        self.assertEqual(compute_action_delay(6, 8, rng=lambda: 0.0), 6.0)
        self.assertEqual(compute_action_delay(6, 8, rng=lambda: 1.0), 8.0)
        self.assertEqual(compute_action_delay(6, 8, rng=lambda: 0.5), 7.0)
        # 上限小于下限时夹取到下限，避免出现负区间
        self.assertEqual(compute_action_delay(8, 6, rng=lambda: 0.0), 8.0)
        value = compute_action_delay(15, 20)
        self.assertGreaterEqual(value, 15.0)
        self.assertLessEqual(value, 20.0)


class MedalApiTests(unittest.TestCase):
    def _api(self, session, cookie=COOKIE):
        return BilibiliLiveAPI(session, cookie=cookie)

    def test_get_medals_paginates_and_dedupes(self):
        page1 = {"code": 0, "data": {
            "special_list": MEDAL_PANEL_DATA["special_list"],
            "list": MEDAL_PANEL_DATA["list"],
            "page_info": {"total_page": 2, "cur_page": 1}}}
        page2 = {"code": 0, "data": {
            "list": [MEDAL_PANEL_DATA["list"][0],
                     {"medal": {"medal_id": 3, "medal_name": "c", "level": 5,
                                "target_id": 3},
                      "room_info": {"room_id": 3, "living_status": 1}}],
            "page_info": {"total_page": 2, "cur_page": 2}}}
        session = _FakeSession(get_payloads=[page1, page2])
        medals = asyncio.run(self._api(session).get_medals())
        self.assertEqual([m["medal_id"] for m in medals], [1, 2, 3])
        self.assertEqual(session.get_calls[0][1], {"page": 1, "page_size": 10})
        self.assertEqual(session.get_calls[1][1], {"page": 2, "page_size": 10})

    def test_get_medals_without_cookie_raises(self):
        session = _FakeSession()
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(self._api(session, cookie="").get_medals())
        self.assertEqual(ctx.exception.code, -101)
        self.assertEqual(session.get_calls, [])

    def test_get_medals_api_error_raises(self):
        session = _FakeSession(get_payloads=[{"code": -352, "message": "风控"}])
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(self._api(session).get_medals())
        self.assertEqual(ctx.exception.code, -352)

    def test_get_medal_task_info_params_and_parse(self):
        session = _FakeSession(get_payloads=[{"code": 0, "data": TASK_INFO_DATA}])
        info = asyncio.run(self._api(session).get_medal_task_info(1891335475))
        url, params = session.get_calls[0]
        self.assertIn("GetActivatedMedalInfo", url)
        self.assertEqual(url, MEDAL_TASK_URL)
        self.assertEqual(params["csrf"], "csrf123")
        self.assertEqual(params["target_id"], 1891335475)
        self.assertTrue(params["web_location"])
        self.assertEqual(len(info["tasks"]), 5)
        self.assertFalse(info["reach_free_intimacy_limit"])

    def test_get_medal_task_info_without_csrf_raises(self):
        session = _FakeSession()
        api = self._api(session, cookie="SESSDATA=x")
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(api.get_medal_task_info(1))
        self.assertEqual(ctx.exception.code, -111)
        self.assertEqual(session.get_calls, [])

    def test_like_room_primary_success(self):
        # nav 失败（无 wbi_img）→ 不带签名继续；likeReportV3 返回 0
        session = _FakeSession(
            get_payloads=[{"code": 0, "data": {}}],
            post_payloads=[{"code": 0}])
        asyncio.run(self._api(session).like_room(5050, 433351, click_time=10))
        url, params, data = session.post_calls[0]
        self.assertEqual(url, LIKE_REPORT_URL)
        self.assertEqual(params["click_time"], 10)
        self.assertEqual(params["room_id"], 5050)
        self.assertEqual(params["anchor_id"], 433351)
        self.assertEqual(params["csrf"], "csrf123")
        self.assertIsNone(data)

    def test_like_room_falls_back_to_interact(self):
        session = _FakeSession(
            get_payloads=[{"code": 0, "data": {}}],
            post_payloads=[{"code": -400, "message": "参数错误"}, {"code": 0}])
        asyncio.run(self._api(session).like_room(5050, 433351))
        self.assertEqual(session.post_calls[0][0], LIKE_REPORT_URL)
        url, params, data = session.post_calls[1]
        self.assertEqual(url, LIKE_INTERACT_URL)
        self.assertEqual(data["roomid"], 5050)
        self.assertEqual(data["csrf"], "csrf123")

    def test_like_room_risk_does_not_fall_back(self):
        session = _FakeSession(
            get_payloads=[{"code": 0, "data": {}}],
            post_payloads=[{"code": -352, "message": "风控"}])
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(self._api(session).like_room(5050, 433351))
        self.assertEqual(ctx.exception.code, -352)
        self.assertEqual(len(session.post_calls), 1)  # 风控不重试、不换接口

    def test_like_room_prechecks(self):
        session = _FakeSession()
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(self._api(session, cookie="").like_room(1, 2))
        self.assertEqual(ctx.exception.code, -101)
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(self._api(session, cookie="SESSDATA=x").like_room(1, 2))
        self.assertEqual(ctx.exception.code, -111)
        self.assertEqual(session.post_calls, [])

    def test_describe_like_error(self):
        self.assertIn("未登录", describe_like_error(-101))
        self.assertIn("csrf", describe_like_error(-111))
        self.assertIn("风控", describe_like_error(-352))
        self.assertIn("boom", describe_like_error(9999, "boom"))
        self.assertEqual(describe_like_error("weird"), "点赞失败（code=weird）")


class MedalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "config.json"

    def test_defaults_disabled(self):
        cfg = load_app_config(self.tmp / "nope.json")
        self.assertFalse(cfg.auto_medal_tasks)
        self.assertEqual(cfg.medal_like_interval, DEFAULT_MEDAL_LIKE_INTERVAL)
        self.assertEqual(cfg.medal_danmaku_interval, DEFAULT_MEDAL_DANMAKU_INTERVAL)
        self.assertEqual(cfg.medal_max_retry, DEFAULT_MEDAL_MAX_RETRY)

    def test_parse_medal_section(self):
        self.path.write_text(json.dumps({
            "allow_write_operations": True,
            "medal_tasks": {"auto": True, "like_interval_sec": [3, 5],
                            "danmaku_interval_sec": [1, 2], "max_retry": 1},
        }), encoding="utf-8")
        cfg = load_app_config(self.path)
        self.assertTrue(cfg.auto_medal_tasks)
        self.assertEqual(cfg.medal_like_interval, (3.0, 5.0))
        self.assertEqual(cfg.medal_danmaku_interval, (1.0, 2.0))
        self.assertEqual(cfg.medal_max_retry, 1)

    def test_invalid_medal_section_falls_back(self):
        # auto 写字符串 / 间隔非法 / 条数非整数 → 一律回退默认（自动关闭）
        self.path.write_text(json.dumps({
            "medal_tasks": {"auto": "true", "like_interval_sec": [-1, 2],
                            "danmaku_interval_sec": 5, "max_retry": -3}}),
            encoding="utf-8")
        cfg = load_app_config(self.path)
        self.assertFalse(cfg.auto_medal_tasks)
        self.assertEqual(cfg.medal_like_interval, DEFAULT_MEDAL_LIKE_INTERVAL)
        self.assertEqual(cfg.medal_danmaku_interval, DEFAULT_MEDAL_DANMAKU_INTERVAL)
        self.assertEqual(cfg.medal_max_retry, DEFAULT_MEDAL_MAX_RETRY)


class RoomEntryAutoMedalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "gui_rooms.json"

    def test_roundtrip(self):
        entries = [RoomEntry(1, auto_like=True, auto_danmaku=True),
                   RoomEntry(2, auto_danmaku=True, auto_danmaku_when_live=True),
                   RoomEntry(3)]
        save_room_entries(self.path, entries)
        self.assertEqual(load_room_entries(self.path), entries)

    def test_legacy_auto_medal_migrates_to_both(self):
        # 旧配置只有一个 auto_medal_tasks（两项合在一起）→ 迁移为两项都开启
        self.path.write_text(
            '{"rooms": [{"room_id": 789, "auto_medal_tasks": true}]}',
            encoding="utf-8")
        entry = load_room_entries(self.path)[0]
        self.assertTrue(entry.auto_like)
        self.assertTrue(entry.auto_danmaku)

    def test_two_switches_independent_and_default_off(self):
        self.path.write_text(
            '{"rooms": [{"room_id": 789}, {"room_id": 790, "auto_like": true}]}',
            encoding="utf-8")
        first, second = load_room_entries(self.path)
        self.assertFalse(first.auto_like)
        self.assertFalse(first.auto_danmaku)
        self.assertFalse(first.auto_danmaku_when_live)  # 默认仅未开播时自动发弹幕
        self.assertTrue(second.auto_like)
        self.assertFalse(second.auto_danmaku)


class _StubApi:
    """执行引擎的离线桩：模拟接口按调用推进任务进度。"""

    def __init__(self, tasks, emoticons=None, like_step=None, danmaku_step=1):
        self.logged_in = True
        self.csrf = "csrf123"
        self.uid = 1
        self._tasks = {t["jump_type"]: t for t in tasks}
        self.emoticons = emoticons or []
        self._like_step = like_step          # None=按 click_time 计入（模拟服务端等量统计）
        self._danmaku_step = danmaku_step
        self.like_calls = []
        self.danmaku_calls = []

    async def get_medal_task_info(self, target_id):
        return {"target_id": target_id, "tasks": list(self._tasks.values()),
                "free_intimacy": 0, "reach_free_intimacy_limit": False}

    async def like_room(self, room_id, anchor_uid, click_time=1):
        self.like_calls.append(click_time)
        step = click_time if self._like_step is None else self._like_step
        task = self._tasks[TASK_LIKE]
        task["current"] = min(task["limit"], task["current"] + step)
        return {"code": 0}

    async def send_danmaku(self, room_id, msg, *, emoticon=None, **kwargs):
        self.danmaku_calls.append(msg)
        task = self._tasks[TASK_SEND_DANMAKU]
        task["current"] = min(task["limit"], task["current"] + self._danmaku_step)
        return {"code": 0}

    async def get_room_emoticons(self, room_id):
        return self.emoticons


def _fast_config():
    return AppConfig(allow_write_operations=True, medal_max_retry=2,
                     medal_like_interval=(0.0, 0.0),
                     medal_danmaku_interval=(0.0, 0.0))


class MedalRunnerTests(unittest.TestCase):
    """执行引擎：完成即停、跳过未开播、无专属表情回退、效率统计。"""

    def test_actions_complete_then_stop(self):
        tasks = [
            {"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
             "limit": 10, "is_done": False, "raw": {}},
            {"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕", "current": 9,
             "limit": 10, "is_done": False, "raw": {}},
        ]
        api = _StubApi(tasks)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 1, room_label="x"))
        self.assertEqual(result["status"], "done")
        # click_time 取标题里的次数（点赞30次→30）；本桩按 click_time 计入，一次即满
        self.assertEqual(api.like_calls, [30])
        self.assertEqual(len(api.danmaku_calls), 1)
        like = result["details"]["like"]
        self.assertEqual((like["actions"], like["clicks"], like["ok"]), (1, 30, True))
        danmaku = result["details"]["danmaku"]
        self.assertEqual((danmaku["sent"], danmaku["used_text"]), (1, 1))
        self.assertIsInstance(result["elapsed"], float)

    def test_like_loops_until_complete_when_server_counts_one_per_request(self):
        """服务端每次只 +1 时需多轮发送直到完成（回归：曾只试 4 轮就判失败）。"""
        tasks = [{"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
                  "limit": 4, "is_done": False, "raw": {}}]
        api = _StubApi(tasks, like_step=1)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 1))
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(api.like_calls), 4)
        self.assertEqual(result["details"]["like"]["actions"], 4)

    def test_like_stops_when_progress_stalls(self):
        """进度完全不推进：连续无进展达上限即停并给出原因（不无限空发）。"""
        tasks = [{"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
                  "limit": 4, "is_done": False, "raw": {}}]
        api = _StubApi(tasks, like_step=0)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 1))
        self.assertNotEqual(result["status"], "done")
        self.assertIn("未推进", result["message"])
        self.assertEqual(len(api.like_calls), 3)  # max_retry(2)+1 次后停止

    def test_danmaku_loops_until_complete(self):
        """发弹幕同样按轮次推进直到完成。"""
        tasks = [{"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕", "current": 0,
                  "limit": 5, "is_done": False, "raw": {}}]
        api = _StubApi(tasks)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 0))
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(api.danmaku_calls), 5)

    def test_like_skipped_when_offline(self):
        tasks = [{"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
                  "limit": 10, "is_done": False, "raw": {}}]
        api = _StubApi(tasks)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 0))
        self.assertEqual(api.like_calls, [])  # 未开播不点赞
        self.assertTrue(result["details"]["like"]["skipped"])
        self.assertEqual(result["status"], "done")  # 跳过不计为失败

    def test_danmaku_uses_room_exclusive_emoticons(self):
        tasks = [{"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕",
                  "current": 0, "limit": 2, "is_done": False, "raw": {}}]
        emoticons = [{"name": "房间专属", "emoticons": [
            {"unique": "room_1_1", "url": "u1"},
            {"unique": "room_1_2", "url": "u2"}]}]
        api = _StubApi(tasks, emoticons=emoticons)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 0))
        self.assertEqual(api.danmaku_calls, ["room_1_1", "room_1_2"])
        self.assertEqual(result["details"]["danmaku"]["used_emoticon"], 2)
        self.assertEqual(result["details"]["danmaku"]["used_text"], 0)

    def test_not_lit_tasks_skipped_without_actions(self):
        """未点亮（limit=0）的写任务不应触发任何点/弹幕请求（回归：曾反复空发导致失败）。"""
        tasks = [
            {"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
             "limit": 0, "is_done": False, "raw": {"sub_title": "仅点亮"}},
            {"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕10次", "current": 0,
             "limit": 0, "is_done": False, "raw": {"sub_title": "仅点亮"}},
        ]
        api = _StubApi(tasks)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 1))
        self.assertEqual(api.like_calls, [])
        self.assertEqual(api.danmaku_calls, [])
        self.assertEqual(result["status"], "done")
        self.assertIn("仅点亮", result["message"])

    def test_emits_task_progress_events(self):
        """每轮复核后上报实时进度，供界面在执行期间就地更新该行。"""
        tasks = [{"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
                  "limit": 3, "is_done": False, "raw": {}}]
        api = _StubApi(tasks, like_step=1)
        events = []
        runner = MedalTaskRunner(api, _fast_config(),
                                 emit=lambda t, p: events.append((t, p)))
        result = asyncio.run(runner.complete_room(100, 200, 1))
        self.assertEqual(result["status"], "done")
        prog = [p for t, p in events if t == "medal_task_progress"]
        self.assertEqual([p["current"] for p in prog], [0, 1, 2])
        self.assertTrue(all(p["jump_type"] == TASK_LIKE for p in prog))
        self.assertEqual([p["limit"] for p in prog], [3, 3, 3])

    def test_only_runs_selected_task(self):
        """only=like 时只点赞，不动发弹幕（界面上两个独立按钮）。"""
        tasks = [
            {"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
             "limit": 10, "is_done": False, "raw": {}},
            {"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕", "current": 0,
             "limit": 10, "is_done": False, "raw": {}},
        ]
        api = _StubApi(tasks)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 1, only=TASK_LIKE))
        self.assertEqual(api.like_calls, [30])
        self.assertEqual(api.danmaku_calls, [])
        self.assertIn("like", result["details"])
        self.assertNotIn("danmaku", result["details"])

    def test_only_danmaku_does_not_like(self):
        tasks = [
            {"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
             "limit": 10, "is_done": False, "raw": {}},
            {"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕", "current": 0,
             "limit": 3, "is_done": False, "raw": {}},
        ]
        api = _StubApi(tasks)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 1, only=TASK_SEND_DANMAKU))
        self.assertEqual(api.like_calls, [])
        self.assertEqual(len(api.danmaku_calls), 3)
        self.assertEqual(result["status"], "done")

    def test_only_accepts_multiple_types(self):
        """自动开关两项都开时，传入类型列表，两项都执行。"""
        tasks = [
            {"jump_type": TASK_LIKE, "title": "点赞30次", "current": 0,
             "limit": 3, "is_done": False, "raw": {}},
            {"jump_type": TASK_SEND_DANMAKU, "title": "发弹幕", "current": 0,
             "limit": 2, "is_done": False, "raw": {}},
            {"jump_type": TASK_WATCH_LIVE, "title": "观看直播满15分钟", "current": 0,
             "limit": 9, "is_done": False, "raw": {}},
        ]
        api = _StubApi(tasks, like_step=1)
        result = asyncio.run(MedalTaskRunner(api, _fast_config()).complete_room(
            100, 200, 1, only=[TASK_LIKE, TASK_SEND_DANMAKU]))
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(api.like_calls), 3)
        self.assertEqual(len(api.danmaku_calls), 2)
        self.assertIn("like", result["details"])
        self.assertIn("danmaku", result["details"])

    def test_only_completed_reports_reason(self):
        tasks = [{"jump_type": TASK_LIKE, "title": "点赞30次", "current": 10,
                  "limit": 10, "is_done": True, "raw": {}}]
        api = _StubApi(tasks)
        result = asyncio.run(MedalTaskRunner(api, _fast_config())
                             .complete_room(100, 200, 1, only=TASK_LIKE))
        self.assertEqual(result["status"], "done")
        self.assertIn("已完成", result["message"])
        self.assertEqual(api.like_calls, [])

    def test_complete_room_requires_write_switch(self):
        api = _StubApi([])
        cfg = AppConfig(allow_write_operations=False)
        result = asyncio.run(MedalTaskRunner(api, cfg).complete_room(1, 2, 1))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(api.like_calls, [])

    def test_format_metrics(self):
        like = MedalTaskRunner._format_metrics(
            "like", {"actions": 3, "progress": (5, 10), "elapsed": 2.5})
        self.assertIn("3 次请求", like)
        self.assertIn("进度 5→10", like)
        self.assertEqual(MedalTaskRunner._format_metrics("like", {"actions": 0}), "")
        self.assertIn("表情 2", MedalTaskRunner._format_metrics(
            "danmaku", {"sent": 3, "used_emoticon": 2, "used_text": 1, "elapsed": 14.0}))
        self.assertEqual(MedalTaskRunner._format_metrics("danmaku", {"sent": 0}), "")


if __name__ == "__main__":
    unittest.main()
