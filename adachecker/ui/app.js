/* ADA Checker window. All text from scanned files is inserted with textContent, never as HTML. */
"use strict";

// ------------------------------------------------------------------ token and api
const params = new URLSearchParams(location.search);
if (params.get("t")) { sessionStorage.setItem("t", params.get("t")); history.replaceState(null, "", "/"); }
const TOKEN = sessionStorage.getItem("t") || "";

async function api(path, body, raw) {
  const opt = { method: body === undefined ? "GET" : "POST", headers: { "X-Token": TOKEN } };
  if (raw) { opt.body = raw.body; opt.headers["X-Filename"] = encodeURIComponent(raw.name); opt.method = "POST"; }
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opt);
  if (r.status === 403) { toast("This window lost its connection. Close it and open ADA Checker again."); throw new Error("forbidden"); }
  return r.json();
}
setInterval(() => api("/api/ping").catch(() => {}), 4000);
api("/api/ping").catch(() => {});

// ------------------------------------------------------------------ tiny DOM helper
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
const $ = (s) => document.querySelector(s);
const STATUS = { "needs work": "Changes needed", "needs review": "Check manually", "pass": "Ready", "error": "Could not check" };
const KIND = { word: "Word", pdf: "PDF", html: "Web page", powerpoint: "PowerPoint", excel: "Excel", zip: "Zip", video: "Video" };
const pill = (st) => h("span", { class: `st st-${st.replace(" ", "-")}` }, STATUS[st] || st);
function toast(msg, ms = 4200) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms); }
function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }
function fmtDate(s) { if (!s) return ""; const d = new Date(s); return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }

// ------------------------------------------------------------------ tabs
const tabs = [...document.querySelectorAll("[role=tab]")];
function showTab(id) {
  for (const t of tabs) {
    const on = t.id === id;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  }
  if (id === "tab-history") loadHistory();
  if (id === "tab-settings") loadSettings();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => showTab(t.id));
  t.addEventListener("keydown", (e) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (d) { const n = tabs[(i + d + tabs.length) % tabs.length]; n.focus(); showTab(n.id); }
  });
});

// ------------------------------------------------------------------ settings state
let SETTINGS = {}, HAS_DIALOGS = true, AI = { found: false };
async function loadSettingsState(fresh) {
  const s = await api("/api/settings" + (fresh ? "?fresh=1" : ""));
  SETTINGS = s.settings; HAS_DIALOGS = s.dialogs; AI = s.ai || { found: false };
  $("#subfolders").checked = SETTINGS.include_subfolders !== false;
  $("#pathbox").hidden = HAS_DIALOGS;
  return s;
}

// ------------------------------------------------------------------ starting checks
async function pick(type) {
  if (!HAS_DIALOGS) { $("#pathinput").focus(); return []; }
  const r = await api("/api/pick", { type });
  return r.paths || [];
}
$("#pick-file").onclick = async () => { const p = await pick("file"); if (p.length) startScan({ items: p.map((path) => ({ path })) }); };
$("#pick-folder").onclick = async () => { const p = await pick("folder"); if (p.length) startScan({ folder: p[0] }); };
$("#pathbox").onsubmit = (e) => { e.preventDefault(); const v = $("#pathinput").value.trim().replace(/^"|"$/g, ""); if (v) startScan({ items: [{ path: v }], folder: null, pasted: v }); };

async function startScan(opts) {
  const body = { items: opts.items || [], folder: opts.folder || null, subfolders: $("#subfolders").checked, force: $("#force").checked };
  if (opts.pasted && !/\.[a-z0-9]{2,5}$/i.test(opts.pasted)) { body.folder = opts.pasted; body.items = []; }
  const r = await api("/api/scan", body);
  if (r.error) return toast(r.error);
  $("#done").hidden = true;
  watchJob("files");
}

