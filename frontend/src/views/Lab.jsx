import { useState } from "react";
import { usePoll } from "../hooks/usePoll.js";
import { api } from "../api/client.js";
import { Card, PageHead, Badge, fmtNum, fmtPct, fmtMoney, Empty } from "../components/ui.jsx";
import { Bars, RadarChart } from "../components/charts/Charts.jsx";

// The Simulation Lab — the heart of the project: six independent search agents (4 specialists +
// 2 meta), each with its own personality and a self-trained search-heuristic model, hunting
// profitable trade-up compositions. This ports the real controls from the classic dashboard.

const PARAM_AXES = [
  ["exploration_rate", "Explore"],
  ["risk_tolerance", "Risk"],
  ["collection_mixing_bias", "Mix"],
  ["wear_optimization_bias", "Wear"],
  ["liquidity_importance", "Liquidity"],
  ["roi_weight", "ROI"],
  ["mutation_strength", "Mutation"],
  ["memory_weight", "Memory"],
];

function busyAction(fn) {
  return async (setPending, key) => {
    setPending((p) => ({ ...p, [key]: true }));
    try { await fn(); } finally { setPending((p) => ({ ...p, [key]: false })); }
  };
}

function AgentControls({ s, pending, run }) {
  const id = s.son_id;
  const running = s.status === "running";
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {running ? (
        <button className="btn secondary" disabled={pending[`${id}-pause`]} onClick={() => run(api.sonAction(id, "pause"), `${id}-pause`)}>Pause</button>
      ) : (
        <button className="btn" disabled={pending[`${id}-start`]} onClick={() => run(api.sonAction(id, "resume"), `${id}-start`)}>Start</button>
      )}
      <button className="btn secondary" disabled={pending[`${id}-stop`]} onClick={() => run(api.sonAction(id, "stop"), `${id}-stop`)}>Stop</button>
      <button className="btn secondary" title="Soft retry: temporarily boosts exploration, keeps its data/model" disabled={pending[`${id}-nudge`]} onClick={() => run(api.sonAction(id, "nudge"), `${id}-nudge`)}>Nudge</button>
      <button className="btn danger" title="Wipes THIS agent's simulations and model, starts it over" disabled={pending[`${id}-reset`]}
        onClick={() => { if (confirm(`Fully reset ${s.agent_name}? This wipes its simulations and trained model — cannot be undone.`)) run(api.sonAction(id, "reset"), `${id}-reset`); }}>Reset</button>
    </div>
  );
}

