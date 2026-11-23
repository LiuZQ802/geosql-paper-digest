"""给历史数据补字段：中文标题/摘要、代码链接。可重复运行，已有的不会重复翻译。

  python -m scholar.backfill
"""
import yaml

from . import vault
from .llm import LLM, USAGE
from .main import DATA, find_code_url, translate
from .util import ROOT, log


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    llm = LLM(cfg["llm"])
    files = sorted((DATA / "daily").glob("*.json"))
    days = {f: vault.load(f, None) for f in files}
    papers = [p for d in days.values() if d for p in d["deep"] + d["skim"]]
    for p in papers:
        p.setdefault("code_url", find_code_url(p.get("abstract", "")))
        if not p["code_url"]:
            p.pop("code_url")
    translate(llm, papers, cfg)
    for f, d in days.items():
        if d:
            vault.save(f, d)
    log(f"backfilled {len(papers)} papers in {len(files)} days, {USAGE['prompt_tokens'] + USAGE['completion_tokens']} tokens")


if __name__ == "__main__":
    main()
