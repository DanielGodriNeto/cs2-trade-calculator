// OPPS — Opportunities screen (spec §6.2). Panels:
//   1. DIST  · outcome distribution   — full-width heatmap from buildDistMatrix(bestSims);
//                                        click a cell → sets the table collection filter.
//   2. OPPS  · trade-ups              — sticky filter row + ranked table (▸ / Cost / ROI% /
//                                        TRUST 4-segment micro-bar). Row ▸ expands a detail
//                                        row: per-input price cells, the FLOAT instrument for
//                                        the output, formula / ML / verified ROI side by side,
//                                        and a "mark done / blacklist" POST.
//   3. LB    · profitable collections / profitable skins — two tables from api.leaderboards().
//   4. ARCH  · full archive          — paginated table from api.archive(), fetched only when
//                                        the panel is expanded.
//   5. COLL  · collector arbitrage   — table from api.patternsOpps().
//
// M1 (Task 7 review carryover, MANDATORY here): every string that comes from an API response
// — collection / skin / agent / pattern / item names and the COLL listing link — reaches the
// DOM via textContent or a createElement path, NEVER interpolated into an innerHTML template.
// The one <a> is given a .href only after the URL passes /^https?:\/\//i. Formatted numbers
// through fmt.js are safe, but cells are built the same createElement way throughout.

import { api } from "../api.js";
import { heatmap, caption, crosshairReadout } from "../charts.js";
import { money, pct, WEAR_BANDS } from "../fmt.js";
import { buildDistMatrix, rampSlateToAccent } from "./overview.js";
import { mountFloatInstrument } from "./float-instrument.js";

// ---------------------------------------------------------------------------
// trustSegments — pure, exported for the selftest.
//   composite   — composite_score
//   confidence  — confidence (selftest) / confidence_score (live)
//   dead_skin   — inverted: 1 - score/100
//   price_basis — cache = 1, stale (or any other basis) = 0.4, absent = 0
// Live rows carry composite_score / confidence_score on a 0..100 scale; the selftest passes a
// 0..1 composite. Anything > 1 is treated as a percentage.
// ---------------------------------------------------------------------------
const clamp01 = (n) => (Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0);
const norm01 = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return clamp01(n > 1 ? n / 100 : n);
};
function worstDeadSkin(row) {
  const units = [...(row.input_mix || []), ...(row.output_distribution || [])];
  return units.reduce((m, u) => Math.max(m, (u && Number(u.dead_skin_score)) || 0), 0);
}
export function trustSegments(row = {}) {
  const r = row || {};
  const ds = r.dead_skin_score != null ? Number(r.dead_skin_score) : worstDeadSkin(r);
  const pb = String(r.price_basis ?? r.cost_basis_source ?? "").toLowerCase();
  return [
    { key: "composite", v01: norm01(r.composite_score ?? r.composite) },
    { key: "confidence", v01: norm01(r.confidence ?? r.confidence_score) },
    { key: "dead_skin", v01: clamp01(1 - (Number(ds) || 0) / 100) },
    { key: "price_basis", v01: pb === "cache" ? 1 : pb ? 0.4 : 0 },
  ];
}

// ---------------------------------------------------------------------------
// DOM helpers — textContent / appendChild only (M1).
// ---------------------------------------------------------------------------
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text);
  return n;
};
const td = (content, cls) => {
  const n = document.createElement("td");
  if (cls) n.className = cls;
  if (content instanceof Node) n.appendChild(content);
  else n.textContent = content == null ? "" : String(content);
  return n;
};
const rowEl = (cells, opts = {}) => {
  const n = document.createElement("tr");
  if (opts.rarity != null && opts.rarity !== "") n.dataset.rarity = opts.rarity; // safe form (supplement)
  for (const c of cells) n.appendChild(c instanceof HTMLTableCellElement ? c : td(c));
  return n;
};
const mkOption = (value, label) => {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label; // agent names are API strings — never innerHTML
  return o;
};
const sgn = (v) => {
  const n = Number(v);
  return !Number.isFinite(n) ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "";
};
const roiOf = (r) => (r.effective_roi_pct != null ? r.effective_roi_pct : r.expected_roi_pct);
const riskLevel = (c) => (c == null ? "medium" : c >= 70 ? "low" : c >= 40 ? "medium" : "high");

