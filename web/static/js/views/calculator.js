// CALC — Calculator screen (spec §6.3). Panels:
//   1. CALC · scan settings (col-6)   — a 2-col form of the 15 legacy #config-form fields;
//      submit → api.setConfig(body) then api.scanNow().
//   2. SCAN · progress (col-6)        — api.status() on a 3 s serial poll: a .tbar-style bar +
//      status / phase / done·total / items·s / ETA, plus Scan / Pause / Resume / Stop buttons.
//   3. BUILD · manual trade-up (col-12) — skin autocomplete (api.skins), wear select, count;
//      an #contract-strip of 10 cells that fill as inputs are added; the FLOAT instrument
//      (marker = current average input float); a landing-odds 100%-stacked bar from
//      landingOdds(); Compute (enabled at 10/10) → api.manualTradeup → a .kpi-row result:
//      Cost · EV · After fee · Profit · ROI%.
//   4. OPPS / COV / ANOM tables (col-12 each) — api.tradeups() / api.covert() / api.anomalies(),
//      same columns as the legacy dashboard, restyled to table.tbl.
//
// M1 (Task 7 & 9 review carryover): every string from an API response (skin names in the
// autocomplete, table cells, compute error text) reaches the DOM via textContent /
// createElement — never interpolated into an innerHTML template. Numbers through fmt.js are safe.

import { api } from "../api.js";
import { money, pct, float as fmtFloat, WEAR_BANDS } from "../fmt.js";
import { crosshairReadout } from "../charts.js";
import { mountFloatInstrument } from "./float-instrument.js";

const WEAR_NAMES = WEAR_BANDS.map((b) => b.name);
const midpointOfWearName = (name) => {
  const b = WEAR_BANDS.find((w) => w.name === name);
  return b ? (b.lo + b.hi) / 2 : null;
};
const sgn = (v) => {
  const n = Number(v);
  return !Number.isFinite(n) ? "" : n > 0 ? "pos" : n < 0 ? "neg" : "";
};
// textContent-only element factory (M1).
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text);
  return n;
};

// ── landingOdds — PURE, exported for the selftest ──────────────────────────────────────────
// Average the input floats, map that average through the output float-cap {min,max} to an
// output float, bucket it to a wear band using WEAR_BANDS. Deterministic: a single 1 in the
// landed bucket, 0 elsewhere. The 5-key shape is kept so a future probability distribution
// can fill it (matches oddsmath.wear_probabilities_for_output's `lo + avg*(hi-lo)` mapping).
export function landingOdds(inputFloats, outputCap) {
  const xs = (inputFloats || []).map(Number).filter(Number.isFinite);
  const avg = xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
  const lo = Number.isFinite(outputCap && outputCap.min) ? outputCap.min : 0;
  const hi = Number.isFinite(outputCap && outputCap.max) ? outputCap.max : 1;
  const f = lo + Math.max(0, Math.min(1, avg)) * (hi - lo);
  const band = WEAR_BANDS.find((w) => f >= w.lo && f < w.hi) || WEAR_BANDS[WEAR_BANDS.length - 1];
  const out = { FN: 0, MW: 0, FT: 0, WW: 0, BS: 0 };
  out[band.short] = 1;
  return out;
}

// ── screen lifecycle (Task 7 OVW standard: gen bump + {once} hashchange + isConnected) ─────
let gen = 0;
let pollTimer = null;
let onHash = null;
let floatInst = null;

function stop() {
  gen++;
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  if (onHash) { removeEventListener("hashchange", onHash); onHash = null; }
  if (floatInst) { try { floatInst.destroy(); } catch { /* already gone */ } floatInst = null; }
}

