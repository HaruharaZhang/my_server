"""Bilibili 视频解析：view 接口拿 cid/标题/封面/时长，playurl 接口拿单文件 mp4 直链。

全程直连不走代理（B 站国内可达，trust_env=False 防止环境变量把请求带进 mihomo）。
对外请求 URL 只由校验过的 BV 号和接口返回的整数 cid 构造，用户原始字符串永不出站。
匿名 platform=html5 接口目前无需 WBI 签名与登录，最高 720p（qn=64）单文件渐进式 mp4。
"""

import asyncio
import urllib.parse

import httpx

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
RETRY_DELAYS = (0.2, 0.4, 0.8, 1.2, 1.8)
RETRYABLE_STATUS_CODES = {408, 425, 429}


class ResolveError(Exception):
    """解析失败（视频不存在/私密/地区限制/付费/接口变动等），对外统一 502。"""


def direct_client(timeout_seconds, read_timeout=True, follow_redirects=True,
                  connect_retries=2):
    """直连出站客户端：B 站 CDN 是多 A 记录，个别 IP 偶发丢包黑洞，
    连接阶段固定 5s 超时；默认保留 transport 层连接重试。解析 API 会显式关闭它，
    改由可观测的应用层重试控制总次数。
    """
    timeout = httpx.Timeout(timeout_seconds, connect=5.0,
                            read=timeout_seconds if read_timeout else None)
    return httpx.AsyncClient(
        trust_env=False,
        timeout=timeout,
        follow_redirects=follow_redirects,
        transport=httpx.AsyncHTTPTransport(retries=connect_retries),
    )


class BadPageError(ResolveError):
    """分 P 序号越界，对外 400。"""


def _retryable_http_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in RETRYABLE_STATUS_CODES or status >= 500
    return isinstance(exc, httpx.RequestError)


async def resolve(bvid, page, cfg, log):
    """解析一条视频；网络类瞬时故障最多额外重试五次。"""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return await _resolve_once(bvid, page, cfg)
        except (httpx.HTTPError, ValueError) as exc:
            retryable = isinstance(exc, ValueError) or _retryable_http_error(exc)
            if not retryable or attempt >= len(RETRY_DELAYS):
                raise ResolveError(f"B 站接口请求失败: {type(exc).__name__}") from exc
            delay = RETRY_DELAYS[attempt]
            log.warning(
                "视频 %s 解析接口暂时失败: attempt=%d/%d error=%s retry_in=%.1fs",
                bvid, attempt + 1, len(RETRY_DELAYS) + 1,
                type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)


async def _resolve_once(bvid, page, cfg):
    headers = {
        "User-Agent": cfg["bilibili"]["user_agent"],
        "Referer": cfg["bilibili"]["referer"],
    }
    async with direct_client(15.0, connect_retries=0) as client:
        view = await _get_json(client, VIEW_API, {"bvid": bvid}, headers)
        if view.get("code") != 0:
            raise ResolveError(f"view 接口 code={view.get('code')}")
        data = view.get("data") or {}

        if page > 1:
            pages = data.get("pages") or []
            if page > len(pages):
                raise BadPageError(f"分 P 越界: p={page}, 共 {len(pages)} P")
            cid = pages[page - 1].get("cid")
            duration = pages[page - 1].get("duration")
        else:
            cid = data.get("cid")
            duration = data.get("duration")
        if not cid:
            raise ResolveError("view 接口缺少 cid")

        playurl = await _get_json(client, PLAYURL_API, {
            "bvid": bvid,
            "cid": int(cid),
            "qn": 64,
            "fnval": 1,
            "platform": "html5",
            "high_quality": 1,
        }, headers)
        if playurl.get("code") != 0:
            raise ResolveError(f"playurl 接口 code={playurl.get('code')}")
        durl = (playurl.get("data") or {}).get("durl") or []
        if not durl or not durl[0].get("url"):
            raise ResolveError("playurl 接口未返回 durl")

    thumbnail = data.get("pic") or ""
    if thumbnail.startswith("http://"):
        thumbnail = "https://" + thumbnail[len("http://"):]

    return {
        "bvid": bvid,
        "page": page,
        "cid": int(cid),
        "video_url": durl[0]["url"],
        "backup_urls": durl[0].get("backup_url") or [],
        "headers": headers,
        "title": data.get("title") or "",
        "thumbnail": thumbnail,
        "duration": duration or 0,
    }


async def expand_short_link(link, cfg, log):
    """把 b23.tv 短链展开成 bilibili.com 长链。

    只跟 Location 跳转、不取正文，最多 3 跳；每一跳的 host 必须仍在短链白名单内，
    最终落点必须在 bilibili 域名白名单内，否则返回空串（调用方按非法链接处理）。
    """
    allowed_hosts = set(cfg["bilibili"]["allowed_hosts"])
    short_hosts = set(cfg["bilibili"]["short_link_hosts"])
    headers = {"User-Agent": cfg["bilibili"]["user_agent"]}
    url = link.strip()
    try:
        async with direct_client(10.0, follow_redirects=False) as client:
            for _ in range(3):
                host = (urllib.parse.urlsplit(url).hostname or "").lower()
                if host in allowed_hosts:
                    return url
                if host not in short_hosts:
                    return ""
                resp = await client.get(url, headers=headers)
                location = resp.headers.get("location")
                if not location:
                    return ""
                url = urllib.parse.urljoin(url, location)
    except httpx.HTTPError as exc:
        raise ResolveError(f"短链展开失败: {exc!r}") from exc
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return url if host in allowed_hosts else ""


async def _get_json(client, url, params, headers):
    resp = await client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp.json()
