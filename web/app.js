"use strict";

/* ============================================================
   地学 SQL 文献日报 —— 前端
   - data/vault.json 存在时：先输入密码，用 WebCrypto 派生密钥解密数据
   - 推荐按日期连成一条信息流；筛选 / 搜索 / 收藏都在本地完成
   ============================================================ */

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d, s = localStorage) { try { const v = s.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v, s = localStorage) { try { s.setItem(k, JSON.stringify(v)); } catch { /* 隐私模式 */ } },
  del(k) { try { localStorage.removeItem(k); sessionStorage.removeItem(k); } catch { /* */ } },
};

const DIRS = [
  { name: "空间/地学 Text-to-SQL", c: "--d1" },
  { name: "通用 Text-to-SQL 前沿", c: "--d2" },
  { name: "GIS Agent / 地理代码生成", c: "--d3" },
  { name: "时空数据库 / KG 查询", c: "--d4" },
  { name: "空间数据库 / GIS 原理", c: "--d6" },
  { name: "空间推理 / 地理知识", c: "--d5" },
];
const dirColor = (name) => `var(${(DIRS.find((d) => d.name === name) || { c: "--d0" }).c})`;
const VERDICT = { must_read: "必读", worth_reading: "值得读", skim_only: "速览即可" };
const METRICS = [["relevance", "相关"], ["transfer", "可迁移"], ["novelty", "新颖"], ["rigor", "严谨"]];
const PAGE = 10;   // 每次加载的天数
const KEY_STORE = "vault-key";

const state = {
  key: null, rawKey: null, index: null, days: new Map(), shown: PAGE,
  view: "feed", q: "", onlyDeep: store.get("onlyDeep", false), zh: store.get("zh", true),
  hidden: new Set(store.get("hiddenDirs", [])), stars: store.get("stars", {}), absLang: {},
};

/* ---------------- 加密 ---------------- */
const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const ub64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));

async function deriveKey(password, meta) {
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: b64(meta.salt), iterations: meta.iterations }, base, 256);
  return bits;
}
async function importKey(raw) {
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["decrypt"]);
}
async function decrypt(key, blob) {
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(blob.iv) }, key, b64(blob.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  const obj = await r.json();
  if (obj && obj.v === 1 && obj.ct) return decrypt(state.key, obj);
  return obj;
}

/* ---------------- 登录 ---------------- */
async function boot() {
  applyThemeIcon();
  let meta = null;
  try {
    const r = await fetch("data/vault.json", { cache: "no-cache" });
    if (r.ok) meta = await r.json();
  } catch { /* 本地明文模式 */ }

  if (!meta) return start();

  const saved = store.get(KEY_STORE, null) || store.get(KEY_STORE, null, sessionStorage);
  if (saved) {
    try {
      state.key = await importKey(b64(saved));
      state.rawKey = saved;
      if ((await decrypt(state.key, meta.check)) === "geosql-digest-ok") return start(true);
    } catch { store.del(KEY_STORE); }
  }
  showGate(meta);
}

function showGate(meta) {
  $("#gate").hidden = false;
  $("#app").hidden = true;
  const form = $("#gate-form"), btn = $("#gate-btn"), err = $("#gate-err"), pw = $("#pw");
  pw.focus();
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (!pw.value) return;
    btn.disabled = true; btn.textContent = "解密中…"; err.hidden = true;
    try {
      const raw = await deriveKey(pw.value, meta);
      const key = await importKey(raw);
      if ((await decrypt(key, meta.check)) !== "geosql-digest-ok") throw new Error("bad");
      state.key = key;
      state.rawKey = ub64(raw);
      store.set(KEY_STORE, ub64(raw), $("#remember").checked ? localStorage : sessionStorage);
      $("#gate").hidden = true;
      start(true);
    } catch {
      err.textContent = "密码不正确，请重试。";
      err.hidden = false;
      form.classList.remove("shake"); void form.offsetWidth; form.classList.add("shake");
      pw.select();
    } finally {
      btn.disabled = false; btn.textContent = "进入";
    }
  };
}

/* ---------------- 数据 ---------------- */
const loaded = new Map();   // date -> 已解析的当天数据（供收藏、切换摘要语言时同步查找）
async function loadDay(date) {
  if (!state.days.has(date)) {
    state.days.set(date, fetchJSON(`data/daily/${date}.json`)
      .then((v) => { loaded.set(date, v); return v; })
      .catch(() => null));
  }
  return state.days.get(date);
}

async function start(locked = false) {
  $("#app").hidden = false;
  $("#logout").hidden = !locked;
  renderSkeleton();
  try {
    state.index = await fetchJSON("data/index.json");
  } catch {
    state.index = { days: [] };
  }
  const u = state.index.updated_at;
  $("#updated").textContent = u ? `更新于 ${new Date(u).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })}` : "尚未运行";
  $("#only-deep").checked = state.onlyDeep;
  $("#show-zh").checked = state.zh;
  renderLegend();
  renderCost();
  renderDates();
  await render();
  loadUploadIndex().then((ix) => {
    const done = new Set((ix.items || []).map((i) => i.id));
    const pending = store.get(PENDING_KEY, []).filter((x) => !done.has(x.id));
    updateUpBadge((ix.items || []).length, pending.length);
    schedulePoll(pending.length > 0);
  });
  if (location.hash.length > 1) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
}

