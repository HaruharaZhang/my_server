#!/usr/bin/env python3
"""Generate a sanitized, static operational snapshot. Standard library only."""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
VALID = {"healthy", "warning", "critical", "unknown"}
PRIVATE = re.compile(r"(?i)(https?://\S+|(?:\d{1,3}\.){3}\d{1,3}|/[\w./-]+|token\S*|[A-Za-z0-9_-]{11})")


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def systemd_time(value):
    """Convert systemd's locale-like timestamp to browser-safe ISO 8601."""
    if not value or value in ("n/a", "0"):
        return None
    try:
        parsed = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z")
        return parsed.replace(tzinfo=UTC).isoformat(timespec="seconds")
    except ValueError:
        return None


def run(args, timeout=3):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 127, "", ""


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    json.loads(payload)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value
    except (OSError, ValueError):
        return default


def status(value, observed=None, detail=""):
    return {"status": value if value in VALID else "unknown", "detail": detail, "observed_at": observed or now_iso()}


def percent(used, total):
    return round(used * 100 / total, 1) if total else 0.0


def read_cpu(previous):
    fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    values = [int(x) for x in fields]
    current = {"total": sum(values), "idle": values[3] + (values[4] if len(values) > 4 else 0)}
    old = previous.get("cpu_counters", {})
    total_delta = current["total"] - old.get("total", current["total"])
    idle_delta = current["idle"] - old.get("idle", current["idle"])
    if total_delta <= 0 or idle_delta < 0:
        cpu = None
    else:
        cpu = round(max(0, min(100, (1 - idle_delta / total_delta) * 100)), 1)
    return cpu, current


def memory():
    data = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        data[key] = int(value.strip().split()[0]) * 1024
    mem_used = data["MemTotal"] - data.get("MemAvailable", data.get("MemFree", 0))
    swap_used = data.get("SwapTotal", 0) - data.get("SwapFree", 0)
    return percent(mem_used, data["MemTotal"]), percent(swap_used, data.get("SwapTotal", 0))


def system_metrics(previous):
    cpu, cpu_counters = read_cpu(previous); mem, swap = memory(); disk = shutil.disk_usage("/")
    load = os.getloadavg(); uptime = int(float(Path("/proc/uptime").read_text().split()[0]))
    disk_pct = percent(disk.used, disk.total)
    levels = ["healthy"]
    previous_cpu = previous.get("last_cpu")
    if cpu is not None and previous_cpu is not None:
        levels.append("critical" if cpu >= 95 and previous_cpu >= 95 else
                      "warning" if cpu >= 80 and previous_cpu >= 80 else "healthy")
    levels.append("critical" if mem >= 92 else "warning" if mem >= 80 else "healthy")
    levels.append("critical" if disk_pct >= 90 else "warning" if disk_pct >= 75 else "healthy")
    if swap >= 25 or load[2] > (os.cpu_count() or 1): levels.append("warning")
    rank = {"healthy": 0, "warning": 1, "critical": 2}
    return {
        "status": max(levels, key=rank.get), "cpu_percent": cpu, "memory_percent": mem,
        "swap_percent": swap, "disk_percent": disk_pct, "load": [round(x, 2) for x in load],
        "uptime_seconds": uptime, "observed_at": now_iso(),
        "cpu_counters": cpu_counters,
    }


def sampler_info(path, max_age=15):
    sample = load_json(path, {})
    age = time.time() - sample.get("sampled_epoch", 0)
    valid = (0 <= age <= max_age and sample.get("sample_count", 0) > 0
             and isinstance(sample.get("rx_peak_bps"), (int, float))
             and isinstance(sample.get("tx_peak_bps"), (int, float)))
    return {
        "status": "healthy" if valid else "warning",
        "sample_count": sample.get("sample_count", 0) if valid else 0,
        "rx_peak_bps": sample.get("rx_peak_bps") if valid else None,
        "tx_peak_bps": sample.get("tx_peak_bps") if valid else None,
        "observed_at": now_iso(),
    }


