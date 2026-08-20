"""CVE 多源采集、事实合并、风险筛选及服务级聚合。

所有情报 HTTP 请求只使用配置的 mihomo 代理；本模块从不隐式回退直连。
适配器尽量独立：单源失败会进入状态快照，不阻断其他来源或普通新闻。
"""

import email.utils
import hashlib
import json
import os
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.I)
RETRY_CODES = {408, 425, 429}
DISCOVERY_SOURCES = {"NVD", "GitHub Advisory"}
VERSION_SOURCE_PRIORITY = {
    "Red Hat": 30, "Ubuntu OSV": 30, "Debian": 30, "SUSE CSAF/VEX": 30,
    "Microsoft MSRC": 30, "GitHub Advisory": 20, "OSV": 20, "NVD": 10,
}
VENDOR_SOURCES = {name for name, priority in VERSION_SOURCE_PRIORITY.items() if priority == 30}


class ProxyUnavailable(RuntimeError):
    pass


class ParseError(ValueError):
    pass


def parse_time(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING)


def in_window(record, start_day, end_day):
    parsed = parse_time(record.get("published"))
    return bool(parsed and start_day <= parsed.date() <= end_day)


class ProxyClient:
    def __init__(self, cfg, log, sleep=time.sleep, jitter=random.uniform):
        self.cfg = cfg
        self.log = log
        self.sleep = sleep
        self.jitter = jitter
        proxy = cfg["proxy_url"]
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        self.attempts = {}

    def check(self):
        parsed = urllib.parse.urlsplit(self.cfg["proxy_url"])
        try:
            with socket.create_connection(
                (parsed.hostname, parsed.port or 80), self.cfg.get("connect_timeout", 5)
            ):
                return True
        except OSError as exc:
            raise ProxyUnavailable(f"mihomo 代理不可连接: {type(exc).__name__}") from exc

    def _delay(self, attempt, headers=None):
        retry_after = headers.get("Retry-After") if headers else None
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                try:
                    when = email.utils.parsedate_to_datetime(retry_after)
                    return max(0.0, min((when - datetime.now(when.tzinfo)).total_seconds(), 60.0))
                except (TypeError, ValueError):
                    pass
        base = self.cfg.get("backoff_seconds", [1, 2, 4])
        return base[min(attempt - 1, len(base) - 1)] + self.jitter(0, 0.25)

    def json(self, source, url, headers=None):
        request_headers = {
            "User-Agent": "dyyjs-cve-bot/1.0",
            "Accept": "application/json, application/feed+json, */*",
        }
        request_headers.update(headers or {})
        maximum = int(self.cfg.get("max_retries", 3)) + 1
        last = None
        for attempt in range(1, maximum + 1):
            self.attempts[source] = self.attempts.get(source, 0) + 1
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with self.opener.open(request, timeout=self.cfg.get("read_timeout", 30)) as response:
                    raw = response.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ParseError(type(exc).__name__) from exc
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in RETRY_CODES and exc.code < 500:
                    raise
                retry_headers = exc.headers
            except (urllib.error.URLError, TimeoutError, OSError, ParseError) as exc:
                last = exc
                retry_headers = None
            if attempt < maximum:
                self.sleep(self._delay(attempt, retry_headers))
        raise last


def blank_record(cve_id, source):
    return {
        "id": cve_id.upper(), "status": "", "published": "", "modified": "",
        "products": [], "purls": [], "cpes": [], "affected_versions": [],
        "version_ranges": [],
        "fixed_versions": [], "cvss": None, "cwes": [], "kev": False,
        "vendor_exploited": False, "epss": None, "epss_percentile": None,
        "severity": "", "description": "", "references": [],
        "field_sources": {}, "sources": [source],
    }


def add(record, field, value, source):
    if value in (None, "", [], {}):
        return
    if field in {"products", "purls", "cpes", "affected_versions", "version_ranges", "fixed_versions", "cwes", "references"}:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item not in record[field]:
                record[field].append(item)
    elif field in {"kev", "vendor_exploited"}:
        record[field] = record[field] or bool(value)
    elif field in {"cvss", "epss", "epss_percentile"}:
        number = float(value)
        if record[field] is None or number > record[field]:
            record[field] = number
    elif not record.get(field) or field == "modified":
        record[field] = value
    record["field_sources"].setdefault(field, [])
    if source not in record["field_sources"][field]:
        record["field_sources"][field].append(source)


