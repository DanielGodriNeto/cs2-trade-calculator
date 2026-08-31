import { NAV, matchCommand, dispatchCommand } from "./command.js";
import { api } from "./api.js";
import { timeAgo } from "./fmt.js";
import { renderLegacy } from "./views/legacy.js";
import { renderOverview } from "./views/overview.js";
import { renderOpportunities } from "./views/opportunities.js";
import { renderCalculator } from "./views/calculator.js";
import { renderPrice } from "./views/price.js";

const root = document.getElementById("view-root");
const ctx = { collections: [], skins: [] };
window.__hud = { route: () => location.hash.slice(1).split("?")[0] || "overview", ctx, refresh: renderRoute };

// nav
document.getElementById("nav").innerHTML = NAV.map((n) => `<a href="${n.hash}" data-code="${n.code}">${n.code}</a>`).join("");

function renderRoute() {
  const view = window.__hud.route();
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${view}`));
  root.innerHTML = "";
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  if (view === "overview") return renderOverview(root);
  if (view === "opportunities") return renderOpportunities(root, params);
  if (view === "calculator") return renderCalculator(root);
  if (view === "price") return renderPrice(root, params);
  if (["patterns", "lab", "connections"].includes(view)) return renderLegacy(root, view);
  location.hash = "#overview";
}
addEventListener("hashchange", renderRoute);

// command bar
const cmd = document.getElementById("cmd"), menu = document.getElementById("cmd-menu");
let items = [], sel = 0, skinTimer = null;
function renderMenu() {
  menu.style.display = items.length ? "block" : "none";
  menu.textContent = "";
  const body = document.createElement("div");
  body.className = "panel-body";
  items.forEach((it, i) => {
    const row = document.createElement("div");
    row.className = "readout-row" + (i === sel ? " sel" : "");
    row.dataset.i = i;
    row.style.cssText = "cursor:pointer;padding:3px 4px";
    const label = document.createElement("span");
    label.textContent = it.label;
    const hint = document.createElement("span");
    hint.className = "readout-label";
    hint.textContent = it.hint;
    row.append(label, hint);
    row.onclick = () => { dispatchCommand(items[+row.dataset.i]); cmd.value = ""; renderMenuReset(); };
    body.appendChild(row);
  });
  menu.appendChild(body);
}
function renderMenuReset() { items = []; sel = 0; menu.style.display = "none"; }
cmd.addEventListener("input", () => {
  clearTimeout(skinTimer);
  items = matchCommand(cmd.value, ctx); sel = 0; renderMenu();
  const issuedFor = cmd.value, q = cmd.value.trim();
  if (q.length < 2) { ctx.skins = []; return; }
  skinTimer = setTimeout(async () => {
    try {
      const rows = await api.skins(q);
      ctx.skins = Array.isArray(rows) ? rows : [];
    } catch { return; }                       // leave ctx.skins as-is on error
    if (cmd.value === issuedFor) { items = matchCommand(cmd.value, ctx); sel = 0; renderMenu(); }
  }, 180);
});
cmd.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { sel = Math.min(sel + 1, items.length - 1); renderMenu(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); renderMenu(); e.preventDefault(); }
  else if (e.key === "Enter" && items[sel]) { dispatchCommand(items[sel]); cmd.value = ""; renderMenuReset(); }
  else if (e.key === "Escape") { cmd.value = ""; renderMenuReset(); cmd.blur(); }
});
addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== cmd
      && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "")
      && !document.activeElement?.isContentEditable) {
    cmd.focus(); e.preventDefault();
  }
});

// telemetry strip + clock, 3s poll
async function poll() {
  try {
    const [s, net] = await Promise.all([api.status(), api.networkStats()]);
    const scan = s.status || "idle";
    const health = (net.consecutive_rate_limits || 0) > 0 ? "warn" : (net.requests_error && !net.requests_ok) ? "err" : "ok";
    // createElement/textContent, same M1 rule as the view modules (values are server enums/numbers).
    const strip = document.getElementById("telemetry");
    const seg = (dotCls, text) => {
      const span = document.createElement("span");
      if (dotCls != null) { const d = document.createElement("span"); d.className = "dot " + dotCls; span.appendChild(d); }
      span.appendChild(document.createTextNode(text));
      return span;
    };
    strip.replaceChildren(
      seg(scan === "running" ? "ok" : scan === "error" ? "err" : "", "SCAN " + scan),
      seg(health, "NET " + (health === "ok" ? "healthy" : health === "warn" ? "rate-limited" : "errors")),
    );
    if (net.last_success_at) strip.appendChild(seg(null, "last px " + timeAgo(net.last_success_at)));
    if (net.cache_size != null) strip.appendChild(seg(null, Number(net.cache_size).toLocaleString() + " priced"));
    if (net.request_interval != null) strip.appendChild(seg(null, "spacing " + net.request_interval + "s"));
  } catch {}
}
(async function pollLoop() {
  await poll();
  setTimeout(pollLoop, 3000);
})();
setInterval(() => { document.getElementById("clock").textContent = new Date().toTimeString().slice(0, 8); }, 1000);

// jump-to targets: most-profitable collections (fire-and-forget — a failure must not break boot)
(async () => {
  try {
    const lb = await api.leaderboards();
    ctx.collections = (lb.collections || []).map((c) => c.collection).filter(Boolean);
    if (cmd.value.trim()) { items = matchCommand(cmd.value, ctx); renderMenu(); }
  } catch { /* no leaderboards — collection mode stays empty */ }
})();

renderRoute();