def unit_info(unit, friendly, core=False):
    props = "ActiveState,SubState,ActiveEnterTimestamp,InactiveEnterTimestamp,ExecMainStartTimestamp,Result"
    rc, out, _ = run(["systemctl", "show", unit, *[f"--property={item}" for item in props.split(",")]])
    values = {}
    if rc == 0:
        for line in out.splitlines():
            if "=" in line:
                key, value = line.split("=", 1); values[key] = value
    active = values.get("ActiveState") == "active"
    state = "healthy" if active else "critical" if core else "warning"
    return {"name": friendly, "status": state, "running": active,
            "since": systemd_time(values.get("ActiveEnterTimestamp")),
            "last_restart": systemd_time(values.get("ExecMainStartTimestamp")),
            "last_result": values.get("Result") or "unknown", "observed_at": now_iso()}


def timer_info(unit, friendly):
    rc, out, _ = run(["systemctl", "show", unit, "--property=LastTriggerUSec", "--property=NextElapseUSecRealtime"])
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if rc == 0 else {}
    service = unit.replace(".timer", ".service")
    _, result, _ = run(["systemctl", "show", service, "--property=Result", "--value"])
    return {"name": friendly, "status": "healthy" if rc == 0 and result in ("success", "") else "warning",
            "last_run": systemd_time(values.get("LastTriggerUSec")),
            "next_run": systemd_time(values.get("NextElapseUSecRealtime")),
            "last_result": result or "unknown", "observed_at": now_iso()}


def domain_info(name):
    observed = now_iso(); dns_ok = False; code = None; latency = None; cert = {}
    dns_rc, _, _ = run(["getent", "ahosts", name], timeout=2)
    dns_ok = dns_rc == 0
    try:
        start = time.monotonic()
        req = urllib.request.Request(f"https://{name}/", method="HEAD", headers={"User-Agent": "dyyjs-status/1"})
        with urllib.request.urlopen(req, timeout=3) as response: code = response.status
        latency = round((time.monotonic() - start) * 1000)
    except Exception:
        pass
    try:
        context = ssl.create_default_context()
        with socket.create_connection((name, 443), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname=name) as tls:
                peer = tls.getpeercert()
        expiry = ssl.cert_time_to_seconds(peer["notAfter"])
        issuer = dict(x[0] for x in peer.get("issuer", []))
        days = int((expiry - time.time()) / 86400)
        cert = {"issuer": issuer.get("organizationName", "公开 CA"), "expires_at": datetime.fromtimestamp(expiry, UTC).isoformat(), "days_remaining": days}
    except Exception:
        days = -1
    state = "healthy" if dns_ok and code and code < 500 and days > 21 else "critical" if not dns_ok or not code or days < 7 else "warning"
    return {"name": name, "status": state, "dns": dns_ok, "https_code": code, "latency_ms": latency,
            "certificate": cert, "observed_at": observed}


def news_info():
    snap = load_json("/opt/dyyjs-news/state/status.json", {})
    if snap:
        snap["observed_at"] = snap.get("finished_at") or snap.get("started_at")
        return snap
    files = sorted(Path("/opt/dyyjs-news/out").glob("items-*.json"), reverse=True)
    if not files: return {**status("unknown", detail="尚无结构化运行记录")}
    payload = load_json(files[0], {}); items = payload.get("items", [])
    cats = Counter(x.get("category", "其他") for x in items)
    return {"status": "warning", "last_success": datetime.fromtimestamp(files[0].stat().st_mtime, UTC).isoformat(),
            "content_date": files[0].stem.removeprefix("items-"), "item_count": len(items), "category_counts": cats,
            "models": payload.get("models_used", []), "usage": {}, "detail": "等待下次任务补充完整统计", "observed_at": now_iso()}


