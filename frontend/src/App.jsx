import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Route, Routes, Navigate } from "react-router-dom";
import { usePoll } from "./hooks/usePoll.js";
import { api } from "./api/client.js";
import { Toggle, timeAgo } from "./components/ui.jsx";
import Home from "./views/Home.jsx";
import Opportunities from "./views/Opportunities.jsx";
import Calculator from "./views/Calculator.jsx";
import Inventory from "./views/Inventory.jsx";
import Patterns from "./views/Patterns.jsx";
import Lab from "./views/Lab.jsx";
import Market from "./views/Market.jsx";
import MyTradeups from "./views/MyTradeups.jsx";
import Connections from "./views/Connections.jsx";

// ---- Advanced-mode context: global switch that reveals the ML/simulation internals ----
const AdvancedCtx = createContext({ advanced: false, setAdvanced: () => {} });
export const useAdvanced = () => useContext(AdvancedCtx);

const NAV = [
  { to: "/", label: "Home", end: true },
  { to: "/opportunities", label: "Opportunities" },
  { to: "/calculator", label: "Calculator" },
  { to: "/inventory", label: "My Skins" },
  { to: "/tradeups", label: "My Trade-ups" },
  { to: "/patterns", label: "Rare Patterns" },
  { to: "/market", label: "Market Intel" },
  { to: "/lab", label: "Lab", advanced: true },
  { to: "/connections", label: "Connections" },
];

function StatusStrip() {
  const { data } = usePoll("status", api.status, 3000);
  const { data: net } = usePoll("net", api.networkStats, 5000);
  const scan = data?.status || "idle";
  const scanTone = scan === "running" ? "ok" : scan === "error" ? "err" : "";
  const rateLimited = (net?.consecutive_rate_limits || 0) > 0;
  const health = rateLimited ? "warn" : (net?.requests_error || 0) > 0 && !(net?.requests_ok) ? "err" : "ok";
  return (
    <div className="statusstrip">
      <span><span className={`dot ${scanTone}`} />Scan: <b>{scan}</b></span>
      <span><span className={`dot ${health}`} />Network: <b>{health === "ok" ? "healthy" : health === "warn" ? "rate-limited" : "errors"}</b></span>
      {net?.last_success_at && <span>Last price update: <b>{timeAgo(net.last_success_at)}</b></span>}
      {net?.cache_size !== undefined && <span><b>{Number(net.cache_size).toLocaleString()}</b> skins priced</span>}
    </div>
  );
}

export default function App() {
  const [advanced, setAdvanced] = useState(() => localStorage.getItem("advanced") === "1");
  useEffect(() => localStorage.setItem("advanced", advanced ? "1" : "0"), [advanced]);

  const visibleNav = NAV.filter((n) => !n.advanced || advanced);

  return (
    <AdvancedCtx.Provider value={{ advanced, setAdvanced }}>
      <div className="app">
        <header className="appbar">
          <div className="brand">
            <span className="logo">&gt;_</span>
            <span>CS2 Trade-Up</span>
          </div>
          <nav className="nav">
            {visibleNav.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? "active" : "")}>
                <span className="label">{n.label}</span>
              </NavLink>
            ))}
          </nav>
          <div className="appbar-spacer" />
          <div className="appbar-tools">
            <Toggle checked={advanced} onChange={setAdvanced}>Advanced</Toggle>
            <a className="nav-classic" href="/legacy" title="The classic dashboard — every original feature, including ones not yet moved into this redesign (price history, live scan mode, connections editing, full archive)">Classic</a>
          </div>
        </header>
        <StatusStrip />
        <main className="main">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/opportunities" element={<Opportunities />} />
            <Route path="/calculator" element={<Calculator />} />
            <Route path="/inventory" element={<Inventory />} />
            <Route path="/tradeups" element={<MyTradeups />} />
            <Route path="/patterns" element={<Patterns />} />
            <Route path="/market" element={<Market />} />
            <Route path="/lab" element={<Lab />} />
            <Route path="/connections" element={<Connections />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </AdvancedCtx.Provider>
  );
}
