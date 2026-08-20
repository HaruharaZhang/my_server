#!/usr/bin/env python3
"""One-shot, failure-tolerant metadata completion for existing catalog entries."""

import asyncio
import logging

import catalog
import render
import ytdlp_client
from config import load_config
from validate import canonical_watch_url


async def main():
    cfg = load_config()
    log = logging.getLogger("catalog-enrichment")
    logging.basicConfig(level=logging.INFO)
    entries = catalog.load_catalog(cfg["paths"]["catalog_path"])
    for video_id in list(entries):
        try:
            plan = await asyncio.to_thread(
                ytdlp_client.resolve, canonical_watch_url(video_id), cfg["network"]["proxy"],
                cfg["youtube"]["format_compat"],
            )
            await catalog.enrich_metadata(
                cfg, video_id, plan.get("title", ""), plan.get("thumbnail", ""),
                plan.get("duration"), log,
            )
        except Exception as exc:
            log.warning("条目 %s 元数据补全失败，保留原资料: %s", video_id, str(exc)[:160])
    render.render_and_publish(cfg, catalog.load_catalog(cfg["paths"]["catalog_path"]), log)


if __name__ == "__main__":
    asyncio.run(main())