def youtube_info():
    snap = load_json("/opt/dyyjs-youtube/state/runtime-status.json", {})
    cache = list(Path("/opt/dyyjs-youtube/cache").glob("*.mp4"))
    catalog = load_json("/opt/dyyjs-youtube/state/catalog.json", {})
    total = sum(p.stat().st_size for p in cache)
    stale = bool(snap and time.time() - snap.get("updated_epoch", 0) > 600)
    phase = "idle" if stale else snap.get("phase", "idle")
    return {"status": "warning" if snap.get("phase") == "error" else "healthy", "phase": phase,
            "mode": snap.get("mode", "compat"), "proxy_speed_bps": snap.get("proxy_speed_bps", 0),
            "proxy_bytes": snap.get("proxy_bytes", 0),
            "proxy_total_bytes": snap.get("proxy_total_bytes", 0), "viewers": snap.get("viewers", 0),
            "cache_files": len(cache), "cache_bytes": total, "catalog_entries": len(catalog),
            "play_count": sum(x.get("play_count", 0) for x in catalog.values() if isinstance(x, dict)),
            "protection_triggered": Path("/opt/dyyjs-youtube/state/killswitch.flag").exists(),
            "proxy_pool": mihomo_pool(), "observed_at": now_iso()}


def mihomo_pool():
    try:
        secret = Path("/etc/mihomo/controller.secret").read_text().strip()
        request = urllib.request.Request(
            "http://127.0.0.1:9090/proxies",
            headers={"Authorization": f"Bearer {secret}"},
        )
        with urllib.request.urlopen(request, timeout=1) as response:
            proxies = json.load(response).get("proxies", {})
        members = proxies.get("YouTubeRelay", {}).get("all", [])
        healthy = sum(1 for name in members if proxies.get(name, {}).get("alive") is True)
        regions = []
        if any("香港" in name for name in members): regions.append("香港")
        if any("日本" in name for name in members): regions.append("日本")
        return {"status": "healthy" if healthy else "warning", "healthy": healthy,
                "total": len(members), "regions": regions}
    except (OSError, ValueError):
        return {"status": "unknown", "healthy": None, "total": None, "regions": []}


def storage_info():
    result = []
    for name, path in (
        ("静态网站", "/var/www/dyyjs"),
        ("新闻系统", "/opt/dyyjs-news"),
        ("Horizon 日报", "/opt/dyyjs-horizon"),
        ("视频系统", "/opt/dyyjs-youtube"),
    ):
        rc, out, _ = run(["du", "-sb", path], timeout=2)
        result.append({"name": name, "bytes": int(out.split()[0]) if rc == 0 and out else None, "observed_at": now_iso()})
    return result


def security_info():
    _, ufw, _ = run(["ufw", "status"])
    _, updates, _ = run(["apt-get", "-s", "upgrade"], timeout=2)
    count = sum(1 for line in updates.splitlines() if line.startswith("Inst "))
    return {"status": "healthy" if "Status: active" in ufw else "critical", "firewall_active": "Status: active" in ufw,
            "reboot_required": Path("/var/run/reboot-required").exists(), "available_updates": count, "observed_at": now_iso()}


def recent_events():
    units = ["caddy", "dyyjs-news", "dyyjs-horizon", "dyyjs-youtube", "dyyjs-youtube-pot", "mihomo",
             "dyyjs-status", "dyyjs-status-sampler"]
    cmd = ["journalctl", "--since=-7 days", "-p", "warning", "--no-pager", "-o", "json", "-n", "300"]
    for unit in units: cmd += ["-u", unit]
    _, out, _ = run(cmd, timeout=3); grouped = {}
    for line in out.splitlines():
        try: row = json.loads(line)
        except ValueError: continue
        message = PRIVATE.sub("[已脱敏]", str(row.get("MESSAGE", "")))[:160]
        source = str(row.get("SYSLOG_IDENTIFIER") or "system")[:30]
        key = hashlib.sha256((source + message).encode()).hexdigest()[:12]
        item = grouped.setdefault(key, {"source": source, "severity": "warning", "summary": message, "count": 0, "time": None})
        item["count"] += 1; item["time"] = datetime.fromtimestamp(int(row.get("__REALTIME_TIMESTAMP", "0")) / 1e6, UTC).isoformat()
    return sorted(grouped.values(), key=lambda x: x["time"] or "", reverse=True)[:20]


