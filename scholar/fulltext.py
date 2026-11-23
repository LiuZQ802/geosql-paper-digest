"""获取论文全文：优先 arXiv HTML（结构干净），其次 PDF，最后退回摘要。"""
import re

from bs4 import BeautifulSoup

from .util import http_get, log


def _clean(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _cut_references(text: str) -> str:
    """把参考文献部分切掉（只在后半部分找，避免误切）。"""
    m = None
    for m in re.finditer(r"\n\s*(References|REFERENCES|Bibliography)\s*\n", text):
        pass
    if m and m.start() > len(text) * 0.5:
        return text[:m.start()]
    return text


def from_arxiv_html(aid):
    r = http_get(f"https://arxiv.org/html/{aid}", retries=2)
    if r.status_code != 200 or "ltx_document" not in r.text:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    art = soup.find("article") or soup.find(class_="ltx_document")
    if not art:
        return None
    for sel in ["script", "style", "nav", ".ltx_bibliography", ".ltx_page_footer", "math annotation"]:
        for el in art.select(sel):
            el.decompose()
    # 数学公式用 alttext 代替 MathML，避免乱码
    for m in art.find_all("math"):
        m.replace_with(f" {m.get('alttext', '')} ")
    return _clean(art.get_text("\n"))


def from_pdf(url):
    import fitz  # pymupdf

    r = http_get(url, retries=2, timeout=90)
    if r.status_code != 200 or not r.content[:5] == b"%PDF-":
        return None
    with fitz.open(stream=r.content, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    return _cut_references(_clean(text))


def get_fulltext(paper, max_chars=60000):
    """返回 (正文, 来源说明)。"""
    attempts = []
    if paper.get("arxiv_id"):
        attempts.append(("arXiv HTML", lambda: from_arxiv_html(paper["arxiv_id"])))
    if paper.get("pdf_url"):
        attempts.append(("PDF", lambda: from_pdf(paper["pdf_url"])))
    if (paper.get("doi") or "").startswith("10.1016/"):
        from .access import elsevier_fulltext
        attempts.append(("Elsevier 全文", lambda: elsevier_fulltext(paper["doi"])))
    from .access import springer_pdf_url
    if springer_pdf_url(paper.get("doi")):
        attempts.append(("Springer 全文", lambda: from_pdf(springer_pdf_url(paper["doi"]))))
    for name, fn in attempts:
        try:
            text = fn()
            if text and len(text) > 3000:
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n\n[……全文过长，已截断……]"
                return text, name
        except Exception as e:  # noqa: BLE001
            log(f"fulltext {name} failed for {paper['uid']}:", e)
    return None, "仅摘要"
