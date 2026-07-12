"""Rare Pattern Arbitrage Engine: continuously scans marketplace listings for items whose
seller priced a documented collector pattern (pattern_db.py) at ordinary market price, and
turns matches into ranked, auditable Rare Pattern Opportunities.

Operates INDEPENDENTLY from the trade-up optimizer (its own background thread, own data file,
own dashboard page) while reusing the shared infrastructure the spec calls for: consensus
pricing + liquidity + confidence come from market_reliability.market_metrics (the same
cached-feed cross-marketplace math the reliability registry uses), marketplace deep links from
marketplaces.py, and the same start/pause/stop thread conventions as every other loop.

Source coverage -- honest, up front (same disclosure style as thirdparty_prices.py):
  - CSFloat's listings API returns per-listing float, paint seed, paint index and stickers
    directly -- the data pattern matching needs. As of 2026-07 (verified live) the endpoint
    returns HTTP 403 without authentication: a CSFLOAT_API_KEY (env var, shared with
    thirdparty_prices.py) is required for THIS source. Keys are free -- CSFloat account ->
    profile -> developers tab. Without one this source reports the 403 plainly in its status
    and finds nothing -- it never fakes results.
  - STEAM MARKET (since 2026-07): Steam's own listings pages don't publish float/seed, but
    each listing DOES carry an in-game inspect link (the market listings /render endpoint
    exposes it per listing). Those inspect links are resolved to float/paint seed/paint
    index/stickers through the CSFloat INSPECT API (api.csfloat.com -- a separate, public
    endpoint from the listings API above; an API key is sent when configured but is not
    required), giving true per-listing pattern data for items listed ON STEAM. This is the
    only way to buy a matched pattern directly on the Steam Market. Steam rate-limits the
    render endpoint aggressively for anonymous callers -- failures back the scanner off and
    are reported in the status panel, never hidden.
  - The Skinport/Buff163 bulk mirrors are price-only: they feed the VALUATION side (the base
    price a pattern's premium is measured against) but cannot be scanned for patterns
    themselves. Their per-listing scan support is a documented limitation, not a silent gap
    -- get_status() says so.

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
import threading
import time
import urllib.parse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import market_reliability as market_reliability_mod
from . import net
from . import pattern_db as pattern_db_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OPPORTUNITIES_FILE = DATA_DIR / "pattern_opportunities.json"

CSFLOAT_LISTINGS_URL = "https://csfloat.com/api/v1/listings"
CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY")  # optional, shared with thirdparty_prices


def configure_csfloat_key(key: str | None):
    """Live setter for CSFLOAT_API_KEY, mirroring thirdparty_prices.configure_csfloat_key() --
    this module keeps its own separate module-level constant (see the comment above), so the
    dashboard's Connections settings page has to call both setters, not just one."""
    global CSFLOAT_API_KEY
    CSFLOAT_API_KEY = key or None

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
STEAM_OPTOUT_COOKIE = "bMarketOptOut=1"
INSPECT_API_URL = "https://api.csfloat.com/"
STEAM_LISTINGS_PER_QUERY = 100  # listings fetched per Steam render call (cheapest-first) --
# 100 is the Market render endpoint's own page-size ceiling, i.e. every live listing Steam
# will hand back in one call, not an arbitrary choice
STEAM_INSPECTS_PER_CYCLE = 10   # of those, how many get an inspect resolution in ONE cycle --
# 2026-07-11 (user request): rather than re-resolving the same top N every cycle, each target
# keeps a cursor (_steam_scan_cursor) into its cheapest-first book and walks it in chunks of
# this size -- cycle 1 covers the 10 cheapest listings, cycle 2 the next 10, cycle 3 the next
# 10 after that, wrapping back to the cheapest once the book (or the 100-listing page ceiling
# above) is exhausted. Each resolution is its own throttled API call (INSPECT_INTERVAL_SECONDS
# apart), so this keeps a single cycle short while still covering the full book over time.
INSPECT_INTERVAL_SECONDS = 1.5  # pacing between inspect resolutions (the public endpoint's
# long-standing informal budget is ~1/second; stay under it)
STEAM_BLOCK_BASE_SECONDS = 60   # first 429 -> skip Steam scanning this long; doubles per
STEAM_BLOCK_MAX_SECONDS = 300   # consecutive block, capped here; any success resets it.
# Matches steam_prices.py's RATE_LIMIT_BACKOFF_SECONDS/MAX_RATE_LIMIT_BACKOFF_SECONDS -- that
# module hit the same Steam 429 wall earlier and 60s/300s is what real-world tuning landed on;
# this scanner's old 300s/3600s was 5-20x more conservative than necessary for no known reason.
STEAM_BLOCK_JITTER_SECONDS = 5  # randomized so repeated failures don't all cool down in lockstep
_INSPECT_CACHE_MAX = 5000       # resolved inspect links are immutable (an item's float/seed
# never changes), so cache them in-memory and only pay the API call once per listing

