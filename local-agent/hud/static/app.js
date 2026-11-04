// iwd / llm — HUD client. No framework, no build step; served under a strict CSP (no inline JS/CSS).
// Model output is untrusted: everything is escaped before the tiny markdown renderer touches it.

const BASE = location.pathname.replace(/[^/]*$/, ""); // "/llm/" in production, "/" locally
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

let csrf = "", skills = [], status = null, jobs = [], selected = null, page = "overview";
let crewInfo = { modes: [], roles: [], slots: 1 }, mission = null, missionTimer = null;
let chatProfiles = { active: 1, profiles: [] };   // chat profiles: separate chat lists + agent memory; this tab's choice is sent with every list/new chat
const transcripts = new Map(); // job id -> {job, events[]}
const genHistory = []; let lastTok = null;
let pending = []; // staged uploads for the next command: {id, url, uploading}

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const method = opts.method || (opts.json || opts.body ? "POST" : "GET");
  let r;
  for (let attempt = 0; ; attempt++) {   // phones on 4G drop requests ("Load failed"): reads retry quietly, writes never (no double sends)
    try {
      r = await fetch(BASE + path, {
        credentials: "same-origin", ...opts, method,
        headers: { "x-csrf": csrf, ...(opts.json ? { "content-type": "application/json" } : {}), ...(opts.headers || {}) },
        body: opts.json ? JSON.stringify(opts.json) : opts.body,
      });
      break;
    } catch (err) {
      if (method !== "GET" || attempt >= 2) throw new Error(navigator.onLine === false ? "You're offline" : "Connection dropped, try again");
      await new Promise((res) => setTimeout(res, 700 * (attempt + 1)));
    }
  }
  if (r.status === 401) { showLogin(); throw new Error("signed out"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data?.detail || data || r.statusText);
  return data;
}
function toast(msg, err = false) { const t = el("div", "toast" + (err ? " err" : ""), msg); document.body.append(t); setTimeout(() => t.remove(), err ? 7000 : 3500); }
const fmtAgo = (ts) => { if (!ts) return "—"; const s = Date.now() / 1000 - ts; return s < 60 ? `${s | 0}s ago` : s < 3600 ? `${(s / 60) | 0}m ago` : s < 86400 ? `${(s / 3600) | 0}h ago` : new Date(ts * 1000).toLocaleDateString(); };
const fmtDur = (a, b) => { if (!a) return ""; const s = ((b || Date.now() / 1000) - a) | 0; return s < 60 ? `${s}s` : `${(s / 60) | 0}m ${s % 60}s`; };

// ---------------------------------------------------------------- login
function showLogin() { $("#app-view").classList.add("hidden"); $("#login-view").classList.remove("hidden"); $("#login-user").focus(); }
$("#login-form")?.addEventListener("submit", async (e) => {
  e.preventDefault(); $("#login-error").textContent = "";
  try {
    const r = await api("api/login", { json: { username: $("#login-user").value, password: $("#login-pass").value } });
    csrf = r.csrf; $("#login-pass").value = ""; boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$$(".logout").forEach((b) => b.addEventListener("click", async () => { try { await api("api/logout", { method: "POST", json: {} }); } catch {} location.reload(); }));

// ---------------------------------------------------------------- nav
function go(p) {
  page = p;
  $$(".rail .nav[data-page]").forEach((b) => b.classList.toggle("active", b.dataset.page === p));
  $$("main .page").forEach((s) => s.classList.toggle("hidden", s.dataset.view !== p));
  history.replaceState(null, "", "#" + p);
  document.body.classList.toggle("on-console", p === "console");
  if (p === "model") loadProfiles();
  if (p === "evals") loadEvals();
  if (p === "logs") loadLog();
  if (p === "usage") loadUsage();
  if (p === "skills") loadSkillsPage();
  if (p === "toolbox") loadToolbox();
}
$$(".rail .nav[data-page]").forEach((b) => b.addEventListener("click", () => go(b.dataset.page)));
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) { e.preventDefault(); go(g.dataset.goto); } });

// ---------------------------------------------------------------- websocket
let ws, wsDelay = 1000;
function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + BASE + "ws");
  ws.onopen = () => { wsDelay = 1000; setPill("#tb-ws", "up", "stream open"); };
  ws.onclose = () => { setPill("#tb-ws", "down", "stream closed"); setTimeout(connect, wsDelay); wsDelay = Math.min(wsDelay * 2, 15000); };
  ws.onmessage = (m) => { const msg = JSON.parse(m.data); if (msg.type === "status") onStatus(msg.data); else if (msg.type === "job") onJob(msg.data); else if (msg.type === "event") onEvent(msg); else if (msg.type === "mission") onMission(msg); };
}
setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000);
function setPill(sel, cls, text) { const p = $(sel); if (p) { p.className = "pill " + cls + (p.dataset.deskOnly ? " desk-only" : ""); p.textContent = text; } }

