"""从 Edge/Chrome 的会话快照文件中提取当前打开的 B 站直播间。

浏览器会把打开的标签页周期性快照到磁盘（Edge 与 Chrome 的目录结构相同），URL 在其中
以明文存储，因此直接按字节扫描 ``live.bilibili.com/<房间号>`` 即可，无需解密、无需
重启浏览器、无需管理员权限。运行中的浏览器会滚动删除旧快照，扫描时需容忍文件消失。
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple, Union

from .room_lock import RoomLock, RoomLockAcquireError

logger = logging.getLogger(__name__)

# 只扫描近期更新过的快照，排除早已关闭的标签页残留
MAX_SESSION_AGE = 24 * 3600

# URL 形如 https://live.bilibili.com/21452505?broadcast_type=0...；
# /p/ 等页面路径与少于 3 位的数字都不是直播间号
ROOM_URL_RE = re.compile(rb"live\.bilibili\.com/(\d{3,})")

# 旧版 Chrome/Edge 把当前会话直接放在 profile 根目录
_LEGACY_SESSION_FILES = ("Current Session", "Current Tabs")


def default_browser_bases() -> List[Tuple[str, Path]]:
    """返回本机存在的浏览器会话根目录 [(浏览器名, User Data 目录)]。"""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return []
    root = Path(local_appdata)
    candidates = [
        ("Edge", root / "Microsoft" / "Edge" / "User Data"),
        ("Chrome", root / "Google" / "Chrome" / "User Data"),
    ]
    return [(name, base) for name, base in candidates if base.is_dir()]


def _iter_session_files(browser_base: Path) -> Iterator[Path]:
    if not browser_base.is_dir():
        return
    try:
        profiles = sorted(browser_base.iterdir())
    except OSError:
        return
    for profile in profiles:
        if not profile.is_dir():
            continue
        sessions_dir = profile / "Sessions"
        if sessions_dir.is_dir():
            yield from sessions_dir.glob("Session_*")
            yield from sessions_dir.glob("Tabs_*")
        for name in _LEGACY_SESSION_FILES:
            legacy = profile / name
            if legacy.exists():
                yield legacy


def find_open_live_rooms(bases: Optional[List[Tuple[str, Path]]] = None) -> Dict[int, Set[str]]:
    """扫描浏览器会话快照，返回 {房间号: 出现的浏览器名集合}。"""
    now = time.time()
    found: Dict[int, Set[str]] = {}
    for browser, base in (bases if bases is not None else default_browser_bases()):
        for path in _iter_session_files(base):
            try:
                if now - path.stat().st_mtime > MAX_SESSION_AGE:
                    continue
                data = path.read_bytes()
            except OSError:
                # 浏览器运行中会滚动/删除快照，文件随时可能消失，跳过即可
                continue
            for match in ROOM_URL_RE.finditer(data):
                found.setdefault(int(match.group(1)), set()).add(browser)
    return found


def is_room_being_recorded(base_dir: Union[str, Path], room_id: int) -> bool:
    """探测房间是否已被其他实例监听：试加锁后立即释放。"""
    lock = RoomLock(Path(base_dir) / f"room_{room_id}")
    try:
        lock.acquire()
    except RoomLockAcquireError:
        return True
    lock.release()
    return False