REQUEST_INTERVAL_SECONDS = 4.0   # one listings query per target per this interval -- polite
# pacing for an unauthenticated public API (a full target cycle takes minutes, and patterns
# don't appear/vanish second-to-second anyway)
LISTINGS_PER_QUERY = 50
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
_stats = {"queries": 0, "listings_seen": 0, "matches": 0, "opportunities_live": 0,
          "last_attempt_at": None, "last_success_at": None, "last_error": None,
          "consecutive_failures": 0, "last_cycle_completed_at": None,
          # Steam Market source (kept separate so a Steam rate-limit streak is visibly a
          # STEAM problem, not blamed on CSFloat's listings API or vice versa):
          "steam_queries": 0, "steam_listings_seen": 0, "steam_last_success_at": None,
          "steam_last_error": None, "steam_consecutive_failures": 0,
          "steam_blocked_until": None,  # epoch until which the Steam source sits out (429 cooldown)
          "inspects_resolved": 0, "inspects_failed": 0, "inspect_last_error": None}

_inspect_cache: dict = {}  # inspect link -> resolved item dict (see _resolve_inspect_link)

_steam_scan_cursor: dict = {}  # market_hash_name -> offset into that target's cheapest-first
# Steam listings; advanced by STEAM_INSPECTS_PER_CYCLE each cycle (see _scan_target_steam)
_steam_scan_cursor_lock = threading.Lock()

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
                "csfloat_key_configured": CSFLOAT_API_KEY is not None,
                "csfloat_key_note": (None if CSFLOAT_API_KEY else
                    "CSFloat requires a (free) API key for its LISTINGS queries as of 2026-07: "
                    "csfloat.com -> profile -> developers tab, then set the CSFLOAT_API_KEY "
                    "environment variable before starting the server. The Steam Market source "
                    "below still works without one (its inspect resolutions use CSFloat's "
                    "separate public inspect endpoint)."),
                "request_interval_seconds": REQUEST_INTERVAL_SECONDS,
                "n_targets": len(build_targets()),
                "scan_sources": {
                    "steam": "supported -- Steam Market listings scanned directly: each "
                             "listing's inspect link is resolved to float/seed/stickers via "
                             "the public CSFloat inspect API "
                             f"({STEAM_INSPECTS_PER_CYCLE} cheapest listings per target per "
                             "cycle, walking the next batch of "
                             f"{STEAM_INSPECTS_PER_CYCLE} each subsequent cycle until the book "
                             "is covered, then wrapping back to the cheapest)",
                    "csfloat": "supported (float + paint seed + stickers per listing; needs "
                               "CSFLOAT_API_KEY)",
                    "skinport": "valuation only -- bulk mirror carries prices, not seeds",
                    "buff163": "valuation only -- bulk mirror carries prices, not seeds",
                }}


# --- Targets ---------------------------------------------------------------------------------

def _market_name(weapon: str, skin: str, wear: str) -> str:
    return f"{weapon} | {skin} ({wear})"