export default function Lab() {
  const { data: sim } = usePoll("simStatus", api.simStatus, 3000);
  const { data: model } = usePoll("model", api.modelStatus, 8000);
  const { data: rr } = usePoll("roundRobin", api.roundRobinStatus, 3000);
  const { data: conv } = usePoll("convergence", api.simConvergence, 6000);
  const { data: retrain } = usePoll("retrain", api.retrainStatus, 6000);
  const { data: priceScan } = usePoll("priceScan", api.priceScanStatus, 4000);
  const { data: verifyQ } = usePoll("verifyQ", api.verifyQueueStatus, 5000);
  const { data: outcomeStats } = usePoll("outcomeStats", api.outcomesStats, 10000);
  const [pending, setPending] = useState({});
  const run = (promise, key) => busyAction(() => promise)(setPending, key);

  const agents = Array.isArray(sim) ? sim : [];
  const bankLabels = agents.map((a) => a.agent_name || `Agent ${a.son_id}`);
  const bankValues = agents.map((a) => Math.round((a.cumulative_expected_profit || 0) * 100) / 100);
  const radarSeries = agents.slice(0, 4).map((a) => ({
    label: a.agent_name,
    values: PARAM_AXES.map(([k]) => a.profile?.[k] ?? 0),
  }));
  const rrRunning = rr?.enabled;
  const retrainOn = retrain?.status === "watching";
  const verifyOn = verifyQ?.status === "watching";
  const priceScanRunning = priceScan?.status === "running" || priceScan?.status === "scanning";

  return (
    <div>
      <PageHead title="Simulation Lab">
        The engine room: six independent search agents hunt profitable trade-up compositions, each
        with its own personality and a self-trained model. Five of each agent's eight search
        parameters self-tune over time. This is theoretical search — nothing is ever bought.
      </PageHead>

      {conv?.last_nudged?.length > 0 && (
        <div className="card" style={{ borderColor: "var(--warn)", marginBottom: 16 }}>
          <b style={{ color: "var(--warn)" }}>Anti-convergence:</b> agents {conv.last_nudged[0].son_ids?.join(", ")} settled on the same
          strategy and were automatically nudged toward broader exploration.
        </div>
      )}

      <Card
        title="Population health"
        caption="Each agent searches independently — if one stalls the others keep finding. Run them individually or all together."
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            {rrRunning
              ? <button className="btn secondary" onClick={() => run(api.roundRobinStop(), "rr-stop")}>Stop run-all</button>
              : <button className="btn" title="Every agent runs one round in turn, round-robin" onClick={() => run(api.roundRobinStart(), "rr-start")}>Run all (round-robin)</button>}
            <button className="btn danger" title="Wipe EVERY agent's data and models — full restart"
              onClick={() => { if (confirm("Restart ALL training & data? Wipes every agent's simulations and models. Cannot be undone.")) run(api.simResetAll(), "reset-all"); }}>Restart all</button>
          </div>
        }
      >
        {agents.length === 0 ? <Empty>No agent data yet.</Empty> : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Agent</th><th>Status</th><th className="num">Round</th><th className="num">Simulated</th>
                  <th className="num">Trainable</th><th className="num">Collections</th><th className="num">Best ROI</th><th>Flag</th><th>Controls</th>
                </tr>
              </thead>
              <tbody>
                {agents.map((a) => (
                  <tr key={a.son_id}>
                    <td title={a.agent_purpose}>{a.agent_name}{a.is_meta_agent && <span style={{ color: "var(--text-dim)" }}> (meta)</span>}</td>
                    <td><Badge tone={a.status === "running" ? "win" : a.status === "error" ? "loss" : ""}>{a.status || "idle"}</Badge></td>
                    <td className="num">{fmtNum(a.round)}</td>
                    <td className="num">{fmtNum(a.n_simulated)}</td>
                    <td className="num" title={`${a.n_low_volume || 0} excluded as thin-liquidity`}>{fmtNum(a.n_trainable)}</td>
                    <td className="num" title="Distinct collections this agent has explored (of ~91)">{a.coverage_collections ?? "—"}/91</td>
                    <td className="num">{a.best_roi_ever != null ? fmtPct(a.best_roi_ever, 0) : "—"}</td>
                    <td>
                      {a.stuck && <Badge tone="warn">stuck {a.rounds_since_improvement}r</Badge>}
                      {a.nudge_rounds_remaining > 0 && <Badge tone="info">nudged {a.nudge_rounds_remaining}r</Badge>}
                      {!a.stuck && !a.nudge_rounds_remaining && "—"}
                    </td>
                    <td><AgentControls s={a} pending={pending} run={run} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid cols-2">
        <Card title="Live search personalities" caption="Each agent's eight search parameters right now — five self-tune over time, drifting as the agent learns. Distinct shapes = a healthily diverse population.">
          {radarSeries.length ? (
            <RadarChart axes={PARAM_AXES.map(([, l]) => l)} series={radarSeries} caption="Overlapping shapes can mean the agents have converged (the watcher nudges them apart)." />
          ) : <Empty>No data yet.</Empty>}
        </Card>
        <Card title="Hypothetical bankroll per agent" caption="If you'd bought every trade each agent ever proposed — cumulative expected profit. Nothing is ever really bought.">
          {agents.length ? <Bars labels={bankLabels} values={bankValues} label="expected profit" caption="Tallest bar = the agent whose picks would have made the most." /> : <Empty>No data yet.</Empty>}
        </Card>
      </div>

      <Card title="ML ROI-calibration model" caption="A SEPARATE model, trained only on trade-up outcomes YOU log — used as a tiebreaker between near-identical formula-ROI candidates, never a replacement. Distinct from each agent's own search-heuristic model above."
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn" disabled={pending["train"]} onClick={() => run(api.trainModel(), "train")}>Train now</button>
            <button className="btn secondary" disabled={pending["reset-model"]} onClick={() => { if (confirm("Delete the trained ROI model? Your logged outcomes are kept — you can retrain from them.")) run(api.resetModel(), "reset-model"); }}>Reset model</button>
            {retrainOn
              ? <button className="btn secondary" onClick={() => run(api.retrainStop(), "retrain-stop")}>Stop auto-retrain</button>
              : <button className="btn secondary" onClick={() => run(api.retrainStart(), "retrain-start")}>Auto-retrain</button>}
          </div>
        }
      >
        <div style={{ fontSize: "0.9rem", color: "var(--text-2)" }}>
          {model?.trained
            ? <>Trained on <b>{fmtNum(model.n_samples)}</b> logged outcomes · validation error (MAE) <b>{model.validation_mae?.toFixed?.(2) ?? "—"}</b> ROI points. {retrainOn ? "Auto-retrain is watching for new outcomes." : ""}</>
            : <>Not trained yet — log at least <b>{model?.min_samples_required ?? 20}</b> real trade-up outcomes (My Trade-ups → log in the classic dashboard) to enable ML calibration. {outcomeStats?.n_total != null && <>You have <b>{outcomeStats.n_total}</b> logged.</>}</>}
        </div>
      </Card>

      <div className="grid cols-2">
        <Card title="Bulk price scan" caption="Warms the Steam price cache across every tradeable skin so agents aren't guessing off a cold cache. Don't run it at the same time as a live scan."
          actions={
            <div style={{ display: "flex", gap: 8 }}>
              {priceScanRunning
                ? <button className="btn secondary" onClick={() => run(api.priceScanPause(), "ps-pause")}>Pause</button>
                : <button className="btn" onClick={() => run(api.priceScanStart(), "ps-start")}>Start</button>}
              <button className="btn secondary" onClick={() => run(api.priceScanStop(), "ps-stop")}>Stop</button>
            </div>
          }>
          <div style={{ fontSize: "0.88rem", color: "var(--text-2)" }}>
            Status: <b>{priceScan?.status || "idle"}</b>
            {priceScan?.progress && <> · {fmtNum(priceScan.progress.current)}/{fmtNum(priceScan.progress.total)}</>}
          </div>
        </Card>
        <Card title="Stage-2 verification queue" caption="Cross-checks a promising find's every input/output against Skinport/Buff163/CSFloat live, one at a time, to confirm the price isn't a Steam-only artifact."
          actions={
            <div style={{ display: "flex", gap: 8 }}>
              {verifyOn
                ? <button className="btn secondary" onClick={() => run(api.verifyQueueStop(), "vq-stop")}>Pause</button>
                : <button className="btn" onClick={() => run(api.verifyQueueStart(), "vq-start")}>Start</button>}
            </div>
          }>
          <div style={{ fontSize: "0.88rem", color: "var(--text-2)" }}>
            {verifyOn ? "Watching for finds to verify." : "Paused."} {verifyQ?.queue_depth != null && <>Queue: <b>{verifyQ.queue_depth}</b></>}
          </div>
        </Card>
      </div>
    </div>
  );
}
