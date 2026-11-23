"""给历史数据补字段，并按当前规则重新挑选精读。可重复运行，已有的不会重复处理。

  python -m scholar.backfill
补：中文标题/摘要、代码链接、一作/通讯/单位/期刊与质量分级；
按现行精读规则把够格的速览论文升为精读（会读全文写解读）；
估算以前没有记录花费的日子的花费（按当日 token 数，标为“估算”）。
"""
from concurrent.futures import ThreadPoolExecutor

import yaml

from . import vault
from .enrich import enrich
from .llm import LLM, USAGE
from .main import DATA, digest, find_code_url, select, translate, update_index
from .util import ROOT, http_get, log


def estimate_cost(llm, tokens, model="kimi-k2.6", prompt_share=0.88):
    """旧数据只记了总 token：按 88% 输入 / 12% 输出 和打分模型价格估算。"""
    try:
        data = http_get(llm.base.removesuffix("/v1") + "/api/pricing", timeout=30).json()
        row = next(r for r in data["data"] if r["model_name"] == model)
    except Exception:  # noqa: BLE001
        return None
    quota = (tokens * prompt_share + tokens * (1 - prompt_share) * row["completion_ratio"]) * row["model_ratio"]
    return {"usd": round(quota / 500000, 4), "estimated": True}


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    sel = cfg["select"]
    llm = LLM(cfg["llm"])
    files = sorted((DATA / "daily").glob("*.json"))
    days = {f: vault.load(f, None) for f in files}
    days = {f: d for f, d in days.items() if d}
    papers = [p for d in days.values() for p in d["deep"] + d["skim"]]

    for p in papers:
        if not p.get("code_url"):
            url = find_code_url(p.get("abstract", ""))
            if url:
                p["code_url"] = url
    enrich(llm, papers, cfg)

    # 按现行规则重新挑选：够格的速览升为精读（补写解读）；已有精读保持不动
    promote = []
    for d in days.values():
        room = sel["max_deep"] - len(d["deep"])
        if room <= 0:
            continue
        new_deep, _ = select(d["skim"], sel)
        for p in new_deep[:room]:
            promote.append((d, p))
    log(f"promoting {len(promote)} skim paper(s) to deep")
    with ThreadPoolExecutor(cfg["llm"].get("concurrency", 4)) as pool:
        results = list(pool.map(lambda dp: digest(llm, dp[1], cfg), promote))
    for (d, p), dg in zip(promote, results):
        if dg:
            p["digest"] = dg
            d["skim"].remove(p)
            d["deep"].append(p)
    translate(llm, papers, cfg)

    for f, d in days.items():
        for p in d["deep"] + d["skim"]:
            p.pop("_fulltext", None)
        d["deep"].sort(key=lambda p: -p.get("score", 0))
        st = d.setdefault("stats", {})
        st["deep"], st["skim"] = len(d["deep"]), len(d["skim"])
        if not st.get("cost") and st.get("tokens"):
            st["cost"] = estimate_cost(llm, st["tokens"])
        vault.save(f, d)
        update_index(d["date"], d)
    log(f"backfilled {len(papers)} papers in {len(days)} days, "
        f"{USAGE['prompt_tokens'] + USAGE['completion_tokens']} tokens, cost {llm.cost()}")


if __name__ == "__main__":
    main()
