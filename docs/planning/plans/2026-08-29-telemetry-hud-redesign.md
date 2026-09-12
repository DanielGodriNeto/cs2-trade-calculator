# Telemetry-HUD UI/UX Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat vanilla-JS dashboard in `web/static/` with a dense, keyboard-driven "telemetry HUD" — a hairline panel grid, a `>_` command bar, and three interactive chart instruments (crosshair price scrubber, float-vs-price curve, collection×ROI heatmap) — rebuilding the Overview / Opportunities / Calculator screens and reskinning the rest.

**Architecture:** No build step. Native ES modules under `web/static/js/`, one vendored chart lib (`uplot`), a rewritten `style.css` (design tokens + panel grid) and `index.html` (shell). The existing Patterns / Lab / Connections screens keep running their current render code, relocated into `js/views/legacy.js` and reskinned by CSS only. `web/server.py` gains a generic handler for two new static dirs (`/js/`, `/vendor/`); no other backend change.

**Tech Stack:** Vanilla ES modules, CSS custom properties + CSS grid, [uPlot](https://github.com/leeoniya/uPlot) (vendored, ~45 KB), HTML5 canvas (heatmap), Python stdlib `http.server` (unchanged except static routing). Google Fonts: Space Grotesk + JetBrains Mono.

**Spec:** `docs/planning/specs/2026-08-29-telemetry-hud-redesign-design.md`

## Global Constraints

- **No build tooling.** No npm, no bundler, no framework. Browser loads `.js` modules directly via `<script type="module">`. (Spec §1 non-goals)
- **No backend redesign.** `web/server.py` changes only to serve `/js/…` and `/vendor/…`. No route, no ML, no scanning logic touched. (Spec §1, §7.2)
- **No feature removal.** Patterns / Lab / Connections keep every current control working via `js/views/legacy.js`. (Spec §7.1)
- **Dark slate only.** No light theme. Base `--bg: #0B0F14`, hairlines `--grid: #1F242C`. (Spec §3.1)
- **One UI accent** `--accent: #5C7CFF`. Green `--profit: #35C88A` / red `--loss: #F2555A` appear **only** for profit/loss values, never as chrome. (Spec §2, §3.1)
- **Type:** Space Grotesk 600 (labels, KPI numbers, panel codes, headers) + JetBrains Mono 400/500 (everything else). `font-variant-numeric: tabular-nums` on every number. Fallbacks: `"Space Grotesk", Inter, system-ui, sans-serif` and `"JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace`. (Spec §3.2)
- **Quality floor:** visible keyboard focus ring on every interactive element; `prefers-reduced-motion` disables the caret blink and digit-flash; panel grid collapses to one column below 1100px and stays usable below 640px. (Spec §3.3, §3.4, §9)
- **No test framework exists in this repo.** Checks are: (a) `web/static/charts-selftest.html` — an assertions page that logs `SELFTEST OK` or throws, for pure logic; (b) preview-browser verification — start the `dashboard` launch config, load the view, confirm zero console errors, screenshot. Every task states which it uses.
- **Commit after every task** with a `feat(ui):` / `chore(ui):` prefixed message.

---

## File Structure

| File | Responsibility |
|---|---|
| `web/static/vendor/uplot.min.js`, `uplot.min.css` | Vendored uPlot. Never edited. |
| `web/static/charts-selftest.html` | Standalone assertions page for pure logic in `fmt.js`, `charts.js`, `command.js`. |
| `web/static/js/fmt.js` | Pure formatters: money, pct, float, signed, timeAgo, wearOfFloat, floatOfWear. |
| `web/static/js/api.js` | `fetch` wrappers over `/api/*`. One object `api` with a method per endpoint. |
| `web/static/js/charts.js` | `uplotTheme()`, `crosshairReadout()`, `heatmap()`, `sparkline()`, `caption()`. |
| `web/static/js/command.js` | Command-bar omnibar: index build, `matchCommand()`, `dispatchCommand()`. |
| `web/static/js/app.js` | Boot: mount shell refs, hashchange router, 3 s poll loop, global keys (`/`, `Esc`). |
| `web/static/js/views/overview.js` | OVW screen: `renderOverview(root, data)`. |
| `web/static/js/views/opportunities.js` | OPPS screen: `renderOpportunities(root)`, row-expand with FLOAT instrument. |
| `web/static/js/views/calculator.js` | CALC screen: scan form, progress, manual builder with FLOAT + landing-odds. |
| `web/static/js/views/price.js` | PRICE instrument screen: multi-wear time series + crosshair readout + wear-band strip. |
| `web/static/js/views/legacy.js` | Mounts the current Patterns / Lab / Connections markup + bind logic, unchanged behaviour. |
| `web/static/index.html` | Rewritten: shell markup (command bar, telemetry strip, nav, `#view-root`), font links, module entry. |
| `web/static/style.css` | Rewritten: tokens, shell, panel grid, tables, forms, instrument classes, `.legacy` scope, media queries, reduced-motion. |
| `web/static/app.js` | **Deleted at the end** (Task 11) once `legacy.js` carries the code it still needs. Kept until then. |
| `web/server.py` | `do_GET`: add generic `/js/…` + `/vendor/…` file serving with a traversal guard. |

Old `web/static/app.js` and `style.css` stay in place until Task 6 replaces `style.css` and Task 3–5 add the new modules; `app.js` is only removed in Task 11 after `legacy.js` proves out.

---

## Task 1: Vendor uPlot and serve the new static dirs

**Files:**
- Create: `web/static/vendor/uplot.min.js` (downloaded), `web/static/vendor/uplot.min.css` (downloaded)
- Create: `web/static/vendor/README.md` (provenance note)
- Modify: `web/server.py` — `do_GET`, just after the `if path == "/style.css"` line (currently ~`web/server.py:486`)

**Interfaces:**
- Consumes: nothing.
- Produces: `GET /vendor/uplot.min.js` → 200 `application/javascript`; `GET /js/<name>.js` → 200 `application/javascript`; both 404 on a missing file and 403 on any path containing `..`.

- [ ] **Step 1: Download uPlot into the repo**

uPlot ships as two files, no dependencies. Fetch a pinned version (1.6.x):

```bash
mkdir -p web/static/vendor
curl -L -o web/static/vendor/uplot.min.js  https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js
curl -L -o web/static/vendor/uplot.min.css https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css
```

Verify both are non-empty and the JS starts with a bundle header (not an HTML error page):

```bash
head -c 80 web/static/vendor/uplot.min.js; echo; wc -c web/static/vendor/uplot.min.*
```

Expected: `.js` ~45 KB, `.css` ~4 KB, JS first line contains `uPlot`.

- [ ] **Step 2: Write the provenance note**

`web/static/vendor/README.md`:

```markdown
# Vendored libraries

- `uplot.min.js` / `uplot.min.css` — uPlot 1.6.31, MIT.
  Source: https://unpkg.com/uplot@1.6.31/ (`uPlot.iife.min.js`, `uPlot.min.css`).
  Vendored (not CDN) so the dashboard shell has no network dependency.
  To update: re-download both from the same version path and bump this note.
```

- [ ] **Step 3: Add the generic static handler to `web/server.py`**

In `do_GET`, immediately after the `if path == "/style.css":` return line, insert:

```python
            if path.startswith("/js/") or path.startswith("/vendor/"):
                rel = path.lstrip("/")
                if ".." in rel.split("/"):
                    self.send_response(403); self.end_headers(); return
                ext = rel.rsplit(".", 1)[-1].lower()
                ctype = {
                    "js": "application/javascript; charset=utf-8",
                    "css": "text/css; charset=utf-8",
                    "html": "text/html; charset=utf-8",
                    "map": "application/json; charset=utf-8",
                }.get(ext, "application/octet-stream")
                return self._send_file(STATIC_DIR / rel, ctype)
```

(`STATIC_DIR` and `_send_file` already exist — `_send_file` returns 404 when the path is missing.)

- [ ] **Step 4: Smoke-test the routing**

```bash
python -m web.server &          # or use the `dashboard` launch config
sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/vendor/uplot.min.js   # 200
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/js/nope.js            # 404
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:5000/js/../server.py"     # 403
kill %1
```

Expected: `200`, `404`, `403`.

- [ ] **Step 5: Commit**

```bash
git add web/static/vendor web/server.py
git commit -m "chore(ui): vendor uPlot and serve /js and /vendor static dirs"
```

---

## Task 2: `fmt.js` — pure formatters

**Files:**
- Create: `web/static/js/fmt.js`
- Create: `web/static/charts-selftest.html` (starts here; later tasks append to it)

**Interfaces:**
- Consumes: nothing.
- Produces (all named exports):
  - `money(n, ccy = "USD") -> string` — `"$0.42"`, `"$1,240.00"`, `"—"` when `n` is null/NaN.
  - `pct(n, digits = 1) -> string` — `"+42.0%"`, `"-3.1%"`, `"—"` for null/NaN. Always signed.
  - `float(n, digits = 3) -> string` — `"0.162"`, `"—"` for null/NaN.
  - `signed(n) -> string` — `"+"` / `""` prefix + `n.toLocaleString()`.
  - `timeAgo(isoOrEpoch) -> string` — `"2m ago"`, `"3h ago"`, `"just now"`, `"—"` for falsy.
  - `WEAR_BANDS -> [{name, short, lo, hi}]` — FN 0–0.07, MW 0.07–0.15, FT 0.15–0.38, WW 0.38–0.45, BS 0.45–1.0.
  - `wearOfFloat(f) -> {name, short}` — bracket containing `f`.
  - `floatOfWear(short) -> number` — midpoint of that bracket (`"FT" -> 0.265`).

- [ ] **Step 1: Write the failing selftest page**

`web/static/charts-selftest.html`:

```html
<!doctype html><meta charset="utf-8"><title>selftest</title>
<body style="font:13px monospace;background:#0B0F14;color:#D7DEE8;padding:16px">
<pre id="out"></pre>
<script type="module">
const out = document.getElementById("out");
let pass = 0, fail = 0;
function eq(got, want, msg) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  out.textContent += `${ok ? "ok  " : "FAIL"} ${msg}  got=${JSON.stringify(got)}${ok ? "" : ` want=${JSON.stringify(want)}`}\n`;
  ok ? pass++ : fail++;
}
window.__eq = eq;

import * as fmt from "./js/fmt.js";
eq(fmt.money(0.42), "$0.42", "money cents");
eq(fmt.money(1240), "$1,240.00", "money thousands");
eq(fmt.money(null), "—", "money null");
eq(fmt.pct(42), "+42.0%", "pct positive signed");
eq(fmt.pct(-3.12), "-3.1%", "pct negative 1dp");
eq(fmt.float(0.1623), "0.162", "float 3dp");
eq(fmt.wearOfFloat(0.20).short, "FT", "wearOfFloat FT");
eq(fmt.floatOfWear("FT"), 0.265, "floatOfWear FT midpoint");

out.textContent += `\n${fail === 0 ? "SELFTEST OK" : "SELFTEST FAILED"} (${pass} pass, ${fail} fail)\n`;
</script>
```

- [ ] **Step 2: Open it, verify it fails**

Preview `http://localhost:5000/charts-selftest.html`. Expected: console `Failed to resolve module ./js/fmt.js` (file not created yet).

- [ ] **Step 3: Implement `web/static/js/fmt.js`**

```js
export const WEAR_BANDS = [
  { name: "Factory New",    short: "FN", lo: 0.00, hi: 0.07 },
  { name: "Minimal Wear",   short: "MW", lo: 0.07, hi: 0.15 },
  { name: "Field-Tested",   short: "FT", lo: 0.15, hi: 0.38 },
  { name: "Well-Worn",      short: "WW", lo: 0.38, hi: 0.45 },
  { name: "Battle-Scarred", short: "BS", lo: 0.45, hi: 1.00 },
];

const nn = (n) => n !== null && n !== undefined && !Number.isNaN(Number(n));

export function money(n, ccy = "USD") {
  if (!nn(n)) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: ccy }).format(Number(n));
}
export function pct(n, digits = 1) {
  if (!nn(n)) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}
export function float(n, digits = 3) {
  return nn(n) ? Number(n).toFixed(digits) : "—";
}
export function signed(n) {
  if (!nn(n)) return "—";
  const v = Number(n);
  return `${v > 0 ? "+" : ""}${v.toLocaleString("en-US")}`;
}
export function timeAgo(t) {
  if (!t) return "—";
  const ms = typeof t === "number" ? t * (t < 1e12 ? 1000 : 1) : Date.parse(t);
  if (Number.isNaN(ms)) return "—";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
export function wearOfFloat(f) {
  const b = WEAR_BANDS.find((w) => f >= w.lo && f < w.hi) || WEAR_BANDS[WEAR_BANDS.length - 1];
  return { name: b.name, short: b.short };
}
export function floatOfWear(short) {
  const b = WEAR_BANDS.find((w) => w.short === short);
  return b ? (b.lo + b.hi) / 2 : null;
}
```

- [ ] **Step 4: Reload the selftest, verify it passes**

Preview reload. Expected page text ends with `SELFTEST OK (8 pass, 0 fail)`, zero console errors.

- [ ] **Step 5: Commit**

```bash
git add web/static/js/fmt.js web/static/charts-selftest.html
git commit -m "feat(ui): fmt.js formatters + selftest harness"
```

---

## Task 3: `charts.js` — shared chart infrastructure

**Files:**
- Create: `web/static/js/charts.js`
- Modify: `web/static/charts-selftest.html` (append a `charts.js` block + load `vendor/uplot`)

**Interfaces:**
- Consumes: `fmt.js` (`money`, `pct`, `float`); global `uPlot` from `vendor/uplot.min.js`.
- Produces (named exports):
  - `cssVar(name) -> string` — trimmed computed value of a `--custom-prop` on `:root`.
  - `uplotTheme() -> object` — partial uPlot opts: `{ cursor, axes, legend }` styled from CSS vars (grid `--grid`, ticks `--text-dim`, mono font, vertical-only 1 px `--accent` cursor, `legend: { show: false }`).
  - `sparkline(el, values, { stroke } = {}) -> { destroy() }` — 1-series uPlot sized to `el`'s box, no axes/legend/cursor.
  - `caption(el, text) -> void` — appends `<p class="chart-caption">text</p>` to `el` (removes any prior one).
  - `crosshairReadout(panelEl, rows) -> void` — renders/replaces a `.readout` list in `panelEl`; `rows` is `[{ label, value, tone }]` where `tone ∈ {"", "pos", "neg", "dim"}`. Instant, no transition.
  - `heatmap(canvas, opts) -> { destroy(), hitTest(px, py) }` where `opts = { rows: string[], cols: string[], values: number[][], colorFor(v, min, max) -> css, fmt(v) -> string, onHover(r, c) , onClick(r, c) }`. `hitTest` returns `{ r, c } | null` for canvas-local pixels. Draws 1 px `--grid` separators and a legend strip below the grid.

- [ ] **Step 1: Append failing assertions to the selftest page**

Before the final summary block in `charts-selftest.html`, add:

```html
<script src="/vendor/uplot.min.js"></script>
<script type="module">
const eq = window.__eq;
import { heatmap, cssVar } from "./js/charts.js";

// colorFor + hitTest are pure geometry — assert them without visual inspection.
const cv = document.createElement("canvas");
cv.width = 300; cv.height = 100;                     // 3 cols x 2 rows => 100x40 cells (legend takes bottom 20)
const hm = heatmap(cv, {
  rows: ["a", "b"], cols: ["x", "y", "z"],
  values: [[1, 2, 3], [4, 5, 6]],
  colorFor: () => "#000", fmt: String, onHover: () => {}, onClick: () => {},
});
eq(hm.hitTest(10, 10), { r: 0, c: 0 }, "heatmap hitTest top-left cell");
eq(hm.hitTest(299, 79), { r: 1, c: 2 }, "heatmap hitTest bottom-right cell");
eq(hm.hitTest(10, 95), null, "heatmap hitTest in legend strip => null");
hm.destroy();
</script>
```

- [ ] **Step 2: Open selftest, verify the new block fails**

Reload preview. Expected: `Failed to resolve module ./js/charts.js`.

- [ ] **Step 3: Implement `web/static/js/charts.js`**

Key logic (write the file with these functions; `sparkline` and the uPlot parts are thin wrappers, `heatmap` is the substantive one):

```js
import { money, pct, float } from "./fmt.js";

export const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export function uplotTheme() {
  const grid = cssVar("--grid"), tick = cssVar("--text-dim"), accent = cssVar("--accent");
  return {
    legend: { show: false },
    cursor: { x: true, y: false, points: { show: false }, drag: { x: false, y: false } },
    axes: [
      { stroke: tick, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: "11px 'JetBrains Mono', monospace" },
      { stroke: tick, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: "11px 'JetBrains Mono', monospace" },
    ],
    _accent: accent,
  };
}

export function caption(el, text) {
  el.querySelector(":scope > .chart-caption")?.remove();
  const p = document.createElement("p");
  p.className = "chart-caption";
  p.textContent = text;
  el.appendChild(p);
}

export function crosshairReadout(panelEl, rows) {
  let box = panelEl.querySelector(":scope > .readout");
  if (!box) { box = document.createElement("div"); box.className = "readout"; panelEl.appendChild(box); }
  box.innerHTML = rows.map((r) =>
    `<div class="readout-row ${r.tone || ""}"><span class="readout-label">${r.label}</span><span class="readout-val">${r.value}</span></div>`
  ).join("");
}

export function sparkline(el, values, { stroke } = {}) {
  const w = el.clientWidth || 80, h = el.clientHeight || 20;
  const u = new uPlot({
    width: w, height: h, cursor: { show: false }, legend: { show: false },
    scales: { x: { time: false } },
    axes: [{ show: false }, { show: false }],
    series: [{}, { stroke: stroke || cssVar("--text-dim"), width: 1, points: { show: false } }],
  }, [values.map((_, i) => i), values], el);
  return { destroy: () => u.destroy() };
}

export function heatmap(canvas, opts) {
  const { rows, cols, values, colorFor, fmt, onHover, onClick } = opts;
  const LEGEND_H = 20;
  const ctx = canvas.getContext("2d");
  const gridCol = cssVar("--grid") || "#1F242C";

  const cellW = () => canvas.width / cols.length;
  const cellH = () => (canvas.height - LEGEND_H) / rows.length;

  function hitTest(px, py) {
    if (py < 0 || py >= canvas.height - LEGEND_H || px < 0 || px >= canvas.width) return null;
    const c = Math.floor(px / cellW()), r = Math.floor(py / cellH());
    if (r < 0 || r >= rows.length || c < 0 || c >= cols.length) return null;
    return { r, c };
  }

  let flat = values.flat().filter((v) => Number.isFinite(v));
  const min = Math.min(...flat), max = Math.max(...flat);

  function draw(hoverRC) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const w = cellW(), h = cellH();
    for (let r = 0; r < rows.length; r++) {
      for (let c = 0; c < cols.length; c++) {
        ctx.fillStyle = colorFor(values[r][c], min, max);
        ctx.fillRect(c * w, r * h, w - 1, h - 1);               // -1 leaves the 1px grid gap
      }
    }
    if (hoverRC) {
      ctx.strokeStyle = cssVar("--accent");
      ctx.lineWidth = 1;
      ctx.strokeRect(hoverRC.c * w + 0.5, hoverRC.r * h + 0.5, w - 2, h - 2);
    }
    // legend strip: min→max gradient
    const g = ctx.createLinearGradient(0, 0, canvas.width, 0);
    g.addColorStop(0, colorFor(min, min, max));
    g.addColorStop(1, colorFor(max, min, max));
    ctx.fillStyle = g;
    ctx.fillRect(0, canvas.height - LEGEND_H + 4, canvas.width, LEGEND_H - 8);
  }

  function move(e) {
    const rect = canvas.getBoundingClientRect();
    const rc = hitTest(e.clientX - rect.left, e.clientY - rect.top);
    draw(rc);
    if (rc) onHover(rc.r, rc.c);
  }
  function click(e) {
    const rect = canvas.getBoundingClientRect();
    const rc = hitTest(e.clientX - rect.left, e.clientY - rect.top);
    if (rc) onClick(rc.r, rc.c);
  }
  canvas.addEventListener("mousemove", move);
  canvas.addEventListener("click", click);
  draw(null);

  return {
    destroy() { canvas.removeEventListener("mousemove", move); canvas.removeEventListener("click", click); },
    hitTest,
  };
}
```

- [ ] **Step 4: Reload selftest, verify pass**

Expected: page ends `SELFTEST OK (11 pass, 0 fail)`, no console errors. (uPlot loaded as IIFE global; `sparkline` not asserted here — it's exercised visually in Task 8.)

- [ ] **Step 5: Commit**

```bash
git add web/static/js/charts.js web/static/charts-selftest.html
git commit -m "feat(ui): charts.js — uplotTheme, heatmap, sparkline, crosshair readout"
```

---

## Task 4: `api.js` — endpoint wrappers

**Files:**
- Create: `web/static/js/api.js`
- Reference (read, do not modify): `web/static/app.js` — every `fetch("/api/…")` call site.

**Interfaces:**
- Consumes: nothing.
- Produces: `export const api` with one method per endpoint the priority views need. All GET methods return parsed JSON and throw `Error("<path> -> <status>")` on non-2xx. All POST methods send JSON and tolerate an empty body.
  - GET: `status()`, `constants()`, `currencies()`, `networkStats()`, `tradeups()`, `covert()`, `anomalies()`, `bestSimulations()`, `archive(params)`, `leaderboards()`, `simStatus()`, `outcomes()`, `outcomesStats()`, `modelStatus()`, `connections()`, `priceHistory({skin,wear,stattrak,currency})`, `patternsOpps(params)`, `skins(q)`.
  - POST: `setConfig(cfg)`, `scanNow()`, `pause()`, `resume()`, `stop()`, `manualTradeup(body)`, `logOutcome(body)`, `updateOutcomeSale(body)`, `verifyTop()`, `trainModel()`.

- [ ] **Step 1: Implement `web/static/js/api.js`**

```js
async function getJSON(path, params) {
  const u = new URL(path, location.origin);
  if (params) for (const [k, v] of Object.entries(params))
    if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
  const r = await fetch(u.pathname + u.search);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}
async function postJSON(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  const t = await r.text();
  return t ? JSON.parse(t) : {};
}

export const api = {
  status: () => getJSON("/api/status"),
  constants: () => getJSON("/api/constants"),
  currencies: () => getJSON("/api/currencies"),
  networkStats: () => getJSON("/api/network-stats"),
  tradeups: () => getJSON("/api/tradeups"),
  covert: () => getJSON("/api/covert"),
  anomalies: () => getJSON("/api/anomalies"),
  bestSimulations: () => getJSON("/api/sim-population/best-simulations"),
  archive: (p) => getJSON("/api/sim-population/opportunities/archive", p),
  leaderboards: () => getJSON("/api/sim-population/leaderboards"),
  simStatus: () => getJSON("/api/sim-population/status"),
  outcomes: () => getJSON("/api/outcomes"),
  outcomesStats: () => getJSON("/api/outcomes/stats"),
  modelStatus: () => getJSON("/api/model-status"),
  connections: () => getJSON("/api/connections/status"),
  priceHistory: (p) => getJSON("/api/price-history", p),
  patternsOpps: (p) => getJSON("/api/patterns/opportunities", p),
  skins: (q) => getJSON("/api/skins", { q, limit: 20 }),

  setConfig: (c) => postJSON("/api/config", c),
  scanNow: () => postJSON("/api/scan-now"),
  pause: () => postJSON("/api/pause"),
  resume: () => postJSON("/api/resume"),
  stop: () => postJSON("/api/stop"),
  manualTradeup: (b) => postJSON("/api/manual-tradeup", b),
  logOutcome: (b) => postJSON("/api/log-outcome", b),
  updateOutcomeSale: (b) => postJSON("/api/update-outcome-sale", b),
  verifyTop: () => postJSON("/api/sim-population/verify-top"),
  trainModel: () => postJSON("/api/train-model"),
};
```

- [ ] **Step 2: Cross-check every path against `web/server.py`**

```bash
grep -oE '"/api/[a-z0-9/-]+"' web/static/js/api.js | sort -u > /tmp/used.txt
grep -oE 'path == "/api/[a-z0-9/-]+"' web/server.py | grep -oE '"/api/[a-z0-9/-]+"' | sort -u > /tmp/have.txt
comm -23 /tmp/used.txt /tmp/have.txt
```

Expected: **no output** (every path used exists in the server). If a line prints, fix the path in `api.js`.

- [ ] **Step 3: Runtime check via selftest**

Append to `charts-selftest.html` (needs the server running):

```html
<script type="module">
import { api } from "./js/api.js";
const eq = window.__eq;
try { const s = await api.status(); eq(typeof s, "object", "api.status() returns object"); }
catch (e) { eq(String(e), "no-error", "api.status() threw: " + e); }
</script>
```

Reload preview. Expected: `ok  api.status() returns object`.

- [ ] **Step 4: Commit**

```bash
git add web/static/js/api.js web/static/charts-selftest.html
git commit -m "feat(ui): api.js endpoint wrappers"
```

---

## Task 5: `command.js` — omnibar match + dispatch

**Files:**
- Create: `web/static/js/command.js`
- Modify: `web/static/charts-selftest.html` (append matcher assertions)

**Interfaces:**
- Consumes: `api.js` (`api.skins`); `location.hash` for view switches.
- Produces (named exports):
  - `NAV -> [{ code, label, hash }]` — `OVW #overview`, `OPPS #opportunities`, `CALC #calculator`, `PRICE #price`, `PAT #patterns`, `LAB #lab`, `CONN #connections`.
  - `ACTIONS -> [{ id, label, run, confirm? }]` — `scan` (`api.scanNow`), `verify top` (`api.verifyTop`, `confirm: true`), `train` (`api.trainModel`).
  - `matchCommand(query, ctx) -> [{ kind, label, hint, apply }]` — `kind ∈ {"view","skin","collection","action"}`; `ctx = { collections: string[], skins: [{name}] }`; case-insensitive token-subset match; max 8 results; `view` matches on code or label, `action` on `id` or `label`, `collection` on substring, `skin` on all-tokens-present. Empty/whitespace query → `[]`.
  - `dispatchCommand(item) -> Promise<void>` — runs `item.apply()`; for actions with `confirm`, calls `window.confirm(...)` first.

- [ ] **Step 1: Append failing assertions**

```html
<script type="module">
import { matchCommand } from "./js/command.js";
const eq = window.__eq;
const ctx = { collections: ["Dreams & Nightmares", "Recoil"], skins: [{ name: "AK-47 | Redline (Field-Tested)" }] };
eq(matchCommand("", ctx), [], "empty query => no results");
eq(matchCommand("opps", ctx)[0].kind, "view", "opps => view");
eq(matchCommand("go calc", ctx)[0].label.includes("CALC"), true, "go calc => CALC view");
eq(matchCommand("dream", ctx)[0].kind, "collection", "dream => collection");
eq(matchCommand("ak redline", ctx)[0].kind, "skin", "ak redline => skin");
eq(matchCommand("scan", ctx)[0].kind, "action", "scan => action");
</script>
```

- [ ] **Step 2: Verify it fails** — reload preview, `Failed to resolve module ./js/command.js`.

- [ ] **Step 3: Implement `web/static/js/command.js`**

```js
import { api } from "./api.js";

export const NAV = [
  { code: "OVW",  label: "OVW · overview",       hash: "#overview" },
  { code: "OPPS", label: "OPPS · opportunities", hash: "#opportunities" },
  { code: "CALC", label: "CALC · calculator",    hash: "#calculator" },
  { code: "PRICE",label: "PRICE · price history",hash: "#price" },
  { code: "PAT",  label: "PAT · rare patterns",  hash: "#patterns" },
  { code: "LAB",  label: "LAB · simulation lab", hash: "#lab" },
  { code: "CONN", label: "CONN · connections",   hash: "#connections" },
];

export const ACTIONS = [
  { id: "scan",       label: "SCAN · start a scan",           run: () => api.scanNow() },
  { id: "verify top", label: "VERIFY TOP · cross-check top 50",run: () => api.verifyTop(), confirm: "Queue the top 50 opportunities for a live cross-check?" },
  { id: "train",      label: "TRAIN · retrain the ROI model", run: () => api.trainModel() },
];

const tokens = (s) => s.toLowerCase().split(/\s+/).filter(Boolean);
const allIn = (needles, hay) => needles.every((n) => hay.includes(n));

export function matchCommand(query, ctx) {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const t = tokens(q.replace(/^go\s+/, ""));
  const out = [];

  for (const n of NAV)
    if (allIn(t, `${n.code} ${n.label}`.toLowerCase()))
      out.push({ kind: "view", label: n.label, hint: "view", apply: () => { location.hash = n.hash; } });

  for (const a of ACTIONS)
    if (allIn(tokens(q), `${a.id} ${a.label}`.toLowerCase()))
      out.push({ kind: "action", label: a.label, hint: "action", apply: a.run, confirm: a.confirm });

  for (const c of ctx.collections || [])
    if (c.toLowerCase().includes(q))
      out.push({ kind: "collection", label: c, hint: "filter OPPS", apply: () => { location.hash = `#opportunities?collection=${encodeURIComponent(c)}`; } });

  for (const s of ctx.skins || [])
    if (allIn(t, s.name.toLowerCase()))
      out.push({ kind: "skin", label: s.name, hint: "open PRICE", apply: () => { location.hash = `#price?skin=${encodeURIComponent(s.name)}`; } });

  return out.slice(0, 8);
}

export async function dispatchCommand(item) {
  if (item.confirm && !window.confirm(item.confirm)) return;
  await item.apply();
}
```

- [ ] **Step 4: Verify pass** — reload preview, summary line count rises, `SELFTEST OK`.

- [ ] **Step 5: Commit**

```bash
git add web/static/js/command.js web/static/charts-selftest.html
git commit -m "feat(ui): command.js omnibar matcher"
```

---

## Task 6: Shell — `style.css`, `index.html`, `app.js` router, `legacy.js`

This is the integration milestone: the new dark shell loads, nav switches views, and Patterns / Lab / Connections still work through relocated code. It is one task because the four files only make sense together — a reviewer accepts or rejects "the shell boots" as a unit.

**Files:**
- Rewrite: `web/static/style.css`
- Rewrite: `web/static/index.html`
- Create: `web/static/js/app.js`
- Create: `web/static/js/views/legacy.js`
- Keep (still referenced by `legacy.js`): `web/static/app.js` (old), for now.

**Interfaces:**
- Consumes: `command.js` (`NAV`, `matchCommand`, `dispatchCommand`), `api.js`, `fmt.js`.
- Produces:
  - `#view-root` — the element every `views/*.js` renders into.
  - `window.__hud = { route(): string, ctx: { collections, skins }, refresh() }` — read by views/selftest.
  - `renderLegacy(root, view)` in `legacy.js` where `view ∈ {"patterns","lab","connections"}` — injects that section's original markup and calls its original bind functions.

- [ ] **Step 1: Write `style.css` — tokens + primitives**

Start the file with the token block **verbatim from spec §3.1** (`:root { --bg:#0B0F14; … --r-gold:#FFD700; }`), then:

```css
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 400 13px/1.35 "JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
}
h1,h2,h3,h4,.code,.kpi-value {
  font-family: "Space Grotesk", Inter, system-ui, sans-serif;
  text-transform: uppercase; letter-spacing: .04em; font-weight: 600; margin: 0;
}
a { color: var(--accent); text-decoration: none; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

/* shell */
.hud { display: flex; flex-direction: column; min-height: 100%; }
.cmdbar, .telemetry { position: sticky; z-index: 20; background: var(--bg); border-bottom: 1px solid var(--grid); }
.cmdbar { top: 0; display: flex; align-items: center; gap: 16px; padding: 8px 14px; }
.telemetry { top: 39px; display: flex; flex-wrap: wrap; gap: 14px; padding: 6px 14px; font-size: 12px; color: var(--text-dim); }
.cmd-input { flex: 0 0 340px; background: var(--bg-raised); border: 1px solid var(--grid-strong); border-radius: var(--r); color: var(--text); padding: 5px 8px; font: inherit; }
.cmd-input:focus { border-color: var(--accent); }
.nav { display: flex; gap: 2px; }
.nav a { padding: 5px 9px; border-radius: var(--r); color: var(--text-dim); font: 600 12px "Space Grotesk", sans-serif; letter-spacing: .04em; }
.nav a.active { background: var(--accent); color: var(--accent-ink); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; background: var(--text-faint); }
.dot.ok { background: var(--accent); } .dot.warn { background: var(--warn); } .dot.err { background: var(--loss); }

/* panel grid — the gap IS the gridline */
.grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 1px; background: var(--grid); padding: 1px; }
.panel { background: var(--bg-panel); display: flex; flex-direction: column; min-width: 0; }
.panel > .panel-head { display: flex; align-items: center; justify-content: space-between; height: 22px; padding: 0 8px; border-bottom: 1px solid var(--grid); font: 600 11px "Space Grotesk", sans-serif; letter-spacing: .06em; color: var(--text-dim); }
.panel > .panel-body { padding: 10px; overflow: auto; }
.col-12 { grid-column: span 12; } .col-6 { grid-column: span 6; } .col-4 { grid-column: span 4; } .col-3 { grid-column: span 3; }

/* tables */
table.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
table.tbl th { text-align: left; color: var(--text-dim); font: 600 11px "Space Grotesk", sans-serif; text-transform: uppercase; letter-spacing: .03em; padding: 4px 8px; border-bottom: 1px solid var(--grid); }
table.tbl td { padding: 0 8px; height: 32px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
table.tbl tbody tr:hover { background: var(--bg-raised); }
td.num, th.num { text-align: right; }
.pos { color: var(--profit); } .neg { color: var(--loss); }

/* instruments */
.chart-box { position: relative; height: 260px; }
.chart-caption { margin: 6px 0 0; font-size: 11px; color: var(--text-faint); }
.readout { display: grid; gap: 2px; font-size: 12px; }
.readout-row { display: flex; justify-content: space-between; gap: 12px; }
.readout-row.pos .readout-val { color: var(--profit); } .readout-row.neg .readout-val { color: var(--loss); }
.readout-label { color: var(--text-dim); }

/* kpi row */
.kpi-row { display: flex; }
.kpi { flex: 1; padding: 8px 12px; border-right: 1px solid var(--grid); }
.kpi:last-child { border-right: 0; }
.kpi-label { font-size: 11px; color: var(--text-dim); }
.kpi-value { font-size: 24px; }
.kpi-spark { height: 18px; margin-top: 2px; }

/* rarity left rule */
tr[data-rarity] td:first-child { border-left: 3px solid var(--grid); }
tr[data-rarity="Restricted"] td:first-child { border-left-color: var(--r-restricted); }
tr[data-rarity="Classified"] td:first-child { border-left-color: var(--r-classified); }
tr[data-rarity="Covert"] td:first-child { border-left-color: var(--r-covert); }
tr[data-rarity="Mil-Spec Grade"] td:first-child { border-left-color: var(--r-milspec); }
tr[data-rarity="Extraordinary"] td:first-child { border-left-color: var(--r-gold); }

/* forms */
input, select, textarea { font: inherit; background: var(--bg-raised); color: var(--text); border: 1px solid var(--grid-strong); border-radius: var(--r); padding: 4px 6px; }
button { font: 600 12px "Space Grotesk", sans-serif; letter-spacing: .03em; border-radius: var(--r); border: 1px solid var(--accent); background: var(--accent); color: var(--accent-ink); padding: 5px 12px; cursor: pointer; }
button.secondary { background: var(--bg-raised); color: var(--text); border-color: var(--grid-strong); }
button:disabled { opacity: .45; cursor: not-allowed; }

/* motion */
@keyframes caret { 50% { opacity: 0; } }
.cmd-caret { animation: caret 1s step-end infinite; }
.flash { animation: flash .5s ease-out; }
@keyframes flash { from { color: var(--accent); } }
@media (prefers-reduced-motion: reduce) { .cmd-caret, .flash { animation: none; } }

/* responsive */
@media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } [class*="col-"] { grid-column: 1 / -1; } }

/* legacy screens: keep old markup readable under the new tokens */
.legacy { padding: 12px; }
.legacy h2, .legacy h3 { font-family: "Space Grotesk", sans-serif; color: var(--text); }
.legacy .disclaimer { color: var(--text-faint); font-size: 11px; }
.legacy table { width: 100%; border-collapse: collapse; font-size: 12px; }
.legacy th, .legacy td { padding: 4px 8px; border-bottom: 1px solid var(--grid); text-align: left; }
.legacy .learning-panel { border: 1px solid var(--grid); background: var(--bg-panel); padding: 10px; margin: 8px 0; }
```

- [ ] **Step 2: Write `index.html` — shell only**

```html
<!doctype html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CS2 Trade-Up — Telemetry HUD</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Space+Grotesk:wght@500;600&display=swap">
<link rel="stylesheet" href="/vendor/uplot.min.css">
<link rel="stylesheet" href="/style.css">
<!-- Chart.js pinned + SRI (CDN-compromise guard). Only the not-yet-migrated Lab charts use it;
     drop this tag once legacy.js Lab charts move to uPlot. Generate the hash with:
       curl -sL https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js | openssl dgst -sha384 -binary | openssl base64 -A -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"
        integrity="sha384-REPLACE_WITH_HASH_FROM_COMMENT_ABOVE" crossorigin="anonymous"></script>
<script src="/vendor/uplot.min.js"></script>
</head><body>

(Google Fonts' `css2` stylesheet has no stable SRI — its body varies by requesting UA — so it
stays plain; the font files it pulls are themselves immutable. If offline robustness matters
later, vendor the two `.woff2` files into `vendor/` and drop the Google `<link>`.)
<div class="hud">
  <div class="cmdbar">
    <span class="code" style="color:var(--accent)">&gt;_</span>
    <input class="cmd-input" id="cmd" placeholder="skin, collection, GO view, or action" autocomplete="off">
    <nav class="nav" id="nav"></nav>
    <span style="flex:1"></span>
    <span id="clock" style="color:var(--text-dim);font-size:12px"></span>
  </div>
  <div class="telemetry" id="telemetry"></div>
  <div id="cmd-menu" class="panel" style="display:none;position:absolute;top:40px;left:40px;width:360px;z-index:30"></div>
  <div id="view-root"></div>
</div>
<script type="module" src="/js/app.js"></script>
</body></html>
```

(Chart.js `<script>` stays — `legacy.js` Lab charts still need it. uPlot loaded here too.)

- [ ] **Step 3: Write `web/static/js/app.js` — boot, router, poll**

```js
import { NAV, matchCommand, dispatchCommand } from "./command.js";
import { api } from "./api.js";
import { timeAgo } from "./fmt.js";
import { renderLegacy } from "./views/legacy.js";
import { renderOverview } from "./views/overview.js";
import { renderOpportunities } from "./views/opportunities.js";
import { renderCalculator } from "./views/calculator.js";
import { renderPrice } from "./views/price.js";

const root = document.getElementById("view-root");
const ctx = { collections: [], skins: [] };
window.__hud = { route: () => location.hash.slice(1).split("?")[0] || "overview", ctx, refresh: renderRoute };

// nav
document.getElementById("nav").innerHTML = NAV.map((n) => `<a href="${n.hash}" data-code="${n.code}">${n.code}</a>`).join("");

function renderRoute() {
  const view = window.__hud.route();
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${view}`));
  root.innerHTML = "";
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  if (view === "overview") return renderOverview(root);
  if (view === "opportunities") return renderOpportunities(root, params);
  if (view === "calculator") return renderCalculator(root);
  if (view === "price") return renderPrice(root, params);
  if (["patterns", "lab", "connections"].includes(view)) return renderLegacy(root, view);
  location.hash = "#overview";
}
addEventListener("hashchange", renderRoute);

