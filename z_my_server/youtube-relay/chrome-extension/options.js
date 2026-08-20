const tokenInput = document.getElementById("token");
const bilibiliTokenInput = document.getElementById("bilibiliToken");
const statusEl = document.getElementById("status");

chrome.storage.local.get(["token", "bilibiliToken"]).then(({ token, bilibiliToken }) => {
  if (token) tokenInput.value = token;
  if (bilibiliToken) bilibiliTokenInput.value = bilibiliToken;
});

document.getElementById("save").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  const bilibiliToken = bilibiliTokenInput.value.trim();
  await chrome.storage.local.set({ token, bilibiliToken });
  statusEl.textContent = token || bilibiliToken ? "已保存 ✓" : "已清空";
  setTimeout(() => (statusEl.textContent = ""), 2000);
});
