"""B 站直播弹幕 WebSocket 协议的封包与解包。

报文头共 16 字节（大端）::

    | 4B 整包长度 | 2B 头部长度(=16) | 2B 协议版本 | 4B 操作码 | 4B 序列号 |

协议版本：
    0/1 —— 正文即 JSON（或心跳应答的人气值等原始数据）
    2   —— 正文为 zlib 压缩，解压后是若干个完整子报文
    3   —— 正文为 brotli 压缩，解压后是若干个完整子报文
"""

from __future__ import annotations

import enum
import struct
import zlib
from typing import Iterator, List, Tuple

try:
    import brotli
except ImportError:  # pragma: no cover
    brotli = None

HEADER_STRUCT = struct.Struct(">IHHII")
HEADER_SIZE = HEADER_STRUCT.size

PROTOCOL_RAW = 0
PROTOCOL_ZLIB = 2
PROTOCOL_BROTLI = 3


class ProtocolError(Exception):
    """报文解析或解压失败。"""


class Operation(enum.IntEnum):
    HEARTBEAT = 2        # 客户端心跳
    HEARTBEAT_REPLY = 3  # 服务器心跳应答，正文前 4 字节为当前人气值
    MESSAGE = 5          # 弹幕 / 事件消息，正文为 JSON
    USER_AUTH = 7        # 客户端认证包
    AUTH_REPLY = 8       # 服务器认证应答，正文为 {"code": 0}


def build_packet(operation: int, body: bytes = b"", protocol: int = PROTOCOL_RAW,
                 sequence: int = 1) -> bytes:
    """按协议格式构造一个完整报文。"""
    header = HEADER_STRUCT.pack(HEADER_SIZE + len(body), HEADER_SIZE, protocol, operation, sequence)
    return header + body


def iter_packets(data: bytes) -> Iterator[Tuple[int, int, bytes]]:
    """把一段缓冲区拆成 (协议版本, 操作码, 正文) 序列。"""
    total = len(data)
    offset = 0
    while offset < total:
        if total - offset < HEADER_SIZE:
            raise ProtocolError(f"报文头不完整，剩余 {total - offset} 字节")
        pack_len, header_len, protocol, operation, _seq = HEADER_STRUCT.unpack_from(data, offset)
        if pack_len < header_len or offset + pack_len > total:
            raise ProtocolError(f"报文长度异常: pack_len={pack_len}, offset={offset}, total={total}")
        yield protocol, operation, data[offset + header_len:offset + pack_len]
        offset += pack_len


def _decompress(protocol: int, body: bytes) -> bytes:
    if protocol == PROTOCOL_ZLIB:
        return zlib.decompress(body)
    if protocol == PROTOCOL_BROTLI:
        if brotli is None:
            raise ProtocolError("收到 brotli 压缩报文但未安装 Brotli 库（pip install Brotli）")
        return brotli.decompress(body)
    raise ProtocolError(f"未知的协议版本: {protocol}")


def flatten_packets(data: bytes) -> List[Tuple[int, int, bytes]]:
    """递归解压，展开成最内层的 (协议版本, 操作码, 正文) 列表。"""
    result: List[Tuple[int, int, bytes]] = []
    pending = [data]
    while pending:
        for protocol, operation, body in iter_packets(pending.pop()):
            if protocol in (PROTOCOL_ZLIB, PROTOCOL_BROTLI):
                pending.append(_decompress(protocol, body))
            else:
                result.append((protocol, operation, body))
    return result
