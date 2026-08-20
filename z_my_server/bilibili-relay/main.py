"""dyyjs.com/bilibili — Bilibili 播放代理 FastAPI 后端。

单进程运行（uvicorn 单 worker），保证熔断计数器/并发计数/plan 缓存这些内存态只有一份。
与 youtube-relay 的区别：B 站国内直连不走代理；匿名 html5 接口拿到的是 720p 单文件
渐进式 mp4，纯 Range 透传转发即可，不落盘缓存、不需要 ffmpeg 合流。
"""

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

import catalog
import render
import resolver
import runtime_status
from config import load_config
from security import TokenGuard
from validate import extract_bvid, is_short_link, parse_query

CFG = load_config()

for key in ("state_dir", "log_dir"):
    Path(CFG["paths"][key]).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(CFG["paths"]["log_dir"]) / "dyyjs-bilibili.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("dyyjs-bilibili")

_tokens_raw = os.environ.get("BILIBILI_RELAY_TOKENS", "")
guard = TokenGuard(
    tokens=[t.strip() for t in _tokens_raw.split(",") if t.strip()],
    killswitch_path=CFG["paths"]["killswitch_path"],
    window_seconds=CFG["limits"]["auth_window_seconds"],
    max_failures=CFG["limits"]["auth_max_failures"],
    log=log,
)

plan_cache = {}
plan_cache_lock = asyncio.Lock()
resolve_tasks = {}
resolve_tasks_lock = asyncio.Lock()

short_link_cache = {}
short_link_cache_lock = asyncio.Lock()
SHORT_LINK_CACHE_MAX = 256

_active_streams = 0
_active_streams_lock = asyncio.Lock()

app = FastAPI()
runtime_status.write(CFG, "idle")


async def _try_acquire_stream():
    global _active_streams
    async with _active_streams_lock:
        if _active_streams >= CFG["limits"]["max_concurrent_streams"]:
            return False
        _active_streams += 1
        return True


async def _release_stream():
    global _active_streams
    async with _active_streams_lock:
        _active_streams = max(0, _active_streams - 1)


async def _get_cached_entry(cache_key):
    async with plan_cache_lock:
        entry = plan_cache.get(cache_key)
        if entry is None:
            return None
        if entry["expires_at"] <= time.monotonic():
            plan_cache.pop(cache_key, None)
            return None
        return entry


async def _cache_plan(cache_key, plan):
    entry = {
        "expires_at": time.monotonic() + CFG["limits"]["plan_cache_seconds"],
        "plan": plan,
        "pull_recorded": False,
    }
    async with plan_cache_lock:
        plan_cache[cache_key] = entry
    return entry


async def _clear_plan(cache_key):
    async with plan_cache_lock:
        plan_cache.pop(cache_key, None)


async def _expand_short_link_cached(link):
    """展开 b23.tv 短链并缓存结果，播放器 Range 探测不会反复请求 b23.tv。"""
    async with short_link_cache_lock:
        cached = short_link_cache.get(link)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        short_link_cache.pop(link, None)

    expanded = await resolver.expand_short_link(link, CFG, log)
    if expanded:
        async with short_link_cache_lock:
            now = time.monotonic()
            expired = [key for key, (expires_at, _) in short_link_cache.items() if expires_at <= now]
            for key in expired:
                short_link_cache.pop(key, None)
            while len(short_link_cache) >= SHORT_LINK_CACHE_MAX:
                short_link_cache.pop(next(iter(short_link_cache)))
            short_link_cache[link] = (now + CFG["limits"]["plan_cache_seconds"], expanded)
    return expanded


async def _record_play(bvid, client_key, plan):
    changed, is_new = await catalog.record_play(
        CFG, bvid, plan.get("title", ""), plan.get("thumbnail", ""),
        plan.get("duration"), client_key, log,
    )
    if changed:
        render.render_and_publish(CFG, catalog.load_catalog(CFG["paths"]["catalog_path"]), log)
    if is_new:
        log.info("catalog 新增条目: %s", bvid)


async def _record_pull_once(entry):
    """同一份解析结果（plan 缓存条目）只记一次拉流会话，播放器反复 Range 探测不刷计数。"""
    async with plan_cache_lock:
        if entry["pull_recorded"]:
            return
        entry["pull_recorded"] = True
    plan = entry["plan"]
    changed, is_new = await catalog.record_pull(
        CFG, plan["bvid"], plan.get("title", ""), plan.get("thumbnail", ""),
        plan.get("duration"), log,
    )
    if changed:
        render.render_and_publish(CFG, catalog.load_catalog(CFG["paths"]["catalog_path"]), log)
    if is_new:
        log.info("catalog 新增拉流条目: %s", plan["bvid"])