// ---------------------------------------------------------------- status
function onStatus(st) {
  status = st;
  const m = st.model || {}, h = st.health || {}, g = st.gpu;
  if (m.tokens_predicted_total != null) {
    const now = st.ts;
    if (lastTok && now > lastTok.ts) { const rate = Math.max(0, (m.tokens_predicted_total - lastTok.n) / (now - lastTok.ts)); genHistory.push(rate); if (genHistory.length > 150) genHistory.shift(); }
    lastTok = { ts: now, n: m.tokens_predicted_total };
  }
  const cur = genHistory.at(-1) || 0, sl = m.slot;
  setText("#tb-profile", m.profile || "—"); setText("#tb-ctx", m.ctx ? `${(m.ctx / 1024) | 0}k` : "—");
  setText("#tb-gen", sl?.processing && sl.prompt_tokens && sl.prompt_done < sl.prompt_tokens ? `prefill ${Math.round((100 * sl.prompt_done) / sl.prompt_tokens)}%` : cur > 0.5 ? `${cur.toFixed(1)} tok/s` : "idle");
  setText("#tb-gpu", g ? `${(g.mem_used / 1024).toFixed(1)}/${(g.mem_total / 1024).toFixed(0)} GB · ${g.util}%` : "—");
  const ms = h.model, dotState = ms === "up" ? (sl?.processing ? "busy" : "up") : ms === "loading" ? "busy" : "down";
  $("#tb-dot").className = "sdot " + dotState;
  setPill("#tb-model", dotState, ms === "up" ? (sl?.processing ? "model busy" : "model ready") : ms === "loading" ? "model loading" : "model down");
  const tj = $("#tb-job");
  const ra = st.running_all || [];
  if (ra.length) { tj.classList.remove("hidden"); tj.textContent = ra.length === 1 ? `#${ra[0].id} · ${fmtDur(ra[0].started)}` : `${ra.length} agents · ${st.crew?.busy ?? ra.length}/${st.crew?.slots ?? "?"} slots`; } else tj.classList.add("hidden");
  if (page === "overview") renderOverview();
  drawSpark();
}
const setText = (sel, v) => { const e = $(sel); if (e) e.textContent = v; };
function kv(dl, rows) { dl.replaceChildren(...rows.flatMap(([k, v, cls]) => { const dt = el("dt", null, k), dd = el("dd", cls || null, v); dd.title = v; return [dt, dd]; })); }
function renderOverview() {
  if (!status) return;
  const m = status.model || {}, ho = status.host || {}, g = status.gpu, h = status.health || {};
  kv($("#ov-model"), [
    ["profile", m.profile || "—"], ["context", m.ctx ? m.ctx.toLocaleString() + " tokens" : "—"],
    ["state", h.model === "up" ? (m.slot?.processing ? "generating" : "idle") : h.model || "—", h.model === "up" ? "ok" : "error"],
    ["avg generation", m.avg_gen_tps ? m.avg_gen_tps.toFixed(1) + " tok/s" : "—"],
    ["avg prefill", m.avg_prefill_tps ? m.avg_prefill_tps.toFixed(0) + " tok/s" : "—"],
    ["tokens generated", m.tokens_predicted_total != null ? Math.round(m.tokens_predicted_total).toLocaleString() : "—"],
  ]);
  kv($("#ov-host"), [
    ["CPU", `${ho.cpus} threads · load ${ho.load?.map((x) => x.toFixed(1)).join(" / ")}`],
    ["RAM free", `${ho.ram_avail_gib} / ${ho.ram_total_gib} GiB`],
    ["GPU memory", g ? `${(g.mem_used / 1024).toFixed(1)} / ${(g.mem_total / 1024).toFixed(1)} GiB` : "—"],
    ["GPU util / temp", g ? `${g.util}% · ${g.temp}°C` : "—"],
    ["queue", Object.entries(status.queue || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "empty"],
  ]);
  const svc = [["llama-server", h.model], ["embeddings", h.embed], ["SearXNG", h.searxng], ["Crawl4AI", h.crawl4ai], ["Qdrant", h.qdrant], ["sandbox relay", h.relay], ["watchdog", status.units?.["la-watchdog.timer"] === "active" ? "up" : "down"]];
  $("#ov-services").replaceChildren(...svc.map(([n, s]) => { const tr = el("tr"); tr.append(el("td", null, n)); const td = el("td", "r"); td.append(el("span", "tag " + (s || "down"), s || "down")); tr.append(td); return tr; }));
  renderCrewCard();
  renderRecent();
}
function roleBadge(j) { const b = el("span", "rb" + (j.role === crewInfo.lead ? " lead" : ""), j.role === crewInfo.lead ? (j.mode || "lead") : (j.role || "")); return b; }
function renderCrewCard() {
  const c = status.crew || {}, ra = status.running_all || [];
  kv($("#ov-crew"), [["slots", `${c.busy ?? 0} busy / ${c.slots ?? 1} total`], ["queued agents", String(c.queued ?? 0)], ["background tasks", String(c.bg_running ?? 0)]]);
  const rows = ra.filter((j) => j.kind === "agent").map((j) => {
    const tr = el("tr", "click"); tr.onclick = () => { go("console"); selectJob(j.id); };
    tr.append(el("td", "mono muted", "#" + j.id));
    const tdR = el("td"); if (j.role) tdR.append(roleBadge(j)); tr.append(tdR);
    tr.append(el("td", null, j.label && j.role !== crewInfo.lead ? `${j.label} — ${j.title}` : j.title));
    tr.append(el("td", "r muted small", fmtDur(j.started))); return tr;
  });
  $("#ov-agents").replaceChildren(...(rows.length ? rows : [el("tr", null, "No agents running.")]));
}
function renderRecent() {
  const rows = jobs.slice(0, 8).map((j) => {
    const tr = el("tr", "click"); tr.onclick = () => { go("console"); selectJob(j.id); };
    tr.append(el("td", "mono muted", "#" + j.id));
    const tdS = el("td"); tdS.append(el("span", "tag " + j.status, j.status)); tr.append(tdS);
    tr.append(el("td", null, j.title));
    tr.append(el("td", "r muted small", j.status === "running" ? fmtDur(j.started) : fmtAgo(j.created))); return tr;
  });
  $("#ov-jobs").replaceChildren(...(rows.length ? rows : [el("tr", null, "No tasks yet.")]));
}
function drawSpark() {
  const c = $("#spark"); if (!c || !c.offsetWidth) return;
  const dpr = devicePixelRatio || 1; c.width = c.offsetWidth * dpr; c.height = c.offsetHeight * dpr;
  const x = c.getContext("2d"); x.scale(dpr, dpr); const w = c.offsetWidth, hh = c.offsetHeight;
  const max = Math.max(10, ...genHistory) * 1.15;
  x.strokeStyle = "#30363D"; x.beginPath(); x.moveTo(0, hh - 0.5); x.lineTo(w, hh - 0.5); x.stroke();
  if (genHistory.length < 2) return;
  const grad = x.createLinearGradient(0, 0, w, 0); grad.addColorStop(0, "#58A6FF"); grad.addColorStop(1, "#BC8CFF");
  x.strokeStyle = grad; x.lineWidth = 1.5; x.beginPath();
  genHistory.forEach((v, i) => { const px = (i / 149) * w, py = hh - (v / max) * (hh - 4) - 2; i ? x.lineTo(px, py) : x.moveTo(px, py); });
  x.stroke(); x.fillStyle = "#8B949E"; x.font = "10px ui-monospace, monospace"; x.fillText(`${max.toFixed(0)} tok/s`, 2, 10);
}

// ---------------------------------------------------------------- jobs / console
async function loadJobs() { jobs = await api(`api/jobs?limit=200&profile=${chatProfiles.active}`); renderJobList(); renderRecent(); }
function onJob(j) {
  const i = jobs.findIndex((x) => x.id === j.id);
  if (i < 0 && j.profile_id && j.profile_id !== chatProfiles.active) return;   // another profile's chat
  if (i >= 0) jobs[i] = { ...jobs[i], ...j }; else jobs.unshift(j);
  if (transcripts.has(j.id)) transcripts.get(j.id).job = { ...transcripts.get(j.id).job, ...j };
  renderJobList(); if (page === "overview") renderRecent();
  if (selected === j.id) { renderTranscriptHead(); if (["done", "failed", "cancelled"].includes(j.status)) renderTranscript(); }
  const cur = transcripts.get(selected)?.job; if (cur && mission && (j.root_id || j.id) === (cur.root_id || cur.id) && j.id !== selected) onMission({ root: cur.root_id || cur.id });
}
function onEvent(m) {
  const t = transcripts.get(m.job); if (!t) return;
  if (t.events.some((e) => e.seq === m.seq)) return;
  t.events.push({ seq: m.seq, ts: m.ts, etype: m.etype, data: m.data });
  if (selected === m.job) appendEvent($("#transcript"), t, t.events.at(-1), true);
}
function renderJobList() {
  const byParent = new Map(); jobs.forEach((j) => { if (j.parent_id) (byParent.get(j.parent_id) || byParent.set(j.parent_id, []).get(j.parent_id)).push(j); });
  const roots = jobs.filter((j) => !j.parent_id || !jobs.some((p) => p.id === j.parent_id));
  // one block per conversation; conversations ordered by latest activity (newest on top)
  const threads = roots.map((root) => {
    const members = [];
    const walk = (j) => { members.push(j); (byParent.get(j.id) || []).sort((a, b) => a.id - b.id).forEach(walk); };
    walk(root);
    return { members, last: Math.max(...members.map((m) => m.finished || m.started || m.created || 0)) };
  }).sort((a, b) => b.last - a.last);
  const items = threads.map((t) => {
    const li = el("li", "thread" + (t.members.some((m) => m.id === selected) ? " has-sel" : ""));
    t.members.forEach((j, i) => {
      const child = j.relation === "child";
      const row = el("div", "jrow" + (i ? (child ? " child" : " follow") : "") + (j.id === selected ? " sel" : ""));
      const jt = el("div", "jt"); if (j.role) jt.append(roleBadge(j)); jt.append(document.createTextNode(child ? (j.label || j.role) + " · " + (j.title || "").replace(/^[^:]*:\s*/, "") : (j.title || `${j.kind} #${j.id}`)));
      row.append(jt);
      const meta = el("div", "jm"); meta.append(el("span", "tag " + j.status, j.status), el("span", null, `#${j.id}`), el("span", null, j.status === "running" ? fmtDur(j.started) : fmtAgo(j.finished || j.created)));
      row.append(meta); row.onclick = () => selectJob(j.id); li.append(row);
    });
    return li;
  });
  $("#job-list").replaceChildren(...items);
}
async function selectJob(id) {
  selected = id; renderJobList();
  closeDrawer();
  const j = await api(`api/jobs/${id}`);
  transcripts.set(id, { job: j, events: j.events });
  renderTranscriptHead(); renderTranscript(); renderLive();
  $("#followup-wrap").classList.toggle("hidden", j.kind !== "agent");
  $("#followup-wrap").lastChild.textContent = j.relation === "child" ? " continue this mission" : " continue this thread";
  const hasMission = j.kind === "agent" && (j.mode && j.mode !== "chat" || j.relation === "child" || j.role);
  $("#mission-btn").classList.toggle("hidden", !hasMission);
  if (hasMission) loadMission(j.root_id || j.id, j.relation !== "child" && !$("#mission").dataset.userHidden);
  else { mission = null; $("#mission").classList.add("hidden"); }
}
const fmtSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(2)} GB`;
const fileIcon = (name, kind) => kind === "archive" || kind === "archive-failed" ? "🗜" : /\.(py|js|ts|tsx|jsx|java|kt|go|rs|c|cc|cpp|h|hpp|cs|rb|php|swift|lua|sh|sql|html|css|json|ya?ml|toml|xml|ipynb)$/i.test(name) ? "⌨" : /\.(pdf|docx?|pptx?|xlsx?|csv|md|txt)$/i.test(name) ? "📄" : "📎";
function fileRow(files) {
  const d = el("div", "att-files");
  files.forEach((f) => {
    const a = el("a", "att-chip"); a.href = BASE + "api/files?path=" + encodeURIComponent(f.path); a.setAttribute("download", f.name); a.dataset.wsfile = f.path;
    a.title = f.kind === "archive" ? `${f.path}\nunpacked to ${f.extracted}/` : f.path;
    a.append(el("span", "ic", fileIcon(f.name, f.kind)), el("span", "nm", f.name),
             el("span", "sz", f.kind === "archive" ? `${fmtSize(f.size)} · ${f.files} files` : f.kind === "archive-failed" ? `${fmtSize(f.size)} · not unpacked` : fmtSize(f.size)));
    d.append(a);
  });
  return d;
}
function imgRow(ids) { const d = el("div", "imgs"); ids.forEach((id) => { const im = el("img"); im.src = BASE + "api/uploads/" + id; im.loading = "lazy"; im.onclick = () => openLightbox(im.src); d.append(im); }); return d; }
function renderTranscriptHead() {
  const t = transcripts.get(selected); if (!t) return; const j = t.job; renderLive();
  $("#tr-title").textContent = j.title || `${j.kind} #${j.id}`;
  const tok = j.tokens ? ` · ${(j.tokens.total || 0).toLocaleString()} tok` : "";
  const who = j.relation === "child" ? ` · ${j.role} (${j.label || ""})` : j.mode && j.mode !== "chat" ? ` · ${j.mode} mission` : j.role ? ` · ${j.role}` : "";
  const tools = (j.n_tools ? ` · ${j.n_tools} tools` : "") + (j.params?.quick_reply ? " · ⚡ quick reply" : j.effort ? ` · ${EFFORT_LABEL[j.effort] || j.effort} effort` : "");
  $("#tr-meta").textContent = `#${j.id} · ${j.status}${who}${j.started ? " · " + fmtDur(j.started, j.finished) : ""}${tools}${tok}`;
  $("#tr-cancel").classList.toggle("hidden", !["queued", "running"].includes(j.status));
  $("#tr-spin").classList.toggle("hidden", j.status !== "running");
  const ctx = status?.model?.agent_ctx || status?.model?.ctx || 0, used = j.tokens?.input || 0, meter = $("#tr-ctx");
  if (ctx && used && j.kind === "agent") { const f = Math.min(1, used / ctx); meter.classList.remove("hidden"); meter.classList.toggle("hot", f > 0.7); meter.querySelector("i").style.setProperty("--w", `${Math.round(f * 100)}%`); meter.querySelector("b").textContent = `ctx ${(used / 1000).toFixed(0)}k/${(ctx / 1000).toFixed(0)}k`; }
  else meter.classList.add("hidden");
  $("#tr-compact").classList.toggle("hidden", !(j.kind === "agent" && j.relation !== "child" && j.status === "done" && j.session_id && !j.params?.compact));
}
$("#tr-compact").addEventListener("click", async () => {
  if (!selected || !confirm("Summarize this conversation and continue from the summary? The next message starts a fresh context seeded with it.")) return;
  try { const j = await api(`api/jobs/${selected}/compact`, { method: "POST", json: {} }); onJob(j); selectJob(j.id); toast("Compacting…"); } catch (err) { toast(err.message, true); }
});
function renderTranscript() {
  const box = $("#transcript"), t = transcripts.get(selected); if (!t) return;
  box.replaceChildren(); delete box.dataset.task; const j = t.job;
  if (j.kind === "agent" && !j.params?.compact) { const u = el("div", "msg user" + (j.relation === "child" ? " brief" : ""), j.params?.prompt || j.title); if (j.params?.images?.length) u.append(imgRow(j.params.images)); if (j.params?.files?.length) u.append(fileRow(j.params.files)); box.append(u); }
  if (j.params?.routing || j.params?.triage) box.append(routeNote(j.params.routing || {}, j.params.triage));
  if (j.params?.compact && !j.final) box.append(el("p", "muted", "Compacting the conversation…"));
  let evs = t.events;
  if (j.final && j.kind === "agent") { let k = evs.length; while (k > 0 && evs[k - 1].etype === "text") k--; evs = evs.slice(0, k); }
  evs.forEach((e) => appendEvent(box, t, e, false));
  if (j.final && j.kind === "agent") {
    if (j.params?.compact) { const d = el("details", "msg compact"); d.append(el("summary", null, "Context compacted — the conversation continues from this summary")); const sm = el("div", "sum"); sm.innerHTML = md(j.final, true); d.append(sm); box.append(d); }
    else { const f = el("div", "msg assistant final"); f.innerHTML = md(j.final, true); box.append(f, msgActions(j.final)); mountCharts(f); attachFiles(f); }
  }
  if (j.error) box.append(el("div", "msg err", j.error));
  if (j.status === "queued") box.append(el("p", "muted", j.relation === "child" ? "Queued — starts when a model slot frees up." : "Queued — waits for a free slot (follow-ups wait for their thread)."));
  box.scrollTop = box.scrollHeight;
}
// ---- plain-English step labels (display only, nothing sent anywhere): each tool call -> [sentence, phase]. Phases split the
// work into task blocks ("Exploring the project (12 steps)", "Editing parser.py (9 steps)"); an active todo / plan step names the block.
const clip = (v, n = 60) => { const t = String(v ?? "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n - 1) + "…" : t; };
const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return clip(u, 40); } };
const where = (p) => { const t = String(p || "").replace(/\/+$/, ""); return !t || t === "." || t === "/workspace" ? "" : t.replace(/^\/workspace\//, ""); };
const TEST_RE = /^(pytest|py\.test|jest|vitest|mocha|tox|nox|phpunit|rspec|ctest)\b|^(python3?|uv run|poetry run)\s+(-m\s+)?(pytest|unittest)\b|^(npm|pnpm|yarn|bun)\s+(run\s+)?test\b|^(go|cargo|deno|bun|dotnet|mix)\s+test\b|^make\s+(test|check)\b/;
const LINT_RE = /^(eslint|ruff|flake8|pylint|black|prettier|mypy|pyright|shellcheck|golangci-lint)\b|^(npm|pnpm|yarn)\s+(run\s+)?(lint|typecheck|format)\b|^tsc\b.*--noEmit|^cargo\s+clippy/;
const BUILD_RE = /^(npm|pnpm|yarn|bun)\s+(run\s+)?build\b|^(cargo|go|dotnet|mvn|gradle)\s+build\b|^make\b|^tsc\b|^vite\s+build|^next\s+build|^cmake\b/;
const INSTALL_RE = /^(pip3?|uv pip|python3? -m pip)\s+install\b|^uv\s+(add|sync)\b|^(npm|pnpm|yarn|bun)\s+(install|i|add|ci)\b|^(apt|apt-get|brew|cargo|go)\s+(install|add|get)\b|^poetry\s+(install|add)\b/;
const SERVE_RE = /^(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start|serve|preview)\b|^(uvicorn|gunicorn|flask run|streamlit run)\b|^python3?\s+-m\s+http\.server|^next\s+(dev|start)/;
function describeCmd(cmd, bg) {
  // the first real command of the line: skip `cd x &&`, exports, env assignments and sudo/timeout/time wrappers
  const segs = String(cmd || "").split(/\s*(?:&&|\|\||;|\n)\s*/).map((x) => x.trim()).filter((x) => x && !/^(cd|pushd|popd|export|source|set|\.)(\s|$)/.test(x));
  let main = (segs[0] || String(cmd || "").trim()).split(/\s+\|\s+/)[0];
  for (let i = 0; i < 3; i++) main = main.replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/, "").replace(/^(sudo|time|nice|env|nohup|timeout\s+\S+|stdbuf\s+\S+)\s+/, "");
  const tk = (main.match(/"[^"]*"|'[^']*'|\S+/g) || []).map((x) => x.replace(/^["']|["']$/g, ""));
  const v = (tk[0] || "").split("/").pop(), args = tk.slice(1).filter((a) => !a.startsWith("-") && !/^[<>|]/.test(a)), flags = tk.slice(1).filter((a) => a.startsWith("-"));
  const f0 = args[0], fb = (x) => baseName(x || "");
  let r;
  if (TEST_RE.test(main)) r = ["Running the tests", "test"];
  else if (LINT_RE.test(main)) r = [/mypy|pyright|tsc|typecheck/.test(main) ? "Type-checking the code" : "Checking the code style", "test"];
  else if (BUILD_RE.test(main)) r = ["Building the project", "test"];
  else if (INSTALL_RE.test(main)) { const pk = args.filter((a) => !/^(install|i|add|ci|sync|pip|get|run)$/.test(a)); r = [pk.length ? `Installing ${clip(pk.slice(0, 3).join(", "), 50)}${pk.length > 3 ? " and more" : ""}` : "Installing dependencies", "setup"]; }
  else if (SERVE_RE.test(main)) r = ["Starting the app", "run"];
  else switch (v) {
    case "ls": case "tree": case "eza": case "exa": case "du": r = [`Listing files in ${where(f0) || "the project"}`, "explore"]; break;
    case "cat": case "head": case "tail": case "less": case "more": case "bat": case "nl": r = [f0 ? `Reading ${fb(f0)}` : "Reading output", "explore"]; break;
    case "sed": r = flags.some((f) => f.startsWith("-i")) ? [`Editing ${fb(args.at(-1))}`, "edit"] : [`Reading ${fb(args.at(-1))}`, "explore"]; break;
    case "awk": case "jq": case "cut": case "sort": case "uniq": r = [args.length > 1 ? `Reading ${fb(args.at(-1))}` : "Filtering output", "explore"]; break;
    case "rg": case "grep": case "egrep": case "ag": case "ack": r = [f0 ? `Searching the code for '${clip(f0, 40)}'` : "Searching the code", "explore"]; break;
    case "find": case "fd": { const i = Math.max(tk.indexOf("-name"), tk.indexOf("-iname")); r = [i > 0 && tk[i + 1] ? `Finding files named ${clip(tk[i + 1], 40)}` : `Finding files in ${where(f0) || "the project"}`, "explore"]; break; }
    case "wc": r = [`Counting lines in ${fb(f0) || "files"}`, "explore"]; break;
    case "diff": case "cmp": r = [`Comparing ${fb(args[0])} and ${fb(args[1])}`, "explore"]; break;
    case "file": case "stat": case "xxd": case "hexdump": case "strings": case "objdump": case "readelf": r = [`Inspecting ${fb(f0)}`, "explore"]; break;
    case "pwd": r = ["Checking the current folder", "explore"]; break;
    case "which": case "type": case "command": r = [`Checking whether ${args.at(-1) || "a tool"} is installed`, "explore"]; break;
    case "git": { const sub = args[0];
      r = sub === "status" ? ["Checking what changed", "explore"] : sub === "diff" || sub === "show" ? ["Looking at the changes", "explore"] : sub === "log" ? ["Reading the git history", "explore"]
        : sub === "add" || sub === "commit" ? ["Committing the changes", "edit"] : sub === "clone" ? [`Downloading ${clip(fb(args[1]).replace(/\.git$/, ""), 40)}`, "setup"]
        : sub === "checkout" || sub === "switch" || sub === "branch" ? ["Switching branches", "run"] : sub === "apply" ? ["Applying a patch", "edit"] : [`Running git ${sub || ""}`.trim(), "run"]; break; }
    case "python": case "python3": case "node": case "deno": case "bun": case "ruby": case "perl": case "php": case "bash": case "sh": case "zsh": case "uv": {
      const lang = v.startsWith("py") ? "Python" : v === "node" || v === "deno" || v === "bun" ? "JavaScript" : v;
      r = tk.includes("-c") || tk.includes("-e") ? [`Running a quick ${lang} snippet`, "run"] : tk[1] === "-m" && tk[2] ? [`Running ${tk[2]}`, "run"]
        : v === "uv" && tk[1] === "run" ? [`Running ${fb(tk[2])}`, "run"] : f0 ? [`Running ${fb(f0)}`, "run"] : [`Running ${v}`, "run"]; break; }
    case "curl": case "wget": case "http": { const u = args.find((a) => /^https?:\/\//.test(a)) || args.find((a) => /^(localhost|127\.0\.0\.1)[:/]/.test(a));
      let loc = null; try { loc = u && new URL(/^https?:/.test(u) ? u : "http://" + u); } catch {}
      r = loc && /^(localhost|127\.0\.0\.1|0\.0\.0\.0)$/.test(loc.hostname) ? [`Testing the app at ${loc.host}${loc.pathname !== "/" ? loc.pathname : ""}`, "test"]
        : [u ? `Fetching ${hostOf(u)}` : "Fetching a URL", "research"]; break; }
    case "mkdir": r = [`Creating the folder ${where(f0) || f0 || ""}`.trim(), "edit"]; break;
    case "rm": case "rmdir": r = [`Deleting ${fb(f0)}${args.length > 1 ? ` and ${args.length - 1} more` : ""}`, "edit"]; break;
    case "cp": r = [`Copying ${fb(args[0])}`, "edit"]; break;
    case "mv": r = [`Moving ${fb(args[0])}${args[1] ? ` to ${fb(args[1])}` : ""}`, "edit"]; break;
    case "touch": r = [`Creating ${fb(f0)}`, "edit"]; break;
    case "chmod": case "chown": r = [`Changing permissions on ${fb(args.at(-1))}`, "edit"]; break;
    case "echo": case "printf": case "tee": case "cat>": { const m = main.match(/>{1,2}\s*(\S+)/); r = m ? [`Writing ${fb(m[1])}`, "edit"] : v === "tee" && f0 ? [`Writing ${fb(f0)}`, "edit"] : ["Printing a message", "run"]; break; }
    case "tar": case "unzip": case "gunzip": case "7z": r = v === "tar" && /c/.test(flags.join("")) ? ["Packing files", "edit"] : [`Unpacking ${fb(args.find((a) => /\.(zip|tar|gz|tgz|xz|bz2|7z)$/.test(a)) || f0)}`, "setup"]; break;
    case "zip": r = [`Zipping ${fb(args[1] || f0)}`, "edit"]; break;
    case "docker": case "podman": r = [`Running docker ${args[0] || ""}`.trim(), "run"]; break;
    case "ps": case "top": case "htop": case "pgrep": case "lsof": case "ss": case "netstat": r = ["Checking running processes", "explore"]; break;
    case "kill": case "pkill": case "killall": r = ["Stopping a process", "run"]; break;
    case "sleep": r = [`Waiting ${f0 || ""}s`, "run"]; break;
    case "sqlite3": case "psql": case "mysql": case "duckdb": r = ["Querying the database", "run"]; break;
    case "ffmpeg": case "ffprobe": case "convert": case "magick": r = ["Processing media", "run"]; break;
    default: r = [`Running ${v || "a command"}`, "run"];
  }
  if (bg) r[0] += " (in the background)";
  return r;
}
const CREW_SAY = {
  wait: () => ["Waiting for the crew", "crew"], status: () => ["Checking on the crew", "crew"], roles: () => ["Looking up the crew roles", "crew"],
  spawn: (i) => [`Starting ${/^[aeiou]/i.test(i.role || "") ? "an" : "a"} ${i.role || "helper"}: ${clip(i.label || i.task, 50)}`, "crew"],
  result: (i) => [`Reading agent #${i.agent_id}'s result`, "crew"], cancel: (i) => [`Stopping agent #${i.agent_id}`, "crew"],
  clock: () => ["Checking the time", "other"], bg_start: (i) => [`Starting '${clip(i.name, 30)}' in the background`, "run"], bg_status: () => ["Checking background tasks", "run"],
  bg_logs: () => ["Reading a background task's log", "run"], bg_stop: () => ["Stopping a background task", "run"],
  repo_map: (i) => [`Mapping the project${i.focus ? ` (focus: ${clip(i.focus, 30)})` : ""}`, "explore"], checkpoint: (i) => [`Saving checkpoint '${clip(i.label, 30)}'`, "other"],
  checkpoints: () => ["Listing checkpoints", "other"], diff: () => ["Reviewing the changes since a checkpoint", "explore"], rollback: () => ["Rolling back to a checkpoint", "edit"],
  state_save: () => ["Saving the mission state", "other"], state_get: () => ["Reading the mission state", "other"], deliver: (i) => [`Delivering a snapshot${i.version ? " " + i.version : ""}`, "other"],
  chats_list: () => ["Looking through earlier chats", "explore"], chat_read: () => ["Reading an earlier chat", "explore"],
  claude_check: () => ["Checking a task for Claude Code", "crew"], claude_run: (i) => [`Handing a task to Claude Code${i.label ? ": " + clip(i.label, 40) : ""}`, "crew"],
  claude_status: () => ["Checking on Claude Code", "crew"], claude_wait: () => ["Waiting for Claude Code", "crew"], claude_result: () => ["Reading Claude Code's result", "crew"], claude_stop: () => ["Stopping Claude Code", "crew"],
};
function describeStep(name, inp) {
  const i = inp && typeof inp === "object" ? inp : {}; name = String(name || "");
  switch (name) {
    case "terminal": return describeCmd(i.command, i.background);
    case "read_file": return [`Reading ${baseName(i.path)}${i.offset > 1 ? ` from line ${i.offset}` : ""}`, "explore"];
    case "write_file": return [`Writing ${baseName(i.path)}`, "edit"];
    case "patch": return [`Editing ${baseName(i.path)}`, "edit"];
    case "search_files": return i.target === "files" ? [`Finding files matching '${clip(i.pattern, 40)}'`, "explore"] : [`Searching the code for '${clip(i.pattern, 40)}'${where(i.path) ? " in " + where(i.path) : ""}`, "explore"];
    case "web_search": return [`Searching the web for '${clip(i.query, 60)}'`, "research"];
    case "web_extract": { const u = [].concat(i.urls || []); return [u.length ? `Reading ${hostOf(u[0])}${u.length > 1 ? ` and ${u.length - 1} more` : ""}` : "Reading web pages", "research"]; }
    case "execute_code": return ["Running Python code", "run"];
    case "process_manage": return [{ poll: "Checking a background process", log: "Reading a background process's output", wait: "Waiting for a background process", kill: "Stopping a background process",
      write: "Sending input to a background process", submit: "Sending input to a background process", list: "Listing background processes" }[i.action] || "Managing a background process", "run"];
    case "skill_view": return [`Loading the ${i.name || ""} skill`, "explore"];
    case "skills_list": return ["Looking through the skills", "explore"];
    case "memory": return [{ add: "Saving a memory", replace: "Updating a memory", remove: "Removing a memory" }[i.action] || "Checking memory", "other"];
    case "session_search": return [i.query ? `Searching past conversations for '${clip(i.query, 40)}'` : "Looking through past conversations", "explore"];
    case "clarify": return ["Asking you a question", "other"];
    case "vision_analyze": return ["Looking at an image", "explore"];
    case "todo": case "todo_list": return ["Updating the task list", "other"];
    case "browser_navigate": return [`Opening ${hostOf(i.url)}`, "research"];
    case "browser_click": return ["Clicking on the page", "research"];
    case "browser_type": return [`Typing '${clip(i.text, 30)}'`, "research"];
    case "browser_snapshot": return ["Reading the page", "research"];
    case "browser_scroll": return [`Scrolling ${i.direction || ""}`.trim(), "research"];
    case "browser_back": return ["Going back a page", "research"];
    case "browser_press": return [`Pressing ${i.key || "a key"}`, "research"];
    case "browser_vision": return ["Looking at the page", "research"];
    case "browser_console": return ["Running JavaScript in the page", "research"];
    case "browser_get_images": return ["Collecting the page's images", "research"];
  }
  if (name.startsWith("mcp__rag__")) return [i.query ? `Searching your documents for '${clip(i.query, 50)}'` : "Looking through your documents", "research"];
  const c = name.replace(/^mcp__crew__/, "");
  if (c !== name && CREW_SAY[c]) return CREW_SAY[c](i);
  return [`Using ${name.replace(/^mcp__[a-z0-9]+__/i, "").replace(/_/g, " ")}`, "other"];
}
// a step fits the open block when it is the same kind of work; reading around while editing or testing stays in that block
const fitsPhase = (g, s) => !s || s === "other" || !g || g === "other" || g === s || (s === "explore" && (g === "edit" || g === "test"));
function groupTitle(g, items) {
  if (g.dataset.task) return g.dataset.task;
  const says = items.map((d) => d.dataset.say || d.dataset.name);
  if (items.length === 1 || new Set(says).size === 1) return says[0] || "Working";
  const edited = [...new Set(items.filter((d) => d.dataset.phase === "edit" && /^(Editing|Writing) /.test(d.dataset.say || "")).map((d) => d.dataset.say.replace(/^(Editing|Writing) /, "")))];
  switch (g.dataset.phase) {
    case "explore": return "Exploring the project";
    case "edit": return edited.length === 1 ? `Editing ${edited[0]}` : edited.length === 2 ? `Editing ${edited[0]} and ${edited[1]}` : edited.length ? `Editing ${edited.length} files` : "Making changes";
    case "test": return says.every((x) => x === "Running the tests" || !/^(Building|Checking|Type-checking)/.test(x)) ? "Running the tests" : "Testing and checking the code";
    case "research": { const w = says.find((x) => x.startsWith("Searching the web for ")); return w ? "Researching " + w.slice(22) : "Browsing the web"; }
    case "setup": return "Setting up";
    case "crew": return "Coordinating the crew";
    case "run": return "Running commands";
    default: return "Working";
  }
}
function lastSentence(raw) {
  const parts = String(raw || "").trim().split(/(?<=[.!?])\s+|\n+/).map((x) => x.trim()).filter((x) => x.length > 3);
  return clip(parts.at(-1) || "", 160);
}
function thinkBlock(box) {
  // reasoning goes inside the open task block (so it never splits one), or stands alone before the first step
  const last = box.lastElementChild, host = last && last.classList.contains("toolgroup") ? last.querySelector(".tg-body") : box;
  let th = host.lastElementChild;
  if (!th || !th.classList.contains("thinking")) {
    th = el("details", "thinking"); th.dataset.raw = ""; th.open = !!box.dataset.live; const sm = el("summary");   // open while streaming, collapsed in history
    sm.append(el("span", "th-label", "Thinking"), el("span", "th-last", "")); th.append(sm, el("div", "th-body")); host.append(th);
  }
  return th;
}
// ---- transcript events: text streams as markdown; tool calls fold into Claude-style activity groups
// ("Ran 3 commands, read 2 files, edited app.py +12 −3"); progress / plan / todo calls render as inline progress lines.
const QUIET = new Set(["mcp__crew__progress", "mcp__crew__plan_set", "mcp__crew__plan_update", "todo_list", "todo"]);
const baseName = (p) => String(p || "").split("/").filter(Boolean).pop() || String(p || "");
const toolCat = (n) => n === "terminal" || n === "execute_code" || n === "process_manage" ? "run" : n === "read_file" ? "read"
  : n === "search_files" || n === "mcp__crew__repo_map" ? "search" : n === "write_file" ? "create" : n === "patch" ? "edit"
  : n === "web_search" ? "web" : n === "web_extract" || n.startsWith("browser_") ? "page" : n === "mcp__crew__spawn" ? "spawn"
  : n === "mcp__crew__wait" || n === "mcp__crew__status" || n === "mcp__crew__result" ? "crew" : n.startsWith("mcp__crew__bg") ? "bg"
  : /mcp__crew__(checkpoint|diff|rollback|state)/.test(n) ? "ckpt" : n.startsWith("mcp__rag") ? "docs" : n === "skill_view" || n === "skills_list" ? "skill" : "other";
