# 地学 SQL 文献日报

每天自动检索与“LLM 生成空间 / 地学 SQL”相关的新论文，筛出真正值得看的几篇，读全文后写成中文解读，发布成网页。

## 工作流程

```
检索 ── arXiv（最新预印本）+ OpenAlex（正式发表）+ Semantic Scholar（种子论文相似推荐）
 │
去重 ── 按 arXiv ID / DOI / 标题合并；已经打过分的论文不再重复处理（state/seen.json）
 │
粗筛 ── 关键词加权打分，免费，把明显无关的剔掉
 │
打分 ── LLM 只看标题和摘要，按 相关性 / 可迁移性 / 新颖性 / 严谨性 打分（标准从严）
 │
精读 ── 综合分 ≥ 7 的前 5 篇：抓全文（arXiv HTML → PDF → 摘要），LLM 写中文解读
 │       一句话总结 · 要解决的问题 · 方法 · 证据 · 价值 · 对你研究的启发 · 局限 · 阅读建议
速览 ── 综合分 ≥ 5.5 的另外 10 篇：只写一句话点评
 │
发布 ── 写入 web/data/，GitHub Pages 自动部署
```

## 部署（一次性设置）

1. 在 GitHub 新建一个**公开**仓库，把本目录推送上去（默认分支 `main`）。
2. 仓库 **Settings → Secrets and variables → Actions → New repository secret**：
   - `LLM_API_KEY`：LLM 接口的 API key（必需）
   - `LLM_BASE_URL`：OpenAI 兼容接口地址，例如 `https://xxx/v1`（必需）
   - `S2_API_KEY`：Semantic Scholar 的 API key（可选；不填也能用，但偶尔会被限流）
   - `OPENALEX_API_KEY`：OpenAlex 的 API key（可选）
3. **Settings → Pages → Build and deployment → Source** 选 **GitHub Actions**。
4. **Actions → daily-digest → Run workflow**，手动跑第一次。跑完后，页面地址显示在 Pages 设置页。

之后每天北京时间 09:30 自动运行。

## 本地运行

```bash
pip install -r requirements.txt
set LLM_API_KEY=sk-xxx              # PowerShell: $env:LLM_API_KEY="sk-xxx"
set LLM_BASE_URL=https://xxx/v1
python -m scholar.main              # 正式运行
python -m scholar.main --no-llm     # 只测试检索和粗筛，不花钱
python -m http.server -d web 8000   # 浏览器打开 http://localhost:8000
```

## 调整

所有配置都在 [config.yaml](config.yaml)：

| 想要…… | 改哪里 |
|---|---|
| 让解读里的“启发”更贴合当前工作 | `profile.description` |
| 调整各方向的推荐比重 | `profile.directions[].weight` |
| 让推荐更准 | `seeds` 里加入你们团队和你认为最关键的论文标题 |
| 推荐太少或太多 | `select.deep_min_score` / `skim_min_score` |
| 换模型或中转站 | Secret `LLM_BASE_URL` / `llm.score_model` / `llm.digest_model` |
| 增加检索词 | `fetch.arxiv_queries` / `fetch.openalex_queries` |

## 成本

每天大约调用 15 次打分（每次 8 篇摘要）和 5 次精读（每次一篇全文，约 2 万 token），合计 20–30 万 token。
运行日志和 `web/data/daily/*.json` 的 `stats.tokens` 字段里记录了每天实际用了多少 token。