// drag and drop
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "dragleave" && drop.contains(e.relatedTarget)) return; drop.classList.remove("over"); }));
drop.addEventListener("drop", async (e) => {
  const files = [];
  const items = [...(e.dataTransfer.items || [])];
  const entries = items.map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  async function walk(entry, prefix) {
    if (entry.isFile) { await new Promise((res) => entry.file((f) => { files.push({ f, rel: prefix + f.name }); res(); }, res)); }
    else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do { batch = await new Promise((res) => reader.readEntries(res, () => res([]))); for (const c of batch) await walk(c, prefix + entry.name + "/"); } while (batch.length);
    }
  }
  if (entries.length) for (const en of entries) await walk(en, "");
  else for (const f of e.dataTransfer.files) files.push({ f, rel: f.name });
  const ok = files.filter((x) => /\.(docx|docm|pdf|html?|pptx|pptm|xlsx|xlsm|zip|doc|ppt|xls|rtf)$/i.test(x.f.name) && !x.f.name.startsWith("~$"));
  if (!ok.length) return toast("None of those are files this can check. Try Word, PDF, HTML, PowerPoint, Excel or a zip.");
  showProgress(true, `Copying ${plural(ok.length, "file")}…`);
  const uploaded = [];
  for (const x of ok) {
    const r = await api("/api/upload", undefined, { body: x.f, name: x.f.name });
    uploaded.push({ path: r.path, name: x.rel, key: "Dropped file: " + x.rel });
  }
  const r = await api("/api/scan", { items: uploaded, subfolders: true, force: true });
  if (r.error) return toast(r.error);
  watchJob("files");
});

// ------------------------------------------------------------------ progress
function showProgress(on, title) {
  $("#progress").hidden = !on;
  if (title) $("#prog-title").textContent = title;
}
let watching = null;
function watchJob(kind) {
  clearInterval(watching);
  showProgress(true, "Checking…");
  const tick = async () => {
    const j = await api("/api/job");
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    $("#prog-bar").firstElementChild.style.width = pct + "%";
    $("#prog-bar").setAttribute("aria-valuenow", pct);
    $("#prog-title").textContent = !j.running ? "Finished" : j.phase === "ai" ? `AI review: ${j.done} of ${j.total || "?"} files` : `Checking: ${j.done} of ${j.total || "?"}`;
    $("#prog-current").textContent = j.current || "";
    $("#prog-log").replaceChildren(...(j.log || []).map((l) => h("li", {}, l)));
    if (!j.running) {
      clearInterval(watching);
      watching = null;
      showProgress(false);
      if (j.error) return toast("Something went wrong: " + j.error, 8000);
      finishScan(j);
    }
  };
  watching = setInterval(tick, 700);
  tick();
}
$("#cancel").onclick = () => api("/api/cancel", {});

function finishScan(j) {
  const results = (j.results || []).filter(Boolean);
  BATCH = results;
  renderList();
  const d = $("#done");
  const c = { "needs work": 0, "needs review": 0, pass: 0, error: 0 };
  results.forEach((r) => c[r.status]++);
  const kids = [h("h2", {}, `Checked ${plural(results.length, "file")}`),
    h("p", {}, `${c["needs work"]} need changes, ${c["needs review"]} need a manual check, ${c.pass} ready` + (c.error ? `, ${c.error} could not be checked.` : "."))];
  const t = j.ai_totals;
  if (t) kids.push(h("p", {}, `AI review cleared ${plural(t.pass, "manual item")}, found ${t.fix} that need a change and left ${t.unsure} for you.` + (t.errors ? ` ${plural(t.errors, "file")} had a problem; see the log.` : "")));
  const row = h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "btn primary", onclick: () => { showTab("tab-results"); } }, "See results"));
  if (AI.found && c["needs review"] && !t) row.append(h("button", { class: "btn", onclick: () => aiReview() }, "AI review the manual checks"));
  if (j.summary) {
    row.append(h("button", { class: "btn", onclick: () => api("/api/open", { path: j.summary.spreadsheet }) }, "Open summary spreadsheet"),
      h("button", { class: "btn", onclick: () => api("/api/open", { path: j.summary.folder }) }, "Open reports folder"));
  }
  kids.push(row);
  d.replaceChildren(...kids);
  d.hidden = false;
  if (REOPEN) { const id = REOPEN; REOPEN = null; openResult(id); showTab("tab-results"); return; }
  if (results.length === 1) { openResult(results[0].id); showTab("tab-results"); }
  else if (results.length) openResult(results[0].id);
}

