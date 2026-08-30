// OVW — Overview screen: KPI board, DIST outcome heatmap, top-5 opportunities, connections.
import { api } from "../api.js";
import { heatmap, sparkline, caption, crosshairReadout, cssVar } from "../charts.js";
import { money, pct } from "../fmt.js";

// spec §5.4 — 8 ROI buckets. Edges: [-inf,-25,0,25,50,100,200,400,inf]
const BUCKETS = [-Infinity, -25, 0, 25, 50, 100, 200, 400, Infinity];
const BUCKET_LABELS = ["<-25%", "-25–0%", "0–25%", "25–50%", "50–100%", "100–200%", "200–400%", "400%+"];

// Field names vary between the synthetic selftest input (roi_pct / collection) and the live
// /api/sim-population/best-simulations payload (expected_roi_pct / primary_collection). Read both.
const roiOf = (s) => s.roi_pct ?? s.roi ?? s.expected_roi ?? s.expected_roi_pct ?? s.effective_roi_pct;
const collOf = (s) => s.collection ?? s.primary_collection ?? s.input_mix?.[0]?.collection ?? "Unknown";

function bucketIndex(roi) {
  const v = Number(roi);
  if (!Number.isFinite(v)) return -1;
  for (let i = 0; i < 8; i++) if (v >= BUCKETS[i] && v < BUCKETS[i + 1]) return i;
  return -1;
}

// { rows: collections (top 8 by count), cols: 8 ROI buckets, values[r][c]: candidate count }.
// Exported for reuse by OPPS.
export function buildDistMatrix(bestSims) {
  const sims = Array.isArray(bestSims) ? bestSims : [];
  const cells = new Map();   // collection -> number[8]
  const totals = new Map();  // collection -> total count
  for (const s of sims) {
    const c = bucketIndex(roiOf(s));
    if (c < 0) continue;
    const k = collOf(s);
    if (!cells.has(k)) cells.set(k, new Array(8).fill(0));
    cells.get(k)[c] += 1;
    totals.set(k, (totals.get(k) || 0) + 1);
  }
  const rows = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([k]) => k);
  return { rows, cols: BUCKET_LABELS.slice(), values: rows.map((r) => cells.get(r)) };
}

// single-hue slate -> accent ramp for counts (spec §5.4). This is the sRGB lerp that
// `color-mix(in srgb, var(--accent) p%, var(--bg-panel))` resolves to, computed here because
// the canvas 2D context (heatmap's fillStyle / addColorStop) can't parse var() or color-mix().
const hexToRgb = (h) => {
  const s = (h || "").replace("#", "");
  const n = s.length === 3 ? s.replace(/./g, "$&$&") : s;
  return [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) || 0);
};
export function rampSlateToAccent(v, min, max) {
  const t = (v - min) / ((max - min) || 1);
  const p = Number.isFinite(t) ? Math.max(0, Math.min(1, t)) : 0;
  const a = hexToRgb(cssVar("--accent") || "#5C7CFF");
  const b = hexToRgb(cssVar("--bg-panel") || "#10151C");
  return `rgb(${a.map((av, i) => Math.round(av * p + b[i] * (1 - p))).join(", ")})`;
}

// ---- screen ----
let timer = null, onHash = null, sparks = [], hm = null, hmKey = "", lastKpi = {}, cache = {}, gen = 0;

function stop() {
  gen++;
  if (timer) { clearTimeout(timer); timer = null; }
  if (onHash) { removeEventListener("hashchange", onHash); onHash = null; }
  sparks.forEach((s) => s.destroy?.()); sparks = [];
  if (hm) { hm.destroy?.(); hm = null; }
  hmKey = "";
}

// one endpoint, swallow failure, fall back to the last good payload so a single dropped
// poll (the Flask dev server resets connections under load) doesn't blank a panel.
const pull = async (key, fn) => {
  try { const v = await fn(); if (v != null) cache[key] = v; return cache[key] ?? null; }
  catch { return cache[key] ?? null; }
};

