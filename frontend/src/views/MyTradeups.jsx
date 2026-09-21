import { useState } from "react";
import { usePoll } from "../hooks/usePoll.js";
import { api } from "../api/client.js";
import { Card, PageHead, Stat, Badge, fmtMoney, fmtPct, fmtNum, timeAgo, Empty } from "../components/ui.jsx";
import { TrendLine } from "../components/charts/Charts.jsx";

const WEARS = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"];
const FROM_RARITIES = ["Consumer Grade", "Industrial Grade", "Mil-Spec Grade", "Restricted", "Classified", "Covert"];
const TO_RARITIES = ["Industrial Grade", "Mil-Spec Grade", "Restricted", "Classified", "Covert", "Extraordinary"];

const emptyInput = () => ({ skin: "", wear: "Field-Tested", count: 1, unit_price_paid: "" });

// The full outcome-logging form — record what actually happened after you executed a trade-up
// manually (this tool never auto-buys/sells). Feeds the ML ROI-calibration model. Ported from the
// classic dashboard, with a structured input-mix builder instead of a raw text box.
function OutcomeForm({ onLogged }) {
  const { data: currencies } = usePoll("currencies", api.currencies, 0);
  const [open, setOpen] = useState(false);
  const [collection, setCollection] = useState("");
  const [kind, setKind] = useState("collection");
  const [stattrak, setStattrak] = useState(false);
  const [fromRarity, setFromRarity] = useState("Mil-Spec Grade");
  const [toRarity, setToRarity] = useState("Restricted");
  const [currency, setCurrency] = useState(1);
  const [inputs, setInputs] = useState([emptyInput()]);
  const [useFiller, setUseFiller] = useState(false);
  const [filler, setFiller] = useState(emptyInput());
  const [fillerCollection, setFillerCollection] = useState("");
  const [outputSkin, setOutputSkin] = useState("");
  const [outputFloat, setOutputFloat] = useState("");
  const [status, setStatus] = useState("sold");
  const [salePrice, setSalePrice] = useState("");
  const [predictedRoi, setPredictedRoi] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const totalCount = inputs.reduce((a, r) => a + (Number(r.count) || 0), 0) + (useFiller ? Number(filler.count) || 0 : 0);
  const setInput = (i, patch) => setInputs((rows) => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  async function submit(e) {
    e.preventDefault();
    setBusy(true); setResult(null);
    try {
      const input_mix = inputs
        .filter((r) => r.skin.trim())
        .map((r) => ({ skin: r.skin.trim(), wear: r.wear, count: Number(r.count) || 1, unit_price_paid: Number(r.unit_price_paid) || 0 }));
      const body = {
        collection: collection.trim(),
        kind, stattrak,
        from_rarity: fromRarity, to_rarity: toRarity,
        currency: Number(currency) || 1,
        input_mix,
        filler_collection: useFiller ? fillerCollection.trim() || null : null,
        filler_input: useFiller && filler.skin.trim()
          ? { skin: filler.skin.trim(), wear: filler.wear, count: Number(filler.count) || 1, unit_price_paid: Number(filler.unit_price_paid) || 0 }
          : null,
        output_skin: outputSkin.trim(),
        output_wear: outputFloat !== "" ? Number(outputFloat).toFixed(2) : "",
        status,
        sale_price: salePrice !== "" ? Number(salePrice) : null,
        predicted_roi_pct: predictedRoi !== "" ? Number(predictedRoi) : null,
        notes: notes.trim(),
      };
      const res = await api.logOutcome(body);
      if (res.ok === false) {
        setResult({ err: res.error });
      } else {
        const o = res.outcome;
        setResult({
          msg: o.status === "sold"
            ? `Logged — cost ${fmtMoney(o.cost_paid)}, profit ${o.profit >= 0 ? "+" : ""}${fmtMoney(o.profit)}, ROI ${fmtPct(o.roi_pct, 1)} (${o.win ? "WIN" : "LOSS"})`
            : `Logged — cost ${fmtMoney(o.cost_paid)}, status ${o.status} (no sale price yet)`,
          win: o.win,
        });
        // Reset the transient fields, keep collection/rarities for logging several in a row.
        setInputs([emptyInput()]); setOutputSkin(""); setOutputFloat(""); setSalePrice(""); setNotes("");
        onLogged?.();
      }
    } catch (err) {
      setResult({ err: String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title="Log a trade-up outcome"
      caption="Record what actually happened after you ran a contract manually. Every logged outcome trains the ML ROI-calibration model (see the Lab)."
      actions={<button className="btn secondary" onClick={() => setOpen((o) => !o)}>{open ? "Hide form" : "Log an outcome"}</button>}
    >
      {!open ? null : (
        <form onSubmit={submit}>
          <div className="grid cols-3" style={{ marginBottom: 12 }}>
            <label className="field">Collection / case<input type="text" value={collection} onChange={(e) => setCollection(e.target.value)} placeholder="e.g. The St. Marc Collection" required /></label>
            <label className="field">Kind<select value={kind} onChange={(e) => setKind(e.target.value)}><option value="collection">Collection</option><option value="case">Case</option></select></label>
            <label className="field">Currency<select value={currency} onChange={(e) => setCurrency(e.target.value)}>{(currencies || [{ code: 1, name: "USD", symbol: "$" }]).map((c) => <option key={c.code} value={c.code}>{c.name} ({c.symbol})</option>)}</select></label>
            <label className="field">From rarity<select value={fromRarity} onChange={(e) => setFromRarity(e.target.value)}>{FROM_RARITIES.map((r) => <option key={r}>{r}</option>)}</select></label>
            <label className="field">To rarity<select value={toRarity} onChange={(e) => setToRarity(e.target.value)}>{TO_RARITIES.map((r) => <option key={r}>{r}</option>)}</select></label>
            <label className="switch" style={{ alignSelf: "end", marginBottom: 8 }}><input type="checkbox" checked={stattrak} onChange={(e) => setStattrak(e.target.checked)} /><span className="track" /> StatTrak</label>
          </div>

          <h4>Inputs you bought <span style={{ fontWeight: 400, color: totalCount === 10 ? "var(--win)" : "var(--text-dim)" }}>({totalCount}/10)</span></h4>
          <div className="tbl-wrap"><table className="tbl">
            <thead><tr><th>Skin</th><th>Wear</th><th className="num">Count</th><th className="num">Unit price paid</th><th></th></tr></thead>
            <tbody>
              {inputs.map((r, i) => (
                <tr key={i}>
                  <td><input type="text" value={r.skin} onChange={(e) => setInput(i, { skin: e.target.value })} placeholder="AK-47 | Redline" /></td>
                  <td><select value={r.wear} onChange={(e) => setInput(i, { wear: e.target.value })} style={{ width: "auto" }}>{WEARS.map((w) => <option key={w}>{w}</option>)}</select></td>
                  <td className="num"><input type="number" min="1" max="10" value={r.count} onChange={(e) => setInput(i, { count: e.target.value })} style={{ width: 64 }} /></td>
                  <td className="num"><input type="number" step="0.01" value={r.unit_price_paid} onChange={(e) => setInput(i, { unit_price_paid: e.target.value })} style={{ width: 90 }} placeholder="0.42" /></td>
                  <td>{inputs.length > 1 && <button type="button" className="btn secondary" onClick={() => setInputs((rows) => rows.filter((_, j) => j !== i))}>Remove</button>}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
          <button type="button" className="btn secondary" style={{ marginTop: 8 }} onClick={() => setInputs((r) => [...r, emptyInput()])}>Add input line</button>

          <div style={{ marginTop: 12 }}>
            <label className="switch"><input type="checkbox" checked={useFiller} onChange={(e) => setUseFiller(e.target.checked)} /><span className="track" /> Used a filler collection</label>
          </div>
          {useFiller && (
            <div className="grid cols-3" style={{ marginTop: 8 }}>
              <label className="field">Filler skin<input type="text" value={filler.skin} onChange={(e) => setFiller({ ...filler, skin: e.target.value })} /></label>
              <label className="field">Filler wear<select value={filler.wear} onChange={(e) => setFiller({ ...filler, wear: e.target.value })}>{WEARS.map((w) => <option key={w}>{w}</option>)}</select></label>
              <label className="field">Filler collection<input type="text" value={fillerCollection} onChange={(e) => setFillerCollection(e.target.value)} /></label>
              <label className="field">Filler count<input type="number" min="1" max="10" value={filler.count} onChange={(e) => setFiller({ ...filler, count: e.target.value })} /></label>
              <label className="field">Filler unit price<input type="number" step="0.01" value={filler.unit_price_paid} onChange={(e) => setFiller({ ...filler, unit_price_paid: e.target.value })} /></label>
            </div>
          )}

          <h4 style={{ marginTop: 16 }}>Result</h4>
          <div className="grid cols-3">
            <label className="field">Output skin<input type="text" value={outputSkin} onChange={(e) => setOutputSkin(e.target.value)} placeholder="AK-47 | Point Disarray" required /></label>
            <label className="field">Output float<input type="number" step="0.0001" min="0" max="1" value={outputFloat} onChange={(e) => setOutputFloat(e.target.value)} placeholder="0.18" title="Exact float 0.00–1.00; mapped to its wear bracket" required /></label>
            <label className="field">Status<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="sold">Sold</option><option value="held">Held</option><option value="kept">Kept</option></select></label>
            <label className="field">Sale price{status === "sold" ? " (required)" : ""}<input type="number" step="0.01" value={salePrice} onChange={(e) => setSalePrice(e.target.value)} required={status === "sold"} /></label>
            <label className="field">Predicted ROI % (optional)<input type="number" step="0.1" value={predictedRoi} onChange={(e) => setPredictedRoi(e.target.value)} /></label>
            <label className="field">Notes<input type="text" value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
          </div>

          <div style={{ marginTop: 16, display: "flex", alignItems: "center", gap: 16 }}>
            <button className="btn" type="submit" disabled={busy}>{busy ? "Logging…" : "Log outcome"}</button>
            {result?.msg && <span style={{ color: result.win === false ? "var(--loss)" : "var(--win)" }}>{result.msg}</span>}
            {result?.err && <span style={{ color: "var(--loss)" }}>Error: {result.err}</span>}
          </div>
        </form>
      )}
    </Card>
  );
}

export default function MyTradeups() {
  const { data: outcomes, refetch } = usePoll("outcomes", api.outcomes, 0);
  const { data: stats, refetch: refetchStats } = usePoll("outcomeStats", api.outcomesStats, 0);
  const list = Array.isArray(outcomes) ? outcomes : [];
  const sold = list.filter((o) => o.status === "sold" && o.profit != null);

  const chron = [...sold].sort((a, b) => (a.logged_at || 0) - (b.logged_at || 0));
  let running = 0;
  const cumLabels = chron.map((_, i) => `#${i + 1}`);
  const cumValues = chron.map((o) => (running += o.profit || 0));
  const winRate = stats?.win_rate_pct;
  const onLogged = () => { refetch(); refetchStats(); };

  return (
    <div>
      <PageHead title="My Trade-ups">
        The trade-ups you've actually executed — real profit, win rate, and how well the predictions held
        up. Log a completed one below; every outcome also trains the ML calibration model.
      </PageHead>

      <OutcomeForm onLogged={onLogged} />

      <div className="grid cols-4" style={{ margin: "24px 0" }}>
        <Stat label="Logged" value={fmtNum(stats?.n_total ?? list.length)} sub={`${stats?.n_sold ?? sold.length} sold`} />
        <Stat label="Win rate" value={winRate == null ? "—" : fmtPct(winRate, 0)} tone={winRate >= 50 ? "pos" : winRate != null ? "neg" : undefined} sub={`${stats?.n_wins ?? 0}W / ${stats?.n_losses ?? 0}L`} />
        <Stat label="Realized profit" value={fmtMoney(cumValues.length ? cumValues[cumValues.length - 1] : 0)} tone={(cumValues[cumValues.length - 1] || 0) >= 0 ? "pos" : "neg"} sub="sum of sold outcomes" />
        <Stat label="Avg ROI (sold)" value={sold.length ? fmtPct(sold.reduce((a, o) => a + (o.roi_pct || 0), 0) / sold.length, 0) : "—"} />
      </div>

      {chron.length > 0 && (
        <Card title="Cumulative realized profit" caption="Your running net profit across sold trade-ups, in the order you logged them.">
          <TrendLine labels={cumLabels} series={[{ label: "cumulative profit", values: cumValues }]} caption="Above zero = you're ahead overall." />
        </Card>
      )}

      <Card title="History">
        {list.length === 0 ? (
          <Empty>No trade-ups logged yet. Use the form above after you execute a contract to start tracking your real results.</Empty>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr><th>Logged</th><th>Collection</th><th>From → To</th><th>Output</th><th>Status</th><th className="num">Cost</th><th className="num">Profit</th><th className="num">ROI</th><th>Win?</th></tr>
              </thead>
              <tbody>
                {[...list].sort((a, b) => (b.logged_at || 0) - (a.logged_at || 0)).map((o) => (
                  <tr key={o.id}>
                    <td>{timeAgo(o.logged_at)}</td>
                    <td>{o.collection}</td>
                    <td style={{ fontSize: "0.8rem" }}>{o.from_rarity} → {o.to_rarity}</td>
                    <td>{o.output_skin}</td>
                    <td><Badge>{o.status}</Badge></td>
                    <td className="num">{fmtMoney(o.cost_paid)}</td>
                    <td className="num" style={{ color: (o.profit ?? 0) >= 0 ? "var(--win)" : "var(--loss)" }}>{o.profit != null ? fmtMoney(o.profit) : "—"}</td>
                    <td className="num">{o.roi_pct != null ? fmtPct(o.roi_pct, 0) : "—"}</td>
                    <td>{o.win == null ? "—" : o.win ? <Badge tone="win">win</Badge> : <Badge tone="loss">loss</Badge>}</td>
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
