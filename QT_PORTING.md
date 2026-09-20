# Tk → Qt 迁移功能清单（追蹤表）

> 目标：把现有 Tk（`blive_sc_get/gui_app.py`，约 4426 行）的渲染层逐项移植到 Qt（PySide6）。
> 业务层（`AsyncHub`、`RoomClient`、`SCStorage`、`medal_runner`、`cookie_server`、`gui_config`、`app_config`、`medal_tasks`、`api`、`client`）与 Tk **完全解耦，Qt 版原样复用**。
> 本表按功能逐项核对，标 ✅ = 已在 Qt 版落地，🔶 = 部分落地（附缺口），⬜ = 尚未落地。
>
> 迁移策略：核心优先分阶段。分批见「批次」列。
>
> **2026-09 复查**：此前本表标记「48/48 ✅」与实际实现不符。经逐项比对 Tk 源码后修正状态，
> 并已修复其中「不可用 / 写错参数」的 7 项（详见文末「复查修正记录」）。

## 通用 / 框架

| # | 功能 | Tk 实现（gui_app.py） | Qt 实现 | 状态 | 批次 |
|---|------|----------------------|---------|------|------|
| 0.1 | 主窗口 + DPI 感知 | `run_gui` ctypes DPI；宽高缩放 | Qt `qt_gui.py` + HiDPI | ✅ | 骨架 |
| 0.2 | 后台 asyncio 线程 + 事件队列 | `AsyncHub` | 复用同一套实现（`qt_app.py` 内同款 `AsyncHub`） | ✅ | 骨架 |
| 0.3 | 队列轮询（100ms） | `_poll_queue` | Qt `QTimer` 轮询 | ✅ | 骨架 |
| 0.4 | 页签框架 | `ttk.Notebook`（直播间/粉丝牌/调试） | `QTabWidget`（直播间/粉丝牌/调试） | ✅ | 骨架 |
| 0.5 | 应用关闭（确认 + 停止任务） | `_on_close`/`_async_shutdown`/`_destroy`（`after(1500, _destroy)` 等落盘） | Qt `closeEvent` + 等待后台线程收尾（join 1.5s） | ✅ | 骨架 |

## 批次 1（本轮核心）

### 直播间页

