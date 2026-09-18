"""Qt 版「宿主 ↔ 子模块」属性接线检查（离线，不启动界面）。

这类不匹配只在**运行期**以 ``AttributeError`` 现身；若发生在 Qt 槽函数里，异常只打
到 stderr（``pythonw`` 启动时无控制台，完全看不见），用户看到的现象是「界面某一块
莫名其妙不再刷新」。实测踩过一次：``qt_medal_tab`` 访问 ``host._medal_emit``
（宿主里实际叫 ``_medal_event``），导致粉丝牌页切换房间时下方的自动开关勾选框
永不更新，还连带让「发弹幕 / 点赞」按钮与自动任务周期全部失效。
"""

import ast
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "blive_sc_get"

HOST_MODULE = "qt_app.py"
HOST_CLASS = "QtScMonitorApp"

# 子模块 → 其中会持有 `self.host` 引子的类
CHILD_MODULES = {
    "qt_medal_tab.py": ("MedalTab",),
    "qt_dm_panel.py": ("DmPanel",),
    "qt_overlay.py": ("QtToastOverlayManager", "QtToastWindow"),
}

# 宿主持有的子模块引子名 → 对应模块
HOST_HOLDERS = {
    "medal_tab": "qt_medal_tab.py",
    "dm_panel": "qt_dm_panel.py",
    "overlay": "qt_overlay.py",
}


def _tree(module: str) -> ast.Module:
    return ast.parse((PKG / module).read_text(encoding="utf-8"))


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到类 {name}")


def _self_attr_names(tree: ast.Module, class_name: str) -> set:
    """类里所有 ``self.<name>``（读取或赋值）的名字。"""
    names = set()
    for node in ast.walk(_class_node(tree, class_name)):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            names.add(node.attr)
    return names


def _attr_of(tree: ast.Module, class_names: tuple, base_names: tuple) -> set:
    """收集 ``<base>.<attr>``（base 为 ``self.host`` 或裸 ``host``）用到的属性名。"""
    found = set()
    for class_name in class_names:
        for node in ast.walk(_class_node(tree, class_name)):
            if not isinstance(node, ast.Attribute):
                continue
            value = node.value
            if (isinstance(value, ast.Attribute) and value.attr in base_names
                    and isinstance(value.value, ast.Name) and value.value.id == "self"):
                found.add(node.attr)
            elif isinstance(value, ast.Name) and value.id in base_names:
                found.add(node.attr)
    return found


def _attr_of_holder(tree: ast.Module, class_name: str, holder: str) -> dict:
    """收集 ``self.<holder>.<attr>`` → {attr: {所在方法名}}。"""
    found: dict = {}
    for node in ast.walk(_class_node(tree, class_name)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Attribute)
                    and sub.value.attr == holder
                    and isinstance(sub.value.value, ast.Name)
                    and sub.value.value.id == "self"):
                found.setdefault(sub.attr, set()).add(node.name)
    return found


