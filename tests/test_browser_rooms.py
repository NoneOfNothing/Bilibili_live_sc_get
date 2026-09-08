"""浏览器会话扫描的单元测试（离线，使用伪造的会话快照文件）。"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from blive_sc_get.browser_rooms import find_open_live_rooms, is_room_being_recorded
from blive_sc_get.room_lock import RoomLock


def _fake_session_file(profile_dir: Path, name: str, urls, mtime_age: float = 0.0) -> None:
    sessions = profile_dir / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    payload = b"\x00\x01SNSS-FEATURES\x00"
    for url in urls:
        payload += b"\x12\x00\x00\x00URL:" + url.encode("ascii") + b"\x00\x00"
    path = sessions / name
    path.write_bytes(payload)
    if mtime_age:
        old = time.time() - mtime_age
        os.utime(path, (old, old))


class FindOpenLiveRoomsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = self.tmp / "FakeBrowser" / "User Data"
        self.default_profile = self.base / "Default"
        self.default_profile.mkdir(parents=True, exist_ok=True)

    def _scan(self):
        return find_open_live_rooms(bases=[("Fake", self.base)])

    def test_extracts_rooms_with_query_and_dedup(self):
        _fake_session_file(
            self.default_profile, "Tabs_1",
            ["https://live.bilibili.com/1727071052?broadcast_type=0&is_room_feed=1"],
        )
        _fake_session_file(
            self.default_profile, "Session_1",
            ["https://live.bilibili.com/1727071052?live_from=1",
             "https://live.bilibili.com/1791260716"],
        )
        rooms = self._scan()
        self.assertEqual(set(rooms), {1727071052, 1791260716})
        self.assertEqual(rooms[1727071052], {"Fake"})

    def test_ignores_non_room_urls_and_short_ids(self):
        _fake_session_file(
            self.default_profile, "Tabs_2",
            ["https://live.bilibili.com/p/100?page=1",   # 非房间页面
             "https://live.bilibili.com/99",             # 少于 3 位
             "https://www.bilibili.com/video/12345678"], # 非直播域名
        )
        self.assertEqual(self._scan(), {})

    def test_ignores_old_snapshots(self):
        _fake_session_file(self.default_profile, "Tabs_old",
                           ["https://live.bilibili.com/1111111111"],
                           mtime_age=3 * 24 * 3600)
        self.assertEqual(self._scan(), {})

    def test_missing_base_returns_empty(self):
        self.assertEqual(find_open_live_rooms(bases=[("Fake", self.tmp / "nope")]), {})

    def test_multiple_profiles(self):
        _fake_session_file(self.default_profile, "Tabs_1",
                           ["https://live.bilibili.com/1111111111"])
        other = self.base / "Profile 1"
        other.mkdir(parents=True, exist_ok=True)
        _fake_session_file(other, "Tabs_2",
                           ["https://live.bilibili.com/2222222222"])
        rooms = self._scan()
        self.assertEqual(set(rooms), {1111111111, 2222222222})


class IsRoomBeingRecordedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_probe_reflects_lock_state(self):
        self.assertFalse(is_room_being_recorded(self.tmp, 9527))
        lock = RoomLock(self.tmp / "room_9527")
        lock.acquire()
        try:
            self.assertTrue(is_room_being_recorded(self.tmp, 9527))
        finally:
            lock.release()
        self.assertFalse(is_room_being_recorded(self.tmp, 9527))


if __name__ == "__main__":
    unittest.main()
