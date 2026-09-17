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