| # | 功能 | Tk 实现 | Qt 实现 | 状态 |
|---|------|---------|---------|------|
| 1.1 | 房间添加输入框（房间号/链接/主页） | `add_var` + `_on_add_room`、`parse_add_input` | `QLineEdit` + 按钮 | ✅ |
| 1.2 | 房间列表表格（room/anchor/status/notify/title/note） | `ttk.Treeview` | `QTableWidget`（同列序） | ✅ |
| 1.3 | 行状态配色（live/offline/stopped/disabled） | `tag_configure` | Qt 前景色（含 occupied→stopped） | ✅ |
| 1.4 | 排序模式 + 「排序」按钮 | `sort_mode_var` + `_on_sort_clicked` | 下拉 + 按钮；置顶在勾选时即时生效；状态档位含「轮播中」 | ✅ |
| 1.20 | 列宽保护（拖动列宽不把列挤出窗口） | `_clamp_columns` 手工收紧总列宽 | 直播标题列 `Stretch` + `minimumSectionSize=40`（拖动时弹性列让位） | ✅ |
| 1.21 | 列表行数随窗格自适应 | `_fit_tree_height` | `QTableWidget` 原生填满可用高度（超出给纵向滚动条） | ✅ |
| 1.5 | 直播中置顶开关 | `_on_pin_live_toggled` | `QCheckBox` | ✅ |
| 1.6 | 悬浮窗通知总开关 | `_on_overlay_toggled` | `QCheckBox` + `qt_overlay.QtToastOverlayManager` | ✅ |
| 1.7 | 弹窗常驻开关（随悬浮窗开禁用） | `_on_persist_toggled` | `QCheckBox` | ✅ |
| 1.8 | 提示音效选择 + 试听 | `_on_sound_selected`（选择即保存+试听）/`_play_notify_sound` | 下拉 `currentTextChanged` + 按钮（试听播下拉当前项） | ✅ |
| 1.9 | 备注输入行 | `note_var` + `_flush_note` | `QLineEdit`（回车/失焦写回） | ✅ |
| 1.10 | 停用监听按钮 | `_on_toggle` | 按钮（多选批量） | ✅ |
| 1.11 | 删除按钮 | `_on_delete` | 按钮 + 确认框 | ✅ |
| 1.12 | 行级点击跳转/复制（房间/主播/提醒列，带点击保护 + 拖动保护） | `_on_tree_press`（`return "break"` 不改选中行）/`_on_tree_release`/`_open_tree_link` | `ProtectedLinkTable`：`selectionCommand` 对跳转列的**按下与松开**都返回 `NoUpdate`，松手且未拖动才 `linkClicked` | ✅ |
| 1.13 | 行拖动排序（Ctrl 多选） | `_on_tree_drag_motion` | Qt `InternalMove` 拖放（`rowsMoved` 回写顺序） | ✅ |
| 1.14 | 选中房间驱动 SC 头/弹幕区 | `_on_room_selected` | Qt selection（切房清空弹幕区 + 重设门控） | ✅ |
| 1.15 | 房间状态/主播名/直播标题实时更新 | `_refresh_row`/`_on_client_event status`（title 存 Treeview） | Qt `titles` 独立缓存；状态列取 `client_states` | ✅ |
| 1.15b | 无连接房间也显示主播/标题（被占用、已停用） | `_async_fetch_uid`/`_on_room_info` + 60s 周期刷新 | 同款只读查询（`_async_fetch_room_info`/`_on_room_info` + `_room_info_timer`） | ✅ |
| 1.16 | 开播提醒（提示音 + 任务栏闪烁 + 悬浮窗） | `_notify_live`/`_play_notify_sound`/`_flash_taskbar`/`ToastOverlayManager` | 提示音✅+闪烁✅+悬浮窗✅（`qt_overlay`） | ✅ |
| 1.17 | 点「提醒」列切换该房间开播提醒 | `_open_tree_link` 的 `#4` 分支 | `cellClicked(col==3)` 取反并落盘 | ✅ |
| 1.18 | 被占用检测（其它实例/CLI） | `_async_start_room` 里 `is_room_being_recorded` + 记录持有者 | 同款检查并置 `occupied`，状态格 tooltip 显示占用者 | ✅ |
| 1.19 | 短房间号 → 真实房间号映射 | `_room_id_map`（发弹幕/任务用真实号） | `real_room_id()` 同款映射 | ✅ |

### SC 区

| # | 功能 | Tk 实现 | Qt 实现 | 状态 |
|---|------|---------|---------|------|
| 2.1 | SC 富文本视图（多段颜色 tag） | `sc_text` tk.Text + tag | `QTextEdit` + `QTextCharFormat` | ✅ |
| 2.2 | 实时追加 SC（含金额梯度配色） | `_append_sc`/`build_sc_segments` | Qt（复用 `build_sc_segments`） | ✅ |
| 2.3 | SC 删除/退款标记 | `_mark_sc_deleted`/`_append_sc(deleted)` | Qt（fragment 属性定位 + 置灰） | ✅ |
| 2.4 | 无限滚动加载历史（滚到顶加载更早） | `_maybe_load_more`/`_load_history` | Qt 滚动条 + 「正在加载历史 SC…」提示 | ✅ |
| 2.5 | 刷新历史按钮 | `_on_refresh_history` | 按钮 | ✅ |
| 2.6 | SC 总数常显 | `_update_sc_total_label` | Qt 标签（切房/加载时同步，含 0 清零） | ✅ |
| 2.7 | 自吸底（新消息到底部） | `_bind_autoscroll`/`_autoscroll_tick` | Qt `verticalScrollBar`（含未读累加） | ✅ |
| 2.10 | 尺寸变化后保持吸底 | `_on_text_configure`/`_restore_bottom`（去抖 80ms） | `BottomHoldTextEdit.resizeEvent` + 轮询记录吸底状态 | ✅ |
| 2.11 | 点击 SC 用户名 → 个人空间 | `_on_sc_click`（按 `uid:` 标签） | `sc_text.click_handler` + `_UID_KEY` 字符属性 | ✅ |
| 2.12 | 中键浏览器式快速滚动 | `_bind_autoscroll`/`_autoscroll_toggle`/`_autoscroll_tick`（SC 区 + 调试日志） | `BottomHoldTextEdit` 内实现（SC / 调试 / 弹幕三处均可用） | ✅ |
| 2.13 | 三板块默认占比 2:3.5:4.5 | `PANE_RATIO` + `_apply_default_pane_ratio` | `PANE_RATIO` + `_apply_pane_ratio()`（首次显示与弹幕区显隐时重算） | ✅ |
| 2.14 | 拖动窗口时暂停 SC 换行（卡顿缓解） | `_on_root_configure`/`_restore_sc_wrap` | 未移植：Qt 的文本重排由原生实现，实测无需该优化 | n/a |
| 2.8 | 未读计数徽标（SC/弹幕，滚回底清除） | `_unseen_badge_text`/`_sync_unseen_from_scroll` | `sc_badge`/`dm_badge` + `_sync_unseen_badges` | ✅ |
| 2.9 | SC 头显粉丝牌名/等级 | `_update_sc_header` | 含房间/主播/标题/同接/舰长/粉丝牌（读 medal_tab 缓存） | ✅ |