let REOPEN = null;
async function aiReview(id) {
  const r = await api("/api/ai_review", id ? { id } : {});
  REOPEN = id || null;
  if (r.error) return toast(r.error, 8000);
  $("#done").hidden = true;
  showTab("tab-check");
  watchJob("files");
}

// ------------------------------------------------------------------ results list
let BATCH = [], FILTER = "all", CURRENT = null;
document.querySelectorAll("#res-list-wrap .filters button").forEach((b) => b.addEventListener("click", () => {
  FILTER = b.dataset.f;
  document.querySelectorAll("#res-list-wrap .filters button").forEach((x) => x.setAttribute("aria-pressed", x === b));
  renderList();
}));
function renderList() {
  const ul = $("#res-list");
  const order = { "needs work": 0, error: 1, "needs review": 2, pass: 3 };
  const rows = BATCH.filter((r) => FILTER === "all" || r.status === FILTER).sort((a, b) => order[a.status] - order[b.status] || b.counts.change - a.counts.change);
  if (!rows.length) { ul.replaceChildren(h("li", { class: "empty" }, BATCH.length ? "Nothing in this group." : "No results yet.")); return; }
  ul.replaceChildren(...rows.map((r) => h("li", {}, h("button", { "aria-current": CURRENT && CURRENT.id === r.id ? "true" : null, onclick: () => openResult(r.id) },
    h("span", { class: "nm" }, r.name), pill(r.status),
    h("span", { class: "ct" }, r.error ? "Could not be checked" : `${KIND[r.kind] || r.kind}, ${r.counts.change} to change, ${r.counts.manual} to check` + (r.children ? `, ${r.children} files inside` : ""))))));
}
async function refreshBatch() { const b = await api("/api/batch"); BATCH = b.results.filter(Boolean); renderList(); }

// ------------------------------------------------------------------ result detail
async function openResult(id, childPath) {
  const isVideo = false;
  const data = await api(`/api/result?id=${id}`);
  if (data.error) return toast("That result is no longer available.");
  CURRENT = { id, data, childPath, isVideo };
  if (!isVideo) renderList();
  renderDetail($("#res-detail"));
}

function findChild(r, path) { if (r.path === path) return r; for (const c of r.children || []) { const f = findChild(c, path); if (f) return f; } return null; }

