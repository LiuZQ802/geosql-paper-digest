"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 隐私模式等 */ } },
};

const VERDICT = { must_read: "必读", worth_reading: "值得读", skim_only: "速览即可" };
const SCORE_LABEL = { relevance: "相关", transfer: "可迁移", novelty: "新颖", rigor: "严谨" };

const state = {
  index: null, days: new Map(), date: null, view: "day", dir: "全部", query: "",
  stars: store.get("stars", {}),
};

// ---------------- data ----------------
async function loadJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
async function loadDay(date) {
  if (!state.days.has(date)) state.days.set(date, await loadJSON(`data/daily/${date}.json`));
  return state.days.get(date);
}
async function loadAllDays() {
  await Promise.all(state.index.days.map((d) => loadDay(d.date).catch(() => null)));
}

// ---------------- render helpers ----------------
function fmtDate(s) {
  const d = new Date(s + "T00:00:00");
  const wk = "日一二三四五六"[d.getDay()];
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日 · 星期${wk}`;
}
function hl(text) {
  const s = esc(text);
  if (!state.query) return s;
  const q = state.query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return s.replace(new RegExp(q, "gi"), (m) => `<mark>${m}</mark>`);
}
function list(arr) {
  if (!arr || !arr.length) return "<p>文中未说明</p>";
  if (!Array.isArray(arr)) return `<p>${hl(arr)}</p>`;
  return `<ul>${arr.map((x) => `<li>${hl(x)}</li>`).join("")}</ul>`;
}
function authors(p) {
  const a = p.authors || [];
  return esc(a.slice(0, 4).join(", ") + (a.length > 4 ? " 等" : ""));
}
function starBtn(p) {
  const on = !!state.stars[p.uid];
  return `<button class="star ${on ? "on" : ""}" data-star="${esc(p.uid)}" title="${on ? "取消收藏" : "收藏"}" aria-pressed="${on}">${on ? "★" : "☆"}</button>`;
}
function links(p) {
  const out = [`<a href="${esc(p.url)}" target="_blank" rel="noopener">原文</a>`];
  if (p.pdf_url) out.push(`<a href="${esc(p.pdf_url)}" target="_blank" rel="noopener">PDF</a>`);
  if (p.arxiv_id) out.push(`<a href="https://www.semanticscholar.org/arxiv/${esc(p.arxiv_id)}" target="_blank" rel="noopener">引用</a>`);
  return `<span class="links">${out.join("")}</span>`;
}

function scoreBars(p) {
  if (!p.scores) return "";
  const bars = Object.entries(SCORE_LABEL).map(([k, label]) => {
    const v = p.scores[k] ?? 0;
    return `<div class="bar"><label>${label}<b>${v}</b></label><div class="track"><div class="fill" style="width:${v * 10}%"></div></div></div>`;
  }).join("");
  return `<div class="scores">${bars}<div class="total"><b>${p.score.toFixed(1)}</b><span>综合分</span></div></div>`;
}

function deepCard(p, i, date) {
  const d = p.digest;
  const meta = `
    <div class="card-meta">
      <span class="rank">#${i + 1}</span>
      ${p.direction ? `<span class="tag-dir">${esc(p.direction)}</span>` : ""}
      ${d?.verdict ? `<span class="verdict ${esc(d.verdict)}">${VERDICT[d.verdict] || esc(d.verdict)}</span>` : ""}
      ${date ? `<span class="src-note">${esc(date)}</span>` : ""}
      <span class="spacer"></span>${starBtn(p)}
    </div>`;
  const head = `
    <h4><a href="${esc(p.url)}" target="_blank" rel="noopener">${hl(p.title)}</a></h4>
    <div class="byline">${authors(p)} · <span class="venue">${esc(p.venue || "")}</span> · ${esc(p.published || "")}</div>`;

  if (!d) {
    return `<article class="card">${meta}${head}
      ${p.one_liner ? `<p class="tldr">${hl(p.one_liner)}</p>` : ""}${scoreBars(p)}
      <details class="more"><summary>原文摘要</summary><p class="abstract">${hl(p.abstract)}</p></details>
      <div class="foot-row">${links(p)}</div></article>`;
  }
  return `<article class="card">${meta}${head}
    <p class="tldr">${hl(d.tldr)}</p>
    ${scoreBars(p)}
    <details class="more" open>
      <summary>完整解读</summary>
      <div class="digest">
        <div class="blk"><h5>它要解决什么问题</h5><p>${hl(d.problem)}</p></div>
        <div class="two">
          <div class="blk"><h5>怎么做的</h5>${list(d.method)}</div>
          <div class="blk"><h5>证据：关键结果</h5>${list(d.evidence)}</div>
        </div>
        <div class="blk"><h5>价值与亮点</h5><p>${hl(d.value)}</p></div>
        <div class="blk insight"><h5>对你研究的启发</h5>${list(d.inspiration)}</div>
        <div class="two">
          <div class="blk limit"><h5>局限与疑点</h5>${list(d.limitations)}</div>
          <div class="blk"><h5>阅读建议</h5><p>${hl(d.verdict_reason)}</p></div>
        </div>
      </div>
      <details class="more"><summary>原文摘要</summary><p class="abstract">${hl(p.abstract)}</p></details>
    </details>
    <div class="foot-row">
      <div class="tags">${(d.tags || []).map((t) => `<span>${hl(t)}</span>`).join("")}</div>
      <span class="src-note">解读依据：${esc(d.fulltext_source || "")}</span>
      ${links(p)}
    </div></article>`;
}

function skimRow(p, date) {
  return `<div class="skim-row">
    <div class="s">${p.score != null ? p.score.toFixed(1) : ""}</div>
    <div>
      <h4><a href="${esc(p.url)}" target="_blank" rel="noopener">${hl(p.title)}</a></h4>
      <p class="ol">${hl(p.one_liner || "")}</p>
      <div class="m">${p.direction ? `<span>${esc(p.direction)}</span>` : ""}<span>${esc(p.venue || "")}</span>${date ? `<span>${esc(date)}</span>` : ""}</div>
      <details><summary>摘要</summary><p class="abstract">${hl(p.abstract)}</p></details>
    </div>
    ${starBtn(p)}
  </div>`;
}

// ---------------- views ----------------
function matches(p) {
  const okDir = state.dir === "全部" || p.direction === state.dir;
  if (!state.query) return okDir;
  const q = state.query.toLowerCase();
  return okDir && JSON.stringify([p.title, p.one_liner, p.abstract, p.digest, p.authors, p.venue]).toLowerCase().includes(q);
}

function dirChips(papers) {
  const dirs = [...new Set(papers.map((p) => p.direction).filter(Boolean))];
  if (dirs.length < 2) return "";
  return `<div class="chips">${["全部", ...dirs].map((d) =>
    `<button class="chip ${state.dir === d ? "active" : ""}" data-dir="${esc(d)}">${esc(d)}</button>`).join("")}</div>`;
}

async function renderDay() {
  const main = $("#main");
  if (!state.date) {
    main.innerHTML = `<div class="empty"><b>还没有数据</b>流水线第一次运行后，这里会出现每日精选。</div>`;
    return;
  }
  const day = await loadDay(state.date);
  const s = day.stats || {};
  const deep = day.deep.filter(matches), skim = day.skim.filter(matches);
  main.innerHTML = `
    <section class="day-head">
      <div class="eyebrow">DAILY DIGEST</div>
      <h2>${fmtDate(day.date)}</h2>
      <div class="funnel">
        <span>检索 <b>${s.fetched ?? "–"}</b></span>
        <span>新论文 <b>${s.unseen ?? "–"}</b></span>
        <span>粗筛 <b>${s.candidates ?? "–"}</b></span>
        <span>精读 <b>${day.deep.length}</b> · 速览 <b>${day.skim.length}</b></span>
      </div>
      ${dirChips([...day.deep, ...day.skim])}
    </section>
    ${day.preview ? `<div class="notice">这是<b>预览数据</b>：只跑了检索和关键词粗筛，没有调用 LLM，所以没有打分和解读。正式运行后会被替换。</div>` : ""}
    <div class="section-title"><h3>精读</h3><small>读过全文，逐篇解读</small></div>
    ${deep.length ? deep.map((p, i) => deepCard(p, i)).join("") :
      `<div class="empty"><b>今天没有达到精读门槛的论文</b>宁缺毋滥，可以看看下面的速览或往期。</div>`}
    ${skim.length ? `<div class="section-title"><h3>速览</h3><small>值得知道，不一定要读</small></div>
      <div class="skim">${skim.map((p) => skimRow(p)).join("")}</div>` : ""}`;
}

async function renderSearch() {
  const main = $("#main");
  main.innerHTML = `<div class="empty">搜索中…</div>`;
  await loadAllDays();
  const hits = [];
  for (const d of state.index.days) {
    const day = state.days.get(d.date);
    if (!day) continue;
    for (const p of day.deep) if (matches(p)) hits.push({ p, date: d.date, deep: true });
    for (const p of day.skim) if (matches(p)) hits.push({ p, date: d.date, deep: false });
  }
  main.innerHTML = `
    <section class="day-head"><div class="eyebrow">SEARCH</div><h2>“${esc(state.query)}”</h2>
      <div class="funnel"><span>在全部 <b>${state.index.days.length}</b> 天中找到 <b>${hits.length}</b> 篇</span></div></section>
    ${hits.filter((h) => h.deep).map((h, i) => deepCard(h.p, i, h.date)).join("")}
    ${hits.some((h) => !h.deep) ? `<div class="section-title"><h3>速览中的匹配</h3></div>
      <div class="skim">${hits.filter((h) => !h.deep).map((h) => skimRow(h.p, h.date)).join("")}</div>` : ""}
    ${hits.length ? "" : `<div class="empty"><b>没有找到</b>换个关键词试试。</div>`}`;
}

function renderStars() {
  const items = Object.values(state.stars).sort((a, b) => (b.starred_at || "").localeCompare(a.starred_at || ""));
  $("#main").innerHTML = `
    <section class="day-head"><div class="eyebrow">STARRED</div><h2>我的收藏</h2>
      <div class="funnel"><span>共 <b>${items.length}</b> 篇 · 收藏只保存在当前浏览器</span></div></section>
    ${items.length ? items.map(({ p, date }, i) => p.digest ? deepCard(p, i, date) : `<div class="skim">${skimRow(p, date)}</div>`).join("")
      : `<div class="empty"><b>还没有收藏</b>点论文卡片右上角的 ☆ 即可收藏。</div>`}`;
}

function render() {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view && !state.query));
  document.querySelectorAll("#date-list button").forEach((b) => b.classList.toggle("active", b.dataset.date === state.date && state.view === "day" && !state.query));
  const n = Object.keys(state.stars).length;
  $("#star-count").textContent = n ? n : "";
  if (state.query) return renderSearch();
  if (state.view === "stars") return renderStars();
  return renderDay();
}

// ---------------- sidebar ----------------
function renderSidebar() {
  const days = state.index.days;
  $("#date-list").innerHTML = days.map((d) =>
    `<li><button data-date="${d.date}"><span class="d">${d.date.slice(5).replace("-", " / ")}</span><span class="c">${d.deep} + ${d.skim}</span></button></li>`).join("");
  $("#date-select").innerHTML = days.map((d) => `<option value="${d.date}">${d.date}（精读 ${d.deep} · 速览 ${d.skim}）</option>`).join("");
  $("#date-select").value = state.date;
}

function go(date) {
  state.date = date; state.view = "day"; state.dir = "全部"; state.query = ""; $("#search").value = "";
  $("#date-select").value = date;
  history.replaceState(null, "", `#${date}`);
  render(); window.scrollTo({ top: 0 });
}