// command bar
const cmd = document.getElementById("cmd"), menu = document.getElementById("cmd-menu");
let items = [], sel = 0;
function renderMenu() {
  menu.style.display = items.length ? "block" : "none";
  menu.innerHTML = `<div class="panel-body">${items.map((it, i) =>
    `<div class="readout-row ${i === sel ? "flash" : ""}" data-i="${i}" style="cursor:pointer;padding:3px 4px">
       <span>${it.label}</span><span class="readout-label">${it.hint}</span></div>`).join("")}</div>`;
  menu.querySelectorAll("[data-i]").forEach((el) =>
    el.onclick = () => { dispatchCommand(items[+el.dataset.i]); cmd.value = ""; renderMenuReset(); });
}
function renderMenuReset() { items = []; sel = 0; menu.style.display = "none"; }
cmd.addEventListener("input", () => { items = matchCommand(cmd.value, ctx); sel = 0; renderMenu(); });
cmd.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { sel = Math.min(sel + 1, items.length - 1); renderMenu(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); renderMenu(); e.preventDefault(); }
  else if (e.key === "Enter" && items[sel]) { dispatchCommand(items[sel]); cmd.value = ""; renderMenuReset(); }
  else if (e.key === "Escape") { cmd.value = ""; renderMenuReset(); cmd.blur(); }
});
addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== cmd) { cmd.focus(); e.preventDefault(); }
});

