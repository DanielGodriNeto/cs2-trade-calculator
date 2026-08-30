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