export function renderCalculator(root) {
  stop();
  const myGen = ++gen;
  const live = () => myGen === gen && root.isConnected;

  root.innerHTML = `
    <div class="grid">
      <div class="panel col-6"><div class="panel-head">CALC · scan settings</div>
        <div class="panel-body" data-k="cfg"></div></div>
      <div class="panel col-6"><div class="panel-head">SCAN · progress</div>
        <div class="panel-body" data-k="scan"></div></div>
      <div class="panel col-12"><div class="panel-head">BUILD · manual trade-up
        <span data-k="mb-count" style="color:var(--text-faint)"></span></div>
        <div class="panel-body" data-k="build"></div></div>
      <div class="panel col-12"><div class="panel-head">OPPS · trade-ups
        <span data-k="tu-count" style="color:var(--text-faint)"></span></div>
        <div class="panel-body" data-k="tradeups">loading…</div></div>
      <div class="panel col-12"><div class="panel-head">COV · covert → knife/glove
        <span data-k="cov-count" style="color:var(--text-faint)"></span></div>
        <div class="panel-body" data-k="covert">loading…</div></div>
      <div class="panel col-12"><div class="panel-head">ANOM · underpriced listings
        <span data-k="an-count" style="color:var(--text-faint)"></span></div>
        <div class="panel-body" data-k="anom">loading…</div></div>
    </div>`;
  const bodyOf = (k) => root.querySelector(`.panel-body[data-k="${k}"]`);

  const curP = api.currencies().catch(() => []);

  buildScanForm(bodyOf("cfg"), curP, live);
  buildScanProgress(bodyOf("scan"), live);
  buildManualBuilder(root, bodyOf("build"), curP, live);

  loadTable(bodyOf("tradeups"), api.tradeups, TRADEUP_COLS, "no trade-ups — run a scan.", live,
    root.querySelector('[data-k="tu-count"]'));
  loadTable(bodyOf("covert"), api.covert, COVERT_COLS,
    "no covert trade-ups — enable “Scan Covert” and rescan.", live, root.querySelector('[data-k="cov-count"]'));
  loadTable(bodyOf("anom"), api.anomalies, ANOM_COLS, "no underpriced listings.", live,
    root.querySelector('[data-k="an-count"]'));

  onHash = () => stop();
  addEventListener("hashchange", onHash, { once: true });
}

// ── panel 1 · scan settings ───────────────────────────────────────────────────────────────
// Each entry maps a legacy #config-form field to the key POST /api/config accepts
// (web/server.py CONFIG_KEYS). Coercion matches the legacy submitConfig handler exactly.
const CFG_FIELDS = [
  { key: "collection", label: "Collection filter", kind: "text", ph: "blank = all (slow)" },
  { key: "currency", label: "Currency", kind: "currency" },
  { key: "min_roi", label: "Min ROI %", kind: "float" },
  { key: "stattrak", label: "StatTrak trade-ups", kind: "check" },
  { key: "fast_mode", label: "Fast mode (fewer price checks)", kind: "check" },
  { key: "skip_anomalies", label: "Skip underpriced-listing scan", kind: "check" },
  { key: "float_strategy", label: "Float-mix strategy (extra requests)", kind: "check" },
  { key: "mixed_strategy", label: "Mixed-collection filler strategy", kind: "check" },
  { key: "strategy_any_roi", label: "Run strategies on every result", kind: "check" },
  { key: "check_thirdparty", label: "Verify 3rd-party prices (drought)", kind: "check" },
  { key: "use_ml", label: "Use ML calibration (tiebreaker)", kind: "check" },
  { key: "anomaly_min_gap_pct", label: "Anomaly min gap %", kind: "float" },
  { key: "anomaly_min_volume", label: "Anomaly min volume", kind: "int" },
  { key: "scan_covert", label: "Scan Covert → knife/glove", kind: "check" },
  { key: "case", label: "Case filter", kind: "text", ph: "blank = all cases" },
];