// telemetry strip + clock, 3s poll
async function poll() {
  try {
    const [s, net] = await Promise.all([api.status(), api.networkStats()]);
    const scan = s.status || "idle";
    const health = (net.consecutive_rate_limits || 0) > 0 ? "warn" : (net.requests_error && !net.requests_ok) ? "err" : "ok";
    document.getElementById("telemetry").innerHTML =
      `<span><span class="dot ${scan === "running" ? "ok" : scan === "error" ? "err" : ""}"></span>SCAN ${scan}</span>` +
      `<span><span class="dot ${health}"></span>NET ${health === "ok" ? "healthy" : health === "warn" ? "rate-limited" : "errors"}</span>` +
      (net.last_success_at ? `<span>last px ${timeAgo(net.last_success_at)}</span>` : "") +
      (net.cache_size != null ? `<span>${Number(net.cache_size).toLocaleString()} priced</span>` : "") +
      (net.request_interval != null ? `<span>spacing ${net.request_interval}s</span>` : "");
  } catch {}
}
setInterval(poll, 3000); poll();
setInterval(() => { document.getElementById("clock").textContent = new Date().toTimeString().slice(0, 8); }, 1000);

renderRoute();
```

- [ ] **Step 4: Write `web/static/js/views/legacy.js`**

Move the current `#view-patterns`, `#view-ml-lab`, `#view-connections` markup out of the old `index.html` into template strings here, and re-call the existing bind/render functions from the old `web/static/app.js` (which still loads its own way is NOT true anymore — instead) load them:

```js
// The old dashboard's render logic still lives in /app.js (old, ~4450 lines). Rather than
// port it, load it once as a classic script and call the section initialisers it exposes.
// If the old app.js does not already expose them on window, wrap its DOMContentLoaded body
// in `window.__legacyInit = { patterns(){…}, lab(){…}, connections(){…} }` as part of this step.

let loaded = false;
function loadOldApp() {
  if (loaded) return Promise.resolve();
  loaded = true;
  return new Promise((res) => {
    const s = document.createElement("script");
    s.src = "/app.js"; s.onload = res; document.body.appendChild(s);
  });
}

const MARKUP = {
  patterns: `<div class="legacy" id="view-patterns">…original #view-patterns innerHTML…</div>`,
  lab:      `<div class="legacy" id="view-ml-lab">…original #view-ml-lab innerHTML…</div>`,
  connections: `<div class="legacy" id="view-connections">…original #view-connections innerHTML…</div>`,
};

export async function renderLegacy(root, view) {
  root.innerHTML = MARKUP[view];
  await loadOldApp();
  window.__legacyInit?.[view]?.();
}
```

**Concretely:** copy the three `<div data-view=…>` blocks from git history (`git show HEAD:web/static/index.html`) into `MARKUP` verbatim, drop the `data-view`/`hidden` attributes, add `class="legacy"`. Then in the **old** `web/static/app.js`, find its `DOMContentLoaded` handler, and split the per-section setup calls (the ones that populate `#patterns-*`, `#son-*`, `#csfloat-*`, etc.) into `window.__legacyInit = { patterns, lab, connections }`. Leave everything else in old `app.js` intact.