### Cookie / 调试

| # | 功能 | Tk 实现 | Qt 实现 | 状态 |
|---|------|---------|---------|------|
| 3.1 | 获取Cookie（读浏览器数据库） | `_on_fetch_cookie`/`_fetch_cookie_worker` | Qt 按钮 + 线程 | ✅ |
| 3.2 | 从插件获取（cookie_server） | `_on_fetch_cookie_plugin`/`_on_cookie_from_plugin_result` | Qt 复用 | ✅ |
| 3.3 | 调试日志页（实时 + 滚动 + 清空 + 日志级别） | `_setup_logging`/`_append_logs`/`_clear_debug` | `_QueueLogHandler` + 日志视图（超 4000 行裁掉一半） | ✅ |
| 3.4 | 日志级别切换（DEBUG/INFO） | `_on_debug_toggle`（改根 logger） | 同款（改根 logger） | ✅ |

## 批次 2

### 弹幕区

| # | 功能 | Tk 实现 | Qt 实现 | 状态 |
|---|------|---------|---------|------|
| 4.1 | 弹幕开关（显示/保存当前房间弹幕） | `dm_var`/`_on_dm_toggled`/`_apply_dm_gate` | 宿主常驻开关 `qt_app.dm_toggle` + `_apply_dm_gate()`（`set_danmaku_enabled`）+ 面板随动 | ✅ |
| 4.2 | 弹幕富文本显示 + 行裁剪 | `_append_dm_batch`/`_trim_dm_text`（4000 行上限） | `DmPanel.append_dm_batch`/`_trim_dm_text`（行号需转 int） | ✅ |
| 4.3 | 弹幕 Emoji 悬浮预览 + 悬浮提示 | `_EmoticonTooltip`/`_show_emoticon_tooltip`（提示窗显示原图、懒加载、缺图兜底、越界回缩） | 弹幕流内**直接显示表情图**（图未到位先显示触发词，下载完成后**原地换图**）+ 悬浮提示只显示配置字段文字（不再重复弹原图；懒加载 / 缺图兜底补 url·id / 虚拟桌面越界回缩）；另有「弹幕表情图」开关：关闭即把已显示的图片还原为文字（悬浮恢复为看原图），重新开启再把已有触发词换回图片 | ✅ |
| 4.4 | 点击弹幕复制内容 + @回复 | `_on_dm_click`/`_copy_dm_content`/`_set_dm_reply_target` | 左键跳转/复制 + 右键「回复该弹幕」/「@该用户」 | ✅ |
| 4.5 | 发送弹幕（配色/样式/分屏/门控/防连点） | `_on_send_danmaku`/`_dm_send_block_reason`/`_load_dm_options_for_selected` | Qt 同款门控 + 服务端颜色/模式按房间刷新 + `replay_dmid` 回复 | ✅ |
| 4.6 | 表情面板（入口/分页/换包/图片/悬停） | `_on_open_emoticons`/`_build_emoticon_panel` 系列（下载图片、缩放、缓存、记忆包序号、Esc 收起） | 发送行「表情」按钮 + 分页/换包/悬停/图片限流下载与缓存/按房间记忆包序号/Esc 收起 | ✅ |
| 4.7 | 弹幕输入不硬截断（仅超长标红） | 去掉客户端长度校验 | `setMaxLength` 已移除，仅标红提示 | ✅ |
| 4.8 | 点击弹幕正文复制到剪贴板 | `_copy_dm_content`（`clipboard_append`）+ 独立复制提示行 | `host.copy_to_clipboard()` + 独立 `dm_copy_hint` 行（不覆盖发送提示） | ✅ |
| 4.9 | 表情条滚轮横向翻动 | `_on_emoticon_wheel` | `eventFilter` 拦 Wheel → 横向滚动条；单行自然宽度 `_sync_strip_width` | ✅ |
| 4.10 | 直播预览（拉流 + 播放） | 无（Tk 没有可用的视频组件） | **Qt 专属增强**（ROADMAP 63 · P1~P4 全部完成）：独立浮窗（`QMediaPlayer` + `LiveStreamProxy` 回环代理注入防盗链头；HLS 优先 / 每 10 分钟无缝换源 / 出错换格式重试 / **断流自愈**（5 秒心跳 + 宽限期 + 重试上限）/ **自动追边**（落后超 `max_drift_sec` 默认 3 秒重载跳最新）；清晰度按新接口 `accept_qn` 只列该房间可用档位，未登录仅 360P·720P，**清晰度记忆**写 `ui.preview_quality`）；P3 **多路宫格**（最多 4 路 `preview.max_rooms`，仅主路出声，副路卡顿或 CPU 偏高自动停路，双击格子放大、双击房间行加入/停止）；P2 观看时长上报（`webHeartBeat`，写操作默认关闭、只对主路）；P4 加密房间密码（右键输入，**仅存内存**不落盘）；失败提示翻成人话 | ✅ |