function buildScanForm(body, curP, live) {
  body.textContent = "";
  const form = el("form");
  const grid = el("div");
  grid.style.cssText = "display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;align-items:end";
  const controls = {};

  for (const f of CFG_FIELDS) {
    const lab = el("label");
    lab.style.cssText = "display:flex;gap:6px;font-size:12px;color:var(--text-dim);align-items:center";
    if (f.kind === "check") {
      const c = el("input"); c.type = "checkbox";
      lab.append(c, document.createTextNode(" " + f.label));
      controls[f.key] = c;
    } else {
      lab.style.flexDirection = "column";
      lab.style.alignItems = "stretch";
      lab.appendChild(document.createTextNode(f.label));
      let c;
      if (f.kind === "currency") {
        c = el("select"); c.dataset.cur = "";
      } else {
        c = el("input");
        c.type = f.kind === "text" ? "text" : "number";
        if (f.kind !== "text") c.step = "1";
        if (f.ph) c.placeholder = f.ph;
      }
      lab.appendChild(c);
      controls[f.key] = c;
    }
    grid.appendChild(lab);
  }

  const btn = el("button", null, "Apply & rescan");
  btn.type = "submit";
  btn.style.marginTop = "10px";
  form.append(grid, btn);
  body.appendChild(form);

  const applyCfg = (cfg) => {
    for (const f of CFG_FIELDS) {
      const c = controls[f.key];
      if (!c || cfg[f.key] === undefined || cfg[f.key] === null) continue;
      if (f.kind === "check") c.checked = !!cfg[f.key];
      else c.value = cfg[f.key];
    }
  };

  // currencies first (so setting cfg.currency on the <select> takes), then current config.
  (async () => {
    const cur = await curP;
    if (!live()) return;
    fillCurrency(controls.currency, cur, "1");
    let st = null;
    try { st = await api.status(); } catch { /* leave defaults */ }
    if (live() && st && st.config) applyCfg(st.config);
  })();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const b = {};
    for (const f of CFG_FIELDS) {
      const c = controls[f.key];
      if (f.kind === "check") b[f.key] = c.checked;
      else if (f.kind === "currency") b[f.key] = parseInt(c.value, 10) || 1;
      else if (f.kind === "int") b[f.key] = parseInt(c.value, 10) || 0;
      else if (f.kind === "float") b[f.key] = parseFloat(c.value) || 0;
      else b[f.key] = c.value.trim();
    }
    btn.disabled = true;
    btn.textContent = "applying…";
    try {
      await api.setConfig(b);      // POST /api/config
      await api.scanNow();         // POST /api/scan-now
      btn.textContent = "applied — scan queued";
    } catch {
      btn.textContent = "apply failed — retry";
    } finally {
      btn.disabled = false;
      setTimeout(() => { if (live()) btn.textContent = "Apply & rescan"; }, 2500);
    }
  });
}

// ── panel 2 · scan progress (3 s serial poll of api.status) ────────────────────────────────
function buildScanProgress(body, live) {
  body.textContent = "";
  const track = el("div");
  track.style.cssText = "height:6px;background:var(--grid);border-radius:var(--r);overflow:hidden";
  const fill = el("div");
  fill.style.cssText = "height:100%;width:0%;background:var(--accent);transition:none";
  track.appendChild(fill);

  const stats = el("div");
  stats.style.marginTop = "8px";

  const btnRow = el("div");
  btnRow.style.cssText = "display:flex;gap:6px;margin-top:10px;flex-wrap:wrap";
  const mk = (label, fn) => {
    const b = el("button", "secondary", label);
    b.type = "button";
    b.addEventListener("click", () => { fn().catch(() => {}); });
    return b;
  };
  btnRow.append(mk("Scan", api.scanNow), mk("Pause", api.pause), mk("Resume", api.resume), mk("Stop", api.stop));

  body.append(track, stats, btnRow);

  let prev = null; // { current, stage, t } — for the items/s + ETA derivative
  const tick = async () => {
    if (!live()) return;
    let s = null;
    try { s = await api.status(); } catch { /* one dropped poll — keep last paint */ }
    if (!live()) return;
    if (s) prev = paintScan(fill, stats, s, prev);
    if (live()) pollTimer = setTimeout(tick, 3000);
  };
  tick();
}