- [ ] **Step 5: Verify the shell boots**

Start the `dashboard` config. Load `http://localhost:5000/`.
- `read_console_messages` → **zero errors**.
- `read_page` → command input, nav with 7 codes, telemetry strip with SCAN/NET, `#view-root` showing the Overview placeholder (empty until Task 7 — acceptable; it must not error).
- Click `PAT` → the Rare Patterns section renders, its "Start scanning" button is present, `read_console_messages` still clean.
- Click `LAB` → agent table + radar canvas present. Click `CONN` → the four connection panels present.
- Press `/` → focus jumps to the command input. Type `opps` → menu shows an OPPS row. `Esc` → menu clears.
- Screenshot the shell with LAB open.

- [ ] **Step 6: Commit**

```bash
git add web/static/style.css web/static/index.html web/static/js/app.js web/static/js/views/legacy.js web/static/app.js
git commit -m "feat(ui): telemetry-HUD shell + command bar + legacy view host"
```

---

## Task 7: OVW — Overview screen

**Files:**
- Create: `web/static/js/views/overview.js`
- Reference: `web/static/js/charts.js` (`heatmap`, `sparkline`, `caption`), `api.js`, `fmt.js`.

**Interfaces:**
- Consumes: `api.status`, `api.outcomesStats`, `api.simStatus`, `api.bestSimulations`, `api.leaderboards`, `api.connections`; `heatmap`, `sparkline`.
- Produces: `renderOverview(root) -> void` — builds a `.grid` of panels, starts its own 3 s interval, clears it on the next `hashchange`.
  - `buildDistMatrix(bestSims) -> { rows, cols, values }` — rows = distinct collections (top 8 by count), cols = the 8 ROI buckets from spec §5.4, `values[r][c]` = candidate count. Exported for reuse by OPPS.