function tableEl(headers) {
  const t = el("table", "tbl");
  const thead = el("thead");
  const htr = el("tr");
  for (const h of headers) htr.appendChild(el("th", h && h.num ? "num" : null, h && h.label != null ? h.label : h));
  thead.appendChild(htr);
  t.appendChild(thead);
  const tbody = el("tbody");
  t.appendChild(tbody);
  return { t, tbody };
}
function emptyRow(tbody, span, text) {
  const c = td(text);
  c.colSpan = span;
  c.style.color = "var(--text-faint)";
  const tr = el("tr");
  tr.appendChild(c);
  tbody.appendChild(tr);
}

// TRUST cell — 4-segment micro-bar. spec §6.2: <span class="tbar"><i style="flex:${v01}">…
function trustBar(row) {
  const span = el("span", "tbar");
  const segs = trustSegments(row);
  span.title = segs.map((s) => `${s.key} ${Math.round(s.v01 * 100)}%`).join("  ·  ");
  for (const s of segs) {
    const i = document.createElement("i");
    i.style.flex = "0 0 " + (s.v01 * 25) + "%";   // each seg 0..25% of the 56px bar; empty space = untrusted
    span.appendChild(i);
  }
  return span;
}

// ---------------------------------------------------------------------------
// FLOAT-instrument inputs. The brief names row.output_prices_by_wear /
// row.output_float_cap / row.avg_input_float; the live /best-simulations row has none of them,
// so derive what we can from output_distribution + input_mix and degrade gracefully.
// ---------------------------------------------------------------------------
const shortOfWearName = (name) => {
  const b = WEAR_BANDS.find((x) => x.name === name);
  return b ? b.short : null;
};
// { FN|MW|FT|WW|BS: probability-weighted mean output price at that wear }
function outputPricesByWear(dist = []) {
  const acc = {};
  for (const o of dist) {
    const short = shortOfWearName(o && o.wear);
    const price = Number(o && o.price);
    if (!short || !Number.isFinite(price)) continue;
    const w = Number(o && o.prob_pct) || 1;
    const a = acc[short] || (acc[short] = { w: 0, p: 0 });
    a.w += w;
    a.p += price * w;
  }
  const out = {};
  for (const [k, a] of Object.entries(acc)) if (a.w) out[k] = a.p / a.w;
  return out;
}
// count-weighted average of the input wear-bracket midpoints
function avgInputFloat(mix = []) {
  let n = 0;
  let s = 0;
  for (const m of mix) {
    const b = WEAR_BANDS.find((x) => x.name === (m && m.wear));
    const c = Number(m && m.count) || 0;
    if (!b || !c) continue;
    s += ((b.lo + b.hi) / 2) * c;
    n += c;
  }
  return n ? s / n : null;
}

// ---------------------------------------------------------------------------
// screen
// ---------------------------------------------------------------------------
let gen = 0;
let hm = null;
let onHash = null;
let S = null;
const details = new Map(); // row.id -> { tr, inst }

function teardownDetails() {
  for (const { tr, inst } of details.values()) {
    try { inst && inst.destroy && inst.destroy(); } catch { /* already gone */ }
    tr.remove();
  }
  details.clear();
}
function stop() {
  gen++;
  teardownDetails();
  if (hm) { try { hm.destroy(); } catch { /* noop */ } hm = null; }
  if (onHash) { removeEventListener("hashchange", onHash); onHash = null; }
  S = null;
}

