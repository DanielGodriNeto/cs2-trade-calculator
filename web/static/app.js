const state = {
  tradeups: [],
  anomalies: [],
  covert: [],
  sort: {
    tradeups: { key: "roi_pct", dir: -1 },
    anomalies: { key: "profit_after_fee", dir: -1 },
    covert: { key: "roi_pct", dir: -1 },
  },
  expandedTradeups: new Set(),
  expandedCovert: new Set(),
  expandedSimulations: new Set(),
  expandedArchive: new Set(),   // like expandedSimulations, but for the "browse all opportunities" panel
  archive: { items: [], total: 0, page: 1, page_size: 50, n_pages: 1 },  // most recent
  // /api/sim-population/opportunities/archive page -- cached in state so pagination/verify/
  // blacklist actions can re-render without an extra round-trip, same reasoning as
  // lastBestSimulations below.
  archiveLoaded: false,  // true once the archive panel has been opened at least once this
  // session -- gates the on-demand fetch so it never loads on the 3s poll, only when the user
  // actually opens the panel (see the collapse-archive-btn wiring).
  lifetimeRoiLoaded: false,  // same gating idea, for the "Full lifetime ROI progress" panel
  simVerifyResults: {},  // sim id -> { rows } | { error } -- survives renderBestSimulations()
  // rebuilding the whole tbody every 3s poll, since that would otherwise wipe a just-fetched
  // verify result before the user has a chance to see it (renderSimulationDetail replays it).
  currencies: [],
  currencySymbol: "$",
  constants: null,
  charts: {},           // one Chart.js instance per canvas, updated in place rather than recreated
  lastModelStatus: null,
  skinImageCache: {},   // skin name -> image URL, filled in as /api/skins autocomplete results arrive
  lastBestSimulations: [],       // most recent unfiltered /api/sim-population/best-simulations payload
  opportunityFilters: { minRoi: null, risk: "", agent: "" },  // Trade-Up Opportunity table filters --
  // applied client-side against lastBestSimulations, same "cache in state, don't just fetch-and-
  // forget" reasoning as simVerifyResults above, so a filter change re-renders instantly without
  // waiting on the next poll.
  lastSimLeaderboards: { collections: [], skins: [] },  // most recent /api/sim-population/leaderboards
  // payload, cached the same way as lastBestSimulations above so the Export CSV buttons have
  // something to read without an extra round-trip.
  lastRoundRobin: { enabled: false, active_son_id: null },
  lastBlacklist: [],  // most recent /api/sim-population/blacklist -- trade-ups the user marked "done"
  lastSimPopulation: [],  // most recent /api/sim-population/status payload (one entry per son)
  manualBuilder: { units: [], result: null, error: null, problems: [] },  // Manual trade-up
  // builder (calculator page): composed input units + the last computed result, cached in
  // state (not just the DOM) per this dashboard's poll-survival rule -- the builder section
  // isn't rebuilt by the 3s poll, but every render still reads from here so it never can be.
  lastMispriced: [],  // most recent /api/mispriced-skins payload (Market intelligence panel)
  lastWearAnomalies: [],  // most recent /api/wear-anomalies payload (Market intelligence panel)
  lastSkinBlacklist: [],   // most recent /api/skin-blacklist payload (permanent user blacklist)
  lastPriceOverrides: [],  // most recent /api/price-overrides payload (manual price overrides)
  simMaeHistory: {},  // son_id -> [{round, mae, trained_at}], accumulated client-side across
  // polls this session -- sim_learning.get_model_status()/sim_roi_model_meta.json only ever
  // persists the CURRENT model's validation_mae, never a history of past trainings, so this is
  // the only way to chart a trend (see _recordSimMaeHistory). Written to state first, rendered
  // from state -- same rule as simVerifyResults above -- so it survives the poll-driven rebuild
  // instead of being recomputed (and losing history) every 3s. Resets on page reload, same as
  // the rest of this dashboard's in-memory-only state.
  lastEvolution: null,      // most recent /api/evolution/status payload (Evolution Manager panel)
  lastRecoveryLog: [],      // most recent /api/recovery/log payload (Exploration Recovery panel)
  lastPatternsStatus: null, // most recent /api/patterns/status payload (Rare Patterns page)
  lastPatternOpps: [],      // most recent /api/patterns/opportunities payload -- cached in state
  // (not just rendered) per this dashboard's poll-survival rule, shared by the Rare Patterns
  // page table, the Results page's Collector Arbitrage table, and the CSV export.
  lastPatternDb: null,      // /api/patterns/db payload -- loaded once at boot + after a manual
  // refresh, not on the 3s poll (the knowledge base only changes on refresh/edit).
  lastBackupStatus: null,   // most recent /api/backup/status payload
  backupFormFilled: false,  // the backup config form is filled from the server ONCE, then left
  // alone -- re-filling it on every 3s poll would clobber whatever the user is typing (the
  // exact async-UI-state-wiped-by-poll failure this dashboard's state rules exist to prevent).
  lastConnectionsStatus: null,  // most recent /api/connections/status payload
  connectionsFormFilled: false, // same fill-once-guard idea as backupFormFilled, for the
  // CSFloat/NTFY forms on the new Connections page.
};

// Client-side view switcher for the top nav -- every view's markup lives in the same document
// (see index.html's data-view divs) and shares this one app.js/state, so switching is just a
// hash-driven show/hide, never a page reload. This keeps every existing render/poll function
// working unmodified regardless of which view is currently visible, since the DOM elements they
// target always exist -- only their visibility changes.
// "overview" is the default/landing view -- a daily user wants a KPI/status snapshot first, not
// scroll past a single feature's internals to get oriented. Every other view (including the
// original "results"/"ml-lab" ids -- kept as-is internally, only their nav labels changed, so no
// other JS in this file needs to know about the rename) is one click away via the top nav.
const VIEWS = ["overview", "results", "calculator", "patterns", "ml-lab", "connections"];

// Which loadX() functions each view's visible DOM actually depends on, so pollStatus() only
// fetches what the CURRENT view needs instead of all ~28 endpoints on every 3s tick regardless
// of visibility (verified by tracing every load*->render* pair's getElementById/querySelector
// targets against index.html's data-view boundaries). "overview" doesn't own any panel of its
// own -- its KPI strip is a cross-view SUMMARY (see renderOverview) computed from sim-population,
// best-simulations, and tradeups data, so those three are listed here explicitly rather than
// pulling in the rest of ml-lab/results/calculator just because overview needs a few numbers
// from each of them.
const VIEW_LOADERS = {
  overview: ["loadSimPopulationStatus", "loadBestSimulations", "loadTradeups"],
  calculator: ["loadTradeups", "loadAnomalies", "loadCovert", "loadModelStatus"],
  "ml-lab": ["loadThirdpartyStats", "loadModelStatus", "loadRetrainLoopStatus", "loadOutcomes",
    "loadPriceScanStatus", "loadRoundRobinStatus", "loadSimPopulationStatus", "loadConvergenceStatus",
    "loadVerifyQueueStatus", "loadDeadSkins", "loadInvalidSkins", "loadMispricedSkins",
    "loadWearAnomalies", "loadMarketReliability", "loadEvolutionStatus", "loadRecoveryLog",
    "loadSkinBlacklist", "loadPriceOverrides"],
  results: ["loadBestSimulations", "loadSimLeaderboards", "loadBlacklist", "loadPatternOpportunities"],
  patterns: ["loadPatternsStatus", "loadPatternOpportunities"],
  connections: ["loadBackupStatus", "loadConnectionsStatus"],
};
// Lives in the sticky topbar (index.html lines 27-43), visible regardless of which view is
// active -- must refresh every tick no matter what.
const ALWAYS_LOADERS = ["loadNetworkStats"];

// name -> function reference, resolved once after all loadX functions are defined below
// (pollStatus itself runs long after module load, so forward references are fine).
function _resolveLoaders(names) {
  return names.map(n => window[n]).filter(fn => typeof fn === "function");
}

function currentView() {
  const hash = (location.hash || "").replace("#", "");
  return VIEWS.includes(hash) ? hash : "overview";
}

// Chart.js gives a canvas a 0x0 size forever if it's first created while its container is
// hidden/display:none (a hidden tab, a collapsed <details>, a collapsed .collapse-btn panel) --
// it writes explicit "width: 0px; height: 0px" straight onto the canvas's inline style at
// creation time. Confirmed live (both this session's own testing and Chart.js's known behavior
// here): once that's happened, NEITHER a bare chart.resize() NOR an explicit
// chart.resize(width, height) recovers it, even when the container's own getBoundingClientRect
// reports a correct non-zero size by then -- responsive:true's ResizeObserver doesn't reliably
// re-fire off that starting state either. The only reliable fix is destroy + recreate. Since
// this codebase's "chart exists -> just .update()" fast path (every renderXChart function)
// never re-measures the DOM, simply destroying the broken instance and clearing it from
// state.charts is enough: the next 3s poll tick's "state.charts.X doesn't exist -> create new
// Chart()" branch recreates it fresh, now inside a visible container, with zero changes needed
// to any individual renderXChart function. state.charts holds either a Chart instance directly
// (singleton charts) or a plain object keyed by son_id (the per-agent small-multiples grids) --
// this walks both shapes. Call this any time a chart's container might have just become
// visible; only charts actually stuck at 0x0 are touched, so a healthy chart's legend-toggle
// state (e.g. renderAgentStatsChart's per-dataset hidden flags) survives untouched.
// Uses setTimeout, deliberately NOT requestAnimationFrame -- rAF only fires for an actively
// rendering/foregrounded tab (confirmed live: it never fired at all in one test environment,
// silently no-opping this entire function), while all we actually need is "run after the
// current synchronous DOM mutation (the hidden-attribute toggle) has applied," which a 0ms
// timeout guarantees regardless of tab visibility/focus.
function resizeAllCharts() {
  setTimeout(() => {
    let wearBracketDestroyed = false;
    for (const [key, value] of Object.entries(state.charts)) {
      if (!value) continue;
      if (typeof value.resize === "function") {
        if (value.canvas && value.canvas.width === 0) {
          value.destroy();
          delete state.charts[key];
          if (key === "wearBracket") wearBracketDestroyed = true;
        }
      } else if (typeof value === "object") {
        for (const [subKey, chart] of Object.entries(value)) {
          if (chart && typeof chart.resize === "function" && chart.canvas && chart.canvas.width === 0) {
            chart.destroy();
            delete value[subKey];
          }
        }
      }
    }
    // Every other destroyed chart gets recreated for free the next time its own view's loaders
    // run (pollStatus only fetches the current view's data -- see VIEW_LOADERS -- and the
    // hashchange listener below forces an immediate poll on every view switch, so a chart
    // destroyed here while its view was hidden gets rebuilt as soon as that view becomes visible
    // again, not up to 3s later). wearBracketChart is the one exception: it's rendered once from
    // /api/constants at page load (loadConstants(), never re-polled since wear breakpoints never
    // change at runtime), so if it was just destroyed nothing would ever recreate it without this.
    if (wearBracketDestroyed && state.constants) {
      renderWearBracketChart(state.constants.wear_breakpoints);
    }
  }, 0);
}

function applyView() {
  const view = currentView();
  for (const v of VIEWS) {
    const el = document.getElementById(`view-${v}`);
    if (el) el.hidden = v !== view;
  }
  document.querySelectorAll("#topbar-nav a").forEach(a => {
    a.classList.toggle("active", a.dataset.viewLink === view);
  });
  resizeAllCharts();
}
// Force an immediate refresh on every view switch (in addition to applyView's show/hide) so a
// newly-visible view's panels don't sit stale/empty for up to 3s waiting for the ambient poll --
// pollStatus's own _pollInFlight guard makes this safe to call even if a tick is already running.
window.addEventListener("hashchange", () => { applyView(); pollStatus(); });
applyView();

async function loadCurrencies() {
  const res = await fetch("/api/currencies");
  state.currencies = await res.json();
  const options = state.currencies.map(c => `<option value="${c.code}">${c.name} (${c.symbol})</option>`).join("");
  document.getElementById("cfg-currency").innerHTML = options;
  document.getElementById("oc-currency").innerHTML = options;
  document.getElementById("ph-currency").innerHTML = options;
  document.getElementById("mb-currency").innerHTML = options;
  document.getElementById("po-currency").innerHTML = options;
  document.getElementById("ph-currency").value = "1"; // default USD -- otherwise the browser
  // picks the first <option> in DOM order, which is whatever sorts first alphabetically
  document.getElementById("oc-currency").value = "1";
  document.getElementById("mb-currency").value = "1";
  document.getElementById("po-currency").value = "1";
}

// ---- Methodology / live constants -----------------------------------------------------
// Pulled from the actual backend modules (src/tradeup.py, src/oddsmath.py, etc.) via
// /api/constants rather than hardcoded into this HTML, so the numbers explained on the page
// can never drift out of sync with the code that actually uses them.
async function loadConstants() {
  const res = await fetch("/api/constants");
  state.constants = await res.json();
  renderMethodologyConstants();
}

function renderMethodologyConstants() {
  const c = state.constants;
  if (!c) return;
  document.getElementById("const-fee").textContent = c.steam_sell_fee_pct.toFixed(0);
  document.getElementById("const-drought").textContent = c.drought_threshold_pct.toFixed(0);
  document.getElementById("const-min-samples").textContent = c.min_training_samples;
  document.getElementById("const-retrain-delta").textContent = c.retrain_trigger_delta;
  document.getElementById("const-min-history").textContent = c.min_price_history_points;
  const simRetrainEl = document.getElementById("const-sim-retrain-delta");
  if (simRetrainEl) simRetrainEl.textContent = c.sim_retrain_trigger_delta;
  renderWearBracketChart(c.wear_breakpoints);
}

const WEAR_COLORS = ["#4caf50", "#8bc34a", "#d4af37", "#ff9800", "#eb4b4b"];
const CHART_AXIS_COLOR = "#8890a0";
const AGENT_PALETTE = ["#8ea1ff", "#4caf50", "#d4af37", "#eb4b4b"];  // one fixed color per son_id
// (0-3), shared by the agent radar chart, the agent-stats-over-rounds chart, and the bankroll
// per-agent breakdown, so the same agent is always the same color everywhere on the page.
const CHART_TEXT_COLOR = "#d7dbe4";

// A single stacked horizontal bar showing the five wear brackets end-to-end across the full
// 0.00-1.00 float range -- the visual companion to the "Float manipulation" methodology text.
function renderWearBracketChart(breakpoints) {
  const ctx = document.getElementById("wear-bracket-chart");
  if (!ctx || typeof Chart === "undefined") return;
  const datasets = breakpoints.map((bp, i) => ({
    label: `${bp[0]} (${bp[1].toFixed(2)}-${bp[2].toFixed(2)})`,
    data: [bp[2] - bp[1]],
    backgroundColor: WEAR_COLORS[i % WEAR_COLORS.length],
  }));
  if (state.charts.wearBracket) {
    state.charts.wearBracket.data.datasets = datasets;
    state.charts.wearBracket.update();
    return;
  }
  state.charts.wearBracket = new Chart(ctx, {
    type: "bar",
    data: { labels: ["Float range"], datasets },
    options: {
      indexAxis: "y",
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { stacked: true, min: 0, max: 1, ticks: { color: CHART_AXIS_COLOR } },
        y: { stacked: true, ticks: { color: CHART_AXIS_COLOR } },
      },
      plugins: { legend: { position: "bottom", labels: { color: CHART_TEXT_COLOR, boxWidth: 12, font: { size: 10 } } } },
    },
  });
}

// ---- ROI distribution histogram (current scan results) --------------------------------
function renderRoiHistogram() {
  const wrap = document.getElementById("roi-histogram-wrap");
  const rows = state.tradeups;
  if (!wrap || typeof Chart === "undefined" || !rows.length) {
    if (wrap) wrap.style.display = "none";
    return;
  }
  wrap.style.display = "block";
  const rois = rows.map(r => r.roi_pct);
  const min = Math.min(...rois), max = Math.max(...rois);
  const binCount = 10;
  const binSize = Math.max(max - min, 1) / binCount;
  const bins = new Array(binCount).fill(0);
  rois.forEach(roi => {
    let idx = Math.floor((roi - min) / binSize);
    idx = Math.max(0, Math.min(binCount - 1, idx));
    bins[idx]++;
  });
  const labels = bins.map((_, i) => `${(min + i * binSize).toFixed(0)}%`);
  const colors = bins.map((_, i) => ((min + i * binSize) >= 0 ? "rgba(76,175,80,0.7)" : "rgba(235,75,75,0.7)"));
  const ctx = document.getElementById("roi-histogram-chart");
  if (state.charts.roiHistogram) {
    state.charts.roiHistogram.data.labels = labels;
    state.charts.roiHistogram.data.datasets[0].data = bins;
    state.charts.roiHistogram.data.datasets[0].backgroundColor = colors;
    state.charts.roiHistogram.update();
    return;
  }
  state.charts.roiHistogram = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets: [{ label: "Trade-ups", data: bins, backgroundColor: colors }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        title: { display: true, text: "ROI % distribution across current results", color: CHART_AXIS_COLOR },
      },
      scales: { x: { ticks: { color: CHART_AXIS_COLOR } }, y: { ticks: { color: CHART_AXIS_COLOR, precision: 0 } } },
    },
  });
}

// ---- "Learning Machine -- top picks" ----------------------------------------------------
// Pure client-side view over state.tradeups -- ml_predicted_roi_pct/ml_confidence are already
// present on every result once --use-ml / "Use ML calibration" is on and a model is trained.
function bestMlPicks() {
  return state.tradeups
    .filter(r => r.ml_predicted_roi_pct != null)
    .map(r => ({ ...r, ml_edge: r.ml_predicted_roi_pct - r.roi_pct }))
    .sort((a, b) => b.ml_predicted_roi_pct - a.ml_predicted_roi_pct)
    .slice(0, 10);
}

function renderMlPicks() {
  const picks = bestMlPicks();
  const table = document.getElementById("ml-picks-table");
  const empty = document.getElementById("ml-picks-empty");
  const countEl = document.getElementById("ml-picks-count");
  if (!table || !empty) return;
  countEl.textContent = picks.length ? `(${picks.length})` : "";
  if (!picks.length) {
    table.style.display = "none";
    const minSamples = state.constants ? state.constants.min_training_samples : 20;
    empty.textContent = (state.lastModelStatus && state.lastModelStatus.trained)
      ? "No ML-scored trade-ups yet -- check \"Use ML calibration\" in Scan settings and click \"Apply & rescan\"."
      : `No trained model yet -- log ${minSamples}+ real outcomes below (see "Log a trade-up outcome") and train the model first.`;
    return;
  }
  table.style.display = "";
  empty.textContent = "";
  const tbody = table.querySelector("tbody");
  tbody.innerHTML = picks.map(r => {
    const edgeClass = r.ml_edge >= 0 ? "profit-positive" : "profit-negative";
    return `
    <tr>
      <td>${copyableName(r.cheapest_input, r.cheapest_input_image)}</td>
      <td>${r.collection}</td>
      <td>${r.from_rarity}</td>
      <td>${r.to_rarity}</td>
      <td class="num">${fmtMoney(r.roi_pct)}%</td>
      <td class="num">${fmtMoney(r.ml_predicted_roi_pct)}%</td>
      <td class="num ${edgeClass}">${r.ml_edge >= 0 ? "+" : ""}${fmtMoney(r.ml_edge)}pp</td>
      <td>${r.ml_confidence || "-"}</td>
    </tr>`;
  }).join("");
  wireCopyButtons(tbody);
}

function symbolForCurrency(code) {
  const found = state.currencies.find(c => c.code === code);
  return found ? found.symbol : "$";
}

function rowKey(r) {
  return `${r.collection}|${r.from_rarity}|${r.to_rarity}|${r.stattrak}`;
}

function covertRowKey(r) {
  return `${r.case}|${r.stattrak}`;
}

const MARKETPLACE_LABELS = { steam: "Steam", csfloat: "CSFloat", skinport: "Skinport", dmarket: "DMarket", bitskins: "BitSkins" };

// buy_links is only populated server-side for +EV (ROI >= 0) results -- nothing to show
// for a losing trade, so this renders empty rather than a row of dead links.
function renderBuyLinks(links) {
  if (!links || !Object.keys(links).length) return "";
  return `<span class="buy-links">${Object.entries(links).map(([key, url]) =>
    `<a href="${url}" target="_blank" rel="noopener noreferrer" class="buy-link" title="Search for a cheap high-float copy on ${MARKETPLACE_LABELS[key] || key}">${MARKETPLACE_LABELS[key] || key}</a>`
  ).join("")}</span>`;
}