- [ ] **Step 1: Implement `buildDistMatrix` + a selftest assertion**

Append to `charts-selftest.html`:

```html
<script type="module">
import { buildDistMatrix } from "./js/views/overview.js";
const eq = window.__eq;
const sims = [
  { collection: "A", roi_pct: 10 }, { collection: "A", roi_pct: 120 }, { collection: "B", roi_pct: -30 },
];
const m = buildDistMatrix(sims);
eq(m.cols.length, 8, "dist matrix has 8 ROI buckets");
eq(m.values[m.rows.indexOf("A")][2], 1, "A has 1 candidate in [0,25) bucket");
</script>
```

Bucket edges: `[-Infinity,-25,0,25,50,100,200,400,Infinity]` → 8 buckets. `[0,25)` is index 2.

- [ ] **Step 2: Verify fail** — `Failed to resolve module ./js/views/overview.js`.

- [ ] **Step 3: Implement `web/static/js/views/overview.js`**

Build these panels into `root` (as `.grid` → `.panel` with `.panel-head` + `.panel-body`):

1. **`OVW · board`** (`col-12`) — a `.kpi-row` of six `.kpi` cells: Best live ROI (`outcomesStats` / `simStatus`), Avg ROI best finds, Valid/invalid trials, Market confidence, Learning progress, Trade-ups analyzed. Each cell: `.kpi-label`, `.kpi-value` (Space Grotesk, `pos`/`neg` class by sign), and a `.kpi-spark` div fed to `sparkline()` when a recent series is available (`simStatus` round history tails; skip the spark if absent). Wrap each `.kpi-value` update in a `.flash` toggle when the value changed since last poll.
2. **`DIST · outcome distribution`** (`col-8`) — a `<canvas>` in a `.chart-box`; `heatmap(canvas, { rows, cols, values: counts, colorFor: rampSlateToAccent, fmt: String, onHover: (r,c) => crosshairReadout(panel, [...]), onClick: (r,c) => location.hash = "#opportunities?collection="+enc(rows[r]) })`. `caption(panel, "How many trade-ups the agents have found in each collection, by how good they are. Bright = crowded. Click a cell to open those in OPPS.")`.
3. **`OPPS · top 5`** (`col-4`) — a `table.tbl`: Collection · ROI% · Cost, top 5 of `bestSimulations` by composite/ROI, each `<tr data-rarity>` for the left rule, row-click → `#opportunities`.
4. **`CONN · status`** (`col-12`) — three inline chips from `api.connections`: CSFloat / GitHub backup / NTFY, each `● label: state`, click → `#connections`.

`rampSlateToAccent(v, min, max)`: `t = (v-min)/(max-min||1)` → `` `color-mix(in srgb, var(--accent) ${Math.round(t*100)}%, var(--bg-panel))` ``.

- [ ] **Step 4: Verify in preview**

Load `#overview`. `read_console_messages` clean. `read_page`: six KPI cells with numbers or `—`, the DIST canvas present, top-5 table populated (or an empty-state line if the sim population is cold). Hover a heatmap cell → the readout list updates. Click a cell → route changes to `#opportunities?collection=…`. Screenshot.

- [ ] **Step 5: Commit**

```bash
git add web/static/js/views/overview.js web/static/charts-selftest.html
git commit -m "feat(ui): OVW overview — KPI board, DIST heatmap, top-5, connections"
```

---

## Task 8: FLOAT instrument (shared component)

Built before OPPS/CALC because both embed it.

**Files:**
- Create: `web/static/js/views/float-instrument.js`
- Modify: `charts-selftest.html`

**Interfaces:**
- Consumes: global `uPlot`; `fmt.js` (`WEAR_BANDS`, `wearOfFloat`, `money`, `float`); `charts.js` (`uplotTheme`, `crosshairReadout`, `caption`).
- Produces: `mountFloatInstrument(el, opts) -> { update(opts), destroy() }` where
  `opts = { pricesByWear: {FN?,MW?,FT?,WW?,BS?}, floatCap: {min,max}, marker?: number, ghosts?: number[], resolvedPoints?: [{float, price}] }`.
  Renders a step line across the 5 bracket-midpoint prices, greys the `< floatCap.min` / `> floatCap.max` x-range, draws `marker` as a solid `--accent` vertical + each `ghosts` value as a faint vertical, plots `resolvedPoints`, and on cursor-move writes `[{label:"float", value}, {label:"wear", value}, {label:"~price", value}]` via `crosshairReadout`.
  - `stepSeriesFromWears(pricesByWear) -> { xs:number[], ys:number[] }` — pure; xs = the 5 midpoints present, ys = their prices, sorted by xs. Exported for selftest.

