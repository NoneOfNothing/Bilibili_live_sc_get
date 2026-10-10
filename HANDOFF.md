# Handoff（交接文档）

> 面向接手者 / 后续 AI 会话的快速上手指引。**本文件不是用户文档**，用户文档是 `README.md`；
> 不要删除本文件，也不要把它当成 README 分发到用户侧。
>
> 项目当前状态：**Tk 版（`gui_app.py`）与 Qt 版（`qt_app.py`）GUI 功能已对等**，
> Qt 迁移清单（`QT_PORTING.md`）48 项全部 ✅，ROADMAP 的「GUI 迁移到 Qt」已标记完成。

## 1. 这是什么

监听 B 站直播间弹幕 WebSocket，自动抓取 SuperChat（SC）并落盘；附带一整套实时 GUI：房间管理、
SC 富文本查看、弹幕显示/发送、表情包、粉丝牌任务、开播悬浮窗提醒、Cookie 获取（读浏览器库 /
浏览器扩展插件）。

协议逻辑、存储、后台线程等**业务层**与 UI 框架**完全解耦**，Tk 版与 Qt 版共用同一套业务层。

## 2. 双 GUI / 双入口（重要）

### 双版并行开发方针（重要）

- **两版同时推进，Tk 为基础、Qt 为增强**：新功能先落 **Tk 版的基础形态**（保证两版都可用、
  行为一致、业务层共用），再在 **Qt 版**实现「需求更高」的形态（性能 / 体验上更重的做法）。
- 典型例子：**直播预览**——Qt 走 `QtMultimedia` + 本地回环代理直接内嵌播放，Tk 只提供
  「复制流地址 / 用外部播放器打开」这类基础能力。
- 因此两版出现差异是**预期内**的：Tk = 基线，Qt = 超集。差异必须写进 `QT_PORTING.md` 的
  「已知差异（保留）」并说明理由，避免以后被误当成 bug 回填；反之 Qt 已有的增强若 Tk 也能
  低成本实现，则可考虑回填 Tk。
- 测试与文档同步更新：业务层改动两边都跑；只在 Qt 落地的能力至少要有一条离线接线检查。

| 版本 | 入口脚本 | 入口模块 | 启动 | 说明 |
|------|---------|---------|------|------|
| **Tk** | `gui.py` | `blive_sc_get/gui_app.py`（~4426 行） | 双击 `start_gui.bat` | 功能最全、最久经考验 |
| **Qt (PySide6)** | `qt_gui.py` | `blive_sc_get/qt_app.py` + `qt_dm_panel.py` + `qt_medal_tab.py` + `qt_overlay.py` + `qt_preview.py`（直播预览，Qt 专属） | 双击 `start_gui_qt.bat` | 迁移目标，已功能对等且带 Qt 专属增强 |

- 命令行走 `main.py`（无 GUI）。
- 业务层（复用）：`api.py`、`client.py`（WebSocket/SC）、`storage.py`（JSONL/CSV 落盘）、
  `medal_runner.py` + `medal_tasks.py`（粉丝牌任务引擎）、`cookie_server.py`（插件收 Cookie）、
  `gui_config.py`（房间条目 + 界面偏好）、`app_config.py`（应用级配置：写操作开关、粉丝牌任务、运行日志）、
  `log_setup.py` + `log_categories.py`（运行日志：级别 / 区块过滤 / 轮转文件，Tk / Qt / CLI 共用）、
  `live_preview.py`（直播预览回环代理：注入防盗链请求头 + m3u8 分片改写；框架无关，两版共用）、`browser_cookie.py`、
  `browser_rooms.py`（`--auto` 实验性）、`overlay.py`（Tk 悬浮窗）、`room_lock.py`、`protocol.py`。
- Qt 迁移只重写了「渲染/交互层」，文件清单见 `QT_PORTING.md`。