const sum = (arr, f) => arr.reduce((a, s) => a + (Number(f(s)) || 0), 0);
const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
const maxFinite = (a) => { const f = (a || []).map(Number).filter(Number.isFinite); return f.length ? Math.max(...f) : null; };
const sgn = (v) => (!Number.isFinite(v) ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
const enc = encodeURIComponent;

export function renderOverview(root) {
  stop();
  lastKpi = {};
  root.innerHTML = `
    <div class="grid">
      <div class="panel col-12"><div class="panel-head">OVW · board</div><div class="panel-body" data-k="board"></div></div>
      <div class="panel col-8"><div class="panel-head">DIST · outcome distribution</div>
        <div class="panel-body" data-k="dist"><div class="chart-box"><canvas></canvas></div></div></div>
      <div class="panel col-4"><div class="panel-head">OPPS · top 5</div><div class="panel-body" data-k="top5"></div></div>
      <div class="panel col-12"><div class="panel-head">CONN · status</div><div class="panel-body" data-k="conn"></div></div>
    </div>`;
  const bodyOf = (k) => root.querySelector(`.panel-body[data-k="${k}"]`);

  // Serial fetch, re-armed 3 s AFTER each cycle finishes — cycles never overlap.
  // best-simulations is ~2 MB and often > 20 s to compute (past the preview proxy's request
  // timeout, so it resets); leaderboards is ~4 s. Both feed slow-moving aggregates (the DIST
  // heatmap and top-5), so refresh them only every 10th cycle — the cheap KPI / connection
  // sources keep polling at 3 s. Until the first best-simulations reply lands, DIST + top-5
  // show the cold empty-state; `pull` then serves the last good copy through later failures.
  // ponytail: fixed 10x throttle; revisit if the sim runs live and DIST needs sub-30 s data.
  const myGen = ++gen;
  const live = () => myGen === gen && root.isConnected;
  let n = 0;
  const paint = (stat, os, sim, conn) => {
    if (!live()) return;
    const sons = Array.isArray(sim) ? sim : [];
    const bestSims = Array.isArray(cache.best) ? cache.best : [];
    renderBoard(bodyOf("board"), { stat, os, sons, bestSims, lb: cache.lb });
    renderDist(bodyOf("dist"), bestSims);
    renderTop5(bodyOf("top5"), bestSims);
    renderConn(bodyOf("conn"), conn || {});
  };
  const tick = async () => {
    if (!live()) return;
    const heavy = n++ % 10 === 0;
    const stat = await pull("stat", api.status);
    const os = await pull("os", api.outcomesStats);
    const sim = await pull("sim", api.simStatus);
    const conn = await pull("conn", api.connections);
    paint(stat, os, sim, conn);                       // fast panels now; DIST/top-5 from cache
    if (heavy) {                                      // ~2 MB + >20 s: refresh on its own slow beat
      await pull("best", api.bestSimulations);
      await pull("lb", api.leaderboards);
      paint(stat, os, sim, conn);                     // repaint with fresh aggregates
    }
    if (live()) timer = setTimeout(tick, 3000);
  };

  cache = {};
  onHash = () => stop();
  addEventListener("hashchange", onHash, { once: true });
  tick();
}

function renderBoard(body, { stat, os, sons, bestSims, lb }) {
  if (!body) return;
  const S = (f) => sum(sons, f);
  const hist = sons.map((s) => s.history).filter(Array.isArray).sort((a, b) => b.length - a.length)[0] || [];
  const tail = (key) => hist.map((h) => h[key]).filter(Number.isFinite).slice(-30);
  const bestRois = bestSims.map(roiOf).map(Number).filter(Number.isFinite);
  const lbCols = lb?.collections || [];

  const nSim = S((s) => s.n_simulated);
  const nInvalid = S((s) => s.n_invalid);
  const conf = tail("avg_confidence");
  const learn = mean(
    sons.map((s) => s.sim_model_status).filter(Boolean).map((m) =>
      m.min_samples_required ? Math.min(1, (m.n_samples || 0) / m.min_samples_required) : (m.trained ? 1 : 0)));

  const bestLive = os?.best_roi_pct ?? maxFinite(lbCols.map((c) => c.best_roi_pct))
    ?? (bestRois.length ? Math.max(...bestRois) : null);
  const avgBest = mean(bestRois) ?? mean(lbCols.map((c) => c.avg_roi_pct).filter(Number.isFinite));
  const confNow = conf.length ? conf[conf.length - 1] : (os?.win_rate_pct ?? null);

  const kpis = [
    { label: "Best live ROI", value: pct(bestLive), cls: sgn(bestLive) },
    { label: "Avg ROI · best finds", value: pct(avgBest), cls: sgn(avgBest), spark: tail("avg_roi_pct") },
    { label: "Valid / invalid trials",
      value: nSim > 0 ? `${(nSim - nInvalid).toLocaleString("en-US")} / ${nInvalid.toLocaleString("en-US")}` : "—",
      cls: "" },
    { label: "Market confidence",
      value: Number.isFinite(confNow) ? `${confNow.toFixed(1)}%` : "—", cls: "", spark: conf },
    { label: "Learning progress",
      value: Number.isFinite(learn) ? `${Math.round(learn * 100)}%` : "—", cls: "" },
    { label: "Trade-ups analyzed",
      value: (nSim || stat?.tradeup_count || 0).toLocaleString("en-US"), cls: "", spark: tail("n_simulated") },
  ];

  sparks.forEach((s) => s.destroy?.()); sparks = [];
  body.innerHTML = `<div class="kpi-row">${kpis.map((k, i) => {
    const flash = lastKpi[k.label] !== undefined && lastKpi[k.label] !== k.value ? " flash" : "";
    lastKpi[k.label] = k.value;
    return `<div class="kpi">
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value ${k.cls}${flash}">${k.value}</div>
      ${k.spark && k.spark.length >= 2 ? `<div class="kpi-spark" data-i="${i}"></div>` : ""}
    </div>`;
  }).join("")}</div>`;

  body.querySelectorAll(".kpi-spark[data-i]").forEach((el) => {
    const s = kpis[+el.dataset.i].spark;
    if (s && s.length >= 2) sparks.push(sparkline(el, s));
  });
}

function renderDist(body, bestSims) {
  if (!body) return;
  const m = buildDistMatrix(bestSims);
  const key = JSON.stringify(m);
  if (key === hmKey) return;                          // static data — keep the live heatmap + its hover
  hmKey = key;
  if (hm) { hm.destroy?.(); hm = null; }

  const box = body.querySelector(".chart-box");
  const canvas = box.querySelector("canvas");
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);

  if (!m.rows.length) { caption(body, "No candidates yet — the sim population is cold."); return; }

  canvas.width = box.clientWidth || 600;
  canvas.height = box.clientHeight || 260;
  hm = heatmap(canvas, {
    rows: m.rows, cols: m.cols, values: m.values,
    colorFor: rampSlateToAccent,
    fmt: String,
    onHover: (r, c) => crosshairReadout(body, [
      { label: "Collection", value: m.rows[r] },
      { label: "ROI bucket", value: m.cols[c] },
      { label: "Candidates", value: String(m.values[r][c]) },
    ]),
    onClick: (r, c) => { location.hash = "#opportunities?collection=" + enc(m.rows[r]); },
  });
  caption(body, "How many trade-ups the agents have found in each collection, by how good they are. Bright = crowded. Click a cell to open those in OPPS.");
}

