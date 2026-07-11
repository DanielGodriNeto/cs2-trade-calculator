"""Steam Community Market prices. Since 2026-07 the PRIMARY source is the csgotrader.app
bulk mirror (see bulk_prices.py) -- real Steam median-sale prices, one free bulk download,
no login -- and the old live priceoverview fetch is an opt-in FALLBACK, because Steam now
answers essentially every anonymous priceoverview request with HTTP 429 regardless of how
politely it's throttled (verified live 2026-07-10: first request after minutes of silence,
clean residential IP, regular Steam pages loading fine). Re-enable the live path with
configure_steam_live_fallback(True) only if that policy changes or requests get a session
cookie; the adaptive throttle below then applies.

This only reads public price data -- it does not place orders or touch any account.

Historical note: this docstring used to blame an intermittent Steam CDN cert chain for
"Basic Constraints of CA cert not marked critical" failures. That was actually an
HTTPS-scanning antivirus (Avast) re-signing every connection with a cert that Python 3.13+'s
strict validation rejects -- 100% of requests, not intermittent. See net.py, whose shared
context (strict flag cleared, full verification kept) every request here now uses.
"""
import json
import random
import re
import threading
import time
import urllib.parse
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

from . import net

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
PRICE_CACHE_FILE = CACHE_DIR / "price_cache.json"
PRICE_TTL_SECONDS = 86400  # 24h -- long enough that a skin/wear looked up once doesn't get
# re-fetched again and again while the guided-simulation loop (or a scan) keeps touching it;
# past this age an entry is purged outright (see _purge_expired), not just treated as stale, so
# a lookup that's actually needed again goes back to a real live fetch instead of quietly
# reusing day-old-plus data forever
MIN_REQUEST_INTERVAL = 3.5  # floor: seconds between live requests when things are going well.
# Steam's anonymous budget was ~20/min (3s) even before the endpoint effectively closed to
# anonymous callers -- the old 1.5s floor ran at double that and guaranteed 429 storms.
MAX_REQUEST_INTERVAL = 60.0  # ceiling the adaptive interval backs off to under sustained 429s
INTERVAL_BACKOFF_FACTOR = 1.6  # widen the interval by this much on each 429 (throttle harder)
INTERVAL_DECAY_FACTOR = 0.97  # narrow it back toward the floor by this much on each success --
# slow on purpose: the old 0.9 slid back under Steam's real limit within ~17 successes, so the
# steady state was a permanent trip/back-off/trip oscillation
RATE_LIMIT_BACKOFF_SECONDS = 60  # base cooldown after a 429; Steam's block windows run minutes,
# so the old 20s base just burned the remaining retries into a wall that hadn't lifted
MAX_RATE_LIMIT_BACKOFF_SECONDS = 300  # cap for the exponential backoff during a 429 streak
RATE_LIMIT_JITTER_SECONDS = 5  # randomized so many collections' requests don't re-sync into lockstep
APPID_CS2 = 730

# The live per-item priceoverview fetch is OFF by default -- Steam 429s anonymous callers
# unconditionally now (see module docstring), so by default a bulk-mirror miss is treated as
# "no price" instead of burning minutes of backoff on a request that cannot succeed.
STEAM_LIVE_FALLBACK = False


def configure_steam_live_fallback(enabled: bool) -> None:
    """Turns the live per-item Steam fetch back on as a fallback for items the bulk mirror
    doesn't carry. Only useful if Steam's anonymous-429 policy changes; everything else
    (throttle, backoff, caching) picks up where it left off."""
    global STEAM_LIVE_FALLBACK
    STEAM_LIVE_FALLBACK = bool(enabled)

