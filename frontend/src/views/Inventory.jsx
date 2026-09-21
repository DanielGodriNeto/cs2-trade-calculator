import { useEffect, useState } from "react";
import { api } from "../api/client.js";
import { Card, PageHead, SkinImage, Badge, fmtMoney, Empty } from "../components/ui.jsx";
import { saveInventory, loadInventory, clearInventory } from "../lib/inventory.js";

export default function Inventory() {
  const [steamid, setSteamid] = useState("");
  const [inv, setInv] = useState(() => loadInventory());
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  // On mount, pick up an inventory the browser extension may have imported server-side (or a
  // prior fetch that outlived this browser's localStorage) so the page shows it without a
  // re-fetch. Only overrides local state when the server actually has one.
  useEffect(() => {
    let cancelled = false;
    api.inventoryCached().then((res) => {
      if (!cancelled && res && res.ok !== false && Array.isArray(res.items)) {
        const local = loadInventory();
        if (!local || (res.fetched_at || 0) > (local.savedAt || 0) / 1000) {
          saveInventory(res);
          setInv({ ...res, savedAt: Date.now() });
        }
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  async function load() {
    setBusy(true); setError(null);
    try {
      const res = await api.inventory(steamid.trim());
      if (res.ok === false) {
        setError(res.error || "Could not load inventory.");
      } else {
        saveInventory(res);
        setInv({ ...res, savedAt: Date.now() });
      }
    } catch (e) {
      setError("Could not reach the inventory service. " + String(e));
    } finally {
      setBusy(false);
    }
  }

  function forget() {
    clearInventory();
    setInv(null);
  }

  const items = inv?.items || [];
  const totalValue = items.reduce((a, it) => a + (it.price || 0) * (it.count || 1), 0);
  const patterns = items.filter((it) => it.pattern);

  return (
    <div>
      <PageHead title="My Skins">
        Load your CS2 inventory so Opportunities and the Calculator can tell you which inputs you already
        own. Two ways: a public inventory by SteamID (no login), or the browser extension for private ones.
      </PageHead>

      <Card title="Connect your inventory" caption="Public inventories load instantly. Nothing here logs into Steam.">
        <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
          <label className="field" style={{ flex: 1, minWidth: 260 }}>SteamID, vanity name, or profile URL
            <input type="text" value={steamid} placeholder="e.g. 76561198… or steamcommunity.com/id/yourname" onChange={(e) => setSteamid(e.target.value)} />
          </label>
          <button className="btn" disabled={busy || !steamid.trim()} onClick={load}>{busy ? "Loading…" : "Load my skins"}</button>
        </div>
        {error && (
          <p style={{ color: "var(--loss)", marginTop: 12 }}>
            {error}{" "}
            {/private|403/i.test(error) && <>Your inventory is private — set it to public in Steam privacy settings, or use the extension.</>}
          </p>
        )}
        <p className="section-caption" style={{ marginTop: 12 }}>
          Private inventory? Install the bundled browser extension (see Connections) and click its icon while
          logged into Steam — it imports your inventory directly.
        </p>
      </Card>

      {patterns.length > 0 && (
        <Card title={`Rare patterns in your inventory (${patterns.length})`} caption="Blue gems, Doppler phases and other documented collector patterns, decoded from your items offline. Screening estimates — verify seed/float before selling.">
          <div className="grid cols-3">
            {patterns.map((it, i) => (
              <div key={i} className="stat" style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <SkinImage url={it.image} name={it.name} size={56} />
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: "0.82rem", fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{it.name}</div>
                  <div><Badge tone={it.pattern_tier === 1 ? "win" : "info"}>{it.pattern}</Badge></div>
                  <div style={{ fontSize: "0.78rem", color: "var(--text-dim)" }}>
                    market {fmtMoney(it.price)} · est. collector {fmtMoney(it.est_collector_value)}
                  </div>
                  {it.pattern_source && <a style={{ fontSize: "0.72rem" }} href={it.pattern_source} target="_blank" rel="noreferrer">verify pattern →</a>}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {inv && (
        <Card
          title="Your skins"
          caption={`${items.length} items · estimated value ${fmtMoney(totalValue)}`}
          actions={<button className="btn secondary" onClick={forget}>Forget</button>}
        >
          {items.length === 0 ? (
            <Empty>No CS2 items found in this inventory.</Empty>
          ) : (
            <div className="grid cols-4">
              {items.slice(0, 200).map((it, i) => (
                <div key={i} className="stat" style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <SkinImage url={it.image} name={it.name} size={56} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: "0.85rem", fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{it.name}</div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-dim)" }}>
                      {it.wear || "—"}{it.float != null && ` · ${it.float.toFixed(4)}`}
                    </div>
                    <div style={{ fontSize: "0.8rem" }}>{fmtMoney(it.price)}{it.count > 1 && ` ×${it.count}`}</div>
                    {it.pattern && <Badge tone="info">{it.pattern}</Badge>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
