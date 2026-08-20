#!/usr/bin/env python3
"""东云资讯每日流水线编排：collect → summarize → images → render → publish。

任一阶段失败：保留昨日已发布页面，退出码非 0（journalctl 可见完整堆栈）。
"""

import asyncio
import argparse
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anime_calendar
import collect
import cve
import images
import render
import summarize
from llm import ModelRouter, load_config

BEIJING = timezone(timedelta(hours=8))


def write_run_status(cfg, payload):
    """Internal structured status only; never published directly."""
    path = Path(cfg["paths"]["state_dir"]) / "status.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def parse_args():
    parser = argparse.ArgumentParser(description="东云资讯流水线")
    parser.add_argument("--backfill", action="store_true", help="回填指定日期，不更新 seen.json")
    parser.add_argument("--date", help="目标日期，格式 YYYY-MM-DD；配合 --backfill 使用")
    return parser.parse_args()


def parse_day(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--date 格式错误，应为 YYYY-MM-DD: {value}") from exc


def setup_logging(cfg, today):
    log_dir = Path(cfg["paths"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / f"news-{today}.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("dyyjs-news")


DATED_FILE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def prune_dated_files(directory, keep_days, log):
    """删除文件名含 YYYY-MM-DD 且超期的文件（归档页、日志、中间 JSON 都用此命名）。"""
    cutoff = (datetime.now(BEIJING) - timedelta(days=keep_days)).date()
    for path in Path(directory).glob("*"):
        match = DATED_FILE_RE.search(path.stem)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            path.unlink()
            log.info("清理过期文件: %s", path)


def load_seen(cfg):
    path = Path(cfg["paths"]["state_dir"]) / "seen.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_seen(cfg, seen, log):
    cutoff = (datetime.now(BEIJING) - timedelta(days=cfg["retention_days"]["seen"])).date()
    kept = {
        url: day for url, day in seen.items()
        if datetime.strptime(day, "%Y-%m-%d").date() >= cutoff
    }
    path = Path(cfg["paths"]["state_dir"]) / "seen.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    log.info("seen.json 现有 %d 条记录", len(kept))


def publish(cfg, html_path, today, log):
    """tmp 写入 + 原子替换，再归档当日快照。"""
    target = Path(cfg["paths"]["publish_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(html_path, tmp)
    os.replace(tmp, target)
    log.info("已发布 %s", target)

    archive_dir = Path(cfg["paths"]["archive_dir"])
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(target, archive_dir / f"{today}.html")


def load_existing_payload(cfg, target, log):
    path = Path(cfg["paths"]["out_dir"]) / f"items-{target}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("跳过无法读取的当天已发布 items 文件 %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def merge_items(existing_items, new_items):
    merged = []
    seen_urls = set()
    for item in list(existing_items or []) + list(new_items or []):
        raw_url = str(item.get("url", "")).strip()
        if not raw_url:
            continue
        url = collect.normalize_url(raw_url)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(item)
    return merged


async def run_llm_stages(cfg, candidates, log, existing_items=None):
    """summarize 阶段共用一个 ModelRouter，返回 (items, digest, digests_by_category, router)。"""
    router = ModelRouter(cfg, log)
    await router.init_models()
    candidates = await summarize.deduplicate_candidates(cfg, router, candidates, log)
    new_items = await summarize.summarize_items(cfg, router, candidates, log)
    items = merge_items(existing_items, new_items)
    if existing_items:
        log.info("合并当天已有条目: 旧 %d 条，新 %d 条，合并后 %d 条",
                 len(existing_items), len(new_items), len(items))
    digest = await summarize.daily_digest(cfg, router, items, log)
    digests_by_category = await summarize.daily_digests_by_category(cfg, router, items, log)
    return items, digest, digests_by_category, router


def main():
    args = parse_args()
    if args.backfill and not args.date:
        raise SystemExit("--backfill 需要指定 --date YYYY-MM-DD")
    if args.date and not args.backfill:
        raise SystemExit("--date 目前仅用于 --backfill，避免误改正常每日流程")

    cfg = load_config()
    for key in ("state_dir", "out_dir", "log_dir"):
        Path(cfg["paths"][key]).mkdir(parents=True, exist_ok=True)
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    target_day = parse_day(args.date) if args.backfill else datetime.now(BEIJING).date() - timedelta(days=1)
    target = target_day.strftime("%Y-%m-%d")
    log = setup_logging(cfg, today)
    out_dir = Path(cfg["paths"]["out_dir"])
    retention = cfg["retention_days"]
    started_at = datetime.now(BEIJING)
    phase = "initializing"
    write_run_status(cfg, {
        "status": "warning", "success": None, "phase": phase,
        "started_at": started_at.isoformat(timespec="seconds"), "content_date": target,
    })
    try:
        cve_payload = cve.load_cache(cfg)
        service_profiles = cve.load_service_profiles(cfg)
        cve_status = dict(cve_payload.get("status") or {})
        if not args.backfill:
            phase = "cve"
            log.info("=== CVE 多源采集开始 ===")
            try:
                fresh_cve = cve.collect(cfg, log, target_day)
                discovery_ok = set(fresh_cve["status"]["successful_sources"]) & cve.DISCOVERY_SOURCES
                if discovery_ok:
                    cve_payload = cve.merge_increment(
                        cve_payload, fresh_cve, target_day, cfg["cve"]["display_days"])
                    cve.save_cache(cfg, cve_payload)
                else:
                    cve_status = fresh_cve["status"]
                    cve_status["fallback"] = "all_discovery_sources_failed"
                    cve_payload["status"] = cve_status
                    log.warning("CVE 规范发现源全部失败，沿用上一版 CVE 数据")
            except cve.ProxyUnavailable as exc:
                cve_status.update({"proxy": "unavailable", "fallback": "proxy_unavailable", "error_category": type(exc).__name__})
                cve_payload["status"] = cve_status
                log.warning("%s；不直连来源，沿用上一版 CVE 数据", exc)
            except Exception as exc:
                cve_status.update({"fallback": "cve_stage_failed", "error_category": type(exc).__name__})
                cve_payload["status"] = cve_status
                log.exception("CVE 阶段失败，普通新闻继续并沿用上一版 CVE 数据")

        if not args.backfill:
            log.info("=== anime_calendar 刷新 ===")
            anime_calendar.refresh(cfg, today, log)

        seen = {} if args.backfill else load_seen(cfg)
        mode = f"backfill {target}" if args.backfill else "daily"
        log.info("=== collect 开始（模式 %s，已知 %d 个已发布 URL）===", mode, len(seen))
        phase = "collect"
        candidates, _ = asyncio.run(collect.collect(cfg, set(seen), log, target_day))
        (out_dir / f"candidates-{target}.json").write_text(
            json.dumps(candidates, ensure_ascii=False, indent=1), encoding="utf-8")
        if not candidates:
            raise RuntimeError("collect 阶段没有产出任何候选条目")

        phase = "summarize"
        log.info("=== summarize 开始 ===")
        existing_payload = {} if args.backfill else load_existing_payload(cfg, target, log)
        existing_items = existing_payload.get("items") or []
        items, digest, digests_by_category, router = asyncio.run(
            run_llm_stages(cfg, candidates, log, existing_items))
        if not args.backfill and cve_payload.get("records"):
            cve_payload = asyncio.run(cve.ai_aggregate_services(
                cve_payload, cfg["cve"], router, log, service_profiles))
            cve.save_cache(cfg, cve_payload)
            cve.save_service_profiles(cfg, service_profiles)
        log.info("summarize 用到的模型: %s；token 用量: %s",
                 "、".join(router.models_used), json.dumps(router.usage))

        phase = "images"
        log.info("=== images 开始 ===")
        images.fetch_images(cfg, items, target, log)

        (out_dir / f"items-{target}.json").write_text(
            json.dumps({
                "items": items,
                "digest": digest,
                "digests_by_category": digests_by_category,
                "models_used": router.models_used,
            },
                       ensure_ascii=False, indent=1), encoding="utf-8")

        phase = "publish"
        log.info("=== render + publish ===")
        html_path = render.render(cfg, items, digest, router.models_used, log, target, digests_by_category, cve_payload)
        publish(cfg, html_path, today, log)

        if args.backfill:
            log.info("backfill 模式跳过 seen.json 更新")
        else:
            for item in items:
                seen[item["url"]] = today
            save_seen(cfg, seen, log)

        prune_dated_files(cfg["paths"]["log_dir"], retention["logs"], log)
        prune_dated_files(cfg["paths"]["archive_dir"], retention["archive"], log)
        prune_dated_files(out_dir, retention["out"], log)
        images.prune_image_dirs(cfg, log)
        finished_at = datetime.now(BEIJING)
        category_counts = {}
        for item in items:
            category = item.get("category", "其他")
            category_counts[category] = category_counts.get(category, 0) + 1
        usage = dict(router.usage)
        usage["total_tokens"] = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        write_run_status(cfg, {
            "status": "healthy", "success": True, "phase": "complete",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round((finished_at - started_at).total_seconds()),
            "content_date": target, "item_count": len(items),
            "category_counts": category_counts, "models": router.models_used,
            "usage": usage, "failure_stage": None, "error_category": None,
            "cve": cve_payload.get("status", {}),
        })
        log.info("完成。token 用量: %s", json.dumps(router.usage))
    except Exception as exc:
        finished_at = datetime.now(BEIJING)
        write_run_status(cfg, {
            "status": "warning", "success": False, "phase": "failed",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round((finished_at - started_at).total_seconds()),
            "content_date": target, "failure_stage": phase,
            "error_category": type(exc).__name__,
        })
        log.exception("流水线失败，保留昨日页面。")
        sys.exit(1)


if __name__ == "__main__":
    main()
