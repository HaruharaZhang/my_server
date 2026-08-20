"""collect 阶段：预取各信息源的条目列表作为候选。

只预取 RSS/API 的“列表页”（feedparser/urllib），不访问任何文章正文页。
条目自带的现成图片 URL 一并提取，供 images 阶段下载消毒。
"""

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from datetime import datetime, timedelta, timezone

import feedparser

from llm import new_usage

USER_AGENT = "Mozilla/5.0 (compatible; dyyjs-news-bot/1.0)"
BEIJING = timezone(timedelta(hours=8))
LOCAL_PROXY = "http://127.0.0.1:7890"


GITHUB_REPO_RE = re.compile(r"^https?://github\.com/([^/?#]+/[^/?#]+)/?$", re.I)
GITHUB_LINK_LAST_RE = re.compile(r"[?&]page=(\d+)[^>]*>;\s*rel=\"last\"")


def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


def normalize_url(url):
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+$", "", parsed.path)
    query_pairs = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    query = urllib.parse.urlencode(query_pairs)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def github_repo_key(url):
    match = GITHUB_REPO_RE.match(url.strip())
    return match.group(1).lower() if match else ""


def seed_dedup_key(seed):
    repo = github_repo_key(seed.get("url", ""))
    if repo:
        return f"github:{repo}"
    return f"url:{normalize_url(seed.get('url', ''))}"


def http_open(url, timeout=10, use_proxy=False, accept="*/*", headers=None):
    handlers = []
    if use_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": LOCAL_PROXY, "https": LOCAL_PROXY}))
    opener = urllib.request.build_opener(*handlers)
    req_headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    return opener.open(req, timeout=timeout)


def http_get_json(url, timeout=10, retries=2, use_proxy=False, headers=None):
    """预取 JSON 列表页，带超时与重试；失败抛出最后一次异常。"""
    last_exc = None
    for _ in range(retries + 1):
        try:
            with http_open(url, timeout=timeout, use_proxy=use_proxy, accept="application/json", headers=headers) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_exc = exc
    raise last_exc


def github_api_headers():
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_github_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BEIJING)
    except ValueError:
        return None


def format_github_time(value):
    dt = parse_github_time(value)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(value or "").replace("T", " ").replace("Z", "")[:16]


def github_branch_count(full_name, source, headers):
    url = f"https://api.github.com/repos/{full_name}/branches?per_page=1"
    with http_open(
        url,
        timeout=5,
        use_proxy=source.get("proxy", False),
        accept="application/vnd.github+json",
        headers=headers,
    ) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        link = resp.headers.get("Link", "")
    match = GITHUB_LINK_LAST_RE.search(link)
    if match:
        return int(match.group(1))
    return len(body) if isinstance(body, list) else ""


def github_latest_release_at(full_name, source, headers):
    url = f"https://api.github.com/repos/{full_name}/releases/latest"
    with http_open(
        url,
        timeout=5,
        use_proxy=source.get("proxy", False),
        accept="application/vnd.github+json",
        headers=headers,
    ) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("published_at") or data.get("created_at") or ""


def github_repo_extra(full_name, source, headers, log):
    extra = {
        "github_branch_count": "",
        "github_latest_release_at": "",
    }
    if not full_name:
        return extra
    try:
        extra["github_branch_count"] = github_branch_count(full_name, source, headers)
    except Exception as exc:
        log.warning("GitHub branch 元信息跳过: %s: %s", full_name, exc)
    try:
        extra["github_latest_release_at"] = format_github_time(github_latest_release_at(full_name, source, headers))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            log.warning("GitHub release 元信息跳过: %s: HTTP %s", full_name, exc.code)
    except Exception as exc:
        log.warning("GitHub release 元信息跳过: %s: %s", full_name, exc)
    return extra


def entry_time(parsed_time):
    if not parsed_time:
        return None
    return datetime(*parsed_time[:6], tzinfo=timezone.utc).astimezone(BEIJING)


