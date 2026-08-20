"""播放历史静态页渲染：Jinja2 显式开启 autoescape（视频标题是攻击者可控内容）。"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

BEIJING = timezone(timedelta(hours=8))
VERSION = "v1.2.0"


def format_duration(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return "待确认"
    total = round(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分{seconds}秒"


def render_and_publish(cfg, catalog, log):
    template_dir = Path(__file__).parent
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"], default=True),
    )
    template = env.get_template("template.html")

    # last_played_at 可能是 None（只被拉取过、从未播放的条目），排序时归为空串垫底
    entries = sorted(catalog.values(), key=lambda e: e.get("last_played_at") or "", reverse=True)
    html = template.render(
        entries=entries,
        format_duration=format_duration,
        updated_at=datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        version=VERSION,
    )

    target = Path(cfg["paths"]["publish_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, target)
    log.info("播放历史页已发布: %s (%d 条)", target, len(entries))