### 粉丝牌页

| # | 功能 | Tk 实现 | Qt 实现 | 状态 |
|---|------|---------|---------|------|
| 5.1 | 我持有的粉丝牌表（分页拉取） | `_on_medal_list`（7 列，含「当前/升级需」） | `MedalTab.on_medal_list`（同 7 列，主播名取勋章自带字段） | ✅ |
| 5.2 | 监听房间任务表（读进度/自动列） | `_update_medal_task_row`/`format_medal_task`（7 列含「自动」） | Qt 同款 `format_medal_task` + `jump_type` 常量 + 自动列 + 「无粉丝牌/获取失败」文案 | ✅ |
| 5.3 | 手动发弹幕/点赞按钮 | `_on_medal_press`/`_complete_medal_selected` | Qt 手动执行（含运行中/无粉丝牌提示） | ✅ |
| 5.4 | 自动发弹幕/自动点赞开关 | `_on_medal_auto_danmaku_toggle`/`_on_medal_auto_like_toggle` | Qt 复选框（含总开关未开时的提示） | ✅ |
| 5.5 | 允许开播时自动发弹幕开关 | `_on_medal_auto_danmaku_live_toggle` | Qt 复选框（文案「允许开播时自动发弹幕」+ 悬浮说明；自动任务两项同批提交见 `auto_task_types`） | ✅ |
| 5.6 | 行点击跳浏览器（点击保护） | `_open_medal_link`/`_on_medal_release` | Qt 单元格点击（列映射已随 7 列修正） | ✅ |
| 5.7 | 页签自动刷新 + 周期自动任务 | `_medal_tab_refresh_tick`（90s，仅停留页签时）/`_medal_auto_tick`（5 分钟） | Qt `_medal_refresh_timer`(90s) + `_medal_tick_timer`(5 分钟) + `currentChanged` | ✅ |
| 5.8 | 任务表行顺序跟随直播间列表 | `_apply_medal_task_order` | Qt `sync_task_rows`/`_apply_task_order`（增删+排序/拖动即时跟随） | ✅ |
| 5.9 | 启动即拉取粉丝牌与任务 | `_on_hub_ready` → `_async_refresh_medals_and_tasks` | Qt `_on_hub_ready` → `medal_tab._on_refresh()` | ✅ |
| 5.10 | 执行期间实时进度 / 提示事件 | `_on_medal_event`（`medal_note`/`medal_start`/`medal_progress`/`medal_task_progress`）/`_apply_medal_task_progress` | 同名事件全支持 + `apply_task_progress` 就地更新行（真实号→输入号映射） | ✅ |
| 5.11 | 页签状态行（写操作/全自动总开关） | `_medal_master_text`/`_update_medal_status` | `MedalTab.master_text()`/`update_status()` | ✅ |
| 5.12 | 任务结束后自动补刷粉丝牌与任务 | `_on_medal_result` 尾部补刷 | 同款（仅刷该房间任务，`room_ids=` 参数） | ✅ |