/* ---------------- 渲染工具 ---------------- */
function hl(text) {
  const s = esc(text);
  if (!state.q) return s;
  const q = state.q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return s.replace(new RegExp(q, "gi"), (m) => `<mark>${m}</mark>`);
}
const icon = (id, cls = "ic") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`;
const list = (arr) => !arr || !arr.length ? "<p>文中未说明</p>"
  : Array.isArray(arr) ? `<ul>${arr.map((x) => `<li>${hl(x)}</li>`).join("")}</ul>` : `<p>${hl(arr)}</p>`;
const pid = (p) => "p-" + p.uid.replace(/[^\w-]+/g, "-");

function fmtDay(s) {
  const d = new Date(s + "T00:00:00");
  return { md: `${d.getMonth() + 1} 月 ${d.getDate()} 日`, wk: "周" + "日一二三四五六"[d.getDay()], y: d.getFullYear() };
}
function authors(p) {
  const a = p.authors || [];
  return esc(a.slice(0, 3).join(", ") + (a.length > 3 ? " 等" : ""));
}
const venueName = (p) => p.venue_norm || p.venue || "";
const STATUS = { accepted: "已接收" };

/* 一作 · 通讯 · 单位 · 期刊（有补全信息时；否则退回作者列表） */
function people(p, compact = false) {
  const pp = p.people;
  if (!pp || !pp.first_author) {
    return `<p class="byline">${authors(p)} · <span class="venue">${esc(venueName(p))}</span>${p.published ? ` · ${esc(p.published)}` : ""}</p>`;
  }
  const who = (n, a) => `<b>${hl(n)}</b>${a ? `<span class="aff">${hl(a)}</span>` : ""}`;
  const corr = (pp.corresponding || []).filter((c) => c && c.name);
  const sameAsFirst = corr.length === 1 && corr[0].name === pp.first_author;
  const st = STATUS[p.venue_status] ? `<span class="st">${STATUS[p.venue_status]}</span>` : "";
  if (compact) {
    return `<p class="who compact">一作 ${who(pp.first_author, pp.first_affil)}${corr.length && !sameAsFirst ? ` · 通讯 ${who(corr[0].name, corr[0].affil)}` : ""}</p>`;
  }
  const others = (pp.institutions || []).filter((i) => i && i !== pp.first_affil && !corr.some((c) => c.affil === i));
  return `<dl class="who">
    <div><dt>一作</dt><dd>${who(pp.first_author, pp.first_affil)}</dd></div>
    <div><dt>通讯</dt><dd>${corr.length ? corr.map((c) => who(c.name, c.affil)).join("、") + (sameAsFirst ? "<span class=\"aff\">（同一作）</span>" : "") : '<span class="na">文中未标注</span>'}</dd></div>
    ${others.length ? `<div><dt>其他单位</dt><dd>${others.map(hl).join("、")}</dd></div>` : ""}
    <div><dt>发表</dt><dd><b>${hl(venueName(p))}</b>${st}${p.published ? `<span class="aff">${esc(p.published)}</span>` : ""}</dd></div>
  </dl>`;
}
function qualityTags(p) {
  const why = esc(p.quality_reason || "");
  let out = "";
  if (p.venue_tier === "top") out += `<span class="tag top" title="${why}">顶会顶刊</span>`;
  if (p.inst_tier === "top") out += `<span class="tag top-inst" title="${why}">顶尖单位</span>`;
  return out;
}
const money = (usd) => usd == null ? "—" : usd < 0.01 ? "<$0.01" : `$${usd.toFixed(2)}`;
function titleBlock(p, tag) {
  const zh = state.zh && p.title_zh;
  const main = zh ? hl(p.title_zh) : hl(p.title);
  return `<${tag}${zh ? "" : ' class="serif"'}><a href="${esc(p.url)}" target="_blank" rel="noopener">${main}</a></${tag}>`
    + (zh ? `<p class="en-title">${hl(p.title)}</p>` : "");
}
function starBtn(p) {
  const on = !!state.stars[p.uid];
  return `<button class="star" data-star="${esc(p.uid)}" aria-pressed="${on}" title="${on ? "取消收藏" : "收藏"}">${icon("star")}<span class="sr-only">收藏</span></button>`;
}
function links(p) {
  const out = [`<a href="${esc(p.url)}" target="_blank" rel="noopener">${icon("ext")}原文</a>`];
  if (p.pdf_url) out.push(`<a href="${esc(p.pdf_url)}" target="_blank" rel="noopener">${icon("pdf")}PDF</a>`);
  if (p.code_url) out.push(`<a href="${esc(p.code_url)}" target="_blank" rel="noopener">${icon("code")}代码</a>`);
  return `<span class="links">${out.join("")}</span>`;
}
function scale(p) {
  if (!p.scores) return "";
  const c = dirColor(p.direction);
  const ms = METRICS.map(([k, label]) => {
    const v = Math.round(p.scores[k] ?? 0);
    const ticks = Array.from({ length: 10 }, (_, i) => `<i class="${i < v ? "f" : ""}"></i>`).join("");
    return `<div class="metric"><span>${label}<b>${v}</b></span><div class="ticks" style="--c:${c}">${ticks}</div></div>`;
  }).join("");
  return `<div class="scale">${ms}<div class="total"><b>${p.score.toFixed(1)}</b><small>综合分</small></div></div>`;
}
function abstractBlock(p) {
  if (!p.abstract) return p.no_abstract ? `<div class="abs"><p class="cost-note">出版社未公开摘要，评分只依据标题和期刊。</p></div>` : "";
  const hasZh = !!p.abstract_zh;
  const lang = state.absLang[p.uid] || (state.zh && hasZh ? "zh" : "en");
  const text = lang === "zh" && hasZh ? p.abstract_zh : p.abstract;
  const seg = hasZh ? `<span class="seg" role="group" aria-label="摘要语言">
      <button data-abs="${esc(p.uid)}" data-lang="zh" aria-pressed="${lang === "zh"}">中文</button>
      <button data-abs="${esc(p.uid)}" data-lang="en" aria-pressed="${lang === "en"}">英文原文</button></span>` : "";
  return `<div class="abs"><div class="abs-head"><h4>摘要</h4>${seg}</div><p class="${lang === "en" ? "en" : ""}">${hl(text)}</p></div>`;
}
function metaRow(p, extra = "") {
  const d = p.digest;
  return `<div class="meta-row">
    <span class="dir"><i class="sw" style="--c:${dirColor(p.direction)}"></i>${esc(p.direction || "其他")}</span>
    ${d?.verdict ? `<span class="tag ${esc(d.verdict)}">${VERDICT[d.verdict] || esc(d.verdict)}</span>` : ""}
    ${qualityTags(p)}
    ${p.cites_seed ? `<span class="tag cite" title="这篇论文引用了团队论文">${icon("cite")}引用 ${esc(p.cites_seed)}</span>` : ""}
    ${p.code_url ? `<span class="tag plain">${icon("code")}开源代码</span>` : ""}
    ${extra}${starBtn(p)}
  </div>`;
}

function paperDeep(p, date) {
  const d = p.digest || {};
  return `<article class="paper" id="${pid(p)}">
    ${metaRow(p, date ? `<span class="tag plain">${esc(date)}</span>` : "")}
    ${titleBlock(p, "h3")}
    ${people(p)}
    ${d.tldr ? `<p class="tldr">${hl(d.tldr)}</p>` : p.one_liner ? `<p class="tldr">${hl(p.one_liner)}</p>` : ""}
    ${d.inspiration?.length ? `<section class="insight"><h4>${icon("bulb")}对你研究的启发</h4>${list(d.inspiration)}</section>` : ""}
    ${scale(p)}
    <details class="fold">
      <summary>完整解读${icon("chev")}</summary>
      <dl class="rows">
        ${d.problem ? `<dt>要解决的问题</dt><dd>${list(d.problem)}</dd>` : ""}
        ${d.method ? `<dt>怎么做的</dt><dd>${list(d.method)}</dd>` : ""}
        ${d.evidence ? `<dt>关键证据</dt><dd>${list(d.evidence)}</dd>` : ""}
        ${d.value ? `<dt>价值与亮点</dt><dd>${list(d.value)}</dd>` : ""}
        ${d.limitations ? `<dt>局限与疑点</dt><dd class="warn">${list(d.limitations)}</dd>` : ""}
        ${d.verdict_reason ? `<dt>阅读建议</dt><dd>${list(d.verdict_reason)}</dd>` : ""}
      </dl>
      ${abstractBlock(p)}
    </details>
    <div class="foot-row">
      <div class="tags">${(d.tags || []).map((t) => `<span>${hl(t)}</span>`).join("")}</div>
      ${d.fulltext_source ? `<span class="src-note">依据：${esc(d.fulltext_source)}</span>` : ""}
      ${links(p)}
    </div>
  </article>`;
}

function paperSkim(p, date) {
  return `<article class="skim" id="${pid(p)}" style="--c:${dirColor(p.direction)}">
    <div class="s">${p.score != null ? p.score.toFixed(1) : ""}</div>
    <div>
      ${titleBlock(p, "h3")}
      ${p.one_liner ? `<p class="ol">${hl(p.one_liner)}</p>` : ""}
      ${people(p, true)}
      <div class="m">
        <span class="dir"><i class="sw" style="--c:${dirColor(p.direction)}"></i>${esc(p.direction || "其他")}</span>
        <span class="venue">${esc(venueName(p))}${STATUS[p.venue_status] ? `（${STATUS[p.venue_status]}）` : ""}</span>${qualityTags(p)}${date ? `<span>${esc(date)}</span>` : ""}
        ${p.cites_seed ? `<span class="tag cite">${icon("cite")}引用 ${esc(p.cites_seed)}</span>` : ""}
        ${p.code_url ? `<a href="${esc(p.code_url)}" target="_blank" rel="noopener">代码</a>` : ""}
      </div>
      <details><summary>摘要${icon("chev")}</summary>${abstractBlock(p)}</details>
    </div>
    ${starBtn(p)}
  </article>`;
}

/* ---------------- 筛选 ---------------- */
function matches(p) {
  if (state.hidden.has(p.direction || "其他")) return false;
  if (!state.q) return true;
  const q = state.q.toLowerCase();
  return JSON.stringify([p.title, p.title_zh, p.one_liner, p.abstract, p.abstract_zh, p.digest, p.authors, p.venue, p.venue_norm, p.people])
    .toLowerCase().includes(q);
}
const filtersActive = () => state.hidden.size > 0 || state.onlyDeep;

function renderLegend() {
  $("#legend").innerHTML = DIRS.map((d) => {
    const on = !state.hidden.has(d.name);
    return `<li><button data-dir="${esc(d.name)}" aria-pressed="${on}"><i class="sw" style="--c:var(${d.c})"></i><span>${esc(d.name)}</span></button></li>`;
  }).join("");
  $("#filter-dot").hidden = !filtersActive();
}
function renderCost() {
  const days = state.index.days.filter((d) => d.cost_usd != null);
  if (!days.length) { $("#cost").innerHTML = `<p class="cost-note">还没有花费记录</p>`; return; }
  const sum = (n) => days.slice(0, n).reduce((s, d) => s + d.cost_usd, 0);
  const total = sum(days.length), rate = 7.3;
  const max = Math.max(...days.slice(0, 14).map((d) => d.cost_usd), 0.0001);
  $("#cost").innerHTML = `
    <div class="cost-big"><b>${money(sum(7))}</b><span>近 7 天</span></div>
    <p class="cost-line">近 30 天 ${money(sum(30))} · 累计 ${money(total)}<br>按中转站汇率约 ¥${(total * rate).toFixed(2)}</p>
    <details class="cost-days"><summary>每日明细${icon("chev")}</summary>
      <ol>${days.slice(0, 30).map((d) => `<li><span>${d.date.slice(5).replace("-", "/")}</span>
        <i style="width:${Math.max(4, (d.cost_usd / max) * 100)}%"></i><b>${money(d.cost_usd)}${d.cost_est ? "<small>估</small>" : ""}</b></li>`).join("")}</ol>
    </details>
    <p class="cost-note">单位是中转站的美元额度，由每天实际用量 × 中转站价格表计算。标“估”的日期没有分模型记录，按总量估算。</p>`;
}

function renderDates() {
  $("#dates").innerHTML = state.index.days.map((d) => {
    const { md } = fmtDay(d.date);
    const n = d.deep + d.skim;
    return `<li><a href="#day-${d.date}" data-date="${d.date}" class="${n ? "" : "empty"}">${md}<span>${n ? `${d.deep} 精读 · ${d.skim} 速览` : "无"}</span></a></li>`;
  }).join("");
}

function renderSkeleton() {
  const line = (w, h = 14) => `<div class="sk" style="width:${w};height:${h}px;margin:10px 0"></div>`;
  $("#feed").innerHTML = `<div class="day"><div class="day-head">${line("160px", 22)}</div>
    <div class="paper">${line("40%", 12)}${line("85%", 22)}${line("60%", 12)}${line("100%")}${line("92%")}${line("70%")}</div></div>`;
}

async function render() {
  document.querySelectorAll(".views button").forEach((b) => b.classList.toggle("on", b.dataset.view === state.view));
  const n = Object.keys(state.stars).length;
  $("#star-n").textContent = n || "";
  if (state.view === "stars") return renderStars();
  if (state.view === "uploads") return renderUploads();
  return renderFeed();
}

async function renderFeed() {
  const days = state.index.days;
  const searching = !!state.q;
  const take = searching ? days : days.slice(0, state.shown);
  const dayData = await Promise.all(take.map((d) => loadDay(d.date)));
  let html = "", total = 0;

  take.forEach((meta, i) => {
    const day = dayData[i];
    if (!day) return;
    const deep = day.deep.filter(matches);
    const skim = state.onlyDeep ? [] : day.skim.filter(matches);
    total += deep.length + skim.length;
    if ((searching || filtersActive()) && !deep.length && !skim.length) return;
    const { md, wk } = fmtDay(day.date);
    html += `<section class="day" id="day-${day.date}">
      <header class="day-head"><h2>${md}<span class="wk">${wk}</span></h2>
        <span class="stat">${day.deep.length} 篇精读 · ${day.skim.length} 篇速览${day.stats?.candidates != null ? ` · 从 ${day.stats.candidates} 篇候选中选出` : ""}${day.stats?.cost?.usd != null ? ` · 花费 ${money(day.stats.cost.usd)}${day.stats.cost.estimated ? "（估）" : ""}` : ""}</span></header>
      ${deep.map((p) => paperDeep(p)).join("")}
      ${skim.length ? `<div class="skims">${skim.map((p) => paperSkim(p)).join("")}</div>` : ""}
      ${!deep.length && !skim.length ? `<p class="day-empty">这天没有达到推荐门槛的新论文。</p>` : ""}
    </section>`;
  });

  const head = searching ? `<div class="notice"><span>“${esc(state.q)}”在全部 ${days.length} 天中找到 ${total} 篇</span><button data-clear-q>清除搜索</button></div>`
    : filtersActive() ? `<div class="notice"><span>已开启筛选，正在显示 ${total} 篇</span><button data-clear-filters>清除筛选</button></div>` : "";

  if (!days.length) html = `<div class="empty"><b>还没有推荐</b>流水线每天北京时间 9:30 运行，第一次运行后这里会出现论文。</div>`;
  else if (!total && (searching || filtersActive())) html = `<div class="empty"><b>没有符合条件的论文</b>换个关键词，或放宽方向筛选。<br><button data-clear-filters data-clear-q>全部清除</button></div>`;

  $("#feed").innerHTML = head + html;
  $("#more-wrap").hidden = searching || state.shown >= days.length;
}

function renderStars() {
  const items = Object.values(state.stars).sort((a, b) => (b.at || "").localeCompare(a.at || ""));
  $("#more-wrap").hidden = true;
  $("#feed").innerHTML = items.length
    ? `<section class="day"><header class="day-head"><h2>我的收藏</h2><span class="stat">${items.length} 篇 · 保存在这台设备的浏览器里</span></header>
       ${items.map(({ p, date }) => p.digest ? paperDeep(p, date) : `<div class="skims">${paperSkim(p, date)}</div>`).join("")}</section>`
    : `<div class="empty"><b>还没有收藏</b>点论文右上角的星标，就能把它收进这里。</div>`;
}

/* ---------------- 交互 ---------------- */
function findPaper(uid) {
  for (const [date, day] of loaded) {
    const p = day && [...day.deep, ...day.skim].find((x) => x.uid === uid);
    if (p) return { p, date };
  }
  const u = up.papers.get(uid);
  if (u) return { p: u.paper, date: "我的上传" };
  return state.stars[uid] || null;
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, a[data-date], a[data-home]");
  if (!t) return;

  if (t.matches("a[data-home]")) {
    e.preventDefault(); state.view = "feed"; state.q = ""; $("#q").value = ""; await render(); scrollTo({ top: 0 }); return;
  }
  if (t.dataset.date) {
    e.preventDefault();
    const i = state.index.days.findIndex((d) => d.date === t.dataset.date);
    if (state.view !== "feed" || state.q || i >= state.shown) {
      state.view = "feed"; state.q = ""; $("#q").value = ""; state.shown = Math.max(state.shown, i + 1); await render();
    }
    document.getElementById(`day-${t.dataset.date}`)?.scrollIntoView({ behavior: "smooth" });
    return;
  }
  if (t.dataset.view) { state.view = t.dataset.view; state.q = ""; $("#q").value = ""; closeRail(); return render(); }
  if (handleUploadClick(t)) return;
  if (t.dataset.dir) {
    const d = t.dataset.dir;
    state.hidden.has(d) ? state.hidden.delete(d) : state.hidden.add(d);
    store.set("hiddenDirs", [...state.hidden]); renderLegend(); return render();
  }
  if (t.hasAttribute("data-clear-filters") || t.hasAttribute("data-clear-q")) {
    if (t.hasAttribute("data-clear-filters")) { state.hidden.clear(); state.onlyDeep = false; $("#only-deep").checked = false; store.set("hiddenDirs", []); store.set("onlyDeep", false); renderLegend(); }
    if (t.hasAttribute("data-clear-q")) { state.q = ""; $("#q").value = ""; }
    return render();
  }
  if (t.dataset.abs) {
    state.absLang[t.dataset.abs] = t.dataset.lang;
    const box = t.closest(".abs");
    const hit = findPaper(t.dataset.abs);
    if (box && hit) box.outerHTML = abstractBlock(hit.p);
    return;
  }
  if (t.dataset.star) {
    const uid = t.dataset.star;
    if (state.stars[uid]) delete state.stars[uid];
    else { const hit = findPaper(uid); if (hit) state.stars[uid] = { p: hit.p, date: hit.date, at: new Date().toISOString() }; }
    store.set("stars", state.stars);
    const on = !!state.stars[uid];
    document.querySelectorAll(`[data-star="${CSS.escape(uid)}"]`).forEach((b) => { b.setAttribute("aria-pressed", on); b.title = on ? "取消收藏" : "收藏"; });
    $("#star-n").textContent = Object.keys(state.stars).length || "";
    if (state.view === "stars") renderStars();
    return;
  }
  if (t.id === "more") { state.shown += PAGE; return render(); }
  if (t.id === "filter-toggle") {
    const open = !$("#rail").classList.contains("open");
    $("#rail").classList.toggle("open", open); t.setAttribute("aria-expanded", open); return;
  }
  if (t.id === "theme") {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark"; store.set("theme", root.dataset.theme); applyThemeIcon(); return;
  }
  if (t.id === "logout") { store.del(KEY_STORE); location.reload(); }
});

function closeRail() { $("#rail").classList.remove("open"); $("#filter-toggle").setAttribute("aria-expanded", "false"); }
function applyThemeIcon() {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $("#theme-ic").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", dark ? "#14191a" : "#f3efe5"));
}

$("#only-deep").addEventListener("change", (e) => { state.onlyDeep = e.target.checked; store.set("onlyDeep", state.onlyDeep); renderLegend(); render(); });
$("#show-zh").addEventListener("change", (e) => { state.zh = e.target.checked; state.absLang = {}; store.set("zh", state.zh); render(); });
let qTimer;
$("#q").addEventListener("input", (e) => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { state.q = e.target.value.trim(); state.view = "feed"; render(); }, 250);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement?.tagName !== "INPUT") { e.preventDefault(); $("#q").focus(); }
  if (e.key === "Escape" && document.activeElement === $("#q")) { $("#q").value = ""; state.q = ""; render(); }
});


/* ================= 添加论文（链接 / PDF → 云端解析） ================= */
const REPO = "LiuZQ802/geosql-paper-digest";
const TOKEN_KEY = "gh-token";
const PENDING_KEY = "uploads-pending";
const MAX_PDF = 20 * 1024 * 1024;
const MODE_HINT = {
  auto: "按内容打分：达到精读线就读全文写完整解读，否则只写速览",
  deep: "不管分数高低，都读全文写完整解读（花费比速览高）",
  skim: "只打分、写一句话点评、翻译摘要（花费很少）",
};
const up = { src: "url", mode: "auto", file: null, index: null, cache: new Map(), papers: new Map(), runs: {}, timer: null };

function bytesToB64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
async function encryptBytes(bytes) {
  const k = await crypto.subtle.importKey("raw", b64(state.rawKey), "AES-GCM", false, ["encrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, k, bytes));
  return { v: 1, iv: bytesToB64(iv), ct: bytesToB64(ct) };
}
const encryptJSON = (obj) => encryptBytes(new TextEncoder().encode(JSON.stringify(obj)));

async function gh(path, opts = {}) {
  const r = await fetch(`https://api.github.com${path}`, {
    ...opts,
    headers: { Accept: "application/vnd.github+json", Authorization: `Bearer ${store.get(TOKEN_KEY, "")}`,
      "X-GitHub-Api-Version": "2022-11-28", ...(opts.headers || {}) },
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).message || ""; } catch { /* */ }
    const msg = r.status === 401 ? "GitHub 令牌无效或已过期，请点“更换 GitHub 令牌”重新填写。"
      : (r.status === 403 || r.status === 404) ? "这个 GitHub 令牌没有权限：请确认创建令牌时选了 geosql-paper-digest 仓库，并且 Actions 和 Contents 都设为 Read and write。"
      : r.status === 422 ? `GitHub 拒绝了请求：${detail}` : `GitHub 返回 ${r.status}：${detail}`;
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  if (r.status === 204) return null;
  try { return await r.json(); } catch { return null; }
}

