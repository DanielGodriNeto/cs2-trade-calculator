# Telemetry-HUD UI/UX Redesign — Design Spec

**Date:** 2026-08-29
**Status:** Approved direction, pending spec review
**Scope:** Complete visual + interaction redesign of the dashboard served from `web/static/`,
delivered as a rewrite-in-place (no build step). Priority screens rebuilt now; remaining
screens reskinned now and rebuilt in a later pass.

---

## 1. Goal

Turn the dashboard from a flat, undifferentiated wall of text and decorative charts into a
**quant-desk instrument**: a dense, keyboard-driven telemetry HUD where charts are technical
instruments (crosshair scrubbers, probability heatmaps) and the operator reads many
interdependent numbers fast and acts.

Reference points named in the brief: Bloomberg Terminal, TradingView, Grafana — for the
*concept* (charts as instruments), not a literal skin of any one.

### Non-goals

- No framework, no npm, no bundler. Native ES modules only.
- No light theme (dark slate only, matching the current app and the brief).
- Not a mobile product. It is a local desktop tool; it must stay usable but not mobile-first.
- Not a backend redesign. `web/server.py` changes only to serve two new static dirs.
- Not rewriting all ~4,450 lines of the current `app.js`. API-call logic is lifted and reused.

---

## 2. Decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| Foundation | Rewrite `web/static/` in place, native ES modules | User choice. Keeps deploy = server serves static files. |
| IA scope | Priority subset first: Overview, Opportunities, Calculator + flagship charts | User choice. The undifferentiated density is the core UX problem; fix the high-traffic screens first. |
| Chart engine | uPlot (vendored) + a small custom canvas heatmap | User choice. uPlot is framework-agnostic, ~45KB, native crosshair cursor, built for high-density time series. |
| Visual approach | "Trading desk" — command bar signature, hairline panel grid, 4-letter panel codes | Stands out without fighting the brief's stated slate palette. |
| UI accent | Mil-Spec blue `#5C7CFF` | Frees green/red to mean **only** profit/loss. Current app dilutes green by using it for chrome + P&L. |
| Data color | CS2 rarity ramp, semantic use only | The real color language of the domain; disciplined (row edge rules, wear bands, badges) — never chrome. |
| Signature element | The `>_` command bar | Fastest way for one operator to work one huge dataset. Everything else stays quiet. |

---

## 3. Visual system

### 3.1 Tokens (`web/static/style.css` `:root`)

```
/* surfaces */
--bg:          #0B0F14
--bg-panel:    #10151C
--bg-raised:   #161C25   /* hover row, input fill, active */
--grid:        #1F242C   /* every hairline: panel borders, chart gridlines, table rules */
--grid-strong: #2C333E   /* focused input border, section dividers */

/* text */
--text:        #D7DEE8
--text-dim:    #8A94A3   /* labels, axis ticks, secondary */
--text-faint:  #5A6472   /* captions, disabled, watermark */

/* the one UI accent */
--accent:      #5C7CFF
--accent-ink:  #06080D
--accent-dim:  #2B3B7A

/* P&L and status — used nowhere else */
--profit:      #35C88A
--loss:        #F2555A
--warn:        #E8B341   /* drought, rate-limit, stale price */

/* rarity ramp — data-only (CS2 canon, nudged for slate contrast) */
--r-consumer:   #B0C3D9
--r-industrial: #5E98D9
--r-milspec:    #4B69FF
--r-restricted: #8847FF
--r-classified: #D32CE6
--r-covert:     #EB4B4B
--r-gold:       #FFD700

/* geometry */
--r:      3px           /* panels, inputs */
--r-pill: 999px         /* status chips */
/* panel grid uses gap:1px over a --grid background: the gap IS the gridline */
```

### 3.2 Type

| Role | Face | Weight | Treatment | Applied to |
|---|---|---|---|---|
| Display | Space Grotesk | 600 | uppercase, `letter-spacing: 0.04em` | panel codes, KPI numbers, view headers |
| Data / body | JetBrains Mono | 400 / 500 | `font-variant-numeric: tabular-nums` always | every number, table cell, form field, command bar, prose |

Fallback stacks:
- Display: `"Space Grotesk", Inter, system-ui, sans-serif`
- Mono: `"JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace`
  (mono fallbacks ship on Windows -> readable before Google Fonts resolves)

Type scale (px): `11 / 12 / 13 / 15 / 18 / 24 / 34`. Base 13 prose, 12 tables, 11 axis
ticks + captions. Line-height 1.35.