function fmtTime(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function fmtMoney(n) {
  return typeof n === "number" ? n.toFixed(2) : n;
}

function fmtCurrency(n) {
  return typeof n === "number" ? `${state.currencySymbol}${n.toFixed(2)}` : n;
}

// A skin name with a small button that copies just the name (no price/wear noise)
// to the clipboard, so it can be pasted straight into the Steam Market search box.
// imageUrl (optional) prefixes a small thumbnail hotlinked straight from Steam's own CDN --
// onerror hides it gracefully rather than showing a broken-image icon if it can't load.
function copyableName(name, imageUrl) {
  const thumb = imageUrl
    ? `<img class="skin-thumb" src="${imageUrl}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : "";
  return `<span class="copyable">${thumb}${name}<button class="copy-btn" data-copy="${encodeURIComponent(name)}" title="Copy skin name">⧉</button></span>`;
}

async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e2) { /* clipboard unavailable, nothing more we can do */ }
    document.body.removeChild(ta);
  }
  const original = btn.textContent;
  btn.textContent = "✓";
  btn.classList.add("copied");
  setTimeout(() => {
    btn.textContent = original;
    btn.classList.remove("copied");
  }, 1200);
}

function wireCopyButtons(container) {
  container.querySelectorAll(".copy-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // don't let the click bubble up and toggle an expandable row
      copyToClipboard(decodeURIComponent(btn.dataset.copy), btn);
    });
  });
}

// Guards against overlapping ticks: setInterval fires every 3s regardless of whether the
// previous pollStatus() finished. If any single fetch is slow even once (e.g. a status endpoint
// that touches a rate-limited live Steam/CSFloat call while a scan is running), the next tick
// piling more requests on top of the still-pending ones -- browsers cap concurrent connections
// per origin at ~6 (HTTP/1.1) -- can snowball into every request queuing forever and the whole
// dashboard looking permanently "disconnected" until a hard reload. Skipping a tick while one is
// already in flight makes a single slow cycle merely skip a beat instead of cascading into a
// deadlock.
let _pollInFlight = false;

async function pollStatus() {
  if (_pollInFlight) return;
  _pollInFlight = true;
  try {
    const res = await fetch("/api/status");
    const s = await res.json();
    renderStatus(s);
    // Only fetch what the CURRENTLY VISIBLE view's DOM actually needs (see VIEW_LOADERS above) --
    // previously this fanned out all ~28 endpoints every 3s regardless of which of the 6 views
    // was showing, which was the dashboard's main source of per-tick lag. A view switch triggers
    // an immediate extra poll (see the hashchange listener below) so newly-visible panels don't
    // wait up to 3s for fresh data the first time you look at them.
    const names = new Set([...ALWAYS_LOADERS, ...(VIEW_LOADERS[currentView()] || [])]);
    await Promise.all(_resolveLoaders([...names]).map(fn => fn()));
    renderOverview();
    updateMarketIntelBadge();
  } catch (e) {
    document.getElementById("status-value").textContent = "disconnected";
    const topbarVal = document.getElementById("topbar-scan-status-value");
    topbarVal.className = "error";
    topbarVal.textContent = "disconnected";
  } finally {
    _pollInFlight = false;
  }
}

function fmtPct(n) {
  return typeof n === "number" ? `${n.toFixed(1)}%` : n;
}

async function loadModelStatus() {
  const res = await fetch("/api/model-status");
  const m = await res.json();
  state.lastModelStatus = m;
  const el = document.getElementById("model-status");
  const importanceWrap = document.getElementById("feature-importance-wrap");
  const importanceEmpty = document.getElementById("feature-importance-empty");
  if (!m.trained) {
    el.textContent = `not trained yet (need ${m.min_samples_required}+ sold outcomes logged)`;
    if (importanceWrap) importanceWrap.style.display = "none";
    if (importanceEmpty) importanceEmpty.style.display = "block";
  } else {
    const algo = m.model_name ? ` -- ${m.model_name.replaceAll("_", " ")} won` : "";
    el.textContent = `trained on ${m.n_samples} outcomes -- MAE ${m.validation_mae != null ? m.validation_mae.toFixed(1) + "pp" : "?"}${algo} (${fmtTime(m.trained_at)})`;
    if (m.feature_importances && importanceWrap) {
      importanceWrap.style.display = "block";
      importanceEmpty.style.display = "none";
      renderFeatureImportanceChart(m.feature_importances);
    }
    renderModelCandidateComparison(m.candidate_maes, m.model_name, "candidate-comparison");
  }
}

// Shows every candidate algorithm's own cross-validated MAE next to the winner -- see
// ml_common.py's model-selection reasoning. targetId lets the real model (methodology
// section) and each sim-population son (feature-importance grid) share this one renderer.
function renderModelCandidateComparison(candidateMaes, winnerName, targetId) {
  const el = document.getElementById(targetId);
  if (!el) return;
  if (!candidateMaes) { el.textContent = ""; return; }
  const parts = Object.entries(candidateMaes)
    .sort((a, b) => a[1] - b[1])
    .map(([name, mae]) => {
      const label = name.replaceAll("_", " ");
      return name === winnerName ? `<strong>${label}: ${mae.toFixed(2)}pp (won)</strong>` : `${label}: ${mae.toFixed(2)}pp`;
    });
  el.innerHTML = `Algorithms compared via cross-validation: ${parts.join(" &middot; ")}`;
}

// Horizontal bar chart of the trained model's permutation feature importances -- lives in the
// Methodology section, right next to the prose explaining what the model uses and why.
function renderFeatureImportanceChart(importances) {
  const ctx = document.getElementById("feature-importance-chart");
  if (!ctx || typeof Chart === "undefined") return;
  const entries = Object.entries(importances).sort((a, b) => b[1] - a[1]);
  const labels = entries.map(e => e[0]);
  const data = entries.map(e => e[1]);
  if (state.charts.featureImportance) {
    state.charts.featureImportance.data.labels = labels;
    state.charts.featureImportance.data.datasets[0].data = data;
    state.charts.featureImportance.update();
    return;
  }
  state.charts.featureImportance = new Chart(ctx, {
    type: "bar",
    data: { labels, datasets: [{ label: "Permutation importance", data, backgroundColor: "#8ea1ff" }] },
    options: {
      indexAxis: "y",
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: CHART_AXIS_COLOR } }, y: { ticks: { color: CHART_AXIS_COLOR, font: { size: 10 } } } },
    },
  });
}


async function loadRetrainLoopStatus() {
  const res = await fetch("/api/retrain-loop/status");
  renderRetrainLoopStatus(await res.json());
}

function renderRetrainLoopStatus(s) {
  const el = document.getElementById("retrain-loop-status");
  const modelPart = s.model_trained
    ? `model trained on ${s.model_n_samples} outcomes (MAE ${s.model_validation_mae != null ? s.model_validation_mae.toFixed(1) + "pp" : "?"})`
    : "model not trained yet";
  el.textContent = `${s.status} | ${modelPart} | ${s.n_sold_outcomes} sold outcomes logged | ${s.outcomes_needed_to_retrain} more needed to retrain`;
  document.getElementById("retrain-loop-start-btn").disabled = s.status !== "paused" && s.status !== "stopped";
  document.getElementById("retrain-loop-stop-btn").disabled = s.status === "paused" || s.status === "stopped";
}

async function loadPriceScanStatus() {
  const res = await fetch("/api/price-scan/status");
  renderPriceScanStatus(await res.json());
}

function renderPriceScanStatus(s) {
  const el = document.getElementById("price-scan-status");
  const progress = s.progress && s.progress.total ? ` -- ${s.progress.current}/${s.progress.total}` : "";
  el.textContent = `${s.status}${progress} | ${s.n_priced} priced this run | updated ${fmtTime(s.updated_at)}`;
  document.getElementById("price-scan-start-btn").disabled = s.status === "running";
  document.getElementById("price-scan-pause-btn").disabled = s.status !== "running";
  document.getElementById("price-scan-stop-btn").disabled = s.status === "paused" || s.status === "idle" || s.status === "stopped";
}

function _temperatureLabel(temp) {
  if (temp <= 0.3) return "exploitative";
  if (temp <= 0.7) return "mostly exploitative";
  if (temp <= 1.3) return "balanced";
  return "explorative";
}

async function loadSimPopulationStatus() {
  const res = await fetch("/api/sim-population/status");
  const sons = await res.json();
  state.lastSimPopulation = sons;
  renderSimPopulation(sons);
  _ensureAgentFilterOptions(sons);
}

async function loadRoundRobinStatus() {
  const res = await fetch("/api/sim-population/round-robin/status");
  state.lastRoundRobin = await res.json();
  renderRoundRobinStatus();
}

function renderRoundRobinStatus() {
  const rr = state.lastRoundRobin;
  const agentNames = (state.constants && state.constants.agent_names) || {};
  document.getElementById("round-robin-start-btn").disabled = rr.enabled;
  document.getElementById("round-robin-stop-btn").disabled = !rr.enabled;
  document.getElementById("round-robin-status").textContent = rr.enabled
    ? `running -- currently: ${agentNames[rr.active_son_id] || `Son ${rr.active_son_id}`}`
    : "";
}

async function loadDeadSkins() {
  const res = await fetch("/api/dead-skins");
  renderDeadSkins(await res.json());
}

function renderDeadSkins(dead) {
  const table = document.getElementById("dead-skins-table");
  const emptyEl = document.getElementById("dead-skins-empty");
  if (!dead.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No skins currently flagged -- scores are reviewed weekly and only reflect skins actually seen by a scan/simulation so far, not the whole market.";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  document.querySelector("#dead-skins-table tbody").innerHTML = dead.map(d => `
    <tr>
      <td>${d.market_hash_name}</td>
      <td class="num">${d.score.toFixed(0)}</td>
      <td>${(d.reasons || []).join(", ")}</td>
      <td>${d.last_seen ? fmtTime(d.last_seen) : "-"}</td>
    </tr>`).join("");
}

async function loadInvalidSkins() {
  const res = await fetch("/api/invalid-skins");
  renderInvalidSkins(await res.json());
}

async function clearInvalidSkins() {
  if (!confirm("Erase all invalid-skins data? This is safe -- any skin still without a price will be re-flagged automatically next time it's simulated.")) return;
  await fetch("/api/invalid-skins/clear", { method: "POST" });
  await loadInvalidSkins();
}

function renderInvalidSkins(invalid) {
  const table = document.getElementById("invalid-skins-table");
  const emptyEl = document.getElementById("invalid-skins-empty");
  if (!invalid.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No skins currently registered as invalid -- a skin lands here only when no price could be obtained from Steam or any third-party site. An empty list is the healthy case.";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  document.querySelector("#invalid-skins-table tbody").innerHTML = invalid.map(d => `
    <tr>
      <td>${d.market_hash_name}</td>
      <td>${d.reason || ""}</td>
      <td>${d.active ? "active (excluded)" : "due for re-check"}</td>
      <td>${d.marked_at ? fmtTime(d.marked_at) : "-"}</td>
    </tr>`).join("");
}

async function loadMispricedSkins() {
  const res = await fetch("/api/mispriced-skins");
  state.lastMispriced = await res.json();
  renderMispricedSkins(state.lastMispriced);
}

async function clearMispricedSkins() {
  if (!confirm("Erase all mispriced-skin marks? Safe -- any skin still mispriced will be re-flagged by the next verification that sees it.")) return;
  await fetch("/api/mispriced-skins/clear", { method: "POST" });
  await loadMispricedSkins();
}

function renderMispricedSkins(rows) {
  const table = document.getElementById("mispriced-skins-table");
  const emptyEl = document.getElementById("mispriced-skins-empty");
  if (!table || !emptyEl) return;
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No verified mispricings on record -- marks only appear after a live Stage-2 verification (automatic queue, a Verify button, or Verify top 50) finds Steam 20%+ away from the wider market.";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  table.querySelector("tbody").innerHTML = rows.map(d => `
    <tr>
      <td>${d.market_hash_name}</td>
      <td><span class="${d.direction === "overvalued" ? "profit-negative" : "profit-positive"}">${d.direction}</span></td>
      <td class="num">${d.gap_pct != null ? fmtMoney(d.gap_pct) + "%" : "-"}</td>
      <td class="num">${d.steam_price != null ? fmtCurrency(d.steam_price) : "-"}</td>
      <td class="num">${d.cheapest_price != null ? fmtCurrency(d.cheapest_price) + " (" + (d.cheapest_source || "?") + ")" : "-"}</td>
      <td>${d.active ? "active (penalized in training)" : "expired -- due for re-check"}</td>
      <td>${d.marked_at ? fmtTime(d.marked_at) : "-"}</td>
    </tr>`).join("");
}

// ---- Skin blacklist (permanent) + manual price overrides (Market intelligence) ----------
// Tables re-render on the 3s poll like the other Market-intel lists; the FORMS live in static
// HTML and are never rebuilt, so typing survives the poll (this dashboard's poll-survival rule).

async function loadSkinBlacklist() {
  const res = await fetch("/api/skin-blacklist");
  state.lastSkinBlacklist = await res.json();
  renderSkinBlacklist(state.lastSkinBlacklist);
}

function renderSkinBlacklist(rows) {
  const table = document.getElementById("skin-blacklist-table");
  const emptyEl = document.getElementById("skin-blacklist-empty");
  if (!table || !emptyEl) return;
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No skins blacklisted. Entries here NEVER expire on their own -- add one above when you've personally confirmed a skin's price data is garbage (e.g. it only really trades above Steam's price cap).";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  table.querySelector("tbody").innerHTML = rows.map(d => `
    <tr>
      <td>${d.skin}</td>
      <td>${d.note || "-"}</td>
      <td>${d.added_at ? fmtTime(d.added_at) : "-"}</td>
      <td><button class="secondary bl-remove-btn" data-skin="${encodeURIComponent(d.skin)}">Remove</button></td>
    </tr>`).join("");
  table.querySelectorAll(".bl-remove-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      await fetch("/api/skin-blacklist/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skin: decodeURIComponent(btn.dataset.skin) }),
      });
      await loadSkinBlacklist();
      loadBestSimulations();  // hidden rows may reappear immediately
    });
  });
}

async function loadPriceOverrides() {
  const res = await fetch("/api/price-overrides");
  state.lastPriceOverrides = await res.json();
  renderPriceOverrides(state.lastPriceOverrides);
}

function renderPriceOverrides(rows) {
  const table = document.getElementById("price-overrides-table");
  const emptyEl = document.getElementById("price-overrides-empty");
  if (!table || !emptyEl) return;
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No price overrides stored. Set one above to force your own price for a skin+wear everywhere -- existing opportunities are re-priced/re-ROI'd immediately.";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  table.querySelector("tbody").innerHTML = rows.map(d => `
    <tr>
      <td>${d.market_hash_name}</td>
      <td class="num">${d.price != null ? d.price.toFixed(2) : "-"}</td>
      <td>${(state.currencies.find(c => c.code === d.currency) || {}).name || d.currency}</td>
      <td>${d.added_at ? fmtTime(d.added_at) : "-"}</td>
      <td><button class="secondary po-remove-btn" data-name="${encodeURIComponent(d.market_hash_name)}">Remove</button></td>
    </tr>`).join("");
  table.querySelectorAll(".po-remove-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      await fetch("/api/price-overrides/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market_hash_name: decodeURIComponent(btn.dataset.name) }),
      });
      await loadPriceOverrides();
      loadBestSimulations();  // repriced rows revert to their as-simulated numbers
    });
  });
}

function _flashPanelMsg(id, text, isError) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = "block";
  el.textContent = text;
  el.style.color = isError ? "var(--loss, #e05555)" : "";
  setTimeout(() => { el.style.display = "none"; }, 6000);
}

async function loadWearAnomalies() {
  const res = await fetch("/api/wear-anomalies");
  state.lastWearAnomalies = await res.json();
  renderWearAnomalies(state.lastWearAnomalies);
}

async function clearWearAnomalies() {
  if (!confirm("Erase all wear-anomaly marks? Safe -- any price still inverted against its own better wears will be re-flagged by the next positive-ROI scan that touches it.")) return;
  await fetch("/api/wear-anomalies/clear", { method: "POST" });
  await loadWearAnomalies();
}

function renderWearAnomalies(rows) {
  const table = document.getElementById("wear-anomalies-table");
  const emptyEl = document.getElementById("wear-anomalies-empty");
  if (!table || !emptyEl) return;
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No wear-order inversions on record -- marks appear when a positive-ROI scan finds an output skin priced above its own better (less worn) condition, which is always a bad data point rather than real revenue.";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  table.querySelector("tbody").innerHTML = rows.map(d => `
    <tr>
      <td>${d.market_hash_name}</td>
      <td class="num"><span class="profit-negative">${d.price != null ? fmtCurrency(d.price) : "-"}</span></td>
      <td class="num">${d.better_price != null ? fmtCurrency(d.better_price) + " (" + (d.better_wear || "?") + ")" : "-"}</td>
      <td class="num">${d.inversion_pct != null ? d.inversion_pct.toFixed(1) + "%" : "-"}</td>
      <td>${d.active ? "active (excluded from recommendations)" : "expired -- due for re-check"}</td>
      <td>${d.marked_at ? fmtTime(d.marked_at) : "-"}</td>
    </tr>`).join("");
}

async function loadMarketReliability() {
  const res = await fetch("/api/market-reliability");
  state.lastMarketReliability = await res.json();
  renderMarketReliability(state.lastMarketReliability);
}

function renderMarketReliability(rows) {
  const table = document.getElementById("market-reliability-table");
  const emptyEl = document.getElementById("market-reliability-empty");
  if (!table || !emptyEl) return;
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No skins currently tracked -- this list is the union of whatever dead_skin.py/mispriced_skins.py has already scored, so it fills in as the agents/scans see more of the market.";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  table.querySelector("tbody").innerHTML = rows.map(d => `
    <tr>
      <td>${d.market_hash_name}</td>
      <td><span class="reliability-badge ${d.state}${d.overridden ? " overridden" : ""}" title="${d.overridden ? "Manual override: " + (d.override_note || "") : ""}">${d.state}${d.overridden ? " (override)" : ""}</span></td>
      <td class="num">${d.reliability_score != null ? d.reliability_score.toFixed(0) : "-"}</td>
      <td>${(d.reasons || []).join(", ") || "-"}</td>
      <td class="num">${d.rolling_z != null ? d.rolling_z.toFixed(2) : "-"}</td>
      <td>${d.computed_at ? fmtTime(d.computed_at) : "-"}</td>
      <td>
        <button class="secondary mr-override-btn" data-name="${encodeURIComponent(d.market_hash_name)}" data-currency="${d.currency}">Override</button>
        ${d.overridden ? `<button class="secondary mr-clear-override-btn" data-name="${encodeURIComponent(d.market_hash_name)}" data-currency="${d.currency}">Clear</button>` : ""}
      </td>
    </tr>`).join("");
  table.querySelectorAll(".mr-override-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const name = decodeURIComponent(btn.dataset.name);
      const chosen = prompt(`Override state for ${name} -- type healthy, suspicious, or excluded:`, "excluded");
      if (!chosen || !["healthy", "suspicious", "excluded"].includes(chosen.trim().toLowerCase())) return;
      const note = prompt("Optional note for this override:", "") || "";
      btn.disabled = true;
      try {
        await fetch("/api/market-reliability/override", {
          method: "POST",
          body: JSON.stringify({
            market_hash_name: name, currency: Number(btn.dataset.currency),
            state: chosen.trim().toLowerCase(), note,
          }),
        });
        loadMarketReliability();
      } catch (e) { btn.disabled = false; }
    });
  });
  table.querySelectorAll(".mr-clear-override-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const name = decodeURIComponent(btn.dataset.name);
      btn.disabled = true;
      try {
        await fetch("/api/market-reliability/override/clear", {
          method: "POST",
          body: JSON.stringify({ market_hash_name: name, currency: Number(btn.dataset.currency) }),
        });
        loadMarketReliability();
      } catch (e) { btn.disabled = false; }
    });
  });
}

async function loadVerifyQueueStatus() {
  const res = await fetch("/api/verify-queue/status");
  renderVerifyQueueStatus(await res.json());
}

function renderVerifyQueueStatus(v) {
  const el = document.getElementById("verify-queue-status");
  el.textContent = `${v.status} | queue depth: ${v.queue_depth} | verified today: ${v.n_verified_today} | ` +
    `total: ${v.n_verified_total} (auto-verify bar: ROI > ${v.roi_threshold_pct}%, confidence >= ${v.confidence_threshold})`;
  document.getElementById("verify-queue-start-btn").disabled = v.status !== "paused" && v.status !== "stopped";
  document.getElementById("verify-queue-stop-btn").disabled = v.status === "paused" || v.status === "stopped";

  const agentNames = (state.constants && state.constants.agent_names) || {};
  document.querySelector("#verify-queue-table tbody").innerHTML = (v.recent || []).map(r => `
    <tr>
      <td>${agentNames[r.son_id] || `Son ${r.son_id}`}</td>
      <td>${r.primary_collection}</td>
      <td class="num">${r.verified_roi_pct != null ? fmtMoney(r.verified_roi_pct) + "%" : "-"}</td>
      <td>${r.price_confidence ? confidenceBadge(r.price_confidence) : "-"}</td>
      <td>${fmtTime(r.verified_at)}</td>
      <td>${r.error || ""}</td>
    </tr>`).join("");
}

// ---- Evolution Manager (Evolution Engine 2.0) -------------------------------------------
async function loadEvolutionStatus() {
  const res = await fetch("/api/evolution/status");
  state.lastEvolution = await res.json();
  renderEvolution(state.lastEvolution);
}

function _evolutionRows(genomes) {
  const agentNames = (state.constants && state.constants.agent_names) || {};
  return (genomes || []).map(g => `
    <tr>
      <td>${agentNames[g.son_id] || `Son ${g.son_id}`}</td>
      <td>${(g.species || "").replaceAll("|", " | ")}</td>
      <td class="num">${g.fitness != null ? g.fitness.toFixed(1) : "-"}</td>
      <td class="num ${g.roi_pct != null && g.roi_pct >= 0 ? "profit-positive" : "profit-negative"}">${g.roi_pct != null ? fmtMoney(g.roi_pct) + "%" : "-"}</td>
      <td>${fmtTime(g.added_at)}</td>
    </tr>`).join("");
}

function renderEvolution(ev) {
  const el = document.getElementById("evolution-status");
  if (!el || !ev) return;
  const toggleBtn = document.getElementById("evolution-toggle-btn");
  toggleBtn.textContent = ev.enabled ? "Disable" : "Enable";
  document.getElementById("evolution-tick-btn").disabled = !ev.enabled;
  document.getElementById("evolution-elite-cap").textContent = ev.elite_capacity;
  el.textContent = ev.enabled
    ? `enabled | generation ${ev.generation} | elite ${ev.elite_size}/${ev.elite_capacity} genomes across ${ev.n_species} species | `
      + `${ev.injections} cross-agent parent injections | novelty archive: ${ev.novelty_archive_size} distinct strategies | `
      + `last tick ${fmtTime(ev.last_tick_at)} | meta agents refreshed ${fmtTime(ev.last_meta_refresh_at)} | last hyperparam transfer ${fmtTime(ev.last_pbt_at)}`
      + (ev.last_error ? ` | last error: ${ev.last_error}` : "")
    : "disabled -- every hook (parent injection, mutation boost, meta refresh, hyperparameter evolution) is returning neutral values; the population behaves exactly as if the manager didn't exist.";
  const eliteTable = document.getElementById("evolution-elite-table");
  const eliteEmpty = document.getElementById("evolution-elite-empty");
  if (!(ev.elite_top || []).length) {
    eliteTable.style.display = "none";
    eliteEmpty.style.display = "block";
  } else {
    eliteTable.style.display = "";
    eliteEmpty.style.display = "none";
    eliteTable.querySelector("tbody").innerHTML = _evolutionRows(ev.elite_top);
  }
  document.querySelector("#evolution-hof-table tbody").innerHTML = _evolutionRows(ev.hall_of_fame);
  const agentNames = (state.constants && state.constants.agent_names) || {};
  const events = [];
  for (const e of ev.pbt_events || []) {
    events.push(`hyperparams: ${agentNames[e.donor] || `Son ${e.donor}`} (reward ${e.donor_reward}) → ${agentNames[e.receiver] || `Son ${e.receiver}`} (reward ${e.receiver_reward}) at ${fmtTime(e.at)}`);
  }
  for (const e of ev.meta_events || []) {
    events.push(`meta refresh from specialists ${(e.parents || []).map(p => agentNames[p] || `Son ${p}`).join(" + ")} at ${fmtTime(e.at)}`);
  }
  document.getElementById("evolution-events").innerHTML = events.length
    ? `Recent evolution events:<br>${events.map(e => `&bull; ${e}`).join("<br>")}`
    : "";
}

// ---- Exploration Recovery Mode log --------------------------------------------------------
async function loadRecoveryLog() {
  const res = await fetch("/api/recovery/log?limit=30");
  state.lastRecoveryLog = await res.json();
  renderRecoveryLog(state.lastRecoveryLog);
}

function renderRecoveryLog(rows) {
  const table = document.getElementById("recovery-log-table");
  const emptyEl = document.getElementById("recovery-log-empty");
  const countEl = document.getElementById("recovery-log-count");
  if (!table || !emptyEl) return;
  countEl.textContent = rows.length ? `(${rows.length} recent)` : "";
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  const agentNames = (state.constants && state.constants.agent_names) || {};
  table.querySelector("tbody").innerHTML = rows.map(r => `
    <tr>
      <td>${agentNames[r.son_id] || `Son ${r.son_id}`}</td>
      <td><span class="recovery-event recovery-event-${r.event}">${r.event}</span></td>
      <td class="num">${r.level != null ? r.level : "-"}</td>
      <td class="num">${r.round != null ? r.round : "-"}</td>
      <td class="num">${r.rounds_since_improvement != null ? r.rounds_since_improvement : "-"}</td>
      <td>${fmtTime(r.at)}</td>
    </tr>`).join("");
}

// ---- Rare Pattern Arbitrage (Collector Arbitrage) ------------------------------------------
function tierBadge(tier) {
  if (tier == null) return "-";
  return `<span class="tier-badge tier-${tier}">Tier ${tier}</span>`;
}

function patConfBadge(conf) {
  return conf ? `<span class="pat-conf pat-conf-${conf}">${conf}</span>` : "-";
}

async function loadPatternsStatus() {
  const res = await fetch("/api/patterns/status");
  state.lastPatternsStatus = await res.json();
  renderPatternsStatus(state.lastPatternsStatus);
}

function renderPatternsStatus(ps) {
  const el = document.getElementById("patterns-scan-status");
  if (!el || !ps) return;
  const s = ps.scanner || {};
  const parts = [
    `${s.status}${s.current_target ? ` -- checking ${s.current_target}` : ""}`,
    `${s.n_targets || 0} scan targets from the knowledge base`,
    `CSFloat: ${s.queries || 0} queries | ${s.listings_seen || 0} listings | last success ${fmtTime(s.last_success_at)}`,
    `Steam Market: ${s.steam_queries || 0} queries | ${s.steam_listings_seen || 0} listings | ${s.inspects_resolved || 0} inspect links resolved | last success ${fmtTime(s.steam_last_success_at)}`,
    `${s.matches || 0} matches | ${s.opportunities_live || 0} live opportunities`,
  ];
  if (s.steam_blocked_until && s.steam_blocked_until * 1000 > Date.now()) {
    parts.push(`⏳ Steam source cooling down after a rate limit -- resumes ${fmtTime(s.steam_blocked_until)} (CSFloat scanning continues meanwhile)`);
  }
  if (s.last_error) parts.push(`CSFloat last error: ${s.last_error}`);
  if (s.steam_last_error) parts.push(`Steam last error: ${s.steam_last_error}`);
  if (s.inspect_last_error) parts.push(`inspect last error: ${s.inspect_last_error}`);
  if (!s.csfloat_key_configured && s.csfloat_key_note) parts.push(`⚠ ${s.csfloat_key_note}`);
  el.textContent = parts.join(" | ");
  document.getElementById("patterns-scan-start-btn").disabled = s.status === "scanning";
  document.getElementById("patterns-scan-pause-btn").disabled = s.status !== "scanning";

  renderPatternsTopbar(s);

  const dbEl = document.getElementById("pattern-db-status");
  const k = ps.knowledge || {};
  if (dbEl) {
    const byType = Object.entries(k.entries_by_type || {}).map(([t, n]) => `${n} ${t.replaceAll("_", " ")}`).join(", ");
    dbEl.textContent = `knowledge version ${k.knowledge_version} | ${k.n_entries} documented patterns (${byType}) | `
      + `last verified ${fmtTime(k.last_verified_at)}${k.refresh_due ? " (refresh due)" : ""}`
      + (k.remote_verification_note ? ` | ${k.remote_verification_note}` : "");
  }
}

// Mirrors the Rare Patterns scanner's own network health (CSFloat + Steam Market inspect
// queries -- a completely separate rate-limited path from the Steam price cache the row above
// tracks) into the global topbar, same as renderNetworkStats does for the price cache. Runs off
// the same 3s poll (loadPatternsStatus is in the pollStatus Promise.all), so it stays current
// from any tab, not just when scrolled to the Rare Patterns page.
function renderPatternsTopbar(s) {
  const headline = document.getElementById("patterns-health-headline");
  if (!headline) return;
  document.getElementById("pat-stat-csfloat-queries").textContent = s.queries || 0;
  document.getElementById("pat-stat-steam-queries").textContent = s.steam_queries || 0;
  document.getElementById("pat-stat-matches").textContent = s.matches || 0;

  const now = Date.now() / 1000;
  const steamCoolingDown = s.steam_blocked_until && s.steam_blocked_until > now;

  if (s.status !== "scanning") {
    headline.textContent = "Rare Patterns scanner: idle -- not running (start it on the Rare Patterns page)";
    headline.className = "health-headline health-idle";
  } else if (steamCoolingDown) {
    const remaining = Math.ceil(s.steam_blocked_until - now);
    headline.textContent = `⏳ Rare Patterns: Steam Market cooling down after a rate limit -- resumes in ${remaining}s (CSFloat continues meanwhile)`;
    headline.className = "health-headline health-warn";
  } else if (s.last_error) {
    headline.textContent = `⚠ Rare Patterns: CSFloat error -- ${s.last_error}`;
    headline.className = "health-headline health-warn";
  } else if (s.steam_last_error) {
    headline.textContent = `⚠ Rare Patterns: Steam Market error -- ${s.steam_last_error}`;
    headline.className = "health-headline health-warn";
  } else {
    headline.textContent = "✓ Rare Patterns scanner running normally";
    headline.className = "health-headline health-ok";
  }
}

function _patternFilterParams() {
  const params = new URLSearchParams();
  const val = (id) => (document.getElementById(id) ? document.getElementById(id).value.trim() : "");
  if (val("pat-weapon")) params.set("weapon", val("pat-weapon"));
  if (val("pat-type")) params.set("pattern_type", val("pat-type"));
  if (val("pat-marketplace")) params.set("marketplace", val("pat-marketplace"));
  if (val("pat-min-profit")) params.set("min_profit", val("pat-min-profit"));
  if (val("pat-min-confidence")) params.set("min_confidence", val("pat-min-confidence"));
  if (val("pat-min-float")) params.set("min_float", val("pat-min-float"));
  if (val("pat-max-float")) params.set("max_float", val("pat-max-float"));
  if (val("pat-tier")) params.set("tier", val("pat-tier"));
  params.set("sort", val("pat-sort") || "score");
  return params;
}

async function loadPatternOpportunities() {
  const res = await fetch(`/api/patterns/opportunities?${_patternFilterParams()}`);
  state.lastPatternOpps = await res.json();
  renderPatternOpportunities(state.lastPatternOpps);
  renderCollectorOpps(state.lastPatternOpps);
}

function renderPatternOpportunities(rows) {
  const table = document.getElementById("patterns-table");
  const emptyEl = document.getElementById("patterns-empty");
  const countEl = document.getElementById("patterns-opps-count");
  if (!table || !emptyEl) return;
  countEl.textContent = rows.length ? `(${rows.length})` : "";
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  table.querySelector("tbody").innerHTML = rows.map(o => `
    <tr title="${(o.explanation || "").replace(/"/g, "&quot;")}">
      <td>${copyableName(o.market_hash_name, o.image_url)}</td>
      <td>${o.pattern_label || "-"} ${tierBadge(o.pattern_tier)}
        ${o.source_url ? `<a href="${o.source_url}" target="_blank" rel="noopener noreferrer" class="buy-link" title="Public source documenting this pattern">source ↗</a>` : ""}</td>
      <td class="num">${o.float_value != null ? o.float_value.toFixed(4) : "-"}</td>
      <td class="num">${o.paint_seed != null ? o.paint_seed : (o.paint_index != null ? `idx ${o.paint_index}` : "-")}</td>
      <td class="num">${fmtCurrency(o.asking_price)}</td>
      <td class="num" title="consensus base ${o.base_market_price != null ? o.base_market_price.toFixed(2) : "?"} across ${o.base_n_sources || 0} source(s) x documented premium">${fmtCurrency(o.estimated_collector_value)}</td>
      <td class="num ${o.expected_profit >= 0 ? "profit-positive" : "profit-negative"}">${fmtCurrency(o.expected_profit)}</td>
      <td class="num">${o.edge_pct != null ? fmtMoney(o.edge_pct) + "%" : "-"}</td>
      <td>${patConfBadge(o.confidence)}</td>
      <td>${MARKETPLACE_LABELS[o.marketplace] || o.marketplace}</td>
      <td>${o.listing_url ? `<a href="${o.listing_url}" target="_blank" rel="noopener noreferrer" class="buy-link">View listing ↗</a>` : "-"}</td>
    </tr>`).join("");
  wireCopyButtons(table.querySelector("tbody"));
}

// The Results page's companion table: same data, clearly identified as Collector Arbitrage
// (NOT trade-ups), top 10 by score, linking back to the dedicated page for the rest.
function renderCollectorOpps(rows) {
  const table = document.getElementById("collector-opps-table");
  const emptyEl = document.getElementById("collector-opps-empty");
  const countEl = document.getElementById("collector-opps-count");
  if (!table || !emptyEl) return;
  countEl.textContent = rows.length ? `(${rows.length})` : "";
  if (!rows.length) {
    table.style.display = "none";
    emptyEl.style.display = "block";
    return;
  }
  table.style.display = "";
  emptyEl.style.display = "none";
  table.querySelector("tbody").innerHTML = rows.slice(0, 10).map(o => `
    <tr title="${(o.explanation || "").replace(/"/g, "&quot;")}">
      <td><span class="collector-tag">Collector Arbitrage</span></td>
      <td>${copyableName(o.market_hash_name, o.image_url)}</td>
      <td>${o.pattern_label || "-"} ${tierBadge(o.pattern_tier)}</td>
      <td class="num">${fmtCurrency(o.asking_price)}</td>
      <td class="num">${fmtCurrency(o.estimated_collector_value)}</td>
      <td class="num ${o.expected_profit >= 0 ? "profit-positive" : "profit-negative"}">${fmtCurrency(o.expected_profit)}</td>
      <td>${patConfBadge(o.confidence)}</td>
      <td>${o.listing_url ? `<a href="${o.listing_url}" target="_blank" rel="noopener noreferrer" class="buy-link">View listing ↗</a>` : "-"}</td>
    </tr>`).join("");
  wireCopyButtons(table.querySelector("tbody"));
}

async function loadPatternDb() {
  const res = await fetch("/api/patterns/db");
  state.lastPatternDb = await res.json();
  renderPatternDb(state.lastPatternDb);
}

function renderPatternDb(db) {
  const table = document.getElementById("pattern-db-table");
  if (!table || !db) return;
  const sourceUrls = {};
  for (const s of db.sources || []) sourceUrls[s.key] = s.url;
  table.querySelector("tbody").innerHTML = (db.entries || []).map(e => {
    const floatReq = (e.min_float != null || e.max_float != null)
      ? `${e.min_float != null ? e.min_float : 0} - ${e.max_float != null ? e.max_float : 1}` : "-";
    const src = sourceUrls[e.source]
      ? `<a href="${sourceUrls[e.source]}" target="_blank" rel="noopener noreferrer" class="buy-link">${e.source} ↗</a>`
      : (e.source || "-");
    return `
    <tr>
      <td>${e.label || e.id}</td>
      <td>${e.weapon === "*" ? "any" : e.weapon}</td>
      <td>${e.skin === "*" ? "any" : e.skin}</td>
      <td>${(e.type || "").replaceAll("_", " ")}</td>
      <td class="num">${e.tier != null ? e.tier : "-"}</td>
      <td class="num">${e.premium_multiplier != null ? e.premium_multiplier : "-"}</td>
      <td>${patConfBadge(e.confidence)}</td>
      <td>${floatReq}</td>
      <td>${src}</td>
      <td>${e.notes || ""}</td>
    </tr>`;
  }).join("");
}

// ---- Automated GitHub backup ---------------------------------------------------------------
async function loadBackupStatus() {
  const res = await fetch("/api/backup/status");
  state.lastBackupStatus = await res.json();
  renderBackupStatus(state.lastBackupStatus);
}

function renderBackupStatus(b) {
  const el = document.getElementById("backup-status");
  if (!el || !b) return;
  const last = b.last_result;
  const lastLine = last
    ? `last run: ${last.action}${last.archive ? ` (${last.archive})` : ""}${last.message ? ` -- ${last.message}` : ""}`
    : "no run yet";
  el.textContent = (b.configured
    ? `configured -- watching ${(b.targets || []).join(", ")} every ${(b.wake_interval_seconds / 3600).toFixed(0)}h`
    : `not configured: ${b.configuration_issue || "?"}`)
    + ` | last backup ${fmtTime(b.last_backup_at)}${b.last_archive ? ` (${b.last_archive})` : ""} | ${lastLine}`;
  if (!state.backupFormFilled && b.config) {
    state.backupFormFilled = true;
    document.getElementById("backup-enabled").checked = !!b.config.enabled;
    document.getElementById("backup-repo-dir").value = b.config.repo_dir || "";
    document.getElementById("backup-remote").value = b.config.remote || "origin";
    document.getElementById("backup-branch").value = b.config.branch || "main";
  }
  document.querySelector("#backup-log-table tbody").innerHTML = (b.recent_log || []).map(r => `
    <tr>
      <td>${r.event}</td>
      <td>${r.archive || r.message || r.reason || r.error || ""}</td>
      <td>${fmtTime(r.at)}</td>
    </tr>`).join("");
}

// ---- Connections (CSFloat key / GitHub backup status / NTFY alerts) ------------------------
async function loadConnectionsStatus() {
  const res = await fetch("/api/connections/status");
  state.lastConnectionsStatus = await res.json();
  renderConnectionsStatus(state.lastConnectionsStatus);
}

function _setConnBadge(id, ok, textOk, textOff) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = ok ? textOk : textOff;
  el.className = "conn-badge " + (ok ? "conn-ok" : "conn-off");
}

function _setConnChip(id, label, ok, textOk, textOff) {
  const el = document.getElementById(id);
  if (!el) return;
  const span = el.querySelector("span");
  span.textContent = ok ? textOk : textOff;
  span.className = ok ? "conn-ok" : "conn-off";
  el.childNodes[0].textContent = `${label}: `;
}

function renderConnectionsStatus(c) {
  if (!c) return;

  _setConnBadge("csfloat-badge", c.csfloat.configured, "configured", "not set");
  const csfloatStatusEl = document.getElementById("csfloat-status");
  if (csfloatStatusEl) {
    csfloatStatusEl.textContent = c.csfloat.configured
      ? `current key: ${c.csfloat.masked_key || "(set)"}`
      : (c.csfloat.note || "no key set -- Rare Patterns' CSFloat source and third-party price checks that need it stay skipped, never an error.");
  }

  const backupOk = !!(c.github_backup && c.github_backup.configured);
  _setConnBadge("backup-badge", backupOk, "configured", "not set");

  const ntfyOk = !!(c.ntfy && c.ntfy.configured && c.ntfy.enabled);
  _setConnBadge("ntfy-badge", ntfyOk, "active", c.ntfy && c.ntfy.configured ? "saved, disabled" : "not set");
  if (!state.connectionsFormFilled && c.ntfy) {
    state.connectionsFormFilled = true;
    const enabledEl = document.getElementById("ntfy-enabled");
    const topicEl = document.getElementById("ntfy-topic");
    const serverEl = document.getElementById("ntfy-server");
    const roiEl = document.getElementById("ntfy-roi-threshold");
    const patEl = document.getElementById("ntfy-pattern-threshold");
    if (enabledEl) enabledEl.checked = !!c.ntfy.enabled;
    if (topicEl) topicEl.value = c.ntfy.topic || "";
    if (serverEl) serverEl.value = c.ntfy.server || "https://ntfy.sh";
    if (roiEl && c.ntfy.roi_threshold_pct != null) roiEl.value = c.ntfy.roi_threshold_pct;
    if (patEl && c.ntfy.pattern_threshold_pct != null) patEl.value = c.ntfy.pattern_threshold_pct;
  }

  // Overview page's compact summary chips -- same three connections, terser labels.
  _setConnChip("conn-chip-csfloat", "CSFloat", c.csfloat.configured, "configured", "not set");
  _setConnChip("conn-chip-backup", "GitHub backup", backupOk, "configured", "not set");
  _setConnChip("conn-chip-ntfy", "NTFY alerts", ntfyOk, "active", "off");
}

document.getElementById("csfloat-config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const keyInput = document.getElementById("csfloat-key-input");
  const body = {};
  if (keyInput.value.trim()) body.csfloat_api_key = keyInput.value.trim();
  const res = await fetch("/api/connections/config", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json();
  keyInput.value = "";
  _flashPanelMsg("csfloat-config-msg", data.ok ? "Saved." : (data.error || "Save failed."), !data.ok);
  await loadConnectionsStatus();
});

document.getElementById("ntfy-config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    ntfy_enabled: document.getElementById("ntfy-enabled").checked,
    ntfy_topic: document.getElementById("ntfy-topic").value.trim(),
    ntfy_server: document.getElementById("ntfy-server").value.trim(),
    ntfy_roi_threshold_pct: parseFloat(document.getElementById("ntfy-roi-threshold").value) || 100,
    ntfy_pattern_threshold_pct: parseFloat(document.getElementById("ntfy-pattern-threshold").value) || 100,
  };
  const res = await fetch("/api/connections/config", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json();
  _flashPanelMsg("ntfy-config-msg", data.ok ? "Saved." : (data.error || "Save failed."), !data.ok);
  await loadConnectionsStatus();
});

document.getElementById("ntfy-test-btn").addEventListener("click", async () => {
  const res = await fetch("/api/connections/ntfy/test", { method: "POST" });
  const data = await res.json();
  _flashPanelMsg("ntfy-config-msg", data.ok ? "Test notification sent -- check your phone." : (data.error || "Send failed."), !data.ok);
});

// Market-intelligence accordion badge: sums the row counts of every table inside it so the
// closed-by-default summary still tells you at a glance whether anything's actually flagged,
// without needing a dedicated server endpoint -- these tables are already rendered by their
// own load*/render* functions on every poll, this just counts what's already in the DOM.
function updateMarketIntelBadge() {
  const tableIds = ["dead-skins-table", "mispriced-skins-table", "wear-anomalies-table",
    "market-reliability-table", "invalid-skins-table", "skin-blacklist-table", "price-overrides-table"];
  const total = tableIds.reduce((sum, id) => {
    const tbody = document.querySelector(`#${id} tbody`);
    return sum + (tbody ? tbody.children.length : 0);
  }, 0);
  const badge = document.getElementById("market-intel-badge");
  if (badge) badge.textContent = total > 0 ? `(${total} flagged)` : "(none flagged)";
}

