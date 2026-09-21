import { usePoll } from "../hooks/usePoll.js";
import { api } from "../api/client.js";
import { Card, PageHead, Badge } from "../components/ui.jsx";

function StatusChip({ ok, label }) {
  return <Badge tone={ok ? "win" : ""}>{label}: {ok ? "connected" : "not set"}</Badge>;
}

export default function Connections() {
  const { data } = usePoll("connections", api.connections, 6000);
  const c = data || {};

  return (
    <div>
      <PageHead title="Connections">
        Optional integrations. Everything is stored locally on your machine — nothing is uploaded.
      </PageHead>

      <Card title="At a glance">
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <StatusChip ok={c.csfloat?.configured} label="CSFloat key" />
          <StatusChip ok={c.steam_session?.configured} label="Steam session" />
          <StatusChip ok={c.backup?.enabled} label="GitHub backup" />
          <StatusChip ok={c.ntfy?.enabled} label="Phone alerts" />
        </div>
      </Card>

      <Card title="Steam session (speeds up Rare Patterns)" caption="The single biggest lever on scan speed — Steam throttles anonymous requests hard.">
        <p style={{ fontSize: "0.88rem", color: "var(--text-2)" }}>
          Install the bundled browser extension in <code>extension/</code>, log into Steam, and click its
          icon. It syncs your session cookie (and, once set up, imports your inventory for the My Skins
          page). Your password is never involved. Full setup notes live in <code>extension/README.md</code>.
        </p>
        <div style={{ marginTop: 8 }}>
          <StatusChip ok={c.steam_session?.configured} label="Steam session" />
        </div>
      </Card>

      <Card title="Phone alerts (ntfy)" caption="Free push notifications when a high-ROI trade-up or rare listing shows up.">
        <p style={{ fontSize: "0.88rem", color: "var(--text-2)" }}>
          Pick a private topic name in the ntfy app and enter it here. Checked every 5 minutes against
          data already scanned — no extra Steam requests.
        </p>
        <div style={{ marginTop: 8 }}><StatusChip ok={c.ntfy?.enabled} label="Alerts" /></div>
      </Card>

      <p className="section-caption">
        Full editing controls for these (API keys, backup repo path, ntfy topic) — plus outcome logging,
        the market-intelligence registries, sim-population controls and CSV/XLSX exports — are being
        migrated into this redesigned UI. Until then everything remains available in the{" "}
        <a href="/legacy">classic dashboard</a>. Saved state is shown above; settings live in{" "}
        <code>connections.local.json</code>.
      </p>
    </div>
  );
}