function renderDetail(box) {
  const { data, childPath, isVideo } = CURRENT;
  const top = data.result;
  const r = childPath ? findChild(top, childPath) : top;
  const marks = data.marks[r.path] || { ignored: [], reviewed: [] };
  const ignored = new Set(marks.ignored), reviewed = new Set(marks.reviewed);
  const only21 = !!SETTINGS.show_21_only;
  const visible = (i) => !only21 || i.standard === "WCAG 2.1" || i.standard === "PDF/UA" || i.group === "export";
  const issues = r.issues.filter(visible);
  const status = statusOf(r, ignored, reviewed);
  const parts = [];

  // header
  const crumbs = childPath ? h("p", { class: "crumb" }, h("button", { class: "btn link", onclick: () => { CURRENT.childPath = null; renderDetail(box); } }, `Back to ${top.name}`)) : null;
  const buttons = h("div", { class: "row" });
  if (!childPath) {
    if (!isVideo) buttons.append(h("button", { class: "btn", onclick: () => rescan(data.scan.id) }, "Rescan"));
    const openManual = r.issues.some((i) => i.group === "manual" && !ignored.has(i.fingerprint) && !reviewed.has(i.fingerprint) && !((data.ai || {})[r.path] || {})[i.fingerprint]);
    if (!isVideo && AI.found && openManual && r.kind !== "zip") buttons.append(h("button", { class: "btn", onclick: () => aiReview(data.scan.id) }, "AI review"));
    buttons.append(h("button", { class: "btn", onclick: async () => { const s = await api("/api/save_report", { id: data.scan.id, video: !!isVideo }); toast(s.error || "Report saved and opened."); } }, "Save report"));
    if (isVideo) buttons.append(h("button", { class: "btn", onclick: () => api("/api/open", { path: r.path }) }, "Open on YouTube"));
    else if (!String(data.scan.file_key).startsWith("Dropped file:")) {
      buttons.append(h("button", { class: "btn", onclick: async () => { const o = await api("/api/open", { path: data.scan.file_key }); if (o.error) toast(o.error); } }, "Open file"),
        h("button", { class: "btn", onclick: () => api("/api/open", { path: data.scan.file_key, reveal: true }) }, "Show in folder"));
    }
  }
  parts.push(h("div", { class: "dhead" }, h("div", {}, crumbs, h("h2", {}, r.name), h("p", { class: "muted small" },
    `${KIND[r.kind] || r.kind}. Checked ${fmtDate(data.scan.scanned_at)}.`)), buttons));

  if (r.error) parts.push(h("div", { class: "issue g-change" }, h("h4", {}, isVideo ? "Could not check this video" : "Could not check this file"), h("p", {}, r.error)));

  // tally
  if (!r.error && r.kind !== "zip") {
    const n = (g) => issues.filter((i) => i.group === g && !ignored.has(i.fingerprint) && (g === "change" || !reviewed.has(i.fingerprint))).length;
    parts.push(h("div", { class: "tally" },
      h("div", {}, h("b", {}, n("change")), h("span", {}, "to change")),
      h("div", {}, h("b", {}, n("manual")), h("span", {}, "to check by hand")),
      n("export") || r.kind === "word" ? h("div", {}, h("b", {}, n("export")), h("span", {}, "after export")) : null,
      h("div", {}, h("b", {}, r.passed.length), h("span", {}, "passed")),
      h("div", { style: "justify-content:center" }, pill(status))));
    parts.push(h("div", { class: "toolbar" }, h("label", { class: "chk" }, h("input", { type: "checkbox", checked: only21 || null, onchange: async (e) => {
      SETTINGS.show_21_only = e.target.checked; await api("/api/settings", { settings: { show_21_only: e.target.checked } }); renderDetail(box); } }),
      "Show only what WCAG 2.1 AA requires")));
  }

  // zip: list of files inside
  if (r.kind === "zip" && r.children.length) {
    const rows = r.children.map((c) => {
      const m = data.marks[c.path] || { ignored: [], reviewed: [] };
      const st = statusOf(c, new Set(m.ignored), new Set(m.reviewed));
      const cnt = c.issues.filter((i) => i.group === "change" && !m.ignored.includes(i.fingerprint)).length;
      return h("tr", {}, h("td", {}, h("button", { class: "btn link", onclick: () => { CURRENT.childPath = c.path; renderDetail(box); box.scrollIntoView(); } }, c.name)),
        h("td", {}, KIND[c.kind] || c.kind), h("td", {}, pill(st)), h("td", {}, c.error ? "" : String(cnt)));
    });
    parts.push(h("section", { class: "group" }, h("h3", {}, `${plural(r.children.length, "file")} inside`),
      h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "File"), h("th", { scope: "col" }, "Type"), h("th", { scope: "col" }, "Status"), h("th", { scope: "col" }, "To change"))), h("tbody", {}, rows)))));
  }

  const GROUPS = [
    ["change", "Fix in the file", "Found automatically. Each one says where it is and how to fix it."],
    ["manual", "Check manually", "These need your judgment. Mark each one reviewed when you have looked at it."],
    ["export", "After you export to PDF", "Do these when you make the PDF from this file."],
    ["info", "Good to know", ""],
  ];
  for (const [g, title, intro] of GROUPS) {
    const list = issues.filter((i) => i.group === g && !ignored.has(i.fingerprint));
    if (!list.length) continue;
    parts.push(h("section", { class: "group" }, h("h3", {}, title, h("span", { class: "muted small" }, `${list.length}`)), intro ? h("p", {}, intro) : null,
      list.map((i) => issueCard(i, r, reviewed.has(i.fingerprint), box))));
  }

  const acc = r.issues.filter((i) => ignored.has(i.fingerprint));
  if (acc.length) {
    parts.push(h("section", { class: "group" }, h("details", { class: "more" }, h("summary", {}, `Accepted as is (${acc.length})`),
      acc.map((i) => h("div", { class: "issue g-info" }, h("h4", {}, i.title), h("div", { class: "actions" },
        h("button", { class: "btn link", onclick: () => mark(r, i, "ignored", false, box) }, "Put back on the list")))))));
  }
  if (r.passed && r.passed.length) parts.push(h("section", { class: "group" }, h("h3", {}, "Passed"), h("ul", { class: "passed" }, r.passed.map((p) => h("li", {}, p)))));
  if (r.previews && r.previews.length) {
    parts.push(h("section", { class: "group previews" }, h("h3", {}, "Color blindness preview"),
      h("p", {}, "How the pictures in this file look with the most common types of color blindness. Check that nothing depends on telling these colors apart."),
      r.previews.map((p) => h("div", {}, h("strong", {}, p.label), h("div", { class: "strip" }, p.views.map((v) => h("figure", {}, h("img", { src: v.src, alt: `${p.label}, ${v.name}` }), h("figcaption", {}, v.name))))))));
  }
  const stats = Object.entries(r.stats || {}).filter(([k]) => !k.startsWith("_"));
  if (stats.length) parts.push(h("section", { class: "group" }, h("h3", {}, "Details"), h("div", { class: "table-wrap" }, h("table", { class: "data kv" }, h("tbody", {},
    stats.map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k[0].toUpperCase() + k.slice(1)), h("td", {}, String(v)))))))));
  box.replaceChildren(...parts);
}

