"""Qt 版 SC（醒目留言）面板：富文本渲染 + 历史分页 + 未读徽标 + 头部信息。

原先这些逻辑全部内联在 ``qt_app.QtScMonitorApp`` 上（``self.sc_text`` / ``_history_gen`` /
``_loaded_count`` 等），宿主只支持「一个当前房间」；抽成**视图级**组件后，每个 SC 面板各自
持有自己的历史页码与未读计数，主窗口与「房间独立窗口」（ROADMAP 84）可以同时渲染不同房间
而互不干扰。

房间来源由 ``room_provider`` 注入：主视图注入「跟随宿主选中房间」，独立窗口注入固定房间。
其余依赖（``hub`` / ``ui_queue`` / 房间元数据字典 / 徽标工具）继续复用宿主实例，避免重复实现。

历史加载的**归属**由宿主按 token 路由（见 ``qt_app._poll_queue`` 的 ``history`` 分支）：
本面板发起请求时带上 ``token``（``id(self)``），宿主找到对应面板再回投，避免主窗口与子窗口
互相把对方的历史结果判掉。
"""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .gui_app import (
    HISTORY_PAGE_SIZE,
    LIVE_DURATION_REFRESH_S,
    SC_TITLE_MAX_LEN,
    build_sc_segments,
    live_duration_text,
    scroll_value_for_anchor,
    unseen_badge_text,
)
from .log_categories import CATEGORY_DATA, get_logger
from .qt_dm_panel import BottomHoldTextEdit

logger = logging.getLogger("gui_qt.sc")
log_data = get_logger(CATEGORY_DATA, "gui_qt.sc.data")

SC_PRICE_COLORS = {
    "time": "#888888",
    "user": "#0055cc",
    "del": "#aaaaaa",
    "info": "#888888",
    "price_0": "#1f1f1f",
    "price_50": "#b8860b",
    "price_100": "#cc4444",
    "price_500": "#9932cc",
}

# QTextCharFormat 自定义属性键（存到 fragment 上，供删除定位 / uid 点击）
_SC_ID_KEY = 0x101
_UID_KEY = 0x102

SC_AT_BOTTOM_TOLERANCE = 4
"""滚动条距底部多少像素以内视为「吸底」。"""

SC_HISTORY_MARKER_PREFIX = "（已读取"
"""历史分页状态行（「（已读取共 x 条…）」）的开头：翻页时要把旧的那条删掉，只留最上面
一条（与 Tk 版每次翻页先 ``delete("1.0", "2.0")`` 再写新标记同一意图）。"""