const plural = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
function toolGroup(box, phase) {
  const last = box.lastElementChild, task = box.dataset.task || "";
  if (last && last.classList.contains("toolgroup") && (last.dataset.task || "") === task && (task || fitsPhase(last.dataset.phase, phase))) {
    if ((!last.dataset.phase || last.dataset.phase === "other") && phase) last.dataset.phase = phase;
    return last;
  }
  const g = el("details", "toolgroup"); g.dataset.task = task; g.dataset.phase = phase || "other"; const sm = el("summary");
  const head = el("span", "tg-head"); head.append(el("span", "tg-title"), el("span", "tg-text"));
  sm.append(el("span", "tg-ic"), head, el("span", "tg-chev", "›"));
  g.append(sm, el("div", "tg-body")); box.append(g); return g;
}
function diffStat(node, add, del) { const w = el("span", "dstat"); if (add) w.append(el("span", "add", `+${add}`)); if (del) w.append(el("span", "del", `−${del}`)); if (add || del) node.append(" ", w); }
function updateGroup(g) {
  const items = [...g.querySelectorAll(":scope > .tg-body > details.tool")];
  const cnt = {}, files = { create: new Map(), edit: new Map() };
  for (const d of items) {
    const c = toolCat(d.dataset.name || ""); cnt[c] = (cnt[c] || 0) + 1;
    if (c === "create" || c === "edit") { const f = d.dataset.file || "file"; const o = files[c].get(f) || { add: 0, del: 0 }; o.add += +(d.dataset.add || 0); o.del += +(d.dataset.del || 0); files[c].set(f, o); }
  }
  const parts = [];
  const phrase = { run: (k) => `ran ${plural(k, "command")}`, read: (k) => `read ${plural(k, "file")}`, search: (k) => `searched the code ${k}×`,
    web: (k) => `searched the web ${k}×`, page: (k) => `read ${plural(k, "page")}`, spawn: (k) => `spawned ${plural(k, "agent")}`, crew: () => "checked on the crew",
    bg: (k) => plural(k, "background task call"), ckpt: () => "checkpointed", docs: (k) => `searched documents ${k}×`, skill: (k) => `loaded ${plural(k, "skill")}`, other: (k) => `used ${plural(k, "tool")}` };
  for (const c of ["run", "read", "search", "web", "page", "docs", "skill", "spawn", "crew", "bg", "ckpt", "other"]) if (cnt[c]) parts.push(document.createTextNode(phrase[c](cnt[c])));
  for (const [c, verb] of [["create", "created"], ["edit", "edited"]]) {
    if (!files[c].size) continue;
    const n = el("span"); n.append(`${verb} `); let i = 0;
    for (const [f, o] of files[c]) { if (i++) n.append(", "); n.append(el("code", "fname", f)); diffStat(n, o.add, o.del); }
    parts.push(n);
  }
  const title = g.querySelector(".tg-title");
  if (title) title.textContent = groupTitle(g, items) + (items.length > 1 ? ` (${items.length} steps)` : "");
  const txt = g.querySelector(".tg-text"); txt.replaceChildren(); txt.classList.toggle("hidden", items.length < 2 && !!title);
  parts.forEach((p, i) => { if (i) txt.append(", "); txt.append(p); });
  if (txt.firstChild?.nodeType === 3) txt.firstChild.textContent = txt.firstChild.textContent.replace(/^./, (c) => c.toUpperCase());
  else if (txt.firstChild?.firstChild?.nodeType === 3) txt.firstChild.firstChild.textContent = txt.firstChild.firstChild.textContent.replace(/^./, (c) => c.toUpperCase());
  const pend = items.find((d) => d.classList.contains("pending")), errs = items.filter((d) => d.classList.contains("err")).length;
  g.classList.toggle("busy", !!pend);
  if (errs) txt.append(" ", el("span", "tg-err", `· ${errs} failed`));
  if (pend && items.length > 1) txt.append(" ", el("span", "tg-now", `· now: ${pend.dataset.say || pend.dataset.name}`));
}
const STEP_MARK = { done: "✓", active: "◔", blocked: "✕", skipped: "–", pending: "○", in_progress: "◔", completed: "✓", cancelled: "–" };
function planCard(title, steps) {
  const c = el("div", "plancard"); c.append(el("div", "pc-t", title));
  const ol = el("ol"); steps.forEach((st) => { const li = el("li", st.status || "pending"); li.append(el("span", "mk", STEP_MARK[st.status] || "○"), el("span", null, st.title || st.content || "")); ol.append(li); });
  c.append(ol); return c;
}
function quietEvent(box, t, e) {
  const n = e.data.name, inp = e.data.input || {};
  t.plan = t.plan || {};
  if (n === "mcp__crew__progress") {
    const d = el("div", "prog"); d.append(el("span", "pi", "◆"), el("span", "pm", String(inp.message || "")), el("span", "pt", fmtClock(e.ts))); box.append(d);
  } else if (n === "mcp__crew__plan_set") {
    const steps = Array.isArray(inp.steps) ? inp.steps : []; steps.forEach((st) => (t.plan[st.id] = st.title));
    const card = planCard(`Plan · ${steps.length} steps`, steps); card.classList.add("live-plan");
    card.querySelectorAll("li").forEach((li, i) => (li.dataset.sid = steps[i]?.id || ""));
    box.querySelectorAll(".plancard.live-plan").forEach((c) => c.classList.remove("live-plan")); box.append(card);
  } else if (n === "mcp__crew__plan_update") {
    const d = el("div", "prog step " + (inp.status || "")); const title = t.plan[inp.step_id] || inp.step_id;
    d.append(el("span", "pi", STEP_MARK[inp.status] || "•"), el("span", "pm"), el("span", "pt", fmtClock(e.ts)));
    d.querySelector(".pm").append(el("b", null, title), inp.note ? ` — ${inp.note}` : (inp.status === "active" ? " — started" : ""));
    box.append(d);
    if (inp.status === "active") box.dataset.task = title;
    else if (box.dataset.task === title) delete box.dataset.task;
    const li = [...box.querySelectorAll(".plancard.live-plan li")].find((x) => x.dataset.sid === String(inp.step_id));
    if (li) { li.className = inp.status || "pending"; li.querySelector(".mk").textContent = STEP_MARK[inp.status] || "○"; }
  } else if (n === "todo_list" || n === "todo") {
    const todos = Array.isArray(inp.todos) ? inp.todos : []; if (todos.length) box.append(planCard("Task list", todos));
    const act = todos.find((x) => x.status === "in_progress");
    if (act) box.dataset.task = clip(act.content || act.title, 80);
    else if (todos.length && todos.every((x) => ["completed", "cancelled", "done"].includes(x.status))) delete box.dataset.task;
  }
}
function appendEvent(box, t, e, live) {
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (live) box.dataset.live = "1"; else delete box.dataset.live;
  if (e.etype === "text") {
    let last = box.lastElementChild;
    if (!last || !last.classList.contains("streaming")) { last = el("div", "msg assistant streaming"); last.dataset.raw = ""; box.append(last); }
    last.dataset.raw += e.data; last.innerHTML = md(last.dataset.raw);
  } else if (e.etype === "tool_use") {
    if (QUIET.has(e.data.name)) { quietEvent(box, t, e); }
    else {
      const [say, phase] = describeStep(e.data.name, e.data.input);
      const g = toolGroup(box, phase), body = g.querySelector(".tg-body");
      const d = el("details", "tool pending"); d.dataset.callId = e.data.id || ""; d.dataset.name = e.data.name; d.dataset.input = JSON.stringify(e.data.input ?? null);
      d.dataset.say = say; d.dataset.phase = phase;
      const inp = e.data.input || {};
      if (e.data.name === "write_file" || e.data.name === "patch") d.dataset.file = baseName(inp.path || inp.file_path);
      if (e.data.name === "write_file") d.dataset.add = String((inp.content || "").split("\n").length);
      const sm = el("summary"); sm.title = e.data.name; sm.append(el("span", "say", say), el("span", "arg", e.data.name === "terminal" ? summarizeInput(e.data.input) : ""), el("span", "muted small", ""));
      d.append(sm, el("pre", null, JSON.stringify(e.data.input, null, 2))); body.append(d); updateGroup(g);
    }
  } else if (e.etype === "tool_result") {
    if (QUIET.has(e.data.name)) { if (e.data.is_error) box.append(el("div", "prog err", `${e.data.name} failed: ${String(e.data.output || "").slice(0, 200)}`)); }
    else {
      const cand = [...box.querySelectorAll("details.tool.pending")].find((d) => (e.data.id && d.dataset.callId === e.data.id) || (!e.data.id && d.dataset.name === e.data.name));
      let d = cand;
      if (!d) { const g = toolGroup(box); d = el("details", "tool"); d.dataset.name = e.data.name; d.dataset.say = describeStep(e.data.name, null)[0]; const sm = el("summary"); sm.append(el("span", "say", d.dataset.say), el("span", "arg", ""), el("span", "muted small", "")); d.append(sm); g.querySelector(".tg-body").append(d); }
      d.classList.remove("pending"); if (e.data.is_error) d.classList.add("err");
      d.querySelector("summary .small").textContent = e.data.ms != null ? `${(e.data.ms / 1000).toFixed(1)}s` : "";
      if (e.data.name === "patch") { try { const o = JSON.parse(e.data.output); const lines = String(o.diff || "").split("\n"); d.dataset.add = lines.filter((l) => l.startsWith("+") && !l.startsWith("+++")).length; d.dataset.del = lines.filter((l) => l.startsWith("-") && !l.startsWith("---")).length; } catch {} }
      d.append(el("pre", null, prettyOut(e.data.output)));
      const g = d.closest(".toolgroup"); if (g) updateGroup(g);
    }
  } else if (e.etype === "thinking") {
    const th = thinkBlock(box); th.dataset.raw += e.data;
    th.querySelector(".th-body").textContent = th.dataset.raw; th.querySelector(".th-last").textContent = lastSentence(th.dataset.raw);
  } else if (e.etype === "log") {
    let last = box.lastElementChild;
    if (!last || !last.classList.contains("loglines")) { last = el("pre", "loglines"); box.append(last); }
    last.textContent += (last.textContent ? "\n" : "") + e.data;
  }
  if (live && stick) box.scrollTop = box.scrollHeight;
  if (live) renderLive();
}
function summarizeInput(inp) { if (!inp) return ""; const v = inp.query || inp.command || inp.url || inp.urls?.join(" ") || inp.path || inp.code || inp.prompt || JSON.stringify(inp); return String(v).replace(/\s+/g, " ").slice(0, 160); }
function prettyOut(s) { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; } }

