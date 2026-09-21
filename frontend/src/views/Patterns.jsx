import { useState } from "react";
import { usePoll } from "../hooks/usePoll.js";
import { api } from "../api/client.js";
import { Card, PageHead, Badge, SkinImage, fmtMoney, fmtPct, Empty } from "../components/ui.jsx";

function confTone(c) {
  return c === "high" ? "win" : c === "medium" ? "warn" : "";
}

export default function Patterns() {
  const { data: status } = usePoll("patStatus", api.patternsStatus, 4000);
  const { data: opps, isLoading } = usePoll("patOpps", () => api.patternsOpps({ sort: "score" }), 5000);
  const { data: db } = usePoll("patDb", api.patternsDb, 0);
  const [showDb, setShowDb] = useState(false);

  const list = Array.isArray(opps) ? opps : opps?.items || [];

  return (
    <div>
      <PageHead title="Rare Patterns">
        Special skins — Blue Gems, Dopplers, rare fades, sticker crafts — that a seller listed at an
        ordinary price. Detection is knowledge-based (no guessing), decoded from each listing locally.
      </PageHead>

      <Card
        title="Scanner"
        caption={status?.running ? "Scanning Steam listings…" : "Idle. Saving your Steam session (Connections) makes this much faster."}
      >
        <div style={{ fontSize: "0.85rem", color: "var(--text-2)" }}>
          {status?.status_line || status?.message || "Ready."}
        </div>
      </Card>

      <Card title="Live opportunities" caption="Every matched listing, profitable or not — sort/filter to find the good ones.">
        {isLoading ? (
          <Empty>Loading…</Empty>
        ) : list.length === 0 ? (
          <Empty>No rare-pattern listings matched yet. If the scanner has never run, start it and save your Steam session for speed.</Empty>
        ) : (
          <div className="grid cols-3">
            {list.slice(0, 60).map((o, i) => (
              <div key={i} className="card" style={{ padding: 14 }}>
                <div style={{ display: "flex", gap: 10 }}>
                  <SkinImage url={o.image_url || o.image} name={o.market_hash_name} size={64} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: "0.88rem" }}>{o.market_hash_name}</div>
                    <div style={{ fontSize: "0.78rem", color: "var(--text-2)" }}>{o.pattern_label || o.pattern}</div>
                    <Badge tone={confTone(o.confidence)}>{o.confidence || "?"} confidence</Badge>
                  </div>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 12, fontSize: "0.85rem" }}>
                  <span>Ask <b>{fmtMoney(o.ask ?? o.price)}</b></span>
                  <span style={{ color: (o.est_profit ?? 0) >= 0 ? "var(--win)" : "var(--loss)" }}>Profit <b>{fmtMoney(o.est_profit)}</b></span>
                </div>
                {o.listing_url && <a className="btn secondary" style={{ marginTop: 10, width: "100%", justifyContent: "center" }} href={o.listing_url} target="_blank" rel="noreferrer">View listing</a>}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card
        title="Pattern knowledge base"
        actions={<button className="btn secondary" onClick={() => setShowDb((s) => !s)}>{showDb ? "Hide" : "Show"}</button>}
        caption="The versioned list of documented collector patterns the scanner matches against."
      >
        {showDb && (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th>Pattern</th><th>Weapon</th><th>Skin</th><th>Type</th><th className="num">Tier</th><th className="num">Premium ×</th><th>Confidence</th></tr></thead>
              <tbody>
                {(db?.entries || []).map((e) => (
                  <tr key={e.id}>
                    <td>{e.label}</td><td>{e.weapon}</td><td>{e.skin}</td><td>{e.type}</td>
                    <td className="num">{e.tier}</td><td className="num">{e.premium_multiplier}×</td>
                    <td><Badge tone={confTone(e.confidence)}>{e.confidence}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
