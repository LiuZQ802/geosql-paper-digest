"""论文信息补全：一作、通讯作者、单位、期刊/会议，以及发表质量分级。

数据来源：
- OpenAlex：作者、机构、is_corresponding 标记，以及论文的所有版本（arXiv 论文正式发表后能拿到期刊/会议）
- arXiv 正文开头（作者、单位、脚注里的通讯作者标记）
再交给 LLM 统一整理、判断档次。只处理候选短名单，成本很低。
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from .fulltext import get_fulltext
from .sources import _openalex_params
from .util import http_get, log


def openalex_record(p):
    doi = p.get("doi") or (f"10.48550/arxiv.{p['arxiv_id']}" if p.get("arxiv_id") else None)
    if not doi:
        return None
    try:
        r = http_get(f"https://api.openalex.org/works/doi:{doi}", retries=2, timeout=30,
                     params={**_openalex_params(), "select": "authorships,primary_location,locations"})
        if r.status_code != 200:
            return None
        w = r.json()
    except Exception:  # noqa: BLE001
        return None
    authors = []
    for a in w.get("authorships") or []:
        insts = [i.get("display_name") for i in a.get("institutions") or [] if i.get("display_name")]
        authors.append({"name": (a.get("author") or {}).get("display_name"), "position": a.get("author_position"),
                        "corresponding": bool(a.get("is_corresponding")), "institutions": insts})
    venues = []
    for loc in w.get("locations") or []:
        src = loc.get("source") or {}
        if src.get("display_name") and src.get("type") in ("journal", "conference", "book series"):
            venues.append(src["display_name"])
    return {"authors": authors[:15], "published_venues": sorted(set(venues))}


def gather(p, fulltext_chars):
    """收集给 LLM 的原始信息；arXiv 论文顺便把全文缓存下来给精读用。"""
    oa = openalex_record(p)
    front = ""
    if p.get("arxiv_id") or p.get("pdf_url"):
        if "_fulltext" not in p:
            p["_fulltext"] = get_fulltext(p, fulltext_chars)
        text = p["_fulltext"][0] or ""
        front = text[:3500]
        # 通讯作者常写在脚注里，脚注有时被排到正文后面：把含关键词的片段也带上
        for kw in ("Corresponding author", "corresponding author", "Correspondence", "通讯作者"):
            i = text.find(kw)
            if i > 3500:
                front += "\n…\n" + text[max(0, i - 200):i + 300]
                break
    time.sleep(0.3)
    return {"id": p["uid"], "title": p["title"], "venue_raw": p.get("venue", ""),
            "arxiv_comment": p.get("comment", ""), "openalex": oa, "front_matter": front}


SYSTEM = """你负责整理论文的作者与发表信息，并判断发表质量。只根据给定材料，不要猜测。

对每篇论文输出：
- first_author：第一作者姓名（保持原文写法）。
- first_affil：第一作者单位，用简洁通行的名称（中国机构用中文，如“武汉大学”；外国机构用英文常用名，如“Stanford University”）。
- corresponding：通讯作者列表 [{{"name": "...", "affil": "..."}}]。只有材料里明确标注（Corresponding author、通讯作者、
  信封符号✉、“*”号且注明 corresponding、OpenAlex 的 corresponding 标记）才填；没有明确标注就给空列表 []，不要猜。
- institutions：所有作者单位去重后的列表，最多 6 个，按出现顺序。
- venue：规范化的发表信息。已正式发表或已接收的写会议/期刊常用简称+年份，如“ACL 2026”“EMNLP 2026 Findings”
  “IJGIS”“PVLDB”；arXiv comment 里写了 accepted/to appear 的算已接收；都没有就写“arXiv 预印本”。
  注意：机构仓库、Zenodo、SSRN、Research Square 不算正式发表。
- venue_status：published | accepted | preprint。
- venue_tier：top | good | other
  top = 该领域公认顶会顶刊（参考名单见下）；good = 正规同行评审的知名会议/期刊（如 CCF-B/中科院二区及以上）；
  other = 预印本、workshop、普通期刊或无法判断。
- inst_tier：top | good | other
  top = 世界一流大学（如 QS 前 100、国内 C9 及同档）、顶级研究机构（如中国科学院、马普所）或顶级工业界实验室
  （Google、Microsoft、Meta、OpenAI、Anthropic、阿里巴巴、腾讯、字节跳动、华为、百度等的研究部门）；
  good = 知名大学或研究机构；other = 其他或无法判断。以最强的作者单位为准。
- quality：A | B | C
  A = venue_tier 为 top，或者 inst_tier 为 top；B = venue_tier 或 inst_tier 为 good；C = 其余。
- quality_reason：一句话说明分级依据（≤30 字），如“ACL 2026 主会”“北京大学 + 微软亚洲研究院，预印本”。

【顶会顶刊参考名单】
{venues}

严格输出 JSON：{{"results": [{{"id": "...", "first_author": "...", "first_affil": "...", "corresponding": [],
 "institutions": [], "venue": "...", "venue_status": "...", "venue_tier": "...", "inst_tier": "...",
 "quality": "...", "quality_reason": "..."}}]}}"""


def enrich(llm, papers, cfg, batch_size=5):
    """给论文补 people / venue / quality 字段（原地修改）。"""
    todo = [p for p in papers if not p.get("quality")]
    if not todo:
        return
    fchars = cfg["llm"].get("fulltext_max_chars", 60000)
    with ThreadPoolExecutor(4) as pool:
        raw = list(pool.map(lambda p: gather(p, fchars), todo))
    system = SYSTEM.format(venues="、".join(cfg.get("quality", {}).get("top_venues", [])))
    by_id = {p["uid"]: p for p in todo}
    batches = [raw[i:i + batch_size] for i in range(0, len(raw), batch_size)]

    def run(batch):
        try:
            return llm.chat_json(cfg["llm"]["score_model"], system,
                                 "论文材料：\n" + json.dumps(batch, ensure_ascii=False), max_tokens=4000, temperature=0)
        except RuntimeError:
            return {}

    with ThreadPoolExecutor(cfg["llm"].get("concurrency", 4)) as pool:
        for res in pool.map(run, batches):
            for r in res.get("results", []):
                p = by_id.get(r.get("id"))
                if not p:
                    continue
                p["people"] = {k: r.get(k) for k in ("first_author", "first_affil", "corresponding", "institutions")}
                p["venue_norm"] = r.get("venue")
                for k in ("venue_status", "venue_tier", "inst_tier", "quality", "quality_reason"):
                    p[k] = r.get(k)
    log(f"enriched {sum(1 for p in todo if p.get('quality'))}/{len(todo)}: "
        + ", ".join(f"{q}={sum(1 for p in todo if p.get('quality') == q)}" for q in "ABC"))
