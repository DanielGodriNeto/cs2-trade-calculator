"""Cross-checks Steam Market prices against a second, independent marketplace, to flag skins
where Steam's price looks anomalously high (a "drought" -- temporary low Steam supply inflating
the price there specifically) vs. what the same skin is actually selling for elsewhere. This is
a screening signal only: it never changes expected_revenue/profit/roi_pct anywhere in the
codebase, it just adds an annotation the user should verify manually -- same spirit as
oddsmath.py's WEAR_MIDPOINT approximation note.

Source coverage (as of 2026-07 -- verify before relying on this at runtime, marketplace
APIs change without notice):
  - Skinport: served from the csgotrader.app bulk mirror (bulk_prices.py) since 2026-07. The
    old direct https://api.skinport.com/v1/items fetch is GONE: that endpoint now sits behind
    Cloudflare Bot Management with a JS challenge (verified live 2026-07-10 -- HTTP 403 to any
    non-browser client, from a clean residential IP, regardless of headers/Accept-Encoding).
    The mirror carries the same starting_at prices, refreshed every few hours.
  - Buff163: served from the same csgotrader.app bulk mirror (bulk_prices.get_buff163_map)
    since 2026-07 -- a SECOND independent source, enabled by default alongside Skinport.
    Buff163 is the deepest third-party CS2 marketplace, so it frequently has a listing for
    cheap trade-up fodder that Skinport doesn't carry -- exactly the gap that used to make
    "no 3rd-party match" so common.
  - CSFloat: has a listings-search endpoint that has historically allowed limited
    unauthenticated use, gated behind an optional CSFLOAT_API_KEY (env var or configure_csfloat_key())
    here -- skipped entirely (not an error) when unset.
  - DMarket / BitSkins: both require an account-issued API key for market-data reads even
    read-only, unlike Skinport/CSFloat -- not implemented in this phase, stubbed below so a
    future phase can fill them in without redesigning the calling code.
"""
import json
import os
import threading
import time
import urllib.parse
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from . import net

CSFLOAT_LISTINGS_URL = "https://csfloat.com/api/v1/listings"
CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY")  # unset = CSFloat checks silently skipped, never an error

# Diagnostics: without this, a fetch that's silently failing (e.g. blocked by Skinport's
# Cloudflare bot-protection, which returns a 403 challenge page instead of JSON) looks
# IDENTICAL to "checked, genuinely no listings" -- both just produce a None price. This tracks
# what actually happened on each attempt so the dashboard/CLI can show the real reason, the
# same way steam_prices.py's get_stats() already does for the Steam side.
_stats_lock = threading.Lock()
_stats = {
    "skinport": {"last_attempt_at": None, "last_success_at": None, "last_error": None, "consecutive_failures": 0},
    "buff163": {"last_attempt_at": None, "last_success_at": None, "last_error": None, "consecutive_failures": 0},
    "csfloat": {"last_attempt_at": None, "last_success_at": None, "last_error": None, "consecutive_failures": 0},
}


def _record_attempt(source: str):
    with _stats_lock:
        _stats[source]["last_attempt_at"] = time.time()


def _record_success(source: str):
    with _stats_lock:
        _stats[source]["last_success_at"] = time.time()
        _stats[source]["last_error"] = None
        _stats[source]["consecutive_failures"] = 0


def _record_failure(source: str, reason: str):
    with _stats_lock:
        _stats[source]["last_error"] = reason
        _stats[source]["consecutive_failures"] += 1