function renderOverview() {
  const sons = state.lastSimPopulation || [];
  const sims = state.lastBestSimulations || [];

  const totalSimulated = sons.reduce((sum, s) => sum + (s.n_simulated || 0), 0);
  const totalInvalid = sons.reduce((sum, s) => sum + (s.n_invalid || 0), 0);
  document.getElementById("kpi-analyzed").textContent =
    (totalSimulated + totalInvalid + state.tradeups.length).toLocaleString();

  const bestRoi = sims.length ? Math.max(...sims.map(s => s.expected_roi_pct)) : null;
  const bestEl = document.getElementById("kpi-best-roi");
  bestEl.textContent = bestRoi != null ? `${fmtMoney(bestRoi)}%` : "-";
  bestEl.className = "kpi-value" + (bestRoi != null ? (bestRoi >= 0 ? " profit-positive" : " profit-negative") : "");

  const avgRoi = sims.length ? sims.reduce((sum, s) => sum + s.expected_roi_pct, 0) / sims.length : null;
  document.getElementById("kpi-avg-roi").textContent = avgRoi != null ? `${fmtMoney(avgRoi)}%` : "-";

  document.getElementById("kpi-valid-invalid").textContent =
    (totalSimulated || totalInvalid) ? `${totalSimulated} valid / ${totalInvalid} invalid` : "-";

  const avgConfidence = sims.length
    ? sims.reduce((sum, s) => sum + (s.confidence_score || 0), 0) / sims.length : null;
  document.getElementById("kpi-confidence").textContent = avgConfidence != null ? avgConfidence.toFixed(0) : "-";

  const totalRounds = sons.reduce((sum, s) => sum + (s.round || 0), 0);
  const modelsTrained = sons.filter(s => s.sim_model_status && s.sim_model_status.trained).length;
  document.getElementById("kpi-learning").textContent = sons.length
    ? `${totalRounds} rounds, ${modelsTrained}/${sons.length} models trained` : "-";
}

function _ensureAgentFilterOptions(sons) {
  ["opp-agent-filter", "archive-agent-filter"].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel || sel.options.length > 1) return;
    sons.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s.son_id;
      opt.textContent = s.agent_name || `Son ${s.son_id}`;
      sel.appendChild(opt);
    });
  });
}

// Radar chart comparing the 4 agents' 8 behavioral parameters -- lets a user see at a
// glance how e.g. the Collection Mixer's high collection_mixing_bias contrasts with the
// ROI Accountant's high roi_weight/liquidity_importance, instead of just reading numbers.
// s.profile carries the LIVE self-tuned values for the five adaptive search params (see
// src/adaptive.py / sim_loop.snapshot), so this chart drifts in real time as agents adapt.
function renderAgentRadarChart(sons) {
  const ctx = document.getElementById("agent-radar-chart");
  if (!ctx || typeof Chart === "undefined" || !sons.length || !sons[0].profile) return;
  const labels = ["Exploration", "Risk tolerance", "Collection mixing", "Wear optimization",
                   "Liquidity importance", "ROI weight", "Mutation strength", "Memory weight"];
  const keys = ["exploration_rate", "risk_tolerance", "collection_mixing_bias", "wear_optimization_bias",
                "liquidity_importance", "roi_weight", "mutation_strength", "memory_weight"];
  const datasets = sons.map((s, i) => ({
    label: s.agent_name || `Son ${s.son_id}`,
    data: keys.map(k => s.profile[k]),
    borderColor: AGENT_PALETTE[i % AGENT_PALETTE.length],
    backgroundColor: AGENT_PALETTE[i % AGENT_PALETTE.length] + "22",
    pointRadius: 2,
  }));
  if (state.charts.agentRadar) {
    state.charts.agentRadar.data.datasets = datasets;
    state.charts.agentRadar.update();
    return;
  }
  state.charts.agentRadar = new Chart(ctx, {
    type: "radar",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: CHART_TEXT_COLOR } } },
      scales: { r: { min: 0, max: 1, ticks: { display: false }, pointLabels: { color: CHART_AXIS_COLOR, font: { size: 10 } },
                     grid: { color: "rgba(255,255,255,0.08)" }, angleLines: { color: "rgba(255,255,255,0.08)" } } },
    },
  });
}

// How to format each selectable stat's value/axis in the agent-stats-over-rounds chart --
// keyed by the <option value> in #agent-stat-select (index.html).
const AGENT_STAT_META = {
  best_roi_ever: { axisLabel: "Best ROI % so far", fmt: v => `${fmtMoney(v)}%` },
  avg_roi_pct: { axisLabel: "Avg ROI % this round", fmt: v => `${fmtMoney(v)}%` },
  avg_input_float: { axisLabel: "Preferred input float (normalized)", fmt: v => v.toFixed(3) },
  avg_confidence: { axisLabel: "Avg confidence score", fmt: v => v.toFixed(1) },
  cumulative_expected_profit: { axisLabel: "Cumulative expected profit (if bought all)", fmt: v => fmtCurrency(v) },
  n_simulated: { axisLabel: "Simulated trials so far (cumulative)", fmt: v => v.toLocaleString() },
};

// Multi-line chart, one line per agent/"son", plotting whichever stat is picked in
// #agent-stat-select against ROUND NUMBER (not array index -- a paused/stuck son can have
// fewer rounds than the others, so {x,y} points on a linear x scale keep each agent's own
// round numbering intact instead of forcing every line onto one shared label axis). Toggling
// an agent's line on/off is Chart.js's own default legend-click behavior, but Chart.js only
// remembers that toggle on the EXISTING dataset objects -- since this function rebuilds a
// brand-new `datasets` array from scratch every 3s poll tick (fresh history from the server),
// naively reassigning `chart.data.datasets` would silently reset every line back to visible
// the moment a user toggled one off (same failure shape as any periodic full-rebuild
// clobbering async/interactive UI state elsewhere in this dashboard -- see the verify-prices
// button's own state.simVerifyResults cache for the same rule applied to a different widget).
// So each new dataset's `hidden` flag is explicitly carried over from the chart's CURRENT
// per-index visibility before the array is replaced.
function renderAgentStatsChart(sons) {
  const ctx = document.getElementById("agent-stats-chart");
  if (!ctx || typeof Chart === "undefined" || !sons.length) return;
  const statSelect = document.getElementById("agent-stat-select");
  const stat = statSelect ? statSelect.value : "best_roi_ever";
  const meta = AGENT_STAT_META[stat] || AGENT_STAT_META.best_roi_ever;
  const existing = state.charts.agentStats;

  const datasets = sons.map((s, i) => ({
    label: s.agent_name || `Son ${s.son_id}`,
    data: (s.history || [])
      .filter(h => h[stat] != null)
      .map(h => ({ x: h.round, y: h[stat] })),
    borderColor: AGENT_PALETTE[i % AGENT_PALETTE.length],
    backgroundColor: AGENT_PALETTE[i % AGENT_PALETTE.length] + "22",
    tension: 0.2, pointRadius: 0, borderWidth: 2,
    hidden: existing ? !existing.isDatasetVisible(i) : false,
  }));

  if (existing) {
    existing.data.datasets = datasets;
    existing.options.scales.y.title.text = meta.axisLabel;
    existing.update();
    return;
  }
  state.charts.agentStats = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: CHART_TEXT_COLOR, boxWidth: 12, font: { size: 10 } } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${meta.fmt(ctx.parsed.y)}` } },
      },
      scales: {
        x: { type: "linear", title: { display: true, text: "Round", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR } },
        y: { title: { display: true, text: meta.axisLabel, color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
      },
    },
  });
}

// Full-lifetime ROI history, on demand (Results page) -- reads round_history.jsonl straight off
// disk via /api/sim-population/round-history, which never trims (unlike the checkpoint-backed
// `history` the agent-stats chart above uses, capped at the last 300 rounds). Same on-open
// loading pattern as the opportunity archive (state.lifetimeRoiLoaded).
async function loadLifetimeRoiHistory() {
  const res = await fetch("/api/sim-population/round-history");
  const data = await res.json();
  renderLifetimeRoiChart(data);
}

function renderLifetimeRoiChart(historyBySon) {
  const ctx = document.getElementById("lifetime-roi-chart");
  if (!ctx || typeof Chart === "undefined") return;
  const sons = state.lastSimPopulation || [];
  const datasets = Object.entries(historyBySon).map(([sonIdStr, history], i) => {
    const sonId = Number(sonIdStr);
    const agent = sons.find(s => s.son_id === sonId);
    return {
      label: (agent && agent.agent_name) || `Son ${sonId}`,
      data: (history || []).filter(h => h.avg_roi_pct != null).map(h => ({ x: h.round, y: h.avg_roi_pct })),
      borderColor: AGENT_PALETTE[i % AGENT_PALETTE.length],
      backgroundColor: AGENT_PALETTE[i % AGENT_PALETTE.length] + "22",
      tension: 0.2, pointRadius: 0, borderWidth: 2,
    };
  });
  if (state.charts.lifetimeRoi) {
    state.charts.lifetimeRoi.data.datasets = datasets;
    state.charts.lifetimeRoi.update();
    return;
  }
  state.charts.lifetimeRoi = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: CHART_TEXT_COLOR, boxWidth: 12, font: { size: 10 } } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtMoney(c.parsed.y)}%` } },
      },
      scales: {
        x: { type: "linear", title: { display: true, text: "Round", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR } },
        y: { title: { display: true, text: "Avg ROI % that round", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
      },
    },
  });
}