# Steam Community Market currency codes (undocumented but stable, community-verified).
# code -> (ISO name, display symbol). Not exhaustive -- just the common ones.
CURRENCIES = {
    1: ("USD", "$"),
    2: ("GBP", "£"),
    3: ("EUR", "€"),
    5: ("RUB", "pуб"),
    6: ("PLN", "zł"),
    7: ("BRL", "R$"),
    8: ("JPY", "¥"),
    9: ("NOK", "kr"),
    12: ("PHP", "₱"),
    13: ("SGD", "S$"),
    14: ("THB", "฿"),
    16: ("KRW", "₩"),
    17: ("TRY", "₺"),
    18: ("UAH", "₴"),
    19: ("MXN", "MX$"),
    20: ("CAD", "CA$"),
    21: ("AUD", "A$"),
    22: ("NZD", "NZ$"),
    23: ("CNY", "¥"),
    24: ("INR", "₹"),
    28: ("ZAR", "R"),
    29: ("HKD", "HK$"),
    31: ("SAR", "SR"),
    34: ("ARS", "ARS$"),
    35: ("ILS", "₪"),
}

_cache: dict = {}
_last_request_time = 0.0
_current_interval = MIN_REQUEST_INTERVAL  # adaptive: widens on 429s, decays back on successes

# Serializes the "have we waited long enough since the last live request" check + the request
# itself. sim_loop.py's population sons now run as real concurrent daemon threads (one per son,
# see SimulationLoopState/run_foreground), so without this lock two sons' threads could both
# read _last_request_time as "stale enough" before either updates it, firing near-simultaneous
# requests and tripping Steam's 429 limiter far more than MIN_REQUEST_INTERVAL should ever allow
# -- see get_price(). Only guards live network calls; get_cached_price() (simulate.py's normal,
# no-network path) never touches this.
_request_lock = threading.Lock()

# Live network health, so the dashboard can show whether it's making real progress or stuck
# (e.g. Steam rate-limiting/blocking us) rather than just looking frozen with no explanation.
_stats_lock = threading.Lock()
_stats = {
    "requests_ok": 0,  # successful price fetches over the network -- includes bulk-mirror
    # fills (see bulk_fills below), which also refresh last_success_at, so the dashboard's
    # "prices are flowing" indicators stay truthful with the live Steam path disabled
    "requests_rate_limited": 0,
    "requests_error": 0,
    "bulk_fills": 0,  # cache misses answered by the csgotrader bulk mirror (bulk_prices.py) --
    # the default source now; a live per-item Steam request only ever happens when the mirror
    # misses AND STEAM_LIVE_FALLBACK was explicitly enabled
    "cache_hits": 0,
    "cache_reads_fresh": 0,    # get_cached_price() found a usable, non-expired entry -- the
    # "no Steam call needed at all" path simulate.py's _price() takes first, on every input/
    # output unit. NOT incremented by get_price()'s own (separate, rarely-hit) cache_hits check.
    "cache_reads_stale": 0,    # get_cached_price() found an entry past PRICE_TTL_SECONDS that
    # hadn't been purged yet -- still usable (simulate.py treats "stale" as a usable price, just
    # flags cost_basis_source), so this is also a no-network read.
    "cache_reads_missing": 0,  # get_cached_price() found nothing -- the only case that can fall
    # through to a real live get_price() call (see simulate._price()). cache_reads_fresh +
    # cache_reads_stale vs. cache_reads_missing is the actual "how warm is the cache" signal for
    # simulate.py's normal call path -- requests_ok/rate_limited/error below only count what
    # happens AFTER a miss already forced a live fetch.
    "last_success_at": None,
    "last_rate_limited_at": None,
    "backoff_until": None,  # epoch time the current 429 backoff wait will end, or None if not waiting
    "rate_limit_streak_start": None,  # epoch time the CURRENT unbroken run of 429s began, or None
    "consecutive_rate_limits": 0,  # resets to 0 on any success; drives the exponential backoff
    "current_interval": MIN_REQUEST_INTERVAL,  # adaptive per-request spacing, surfaced for the dashboard
    "by_label": {},  # label (e.g. "son_0".."son_3", "price_scan") -> {stat_key: count} -- see
    # set_attribution(). Only keys that have actually happened for that label are present.
}

# Thread-local call attribution: each population son (sim_loop.py) runs its own dedicated
# background thread for its entire lifetime, so tagging "the current thread" with a label is
# enough to get a clean per-son breakdown in _stats["by_label"] even while all 4 sons hit this
# module concurrently -- no cross-son attribution race, no need to pass a label through every
# call site in simulate.py/random_trade.py.
_thread_local = threading.local()