// ---- markdown: marked (GFM) -> DOM sanitizer (whitelist) -> code blocks (highlight.js, copy) -> ```chart cards
const MD_TAGS = new Set(["P","BR","STRONG","B","EM","I","DEL","S","CODE","PRE","UL","OL","LI","H1","H2","H3","H4","H5","H6","BLOCKQUOTE","TABLE","THEAD","TBODY","TR","TH","TD","A","HR","INPUT","SUP","SUB","KBD"]);
const marked_ = window.marked;
if (marked_) marked_.setOptions({ gfm: true, breaks: true, async: false });
function sanitize(root) {
  for (const n of [...root.childNodes]) {
    if (n.nodeType === 3) continue;
    if (n.nodeType !== 1 || !MD_TAGS.has(n.tagName)) { n.replaceWith(document.createTextNode(n.textContent)); continue; }
    for (const a of [...n.attributes]) {
      if (n.tagName === "A" && a.name === "href") { const ws = wsPath(a.value); if (ws) { n.setAttribute("data-raw-href", ws); n.removeAttribute("href"); continue; } }
      if (n.tagName === "A" && a.name === "data-raw-href") continue;
      const keep = (n.tagName === "A" && a.name === "href" && /^(https?:|mailto:)/i.test(a.value)) || (n.tagName === "CODE" && a.name === "class" && /^language-[\w+#.-]+$/.test(a.value))
        || (n.tagName === "INPUT" && (a.name === "type" && a.value === "checkbox" || a.name === "checked" || a.name === "disabled")) || ((n.tagName === "TD" || n.tagName === "TH") && a.name === "align");
      if (!keep) n.removeAttribute(a.name);
    }
    if (n.tagName === "A") {
      const raw = n.getAttribute("data-raw-href") || "";
      if (raw) { n.dataset.wsfile = raw; n.href = BASE + "api/files?path=" + encodeURIComponent(raw); n.setAttribute("download", ""); n.classList.add("wsfile"); n.removeAttribute("data-raw-href"); }
      else { n.target = "_blank"; n.rel = "noopener noreferrer"; }
    }
    if (n.tagName === "INPUT") { n.disabled = true; if (n.type !== "checkbox") { n.remove(); continue; } n.closest("li")?.classList.add("task"); }
    sanitize(n);
  }
}
function md(src, rich = false) {
  const t = document.createElement("template");
  try { t.innerHTML = marked_ ? marked_.parse(src || "") : `<p>${esc(src).replace(/\n/g, "<br>")}</p>`; }
  catch { t.innerHTML = `<p>${esc(src).replace(/\n/g, "<br>")}</p>`; }
  sanitize(t.content);
  for (const dead of t.content.querySelectorAll("a:not([href])")) { const sp = el("span", "deadlink", dead.textContent); dead.replaceWith(sp); }
  for (const tb of t.content.querySelectorAll("table")) { const w = el("div", "tblwrap"); tb.replaceWith(w); w.append(tb);
    for (const tr of tb.rows) for (const c of tr.cells) if (/^\s*[-+]?[\d.,]+\s*(%|[a-zA-Z]{0,4})?\s*$/.test(c.textContent) && c.textContent.trim()) c.classList.add("num"); }
  for (const pre of t.content.querySelectorAll("pre")) decorateCode(pre, rich);
  return t.innerHTML;
}
function decorateCode(pre, rich) {
  const code = pre.querySelector("code"); if (!code) { pre.classList.add("plain"); return; }
  const lang = (code.className.match(/language-([\w+#.-]+)/) || [])[1] || "";
  const text = code.textContent;
  if (lang === "chart") { const card = chartCard(text, rich); pre.replaceWith(card); return; }
  const box = el("div", "codeblock" + (text.length < 2000 && !/\n.{120,}/.test(text) ? "" : " wrap"));
  const head = el("div", "cb-head"); head.append(el("span", "lang", lang || "text"), el("span", "muted", `${text.split("\n").length} lines`), el("span", "grow"));
  const copy = el("button", null, "copy"); copy.type = "button"; copy.dataset.copy = "1"; head.append(copy);
  pre.replaceWith(box); box.append(head, pre);
  if (rich && window.hljs && text.length < 60000) { try { if (lang && hljs.getLanguage(lang)) { code.innerHTML = hljs.highlight(text, { language: lang }).value; } else if (!lang && text.length < 8000) { code.innerHTML = hljs.highlightAuto(text).value; } } catch {} }
}
async function downloadWs(path, name) {
  const url = BASE + "api/files?path=" + encodeURIComponent(path);
  let blob;
  try { const r = await fetch(url, { credentials: "same-origin" }); if (!r.ok) throw new Error(r.status === 404 ? "file not found in the workspace" : "HTTP " + r.status); blob = await r.blob(); }
  catch (err) { toast("Download failed: " + err.message, true); return; }
  const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
  const touch = matchMedia("(pointer: coarse)").matches;
  if (touch && navigator.canShare && navigator.canShare({ files: [file] })) {      // iPhone/Android: share sheet -> Save to Files
    try { await navigator.share({ files: [file], title: name }); return; } catch (err) { if (err.name === "AbortError") return; }
  }
  const a = document.createElement("a"); const obj = URL.createObjectURL(blob); a.href = obj; a.download = name; a.rel = "noopener"; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(obj), 30000);
  toast("Downloading " + name);
}
document.addEventListener("click", (e) => {
  const f = e.target.closest("a.filecard, a.wsfile, a.att-chip");
  if (f && f.dataset.wsfile) { e.preventDefault(); downloadWs(f.dataset.wsfile, f.dataset.wsfile.split("/").pop()); return; }
  const b = e.target.closest("button[data-copy]"); if (!b) return;
  const code = b.closest(".codeblock")?.querySelector("pre code"); if (!code) return;
  navigator.clipboard?.writeText(code.textContent).then(() => { b.textContent = "copied"; setTimeout(() => (b.textContent = "copy"), 1500); }).catch(() => toast("Clipboard blocked", true));
});
// a link target that means "a file in the agent's workspace", in any of the forms the model tends to write:
// /workspace/x, sandbox:/workspace/x, file:///workspace/x, /mnt/data/local-agent/workspace/x, workspace/x, ./outbox/x, outbox/x
function wsPath(href) {
  let h = String(href || "").trim().replace(/^(sandbox:|file:\/\/)/i, "");
  try { h = decodeURIComponent(h); } catch {}
  if (/^https?:\/\//i.test(h)) { try { const u = new URL(h); if (u.pathname.startsWith("/workspace/")) h = u.pathname; else return null; } catch { return null; } }
  h = h.replace(/^\/mnt\/data\/local-agent\/workspace\//, "/workspace/").replace(/^\.?\/?workspace\//, "/workspace/");
  if (/^\.?\/?(outbox|uploads|data|docs|reports|output|outputs)\//i.test(h)) h = "/workspace/" + h.replace(/^\.?\/+/, "");
  return /^\/workspace\/[^\s"'<>]+$/.test(h) ? h : null;
}
// ---- files the agent handed over: links to /workspace/... (and `/workspace/...` code spans) -> download cards
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(2)} GB`;
const FILE_ICON = (name) => { const e = (name.split(".").pop() || "").toLowerCase();
  return /^(png|jpe?g|gif|webp|svg|bmp)$/.test(e) ? "img" : /^(zip|tar|gz|tgz|7z|xz|bz2)$/.test(e) ? "zip" : /^(csv|tsv|xlsx?|parquet|json|jsonl)$/.test(e) ? "data"
    : /^(pdf|docx?|md|txt|html?)$/.test(e) ? "doc" : /^(py|js|ts|sh|go|rs|java|c|cpp|rb|php|sql)$/.test(e) ? "code" : "file"; };
async function attachFiles(msgEl) {
  const paths = new Set();
  msgEl.querySelectorAll("a[data-wsfile]").forEach((a) => paths.add(a.dataset.wsfile));
  msgEl.querySelectorAll(":not(pre) > code").forEach((c) => { const t = c.textContent.trim(); if (/^\/workspace\/[^\s]+\.[\w]{1,8}$/.test(t)) paths.add(t); });
  for (const m of msgEl.textContent.matchAll(/(?:\/mnt\/data\/local-agent)?\/workspace\/[\w./ -]+?\.[A-Za-z0-9]{1,8}(?=[\s)\]"'`,;:]|$)/g)) { const ws = wsPath(m[0]); if (ws) paths.add(ws); }
  if (!paths.size) return;
  let files = [];
  try { files = (await api("api/files/stat", { json: { paths: [...paths].slice(0, 60) } })).files; } catch { return; }
  if (!files.length) return;
  const wrap = el("div", "filecards");
  for (const f of files) {
    const a = el("a", "filecard"); a.href = BASE + "api/files?path=" + encodeURIComponent(f.path); a.setAttribute("download", f.name); a.dataset.wsfile = f.path;
    const ic = el("span", "fc-ic " + FILE_ICON(f.name), (f.name.split(".").pop() || "file").slice(0, 4).toUpperCase());
    const meta = el("span", "fc-meta"); meta.append(el("span", "fc-name", f.name), el("span", "fc-sub", `${fmtBytes(f.size)} · ${f.path.replace(/^\/workspace\//, "")}`));
    const dl = el("span", "fc-dl"); dl.innerHTML = '<svg viewBox="0 0 16 16"><path d="M7.25 1.75a.75.75 0 0 1 1.5 0v7.19l2.22-2.22a.75.75 0 1 1 1.06 1.06l-3.5 3.5a.75.75 0 0 1-1.06 0l-3.5-3.5a.75.75 0 1 1 1.06-1.06l2.22 2.22V1.75ZM2.75 13.5h10.5a.75.75 0 0 1 0 1.5H2.75a.75.75 0 0 1 0-1.5Z"/></svg>';
    a.append(ic, meta, dl); wrap.append(a);
  }
  msgEl.append(wrap);
}
// ```chart  {"type":"bar|line|pie|doughnut|scatter","title":"…","labels":[…],"series":[{"name":"…","data":[…]}],"x":"…","y":"…","stacked":false,"horizontal":false}
const CHART_COLORS = ["#58A6FF", "#BC8CFF", "#39C5CF", "#3FB950", "#D29922", "#F85149", "#F778BA", "#79C0FF"];
let chartSeq = 0;
function chartCard(text, rich) {
  const card = el("div", "chartcard"); let spec;
  try { spec = JSON.parse(text); } catch (err) { card.append(el("div", "cerr", "chart: invalid JSON — " + err.message), el("pre", "plain", text)); return card; }
  if (spec.title) card.append(el("div", "ct", String(spec.title)));
  const wrap = el("div", "cwrap"); const c = el("canvas"); c.id = "chart-" + (++chartSeq); wrap.append(c); card.append(wrap);
  if ((spec.series || []).length > 4 || (spec.labels || []).length > 12) card.classList.add("tall");
  card.dataset.spec = JSON.stringify(spec);
  return card;
}
function mountCharts(root) {
  if (!window.Chart) return;
  for (const card of root.querySelectorAll(".chartcard[data-spec]")) {
    const canvas = card.querySelector("canvas"); if (!canvas || canvas.dataset.mounted) continue; canvas.dataset.mounted = "1";
    let spec; try { spec = JSON.parse(card.dataset.spec); } catch { continue; }
    try { buildChart(canvas, spec); } catch (err) { card.append(el("div", "cerr", "chart error: " + err.message)); }
  }
}
function buildChart(canvas, spec) {
  const type = ["bar", "line", "pie", "doughnut", "scatter", "radar", "polarArea"].includes(spec.type) ? spec.type : "bar";
  const labels = Array.isArray(spec.labels) ? spec.labels.map(String) : [];
  const series = Array.isArray(spec.series) && spec.series.length ? spec.series : [{ name: spec.name || "", data: spec.data || spec.values || [] }];
  const pie = type === "pie" || type === "doughnut" || type === "polarArea";
  const datasets = series.map((sr, i) => {
    const col = CHART_COLORS[i % CHART_COLORS.length];
    const data = (sr.data || []).map((v) => (v && typeof v === "object") ? v : (v == null ? null : Number(v)));
    return { label: String(sr.name ?? sr.label ?? `series ${i + 1}`), data,
      backgroundColor: pie ? data.map((_, k) => CHART_COLORS[k % CHART_COLORS.length] + "cc") : (type === "line" ? col + "33" : col + "cc"),
      borderColor: pie ? "#0D1117" : col, borderWidth: type === "line" ? 2 : 1, tension: 0.25, fill: type === "line" ? !!spec.fill : undefined, pointRadius: type === "scatter" ? 4 : 2 };
  });
  const grid = { color: "#30363D", borderColor: "#30363D" }, ticks = { color: "#8B949E", font: { size: 11 } };
  const opts = { responsive: true, maintainAspectRatio: false, animation: false, indexAxis: spec.horizontal ? "y" : "x",
    plugins: { legend: { display: pie || datasets.length > 1, labels: { color: "#C9D1D9", boxWidth: 12, font: { size: 11 } } }, tooltip: { backgroundColor: "#161B22", borderColor: "#30363D", borderWidth: 1, titleColor: "#E6EDF3", bodyColor: "#C9D1D9" } },
    scales: pie || type === "radar" ? {} : { x: { grid, ticks, stacked: !!spec.stacked, title: { display: !!spec.x, text: spec.x || "", color: "#8B949E" }, type: type === "scatter" ? "linear" : "category" },
      y: { grid, ticks, stacked: !!spec.stacked, beginAtZero: spec.zero !== false, title: { display: !!spec.y, text: spec.y || "", color: "#8B949E" } } } };
  new Chart(canvas, { type, data: { labels, datasets }, options: opts });
}
// ---- composer: skills, options, auto-grow, image attachments
let allSkills = [];   // every enabled Hermes skill (name, category, short) — fetched once, refreshed after imports
const extraSkills = new Set();
function skillChip(name, checked = false) { const l = el("label", "chip"); const i = el("input"); i.type = "checkbox"; i.value = name; i.checked = checked; l.append(i, document.createTextNode(name)); return l; }
function renderSkills() {
  const box = $("#skill-chips"), checked = new Set($$("#skill-chips input:checked").map((i) => i.value));
  box.replaceChildren(...skills.map((s) => skillChip(s, checked.has(s))), ...[...extraSkills].map((s) => skillChip(s, checked.has(s) || true)));
  const shown = new Set([...skills, ...extraSkills]);
  const rest = allSkills.filter((s) => s.enabled && !shown.has(s.name));
  if (!rest.length) return;
  const add = el("span", "chip add"), sel = el("select"); sel.title = "preload another installed skill";
  sel.append(new Option("+ skill…", ""));
  const byCat = {}; rest.forEach((s) => (byCat[s.category || "other"] = byCat[s.category || "other"] || []).push(s));
  Object.keys(byCat).sort().forEach((c) => { const g = el("optgroup"); g.label = c; byCat[c].forEach((s) => { const o = new Option(s.name, s.name); o.title = s.short; g.append(o); }); sel.append(g); });
  sel.addEventListener("change", () => { if (sel.value) { extraSkills.add(sel.value); renderSkills(); } });
  add.append(sel); box.append(add);
}
async function loadAllSkills() { try { const r = await api("api/skills"); allSkills = r.skills || []; skillBundles = r.bundles || {}; renderSkills(); } catch {} }
let skillBundles = {};
function renderModes() {
  const rs = $("#role-sel"); rs.replaceChildren();
  const byClass = {}; crewInfo.roles.forEach((r) => (byClass[r.class] = byClass[r.class] || []).push(r));
  Object.entries(byClass).forEach(([c, rr]) => { const g = el("optgroup"); g.label = c; rr.forEach((r) => { const o = el("option", null, `${r.name} — ${r.title}`); o.value = r.name; g.append(o); }); rs.append(g); });
  try { const m = localStorage.getItem("la_mode"); if (m) $("#mode-sel").value = m; const r = localStorage.getItem("la_role"); if (r) rs.value = r; } catch {}
  onModeChange();
}
function currentMode() { const m = $("#mode-sel").value; return m === "specialist" ? { mode: "chat", role: $("#role-sel").value || null } : { mode: m, role: null }; }
function onModeChange() {
  const m = $("#mode-sel").value, spec = m === "specialist";
  $("#role-wrap").classList.toggle("hidden", !spec);
  const md = crewInfo.modes.find((x) => x.name === (spec ? "chat" : m)) || {};
  const role = spec ? crewInfo.roles.find((r) => r.name === $("#role-sel").value) : null;
  const turns = role ? role.max_turns : (md.max_turns || 40);
  $("#max-turns").value = turns;
  const crew = !spec && m !== "chat";
  $("#budget-wrap").classList.toggle("hidden", !crew); $("#deliver-wrap").classList.toggle("hidden", !crew);
  if (crew) { $("#budget-h").value = (md.budget_s || 0) / 3600; $("#budget-h").max = md.max_budget_h || 24; $("#deliver-h").value = md.deliver_every_h ?? 2; }
  const budget = role ? role.budget_s : (md.budget_s || 0);
  $("#mode-hint").textContent = spec ? (role ? `${role.summary} · ${turns} turns · ${Math.round(budget / 60)} min budget` : "") :
    m === "chat" ? "plain Hermes, no crew" : `lead + up to ${Math.max(0, (crewInfo.slots || 1) - 1)} parallel crew · ${turns} turns · ${Math.round(budget / 3600)} h budget · skills: ${(md.skills || []).join(", ") || "—"}`;
  const pill = $("#mode-pill"); pill.textContent = spec ? (role ? role.name : "specialist") : m; pill.classList.toggle("crew", m !== "chat");
  try { localStorage.setItem("la_mode", m); localStorage.setItem("la_role", $("#role-sel").value); } catch {}
}
const EFFORT_LABEL = { auto: "auto", fast: "fast", medium: "medium", high: "high", xhigh: "extra high", max: "max" };
const EFFORT_HINT = { auto: "the decision model picks fast / medium / high for each message", fast: "no thinking, quickest replies", medium: "short thinking (≤4k tokens)", high: "thinks it through (≤12k tokens)",
  xhigh: "long, careful reasoning (≤32k tokens)", max: "extended thinking, unlimited budget, self-verification" };
let effort = null;
function renderEffort() {
  const seg = $("#effort-seg"); seg.replaceChildren(...(crewInfo.efforts || ["fast", "medium", "high", "xhigh", "max"]).map((e) => {
    const b = el("button", e === effort ? "on" : "", EFFORT_LABEL[e]); b.type = "button"; b.title = EFFORT_HINT[e];
    b.onclick = () => { effort = e; try { localStorage.setItem("la_effort", e); } catch {} renderEffort(); }; return b; }));
  const p = $("#effort-pill"); p.textContent = EFFORT_LABEL[effort] || effort; p.className = "pill effort-pill e-" + effort; p.title = `Effort: ${EFFORT_LABEL[effort]} — ${EFFORT_HINT[effort]}`;
}
function initEffort() { try { effort = localStorage.getItem("la_effort"); } catch {} if (!(crewInfo.efforts || []).includes(effort)) effort = "medium"; renderEffort(); }
$("#effort-pill").addEventListener("click", () => $("#composer-opts").classList.remove("hidden"));
$("#mode-sel").addEventListener("change", onModeChange);
$("#role-sel").addEventListener("change", onModeChange);
$("#mode-pill").addEventListener("click", () => $("#composer-opts").classList.remove("hidden"));
$("#opt-btn").addEventListener("click", () => $("#composer-opts").classList.toggle("hidden"));
$("#opts-done").addEventListener("click", () => $("#composer-opts").classList.add("hidden"));
$("#opts-backdrop").addEventListener("click", () => $("#composer-opts").classList.add("hidden"));
const ta = $("#prompt");
function grow() { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, Math.round(window.innerHeight * 0.4)) + "px"; }
ta.addEventListener("input", grow);
function renderAttachments() {
  const row = $("#attach-row");
  row.classList.toggle("hidden", pending.length === 0);
  row.replaceChildren(...pending.map((p) => {
    if (p.type === "file") return fileThumb(p);
    const d = el("div", "thumb" + (p.state === "fail" ? " fail" : ""));
    const im = el("img"); im.src = p.preview; d.append(im);
    if (p.state !== "done" && p.state !== "fail") {
      d.classList.add("busy");
      const lab = el("span", "lab", thumbLabel(p)); d.append(lab); p.lab = lab;
      const bar = el("i", "prog"); bar.style.width = `${Math.round((p.progress || 0) * 100)}%`; d.append(bar); p.bar = bar;
    }
    if (p.state === "fail") d.onclick = () => startUpload(p);
    else if (p.state === "done") im.onclick = () => openLightbox(p.preview);
    const x = el("button", "x", "×"); x.type = "button";
    x.onclick = (ev) => { ev.stopPropagation(); p.xhr?.abort(); pending = pending.filter((q) => q !== p); renderAttachments(); updateSend(); };
    d.append(x); return d;
  }));
}
function fileThumb(p) {
  const d = el("div", "fthumb" + (p.state === "fail" ? " fail" : p.state === "done" ? " done" : " busy"));
  d.append(el("span", "ic", fileIcon(p.name, p.meta?.kind)));
  const t = el("div", "tx"); t.append(el("span", "nm", p.name));
  const sub = p.state === "done" ? (p.meta?.kind === "archive" ? `${fmtSize(p.size)} · ${p.meta.files} files unpacked` : p.meta?.kind === "archive-failed" ? "could not unpack — sent as a file" : fmtSize(p.size))
    : p.state === "fail" ? "failed — tap to retry" : p.state === "finishing" ? (/\.(zip|tar|tgz|txz|tbz2)$|\.tar\.\w+$/i.test(p.name) ? "unpacking…" : "finishing…")
    : p.state === "queued" ? "waiting" : `${Math.round((p.progress || 0) * 100)}% of ${fmtSize(p.size)}`;
  const lab = el("span", "sz", sub); t.append(lab); d.append(t); p.lab = lab;
  if (p.state !== "done" && p.state !== "fail") { const bar = el("i", "prog"); bar.style.width = `${Math.round((p.progress || 0) * 100)}%`; d.append(bar); p.bar = bar; }
  if (p.state === "fail") d.onclick = () => startUpload(p);
  const x = el("button", "x", "×"); x.type = "button"; x.setAttribute("aria-label", "Remove");
  x.onclick = (ev) => { ev.stopPropagation(); p.abort = true; p.xhr?.abort(); pending = pending.filter((q) => q !== p); renderAttachments(); updateSend(); };
  d.append(x); return d;
}
const thumbLabel = (p) => p.state === "preparing" ? "shrinking" : p.state === "queued" ? "waiting" : `${Math.round((p.progress || 0) * 100)}%`;
let sendWhenReady = false;
function updateSend() {
  const btn = $(".send-btn"), busy = pending.filter((p) => p.state !== "done" && p.state !== "fail").length;
  btn.disabled = !ta.value.trim() || sendWhenReady;
  btn.classList.toggle("waiting", busy > 0 || sendWhenReady);
  btn.title = sendWhenReady ? "Will send when uploads finish" : busy ? `${busy} upload(s) still running — tap to send when done` : "Send";
  $("#send-note").textContent = sendWhenReady ? `Sending when ${busy} upload${busy === 1 ? "" : "s"} finish…` : busy ? `Uploading ${busy} file${busy === 1 ? "" : "s"}…` : "";
  if (sendWhenReady && pending.some((p) => p.state === "fail")) sendWhenReady = false;
  if (sendWhenReady && busy === 0) {
    sendWhenReady = false;
    if (pending.some((p) => p.state === "fail")) toast("An upload failed — tap it to retry, or × to send without it", true);
    else $("#composer").requestSubmit();
  }
}
ta.addEventListener("input", updateSend);

// Phones send 3-14 MB photos; the tunnel carries ~35 KB/s inbound, so shrink on the device first:
// <=1600 px JPEG q0.82 (~200-400 KB for a real photo). Drawing through an <img> also converts
// iPhone HEIC and applies EXIF rotation; re-encoding drops EXIF/GPS. Falls back to the original.
async function prepareImage(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image(); img.src = url; await img.decode();
    const k = Math.min(1, 1280 / Math.max(img.naturalWidth, img.naturalHeight));
    const c = document.createElement("canvas"); c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.8));
    return blob && blob.size < file.size ? blob : file;
  } catch { return file; } finally { URL.revokeObjectURL(url); }
}
function uploadBlob(p) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); p.xhr = xhr;
    xhr.open("POST", BASE + "api/uploads"); xhr.setRequestHeader("x-csrf", csrf); xhr.timeout = 150000;
    // No stall detector: iOS Safari fires upload progress rarely, so "no progress" != stuck.
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { p.progress = e.loaded / e.total; if (p.bar) p.bar.style.width = `${Math.round(p.progress * 100)}%`; if (p.lab) p.lab.textContent = thumbLabel(p); } };
    xhr.onload = () => { if (xhr.status === 200) resolve(JSON.parse(xhr.responseText)); else reject(new Error(xhr.status === 413 ? "image too large" : xhr.status === 401 ? "signed out" : `HTTP ${xhr.status}`)); };
    xhr.onerror = () => reject(new Error("network error")); xhr.ontimeout = () => reject(new Error("timed out")); xhr.onabort = () => reject(new Error("aborted"));
    const fd = new FormData(); fd.append("file", p.blob, (p.name || "image").replace(/\.\w+$/, "") + ".jpg"); xhr.send(fd);
  });
}
// Files (code, zips, docs): resumable chunks sized to the measured link speed (~20 s each, 256 KB–8 MB). The tunnel's
// inbound leg is slow and drops requests that run for minutes, so a fixed big chunk would fail forever; a failed
// chunk halves the size and resumes from the server's offset.
const ALREADY_PACKED = /\.(zip|gz|tgz|bz2|tbz2|xz|txz|7z|rar|zst|jar|apk|whl|png|jpe?g|gif|webp|heic|avif|mp[34]|m4a|mov|webm|mkv|ogg|flac|pdf|docx|xlsx|pptx|woff2?)$/i;
const CHUNK_MIN = 256 * 1024, CHUNK_MAX = 8 * 1024 * 1024, CHUNK_SECS = 20;
let chunkSize = 1024 * 1024;
function xhrPost(url, body, onprog) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhrPost.cur = xhr;
    xhr.open("POST", url); xhr.setRequestHeader("x-csrf", csrf); xhr.timeout = 600000;
    if (body instanceof Blob) xhr.setRequestHeader("content-type", "application/octet-stream"); else if (body) xhr.setRequestHeader("content-type", "application/json");
    if (onprog) xhr.upload.onprogress = (e) => { if (e.lengthComputable) onprog(e.loaded); };
    xhr.onload = () => { let d = null; try { d = JSON.parse(xhr.responseText); } catch {} if (xhr.status === 200) resolve(d); else { const er = new Error(d?.detail || (xhr.status === 401 ? "signed out" : `HTTP ${xhr.status}`)); er.status = xhr.status; reject(er); } };
    xhr.onerror = () => reject(new Error("network error")); xhr.ontimeout = () => reject(new Error("timed out")); xhr.onabort = () => reject(new Error("aborted"));
    xhr.send(body instanceof Blob ? body : body ? JSON.stringify(body) : null);
  });
}
async function uploadFileChunked(p) {
  const setProg = (v) => { p.progress = v; if (p.bar) p.bar.style.width = `${Math.round(v * 100)}%`; if (p.lab) p.lab.textContent = `${Math.round(v * 100)}% of ${fmtSize(p.size)}` + (p.rate ? ` · ${fmtSize(p.rate)}/s` : ""); };
  // Plain text/code shrinks 3-10x with gzip, and the tunnel's inbound leg is the bottleneck: compress before sending.
  if (!p.aid && !p.wire && p.size > 32 * 1024 && p.size < 300 * 1024 * 1024 && typeof CompressionStream === "function" && !ALREADY_PACKED.test(p.name)) {
    try {
      if (p.lab) p.lab.textContent = "compressing…";
      const z = await new Response(p.file.stream().pipeThrough(new CompressionStream("gzip"))).blob();
      if (z.size < p.size * 0.9) { p.wire = z; p.encoding = "gzip"; }
    } catch {}
  }
  const blob = p.wire || p.file, total = blob.size;
  if (!p.aid) p.aid = (await xhrPost(BASE + "api/attach/init", { name: p.name, size: total, encoding: p.encoding || "", orig_size: p.encoding ? p.size : 0 })).id;
  let off = p.sent || 0;
  while (off < total) {
    if (p.abort) throw new Error("aborted");
    let tries = 0;
    for (;;) {
      const end = Math.min(total, off + chunkSize), t0 = performance.now();
      try {
        const r = await xhrPost(`${BASE}api/attach/${p.aid}/chunk?offset=${off}`, blob.slice(off, end), (l) => setProg((off + l) / Math.max(1, total)));
        const secs = Math.max(0.2, (performance.now() - t0) / 1000), rate = (r.received - off) / secs;
        if (r.received > off) chunkSize = Math.round(Math.min(CHUNK_MAX, Math.max(CHUNK_MIN, rate * CHUNK_SECS)));
        p.rate = rate; off = r.received; break;
      } catch (err) {
        if (err.message === "aborted" || p.abort) throw err;
        if (err.status === 401) throw err;
        try { const st = await api(`api/attach/${p.aid}`); off = st.received || 0; } catch {}       // resume where the server is
        if (err.status === 409) break;
        chunkSize = Math.max(CHUNK_MIN, Math.floor(chunkSize / 2));
        if (++tries >= 8) throw err;
        await new Promise((r) => setTimeout(r, 800 * 2 ** Math.min(tries, 4)));
      }
    }
    p.sent = off; setProg(off / Math.max(1, total));
  }
  p.state = "finishing"; renderAttachments();
  p.meta = await xhrPost(`${BASE}api/attach/${p.aid}/finish`, null);
  return p.meta;
}
let active = 0; const queue = [];
function startUpload(p) { p.state = "queued"; p.progress = 0; renderAttachments(); updateSend(); queue.push(p); pump(); }
function pump() {
  while (active < 2 && queue.length) {
    const p = queue.shift(); if (!pending.includes(p)) continue; active++;
    p.state = "uploading"; if (p.lab) p.lab.textContent = thumbLabel(p);
    (async () => {
      for (let attempt = 0; attempt < 3; attempt++) {
        try { const r = p.type === "file" ? await uploadFileChunked(p) : await uploadBlob(p); p.id = r.id; p.state = "done"; break; }
        catch (err) { if (err.message === "aborted" || !pending.includes(p)) break; if (attempt === 2 || err.status === 507 || err.status === 422) { p.state = "fail"; sendWhenReady = false; toast(`Upload failed (${err.message}) — tap it to retry`, true); break; } }
      }
      active--; renderAttachments(); updateSend(); pump();
    })();
  }
}
async function addFiles(files) {
  const isImg = (f) => (f.type.startsWith("image/") && !/svg/.test(f.type)) || /\.(heic|heif)$/i.test(f.name);
  const others = files.filter((f) => !isImg(f));
  const fRoom = 20 - pending.filter((p) => p.type === "file").length;
  if (others.length > fRoom) toast("Up to 20 files per message", true);
  const big = others.filter((f) => f.size > 1024 ** 3); if (big.length) toast(`${big[0].name} is over 1 GB`, true);
  const fitems = others.filter((f) => f.size <= 1024 ** 3).slice(0, Math.max(0, fRoom)).map((f) => ({ type: "file", name: f.name, size: f.size, file: f, state: "queued", progress: 0 }));
  if (fitems.length) { pending.push(...fitems); renderAttachments(); updateSend(); fitems.forEach(startUpload); }
  const imgs = files.filter(isImg);
  if (!imgs.length) return;
  const room = 6 - pending.filter((p) => p.type !== "file").length; if (imgs.length > room) toast("Up to 6 images per message", true);
  const items = imgs.slice(0, Math.max(0, room)).map((f) => ({ name: f.name, file: f, state: "preparing", progress: 0, preview: URL.createObjectURL(f) }));
  pending.push(...items); renderAttachments(); updateSend();
  // read every selected file NOW (iOS invalidates the picker's files once the input is reset)
  await Promise.all(items.map(async (p) => { p.blob = await prepareImage(p.file); p.preview = URL.createObjectURL(p.blob); p.file = null; }));
  items.forEach(startUpload);
}
$("#attach-btn").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", async (e) => { const files = [...e.target.files]; await addFiles(files); e.target.value = ""; });
ta.addEventListener("paste", (e) => { const fs = [...e.clipboardData.items].filter((i) => i.kind === "file").map((i) => i.getAsFile()).filter(Boolean); if (fs.length) { e.preventDefault(); addFiles(fs); } });
const comp = $("#composer");
["dragover", "dragenter"].forEach((ev) => comp.addEventListener(ev, (e) => { e.preventDefault(); comp.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => comp.addEventListener(ev, (e) => { e.preventDefault(); if (ev !== "dragleave" || !comp.contains(e.relatedTarget)) comp.classList.remove("drag"); }));
comp.addEventListener("drop", (e) => { if (e.dataTransfer?.files?.length) addFiles([...e.dataTransfer.files]); });

async function sendCommand(prompt, opts = {}) {
  const body = { prompt, skills: opts.skills ?? $$("#skill-chips input:checked").map((i) => i.value), max_turns: +($("#max-turns").value || 0), images: opts.images || [], files: opts.files || [] };
  if (opts.parent) body.parent_id = opts.parent; else Object.assign(body, opts.modeRole || currentMode());
  if (!$("#budget-wrap").classList.contains("hidden")) { body.budget_h = +$("#budget-h").value || 0; body.deliver_every_h = Math.max(0, +$("#deliver-h").value || 0); }
  body.effort = opts.effort || effort || "medium";
  body.auto_skills = $("#auto-skills").checked;
  body.quick = $("#quick-replies").checked;
  body.profile_id = chatProfiles.active;
  const j = await api("api/jobs/agent", { json: body }); onJob(j); return j;
}
$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault(); const p = ta.value.trim(); if (!p) return;
  if (pending.some((x) => x.state === "fail")) return toast("An upload failed — tap it to retry, or × to remove it", true);
  if (pending.some((x) => x.state !== "done")) { sendWhenReady = true; updateSend(); return; }
  const images = pending.filter((x) => x.type !== "file").map((x) => x.id), files = pending.filter((x) => x.type === "file").map((x) => x.id);
  const sel = transcripts.get(selected)?.job;
  let parent = null;
  if (sel && sel.kind === "agent" && $("#followup").checked) {
    if (sel.relation === "child") {            // viewing a crew member: continue the mission (its latest lead turn)
      const lead = jobs.filter((x) => x.root_id === sel.root_id && x.kind === "agent" && x.relation !== "child" && x.session_id).sort((a, b) => b.id - a.id)[0];
      parent = lead ? lead.id : null;
    } else parent = sel.session_id ? sel.id : null;
  }
  try { const j = await sendCommand(p, { parent, images, files }); ta.value = ""; grow(); pending = []; renderAttachments(); updateSend(); selectJob(j.id); } catch (err) { toast(err.message, true); }
});
ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#composer").requestSubmit(); });
// ---- chat sidebar: collapsible on desktop (remembered), slide-over drawer on phones
const isMobile = () => matchMedia("(max-width: 760px)").matches;
function closeDrawer() { $("#console").classList.remove("drawer-open"); }
try { if (localStorage.getItem("la_sidebar") === "collapsed") $("#console").classList.add("collapsed"); } catch {}
$("#sidebar-btn").addEventListener("click", () => {
  const c = $("#console");
  if (isMobile()) c.classList.toggle("drawer-open");
  else { c.classList.toggle("collapsed"); try { localStorage.setItem("la_sidebar", c.classList.contains("collapsed") ? "collapsed" : "open"); } catch {} }
});
$("#drawer-backdrop").addEventListener("click", closeDrawer);
$("#drawer-close").addEventListener("click", closeDrawer);
function msgActions(text) {
  const w = el("div", "msg-actions");
  const b = el("button", "", ""); b.type = "button"; b.title = "Copy"; b.setAttribute("aria-label", "Copy");
  b.innerHTML = '<svg viewBox="0 0 16 16"><path d="M0 6.75C0 5.78.78 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 9.25 16h-7.5A1.75 1.75 0 0 1 0 14.25Z"/><path d="M5 1.75C5 .78 5.78 0 6.75 0h7.5C15.22 0 16 .78 16 1.75v7.5A1.75 1.75 0 0 1 14.25 11h-7.5A1.75 1.75 0 0 1 5 9.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z"/></svg>';
  b.addEventListener("click", async () => { try { await navigator.clipboard.writeText(text || ""); toast("Copied"); } catch { toast("Copy failed", true); } });
  w.append(b); return w;
}
// ---- chat action sheet (phones): what the desktop header shows as buttons
function openChatSheet() {
  const t = transcripts.get(selected), j = t?.job;
  $("#cs-title").textContent = j ? (j.title || `${j.kind} #${j.id}`).slice(0, 40) : "New chat";
  const info = $("#cs-info"); info.replaceChildren();
  const kv = (k, v) => { const r = el("div", "k"); r.append(el("span", null, k), el("b", null, v)); info.append(r); };
  if (j) {
    kv("Status", j.status + (j.started ? ` · ${fmtDur(j.started, j.finished)}` : ""));
    kv("Mode", (j.mode && j.mode !== "chat" ? `${j.mode} mission` : j.role ? j.role : "chat") + (j.effort ? ` · ${EFFORT_LABEL[j.effort] || j.effort}` : ""));
    if (j.tokens) kv("Tokens", `${(j.tokens.total || 0).toLocaleString()}${j.n_tools ? ` · ${j.n_tools} tools` : ""}`);
    const ctx = status?.model?.agent_ctx || status?.model?.ctx || 0, used = j.tokens?.input || 0;
    if (ctx && used && j.kind === "agent") { const f = Math.min(1, used / ctx); kv("Context window", `${Math.round(f * 100)}% · ${(used / 1000).toFixed(0)}k of ${(ctx / 1000).toFixed(0)}k`); const bar = el("div", "ctxbar" + (f > 0.7 ? " hot" : "")); bar.style.setProperty("--w", `${Math.round(f * 100)}%`); bar.append(el("i")); info.append(bar); }
  } else kv("Status", "nothing selected — write below to start");
  info.classList.toggle("hidden", !info.children.length);
  $("#cs-mission").classList.toggle("hidden", $("#mission-btn").classList.contains("hidden"));
  $("#cs-compact").classList.toggle("hidden", $("#tr-compact").classList.contains("hidden"));
  $("#cs-stop").classList.toggle("hidden", $("#tr-cancel").classList.contains("hidden"));
  $("#cs-profile-name").textContent = curProfile().name;
  $("#chat-sheet").classList.remove("hidden"); $("#sheet-backdrop").classList.remove("hidden");
}
function closeChatSheet() { $("#chat-sheet").classList.add("hidden"); $("#sheet-backdrop").classList.add("hidden"); }
$("#tr-more").addEventListener("click", openChatSheet);
$("#cs-close").addEventListener("click", closeChatSheet);
$("#sheet-backdrop").addEventListener("click", closeChatSheet);
$("#cs-new").addEventListener("click", () => { closeChatSheet(); newChatView(); });
$("#cs-chats").addEventListener("click", () => { closeChatSheet(); $("#console").classList.add("drawer-open"); });
$("#cs-mission").addEventListener("click", () => { closeChatSheet(); $("#mission-btn").click(); });
$("#cs-compact").addEventListener("click", () => { closeChatSheet(); $("#tr-compact").click(); });
$("#cs-stop").addEventListener("click", () => { closeChatSheet(); $("#tr-cancel").click(); });
$("#cs-profile").addEventListener("click", () => { closeChatSheet(); $("#console").classList.add("drawer-open"); setTimeout(() => $("#profile-btn").click(), 250); });
// the floating composer / live bar overlay the transcript on phones: keep the transcript's bottom padding in step
const dock = () => { const w = $(".transcript-wrap"), c = $("#composer"), l = $("#livebar"); if (!w || !c) return; const ch = c.offsetHeight, lh = l && !l.classList.contains("hidden") ? l.offsetHeight + 8 : 0; w.style.setProperty("--composer-h", `${ch}px`); w.style.setProperty("--dock-h", `${ch + lh}px`); };
if (window.ResizeObserver) { const ro = new ResizeObserver(dock); ro.observe($("#composer")); ro.observe($("#livebar")); }
addEventListener("resize", dock); setTimeout(dock, 300);
function newChatView(focus = true) {
  selected = null; renderJobList(); closeDrawer();
  $("#tr-title").textContent = "New chat"; $("#tr-meta").textContent = chatProfiles.profiles.length > 1 ? `profile: ${curProfile().name}` : ""; $("#tr-cancel").classList.add("hidden"); $("#followup-wrap").classList.add("hidden");
  $("#tr-ctx")?.classList.add("hidden"); $("#tr-compact")?.classList.add("hidden"); $("#livebar")?.classList.add("hidden"); $("#tr-spin")?.classList.add("hidden");
  mission = null; $("#mission").classList.add("hidden"); $("#mission-btn").classList.add("hidden");
  $("#transcript").replaceChildren(el("p", "muted empty", "New session · write below · attach with + · modes and effort under ⋮"));
  if (focus) ta.focus();
}
$("#new-thread").addEventListener("click", () => newChatView());