// "If you'd bought literally every trade every agent ever proposed" -- see sim_loop.py's
// _empty_checkpoint docstring for why this is a defensible EV sum rather than a random guess.
function renderSimBankroll(sons) {
  if (!sons.length) return;
  const totalCost = sons.reduce((sum, s) => sum + (s.cumulative_cost || 0), 0);
  const totalProfit = sons.reduce((sum, s) => sum + (s.cumulative_expected_profit || 0), 0);

  document.getElementById("bankroll-spent").textContent = fmtCurrency(totalCost);
  const netEl = document.getElementById("bankroll-net");
  netEl.textContent = fmtCurrency(totalProfit);
  netEl.className = "kpi-value" + (totalProfit >= 0 ? " profit-positive" : " profit-negative");
  const roiEl = document.getElementById("bankroll-roi");
  const roi = totalCost ? (totalProfit / totalCost * 100) : null;
  roiEl.textContent = roi != null ? `${fmtMoney(roi)}%` : "-";
  roiEl.className = "kpi-value" + (roi != null ? (roi >= 0 ? " profit-positive" : " profit-negative") : "");

  document.getElementById("sim-bankroll-per-agent").innerHTML = sons.map((s, i) => {
    const cost = s.cumulative_cost || 0;
    const profit = s.cumulative_expected_profit || 0;
    const agentRoi = cost ? (profit / cost * 100) : null;
    return `<span><span class="agent-dot" style="background:${AGENT_PALETTE[i % AGENT_PALETTE.length]}"></span>` +
      `${s.agent_name || `Son ${s.son_id}`}: ${fmtCurrency(cost)} spent, ` +
      `${agentRoi != null ? fmtMoney(agentRoi) + "%" : "-"} blended ROI</span>`;
  }).join("");
}

// Overlapping "money if you'd bought every trade" chart: one line per agent, plotting each
// agent's running NET expected profit (cumulative_expected_profit, already net of cost -- see
// sim_loop._empty_checkpoint) against round number. All agents share one axis so the current
// front-runner is just whichever line sits highest at the right edge. Same {x,y}-on-a-linear-x
// approach and same carry-over-hidden-flag rule as renderAgentStatsChart (a paused agent has
// fewer rounds; a legend toggle must survive the 3s poll rebuild).
function renderSimBankrollChart(sons) {
  const ctx = document.getElementById("sim-bankroll-chart");
  if (!ctx || typeof Chart === "undefined" || !sons.length) return;
  const existing = state.charts.simBankroll;
  const datasets = sons.map((s, i) => ({
    label: s.agent_name || `Son ${s.son_id}`,
    data: (s.history || [])
      .filter(h => h.cumulative_expected_profit != null)
      .map(h => ({ x: h.round, y: h.cumulative_expected_profit })),
    borderColor: AGENT_PALETTE[i % AGENT_PALETTE.length],
    backgroundColor: AGENT_PALETTE[i % AGENT_PALETTE.length] + "22",
    tension: 0.2, pointRadius: 0, borderWidth: 2,
    hidden: existing ? !existing.isDatasetVisible(i) : false,
  }));

  if (existing) {
    existing.data.datasets = datasets;
    existing.update();
    return;
  }
  state.charts.simBankroll = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: CHART_TEXT_COLOR, boxWidth: 12, font: { size: 10 } } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtCurrency(c.parsed.y)}` } },
      },
      scales: {
        x: { type: "linear", title: { display: true, text: "Round", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR } },
        y: { title: { display: true, text: "Net if bought everything", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
      },
    },
  });
}

// Compact status strip above the son cards -- round / n_simulated / n_invalid / stuck flag /
// temperature for all 4 agents side by side, so a frozen agent (round 0 while others progress)
// is visible in one glance instead of requiring a scroll through 4 separate .son-card blocks.
// Fully rebuilt from `sons` (== state.lastSimPopulation) every poll, same as the rest of this
// panel -- nothing async/on-demand is written here, so there's nothing for the 3s rebuild to wipe.
function renderSonHealthStrip(sons) {
  const tbody = document.querySelector("#son-health-table tbody");
  if (!tbody) return;
  tbody.innerHTML = sons.map(s => {
    const total = (s.n_simulated || 0) + (s.n_invalid || 0);
    const invalidPct = total ? (s.n_invalid / total * 100) : 0;
    const rowClass = s.stuck ? "stuck" : (s.round === 0 ? "round-zero" : "");
    const flag = s.stuck
      ? `<span class="stuck-badge">STUCK</span>`
      : (s.round === 0 ? `<span class="nudging-badge">round 0</span>` : "-");
    return `
    <tr class="son-health-row ${rowClass}">
      <td>${s.agent_name || `Son ${s.son_id}`}</td>
      <td>${s.status}</td>
      <td class="num">${s.round}</td>
      <td class="num">${(s.n_simulated || 0).toLocaleString()}</td>
      <td class="num">${(s.n_invalid || 0).toLocaleString()}</td>
      <td class="num">${invalidPct.toFixed(0)}%</td>
      <td>${flag}</td>
      <td class="num">${s.temperature.toFixed(2)}</td>
    </tr>`;
  }).join("");
}

// sim_learning.get_model_status()/sim_roi_model_meta.json only ever stores the CURRENT model's
// validation_mae -- never a history of past trainings (unlike checkpoint.json's own `history`
// array, which tracks ROI/profit stats per round but was never given an MAE field). So this
// samples validation_mae on every poll and appends a point only when a NEW training actually
// happened (trained_at changed since the last point recorded for this son), building a genuine
// client-side trend instead of re-plotting the same static value. Written to state.simMaeHistory
// BEFORE rendering -- same "cache in state, don't just paint the DOM" rule the poll rebuild
// requires everywhere else on this dashboard -- so a later poll can only ever append, never wipe.
function _recordSimMaeHistory(sons) {
  sons.forEach(s => {
    const ms = s.sim_model_status;
    if (!ms || !ms.trained || ms.validation_mae == null) return;
    const hist = state.simMaeHistory[s.son_id] || (state.simMaeHistory[s.son_id] = []);
    const last = hist[hist.length - 1];
    if (last && last.trained_at === ms.trained_at) return;  // no new training since last poll
    hist.push({ round: s.round, mae: ms.validation_mae, trained_at: ms.trained_at });
    if (hist.length > 200) hist.shift();  // cap memory growth over a long-running session
  });
}

// Multi-line chart, one line per agent, plotting the client-accumulated MAE trend from
// state.simMaeHistory (see _recordSimMaeHistory above) against the round it was trained as of.
// Same {x,y}-on-a-linear-x-axis and carry-over-hidden-flag approach as renderAgentStatsChart,
// for the same reason (a paused/round-0 agent has fewer -- or zero -- points than the others).
function renderSimMaeChart(sons) {
  const chartWrap = document.getElementById("sim-mae-chart-wrap");
  const empty = document.getElementById("sim-mae-empty");
  const ctx = document.getElementById("sim-mae-chart");
  if (!ctx || typeof Chart === "undefined") return;
  const sonIds = Object.keys(state.simMaeHistory).filter(id => state.simMaeHistory[id].length);
  if (!sonIds.length) {
    if (chartWrap) chartWrap.style.display = "none";
    if (empty) empty.style.display = "block";
    return;
  }
  if (chartWrap) chartWrap.style.display = "";
  if (empty) empty.style.display = "none";
  const existing = state.charts.simMae;
  const datasets = sonIds.map((id, i) => {
    const idx = Number(id);
    const s = sons.find(x => x.son_id === idx);
    return {
      label: (s && s.agent_name) || `Son ${id}`,
      data: state.simMaeHistory[id].map(h => ({ x: h.round, y: h.mae })),
      borderColor: AGENT_PALETTE[idx % AGENT_PALETTE.length],
      backgroundColor: AGENT_PALETTE[idx % AGENT_PALETTE.length] + "22",
      tension: 0.2, pointRadius: 2, borderWidth: 2,
      hidden: existing ? !existing.isDatasetVisible(i) : false,
    };
  });
  if (existing) {
    existing.data.datasets = datasets;
    existing.update();
    return;
  }
  state.charts.simMae = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: CHART_TEXT_COLOR, boxWidth: 12, font: { size: 10 } } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(2)}pp MAE` } },
      },
      scales: {
        x: { type: "linear", title: { display: true, text: "Round (as of that training)", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR } },
        y: { title: { display: true, text: "Validation MAE (pp, lower = better)", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
      },
    },
  });
}

// One small-multiple card + chart per agent (created once, reused after -- same lazy-create-
// then-reuse pattern as _ensureSonFeatureCharts, so this survives the 3s poll rebuild without
// flashing/recreating canvases, which would otherwise leak Chart.js instances).
function _ensureSonDataQualityCharts(sons) {
  const container = document.getElementById("sim-data-quality-wrap");
  if (!container || container.querySelectorAll(".sim-feature-card").length === sons.length) return;
  container.innerHTML = sons.map(s => `
    <div class="sim-feature-card" data-son="${s.son_id}">
      <h5>${s.agent_name || `Son ${s.son_id}`}</h5>
      <div class="chart-wrap"><canvas></canvas></div>
    </div>`).join("");
}

// Per-agent snapshot of training-data health, split into 4 separate small-multiple charts (one
// per agent, same grid as the feature-importance small multiples below) instead of one shared
// grouped-bar chart -- easier to compare an agent against its OWN history of totals at a glance.
// Three categories, all-time cumulative totals from that son's checkpoint: Trainable (priced AND
// liquid enough that sim_learning.train() actually fits on it), Low-volume (priced fine, but
// thin-liquidity enough that low_volume_warning excludes it from training -- see
// SimulationRecord's own docstring), and Invalid (no Steam/third-party price at all, never even
// becomes a SimulationRecord -- see simulate.InvalidSimulation). Previously this chart only
// tracked Valid vs. Invalid, which silently miscounted Low-volume rows as "trainable" even
// though sim_learning.py's train() excludes them -- see n_low_volume/n_trainable on the son
// snapshot. Degrades to three empty bars (0/0/0) for a round-0 agent rather than erroring.
function renderSimDataQualityChart(sons) {
  _ensureSonDataQualityCharts(sons);
  if (!state.charts.sonDataQuality) state.charts.sonDataQuality = {};
  if (typeof Chart === "undefined") return;
  sons.forEach(s => {
    const card = document.querySelector(`#sim-data-quality-wrap .sim-feature-card[data-son="${s.son_id}"]`);
    if (!card) return;
    const trainable = (s.n_trainable != null ? s.n_trainable : (s.n_simulated || 0) - (s.n_low_volume || 0));
    const lowVolume = s.n_low_volume || 0;
    const invalid = s.n_invalid || 0;
    const data = [trainable, lowVolume, invalid];
    const existing = state.charts.sonDataQuality[s.son_id];
    if (existing) {
      existing.data.datasets[0].data = data;
      existing.update();
      return;
    }
    const ctx = card.querySelector("canvas");
    state.charts.sonDataQuality[s.son_id] = new Chart(ctx, {
      type: "bar",
      data: {
        labels: ["Trainable", "Low-volume", "Invalid"],
        datasets: [{
          data,
          backgroundColor: ["rgba(76,175,80,0.7)", "rgba(212,175,55,0.7)", "rgba(235,75,75,0.7)"],
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
          y: { beginAtZero: true, ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
        },
      },
    });
  });
}

// One .sim-feature-card per agent (created once, reused after) -- same lazy-create-then-reuse
// pattern as _ensureSonCards, so this survives the 3s poll rebuild without flashing/recreating
// canvases (which would otherwise leak Chart.js instances).
function _ensureSonFeatureCharts(sons) {
  const container = document.getElementById("sim-feature-wrap");
  if (!container || container.querySelectorAll(".sim-feature-card").length === sons.length) return;
  container.innerHTML = sons.map(s => `
    <div class="sim-feature-card" data-son="${s.son_id}">
      <h5>${s.agent_name || `Son ${s.son_id}`}</h5>
      <div class="chart-wrap"><canvas></canvas></div>
      <p class="empty-state" data-field="empty" style="display:none;"></p>
    </div>`).join("");
}

// Small-multiples grid, one horizontal permutation-importance bar chart per agent's OWN sim-
// model (src/sim_learning.py) -- same chart shape as renderFeatureImportanceChart above for the
// real model (Methodology section), just once per agent instead of once for the whole dashboard.
// Degrades to a plain "not trained yet" note per card for any agent below
// sim_learning.MIN_TRAINING_SAMPLES (son_1/son_2 at round 0, son_3 with no model dir at all)
// rather than erroring on a missing feature_importances field.
function renderSimFeatureImportanceGrid(sons) {
  _ensureSonFeatureCharts(sons);
  if (!state.charts.sonFeatureImportance) state.charts.sonFeatureImportance = {};
  sons.forEach(s => {
    // Scoped to #sim-feature-wrap: the data-quality grid reuses the same .sim-feature-card
    // class (and sits earlier in the DOM), so an unscoped query matches ITS card instead --
    // which has no [data-field="empty"] element, crashing the whole renderSimPopulation pass
    // for any agent without a trained model. Same scoping renderSimDataQualityChart already uses.
    const card = document.querySelector(`#sim-feature-wrap .sim-feature-card[data-son="${s.son_id}"]`);
    if (!card) return;
    const chartWrap = card.querySelector(".chart-wrap");
    const emptyEl = card.querySelector('[data-field="empty"]');
    const ms = s.sim_model_status;
    if (!ms || !ms.trained || !ms.feature_importances) {
      chartWrap.style.display = "none";
      emptyEl.style.display = "block";
      emptyEl.textContent = `not trained yet (need ${ms ? ms.min_samples_required : "?"}+ volume-confirmed sims)`;
      return;
    }
    chartWrap.style.display = "";
    emptyEl.style.display = "none";
    const entries = Object.entries(ms.feature_importances).sort((a, b) => b[1] - a[1]);
    const labels = entries.map(e => e[0]);
    const data = entries.map(e => e[1]);
    const ctx = chartWrap.querySelector("canvas");
    const existing = state.charts.sonFeatureImportance[s.son_id];
    if (existing) {
      existing.data.labels = labels;
      existing.data.datasets[0].data = data;
      existing.update();
      return;
    }
    if (typeof Chart === "undefined") return;
    state.charts.sonFeatureImportance[s.son_id] = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets: [{ label: "Permutation importance", data, backgroundColor: AGENT_PALETTE[s.son_id % AGENT_PALETTE.length] }] },
      options: {
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
          y: { ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } },
        },
      },
    });
  });
}

async function loadSimLeaderboards() {
  const res = await fetch("/api/sim-population/leaderboards");
  state.lastSimLeaderboards = await res.json();
  renderSimLeaderboards(state.lastSimLeaderboards);
}

function renderSimLeaderboards(data) {
  const collections = data.collections || [];
  document.getElementById("profitable-collections-table").style.display = collections.length ? "" : "none";
  document.getElementById("profitable-collections-empty").style.display = collections.length ? "none" : "block";
  document.querySelector("#profitable-collections-table tbody").innerHTML = collections.map(c => `
    <tr>
      <td>${c.collection}</td>
      <td class="num">${c.count}</td>
      <td class="num">${fmtMoney(c.avg_roi_pct)}%</td>
      <td class="num">${fmtMoney(c.best_roi_pct)}%</td>
      <td class="num">${fmtCurrency(c.total_expected_profit)}</td>
    </tr>`).join("");

  const skins = data.skins || [];
  document.getElementById("profitable-skins-table").style.display = skins.length ? "" : "none";
  document.getElementById("profitable-skins-empty").style.display = skins.length ? "none" : "block";
  document.querySelector("#profitable-skins-table tbody").innerHTML = skins.map(s => `
    <tr>
      <td>${copyableName(s.skin, s.image)}</td>
      <td>${s.wear}</td>
      <td class="num">${s.appearances}</td>
      <td class="num">${fmtCurrency(s.weighted_profit)}</td>
    </tr>`).join("");
}

async function loadConvergenceStatus() {
  const res = await fetch("/api/sim-population/convergence");
  renderConvergenceBanner(await res.json());
}

function renderConvergenceBanner(c) {
  const el = document.getElementById("convergence-banner");
  if (!el) return;
  if (!c.last_nudged || !c.last_nudged.length) {
    el.style.display = "none";
    return;
  }
  const latest = c.last_nudged[0];
  const region = Array.isArray(latest.signature) ? latest.signature.slice(0, 3).join(" -> ") : "a shared region";
  el.style.display = "block";
  el.textContent = `Anti-convergence: agents ${latest.son_ids.join(", ")} settled on the same strategy (${region}) -- automatically nudged toward broader exploration at ${fmtTime(latest.at)}.`;
}

function _ensureSonCards(sons) {
  const container = document.getElementById("sim-population-container");
  if (container.querySelectorAll(".son-card").length === sons.length) return;
  container.innerHTML = sons.map(s => `
    <div class="son-card" data-son="${s.son_id}">
      <h5>${s.agent_name || `Son ${s.son_id}`} <span class="muted-inline">(temp ${s.temperature.toFixed(2)} -- ${_temperatureLabel(s.temperature)})</span></h5>
      <div class="muted-inline agent-purpose">${s.agent_purpose || ""}</div>
      <div class="son-stats" data-field="stats">loading&hellip;</div>
      <div class="son-chart-wrap"><canvas></canvas></div>
      <div class="son-controls">
        <button data-action="start">Start</button>
        <button data-action="pause" class="secondary">Pause</button>
        <button data-action="stop" class="secondary">Stop</button>
        <button data-action="nudge" class="secondary" title="Soft retry: temporarily boosts exploration, keeps its data/model">Nudge</button>
        <button data-action="reset" class="secondary" title="Hard retry: wipes this son's data/model and starts it over">Full reset</button>
      </div>
    </div>`).join("");
  container.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const card = btn.closest(".son-card");
    const sonId = card.dataset.son;
    const action = btn.dataset.action;
    if (action === "reset" && !confirm(`Fully reset son ${sonId}? This wipes its logged simulations and trained model -- cannot be undone.`)) return;
    btn.disabled = true;
    try {
      await fetch(`/api/sim-population/${sonId}/${action}`, { method: "POST" });
      await loadSimPopulationStatus();
    } finally {
      btn.disabled = false;
    }
  });
}

