// PRICE instrument (spec §5.2) — replaces the Task 6 stub.
// One col-12 panel: a lookup form, a multi-wear price-vs-time uPlot with a left-docked
// crosshair readout + a naive 24h trend ghost, and a float↔wear↔price band strip.
//
// Data: GET /api/price-history?skin=&wear=&stattrak=&currency= — fired once per wear bracket
// (the server needs a concrete `wear`; it 400s without one). Response per call:
//   { market_hash_name, history:[{ ts, lowest_price, median_price, volume }], prediction, min_points_required }
// We chart `lowest_price` over `ts` (unix seconds). There is no multi-wear endpoint.
//
// M1: every API string (skin names, market_hash_name) reaches the DOM via textContent /
// createElement — never innerHTML interpolation. money/pct/float output is safe.

import { api } from "../api.js";
import { WEAR_BANDS, money, pct, float as fmtFloat } from "../fmt.js";
import { uplotTheme, crosshairReadout, caption } from "../charts.js";

export const MIN_TREND_POINTS = 8;

// desaturated blue-grey tint per wear position (FN lightest → BS darkest)
const WEAR_TINT = { FN: "#9fb6d9", MW: "#8098c1", FT: "#667fa8", WW: "#52708f", BS: "#405a78" };

const EMPTY_TEXT =
  "Look up a skin to start recording. Steam has no price-history API — this is not retroactive.";

// ── trendExtrapolate — PURE, exported for the selftest ────────────────────────────────────
// Least-squares line through points=[{t,v}]. Returns null below MIN_TREND_POINTS, or when
// every t is equal (zero x-variance → no slope, would divide by zero). Centered sums keep
// epoch-second timestamps precise; the returned coeffs still satisfy at(ts)=slope*ts+intercept.
export function trendExtrapolate(points, hoursAhead = 24) {
  if (!Array.isArray(points) || points.length < MIN_TREND_POINTS) return null;
  const n = points.length;
  const xs = points.map((p) => Number(p.t));
  const ys = points.map((p) => Number(p.v));
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let sxx = 0, sxy = 0;
  for (let i = 0; i < n; i++) { const dx = xs[i] - mx; sxx += dx * dx; sxy += dx * (ys[i] - my); }
  if (sxx === 0) return null;
  const slope = sxy / sxx;
  const intercept = my - slope * mx;
  return { slope, intercept, at: (ts) => slope * Number(ts) + intercept };
}

// textContent-only element factory (M1).
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text);
  return n;
};

// Linear value at t between sorted knots. clamp=true flat-extrapolates past the ends;
// clamp=false returns null outside [first,last] (used for the 24h-ago baseline).
function interp(pts, t, clamp = true) {
  if (!pts || !pts.length) return null;
  if (t <= pts[0].t) return clamp ? pts[0].v : null;
  const last = pts[pts.length - 1];
  if (t >= last.t) return clamp ? last.v : null;
  for (let i = 1; i < pts.length; i++) {
    if (t <= pts[i].t) {
      const span = (pts[i].t - pts[i - 1].t) || 1;
      const k = (t - pts[i - 1].t) / span;
      return pts[i - 1].v + k * (pts[i].v - pts[i - 1].v);
    }
  }
  return last.v;
}

// ── screen lifecycle (Task 7 OVW standard: gen bump + {once} hashchange + isConnected) ─────
let gen = 0;
let onHash = null;
let uplot = null;

function stop() {
  gen++;
  if (onHash) { removeEventListener("hashchange", onHash); onHash = null; }
  if (uplot) { try { uplot.destroy(); } catch { /* already gone */ } uplot = null; }
}