class ScPanel(QWidget):
    """SC 显示区：头部信息行（房间/主播/标题/同接/舰长/粉丝牌）+ 累计 SC + 文本区。"""

    def __init__(self, host, room_provider: Callable[[], Optional[int]]):
        super().__init__()
        self.host = host
        self._room_provider = room_provider
        # 视图级状态：每个面板各自持有（多窗口并存的前提）
        self._history_gen = 0
        self._loaded_count = 0
        self._has_more = False
        self._loading_more = False
        self._sc_unseen = 0
        self._header_tick = 0.0  # 头部行上次因「已播」刷新时刻（秒级节流用）
        self._build_ui()
        host.register_sc_panel(self)

    # ---------- 房间 ----------

    @property
    def room_id(self) -> Optional[int]:
        """本视图当前显示的房间号（主视图=宿主选中房间，独立窗口=固定房间）。"""
        return self._room_provider()

    @property
    def token(self) -> int:
        """历史请求的归属标识（宿主据此把结果投回发起面板）。"""
        return id(self)

    # ---------- UI ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # 头部：左侧直播标题 + 同接/舰长/粉丝牌，右侧累计 SC（同一行，省一行高度）
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        self.sc_header = QLabel("")
        self.sc_header.setWordWrap(False)  # 单行显示；过长与 Tk 版一样裁掉，完整内容见悬浮提示
        header_row.addWidget(self.sc_header, 1)
        self.sc_total_label = QLabel("")
        self.sc_total_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header_row.addWidget(self.sc_total_label, 0)
        layout.addLayout(header_row)

        self.sc_text = BottomHoldTextEdit()
        self.sc_text.setReadOnly(True)
        self.sc_text.setFont(QFont("Microsoft YaHei UI", 10))
        self.sc_text.setLineWrapMode(BottomHoldTextEdit.WidgetWidth)
        self.sc_text.verticalScrollBar().valueChanged.connect(self._on_sc_wheel)
        # 点击 SC 里的用户名 → 打开其个人空间（与 Tk 版 _on_sc_click 一致）
        self.sc_text.click_handler = self._on_sc_clicked
        layout.addWidget(self.sc_text, 1)
        self.sc_badge = self.host._make_unseen_badge(self.sc_text)
        self.sc_badge.clicked.connect(self._on_sc_badge_clicked)
        self.sc_badge.hide()

    # ---------- 每轮轮询 ----------

    def poll_tick(self) -> None:
        """100ms 轮询：记录吸底状态 + 刷新累计 SC + 每秒刷新「已播」时长。

        「已播」按秒跳动，用同一轮询做 1 秒节流（不新增定时器）；只在直播中且有起点时
        才重设头部文本，其余房间一次 ``setText`` 都不做。
        """
        self.sc_text.refresh_bottom_state()
        room_id = self.room_id
        if room_id is not None and self.host._sc_total.get(room_id):
            self.update_total()
        self._tick_header(room_id)

    def _tick_header(self, room_id: Optional[int]) -> None:
        """每秒刷新一次头部行（让「已播」的秒数往前走）。

        状态变化本身由宿主即时刷新头部（``_refresh_sc_header``），这里只补「时间在走」
        这一件事，因此不需要在毫秒级轮询里反复重设文本。
        """
        now = time.monotonic()
        if now - self._header_tick < LIVE_DURATION_REFRESH_S:
            return
        self._header_tick = now
        if room_id is None or self.host.live_state.get(room_id) != "直播中":
            return
        if self.host.live_started_at.get(room_id) is None:
            return
        self.update_header()

    def sync_unseen_badge(self) -> None:
        """同步未读徽标：回到底部即清零隐藏。"""
        if self._sc_unseen and self._sc_at_bottom():
            self._sc_unseen = 0
        self.host._place_unseen_badge(
            self.sc_badge, self.sc_text, unseen_badge_text(self._sc_unseen, "SC"))

    # ---------- 房间切换 / 清空 ----------

    def apply_room(self, room_id: Optional[int]) -> None:
        """宿主（或独立窗口）切换本视图房间后的刷新：头部 + 累计 + 重载历史。

        SC 文本区**不立即清空**（与原实现一致）：等历史加载回来时再清，期间先显示
        「正在加载历史 SC…」提示；切到「无选中房间」时才清空。
        """
        if room_id is None:
            self.clear_view()
            return
        self.update_header()
        self.update_total()
        self.load_history(room_id)

    def clear_view(self) -> None:
        self.sc_text.clear()
        self._sc_unseen = 0
        self.sc_badge.hide()
        self._history_gen += 1

    # ---------- 历史分页 ----------

    def load_history(self, room_id: int, skip: int = 0) -> None:
        self._history_gen += 1
        gen = self._history_gen
        log_data.debug("读取历史 SC：房间 %s，skip=%d", room_id, skip)
        storage = self.host.hub.storage
        if storage is None:
            self.append_info("（后台网络初始化中，稍后会自动加载历史 SC…）")
            return
        if skip == 0:
            self._loaded_count = 0
            self._has_more = False
            self._loading_more = False
            self.append_info("（正在加载历史 SC…）")
        else:
            self._loading_more = True

        token = self.token
        ui_queue = self.host.ui_queue

        def worker() -> None:
            try:
                page = storage.load_sc_page(room_id, limit=HISTORY_PAGE_SIZE, skip=skip)
                deleted = storage.load_deleted_ids(room_id)
                total = storage.count_sc_records(room_id)
            except Exception:
                log_data.exception("读取历史 SC 失败 room=%s", room_id)
                page, deleted, total = [], {}, 0
            ui_queue.put(("history", {
                "gen": gen, "room_id": room_id, "records": page, "deleted": deleted,
                "total": total, "skip": skip, "token": token,
            }))

        threading.Thread(target=worker, name=f"history-{room_id}", daemon=True).start()

    def on_history_loaded(self, payload: dict) -> None:
        """宿主按 token 路由回来的历史结果（只处理本面板发起的那一次）。"""
        if payload["gen"] != self._history_gen:
            return
        if payload["room_id"] != self.room_id:
            return
        room_id = payload["room_id"]
        records = payload["records"]
        deleted = payload["deleted"]
        total = int(payload.get("total") or 0)
        skip = int(payload.get("skip") or 0)
        self.host._sc_total[room_id] = total
        self.update_total()
        self._loading_more = False
        self._loaded_count = skip + len(records)
        self._has_more = self._loaded_count < total
        log_data.debug("历史 SC 读取完成：房间 %s，skip=%d，本页 %d 条，累计 %d/%d 条",
                       room_id, skip, len(records), self._loaded_count, total)
        marker = self._history_marker(self._loaded_count, total)
        if skip == 0:
            self.clear_view()
            if not records:
                self.append_info("（尚无 SC 记录，收到新 SC 后会实时显示在这里）")
                return
            for record in records:
                self._render_record(record, deleted)
            self.append_info(f"（历史 {total} 条sc记录）")
            self._prepend_info(marker)
            self._scroll_to_bottom()
            return
        # 向上翻页：更早记录插在顶部，最终顺序与 Tk 版一致 ——
        # [新标记][本页记录][（已读取 x 条历史记录）][旧内容…]
        anchor = self._top_anchor()
        self._drop_top_marker()  # 旧的顶部状态行删掉，只留最新一条
        # 插入顺序＝最终顺序的倒序（都是往文档最前端插）：分割点先铺在旧内容顶上，
        # 本页记录再插到它前面，最后放顶部状态行
        self._prepend_info(f"（已读取 {skip} 条历史记录）")
        block = self._pack_records(records, deleted)
        self._prepend_block(block)
        self._prepend_info(marker)
        self._restore_top_anchor(anchor)

    def _maybe_load_more(self) -> None:
        room_id = self.room_id
        if (room_id is None or not self._has_more
                or self._loading_more or self.host.hub.storage is None):
            return
        if self.sc_text.verticalScrollBar().value() > 0:
            return
        self.load_history(room_id, skip=self._loaded_count)

    # ---------- 渲染 ----------

    def append_sc(self, time_str: str, sc: dict, *,
                  deleted: bool = False, pending: bool = False,
                  count_unseen: bool = False) -> None:
        follow = self._sc_at_bottom()
        segments = build_sc_segments(time_str, sc, deleted=deleted, pending=pending)
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        sc_id = sc.get("id")
        for idx, (chunk, tag) in enumerate(segments):
            fmt = QTextCharFormat()
            tag = (tag or "").strip()
            seg_tags = set(tag.split()) if tag else set()
            color = None
            for name, color_hex in SC_PRICE_COLORS.items():
                if name in seg_tags:
                    color = QColor(color_hex)
                    break
            if color is not None:
                fmt.setForeground(color)
            if idx < len(segments) - 1 and sc_id is not None:
                fmt.setProperty(_SC_ID_KEY, str(sc_id))
            user_tag = next((t for t in seg_tags if t.startswith("uid:")), None)
            if user_tag:
                fmt.setProperty(_UID_KEY, int(user_tag.split(":", 1)[1]))
            cursor.insertText(chunk, fmt)
        # segments 末尾已含 "\n"（Qt 里会另起一个文本块），不能再 insertBlock，
        # 否则每条 SC 后面会多出一整行空行（行距翻倍）
        if follow:
            self._scroll_to_bottom()
        elif count_unseen:
            # 用户正在向上翻阅：累加未读计数并显示「N 条新SC ↓」徽标
            self._sc_unseen += 1
            self.sync_unseen_badge()

    def append_info(self, text: str) -> None:
        follow = self._sc_at_bottom()
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(SC_PRICE_COLORS["info"]))
        cursor.insertText(text + "\n", fmt)
        if follow:
            self._scroll_to_bottom()

    def _drop_top_marker(self) -> None:
        """删掉顶部的历史分页状态行（仅当它确实在顶部时才删）。

        Tk 版每次翻页都先 ``delete("1.0", "2.0")`` 再写新标记；Qt 版此前「只插不删」，
        翻几次页顶部就会叠上好几行「已读取共 x 条历史记录」，看着像出了错。
        """
        first = self.sc_text.document().begin()
        if not first.isValid() or not first.text().startswith(SC_HISTORY_MARKER_PREFIX):
            return
        cursor = self.sc_text.textCursor()
        cursor.setPosition(first.position())
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()  # 清空这一行的内容
        if first.next().isValid():
            cursor.deleteChar()  # 再吃掉块分隔符，让整行彻底消失（勿在末块上删）

    def _top_anchor(self) -> Tuple[int, int]:
        """记下「视口顶端那条记录」的文档位置与当时的字符总数（插入更早记录前调用）。

        顶部若正好是分页状态行本身，就往下取到下一条记录再当锚点：那条状态行在翻页时
        会被删掉重写，拿它当锚点位置会算偏（且新标记文本长度也会变），而用户眼睛盯着
        的其实是它下面那条记录。
        """
        document = self.sc_text.document()
        position = self.sc_text.cursorForPosition(QPoint(0, 0)).position()
        block = document.findBlock(position)
        if block.isValid() and block.text().startswith(SC_HISTORY_MARKER_PREFIX):
            nxt = block.next()
            if nxt.isValid():
                position = nxt.position()
        return (position, document.characterCount())

    def _restore_top_anchor(self, anchor: Tuple[int, int]) -> None:
        """把视图按「最小滚动」摆好，等价于 Tk 版 ``see(旧顶行 + 新增行数)``。

        更早的记录是**插在文档最前端**的：Qt 插入内容后不会自己调整滚动条，视口仍停在
        数值 0，于是直接显示了「整段历史的最开头」（刚加载那批里最早的一条）——这就是
        ROADMAP 95 最初报的「触发时直接跳到了顶部」。

        但**也不能把锚点钉在视口顶边**（第一版返工就是这么写的）：那样新加载的一整页
        全落在视口上方、画面看起来和加载前**一模一样**，用户会以为「滚到顶没触发加载」。
        Tk 的 ``see()`` 是**最小滚动**——目标行在视口下方时只把它带到**底边**，于是新加载
        的那一页立刻填满视口（接缝停在底边，正好是用户想看的「从第 101 条开始」）。
        这里照搬同一语义：锚点在视口下方 → 贴底边；在视口上方 → 贴顶边；已经在视口内
        → 一点不动（避免无谓跳动）。

        ``cursorRect`` 给的是**视口坐标**，加上当前滚动值即锚点在文档中的纵向位置。
        """
        position, chars_before = anchor
        document = self.sc_text.document()
        delta = document.characterCount() - chars_before
        if delta <= 0:
            return
        cursor = self.sc_text.textCursor()
        cursor.setPosition(min(position + delta, document.characterCount() - 1))
        bar = self.sc_text.verticalScrollBar()
        rect = self.sc_text.cursorRect(cursor)
        bar.setValue(scroll_value_for_anchor(
            rect.top(), rect.height(), self.sc_text.viewport().height(), bar.value()))
        log_data.debug("历史翻页后视口：锚点行顶 %.0f、视口高 %d → 滚动条 %d",
                       rect.top(), self.sc_text.viewport().height(), bar.value())

    def _prepend_info(self, text: str) -> None:
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.Start)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(SC_PRICE_COLORS["info"]))
        cursor.insertText(text + "\n", fmt)

    def _prepend_block(self, records: list) -> None:
        cursor = self.sc_text.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for time_str, sc, deleted in records:
            segments = build_sc_segments(time_str, sc, deleted=deleted)
            sc_id = sc.get("id")
            for idx, (chunk, tag) in enumerate(segments):
                fmt = QTextCharFormat()
                tag = (tag or "").strip()
                seg_tags = set(tag.split()) if tag else set()
                for name, color_hex in SC_PRICE_COLORS.items():
                    if name in seg_tags:
                        fmt.setForeground(QColor(color_hex))
                        break
                if idx < len(segments) - 1 and sc_id is not None:
                    fmt.setProperty(_SC_ID_KEY, str(sc_id))
                cursor.insertText(chunk, fmt)
            # 同上：末尾 "\n" 已换行，勿再 insertBlock（避免空行）

    def _pack_records(self, records: list, deleted: dict) -> list:
        block: list = []
        for record in records:
            sc = record["sc"]
            block.append((record["time_received"], sc,
                          str(sc.get("id")) in deleted))
        return block

    def _render_record(self, record: dict, deleted: dict) -> None:
        sc = record["sc"]
        self.append_sc(record["time_received"], sc,
                       deleted=str(sc.get("id")) in deleted)

    def mark_sc_deleted(self, sc_id) -> None:
        """实时标记已删除/退款的 SC：整条置灰并在行尾追加说明。"""
        doc = self.sc_text.document()
        target = str(sc_id)
        block = doc.begin()
        while block.isValid():
            # PySide6 未绑定 C++ 的 QTextBlock::textLayout()（调用即 AttributeError，
            # 异常在槽函数里被吞掉 → 表现为「删除标记不生效」）。改用块长度判空块。
            if block.length() > 1:
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid() and frag.charFormat().property(_SC_ID_KEY) == target:
                        # 整块置灰 + 行尾追加说明
                        cursor = self.sc_text.textCursor()
                        cursor.setPosition(block.position())
                        cursor.select(QTextCursor.BlockUnderCursor)
                        del_fmt = QTextCharFormat()
                        del_fmt.setForeground(QColor(SC_PRICE_COLORS["del"]))
                        cursor.mergeCharFormat(del_fmt)
                        cursor.movePosition(QTextCursor.EndOfBlock)
                        cursor.insertText("  （已删除，退款）", del_fmt)
                        return
                    it += 1
            block = block.next()
        self.append_info(f"SC {sc_id} 已被删除（退款）")

    # ---------- 头部 / 滚动 / 点击 ----------

    def update_header(self) -> None:
        """刷新头部行：房间号 · 主播 · 标题 · 同接 · 舰长 · 粉丝牌。"""
        room_id = self.room_id
        if room_id is None:
            return
        anchor = self.host.anchor_names.get(room_id) or "未知主播"
        title = self.host.titles.get(room_id) or ""
        parts = [f"房间 {room_id} · {anchor}"]
        if title:
            if len(title) > SC_TITLE_MAX_LEN:
                title = title[:SC_TITLE_MAX_LEN] + "…"
            parts.append(f"标题：{title}")
        viewers = self.host.viewers.get(room_id)
        guards = self.host.guard_num.get(room_id)
        if viewers:
            parts.append(f"同接 {viewers}")
        if guards is not None and guards >= 0:
            parts.append(f"舰长 {guards}")
        # 「已播 01:23:45」：仅直播中且有起点时出现（与 Tk 版同一套判断与格式）
        duration = live_duration_text(self.host.live_started_at.get(room_id),
                                      self.host.live_state.get(room_id) == "直播中")
        if duration:
            parts.append(duration)
        medal_name, medal_level = self.host._fan_medal_for(room_id)
        if medal_name:
            parts.append(f"粉丝牌 {medal_name} Lv{medal_level}")
        text = " · ".join(parts)
        self.sc_header.setText(text)
        self.sc_header.setToolTip(text)  # 单行显示：过长时靠悬浮查看完整内容

    def update_total(self) -> None:
        room_id = self.room_id
        total = self.host._sc_total.get(room_id, 0)
        self.sc_total_label.setText(f"累计 SC：{total}" if room_id else "")

    def _on_sc_clicked(self, pos) -> None:
        """点击 SC 中的用户名 → 打开其个人空间（与 Tk 版 _on_sc_click 一致）。"""
        cursor = self.sc_text.cursorForPosition(pos)
        uid = int(cursor.charFormat().property(_UID_KEY) or 0)
        if uid:
            webbrowser.open(f"https://space.bilibili.com/{uid}")

    def _on_sc_wheel(self, _value: int) -> None:
        if self.sc_text.verticalScrollBar().value() <= 0:
            self._maybe_load_more()

    def _sc_at_bottom(self) -> bool:
        bar = self.sc_text.verticalScrollBar()
        return bar.maximum() - bar.value() <= SC_AT_BOTTOM_TOLERANCE

    def _scroll_to_bottom(self) -> None:
        bar = self.sc_text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _history_marker(self, loaded: int, total: int) -> str:
        if loaded < total:
            return f"（已读取共 {loaded} 条历史记录，向上滚动加载更早记录）"
        return f"（已读取全部 {loaded} 条历史记录）"

    def _on_sc_badge_clicked(self) -> None:
        self._sc_unseen = 0
        self._scroll_to_bottom()
        self.sync_unseen_badge()
