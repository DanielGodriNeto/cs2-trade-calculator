"""Rare Pattern Arbitrage Engine: continuously scans STEAM MARKET listings for items whose
seller priced a documented collector pattern (pattern_db.py) at ordinary market price, and
turns matches into ranked, auditable Rare Pattern Opportunities.

Operates INDEPENDENTLY from the trade-up optimizer (its own background thread, own data file,
own dashboard page) while reusing the shared infrastructure the spec calls for: consensus
pricing + liquidity + confidence come from market_reliability.market_metrics (the same
cached-feed cross-marketplace math the reliability registry uses), marketplace deep links from
marketplaces.py, and the same start/pause/stop thread conventions as every other loop.

Source coverage -- honest, up front (same disclosure style as thirdparty_prices.py):
  - STEAM MARKET is the ONLY listing source (2026-07-12: a prior CSFloat listings-API source
    was removed on user request -- Steam price/listing checks stay Steam-first project-wide,
    third-party sources are for cross-verification only, never primary discovery). Steam's
    listings render endpoint (`/market/listings/730/<name>/render/`) returns each live
    listing's price and an in-game inspect-link template. This is also the only way to
    actually buy a matched pattern directly on the Steam Market.
  - WHY STEAM AND NOT A SEED-INDEXED MARKETPLACE (2026-07-22, user's call, and it is the right
    one): CSFloat/Skinport expose paint_seed as a server-side indexed filter, so "find every
    Blue Gem" there is one request instead of thousands. But that index is exactly why the EDGE
    is gone there -- a seller on a seed-indexed marketplace can see their own pattern, and
    prices it accordingly. The mispricing this engine hunts only exists where the seller does
    not know what they have, and that is Steam: no seed display, no pattern filter, casual
    sellers dumping inventory at "ordinary" prices. Scanning fast against the wrong market
    finds nothing worth buying.
  - Float/paint seed/paint index come from THE RENDER PAYLOAD ITSELF -- zero extra network
    calls, microseconds per listing, no API key. Verified live 2026-07-22 against 85 AK-47 Case
    Hardened listings: each asset carries `asset_properties` with propid 1 = paint seed,
    propid 2 = float, propid 6 = a self-encoding inspect payload, and the inspect link is a
    template (`+csgo_econ_action_preview %propid:6%`) filled from those same properties.
    Decoding propid 6 (inspect_decode.py) reproduced Steam's own propid 1 and 2 EXACTLY on
    85/85 assets, so the two sources independently corroborate each other; the decode is what
    also yields paint_index, which Steam does not publish as a property and which the
    Doppler/Gamma Doppler entries match on. This is the same trick the fast browser extensions
    use (CS2 Float Scanner: "instant float display -- no delays or queues", "no external
    requests"). It replaces the old per-listing call to CSFloat's hosted INSPECT endpoint,
    which is GONE: api.csfloat.com no longer resolves (NXDOMAIN) and csfloat/inspect was
    archived 2026-03-25. Legacy M/A/D links carry no embedded data; those fall back to Steam's
    own properties, then to an inspect service only if INSPECT_API_URL is set, and are counted
    honestly in the status panel rather than silently dropped.
  - CORRECTED 2026-07-22, and this module was wrong about it for a long time: the endless
    "HTTP 429" from the render endpoint was NOT primarily a rate limit. That URL is an XHR the
    market page fires, and Steam rejects calls that don't carry the headers a browser sends with
    it -- answering 429 rather than 403, which is exactly why it read as throttling. Same URL,
    same cookie, same second: without the headers 429, with them 200 and a full page of
    listings (see _fetch_steam_listings). Sending them is mandatory, not politeness.
    A real rate limit does exist on top of that, so REQUEST_INTERVAL_SECONDS pacing and the
    429 cooldown below both stay. STEAM_LOGIN_SECURE_COOKIE (settable from the dashboard's
    Connections page, or as an env var) raises that limit the way any logged-in browser gets.
    Since seed resolution is now free, this render call is the ONLY per-target network cost.
  - The Skinport/Buff163 bulk mirrors remain price-only cross-checks for the VALUATION side
    (the base price a pattern's premium is measured against) -- they were never a listing
    source and still aren't; that's a documented limitation, not a silent gap.

Valuation base price (since 2026-07) is STEAM-FIRST: the skin's own Steam median-sale price
from the bulk mirror when it exists, falling back to the cross-market consensus only when
Steam has no sale evidence (in which case the opportunity's confidence is demoted). The
mirror's skinport/buff163 dumps conflate souvenir-only items under plain names and can carry
listing prices far above anything Steam ever pays out (the "AWP | Gungnir" class of bug) --
anchoring collector value to a price the pattern's own market doesn't support would
manufacture fake edge.

Detection is knowledge-driven, never ML-driven (spec): a listing only becomes an opportunity
when it matches a pattern_db entry (documented public collector knowledge). The ranking score
is a transparent formula over expected profit / tier / confidence / float desirability /
liquidity -- no model anywhere in the match path.

Valuation: estimated_collector_value = consensus market price for that exact skin+wear
(market_reliability.market_metrics -- Steam mirror + Skinport + Buff163 median) x the entry's
conservative premium multiplier. Every listing that matches a documented pattern becomes an
opportunity row -- profitable or not (2026-07-11, user request: show the whole picture, not
just the ones that already clear a hardcoded edge bar; let the user sort/filter instead). Both
the ask and the estimate ride on the row so the user can audit the math and judge profit for
themselves, alongside the pattern's documenting source link. Per the spec's cross-verification
rule, opportunities whose BASE price can't be cross-verified (fewer than 2 sources agreeing)
have their confidence demoted and are marked accordingly rather than silently trusted.
"""
import json
import os
import random
import re
import threading
import time
import urllib.parse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import inspect_decode
from . import market_reliability as market_reliability_mod
from . import net
from . import oddsmath
from . import pattern_db as pattern_db_mod
from . import user_overrides as user_overrides_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OPPORTUNITIES_FILE = DATA_DIR / "pattern_opportunities.json"

CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY")  # optional, shared with thirdparty_prices --
# only used here for the optional fallback INSPECT service (see _resolve_via_api); the
# listings-API source that used to require this key was removed, see module docstring.
STEAM_LOGIN_SECURE_COOKIE = os.environ.get("STEAM_LOGIN_SECURE_COOKIE")  # optional; see
# module docstring's honest caveat about anonymous Steam rate limits


def configure_csfloat_key(key: str | None):
    """Live setter for CSFLOAT_API_KEY, mirroring thirdparty_prices.configure_csfloat_key() --
    this module keeps its own separate module-level constant, so the dashboard's Connections
    settings page has to call both setters, not just one. Only affects the optional INSPECT
    rate budget now (see module docstring) -- Steam listing scanning works without it."""
    global CSFLOAT_API_KEY
    CSFLOAT_API_KEY = key or None