export function renderPrice(root, params) {
  stop();
  const myGen = ++gen;
  const live = () => myGen === gen && root.isConnected;

  const wearNames = WEAR_BANDS.map((b) => b.name);
  const rawSkin = (params.get("skin") || "").trim();
  let skin = rawSkin;
  let wear = (params.get("wear") || "").trim();
  // omnibar routes `#price?skin=AK-47 | Redline (Field-Tested)` — split the trailing "(Wear)".
  const m = rawSkin.match(/^(.+?)\s+\(([^()]+)\)\s*$/);
  if (m && wearNames.includes(m[2].trim())) { skin = m[1].trim(); wear = m[2].trim(); }
  if (!wearNames.includes(wear)) wear = "Field-Tested";
  const stattrak = params.get("stattrak") === "true";
  const currency = parseInt(params.get("currency"), 10) || 1;

  root.innerHTML = `<div class="grid"><div class="panel col-12">
    <div class="panel-head"><span data-k="ttl"></span></div>
    <div class="panel-body" data-k="body"></div></div></div>`;
  root.querySelector('[data-k="ttl"]').textContent = `PRICE · ${skin || "price history"}`;
  const body = root.querySelector('[data-k="body"]');

  onHash = () => stop();
  addEventListener("hashchange", onHash, { once: true });

  buildForm(body, { skin, wear, stattrak, currency }, live);

  const readoutBox = el("div"); readoutBox.style.margin = "8px 0";
  const chartBox = el("div", "chart-box");
  const stripWrap = el("div");
  body.append(readoutBox, chartBox, stripWrap);
  caption(body, "Every price this tool has recorded for this skin since you first looked it up.");

  if (!skin) { crosshairReadout(readoutBox, [{ label: "", value: EMPTY_TEXT }]); return; }
  crosshairReadout(readoutBox, [{ label: "", value: "loading price history…" }]);

  Promise.allSettled(
    WEAR_BANDS.map((b) => api.priceHistory({
      skin, wear: b.name, stattrak: stattrak ? "true" : "false", currency,
    })),
  ).then((settled) => {
    if (!live()) return;
    const series = [];
    settled.forEach((r, i) => {
      const b = WEAR_BANDS[i];
      const hist = r.status === "fulfilled" && Array.isArray(r.value?.history) ? r.value.history : [];
      const pts = hist
        .map((e) => ({ t: Number(e.ts), v: e.lowest_price == null ? NaN : Number(e.lowest_price) }))
        .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
        .sort((a, c) => a.t - c.t);
      if (pts.length) series.push({ short: b.short, mid: (b.lo + b.hi) / 2, tint: WEAR_TINT[b.short], pts });
    });

    const total = series.reduce((n, s) => n + s.pts.length, 0);
    if (!series.length || total < MIN_TREND_POINTS) {
      crosshairReadout(readoutBox, [{ label: "", value: EMPTY_TEXT }]);
      return; // no chart, no strip → no dangling axes
    }

    const selShort = WEAR_BANDS.find((b) => b.name === wear)?.short;
    const sel = series.find((s) => s.short === selShort)
      || series.slice().sort((a, c) => c.pts.length - a.pts.length)[0];
    const tf = trendExtrapolate(sel.pts);

    drawChart(chartBox, readoutBox, series, sel, tf, stattrak);
    drawStrip(stripWrap, series);
  });
}

