"""网页端提交的单篇论文解析（链接 / DOI / arXiv / 上传的 PDF）。

由 GitHub Actions 的 workflow_dispatch 触发：
  REQUEST='{"v":1,"iv":..,"ct":..}'  python -m scholar.manual --id <id> [--file <加密的 PDF>]
REQUEST 是网页用站点密钥加密的 JSON：{"kind": "url"|"pdf", "url": "...", "mode": "auto"|"deep"|"skim", "name": "..."}
结果写到 web/data/uploads/<id>.json（加密），并重建 web/data/uploads/index.json。
"""
import argparse
import datetime as dt
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from . import vault
from .access import find_oa_pdfs, recover_abstracts
from .enrich import enrich
from .fulltext import _clean, _cut_references
from .llm import LLM, USAGE
from .main import DATA, digest, find_code_url, llm_score, public, translate
from .sources import ATOM, _paper
from .util import ROOT, http_get, log

UPLOADS = DATA / "uploads"
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
              "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8"}
ARXIV_RX = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
DOI_RX = re.compile(r"(10\.\d{4,9}/[^\s\"'<>?#]+)", re.I)


class UserError(Exception):
    """要原样展示给用户的错误（链接打不开、PDF 读不出文字等）。"""


# ---------------- 元数据获取 ----------------
def from_arxiv(aid):
    r = http_get("https://export.arxiv.org/api/query", params={"id_list": aid}, retries=2)
    e = ET.fromstring(r.content).find("a:entry", ATOM)
    if e is None or not e.findtext("a:title", "", ATOM).strip():
        raise UserError(f"arXiv 上找不到论文 {aid}，请检查编号。")
    return _paper(uid=f"arxiv:{aid}", arxiv_id=aid, title=e.findtext("a:title", "", ATOM),
                  abstract=e.findtext("a:summary", "", ATOM),
                  authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
                  venue="arXiv", published=(e.findtext("a:published", "", ATOM) or "")[:10],
                  url=f"https://arxiv.org/abs/{aid}", pdf_url=f"https://arxiv.org/pdf/{aid}",
                  sources=["手动添加"], comment=e.findtext("arxiv:comment", "", ATOM) or "")


def from_doi(doi):
    doi = doi.rstrip(".,;)").lower()
    r = http_get(f"https://api.crossref.org/works/{doi}", retries=2, timeout=30,
                 headers={"User-Agent": "geosql-paper-digest/1.0 (academic literature monitor)"})
    if r.status_code != 200:
        raise UserError(f"DOI {doi} 查不到（Crossref 返回 {r.status_code}），请检查 DOI 是否正确。")
    m = r.json()["message"]
    date = (m.get("published") or m.get("issued") or {}).get("date-parts", [[None]])[0]
    p = _paper(uid=f"doi:{doi}", doi=doi, title=(m.get("title") or [""])[0],
               abstract=re.sub(r"<[^>]+>", " ", m.get("abstract") or ""),
               authors=[f"{a.get('given', '')} {a.get('family', '')}".strip() for a in m.get("author", [])][:12],
               venue=(m.get("container-title") or [""])[0],
               published="-".join(f"{x:02d}" if i else str(x) for i, x in enumerate(date) if x) if date[0] else "",
               url=f"https://doi.org/{doi}", sources=["手动添加"])
    if doi.startswith("10.48550/arxiv."):
        return from_arxiv(doi.split("arxiv.", 1)[1])
    return p


