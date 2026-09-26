"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const el = {
  messages: $("messages"), welcome: $("welcome"), input: $("input"), send: $("send"), composer: $("composer"),
  execute: $("execute"), banner: $("banner"), chatTitle: $("chatTitle"), sessionList: $("sessionList"),
  sessionEmpty: $("sessionEmpty"), schemaTree: $("schemaTree"), dbNote: $("dbNote"), track: $("track"),
  status: $("pipelineStatus"), sheetEmpty: $("sheetEmpty"), sheetContent: $("sheetContent"),
  sqlOut: $("sqlOut"), validBadge: $("validBadge"), explanation: $("explanation"), notes: $("notes"),
  originalBox: $("originalBox"), originalSql: $("originalSql"), toast: $("toast"),
  paneResults: $("paneResults"), paneChecks: $("paneChecks"), panePerf: $("panePerf"),
  rail: $("rail"), sheet: $("sheet"),
};

const state = { sessionId: null, running: false, current: null };

function toast(text) {
  el.toast.textContent = text;
  el.toast.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.toast.classList.remove("show"), 1800);
}

function showBanner(html) { el.banner.innerHTML = html; el.banner.hidden = false; }
function hideBanner() { el.banner.hidden = true; }

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) {
    let msg = `Request failed (${res.status}).`;
    try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return res;
}

/* ------------------------------------------------------ SQL syntax highlight */
const SQL_KEYWORDS = new Set(("select from where group by order having limit offset join left right inner outer full cross natural on using as and or not in is null like between exists distinct all union intersect except with recursive case when then else end asc desc over partition window collate glob escape cast filter create index").split(" "));
const SQL_FUNCS = new Set(("count sum avg min max round abs coalesce ifnull nullif length lower upper substr trim strftime date datetime julianday printf replace instr total group_concat row_number rank dense_rank lag lead ntile").split(" "));