export function renderOpportunities(root, params) {
  stop();
  const myGen = ++gen;
  const live = () => myGen === gen && root.isConnected;

  root.innerHTML = `
    <div class="grid">
      <div class="panel col-12"><div class="panel-head">DIST · outcome distribution</div>
        <div class="panel-body" data-k="dist"><div class="chart-box"><canvas></canvas></div></div></div>
      <div class="panel col-8"><div class="panel-head">OPPS · trade-ups <span data-k="opps-count" style="color:var(--text-faint)"></span></div>
        <div class="panel-body" data-k="opps"></div></div>
      <div class="panel col-4"><div class="panel-head">LB · profitable collections</div>
        <div class="panel-body" data-k="lb-coll"></div></div>
      <div class="panel col-4"><div class="panel-head">LB · profitable skins</div>
        <div class="panel-body" data-k="lb-skin"></div></div>
      <div class="panel col-12"><div class="panel-head">ARCH · full archive
        <button class="secondary" data-k="arch-toggle">expand</button></div>
        <div class="panel-body" data-k="arch"><div style="color:var(--text-faint)">Collapsed — expand to browse every simulated composition.</div></div></div>
      <div class="panel col-12"><div class="panel-head">COLL · collector arbitrage</div>
        <div class="panel-body" data-k="coll"></div></div>
    </div>`;

  const bodyOf = (k) => root.querySelector(`.panel-body[data-k="${k}"]`);

  S = {
    root,
    sims: [],
    simsLoaded: false,
    agentNames: {},
    filters: { minRoi: "", risk: "", agent: "", collection: (params && params.get("collection")) || "" },
    els: {},
  };

  // ----- OPPS: sticky filter bar + table shell -----
  const opps = bodyOf("opps");
  const bar = el("div", "opps-filters");
  bar.style.cssText =
    "position:sticky;top:0;background:var(--bg-panel);display:flex;gap:8px;flex-wrap:wrap;padding-bottom:8px;margin-bottom:2px;z-index:1";
  const minRoi = el("input");
  minRoi.type = "number";
  minRoi.placeholder = "min ROI %";
  minRoi.value = S.filters.minRoi;
  minRoi.style.width = "96px";
  const collIn = el("input");
  collIn.type = "text";
  collIn.placeholder = "collection";
  collIn.value = S.filters.collection;
  collIn.style.width = "150px";
  const risk = el("select");
  for (const [v, l] of [["", "risk: any"], ["low", "low"], ["medium", "med"], ["high", "high"]]) risk.appendChild(mkOption(v, l));
  const agent = el("select");
  agent.appendChild(mkOption("", "agent: any"));
  bar.append(minRoi, collIn, risk, agent);
  const tableWrap = el("div", "opps-table");
  tableWrap.textContent = "loading…";
  opps.append(bar, tableWrap);

  S.els = { minRoi, collIn, risk, agent, tableWrap, count: root.querySelector('[data-k="opps-count"]'), distBody: bodyOf("dist") };

  const applyFilters = () => {
    S.filters = { minRoi: minRoi.value.trim(), risk: risk.value, agent: agent.value, collection: collIn.value.trim() };
    buildOppsTable();
  };
  S.applyFilters = applyFilters;
  for (const inp of [minRoi, collIn, risk, agent]) {
    inp.addEventListener("change", applyFilters);
    inp.addEventListener("input", applyFilters);
  }

  // ----- ARCH: expand / collapse (fetches only when open) -----
  const archBtn = root.querySelector('[data-k="arch-toggle"]');
  let archOpen = false;
  archBtn.addEventListener("click", () => {
    archOpen = !archOpen;
    archBtn.textContent = archOpen ? "collapse" : "expand";
    if (archOpen) loadArchive(1);
    else bodyOf("arch").innerHTML =
      '<div style="color:var(--text-faint)">Collapsed — expand to browse every simulated composition.</div>';
  });

  // ----- fetch (single shot; no poll on this screen). Each panel renders as its own
  // endpoint lands — best-simulations is ~2 MB and can reset on a cold load, so it must not
  // block the leaderboard / arbitrage / agent-list panels. -----
  api.constants()
    .then((c) => {
      if (!live()) return;
      S.agentNames = c && c.agent_names ? c.agent_names : {};
      rebuildAgentOptions();
    })
    .catch(() => {});

  // best-simulations is ~2 MB and the dev server routinely resets it on a cold call; one
  // automatic retry catches that without the operator having to hit reload.
  const loadSims = (retry = true) =>
    api
      .bestSimulations()
      .then((sims) => {
        if (!live()) return;
        S.sims = Array.isArray(sims) ? sims : [];
        S.simsLoaded = true;
        rebuildAgentOptions();
        renderDist();
        buildOppsTable();
      })
      .catch(() => {
        if (!live()) return;
        if (retry) return loadSims(false);
        S.simsLoaded = true;
        renderDist();
        buildOppsTable();
      });
  loadSims();

  api.leaderboards()
    .then((lb) => {
      if (!live()) return;
      renderLbCollections(lb);
      renderLbSkins(lb);
    })
    .catch(() => {
      if (!live()) return;
      renderLbCollections(null);
      renderLbSkins(null);
    });

  api.patternsOpps()
    .then((coll) => live() && renderColl(coll))
    .catch(() => live() && renderColl(null));

  onHash = () => stop();
  addEventListener("hashchange", onHash, { once: true });
}