- [ ] **Step 1: Selftest for `stepSeriesFromWears`**

```html
<script type="module">
import { stepSeriesFromWears } from "./js/views/float-instrument.js";
const eq = window.__eq;
const s = stepSeriesFromWears({ FT: 0.42, FN: 1.10, MW: 0.71 });
eq(s.xs, [0.035, 0.11, 0.265], "xs are FN,MW,FT midpoints sorted");
eq(s.ys, [1.10, 0.71, 0.42], "ys track xs order");
</script>
```

- [ ] **Step 2: Verify fail.**

- [ ] **Step 3: Implement.** `mountFloatInstrument` creates a uPlot with `...uplotTheme()`, one stepped series (`paths: uPlot.paths.stepped({ align: 1 })`), an x-scale fixed to `[0,1]`, plus a `hooks.setCursor` that maps `u.cursor.left` → float → `wearOfFloat` → interpolated price and calls `crosshairReadout`. The greyed cap zone + verticals are drawn in a `hooks.draw` via `u.ctx` rectangles/lines at `u.valToPos(...)`. `update()` calls `u.setData(...)` and re-reads `opts` for markers. `caption(el, "Where the output's value jumps as its float changes. Your input mix's average float is the blue line.")`.

- [ ] **Step 4: Verify pass** (selftest) + a quick visual: temporarily mount it in `charts-selftest.html` with `{ pricesByWear:{FN:1.1,MW:.71,FT:.42,WW:.4,BS:.38}, floatCap:{min:0,max:.8}, marker:.27 }`, confirm a stepped line + one blue vertical render with no console error. Remove the temp mount before commit.

- [ ] **Step 5: Commit**

```bash
git add web/static/js/views/float-instrument.js web/static/charts-selftest.html
git commit -m "feat(ui): FLOAT instrument — float-vs-price step curve with wear bands"
```

---

## Task 9: OPPS — Opportunities screen

**Files:**
- Create: `web/static/js/views/opportunities.js`
- Reference: `overview.js` (`buildDistMatrix`), `float-instrument.js`, `charts.js`, `api.js`, `fmt.js`.

**Interfaces:**
- Consumes: `api.bestSimulations`, `api.archive`, `api.leaderboards`, `api.patternsOpps`; `buildDistMatrix` + `heatmap`; `mountFloatInstrument`.
- Produces: `renderOpportunities(root, params) -> void`. Reads `params.get("collection")` to pre-fill the collection filter.
  - `trustSegments(row) -> [{key, v01}]` — `composite`, `confidence`, `dead_skin` (inverted: `1 - score/100`), `price_basis` (`cache` = 1, `stale` = 0.4). Pure; exported for selftest.

- [ ] **Step 1: Selftest `trustSegments`**

```html
<script type="module">
import { trustSegments } from "./js/views/opportunities.js";
const eq = window.__eq;
const seg = trustSegments({ composite_score: 0.8, confidence: 70, dead_skin_score: 40, price_basis: "cache" });
eq(seg.find((s) => s.key === "dead_skin").v01, 0.6, "dead_skin 40 => 0.6 trust");
eq(seg.find((s) => s.key === "price_basis").v01, 1, "cache => 1");
</script>
```

- [ ] **Step 2: Verify fail.**

- [ ] **Step 3: Implement `web/static/js/views/opportunities.js`** — panels into `root`:

1. **`DIST · outcome distribution`** (`col-12`) — full-width `heatmap` from `buildDistMatrix(bestSims)`; `onClick(r,c)` sets the table's collection filter and re-renders.
2. **`OPPS · trade-ups`** (`col-8`) — sticky filter row (`min ROI` number, `risk` select any/low/med/high, `agent` select) then a `table.tbl`: cols `▸` · Cost · ROI% · TRUST. `<tr data-rarity="<to_rarity>">`. ROI cell `pos`/`neg`. TRUST cell = a 4-segment micro-bar: `<span class="tbar"><i style="flex:${v01}"></i>…</span>` (add `.tbar{display:flex;gap:1px;height:6px;width:56px}` `.tbar i{background:var(--accent)}` to `style.css` in this task). Row `▸` click → expand a detail `<tr>`:
   - per-input price cells (from `row.inputs`),
   - `mountFloatInstrument` for the output (`pricesByWear` from `row.output_prices_by_wear`, `floatCap` from `row.output_float_cap`, `marker` = `row.avg_input_float`),
   - **three ROI figures side by side**: `formula` (`row.roi_pct`), `ML` (`row.ml_predicted_roi_pct` or `—`), `verified` (`row.verified_roi_pct` or `—`) — each a `.kpi` cell,
   - a `mark done / blacklist` button → `POST /api/sim-population/blacklist` (reuse the legacy payload shape; read it from old `app.js`).
3. **`LB · profitable collections`** (`col-4`) and **`LB · profitable skins`** (`col-4`) — two `table.tbl` from `api.leaderboards`.
4. **`ARCH · full archive`** (`col-12`, collapsed by default) — paginated `table.tbl` from `api.archive({ collection, min_roi, agent, page, page_size })`; Prev/Next buttons; only fetches when expanded.
5. **`COLL · collector arbitrage`** (`col-12`) — `table.tbl` from `api.patternsOpps()` (Item · Pattern · Ask · Est value · Est profit · Confidence · Listing link).

- [ ] **Step 4: Verify in preview**

Load `#opportunities`. Console clean. Table populated (or explicit empty-state). Expand a row → FLOAT instrument renders, three ROI cells show, no error. Set `min ROI = 50` → rows filter. Load `#opportunities?collection=Recoil` → filter pre-filled. Click a DIST cell → filter changes. Screenshot an expanded row.

- [ ] **Step 5: Commit**

```bash
git add web/static/js/views/opportunities.js web/static/style.css web/static/charts-selftest.html
git commit -m "feat(ui): OPPS — ranked table, TRUST bar, row-expand w/ FLOAT + 3 ROI figures"
```

---

## Task 10: CALC — Calculator screen

**Files:**
- Create: `web/static/js/views/calculator.js`
- Reference: `float-instrument.js`, `api.js`, `fmt.js`.

**Interfaces:**
- Consumes: `api.constants`, `api.currencies`, `api.status`, `api.setConfig`, `api.scanNow/pause/resume/stop`, `api.manualTradeup`, `api.tradeups`, `api.covert`, `api.anomalies`, `api.skins`; `mountFloatInstrument`.
- Produces: `renderCalculator(root) -> void`.
  - `landingOdds(inputFloats, outputCap) -> {FN,MW,FT,WW,BS}` — pure: average normalised input float → output range → wear bucket probabilities (single deterministic bucket = 1.0 when floats are exact; keep the signature so a future distribution can fill it). Exported for selftest.

- [ ] **Step 1: Selftest `landingOdds`**

```html
<script type="module">
import { landingOdds } from "./js/views/calculator.js";
const eq = window.__eq;
const o = landingOdds([0.05, 0.05, 0.05], { min: 0, max: 1 });
eq(o.FN, 1, "avg float 0.05 => lands FN");
</script>
```

- [ ] **Step 2: Verify fail.**

- [ ] **Step 3: Implement** — panels:

1. **`CALC · scan settings`** (`col-6`) — a 2-col form built from the old `#config-form` fields (collection, currency, min ROI, StatTrak, fast mode, skip-anomalies, float-mix, mixed-filler, strategy-any-roi, check-thirdparty, use-ml, gap, volume, scan-covert, case). Submit → `api.setConfig` then `api.scanNow`.
2. **`SCAN · progress`** (`col-6`) — from `api.status` on the 3 s poll: a `.tbar`-style progress bar + `phase`, `done/total`, computed `items/s` and ETA; Scan / Pause / Resume / Stop buttons wired to the matching `api.*`.
3. **`BUILD · manual trade-up`** (`col-12`) — skin autocomplete (`api.skins`), wear select, count; an **`#contract-strip`** of 10 cells that fill as inputs are added (`<i>` per unit, `--accent` border when filled); the **FLOAT instrument** (`mountFloatInstrument`) updating `marker` to the current average input float and `ghosts` to nothing for now; a **landing-odds bar** — a 100%-stacked horizontal bar of `landingOdds(...)` (FN/MW/FT/WW/BS segments, each labelled). `Compute` (enabled at 10/10) → `api.manualTradeup` → a `.kpi-row` result: Cost · EV · After fee · Profit · ROI%.
4. **`OPPS · trade-ups`**, **`COV · covert`**, **`ANOM · underpriced listings`** (`col-12` each) — `table.tbl` from `api.tradeups` / `api.covert` / `api.anomalies`, same columns as today, restyled.

