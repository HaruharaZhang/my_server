"""yt-dlp 库接口封装：只做解析（--skip-download 等价），不下载。

统一走 yt_dlp.YoutubeDL 库调用而不是 shell 出去跑命令行，
从机制上排除 shell 元字符注入的可能。
"""

import importlib.metadata
import os
from pathlib import Path

import yt_dlp


class ResolveError(Exception):
    pass


def validate_environment(youtube_config):
    runtime_path = Path(youtube_config["node_runtime_path"])
    if not runtime_path.is_file() or not os.access(runtime_path, os.X_OK):
        raise RuntimeError("YouTube Node runtime is unavailable")
    try:
        importlib.metadata.version("yt-dlp-ejs")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("yt-dlp-ejs is unavailable") from exc


def resolve(video_url, proxy, format_selector, youtube_config):
    ydl_opts = {
        "proxy": proxy,
        "format": format_selector,
        "extractor_args": {
            "youtube": {"player_client": [youtube_config["player_client"]]},
        },
        "js_runtimes": {
            "node": {"path": youtube_config["node_runtime_path"]},
        },
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": youtube_config["socket_timeout_seconds"],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ResolveError(str(exc)) from exc
    if info is None:
        raise ResolveError("yt-dlp 未返回任何信息")
    return build_plan(info)


def _headers(fmt, info):
    return fmt.get("http_headers") or info.get("http_headers") or {}


def build_plan(info):
    """把 yt-dlp 的 info dict 归约成一个明确的取流方案。"""
    title = str(info.get("title") or "").strip() or "untitled"
    duration = info.get("duration")
    thumbnail = str(info.get("thumbnail") or "").strip()

    requested = info.get("requested_formats")
    if requested and len(requested) >= 2:
        video_fmt, audio_fmt = requested[0], requested[1]
        vcodec = str(video_fmt.get("vcodec") or "")
        acodec = str(audio_fmt.get("acodec") or "")
        copy_ok = vcodec.startswith("avc1") and acodec.startswith("mp4a")
        return {
            "mode": "mux_copy" if copy_ok else "mux_transcode",
            "title": title,
            "duration": duration,
            "thumbnail": thumbnail,
            "video_url": video_fmt.get("url"),
            "audio_url": audio_fmt.get("url"),
            "headers": _headers(video_fmt, info),
        }

    vcodec = str(info.get("vcodec") or "")
    acodec = str(info.get("acodec") or "")
    url = info.get("url")
    if not url:
        raise ResolveError("yt-dlp 未返回可用的直链")
    copy_ok = vcodec.startswith("avc1") and acodec.startswith("mp4a")
    return {
        "mode": "passthrough" if copy_ok else "single_transcode",
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "video_url": url,
        "audio_url": None,
        "headers": _headers(info, info),
    }