function statusOf(r, ignored, reviewed) {
  if (r.error) return "error";
  if (r.kind === "zip") {
    const sts = r.children.map((c) => { const m = CURRENT.data.marks[c.path] || { ignored: [], reviewed: [] }; return statusOf(c, new Set(m.ignored), new Set(m.reviewed)); });
    for (const s of ["needs work", "error", "needs review"]) if (sts.includes(s)) return s;
    return "pass";
  }
  if (r.issues.some((i) => i.group === "change" && !ignored.has(i.fingerprint))) return "needs work";
  if (r.issues.some((i) => i.group === "manual" && !ignored.has(i.fingerprint) && !reviewed.has(i.fingerprint))) return "needs review";
  return "pass";
}

function issueCard(i, r, done, box) {
  const tags = [];
  if (i.standard) tags.push(h("span", { class: "tag" + (i.standard === "WCAG 2.1" ? " req" : "") }, i.standard === "WCAG 2.1" ? "Required (WCAG 2.1)" : i.standard));
  (i.wcag_labels || []).forEach((w) => tags.push(h("span", { class: "tag" }, w)));
  if (i.severity === "high") tags.push(h("span", { class: "tag p-high" }, "High priority"));
  const places = i.items || [];
  const placeList = (arr) => h("ul", { class: "places" }, arr.map((p) => h("li", {}, linkify(p))));
  const card = h("div", { class: `issue g-${i.group}${done ? " done" : ""}` },
    h("h4", {}, i.title), h("div", { class: "tags" }, tags),
    i.detail ? h("p", {}, i.detail) : null,
    i.location ? h("p", { class: "muted small" }, "Where: " + i.location) : null,
    places.length ? placeList(places.slice(0, 6)) : null,
    places.length > 6 ? h("details", { class: "more" }, h("summary", {}, `Show ${places.length - 6} more`), placeList(places.slice(6))) : null,
    i.fix ? h("div", { class: "fix" }, h("b", {}, "How to fix: "), i.fix) : null);
  const ai = ((CURRENT.data.ai || {})[r.path] || {})[i.fingerprint];
  if (ai) {
    const head = { pass: "AI review: looks fine.", fix: "AI review: needs a change.", unsure: "AI review: could not decide, check it yourself." }[ai.verdict] || "AI review.";
    card.append(h("div", { class: `ai ai-${ai.verdict}` }, h("b", {}, head + " "), ai.note,
      ai.suggestion ? h("div", { class: "ai-sugg" }, h("span", { class: "muted small" }, "Suggested:"), h("p", {}, ai.suggestion),
        h("button", { class: "btn link", onclick: async () => { try { await navigator.clipboard.writeText(ai.suggestion); toast("Copied."); } catch { toast("Could not copy."); } } }, "Copy")) : null));
  }
  const actions = h("div", { class: "actions" });
  if (i.group === "manual" || i.group === "export") {
    const id = "rv-" + i.fingerprint;
    actions.append(h("label", { class: "chk", for: id }, h("input", { type: "checkbox", id, checked: done || null, onchange: (e) => mark(r, i, "reviewed", e.target.checked, box) }),
      i.group === "export" ? "Done" : done && ai && ai.verdict === "pass" ? "Reviewed by AI" : "Reviewed"));
  }
  if (i.group !== "export") {
    actions.append(h("button", { class: "btn link", onclick: () => {
      const why = prompt("Accept this as is? Add a short reason for your records (optional):", "");
      if (why !== null) mark(r, i, "ignored", true, box, why);
    } }, "Accept as is"));
  }
  card.append(actions);
  return card;
}

