"""房间级互斥锁：防止两个程序实例同时监听同一房间，并发写同一批数据文件。

实现方式为「文件句柄上的字节范围锁」：实例存活期间持有锁，进程退出（包括崩溃、
被强杀）时由操作系统自动释放，因此不存在残留死锁。锁文件本身会留在磁盘上（内容
为持有者信息），残留无害，下次启动会重新竞争加锁。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

if fcntl is None:
    import msvcrt

LOCK_FILE_NAME = ".room_lock"
# 锁定一个远超文件实际长度的字节位置，文件内容（持有者信息）始终可被其他进程读取
LOCK_BYTE_OFFSET = 65536


class RoomLockAcquireError(RuntimeError):
    """房间已被本机上的其他程序实例占用。"""


class RoomLock:
    """绑定一个房间目录的互斥锁；acquire() 失败会抛出 RoomLockAcquireError。"""

    def __init__(self, room_dir: Path):
        self._path = Path(room_dir) / LOCK_FILE_NAME
        self._fp = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        if self._fp is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 用 "a+" 而非 "w"：加锁失败时不破坏文件里已有的持有者信息
        fp = self._path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fp.seek(LOCK_BYTE_OFFSET)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            fp.close()
            holder = self._read_holder()
            detail = f"（持有者: {holder}）" if holder else ""
            raise RoomLockAcquireError(
                f"另一个实例正在监听该房间 {detail}，锁文件: {self._path}"
            ) from exc
        # 加锁成功后记录持有者信息，便于排查是谁在占用
        fp.seek(0)
        fp.truncate(0)
        fp.write(f"pid={os.getpid()} started={datetime.now().isoformat(timespec='seconds')}\n")
        fp.flush()
        self._fp = fp

    def release(self) -> None:
        if self._fp is None:
            return
        fp, self._fp = self._fp, None
        try:
            if fcntl is not None:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            else:
                fp.seek(LOCK_BYTE_OFFSET)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            # 释放失败也无妨：进程退出时操作系统会释放句柄上的全部锁
            pass
        finally:
            fp.close()

    def _read_holder(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8").strip().replace("\n", " ")
        except OSError:
            return ""
