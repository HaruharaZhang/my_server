"""render 阶段：Jinja2 渲染 template.html 生成静态资讯页。

digest 的 [文字](#序号) 标记在这里转成安全 HTML：先整体转义，
再把合法序号替换为指向已收录条目 URL 的链接；序号不存在只留文字。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

BEIJING = timezone(timedelta(hours=8))

PAGE_VERSION = "v3.4.4"
DISPLAY_DAYS = 30

DIGEST_LINK_RE = re.compile(r"\[([^\[\]]+)\]\(#(\d+)\)")


def digest_to_html(digest, items):
    """把 digest 文本转成安全 HTML。href 只来自已收录条目的 URL。"""
    escaped = escape(digest)

    def replace(match):
        text, index = match.group(1), int(match.group(2))
        if 0 <= index < len(items):
            url = escape(items[index]["url"], quote=True)
            return f'<a href="{url}" target="_blank" rel="noopener">{text}</a>'
        return text  # 模型编造的序号：去掉标记只留文字

    return DIGEST_LINK_RE.sub(replace, escaped)


DISPLAY_FIELDS = (
    "title",
    "title_zh",
    "url",
    "summary",
    "source",
    "published_at",
    "image",
    "github_stars",
    "github_open_issues",
    "github_branch_count",
    "github_latest_release_at",
    "github_created_at",
)
GITHUB_META_CATEGORIES = {"热门开源项目", "人气攀升开源项目"}


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_models(models_used, cfg):
    priority = list(cfg.get("api", {}).get("model_priority", []))
    labels = []
    for model in models_used or []:
        try:
            rank = priority.index(model) + 1
        except ValueError:
            labels.append(model)
        else:
            labels.append(f"{model} ({ordinal(rank)})")
    return labels


def parse_item_day(path):
    match = re.search(r"items-(20\d{2}-\d{2}-\d{2})\.json$", path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def load_recent_payloads(cfg, current_payload, current_date, log):
    try:
        end_day = datetime.strptime(current_date, "%Y-%m-%d").date()
    except ValueError:
        log.warning("current_date 格式错误，回退为今天: %s", current_date)
        end_day = datetime.now(BEIJING).date()
        current_date = end_day.isoformat()
    cutoff = end_day - timedelta(days=DISPLAY_DAYS - 1)
    payloads = {}
    out_dir = Path(cfg["paths"]["out_dir"])
    for path in out_dir.glob("items-*.json"):
        day = parse_item_day(path)
        if not day or day < cutoff or day > end_day:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("跳过无法读取的历史 items 文件 %s: %s", path, exc)
            continue
        payloads[day.isoformat()] = payload
    if current_payload and (
        current_payload.get("items")
        or current_payload.get("digest")
        or current_payload.get("digests_by_category")
        or current_payload.get("models_used")
    ):
        payloads[current_date] = current_payload
    return [(day, payloads[day]) for day in sorted(payloads, reverse=True)]


def category_digest(payload, category, has_entries):
    digests = payload.get("digests_by_category") or {}
    if isinstance(digests, dict) and digests.get(category):
        return digests[category]
    return payload.get("digest", "") if has_entries else ""


def label_day(day):
    dt = datetime.strptime(day, "%Y-%m-%d")
    return dt.strftime("%m月%d日")


def load_anime_timeline(cfg, log):
    path = Path(cfg["paths"]["out_dir"]) / "anime-timeline.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("跳过无法读取的动漫时间表文件 %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def build_calendar_section(category, timeline_payload, current_date):
    weekday_items = timeline_payload.get("weekday_items") or {}
    total = sum(len(v) for v in weekday_items.values())
    return {
        "name": category,
        "count": total,
        "layout": "calendar",
        "weekday_items": weekday_items,
        "luoxiaohei": timeline_payload.get("luoxiaohei"),
        "today": current_date,
    }


def display_item(item, category):
    entry = {k: item.get(k, "") for k in DISPLAY_FIELDS}
    if category not in GITHUB_META_CATEGORIES:
        for key in DISPLAY_FIELDS:
            if key.startswith("github_"):
                entry[key] = ""
    return entry


def parse_published_at(value):
    text = str(value or "").strip()
    if not text:
        return None
    formats = (
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    )
    for fmt, length in formats:
        try:
            return datetime.strptime(text[:length], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def sort_entries(entries):
    def key(entry):
        published = parse_published_at(entry.get("published_at"))
        if not published:
            return (0, datetime.min)
        if published.tzinfo:
            published = published.astimezone(BEIJING).replace(tzinfo=None)
        return (1, published)

    return sorted(entries, key=key, reverse=True)


def build_cve_section(category, payload):
    status = payload.get("status") or {}
    services = payload.get("services") or []
    unique_services = {service.get("service_key") for service in services if service.get("service_key")}
    return {
        "name": category, "count": len(unique_services), "layout": "cve",
        "days": payload.get("days") or [], "status": status,
        "window": payload.get("window") or {},
    }


def build_sections(cfg, dated_payloads, timeline_payload, current_date, cve_payload=None):
    sections = []
    layout_by_category = cfg.get("category_layout", {})
    for category in cfg["categories"]:
        if layout_by_category.get(category) == "cve":
            sections.append(build_cve_section(category, cve_payload or {}))
            continue
        if layout_by_category.get(category) == "calendar":
            sections.append(build_calendar_section(category, timeline_payload, current_date))
            continue
        days = []
        total = 0
        for day, payload in dated_payloads:
            all_items = payload.get("items") or []
            entries = [
                display_item(item, category)
                for item in all_items if item.get("category") == category
            ]
            entries = sort_entries(entries)
            digest = category_digest(payload, category, bool(entries))
            if not entries and not digest:
                continue
            total += len(entries)
            days.append({
                "date": day,
                "label": label_day(day),
                "entries": entries,
                "digest_html": digest_to_html(digest, all_items) if digest else "",
                "models": format_models(payload.get("models_used") or [], cfg),
            })
        sections.append({"name": category, "count": total, "days": days})
    return sections


def render(cfg, items, digest, models_used, log, current_date=None, digests_by_category=None, cve_payload=None):
    """按 config 的分类顺序分组渲染多日期静态页面，返回生成的 HTML 文件路径。"""
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(["html"]),
    )
    current_date = current_date or datetime.now(BEIJING).strftime("%Y-%m-%d")
    current_payload = {
        "items": items,
        "digest": digest,
        "digests_by_category": digests_by_category or {},
        "models_used": models_used,
    } if items is not None else None
    dated_payloads = load_recent_payloads(cfg, current_payload, current_date, log)
    timeline_payload = load_anime_timeline(cfg, log)
    sections = build_sections(cfg, dated_payloads, timeline_payload, current_date, cve_payload)
    # 卡片由前端 JS 按需渲染；JSON 内嵌进 <script>，转义 < 防止提前闭合标签
    sections_json = json.dumps(sections, ensure_ascii=False).replace("<", "\\u003c")
    html = env.get_template("template.html").render(
        sections=sections,
        sections_json=sections_json,
        models_used=format_models(models_used, cfg),
        version=PAGE_VERSION,
        updated_at=datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
    )
    out_path = Path(cfg["paths"]["out_dir"]) / "index.html"
    out_path.write_text(html, encoding="utf-8")
    log.info("已渲染 %s（%d 字节）", out_path, len(html.encode("utf-8")))
    return out_path