function linkify(text) {
  const m = String(text).match(/(https:\/\/youtu\.be\/[\w-]+\?t=\d+)/);
  if (!m) return text;
  const [before, after] = String(text).split(m[1]);
  const a = h("a", { href: "#", onclick: (e) => { e.preventDefault(); api("/api/open", { path: m[1] }); } }, "open at this time");
  return h("span", {}, before, a, after);
}

async function mark(r, i, kind, on, box, note) {
  await api("/api/mark", { file_key: r.path, fingerprint: i.fingerprint, kind, on, note: note || "", title: `${r.name}: ${i.title}`, scan_id: CURRENT.isVideo ? null : CURRENT.data.scan.id });
  const m = CURRENT.data.marks[r.path] || (CURRENT.data.marks[r.path] = { ignored: [], reviewed: [] });
  const arr = m[kind];
  const at = arr.indexOf(i.fingerprint);
  if (on && at < 0) arr.push(i.fingerprint);
  if (!on && at >= 0) arr.splice(at, 1);
  renderDetail(box);
  refreshBatch();
}

async function rescan(id) {
  const r = await api("/api/rescan", { id });
  if (r.error) return toast(r.error, 6000);
  showTab("tab-check");
  watchJob("files");
}

// ------------------------------------------------------------------ history
async function loadHistory() {
  const d = await api("/api/history");
  const p = d.progress;
  const box = $("#hist-body");
  const stat = (n, label) => h("div", { class: "stat" }, h("b", {}, n), h("span", { class: "muted" }, label));
  const month = (m) => {
    const tot = Math.max(m.files, 1);
    return h("div", { class: "month" }, h("span", {}, m.label),
      h("div", {}, h("div", { class: "stack", role: "img", "aria-label": `${m.label}: ${m.ready} ready, ${m["needs review"]} need a manual check, ${m["needs work"]} need changes, of ${m.files} files checked` },
        h("i", { class: "k-pass", style: `width:${(m.ready / tot) * 100}%` }), h("i", { class: "k-review", style: `width:${(m["needs review"] / tot) * 100}%` }), h("i", { class: "k-work", style: `width:${(m["needs work"] / tot) * 100}%` })),
        h("span", { class: "small muted" }, m.files ? `${m.ready} of ${m.files} files ready` : "No files checked")));
  };
  const rows = d.latest.map((r) => h("tr", {}, h("td", {}, h("button", { class: "btn link", onclick: () => { showTab("tab-results"); openResult(r.id); } }, r.name)),
    h("td", {}, pill(r.status)), h("td", {}, fmtDate(r.scanned_at)), h("td", {}, String(r.n_change)), h("td", {}, String(r.n_manual)), h("td", { class: "small muted" }, r.file_key)));
  box.replaceChildren(
    h("div", { class: "cards" }, stat(p.files_tracked, "files checked"), stat(p.now.pass, "ready now"), stat(p.now["needs review"], "need a manual check"), stat(p.now["needs work"], "need changes")),
    h("h3", {}, "This month and last"),
    h("div", { class: "legend" }, h("span", {}, h("i", { style: "background:var(--green)" }), "Ready"), h("span", {}, h("i", { style: "background:#c9971c" }), "Check manually"), h("span", {}, h("i", { style: "background:var(--red)" }), "Changes needed")),
    h("div", { class: "months" }, month(p.this_month), month(p.last_month)),
    h("h3", { style: "margin-top:10px" }, "Every file, latest check"),
    d.latest.length ? h("div", { class: "table-wrap", style: "margin-top:8px" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["File", "Status", "Last checked", "To change", "To check", "Location"].map((t) => h("th", { scope: "col" }, t)))), h("tbody", {}, rows)))
      : h("p", { class: "empty" }, "Nothing checked yet."),
    d.ignored.length ? h("details", { class: "more", style: "margin-top:22px" }, h("summary", {}, `Items accepted as is (${d.ignored.length})`),
      h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, ["Date", "Item", "Reason", ""].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, d.ignored.map((m) => h("tr", {}, h("td", {}, fmtDate(m.created_at)), h("td", {}, m.title), h("td", {}, m.note || ""),
          h("td", {}, h("button", { class: "btn link", onclick: async () => { await api("/api/mark", { file_key: m.file_key, fingerprint: m.fingerprint, kind: "ignored", on: false }); loadHistory(); } }, "Put back")))))))) : null,
    h("div", { class: "row", style: "margin-top:26px" }, h("button", { class: "btn danger", onclick: async () => {
      if (confirm("Clear the scan history? Reviewed and accepted items are kept.")) { await api("/api/clear_history", {}); loadHistory(); BATCH = []; renderList(); }
    } }, "Clear scan history")));
}
$("#export-record").onclick = async () => { const r = await api("/api/export_record", {}); toast(r.error || "Compliance record saved and opened."); };