def build_targets() -> list[dict]:
    """One target = one CSFloat listings query = (market_hash_name, [entries to match]).
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


# --- CSFloat fetch -----------------------------------------------------------------------------

def _fetch_listings(market_hash_name: str) -> list | None:
    """One listings query. None = fetch failed (recorded); [] = genuinely no listings."""
    params = urllib.parse.urlencode({"market_hash_name": market_hash_name,
                                      "sort_by": "lowest_price", "limit": LISTINGS_PER_QUERY})
    headers = {"User-Agent": "cs2-trade-calculator pattern scanner"}
    if CSFLOAT_API_KEY:
        headers["Authorization"] = CSFLOAT_API_KEY
    req = Request(f"{CSFLOAT_LISTINGS_URL}?{params}", headers=headers)
    with _stats_lock:
        _stats["queries"] += 1
        _stats["last_attempt_at"] = time.time()
    try:
        with urlopen(req, timeout=30, context=net.SSL_CONTEXT) as resp:
            payload = json.loads(resp.read())
    except HTTPError as e:
        with _stats_lock:
            _stats["last_error"] = f"HTTP {e.code}: {e.reason}"
            _stats["consecutive_failures"] += 1
        return None
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        with _stats_lock:
            _stats["last_error"] = f"{type(e).__name__}: {e}"
            _stats["consecutive_failures"] += 1
        return None
    listings = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(listings, list):
        with _stats_lock:
            _stats["last_error"] = "unexpected response shape"
            _stats["consecutive_failures"] += 1
        return None
    with _stats_lock:
        _stats["last_success_at"] = time.time()
        _stats["last_error"] = None
        _stats["consecutive_failures"] = 0
        _stats["listings_seen"] += len(listings)
    return listings


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
    in the status panel."""
    params = urllib.parse.urlencode({"query": "", "start": 0, "count": STEAM_LISTINGS_PER_QUERY,
                                      "country": "US", "language": "english", "currency": 1})
    url = STEAM_RENDER_URL.format(urllib.parse.quote(market_hash_name)) + f"?{params}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 cs2-trade-calculator/1.0",
                                 "Cookie": STEAM_OPTOUT_COOKIE})
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
        inspect = link.replace("%listingid%", str(lid)).replace("%assetid%", str(asset_ref.get("id", "")))
        out.append({"listing_id": str(lid), "ask": round(price_cents / 100.0, 2),
                    "inspect_link": inspect, "icon_url": asset.get("icon_url")})
    out.sort(key=lambda l: l["ask"])
    with _stats_lock:
        _stats["steam_last_success_at"] = time.time()
        _stats["steam_last_error"] = None
        _stats["steam_consecutive_failures"] = 0
        _stats["steam_blocked_until"] = None
        _stats["steam_listings_seen"] += len(out)
    return out


def _resolve_inspect_link(inspect_link: str) -> dict | None:
    """Resolves one in-game inspect link to {float_value, paint_seed, paint_index, stickers}
    via CSFloat's public INSPECT endpoint (api.csfloat.com -- distinct from the key-gated
    listings API; the key is sent when configured, for a friendlier rate budget). Cached
    permanently in-memory: an item's float/seed never change, so each listing costs one call
    ever. None = resolution failed (recorded, never guessed)."""
    cached = _inspect_cache.get(inspect_link)
    if cached is not None:
        return cached
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
    item = {
        "float_value": info.get("floatvalue"),
        "paint_seed": info.get("paintseed"),
        "paint_index": info.get("paintindex"),
        "stickers": [{"name": s.get("name")} for s in (info.get("stickers") or [])
                     if isinstance(s, dict)],
        "wear_name": info.get("wear_name"),
        "market_hash_name": info.get("full_item_name"),
    }
    with _stats_lock:
        _stats["inspects_resolved"] += 1
        _stats["inspect_last_error"] = None
    if len(_inspect_cache) >= _INSPECT_CACHE_MAX:
        _inspect_cache.clear()  # crude but sufficient: refilling costs one call per live listing
    _inspect_cache[inspect_link] = item
    return item


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


