"""项目根目录的应用级配置（``config.json``）。

与界面偏好（``gui_rooms.json`` 的 ui 段）分离：这里放跨模式的**应用行为开关**。

安全约定：程序默认是「只读」的——发送弹幕、发评论等向 B 站提交数据的
**写操作一律默认关闭**，必须在 ``config.json`` 中显式开启才会启用。
字段缺失、文件损坏或类型非法时一律按「关闭」处理（fail-safe）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.json"
CONFIG_FILE_PATH = Path(__file__).resolve().parent.parent / CONFIG_FILE_NAME


@dataclass(frozen=True)
class AppConfig:
    """应用级配置。默认值即「只读」：一切写操作关闭。"""

    allow_write_operations: bool = False
    """写操作总开关（发送弹幕等）。默认 False，需在 config.json 中显式开启。"""


def _as_bool(value: Any, default: bool = False) -> bool:
    """严格取布尔值：只有真正的 JSON 布尔才认，字符串 \"false\"/数字一律回退默认。

    避免误把 "false" 这类字符串当真值而意外开启写操作。
    """
    return value if isinstance(value, bool) else default


def load_app_config(path: Optional[Union[str, Path]] = None) -> AppConfig:
    """读取应用配置；文件缺失/损坏/字段非法时回退默认（写操作关闭）。"""
    config_path = Path(path) if path is not None else CONFIG_FILE_PATH
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("未找到应用配置 %s，使用默认（写操作关闭）", config_path)
        return AppConfig()
    except (OSError, ValueError) as exc:
        logger.warning("读取应用配置失败（%s），使用默认（写操作关闭）: %s",
                       config_path, exc)
        return AppConfig()
    raw: Dict[str, Any] = data if isinstance(data, dict) else {}
    if not isinstance(data, dict):
        logger.warning("应用配置格式异常（应为 JSON 对象），使用默认（写操作关闭）")
    return AppConfig(
        allow_write_operations=_as_bool(raw.get("allow_write_operations"), False),
    )