// ------------------------------------------------------------------ settings
async function loadSettings() {
  const s = await loadSettingsState();
  $("#s-links").checked = !!SETTINGS.check_links;
  $("#s-colorblind").checked = !!SETTINGS.colorblind;
  $("#s-21").checked = !!SETTINGS.show_21_only;
  $("#s-folder").value = SETTINGS.report_folder || "";
  $("#s-ai").checked = !!SETTINGS.ai_review;
  $("#s-ai-model").value = SETTINGS.ai_model || "sonnet";
  showAiStatus();
  $("#s-version").textContent = `Checker version ${s.version}. History is kept in ${s.data}.`;
  $("#s-browse").hidden = !HAS_DIALOGS;
}
function showAiStatus() {
  const el = $("#s-ai-status");
  el.textContent = AI.found ? `${AI.message} It uses your Claude subscription, not an API key.`
    : `${AI.message || "Claude Code was not found."} To set it up, open PowerShell and run: irm https://claude.ai/install.ps1 | iex   Then run claude once and sign in with your Claude account. Then press Check again.`;
  el.className = "hint" + (AI.found ? "" : " warn");
}
$("#s-ai-check").onclick = async () => { $("#s-ai-status").textContent = "Looking for Claude Code…"; await loadSettingsState(true); showAiStatus(); };
$("#s-browse").onclick = async () => { const p = await pick("report_folder"); if (p.length) $("#s-folder").value = p[0]; };
$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  await api("/api/settings", { settings: {
    check_links: $("#s-links").checked, colorblind: $("#s-colorblind").checked,
    show_21_only: $("#s-21").checked, report_folder: $("#s-folder").value.trim(), include_subfolders: $("#subfolders").checked,
    ai_review: $("#s-ai").checked, ai_model: $("#s-ai-model").value } });
  await loadSettingsState();
  toast("Settings saved.");
};

// ------------------------------------------------------------------ start
loadSettingsState().then(refreshBatch).then(async () => {
  const j = await api("/api/job");
  if (j.running) watchJob("files");
  else if (BATCH.length) openResult(BATCH[0].id);
});
// a file sent from File Explorer while the window is open
setInterval(async () => { if (watching) return; const j = await api("/api/job"); if (j.running && $("#progress").hidden) watchJob("files"); }, 2500);
