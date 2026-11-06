"""Self-check for how steam_prices caches a MISS in the csgotrader steam mirror.

A name absent from the mirror is cached as "no price" only while the mirror itself is fresh
and FX is available, and then only for MIRROR_MISS_TTL_SECONDS (not the 24h PRICE_TTL_SECONDS).
A stale mirror or FX down caches nothing. Plain None entries (live-fallback "no listings") keep
the 24h rule. Everything is mocked: no network, no disk writes.

Run: python test_steam_mirror_miss.py
"""
import time

from src import bulk_prices, price_history, steam_prices, user_overrides

NAME = "Test Gun | New Skin (Field-Tested)"
OTHER = "Test Gun | Listed Skin (Field-Tested)"
EUR = 3
HOUR = 3600
KEY = f"{EUR}:{NAME}"


def _no_network(feed):
    raise AssertionError(f"network fetch attempted for feed {feed!r}")


bulk_prices._fetch = _no_network
steam_prices._load_cache = lambda: None
steam_prices._save_cache = lambda: None
price_history.record_snapshot = lambda *args, **kwargs: None
steam_prices.STEAM_LIVE_FALLBACK = False
user_overrides._overrides = {}
user_overrides._blacklist = {}


def _set_state(feed_age_seconds: float, fx_ok: bool) -> None:
    now = time.time()
    bulk_prices._feeds["steam"] = {"_fetched_at": now - feed_age_seconds,
                                   "data": {OTHER: {"last_24h": 5.0}}}
    bulk_prices._feeds["rates"] = {"_fetched_at": now, "data": {"EUR": 0.5} if fx_ok else {}}
    # Cooldown means a stale feed is served as-is; nothing here ever downloads.
    bulk_prices._last_failure["steam"] = now
    steam_prices._cache = {}


# 1. Fresh mirror, FX ok, name missing -> cached as a short-lived miss.
_set_state(feed_age_seconds=HOUR, fx_ok=True)
assert steam_prices.get_price(NAME, EUR) is None
entry = steam_prices._cache.get(KEY)
assert entry is not None and entry["data"] is None, "fresh-mirror miss must be cached"
assert entry.get("_mirror_miss") is True, "mirror miss must be flagged for the short TTL"
assert steam_prices.get_cached_price(NAME, EUR) == (None, "missing")

# Past the short TTL (still well under 24h): not "stale" (which simulate.py treats as proof),
# but "missing", so the next lookup re-checks the mirror instead of trusting the old miss.
steam_prices._cache[KEY]["_fetched_at"] = time.time() - (steam_prices.MIRROR_MISS_TTL_SECONDS + 60)
assert steam_prices.get_cached_price(NAME, EUR) == (None, "missing"), "expired miss must re-check"
assert steam_prices.get_price(NAME, EUR) is None
assert steam_prices._cache[KEY]["_fetched_at"] > time.time() - 60, "fresh-mirror miss re-cached"

# _purge_expired drops an expired miss at the short TTL, not after 24h.
steam_prices._cache[KEY]["_fetched_at"] = time.time() - (steam_prices.MIRROR_MISS_TTL_SECONDS + 60)
steam_prices._purge_expired()
assert KEY not in steam_prices._cache, "expired miss must be purged at the short TTL"

# 2. Stale mirror (past FEED_TTL), FX ok, name missing -> nothing cached at all.
_set_state(feed_age_seconds=bulk_prices.FEED_TTL_SECONDS + 60, fx_ok=True)
assert steam_prices.get_price(NAME, EUR) is None
assert KEY not in steam_prices._cache, "stale-mirror miss must not be cached"
assert steam_prices.get_cached_price(NAME, EUR) == (None, "missing")

# 3. Fresh mirror, FX down, name missing -> nothing cached (existing guard, kept).
_set_state(feed_age_seconds=HOUR, fx_ok=False)
assert steam_prices.get_price(NAME, EUR) is None
assert KEY not in steam_prices._cache, "FX-down miss must not be cached"

# 4. Plain None entry (no mirror-miss flag, e.g. live "no listings") keeps the 24h rule: still
# "fresh" at 3h (not shortened to the mirror-miss window), and "stale" past 24h -> simulate.py
# still counts it as confirmed no price.
steam_prices._cache = {KEY: {"_fetched_at": time.time() - 3 * HOUR, "data": None}}
assert steam_prices.get_cached_price(NAME, EUR) == (None, "fresh"), "live None must not use the short TTL"
steam_prices._cache = {KEY: {"_fetched_at": time.time() - 25 * HOUR, "data": None}}
assert steam_prices.get_cached_price(NAME, EUR) == (None, "stale"), "live None keeps 24h semantics"

# 5. Name present in a fresh mirror -> real price, 24h TTL, no miss flag.
_set_state(feed_age_seconds=HOUR, fx_ok=True)
bulk_prices._feeds["steam"]["data"][NAME] = {"last_24h": 8.0}
price = steam_prices.get_price(NAME, EUR)
assert price is not None and price["lowest_price"] == 4.0, price
assert "_mirror_miss" not in steam_prices._cache[KEY], "a real price must not be a miss"

print("ok")
