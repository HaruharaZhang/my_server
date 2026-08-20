"""Atomic, anonymous runtime snapshot for the local status collector."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _traffic_path(cfg):
    return Path(cfg["paths"]["state_dir"]) / "proxy-traffic.json"


def proxy_total_bytes(cfg):
    try:
        payload = json.loads(_traffic_path(cfg).read_text(encoding="utf-8"))
        return max(0, int(payload.get("total_bytes", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def add_proxy_bytes(cfg, byte_count):
    added = max(0, int(byte_count))
    total = proxy_total_bytes(cfg) + added
    path = _traffic_path(cfg)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"total_bytes": total}), encoding="utf-8")
    os.replace(tmp, path)
    return total


def write(cfg, phase, mode="compat", proxy_bytes=0, proxy_speed_bps=0, viewers=0, error_category=None):
    path = Path(cfg["paths"]["state_dir"]) / "runtime-status.json"
    payload = {
        "phase": phase,
        "mode": "high" if mode == "high" else "compat",
        "proxy_bytes": max(0, int(proxy_bytes)),
        "proxy_speed_bps": max(0, int(proxy_speed_bps)),
        "proxy_total_bytes": proxy_total_bytes(cfg),
        "viewers": max(0, int(viewers)),
        "error_category": error_category,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_epoch": time.time(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