// ── lookup form ──────────────────────────────────────────────────────────────────────────
function buildForm(body, init, live) {
  const form = el("form");
  form.style.cssText = "display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;position:relative;margin-bottom:6px";

  const skinWrap = el("label");
  skinWrap.style.cssText = "display:flex;flex-direction:column;gap:2px;position:relative;flex:1;min-width:240px;font-size:12px;color:var(--text-dim)";
  skinWrap.appendChild(document.createTextNode("Skin"));
  const skinIn = el("input");
  skinIn.type = "text"; skinIn.autocomplete = "off";
  skinIn.placeholder = "start typing a skin name…";
  skinIn.value = init.skin || "";
  const sugg = el("div");
  sugg.style.cssText = "position:absolute;top:100%;left:0;right:0;z-index:6;background:var(--bg-raised);border:1px solid var(--grid-strong);max-height:220px;overflow:auto;display:none";
  skinWrap.append(skinIn, sugg);

  const wearWrap = el("label");
  wearWrap.style.cssText = "display:flex;flex-direction:column;gap:2px;font-size:12px;color:var(--text-dim)";
  wearWrap.appendChild(document.createTextNode("Wear"));
  const wearSel = el("select");
  for (const b of WEAR_BANDS) {
    const o = el("option", null, b.name); o.value = b.name;
    if (b.name === init.wear) o.selected = true;
    wearSel.appendChild(o);
  }
  wearWrap.appendChild(wearSel);

  const stLab = el("label");
  stLab.style.cssText = "display:flex;gap:6px;align-items:center;font-size:12px;color:var(--text-dim)";
  const stIn = el("input"); stIn.type = "checkbox"; stIn.checked = !!init.stattrak;
  stLab.append(stIn, document.createTextNode(" StatTrak"));

  const curLab = el("label");
  curLab.style.cssText = "display:flex;flex-direction:column;gap:2px;font-size:12px;color:var(--text-dim)";
  curLab.appendChild(document.createTextNode("Currency"));
  const curSel = el("select");
  const o0 = el("option", null, "USD ($)"); o0.value = "1"; curSel.appendChild(o0);
  curSel.value = String(init.currency || 1);
  curLab.appendChild(curSel);

  const go = el("button", null, "Look up"); go.type = "submit";
  form.append(skinWrap, wearWrap, stLab, curLab, go);
  body.appendChild(form);

  api.currencies().then((list) => {
    if (!live() || !Array.isArray(list) || !list.length) return;
    curSel.textContent = "";
    for (const c of list) {
      const o = el("option", null, `${c.name} (${c.symbol})`); o.value = String(c.code);
      curSel.appendChild(o);
    }
    curSel.value = String(init.currency || 1);
  }).catch(() => {});

  // debounced api.skins autocomplete — mirrors calculator.js's shape (M1: names via textContent)
  let t = null;
  skinIn.addEventListener("input", () => {
    clearTimeout(t);
    const q = skinIn.value.trim();
    if (q.length < 2) { sugg.style.display = "none"; return; }
    t = setTimeout(async () => {
      let rows = [];
      try { rows = await api.skins(q); } catch { rows = []; }
      if (!live()) return;
      rows = Array.isArray(rows) ? rows : [];
      sugg.textContent = "";
      if (!rows.length) { sugg.style.display = "none"; return; }
      for (const s of rows) {
        const item = el("div");
        item.style.cssText = "padding:4px 6px;cursor:pointer;font-size:12px;border-bottom:1px solid var(--grid)";
        item.appendChild(el("span", null, s.name));
        const tag = el("span", null, "  " + (s.rarity_name || ""));
        tag.style.color = "var(--text-faint)";
        item.appendChild(tag);
        item.addEventListener("mousedown", (ev) => {
          ev.preventDefault();
          skinIn.value = s.name;
          if (Array.isArray(s.wears) && s.wears.length) {
            wearSel.textContent = "";
            for (const w of s.wears) { const o = el("option", null, w); o.value = w; wearSel.appendChild(o); }
          }
          sugg.style.display = "none";
        });
        sugg.appendChild(item);
      }
      sugg.style.display = "block";
    }, 250);
  });
  skinIn.addEventListener("blur", () => setTimeout(() => { sugg.style.display = "none"; }, 150));

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    let name = skinIn.value.trim();
    if (!name) return;
    // strip a trailing "(Wear)" so the hash carries a clean base skin + an explicit wear
    const mm = name.match(/^(.+?)\s+\(([^()]+)\)\s*$/);
    if (mm && WEAR_BANDS.some((b) => b.name === mm[2].trim())) name = mm[1].trim();
    const qs = new URLSearchParams({
      skin: name, wear: wearSel.value,
      stattrak: stIn.checked ? "true" : "false", currency: curSel.value || "1",
    });
    location.hash = "#price?" + qs.toString();
  });
}