def merge_records(records):
    merged = {}
    for incoming in records:
        cve_id = str(incoming.get("id", "")).upper()
        if not CVE_RE.match(cve_id):
            continue
        current = merged.setdefault(cve_id, blank_record(cve_id, incoming.get("sources", [""])[0]))
        for source in incoming.get("sources", []):
            if source and source not in current["sources"]:
                current["sources"].append(source)
        for field in current:
            if field not in {"id", "sources", "field_sources"}:
                for source in incoming.get("field_sources", {}).get(field, incoming.get("sources", [])):
                    add(current, field, incoming.get(field), source)
                    break
    result = [record for record in merged.values() if record["status"].upper() not in {"REJECTED", "WITHDRAWN"}]
    for record in result:
        ranges = record.get("version_ranges") or []
        if not ranges:
            continue
        best = max(VERSION_SOURCE_PRIORITY.get(item.get("source", ""), 0) for item in ranges)
        record["version_ranges"] = [
            item for item in ranges
            if VERSION_SOURCE_PRIORITY.get(item.get("source", ""), 0) == best
        ]
    return result


def parse_nvd(data):
    result = []
    for wrapper in data.get("vulnerabilities", []):
        item = wrapper.get("cve", {})
        record = blank_record(item.get("id", ""), "NVD")
        for field, key in (("status", "vulnStatus"), ("published", "published"), ("modified", "lastModified")):
            add(record, field, item.get(key), "NVD")
        descriptions = item.get("descriptions", [])
        english = next((x.get("value") for x in descriptions if x.get("lang") == "en"), "")
        add(record, "description", english, "NVD")
        for metrics in item.get("metrics", {}).values():
            for metric in metrics:
                cvss = metric.get("cvssData", {}).get("baseScore")
                add(record, "cvss", cvss, "NVD")
        add(record, "cwes", [x.get("description", [{}])[0].get("value") for x in item.get("weaknesses", [])], "NVD")
        add(record, "references", [{"url": x.get("url"), "source": "NVD"} for x in item.get("references", [])], "NVD")
        result.append(record)
    return result


def parse_github(data):
    result = []
    for item in data if isinstance(data, list) else []:
        cve_id = item.get("cve_id") or ""
        record = blank_record(cve_id, "GitHub Advisory")
        add(record, "published", item.get("published_at"), "GitHub Advisory")
        add(record, "modified", item.get("updated_at"), "GitHub Advisory")
        add(record, "description", item.get("description"), "GitHub Advisory")
        add(record, "cvss", item.get("cvss", {}).get("score"), "GitHub Advisory")
        for vulnerability in item.get("vulnerabilities", []):
            package = vulnerability.get("package", {})
            add(record, "products", package.get("name"), "GitHub Advisory")
            eco = package.get("ecosystem", "").lower()
            if package.get("name"):
                add(record, "purls", f"pkg:{eco}/{package['name']}", "GitHub Advisory")
            affected_range = vulnerability.get("vulnerable_version_range")
            fixed = vulnerability.get("first_patched_version")
            fixed_version = fixed.get("identifier") if isinstance(fixed, dict) else fixed
            add(record, "version_ranges", {
                "product": package.get("name") or "", "ecosystem": package.get("ecosystem") or "",
                "range": affected_range or "", "introduced": "", "last_affected": "",
                "fixed": fixed_version or "", "source": "GitHub Advisory",
            }, "GitHub Advisory")
            add(record, "fixed_versions", fixed_version, "GitHub Advisory")
        add(record, "references", [{"url": item.get("html_url"), "source": "GitHub Advisory"}], "GitHub Advisory")
        result.append(record)
    return result