function highlightSQL(sql) {
  const re = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*')|("(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|([\s\S])/g;
  let out = "", m;
  while ((m = re.exec(sql)) !== null) {
    if (m[1]) out += `<span class="tok-cmt">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="tok-str">${esc(m[2])}</span>`;
    else if (m[3]) out += esc(m[3]);
    else if (m[4]) out += `<span class="tok-num">${esc(m[4])}</span>`;
    else if (m[5]) {
      const w = m[5].toLowerCase();
      const next = sql.slice(re.lastIndex).match(/^\s*\(/);
      if (SQL_KEYWORDS.has(w)) out += `<span class="tok-kw">${esc(m[5])}</span>`;
      else if (next && SQL_FUNCS.has(w)) out += `<span class="tok-fn">${esc(m[5])}</span>`;
      else out += esc(m[5]);
    } else out += esc(m[6]);
  }
  return out;
}

/* ------------------------------------------------------------ pipeline track */
const STAGES = [
  { key: "guard_input", label: "Guard" },
  { key: "classify_intent", label: "Intent" },
  { key: "retrieve_schema", label: "Schema" },
  { key: "draft", label: "Draft SQL", nodes: ["generate_sql", "review_sql"] },
  { key: "validate_sql", label: "Validate" },
  { key: "optimize", label: "Optimize" },
  { key: "execute_sql", label: "Execute" },
  { key: "explain", label: "Explain" },
  { key: "respond", label: "Respond" },
];
const stageOf = (node) => STAGES.findIndex((s) => s.key === node || (s.nodes || []).includes(node));

function resetTrack() {
  el.track.innerHTML = STAGES.map((s) => `<li data-stage="${s.key}" title="${esc(s.label)}"><span>${esc(s.label)}</span></li>`).join("");
  state.validations = 0;
  state.shortCircuit = false;
}

function trackRunning() {
  resetTrack();
  el.track.children[0].classList.add("active");
  setStatus("Checking the request…");
}

function trackStep(node, label) {
  const items = [...el.track.children];
  const idx = stageOf(node);
  if (node === "refuse" || node === "clarify" || node === "answer_info") {
    state.shortCircuit = true;
    items.forEach((li, i) => { li.classList.remove("active"); if (i > 1 && i < items.length - 1) li.classList.add("skipped"); });
    setStatus(label + "…");
    return;
  }
  if (idx < 0) return;
  if (node === "validate_sql") {
    state.validations += 1;
    if (state.validations > 1) {
      items[idx].querySelector("span").innerHTML = `Validate<span class="retry">×${state.validations}</span>`;
    }
  }
  items.forEach((li, i) => {
    li.classList.remove("active");
    if (state.shortCircuit && li.classList.contains("skipped")) return;  // refusal path: stages never ran
    if (i <= idx) { li.classList.add("done"); li.classList.remove("skipped"); }
    else li.classList.remove("done");   // a repair loop moves the track back
  });
  if (node === "optimize" && !el.execute.checked) items[stageOf("execute_sql")].classList.add("skipped");
  const next = items.slice(idx + 1).find((li) => !li.classList.contains("skipped"));
  if (next && node !== "respond") next.classList.add("active");
  setStatus(label + "…");
}

function trackFinish(response) {
  const items = [...el.track.children];
  items.forEach((li) => li.classList.remove("active"));
  if (response.type === "sql") {
    const r = response.results;
    const tail = r ? (r.error ? " Execution failed." : ` ${r.row_count}${r.truncated ? "+" : ""} row${r.row_count === 1 ? "" : "s"} returned in ${r.elapsed_ms} ms.`) : "";
    setStatus(`Validated read-only SQL.${tail}`);
  } else if (response.type === "refusal") {
    setStatus("Declined: " + ({ destructive: "write operations are not allowed.", injection: "the request tried to change the assistant's instructions.", out_of_scope: "outside SQL and this database." }[response.reason] || "not a database request."), "refused");
  } else if (response.type === "error") {
    items.forEach((li) => { if (!li.classList.contains("done")) li.classList.add("skipped"); });
    const v = items[stageOf("validate_sql")];
    if (response.validation) v.classList.add("failed");
    setStatus("Stopped: " + (response.validation ? "the query could not be validated." : "the request could not be completed."), "failed");
  } else if (response.type === "clarification") {
    setStatus("Waiting for more detail.");
  } else if (response.type === "cannot_answer") {
    setStatus("The schema doesn't hold the data for this request.", "refused");
  } else {
    setStatus("Answered.");
  }
}

function setStatus(text, kind = "") {
  el.status.textContent = text;
  el.status.className = "pipeline-status" + (kind ? " " + kind : "");
}

/* ---------------------------------------------------------------- messages */
function renderText(text) {
  // Minimal formatting: paragraphs and "- " bullet lines. Everything is escaped.
  const lines = String(text || "").split("\n");
  let html = "", list = [];
  const flush = () => { if (list.length) { html += "<ul>" + list.map((l) => `<li>${esc(l)}</li>`).join("") + "</ul>"; list = []; } };
  for (const line of lines) {
    if (/^\s*[-•]\s+/.test(line)) list.push(line.replace(/^\s*[-•]\s+/, ""));
    else { flush(); if (line.trim()) html += `<p>${esc(line)}</p>`; }
  }
  flush();
  return html;
}

function addUserMessage(text) {
  el.welcome.hidden = true;
  const div = document.createElement("div");
  div.className = "msg msg-user";
  div.textContent = text;
  el.messages.appendChild(div);
  scrollDown();
}

function addAgentMessage(response) {
  el.welcome.hidden = true;
  const div = document.createElement("div");
  div.className = `msg msg-agent kind-${response.type || "info"}`;
  let html = renderText(response.message);
  if (response.type === "sql" && response.explanation && response.intent !== "explain") {
    html += `<p>${esc(response.explanation)}</p>`;
  }
  if (response.sql) {
    html += `<pre class="msg-sql" title="Open in the query panel"><code>${highlightSQL(response.sql)}</code></pre>`;
  }
  if (response.type === "sql") {
    const r = response.results;
    const bits = [`<span class="ok">✓ Validated</span>`];
    if (r && !r.error) bits.push(`<span>${r.row_count}${r.truncated ? "+" : ""} row${r.row_count === 1 ? "" : "s"}</span>`);
    if (r && r.error) bits.push(`<span class="bad">Execution failed</span>`);
    bits.push(`<button type="button" class="open-panel">Show details</button>`);
    html += `<div class="msg-meta">${bits.join("")}</div>`;
  }
  div.innerHTML = html;
  const open = () => { showInSheet(response); openSheetOnMobile(); };
  div.querySelectorAll(".msg-sql, .open-panel").forEach((n) => n.addEventListener("click", open));
  el.messages.appendChild(div);
  scrollDown();
  return div;
}

function addThinking() {
  const div = document.createElement("div");
  div.className = "thinking";
  div.innerHTML = `<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span><span class="thinking-label">Working on it…</span>`;
  el.messages.appendChild(div);
  scrollDown();
  return div;
}

function scrollDown() { el.messages.scrollTop = el.messages.scrollHeight; }

/* ------------------------------------------------------------- query sheet */
function showInSheet(response) {
  if (!response || !response.sql) return;
  state.current = response;
  el.sheetEmpty.hidden = true;
  el.sheetContent.hidden = false;
  el.sqlOut.querySelector("code").innerHTML = highlightSQL(response.sql);

  const ok = response.validation && response.validation.ok;
  el.validBadge.textContent = ok ? "Validated" : "Not valid";
  el.validBadge.className = "badge " + (ok ? "ok" : "bad");

  if (response.original_sql && response.original_sql.trim() !== response.sql.trim()) {
    el.originalBox.hidden = false;
    el.originalSql.querySelector("code").innerHTML = highlightSQL(response.original_sql);
  } else {
    el.originalBox.hidden = true;
  }

  el.explanation.textContent = response.explanation || (ok ? "" : response.message || "");
  let notes = "";
  const block = (title, items, cls) => items && items.length
    ? `<h3 class="note-heading">${title}</h3><ul class="note-list ${cls}">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "";
  notes += block("Issues found", response.issues, "issues");
  notes += block("Changes made", response.changes, "changes");
  notes += block("Assumptions", response.assumptions, "assumptions");
  el.notes.innerHTML = notes;

  renderResults(response.results);
  renderChecks(response.validation);
  renderPerformance(response.optimization);
}

function renderResults(results) {
  const pane = el.paneResults;
  if (!results) {
    pane.innerHTML = `<p class="pane-empty">The query wasn't run. Turn on "Run query on the sample data" and ask again to see results.</p>`;
    return;
  }
  if (results.error) {
    pane.innerHTML = `<p class="pane-empty" style="color:var(--err)">${esc(results.error)}</p>`;
    return;
  }
  const head = results.columns.map((c) => `<th scope="col">${esc(c)}</th>`).join("");
  const body = results.rows.map((row) => "<tr>" + row.map((v) => {
    if (v === null) return `<td class="null">null</td>`;
    const isNum = typeof v === "number";
    const shown = isNum && !Number.isInteger(v) ? Math.round(v * 100) / 100 : v;
    return `<td class="${isNum ? "num" : ""}">${esc(shown)}</td>`;
  }).join("") + "</tr>").join("");
  pane.innerHTML = `
    <div class="results-meta">
      <span>${results.row_count}${results.truncated ? "+" : ""} row${results.row_count === 1 ? "" : "s"}${results.truncated ? " (display limit reached; CSV export includes more)" : ""}, ${results.elapsed_ms} ms</span>
      <button class="btn btn-small" type="button" id="downloadCsv">Download CSV</button>
    </div>
    ${results.row_count ? `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`
      : `<p class="pane-empty">The query ran successfully but matched no rows.</p>`}`;
  $("downloadCsv").addEventListener("click", downloadCsv);
}