// ── chart ────────────────────────────────────────────────────────────────────────────────
function drawChart(chartBox, readoutBox, series, sel, tf, stattrak) {
  const theme = uplotTheme();
  const accent = theme._accent || "#5C7CFF";

  const tsSet = new Set();
  for (const s of series) for (const p of s.pts) tsSet.add(p.t);
  const xs = [...tsSet].sort((a, b) => a - b);
  const lastTs = xs[xs.length - 1];
  const horizonTs = lastTs + 86400;
  xs.push(horizonTs); // phantom x so uPlot's auto-range leaves room for the trend ghost

  const data = [xs];
  for (const s of series) {
    const map = new Map(s.pts.map((p) => [p.t, p.v]));
    data.push(xs.map((tv) => (map.has(tv) ? map.get(tv) : null)));
  }

  const seriesOpts = [{}];
  for (const s of series) {
    seriesOpts.push({
      label: s.short,
      stroke: s.tint,
      width: s.short === sel.short ? 2 : 1.25,
      points: { show: false },
      spanGaps: true,
      dash: stattrak ? [6, 4] : undefined,
    });
  }

  const paint = (ts) => {
    const rows = series.map((s) => {
      const anchorT = ts == null ? s.pts[s.pts.length - 1].t : ts;
      const cur = ts == null ? s.pts[s.pts.length - 1].v : interp(s.pts, ts);
      const base = interp(s.pts, anchorT - 86400, false);
      const d = (base != null && base !== 0 && cur != null) ? ((cur - base) / base) * 100 : null;
      return {
        label: s.short,
        value: `${money(cur)}  Δ24h ${d == null ? "—" : pct(d)}  float≈${fmtFloat(s.mid, 2)}`,
        tone: d == null ? "" : d > 0 ? "pos" : d < 0 ? "neg" : "",
      };
    });
    if (tf) rows.push({ label: `trend +24h (${sel.short})`, value: `${money(tf.at(horizonTs))}  naive` });
    crosshairReadout(readoutBox, rows);
  };

  const drawGhost = (u) => {
    if (!tf) return;
    const { ctx } = u;
    const x0 = u.valToPos(lastTs, "x", true), x1 = u.valToPos(horizonTs, "x", true);
    const y0 = u.valToPos(interp(sel.pts, lastTs), "y", true);
    const y1 = u.valToPos(tf.at(horizonTs), "y", true);
    ctx.save();
    ctx.strokeStyle = accent; ctx.setLineDash([5, 4]); ctx.lineWidth = 1.5; ctx.globalAlpha = 0.7;
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
    ctx.restore();
  };

  uplot = new uPlot({
    ...theme,
    width: chartBox.clientWidth || 600,
    height: chartBox.clientHeight || 260,
    series: seriesOpts,
    hooks: {
      draw: [drawGhost],
      setCursor: [(u) => {
        const lx = u.cursor.left;
        paint(lx == null || lx < 0 ? null : u.posToVal(lx, "x"));
      }],
    },
  }, data, chartBox);

  paint(null);
}

// ── wear-band strip (float 0→1, tinted per band, tick at each recorded bracket midpoint) ──
function drawStrip(wrap, series) {
  wrap.textContent = "";
  const present = new Set(series.map((s) => s.short));
  const bar = el("div");
  bar.style.cssText = "display:flex;height:40px;margin-top:8px;border:1px solid var(--grid)";
  for (const b of WEAR_BANDS) {
    const on = present.has(b.short);
    const cell = el("div");
    cell.style.cssText = `flex:${(b.hi - b.lo).toFixed(3)} 0 0;position:relative;`
      + `background:${WEAR_TINT[b.short]};opacity:${on ? "1" : "0.28"};`
      + "border-right:1px solid var(--grid);display:flex;align-items:flex-end;justify-content:center";
    if (on) {
      const tick = el("i");
      tick.style.cssText = "position:absolute;top:0;bottom:0;left:50%;width:1px;background:var(--accent)";
      const lab = el("span", null, `${b.short} float≈${fmtFloat((b.lo + b.hi) / 2, 2)}`);
      lab.style.cssText = "position:relative;font:600 9px 'Space Grotesk',sans-serif;color:var(--accent-ink);padding-bottom:2px;letter-spacing:.03em";
      cell.append(tick, lab);
    }
    bar.appendChild(cell);
  }
  wrap.appendChild(bar);
  wrap.appendChild(el("p", "chart-caption",
    "Wear brackets across float 0→1. Ticks mark each recorded bracket's midpoint float — the value shown as float≈ in the readout."));
}
