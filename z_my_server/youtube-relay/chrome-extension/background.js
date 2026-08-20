// 点击工具栏图标：把当前 YouTube / Bilibili 视频页包装成对应的 dyyjs 中转链接并复制到剪贴板。
// 生成的链接不带 quality 参数（走服务端默认档位）。
// 两个中转服务的 token 不同，分别存在 storage 的 token（YouTube）和 bilibiliToken 里。

const YOUTUBE_RELAY_BASE = "https://dyyjs.com/youtube";
const BILIBILI_RELAY_BASE = "https://dyyjs.com/bilibili";
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
// 与服务端 bilibili-relay/validate.py 的 BVID_RE 保持一致
const BVID_RE = /BV[0-9A-Za-z]{10}/;

// 与服务端 validate.py 的规则保持一致：支持 watch?v=、youtu.be/、/shorts/ 三种形式
function extractVideoId(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return "";
  }
  const host = url.hostname.toLowerCase();
  let candidate = "";
  if (host === "youtu.be") {
    candidate = url.pathname.replace(/^\//, "").split("/")[0];
  } else if (host === "youtube.com" || host.endsWith(".youtube.com")) {
    candidate = url.searchParams.get("v") || "";
    if (!candidate && url.pathname.startsWith("/shorts/")) {
      candidate = url.pathname.slice("/shorts/".length).split("/")[0];
    }
  }
  return VIDEO_ID_RE.test(candidate) ? candidate : "";
}

// 与服务端 validate.py 的规则保持一致：域名白名单内正则搜 BV 号，分 P 取 query 里的 p=
function extractBilibiliVideo(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return "";
  }
  const host = url.hostname.toLowerCase();
  if (!["bilibili.com", "www.bilibili.com", "m.bilibili.com"].includes(host)) {
    return "";
  }
  const match = BVID_RE.exec(url.pathname);
  if (!match) return "";

  let link = `https://www.bilibili.com/video/${match[0]}`;
  const page = parseInt(url.searchParams.get("p") || "", 10);
  if (page > 1) link += `?p=${page}`;
  return link;
}

// 在页面里执行：复制文本并显示一个短暂的提示条
function copyAndToast(text, message, isError) {
  const showToast = (msg, bad) => {
    const el = document.createElement("div");
    el.textContent = msg;
    el.style.cssText = [
      "position:fixed",
      "top:16px",
      "left:50%",
      "transform:translateX(-50%)",
      "z-index:2147483647",
      "padding:10px 18px",
      "border-radius:8px",
      "font:14px/1.4 sans-serif",
      "color:#fff",
      `background:${bad ? "#c0392b" : "#1a7f37"}`,
      "box-shadow:0 2px 8px rgba(0,0,0,.35)",
    ].join(";");
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2200);
  };

  if (isError) {
    showToast(message, true);
    return;
  }

  const fallbackCopy = () => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  };

  navigator.clipboard
    .writeText(text)
    .then(() => showToast(message, false))
    .catch(() => {
      if (fallbackCopy()) {
        showToast(message, false);
      } else {
        showToast("复制失败，请重试", true);
      }
    });
}

function runInTab(tabId, args) {
  return chrome.scripting.executeScript({
    target: { tabId },
    func: copyAndToast,
    args,
  });
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab || !tab.id) return;

  const videoId = extractVideoId(tab.url || "");
  const bilibiliLink = videoId ? "" : extractBilibiliVideo(tab.url || "");
  if (!videoId && !bilibiliLink) {
    try {
      await runInTab(tab.id, ["", "当前页面不是 YouTube 或 B 站视频页", true]);
    } catch {
      // chrome:// 等页面无法注入脚本，只能静默忽略
    }
    return;
  }

  const { token, bilibiliToken } = await chrome.storage.local.get([
    "token",
    "bilibiliToken",
  ]);

  let relayUrl;
  if (videoId) {
    if (!token) {
      await runInTab(tab.id, ["", "尚未设置 YouTube token，请在插件选项中填入", true]);
      chrome.runtime.openOptionsPage();
      return;
    }
    // 服务端把 link= 之后到结尾的内容整体当作链接，所以 link 必须是最后一个参数
    relayUrl =
      `${YOUTUBE_RELAY_BASE}?token=${encodeURIComponent(token)}` +
      `&link=https://www.youtube.com/watch?v=${videoId}`;
  } else {
    if (!bilibiliToken) {
      await runInTab(tab.id, ["", "尚未设置 B 站 token，请在插件选项中填入", true]);
      chrome.runtime.openOptionsPage();
      return;
    }
    relayUrl =
      `${BILIBILI_RELAY_BASE}?token=${encodeURIComponent(bilibiliToken)}` +
      `&link=${bilibiliLink}`;
  }

  await runInTab(tab.id, [relayUrl, "已复制中转链接 ✓", false]);
});