### 线程模型（两版一致，务必遵守）
- asyncio 核心跑在**后台守护线程**（`AsyncHub`），把事件丢进线程安全队列 `ui_queue`；
- 主线程用 `QTimer`（Qt 100ms）/ `after`（Tk）周期轮询队列并更新控件；
- **绝不在后台线程直接触碰 UI 对象**。跨线程数据一律走 `ui_queue`。

## 3. 怎么跑

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# Qt GUI
.venv\Scripts\python qt_gui.py          # 或双击 start_gui_qt.bat
# Tk GUI
.venv\Scripts\python gui.py              # 或双击 start_gui.bat
# CLI（无 GUI）
.venv\Scripts\python main.py 直播间号
```

## 4. 测试

```bash
.venv\Scripts\python -m unittest discover -s tests -q   # 当前 611 项，全绿
```

没有网络依赖。改动后**必跑**：`py_compile` + 这套 unittest；涉及 Qt 控件逻辑时先在
`QT_QPA_PLATFORM=offscreen` 下做实例化冒烟（本项目惯例：临时 `_smoke_qt*.py`，验证后删除）。

## 5. 关键文件速查

| 文件 | 作用 |
|------|------|
| `blive_sc_get/qt_app.py` | Qt 版主窗口（房间/S C/弹幕/粉丝牌/调试页签、事件轮询、悬浮窗、未读徽标、常驻弹幕开关） |
| `blive_sc_get/qt_dm_panel.py` | Qt 弹幕区：显示/裁剪/点击复制/@回复/右键回复/表情悬浮提示/发送/表情面板（`DmPanel(host, room_provider)`：主视图跟随选中房间、独立窗口注入固定房间） |
| `blive_sc_get/qt_sc_panel.py` | Qt SC 面板组件：SC 文本/头部/累计/未读徽标/历史分页/删除置灰/点击跳转；`ScPanel(host, room_provider)`，历史结果按 `token` 回投（ROADMAP 84 抽出的视图级组件） |
| `blive_sc_get/qt_room_window.py` | Qt 房间独立窗口：`RoomChatWindow`=ScPanel+DmPanel，一房一窗、不抢焦点、不置顶、几何记忆、随宿主回收 |
| `blive_sc_get/tk_room_window.py` | Tk 房间独立窗口：`RoomChatWindow(tk.Toplevel)`（自带渲染与历史小队列，不消费宿主 `ui_queue`），由宿主按房间广播事件 |
| `blive_sc_get/qt_medal_tab.py` | Qt 粉丝牌页：列表/任务/手动+自动执行/顺序跟随/缓存粉丝牌名与等级 |
| `blive_sc_get/qt_overlay.py` | Qt 开播悬浮窗（`Qt.Tool|FramelessWindowHint|WindowStaysOnTopHint` + `WS_EX_NOACTIVATE` 不抢焦点，自下而上堆叠） |
| `blive_sc_get/gui_app.py` | Tk 版完整实现（功能对等参照物） |
| `QT_PORTING.md` | Tk→Qt 逐项核对表（当前 48/48 ✅） |
| `ROADMAP.md` | 历史功能/优化/BUG 追踪 |
| `CHANGELOG.md` | 版本更新日志（读写口径以它为准） |

## 6. 配置与数据（别搞乱）

- `config.json`（根目录，**不入库，首次运行自动生成**）：**写操作总开关** `allow_write_operations`
  （默认 `false`，只读；发送弹幕/自动点赞等需显式改 `true` 重启生效）、`emoticon_tooltip` 悬浮提示字段、
  `medal_tasks`（自动任务总开关 + 间隔 + 重试上限）。
- `data/gui_rooms.json`：房间条目 + 界面偏好 + 每房间表情包记忆。
- `cookie.txt`（不入库）：登录 Cookie；`SESSDATA` 用于读粉丝牌、`bili_jct` 用于写操作。
- 数据落盘在 `data/room_<id>/`：`sc_*.jsonl/csv`、`dm_YYYYMMDD.jsonl`、`deleted_ids.json`、
  `.room_lock`（房间互斥锁，残留无害）。

## 7. 已知缺口 / 后续建议（未做，非阻塞）

1. ~~Qt 弹幕表情「原图预览」仍是文字提示~~ **已完成**：Qt 版现在把表情图**内嵌进弹幕流**（未下载完
   先显示触发词，图片到位后按 `QTextBlock` 原地换图），并带「弹幕表情图」开关（关闭即把已显示的图片
   还原为文字、悬浮恢复为看原图；重新开启再把已有触发词换回图片）。
2. **直播预览（ROADMAP 63）P1~P4 全部完成**：P1 单路预览（Qt 版浮窗 `qt_preview.py` + 回环代理
   `live_preview.py` + 定时换源 + 未登录仅 360P/720P + **断流自愈**：5 秒心跳检查「在播 + 未暂停 +
   播放器停住」即自动重新拉流，含 15 秒起播宽限期与 3 次重试上限）；P2 观看时长上报
   （`webHeartBeat` 心跳 + 浮窗「上报观看时长」开关 + 「已上报 mm:ss」，默认关闭、受
   `allow_write_operations` 与登录态双重约束，只对主路、只在真正在看时上报）；P3 多路预览
   （最多 4 路宫格、仅主路出声、副路卡顿或 CPU 偏高自动停路、每路**独立**播放器与回环代理）；
   P4 打磨（加密房间密码——**只存内存**不落盘、清晰度记忆 `ui.preview_quality`、失败提示文案、
   双击放大 / 双击房间行加入预览），以及后续按反馈补的**自动追边**（实测播放端固有落后约
   0.1~0.2 秒/分钟，累计超过 `preview.max_drift_sec`（默认 3 秒）自动重载跳到最新；代理转发
   改为 `iter_any()` 不攒块）。
   Tk 版按双版方针只保留框架无关的拉流与代理层，未接界面。
3. **`--auto` 自动跟随浏览器**是实验性实现（README 明确不推荐），磁盘快照字节扫描可靠性有限。
   后续方向（讨论过未实施）：把浏览器扩展升级为「常驻双向通道」以替代磁盘快照监控；弹幕/表情
   处理做成可配置；SQLite 归档去重迁移数据层；过滤日志 + 崩溃自愈的可观测性增强。
4. Tk 版仍是「另一份完整实现」：改业务层时要两版同步；若日后只保留 Qt，可删除 `gui_app.py` 的
   Tk 部分但需谨慎（`CLI`、`overlay.py`、工具函数共享，先解耦再删）。

## 8. 本会话最近完成（会话 2026 批次 Qt 收尾）

- 修复 Qt 版弹幕接收链路 bug：原来从 `payload["batch"]` 取弹幕，客户端实为逐条 emit 单个 dict；
  现宿主 `_dm_pending` 累积、每轮询按选中房间聚合后 `append_dm_batch`（`qt_app.py`），
  `append_dm_batch` 改为消费 dict 负载。
- 弹幕交互（`qt_dm_panel.py`）：左键用户名→个人空间、左键正文→复制（带行尾/空白误触保护）、
  右键「回复该弹幕 / @该用户」、表情悬浮提示（300ms 去抖）。
- 弹幕开关移到**宿主常驻位** `qt_app.dm_toggle`（拆分器上方）——原开关在面板内部、面板一隐藏开关
  就消失导致「找不到弹幕区/开关」，已修复。
- 未读徽标（SC/弹幕），开播悬浮窗（Qt 版），SC 头显粉丝牌名/等级。
- 冒烟验证过：offscreen 实例化、开关往返、悬浮窗弹出/关闭、弹幕渲染/交互；250 单测全绿。

## 9. 房间独立窗口（ROADMAP 84，两版）

房间列表右键「打开独立窗口」→ 只含该直播间 SC 区 + 弹幕区的独立窗口（一房一窗、可多个）。

- **Qt**：`qt_sc_panel.ScPanel` + `qt_dm_panel.DmPanel` 组成 `qt_room_window.RoomChatWindow`。
  宿主 `qt_app` 维护三个注册表：`_sc_panels`（token → 面板，历史结果按 token 回投）、
  `_dm_panels`（列表）、`_room_windows`（room_id → 窗口）；`sc`/`delete` 经 `sc_panels_for`
  分发、`dm` 由 `_dm_pending` **按房间分桶**后逐房间渲染、`status`/`online_count`/`guards`
  经 `_refresh_sc_header` 顺带刷新窗口标题；`DmPanel.selected_room` 改为 `room_provider`
  注入；`_dm_rooms()`（主视图选中房间 + 各窗口房间）驱动 `_apply_dm_gate`
  （独立窗口弹幕区恒显示，主界面开关关着也收）。
- **Tk**：`tk_room_window.RoomChatWindow` 自带一份精简渲染与**自有历史队列**
  （`LOCAL_POLL_MS` 轮询，绝不 `get` 宿主的 `ui_queue`）；`gui_app` 在 `_on_client_event` /
  `_poll_queue` 里按房间广播 `on_sc` / `on_delete` / `on_dm_batch` / `on_meta_changed` /
  `on_dm_send_result` / `on_emoticons` / `on_emoticon_image`，右键入口为
  `tree.bind("<Button-3>", ...)`（此前房间列表没有右键菜单）。
- **几何记忆**：`data/gui_rooms.json` 的 `ui.room_windows`（`room_id -> {x,y,width,height}`），
  解析在 `gui_config.parse_room_window_rects`，恢复前用 `clamp_window_rect`
  （两版各一份纯函数）收拢到屏幕内；移动/缩放去抖 1 秒写盘 + 关窗立即写。**重启只恢复几何、
  不自动重开窗口**。
- 写操作（发弹幕/表情）沿用 `config.json` 的 `allow_write_operations` 与登录态门控。
- **「弹幕表情图」开关**统一走 `qt_app.set_dm_emoticon_image`：主界面底部与**每个独立窗口底部**
  各有一个勾选框，任一处切换都会广播给所有 `DmPanel` 并回写所有勾选框（`blockSignals` 防回环），
  偏好落 `ui.dm_emoticon_image`；新开的窗口按当前偏好初始化。
  （Tk 版不内嵌表情图，没有这个开关。）
- **日志**：打开 / 前置 / 关闭窗口各有 info；几何记忆的**写入与恢复**各记 debug（「位置没记住」
  时直接对日志）；**弹幕接收门控**（哪些房间在收弹幕）在**变化时**记一条 debug——
  `client.set_danmaku_enabled` 返回「是否变化」，宿主据此汇总，排查「独立窗口里没弹幕」靠它。

## 10. 排序方案（ROADMAP 85，两版共用同一份逻辑）

- 排序栏只有**下拉**与**最右的一个「ⓘ」按钮**（**选中即生效**，不再有「排序」按钮与
  「直播中置顶」勾选框），选择记在 `ui.sort_mode`；旧配置的 `pin_live=true` 在
  `gui_config.load_ui_prefs` 里迁移为 `status`。
- **排序说明收在「ⓘ」按钮里**（两版位置一致：排序栏最右）：文案是共用的 `SORT_MODE_HELP`
  （多行，逐条写清该方案规则与能不能拖动），点击弹出完整说明（Qt `QMessageBox` / Tk
  `messagebox.showinfo`，标题带方案名），Qt 另有悬浮提示。**长文案不再铺在栏里**——原先那行
  随方案变化的提示在窗口变窄时会被裁掉（用户实测反馈「太长、显示不全」）。
- **两层顺序**：`自定义顺序`（用户拖出来的，**唯一持久化**到 `gui_rooms.json` 的 `rooms`）与
  `显示顺序`（= 自定义顺序 + 方案规则合成）。Qt 里分别是 `_custom_order` / `_room_order`
  （`_save_config` 只写前者）；Tk 里是 `entries` 顺序（自定义）与 tree 行序（显示）。
- **规则**（纯函数 `gui_app.order_room_ids`）：`manual` 原样返回；`room` 按房间号；`anchor`
  按主播名（无名者最后）；`status` = 正在直播的按**开播先后倒序** + 已下播的按**关播先后倒序**
  + 从未开播的按自定义顺序，**轮播中按未开播处理**（不单独成段）。
- **实时重排**：开播/关播跳变（弹幕推送与 `_on_room_info` 的只读刷新都算）经
  `gui_app.update_live_activity` 记录先后标记后重排，且**仅 `status` 方案**重排。
  标记**不用时钟**：Windows 上 `time.monotonic()` 精度约 15ms，同一轮事件里几次状态变化会
  撞成同值（实测踩到），改为「现有标记最大值 + 1」的自增标记。
- **拖动只在 `manual`（自定义排序）可用**：Qt 看 `ProtectedLinkTable.drag_enabled` +
  `on_drag_blocked`；Tk 看 `_drag_allowed()`（在 `_on_tree_press` 里就拦下，不记录拖动行）。
  被拒时把「ⓘ」临时变成「⚠」（Qt 同步把悬浮提示改为「当前排序方案不可拖动，请先切到「自定义排序」」）
  并记 debug 日志，2.5 秒后恢复——原因就在按钮里，不弹窗打断。
- **时间基准是「真实墙钟 + 持久化」（ROADMAP 87，修掉「重启后顺序被打乱」）**：
  `live_started_at`（**真实开播时刻**，与第 11 节的「已播」共用同一字典：接口值优先、
  可校正本地观测、下播清空）与 `_offline_at`（关播时刻，纯函数 `update_offline_at`）
  都写进 `gui_rooms.json` 的 `ui.live_started_at` / `ui.live_offline_at`，启动时经
  `prune_live_marks` 读回（**规则不同**，ROADMAP 110：开播时刻 >24 小时丢弃——跨场次
  沿用会把「已播」算错；关播时刻**永久保留**、只丢未来 / 非法值——它只参与排序、旧记录
  依然准确；恢复后 `backfill_offline_marks` 给没有关播记录的房间补 `NEW_ROOM_MARK_S`
  （2000-01-01）默认标记，让新监控 / 从未观察到直播的房间确定性沉底）。排序时 `live_since`
  直接传 `live_started_at`，**旧的自增标记 `_live_since` 已整体弃用**。
  因此**重启后顺序不变**；启动时就已在直播的房间等于用「已播」时长**反推**了开播时间。
  `update_offline_at` 靠 `was_live` 只认「直播中 → 非直播中」的真实跳变（重复上报不推后、
  从未开播不留记录），同一轮撞值用「现有最大值 + 1 毫秒」保证仍可排序。
  **日志**：启动恢复由 `restore_live_marks` 记一条（恢复条数 + 丢弃的过期/非法条数，
  无记录时降 debug）；时间基准每次变化记一条（房间号、原状态、开播/关播时刻、已落盘）；
  新加房间已在直播、删房清理各记一条 debug。时间串统一走 `format_live_mark`。
- **启动即应用方案 + 重排合并执行（ROADMAP 88）**：Tk 的 `_populate_rows` 按
  `_sorted_room_ids()` 填充（此前固定按配置顺序 → 表现为「记住了方案却仍是自定义顺序」）；
  状态跳变（**不限**时间基准是否变化）只是把 `_sort_dirty` 置真并记 debug，由本轮 100ms
  轮询末尾统一 `_apply_sort()` 一次——启动时 N 个房间的状态事件不会逐个重排。
- 测试：`tests/test_sort_modes.py`（排序合成、开播/关播时间、持久化与脏数据过滤、文案），
  `tests/test_qt_wiring.py` 另有接线检查（切换即生效并落盘、禁用拖动、保存按自定义顺序、
  时间基准的读回/落盘/删房清理）。

## 11. 直播时长显示（ROADMAP 86，两版）

SC 头部信息行新增 `已播 01:23:45`（排在「舰长」之后、粉丝牌之前），**仅「直播中且已拿到起点」时出现**。

- **数据来源（已实测）**：`api.parse_live_started_at`（纯函数）把接口给的开播时刻归一化为 epoch 秒，
  写回房间信息 dict 的 `LIVE_STARTED_AT_KEY = "live_started_at"`。两种形态：
  H5 `getH5InfoByRoom` 的 `room_info.live_start_time` 是**秒级时间戳**（**优先**，且
  `get_full_room_info` 本来就在调它 → 零额外请求）；`room/v1/Room/get_info` 的 `live_time` 是
  **北京时间字符串**（回退，解析必须显式按 UTC+8，否则非东八区会整体偏 8 小时）；
  候选 `getRoomInfoOld` 实测 `code=-400`、不可用。占位 `"0000-00-00 00:00:00"`、`0`、非法、
  未来 2 分钟以上一律 `None`（**不在解析层编造时间**，否则时长会凭空开始计时）。
- **传输**：`client._emit_status` 把 `live_started_at` 放进 status 事件且**非直播中自动清空**；
  两版只读查询路径（`_async_fetch_room_info` / `_async_fetch_uid`）的 `room_info` 事件也带上它，
  两条来源共用同一个键。
- **起点维护**：两版共用纯函数 `gui_app.update_live_started_at`，宿主字典 `live_started_at`
  （Qt / Tk 各一份）。规则：接口值优先并可**校正**先到的本地观测（开播推送常早于接口刷新）；
  **同状态重复上报不漂移**（周期复核每 5 分钟一次，若每次重记，起点会被一路推后、时长永远从零开始）；
  下播清空。**与排序用的 `_live_since`（自增标记）完全分离，互不影响**。
- **渲染**：共享纯函数 `live_duration_text` / `format_live_duration`（固定两位补零、超 24 小时按
  累计小时、负数钳 0；未直播或无起点返回 `None`）；渲染点为 Qt `qt_sc_panel.ScPanel.update_header`
  与 Tk `gui_app._update_sc_header` + `tk_room_window.RoomChatWindow._update_sc_header`。
- **每秒跳动**：复用现有 100ms 轮询做 1 秒节流，不新增定时器——Qt `ScPanel.poll_tick` → `_tick_header`、
  Tk `gui_app._tick_live_duration`（在 `_poll_queue` 里调用）；只刷「直播中且有起点」的当前房间与各
  独立窗口（Tk 走 `RoomChatWindow.refresh_header`，只刷头部、不重设窗口标题）。
- 测试：`tests/test_gui_support.py`（解析 / 起点维护 / 格式化 / 显示门控）、`tests/test_qt_wiring.py`
  与 `tests/test_room_windows.py`（接线 + 每秒节流 + 非直播中清空）。

## 12. 协作约定（请遵守）

- 沟通语言：中文。
- **不修改 `README.md` 与文档性内容，除非被明确指示**；`QT_PORTING.md`/`ROADMAP.md`/`CHANGELOG.md`
  是开发追踪记录，改代码后应同步。
- 保持「业务层与 UI 解耦」的架构；新写 UI 走 Qt 版。
- 默认只读，写操作相关功能受 `config.json` 控制且默认关闭——不要绕过该开关自动发送/点赞。
- 弹幕区有 4000 行上限（超限裁掉最旧一半），不要去掉该保护（历史实测会卡死）。

## 13. 本轮补充（ROADMAP 95–98，会话 2026-09-27）

四件事都已落地，含离线测试与文档；两版保持对等（纯函数两版共用，宿主各接一遍）。

- **95 · Qt 历史分页的视口**：更早的记录是插在文档最前端的，Qt 不会自己调滚动条。现在插入前记下
  「视口顶端那条记录」的字符位置与当时的字符总数（顶部若是状态行则下移一条），插完按 Tk `see()` 的
  **最小滚动**摆视口：锚点被新内容挤出视口**下方**就贴**底边**（新加载那一页立刻填满视口、接缝停在
  最下一行）、挤到上方才贴顶边、本来就在视口内则一点不动。判定抽成纯函数
  `gui_app.scroll_value_for_anchor`。**踩过的坑**：第一版把锚点钉在**顶边**，新加载的一整页全落在
  视口外、画面与加载前完全一样，被当成「滚到顶没触发加载」报回来（触发其实是好的）——排查这类问题
  看 `data` 区块两条日志：`历史 SC 读取完成`、`历史翻页后视口`。顶部状态行只留一条，并补上每页
  边界分割点「（已读取 x 条历史记录）」。
- **96 · Qt 表情面板**：三处与 Tk 对齐（下拉跟随页码、刷新保持当前这个包、展开先用宿主缓存渲染，
  避免冷却挡下刷新时留着上一个房间的列表），记忆升级为**按包 id**——实测一个房间 13 个包里有 9 个都叫
  「装扮表情」，只比包名永远落在第一个；`gui_rooms.json` 的 `emoticon` 段多一个 `id` 键（老配置缺键按 0，
  等价退回按包名找）。纯函数 `emoticon_package_id`。
- **97 · 排序用的下播时刻**：新增关注列表接口 `xlive/web-ucenter/user/following` 的 `record_live_time`
  （唯一能给出**真实下播时刻**的公开来源；只覆盖本账号关注的主播，20 条/页、实测 950 个主播约 4.8 秒），
  再加退出时给仍在直播房间记的下播估算（＝退出时刻，写进 `ui.live_offline_at`），排序取两者**较晚**者；
  纯函数 `gui_app.merge_offline_marks`、抓取入口 `api.get_following_live_marks(wanted)`，只在「按直播状态」
  下抓（10 分钟冷却、未登录/失败只记日志不打扰）。
- **98 · 启动自动选中上次的直播间**：新增 `ui.selected_room`（切房即写、值变才落盘），启动优先恢复它，
  **恢复前核对房间是否还在**（已删除则回退成「显示顺序第一行」）；顺带把 Qt 的「第一行」从
  `entries` 第一个改成**显示顺序**第一行（此前与 Tk 不一致）。多选态不恢复。
- **日志覆盖**：以上路径都补了留痕——分页完成与翻页后视口、表情面板渲染到哪个包、关注列表的三种
  跳过原因与空结果、启动恢复选中失败的原因；级别沿用项目约定（用户动作 info、细节与判定 debug），
  不记录 cookie。

## 14. 接口自检结论与根目录开发脚本（会话 2026-10-01）

### 14.1 为什么 SC 只能走 WebSocket（2026-09-30 用本机 `cookie.txt` 实测，脚本已删，结论归档在此）

**结论：B 站目前不通过 REST 接口对外提供 SC 列表**，SC 只能靠弹幕 WebSocket 实时推送
（`cmd=SUPER_CHAT_MESSAGE`）获取；「进场时已有的 SC」也拿不到。逐条实测（`nav` 登录态 OK、cookie 有效）：

| 接口 | 实测结果 |
|------|----------|
| `xlive/web-room/v1/index/getInfoByRoom` | `code=0`，返回完整房间信息（87 个顶层字段） |
| 同上返回里的 `super_chat_message` | 恒为 `null` —— 2019 年的老字段，已失效 |
| 同上返回里的 `super_chat_info.message_list` | 200 个在播房间（跨虚拟主播 / 娱乐 / 游戏等分区）**全为空** → 拿不到 SC |
| `av/v1/SuperChat/config` | 可用（返回 SC 配置，含当前登录用户信息） |
| `av/v1/SuperChat/getRankMessageList` / `getAnchorRankedList` | `code=0` 但 `data={}` 空 |
| `av/v1/SuperChat/getMessageList`（老） | `{"code":0,"data":{"list":null}}` |
| `xlive/…/getDanmuInfo` | **−352 风控**（加 WBI 签名同样 −352）；它是连弹幕服务器（拿 token/host）的前置，被风控会影响 WebSocket 连接 |

需要复测时按上表逐接口重跑即可（当初的自检脚本 `_test_getInfoByRoom.py` 已删除，结论留在这里）。

### 14.2 根目录开发脚本的去留（2026-10-01 整理）

- **保留 `_spike_live_preview.py`**：ROADMAP 63 明确约定「保留在工作区供复测」，`requirements.txt`
  也引用它复测本机解码内核（需 PySide6≥6.8 / FFmpeg 默认后端）。
- **保留 `_dm_stress_test.py`**：弹幕写入压测小程序（独立、不依赖项目代码），CHANGELOG 里「弹幕卡死」
  那几条结论就是它跑出来的；改动弹幕区渲染或行数上限时用它复现临界点。
- **已删 `_diag_audio_device.py`**：结论与做法已固化在 ROADMAP 76 / CHANGELOG（含两次踩坑与最终方案），
  且正式程序自带 10 秒巡检 + `window` debug 日志（打印系统默认与各路实际设备），诊断手段不再需要单独脚本。

## 15. 弹幕发送行的记忆 / 历史 / 填入（ROADMAP 106–109，会话 2026-10-09）

三处发送行（Tk 主界面、Tk 房间独立窗口、Qt `DmPanel`）同时改了三件事，实现都收敛在
「共享纯函数 + 各视图各接一遍」：

- **106 · 颜色 / 模式按房间记忆**：存 `RoomEntry.dm_color` / `dm_mode`（**名称**，不是数值），
  切房套用、改了下拉即落盘；记忆项在本房间可用项里已不存在时，由共享纯函数
  `resolve_dm_choice` 回落默认并**改写记忆**——判定只发生在**拿到本房间可用项**时
  （切房瞬间下拉里还是上一个房间的列表，那时判定的「失效」不作数）。房间独立窗口只用内置
  预设，它的记忆项不在预设里时按默认显示但**不改写记忆**（那份记忆对主界面可能仍有效）。
- **107 · 快捷弹幕取消条数上限**：删掉 `QUICK_DANMAKU_MAX`、清洗里的截断、两版对话框的拦截与
  占位文案；Tk 管理对话框补了纵向滚动条。**别再引入任何截断**——截断会在「打开管理对话框再
  关闭」时把超出的条目写没（与 ROADMAP 91 的 20 字截断是同一个坑）。
- **108 · 输入框 ↑ / ↓ 翻历史**：按房间记**本次运行内发送成功**的文字弹幕（只在内存、上限
  `DM_SEND_HISTORY_MAX = 50`、表情包不入栈），历史本体存**宿主**、三处视图共用同一份；
  语义是纯函数 `dm_history_push` / `dm_history_step`（到最早即停、↓ 越过最新回到草稿）。
  Tk 的上下键处理器**必须返回 `"break"`**；Qt 走 `eventFilter`——注意该过滤器装在多个控件上，
  且 `dm_text` 早于输入框创建，**一律用 `_key_entry` 判等**（构建期可能还是 `None`），
  直接摸 `self.dm_send_entry` 会在构建期抛 `AttributeError`（探针实测踩到、已加回归测试）。
- **109 · 点击弹幕填入发送框**：全局偏好 `ui.copy_dm_fill`（默认关），勾选后点正文**覆盖式**
  填入发送框、不占用剪贴板；发送框不可用时**回退为复制**并在提示行说明原因。宿主统一入口
  `set_copy_dm_fill`（值变才落盘 + 广播），各视图只回写勾选框。
- **测试**：`tests/test_dm_send_ux.py`（33 项：纯函数语义 + 三处视图 AST 接线，含
  「eventFilter 构建期不许直接访问输入框」与「`DEFAULT_UI_PREFS` 的 `copy_dm_fill` 必须读得回」
  两道守卫）；故障植入验证——临时植入 7 处故障共触发 9 项失败，还原即全绿。