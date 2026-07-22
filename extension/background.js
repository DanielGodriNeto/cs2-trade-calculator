/* CS2 Trade-Up Dashboard -- Steam Session Sync
 *
 * Does exactly one thing: reads the `steamLoginSecure` cookie from steamcommunity.com and POSTs
 * it to your own dashboard running on localhost. Nothing else is read, and nothing is ever sent
 * anywhere except 127.0.0.1.
 *
 * Why an extension is needed at all: steamLoginSecure is an HttpOnly cookie, so it is invisible
 * to page scripts and bookmarklets by design. Devtools or an extension with the `cookies`
 * permission are the only two ways to read it -- this replaces the devtools copy/paste step.
 * It is NOT a Steam login: it never sees your password, and it cannot log you in.
 */

const DASHBOARDS = ["http://127.0.0.1:5000", "http://localhost:5000"];
const COOKIE_URL = "https://steamcommunity.com";
const COOKIE_NAME = "steamLoginSecure";

async function readSteamCookie() {
  // Ask for the exact cookie by name; Steam sets it on both the bare domain and .steamcommunity.com.
  const cookie = await chrome.cookies.get({ url: COOKIE_URL, name: COOKIE_NAME });
  return cookie && cookie.value ? cookie.value : null;
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
  if (!silent || result.ok) {
    await badge(result.ok ? "OK" : "ERR", result.ok ? "#39d97a" : "#e0524f",
      result.ok
        ? `Steam session synced (${result.masked || "saved"}). Click again after re-logging into Steam.`
        : `Sync failed: ${result.error}. Is the dashboard running on localhost:5000?`);
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
