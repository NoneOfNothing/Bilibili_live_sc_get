#!/usr/bin/env python3
"""入口脚本：python main.py <房间号> [房间号2 ...]"""

import sys

from blive_sc_get.cli import main

if __name__ == "__main__":
    sys.exit(main())