function renderSimPopulation(sons) {
  _ensureSonCards(sons);
  renderSonHealthStrip(sons);
  renderAgentRadarChart(sons);
  renderAgentStatsChart(sons);
  renderSimBankroll(sons);
  renderSimBankrollChart(sons);
  _recordSimMaeHistory(sons);
  renderSimMaeChart(sons);
  renderSimDataQualityChart(sons);
  renderSimFeatureImportanceGrid(sons);
  if (!state.charts.sonHistory) state.charts.sonHistory = {};
  sons.forEach(s => {
    const card = document.querySelector(`.son-card[data-son="${s.son_id}"]`);
    if (!card) return;
    const progress = s.progress && s.progress.total ? ` (${s.progress.current}/${s.progress.total})` : "";
    const model = s.sim_model_status && s.sim_model_status.trained
      ? `model trained on ${s.sim_model_status.n_samples} sims (MAE ${s.sim_model_status.validation_mae != null ? s.sim_model_status.validation_mae.toFixed(1) + "pp" : "?"})`
      : `model not trained yet (need ${s.sim_model_status ? s.sim_model_status.min_samples_required : "?"}+)`;
    const stuckBadge = s.stuck ? `<span class="stuck-badge">STUCK -- ${s.rounds_since_improvement} rounds w/o improvement</span>` : "";
    const nudgeBadge = s.nudge_rounds_remaining > 0 ? `<span class="nudging-badge">nudged, ${s.nudge_rounds_remaining} rounds left (temp x${(s.effective_temperature / s.temperature).toFixed(1)})</span>` : "";
    // Exploration Recovery Mode (src/recovery.py): engages only after a nudge has demonstrably
    // not been enough, escalates levels 1-4, then bounds-clamped random perturbations.
    const rec = s.recovery;
    const recoveryBadge = rec && rec.active
      ? `<span class="recovery-badge" title="Progressive escalation: mutation/exploration/novelty up, exploitation down -- exits the moment a real improvement lands. See the Exploration Recovery Mode panel below for the event log.">RECOVERY MODE lvl ${rec.level}${rec.perturbations ? ` -- ${rec.perturbations} perturbation${rec.perturbations > 1 ? "s" : ""}` : ""}</span>`
      : "";
    // Self-tuning status (src/adaptive.py): this agent A/B-tests small tweaks of its own
    // search preferences every few rounds, keeping only ones that beat the previous window's
    // ROI-first + novelty reward. The radar chart above already plots the LIVE (drifted)
    // parameter values from s.profile, so this line just narrates where in that cycle it is.
    const a = s.adaptive;
    const adaptLine = a
      ? `self-tuning: ${a.phase === "trial" ? "testing tweaked preferences" : "measuring current preferences"} `
        + `(round ${a.window_rounds}/${a.window_size} of window) | ${a.adaptations_adopted}/${a.adaptations_tried} tweaks kept`
        + `${a.last_reward != null ? ` | last window reward ${fmtMoney(a.last_reward)}pp` : ""}`
        + ` | ${s.coverage_collections || 0} collections explored`
      : "self-tuning: starts with the first completed round";
    card.querySelector('[data-field="stats"]').innerHTML = `
      status: <strong>${s.status}</strong>${progress}<br>
      round ${s.round} | ${s.n_simulated} simulated | ${s.n_invalid} invalid (missing price data) | best ROI ever:
      <span class="${s.best_roi_ever != null && s.best_roi_ever >= 0 ? "profit-positive" : "profit-negative"}">${s.best_roi_ever != null ? fmtMoney(s.best_roi_ever) + "%" : "-"}</span><br>
      ${model}<br>
      <span class="muted-inline">${adaptLine}</span><br>
      ${stuckBadge} ${nudgeBadge} ${recoveryBadge}<br>
      updated ${fmtTime(s.updated_at)}`;
    // While the round-robin scheduler owns the population, it's the one flipping each son's
    // pause_event in turn -- disable the per-son Start/Pause/Stop so a manual click can't race
    // with it (Nudge/Full reset are unaffected, those don't touch pause state).
    const roundRobinActive = state.lastRoundRobin.enabled;
    card.classList.toggle("active-turn", roundRobinActive && state.lastRoundRobin.active_son_id === s.son_id);
    card.querySelector('[data-action="start"]').disabled = roundRobinActive || s.status === "running";
    card.querySelector('[data-action="pause"]').disabled = roundRobinActive || s.status !== "running";
    card.querySelector('[data-action="stop"]').disabled = roundRobinActive || s.status === "paused" || s.status === "idle";

    const ctx = card.querySelector("canvas");
    const labels = s.history.map(h => h.round);
    const data = s.history.map(h => h.best_roi_ever);
    if (state.charts.sonHistory[s.son_id]) {
      state.charts.sonHistory[s.son_id].data.labels = labels;
      state.charts.sonHistory[s.son_id].data.datasets[0].data = data;
      state.charts.sonHistory[s.son_id].update();
    } else if (typeof Chart !== "undefined") {
      state.charts.sonHistory[s.son_id] = new Chart(ctx, {
        type: "line",
        data: { labels, datasets: [{ label: "Best ROI % so far", data, borderColor: "#8ea1ff",
                                      backgroundColor: "rgba(142,161,255,0.15)", fill: true, tension: 0.2, pointRadius: 0 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { display: false }, y: { ticks: { color: CHART_AXIS_COLOR, font: { size: 9 } } } },
        },
      });
    }
  });
}

async function loadBestSimulations() {
  const res = await fetch("/api/sim-population/best-simulations");
  state.lastBestSimulations = await res.json();
  renderBestSimulations(state.lastBestSimulations);
}

function _riskLevel(confidenceScore) {
  if (confidenceScore == null) return "medium";
  if (confidenceScore >= 70) return "low";
  if (confidenceScore >= 40) return "medium";
  return "high";
}

function _riskBadge(confidenceScore) {
  const level = _riskLevel(confidenceScore);
  const label = level === "low" ? "low risk" : level === "medium" ? "medium risk" : "high risk";
  return `<span class="confidence-badge risk-${level}">${confidenceScore != null ? confidenceScore.toFixed(0) : "?"} (${label})</span>`;
}

function _deadSkinRiskBadge(s) {
  const units = [...(s.input_mix || []), ...(s.output_distribution || [])];
  const worst = units.reduce((m, u) => Math.max(m, u.dead_skin_score || 0), 0);
  return worst > 0 ? `<span class="dead-skin-badge">score ${worst.toFixed(0)}</span>` : `<span class="tag-same">clean</span>`;
}

function _applyOpportunityFilters(sims) {
  const f = state.opportunityFilters;
  return sims.filter(s => {
    if (f.minRoi != null && !isNaN(f.minRoi) && s.expected_roi_pct < f.minRoi) return false;
    if (f.risk && _riskLevel(s.confidence_score) !== f.risk) return false;
    if (f.agent !== "" && String(s.son) !== String(f.agent)) return false;
    return true;
  });
}

function renderSimInputs(inputMix) {
  const totalCount = inputMix.reduce((sum, m) => sum + m.count, 0);
  const sorted = [...inputMix].sort((a, b) => b.count - a.count);
  return `
    <strong>Inputs (${totalCount} total)</strong>
    <table class="drops-table">
      <thead><tr><th>Skin</th><th>Wear</th><th>Count</th><th>Unit price</th><th>Volume</th><th>Line total</th></tr></thead>
      <tbody>
        ${sorted.map(m => `
          <tr>
            <td>${copyableName(m.skin, m.image)}</td>
            <td>${m.wear}</td>
            <td class="num">×${m.count}</td>
            <td class="num">${fmtCurrency(m.unit_price)}</td>
            <td class="num">${m.volume != null ? m.volume : "-"} ${lowVolumeBadge(m.volume)}</td>
            <td class="num">${fmtCurrency(m.unit_price * m.count)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function confidenceBadge(confidence) {
  return confidence === "ok" ? `<span class="tag-same">ok</span>` : `<span class="tag-drought">${confidence}</span>`;
}

function renderVerifyRows(rows) {
  if (!rows.length) return "<em>No priced rows to verify.</em>";
  return `
    <table class="drops-table">
      <thead><tr>
        <th>Role</th><th>Skin</th><th>Steam</th><th>Skinport</th><th>Buff163</th><th>CSFloat</th>
        <th>Cheapest</th><th>Gap %</th><th>Used for ROI</th><th>Confidence</th>
      </tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td>${r.role}</td>
            <td>${r.skin} (${r.wear})</td>
            <td class="num">${r.steam_price != null ? fmtCurrency(r.steam_price) : "-"}</td>
            <td class="num">${r.skinport_price != null ? fmtCurrency(r.skinport_price) : "-"}</td>
            <td class="num">${r.buff163_price != null ? fmtCurrency(r.buff163_price) : "-"}</td>
            <td class="num">${r.csfloat_price != null ? fmtCurrency(r.csfloat_price) : "-"}</td>
            <td class="num">${r.cheapest_price != null ? fmtCurrency(r.cheapest_price) + " (" + r.cheapest_source + ")" : "-"}</td>
            <td class="num">${r.steam_vs_cheapest_pct != null ? fmtMoney(r.steam_vs_cheapest_pct) + "%" : "-"}</td>
            <td class="num">${r.consensus_price != null
              ? fmtCurrency(r.consensus_price) + (r.high_spread
                ? ` <span class="tag-drought" title="The ${r.n_sources} sources that returned a price disagree by ${fmtMoney(r.source_spread_pct)}% -- the verified ROI uses their MEDIAN instead of trusting the cheapest single site">median of ${r.n_sources}</span>`
                : ` <span class="tag-same" title="${r.source_spread_pct != null ? "Sources agree (spread " + fmtMoney(r.source_spread_pct) + "%, under threshold)" : "Only one source returned a price"} -- cheapest price used as-is">cheapest</span>`)
              : "-"}</td>
            <td>${confidenceBadge(r.price_confidence)}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderSimulationDetail(s) {
  const outputsHtml = renderDrops(s.output_distribution.map(o => ({
    name: o.skin, image: o.image, wear: o.wear, probability_pct: o.prob_pct,
    price: o.price, volume: o.volume, profit_after_fee: o.net_profit, max_input_price: null,
    wear_probs: o.wear_probs,
  })));
  const idAttr = encodeURIComponent(s.id);
  // Replay a cached verify result if one exists -- renderBestSimulations() rebuilds this whole
  // block on every 3s poll, so without this a result would flash and disappear before the user
  // could read it (see state.simVerifyResults' own comment).
  const cached = state.simVerifyResults[s.id];
  const cachedHtml = cached ? (cached.error ? `<em>Verification failed: ${cached.error}</em>` : renderVerifyRows(cached.rows)) : "";
  return `
    <div class="sim-detail">
      ${renderSimInputs(s.input_mix)}
      <strong>Possible outputs</strong>
      ${outputsHtml}
      <div class="sim-verify">
        <button class="secondary verify-prices-btn" data-id="${idAttr}">Verify prices vs Steam/Skinport/Buff163/CSFloat</button>
        <button class="secondary blacklist-btn" data-id="${idAttr}" title="You already executed this trade-up -- stop showing it and stop the agents re-proposing it">Mark done / blacklist</button>
        <div class="verify-result" data-id="${idAttr}">${cachedHtml}</div>
      </div>
    </div>`;
}

function wireVerifyButtons(tbody, sims) {
  tbody.querySelectorAll(".verify-prices-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = decodeURIComponent(btn.dataset.id);
      const s = sims.find(x => x.id === id);
      const resultEl = tbody.querySelector(`.verify-result[data-id="${btn.dataset.id}"]`);
      if (!s || !resultEl) return;
      btn.disabled = true;
      resultEl.innerHTML = "<em>Checking Skinport/Buff163/CSFloat&hellip;</em>";
      try {
        const res = await fetch("/api/sim-population/verify-prices", {
          method: "POST",
          body: JSON.stringify({
            id: s.id, son: s.son,
            primary_collection: s.primary_collection, from_rarity: s.from_rarity, to_rarity: s.to_rarity,
            stattrak: s.stattrak, expected_roi_pct: s.expected_roi_pct, input_mix: s.input_mix,
            output_distribution: s.output_distribution, currency: 1,
          }),
        });
        const data = await res.json();
        state.simVerifyResults[id] = data.ok ? { rows: data.rows } : { error: data.error };
        resultEl.innerHTML = data.ok ? renderVerifyRows(data.rows) : `<em>Verification failed: ${data.error}</em>`;
        // The backend persisted verified_roi_pct back onto this entry's checkpoint record --
        // refresh the table so the new "Verified ROI %"/"Last verified" columns show it
        // without waiting for the next 3s poll.
        if (data.ok && data.verified_roi_pct != null) loadBestSimulations();
      } catch (e) {
        state.simVerifyResults[id] = { error: String(e) };
        resultEl.innerHTML = `<em>Verification failed: ${e}</em>`;
      } finally {
        btn.disabled = false;
      }
    });
  });
}

// Once a composition has been through Stage-2 verification (live Skinport/Buff163/CSFloat
// cross-check, see verify_queue.py), the verified number is the more trustworthy one -- shown
// here as the PRIMARY ROI value instead of leaving the headline column stuck on the original
// (possibly bugged/inflated) simulated estimate forever. The predicted number is kept alongside,
// small, for transparency about what changed. Before verification, this is identical to the old
// plain expected_roi_pct display. Shared by renderBestSimulations and renderOpportunityArchive
// so both tables update the same way.
function _roiCell(s) {
  const verified = s.verified_roi_pct;
  const predicted = s.expected_roi_pct;
  const primary = verified != null ? verified : predicted;
  const cls = primary >= 0 ? "profit-positive" : "profit-negative";
  const overrideTag = s.repriced_by_override
    ? ` <span class="tag-drought" title="Recomputed with your manual price override(s): ${s.repriced_by_override.join(", ")}">repriced</span>`
    : "";
  const predictedNote = verified != null
    ? ` <span class="muted-inline" title="Original simulated estimate before Skinport/Buff163/CSFloat verification">(predicted ${fmtMoney(predicted)}%)</span>`
    : "";
  return `<td class="num ${cls}">${fmtMoney(primary)}%${overrideTag}${predictedNote}</td>`;
}

function renderBestSimulations(simsAll) {
  const sims = _applyOpportunityFilters(simsAll);
  const tbody = document.querySelector("#best-simulations-table tbody");
  const countEl = document.getElementById("best-sim-count");
  if (countEl) countEl.textContent = simsAll.length ? `(${sims.length} of ${simsAll.length})` : "";
  const agentNames = (state.constants && state.constants.agent_names) || {};
  tbody.innerHTML = sims.map(s => {
    const expanded = state.expandedSimulations.has(s.id);
    const agentName = agentNames[s.son] || `Son ${s.son}`;
    const verifiedCell = s.verified_roi_pct != null
      ? `<span class="${s.verified_roi_pct >= 0 ? "profit-positive" : "profit-negative"}">${fmtMoney(s.verified_roi_pct)}%</span>`
      : `<span class="muted-inline">not verified</span>`;
    return `
    <tr class="sim-row" data-id="${encodeURIComponent(s.id)}">
      <td>${expanded ? "▾" : "▸"} ${agentName}</td>
      <td>${s.primary_collection}</td>
      <td>${s.from_rarity}</td>
      <td>${s.to_rarity}</td>
      <td>${s.stattrak ? "Yes" : "No"}</td>
      <td class="num">${fmtMoney(s.cost)}</td>
      ${_roiCell(s)}
      <td class="num">${verifiedCell}</td>
      <td class="num">${s.composite_score != null ? s.composite_score.toFixed(1) : "-"}</td>
      <td>${_riskBadge(s.confidence_score)}</td>
      <td>${_deadSkinRiskBadge(s)}</td>
      <td>${s.cost_basis_source}</td>
      <td>${s.verified_at ? fmtTime(s.verified_at) : "-"}</td>
    </tr>
    <tr class="drop-row" style="${expanded ? "" : "display:none;"}">
      <td colspan="13">${expanded ? renderSimulationDetail(s) : ""}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll(".sim-row").forEach(tr => {
    tr.addEventListener("click", () => {
      const id = decodeURIComponent(tr.dataset.id);
      if (state.expandedSimulations.has(id)) state.expandedSimulations.delete(id);
      else state.expandedSimulations.add(id);
      renderBestSimulations(simsAll);
    });
  });
  wireCopyButtons(tbody);
  wireVerifyButtons(tbody, sims);
  wireBlacklistButtons(tbody, sims);
}

function wireBlacklistButtons(tbody, sims) {
  tbody.querySelectorAll(".blacklist-btn").forEach(btn => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();  // don't also toggle the row's expand/collapse
      const id = decodeURIComponent(btn.dataset.id);
      const s = sims.find(x => x.id === id);
      if (!s) return;
      btn.disabled = true;
      try {
        const res = await fetch("/api/sim-population/blacklist", {
          method: "POST",
          body: JSON.stringify({
            primary_collection: s.primary_collection, from_rarity: s.from_rarity, to_rarity: s.to_rarity,
            stattrak: s.stattrak, expected_roi_pct: s.expected_roi_pct, input_mix: s.input_mix,
          }),
        });
        const data = await res.json();
        if (!data.ok) { btn.disabled = false; return; }
        // Refresh the opportunity table (this row disappears), the blacklist list, and the
        // archive browser if it's open -- is_blacklisted() is checked live at list time in both
        // sim_loop.list_best_simulations and list_all_opportunities, so a freshly-blacklisted
        // signature disappears from both the instant either is re-fetched.
        loadBestSimulations();
        loadBlacklist();
        if (state.archiveLoaded) loadOpportunityArchive(state.archive.page);
      } catch (e) {
        btn.disabled = false;
      }
    });
  });
}

async function loadBlacklist() {
  const res = await fetch("/api/sim-population/blacklist");
  state.lastBlacklist = await res.json();
  renderBlacklist(state.lastBlacklist);
}

function renderBlacklist(entries) {
  const countEl = document.getElementById("blacklist-count");
  if (countEl) countEl.textContent = entries.length ? `(${entries.length})` : "";
  const tbody = document.querySelector("#blacklist-table tbody");
  if (!tbody) return;
  const agentNames = (state.constants && state.constants.agent_names) || {};
  tbody.innerHTML = entries.map(e => `
    <tr>
      <td>${e.added_at ? fmtTime(e.added_at) : "-"}</td>
      <td>${e.primary_collection || "-"}</td>
      <td>${e.from_rarity || "-"}</td>
      <td>${e.to_rarity || "-"}</td>
      <td>${e.stattrak ? "Yes" : "No"}</td>
      <td class="num">${e.n_inputs != null ? e.n_inputs : "-"}</td>
      <td class="num ${(e.expected_roi_pct || 0) >= 0 ? "profit-positive" : "profit-negative"}">${e.expected_roi_pct != null ? fmtMoney(e.expected_roi_pct) + "%" : "-"}</td>
      <td><button class="secondary blacklist-remove-btn" data-sig="${e.signature}">Restore</button></td>
    </tr>`).join("");
  tbody.querySelectorAll(".blacklist-remove-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await fetch("/api/sim-population/blacklist/remove", {
          method: "POST", body: JSON.stringify({ signature: btn.dataset.sig }),
        });
        loadBlacklist();
        loadBestSimulations();  // a restored trade-up can show up again
        if (state.archiveLoaded) loadOpportunityArchive(state.archive.page);
      } catch (e) { btn.disabled = false; }
    });
  });
}

// ---- Browse-all-opportunities archive (paginated, on-demand) --------------------------------
// Backs sim_loop.list_all_opportunities() -- EVERY unique trade-up composition the population
// has ever simulated, not just the top-50-per-agent curated list the table above shows. Loaded
// only when the panel is opened/paged/searched, never on the 3s poll (see state.archiveLoaded),
// so an archive that's grown to thousands of rows over weeks of runtime never slows down the
// dashboard's normal refresh.
async function loadOpportunityArchive(page) {
  const params = new URLSearchParams();
  const collection = document.getElementById("archive-collection").value.trim();
  const minRoi = document.getElementById("archive-min-roi").value;
  const agent = document.getElementById("archive-agent-filter").value;
  const pageSize = document.getElementById("archive-page-size").value;
  if (collection) params.set("collection", collection);
  if (minRoi !== "") params.set("min_roi", minRoi);
  if (agent !== "") params.set("son", agent);
  params.set("page", String(page));
  params.set("page_size", pageSize);
  const res = await fetch(`/api/sim-population/opportunities/archive?${params.toString()}`);
  state.archive = await res.json();
  renderOpportunityArchive(state.archive);
}