function fmtEta(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function paintScan(fill, statsBox, s, prev) {
  const p = s.progress || {};
  const cur = Number(p.current) || 0;
  const tot = Number(p.total) || 0;
  fill.style.width = (tot > 0 ? Math.max(0, Math.min(1, cur / tot)) * 100 : 0).toFixed(1) + "%";

  const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
  let rate = null;
  if (prev && prev.stage === p.stage && tot > 0 && cur >= prev.current && now > prev.t) {
    rate = (cur - prev.current) / ((now - prev.t) / 1000);
  }
  const eta = rate && rate > 0 && tot > cur ? (tot - cur) / rate : null;

  crosshairReadout(statsBox, [
    { label: "status", value: s.status || "idle" },
    { label: "phase", value: p.stage || "—" },
    { label: "done / total", value: tot ? `${cur} / ${tot}` : "—" },
    { label: "items/s", value: rate && Number.isFinite(rate) ? rate.toFixed(1) : "—" },
    { label: "eta", value: eta != null ? fmtEta(eta) : "—" },
  ]);
  return { current: cur, stage: p.stage, t: now };
}

// ── panel 3 · manual trade-up builder ─────────────────────────────────────────────────────
function buildManualBuilder(root, body, curP, live) {
  body.textContent = "";
  body.style.display = "grid";
  body.style.gap = "12px";

  const units = [];          // [{ skin, wear, count }]
  let selected = null;       // the api.skins() row picked from the dropdown
  let result = null;         // last successful /api/manual-tradeup result
  let need = 10;             // constants.standard_input_count
  let suggestTimer = null;

  // -- add-skin form --
  const form = el("form");
  form.style.cssText = "display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;position:relative";

  const skinWrap = el("label");
  skinWrap.style.cssText = "display:flex;flex-direction:column;gap:2px;position:relative;flex:1;min-width:220px;font-size:12px;color:var(--text-dim)";
  skinWrap.appendChild(document.createTextNode("Skin"));
  const skinIn = el("input");
  skinIn.type = "text";
  skinIn.placeholder = "start typing a skin name…";
  skinIn.autocomplete = "off";
  const sugg = el("div");
  sugg.style.cssText = "position:absolute;top:100%;left:0;right:0;z-index:6;background:var(--bg-raised);border:1px solid var(--grid-strong);max-height:220px;overflow:auto;display:none";
  skinWrap.append(skinIn, sugg);

  const wearWrap = el("label");
  wearWrap.style.cssText = "display:flex;flex-direction:column;gap:2px;font-size:12px;color:var(--text-dim)";
  wearWrap.appendChild(document.createTextNode("Wear"));
  const wearSel = el("select");
  wearWrap.appendChild(wearSel);

  const countWrap = el("label");
  countWrap.style.cssText = "display:flex;flex-direction:column;gap:2px;font-size:12px;color:var(--text-dim)";
  countWrap.appendChild(document.createTextNode("Count"));
  const countIn = el("input");
  countIn.type = "number"; countIn.min = "1"; countIn.max = "10"; countIn.value = "1";
  countIn.style.width = "64px";
  countWrap.appendChild(countIn);

  const addBtn = el("button", null, "Add"); addBtn.type = "submit";
  const clearBtn = el("button", "secondary", "Clear"); clearBtn.type = "button";
  form.append(skinWrap, wearWrap, countWrap, addBtn, clearBtn);

  // -- contract strip + FLOAT + landing odds --
  const strip = el("div");
  strip.id = "contract-strip";
  strip.style.cssText = "display:flex;gap:3px";

  const stripCaption = el("p", "chart-caption");

  const floatBox = el("div"); // FLOAT instrument sizes once from clientWidth — panel is on-screen

  const oddsWrap = el("div");
  const oddsLabel = el("div", "chart-caption", "Landing odds (average input float → output wear)");
  const oddsBar = el("div");
  oddsBar.style.cssText = "display:flex;height:22px;width:100%;border:1px solid var(--grid);overflow:hidden";
  oddsWrap.append(oddsLabel, oddsBar);

  // -- stattrak / currency / compute --
  const ctl = el("div");
  ctl.style.cssText = "display:flex;gap:12px;align-items:center;flex-wrap:wrap";
  const stLab = el("label");
  stLab.style.cssText = "display:flex;gap:6px;align-items:center;font-size:12px;color:var(--text-dim)";
  const stIn = el("input"); stIn.type = "checkbox";
  stLab.append(stIn, document.createTextNode(" StatTrak"));
  const curLab = el("label");
  curLab.style.cssText = "display:flex;gap:6px;align-items:center;font-size:12px;color:var(--text-dim)";
  curLab.appendChild(document.createTextNode("Currency"));
  const curSel = el("select"); curSel.dataset.cur = "";
  curLab.appendChild(curSel);
  const computeBtn = el("button", null, "Compute (0/10)");
  computeBtn.type = "button";
  computeBtn.disabled = true;
  ctl.append(stLab, curLab, computeBtn);

  const unitBox = el("div");
  const resultBox = el("div");
  const msg = el("div");
  msg.style.cssText = "font-size:12px;color:var(--warn);white-space:pre-wrap";
  const setMsg = (t) => { msg.textContent = t || ""; };

  body.append(form, strip, stripCaption, unitBox, oddsWrap, floatBox, ctl, resultBox, msg);

  // FLOAT instrument — mount once now that floatBox is in the live DOM (real width).
  try {
    floatInst = mountFloatInstrument(floatBox, { pricesByWear: {}, floatCap: { min: 0, max: 1 }, marker: null });
  } catch {
    floatBox.textContent = "float instrument unavailable";
  }

  // currency select + required-count from constants
  curP.then((cur) => { if (live()) fillCurrency(curSel, cur, "1"); });
  api.constants().then((c) => {
    if (live() && c && Number(c.standard_input_count) > 0) { need = Number(c.standard_input_count); renderAll(); }
  }).catch(() => {});

  const fillWears = (wears) => {
    const list = Array.isArray(wears) && wears.length ? wears : WEAR_NAMES;
    wearSel.textContent = "";
    for (const w of list) {
      const o = el("option", null, w);
      o.value = w;
      if (w === "Field-Tested") o.selected = true;
      wearSel.appendChild(o);
    }
  };
  fillWears();

  const totalUnits = () => units.reduce((n, u) => n + u.count, 0);
  const inputFloats = () => {
    const xs = [];
    for (const u of units) {
      const m = midpointOfWearName(u.wear);
      if (m == null) continue;
      for (let i = 0; i < u.count; i++) xs.push(m);
    }
    return xs;
  };
  const pricesByWearFromResult = (r) => {
    if (!r || !Array.isArray(r.output_distribution)) return {};
    const acc = {};
    for (const o of r.output_distribution) {
      const b = WEAR_BANDS.find((w) => w.name === o.wear);
      const price = Number(o.price);
      if (!b || !Number.isFinite(price)) continue;
      const w = Number(o.prob_pct) || 1;
      const a = acc[b.short] || (acc[b.short] = { w: 0, p: 0 });
      a.w += w; a.p += price * w;
    }
    const out = {};
    for (const [k, a] of Object.entries(acc)) if (a.w) out[k] = a.p / a.w;
    return out;
  };

  function renderAll() {
    const total = totalUnits();
    const filled = Math.min(total, need);

    // contract strip — one <i> per filled cell, --accent border when filled
    strip.textContent = "";
    for (let i = 0; i < need; i++) {
      const cell = el("div");
      const on = i < filled;
      cell.style.cssText = "flex:1;height:26px;display:flex;align-items:center;justify-content:center;"
        + "background:var(--bg-panel);border:1px solid " + (on ? "var(--accent)" : "var(--grid)");
      if (on) {
        const mark = el("i");
        mark.style.cssText = "width:8px;height:8px;background:var(--accent);display:block";
        cell.appendChild(mark);
      }
      strip.appendChild(cell);
    }

    const xs = inputFloats();
    const avg = xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
    stripCaption.textContent = `${total}/${need} inputs`
      + (avg != null ? `  ·  average input float ~ ${fmtFloat(avg)}` : "");

    // unit list (removable)
    unitBox.textContent = "";
    if (units.length) {
      const t = el("table", "tbl");
      const th = el("thead"), htr = el("tr");
      for (const h of ["Skin", "Wear", "×", ""]) htr.appendChild(el("th", h === "×" ? "num" : null, h));
      th.appendChild(htr); t.appendChild(th);
      const tb = el("tbody");
      units.forEach((u, i) => {
        const tr = el("tr");
        tr.appendChild(el("td", null, u.skin));   // API string → textContent (M1)
        tr.appendChild(el("td", null, u.wear));
        tr.appendChild(el("td", "num", "×" + u.count));
        const rm = el("button", "secondary", "remove");
        rm.type = "button";
        rm.addEventListener("click", () => { units.splice(i, 1); result = null; setMsg(""); renderAll(); });
        const rmTd = el("td"); rmTd.appendChild(rm);
        tr.appendChild(rmTd);
        tb.appendChild(tr);
      });
      t.appendChild(tb);
      unitBox.appendChild(t);
    }

    // landing-odds bar — 5 labelled segments, one active (deterministic bucket)
    const odds = landingOdds(xs, { min: 0, max: 1 });
    oddsBar.textContent = "";
    for (const k of ["FN", "MW", "FT", "WW", "BS"]) {
      const v = odds[k] || 0;
      const seg = el("div", null, v > 0 ? `${k} ${Math.round(v * 100)}%` : k);
      seg.style.cssText = "display:flex;align-items:center;justify-content:center;overflow:hidden;white-space:nowrap;"
        + "font:600 10px 'Space Grotesk',sans-serif;letter-spacing:.04em;border-right:1px solid var(--grid);"
        + (v > 0
          ? "flex:1 1 0;background:var(--accent);color:var(--accent-ink);"
          : "flex:0 0 22px;background:var(--bg-raised);color:var(--text-faint);");
      oddsBar.appendChild(seg);
    }

    // FLOAT instrument — marker follows the live average; per-wear prices once computed
    if (floatInst) {
      try {
        floatInst.update({
          pricesByWear: pricesByWearFromResult(result),
          floatCap: { min: 0, max: 1 },
          marker: avg,
        });
      } catch { /* instrument gone */ }
    }

    // result KPI row
    resultBox.textContent = "";
    if (result) {
      const kr = el("div", "kpi-row");
      const cell = (label, value, cls) => {
        const k = el("div", "kpi");
        k.appendChild(el("div", "kpi-label", label));
        k.appendChild(el("div", "kpi-value " + (cls || ""), value));
        return k;
      };
      const prof = Number(result.expected_profit);
      const roi = Number(result.expected_roi_pct);
      kr.append(
        cell("Cost", money(result.cost)),
        cell("EV", money(result.expected_revenue)),
        cell("After fee", money(result.expected_revenue_after_fee)),
        cell("Profit", money(prof), sgn(prof)),
        cell("ROI%", pct(roi), sgn(roi)),
      );
      resultBox.appendChild(kr);
      if (result.low_volume_warning) {
        resultBox.appendChild(el("p", "chart-caption",
          "At least one price rests on thin / dead-skin market data — verify before spending."));
      }
    }

    // panel-head count + compute gating
    const countSpan = root.querySelector('[data-k="mb-count"]');
    if (countSpan) countSpan.textContent = units.length ? `(${total}/${need})` : "";
    computeBtn.textContent = `Compute (${total}/${need})`;
    computeBtn.disabled = total !== need;
  }

  // -- autocomplete --
  skinIn.addEventListener("input", () => {
    clearTimeout(suggestTimer);
    selected = null;
    fillWears();
    const q = skinIn.value.trim();
    if (q.length < 2) { sugg.style.display = "none"; return; }
    suggestTimer = setTimeout(async () => {
      let rows = [];
      try { rows = await api.skins(q); } catch { rows = []; }
      if (!live()) return;
      rows = Array.isArray(rows) ? rows : [];
      sugg.textContent = "";
      if (!rows.length) { sugg.style.display = "none"; return; }
      for (const s of rows) {
        const item = el("div");
        item.style.cssText = "padding:4px 6px;cursor:pointer;font-size:12px;border-bottom:1px solid var(--grid)";
        item.appendChild(el("span", null, s.name)); // API string → textContent (M1)
        const tag = el("span", null, "  " + (s.rarity_name || "") + (s.tradeable === false ? " · can't trade up" : ""));
        tag.style.color = "var(--text-faint)";
        item.appendChild(tag);
        item.addEventListener("mousedown", (ev) => {
          ev.preventDefault();
          selected = s;
          skinIn.value = s.name;
          fillWears(s.wears);
          sugg.style.display = "none";
        });
        sugg.appendChild(item);
      }
      sugg.style.display = "block";
    }, 250);
  });
  skinIn.addEventListener("blur", () => { setTimeout(() => { sugg.style.display = "none"; }, 150); });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const name = skinIn.value.trim();
    if (!name) return;
    if (!selected || selected.name !== name) {
      setMsg("Pick the skin from the list so its wears / collection are known.");
      return;
    }
    const room = need - totalUnits();
    if (room <= 0) { setMsg(`A contract is exactly ${need} inputs — remove one first.`); return; }
    const wear = wearSel.value;
    const add = Math.max(1, Math.min(parseInt(countIn.value, 10) || 1, room));
    const ex = units.find((u) => u.skin === name && u.wear === wear);
    if (ex) ex.count += add;
    else units.push({ skin: name, wear, count: add });
    selected = null;
    skinIn.value = "";
    countIn.value = "1";
    result = null;
    setMsg("");
    renderAll();
  });

  clearBtn.addEventListener("click", () => {
    units.length = 0;
    result = null;
    setMsg("");
    renderAll();
  });

  computeBtn.addEventListener("click", async () => {
    if (totalUnits() !== need) return;
    computeBtn.disabled = true;
    computeBtn.textContent = "pricing every input & output (live)…";
    setMsg("");
    // Direct fetch (not api.manualTradeup) so the server's {ok:false,error} body on a 400/422
    // — the "prices are missing" case — reaches the operator instead of a bare status code.
    try {
      const res = await fetch("/api/manual-tradeup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          stattrak: stIn.checked,
          currency: parseInt(curSel.value, 10) || 1,
          units: units.map((u) => ({ skin: u.skin, wear: u.wear, count: u.count })),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (data && data.ok) {
        result = data.result;
      } else {
        result = null;
        const probs = Array.isArray(data && data.problems) && data.problems.length
          ? ": " + data.problems.map((p) => `${p.skin} — ${p.detail || p.issue}`).join("; ")
          : "";
        setMsg((data && data.error ? data.error : `compute failed (${res.status})`) + probs);
      }
    } catch (err) {
      result = null;
      setMsg("compute failed — " + err);
    }
    if (live()) renderAll();
  });

  renderAll();
}

