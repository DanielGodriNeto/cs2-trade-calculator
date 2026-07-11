"""Rare Pattern Arbitrage Engine: continuously scans marketplace listings for items whose
seller priced a documented collector pattern (pattern_db.py) at ordinary market price, and
turns matches into ranked, auditable Rare Pattern Opportunities.

Operates INDEPENDENTLY from the trade-up optimizer (its own background thread, own data file,
own dashboard page) while reusing the shared infrastructure the spec calls for: consensus
pricing + liquidity + confidence come from market_reliability.market_metrics (the same
cached-feed cross-marketplace math the reliability registry uses), marketplace deep links from
marketplaces.py, and the same start/pause/stop thread conventions as every other loop.

Source coverage -- honest, up front (same disclosure style as thirdparty_prices.py):
  - CSFloat is the ONLY supported marketplace whose listings API returns per-listing float,
    paint seed, paint index and stickers -- the data pattern matching needs. As of 2026-07
    (verified live) the endpoint returns HTTP 403 without authentication: a CSFLOAT_API_KEY
    (env var, shared with thirdparty_prices.py) is REQUIRED to scan. Keys are free -- CSFloat
    account -> profile -> developers tab. Without one the scanner runs, reports the 403
    plainly in its status, and finds nothing -- it never fakes results.
  - Steam Market listings carry no float/seed without per-item inspect resolution (no public
    API); the Skinport/Buff163 bulk mirrors are price-only. So those marketplaces feed the
    VALUATION side (consensus price a pattern's premium is measured against) but cannot be
    scanned for patterns themselves. Their per-listing scan support is a documented limitation,
    not a silent gap -- get_status() says so.

Detection is knowledge-driven, never ML-driven (spec): a listing only becomes an opportunity
when it matches a pattern_db entry (documented public collector knowledge). The ranking score
is a transparent formula over expected profit / tier / confidence / float desirability /
liquidity -- no model anywhere in the match path.

Valuation: estimated_collector_value = consensus market price for that exact skin+wear
(market_reliability.market_metrics -- Steam mirror + Skinport + Buff163 median) x the entry's
conservative premium multiplier. A listing is flagged when its ask sits below that estimate by
at least MIN_EDGE_FRAC. Both numbers ride on the opportunity row so the user can audit the
math, alongside the pattern's documenting source link. Per the spec's cross-verification rule,
opportunities whose BASE price can't be cross-verified (fewer than 2 sources agreeing) have
their confidence demoted and are marked accordingly rather than silently trusted.
"""
import json
import os
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

REQUEST_INTERVAL_SECONDS = 4.0   # one listings query per target per this interval -- polite
# pacing for an unauthenticated public API (a full target cycle takes minutes, and patterns
# don't appear/vanish second-to-second anyway)
LISTINGS_PER_QUERY = 50
MIN_EDGE_FRAC = 0.30             # ask must sit >=30% below estimated collector value
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
          "consecutive_failures": 0, "last_cycle_completed_at": None}

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
                    "CSFloat requires a (free) API key for listings queries as of 2026-07: "
                    "csfloat.com -> profile -> developers tab, then set the CSFLOAT_API_KEY "
                    "environment variable before starting the server."),
                "request_interval_seconds": REQUEST_INTERVAL_SECONDS,
                "n_targets": len(build_targets()),
                "scan_sources": {
                    "csfloat": "supported (float + paint seed + stickers per listing)",
                    "steam": "valuation only -- no public per-listing float/seed API",
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


def _opportunity_from(listing: dict, entry: dict, market_hash_name: str) -> dict | None:
    item = listing.get("item") or {}
    price_cents = listing.get("price")
    if price_cents is None:
        return None
    ask = round(price_cents / 100.0, 2)
    name = item.get("market_hash_name") or market_hash_name
    metrics = market_reliability_mod.market_metrics(name, 1)
    base = metrics["consensus_price"]
    if not base:
        return None  # can't value the pattern without a base price -- skip, never guess
    collector_value = round(base * float(entry.get("premium_multiplier") or 1.0), 2)
    edge_frac = (collector_value - ask) / collector_value if collector_value else 0.0
    if edge_frac < MIN_EDGE_FRAC:
        return None
    confidence = entry.get("confidence", "low")
    # Spec's cross-verification rule: a base price fewer than 2 sources can confirm is not
    # "verified against multiple trusted marketplaces" -- demote, don't trust silently.
    if (metrics["n_sources"] or 0) < 2 and confidence != "low":
        confidence = "medium" if confidence == "high" else "low"
    listing_id = str(listing.get("id") or f"{name}:{item.get('paint_seed')}:{ask}")
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
        "marketplace": "csfloat",
        "listing_url": f"https://csfloat.com/item/{listing.get('id')}" if listing.get("id") else None,
        "image_url": (f"https://community.fastly.steamstatic.com/economy/image/{item['icon_url']}"
                      if item.get("icon_url") else None),
        "explanation": (f"{entry.get('label')} -- documented collector pattern "
                        f"({'seed ' + str(item.get('paint_seed')) if entry.get('type') == 'paint_seed' else 'paint index ' + str(item.get('paint_index')) if entry.get('type') == 'paint_index' else 'sticker craft'}). "
                        f"Ordinary {name} consensus price is {base:.2f} across "
                        f"{metrics['n_sources']} source(s); this pattern's documented premium "
                        f"(~{entry.get('premium_multiplier')}x, conservative) values it near "
                        f"{collector_value:.2f}, {edge_frac * 100.0:.0f}% above the ask."),
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


def list_opportunities(*, weapon: str | None = None, pattern_type: str | None = None,
                        marketplace: str | None = None, min_profit: float | None = None,
                        min_confidence: str | None = None, min_float: float | None = None,
                        max_float: float | None = None, tier: int | None = None,
                        sort: str = "score") -> list[dict]:
    """Filter + sort per the spec's dedicated-page requirements. sort: score (default,
    'best opportunity') | profit | premium | confidence | newest."""
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
            "confidence": lambda o: CONFIDENCE_FACTOR.get(o.get("confidence"), 0),
            "newest": lambda o: o.get("first_seen_at") or 0}
    out.sort(key=keys.get(sort, keys["score"]), reverse=True)
    return out


# --- Scan loop -------------------------------------------------------------------------------------

def scan_target(target: dict) -> list[dict]:
    """One listings query + match pass for one target. Sticker-craft entries are matched
    opportunistically against every listing fetched here, whatever the target was."""
    listings = _fetch_listings(target["market_hash_name"])
    if not listings:
        return []
    sticker_entries = pattern_db_mod.entries("sticker_craft")
    found = []
    for listing in listings:
        item = listing.get("item") or {}
        for entry in target["entries"] + sticker_entries:
            if _match_entry(entry, item):
                o = _opportunity_from(listing, entry, target["market_hash_name"])
                if o is not None:
                    found.append(o)
                break  # one opportunity per listing -- first (most specific) entry wins
    if found:
        _record_opportunities(found)
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
        # Back off harder while the API is refusing us -- no point hammering through a 429.
        with _stats_lock:
            failures = _stats["consecutive_failures"]
        delay = REQUEST_INTERVAL_SECONDS * (1 + min(failures, 10))
        if state.stop_event.wait(timeout=delay):
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