## 复查清单（零散工具函数）

以下顶层辅助函数是纯函数/工具，Qt 一并复用或重写：
`parse_add_input`、`build_sc_segments`、`text_scrolled_to_bottom`、`danmaku_send_guard`、
`dm_trim_index`、`unseen_badge_text`、`danmaku_content_from_line`、`emoticon_*`、
`tooltip_position`、`select_dm_options`、`fit_emoticon_scale`、`emoticon_display_size`。
（多数与 UI 相关，随对应功能迁移；纯逻辑的部分由两版共享 `gui_app` 中的实现。）

**注意**：`dm_trim_index` 返回的是 Tk 风格行号字符串（`"2000.0"`），Qt 侧使用前必须转 `int`。

## 复查修正记录（2026-09）

按 Tk 源码逐项比对后修复的问题（均已补冒烟验证）：

1. **弹幕门控从未设置** —— `RoomClient._dm_enabled` 默认 `False`，Qt 全文无 `set_danmaku_enabled` 调用，
   弹幕既不显示也不落盘。现补 `_apply_dm_gate()`，并在 `_start_room`/开关/切房/启停时同步。
2. **弹幕行裁剪抛 TypeError** —— `dm_trim_index()` 返回字符串，被直接传给 `findBlockByNumber(int)`。
   现转 `int` 并做合法性校验（超 4000 行不再崩）。
3. **表情面板没有入口** —— 补发送行「表情」按钮（`dm_emoji_btn`），并实现面板内表情图片下载/缩放/缓存。
4. **调试日志页永远空白** —— 补 `_setup_logging()`（`_QueueLogHandler` + Formatter），日志级别改根 logger，
   日志裁剪改为「超 4000 行删最旧一半」。
5. **粉丝牌任务查询参数错误** —— `get_medal_task_info(target_id)` 传的是房间号；现改为传主播 uid，
   并补「未知 uid / 未持有粉丝牌」分支与逐房间请求节流。
6. **「发弹幕」任务列恒为 `—`** —— 硬编码 `"danmaku"`，实际 `jump_type` 为 `sendDanmu`；现改用 `TASK_*` 常量。
7. **自动任务绕过总开关且频率过高** —— 现要求 `medal_tasks.auto` + `allow_write_operations` 同时开启，
   周期由 5 秒改回 Tk 的 5 分钟。
8. **直播标题从未保存** —— 状态列与标题列显示同一串状态文本；现独立缓存 `titles`，
   `_status_text` 按 `client_states` 给出「已停用/连接中…/被占用/已停止」。
9. **音效选择不生效** —— 下拉未接信号、试听播的是已保存值；现选择即保存并试听。
10. **偏好键不一致** —— Qt 读写 `show_danmaku`，`gui_config` 只认 `dm_visible`；现统一为 `dm_visible`。
11. **弹幕颜色/模式不按房间刷新** —— 补 `_load_dm_options_for_selected`/`_async_load_dm_config` +
    `DmPanel.on_dm_config`（移除会对不存在方法报错的死分支）。
12. **切房间不清空弹幕区** —— 补 `DmPanel.clear_view()`，切换房间/清空选择时调用。
13. **发表情方式错误** —— 原先把触发词当普通弹幕发（服务端 `10203`）；现整条表情条目传给 `send_danmaku(emoticon=...)`。
14. **回复弹幕未带 `replay_dmid`** —— 现与 Tk 一致透传。
15. **粉丝牌页缺失项** —— 补「自动」列、「当前/升级需」列、任务表行跟随直播间顺序、删除房间同步移除任务行。
16. **退出不等落盘** —— `closeEvent` 现在等待后台线程收尾（≤1.5s）再退出，避免未刷盘的 SC 丢失。
17. **「被占用」无从排查** —— 房间被其它实例持锁时只显示「被占用」。现新增
    `browser_rooms.read_room_lock_holder()`，两版 GUI 都把持有者（`pid=… started=…`）写进
    日志，Qt 版还在状态格上加 tooltip 说明「关闭对应窗口/进程后重启即可恢复」。