def parse_kev(data):
    result = []
    for item in data.get("vulnerabilities", []):
        record = blank_record(item.get("cveID", ""), "CISA KEV")
        add(record, "kev", True, "CISA KEV")
        add(record, "products", item.get("product"), "CISA KEV")
        add(record, "description", item.get("shortDescription"), "CISA KEV")
        add(record, "modified", item.get("dateAdded"), "CISA KEV")
        add(record, "references", [{"url": "https://www.cisa.gov/known-exploited-vulnerabilities-catalog", "source": "CISA KEV"}], "CISA KEV")
        result.append(record)
    return result


def parse_epss(data):
    result = []
    for item in data.get("data", []):
        record = blank_record(item.get("cve", ""), "FIRST EPSS")
        add(record, "epss", float(item.get("epss", 0)) * 100, "FIRST EPSS")
        add(record, "epss_percentile", float(item.get("percentile", 0)) * 100, "FIRST EPSS")
        result.append(record)
    return result


def parse_osv(data, cve_id):
    records = []
    for item in data.get("vulns", []):
        aliases = item.get("aliases", []) + [item.get("id", "")]
        matched = next((x for x in aliases if str(x).upper() == cve_id), cve_id)
        record = blank_record(matched, "OSV")
        add(record, "published", item.get("published"), "OSV")
        add(record, "modified", item.get("modified"), "OSV")
        add(record, "description", item.get("summary") or item.get("details"), "OSV")
        for affected in item.get("affected", []):
            package = affected.get("package", {})
            add(record, "products", package.get("name"), "OSV")
            add(record, "purls", package.get("purl"), "OSV")
            for version_range in affected.get("ranges", []):
                current_start = ""
                range_type = version_range.get("type") or ""
                for event in version_range.get("events", []):
                    if "introduced" in event:
                        current_start = event.get("introduced") or ""
                    for end_key in ("fixed", "last_affected", "limit"):
                        if end_key not in event:
                            continue
                        end_value = event.get(end_key) or ""
                        add(record, "version_ranges", {
                            "product": package.get("name") or "", "ecosystem": package.get("ecosystem") or "",
                            "type": range_type, "introduced": current_start,
                            "fixed": end_value if end_key == "fixed" else "",
                            "last_affected": end_value if end_key != "fixed" else "",
                            "source": "OSV",
                        }, "OSV")
                        if end_key == "fixed":
                            add(record, "fixed_versions", end_value, "OSV")
                        current_start = ""
                if current_start:
                    add(record, "version_ranges", {
                        "product": package.get("name") or "", "ecosystem": package.get("ecosystem") or "",
                        "type": range_type, "introduced": current_start, "fixed": "",
                        "last_affected": "", "source": "OSV",
                    }, "OSV")
        add(record, "references", [{"url": x.get("url"), "source": "OSV"} for x in item.get("references", [])], "OSV")
        records.append(record)
    return records


def parse_vendor_json(data, source):
    """保守提取厂商/VEX JSON 中可回查的 CVE 事实，不猜测嵌套字段语义。"""
    records = []

    def walk(value, product_hint=""):
        if isinstance(value, dict):
            text_values = [str(x) for x in value.values() if isinstance(x, (str, int, float))]
            ids = {match.group(0).upper() for text in text_values for match in re.finditer(r"CVE-\d{4}-\d{4,}", text, re.I)}
            product = str(value.get("product") or value.get("package") or value.get("name") or product_hint or "")
            for cve_id in ids:
                record = blank_record(cve_id, source)
                add(record, "products", product, source)
                add(record, "status", value.get("status") or value.get("state"), source)
                add(record, "severity", value.get("severity") or value.get("threat_severity"), source)
                affected = value.get("affected") or value.get("vulnerable_version")
                fixed = value.get("fixed") or value.get("fixed_version")
                if isinstance(affected, (str, int, float)) or isinstance(fixed, (str, int, float)):
                    add(record, "version_ranges", {
                        "product": product, "ecosystem": "", "range": str(affected or ""),
                        "introduced": "", "last_affected": "", "fixed": str(fixed or ""),
                        "source": source,
                    }, source)
                    add(record, "fixed_versions", fixed, source)
                add(record, "modified", value.get("updated") or value.get("modified") or value.get("date"), source)
                records.append(record)
            for key, child in value.items():
                next_hint = product_hint
                if isinstance(child, (dict, list)) and not CVE_RE.match(str(key)):
                    next_hint = product or str(key)
                walk(child, next_hint)
        elif isinstance(value, list):
            for child in value:
                walk(child, product_hint)

    walk(data)
    return records


