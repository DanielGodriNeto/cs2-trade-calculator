"""Free bulk price mirror (prices.csgotrader.app) -- the PRIMARY price source since 2026-07.

Why a mirror instead of the marketplaces' own APIs (all verified live, 2026-07-10):
  - steamcommunity.com/market/priceoverview now returns HTTP 429 to essentially EVERY
    anonymous (cookie-less) request -- even the first one after minutes of silence, from a
    clean residential IP whose regular Steam pages load fine. It is no longer a rate limit
    this codebase can be polite enough to stay under; it's an endpoint policy (a logged-in
    session cookie appears to be required now). See steam_prices.py for the fallback that
    remains for people who re-enable it.
  - api.skinport.com/v1/items sits behind Cloudflare Bot Management with a JS challenge (since
    ~2025): any non-browser client gets HTTP 403 regardless of headers, IP class, or
    Accept-Encoding. The old note here that it "works from a residential connection" is stale.

csgotrader.app (the browser extension's backend) publishes free static JSON dumps on
CloudFront -- no auth, no bot protection, refreshed several times a day:
  - steam.json: {market_hash_name: {last_24h, last_7d, last_30d, last_90d}} -- Steam Market
    MEDIAN SALE prices in USD. This is real Steam price data, i.e. exactly the market this
    project buys inputs and sells outputs on, just mirrored -- unlike Skinport prices, which
    are a different (cheaper) marketplace entirely.
  - skinport.json: {market_hash_name: {starting_at, suggested_price}} in USD -- feeds
    thirdparty_prices.py's drought cross-check, replacing the blocked live endpoint.
  - buff163.json: {market_hash_name: {starting_at: {price}, highest_order: {price}}} in USD --
    Buff163 is the largest third-party CS2 marketplace, so its listing price is a second,
    genuinely independent cross-check source alongside Skinport (Skinport and Steam can both
    run thin on the same obscure skin; Buff's much deeper book usually still has listings).
  - exchange_rates.json: {ISO: units per USD} for ~50 currencies -- lets the USD dumps serve
    any Steam currency code this project supports (see steam_prices.CURRENCIES).

Freshness trade-off: the dumps update every few hours, so prices here can lag Steam by up to
roughly half a day -- slightly staler than a live priceoverview hit, but infinitely fresher
than the HTTP 429 a live hit actually returns now. The 24h TTL price cache in steam_prices.py
already assumed day-scale freshness anyway.
"""
import gzip
import json
import threading
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from . import net

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"  # regenerable/disposable

BASE_URL = "https://prices.csgotrader.app/latest"
FEED_TTL_SECONDS = 3 * 3600  # the dumps regenerate every few hours; re-fetching faster than
# this just re-downloads identical data (steam.json is ~0.5 MB compressed / ~4 MB parsed)
FAILURE_COOLDOWN_SECONDS = 120  # after a failed fetch, serve stale data (any age) and don't
# re-attempt for this long -- same rationale as thirdparty_prices' old cooldown: get_price()
# calls this on every cache miss, so an outage must not turn every miss into a live attempt

USER_AGENT = "Mozilla/5.0 cs2-trade-calculator/1.0"

_FEEDS = {
    "steam": {"url": f"{BASE_URL}/steam.json", "file": CACHE_DIR / "csgotrader_steam.json"},
    "skinport": {"url": f"{BASE_URL}/skinport.json", "file": CACHE_DIR / "csgotrader_skinport.json"},
    "buff163": {"url": f"{BASE_URL}/buff163.json", "file": CACHE_DIR / "csgotrader_buff163.json"},
    "rates": {"url": f"{BASE_URL}/exchange_rates.json", "file": CACHE_DIR / "csgotrader_rates.json"},
}

_lock = threading.Lock()
_feeds: dict = {}  # feed name -> {"_fetched_at": epoch, "data": {...}} (mirrors the disk files)
_last_failure: dict = {}  # feed name -> epoch of last failed fetch attempt
_inflight: set = set()  # feeds a thread is CURRENTLY downloading -- other threads serve stale
# data immediately instead of each starting their own 60s download. Without this, anything
# that consults a feed on a hot concurrent path (the dashboard's 3s poll walking
# has_market_listing across every best-list output, several sim sons cache-missing at once)
# could stampede dozens of parallel multi-MB fetches the moment the TTL lapsed, drowning the
# whole server for minutes (observed live 2026-07-11).
_converted_maps: dict = {}  # (feed, currency) -> {"_from_fetch": epoch, "map": {name: price}}
# -- rebuilt only when the underlying feed actually re-fetched, not on every call

