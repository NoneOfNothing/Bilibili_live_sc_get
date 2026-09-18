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


def _method(tree: ast.Module, class_name: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(_class_node(tree, class_name)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
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