// ---- chat profiles: each has its own chat list and its own agent memory / past sessions (model, skills, settings and
// /workspace files are shared). Switching shows that profile's chats; chats keep running in their own profile.
const curProfile = () => chatProfiles.profiles.find((p) => p.id === chatProfiles.active) || { id: 1, name: "Main" };
function renderProfileBtn() { $("#profile-name").textContent = curProfile().name; $("#profile-btn").title = `Profile: ${curProfile().name} (switch / new)`; }
function renderProfileMenu() {
  const m = $("#profile-menu"); m.replaceChildren();
  for (const p of chatProfiles.profiles) {
    const b = el("button", "pm-item" + (p.id === chatProfiles.active ? " on" : "")); b.type = "button";
    b.append(el("span", "pm-check", p.id === chatProfiles.active ? "✓" : ""), el("span", "pm-name", p.name),
      el("span", "pm-meta", `${p.chats} chat${p.chats === 1 ? "" : "s"}${p.busy ? ` · ${p.busy} running` : ""}`));
    b.addEventListener("click", () => switchProfile(p.id)); m.append(b);
  }
  m.append(el("div", "pm-sep"));
  const nb = el("button", "pm-item pm-act", "+ New profile"); nb.type = "button"; nb.addEventListener("click", createProfile);
  const rb = el("button", "pm-item pm-act", "Rename this profile…"); rb.type = "button"; rb.addEventListener("click", renameProfile);
  m.append(nb, rb);
}
function closeProfileMenu() { $("#profile-menu").classList.add("hidden"); }
async function enterProfile(msg) {
  renderProfileBtn(); closeProfileMenu();
  transcripts.clear(); jobs = []; newChatView(false);
  try { await loadJobs(); } catch (err) { toast(err.message, true); }
  if (msg) toast(msg);
}
async function switchProfile(id) {
  if (id === chatProfiles.active) return closeProfileMenu();
  try { chatProfiles = await api(`api/chat-profiles/${id}/use`, { method: "POST", json: {} }); } catch (err) { return toast(err.message, true); }
  await enterProfile(`Switched to ${curProfile().name}`);
}
async function createProfile() {
  closeProfileMenu();
  const name = (prompt("Name for the new profile (it starts with no chats and no agent memory):") || "").trim(); if (!name) return;
  try { chatProfiles = await api("api/chat-profiles", { json: { name } }); } catch (err) { return toast(err.message, true); }
  await enterProfile(`New profile ${curProfile().name} — clean chat history`);
}
async function renameProfile() {
  closeProfileMenu();
  const name = (prompt("Rename this profile:", curProfile().name) || "").trim(); if (!name || name === curProfile().name) return;
  try { chatProfiles = await api(`api/chat-profiles/${chatProfiles.active}/rename`, { json: { name } }); renderProfileBtn(); } catch (err) { toast(err.message, true); }
}
$("#profile-btn").addEventListener("click", async (e) => {
  e.stopPropagation(); const m = $("#profile-menu");
  if (!m.classList.contains("hidden")) return closeProfileMenu();
  try { const fresh = await api("api/chat-profiles"); chatProfiles = { ...fresh, active: chatProfiles.active }; } catch {}
  renderProfileMenu(); m.classList.remove("hidden");
});
document.addEventListener("click", (e) => { if (!e.target.closest(".prof-wrap")) closeProfileMenu(); });
$("#tr-cancel").addEventListener("click", async () => { try { await api(`api/jobs/${selected}/cancel`, { method: "POST", json: {} }); toast("Stop requested"); } catch (err) { toast(err.message, true); } });
$("#quick-form").addEventListener("submit", async (e) => {
  e.preventDefault(); const p = $("#quick-prompt").value.trim(); if (!p) return;
  try { const j = await sendCommand(p, { skills: [], modeRole: { mode: "chat", role: null } }); $("#quick-prompt").value = ""; go("console"); selectJob(j.id); } catch (err) { toast(err.message, true); }
});