// ── panel 4 · OPPS / COV / ANOM tables ────────────────────────────────────────────────────
// col = [header, valueFn, numeric?, signClassFn?]. valueFn returns a fmt.js string (safe) or a
// raw API string; either way it lands via el()'s textContent (M1).
const TRADEUP_COLS = [
  ["Collection", (r) => r.collection, false],
  ["From", (r) => r.from_rarity, false],
  ["To", (r) => r.to_rarity, false],
  ["Cost", (r) => money(r.cost), true],
  ["Cheapest input", (r) => r.cheapest_input, false],
  ["Revenue", (r) => money(r.expected_revenue), true],
  ["After fee", (r) => money(r.expected_revenue_after_fee), true],
  ["Profit", (r) => money(r.profit), true, (r) => sgn(r.profit)],
  ["ROI %", (r) => pct(r.roi_pct), true, (r) => sgn(r.roi_pct)],
  ["Max buy", (r) => money(r.max_input_price), true],
];
const COVERT_COLS = [
  ["Case", (r) => r.case, false],
  ["Cost", (r) => money(r.cost), true],
  ["Cheapest input", (r) => r.cheapest_input, false],
  ["Revenue", (r) => money(r.expected_revenue), true],
  ["After fee", (r) => money(r.expected_revenue_after_fee), true],
  ["Profit", (r) => money(r.profit), true, (r) => sgn(r.profit)],
  ["ROI %", (r) => pct(r.roi_pct), true, (r) => sgn(r.roi_pct)],
  ["Max buy", (r) => money(r.max_input_price), true],
];
const ANOM_COLS = [
  ["Item", (r) => r.name, false],
  ["Lowest", (r) => money(r.lowest_price), true],
  ["Median", (r) => money(r.median_price), true],
  ["Volume", (r) => (r.volume == null ? "—" : String(r.volume)), true],
  ["Gap %", (r) => pct(r.gap_pct), true, (r) => sgn(r.gap_pct)],
  ["Profit after fee", (r) => money(r.profit_after_fee), true, (r) => sgn(r.profit_after_fee)],
  ["Max buy", (r) => money(r.max_buy_price), true],
];