function renderOpportunityArchive(data) {
  const tbody = document.querySelector("#archive-table tbody");
  if (!tbody) return;
  const sims = data.items || [];
  const agentNames = (state.constants && state.constants.agent_names) || {};
  const countEl = document.getElementById("archive-count");
  if (countEl) countEl.textContent = data.total ? `(${data.total} unique)` : "";
  tbody.innerHTML = sims.map(s => {
    const expanded = state.expandedArchive.has(s.id);
    const agentName = agentNames[s.son] || `Son ${s.son}`;
    const verifiedCell = s.verified_roi_pct != null
      ? `<span class="${s.verified_roi_pct >= 0 ? "profit-positive" : "profit-negative"}">${fmtMoney(s.verified_roi_pct)}%</span>`
      : `<span class="muted-inline">not verified</span>`;
    return `
    <tr class="sim-row" data-id="${encodeURIComponent(s.id)}">
      <td>${expanded ? "▾" : "▸"} ${agentName}</td>
      <td>${s.primary_collection}</td>
      <td>${s.from_rarity}</td>
      <td>${s.to_rarity}</td>
      <td>${s.stattrak ? "Yes" : "No"}</td>
      <td class="num">${fmtMoney(s.cost)}</td>
      ${_roiCell(s)}
      <td class="num">${verifiedCell}</td>
      <td class="num">${s.composite_score != null ? s.composite_score.toFixed(1) : "-"}</td>
      <td>${_riskBadge(s.confidence_score)}</td>
      <td>${_deadSkinRiskBadge(s)}</td>
      <td>${s.cost_basis_source}</td>
      <td>${s.verified_at ? fmtTime(s.verified_at) : "-"}</td>
    </tr>
    <tr class="drop-row" style="${expanded ? "" : "display:none;"}">
      <td colspan="13">${expanded ? renderSimulationDetail(s) : ""}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll(".sim-row").forEach(tr => {
    tr.addEventListener("click", () => {
      const id = decodeURIComponent(tr.dataset.id);
      if (state.expandedArchive.has(id)) state.expandedArchive.delete(id);
      else state.expandedArchive.add(id);
      renderOpportunityArchive(data);
    });
  });
  wireCopyButtons(tbody);
  wireVerifyButtons(tbody, sims);
  wireBlacklistButtons(tbody, sims);

  const statusEl = document.getElementById("archive-page-status");
  if (statusEl) statusEl.textContent = `Page ${data.page || 1} of ${data.n_pages || 1} (${data.total || 0} total)`;
  const prevBtn = document.getElementById("archive-prev-btn");
  const nextBtn = document.getElementById("archive-next-btn");
  if (prevBtn) prevBtn.disabled = (data.page || 1) <= 1;
  if (nextBtn) nextBtn.disabled = (data.page || 1) >= (data.n_pages || 1);
}

document.getElementById("invalid-skins-clear-btn").addEventListener("click", clearInvalidSkins);
document.getElementById("mispriced-skins-clear-btn").addEventListener("click", clearMispricedSkins);
document.getElementById("wear-anomalies-clear-btn").addEventListener("click", clearWearAnomalies);

// "Verify top 50" (Results page): force-queue the leaderboard's top rows into the Stage-2
// verification queue -- drained one at a time by the same consumer as the automatic queue, so
// this schedules work rather than blocking; progress shows in the queue-depth counter and the
// verified-ROI columns fill in as each row completes.
document.getElementById("verify-top-btn").addEventListener("click", async () => {
  const btn = document.getElementById("verify-top-btn");
  const statusEl = document.getElementById("verify-top-status");
  btn.disabled = true;
  statusEl.textContent = "queueing…";
  try {
    const res = await fetch("/api/sim-population/verify-top", {
      method: "POST", body: JSON.stringify({ n: 50 }),
    });
    const data = await res.json();
    statusEl.textContent = data.ok
      ? (data.queued
          ? `${data.queued} queued -- verifying one at a time (watch the Stage-2 queue panel on the ML Lab page); mispriced skins land in Market intelligence and get penalized in future training`
          : "nothing new to queue -- leaderboard empty or all rows already queued")
      : `failed: ${data.error}`;
  } catch (e) {
    statusEl.textContent = `failed: ${e}`;
  } finally {
    btn.disabled = false;
  }
});
document.getElementById("collapse-lifetime-roi-btn").addEventListener("click", () => {
  const wrap = document.getElementById("lifetime-roi-wrap");
  const aboutToShow = wrap && wrap.style.display === "none";
  if (aboutToShow && !state.lifetimeRoiLoaded) {
    state.lifetimeRoiLoaded = true;
    loadLifetimeRoiHistory();
  }
});
document.getElementById("collapse-archive-btn").addEventListener("click", () => {
  // Predicts the post-toggle state from the CURRENT (pre-toggle) display instead of reading it
  // after the fact -- DOM listeners on the same element fire in attachment order, and this
  // listener is attached earlier in the script than the generic .collapse-btn wiring further
  // down, so relying on "already toggled" would be backwards. Lazy-loads page 1 only the first
  // time the panel is actually opened, so an archive nobody ever looks at costs nothing.
  const wrap = document.getElementById("archive-wrap");
  const aboutToShow = wrap && wrap.style.display === "none";
  if (aboutToShow && !state.archiveLoaded) {
    state.archiveLoaded = true;
    loadOpportunityArchive(1);
  }
});
document.getElementById("archive-search-btn").addEventListener("click", (e) => {
  e.preventDefault();
  state.archiveLoaded = true;
  loadOpportunityArchive(1);
});
document.getElementById("archive-prev-btn").addEventListener("click", () => {
  if ((state.archive.page || 1) > 1) loadOpportunityArchive(state.archive.page - 1);
});
document.getElementById("archive-next-btn").addEventListener("click", () => {
  if ((state.archive.page || 1) < (state.archive.n_pages || 1)) loadOpportunityArchive(state.archive.page + 1);
});
document.getElementById("export-archive-xlsx-btn").addEventListener("click", () => {
  window.location.href = "/api/export/xlsx";
});

async function loadOutcomes() {
  const res = await fetch("/api/outcomes");
  renderOutcomes(await res.json());
}

function renderOutcomes(outcomes) {
  document.getElementById("outcomes-count").textContent = `(${outcomes.length})`;
  const sorted = [...outcomes].sort((a, b) => b.logged_at - a.logged_at);
  const tbody = document.querySelector("#outcomes-table tbody");
  tbody.innerHTML = sorted.map(o => `
    <tr>
      <td>${fmtTime(o.logged_at)}</td>
      <td>${o.collection}</td>
      <td>${o.from_rarity}</td>
      <td>${o.to_rarity}</td>
      <td>${o.status}</td>
      <td class="num">${fmtMoney(o.cost_paid)}</td>
      <td class="num ${o.profit >= 0 ? "profit-positive" : (o.profit < 0 ? "profit-negative" : "")}">${o.profit != null ? fmtMoney(o.profit) : "-"}</td>
      <td class="num">${o.roi_pct != null ? fmtPct(o.roi_pct) : "-"}</td>
      <td>${o.win === true ? "Win" : (o.win === false ? "Loss" : "-")}</td>
    </tr>
  `).join("");
  renderCumulativeProfitChart(outcomes);
  renderCalibrationChart(outcomes);
}

// Predicted (x) vs actual (y) ROI% for every sold outcome that had an ML prediction attached
// at logging time -- the honest "is this model any good" visual, lives in the Methodology
// section right after the feature-importance chart.
function renderCalibrationChart(outcomes) {
  const wrap = document.getElementById("calibration-wrap");
  const empty = document.getElementById("calibration-empty");
  if (!wrap || typeof Chart === "undefined") return;
  const points = outcomes.filter(o => o.status === "sold" && o.ml_predicted_roi_pct != null && o.roi_pct != null);
  if (!points.length) {
    wrap.style.display = "none";
    empty.style.display = "block";
    return;
  }
  wrap.style.display = "block";
  empty.style.display = "none";

  const scatterData = points.map(o => ({ x: o.ml_predicted_roi_pct, y: o.roi_pct }));
  const allVals = points.flatMap(o => [o.ml_predicted_roi_pct, o.roi_pct]);
  const lo = Math.min(...allVals), hi = Math.max(...allVals);
  const pad = Math.max((hi - lo) * 0.1, 1);
  const lineData = [{ x: lo - pad, y: lo - pad }, { x: hi + pad, y: hi + pad }];

  const ctx = document.getElementById("calibration-chart");
  if (state.charts.calibration) {
    state.charts.calibration.data.datasets[0].data = scatterData;
    state.charts.calibration.data.datasets[1].data = lineData;
    state.charts.calibration.update();
    return;
  }
  state.charts.calibration = new Chart(ctx, {
    type: "scatter",
    data: {
      datasets: [
        { label: "Predicted vs. actual", data: scatterData, backgroundColor: "#8ea1ff", pointRadius: 5 },
        { label: "Perfect calibration (y = x)", data: lineData, type: "line", borderColor: "#4caf50",
          borderDash: [5, 5], pointRadius: 0, fill: false, borderWidth: 1.5 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: CHART_TEXT_COLOR } } },
      scales: {
        x: { title: { display: true, text: "ML-predicted ROI %", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR } },
        y: { title: { display: true, text: "Actual ROI %", color: CHART_AXIS_COLOR }, ticks: { color: CHART_AXIS_COLOR } },
      },
    },
  });
}

// Running total of real (logged, sold) profit over time -- the "did this actually make
// money" storytelling chart for the outcomes you've logged so far.
function renderCumulativeProfitChart(outcomes) {
  const wrap = document.getElementById("cumulative-profit-wrap");
  if (!wrap || typeof Chart === "undefined") return;
  const sold = outcomes.filter(o => o.status === "sold" && o.profit != null).sort((a, b) => a.logged_at - b.logged_at);
  if (!sold.length) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "block";
  let running = 0;
  const data = sold.map(o => { running += o.profit; return running; });
  const labels = sold.map((_, i) => `#${i + 1}`);
  const ctx = document.getElementById("cumulative-profit-chart");
  if (state.charts.cumulativeProfit) {
    state.charts.cumulativeProfit.data.labels = labels;
    state.charts.cumulativeProfit.data.datasets[0].data = data;
    state.charts.cumulativeProfit.update();
    return;
  }
  state.charts.cumulativeProfit = new Chart(ctx, {
    type: "line",
    data: { labels, datasets: [{ label: "Cumulative profit", data, borderColor: "#4caf50",
                                  backgroundColor: "rgba(76,175,80,0.15)", fill: true, tension: 0.15 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        title: { display: true, text: "Cumulative real profit/loss over logged outcomes", color: CHART_AXIS_COLOR },
      },
      scales: { x: { ticks: { color: CHART_AXIS_COLOR } }, y: { ticks: { color: CHART_AXIS_COLOR } } },
    },
  });
}

function _parseMixLine(line) {
  const parts = line.split(":");
  if (parts.length !== 4) throw new Error(`Bad mix line (expected skin:wear:count:price): ${line}`);
  return { skin: parts[0], wear: parts[1], count: parseInt(parts[2]), unit_price_paid: parseFloat(parts[3]) };
}

async function submitOutcome(e) {
  e.preventDefault();
  const resultEl = document.getElementById("outcome-form-result");
  const submitBtn = document.querySelector("#outcome-form button[type=submit]");
  if (submitBtn.disabled) return;  // already in flight -- ignore a double-click/double-submit
  resultEl.textContent = "";
  try {
    submitBtn.disabled = true;
    const inputMixLines = document.getElementById("oc-input-mix").value.split("\n").map(l => l.trim()).filter(Boolean);
    const inputMix = inputMixLines.map(_parseMixLine);
    const fillerLine = document.getElementById("oc-filler").value.trim();
    const filler = fillerLine ? _parseMixLine(fillerLine) : null;
    const body = {
      collection: document.getElementById("oc-collection").value.trim(),
      kind: document.getElementById("oc-kind").value,
      stattrak: document.getElementById("oc-stattrak").checked,
      from_rarity: document.getElementById("oc-from-rarity").value,
      to_rarity: document.getElementById("oc-to-rarity").value,
      currency: parseInt(document.getElementById("oc-currency").value) || 1,
      input_mix: inputMix,
      filler_collection: document.getElementById("oc-filler-collection").value.trim() || null,
      filler_input: filler,
      output_skin: document.getElementById("oc-output-skin").value.trim(),
      // Sent as a float string (e.g. "0.18"), rounded to 2 decimals -- the server resolves it
      // to a wear-bracket name via outcomes.resolve_wear(), same as each input_mix line's wear.
      output_wear: Number(document.getElementById("oc-output-wear").value).toFixed(2),
      status: document.getElementById("oc-status").value,
      sale_price: document.getElementById("oc-sale-price").value ? parseFloat(document.getElementById("oc-sale-price").value) : null,
      predicted_roi_pct: document.getElementById("oc-predicted-roi").value ? parseFloat(document.getElementById("oc-predicted-roi").value) : null,
      ml_predicted_roi_pct: document.getElementById("oc-ml-predicted-roi").value ? parseFloat(document.getElementById("oc-ml-predicted-roi").value) : null,
      notes: document.getElementById("oc-notes").value.trim(),
    };
    const res = await fetch("/api/log-outcome", { method: "POST", body: JSON.stringify(body) });
    const data = await res.json();
    if (!data.ok) {
      resultEl.textContent = `Error: ${data.error}`;
      resultEl.className = "profit-negative";
      return;
    }
    const o = data.outcome;
    resultEl.textContent = o.status === "sold"
      ? `Logged -- cost ${fmtMoney(o.cost_paid)}, profit ${o.profit >= 0 ? "+" : ""}${fmtMoney(o.profit)}, ROI ${fmtPct(o.roi_pct)} (${o.win ? "WIN" : "LOSS"})`
      : `Logged -- cost ${fmtMoney(o.cost_paid)}, status ${o.status} (no sale price yet)`;
    resultEl.className = o.win === false ? "profit-negative" : "profit-positive";
    document.getElementById("outcome-form").reset();
    await loadOutcomes();
  } catch (err) {
    resultEl.textContent = `Error: ${err.message}`;
    resultEl.className = "profit-negative";
  } finally {
    submitBtn.disabled = false;
  }
}

async function loadNetworkStats() {
  const res = await fetch("/api/network-stats");
  renderNetworkStats(await res.json());
}

async function loadThirdpartyStats() {
  const res = await fetch("/api/thirdparty-stats");
  renderThirdpartyStats(await res.json());
}

// ---- "Log this" -- pre-fills the outcome form from a scan result -------------------------
// Deliberately does NOT submit anything or train the model on its own -- it only saves you
// retyping what the scan already found. The ML model still only ever learns from what you
// actually confirm happened (real prices paid/received) after you fill in the rest and submit.
function parseMarketHashName(fullName) {
  let name = fullName;
  const stattrak = name.startsWith("StatTrak™ ");
  if (stattrak) name = name.slice("StatTrak™ ".length);
  const m = name.match(/^(.*) \(([^)]+)\)$/);
  if (m) return { skin: m[1], wear: m[2], stattrak };
  return { skin: name, wear: "", stattrak };
}

function prefillOutcomeForm(r, kind) {
  const groupName = kind === "case" ? r.case : r.collection;
  document.getElementById("oc-collection").value = groupName || "";
  document.getElementById("oc-kind").value = kind;
  document.getElementById("oc-stattrak").checked = !!r.stattrak;
  document.getElementById("oc-from-rarity").value = kind === "case" ? "Covert" : r.from_rarity;
  document.getElementById("oc-to-rarity").value = kind === "case" ? "Extraordinary" : r.to_rarity;
  if (state.config && state.config.currency) {
    document.getElementById("oc-currency").value = state.config.currency;
  }

  let mixLines, predictedRoi;
  if (r.float_strategies && r.float_strategies.length) {
    // Prefer the recommended wear-mix (the actual play the scan suggests), not just the
    // plain cheapest-single-wear default.
    const best = r.float_strategies[0];
    const parsed = parseMarketHashName(r.cheapest_input);
    mixLines = best.input_mix.map(m => `${parsed.skin}:${m.wear}:${m.count}:${m.unit_price}`);
    predictedRoi = best.roi_pct;
  } else {
    const parsed = parseMarketHashName(r.cheapest_input);
    mixLines = [`${parsed.skin}:${parsed.wear || "Field-Tested"}:10:${(r.cost / 10).toFixed(4)}`];
    predictedRoi = r.roi_pct;
  }
  document.getElementById("oc-input-mix").value = mixLines.join("\n");
  document.getElementById("oc-filler").value = "";
  document.getElementById("oc-filler-collection").value = "";
  document.getElementById("oc-output-skin").value = "";
  document.getElementById("oc-output-wear").value = "";  // the scan doesn't know the real output
  // float you'll actually get -- always left blank for you to fill in after crafting
  document.getElementById("oc-sale-price").value = "";
  document.getElementById("oc-status").value = "sold";
  document.getElementById("oc-predicted-roi").value = predictedRoi != null ? predictedRoi.toFixed(2) : "";
  // Only carried through when the scan itself had an ML prediction for this exact result
  // (--use-ml on, model trained) -- this is what powers the predicted-vs-actual calibration
  // chart once you confirm a real sale price for it.
  document.getElementById("oc-ml-predicted-roi").value = r.ml_predicted_roi_pct != null ? r.ml_predicted_roi_pct : "";
  document.getElementById("oc-notes").value =
    `Pre-filled from scan: ${groupName || ""}${r.from_rarity ? " " + r.from_rarity : ""}${r.to_rarity ? " -> " + r.to_rarity : ""}`.trim();

  document.getElementById("learning-section").scrollIntoView({ behavior: "smooth" });
  document.getElementById("oc-output-skin").focus();
}

function wireLogOutcomeButtons(container, kind) {
  container.querySelectorAll(".log-outcome-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      prefillOutcomeForm(JSON.parse(decodeURIComponent(btn.dataset.row)), kind);
    });
  });
}

// Distinguishes "genuinely no listings found" from "the source is unreachable/blocked" --
// those look identical from a single price lookup (both are just `None`), so this reads the
// diagnostic counters src/thirdparty_prices.py tracks on every fetch attempt instead.
function renderThirdpartyStats(stats) {
  const el = document.getElementById("thirdparty-headline");
  const sp = stats.skinport;
  const bf = stats.buff163 || {};
  const cf = stats.csfloat;
  if (!sp.last_attempt_at && !bf.last_attempt_at && !cf.last_attempt_at) {
    el.textContent = "3rd-party price checks: not used yet this session (check \"Verify 3rd-party prices\" and scan to test)";
    el.className = "health-headline health-idle";
    return;
  }
  const parts = [];
  if (sp.last_attempt_at) {
    if (sp.last_error) {
      parts.push(`Skinport: ${sp.last_error}${sp.consecutive_failures > 1 ? ` (${sp.consecutive_failures} attempts in a row)` : ""}`);
    } else {
      parts.push(`Skinport: OK (last success ${fmtTime(sp.last_success_at)})`);
    }
  }
  if (bf.last_attempt_at) {
    parts.push(bf.last_error
      ? `Buff163: ${bf.last_error}${bf.consecutive_failures > 1 ? ` (${bf.consecutive_failures} attempts in a row)` : ""}`
      : `Buff163: OK (${bf.n_items ? bf.n_items.toLocaleString() + " items, " : ""}last success ${fmtTime(bf.last_success_at)})`);
  }
  if (cf.configured) {
    parts.push(cf.last_error
      ? `CSFloat: ${cf.last_error}`
      : (cf.last_attempt_at ? `CSFloat: OK (last success ${fmtTime(cf.last_success_at)})` : "CSFloat: configured, not tried yet"));
  } else {
    parts.push("CSFloat: not configured (set CSFLOAT_API_KEY to enable)");
  }
  el.textContent = `3rd-party price checks: ${parts.join(" | ")}`;
  el.className = "health-headline " + (sp.last_error || bf.last_error ? "health-warn" : "health-ok");
}

// Answers "is it doing something, or have I been blocked?" from real request outcomes,
// not just the scan's own progress counter (which can't tell the difference between
// "working through a big collection" and "stuck retrying a rate-limited request").
function renderNetworkStats(stats) {
  document.getElementById("stat-ok").textContent = stats.requests_ok;
  document.getElementById("stat-limited").textContent = stats.requests_rate_limited;
  document.getElementById("stat-error").textContent = stats.requests_error;
  document.getElementById("stat-cached").textContent = stats.cache_hits;
  document.getElementById("stat-last-success").textContent = fmtTime(stats.last_success_at);
  document.getElementById("stat-interval").textContent = (stats.current_interval ?? 1.5).toFixed(1);
  document.getElementById("stat-catalogued").textContent = stats.universe_size
    ? `${stats.cache_size ?? 0} / ${stats.universe_size}`
    : String(stats.cache_size ?? 0);

  const headline = document.getElementById("health-headline");
  const now = Date.now() / 1000;
  if (stats.backoff_until && stats.backoff_until > now) {
    const remaining = Math.ceil(stats.backoff_until - now);
    headline.textContent = `⏳ Steam just rate-limited a request -- waiting ${remaining}s before retrying (this is normal, not a ban)`;
    headline.className = "health-warn";
  } else if (stats.last_success_at && (now - stats.last_success_at) < 20) {
    headline.textContent = "✓ Working normally -- getting real prices back from Steam";
    headline.className = "health-ok";
  } else if (stats.requests_rate_limited > 0 &&
             (!stats.last_success_at || (stats.last_rate_limited_at || 0) > stats.last_success_at)) {
    headline.textContent = "⚠ Steam is currently throttling requests -- retrying automatically, this is temporary";
    headline.className = "health-warn";
  } else if (!stats.requests_ok && !stats.requests_rate_limited && !stats.cache_hits) {
    headline.textContent = "No requests made yet";
    headline.className = "health-idle";
  } else {
    headline.textContent = "Idle -- no request in the last few seconds";
    headline.className = "health-idle";
  }

  // If Steam has refused every request for 5+ minutes straight, a single-location IP-level
  // throttle is more likely explanation than "wait a bit longer" -- switching VPN server/location
  // usually gets a fresh IP and clears it immediately, so surface that explicitly.
  const vpnWarning = document.getElementById("vpn-warning");
  const RATE_LIMIT_WARNING_THRESHOLD_SECONDS = 5 * 60;
  if (stats.rate_limit_streak_start && (now - stats.rate_limit_streak_start) >= RATE_LIMIT_WARNING_THRESHOLD_SECONDS) {
    const minutes = Math.floor((now - stats.rate_limit_streak_start) / 60);
    vpnWarning.style.display = "block";
    document.getElementById("vpn-warning-text").textContent =
      `⚠ Steam has rate-limited every request for the last ${minutes} minute(s) straight. ` +
      `Try switching your VPN server/location (e.g. in Avast SecureLine, connect to a different city or country) ` +
      `to get a fresh IP. The scanner keeps retrying automatically every 5 minutes on its own, or click Retry now ` +
      `to try immediately after switching.`;
  } else {
    vpnWarning.style.display = "none";
  }
}

function renderStatus(s) {
  const val = document.getElementById("status-value");
  val.className = s.status;
  if (s.status === "running") {
    const p = s.progress;
    val.textContent = `running (${p.stage} ${p.current}/${p.total}${p.label ? ": " + p.label : ""})`;
  } else if (s.status === "paused") {
    const p = s.progress;
    val.textContent = `paused (${p.stage} ${p.current}/${p.total}${p.label ? ": " + p.label : ""}) -- click Continue to resume`;
  } else if (s.status === "error") {
    val.textContent = `error: ${s.error}`;
  } else {
    val.textContent = s.status;
  }
  // Mirrored into the always-visible sticky topbar (not just this Calculator-only status bar)
  // so a background scan erroring out, or just running, is never invisible from ML Lab/Results/
  // Rare Patterns -- see topbar-scan-status in index.html.
  const topbarVal = document.getElementById("topbar-scan-status-value");
  const topbarLink = document.getElementById("topbar-scan-status");
  topbarVal.className = s.status;
  topbarVal.textContent = s.status === "error" ? `error: ${s.error}` : (val.textContent || s.status);
  topbarLink.title = s.status === "error"
    ? `Scan error: ${s.error} -- click to open the Trade-Up Calculator page`
    : "Trade-Up Calculator scan status -- shown here on every page so a background scan (or an error in it) is never invisible just because you're on ML Lab/Results/Rare Patterns";
  document.getElementById("last-run").textContent = fmtTime(s.last_run);
  document.getElementById("tradeup-count").textContent = `(${s.tradeup_count})`;
  document.getElementById("anomaly-count").textContent = `(${s.anomaly_count})`;
  document.getElementById("covert-count").textContent = `(${s.covert_count})`;

  const btn = document.getElementById("scan-now-btn");
  btn.disabled = s.status === "running";
  document.getElementById("stop-btn").disabled = !(s.status === "running" || s.status === "paused");
  document.getElementById("pause-btn").disabled = s.status !== "running";
  document.getElementById("resume-btn").style.display = s.status === "paused" ? "" : "none";

  const progressWrap = document.getElementById("progress-wrap");
  if (s.status === "running" && s.progress.total) {
    progressWrap.style.display = "block";
    const pct = (s.progress.current / s.progress.total) * 100;
    document.getElementById("progress-fill").style.width = `${pct}%`;
    document.getElementById("progress-label").textContent =
      `${s.progress.stage}: ${s.progress.current} / ${s.progress.total}`;
  } else {
    progressWrap.style.display = "none";
  }

  // the symbol must always track the server's current currency (results reflect whatever
  // currency the last scan actually ran in), not just what's picked in the form right now
  if (s.config) {
    state.currencySymbol = symbolForCurrency(s.config.currency);
  }

  // kept live (not just on first load) so renderTradeups can tell whether both strategies
  // are currently enabled, to decide whether to surface a better-strategy badge on the row
  state.config = s.config;

  // reflect current server-side config into the form on first load
  if (!state.configLoaded && s.config) {
    document.getElementById("cfg-collection").value = s.config.collection || "";
    document.getElementById("cfg-currency").value = s.config.currency;
    document.getElementById("cfg-min-roi").value = s.config.min_roi;
    document.getElementById("cfg-stattrak").checked = !!s.config.stattrak;
    document.getElementById("cfg-fast-mode").checked = !!s.config.fast_mode;
    document.getElementById("cfg-skip-anomalies").checked = !!s.config.skip_anomalies;
    document.getElementById("cfg-float-strategy").checked = !!s.config.float_strategy;
    document.getElementById("cfg-mixed-strategy").checked = !!s.config.mixed_strategy;
    document.getElementById("cfg-strategy-any-roi").checked = !!s.config.strategy_any_roi;
    document.getElementById("cfg-gap").value = s.config.anomaly_min_gap_pct;
    document.getElementById("cfg-volume").value = s.config.anomaly_min_volume;
    document.getElementById("cfg-scan-covert").checked = !!s.config.scan_covert;
    document.getElementById("cfg-case").value = s.config.case || "";
    document.getElementById("cfg-check-thirdparty").checked = !!s.config.check_thirdparty;
    document.getElementById("cfg-use-ml").checked = !!s.config.use_ml;
    state.configLoaded = true;
  }
}

async function loadTradeups() {
  const res = await fetch("/api/tradeups");
  state.tradeups = await res.json();
  renderTradeups();
}

async function loadAnomalies() {
  const res = await fetch("/api/anomalies");
  state.anomalies = await res.json();
  renderAnomalies();
}

async function loadCovert() {
  const res = await fetch("/api/covert");
  state.covert = await res.json();
  renderCovert();
}

function sortRows(rows, sortSpec) {
  const { key, dir } = sortSpec;
  return [...rows].sort((a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
    return String(av).localeCompare(String(bv)) * dir;
  });
}

// A low Steam sale-volume count means the price is resting on very few real trades -- possibly
// just one seller's ask nobody's actually buying, not a price you could reliably sell at. Same
// threshold/convention as anomalies.py's own --min-volume filter (see /api/constants).
function lowVolumeBadge(volume) {
  const threshold = (state.constants && state.constants.low_volume_threshold) || 5;
  if (volume == null || volume >= threshold) return "";
  return `<span class="tag-lowvol" title="Only ${volume} recent Steam sale(s) backing this price -- may not reflect what you could actually sell at. Use the 'Price report (CSV)' button to cross-check against Skinport/CSFloat.">⚠ low volume (${volume})</span>`;
}

// Short wear labels for the wear-landing-odds column (FN 72% / MW 28%).
const WEAR_ABBREV = { "Factory New": "FN", "Minimal Wear": "MW", "Field-Tested": "FT",
                      "Well-Worn": "WW", "Battle-Scarred": "BS" };

function wearProbsBadge(wearProbs) {
  const entries = Object.entries(wearProbs || {});
  if (!entries.length) return "-";
  return entries.map(([wear, pct]) =>
    `<span class="wear-prob" title="${wear}: ${pct}% chance the output float lands in this bracket, given your input wear mix (sampled across each input's full wear range, not just its midpoint)">${WEAR_ABBREV[wear] || wear} ${pct.toFixed(0)}%</span>`
  ).join(" ");
}

function renderDrops(drops) {
  if (!drops || !drops.length) return "<em>No drop data available.</em>";
  const sorted = [...drops].sort((a, b) => (b.price ?? -1) - (a.price ?? -1));
  const hasWearProbs = sorted.some(d => d.wear_probs && Object.keys(d.wear_probs).length);
  return `
    <table class="drops-table">
      <thead><tr><th>Possible drop</th><th>Probability</th>${hasWearProbs ? `<th title="Where the output float could actually land, given the inputs' wear-bracket ranges">Wear odds</th>` : ""}<th>Price</th><th>Volume</th><th>Profit after fee</th><th>Max buy price</th></tr></thead>
      <tbody>
        ${sorted.map(d => {
          const hasProfit = d.profit_after_fee != null;
          const rowClass = hasProfit ? (d.profit_after_fee >= 0 ? "drop-win" : "drop-loss") : "";
          const profitClass = hasProfit ? (d.profit_after_fee >= 0 ? "profit-positive" : "profit-negative") : "";
          return `
          <tr class="${rowClass}">
            <td>${copyableName(d.name, d.image)}</td>
            <td class="num">${d.probability_pct.toFixed(2)}%</td>
            ${hasWearProbs ? `<td class="num">${wearProbsBadge(d.wear_probs)}</td>` : ""}
            <td class="num">${d.price != null ? fmtCurrency(d.price) : "no price data"} ${lowVolumeBadge(d.volume)}</td>
            <td class="num">${d.volume != null ? d.volume : "-"}</td>
            <td class="num ${profitClass}">${hasProfit ? fmtCurrency(d.profit_after_fee) : "-"}</td>
            <td class="num">${d.max_input_price != null ? fmtCurrency(d.max_input_price) : "-"}</td>
          </tr>
        `;
        }).join("")}
      </tbody>
    </table>`;
}

