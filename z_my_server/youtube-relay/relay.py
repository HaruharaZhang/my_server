"""实时流式转发：边收字节边通过 Broadcaster 吐给客户端，同时 tee 落盘。

- passthrough: 已经是单路 h264/aac 合并流，httpx 直接转发，零转码。
- mux_copy: 分离的 avc1 视频轨 + mp4a 音频轨，ffmpeg 只封装不转码（-c copy）。
- mux_transcode / single_transcode: 源轨不是 h264/aac，ffmpeg 重新编码
  （只发生在首次拉取这一次性成本上，不影响后续走缓存的播放）。

全部转发正常结束后，对已经落地的 .part 文件做一次纯本地 faststart 重新封装，
产出的正式缓存文件才支持 Range 随意拖动；任何环节失败都删除 .part，不留半成品。
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx
import runtime_status

_MAX_FETCH_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 1.0


async def _stream_with_retry(url, headers, proxy, on_chunk, chunk_size, log, label,
                             on_proxy_bytes=None):
    """按 chunk 拉流，网络层瞬时失败（如 CDN 中途关闭连接、读超时）时用已收字节数
    发 Range 续传重试，而不是从头重来——避免已经发给订阅者/写入文件的字节和续传的
    字节错位或重复。HTTP 4xx/5xx（resp.raise_for_status()）不在重试范围内，因为那
    通常意味着请求本身有问题，重试也不会恢复。
    """
    headers = dict(headers or {})
    bytes_received = 0
    for attempt in range(1, _MAX_FETCH_RETRIES + 2):
        req_headers = dict(headers)
        if bytes_received:
            req_headers["Range"] = f"bytes={bytes_received}-"
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(30.0, read=None), follow_redirects=True) as client:
                async with client.stream("GET", url, headers=req_headers) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        if on_proxy_bytes:
                            on_proxy_bytes(len(chunk))
                        await on_chunk(chunk)
                        bytes_received += len(chunk)
            return bytes_received
        except httpx.TransportError as exc:
            if attempt > _MAX_FETCH_RETRIES:
                raise
            log.warning("%s 取流中断（第 %d 次重试，已收 %d 字节）：%s，%.0fs 后续传",
                        label, attempt, bytes_received, exc, _RETRY_BACKOFF_SECONDS)
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)


async def _relay_passthrough(plan, proxy, part_path, broadcaster, chunk_size, log,
                             on_first_chunk, on_proxy_bytes):
    with open(part_path, "wb") as fh:
        async def on_chunk(chunk):
            await on_first_chunk()
            fh.write(chunk)
            await broadcaster.publish(chunk)

        return await _stream_with_retry(
            plan["video_url"], plan.get("headers"), proxy, on_chunk, chunk_size, log,
            "passthrough", on_proxy_bytes)


async def _fetch_into_fifo(url, headers, proxy, fifo_path, log, label, on_proxy_bytes):
    """把网络取流完全交给 httpx（已验证能可靠地经 mihomo 代理拿到 googlevideo 字节），
    ffmpeg 只读本地命名管道，不自己碰网络/代理——规避 ffmpeg 自带 HTTPS 代理隧道
    在这套环境里偶发 403（很可能是与解析阶段不同的出口路径/TLS 指纹导致）的问题。
    """
    fd = await asyncio.to_thread(os.open, fifo_path, os.O_WRONLY)
    try:
        async def on_chunk(chunk):
            await asyncio.to_thread(os.write, fd, chunk)

        return await _stream_with_retry(
            url, headers, proxy, on_chunk, 65536, log, label, on_proxy_bytes)
    finally:
        await asyncio.to_thread(os.close, fd)


async def _relay_ffmpeg(plan, proxy, part_path, broadcaster, chunk_size, log,
                        on_first_chunk, on_proxy_bytes):
    with tempfile.TemporaryDirectory(prefix="dyyjs-yt-") as tmpdir:
        video_fifo = Path(tmpdir) / "video.fifo"
        os.mkfifo(video_fifo)
        fetch_tasks = [asyncio.create_task(
            _fetch_into_fifo(plan["video_url"], plan.get("headers"), proxy, str(video_fifo),
                             log, "video", on_proxy_bytes))]

        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
        if plan["mode"] in ("mux_copy", "mux_transcode"):
            audio_fifo = Path(tmpdir) / "audio.fifo"
            os.mkfifo(audio_fifo)
            fetch_tasks.append(asyncio.create_task(
                _fetch_into_fifo(plan["audio_url"], plan.get("headers"), proxy, str(audio_fifo),
                                 log, "audio", on_proxy_bytes)))
            cmd += ["-i", str(video_fifo), "-i", str(audio_fifo), "-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-i", str(video_fifo)]
        if plan["mode"] == "mux_copy":
            cmd += ["-c", "copy"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
        cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            try:
                with open(part_path, "wb") as fh:
                    while True:
                        chunk = await proc.stdout.read(chunk_size)
                        if not chunk:
                            break
                        await on_first_chunk()
                        fh.write(chunk)
                        await broadcaster.publish(chunk)
                stderr = await proc.stderr.read()
                returncode = await proc.wait()
            finally:
                for task in fetch_tasks:
                    if not task.done():
                        task.cancel()
                fetch_errors = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        finally:
            # 正常路径已经 proc.wait() 过，returncode 非 None；只有取消/异常提前跳出
            # 这条 finally 才会看到它还在跑——这时必须显式杀掉，否则每次因"无人观看"
            # 取消都会留一个孤儿 ffmpeg 进程。
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        for err in fetch_errors:
            if isinstance(err, Exception) and not isinstance(err, asyncio.CancelledError):
                raise RuntimeError(f"取流请求失败: {err}") from err
        if returncode != 0:
            raise RuntimeError(f"ffmpeg 取流失败 (rc={returncode}): {stderr.decode('utf-8', 'ignore')[-500:]}")
        return sum(result for result in fetch_errors if isinstance(result, int))


async def _faststart_remux(part_path, final_path, log):
    """纯本地重新封装：不重新编码，只重排 moov box，方便 Range 随意拖动。"""
    tmp_path = final_path.with_suffix(".mp4.tmp")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
           "-i", str(part_path), "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not tmp_path.exists():
        log.warning("faststart 重新封装失败，直接用原始文件当缓存: %s",
                    stderr.decode("utf-8", "ignore")[-300:])
        os.replace(part_path, final_path)
        return
    os.replace(tmp_path, final_path)
    part_path.unlink(missing_ok=True)


def _prune_cache_to_latest(cache_dir, final_path, max_cached_videos, log):
    """新视频缓存完成后，删除旧视频和残留临时文件。"""
    keep_count = max(1, int(max_cached_videos))
    other_files = [path for path in cache_dir.glob("*.mp4") if path != final_path]
    other_files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    keep = {final_path, *other_files[:keep_count - 1]}
    mp4_files = [final_path, *other_files]
    for path in mp4_files:
        if path not in keep:
            path.unlink(missing_ok=True)
            log.info("清理旧视频缓存: %s", path)

    for path in cache_dir.glob("*.mp4.part"):
        if path != final_path.with_suffix(".mp4.part"):
            path.unlink(missing_ok=True)
            log.info("清理残留临时缓存: %s", path)


async def run_relay(
    video_id,
    broadcaster,
    plan,
    cfg,
    registry,
    admission,
    on_success,
    on_pull_started,
    log,
    cache_suffix="",
    stream_key=None,
    quality_mode="compat",
):
    cache_dir = Path(cfg["paths"]["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    part_path = cache_dir / f"{video_id}{cache_suffix}.mp4.part"
    final_path = cache_dir / f"{video_id}{cache_suffix}.mp4"
    registry_key = stream_key or video_id
    proxy = cfg["network"]["proxy"]
    chunk_size = cfg["limits"]["chunk_size"]
    started = asyncio.get_running_loop().time()
    pull_recorded = False
    proxy_bytes = 0
    proxy_bytes_committed = False

    def count_proxy_bytes(byte_count):
        nonlocal proxy_bytes
        proxy_bytes += byte_count

    def commit_proxy_bytes():
        nonlocal proxy_bytes_committed
        if not proxy_bytes_committed:
            runtime_status.add_proxy_bytes(cfg, proxy_bytes)
            proxy_bytes_committed = True

    async def record_first_chunk():
        nonlocal pull_recorded
        if pull_recorded:
            return
        pull_recorded = True
        await on_pull_started(video_id, quality_mode, plan)

    try:
        runtime_status.write(cfg, "streaming", quality_mode)
        if plan["mode"] == "passthrough":
            await _relay_passthrough(
                plan, proxy, part_path, broadcaster, chunk_size, log, record_first_chunk,
                count_proxy_bytes)
        else:
            await _relay_ffmpeg(
                plan, proxy, part_path, broadcaster, chunk_size, log, record_first_chunk,
                count_proxy_bytes)
        if not part_path.exists() or part_path.stat().st_size == 0:
            raise RuntimeError("取流结束但没有产生任何字节")
        runtime_status.write(cfg, "finalizing", quality_mode, proxy_bytes)
        await _faststart_remux(part_path, final_path, log)
        _prune_cache_to_latest(cache_dir, final_path, cfg["limits"]["max_cached_videos"], log)
        await broadcaster.finish()
        log.info("视频 %s 拉取完成并已缓存: %s", video_id, final_path)
        await on_success(video_id, plan, final_path)
        elapsed = max(1, asyncio.get_running_loop().time() - started)
        commit_proxy_bytes()
        runtime_status.write(cfg, "idle", quality_mode, proxy_bytes, proxy_bytes / elapsed)
    except asyncio.CancelledError:
        # 无人观看超时被看门狗取消（见 main.py _watch_idle）：和异常路径一样清场，
        # 但要 re-raise，否则这个 task 对外看起来就是"正常结束"而不是"被取消"。
        log.info("视频 %s 拉取被取消（无人观看超时）", video_id)
        part_path.unlink(missing_ok=True)
        await broadcaster.finish(error="播放已取消（长时间无人观看）")
        commit_proxy_bytes()
        runtime_status.write(cfg, "idle", quality_mode)
        raise
    except Exception as exc:  # noqa: BLE001 - 必须兜住任何取流异常，通知所有订阅者并清理半成品
        log.exception("视频 %s 转发失败", video_id)
        part_path.unlink(missing_ok=True)
        await broadcaster.finish(error=str(exc))
        commit_proxy_bytes()
        runtime_status.write(cfg, "error", quality_mode, error_category=type(exc).__name__)
    finally:
        await admission.release()
        await registry.remove(registry_key)
