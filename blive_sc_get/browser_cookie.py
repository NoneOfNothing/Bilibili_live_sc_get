"""从本机浏览器 Cookie 数据库中提取 B 站 Cookie（GUI 一键获取功能）。

支持：
- Chromium 系：Edge / Chrome / Brave / Vivaldi / Opera / Chromium（各 profile）
  Cookie 经 DPAPI 保护的主密钥 + AES-GCM 加密（v10）；新版 v20（App-Bound
  Encryption）暂无法解密，会在结果中注明。
- Firefox：cookies.sqlite 明文存储（未设置主密码时），直接读取。

优先级：正在运行的浏览器优先，自动选择第一份可用 Cookie；带 SESSDATA
（已登录）的优先于未登录的。读取时先把数据库复制到临时目录再打开，
避免与正在运行的浏览器争锁。隐私：仅读取 bilibili.com 域名的 Cookie，
只写本地 cookie.txt，不做任何上传。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

logger = logging.getLogger(__name__)

BILIBILI_DOMAIN_PATTERN = "%bilibili.com"

_CHROMIUM_BASES = [
    ("Edge", Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"),
    ("Chrome", Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"),
    ("Brave", Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "User Data"),
    ("Vivaldi", Path(os.environ.get("LOCALAPPDATA", "")) / "Vivaldi" / "User Data"),
    ("Chromium", Path(os.environ.get("LOCALAPPDATA", "")) / "Chromium" / "User Data"),
    ("Opera", Path(os.environ.get("LOCALAPPDATA", "")) / "Opera Software"),
]

_FIREFOX_PROFILES_DIR = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "Profiles"

_BROWSER_PROCESS_NAMES = {
    "msedge.exe": "Edge",
    "chrome.exe": "Chrome",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
    "opera.exe": "Opera",
    "firefox.exe": "Firefox",
}


def is_bilibili_host(host: str) -> bool:
    host = (host or "").lstrip(".").lower()
    return host == "bilibili.com" or host.endswith(".bilibili.com")


def build_cookie_string(rows) -> str:
    """把 (name, value) 序列组装成请求头用的 cookie 字符串。"""
    return "; ".join(f"{name}={value}" for name, value in rows)


def running_browser_names():
    """返回当前正在运行的受支持浏览器显示名集合（失败返回空集）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    running = set()
    for line in out.splitlines():
        if '","' not in line:
            continue
        exe = line.split('","')[0].strip('"').lower()
        name = _BROWSER_PROCESS_NAMES.get(exe)
        if name:
            running.add(name)
    return running


def dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI 解密（当前用户上下文），用于 Local State 主密钥。"""
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI 解密失败（CryptUnprotectData）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
def load_chromium_aes_key(user_data_dir):
    """从 Local State 解出 Chromium 系浏览器的 AES 主密钥。"""
    local_state = user_data_dir / "Local State"
    data = json.loads(local_state.read_text(encoding="utf-8"))
    encrypted_key = base64.b64decode(data["os_crypt"]["encrypted_key"])
    if not encrypted_key.startswith(b"DPAPI"):
        raise ValueError("Local State 主密钥缺少 DPAPI 前缀")
    return dpapi_unprotect(encrypted_key[5:])


def decrypt_chromium_value(encrypted, aes_key):
    """解密单条 Chromium cookie。v10=AES-GCM；v20=App-Bound 加密（返回 None）。"""
    if AES is None:
        raise RuntimeError("未安装 pycryptodome，无法解密浏览器 Cookie")
    if encrypted.startswith(b"v10") and len(encrypted) > 31:
        nonce, payload = encrypted[3:15], encrypted[15:]
        cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
        try:
            plain = cipher.decrypt_and_verify(payload[:-16], payload[-16:])
        except ValueError:
            return None
        return plain.decode("utf-8", errors="replace")
    if encrypted.startswith(b"v20") or encrypted.startswith(b"app_bound"):
        return None
    if encrypted.startswith(b"DPAPI"):
        try:
            return dpapi_unprotect(encrypted[5:]).decode("utf-8", errors="replace")
        except OSError:
            return None
    return None


def _iter_chromium_profiles(base):
    """枚举 User Data 下含 Cookies 数据库的 profile 目录。"""
    if not base.is_dir():
        return
    for profile in sorted(base.iterdir()):
        if not profile.is_dir() or profile.name == "System Profile":
            continue
        for cookie_path in (profile / "Network" / "Cookies", profile / "Cookies"):
            if cookie_path.exists():
                yield profile, cookie_path
                break


def _copy_for_read(path, td, name):
    """把数据库文件（含 wal/shm）复制到临时目录（immutable 直读失败的回退）。"""
    copy = Path(td) / name
    shutil.copyfile(path, copy)
    for suffix in ("-wal", "-shm"):
        extra = path.with_name(path.name + suffix)
        if extra.exists():
            shutil.copyfile(extra, Path(td) / (name + suffix))
    return copy


def _lock_hint(exc: Exception) -> str:
    """共享冲突（浏览器运行中独占锁定数据库）时给出明确指引。"""
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (32, 33):
        return "（该浏览器正在运行并锁定了 Cookie 数据库，请完全退出此浏览器后重试）"
    return ""


def _fetch_rows(db_path, name, sql, params):
    """读取可能被浏览器锁定的 SQLite 数据库。

    优先以 immutable=1 只读 URI 直接读（绕开浏览器锁）；失败再把文件
    复制到临时目录读取。
    """
    try:
        uri = db_path.as_uri() + "?immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("immutable 直读失败 %s: %s", db_path, exc)
    with tempfile.TemporaryDirectory(prefix="blive_cookie_") as td:
        copy = _copy_for_read(db_path, td, name)
        conn = sqlite3.connect(str(copy))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
def _extract_chromium(source, base, errors, key_cache):
    """提取一个 Chromium 系浏览器的 B 站 cookie，返回 (cookie, 来源描述) 或 None。"""
    if base not in key_cache:
        try:
            key_cache[base] = load_chromium_aes_key(base)
        except Exception as exc:
            key_cache[base] = None
            errors.append(f"{source}: 读取主密钥失败（{exc}）")
            return None
    aes_key = key_cache[base]
    if aes_key is None:
        return None

    best = None
    for profile, cookie_path in _iter_chromium_profiles(base):
        try:
            rows = _fetch_rows(
                cookie_path, "Cookies",
                "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?",
                (BILIBILI_DOMAIN_PATTERN,))
        except (OSError, sqlite3.Error) as exc:
            hint = _lock_hint(exc)
            errors.append(f"{source}({profile.name}): Cookie 数据库读取失败（{exc}）{hint}")
            continue
        pairs = []
        undecryptable = 0
        for name, encrypted in rows:
            if not encrypted:
                continue
            value = decrypt_chromium_value(bytes(encrypted), aes_key)
            if value is None:
                undecryptable += 1
                continue
            pairs.append((name, value))
        if undecryptable:
            errors.append(f"{source}({profile.name}): 有 {undecryptable} 条新版 App-Bound 加密 Cookie 暂无法解密")
        if not pairs:
            continue
        cookie = build_cookie_string(pairs)
        has_sessdata = any(n == "SESSDATA" for n, _v in pairs)
        if has_sessdata:
            return cookie, f"{source}（{profile.name}）"
        if best is None:
            best = (cookie, f"{source}（{profile.name}），未登录（无 SESSDATA）")
    return best


def _extract_firefox(source, profiles_dir, errors):
    """提取 Firefox 的 B 站 cookie（未设主密码时为明文）。"""
    if not profiles_dir.is_dir():
        return None
    for profile in sorted(profiles_dir.iterdir()):
        db = profile / "cookies.sqlite"
        if not db.exists():
            continue
        try:
            rows = _fetch_rows(
                db, "cookies.sqlite",
                "SELECT name, value FROM moz_cookies WHERE domain LIKE ?",
                (BILIBILI_DOMAIN_PATTERN,))
        except (OSError, sqlite3.Error) as exc:
            hint = _lock_hint(exc)
            errors.append(f"{source}({profile.name}): Cookie 数据库读取失败（{exc}）{hint}")
            continue
        if not rows:
            continue
        cookie = build_cookie_string(rows)
        has_sessdata = any(n == "SESSDATA" for n, _v in rows)
        desc = f"{source}（{profile.name}）"
        if has_sessdata:
            return cookie, desc
        return cookie, desc + "，未登录（无 SESSDATA）"
    return None


def get_bilibili_cookie():
    """按优先级扫描本机浏览器，返回 (cookie, 来源描述, 错误列表)。

    优先级：正在运行的浏览器 -> 其余浏览器；同一浏览器内带 SESSDATA
    （已登录）的 profile 优先。未找到时 cookie 为 None，errors 说明原因。
    """
    errors = []
    key_cache: Dict[Path, Optional[bytes]] = {}
    running = running_browser_names()
    sources = [("Firefox", _FIREFOX_PROFILES_DIR)]
    for name, base in _CHROMIUM_BASES:
        sources.append((name, base))
    sources.sort(key=lambda s: s[0] not in running)

    fallback = None
    for source, target in sources:
        if source == "Firefox":
            found = _extract_firefox(source, target, errors)
        else:
            found = _extract_chromium(source, target, errors, key_cache)
        if found is None:
            continue
        cookie, desc = found
        if "无 SESSDATA" not in desc:
            return cookie, desc, errors
        if fallback is None:
            fallback = (cookie, desc)
    if fallback is not None:
        return fallback[0], fallback[1], errors
    if not errors:
        errors.append("未在本机受支持的浏览器中找到 bilibili.com 的 Cookie")
    return None, None, errors
