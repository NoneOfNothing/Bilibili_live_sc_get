"""协议封包/解包与 SC 处理、存储的单元测试（不依赖网络）。"""

import asyncio
import csv
import json
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest import mock

from blive_sc_get import protocol
from blive_sc_get import client as client_module
from blive_sc_get.client import RoomClient
from blive_sc_get.room_lock import RoomLock, RoomLockAcquireError
from blive_sc_get.storage import SCStorage


SAMPLE_SC = {
    "cmd": "SUPER_CHAT_MESSAGE",
    "data": {
        "id": 10001,
        "uid": 424242,
        "price": 30,
        "message": "主播加油\n666",
        "start_time": 1757116800,
        "end_time": 1757120400,
        "time": 3600,
        "user_info": {"uname": "测试用户", "face": "https://example.com/face.png"},
    },
}


def _packet(op, body, protocol_version=protocol.PROTOCOL_RAW):
    return protocol.build_packet(op, body, protocol=protocol_version)


class ProtocolTests(unittest.TestCase):
    def test_roundtrip(self):
        body = json.dumps({"cmd": "TEST"}).encode("utf-8")
        packets = protocol.flatten_packets(_packet(protocol.Operation.MESSAGE, body))
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0][1], protocol.Operation.MESSAGE)
        self.assertEqual(packets[0][2], body)

    def test_concatenated_packets(self):
        p1 = _packet(protocol.Operation.MESSAGE, b'{"a": 1}')
        p2 = _packet(protocol.Operation.MESSAGE, b'{"b": 2}')
        packets = protocol.flatten_packets(p1 + p2)
        self.assertEqual(len(packets), 2)

    def test_zlib_nested(self):
        inner = _packet(protocol.Operation.MESSAGE, b'{"a": 1}')
        packet = _packet(protocol.Operation.MESSAGE, zlib.compress(inner), protocol.PROTOCOL_ZLIB)
        packets = protocol.flatten_packets(packet)
        self.assertEqual(len(packets), 1)
        self.assertEqual(json.loads(packets[0][2]), {"a": 1})

    @unittest.skipIf(protocol.brotli is None, "未安装 Brotli")
    def test_brotli_nested(self):
        inner = _packet(protocol.Operation.HEARTBEAT, struct.pack(">I", 233))
        packet = _packet(
            protocol.Operation.MESSAGE, protocol.brotli.compress(inner), protocol.PROTOCOL_BROTLI
        )
        packets = protocol.flatten_packets(packet)
        self.assertEqual(packets[0][1], protocol.Operation.HEARTBEAT)
        self.assertEqual(struct.unpack(">I", packets[0][2])[0], 233)

    def test_truncated_packet_raises(self):
        body = json.dumps({"cmd": "TEST"}).encode("utf-8")
        raw = _packet(protocol.Operation.MESSAGE, body)[:-3]
        with self.assertRaises(protocol.ProtocolError):
            protocol.flatten_packets(raw)

    def test_zlib_decompression_bomb_rejected(self):
        # 解压后超过上限的报文应被拒绝而不是耗尽内存
        bomb = zlib.compress(b"\x00" * (protocol.MAX_DECOMPRESSED_SIZE + 1024))
        packet = _packet(protocol.Operation.MESSAGE, bomb, protocol.PROTOCOL_ZLIB)
        with self.assertRaises(protocol.ProtocolError):
            protocol.flatten_packets(packet)

    def test_normal_sized_compressed_packet_ok(self):
        inner = _packet(protocol.Operation.MESSAGE, b'{"a": 1}')
        packet = _packet(protocol.Operation.MESSAGE, zlib.compress(inner), protocol.PROTOCOL_ZLIB)
        packets = protocol.flatten_packets(packet)
        self.assertEqual(json.loads(packets[0][2]), {"a": 1})


class SuperChatHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.storage = SCStorage(self.tmp)
        self.client = RoomClient(api=None, room_id=9527, storage=self.storage)
        self.room_dir = self.tmp / "room_9527"

    def test_save_sc(self):
        self.client._handle_business_message(json.dumps(SAMPLE_SC, ensure_ascii=False).encode("utf-8"))
        jsonl_files = list(self.room_dir.glob("sc_*.jsonl"))
        self.assertEqual(len(jsonl_files), 1)
        record = json.loads(jsonl_files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["type"], "sc")
        self.assertEqual(record["sc"]["id"], 10001)
        csv_files = list(self.room_dir.glob("sc_*.csv"))
        self.assertEqual(len(csv_files), 1)
        content = csv_files[0].read_text(encoding="utf-8-sig")
        self.assertIn("测试用户", content)
        self.assertIn("10001", content)

    def test_dedup_same_session(self):
        payload = json.dumps(SAMPLE_SC, ensure_ascii=False).encode("utf-8")
        self.client._handle_business_message(payload)
        self.client._handle_business_message(payload)
        lines = next(self.room_dir.glob("sc_*.jsonl")).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_dedup_after_restart(self):
        # 模拟重启：新客户端实例应能从历史记录里读到已保存的 SC id
        self.client._handle_business_message(json.dumps(SAMPLE_SC, ensure_ascii=False).encode("utf-8"))
        client2 = RoomClient(api=None, room_id=9527, storage=self.storage)
        self.assertEqual(client2._storage.load_known_sc_ids(9527), {10001})

    def test_delete(self):
        delete_msg = {"cmd": "SUPER_CHAT_MESSAGE_DELETE", "data": {"ids": [10001]}}
        self.client._handle_business_message(json.dumps(delete_msg).encode("utf-8"))
        deleted = json.loads((self.room_dir / "deleted_ids.json").read_text(encoding="utf-8"))
        self.assertIn("10001", deleted)
        jsonl_text = next(self.room_dir.glob("sc_*.jsonl")).read_text(encoding="utf-8")
        self.assertIn('"type": "delete"', jsonl_text.replace("'", '"'))

    def test_nested_data_format(self):
        # 兼容 data.data 双层嵌套的历史格式
        nested = {"cmd": "SUPER_CHAT_MESSAGE",
                  "data": {"data": dict(SAMPLE_SC["data"], id=10002)}}
        self.client._handle_business_message(json.dumps(nested, ensure_ascii=False).encode("utf-8"))
        record = json.loads(
            next(self.room_dir.glob("sc_*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertEqual(record["sc"]["id"], 10002)


class DanmakuTests(unittest.TestCase):
    """DANMU_MSG 解析、门控与弹幕落盘（dm_*.jsonl 与 SC 文件隔离）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.storage = SCStorage(self.tmp)
        self.events = []
        self.client = RoomClient(api=None, room_id=9527, storage=self.storage,
                                 event_callback=lambda et, p: self.events.append((et, p)))
        self.room_dir = self.tmp / "room_9527"

    @staticmethod
    def _dm(text="你好", uname="弹幕哥"):
        return {"cmd": "DANMU_MSG",
                "info": [[0, 1, 25, 16777271], text, [0, uname, 1, 0]]}

    def test_disabled_by_default(self):
        self.client._handle_business_message(json.dumps(self._dm()).encode("utf-8"))
        self.assertEqual(self.events, [])
        self.assertFalse(list(self.room_dir.glob("dm_*.jsonl")))

    def test_enabled_emits_and_saves(self):
        self.client.set_danmaku_enabled(True)
        self.client._handle_business_message(json.dumps(self._dm()).encode("utf-8"))
        self.assertEqual(len(self.events), 1)
        etype, payload = self.events[0]
        self.assertEqual(etype, "dm")
        self.assertEqual(payload["room_id"], 9527)
        self.assertEqual(payload["uname"], "弹幕哥")
        self.assertEqual(payload["text"], "你好")
        self.storage.flush_danmaku_buffers()  # 缓冲写，周期刷盘后落盘
        files = list(self.room_dir.glob("dm_*.jsonl"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["type"], "dm")
        self.assertEqual(record["text"], "你好")
        # 弹幕文件不混入 SC 读取
        self.assertEqual(self.storage.load_sc_page(9527), [])

    def test_malformed_info_tolerated(self):
        self.client.set_danmaku_enabled(True)
        for bad in ({"cmd": "DANMU_MSG"},
                    {"cmd": "DANMU_MSG", "info": [1, 2]},
                    {"cmd": "DANMU_MSG", "info": [[], "", []]}):
            self.client._handle_business_message(json.dumps(bad).encode("utf-8"))
        self.assertEqual(self.events, [])

    def test_emote_danmu_wrapped_in_brackets(self):
        # 表情包弹幕（dm_type=1，info[0][13]）：info[1] 为触发词，显示为 [触发词]
        self.client.set_danmaku_enabled(True)
        msg = {"cmd": "DANMU_MSG",
               "info": [[0, 1, 25, 16777215, 0, 0, 0, "h", 0, 0, 0, "", 0, 1, 0,
                         '{"dm_type": 1}'], "百岁山", [0, "弹幕哥", 1, 0]]}
        self.client._handle_business_message(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1]["text"], "[百岁山]")
        self.storage.flush_danmaku_buffers()
        record = json.loads(next(self.room_dir.glob("dm_*.jsonl"))
                            .read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["text"], "[百岁山]")

    def test_emote_danmu_with_brackets_unchanged(self):
        # 触发词本身已带 [] 时不重复包裹
        self.client.set_danmaku_enabled(True)
        msg = {"cmd": "DANMU_MSG",
               "info": [[0, 1, 25, 16777215, 0, 0, 0, "h", 0, 0, 0, "", 0, 1, 0, "{}"],
                        "[已带括号]", [0, "弹幕哥", 1, 0]]}
        self.client._handle_business_message(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(self.events[0][1]["text"], "[已带括号]")

    def test_emote_danmu_real_structure(self):
        # 真实结构：info[0][13] 是表情图片信息对象（不是 dm_type），
        # info[1] 为空时触发词从 extra 的 content 回退提取
        extra = json.dumps({"dm_type": 1, "content": "打call",
                            "emoticon_unique": "room_9527_109824"}, ensure_ascii=False)
        msg = {"cmd": "DANMU_MSG",
               "info": [[0, 4, 25, 14893055, 0, 0, 0, "h", 0, 0, 43, "", 1,
                         {"bulge_display": 1, "emoticon_unique": "room_9527_109824",
                          "url": "https://i0.hdslb.com/bfs/garb/x.jpg", "width": 162},
                         "{}", {"extra": extra}],
                        "", [0, "弹幕哥", 1, 0]]}
        self.client.set_danmaku_enabled(True)
        self.client._handle_business_message(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1]["text"], "[打call]")
        self.storage.flush_danmaku_buffers()
        record = json.loads(next(self.room_dir.glob("dm_*.jsonl"))
                            .read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["text"], "[打call]")

    def test_close_buffer_flushes_to_disk(self):
        self.client.set_danmaku_enabled(True)
        self.client._handle_business_message(json.dumps(self._dm()).encode("utf-8"))
        self.storage.close_danmaku_buffers()
        files = list(self.room_dir.glob("dm_*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertIn("你好", files[0].read_text(encoding="utf-8"))

    def test_set_danmaku_enabled_disables_again(self):
        self.client.set_danmaku_enabled(True)
        self.client.set_danmaku_enabled(False)
        self.client._handle_business_message(json.dumps(self._dm()).encode("utf-8"))
        self.assertEqual(self.events, [])

    def test_dmid_extracted_from_candidates(self):
        # dmid 取候选下标中最长的纯数字串；占位/空值取不到时返回 ""
        self.assertEqual(RoomClient._extract_dmid([[]]), "")
        self.assertEqual(RoomClient._extract_dmid([[0, 0, 0, 0, 0, 0, 0, 0]]), "")
        self.assertEqual(
            RoomClient._extract_dmid([[0, 0, 0, 0, 0, 0, "627348750013235456", 0]]),
            "627348750013235456")
        # 下标 6 为短数字时，取更长的下标 7（雪花号）
        self.assertEqual(
            RoomClient._extract_dmid([[0, 0, 0, 0, 0, 0, 12, "123456789012345678"]]),
            "123456789012345678")

    def test_dmid_in_dm_event(self):
        self.client.set_danmaku_enabled(True)
        msg = {"cmd": "DANMU_MSG",
               "info": [[0, 1, 25, 16777215, 0, 0, "627348750013235456", 0],
                        "你好", [0, "弹幕哥", 1, 0]]}
        self.client._handle_business_message(
            json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1]["dmid"], "627348750013235456")


RECEIVED_AT = datetime(2026, 9, 6, 20, 0, 0)


class RetryQueueTests(unittest.TestCase):
    """CSV/JSONL 被占用时进入重试队列，运行中补写，且能跨重启恢复。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.storage = SCStorage(self.tmp)
        self.room_dir = self.tmp / "room_9527"

    def _sc(self, sc_id=10001):
        return dict(SAMPLE_SC["data"], id=sc_id)

    def _jsonl_lines(self):
        return (self.room_dir / "sc_20260906.jsonl").read_text(encoding="utf-8").strip().splitlines()

    def _csv_rows(self):
        """按 CSV 语义解析出记录行；文件不存在时返回空列表。"""
        path = self.room_dir / "sc_20260906.csv"
        if not path.exists():
            return []
        with path.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    def _pending_path(self):
        return self.room_dir / "pending_records.jsonl"

    def test_csv_locked_enqueued_then_flushed(self):
        with mock.patch.object(self.storage, "_write_sc_csv", side_effect=OSError(13, "locked")):
            self.assertFalse(self.storage.save_sc(9527, self._sc(), RECEIVED_AT))
        self.assertEqual(len(self._jsonl_lines()), 1)          # JSONL 已正常写入
        self.assertEqual(self._csv_rows(), [])                 # CSV 完全没写出来
        self.assertTrue(self._pending_path().exists())         # 队列已持久化

        self.assertEqual(self.storage.flush_pending(9527), 1)  # 解锁后补写
        rows = self._csv_rows()
        self.assertEqual([r[0] for r in rows[1:]], ["10001"])  # 只补了缺的那行
        self.assertEqual(len(self._jsonl_lines()), 1)          # 不重复写 JSONL
        self.assertFalse(self._pending_path().exists())        # 补写完队列文件删除

    def test_pending_survives_restart(self):
        with mock.patch.object(self.storage, "_write_sc_csv", side_effect=OSError(13, "locked")):
            self.storage.save_sc(9527, self._sc(), RECEIVED_AT)
        # 模拟重启：全新的 storage 实例从磁盘恢复队列并补写
        restarted = SCStorage(self.tmp)
        self.assertEqual(restarted.load_pending(9527), 1)
        self.assertEqual(restarted.flush_pending(9527), 1)
        self.assertEqual([r[0] for r in self._csv_rows()[1:]], ["10001"])
        self.assertEqual(len(self._jsonl_lines()), 1)
        self.assertFalse(self._pending_path().exists())

    def test_jsonl_locked_enqueued_then_flushed(self):
        with mock.patch.object(self.storage, "_write_sc_jsonl", side_effect=OSError(13, "locked")):
            self.assertFalse(self.storage.save_sc(9527, self._sc(), RECEIVED_AT))
        self.assertFalse((self.room_dir / "sc_20260906.jsonl").exists())   # 整条未写
        self.assertEqual([r[0] for r in self._csv_rows()[1:]], ["10001"])  # CSV 已写入

        self.assertEqual(self.storage.flush_pending(9527), 1)
        self.assertEqual(len(self._jsonl_lines()), 1)
        self.assertEqual(len(self._csv_rows()), 2)              # 表头+1行，不重复
        self.assertFalse(self._pending_path().exists())

    def test_repeated_failures_until_unlock(self):
        with mock.patch.object(self.storage, "_write_sc_csv", side_effect=OSError(13, "locked")):
            self.storage.save_sc(9527, self._sc(), RECEIVED_AT)
            self.assertEqual(self.storage.flush_pending(9527), 0)  # 仍被占用，保留队列
            self.assertTrue(self._pending_path().exists())
        self.assertEqual(self.storage.flush_pending(9527), 1)      # 解锁后补写成功
        self.assertEqual(self.storage.flush_pending(9527), 0)      # 队列已清空

    def test_csv_formula_injection_guard(self):
        # 以 = + - @ 开头的内容在 Excel 中会被当作公式，须前缀单引号防护。
        # 注意 user_info 必须整体替换而非原地修改，避免污染共享的 SAMPLE_SC
        sc = self._sc()
        sc["message"] = '=HYPERLINK("http://evil.example")'
        sc["user_info"] = {"uname": "+8613800000000"}
        self.storage.save_sc(9527, sc, RECEIVED_AT)
        rows = self._csv_rows()
        self.assertTrue(rows[1][4].startswith("'="), rows[1][4])   # message 列
        self.assertTrue(rows[1][2].startswith("'+"), rows[1][2])   # username 列


class RoomLockTests(unittest.TestCase):
    """同一房间目录同一时间只能被一个实例持有；释放/崩溃后可重新获取。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_second_acquire_fails_and_reads_holder(self):
        lock1 = RoomLock(self.tmp)
        lock1.acquire()
        try:
            lock_path = self.tmp / ".room_lock"
            self.assertIn(f"pid={os.getpid()}", lock_path.read_text(encoding="utf-8"))
            with self.assertRaises(RoomLockAcquireError):
                RoomLock(self.tmp).acquire()
        finally:
            lock1.release()

    def test_reacquire_after_release(self):
        lock1 = RoomLock(self.tmp)
        lock1.acquire()
        lock1.release()
        lock2 = RoomLock(self.tmp)
        lock2.acquire()
        lock2.release()

    def test_release_is_idempotent(self):
        lock = RoomLock(self.tmp)
        lock.acquire()
        lock.release()
        lock.release()  # 重复释放不应报错

    def test_acquire_creates_room_dir(self):
        room_dir = self.tmp / "room_9527"
        lock = RoomLock(room_dir)
        lock.acquire()
        try:
            self.assertTrue(room_dir.is_dir())
        finally:
            lock.release()


class _FakeAPI:
    """仅用于验证客户端在加锁失败时不会继续后续流程。"""

    uid = 0
    buvid3 = ""
    session = None

    def __init__(self):
        self.danmu_called = False

    def ws_headers(self):
        return {}

    async def get_room_info(self, room_id):
        return {"room_id": room_id, "live_status": 1, "title": "t"}

    async def get_full_room_info(self, room_id):
        return await self.get_room_info(room_id)

    async def get_anchor_name(self, uid):
        return "测试主播"

    async def get_danmu_info(self, room_id):
        self.danmu_called = True
        return {"token": "x", "host_list": []}


class RoomClientLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.storage = SCStorage(self.tmp)

    def test_locked_room_stops_before_connecting(self):
        holder = RoomLock(self.tmp / "room_9527")
        holder.acquire()
        try:
            api = _FakeAPI()
            client = RoomClient(api=api, room_id=9527, storage=self.storage)
            with self.assertRaises(RoomLockAcquireError):
                asyncio.run(client._run_once())
            self.assertFalse(api.danmu_called)  # 加锁失败就不会连接弹幕服务器
        finally:
            holder.release()

    def test_run_stops_cleanly_when_room_locked(self):
        holder = RoomLock(self.tmp / "room_9527")
        holder.acquire()
        try:
            client = RoomClient(api=_FakeAPI(), room_id=9527, storage=self.storage)
            # run() 应因锁冲突记录错误并直接返回，而不是抛异常或挂起重连
            asyncio.run(asyncio.wait_for(client.run(), 5.0))
            self.assertIsNone(client._room_lock)  # finally 已清理
        finally:
            holder.release()

    def test_client_acquires_lock_on_connect(self):
        client = RoomClient(api=_FakeAPI(), room_id=9527, storage=self.storage)
        # FakeAPI 没有 session，ws_connect 会失败，但加锁发生在连接之前
        with self.assertRaises(AttributeError):
            asyncio.run(client._run_once())
        self.assertIsNotNone(client._room_lock)
        self.assertTrue((self.tmp / "room_9527" / ".room_lock").exists())
        client._room_lock.release()


class _CachedLiveAPI:
    """模拟关播瞬间接口缓存：get_full_room_info 永远返回 live_status=1。"""

    def __init__(self):
        self.calls = 0

    async def get_full_room_info(self, room_id):
        self.calls += 1
        return {"room_id": room_id, "uid": 42, "title": "缓存标题", "live_status": 1}


class OfflineSignalTests(unittest.TestCase):
    """关播信号链路：乐观置为未开播、纠正接口缓存、确认刷新、节流合并。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 常量调小让确认刷新在测试里立刻执行
        self._orig = (client_module.OFFLINE_CONFIRM_DELAY,
                      client_module.OFFLINE_CONFIRM_RETRY_DELAY,
                      client_module.STATUS_REFRESH_DEBOUNCE)
        client_module.OFFLINE_CONFIRM_DELAY = 0.01
        client_module.OFFLINE_CONFIRM_RETRY_DELAY = 0.01
        client_module.STATUS_REFRESH_DEBOUNCE = 0.05
        self.addCleanup(self._restore)

    def _restore(self):
        (client_module.OFFLINE_CONFIRM_DELAY,
         client_module.OFFLINE_CONFIRM_RETRY_DELAY,
         client_module.STATUS_REFRESH_DEBOUNCE) = self._orig

    def _make_client(self, api, events):
        return RoomClient(api=api, room_id=9527, storage=SCStorage(self.tmp),
                          event_callback=lambda t, p: events.append((t, p)))

    def _feed(self, client, command: dict) -> None:
        client._handle_business_message(json.dumps(command).encode("utf-8"))

    def test_preparing_emits_offline_despite_cached_api(self):
        events = []
        api = _CachedLiveAPI()

        async def scenario():
            client = self._make_client(api, events)
            client._emit_status(1, "旧标题", 42)  # 连接时的基线：直播中
            self._feed(client, {"cmd": "PREPARING"})
            await asyncio.sleep(0)
            # 立即乐观置为未开播，携带缓存标题
            self.assertTrue(client._offline_signal)
            self.assertEqual(events[-1][0], "status")
            self.assertEqual(events[-1][1]["live_status"], 0)
            self.assertEqual(events[-1][1]["title"], "旧标题")
            await asyncio.sleep(0.1)  # 等待延迟确认刷新
            statuses = [p for t, p in events if t == "status"]
            # 确认刷新也发出 0：接口缓存的 1 被关播信号纠正
            self.assertGreaterEqual(len(statuses), 3)
            self.assertTrue(all(p["live_status"] == 0 for p in statuses[1:]))
            self.assertEqual(statuses[-1]["title"], "缓存标题")
            self.assertGreaterEqual(api.calls, 1)

        asyncio.run(scenario())

    def test_stop_live_room_list_hit_and_miss(self):
        events = []
        api = _CachedLiveAPI()

        async def scenario():
            client = self._make_client(api, events)
            self._feed(client, {"cmd": "STOP_LIVE_ROOM_LIST",
                                "data": {"room_id_list": [9527, 111]}})
            await asyncio.sleep(0)
            self.assertTrue(client._offline_signal)
            self.assertEqual(events[-1][1]["live_status"], 0)

            miss = self._make_client(_CachedLiveAPI(), [])
            self._feed(miss, {"cmd": "STOP_LIVE_ROOM_LIST",
                              "data": {"room_id_list": [111, 222]}})
            await asyncio.sleep(0)
            self.assertFalse(miss._offline_signal)

        asyncio.run(scenario())

    def test_live_clears_offline_signal(self):
        events = []
        api = _CachedLiveAPI()

        async def scenario():
            client = self._make_client(api, events)
            self._feed(client, {"cmd": "PREPARING"})
            await asyncio.sleep(0.1)  # 乐观 + 确认刷新均为 0
            self._feed(client, {"cmd": "LIVE"})
            await asyncio.sleep(0.1)
            self.assertFalse(client._offline_signal)
            self.assertEqual(events[-1][1]["live_status"], 1)  # 开播信号权威

        asyncio.run(scenario())

    def test_throttle_merges_burst_into_trailing_refresh(self):
        events = []
        api = _CachedLiveAPI()

        async def scenario():
            client = self._make_client(api, events)
            for _ in range(3):
                self._feed(client, {"cmd": "ROOM_CHANGE"})
            await asyncio.sleep(0.3)
            # 3 次触发 → 1 次立即刷新 + 1 次窗口末合并补发（而非丢弃或 3 次）
            self.assertEqual(api.calls, 2)
            self.assertEqual(len([p for t, p in events if t == "status"]), 2)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