class QtHostWiringTests(unittest.TestCase):
    """子模块与宿主之间的属性引用必须存在（静态即可发现，不必等运行期报错）。"""

    @classmethod
    def setUpClass(cls):
        try:  # 需要 PySide6 才能拿到继承来的 Qt 成员（setEnabled 等）
            from blive_sc_get.qt_app import QtScMonitorApp
        except Exception as exc:  # pragma: no cover - 环境缺 PySide6 时跳过
            raise unittest.SkipTest(f"无法导入 Qt 宿主模块：{exc}")
        cls.host_cls = QtScMonitorApp

    def _known_host_names(self) -> set:
        tree = _tree(HOST_MODULE)
        # dir() 覆盖 Qt 基类方法与属性；AST 覆盖 __init__ 里赋值的实例属性
        return set(dir(self.host_cls)) | _self_attr_names(tree, HOST_CLASS)

    def test_child_modules_only_touch_existing_host_members(self):
        known = self._known_host_names()
        missing = {}
        for module, classes in CHILD_MODULES.items():
            for name in _attr_of(_tree(module), classes, ("host",)):
                if name not in known:
                    missing.setdefault(name, set()).add(module)
        self.assertEqual(
            missing, {},
            "以下 host.<name> 在 QtScMonitorApp 上不存在（运行期会 AttributeError）："
            + "；".join(f"{n}（{', '.join(sorted(m))}）"
                        for n, m in sorted(missing.items())))

    def test_host_only_touches_existing_child_members(self):
        import importlib

        host_tree = _tree(HOST_MODULE)
        missing = {}
        for holder, module in HOST_HOLDERS.items():
            module_tree = _tree(module)
            mod = importlib.import_module(f"blive_sc_get.{module[:-3]}")
            known = set()
            for class_name in CHILD_MODULES[module]:
                # dir() 覆盖 Qt 基类成员（setEnabled 等）；AST 覆盖实例属性
                known |= set(dir(getattr(mod, class_name)))
                known |= _self_attr_names(module_tree, class_name)
            for name, where in _attr_of_holder(host_tree, HOST_CLASS, holder).items():
                if name not in known:
                    missing.setdefault(f"self.{holder}.{name}", set()).update(where)
        self.assertEqual(
            missing, {},
            "宿主访问了子模块上不存在的成员（运行期会 AttributeError）："
            + "；".join(f"{n}（{', '.join(sorted(w))}）"
                        for n, w in sorted(missing.items())))


def _method(tree: ast.Module, class_name: str, name: str):
    """取类里的方法节点（含 ``async def``）。"""
    for node in ast.walk(_class_node(tree, class_name)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"未找到 {class_name}.{name}")


def _names(node) -> set:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _attr_names(node) -> set:
    """节点里出现过的属性名（``a.b`` 取 ``b``）。"""
    return {sub.attr for sub in ast.walk(node) if isinstance(sub, ast.Attribute)}


def _called_attrs(node) -> set:
    return {sub.func.attr for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)}


class QtDanmakuClickTests(unittest.TestCase):
    """弹幕「点击复制正文」的接线（静态检查）——修复过的两处回归都靠它兜住：

    1. 正文段必须带 ``_DM_BODY_KEY`` 标记：缺 dmid 的弹幕原先**没有任何点击分支**，
       点正文毫无反应（表现为「部分弹幕无法复制」）；
    2. 命中判定必须带几何约束（``cursorRect``）：只用 ``characterAt`` 判断字符是否空白
       时，行尾右侧 / 文本区下方的空白会被吸附到最近的字符上（表现为「点空白也复制」）。
    """

    def setUp(self):
        self.tree = _tree("qt_dm_panel.py")

    def test_body_segment_is_tagged_and_clicked(self):
        constants = {target.id for node in self.tree.body if isinstance(node, ast.Assign)
                     for target in node.targets if isinstance(target, ast.Name)}
        self.assertIn("_DM_BODY_KEY", constants, "缺少正文段标记常量")
        self.assertIn("_DM_BODY_KEY", _names(_method(self.tree, "DmPanel", "append_dm_batch")),
                      "渲染正文段时未打 _DM_BODY_KEY 标记")
        self.assertIn("_DM_BODY_KEY", _names(_method(self.tree, "DmPanel", "_on_dm_press")),
                      "点击处理未依据 _DM_BODY_KEY 判定正文（缺 dmid 的弹幕将无法复制）")

    def test_click_hit_test_uses_geometry(self):
        hit = _method(self.tree, "DmPanel", "_clicked_char")
        self.assertIn("cursorRect", _called_attrs(hit),
                      "命中判定缺少几何约束（行高 / 行右边界）")
        fmt = _method(self.tree, "DmPanel", "_clicked_fmt")
        self.assertIn("_clicked_char", _called_attrs(fmt),
                      "_clicked_fmt 未走 _clicked_char 的几何命中判定")
        # 悬停（表情提示）与右键菜单同样要走命中判定，避免空白处误触发
        for name in ("_on_dm_motion", "_on_dm_context_menu"):
            self.assertIn("_clicked_fmt", _called_attrs(_method(self.tree, "DmPanel", name)),
                          f"{name} 未使用 _clicked_fmt 命中判定")


