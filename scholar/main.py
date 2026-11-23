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

from . import prompts, sources, vault
from .access import find_oa_pdfs, recover_abstracts
from .enrich import enrich
from .fulltext import get_fulltext
from .llm import LLM, USAGE
from .util import ROOT, log, read_json, write_json

CODE_RX = re.compile(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|huggingface\.co)/[\w.-]+/[\w.-]+", re.I)
CODE_SKIP = ("github.com/features", "github.com/about", "github.com/pricing")

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
        if p.get("cites_seed"):  # 引用了团队论文的新文章：直接放行并加权
            p["pre_score"] += 10
            p["pre_hits"].insert(0, f"引用 {p['cites_seed']}")
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
            return llm.chat_json(lcfg["score_model"], system, prompts.score_user(batch),
                                 max_tokens=3000, temperature=0)
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


# ---------------- 选择 ----------------
QUALITY_BONUS = {"A": 0.8, "B": 0.3, "C": 0.0}


def select(shortlist, sel):
    """精读 = 分数够 + 发表质量够（顶会顶刊，或好单位的预印本）；其余进速览。"""
    allowed = set(sel.get("deep_quality", ["A", "B"]))
    eligible = [p for p in shortlist
                if p["score"] >= sel["deep_min_score"] and (p.get("quality") or "C") in allowed]
    eligible.sort(key=lambda p: -(p["score"] + QUALITY_BONUS.get(p.get("quality"), 0)))
    deep = eligible[: sel["max_deep"]]
    skim = [p for p in shortlist if p not in deep][: sel["max_skim"]]
    return deep, skim


# ---------------- 精读 ----------------
def digest(llm, paper, cfg):
    lcfg = cfg["llm"]
    if "_fulltext" not in paper:  # 信息补全阶段通常已经抓过，复用
        paper["_fulltext"] = get_fulltext(paper, lcfg.get("fulltext_max_chars", 60000))
    text, src = paper["_fulltext"]
    log(f"digest [{src}] {paper['title'][:70]}")
    if text and not paper.get("code_url"):
        paper["code_url"] = find_code_url(text[:20000])  # 只看正文前部，避免抓到参考文献里别人的仓库
    try:
        d = llm.chat_json(lcfg["digest_model"], prompts.digest_system(cfg["profile"]),
                          prompts.digest_user(paper, text, src), max_tokens=4000, temperature=0.3)
    except RuntimeError:
        return None
    d["fulltext_source"] = src
    return d


def find_code_url(text):
    for m in CODE_RX.finditer(text or ""):
        url = m.group(0).rstrip(".,;)")
        if not any(s in url.lower() for s in CODE_SKIP):
            return url
    return None


# ---------------- 中文翻译 ----------------
def translate(llm, papers, cfg, batch_size=4):
    """给要展示的论文补中文标题和中文摘要（只翻译最终入选的，省钱）。"""
    todo = [p for p in papers if not p.get("title_zh") or (p.get("abstract") and not p.get("abstract_zh"))]
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    by_id = {p["uid"]: p for p in todo}

    def run(batch):
        try:
            return llm.chat_json(cfg["llm"]["score_model"], prompts.TRANSLATE_SYSTEM,
                                 prompts.translate_user(batch), max_tokens=8000, temperature=0)
        except RuntimeError:
            return {}

    with ThreadPoolExecutor(cfg["llm"].get("concurrency", 4)) as pool:
        for res in pool.map(run, batches):
            for r in res.get("results", []):
                p = by_id.get(r.get("id"))
                if p and r.get("title_zh"):
                    p["title_zh"] = r["title_zh"]
                    if r.get("abstract_zh") and p.get("abstract"):
                        p["abstract_zh"] = r["abstract_zh"]
    log(f"translated {sum(1 for p in todo if p.get('title_zh'))}/{len(todo)}")


# ---------------- 输出 ----------------
PUBLIC_FIELDS = ["uid", "arxiv_id", "doi", "title", "title_zh", "authors", "venue", "published", "url",
                 "pdf_url", "code_url", "cites_seed", "sources", "abstract", "abstract_zh", "scores", "score",
                 "direction", "one_liner", "digest", "people", "venue_norm", "venue_status", "venue_tier",
                 "inst_tier", "quality", "quality_reason", "no_abstract", "abstract_src"]


def public(p):
    out = {k: p.get(k) for k in PUBLIC_FIELDS if p.get(k) is not None}
    out["authors"] = p["authors"][:10]
    return out