18. **尺寸变化后不吸底** —— 拖窗口/拖分隔条后，原本贴底的 SC/弹幕区会停在旧位置、看不到最新内容。
    现新增 `BottomHoldTextEdit`（resize 时若「变化前位于底部」则重新贴底），吸底状态由 100ms 轮询维护，
    与 Tk 版 `_on_text_configure`/`_restore_bottom` 行为一致；用户向上翻阅时不受影响。
19. **弹幕表情只有文字提示、没有原图**（本表仅存的 🔶）—— 现补齐完整链路：
    ① 建 `emoticon_unique -> {text, unique, id, room_id, url}` 记录（**带上限 400**，此前无上限会一直涨）；
    ② 报文不带图片地址时，用**宿主缓存的房间表情包**兜底补 `url`/数字 `id`，包未加载则后台拉一次（30s 冷却）；
    ③ 悬浮命中表情时先懒加载原图，**图就绪后自动回填到正在显示的提示窗**（无图期间显示配置文案）；
    ④ 提示窗改为「有图只显示图、无图显示文案」，并改用虚拟桌面并集做越界回缩，多显示器下不跑出屏幕。
    顺带修掉一个遗留 bug：默认配置下触发词会在提示里**重复出现两遍**（`text` 字段被额外拼接了一次）；
    表情面板按钮的原生 tooltip 也从写死的 `("trigger","unique","id")`（`trigger` 不是合法字段名）改为按
    `config.json` 的 `emoticon_tooltip` 渲染。
20. **点击 SC 用户名不跳转**（`_UID_KEY` 只写不读）—— 补 `sc_text.click_handler` → `_on_sc_clicked`。
21. **点击弹幕正文没有真的复制**（只显示「已复制」）—— `copy_to_clipboard` 定义了却无人调用；
    现真实写入剪贴板，并把「已复制」拆到独立提示行，不再覆盖发送状态文案。
22. **中键快速滚动缺失** —— 在 `BottomHoldTextEdit` 内实现（SC / 调试 / 弹幕三处）。
23. **备注可能写到错误的房间** —— `_flush_note` 原先用「当前选中房间」，切换行的瞬间会把
    A 房间的备注写进 B 房间；现按 Tk 的 `_note_room_id`（备注所属房间）写回，并在切换前先 flush。
24. **粉丝牌缺少过程反馈** —— 补 `medal_note`/`medal_start`/`medal_progress`/`medal_task_progress`
    四类事件处理与**执行期实时进度**（原先只认不存在的 `manual_start`，事件全丢）；补页签状态行
    「写操作：启用/关闭 · 全自动总开关：开/关」；任务结束后补刷粉丝牌与该房间任务；自动点赞前
    补「未持有粉丝牌」跳过判断。
25. **表情条被压扁而不是横向滚动** —— `QScrollArea(widgetResizable=True)` 会把单行内容缩到视口宽度；
    现按各按钮自然宽度设置 `minimumWidth`（对应 Tk 版 `_sync_emoticon_strip_width`），窄时铺满、
    宽时横向滚动，并支持滚轮横向翻动。
26. **三板块占比硬编码** —— 改为按 Tk 的 `PANE_RATIO` 计算，并在首次显示与弹幕区显隐时重新分配。
27. **调试日志逐行插入** —— 改为批量插入（同 Tk 版 `_append_logs`），减少重排。
28. **被占用 / 已停用监听的房间不显示主播与直播标题** —— 这两类房间不会建立弹幕连接，
    自然收不到 `status` 事件，列表里「主播」「直播标题」永远是空的（占用时尤其明显：
    同一房间被另一个实例监听，用户只看到「被占用」）。现补一条**只读 HTTP 通道**：
    占用判定命中时、启动时就停用的房间、以及每 60 秒为一轮（`ROOM_INFO_REFRESH_MS`），
    用 `get_full_room_info` + `get_anchor_name` 填主播名/标题/直播状态；
    **有弹幕连接时直播状态仍以弹幕推送为准**（不覆盖，避免关播瞬间接口缓存旧状态）。
    两版 GUI 同步实现（Tk：`_async_fetch_uid` 扩展为完整房间信息 + `_room_info_tick`）。
29. **行间距偏大** —— ① SC 区每条记录后多一整行空行：`build_sc_segments` 末尾已带 `\n`
    （Qt 的 `insertText("\n")` 会另起文本块），代码又调了一次 `insertBlock()`，等于双倍行距；
    实时追加与向上翻页两处均已去掉多余换行。② 表格行高改为 `字体行高 + TABLE_ROW_PADDING(8)`
    （Qt 默认内边距约 18px），房间列表 / 粉丝牌 / 任务表三张表统一，随 DPI 自动缩放。
    ③ 弹幕区字号 10pt → 9pt（对齐 Tk 版，SC 区仍 10pt）。
