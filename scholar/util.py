import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
UA = "geo-sql-paper-digest/1.0 (academic literature monitor)"

_session = requests.Session()
_session.headers["User-Agent"] = UA

# 出版社接口要用机构网络出口（不走本机代理），否则拿不到授权全文
DIRECT_HOSTS = ("api.elsevier.com", "link.springer.com", "api.springernature.com")
_direct = requests.Session()
_direct.trust_env = False          # 忽略 HTTP(S)_PROXY 环境变量
_direct.headers["User-Agent"] = UA


def log(*args):
    print("[digest]", *args, file=sys.stderr, flush=True)


def _request(method, url, retries=4, backoff=5, **kw):
    kw.setdefault("timeout", 60)
    last = None
    sess = _direct if any(h in url for h in DIRECT_HOSTS) else _session
    for i in range(retries):
        try:
            r = sess.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(backoff * (i + 1))
                continue
            return r
        except requests.RequestException as e:
            last = str(e)
            time.sleep(backoff * (i + 1))
    raise RuntimeError(f"{method} {url} failed: {last}")


def http_get(url, **kw):
    return _request("GET", url, **kw)


def http_post(url, **kw):
    return _request("POST", url, **kw)


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