def configure_steam_cookie(value: str | None):
    """Live setter for STEAM_LOGIN_SECURE_COOKIE, so the dashboard's Connections page can set it
    without a process restart (_fetch_steam_listings reads this at call time).

    SECURITY, stated plainly because this value is not an API key: steamLoginSecure IS a Steam
    session token. Anything holding it can act as that Steam account on Steam's web endpoints.
    It is therefore persisted ONLY to connections.local.json -- gitignored, and deliberately
    outside data/ so backup_engine's 6-hourly archive sweep can never push it anywhere (see
    connections_config.py's docstring) -- never logged, and never echoed back to the browser in
    full (connections_config.masked_key)."""
    global STEAM_LOGIN_SECURE_COOKIE
    value = (value or "").strip() or None
    # Users paste either the raw value or the whole "steamLoginSecure=<value>" cookie pair from
    # devtools; accept both rather than silently sending a malformed cookie header.
    if value and value.lower().startswith("steamloginsecure="):
        value = value.split("=", 1)[1].strip()
    STEAM_LOGIN_SECURE_COOKIE = value

# Steam Market per-listing scan (see module docstring, "STEAM MARKET"): the listings render
# endpoint returns each live listing's price AND its inspect-link template; the CSFloat
# INSPECT API (public, key optional) resolves an inspect link to float/seed/index/stickers.
#
# Two 2026-07 realities, both verified live:
#   - Steam's NEW "Market Beta" frontend redirects /render/ to an HTML page. The classic
#     endpoint still exists behind the beta opt-out cookie (bMarketOptOut=1 -- the exact
#     cookie the beta UI's own "Exit Market Beta" button sets), so every request sends it.
#   - Steam 429s anonymous JSON endpoints in minutes-long windows (same policy that killed
#     priceoverview, see steam_prices.py). One 429 here therefore puts the whole Steam
#     source on a dedicated, exponentially-growing cooldown (STEAM_BLOCK_BASE_SECONDS,
#     capped at STEAM_BLOCK_MAX_SECONDS) instead of burning the scan cycle's short per-target
#     backoff into a wall that hasn't lifted. CSFloat scanning continues unaffected
#     throughout; the status panel shows the cooldown plainly. Values mirror steam_prices.py's
#     already-tuned RATE_LIMIT_BACKOFF_SECONDS/MAX_RATE_LIMIT_BACKOFF_SECONDS (60s/300s, not
#     5-20x that) and honor the server's own Retry-After header when Steam sends one, since a
#     blind fixed-minutes wait either undershoots a real block or (far more often) way
#     overshoots a block that already lifted -- see steam_prices.py's _mark_rate_limited.
STEAM_RENDER_URL = "https://steamcommunity.com/market/listings/730/{}/render/"
STEAM_LISTING_PAGE_URL = "https://steamcommunity.com/market/listings/730/{}"  # Referer for the above
STEAM_OPTOUT_COOKIE = "bMarketOptOut=1"
STEAM_LISTINGS_PER_QUERY = 100  # listings fetched per Steam render call (cheapest-first) --
# 100 is the Market render endpoint's own page-size ceiling, i.e. every live listing Steam
# will hand back in one call, not an arbitrary choice

# Fallback inspect service for LEGACY M/A/D links only (see inspect_decode.py). Ships DISABLED:
# the old default (api.csfloat.com) is a dead hostname, and pointing at a dead host just burns
# the cycle on DNS failures. Set INSPECT_API_URL to a working resolver to re-enable; the
# scanner works without it whenever Steam serves self-encoding links, which is the normal case.
INSPECT_API_URL = os.environ.get("INSPECT_API_URL") or None
MAX_API_INSPECTS_PER_CYCLE = 5  # hard cap on fallback calls per target per cycle, so a target
# whose links are all legacy can never blow up a cycle the way the old design did
INSPECT_INTERVAL_SECONDS = 1.5  # pacing between FALLBACK inspect calls only (~1/second is the
# long-standing informal budget for such endpoints). The offline decode path never sleeps.

STEAM_CHEAPEST_LISTINGS = 30    # how many of the cheapest listings to match per target, EVERY
# cycle. 2026-07-22: this replaces the old per-target cursor that walked 10 listings deeper
# each cycle and wrapped around. That walk was backwards for this engine's whole thesis -- an
# uninformed seller prices a rare pattern at the ORDINARY price, so the opportunity is always
# at the cheap end of the book, and a gem listed at ordinary price gets sniped in minutes. The
# old design spent 9 of every 10 cycles resolving listings too expensive to ever be a deal and
# only returned to the cheap end every ~2.8 hours. Now that decoding is free (no API call, no
# sleep) there is no reason to ration: re-check the cheap end on every single cycle.

STEAM_BLOCK_BASE_SECONDS = 60   # first 429 -> skip Steam scanning this long; doubles per
STEAM_BLOCK_MAX_SECONDS = 300   # consecutive block, capped here; any success resets it.
# Matches steam_prices.py's RATE_LIMIT_BACKOFF_SECONDS/MAX_RATE_LIMIT_BACKOFF_SECONDS -- that
# module hit the same Steam 429 wall earlier and 60s/300s is what real-world tuning landed on;
# this scanner's old 300s/3600s was 5-20x more conservative than necessary for no known reason.
STEAM_BLOCK_JITTER_SECONDS = 5  # randomized so repeated failures don't all cool down in lockstep

INSPECT_CACHE_FILE = DATA_DIR / "inspect_cache.json"
_INSPECT_CACHE_MAX = 200000     # a resolved item is IMMUTABLE (float/seed never change), so the
# cache is write-once per listing and persists to disk across restarts -- see _load_inspect_cache.
# The old in-memory-only cache was wiped on every restart and cleared WHOLESALE on overflow, so
# the scanner kept re-paying for items it had already seen. Entries are tiny (~120 bytes), so a
# ceiling this high is a few tens of MB at worst and realistically never reached.

REQUEST_INTERVAL_SECONDS = 4.0   # baseline pacing between targets in the scan loop -- polite
# even though this is now Steam-only (a full target cycle takes minutes regardless, and
# patterns don't appear/vanish second-to-second)
OPPORTUNITY_TTL_SECONDS = 24 * 3600  # unrefreshed opportunities age out (delisted/sold)
CONFIDENCE_FACTOR = {"high": 1.0, "medium": 0.7, "low": 0.4}

WEARS = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
WEAR_MAX_FLOAT = {"Factory New": 0.07, "Minimal Wear": 0.15, "Field-Tested": 0.38,
                   "Well-Worn": 0.45, "Battle-Scarred": 1.0}

# Doppler/Gamma Doppler carriers for the wildcard paint_index entries -- kept to the liquid
# knife families so a full scan cycle stays short; extend via pattern_db entries if needed.
DOPPLER_WEAPONS = ["★ Karambit", "★ M9 Bayonet", "★ Bayonet", "★ Butterfly Knife",
                    "★ Flip Knife", "★ Talon Knife"]
