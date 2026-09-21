import { useMemo, useState } from "react";
import { usePoll } from "../hooks/usePoll.js";
import { api } from "../api/client.js";
import { Card, PageHead, Badge, SkinImage, fmtPct, fmtMoney, Empty, riskFromConfidence } from "../components/ui.jsx";
import { loadInventory, ownedCounts } from "../lib/inventory.js";

// A "suspicious" ROI almost always rests on a bogus (over-valued) output price rather than real
// edge — the classic dashboard leaned on the Trust badge + market-reliability exclusions for
// this. We surface it explicitly so a 3000% row reads as "check the data", not "free money".
const SANE_ROI_CEILING = 200;

// Inline kill-tools: blacklist a garbage skin, or override its price — the same fixes the old
// dashboard put right on each input/output row. After either, the opportunities re-price and the
// bad row drops out (min-ROI / reliability filters catch it).
function KillButtons({ skin, wear, stattrak, onDone }) {
  async function blacklist(e) {
    e.stopPropagation();
    if (!confirm(`Blacklist "${skin}" everywhere (input and output)? Every trade-up using it disappears.`)) return;
    await api.addBlacklist({ skin, note: "flagged from Opportunities" });
    onDone?.();
  }
  async function override(e) {
    e.stopPropagation();
    const val = prompt(`Set your own price for ${skin} (${wear || "any wear"}). This wins over every source and re-prices instantly.`);
    if (val == null || val === "") return;
    const price = Number(val);
    if (!(price > 0)) { alert("Enter a positive number."); return; }
    await api.setPriceOverride({ skin, wear: wear || "Field-Tested", stattrak: !!stattrak, price, currency: 1 });
    onDone?.();
  }
  return (
    <span style={{ whiteSpace: "nowrap" }}>
      <button className="btn secondary" style={{ padding: "1px 6px", fontSize: "0.72rem" }} title="Set a correct price for this skin" onClick={override}>$ fix</button>{" "}
      <button className="btn secondary" style={{ padding: "1px 6px", fontSize: "0.72rem" }} title="Blacklist this skin everywhere" onClick={blacklist}>ban</button>
    </span>
  );
}

