import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client.js";
import { Card, PageHead, Badge, SkinImage, fmtPct, fmtMoney, Empty } from "../components/ui.jsx";
import SkinPicker from "../components/SkinPicker.jsx";
import { loadInventory, ownedCounts } from "../lib/inventory.js";

// Encode/decode a build into the URL hash so a contract can be saved or shared (TradeUpSpy's
// signature feature). Only the essentials travel; images/prices are re-fetched on load.
function encodeBuild(units, stattrak) {
  const payload = { s: stattrak ? 1 : 0, u: units.map((u) => [u.skin, u.wear, u.count]) };
  return btoa(encodeURIComponent(JSON.stringify(payload)));
}
function decodeBuild(hash) {
  try {
    const m = /[#&]b=([^&]+)/.exec(hash || "");
    if (!m) return null;
    const p = JSON.parse(decodeURIComponent(atob(m[1])));
    return { stattrak: !!p.s, units: (p.u || []).map(([skin, wear, count]) => ({ skin, wear, count })) };
  } catch {
    return null;
  }
}

export default function Calculator() {
  const [units, setUnits] = useState([]); // {skin, wear, count, image}
  const [stattrak, setStattrak] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [shared, setShared] = useState(false);
  const owned = useMemo(() => ownedCounts(loadInventory()), []);

  // Load a shared build from the URL hash — on mount, and if the hash changes while the page
  // is open (someone pastes a #b=... link into the address bar without a full reload).
  useEffect(() => {
    const apply = () => {
      const b = decodeBuild(window.location.hash);
      if (b && b.units.length) {
        setUnits(b.units.map((u) => ({ ...u, image: "" })));
        setStattrak(b.stattrak);
        setResult(null);
      }
    };
    apply();
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, []);

  function shareLink() {
    const url = `${window.location.origin}/calculator#b=${encodeBuild(units, stattrak)}`;
    navigator.clipboard?.writeText(url).then(() => { setShared(true); setTimeout(() => setShared(false), 2000); });
    window.history.replaceState(null, "", `#b=${encodeBuild(units, stattrak)}`);
  }

  const total = units.reduce((a, u) => a + u.count, 0);

  function addSkin(s) {
    const wear = s.wears?.[0] || "Field-Tested";
    setUnits((u) => [...u, { skin: s.name, wear, count: 1, image: s.image }]);
  }
  function update(i, patch) {
    setUnits((u) => u.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  }
  function remove(i) {
    setUnits((u) => u.filter((_, j) => j !== i));
  }

  async function compute() {
    setBusy(true); setError(null); setResult(null);
    try {
      const res = await api.manualTradeup({ units: units.map(({ skin, wear, count }) => ({ skin, wear, count })), stattrak, currency: 1 });
      if (res.ok === false) {
        setError(res.error + (res.problems ? ": " + res.problems.map((p) => `${p.skin} (${p.detail})`).join("; ") : ""));
      } else {
        setResult(res.result || res);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const r = result;
  const roi = r ? (r.expected_roi_pct ?? r.roi_pct) : null;

  return (
    <div>
      <PageHead title="Trade-up Calculator">
        Build any 10-skin contract and see the exact cost, the odds on every possible output, and the
        expected profit — the same math the bots use. Mixing collections is allowed.
      </PageHead>

      <Card title="1. Add your 10 input skins" caption={`${total}/10 added`}>
        <div style={{ maxWidth: 480, marginBottom: 16 }}>
          <SkinPicker onPick={addSkin} />
        </div>
        {units.length === 0 ? (
          <Empty>Search a skin above to start building.</Empty>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th>Skin</th><th>Wear</th><th className="num">Count</th><th>Owned</th><th></th></tr></thead>
              <tbody>
                {units.map((u, i) => {
                  const have = owned[`${u.skin}|${u.wear}`] || 0;
                  return (
                    <tr key={i}>
                      <td><div className="skin-cell"><SkinImage url={u.image} name={u.skin} />{u.skin}</div></td>
                      <td>
                        <select value={u.wear} onChange={(e) => update(i, { wear: e.target.value })} style={{ width: "auto" }}>
                          {["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"].map((w) => <option key={w}>{w}</option>)}
                        </select>
                      </td>
                      <td className="num"><input type="number" min="1" max="10" value={u.count} onChange={(e) => update(i, { count: Math.max(1, Math.min(10, +e.target.value)) })} style={{ width: 64 }} /></td>
                      <td>{have > 0 ? <Badge tone="win">{have} owned</Badge> : "—"}</td>
                      <td><button className="btn secondary" onClick={() => remove(i)}>Remove</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div style={{ display: "flex", alignItems: "center", gap: 16, marginTop: 16, flexWrap: "wrap" }}>
          <label className="switch"><input type="checkbox" checked={stattrak} onChange={(e) => setStattrak(e.target.checked)} /><span className="track" /> StatTrak</label>
          <button className="btn" disabled={total !== 10 || busy} onClick={compute}>
            {busy ? "Computing…" : total === 10 ? "Compute trade-up" : `Add ${10 - total} more`}
          </button>
          {units.length > 0 && (
            <button className="btn secondary" onClick={shareLink} title="Copy a link that reopens this exact build">
              {shared ? "Link copied" : "Copy shareable link"}
            </button>
          )}
        </div>
        {error && <p style={{ color: "var(--loss)", marginTop: 12 }}>{error}</p>}
      </Card>

      {r && (
        <Card title="2. Result">
          <div className="grid cols-4" style={{ marginBottom: 16 }}>
            <div className="stat"><div className="label">Cost</div><div className="value">{fmtMoney(r.cost)}</div></div>
            <div className="stat"><div className="label">Expected revenue</div><div className="value">{fmtMoney(r.expected_revenue_after_fee ?? r.expected_revenue)}</div></div>
            <div className="stat"><div className="label">Profit</div><div className={`value ${(r.expected_profit ?? 0) >= 0 ? "pos" : "neg"}`}>{fmtMoney(r.expected_profit ?? r.profit)}</div></div>
            <div className="stat"><div className="label">ROI</div><div className={`value ${roi >= 0 ? "pos" : "neg"}`}>{fmtPct(roi, 1)}</div></div>
          </div>
          {(() => {
            const outs = Array.isArray(r.output_distribution) ? r.output_distribution : [];
            const worst = outs.length ? outs.reduce((a, b) => ((a.net_profit ?? 0) <= (b.net_profit ?? 0) ? a : b)) : null;
            const winChance = outs.filter((d) => (d.net_profit ?? 0) >= 0).reduce((a, d) => a + (d.prob_pct || 0), 0);
            const breakEven = r.expected_revenue_after_fee;
            return (
              <div className="grid cols-3" style={{ marginBottom: 16 }}>
                <div className="stat">
                  <div className="label">Break-even cost</div>
                  <div className="value">{fmtMoney(breakEven)}</div>
                  <div className="sub">buy the 10 inputs for less than this and you profit on average</div>
                </div>
                <div className="stat">
                  <div className="label">Win chance</div>
                  <div className={`value ${winChance >= 50 ? "pos" : "neg"}`}>{fmtPct(winChance, 0)}</div>
                  <div className="sub">odds the output is worth more than you paid</div>
                </div>
                <div className="stat">
                  <div className="label">Worst case</div>
                  <div className="value neg">{worst ? fmtMoney(worst.net_profit) : "—"}</div>
                  <div className="sub">{worst ? `${worst.skin} (${fmtPct(worst.prob_pct, 1)} chance)` : ""}</div>
                </div>
              </div>
            );
          })()}
          <p className="section-caption" style={{ marginBottom: 12 }}>
            Average input float (normalized): <b>{(r.avg_input_float ?? 0).toFixed(4)}</b> — lower steers outputs toward cleaner wears.
            Each output's wear-landing odds are baked into the table below.
          </p>
          {Array.isArray(r.output_distribution) && (
            <div className="tbl-wrap">
              <table className="tbl">
                <thead><tr><th>Possible output</th><th className="num">Chance</th><th className="num">Value</th><th className="num">Net profit</th></tr></thead>
                <tbody>
                  {[...r.output_distribution].sort((a, b) => (b.prob_pct || 0) - (a.prob_pct || 0)).map((d, i) => (
                    <tr key={i}>
                      <td><div className="skin-cell"><SkinImage url={d.image} name={d.skin} />{d.skin} <span style={{ color: "var(--text-dim)" }}>({d.wear})</span></div></td>
                      <td className="num">{fmtPct(d.prob_pct, 1)}</td>
                      <td className="num">{fmtMoney(d.price)}</td>
                      <td className="num" style={{ color: (d.net_profit ?? 0) >= 0 ? "var(--win)" : "var(--loss)" }}>{fmtMoney(d.net_profit)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