GAMMA_DOPPLER_WEAPONS = ["★ Karambit", "★ M9 Bayonet", "★ Bayonet", "★ Flip Knife",
                          "★ Gut Knife", "Glock-18"]

_stats_lock = threading.Lock()
_stats = {"matches": 0, "opportunities_live": 0, "last_attempt_at": None,
          "last_cycle_completed_at": None,
          # Steam Market listings (the only source, see module docstring):
          "steam_queries": 0, "steam_listings_seen": 0, "steam_last_success_at": None,
          "steam_last_error": None, "steam_consecutive_failures": 0,
          "steam_blocked_until": None,  # epoch until which the Steam source sits out (429 cooldown)
          # Seed/float resolution (see module docstring -- offline decode is the primary path):
          "inspects_resolved": 0, "inspects_failed": 0, "inspect_last_error": None,
          "decoded_offline": 0,      # resolved locally from the link, zero network cost
          "resolved_from_properties": 0,  # fell back to Steam's own published seed/float props
          "resolved_via_api": 0,     # legacy M/A/D links sent to a configured fallback service
          "legacy_links_skipped": 0, # legacy links seen with no fallback configured
          "cache_hits": 0,           # already known from a previous cycle/run (disk-backed)
          "skipped_price_ceiling": 0}  # too expensive to ever be a deal -- never even decoded

_inspect_cache: dict = {}  # inspect link -> resolved item dict (see _resolve_item)
_inspect_cache_lock = threading.Lock()
_inspect_cache_loaded = False
_inspect_cache_dirty = False

_store_lock = threading.Lock()


class PatternScanState:
    """Thread-control wrapper, same pause/stop/wake shape as retrain_loop.RetrainLoopState.
    Starts PAUSED: the scanner talks to an external API, so scanning is opt-in via the
    dashboard's Start button (same opt-in convention as live Steam fetching)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "paused"  # paused | scanning | stopped
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.wake_event = threading.Event()
        self.current_target = None

    def snapshot(self) -> dict:
        with self.lock:
            status = self.status
            current = self.current_target
        with _stats_lock:
            stats = dict(_stats)
        stats["opportunities_live"] = len(_load_store()["opportunities"])
        return {"status": status, "current_target": current, **stats,
                "steam_login_cookie_configured": STEAM_LOGIN_SECURE_COOKIE is not None,
                "steam_login_cookie_note": (
                    "Scanning Steam anonymously -- Steam rate-limits this listings endpoint "
                    "aggressively, often refusing the very first request, so scans can spend "
                    "real time sitting out cooldowns rather than finding listings (see "
                    "steam_blocked_until above). Save your own Steam session cookie under "
                    "Connections > Steam session (or set the STEAM_LOGIN_SECURE_COOKIE env "
                    "var) for the much friendlier authenticated rate limit. This is now the "
                    "scanner's ONLY network cost -- float and paint seed are decoded locally "
                    "-- so it is the single biggest lever on scan speed. Optional: the scanner "
                    "works without it, just far slower and burstier."
                    if not STEAM_LOGIN_SECURE_COOKIE else None),
                "csfloat_key_configured": CSFLOAT_API_KEY is not None,
                "csfloat_key_note": (
                    "Not needed for scanning any more -- float/paint seed are decoded from the "
                    "inspect link locally, with no API call at all (see inspect_decode.py). "
                    "Only used if you configure INSPECT_API_URL as a fallback for legacy "
                    "inspect links."),
                "inspect_api_configured": INSPECT_API_URL is not None,
                "inspect_api_note": (None if INSPECT_API_URL else
                    "No external inspect service configured -- and none is needed: modern CS2 "
                    "inspect links self-encode float/paint seed/paint index, so the scanner "
                    "decodes them locally at zero cost. Set INSPECT_API_URL only if you see "
                    "'legacy_links_skipped' climbing, which means Steam is serving old-style "
                    "M/A/D links that carry no embedded data."),
                "inspect_cache_size": len(_inspect_cache),
                "request_interval_seconds": REQUEST_INTERVAL_SECONDS,
                "n_targets": len(build_targets()),
                "scan_sources": {
                    "steam": "the only listing source -- Steam Market listings scanned "
                             "directly; each listing's float/paint seed is decoded from its "
                             "inspect link locally (no API call, no rate limit). The "
                             f"{STEAM_CHEAPEST_LISTINGS} cheapest listings per target are "
                             "re-checked EVERY cycle, because an uninformed seller prices a "
                             "rare pattern at the ordinary price, so opportunities only ever "
                             "appear at the cheap end -- and get sniped within minutes",
                    "skinport": "valuation cross-check only -- bulk mirror carries prices, not listings",
                    "buff163": "valuation cross-check only -- bulk mirror carries prices, not listings",
                }}


# --- Targets ---------------------------------------------------------------------------------

def _market_name(weapon: str, skin: str, wear: str) -> str:
    return f"{weapon} | {skin} ({wear})"


def build_targets() -> list[dict]:
    """One target = one Steam Market listings query = (market_hash_name, [entries to match]).
    Derived fresh from the live pattern DB so a knowledge refresh/entry edit changes the scan
    plan on the next cycle without a restart. Wildcard-weapon paint_index entries (Doppler
    phases) expand across the carrier lists above; sticker_craft entries have no queryable
    name of their own and are matched opportunistically against every fetched listing."""
    targets: dict[str, dict] = {}

    def add(name: str, entry: dict):
        t = targets.setdefault(name, {"market_hash_name": name, "entries": []})
        t["entries"].append(entry)

    for entry in pattern_db_mod.entries("paint_seed"):
        max_f = entry.get("max_float")
        for wear in WEARS:
            if max_f is not None and WEAR_MAX_FLOAT[wear] is not None:
                # skip wears entirely above the entry's float requirement (e.g. FFI = FN only)
                lower_bound = {"Factory New": 0.0, "Minimal Wear": 0.07, "Field-Tested": 0.15,
                               "Well-Worn": 0.38, "Battle-Scarred": 0.45}[wear]
                if lower_bound >= max_f:
                    continue
            add(_market_name(entry["weapon"], entry["skin"], wear), entry)

    for entry in pattern_db_mod.entries("paint_index"):
        weapons = ([entry["weapon"]] if entry.get("weapon") and entry["weapon"] != "*"
                   else (GAMMA_DOPPLER_WEAPONS if entry.get("skin") == "Gamma Doppler"
                         else DOPPLER_WEAPONS))
        for weapon in weapons:
            for wear in ("Factory New", "Minimal Wear"):  # Doppler-family knives are FN/MW
                add(_market_name(weapon, entry["skin"], wear), entry)

    return list(targets.values())


# --- Steam Market fetch (listings render endpoint + CSFloat inspect resolution) ------------------

def _record_steam_failure(reason: str, rate_limited: bool = False,
                           retry_after: float | None = None) -> None:
    """Records a Steam fetch failure. When rate_limited, honors the server's own Retry-After
    header when Steam sent one (it knows its own cooldown better than we do); otherwise backs
    off exponentially from STEAM_BLOCK_BASE_SECONDS (capped at STEAM_BLOCK_MAX_SECONDS) with
    jitter, same shape as steam_prices.py's _mark_rate_limited."""
    with _stats_lock:
        _stats["steam_last_error"] = reason
        _stats["steam_consecutive_failures"] += 1
        if rate_limited:
            if retry_after is not None:
                block = retry_after
            else:
                streak = _stats["steam_consecutive_failures"]
                block = min(STEAM_BLOCK_BASE_SECONDS * (2 ** (streak - 1)), STEAM_BLOCK_MAX_SECONDS)
                block += random.uniform(0, STEAM_BLOCK_JITTER_SECONDS)
            _stats["steam_blocked_until"] = time.time() + block