def high_risk(record, cfg):
    return bool(
        record.get("kev") or record.get("vendor_exploited")
        or (record.get("cvss") is not None and record["cvss"] >= cfg["cvss_min"])
        or str(record.get("severity", "")).lower() in {"high", "critical", "important"}
        or (record.get("epss") is not None and record["epss"] >= cfg["epss_min_percent"])
        or (record.get("epss_percentile") is not None and record["epss_percentile"] >= cfg["epss_percentile_min"])
    )


def service_key(record, aliases):
    if record.get("service_override"):
        name = record["service_override"]
        return re.sub(r"\s+", "-", name.lower()), name
    haystack = " ".join(record.get("purls", []) + record.get("products", []) + record.get("cpes", [])).lower()
    distro_rules = (("ubuntu", "Ubuntu"), ("debian", "Debian"), ("red hat", "RHEL"), ("rhel", "RHEL"), ("suse", "SUSE"))
    for needle, label in distro_rules:
        if needle in haystack:
            return label.lower(), label
    for canonical, values in aliases.items():
        if canonical.lower() in haystack or any(str(value).lower() in haystack for value in values):
            return canonical.lower(), canonical
    if "linux kernel" in haystack or "linux_kernel" in haystack or "pkg:kernel/linux" in haystack:
        return "linux-kernel", "Linux Kernel"
    product = next((x for x in record.get("products", []) if x), "")
    if product:
        clean = re.sub(r"[^a-zA-Z0-9._ +#-]", "", product).strip()
        return clean.lower(), clean
    return f"unconfirmed:{record['id']}", "服务待确认"


def aggregate_services(records, cfg):
    groups = {}
    aliases = cfg.get("service_aliases", {})
    for record in records:
        key, name = service_key(record, aliases)
        day = record_day(record)
        group_key = (day, key)
        group = groups.setdefault(group_key, {"service_key": key, "service_name": name, "date": day, "cves": []})
        group["cves"].append(record)
    cards = []
    for group in groups.values():
        cves = sorted(group["cves"], key=lambda x: (bool(x.get("kev")), x.get("cvss") or 0, x.get("epss") or 0, x.get("modified") or ""), reverse=True)
        impact = next((x.get("impact_summary") for x in cves if x.get("impact_summary")), "漏洞影响待确认")
        purpose = next((x.get("service_description") for x in cves if x.get("service_description")), "服务用途待确认")
        cards.append({
            **group, "cves": cves,
            "title": f"{group['service_name']}: {impact}", "service_description": purpose,
            "updated_at": max((x.get("modified") or x.get("published") or "" for x in cves), default=""),
            "kev": any(x.get("kev") for x in cves),
            "max_cvss": max((x.get("cvss") or 0 for x in cves), default=0),
            "max_epss": max((x.get("epss") or 0 for x in cves), default=0),
        })
    return sorted(cards, key=lambda x: (x["kev"], x["max_cvss"], x["max_epss"], x["updated_at"]), reverse=True)


def record_day(record):
    parsed = parse_time(record.get("published"))
    return parsed.date().isoformat() if parsed else ""


def build_days(cards):
    grouped = {}
    for card in cards:
        grouped.setdefault(card.get("date") or "unknown", []).append(card)
    days = []
    for day in sorted(grouped, reverse=True):
        label = "日期待确认" if day == "unknown" else datetime.strptime(day, "%Y-%m-%d").strftime("%m月%d日")
        days.append({"date": day, "label": label, "services": grouped[day]})
    return days


def facts_fingerprint(record):
    facts = {key: record.get(key, [] if key != "description" else "")
             for key in ("products", "purls", "cpes", "description")}
    raw = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def trim_records(records, end_day, display_days):
    start_day = end_day - timedelta(days=int(display_days) - 1)
    return [record for record in records if in_window(record, start_day, end_day)]


