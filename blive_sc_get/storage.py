"""SC 数据的本地持久化：JSONL 保存完整数据，CSV 保存关键字段（Excel 友好）。

目录结构::

    <base_dir>/
      room_<房间号>/
        sc_20260906.jsonl        每日完整记录（含 SC 原始字段与删除事件）
        sc_20260906.csv          每日关键信息，utf-8-sig 编码，Excel 可直接打开
        deleted_ids.json         被删除（退款）的 SC id -> 删除时间
        pending_records.jsonl    因文件被占用暂未写入的记录（重试队列，补写完自动删除）

CSV/JSONL 被其他程序（如 Excel）独占锁定导致写入失败时，save_sc 会把记录放入
按房间隔离的重试队列并持久化到磁盘；之后由定时任务补写。程序中途退出也没关系，
重启连接房间后会先从磁盘恢复队列继续补写，补写成功后队列文件自动删除。
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Union

logger = logging.getLogger(__name__)

JSONL_PREFIX = "sc_"
DELETED_FILE_NAME = "deleted_ids.json"
PENDING_FILE_NAME = "pending_records.jsonl"

CSV_COLUMNS = [
    "sc_id",
    "price_cny",
    "username",
    "uid",
    "message",
    "start_time",
    "end_time",
    "duration_sec",
    "received_time",
]


def _fmt_ts(value: Any) -> str:
    """把秒级时间戳格式化为本地时间字符串，无效值返回空串。"""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")
    return ""


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_field(value: str) -> str:
    """防 Excel 公式注入（OWASP CSV Injection）：以 = + - @ 或制表符开头的
    单元格内容前缀单引号，避免被 Excel 当作公式执行。前缀本身不显示。"""
    if value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


class SCStorage:
    """所有方法都应在事件循环内调用；写入均为小量追加，不做并发文件锁。"""

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir)
        # room_id -> 待补写记录列表，元素形如
        # {"sc": {...}, "received_at": iso, "jsonl_done": bool, "csv_done": bool}
        self._pending: Dict[int, List[Dict[str, Any]]] = {}

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _room_dir(self, room_id: int) -> Path:
        room_dir = self._base_dir / f"room_{room_id}"
        room_dir.mkdir(parents=True, exist_ok=True)
        return room_dir

    # ---------- 读取 ----------

    def load_known_sc_ids(self, room_id: int) -> Set[int]:
        """读取已有的 JSONL 记录，避免程序重启后把同一条 SC 重复写入。"""
        room_dir = self._base_dir / f"room_{room_id}"
        known: Set[int] = set()
        if not room_dir.is_dir():
            return known
        for path in sorted(room_dir.glob(f"{JSONL_PREFIX}*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        sc = record.get("sc")
                        if isinstance(sc, dict) and "id" in sc:
                            known.add(sc["id"])
            except OSError as exc:
                logger.warning("读取历史记录失败 %s: %s", path, exc)
        return known

    def load_pending(self, room_id: int) -> int:
        """从磁盘恢复该房间的重试队列（程序重启后调用），返回恢复的条数。"""
        path = self._base_dir / f"room_{room_id}" / PENDING_FILE_NAME
        items: List[Dict[str, Any]] = []
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(item, dict) and isinstance(item.get("sc"), dict):
                            items.append(item)
            except OSError as exc:
                logger.warning("读取重试队列失败 %s: %s", path, exc)
        self._pending[room_id] = items
        return len(items)

    # ---------- 写入 ----------

    def save_sc(self, room_id: int, sc: Dict[str, Any], received_at: datetime) -> bool:
        """写入一条 SC 记录；文件被占用时进入重试队列，返回是否全部写入成功。"""
        jsonl_ok = True
        csv_ok = True
        try:
            self._write_sc_jsonl(room_id, sc, received_at)
        except OSError as exc:
            jsonl_ok = False
            logger.warning("JSONL 写入失败，已加入重试队列 room=%s sc_id=%s: %s",
                           room_id, sc.get("id"), exc)
        try:
            self._write_sc_csv(room_id, sc, received_at)
        except OSError as exc:
            csv_ok = False
            logger.warning("CSV 写入失败，已加入重试队列 room=%s sc_id=%s: %s",
                           room_id, sc.get("id"), exc)
        if jsonl_ok and csv_ok:
            return True
        self._pending.setdefault(room_id, []).append({
            "sc": sc,
            "received_at": received_at.isoformat(timespec="seconds"),
            "jsonl_done": jsonl_ok,
            "csv_done": csv_ok,
        })
        self._persist_pending(room_id)
        return False

    def flush_pending(self, room_id: int) -> int:
        """立即补写该房间的重试队列，返回本次成功补写的条数。"""
        items = self._pending.get(room_id)
        if not items:
            return 0
        remaining: List[Dict[str, Any]] = []
        flushed = 0
        for item in items:
            try:
                sc = item["sc"]
                received_at = self._parse_received_at(item)
                # 只补写上次没写成功的那部分，避免重复行
                if not item.get("jsonl_done"):
                    self._write_sc_jsonl(room_id, sc, received_at)
                    item["jsonl_done"] = True
                if not item.get("csv_done"):
                    self._write_sc_csv(room_id, sc, received_at)
                    item["csv_done"] = True
                flushed += 1
            except (OSError, KeyError, TypeError) as exc:
                logger.debug("补写暂未成功 room=%s sc_id=%s: %s",
                             room_id, item.get("sc", {}).get("id"), exc)
                remaining.append(item)
        self._pending[room_id] = remaining
        self._persist_pending(room_id)
        if flushed:
            logger.info("重试队列已补写 %d 条 room=%s，剩余 %d 条",
                        flushed, room_id, len(remaining))
        return flushed

    def flush_all_pending(self) -> int:
        """补写所有房间的重试队列，由定时任务周期性调用。"""
        return sum(self.flush_pending(room_id) for room_id in list(self._pending))

    @staticmethod
    def _parse_received_at(item: Dict[str, Any]) -> datetime:
        try:
            return datetime.fromisoformat(item["received_at"])
        except (KeyError, TypeError, ValueError):
            return datetime.now()

    def _persist_pending(self, room_id: int) -> None:
        """把重试队列整体写盘（队列通常只有几条）；清空时删除文件。"""
        items = self._pending.get(room_id) or []
        path = self._base_dir / f"room_{room_id}" / PENDING_FILE_NAME
        try:
            if not items:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                for item in items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("重试队列写盘失败 room=%s: %s", room_id, exc)

    # ---------- 内部：单条写入（失败抛 OSError，由调用方决定是否入队） ----------

    def _write_sc_jsonl(self, room_id: int, sc: Dict[str, Any], received_at: datetime) -> None:
        room_dir = self._room_dir(room_id)
        date_str = received_at.strftime("%Y%m%d")
        record = {
            "type": "sc",
            "time_received": received_at.isoformat(timespec="seconds"),
            "room_id": room_id,
            "sc": sc,
        }
        with (room_dir / f"{JSONL_PREFIX}{date_str}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_sc_csv(self, room_id: int, sc: Dict[str, Any], received_at: datetime) -> None:
        room_dir = self._room_dir(room_id)
        path = room_dir / f"{JSONL_PREFIX}{received_at.strftime('%Y%m%d')}.csv"
        user = sc.get("user_info") or {}
        message = str(sc.get("message") or "").replace("\r\n", "\n")
        # 上次写表头时可能失败留下空文件，按大小而非存在性判断
        is_new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if is_new_file:
                writer.writerow(CSV_COLUMNS)
            writer.writerow([
                sc.get("id", ""),
                sc.get("price", ""),
                _sanitize_csv_field(user.get("uname") or sc.get("uname") or ""),
                sc.get("uid", ""),
                _sanitize_csv_field(message),
                _fmt_ts(sc.get("start_time")),
                _fmt_ts(sc.get("end_time")),
                sc.get("time", ""),
                received_at.isoformat(timespec="seconds"),
            ])

    def load_sc_history(self, room_id: int) -> List[Dict[str, Any]]:
        """按时间顺序读取该房间所有已保存的 SC 记录（跨所有日期文件）。"""
        room_dir = self._base_dir / f"room_{room_id}"
        records: List[Dict[str, Any]] = []
        if not room_dir.is_dir():
            return records
        for path in sorted(room_dir.glob(f"{JSONL_PREFIX}*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if record.get("type") == "sc" and isinstance(record.get("sc"), dict):
                            records.append({
                                "time_received": record.get("time_received", ""),
                                "sc": record["sc"],
                            })
            except OSError as exc:
                logger.warning("读取历史 SC 失败 %s: %s", path, exc)
        return records

    def iter_sc_history_desc(self, room_id: int):
        """从最新到最旧逐条产出 SC 记录（跨所有日期文件，倒序遍历）。"""
        room_dir = self._base_dir / f"room_{room_id}"
        if not room_dir.is_dir():
            return
        for path in sorted(room_dir.glob(f"{JSONL_PREFIX}*.jsonl"), reverse=True):
            try:
                with path.open("r", encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError as exc:
                logger.warning("读取历史 SC 失败 %s: %s", path, exc)
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") == "sc" and isinstance(record.get("sc"), dict):
                    yield {
                        "time_received": record.get("time_received", ""),
                        "sc": record["sc"],
                    }

    def load_sc_page(self, room_id: int, limit: int = 100, skip: int = 0) -> List[Dict[str, Any]]:
        """从最新一条往前跳过 skip 条后取最多 limit 条，按时间正序返回。

        GUI 分页加载用：先取最新一页，滚到顶再取更早的一页，
        避免历史记录很多时一次性读入全部内容。
        """
        page: List[Dict[str, Any]] = []
        taken = 0
        for record in self.iter_sc_history_desc(room_id):
            if taken < skip:
                taken += 1
                continue
            page.append(record)
            if len(page) >= limit:
                break
        page.reverse()
        return page

    def count_sc_records(self, room_id: int) -> int:
        """统计该房间已保存的 SC 记录总数。"""
        return sum(1 for _ in self.iter_sc_history_desc(room_id))

    def load_deleted_ids(self, room_id: int) -> Dict[str, str]:
        """读取被删除（退款）的 SC id -> 删除时间。"""
        deleted_path = self._base_dir / f"room_{room_id}" / DELETED_FILE_NAME
        if not deleted_path.exists():
            return {}
        try:
            data = json.loads(deleted_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def mark_deleted(self, room_id: int, sc_ids: Iterable[int], deleted_at: datetime) -> None:
        room_dir = self._room_dir(room_id)
        time_str = deleted_at.isoformat(timespec="seconds")
        record = {
            "type": "delete",
            "time_deleted": time_str,
            "room_id": room_id,
            "ids": list(sc_ids),
        }
        date_str = deleted_at.strftime("%Y%m%d")
        with (room_dir / f"{JSONL_PREFIX}{date_str}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        deleted_path = room_dir / DELETED_FILE_NAME
        existing: Dict[str, str] = {}
        if deleted_path.exists():
            try:
                existing = json.loads(deleted_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except ValueError:
                existing = {}
        for sc_id in record["ids"]:
            existing[str(sc_id)] = time_str
        deleted_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