function renderTop5(body, bestSims) {
  if (!body) return;
  const scoreOf = (s) => Number(s.composite_score ?? s.score ?? roiOf(s) ?? 0);
  const top = [...bestSims].sort((a, b) => scoreOf(b) - scoreOf(a)).slice(0, 5);
  const rows = top.map((s) => {
    const roi = Number(roiOf(s));
    return `<tr data-rarity="${s.to_rarity || ""}" style="cursor:pointer">
      <td>${collOf(s)}</td>
      <td class="num ${sgn(roi)}">${pct(roi)}</td>
      <td class="num">${money(s.cost)}</td>
    </tr>`;
  }).join("");
  body.innerHTML = `<table class="tbl">
    <thead><tr><th>Collection</th><th class="num">ROI%</th><th class="num">Cost</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="3">No candidates yet — sim population is cold.</td></tr>`}</tbody>
  </table>`;
  body.querySelectorAll("tbody tr[data-rarity]").forEach((tr) =>
    tr.addEventListener("click", () => { location.hash = "#opportunities"; }));
}

function renderConn(body, c) {
  if (!body) return;
  const chip = (label, ok) =>
    `<span class="conn-chip" style="cursor:pointer;margin-right:16px;display:inline-block">
       <span class="dot ${ok ? "ok" : ""}"></span>${label}: ${ok ? "connected" : "not configured"}</span>`;
  body.innerHTML =
    chip("CSFloat", !!c.csfloat?.configured) +
    chip("GitHub backup", !!c.github_backup?.configured) +
    chip("NTFY", !!(c.ntfy?.configured ?? c.ntfy?.enabled));
  body.querySelectorAll(".conn-chip").forEach((el) =>
    el.addEventListener("click", () => { location.hash = "#connections"; }));
}
