#!/usr/bin/env python3
"""每日定时任务：缓存兜底清理，只保留最新视频。"""

import logging
from pathlib import Path

from catalog import prune_cache_to_latest, prune_stale_cache
from config import load_config


def main():
    cfg = load_config()
    log_dir = Path(cfg["paths"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "cleanup.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    log = logging.getLogger("dyyjs-youtube-cleanup")
    prune_stale_cache(cfg["paths"]["cache_dir"], cfg["limits"]["cache_retention_days"], log)
    prune_cache_to_latest(cfg["paths"]["cache_dir"], cfg["limits"]["max_cached_videos"], log)


if __name__ == "__main__":
    main()