def pdf_text(data: bytes):
    import fitz  # pymupdf

    if not data[:5] == b"%PDF-":
        raise UserError("这不是有效的 PDF 文件。")
    with fitz.open(stream=data, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    text = _cut_references(_clean(text))
    if len(text) < 1500:
        raise UserError("PDF 里几乎没有可提取的文字（可能是扫描版图片），暂时无法解析。")
    return text


META_SYSTEM = """从论文正文开头提取题录信息，只根据给定文字，不要编造。严格输出 JSON：
{"title": "论文标题", "abstract": "摘要原文（找不到就空字符串）", "authors": ["作者1", "作者2"],
 "doi": "正文里印着的 DOI（没有就空字符串）", "venue": "期刊/会议名（正文里写了才填）", "year": "年份或空字符串"}"""


def from_pdf_bytes(llm, cfg, data, name):
    text = pdf_text(data)
    meta = llm.chat_json(cfg["llm"]["score_model"], META_SYSTEM, text[:8000], max_tokens=2500, temperature=0)
    doi = (meta.get("doi") or "").strip().lower() or None
    if doi and not DOI_RX.fullmatch(doi):
        doi = None
    p = None
    if doi:
        try:
            p = from_doi(doi)
        except Exception:  # noqa: BLE001  正文印的 DOI 查不到就算了，用 PDF 自己的信息
            p = None
    if p is None:
        title = meta.get("title") or Path(name or "upload").stem
        p = _paper(uid=f"upload:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:60]}", doi=doi, title=title,
                   abstract=meta.get("abstract") or "", authors=meta.get("authors") or [],
                   venue=meta.get("venue") or "", published=str(meta.get("year") or ""),
                   url=f"https://doi.org/{doi}" if doi else "", sources=["上传 PDF"])
    if not p["abstract"] and meta.get("abstract"):
        p["abstract"] = meta["abstract"]
    p["sources"] = ["上传 PDF"]
    p["_fulltext"] = (text[: cfg["llm"].get("fulltext_max_chars", 60000)], "上传的 PDF")
    return p


def from_url(url, cfg):
    """链接 → 论文。打不开时抛出带原因的 UserError，提示用户改为上传 PDF。"""
    m = ARXIV_RX.search(url)
    if m and ("arxiv" in url.lower() or re.fullmatch(r"\s*\d{4}\.\d{4,5}(v\d+)?\s*", url)):
        return from_arxiv(m.group(1))
    m = DOI_RX.search(url)
    if m and ("doi.org" in url.lower() or url.strip().lower().startswith("10.")):
        return from_doi(m.group(1))
    if not re.match(r"https?://", url.strip(), re.I):
        raise UserError("无法识别这个链接：请粘贴完整网址（以 http 开头）、DOI（10.xxxx/...）或 arXiv 编号。")
    try:
        import requests
        r = requests.get(url.strip(), headers=BROWSER_UA, timeout=40, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        raise UserError(f"链接无法访问（{type(e).__name__}）。请确认链接正确；如果是付费论文，请下载 PDF 后上传。") from e
    if r.status_code in (401, 402, 403, 429) or r.status_code >= 500:
        raise UserError(f"链接无法访问（HTTP {r.status_code}）：这个网站拦截了程序访问或需要机构权限。"
                        "请在浏览器里下载 PDF 后上传。")
    if r.status_code != 200:
        raise UserError(f"链接无法访问（HTTP {r.status_code}），请检查链接是否正确。")
    if r.content[:5] == b"%PDF-":
        raise _PdfFromUrl(r.content, url)
    soup = BeautifulSoup(r.text, "html.parser")
    metas = {(t.get("name") or t.get("property") or "").lower(): t.get("content") for t in soup.find_all("meta")}
    doi = metas.get("citation_doi") or metas.get("dc.identifier") or metas.get("prism.doi")
    m = DOI_RX.search(doi or "") or DOI_RX.search(r.url) or DOI_RX.search(r.text[:20000])
    try:
        p = from_doi(m.group(1)) if m else None
    except UserError:  # 页面里的 DOI 查不到，就用页面自己的元信息
        p = None
    if p is None:
        title = metas.get("citation_title") or metas.get("og:title") or (soup.title.string if soup.title else "")
        if not title:
            raise UserError("打开了链接，但页面里找不到论文信息。请换成论文的 DOI、arXiv 链接，或上传 PDF。")
        p = _paper(uid=f"url:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:60]}", title=title,
                   abstract=metas.get("citation_abstract") or metas.get("dc.description") or metas.get("og:description") or "",
                   authors=[t.get("content") for t in soup.find_all("meta", attrs={"name": "citation_author"})][:12],
                   venue=metas.get("citation_journal_title") or metas.get("citation_conference_title") or "",
                   published=metas.get("citation_publication_date") or metas.get("citation_date") or "",
                   url=url, sources=["手动添加"])
    if metas.get("citation_pdf_url") and not p.get("pdf_url"):
        p["pdf_url"] = metas["citation_pdf_url"]
    return p


class _PdfFromUrl(Exception):
    def __init__(self, data, url):
        super().__init__(url)
        self.data, self.url = data, url


# ---------------- 主流程 ----------------
def analyze(req, file_blob, cfg, llm):
    notes = []
    if req["kind"] == "pdf":
        if not file_blob:
            raise UserError("上传的 PDF 没有找到，请重新上传。")
        p = from_pdf_bytes(llm, cfg, vault.decrypt_bytes(file_blob), req.get("name"))
    else:
        try:
            p = from_url(req["url"], cfg)
        except _PdfFromUrl as e:
            p = from_pdf_bytes(llm, cfg, e.data, e.url.rsplit("/", 1)[-1])
            p["url"] = p.get("url") or e.url

    recover_abstracts([p])
    find_oa_pdfs([p])
    if not p.get("abstract") and "_fulltext" not in p:
        notes.append("没有拿到摘要")
    p["code_url"] = p.get("code_url") or find_code_url(f"{p.get('abstract', '')} {p.get('comment', '')}")

    scored = llm_score(llm, [p], cfg)
    if not scored:
        raise RuntimeError("打分失败（模型接口暂时不可用），请稍后重试。")

    sel, mode = cfg["select"], req.get("mode", "auto")
    final = mode if mode in ("deep", "skim") else ("deep" if p["score"] >= sel["deep_min_score"] else "skim")
    reason = {"deep": "你选择了精读", "skim": "你选择了速览"}.get(mode) or (
        f"自动判断：综合分 {p['score']:.1f} {'≥' if final == 'deep' else '<'} 精读线 {sel['deep_min_score']}")

    enrich(llm, [p], cfg)
    if final == "deep":
        p["digest"] = digest(llm, p, cfg) or digest(llm, p, cfg)  # 接口偶发失败：重试一次
        if not p["digest"]:
            final, reason = "skim", reason + "；但精读解读生成失败（模型接口暂时不可用），先按速览展示"
        elif p["digest"].get("fulltext_source") == "仅摘要":
            notes.append("没能获取全文，精读只基于摘要；需要完整解读请上传 PDF")
    translate(llm, [p], cfg)
    return p, final, reason, notes


def write_result(rid, item):
    vault.save(UPLOADS / f"{rid}.json", item)
    rebuild_index()


def rebuild_index():
    items = []
    for f in sorted(UPLOADS.glob("*.json")):
        if f.name == "index.json":
            continue
        it = vault.load(f, None)
        if it:
            items.append({k: it.get(k) for k in ("id", "status", "submitted_at", "finished_at", "title", "mode_final")})
    items.sort(key=lambda x: x.get("submitted_at") or "", reverse=True)
    vault.save(UPLOADS / "index.json", {"items": items,
                                         "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="请求编号")
    ap.add_argument("--file", help="加密的 PDF 文件路径（上传 PDF 时）")
    ap.add_argument("--reindex", action="store_true", help="只重建 uploads/index.json")
    args = ap.parse_args()
    if args.reindex:
        return rebuild_index()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    rid = re.sub(r"[^\w-]", "", args.id or "")[:40]
    if not rid:
        raise SystemExit("缺少 --id")
    now = lambda: dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    item = {"id": rid, "status": "error", "submitted_at": now()}
    try:
        req = vault.decrypt_blob(json.loads(os.environ["REQUEST"]))
        item.update(kind=req.get("kind"), input=req.get("url") or req.get("name"), mode_requested=req.get("mode", "auto"),
                    submitted_at=req.get("submitted_at") or item["submitted_at"])
        blob = json.loads(Path(args.file).read_text(encoding="utf-8")) if args.file and Path(args.file).exists() else None
        llm = LLM(cfg["llm"])
        p, final, reason, notes = analyze(req, blob, cfg, llm)
        item.update(status="done", mode_final=final, reason=reason, notes=notes, title=p.get("title_zh") or p["title"],
                    paper=public(p), cost=llm.cost())
        log(f"done: {final} | {p['title'][:70]} | {reason}")
    except UserError as e:
        item.update(message=str(e), title=item.get("input"))
        log("user error:", e)
    except Exception as e:  # noqa: BLE001
        item.update(message=f"解析出错：{e}。可以稍后重试，或换成上传 PDF。", title=item.get("input"))
        log("error:", repr(e))
    item["finished_at"] = now()
    item["tokens"] = USAGE["prompt_tokens"] + USAGE["completion_tokens"]
    write_result(rid, item)


if __name__ == "__main__":
    main()
