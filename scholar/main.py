"""每日流水线：检索 → 去重 → 关键词粗筛 → LLM 打分 → 读全文精读 → 写入网页数据。

用法：
  python -m scholar.main                 # 正式运行（需要环境变量 LLM_API_KEY）
  python -m scholar.main --no-llm        # 只测试检索和粗筛，不调用 LLM、不记录已读
  python -m scholar.main --date 2026-11-14
"""
import argparse
import datetime as dt
import re
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from . import prompts, sources
from .fulltext import get_fulltext
from .llm import LLM, USAGE
from .util import ROOT, log, read_json, write_json

DATA = ROOT / "web" / "data"
STATE = ROOT / "state"


# ---------------- 关键词粗筛 ----------------
def compile_terms(terms):
    out = []
    for term, w in terms.items():
        term = term.strip().lower()
        prefix = term.endswith("*")
        term = term.rstrip("*").strip()
        pat = r"\b" + re.escape(term).replace(r"\ ", r"[\s\-]+") + ("" if prefix else r"\b")
        out.append((term, re.compile(pat, re.I), float(w)))
    return out


def prefilter(papers, cfg):
    terms = compile_terms(cfg["terms"])
    kept = []
    for p in papers:
        text = f"{p['title']} {p['title']} {p['abstract']}"  # 标题算两次，更重要
        hits = [(t, w) for t, rx, w in terms if rx.search(text)]
        p["pre_score"] = round(sum(w for _, w in hits), 1)
        p["pre_hits"] = [t for t, _ in hits]
        if p["pre_score"] >= cfg["min_score"]:
            kept.append(p)
    kept.sort(key=lambda p: -p["pre_score"])
    return kept


# ---------------- LLM 打分 ----------------
def final_score(s, weight):
    raw = 0.4 * s["relevance"] + 0.25 * s["transfer"] + 0.2 * s["novelty"] + 0.15 * s["rigor"]
    return round(raw * (0.6 + 0.4 * weight), 2)


def llm_score(llm, papers, cfg):
    profile, lcfg = cfg["profile"], cfg["llm"]
    weights = {d["name"]: d.get("weight", 1.0) for d in profile["directions"]}
    system = prompts.score_system(profile)
    by_id = {p["uid"]: p for p in papers}
    bs = lcfg.get("score_batch_size", 8)
    batches = [papers[i:i + bs] for i in range(0, len(papers), bs)]

    def run(batch):
        try:
            return llm.chat_json(lcfg["score_model"], system, prompts.score_user(batch), max_tokens=3000)
        except RuntimeError:
            return {}

    done = 0
    with ThreadPoolExecutor(lcfg.get("concurrency", 4)) as pool:
        for res in pool.map(run, batches):
            done += 1
            log(f"scored batch {done}/{len(batches)}")
            _apply_scores(res, by_id, weights)
    return [p for p in papers if "score" in p]


def _apply_scores(res, by_id, weights):
    for r in res.get("results", []):
        p = by_id.get(r.get("id"))
        if not p:
            continue
        s = {k: float(r.get(k, 0) or 0) for k in ("relevance", "novelty", "transfer", "rigor")}
        direction = r.get("direction") if r.get("direction") in weights else "其他"
        p.update(scores=s, direction=direction, one_liner=r.get("one_liner", ""),
                 score=final_score(s, weights.get(direction, 0.5)))


# ---------------- 精读 ----------------
def digest(llm, paper, cfg):
    lcfg = cfg["llm"]
    text, src = get_fulltext(paper, lcfg.get("fulltext_max_chars", 60000))
    log(f"digest [{src}] {paper['title'][:70]}")
    try:
        d = llm.chat_json(lcfg["digest_model"], prompts.digest_system(cfg["profile"]),
                          prompts.digest_user(paper, text, src), max_tokens=4000, temperature=0.3)
    except RuntimeError:
        return None
    d["fulltext_source"] = src
    return d


# ---------------- 输出 ----------------
PUBLIC_FIELDS = ["uid", "arxiv_id", "doi", "title", "authors", "venue", "published", "url", "pdf_url",
                 "sources", "abstract", "scores", "score", "direction", "one_liner", "digest"]


def public(p):
    out = {k: p.get(k) for k in PUBLIC_FIELDS if p.get(k) is not None}
    out["authors"] = p["authors"][:10]
    return out