function newId() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}-${Math.random().toString(36).slice(2, 6)}`;
}
function addMsg(kind, html) {
  const el = $("#add-msg");
  if (!kind) { el.hidden = true; return; }
  el.className = `add-msg ${kind}`;
  el.innerHTML = `${icon({ err: "alert", info: "clock", ok: "bulb" }[kind])}<span>${html}</span>`;
  el.hidden = false;
}
function openAdd(open, prefill) {
  $("#add-panel").hidden = !open;
  $("#add-toggle").setAttribute("aria-expanded", String(open));
  if (!open) return;
  const hasToken = !!store.get(TOKEN_KEY, "");
  $("#token-box").hidden = hasToken;
  $("#token-reset").hidden = !hasToken;
  if (!state.rawKey) addMsg("err", "本地预览模式没有加密密钥，不能提交。请在正式网页上登录后使用。");
  if (prefill) { setSrc("url"); $("#add-url").value = prefill; }
  scrollTo({ top: 0, behavior: "smooth" });
  (up.src === "url" ? $("#add-url") : $("#add-file")).focus();
}
function setSrc(src) {
  up.src = src;
  document.querySelectorAll("[data-src]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.src === src)));
  $("#src-url").hidden = src !== "url";
  $("#src-pdf").hidden = src !== "pdf";
  addMsg(null);
}
function setFile(f) {
  if (f && !(f.type === "application/pdf" || /\.pdf$/i.test(f.name))) { addMsg("err", "只能上传 PDF 文件。"); f = null; }
  else if (f && f.size > MAX_PDF) { addMsg("err", `文件太大（${(f.size / 1048576).toFixed(1)} MB），最大 20 MB。`); f = null; }
  else addMsg(null);
  up.file = f;
  $("#drop").classList.toggle("has-file", !!f);
  $("#drop-text").textContent = f ? `${f.name}（${(f.size / 1048576).toFixed(1)} MB）` : "把 PDF 拖到这里，或点击选择文件";
  if (!f) $("#add-file").value = "";
}
const looksValid = (v) => /^https?:\/\/\S+\.\S+/i.test(v) || /^10\.\d{4,9}\/\S+$/.test(v) || /^(arxiv:)?\s*\d{4}\.\d{4,5}(v\d+)?$/i.test(v);

async function submitPaper() {
  if (!state.rawKey) return addMsg("err", "本地预览模式没有加密密钥，不能提交。请在正式网页上登录后使用。");
  const typed = $("#add-token").value.trim();
  if (typed) store.set(TOKEN_KEY, typed);
  if (!store.get(TOKEN_KEY, "")) {
    $("#token-box").hidden = false;
    $("#add-token").focus();
    return addMsg("err", "第一次使用请先填写 GitHub 令牌。");
  }

  let req, label;
  if (up.src === "url") {
    const url = $("#add-url").value.trim();
    if (!url) return addMsg("err", "请粘贴论文链接、DOI 或 arXiv 编号。");
    if (!looksValid(url)) return addMsg("err", "格式不对：请粘贴完整网址（http 开头）、DOI（如 10.1016/j.xxx）或 arXiv 编号（如 2509.25264）。");
    req = { kind: "url", url };
    label = url;
  } else {
    if (!up.file) return addMsg("err", "请先选择一个 PDF 文件。");
    req = { kind: "pdf", name: up.file.name };
    label = up.file.name;
  }
  const id = newId();
  Object.assign(req, { mode: up.mode, submitted_at: new Date().toISOString() });
  const btn = $("#add-submit");
  btn.disabled = true;
  try {
    let file = "";
    if (req.kind === "pdf") {
      addMsg("info", "正在加密并上传 PDF…");
      const blob = await encryptBytes(new Uint8Array(await up.file.arrayBuffer()));
      file = `inbox/${id}.pdf.enc`;
      await gh(`/repos/${REPO}/contents/${file}`, { method: "PUT", body: JSON.stringify({
        message: `upload ${id}`, branch: "inbox", content: bytesToB64(new TextEncoder().encode(JSON.stringify(blob))) }) });
    }
    addMsg("info", "正在提交解析任务…");
    await gh(`/repos/${REPO}/actions/workflows/daily.yml/dispatches`, { method: "POST", body: JSON.stringify({
      ref: "main", inputs: { request_id: id, request: JSON.stringify(await encryptJSON(req)), file } }) });
    const pending = store.get(PENDING_KEY, []);
    pending.unshift({ id, label, kind: req.kind, mode: req.mode, at: req.submitted_at });
    store.set(PENDING_KEY, pending.slice(0, 50));
    $("#add-url").value = "";
    $("#add-token").value = "";
    setFile(null);
    $("#token-box").hidden = true;
    $("#token-reset").hidden = false;
    addMsg("ok", "已提交，云端大约 2–5 分钟完成。结果会出现在下面的“我的上传”里，页面会自动刷新。");
    state.view = "uploads";
    state.q = "";
    $("#q").value = "";
    render();
  } catch (e) {
    if ([401, 403, 404].includes(e.status)) $("#token-box").hidden = false;
    addMsg("err", esc(e.message || String(e)));
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 我的上传 ---------- */
async function loadUploadIndex() {
  try { up.index = await fetchJSON(`data/uploads/index.json?t=${Date.now()}`); } catch { up.index = { items: [] }; }
  return up.index;
}
function loadUpload(id) {
  if (!up.cache.has(id)) {
    up.cache.set(id, fetchJSON(`data/uploads/${id}.json`).then((it) => {
      if (it?.paper) up.papers.set(it.paper.uid, it);
      return it;
    }).catch(() => null));
  }
  return up.cache.get(id);
}
async function refreshRuns(pending) {
  if (!store.get(TOKEN_KEY, "") || !pending.length) return;
  try {
    const res = await gh(`/repos/${REPO}/actions/runs?event=workflow_dispatch&per_page=30`);
    for (const run of res?.workflow_runs || []) {
      const m = /^解析 (\S+)$/.exec(run.display_title || "");
      if (m) up.runs[m[1]] = { status: run.status, conclusion: run.conclusion, url: run.html_url };
    }
  } catch { /* 查询失败不影响展示 */ }
}
const mins = (iso) => Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
const MODE_NAME = { auto: "自动判断", deep: "精读", skim: "速览" };

function pendingCard(p) {
  const run = up.runs[p.id] || {};
  const failed = run.status === "completed" && run.conclusion !== "success";
  let chip = `<span class="chip wait">${icon("clock")}已提交</span>`, note = "";
  if (["queued", "waiting", "pending", "requested"].includes(run.status)) chip = `<span class="chip wait">${icon("clock")}排队中</span>`;
  else if (run.status === "in_progress") chip = `<span class="chip wait">${icon("clock")}解析中</span>`;
  else if (run.status === "completed" && !failed) { chip = `<span class="chip wait">${icon("clock")}即将显示</span>`; note = "解析已完成，网页正在更新（约 1 分钟）。"; }
  else if (failed) { chip = `<span class="chip fail">${icon("alert")}任务失败</span>`; note = `云端任务没有跑完（${esc(run.conclusion)}）。<a href="${esc(run.url)}" target="_blank" rel="noopener">查看日志</a>，或重新提交。`; }
  return `<div class="up-item"><div class="up-status">${chip}<span>${MODE_NAME[p.mode] || ""} · 提交于 ${mins(p.at)} 分钟前</span></div>
    <div class="up-card${failed ? " fail" : ""}"><h3>${esc(p.label)}</h3>${note ? `<p>${note}</p>` : ""}
    ${failed ? `<div class="actions"><button data-forget="${esc(p.id)}">从列表移除</button>${p.kind === "url" ? `<button data-retry="${esc(p.label)}">重新提交</button>` : `<button data-upload-pdf>重新上传</button>`}</div>` : ""}</div></div>`;
}
function resultCard(it) {
  const when = it.finished_at ? new Date(it.finished_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  if (it.status !== "done") {
    return `<div class="up-item"><div class="up-status"><span class="chip fail">${icon("alert")}解析失败</span><span>${esc(when)}</span></div>
      <div class="up-card fail"><h3>${esc(it.input || it.title || "")}</h3><p>${esc(it.message || "未知错误")}</p>
      <div class="actions">${it.kind === "url" ? `<button data-retry="${esc(it.input)}">修改后重新提交</button>` : ""}<button data-upload-pdf>改为上传 PDF</button></div></div></div>`;
  }
  const p = it.paper;
  const notes = (it.notes || []).map((n) => `<span class="chip fail">${icon("alert")}${esc(n)}</span>`).join("");
  const cost = it.cost?.usd != null ? ` · 花费 ${money(it.cost.usd)}` : "";
  return `<div class="up-item"><div class="up-status"><span class="chip done">${it.mode_final === "deep" ? "精读" : "速览"}</span>
      <span class="up-reason">${esc(it.reason || "")}${cost} · ${esc(when)}</span>${notes}</div>
    ${it.mode_final === "deep" && p.digest ? paperDeep(p) : `<div class="skims">${paperSkim(p)}</div>`}</div>`;
}

async function renderUploads() {
  $("#more-wrap").hidden = true;
  await loadUploadIndex();
  const items = up.index.items || [];
  const done = new Set(items.map((i) => i.id));
  const pending = store.get(PENDING_KEY, []).filter((p) => !done.has(p.id));
  store.set(PENDING_KEY, pending);
  await refreshRuns(pending);
  const results = await Promise.all(items.map((i) => loadUpload(i.id)));
  updateUpBadge(items.length, pending.length);
  if (state.view !== "uploads") return;
  $("#feed").innerHTML = `<section class="day"><header class="day-head"><h2>我的上传</h2>
      <span class="stat">手动添加的论文 · 已完成 ${items.length} 篇${pending.length ? ` · ${pending.length} 篇处理中，页面每 20 秒自动刷新` : ""}</span></header>
    ${pending.map(pendingCard).join("")}${results.filter(Boolean).map(resultCard).join("")}
    ${!items.length && !pending.length ? `<div class="empty"><b>还没有上传过论文</b>点右上角的“添加论文”，粘贴链接或上传 PDF 就能解析。<br><button data-open-add>添加论文</button></div>` : ""}
  </section>`;
  schedulePoll(pending.length > 0);
}
function updateUpBadge(n, pendingN) { $("#up-n").textContent = pendingN ? `${pendingN} 处理中` : (n || ""); }
function schedulePoll(on) {
  clearTimeout(up.timer);
  if (!on) return;
  up.timer = setTimeout(async () => {
    if (state.view === "uploads") return renderUploads();
    await loadUploadIndex();
    const done = new Set((up.index.items || []).map((i) => i.id));
    const pending = store.get(PENDING_KEY, []).filter((p) => !done.has(p.id));
    store.set(PENDING_KEY, pending);
    updateUpBadge((up.index.items || []).length, pending.length);
    schedulePoll(pending.length > 0);
  }, 20000);
}

function handleUploadClick(t) {
  if (t.id === "add-toggle") { openAdd($("#add-panel").hidden); return true; }
  if (t.id === "add-close") { openAdd(false); return true; }
  if (t.id === "add-submit") { submitPaper(); return true; }
  if (t.id === "token-reset") { store.del(TOKEN_KEY); $("#token-box").hidden = false; t.hidden = true; $("#add-token").focus(); return true; }
  if (t.dataset.src) { setSrc(t.dataset.src); return true; }
  if (t.dataset.mode) {
    up.mode = t.dataset.mode;
    document.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b === t)));
    $("#mode-hint").textContent = MODE_HINT[up.mode];
    return true;
  }
  if (t.hasAttribute("data-open-add")) { openAdd(true); return true; }
  if (t.hasAttribute("data-upload-pdf")) { openAdd(true); setSrc("pdf"); return true; }
  if (t.dataset.retry) { openAdd(true, t.dataset.retry); return true; }
  if (t.dataset.forget) {
    store.set(PENDING_KEY, store.get(PENDING_KEY, []).filter((p) => p.id !== t.dataset.forget));
    renderUploads();
    return true;
  }
  return false;
}

(function wireUploads() {
  const drop = $("#drop"), input = $("#add-file");
  input.addEventListener("change", () => setFile(input.files[0] || null));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0] || null));
  $("#add-url").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitPaper(); } });
})();

boot();