class QtDanmakuEmoticonImageTests(unittest.TestCase):
    """弹幕流内嵌表情图（Qt 版专有）的接线检查。

    链路：渲染时能拿到图就直接插入图片，拿不到就先显示触发词并**记下该行**；图片下载
    完成后把这些行原地换成图。任一环断开的表现都是「表情弹幕一直只有触发词文字」。
    """

    def setUp(self):
        self.tree = _tree("qt_dm_panel.py")

    def test_render_embeds_image_and_tracks_placeholder(self):
        batch = _method(self.tree, "DmPanel", "append_dm_batch")
        called = _called_attrs(batch)
        self.assertIn("_dm_emoticon_pixmap", called, "渲染时未查表情图")
        self.assertIn("_insert_dm_emoticon_image", called, "渲染时未插入表情图")
        self.assertIn("_remember_dm_image_block", called, "未记下待换图的行")

    def test_image_ready_replaces_placeholder_blocks(self):
        self.assertIn("_fill_dm_emoticon_blocks",
                      _called_attrs(_method(self.tree, "DmPanel", "on_emoticon_image")),
                      "图片到达后未回填弹幕流")
        fill = _method(self.tree, "DmPanel", "_fill_dm_emoticon_blocks")
        self.assertIn("_replace_block_emoticon", _called_attrs(fill),
                      "未把占位文字换成图片")

    def test_tooltip_shows_text_only(self):
        tip = _method(self.tree, "DmPanel", "_show_emoji_tip")
        args = [arg.arg for arg in tip.args.args]
        self.assertEqual(args[:2], ["self", "info"], "悬浮提示不应再接收图片参数")
        self.assertNotIn("pixmap", args)
        self.assertIn("emoticon_tooltip_text", _names(tip), "悬浮提示应按配置拼文字")