def _steam_blocked() -> bool:
    with _stats_lock:
        until = _stats["steam_blocked_until"]
    return until is not None and time.time() < until


def _fetch_steam_listings(market_hash_name: str) -> list | None:
    """One Steam Market listings render call for one item. Returns a list of
    {listing_id, ask, inspect_link} for the cheapest live listings (None = fetch failed --
    recorded; [] = genuinely no listings). The render endpoint is the same JSON the classic
    market page loads (the bMarketOptOut cookie keeps Steam from redirecting it to the new
    beta frontend's HTML -- see the constants block above); each listing's asset carries an
    inspect-link TEMPLATE with %listingid%/%assetid% placeholders this fills in. Steam
    rate-limits anonymous callers hard (see steam_prices.py's history with priceoverview) --
    a 429/HTML/null body puts the Steam source on its own minutes-long cooldown and shows up
    in the status panel. An optional STEAM_LOGIN_SECURE_COOKIE (see module docstring) rides
    along with the opt-out cookie and gets Steam's much friendlier authenticated rate limit
    instead of the anonymous one -- this is the main lever for a Steam-only scanner to see
    real throughput rather than mostly sitting in cooldown."""
    params = urllib.parse.urlencode({"query": "", "start": 0, "count": STEAM_LISTINGS_PER_QUERY,
                                      "country": "US", "language": "english", "currency": 1})
    url = STEAM_RENDER_URL.format(urllib.parse.quote(market_hash_name)) + f"?{params}"
    cookie = STEAM_OPTOUT_COOKIE
    if STEAM_LOGIN_SECURE_COOKIE:
        cookie += f"; steamLoginSecure={STEAM_LOGIN_SECURE_COOKIE}"
    # 2026-07-22, verified live: the persistent "HTTP 429" from this endpoint was NOT a rate
    # limit. The classic /render/ URL is an XHR the market page fires, and Steam rejects calls
    # that don't carry the headers a real browser sends with it -- same URL, same cookie, same
    # second: without these headers 429, with them 200 and a full listing page. Sending them is
    # what makes the endpoint usable at all, authenticated or not, so they are not optional
    # politeness. Referer must point at the item's own listing page (Sec-Fetch-Site:
    # same-origin is only truthful if it does).
    req = Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Cookie": cookie,
        "Referer": STEAM_LISTING_PAGE_URL.format(urllib.parse.quote(market_hash_name)),
        "X-Requested-With": "XMLHttpRequest",
        "X-Prototype-Version": "1.7",
        "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    with _stats_lock:
        _stats["steam_queries"] += 1
        _stats["last_attempt_at"] = time.time()
    try:
        with urlopen(req, timeout=30, context=net.SSL_CONTEXT) as resp:
            raw = resp.read()
        if raw[:1] == b"<":
            # An HTML answer means Steam redirected to the new beta frontend's page instead
            # of serving the classic JSON -- treat as a (soft) block, not data.
            _record_steam_failure("HTML response (redirected to Market Beta page)", rate_limited=True)
            return None
        body = json.loads(raw)
    except HTTPError as e:
        retry_after = None
        header_val = e.headers.get("Retry-After") if e.headers else None
        if header_val is not None:
            try:
                retry_after = float(header_val)
            except ValueError:
                retry_after = None  # some servers send an HTTP-date instead of seconds; ignore those
        _record_steam_failure(f"HTTP {e.code}: {e.reason}", rate_limited=e.code in (429, 403),
                               retry_after=retry_after)
        return None
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        _record_steam_failure(f"{type(e).__name__}: {e}")
        return None
    if not isinstance(body, dict) or not body.get("success"):
        # A soft-blocked Steam answers with a literal JSON null or {"success": false}.
        _record_steam_failure("unexpected response (rate-limited or blocked)", rate_limited=True)
        return None
    listinginfo = body.get("listinginfo") or {}
    assets = ((body.get("assets") or {}).get("730") or {})
    out = []
    for lid, info in listinginfo.items():
        if not isinstance(info, dict):
            continue
        price_cents = (info.get("converted_price") or 0) + (info.get("converted_fee") or 0)
        if not price_cents:
            continue  # sold/expired listings linger in the payload with no price
        asset_ref = info.get("asset") or {}
        asset = (assets.get(str(asset_ref.get("contextid", 2))) or {}).get(str(asset_ref.get("id", "")))
        if not isinstance(asset, dict):
            continue
        actions = asset.get("market_actions") or asset.get("actions") or []
        link = next((a.get("link") for a in actions
                     if isinstance(a, dict) and "csgo_econ_action_preview" in (a.get("link") or "")), None)
        if not link:
            continue
        inspect = _fill_inspect_template(link, lid, asset_ref.get("id", ""), asset)
        props = _asset_properties(asset)
        out.append({"listing_id": str(lid), "ask": round(price_cents / 100.0, 2),
                    "inspect_link": inspect, "icon_url": asset.get("icon_url"),
                    "sticker_names": _sticker_names_from_asset(asset),
                    # Steam's own published values, used to cross-check the decode (see
                    # _resolve_item). PROP_PAINT_SEED/PROP_FLOAT are authoritative when present.
                    "prop_paint_seed": _as_int(props.get(PROP_PAINT_SEED)),
                    "prop_float": _as_float(props.get(PROP_FLOAT))})
    out.sort(key=lambda l: l["ask"])
    with _stats_lock:
        _stats["steam_last_success_at"] = time.time()
        _stats["steam_last_error"] = None
        _stats["steam_consecutive_failures"] = 0
        _stats["steam_blocked_until"] = None
        _stats["steam_listings_seen"] += len(out)
    return out


