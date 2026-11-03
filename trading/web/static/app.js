"use strict";
/* Thin UI over the Python engine: no trading logic lives here. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const SVG = "http://www.w3.org/2000/svg";

const state = { result: null, ticker: null, csvText: null };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n, sign = false) => {
  const v = Number(n) || 0;
  const s = Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (v < 0 ? "−$" : sign && v > 0 ? "+$" : "$") + s;
};
const px = (n) => (Number(n) || 0).toFixed(2);
const hhmm = (iso) => (iso || "").slice(11, 16);
const key = (iso) => (iso || "").slice(0, 16);
const tone = (v) => (v > 0 ? "gain" : v < 0 ? "loss" : "");

/* ---------------- config form ---------------- */

const NUM_FIELDS = ["account_size", "risk_per_trade_pct", "max_daily_loss_pct", "max_open_positions",
  "max_trades_per_day", "min_reward_risk", "target_r_multiple", "swing_lookback", "volume_lookback",
  "news_blackout_minutes"];

function fillForm(cfg) {
  const f = $("#config-form");
  for (const [k, v] of Object.entries(cfg)) {
    const el = f.elements[k];
    if (!el) continue;
    if (k === "tickers") el.value = v.join(", ");
    else if (k === "news_events") el.value = v.map((e) => `${e.at.slice(0, 16)} ${e.name}${e.ticker ? " @" + e.ticker : ""}`).join("\n");
    else if (el instanceof RadioNodeList) { for (const r of el) r.checked = r.value === v; }
    else if (el.type === "checkbox") el.checked = !!v;
    else el.value = v;
  }
}

function readForm() {
  const f = $("#config-form");
  const cfg = {
    broker: f.broker.value.trim() || "paper",
    tickers: f.tickers.value.split(/[\s,]+/).map((t) => t.trim().toUpperCase()).filter(Boolean),
    trade_start: f.trade_start.value,
    trade_end: f.trade_end.value,
    allow_short: f.allow_short.checked,
    strategy_description: f.strategy_description.value.trim(),
    decider: f.decider.value || "rules",
    news_events: [],
  };
  for (const name of NUM_FIELDS) cfg[name] = Number(f[name].value);
  const bad = [];
  f.news_events.value.split("\n").map((l) => l.trim()).filter(Boolean).forEach((line, i) => {
    const m = line.match(/^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})\s+(.+?)(?:\s+@([A-Za-z.]+))?$/);
    if (!m) { bad.push(i + 1); return; }
    cfg.news_events.push({ at: m[1].replace(" ", "T") + ":00", name: m[2], ticker: m[3] ? m[3].toUpperCase() : null });
  });
  if (bad.length) throw new Error(`News events line ${bad.join(", ")}: use "YYYY-MM-DDTHH:MM Name [@TICKER]".`);
  if (!cfg.tickers.length) throw new Error("Add at least one ticker.");
  return cfg;
}

