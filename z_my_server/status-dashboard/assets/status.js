"use strict";

const $ = id => document.getElementById(id);
const labels = {healthy: "正常", warning: "需关注", critical: "异常", unknown: "暂无数据"};
const esc = value => String(value ?? "—").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

function time(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
  } catch {
    return "—";
  }
}

function duration(seconds) {
  if (seconds == null) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds % 86400 / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return days ? `${days}天 ${hours}小时` : hours ? `${hours}小时 ${minutes}分` : `${minutes}分钟`;
}

function bytes(value) {
  if (value == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

const percent = value => value == null ? "—" : `${value}%`;

function item(name, state, rows) {
  return `<article class="item ${esc(state)}"><div class="item-head"><strong>${esc(name)}</strong>` +
    `<span class="badge">${labels[state] || labels.unknown}</span></div><dl>` +
    rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("") + "</dl></article>";
}

function details(state, rows) {
  return `<div class="item-head"><span></span><span class="badge ${esc(state)}">` +
    `${labels[state] || labels.unknown}</span></div><dl>` +
    rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value ?? "—")}</dd>`).join("") + "</dl>";
}

function empty() { return `<article class="item muted">暂无数据</article>`; }

function render(data) {
  $("notice").className = "notice ready";
  $("overall").textContent = labels[data.overall_status] || labels.unknown;
  $("overall-dot").className = `dot ${data.overall_status}`;
  $("updated").textContent = time(data.generated_at);
  const age = (Date.now() - new Date(data.generated_at)) / 60000;
  $("freshness").textContent = age > 30 ? "数据已明显过期" : age > 10 ? "数据更新稍有延迟" : "数据新鲜";

  const system = data.system || {};
  const metrics = [
    ["CPU", percent(system.cpu_percent)], ["内存", percent(system.memory_percent)],
    ["Swap", percent(system.swap_percent)], ["磁盘", percent(system.disk_percent)],
    ["15 分钟负载", system.load?.[2] ?? "—"], ["运行时间", duration(system.uptime_seconds)],
  ];
  $("metrics").innerHTML = metrics.map(([name, value]) =>
    `<article class="card metric"><strong>${esc(value)}</strong><span>${esc(name)}</span></article>`).join("");

  $("services").innerHTML = (data.services || []).map(service => item(service.name, service.status, [
    ["运行状态", service.running ? "运行中" : "未运行"],
    ["启动时间", time(service.since)], ["最近重启", time(service.last_restart)],
  ])).join("") || empty();

  const news = data.news || {};
  const usage = news.usage || {};
  $("news").innerHTML = details(news.status, [
    ["最近成功", time(news.last_success || news.finished_at)], ["内容日期", news.content_date],
    ["条目总数", news.item_count], ["公开模型", (news.models || []).join("、") || "—"],
    ["Prompt Token", usage.prompt_tokens], ["Completion Token", usage.completion_tokens],
    ["API 调用", usage.api_calls], ["任务耗时", duration(news.duration_seconds)],
  ]);

  const youtube = data.youtube || {};
  $("youtube").innerHTML = details(youtube.status, [
    ["当前阶段", ({idle: "空闲", resolving: "解析中", streaming: "转发中", finalizing: "缓存整理中", error: "异常"})[youtube.phase] || "暂无数据"],
    ["模式", youtube.mode === "high" ? "高清" : "兼容"], ["最近代理拉取速度", `${bytes(youtube.proxy_speed_bps)}/s`],
    ["累计代理拉取流量", bytes(youtube.proxy_total_bytes)],
    ["最近代理拉取流量", bytes(youtube.proxy_bytes)], ["观看连接", youtube.viewers],
    ["缓存", `${youtube.cache_files ?? 0} 个 · ${bytes(youtube.cache_bytes)}`],
    ["历史播放", youtube.play_count], ["保护开关", youtube.protection_triggered ? "已触发" : "未触发"],
  ]);
  const pool = youtube.proxy_pool || {};
  if (pool.total != null) {
    $("youtube").insertAdjacentHTML("beforeend", `<dl><dt>匿名代理池</dt><dd>${esc(`${pool.healthy}/${pool.total} 健康 · ${(pool.regions || []).join("/")}池`)}</dd></dl>`);
  }

  $("domains").innerHTML = (data.domains || []).map(domain => item(domain.name, domain.status, [
    ["DNS", domain.dns ? "解析正常" : "解析失败"], ["HTTPS", domain.https_code || "不可用"],
    ["响应时间", domain.latency_ms == null ? "—" : `${domain.latency_ms} ms`],
    ["证书机构", domain.certificate?.issuer],
    ["剩余有效期", domain.certificate?.days_remaining == null ? "—" : `${domain.certificate.days_remaining} 天`],
  ])).join("") || empty();

  $("timers").innerHTML = (data.timers || []).map(timer => item(timer.name, timer.status, [
    ["上次结果", timer.last_result], ["上次执行", time(timer.last_run)], ["下次执行", time(timer.next_run)],
  ])).join("") || empty();

  const security = data.security || {};
  $("security").innerHTML = details(security.status, [
    ["主机防火墙", security.firewall_active ? "已启用" : "未启用"],
    ["等待重启", security.reboot_required ? "是" : "否"],
    ["可安装更新", security.available_updates == null ? "—" : `${security.available_updates} 项`],
    ["网络探测", data.network?.status === "healthy" ? "正常" : "需关注"],
  ]);
  $("storage").innerHTML = (data.storage || []).map(entry => item(entry.name, "healthy", [
    ["占用空间", bytes(entry.bytes)],
  ])).join("");

  $("events").innerHTML = (data.recent_events || []).map(event =>
    `<article class="item event ${esc(event.severity)}"><span class="badge">${esc(event.source)}</span>` +
    `<p>${esc(event.summary)}${event.count > 1 ? ` <small>×${event.count}</small>` : ""}</p>` +
    `<time>${time(event.time)}</time></article>`).join("") ||
    `<article class="item muted">最近 7 天没有需要展示的告警摘要。</article>`;
  $("version").textContent = data.collector?.version || "—";
}

function drawChart(canvas, tooltip, points, series, maximum, valueLabel) {
  const dpr = devicePixelRatio || 1;
  const width = canvas.clientWidth || 500;
  const height = 210;
  const pad = {left: 42, right: 10, top: 12, bottom: 25};
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const endTime = Date.now();
  const startTime = endTime - 7 * 86400000;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const context = canvas.getContext("2d");
  context.scale(dpr, dpr);
  context.clearRect(0, 0, width, height);
  context.font = "11px system-ui, sans-serif";
  context.fillStyle = "#8a8177";
  context.strokeStyle = "#ded3c4";
  context.lineWidth = 1;

  for (let index = 0; index <= 4; index += 1) {
    const value = maximum * (4 - index) / 4;
    const y = pad.top + plotHeight * index / 4;
    context.beginPath(); context.moveTo(pad.left, y); context.lineTo(width - pad.right, y); context.stroke();
    context.textAlign = "right"; context.textBaseline = "middle";
    context.fillText(valueLabel(value), pad.left - 6, y);
  }
  context.textBaseline = "alphabetic";
  context.textAlign = "left"; context.fillText("7天前", pad.left, height - 5);
  context.textAlign = "right"; context.fillText("现在", width - pad.right, height - 5);

  const plotted = points.map(point => ({...point, timestamp: new Date(point.at).getTime()}))
    .filter(point => Number.isFinite(point.timestamp) && point.timestamp >= startTime && point.timestamp <= endTime);
  const xFor = timestamp => pad.left + (timestamp - startTime) / (endTime - startTime) * plotWidth;
  const yFor = value => pad.top + plotHeight - Math.min(Math.max(value, 0) / maximum, 1) * plotHeight;

  series.forEach(({key, color}) => {
    context.strokeStyle = color; context.lineWidth = 2; context.beginPath();
    let connected = false;
    plotted.forEach(point => {
      const raw = point[key];
      if (raw == null || !Number.isFinite(Number(raw))) { connected = false; return; }
      const x = xFor(point.timestamp); const y = yFor(Number(raw));
      if (connected) context.lineTo(x, y); else context.moveTo(x, y);
      connected = true;
    });
    context.stroke();
  });

  canvas.onmousemove = event => {
    if (!plotted.length) { tooltip.hidden = true; return; }
    const rect = canvas.getBoundingClientRect();
    const mouseX = (event.clientX - rect.left) * width / rect.width;
    const nearest = plotted.reduce((best, point) =>
      Math.abs(xFor(point.timestamp) - mouseX) < Math.abs(xFor(best.timestamp) - mouseX) ? point : best);
    const x = xFor(nearest.timestamp);
    if (Math.abs(x - mouseX) > 22) { tooltip.hidden = true; return; }
    const rows = series.map(({key, name, color}) => {
      const value = nearest[key];
      return `<span style="color:${color}">●</span> ${esc(name)}：${value == null ? "暂无数据" : esc(valueLabel(Number(value)))}`;
    });
    tooltip.innerHTML = `<strong>${esc(time(nearest.at))}</strong><br>${rows.join("<br>")}`;
    const cssX = x / width * rect.width;
    tooltip.style.left = `${Math.min(Math.max(cssX, 75), rect.width - 75)}px`;
    tooltip.style.top = `${pad.top + 8}px`;
    tooltip.hidden = false;
  };
  canvas.onmouseleave = () => { tooltip.hidden = true; };
}

async function load() {
  try {
    const stamp = Date.now();
    const [statusResponse, historyResponse] = await Promise.all([
      fetch(`data/status.json?t=${stamp}`, {cache: "no-store"}),
      fetch(`data/history.json?t=${stamp}`, {cache: "no-store"}),
    ]);
    if (!statusResponse.ok) throw new Error("status unavailable");
    const data = await statusResponse.json();
    render(data);
    const history = historyResponse.ok ? await historyResponse.json() : {points: []};
    const points = history.points || [];
    drawChart($("resource-chart"), $("resource-tooltip"), points, [
      {key: "cpu", name: "CPU", color: "#d97757"},
      {key: "memory", name: "内存", color: "#4c8a65"},
      {key: "disk", name: "磁盘", color: "#9b79a6"},
    ], 100, value => `${Math.round(value)}%`);
    const peak = Math.max(1024, ...points.flatMap(point => [point.rx || 0, point.tx || 0]));
    drawChart($("network-chart"), $("network-tooltip"), points, [
      {key: "rx", name: "五分钟接收峰值", color: "#4f7ea8"},
      {key: "tx", name: "五分钟发送峰值", color: "#c68a2f"},
    ], peak, value => `${bytes(value)}/s`);
  } catch {
    $("notice").className = "notice error";
    $("notice").textContent = "状态数据暂不可用，上一版静态页面仍可继续浏览。";
  }
}

load();
setInterval(load, 60000);
addEventListener("resize", load);