def rss_entry_image(entry):
    """提取 RSS 条目自带的图片 URL；没有现成图片就返回空串，不去正文页找。"""
    for media in entry.get("media_content") or []:
        if media.get("url"):
            return media["url"]
    for media in entry.get("media_thumbnail") or []:
        if media.get("url"):
            return media["url"]
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/") and link.get("href"):
            return link["href"]
    match = re.search(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", entry.get("summary", ""), flags=re.I)
    return unescape(match.group(1)) if match else ""


def rss_seeds(source, limit, log):
    if source.get("proxy"):
        with http_open(source["url"], timeout=source.get("timeout", 20), use_proxy=True) as resp:
            parsed = feedparser.parse(resp.read())
    else:
        parsed = feedparser.parse(source["url"], agent=USER_AGENT)
    if not parsed.entries:
        log.warning("源不可用（无条目）: %s %s", source["name"], source["url"])
        return []
    seeds = []
    for entry in parsed.entries[:limit]:
        published = entry_time(entry.get("published_parsed") or entry.get("updated_parsed"))
        seeds.append({
            "title": (entry.get("title") or "").strip(),
            "url": entry.get("link") or "",
            "hint": strip_html(entry.get("summary", ""))[:200],
            "published_at": published.strftime("%Y-%m-%d %H:%M") if published else "",
            "_published": published,
            "source": source["name"],
            "category_hint": "/".join(source["categories"]),
            "image_url": rss_entry_image(entry),
            "_proxy": bool(source.get("proxy")),
            "_dedup_priority": int(source.get("dedup_priority", 0)),
        })
    return seeds


def v2ex_seeds(source, limit, log):
    topics = http_get_json(source["url"], use_proxy=source.get("proxy", False))
    seeds = []
    for topic in topics[:limit]:
        created = datetime.fromtimestamp(topic.get("created", 0), tz=BEIJING)
        seeds.append({
            "title": (topic.get("title") or "").strip(),
            "url": topic.get("url") or "",
            "hint": strip_html(topic.get("content_rendered", ""))[:200],
            "published_at": created.strftime("%Y-%m-%d %H:%M"),
            "_published": created,
            "source": source["name"],
            "category_hint": "/".join(source["categories"]),
            "image_url": "",
            "_proxy": bool(source.get("proxy")),
            "_dedup_priority": int(source.get("dedup_priority", 0)),
        })
    return seeds


def github_seeds(source, limit, log, target_date=None):
    """GitHub search 尽力而为：10s 超时 + 2 次重试，失败由调用方跳过该源。"""
    base_day = target_date or datetime.now(BEIJING).date()
    since = base_day - timedelta(days=source["created_within_days"])
    raw_query = source.get("query", "created:>{since}").format(since=since.isoformat())
    if target_date:
        target_expr = f"created:{target_date.isoformat()}"
        if re.search(r"created:>\d{4}-\d{2}-\d{2}", raw_query):
            raw_query = re.sub(r"created:>\d{4}-\d{2}-\d{2}", target_expr, raw_query)
        elif "created:" not in raw_query:
            raw_query = f"{raw_query} {target_expr}"
    query = urllib.parse.quote(raw_query)
    url = (
        "https://api.github.com/search/repositories"
        f"?q={query}&sort=stars&order=desc&per_page={limit}"
    )
    headers = github_api_headers()
    data = http_get_json(url, timeout=10, retries=2, use_proxy=source.get("proxy", False), headers=headers)
    seeds = []
    for repo in data.get("items", [])[:limit]:
        created_raw = repo.get("created_at", "")
        created_dt = parse_github_time(created_raw)
        created = format_github_time(created_raw)
        full_name = repo.get("full_name") or ""
        seed = {
            "title": full_name,
            "url": repo.get("html_url") or "",
            "hint": f"{repo.get('description') or ''} | stars: {repo.get('stargazers_count', 0)}"[:200],
            "published_at": created,
            "_published": created_dt,
            "source": source["name"],
            "category_hint": "/".join(source["categories"]),
            "image_url": f"https://opengraph.githubassets.com/1/{full_name}" if full_name else "",
            "_proxy": True,  # GitHub OG 图片走本地代理
            "_dedup_priority": int(source.get("dedup_priority", 0)),
            "github_stars": repo.get("stargazers_count", 0),
            "github_open_issues": repo.get("open_issues_count", 0),
            "github_created_at": created,
        }
        seed.update(github_repo_extra(full_name, source, headers, log))
        seeds.append(seed)
    return seeds


def html_list_seeds(source, limit, log):
    """从官方列表页提取新闻链接；用于无 RSS 的站点，如央视网科技频道。"""
    with http_open(source["url"], timeout=15, use_proxy=source.get("proxy", False)) as resp:
        html = resp.read().decode("utf-8", "ignore")
    allow = re.compile(source.get("allow_url_regex", r"https?://[^\"'<> ]+"))
    anchors = re.findall(r"<a\b([^>]*)>(.*?)</a>", html, flags=re.I | re.S)
    seeds, seen = [], set()
    for attrs, body in anchors:
        href_match = re.search(r"""href=["']([^"']+)["']""", attrs, flags=re.I)
        if not href_match:
            continue
        url = urllib.parse.urljoin(source["url"], unescape(href_match.group(1)).strip())
        if not allow.search(url) or url in seen:
            continue
        title_match = re.search(r"""title=["']([^"']+)["']""", attrs, flags=re.I)
        title = unescape(title_match.group(1)).strip() if title_match else strip_html(unescape(body))
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) < 6:
            continue
        published = ""
        date_match = re.search(r"/(20\d{2})/(\d{2})/(\d{2})/", url)
        published_dt = None
        if date_match:
            year, month, day = map(int, date_match.groups())
            published_dt = datetime(year, month, day, tzinfo=BEIJING)
            published = published_dt.strftime("%Y-%m-%d %H:%M")
        seeds.append({
            "title": title,
            "url": url,
            "hint": title,
            "published_at": published,
            "_published": published_dt,
            "source": source["name"],
            "category_hint": "/".join(source["categories"]),
            "image_url": "",
            "_proxy": False,
            "_dedup_priority": int(source.get("dedup_priority", 0)),
        })
        seen.add(url)
        if len(seeds) >= limit:
            break
    if not seeds:
        log.warning("源不可用（列表页无匹配链接）: %s %s", source["name"], source["url"])
    return seeds


