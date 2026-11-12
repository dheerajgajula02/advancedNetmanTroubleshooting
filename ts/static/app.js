"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const words = s => String(s ?? "").replace(/_/g, " ");
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

/* ---------- the frame: drawn into #ts, inside the Netman GUI or on this service's own page ---------- */
const ROOT = document.getElementById("ts");
const BASE = ROOT.dataset.api || "";   /* where the api lives: "" here, "/troubleshooting/" inside Netman */
/* the sidebar has one entry per group; each group's pages are the tabs across the top */
const GROUPS = [
  {key: "incidents", href: "#/", tabs: [["now", "Now", "#/"], ["failures", "History", "#/failures"], ["flow", "How it works", "#/flow"]]},
  {key: "rules", href: "#/review", tabs: [["review", "Review", "#/review"], ["rules", "Rules", "#/rules"], ["tags", "Tags", "#/tags"]]},
  {key: "monitors", href: "#/monitors", tabs: [["monitors", "Monitors", "#/monitors"], ["mdash", "Dashboard", "#/monitors/dashboard"]]},
  {key: "ai", href: "#/models", tabs: [["models", "Models", "#/models"], ["collect", "Collect", "#/collect"], ["aidata", "Data", "#/ai-data"], ["train", "Train", "#/train"]]},
];
ROOT.innerHTML = `<nav class="ts-tabs" aria-label="Troubleshooting pages"><span class="ts-links" id="ts-links"></span><span class="ts-spacer"></span><span class="ts-tele" id="tele"></span><button class="find" id="find" type="button">Find anything <kbd>Ctrl K</kbd></button></nav>
<div id="main" tabindex="-1"><p class="muted">Loading</p></div>
<div class="scrim" id="scrim"></div>
<aside class="drawer" id="drawer" aria-labelledby="d-title" hidden></aside>
<div class="toast" id="toast" role="status"></div>
<div class="pal" id="pal" hidden><div class="palbox" role="dialog" aria-modal="true" aria-label="Find anything"><input id="pal-q" type="search" placeholder="Find a device, problem, rule, tag or page" autocomplete="off" aria-label="Find anything" aria-controls="pal-list"><ul id="pal-list" role="listbox"></ul></div></div>
<div class="sr" aria-live="polite" id="live"></div>`;
/* sidebar links to these pages, on this page: their group is named by the part after # */
const sidebarLinks = () => $$("nav.tabs a").map(a => { const u = new URL(a.href, location.href); return {a, group: u.origin === location.origin && u.pathname.replace(/\/$/, "") === location.pathname.replace(/\/$/, "") ? GROUPS.find(g => g.href === (u.hash || "#/")) : null}; }).filter(x => x.group);
/* the service lights sit at the foot of the sidebar when there is one, as the old page had them */
(() => {
  const side = $("aside.side"), tele = $("#tele");
  if (!side) return;
  const box = document.createElement("div");
  box.className = "ts-side";
  box.id = "tele";
  tele.remove();
  side.insertBefore(box, $(".side-foot", side));
})();

/* rule text: {name} is a blank that an earlier check fills in, named the way a person would say it */
const BLANKS = {found_device: "the device it found", alarm_device: "the device that raised the alarm", peer_device: "far-end device", peer_interface: "far-end port", far_device: "far-end device", far_interface: "far-end port", interface: "port",
  neighbor_id: "neighbor", neighbor_ip: "neighbor address", peer_ip: "peer address", expected_master: "usual master", current_master: "current master",
  route_via: "gateway", gateway_interface: "gateway interface", default_prefix: "default route", internet_target: "test address", prefix: "route",
  expected_protocol: "routing protocol", drift_lines: "changed lines", section: "changed section", changed_by: "who changed it", started_at: "restart time",
  loss_pct: "packet loss", error_detail: "error counts", lossy_links: "other bad links", working_border: "working border", cut_border: "cut-off border", mismatch: "difference"};
const tpl = s => esc(s).replace(/\{([a-z_]+)\}/g, (_, n) => `<span class="ph">${esc(BLANKS[n] || words(n))}</span>`);

const tfmt = (t, opts = {}) => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, ...opts});
const hm = t => tfmt(t, {second: undefined});
const dfmt = t => new Date(t * 1000).toLocaleDateString([], {month: "short", day: "numeric"});
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function span(s) {
  if (s < 90) return `${Math.max(0, Math.round(s))} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

const STATE = {needs_you: {t: "Needs you", c: "red"}, workaround: {t: "Working for now", c: "amber"}, working: {t: "Being worked on", c: "violet"}, fixed: {t: "Fixed", c: "green"}, cleared: {t: "Cleared", c: "grey"},
  held: {t: "Held back", c: "amber"}, watching: {t: "Watch only", c: "blue"}, info: {t: "Noted", c: "grey"}, handled: {t: "Handled", c: "grey"}};
const ticks = s => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
const plainCause = s => String(s ?? "").replace(/^[a-z_]+(\.[a-z_]+)+:\s*/i, "");   /* the AI opens with its cause label, which reads as noise to a person */
const stateOf = k => STATE[k] || {t: words(k), c: "grey"};
const ORIGIN_WORDS = {trap: "Trap", syslog: "Log message", gnmi: "Telemetry", health: "Heartbeat", ticket: "Ticket", manual: "Started by hand", monitor: "Monitor"};
const sourceTag = r => { const word = r.ticket ? `Ticket T-${r.ticket}` : ORIGIN_WORDS[r.source]; return word ? `<span class="src" title="Where this came from">${esc(word)}</span>` : ""; };
const byWord = b => b === "ai" ? `<span class="ai">AI</span>` : "Rules";

const S = {now: null, offline: false, tags: null, cache: new Map(), token: 0, q: new URLSearchParams(), picking: false, picked: new Set(), asking: false, last: null,
  rec: null, rule: null, frames: [], pos: 0, playing: false, timer: null, speed: 1, follow: false, drawer: null, poll: null, rules: null, ruleWant: {}, confirm: null, reviewAsk: {}};

async function api(path) {
  const r = await fetch(BASE + path, {cache: "no-store"});
  if (!r.ok) throw new Error(`${r.status} on ${path}`);
  return r.json();
}
async function post(path, body) {
  const r = await fetch(BASE + path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `The server answered ${r.status}.`);
  return j;
}
let toastTimer;
function toast(text, bad = false, undo = null) {
  const t = $("#toast");
  t.innerHTML = esc(text) + (undo ? '<button class="toast-undo" type="button">Undo</button>' : "");
  t.className = `toast on ${bad ? "bad" : ""} ${undo ? "undoable" : ""}`;
  if (undo) $(".toast-undo", t).onclick = () => { t.className = "toast"; undo(); };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast"; }, undo ? 8000 : bad ? 6000 : 3000);
}
const failed = e => `<div class="err">This did not load: ${esc(e.message)}. Check that the UI server is running, then reload.</div>`;

/* ---------- the tab, the nav and the lights: kept current on every page ---------- */
function setFavicon(color) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="${color}"/></svg>`;
  let fav = $("#fav");
  if (!fav) { fav = document.createElement("link"); fav.rel = "icon"; fav.id = "fav"; document.head.append(fav); }
  fav.href = "data:image/svg+xml," + encodeURIComponent(svg);
}
const TONES = {red: "#ef7672", amber: "#e3b35f", green: "#5fc493", grey: "#5d646e"};
const svc = (d, name) => (d.services.find(s => s.name === name) || {}).ok;

function bandFor(d) {
  if (S.offline) return {c: "amber", big: "Cannot reach the server", small: `The last good update was at ${d ? tfmt(d.time) : "no time yet"}, so what you see may be out of date.`};
  if (!svc(d, "Watcher")) return {c: "red", big: "The watcher has stopped", small: "Alarms are not being followed, so new problems will not show up here until it runs again."};
  const broken = together(d.broken.map(b => ({...b, id: b.run || b.key}))), n = broken.length;
  if (n) {
    const open = d.broken.filter(b => !["working", "fixed"].includes(b.state)).length;
    const rest = n > 1 ? ` and ${plural(n - 1, "other problem")}` : "";
    return {c: open ? "red" : "amber", big: `${n} ${n === 1 ? "thing is" : "things are"} broken right now`, small: `${esc(broken[0].title)}${rest}.${open ? "" : " All of it is being worked on."}`};
  }
  const checks = d.groups.reduce((a, g) => a + g.total, 0);
  if (!svc(d, "Heartbeat")) return {c: "amber", big: "The heartbeat has gone quiet", small: `It last looked at ${d.swept ? tfmt(d.swept) : "no time yet"}, so problems without an alarm could be missed.`};
  const left = attention(d).waiting.length;
  return {c: "green", big: "Everything is working", small: `All ${checks} checks pass. Last looked ${d.swept ? ago(d.swept) : "just now"}.` + (left ? ` ${plural(left, "earlier problem")} still ${left === 1 ? "needs" : "need"} a look.` : "")};
}

function drawChrome(d) {
  const b = bandFor(d);
  if ($("#brand-led")) $("#brand-led").className = `led ${b.c}`;
  setFavicon(TONES[b.c] || TONES.grey);
  const prefix = S.offline ? "Offline" : d.broken.length ? `(${d.broken.length}) Broken` : "OK";
  S.tabPrefix = prefix;
  document.title = `${prefix}: ${S.pageName || "Network Troubleshooting"}`;
  if (!d) return;
  drawCounts(d);
  const lamp = (on, name, what, bad = "red") => `<span title="${esc(what)}"${on ? "" : ' class="off"'}><i class="led ${on ? "green" : bad}"></i>${esc(name)}${on ? "" : bad === "off" ? " off" : " silent"}</span>`;
  const core = d.services.filter(s => ["Watcher", "Sweep"].includes(s.name));
  $("#tele").innerHTML = core.map(s => lamp(s.ok, s.name, s.note || "")).join("")
    + lamp(d.switches.repairs, "Repairs", "Rules put things back to known-good by themselves.", "off")
    + lamp(d.switches.ai, "AI takeover", "The AI investigates what the rules could not settle.", "off")
    + lamp(d.switches.ai_changes, "AI changes", d.switches.ai_changes ? "The AI may try careful changes, each one checked and undone if it does no good." : "The AI only reads; it changes nothing.", "off")
    + `<span class="stamp">${S.offline ? "Not updating, last" : "Updated"} ${tfmt(d.time)}</span>`;
}
/* the numbers beside Now and Review, on the tabs and in the sidebar */
function drawCounts(d) {
  if (!d) return;
  const waiting = attention(d), broken = waiting.live.length + waiting.waiting.length;
  const counts = {incidents: [broken, d.broken.length ? "red" : "amber"], rules: [d.review.pending, "violet"]};
  const set = (el, n, cls, base) => { if (el) { el.textContent = n || ""; el.className = `${base} ${n ? cls : ""}`; } };
  set($("#n-broken"), broken, counts.incidents[1], "count");
  set($("#n-review"), d.review.pending, "violet", "count");
  sidebarLinks().forEach(({a, group}) => {
    const c = counts[group.key];
    if (!c) return;
    let el = $(".ts-count", a);
    if (!el) { el = document.createElement("span"); el.className = "ts-count"; a.append(el); }
    set(el, c[0], c[1], "ts-count");
  });
}
function naming(name) { S.pageName = name; document.title = `${S.tabPrefix ? S.tabPrefix + ": " : ""}${name}`; }

async function refreshNow() {
  try {
    const d = await api("api/now");
    S.offline = false;
    const changed = !S.now || JSON.stringify({...d, time: 0, watcher_seen: 0, swept: 0}) !== JSON.stringify({...S.now, time: 0, watcher_seen: 0, swept: 0});
    S.now = d;
    drawChrome(d);
    if (S.page === "now") drawNow(d, changed);
  } catch (e) {
    S.offline = true;
    if (S.now) { drawChrome(S.now); if (S.page === "now") drawNow(S.now, true); }
  }
}
setInterval(() => { if (!document.hidden) refreshNow(); }, 10000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshNow(); });