async def _resolve_and_cache_once(bvid, page, cache_key):
    """解析并写入 plan 缓存；失败时直接返回错误响应对象。"""
    try:
        plan = await resolver.resolve(bvid, page, CFG, log)
    except resolver.BadPageError as exc:
        log.warning("视频 %s 分 P 无效: %s", bvid, exc)
        return PlainTextResponse("Bad Request: 分 P 不存在", status_code=400)
    except resolver.ResolveError as exc:
        runtime_status.write(CFG, "error", error_category="resolve_error")
        log.warning("视频 %s 解析失败: %s", bvid, exc)
        return PlainTextResponse("Bad Gateway: 无法解析该视频", status_code=502)

    duration = plan.get("duration") or 0
    if duration > CFG["limits"]["max_duration_seconds"]:
        log.warning("视频 %s 超过时长上限: %ss", bvid, duration)
        return PlainTextResponse("Payload Too Large: 视频过长", status_code=413)

    return await _cache_plan(cache_key, plan)


async def _remove_resolve_task(cache_key, task):
    async with resolve_tasks_lock:
        if resolve_tasks.get(cache_key) is task:
            resolve_tasks.pop(cache_key, None)
    if not task.cancelled():
        task.exception()


def _schedule_resolve_task_cleanup(cache_key, task):
    asyncio.create_task(_remove_resolve_task(cache_key, task))


async def _resolve_and_cache(bvid, page, cache_key):
    """同一 BV+分P 的冷缓存请求共享一次解析，避免 Range 探测并发冲击接口。"""
    async with resolve_tasks_lock:
        task = resolve_tasks.get(cache_key)
        if task is None:
            task = asyncio.create_task(
                _resolve_and_cache_once(bvid, page, cache_key))
            resolve_tasks[cache_key] = task
            task.add_done_callback(
                lambda done: _schedule_resolve_task_cleanup(cache_key, done))
    return await asyncio.shield(task)


async def _direct_passthrough_response(request, plan):
    """B 站单文件 mp4 天然支持字节 Range；把播放器的 Range 原样透传给 CDN。

    成功返回后，并发名额由响应 body 的 finally 释放；本函数抛异常时名额仍归调用方管。
    """
    req_headers = dict(plan.get("headers") or {})
    range_header = request.headers.get("range")
    if range_header:
        req_headers["Range"] = range_header

    client = resolver.direct_client(30.0, read_timeout=False)
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
    upstream_bytes = 0
    runtime_status.write(CFG, "streaming")

    async def body():
        nonlocal upstream_bytes
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=CFG["limits"]["chunk_size"]):
                if chunk:
                    upstream_bytes += len(chunk)
                    yield chunk
        finally:
            await stream.__aexit__(None, None, None)
            await client.aclose()
            await _release_stream()
            elapsed = max(1, time.monotonic() - started)
            runtime_status.add_upstream_bytes(CFG, upstream_bytes)
            runtime_status.write(CFG, "idle", upstream_bytes, upstream_bytes / elapsed)

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )


@app.get("/bilibili")
async def bilibili_relay(request: Request):
    if guard.killswitch_active():
        return PlainTextResponse("服务器繁忙，请稍后重试", status_code=503)

    raw_query = request.scope["query_string"].decode("utf-8", "ignore")
    token, link = parse_query(raw_query)
    client_ip = request.client.host if request.client else "unknown"

    if not guard.check(token, client_ip):
        if guard.killswitch_active():
            return PlainTextResponse("服务器繁忙，请稍后重试", status_code=503)
        return PlainTextResponse("Forbidden", status_code=403)

    if not link:
        return PlainTextResponse("Bad Request: missing link", status_code=400)

    if is_short_link(link, set(CFG["bilibili"]["short_link_hosts"])):
        try:
            link = await _expand_short_link_cached(link)
        except resolver.ResolveError as exc:
            log.warning("短链展开失败: %s", exc)
            return PlainTextResponse("Bad Gateway: 短链展开失败", status_code=502)

    bvid, page = extract_bvid(link, set(CFG["bilibili"]["allowed_hosts"]))
    if not bvid:
        return PlainTextResponse("Bad Request: unsupported link", status_code=400)

    cache_key = f"{bvid}:{page}"
    entry = await _get_cached_entry(cache_key)
    if entry is None:
        entry = await _resolve_and_cache(bvid, page, cache_key)
        if isinstance(entry, PlainTextResponse):
            return entry

    if not await _try_acquire_stream():
        return PlainTextResponse("服务器繁忙，请稍后重试", status_code=503)

    handed_over = False
    try:
        try:
            response = await _direct_passthrough_response(request, entry["plan"])
        except httpx.HTTPError as exc:
            # 直链失效（签名过期/网络错误）：清缓存重新解析，整个请求只重试一次
            log.warning("视频 %s 直通失败，重新解析后重试: %r", cache_key, exc)
            await _clear_plan(cache_key)
            entry = await _resolve_and_cache(bvid, page, cache_key)
            if isinstance(entry, PlainTextResponse):
                return entry
            try:
                response = await _direct_passthrough_response(request, entry["plan"])
            except httpx.HTTPError as exc2:
                log.warning("视频 %s 重试后仍直通失败: %r", cache_key, exc2)
                return PlainTextResponse("Bad Gateway: 视频转发失败", status_code=502)
        handed_over = True
    finally:
        if not handed_over:
            await _release_stream()

    await _record_pull_once(entry)
    await _record_play(bvid, client_ip, entry["plan"])
    return response