# Steam's asset_properties (2026-07 market payload). VERIFIED live against 85 AK-47 Case Hardened
# listings: propid 1 == paint seed, propid 2 == float, propid 6 == the masked inspect payload.
# Decoding propid 6 reproduced propid 1 and 2 exactly on 85/85 assets, so the two sources agree
# and either can validate the other. paint_index is NOT published as a property -- it only comes
# out of the decode, and Doppler/Gamma Doppler entries match on it, so the decode stays essential.
PROP_PAINT_SEED = 1
PROP_FLOAT = 2
PROP_INSPECT_PAYLOAD = 6

_PROPID_RE = re.compile(r"%propid:(\d+)%")
# Steam renders each sticker as its own <img title="Sticker: <name>"> inside the sticker_info
# block -- NOT as a comma-separated "Sticker: A, B, C" line. Matching on the quoted attribute is
# what keeps names intact; a naive [^<]+ scrape captures the attribute's own closing '">' and
# produces names like 'Crown (Foil)">'.
_STICKER_RE = re.compile(r'title="Sticker:\s*([^"]+)"', re.IGNORECASE)
_STICKER_TEXT_RE = re.compile(r"Sticker:\s*([^<\"]+)", re.IGNORECASE)  # older plain-text shape


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _asset_properties(asset: dict) -> dict:
    """{propertyid: value} from Steam's asset_properties list, whichever typed field it used."""
    out = {}
    for pr in (asset.get("asset_properties") or []):
        if not isinstance(pr, dict) or "propertyid" not in pr:
            continue
        for k in ("string_value", "int_value", "float_value"):
            if k in pr:
                out[pr["propertyid"]] = pr[k]
                break
    return out


def _fill_inspect_template(link: str, listing_id, asset_id, asset: dict) -> str:
    """Substitute Steam's inspect-link template placeholders.

    2026-07: CS2 market listings hand back `steam://run/730//+csgo_econ_action_preview %propid:6%`
    -- the item data rides in the asset's OWN properties, not in an M/A/D handle the way it used
    to. Filling %propid:N% from asset_properties yields a self-encoding link that decodes offline
    (inspect_decode.py). The legacy %listingid%/%assetid% substitutions are kept so older-shaped
    payloads still work."""
    props = _asset_properties(asset)
    link = _PROPID_RE.sub(lambda m: str(props.get(_as_int(m.group(1)), m.group(0))), link)
    return link.replace("%listingid%", str(listing_id)).replace("%assetid%", str(asset_id))


def _sticker_names_from_asset(asset: dict) -> list:
    """Sticker NAMES straight out of Steam's own listing description -- free, already in the
    render payload we just fetched. Offline link decoding yields sticker IDs, not names, and
    pattern_db's sticker_craft entries match on names ("Katowice 2014", "Holo"), so without this
    the two sticker_craft entries would silently stop matching. Steam renders them as a single
    "Sticker: A, B, C" line inside the sticker_info description block."""
    for d in (asset.get("descriptions") or []):
        if not isinstance(d, dict):
            continue
        value = d.get("value") or ""
        names = [n.strip() for n in _STICKER_RE.findall(value) if n.strip()]
        if names:
            return names
        m = _STICKER_TEXT_RE.search(value)
        if m:
            return [s.strip() for s in m.group(1).split(",") if s.strip()]
    return []


# --- Inspect cache (disk-backed) -----------------------------------------------------------

def _load_inspect_cache() -> None:
    """Loads the persistent resolved-item cache once per process. A resolved item is IMMUTABLE
    (an item's float/paint seed never change), so this is a pure write-once store: after the
    first pass over a target's book, every listing already seen is free forever -- including
    across restarts, which the old in-memory-only cache threw away every time."""
    global _inspect_cache_loaded
    with _inspect_cache_lock:
        if _inspect_cache_loaded:
            return
        _inspect_cache_loaded = True
        if not INSPECT_CACHE_FILE.exists():
            return
        try:
            data = json.loads(INSPECT_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # corrupt/unreadable cache is a performance loss, never a correctness one
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            _inspect_cache.update(data["items"])


def save_inspect_cache() -> None:
    """Flushes the resolved-item cache to disk (called at the end of each scan cycle)."""
    global _inspect_cache_dirty
    with _inspect_cache_lock:
        if not _inspect_cache_dirty:
            return
        snapshot = dict(_inspect_cache)
        _inspect_cache_dirty = False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INSPECT_CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"items": snapshot, "updated_at": time.time()}),
                       encoding="utf-8")
        tmp.replace(INSPECT_CACHE_FILE)  # atomic, same convention as the other stores here
    except OSError:
        pass  # a cache we couldn't persist just costs speed next run


def _cache_put(inspect_link: str, item: dict) -> None:
    global _inspect_cache_dirty
    with _inspect_cache_lock:
        if len(_inspect_cache) < _INSPECT_CACHE_MAX:
            _inspect_cache[inspect_link] = item
            _inspect_cache_dirty = True


def _resolve_item(inspect_link: str, *, market_hash_name: str, sticker_names: list,
                   allow_api: bool, prop_paint_seed: int | None = None,
                   prop_float: float | None = None) -> dict | None:
    """One listing -> {float_value, paint_seed, paint_index, stickers, wear_name,
    market_hash_name}, in the cheapest way that works:

      1. disk-backed cache (free)
      2. OFFLINE decode of the inspect link (free, microseconds -- the normal path, see
         inspect_decode.py and the module docstring)
      3. a configured fallback inspect service, ONLY for legacy M/A/D links that genuinely
         carry no embedded data, and only if allow_api left budget this cycle

    None = could not resolve; every reason is counted in _stats rather than guessed at."""
    _load_inspect_cache()
    core = _inspect_cache.get(inspect_link)
    if core is not None:
        with _stats_lock:
            _stats["cache_hits"] += 1
    else:
        core = inspect_decode.decode_inspect_link(inspect_link)
        if core is not None:
            with _stats_lock:
                _stats["decoded_offline"] += 1
                _stats["inspects_resolved"] += 1
            _cache_put(inspect_link, core)
        elif prop_paint_seed is not None and prop_float is not None:
            # Decode didn't apply (legacy link, or a payload shape we don't know), but Steam
            # published the seed and float as asset properties anyway. Those are authoritative
            # -- verified identical to the decode on 85/85 live listings -- so paint_seed
            # entries still match. paint_index stays None, so Doppler entries correctly do NOT
            # match rather than matching on a guess.
            core = {"paint_seed": prop_paint_seed, "float_value": prop_float,
                    "paint_index": None}
            with _stats_lock:
                _stats["resolved_from_properties"] = _stats.get("resolved_from_properties", 0) + 1
                _stats["inspects_resolved"] += 1
            _cache_put(inspect_link, core)
        else:
            # No embedded data (legacy M/A/D link). Only a real inspect service can help.
            if not (allow_api and INSPECT_API_URL):
                with _stats_lock:
                    _stats["legacy_links_skipped"] += 1
                    if not INSPECT_API_URL:
                        _stats["inspect_last_error"] = (
                            "legacy M/A/D inspect link carries no embedded item data and no "
                            "INSPECT_API_URL fallback is configured -- skipped, not guessed")
                return None
            core = _resolve_via_api(inspect_link)
            if core is None:
                return None
            with _stats_lock:
                _stats["resolved_via_api"] += 1
                _stats["inspects_resolved"] += 1
            _cache_put(inspect_link, core)
    fv = core.get("float_value")
    return {
        "float_value": fv,
        "paint_seed": core.get("paint_seed"),
        "paint_index": core.get("paint_index"),
        # Sticker NAMES come from Steam's own description (see _sticker_names_from_asset);
        # an API-resolved item may already carry them, in which case prefer those.
        "stickers": ([{"name": n} for n in (core.get("sticker_names") or sticker_names or [])]),
        "wear_name": (core.get("wear_name")
                      or (oddsmath.wear_from_float(fv) if isinstance(fv, (int, float)) else None)),
        "market_hash_name": core.get("market_hash_name") or market_hash_name,
    }