def validate_public(value):
    text = json.dumps(value, ensure_ascii=False)
    forbidden = [r"(?:\d{1,3}\.){3}\d{1,3}", r"/opt/", r"/var/", r"token=", r"youtube\.com", r"youtu\.be"]
    hits = [pattern for pattern in forbidden if re.search(pattern, text, re.I)]
    if hits: raise ValueError("public payload failed privacy validation")


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--config", required=True); args = parser.parse_args()
    cfg = load_json(args.config, {}); site = Path(cfg["site_root"]); state_dir = Path(cfg["state_dir"])
    previous = load_json(state_dir / "collector-state.json", {})
    generated = now_iso(); started = time.monotonic()
    system = system_metrics(previous)
    traffic = sampler_info(cfg["sampler_snapshot"])
    services = [unit_info(x["unit"], x["name"], x.get("core", False)) for x in cfg["services"]]
    timers = [timer_info(x["unit"], x["name"]) for x in cfg["timers"]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        domain_jobs = [pool.submit(domain_info, name) for name in cfg["domains"]]
        storage_job = pool.submit(storage_info)
        security_job = pool.submit(security_info)
        events_job = pool.submit(recent_events)
        domains = [job.result() for job in domain_jobs]
        storage = storage_job.result(); security = security_job.result(); events = events_job.result()
    failure_counts = dict(previous.get("domain_failures", {}))
    for domain in domains:
        failed = not domain["dns"] or not domain["https_code"]
        failure_counts[domain["name"]] = failure_counts.get(domain["name"], 0) + 1 if failed else 0
        if failed and failure_counts[domain["name"]] < 2:
            domain["status"] = "warning"
    snapshot = {"generated_at": generated, "overall_status": "healthy", "system": {k:v for k,v in system.items() if k not in ("counters", "cpu_counters")},
                "services": services, "news": news_info(), "youtube": youtube_info(),
                "network": {"status": "healthy" if all(x["status"] == "healthy" for x in domains) and traffic["status"] == "healthy" else "warning", "direct": "available", "proxy": "available", "traffic_sampler": traffic, "observed_at": generated},
                "domains": domains, "timers": timers, "storage": storage, "security": security,
                "recent_events": events, "collector": {"status": "healthy", "version": cfg["version"], "duration_ms": 0, "last_success": generated, "observed_at": generated}}
    states = [snapshot["system"]["status"], snapshot["security"]["status"], snapshot["news"]["status"],
              snapshot["youtube"]["status"], snapshot["network"]["status"]] + [x["status"] for x in services + domains + timers]
    snapshot["overall_status"] = "critical" if "critical" in states else "warning" if "warning" in states else "healthy"
    snapshot["collector"]["duration_ms"] = round((time.monotonic() - started) * 1000)
    validate_public(snapshot)
    history = load_json(state_dir / "history.json", {"points": []}); points = history.get("points", [])
    if not history.get("network_peak_v1"):
        points = [{**point, "rx": None, "tx": None} for point in points]
    points.append({"at": generated, "cpu": system["cpu_percent"], "memory": system["memory_percent"], "disk": system["disk_percent"], "rx": traffic["rx_peak_bps"], "tx": traffic["tx_peak_bps"], "news_success": snapshot["news"].get("success")})
    cutoff = time.time() - 7 * 86400
    points = [x for x in points[-2016:] if datetime.fromisoformat(x["at"]).timestamp() >= cutoff]
    public_history = {"generated_at": generated, "network_peak_v1": True, "points": points}
    validate_public(public_history)
    atomic_json(site / "data/status.json", snapshot); atomic_json(site / "data/history.json", public_history)
    atomic_json(state_dir / "history.json", public_history); atomic_json(state_dir / "collector-state.json", {
        "cpu_counters": system["cpu_counters"], "last_cpu": system["cpu_percent"],
        "domain_failures": failure_counts, "last_success": generated,
    })


if __name__ == "__main__":
    main()
