"""加载 config.json，供各模块共用同一份配置对象。"""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
