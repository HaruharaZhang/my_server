"""dyyjs.com/youtube — YouTube 播放代理 FastAPI 后端。

单进程运行（uvicorn 单 worker），保证熔断计数器/并发准入/single-flight
这些内存态只有一份，逻辑简单可靠。
"""

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

import catalog
import render
import runtime_status
import ytdlp_client
from broadcast import Admission, StreamRegistry
from config import load_config
from relay import run_relay
from security import TokenGuard
from validate import canonical_watch_url, extract_video_id, parse_query

CFG = load_config()
ytdlp_client.validate_environment(CFG["youtube"])

for key in ("cache_dir", "state_dir", "log_dir"):
    Path(CFG["paths"][key]).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(CFG["paths"]["log_dir"]) / "dyyjs-youtube.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("dyyjs-youtube")

_tokens_raw = os.environ.get("YOUTUBE_RELAY_TOKENS", "")
guard = TokenGuard(
    tokens=[t.strip() for t in _tokens_raw.split(",") if t.strip()],
    killswitch_path=CFG["paths"]["killswitch_path"],
    window_seconds=CFG["limits"]["auth_window_seconds"],
    max_failures=CFG["limits"]["auth_max_failures"],
    log=log,
)
registry = StreamRegistry(CFG["limits"]["subscriber_queue_size"])
admission = Admission(CFG["limits"]["max_concurrent_streams"])
active_relays = {}
active_relays_lock = asyncio.Lock()
passthrough_plan_cache = {}
passthrough_plan_cache_lock = asyncio.Lock()
PASSTHROUGH_PLAN_CACHE_SECONDS = 600
QUALITY_HIGH = "high"
QUALITY_COMPAT = "compat"

app = FastAPI()
runtime_status.write(CFG, "idle")


def _quality_mode(quality):
    return QUALITY_HIGH if quality == QUALITY_HIGH else QUALITY_COMPAT


def _stream_key(video_id, quality_mode):
    return f"{video_id}:{quality_mode}"


def _cache_suffix(quality_mode):
    return ".high" if quality_mode == QUALITY_HIGH else ""


def _format_selector(quality_mode):
    if quality_mode == QUALITY_HIGH:
        return CFG["youtube"]["format_high"]
    return CFG["youtube"]["format_compat"]


async def _record_started(video_id, quality_mode, client_key, plan=None):
    plan = plan or {}
    changed, is_new = await catalog.record_play(
        CFG, video_id, plan.get("title", ""), plan.get("thumbnail", ""),
        plan.get("duration"), quality_mode, client_key, log,
    )
    if changed:
        render.render_and_publish(CFG, catalog.load_catalog(CFG["paths"]["catalog_path"]), log)
    if is_new:
        log.info("catalog 新增条目: %s", video_id)


async def _record_pull_started(video_id, quality_mode, plan=None):
    plan = plan or {}
    changed, is_new = await catalog.record_pull(
        CFG, video_id, plan.get("title", ""), plan.get("thumbnail", ""),
        plan.get("duration"), quality_mode, log,
    )
    if changed:
        render.render_and_publish(CFG, catalog.load_catalog(CFG["paths"]["catalog_path"]), log)
    if is_new:
        log.info("catalog 新增拉流条目: %s", video_id)


async def _on_cache_complete(video_id, plan, final_path):
    return None


async def _untrack_relay(stream_key, relay_task):
    async with active_relays_lock:
        if active_relays.get(stream_key) is relay_task:
            active_relays.pop(stream_key, None)


async def _track_relay(stream_key, relay_task):
    async with active_relays_lock:
        active_relays[stream_key] = relay_task
    relay_task.add_done_callback(
        lambda task: asyncio.create_task(_untrack_relay(stream_key, task))
    )


async def _cancel_other_relays(stream_key):
    async with active_relays_lock:
        targets = [
            (active_stream_key, relay_task)
            for active_stream_key, relay_task in active_relays.items()
            if active_stream_key != stream_key and not relay_task.done()
        ]
    for active_stream_key, relay_task in targets:
        log.info("收到新视频/模式 %s 请求，取消当前旧推流 %s 以释放唯一推流名额", stream_key, active_stream_key)
        relay_task.cancel()
    return len(targets)


async def _try_admit_for_stream(stream_key):
    if await admission.try_acquire():
        return True

    cancelled = await _cancel_other_relays(stream_key)
    if not cancelled:
        return False

    deadline = asyncio.get_running_loop().time() + 3.0
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
        if await admission.try_acquire():
            return True
    return False