// ---- mission panel: plan steps, crew members, background tasks, timeline
const fmtS = (s) => { s = Math.max(0, s | 0); return s < 60 ? `${s}s` : s < 3600 ? `${(s / 60) | 0}m ${s % 60}s` : `${(s / 3600) | 0}h ${((s % 3600) / 60) | 0}m`; };
const fmtClock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
let missionLoading = null;
async function loadMission(rootId, show = true) {
  if (missionLoading) return;
  missionLoading = api(`api/jobs/${rootId}/mission`).then((m) => { mission = m; renderMission(); if (show) $("#mission").classList.remove("hidden"); }).catch(() => {}).finally(() => { missionLoading = null; });
  await missionLoading;
}
let missionDirty = 0;
function onMission(msg) {
  const j = transcripts.get(selected)?.job; if (!j) return;
  if ((j.root_id || j.id) !== msg.root) return;
  clearTimeout(missionDirty); missionDirty = setTimeout(() => loadMission(msg.root, !$("#mission").classList.contains("hidden")), 400);
}
const STEP_ICON = { pending: "○", active: "◔", done: "●", blocked: "✕", skipped: "–" };
function renderMission() {
  const m = mission; if (!m) return;
  const now = Date.now() / 1000;
  $("#m-title").textContent = `${m.mode} mission #${m.root}`;
  const done = m.steps.filter((s) => s.status === "done").length, run = m.members.filter((x) => x.status === "running").length, qd = m.members.filter((x) => x.status === "queued").length;
  $("#m-sum").textContent = `${m.active ? "running" : "finished"} · ${fmtS(m.active ? now - m.started : m.elapsed_s)} · steps ${done}/${m.steps.length} · crew ${run} running${qd ? `, ${qd} queued` : ""}, ${m.members.length} total`;
  $("#m-steps-n").textContent = m.steps.length ? `${done}/${m.steps.length}` : "";
  $("#m-steps").replaceChildren(...(m.steps.length ? m.steps.map((s) => {
    const li = el("li", s.status); li.append(el("span", "ic", STEP_ICON[s.status] || "○"), el("span", "t", s.title));
    const dur = s.started ? fmtS((s.finished || (s.status === "active" ? now : s.updated)) - s.started) : "";
    li.append(el("span", "d", dur)); if (s.note || s.detail) li.append(el("span", "n", s.note || s.detail)); li.title = s.detail || ""; return li;
  }) : [el("li", "ph", "No plan published yet.")]));
  $("#m-crew-n").textContent = m.members.length ? String(m.members.length) : "";
  $("#m-crew").replaceChildren(...(m.members.length ? m.members.map((x) => {
    const tr = el("tr", "click" + (x.id === selected ? " sel" : "")); tr.onclick = () => selectJob(x.id);
    tr.append(el("td", "mono muted", "#" + x.id));
    const td = el("td"); td.append(el("span", "rb", x.role)); td.append(document.createTextNode(" " + (x.label !== x.role ? x.label : ""))); tr.append(td);
    const ts = el("td"); ts.append(el("span", "tag " + x.status, x.status)); tr.append(ts);
    const el_ = x.status === "running" ? fmtS(now - x.started) : x.started ? fmtS(x.elapsed_s) : "";
    tr.append(el("td", "r muted small", el_));
    tr.append(el("td", "muted small", x.status === "running" && x.last_tool ? `${x.n_tools} tools · ${x.last_tool} ${fmtS(x.last_tool_ago_s)} ago` : x.n_tools ? `${x.n_tools} tools` : ""));
    return tr;
  }) : [el("tr", null, "No crew spawned.")]));
  const cl = m.claude || [];
  $("#m-cl-n").textContent = cl.length ? String(cl.length) : "";
  $("#m-claude").replaceChildren(...(cl.length ? cl.map((c) => {
    const d = el("div", "bg claude"); const h = el("div", "bh");
    h.append(el("span", "nm", c.label || c.id), el("span", "rb", `${c.model}/${c.effort}${c.mode === "readonly" ? " · read-only" : ""}`),
      el("span", "tag " + (c.state === "done" ? "done" : c.state === "running" ? "running" : "failed"), c.state),
      el("span", "muted small", (c.state === "running" ? fmtS(now - c.created) : fmtS(c.elapsed_s)) + (c.cost_usd ? ` · $${c.cost_usd.toFixed(2)}` : "") + (c.n_tools ? ` · ${c.n_tools} tools` : "")), el("span", "grow"));
    if (c.state === "running") { const k = el("button", "danger small", "stop"); k.onclick = async () => { try { await api(`api/claude/runs/${c.id}/stop`, { method: "POST", json: {} }); toast("Stopping"); } catch (e) { toast(e.message, true); } }; h.append(k); }
    const br = el("button", "ghost small", "brief"); br.onclick = async () => { try { const r = await api(`api/claude/runs/${c.id}`); showText(`Brief sent to Claude Code (run ${c.id}, ${r.model}/${r.effort}, ${r.mode})` + (r.verdict?.redactions?.length ? `\nredactions: ${r.verdict.redactions.join(", ")}` : "") + (r.verdict?.judge?.reason ? `\njudge: ${r.verdict.judge.verdict} — ${r.verdict.judge.reason}` : ""), r.brief || "(none)"); } catch (e) { toast(e.message, true); } };
    const out = el("button", "ghost small", c.state === "running" ? "live" : "report"); out.onclick = async () => { try { const r = await api(`api/claude/runs/${c.id}`); showText(`Claude Code run ${c.id} — ${r.state}` + (r.served_model ? ` (${r.served_model})` : "") + (r.error ? `\nerror: ${r.error}` : ""), (r.state === "running" || !r.final ? (r.tail || []).join("\n") : r.final) + (r.files?.length ? "\n\nFiles changed:\n" + r.files.map((f) => `${f.status} ${f.path}`).join("\n") : "")); } catch (e) { toast(e.message, true); } };
    h.append(br, out);
    if (c.ckpt_before && c.ckpt_after) { const df = el("button", "ghost small", "diff"); df.onclick = async () => { try { const r = await api(`api/jobs/${m.root}/diff/${c.ckpt_before}?to=${c.ckpt_after}`); showDiff({ checkpoint: c.ckpt_before, label: `Claude run ${c.id}` }, r); } catch (e) { toast(e.message, true); } }; h.append(df); }
    d.append(h);
    if (c.error) d.append(el("div", "small err", c.error));
    if (c.files?.length) d.append(el("div", "muted small mono", c.files.map((f) => `${f.status} ${f.path}`).join("  ")));
    d.append(el("pre", null, (c.tail || []).join("\n"))); return d;
  }) : [el("p", "muted small", "No Claude Code runs.")]));
  $("#m-bg-n").textContent = m.bg.length ? String(m.bg.length) : "";
  $("#m-bg").replaceChildren(...(m.bg.length ? m.bg.map((b) => {
    const d = el("div", "bg"); const h = el("div", "bh");
    h.append(el("span", "nm", b.name), el("span", "tag " + b.state, b.state + (b.exit_code != null ? ` (${b.exit_code})` : "")), el("span", "muted small", b.state === "running" ? fmtS(now - b.created) : fmtS(b.elapsed_s)), el("span", "grow"));
    if (b.state === "running") { const k = el("button", "danger small", "stop"); k.onclick = async () => { try { await api(`api/bg/${b.id}/stop`, { method: "POST", json: {} }); toast("Stopping"); } catch (e) { toast(e.message, true); } }; h.append(k); }
    const lg = el("button", "ghost small", "log"); lg.onclick = async () => { try { const r = await api(`api/bg/${b.id}/logs?tail=400`); const pre = d.querySelector("pre"); pre.textContent = r.log || "(no output)"; pre.style.maxHeight = "260px"; } catch (e) { toast(e.message, true); } }; h.append(lg);
    d.append(h, el("div", "muted small mono", b.command), el("pre", null, (b.tail || []).join("\n"))); return d;
  }) : [el("p", "muted small", "None.")]));
  const cks = m.checkpoints || [];
  $("#m-ck-n").textContent = cks.length ? String(cks.length) : "";
  $("#m-ckpts").replaceChildren(...(cks.length ? cks.map((c) => {
    const li = el("li"); li.append(el("span", "ts", fmtClock(c.ts)), el("span", "id", c.checkpoint), el("span", null, c.label));
    const b = el("div"); const d = el("button", "ghost small", "diff"); d.onclick = async () => { try { const r = await api(`api/jobs/${m.root}/diff/${c.checkpoint}`); showDiff(c, r); } catch (e) { toast(e.message, true); } };
    const rb = el("button", "small", "restore"); rb.onclick = async () => { if (!confirm(`Restore the whole workspace to checkpoint ${c.checkpoint} (${c.label})? A pre-rollback checkpoint is taken first.`)) return; try { const r = await api(`api/jobs/${m.root}/rollback`, { json: { checkpoint: c.checkpoint } }); toast(r.error || `Restored ${c.checkpoint}`, !!r.error); } catch (e) { toast(e.message, true); } };
    b.append(d, rb); li.append(b); return li;
  }) : [el("li", "ph", "No checkpoints yet.")]));
  $("#m-state-wrap").classList.toggle("hidden", !m.state); if (m.state) $("#m-state").textContent = m.state;
  const dv = m.deliveries || [], ds = m.deliverable;
  $("#m-deliv-n").textContent = (dv.length ? `${dv.length}` : "") + (ds && m.active ? `${dv.length ? " · " : ""}next ${ds.overdue ? "OVERDUE" : "in " + fmtS(Math.max(0, ds.next_due_in_s))}` : "");
  $("#m-deliv").replaceChildren(...(dv.length ? dv.map((d) => {
    const row = el("div", "deliv"), a = el("a", "filecard wsfile", `v${d.n}${d.version ? " " + d.version : ""} · ${fmtClock(d.ts)} · ${d.files} files`);
    a.href = BASE + "api/files?path=" + encodeURIComponent(d.zip); a.setAttribute("download", d.zip.split("/").pop()); a.dataset.wsfile = d.zip;
    row.append(a, el("div", "small muted", d.summary || "")); return row;
  }) : [el("div", "ph small muted", ds ? `No deliverable yet (every ${(ds.every_s / 3600).toFixed(1).replace(/\.0$/, "")} h).` : "No deliverable cadence for this mission.")]));
  $("#m-notes").replaceChildren(...(m.notes.length ? m.notes.slice(-80).map((n) => {
    const li = el("li"); const who = n.depth ? (n.label || n.role || "member") : "lead";
    li.append(el("span", "ts", fmtClock(n.ts)), el("span", "who" + (n.depth ? "" : " lead"), who), el("span", null, n.message)); return li;
  }) : [el("li", "ph", "No updates yet.")]));
  const nl = $("#m-notes"); nl.scrollTop = nl.scrollHeight;
}
function showText(title, body) {
  const lb = $("#lightbox"); lb.querySelector("img").classList.add("hidden");
  let box = lb.querySelector(".diffbox"); if (!box) { box = el("pre", "diffbox"); box.onclick = (e) => e.stopPropagation(); lb.append(box); }
  box.textContent = `# ${title}\n\n${body}`; box.classList.remove("hidden"); lb.classList.remove("hidden");
}
function showDiff(c, r) {
  const lb = $("#lightbox"); lb.querySelector("img").classList.add("hidden");
  let box = lb.querySelector(".diffbox"); if (!box) { box = el("pre", "diffbox"); box.onclick = (e) => e.stopPropagation(); lb.append(box); }
  box.textContent = `# diff since checkpoint ${c.checkpoint} (${c.label})${r.truncated ? " — truncated" : ""}\n\n` + (r.diff || "(no changes)");
  box.classList.remove("hidden"); lb.classList.remove("hidden");
}
$("#mission-btn").addEventListener("click", () => { const p = $("#mission"); const show = p.classList.contains("hidden"); p.classList.toggle("hidden", !show); p.dataset.userHidden = show ? "" : "1"; if (show) { const j = transcripts.get(selected)?.job; if (j) loadMission(j.root_id || j.id, true); } });
$("#m-notify").addEventListener("click", async () => {
  if (!mission) return;
  const message = prompt("Notice for the running crew (they see it in their next clock/wait/status/progress result for 30 min):");
  if (!message || message.trim().length < 3) return;
  try { await api(`api/jobs/${mission.root}/notice`, { json: { message: message.trim(), ttl_min: 30 } }); toast("Notice posted to the crew"); loadMission(mission.root, false); } catch (err) { toast(err.message, true); }
});
$("#m-close").addEventListener("click", () => { $("#mission").classList.add("hidden"); $("#mission").dataset.userHidden = "1"; });
setInterval(() => { if (mission && mission.active && !$("#mission").classList.contains("hidden")) renderMission(); }, 1000);

// ---- live progress bar under the transcript while a job runs: elapsed, plan step, crew, last tool, latest note
function renderLive() {
  const lb = $("#livebar"), t = transcripts.get(selected), j = t?.job;
  if (!j || !["running", "queued"].includes(j.status)) { lb.classList.add("hidden"); return; }
  lb.classList.remove("hidden"); lb.replaceChildren();
  const row = el("div", "lb-row"); row.append(el("span", "lb-dot" + (j.status === "queued" ? " q" : "")));
  if (j.status === "queued") { row.append(el("span", null, "Queued — waiting for a model slot")); lb.append(row); return; }
  row.append(el("b", null, `Working ${fmtDur(j.started)}`));
  const m = mission && mission.root === (j.root_id || j.id) ? mission : null;
  if (m && m.steps.length) {
    const done = m.steps.filter((x) => x.status === "done").length, act = m.steps.find((x) => x.status === "active");
    row.append(el("span", "lb-sep", "·"), el("span", null, `step ${Math.min(done + 1, m.steps.length)}/${m.steps.length}`));
    const bar = el("i", "lb-bar"); bar.style.setProperty("--w", `${Math.round((100 * done) / m.steps.length)}%`); row.append(bar);
    if (act) row.append(el("span", "lb-step", act.title));
  }
  if (m) { const run = m.members.filter((x) => x.status === "running").length; if (run) row.append(el("span", "lb-sep", "·"), el("span", null, plural(run, "agent") + " working")); }
  const tools = t.events.filter((e) => e.etype === "tool_use"); const last = tools.at(-1);
  if (j.params?.triage?.quick && !j.params.triage.handoff) row.append(el("span", "lb-sep", "·"), el("span", null, "⚡ quick reply"));
  else row.append(el("span", "lb-sep", "·"), el("span", "muted", plural(tools.length, "step")));
  lb.append(row);
  let k = t.events.length; while (k > 0 && t.events[k - 1].etype === "thinking") k--;
  const thought = k < t.events.length ? t.events.slice(k).map((e) => e.data).join("") : "";
  const ago = (ts) => `${Math.max(0, (Date.now() / 1000 - ts) | 0)}s ago`;
  if (thought) { const r2 = el("div", "lb-row sub"); r2.append(el("span", "muted", "thinking"), el("span", "lb-arg lb-say", lastSentence(thought))); lb.append(r2); }
  else if (last) { const r2 = el("div", "lb-row sub"); r2.title = summarizeInput(last.data.input); r2.append(el("span", "muted", "now"), el("span", "lb-arg lb-say", describeStep(last.data.name, last.data.input)[0]), el("span", "muted", ago(last.ts))); lb.append(r2); }
  const note = m?.notes?.at(-1);
  if (note) { const r3 = el("div", "lb-row sub"); r3.append(el("span", "muted", "latest"), el("span", "lb-who", note.depth ? (note.label || note.role) : "lead"), el("span", "lb-arg", note.message)); lb.append(r3); }
}
setInterval(renderLive, 1000);