/* ---------------- API ---------------- */

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: `Server returned ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function run() {
  const err = $("#form-error");
  err.hidden = true;
  let config;
  try { config = readForm(); } catch (e) { return showError(e.message); }

  const source = $("input[name=source]:checked").value;
  const payload = { config, source };
  if (source === "demo") payload.seed = Number($("#seed").value) || 0;
  else {
    if (!state.csvText) return showError("Choose a CSV file first.");
    payload.csv = state.csvText;
  }

  setBusy(true);
  try {
    const [result, prompt] = await Promise.all([api("/api/run", payload), api("/api/prompt", { config })]);
    state.result = result;
    state.result.prompt = prompt.prompt;
    render();
  } catch (e) {
    showError(e.message);
  } finally {
    setBusy(false);
  }
}

function showError(msg) {
  const err = $("#form-error");
  err.textContent = msg;
  err.hidden = false;
}

function setBusy(busy) {
  $$("#run, [data-run]").forEach((b) => { b.disabled = busy; });
  $("#run").textContent = busy ? "Running…" : "Run paper session";
  $("#loading").hidden = !busy;
  if (busy) $("#empty").hidden = true;
}

/* ---------------- rendering ---------------- */

function totals(summaries) {
  const t = { trades: 0, closed: 0, wins: 0, pnl: 0, blocked: 0, violations: [], best: 0, worst: 0, days: summaries.length };
  for (const s of summaries) {
    t.trades += s.trades_taken; t.closed += s.trades_closed; t.wins += s.wins;
    t.pnl += s.realized_pnl; t.blocked += s.blocked_proposals.length;
    t.best = Math.max(t.best, s.best_trade); t.worst = Math.min(t.worst, s.worst_trade);
    t.violations.push(...s.rule_violations.map((v) => `${s.date}: ${v}`));
  }
  t.winRate = t.closed ? (100 * t.wins) / t.closed : 0;
  return t;
}

function render() {
  const r = state.result;
  $("#output").hidden = false;
  const t = totals(r.summaries);
  const acct = r.config.account_size;
  const cards = [
    ["Realized P&L", `<span class="${tone(t.pnl)}">${money(t.pnl, true)}</span>`, `${((100 * t.pnl) / acct).toFixed(2)}% of ${money(acct)}`],
    ["Win rate", `${t.winRate.toFixed(0)}%`, `${t.wins}W / ${t.closed - t.wins}L`],
    ["Trades taken", t.trades, `max ${r.config.max_trades_per_day}/day · ${t.days} day${t.days === 1 ? "" : "s"}`],
    ["Best / worst", `<span class="${tone(t.best)}">${money(t.best)}</span>`, `worst <span class="${tone(t.worst)}">${money(t.worst)}</span>`],
    ["Blocked by rules", `<span class="${t.blocked ? "warn" : ""}">${t.blocked}</span>`, "proposals rejected"],
    ["Rule violations", `<span class="${t.violations.length ? "loss" : "gain"}">${t.violations.length}</span>`, t.violations.length ? "see Summary" : "none"],
  ];
  $("#cards").innerHTML = cards.map(([k, v, s]) => `<div class="card"><div class="k">${k}</div><div class="v num">${v}</div><div class="s">${s}</div></div>`).join("");

  const tickers = Object.keys(r.series);
  if (!tickers.includes(state.ticker)) {
    const traded = allTrades().map((t) => t.ticker);
    state.ticker = tickers.find((tk) => traded.includes(tk)) || tickers[0] || null;
  }
  $("#ticker-pills").innerHTML = tickers.map((tk) => `<button type="button" data-ticker="${esc(tk)}" aria-pressed="${tk === state.ticker}">${esc(tk)}</button>`).join("");
  $("#log-ticker").innerHTML = `<option value="">All tickers</option>` + tickers.map((tk) => `<option>${esc(tk)}</option>`).join("");

  renderChart();
  renderEquity();
  renderTrades();
  renderLog();
  $("#summary-text").textContent = r.summary_text || "No sessions.";
  $("#violations").innerHTML = t.violations.length
    ? `<div class="callout bad"><b>Rule violations</b><br>${t.violations.map(esc).join("<br>")}</div>`
    : `<div class="callout ok">No rule violations. Every executed order passed the risk checks.</div>`;
  $("#prompt-text").textContent = r.prompt || "";
}

function allTrades() {
  return state.result.summaries.flatMap((s) => s.trades);
}

function renderTrades() {
  const trades = allTrades();
  const head = `<thead><tr><th>Opened</th><th>Closed</th><th>Ticker</th><th>Side</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Exit</th><th class="r">P&amp;L</th><th>Exit reason</th></tr></thead>`;
  const rows = trades.length ? trades.map((t) => `<tr>
      <td class="num">${esc(t.opened_at.slice(5, 16).replace("T", " "))}</td>
      <td class="num">${esc(hhmm(t.closed_at))}</td>
      <td><b>${esc(t.ticker)}</b></td>
      <td><span class="pill ${t.side === "long" ? "BUY" : "SHORT"}">${esc(t.side.toUpperCase())}</span></td>
      <td class="r num">${t.quantity}</td>
      <td class="r num">${px(t.entry)}</td>
      <td class="r num">${px(t.exit)}</td>
      <td class="r num ${tone(t.pnl)}">${money(t.pnl, true)}</td>
      <td>${esc(t.exit_reason)}</td></tr>`).join("")
    : `<tr><td colspan="9" class="empty-row">No trades. The rules held all session, which is often the right call.</td></tr>`;
  $("#trades").innerHTML = head + `<tbody>${rows}</tbody>`;
}

function renderLog() {
  const filter = $("input[name=logfilter]:checked").value;
  const tk = $("#log-ticker").value;
  let rows = state.result.decisions.filter((d) => !tk || d.ticker === tk);
  if (filter === "actions") rows = rows.filter((d) => d.executed.action !== "HOLD" || d.blocked.length);
  if (filter === "blocked") rows = rows.filter((d) => d.blocked.length);
  const shown = rows.slice(0, 1500);
  const head = `<thead><tr><th>Time</th><th>Ticker</th><th>Action</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Stop</th><th class="r">Target</th><th class="r">Conf.</th><th>By</th><th>Reason</th></tr></thead>`;
  const body = shown.length ? shown.map((d) => {
    const e = d.executed;
    const blocked = d.blocked.length > 0;
    const isEntry = e.action === "BUY" || e.action === "SHORT";
    const reason = [
      `<span>${esc(blocked ? d.proposed.reason : e.reason)}</span>`,
      ...d.blocked.map((b) => `<span class="blk">Blocked (${esc(d.proposed.action)}): ${esc(b)}</span>`),
      ...d.adjustments.map((a) => `<span class="adj">Adjusted: ${esc(a)}</span>`),
    ].join("");
    return `<tr>
      <td class="num">${esc(d.time.slice(5, 16).replace("T", " "))}</td>
      <td><b>${esc(d.ticker)}</b></td>
      <td><span class="pill ${blocked ? "BLOCKED" : e.action}">${blocked ? "BLOCKED" : e.action}</span></td>
      <td class="r num">${isEntry ? e.quantity : ""}</td>
      <td class="r num">${isEntry ? px(e.entry) : ""}</td>
      <td class="r num">${isEntry ? px(e.stop_loss) : ""}</td>
      <td class="r num">${isEntry ? px(e.take_profit) : ""}</td>
      <td class="r num">${isEntry ? e.confidence : ""}</td>
      <td>${esc(d.decider)}</td>
      <td class="reason">${reason}</td></tr>`;
  }).join("") : `<tr><td colspan="10" class="empty-row">Nothing matches this filter.</td></tr>`;
  const more = rows.length > shown.length ? `<tr><td colspan="10" class="empty-row">Showing first ${shown.length} of ${rows.length}.</td></tr>` : "";
  $("#log").innerHTML = head + `<tbody>${body}${more}</tbody>`;
}

/* ---------------- charts (plain SVG) ---------------- */

function el(name, attrs = {}, parent) {
  const n = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function frame(svg) {
  svg.innerHTML = "";
  const w = svg.clientWidth || 800;
  const h = svg.clientHeight || 360;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  return { w, h, m: { l: 56, r: 12, t: 12, b: 26 } };
}

function scale(d0, d1, r0, r1) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
}

function niceTicks(lo, hi, n = 5) {
  const step0 = (hi - lo) / n || 1;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= step0);
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(+v.toFixed(6));
  return ticks;
}

function axes(svg, f, y, lo, hi, labels, x, fmtY) {
  const g = el("g", {}, svg);
  for (const v of niceTicks(lo, hi)) {
    el("line", { x1: f.m.l, x2: f.w - f.m.r, y1: y(v), y2: y(v), stroke: cssVar("--grid") }, g);
    el("text", { x: f.m.l - 8, y: y(v) + 4, "text-anchor": "end" }, g).textContent = fmtY(v);
  }
  const every = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor((f.w - f.m.l) / 70))));
  labels.forEach((lab, i) => {
    if (i % every) return;
    const anchor = i === 0 ? "start" : x(i) > f.w - f.m.r - 24 ? "end" : "middle";
    el("text", { x: x(i), y: f.h - 8, "text-anchor": anchor }, g).textContent = lab;
  });
}

function path(points) {
  return points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
}

function renderChart() {
  const svg = $("#chart");
  const data = state.result.series[state.ticker] || [];
  const f = frame(svg);
  if (data.length < 2) { el("text", { x: f.w / 2, y: f.h / 2, "text-anchor": "middle" }, svg).textContent = "Not enough bars to chart."; return; }

  const vals = data.flatMap((b) => [b.h, b.l, b.vwap]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
  const x = scale(0, data.length - 1, f.m.l, f.w - f.m.r);
  const y = scale(lo, hi, f.h - f.m.b, f.m.t);
  const multiDay = data[0].t.slice(0, 10) !== data[data.length - 1].t.slice(0, 10);
  axes(svg, f, y, lo, hi, data.map((b) => (multiDay ? b.t.slice(5, 10) + " " : "") + hhmm(b.t)), x, (v) => v.toFixed(2));

  // trading window shading
  const cfg = state.result.config;
  data.forEach((b, i) => {
    const t = hhmm(b.t);
    if (t < cfg.trade_start || t >= cfg.trade_end) {
      el("rect", { x: x(i) - (x(1) - x(0)) / 2, y: f.m.t, width: Math.max(1, x(1) - x(0)), height: f.h - f.m.t - f.m.b, fill: cssVar("--panel-2"), opacity: 0.8 }, svg);
    }
  });

  const line = (k, color, width, dash) => el("path", {
    d: path(data.map((b, i) => [x(i), y(b[k])])), fill: "none", stroke: color, "stroke-width": width,
    "stroke-dasharray": dash || "", "stroke-linejoin": "round",
  }, svg);
  line("ema21", cssVar("--ema21"), 1.2, "4 3");
  line("ema9", cssVar("--ema9"), 1.2, "4 3");
  line("vwap", cssVar("--vwap"), 1.6);
  line("c", cssVar("--price"), 1.8);

  // markers
  const idx = new Map(data.map((b, i) => [key(b.t), i]));
  const entries = state.result.decisions.filter((d) => d.ticker === state.ticker && ["BUY", "SHORT"].includes(d.executed.action));
  for (const d of entries) {
    const i = idx.get(key(d.time)); if (i === undefined) continue;
    const cx = x(i), cy = y(d.executed.entry), up = d.executed.action === "BUY";
    const pts = up ? `${cx},${cy + 3} ${cx - 6},${cy + 13} ${cx + 6},${cy + 13}` : `${cx},${cy - 3} ${cx - 6},${cy - 13} ${cx + 6},${cy - 13}`;
    el("polygon", { points: pts, fill: up ? cssVar("--gain") : cssVar("--loss") }, svg);
    // stop / target guides
    for (const [lvl, col] of [[d.executed.stop_loss, "--loss"], [d.executed.take_profit, "--gain"]]) {
      if (lvl > lo && lvl < hi) el("line", { x1: cx, x2: Math.min(f.w - f.m.r, cx + 60), y1: y(lvl), y2: y(lvl), stroke: cssVar(col), "stroke-width": 1, "stroke-dasharray": "2 2" }, svg);
    }
  }
  for (const t of allTrades().filter((t) => t.ticker === state.ticker)) {
    let i = idx.get(key(t.closed_at));
    if (i === undefined) i = data.length - 1;
    el("circle", { cx: x(i), cy: y(t.exit), r: 4.5, fill: cssVar("--panel"), stroke: t.pnl >= 0 ? cssVar("--gain") : cssVar("--loss"), "stroke-width": 2 }, svg);
  }

  // hover
  const cross = el("line", { y1: f.m.t, y2: f.h - f.m.b, stroke: cssVar("--muted"), "stroke-width": 1, visibility: "hidden" }, svg);
  const hit = el("rect", { x: f.m.l, y: f.m.t, width: f.w - f.m.l - f.m.r, height: f.h - f.m.t - f.m.b, fill: "transparent" }, svg);
  const tip = $("#tip");
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const sx = ((ev.clientX - rect.left) / rect.width) * f.w;
    const i = Math.max(0, Math.min(data.length - 1, Math.round(((sx - f.m.l) / (f.w - f.m.l - f.m.r)) * (data.length - 1))));
    const b = data[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    const dec = state.result.decisions.find((d) => d.ticker === state.ticker && key(d.time) === key(b.t));
    tip.innerHTML = `<b>${esc(state.ticker)} · ${esc(b.t.slice(0, 10))} ${esc(hhmm(b.t))}</b>
      <div class="row"><span>Close</span><span class="num">${px(b.c)}</span></div>
      <div class="row"><span>High / Low</span><span class="num">${px(b.h)} / ${px(b.l)}</span></div>
      <div class="row"><span>VWAP</span><span class="num">${px(b.vwap)}</span></div>
      <div class="row"><span>EMA 9 / 21</span><span class="num">${px(b.ema9)} / ${px(b.ema21)}</span></div>
      <div class="row"><span>Volume</span><span class="num">${Math.round(b.v).toLocaleString()}</span></div>
      ${dec ? `<div style="margin-top:4px"><span class="pill ${dec.blocked.length ? "BLOCKED" : dec.executed.action}">${dec.blocked.length ? "BLOCKED" : dec.executed.action}</span></div>` : ""}`;
    tip.hidden = false;
    const tx = (x(i) / f.w) * rect.width;
    tip.style.left = `${Math.min(rect.width - tip.offsetWidth - 4, Math.max(4, tx + 12))}px`;
    tip.style.top = `12px`;
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
}

function renderEquity() {
  const svg = $("#equity");
  const f = frame(svg);
  f.m.t = 10; f.m.b = 22;
  const start = state.result.config.account_size;
  const trades = [...allTrades()].sort((a, b) => a.closed_at.localeCompare(b.closed_at));
  const pts = [{ label: "start", v: start }];
  let eq = start;
  for (const t of trades) { eq += t.pnl; pts.push({ label: hhmm(t.closed_at), v: eq }); }
  const vals = pts.map((p) => p.v);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = Math.max((hi - lo) * 0.15, start * 0.001); lo -= pad; hi += pad;
  const x = scale(0, Math.max(1, pts.length - 1), f.m.l, f.w - f.m.r);
  const y = scale(lo, hi, f.h - f.m.b, f.m.t);
  axes(svg, f, y, lo, hi, pts.map((p) => p.label), x, (v) => "$" + Math.round(v).toLocaleString());
  el("line", { x1: f.m.l, x2: f.w - f.m.r, y1: y(start), y2: y(start), stroke: cssVar("--muted"), "stroke-dasharray": "3 3" }, svg);
  const col = eq >= start ? cssVar("--gain") : cssVar("--loss");
  el("path", { d: path(pts.map((p, i) => [x(i), y(p.v)])), fill: "none", stroke: col, "stroke-width": 2 }, svg);
  pts.forEach((p, i) => i && el("circle", { cx: x(i), cy: y(p.v), r: 3, fill: col }, svg));
}

/* ---------------- events ---------------- */

function selectTab(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab").forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
  if (name === "charts" && state.result) { renderChart(); renderEquity(); }
}

function wire() {
  $("#run").addEventListener("click", run);
  $$("[data-run]").forEach((b) => b.addEventListener("click", run));
  $(".tabs").addEventListener("click", (e) => { const b = e.target.closest("button[data-tab]"); if (b) selectTab(b.dataset.tab); });
  $("#ticker-pills").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-ticker]"); if (!b) return;
    state.ticker = b.dataset.ticker;
    $$("#ticker-pills button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    renderChart();
  });
  $$("input[name=logfilter]").forEach((r) => r.addEventListener("change", renderLog));
  $("#log-ticker").addEventListener("change", renderLog);
  $$("input[name=source]").forEach((r) => r.addEventListener("change", () => {
    const csv = $("input[name=source]:checked").value === "csv";
    $("#csv-opts").hidden = !csv; $("#demo-opts").hidden = csv;
  }));
  $("#reseed").addEventListener("click", () => { $("#seed").value = Math.floor(Math.random() * 10000); run(); });
  $("#csv").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    state.csvText = null;
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { showError("CSV is larger than 8 MB."); return; }
    state.csvText = await file.text();
    $("#csv-name").textContent = file.name;
    $("#form-error").hidden = true;
  });
  $("#copy-prompt").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#prompt-text").textContent); $("#copy-prompt").textContent = "Copied"; }
    catch { $("#copy-prompt").textContent = "Select the text to copy"; }
    setTimeout(() => { $("#copy-prompt").textContent = "Copy prompt"; }, 1500);
  });
  $("#download").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(state.result, null, 2)], { type: "application/json" });
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "paper-session.json" });
    a.click(); URL.revokeObjectURL(a.href);
  });
  let rt;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => state.result && !$("#tab-charts").hidden && (renderChart(), renderEquity()), 150); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => state.result && (renderChart(), renderEquity()));
}

(async function init() {
  wire();
  try {
    fillForm(await api("/api/config"));
  } catch (e) {
    showError(`Could not reach the server: ${e.message}`);
  }
})();
