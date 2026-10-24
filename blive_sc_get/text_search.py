"""SC / 弹幕搜索的共享纯函数（ROADMAP 118）。

两版（Qt / Tk）共用同一套匹配、排序、计数与截断语义，避免行为漂移：

- SC 搜索覆盖**全部落盘历史**：数据源是 ``storage.iter_sc_history_desc``（最新 → 更旧），
  命中结果保持这个顺序（``index=0`` 就是最新一条），跳转时「下一个」走向更旧；
- 弹幕只搜**当前内存视图**（最近约 4000 行），本模块只负责行匹配与索引循环，
  具体文本控件的高亮由各视图实现。

这里刻意不 import 任何 UI 框架，便于在无 Qt / 无 Tk 环境跑单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

SC_SEARCH_PREVIEW_LEN = 80
"""搜索结果摘要的最大字符数（命中位置附近截取）。"""

SC_SEARCH_LIMIT = 2000
"""SC 全量搜索最多保留多少条命中（再多也没有逐个跳转的意义，且会拖慢界面）。"""


def normalize_query(text: Any) -> str:
    """查询词归一化：去首尾空白 + casefold（默认不区分大小写，中文无影响）。"""
    return str(text or "").strip().casefold()


def _record_parts(record: Any) -> Tuple[str, str, str]:
    """取出（时间、用户名、内容）三个可搜索字段。"""
    if not isinstance(record, dict):
        return "", "", ""
    sc = record.get("sc") if isinstance(record.get("sc"), dict) else {}
    user = sc.get("user") if isinstance(sc.get("user"), dict) else {}
    return (
        str(record.get("time_received") or ""),
        str(user.get("uname") or ""),
        str(sc.get("message") or ""),
    )


def sc_record_text(record: Any) -> str:
    """SC 记录的可搜索文本（与界面展示一致：时间 + 用户名 + 内容）。"""
    return " ".join(part for part in _record_parts(record) if part)


def sc_record_matches(record: Any, query: str) -> bool:
    """该条 SC 是否命中（空查询一律不命中，避免把「清空」当成全匹配）。"""
    keyword = normalize_query(query)
    return bool(keyword) and keyword in sc_record_text(record).casefold()


def sc_preview(record: Any, query: str = "",
               limit: int = SC_SEARCH_PREVIEW_LEN) -> str:
    """结果摘要：优先围绕命中位置截取内容；取不到命中时退回「用户名：内容」。"""
    _time, uname, message = _record_parts(record)
    body = message or uname
    keyword = normalize_query(query)
    text = body
    if keyword and text:
        position = text.casefold().find(keyword)
        if position >= 0:
            half = max(8, (limit - len(keyword)) // 2)
            start = max(0, position - half)
            end = min(len(text), start + limit)
            start = max(0, end - limit)
            snippet = text[start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(text):
                snippet = snippet + "…"
            return snippet
    if len(text) > limit:
        text = text[:limit] + "…"
    if uname and message:
        return f"{uname}：{text}"
    return text


@dataclass(frozen=True)
class ScSearchHit:
    """一条 SC 搜索命中（``index`` 为从最新数的序号，0 = 最新一条）。"""

    index: int
    sc_id: str
    time_received: str
    uname: str
    preview: str


def sc_hit_from_record(record: Any, index: int, query: str = "") -> ScSearchHit:
    """把一条 SC 记录转成命中项（调用方保证它确实命中）。"""
    sc = record.get("sc") if isinstance(record, dict) else {}
    sc = sc if isinstance(sc, dict) else {}
    user = sc.get("user") if isinstance(sc.get("user"), dict) else {}
    time_received, uname, _message = _record_parts(record)
    return ScSearchHit(
        index=int(index),
        sc_id=str(sc.get("id") or ""),
        time_received=time_received,
        uname=uname,
        preview=sc_preview(record, query),
    )


def iter_sc_hits(records_desc: Iterable[Any], query: str,
                 limit: int = SC_SEARCH_LIMIT) -> Tuple[List[ScSearchHit], bool]:
    """对「最新 → 更旧」的记录流做匹配，返回 ``(命中列表, 是否被截断)``。

    空查询直接返回空结果（不读流）。达到 ``limit`` 后继续扫到下一条命中即停——
    这样只需知道「还有更多」，不必把整份历史读完。
    """
    keyword = normalize_query(query)
    if not keyword or limit <= 0:
        return [], False
    hits: List[ScSearchHit] = []
    for index, record in enumerate(records_desc):
        if not sc_record_matches(record, keyword):
            continue
        if len(hits) >= limit:
            return hits, True
        hits.append(sc_hit_from_record(record, index, keyword))
    return hits, False


def dm_line_matches(line: Any, query: str) -> bool:
    """弹幕行是否命中（整行文本包含关键词，默认不区分大小写）。"""
    keyword = normalize_query(query)
    return bool(keyword) and keyword in str(line or "").casefold()


def dm_match_indices(lines: Iterable[Any], query: str) -> List[int]:
    """按文档顺序（旧 → 新）返回命中的行号；视图通常把最后一个当「最新」初始项。"""
    keyword = normalize_query(query)
    if not keyword:
        return []
    return [index for index, line in enumerate(lines)
            if keyword in str(line or "").casefold()]


def step_index(current: int, total: int, delta: int) -> int:
    """在 ``0..total-1`` 上循环移动（total<=0 返回 0）。"""
    if total <= 0:
        return 0
    return (int(current) + int(delta)) % total


def search_counter(current: int, total: int, *, truncated: bool = False) -> str:
    """搜索条右侧计数文案（无匹配 / 第 x/y 个 / 仅前 N 个）。"""
    if total <= 0:
        return "无匹配"
    shown = max(1, min(int(current) + 1, total))
    suffix = "+" if truncated else ""
    return f"第 {shown}/{total}{suffix} 个"


def sc_hit_tooltip(hit: ScSearchHit) -> str:
    """命中项悬浮/日志用的说明（时间 + 用户名 + 摘要）。"""
    parts = [part for part in (hit.time_received, hit.uname) if part]
    head = " · ".join(parts)
    return f"{head}\n{hit.preview}" if head else hit.preview
