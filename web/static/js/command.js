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