function setNav(name, lit = name) {
  S.page = name;
  const group = GROUPS.find(g => g.tabs.some(t => t[0] === lit)) || GROUPS[0];
  const count = k => k === "now" ? '<span class="count" id="n-broken"></span>' : k === "review" ? '<span class="count" id="n-review"></span>' : "";
  $("#ts-links").innerHTML = group.tabs.map(([k, label, href]) => `<a href="${href}"${k === lit ? ' aria-current="page"' : ""}>${esc(label)}${count(k)}</a>`).join("");
  sidebarLinks().forEach(({a, group: g}) => { if (g === group) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  drawCounts(S.now);
}

/* ---------- ai data ---------- */
const fmtN = n => Number(n || 0).toLocaleString();
const fmtK = n => { n = Number(n || 0); return n >= 1e6 ? (n / 1e6).toFixed(1) + " M" : n >= 1e3 ? (n / 1e3).toFixed(1) + " k" : String(Math.round(n)); };
const fmtBytes = b => { b = Number(b || 0); return b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.round(b / 1e3) + " kB"; };
const share = (a, b) => b ? Math.round(100 * a / b) + "%" : "0%";
const EPISODE_KINDS = {agent: "AI takeover", analyst: "Written read", desk: "Service desk", notes: "Memory note", person: "Someone at a computer", other: "Other"};
const SOURCE_WORDS = {instructions: "Instructions", tools: "Tool list", incident: "The incident", rules: "What the rules found", retrieved: "Retrieved (RAG)", live: "Live sources", snapshot: "Stored, not sent"};
const RETRIEVAL = ["search_memory", "search_runs", "search_rejected", "list_rules", "list_tags"];
const BAD_STEPS = ["refused", "invalid", "error", "undone", "reverted", "rolled_back", "failed"];
const dataHash = (q, over) => {
  const p = new URLSearchParams(q);
  if (!("page" in over)) p.delete("page");
  Object.entries(over).forEach(([k, v]) => { if (v) p.set(k, v); else p.delete(k); });
  const s = p.toString();
  return "#/ai-data" + (s ? "?" + s : "");
};
const episodeResult = e => (e.outcome && (e.outcome.outcome || e.outcome.assessment)) || e.status;
function dataTile(label, value, note) { return `<div class="dtile"><dt>${esc(label)}</dt><dd>${esc(value)}</dd><small>${esc(note)}</small></div>`; }
function dataDown(d) {
  return `<div class="wrap"><div class="head"><h1>Data</h1></div><div class="panel empty">The training database is not answering, so nothing can be shown. New data is being kept in a file and goes in when it is back. Start it with <code>docker compose -f deploy/training-db/compose.yaml up -d</code>.${d.why ? `<br><span class="muted">${esc(d.why)}</span>` : ""}</div></div>`;
}
async function pageData(q) {
  setNav("aidata");
  naming("Data");
  const d = await api("api/training?" + q.toString());
  if (d.down) { $("#main").innerHTML = dataDown(d); return; }
  const t = d.totals, kind = q.get("kind") || "", cap = q.get("capture") || "";
  const seg = (name, on, list) => `<div class="seg">${list.map(([k, l]) => `<a href="${dataHash(q, {[name]: k})}" ${on === k ? 'aria-current="page"' : ""}>${l}</a>`).join("")}</div>`;
  const from = d.total ? (d.page - 1) * d.size + 1 : 0, to = Math.min(d.total, d.page * d.size);
  const row = e => `<tr data-ep="${esc(e.id)}"><td class="nw">${esc(dfmt(e.started))}, ${esc(hm(e.started))}</td>
    <td><b>${esc(e.title || e.run_id || "A model call")}</b>${e.capture === "backfill" ? ` <span class="chip">older run</span>` : ""}<br><small class="muted">${esc(EPISODE_KINDS[e.kind] || e.kind)}</small></td>
    <td class="wide roomy num">${fmtK(e.input_chars)}</td><td class="num">${fmtN(e.steps)}</td><td class="wide num">${e.steps ? share(e.reasons, e.steps) : "n/a"}</td><td class="wide num">${e.thinking_chars ? fmtK(e.thinking_chars) : "none"}</td>
    <td class="wide roomy num">${e.tokens_in ? `${fmtK(e.tokens_in)} / ${fmtK(e.tokens_out)}` : "n/a"}</td><td>${esc(words(episodeResult(e)))}</td></tr>`;
  $("#main").innerHTML = `<div class="wrap">
    <div class="head"><h1>Data</h1><p>Everything the AI was given, what it thought and why it ran each command, kept for fine-tuning.</p></div>
    <dl class="dtiles">
      ${dataTile("Runs stored", fmtN(t.runs), "every investigation, AI or not")}
      ${dataTile("AI sessions", fmtN(t.episodes), `${fmtN(t.live)} captured in full`)}
      ${dataTile("Commands", fmtN(t.steps), `${share(t.reasons, t.steps)} have a stated reason`)}
      ${dataTile("Thinking", fmtK(t.thinking_chars) + " chars", `from ${fmtN(t.with_thinking)} of ${fmtN(t.llm_calls)} model calls`)}
      ${dataTile("Tokens", `${fmtK(t.tokens_in)} / ${fmtK(t.tokens_out)}`, "in and out, captured sessions")}
      ${dataTile("People's decisions", fmtN(t.person_labels), `${fmtN(t.faults)} drills with ground truth`)}
      ${dataTile("Database", fmtBytes(t.bytes), t.waiting ? `${fmtN(t.waiting)} rows waiting to go in` : "nothing waiting to go in")}
    </dl>
    <div id="d-sets" class="cstack dsets"></div>
    <div class="dfilters">${seg("kind", kind, [["", "Everything"], ["agent", "AI takeovers"], ["analyst", "Written reads"], ["desk", "Service desk"], ["notes", "Memory notes"], ["person", "People at computers"]])}${seg("capture", cap, [["", "All"], ["live", "Captured in full"], ["backfill", "Older runs"]])}
      <input id="d-q" type="search" placeholder="Search device, title or run" aria-label="Search" value="${esc(q.get("q") || "")}"></div>
    <div class="tblwrap"><table class="tbl dtbl"><thead><tr><th>When</th><th>What</th><th class="wide roomy num">Given (chars)</th><th class="num">Steps</th><th class="wide num">With a reason</th><th class="wide num">Thinking</th><th class="wide roomy num">Tokens in / out</th><th>Ended as</th></tr></thead>
      <tbody>${d.items.map(row).join("") || `<tr><td colspan="8" class="empty">Nothing matches. <a href="#/ai-data">Clear the filters</a>.</td></tr>`}</tbody></table></div>
    <div class="pager"><span class="count">${from}–${to} of ${fmtN(d.total)}</span>${d.page > 1 ? `<a class="btn small" href="${dataHash(q, {page: String(d.page - 1)})}">Previous</a>` : ""}${d.page < d.pages ? `<a class="btn small" href="${dataHash(q, {page: String(d.page + 1)})}">Next</a>` : ""}</div>
    <section class="panel dsnaps"><div class="head"><h2>Snapshots</h2><p>One folder with the database, every run, the search index, the rules and the fine-tuning files.</p><span class="acts"><button class="btn" id="d-snap" type="button">Take a snapshot</button></span></div>
      ${d.snapshots.length ? `<ul class="dlist">${d.snapshots.map(s => `<li><b>${esc(dfmt(s.taken))}, ${esc(hm(s.taken))}</b> <span class="muted">${fmtBytes(s.bytes)}, ${fmtN(s.runs)} runs, ${fmtN(s.steps)} commands, ${fmtN(s.examples)} training examples</span><br><code>${esc(s.path)}</code></li>`).join("")}</ul>` : `<p class="muted">None taken yet.</p>`}</section>
  </div>`;
  $$(".dtbl tr[data-ep]").forEach(tr => tr.onclick = () => { location.hash = "#/ai-data/" + tr.dataset.ep; });
  drawDatasets().catch(() => {});
  $("#d-q").onchange = e => { location.hash = dataHash(q, {q: e.target.value.trim()}); };
  $("#d-snap").onclick = async e => {
    e.target.disabled = true; e.target.textContent = "Taking a snapshot";
    try { const s = await post("api/training/snapshot"); toast(`Snapshot saved, ${fmtBytes(s.bytes)}.`); route(); }
    catch (x) { toast(x.message, true); e.target.disabled = false; e.target.textContent = "Take a snapshot"; }
  };
}
function inputsBlock(d) {
  const sent = d.sections.filter(s => s.sent), kept = d.sections.filter(s => !s.sent), total = sent.reduce((a, s) => a + Number(s.chars), 0);
  const line = s => `<button type="button" class="irow" data-section="${s.n}"><i class="sw src-${esc(s.source)}"></i><b>${esc(s.section)}</b><span class="muted">${esc(SOURCE_WORDS[s.source] || s.source)}</span><span class="num">${fmtN(s.chars)} chars${s.sent ? `, ${share(s.chars, total)}` : ""}</span></button>`;
  const present = [...new Set(sent.map(s => s.source))];
  return `<section class="panel"><h2>What it was given</h2><p class="muted">${fmtN(total)} characters at the start. Select any part to read it exactly as the model did.</p>
    ${d.settings && d.settings.prompt_cut ? `<p class="dwarn">The prompt reached its 12,000 character limit, so the end of the incident text was cut off.</p>` : ""}
    <div class="ibar">${sent.map(s => `<button type="button" class="src-${esc(s.source)}" style="flex-grow:${Number(s.chars)}" data-section="${s.n}" title="${esc(s.section)}, ${fmtN(s.chars)} characters" aria-label="${esc(s.section)}"></button>`).join("")}</div>
    <div class="ilegend">${present.map(s => `<span><i class="sw src-${esc(s)}"></i>${esc(SOURCE_WORDS[s] || s)}</span>`).join("")}</div>
    <div class="ilist">${sent.map(line).join("")}</div>
    ${kept.length ? `<h3>Also stored with it</h3><div class="ilist">${kept.map(line).join("")}</div>` : ""}</section>`;
}
function stepHtml(s, prev) {
  const a = s.action || {}, args = Object.entries(a.args || {}).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ");
  const bad = BAD_STEPS.includes(s.outcome) || BAD_STEPS.includes(s.change_status), cls = bad ? "bad" : s.kind === "change" ? "chg" : s.kind === "finish" ? "end" : "";
  const think = s.thinking && (!prev || prev.llm_call !== s.llm_call) ? `<details class="think"><summary><span class="ai">AI</span> Thinking, ${fmtN(s.thinking.length)} characters</summary><p>${esc(s.thinking)}</p></details>` : "";
  return `${think}<li class="dstep ${cls}"><button type="button" data-step="${s.n}"><span class="n">#${s.n}</span><b>${esc(a.tool)}</b><span class="args">(${esc(args.length > 200 ? args.slice(0, 200) + "…" : args)})</span>
    ${RETRIEVAL.includes(a.tool) ? `<span class="chip">retrieval</span>` : ""}<span class="out">${esc(words(s.change_status || s.outcome || ""))}</span><span class="lat">${s.latency_ms == null ? "" : fmtN(s.latency_ms) + " ms"}</span></button>
    ${s.reason ? `<p class="why">${esc(s.reason)}</p>` : ""}</li>`;
}
async function pageEpisode(id) {
  setNav("aidata");
  const d = await api("api/training/" + id);
  if (d.down) { $("#main").innerHTML = dataDown(d); return; }
  naming(d.title || "Data");
  const fact = (k, v) => `<div><dt>${esc(k)}</dt><dd>${v}</dd></div>`;
  const people = d.labels.filter(l => l.labeler === "person"), broken = st => (st.state.broken || st.state.problems || []).length;
  const states = d.states.length ? `<section class="panel"><h2>The network around it</h2><ul class="dlist">${d.states.map(st => `<li><b>${esc(words(st.phase))}</b>${st.n ? ` <span class="muted">at step ${st.n}</span>` : ""}: ${broken(st)} problem${broken(st) === 1 ? "" : "s"} showing</li>`).join("")}</ul></section>` : "";
  $("#main").innerHTML = `<div class="wrap" style="max-width:1060px">
    <a href="#/ai-data">Back to Data</a>
    <div class="head" style="margin-top:8px"><h1>${esc(d.title || d.run_id || "A model call")}</h1></div>
    <dl class="factrow">${fact("Kind", esc(EPISODE_KINDS[d.kind] || d.kind))}${fact("Model", esc(d.model || "unknown"))}${fact("Ended as", esc(words(episodeResult(d))))}${fact("Started", `${esc(dfmt(d.started))}, ${esc(hm(d.started))}`)}${d.run_id ? fact("Run", `<a href="#/run/${esc(d.run_id)}">Open the run</a>`) : ""}${fact("Thinking", d.thinking_chars ? `${fmtN(d.thinking_chars)} chars` : "none")}${fact("Tokens in / out", d.tokens_in ? `${fmtN(d.tokens_in)} / ${fmtN(d.tokens_out)}` : "not recorded")}</dl>
    ${d.settings && d.settings.note ? `<p class="dwarn">${esc(d.settings.note)}</p>` : ""}
    ${inputsBlock(d)}
    ${d.steps.length ? `<section class="panel"><h2>What it did</h2><p class="muted">${fmtN(d.steps.length)} commands, ${fmtN(d.reasons)} with a stated reason, ${fmtN(d.retrievals)} of them searches. Select a command for everything about it.</p><ol class="dtraj">${d.steps.map((s, i) => stepHtml(s, d.steps[i - 1])).join("")}</ol></section>` : d.calls.length ? `<section class="panel"><h2>What it answered</h2>${d.calls.map(c => `${c.thinking ? `<details class="think" open><summary><span class="ai">AI</span> Thinking, ${fmtN(c.thinking.length)} characters</summary><p>${esc(c.thinking)}</p></details>` : ""}${c.content ? `<pre>${esc(c.content)}</pre>` : ""}`).join("")}</section>` : ""}
    ${states}
    ${people.length ? `<section class="panel"><h2>What people decided</h2><ul class="dlist">${people.map(l => `<li><b>${esc(l.label.decision)}</b> #${esc(l.label.proposal)} ${esc(l.label.title)}${l.label.reason ? `<br><span class="muted">${esc(l.label.reason)}</span>` : ""}</li>`).join("")}</ul></section>` : ""}
    ${d.truth.length ? `<section class="panel"><h2>Hidden from the AI</h2><details><summary>Show what was really broken, from the drill</summary><pre>${esc(JSON.stringify(d.truth.map(t => ({drill: t.drill, ...t.spec})), null, 1))}</pre></details></section>` : ""}
  </div>`;
  $$("[data-section]").forEach(b => b.onclick = () => openSection(id, b.dataset.section));
  $$("[data-step]").forEach(b => b.onclick = () => openStep(id, b.dataset.step));
}
function itemsHtml(s) {
  const it = s.items;
  if (it == null) return "";
  if (s.source === "tools") return `<h3>The tools</h3><ul class="dlist">${it.map(t => `<li><b>${esc(t.function.name)}</b><br><span class="muted">${esc(t.function.description)}</span></li>`).join("")}</ul>`;
  if (Array.isArray(it) && it.length && typeof it[0] === "object") {
    return `<h3>${fmtN(it.length)} records behind this</h3><ul class="dlist">${it.map(x => `<li>${x.ref ? `<span class="chip">${esc(x.ref)}</span> ` : ""}${x.source ? `<span class="muted">${esc(x.source)}</span> ` : ""}<b>${esc(x.title || `${x.device || ""} ${x.subject || ""}`)}</b>${x.score != null ? ` <span class="muted">score ${esc(x.score)}</span>` : ""}${x.text ? `<br><span class="muted">${esc(String(x.text).slice(0, 600))}</span>` : ""}</li>`).join("")}</ul>`;
  }
  return `<h3>The stored data</h3><pre>${esc(JSON.stringify(it, null, 1).slice(0, 60000))}</pre>`;
}
async function openSection(id, n) {
  try {
    const s = await api(`api/training/${id}/input/${n}`);
    openDrawer("sec-" + n, false, `${top_(esc(SOURCE_WORDS[s.source] || s.source))}<h2 id="d-title">${esc(s.section)}</h2><p class="about">${fmtN(s.chars)} characters, ${s.sent ? "sent to the model" : "stored for training and not sent"}.</p>${s.body && s.source !== "tools" ? `<h3>As the model read it</h3><pre>${esc(s.body)}</pre>` : ""}${itemsHtml(s)}`);
  } catch (e) { toast(e.message, true); }
}
async function openStep(id, n) {
  try {
    const s = await api(`api/training/${id}/step/${n}`), a = s.action || {}, v = s.validation || {};
    openDrawer("step-" + n, false, `${top_(`<span class="ai">AI</span> Command ${esc(n)}`)}<h2 id="d-title">${esc(a.tool)}</h2><div class="outcome">${esc(words(s.outcome || ""))}</div>
      ${s.reason ? `<h3>Why it ran this</h3><p>${esc(s.reason)}</p>` : `<p class="note">No stated reason was recorded for this command.</p>`}
      ${s.thinking ? `<h3>What it was thinking</h3><p class="dthink">${esc(s.thinking)}</p>` : ""}
      <h3>What it asked for</h3><pre>${esc(JSON.stringify(a.args || {}, null, 1))}</pre>
      ${s.saw ? `<h3>What came back</h3><p>${esc(s.saw)}</p>` : ""}${s.tool_output ? `<h3>Full output</h3><pre>${esc(s.tool_output)}</pre>` : ""}
      ${(v.ran || []).length ? `<h3>Commands that really ran</h3>${v.ran.map(x => `<pre class="cmd">${esc(x)}</pre>`).join("")}` : ""}
      ${v.change ? `<h3>The change</h3><pre>${esc(JSON.stringify(v.change, null, 1))}</pre>` : ""}
      ${s.given_back ? `<details><summary>Exactly what went back to the model</summary><pre>${esc(s.given_back)}</pre></details>` : ""}`);
  } catch (e) { toast(e.message, true); }
}

/* ---------- how it works: the whole flow, drawn and played ---------- */
/* nodes: x, y (centre), title, line under it, kind (sets the colour) */
const FLOW_NODES = {
  ticket: [90, 150, "Ticket", "a person writes in", "trigger"],
  trap: [90, 280, "SNMP trap", "link, BGP, OSPF, VRRP", "trigger"],
  monitor: [90, 410, "Monitor", "Prometheus conditions", "trigger"],
  sweep: [90, 540, "Heartbeat", "every 20 minutes", "trigger"],
  desk: [250, 150, "Service desk", "asks what it needs", "person"],
  confirm: [250, 280, "Wait and merge", "2 min, one run a burst", "rules"],
  holds: [250, 410, "Holds long enough", "once per episode", "rules"],
  checks: [250, 540, "Health checks", "what fails right now", "rules"],
  pick: [410, 340, "Pick the rules", "by kind and tags", "rules"],
  first: [490, 480, "First look", "back to known-good", "change"],
  rules: [570, 340, "Rules in Jenkins", "one build per rule", "rules"],
  repair: [730, 340, "Repair in Jenkins", "safe? change, verify", "change"],
  context: [890, 340, "Context", "past runs, memory", "rules"],
  needai: [1050, 340, "Does the AI look?", "the hand-over rules", "decide"],
  check: [1050, 480, "AI check", "is it still broken?", "ai"],
  agent: [1050, 620, "Support agent", "investigates, fixes", "ai"],
  report: [1195, 340, "Report", "read, email, ticket", "end"],
  suggest: [1195, 70, "Suggestions", "rules, monitors, tags", "learn"],
  review: [890, 70, "Review", "a person decides", "person"],
  install: [570, 70, "Installed", "rule + pipeline, monitor", "learn"],
};
const NW = 140, NH = 50;
/* edges: from, to, path (null = straight), kind */
const FLOW_EDGES = [
  ["ticket", "desk"], ["trap", "confirm"], ["monitor", "holds"], ["sweep", "checks"],
  ["desk", "pick", "M320,150 C365,150 380,250 395,315"], ["confirm", "pick", "M320,280 C350,280 360,330 340,340"], ["holds", "pick", "M320,410 C350,410 360,350 340,340"], ["checks", "pick", "M320,540 C370,540 400,450 405,365"],
  ["pick", "rules"], ["pick", "first", "M410,365 C410,440 420,480 420,480"], ["first", "rules", "M560,480 C575,480 570,420 570,365"],
  ["rules", "repair"], ["repair", "context"], ["rules", "context", "M580,315 C640,240 840,240 880,315"],
  ["context", "needai"], ["needai", "report"], ["needai", "check"], ["needai", "agent", "M985,350 C930,420 930,600 980,620"],
  ["check", "report", "M1120,480 C1170,480 1185,430 1185,365"], ["check", "agent"], ["agent", "report", "M1120,620 C1240,620 1250,470 1215,365"],
  ["report", "suggest", null, "learn"], ["suggest", "review", null, "learn"], ["review", "install", null, "learn"],
  ["install", "pick", "M520,95 C470,190 420,230 410,315", "learn"], ["install", "monitor", "M500,70 L12,70 Q4,70 4,78 L4,402 Q4,410 12,410 L20,410", "learn"],
  ["report", "desk", "M1150,315 C1120,215 1090,215 1050,215 L330,215 C300,215 290,200 290,175", "person"],
];
const flowEdgeKey = (a, b) => `${a}>${b}`;
function flowEdgePath([a, b, d]) {
  if (d) return d;
  const [x1, y1] = FLOW_NODES[a], [x2, y2] = FLOW_NODES[b];
  if (y1 === y2) return `M${x1 + (x2 > x1 ? NW / 2 : -NW / 2)},${y1} L${x2 + (x2 > x1 ? -NW / 2 : NW / 2)},${y2}`;
  if (x1 === x2) return `M${x1},${y1 + (y2 > y1 ? NH / 2 : -NH / 2)} L${x2},${y2 + (y2 > y1 ? -NH / 2 : NH / 2)}`;
  return `M${x1},${y1} L${x2},${y2}`;
}
const FLOW_KIND = {trigger: "Something starts a run", person: "A person", rules: "Rules and checks", change: "Changes the network", decide: "A decision", ai: "The AI", end: "The end of a run", learn: "Learning for next time"};
/* what each box is, for the panel beside the chart */
const FLOW_DOCS = {
  ticket: ["Someone writes in on the service desk. A ticket goes to the support agent with everything it needs, and nothing is settled until the person says they can see it working.", "Service desk :8771, ts/desk.py, ts/tickets.py"],
  trap: ["A device sends an SNMP trap: a port went down, a BGP or OSPF neighbour dropped, a VRRP master moved. Traps are read from the trap log as they arrive.", "ts/intake.py"],
  monitor: ["A monitor watches Prometheus numbers and starts a run when its conditions hold. It counts only what happened after it was accepted, fires once per episode, and stays quiet while a test job is changing the network. A monitor the AI suggested is marked AI monitor everywhere its runs appear.", "ts/monitors.py, Monitors page, Grafana dashboard"],
  sweep: ["Every 20 minutes the watcher checks the whole network the same way a person would: can each host reach what it should, are the neighbours up.", "ts/sweep.py"],
  desk: ["The desk talks to the person, asks only what it needs, and passes everything on through the ticket. When the work is done it tells them and asks if they can see it working.", "ts/desk.py"],
  confirm: ["A trap is looked at 2 minutes after it arrives, so a flap that settles by itself is seen as one. Alarms about the same thing that arrive close together become one run.", "intake.Waiting, CONFIRM_DELAY 120 s"],
  holds: ["The conditions must hold for the monitor's hold time before it fires, then it waits for its cooldown before it can fire again.", "monitors.Watch"],
  checks: ["The sweep's checks say what is failing right now. Each failure becomes an event, investigated like a trap.", "ts/sweep.py"],
  pick: ["The rules that cover this kind of event, or carry the monitor's tags, run in order: those placed at the beginning, the main rule, those at the end. A watch-only rule runs and is recorded but decides nothing. If any rule here was written by the AI, the first look comes before them.", "engine.chain_for"],
  first: ["Only before rules the AI wrote: has anything moved from known-good? Each device's config is compared with the golden config. Whatever differs and nothing on record explains is put back first, as its own repair build, so those rules start from the network as it is meant to be.", "pipeline.before_rules, firstlook"],
  rules: ["Each rule is a pipeline in Jenkins, troubleshooting-pipelines. Every step is a stage: the troubleshooter runs the check and the build decides where the rule goes next. A rule ends with a cause, a stop, or a call for someone to look.", "jenkins_runs.py, pipelines.py"],
  repair: ["A cause with a repair rule gets its own Jenkins build: is it safe to change, the change, did it work, then keep it or undo it. It holds the network change lock from the change to the end. It is held back while a deployment or a test job is running.", "fixer.py, troubleshooting-pipelines"],
  context: ["What else was going on: Jenkins, GitHub, Prometheus, Grafana and the data lake, every earlier run like this one, and the AI's own notes. Then the pattern: how often this has happened today and this week.", "sources/, history.py, rag.py"],
  needai: ["The AI takes over when a ticket has no rule, when the rules ran out or asked for someone, when they found a cause but have no repair, or when the repair failed. It does not for things only a person can fix, like a dead link, nor while a job is changing the network. After rules the AI wrote, the AI check always comes first.", "agent.needed"],
  check: ["After rules the AI wrote, a model that cannot change anything looks at whether the trouble is still there: at most 8 reads and 4 minutes, then working, still broken, or unsure. Working ends the run there. Only otherwise does the support agent start. Its model is chosen on the AI models page, under AI check.", "aicheck.py"],
  agent: ["The support agent reads, tests and changes the network. Every change is tried with its own check and undone if the check does not pass. It has 30 reads, 12 changes or suggestions, 3 changes a run and 6 an hour, and 10 minutes, 15 on a ticket. At most 8 sessions an hour, and the same problem on the same device goes to a person within half an hour.", "agent.py, tools.py"],
  report: ["If anything needs saying, a model reads the whole run and it is emailed. A run that cleared by itself or that the AI check found working is not emailed, unless it keeps coming back. A ticket goes back to the desk, which asks the person to confirm.", "report.py, analyst.py"],
  suggest: ["The support agent may suggest a rule that would have solved this without it, and after every run with a cause the AI looks for a monitor that would have caught it sooner. Nothing is installed until a person accepts it.", "review.py, monitor_ai.py"],
  review: ["A person reads each suggestion with its proof: what the rule would have done on past runs, how often a monitor would have fired. Accept, or reject with a reason the AI sees next time.", "Rules & review page"],
  install: ["An accepted rule is checked and gets its Jenkins pipeline straight away. An accepted monitor starts watching from that moment. Next time, the rules handle it, and the AI only checks.", "rulebook.py, pipelines.sync, monitors.add"],
};
/* the stories: each step is a box and what is happening there; the edge to it is taken from the box before */
const FLOW_STORIES = {
  ticket: {name: "Ticket", lead: "Someone writes in on the service desk.", outcomes: {
    fixed: ["The AI fixes it", [
      ["ticket", "A person writes in: they cannot reach a web site from their computer."],
      ["desk", "The desk asks what it needs to know, like which computer and since when, and opens the ticket."],
      ["pick", "No rule covers a ticket like this yet, so nothing is checked before the AI."],
      ["rules", "Rules that cover tickets of this kind would run here, each as a Jenkins build."],
      ["context", "Every earlier run like this, the AI's notes, and what Jenkins, GitHub and Prometheus saw lately."],
      ["needai", "A person wrote in and no rule covers it, so the support agent takes it."],
      ["agent", "It reads, tests, finds the cause and tries a fix with its own check. The fix stays only because the check passed."],
      ["report", "The run is written up and emailed. The ticket goes back to the desk."],
      ["desk", "The desk tells the person and asks if they can see it working. Only their answer settles the ticket."],
      ["suggest", "The agent suggests a rule that would have solved this without it."],
      ["review", "A person reads the suggestion with its proof and accepts it."],
      ["install", "The rule is installed and gets its own Jenkins pipeline. Next time the rules handle it."]]],
    person: ["It needs a person", [
      ["ticket", "A person writes in: the office has had no internet since this morning."],
      ["desk", "The desk asks what it needs and opens the ticket."],
      ["pick", "No rule covers it."],
      ["rules", "Nothing to run here."],
      ["context", "Past runs show the same border lost its ISP before."],
      ["needai", "A person wrote in, so the support agent takes it."],
      ["agent", "Both borders are up but the ISP is silent. Nothing on our side can fix that, so it ends as needs a person, with what it found."],
      ["report", "Emailed as NEEDS YOU. The ticket goes back to the desk."],
      ["desk", "The desk tells the person what is happening and that an engineer has it."]]],
  }},
  trap: {name: "Trap", lead: "A device sends an SNMP trap.", outcomes: {
    repaired: ["The rules repair it", [
      ["trap", "r3 sends linkDown for Ethernet2."],
      ["confirm", "Wait 2 minutes, so a flap that settles counts once. The link is still down."],
      ["pick", "The link-down rule covers this event."],
      ["rules", "Its Jenkins build checks the port, the far end and the config: Ethernet2 was shut down by hand, and nothing on record explains it."],
      ["repair", "The repair build: safe to change? Yes. Put the config back to known-good. Did it work? The link is up. Keep it."],
      ["context", "Gathered for the record: past runs and what else happened."],
      ["needai", "The rules found the cause and fixed it, so the AI is not needed."],
      ["report", "Emailed as FIXED, with what was changed and why."]]],
    noFix: ["No repair, the AI takes over", [
      ["trap", "r4 sends linkDown, then linkUp, again and again."],
      ["confirm", "Merged into one run after 2 minutes."],
      ["pick", "The flapping rule covers this."],
      ["rules", "Its build finds a link that keeps dropping, with errors on the port."],
      ["context", "This port flapped twice this week."],
      ["needai", "The rules found the cause, but no rule can repair it."],
      ["agent", "It moves traffic off the port with an OSPF cost change, held as a temporary stop-gap, and says the port needs a person."],
      ["report", "Emailed: the stop-gap holds, and someone has to look at the hardware."],
      ["suggest", "It suggests a monitor on this port's error rate, so it is caught sooner next time."],
      ["review", "A person accepts the monitor."],
      ["install", "The monitor starts watching from now. Only what happens after this moment counts."],
      ["monitor", "From now on this monitor can start runs of its own, marked AI monitor."]]],
  }},
  monitor: {name: "AI monitor", lead: "A monitor the AI suggested starts the run.", outcomes: {
    working: ["The AI check finds it working", [
      ["monitor", "The AI monitor on s4's uplink sees the link flapping. It is marked AI monitor, in the run list, on the run page and in Jenkins."],
      ["holds", "The flapping holds for the hold time, so it fires, once for this episode."],
      ["pick", "The rules carrying this monitor's tags run. They were written by the AI, so the first look comes first."],
      ["first", "Has anything moved from known-good? Nothing has, so nothing is put back."],
      ["rules", "The AI's rules run as Jenkins builds and find the link flapping."],
      ["context", "Past runs, memory, sources."],
      ["needai", "The rules found the cause but have no repair. The rules were the AI's, so the AI checks first."],
      ["check", "It reads how long the link has been up and whether it dropped again: up for twelve minutes, no drops. Working."],
      ["report", "Working, so the support agent never starts. Not emailed, unless it keeps coming back."]]],
    broken: ["Still broken, the support agent starts", [
      ["monitor", "The AI monitor on r4 sees packet loss on its transit link. Marked AI monitor."],
      ["holds", "It holds, so the monitor fires."],
      ["pick", "The AI's rules for this monitor's tags. The first look comes first."],
      ["first", "r4's config differs from known-good and nothing explains it, so it is put back first, as its own repair build."],
      ["rules", "The AI's rules run and find loss with no errors on the port."],
      ["context", "Past runs, memory, sources."],
      ["needai", "A cause and no repair. The AI checks first."],
      ["check", "Loss is still there in the last minute. Still broken."],
      ["agent", "The support agent takes over, within its limits, and works it like any other run."],
      ["report", "Written up and emailed with what the agent did."]]],
  }},
  sweep: {name: "Heartbeat", lead: "The 20-minute sweep finds something failing.", outcomes: {
    quiet: ["Nothing really wrong", [
      ["sweep", "The heartbeat checks every host and neighbour."],
      ["checks", "h2 failed one reach test."],
      ["pick", "The reachability rule covers this."],
      ["rules", "It confirms first, and h2 reaches everything now. The run stops at the confirm step and is not kept, so nothing piles up."]]],
    cause: ["It finds a cause", [
      ["sweep", "The heartbeat checks every host and neighbour."],
      ["checks", "h4 cannot reach the internet."],
      ["pick", "The internet rule covers this."],
      ["rules", "Both borders reach the internet but h4 does not: its default route is gone."],
      ["repair", "A repair rule puts the default route back from known-good, and the check passes."],
      ["context", "Past runs and sources."],
      ["needai", "Fixed by the rules."],
      ["report", "Emailed as FIXED."]]],
  }},
};
/* the Jenkins stages a story's boxes show as, in troubleshooting-run */
const FLOW_STAGE = {ticket: "Trigger", trap: "Trigger", monitor: "Trigger", sweep: "Trigger", first: "First look", rules: "Rules", repair: "Repair", check: "AI check", agent: "AI support agent", report: "Report"};

function flowSvg() {
  const nodes = Object.entries(FLOW_NODES).map(([id, [x, y, t, sub, kind]]) => `<g class="fnode k-${kind}" data-node="${id}" tabindex="0" role="button" aria-label="${esc(t)}: ${esc(sub)}" transform="translate(${x - NW / 2},${y - NH / 2})">
      <rect width="${NW}" height="${NH}" rx="10"></rect><text x="${NW / 2}" y="21" class="ft">${esc(t)}</text><text x="${NW / 2}" y="37" class="fs">${esc(sub)}</text>${id === "monitor" ? `<g class="aimon-tag"><rect x="${NW - 30}" y="-9" width="38" height="16" rx="4"></rect><text x="${NW - 11}" y="3">AI</text></g>` : ""}</g>`).join("");
  const edges = FLOW_EDGES.map(e => `<path class="fedge ${e[3] ? "e-" + e[3] : ""}" id="fe-${e[0]}-${e[1]}" data-edge="${flowEdgeKey(e[0], e[1])}" d="${flowEdgePath(e)}" marker-end="url(#farrow)"></path>`).join("");
  const lanes = `<text class="flane" x="20" y="105">START</text><text class="flane" x="370" y="300">RULES · JENKINS</text><text class="flane" x="990" y="300">AI</text><text class="flane" x="510" y="32">LEARNING</text>`;
  return `<svg class="fsvg" viewBox="0 0 1280 680" role="img" aria-labelledby="flow-title">
    <title id="flow-title">How a run goes, from what starts it to the report and what is learned</title>
    <defs><marker id="farrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z"></path></marker>
      <filter id="fglow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="4" result="b"></feGaussianBlur><feMerge><feMergeNode in="b"></feMergeNode><feMergeNode in="SourceGraphic"></feMergeNode></feMerge></filter></defs>
    ${lanes}<g class="fedges">${edges}</g><g class="fnodes">${nodes}</g><circle class="ftoken" r="7" cx="-20" cy="-20" filter="url(#fglow)"></circle></svg>`;
}
const F = {story: "monitor", outcome: "working", steps: [], i: -1, playing: false, speed: 1, timer: 0, raf: 0, real: null};
function flowSteps() {
  if (F.real) return F.real.steps;
  const s = FLOW_STORIES[F.story];
  return s.outcomes[F.outcome][1];
}
function flowDoc(id, line = "") {
  const [, , t, sub, kind] = FLOW_NODES[id], [what, where] = FLOW_DOCS[id];
  return `<div class="fdoc k-${kind}"><span class="fkind">${esc(FLOW_KIND[kind])}</span><h3>${esc(t)}</h3>${line ? `<p class="fnow">${esc(line)}</p>` : ""}<p>${esc(what)}</p><p class="fwhere">${esc(where)}</p></div>`;
}
function flowDraw() {
  const steps = flowSteps(), i = F.i, seen = new Set(), went = new Set();
  steps.slice(0, i + 1).forEach(([id], k) => { seen.add(id); if (k) went.add(flowEdgeKey(steps[k - 1][0], id)); });
  $$(".fnode").forEach(n => { n.classList.toggle("lit", seen.has(n.dataset.node)); n.classList.toggle("now", i >= 0 && steps[i][0] === n.dataset.node); n.classList.toggle("dim", i >= 0 && !seen.has(n.dataset.node)); });
  $$(".fedge").forEach(e => { e.classList.toggle("lit", went.has(e.dataset.edge)); e.classList.toggle("dim", i >= 0 && !went.has(e.dataset.edge)); });
  $$(".fsvg .aimon-tag").forEach(t => t.classList.toggle("on", F.story === "monitor" && !F.real || !!(F.real && F.real.aiMonitor)));
  $("#f-steps").innerHTML = steps.map(([id, line], k) => `<li class="${k < i ? "done" : k === i ? "now" : ""}"><button type="button" data-fstep="${k}"><b>${esc(FLOW_NODES[id][2])}</b><span>${esc(line)}</span></button></li>`).join("");
  $$("#f-steps [data-fstep]").forEach(b => b.onclick = () => { flowStop(); flowGo(+b.dataset.fstep, false); });
  $("#f-doc").innerHTML = i >= 0 ? flowDoc(steps[i][0], steps[i][1]) : `<div class="fdoc"><h3>${esc(F.real ? F.real.title : FLOW_STORIES[F.story].lead)}</h3><p>Press Play to follow it box by box, or step through it. Select any box to read what it does.</p></div>`;
  const stages = [];
  steps.slice(0, Math.max(i + 1, 0)).forEach(([id]) => { const s = FLOW_STAGE[id]; if (s && stages[stages.length - 1] !== s) stages.push(s); });
  const all = [];
  steps.forEach(([id]) => { const s = FLOW_STAGE[id]; if (s && all[all.length - 1] !== s) all.push(s); });
  $("#f-jenk").innerHTML = all.map((s, k) => `<li class="${k < stages.length ? "lit" : ""} ${s.startsWith("AI") ? "byai" : ""} ${s === "Trigger" && (F.story === "monitor" && !F.real || F.real && F.real.aiMonitor) ? "aimon" : ""}">${esc(s === "Trigger" && (F.story === "monitor" && !F.real || F.real && F.real.aiMonitor) ? "Trigger: AI monitor" : s)}</li>`).join("");
  $("#f-play").textContent = F.playing ? "Pause" : i >= steps.length - 1 && i >= 0 ? "Play again" : "Play";
  $("#f-prev").disabled = i <= 0; $("#f-next").disabled = i >= steps.length - 1;
  $("#flow-live").textContent = i >= 0 ? `${FLOW_NODES[steps[i][0]][2]}: ${steps[i][1]}` : "";
}
function flowToken(path, done) {
  const tok = $(".ftoken");
  if (!path || reduced || !tok) { done(); return; }
  const len = path.getTotalLength(), dur = Math.max(350, Math.min(1100, len * 2.2)) / F.speed, t0 = performance.now();
  tok.classList.toggle("ai", path.dataset.edge.endsWith(">check") || path.dataset.edge.endsWith(">agent"));
  const tick = now => {
    const k = Math.min(1, (now - t0) / dur), e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2, p = path.getPointAtLength(len * e);
    tok.setAttribute("cx", p.x); tok.setAttribute("cy", p.y);
    if (k < 1) F.raf = requestAnimationFrame(tick); else done();
  };
  cancelAnimationFrame(F.raf);
  F.raf = requestAnimationFrame(tick);
}
function flowGo(k, animate) {
  const steps = flowSteps();
  if (k < 0 || k >= steps.length) return;
  const from = F.i >= 0 && k === F.i + 1 ? steps[F.i][0] : null, to = steps[k][0];
  const path = from ? document.querySelector(`.fedge[data-edge="${flowEdgeKey(from, to)}"]`) : null;
  const land = () => {
    F.i = k;
    const [x, y] = FLOW_NODES[to], tok = $(".ftoken");
    if (tok) { tok.setAttribute("cx", x); tok.setAttribute("cy", y - NH / 2 - 2); }
    flowDraw();
    if (F.playing) F.timer = setTimeout(() => k + 1 < steps.length ? flowGo(k + 1, true) : (F.playing = false, flowDraw()), 2300 / F.speed);
  };
  if (animate && path) flowToken(path, land); else land();
}
function flowStop() { F.playing = false; clearTimeout(F.timer); cancelAnimationFrame(F.raf); }
function flowPlay() {
  if (F.playing) { flowStop(); flowDraw(); return; }
  const steps = flowSteps();
  F.playing = true;
  if (F.i >= steps.length - 1) { F.i = -1; }
  flowGo(F.i + 1, true);
  flowDraw();
}
function flowPick(story, outcome) {
  flowStop();
  F.real = null; F.story = story; F.outcome = outcome || Object.keys(FLOW_STORIES[story].outcomes)[0]; F.i = -1;
  $$("#f-stories [data-story]").forEach(b => b.setAttribute("aria-pressed", b.dataset.story === story));
  $("#f-outcomes").innerHTML = Object.entries(FLOW_STORIES[story].outcomes).map(([k, [label]]) => `<button type="button" data-outcome="${k}" aria-pressed="${k === F.outcome}">${esc(label)}</button>`).join("");
  $$("#f-outcomes [data-outcome]").forEach(b => b.onclick = () => flowPick(F.story, b.dataset.outcome));
  $$("#f-real [data-real]").forEach(b => b.setAttribute("aria-pressed", "false"));
  flowDraw();
}
/* a real run, mapped onto the same boxes from the stages it recorded */
const FLOW_FROM = {ticket: ["ticket", "desk"], trap: ["trap", "confirm"], health: ["sweep", "checks"], monitor: ["monitor", "holds"]};
async function flowReal(id) {
  flowStop();
  const rec = await api("api/incidents/" + encodeURIComponent(id));
  const src = FLOW_FROM[rec.event.source] || FLOW_FROM.trap, steps = [], st = rec.stages || [];
  const say = p => (p && p.text ? p.text.split("\n").filter(Boolean).slice(0, 2).join(" ") : "");
  steps.push([src[0], say(st[0]) || rec.event.title || ""], [src[1], rec.event.title || ""]);
  steps.push(["pick", st.filter(p => p.key.startsWith("rule:")).map(p => p.label.replace(/^Rule: /, "")).join(", ") || "No rule covers it."]);
  const first = st.find(p => p.key === "first_look");
  if (first) steps.push(["first", say(first)]);
  const ruleParts = st.filter(p => p.key.startsWith("rule:") && p.key !== "rule:none");
  if (ruleParts.length) steps.push(["rules", ruleParts.map(p => `${p.label.replace(/^Rule: /, "")}: ${(p.text.split("\n").pop() || "").replace(/^Ended: /, "")}`).join(" · ")]);
  const fix = st.find(p => p.key === "repair");
  if (fix) steps.push(["repair", say(fix)]);
  steps.push(["context", rec.similar && rec.similar.length ? `${plural(rec.similar.length, "similar run")} found.` : "Past runs, memory and sources gathered."]);
  const chk = st.find(p => p.key === "check"), ag = st.find(p => p.key === "agent");
  steps.push(["needai", rec.agent ? (rec.agent.why || "The AI was needed.") : chk ? "The AI checked after rules it wrote." : "The rules dealt with it, so the AI was not needed."]);
  if (chk) steps.push(["check", say(chk)]);
  if (ag) steps.push(["agent", say(ag)]);
  steps.push(["report", say(st.find(p => p.key === "report")) || "Concluded."]);
  if (rec.event.source === "ticket") steps.push(["desk", "The desk takes the answer back to the person."]);
  F.real = {steps, title: `${rec.event.title || rec.id}, ${dfmt(rec.event.time)} ${hm(rec.event.time)}`, aiMonitor: !!rec.ai_monitor};
  F.i = -1;
  $$("#f-stories [data-story]").forEach(b => b.setAttribute("aria-pressed", "false"));
  $("#f-outcomes").innerHTML = `<span class="muted">A real run: <a href="#/run/${esc(rec.id)}">open it</a>${rec.jenkins_run && rec.jenkins_run.url ? ` · <a href="${esc(rec.jenkins_run.url)}" target="_blank" rel="noreferrer">its Jenkins build &#8599;</a>` : ""}</span>`;
  $$("#f-real [data-real]").forEach(b => b.setAttribute("aria-pressed", b.dataset.real === id));
  flowPlay();
}
const FLOW_SECTIONS = [
  ["What can start a run", `<p>Four things, and nothing else: a <b>trap</b> from a device, a <b>ticket</b> on the service desk, the <b>heartbeat</b> sweep every 20 minutes, and an accepted <b>monitor</b>. A trap waits 2 minutes before it is looked at, so a flap that settles counts once, and alarms about the same thing that arrive together become one run. A monitor fires once per episode, counts only what happened after it was accepted, and never fires while a test job or our own change is changing the network.</p>`],
  ["Rules, and where they run", `<p>Every rule is a pipeline in Jenkins, in <b>troubleshooting-pipelines</b>, written from the rule and rewritten whenever it changes. Each step is a stage: the troubleshooter runs the check with its lab access and its scrubbing, and the build decides where the rule goes. If Jenkins cannot take a build within a minute, the rule runs in the troubleshooter instead and the run says so.</p><p>Rules placed at the beginning run first, then the main rule, then those at the end. A <b>watch-only</b> rule runs and is recorded but decides nothing: its finding is shown with it, never used as the run's answer.</p>`],
  ["Known-good, and the first look", `<p>The golden configs are known-good. A difference with no recorded reason is put back. A deliberate earlier change, like an OSPF stop-gap kept for failing hardware, is never undone blindly.</p><p>Before rules the AI wrote, the run takes a <b>first look</b>: has anything moved from known-good? Whatever has, and nothing explains, is put back first as its own repair build, so the AI's rules always start from the network as it is meant to be.</p>`],
  ["Repairs", `<p>A cause with a repair rule gets its own Jenkins build: <i>Safe to change?</i>, the change, <i>Did it work?</i>, then keep it or undo it. It holds the network change lock from the change to the end, and if the build stops part-way, the troubleshooter undoes the change and lets go of the lock itself. A repair is held back while a deployment or test job is running; tampering or failing hardware is still emailed, once a day.</p>`],
  ["When the AI comes in", `<p>The <b>support agent</b> takes over when a ticket has no rule, when the rules ran out or asked for someone, when they found a cause with no repair, or when the repair failed. It stays out of what only a person can fix (a dead link, a silent ISP) and out of anything a job is changing.</p><p>After rules the AI wrote, the <b>AI check</b> always comes first: a model that cannot change anything decides whether it is still broken, in at most 8 reads and 4 minutes. Working ends the run. Still broken, or unsure, starts the support agent, within its limits: 8 sessions an hour, and the same problem on the same device only once in half an hour.</p><p>Every change the agent makes is a transaction with its own check: kept only if the check passes, undone otherwise. Each AI job's model is chosen on the AI models page.</p>`],
  ["Reports and email", `<p>If anything needs saying, a model reads the whole run and it is emailed: FIXED, FIXED BY AI, NEEDS YOU. A run that cleared by itself, or that the AI check found working, is not emailed unless the same thing keeps coming back. Tickets go back to the desk, and only the person's answer to “can you see it working?” settles them.</p>`],
  ["How it learns", `<p>The support agent may suggest a rule that would have handled this without it, and after every run with a cause the AI looks for a monitor that would have caught it sooner. Nothing goes in until a person accepts it on the Review page, seeing what the rule would have done on past runs and how often the monitor would have fired. A rule gets its Jenkins pipeline straight away; a monitor watches from that moment. The next time, the rules handle it and the AI only checks.</p>`],
  ["Where to see it", `<p><b>Now</b> and <b>History</b> list every run. A run started by a monitor the AI suggested is highlighted in violet and marked <span class="chip aimon">AI monitor</span>; runs where the AI's rules ran say <span class="chip airule">AI rules</span> and what the AI check found. Each run's page shows its stages in order, the same stages as its <b>troubleshooting-run</b> build in Jenkins, where the build's name starts with [AI monitor] for those runs. The rules' and repairs' own builds are linked from each stage.</p>`],
];
async function pageFlow() {
  setNav("flow");
  naming("How it works");
  $("#main").innerHTML = `<div class="wrap flow">
    <div class="head"><h1>How it works</h1><p>Everything that happens between an alarm and the report, as it runs today. Pick what starts the run and press Play, or replay a real run.</p></div>
    <div class="fbar">
      <div class="seg" id="f-stories" role="group" aria-label="What starts the run">${Object.entries(FLOW_STORIES).map(([k, s]) => `<button type="button" data-story="${k}" aria-pressed="false">${esc(s.name)}</button>`).join("")}</div>
      <div class="seg fout" id="f-outcomes" role="group" aria-label="How it ends"></div>
      <span class="fctl"><button class="btn go" id="f-play" type="button">Play</button><button class="btn" id="f-prev" type="button">Back</button><button class="btn" id="f-next" type="button">Forward</button>
        <span class="speed" role="group" aria-label="Speed">${[1, 2].map(n => `<button type="button" data-fspeed="${n}" aria-pressed="${F.speed === n}">${n}×</button>`).join("")}</span></span>
    </div>
    <div class="fgrid">
      <div class="fchart panel">${flowSvg()}
        <div class="flegend">${Object.entries(FLOW_KIND).map(([k, t]) => `<span><i class="sw k-${k}"></i>${esc(t)}</span>`).join("")}<span><i class="sw aimon"></i>Started by an AI monitor</span></div>
        <div class="fjenk"><b>In Jenkins</b> <span class="muted">troubleshooting-run, one build per run, its stages as they happen</span><ol id="f-jenk"></ol></div>
      </div>
      <aside class="fside"><div id="f-doc"></div><ol class="fsteps" id="f-steps"></ol></aside>
    </div>
    <section class="panel freal"><h2>Replay a real run</h2><p class="muted">The latest runs, drawn on the same boxes from the stages they recorded.</p><div class="seg" id="f-real"><span class="muted">Loading</span></div></section>
    <div class="fdocs">${FLOW_SECTIONS.map(([t, body], k) => `<details class="panel" ${k < 2 ? "open" : ""}><summary><h2>${esc(t)}</h2></summary>${body}</details>`).join("")}</div>
    <div class="sr" aria-live="polite" id="flow-live"></div>
  </div>`;
  $$("#f-stories [data-story]").forEach(b => b.onclick = () => flowPick(b.dataset.story));
  $("#f-play").onclick = flowPlay;
  $("#f-prev").onclick = () => { flowStop(); flowGo(F.i - 1, false); };
  $("#f-next").onclick = () => { flowStop(); flowGo(F.i + 1, true); };
  $$("[data-fspeed]").forEach(b => b.onclick = () => { F.speed = +b.dataset.fspeed; $$("[data-fspeed]").forEach(x => x.setAttribute("aria-pressed", x === b)); });
  $$(".fnode").forEach(n => {
    const show = () => { if (F.playing) return; $$(".fnode").forEach(x => x.classList.toggle("picked", x === n)); $("#f-doc").innerHTML = flowDoc(n.dataset.node); };
    n.onclick = show; n.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(); } };
  });
  flowPick(F.story, F.outcome);
  try {
    const runs = (await api("api/incidents")).slice(0, 8);
    $("#f-real").innerHTML = runs.length ? runs.map(r => `<button type="button" data-real="${esc(r.id)}" aria-pressed="false" class="${r.ai_monitor ? "aimon" : ""}" title="${esc(r.headline)}"><span class="led ${stateOf(r.state).c}"></span>${esc(r.title)} <small>${esc(ago(r.time))}</small></button>`).join("") : `<span class="muted">No runs yet.</span>`;
    $$("#f-real [data-real]").forEach(b => b.onclick = () => flowReal(b.dataset.real).catch(e => toast(e.message, true)));
  } catch (e) { $("#f-real").innerHTML = `<span class="muted">The runs could not be loaded: ${esc(e.message)}</span>`; }
}

