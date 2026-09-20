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
.venv\Scripts\python -m unittest discover -s tests -q   # 当前 436 项，全绿
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

## 10. 协作约定（请遵守）

- 沟通语言：中文。
- **不修改 `README.md` 与文档性内容，除非被明确指示**；`QT_PORTING.md`/`ROADMAP.md`/`CHANGELOG.md`
  是开发追踪记录，改代码后应同步。
- 保持「业务层与 UI 解耦」的架构；新写 UI 走 Qt 版。
- 默认只读，写操作相关功能受 `config.json` 控制且默认关闭——不要绕过该开关自动发送/点赞。
- 弹幕区有 4000 行上限（超限裁掉最旧一半），不要去掉该保护（历史实测会卡死）。