def gather_seeds(cfg, seen_urls, log, target_date=None):
    """逐源预取种子；单个源失败只记录并跳过，不影响整体。"""
    socket.setdefaulttimeout(20)
    limits = cfg["limits"]
    now = datetime.now(BEIJING)
    # 有明确目标日时只保留该北京时间自然日 [00:00, 24:00) 内发布的条目。
    # 没有发布时间的源保持宽松策略，避免因列表缺时间直接丢弃。
    days = limits["seed_window_days"]
    if target_date:
        window_start = datetime.combine(target_date, datetime.min.time(), tzinfo=BEIJING)
        window_end = window_start + timedelta(days=1)
    else:
        window_start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = None
    fetchers = {
        "rss": rss_seeds,
        "v2ex": v2ex_seeds,
        "github": github_seeds,
        "html_list": html_list_seeds,
    }
    seeds, unreachable = [], []
    source_stats = {}
    for source in cfg["sources"]:
        if not source.get("enabled"):
            continue
        try:
            if source["type"] == "github":
                batch = github_seeds(source, limits["seed_max_per_source"], log, target_date)
            else:
                batch = fetchers[source["type"]](source, limits["seed_max_per_source"], log)
        except Exception as exc:
            log.warning("源不可用（预取失败）: %s: %s", source["name"], exc)
            unreachable.append(source["name"])
            continue
        kept = []
        for seed in batch:
            if not seed["title"] or not seed["url"] or seed["url"] in seen_urls:
                continue
            if seed["_published"] and seed["_published"] < window_start:
                continue
            if target_date and seed["_published"] and seed["_published"] >= window_end:
                continue
            seed.pop("_published")
            kept.append(seed)
        source_stats[source["name"]] = {
            "prefetched": len(batch),
            "kept_before_dedup": len(kept),
            "dropped_duplicate": 0,
            "final": 0,
        }
        log.info("源 %s: 预取 %d 条，保留 %d 条", source["name"], len(batch), len(kept))
        seeds.extend(kept)
    # 同一 URL/GitHub repo 只保留一个；显式优先级可让更具体的源保住自己的分类。
    unique, key_to_index = [], {}
    for seed in seeds:
        key = seed_dedup_key(seed)
        if key not in key_to_index:
            key_to_index[key] = len(unique)
            unique.append(seed)
            continue
        existing_index = key_to_index[key]
        existing = unique[existing_index]
        existing_priority = int(existing.get("_dedup_priority", 0))
        seed_priority = int(seed.get("_dedup_priority", 0))
        if seed_priority > existing_priority:
            source_stats[existing["source"]]["dropped_duplicate"] += 1
            unique[existing_index] = seed
        else:
            source_stats[seed["source"]]["dropped_duplicate"] += 1
    for seed in unique:
        source_stats[seed["source"]]["final"] += 1
    for name, stats in source_stats.items():
        if stats["kept_before_dedup"] or stats["dropped_duplicate"]:
            log.info(
                "源 %s 去重: 前 %d 条，重复丢弃 %d 条，最终 %d 条",
                name,
                stats["kept_before_dedup"],
                stats["dropped_duplicate"],
                stats["final"],
            )
    return unique, unreachable


async def collect(cfg, seen_urls, log, target_date=None):
    """返回 (候选条目列表, token 用量)。不由 AI 做价值筛选。"""
    seeds, unreachable = gather_seeds(cfg, seen_urls, log, target_date)
    if unreachable:
        log.warning("本次不可达的源: %s", ", ".join(unreachable))
    if not seeds:
        raise RuntimeError("所有信息源均无可用种子")
    log.info("种子总数: %d", len(seeds))

    usage = new_usage()
    per_category = {c: 0 for c in cfg["categories"]}
    candidates = []
    for seed in seeds:
        category = seed.get("category_hint", "").split("/", 1)[0]
        if category not in per_category:
            category = cfg["categories"][0]
        hint = str(seed.get("hint", "")).strip()
        candidates.append({
            "title": str(seed["title"]).strip(),
            "url": seed["url"],
            "summary": hint[:80] or str(seed["title"]).strip(),
            "source": str(seed.get("source", "")).strip(),
            "published_at": str(seed.get("published_at", "")).strip(),
            "category": category,
            "image_url": str(seed.get("image_url", "")).strip(),
            "image_proxy": bool(seed.get("_proxy")),
            "github_stars": seed.get("github_stars", ""),
            "github_open_issues": seed.get("github_open_issues", ""),
            "github_branch_count": seed.get("github_branch_count", ""),
            "github_latest_release_at": seed.get("github_latest_release_at", ""),
            "github_created_at": seed.get("github_created_at", ""),
        })
        per_category[category] += 1
    log.info("候选条目: %d 条，分类分布: %s", len(candidates),
             {k: v for k, v in per_category.items() if v})
    return candidates, usage