- [ ] **Step 4: Verify in preview**

Load `#calculator`. Console clean. Add a skin ×10 → contract strip fills, FLOAT marker moves, landing-odds bar updates. `Compute` → result KPI row populates (or a clear error if prices are missing). Change a scan setting + Apply → `read_network_requests` shows `POST /api/config` then `/api/scan-now`. Screenshot the manual builder at 10/10.

- [ ] **Step 5: Commit**

```bash
git add web/static/js/views/calculator.js web/static/charts-selftest.html
git commit -m "feat(ui): CALC — scan form, progress instrument, manual builder w/ FLOAT + landing odds"
```

---

## Task 11: PRICE instrument screen

**Files:**
- Create: `web/static/js/views/price.js`
- Delete: `web/static/app.js` (old) — only if Task 6's `legacy.js` was wired to expose `__legacyInit` from a **copy** of the needed code; if `legacy.js` still `<script src="/app.js">`s it, keep the file and note that in the commit. (Decide during Task 6; this step just records the intent.)

**Interfaces:**
- Consumes: `api.priceHistory`, `api.currencies`; `charts.js` (`uplotTheme`, `crosshairReadout`, `caption`); `fmt.js` (`WEAR_BANDS`, `money`, `pct`, `float`); global `uPlot`.
- Produces: `renderPrice(root, params) -> void`. Reads `params.get("skin")`; splits a `"Name (Field-Tested)"` string into `{ skin, wear }` when present.
  - `trendExtrapolate(points, hoursAhead = 24) -> { slope, intercept, at(ts) }` — least-squares fit on `[{t, v}]`; `null` when `points.length < MIN` (MIN = 8). Exported for selftest.

- [ ] **Step 1: Selftest `trendExtrapolate`**

```html
<script type="module">
import { trendExtrapolate } from "./js/views/price.js";
const eq = window.__eq;
const pts = Array.from({ length: 10 }, (_, i) => ({ t: i, v: 2 * i + 1 }));
const f = trendExtrapolate(pts);
eq(Math.round(f.at(20)), 41, "linear fit y=2x+1 extrapolates to 41 at x=20");
eq(trendExtrapolate(pts.slice(0, 5)), null, "fewer than 8 points => null");
</script>
```

- [ ] **Step 2: Verify fail.**

- [ ] **Step 3: Implement** — a single **`PRICE · <skin>`** panel (`col-12`):
  - a lookup form (skin autocomplete, wear select, StatTrak, currency) — submitting sets `#price?skin=…&wear=…`;
  - a `.chart-box` uPlot: x = time, one series per wear bracket present in the response (`FN/MW/FT/WW/BS`), StatTrak variant dashed, series stroke = a desaturated tint per wear;
  - `uplotTheme()` cursor = the vertical crosshair; `hooks.setCursor` → `crosshairReadout(panel, rows)` with one row per series (`FT  $0.42  Δ24h −3.1%  float≈0.27` — Δ from the value 24 h before the cursor, `float≈` = `WEAR_BANDS` midpoint) plus a `trend` row from `trendExtrapolate` (drawn as a ghost dashed segment past the last timestamp);
  - a **wear-band strip** under the plot: a 40 px flex row of 5 divs tinted per band across float 0→1, each with a tick showing where that series' bracket midpoint sits;
  - empty state (fewer than MIN points, or 404): the readout shows *"Look up a skin to start recording. Steam has no price-history API — this is not retroactive."* and no axes are drawn.
  - `caption(panel, "Every price this tool has recorded for this skin since you first looked it up.")`

- [ ] **Step 4: Verify in preview**

Load `#price?skin=AK-47 | Redline (Field-Tested)`. Console clean. If history exists: multi-series lines + moving crosshair readout. If not: the empty-state text, no broken chart. From the command bar, type `ak redline` and Enter → routes here. Screenshot.

- [ ] **Step 5: Commit**

```bash
git add web/static/js/views/price.js
git commit -m "feat(ui): PRICE instrument — multi-wear time series w/ crosshair readout + trend"
```

---

## Task 12: Verification pass, cleanup, README

**Files:**
- Modify: `README.md` (a short "Dashboard (telemetry HUD)" subsection)
- Modify: `web/static/charts-selftest.html` (final: ensure it still ends `SELFTEST OK`)
- Possibly delete: `web/static/app.js` old, `web/static/style.css` has no old remnants.

- [ ] **Step 1: Full selftest** — load `/charts-selftest.html` with the server up. Expected final line: `SELFTEST OK (N pass, 0 fail)`. Fix anything red.

- [ ] **Step 2: Route sweep** — for each of `#overview #opportunities #calculator #price #patterns #lab #connections`:
  - `read_console_messages` → zero errors,
  - `read_page` → the view's key controls are present,
  - resize to 900 px (`resize_window`) → panels stack to one column, still readable.
  Record a screenshot of `#overview`, an expanded `#opportunities` row, and the `#calculator` manual builder.

- [ ] **Step 3: Legacy regression** — on `#lab`: start/stop one agent, confirm the request fires (`read_network_requests`). On `#connections`: the CSFloat + Steam-session + backup + NTFY forms all render and accept input. On `#patterns`: "Start scanning" toggles status.

- [ ] **Step 4: Reduced-motion + focus** — with `prefers-reduced-motion` emulated: the command caret does not blink, no digit-flash. Tab through the command bar, nav, a table's filter row → focus ring visible at every stop.

- [ ] **Step 5: README note**

Add under the dashboard section of `README.md`:

```markdown
### Dashboard (telemetry HUD)

`web/static/` is a no-build, native-ES-module app served by `web/server.py`.
Entry: `index.html` → `js/app.js` (hash router). Screens: `js/views/*.js`.
Charts: vendored uPlot (`vendor/`) + a canvas heatmap in `js/charts.js`.
Overview / Opportunities / Calculator / Price are the rebuilt screens; Patterns /
Lab / Connections still run the previous code via `js/views/legacy.js`.
Logic self-check: open `/charts-selftest.html` — it prints `SELFTEST OK`.
Command bar: press `/`, type a skin / collection / `GO <view>` / `scan|verify top|train`.
```

- [ ] **Step 6: Commit**

```bash
git add README.md web/static/charts-selftest.html
git commit -m "docs(ui): telemetry-HUD dashboard notes + final selftest"
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Task(s) |
|---|---|
| §3.1 tokens | 6 (verbatim block) |
| §3.2 type / fonts | 6 (`index.html` links, `style.css` faces) |
| §3.3 density, responsive | 6 (media query), 12 (resize check) |
| §3.4 motion + reduced-motion | 6 (`@keyframes`, `@media`), 12 (check) |
| §4 shell / telemetry strip | 6 |
| §4.1 command bar | 5 (matcher) + 6 (UI + keys) |
| §5.1 charts infra | 3 |
| §5.2 PRICE instrument | 11 |
| §5.3 FLOAT instrument | 8 |
| §5.4 DIST heatmap | 7 (`buildDistMatrix` + render) + 9 (full-width) |
| §6.1 OVW | 7 |
| §6.2 OPPS (rarity rule, TRUST bar, 3 ROI, leaderboards, archive, collector) | 9 |
| §6.3 CALC (form, progress, 10-cell strip, landing odds, tables) | 10 |
| §7 file plan | matches "File Structure" above |
| §7.2 server change | 1 |
| §7.3 fonts/vendor/Chart.js | 1, 6 |
| §8 testing | selftest built up in 2/3/4/5/7/8/9/10/11; preview checks each view; 12 sweep |
| §9 risks (legacy verbatim, float≈ labelling, empty states, focus/motion) | 6 (legacy), 8/11 (`≈`/empty), 12 (focus/motion) |

No uncovered spec requirement.

**2. Placeholder scan** — the only intentionally-deferred spots are `legacy.js` `MARKUP` ("copy the three blocks from `git show HEAD:web/static/index.html`") and the Task 11 delete decision, both with an explicit concrete instruction, not a vague "handle it". Acceptable. No "TBD" / "add error handling" / bare "write tests".

**3. Type consistency** — `renderOverview(root)`, `renderOpportunities(root, params)`, `renderCalculator(root)`, `renderPrice(root, params)`, `renderLegacy(root, view)` used identically in `app.js` and their task headers. `mountFloatInstrument(el, opts) -> {update, destroy}` consistent between Tasks 8, 9, 10. `heatmap(canvas, opts) -> {destroy, hitTest}` consistent between Tasks 3, 7, 9. `buildDistMatrix` produced in 7, consumed in 9. `matchCommand(query, ctx)` / `dispatchCommand(item)` consistent between 5 and 6.
