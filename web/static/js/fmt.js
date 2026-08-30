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