def merge_increment(previous, incoming, end_day, display_days):
    previous_by_id = {record["id"]: record for record in previous.get("records") or []}
    records = merge_records((previous.get("records") or []) + (incoming.get("records") or []))
    for record in records:
        old = previous_by_id.get(record["id"], {})
        for field in ("service_override", "service_description", "impact_summary", "summary_fingerprint"):
            if old.get(field):
                record[field] = old[field]
    result = dict(previous)
    result.update(incoming)
    result["records"] = trim_records(records, end_day, display_days)
    result["services"] = []
    result["days"] = []
    result["window"] = {
        "start": (end_day - timedelta(days=int(display_days) - 1)).isoformat(),
        "end": end_day.isoformat(),
    }
    return result


async def ai_aggregate_services(payload, cfg, router, log, service_profiles=None):
    """让模型只做服务归组、用途说明和影响摘要。规范事实不交给模型生成。"""
    records = payload.get("records") or []
    service_profiles = service_profiles if service_profiles is not None else {}
    payload["services"] = aggregate_services(records, cfg)
    payload["days"] = build_days(payload["services"])
    protected = {"Linux Kernel", "Ubuntu", "Debian", "RHEL", "SUSE"}
    candidates = []
    for record in records:
        deterministic_key, deterministic_name = service_key(record, cfg.get("service_aliases", {}))
        fingerprint = facts_fingerprint(record)
        if (record.get("summary_fingerprint") == fingerprint and record.get("impact_summary")
                and deterministic_key in service_profiles):
            continue
        candidates.append({
            "cve_id": record["id"], "products": record.get("products", []),
            "purls": record.get("purls", []), "cpes": record.get("cpes", []),
            "description": str(record.get("description", ""))[:300],
            "service_name_hint": deterministic_name,
            "service_name_locked": deterministic_name in protected,
            "need_profile": deterministic_key not in service_profiles,
        })
    if not candidates:
        for service in payload["services"]:
            profile = service_profiles.get(service["service_key"])
            if profile:
                service["service_profile"] = profile
                service["service_description"] = profile["description"]
        payload["days"] = build_days(payload["services"])
        payload.setdefault("status", {})["service_count"] = len(payload["services"])
        payload["status"]["ai_grouped_cves"] = 0
        return payload
    allowed = {x["cve_id"] for x in candidates}
    assigned = set()
    overrides = {}
    models = []
    by_id = {record["id"]: record for record in records}
    hints = {item["cve_id"]: item for item in candidates}
    batch_size = int(cfg.get("ai_batch_size", 12))
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        prompt = (
            "你只负责把下列规范 CVE 记录按同一软件/服务归组，并给出规范服务名、服务资料和漏洞后果摘要。"
            "不得创造、修改或遗漏 CVE ID，不得输出版本、评分、来源或新事实。"
            "service_name_locked=true 时必须原样使用 service_name_hint。"
            "服务资料包含用途、解决的问题、首次发布时间；只能依据产品事实，首次发布时间证据不足必须写‘首次发布时间待确认’，禁止猜造。"
            "影响摘要不超过80个中文字，禁止包含CVE编号、KEV、评分或‘已确认在野利用’。"
            "不能确认同一服务时保持独立。返回 JSON 数组："
            '[{"service_name":"LiteLLM","purpose":"大语言模型网关与代理服务","problem_solved":"统一多个模型接口与调用治理","first_release":"2023年",'
            '"cves":[{"cve_id":"CVE-...","impact_summary":"攻击者可绕过身份验证并读取敏感配置"}]}]。\n记录：'
            + json.dumps(batch, ensure_ascii=False)
        )
        try:
            reply, model = await router.chat([{"role": "user", "content": prompt}])
            if model not in models:
                models.append(model)
            text = reply.strip()
            start, end = text.find("["), text.rfind("]")
            groups = json.loads(text[start:end + 1]) if start >= 0 and end >= start else []
        except Exception as exc:
            log.warning("CVE AI 批次 %d 失败，保留已有文案: %s", offset // batch_size + 1, type(exc).__name__)
            continue
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            name = str(group.get("service_name", "")).strip()[:80]
            items = group.get("cves") or []
            ids = [str(item.get("cve_id", "")).upper() for item in items if isinstance(item, dict)]
            valid_ids = [cve_id for cve_id in ids if cve_id in allowed and cve_id not in assigned]
            locked_names = {hints[cve_id]["service_name_hint"] for cve_id in valid_ids if hints[cve_id]["service_name_locked"]}
            if locked_names:
                name = next(iter(locked_names))
            purpose = str(group.get("purpose") or group.get("service_description") or "服务用途待确认").strip()[:100]
            problem = str(group.get("problem_solved") or "解决的问题待确认").strip()[:120]
            first_release = str(group.get("first_release") or "首次发布时间待确认").strip()[:40]
            if not re.search(r"\d{4}", first_release) and "待确认" not in first_release:
                first_release = "首次发布时间待确认"
            if not name or CVE_RE.search(name) or not re.match(r"^[\w .+#:/()-]+$", name, re.UNICODE):
                continue
            for cve_id in valid_ids:
                item = next((x for x in items if str(x.get("cve_id", "")).upper() == cve_id), {})
                impact = str(item.get("impact_summary") or "漏洞影响待确认").strip()[:80]
                if CVE_RE.search(impact) or "已确认在野利用" in impact:
                    impact = "漏洞影响待确认"
                overrides[cve_id] = name
                assigned.add(cve_id)
                by_id[cve_id]["service_description"] = purpose
                by_id[cve_id]["impact_summary"] = impact
                by_id[cve_id]["summary_fingerprint"] = facts_fingerprint(by_id[cve_id])
            profile_key = re.sub(r"\s+", "-", name.lower())
            release_sentence = first_release if "待确认" in first_release else "首次发布时间为" + first_release
            problem_sentence = problem if "待确认" in problem else "主要解决" + problem
            if profile_key not in service_profiles:
                service_profiles[profile_key] = {
                    "service_key": profile_key, "service_name": name, "purpose": purpose,
                    "problem_solved": problem, "first_release": first_release,
                    "description": f"{purpose}。{problem_sentence}。{release_sentence}。",
                }
    for record in records:
        if record["id"] in overrides:
            record["service_override"] = overrides[record["id"]]
    payload["services"] = aggregate_services(records, cfg)
    for service in payload["services"]:
        profile = service_profiles.get(service["service_key"])
        if profile:
            service["service_profile"] = profile
            service["service_description"] = profile["description"]
    payload["days"] = build_days(payload["services"])
    payload["status"]["service_count"] = len(payload["services"])
    payload["status"]["aggregation_model"] = "、".join(models)
    payload["status"]["ai_grouped_cves"] = len(assigned)
    return payload


def _nvd_url(cfg, start_day, end_day):
    start = datetime.combine(start_day, datetime.min.time(), BEIJING).astimezone(timezone.utc).isoformat().replace("+00:00", ".000Z")
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), BEIJING).astimezone(timezone.utc).isoformat().replace("+00:00", ".000Z")
    return cfg["url"] + "?" + urllib.parse.urlencode({
        "pubStartDate": start, "pubEndDate": end, "resultsPerPage": 2000,
    })