_stats_lock = threading.Lock()
_stats = {
    name + "_feed": {"last_attempt_at": None, "last_success_at": None, "last_error": None,
                     "consecutive_failures": 0}
    for name in _FEEDS
}


def get_stats() -> dict:
    """Per-feed fetch health plus current age/size, same shape thirdparty_prices.get_stats()
    uses so the dashboard can show 'mirror stale/unreachable' instead of a silent None price."""
    with _stats_lock:
        out = {k: dict(v) for k, v in _stats.items()}
    now = time.time()
    with _lock:
        for name in _FEEDS:
            entry = _feeds.get(name)
            out[name + "_feed"]["age_seconds"] = (now - entry["_fetched_at"]) if entry else None
            out[name + "_feed"]["n_items"] = len(entry["data"]) if entry else 0
    return out


def _record_attempt(feed: str):
    with _stats_lock:
        _stats[feed + "_feed"]["last_attempt_at"] = time.time()


def _record_success(feed: str):
    with _stats_lock:
        _stats[feed + "_feed"]["last_success_at"] = time.time()
        _stats[feed + "_feed"]["last_error"] = None
        _stats[feed + "_feed"]["consecutive_failures"] = 0


def _record_failure(feed: str, reason: str):
    with _stats_lock:
        _stats[feed + "_feed"]["last_error"] = reason
        _stats[feed + "_feed"]["consecutive_failures"] += 1


def _fetch(feed: str) -> dict | None:
    """One live download of a feed. CloudFront serves these gzip-encoded even without an
    Accept-Encoding header, so decompress explicitly (urllib never does it for you)."""
    _record_attempt(feed)
    req = Request(_FEEDS[feed]["url"], headers={"User-Agent": USER_AGENT})
    last_exception = None
    for _attempt in range(3):
        try:
            with urlopen(req, timeout=60, context=net.SSL_CONTEXT) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            data = json.loads(raw)
            if not isinstance(data, dict) or not data:
                _record_failure(feed, "unexpected response shape (not a non-empty JSON object)")
                return None
            _record_success(feed)
            return data
        except HTTPError as e:
            _record_failure(feed, f"HTTP {e.code}: {e.reason}")
            return None
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_exception = e
            time.sleep(1.0)
    _record_failure(feed, f"{type(last_exception).__name__}: {last_exception}")
    return None


def _load_feed(feed: str) -> dict | None:
    """Returns the feed's {name: entry} data dict, fetching/refreshing under the TTL and
    failure-cooldown rules. Stale data (past TTL, or any age during an outage) is still
    returned rather than nothing -- same stale-beats-empty policy steam_prices' own cache and
    thirdparty_prices' old skinport cache both follow. Caller must NOT hold _lock.

    A stale-but-present entry is refreshed on a BACKGROUND thread while this call returns the
    stale data immediately -- the calling request must never pay for someone else's live
    multi-MB download. (Measured live 2026-07-12: a dashboard poll request landed the moment
    the TTL lapsed and blocked for ~20s on this exact download, against a normal ~350ms.) Only
    a genuine first-ever fetch (no cached data at all, nothing else to return) blocks
    synchronously."""
    now = time.time()
    with _lock:
        entry = _feeds.get(feed)
        if entry is None:
            path = _FEEDS[feed]["file"]
            if path.exists():
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(entry, dict) and "data" in entry:
                        _feeds[feed] = entry
                    else:
                        entry = None
                except (json.JSONDecodeError, OSError):
                    entry = None
        fresh = entry is not None and (now - entry.get("_fetched_at", 0)) < FEED_TTL_SECONDS
        cooling_down = feed in _last_failure and (now - _last_failure[feed]) < FAILURE_COOLDOWN_SECONDS
        if fresh or cooling_down:
            return entry["data"] if entry else None
        if feed in _inflight:
            # Another thread is already refreshing this feed -- stale-beats-empty, and one
            # download at a time (see _inflight's comment above).
            return entry["data"] if entry else None
        _inflight.add(feed)
        if entry is not None:
            threading.Thread(target=_refresh_feed_background, args=(feed,), daemon=True).start()
            return entry["data"]

    # No cached data at all (first-ever fetch for this feed) -- there's nothing stale to hand
    # back, so this is the one path that still blocks the caller.
    fetched = _fetch(feed)  # deliberately outside _lock: a 60s download must not block
    # get_skinport_map()/get_steam_entry() callers that only need the already-loaded data
    with _lock:
        _inflight.discard(feed)
        if fetched is not None:
            entry = {"_fetched_at": time.time(), "data": fetched}
            _feeds[feed] = entry
            _last_failure.pop(feed, None)
            _persist_feed(feed, entry)
            return fetched
        _last_failure[feed] = time.time()
        return None


