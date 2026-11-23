"""检索来源：arXiv（最新预印本）、OpenAlex（正式发表）、Semantic Scholar（种子论文推荐）。"""
import datetime as dt
import os
import re
import time
import xml.etree.ElementTree as ET

from .util import ROOT, http_get, http_post, log, norm_title, read_json, write_json

ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _paper(**kw):
    base = dict(uid="", arxiv_id=None, doi=None, title="", abstract="", authors=[],
                venue="", published="", url="", pdf_url=None, sources=[])
    base.update(kw)
    base["title"] = re.sub(r"\s+", " ", base["title"]).strip()
    base["abstract"] = re.sub(r"\s+", " ", base["abstract"] or "").strip()
    return base


# ---------------- arXiv ----------------
def fetch_arxiv(queries, since: dt.date, max_results=150):
    out = []
    for q in queries:
        try:
            r = http_get("https://export.arxiv.org/api/query", params={
                "search_query": q, "sortBy": "submittedDate", "sortOrder": "descending",
                "start": 0, "max_results": max_results})
            root = ET.fromstring(r.content)
        except Exception as e:  # noqa: BLE001
            log("arXiv query failed:", q[:60], e)
            continue
        n = 0
        for e in root.findall("a:entry", ATOM):
            published = (e.findtext("a:published", "", ATOM) or "")[:10]
            if not published or dt.date.fromisoformat(published) < since:
                continue
            raw_id = e.findtext("a:id", "", ATOM).rsplit("/abs/", 1)[-1]
            aid = re.sub(r"v\d+$", "", raw_id)
            journal = e.findtext("arxiv:journal_ref", "", ATOM) or ""
            comment = e.findtext("arxiv:comment", "", ATOM) or ""
            doi = e.findtext("arxiv:doi", None, ATOM)
            out.append(_paper(
                uid=f"arxiv:{aid}", arxiv_id=aid, doi=doi.lower() if doi else None,
                title=e.findtext("a:title", "", ATOM),
                abstract=e.findtext("a:summary", "", ATOM),
                authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
                venue=journal or _venue_from_comment(comment) or "arXiv",
                published=published,
                url=f"https://arxiv.org/abs/{aid}", pdf_url=f"https://arxiv.org/pdf/{aid}",
                sources=["arXiv"], comment=comment))
            n += 1
        log(f"arXiv: {n} recent hits for query #{queries.index(q) + 1}")
        time.sleep(3.5)  # arXiv 要求请求间隔 ≥3s
    return out


def _venue_from_comment(comment: str) -> str:
    """作者常在 comment 里写 'Accepted to ACL 2026' 之类，抓出来作为质量信号。"""
    m = re.search(r"(?:accepted|to appear|published)\s+(?:at|to|in|by)\s+([^.;,]+)", comment, re.I)
    return f"arXiv · {m.group(1).strip()[:60]}" if m else ""