// ---- usage history page
let usageDays = 30; const uCharts = {};
try { usageDays = +(localStorage.getItem("la_usage_days") || 30); } catch {}
const fmtNum = (n) => n >= 1e9 ? (n / 1e9).toFixed(2) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? (n / 1e3).toFixed(1) + "k" : Math.round(n).toLocaleString();
const fmtHrs = (s) => s >= 3600 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 60)} min`;
function uChart(id, cfg) { if (uCharts[id]) uCharts[id].destroy(); uCharts[id] = new Chart($("#" + id), cfg); }
const U_GRID = { color: "#30363D55" }, U_TICK = { color: "#8B949E", font: { size: 11 } };
function uBase(extra = {}) { return { responsive: true, maintainAspectRatio: false, animation: { duration: 500 }, interaction: { mode: "index", intersect: false },
  plugins: { legend: { labels: { color: "#C9D1D9", boxWidth: 10, font: { size: 11 } } }, tooltip: { backgroundColor: "#161B22", borderColor: "#30363D", borderWidth: 1 } }, ...extra }; }
function gradient(canvas, c1) { const g = canvas.getContext("2d").createLinearGradient(0, 0, 0, canvas.clientHeight || 260); g.addColorStop(0, c1 + "aa"); g.addColorStop(1, c1 + "05"); return g; }
async function loadUsage() {
  $$("#u-range button").forEach((b) => b.classList.toggle("on", +b.dataset.d === usageDays));
  let u; try { u = await api(`api/usage?days=${usageDays}`); } catch (err) { toast(err.message, true); return; }
  const t = u.totals, ok = u.status.done || 0, all = Object.values(u.status).reduce((a, b) => a + b, 0) || 1;
  const kpis = [["Runs", fmtNum(t.jobs), `${t.missions} missions · ${t.members} crew`], ["Generated", fmtNum(t.output), "output tokens"],
    ["Read", fmtNum(t.input + t.cached), `${fmtNum(t.cached)} from cache`], ["Agent time", fmtHrs(t.agent_s), u.longest ? `longest ${fmtHrs(u.longest.s)}` : ""],
    ["Tool calls", fmtNum(t.tools), t.jobs ? `${(t.tools / t.jobs).toFixed(1)} per run` : ""], ["Success", `${Math.round((100 * ok) / all)}%`, `${u.status.failed || 0} failed`],
    ["Speed", u.model.avg_gen_tps ? `${u.model.avg_gen_tps.toFixed(1)} t/s` : "—", u.model.avg_prefill_tps ? `prefill ${Math.round(u.model.avg_prefill_tps)} t/s` : ""]];
  $("#u-kpis").replaceChildren(...kpis.map(([k, v, sub], i) => { const d = el("div", "kpi"); d.style.animationDelay = `${i * 40}ms`; d.append(el("span", "k", k), el("span", "v", v), el("span", "s", sub)); return d; }));
  const labels = u.labels.map((d) => { const dt = new Date(d + "T00:00"); return usageDays <= 31 ? dt.toLocaleDateString([], { month: "short", day: "numeric" }) : dt.toLocaleDateString([], { month: "short", day: "numeric" }); });
  const pd = u.per_day;
  const cTok = $("#uc-tokens");
  uChart("uc-tokens", { type: "bar", data: { labels, datasets: [
    { type: "line", label: "generated", data: pd.map((d) => d.output), yAxisID: "y2", borderColor: "#BC8CFF", backgroundColor: gradient(cTok, "#BC8CFF"), fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 },
    { label: "prompt (new)", data: pd.map((d) => d.input), backgroundColor: "#58A6FFcc", borderRadius: 3, stack: "p" },
    { label: "prompt (cached)", data: pd.map((d) => d.cached), backgroundColor: "#39C5CF55", borderRadius: 3, stack: "p" }] },
    options: uBase({ scales: { x: { grid: { display: false }, ticks: U_TICK, stacked: true }, y: { grid: U_GRID, ticks: { ...U_TICK, callback: (v) => fmtNum(v) }, stacked: true, title: { display: true, text: "prompt", color: "#8B949E" } },
      y2: { position: "right", grid: { display: false }, ticks: { ...U_TICK, callback: (v) => fmtNum(v) }, title: { display: true, text: "generated", color: "#8B949E" } } } }) });
  const cTime = $("#uc-time");
  uChart("uc-time", { type: "line", data: { labels, datasets: [{ label: "hours", data: pd.map((d) => +(d.agent_s / 3600).toFixed(2)), borderColor: "#3FB950", backgroundColor: gradient(cTime, "#3FB950"), fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 }] },
    options: uBase({ plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: U_TICK }, y: { grid: U_GRID, ticks: U_TICK, beginAtZero: true } } }) });
  uChart("uc-jobs", { type: "bar", data: { labels, datasets: [{ label: "ok", data: pd.map((d) => d.jobs - d.failed), backgroundColor: "#58A6FFcc", borderRadius: 4, stack: "j" }, { label: "failed", data: pd.map((d) => d.failed), backgroundColor: "#F85149cc", borderRadius: 4, stack: "j" }] },
    options: uBase({ scales: { x: { grid: { display: false }, ticks: U_TICK, stacked: true }, y: { grid: U_GRID, ticks: { ...U_TICK, precision: 0 }, stacked: true, beginAtZero: true } } }) });
  const pal = ["#58A6FF", "#BC8CFF", "#39C5CF", "#3FB950", "#D29922", "#F778BA", "#F85149", "#79C0FF"];
  const dough = (id, obj, order) => { const keys = order ? order.filter((k) => obj[k]) : Object.keys(obj); uChart(id, { type: "doughnut", data: { labels: keys.map((k) => EFFORT_LABEL[k] && order ? EFFORT_LABEL[k] : k), datasets: [{ data: keys.map((k) => obj[k]), backgroundColor: keys.map((_, i) => pal[i % pal.length] + "dd"), borderColor: "#0D1117", borderWidth: 2 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: "68%", plugins: { legend: { position: "right", labels: { color: "#C9D1D9", boxWidth: 10, font: { size: 11 } } } } } }); };
  dough("uc-modes", u.by_mode); dough("uc-effort", u.by_effort, ["fast", "medium", "high", "xhigh", "max"]);
  const hbar = (id, items, color) => uChart(id, { type: "bar", data: { labels: items.map((x) => x[0]), datasets: [{ data: items.map((x) => x[1]), backgroundColor: color + "cc", borderRadius: 4 }] },
    options: uBase({ indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { grid: U_GRID, ticks: { ...U_TICK, precision: 0 } }, y: { grid: { display: false }, ticks: U_TICK } } }) });
  hbar("uc-tools", u.tools.map((x) => [String(x.name || "?").replace(/^mcp__crew__/, "crew.").replace(/^mcp__rag__/, "rag."), x.count]), "#39C5CF");
  hbar("uc-roles", Object.entries(u.by_role).sort((a, b) => b[1] - a[1]), "#BC8CFF");
  const max = Math.max(1, ...u.heat.flat()), days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], hm = $("#u-heat"); hm.replaceChildren(el("span"));
  for (let h = 0; h < 24; h++) hm.append(el("span", "hh", h % 3 === 0 ? String(h) : ""));
  u.heat.forEach((row, d) => { hm.append(el("span", "hd", days[d])); row.forEach((v, h) => { const c = el("span", "hc"); c.style.setProperty("--a", v ? (0.15 + 0.85 * v / max).toFixed(2) : 0); c.title = `${days[d]} ${h}:00 — ${v} run${v === 1 ? "" : "s"}`; hm.append(c); }); });
}
$$("#u-range button").forEach((b) => b.addEventListener("click", () => { usageDays = +b.dataset.d; try { localStorage.setItem("la_usage_days", usageDays); } catch {} loadUsage(); }));

// ---- collapsible top bar + desktop sidebar rail (remembered per browser)
function setFlag(cls, key, on) { document.body.classList.toggle(cls, on); try { localStorage.setItem(key, on ? "1" : "0"); } catch {} setTimeout(drawSpark, 50); }
try { if (localStorage.getItem("la_tb_hidden") === "1") document.body.classList.add("tb-hidden"); if (localStorage.getItem("la_rail_hidden") === "1") document.body.classList.add("rail-hidden"); } catch {}
$("#tb-hide").addEventListener("click", () => setFlag("tb-hidden", "la_tb_hidden", true));
$("#tb-show").addEventListener("click", () => setFlag("tb-hidden", "la_tb_hidden", false));
$("#rail-hide").addEventListener("click", () => setFlag("rail-hidden", "la_rail_hidden", true));
$("#rail-show").addEventListener("click", () => setFlag("rail-hidden", "la_rail_hidden", false));

// ---- lightbox
function openLightbox(src) { const lb = $("#lightbox"); lb.querySelector(".diffbox")?.classList.add("hidden"); const im = lb.querySelector("img"); im.classList.remove("hidden"); im.src = src; lb.classList.remove("hidden"); }
$("#lightbox").addEventListener("click", () => $("#lightbox").classList.add("hidden"));

// ---------------------------------------------------------------- model page
let profiles = null, tuneSel = null;
const HINTS = { CTX: "context tokens", NGL: "layers on GPU (99 = all)", N_CPU_MOE: "MoE layers, experts in RAM", THREADS: "CPU threads", BATCH: "logical batch", UBATCH: "physical batch (prefill vs VRAM)", KV_TYPE: "KV cache precision", TEMP: "temperature", TOP_P: "nucleus", TOP_K: "top-k", MIN_P: "min-p", PARALLEL: "llama-server slots (unified KV pool of CTX tokens)", AGENT_CTX: "one agent's window; concurrent agents = CTX / AGENT_CTX" };
async function loadProfiles() {
  profiles = await api("api/profiles"); tuneSel = tuneSel || profiles.active;
  $("#profile-list").replaceChildren(...profiles.profiles.map((p) => {
    const d = el("div", "profile" + (p.name === tuneSel ? " sel" : "")); d.append(el("span", "pn", p.name));
    if (p.active) d.append(el("span", "tag active", "active"));
    else { const b = el("button", "small", "activate"); b.onclick = async (ev) => { ev.stopPropagation(); if (!confirm(`Switch to ${p.name}? llama-server restarts; a running task will fail.`)) return; try { await api(`api/profiles/${p.name}/activate`, { method: "POST", json: {} }); toast(`Activating ${p.name}…`); setTimeout(loadProfiles, 800); } catch (err) { toast(err.message, true); } }; d.append(b); }
    d.onclick = () => { tuneSel = p.name; loadProfiles(); }; return d;
  }));
  const p = profiles.profiles.find((x) => x.name === tuneSel);
  $("#tune-name").textContent = p.name + (p.active ? " (active)" : "");
  $("#tune-form").replaceChildren(...Object.entries(profiles.tunable).map(([k, spec]) => {
    const l = el("label"); l.append(document.createTextNode(k)); let inp;
    if (Array.isArray(spec)) { inp = el("select"); spec.forEach((o) => { const op = el("option", null, String(o)); op.value = o; inp.append(op); }); }
    else { inp = el("input"); inp.type = "number"; inp.inputMode = /TEMP|TOP_P|MIN_P/.test(k) ? "decimal" : "numeric"; inp.min = spec.min; inp.max = spec.max; inp.step = /TEMP|TOP_P|MIN_P/.test(k) ? "0.01" : "1"; }
    inp.name = k; inp.value = p.values[k] ?? ""; l.append(inp, el("span", "hint", HINTS[k] || "")); return l;
  }));
  $("#profile-notes").textContent = [...p.notes, `model: ${p.values.MODEL?.split("/").pop()}`, p.values.EXTRA_ARGS ? `extra args (disk only): ${p.values.EXTRA_ARGS}` : ""].filter(Boolean).join("\n");
  loadModelLog();
}
async function saveTune(restart) {
  const values = Object.fromEntries($$("#tune-form [name]").map((i) => [i.name, i.value]));
  const p = profiles.profiles.find((x) => x.name === tuneSel);
  if (restart && p.active && !confirm("Save and restart llama-server? A running task will fail.")) return;
  try { const r = await api(`api/profiles/${tuneSel}`, { method: "PUT", json: { values, restart } }); $("#tune-msg").className = "small ok"; $("#tune-msg").textContent = `Saved · ${r.result}`; loadProfiles(); }
  catch (err) { $("#tune-msg").className = "small error"; $("#tune-msg").textContent = err.message; }
}
$("#tune-save").addEventListener("click", () => saveTune(true));
$("#tune-save-only").addEventListener("click", () => saveTune(false));
$("#model-restart").addEventListener("click", async () => { if (!confirm("Restart llama-server?")) return; try { await api("api/model/restart", { method: "POST", json: {} }); toast("Restarting model server…"); } catch (err) { toast(err.message, true); } });
$("#bench").addEventListener("click", async () => { try { const j = await api("api/jobs/bench", { method: "POST", json: { contexts: [128, 16384] } }); onJob(j); toast(`Benchmark queued (#${j.id})`); } catch (err) { toast(err.message, true); } });
async function loadModelLog() { try { const r = await api("api/logs/model?lines=120"); const p = $("#model-log"); p.textContent = r.lines.join("\n"); p.scrollTop = p.scrollHeight; } catch {} }

// ---------------------------------------------------------------- evals
async function loadEvals() {
  const runs = await api("api/evals");
  $("#eval-runs").replaceChildren(...runs.map((r, idx) => {
    const d = el("details", "card eval-run"); if (idx === 0) d.open = true;
    const s = el("summary"); s.append(el("span", "lbl", r.label), el("span", scoreCls(r.overall), `overall ${r.overall.toFixed(3)}`), el("span", "muted", `${r.n} tasks · weakest ${r.weakest} · ${fmtAgo(r.mtime)}`)); d.append(s);
    const bars = el("div", "catbars");
    Object.entries(r.by_category).forEach(([c, v]) => { const cb = el("div", "cb"); cb.append(document.createTextNode(c), el("b", null, v.toFixed(3))); const bar = el("div", "bar"); const i = el("i"); i.style.width = `${v * 100}%`; bar.append(i); cb.append(bar); bars.append(cb); });
    d.append(bars);
    const t = el("table", "grid");
    r.results.forEach((x) => { const tr = el("tr"); tr.append(el("td", "mono", x.id), el("td", "r " + scoreCls(x.score), x.score.toFixed(2)), el("td", "r muted", `${x.tool_calls ?? "–"} tools`), el("td", "r muted", `${x.wall_s ?? "–"}s`), el("td", "muted small", x.note)); t.append(tr); });
    d.append(t); return d;
  }));
}
const scoreCls = (v) => (v >= 0.999 ? "score-1" : v > 0.5 ? "score-mid" : "score-0");
async function queueEval(only) { try { const j = await api("api/jobs/eval", { method: "POST", json: { only } }); onJob(j); toast(`Eval queued (#${j.id})`); } catch (err) { toast(err.message, true); } }
$("#eval-all").addEventListener("click", () => confirm("Run the full 24-task suite (~35 min)?") && queueEval([]));
$("#eval-some").addEventListener("click", () => { const only = $$(".eval-cat:checked").map((i) => i.value); if (!only.length) return toast("Pick at least one category"); queueEval(only); });

// ---------------------------------------------------------------- System One router (local decision model)
function routeNote(r, tri) {
  const d = el("div", "route-note"), bits = [];
  if (tri) {
    if (tri.quick && !tri.handoff) bits.push(`quick reply, skipped the agent (${tri.via === "rule" ? "greeting" : "p " + (+tri.p).toFixed(2)})`);
    else if (tri.quick) bits.push(tri.error ? "quick reply failed, the agent answered" : "the quick reply handed this to the agent");
    else if (tri.error) bits.push(`Jev unavailable (${String(tri.error).slice(0, 60)}), the agent answered`);
    else bits.push(tri.via === "rule" ? "needs the agent (continues earlier work)" : `needs the agent (quick p ${(+tri.p).toFixed(2)})`);
  }
  if (r.error) bits.push(`router unavailable (${r.error.slice(0, 80)}) — ran without it`);
  if (r.skills) bits.push(r.skills.added?.length ? `loaded ${r.skills.added.map((s) => { const t = (r.skills.top || []).find((x) => x[0] === s); return t ? `${s} (${t[1].toFixed(2)})` : s; }).join(", ")}`
    : r.skills.picked?.length ? `kept ${r.skills.picked.join(", ")} (already loaded in this thread)` : `no skill needed (need ${(+r.skills.need).toFixed(2)})`);
  if (r.effort) bits.push(`effort ${r.effort.effort}`);
  const ms = (tri?.ms || 0) + (tri?.ms_reply || 0) + (r.skills?.ms || 0) + (r.effort?.ms || 0); if (ms) bits.push(`${(ms / 1000).toFixed(1)} s`);
  d.textContent = "⚡ Jev: " + bits.join(" · "); d.title = r.skills ? "top picks: " + (r.skills.top || []).map((x) => `${x[0]} ${x[1]}`).join(", ") : ""; return d;
}
try { const a = localStorage.getItem("la_auto_skills"); $("#auto-skills").checked = a === null ? true : a === "1"; } catch { $("#auto-skills").checked = true; }
$("#auto-skills").addEventListener("change", (e) => { try { localStorage.setItem("la_auto_skills", e.target.checked ? "1" : "0"); } catch {} });
try { const a = localStorage.getItem("la_quick"); $("#quick-replies").checked = a === null ? true : a === "1"; } catch { $("#quick-replies").checked = true; }
$("#quick-replies").addEventListener("change", (e) => { try { localStorage.setItem("la_quick", e.target.checked ? "1" : "0"); } catch {} });
$("#rt-go").addEventListener("click", async () => {
  const q = $("#rt-q").value.trim(); if (!q) return toast("Type a request");
  const b = $("#rt-go"), out = $("#rt-out"); b.disabled = true; b.textContent = "deciding…"; out.textContent = "";
  try {
    const r = await api("api/decide/route", { json: { prompt: q } }), s = r.skills, t = el("table", "grid");
    (s.top || []).forEach(([n, p]) => { const tr = el("tr"); const bar = el("div", "bar"); const i = el("i"); i.style.width = `${Math.round(p * 100)}%`; bar.append(i);
      const c = el("td"); c.append(bar); tr.append(el("td", "name", n + (s.skills.includes(n) ? "  ◀ load" : "")), el("td", "r", p.toFixed(2)), c); t.append(tr); });
    out.replaceChildren(el("p", null, `needs a skill: ${s.need.toFixed(2)} → ${s.skills.length ? "loads " + s.skills.join(", ") : "loads nothing"} · effort ${r.effort.effort} (${Object.entries(r.effort.probs).map(([k, v]) => `${k} ${v}`).join(", ")}) · ${((s.ms + r.effort.ms) / 1000).toFixed(1)} s on ${s.model || "decision model"}`), t);
  } catch (err) { toast(err.message, true); }
  b.disabled = false; b.textContent = "Ask the router";
});
$("#rt-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#rt-go").click(); });

// ---------------------------------------------------------------- skills
let skTask = null;
const SRC_LABEL = { ported: "ported", hub: "hub", local: "local-agent", builtin: "built-in" };
function renderSkillList() {
  const f = $("#sk-q").value.trim().toLowerCase();
  const rows = allSkills.filter((s) => !f || (s.name + " " + s.category + " " + s.description + " " + (s.tags || []).join(" ")).toLowerCase().includes(f));
  const byCat = {}; rows.forEach((s) => (byCat[s.category || "(root)"] = byCat[s.category || "(root)"] || []).push(s));
  const out = [];
  const order = Object.keys(byCat).sort((a, b) => (a.startsWith("local-agent") ? -1 : b.startsWith("local-agent") ? 1 : a.localeCompare(b)));
  for (const c of order) {
    out.push(el("div", "cat", `${c} · ${byCat[c].length}`));
    const t = el("table", "grid");
    byCat[c].forEach((s) => {
      const tr = el("tr", s.enabled ? "" : "off");
      const name = el("td", "name", s.name);
      const desc = el("td", "desc"); desc.textContent = s.description || "—"; desc.title = s.port ? `ported from ${s.port.kind}: ${s.port.source}` : "";
      const src = el("td"); src.append(el("span", `pill src-${s.source}`, SRC_LABEL[s.source] || s.source));
      const acts = el("td", "acts");
      const on = el("label", "small muted"); const cb = el("input"); cb.type = "checkbox"; cb.checked = s.enabled; cb.title = "enabled for the agent";
      cb.addEventListener("change", async () => { try { await api(`api/skills/${encodeURIComponent(s.name)}/enable`, { json: { enabled: cb.checked } }); s.enabled = cb.checked; tr.classList.toggle("off", !cb.checked); renderSkills(); toast(`${s.name} ${cb.checked ? "enabled" : "disabled"}`); } catch (err) { cb.checked = !cb.checked; toast(err.message, true); } });
      on.append(cb); acts.append(on);
      const use = el("button", "small", "use"); use.title = "preload in the next command"; use.addEventListener("click", () => { extraSkills.add(s.name); renderSkills(); $("#composer-opts").classList.remove("hidden"); go("console"); toast(`${s.name} added to the composer`); }); acts.append(use);
      if (s.source === "ported" || s.source === "hub") { const rm = el("button", "small danger", "remove"); rm.addEventListener("click", async () => { if (!confirm(`Remove skill ${s.name}?`)) return; try { await api(`api/skills/${encodeURIComponent(s.name)}/remove`, { json: {} }); toast(`${s.name} removed`); await loadSkillsPage(); } catch (err) { toast(err.message, true); } }); acts.append(rm); }
      tr.append(name, desc, src, acts); t.append(tr);
    });
    out.push(t);
  }
  if (!rows.length) out.push(el("p", "muted small", f ? "No installed skill matches — try “Search registries”." : "No skills found."));
  $("#sk-list").replaceChildren(...out);
}
async function loadSkillsPage() {
  await loadAllSkills(); renderSkillList();
  const sel = $("#sk-bundle"); const cur = sel.value; sel.replaceChildren(new Option("curated bundle…", ""));
  Object.entries(skillBundles).forEach(([k, b]) => { const o = new Option(`${k} (${b.items.length})`, k); o.title = b.why; sel.append(o); }); sel.value = cur;
}
function renderSearchResults(r) {
  const rows = [...(r.local || []).map((x) => ({ ...x, source: "local " + x.kind, trust: "on this box", identifier: x.path })), ...(r.remote || []).map((x) => ({ ...x, trust: x.trust_level }))];
  const box = $("#sk-results");
  if (!rows.length) { box.replaceChildren(el("p", "muted small", "Nothing found in the registries.")); return; }
  const t = el("table", "grid");
  rows.forEach((x) => {
    const tr = el("tr"); const d = el("td", "desc"); d.textContent = (x.description || "").slice(0, 200);
    const src = el("td"); src.append(el("span", "pill", `${x.source || ""} · ${x.trust || ""}`));
    const id = el("td", "desc mono small"); id.textContent = x.identifier || ""; id.title = x.identifier || "";
    const acts = el("td", "acts"); const b = el("button", "small primary", "install"); b.addEventListener("click", () => startSkillTask({ source: x.identifier })); acts.append(b);
    tr.append(el("td", "name", x.name || ""), d, src, id, acts); t.append(tr);
  });
  box.replaceChildren(el("p", "muted small", `${rows.length} result(s) — installs are scanned by skills-guard; community skills with findings are refused unless forced from the CLI.`), t);
}
async function startSkillTask(body) {
  if (skTask) return toast("an import is already running — wait for it to finish", true);
  const log = $("#sk-log"); log.classList.remove("hidden"); log.textContent = `$ skillport ${body.bundle ? "bundle" : "import"} ${body.source}\n`;
  try { const r = await api("api/skills/import", { json: body }); skTask = r.task; } catch (err) { skTask = null; return toast(err.message, true); }
  const poll = async () => {
    let t; try { t = await api(`api/skills/tasks/${skTask}`); } catch (err) { skTask = null; return toast(err.message, true); }
    log.textContent = `$ skillport ${body.bundle ? "bundle" : "import"} ${body.source}\n` + t.log; log.scrollTop = log.scrollHeight;
    if (!t.done) return setTimeout(poll, 2000);
    skTask = null; toast(t.rc === 0 ? "skill import finished" : `skill import exited ${t.rc}`, t.rc !== 0); await loadSkillsPage();
  };
  setTimeout(poll, 1500);
}
$("#sk-q").addEventListener("input", () => { if (allSkills.length) renderSkillList(); });
$("#sk-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#sk-search").click(); });
$("#sk-search").addEventListener("click", async () => {
  const q = $("#sk-q").value.trim(); if (!q) return toast("Type something to search for");
  const b = $("#sk-search"); b.disabled = true; b.textContent = "searching…"; $("#sk-results").replaceChildren(el("p", "muted small", "Asking skills.sh, ClawHub, GitHub and the official catalog… (10–60 s)"));
  try { renderSearchResults(await api(`api/skills/search?q=${encodeURIComponent(q)}`)); } catch (err) { toast(err.message, true); $("#sk-results").replaceChildren(); }
  b.disabled = false; b.textContent = "Search registries";
});
$("#sk-import").addEventListener("click", () => { const src = $("#sk-src").value.trim(); if (!src) return toast("Paste a path, URL or hub identifier first"); startSkillTask({ source: src }); });
$("#sk-src").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#sk-import").click(); } });
$("#sk-bundle-go").addEventListener("click", () => { const b = $("#sk-bundle").value; if (!b) return toast("Pick a bundle"); startSkillTask({ source: b, bundle: true }); });