class QtPreviewWiringTests(unittest.TestCase):
    """直播预览（P1）的接线：宿主入口 → 浮窗 → 后台拉流 → 本地代理地址。"""

    def test_host_wires_preview_button_and_queue(self):
        host = _tree(HOST_MODULE)
        click = _method(host, HOST_CLASS, "_on_preview_clicked")
        called = _called_attrs(click)
        self.assertIn("_preview_window", called, "按钮未走惰性创建入口")
        self.assertIn("start", called, "按钮未启动预览")
        self.assertIn("remove_room", called, "按钮未停止预览（多路时只停选中的那几路）")
        self.assertIn("room_ids", called, "未按「已在预览」决定加入还是停止")

        constants = {node.value for node in ast.walk(_method(host, HOST_CLASS, "_poll_queue"))
                     if isinstance(node, ast.Constant)}
        for kind in ("preview_ready", "preview_error", "watch_reported", "watch_error"):
            self.assertIn(kind, constants, f"_poll_queue 未处理 {kind} 事件")

        close_consts = {node.value for node in ast.walk(_method(host, HOST_CLASS, "closeEvent"))
                        if isinstance(node, ast.Constant)}
        self.assertIn("preview", close_consts, "退出时未回收预览浮窗/代理")
        self.assertIn("_follow_preview",
                      _called_attrs(_method(host, HOST_CLASS, "_on_room_selected")),
                      "切换直播间时未跟随预览")

    def test_room_context_menu_previews_row(self):
        host = _tree(HOST_MODULE)
        build = _method(host, HOST_CLASS, "_build_rooms_tab")
        self.assertIn("customContextMenuRequested", _attr_names(build),
                      "房间列表未接右键菜单")
        menu = _method(host, HOST_CLASS, "_on_room_context_menu")
        called = _called_attrs(menu)
        self.assertIn("start", called, "右键菜单未启动预览")
        self.assertIn("remove_room", called, "右键菜单未提供「停止预览该房间」（只停这一路）")
        self.assertIn("set_main_room", called, "右键菜单未提供「设为主路」")
        self.assertIn("_select_room", called, "右键预览后未同步选中行")
        self.assertIn("_room_order", _attr_names(menu), "未按行取房间号")

    def test_stop_preview_closes_window_without_recursion(self):
        """「停止预览」= 停播放 + 回收代理 + **关窗**；closeEvent 不能再 close（递归）。"""
        tree = _tree("qt_preview.py")
        stop = _method(tree, "PreviewWindow", "stop")
        self.assertIn("close", [arg.arg for arg in stop.args.kwonlyargs],
                      "stop 应有 close 开关")
        defaults = [node for node in stop.args.kw_defaults if node is not None]
        self.assertTrue(any(getattr(node, "value", None) is True for node in defaults),
                        "stop 的 close 默认值应为 True（停止预览即关窗）")
        close_keywords = [kw.arg for node in ast.walk(_method(tree, "PreviewWindow", "closeEvent"))
                          if isinstance(node, ast.Call) for kw in node.keywords]
        self.assertIn("close", close_keywords, "closeEvent 未显式传 close，可能递归")

        host_close = _method(_tree(HOST_MODULE), HOST_CLASS, "closeEvent")
        exit_calls = [node for node in ast.walk(host_close)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "stop"]
        self.assertTrue(exit_calls, "宿主退出时未停止预览")

    def test_pause_button_pauses_instead_of_stopping(self):
        """浮窗内的按钮是「暂停/继续」（定格画面），不能等同于停止预览。"""
        tree = _tree("qt_preview.py")
        handler = _method(tree, "PreviewWindow", "_on_pause_clicked")
        called = _called_attrs(handler)
        self.assertIn("toggle_pause", called, "浮窗按钮未暂停/继续主路")
        self.assertNotIn("stop", called, "浮窗按钮不应停止预览（那会关窗并释放代理）")
        # 暂停 / 继续的实现在一路（PreviewTile）里：暂停走 pause()，继续走 _resume()
        tile_pause = _method(tree, "PreviewTile", "toggle_pause")
        self.assertIn("pause", _called_attrs(tile_pause), "未真正暂停播放")
        self.assertIn("_resume", _called_attrs(tile_pause), "浮窗按钮未提供「继续」")

    def test_reload_button_and_pause_resync(self):
        """「刷新」重新拉流跳到最新画面；暂停过久再继续也自动跳回最新。"""
        tree = _tree("qt_preview.py")
        build = _method(tree, "PreviewWindow", "_build_ui")
        self.assertIn("_on_reload_clicked", _attr_names(build), "控制条未接「刷新」按钮")
        handler = _method(tree, "PreviewWindow", "_on_reload_clicked")
        self.assertIn("_main_tile", _called_attrs(handler), "刷新未作用于主路")
        self.assertIn("reload", _called_attrs(handler), "刷新未重新拉流")
        reload_tile = _method(tree, "PreviewTile", "reload")
        self.assertIn("request_prepare", _called_attrs(reload_tile), "刷新未重新拉流（装载）")
        resume = _method(tree, "PreviewTile", "_resume")
        self.assertIn("PAUSE_RESYNC_S", _names(resume),
                      "暂停久了的滞后未处理（继续时应跳回最新画面）")

    def test_quality_switch_forces_reload(self):
        """换清晰度：必须先清空 source 再设置，否则同一地址不会重载（黑屏）。"""
        tree = _tree("qt_preview.py")
        ready = _method(tree, "PreviewTile", "on_streams_ready")
        set_source_calls = [node for node in ast.walk(ready)
                            if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "setSource"]
        self.assertGreaterEqual(len(set_source_calls), 2,
                                "换源前应先 setSource(QUrl()) 清空，再设新地址")
        self.assertIn("on_main_qualities", _called_attrs(ready),
                      "未按该房间可用清晰度收缩下拉档位（主路的可用档位驱动下拉）")
        prepare = _method(tree, "PreviewTile", "_async_prepare")
        constants = {node.value for node in ast.walk(prepare)
                     if isinstance(node, ast.Constant)}
        self.assertIn("accept_quality", constants,
                      "未把接口返回的可用档位带回主线程")

    def test_preview_plays_through_local_proxy(self):
        """每一路都走「后台拉直链 → 本机代理 → 播放器」这条链路。"""
        tree = _tree("qt_preview.py")
        prepare = _method(tree, "PreviewTile", "_async_prepare")
        called = _called_attrs(prepare)
        self.assertIn("get_live_stream_urls", called, "未拉取直播直链")
        self.assertIn("set_streams", called, "未把直链交给回环代理")
        self.assertIn("play_urls", _attr_names(prepare), "未使用代理的本地播放地址")

        build = _method(tree, "PreviewTile", "_build_ui")
        self.assertIn("QVideoWidget", _names(build), "缺少视频组件")
        self.assertIn("QMediaPlayer", _names(build), "缺少播放器")
        self.assertIn("QAudioOutput", _names(build), "缺少声音输出（主路出声靠它）")

        self.assertIn("available_qualities",
                      _names(_method(tree, "PreviewWindow", "_update_quality_choices")),
                      "清晰度档位未按登录态过滤（未登录应只有 720P）")

        ready = _method(tree, "PreviewTile", "on_streams_ready")
        self.assertIn("setSource", _called_attrs(ready), "未把本地地址交给播放器")

    def test_follow_only_changes_main_route(self):
        """「跟随选中房间」只切主路声音：绝不能增删或替换预览的路数。

        P3 早期版本在「只有一路」时会把那一路**换成**选中房间——于是在主界面点几下预览
        就被换掉了，也没法逐个把房间加进来（用户反馈「切换主界面的直播间就切换预览」）。
        """
        host = _tree(HOST_MODULE)
        follow = _method(host, HOST_CLASS, "_follow_preview")
        called = _called_attrs(follow)
        self.assertIn("set_main_room", called, "跟随未把选中房间设为主路")
        for forbidden in ("start", "remove_room", "replace_main"):
            self.assertNotIn(forbidden, called,
                             f"跟随不应改动预览路数（调用了 {forbidden}）")

    def test_remove_room_relayouts_and_zoom_reset(self):
        """停一路必须重排（否则被停的那格留黑框）；双击放大要能还原、放大路被关自动退出。"""
        tree = _tree("qt_preview.py")
        remove = _method(tree, "PreviewWindow", "remove_room")
        self.assertIn("_relayout", _called_attrs(remove),
                      "停一路后未重排布局（会残留黑框 / 格子错位）")
        zoom = _method(tree, "PreviewWindow", "toggle_zoom")
        self.assertIn("_zoomed_index", _attr_names(zoom), "双击未切换放大状态")
        self.assertIn("_relayout", _called_attrs(zoom), "双击放大后未重排")
        relayout = _method(tree, "PreviewWindow", "_relayout")
        self.assertIn("_zoomed_index", _attr_names(relayout),
                      "重排未考虑放大状态（被放大的那路关掉后不会退出放大）")

    def test_host_wires_room_double_click(self):
        """双击房间行也能加入 / 停止预览（Ctrl 多选之外的顺手入口）。"""
        host = _tree(HOST_MODULE)
        build = _method(host, HOST_CLASS, "_build_rooms_tab")
        self.assertIn("cellDoubleClicked", _attr_names(build), "房间列表未接双击")
        handler = _method(host, HOST_CLASS, "_on_room_double_clicked")
        called = _called_attrs(handler)
        self.assertIn("start", called, "双击未加入预览")
        self.assertIn("remove_room", called, "双击未停止预览")
        self.assertIn("link_columns", _attr_names(handler), "双击未避开跳转列")

    def test_auto_catch_up_wiring(self):
        """自动追边：播放中测「越看越落后」，超阈值就重新拉流跳到最新。"""
        tree = _tree("qt_preview.py")
        health = _method(tree, "PreviewTile", "health_tick")
        self.assertIn("_check_drift", _called_attrs(health), "播放中未测播放落后")
        check = _method(tree, "PreviewTile", "_check_drift")
        self.assertIn("needs_catch_up", _names(check), "未按阈值判定是否追边")
        self.assertIn("reload", _called_attrs(check), "追边未重新拉流跳到最新")

    def test_encrypted_room_password_flow(self):
        """加密房间（P4）：右键输入密码 → 只存内存 → 服务端校验 → 带密码重拉。"""
        tree = _tree("qt_preview.py")
        menu = _method(tree, "PreviewTile", "contextMenuEvent")
        self.assertIn("prompt_password", _called_attrs(menu), "格子右键菜单未提供输入密码")
        prompt = _method(tree, "PreviewTile", "prompt_password")
        self.assertIn("submit_password", _called_attrs(prompt), "输入密码后未提交给窗口")
        submit = _method(tree, "PreviewWindow", "submit_password")
        self.assertIn("_async_unlock", _called_attrs(submit), "提交密码后未走服务端校验")
        self.assertNotIn("_save_config", _called_attrs(submit),
                         "密码不应写入配置文件（只存内存）")
        prepare = _method(tree, "PreviewTile", "_async_prepare")
        self.assertIn("get_stream_info", _called_attrs(prepare), "拉流前未查加密状态")
        self.assertIn("pwd", _attr_names(prepare), "拉流未带密码")
        consts = {node.value for node in ast.walk(prepare) if isinstance(node, ast.Constant)}
        self.assertIn("encrypted", consts, "未把「加密未解锁」回投界面")
        host = _tree(HOST_MODULE)
        consts = {node.value for node in ast.walk(_method(host, HOST_CLASS, "_poll_queue"))
                  if isinstance(node, ast.Constant)}
        self.assertIn("preview_unlocked", consts, "_poll_queue 未处理密码校验结果")

    def test_quality_is_remembered(self):
        """清晰度记忆（P4）：启动读 ui_prefs、切换时写回；宿主提供保存入口。"""
        window = _tree("qt_preview.py")
        init = _method(window, "PreviewWindow", "__init__")
        consts = {node.value for node in ast.walk(init) if isinstance(node, ast.Constant)}
        self.assertIn("preview_quality", consts, "启动未读取上次用的清晰度")
        changed = _method(window, "PreviewWindow", "_on_quality_changed")
        consts = {node.value for node in ast.walk(changed) if isinstance(node, ast.Constant)}
        self.assertIn("save_preview_quality", consts, "切换清晰度未写回记忆")
        self.assertIn("getattr", _names(changed), "宿主方法应做存在性判断（防御式调用）")
        host = _tree(HOST_MODULE)
        save = _method(host, HOST_CLASS, "save_preview_quality")
        self.assertIn("_save_config", _called_attrs(save), "未写入 gui_rooms.json")
        self.assertIn("ui_prefs", _attr_names(save), "未写入界面偏好")

    def test_selected_rooms_helper_tolerates_missing_table(self):
        """启动早期（表格未建）刷新预览按钮不能崩。

        P3 多路改造把 ``refresh_preview_button`` 改成按「选中的房间」决定文案，而它在
        ``_build_rooms_tab`` 里**提前**被调用（那时 ``self.table`` 还没创建），于是启动即
        ``AttributeError: 'QtScMonitorApp' object has no attribute 'table'``——窗口一闪就退。
        所以选中查询必须容忍表格不存在。
        """
        host = _tree(HOST_MODULE)
        helper = _method(host, HOST_CLASS, "_get_selected_room_ids")
        self.assertIn("getattr", _names(helper),
                      "选中房间查询未容忍 self.table 尚未创建（启动早期会崩）")
        self.assertIn("table",
                      {node.value for node in ast.walk(helper) if isinstance(node, ast.Constant)},
                      "未对 self.table 做存在性判断")
        refresh = _method(host, HOST_CLASS, "refresh_preview_button")
        self.assertIn("_get_selected_room_ids", _called_attrs(refresh),
                      "预览按钮文案未按选中的房间决定")

    def test_multi_room_grid_and_resource_guard(self):
        """多路（P3）：宫格布局 / 每格独立播放器 / 上限拒绝 / 卡顿或 CPU 偏高自动停路。"""
        tree = _tree("qt_preview.py")
        relayout = _method(tree, "PreviewWindow", "_relayout")
        self.assertIn("grid_shape", _names(relayout), "未按路数排宫格")

        start = _method(tree, "PreviewWindow", "start")
        self.assertIn("_free_tile", _called_attrs(start), "加入路时未找空格子")
        self.assertIn("max_rooms", _attr_names(start), "加入路时未检查路数上限")
        self.assertIn("_set_main", _called_attrs(start), "加入新路后未把它设为主路")

        remove = _method(tree, "PreviewWindow", "remove_room")
        self.assertIn("is_active", _called_attrs(remove), "停一路时未判断是否还有其它路")
        self.assertIn("stop", _called_attrs(remove), "最后一路停掉时未收窗")

        # 资源保护：副路卡顿 → 停路；CPU 偏高 → 停最后加入的副路
        health = _method(tree, "PreviewTile", "health_tick")
        self.assertIn("drop_tile_for_failure", _called_attrs(health),
                      "副路重连失败后未停掉该路")
        cpu = _method(tree, "PreviewWindow", "_on_cpu_tick")
        self.assertIn("pick_victim_index", _names(cpu), "CPU 保护未选择要停的路")
        drop = _method(tree, "PreviewWindow", "drop_tile_for_failure")
        self.assertIn("remove_room", _called_attrs(drop), "自动停路未真正移除该路")

        # 声音只给主路：副路一律静音
        audio = _method(tree, "PreviewTile", "apply_audio")
        self.assertIn("is_main", _attr_names(audio), "未区分主路/副路的声音")


