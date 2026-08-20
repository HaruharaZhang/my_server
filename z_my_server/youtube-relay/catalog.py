"""播放目录 catalog.json：记录视频元数据、转发用户和服务器拉流任务数。

封面图下载前校验是 YouTube 官方图床域名，Pillow 重编码消毒后落地存本地，
不直接热链任何外部图片，做法与 news-pipeline images.py 一致。
"""

import asyncio
import hashlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from PIL import Image

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


async def _fetch_thumbnail(url, proxy, image_dir, video_id, max_bytes, max_width, allowed_hosts, log):
    if not url or not is_allowed_thumbnail(url, allowed_hosts):
        return ""
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=15.0, follow_redirects=True) as client:
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
        name = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:16] + ".jpg"
        Path(image_dir).mkdir(parents=True, exist_ok=True)
        (Path(image_dir) / name).write_bytes(buf.getvalue())
        return f"/youtube/history/images/{name}"
    except Exception as exc:
        log.info("封面处理跳过（%s）: %s", str(exc)[:120], url[:120])
        return ""


async def record_play(cfg, video_id, title, thumbnail_url, duration_seconds,
                      quality_mode, client_key, log, monotonic=None):
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
        session_key = (client_key, video_id, quality_mode)
        if current - _sessions.get(session_key, -SESSION_SECONDS) < SESSION_SECONDS:
            return False, False
        _sessions[session_key] = current

        catalog_path = cfg["paths"]["catalog_path"]
        data = load_catalog(catalog_path)
        now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
        entry = data.get(video_id)
        is_new = entry is None
        if entry is None:
            image = await _fetch_thumbnail(
                thumbnail_url, cfg["network"]["proxy"], cfg["paths"]["image_dir"], video_id,
                cfg["limits"]["image_max_bytes"], cfg["limits"]["image_max_width"],
                cfg["youtube"]["allowed_thumbnail_hosts"], log,
            )
            entry = {
                "video_id": video_id,
                "title": title or "标题待确认",
                "image": image,
                "duration_seconds": duration_seconds or None,
                "last_quality": quality_mode,
                "first_played_at": now,
                "last_played_at": now,
                "play_count": 1,
            }
        else:
            # record_pull 创建的条目 first_played_at 是 None，首次真实播放时回填
            if not entry.get("first_played_at"):
                entry["first_played_at"] = now
            entry["last_played_at"] = now
            entry["play_count"] = int(entry.get("play_count", 0)) + 1
            if title:
                entry["title"] = title
            if duration_seconds:
                entry["duration_seconds"] = duration_seconds
            entry["last_quality"] = quality_mode
            if not entry.get("image") and thumbnail_url:
                entry["image"] = await _fetch_thumbnail(
                    thumbnail_url, cfg["network"]["proxy"], cfg["paths"]["image_dir"], video_id,
                    cfg["limits"]["image_max_bytes"], cfg["limits"]["image_max_width"],
                    cfg["youtube"]["allowed_thumbnail_hosts"], log,
                )
        data[video_id] = entry
        _save(catalog_path, data)
        return True, is_new


async def record_pull(cfg, video_id, title, thumbnail_url, duration_seconds,
                      quality_mode, log):
    """Record one server-to-YouTube media pull task.

    This counter is deliberately independent from the ten-minute client session
    deduplication used by ``record_play``.  Callers must invoke it once at the
    task boundary, after upstream media has successfully started.
    """
    async with _catalog_lock:
        catalog_path = cfg["paths"]["catalog_path"]
        data = load_catalog(catalog_path)
        now = datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M")
        entry = data.get(video_id)
        is_new = entry is None
        if entry is None:
            image = await _fetch_thumbnail(
                thumbnail_url, cfg["network"]["proxy"], cfg["paths"]["image_dir"], video_id,
                cfg["limits"]["image_max_bytes"], cfg["limits"]["image_max_width"],
                cfg["youtube"]["allowed_thumbnail_hosts"], log,
            )
            entry = {
                "video_id": video_id,
                "title": title or "标题待确认",
                "image": image,
                "duration_seconds": duration_seconds or None,
                "last_quality": quality_mode,
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
            entry["last_quality"] = quality_mode
            if not entry.get("image") and thumbnail_url:
                entry["image"] = await _fetch_thumbnail(
                    thumbnail_url, cfg["network"]["proxy"], cfg["paths"]["image_dir"], video_id,
                    cfg["limits"]["image_max_bytes"], cfg["limits"]["image_max_width"],
                    cfg["youtube"]["allowed_thumbnail_hosts"], log,
                )
        data[video_id] = entry
        _save(catalog_path, data)
        return True, is_new


async def enrich_metadata(cfg, video_id, title, thumbnail_url, duration_seconds, log):
    """Fill missing facts without changing trusted playback history or counts."""
    async with _catalog_lock:
        data = load_catalog(cfg["paths"]["catalog_path"])
        entry = data.get(video_id)
        if not isinstance(entry, dict):
            return False
        changed = False
        if title and not entry.get("title"):
            entry["title"] = title
            changed = True
        if duration_seconds and not entry.get("duration_seconds"):
            entry["duration_seconds"] = duration_seconds
            changed = True
        if thumbnail_url and not entry.get("image"):
            image = await _fetch_thumbnail(
                thumbnail_url, cfg["network"]["proxy"], cfg["paths"]["image_dir"], video_id,
                cfg["limits"]["image_max_bytes"], cfg["limits"]["image_max_width"],
                cfg["youtube"]["allowed_thumbnail_hosts"], log,
            )
            if image:
                entry["image"] = image
                changed = True
        if changed:
            data[video_id] = entry
            _save(cfg["paths"]["catalog_path"], data)
        return changed


def prune_stale_cache(cache_dir, retention_days, log):
    """删除超过 retention_days 天未被访问（atime）的缓存 mp4；不动 catalog.json 历史记录。"""
    cutoff = datetime.now(BEIJING) - timedelta(days=retention_days)
    root = Path(cache_dir)
    if not root.exists():
        return
    for path in root.glob("*.mp4"):
        accessed = datetime.fromtimestamp(path.stat().st_atime, tz=BEIJING)
        if accessed < cutoff:
            path.unlink()
            log.info("清理过期缓存: %s", path)


def prune_cache_to_latest(cache_dir, max_cached_videos, log):
    """只保留最近写入的 max_cached_videos 个完整 mp4，并清理残留 part 文件。"""
    root = Path(cache_dir)
    if not root.exists():
        return

    keep_count = max(0, int(max_cached_videos))
    mp4_files = sorted(root.glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
    keep = set(mp4_files[:keep_count])
    for path in mp4_files[keep_count:]:
        path.unlink(missing_ok=True)
        log.info("清理旧视频缓存: %s", path)

    for path in root.glob("*.mp4.part"):
        path.unlink(missing_ok=True)
        log.info("清理残留临时缓存: %s", path)