function renderFloatStrategy(r) {
  if (!r.float_strategies || !r.float_strategies.length) return "";
  const best = r.float_strategies[0];
  const isBetter = best.roi_pct > r.roi_pct;
  const thumb = r.cheapest_input_image
    ? `<img class="skin-thumb" src="${r.cheapest_input_image}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : "";
  const mixDesc = best.input_mix.map(m => `${m.count}x ${m.wear} (${fmtCurrency(m.unit_price)} ea)`).join(" + ");
  return `
    <div class="float-strategy ${isBetter ? "float-better" : ""}">
      <strong>Float-mix strategy</strong> -- target landing output in <em>${best.target_wear}</em>
      ${isBetter ? "<span class=\"tag-better\">beats default</span>" : "<span class=\"tag-same\">no better than default</span>"}
      <div class="float-strategy-row">
        <span>Buy: ${thumb}${mixDesc}</span>
        <span class="num">Cost ${fmtCurrency(best.input_cost)}</span>
        <span class="num">Revenue ${fmtCurrency(best.expected_revenue)}</span>
        <span class="num ${best.profit >= 0 ? "profit-positive" : "profit-negative"}">Profit ${fmtCurrency(best.profit)}</span>
        <span class="num ${best.roi_pct >= 0 ? "profit-positive" : "profit-negative"}">ROI ${best.roi_pct.toFixed(1)}%</span>
      </div>
    </div>`;
}

// mixed_strategy/mixed_float_strategy come from either the collection-based modules
// (mixed_tradeup.py/mixed_float.py, fields named *_collection) or the case-based ones for
// Covert -> knife/glove (mixed_covert.py/mixed_float_covert.py, fields named *_case) -- these
// two small helpers read whichever is present so one render function serves both tables.
function groupLabel(m) {
  return m.primary_collection ?? m.primary_case;
}
function fillerGroupLabel(m) {
  return m.filler_collection ?? m.filler_case;
}

function renderMixedStrategy(r) {
  const m = r.mixed_strategy;
  if (!m) return "";
  const groupWord = m.primary_collection !== undefined ? "collection" : "case";
  return `
    <div class="float-strategy float-better">
      <strong>Mixed-${groupWord} filler strategy</strong> -- diluting with ${fillerGroupLabel(m)}
      <span class="tag-better">beats single-${groupWord}</span>
      <div class="float-strategy-row">
        <span>Buy: ${m.primary_count}x ${copyableName(m.primary_input, m.primary_input_image)} + ${m.filler_count}x ${copyableName(m.filler_input, m.filler_input_image)} (filler)</span>
        <span class="num">Cost ${fmtCurrency(m.cost)}</span>
        <span class="num">Revenue ${fmtCurrency(m.expected_revenue)}</span>
        <span class="num ${m.profit >= 0 ? "profit-positive" : "profit-negative"}">Profit ${fmtCurrency(m.profit)}</span>
        <span class="num profit-positive">ROI ${m.roi_pct.toFixed(1)}% (+${m.roi_improvement_pct.toFixed(1)}pp)</span>
      </div>
      ${Object.keys(m.buy_links || {}).length ? `<div class="float-strategy-row">${renderBuyLinks(m.buy_links.primary)} ${renderBuyLinks(m.buy_links.filler)}</div>` : ""}
    </div>`;
}

function renderMixedFloatStrategy(r) {
  const m = r.mixed_float_strategy;
  if (!m) return "";
  const groupWord = m.primary_collection !== undefined ? "collection" : "case";
  const hasFiller = m.filler_count > 0;
  const primaryThumb = m.primary_skin_image
    ? `<img class="skin-thumb" src="${m.primary_skin_image}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : "";
  const mixDesc = m.primary_mix.map(p => `${p.count}x ${p.wear} (${fmtCurrency(p.unit_price)} ea)`).join(" + ");
  const primaryLinks = m.buy_links && m.buy_links.primary
    ? Object.entries(m.buy_links.primary).map(([wear, links]) =>
        `<span title="${wear}">${renderBuyLinks(links)}</span>`).join(" ")
    : "";
  const fillerLinks = hasFiller && m.buy_links ? renderBuyLinks(m.buy_links.filler) : "";
  return `
    <div class="float-strategy float-better">
      <strong>Mixed-float strategy</strong> -- ${hasFiller
        ? `wear-mixing ${groupLabel(m)} diluted with ${fillerGroupLabel(m)}`
        : `wear-mixing ${groupLabel(m)} alone (no filler ${groupWord} beat it)`}
      <span class="tag-better">beats single-${groupWord} default</span>
      <div class="float-strategy-row">
        <span>Primary: ${primaryThumb}${mixDesc}</span>
        ${hasFiller ? `<span>Filler: ${m.filler_count}x ${copyableName(m.filler_input, m.filler_skin_image)} (${fmtCurrency(m.filler_unit_price)} ea)</span>` : ""}
      </div>
      <div class="float-strategy-row">
        <span class="num">Cost ${fmtCurrency(m.cost)}</span>
        <span class="num">Revenue ${fmtCurrency(m.expected_revenue)}</span>
        <span class="num ${m.profit >= 0 ? "profit-positive" : "profit-negative"}">Profit ${fmtCurrency(m.profit)}</span>
        <span class="num profit-positive">ROI ${m.roi_pct.toFixed(1)}% (+${m.roi_improvement_pct.toFixed(1)}pp)</span>
      </div>
      ${primaryLinks || fillerLinks ? `<div class="float-strategy-row">${primaryLinks} ${fillerLinks}</div>` : ""}
    </div>`;
}

// The best-known ROI across whatever strategy results this row has, so the row itself (not
// just its dropdown) can flag "there's a better play here" without the user having to expand
// it first to find out. Only meaningful when both strategy toggles are on, since that's the
// only time the server bothers computing (and returning) the combined mixed-float search.
function bestStrategyRoi(r) {
  if (r.mixed_float_strategy) return r.mixed_float_strategy.roi_pct;
  let best = null;
  if (r.mixed_strategy) best = r.mixed_strategy.roi_pct;
  if (r.float_strategies && r.float_strategies.length) {
    const floatRoi = r.float_strategies[0].roi_pct;
    if (best === null || floatRoi > best) best = floatRoi;
  }
  return best;
}

// ML-predicted ROI badge -- a tiebreaker-grade signal from learning.py, never a ranking
// override, so it's always shown alongside (not instead of) the formula ROI.
function mlBadge(r) {
  if (r.ml_predicted_roi_pct == null) return "";
  return `<span class="tag-ml" title="ML-predicted ROI (confidence: ${r.ml_confidence || "unknown"})">(ML) ${r.ml_predicted_roi_pct.toFixed(1)}%</span>`;
}

// "Drought" flag: Steam priced well above the cheapest verified 3rd-party price for the
// cheapest input -- only present when "Verify 3rd-party prices" was checked for the scan.
function droughtBadge(r) {
  const tp = r.thirdparty_check;
  if (!tp || !tp.is_drought) return "";
  return `<span class="tag-drought" title="Steam is ${tp.gap_pct.toFixed(0)}% above the cheapest verified price (${fmtCurrency(tp.cheapest_thirdparty_price)} on ${tp.cheapest_source}) -- may be a temporary supply drought inflating the Steam price specifically">⚠ drought +${tp.gap_pct.toFixed(0)}%</span>`;
}

function renderTradeups() {
  const rows = sortRows(state.tradeups, state.sort.tradeups);
  const tbody = document.querySelector("#tradeup-table tbody");
  const bothStrategiesOn = !!(state.config && state.config.float_strategy && state.config.mixed_strategy);
  tbody.innerHTML = rows.map(r => {
    const key = rowKey(r);
    const expanded = state.expandedTradeups.has(key);
    const bestRoi = bothStrategiesOn ? bestStrategyRoi(r) : null;
    // Surfaced outside the dropdown (right on the collapsed row) so a better play doesn't
    // require expanding every row to discover -- only shown once both strategy toggles are
    // on and the best one actually beats this row's plain ROI.
    const strategyBadge = bestRoi != null && bestRoi > r.roi_pct
      ? `<span class="tag-better" title="Best strategy ROI found for this trade-up">↑ ${bestRoi.toFixed(1)}% available</span>`
      : "";
    return `
    <tr class="tradeup-row" data-key="${encodeURIComponent(key)}">
      <td>${expanded ? "▾" : "▸"} ${r.collection}</td>
      <td>${r.from_rarity}</td>
      <td>${r.to_rarity}</td>
      <td class="num">${fmtCurrency(r.cost)}</td>
      <td>${copyableName(r.cheapest_input, r.cheapest_input_image)} ${droughtBadge(r)}</td>
      <td class="num">${fmtCurrency(r.expected_revenue)}</td>
      <td class="num">${fmtCurrency(r.expected_revenue_after_fee)}</td>
      <td class="num ${r.profit >= 0 ? "profit-positive" : "profit-negative"}">${fmtCurrency(r.profit)}</td>
      <td class="num ${r.roi_pct >= 0 ? "profit-positive" : "profit-negative"}">${fmtMoney(r.roi_pct)}% ${strategyBadge} ${mlBadge(r)}</td>
      <td class="num">${fmtCurrency(r.max_input_price)}</td>
      <td onclick="event.stopPropagation()">${renderBuyLinks(r.buy_links)}<button class="log-outcome-btn" data-row="${encodeURIComponent(JSON.stringify(r))}" title="Pre-fill the outcome-logging form below with this trade-up's details -- you still confirm the real prices/results yourself">&#128203; Log this</button></td>
    </tr>
    <tr class="drop-row" style="${expanded ? "" : "display:none;"}">
      <td colspan="11">${expanded ? (r.mixed_float_strategy ? renderMixedFloatStrategy(r) : renderMixedStrategy(r) + renderFloatStrategy(r)) + renderDrops(r.possible_drops) : ""}</td>
    </tr>
  `;
  }).join("");

  renderRoiHistogram();
  renderMlPicks();
  wireLogOutcomeButtons(tbody, "collection");

  tbody.querySelectorAll(".tradeup-row").forEach(tr => {
    tr.addEventListener("click", () => {
      const key = decodeURIComponent(tr.dataset.key);
      if (state.expandedTradeups.has(key)) state.expandedTradeups.delete(key);
      else state.expandedTradeups.add(key);
      renderTradeups();
    });
  });
  wireCopyButtons(tbody);
}

function renderAnomalies() {
  const rows = sortRows(state.anomalies, state.sort.anomalies);
  const tbody = document.querySelector("#anomaly-table tbody");
  tbody.innerHTML = rows.map(a => `
    <tr>
      <td>${copyableName(a.name, a.image)}</td>
      <td class="num">${fmtCurrency(a.lowest_price)}</td>
      <td class="num">${fmtCurrency(a.median_price)}</td>
      <td class="num">${a.volume}</td>
      <td class="num">${fmtMoney(a.gap_pct)}%</td>
      <td class="num profit-positive">${fmtCurrency(a.profit_after_fee)}</td>
      <td class="num">${fmtCurrency(a.max_buy_price)}</td>
    </tr>
  `).join("");
  wireCopyButtons(tbody);
}

function renderCovert() {
  const rows = sortRows(state.covert, state.sort.covert);
  const tbody = document.querySelector("#covert-table tbody");
  const bothStrategiesOn = !!(state.config && state.config.float_strategy && state.config.mixed_strategy);
  tbody.innerHTML = rows.map(r => {
    const key = covertRowKey(r);
    const expanded = state.expandedCovert.has(key);
    const bestRoi = bothStrategiesOn ? bestStrategyRoi(r) : null;
    const strategyBadge = bestRoi != null && bestRoi > r.roi_pct
      ? `<span class="tag-better" title="Best strategy ROI found for this trade-up">↑ ${bestRoi.toFixed(1)}% available</span>`
      : "";
    return `
    <tr class="tradeup-row covert-row" data-key="${encodeURIComponent(key)}">
      <td>${expanded ? "▾" : "▸"} ${r.case}</td>
      <td class="num">${fmtCurrency(r.cost)}</td>
      <td>${copyableName(r.cheapest_input, r.cheapest_input_image)} ${droughtBadge(r)}</td>
      <td class="num">${fmtCurrency(r.expected_revenue)}</td>
      <td class="num">${fmtCurrency(r.expected_revenue_after_fee)}</td>
      <td class="num ${r.profit >= 0 ? "profit-positive" : "profit-negative"}">${fmtCurrency(r.profit)}</td>
      <td class="num ${r.roi_pct >= 0 ? "profit-positive" : "profit-negative"}">${fmtMoney(r.roi_pct)}% ${strategyBadge} ${mlBadge(r)}</td>
      <td class="num">${fmtCurrency(r.max_input_price)}</td>
      <td onclick="event.stopPropagation()">${renderBuyLinks(r.buy_links)}<button class="log-outcome-btn" data-row="${encodeURIComponent(JSON.stringify(r))}" title="Pre-fill the outcome-logging form below with this trade-up's details -- you still confirm the real prices/results yourself">&#128203; Log this</button></td>
    </tr>
    <tr class="drop-row" style="${expanded ? "" : "display:none;"}">
      <td colspan="9">${expanded ? (r.mixed_float_strategy ? renderMixedFloatStrategy(r) : renderMixedStrategy(r) + renderFloatStrategy(r)) + renderDrops(r.possible_drops) : ""}</td>
    </tr>
  `;
  }).join("");

  wireLogOutcomeButtons(tbody, "case");

  tbody.querySelectorAll(".covert-row").forEach(tr => {
    tr.addEventListener("click", () => {
      const key = decodeURIComponent(tr.dataset.key);
      if (state.expandedCovert.has(key)) state.expandedCovert.delete(key);
      else state.expandedCovert.add(key);
      renderCovert();
    });
  });
  wireCopyButtons(tbody);
}

function wireSortableHeaders(tableId, tableKey, renderFn) {
  document.querySelectorAll(`#${tableId} th`).forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      const spec = state.sort[tableKey];
      spec.dir = spec.key === key ? -spec.dir : -1;
      spec.key = key;
      renderFn();
    });
  });
}

async function submitConfig(e) {
  e.preventDefault();
  const body = {
    collection: document.getElementById("cfg-collection").value.trim(),
    currency: parseInt(document.getElementById("cfg-currency").value) || 1,
    min_roi: parseFloat(document.getElementById("cfg-min-roi").value) || 0,
    stattrak: document.getElementById("cfg-stattrak").checked,
    fast_mode: document.getElementById("cfg-fast-mode").checked,
    skip_anomalies: document.getElementById("cfg-skip-anomalies").checked,
    float_strategy: document.getElementById("cfg-float-strategy").checked,
    mixed_strategy: document.getElementById("cfg-mixed-strategy").checked,
    strategy_any_roi: document.getElementById("cfg-strategy-any-roi").checked,
    anomaly_min_gap_pct: parseFloat(document.getElementById("cfg-gap").value) || 0,
    anomaly_min_volume: parseInt(document.getElementById("cfg-volume").value) || 0,
    scan_covert: document.getElementById("cfg-scan-covert").checked,
    case: document.getElementById("cfg-case").value.trim(),
    check_thirdparty: document.getElementById("cfg-check-thirdparty").checked,
    use_ml: document.getElementById("cfg-use-ml").checked,
  };
  await fetch("/api/config", { method: "POST", body: JSON.stringify(body) });
  await fetch("/api/scan-now", { method: "POST" });
}

document.getElementById("config-form").addEventListener("submit", submitConfig);
document.getElementById("scan-now-btn").addEventListener("click", () => fetch("/api/scan-now", { method: "POST" }));
document.getElementById("stop-btn").addEventListener("click", () => fetch("/api/stop", { method: "POST" }));
document.getElementById("pause-btn").addEventListener("click", () => fetch("/api/pause", { method: "POST" }));
document.getElementById("resume-btn").addEventListener("click", () => fetch("/api/resume", { method: "POST" }));
// Deliberately not gated by scan status like the main Scan now button: when Steam is stuck
// rate-limiting, the background scan is technically still "running" (just stuck retrying),
// so this is the one button that stays clickable to force an immediate fresh attempt.
document.getElementById("retry-now-btn").addEventListener("click", () => fetch("/api/scan-now", { method: "POST" }));

// "Show only underpriced listings" is a pure display filter -- no server round-trip needed,
// the trade-up data is already loaded, just hidden.
const viewOnlyAnomaliesCheckbox = document.getElementById("view-only-anomalies");
function applyViewFilter() {
  document.getElementById("tradeups-section").style.display = viewOnlyAnomaliesCheckbox.checked ? "none" : "";
}
viewOnlyAnomaliesCheckbox.addEventListener("change", applyViewFilter);
applyViewFilter();

// CSV export -- builds from whatever's currently loaded and sorted in the browser, since a
// spreadsheet is just a snapshot of what you're already looking at.
function downloadCSV(filename, columns, rows) {
  const escape = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [columns.map(c => escape(c.label)).join(",")];
  for (const row of rows) {
    lines.push(columns.map(c => escape(c.value(row))).join(","));
  }
  const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

document.getElementById("export-tradeups-btn").addEventListener("click", (e) => {
  e.preventDefault();
  const rows = sortRows(state.tradeups, state.sort.tradeups);
  downloadCSV("tradeups.csv", [
    { label: "collection", value: r => r.collection },
    { label: "from_rarity", value: r => r.from_rarity },
    { label: "to_rarity", value: r => r.to_rarity },
    { label: "cost", value: r => r.cost.toFixed(2) },
    { label: "cheapest_input", value: r => r.cheapest_input },
    { label: "expected_revenue", value: r => r.expected_revenue.toFixed(2) },
    { label: "expected_revenue_after_fee", value: r => r.expected_revenue_after_fee.toFixed(2) },
    { label: "profit", value: r => r.profit.toFixed(2) },
    { label: "roi_pct", value: r => r.roi_pct.toFixed(2) },
    { label: "max_input_price", value: r => r.max_input_price.toFixed(2) },
  ], rows);
});

// Server-side, unlike the plain CSV exports above -- it cross-checks Skinport/CSFloat, which
// needs a request from the server (not just what's already loaded in the browser). Reuses
// whatever the current scan already found (server-side, respecting Min ROI%) rather than
// starting a new scan, so this is fast even though the exports above are instant.
document.getElementById("price-report-btn").addEventListener("click", () => {
  const mlOnly = document.getElementById("price-report-ml-only").checked;
  window.location.href = `/api/price-report?ml_only=${mlOnly}`;
});

document.getElementById("export-anomalies-btn").addEventListener("click", (e) => {
  e.preventDefault();
  const rows = sortRows(state.anomalies, state.sort.anomalies);
  downloadCSV("underpriced_listings.csv", [
    { label: "name", value: a => a.name },
    { label: "lowest_price", value: a => a.lowest_price.toFixed(2) },
    { label: "median_price", value: a => a.median_price.toFixed(2) },
    { label: "volume", value: a => a.volume },
    { label: "gap_pct", value: a => a.gap_pct.toFixed(2) },
    { label: "profit_after_fee", value: a => a.profit_after_fee.toFixed(2) },
    { label: "max_buy_price", value: a => a.max_buy_price.toFixed(2) },
  ], rows);
});

document.getElementById("export-covert-btn").addEventListener("click", (e) => {
  e.preventDefault();
  const rows = sortRows(state.covert, state.sort.covert);
  downloadCSV("covert_tradeups.csv", [
    { label: "case", value: r => r.case },
    { label: "cost", value: r => r.cost.toFixed(2) },
    { label: "cheapest_input", value: r => r.cheapest_input },
    { label: "expected_revenue", value: r => r.expected_revenue.toFixed(2) },
    { label: "expected_revenue_after_fee", value: r => r.expected_revenue_after_fee.toFixed(2) },
    { label: "profit", value: r => r.profit.toFixed(2) },
    { label: "roi_pct", value: r => r.roi_pct.toFixed(2) },
    { label: "max_input_price", value: r => r.max_input_price.toFixed(2) },
  ], rows);
});

wireSortableHeaders("tradeup-table", "tradeups", renderTradeups);
wireSortableHeaders("anomaly-table", "anomalies", renderAnomalies);
wireSortableHeaders("covert-table", "covert", renderCovert);

// Hide/show toggles for the multi-agent simulation panel's three sub-tables -- purely a
// display collapse, nothing is unloaded, so the next 3s poll doesn't need any special handling.
document.querySelectorAll(".collapse-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const target = document.getElementById(btn.dataset.target);
    if (!target) return;
    const hidden = target.style.display === "none";
    target.style.display = hidden ? "" : "none";
    btn.textContent = hidden ? "Hide" : "Show";
    if (hidden) resizeAllCharts();  // just revealed -- any chart inside was possibly born at 0x0
  });
});

// Same 0x0-on-hidden-container problem as the tab/.collapse-btn cases above, but for native
// <details> elements (methodology's feature-importance/calibration charts, the new market-
// intelligence accordion) -- a "toggle" event fires on both open AND close, resizeAllCharts()
// is cheap/idempotent so no need to distinguish.
document.querySelectorAll("details").forEach(el => {
  el.addEventListener("toggle", () => { if (el.open) resizeAllCharts(); });
});

document.getElementById("export-profitable-collections-btn").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCSV("most_profitable_collections.csv", [
    { label: "collection", value: c => c.collection },
    { label: "count", value: c => c.count },
    { label: "avg_roi_pct", value: c => c.avg_roi_pct.toFixed(2) },
    { label: "best_roi_pct", value: c => c.best_roi_pct.toFixed(2) },
    { label: "total_expected_profit", value: c => c.total_expected_profit.toFixed(2) },
  ], state.lastSimLeaderboards.collections || []);
});

document.getElementById("export-profitable-skins-btn").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCSV("most_profitable_skins.csv", [
    { label: "skin", value: s => s.skin },
    { label: "wear", value: s => s.wear },
    { label: "appearances", value: s => s.appearances },
    { label: "weighted_expected_profit", value: s => s.weighted_profit.toFixed(2) },
  ], state.lastSimLeaderboards.skins || []);
});

document.getElementById("export-best-sim-btn").addEventListener("click", (e) => {
  e.preventDefault();
  const agentNames = (state.constants && state.constants.agent_names) || {};
  const rows = _applyOpportunityFilters(state.lastBestSimulations);
  downloadCSV("tradeup_opportunities.csv", [
    { label: "agent", value: s => agentNames[s.son] || `Son ${s.son}` },
    { label: "collection", value: s => s.primary_collection },
    { label: "from_rarity", value: s => s.from_rarity },
    { label: "to_rarity", value: s => s.to_rarity },
    { label: "stattrak", value: s => s.stattrak ? "Yes" : "No" },
    { label: "cost", value: s => s.cost.toFixed(2) },
    { label: "roi_pct", value: s => s.expected_roi_pct.toFixed(2) },
    { label: "verified_roi_pct", value: s => s.verified_roi_pct != null ? s.verified_roi_pct.toFixed(2) : "" },
    { label: "composite_score", value: s => s.composite_score != null ? s.composite_score.toFixed(1) : "" },
    { label: "confidence_score", value: s => s.confidence_score != null ? s.confidence_score.toFixed(0) : "" },
    { label: "cost_basis_source", value: s => s.cost_basis_source },
    { label: "verified_at", value: s => s.verified_at ? fmtTime(s.verified_at) : "" },
  ], rows);
});

document.getElementById("retrain-loop-start-btn").addEventListener("click", () => fetch("/api/retrain-loop/start", { method: "POST" }));
document.getElementById("retrain-loop-stop-btn").addEventListener("click", () => fetch("/api/retrain-loop/stop", { method: "POST" }));

document.getElementById("verify-queue-start-btn").addEventListener("click", () => fetch("/api/verify-queue/start", { method: "POST" }));
document.getElementById("verify-queue-stop-btn").addEventListener("click", () => fetch("/api/verify-queue/stop", { method: "POST" }));

["opp-min-roi", "opp-risk-filter", "opp-agent-filter"].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener("input", () => {
    state.opportunityFilters = {
      minRoi: document.getElementById("opp-min-roi").value === "" ? null : parseFloat(document.getElementById("opp-min-roi").value),
      risk: document.getElementById("opp-risk-filter").value,
      agent: document.getElementById("opp-agent-filter").value,
    };
    renderBestSimulations(state.lastBestSimulations);
  });
});

document.getElementById("agent-stat-select").addEventListener("change", () => {
  renderAgentStatsChart(state.lastSimPopulation || []);
});

document.getElementById("price-scan-start-btn").addEventListener("click", async () => {
  const collection = document.getElementById("price-scan-collection").value.trim();
  await fetch("/api/price-scan/config", { method: "POST", body: JSON.stringify({ collection }) });
  await fetch("/api/price-scan/start", { method: "POST" });
});
document.getElementById("price-scan-pause-btn").addEventListener("click", () => fetch("/api/price-scan/pause", { method: "POST" }));
document.getElementById("price-scan-stop-btn").addEventListener("click", () => fetch("/api/price-scan/stop", { method: "POST" }));
// Sim-population son cards (Start/Pause/Stop/Nudge/Full reset) are wired via one delegated
// listener on #sim-population-container, set up in _ensureSonCards() the first time it renders.

