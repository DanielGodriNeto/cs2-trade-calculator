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
let items = [], sel = 0;
function renderMenu() {
  menu.style.display = items.length ? "block" : "none";
  menu.innerHTML = `<div class="panel-body">${items.map((it, i) =>
    `<div class="readout-row ${i === sel ? "flash" : ""}" data-i="${i}" style="cursor:pointer;padding:3px 4px">
       <span>${it.label}</span><span class="readout-label">${it.hint}</span></div>`).join("")}</div>`;
  menu.querySelectorAll("[data-i]").forEach((el) =>
    el.onclick = () => { dispatchCommand(items[+el.dataset.i]); cmd.value = ""; renderMenuReset(); });
}
function renderMenuReset() { items = []; sel = 0; menu.style.display = "none"; }
cmd.addEventListener("input", () => { items = matchCommand(cmd.value, ctx); sel = 0; renderMenu(); });
cmd.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { sel = Math.min(sel + 1, items.length - 1); renderMenu(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); renderMenu(); e.preventDefault(); }
  else if (e.key === "Enter" && items[sel]) { dispatchCommand(items[sel]); cmd.value = ""; renderMenuReset(); }
  else if (e.key === "Escape") { cmd.value = ""; renderMenuReset(); cmd.blur(); }
});
addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== cmd) { cmd.focus(); e.preventDefault(); }
});

// telemetry strip + clock, 3s poll
async function poll() {
  try {
    const [s, net] = await Promise.all([api.status(), api.networkStats()]);
    const scan = s.status || "idle";
    const health = (net.consecutive_rate_limits || 0) > 0 ? "warn" : (net.requests_error && !net.requests_ok) ? "err" : "ok";
    document.getElementById("telemetry").innerHTML =
      `<span><span class="dot ${scan === "running" ? "ok" : scan === "error" ? "err" : ""}"></span>SCAN ${scan}</span>` +
      `<span><span class="dot ${health}"></span>NET ${health === "ok" ? "healthy" : health === "warn" ? "rate-limited" : "errors"}</span>` +
      (net.last_success_at ? `<span>last px ${timeAgo(net.last_success_at)}</span>` : "") +
      (net.cache_size != null ? `<span>${Number(net.cache_size).toLocaleString()} priced</span>` : "") +
      (net.request_interval != null ? `<span>spacing ${net.request_interval}s</span>` : "");
  } catch {}
}
setInterval(poll, 3000); poll();
setInterval(() => { document.getElementById("clock").textContent = new Date().toTimeString().slice(0, 8); }, 1000);

renderRoute();
