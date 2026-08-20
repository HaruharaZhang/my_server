"""summarize 阶段：大模型批量生成条目中文摘要 + 当日「今日速览」总览。

单批摘要失败（含降级后仍失败）只保留该批条目的原始摘要，不整批丢弃；
digest 失败降级为分类分段兜底，仍失败则返回空串（页面不显示速览块）。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from llm import parse_json_reply

BEIJING = timezone(timedelta(hours=8))

SUMMARY_SYSTEM_PROMPT = """你是「东云资讯」的中文编辑。现在是北京时间 {now}。
用户给你一批资讯条目（含 index、title、hint、source、category）。请为每条写一个中文标题和一段话中文摘要：
中文标题 10–28 字，准确概括原始标题，不要使用夸张营销语。
2–3 句、约 60–120 字，讲清这条资讯是什么、有什么关键点。开源项目条目说明项目用途与亮点。
不得编造 hint 里没有的具体数字或事实；hint 信息太少时基于标题客观概括。
可选分类：{categories}。仅在条目现有分类明显错误时才给出修正后的 category，否则不返回该字段。

只输出一个 JSON 数组，不要任何其他文字，每个元素形如：
{{"index": 0, "title_zh": "……", "summary": "……", "category": "仅需修正时给出"}}"""

DIGEST_SYSTEM_PROMPT = """你是「东云资讯」的中文主编。现在是北京时间 {now}。
用户给你今天全部资讯条目的编号列表（序号、标题、摘要）。请写一段约 200–300 字的中文「今日速览」：
一段连贯的话，涵盖当天最重要的动态（大厂发布、硬件进展、明星开源项目等），让读者一眼看清今天发生了什么。
在提到具体条目的文字处，用 Markdown 链接标记指向该条目的序号，形如 [提及文字](#12)。
只使用列表中真实存在的序号，不要编造。只输出这一段文字，不要标题、列表或其他内容。"""

DEDUP_SYSTEM_PROMPT = """你是新闻去重助手。用户给你一组可能重复的资讯候选（含 index、title、source、url、category、hint）。
请保守判断哪些 index 指向同一个 URL、同一个 GitHub 仓库、同一个产品发布/服务公告/事件。
只有在明显是同一件事时才合并；不同媒体的不同分析角度、后续报道、相近主题、同一公司的不同公告都不要合并。

只输出 JSON 数组，每个元素是一个重复组，例如 [[1, 4], [8, 9, 12]]。没有重复时输出 []。"""

GITHUB_REPO_RE = re.compile(r"^https?://github\.com/([^/?#]+/[^/?#]+)/?$", re.I)
WORD_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+", re.I)


def batched(items, size):
    for start in range(0, len(items), size):
        yield start, items[start:start + size]


def normalize_url(url):
    parsed = urlsplit(str(url).strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+$", "", parsed.path)
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))


def github_repo_key(url):
    match = GITHUB_REPO_RE.match(str(url).strip())
    return match.group(1).lower() if match else ""


def normalize_title(title):
    words = WORD_RE.findall(str(title).lower())
    return " ".join(words)


def dedup_keys(item):
    keys = []
    url = normalize_url(item.get("url", ""))
    if url:
        keys.append(f"url:{url}")
    repo = github_repo_key(item.get("url", ""))
    if repo:
        keys.append(f"github:{repo}")
    title = normalize_title(item.get("title_zh") or item.get("title", ""))
    if len(title) >= 18:
        keys.append(f"title:{title}")
    return keys


def append_alternate(canonical, duplicate):
    alternates = canonical.setdefault("_alternate_sources", [])
    alternate = {
        "source": duplicate.get("source", ""),
        "url": duplicate.get("url", ""),
        "title": duplicate.get("title", ""),
    }
    if alternate not in alternates:
        alternates.append(alternate)


def merge_duplicate_group(items, duplicate_indexes, removed):
    indexes = sorted({idx for idx in duplicate_indexes if 0 <= idx < len(items)})
    active = [idx for idx in indexes if idx not in removed]
    if len(active) < 2:
        return 0
    canonical_index = active[0]
    canonical = items[canonical_index]
    count = 0
    for idx in active[1:]:
        append_alternate(canonical, items[idx])
        removed.add(idx)
        count += 1
    return count


def deterministic_dedup(items, log):
    key_to_index = {}
    removed = set()
    removed_count = 0
    for idx, item in enumerate(items):
        matched = None
        for key in dedup_keys(item):
            existing = key_to_index.get(key)
            if existing is not None and existing not in removed:
                matched = existing
                break
        if matched is None:
            for key in dedup_keys(item):
                key_to_index[key] = idx
            continue
        append_alternate(items[matched], item)
        removed.add(idx)
        removed_count += 1
    deduped = [item for idx, item in enumerate(items) if idx not in removed]
    log.info("确定性去重: 输入 %d 条，移除 %d 条，剩余 %d 条", len(items), removed_count, len(deduped))
    return deduped


def candidate_duplicate_groups(items):
    groups = []
    for left in range(len(items)):
        left_words = {word for word in WORD_RE.findall(str(items[left].get("title", "")).lower()) if len(word) >= 3}
        if not left_words:
            continue
        for right in range(left + 1, len(items)):
            if items[left].get("category") != items[right].get("category"):
                continue
            right_words = {word for word in WORD_RE.findall(str(items[right].get("title", "")).lower()) if len(word) >= 3}
            if not right_words:
                continue
            overlap = len(left_words & right_words) / max(len(left_words), len(right_words))
            left_title = normalize_title(items[left].get("title", ""))
            right_title = normalize_title(items[right].get("title", ""))
            same_title_family = left_title in right_title or right_title in left_title
            if overlap >= 0.6 or same_title_family:
                groups.append((left, right))
    return groups[:40]


async def ai_dedup(items, router, log):
    groups = candidate_duplicate_groups(items)
    if not groups:
        log.info("AI 去重: 没有可疑重复组")
        return items
    compact = []
    included = set()
    for group in groups:
        for idx in group:
            if idx in included:
                continue
            item = items[idx]
            compact.append({
                "index": idx,
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "category": item.get("category", ""),
                "hint": item.get("summary", "")[:120],
            })
            included.add(idx)
    messages = [
        {"role": "system", "content": DEDUP_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
    ]
    reply, model = await router.chat(messages)
    duplicate_groups = parse_json_reply(reply)
    removed = set()
    removed_count = 0
    if isinstance(duplicate_groups, list):
        for group in duplicate_groups:
            if not isinstance(group, list):
                continue
            try:
                indexes = [int(idx) for idx in group]
            except (TypeError, ValueError):
                continue
            removed_count += merge_duplicate_group(items, indexes, removed)
    deduped = [item for idx, item in enumerate(items) if idx not in removed]
    log.info("AI 去重: 模型 %s 检查 %d 个候选组，移除 %d 条，剩余 %d 条",
             model, len(groups), removed_count, len(deduped))
    return deduped


async def deduplicate_candidates(cfg, router, candidates, log):
    """summarize 前去重；AI 失败时保留确定性去重结果继续。"""
    del cfg
    deterministic = deterministic_dedup(candidates, log)
    try:
        return await ai_dedup(deterministic, router, log)
    except Exception as exc:
        log.warning("AI 去重失败，保留确定性去重结果继续: %s", exc)
        return deterministic


async def summarize_items(cfg, router, candidates, log):
    """为每条候选生成中文一段话摘要（就地更新 summary/category）。"""
    system = SUMMARY_SYSTEM_PROMPT.format(
        now=datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        categories="、".join(cfg["categories"]),
    )
    batch_size = cfg["limits"]["summary_batch_size"]
    done = 0
    for start, batch in batched(candidates, batch_size):
        payload = [
            {"index": start + i, "title": c["title"], "hint": c["summary"],
             "source": c["source"], "category": c["category"],
             "alternate_sources": c.get("_alternate_sources", [])[:3]}
            for i, c in enumerate(batch)
        ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            reply, model = await router.chat(messages)
            rows = parse_json_reply(reply)
        except Exception as exc:
            log.warning("摘要批次 %d-%d 失败，保留原始摘要: %s", start, start + len(batch) - 1, exc)
            continue
        applied = 0
        for row in rows if isinstance(rows, list) else []:
            try:
                idx = int(row["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (start <= idx < start + len(batch)):
                continue
            summary = str(row.get("summary", "")).strip()
            if not summary:
                continue
            candidates[idx]["summary"] = summary
            title_zh = str(row.get("title_zh", "")).strip()
            if title_zh:
                candidates[idx]["title_zh"] = title_zh
            category = str(row.get("category", "")).strip()
            if category in cfg["categories"]:
                candidates[idx]["category"] = category
            applied += 1
        done += applied
        log.info("摘要批次 %d-%d: 模型 %s 更新 %d/%d 条",
                 start, start + len(batch) - 1, model, applied, len(batch))
    log.info("摘要完成: 共更新 %d/%d 条", done, len(candidates))
    return candidates


def digest_item_lines(items):
    return "\n".join(f"{i}. [{item['category']}] {item['title']} —— {item['summary']}"
                     for i, item in enumerate(items))


async def run_digest(router, system, user_text):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]
    reply, model = await router.chat(messages)
    return reply.strip(), model


async def daily_digest(cfg, router, items, log):
    """生成一段中文总览；条目引用用 [文字](#序号) 标记，render 阶段转成安全链接。"""
    system = DIGEST_SYSTEM_PROMPT.format(now=datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"))
    try:
        digest, model = await run_digest(router, system, digest_item_lines(items))
        log.info("今日速览生成成功（模型 %s，%d 字）", model, len(digest))
        return digest
    except Exception as exc:
        log.warning("整体生成今日速览失败，改为按分类分段兜底: %s", exc)
    # 兜底：每个分类各生成一句，再拼成一段
    parts = []
    for category in cfg["categories"]:
        subset = [(i, item) for i, item in enumerate(items) if item["category"] == category]
        if not subset:
            continue
        lines = "\n".join(f"{i}. {item['title']} —— {item['summary']}" for i, item in subset)
        part_system = system + f"\n本次只给你「{category}」分类的条目，请只写 1–2 句话概括该分类。"
        try:
            part, model = await run_digest(router, part_system, lines)
            parts.append(part)
        except Exception as exc:
            log.warning("分类 %s 速览生成失败，跳过: %s", category, exc)
    digest = " ".join(parts)
    if digest:
        log.info("今日速览分段兜底完成（%d 字）", len(digest))
    else:
        log.warning("今日速览全部失败，页面将不显示速览块")
    return digest


async def daily_digests_by_category(cfg, router, items, log):
    """按分类生成速览；失败的分类返回空串，页面可只展示条目。"""
    digests = {}
    system = DIGEST_SYSTEM_PROMPT.format(now=datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"))
    for category in cfg["categories"]:
        subset = [(i, item) for i, item in enumerate(items) if item["category"] == category]
        if not subset:
            digests[category] = ""
            continue
        lines = "\n".join(f"{i}. {item['title']} —— {item['summary']}" for i, item in subset)
        category_system = (
            system
            + f"\n本次只给你「{category}」分类的条目。请写 120–220 字的该分类速览，仍使用 [文字](#序号) 引用条目。"
        )
        try:
            digest, model = await run_digest(router, category_system, lines)
        except Exception as exc:
            log.warning("分类 %s 速览生成失败: %s", category, exc)
            digests[category] = ""
            continue
        digests[category] = digest
        log.info("分类 %s 速览生成成功（模型 %s，%d 字）", category, model, len(digest))
    return digests
