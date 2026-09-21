import { Link } from "react-router-dom";
import { usePoll } from "../hooks/usePoll.js";
import { api } from "../api/client.js";
import { Card, Stat, PageHead, fmtPct, fmtMoney, fmtNum, SkinImage, Badge } from "../components/ui.jsx";
import { Histogram } from "../components/charts/Charts.jsx";

function useHomeData() {
  const best = usePoll("best", api.bestSimulations, 4000);
  const lb = usePoll("leaderboards", api.leaderboards, 8000);
  const sim = usePoll("simStatus", api.simStatus, 4000);
  return { best, lb, sim };
}

function roiHistogram(items) {
  // Bucket ROI% into readable ranges instead of dumping a raw axis.
  const buckets = [
    { label: "<0%", lo: -Infinity, hi: 0 },
    { label: "0–25%", lo: 0, hi: 25 },
    { label: "25–50%", lo: 25, hi: 50 },
    { label: "50–100%", lo: 50, hi: 100 },
    { label: "100–500%", lo: 100, hi: 500 },
    { label: "500%+", lo: 500, hi: Infinity },
  ];
  const counts = buckets.map(
    (b) => items.filter((i) => (i.verified_roi_pct ?? i.expected_roi_pct) >= b.lo && (i.verified_roi_pct ?? i.expected_roi_pct) < b.hi).length
  );
  return { labels: buckets.map((b) => b.label), values: counts };
}

export default function Home() {
  const { best, lb, sim } = useHomeData();
  const items = best.data || [];
  const profitable = items.filter((i) => (i.verified_roi_pct ?? i.expected_roi_pct) > 0);
  const bestRoi = items.length ? Math.max(...items.map((i) => i.verified_roi_pct ?? i.expected_roi_pct)) : null;
  const avgRoi = profitable.length ? profitable.reduce((a, i) => a + (i.verified_roi_pct ?? i.expected_roi_pct), 0) / profitable.length : null;
  const totalSimulated = (sim.data || []).reduce((a, s) => a + (s.n_simulated || 0), 0);
  const hist = roiHistogram(items);
  const top = [...items].sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0)).slice(0, 5);

  return (
    <div>
      <PageHead title="Welcome back">
        Your bots have been hunting profitable CS2 trade-ups. Here's the short version — dig into any
        card to see the details.
      </PageHead>

      <div className="grid cols-4" style={{ marginBottom: 24 }}>
        <Stat label="Opportunities found" value={fmtNum(profitable.length)} sub={`${items.length} analysed`} help="Trade-ups the bots currently rate as profitable." />
        <Stat label="Best ROI right now" value={bestRoi === null ? "—" : fmtPct(bestRoi, 0)} tone={bestRoi > 0 ? "pos" : undefined} help="Highest expected return among current finds. Very high numbers usually rest on thin/unverified prices — check Trust." />
        <Stat label="Avg ROI (profitable)" value={avgRoi === null ? "—" : fmtPct(avgRoi, 0)} tone={avgRoi > 0 ? "pos" : undefined} />
        <Stat label="Trades simulated" value={fmtNum(totalSimulated)} sub="all-time by the bots" help="How much the search bots have explored." />
      </div>

      <div className="grid cols-2">
        <Card title="Start here" caption="Three steps to your first profitable trade-up.">
          <ol style={{ margin: 0, paddingLeft: 18, lineHeight: 2 }}>
            <li><Link to="/opportunities">Browse Opportunities</Link> — ranked, best first.</li>
            <li><Link to="/inventory">Load your skins</Link> — see which inputs you already own.</li>
            <li><Link to="/calculator">Build it in the Calculator</Link> — confirm cost, odds and profit.</li>
          </ol>
        </Card>

        <Card title="Where the profit is" caption="Higher bars = more of your finds fall in that return range.">
          {items.length ? (
            <Histogram labels={hist.labels} values={hist.values} label="finds" caption="Most edge sits in modest ROI bands — the giant ones are usually price artifacts." />
          ) : (
            <div className="empty">No finds yet. The bots build these up as they run.</div>
          )}
        </Card>
      </div>

      <Card title="Top picks" caption="Ranked by a blended score (ROI, profit, liquidity, confidence), not raw ROI alone." className="">
        {top.length ? (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr><th>Cheapest input</th><th>Collection</th><th className="num">Cost</th><th className="num">ROI</th><th>Trust</th></tr>
              </thead>
              <tbody>
                {top.map((t) => {
                  const cheapest = [...(t.input_mix || [])].sort((a, b) => (a.unit_price || 0) - (b.unit_price || 0))[0];
                  const roi = t.verified_roi_pct ?? t.expected_roi_pct;
                  const trustTone = (t.confidence_score || 0) >= 70 ? "win" : (t.confidence_score || 0) >= 40 ? "warn" : "loss";
                  return (
                    <tr key={t.id}>
                      <td><div className="skin-cell"><SkinImage url={cheapest?.image} name={cheapest?.skin} />{cheapest?.skin || "—"}</div></td>
                      <td>{t.primary_collection}</td>
                      <td className="num">{fmtMoney(t.cost)}</td>
                      <td className="num" style={{ color: roi > 0 ? "var(--win)" : "var(--loss)" }}>{fmtPct(roi, 0)}</td>
                      <td><Badge tone={trustTone}>{t.verified_roi_pct != null ? "verified" : `${Math.round(t.confidence_score || 0)}/100`}</Badge></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty">No picks yet.</div>
        )}
        <div style={{ marginTop: 16 }}><Link className="btn secondary" to="/opportunities">See all opportunities →</Link></div>
      </Card>
    </div>
  );
}
