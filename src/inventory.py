"""Load a user's own CS2 (app 730) inventory so the dashboard can be inventory-aware
("you already own 6/10 of this trade-up's inputs") and value the whole portfolio.

Two entry points, both keyless:
  - fetch_public(steamid_or_url): the PUBLIC Steam Community inventory JSON endpoint
    (https://steamcommunity.com/inventory/<id>/730/2). No login, no API key -- it just needs
    the inventory's privacy set to Public. A private inventory returns HTTP 403, surfaced as a
    clear "make it public or use the extension" error rather than a raw failure.
  - import_raw(payload): accepts an inventory JSON blob the bundled browser extension already
    fetched from the logged-in session (POST /api/inventory/import), so PRIVATE inventories work
    too without this server ever handling the user's Steam credentials.

Both funnel into _build_items(), which:
  - joins each asset to its description (market_hash_name, icon, tradable, inspect link),
  - decodes float / paint seed OFFLINE from the inspect link (src.inspect_decode -- the same
    zero-network trick the Rare Patterns scanner uses), no game-coordinator call,
  - prices each item from the local Steam price cache, falling back to the csgotrader bulk
    mirror (src.bulk_prices) -- so a whole inventory is valued without a single live Steam
    request, which Steam 429s anonymously anyway (see steam_prices.py).

Read-only: this module only ever GETs the public inventory endpoint (or receives a blob from
the extension). It never logs in, trades, or writes to the Steam account.
"""
import json
import re
import time
import urllib.parse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import net
from . import bulk_prices as bulk_prices_mod
from . import steam_prices as steam_prices_mod
from . import inspect_decode as inspect_decode_mod
from .oddsmath import WEAR_BREAKPOINTS

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_STEAMID64_RE = re.compile(r"^\d{17}$")
_PROFILES_RE = re.compile(r"steamcommunity\.com/profiles/(\d{17})")
_VANITY_URL_RE = re.compile(r"steamcommunity\.com/id/([^/?#]+)")
# Wear tokens as they appear inside a market_hash_name, e.g. "AK-47 | Redline (Field-Tested)".
_WEAR_IN_NAME_RE = re.compile(r"\(([^()]+)\)\s*$")
_WEAR_NAMES = {name for name, _lo, _hi in WEAR_BREAKPOINTS}


class InventoryError(Exception):
    """User-actionable failure (bad id, private inventory, unreachable). The message is safe to
    show verbatim in the dashboard."""


def resolve_steamid(raw: str) -> str:
    """Turn any of: a 17-digit SteamID64, a /profiles/<id> or /id/<vanity> URL, or a bare vanity
    name, into a SteamID64. Vanity names are resolved through the KEYLESS profile XML endpoint
    (steamcommunity.com/id/<vanity>?xml=1 exposes <steamID64>), so no Steam Web API key is
    needed. Raises InventoryError with a clear message on anything unresolvable."""
    raw = (raw or "").strip()
    if not raw:
        raise InventoryError("Enter a SteamID, profile URL, or custom URL name.")
    if _STEAMID64_RE.match(raw):
        return raw
    m = _PROFILES_RE.search(raw)
    if m:
        return m.group(1)
    m = _VANITY_URL_RE.search(raw)
    vanity = m.group(1) if m else raw  # bare word -> treat as a custom-URL (vanity) name
    return _resolve_vanity(vanity)


def _resolve_vanity(vanity: str) -> str:
    url = f"https://steamcommunity.com/id/{urllib.parse.quote(vanity)}/?xml=1"
    try:
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=20, context=net.SSL_CONTEXT) as resp:
            body = resp.read().decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError) as e:
        raise InventoryError(f"Couldn't reach Steam to resolve '{vanity}' ({e}).")
    m = re.search(r"<steamID64>(\d{17})</steamID64>", body)
    if not m:
        raise InventoryError(
            f"'{vanity}' didn't resolve to a Steam profile. Use your 17-digit SteamID64, or the "
            f"full profile URL.")
    return m.group(1)


def _wear_from_name(market_hash_name: str) -> str | None:
    m = _WEAR_IN_NAME_RE.search(market_hash_name or "")
    if m and m.group(1) in _WEAR_NAMES:
        return m.group(1)
    return None