Fonts loaded via `<link>` to Google Fonts (`fonts.googleapis.com` / `fonts.gstatic.com`).

### 3.3 Density rules

- Panels butt together on the 1px hairline grid. No floating cards in whitespace.
- Table rows 32px. Full-bleed width (this is a desk, not a landing page).
- Below 1100px: panel grid collapses to one column. Below 640px: degraded-but-usable.

### 3.4 Motion (deliberate, minimal)

- Command-bar caret blink.
- Crosshair readouts update with **no** transition (instruments don't ease).
- One 120ms opacity fade when a panel first receives data; nothing on the 3s poll.
- Brief accent flash on a changed digit group.
- `prefers-reduced-motion`: kills the flash and the blink.

---

## 4. App shell

Sticky top, two rows, then a full-width scrolling panel grid.

```
+----------------------------------------------------------------------------------+
| >_ ak redline ft|          [ OVW  OPPS  CALC  PAT  LAB  CONN ]      14:22 . b3f1a |  row 1
+----------------------------------------------------------------------------------+
| * SCAN idle   * NET healthy   last px 2m ago   8,412 priced   spacing 1.5s   ! - |  row 2
+=====================+=====================+======================================+
| OVW - overview      |  (KPI cells)        | DIST - outcome distribution           |
|                     |                     |                                      |
+---------------------+---------------------+                                      |
| top 5 opportunities             (mini tbl)|                                      |
+===========================================+======================================+
```

- **Row 1:** command input (left) . nav as 4-letter codes . clock + short build hash.
- **Row 2:** telemetry strip — the current top-bar stats, restyled. Same data
  (`/api/status`, `/api/network-stats`). Dot color = status (accent/warn/loss).
- **Panel grid:** CSS grid, up to 12 cols, `gap: 1px` over a `--grid` background.
  Each panel = 22px title bar (`CODE - full name` + optional right-aligned control) then body.
  Panels span 12 / 6 / 4 / 3 cols; no outer margins.

### 4.1 Command bar (`web/static/js/command.js`) — the signature

Persistent `>_` input, monospace, blinking accent caret. Typing opens an omnibar dropdown
matching across a static index:

| Input | Target |
|---|---|
| skin name (`ak redline ft`) | open PRICE instrument for that skin/wear |
| collection name | jump to OPPS filtered to that collection |
| `go opps` / `go calc` / ... | switch view |
| `scan` / `verify top` / `train` | fire the matching POST action (with a confirm for destructive ones) |

- `/` focuses it from anywhere; `Esc` clears and blurs.
- Index built from: nav list (static), `/api/skins?q=` (debounced live), collection list from
  loaded opportunities, a hardcoded action list.
- No fuzzy-search lib — simple case-insensitive substring + token match is enough.

---

## 5. Chart instruments

### 5.1 Shared infra (`web/static/js/charts.js`)

- `uplotTheme()` -> uPlot opts fragment from CSS vars: `--grid` gridlines, `--text-dim`
  ticks, mono tick font, accent vertical-only cursor (1px).
- `crosshairReadout(panelEl, series[])` -> renders/updates a **left-docked readout list**
  (not a floating tooltip). Driven from uPlot's `setCursor` hook.
- `heatmap(canvas, {rows, cols, values, colorFor, fmt, onHover, onClick})` -> ~80 lines of
  canvas. Grid of cells, 1px `--grid` separators, hover highlights row+col+cell and fires
  `onHover`, click fires `onClick`. Renders a color-legend strip.
- `sparkline(el, values, {w, h})` -> tiny uPlot, no axes/legend/cursor. For KPI trend chips.
- `caption(el, text)` -> appends `<p class="chart-caption">` in plain language. Every chart
  gets one (addresses the standing "the graphs don't make sense" complaint).

### 5.2 Instrument 1 — PRICE  (Time x Price, multi-wear, synced crosshair)

- **Where:** own panel; opened from the command bar or an OPPS row expand. Replaces the
  current `#price-history-section`.
- **Data:** `GET /api/price-history?skin=&wear=&stattrak=&currency=` (exists). One uPlot
  series per wear bracket with data (FN/MW/FT/WW/BS). StatTrak variant = dashed. Series
  color = desaturated tint keyed to wear position.
- **Crosshair:** uPlot native cursor, vertical only. `setCursor` hook updates the
  left-docked readout: per series -> `FT   $0.42   d24h -3.1%   float~0.27`
  (wear->float = bracket midpoint, per the app's own methodology). Plus a **trend row**:
  the existing naive linear-regression extrapolation as a ghost segment past "now",
  readout shows the 24h-ahead figure.
- **Wear-band strip:** 40px element under the plot — horizontal float axis 0->1, the five
  wear bands tinted, a tick per series at its bracket midpoint. Ties wear <-> float <-> price.
- **Empty state** (below min history points): readout shows plain text —
  "Look up a skin to start recording. Steam has no price-history API — this is not
  retroactive." No broken axes.
- **Caption:** "Every price this tool has recorded for this skin since you first looked it
  up."

### 5.3 Instrument 2 — FLOAT  (Float x Price step curve with wear bands)

- **Where:** inside an OPPS row expansion (output skin) and live in the CALC manual builder.
  The CS2-specific instrument — nothing generic has it.
- **Data:** the five per-wear median prices already in each trade-up / skin payload
  (`/api/tradeups`, `/api/manual-tradeup`, `/api/sim-population/best-simulations`).
  X = float 0.000->1.000, Y = price. Step line across the five bracket midpoints. The
  specific skin's min/max float cap -> out-of-range zone hatched/greyed. CSFloat-resolved
  real listings (when present) plotted as points.
- **Crosshair:** hover/drag a marker along X -> readout
  `float 0.162 -> Minimal Wear -> ~$0.71`. In the manual builder: current 10-input **average**
  float = solid accent vertical; each optimizer-proposed wear-mix = a ghost vertical.
- **Caption:** "Where the output's value jumps as its float changes. Your input mix's
  average float is the blue line."

### 5.4 Instrument 3 — DIST  (2D outcome heatmap, collection x ROI-bucket)

- **Where:** OVW (compact, ~6 rows) and a full panel on OPPS. This is the brief's
  "2D probability heatmap showing outcome distributions across input weapon collections".
- **Data:** `/api/sim-population/best-simulations` + `/api/sim-population/opportunities/archive`.
  Rows = weapon collection. Cols = ROI buckets
  `[-inf,-25) [-25,0) [0,25) [25,50) [50,100) [100,200) [200,400) [400,inf)`.
  Cell value = candidate count (toggle: sum of expected profit). Color = single-hue
  slate->accent ramp for counts; diverging loss<->profit ramp for signed profit.
- **Interaction:** hover cell -> readout
  `Dreams & Nightmares x [+100,+200)%  .  34 candidates  .  best +187%  .  median liq 0.4`.
  Click cell -> filters the OPPS table to those rows. Row sort: best ROI / count / total
  profit. Second toggle: cols = output wear (FN..BS), cell = mean ROI.
- **Caption:** "How many trade-ups the agents have found in each collection, by how good
  they are. Bright = crowded. Click a cell to see those trade-ups."

### 5.5 Chart.js

Stays loaded only for the not-yet-migrated Lab charts. Removed when those migrate.

---

## 6. Priority screens

### 6.1 OVW — Overview

- **KPI readout row** (replaces `.kpi-grid` cards): one hairline-divided strip, six cells —
  Best live ROI, Avg ROI (best finds), Valid/invalid trials, Market confidence, Learning
  progress, Trade-ups analyzed. Each cell = Space Grotesk number (24px) + 11px label +
  `sparkline()` where the data allows.
- **DIST heatmap** (compact) left . **Top 5 opportunities** mini-table right.
- **Connections** = three inline status chips (CSFloat / GitHub backup / NTFY); click -> CONN.
- **Data:** `/api/status`, `/api/outcomes/stats`, `/api/sim-population/status`,
  `/api/sim-population/leaderboards`, `/api/connections/status`. Poll 3s.

### 6.2 OPPS — Opportunities

- **Ranked trade-up table**, restyled: 32px rows, `tabular-nums`, **3px rarity left-rule**
  per row = target tier. `TRUST` column -> **4-segment micro-bar**
  (composite / confidence / dead-skin / price-basis), not hover-only text.
- **Sticky filter row:** min ROI, risk level, agent — inline mono inputs.
- **Row expand:** per-input price cells, the **FLOAT instrument** for the output,
  formula ROI + ML ROI + verified ROI shown together (backlog item), "mark done / blacklist".
- **Side panels:** Most-profitable collections . Most-profitable skins.
- **Archive:** paginated panel (unchanged behaviour). **Collector-arbitrage** table kept.
- **Full DIST heatmap panel** at top.
- **Data:** `/api/sim-population/best-simulations`, `/opportunities/archive`,
  `/leaderboards`, `/api/patterns/opportunities`.

### 6.3 CALC — Calculator

- **Scan settings:** tight 2-col mono form (all existing `#config-form` fields).
- **Scan progress:** progress instrument — bar + items/s + ETA, from `/api/status`.
- **Manual trade-up builder:** contract as a **10-cell strip** that fills as inputs are
  added; **FLOAT instrument** live with the input-float average; **landing odds**
  (FN/MW/FT/WW/BS) as a horizontal 100%-stacked bar. Result = KPI readout row
  (cost / EV / after-fee / profit / ROI).
- **Trade-up + covert + underpriced-listings tables:** restyled, same columns.
- **Data:** `/api/config`, `/api/scan-now|pause|resume|stop`, `/api/manual-tradeup`,
  `/api/tradeups`, `/api/covert`, `/api/anomalies`, `/api/skins`.

---

## 7. File / module plan (no build step)

```
web/static/
  index.html         rewritten: shell markup + <script type="module" src="/js/app.js">
  style.css          rewritten: tokens, shell, panels, tables, instruments, .legacy scope
  vendor/
    uplot.min.js      vendored (~45KB) — no CDN dependency for the UI
    uplot.min.css
  js/
    app.js           boot, hashchange view router, poll loop, global keys (/ , Esc)
    api.js           fetch wrappers over /api/* (lifted from current app.js)
    command.js       the >_ omnibar: index, match, dispatch
    charts.js        uplotTheme, crosshairReadout, heatmap, sparkline, caption
    fmt.js           money / pct / float / timeAgo formatters
    views/
      overview.js
      opportunities.js
      calculator.js
      legacy.js       mounts old markup + logic for PAT / LAB / CONN, CSS-reskinned only
```

### 7.1 Legacy coexistence

The current `index.html` bodies for `#view-ml-lab`, `#view-patterns`, `#view-connections`
move into `js/views/legacy.js` as template strings plus the relevant slices of today's
`app.js` render/bind logic, kept working. They inherit the new CSS (same class names where
possible; a `.legacy` scope for the rest). No feature is lost — those three are just not
rethought this pass.

### 7.2 Server change

`web/server.py` GET handler currently special-cases `/app.js` and `/style.css` only. Add
`/js/...` and `/vendor/...` to the static-serve path (content-type by extension). ~6 lines,
no route/logic changes elsewhere.

### 7.3 Fonts / assets

Google Fonts `<link>` for Space Grotesk + JetBrains Mono. uPlot vendored (not CDN) so the
UI shell has no network dependency. Chart.js CDN `<script>` stays until Lab migrates.

---

## 8. Testing

- **`web/static/charts-selftest.html`** — loads `charts.js` with synthetic data, asserts:
  crosshair readout updates on a simulated cursor move; heatmap `onHover` fires with the
  correct `(row, col)`; `sparkline` renders N points. Logs `SELFTEST OK` / throws. No framework.
- **Manual verification** via the preview browser: start `web.server`, load OVW / OPPS /
  CALC, confirm zero console errors, screenshot Overview + an expanded OPPS row + the
  manual builder.
- **Regression:** PAT / LAB / CONN still render and their existing controls still work
  (they run the same JS, only CSS changed).

---

## 9. Risks / open items

| Risk | Mitigation |
|---|---|
| Lifting API logic out of the 4,450-line `app.js` misses a binding | Keep `legacy.js` running the original code verbatim for non-priority views; only priority views get new render code. |
| uPlot crosshair "float" value is only a bracket midpoint (Steam has no per-listing float) | Labelled `float~` everywhere; real floats shown only where CSFloat resolved them. Matches the app's existing methodology disclosure. |
| DIST heatmap empty until the sim population has run enough | Compact OVW version shows an explicit empty state; not a blocking dependency. |
| `prefers-reduced-motion` / keyboard focus regressions | Quality floor: visible focus ring on every interactive element, reduced-motion honored, checked during manual verification. |
| Scope creep into LAB (huge: radar, bankroll, MAE, feature-importance, market-intel registries) | Explicitly out of scope this pass. Reskin only. |

---

## 10. Rollout

1. Vendored uPlot + `server.py` static-dir change + `charts.js` + `charts-selftest.html`.
2. New `style.css` (tokens + shell + panels + tables) + `index.html` shell + `app.js`
   router + `legacy.js` (all non-priority views live here, working, reskinned).
3. `command.js`.
4. OVW, then OPPS, then CALC — each with its instruments.
5. Manual verification pass + screenshots.

Implementation plan (task breakdown) to follow via the writing-plans skill.
