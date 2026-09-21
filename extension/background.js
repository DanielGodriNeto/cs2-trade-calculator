/* CS2 Trade-Up Dashboard -- Steam Session Sync + Inventory Import
 *
 * On click it does two things, both to your own dashboard on localhost only:
 *   1. reads the `steamLoginSecure` cookie and POSTs it to /api/connections/steam-session
 *      (raises the Rare Patterns scan rate limit), and
 *   2. fetches YOUR OWN CS2 inventory from the logged-in Steam session and POSTs it to
 *      /api/inventory/import, so the "My Skins" page works even on a PRIVATE inventory.
 * Nothing else is read, and nothing is ever sent anywhere except 127.0.0.1.
 *
 * Why an extension is needed at all: steamLoginSecure is an HttpOnly cookie, so it is invisible
 * to page scripts and bookmarklets by design. Devtools or an extension with the `cookies`
 * permission are the only two ways to read it -- this replaces the devtools copy/paste step.
 * It is NOT a Steam login: it never sees your password, and it cannot log you in. The inventory
 * fetch uses the browser's own logged-in session (same as opening your inventory page), so it
 * sees your private inventory without any credential ever reaching the dashboard.
 */

const DASHBOARDS = ["http://127.0.0.1:5000", "http://localhost:5000"];
const COOKIE_URL = "https://steamcommunity.com";
const COOKIE_NAME = "steamLoginSecure";

async function readSteamCookie() {
  // Ask for the exact cookie by name; Steam sets it on both the bare domain and .steamcommunity.com.
  const cookie = await chrome.cookies.get({ url: COOKIE_URL, name: COOKIE_NAME });
  return cookie && cookie.value ? cookie.value : null;
}

// The steamLoginSecure value is "<steamid64>||<token>" (URL-encoded as %7C%7C). The leading
// 17-digit run is the account's SteamID64 -- enough to hit its own inventory endpoint.
function steamIdFromCookie(value) {
  const decoded = decodeURIComponent(value || "");
  const m = decoded.match(/^(\d{17})/);
  return m ? m[1] : null;
}

async function importInventory(cookieValue) {
  const steamid = steamIdFromCookie(cookieValue);
  if (!steamid) return { ok: false, error: "couldn't read SteamID from session" };
  let inv;
  try {
    // Runs with the browser's Steam cookies, so a private inventory loads fine.
    const res = await fetch(
      `https://steamcommunity.com/inventory/${steamid}/730/2?l=english&count=2000`,
      { credentials: "include" }
    );
    if (!res.ok) return { ok: false, error: `Steam inventory HTTP ${res.status}` };
    inv = await res.json();
  } catch (e) {
    return { ok: false, error: "couldn't fetch inventory from Steam" };
  }
  for (const base of DASHBOARDS) {
    try {
      const res = await fetch(`${base}/api/inventory/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ steamid, assets: inv.assets, descriptions: inv.descriptions }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) return { ok: true, n: data.n_distinct };
    } catch (e) {
      /* try next base */
    }
  }
  return { ok: false, error: "dashboard not reachable" };
}

async function postToDashboard(value) {
  let lastError = null;
  for (const base of DASHBOARDS) {
    try {
      const res = await fetch(`${base}/api/connections/steam-session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ steam_login_secure: value }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) return { ok: true, masked: data.masked_key };
      lastError = data.error || `HTTP ${res.status}`;
    } catch (e) {
      lastError = "dashboard not reachable";
    }
  }
  return { ok: false, error: lastError };
}

async function badge(text, color, title) {
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
  if (title) await chrome.action.setTitle({ title });
  if (text) setTimeout(() => chrome.action.setBadgeText({ text: "" }), 4000);
}

async function sync(silent) {
  const value = await readSteamCookie();
  if (!value) {
    if (!silent) {
      await badge("!", "#d9a83c",
        "Not logged into Steam in this browser -- open steamcommunity.com, log in, then click again.");
    }
    return { ok: false, error: "not logged into Steam in this browser" };
  }
  const result = await postToDashboard(value);
  // A manual (non-silent) click also imports the inventory, so one click sets up both the scan
  // rate limit AND the "My Skins" page. The background cookie-rotation re-sync stays silent and
  // cookie-only -- no need to re-pull the whole inventory every time Steam rotates the token.
  let invResult = null;
  if (!silent && result.ok) {
    invResult = await importInventory(value);
  }
  if (!silent || result.ok) {
    const okTitle = invResult && invResult.ok
      ? `Steam session synced and ${invResult.n} skins imported. Open "My Skins" on the dashboard.`
      : `Steam session synced (${result.masked || "saved"}).` +
        (invResult ? ` Inventory import failed: ${invResult.error}.` : "");
    await badge(result.ok ? "OK" : "ERR", result.ok ? "#39d97a" : "#e0524f",
      result.ok ? okTitle : `Sync failed: ${result.error}. Is the dashboard running on localhost:5000?`);
  }
  return result;
}

chrome.action.onClicked.addListener(() => sync(false));

// Steam rotates this cookie periodically and on re-login. Re-syncing quietly in the background
// keeps the dashboard authenticated without the user having to think about it again.
chrome.cookies.onChanged.addListener((info) => {
  if (info.cookie.name !== COOKIE_NAME) return;
  if (!/(^|\.)steamcommunity\.com$/.test(info.cookie.domain)) return;
  if (info.removed) return;
  sync(true);
});