// ---------------------------------------------------------------------------
// DIST heatmap
// ---------------------------------------------------------------------------
function renderDist() {
  const body = S.els.distBody;
  const m = buildDistMatrix(S.sims);
  if (hm) { try { hm.destroy(); } catch { /* noop */ } hm = null; }

  const box = body.querySelector(".chart-box");
  const canvas = box.querySelector("canvas");
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);

  if (!m.rows.length) {
    caption(body, "No candidates yet — the sim population is cold, or /best-simulations was slow. Reload.");
    return;
  }
  canvas.width = box.clientWidth || 600;
  canvas.height = box.clientHeight || 260;
  hm = heatmap(canvas, {
    rows: m.rows,
    cols: m.cols,
    values: m.values,
    colorFor: rampSlateToAccent,
    fmt: String,
    onHover: (r, c) =>
      crosshairReadout(body, [
        { label: "Collection", value: m.rows[r] },
        { label: "ROI bucket", value: m.cols[c] },
        { label: "Candidates", value: String(m.values[r][c]) },
      ]),
    onClick: (r) => {
      S.els.collIn.value = m.rows[r];
      S.applyFilters();
    },
  });
  caption(
    body,
    "How many trade-ups the agents have found in each collection, by how good they are. Bright = crowded. Click a cell to filter the table.",
  );
}

// Agent <select> — rebuilt whenever constants or best-simulations lands (either order).
// Options: every son id present in the loaded rows, labelled from constants.agent_names.
function rebuildAgentOptions() {
  const sel = S.els.agent;
  if (!sel) return;
  const keep = sel.value;
  sel.textContent = "";
  sel.appendChild(mkOption("", "agent: any"));
  const ids = [...new Set(S.sims.map((r) => r.son ?? r.agent).filter((v) => v != null))].sort((a, b) => a - b);
  for (const id of ids) sel.appendChild(mkOption(String(id), S.agentNames[String(id)] || `Agent ${id}`));
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

// ---------------------------------------------------------------------------
// OPPS ranked table + row expansion
// ---------------------------------------------------------------------------
function matchFilters(r) {
  const f = S.filters;
  if (f.minRoi !== "" && !Number.isNaN(Number(f.minRoi)) && Number(r.expected_roi_pct) < Number(f.minRoi)) return false;
  if (f.risk && riskLevel(r.confidence_score) !== f.risk) return false;
  if (f.agent !== "" && String(r.son ?? r.agent) !== String(f.agent)) return false;
  if (f.collection && !String(r.primary_collection || "").toLowerCase().includes(f.collection.toLowerCase())) return false;
  return true;
}

function buildOppsTable() {
  teardownDetails();
  const wrap = S.els.tableWrap;
  wrap.textContent = "";
  const rows = S.sims
    .filter(matchFilters)
    .sort((a, b) => (Number(b.composite_score) || 0) - (Number(a.composite_score) || 0));

  const { t, tbody } = tableEl(["▸", { label: "Cost", num: true }, { label: "ROI%", num: true }, "TRUST"]);
  if (!S.simsLoaded) emptyRow(tbody, 4, "loading /best-simulations (~2 MB)…");
  else if (!S.sims.length) emptyRow(tbody, 4, "No candidates — the sim population is cold, or /best-simulations reset on a cold load. Reload.");
  else if (!rows.length) emptyRow(tbody, 4, "No rows match the current filters.");
  else
    for (const r of rows) {
      const roi = Number(roiOf(r));
      const tr = rowEl(
        [td("▸", "toggle"), td(money(r.cost), "num"), td(pct(roi), "num " + sgn(roi)), td(trustBar(r))],
        { rarity: r.to_rarity },
      );
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => toggleDetail(tr, r));
      tbody.appendChild(tr);
    }
  wrap.appendChild(t);
  if (S.els.count) S.els.count.textContent = S.sims.length ? `(${rows.length} of ${S.sims.length})` : "";
}