class QtDanmakuEmoticonSwitchTests(unittest.TestCase):
    """「弹幕表情图」开关的接线：界面勾选框 → 面板 → 渲染/还原。"""

    def test_switch_wired_from_host_to_panel(self):
        host = _tree(HOST_MODULE)
        handler = _method(host, HOST_CLASS, "_on_dm_emoticon_image_toggled")
        self.assertIn("set_emoticon_image_enabled", _called_attrs(handler),
                      "勾选框未把开关状态传给弹幕面板")
        self.assertIn("ui_prefs", _attr_names(handler), "开关状态未写入界面偏好")

    def test_render_and_strip_respect_switch(self):
        tree = _tree("qt_dm_panel.py")
        batch = _method(tree, "DmPanel", "append_dm_batch")
        self.assertIn("_dm_emoticon_image", _attr_names(batch), "渲染未检查开关")
        strip = _method(tree, "DmPanel", "_strip_dm_emoticon_images")
        self.assertIn("insertText", _called_attrs(strip), "关闭开关时应把图片还原为文字")

    def test_switch_refills_existing_rows(self):
        """开关要双向作用于已有弹幕：关闭还原文字、重新开启换回图片。"""
        setter = _method(_tree("qt_dm_panel.py"), "DmPanel",
                         "set_emoticon_image_enabled")
        self.assertIn("_strip_dm_emoticon_images", _called_attrs(setter),
                      "关闭时未还原已显示的图片")
        self.assertIn("_refill_dm_emoticon_images", _called_attrs(setter),
                      "重新开启时未把已有触发词换回图片")
