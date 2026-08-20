"""播放目录 catalog.json：记录视频元数据、转发用户和服务器拉流会话数。

封面图下载前校验是 B 站官方图床域名（*.hdslb.com），Pillow 重编码消毒后落地存本地，
不直接热链任何外部图片，做法与 youtube-relay / news-pipeline 一致。下载直连不走代理。
"""

import asyncio
import hashlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from resolver import direct_client
from validate import is_allowed_thumbnail

BEIJING = timezone(timedelta(hours=8))
SESSION_SECONDS = 600
_catalog_lock = asyncio.Lock()
_sessions = {}


def load_catalog(catalog_path):
    path = Path(catalog_path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(catalog_path, data):
    path = Path(catalog_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


async def _fetch_thumbnail(url, image_dir, bvid, max_bytes, max_width, allowed_host_suffix, log):
    if not url or not is_allowed_thumbnail(url, allowed_host_suffix):
        return ""
    try:
        async with direct_client(15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if not content_type.lower().startswith("image/"):
                raise ValueError(f"Content-Type 不是图片: {content_type}")
            data = resp.content
            if len(data) > max_bytes:
                raise ValueError("封面超过大小上限")
        Image.open(io.BytesIO(data)).verify()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        if img.width > max_width:
            img = img.resize((max_width, max(1, round(img.height * max_width / img.width))))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        name = hashlib.sha256(bvid.encode("utf-8")).hexdigest()[:16] + ".jpg"
        Path(image_dir).mkdir(parents=True, exist_ok=True)
        (Path(image_dir) / name).write_bytes(buf.getvalue())
        return f"/bilibili/history/images/{name}"
    except Exception as exc:
        log.info("封面处理跳过（%s）: %s", str(exc)[:120], url[:120])
        return ""


async def _fetch_thumbnail_from_cfg(cfg, thumbnail_url, bvid, log):
    return await _fetch_thumbnail(
        thumbnail_url, cfg["paths"]["image_dir"], bvid,
        cfg["limits"]["image_max_bytes"], cfg["limits"]["image_max_width"],
        cfg["bilibili"]["allowed_thumbnail_host_suffix"], log,
    )


async def record_play(cfg, bvid, title, thumbnail_url, duration_seconds,
                      client_key, log, monotonic=None):
    """Record one successfully-started, ten-minute playback session.

    The client key exists only in this process.  It is deliberately never copied
    into catalog.json or the public page.
    """
    clock = monotonic or asyncio.get_running_loop().time
    async with _catalog_lock:
        current = clock()
        expired = [key for key, seen_at in _sessions.items()
                   if current - seen_at >= SESSION_SECONDS]
        for key in expired:
            _sessions.pop(key, None)
        session_key = (client_key, bvid)
        if current - _sessions.get(session_key, -SESSION_SECONDS) < SESSION_SECONDS:
            return False, False
        _sessions[session_key] = current

        catalog_path = cfg["paths"]["catalog_path"]
        data = load_catalog(catalog_path)
        now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
        entry = data.get(bvid)
        is_new = entry is None
        if entry is None:
            image = await _fetch_thumbnail_from_cfg(cfg, thumbnail_url, bvid, log)
            entry = {
                "bvid": bvid,
                "title": title or "标题待确认",
                "image": image,
                "duration_seconds": duration_seconds or None,
                "first_played_at": now,
                "last_played_at": now,
                "play_count": 1,
            }
        else:
            entry["last_played_at"] = now
            entry["play_count"] = int(entry.get("play_count", 0)) + 1
            if title:
                entry["title"] = title
            if duration_seconds:
                entry["duration_seconds"] = duration_seconds
            if not entry.get("image") and thumbnail_url:
                entry["image"] = await _fetch_thumbnail_from_cfg(cfg, thumbnail_url, bvid, log)
        data[bvid] = entry
        _save(catalog_path, data)
        return True, is_new


async def record_pull(cfg, bvid, title, thumbnail_url, duration_seconds, log):
    """Record one server-to-Bilibili resolve-and-pull session.

    This counter is deliberately independent from the ten-minute client session
    deduplication used by ``record_play``.  Callers must invoke it once per
    resolved plan, after upstream media has successfully started.
    """
    async with _catalog_lock:
        catalog_path = cfg["paths"]["catalog_path"]
        data = load_catalog(catalog_path)
        now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
        entry = data.get(bvid)
        is_new = entry is None
        if entry is None:
            image = await _fetch_thumbnail_from_cfg(cfg, thumbnail_url, bvid, log)
            entry = {
                "bvid": bvid,
                "title": title or "标题待确认",
                "image": image,
                "duration_seconds": duration_seconds or None,
                "first_played_at": None,
                "last_played_at": None,
                "play_count": 0,
                "pull_count": 1,
                "pull_count_started_at": now,
            }
        else:
            entry["pull_count"] = int(entry.get("pull_count", 0)) + 1
            entry.setdefault("pull_count_started_at", now)
            if title:
                entry["title"] = title
            if duration_seconds:
                entry["duration_seconds"] = duration_seconds
            if not entry.get("image") and thumbnail_url:
                entry["image"] = await _fetch_thumbnail_from_cfg(cfg, thumbnail_url, bvid, log)
        data[bvid] = entry
        _save(catalog_path, data)
        return True, is_new