// ---------------- events ----------------
document.addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.date) return go(t.dataset.date);
  if (t.dataset.view) { state.view = t.dataset.view; state.query = ""; $("#search").value = ""; return render(); }
  if (t.dataset.dir) { state.dir = t.dataset.dir; return render(); }
  if (t.dataset.star) {
    const uid = t.dataset.star;
    if (state.stars[uid]) delete state.stars[uid];
    else {
      for (const [date, day] of state.days) {
        const p = [...day.deep, ...day.skim].find((x) => x.uid === uid);
        if (p) { state.stars[uid] = { p, date, starred_at: new Date().toISOString() }; break; }
      }
    }
    store.set("stars", state.stars);
    const on = !!state.stars[uid];
    document.querySelectorAll(`[data-star="${CSS.escape(uid)}"]`).forEach((b) => {
      b.classList.toggle("on", on); b.textContent = on ? "★" : "☆"; b.setAttribute("aria-pressed", on);
    });
    const n = Object.keys(state.stars).length;
    $("#star-count").textContent = n ? n : "";
    if (state.view === "stars") renderStars();
  }
});
$("#date-select").addEventListener("change", (e) => go(e.target.value));
let timer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(timer);
  timer = setTimeout(() => { state.query = e.target.value.trim(); render(); }, 250);
});
$("#theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  store.set("theme", root.dataset.theme);
});

// ---------------- boot ----------------
(async function init() {
  const t = store.get("theme", null);
  if (t) document.documentElement.dataset.theme = t;
  try {
    state.index = await loadJSON("data/index.json");
  } catch {
    state.index = { days: [] };
  }
  const days = state.index.days;
  $("#updated").textContent = state.index.updated_at
    ? `最近更新 ${new Date(state.index.updated_at).toLocaleString("zh-CN", { hour12: false })}`
    : "尚未运行";
  const want = location.hash.slice(1);
  state.date = days.some((d) => d.date === want) ? want : days[0]?.date ?? null;
  renderSidebar();
  render();
})();
