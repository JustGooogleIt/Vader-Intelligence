// Vader Command Center page. All data from the server is rendered through textContent
// (never innerHTML with data). Source time and UI refresh time are shown separately.
"use strict";
(function () {
  const state = { session: null, view: "overview", game: null, run: null, timer: null, fails: 0, page: 1 };
  const $ = (id) => document.getElementById(id);
  const el = (tag, attrs, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else if (k.startsWith("on")) node.addEventListener(k.slice(2), v); else node.setAttribute(k, v);
    }
    for (const c of children) if (c != null) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    return node;
  };
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };
  const fmt = (t) => (t ? String(t).replace("T", " ").replace(/\.\d+/, "").replace("+00:00", "Z") : "—");
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));
  const setState = (id, s, extra) => { const n = $(id); n.className = "state " + s; n.textContent = extra ? s + " · " + extra : s; };
  const countdown = (seconds) => {
    if (seconds == null) return "cutoff unknown";
    const s = Math.round(seconds); const a = Math.abs(s);
    const text = `${Math.floor(a / 3600)}h ${Math.floor((a % 3600) / 60)}m`;
    return s < 0 ? `cutoff passed ${text} ago` : `T−60 in ${text}`;
  };
  const kv = (rows) => {
    const t = el("table");
    for (const [k, v] of rows) t.appendChild(el("tr", null, el("th", { text: k }), el("td", { text: v == null ? "unknown" : String(v) })));
    return t;
  };
  const table = (head, rows) => {
    const t = el("table"), hr = el("tr");
    head.forEach((h) => hr.appendChild(el("th", { text: h })));
    t.appendChild(hr);
    rows.forEach((r) => { const tr = el("tr"); r.forEach((c) => tr.appendChild(c instanceof Node ? el("td", null, c) : el("td", { text: c == null ? "—" : String(c) }))); t.appendChild(tr); });
    return t;
  };

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ cache: "no-store" }, options || {}));
    const body = await res.json();
    if (!res.ok && !body.data) throw new Error(body.error || res.status);
    return body;
  }
  async function post(kind, run_id, payload) {
    const command_id = uuid();
    const res = await fetch("/api/commands", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Vader-Session": state.session.token },
      body: JSON.stringify({ command_id, kind, run_id, payload: payload || {} }),
    });
    const body = await res.json();
    return { status: res.status, body, command_id };
  }

  function setConnected(ok, msg) {
    const c = $("conn");
    c.className = ok ? "ok" : (state.fails > 2 ? "bad" : "stale");
    c.textContent = ok ? "connected" : (state.fails > 2 ? "disconnected: " + msg : "retrying: " + msg);
  }

  // Overview -----------------------------------------------------------------------
  function renderOverview(d) {
    const box = clear($("overview"));
    setState("overview-state", d.state);
    const labels = d.label && Object.keys(d.label).length ? Object.entries(d.label).map(([k, v]) => `${k}: ${v}`).join(", ") : "none";
    box.appendChild(el("div", { class: "note", text: `Source states — archive: ${d.sources.archive}, games: ${d.sources.games}, collection: ${d.sources.collection}, farms: ${d.sources.farms}. Labels: ${labels}.` }));
    box.appendChild(el("h3", { text: "Archive" }));
    const a = d.archive;
    box.appendChild(kv([["state", a.state], ["label", a.label || "unlabelled"], ["schema", a.schema_version], ["latest retrieval (source time)", fmt(a.latest_retrieval)], ["runs", (a.runs || []).map((r) => `${r.kind}:${r.status}`).join(", ") || "none"], ["counts", a.counts ? JSON.stringify(a.counts) : a.reason]]));
    box.appendChild(el("h3", { text: "Collection / shadow (operations state)" }));
    const c = d.collection;
    const sh = c.shadow || {};
    box.appendChild(kv([["state", c.state], ["status", c.status || c.reason], ["age (s)", c.age_seconds == null ? null : Math.round(c.age_seconds)], ["cadence (s)", c.cadence], ["label", c.label], ["shadow", sh.status ? `${sh.status} — ${sh.meaning}` : "n/a"], ["archive matches ops database", c.database_matches_archive]]));
    box.appendChild(el("h3", { text: "Contracts & coverage" }));
    const cov = d.coverage || {};
    box.appendChild(kv([["eligible contracts", d.contracts.eligible], ["excluded contracts", d.contracts.excluded], ["markets with a book", cov.markets == null ? null : `${cov.with_book} / ${cov.markets}`], ["newest book (source time)", fmt(cov.newest_book)]]));
    box.appendChild(el("h3", { text: "Farms & budgets" }));
    const b = d.budgets;
    const runs = d.farms.runs || [];
    box.appendChild(kv([["active runs", runs.filter((r) => !["canceled", "completed", "failed"].includes(r.state)).length], ["total runs", runs.length], ["active agent processes", b.active_processes], ["global cap", b.global_agent_cap], ["provider cost (USD)", b.cost_usd == null ? "unknown (not metered)" : b.cost_usd]]));
    if (b.pods) box.appendChild(table(["pod", "role", "max concurrent", "tasks today", "cpu s today", "waiting"], b.pods.map((p) => [p.pod_id, p.role, p.budgets.max_concurrent, p.usage.tasks_today, p.usage.cpu_seconds_today == null ? "unknown" : Math.round(p.usage.cpu_seconds_today), p.waiting_tasks])));
    box.appendChild(el("h3", { text: "Failures" }));
    if (!d.failures.length) box.appendChild(el("div", { class: "note", text: "none reported" }));
    else box.appendChild(table(["source", "detail"], d.failures.map((f) => [f.source, typeof f.detail === "string" ? f.detail : JSON.stringify(f.detail)])));
  }

  function renderGames(d) {
    const list = clear($("games-list"));
    setState("games-state", d.state, d.label || undefined);
    if (d.state !== "ok") { list.appendChild(el("div", { class: "note", text: d.reason || (d.state === "empty" ? "no games in the archive" : d.state) })); return; }
    for (const g of d.games) {
      const node = el("div", { class: "game" + (state.game === g.game_id ? " selected" : ""), onclick: () => { state.game = g.game_id; state.view = "game"; refresh(); } });
      node.appendChild(el("div", { class: "teams", text: `${g.away.name || "?"} @ ${g.home.name || "?"}` }));
      node.appendChild(el("div", { class: "meta", text: `game ${g.game_id} · ${g.event_ticker || g.event_tickers.join("/") || "no event"} · start ${fmt(g.scheduled_start)}` }));
      const line = el("div", { class: "meta", text: countdown(g.countdown_seconds) + ` · ${g.eligible_contracts} eligible / ${g.excluded_contracts} excluded` });
      if (g.identity === "ambiguous") line.appendChild(el("span", { class: "badge bad", text: "ambiguous: " + g.identity_reasons.join(", ") }));
      if (d.label) line.appendChild(el("span", { class: "badge fixture", text: d.label }));
      node.appendChild(line);
      list.appendChild(node);
    }
    const u = clear($("unmapped"));
    u.textContent = d.unmapped.length ? d.unmapped.map((x) => `${x.ticker}: ${x.reason}`).join("; ") : "none";
  }

  // Game workspace -----------------------------------------------------------------
  function renderGame(d) {
    const box = clear($("game"));
    setState("game-state", d.state);
    if (d.state !== "ok") { box.appendChild(el("div", { class: "note", text: d.reason || d.state })); return; }
    const g = d.game;
    for (const w of d.warnings) box.appendChild(el("div", { class: "warn", text: "⚠ " + w }));
    box.appendChild(el("h3", { text: `${g.away.name} (${g.away.id}) @ ${g.home.name} (${g.home.id})` }));
    box.appendChild(kv([["canonical game id", g.game_id], ["identity", g.identity + (g.identity_reasons.length ? ": " + g.identity_reasons.join(", ") : "")], ["event ticker", g.event_ticker], ["markets", d.pins.market_tickers.join(", ")], ["scheduled start (official)", fmt(g.scheduled_start)], ["official date / status", `${g.official_date || "—"} / ${g.status || "—"}`], ["cutoff (T−60)", fmt(g.cutoff) + " · " + countdown(g.countdown_seconds)], ["source runs", d.pins.source_runs.join(", ")], ["label", d.pins.label || "unlabelled"], ["can launch a farm", g.can_launch]]));
    box.appendChild(el("h3", { text: "Mapping / eligibility (latest per contract)" }));
    box.appendChild(table(["ticker", "eligible", "reason", "cutoff", "checked (source)", "schedule fetch", "market fetch"], g.contracts.map((c) => [c.ticker, c.eligible, c.reason, fmt(c.cutoff), fmt(c.checked_at), c.schedule_fetch_id, c.market_fetch_id])));
    box.appendChild(el("h3", { text: `Evidence timeline (newest first, ${d.observations.length} observations, ${d.eligibility.length} checks)` }));
    const timeline = [];
    d.eligibility.forEach((e) => timeline.push([fmt(e.checked_at), "eligibility", e.ticker, `${e.reason} (game ${e.game_id})`]));
    d.observations.forEach((o) => timeline.push([fmt(o.retrieved_at), o.kind, o.ticker, o.kind === "book" ? `yes ${o.data.yes_bid_dollars || "—"}/${o.data.yes_ask_dollars || "—"} no ${o.data.no_bid_dollars || "—"}/${o.data.no_ask_dollars || "—"} ${(o.data.flags || []).join(",")}` : JSON.stringify(o.data).slice(0, 120)]));
    d.fetches.forEach((f) => timeline.push([fmt(f.retrieved_at), "fetch:" + f.stage, f.id, `${f.state} ${f.status} ${f.url}`]));
    timeline.sort((x, y) => (x[0] < y[0] ? 1 : -1));
    box.appendChild(table(["source time", "kind", "subject", "detail"], timeline.slice(0, 100)));
    box.appendChild(el("h3", { text: "Frozen forecasts & publication" }));
    if (!d.decisions.length) box.appendChild(el("div", { class: "note", text: "no decisions recorded for this game (empty, not zero)" }));
    else box.appendChild(table(["decision", "mode", "forward evidence", "cutoff", "created", "frozen", "publication"], d.decisions.map((x) => [x.decision_id, x.mode, x.forward_evidence, fmt(x.cutoff), fmt(x.created_at), JSON.stringify(x.frozen).slice(0, 160), x.publication ? `${x.publication.verdict}/${x.publication.status} receipts ${x.publication.receipts}` : "not attempted"])));
    box.appendChild(el("h3", { text: "Official results / settlement" }));
    const s = d.settlement;
    if (!s.available) box.appendChild(el("div", { class: "note", text: "settlement tables absent (archive schema < 2)" }));
    else if (!s.versions.length) box.appendChild(el("div", { class: "note", text: "no settlement versions for this game's markets" }));
    else box.appendChild(table(["kind", "key", "rev", "change", "digest"], s.versions.map((v) => [v.kind, v.entity_key, v.revision, v.change_kind, v.digest.slice(0, 12)])));
    box.appendChild(el("h3", { text: "Scored evaluation" }));
    const ev = d.evaluation;
    if (!ev.available || !ev.items.length) box.appendChild(el("div", { class: "note", text: "no evaluation items for this game" }));
    else box.appendChild(table(["evaluation", "mode", "view", "created", "decision", "scored", "outcome"], ev.items.map((i) => [i.evaluation_id, i.mode, i.view, fmt(i.created_at), i.decision_id, i.scored, i.outcome])));
    box.appendChild(el("h3", { text: "Research farms for this game" }));
    const ctl = el("div", { class: "controls" });
    const start = el("button", { class: "primary", text: "Start evidence_review_v1 farm", onclick: () => startFarm(g) });
    start.disabled = !g.can_launch;
    ctl.appendChild(start);
    ctl.appendChild(el("span", { class: "note", text: g.can_launch ? "roles: evidence_inspector, baseline_analyst, independent_checker (fixed Python operations under the pods boundary — not an LLM agent farm); model_analyst and llm_agent unavailable" : "launch disabled: " + (g.identity_reasons.join(", ") || "identity") }));
    box.appendChild(ctl);
    if (!d.runs.length) box.appendChild(el("div", { class: "note", text: "no runs for this game" }));
    else box.appendChild(table(["run", "state", "template", "stage", "created", "outcome", ""], d.runs.map((r) => [r.run_id.slice(0, 8), r.state + (r.paused ? " (paused)" : ""), r.template, r.stage, fmt(r.created_at), r.outcome, el("button", { text: "open", onclick: () => { state.run = r.run_id; state.view = "farm"; refresh(); } })])));
    $("command-log") || box.appendChild(el("div", { id: "command-log", class: "note" }));
  }

  async function startFarm(g) {
    const r = await post("start", null, { game_id: g.game_id, event_ticker: g.event_ticker, template: "evidence_review_v1", roles: ["evidence_inspector", "baseline_analyst", "independent_checker"], limits: { max_concurrent: 2, max_tasks: 3, wall_seconds: 600 }, execution: "pods" });
    logCommand(r);
    refresh();
  }
  function logCommand(r) {
    const log = $("command-log") || $("farm");
    const rc = r.body.receipt || {};
    log.appendChild(el("div", { class: "note", text: `command ${r.command_id.slice(0, 8)} → HTTP ${r.status} ${r.body.ok ? (r.body.stored ? "accepted" : "duplicate (one run)") : r.body.error} · receipt ${rc.state || "—"} ${rc.reason || ""}` }));
  }

  // Farm view ----------------------------------------------------------------------
  function renderFarms(d) {
    const box = clear($("farm"));
    setState("farm-state", d.state);
    if (d.state === "unavailable") { box.appendChild(el("div", { class: "bad", text: d.reason })); return; }
    const runs = d.runs || [];
    box.appendChild(kv([["active agent processes", d.active_processes], ["global cap", d.global_cap], ["orchestration store", d.pods ? `queued ${d.pods.queued}, failed ${d.pods.failed}, quarantined ${d.pods.quarantined}, active ${d.pods.active_attempts.length}` : "absent"]]));
    if (!runs.length) { box.appendChild(el("div", { class: "note", text: "no runs" })); return; }
    box.appendChild(table(["run", "kind", "state", "template", "stage", "game", "created", ""], runs.map((r) => [r.run_id.slice(0, 8), r.kind, r.state + (r.paused ? " (paused)" : ""), r.template, r.stage, r.pins.game_id || r.pins.issue || "—", fmt(r.created_at), el("button", { text: "open", onclick: () => { state.run = r.run_id; refresh(); } })])));
    if (state.run) renderFarm(box);
  }
  async function renderFarm(box) {
    const r = await api("/api/farms/" + state.run);
    const d = r.data;
    const sub = el("div");
    box.appendChild(sub);
    if (d.state !== "ok") { sub.appendChild(el("div", { class: "note", text: d.reason || d.state })); return; }
    const run = d.run;
    sub.appendChild(el("h3", { text: `Run ${run.run_id} — ${run.state}${run.paused ? " (paused: no new dispatch)" : ""}` }));
    sub.appendChild(kv([["kind / template", `${run.kind} / ${run.template}`], ["pins", JSON.stringify(run.pins)], ["limits", JSON.stringify(run.limits)], ["pod", run.pod_id], ["stage", run.stage], ["created / finished (source)", `${fmt(run.created_at)} / ${fmt(run.finished_at)}`], ["outcome", run.outcome], ["detail", JSON.stringify(run.detail)]]));
    const ctl = el("div", { class: "controls" });
    const terminal = ["canceled", "completed", "failed"].includes(run.state);
    for (const [kind, label] of [["pause", "Pause new dispatch"], ["resume", "Resume"], ["cancel", "Cancel owned work"]]) {
      const b = el("button", { text: label, onclick: async () => { const res = await post(kind, run.run_id, {}); logCommand(res); refresh(); } });
      b.disabled = terminal || (kind === "pause" && run.paused) || (kind === "resume" && !run.paused);
      ctl.appendChild(b);
    }
    ctl.appendChild(el("span", { class: "note", text: "scope: this run's owned tasks only; collection services untouched" }));
    sub.appendChild(ctl);
    sub.appendChild(el("div", { id: "command-log", class: "note" }));
    sub.appendChild(el("h3", { text: "Tasks" }));
    sub.appendChild(table(["role", "stage", "task", "type", "state", "attempts", "elapsed (s)", "result artifacts"], d.tasks.map((t) => {
      const last = t.attempts[t.attempts.length - 1];
      const arts = el("span");
      (t.result ? t.result.artifacts : []).forEach((a) => arts.appendChild(el("button", { text: a.name, onclick: () => showArtifact(run.run_id, a.sha256) })));
      return [t.role, t.stage, t.task_id.slice(0, 10), t.type, t.state, t.attempts.map((a) => `#${a.n} ${a.outcome || "running"} pid ${a.worker_pid || "—"} lease ${fmt(a.lease_expires_at)}`).join("; ") || "none", last && last.elapsed_seconds != null ? Math.round(last.elapsed_seconds) : "—", arts];
    })));
    sub.appendChild(el("div", { id: "artifact" }));
    renderActivity(run.run_id, d.workers);
  }
  async function showArtifact(run_id, sha) {
    const r = await api(`/api/farms/${run_id}/artifacts/${sha}`);
    const box = clear($("artifact"));
    box.appendChild(el("div", { class: "meta", text: `artifact ${sha.slice(0, 16)} · ${r.data.state}${r.data.truncated ? " · truncated" : ""}` }));
    box.appendChild(el("pre", { text: r.data.text || r.data.reason || "" }));
  }
  async function renderActivity(run_id, workers) {
    const box = clear($("activity"));
    box.appendChild(el("h3", { text: "Workers / panes" }));
    if (!workers.length) box.appendChild(el("div", { class: "note", text: "no registered workers" }));
    for (const w of workers) {
      const row = el("div", { class: "game" });
      row.appendChild(el("div", { text: `${w.worker_id} · ${w.role} · attempt ${(w.attempt_id || "—").slice(0, 10)} · pane ${w.pane_id || "—"}` }));
      row.appendChild(el("button", { text: "capture registered pane", onclick: async () => {
        const r = await api(`/api/farms/${run_id}/workers/${w.worker_id}/pane`);
        const pre = el("pre", { text: r.data.state === "ok" ? r.data.text : `${r.data.state}: ${r.data.reason || ""}` });
        row.appendChild(el("div", { class: "meta", text: `identity ${r.data.identity || "unknown"} · observed ${fmt(r.data.observed_at)} · text is data, never completion` }));
        row.appendChild(pre);
      } }));
      box.appendChild(row);
    }
    const act = await api(`/api/farms/${run_id}/activity?page=${state.page}`);
    box.appendChild(el("h3", { text: `Activity (page ${act.data.page}, ${act.data.page_size} per page)` }));
    const nav = el("div", { class: "controls" });
    nav.appendChild(el("button", { text: "newer", onclick: () => { state.page = Math.max(1, state.page - 1); refresh(); } }));
    nav.appendChild(el("button", { text: "older", onclick: () => { state.page += 1; refresh(); } }));
    box.appendChild(nav);
    if (!act.data.events.length) box.appendChild(el("div", { class: "note", text: "no events on this page" }));
    else box.appendChild(table(["at", "task", "kind", "detail"], act.data.events.map((e) => [fmt(e.at), e.task_id.slice(0, 10), e.kind, JSON.stringify(e.detail).slice(0, 120)])));
  }

  // Engineering --------------------------------------------------------------------
  function renderEngineering(d) {
    const box = clear($("engineering"));
    setState("eng-state", d.state);
    if (!d.runs || !d.runs.length) { box.appendChild(el("div", { class: "note", text: "no maintenance runs" })); return; }
    box.appendChild(table(["run", "state", "issue", "executor", "writer commit", "checks", "review", "recommendation", "integration"], d.runs.map((r) => {
      const dt = r.detail || {};
      return [r.run_id.slice(0, 8), r.state, r.pins.issue || "—", (r.pins.executor || "—") + (r.pins.simulated ? " (simulated)" : ""), (dt.commit || "—").slice(0, 12), dt.checks ? dt.checks.map((c) => `${c.name}:${c.exit_code}`).join(" ") : "not run", dt.review ? dt.review.verdict : "pending", dt.recommendation || "—", dt.integration || "not merged (manual)"];
    })));
    box.appendChild(el("div", { class: "note", text: "Worker claims stay unverified until the trusted check/review path establishes them. Nothing merges automatically." }));
  }

  // Refresh loop ---------------------------------------------------------------------
  function showView() {
    for (const v of ["overview", "game", "farm", "engineering"]) $("view-" + v).classList.toggle("hidden", v !== state.view);
    document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("primary", b.dataset.view === state.view));
  }
  let refresh = async function () {
    showView();
    try {
      if (!state.session) state.session = (await api("/api/session"));
      const [ov, gm] = await Promise.all([api("/api/overview"), api("/api/games")]);
      renderOverview(ov.data);
      renderGames(gm.data);
      $("ui-time").textContent = fmt(ov.data.generated_at) + (ov.data.latency && ov.data.latency.p95_ms != null ? ` (read p95 ${ov.data.latency.p95_ms} ms, n=${ov.data.latency.samples})` : "");
      $("source-time").textContent = fmt(ov.data.source_time);
      if (state.view === "game" && state.game != null) renderGame((await api("/api/games/" + state.game)).data);
      if (state.view === "farm") renderFarms((await api("/api/farms")).data);
      if (state.view === "engineering") renderEngineering((await api("/api/engineering")).data);
      state.fails = 0; setConnected(true);
    } catch (e) {
      state.fails += 1; setConnected(false, e.message);
    }
    clearTimeout(state.timer);
    state.timer = setTimeout(refresh, (state.session ? state.session.refresh_seconds : 5) * 1000);
  }
  // Deep links: #overview, #game/<game_id>, #farm/<run_id>, #engineering (data never comes from the hash).
  function readHash() {
    const m = /^#(overview|game|farm|engineering)(?:\/([A-Za-z0-9-]{1,64}))?$/.exec(location.hash || "");
    if (!m) return;
    state.view = m[1];
    if (m[1] === "game" && m[2] && /^[0-9]{1,12}$/.test(m[2])) state.game = Number(m[2]);
    if (m[1] === "farm" && m[2]) state.run = m[2];
  }
  function writeHash() {
    const target = state.view === "game" && state.game != null ? `#game/${state.game}` : state.view === "farm" && state.run ? `#farm/${state.run}` : `#${state.view}`;
    if (location.hash !== target) history.replaceState(null, "", target);
  }
  const originalRefresh = refresh;
  refresh = async function () { writeHash(); return originalRefresh(); };
  document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => { state.view = b.dataset.view; refresh(); }));
  window.addEventListener("hashchange", () => { readHash(); refresh(); });
  readHash();
  refresh();
})();