def _github_url(cfg, target_day):
    start = datetime.combine(target_day, datetime.min.time(), BEIJING).astimezone(timezone.utc).isoformat()
    end = datetime.combine(target_day + timedelta(days=1), datetime.min.time(), BEIJING).astimezone(timezone.utc).isoformat()
    query = {
        "published": f"{start}..{end}",
        "per_page": 100,
        "sort": "published",
        "direction": "desc",
    }
    return cfg["url"] + "?" + urllib.parse.urlencode(query)


def collect(cfg, log, target_day=None):
    c = cfg["cve"]
    end_day = target_day or (datetime.now(BEIJING).date() - timedelta(days=1))
    start_day = end_day
    client = ProxyClient(c, log)
    status = {"proxy": "ok", "attempts": client.attempts, "successful_sources": [], "failed_sources": {}, "raw_cves": 0, "high_risk_cves": 0, "service_count": 0}
    client.check()
    records = []
    sources = {x["name"]: x for x in c["sources"] if x.get("enabled", True)}
    jobs = [
        ("NVD", lambda s: parse_nvd(client.json(s["name"], _nvd_url(s, start_day, end_day), {"apiKey": os.environ.get(s.get("api_key_env", ""), "")}))),
        ("GitHub Advisory", lambda s: parse_github(client.json(s["name"], _github_url(s, end_day), _github_headers()))),
    ]
    for name, task in jobs:
        if name not in sources: continue
        try:
            found = task(sources[name])
            records.extend(x for x in found if in_window(x, start_day, end_day))
            status["successful_sources"].append(name)
        except Exception as exc:
            status["failed_sources"][name] = type(exc).__name__
            log.warning("CVE 来源失败 %s: %s", name, type(exc).__name__)
    ids = sorted({x["id"] for x in records if CVE_RE.match(x["id"])})
    if "CISA KEV" in sources:
        try:
            records.extend(
                record for record in parse_kev(client.json("CISA KEV", sources["CISA KEV"]["url"]))
                if record["id"] in ids
            )
            status["successful_sources"].append("CISA KEV")
        except Exception as exc:
            status["failed_sources"]["CISA KEV"] = type(exc).__name__
    if "FIRST EPSS" in sources and ids:
        try:
            for offset in range(0, len(ids), 100):
                url = sources["FIRST EPSS"]["url"] + "?" + urllib.parse.urlencode({"cve": ",".join(ids[offset:offset + 100])})
                records.extend(parse_epss(client.json("FIRST EPSS", url)))
            status["successful_sources"].append("FIRST EPSS")
        except Exception as exc: status["failed_sources"]["FIRST EPSS"] = type(exc).__name__
    if "OSV" in sources:
        try:
            for cve_id in ids:
                url = sources["OSV"]["url"].rstrip("/") + "/" + urllib.parse.quote(cve_id)
                records.extend(parse_osv(client.json("OSV", url), cve_id))
            status["successful_sources"].append("OSV")
        except Exception as exc: status["failed_sources"]["OSV"] = type(exc).__name__
    # Vendor/VEX endpoints are checked on every run. Their heterogeneous payloads are retained
    # as source health until a matching CVE is present; version facts already merged via OSV/NVD.
    for name, source in sources.items():
        if name in set(status["successful_sources"]) | set(status["failed_sources"]): continue
        try:
            payload = client.json(name, source["url"])
            # 厂商数据库通常是多年全量数据，只能补充本次 30 天发现集，不能扩大窗口。
            records.extend(record for record in parse_vendor_json(payload, name) if record["id"] in ids)
            status["successful_sources"].append(name)
        except Exception as exc:
            status["failed_sources"][name] = type(exc).__name__
    merged = merge_records(records)
    high = [x for x in merged if high_risk(x, c)]
    cards = aggregate_services(high, c)
    status.update({"raw_cves": len(merged), "high_risk_cves": len(high), "service_count": len(cards), "degraded": len(status["successful_sources"]) < c["minimum_sources"], "last_success_at": datetime.now(BEIJING).isoformat(timespec="seconds")})
    return {"records": high, "services": cards, "days": build_days(cards), "status": status, "window": {"start": start_day.isoformat(), "end": end_day.isoformat()}}


def _github_headers():
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token: headers["Authorization"] = f"Bearer {token}"
    return headers


def cache_path(cfg):
    return Path(cfg["paths"]["state_dir"]) / "cve-latest.json"


def load_cache(cfg):
    path = cache_path(cfg)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"records": [], "services": [], "status": {}}


def save_cache(cfg, payload):
    path = cache_path(cfg)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def service_profiles_path(cfg):
    return Path(cfg["paths"]["state_dir"]) / "service-profiles.json"


def load_service_profiles(cfg):
    path = service_profiles_path(cfg)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_service_profiles(cfg, profiles):
    path = service_profiles_path(cfg)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