/* ---------- routing ---------- */
function parseHash() {
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  return {path, q: new URLSearchParams(qs || "")};
}
async function route() {
  stopReplay();
  flowStop();
  closeDrawer(true);
  clearInterval(S.poll);
  const {path, q} = parseHash();
  if (!/^(run|incident)\//.test(path)) S.backTo = location.hash || "#/";
  const wasFailures = S.page === "failures" && $("#f-table");
  try {
    if (path === "" || path === "now") return await pageNow();
    if (path === "failures") { if (wasFailures) { S.q = q; syncFilters(); return loadRuns(q); } return pageFailures(q); }
    const m = path.match(/^(?:run|incident)\/(.+)$/);
    if (m) return await pageRun(decodeURIComponent(m[1]));
    if (path === "flow") return await pageFlow();
    if (path === "review") return await pageReview(q);
    if (path === "rules") return await pageRules();
    if (path === "tags") return await pageTags();
    if (path === "monitors") return await pageMonitors();
    if (path === "monitors/dashboard") return await pageMonitorDashboard();
    if (path === "models") return await pageModels();
    if (path === "ai-data") return await pageData(q);
    if (path === "collect") return await pageCollect();
    if (path === "train") return await pageTrain(q);
    const job = path.match(/^train\/(ft-[0-9a-z-]+)$/);
    if (job) return await pageTrainJob(job[1]);
    const set = path.match(/^ai-data\/set\/([a-z0-9-]+)$/);
    if (set) return await pageSet(set[1], q);
    const ep = path.match(/^ai-data\/([0-9a-f-]{36})$/);
    if (ep) return await pageEpisode(ep[1]);
    setNav("");
    $("#main").innerHTML = `<div class="wrap"><div class="empty">There is no page here. <a href="#/">Go to the front page</a>.</div></div>`;
  } catch (e) { $("#main").innerHTML = `<div class="wrap">${failed(e)}</div>`; }
  finally { if (!wasFailures) window.scrollTo(0, 0); }
}
window.addEventListener("hashchange", route);

/* ---------- collect ---------- */
const meter = (a, b) => `<span class="meter" aria-hidden="true"><i style="width:${b ? Math.min(100, Math.round(100 * a / b)) : 0}%"></i></span>`;
const pct = (a, b) => b ? `${Math.round(100 * a / b)}%` : "none yet";
const STATE_WORDS = {running: "Running", done: "Done", stopped: "Stopped", queued: "Waiting to start", stopping: "Stopping", failed: "Failed", canceled: "Canceled"};
const stateChip = s => `<span class="chip st-${esc(s)}">${esc(STATE_WORDS[s] || s)}</span>`;
// The service desk: its own port on the LAN, /desk/ on the public address (nmas.shiplabs.net, no port).
const deskLink = n => `${location.port ? `http://${location.hostname}:8771/` : "/desk/"}#/ticket/${encodeURIComponent(n)}`;
function agoWords(t) { const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`; }
function collectedPanel(d) {
  const t = d.totals, max = Math.max(1, ...d.triggers.map(x => x.episodes));
  return `<section class="panel"><h2>Collected so far <small>${fmtN(t.episodes)} AI runs, ${fmtN(t.steps)} commands</small></h2>
    <div class="tblwrap"><table class="tbl ctbl"><thead><tr><th>Started by</th><th>Runs</th><th class="num">Solved</th><th class="num">Held out for testing</th></tr></thead>
    <tbody>${d.triggers.map(x => `<tr><td>${esc(x.label)}</td><td class="c-bar">${meter(x.episodes, max)}<span>${fmtN(x.episodes)}</span></td><td class="num">${pct(x.solved, x.episodes)}</td><td class="num">${fmtN(x.held_out)}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">Nothing yet. Runs appear here as traps, tickets and the heartbeat start them.</td></tr>`}</tbody></table></div></section>`;
}
function campaignCard(c, target) {
  const left = c.planned - c.finished, now = c.plan[0];
  const next = c.dataset ? `<a class="btn small" href="#/train?dataset=${encodeURIComponent(c.dataset.folder)}">Fine-tune it</a>` : c.finished ? `<button class="btn small" type="button" data-build="${esc(c.name)}">Build the dataset</button>` : "";
  return `<section class="panel ccard"><header><h2>${esc(c.name)}</h2>${stateChip(c.state)}<span class="muted">${esc(agoWords(c.created))}, ${c.rounds === 1 ? "one round" : `${c.rounds} rounds`}</span>${c.state === "running" ? `<button class="btn small" type="button" data-stop="${esc(c.id)}">Stop</button>` : ""}</header>
    <ol class="csteps">${c.stages.map(s => `<li class="${esc(s.state)}"><i></i>${esc(s.label)}</li>`).join("")}</ol>
    <div class="tblwrap"><table class="tbl ctbl"><thead><tr><th>Situation</th><th>Runs</th><th class="num">Solved (target ${Math.round(target * 100)}%)</th><th class="num">Held out</th></tr></thead>
    <tbody>${c.lines.map(l => `<tr><td>${esc(l.title)}${l.staged ? ` <span class="chip st-running">Now</span>` : ""}</td><td class="c-bar">${meter(l.done, l.planned)}<span>${fmtN(l.done)} / ${fmtN(l.planned)}</span></td><td class="num ${l.done && l.solved / Math.max(1, l.episodes) < target ? "under" : ""}">${pct(l.solved, l.episodes)}</td><td class="num">${fmtN(l.held_out)}</td></tr>`).join("")}</tbody></table></div>
    <p class="cline">${fmtN(c.finished)} of ${fmtN(c.planned)} runs finished${now ? `, working on <a href="${deskLink(now.ticket)}">ticket T-${esc(now.ticket)}</a> now` : left && c.state === "running" ? ", the next one starts when the network and the AI are free" : ""}.</p>
    ${c.dataset ? `<p class="cline">Dataset <a href="#/ai-data/set/${encodeURIComponent(c.dataset.folder)}"><b>${esc(c.dataset.folder)}</b></a>, ${fmtN(c.dataset.train_rows)} training examples, ${fmtN(c.dataset.test_rows)} held out for the test. ${next}</p>` : next ? `<p class="cline">${next}</p>` : ""}
    ${c.job ? `<p class="cline">Fine-tune: <a href="#/train/${esc(c.job.id)}">${esc(c.job.name)}</a> ${stateChip(c.job.state)}</p>` : ""}</section>`;
}
async function pageCollect() {
  setNav("collect"); naming("Collect");
  $("#main").innerHTML = `<div class="wrap"><div class="head"><h1>Collect</h1><p>Each situation really breaks the network, files the ticket that person would write, and keeps the run that follows for training.</p><span class="acts"><button class="btn go" id="c-open" type="button">New campaign</button></span></div>
    <form class="panel cform" id="c-form" hidden></form><div id="c-list" class="cstack"><p class="muted">Loading</p></div></div>`;
  let first = true;
  const draw = async () => {
    const d = await api("api/workbench");
    if (d.down) { $("#c-list").innerHTML = dataDown(d); return; }
    if (first) {
      first = false;
      $("#c-form").innerHTML = `<h2>New campaign</h2><label class="fld"><span>Name</span><input id="c-name" required placeholder="for example internet-week1" autocomplete="off"></label>
        <fieldset class="fld"><legend>Situations</legend>${d.situations.map(s => `<label class="ck"><input type="checkbox" value="${esc(s.id)}" checked><span><b>${esc(s.title)}</b><small class="muted">${esc(s.what)}</small></span></label>`).join("")}</fieldset>
        <label class="fld short"><span>Rounds</span><input id="c-rounds" type="number" min="1" max="${d.max_rounds}" value="1"></label>
        <p class="muted">One at a time. The network goes back to normal after each ticket is settled. The same situation waits half an hour before it comes round again, so a large campaign takes hours.</p>
        <div class="acts"><button class="btn go" type="submit">Start collecting</button><button class="btn" id="c-cancel" type="button">Cancel</button></div>`;
      $("#c-cancel").onclick = () => { $("#c-form").hidden = true; };
      $("#c-form").onsubmit = async e => {
        e.preventDefault();
        try { await post("api/workbench/campaigns", {name: $("#c-name").value, rounds: Number($("#c-rounds").value), scenarios: $$('#c-form input[type=checkbox]:checked').map(x => x.value)}); $("#c-form").hidden = true; toast("Campaign started."); await draw(); }
        catch (x) { toast(x.message, true); }
      };
    }
    $("#c-list").innerHTML = `${d.why_not ? `<p class="dwarn">New situations are waiting: ${esc(d.why_not)}.</p>` : ""}${collectedPanel(d)}${d.campaigns.map(c => campaignCard(c, d.target)).join("") || `<div class="panel empty">No campaigns yet. Start one to give the AI practice on every kind of fault you have a situation for.</div>`}`;
    $$("[data-stop]").forEach(b => b.onclick = async () => { await post(`api/workbench/campaigns/${b.dataset.stop}/stop`, {}); toast("Campaign stopped."); draw(); });
    $$("[data-build]").forEach(b => b.onclick = async () => {
      b.disabled = true; b.textContent = "Building";
      try { const s = await post("api/workbench/datasets", {name: b.dataset.build, include: "graded"}); toast(`Dataset ${s.folder} is ready.`); await draw(); }
      catch (x) { toast(x.message, true); b.disabled = false; b.textContent = "Build the dataset"; }
    });
  };
  $("#c-open").onclick = () => { $("#c-form").hidden = false; $("#c-name").focus(); };
  await draw();
  S.poll = setInterval(() => { if (!document.hidden) draw().catch(() => {}); }, 6000);
}

/* ---------- data: what training reads ---------- */
async function drawDatasets() {
  const box = $("#d-sets");
  if (!box) return;
  const d = await api("api/workbench");
  if (d.down) return;
  const t = d.totals, read = [["Commands that come with a stated reason", d.share.reasons], ["Model calls with the model's own thinking", d.share.thought], ["AI runs that solved the problem", t.episodes ? t.solved / t.episodes : 0], ["Runs held out of training, for the test", t.episodes ? t.held_out / t.episodes : 0]];
  box.innerHTML = `<section class="panel"><h2>What training reads</h2><div class="reads">${read.map(([l, v]) => `<div><span>${esc(l)}</span>${meter(v, 1)}<b>${Math.round(v * 100)}%</b></div>`).join("")}</div></section>
    <section class="panel"><h2>Gathered for each agent <small>every conversation with a model, so each one can have its own fine-tune</small></h2>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>Agent</th><th class="num">Conversations</th><th class="num">Model calls</th><th class="num">With its thinking</th></tr></thead><tbody>${(d.agents || []).map(a => `<tr><td>${esc(a.name)}</td><td class="num">${fmtN(a.conversations)}</td><td class="num">${fmtN(a.calls)}</td><td class="num">${fmtN(a.thought)}</td></tr>`).join("")}</tbody></table></div></section>
    <section class="panel"><h2>Datasets <small>a fixed copy of the examples; open one to read it</small></h2>
      <form class="cinline" id="d-build"><label class="fld"><span>Agent</span><select id="d-who">${(d.agents || []).map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select></label><label class="fld"><span>Name</span><input id="d-name" required placeholder="for example first-model" autocomplete="off"></label>
        <label class="fld"><span>Use runs</span><select id="d-include"><option value="graded">Solved runs whole, others up to their first mistake</option><option value="solved">Solved runs only</option></select></label><button class="btn" type="submit">Build a dataset</button></form>
      ${d.datasets.length ? `<div class="tblwrap"><table class="tbl"><thead><tr><th>Dataset</th><th>Agent</th><th>Built</th><th class="num">Training examples</th><th class="num">Held out</th><th class="num">Solved runs</th><th></th></tr></thead><tbody>${d.datasets.map(s => `<tr><td><a href="#/ai-data/set/${encodeURIComponent(s.folder)}"><b>${esc(s.folder)}</b></a></td><td>${esc(s.stats.who_name || "Support agent")}</td><td>${esc(agoWords(s.created))}</td><td class="num">${fmtN(s.stats.train_rows)}</td><td class="num">${fmtN(s.stats.test_rows)}</td><td class="num">${fmtN(s.stats.runs_solved)}</td><td class="num"><a href="#/train?dataset=${encodeURIComponent(s.folder)}">Fine-tune it</a></td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">None built yet.</p>`}</section>`;
  $("#d-build").onsubmit = async e => {
    e.preventDefault();
    try { const s = await post("api/workbench/datasets", {name: $("#d-name").value, include: $("#d-include").value, who: $("#d-who").value}); toast(`Dataset ${s.folder} is ready.`); drawDatasets(); }
    catch (x) { toast(x.message, true); }
  };
}
async function pageSet(folder, q) {
  setNav("aidata"); naming(folder);
  const split = q.get("split") === "test" ? "test" : "train", page = Math.max(1, Number(q.get("page")) || 1);
  const d = await api(`api/workbench/datasets/${encodeURIComponent(folder)}?split=${split}&page=${page}`);
  const pages = Math.max(1, Math.ceil(d.total / d.size)), link = (s, p) => `#/ai-data/set/${encodeURIComponent(folder)}?split=${s}&page=${p}`;
  const row = (r, i) => `<details class="panel xrow"><summary><span class="muted">${fmtN((page - 1) * d.size + i + 1)}</span><b>${esc(r.answers[0].name)}</b><span class="muted">${r.meta.decision ? esc(r.meta.decision === "report" ? "Last decision" : "Makes the change") + ", " : ""}step ${esc(r.meta.step)}${r.weight > 1 ? `, counted ${r.weight} times` : ""}</span></summary>
    <h3>What it sees</h3><pre>${esc(r.query)}</pre><h3>What it should do next</h3><pre>${esc(JSON.stringify(r.answers[0], null, 1))}</pre>${r.reasoning ? `<h3>Why</h3><p>${esc(r.reasoning)}</p>` : ""}</details>`;
  $("#main").innerHTML = `<div class="wrap"><a href="#/ai-data">Back to Data</a><div class="head"><h1>${esc(folder)}</h1><p>These are the examples, exactly as training reads them.</p></div>
    <div class="seg"><a href="${link("train", 1)}" ${split === "train" ? 'aria-current="page"' : ""}>For training</a><a href="${link("test", 1)}" ${split === "test" ? 'aria-current="page"' : ""}>Held out for the test</a></div>
    <div class="cstack">${d.rows.map(row).join("") || `<div class="panel empty">No examples here.</div>`}</div>
    <div class="pager"><span class="count">Page ${page} of ${pages}, ${fmtN(d.total)} examples</span>${page > 1 ? `<a class="btn small" href="${link(split, page - 1)}">Previous</a>` : ""}${page < pages ? `<a class="btn small" href="${link(split, page + 1)}">Next</a>` : ""}</div></div>`;
}

/* ---------- train ---------- */
const money = v => `$${Number(v || 0).toFixed(2)}`;
const money3 = v => `$${Number(v || 0) < 0.1 ? Number(v || 0).toFixed(3) : Number(v || 0).toFixed(2)}`;
const minutes = m => m < 90 ? `${Math.round(m)} min` : `${(m / 60).toFixed(1)} h`;
function jobRow(j) {
  const t = j.result && j.result.tuned && j.result.tuned.overall, spark = j.loss_spark || [];
  return `<tr data-job="${esc(j.id)}"><td><b>${esc(j.name)}</b><br><small class="muted">${esc(agoWords(j.created))}${j.gpu_name ? `, ${esc(j.gpu_name)}` : ""}</small></td><td>${stateChip(j.state)}<br><small class="muted">${esc(j.stage)}</small></td><td class="wide">${sparkline(spark)}</td><td class="num">${t ? `${t.tool}%` : "not scored"}</td><td class="num">${money(j.cost_usd)}<br><small class="muted">of ${money(j.limit_usd)}</small></td><td>${j.machine === "deleted" ? "GPU deleted" : j.machine === "check" ? `<b class="under">Check the Vast account</b>` : j.machine === "rented" ? "GPU rented" : ""}</td></tr>`;
}
function sparkline(v) {
  if (v.length < 2) return "";
  const hi = Math.max(...v), lo = Math.min(...v), y = x => 22 - (hi === lo ? 10 : 20 * (x - lo) / (hi - lo));
  return `<svg class="spark" viewBox="0 0 100 24" preserveAspectRatio="none" role="img" aria-label="Training loss, from ${v[0].toFixed(2)} to ${v[v.length - 1].toFixed(2)}"><polyline points="${v.map((x, i) => `${(i * 100 / (v.length - 1)).toFixed(1)},${y(x).toFixed(1)}`).join(" ")}" vector-effect="non-scaling-stroke"></polyline></svg>`;
}
const T = {};
async function pageTrain(q) {
  setNav("train"); naming("Train");
  const d = await api("api/train");
  Object.assign(T, {model: "qwen3-0.6b", method: "lora", dataset: q.get("dataset") || (d.datasets[0] && d.datasets[0].folder) || "", config: {...d.defaults}, offer: null, offers: [], limit: "", started: false});
  const num = (key, label, step, note) => `<label class="fld"><span>${esc(label)}</span><input data-k="${key}" type="number" step="${step}" min="${d.limits[key][0]}" max="${d.limits[key][1]}" value="${d.defaults[key]}">${note ? `<small class="muted">${esc(note)}</small>` : ""}</label>`;
  $("#main").innerHTML = `<div class="wrap"><div class="head"><h1>Train</h1><p>Train a model on what was collected: pick a model, the data and a GPU, and the estimate follows. You set the spending limit when you start. At the limit training stops, the model is saved and copied here, and the GPU is deleted.</p></div>
    <dl class="dtiles">${dataTile("Spent today", money(d.spend.today), "fine-tuning")}${dataTile("All time", money(d.spend.total), "fine-tuning")}${dataTile("Credit left", d.credit == null ? "unknown" : money(d.credit), "on the GPU account")}</dl>
    <div class="tsteps">
      <section class="panel"><h2><i>1</i>Model</h2><div class="picks">${d.models.map(m => `<label class="pick"><input type="radio" name="t-model" value="${esc(m.id)}" ${m.id === T.model ? "checked" : ""}><span><b>${esc(m.title)}</b><small class="muted">${m.params_b} billion parameters, ${esc(m.license)}</small></span></label>`).join("")}</div>
        <fieldset class="fld"><legend>How to train it</legend><div class="picks" id="t-methods"></div></fieldset></section>
      <section class="panel"><h2><i>2</i>Data</h2>
        ${d.datasets.length ? `<label class="fld"><span>Dataset</span><select id="t-ds">${d.datasets.map(s => `<option value="${esc(s.folder)}" ${s.folder === T.dataset ? "selected" : ""}>${esc(s.folder)}, ${esc(s.stats.who_name || "Support agent")}, ${fmtN(s.stats.train_rows)} examples</option>`).join("")}</select></label>` : `<p class="dwarn">There is no dataset yet. <a href="#/ai-data">Build one on the Data page</a>.</p>`}
        <div class="fgrid">${num("epochs", "Passes over the data", "0.25", "")}${num("max_examples", "Training examples", "100", "0 uses them all")}${num("eval_examples", "Test decisions scored", "10", "")}</div>
        <label class="ck"><input type="checkbox" id="t-base" ${d.defaults.eval_base ? "checked" : ""}><span>Also score the untrained model, for before and after</span></label>
        <details class="adv"><summary>Advanced</summary><div class="fgrid">${num("batch_size", "Batch size", "1", "")}${num("grad_accum", "Gradient accumulation", "1", "")}${num("max_len", "Longest example, in tokens", "512", "Longer ones are skipped")}${num("lora_rank", "Adapter size", "4", "")}${num("lr", "Learning rate", "0.00001", "")}</div></details>
        <p class="muted">A small model cannot read the whole history a real run is given. Training examples keep the incident, what the rules found and the calls so far.</p></section>
      <section class="panel"><h2><i>3</i>GPU</h2><div id="t-offers"><p class="muted">Pick a dataset to see what is available.</p></div></section>
      <section class="panel tstart"><h2>Start</h2><div id="t-est"></div>
        <label class="fld short"><span>Spending limit, dollars</span><input id="t-limit" type="number" step="0.5" min="0.5" max="${d.max_limit}"></label>
        <button class="btn go" id="t-go" type="button" disabled>Start training</button><p class="muted">The GPU is deleted when the job ends, whatever the reason.</p></section>
    </div>
    <section class="panel"><h2>Runs</h2><div class="tblwrap"><table class="tbl dtbl"><thead><tr><th>Run</th><th>State</th><th class="wide">Loss</th><th class="num">Right next command</th><th class="num">Cost</th><th>Machine</th></tr></thead><tbody>${d.jobs.map(jobRow).join("") || `<tr><td colspan="6" class="empty">No runs yet.</td></tr>`}</tbody></table></div></section></div>`;
  $$("tr[data-job]").forEach(tr => tr.onclick = () => { location.hash = "#/train/" + tr.dataset.job; });
  const methods = () => {
    const m = d.models.find(x => x.id === T.model);
    if (!m.methods.includes(T.method)) T.method = "lora";
    $("#t-methods").innerHTML = m.methods.map(k => `<label class="pick"><input type="radio" name="t-method" value="${k}" ${k === T.method ? "checked" : ""}><span><b>${esc(d.methods[k].title)}</b><small class="muted">${esc(d.methods[k].what)}</small></span></label>`).join("");
    $$('input[name=t-method]').forEach(r => r.onchange = () => { T.method = r.value; reprice(); });
  };
  let wait = null, ask = 0;
  const estimate = () => {
    const o = T.offer, go = $("#t-go");
    go.disabled = !o || !Number($("#t-limit").value);
    $("#t-est").innerHTML = o ? `<p class="big">About ${esc(minutes(o.estimate.minutes.total))} and ${esc(money3(o.estimate.usd))}</p><p class="muted">Setup ${esc(minutes(o.estimate.minutes.setup))}, training ${esc(minutes(o.estimate.minutes.train))}, scoring ${esc(minutes(o.estimate.minutes.scoring))}, on ${esc(o.gpu_name)} at ${esc(money3(o.price_per_hour))} an hour.</p>` : `<p class="muted">Pick a GPU for an estimate.</p>`;
  };
  const drawOffers = r => {
    const box = $("#t-offers");
    if (!r.offers.length) { box.innerHTML = `<p class="dwarn">No GPU with ${esc(r.need_vram_gb)} GB of memory is available right now. Try a smaller batch or model, or look again in a few minutes.</p>`; T.offer = null; estimate(); return; }
    T.offers = r.offers;
    T.offer = T.offers.find(o => T.offer && o.id === T.offer.id) || T.offers[0];
    if (!$("#t-limit").value && r.suggested_limit) $("#t-limit").value = r.suggested_limit;
    box.innerHTML = `<p class="muted">This job needs about ${esc(r.need_vram_gb)} GB of GPU memory. Cheapest for this job first.</p><div class="tblwrap"><table class="tbl"><thead><tr><th></th><th>GPU</th><th class="num">Memory</th><th class="num">Per hour</th><th class="num">Time</th><th class="num">Cost</th><th class="wide">Where</th></tr></thead><tbody>${T.offers.map(o => `<tr><td><input type="radio" name="t-offer" value="${o.id}" aria-label="${esc(o.gpu_name)}" ${o.id === T.offer.id ? "checked" : ""}></td><td><b>${esc(o.gpu_name)}</b></td><td class="num">${o.vram_gb} GB</td><td class="num">${esc(money3(o.price_per_hour))}</td><td class="num">${esc(minutes(o.estimate.minutes.total))}</td><td class="num">${esc(money3(o.estimate.usd))}</td><td class="wide">${esc(o.where)}</td></tr>`).join("")}</tbody></table></div>`;
    $$('input[name=t-offer]').forEach(x => x.onchange = () => { T.offer = T.offers.find(o => String(o.id) === x.value); estimate(); });
    estimate();
  };
  const reprice = () => {
    clearTimeout(wait);
    if (!T.dataset) return;
    wait = setTimeout(async () => {
      const mine = ++ask;
      $("#t-offers").setAttribute("aria-busy", "true");
      try { const r = await post("api/train/offers", {base_model: T.model, method: T.method, dataset: T.dataset, config: T.config}); if (mine === ask) drawOffers(r); }
      catch (x) { if (mine === ask) { $("#t-offers").innerHTML = `<p class="dwarn">${esc(x.message)}</p>`; T.offer = null; estimate(); } }
      finally { $("#t-offers").removeAttribute("aria-busy"); }
    }, 350);
  };
  $$('input[name=t-model]').forEach(r => r.onchange = () => { T.model = r.value; methods(); reprice(); });
  $$("input[data-k]").forEach(i => i.onchange = () => { T.config[i.dataset.k] = Number(i.value); reprice(); });
  $("#t-base").onchange = e => { T.config.eval_base = e.target.checked; reprice(); };
  if ($("#t-ds")) $("#t-ds").onchange = e => { T.dataset = e.target.value; reprice(); };
  $("#t-limit").oninput = estimate;
  $("#t-go").onclick = async e => {
    const limit = Number($("#t-limit").value), btn = e.target;
    if (btn.dataset.armed !== "1") { btn.dataset.armed = "1"; btn.textContent = `Rent the GPU, spend up to ${money(limit)}`; return; }
    btn.disabled = true; btn.textContent = "Starting";
    try { const j = await post("api/train/jobs", {base_model: T.model, method: T.method, dataset: T.dataset, config: T.config, offer: T.offer, limit_usd: limit}); location.hash = "#/train/" + j.id; }
    catch (x) { toast(x.message, true); btn.disabled = false; btn.dataset.armed = ""; btn.textContent = "Start training"; }
  };
  methods(); estimate(); reprice();
}
function lossChart(rows) {
  if (rows.length < 2) return `<p class="muted">The loss appears here once training starts.</p>`;
  const W = 640, H = 170, L = 40, R = 8, T0 = 8, B = 22, xs = rows.map(r => r.step), ys = rows.map(r => r.loss), x0 = Math.min(...xs), x1 = Math.max(...xs), hi = Math.max(...ys), lo = Math.min(...ys, 0);
  const X = v => L + (W - L - R) * (v - x0) / Math.max(1, x1 - x0), Y = v => T0 + (H - T0 - B) * (1 - (v - lo) / Math.max(1e-9, hi - lo));
  const ticks = [lo, (lo + hi) / 2, hi];
  return `<div class="lchart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Training loss by step, from ${ys[0].toFixed(2)} to ${ys[ys.length - 1].toFixed(2)}">
    ${ticks.map(t => `<line class="g" x1="${L}" x2="${W - R}" y1="${Y(t).toFixed(1)}" y2="${Y(t).toFixed(1)}"></line><text x="${L - 6}" y="${(Y(t) + 4).toFixed(1)}" text-anchor="end">${t.toFixed(2)}</text>`).join("")}
    <text x="${L}" y="${H - 5}">step ${x0}</text><text x="${W - R}" y="${H - 5}" text-anchor="end">step ${x1}</text>
    <polyline class="l" points="${rows.map(r => `${X(r.step).toFixed(1)},${Y(r.loss).toFixed(1)}`).join(" ")}"></polyline><line class="x" y1="${T0}" y2="${H - B}" hidden></line><circle class="d" r="4" hidden></circle></svg><div class="tip" hidden></div></div>
    <details class="adv"><summary>The numbers</summary><div class="tblwrap"><table class="tbl"><thead><tr><th class="num">Step</th><th class="num">Examples</th><th class="num">Loss</th></tr></thead><tbody>${rows.filter((_, i) => i % Math.ceil(rows.length / 20) === 0).map(r => `<tr><td class="num">${fmtN(r.step)}</td><td class="num">${fmtN(r.examples)}</td><td class="num">${r.loss.toFixed(3)}</td></tr>`).join("")}</tbody></table></div></details>`;
}
function hoverLoss(rows) {
  const box = $(".lchart"); if (!box) return;
  const svg = $("svg", box), tip = $(".tip", box), line = $(".x", box), dot = $(".d", box), pts = $(".l", box).getAttribute("points").split(" ").map(p => p.split(",").map(Number));
  svg.onpointermove = e => {
    const r = svg.getBoundingClientRect(), mx = (e.clientX - r.left) * 640 / r.width;
    let best = 0; pts.forEach((p, i) => { if (Math.abs(p[0] - mx) < Math.abs(pts[best][0] - mx)) best = i; });
    const [x, y] = pts[best];
    line.setAttribute("x1", x); line.setAttribute("x2", x); line.hidden = false; dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.hidden = false;
    tip.hidden = false; tip.textContent = `Step ${fmtN(rows[best].step)}, loss ${rows[best].loss.toFixed(3)}`;
    tip.style.left = `${Math.min(r.width - 150, Math.max(0, x * r.width / 640 - 70))}px`;
  };
  svg.onpointerleave = () => { line.hidden = dot.hidden = tip.hidden = true; };
}
function scoreTable(res) {
  const col = [["The untrained model", res.base], ["Repeating the last call", res.copy_last], ["The trained model", res.tuned]].filter(c => c[1]);
  if (!col.length) return `<p class="muted">Scores appear when the job finishes.</p>`;
  const row = (label, get) => `<tr><td>${esc(label)}</td>${col.map(c => { const v = get(c[1]); return `<td class="num">${v == null ? "n/a" : `${v}%`}</td>`; }).join("")}</tr>`;
  return `<div class="tblwrap"><table class="tbl"><thead><tr><th>On ${fmtN(col[0][1].examples)} held-out decisions</th>${col.map(c => `<th class="num">${esc(c[0])}</th>`).join("")}</tr></thead><tbody>
    ${row("Picks the right next command", s => s.overall.tool)}${row("Right command and details", s => s.overall.exact)}${row("Right command, now or later in the run", s => s.overall.exact_or_later)}${row("Ends the run with the right outcome", s => s.report_status)}${row("Answered with a usable command", s => s.overall.no_call == null ? null : Math.round((100 - s.overall.no_call) * 10) / 10)}</tbody></table></div>`;
}
async function pageTrainJob(id) {
  setNav("train");
  let shown = "";
  const draw = async () => {
    const j = await api("api/train/jobs/" + id);
    naming(j.name);
    const active = ["queued", "running", "stopping"].includes(j.state), p = j.progress || {}, key = JSON.stringify([j.state, j.stage, j.cost_usd, j.metrics.length, j.events.length, j.machine]);
    if (key === shown) return active;
    shown = key;
    $("#main").innerHTML = `<div class="wrap"><a href="#/train">Back to Train</a><div class="head"><h1>${esc(j.name)}</h1>${stateChip(j.state)}<span class="acts">${active && !j.stop_requested ? `<button class="btn" id="j-stop" type="button">Stop and save</button>` : ""}</span></div>
      ${j.error ? `<p class="dwarn">${esc(j.error)}</p>` : ""}
      <dl class="dtiles">${dataTile("Now", j.stage, active && p.total ? `${fmtN(p.examples)} of ${fmtN(p.total)} examples` : "")}${dataTile("Spent", money(j.cost_usd), `limit ${money(j.limit_usd)}`)}${dataTile("GPU", j.gpu_name || "not rented yet", j.price_per_hour ? `${money3(j.price_per_hour)} an hour` : "")}${dataTile("Machine", j.machine === "deleted" ? "Deleted" : j.machine === "check" ? "Check the Vast account" : j.machine === "rented" ? "Rented" : "None", j.released_at ? `${agoWords(j.released_at)}` : "")}</dl>
      <div class="cstack"><section class="panel"><h2>Before and after</h2>${scoreTable(j.result || {})}</section>
      <section class="panel"><h2>Training loss</h2>${lossChart(j.metrics)}</section>
      <section class="panel"><h2>What happened</h2><ul class="jlog">${j.events.map(e => `<li class="${esc(e.level)}"><time>${esc(hm(e.time))}</time><span>${esc(e.msg)}</span></li>`).join("")}</ul></section></div></div>`;
    hoverLoss(j.metrics);
    if ($("#j-stop")) $("#j-stop").onclick = async () => { await post(`api/train/jobs/${id}/stop`, {}); toast("Stopping. It saves what it has first."); shown = ""; draw(); };
    return active;
  };
  if (await draw()) S.poll = setInterval(async () => { if (document.hidden) return; if (!(await draw().catch(() => true))) clearInterval(S.poll); }, 5000);
}

/* ---------- now ---------- */
async function pageNow() {
  setNav("now");
  naming("Network Troubleshooting");
  if (!S.now) await refreshNow();
  if (S.now) drawNow(S.now, true); else $("#main").innerHTML = `<div class="wrap">${failed(new Error("no answer from the server"))}</div>`;
}
/* one fault raises the same alarm on several devices: say it once */
const names = list => list.length < 2 ? (list[0] || "") : `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
function together(items) {
  const groups = new Map();
  for (const r of items) {
    const title = r.title || "", leads = r.device && title.startsWith(r.device + " ");
    const key = leads ? [title.slice(r.device.length), String(r.headline || "").split(r.device).join("#"), r.state].join("|") : r.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  return [...groups.values()].map(all => {
    const first = all[0], devices = uniq(all.map(r => r.device)), others = devices.slice(1);
    if (all.length === 1) return {...first, all};
    const repeats = all.length > devices.length ? ` Seen ${all.length} times.` : "";
    return {...first, all, title: others.length ? names(devices) + first.title.slice(first.device.length) : first.title, headline: `${plainCause(first.headline)}${others.length ? ` Same on ${names(others)}.` : ""}${repeats}`};
  });
}
/* every problem appears once: failing now, or waiting for a person */
function attention(d) {
  const inBroken = new Set(d.broken.map(b => b.run).filter(Boolean));
  const live = together([...d.broken.map(b => ({...b, id: b.run || b.key})), ...d.working.filter(r => !inBroken.has(r.id)).map(r => ({...r, run: r.id, doing: r.agent_doing, steps: r.agent_steps}))]);
  return {live, waiting: together(d.needs_you.filter(r => !inBroken.has(r.id)))};
}
function liveRow(g) {
  const working = g.all.some(x => x.state === "working"), doing = (g.all.find(x => x.doing) || {}).doing, steps = Math.max(...g.all.map(x => x.steps || 0));
  const v = doing ? callView({tool: doing.tool, args: doing.args || {}}) : null;
  const note = v ? `Right now: ${esc(v.q)}${v.cmd ? ` <code>${esc(v.cmd)}</code>` : ""}` : working ? "The AI has this and is about to start." : esc(plainCause(g.headline || g.detail || g.group));
  const who = working ? `<span class="ai">AI</span> on it${steps ? `, ${plural(steps, "step")}` : ""}` : !g.run ? "Being confirmed" : g.state === "fixed" ? "Repaired, checking" : esc(stateOf(g.state).t);
  return `<li class="qrow"><a class="qmain" href="${g.run ? "#/run/" + esc(g.run) : "#/failures"}"><span class="led ${working ? "violet pulse" : g.state === "fixed" ? "green" : "red pulse"}"></span>
    <span class="qtext"><b>${esc(g.title)}${sourceTag(g)}</b><small>${note}</small></span><span class="qside">${who}</span></a></li>`;
}
function waitRow(g) {
  const failing = g.all.some(r => r.still === "failing"), clear = g.all.every(r => r.still === "clear");
  const still = failing ? ', <span class="failing">still failing</span>' : clear ? ", not failing now" : "";
  return `<li class="qrow"><a class="qmain" href="#/run/${esc(g.id)}"><span class="led ${failing ? "red" : "amber"}"></span>
    <span class="qtext"><b>${esc(g.title)}${sourceTag(g)}</b><small>${ticks(plainCause(g.headline))}</small><span class="qwhen">${esc(ago(g.time))}${still}</span></span></a>
    <button class="btn small" type="button" data-handle="${esc(g.all.map(r => r.id).join(","))}">Mark handled</button></li>`;
}
function fixedRow(r) {
  return `<li class="qrow"><a class="qmain" href="#/run/${esc(r.id)}"><span class="led green"></span><span class="qtext"><b>${esc(r.title)}</b></span><span class="qside">${r.by === "ai" ? '<span class="ai">AI</span>' : "Rules"}, ${esc(ago(r.time))}</span></a></li>`;
}
function rack(groups) {
  const list = [...groups].sort((a, b) => b.bad - a.bad);
  return list.map(g => {
    const lights = g.bad ? `<span class="leds" aria-hidden="true">${[...Array(g.bad).fill("red"), ...Array(g.total - g.bad).fill("green")].map(c => `<span class="led ${c}"></span>`).join("")}</span>` : "";
    return `<li class="${g.bad ? "bad" : ""}" aria-label="${esc(g.name)}: ${g.total - g.bad} of ${g.total} fine"><span class="name">${esc(g.name)}</span><span class="n">${g.bad ? `${g.bad} of ${g.total} bad` : `${g.total} fine`}</span>${lights}</li>`;
  }).join("");
}
const TILE = {"Detected": {href: "#/failures?window=day"}, "Rules fixed it": {href: "#/failures?state=fixed&by=rules&window=day", c: "green"}, "AI took over": {href: "#/failures?by=ai&window=day", c: "violet"}, "Needs you": {href: "#/failures?state=needs_you&window=day", c: "red"}};
function tallyRow(s) {
  const t = TILE[s.step] || {}, c = s.count ? t.c || "" : "", inner = `<span>${esc(s.step)}</span><b>${s.count}</b>`;
  return `<li class="${c}" title="${esc(s.note)}">${t.href ? `<a href="${t.href}">${inner}</a>` : `<div>${inner}</div>`}</li>`;
}

/* the network as you would draw it: traffic leaves the hosts on the left and reaches the internet on the right */
const MAP_AT = {h1: [64, 46], h3: [64, 100], h2: [64, 160], h4: [64, 214], s1: [236, 73], s2: [236, 187], r1: [408, 73], r2: [408, 187], s3: [580, 73], s4: [580, 187], "dhcp-Server": [580, 20], r3: [752, 73], r4: [752, 187], r5: [934, 130], webserver: [1108, 130]};
const MAP_COLS = [[64, "Hosts"], [236, "Access"], [408, "Distribution"], [580, "Core"], [752, "Border"], [934, "ISP"]];
const MAP_NAME = {"dhcp-Server": "DHCP", webserver: "Web server"};
const ROLE_WORDS = {border: "Border router", core: "Core switch", distribution: "Distribution router", access: "Access switch", expansion: "Expansion device", host: "Host", isp: "The ISP's router, out of scope"};
const uniq = list => [...new Set(list)];
const deviceSays = n => n.bad.length ? `failing: ${uniq(n.bad).join(", ")}` : n.working ? "the AI is working on it" : n.waiting ? `${plural(n.waiting, "problem")} waiting for you` : n.total ? `all ${n.total} checks pass` : "not watched";
const nodeClass = n => [n.bad.length && "bad", n.working && "work", !n.bad.length && n.waiting && "wait", !n.total && !n.working && !n.waiting && "idle"].filter(Boolean).join(" ");
function mapLine(l) {
  const a = MAP_AT[l.a], b = MAP_AT[l.b];
  if (!a || !b) return "";
  const mid = (a[0] + b[0]) / 2, d = a[0] === b[0] ? `M${a[0]},${a[1]}L${b[0]},${b[1]}` : `M${a[0]},${a[1]}C${mid},${a[1]} ${mid},${b[1]} ${b[0]},${b[1]}`;
  return `<path class="mlink ${l.bad ? "bad" : ""}" d="${d}"><title>${esc(l.a)} ${esc(l.ai)} to ${esc(l.b)} ${esc(l.bi)}</title></path>`;
}
function mapNode(n) {
  const at = MAP_AT[n.id];
  if (!at) return "";
  const label = MAP_NAME[n.id] || n.id, w = Math.max(60, label.length * 10 + 24), says = `${n.id}: ${deviceSays(n)}`;
  return `<g class="mnode ${nodeClass(n)}" data-dev="${esc(n.id)}" role="button" tabindex="0" aria-label="${esc(says)}"><title>${esc(says)}</title><rect x="${at[0] - w / 2}" y="${at[1] - 18}" width="${w}" height="36" rx="8"/><text x="${at[0]}" y="${at[1] + 1}">${esc(label)}</text></g>`;
}
function networkMap(m) {
  if (!m || !m.nodes.length) return "";
  const off = m.nodes.filter(n => !MAP_AT[n.id]);
  return `<div class="netmap"><div class="mapscroll"><svg viewBox="0 0 1180 262" role="group" aria-label="The network, with each device's state">${m.links.map(mapLine).join("")}${m.nodes.map(mapNode).join("")}${MAP_COLS.map(([x, t]) => `<text class="mcol" x="${x}" y="254">${t}</text>`).join("")}</svg></div>
    <div class="mapfoot"><span><i class="swatch bad"></i>Failing</span><span><i class="swatch work"></i>AI on it</span><span><i class="swatch wait"></i>Waiting for you</span>
      ${off.length ? `<span class="moff">Off the cabling plan<span class="chips">${off.map(n => `<button class="chip" type="button" data-dev="${esc(n.id)}" title="${esc(deviceSays(n))}"><span class="led ${n.bad.length ? "red" : n.total ? "green" : "off"}"></span>${esc(n.id)}</button>`).join("")}</span></span>` : ""}</div></div>`;
}
async function openDevice(id) {
  const n = S.now.map.nodes.find(x => x.id === id);
  if (!n) return;
  const failing = S.now.broken.filter(b => b.device === id);
  const links = S.now.map.links.filter(l => l.a === id || l.b === id).map(l => l.a === id ? `${l.ai} to ${l.b} ${l.bi}` : `${l.bi} to ${l.a} ${l.ai}`);
  openDrawer("dev-" + id, false, `${top_(esc(ROLE_WORDS[n.role] || "Device"))}<h2 id="d-title">${esc(id)}</h2><p class="about">${esc(deviceSays(n)[0].toUpperCase() + deviceSays(n).slice(1))}.</p>
    ${failing.length ? `<h3>Failing now</h3><ul class="reclist">${failing.map(b => `<li><span class="led red"></span><div>${b.run ? `<a href="#/run/${esc(b.run)}">${esc(b.title)}</a>` : esc(b.title)}<small>${esc(b.group)}</small></div></li>`).join("")}</ul>` : ""}
    <h3>Recent runs</h3><div id="dev-runs" class="muted">Loading</div>
    ${links.length ? `<h3>Cabled to</h3><ul class="reclist" style="gap:3px">${links.map(l => `<li style="display:block">${esc(l)}</li>`).join("")}</ul>` : ""}`);
  const box = () => $("#dev-runs");
  try {
    const d = await api(`api/runs?device=${encodeURIComponent(id)}&size=6`);
    if (box()) box().outerHTML = d.items.length ? `<ul class="reclist" style="color:var(--ink)">${d.items.map(r => `<li><span class="led ${stateOf(r.state).c}"></span><div><a href="#/run/${esc(r.id)}">${esc(r.title)}</a><small>${esc(stateOf(r.state).t)}, ${esc(ago(r.time))}</small></div></li>`).join("")}</ul>${d.total > d.items.length ? `<p class="note"><a href="#/failures?device=${encodeURIComponent(id)}">All ${d.total} runs on ${esc(id)}</a></p>` : ""}` : `<p class="muted">Nothing has happened on ${esc(id)} yet.</p>`;
  } catch (e) { if (box()) box().textContent = "Could not load its runs."; }
}

async function markHandled(ids, on = true) {
  const done = [], bad = [];
  for (const id of ids) {
    try { await post(`api/incidents/${encodeURIComponent(id)}/handled`, {handled: on}); done.push(id); } catch (e) { bad.push(e.message); }
  }
  S.cache.clear();
  return {done, bad};
}
async function afterHandled() { await refreshNow(); if (S.page !== "now") route(); }
async function handle(ids) {
  const {done, bad} = await markHandled(ids);
  if (!done.length) toast(bad[0] || "Nothing was marked.", true);
  else toast(done.length === 1 ? "Marked handled." : `Marked ${done.length} runs handled.`, false, async () => { await markHandled(done, false); toast("Put back in the queue."); afterHandled(); });
  afterHandled();
}
function drawNow(d, full) {
  const b = bandFor(d), stamp = $("#band-small");
  if (!full && stamp) { stamp.innerHTML = b.small; return; }
  const {live, waiting} = attention(d), every = waiting.flatMap(g => g.all.map(r => r.id));
  const rows = S.allWaiting ? waiting : waiting.slice(0, 6), more = waiting.length - rows.length;
  const held = d.waiting.map(w => `<li class="qrow"><div class="qmain"><span class="led amber"></span><span class="qtext"><b>${esc(w.title)}${sourceTag(w)}</b><small>Waiting to see if it clears by itself${w.count > 1 ? `, ${w.count} alarms merged` : ""}.</small></span><span class="qside">${w.due > Date.now() / 1000 ? `checks in ${esc(span(w.due - Date.now() / 1000))}` : "checking"}</span></div></li>`).join("");
  const review = d.review.pending ? `<li class="qrow"><a class="qmain" href="#/review"><span class="led violet"></span><span class="qtext"><b>${plural(d.review.pending, "suggestion")} from the AI to decide on</b><small>Accept or reject each one on the Review page.</small></span><span class="qside"></span></a></li>` : "";
  const fixed = d.latest.filter(r => r.state === "fixed").slice(0, 4);
  $("#main").innerHTML = `<div class="wrap">
    <div class="hero"><section class="band ${b.c}"><span class="led ${b.c} ${b.c === "red" ? "pulse" : ""}"></span><div><h1>${esc(b.big)}</h1><p id="band-small">${b.small}</p></div></section>${networkMap(d.map)}</div>
    <div class="now-grid">
      <div class="stack">
        ${live.length || held || d.ai_queue ? `<section class="panel alarm"><h2>Happening now</h2><ul class="qlist">${live.map(liveRow).join("")}${held}</ul>${d.ai_queue ? `<p class="note">${plural(d.ai_queue, "more run")} waiting for the AI.</p>` : ""}</section>` : ""}
        <section class="panel"><h2>Waiting for you${every.length > 1 ? `<button class="text-btn" id="q-all" type="button">Mark all handled</button>` : ""}</h2>
          ${review || rows.length ? `<ul class="qlist">${review}${rows.map(waitRow).join("")}</ul>` : `<p class="none">You are all caught up.</p>`}
          ${more ? `<button class="text-btn more-btn" id="q-more" type="button">Show ${more} more</button>` : ""}</section>
        ${fixed.length ? `<section class="panel plain"><h2>Fixed without you<a href="#/failures">Full history</a></h2><ul class="qlist">${fixed.map(fixedRow).join("")}</ul></section>` : ""}
      </div>
      <aside class="stack">
        <section class="panel"><h2>Last 24 hours</h2><ul class="tally">${d.funnel.map(tallyRow).join("")}</ul></section>
        <section class="panel"><h2>Network health <small>${d.groups.reduce((a, g) => a + g.total, 0)} checks</small></h2>${d.groups.length ? `<ul class="rack">${rack(d.groups)}</ul>` : `<p class="none">The heartbeat has not reported yet.</p>`}</section>
        <section class="panel"><h2>Services</h2><ul class="rack">${d.services.map(s => `<li><span class="name"><span class="led ${s.ok ? "green" : "red"}" style="margin-right:8px"></span>${esc(s.name)}</span><span class="n">${esc(s.note)}</span></li>`).join("")}</ul></section>
      </aside>
    </div></div>`;
  $$("#main [data-handle]").forEach(el => el.onclick = () => { el.disabled = true; handle(el.dataset.handle.split(",")); });
  if ($("#q-all")) $("#q-all").onclick = () => handle(every);
  if ($("#q-more")) $("#q-more").onclick = () => { S.allWaiting = true; drawNow(d, true); };
  $$("#main [data-dev]").forEach(el => {
    el.onclick = () => openDevice(el.dataset.dev);
    el.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDevice(el.dataset.dev); } };
  });
}

/* ---------- failures ---------- */
const WINDOW = {hour: 3600, day: 86400, week: 604800};
const STATE_ORDER = ["needs_you", "workaround", "working", "fixed", "held", "watching", "handled", "cleared", "info"];
function runsURL(q) {
  const p = new URLSearchParams();
  for (const k of ["state", "device", "by", "tag", "q", "sort", "dir", "page", "size"]) if (q.get(k)) p.set(k, q.get(k));
  if (WINDOW[q.get("window")]) p.set("since", Math.floor(Date.now() / 1000 - WINDOW[q.get("window")]));
  return "api/runs?" + p;
}
function withFilter(change, replace = false) {
  const q = new URLSearchParams(S.q);
  for (const [k, v] of Object.entries(change)) { if (v) q.set(k, v); else q.delete(k); }
  if (!("page" in change)) q.delete("page");
  const text = q.toString(), hash = "#/failures" + (text ? "?" + text : "");
  if (replace) { history.replaceState(null, "", hash); S.q = q; syncFilters(); loadRuns(q); }
  else location.hash = hash;
}
async function pageFailures(q) {
  setNav("failures");
  naming("History");
  S.q = q; S.picking = false; S.picked.clear(); S.asking = false; S.last = null;
  if (!S.tags) S.tags = await api("api/tags").catch(() => []);
  $("#main").innerHTML = `<div class="wrap">
    <div class="head"><h1>History</h1><p id="f-sum"></p><div class="acts"><button class="btn small" id="f-pick" type="button">Select</button></div></div>
    <div class="seg" id="f-states" role="group" aria-label="State"></div>
    <div class="fbar">
      <input type="search" id="f-q" placeholder="Search by device, problem or run" aria-label="Search runs">
      <select id="f-device" aria-label="Device"></select><select id="f-by" aria-label="Handled by"></select>
      <select id="f-window" aria-label="When"><option value="">Any time</option><option value="hour">Last hour</option><option value="day">Last 24 hours</option><option value="week">Last 7 days</option></select>
      <select id="f-sort" class="only-narrow" aria-label="Sort by"><option value="time:desc">Newest first</option><option value="time:asc">Oldest first</option><option value="state:asc">By state</option><option value="device:asc">By device</option><option value="took:desc">Slowest first</option></select>
      <button class="text-btn" id="f-clear" type="button" hidden>Clear filters</button>
    </div>
    <div class="flayout">
      <details class="tagbox" id="f-tagbox" ${matchMedia("(min-width: 1121px)").matches ? "open" : ""}><summary>Tags</summary><div class="areas" id="f-tags"></div></details>
      <div><div class="bulk" id="f-bulk" hidden></div><p class="legend" id="f-legend" hidden><span><i class="sw aimon"></i>Started by a monitor the AI suggested</span><span><span class="chip airule">AI rules</span> rules the AI wrote ran</span><span><span class="chip check">AI check</span> what the AI found when it checked after them</span></p><div class="tblwrap" id="f-wrap"><table class="tbl" id="f-table"></table></div><div class="pager" id="f-pager"></div></div>
    </div></div>`;
  let typing;
  $("#f-q").oninput = e => { clearTimeout(typing); typing = setTimeout(() => withFilter({q: e.target.value.trim()}, true), 180); };
  $("#f-device").onchange = e => withFilter({device: e.target.value});
  $("#f-by").onchange = e => withFilter({by: e.target.value});
  $("#f-window").onchange = e => withFilter({window: e.target.value});
  $("#f-sort").onchange = e => { const [sort, dir] = e.target.value.split(":"); withFilter({sort, dir}); };
  $("#f-clear").onclick = () => { location.hash = "#/failures"; };
  $("#f-pick").onclick = () => setPicking(!S.picking);
  syncFilters();
  loadRuns(q);
}
function syncFilters() {
  const q = S.q;
  const box = $("#f-q");
  if (box && document.activeElement !== box) box.value = q.get("q") || "";
  if ($("#f-window")) $("#f-window").value = q.get("window") || "";
  if ($("#f-sort")) $("#f-sort").value = `${q.get("sort") || "time"}:${q.get("dir") || "desc"}`;
}
async function loadRuns(q, quiet = false) {
  const key = q.toString(), hit = S.cache.get(key), mine = ++S.token;
  if (hit) drawRuns(hit, q); else if (!quiet) $("#f-wrap")?.classList.add("loading");
  try {
    const data = await api(runsURL(q));
    if (mine !== S.token || S.page !== "failures") return;
    S.cache.set(key, data);
    if (S.cache.size > 40) S.cache.delete(S.cache.keys().next().value);
    if (!hit || JSON.stringify(hit) !== JSON.stringify(data)) drawRuns(data, q);
    $("#f-wrap")?.classList.remove("loading");
    if (data.page < data.pages) {   /* the next page is ready before it is asked for */
      const next = new URLSearchParams(q); next.set("page", data.page + 1);
      if (!S.cache.has(next.toString())) api(runsURL(next)).then(d => S.cache.set(next.toString(), d)).catch(() => {});
    }
  } catch (e) { if (mine === S.token && $("#f-wrap")) $("#f-wrap").innerHTML = failed(e); }
}
function options(el, facet, all, want, name) {
  const keys = Object.keys(facet).sort((a, b) => a.localeCompare(b));
  if (want && !keys.includes(want)) keys.unshift(want);
  el.innerHTML = `<option value="">${esc(all)}</option>` + keys.map(k => `<option value="${esc(k)}" ${k === want ? "selected" : ""}>${esc(name(k))} (${facet[k] || 0})</option>`).join("");
}
function drawRuns(d, q) {
  S.last = d;
  const want = k => q.get(k) || "";
  $("#f-sum").textContent = d.total === d.all ? plural(d.all, "run") : `${d.total} of ${plural(d.all, "run")}`;
  $("#f-clear").hidden = ![...q.keys()].some(k => !["sort", "dir", "page", "size"].includes(k));
  options($("#f-device"), d.facets.device, "All devices", want("device"), x => x);
  options($("#f-by"), d.facets.by, "Rules and AI", want("by"), x => x === "ai" ? "AI" : "Rules");
  const totalStates = Object.values(d.facets.state).reduce((a, b) => a + b, 0);
  const seen = STATE_ORDER.filter(s => d.facets.state[s] || s === want("state"));
  $("#f-states").innerHTML = `<button type="button" data-state="" aria-pressed="${!want("state")}">All <i>${totalStates}</i></button>` +
    seen.map(s => `<button type="button" data-state="${s}" aria-pressed="${want("state") === s}"><span class="led ${stateOf(s).c}"></span>${esc(stateOf(s).t)} <i>${d.facets.state[s] || 0}</i></button>`).join("");
  $$("#f-states button").forEach(b => b.onclick = () => withFilter({state: b.dataset.state}));
  const areas = [...new Set((S.tags || []).map(t => t.area))];
  const tagN = d.facets.tag;
  $("#f-tags").innerHTML = areas.map(a => {
    const list = S.tags.filter(t => t.area === a), sum = list.reduce((n, t) => n + (tagN[t.tag] || 0), 0);
    return `<div class="area"><button type="button" data-tag="${esc(a)}" aria-pressed="${want("tag") === a}">${esc(a[0].toUpperCase() + a.slice(1))} <i>${sum || ""}</i></button>
      ${list.map(t => `<button class="tagb" type="button" data-tag="${esc(t.tag)}" aria-pressed="${want("tag") === t.tag}" title="${esc(t.description)}" ${tagN[t.tag] || want("tag") === t.tag ? "" : "disabled"}>${esc(t.name)} <i>${tagN[t.tag] || ""}</i></button>`).join("")}</div>`;
  }).join("");
  $$("#f-tags [data-tag]").forEach(b => b.onclick = () => withFilter({tag: b.getAttribute("aria-pressed") === "true" ? "" : b.dataset.tag}));
  const sort = want("sort") || "time", dir = want("dir") || "desc";
  const th = (key, label, cls = "") => `<th class="${cls}" ${sort === key ? `aria-sort="${dir === "asc" ? "ascending" : "descending"}"` : ""}><button type="button" data-sort="${key}">${label}</button></th>`;
  $("#f-table").className = `tbl ${S.picking ? "picking" : ""}`;
  $("#f-table").innerHTML = d.items.length ? `<thead><tr>${S.picking ? '<th class="c-pick"><span class="sr">Choose</span></th>' : ""}${th("state", "State")}${th("time", "When")}${th("device", "Device")}${th("title", "What happened")}<th>Tags</th>${th("by", "Handled by")}${th("took", "Took", "th-took")}</tr></thead>
    <tbody>${d.items.map(r => {
      const st = stateOf(r.state);
      return `<tr data-run="${esc(r.id)}" class="${r.ai_monitor ? "from-aimon" : ""}">${S.picking ? `<td class="c-pick"><input type="checkbox" data-pick="${esc(r.id)}" aria-label="Choose ${esc(r.title)}" ${S.picked.has(r.id) ? "checked" : ""}></td>` : ""}
        <td class="c-state"><span class="led ${st.c}"></span>${esc(st.t)}</td>
        <td class="c-when" title="${esc(dfmt(r.time))}, ${esc(tfmt(r.time))}">${esc(ago(r.time))}</td>
        <td class="c-dev"><a href="#/failures?device=${encodeURIComponent(r.device)}" style="text-decoration:none">${esc(r.device)}</a></td>
        <td class="c-what"><a href="#/run/${esc(r.id)}">${esc(r.title)}</a>${r.ticket ? ` <span class="chip">Ticket T-${esc(r.ticket)}</span>` : ""}${r.ai_monitor ? ` <span class="chip aimon" title="Started by monitor ${esc(r.monitor)}, which the AI suggested">AI monitor ${esc(r.monitor)}</span>` : ""}${r.learned ? ` <span class="chip airule" title="Rules the AI wrote ran in this run">AI rules</span>` : ""}${checkChip(r.check)}<small>${esc(r.headline)}</small></td>
        <td class="c-tags"><div class="chips">${r.tags.map(t => `<span class="chip" title="${esc(t)}">${esc(t.split("/").pop())}</span>`).join("")}</div></td>
        <td class="c-by">${byWord(r.by)}</td><td class="c-took">${esc(span(r.took))}</td></tr>`;
    }).join("")}</tbody>` : `<tbody><tr><td><div class="empty">No runs match these filters. <button class="text-btn" type="button" id="f-none">Clear filters</button></div></td></tr></tbody>`;
  const legend = $("#f-legend");
  if (legend) legend.hidden = !d.items.some(r => r.ai_monitor || r.learned);
  if ($("#f-none")) $("#f-none").onclick = () => { location.hash = "#/failures"; };
  $$("#f-table th button").forEach(b => b.onclick = () => {
    const k = b.dataset.sort, nextDir = sort === k ? (dir === "asc" ? "desc" : "asc") : (k === "time" || k === "took" ? "desc" : "asc");
    withFilter({sort: k, dir: nextDir});
  });
  $$("#f-table tr[data-run]").forEach(tr => tr.onclick = e => {
    if (e.target.closest("a, input, button, .c-pick") || getSelection().toString()) return;
    location.hash = "#/run/" + tr.dataset.run;
  });
  $$("#f-table [data-pick]").forEach(c => c.onchange = () => { if (c.checked) S.picked.add(c.dataset.pick); else S.picked.delete(c.dataset.pick); S.asking = false; drawBulk(); });
  drawPager(d);
  if (S.picking) drawBulk();
}
function drawPager(d) {
  const from = d.total ? (d.page - 1) * d.size + 1 : 0, to = Math.min(d.total, d.page * d.size);
  const nums = [...new Set([1, d.page - 1, d.page, d.page + 1, d.pages])].filter(n => n >= 1 && n <= d.pages).sort((a, b) => a - b);
  let last = 0, html = "";
  for (const n of nums) {
    if (n - last > 1) html += `<span class="muted">…</span>`;
    html += `<button type="button" data-page="${n}" ${n === d.page ? 'aria-current="page"' : ""} aria-label="Page ${n}">${n}</button>`;
    last = n;
  }
  $("#f-pager").innerHTML = `<span class="count">${from}–${to} of ${d.total}</span>
    <button type="button" data-page="${d.page - 1}" ${d.page <= 1 ? "disabled" : ""}>Previous</button>${html}
    <button type="button" data-page="${d.page + 1}" ${d.page >= d.pages ? "disabled" : ""}>Next</button>
    <select id="f-size" aria-label="Runs per page">${[25, 50, 100].map(n => `<option value="${n}" ${n === d.size ? "selected" : ""}>${n} a page</option>`).join("")}</select>`;
  $$("#f-pager [data-page]").forEach(b => b.onclick = () => withFilter({page: String(b.dataset.page)}));
  $("#f-size").onchange = e => withFilter({size: e.target.value});
}
function setPicking(on) {
  S.picking = on; S.asking = false; S.picked.clear();
  $("#f-pick").textContent = on ? "Cancel" : "Select";
  $("#f-bulk").hidden = !on;
  if (S.last) drawRuns(S.last, S.q);
  if (on) drawBulk();
}
function drawBulk() {
  const n = S.picked.size, ids = S.last ? S.last.items.map(i => i.id) : [], every = ids.length && ids.every(i => S.picked.has(i));
  $("#f-bulk").innerHTML = S.asking
    ? `<span>Delete ${plural(n, "run")}? This cannot be undone.</span><span class="right"><button class="btn small danger" id="b-yes" type="button">Delete ${plural(n, "run")}</button><button class="btn small" id="b-no" type="button">Keep them</button></span>`
    : `<span>${n ? `${n} selected` : "Choose some runs"}</span><button class="text-btn" id="b-all" type="button">${every ? "Clear this page" : "Select this page"}</button><span class="right"><button class="btn small" id="b-done" type="button" ${n ? "" : "disabled"}>Mark handled</button><button class="btn small danger" id="b-del" type="button" ${n ? "" : "disabled"}>Delete</button></span>`;
  if ($("#b-done")) $("#b-done").onclick = async () => {
    const list = [...S.picked], {done, bad} = await markHandled(list);
    toast(done.length ? `Marked ${plural(done.length, "run")} handled.${bad.length ? ` ${plural(bad.length, "run")} did not need you, so stayed as they were.` : ""}` : "None of those runs were waiting for you.", !done.length, done.length ? async () => { await markHandled(done, false); toast("Put back in the queue."); afterHandled(); } : null);
    setPicking(false);
    afterHandled();
  };
  if ($("#b-all")) $("#b-all").onclick = () => { ids.forEach(i => every ? S.picked.delete(i) : S.picked.add(i)); drawRuns(S.last, S.q); };
  if ($("#b-del")) $("#b-del").onclick = () => { S.asking = true; drawBulk(); $("#b-no").focus(); };
  if ($("#b-no")) $("#b-no").onclick = () => { S.asking = false; drawBulk(); };
  if ($("#b-yes")) $("#b-yes").onclick = async () => {
    const bad = await deleteRuns([...S.picked]);
    S.cache.clear();
    if (bad.length) toast(`${plural(bad.length, "run")} could not be deleted.`, true); else toast(`Deleted ${plural(n, "run")}.`);
    setPicking(false);
    loadRuns(S.q);
    refreshNow();
  };
}
async function deleteRuns(ids) {
  const bad = [];
  for (const id of ids) {
    try {
      const r = await fetch(BASE + "api/incidents/" + encodeURIComponent(id), {method: "DELETE"});
      if (!r.ok && r.status !== 404) bad.push(id);   /* 404: already gone, which is what was asked for */
    } catch (e) { bad.push(id); }
  }
  return bad;
}
setInterval(() => { if (S.page === "failures" && !document.hidden && !S.picking && $("#f-table")) loadRuns(S.q, true); }, 15000);

/* ---------- one run ---------- */
const KIND = {cause: {title: "Cause found", c: "red"}, stop: {title: "Stopped", c: "grey"}, escalate: {title: "Needs a person", c: "amber"}, goto: {title: "Handed on", c: "grey"}};
const notConfirmed = r => r.kind === "stop" && /^Not confirmed/.test(r.text || "");
function verdictWords(r) {
  if (notConfirmed(r)) return ["Not confirmed", r.text.replace(/^Not confirmed\.\s*/, "")];
  return [KIND[r.kind]?.title || r.kind, r.text || r.label || ""];
}
const isLinkDown = ev => !ev.event || ev.event === "interface.down";
const swept = ev => ev.source === "health";
const ticketed = ev => ev.source === "ticket";
const startedBy = ev => ticketed(ev) ? "Ticket" : swept(ev) ? "Heartbeat" : "Trap";
const ticketLink = n => `<a href="${deskLink(n)}">T-${esc(n)}</a>`;
const evTitle = ev => ev.title || `${ev.device} ${ev.interface} went down`;
const evTrigger = ev => ev.trigger || `linkDown arrived from ${ev.device}`;
const ABOUT = {
  "ospf.neighbor_down": "The device told the trap receiver that an OSPF neighbor went down. OSPF often recovers by itself, so the first check asks the device whether the neighbor is still gone.",
  "vrrp.master_change": "A switch took over the gateway for a VLAN. That is normal during a failover, so the first check asks which switch holds it now.",
  "config.changed": "The device said its running config was saved. Most saves change nothing, so the checks look for a real difference before anything else.",
  "device.restarted": "The SNMP agent on the device said it just started. The agent can restart on its own, so the checks look at how long the device has really been up."};
const SWEPT_ABOUT = "Nothing on the network sends a trap for this, so the watcher looks for itself once a minute. An investigation opens only when something that was fine stops being fine.";
const LINK_ABOUT = "The switch told the trap receiver that one of its ports went down. A trap on its own can be a one-second flap, or it can get lost, so the next checks ask Prometheus whether it agrees.";
const aboutEvent = ev => isLinkDown(ev) ? LINK_ABOUT : swept(ev) ? SWEPT_ABOUT : (ABOUT[ev.event] || "");
function originText(ev) {
  if (isLinkDown(ev)) return `${ev.interface}${ev.description ? ` (${ev.description})` : ""}, admin status ${ev.admin_status}, at ${tfmt(ev.time)}.`;
  return `${ev.detail || ""} At ${tfmt(ev.time)}.${ev.merged ? ` ${ev.merged} of these arrived close together and count as one.` : ""}`;
}
const EVENT_FACTS = [["interface", "Port"], ["neighbor_id", "Neighbor"], ["neighbor_ip", "Neighbor address"], ["peer_ip", "Peer"], ["vrid", "VRRP group"], ["master_ip", "Address claimed"],
  ["prefix", "Route"], ["expected_protocol", "Last came from"], ["internet_target", "Test address"], ["default_prefix", "Route it needs"], ["merged", "Merged into this run"]];
const SOURCE_NAMES = {jenkins: "Jenkins", github: "GitHub", prometheus: "Prometheus", datalake: "Data lake", grafana: "Grafana"};
const REPAIR_WORDS = {fixed: "Repaired", failed: "Repair failed", held: "Not repaired", would_fix: "Would repair", already: "Already back"};
const VERDICTS = {bad_hardware: "Looks like failing hardware", tampering: "Looks like someone keeps doing this", scheduled: "Looks like a scheduled job", repeating: "This keeps happening"};

const TODO_WORDS = {needs_you: "What to do", workaround: "What to do", handled: "Handled", working: "In progress", fixed: "Done", cleared: "Nothing to do", held: "Held back", watching: "Watch only", info: "For the record"};
const BACK_WORDS = {"": "Now", now: "Now", failures: "History", review: "Review", rules: "Rules", tags: "Tags", "ai-data": "Data", collect: "Collect", train: "Train", monitors: "Monitors"};
function backLink() {
  const to = S.backTo || "#/failures", name = to.replace(/^#\/?/, "").split(/[?/]/)[0];
  return `<a class="back" href="${esc(to)}">Back to ${esc(BACK_WORDS[name] || "History")}</a>`;
}
const fixSteps = rec => rec.repair ? (rec.repair.steps || []).filter(s => s.kind !== "require") : [];
const changedNetwork = rec => !!rec.repair && ["fixed", "failed"].includes(rec.repair.status);
const aiCalls = rec => (rec.agent && rec.agent.calls) || [];
const tookOver = rec => !!rec.agent && ["queued", "working", "done", "failed"].includes(rec.agent.status);
const RANK = {beginning: 0, main: 1, end: 2};
function triedSplit(rec) {
  const mine = RANK[rec.rule.position || "main"], list = rec.tried || [];
  return {before: list.filter(t => RANK[t.position] <= mine), after: list.filter(t => RANK[t.position] > mine)};
}
function buildFrames(rec) {
  const t = triedSplit(rec), f = [{type: "trap"}];
  t.before.forEach(x => f.push({type: "tried", t: x, n: rec.tried.indexOf(x)}));
  rec.steps.forEach((s, k) => f.push({type: "step", k, i: s.n - 1}));
  f.push({type: "result"});
  t.after.forEach(x => f.push({type: "tried", t: x, n: rec.tried.indexOf(x)}));
  fixSteps(rec).forEach((_, k) => f.push({type: "fix", k}));
  if (rec.retrieval) f.push({type: "memory"});
  if (tookOver(rec)) { f.push({type: "handover"}); aiCalls(rec).forEach((_, k) => f.push({type: "call", k})); }
  else if (rec.analysis) f.push({type: "ai"});
  return f;
}
const keyOf = f => f.type === "step" ? "step-" + f.i : f.type === "fix" ? "fix-" + f.k : f.type === "call" ? "call-" + f.k : f.type === "tried" ? "tried-" + f.n : f.type;
const frameIndex = key => S.frames.findIndex(f => keyOf(f) === key);
const lastFrame = () => S.frames.length - 1;

function doneLines(rec) {
  const out = [], n = rec.steps.length, f = rec.repair, ai = rec.agent;
  out.push(`The rules ran ${plural(n, "check")}.`);
  if (f) {
    const when = f.finished ? ` at ${tfmt(f.finished)}` : "";
    out.push(f.status === "fixed" ? `${esc(f.title)}${when}, and it verified.` : `${esc(REPAIR_WORDS[f.status] || f.status)}: ${esc(f.why || f.title)}`);
  }
  if (tookOver(rec)) {
    const ch = ai.changes || [], kept = ch.filter(c => c.status === "kept").length, back = ch.filter(c => c.status === "reverted").length;
    const bits = [plural(aiCalls(rec).length, "step")];
    if (kept) bits.push(`${kept} kept`);
    if (back) bits.push(`${back} put back`);
    if ((ai.proposals || []).length) bits.push(plural(ai.proposals.length, "suggestion"));
    out.push(`<span class="ai">AI</span> ${ai.status === "working" || ai.status === "queued" ? "Working now" : "Took over"}: ${bits.join(", ")}.`);
  } else if (ai && ai.status === "skipped") out.push(`The AI did not take this: ${esc(ai.note)}`);
  if (!changedNetwork(rec) && !(ai && (ai.changes || []).some(c => ["kept", "reverted"].includes(c.status)))) out.push("Nothing on the network was changed.");
  if (rec.email) out.push(rec.email.sent ? "Emailed." : `Not emailed: ${esc(rec.email.why)}.`);
  return out;
}
const CHECK_WORDS = {working: ["AI check: working", "green"], broken: ["AI check: still broken", "red"], unsure: ["AI check: unsure", "amber"], checking: ["AI checking", "violet"]};
function checkChip(v) {
  const c = CHECK_WORDS[v];
  return c ? ` <span class="chip check ${c[1]}" title="After the rules the AI wrote, the AI checked whether it was still broken">${esc(c[0])}</span>` : "";
}
/* the parts of the run in order, the same stages its troubleshooting-run build shows in Jenkins */
const STAGE_TONE = {done: "green", failed: "red", running: "violet", waiting: "off", skipped: "off"};
const STAGE_WORDS = {done: "Done", failed: "Did not work", running: "Running", waiting: "Waiting", skipped: "Skipped"};
function stageStrip(rec) {
  const list = rec.stages || [];
  if (!list.length) return "";
  const jr = rec.jenkins_run || {};
  const jlink = jr.url ? `<a href="${esc(jr.url)}" target="_blank" rel="noreferrer">troubleshooting-run #${esc(jr.number)} &#8599;</a>` : jr.error ? `<span class="muted">Not in Jenkins: ${esc(jr.error)}</span>` : "";
  const open = S.stageOpen;
  const item = (p, k) => {
    const marks = [p.ai_monitor ? '<span class="chip aimon">AI monitor</span>' : "", p.ai ? '<span class="ai">AI</span>' : "", p.watch_only ? '<span class="chip">watch-only</span>' : "",
      ["check", "agent"].includes(p.key) ? '<span class="ai">AI</span>' : ""].join("");
    return `<li class="stg ${p.state} ${p.ai_monitor ? "aimon" : ""} ${p.ai || ["check", "agent"].includes(p.key) ? "byai" : ""}" style="--i:${k}">
      <button type="button" data-stage="${k}" aria-expanded="${open === k}"><span class="led ${STAGE_TONE[p.state] || "off"} ${p.state === "running" ? "pulse" : ""}"></span><span class="stl">${esc(p.label.replace(/^(Rule|Repair|Trigger|First look): /, ""))}</span>${marks}<span class="sts">${esc(STAGE_WORDS[p.state] || p.state)}</span></button></li>`;
  };
  const shown = list[open];
  return `<section class="panel stages"><h2>How this run went <small>the same stages as in Jenkins</small>${jlink ? `<span class="jl">${jlink}</span>` : ""}</h2>
    <ol class="strip">${list.map(item).join("")}</ol>
    ${shown ? `<div class="stgtext"><b>${esc(shown.label)}</b>${shown.link ? ` <a href="${esc(shown.link)}" target="_blank" rel="noreferrer">its build &#8599;</a>` : ""}<pre>${esc(shown.text || "")}</pre></div>` : '<p class="sub muted">Choose a stage to see what it found.</p>'}</section>`;
}
function aiMonitorBanner(rec) {
  const m = rec.ai_monitor;
  if (!m) return "";
  return `<div class="aimon-banner"><span class="chip aimon">AI monitor ${esc(rec.event.monitor || "")}</span> ${esc(rec.started_by || "")}${m.from_run ? ` <a href="#/run/${esc(m.from_run)}">The run it came from</a>.` : ""} <a href="#/monitors">Monitors</a></div>`;
}
/* the two questions asked before anything was changed */
function firstLook(rec) {
  const f = rec.first_look;
  if (!f) return "";
  const c = f.changed || {}, w = f.working || {};
  const config = (c.config || []).flatMap(d => d.sections.map(s => `${d.device}, ${s.section}: ${[...(s.removed || []).map(x => "removed " + x), ...(s.added || []).map(x => "added " + x)].join("; ")}. ${s.because ? `Made by run ${s.because.run}${s.because.ticket ? ` for T-${s.because.ticket}` : ""}: ${s.because.why || "no reason recorded"}` : "Nothing on record explains it."}`));
  const earlier = (c.earlier || []).flatMap(g => g.changes.map(x => `Left in place on purpose${g.ticket_words ? ` for ${g.ticket_words}` : ""}, ${x.device}: ${x.lines.join("; ")}. Why: ${x.why || "not recorded"}`));
  const more = [...(w.lines || []), ...config, ...earlier, ...(c.unread || []), ...(c.ours || []), ...(c.deploys || []), ...(c.held || []).map(x => "On hold: " + x), ...(c.events || [])];
  return `<section class="panel"><h2>Checked first</h2><p><b>Was it working before?</b> ${esc(w.summary || "Not recorded.")}</p><p><b>Has anything changed?</b> ${esc(c.summary || "Not recorded.")}</p>${more.length ? `<details class="more"><summary>What was looked at</summary><ul class="sub">${more.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}</section>`;
}
function standing(rec) {
  const a = rec.analysis || {}, ai = rec.agent || {}, f = rec.repair || {};
  const next = a.next && a.next.toLowerCase() !== "nothing" ? a.next : "";
  switch (rec.state) {
    case "fixed": return rec.by === "ai" ? `Fixed by the AI, and the change was kept. ${next}` : `Fixed and verified.${rec.check && rec.check.verdict === "working" ? " The AI checked afterwards and agreed it is working." : ""} ${next}`;
    case "workaround": return `A temporary change is keeping this working, and known-good will not undo it for now. Someone still has to do the real repair. ${next}`;
    case "handled": return `${String(rec.handled.by).startsWith("review:") ? `You accepted suggestion #${rec.handled.by.slice(7)}` : "You marked this handled"} ${ago(rec.handled.at)}. ${next}`;
    case "cleared": return ai.status === "not_needed" ? (rec.check ? `The AI checked after the rules it wrote and found it working, so the support agent was not started. ${rec.check.summary || ""}` : "It had already cleared when the AI's turn came.") : "It went away by itself. Nothing more to do.";
    case "working": return rec.check && rec.check.status === "working" ? "The AI is checking whether it is still broken. Only if it is does the support agent start." : ai.status === "queued" ? (rec.learned && !rec.check ? "Waiting for the AI to check whether it is still broken." : "Waiting for the AI to start.") : "The AI is working on this now. This page follows along.";
    case "held": return `The repair was held back. ${f.why || ""}`;
    case "watching": return `This repair is in watch-only mode, so it did not run. ${f.why || ""}`;
    case "info": return "Recorded for reference. Nothing needs doing.";
    default: return next || ai.error || "A person needs to look at this.";
  }
}
async function pageRun(id) {
  const cameFrom = (S.backTo || "#/failures").replace(/^#\/?/, "").split(/[?/]/)[0];
  setNav("failures", {"": "now", "ai-data": "aidata"}[cameFrom] ?? cameFrom);   /* the sidebar stays on the page you came from */
  const rec = await api("api/incidents/" + encodeURIComponent(id));
  showRun(rec);
  clearInterval(S.poll);
  S.poll = setInterval(async () => {
    if (document.hidden || S.playing || !S.rec || S.rec.id !== id || S.rec.state !== "working") return;
    try {
      const fresh = await api("api/incidents/" + encodeURIComponent(id));
      if (JSON.stringify(fresh.agent) !== JSON.stringify(S.rec.agent) || fresh.state !== S.rec.state || JSON.stringify(fresh.stages) !== JSON.stringify(S.rec.stages)) showRun(fresh);
    } catch (e) { /* the next tick tries again */ }
  }, 4000);
}
const BUILD_WORDS = {SUCCESS: ["Passed", "green"], UNSTABLE: ["Unstable", "amber"], FAILURE: ["Failed", "red"], ABORTED: ["Stopped", "amber"]};
function jenkinsRuns(rec) {
  const t = triedSplit(rec), rows = [], add = (what, j) => { if (j) rows.push([what, j]); };
  t.before.forEach(x => add(x.title || x.rule, x.jenkins));
  add(rec.rule.title || rec.rule.id, rec.jenkins);
  t.after.forEach(x => add(x.title || x.rule, x.jenkins));
  if (rec.repair) add(`Repair: ${rec.repair.title || rec.repair.rule}`, rec.repair.jenkins);
  if (!rows.length) return "";
  const item = ([what, j]) => {
    const [w, tone] = j.ran === "here" ? ["Ran here", "amber"] : j.broke ? ["Broke part-way", "amber"] : BUILD_WORDS[j.result] || [j.result ? words(j.result.toLowerCase()) : "Done", ""];
    const link = j.url ? `<a href="${esc(j.url)}" target="_blank" rel="noreferrer">${esc(j.pipeline)} #${esc(j.number)} &#8599;</a>` : `<a href="${esc(j.pipeline_url)}" target="_blank" rel="noreferrer">${esc(j.pipeline)} &#8599;</a>`;
    const why = j.ran === "here" ? `Jenkins could not run it, so the troubleshooter did: ${j.why || ""}` : j.broke ? `The build stopped after the change, so it was finished here and anything left half-done was undone: ${j.broke}` : "";
    return `<li><div class="jrow"><span class="jwhat">${esc(what)}</span>${link}<span class="ans ${tone}">${esc(w)}</span></div>${why ? `<p class="sub">${esc(why)}</p>` : ""}</li>`;
  };
  return `<section class="panel jenk"><h2>Ran in Jenkins <small>${esc(jenkinsFolder())}</small></h2><ul>${rows.map(item).join("")}</ul></section>`;
}
function showRun(rec) {
  const openWas = S.drawer;
  if (!S.rec || S.rec.id !== rec.id) { S.showReads = false; S.stageOpen = null; }
  S.rec = rec; S.rule = rec.rule; S.frames = buildFrames(rec); S.pos = lastFrame();
  const ev = rec.event, st = stateOf(rec.state), a = rec.analysis, [vt, vx] = verdictWords(rec.result), ai = rec.agent;
  const total = (ai && ai.finished || rec.finished || ev.time) - ev.time;
  naming(evTitle(ev));
  const act = ["needs_you", "workaround"].includes(rec.state) ? `<button class="btn go" id="r-handled" type="button">Mark handled</button>` : rec.state === "handled" ? `<button class="btn" id="r-unhandle" type="button">Put back in the queue</button>` : "";
  $("#main").innerHTML = `<div class="wrap">
    ${backLink()}
    ${aiMonitorBanner(rec)}
    <header class="runh ${st.c} ${rec.ai_monitor ? "aimon" : ""}">
      <div class="runtop"><div>
        <div class="state"><span class="led ${st.c} ${rec.state === "working" ? "pulse" : ""}"></span>${esc(st.t)}${rec.by === "ai" ? ' <span class="ai">AI</span>' : ""}</div>
        <h1>${esc(evTitle(ev))}</h1>${ev.ticket ? `<p class="muted">Started by ticket ${ticketLink(ev.ticket)} on the service desk</p>` : ""}
        <p class="meta"><a href="#/failures?device=${encodeURIComponent(ev.device)}">${esc(ev.device)}</a> on ${esc(dfmt(ev.time))} at ${esc(tfmt(ev.time))}, ${esc(ago(ev.time))}. Took ${esc(span(total))}.${rec.pattern && rec.pattern.count_7d > 1 ? ` Seen ${rec.pattern.count_7d} times this week.` : ""}</p>
      </div>${act ? `<div class="runacts">${act}</div>` : ""}</div>
      ${(rec.tags || []).length ? `<div class="chips">${rec.tags.map(t => `<a class="chip" href="#/failures?tag=${encodeURIComponent(t)}">${esc(t)}</a>`).join("")}</div>` : ""}
    </header>
    <section class="todo"><h2>${esc(TODO_WORDS[rec.state] || "Where it stands")}</h2><p>${ticks(standing(rec))}</p></section>
    <div id="stagebox">${stageStrip(rec)}</div>
    ${firstLook(rec)}
    <div class="pair">
      <section class="panel"><h2>What broke</h2><p>${ai && ai.root_cause ? ticks(plainCause(ai.root_cause)) : `<b>${esc(vt)}.</b> ${esc(vx)}`}</p></section>
      <section class="panel"><h2>What was done</h2><ul>${doneLines(rec).map(x => `<li>${x}</li>`).join("")}</ul></section>
    </div>
    ${jenkinsRuns(rec)}
    ${a ?`<section class="airead"><h2><span class="ai">AI</span> What it makes of this</h2><p>${esc(a.summary)}</p>${a.why ? `<details class="more"><summary>Why it thinks so</summary><p class="sub">${esc(a.why)}</p></details>` : ""}<p class="sub" style="margin-top:6px"><a href="#/ai-data?q=${esc(rec.id)}">See everything it was given and what it was thinking</a></p></section>` : ""}
    <div class="replay" role="group" aria-label="Replay controls">
      <h2>Replay</h2>
      <button class="btn go" id="play" type="button"></button>
      <button class="btn" id="prev" type="button">Back</button>
      <button class="btn" id="next" type="button">Forward</button>
      <span class="speed" role="group" aria-label="Replay speed">${[1, 2, 4].map(n => `<button type="button" data-speed="${n}" aria-pressed="${S.speed === n}">${n}×</button>`).join("")}</span>
      <label class="follow"><input type="checkbox" id="follow" ${S.follow ? "checked" : ""}> Open each step as it plays</label>
    </div>
    <div class="ticks" id="ticks"></div>
    <ol class="map" id="map" aria-label="The route this investigation took"></ol>
    ${afterwards(rec)}
    <p class="rundel" id="delrun"></p></div>`;
  const stageClicks = () => $$("#stagebox [data-stage]").forEach(b => b.onclick = () => {
    const k = +b.dataset.stage;
    S.stageOpen = S.stageOpen === k ? null : k;
    $("#stagebox").innerHTML = stageStrip(S.rec);
    stageClicks();
  });
  stageClicks();
  if ($("#r-handled")) $("#r-handled").onclick = () => handle([rec.id]);
  if ($("#r-unhandle")) $("#r-unhandle").onclick = async () => { await markHandled([rec.id], false); toast("Put back in the queue."); afterHandled(); };
  $("#play").onclick = () => S.playing ? stopReplay() : play();
  $("#prev").onclick = () => { stopReplay(); go(S.pos - 1, true); };
  $("#next").onclick = () => { stopReplay(); go(S.pos + 1, true); };
  $("#follow").onchange = e => { S.follow = e.target.checked; };
  $$(".speed button").forEach(b => b.onclick = () => {
    S.speed = +b.dataset.speed;
    $$(".speed button").forEach(x => x.setAttribute("aria-pressed", x === b));
    if (S.playing) { clearTimeout(S.timer); schedule(); }
  });
  drawDelete();
  draw();
  if (openWas) openDrawer(openWas, true);
}
function drawDelete(asking = false, oops = "") {
  const box = $("#delrun");
  if (!box) return;
  box.innerHTML = asking
    ? `<span>Delete this run?</span><button class="btn small danger" type="button" id="del-yes">Delete run</button><button class="btn small" type="button" id="del-keep">Keep</button>`
    : `${oops ? `<span>${esc(oops)}</span>` : ""}<button class="text-btn" type="button" id="del-ask">Delete run</button>`;
  if (asking) {
    $("#del-keep").onclick = () => drawDelete();
    $("#del-yes").onclick = async () => {
      if ((await deleteRuns([S.rec.id])).length) return drawDelete(false, "This run could not be deleted.");
      S.cache.clear(); toast("Deleted the run."); refreshNow(); location.hash = "#/failures";
    };
    $("#del-keep").focus();
  } else $("#del-ask").onclick = () => drawDelete(true);
}

const pos = () => S.pos;
function row({key, phase = "", cls = "", q, ans = "", saw = "", cmd = "", branches = ""}) {
  const open = S.drawer === key;
  return `<li class="st ${cls}"><div class="phase">${esc(phase)}</div><div class="rail"><span class="dot"></span></div><div class="body">
    <button class="station" type="button" data-open="${esc(key)}" aria-expanded="${open}"><div class="q"><span>${q}</span>${ans}</div>${cmd ? `<span class="cmdline">${esc(cmd)}</span>` : ""}${saw ? `<div class="saw">${saw}</div>` : ""}</button>${branches}</div></li>`;
}
function classes(f, animate, extra = "") {
  const on = f >= 0 && f <= S.pos, out = f >= 0 && f < S.pos;
  return `${on ? "in" : "future"} ${out ? "out" : ""} ${animate && f === S.pos ? "now" : ""} ${extra}`;
}
const CALL_WORDS = {kept: ["Kept", "green"], reverted: ["Put back", "amber"], refused: ["Refused", ""], failed: ["Failed", "amber"], saved: ["Waiting for review", "violet"], invalid: ["Not accepted", ""], error: ["Error", "amber"], done: ["Done", "violet"], ok: ["", ""]};
function callView(c) {
  const a = c.args || {}, t = c.tool, on = a.device ? ` on ${a.device}` : "";
  if (t === "show") return {q: `Read from ${a.device}`, cmd: a.command};
  if (t.startsWith("check_")) return {q: `Check ${words(t.slice(6))}${on}`};
  if (t.startsWith("action_")) return {q: `Run ${words(t.slice(7))}${on}`, change: true};
  return ({retire_earlier_change: {q: "Take out an earlier stop-gap", cmd: a.run || "", change: true}, recall_my_notes: {q: "Look through its own notes", cmd: a.text || ""}, try_config: {q: `Try a config change${on}`, cmd: (a.lines || []).join("; "), change: true}, try_command: {q: `Try a command${on}`, cmd: (a.argv || []).join(" "), change: true},
    search_memory: {q: "Search the stored records", cmd: a.text || ""}, search_jenkins: {q: "Search Jenkins builds", cmd: [a.text, a.job, a.result].filter(Boolean).join(" · ")}, read_build: {q: `Read Jenkins build ${a.job || ""} #${a.number || ""}`, cmd: a.part > 1 ? `part ${a.part}` : ""}, search_runs: {q: "Search past runs", cmd: a.text || ""}, search_rejected: {q: "Check what people rejected", cmd: a.text || ""},
    list_tags: {q: "Read the tag list"}, list_rules: {q: "Read the installed rules"}, propose_rule: {q: `Suggest a rule: ${a.title || a.id}`, propose: true}, propose_tag: {q: `Suggest a new tag: ${a.area}/${a.name}`, propose: true},
    propose_backdate: {q: `Suggest ${a.tag} for past runs`, propose: true}, finish: {q: `Finish: ${words(a.outcome || "done")}`}})[t] || {q: words(t)};
}
const callAns = c => {
  const [w, tone] = CALL_WORDS[c.outcome] || [words(c.outcome), ""];
  return w ? `<span class="ans ${tone}">${esc(w)}</span>` : (c.tool.startsWith("check_") ? `<span class="ans">${esc(words(c.outcome))}</span>` : "");
};
const callTone = c => ({kept: "tone-green", reverted: "tone-amber", failed: "tone-amber"})[c.outcome] || "";

function draw(animate = false) {
  const rec = S.rec, F = S.frames, rule = S.rule, tries = triedSplit(rec), html = [];
  const rf = F.findIndex(f => f.type === "result"), decided = rec.result.step === "end" ? "end" : rec.result.step - 1;
  html.push(row({key: "trap", phase: startedBy(rec.event), cls: classes(0, animate, "first"), q: esc(evTrigger(rec.event)), saw: esc(originText(rec.event))}));
  const triedRow = (x, phase) => {
    const n = rec.tried.indexOf(x), f = frameIndex("tried-" + n);
    html.push(row({key: "tried-" + n, phase, cls: classes(f, animate, "small"), q: `Also tried: ${esc(x.title || x.rule)}`, ans: `<span class="ans">${x.mode === "shadow" ? "Watch only" : esc(verdictWords(x.result)[0])}</span>`, saw: esc(x.result.text || "")}));
  };
  tries.before.forEach((x, n) => triedRow(x, n === 0 ? "Before" : ""));
  let lastPhase = "";
  (rule.steps || []).forEach((s, i) => {
    const f = frameIndex("step-" + i), st = f >= 0 ? rec.steps[F[f].k] : null, phase = s.phase !== lastPhase ? ({confirm: "Confirm", investigate: "Investigate"}[s.phase] || "") : "";
    lastPhase = s.phase;
    const reached = f >= 0 && f <= S.pos, taken = name => reached && st.outcome === name && decided === i && S.pos >= rf;
    const entries = Object.entries(s.exits || {}).filter(([name]) => taken(name));   /* every other way out is in the side panel */
    const branches = entries.length ? `<div class="branches">${entries.map(([name, e]) => `<button type="button" class="br ${taken(name) ? "taken" : ""}" data-open="${taken(name) ? "result" : `exit-${i}-${esc(name)}`}"><b>if ${esc(words(name))}</b><span>${taken(name) ? esc(rec.result.text) : tpl(e.text || e.label || e.target)}</span></button>`).join("")}</div>` : "";
    html.push(row({key: "step-" + i, phase, cls: classes(f, animate, f < 0 ? "small" : ""), q: reached ? esc(st.ask) : tpl(s.ask), ans: reached ? `<span class="ans">${esc(words(st.outcome))}</span>` : "", saw: reached ? esc(st.saw) : "", branches: reached || f < 0 ? branches : ""}));
  });
  const [vt, vx] = verdictWords(rec.result), onRes = S.pos >= rf;
  html.push(row({key: "result", phase: "Answer", cls: classes(rf, animate, onRes ? ({cause: "tone-red", escalate: "tone-amber"}[rec.result.kind] || "") : ""), q: esc(onRes ? vt : "The answer"), saw: onRes ? esc(vx) : ""}));
  tries.after.forEach((x, n) => triedRow(x, n === 0 ? "After" : ""));
  fixSteps(rec).forEach((s, k) => {
    const f = frameIndex("fix-" + k), on = f <= S.pos, good = ["done", "pass", "match", "up", "reachable", "present"].includes(s.outcome);
    html.push(row({key: "fix-" + k, phase: k === 0 ? "Repair" : "", cls: classes(f, animate, on && good ? "tone-green" : ""), q: esc(s.ask), ans: on ? `<span class="ans">${esc(words(s.outcome))}</span>` : "", saw: on ? esc(s.saw) : ""}));
  });
  if (rec.retrieval) {
    const f = frameIndex("memory"), on = f <= S.pos, m = rec.retrieval;
    html.push(row({key: "memory", phase: "Memory", cls: classes(f, animate), q: "What has it seen like this before?", ans: on ? `<span class="ans">${(rec.retrieved || []).length} found</span>` : "", saw: on ? `Searched ${m.searched.runs} past runs and ${m.searched.docs} stored records.` : ""}));
  }
  if (tookOver(rec)) {
    const f = frameIndex("handover"), ai = rec.agent;
    html.push(`<li class="st hand ${classes(f, false)}"><div class="phase">AI</div><div class="rail"></div><div class="body"><button class="note" type="button" data-open="handover" aria-expanded="${S.drawer === "handover"}"><b><span class="ai">AI</span> took over here${ai.started ? ` at ${esc(tfmt(ai.started))}` : ""}</b><span>${esc(ai.why || "")}</span></button></div></li>`);
    const quiet = c => { const v = callView(c); return !S.showReads && rec.state !== "working" && !v.change && !v.propose && c.tool !== "finish"; };
    let folded = 0;
    const unfold = () => { if (folded) html.push(`<li class="st byai in small"><div class="phase"></div><div class="rail"><span class="dot"></span></div><div class="body"><button class="station fold" type="button" data-fold="1">${plural(folded, "read or search", "reads and searches")}<span class="muted">Show them</span></button></div></li>`); folded = 0; };
    aiCalls(rec).forEach((c, k) => {
      if (quiet(c)) { folded++; return; }
      unfold();
      const fr = frameIndex("call-" + k), v = callView(c), on = fr <= S.pos;
      html.push(row({key: "call-" + k, cls: classes(fr, animate, `byai ${on ? callTone(c) : ""}`), q: esc(v.q), ans: on ? callAns(c) : "", cmd: v.cmd || "", saw: on ? esc(c.saw || "") : ""}));
    });
    unfold();
    if (ai.status === "working" || ai.status === "queued") html.push(`<li class="st byai"><div class="phase"></div><div class="rail"><span class="dot"></span></div><div class="body"><div class="station"><div class="q muted">${ai.status === "queued" ? "Waiting for its turn" : "Working"}</div></div></div></li>`);
    if (ai.error) html.push(`<li class="st byai in"><div class="phase"></div><div class="rail"><span class="dot"></span></div><div class="body"><div class="station"><div class="q">It stopped early</div><div class="saw">${esc(ai.error)}</div></div></div></li>`);
  } else if (rec.analysis) {
    const f = frameIndex("ai"), on = f <= S.pos;
    html.push(row({key: "ai", phase: "AI", cls: classes(f, animate, "byai"), q: `The AI read the whole run`, ans: on ? `<span class="ans violet">${esc(words(rec.analysis.assessment || "done"))}</span>` : "", saw: on ? esc(rec.analysis.summary) : ""}));
  }
  $("#map").innerHTML = html.join("");
  $("#map").lastElementChild.classList.add("last");
  $$("#map [data-open]").forEach(b => b.onclick = () => { stopReplay(); openDrawer(b.dataset.open); });
  $$("#map [data-fold]").forEach(b => b.onclick = () => { S.showReads = true; draw(); });
  drawTicks();
  updateControls();
}
function frameLabel(f) {
  const rec = S.rec;
  if (f.type === "trap") return ticketed(rec.event) ? "The ticket comes in" : swept(rec.event) ? "The heartbeat finds it" : "The trap arrives";
  if (f.type === "result") return "The answer";
  if (f.type === "fix") return `Repair: ${fixSteps(rec)[f.k].ask}`;
  if (f.type === "memory") return "Memory search";
  if (f.type === "ai") return "The AI reads the run";
  if (f.type === "handover") return "The AI takes over";
  if (f.type === "tried") return `Also tried: ${f.t.title || f.t.rule}`;
  if (f.type === "call") return `AI: ${callView(aiCalls(rec)[f.k]).q}`;
  return `Check ${f.i + 1}: ${rec.steps[f.k].ask}`;
}
function drawTicks() {
  $("#ticks").innerHTML = S.frames.map((f, n) => {
    const c = f.type === "call" ? aiCalls(S.rec)[f.k] : null, tone = c && c.outcome === "kept" ? "kept" : c && c.outcome === "reverted" ? "bad" : (["call", "handover", "ai"].includes(f.type) ? "byai" : "");
    return `<button type="button" class="tick ${tone} ${n <= S.pos ? "done" : ""} ${n === S.pos ? "now" : ""}" data-n="${n}" title="${esc(frameLabel(f))}" aria-label="${esc(frameLabel(f))}" ${n === S.pos ? 'aria-current="step"' : ""}><i></i></button>`;
  }).join("");
  $$("#ticks .tick").forEach(t => t.onclick = () => { stopReplay(); go(+t.dataset.n, true); });
}
function updateControls() {
  const p = $("#play");
  if (!p) return;
  const atEnd = S.pos >= lastFrame();
  p.innerHTML = S.playing
    ? `<svg viewBox="0 0 14 14" aria-hidden="true"><rect x="2" y="1" width="3.5" height="12" fill="currentColor"/><rect x="8.5" y="1" width="3.5" height="12" fill="currentColor"/></svg>Pause`
    : `<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M3 1.5v11l9.5-5.5z" fill="currentColor"/></svg>${atEnd ? "Replay" : "Play"}`;
  $("#prev").disabled = S.pos <= 0;
  $("#next").disabled = atEnd;
}
function go(n, user = false) {
  n = Math.max(0, Math.min(lastFrame(), n));
  S.pos = n;
  if (S.frames[n].type === "call") S.showReads = true;
  draw(true);
  const f = S.frames[n], key = keyOf(f);
  $("#live").textContent = frameLabel(f);
  if (S.follow || user) openDrawer(key, true);
  const target = $(`#map [data-open="${key}"]`);
  if (target) target.scrollIntoView({block: "center", behavior: reduced ? "auto" : "smooth"});
}
function schedule() {
  S.timer = setTimeout(() => {
    if (!S.playing) return;
    if (S.pos >= lastFrame()) { stopReplay(); return; }
    go(S.pos + 1);
    if (S.pos >= lastFrame()) stopReplay(); else schedule();
  }, 1500 / S.speed);
}
function play() {
  if (S.pos >= lastFrame()) { S.pos = 0; go(0); }
  S.playing = true;
  updateControls();
  schedule();
}
function stopReplay() { S.playing = false; clearTimeout(S.timer); updateControls(); }

/* ---------- side panel ---------- */
function openDrawer(key, quiet = false, html) {
  S.drawer = key;
  const d = $("#drawer");
  d.innerHTML = `<div class="dbody">${html ?? drawerBody(key)}</div>`;
  d.hidden = false;
  document.body.classList.add("drawer-open");
  requestAnimationFrame(() => { d.classList.add("open"); $("#scrim").classList.add("open"); });
  $(".x", d).onclick = () => closeDrawer();
  $$("[data-frame]", d).forEach(b => b.onclick = () => { stopReplay(); go(+b.dataset.frame, true); });
  $$("#map [aria-expanded]").forEach(el => el.setAttribute("aria-expanded", el.dataset.open === key));
  d.scrollTop = 0;
  if (!quiet) { const h = $("#d-title", d); if (h) { h.tabIndex = -1; h.focus({preventScroll: true}); } }
}
function closeDrawer(silent = false) {
  if (silent && !S.drawer) return;
  S.drawer = null;
  const d = $("#drawer");
  d.classList.remove("open");
  $("#scrim").classList.remove("open");
  document.body.classList.remove("drawer-open");
  $$("#map [aria-expanded]").forEach(el => el.setAttribute("aria-expanded", "false"));
  setTimeout(() => { if (!S.drawer) d.hidden = true; }, 240);
}
$("#scrim").onclick = () => closeDrawer();
const X = `<button class="x" type="button" aria-label="Close"><svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true"><path d="M2 2l10 10M12 2L2 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>`;
const top_ = text => `<div class="dtop"><span>${text}</span>${X}</div>`;
function navButtons(n) {
  if (!S.rec || n === undefined || n < 0) return "";
  return `<div class="dnav">${n > 0 ? `<button class="btn small" type="button" data-frame="${n - 1}">Previous step</button>` : "<span></span>"}${n < lastFrame() ? `<button class="btn small" type="button" data-frame="${n + 1}">Next step</button>` : ""}</div>`;
}
function drawerBody(key) {
  if (key === "trap") return trapBody();
  if (key === "result") return resultBody();
  if (key === "memory") return memoryBody();
  if (key === "ai") return aiBody();
  if (key === "handover") return handoverBody();
  let m = key.match(/^fix-(\d+)$/); if (m) return fixBody(+m[1]);
  m = key.match(/^step-(\d+)$/); if (m) return stepBody(+m[1]);
  m = key.match(/^call-(\d+)$/); if (m) return callBody(+m[1]);
  m = key.match(/^tried-(\d+)$/); if (m) return triedBody(+m[1]);
  m = key.match(/^exit-(\d+)-(.+)$/); if (m) return exitBody(S.rule.steps[+m[1]].exits[m[2]], m[2], +m[1]);
  return top_("") + "";
}
function trapBody() {
  const ev = S.rec.event, link = isLinkDown(ev);
  const facts = link ? `<dt>Port</dt><dd>${esc(ev.interface)}</dd>${ev.description ? `<dt>Link name</dt><dd>${esc(ev.description)}</dd>` : ""}<dt>Admin status</dt><dd>${esc(ev.admin_status)}${ev.admin_status === "down" ? ", so the port was disabled on purpose" : ""}</dd>`
    : EVENT_FACTS.filter(([k]) => ev[k]).map(([k, label]) => `<dt>${esc(label)}</dt><dd>${esc(ev[k])}</dd>`).join("");
  return `${top_("Where it started")}<h2 id="d-title">${esc(link ? `A linkDown trap arrived from ${ev.device}` : evTrigger(ev))}</h2><p class="about">${esc(aboutEvent(ev))}</p>
    <h3>${ticketed(ev) ? "What the person wrote" : swept(ev) ? "What the heartbeat saw" : "What the trap said"}</h3>${link ? "" : `<p>${esc(ev.detail || "")}</p>`}
    <dl class="facts" style="margin-top:8px">${ev.ticket ? `<dt>Ticket</dt><dd>${ticketLink(ev.ticket)}</dd>` : ""}<dt>Device</dt><dd>${esc(ev.device)}</dd>${facts}<dt>${swept(ev) ? "Seen" : "Received"}</dt><dd>${esc(dfmt(ev.time))}, ${esc(tfmt(ev.time))}</dd></dl>
    ${swept(ev) || !ev.raw ? "" : `<h3>As written to the trap log</h3><pre>${esc(ev.raw.replace(/\t/g, "\n"))}</pre>`}${navButtons(0)}`;
}
function evidenceBlock(e) {
  const label = {device: "Command on the device", prometheus: "Prometheus query", file: "Inventory record", docker: "Command"}[e.kind] || e.kind;
  let body = `<pre class="cmd">${esc(e.ran)}</pre>`;
  if (e.series && e.series.length) body += chart(e); else if (e.output) body += `<pre>${esc(e.output)}</pre>`;
  if (e.note) body += `<p class="note">${esc(e.note)}</p>`;
  return `<div class="ev"><div class="lbl">${esc(label)}</div>${body}</div>`;
}
function chart(e) {
  const pts = e.series, trap = S.rec.event.time, W = 440, H = 132, L = 52, R = 10, T = 28, B = 24;
  const xs = pts.map(p => p[0]).concat([trap]), ys = pts.map(p => p[1]), x0 = Math.min(...xs), x1 = Math.max(...xs), status = /ifOperStatus/.test(e.note || "");
  let y0 = status ? 1 : Math.min(...ys), y1 = status ? 2 : Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const X = t => L + (W - L - R) * ((t - x0) / Math.max(1, x1 - x0));
  const Y = v => status ? (v >= 2 ? H - B : T) : H - B - (H - B - T) * ((v - y0) / (y1 - y0));
  const d = pts.map((p, n) => n === 0 ? `M${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}` : `H${X(p[0]).toFixed(1)}V${Y(p[1]).toFixed(1)}`).join("");
  const dots = pts.map(p => `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="3.2" fill="${status && p[1] >= 2 ? "#e5534b" : "#ebe9e4"}"/>`).join("");
  const yl = status ? [[T, "up"], [H - B, "down"]] : [[T, Math.round(y1).toLocaleString()], [H - B, Math.round(y0).toLocaleString()]];
  const tx = X(trap).toFixed(1), txt = (x, y, s, extra = "") => `<text x="${x}" y="${y}" font-size="11" fill="#98978f" font-family="Overpass, sans-serif" ${extra}>${esc(s)}</text>`;
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(pts.length)} samples around the trap">
    ${yl.map(([y, t]) => txt(L - 8, y + 4, t, 'text-anchor="end"') + `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="rgba(235,233,228,.16)" stroke-dasharray="2 4"/>`).join("")}
    <line x1="${tx}" x2="${tx}" y1="2" y2="${H - B + 4}" stroke="#f2a93b" stroke-width="2"/><text x="${+tx + 5}" y="11" font-size="11" fill="#f2a93b" font-family="Overpass, sans-serif" font-weight="600">trap</text>
    <path d="${d}" fill="none" stroke="#ebe9e4" stroke-width="2"/>${dots}${txt(L, H - 5, tfmt(x0))}${txt(W - R, H - 5, tfmt(x1), 'text-anchor="end"')}</svg></div>`;
}
function possibleAnswers(s) {
  const rows = Object.entries(s.exits || {}).map(([name, e]) => `<div class="decision"><div>If <b>${esc(words(name))}</b>: ${tpl(e.text || e.label)}</div></div>`).join("");
  const cont = (s.continue_on || []).length ? `<div class="decision"><div>If ${s.continue_on.map(x => `<b>${esc(words(x))}</b>`).join(" or ")}: move on to the next check.</div></div>` : "";
  return `<h3>What each answer leads to</h3>${rows}${cont}<h3>The check in the rule file</h3><pre class="cmd">${esc(s.check)}</pre>`;
}
function stepBody(i) {
  const s = S.rule.steps[i], key = "step-" + i, f = frameIndex(key), rec = f >= 0 ? S.rec.steps[S.frames[f].k] : null;
  const head = `${top_(`Check ${i + 1} of ${S.rule.steps.length}, ${esc(s.phase)}`)}<h2 id="d-title">${tpl(rec ? rec.ask : s.ask)}</h2><p class="about">${esc(s.description)}.</p>`;
  if (!rec) return head + `<p class="note">This run never got this far: an earlier check already had the answer.</p>` + possibleAnswers(s);
  const n = rec.next;
  const after = n.kind === "next" ? `The answer was <b>${esc(words(rec.outcome))}</b>, so it moved on to check ${i + 2}.` : `The answer was <b>${esc(words(rec.outcome))}</b>, so it stopped here. ${esc(n.text || n.label)}`;
  return head + `<h3>What it found</h3><p>${esc(rec.saw)}</p><h3>What it decided</h3><div class="decision"><div>${after}</div></div>
    <h3>What it ran</h3>${(rec.evidence || []).map(evidenceBlock).join("") || `<p class="muted">Nothing outside the rule itself.</p>`}
    <p class="note">Ran at ${esc(tfmt(rec.started))} and took ${rec.ms} ms.</p><details class="more"><summary>What each answer leads to</summary>${possibleAnswers(s)}</details>${navButtons(f)}`;
}
function fixBody(k) {
  const s = fixSteps(S.rec)[k], f = frameIndex("fix-" + k), what = {do: "The repair", verify: "Checking the repair", undo: "Undoing the repair"}[s.kind] || "Repair";
  return `${top_(esc(what))}<h2 id="d-title">${esc(s.ask)}</h2><h3>What happened</h3><p>${esc(s.saw)}</p><h3>What it ran</h3>${(s.evidence || []).map(evidenceBlock).join("") || `<p class="muted">Nothing outside the rule itself.</p>`}${s.started ? `<p class="note">Ran at ${esc(tfmt(s.started))}.</p>` : ""}${navButtons(f)}`;
}
function exitBody(e, name, i) {
  const k = KIND[e.kind] || {};
  return `${top_(`A way out of check ${i + 1}`)}<h2 id="d-title">${esc(k.title || e.kind)} if the answer is ${esc(words(name))}</h2><div class="decision" style="margin-top:10px"><div>${tpl(e.text || e.label)}</div></div>
    ${e.kind === "cause" ? `<p class="note">Cause label <code>${esc(e.label || e.target)}</code></p>` : ""}<p class="note">This run did not take this way out.</p>`;
}
function resultBody() {
  const rec = S.rec, r = rec.result, [title, text] = verdictWords(r);
  const skip = new Set(["raw", "time", "trap_time", "event", "source", "severity", "role", "platform", "title", "trigger", "detail", "subject", "merge_key", "change_running", "merged", "link_run"]);
  const facts = Object.entries(rec.values || {}).filter(([k, v]) => !skip.has(k) && v !== "").map(([k, v]) => `<dt>${esc(words(k))}</dt><dd>${esc(v)}</dd>`).join("");
  return `${top_(`The answer, ${r.step === "end" ? "after every check" : `at check ${esc(r.step)}`}`)}<h2 id="d-title">${esc(title)}</h2><div class="decision" style="margin-top:10px"><div>${esc(text)}</div></div>
    ${r.kind === "cause" ? `<p class="note">Cause label <code>${esc(r.label)}</code>.</p>` : ""}
    <p class="note">The investigation only read from the network. It took ${((rec.finished - rec.started) || 0).toFixed(1)} s.</p>
    ${rec.values && rec.values.link_run ? `<p class="note"><a href="#/run/${esc(rec.values.link_run)}">Open the link-down run</a></p>` : ""}
    ${facts ? `<h3>Everything it learned</h3><dl class="facts">${facts}</dl>` : ""}${navButtons(frameIndex("result"))}`;
}
function triedBody(n) {
  const t = S.rec.tried[n], [title, text] = verdictWords(t.result);
  return `${top_(`Another rule, at the ${esc(t.position)}${t.mode === "shadow" ? ", watch only" : ""}`)}<h2 id="d-title">${esc(t.title || t.rule)}</h2><div class="decision" style="margin-top:10px"><div><b>${esc(title)}.</b> ${esc(text)}</div></div>
    ${t.mode === "shadow" ? `<p class="note">This rule is in watch-only mode, so what it found changed nothing.</p>` : ""}
    <h3>What it checked</h3>${t.steps.map(s => `<div class="decision"><div><b>${esc(s.ask)}</b><br>${esc(words(s.outcome))}: ${esc(s.saw)}</div></div>`).join("") || `<p class="muted">Nothing.</p>`}`;
}
function memoryItem(m, used) {
  const name = m.url ? `<a href="${esc(m.url)}" rel="noreferrer">${esc(m.title)}</a>` : esc(m.title);
  return `<li><span class="muted">${esc(dfmt(m.time))}, ${esc(hm(m.time))}, ${esc(SOURCE_NAMES[m.source] || m.source)}${m.times_seen > 1 ? `, seen ${m.times_seen} times` : ""}</span>${name}${used.has(m.ref) ? ` <span class="ai">used</span>` : ""}</li>`;
}
function memoryBody() {
  const m = S.rec.retrieval, used = new Set((S.rec.analysis || {}).used || []), items = S.rec.retrieved || [];
  return `${top_("Memory search")}<h2 id="d-title">What has it seen like this before?</h2><p class="about">A keyword search over everything stored before ${esc(dfmt(m.before))}, ${esc(hm(m.before))}. No AI is involved in this step.</p>
    <h3>The search</h3><dl class="facts"><dt>Past runs held</dt><dd>${m.searched.runs}</dd><dt>Records held</dt><dd>${m.searched.docs}</dd><dt>Matched the words</dt><dd>${m.matched}</dd><dt>Kept as relevant</dt><dd>${items.length}</dd><dt>Took</dt><dd>${Math.round(m.seconds * 1000)} ms</dd></dl>
    <h3>What it looked for</h3><ul class="terms">${m.terms.map(t => `<li class="chip">${esc(t)}</li>`).join("")}</ul>
    <h3>What it found</h3>${items.length ? `<ul class="list">${items.map(i => memoryItem(i, used)).join("")}</ul>` : `<p class="muted">Nothing older was really about this.</p>`}${navButtons(frameIndex("memory"))}`;
}
function aiBody() {
  const a = S.rec.analysis, g = a.given, used = new Set(a.used || []), cited = (S.rec.retrieved || []).filter(m => used.has(m.ref)), runs = (S.rec.similar || []).filter(r => used.has(r.ref));
  return `${top_('<span class="ai">AI</span> Read the run, changed nothing')}<h2 id="d-title">The AI read this run</h2><p class="about">Everything before this step came from rules and checks on the devices.</p>
    <dl class="facts" style="margin-top:9px"><dt>Model</dt><dd>${esc(a.model)}</dd>${a.started ? `<dt>Started</dt><dd>${esc(tfmt(a.started))}</dd>` : ""}<dt>Took</dt><dd>${Math.round(a.seconds)} s</dd>${a.assessment ? `<dt>Its read</dt><dd>${esc(words(a.assessment))}</dd>` : ""}</dl>
    <h3>What it said</h3><p>${esc(a.summary)}</p>${a.why ? `<h3>Why</h3><p>${esc(a.why)}</p>` : ""}${a.next && a.next.toLowerCase() !== "nothing" ? `<h3>What it suggests</h3><p>${esc(a.next)}</p>` : ""}
    ${g ? `<h3>What it was given</h3><p>${g.checks} checks, the conclusion${g.repair ? ", the repair" : ""}, ${g.similar} similar past runs, ${g.memory} records from memory, and ${g.live} records from around that time.</p>` : ""}
    ${cited.length || runs.length ? `<h3>What it relied on</h3><ul class="list">${runs.map(r => `<li><a href="#/run/${esc(r.id)}">${esc(dfmt(r.time))}, ${esc(r.device)} ${esc(r.subject)}</a><span class="muted">${esc(r.text || r.result_kind)}</span></li>`).join("")}${cited.map(m => memoryItem(m, new Set())).join("")}</ul>` : ""}
    ${a.prompt ? `<details class="more"><summary>The exact text it was given</summary><pre>${esc(a.prompt)}</pre></details>` : ""}${a.reasoning ? `<details class="more"><summary>Its working</summary><pre>${esc(a.reasoning)}</pre></details>` : ""}${navButtons(frameIndex("ai"))}`;
}
function handoverBody() {
  const ai = S.rec.agent, ch = ai.changes || [];
  return `${top_('<span class="ai">AI</span> Where the rules handed over')}<h2 id="d-title">The AI took over here</h2><p class="about">${esc(ai.why || "")}</p>
    <dl class="facts" style="margin-top:9px"><dt>Model</dt><dd>${esc(ai.model || "")}</dd>${ai.started ? `<dt>Started</dt><dd>${esc(tfmt(ai.started))}</dd>` : ""}${ai.finished ? `<dt>Finished</dt><dd>${esc(tfmt(ai.finished))}</dd>` : ""}<dt>Steps</dt><dd>${aiCalls(S.rec).length}</dd><dt>Changes tried</dt><dd>${ch.length}</dd><dt>Suggestions</dt><dd>${(ai.proposals || []).length}</dd>${ai.outcome ? `<dt>Outcome</dt><dd>${esc(words(ai.outcome))}</dd>` : ""}</dl>
    ${ai.error ? `<h3>It stopped early</h3><p>${esc(ai.error)}</p>` : ""}
    ${ai.prompt ? `<details class="more"><summary>The exact text it started from</summary><pre>${esc(ai.prompt)}</pre></details>` : ""}${ai.reasoning ? `<details class="more"><summary>Its working</summary><pre>${esc(ai.reasoning)}</pre></details>` : ""}${navButtons(frameIndex("handover"))}`;
}
function changeFor(c) { return (S.rec.agent.changes || []).find(x => x.started >= c.started - .01 && x.started <= c.started + c.ms / 1000 + .01); }
function verifyText(v) { return v ? `${v.check}${Object.values(v.args || {}).length ? " " + Object.values(v.args).join(" ") : ""}, should answer ${(v.expect || []).join(" or ")}` : ""; }
function changeBlock(ch) {
  const list = a => (a || []).length ? a.map(esc).join(", ") : "none";
  const undo = (ch.steps || []).filter(s => s.kind === "undo");
  return `<h3>The change</h3><pre class="cmd">${esc((ch.what || []).join("\n"))}</pre>${ch.why ? `<p class="note">${esc(ch.why)}</p>` : ""}${ch.earlier_change ? `<h3>What it weighed about the earlier change</h3><p>${esc(ch.earlier_change)}</p>` : ""}
    <h3>How it was judged</h3><dl class="facts"><dt>Check</dt><dd>${esc(verifyText(ch.verify))}</dd>${ch.verify_before ? `<dt>Before</dt><dd>${esc(words(ch.verify_before))}</dd>` : ""}${ch.verify_after ? `<dt>After</dt><dd>${esc(words(ch.verify_after))}</dd>` : ""}
      ${ch.problems_before ? `<dt>Wrong before</dt><dd>${list(ch.problems_before)}</dd><dt>Cleared by it</dt><dd>${list(ch.healed)}</dd><dt>New problems</dt><dd>${list(ch.new)}</dd>` : ""}</dl>
    ${undo.length ? `<h3>How it was taken back out</h3>${undo.map(s => `<div class="decision"><div>${esc(s.saw || s.ask || "")}</div></div>`).join("")}` : ""}
    ${(ch.evidence || []).length ? `<h3>What it ran</h3>${ch.evidence.map(evidenceBlock).join("")}` : ""}`;
}
function callBody(k) {
  const calls = aiCalls(S.rec), c = calls[k], v = callView(c), ch = v.change ? changeFor(c) : null, a = c.args || {}, [w, tone] = CALL_WORDS[c.outcome] || [words(c.outcome), ""];
  const finish = c.tool === "finish" ? `${a.root_cause ? `<h3>Root cause</h3><p>${esc(a.root_cause)}</p>` : ""}${a.summary ? `<h3>Summary</h3><p>${esc(a.summary)}</p>` : ""}${a.why ? `<h3>Why</h3><p>${esc(a.why)}</p>` : ""}${a.next ? `<h3>Next</h3><p>${esc(a.next)}</p>` : ""}` : "";
  return `${top_(`<span class="ai">AI</span> Step ${k + 1} of ${calls.length}`)}<h2 id="d-title">${esc(v.q)}</h2>${w ? `<div class="outcome ${tone}">${esc(w)}</div>` : c.tool.startsWith("check_") ? `<div class="outcome">${esc(words(c.outcome))}</div>` : ""}
    ${a.why && c.tool !== "finish" && !ch ? `<h3>Why</h3><p>${esc(a.why)}</p>` : ""}
    ${c.reason ? `<h3>Why it ran this</h3><p>${esc(c.reason)}</p>` : ""}
    ${c.saw ? `<h3>What came back</h3><p>${esc(c.saw)}</p>` : ""}${ch ? changeBlock(ch) : ""}${finish}
    ${v.propose ? `<p class="note"><a href="#/review">Open the review page</a></p>` : ""}
    ${(c.ran || []).length && !ch ? `<h3>What it ran</h3>${c.ran.map(x => `<pre class="cmd">${esc(x)}</pre>`).join("")}` : ""}${c.output ? `<h3>Output</h3><pre>${esc(c.output)}</pre>` : ""}
    <details class="more"><summary>Exact arguments</summary><pre>${esc(JSON.stringify(a, null, 1))}</pre></details>
    <p class="note">Ran at ${esc(tfmt(c.started))} and took ${c.ms} ms.</p>${navButtons(frameIndex("call-" + k))}`;
}
function afterwards(rec) {
  const a = rec.analysis, p = rec.pattern, parts = [], used = new Set((a || {}).used || []);
  if (p && (p.note || p.count_7d > 1)) parts.push(`<section class="panel"><h2>${esc(VERDICTS[p.verdict] || "Has this happened before")}</h2><p>${esc(p.note || `${p.count_7d} times in the last 7 days.`)}</p></section>`);
  if ((rec.similar || []).length) parts.push(`<section class="panel"><h2>Runs that read like this one</h2><ul class="list">${rec.similar.map(x => `<li><a href="#/run/${esc(x.id)}">${esc(dfmt(x.time))}, ${esc(hm(x.time))}, ${esc(x.device)} ${esc(x.subject)}</a><span class="muted">${esc(x.text || x.result_kind)}</span></li>`).join("")}</ul></section>`);
  if ((rec.retrieved || []).length) parts.push(`<section class="panel"><h2>Found in memory</h2><ul class="list">${rec.retrieved.map(m => memoryItem(m, used)).join("")}</ul></section>`);
  if ((rec.context || []).length) parts.push(`<section class="panel"><h2>What else was going on</h2><ul class="list">${rec.context.slice(-20).map(d => `<li><span class="muted">${esc(hm(d.time))}, ${esc(SOURCE_NAMES[d.source] || d.source)}</span>${d.url ? `<a href="${esc(d.url)}" rel="noreferrer">${esc(d.title)}</a>` : esc(d.title)}</li>`).join("")}</ul></section>`);
  return parts.length ? `<div class="more-sec">${parts.join("")}</div>` : "";
}

/* ---------- review ---------- */
const POSITIONS = {beginning: "At the start, before the usual checks", main: "As the main rule", end: "At the end, only if the usual checks run out"};
const KIND_WORDS = {rule: "Rule", tag: "New tag", backdate: "Tags on past runs"};
async function pageReview(q) {
  setNav("review");
  naming("Review");
  const status = q.get("status") || "pending", kind = q.get("kind") || "", page = q.get("page") || "1";
  const [d, rules] = await Promise.all([api(`api/review?status=${status}&kind=${kind}&page=${page}&size=12`), S.rules || api("api/rules").catch(() => ({rules: []}))]);
  S.rules = rules; S.installed = new Set(rules.rules.map(r => r.id));
  const c = d.counts, tab = (key, label, n) => `<a href="#/review?status=${key}${kind ? "&kind=" + kind : ""}" ${status === key ? 'aria-current="page"' : ""}>${label} <i>${n}</i></a>`;
  const kinds = [["", "Everything"], ["rule", "Rules"], ["monitor", "Monitors"], ["tag", "New tags"], ["backdate", "Past runs"]];
  const pages = Math.max(1, Math.ceil(d.total / d.size));
  $("#main").innerHTML = `<div class="wrap" style="max-width:940px">
    <div class="head"><h1>Review</h1><p>What the AI suggested. Nothing here takes effect until you accept it.</p></div>
    <div class="seg">${tab("pending", "Waiting", c.pending)}${tab("accepted", "Accepted", c.accepted)}${tab("rejected", "Rejected", c.rejected)}</div>
    <div class="seg">${kinds.map(([k, l]) => `<a href="#/review?status=${status}${k ? "&kind=" + k : ""}" ${kind === k ? 'aria-current="page"' : ""}>${l}</a>`).join("")}</div>
    ${d.items.length ? d.items.map(card).join("") : `<div class="panel empty">${esc(reviewEmpty(status, kind))}</div>`}
    ${pages > 1 ? `<div class="pager"><span class="count">Page ${d.page} of ${pages}</span>${d.page > 1 ? `<a class="btn small" href="#/review?status=${status}&kind=${kind}&page=${d.page - 1}">Previous</a>` : ""}${d.page < pages ? `<a class="btn small" href="#/review?status=${status}&kind=${kind}&page=${d.page + 1}">Next</a>` : ""}</div>` : ""}
  </div>`;
  S.reviewItems = Object.fromEntries(d.items.map(p => [p.id, p]));
  $$(".prop[data-id]").forEach(wireCard);
  $$("[data-preview]").forEach(el => drawPreview(el, `api/monitors/preview?proposal=${el.dataset.preview}`));
}
function reviewEmpty(status, kind) {
  const what = {rule: "rule", monitor: "monitor", tag: "new tag", backdate: "past-run tag"}[kind];
  if (status === "pending") return what ? `No ${what} suggestions are waiting.` : "Nothing is waiting. When the AI works out something the rules could not, its suggestion appears here.";
  return `No ${what ? what + " " : ""}suggestions ${status} yet.`;
}
function thenText(raw, causes) {
  const m = String(raw).match(/^(cause|stop|escalate|jump|next)\b\s*(.*)$/);
  if (!m) return esc(raw);
  if (m[1] === "cause") { const label = m[2].split(/\s+/)[0]; return `Cause found: ${tpl(causes[label] || label)}`; }
  if (m[1] === "stop") return `Stop. ${tpl(m[2])}`;
  if (m[1] === "escalate") return `Hand to a person. ${tpl(m[2])}`;
  return "Move on.";
}
const inFix = s => tpl(String(s ?? "").replace(/\{device\}/g, "{found_device}"));  // in a fix, {device} is where the cause was found
function fixText(fix) {
  const d = fix.action.do, a = d.args || {};
  if (d.name === "config_lines") return `<p>On <b>${inFix(a.device)}</b>, add this config. Its undo is worked out from a snapshot taken first.</p><pre>${esc((a.lines || []).join("\n"))}</pre>`;
  if (d.name === "linux_change") return `<p>On <b>${inFix(a.device)}</b>, run:</p><pre>${esc((a.argv || []).join(" "))}</pre><p class="note">If it does not help, it runs this to undo it:</p><pre>${esc((a.undo_argv || []).join(" "))}</pre>`;
  return `<p>Run the built-in action <b>${esc(words(d.name))}</b>${Object.keys(a).length ? ` with ${esc(JSON.stringify(a))}` : ""}.</p>`;
}
const when = p => `<time>${esc(dfmt(p.created))}, ${esc(hm(p.created))}</time>`;
const runLink = id => id ? `<a href="#/run/${esc(id)}">Open the run</a>` : "none";
const jenkinsFolder = () => (S.rules && S.rules.jenkins && S.rules.jenkins.folder) || "troubleshooting-pipelines";
const pipelineUrl = name => S.rules && S.rules.jenkins ? `${S.rules.jenkins.url}job/${encodeURIComponent(name)}/` : "";
function proposedPipelines(p) {
  const names = p.body.rules.map(r => r.pipeline || r.id);
  if (p.status === "accepted") return p.body.rules.map((r, i) => pipelineUrl(names[i]) && S.installed && S.installed.has(r.id) ? `<a href="${esc(pipelineUrl(names[i]))}" target="_blank" rel="noreferrer">${esc(names[i])} &#8599;</a>` : esc(names[i])).join(", ");
  return `${names.map(n => `<code>${esc(jenkinsFolder())}/${esc(n)}</code>`).join(" and ")}${p.status === "pending" ? `<span class="muted">, made when you accept</span>` : ""}`;
}
function factRow(items) { return `<dl class="factrow">${items.filter(Boolean).map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${v}</dd></div>`).join("")}</dl>`; }
function runChecks(p) {
  const rows = p.body.runs || [];
  if (!rows.length) return "";
  const pending = p.status === "pending";
  return `<h3>Past runs it would go on</h3><ul class="pruns">${rows.map(r => `<li><label>${pending ? `<input type="checkbox" data-run-pick="${esc(r.run_id)}" checked>` : ""}<span><a href="#/run/${esc(r.run_id)}">${esc(r.run_id)}</a> <span class="muted">${esc(r.why)}</span></span></label></li>`).join("")}</ul>`;
}
function ruling(p) {
  if (p.status === "rejected") return `<div class="ruling rejected"><b>Rejected ${esc(ago(p.decided))}.</b> ${esc(p.reason)} <span class="muted">The AI is shown this reason whenever it works on something similar.</span></div>`;
  if (p.status === "accepted" && p.kind === "monitor") return `<div class="ruling accepted"><b>Accepted ${esc(ago(p.decided))}.</b> Monitor <a href="#/monitors">${esc(p.proof.monitor || "")}</a> is watching.</div>`;
  if (p.status === "accepted") return `<div class="ruling accepted"><b>Accepted ${esc(ago(p.decided))}.</b> ${p.proof.mode ? `Installed ${p.proof.mode === "active" ? "live" : "watch only"}.` : `Put on ${plural(p.proof.tagged || 0, "run")}.`}</div>`;
  return "";
}
function card(p) {
  let inner = "";
  if (p.kind === "rule") {
    const [d, fix] = p.body.rules, tags = (p.body.trigger || {}).tags || d.tags || [], replay = p.proof.replay || {};
    inner = factRow([["Runs for", `<div class="chips">${tags.map(t => `<span class="chip">${esc(t)}</span>`).join("")}</div>`], ["Where", esc(POSITIONS[p.body.position] || p.body.position)], ["Learned from", runLink(p.run_id)], ["Suggested by", esc(p.model || "AI")], ["Jenkins pipeline", proposedPipelines(p)]]) +
      `<h3>What it checks, in order</h3><ol class="story">${d.steps.map((s, n) => `<li><span class="n">${n + 1}.</span><div>${tpl(s.ask)} <code>${esc(s.check.name === "output_has" ? `${s.check.args.command} on ${s.check.args.device}, looking for "${s.check.args.text}"` : `${s.check.name} ${Object.values(s.check.args).join(" ")}`)}</code>${Object.entries(s.then || {}).map(([k, v]) => `<div class="then">If ${esc(words(k))}: ${thenText(v, d.causes || {})}</div>`).join("")}</div></li>`).join("")}</ol>` +
      (fix ? `<h3>What it would change: ${esc(fix.title)}</h3>${fixText(fix)}<p class="note">Risk ${esc(fix.risk)}. Afterwards it must pass: ${inFix((fix.verify || []).map(v => `${v.check.name} ${Object.values(v.check.args).join(" ")} = ${v.expect.join(" or ")}`).join("; "))}.</p>` : `<h3>What it would change</h3><p class="muted">Nothing. It only names the cause.</p>`) +
      (replay.ok ? `<div class="proof"><span class="led green"></span><div><b>Tested on what the AI saw.</b> Walked ${plural((replay.path || []).length, "step")} and reached ${esc(replay.reached)}.</div></div>` : `<div class="proof none"><span class="led off"></span><div>No replay on record.</div></div>`);
  } else if (p.kind === "monitor") {
    inner = monitorProposal(p);
  } else if (p.kind === "tag") {
    const b = p.body;
    inner = factRow([["Tag", `<span class="chip">${esc(b.tag)}</span>`], ["What it means", esc(b.description)], ["Learned from", runLink(p.run_id)], ["Suggested by", esc(p.model || "AI")]]) + runChecks(p);
  } else {
    inner = factRow([["Tag", `<span class="chip">${esc(p.body.tag)}</span>`], ["Learned from", runLink(p.run_id)], ["Suggested by", esc(p.model || "AI")]]) + runChecks(p);
  }
  const hasFix = p.kind === "rule" && p.body.rules[1];
  const dup = p.status === "pending" && p.kind === "rule" && p.body.rules.some(r => S.installed && S.installed.has(r.id));
  const actions = p.status !== "pending" ? ruling(p) : dup ? `<div class="decide" data-decide><p class="warn">A rule with this name is already installed, so this one cannot be added.</p><button class="btn go" type="button" data-act="dup">Reject as a duplicate</button></div>` : `<div class="decide" data-decide>
    ${p.kind === "rule" ? `<button class="btn go" type="button" data-act="shadow">Accept, watch first</button><button class="btn" type="button" data-act="${hasFix ? "ask-live" : "active"}">Accept, go live</button>` : `<button class="btn go" type="button" data-act="accept">Accept</button>`}
    <button class="btn" type="button" data-act="ask-reject">Reject</button></div>`;
  return `<article class="prop" data-id="${p.id}"><header><span class="ai">AI</span><h2>${esc(p.title)}</h2>${when(p)}</header>${p.why ? `<p class="why">${esc(p.why)}</p>` : ""}${inner}${actions}</article>`;
}
function wireCard(el) {
  const id = +el.dataset.id, box = $("[data-decide]", el);
  if (!box) return;
  const original = box.innerHTML;
  const decided = async (fn, ok) => {
    $$("button", box).forEach(b => { b.disabled = true; });
    try {
      await fn();
      toast(ok);
      S.tags = null; S.rules = null;
      refreshNow();
      route();
    } catch (e) { toast(e.message, true); $(".warn", box)?.remove(); box.insertAdjacentHTML("afterbegin", `<p class="warn">${esc(e.message)}</p>`); $$("button", box).forEach(b => { b.disabled = false; }); }
  };
  const runs = () => { const picks = $$("[data-run-pick]", el); return picks.length ? picks.filter(c => c.checked).map(c => c.dataset.runPick) : undefined; };
  const wire = () => $$("[data-act]", box).forEach(b => b.onclick = () => {
    const a = b.dataset.act;
    if (a === "dup") decided(() => post(`api/review/${id}/reject`, {reason: "Duplicate: a rule with this name is already installed."}), "Rejected as a duplicate.");
    if (a === "shadow") decided(() => post(`api/review/${id}/accept`, {mode: "shadow"}), "Accepted. It will run and record, but decide nothing yet.");
    if (a === "active") decided(() => post(`api/review/${id}/accept`, {mode: "active"}), "Accepted and live.");
    if (a === "accept") decided(() => post(`api/review/${id}/accept`, {runs: runs()}), "Accepted.");
    if (a === "ask-live") {
      box.innerHTML = `<p class="warn">Once live, this rule can change the network by itself whenever it finds this cause.</p><button class="btn danger" type="button" data-act="active">Go live</button><button class="btn" type="button" data-act="back">Go back</button>`;
      wire();
    }
    if (a === "back") { box.innerHTML = original; wire(); }
    if (a === "ask-reject") {
      box.innerHTML = `<div class="rejectbox"><label for="why-${id}">Why not? The AI reads this before it suggests anything similar.</label><textarea id="why-${id}"></textarea><div class="acts"><button class="btn danger" type="button" data-act="reject">Reject</button><button class="btn" type="button" data-act="back">Go back</button></div></div>`;
      wire();
      $(`#why-${id}`).focus();
    }
    if (a === "reject") {
      const reason = $(`#why-${id}`).value.trim();
      if (reason.length < 4) { toast("Say why, so it is not suggested again.", true); return; }
      decided(() => post(`api/review/${id}/reject`, {reason}), "Rejected. The AI will see your reason.");
    }
  });
  wire();
}

