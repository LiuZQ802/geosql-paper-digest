"""付费期刊论文的摘要补全与全文获取。

OpenAlex 拿不到大多数 Elsevier 期刊的摘要（出版社不授权），这里按顺序补：
  1. Elsevier 官方 API（需要 ELSEVIER_API_KEY；摘要任何网络都能拿到，全文需要机构授权的 IP 或 ELSEVIER_INSTTOKEN）
  2. Semantic Scholar 批量接口
  3. Crossref（部分出版社会存摘要）
"""
import os
import re
import time

from .util import http_get, http_post, log


def _clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")          # 去掉 JATS/HTML 标签
    text = re.sub(r"^\s*(Abstract|ABSTRACT)[:.\s]*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _elsevier_headers():
    h = {"X-ELS-APIKey": os.environ["ELSEVIER_API_KEY"], "Accept": "application/json"}
    if os.getenv("ELSEVIER_INSTTOKEN"):
        h["X-ELS-Insttoken"] = os.environ["ELSEVIER_INSTTOKEN"]
    return h


def elsevier_abstract(doi):
    if not os.getenv("ELSEVIER_API_KEY") or not doi.startswith("10.1016/"):
        return None
    try:
        r = http_get(f"https://api.elsevier.com/content/article/doi/{doi}", retries=2, timeout=30,
                     params={"view": "META_ABS"}, headers=_elsevier_headers())
        if r.status_code != 200:
            return None
        core = r.json().get("full-text-retrieval-response", {}).get("coredata", {})
        return _clean(core.get("dc:description"))
    except Exception:  # noqa: BLE001
        return None


def elsevier_fulltext(doi):
    """有机构授权（校园 IP 或 insttoken）时返回全文纯文本，否则返回 None。"""
    if not os.getenv("ELSEVIER_API_KEY") or not (doi or "").startswith("10.1016/"):
        return None
    try:
        h = _elsevier_headers()
        h["Accept"] = "text/plain"
        r = http_get(f"https://api.elsevier.com/content/article/doi/{doi}", retries=2, timeout=60,
                     params={"view": "FULL"}, headers=h)
        text = r.text if r.status_code == 200 else ""
        return text if len(text) > 8000 else None   # 无授权时只返回题录+摘要，很短
    except Exception:  # noqa: BLE001
        return None


def s2_abstracts(dois):
    out = {}
    for i in range(0, len(dois), 100):
        chunk = dois[i:i + 100]
        try:
            h = {"x-api-key": os.environ["S2_API_KEY"]} if os.getenv("S2_API_KEY") else {}
            r = http_post("https://api.semanticscholar.org/graph/v1/paper/batch", retries=3, timeout=60,
                          params={"fields": "abstract,externalIds"}, json={"ids": [f"DOI:{d}" for d in chunk]},
                          headers=h)
            if r.status_code != 200:
                continue
            for doi, row in zip(chunk, r.json()):
                if row and row.get("abstract"):
                    out[doi] = _clean(row["abstract"])
        except Exception:  # noqa: BLE001
            continue
    return out


def crossref_abstract(doi):
    try:
        r = http_get(f"https://api.crossref.org/works/{doi}", retries=2, timeout=30,
                     headers={"User-Agent": "geosql-paper-digest/1.0 (academic literature monitor)"})
        if r.status_code != 200:
            return None
        return _clean(r.json().get("message", {}).get("abstract"))
    except Exception:  # noqa: BLE001
        return None


def recover_abstracts(papers):
    """给没有摘要的论文补摘要（原地修改）。补不到的标记 no_abstract=True。"""
    todo = [p for p in papers if not p.get("abstract") and p.get("doi")]
    if not todo:
        return
    got = {"Elsevier": 0, "S2": 0, "Crossref": 0}
    for p in todo:
        a = elsevier_abstract(p["doi"])
        if a:
            p["abstract"], p["abstract_src"] = a, "Elsevier"
            got["Elsevier"] += 1
    rest = [p for p in todo if not p.get("abstract")]
    s2 = s2_abstracts([p["doi"] for p in rest])
    for p in rest:
        if s2.get(p["doi"]):
            p["abstract"], p["abstract_src"] = s2[p["doi"]], "S2"
            got["S2"] += 1
    for p in [p for p in rest if not p.get("abstract")]:
        a = crossref_abstract(p["doi"])
        time.sleep(0.2)
        if a:
            p["abstract"], p["abstract_src"] = a, "Crossref"
            got["Crossref"] += 1
    for p in todo:
        if not p.get("abstract"):
            p["no_abstract"] = True
    log(f"abstract recovery: {len(todo)} missing → " + ", ".join(f"{k} {v}" for k, v in got.items())
        + f", still missing {sum(1 for p in todo if p.get('no_abstract'))}")