def _opportunity_from(listing: dict, entry: dict, market_hash_name: str) -> dict | None:
    """CSFloat-listing adapter over _opportunity_from_item (the marketplace-agnostic core)."""
    item = listing.get("item") or {}
    price_cents = listing.get("price")
    if price_cents is None:
        return None
    ask = round(price_cents / 100.0, 2)
    name = item.get("market_hash_name") or market_hash_name
    listing_id = str(listing.get("id") or f"{name}:{item.get('paint_seed')}:{ask}")
    return _opportunity_from_item(
        item, ask, entry, name, marketplace="csfloat",
        listing_id=f"csfloat:{listing_id}",
        listing_url=f"https://csfloat.com/item/{listing.get('id')}" if listing.get("id") else None,
        image_url=(f"https://community.fastly.steamstatic.com/economy/image/{item['icon_url']}"
                   if item.get("icon_url") else None))


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
    _inspect_cache.clear()
    with _steam_scan_cursor_lock:
        _steam_scan_cursor.clear()
    with _stats_lock:
        for key in ("queries", "listings_seen", "matches", "steam_queries", "steam_listings_seen",
                    "inspects_resolved", "inspects_failed", "consecutive_failures",
                    "steam_consecutive_failures"):
            _stats[key] = 0
        for key in ("last_attempt_at", "last_success_at", "last_error", "last_cycle_completed_at",
                    "steam_last_success_at", "steam_last_error", "steam_blocked_until",
                    "inspect_last_error"):
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
    """One match pass for one target across BOTH per-listing sources: CSFloat's listings API
    (when a key is configured) and the Steam Market itself (listings render endpoint +
    inspect-link resolution -- see _fetch_steam_listings/_resolve_inspect_link). Sticker-craft
    entries are matched opportunistically against every listing fetched here, whatever the
    target was."""
    sticker_entries = pattern_db_mod.entries("sticker_craft")
    found = []

    listings = _fetch_listings(target["market_hash_name"]) or []
    for listing in listings:
        item = listing.get("item") or {}
        for entry in target["entries"] + sticker_entries:
            if _match_entry(entry, item):
                o = _opportunity_from(listing, entry, target["market_hash_name"])
                if o is not None:
                    found.append(o)
                break  # one opportunity per listing -- first (most specific) entry wins

    found.extend(_scan_target_steam(target, sticker_entries))

    if found:
        _record_opportunities(found)
    return found


def _scan_target_steam(target: dict, sticker_entries: list) -> list[dict]:
    """The Steam Market half of scan_target: fetch this target's cheapest live Steam listings,
    resolve each one's inspect link to real float/seed/sticker data (throttled, cached), and
    match exactly like the CSFloat path. The ask used for edge math is Steam's full
    buyer-pays price, and the listing link goes straight to the item's own Steam Market page.

    Only resolves STEAM_INSPECTS_PER_CYCLE listings THIS cycle, starting from a per-target
    cursor (_steam_scan_cursor): the first cycle covers the 10 cheapest, the next cycle the
    next 10, and so on, wrapping back to the cheapest once the fetched book is exhausted --
    see STEAM_INSPECTS_PER_CYCLE's comment."""
    if _steam_blocked():
        return []  # sitting out a Steam 429 cooldown -- CSFloat scanning continues meanwhile
    name = target["market_hash_name"]
    listings = _fetch_steam_listings(name)
    if not listings:
        return []
    with _steam_scan_cursor_lock:
        offset = _steam_scan_cursor.get(name, 0)
        if offset >= len(listings):
            offset = 0  # walked past the end of this cycle's book -- wrap back to the cheapest
        _steam_scan_cursor[name] = offset + STEAM_INSPECTS_PER_CYCLE
    found = []
    market_url = f"https://steamcommunity.com/market/listings/730/{urllib.parse.quote(name)}"
    for listing in listings[offset:offset + STEAM_INSPECTS_PER_CYCLE]:
        was_cached = listing["inspect_link"] in _inspect_cache
        item = _resolve_inspect_link(listing["inspect_link"])
        if not was_cached:
            time.sleep(INSPECT_INTERVAL_SECONDS)  # polite pacing for the public inspect API
        if item is None:
            continue
        item = {**item, "icon_url": listing.get("icon_url")}
        for entry in target["entries"] + sticker_entries:
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
            targets = build_targets()
            idx = 0
            if not targets:
                state.stop_event.wait(timeout=60)
                continue
            with _stats_lock:
                if idx == 0 and _stats["queries"]:
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
        # Back off harder while the CSFloat listings API is refusing us -- no point hammering
        # through a 429. (Steam refusals don't slow this loop: the Steam source sits out its
        # own dedicated cooldown instead -- see _record_steam_failure/_steam_blocked -- so
        # CSFloat scanning continues at full pace meanwhile.)
        with _stats_lock:
            failures = _stats["consecutive_failures"]
        delay = REQUEST_INTERVAL_SECONDS * (1 + min(failures, 10))
        if state.stop_event.wait(timeout=delay):
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