async function loadTable(body, fetchFn, cols, emptyMsg, live, countEl) {
  let rows;
  try { rows = await fetchFn(); } catch { if (live()) body.textContent = "fetch failed — reload."; return; }
  if (!live()) return;
  rows = Array.isArray(rows) ? rows : [];
  body.textContent = "";

  const t = el("table", "tbl");
  const thead = el("thead"), htr = el("tr");
  for (const c of cols) htr.appendChild(el("th", c[2] ? "num" : null, c[0]));
  thead.appendChild(htr);
  t.appendChild(thead);

  const tb = el("tbody");
  if (!rows.length) {
    const c = el("td", null, emptyMsg);
    c.colSpan = cols.length;
    c.style.color = "var(--text-faint)";
    const tr = el("tr"); tr.appendChild(c); tb.appendChild(tr);
  } else {
    for (const r of rows) {
      const tr = el("tr");
      for (const c of cols) {
        const cls = [c[2] ? "num" : "", c[3] ? c[3](r) : ""].filter(Boolean).join(" ");
        tr.appendChild(el("td", cls || null, c[1](r)));
      }
      tb.appendChild(tr);
    }
  }
  t.appendChild(tb);
  body.appendChild(t);
  if (countEl) countEl.textContent = rows.length ? `(${rows.length})` : "";
}

// shared: fill a currency <select> from /api/currencies [{code,name,symbol}]
function fillCurrency(sel, list, def) {
  if (!sel) return;
  const keep = sel.value;
  sel.textContent = "";
  for (const c of (Array.isArray(list) ? list : [])) {
    const o = el("option", null, `${c.name} (${c.symbol})`);
    o.value = String(c.code);
    sel.appendChild(o);
  }
  sel.value = keep || def || "1";
}