def get_stats() -> dict:
    """{"skinport": {...}, "csfloat": {...}} -- last_attempt_at/last_success_at/last_error/
    consecutive_failures per source, plus csfloat.configured (whether a key is set) and
    skinport.cached_currencies (age/size of the mirror feed backing every currency). The
    skinport fields mirror bulk_prices.get_stats()["skinport_feed"] -- fetch attempts happen
    there now (see module docstring), this module no longer talks to Skinport directly."""
    with _stats_lock:
        out = {k: dict(v) for k, v in _stats.items()}
    out["csfloat"]["configured"] = CSFLOAT_API_KEY is not None
    from . import bulk_prices  # lazy: matches this module's original import-light shape
    bulk_stats = bulk_prices.get_stats()
    feed = bulk_stats["skinport_feed"]
    for field in ("last_attempt_at", "last_success_at", "last_error", "consecutive_failures"):
        out["skinport"][field] = feed[field]
    # Buff163 comes from the same mirror -- mirror its feed health the same way skinport's is.
    buff_feed = bulk_stats["buff163_feed"]
    for field in ("last_attempt_at", "last_success_at", "last_error", "consecutive_failures"):
        out["buff163"][field] = buff_feed[field]
    out["buff163"]["n_items"] = buff_feed["n_items"]
    # One shared USD feed converts to every currency now, so cached_currencies (kept for the
    # dashboard's existing display) reports that single feed's age/size under "all".
    out["skinport"]["cached_currencies"] = (
        {"all": {"age_seconds": feed["age_seconds"], "n_items": feed["n_items"]}}
        if feed["n_items"] else {})
    return out


def configure_csfloat_key(key: str | None):
    global CSFLOAT_API_KEY
    CSFLOAT_API_KEY = key or None


def _load_skinport_bulk(currency_code: int, abort_check=None) -> dict:
    """{market_hash_name: cheapest current Skinport listing price} for the whole catalog in
    the given Steam currency code, served entirely by the csgotrader mirror (see module
    docstring -- Skinport's own endpoint is Cloudflare-blocked to non-browsers now).
    bulk_prices owns the TTL/failure-cooldown/disk-cache behavior this module used to
    implement itself; abort_check is accepted for call-site compatibility but a mirror hit
    is a single cheap dict lookup, so there's nothing long-running left to abort."""
    from . import bulk_prices  # lazy: matches this module's original import-light shape
    return bulk_prices.get_skinport_map(currency_code)


# CSFloat's `category` query param on GET /v1/listings: 0 = any, 1 = normal, 2 = StatTrak, 3 =
# souvenir (per CSFloat's own API docs). Combined with market_hash_name (which already carries
# the "StatTrak(TM) " prefix) rather than instead of it, as an extra server-side filter.
CSFLOAT_CATEGORY_NORMAL = 1
CSFLOAT_CATEGORY_STATTRAK = 2


