"""Atomic, anonymous runtime snapshot for the local status collector.

与 youtube-relay 的同名模块结构一致，但 Bilibili 直连不走代理，
字段按 upstream（服务器从 B 站 CDN 收到的媒体字节）命名。
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _traffic_path(cfg):
    return Path(cfg["paths"]["state_dir"]) / "upstream-traffic.json"


def upstream_total_bytes(cfg):
    try:
        payload = json.loads(_traffic_path(cfg).read_text(encoding="utf-8"))
        return max(0, int(payload.get("total_bytes", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def add_upstream_bytes(cfg, byte_count):
    added = max(0, int(byte_count))
    total = upstream_total_bytes(cfg) + added
    path = _traffic_path(cfg)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"total_bytes": total}), encoding="utf-8")
    os.replace(tmp, path)
    return total


def write(cfg, phase, upstream_bytes=0, upstream_speed_bps=0, error_category=None):
    path = Path(cfg["paths"]["state_dir"]) / "runtime-status.json"
    payload = {
        "phase": phase,
        "upstream_bytes": max(0, int(upstream_bytes)),
        "upstream_speed_bps": max(0, int(upstream_speed_bps)),
        "upstream_total_bytes": upstream_total_bytes(cfg),
        "error_category": error_category,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_epoch": time.time(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
