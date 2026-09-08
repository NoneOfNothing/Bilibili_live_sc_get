#!/usr/bin/env python3
"""GUI 入口：python gui.py（等价于 python main.py --gui）"""

import sys

from blive_sc_get.cli import main

if __name__ == "__main__":
    sys.exit(main(["--gui"]))