def _get_csfloat_price(market_hash_name: str, stattrak: bool, abort_check=None,
                        min_float: float | None = None, max_float: float | None = None) -> float | None:
    """Cheapest current CSFloat *buy-now* listing price for this exact market_hash_name, or
    None if CSFLOAT_API_KEY isn't configured, the lookup fails, or there's no matching listing.
    Not attempted (and not counted as a failure) when no key is configured -- get_stats()'s
    csfloat.configured field is what distinguishes "never tried" from "tried and failed".

    min_float/max_float (both optional) narrow the search to listings within that float
    sub-range instead of any listing matching the wear-bracket name -- useful when verifying a
    specific float-mix target rather than just "any copy of this wear condition"."""
    if not CSFLOAT_API_KEY:
        return None
    if abort_check and abort_check():
        return None
    params = {
        "market_hash_name": market_hash_name,
        "sort_by": "lowest_price",
        "limit": 1,
        "category": CSFLOAT_CATEGORY_STATTRAK if stattrak else CSFLOAT_CATEGORY_NORMAL,
        "type": "buy_now",  # exclude auctions -- a current bid isn't a price comparable to
        # Steam's/Skinport's listed price the way a fixed buy-now price is
    }
    if min_float is not None:
        params["min_float"] = min_float
    if max_float is not None:
        params["max_float"] = max_float
    url = f"{CSFLOAT_LISTINGS_URL}?{urllib.parse.urlencode(params)}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 cs2-trade-calculator/1.0",
                                 "Authorization": CSFLOAT_API_KEY})
    _record_attempt("csfloat")
    try:
        with urlopen(req, timeout=15, context=net.SSL_CONTEXT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        _record_failure("csfloat", f"HTTP {e.code}: {e.reason}")
        return None
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        _record_failure("csfloat", f"{type(e).__name__}: {e}")
        return None
    listings = body if isinstance(body, list) else body.get("data", [])
    if not listings:
        _record_success("csfloat")  # a real, successful response -- just no listings for this item
        return None
    price_cents = listings[0].get("price")
    _record_success("csfloat")
    return price_cents / 100 if price_cents is not None else None


def get_thirdparty_price(skin_name: str, wear: str, stattrak: bool, currency: int,
                          abort_check=None, min_float: float | None = None,
                          max_float: float | None = None) -> dict:
    """Returns {"skinport": float|None, "buff163": float|None, "csfloat": float|None} -- keys
    are only present for sources that were actually checked (CSFloat is absent entirely, not
    None, when no key is configured), so callers can distinguish "didn't check" from
    "checked, found nothing".

    min_float/max_float let a caller narrow the CSFloat side to a specific float sub-range (e.g.
    a float-mix target); when omitted, this defaults to the standard min/max of `wear`'s own
    bracket (see oddsmath.WEAR_BREAKPOINTS) so the CSFloat lookup is verifying the same float
    range the Steam/Skinport price is for, not just trusting the market_hash_name's "(wear)"
    suffix to match on its own."""
    prefix = "StatTrak™ " if stattrak else ""
    full_name = f"{prefix}{skin_name} ({wear})"
    out = {}

    skinport_prices = _load_skinport_bulk(currency, abort_check)
    out["skinport"] = skinport_prices.get(full_name)

    from . import bulk_prices  # lazy: matches this module's original import-light shape
    out["buff163"] = bulk_prices.get_buff163_map(currency).get(full_name)

    if CSFLOAT_API_KEY:
        if min_float is None and max_float is None:
            from . import oddsmath
            bracket = next((b for b in oddsmath.WEAR_BREAKPOINTS if b[0] == wear), None)
            if bracket:
                min_float, max_float = bracket[1], bracket[2]
        out["csfloat"] = _get_csfloat_price(full_name, stattrak, abort_check, min_float, max_float)

    return out


def drought_flag(steam_price: float, thirdparty_prices: dict, threshold_pct: float = 15.0) -> dict | None:
    """Compares steam_price against the cheapest available third-party price. Returns None if
    no third-party data was available to compare (never flags on missing data), else
    {gap_pct, cheapest_thirdparty_price, cheapest_source, is_drought} -- is_drought is True when
    Steam is priced threshold_pct or more above the cheapest verified alternative."""
    candidates = [(src, price) for src, price in thirdparty_prices.items() if price is not None]
    if not candidates or not steam_price:
        return None
    cheapest_source, cheapest_price = min(candidates, key=lambda kv: kv[1])
    if cheapest_price <= 0:
        return None
    gap_pct = (steam_price - cheapest_price) / cheapest_price * 100
    return {
        "gap_pct": gap_pct,
        "cheapest_thirdparty_price": cheapest_price,
        "cheapest_source": cheapest_source,
        "is_drought": gap_pct >= threshold_pct,
    }


def get_dmarket_price(*args, **kwargs):
    raise NotImplementedError(
        "DMarket's market-data endpoints require an account-issued public key even for "
        "read-only price search -- not a genuinely no-auth API, so this isn't wired up yet. "
        "A future phase can implement this the same shape as _get_csfloat_price() once a "
        "DMarket key is configured.")


def get_bitskins_price(*args, **kwargs):
    raise NotImplementedError(
        "BitSkins' current API requires an API key for all endpoints (no anonymous tier) -- "
        "not implemented yet. A future phase can implement this the same shape as "
        "_get_csfloat_price() once a BitSkins key is configured.")