def set_attribution(label: str | None) -> None:
    """Tags every get_cached_price()/get_price() call made by THE CURRENT THREAD from now on
    with `label`, bucketed separately in get_stats()["by_label"][label] -- see module-level
    _thread_local docstring. Call once near the top of a son's run_round() (sim_loop.py) or a
    scan's run loop (price_scan.py); None clears the tag (calls go unlabeled, e.g. ad-hoc CLI/
    dashboard "verify one price" lookups). Purely additive bookkeeping -- never changes what
    get_price()/get_cached_price() actually do."""
    _thread_local.label = label


def get_stats() -> dict:
    if not _cache:
        _load_cache()
    with _stats_lock:
        stats = dict(_stats)
    # How many distinct skin+wear+StatTrak+currency combos currently have a live price cached --
    # the dashboard top bar's "skins catalogued" figure. Computed fresh each call (not stored in
    # _stats) since _cache's size changes on every fetch; cheap (one len() on an already-loaded
    # dict, no disk read once _load_cache has run once).
    stats["cache_size"] = len(_cache)
    # Additive: fetch health of the csgotrader bulk mirror (the primary source now), so
    # "mirror stale/unreachable" is visible instead of just a mysterious run of None prices.
    from . import bulk_prices
    stats["bulk_mirror"] = bulk_prices.get_stats()
    stats["steam_live_fallback"] = STEAM_LIVE_FALLBACK
    return stats


def _bump_label(key: str) -> None:
    """Caller must already hold _stats_lock. No-op if the current thread never called
    set_attribution() (the common case for anything outside sim_loop.py/price_scan.py)."""
    label = getattr(_thread_local, "label", None)
    if label:
        bucket = _stats["by_label"].setdefault(label, {})
        bucket[key] = bucket.get(key, 0) + 1


def _record(increment: str | None = None, **updates):
    with _stats_lock:
        if increment:
            _stats[increment] += 1
            _bump_label(increment)
        _stats.update(updates)


def _mark_rate_limited(retry_after: float | None = None) -> float:
    """Records a 429 and returns how long to back off for. Backs off exponentially with each
    consecutive 429 (capped at MAX_RATE_LIMIT_BACKOFF_SECONDS) rather than always waiting the
    same fixed amount -- a single 429 is probably a brief blip, but a run of them means we're
    still inside Steam's throttle window and waiting longer each time clears it faster than
    repeatedly retrying too soon. Jittered so many collections' requests don't resync into
    lockstep and all get rate-limited together again. Honors the server's own Retry-After
    header when present instead of guessing, since Steam knows its own cooldown better than we do."""
    global _current_interval
    with _stats_lock:
        _stats["requests_rate_limited"] += 1
        _bump_label("requests_rate_limited")
        _stats["consecutive_rate_limits"] += 1
        streak = _stats["consecutive_rate_limits"]
        now = time.time()
        _stats["last_rate_limited_at"] = now
        if retry_after is not None:
            backoff = retry_after
        else:
            backoff = min(RATE_LIMIT_BACKOFF_SECONDS * (2 ** (streak - 1)), MAX_RATE_LIMIT_BACKOFF_SECONDS)
            backoff += random.uniform(0, RATE_LIMIT_JITTER_SECONDS)
        _stats["backoff_until"] = now + backoff
        if _stats["rate_limit_streak_start"] is None:
            _stats["rate_limit_streak_start"] = now  # first 429 since the last success
        _current_interval = min(_current_interval * INTERVAL_BACKOFF_FACTOR, MAX_REQUEST_INTERVAL)
        _stats["current_interval"] = _current_interval
    return backoff


def _mark_success():
    global _current_interval
    with _stats_lock:
        _stats["requests_ok"] += 1
        _bump_label("requests_ok")
        _stats["last_success_at"] = time.time()
        _stats["rate_limit_streak_start"] = None  # streak broken -- Steam is answering again
        _stats["consecutive_rate_limits"] = 0
        _current_interval = max(_current_interval * INTERVAL_DECAY_FACTOR, MIN_REQUEST_INTERVAL)
        _stats["current_interval"] = _current_interval