def update_index(date_str, day):
    idx = vault.load(DATA / "index.json", {"days": []})
    idx["days"] = [d for d in idx["days"] if d["date"] != date_str]
    idx["days"].append({
        "date": date_str, "deep": len(day["deep"]), "skim": len(day["skim"]),
        "top": [p["title"] for p in day["deep"][:3]],
        "cost_usd": (day["stats"].get("cost") or {}).get("usd"),
        "cost_est": (day["stats"].get("cost") or {}).get("estimated", False),
        "preview": day.get("preview", False)})
    idx["days"].sort(key=lambda d: d["date"], reverse=True)
    idx["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    vault.save(DATA / "index.json", idx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--no-llm", action="store_true", help="只检索和粗筛，不调用 LLM")
    ap.add_argument("--scopus-days", type=int, help="临时放宽 Scopus 回看天数（补检用）")
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
    raw += sources.fetch_scopus(f.get("scopus_queries") or [],
                                today - dt.timedelta(days=args.scopus_days or f.get("scopus_lookback_days", 7)))
    raw += sources.fetch_citing_papers(cfg.get("team_papers") or [],
                                       today - dt.timedelta(days=f.get("citation_lookback_days", 60)))
    for p in raw:
        p["code_url"] = p.get("code_url") or find_code_url(f"{p['abstract']} {p.get('comment', '')}")
    papers = sources.dedupe([p for p in raw if p["title"]])

    # 2. 过滤已经看过的
    seen = set(read_json(STATE / "seen.json", []))
    fresh = [p for p in papers if not any(k in seen for k in sources.seen_keys(p))]
    log(f"fetched {len(raw)} → unique {len(papers)} → unseen {len(fresh)}")

    # 3. 关键词粗筛
    cands = prefilter(fresh, cfg["prefilter"])[: cfg["llm"].get("max_candidates", 120)]
    log(f"prefilter kept {len(cands)}")
    if not args.no_llm:
        recover_abstracts(cands)  # 付费期刊（尤其 Elsevier）常缺摘要：用 Elsevier API / S2 / Crossref 补

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
        # 短名单：够得上速览的论文，补全作者/单位/期刊并分级
        shortlist = [p for p in ranked if p["score"] >= sel["skim_min_score"]][: sel.get("max_shortlist", 25)]
        find_oa_pdfs(shortlist)  # Scopus/OpenAlex 来的论文补开放获取 PDF（PVLDB、作者自存稿、arXiv 版本）
        enrich(llm, shortlist, cfg)
        deep, skim = select(shortlist, sel)
        with ThreadPoolExecutor(cfg["llm"].get("concurrency", 4)) as pool:
            for p, d in zip(deep, pool.map(lambda p: digest(llm, p, cfg), deep)):
                p["digest"] = d
        # 解读失败（多为接口临时故障）的论文不记为已读、不进今天的精读，明天自动重试
        failed = {p["uid"] for p in deep if not p.get("digest")}
        if failed:
            log(f"digest failed for {len(failed)} paper(s), will retry next run")
        deep = [p for p in deep if p["uid"] not in failed]
        translate(llm, deep + skim, cfg)
        # 已打过分的都记为已读，避免明天重复花钱
        for p in cands:
            if "score" in p and p["uid"] not in failed:
                seen.update(sources.seen_keys(p))
        write_json(STATE / "seen.json", sorted(seen))
        stats["scored"] = len(ranked)

    stats.update(deep=len(deep), skim=len(skim), seconds=round(time.time() - t0),
                 tokens=USAGE["prompt_tokens"] + USAGE["completion_tokens"], llm_calls=USAGE["calls"])
    if not args.no_llm:
        stats["cost"] = llm.cost()
        if stats["cost"]:
            log(f"cost ≈ ${stats['cost']['usd']:.3f} (relay quota)")

    # 同一天多次运行：合并而不是覆盖
    path = DATA / "daily" / f"{today.isoformat()}.json"
    old = vault.load(path, None)
    if old and not old.get("preview") and not args.no_llm:
        # 同一天多次运行：花费、token 累加
        old_cost = (old.get("stats") or {}).get("cost") or {}
        if old_cost.get("usd") is not None:
            new_cost = stats.get("cost") or {"usd": 0}
            stats["cost"] = {**new_cost, "usd": round(new_cost["usd"] + old_cost["usd"], 4),
                             "estimated": bool(old_cost.get("estimated"))}
        stats["tokens"] = stats.get("tokens", 0) + (old.get("stats") or {}).get("tokens", 0)
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
    vault.save(path, day)
    update_index(today.isoformat(), day)
    log(f"done: {len(deep)} deep, {len(skim)} skim, {stats['tokens']} tokens, {stats['seconds']}s → {path}")


if __name__ == "__main__":
    main()
