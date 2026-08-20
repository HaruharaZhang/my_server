"""共用代码：加载配置、连接百炼 OpenAI 兼容端点、按优先级降级的模型路由。

流水线不再抓取文章正文页；LLM 只做摘要与每日速览，网络访问全部在 collect/images 阶段的代码里。
"""

import json
import os
from pathlib import Path

import httpx
from openai import AsyncOpenAI


def load_config():
    path = Path(__file__).with_name("config.json")
    return json.loads(path.read_text(encoding="utf-8"))


def make_client(cfg):
    env_name = cfg["api"]["api_key_env"]
    api_key = os.environ.get(env_name, "")
    if not api_key:
        raise RuntimeError(f"环境变量 {env_name} 未设置")
    return AsyncOpenAI(
        base_url=cfg["api"]["base_url"],
        api_key=api_key,
        timeout=180,
        max_retries=0,  # 重试与降级由 ModelRouter 统一控制
        # 模型必须直连。不能让主机上的 mihomo 或代理环境变量影响模型列表与聊天请求。
        http_client=httpx.AsyncClient(trust_env=False),
    )


def new_usage():
    return {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0}


def add_usage(total, other):
    for key in total:
        total[key] += other.get(key, 0)


# 错误信息里出现这些词视为限流/额度耗尽，本次运行内禁用该模型
THROTTLE_MARKERS = ("throttling", "rate limit", "ratelimit", "limit", "quota", "exhausted", "429")


def is_throttle_error(exc):
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in THROTTLE_MARKERS)


class ModelRouter:
    """按 config 的 model_priority 依序调用；限流自动降级到下一个模型。"""

    def __init__(self, cfg, log):
        self.client = make_client(cfg)
        self.priority = list(cfg["api"]["model_priority"])
        self.disabled = set()      # 本次运行内被限流禁用的模型
        self.models_used = []      # 实际成功用到的模型（有序去重），供页面展示
        self.usage = new_usage()
        self.log = log

    async def init_models(self):
        """与 /models 实际列表取交集；接口失败则原样使用配置列表。"""
        try:
            listed = {m.id async for m in self.client.models.list()}
        except Exception as exc:
            self.log.warning("拉取 /models 失败，直接使用配置的优先级列表: %s", exc)
            return
        available = [m for m in self.priority if m in listed]
        skipped = [m for m in self.priority if m not in listed]
        if skipped:
            self.log.info("优先级列表中不存在的模型（跳过）: %s", ", ".join(skipped))
        if not available:
            raise RuntimeError("model_priority 中没有任何模型存在于 /models 列表")
        self.priority = available
        self.log.info("可用模型优先级: %s", " > ".join(available))

    async def chat(self, messages):
        """按优先级逐个尝试，返回 (回复文本, 实际使用的模型名)。"""
        for model in self.priority:
            if model in self.disabled:
                continue
            for attempt in (1, 2):
                try:
                    resp = await self.client.chat.completions.create(model=model, messages=messages)
                except Exception as exc:
                    if is_throttle_error(exc):
                        self.log.warning("模型 %s 被限流，本次运行内禁用: %s", model, str(exc)[:200])
                        self.disabled.add(model)
                        break  # 换下一个模型
                    if attempt == 1:
                        self.log.warning("模型 %s 调用出错，重试一次: %s", model, str(exc)[:200])
                        continue
                    self.log.warning("模型 %s 重试仍失败，降级: %s", model, str(exc)[:200])
                    break
                self.usage["api_calls"] += 1
                if resp.usage:
                    self.usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
                    self.usage["completion_tokens"] += resp.usage.completion_tokens or 0
                if model not in self.models_used:
                    self.models_used.append(model)
                self.log.info("模型 %s 调用成功", model)
                return resp.choices[0].message.content or "", model
        raise RuntimeError("所有优先级模型均不可用（限流或调用失败）")


def parse_json_reply(text):
    """解析模型回复中的 JSON，容忍 ```json 围栏和前后杂散文字。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    ends = [i for i in (text.rfind("]"), text.rfind("}")) if i != -1]
    if not starts or not ends:
        raise ValueError(f"回复中找不到 JSON: {text[:200]}")
    return json.loads(text[min(starts):max(ends) + 1])
