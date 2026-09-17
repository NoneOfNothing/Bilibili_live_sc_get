#!/usr/bin/env python3
"""Qt 版 GUI 入口：python qt_gui.py（基于 PySide6，与 Tk 版 gui.py 并存）。"""

import sys

from blive_sc_get.qt_app import run_gui_qt

if __name__ == "__main__":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    sys.exit(run_gui_qt())