"""Qt 版粉丝牌页模块。

复用 Tk 版（gui_app）的纯函数与 MedalTaskRunner 业务层；本模块只承载
粉丝牌列表 / 房间任务表 / 手动执行按钮 / 自动开关 的 UI 与事件绑定。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Union

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .gui_app import MEDAL_TASK_REFRESH_GAP_S
from .medal_runner import MedalTaskRunner
from .medal_tasks import (
    TASK_LIKE,
    TASK_SEND_DANMAKU,
    TASK_WATCH_LIVE,
    find_task,
    is_task_complete,
    task_label,
)

from .log_categories import CATEGORY_TASK, get_logger

logger = logging.getLogger("gui_qt.medal")
log_task = get_logger(CATEGORY_TASK, "gui_qt.medal.task")

# 跳转列映射（列索引 → 目标类型）
MEDAL_LINK_COLUMNS = {2: "space", 3: "room"}        # 粉丝牌表：主播/房间
MEDAL_TASK_LINK_COLUMNS = {0: "room", 1: "space"}   # 任务表：房间/主播


def format_medal_task(tasks: list, jump_type: str) -> str:
    """按跳转类型找任务并格式化进度（与 Tk 版 _format_medal_task 一致）。"""
    for task in tasks or []:
        if task.get("jump_type") != jump_type:
            continue
        limit = int(task.get("limit") or 0)
        current = int(task.get("current") or 0)
        if is_task_complete(task):
            return f"{current}/{limit} 完成" if limit else "已完成"
        if limit <= 0:
            return "仅点亮"
        return f"{current}/{limit}"
    return "—"


class MedalTab(QWidget):
    """粉丝牌页：持有的粉丝牌 + 监听房间任务 + 手动执行 + 自动开关。"""

    def __init__(self, host):
        super().__init__()
        self.host = host
        self._medals: list = []
        self._medal_levels_room: Dict[int, int] = {}
        self._medal_levels_uid: Dict[int, int] = {}
        self._medal_names_room: Dict[int, str] = {}
        self._medal_names_uid: Dict[int, str] = {}
        self._medal_uid_by_iid: Dict[int, int] = {}
        self._medal_room_by_iid: Dict[int, int] = {}
        self._medal_tasks: Dict[int, dict] = {}
        self._medal_running: set = set()
        self._pending = None
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        from .qt_app import ProtectedLinkTable, compact_row_height

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)  # 紧凑布局：Qt 默认边距比 Tk 大一截
        outer.setSpacing(4)

        self.medal_status = QLabel("")
        self.medal_status.setStyleSheet("color:#666;")
        outer.addWidget(self.medal_status)

        outer.addWidget(QLabel("我持有的粉丝牌（点击主播 / 房间可跳转）："))

        # 主播 / 房间号列为「跳转列」：点击不改变选中行（点击保护）
        self.medal_tree = ProtectedLinkTable(0, 7)
        self.medal_tree.link_columns = (2, 3)
        self.medal_tree.setHorizontalHeaderLabels(
            ["粉丝牌", "等级", "主播", "房间号", "今日亲密度", "当前/升级需", "状态"])
        self.medal_tree.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.medal_tree.setSelectionBehavior(QTableWidget.SelectRows)
        self.medal_tree.setSelectionMode(QTableWidget.SingleSelection)
        self.medal_tree.linkClicked.connect(self._on_medal_cell_clicked)
        # 行高按字体紧凑设置（Qt 默认行内边距偏大）
        self.medal_tree.verticalHeader().setDefaultSectionSize(
            compact_row_height(self.medal_tree))
        outer.addWidget(self.medal_tree, 1)

        outer.addWidget(QLabel("监听房间 · 粉丝牌任务（每日刷新，上限随等级提升；点击主播 / 房间可跳转）："))

        # 房间号 / 主播列为「跳转列」：点击不改变选中行，避免顺手改掉要执行任务的房间
        self.medal_task_tree = ProtectedLinkTable(0, 7)
        self.medal_task_tree.link_columns = (0, 1)
        self.medal_task_tree.setHorizontalHeaderLabels(
            ["房间号", "主播", "粉丝牌", "观看直播", "发弹幕", "点赞", "自动"])
        self.medal_task_tree.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.medal_task_tree.setSelectionBehavior(QTableWidget.SelectRows)
        self.medal_task_tree.setSelectionMode(QTableWidget.SingleSelection)
        self.medal_task_tree.itemSelectionChanged.connect(self._on_task_selected)
        self.medal_task_tree.linkClicked.connect(self._on_task_cell_clicked)
        self.medal_task_tree.verticalHeader().setDefaultSectionSize(
            compact_row_height(self.medal_task_tree))
        outer.addWidget(self.medal_task_tree, 1)

        self.medal_hint = QLabel("")
        self.medal_hint.setWordWrap(True)
        self.medal_hint.setStyleSheet("color:#555;")
        outer.addWidget(self.medal_hint)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        self.medal_danmaku_btn = QPushButton("发弹幕")
        self.medal_danmaku_btn.clicked.connect(
            lambda: self._complete_selected(TASK_SEND_DANMAKU))
        btns.addWidget(self.medal_danmaku_btn)
        self.medal_like_btn = QPushButton("点赞")
        self.medal_like_btn.clicked.connect(
            lambda: self._complete_selected(TASK_LIKE))
        btns.addWidget(self.medal_like_btn)
        btns.addStretch(1)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._on_refresh)
        btns.addWidget(refresh_btn)
        outer.addLayout(btns)

        auto = QHBoxLayout()
        auto.setSpacing(4)
        self.auto_danmaku_check = QCheckBox("自动发弹幕（选中房间）")
        self.auto_danmaku_check.toggled.connect(
            lambda v: self._on_auto_toggle(v, "danmaku"))
        self.auto_danmaku_check.setToolTip(
            "自动发弹幕默认只在未开播时执行（避免在直播弹幕区刷屏）；"
            "需在开播时也执行，请另勾右侧开关。手动「发弹幕」按钮不受此限制。")
        auto.addWidget(self.auto_danmaku_check)
        # 文案必须表达「**额外允许**开播时执行」：默认关 = 仅未开播时自动发弹幕，
        # 勾选 = 开播（直播中）时也自动发（写成「仅开播时」会正好说反）。
        self.auto_danmaku_live_check = QCheckBox("允许开播时自动发弹幕")
        self.auto_danmaku_live_check.toggled.connect(
            lambda v: self._on_auto_toggle(v, "danmaku_when_live"))
        self.auto_danmaku_live_check.setToolTip(
            "勾选后：直播间开播（直播中）时也自动发弹幕；不勾选则只在未开播时自动发。"
            "需先勾选左侧「自动发弹幕」才会生效；手动「发弹幕」按钮不受此限制。")
        auto.addWidget(self.auto_danmaku_live_check)
        self.auto_like_check = QCheckBox("自动点赞（选中房间）")
        self.auto_like_check.toggled.connect(
            lambda v: self._on_auto_toggle(v, "like"))
        auto.addWidget(self.auto_like_check)
        auto.addStretch(1)
        outer.addLayout(auto)

    # ---------- 工具 ----------

    def api(self):
        return self.host.hub.api

    def ui_queue(self):
        return self.host.ui_queue

    def runner(self) -> Optional[MedalTaskRunner]:
        host = self.host
        if host._runner is None and host.hub.api is not None:
            # 事件回调走宿主的 _medal_event（线程安全入队，与 Tk 版 _medal_emit 等价；
            # 宿主没有 _medal_emit 这个名字，写错会在此处抛 AttributeError，
            # 导致 _on_task_selected 里后面的勾选框同步整段被跳过）
            host._runner = MedalTaskRunner(
                host.hub.api, host.app_config, emit=host._medal_event)
        return host._runner

    def _selected_room(self) -> Optional[int]:
        rows = self.medal_task_tree.selectedItems()
        if not rows:
            return None
        row = rows[0].row()
        item = self.medal_task_tree.item(row, 0)
        try:
            room_id = int(item.text() or 0)
        except Exception:
            return None
        return room_id if room_id in self.host.entries else None

    def set_hint(self, text: str) -> None:
        """设置页签提示（供宿主与内部统一走一处，便于后续换控件）。"""
        self.medal_hint.setText(text)

    def master_text(self) -> str:
        """写操作/全自动总开关的当前状态（与 Tk 版 _medal_master_text 一致）。"""
        app_config = self.host.app_config
        return (f"写操作：{'启用' if app_config.allow_write_operations else '关闭'}"
                f" · 全自动总开关：{'开' if app_config.auto_medal_tasks else '关'}")

    def update_status(self, medals_count: Optional[int] = None) -> None:
        count = len(self._medals) if medals_count is None else medals_count
        self.medal_status.setText(f"共持有 {count} 个粉丝牌 · {self.master_text()}")

    # ---------- 刷新 ----------

    def _on_refresh(self) -> None:
        host = self.host
        if host.hub.api is None:
            self.medal_status.setText("后台尚未就绪")
            return
        if not host.hub.api.logged_in:
            self.medal_status.setText("未登录（请先获取 Cookie）")
            return
        self.medal_status.setText("正在获取粉丝牌…")
        self.sync_task_rows()
        host.hub.submit(self._async_refresh_medals())
        host.hub.submit(self._async_refresh_tasks())

    async def _async_refresh_medals(self) -> None:
        api = self.api()
        if api is None:
            self.ui_queue().put(("medal_list", {"ok": False, "medals": [],
                                               "error": "后台未就绪"}))
            return
        try:
            medals = await api.get_medals()
            self.ui_queue().put(("medal_list", {"ok": True, "medals": medals,
                                                "error": None}))
        except Exception as exc:
            self.ui_queue().put(("medal_list", {"ok": False, "medals": [],
                                               "error": str(exc)}))

    async def _async_refresh_tasks(self, room_ids: Optional[List[int]] = None,
                                   announce: bool = True) -> None:
        """逐个房间刷新粉丝牌任务。

        ``get_medal_task_info`` 要的是**主播 uid**（不是房间号）；已拉到粉丝牌
        列表时，未持有该主播粉丝牌的房间直接标记「无粉丝牌」，不再请求接口。
        ``room_ids`` 为空表示刷新全部监听房间；``announce=False`` 供定时静默刷新
        与「任务结束后补刷」使用（不覆盖用户可见提示）。
        """
        host = self.host
        api = self.api()
        rooms = (list(host.entries.keys()) if room_ids is None
                 else [r for r in room_ids if r in host.entries])
        if not rooms:
            if announce:
                self.ui_queue().put(("medal_tasks_done", {
                    "total": 0, "ok": 0, "no_medal": 0, "error": 0}))
            return
        if api is None:
            if announce:
                self.ui_queue().put(("medal_tasks_done", {
                    "total": len(rooms), "api": False}))
            return
        known_uids = {int(m.get("target_id") or 0) for m in (self._medals or [])}
        known_rooms = {int(m.get("room_id") or 0) for m in (self._medals or [])}
        done = {"total": len(rooms), "ok": 0, "no_medal": 0, "error": 0}
        for index, room_id in enumerate(rooms):
            entry = host.entries.get(room_id)
            uid = int(entry.uid) if entry else 0
            real_room = host.real_room_id(room_id)
            if not uid:
                self.ui_queue().put(("medal_task_info", {
                    "room_id": room_id, "ok": False, "no_medal": False,
                    "error": "未知主播 uid", "tasks": []}))
                done["error"] += 1
            elif known_uids and uid not in known_uids and real_room not in known_rooms:
                self.ui_queue().put(("medal_task_info", {
                    "room_id": room_id, "ok": True, "no_medal": True, "tasks": []}))
                done["no_medal"] += 1
            else:
                try:
                    info = await api.get_medal_task_info(uid)
                except Exception as exc:
                    self.ui_queue().put(("medal_task_info", {
                        "room_id": room_id, "ok": False, "no_medal": False,
                        "error": str(exc), "tasks": []}))
                    done["error"] += 1
                else:
                    self.ui_queue().put(("medal_task_info", {
                        "room_id": room_id, "ok": True,
                        "no_medal": bool(info.get("no_medal", False)),
                        "error": None, "tasks": info.get("tasks") or [],
                        "free_intimacy": info.get("free_intimacy"),
                        "reach_free_intimacy_limit": info.get(
                            "reach_free_intimacy_limit")}))
                    done["ok"] += 1
            # 逐个房间节流，避免瞬时并发请求触发风控（与 Tk 版一致）
            if index + 1 < len(rooms):
                await asyncio.sleep(MEDAL_TASK_REFRESH_GAP_S)
        if announce:
            self.ui_queue().put(("medal_tasks_done", done))

    # ---------- 队列回调 ----------

    def on_medal_list(self, payload: dict) -> None:
        if not payload.get("ok"):
            self.medal_status.setText(f"获取粉丝牌失败：{payload.get('error')}")
            return
        medals = payload.get("medals") or []
        self._medals = medals
        self._medal_levels_room = {}
        self._medal_levels_uid = {}
        self._medal_names_room = {}
        self._medal_names_uid = {}
        self._medal_uid_by_iid = {}
        self._medal_room_by_iid = {}
        self.medal_tree.setRowCount(0)
        for i, medal in enumerate(medals):
            room_id = int(medal.get("room_id") or 0)
            uid = int(medal.get("target_id") or 0)
            level = int(medal.get("level") or 0)
            name = str(medal.get("medal_name") or "")
            if room_id and room_id not in self._medal_levels_room:
                self._medal_levels_room[room_id] = level
                self._medal_names_room[room_id] = name
            if uid and uid not in self._medal_levels_uid:
                self._medal_levels_uid[uid] = level
                self._medal_names_uid[uid] = name
            state = "点亮" if int(medal.get("is_lighted") or 0) == 1 else "未点亮"
            if int(medal.get("living_status") or 0) == 1:
                state += "·直播中"
            anchor = medal.get("anchor_name") or self.host.anchor_names.get(room_id) \
                or "未知主播"
            # 今日亲密度 = 今日已获取；当前/升级需 = 当前亲密度 / 下一级门槛
            today_feed = int(medal.get("today_feed") or 0)
            intimacy = int(medal.get("intimacy") or 0)
            next_intimacy = int(medal.get("next_intimacy") or 0)
            today = f"+{today_feed}"
            exp = f"{intimacy}/{next_intimacy}" if next_intimacy else str(intimacy)
            self.medal_tree.insertRow(i)
            for col, text in enumerate((name, str(level or "—"), anchor,
                                        str(room_id or "—"), today, exp, state)):
                self.medal_tree.setItem(i, col, QTableWidgetItem(text))
            self._medal_uid_by_iid[i] = uid
            self._medal_room_by_iid[i] = room_id
        self.update_status(len(medals))
        # 粉丝牌等级/名称缓存更新后，刷新各房间任务行的粉丝牌等级列
        self.sync_task_rows()
        if self.host._selected_room_id is not None:
            # 刷新绑定该房间的 SC 视图头部（主视图 + 该房间的独立窗口）
            self.host._refresh_sc_header(self.host._selected_room_id)

    def on_medal_task_info(self, payload: dict) -> None:
        room_id = payload.get("room_id")
        if room_id is None:
            return
        self._medal_tasks[room_id] = payload
        self._update_task_row(room_id)

    def on_medal_tasks_done(self, payload: dict) -> None:
        if payload.get("api") is False:
            self.medal_hint.setText("后台未就绪，任务刷新取消")
            return
        parts = [f"任务刷新完成（{payload.get('total', 0)} 个房间）"]
        if payload.get("ok"):
            parts.append(f"有任务 {payload['ok']}")
        if payload.get("no_medal"):
            parts.append(f"无粉丝牌 {payload['no_medal']}")
        if payload.get("error"):
            parts.append(f"失败 {payload['error']}")
        self.medal_hint.setText("；".join(parts))
        self.update_status()

    def apply_task_progress(self, payload: dict) -> None:
        """执行期间的实时进度：就地更新该房间任务行（无需等整轮结束，同 Tk 版）。"""
        room_id = payload.get("room_id")
        jump_type = payload.get("jump_type")
        if room_id is None or not jump_type:
            return
        info = self._medal_tasks.get(int(room_id))
        if not info:
            return
        task = find_task(info.get("tasks") or [], str(jump_type))
        if task is None:
            return
        task["current"] = int(payload.get("current") or 0)
        task["limit"] = int(payload.get("limit") or 0)
        task["is_done"] = bool(payload.get("is_done"))
        self._update_task_row(int(room_id))

    # ---------- 任务表：行与顺序 ----------

    def _row_of_room(self, room_id: int) -> Optional[int]:
        tree = self.medal_task_tree
        for row in range(tree.rowCount()):
            item = tree.item(row, 0)
            if item is None:
                continue
            try:
                if int(item.text() or 0) == room_id:
                    return row
            except (TypeError, ValueError):
                continue
        return None

    def sync_task_rows(self) -> None:
        """按 host.entries 增删任务行，并让行顺序与直播间列表保持一致。"""
        tree = self.medal_task_tree
        for row in range(tree.rowCount() - 1, -1, -1):
            item = tree.item(row, 0)
            try:
                rid = int(item.text() or 0) if item is not None else 0
            except (TypeError, ValueError):
                rid = 0
            if rid not in self.host.entries:
                tree.removeRow(row)
        for room_id in list(self.host.entries.keys()):
            if self._row_of_room(room_id) is None:
                # 先写房间号：_row_of_room 以第 0 列的房间号定位行
                row = tree.rowCount()
                tree.insertRow(row)
                tree.setItem(row, 0, QTableWidgetItem(str(room_id)))
            self._update_task_row(room_id)
        self._apply_task_order()

    def _apply_task_order(self) -> None:
        """让任务表行顺序与直播间列表**完全一致**（排序/拖动即刻反映）。"""
        tree = self.medal_task_tree
        order = [r for r in self.host._room_order if r in self.host.entries]
        order += [r for r in self.host.entries if r not in order]
        if len(order) != tree.rowCount():
            return
        selected = self._selected_room()
        rows = {rid: self._row_of_room(rid) for rid in order}
        if any(row is None for row in rows.values()):
            return
        texts = {
            rid: [(tree.item(rows[rid], col).text()
                   if tree.item(rows[rid], col) is not None else "")
                  for col in range(tree.columnCount())]
            for rid in order
        }
        tree.blockSignals(True)
        for index, rid in enumerate(order):
            for col, text in enumerate(texts[rid]):
                item = tree.item(index, col)
                if item is None:
                    tree.setItem(index, col, QTableWidgetItem(text))
                elif item.text() != text:
                    item.setText(text)
        tree.blockSignals(False)
        if selected is not None:
            row = self._row_of_room(selected)
            if row is not None:
                tree.selectRow(row)

    def _update_task_row(self, room_id: int) -> None:
        """按该房间的任务信息重绘行（含「无粉丝牌 / 获取失败」文案与自动列）。"""
        row = self._row_of_room(room_id)
        if row is None:
            if room_id not in self.host.entries:
                return
            # 兜底建行（正常路径由 sync_task_rows 建行）
            row = self.medal_task_tree.rowCount()
            self.medal_task_tree.insertRow(row)
            self.medal_task_tree.setItem(row, 0, QTableWidgetItem(str(room_id)))
        entry = self.host.entries.get(room_id)
        payload = self._medal_tasks.get(room_id) or {}
        uid = int(entry.uid) if entry else 0
        level = self._medal_levels_room.get(room_id)
        if level is None and uid:
            level = self._medal_levels_uid.get(uid)
        if payload.get("no_medal"):
            cells = ("无粉丝牌", "无粉丝牌", "无粉丝牌")
        elif payload.get("error"):
            cells = ("获取失败", "获取失败", "获取失败")
        else:
            tasks = payload.get("tasks") or []
            cells = (format_medal_task(tasks, TASK_WATCH_LIVE),
                     format_medal_task(tasks, TASK_SEND_DANMAKU),
                     format_medal_task(tasks, TASK_LIKE))
        auto = "关"
        if entry is not None:
            if entry.auto_like and entry.auto_danmaku:
                auto = "赞+弹"
            elif entry.auto_like:
                auto = "赞"
            elif entry.auto_danmaku:
                auto = "弹"
        values = [str(room_id),
                  self.host.anchor_names.get(room_id, ""),
                  str(level or "—"),
                  cells[0], cells[1], cells[2], auto]
        for col, text in enumerate(values):
            item = self.medal_task_tree.item(row, col)
            if item is None:
                item = QTableWidgetItem(text)
                self.medal_task_tree.setItem(row, col, item)
            elif item.text() != text:
                item.setText(text)
        self._pending = None

    # ---------- 手动执行 ----------

    def _complete_selected(self, task_type: str) -> None:
        """按指定任务类型对**选中房间**执行（点赞 / 发弹幕各自独立按钮）。"""
        room_id = self._selected_room()
        if room_id is None:
            self.medal_hint.setText("请先在上方列表选择一个直播间")
            return
        if (self._medal_tasks.get(room_id) or {}).get("no_medal"):
            self.medal_hint.setText("该主播未持有粉丝牌，无法执行任务")
            return
        entry = self.host.entries.get(room_id)
        live = self.host.live_state.get(room_id)
        live_status = 1 if live == "直播中" else 0
        log_task.info("手动触发粉丝牌任务：房间 %s，类型 %s", room_id, task_label(task_type))
        run = self.runner()
        if run is None:
            self.medal_hint.setText("后台初始化中，请稍候…")
            return
        if run.is_running(room_id):
            self.medal_hint.setText("该房间任务正在执行中")
            return
        self.medal_hint.setText(
            f"正在执行房间 {room_id} 的{task_label(task_type)}任务…")
        self.host.hub.submit(self._start_medal_room(
            room_id, entry.uid if entry else 0, live_status,
            only=task_type, manual=True))

    async def _start_medal_room(self, room_id: int, anchor_uid: int,
                                live_status: int, *,
                                only: Optional[Union[str, List[str]]] = None,
                                manual: bool = False) -> None:
        """触发一次任务执行。``only`` 为单个类型或类型列表（自动任务两项一起传，
        由同一次执行依次完成，避免后一项被「该房间正在执行中」挡住）。
        """
        log_task.debug("提交粉丝牌任务：房间 %s，类型 %s，来源 %s", room_id,
                       only or "全部", "手动" if manual else "自动")
        run = self.runner()
        if run is None:
            self.ui_queue().put(("medal_result", {
                "room_id": room_id, "status": "blocked", "message": "后台未就绪",
                "manual": manual}))
            return
        if run.is_running(room_id):
            return
        t0 = time.time()
        try:
            # 短号场景要用真实房间号调接口（与 Tk 版 _room_id_map 一致）
            result = await run.complete_room(
                self.host.real_room_id(room_id), anchor_uid, live_status,
                room_label=self.host.anchor_names.get(room_id, ""), only=only,
                auto=not manual)
        except Exception as exc:
            result = {"status": "risk", "message": str(exc)}
        result = dict(result, elapsed=time.time() - t0, manual=manual,
                      room_id=room_id)
        self.ui_queue().put(("medal_result", result))

    # ---------- 自动开关 ----------

    def _on_auto_toggle(self, checked: bool, kind: str) -> None:
        room_id = self._selected_room()
        if room_id is None or room_id not in self.host.entries:
            return
        entry = self.host.entries[room_id]
        if kind == "danmaku":
            entry.auto_danmaku = checked
            name = "自动发弹幕"
        elif kind == "danmaku_when_live":
            entry.auto_danmaku_when_live = checked
            name = "允许开播时自动发弹幕"
        else:
            entry.auto_like = checked
            name = "自动点赞"
        self.host._save_config()
        self._update_task_row(room_id)
        log_task.info("房间 %s 的「%s」自动开关：%s", room_id, name,
                      "开" if checked else "关")
        app_config = self.host.app_config
        if kind == "danmaku_when_live":
            self.medal_hint.setText(
                "已为该房间" + ("允许" if checked else "禁止") + "在开播时自动发弹幕"
                + ("" if checked else "（仅在未开播时执行）"))
        elif checked and not app_config.auto_medal_tasks:
            self.medal_hint.setText(
                f"已为该房间开启{name}，但 config.json 的 medal_tasks.auto 为 false，"
                "暂不会自动执行")
        elif checked and not app_config.allow_write_operations:
            self.medal_hint.setText(
                f"已为该房间开启{name}，但 allow_write_operations 为 false，"
                "暂不会自动执行")
        else:
            self.medal_hint.setText(f"已为该房间{'开启' if checked else '关闭'}{name}")
        if checked and kind == "like" and self.host.hub.ready.is_set():
            self.host._try_start_auto_room(room_id)

    def refresh_auto_checks(self, room_id: Optional[int] = None) -> None:
        room_id = room_id or self._selected_room()
        if room_id is None or room_id not in self.host.entries:
            return
        entry = self.host.entries[room_id]
        for check, val in ((self.auto_danmaku_check, entry.auto_danmaku),
                           (self.auto_like_check, entry.auto_like),
                           (self.auto_danmaku_live_check, entry.auto_danmaku_when_live)):
            check.blockSignals(True)
            check.setChecked(bool(val))
            check.blockSignals(False)

    def _on_task_selected(self) -> None:
        """选中行变化：同步自动开关取值，并按「是否有选中房间/是否在执行」置灰控件。

        先做**纯界面同步**（开关取值 + 可用状态），再去查「是否正在执行」——后者要
        触碰执行器，一旦出错也不能让勾选框停在上一个房间的取值上（曾因宿主属性名写错
        抛 AttributeError，导致这里的界面同步整段被跳过、切房间时勾选框完全不跟随）。
        """
        room_id = self._selected_room()
        has = room_id is not None
        for w in (self.auto_danmaku_check, self.auto_danmaku_live_check,
                  self.auto_like_check):
            w.setEnabled(has)
        if has:
            self.refresh_auto_checks(room_id)
        running = False
        if has:
            try:
                runner = self.runner()
                running = bool(runner and runner.is_running(room_id))
            except Exception:  # 执行器构造/查询异常不应连带冻结界面（调试页可见）
                log_task.warning("查询房间 %s 的粉丝牌任务执行状态失败", room_id,
                               exc_info=True)
        for w in (self.medal_like_btn, self.medal_danmaku_btn):
            w.setEnabled(has and not running)

    # ---------- 点击跳转 ----------

    def _on_medal_cell_clicked(self, row: int, col: int) -> None:
        import webbrowser

        kind = MEDAL_LINK_COLUMNS.get(col)
        if kind == "room":
            room_id = self._medal_room_by_iid.get(row)
            if room_id:
                webbrowser.open(f"https://live.bilibili.com/{room_id}")
        elif kind == "space":
            uid = self._medal_uid_by_iid.get(row)
            if uid:
                webbrowser.open(f"https://space.bilibili.com/{uid}")

    def _on_task_cell_clicked(self, row: int, col: int) -> None:
        import webbrowser

        kind = MEDAL_TASK_LINK_COLUMNS.get(col)
        item = self.medal_task_tree.item(row, 0)
        if item is None:
            return
        try:
            room_id = int(item.text() or 0)
        except Exception:
            return
        if kind == "room" and room_id:
            webbrowser.open(f"https://live.bilibili.com/{room_id}")
        elif kind == "space":
            entry = self.host.entries.get(room_id)
            uid = entry.uid if entry else 0
            if uid:
                webbrowser.open(f"https://space.bilibili.com/{uid}")