function toggleDetail(tr, row) {
  const key = row.id || tr;
  const open = details.get(key);
  if (open) {
    try { open.inst && open.inst.destroy && open.inst.destroy(); } catch { /* noop */ }
    open.tr.remove();
    details.delete(key);
    if (tr.firstElementChild) tr.firstElementChild.textContent = "▸";
    return;
  }

  const dtr = el("tr", "opps-detail");
  const cell = document.createElement("td");
  cell.colSpan = 4;
  dtr.appendChild(cell);
  tr.after(dtr); // now in the live DOM & visible → real width for the FLOAT instrument
  if (tr.firstElementChild) tr.firstElementChild.textContent = "▾";

  buildDetail(cell, row);

  // Mount the FLOAT instrument only now that .opps-float has a real width (supplement: it
  // sizes once from clientWidth||600 and has no setSize). destroy() on collapse / re-render.
  let inst = null;
  const floatEl = cell.querySelector(".opps-float");
  const pbw = outputPricesByWear(row.output_distribution);
  if (floatEl && Object.keys(pbw).length) {
    try {
      inst = mountFloatInstrument(floatEl, {
        pricesByWear: pbw,
        floatCap: row.output_float_cap || {},
        marker: row.avg_input_float != null ? row.avg_input_float : avgInputFloat(row.input_mix),
        caption: "Output value by landed wear, averaged from this candidate's outcome distribution — not a per-listing float sweep.",
      });
    } catch {
      floatEl.textContent = "float instrument unavailable";
    }
  } else if (floatEl) {
    floatEl.textContent = "— no per-wear output prices on this candidate";
  }
  details.set(key, { tr: dtr, inst });
}

function buildDetail(cell, row) {
  const box = el("div", "opps-detail-body");
  box.style.cssText = "display:grid;gap:12px;padding:6px 2px";

  // per-input price cells
  const { t: it, tbody: itb } = tableEl([
    "Skin",
    "Wear",
    { label: "×", num: true },
    { label: "Unit", num: true },
    { label: "Line", num: true },
  ]);
  const mix = Array.isArray(row.input_mix) ? row.input_mix : [];
  if (!mix.length) emptyRow(itb, 5, "no input breakdown");
  for (const m of mix) {
    const c = Number(m.count) || 0;
    const u = Number(m.unit_price);
    itb.appendChild(
      rowEl([
        td(m.skin || "—"), // API string → textContent
        td(m.wear || "—"), // API string → textContent
        td("×" + c, "num"),
        td(Number.isFinite(u) ? money(u) : "—", "num"),
        td(Number.isFinite(u) ? money(u * c) : "—", "num"),
      ]),
    );
  }
  box.appendChild(it);

  // three ROI figures side by side — formula / ML / verified
  const kr = el("div", "kpi-row");
  const fig = (label, val, cls) => {
    const k = el("div", "kpi");
    k.appendChild(el("div", "kpi-label", label));
    k.appendChild(el("div", "kpi-value " + (cls || ""), val));
    return k;
  };
  const fRoi = row.roi_pct != null ? row.roi_pct : row.expected_roi_pct; // brief: row.roi_pct; live: expected_roi_pct
  const mlRoi = row.ml_predicted_roi_pct;
  const vRoi = row.verified_roi_pct;
  kr.appendChild(fig("formula ROI", pct(fRoi), sgn(fRoi)));
  kr.appendChild(fig("ML ROI", mlRoi != null ? pct(mlRoi) : "—", mlRoi != null ? sgn(mlRoi) : ""));
  kr.appendChild(fig("verified ROI", vRoi != null ? pct(vRoi) : "—", vRoi != null ? sgn(vRoi) : ""));
  box.appendChild(kr);

  // FLOAT instrument mount point (filled by toggleDetail once this row is on screen)
  box.appendChild(el("div", "opps-float"));

  // mark done / blacklist
  const btn = el("button", "secondary", "mark done / blacklist");
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    blacklist(row, btn);
  });
  box.appendChild(btn);

  cell.appendChild(box);
}