def _price_for(market_hash_name: str, currency: int) -> float | None:
    """Value one item with zero live requests: the warm price cache first, then the csgotrader
    bulk mirror. Returns the item's lowest/median price (they're the same in the mirror), or
    None when neither source knows it."""
    data, _fresh = steam_prices_mod.get_cached_price(market_hash_name, currency)
    if data is None:
        data = bulk_prices_mod.get_steam_entry(market_hash_name, currency)
    if not data:
        return None
    return data.get("lowest_price") or data.get("median_price")


def _inspect_link(desc: dict, steamid: str, assetid: str) -> str | None:
    """The item's inspect link with %owner_steamid%/%assetid% placeholders filled in, so the
    offline decoder can pull float/seed out of it. Steam puts it under actions[] with a name
    like 'Inspect in Game...'."""
    for action in desc.get("actions") or []:
        link = action.get("link") or ""
        if "action_preview" in link or "inspect" in (action.get("name") or "").lower():
            return (link.replace("%owner_steamid%", str(steamid))
                        .replace("%assetid%", str(assetid)))
    return None


def _match_inventory_pattern(item: dict, name: str) -> dict | None:
    """Flag a rare collector pattern on one OWN inventory item, reusing the same knowledge base
    (pattern_db) and matcher (pattern_scanner._match_entry) the marketplace scanner uses -- so
    "does my own Case Hardened have a blue-gem seed / is my Doppler a Ruby" is answered from the
    exact same source of truth, offline, from the float/seed/index already decoded from the
    item's inspect link. Only paint_seed / paint_index patterns are checked (sticker crafts need
    sticker NAMES, which the offline decode doesn't carry -- it only has sticker ids). Returns a
    small annotation dict (label, tier, premium, est. collector value) or None."""
    if item.get("paint_seed") is None and item.get("paint_index") is None:
        return None
    from . import pattern_db as pattern_db_mod
    from . import pattern_scanner as pattern_scanner_mod
    match_item = {
        "paint_seed": item.get("paint_seed"),
        "paint_index": item.get("paint_index"),
        "float_value": item.get("float"),
        "stickers": [],
    }
    best = None
    for entry in pattern_db_mod.entries():
        if entry.get("type") == "sticker_craft":
            continue  # needs sticker names, not available from the offline decode
        weapon = entry.get("weapon") or "*"
        skin = entry.get("skin") or "*"
        if weapon != "*" and weapon not in name:
            continue
        if skin != "*" and skin not in name:
            continue
        if not pattern_scanner_mod._match_entry(entry, match_item):
            continue
        # Prefer the highest-premium match if several fire (e.g. a seed in two tiers).
        if best is None or entry.get("premium_multiplier", 0) > best.get("premium_multiplier", 0):
            best = entry
    if best is None:
        return None
    price = item.get("price")
    mult = best.get("premium_multiplier") or 1.0
    return {
        "pattern": best.get("label") or best.get("id"),
        "pattern_tier": best.get("tier"),
        "pattern_confidence": best.get("confidence"),
        "pattern_source": pattern_db_mod.source_url(best.get("source")),
        "est_collector_value": round(price * mult, 2) if price is not None else None,
    }