def _resolve_via_api(inspect_link: str) -> dict | None:
    """FALLBACK ONLY: resolve a legacy M/A/D link through a configured external inspect service
    (INSPECT_API_URL). Ships disabled -- the historical default, api.csfloat.com, is a dead
    hostname (NXDOMAIN) since csfloat/inspect was archived 2026-03-25. Kept because a legacy
    link is genuinely undecodable offline, so a self-hosted resolver is the only remedy.
    None = failed (recorded, never guessed)."""
    headers = {"User-Agent": "cs2-trade-calculator pattern scanner"}
    if CSFLOAT_API_KEY:
        headers["Authorization"] = CSFLOAT_API_KEY
    url = f"{INSPECT_API_URL}?url={urllib.parse.quote(inspect_link, safe='')}"
    try:
        with urlopen(Request(url, headers=headers), timeout=30, context=net.SSL_CONTEXT) as resp:
            payload = json.loads(resp.read())
    except HTTPError as e:
        with _stats_lock:
            _stats["inspects_failed"] += 1
            _stats["inspect_last_error"] = f"HTTP {e.code}: {e.reason}"
        return None
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        with _stats_lock:
            _stats["inspects_failed"] += 1
            _stats["inspect_last_error"] = f"{type(e).__name__}: {e}"
        return None
    info = payload.get("iteminfo") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        with _stats_lock:
            _stats["inspects_failed"] += 1
            _stats["inspect_last_error"] = "unexpected response shape"
        return None
    with _stats_lock:
        _stats["inspect_last_error"] = None
    # Same shape the offline decoder returns, so _resolve_item can treat both identically.
    return {
        "float_value": info.get("floatvalue"),
        "paint_seed": info.get("paintseed"),
        "paint_index": info.get("paintindex"),
        "sticker_names": [s.get("name") for s in (info.get("stickers") or [])
                          if isinstance(s, dict) and s.get("name")],
        "wear_name": info.get("wear_name"),
        "market_hash_name": info.get("full_item_name"),
    }


# --- Matching + valuation -----------------------------------------------------------------------

def _match_entry(entry: dict, item: dict) -> bool:
    etype = entry.get("type")
    if etype == "paint_seed":
        if item.get("paint_seed") not in (entry.get("paint_seeds") or []):
            return False
    elif etype == "paint_index":
        if item.get("paint_index") not in (entry.get("paint_indexes") or []):
            return False
    elif etype == "sticker_craft":
        stickers = item.get("stickers") or []
        needles = entry.get("sticker_match") or []
        require_holo = entry.get("sticker_require_holo")
        hit = False
        for s in stickers:
            name = s.get("name") or ""
            if any(n in name for n in needles):
                if require_holo and "Holo" not in name:
                    continue
                hit = True
                break
        if not hit:
            return False
    else:
        return False
    max_f = entry.get("max_float")
    min_f = entry.get("min_float")
    fv = item.get("float_value")
    if max_f is not None and (fv is None or fv > max_f):
        return False
    if min_f is not None and (fv is None or fv < min_f):
        return False
    return True


def _score_opportunity(o: dict) -> float:
    """Transparent ranking formula (deliberately NOT a model -- see module docstring):
    expected profit anchors the score; pattern tier, knowledge confidence, base-price market
    confidence, float desirability and liquidity scale it."""
    profit = max(0.0, o["expected_profit"] or 0.0)
    tier_factor = {1: 1.0, 2: 0.75, 3: 0.55}.get(o.get("pattern_tier"), 0.5)
    conf_factor = CONFIDENCE_FACTOR.get(o.get("confidence"), 0.4)
    market_conf_factor = {"high": 1.0, "medium": 0.8, "low": 0.5, "none": 0.3}.get(
        o.get("base_market_confidence"), 0.5)
    fv = o.get("float_value")
    float_factor = 1.0 if fv is None else max(0.7, 1.1 - 0.4 * fv)  # lower float = mildly better
    liquidity_factor = 0.5 + 0.5 * ((o.get("base_liquidity_score") or 50.0) / 100.0)
    return round(profit * tier_factor * conf_factor * market_conf_factor
                 * float_factor * liquidity_factor, 2)


def _base_price(name: str, metrics: dict) -> tuple[float | None, str]:
    """The ordinary-market price a pattern premium multiplies -- STEAM-FIRST (see module
    docstring): the skin's own Steam median-sale price from the bulk mirror when it exists,
    else the cross-market consensus (which can rest on souvenir-conflated or above-Steam-cap
    third-party listings, so callers demote confidence on the fallback). Returns
    (price | None, source_label)."""
    from . import bulk_prices  # lazy, matches this codebase's keep-startup-light convention
    steam_entry = bulk_prices.get_steam_entry(name, 1)
    if steam_entry and steam_entry.get("median_price"):
        return steam_entry["median_price"], "steam"
    return metrics["consensus_price"], "consensus"