document.getElementById("round-robin-start-btn").addEventListener("click", async (e) => {
  e.target.disabled = true;
  await fetch("/api/sim-population/round-robin/start", { method: "POST" });
  await loadRoundRobinStatus();
  await loadSimPopulationStatus();
});
document.getElementById("round-robin-stop-btn").addEventListener("click", async (e) => {
  e.target.disabled = true;
  await fetch("/api/sim-population/round-robin/stop", { method: "POST" });
  await loadRoundRobinStatus();
  await loadSimPopulationStatus();
});
document.getElementById("reset-all-btn").addEventListener("click", async (e) => {
  if (!confirm("Restart ALL agents? This wipes every agent's logged simulations, trained models and progress and starts the whole population over from scratch -- cannot be undone.")) return;
  e.target.disabled = true;
  try {
    await fetch("/api/sim-population/reset-all", { method: "POST" });
    await loadRoundRobinStatus();
    await loadSimPopulationStatus();
  } finally {
    e.target.disabled = false;
  }
});
document.getElementById("reset-all-data-btn").addEventListener("click", async (e) => {
  const typed = prompt("This wipes EVERY regenerable cache/registry app-wide: the ML sim population (all agents' simulations, models and progress), rare-pattern scan results, the Steam price cache, and the invalid-skin/mispriced-skin/wear-anomaly/dead-skin registries.\n\nYour real logged trade history, the real ROI model, and your permanent skin blacklist/price overrides are NOT touched.\n\nCannot be undone. Type RESET to confirm:");
  if (typed !== "RESET") return;
  e.target.disabled = true;
  try {
    const res = await fetch("/api/reset-all-data", { method: "POST" });
    const data = await res.json();
    alert("Reset complete. Removed: " + Object.entries(data.removed || {})
      .map(([k, v]) => `${k}=${v}`).join(", "));
    location.reload();
  } finally {
    e.target.disabled = false;
  }
});
document.getElementById("train-now-btn").addEventListener("click", async () => {
  const btn = document.getElementById("train-now-btn");
  btn.disabled = true;
  try {
    const res = await fetch("/api/train-model", { method: "POST" });
    const result = await res.json();
    await loadModelStatus();
    await loadRetrainLoopStatus();
    if (!result.trained) alert(`Not trained: ${result.reason}`);
  } finally {
    btn.disabled = false;
  }
});
document.getElementById("reset-model-btn").addEventListener("click", async () => {
  if (!confirm("Reset the trained ML ROI model? Your logged outcomes are kept -- you can retrain from them with \"Train now\" afterward.")) return;
  const btn = document.getElementById("reset-model-btn");
  btn.disabled = true;
  try {
    await fetch("/api/reset-model", { method: "POST" });
    await loadModelStatus();
    await loadRetrainLoopStatus();
  } finally {
    btn.disabled = false;
  }
});
document.getElementById("outcome-form").addEventListener("submit", submitOutcome);

// If the user manually retypes the collection or input mix after using "Log this", the
// prefilled ml_predicted_roi_pct (and predicted_roi_pct) no longer describes what's actually
// being submitted -- clear them so a stale prediction never gets attached to a different trade.
// Only fires on real user typing (programmatic .value = ... from prefillOutcomeForm does not
// dispatch an "input" event), so a fresh "Log this" click still works normally.
["oc-collection", "oc-input-mix"].forEach(id => {
  document.getElementById(id).addEventListener("input", () => {
    document.getElementById("oc-ml-predicted-roi").value = "";
    document.getElementById("oc-predicted-roi").value = "";
  });
});

document.getElementById("export-data-btn").addEventListener("click", () => {
  window.location.href = "/api/export-data";
});

// ---- Shared /api/skins autocomplete (price history, skin blacklist, price overrides) -----
function wireSkinAutocomplete(inputId, boxId, formId) {
  let timer = null;
  document.getElementById(inputId).addEventListener("input", (e) => {
    clearTimeout(timer);
    const q = e.target.value.trim();
    const box = document.getElementById(boxId);
    if (q.length < 2) {
      box.style.display = "none";
      return;
    }
    timer = setTimeout(async () => {
      const res = await fetch(`/api/skins?q=${encodeURIComponent(q)}`);
      const skins = await res.json();
      skins.forEach(s => { state.skinImageCache[s.name] = s.image; });
      if (!skins.length) {
        box.style.display = "none";
        return;
      }
      box.innerHTML = skins.map(s => `
        <div class="suggestion-item" data-name="${encodeURIComponent(s.name)}">
          ${s.image ? `<img class="skin-thumb" src="${s.image}" alt="" loading="lazy" onerror="this.style.display='none'">` : ""}${s.name}
        </div>`).join("");
      box.style.display = "block";
      box.querySelectorAll(".suggestion-item").forEach(el => {
        el.addEventListener("click", () => {
          document.getElementById(inputId).value = decodeURIComponent(el.dataset.name);
          box.style.display = "none";
        });
      });
    }, 250);
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(`#${formId}`)) {
      const box = document.getElementById(boxId);
      if (box) box.style.display = "none";
    }
  });
}

// ---- Skin price history & prediction ---------------------------------------------------
wireSkinAutocomplete("ph-skin", "ph-suggestions", "price-history-form");

// ---- Skin blacklist + price override forms (Market intelligence) ------------------------
wireSkinAutocomplete("bl-skin", "bl-suggestions", "skin-blacklist-form");
wireSkinAutocomplete("po-skin", "po-suggestions", "price-override-form");

document.getElementById("skin-blacklist-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const skin = document.getElementById("bl-skin").value.trim();
  if (!skin) return;
  if (!confirm(`Permanently blacklist "${skin}"?\n\nEvery existing trade-up opportunity containing it (as input OR output) disappears, and the agents/scans/manual builder refuse it until you remove it from the list.`)) return;
  const res = await fetch("/api/skin-blacklist", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin, note: document.getElementById("bl-note").value.trim() }),
  });
  const data = await res.json();
  if (!data.ok) {
    _flashPanelMsg("skin-blacklist-msg", data.error || "failed", true);
    return;
  }
  document.getElementById("bl-skin").value = "";
  document.getElementById("bl-note").value = "";
  _flashPanelMsg("skin-blacklist-msg", `Blacklisted ${data.entry.skin} -- existing opportunities containing it are hidden now.`);
  await loadSkinBlacklist();
  loadBestSimulations();
});

document.getElementById("price-override-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const skin = document.getElementById("po-skin").value.trim();
  const price = parseFloat(document.getElementById("po-price").value);
  if (!skin || !(price > 0)) {
    _flashPanelMsg("price-override-msg", "Skin name and a positive price are required.", true);
    return;
  }
  const res = await fetch("/api/price-overrides", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      skin, price,
      wear: document.getElementById("po-wear").value,
      stattrak: document.getElementById("po-stattrak").checked,
      currency: parseInt(document.getElementById("po-currency").value || "1", 10),
    }),
  });
  const data = await res.json();
  if (!data.ok) {
    _flashPanelMsg("price-override-msg", data.error || "failed", true);
    return;
  }
  document.getElementById("po-skin").value = "";
  document.getElementById("po-price").value = "";
  _flashPanelMsg("price-override-msg", `Override set: ${data.entry.market_hash_name} = ${data.entry.price} -- existing opportunities re-priced.`);
  await loadPriceOverrides();
  loadBestSimulations();
});

document.getElementById("price-history-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const skin = document.getElementById("ph-skin").value.trim();
  const wear = document.getElementById("ph-wear").value;
  const stattrak = document.getElementById("ph-stattrak").checked;
  const currency = document.getElementById("ph-currency").value || 1;
  if (!skin) return;
  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "Looking up...";
  try {
    const params = new URLSearchParams({ skin, wear, stattrak: stattrak ? "true" : "false", currency });
    const res = await fetch(`/api/price-history?${params}`);
    const data = await res.json();
    renderPriceHistoryResult(skin, data);
  } catch (err) {
    document.getElementById("ph-prediction").textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
});

function renderPriceHistoryResult(skinName, data) {
  document.getElementById("ph-result").style.display = "block";
  document.getElementById("ph-name").textContent = data.market_hash_name || skinName;

  const imgEl = document.getElementById("ph-image");
  const cachedImage = state.skinImageCache[skinName];
  if (cachedImage) {
    imgEl.src = cachedImage;
    imgEl.style.display = "inline-block";
  } else {
    imgEl.style.display = "none";
  }

  const history = data.history || [];
  const predictionEl = document.getElementById("ph-prediction");

  if (typeof Chart !== "undefined") {
    const labels = history.map(h => new Date(h.ts * 1000).toLocaleDateString());
    const recorded = history.map(h => h.lowest_price);
    let chartLabels = labels;
    let predictedSeries = null;
    if (data.prediction) {
      chartLabels = [...labels, `+${data.prediction.horizon_hours.toFixed(0)}h`];
      recorded.push(null); // don't extend the solid "recorded" line into the predicted point
      predictedSeries = new Array(Math.max(labels.length - 1, 0)).fill(null)
        .concat([history.length ? history[history.length - 1].lowest_price : null, data.prediction.predicted_price]);
    }
    const datasets = [{ label: "Recorded lowest price", data: recorded, borderColor: "#d4af37",
                         backgroundColor: "rgba(212,175,55,0.15)", fill: true, tension: 0.15, spanGaps: false }];
    if (predictedSeries) {
      datasets.push({ label: "Predicted trend", data: predictedSeries, borderColor: "#8ea1ff",
                       borderDash: [6, 4], fill: false, spanGaps: true,
                       pointRadius: predictedSeries.map((_, i) => (i === predictedSeries.length - 1 ? 5 : 0)) });
    }
    const ctx = document.getElementById("price-history-chart");
    if (state.charts.priceHistory) state.charts.priceHistory.destroy();
    state.charts.priceHistory = new Chart(ctx, {
      type: "line",
      data: { labels: chartLabels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: CHART_TEXT_COLOR } } },
        scales: { x: { ticks: { color: CHART_AXIS_COLOR } }, y: { ticks: { color: CHART_AXIS_COLOR } } },
      },
    });
  }

  if (!history.length) {
    predictionEl.textContent = "No price data could be fetched for this skin/wear -- double-check the spelling, or Steam may be rate-limited right now.";
  } else if (!data.prediction) {
    predictionEl.textContent = `${history.length} point(s) recorded so far -- need ${data.min_points_required}+ before a trend prediction is shown. History only builds going forward from real lookups, it can't be backfilled.`;
  } else {
    const p = data.prediction;
    const dir = p.slope_per_day >= 0 ? "up" : "down";
    predictionEl.textContent = `Trending ${dir} ~${Math.abs(p.slope_per_day).toFixed(3)}/day based on ${p.n_points} points ` +
      `(R²=${p.r_squared.toFixed(2)}) -- naive linear-trend prediction ${p.horizon_hours.toFixed(0)}h ahead: ` +
      `${p.predicted_price.toFixed(2)}. Coursework-level baseline, not financial advice.`;
  }
}

// ---- Manual trade-up builder (calculator page) -----------------------------------------
// Compose a contract from ANY skins (mixing collections is the point -- group-based odds),
// checked for real eligibility and priced via POST /api/manual-tradeup, which runs the same
// rules_engine.validate_contract -> simulate.simulate path the agents use. All builder state
// lives in state.manualBuilder (poll-survival rule), rendered by renderManualBuilder().

let mbSelectedSkin = null;  // the /api/skins entry picked from the suggestion box
let mbSuggestTimer = null;

document.getElementById("mb-skin").addEventListener("input", (e) => {
  clearTimeout(mbSuggestTimer);
  mbSelectedSkin = null;
  fillMbWears();
  const q = e.target.value.trim();
  const box = document.getElementById("mb-suggestions");
  if (q.length < 2) { box.style.display = "none"; return; }
  mbSuggestTimer = setTimeout(async () => {
    const res = await fetch(`/api/skins?q=${encodeURIComponent(q)}`);
    const skins = await res.json();
    skins.forEach(s => { state.skinImageCache[s.name] = s.image; });
    if (!skins.length) { box.style.display = "none"; return; }
    box.innerHTML = skins.map((s, i) => `
      <div class="suggestion-item" data-i="${i}">
        ${s.image ? `<img class="skin-thumb" src="${s.image}" alt="" loading="lazy" onerror="this.style.display='none'">` : ""}${s.name}
        <span class="muted-inline">${s.rarity_name || ""}${s.tradeable === false ? " — ⚠ can't trade up" : ""}</span>
      </div>`).join("");
    box.style.display = "block";
    box.querySelectorAll(".suggestion-item").forEach(el => {
      el.addEventListener("click", () => {
        mbSelectedSkin = skins[Number(el.dataset.i)];
        document.getElementById("mb-skin").value = mbSelectedSkin.name;
        fillMbWears();
        box.style.display = "none";
      });
    });
  }, 250);
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#mb-form")) {
    const box = document.getElementById("mb-suggestions");
    if (box) box.style.display = "none";
  }
});

function fillMbWears() {
  const sel = document.getElementById("mb-wear");
  const wears = mbSelectedSkin ? mbSelectedSkin.wears
    : ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"];
  sel.innerHTML = wears.map(w => `<option${w === "Field-Tested" ? " selected" : ""}>${w}</option>`).join("");
}
fillMbWears();

function mbRequiredCount() {
  return (state.constants && state.constants.standard_input_count) || 10;
}

function mbTotalUnits() {
  return state.manualBuilder.units.reduce((sum, u) => sum + u.count, 0);
}

document.getElementById("mb-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const mb = state.manualBuilder;
  const name = document.getElementById("mb-skin").value.trim();
  const wear = document.getElementById("mb-wear").value;
  const count = Math.max(1, parseInt(document.getElementById("mb-count").value, 10) || 1);
  if (!name) return;
  if (!mbSelectedSkin || mbSelectedSkin.name !== name) {
    mb.error = "Pick the skin from the suggestion list so its collection/wears are known.";
    renderManualBuilder();
    return;
  }
  const need = mbRequiredCount();
  const room = need - mbTotalUnits();
  if (room <= 0) {
    mb.error = `A contract takes exactly ${need} inputs -- remove something first.`;
    renderManualBuilder();
    return;
  }
  const existing = mb.units.find(u => u.skin === name && u.wear === wear);
  if (existing) existing.count += Math.min(count, room);
  else mb.units.push({
    skin: name, wear, count: Math.min(count, room),
    collection: (mbSelectedSkin.collections || [])[0] || "",
    rarity_name: mbSelectedSkin.rarity_name || "", image: mbSelectedSkin.image || "",
    tradeable: mbSelectedSkin.tradeable !== false,
    tradeable_reason: mbSelectedSkin.tradeable_reason || "",
  });
  mb.error = null;
  mb.result = null;
  mb.problems = [];
  document.getElementById("mb-skin").value = "";
  mbSelectedSkin = null;
  fillMbWears();
  renderManualBuilder();
});

document.getElementById("mb-clear-btn").addEventListener("click", () => {
  state.manualBuilder = { units: [], result: null, error: null, problems: [] };
  renderManualBuilder();
});

document.getElementById("mb-compute-btn").addEventListener("click", async () => {
  const mb = state.manualBuilder;
  const btn = document.getElementById("mb-compute-btn");
  btn.disabled = true;
  btn.textContent = "Pricing every input & output (live)…";
  mb.error = null;
  mb.problems = [];
  mb.result = null;
  try {
    const res = await fetch("/api/manual-tradeup", {
      method: "POST",
      body: JSON.stringify({
        stattrak: document.getElementById("mb-stattrak").checked,
        currency: parseInt(document.getElementById("mb-currency").value, 10) || 1,
        units: mb.units.map(u => ({ skin: u.skin, wear: u.wear, count: u.count })),
      }),
    });
    const data = await res.json();
    if (data.ok) mb.result = data.result;
    else { mb.error = data.error; mb.problems = data.problems || []; }
  } catch (e) {
    mb.error = String(e);
  }
  renderManualBuilder();
});

function renderManualBuilder() {
  const mb = state.manualBuilder;
  const need = mbRequiredCount();
  const total = mbTotalUnits();
  const table = document.getElementById("mb-units-table");
  const tbody = table.querySelector("tbody");
  const problemsBySkin = {};
  (mb.problems || []).forEach(p => { problemsBySkin[p.skin] = p.detail || p.issue; });
  table.style.display = mb.units.length ? "" : "none";
  tbody.innerHTML = mb.units.map((u, i) => {
    const problem = problemsBySkin[u.skin] || (!u.tradeable
      ? ((state.constants && state.constants.tradability_reasons || {})[u.tradeable_reason] || u.tradeable_reason)
      : "");
    return `
    <tr>
      <td>${copyableName(u.skin, u.image)}</td>
      <td>${u.collection}</td>
      <td>${u.rarity_name}</td>
      <td>${u.wear}</td>
      <td class="num">×${u.count}</td>
      <td>${problem ? `<span class="tag-drought" title="${problem}">⚠ ${problem}</span>` : `<span class="tag-same">ok</span>`}</td>
      <td><button type="button" class="secondary mb-remove-btn" data-i="${i}">Remove</button></td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll(".mb-remove-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      mb.units.splice(Number(btn.dataset.i), 1);
      mb.result = null;
      mb.problems = [];
      renderManualBuilder();
    });
  });

  document.getElementById("mb-total-label").textContent = mb.units.length ? `(${total}/${need} inputs)` : "";
  const computeBtn = document.getElementById("mb-compute-btn");
  computeBtn.disabled = total !== need;
  computeBtn.textContent = `Compute trade-up (${total}/${need})`;

  document.getElementById("mb-messages").innerHTML = mb.error
    ? `<p class="empty-state">⚠ ${mb.error}</p>` : "";

  const resultEl = document.getElementById("mb-result");
  if (!mb.result) { resultEl.innerHTML = ""; return; }
  const r = mb.result;
  const collections = [...new Set(r.input_mix.map(m => m.collection))];
  const outputs = renderDrops(r.output_distribution.map(o => ({
    name: o.skin, image: o.image, wear: o.wear, probability_pct: o.prob_pct,
    price: o.price, volume: o.volume, profit_after_fee: o.net_profit, max_input_price: null,
    wear_probs: o.wear_probs,
  })));
  resultEl.innerHTML = `
    <div class="sim-detail">
      <div class="float-strategy ${r.expected_roi_pct >= 0 ? "float-better" : ""}">
        <strong>${collections.length > 1 ? `Mixed-collection contract (${collections.length} collections)` : "Single-collection contract"}</strong>
        -- ${r.from_rarity} → ${r.to_rarity}${r.stattrak ? " (StatTrak)" : ""}
        <div class="float-strategy-row">
          <span class="num">Cost ${fmtCurrency(r.cost)}</span>
          <span class="num">Expected revenue ${fmtCurrency(r.expected_revenue)} (${fmtCurrency(r.expected_revenue_after_fee)} after fee)</span>
          <span class="num ${r.expected_profit >= 0 ? "profit-positive" : "profit-negative"}">Profit ${fmtCurrency(r.expected_profit)}</span>
          <span class="num ${r.expected_roi_pct >= 0 ? "profit-positive" : "profit-negative"}">ROI ${r.expected_roi_pct.toFixed(1)}%</span>
        </div>
        ${r.low_volume_warning ? `<p class="disclaimer">⚠ at least one price here rests on thin/unreliable market data (dead-skin flagged) -- verify before spending money.</p>` : ""}
        <p class="disclaimer">Priced ${r.priced_output_variants}/${r.n_output_variants} possible outputs -- probability-weighted expected value across every priced output, ${(state.constants ? state.constants.steam_sell_fee_pct.toFixed(0) : "13")}% Steam fee applied. Theoretical estimate, not an order.</p>
      </div>
      ${renderSimInputs(r.input_mix)}
      <strong>Possible outputs</strong>
      ${outputs}
    </div>`;
}

// ---- Stage-4 panel wiring (Evolution / Rare Patterns / Backup) ------------------------------
document.getElementById("evolution-toggle-btn").addEventListener("click", async () => {
  const enabled = !(state.lastEvolution && state.lastEvolution.enabled);
  await fetch("/api/evolution/enable", { method: "POST", body: JSON.stringify({ enabled }) });
  await loadEvolutionStatus();
});
document.getElementById("evolution-tick-btn").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    await fetch("/api/evolution/tick", { method: "POST" });
    await loadEvolutionStatus();
  } finally {
    e.target.disabled = false;
  }
});

document.getElementById("patterns-scan-start-btn").addEventListener("click", async () => {
  await fetch("/api/patterns/scan/start", { method: "POST" });
  await loadPatternsStatus();
});
document.getElementById("patterns-scan-pause-btn").addEventListener("click", async () => {
  await fetch("/api/patterns/scan/pause", { method: "POST" });
  await loadPatternsStatus();
});
document.getElementById("patterns-refresh-btn").addEventListener("click", async (e) => {
  e.target.disabled = true;
  const resultEl = document.getElementById("patterns-refresh-result");
  resultEl.textContent = "refreshing…";
  try {
    const res = await fetch("/api/patterns/refresh", { method: "POST" });
    const data = await res.json();
    resultEl.textContent = data.ok
      ? `refreshed -- ${data.result.n_changes} change(s)${data.result.remote_verification_available ? "" : " (seed re-validation only -- no remote verifiers wired up)"}`
      : `failed: ${data.error}`;
    await Promise.all([loadPatternDb(), loadPatternsStatus()]);
  } catch (err) {
    resultEl.textContent = `failed: ${err}`;
  } finally {
    e.target.disabled = false;
  }
});
// Filter/sort changes re-fetch server-side-filtered opportunities immediately -- the values
// live in the inputs themselves and are re-read on every poll (see _patternFilterParams), so
// the 3s poll keeps respecting whatever is currently typed.
for (const id of ["pat-weapon", "pat-type", "pat-marketplace", "pat-min-profit", "pat-min-confidence",
                   "pat-min-float", "pat-max-float", "pat-tier", "pat-sort"]) {
  document.getElementById(id).addEventListener("change", () => loadPatternOpportunities());
}
document.getElementById("export-patterns-btn").addEventListener("click", (e) => {
  e.preventDefault();
  downloadCSV("rare-pattern-opportunities.csv", [
    { label: "item", value: o => o.market_hash_name },
    { label: "pattern", value: o => o.pattern_label },
    { label: "pattern_type", value: o => o.pattern_type },
    { label: "pattern_tier", value: o => o.pattern_tier },
    { label: "float", value: o => o.float_value },
    { label: "paint_seed", value: o => o.paint_seed },
    { label: "paint_index", value: o => o.paint_index },
    { label: "asking_price", value: o => o.asking_price },
    { label: "base_market_price", value: o => o.base_market_price },
    { label: "estimated_collector_value", value: o => o.estimated_collector_value },
    { label: "expected_profit", value: o => o.expected_profit },
    { label: "edge_pct", value: o => o.edge_pct },
    { label: "confidence", value: o => o.confidence },
    { label: "marketplace", value: o => o.marketplace },
    { label: "listing_url", value: o => o.listing_url },
    { label: "source_url", value: o => o.source_url },
    { label: "explanation", value: o => o.explanation },
  ], state.lastPatternOpps);
});

document.getElementById("backup-config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await fetch("/api/backup/config", {
    method: "POST",
    body: JSON.stringify({
      enabled: document.getElementById("backup-enabled").checked,
      repo_dir: document.getElementById("backup-repo-dir").value.trim() || null,
      remote: document.getElementById("backup-remote").value.trim() || "origin",
      branch: document.getElementById("backup-branch").value.trim() || "main",
    }),
  });
  await loadBackupStatus();
});
document.getElementById("backup-run-now-btn").addEventListener("click", async (e) => {
  e.target.disabled = true;
  const resultEl = document.getElementById("backup-run-result");
  resultEl.textContent = "running full backup cycle…";
  try {
    const res = await fetch("/api/backup/run-now", { method: "POST" });
    const data = await res.json();
    const r = data.result || {};
    resultEl.textContent = `${r.action || "?"}${r.archive ? ` -- ${r.archive}` : ""}${r.message ? ` -- ${r.message}` : ""}`;
    await loadBackupStatus();
  } catch (err) {
    resultEl.textContent = `failed: ${err}`;
  } finally {
    e.target.disabled = false;
  }
});

loadCurrencies().then(pollStatus);
loadConstants();
loadPatternDb();
setInterval(pollStatus, 3000);
