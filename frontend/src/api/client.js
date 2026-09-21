// Thin fetch wrappers over the Python server's /api/* routes (web/server.py). Every call goes
// through here so error handling and JSON parsing live in one place. In dev, Vite proxies /api
// to :5000; in production the same server serves both the bundle and the API, so relative URLs
// just work.

async function getJSON(path, params) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    }
  }
  const res = await fetch(url.pathname + url.search);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

export const api = {
  status: () => getJSON("/api/status"),
  constants: () => getJSON("/api/constants"),
  currencies: () => getJSON("/api/currencies"),
  networkStats: () => getJSON("/api/network-stats"),

  tradeups: () => getJSON("/api/tradeups"),
  anomalies: () => getJSON("/api/anomalies"),
  covert: () => getJSON("/api/covert"),

  bestSimulations: () => getJSON("/api/sim-population/best-simulations"),
  archive: (params) => getJSON("/api/sim-population/opportunities/archive", params),
  leaderboards: () => getJSON("/api/sim-population/leaderboards"),
  simStatus: () => getJSON("/api/sim-population/status"),

  patternsStatus: () => getJSON("/api/patterns/status"),
  patternsOpps: (params) => getJSON("/api/patterns/opportunities", params),
  patternsDb: () => getJSON("/api/patterns/db"),

  outcomes: () => getJSON("/api/outcomes"),
  modelStatus: () => getJSON("/api/model-status"),
  connections: () => getJSON("/api/connections/status"),

  skins: (q) => getJSON("/api/skins", { q, limit: 20 }),

  manualTradeup: (body) => postJSON("/api/manual-tradeup", body),

  // Inventory (Phase 3)
  inventory: (steamid) => getJSON("/api/inventory", { steamid }),
  inventoryCached: () => getJSON("/api/inventory/cached"),

  scanNow: () => postJSON("/api/scan-now"),
  pause: () => postJSON("/api/pause"),
  resume: () => postJSON("/api/resume"),
  stop: () => postJSON("/api/stop"),
  setConfig: (cfg) => postJSON("/api/config", cfg),

  // ---- Simulation Lab (6-agent ML population) ----
  simConvergence: () => getJSON("/api/sim-population/convergence"),
  roundRobinStatus: () => getJSON("/api/sim-population/round-robin/status"),
  roundRobinStart: () => postJSON("/api/sim-population/round-robin/start"),
  roundRobinStop: () => postJSON("/api/sim-population/round-robin/stop"),
  simResetAll: () => postJSON("/api/sim-population/reset-all"),
  sonAction: (id, action) => postJSON(`/api/sim-population/${id}/${action}`), // start|resume|pause|stop|nudge|reset|train
  roundHistory: () => getJSON("/api/sim-population/round-history"),
  verifyQueueStatus: () => getJSON("/api/verify-queue/status"),
  verifyQueueStart: () => postJSON("/api/verify-queue/start"),
  verifyQueueStop: () => postJSON("/api/verify-queue/stop"),

  // ---- ML ROI-calibration model + loops ----
  retrainStatus: () => getJSON("/api/retrain-loop/status"),
  retrainStart: () => postJSON("/api/retrain-loop/start"),
  retrainStop: () => postJSON("/api/retrain-loop/stop"),
  trainModel: () => postJSON("/api/train-model"),
  resetModel: () => postJSON("/api/reset-model"),
  outcomesStats: () => getJSON("/api/outcomes/stats"),
  logOutcome: (body) => postJSON("/api/log-outcome", body),

  // ---- Bulk price scan ----
  priceScanStatus: () => getJSON("/api/price-scan/status"),
  priceScanStart: () => postJSON("/api/price-scan/start"),
  priceScanPause: () => postJSON("/api/price-scan/pause"),
  priceScanStop: () => postJSON("/api/price-scan/stop"),

  // ---- Market intelligence: user overrides ----
  priceOverrides: () => getJSON("/api/price-overrides"),
  setPriceOverride: (body) => postJSON("/api/price-overrides", body),
  removePriceOverride: (market_hash_name) => postJSON("/api/price-overrides/remove", { market_hash_name }),
  skinBlacklist: () => getJSON("/api/skin-blacklist"),
  addBlacklist: (body) => postJSON("/api/skin-blacklist", body),
  removeBlacklist: (skin) => postJSON("/api/skin-blacklist/remove", { skin }),

  // ---- Market intelligence: registries (read + clear) ----
  deadSkins: () => getJSON("/api/dead-skins"),
  invalidSkins: () => getJSON("/api/invalid-skins"),
  clearInvalidSkins: () => postJSON("/api/invalid-skins/clear"),
  mispricedSkins: () => getJSON("/api/mispriced-skins"),
  clearMispricedSkins: () => postJSON("/api/mispriced-skins/clear"),
  wearAnomalies: () => getJSON("/api/wear-anomalies"),
  clearWearAnomalies: () => postJSON("/api/wear-anomalies/clear"),
  marketReliability: () => getJSON("/api/market-reliability"),

  // ---- Opportunity blacklist (mark trade-up done) ----
  simBlacklist: () => getJSON("/api/sim-population/blacklist"),
};

export { getJSON, postJSON };