def _opportunity_from_item(item: dict, ask: float, entry: dict, name: str, *, marketplace: str,
                            listing_id: str, listing_url: str | None,
                            image_url: str | None) -> dict | None:
    metrics = market_reliability_mod.market_metrics(name, 1)
    base, base_source = _base_price(name, metrics)
    if not base:
        return None  # can't value the pattern without a base price -- skip, never guess
    collector_value = round(base * float(entry.get("premium_multiplier") or 1.0), 2)
    edge_frac = (collector_value - ask) / collector_value if collector_value else 0.0
    # Every match is kept regardless of edge/profit -- the dashboard shows the whole picture
    # and lets the user sort/filter by profit, price, etc. themselves (see module docstring).
    confidence = entry.get("confidence", "low")
    # Spec's cross-verification rule: a base price fewer than 2 sources can confirm is not
    # "verified against multiple trusted marketplaces" -- demote, don't trust silently. A
    # base that ISN'T Steam's own sale price gets the same demotion: it may rest on
    # souvenir-conflated / above-Steam-cap third-party listings (see _base_price).
    if ((metrics["n_sources"] or 0) < 2 or base_source != "steam") and confidence != "low":
        confidence = "medium" if confidence == "high" else "low"
    o = {
        "listing_id": listing_id,
        "market_hash_name": name,
        "weapon": entry.get("weapon") if entry.get("weapon") not in (None, "*") else name.split(" | ")[0],
        "skin": entry.get("skin") if entry.get("skin") != "*" else name,
        "wear": item.get("wear_name"),
        "float_value": item.get("float_value"),
        "paint_seed": item.get("paint_seed"),
        "paint_index": item.get("paint_index"),
        "stickers": [s.get("name") for s in (item.get("stickers") or [])],
        "pattern_entry_id": entry.get("id"),
        "pattern_label": entry.get("label"),
        "pattern_tier": entry.get("tier"),
        "pattern_type": entry.get("type"),
        "confidence": confidence,
        "asking_price": ask,
        "base_market_price": base,
        "base_n_sources": metrics["n_sources"],
        "base_market_confidence": metrics["market_confidence"],
        "base_liquidity_score": metrics["liquidity_score"],
        "estimated_collector_value": collector_value,
        "expected_profit": round(collector_value - ask, 2),
        "edge_pct": round(edge_frac * 100.0, 1),
        "marketplace": marketplace,
        "base_price_source": base_source,
        "listing_url": listing_url,
        "image_url": image_url,
        "explanation": (f"{entry.get('label')} -- documented collector pattern "
                        f"({'seed ' + str(item.get('paint_seed')) if entry.get('type') == 'paint_seed' else 'paint index ' + str(item.get('paint_index')) if entry.get('type') == 'paint_index' else 'sticker craft'}). "
                        f"Ordinary {name} "
                        f"{'Steam median-sale price' if base_source == 'steam' else 'cross-market consensus price (no Steam sale evidence -- confidence demoted)'} "
                        f"is {base:.2f}; this pattern's documented premium "
                        f"(~{entry.get('premium_multiplier')}x, conservative) values it near "
                        f"{collector_value:.2f}, "
                        + (f"{edge_frac * 100.0:.0f}% above the ask."
                           if edge_frac >= 0 else
                           f"{abs(edge_frac) * 100.0:.0f}% below the ask (not profitable at this price).")),
        "source_key": entry.get("source"),
        "source_url": pattern_db_mod.source_url(entry.get("source", "")),
        "first_seen_at": time.time(),
        "last_seen_at": time.time(),
    }
    o["score"] = _score_opportunity(o)
    return o


# --- Opportunity store ---------------------------------------------------------------------------

def _load_store() -> dict:
    if not OPPORTUNITIES_FILE.exists():
        return {"opportunities": {}, "updated_at": None}
    try:
        store = json.loads(OPPORTUNITIES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"opportunities": {}, "updated_at": None}
    store.setdefault("opportunities", {})
    return store