function renderChecks(v) {
  if (!v) { el.paneChecks.innerHTML = `<p class="pane-empty">No validation data.</p>`; return; }
  const errors = v.errors || [], warnings = v.warnings || [];
  const passed = v.ok;
  const row = (cls, mark, text, sub = "") => `<li class="${cls}"><span class="mark">${mark}</span><span>${esc(text)}${sub ? `<div class="check-sub">${esc(sub)}</div>` : ""}</span></li>`;
  let html = `<ul class="check-list">`;
  html += row(passed ? "pass" : "fail", passed ? "✓" : "✕", "Syntax is valid SQLite", passed ? "Compiled by the database engine without running it." : "");
  html += row(!v.destructive ? "pass" : "fail", !v.destructive ? "✓" : "✕", "Read-only: no DELETE, UPDATE, INSERT, DROP, ALTER or TRUNCATE", "Enforced by a SQLite authorizer, not just a keyword check.");
  html += row(passed ? "pass" : "fail", passed ? "✓" : "✕", "All tables exist", (v.tables || []).join(", "));
  html += row(passed ? "pass" : "fail", passed ? "✓" : "✕", "All columns exist", `${(v.columns || []).length} column reference${(v.columns || []).length === 1 ? "" : "s"} checked`);
  const relFail = errors.some((e) => e.includes("relationship"));
  html += row(relFail ? "fail" : "pass", relFail ? "✕" : "✓", "Joins follow declared foreign keys");
  errors.forEach((e) => { html += row("fail", "✕", e); });
  warnings.forEach((w) => { html += row("warn", "!", w); });
  html += `</ul>`;
  el.paneChecks.innerHTML = html;
}