def _refresh_feed_background(feed: str) -> None:
    """The background-thread half of _load_feed's stale-entry path -- runs entirely off the
    request thread. _inflight already reserved this feed under _lock before the thread
    started, same single-flight contract _load_feed's synchronous path follows."""
    fetched = _fetch(feed)
    with _lock:
        _inflight.discard(feed)
        if fetched is not None:
            entry = {"_fetched_at": time.time(), "data": fetched}
            _feeds[feed] = entry
            _last_failure.pop(feed, None)
            _persist_feed(feed, entry)
        else:
            _last_failure[feed] = time.time()


def _persist_feed(feed: str, entry: dict) -> None:
    """Caller must hold _lock. Disk cache is an optimization -- a write failure is swallowed
    since the in-memory copy in _feeds is what every reader actually consults."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _FEEDS[feed]["file"].write_text(json.dumps(entry), encoding="utf-8")
    except OSError:
        pass


def rate_for(currency: int) -> float:
    """Public alias for _rate_for -- user_overrides.py converts user-entered override prices
    between currencies through the same rates feed every bulk price conversion here uses."""
    return _rate_for(currency)


def _rate_for(currency: int) -> float:
    """USD -> the given Steam currency code (steam_prices.CURRENCIES). Unknown code or a
    missing/unloadable rates feed falls back to 1.0 (i.e. prices stay USD) rather than
    failing the whole price lookup over a conversion nicety."""
    if currency == 1:
        return 1.0
    from . import steam_prices  # lazy: steam_prices lazily imports this module right back,
    # so a module-level import here would be a cycle at import time
    iso = steam_prices.CURRENCIES.get(currency, ("USD",))[0]
    rates = _load_feed("rates") or {}
    rate = rates.get(iso)
    return float(rate) if rate else 1.0


# Preference order across steam.json's averaging windows: the shortest window with data is
# the closest thing to "the current price"; the longer ones only matter for items that trade
# so rarely that the short windows are empty (which the volume-less bulk data can't flag --
# the window that answered is recorded in "source" instead).
_STEAM_WINDOWS = ("last_24h", "last_7d", "last_30d", "last_90d")


def get_steam_entry(market_hash_name: str, currency: int = 1) -> dict | None:
    """Steam Market price for one item from the mirror, shaped exactly like
    steam_prices.get_price()'s return ({"lowest_price", "median_price", "volume"}) so it can
    drop straight into the same price cache / price_history flow. None = the mirror has no
    price for this name (treat like Steam's own "success": false, i.e. no listings).

    Approximation, documented on purpose: the mirror provides one number per item (a recent
    MEDIAN SALE price), so lowest_price and median_price are both set to it. The old live
    endpoint distinguished the cheapest current listing from the recent median; profit math
    that uses lowest for costs and median for revenue sees the same number now. volume is
    None (the mirror doesn't carry it) -- dead_skin.py/simulate.py already treat a missing
    volume leniently by design. The extra "source" key is additive and only for diagnostics."""
    data = _load_feed("steam")
    if not data:
        return None
    entry = data.get(market_hash_name)
    if not isinstance(entry, dict):
        return None
    for window in _STEAM_WINDOWS:
        price = entry.get(window)
        if price is not None:
            converted = round(float(price) * _rate_for(currency), 2)
            return {"lowest_price": converted, "median_price": converted, "volume": None,
                    "source": f"csgotrader:{window}"}
    return None


def _extract_skinport_price(entry) -> float | None:
    price = entry.get("starting_at") if isinstance(entry, dict) else None
    return float(price) if price is not None else None


def _extract_buff163_price(entry) -> float | None:
    # buff163.json nests one level deeper than skinport.json: starting_at is itself a dict
    # ({"price": 29.71, optionally "doppler": {...}}). The plain "price" is the cheapest
    # standard listing -- Doppler-phase sub-prices are ignored, same wear-bracket-level
    # granularity every other source in this project uses.
    if not isinstance(entry, dict):
        return None
    starting = entry.get("starting_at")
    price = starting.get("price") if isinstance(starting, dict) else None
    return float(price) if price is not None else None


def _listing_map(feed: str, currency: int, extract) -> dict:
    """Shared implementation for get_skinport_map()/get_buff163_map(): the whole catalog's
    {market_hash_name: cheapest current listing price} in the given Steam currency code.
    Empty dict when the feed has never been fetchable. The converted map is rebuilt only when
    the underlying feed actually re-fetched, not on every call (39k-item dict comprehension,
    cheap but not free on a hot path)."""
    data = _load_feed(feed)
    if not data:
        return {}
    with _lock:
        fetched_at = _feeds[feed]["_fetched_at"]
        cached = _converted_maps.get((feed, currency))
        if cached and cached["_from_fetch"] == fetched_at:
            return cached["map"]
    rate = _rate_for(currency)
    converted = {}
    for name, entry in data.items():
        price = extract(entry)
        if price is not None:
            converted[name] = round(price * rate, 2)
    with _lock:
        _converted_maps[(feed, currency)] = {"_from_fetch": fetched_at, "map": converted}
    return converted


def get_skinport_map(currency: int = 1) -> dict:
    """{market_hash_name: cheapest current Skinport listing price} -- what
    thirdparty_prices._load_skinport_bulk() serves instead of the Cloudflare-blocked live
    endpoint. See _listing_map for the caching behavior."""
    return _listing_map("skinport", currency, _extract_skinport_price)


def get_buff163_map(currency: int = 1) -> dict:
    """{market_hash_name: cheapest current Buff163 listing price} -- the second independent
    third-party source alongside Skinport (see the module docstring). Same shape/caching as
    get_skinport_map."""
    return _listing_map("buff163", currency, _extract_buff163_price)


def ensure_feeds_loaded() -> bool:
    """Force-load (fetch/refresh under the usual TTL/cooldown rules) the steam feed
    has_market_listing() reads, returning whether it's actually available. Callers that
    iterate the whole catalog (tradability.unobtainable_skin_names) call this once up front
    instead of letting the very first per-name lookup decide feed availability."""
    return bool(_load_feed("steam"))


def has_market_listing(market_hash_name: str) -> "bool | None":
    """Market-existence oracle: does this exact (non-souvenir, wear-suffixed, possibly
    StatTrak-prefixed) market_hash_name show evidence of really being buyable -- a Steam
    median-sale price in ANY of the mirror's averaging windows (24h through 90d)?

    Used by tradability.py to detect skins that can't be trade-up inputs because there's no
    non-souvenir copy to buy (e.g. the feed carries "AWP | Dragon Lore (Field-Tested)" as a
    catalog entry with every price window null -- the only Dragon Lores that actually trade
    are "Souvenir AWP | Dragon Lore (...)", and souvenir items can't enter a contract).

    DELIBERATELY steam-feed-only: the mirror's skinport.json/buff163.json dumps list
    souvenir-only items under the PLAIN name too (verified 2026-07-10: both price
    "AWP | Dragon Lore (Field-Tested)" at the souvenir item's real ~$6.5k price, alongside a
    separate "Souvenir ..." key), so consulting them here would false-positive exactly the
    skins this oracle exists to catch. Steam's feed keys souvenirs correctly.

    Returns None (unknown, treat as "don't filter") when the steam feed has never been
    fetchable at all -- an outage must degrade to no filtering, never to "everything is
    untradeable"."""
    steam = _load_feed("steam")
    if not steam:
        return None
    entry = steam.get(market_hash_name)
    return isinstance(entry, dict) and any(entry.get(w) is not None for w in _STEAM_WINDOWS)