def _save_store(store: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = time.time()
    OPPORTUNITIES_FILE.write_text(json.dumps(store), encoding="utf-8")


def _record_opportunities(found: list[dict]) -> None:
    now = time.time()
    with _store_lock:
        store = _load_store()
        opps = store["opportunities"]
        for o in found:
            prev = opps.get(o["listing_id"])
            if prev is not None:
                o["first_seen_at"] = prev.get("first_seen_at", o["first_seen_at"])
            opps[o["listing_id"]] = o
        for lid in [lid for lid, o in opps.items()
                    if now - (o.get("last_seen_at") or 0) > OPPORTUNITY_TTL_SECONDS]:
            del opps[lid]
        _save_store(store)
    with _stats_lock:
        _stats["matches"] += len(found)


def clear_opportunities() -> int:
    """Wipes every scanned opportunity plus the in-memory inspect-link cache and cumulative
    stats counters. This is a re-scannable market snapshot (matches re-appear on the next
    cycle if they're still live), not simulated/outcome data. Returns how many opportunities
    were removed. Used by the dashboard's global 'Reset all data' button."""
    with _store_lock:
        store = _load_store()
        n = len(store["opportunities"])
        _save_store({"opportunities": {}, "updated_at": None})
    global _inspect_cache_dirty
    with _inspect_cache_lock:
        _inspect_cache.clear()
        _inspect_cache_dirty = False
    try:
        INSPECT_CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    with _stats_lock:
        for key in ("matches", "steam_queries", "steam_listings_seen",
                    "inspects_resolved", "inspects_failed", "steam_consecutive_failures",
                    "decoded_offline", "resolved_via_api", "legacy_links_skipped",
                    "cache_hits", "skipped_price_ceiling"):
            _stats[key] = 0
        for key in ("last_attempt_at", "last_cycle_completed_at", "steam_last_success_at",
                    "steam_last_error", "steam_blocked_until", "inspect_last_error"):
            _stats[key] = None
    return n


def list_opportunities(*, weapon: str | None = None, pattern_type: str | None = None,
                        marketplace: str | None = None, min_profit: float | None = None,
                        min_confidence: str | None = None, min_float: float | None = None,
                        max_float: float | None = None, tier: int | None = None,
                        sort: str = "score") -> list[dict]:
    """Filter + sort per the spec's dedicated-page requirements. sort: score (default,
    'best opportunity') | profit | premium | price | confidence | newest."""
    out = list(_load_store()["opportunities"].values())
    # The user's skin blacklist bans a skin EVERYWHERE, and this list sits on the same
    # Opportunities page as the trade-up leaderboard -- blacklisting the skin behind a bogus
    # price has to clear it from here too, not just from the trade-up tables. Filtered at list
    # time (not at scan time) for the same reason sim_loop._entry_hidden is: opportunities
    # banked before the blacklist entry existed disappear immediately, with no re-scan, and
    # come back by themselves if the entry is removed or its duration runs out.
    blacklisted = user_overrides_mod.blacklisted_skins()
    if blacklisted:
        out = [o for o in out
               if user_overrides_mod.bare_skin_name(o.get("market_hash_name") or "") not in blacklisted]
    if weapon:
        out = [o for o in out if weapon.lower() in (o.get("weapon") or "").lower()]
    if pattern_type:
        out = [o for o in out if o.get("pattern_type") == pattern_type]
    if marketplace:
        out = [o for o in out if o.get("marketplace") == marketplace]
    if min_profit is not None:
        out = [o for o in out if (o.get("expected_profit") or 0) >= min_profit]
    if min_confidence:
        floor = CONFIDENCE_FACTOR.get(min_confidence, 0.0)
        out = [o for o in out if CONFIDENCE_FACTOR.get(o.get("confidence"), 0.0) >= floor]
    if min_float is not None:
        out = [o for o in out if o.get("float_value") is not None and o["float_value"] >= min_float]
    if max_float is not None:
        out = [o for o in out if o.get("float_value") is not None and o["float_value"] <= max_float]
    if tier is not None:
        out = [o for o in out if o.get("pattern_tier") == tier]
    keys = {"score": lambda o: o.get("score") or 0,
            "profit": lambda o: o.get("expected_profit") or 0,
            "premium": lambda o: o.get("estimated_collector_value") or 0,
            "price": lambda o: -(o.get("asking_price") or 0),  # negated: cheapest first below
            "confidence": lambda o: CONFIDENCE_FACTOR.get(o.get("confidence"), 0),
            "newest": lambda o: o.get("first_seen_at") or 0}
    out.sort(key=keys.get(sort, keys["score"]), reverse=True)
    return out


# --- Scan loop -------------------------------------------------------------------------------------

def scan_target(target: dict) -> list[dict]:
    """One match pass for one target against the Steam Market listings render endpoint +
    inspect-link resolution (see _fetch_steam_listings/_resolve_item -- the only
    listing source, see module docstring). Sticker-craft entries are matched opportunistically
    against every listing fetched here, whatever the target was."""
    sticker_entries = pattern_db_mod.entries("sticker_craft")
    found = _scan_target_steam(target, sticker_entries)
    if found:
        _record_opportunities(found)
    return found


def _price_ceiling(name: str, entries: list) -> float | None:
    """Highest ask at which ANY entry considered for this target could still be a deal, i.e.
    base price x the most generous premium multiplier in play. Listings above it cannot be
    profitable even if they DO carry the pattern, so they are never even decoded.

    None = no base price known, in which case no listing is filtered (never guess)."""
    try:
        metrics = market_reliability_mod.market_metrics(name, 1)
        base, _src = _base_price(name, metrics)
    except Exception:  # noqa: BLE001 -- a valuation hiccup must not stop the scan
        return None
    if not base:
        return None
    mults = [float(e.get("premium_multiplier") or 1.0) for e in entries]
    return base * max(mults) if mults else None


def _scan_target_steam(target: dict, sticker_entries: list) -> list[dict]:
    """The Steam Market half of scan_target: fetch this target's cheapest live Steam listings,
    resolve each one's float/seed/stickers (offline decode first -- see _resolve_item), and
    match. The ask used for edge math is Steam's full buyer-pays price, and the listing link
    goes straight to the item's own Steam Market page.

    Scans the STEAM_CHEAPEST_LISTINGS cheapest listings EVERY cycle rather than walking deeper
    into the book each cycle -- see that constant's comment for why the old cursor walk was
    backwards for this engine. Listings priced above _price_ceiling() are skipped outright."""
    if _steam_blocked():
        return []  # sitting out a Steam 429 cooldown
    name = target["market_hash_name"]
    listings = _fetch_steam_listings(name)
    if not listings:
        return []
    all_entries = target["entries"] + sticker_entries
    ceiling = _price_ceiling(name, all_entries)
    found = []
    api_budget = MAX_API_INSPECTS_PER_CYCLE
    market_url = f"https://steamcommunity.com/market/listings/730/{urllib.parse.quote(name)}"
    window = listings[:STEAM_CHEAPEST_LISTINGS]
    for i, listing in enumerate(window):
        # listings are sorted cheapest-first, so the first one over the ceiling ends the walk
        if ceiling is not None and listing["ask"] > ceiling:
            with _stats_lock:
                _stats["skipped_price_ceiling"] += len(window) - i
            break
        link = listing["inspect_link"]
        # Decides only whether the fallback-pacing sleep below applies. This decodes the link a
        # second time (_resolve_item decodes it too), which is deliberate: a decode is ~10us of
        # pure CPU, so ~30ms per full cycle, against a cycle measured in minutes -- not worth
        # threading an extra out-param through _resolve_item to avoid.
        needs_api = (link not in _inspect_cache
                     and inspect_decode.decode_inspect_link(link) is None)
        item = _resolve_item(link, market_hash_name=name,
                             sticker_names=listing.get("sticker_names") or [],
                             allow_api=(api_budget > 0),
                             prop_paint_seed=listing.get("prop_paint_seed"),
                             prop_float=listing.get("prop_float"))
        if needs_api and INSPECT_API_URL and api_budget > 0:
            api_budget -= 1
            time.sleep(INSPECT_INTERVAL_SECONDS)  # pacing applies to the fallback path ONLY
        if item is None:
            continue
        item = {**item, "icon_url": listing.get("icon_url")}
        for entry in all_entries:
            if _match_entry(entry, item):
                o = _opportunity_from_item(
                    item, listing["ask"], entry, item.get("market_hash_name") or name,
                    marketplace="steam",
                    listing_id=f"steam:{listing['listing_id']}",
                    listing_url=market_url,
                    image_url=(f"https://community.fastly.steamstatic.com/economy/image/{listing['icon_url']}"
                               if listing.get("icon_url") else None))
                if o is not None:
                    found.append(o)
                break  # one opportunity per listing -- first (most specific) entry wins
    return found


def scan_loop(state: PatternScanState) -> None:
    """Background loop: cycles the target list, one polite query per interval; refreshes the
    pattern knowledge every REFRESH_INTERVAL_SECONDS (pattern_db.refresh_due) so the scan plan
    follows the knowledge base. Same pause/stop conventions as every other loop here."""
    idx = 0
    targets: list = []
    while True:
        if state.stop_event.is_set():
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
        if state.pause_event.is_set():
            with state.lock:
                state.status = "paused"
                state.current_target = None
            while state.pause_event.is_set() and not state.stop_event.is_set():
                state.stop_event.wait(timeout=1.0)
            continue
        with state.lock:
            state.status = "scanning"
        if pattern_db_mod.refresh_due():
            try:
                pattern_db_mod.refresh_knowledge()
            except Exception:  # noqa: BLE001 -- refresh failure must never stop scanning
                pass
        if idx == 0 or idx >= len(targets):
            save_inspect_cache()  # flush newly-resolved items at each cycle boundary
            targets = build_targets()
            idx = 0
            if not targets:
                state.stop_event.wait(timeout=60)
                continue
            with _stats_lock:
                if idx == 0 and _stats["steam_queries"]:
                    _stats["last_cycle_completed_at"] = time.time()
        target = targets[idx]
        idx += 1
        with state.lock:
            state.current_target = target["market_hash_name"]
        try:
            scan_target(target)
        except Exception as e:  # noqa: BLE001 -- surface, don't die (same as sim_loop)
            with _stats_lock:
                _stats["last_error"] = f"{type(e).__name__}: {e}"
        # Back off the between-target pacing while Steam is refusing us, on top of (not
        # instead of) the dedicated _steam_blocked() cooldown _scan_target_steam already sits
        # out -- no point hammering through a run of 429s at the normal cadence either.
        with _stats_lock:
            failures = _stats["steam_consecutive_failures"]
        delay = REQUEST_INTERVAL_SECONDS * (1 + min(failures, 10))
        if state.stop_event.wait(timeout=delay):
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