/* ---------- rules ---------- */
const MODE_WORDS = {active: "Live", shadow: "Watch only", proposed: "Proposed", retired: "Retired"};
const WHERE_WORDS = {beginning: "Start", main: "Main", end: "End"};
async function pageRules() {
  setNav("rules");
  naming("Rules");
  S.rules = await api("api/rules");
  S.ruleWant = {};
  S.confirm = null;
  drawRules();
  if (S.openRule) { openDrawer("rule-" + S.openRule, false, ruleBody(S.openRule)); S.openRule = null; }
}
function drawRules() {
  const d = S.rules, w = S.ruleWant;
  const rules = d.rules.filter(r => r.table !== "filter");
  const kept = rules.filter(r => (!w.kind || r.table === w.kind) && (!w.mode || r.mode === w.mode) && (!w.from || (r.source.by === "ai") === (w.from === "ai")) && (!w.where || r.position === w.where) &&
    (!w.q || `${r.title} ${r.id} ${r.tags.join(" ")} ${r.description}`.toLowerCase().includes(w.q)));
  const area = r => (r.tags[0] || "other/other").split("/")[0], areas = [...new Set(rules.map(area))].sort();
  const seg = (key, all, opts) => `<div class="seg" role="group" style="margin:0"><button type="button" data-w="${key}" data-v="" aria-pressed="${!w[key]}">${all}</button>${opts.map(([v, l]) => `<button type="button" data-w="${key}" data-v="${v}" aria-pressed="${w[key] === v}">${l}</button>`).join("")}</div>`;
  const body = areas.map(a => {
    const list = kept.filter(r => area(r) === a);
    if (!list.length) return "";
    return `<tbody><tr><td colspan="6" class="group-cell"><div class="group-h">${esc(a[0].toUpperCase() + a.slice(1))} <small>${plural(list.length, "rule")}</small></div></td></tr>${list.map(ruleRow).join("")}</tbody>`;
  }).join("");
  const box = $("#r-q"), had = box && document.activeElement === box, value = box ? box.value : "", pos = box ? box.selectionStart : 0;
  $("#main").innerHTML = `<div class="wrap">
    <div class="head"><h1>Rules</h1><p>${plural(rules.length, "rule")}. Beginning rules run first, then one main rule, then end rules. If they run out, the AI takes over.</p></div>
    <div class="fbar"><input type="search" id="r-q" placeholder="Search rules" aria-label="Search rules">${seg("kind", "All kinds", [["diagnose", "Finds the cause"], ["fix", "Repairs"]])}${seg("mode", "Any mode", [["active", "Live"], ["shadow", "Watch only"], ["retired", "Retired"]])}${seg("from", "Anyone", [["ai", "From the AI"]])}</div>
    ${(d.load_errors || []).length ? `<div class="err" style="margin-bottom:10px">${d.load_errors.length} rule files could not be loaded: ${esc(JSON.stringify(d.load_errors).slice(0, 300))}</div>` : ""}
    <div class="tblwrap"><table class="tbl rules-tbl"><thead><tr><th>Rule</th><th>Kind</th><th>Runs for</th><th>Where</th><th>Mode</th><th>From</th></tr></thead>${body || `<tbody><tr><td colspan="6"><div class="empty">No rules match.</div></td></tr></tbody>`}</table></div></div>`;
  if (had) { const n = $("#r-q"); n.value = value; n.focus(); n.setSelectionRange(pos, pos); } else if (value) $("#r-q").value = value;
  $("#r-q").oninput = e => { S.ruleWant.q = e.target.value.trim().toLowerCase(); drawRules(); };
  $$("[data-w]").forEach(b => b.onclick = () => { S.ruleWant[b.dataset.w] = b.dataset.v; drawRules(); });
  $$("[data-rule]").forEach(b => b.onclick = () => openDrawer("rule-" + b.dataset.rule, false, ruleBody(b.dataset.rule)));
  $$("[data-mode]").forEach(s => s.onchange = () => askMode(s.dataset.mode, s.value));
  $$("[data-go]").forEach(b => b.onclick = () => setMode(b.dataset.go, b.dataset.to));
  $$("[data-stay]").forEach(b => b.onclick = () => { S.confirm = null; drawRules(); });
}
function ruleRow(r) {
  const issue = (r.issues || []).find(i => i.level === "error");
  const ask = S.confirm && S.confirm.id === r.id;
  return `<tr><td><button class="linkish" type="button" data-rule="${esc(r.id)}">${esc(r.title || r.id)}</button><span class="rid">${esc(r.id)}</span>${issue ? `<span class="rid" style="color:var(--red)">${esc(issue.message)}</span>` : ""}</td>
    <td>${r.table === "fix" ? "Repairs" : "Finds the cause"}</td><td><div class="chips">${r.tags.map(t => `<span class="chip">${esc(t)}</span>`).join("")}</div></td>
    <td>${esc(WHERE_WORDS[r.position] || r.position)}</td>
    <td><select data-mode="${esc(r.id)}" aria-label="Mode of ${esc(r.title || r.id)}">${["active", "shadow", "retired", ...(r.mode === "proposed" ? ["proposed"] : [])].map(m => `<option value="${m}" ${(ask ? S.confirm.to : r.mode) === m ? "selected" : ""}>${MODE_WORDS[m]}</option>`).join("")}</select></td>
    <td>${r.source.by === "ai" ? '<span class="ai">AI</span>' : "Built in"}</td></tr>
    ${ask ? `<tr class="confirmrow"><td colspan="6">${esc(S.confirm.say)} <span style="white-space:nowrap;margin-left:8px"><button class="btn small danger" type="button" data-go="${esc(r.id)}" data-to="${esc(S.confirm.to)}">${esc(S.confirm.label)}</button> <button class="btn small" type="button" data-stay="1">Keep it as it is</button></span></td></tr>` : ""}`;
}
function askMode(id, to) {
  const r = S.rules.rules.find(x => x.id === id);
  if (to === "active" && r.table === "fix") { S.confirm = {id, to, label: "Go live", say: "Once live, this rule changes the network by itself whenever it finds this cause."}; return drawRules(); }
  if (to === "retired") { S.confirm = {id, to, label: "Retire", say: "A retired rule stops running."}; return drawRules(); }
  setMode(id, to);
}
async function setMode(id, to) {
  try {
    await post(`api/rules/${encodeURIComponent(id)}/mode`, {mode: to});
    toast(`Now ${MODE_WORDS[to].toLowerCase()}.`);
  } catch (e) { toast(e.message, true); }
  S.confirm = null;
  S.rules = await api("api/rules");
  drawRules();
}
function ruleBody(id) {
  const r = S.rules.rules.find(x => x.id === id);
  const head = `${top_(`${r.table === "fix" ? "Repairs" : "Finds the cause"}, ${esc(MODE_WORDS[r.mode] || r.mode)}`)}<h2 id="d-title">${esc(r.title || r.id)}</h2><p class="about">${esc(r.description)}</p>
    <dl class="facts" style="margin-top:9px"><dt>Runs for</dt><dd>${esc(r.tags.join(", ") || r.match_text)}</dd><dt>Trigger</dt><dd>${esc(r.match_text)}</dd><dt>Where</dt><dd>${esc(POSITIONS[r.position] || r.position)}</dd><dt>From</dt><dd>${r.source.by === "ai" ? `The AI, from <a href="#/run/${esc(r.source.incident)}">this run</a>${r.source.approved_by ? `, approved by ${esc(r.source.approved_by)}` : ""}` : "Built in"}</dd>${r.pipeline ? `<dt>Jenkins</dt><dd>${r.pipeline_url ? `<a href="${esc(r.pipeline_url)}" target="_blank" rel="noreferrer">${esc(jenkinsFolder())}/${esc(r.pipeline)} &#8599;</a>` : `${esc(jenkinsFolder())}/${esc(r.pipeline)}, made when it is accepted`}</dd>` : ""}</dl>`;
  if (r.table === "fix") {
    return `${head}<h3>What it does</h3><pre class="cmd">${esc(r.action.do)}</pre>${r.action.undo ? `<h3>How it undoes</h3><pre class="cmd">${esc(r.action.undo)}</pre>` : `<p class="note">Undo: ${esc(r.action.undo_policy)}.</p>`}
      <h3>Must be true first</h3>${r.require.map(c => `<pre class="cmd">${esc(c.text)}</pre>`).join("")}<h3>Must be true after</h3>${r.verify.map(c => `<pre class="cmd">${esc(c.text)}</pre>`).join("")}
      <p class="note">Risk ${esc(r.risk)}.${r.produced_by.length ? ` Repairs causes found by ${esc(r.produced_by.join(", "))}.` : ""}</p>`;
  }
  return `${head}<h3>What it checks, in order</h3>${r.steps.map(s => `<div class="decision"><div><b>${s.n}. ${esc(s.description)}</b><pre class="cmd">${esc(s.check)}</pre>${Object.entries(s.outcomes).map(([k, o]) => `<div class="note" style="margin-top:4px">If ${esc(words(k))}: ${tpl(o.text)}</div>`).join("")}</div></div>`).join("")}<p class="note">If every check moves on: ${tpl(r.end.text)}</p>`;
}