// ---------------------------------------------------------------- toolbox page (the agent's persistent tools + usage)
let txDays = 30, txData = null, txTask = null;
try { txDays = +localStorage.getItem("la_tx_days") || 30; } catch {}
const fmtWhen = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
async function loadToolbox() {
  $$("#tx-range button").forEach((b) => b.classList.toggle("on", +b.dataset.d === txDays));
  try { txData = await api(`api/toolbox?days=${txDays}`); } catch (err) { toast(err.message, true); return; }
  const u = txData, tools = u.tools, uses = u.per_day.reduce((a, b) => a + b, 0);
  const disk = Object.values(u.disk || {}).reduce((a, b) => a + b, 0), doc = u.doctor;
  const kpis = [["Tools", String(tools.length), `${tools.filter((t) => t.where === "host").length} host · ${tools.filter((t) => t.shim).length} counted by shim`],
    ["Uses", fmtNum(uses), `last ${txDays}d · ${fmtNum(u.events_total)} ever`], ["Installs", String(u.installs_range), `via tb, last ${txDays}d`],
    ["Host runs", String(u.host_runs_range), `last ${txDays}d · ${u.host_runs.length} logged`], ["Disk", disk ? fmtBytes(disk) : "—", "env + venvs + opt + caches"],
    ["Doctor", doc ? (doc.missing === 0 ? "all good" : doc.missing == null ? "?" : `${doc.missing} missing`) : "—", doc ? `checked ${fmtAgo(doc.ts)}` : ""]];
  $("#tx-kpis").replaceChildren(...kpis.map(([k, v, sub], i) => { const d = el("div", "kpi"); d.style.animationDelay = `${i * 40}ms`; d.append(el("span", "k", k), el("span", "v", v), el("span", "s", sub)); return d; }));
  const labels = u.labels.map((d) => new Date(d + "T00:00").toLocaleDateString([], { month: "short", day: "numeric" }));
  uChart("txc-days", { type: "bar", data: { labels, datasets: [{ label: "uses", data: u.per_day, backgroundColor: "#39C5CFcc", borderRadius: 4 }] },
    options: uBase({ plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: U_TICK }, y: { grid: U_GRID, ticks: { ...U_TICK, precision: 0 }, beginAtZero: true } } }) });
  uChart("txc-top", { type: "bar", data: { labels: u.top.map((x) => x[0]), datasets: [{ data: u.top.map((x) => x[1]), backgroundColor: "#BC8CFFcc", borderRadius: 4 }] },
    options: uBase({ indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { grid: U_GRID, ticks: { ...U_TICK, precision: 0 } }, y: { grid: { display: false }, ticks: U_TICK } } }) });
  const pal = ["#58A6FF", "#BC8CFF", "#39C5CF", "#3FB950", "#D29922", "#F778BA"];
  const kk = Object.entries(u.kinds).sort((a, b) => b[1] - a[1]), ww = Object.entries(u.where);
  uChart("txc-kinds", { type: "doughnut", data: { labels: [...kk.map((x) => x[0]), ...ww.map((x) => "in " + x[0])], datasets: [
    { data: kk.map((x) => x[1]), backgroundColor: kk.map((_, i) => pal[i % pal.length] + "dd"), borderColor: "#0D1117", borderWidth: 2 },
    { data: ww.map((x) => x[1]), backgroundColor: ww.map((x) => (x[0] === "sandbox" ? "#58A6FF88" : "#D2992288")), borderColor: "#0D1117", borderWidth: 2 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: "55%", plugins: { legend: { position: "right", labels: { color: "#C9D1D9", boxWidth: 10, font: { size: 11 } } } } } });
  const dk = $("#tx-disk"); dk.replaceChildren(...Object.entries(u.disk || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => { const r = el("div", "drow"); r.append(el("span", "mono", k), el("span", "muted", fmtBytes(v))); const bar = el("i"); bar.style.width = `${Math.max(2, (100 * v) / Math.max(1, disk))}%`; r.append(bar); return r; }));
  if (doc) { const o = $("#tx-doctor-out"); o.textContent = doc.missing === 0 ? `Doctor: every registered command resolves (${doc.tools} tools).` : `Doctor: ${doc.output.split("\n").slice(-3).join(" · ")}`; o.title = doc.output; }
  renderToolboxList(); renderHostRuns(); renderToolboxRecent();
}
function txDetail(tr, build) {
  const next = tr.nextElementSibling;
  if (next?.classList.contains("detail")) { next.remove(); return; }
  const d = el("tr", "detail"); const td = el("td"); td.colSpan = 6; d.append(td); tr.after(d); build(td);
}
function renderToolboxList() {
  const f = $("#tx-q").value.trim().toLowerCase(), u = txData; if (!u) return;
  const rows = u.tools.filter((t) => !f || `${t.name} ${t.desc} ${t.run} ${t.where}`.toLowerCase().includes(f));
  const out = [];
  const t = el("table", "grid");
  const head = el("tr", "head"); ["tool", "what", "run", `uses ${txDays}d / ever`, "last used", ""].forEach((h) => head.append(el("th", "", h))); t.append(head);
  rows.sort((a, b) => b.usage.range - a.usage.range || b.usage.total - a.usage.total || a.name.localeCompare(b.name));
  rows.forEach((x) => {
    const tr = el("tr", "click");
    const name = el("td", "name"); name.append(el("span", "", x.name), el("span", `pill ${x.where === "host" ? "busy" : x.where === "sandbox" ? "blue" : "muted-pill"}`, x.where || "both"));
    const desc = el("td", "desc"); desc.textContent = x.desc || "—"; desc.title = `registered ${x.created} by ${x.by || "?"}${x.updated !== x.created ? ` · updated ${x.updated}` : ""}`;
    const run = el("td", "run"); const c = el("code", "", x.run || ""); c.title = x.run || ""; run.append(c);
    const uses = el("td", "num"); uses.append(el("b", "", String(x.usage.range)), el("span", "muted", ` / ${x.usage.total}`));
    const last = el("td", "small muted", x.usage.last ? `${fmtAgo(x.usage.last)}${x.usage.last_where ? ` · ${x.usage.last_where}` : ""}` : "never"); last.title = x.usage.last_args ? `last args: ${x.usage.last_args}` : "";
    const acts = el("td", "acts");
    const rec = el("button", "small", "recipe"); rec.addEventListener("click", (e) => { e.stopPropagation(); txDetail(tr, (td) => loadRecipe(td, x.name)); }); acts.append(rec);
    const rm = el("button", "small danger", "forget"); rm.title = "remove from the registry (packages/files stay)";
    rm.addEventListener("click", async (e) => { e.stopPropagation(); if (!confirm(`Forget ${x.name}? Nothing is uninstalled; the agent just won't see it in tb list.`)) return; try { await api(`api/toolbox/${encodeURIComponent(x.name)}/remove`, { json: {} }); toast(`${x.name} forgotten`); loadToolbox(); } catch (err) { toast(err.message, true); } }); acts.append(rm);
    tr.addEventListener("click", () => txDetail(tr, (td) => loadRecipe(td, x.name)));
    tr.append(name, desc, run, uses, last, acts); t.append(tr);
  });
  out.push(el("div", "cat", `${rows.length} registered tool${rows.length === 1 ? "" : "s"}`), t);
  if (u.other.length) {
    out.push(el("div", "cat", `used but not registered · ${u.other.length}`));
    const t2 = el("table", "grid");
    u.other.forEach((x) => { const tr = el("tr"); tr.append(el("td", "name", x.name), el("td", "desc muted", `${x.usage.last_args || ""}`), el("td", "num", `${x.usage.range} / ${x.usage.total}`), el("td", "small muted", fmtAgo(x.usage.last))); t2.append(tr); });
    out.push(t2, el("p", "muted small", "These ran through the toolbox but have no registry entry — the agent (or you) can `tb register` them with a description and run command."));
  }
  $("#tx-list").replaceChildren(...out);
}
async function loadRecipe(td, name) {
  td.textContent = "loading…";
  let r; try { r = await api(`api/toolbox/recipe/${encodeURIComponent(name)}`); } catch (err) { td.textContent = err.message; return; }
  const pre = el("pre", "recipe", r.recipe || "(no recipe yet)");
  const bar = el("div", "row");
  const edit = el("button", "small", "edit recipe"); const save = el("button", "small primary hidden", "save"); const ta = el("textarea", "hidden"); ta.value = r.recipe || `# ${name}\n\n`;
  edit.addEventListener("click", () => { pre.classList.toggle("hidden"); ta.classList.toggle("hidden"); save.classList.toggle("hidden"); });
  save.addEventListener("click", async () => { try { await api(`api/toolbox/recipe/${encodeURIComponent(name)}`, { json: { text: ta.value } }); pre.textContent = ta.value; pre.classList.remove("hidden"); ta.classList.add("hidden"); save.classList.add("hidden"); toast("recipe saved"); } catch (err) { toast(err.message, true); } });
  bar.append(edit, save, el("span", "muted small", `recipes/${name}.md — what the agent reads with tb show ${name}`));
  td.replaceChildren(bar, pre, ta);
  if (r.host_script) { td.append(el("div", "cat", `host/${name} — runs on the real desktop`), el("pre", "recipe", r.host_script)); }
}
function renderHostRuns() {
  const runs = txData.host_runs || [], box = $("#tx-host");
  if (!runs.length) { box.replaceChildren(el("p", "muted small", "No host runs yet.")); return; }
  const t = el("table", "grid");
  runs.forEach((r) => {
    const tr = el("tr", "click");
    const ok = r.exit === 0; tr.append(el("td", "small muted", fmtWhen(r.ts)), el("td", "name", r.tool || "?"), el("td", "desc mono small", r.args || ""));
    const st = el("td"); st.append(el("span", `tag ${ok ? "done" : r.exit === "timeout" ? "timeout" : "failed"}`, ok ? "ok" : `exit ${r.exit}`)); tr.append(st, el("td", "num small", r.seconds != null ? `${r.seconds}s` : ""));
    const acts = el("td", "acts"); const b = el("button", "small", "log"); b.addEventListener("click", (e) => { e.stopPropagation(); txDetail(tr, (td) => loadHostLog(td, r.id)); }); acts.append(b); tr.append(acts);
    tr.addEventListener("click", () => txDetail(tr, (td) => loadHostLog(td, r.id))); t.append(tr);
  });
  box.replaceChildren(t);
}
async function loadHostLog(td, id) { td.textContent = "loading…"; try { const r = await api(`api/toolbox/hostlog/${encodeURIComponent(id)}`); td.replaceChildren(el("pre", "recipe", r.text || "(empty)")); } catch (err) { td.textContent = err.message; } }
function renderToolboxRecent() {
  const ev = txData.recent || [], box = $("#tx-recent");
  if (!ev.length) { box.replaceChildren(el("p", "muted small", "Nothing logged yet — uses appear here as soon as the agent runs a toolbox command.")); return; }
  const t = el("table", "grid");
  ev.forEach((e) => { const tr = el("tr"); tr.append(el("td", "small muted", fmtWhen(e.ts)), el("td", "name", e.tool), el("td", "", ""), el("td", "desc mono small", `${e.kind}${e.exit != null ? ` exit ${e.exit}` : ""} ${e.args || ""}`)); const w = el("td"); w.append(el("span", `pill ${e.where === "sandbox" ? "blue" : "busy"}`, e.where || "?")); tr.append(w); t.append(tr); });
  box.replaceChildren(t);
}
async function startToolboxInstall() {
  const pkgs = $("#tx-pkgs").value.trim(); if (!pkgs) return toast("Type one or more package names first");
  if (txTask) return toast("an install is already running", true);
  const log = $("#tx-log"); log.classList.remove("hidden");
  let r; try { r = await api("api/toolbox/install", { json: { kind: $("#tx-kind").value, pkgs } }); } catch (err) { return toast(err.message, true); }
  txTask = r.task; log.textContent = `$ ${r.cmd}\n`;
  const poll = async () => {
    let t; try { t = await api(`api/toolbox/tasks/${txTask}`); } catch (err) { txTask = null; return toast(err.message, true); }
    log.textContent = `$ ${t.cmd}\n` + t.log; log.scrollTop = log.scrollHeight;
    if (!t.done) return setTimeout(poll, 2000);
    txTask = null; toast(t.rc === 0 ? "installed and registered" : `install exited ${t.rc}`, t.rc !== 0); if (t.rc === 0) $("#tx-pkgs").value = ""; loadToolbox();
  };
  setTimeout(poll, 1500);
}
$("#tx-install").addEventListener("click", startToolboxInstall);
$("#tx-pkgs").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); startToolboxInstall(); } });
$("#tx-q").addEventListener("input", renderToolboxList);
$("#tx-refresh").addEventListener("click", loadToolbox);
$("#tx-doctor").addEventListener("click", async () => { const b = $("#tx-doctor"); b.disabled = true; b.textContent = "checking…"; try { const d = await api("api/toolbox/doctor", { json: {} }); $("#tx-doctor-out").textContent = d.missing === 0 ? `Doctor: every registered command resolves (${d.tools} tools).` : `Doctor: ${d.output.split("\n").slice(-6).join(" · ")}`; if (txData) txData.doctor = d; } catch (err) { toast(err.message, true); } b.disabled = false; b.textContent = "Doctor"; });
$$("#tx-range button").forEach((b) => b.addEventListener("click", () => { txDays = +b.dataset.d; try { localStorage.setItem("la_tx_days", txDays); } catch {} loadToolbox(); }));

// ---------------------------------------------------------------- logs
async function loadLog() { try { const r = await api(`api/logs/${$("#log-name").value}?lines=500`); const p = $("#log-view"); const stick = p.scrollHeight - p.scrollTop - p.clientHeight < 60; p.textContent = r.lines.join("\n"); if (stick) p.scrollTop = p.scrollHeight; } catch {} }
$("#log-name").addEventListener("change", loadLog);
setInterval(() => { if (page === "logs" && $("#log-follow").checked) loadLog(); if (page === "model") loadModelLog(); }, 4000);
setInterval(() => { if (page === "console") renderJobList(); if (selected && transcripts.get(selected)?.job.status === "running") renderTranscriptHead(); }, 1000);

// ---------------------------------------------------------------- starfield
function stars() {
  const c = $("#stars"), x = c.getContext("2d"); let pts = [];
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  function resize() { c.width = innerWidth; c.height = innerHeight; pts = Array.from({ length: Math.round((innerWidth * innerHeight) / 11000) }, () => ({ x: Math.random() * innerWidth, y: Math.random() * innerHeight, r: Math.random() * 1.1 + 0.2, a: Math.random() * 0.5 + 0.1, v: Math.random() * 0.06 + 0.01 })); }
  function frame() { x.clearRect(0, 0, c.width, c.height); for (const p of pts) { if (!reduce) { p.y += p.v; if (p.y > c.height) p.y = 0; } x.globalAlpha = p.a; x.fillStyle = "#c9d1d9"; x.beginPath(); x.arc(p.x, p.y, p.r, 0, 6.283); x.fill(); } if (!reduce) requestAnimationFrame(frame); }
  addEventListener("resize", resize); resize(); frame();
}

// ---------------------------------------------------------------- boot
async function boot() {
  try { const me = await api("api/me"); csrf = me.csrf; skills = me.skills; crewInfo = { modes: me.modes || [], roles: me.roles || [], slots: me.slots || 1, lead: me.lead, efforts: me.efforts || [] }; if (me.chat_profiles) chatProfiles = me.chat_profiles; } catch { return; }
  renderProfileBtn();
  $("#login-view").classList.add("hidden"); $("#app-view").classList.remove("hidden");
  renderSkills(); renderModes(); initEffort(); updateSend(); connect(); await loadJobs(); loadAllSkills();
  try { onStatus(await api("api/status")); } catch {}
  go((location.hash || "#overview").slice(1).replace(/[^a-z]/g, "") || "overview");
}
stars();
fetch(BASE + "api/me", { credentials: "same-origin" }).then((r) => (r.ok ? boot() : showLogin())).catch(showLogin);

// ---- self-update: a page left open (or the home-screen app) keeps running old code after a deploy; check the
// served index for a newer script version when the tab comes back or every 5 min, and reload when it's safe.
const MY_VER = new URL(import.meta.url).searchParams.get("v");
async function checkUpdate() {
  try {
    const html = await (await fetch(BASE, { cache: "no-store", credentials: "same-origin" })).text();
    const v = (html.match(/app\.js\?v=(\d+)/) || [])[1];
    if (!v || !MY_VER || v === MY_VER) return;
    if (!pending.length && !ta.value.trim()) location.reload();
    else toast("The dashboard was updated — it will reload after you send this message");
  } catch {}
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkUpdate(); });
setInterval(checkUpdate, 300000);