30. **缺少「点击保护」**（Tk 的 `_on_tree_press` 对跳转列 `return "break"`）—— Qt 用
    `cellClicked`，而**选中行在按下时就已经变了**：点「房间号/主播」跳浏览器的同时会把下方
    SC/弹幕面板切走；粉丝牌任务表更麻烦——点主播名会顺手改掉「要执行任务的房间」。
    现新增 `ProtectedLinkTable`（房间表跳转列 0/1/3、任务表 0/1、持有表 2/3）：重写
    `selectionCommand` 对跳转列的鼠标**按下与松开都返回 `NoUpdate`**（Qt 在「按下未改变选中」
    时会在**松开时补一次选中**，只拦按下会导致保护在松手瞬间失效——这是调试时才发现的坑），
    松手且未拖动到其它单元格才发出 `linkClicked`；行拖动排序不受影响（基类仍记录按下项）。

## 已知差异（保留）

本表逐项核对后，**Tk 的界面功能已全部在 Qt 落地**（含上一轮补齐的 SC 用户名跳转、中键自动
滚动、列宽保护、复制提示、粉丝牌实时进度、表情条滚轮等）。仅剩以下「有意不同」或「不需要」的项：

- 弹幕表情提示定位：Qt 用 `QGuiApplication.screens()` 的**虚拟桌面并集**做越界回缩，
  副屏在主屏左侧/上方时也能贴光标显示（Tk 受 Windows 限制会被夹到主屏边缘，见 `tooltip_position` 注释）。
- 弹幕表情的呈现方式：Qt 版把表情图**内嵌进弹幕流**（图未下载完先显示触发词，图片到达后按
  `QTextBlock` **原地换图**，不影响滚动位置；点击图片同样能复制该条弹幕），悬浮提示只补触发词 /
  唯一标识等文字信息；Tk 版受 `Text` + `PhotoImage` 的性能限制不内嵌，仍靠悬浮看原图。
- 拖动窗口暂停 SC 自动换行的卡顿缓解（Tk `_on_root_configure`）：Qt 的文本重排为原生实现，
  不需要该变通（见 2.14，标 n/a）。
- 弹幕区也支持中键快速滚动：Tk 只绑定了 SC 区与调试日志，Qt 三处一致（超集，无副作用）。
- **房间独立窗口（ROADMAP 84）内部实现方式不同、对外行为一致**：Qt 把 SC / 弹幕做成可复用的
  面板组件（`qt_sc_panel.ScPanel` + `qt_dm_panel.DmPanel`），主界面与子窗口**共用同一份渲染代码**，
  宿主按 `room_id` 路由事件、历史结果按 token 回投；Tk 的主界面渲染与宿主控件强耦合（历史久、
  没有自动化 UI 测试兜底），为**不触碰这条最久经考验的路径**，`tk_room_window.RoomChatWindow`
  自己实现一份精简渲染（复用业务层与框架无关的纯函数，并带**自有**的历史读盘队列，绝不消费
  宿主的 `ui_queue`），由宿主在事件分发处按房间**广播**给窗口。两版对外行为一致（一房一窗、
  重复打开只前置、打开不抢焦点、不置顶、按房间记忆几何、随主窗口回收、删除房间即关窗、
  写操作沿用 `allow_write_operations` 与登录态门控）。另有一处小差别：Qt 窗口的弹幕发送
  颜色/模式下拉会按该房间从服务端拉到可用项（与主界面一致），Tk 窗口用内置预设（未接
  `dm_config`，后续要补的话按窗口缓存即可）。
- **直播预览是 Qt 专属**：Tk 没有可用的视频组件（内嵌播放需要 QtMultimedia / QtWebEngine 这类重依赖）。
  按双版方针「Tk 基础、Qt 增强」，预览只做在 Qt 版；框架无关的拉流与代理层
  （`api.get_live_stream_urls` + `live_preview.LiveStreamProxy`）两版共用，Tk 版将来若要跟进，
  只需接一个「把本地代理地址交给外部播放器」的入口即可。