// POST /api/sim-population/blacklist — legacy payload shape (web/static/app.js:2667).
async function blacklist(row, btn) {
  btn.disabled = true;
  try {
    const res = await fetch("/api/sim-population/blacklist", {
      method: "POST",
      body: JSON.stringify({
        primary_collection: row.primary_collection,
        from_rarity: row.from_rarity,
        to_rarity: row.to_rarity,
        stattrak: row.stattrak,
        expected_roi_pct: row.expected_roi_pct,
        input_mix: row.input_mix,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (data && data.ok) {
      // re-fetch best-simulations so the blacklisted row drops out, then rebuild the table
      const fresh = await api.bestSimulations().catch(() => null);
      if (Array.isArray(fresh)) S.sims = fresh;
      buildOppsTable();
    } else {
      btn.disabled = false;
      btn.textContent = "blacklist failed — retry";
    }
  } catch {
    btn.disabled = false;
    btn.textContent = "blacklist failed — retry";
  }
}

// ---------------------------------------------------------------------------
// LB — leaderboards
// ---------------------------------------------------------------------------
function renderLbCollections(lb) {
  const body = S.root.querySelector('.panel-body[data-k="lb-coll"]');
  body.textContent = "";
  const list = lb && Array.isArray(lb.collections) ? lb.collections : [];
  const { t, tbody } = tableEl([
    "Collection",
    { label: "n", num: true },
    { label: "Avg", num: true },
    { label: "Best", num: true },
    { label: "Σ profit", num: true },
  ]);
  if (!list.length) emptyRow(tbody, 5, "no leaderboard yet — sim population is cold.");
  for (const c of list) {
    const tr = rowEl([
      td(c.collection || "—"), // API string
      td(String(c.count ?? "—"), "num"),
      td(pct(c.avg_roi_pct), "num " + sgn(c.avg_roi_pct)),
      td(pct(c.best_roi_pct), "num " + sgn(c.best_roi_pct)),
      td(money(c.total_expected_profit), "num " + sgn(c.total_expected_profit)),
    ]);
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => {
      S.els.collIn.value = c.collection || "";
      S.applyFilters();
    });
    tbody.appendChild(tr);
  }
  body.appendChild(t);
}

function renderLbSkins(lb) {
  const body = S.root.querySelector('.panel-body[data-k="lb-skin"]');
  body.textContent = "";
  const list = lb && Array.isArray(lb.skins) ? lb.skins : [];
  const { t, tbody } = tableEl(["Skin", "Wear", { label: "Seen", num: true }, { label: "Wtd profit", num: true }]);
  if (!list.length) emptyRow(tbody, 4, "no leaderboard yet — sim population is cold.");
  for (const s of list) {
    tbody.appendChild(
      rowEl([
        td(s.skin || "—"), // API string
        td(s.wear || "—"), // API string
        td(String(s.appearances ?? "—"), "num"),
        td(money(s.weighted_profit), "num " + sgn(s.weighted_profit)),
      ]),
    );
  }
  body.appendChild(t);
}

// ---------------------------------------------------------------------------
// COLL — collector arbitrage
// ---------------------------------------------------------------------------
function renderColl(payload) {
  const body = S.root.querySelector('.panel-body[data-k="coll"]');
  body.textContent = "";
  const rows = Array.isArray(payload) ? payload : payload && Array.isArray(payload.items) ? payload.items : [];
  const { t, tbody } = tableEl([
    "Item",
    "Pattern",
    { label: "Ask", num: true },
    { label: "Est value", num: true },
    { label: "Est profit", num: true },
    "Confidence",
    "Listing",
  ]);
  if (!rows.length) {
    emptyRow(tbody, 7, "no collector-arbitrage opportunities right now.");
    body.appendChild(t);
    return;
  }
  for (const o of rows) {
    const profit = o.expected_profit;
    const linkCell = td("");
    const url = String(o.listing_url || "");
    if (/^https?:\/\//i.test(url)) {
      const a = document.createElement("a");
      a.href = url; // property assignment, only after http/https validation (M1)
      a.textContent = "open";
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      linkCell.appendChild(a);
    } else {
      linkCell.textContent = "—";
    }
    tbody.appendChild(
      rowEl([
        td(o.market_hash_name || o.skin || "—"), // API string
        td(o.pattern_label || o.pattern_type || "—"), // API string
        td(money(o.asking_price), "num"),
        td(money(o.estimated_collector_value), "num"),
        td(money(profit), "num " + sgn(profit)),
        td(o.confidence || "—"), // API string
        linkCell,
      ]),
    );
  }
  body.appendChild(t);
}

// ---------------------------------------------------------------------------
// ARCH — paginated archive (on demand only). Endpoint reads ?son=, not ?agent=.
// ---------------------------------------------------------------------------
async function loadArchive(page) {
  const body = S.root.querySelector('.panel-body[data-k="arch"]');
  body.textContent = "loading…";
  const f = S.filters;
  const q = { page, page_size: 50 };
  if (f.collection) q.collection = f.collection;
  if (f.minRoi !== "" && !Number.isNaN(Number(f.minRoi))) q.min_roi = f.minRoi;
  if (f.agent !== "") q.son = f.agent;

  let data;
  try {
    data = await api.archive(q);
  } catch {
    body.textContent = "";
    body.appendChild(el("div", null, "archive fetch failed — collapse and expand to retry."));
    return;
  }
  const items = data && Array.isArray(data.items) ? data.items : [];
  const nPages = data && data.n_pages ? data.n_pages : 1;
  const cur = data && data.page ? data.page : page;

  body.textContent = "";
  const nav = el("div");
  nav.style.cssText = "display:flex;gap:8px;align-items:center;padding-bottom:8px";
  const prev = el("button", "secondary", "‹ prev");
  const next = el("button", "secondary", "next ›");
  prev.disabled = cur <= 1;
  next.disabled = cur >= nPages;
  prev.addEventListener("click", () => loadArchive(cur - 1));
  next.addEventListener("click", () => loadArchive(cur + 1));
  nav.append(
    prev,
    next,
    el("span", null, `page ${cur} / ${nPages}  ·  ${data && data.total != null ? data.total : items.length} unique`),
  );
  body.appendChild(nav);

  const { t, tbody } = tableEl(["Collection", { label: "Cost", num: true }, { label: "ROI%", num: true }, "TRUST"]);
  if (!items.length) emptyRow(tbody, 4, "no rows for these filters.");
  for (const r of items) {
    const roi = Number(roiOf(r));
    tbody.appendChild(
      rowEl([td(r.primary_collection || "—"), td(money(r.cost), "num"), td(pct(roi), "num " + sgn(roi)), td(trustBar(r))], {
        rarity: r.to_rarity,
      }),
    );
  }
  body.appendChild(t);
}
