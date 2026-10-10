"""程序图标（ROADMAP 111）的离线测试：资产存在性、容错、两版接线。

Tk 的 ``apply_app_icon_tk`` 用假窗口直调真实函数；Qt 侧在 offscreen 下验证图标能加载，
两版的接线用 AST 静态断言（启动入口不该在本测试里真的跑起来）。
"""

import ast
import os
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from blive_sc_get import gui_app

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "blive_sc_get"


def _tree(module: str) -> ast.Module:
    return ast.parse((PKG / module).read_text(encoding="utf-8"))


def _method(module: str, cls: str, name: str) -> ast.FunctionDef:
    klass = next((n for n in ast.walk(_tree(module))
                  if isinstance(n, ast.ClassDef) and n.name == cls), None)
    node = next((n for n in klass.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise AssertionError(f"{module} 的 {cls} 缺少 {name}")
    return node


def _func(module: str, name: str) -> ast.FunctionDef:
    node = next((n for n in ast.parse((PKG / module).read_text(encoding="utf-8")).body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise AssertionError(f"{module} 缺少顶层函数 {name}")
    return node


class IconAssetTests(unittest.TestCase):
    """assets/app.ico 必须真实存在且是合法的 ICO（用户生成的资产）。"""

    def test_icon_exists_and_is_valid(self):
        self.assertTrue(gui_app.APP_ICON_PATH.is_file(),
                        f"程序图标缺失：{gui_app.APP_ICON_PATH}")
        self.assertEqual(gui_app.APP_ICON_PATH.suffix, ".ico")
        header = gui_app.APP_ICON_PATH.read_bytes()[:6]
        _reserved, kind, count = struct.unpack("<HHH", header)
        self.assertEqual(kind, 1, "不是 ICO 文件（kind 应为 1）")
        self.assertGreaterEqual(count, 1, "ICO 内没有任何尺寸")

    def test_path_resolves_to_repo_root(self):
        """路径按仓库根解析：blive_sc_get 的上一级下的 assets/app.ico。"""
        self.assertEqual(gui_app.APP_ICON_PATH, ROOT / "assets" / "app.ico")


class ApplyAppIconTkTests(unittest.TestCase):
    """Tk 图标设置是**容错**的：文件缺失 / TclError 都只返回 False，绝不影响启动。"""

    def test_sets_icon_and_returns_true(self):
        calls = []
        window = SimpleNamespace(iconbitmap=lambda path: calls.append(path))
        self.assertTrue(gui_app.apply_app_icon_tk(window))
        self.assertEqual(calls, [str(gui_app.APP_ICON_PATH)])

    def test_missing_file_is_tolerated(self):
        with mock.patch.object(gui_app, "APP_ICON_PATH",
                               Path("Z:") / "nope" / "app.ico"):
            window = SimpleNamespace(
                iconbitmap=lambda _path: self.fail("文件缺失时不应调用 iconbitmap"))
            self.assertFalse(gui_app.apply_app_icon_tk(window))

    def test_tcl_error_is_tolerated(self):
        def boom(_path):
            raise gui_app.tk.TclError("格式不对的图标")

        self.assertFalse(gui_app.apply_app_icon_tk(
            SimpleNamespace(iconbitmap=boom)))


class IconWiringTests(unittest.TestCase):
    """两版启动入口的接线（AST 静态断言）。"""

    def test_tk_entry_applies_icon(self):
        source = ast.unparse(_func("gui_app.py", "run_gui"))
        self.assertIn("apply_app_icon_tk(root)", source, "Tk 主入口未设置程序图标")

    def test_tk_room_window_applies_icon(self):
        node = _method("tk_room_window.py", "RoomChatWindow", "__init__")
        self.assertIn("apply_app_icon_tk", ast.unparse(node),
                      "Tk 房间独立窗口未设置程序图标（Toplevel 不一定继承 root）")

    def test_qt_sets_application_icon(self):
        source = ast.unparse(_func("qt_app.py", "run_gui_qt"))
        self.assertIn("setWindowIcon", source, "Qt 未设置应用图标")
        self.assertIn("APP_ICON_PATH", source, "Qt 未使用共享的图标路径")
        self.assertIn("isNull", source, "Qt 未处理图标缺失 / 加载失败")


class QtIconLoadTests(unittest.TestCase):
    """offscreen 下验证 assets/app.ico 能被 QIcon 加载并设到 QApplication 上。"""

    def test_qt_loads_icon(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        icon = QIcon(str(gui_app.APP_ICON_PATH))
        self.assertFalse(icon.isNull(), "assets/app.ico 无法被 QIcon 加载")
        app.setWindowIcon(icon)
        self.assertFalse(app.windowIcon().isNull(),
                         "设置后 QApplication.windowIcon() 仍为空")


if __name__ == "__main__":
    unittest.main()