function renderPerformance(opt) {
  if (!opt || !opt.plan) { el.panePerf.innerHTML = `<p class="pane-empty">No performance data.</p>`; return; }
  let html = "";
  if (opt.cost && opt.cost.level) {
    html += `<div class="cost"><span>Estimated rows examined</span><strong>≈ ${Number(opt.cost.estimated_rows_examined).toLocaleString()}</strong>
      <span class="level level-${esc(opt.cost.level)}">${esc(opt.cost.level)} cost</span></div>`;
  }
  html += opt.suggestions && opt.suggestions.length
    ? `<ul class="perf-list">${opt.suggestions.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>`
    : `<p class="pane-empty">No performance issues found.</p>`;
  if (opt.index_recommendations && opt.index_recommendations.length) {
    html += `<h3 class="note-heading">Recommended indexes</h3><pre class="sql">${highlightSQL(opt.index_recommendations.join("\n"))}</pre>`;
  }
  html += `<h3 class="note-heading">SQLite query plan</h3><pre class="plan">${esc(opt.plan.join("\n"))}</pre>`;
  if (opt.cost && opt.cost.basis) html += `<p class="check-sub">${esc(opt.cost.basis)}</p>`;
  el.panePerf.innerHTML = html;
}

function selectTab(id) {
  for (const [tab, pane] of [["tabResults", "paneResults"], ["tabChecks", "paneChecks"], ["tabPerf", "panePerf"]]) {
    const on = tab === id;
    $(tab).setAttribute("aria-selected", on);
    $(pane).hidden = !on;
  }
}

