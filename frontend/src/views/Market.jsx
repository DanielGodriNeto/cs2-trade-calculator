import { useMemo, useState } from "react";
import { usePoll } from "../hooks/usePoll.js";
import { api } from "../api/client.js";
import { Card, PageHead, Badge, fmtMoney, fmtPct, timeAgo, Empty } from "../components/ui.jsx";
import SkinPicker from "../components/SkinPicker.jsx";

const WEARS = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"];

function PriceOverrides() {
  const { data, refetch } = usePoll("priceOverrides", api.priceOverrides, 0);
  const [skin, setSkin] = useState("");
  const [wear, setWear] = useState("Field-Tested");
  const [stattrak, setStattrak] = useState(false);
  const [price, setPrice] = useState("");
  const [msg, setMsg] = useState(null);
  const list = Array.isArray(data) ? data : [];

  async function save() {
    setMsg(null);
    const res = await api.setPriceOverride({ skin, wear, stattrak, price: Number(price), currency: 1 });
    if (res.ok === false) setMsg(res.error);
    else { setSkin(""); setPrice(""); refetch(); }
  }
  async function remove(name) { await api.removePriceOverride(name); refetch(); }

  return (
    <Card title="Manual price overrides" caption="Set your own price for a skin+wear — it wins over every source and instantly re-prices every opportunity, so a bogus price behind an absurd ROI is fixed at once.">
      <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 12 }}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <label className="field">Skin name<SkinPicker onPick={(s) => setSkin(s.name)} /></label>
          {skin && <div style={{ fontSize: "0.78rem", color: "var(--accent)", marginTop: 4 }}>{skin}</div>}
        </div>
        <label className="field">Wear<select value={wear} onChange={(e) => setWear(e.target.value)}>{WEARS.map((w) => <option key={w}>{w}</option>)}</select></label>
        <label className="switch" style={{ marginBottom: 8 }}><input type="checkbox" checked={stattrak} onChange={(e) => setStattrak(e.target.checked)} /><span className="track" /> StatTrak</label>
        <label className="field" style={{ maxWidth: 120 }}>Price<input type="number" step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="e.g. 1.50" /></label>
        <button className="btn" disabled={!skin || !price} onClick={save}>Set price</button>
      </div>
      {msg && <p style={{ color: "var(--loss)" }}>{msg}</p>}
      {list.length === 0 ? <Empty>No price overrides set.</Empty> : (
        <div className="tbl-wrap"><table className="tbl">
          <thead><tr><th>Skin</th><th className="num">Price</th><th>Added</th><th></th></tr></thead>
          <tbody>{list.map((o) => (
            <tr key={o.market_hash_name}><td>{o.market_hash_name}</td><td className="num">{fmtMoney(o.price)}</td><td>{timeAgo(o.added_at)}</td>
              <td><button className="btn secondary" onClick={() => remove(o.market_hash_name)}>Remove</button></td></tr>
          ))}</tbody>
        </table></div>
      )}
    </Card>
  );
}

function SkinBlacklist() {
  const { data, refetch } = usePoll("skinBlacklist", api.skinBlacklist, 0);
  const [skin, setSkin] = useState("");
  const [duration, setDuration] = useState("");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState(null);
  const list = Array.isArray(data) ? data : [];

  async function add() {
    setMsg(null);
    const res = await api.addBlacklist({ skin, note, duration_days: duration || undefined });
    if (res.ok === false) setMsg(res.error);
    else { setSkin(""); setNote(""); refetch(); }
  }
  async function remove(s) { await api.removeBlacklist(s); refetch(); }

  return (
    <Card title="Skin blacklist" caption="Ban a skin everywhere — as an input and an output. Every opportunity containing it disappears at once, and agents stop proposing it. Use it for skins whose price data you've confirmed is garbage (e.g. the one behind an absurd ROI).">
      <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 12 }}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <label className="field">Skin name<SkinPicker onPick={(s) => setSkin(s.name)} /></label>
          {skin && <div style={{ fontSize: "0.78rem", color: "var(--accent)", marginTop: 4 }}>{skin}</div>}
        </div>
        <label className="field">Duration<select value={duration} onChange={(e) => setDuration(e.target.value)}>
          <option value="">Forever</option><option value="1">1 day</option><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option>
        </select></label>
        <label className="field" style={{ flex: 1, minWidth: 160 }}>Note<input type="text" value={note} onChange={(e) => setNote(e.target.value)} placeholder="why, e.g. bogus mirror price" /></label>
        <button className="btn" disabled={!skin} onClick={add}>Blacklist skin</button>
      </div>
      {msg && <p style={{ color: "var(--loss)" }}>{msg}</p>}
      {list.length === 0 ? <Empty>No skins blacklisted.</Empty> : (
        <div className="tbl-wrap"><table className="tbl">
          <thead><tr><th>Skin</th><th>Note</th><th>Added</th><th>Expires</th><th></th></tr></thead>
          <tbody>{list.map((b) => (
            <tr key={b.skin}><td>{b.skin}</td><td>{b.note || "—"}</td><td>{timeAgo(b.added_at)}</td><td>{b.expires_at ? timeAgo(b.expires_at) : "never"}</td>
              <td><button className="btn secondary" onClick={() => remove(b.skin)}>Remove</button></td></tr>
          ))}</tbody>
        </table></div>
      )}
    </Card>
  );
}

function FlaggedSkins() {
  const { data } = usePoll("marketReliability", api.marketReliability, 0);
  const flagged = useMemo(() => (Array.isArray(data) ? data : []).filter((r) => r.state !== "healthy"), [data]);
  return (
    <Card title={`Flagged skins (${flagged.length})`} caption="Skins the reliability engine rates Suspicious or Excluded — thin markets, verified-mispriced, or abnormal price swings. Excluded skins are kept out of ranking and training. This is usually where a bogus 3000%-ROI price hides.">
      {flagged.length === 0 ? <Empty>Nothing flagged — all tracked skins look healthy.</Empty> : (
        <div className="tbl-wrap" style={{ maxHeight: 420, overflowY: "auto" }}><table className="tbl">
          <thead><tr><th>Skin</th><th>State</th><th className="num">Score</th><th>Reasons</th><th className="num">Gap %</th><th>Checked</th></tr></thead>
          <tbody>{flagged.slice(0, 200).map((r) => (
            <tr key={r.market_hash_name}>
              <td>{r.market_hash_name}</td>
              <td><Badge tone={r.state === "excluded" ? "loss" : "warn"}>{r.state}{r.overridden ? " (override)" : ""}</Badge></td>
              <td className="num">{r.reliability_score?.toFixed?.(0) ?? "—"}</td>
              <td style={{ fontSize: "0.8rem", color: "var(--text-2)" }}>{(r.reasons || []).join(", ") || "—"}</td>
              <td className="num">{r.mispriced_gap_pct != null ? fmtPct(r.mispriced_gap_pct, 0) : "—"}</td>
              <td>{timeAgo(r.computed_at)}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </Card>
  );
}

export default function Market() {
  return (
    <div>
      <PageHead title="Market Intelligence">
        The tools that keep bad price data out of your opportunities: override a wrong price, blacklist a
        garbage skin, and see which skins the reliability engine has flagged. This is how you kill a
        bogus "3000% ROI" find — override the inflated output price or blacklist the skin.
      </PageHead>
      <PriceOverrides />
      <SkinBlacklist />
      <FlaggedSkins />
    </div>
  );
}
