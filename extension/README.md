# Steam Session Sync (helper extension)

One button that keeps the Rare Patterns scanner fast.

## Why this exists

Steam rate-limits **anonymous** Market listing requests hard — often refusing the very first
one. Sending your own Steam session cookie (`steamLoginSecure`) gets the same friendlier limit
any logged-in browser gets. Since the scanner decodes float/paint-seed locally now, that Market
request is its *only* network cost, so this is the single biggest lever on scan speed.

That cookie is **HttpOnly**: page scripts and bookmarklets cannot read it, by design. Only
devtools or an extension with the `cookies` permission can. This extension replaces the
devtools copy/paste step — that is its entire purpose.

## What it is not

- **It is not a Steam login.** It never sees your password and cannot log you in.
- It does not use Steam OpenID ("Sign in through Steam"), which only returns your SteamID and
  would not affect the rate limit at all.
- It reads exactly one cookie, and sends it to exactly one place: `127.0.0.1:5000`, your own
  dashboard. There is no remote server involved.

## Install (Chrome / Edge / Brave)

1. Start the dashboard (`python -m web.server`).
2. Go to `chrome://extensions` (Edge: `edge://extensions`).
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and select this `extension/` folder.
5. Pin the green gem icon to your toolbar.

Firefox: go to `about:debugging` → **This Firefox** → **Load Temporary Add-on** → pick
`manifest.json`. Firefox unloads temporary add-ons on restart.

## Use

1. Be logged into [steamcommunity.com](https://steamcommunity.com) in the same browser.
2. Click the extension icon.
3. A green **OK** badge means the dashboard is authenticated. Check
   **Connections → Steam session** — it will show the masked value.

Badges: **OK** synced · **ERR** dashboard unreachable (is it running?) · **!** you are not
logged into Steam in this browser.

After the first sync it re-syncs itself automatically whenever Steam rotates the cookie, so you
should not need to click it again.

## Security notes

- Only the last 4 characters of the cookie are ever shown in the dashboard, and the stored value
  is never returned to any page — not even to this extension.
- It is saved in `connections.local.json`, which is gitignored and deliberately kept outside
  `data/`, so the repo's 6-hourly GitHub backup can never push it anywhere.
- The receiving endpoint rejects posts whose `Origin` is a normal web page, and the server binds
  to `127.0.0.1` only.
- To revoke: **Clear** in the dashboard's Connections page, or log out of Steam (Steam's
  "Log out of all devices" invalidates the cookie immediately).