function OpportunityDetail({ o, owned, onDone }) {
  const outputs = [...(o.output_distribution || [])].sort((a, b) => (b.prob_pct || 0) - (a.prob_pct || 0));
  const sb = o.score_breakdown || {};
  const st = o.stattrak;
  return (
    <tr>
      <td colSpan={7} style={{ background: "var(--surface-2)" }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24, padding: 8 }}>
          <div>
            <h4>Inputs (10 skins)</h4>
            <table className="tbl">
              <thead><tr><th>Skin</th><th className="num">×</th><th className="num">Unit</th><th>Owned</th><th>Fix</th></tr></thead>
              <tbody>
                {(o.input_mix || []).map((m, i) => {
                  const own = owned[`${m.skin}|${m.wear}`] || 0;
                  return (
                    <tr key={i}>
                      <td><div className="skin-cell"><SkinImage url={m.image} name={m.skin} />{m.skin} <span style={{ color: "var(--text-dim)" }}>({m.wear})</span></div></td>
                      <td className="num">{m.count}</td>
                      <td className="num">{fmtMoney(m.unit_price)}</td>
                      <td>{own > 0 ? <Badge tone="win">{Math.min(own, m.count)}/{m.count}</Badge> : "—"}</td>
                      <td><KillButtons skin={m.skin} wear={m.wear} stattrak={st} onDone={onDone} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div>
            <h4>Possible outputs <span style={{ fontWeight: 400, color: "var(--text-dim)", fontSize: "0.8rem" }}>— a wrong (inflated) price here is what fakes a high ROI</span></h4>
            <table className="tbl">
              <thead><tr><th>Skin</th><th className="num">Chance</th><th className="num">Value</th><th className="num">Net</th><th>Fix</th></tr></thead>
              <tbody>
                {outputs.slice(0, 10).map((d, i) => (
                  <tr key={i}>
                    <td><div className="skin-cell"><SkinImage url={d.image} name={d.skin} />{d.skin}</div></td>
                    <td className="num">{fmtPct(d.prob_pct, 1)}</td>
                    <td className="num">{fmtMoney(d.price)}{(d.dead_skin_reasons || []).includes("verified_overvalued") && <Badge tone="loss">overvalued</Badge>}</td>
                    <td className="num" style={{ color: d.net_profit >= 0 ? "var(--win)" : "var(--loss)" }}>{fmtMoney(d.net_profit)}</td>
                    <td><KillButtons skin={d.skin} wear={d.wear} stattrak={st} onDone={onDone} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h4 style={{ marginTop: 16 }}>Why it's ranked here</h4>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, fontSize: "0.82rem", color: "var(--text-2)" }}>
              {Object.keys(sb).length ? (
                Object.entries(sb).map(([k, v]) => (
                  <span key={k} className="badge">{k}: <b style={{ marginLeft: 4, color: "var(--text)" }}>{typeof v === "number" ? v.toFixed(1) : String(v)}</b></span>
                ))
              ) : (
                <span>Blended score: ROI 60% · profit 15% · liquidity 10% · confidence 10% · discovery 5%, dampened by market-reliability.</span>
              )}
            </div>
          </div>
        </div>
      </td>
    </tr>
  );
}

const SORTS = {
  trust: { label: "Best (ROI + trust)", fn: (a, b) => (b.composite_score || 0) - (a.composite_score || 0) },
  roi: { label: "Highest ROI", fn: (a, b) => roiOf(b) - roiOf(a) },
  profit: { label: "Most profit", fn: (a, b) => (b.profit_amount ?? b.cost * roiOf(b) / 100) - (a.profit_amount ?? a.cost * roiOf(a) / 100) },
  costLow: { label: "Cheapest first", fn: (a, b) => (a.cost || 0) - (b.cost || 0) },
  confidence: { label: "Most trusted", fn: (a, b) => (b.confidence_score || 0) - (a.confidence_score || 0) },
};
const roiOf = (t) => t.verified_roi_pct ?? t.expected_roi_pct ?? 0;

export default function Opportunities() {
  const { data, isLoading, refetch } = usePoll("best", api.bestSimulations, 4000);
  const [expanded, setExpanded] = useState(null);
  const [minRoi, setMinRoi] = useState("");
  const [risk, setRisk] = useState("");
  const [ownedOnly, setOwnedOnly] = useState(false);
  const [hideSuspicious, setHideSuspicious] = useState(false);
  const [sort, setSort] = useState("trust");
  const inv = loadInventory();
  const owned = useMemo(() => ownedCounts(inv), [inv]);

  const rows = useMemo(() => {
    let items = (data || []).slice();
    if (minRoi !== "") items = items.filter((i) => roiOf(i) >= Number(minRoi));
    if (risk) {
      items = items.filter((i) => {
        const c = i.confidence_score || 0;
        return risk === "low" ? c >= 70 : risk === "medium" ? c >= 40 && c < 70 : c < 40;
      });
    }
    if (hideSuspicious) items = items.filter((i) => roiOf(i) <= SANE_ROI_CEILING || i.verified_roi_pct != null);
    items = items.map((i) => {
      let ownedInputs = 0, oop = 0;
      for (const m of i.input_mix || []) {
        const have = Math.min(owned[`${m.skin}|${m.wear}`] || 0, m.count);
        ownedInputs += have;
        oop += (m.count - have) * (m.unit_price || 0);
      }
      return { ...i, _owned: ownedInputs, _oop: oop };
    });
    if (ownedOnly) items = items.filter((i) => i._owned > 0);
    return items.sort((SORTS[sort] || SORTS.trust).fn);
  }, [data, minRoi, risk, ownedOnly, hideSuspicious, sort, owned]);

  const hasInv = Object.keys(owned).length > 0;
  const suspiciousCount = (data || []).filter((i) => roiOf(i) > SANE_ROI_CEILING && i.verified_roi_pct == null).length;

  return (
    <div>
      <PageHead title="Opportunities">
        Every profitable trade-up the bots have found. Sort and filter, then expand a row to see the
        inputs, output odds, and why it's ranked there. Very high ROIs usually mean a bad output price —
        expand the row and use "$ fix" or "ban" to correct it.
      </PageHead>

      <Card>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "flex-end" }}>
          <label className="field" style={{ maxWidth: 200 }}>Sort by
            <select value={sort} onChange={(e) => setSort(e.target.value)}>
              {Object.entries(SORTS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </select>
          </label>
          <label className="field" style={{ maxWidth: 120 }}>Min ROI %
            <input type="number" value={minRoi} onChange={(e) => setMinRoi(e.target.value)} placeholder="any" />
          </label>
          <label className="field" style={{ maxWidth: 180 }}>Risk level
            <select value={risk} onChange={(e) => setRisk(e.target.value)}>
              <option value="">Any</option>
              <option value="low">Low (conf ≥ 70)</option>
              <option value="medium">Medium (40–70)</option>
              <option value="high">High (&lt; 40)</option>
            </select>
          </label>
          <label className="switch" style={{ marginBottom: 8 }} title={`${suspiciousCount} finds have an ROI above ${SANE_ROI_CEILING}% and aren't verified — almost always a bad price`}>
            <input type="checkbox" checked={hideSuspicious} onChange={(e) => setHideSuspicious(e.target.checked)} />
            <span className="track" /> Hide suspicious ROI{suspiciousCount > 0 ? ` (${suspiciousCount})` : ""}
          </label>
          {hasInv && (
            <label className="switch" style={{ marginBottom: 8 }}>
              <input type="checkbox" checked={ownedOnly} onChange={(e) => setOwnedOnly(e.target.checked)} />
              <span className="track" /> Only trades I partly own
            </label>
          )}
          <div style={{ marginLeft: "auto", color: "var(--text-dim)", fontSize: "0.85rem" }}>{rows.length} shown</div>
        </div>
      </Card>

      <Card>
        {isLoading ? (
          <Empty>Loading…</Empty>
        ) : rows.length === 0 ? (
          <Empty>No opportunities match. The bots keep searching — check back, or loosen the filters.</Empty>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th></th><th>Cheapest input</th><th>Collection</th>
                  <th className="num">Cost{hasInv ? " / your cost" : ""}</th>
                  <th className="num">ROI</th><th className="num">Profit</th><th>Trust</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((t) => {
                  const cheapest = [...(t.input_mix || [])].sort((a, b) => (a.unit_price || 0) - (b.unit_price || 0))[0];
                  const roi = roiOf(t);
                  const profit = t.profit_amount ?? (t.cost * roi / 100);
                  const rk = riskFromConfidence(t.confidence_score);
                  const suspicious = roi > SANE_ROI_CEILING && t.verified_roi_pct == null;
                  const isOpen = expanded === t.id;
                  return [
                    <tr key={t.id} onClick={() => setExpanded(isOpen ? null : t.id)} style={{ cursor: "pointer" }}>
                      <td>{isOpen ? "▾" : "▸"}</td>
                      <td><div className="skin-cell"><SkinImage url={cheapest?.image} name={cheapest?.skin} />{cheapest?.skin || "—"}</div></td>
                      <td>{t.primary_collection}</td>
                      <td className="num">
                        {fmtMoney(t.cost)}
                        {hasInv && t._owned > 0 && <div style={{ fontSize: "0.75rem", color: "var(--win)" }}>you: {fmtMoney(t._oop)} ({t._owned}/10 owned)</div>}
                      </td>
                      <td className="num" style={{ color: roi > 0 ? "var(--win)" : "var(--loss)", fontWeight: 600 }}>
                        {fmtPct(roi, 0)}{suspicious && <span title="Above 200% and unverified — almost always a bad output price. Expand to fix." style={{ color: "var(--warn)", fontWeight: 700 }}> (!)</span>}
                      </td>
                      <td className="num">{fmtMoney(profit)}</td>
                      <td><Badge tone={suspicious ? "warn" : rk.tone}>{t.verified_roi_pct != null ? "verified" : suspicious ? "check data" : rk.label}</Badge></td>
                    </tr>,
                    isOpen && <OpportunityDetail key={t.id + "-d"} o={t} owned={owned} onDone={refetch} />,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