/* ---------- tags ---------- */
async function pageTags() {
  setNav("tags");
  naming("Tags");
  S.tags = await api("api/tags");
  const areas = [...new Set(S.tags.map(t => t.area))];
  $("#main").innerHTML = `<div class="wrap"><div class="head"><h1>Tags</h1><p>A fixed list, so every run is described the same way. The AI can suggest a new tag on the review page.</p></div>
    <div class="tgrid">${areas.map(a => `<section class="panel"><h2>${esc(a[0].toUpperCase() + a.slice(1))}</h2><ul class="tlist">${S.tags.filter(t => t.area === a).map(t => `<li><b>${esc(t.name)}${t.built_in ? "" : ' <span class="ai">AI</span>'}</b><small>${esc(t.description)}</small>${t.runs ? `<a href="#/failures?tag=${encodeURIComponent(t.tag)}">${plural(t.runs, "run")}</a>` : `<a style="visibility:hidden" tabindex="-1" aria-hidden="true">0</a>`}</li>`).join("")}</ul></section>`).join("")}</div></div>`;
}

/* ---------- which model does what ---------- */
const MODELS = {window: "day"};
const roleNeedsTools = r => r.host || ["support", "customer"].includes(r.id);
const costWords = c => c === null || c === undefined ? "Not published" : c === 0 ? "Free" : c < 0.01 ? "Under 1 cent" : `$${c.toFixed(2)}`;
const priceNote = m => m.input === null || m.input === undefined ? "" : !m.input && !m.output ? ", free" : `, $${m.input} in, $${m.output} out`;
function modelOptions(list, role, chosen) {
  const known = list.some(m => m.id === chosen);
  return (known || !chosen ? "" : `<option selected value="${esc(chosen)}">${esc(chosen)}</option>`) + list.map(m => {
    const cannot = roleNeedsTools(role) && m.tools === false;
    return `<option value="${esc(m.id)}" ${m.id === chosen ? "selected" : ""} ${cannot ? "disabled" : ""}>${esc(m.name || m.id)}${esc(priceNote(m))}${cannot ? ", cannot use tools" : ""}</option>`;
  }).join("");
}
function modelRow(r, d, defaultFor) {
  const inherits = r.host && !r.model, endpoint = r.endpoint || "opencode", list = d.models[endpoint] || [];
  const model = list.find(m => m.id === r.model), efforts = (model && model.efforts) || [];
  const endpoints = d.endpoints.map(e => `<option value="${esc(e.id)}" ${!inherits && e.id === endpoint ? "selected" : ""}>${esc(e.name)}</option>`).join("");
  return `<tr data-role="${esc(r.id)}"><td><b>${esc(r.label)}</b><br><small class="muted">${esc(r.note || "")}</small></td>
    <td><select id="ep-${esc(r.id)}" data-pick="endpoint" aria-label="Endpoint for ${esc(r.label)}">${r.host ? `<option value="" ${inherits ? "selected" : ""}>Same as the default</option>` : ""}${endpoints}</select></td>
    <td>${inherits ? `<span class="muted">${esc(defaultFor.model)}</span>` : `<select id="md-${esc(r.id)}" data-pick="model" aria-label="Model for ${esc(r.label)}">${modelOptions(list, r, r.model)}</select>`}</td>
    <td>${inherits ? "" : efforts.length ? `<select id="ef-${esc(r.id)}" data-pick="effort" aria-label="Thinking effort for ${esc(r.label)}"><option value="">Model default</option>${efforts.map(e => `<option ${e === r.effort ? "selected" : ""}>${esc(e)}</option>`).join("")}</select>` : `<span class="muted">Not offered</span>`}</td></tr>`;
}
function usageTable(d, label) {
  const rows = d.usage[MODELS.window] || [];
  if (!rows.length) return `<p class="muted">No calls in this period.</p>`;
  const sum = f => rows.reduce((n, r) => n + (r[f] || 0), 0), unpriced = rows.some(r => r.cost === null || r.cost === undefined);
  return `<div class="scrollx"><table class="tbl"><thead><tr><th>Model</th><th>Used for</th><th class="num">Calls</th><th class="num">Tokens in</th><th class="num">Of those, cached</th><th class="num">Tokens out</th><th class="num">Estimated cost</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r.model)}</td><td>${esc(Object.entries(r.jobs).map(([k, n]) => `${label(k)} ${n}`).join(", "))}</td><td class="num">${fmtN(r.calls)}</td><td class="num">${fmtK(r.input)}</td><td class="num">${fmtK(r.cached)}</td><td class="num">${fmtK(r.output)}</td><td class="num">${costWords(r.cost)}</td></tr>`).join("")}
    <tr><td><b>All models</b></td><td></td><td class="num"><b>${fmtN(sum("calls"))}</b></td><td class="num"><b>${fmtK(sum("input"))}</b></td><td class="num"><b>${fmtK(sum("cached"))}</b></td><td class="num"><b>${fmtK(sum("output"))}</b></td><td class="num"><b>${unpriced ? "At least " : ""}${costWords(rows.reduce((n, r) => n + (r.cost || 0), 0))}</b></td></tr>
  </tbody></table></div>`;
}
async function pageModels() {
  setNav("models");
  naming("AI models");
  const kept = document.activeElement && document.activeElement.id;
  const d = await api("api/ai-models");
  const staff = d.roles.filter(r => !r.host), computers = d.roles.filter(r => r.host), customer = d.roles.find(r => r.id === "customer");
  const label = id => (d.roles.find(r => r.id === id) || {label: id}).label;
  const head = `<thead><tr><th>Job</th><th>Endpoint</th><th>Model</th><th>Thinking effort</th></tr></thead>`;
  $("#main").innerHTML = `<div class="wrap models"><div class="head"><h1>AI models</h1><p>Choose what each job runs on. A change applies from the next call.</p></div>
    <section class="panel"><h2>Support side</h2><div class="scrollx"><table class="tbl">${head}<tbody>${staff.map(r => modelRow(r, d, customer)).join("")}</tbody></table></div></section>
    <section class="panel"><h2>People at their computers <small>Each one can have a different model</small></h2><div class="scrollx"><table class="tbl">${head}<tbody>${computers.map(r => modelRow(r, d, customer)).join("")}</tbody></table></div></section>
    <section class="panel"><h2>Endpoints <small>Anything that speaks the OpenAI chat format</small></h2><div class="scrollx"><table class="tbl"><thead><tr><th>Name</th><th>Address</th><th>Key</th><th>Models</th><th></th></tr></thead><tbody>
      ${d.endpoints.map(e => `<tr data-endpoint="${esc(e.id)}"><td>${esc(e.name)}</td><td>${esc(e.base)}</td><td>${e.has_key ? "Saved" : '<span class="muted">None</span>'}</td><td class="num">${fmtN((d.models[e.id] || []).length)}</td><td class="row-acts"><button class="btn small" type="button" data-refresh>Refresh list</button>${e.built_in ? "" : `<button class="btn small" type="button" data-edit>Change</button><button class="btn small" type="button" data-delete>Delete</button>`}</td></tr>`).join("")}
    </tbody></table></div>
      <form class="epform" id="ep-form"><h3 id="ep-title">Add an endpoint</h3>
        <label>Name<input type="text" id="ep-name" required maxlength="60" autocomplete="off"></label>
        <label>Address<input type="url" id="ep-base" required placeholder="The address that ends in /v1" autocomplete="off"></label>
        <label>API key<input type="password" id="ep-key" autocomplete="new-password"></label>
        <div class="row-acts"><button class="btn go" type="submit" id="ep-save">Add endpoint</button><button class="btn" type="button" id="ep-cancel" hidden>Cancel</button></div>
      </form></section>
    <section class="panel"><h2>What it has used</h2><div class="seg"><button type="button" data-window="day" aria-pressed="${MODELS.window === "day"}">Last 24 hours</button><button type="button" data-window="week" aria-pressed="${MODELS.window === "week"}">Last 7 days</button></div>${usageTable(d, label)}</section></div>`;
  if (kept && document.getElementById(kept)) document.getElementById(kept).focus();
  const save = async (role, endpoint, model, effort) => {
    try { await post("api/ai-models/roles", {role, endpoint, model, effort}); toast("Saved. The next call uses it."); }
    catch (e) { toast(e.message, true); }
    await pageModels();
  };
  $$("[data-pick]").forEach(s => s.onchange = () => {
    const tr = s.closest("tr"), role = tr.dataset.role, now = d.roles.find(r => r.id === role);
    if (s.dataset.pick === "endpoint") {
      if (!s.value) return save(role, "", "", "");
      const usable = (d.models[s.value] || []).filter(m => !roleNeedsTools(now) || m.tools !== false);
      if (!usable.length) { toast("That endpoint has no models that can do this job. Refresh its list first.", true); return pageModels(); }
      const wanted = now.model || customer.model;
      return save(role, s.value, usable.some(m => m.id === wanted) ? wanted : usable[0].id, "");
    }
    if (s.dataset.pick === "model") {
      const endpoint = $("[data-pick=endpoint]", tr).value, m = (d.models[endpoint] || []).find(x => x.id === s.value), effort = $("[data-pick=effort]", tr);
      return save(role, endpoint, s.value, m && effort && (m.efforts || []).includes(effort.value) ? effort.value : "");
    }
    save(role, $("[data-pick=endpoint]", tr).value, $("[data-pick=model]", tr).value, s.value);
  });
  $$("[data-window]").forEach(b => b.onclick = () => { MODELS.window = b.dataset.window; pageModels(); });
  let editing = "";
  const resetForm = () => { editing = ""; $("#ep-form").reset(); $("#ep-title").textContent = "Add an endpoint"; $("#ep-save").textContent = "Add endpoint"; $("#ep-cancel").hidden = true; $("#ep-key").placeholder = ""; };
  $("#ep-cancel").onclick = resetForm;
  $("#ep-form").onsubmit = async e => {
    e.preventDefault();
    try { await post("api/ai-models/endpoints", {name: $("#ep-name").value, base: $("#ep-base").value, key: $("#ep-key").value, id: editing}); toast(editing ? "Endpoint changed." : "Endpoint added."); await pageModels(); }
    catch (x) { toast(x.message, true); }
  };
  $$("[data-endpoint]").forEach(tr => {
    const id = tr.dataset.endpoint, found = d.endpoints.find(x => x.id === id);
    $("[data-refresh]", tr).onclick = async ev => {
      ev.target.disabled = true; ev.target.textContent = "Refreshing";
      try { const r = await post(`api/ai-models/endpoints/${id}/models`); toast(`${r.models.length} models found.`); }
      catch (x) { toast(x.message, true); }
      await pageModels();
    };
    const edit = $("[data-edit]", tr), del = $("[data-delete]", tr);
    if (edit) edit.onclick = () => { editing = id; $("#ep-name").value = found.name; $("#ep-base").value = found.base; $("#ep-key").value = ""; $("#ep-key").placeholder = "Leave empty to keep the saved key"; $("#ep-title").textContent = `Change ${found.name}`; $("#ep-save").textContent = "Save changes"; $("#ep-cancel").hidden = false; $("#ep-name").focus(); };
    if (del) del.onclick = async () => {
      if (del.dataset.sure !== "1") { del.dataset.sure = "1"; del.textContent = "Delete it for good"; del.classList.add("danger"); return; }
      try { await post(`api/ai-models/endpoints/${id}/delete`); toast("Endpoint deleted."); }
      catch (x) { toast(x.message, true); }
      await pageModels();
    };
  });
}

/* ---------- monitors ---------- */
const MON_STATE = {ok: ["Quiet", "green"], holding: ["Conditions holding", "amber"], fired: ["Started a run", "red"], held_back: ["Held back: a test was running", "blue"], no_data: ["No data", "off"], error: ["Cannot read its variables", "violet"]};
const MON_EVENT = {fired: "Started a run", held_back: "Held back", cleared: "Cleared", accepted: "Accepted", paused: "Paused", active: "Resumed", retired: "Retired", error: "Could not read"};
const OPW = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "=", "!=": "≠"};
const grafanaBase = () => BASE ? "/grafana" : `${location.protocol}//${location.hostname}:8051`;  /* inside Netman its own proxy; here, Netman's proxy port */
const fmtVal = v => v == null ? "no data" : Math.abs(v) >= 1000 ? fmtK(v) : String(Number(Number(v).toPrecision(3)));
const minutesWords = n => n === 0 ? "the minute it began" : n < 0 ? `${Math.abs(n)} min before the alarm` : `${n} min after it began`;
function monitorConditions(m) {
  const join = m.match === "all" ? " <b>and</b> " : " <b>or</b> ";
  return m.conditions.map(c => `<code>${esc(c.variable)}</code> ${esc(OPW[c.op] || c.op)} ${esc(fmtVal(c.value))}`).join(join) + (m.hold_seconds ? `, holding for ${esc(m.hold_seconds)} s` : "") + `. Then quiet for at least ${esc(m.cooldown_minutes)} min.`;
}
function monitorProposal(p) {
  const m = p.body.monitor, t = (p.proof || {}).test || {}, vars = p.body.variables || {};
  const tags = (m.tags || []).length ? `<div class="chips">${m.tags.map(x => `<span class="chip">${esc(x)}</span>`).join("")}</div>` : "";
  const parts = [];
  if (t.catches_this_incident) parts.push(`Caught this incident${t.fired_minutes_from_incident != null ? `, ${esc(minutesWords(t.fired_minutes_from_incident))}` : ""}.`);
  parts.push(t.false_alarms_in_week ? `Would have started ${plural(t.false_alarms_in_week, "other run")} in the week${(t.false_alarm_times || []).length ? ` (${esc(t.false_alarm_times.join(", "))})` : ""}.` : "No false alarms in the week.");
  if (t.held_back_during_tests) parts.push(`Held back ${plural(t.held_back_during_tests, "time")} while test jobs were changing the network.`);
  const proof = t.passed ? `<div class="proof"><span class="led green"></span><div><b>Replayed over the past week.</b> ${parts.join(" ")}</div></div>` : `<div class="proof none"><span class="led off"></span><div>No replay on record.</div></div>`;
  return factRow([["Starts a run on", esc(m.interface ? `${m.device} ${m.interface}` : m.device)], tags && ["Tags", tags], ["Learned from", runLink(p.run_id)], ["Suggested by", esc(p.model || "AI")]]) +
    `<h3>What it watches</h3><ul class="mlist">${m.variables.map(v => `<li><code>${esc(v.name)}</code> ${esc(vars[v.name] || "")}</li>`).join("")}</ul>` +
    `<h3>When it starts a run</h3><p class="mwhen">${monitorConditions(m)}</p>` + proof +
    `<div class="mcharts" data-preview="${esc(p.id)}"><p class="muted">Loading the graphs</p></div>`;
}
function monChart(sr, d) {
  const t0 = d.from, t1 = d.to, X = t => 100 * (t - t0) / Math.max(1, t1 - t0);
  const have = sr.values.filter(v => v != null), thr = sr.thresholds.map(t => t.value);
  if (!have.length) return `<div class="plot"><span class="lab ytop">No values were recorded in this stretch.</span></div>`;
  const top = Math.max(1, ...have, ...thr.map(v => v * 1.2)), bottom = Math.min(0, ...have, ...thr);
  const Y = v => 6 + 88 * (1 - (v - bottom) / Math.max(1e-9, top - bottom));
  let path = "", pen = false;
  sr.times.forEach((t, i) => { const v = sr.values[i]; if (v == null) { pen = false; return; } path += `${pen ? "L" : "M"}${X(t).toFixed(2)} ${Y(v).toFixed(2)}`; pen = true; });
  const bands = (d.episodes || []).map(e => { const a = Math.max(t0, e.began), b = Math.min(t1, (e.until || e.began) + 60); return b > a ? `<rect class="${e.held_back ? "held" : "fire"}" x="${X(a).toFixed(2)}" y="0" width="${Math.max(0.5, X(b) - X(a)).toFixed(2)}" height="100"></rect>` : ""; }).join("");
  const inc = d.incident_at && d.incident_at >= t0 && d.incident_at <= t1;
  return `<div class="plot"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">${bands}${sr.thresholds.map(t => `<line class="thr" x1="0" x2="100" y1="${Y(t.value).toFixed(2)}" y2="${Y(t.value).toFixed(2)}"/>`).join("")}${inc ? `<line class="inc" x1="${X(d.incident_at).toFixed(2)}" x2="${X(d.incident_at).toFixed(2)}" y1="0" y2="100"/>` : ""}<path class="line" d="${path}"/></svg>
    <span class="lab ytop">${esc(fmtVal(top))}</span><span class="lab ybot">${esc(fmtVal(bottom))}</span>${sr.thresholds.map(t => `<span class="lab thr-l" style="top:${Y(t.value).toFixed(1)}%">${esc(OPW[t.op] || t.op)} ${esc(fmtVal(t.value))}</span>`).join("")}${inc ? `<span class="lab inc-l" style="left:${X(d.incident_at).toFixed(1)}%">incident</span>` : ""}</div>
    <div class="xlab"><span>${esc(dfmt(t0))}, ${esc(hm(t0))}</span><span>${esc(dfmt(t1))}, ${esc(hm(t1))}</span></div>`;
}
/* graphs are drawn here, not embedded from Grafana: each embedded panel starts a whole Grafana app and sits blank for seconds */
async function drawPreview(el, url, live = false) {
  try {
    const d = await api(url);  /* the old graphs stay until the new ones are ready, so a redraw never blanks them */
    el.innerHTML = `<div class="mgrid">${Object.entries(d.series).map(([name, sr]) => `<figure class="mchart"><figcaption><code>${esc(name)}</code>${live ? "" : ` ${esc(sr.words)}`}</figcaption>${monChart(sr, d)}</figure>`).join("")}</div>` +
      `<div class="mkey"><span><i class="k-fire"></i>${live ? "started a run" : "would have started a run"}</span><span><i class="k-held"></i>held back: a test was running</span><span><i class="k-thr"></i>threshold</span>${d.incident_at ? `<span><i class="k-inc"></i>the incident</span>` : ""}</div>`;
  } catch (e) { if (!live || !el.querySelector("svg")) el.innerHTML = `<p class="muted">The graphs could not be drawn: ${esc(e.message)}</p>`; }
}
const drawLive = () => $$("[data-live]").forEach(el => drawPreview(el, `api/monitors/preview?monitor=${encodeURIComponent(el.dataset.live)}&hours=6`, true));
function monitorCard(m, d) {
  const sp = m.spec, paused = m.status === "paused", [stateText, led] = MON_STATE[m.state] || ["Not looked at yet", "off"], vals = (m.last || {}).values || {};
  const numbers = `<div class="mvars">${sp.variables.map(v => `<div class="mvar"><span class="muted"><code>${esc(v.name)}</code> now</span><b class="num" data-val="${esc(v.name)}">${esc(fmtVal(vals[v.name]))}</b><small>${sp.conditions.filter(c => c.variable === v.name).map(c => `starts a run at ${esc(OPW[c.op] || c.op)} ${esc(fmtVal(c.value))}`).join(", ")}</small><p>${esc(m.variables_words[v.name] || "")}</p></div>`).join("")}</div>`;
  const panels = `<div class="mcharts" data-live="${esc(sp.id)}"><p class="muted">Loading the graphs</p></div><p class="note">The last 6 hours, read the same way the watcher reads them. The same graphs are on the <a href="#/monitors/dashboard">Grafana dashboard</a>.</p>`;
  const evs = m.events.length ? `<details class="mevents"><summary>What it did lately</summary><ul>${m.events.map(e => `<li><time>${esc(dfmt(e.time))}, ${esc(hm(e.time))}</time> <b>${esc(MON_EVENT[e.kind] || e.kind)}</b> <span class="muted">${esc(e.note || "")}</span>${e.run_id ? ` <a href="#/run/${esc(e.run_id)}">Open the run</a>` : ""}</li>`).join("")}</ul></details>` : "";
  const from = m.proposal ? `<a href="#/review?status=accepted&kind=monitor">Suggestion #${esc(m.proposal)}</a>${m.run_id ? `, from <a href="#/run/${esc(m.run_id)}">a run</a>` : ""}` : `Added by ${esc(m.by || "hand")}`;
  return `<article class="prop mon ${m.proposal ? "aimon" : ""}" data-mon="${esc(sp.id)}"><header><h2>${esc(sp.title)}${m.proposal ? ' <span class="chip aimon" title="The AI suggested this monitor and a person accepted it. Runs it starts are marked AI monitor.">AI monitor</span>' : ""}</h2><span class="mstate"><span class="led ${paused ? "off" : led}${m.state === "fired" && !paused ? " pulse" : ""}" data-led></span><span data-state>${paused ? "Paused" : esc(stateText)}</span></span></header>
    ${m.why ? `<p class="why">${esc(m.why)}</p>` : ""}
    ${factRow([["Starts a run on", esc(sp.interface ? `${sp.device} ${sp.interface}` : sp.device)], ["When", monitorConditions(sp)], ["Last looked", `<span data-looked>${m.last_eval ? esc(agoWords(m.last_eval)) : "not yet"}</span>`], ["Came from", from]])}
    ${numbers}${panels}${evs}
    <div class="decide" data-mon-acts>${paused ? `<button class="btn go" type="button" data-mon-act="active">Resume</button>` : `<button class="btn" type="button" data-mon-act="paused">Pause</button>`}<button class="btn" type="button" data-mon-act="ask-retire">Retire</button></div></article>`;
}
function sessionsBlock(d) {
  if (!d.sessions.length) return "";
  return `<section class="panel msess"><h2>Suggestion sessions</h2><p class="muted">Each time a run finds a cause, the AI looks for a monitor that would have caught it sooner. Its model is chosen on the AI models page, under Monitor suggestions.</p>
    <ul class="dlist">${d.sessions.map(s => `<li><b>${esc(dfmt(s.time))}, ${esc(hm(s.time))}</b> <a href="#/run/${esc(s.run_id)}">${esc(s.run_id)}</a> <span class="chip">${esc(words(s.status))}</span> <span class="muted">${esc(s.model || "")}</span>${s.proposals.length ? ` suggested ${s.proposals.map(n => `<a href="#/review?status=pending&kind=monitor">#${esc(n)}</a>`).join(", ")}` : ""}${s.summary || s.error ? `<br><span class="muted">${esc(s.summary || s.error)}</span>` : ""}</li>`).join("")}</ul></section>`;
}
function wireMonitors() {
  $$(".mon[data-mon]").forEach(el => {
    const id = el.dataset.mon, box = $("[data-mon-acts]", el), original = box.innerHTML;
    const said = {paused: "Paused. It is not looked at until you resume it.", active: "Watching again.", retired: "Retired. Its history stays."};
    const act = async status => {
      $$("button", box).forEach(b => { b.disabled = true; });
      try { const r = await post(`api/monitors/${id}/status`, {status}); toast(said[status] + (/^could not/.test(r.grafana || "") ? ` The Grafana dashboard ${r.grafana}.` : "")); route(); }
      catch (e) { toast(e.message, true); $$("button", box).forEach(b => { b.disabled = false; }); }
    };
    const wire = () => $$("[data-mon-act]", box).forEach(b => b.onclick = () => {
      const a = b.dataset.monAct;
      if (a === "ask-retire") { box.innerHTML = `<p class="warn">Retiring takes it off this page and the dashboard for good. What it did stays on record.</p><button class="btn danger" type="button" data-mon-act="retired">Retire</button><button class="btn" type="button" data-mon-act="back">Go back</button>`; wire(); return; }
      if (a === "back") { box.innerHTML = original; wire(); return; }
      act(a);
    });
    wire();
  });
}
async function refreshMonitors() {
  if (document.hidden || S.page !== "monitors") return;
  try {
    const d = await api("api/monitors");
    d.monitors.forEach(m => {
      const el = $(`.mon[data-mon="${CSS.escape(m.spec.id)}"]`);
      if (!el || m.status === "paused") return;
      const [text, led] = MON_STATE[m.state] || ["Not looked at yet", "off"], vals = (m.last || {}).values || {};
      $("[data-state]", el).textContent = text;
      $("[data-led]", el).className = `led ${led}${m.state === "fired" ? " pulse" : ""}`;
      $("[data-looked]", el).textContent = m.last_eval ? agoWords(m.last_eval) : "not yet";
      $$("[data-val]", el).forEach(b => { b.textContent = fmtVal(vals[b.dataset.val]); });
    });
    if (++S.monTicks % 3 === 0) drawLive();  /* the graphs once a minute, the numbers every 20 s */
  } catch (e) { /* the next look will try again */ }
}
async function pageMonitors() {
  setNav("monitors");
  naming("Monitors");
  const d = await api("api/monitors");
  const full = `${grafanaBase()}/d/${d.grafana.uid}/${d.grafana.slug}?theme=dark&from=now-6h&to=now`;
  $("#main").innerHTML = `<div class="wrap" style="max-width:1060px">
    <div class="head"><h1>Monitors</h1><p>Numbers the network already reports, with conditions that start a troubleshooting run. After a run finds a cause, the AI suggests a monitor that would have caught it sooner, replayed over the past week. It starts watching once you accept it in Review.</p></div>
    ${d.watching === false ? `<p class="mwarn">The watcher has not looked at the monitors in the last few minutes, so nothing is being watched. Check the troubleshooting watcher service.</p>` : ""}
    <div class="mbar">${d.waiting ? `<a class="btn small go" href="#/review?status=pending&kind=monitor">${plural(d.waiting, "suggestion")} waiting</a>` : ""}<a class="btn small" href="#/monitors/dashboard">Dashboard</a><a class="btn small" href="${esc(full)}" target="_blank" rel="noreferrer">Open in Grafana &#8599;</a></div>
    ${d.monitors.length ? d.monitors.map(m => monitorCard(m, d)).join("") : `<div class="panel empty">No monitors yet. After a run finds a cause, the AI suggests monitors that would have caught it sooner. They wait in <a href="#/review?kind=monitor">Review</a> until you accept one.</div>`}
    ${sessionsBlock(d)}
  </div>`;
  wireMonitors();
  drawLive();
  S.monTicks = 0;
  S.poll = setInterval(refreshMonitors, 20000);
}
async function pageMonitorDashboard() {
  setNav("mdash");
  naming("Monitors dashboard");
  const d = await api("api/monitors"), url = `${grafanaBase()}/d/${d.grafana.uid}/${d.grafana.slug}?theme=dark&refresh=30s&from=now-6h&to=now`;
  $("#main").innerHTML = `<div class="wrap" style="max-width:1240px">
    <div class="head"><h1>Monitors dashboard</h1><p>Every monitor in Grafana: its state, and each variable against its threshold, as Prometheus stores them.</p></div>
    <div class="mbar"><a class="btn small" href="#/monitors">Monitors</a><a class="btn small" href="${esc(url)}" target="_blank" rel="noreferrer">Open in Grafana &#8599;</a></div>
    <div class="gfull"><iframe src="${esc(url + "&kiosk")}" title="${esc(d.grafana.title)}"></iframe></div></div>`;
}

/* ---------- find anything ---------- */
const PAL = {items: [], at: 0, token: 0, back: null};
function palStatic() {
  const pages = [["Now", "#/"], ["History", "#/failures"], ["How it works", "#/flow"], ["Waiting for you", "#/failures?state=needs_you"], ["Review", "#/review"], ["Rules", "#/rules"], ["Tags", "#/tags"], ["Monitors", "#/monitors"], ["Monitors dashboard", "#/monitors/dashboard"], ["AI models", "#/models"], ["Collect", "#/collect"], ["Data", "#/ai-data"], ["Train", "#/train"]].map(([t, go]) => ({t, sub: "Page", go}));
  const devices = (S.now && S.now.map ? S.now.map.nodes : []).map(n => ({t: n.id, sub: `Device, ${n.bad.length ? "failing" : ROLE_WORDS[n.role] || "no problems"}`, dev: n.id, led: n.bad.length ? "red" : ""}));
  const rules = (S.rules ? S.rules.rules : []).filter(r => r.table !== "filter").map(r => ({t: r.title || r.id, sub: "Rule", rule: r.id}));
  const tags = (S.tags || []).map(t => ({t: t.tag, sub: "Tag", go: "#/failures?tag=" + encodeURIComponent(t.tag)}));
  return [...pages, ...devices, ...rules, ...tags];
}
function palMark() {
  $$("#pal-list [data-n]").forEach(li => { const on = +li.dataset.n === PAL.at; li.setAttribute("aria-selected", on); if (on) li.scrollIntoView({block: "nearest"}); });
  $("#pal-q").setAttribute("aria-activedescendant", PAL.items.length ? "pal-" + PAL.at : "");
}
function palDraw() {
  $("#pal-list").innerHTML = PAL.items.length ? PAL.items.map((it, n) => `<li role="option" id="pal-${n}" data-n="${n}" aria-selected="false"><span>${it.led ? `<span class="led ${it.led}"></span>` : ""}${esc(it.t)}</span><small>${esc(it.sub)}</small></li>`).join("") : `<li class="none">Nothing matches.</li>`;
  $$("#pal-list [data-n]").forEach(li => { li.onclick = () => palPick(+li.dataset.n); li.onmousemove = () => { if (PAL.at !== +li.dataset.n) { PAL.at = +li.dataset.n; palMark(); } }; });
  palMark();
}
async function palSearch() {
  const q = $("#pal-q").value.trim().toLowerCase(), mine = ++PAL.token, starts = it => it.t.toLowerCase().startsWith(q);
  const found = palStatic().filter(it => !q || it.t.toLowerCase().includes(q) || it.sub.toLowerCase().includes(q)).sort((a, b) => starts(b) - starts(a));
  PAL.items = (q ? found : found.filter(i => i.sub === "Page" || i.led === "red")).slice(0, 9);
  PAL.at = 0;
  palDraw();
  if (q.length < 2) return;
  const d = await api("api/runs?size=5&q=" + encodeURIComponent(q)).catch(() => null);
  if (mine !== PAL.token || !d) return;
  PAL.items = [...PAL.items, ...d.items.map(r => ({t: r.title, sub: `Run, ${stateOf(r.state).t.toLowerCase()}, ${ago(r.time)}`, go: "#/run/" + r.id, led: stateOf(r.state).c}))];
  palDraw();
}
async function openPalette() {
  if (!$("#pal").hidden) return;
  PAL.back = document.activeElement;
  $("#pal").hidden = false;
  $("#pal-q").value = "";
  $("#pal-q").focus();
  palSearch();
  const need = [];
  if (!S.rules) need.push(api("api/rules").then(d => { S.rules = d; }).catch(() => {}));
  if (!S.tags) need.push(api("api/tags").then(d => { S.tags = d; }).catch(() => {}));
  if (need.length) { await Promise.all(need); if (!$("#pal").hidden) palSearch(); }
}
function closePalette() { $("#pal").hidden = true; if (PAL.back && PAL.back.focus) PAL.back.focus(); }
function palPick(n) {
  const it = PAL.items[n];
  if (!it) return;
  $("#pal").hidden = true;
  if (it.dev) { if (S.page === "now") openDevice(it.dev); else location.hash = "#/failures?device=" + encodeURIComponent(it.dev); return; }
  if (it.rule) { if (S.page === "rules") openDrawer("rule-" + it.rule, false, ruleBody(it.rule)); else { S.openRule = it.rule; location.hash = "#/rules"; } return; }
  location.hash = it.go;
}
let palTyping;
$("#find").onclick = openPalette;
$("#pal").onclick = e => { if (e.target.id === "pal") closePalette(); };
$("#pal-q").oninput = () => { clearTimeout(palTyping); palTyping = setTimeout(palSearch, 120); };
$("#pal-q").onkeydown = e => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (PAL.items.length) { PAL.at = (PAL.at + (e.key === "ArrowDown" ? 1 : -1) + PAL.items.length) % PAL.items.length; palMark(); } }
  if (e.key === "Enter") { e.preventDefault(); palPick(PAL.at); }
};

/* ---------- keys ---------- */
document.addEventListener("keydown", e => {
  const typing = e.target.matches("input, textarea, select");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
  if (e.key === "Escape" && !$("#pal").hidden) { closePalette(); return; }
  if (e.key === "Escape" && S.drawer) { closeDrawer(); return; }
  if (typing) return;
  if (e.key === "/") { e.preventDefault(); if ($("#f-q")) $("#f-q").focus(); else openPalette(); return; }
  if (!S.rec || !$("#play")) return;
  if (e.key === "ArrowRight") { stopReplay(); go(S.pos + 1, true); }
  if (e.key === "ArrowLeft") { stopReplay(); go(S.pos - 1, true); }
  if (e.key === " " && !e.target.matches("button, a")) { e.preventDefault(); S.playing ? stopReplay() : play(); }
});

refreshNow().then(route);
