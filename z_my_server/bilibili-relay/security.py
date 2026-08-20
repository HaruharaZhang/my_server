"""token 校验 + 全局滑动窗口失败计数 + 熔断（killswitch）。

熔断只手动恢复：写文件后无论 token 对不对都直接 503，直到有人手动删除该文件。
"""

import hmac
import json
import os
import time
from collections import deque
from pathlib import Path


def mask_token(token):
    if not token:
        return "<empty>"
    return token[:4] + "***"


class TokenGuard:
    def __init__(self, tokens, killswitch_path, window_seconds, max_failures, log):
        self._tokens = [t for t in tokens if t]
        self._killswitch_path = Path(killswitch_path)
        self._window_seconds = window_seconds
        self._max_failures = max_failures
        self._log = log
        self._failures = deque()

    def killswitch_active(self):
        return self._killswitch_path.exists()

    def check(self, token, client_ip):
        """返回 True=校验通过；False 时调用方直接 403（除非已触发熔断）。"""
        if not token:
            self._record_failure(client_ip, "")
            return False
        matched = any(hmac.compare_digest(token, valid) for valid in self._tokens)
        if not matched:
            self._record_failure(client_ip, token)
            return False
        return True

    def _record_failure(self, client_ip, token):
        now = time.monotonic()
        self._failures.append(now)
        cutoff = now - self._window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        self._log.warning("token 校验失败: ip=%s token_prefix=%s window_count=%d",
                           client_ip, mask_token(token), len(self._failures))
        if len(self._failures) >= self._max_failures and not self.killswitch_active():
            self._trip(client_ip, token)

    def _trip(self, client_ip, token):
        self._killswitch_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "triggered_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "failure_count": len(self._failures),
            "window_seconds": self._window_seconds,
            "sample_ip": client_ip,
            "sample_token_prefix": mask_token(token),
        }
        tmp = self._killswitch_path.with_suffix(".flag.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._killswitch_path)
        self._log.error("触发熔断: %s", json.dumps(payload, ensure_ascii=False))
