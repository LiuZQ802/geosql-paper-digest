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
  { name: "空间推理 / 地理知识", c: "--d5" },
];
const dirColor = (name) => `var(${(DIRS.find((d) => d.name === name) || { c: "--d0" }).c})`;
const VERDICT = { must_read: "必读", worth_reading: "值得读", skim_only: "速览即可" };
const METRICS = [["relevance", "相关"], ["transfer", "可迁移"], ["novelty", "新颖"], ["rigor", "严谨"]];
const PAGE = 10;   // 每次加载的天数
const KEY_STORE = "vault-key";

const state = {
  key: null, index: null, days: new Map(), shown: PAGE,
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
  renderDates();
  await render();
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
  if (!p.abstract) return "";
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
    <p class="byline">${authors(p)} · <span class="venue">${esc(p.venue || "")}</span> · ${esc(p.published || "")}</p>
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
      <div class="m">
        <span class="dir"><i class="sw" style="--c:${dirColor(p.direction)}"></i>${esc(p.direction || "其他")}</span>
        <span>${esc(p.venue || "")}</span>${date ? `<span>${esc(date)}</span>` : ""}
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
  return JSON.stringify([p.title, p.title_zh, p.one_liner, p.abstract, p.abstract_zh, p.digest, p.authors, p.venue])
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
        <span class="stat">${day.deep.length} 篇精读 · ${day.skim.length} 篇速览${day.stats?.candidates != null ? ` · 从 ${day.stats.candidates} 篇候选中选出` : ""}</span></header>
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
  if (t.dataset.view) { state.view = t.dataset.view; closeRail(); return render(); }
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

boot();