def _load_cache():
    global _cache
    if PRICE_CACHE_FILE.exists():
        try:
            _cache = json.loads(PRICE_CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _cache = {}
    else:
        _cache = {}
    _purge_expired()  # drop anything that already aged out while this file sat untouched


_dirty_writes = 0
SAVE_EVERY_N_WRITES = 200  # batch disk writes -- serializing/writing the whole cache on every
# single price lookup is wasted I/O once it has hundreds/thousands of entries. Was 10 back
# when every write followed a >=MIN_REQUEST_INTERVAL network wait; bulk-mirror fills are
# instant dict lookups now, so at 10 a full 34k-item scan spent most of its time re-dumping
# an ever-growing multi-MB JSON file. Worst case on a crash is ~200 lost lookups, all
# re-answerable from the mirror in microseconds -- flush_cache() still covers clean stops.


def _purge_expired():
    """Removes every cache entry older than PRICE_TTL_SECONDS outright, in-memory and (via the
    next disk write) on disk too -- a skin/wear's price is kept for 24h so nothing re-fetches it
    over and over in that window, but past that age it's actually deleted rather than lingering
    forever as stale data. A later lookup for that same skin/wear is just a normal cache miss at
    that point, which naturally goes back to a real live fetch (get_price) -- see the module
    docstring. Cheap enough (one dict comprehension) to run on every disk write rather than on
    some separate schedule."""
    global _cache
    now = time.time()
    _cache = {k: v for k, v in _cache.items() if now - v.get("_fetched_at", 0) < PRICE_TTL_SECONDS}


def _write_cache_to_disk():
    _purge_expired()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PRICE_CACHE_FILE.write_text(json.dumps(_cache), encoding="utf-8")


def _save_cache():
    global _dirty_writes
    _dirty_writes += 1
    if _dirty_writes >= SAVE_EVERY_N_WRITES:
        _dirty_writes = 0
        _write_cache_to_disk()


def flush_cache():
    """Force any pending batched writes to disk right now. Call this at safe checkpoints
    (e.g. the end of a scan phase, or before pausing/stopping) so recent successful lookups
    aren't lost if the process exits before the next batch threshold is reached."""
    global _dirty_writes
    if _dirty_writes:
        _dirty_writes = 0
        _write_cache_to_disk()


def clear_cache() -> int:
    """Wipes every cached skin/wear price, in-memory and on disk. This is a re-fetchable
    market snapshot, not simulated/outcome data -- the next lookup for any skin just goes
    back to the bulk mirror / a live fetch, same as a cold start. Returns how many entries
    were removed. Used by the dashboard's global 'Reset all data' button."""
    global _cache, _dirty_writes
    n = len(_cache)
    _cache = {}
    _dirty_writes = 0
    if PRICE_CACHE_FILE.exists():
        PRICE_CACHE_FILE.unlink()
    return n


def _parse_money(s: str | None) -> float | None:
    """Parses Steam's localized price strings across currencies/formats, e.g.
    "$41.51" (USD), "R$ 123,45" (BRL), "1.234,56 €" (EUR-style), "¥1,234" (JPY --
    whole yen, comma is a thousands separator, not a decimal point), or
    "1 234,56" (space as thousands separator, used by some locales)."""
    if not s:
        return None
    m = re.search(r"[\d.,\s ]+", s)
    if not m:
        return None
    num = m.group(0).strip().replace(" ", "").replace(" ", "")
    if not num:
        return None
    if num.count(",") and num.count("."):
        # Both separators present: whichever comes last is the decimal point, the other is
        # a thousands separator (e.g. "1,234.56" vs "1.234,56").
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif num.count(","):
        groups = num.split(",")
        # 2+ commas can only be thousands separators (a number never has two decimal
        # points). A single comma followed by exactly 3 digits is also almost always a
        # thousands separator (e.g. JPY/KRW "1,234" = 1234, not 1.234) -- no currency we
        # support has 3 decimal digits, so that case is unambiguous too.
        if len(groups) > 2 or len(groups[-1]) == 3:
            num = num.replace(",", "")
        else:
            num = num.replace(",", ".")
    elif num.count("."):
        groups = num.split(".")
        if len(groups) > 2 or len(groups[-1]) == 3:
            num = num.replace(".", "")
        # else: leave as-is, a single "." with 1-2 trailing digits is a normal decimal point
    try:
        return float(num)
    except ValueError:
        return None


def _interruptible_sleep(seconds: float, abort_check=None):
    """Sleeps in short slices so a scope/config change can interrupt a long wait (e.g. the
    429 backoff) almost immediately instead of only between whole price requests."""
    if not abort_check:
        time.sleep(seconds)
        return
    end = time.time() + seconds
    while time.time() < end:
        if abort_check():
            return
        time.sleep(min(0.5, end - time.time()))


def get_price(market_hash_name: str, currency: int = 1, abort_check=None) -> dict | None:
    """Returns {"lowest_price": float|None, "median_price": float|None, "volume": int|None}.
    A user price override (user_overrides.py) wins over EVERY other source; otherwise served
    from the 24h disk cache, then the csgotrader bulk mirror (bulk_prices.py -- the
    default live source now), then, ONLY if configure_steam_live_fallback(True) was called,
    the original per-item Steam priceoverview request (_live_steam_fetch)."""
    # User override first: the whole point of a manual price is that it beats whatever the
    # market data says (bugged mirror entries, souvenir conflation, ...). Never cached, never
    # recorded in price_history -- it's the user's own number, not an observation.
    from . import user_overrides
    override = user_overrides.get_override_entry(market_hash_name, currency)
    if override is not None:
        return override

    if not _cache:
        _load_cache()

    key = f"{currency}:{market_hash_name}"
    cached = _cache.get(key)
    if cached and (time.time() - cached.get("_fetched_at", 0)) < PRICE_TTL_SECONDS:
        _record(increment="cache_hits")
        return cached["data"]

    # Everything from here down -- the "have we waited long enough" check through the actual
    # request and its cache write -- is serialized by _request_lock (see its module-level
    # docstring): population sons now run as real concurrent threads, so without this lock two
    # threads could both see "elapsed >= _current_interval" and fire near-simultaneous requests,
    # defeating the whole point of the adaptive spacing and tripping Steam's 429 limiter harder
    # than a single-threaded caller ever would.
    with _request_lock:
        # Re-check the cache now that we actually hold the lock -- another thread queued behind
        # us may have just fetched this SAME key while we were waiting, in which case reuse its
        # result instead of spending a second live request on it.
        cached = _cache.get(key)
        if cached and (time.time() - cached.get("_fetched_at", 0)) < PRICE_TTL_SECONDS:
            _record(increment="cache_hits")
            return cached["data"]

        # PRIMARY source: the csgotrader bulk mirror (see module docstring / bulk_prices.py).
        # Lazy import to match the module's existing keep-startup-light convention (and
        # bulk_prices lazily imports CURRENCIES from here, so eager would be a cycle).
        from . import bulk_prices
        data = bulk_prices.get_steam_entry(market_hash_name, currency)
        if data is not None:
            _record(increment="bulk_fills")
            _mark_success()
            _cache[key] = {"_fetched_at": time.time(), "data": data}
            _save_cache()
        elif not STEAM_LIVE_FALLBACK:
            # Mirror is loaded but doesn't carry this name -> treat exactly like Steam's own
            # "success": false (genuinely no listings): cache the None so get_cached_price()
            # answers "fresh, no price" for the next 24h instead of every simulate round
            # re-triggering this same fallback for the same unpriceable item. BUT if the
            # mirror itself is unreachable (never fetched), cache nothing -- that's an
            # outage, not information about this item, and it must not masquerade as
            # "confirmed no price" for a day (same reasoning as _live_steam_fetch's own
            # transient-error paths).
            if bulk_prices.get_stats()["steam_feed"]["n_items"]:
                _cache[key] = {"_fetched_at": time.time(), "data": None}
                _save_cache()
            else:
                _record(increment="requests_error")
            return None
        else:
            data = _live_steam_fetch(market_hash_name, currency, key, abort_check)
            if data is _LIVE_FETCH_FAILED:
                return None

    if data:
        # Record one price_history.py snapshot per genuinely fresh fetch (never on a cache
        # hit, above -- that's not new information). Lazy import: price_history.py is a leaf
        # module with no dependencies of its own, but importing it eagerly at module load
        # would be one more thing that could theoretically fail before steam_prices.py (used
        # by literally every scan) even starts up -- keep this side effect optional and late.
        from . import price_history
        price_history.record_snapshot(market_hash_name, currency, data)

    return data


_LIVE_FETCH_FAILED = object()  # sentinel: transient failure, nothing cached -- distinct from
# a real None price, which IS cached ("confirmed no listings")


def _live_steam_fetch(market_hash_name: str, currency: int, key: str, abort_check=None):
    """The original live per-item priceoverview request -- now only reachable when the bulk
    mirror misses an item AND configure_steam_live_fallback(True) was called. Caller must
    hold _request_lock. Returns the data dict (cached here), None for a real "no listings"
    response (also cached), or _LIVE_FETCH_FAILED for transient errors (never cached)."""
    global _last_request_time

    # Honor any still-running 429 backoff BEFORE firing, whichever get_price() call incurred
    # it -- previously the backoff was only slept inside one item's retry loop, so the very
    # next item's request went out after at most MAX_REQUEST_INTERVAL and kept poking Steam
    # mid-block (which can extend the block).
    with _stats_lock:
        backoff_until = _stats["backoff_until"]
    if backoff_until:
        remaining = backoff_until - time.time()
        if remaining > 0:
            _interruptible_sleep(remaining, abort_check)
        _record(backoff_until=None)

    elapsed = time.time() - _last_request_time
    if elapsed < _current_interval:
        _interruptible_sleep(_current_interval - elapsed, abort_check)

    params = urllib.parse.urlencode({
        "currency": currency,
        "appid": APPID_CS2,
        "market_hash_name": market_hash_name,
    })
    url = f"https://steamcommunity.com/market/priceoverview/?{params}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 cs2-trade-calculator/1.0"})

    # Retry transient failures a couple of times -- connection resets and edge-node hiccups
    # happen, and one shouldn't sink an otherwise-fine request. (Cert-validation failures used
    # to dominate this path; that was the antivirus MITM documented in net.py, not the CDN.)
    last_exception = None
    for attempt in range(3):
        if abort_check and abort_check():
            return _LIVE_FETCH_FAILED
        try:
            _last_request_time = time.time()
            with urlopen(req, timeout=15, context=net.SSL_CONTEXT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            last_exception = None
            break
        except HTTPError as e:
            if e.code in (429, 403):
                # 403 included on purpose: when Steam soft-blocks, it often answers 403 (or
                # 5xx) instead of 429 -- treating those as generic errors meant NO backoff at
                # all, so a blocked scan kept firing at full interval speed into the block.
                retry_after = None
                header_val = e.headers.get("Retry-After") if e.headers else None
                if header_val is not None:
                    try:
                        retry_after = float(header_val)
                    except ValueError:
                        retry_after = None  # some servers send an HTTP-date instead of seconds; ignore those
                backoff = _mark_rate_limited(retry_after)
                _interruptible_sleep(backoff, abort_check)
                _record(backoff_until=None)
                # Retry within the SAME bounded attempt budget instead of giving up right
                # after paying the backoff wait -- the wait already satisfied the rate limit,
                # so returning early here without ever using it wasted the cost and forced the
                # caller to start over from scratch (compounding into more invalid-looking
                # candidates during a rate-limit storm than the actual limiter should cause).
                last_exception = e
                continue
            _record(increment="requests_error")
            # Any other HTTP error is transient/server-side, not "this item has no
            # listings" -- don't cache it, or a temporary failure would masquerade as
            # permanent "no price data" for a full day (PRICE_TTL_SECONDS) even after
            # Steam recovers.
            return _LIVE_FETCH_FAILED
        except (URLError, json.JSONDecodeError, TimeoutError) as e:
            last_exception = e
            _interruptible_sleep(1.0, abort_check)

    if last_exception is not None:
        # Same reasoning as the 429 case: connection resets, timeouts, bad cert chains, and
        # malformed responses are transient network hiccups, not "this item has no listings".
        _record(increment="requests_error")
        return _LIVE_FETCH_FAILED

    if not isinstance(body, dict):
        # A heavily-blocked Steam answers some requests with a literal JSON `null` body --
        # json.loads() happily returns None for it, and body.get() below would crash.
        _record(increment="requests_error")
        return _LIVE_FETCH_FAILED

    _mark_success()

    # Only a real, successful response is cached -- including a genuine "success": false,
    # which does mean "this item really has no listings" (as opposed to the exceptions above).
    data = None
    if body.get("success"):
        data = {
            "lowest_price": _parse_money(body.get("lowest_price")),
            "median_price": _parse_money(body.get("median_price")),
            "volume": int(body["volume"].replace(",", "")) if body.get("volume") else None,
        }
    _cache[key] = {"_fetched_at": time.time(), "data": data}
    _save_cache()
    return data


def get_cached_price(market_hash_name: str, currency: int = 1) -> tuple[dict | None, str]:
    """Reads price_cache.json only -- NEVER makes a live request itself. Returns (data,
    freshness) where freshness is "fresh" (within PRICE_TTL_SECONDS), "stale" (cached but
    expired), or "missing" (never fetched). This is the FIRST thing simulate.py's _price()
    checks for every input/output unit -- a "missing" result is what triggers _price()'s own
    fallback to a live get_price() call (see that function's module docstring for why this
    reversed an earlier cache-only design) -- see price_scan.py for the mode that warms this
    cache in bulk ahead of time, reducing how often that fallback is needed.

    Every call bumps get_stats()["cache_reads_fresh"/"cache_reads_stale"/"cache_reads_missing"]
    (and the calling thread's set_attribution() bucket, if any) -- this is simulate.py's actual
    hot-path cache-hit-rate signal, since get_price()'s own "cache_hits" counter is only ever
    reached when a caller skips this function and goes straight to a live fetch (which
    simulate._price() never does -- see that module)."""
    global _cache
    # Same override-wins rule as get_price() -- simulate._price() checks this function FIRST
    # for every unit, so without this hook an overridden skin whose cache entry is warm would
    # keep pricing off the stale market number.
    from . import user_overrides
    override = user_overrides.get_override_entry(market_hash_name, currency)
    if override is not None:
        return override, "fresh"
    if not _cache:
        _load_cache()
    key = f"{currency}:{market_hash_name}"
    cached = _cache.get(key)
    if not cached:
        _record(increment="cache_reads_missing")
        return None, "missing"
    age = time.time() - cached.get("_fetched_at", 0)
    if age < PRICE_TTL_SECONDS:
        _record(increment="cache_reads_fresh")
        return cached["data"], "fresh"
    _record(increment="cache_reads_stale")
    return cached["data"], "stale"


def iter_cached_entries():
    """Yields (market_hash_name, currency, data, fetched_at) for every currently-cached
    entry that has real price data -- read-only, triggers nothing beyond the same lazy
    _load_cache() get_cached_price() already does. Used by dead_skin.py's weekly review to
    re-derive scores from whatever's already been fetched, without any new live network calls."""
    global _cache
    if not _cache:
        _load_cache()
    for key, entry in _cache.items():
        data = entry.get("data")
        if not data:
            continue
        currency_str, _, name = key.partition(":")
        try:
            currency = int(currency_str)
        except ValueError:
            continue
        yield name, currency, data, entry.get("_fetched_at")


def market_hash_name(skin_name: str, wear: str, stattrak: bool) -> str:
    prefix = "StatTrak™ " if stattrak else ""
    return f"{prefix}{skin_name} ({wear})"
