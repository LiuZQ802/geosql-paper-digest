"""网页数据加密：AES-256-GCM，密钥由站点密码经 PBKDF2-SHA256 派生。

浏览器端 web/app.js 用 WebCrypto 做同样的派生和解密，两边参数必须一致。
- 设置了环境变量 SITE_PASSWORD：web/data 下的 JSON 一律加密读写；
- 没设置：明文读写（本地开发用）。
"""
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .util import ROOT

DATA = ROOT / "web" / "data"
META = DATA / "vault.json"
ITERATIONS = 310_000
CHECK_TEXT = "geosql-digest-ok"

_key = None


def _b64(b):
    return base64.b64encode(b).decode()


def _unb64(s):
    return base64.b64decode(s)


def enabled():
    return bool(os.getenv("SITE_PASSWORD"))


def _derive(password, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode())


def _encrypt(key, obj):
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    return {"v": 1, "iv": _b64(iv), "ct": _b64(ct)}


def _decrypt(key, blob):
    return json.loads(AESGCM(key).decrypt(_unb64(blob["iv"]), _unb64(blob["ct"]), None))


def key():
    """取得（必要时创建）站点密钥。密码变了会重建 vault.json，旧数据需用 rekey 重新加密。"""
    global _key
    if _key:
        return _key
    pw = os.environ["SITE_PASSWORD"]
    meta = json.loads(META.read_text(encoding="utf-8")) if META.exists() else None
    if meta:
        k = _derive(pw, _unb64(meta["salt"]), meta["iterations"])
        try:
            if _decrypt(k, meta["check"]) == CHECK_TEXT:
                _key = k
                return k
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit("SITE_PASSWORD 与 web/data/vault.json 不匹配：改密码请运行 python -m scholar.vault rekey <旧密码>")
    salt = os.urandom(16)
    k = _derive(pw, salt, ITERATIONS)
    META.parent.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps({"kdf": "PBKDF2-SHA256", "iterations": ITERATIONS, "salt": _b64(salt),
                                "check": _encrypt(k, CHECK_TEXT)}, indent=1), encoding="utf-8")
    _key = k
    return k


def load(path: Path, default):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    if isinstance(obj, dict) and obj.get("v") == 1 and "ct" in obj:
        if not enabled():
            raise SystemExit(f"{path} 已加密，请设置环境变量 SITE_PASSWORD")
        return _decrypt(key(), obj)
    return obj


def save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _encrypt(key(), obj) if enabled() else obj
    path.write_text(json.dumps(data, ensure_ascii=False, indent=None if enabled() else 1), encoding="utf-8")


def _all_data_files():
    return [DATA / "index.json", *sorted((DATA / "daily").glob("*.json"))]


def encrypt_all():
    """把现有明文数据全部加密（首次开启密码时用）。"""
    for p in _all_data_files():
        if p.exists():
            save(p, load(p, None))
    print("encrypted", len(_all_data_files()), "files")


def rekey(old_password):
    """改密码：用旧密码解密全部数据，再用新密码（SITE_PASSWORD）重新加密。"""
    global _key
    new = os.environ["SITE_PASSWORD"]
    os.environ["SITE_PASSWORD"] = old_password
    data = {p: load(p, None) for p in _all_data_files() if p.exists()}
    os.environ["SITE_PASSWORD"] = new
    META.unlink()
    _key = None
    for p, obj in data.items():
        save(p, obj)
    print("re-encrypted", len(data), "files with the new password")


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "encrypt-all":
        encrypt_all()
    elif cmd == "rekey" and len(sys.argv) == 3:
        rekey(sys.argv[2])
    else:
        print("用法: python -m scholar.vault encrypt-all | rekey <旧密码>   （新密码放在环境变量 SITE_PASSWORD）")
