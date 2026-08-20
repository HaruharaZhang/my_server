"""anime_calendar 阶段：日本动漫周表 + 罗小黑战记最新一话，日历风格 tab 的数据来源。

周表改用 Jikan（MyAnimeList 非官方 API）：按星期几分别请求，只覆盖日本
本土在播动漫，不含国产动画——之前用的 B 站 timeline_global 混了大量
国产番剧，不符合"只看日本动漫"的需求。Jikan 是海外目标，走本地代理；
每天一页已够用（各星期几观察到最多 24 条，均未触发分页）。

罗小黑战记不属于"日本动漫"周表，但用户单独要求跟踪最新一话，用 B 站
番剧 season 接口单独抓，不受上面换源影响，直连即可（B 站本身可达）。

两个来源互不影响：任一部分抓取失败只记 warning 并保留该部分上次缓存，
不会因为一个源挂了就把另一个也清空。
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import collect
import images

BEIJING = timezone(timedelta(hours=8))
JIKAN_SCHEDULE_URL = "https://api.jikan.moe/v4/schedules"
WEEKDAY_FILTERS = [
    ("monday", 1), ("tuesday", 2), ("wednesday", 3), ("thursday", 4),
    ("friday", 5), ("saturday", 6), ("sunday", 7),
]
LUOXIAOHEI_SEASON_URL = "https://api.bilibili.com/pgc/view/web/season?season_id=1733"


def fetch_weekday_items(log):
    """按星期几分别请求 Jikan schedule；全部失败则上抛，由调用方回退缓存。"""
    weekday_items = {str(n): [] for _, n in WEEKDAY_FILTERS}
    ok_days = 0
    for filter_name, day_num in WEEKDAY_FILTERS:
        url = f"{JIKAN_SCHEDULE_URL}?filter={filter_name}&sfw=true&kids=false"
        try:
            raw = collect.http_get_json(url, timeout=15, retries=2, use_proxy=True)
        except Exception as exc:
            log.warning("Jikan 周表抓取失败（%s）: %s", filter_name, exc)
            continue
        ok_days += 1
        for anime in raw.get("data") or []:
            broadcast = anime.get("broadcast") or {}
            jpg = (anime.get("images") or {}).get("jpg") or {}
            title = anime.get("title_english") or anime.get("title") or ""
            if not title or not anime.get("url"):
                continue
            weekday_items[str(day_num)].append({
                "title": title,
                "url": anime.get("url", ""),
                "cover_url": jpg.get("large_image_url") or jpg.get("image_url") or "",
                "pub_time": broadcast.get("time") or "",
                "delay": False,
                "delay_reason": "",
            })
        time.sleep(0.4)  # 遵守 Jikan 3 req/s 限速
    if ok_days == 0:
        raise RuntimeError("Jikan 周表全部抓取失败")
    for items_for_day in weekday_items.values():
        items_for_day.sort(key=lambda item: item["pub_time"])
    return weekday_items


def fetch_luoxiaohei(log):
    """罗小黑战记最新一话：B 站番剧 season 接口，episodes 最后一条即最新。"""
    raw = collect.http_get_json(LUOXIAOHEI_SEASON_URL, timeout=15, retries=2, use_proxy=False)
    episodes = ((raw or {}).get("result") or {}).get("episodes") or []
    if not episodes:
        return None
    latest = episodes[-1]
    pub_ts = latest.get("pub_time")
    pub_dt = datetime.fromtimestamp(pub_ts, tz=BEIJING) if pub_ts else None
    return {
        "episode_number": str(latest.get("title") or ""),
        "episode_title": latest.get("long_title") or "",
        "pub_date": pub_dt.strftime("%Y-%m-%d") if pub_dt else "",
        "url": latest.get("link", ""),
        "cover_url": latest.get("cover", ""),
    }


def attach_images(cfg, weekday_items, luoxiaohei, today, log):
    """按 cover_url 去重下载消毒图片；周表（海外源）走代理，罗小黑（B 站）直连。"""
    by_cover = {}
    for items_for_day in weekday_items.values():
        for item in items_for_day:
            url = item.get("cover_url", "")
            if url:
                by_cover.setdefault(url, []).append(item)

    pseudo_items = [{"image_url": url, "image_proxy": True} for url in by_cover]
    lxh_cover = luoxiaohei.get("cover_url") if luoxiaohei else ""
    if lxh_cover:
        pseudo_items.append({"image_url": lxh_cover, "image_proxy": False})

    images.fetch_images(cfg, pseudo_items, today, log)
    for pseudo in pseudo_items:
        image = pseudo.get("image")
        if not image:
            continue
        if luoxiaohei and pseudo["image_url"] == lxh_cover:
            luoxiaohei["image"] = image
        for item in by_cover.get(pseudo["image_url"], []):
            item["image"] = image


def refresh(cfg, today, log):
    """两个来源分别回退缓存，最终写入前任何异常都不上抛，保留上次缓存文件。"""
    out_path = Path(cfg["paths"]["out_dir"]) / "anime-timeline.json"
    previous = {}
    if out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    try:
        weekday_items = fetch_weekday_items(log)
    except Exception:
        log.warning("动漫周表刷新失败，保留上次缓存", exc_info=True)
        weekday_items = previous.get("weekday_items") or {str(n): [] for _, n in WEEKDAY_FILTERS}

    try:
        luoxiaohei = fetch_luoxiaohei(log) or previous.get("luoxiaohei")
    except Exception:
        log.warning("罗小黑战记最新话刷新失败，保留上次缓存", exc_info=True)
        luoxiaohei = previous.get("luoxiaohei")

    try:
        attach_images(cfg, weekday_items, luoxiaohei, today, log)
        payload = {"weekday_items": weekday_items, "luoxiaohei": luoxiaohei, "updated_at": today}
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, out_path)
        total = sum(len(v) for v in weekday_items.values())
        log.info("动漫时间表刷新完成，共 %d 部日本动漫，罗小黑战记=%s", total, "有" if luoxiaohei else "无")
    except Exception:
        log.warning("动漫时间表写入失败，保留上次缓存", exc_info=True)