def update_index(date_str, day):
    idx = read_json(DATA / "index.json", {"days": []})
    idx["days"] = [d for d in idx["days"] if d["date"] != date_str]
    idx["days"].append({
        "date": date_str, "deep": len(day["deep"]), "skim": len(day["skim"]),
        "top": [p["title"] for p in day["deep"][:3]],
        "preview": day.get("preview", False)})
    idx["days"].sort(key=lambda d: d["date"], reverse=True)
    idx["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    write_json(DATA / "index.json", idx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--no-llm", action="store_true", help="只检索和粗筛，不调用 LLM")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    f, sel = cfg["fetch"], cfg["select"]
    t0 = time.time()

    # 1. 检索
    raw = []
    raw += sources.fetch_arxiv(f["arxiv_queries"], today - dt.timedelta(days=f["arxiv_lookback_days"]),
                               f.get("arxiv_max_results", 150))
    raw += sources.fetch_openalex(f["openalex_queries"], today - dt.timedelta(days=f["openalex_lookback_days"]))
    raw += sources.fetch_s2_recommendations(cfg.get("seeds") or [],
                                            today - dt.timedelta(days=f["s2_lookback_days"]))
    papers = sources.dedupe([p for p in raw if p["title"] and p["abstract"]])

    # 2. 过滤已经看过的
    seen = set(read_json(STATE / "seen.json", []))
    fresh = [p for p in papers if not any(k in seen for k in sources.seen_keys(p))]
    log(f"fetched {len(raw)} → unique {len(papers)} → unseen {len(fresh)}")

    # 3. 关键词粗筛
    cands = prefilter(fresh, cfg["prefilter"])[: cfg["llm"].get("max_candidates", 120)]
    log(f"prefilter kept {len(cands)}")

    stats = {"fetched": len(raw), "unique": len(papers), "unseen": len(fresh), "candidates": len(cands)}

    if args.no_llm:
        for p in cands:
            p["score"] = p["pre_score"]
            p["one_liner"] = "（预览模式：未调用 LLM，按关键词命中排序）命中：" + "、".join(p["pre_hits"][:6])
        ranked = cands
        deep, skim = ranked[:sel["max_deep"]], ranked[sel["max_deep"]:sel["max_deep"] + sel["max_skim"]]
    else:
        llm = LLM(cfg["llm"])
        ranked = sorted(llm_score(llm, cands, cfg), key=lambda p: -p["score"])
        deep = [p for p in ranked if p["score"] >= sel["deep_min_score"]][: sel["max_deep"]]
        rest = [p for p in ranked if p not in deep]
        skim = [p for p in rest if p["score"] >= sel["skim_min_score"]][: sel["max_skim"]]
        with ThreadPoolExecutor(cfg["llm"].get("concurrency", 4)) as pool:
            for p, d in zip(deep, pool.map(lambda p: digest(llm, p, cfg), deep)):
                p["digest"] = d
        # 已打过分的都记为已读，避免明天重复花钱
        for p in cands:
            if "score" in p:
                seen.update(sources.seen_keys(p))
        write_json(STATE / "seen.json", sorted(seen))
        stats["scored"] = len(ranked)

    stats.update(deep=len(deep), skim=len(skim), seconds=round(time.time() - t0),
                 tokens=USAGE["prompt_tokens"] + USAGE["completion_tokens"], llm_calls=USAGE["calls"])

    # 同一天多次运行：合并而不是覆盖
    path = DATA / "daily" / f"{today.isoformat()}.json"
    old = read_json(path, None)
    if old and not old.get("preview") and not args.no_llm:
        known = {p["uid"] for p in old["deep"] + old["skim"]}
        deep = [*old["deep"], *[public(p) for p in deep if p["uid"] not in known]]
        skim = [*old["skim"], *[public(p) for p in skim if p["uid"] not in known]]
        deep.sort(key=lambda p: -p.get("score", 0))
        skim.sort(key=lambda p: -p.get("score", 0))
    else:
        deep, skim = [public(p) for p in deep], [public(p) for p in skim]

    day = {"date": today.isoformat(), "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "stats": stats, "deep": deep, "skim": skim}
    if args.no_llm:
        day["preview"] = True
    write_json(path, day)
    update_index(today.isoformat(), day)
    log(f"done: {len(deep)} deep, {len(skim)} skim, {stats['tokens']} tokens, {stats['seconds']}s → {path}")


if __name__ == "__main__":
    main()
