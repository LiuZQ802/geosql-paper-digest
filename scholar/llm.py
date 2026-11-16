"""OpenAI 兼容的 Chat Completions 客户端（DeepSeek / 中转站 / OpenAI 都能用）。"""
import json
import os
import re
import time

from .util import http_post, log

USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


class LLM:
    def __init__(self, cfg):
        self.base = (os.getenv(cfg.get("base_url_env", "LLM_BASE_URL")) or cfg.get("base_url") or "").rstrip("/")
        if not self.base:
            raise SystemExit("缺少接口地址：请设置环境变量 LLM_BASE_URL，或在 config.yaml 的 llm.base_url 中填写")
        self.key = os.getenv(cfg.get("api_key_env", "LLM_API_KEY"), "")
        if not self.key:
            raise SystemExit(f"缺少 API key：请设置环境变量 {cfg.get('api_key_env', 'LLM_API_KEY')}")

    def chat_json(self, model, system, user, max_tokens=4000, temperature=0.2, retries=5):
        body = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        err = None
        for i in range(retries):
            try:
                r = http_post(f"{self.base}/chat/completions", json=body, timeout=300,
                              headers={"Authorization": f"Bearer {self.key}"})
                if r.status_code != 200:
                    err = f"HTTP {r.status_code}: {r.text[:300]}"
                    if r.status_code in (400, 401, 402, 403, 404):
                        break  # 配置错误，重试无意义
                    time.sleep(15 * (i + 1))
                    continue
                data = r.json()
                if "choices" not in data:
                    err = str(data.get("error", data))[:300]
                    time.sleep(15 * (i + 1))
                    continue
                u = data.get("usage") or {}
                USAGE["prompt_tokens"] += u.get("prompt_tokens", 0)
                USAGE["completion_tokens"] += u.get("completion_tokens", 0)
                USAGE["calls"] += 1
                return _parse_json(data["choices"][0]["message"]["content"])
            except (ValueError, KeyError, RuntimeError) as e:
                err = str(e)
                time.sleep(3)
        log("LLM call failed:", err)
        raise RuntimeError(err)


def _parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError("LLM 返回的不是 JSON: " + text[:200])
