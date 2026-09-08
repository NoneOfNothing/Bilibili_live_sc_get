"""命令行入口与参数解析。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence, Set

import aiohttp

from .api import ApiError, BilibiliLiveAPI
from .browser_rooms import find_open_live_rooms, is_room_being_recorded
from .client import RoomClient
from .storage import SCStorage

logger = logging.getLogger("blive_sc_get")

COOKIE_FILE_NAME = "cookie.txt"
COOKIE_FILE_HINT = (
    "可通过 --cookie 参数、环境变量 BILI_COOKIE 或项目根目录下的 cookie.txt 提供 B 站 cookie"
)


def parse_room_id(text: str) -> int:
    """支持纯数字、短号，或直接粘贴直播间链接。"""
    match = re.search(r"\d{3,}", str(text))
    if not match:
        raise argparse.ArgumentTypeError(f"无法从 {text!r} 中解析出房间号")
    return int(match.group())


def resolve_cookie(explicit: Optional[str]) -> Optional[str]:
    """cookie 的优先级：--cookie > 环境变量 BILI_COOKIE > cookie.txt。"""
    if explicit:
        source = "--cookie 参数"
        cookie = explicit.strip()
    else:
        env_cookie = os.environ.get("BILI_COOKIE", "").strip()
        cookie_file = Path(__file__).resolve().parent.parent / COOKIE_FILE_NAME
        if env_cookie:
            source = "环境变量 BILI_COOKIE"
            cookie = env_cookie
        elif cookie_file.exists():
            source = f"文件 {cookie_file}"
            cookie = cookie_file.read_text(encoding="utf-8").strip()
        else:
            return None
    if not cookie:
        return None
    logger.info("已从%s加载 cookie（长度 %d）", source, len(cookie))
    return cookie


def setup_logging(verbose: bool, log_file: Optional[Path]) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blive_sc_get",
        description="监听 B 站直播间弹幕 WebSocket，自动抓取 SuperChat（醒目留言）并保存到本地。",
    )
    parser.add_argument(
        "rooms", nargs="*", type=parse_room_id, metavar="ROOM",
        help="直播间号（支持短号或直播间链接，可同时监听多个房间；与 --auto 二选一）",
    )
    parser.add_argument(
        "-a", "--auto", action="store_true",
        help="自动从 Edge/Chrome 会话中检测当前打开的直播间并监听；"
             "运行中每 30 秒重扫一次，新开的直播间自动加入，已加入的不会自动停止",
    )
    parser.add_argument("-o", "--output-dir", default="data", help="数据保存目录（默认 data）")
    parser.add_argument(
        "--cookie", default=None,
        help="B 站 cookie 字符串；不填则依次尝试环境变量 BILI_COOKIE 与 cookie.txt 文件",
    )
    parser.add_argument(
        "--duration", type=float, default=0,
        help="运行秒数，超时后自动退出；0 表示一直运行（默认）",
    )
    parser.add_argument("--log-file", default=None, help="同时把日志写入指定文件")
    parser.add_argument("--gui", action="store_true", help="启动图形界面（忽略房间号等参数）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志（全部消息类型、人气值）")
    parser.add_argument("--version", action="version", version="blive_sc_get 1.3.0")
    return parser


PENDING_FLUSH_INTERVAL = 5.0
"""重试队列的补写周期（秒）。"""

AUTO_RESCAN_INTERVAL = 30.0
"""--auto 模式下重新扫描浏览器会话的周期（秒）。"""


async def _pending_flush_loop(storage: SCStorage) -> None:
    """定期补写因文件被占用（如 Excel 打开 CSV）而暂存的记录。"""
    while True:
        await asyncio.sleep(PENDING_FLUSH_INTERVAL)
        try:
            storage.flush_all_pending()
        except Exception:
            logger.exception("重试队列补写出错")


def _start_room_task(args: argparse.Namespace, api: BilibiliLiveAPI, storage: SCStorage,
                     room_id: int, tasks: Dict[int, asyncio.Task]) -> bool:
    """把房间加入监听（跳过已被其他实例监听的房间）；返回是否真的启动。"""
    if is_room_being_recorded(args.output_dir, room_id):
        logger.info("房间 %s 已被其他实例监听，跳过", room_id)
        return False
    client = RoomClient(api, room_id, storage)
    tasks[room_id] = asyncio.create_task(client.run(), name=f"room-{room_id}")
    logger.info("已加入监听: 房间 %s", room_id)
    return True


async def _auto_supervisor(args: argparse.Namespace, api: BilibiliLiveAPI,
                           storage: SCStorage, tasks: Dict[int, asyncio.Task]) -> None:
    """周期性重扫浏览器会话：新开的直播间自动加入，已加入的不会自动停止。"""
    skipped: Set[int] = set()
    last_seen: Optional[Set[int]] = None
    while True:
        try:
            detected = find_open_live_rooms()
            detected_ids = set(detected)
            if detected_ids != last_seen:
                logger.info("浏览器中检测到直播间: %s",
                            ", ".join(str(r) for r in sorted(detected_ids)) or "（无）")
                last_seen = detected_ids
            for room_id in sorted(detected_ids):
                task = tasks.get(room_id)
                if task is not None and not task.done():
                    continue
                if room_id in skipped:
                    continue
                if not _start_room_task(args, api, storage, room_id, tasks):
                    skipped.add(room_id)
        except Exception:
            logger.exception("自动扫描浏览器会话出错")
        await asyncio.sleep(AUTO_RESCAN_INTERVAL)


async def run(args: argparse.Namespace) -> bool:
    """运行房间客户端；返回 False 表示启动失败或（手动模式）所有房间都已停止。"""
    cookie = resolve_cookie(args.cookie)
    if not cookie:
        logger.info("未提供 cookie，将以游客身份监听（一般够用；若被风控请 %s）", COOKIE_FILE_HINT)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        api = BilibiliLiveAPI(session, cookie)
        await api.init_session_info()
        storage = SCStorage(args.output_dir)

        tasks: Dict[int, asyncio.Task] = {}
        supervisor_task: Optional[asyncio.Task] = None
        if args.auto:
            detected = find_open_live_rooms()
            if not detected:
                logger.error("未在浏览器（Edge/Chrome）会话中检测到直播间："
                             "请先在浏览器打开直播间页面再试，或手动传入房间号。")
                return False
            logger.info("浏览器中检测到直播间: %s", ", ".join(str(r) for r in sorted(detected)))
            for room_id in sorted(detected):
                _start_room_task(args, api, storage, room_id, tasks)
            if not tasks:
                logger.error("检测到的直播间均已被其他实例监听，无需重复启动。")
                return False
            supervisor_task = asyncio.create_task(
                _auto_supervisor(args, api, storage, tasks), name="auto-supervisor"
            )
        else:
            for room_id in args.rooms:
                client = RoomClient(api, room_id, storage)
                tasks[room_id] = asyncio.create_task(client.run(), name=f"room-{room_id}")

        flush_task = asyncio.create_task(_pending_flush_loop(storage), name="pending-flush")
        all_ok = True
        try:
            if args.duration > 0:
                wait_set = {supervisor_task} if supervisor_task else set(tasks.values())
                await asyncio.wait(wait_set, timeout=args.duration)
                logger.info("已运行 %.0f 秒，自动退出", args.duration)
            elif supervisor_task is not None:
                # auto 模式由监督协程动态管理房间，进程一直运行到被手动停止
                await asyncio.wait({supervisor_task})
            else:
                # 手动模式：所有房间都停止时（如被锁拒绝、接口失败），整个进程退出
                done, _pending = await asyncio.wait(set(tasks.values()))
                failed = [t for t in done if not t.cancelled() and t.exception() is not None]
                if failed:
                    all_ok = False
                    logger.error("%d 个房间异常退出，程序结束", len(failed))
                else:
                    logger.info("所有房间均已停止监听，程序退出")
        finally:
            everything = [*tasks.values(), flush_task]
            if supervisor_task is not None:
                everything.append(supervisor_task)
            for task in everything:
                task.cancel()
            await asyncio.gather(*everything, return_exceptions=True)
            storage.flush_all_pending()  # 退出前最后补写一次
        return all_ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows 控制台默认 GBK，SC 文本可能包含 emoji，统一改成 UTF-8 并容错
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.gui:
        from .gui_app import run_gui
        return run_gui(args.output_dir)
    if args.auto and args.rooms:
        parser.error("--auto 与手动指定的房间号不能同时使用")
    if not args.auto and not args.rooms:
        parser.error("需要提供房间号，或使用 --auto 自动检测浏览器里的直播间")
    setup_logging(args.verbose, Path(args.log_file) if args.log_file else None)
    try:
        all_ok = asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，已停止")
        return 0
    except (ApiError, aiohttp.ClientError, OSError) as exc:
        logger.error("运行失败: %s", exc)
        return 1
    return 0 if all_ok else 1
