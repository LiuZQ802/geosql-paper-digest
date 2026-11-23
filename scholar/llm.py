"""OpenAI 兼容的 Chat Completions 客户端（DeepSeek / 中转站 / OpenAI 都能用）。"""
import json
import os
import re
import time

import threading

from .util import http_get, http_post, log

USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "by_model": {}}
_lock = threading.Lock()


def _record(model, u):
    with _lock:
        p, c = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        USAGE["prompt_tokens"] += p
        USAGE["completion_tokens"] += c
        USAGE["calls"] += 1
        m = USAGE["by_model"].setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
        m["prompt_tokens"] += p
        m["completion_tokens"] += c
        m["calls"] += 1


class LLM:
    def __init__(self, cfg):
        self.base = (os.getenv(cfg.get("base_url_env", "LLM_BASE_URL")) or cfg.get("base_url") or "").rstrip("/")
        if not self.base:
            raise SystemExit("缺少接口地址：请设置环境变量 LLM_BASE_URL，或在 config.yaml 的 llm.base_url 中填写")
        self.key = os.getenv(cfg.get("api_key_env", "LLM_API_KEY"), "")
        if not self.key:
            raise SystemExit(f"缺少 API key：请设置环境变量 {cfg.get('api_key_env', 'LLM_API_KEY')}")

    def chat_json(self, model, system, user, max_tokens=4000, temperature=0.2, retries=None):
        """model 可以是单个模型名，也可以是列表：按顺序尝试，前一个失败就换下一个。"""
        models = [model] if isinstance(model, str) else list(model)
        if retries is None:
            retries = 5 if len(models) == 1 else 2
        err = None
        for m in models:
            try:
                return self._chat_once(m, system, user, max_tokens, temperature, retries)
            except RuntimeError as e:
                err = e
                if m != models[-1]:
                    log(f"model {m} unavailable, falling back to next model")
        raise err

    def _chat_once(self, model, system, user, max_tokens, temperature, retries):
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
                _record(model, data.get("usage") or {})
                return _parse_json(data["choices"][0]["message"]["content"])
            except (ValueError, KeyError, RuntimeError) as e:
                err = str(e)
                time.sleep(3)
        log(f"LLM call failed ({model}):", err)
        raise RuntimeError(err)


    def cost(self):
        """按中转站（New API）公开的价格表估算本次运行花费，单位是中转站的美元额度。

        New API 计费：quota = (输入 token + 输出 token × completion_ratio) × model_ratio × group_ratio，
        500000 quota = $1。部分模型有分时段加价表达式，这里只处理常见的“某时段 ×2”。
        """
        try:
            r = http_get(self.base.removesuffix("/v1") + "/api/pricing", retries=2, timeout=30,
                         headers={"Authorization": f"Bearer {self.key}"})
            data = r.json()
            prices = {row["model_name"]: row for row in data.get("data", [])}
            group = (data.get("group_ratio") or {}).get("default", 1)
        except Exception as e:  # noqa: BLE001
            log("pricing unavailable:", e)
            return None
        hour = time.gmtime().tm_hour + 8  # 北京时间
        hour %= 24
        total, by_model = 0.0, {}
        for model, u in USAGE["by_model"].items():
            row = prices.get(model)
            if not row:
                continue
            quota = (u["prompt_tokens"] + u["completion_tokens"] * row.get("completion_ratio", 1)) \
                * row.get("model_ratio", 0) * group
            for lo, hi in re.findall(r'hour\("Asia/Shanghai"\) >= (\d+) && hour\("Asia/Shanghai"\) < (\d+) \? 2',
                                     row.get("billing_expr") or ""):
                if int(lo) <= hour < int(hi):
                    quota *= 2
            usd = quota / 500000
            by_model[model] = {**u, "usd": round(usd, 4)}
            total += usd
        return {"usd": round(total, 4), "by_model": by_model}


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