/* ------------------------------------------------------------------ actions */
async function copySql() {
  const sql = state.current && state.current.sql;
  if (!sql) return;
  try {
    await navigator.clipboard.writeText(sql);
  } catch (e) {   // clipboard API needs a secure context; fall back
    const ta = document.createElement("textarea");
    ta.value = sql; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast("SQL copied");
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadSql() {
  if (state.current && state.current.sql) saveBlob(new Blob([state.current.sql + ";\n"], { type: "application/sql" }), "query.sql");
}

async function downloadCsv() {
  if (!state.current || !state.current.sql) return;
  try {
    const res = await api("/api/export/csv", { method: "POST", body: JSON.stringify({ sql: state.current.sql }) });
    saveBlob(await res.blob(), "query_results.csv");
    toast("CSV downloaded");
  } catch (e) {
    showBanner(esc(e.message));
  }
}

/* --------------------------------------------------------------------- chat */
async function send(text) {
  text = text.trim();
  if (!text || state.running) return;
  hideBanner();
  state.running = true;
  el.send.disabled = true;
  el.input.value = "";
  autosize();
  addUserMessage(text);
  const thinking = addThinking();
  trackRunning();

  let final = null;
  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, message: text, execute: el.execute.checked }),
    });
    if (!res.ok || !res.body) {
      let msg = `The server returned ${res.status}.`;
      try { msg = (await res.json()).error || msg; } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const event = (chunk.match(/^event: (.*)$/m) || [])[1];
        const data = (chunk.match(/^data: (.*)$/m) || [])[1];
        if (!event || !data) continue;
        const payload = JSON.parse(data);
        if (event === "step") {
          trackStep(payload.node, payload.label);
          thinking.querySelector(".thinking-label").textContent = payload.label + "…";
        } else if (event === "final") {
          final = payload;
        }
      }
    }
    if (!final) throw new Error("The connection closed before the answer arrived. Please try again.");
  } catch (e) {
    final = { response: { type: "error", message: `Couldn't reach the assistant: ${e.message}` } };
    showBanner(esc(e.message));
  } finally {
    thinking.remove();
    state.running = false;
    el.send.disabled = false;
    el.input.focus();
  }

  if (final.session_id) {
    state.sessionId = final.session_id;
    el.chatTitle.textContent = final.title || "Conversation";
  }
  const response = final.response;
  addAgentMessage(response);
  trackFinish(response);
  if (response.sql) {
    showInSheet(response);
    selectTab(response.results && !response.results.error ? "tabResults" : "tabChecks");
  }
  loadSessions();
}

/* ----------------------------------------------------------------- sessions */
async function loadSessions() {
  try {
    const data = await (await api("/api/sessions")).json();
    el.sessionEmpty.hidden = data.sessions.length > 0;
    el.sessionList.innerHTML = data.sessions.map((s) => `
      <li class="session-item ${s.id === state.sessionId ? "active" : ""}" data-id="${esc(s.id)}">
        <button type="button" class="open" title="${esc(s.title)}">${esc(s.title)}</button>
        <button type="button" class="del" aria-label="Delete conversation ${esc(s.title)}">Delete</button>
      </li>`).join("");
  } catch (e) { /* the sidebar is non-critical */ }
}

async function openSession(id) {
  try {
    const data = await (await api(`/api/sessions/${encodeURIComponent(id)}`)).json();
    clearChat();
    state.sessionId = data.id;
    el.chatTitle.textContent = data.title;
    let lastSql = null;
    for (const m of data.messages) {
      if (m.role === "user") addUserMessage(m.content);
      else { addAgentMessage(m.response); if (m.response.sql) lastSql = m.response; }
    }
    if (lastSql) { showInSheet(lastSql); setStatus("Showing the latest query from this conversation."); }
    loadSessions();
    el.rail.classList.remove("open");
  } catch (e) {
    showBanner(esc(e.message));
  }
}

async function deleteSession(id) {
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (id === state.sessionId) newConversation();
    loadSessions();
    toast("Conversation deleted");
  } catch (e) { showBanner(esc(e.message)); }
}

function clearChat() {
  el.messages.querySelectorAll(".msg, .thinking").forEach((n) => n.remove());
  el.welcome.hidden = false;
  el.sheetEmpty.hidden = false;
  el.sheetContent.hidden = true;
  state.current = null;
  resetTrack();
  setStatus("Waiting for a question.");
}