def _build_items(inv: dict, steamid: str, currency: int) -> list[dict]:
    """Join assets to descriptions, decode floats offline, price each, and aggregate identical
    market_hash_names into a single row with a count. Returns rows shaped for the frontend."""
    descriptions = {}
    for d in inv.get("descriptions") or []:
        descriptions[(str(d.get("classid")), str(d.get("instanceid")))] = d

    rows: dict[str, dict] = {}
    for asset in inv.get("assets") or []:
        key = (str(asset.get("classid")), str(asset.get("instanceid")))
        desc = descriptions.get(key)
        if not desc:
            continue
        name = desc.get("market_hash_name")
        if not name:
            continue
        wear = _wear_from_name(name)
        # Offline float/seed decode -- best-effort; classic links (no embedded data) just leave
        # float/seed None and the UI shows the wear bracket from the name instead.
        float_val = seed = paint_index = None
        link = _inspect_link(desc, steamid, asset.get("assetid"))
        if link:
            decoded = inspect_decode_mod.decode_inspect_link(link)
            if decoded:
                float_val = decoded.get("float_value")
                seed = decoded.get("paint_seed") or None
                paint_index = decoded.get("paint_index")

        existing = rows.get(name)
        if existing is None:
            price = _price_for(name, currency)
            icon = desc.get("icon_url") or ""
            row = {
                "name": name,
                "market_hash_name": name,
                "wear": wear,
                "image": (f"https://community.fastly.steamstatic.com/economy/image/{icon}/96x96"
                          if icon else ""),
                "price": price,
                "count": int(asset.get("amount", 1) or 1),
                "float": float_val,
                "paint_seed": seed,
                "paint_index": paint_index,
                "tradable": bool(desc.get("tradable")),
                "marketable": bool(desc.get("marketable")),
            }
            # Rare-pattern flag on this OWN item (blue gems, Doppler phases, ...) -- same
            # knowledge base as the marketplace scanner, decoded offline. Screening only.
            pat = _match_inventory_pattern(row, name)
            if pat:
                row.update(pat)
            rows[name] = row
        else:
            existing["count"] += int(asset.get("amount", 1) or 1)
            # Prefer keeping a concrete decoded float if a later copy has one and the first didn't
            if existing.get("float") is None and float_val is not None:
                existing["float"] = float_val
                existing["paint_seed"] = seed
    items = list(rows.values())
    items.sort(key=lambda r: (r["price"] or 0) * r["count"], reverse=True)
    return items


def _summarize(items: list[dict], steamid: str) -> dict:
    total = sum((it["price"] or 0) * it["count"] for it in items)
    n_items = sum(it["count"] for it in items)
    return {
        "ok": True,
        "steamid": steamid,
        "items": items,
        "n_items": n_items,
        "n_distinct": len(items),
        "total_value": round(total, 2),
        "priced": sum(1 for it in items if it["price"] is not None),
        "fetched_at": time.time(),
    }


def fetch_public(steamid_or_url: str, currency: int = 1, steam_cookie: str | None = None) -> dict:
    """Fetch and value a PUBLIC CS2 inventory. `steam_cookie` (steamLoginSecure) is optional and
    only needed for a private inventory the caller is allowed to see (e.g. the user's own, when
    the cookie is configured under Connections) -- omitted, this is a fully anonymous request."""
    steamid = resolve_steamid(steamid_or_url)
    url = (f"https://steamcommunity.com/inventory/{steamid}/730/2"
           f"?l=english&count=2000")
    headers = {"User-Agent": _UA, "Referer": f"https://steamcommunity.com/profiles/{steamid}/inventory/"}
    if steam_cookie:
        headers["Cookie"] = f"steamLoginSecure={steam_cookie}"
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=30, context=net.SSL_CONTEXT) as resp:
            inv = json.loads(resp.read())
    except HTTPError as e:
        if e.code == 403:
            raise InventoryError(
                "This inventory is private. Set it to Public in Steam privacy settings, or use "
                "the browser extension to import it directly.")
        if e.code == 429:
            raise InventoryError("Steam is rate-limiting inventory requests right now. Try again in a minute.")
        raise InventoryError(f"Steam returned HTTP {e.code} for that inventory.")
    except (URLError, TimeoutError) as e:
        raise InventoryError(f"Couldn't reach Steam to load the inventory ({e}).")
    except json.JSONDecodeError:
        raise InventoryError("Steam returned an unexpected response (not the inventory JSON).")
    if inv is None or not inv.get("descriptions"):
        # Steam returns {"success":1,"total_inventory_count":0} for an empty/hidden CS2 inventory.
        raise InventoryError("No CS2 items found (the inventory may be empty, private, or hidden).")
    return _summarize(_build_items(inv, steamid, currency), steamid)


def import_raw(payload: dict, currency: int = 1) -> dict:
    """Value an inventory JSON blob the browser extension fetched from the logged-in session.
    The blob is the same {assets, descriptions} shape as the public endpoint. `steamid` may be
    supplied alongside for inspect-link owner substitution; it's cosmetic for pricing."""
    inv = payload.get("inventory") if isinstance(payload.get("inventory"), dict) else payload
    steamid = str(payload.get("steamid") or inv.get("steamid") or "")
    if not inv.get("descriptions"):
        raise InventoryError("The imported inventory data had no items in it.")
    return _summarize(_build_items(inv, steamid, currency), steamid)
