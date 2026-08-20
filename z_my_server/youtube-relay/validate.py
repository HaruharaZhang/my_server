"""不可信输入校验：query string 手动拆分、YouTube 域名白名单、video_id 提取。

link= 之后到字符串末尾的原始内容整体取出（不用标准 query dict 解析），
这样用户粘贴未转义的 YouTube 链接（内含 & 和 =）也不会被截断。
"""

import re
import urllib.parse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TOKEN_RE = re.compile(r"(?:^|&)token=([^&]*)")
QUALITY_RE = re.compile(r"(?:^|&)quality=([^&]*)")


def parse_query(raw_query):
    """从原始 query string 里取出 token/quality 和 link（原始尾部整体取出）。"""
    token = ""
    match = TOKEN_RE.search(raw_query)
    if match:
        token = urllib.parse.unquote_plus(match.group(1))

    link_idx = raw_query.find("link=")
    prefix = raw_query[:link_idx] if link_idx != -1 else raw_query
    quality = ""
    match = QUALITY_RE.search(prefix)
    if match:
        quality = urllib.parse.unquote_plus(match.group(1)).strip().lower()

    link = urllib.parse.unquote(raw_query[link_idx + 5:]) if link_idx != -1 else ""
    return token, link, quality


def extract_video_id(link, allowed_hosts):
    """校验 host 在白名单内并返回合法的 11 位 video_id；不合法返回空串。"""
    if not link:
        return ""
    try:
        parsed = urllib.parse.urlsplit(link.strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        return ""
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    else:
        qs = urllib.parse.parse_qs(parsed.query)
        values = qs.get("v", [])
        candidate = values[0] if values else ""
        if not candidate and parsed.path.startswith("/shorts/"):
            candidate = parsed.path[len("/shorts/"):].split("/")[0]
    candidate = candidate.strip()
    return candidate if VIDEO_ID_RE.match(candidate) else ""


def canonical_watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


def is_allowed_thumbnail(url, allowed_hosts):
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    return (parsed.hostname or "").lower() in allowed_hosts