# ---------------- OpenAlex ----------------
def _abstract_from_inverted(inv):
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _openalex_params():
    params = {}
    if os.getenv("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    if os.getenv("OPENALEX_MAILTO"):
        params["mailto"] = os.environ["OPENALEX_MAILTO"]
    return params


def _openalex_work(w, source_tag):
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
    arxiv_id = None
    if doi and doi.startswith("10.48550/arxiv."):
        arxiv_id = doi.split("arxiv.", 1)[1]
    best = w.get("best_oa_location") or {}
    return _paper(
        uid=f"doi:{doi}" if doi else f"openalex:{w['id'].rsplit('/', 1)[-1]}",
        arxiv_id=arxiv_id, doi=doi,
        title=w.get("title") or "",
        abstract=_abstract_from_inverted(w.get("abstract_inverted_index")),
        authors=[a["author"]["display_name"] for a in w.get("authorships", [])[:12]],
        venue=src.get("display_name") or "",
        published=w.get("publication_date") or "",
        url=w.get("doi") or loc.get("landing_page_url") or w["id"],
        pdf_url=best.get("pdf_url"),
        sources=[source_tag])


def _openalex_get(filter_str, per_page=50):
    r = http_get("https://api.openalex.org/works", params={
        **_openalex_params(), "per_page": per_page, "sort": "publication_date:desc", "filter": filter_str})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("results", [])


def fetch_openalex(queries, since: dt.date, per_page=50):
    """只在标题+摘要里检索（比全文检索精确得多：同一检索词噪声从 ~200 篇降到个位数）。"""
    out = []
    for q in queries:
        q_clean = q.replace(",", " ")
        try:
            results = _openalex_get(f"from_publication_date:{since.isoformat()},type:article|preprint|review,"
                                    f"title_and_abstract.search:{q_clean}", per_page)
        except Exception as e:  # noqa: BLE001
            log("OpenAlex query failed:", q, e)
            continue
        out += [_openalex_work(w, "OpenAlex") for w in results]
        log(f"OpenAlex: {len(results)} hits for '{q}'")
        time.sleep(3)  # OpenAlex 对无 key 请求限流较严
    return out


def resolve_openalex_ids(titles):
    """按标题找种子论文在 OpenAlex 的 ID（缓存到 state/openalex_seeds.json）。"""
    cache_path = ROOT / "state" / "openalex_seeds.json"
    cache = read_json(cache_path, {})
    for t in titles:
        if t in cache:
            continue
        try:
            r = http_get("https://api.openalex.org/works", params={
                **_openalex_params(), "per_page": 5,
                "filter": f"title.search:{re.sub(r'[^A-Za-z0-9 ]+', ' ', t)}"})
            hits = r.json().get("results", []) if r.status_code == 200 else None
        except Exception as e:  # noqa: BLE001
            log("OpenAlex seed resolve failed:", t, e)
            continue
        if hits is None:
            continue
        # 同一篇论文可能有预印本和期刊两个版本，都收下，引用追踪更全
        ids = [w["id"].rsplit("/", 1)[-1] for w in hits
               if norm_title(w.get("title"))[:40] == norm_title(t)[:40]]
        cache[t] = ids
        if not ids:
            log("OpenAlex seed not found:", t)
        time.sleep(1.5)
    write_json(cache_path, cache)
    return {t: cache.get(t) or [] for t in titles}


def fetch_citing_papers(seed_titles, since: dt.date):
    """引用追踪：最近引用了种子论文（尤其是团队自己的论文）的新文章。相关性通常最高。"""
    out = []
    for title, ids in resolve_openalex_ids(seed_titles).items():
        if not ids:
            continue
        try:
            results = _openalex_get(f"cites:{'|'.join(ids)},from_publication_date:{since.isoformat()},"
                                    f"type:article|preprint|review", 50)
        except Exception as e:  # noqa: BLE001
            log("OpenAlex citations failed:", title[:40], e)
            continue
        for w in results:
            p = _openalex_work(w, "引用追踪")
            p["cites_seed"] = title.split(":")[0][:40]
            out.append(p)
        log(f"citations: {len(results)} recent papers cite '{title[:40]}'")
        time.sleep(3)
    return out


# ---------------- Semantic Scholar ----------------
S2 = "https://api.semanticscholar.org"
S2_FIELDS = "title,abstract,externalIds,publicationDate,venue,authors,url,openAccessPdf"


def _s2_headers():
    k = os.getenv("S2_API_KEY")
    return {"x-api-key": k} if k else {}


def resolve_seeds(titles):
    cache_path = ROOT / "state" / "seeds_resolved.json"
    cache = read_json(cache_path, {})
    ids = []
    for t in titles:
        if t in cache:
            if cache[t]:
                ids.append(cache[t])
            continue
        try:
            r = http_get(f"{S2}/graph/v1/paper/search/match",
                         params={"query": t, "fields": "paperId,title"}, headers=_s2_headers())
            data = r.json().get("data") if r.status_code == 200 else None
            pid = data[0]["paperId"] if data else None
            if pid and norm_title(data[0]["title"])[:40] != norm_title(t)[:40]:
                log("seed title mismatch, skipped:", t, "->", data[0]["title"])
                pid = None
        except Exception as e:  # noqa: BLE001
            log("seed resolve failed:", t, e)
            continue  # 不缓存失败，下次再试
        cache[t] = pid
        if pid:
            ids.append(pid)
        else:
            log("seed not found:", t)
        time.sleep(1.2)
    write_json(cache_path, cache)
    return ids


def fetch_s2_recommendations(seed_titles, since: dt.date, limit=100):
    if not seed_titles:
        return []
    ids = resolve_seeds(seed_titles)
    if not ids:
        return []
    try:
        r = http_post(f"{S2}/recommendations/v1/papers",
                      params={"limit": limit, "fields": S2_FIELDS},
                      json={"positivePaperIds": ids, "negativePaperIds": []},
                      headers=_s2_headers())
        recs = r.json().get("recommendedPapers", []) if r.status_code == 200 else []
        if r.status_code != 200:
            log("S2 recommendations", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log("S2 recommendations failed:", e)
        return []
    out = []
    for p in recs:
        pub = p.get("publicationDate") or ""
        if not pub or dt.date.fromisoformat(pub) < since or not p.get("abstract"):
            continue
        ext = p.get("externalIds") or {}
        aid = ext.get("ArXiv")
        doi = (ext.get("DOI") or "").lower() or None
        out.append(_paper(
            uid=f"arxiv:{aid}" if aid else (f"doi:{doi}" if doi else f"s2:{p['paperId']}"),
            arxiv_id=aid, doi=doi, title=p.get("title") or "", abstract=p.get("abstract") or "",
            authors=[a["name"] for a in (p.get("authors") or [])[:12]],
            venue=p.get("venue") or "", published=pub,
            url=f"https://arxiv.org/abs/{aid}" if aid else p.get("url"),
            pdf_url=f"https://arxiv.org/pdf/{aid}" if aid else (p.get("openAccessPdf") or {}).get("url"),
            sources=["S2-推荐"]))
    log(f"S2: {len(out)} recent recommendations from {len(ids)} seeds")
    return out


# ---------------- 去重合并 ----------------
def dedupe(papers):
    """按 arXiv ID / DOI / 标准化标题合并，同一篇论文的多来源信息合在一起。"""
    merged, index = [], {}
    for p in papers:
        keys = [k for k in (p["arxiv_id"] and f"a:{p['arxiv_id']}", p["doi"] and f"d:{p['doi']}",
                            f"t:{norm_title(p['title'])}") if k]
        hit = next((index[k] for k in keys if k in index), None)
        if hit is None:
            merged.append(p)
            hit = p
        else:
            for src in p["sources"]:
                if src not in hit["sources"]:
                    hit["sources"].append(src)
            # 正式发表的 venue 比 "arXiv" 更有信息量
            if p["venue"] and (not hit["venue"] or hit["venue"] == "arXiv") and p["venue"] != "arXiv":
                hit["venue"] = p["venue"]
            for f in ("arxiv_id", "doi", "pdf_url"):
                hit[f] = hit[f] or p[f]
            if p.get("cites_seed"):
                hit["cites_seed"] = p["cites_seed"]
            if len(p["abstract"]) > len(hit["abstract"]):
                hit["abstract"] = p["abstract"]
            if hit["arxiv_id"]:
                hit["uid"] = f"arxiv:{hit['arxiv_id']}"
                hit["url"] = f"https://arxiv.org/abs/{hit['arxiv_id']}"
        for k in keys:
            index[k] = hit
    return merged


def seen_keys(p):
    return [k for k in (p["arxiv_id"] and f"a:{p['arxiv_id']}", p["doi"] and f"d:{p['doi']}",
                        f"t:{norm_title(p['title'])}") if k]