async def _watch_idle(video_id, broadcaster, relay_task, idle_cancel_seconds, log):
    """没人看的直播会一直占着唯一的并发拉取名额，导致切视频时永远 503。
    持续 idle_cancel_seconds 秒订阅者数为 0 就取消。新视频请求会主动取消旧流，
    所以这里保留一点播放器重连宽限期，避免媒体探测/重开连接时误杀当前流。
    """
    idle_elapsed = 0.0
    poll_interval = 1.0
    while not relay_task.done():
        await asyncio.sleep(poll_interval)
        if relay_task.done():
            return
        if await broadcaster.subscriber_count() == 0:
            idle_elapsed += poll_interval
            if idle_elapsed >= idle_cancel_seconds:
                log.info("视频 %s 已连续 %.0fs 无人观看，取消拉取以释放并发名额", video_id, idle_elapsed)
                relay_task.cancel()
                return
        else:
            idle_elapsed = 0.0


async def _consume(sid, queue, broadcaster):
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
    finally:
        await broadcaster.unsubscribe(sid)


async def _get_cached_passthrough_plan(cache_key):
    async with passthrough_plan_cache_lock:
        entry = passthrough_plan_cache.get(cache_key)
        if entry is None:
            return None
        expires_at, plan = entry
        if expires_at <= time.monotonic():
            passthrough_plan_cache.pop(cache_key, None)
            return None
        return plan


async def _cache_passthrough_plan(cache_key, plan):
    async with passthrough_plan_cache_lock:
        passthrough_plan_cache[cache_key] = (
            time.monotonic() + PASSTHROUGH_PLAN_CACHE_SECONDS,
            plan,
        )


async def _clear_passthrough_plan(cache_key):
    async with passthrough_plan_cache_lock:
        passthrough_plan_cache.pop(cache_key, None)


async def _direct_passthrough_response(request, plan, quality_mode):
    """Progressive MP4 already supports byte ranges; preserve that for media players."""
    req_headers = dict(plan.get("headers") or {})
    range_header = request.headers.get("range")
    if range_header:
        req_headers["Range"] = range_header

    client = httpx.AsyncClient(
        proxy=CFG["network"]["proxy"],
        timeout=httpx.Timeout(30.0, read=None),
        follow_redirects=True,
    )
    stream = client.stream("GET", plan["video_url"], headers=req_headers)
    try:
        upstream = await stream.__aenter__()
        upstream.raise_for_status()
        if upstream.status_code not in (200, 206):
            raise httpx.HTTPStatusError(
                "上游未返回有效媒体状态", request=upstream.request, response=upstream)
    except Exception:
        await stream.__aexit__(None, None, None)
        await client.aclose()
        raise

    response_headers = {}
    for name in ("content-length", "content-range", "accept-ranges"):
        value = upstream.headers.get(name)
        if value:
            response_headers[name] = value
    response_headers.setdefault("accept-ranges", "bytes")

    started = time.monotonic()
    proxy_bytes = 0
    runtime_status.write(CFG, "streaming", quality_mode)

    async def body():
        nonlocal proxy_bytes
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=CFG["limits"]["chunk_size"]):
                if chunk:
                    proxy_bytes += len(chunk)
                    yield chunk
        finally:
            await stream.__aexit__(None, None, None)
            await client.aclose()
            elapsed = max(1, time.monotonic() - started)
            runtime_status.add_proxy_bytes(CFG, proxy_bytes)
            runtime_status.write(
                CFG, "idle", quality_mode, proxy_bytes, proxy_bytes / elapsed,
            )

    status_code = upstream.status_code if upstream.status_code in (200, 206) else 200
    return StreamingResponse(
        body(),
        status_code=status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )


@app.get("/youtube")
async def youtube_relay(request: Request):
    if guard.killswitch_active():
        return PlainTextResponse("服务器繁忙，请稍后重试", status_code=503)

    raw_query = request.scope["query_string"].decode("utf-8", "ignore")
    token, link, quality = parse_query(raw_query)
    quality_mode = _quality_mode(quality)
    client_ip = request.client.host if request.client else "unknown"

    if not guard.check(token, client_ip):
        if guard.killswitch_active():
            return PlainTextResponse("服务器繁忙，请稍后重试", status_code=503)
        return PlainTextResponse("Forbidden", status_code=403)

    if not link:
        return PlainTextResponse("Bad Request: missing link", status_code=400)

    video_id = extract_video_id(link, set(CFG["youtube"]["allowed_hosts"]))
    if not video_id:
        return PlainTextResponse("Bad Request: unsupported link", status_code=400)

    stream_key = _stream_key(video_id, quality_mode)
    cache_path = Path(CFG["paths"]["cache_dir"]) / f"{video_id}{_cache_suffix(quality_mode)}.mp4"
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        os.utime(cache_path, None)
        await _record_started(video_id, quality_mode, client_ip)
        return FileResponse(cache_path, media_type="video/mp4")

    cached_plan = await _get_cached_passthrough_plan(stream_key)
    if cached_plan is not None:
        try:
            response = await _direct_passthrough_response(request, cached_plan, quality_mode)
            await _record_pull_started(video_id, quality_mode, cached_plan)
            await _record_started(video_id, quality_mode, client_ip, cached_plan)
            return response
        except httpx.HTTPError as exc:
            await _clear_passthrough_plan(stream_key)
            log.warning("视频 %s/%s cached progressive 直通失败，清除缓存后重新解析: %r",
                        video_id, quality_mode, exc)

    broadcaster, is_new = await registry.join_or_create(stream_key)

    if is_new:
        if not await _try_admit_for_stream(stream_key):
            await registry.remove(stream_key)
            return PlainTextResponse("服务器繁忙，请稍后重试", status_code=503)
        try:
            runtime_status.write(CFG, "resolving", quality_mode)
            plan = await asyncio.to_thread(
                ytdlp_client.resolve, canonical_watch_url(video_id),
                CFG["network"]["proxy"], _format_selector(quality_mode), CFG["youtube"],
            )
        except ytdlp_client.ResolveError as exc:
            runtime_status.write(CFG, "error", quality_mode, error_category="resolve_error")
            log.warning("视频 %s/%s 解析失败: %s", video_id, quality_mode, exc)
            await admission.release()
            await registry.remove(stream_key)
            return PlainTextResponse("Bad Gateway: 无法解析该视频", status_code=502)

        duration = plan.get("duration") or 0
        if duration > CFG["limits"]["max_duration_seconds"]:
            log.warning("视频 %s/%s 超过时长上限: %ss", video_id, quality_mode, duration)
            await admission.release()
            await registry.remove(stream_key)
            return PlainTextResponse("Payload Too Large: 视频过长", status_code=413)

        if plan["mode"] == "passthrough":
            await admission.release()
            await registry.remove(stream_key)
            await _cache_passthrough_plan(stream_key, plan)
            try:
                response = await _direct_passthrough_response(request, plan, quality_mode)
                await _record_pull_started(video_id, quality_mode, plan)
                await _record_started(video_id, quality_mode, client_ip, plan)
                return response
            except httpx.HTTPError as exc:
                await _clear_passthrough_plan(stream_key)
                log.warning("视频 %s/%s progressive 直通失败: %r", video_id, quality_mode, exc)
                return PlainTextResponse("Bad Gateway: 视频转发失败", status_code=502)

        broadcaster.plan = plan
        relay_task = asyncio.create_task(
            run_relay(
                video_id,
                broadcaster,
                plan,
                CFG,
                registry,
                admission,
                _on_cache_complete,
                _record_pull_started,
                log,
                cache_suffix=_cache_suffix(quality_mode),
                stream_key=stream_key,
                quality_mode=quality_mode,
            )
        )
        await _track_relay(stream_key, relay_task)
        asyncio.create_task(
            _watch_idle(video_id, broadcaster, relay_task, CFG["limits"]["idle_cancel_seconds"], log)
        )

    sid, queue = await broadcaster.subscribe()
    # 等到第一块真实字节（或失败信号）再决定响应状态码：一旦 StreamingResponse
    # 开始发送就无法再改成 502，所以必须在返回响应前就确认取流已经成功启动。
    first_chunk = await queue.get()
    if first_chunk is None:
        await broadcaster.unsubscribe(sid)
        log.warning("视频 %s/%s 转发失败，无法返回任何字节: %s",
                    video_id, quality_mode, broadcaster.error)
        return PlainTextResponse("Bad Gateway: 视频转发失败", status_code=502)

    await _record_started(video_id, quality_mode, client_ip, broadcaster.plan if hasattr(broadcaster, "plan") else (plan if is_new else None))

    async def _stream():
        yield first_chunk
        async for chunk in _consume(sid, queue, broadcaster):
            yield chunk

    return StreamingResponse(_stream(), media_type="video/mp4")
