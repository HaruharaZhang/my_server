"""不可信输入校验：query string 手动拆分、Bilibili 域名白名单、BV 号提取。

link= 之后到字符串末尾的原始内容整体取出（不用标准 query dict 解析），
这样用户粘贴未转义的 Bilibili 链接（内含 & 和 =）也不会被截断。
"""

import re
import urllib.parse

BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
TOKEN_RE = re.compile(r"(?:^|&)token=([^&]*)")


def parse_query(raw_query):
    """从原始 query string 里取出 token 和 link（原始尾部整体取出）。"""
    token = ""
    match = TOKEN_RE.search(raw_query)
    if match:
        token = urllib.parse.unquote_plus(match.group(1))

    link_idx = raw_query.find("link=")
    link = urllib.parse.unquote(raw_query[link_idx + 5:]) if link_idx != -1 else ""
    return token, link


def extract_bvid(link, allowed_hosts):
    """校验 host 在白名单内并提取 BV 号和分 P 序号；不合法返回 ("", 1)。

    BV 号直接在整个链接字符串里正则搜索，路径后面跟着的 ?spm_id_from=...
    之类的杂质参数自然被忽略。分 P 取链接 query 里的 p=（正整数），默认第 1 P。
    """
    if not link:
        return "", 1
    try:
        parsed = urllib.parse.urlsplit(link.strip())
    except ValueError:
        return "", 1
    if parsed.scheme not in ("http", "https"):
        return "", 1
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        return "", 1

    match = BVID_RE.search(link)
    if not match:
        return "", 1

    page = 1
    values = urllib.parse.parse_qs(parsed.query).get("p", [])
    if values:
        try:
            page = max(1, int(values[0]))
        except ValueError:
            page = 1
    return match.group(0), page


def is_short_link(link, short_hosts):
    """b23.tv 分享短链：host 精确白名单 + 默认端口，服务器才允许主动展开。"""
    try:
        parsed = urllib.parse.urlsplit(link.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    try:
        if parsed.port not in (None, 80, 443):
            return False
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in short_hosts


def canonical_video_url(bvid, page=1):
    url = f"https://www.bilibili.com/video/{bvid}"
    return f"{url}?p={page}" if page > 1 else url


def is_allowed_thumbnail(url, allowed_host_suffix):
    """B 站封面在 i0/i1/i2.hdslb.com 等图床上，按域名后缀白名单校验。"""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host.endswith(allowed_host_suffix)