function newConversation() {
  state.sessionId = null;
  el.chatTitle.textContent = "New conversation";
  clearChat();
  hideBanner();
  loadSessions();
  el.rail.classList.remove("open");
  el.input.focus();
}

/* ------------------------------------------------------------------- schema */
async function loadSchema() {
  try {
    const data = await (await api("/api/schema")).json();
    el.dbNote.textContent = `${data.tables.length} tables, SQLite`;
    el.schemaTree.innerHTML = data.tables.map((t) => {
      const fks = Object.fromEntries(t.foreign_keys.map((f) => [f.column, `${f.ref_table}.${f.ref_column}`]));
      const cols = t.columns.map((c) => `<li title="${esc(c.note || "")}"><span>${esc(c.name)}</span><span class="type">${esc(c.type.toLowerCase())}</span>${c.primary_key ? `<span class="key">pk</span>` : ""}${fks[c.name] ? `<span class="key" title="references ${esc(fks[c.name])}">→ ${esc(fks[c.name].split(".")[0])}</span>` : ""}</li>`).join("");
      return `<details class="schema-table"><summary><span>${esc(t.name)}</span><span class="count">${t.row_count.toLocaleString()}</span></summary><ul class="schema-cols">${cols}</ul></details>`;
    }).join("");
  } catch (e) {
    el.dbNote.textContent = "Couldn't load the schema.";
  }
}

async function checkHealth() {
  try {
    const h = await (await api("/api/health")).json();
    if (!h.llm_ready) {
      showBanner(`The language model isn't configured, so questions can't be answered yet. Set <code>LLM_MODEL</code> and the provider's API key (for example <code>OPENAI_API_KEY</code>) in <code>.env</code>, then restart the server. Details: ${esc(h.llm_error)}`);
    }
  } catch (e) {
    showBanner("Can't reach the server. Check that it is running, then reload the page.");
  }
}

/* -------------------------------------------------------------------- wiring */
function autosize() {
  el.input.style.height = "auto";
  el.input.style.height = Math.min(el.input.scrollHeight, 200) + "px";
}

function openSheetOnMobile() {
  if (window.matchMedia("(max-width: 900px)").matches) el.sheet.classList.add("open");
}

function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("themeToggle").textContent = dark ? "Light mode" : "Dark mode";
  $("themeToggle").setAttribute("aria-pressed", String(dark));
  try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* private mode */ }
}

el.composer.addEventListener("submit", (e) => { e.preventDefault(); send(el.input.value); });
el.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(el.input.value); }
});
el.input.addEventListener("input", autosize);
$("examples").addEventListener("click", (e) => { const b = e.target.closest(".example"); if (b) send(b.textContent); });
$("newChat").addEventListener("click", newConversation);
$("copySql").addEventListener("click", copySql);
$("downloadSql").addEventListener("click", downloadSql);
["tabResults", "tabChecks", "tabPerf"].forEach((id) => $(id).addEventListener("click", () => selectTab(id)));
el.sessionList.addEventListener("click", (e) => {
  const item = e.target.closest(".session-item");
  if (!item) return;
  if (e.target.closest(".del")) deleteSession(item.dataset.id); else openSession(item.dataset.id);
});
$("themeToggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme !== "dark"));
$("railToggle").addEventListener("click", () => el.rail.classList.toggle("open"));
$("sheetToggle").addEventListener("click", () => el.sheet.classList.toggle("open"));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { el.rail.classList.remove("open"); el.sheet.classList.remove("open"); } });
document.addEventListener("click", (e) => {
  for (const panel of [el.rail, el.sheet]) {
    if (panel.classList.contains("open") && !panel.contains(e.target) && !e.target.closest("#railToggle, #sheetToggle, .msg-sql, .open-panel")) panel.classList.remove("open");
  }
});

setTheme(document.documentElement.dataset.theme === "dark");
resetTrack();
checkHealth();
loadSchema();
loadSessions();
