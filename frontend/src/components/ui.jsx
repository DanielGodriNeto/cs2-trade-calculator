// Small shared presentational components + formatting helpers used across every view.
// Kept deliberately tiny and dependency-free so the whole design system reads at a glance.

export function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v >= 0 ? "" : ""}${v.toFixed(digits)}%`;
}

export function fmtMoney(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function fmtNum(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString();
}

export function timeAgo(epochSecOrMs) {
  if (!epochSecOrMs) return "—";
  const ms = epochSecOrMs > 1e12 ? epochSecOrMs : epochSecOrMs * 1000;
  const diff = Date.now() - ms;
  if (diff < 0) return "just now";
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function Card({ title, caption, actions, children, className = "" }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: caption ? 4 : 12 }}>
          {title && <h3 style={{ margin: 0 }}>{title}</h3>}
          {actions && <div style={{ display: "flex", gap: 8 }}>{actions}</div>}
        </div>
      )}
      {caption && <p className="section-caption" style={{ marginBottom: 16 }}>{caption}</p>}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, tone, help }) {
  const cls = tone === "pos" ? "value pos" : tone === "neg" ? "value neg" : "value";
  return (
    <div className="stat">
      <div className="label">
        {label}
        {help && <Help text={help} />}
      </div>
      <div className={cls}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

export function Badge({ children, tone }) {
  return <span className={`badge ${tone || ""}`}>{children}</span>;
}

export function Help({ text }) {
  return (
    <span className="help" title={text} aria-label={text}>
      ?
    </span>
  );
}

// Steam-CDN skin thumbnail. Hides itself gracefully on load error (same trick the old
// app.js used). `url` may be a full URL or a Steam icon hash fragment.
export function SkinImage({ url, name, size = 46 }) {
  if (!url) return null;
  const src = url.startsWith("http")
    ? url
    : `https://community.fastly.steamstatic.com/economy/image/${url}/${size * 2}x${size}`;
  return (
    <img
      className="skin-img"
      src={src}
      alt=""
      loading="lazy"
      onError={(e) => {
        e.currentTarget.style.display = "none";
      }}
      title={name || ""}
    />
  );
}

export function Toggle({ checked, onChange, children }) {
  return (
    <label className="switch">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="track" />
      {children && <span>{children}</span>}
    </label>
  );
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>;
}

export function PageHead({ title, children }) {
  return (
    <div className="page-head">
      <h1>{title}</h1>
      {children && <p>{children}</p>}
    </div>
  );
}

// Risk level from a 0-100 confidence score, matching the old dashboard's thresholds.
export function riskFromConfidence(conf) {
  if (conf === null || conf === undefined) return { label: "unknown", tone: "" };
  if (conf >= 70) return { label: "low risk", tone: "win" };
  if (conf >= 40) return { label: "medium risk", tone: "warn" };
  return { label: "high risk", tone: "loss" };
}
