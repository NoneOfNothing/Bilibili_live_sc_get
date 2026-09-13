"""项目根目录的应用级配置（``config.json``）。

与界面偏好（``gui_rooms.json`` 的 ui 段）分离：这里放跨模式的**应用行为开关**
（写操作总开关、表情悬浮提示显示哪些字段等）。

安全约定：程序默认是「只读」的——发送弹幕、发评论等向 B 站提交数据的
**写操作一律默认关闭**，必须在 ``config.json`` 中显式开启才会启用。
字段缺失、文件损坏或类型非法时一律按「关闭」处理（fail-safe）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.json"
CONFIG_FILE_PATH = Path(__file__).resolve().parent.parent / CONFIG_FILE_NAME

EMOTICON_TOOLTIP_FIELDS = ("text", "unique", "id")
"""悬浮提示可显示的表情字段（元组顺序即拼接顺序）：

- ``text``：触发词（如 ``[百岁山]``）
- ``unique``：表情唯一标识 ``emoticon_unique``（如 ``room_9527_109824``）
- ``id``：数字 ``emoticon_id``
"""

DEFAULT_EMOTICON_TOOLTIP: Tuple[str, ...] = ("text",)
"""悬浮提示的默认字段：**仅触发词**（可在 config.json 的 emoticon_tooltip 段增删）。"""

DEFAULT_CONFIG_TEMPLATE = """{
  "_说明": "应用级配置（首次运行自动生成，可随时删除，下次运行会按需重建）。allow_write_operations 是写操作总开关（发送弹幕等会向 B 站提交数据的操作），出于安全考虑默认关闭；确认了解风险后改为 true 才会启用。emoticon_tooltip 控制鼠标悬浮表情时提示哪些字段：text=触发词、unique=表情唯一标识、id=数字 id，默认仅 text，写 [] 或全部 false 表示不显示提示。修改后需重启程序生效。字段缺失/文件损坏/类型非法一律按默认值处理。",
  "allow_write_operations": false,
  "emoticon_tooltip": {
    "text": true,
    "unique": false,
    "id": false
  }
}
"""
"""首次运行自动生成的默认 ``config.json`` 模板（带字段说明）。"""


@dataclass(frozen=True)
class AppConfig:
    """应用级配置。默认值即「只读」：一切写操作关闭。"""

    allow_write_operations: bool = False
    """写操作总开关（发送弹幕等）。默认 False，需在 config.json 中显式开启。"""

    emoticon_tooltip: Tuple[str, ...] = DEFAULT_EMOTICON_TOOLTIP
    """鼠标悬浮在表情上时提示哪些字段（``EMOTICON_TOOLTIP_FIELDS`` 的子集）。

    默认仅 ``text``（触发词）；给空元组表示不显示悬浮提示。
    """


def _as_bool(value: Any, default: bool = False) -> bool:
    """严格取布尔值：只有真正的 JSON 布尔才认，字符串 \"false\"/数字一律回退默认。

    避免误把 "false" 这类字符串当真值而意外开启写操作。
    """
    return value if isinstance(value, bool) else default


def _parse_tooltip_fields(value: Any) -> Tuple[str, ...]:
    """解析 ``emoticon_tooltip``（纯函数）。

    支持两种写法，直接编辑 config.json 即可：

    - 列表：``["text", "unique", "id"]``
    - 开关对象：``{"text": true, "unique": false, "id": false}``（只认 JSON 布尔）

    - 字段缺失、类型非法（含字符串 "true" 这类非布尔值）→ 回退默认（仅触发词）；
    - **显式**给空列表或全 false 对象 → 返回空元组（即不显示悬浮提示）；
    - 名单里全是拼错的字段名 → 视为写错，回退默认。
    """
    if isinstance(value, list):
        entries = [str(item).strip().lower() for item in value if isinstance(item, str)]
    elif isinstance(value, dict):
        # 只认真正的 JSON 布尔：出现 "true"/1 这类写法视为写错，整体回退默认
        if not all(isinstance(on, bool) for on in value.values()):
            return DEFAULT_EMOTICON_TOOLTIP
        entries = [str(name).strip().lower() for name, on in value.items() if on]
    else:
        return DEFAULT_EMOTICON_TOOLTIP
    if not entries:
        return ()
    picked = set(entries)
    matched = tuple(name for name in EMOTICON_TOOLTIP_FIELDS if name in picked)
    return matched or DEFAULT_EMOTICON_TOOLTIP


def _write_default_config(config_path: Path) -> None:
    """首次运行（或文件不存在）时生成带字段说明的默认配置文件。

    生成失败（如目录只读）仅记录日志，不影响本次运行使用默认值。
    """
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
        logger.info("已生成默认应用配置 %s（写操作默认关闭）", config_path)
    except OSError as exc:
        logger.debug("生成默认应用配置失败（%s）: %s", config_path, exc)


def load_app_config(path: Optional[Union[str, Path]] = None) -> AppConfig:
    """读取应用配置；文件损坏/字段非法时回退默认（写操作关闭）。

    文件不存在时视为首次运行：自动生成带字段说明的默认配置文件
    （生成失败不影响本次运行）。
    """
    config_path = Path(path) if path is not None else CONFIG_FILE_PATH
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("未找到应用配置 %s，生成默认配置（写操作关闭）", config_path)
        _write_default_config(config_path)
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
        emoticon_tooltip=_parse_tooltip_fields(raw.get("emoticon_tooltip")),
    )
