"""Wear-order price-inversion check: is a BETTER condition of this skin cheaper?

Skin prices are monotonic in condition -- a Factory New copy of a skin is worth at least
as much as its own Field-Tested copy, always (same skin, strictly better float). So when
the price feed says otherwise ("FT $58, FN $6"), the higher price is a bad data point,
not real revenue: a lone troll listing, a stale median resting on one sale, or the known
souvenir conflation in the bulk dumps (souvenir-only wears priced under the plain name --
see tradability.py's module docstring). A positive-ROI trade-up whose expected revenue
leans on such a price is fake, and recommending it sends someone to buy 10 inputs for an
output that will actually sell near the CHEAPER better-wear price.

This module is the cheap, zero-extra-network sanity check for exactly that: prices come
from the same bulk mirror the scan already loaded (see steam_prices.py), so comparing a
priced output against its own better wears costs dictionary lookups, not requests.

Distinction from the neighbors (all temporary, self-healing registries, same pattern):
  - invalid_skins.py:   NO price on any source (can't be valued at all).
  - dead_skin.py:       price exists but Steam's own liquidity signals look thin.
  - mispriced_skins.py: price disagrees with a live SECOND marketplace (needs a Stage-2
                        verification spending rate-limited third-party requests).
  - THIS module:        price disagrees with the SAME marketplace's own better wears --
                        internally inconsistent, so no second source is needed to know
                        it's wrong. Runs inline on every positive-ROI scan result.

Consumed by tradeup.py: scan_collection() calls check_output_price() on every priced
output of a positive-ROI result (a negative result never gets recommended, so it isn't
worth even the cheap check), drops flagged prices from the revenue average, and the
matched-wear pricing helpers consult is_flagged() so mixed/float/covert strategy searches
skip an actively flagged skin+wear too -- the skin stops being recommended everywhere.

Like the neighbors, marks expire after RECHECK_SECONDS (the flagged price cache/dump entry
will have refreshed by then) and clear immediately when a re-check finds the wear ladder
back in order. One shared registry -- a market fact, not per-son data.
"""
import json
import threading
import time
from pathlib import Path

from . import steam_prices as steam_prices_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WEAR_ANOMALY_FILE = DATA_DIR / "wear_anomalies.json"

INVERSION_TOLERANCE_PCT = 5.0  # a better wear must be cheaper than the priced wear by MORE
# than this to flag. Real order books do show hairline inversions (an FN listed $9.90 while
# FT's median reads $10.00 -- spread noise, not a bogus price), and flagging those would
# blacklist half the catalog for nothing. The anomalies this exists to catch are nowhere
# near the line: souvenir-conflated or troll-listed prices sit 3x-100x above the skin's real
# wear ladder, so 5% keeps every honest price safe while still catching them all.
RECHECK_SECONDS = 24 * 3600  # matches steam_prices.PRICE_TTL_SECONDS / the neighbors' cadence:
# by the time a mark expires, the price entry it flagged has been refreshed anyway, so the
# re-check runs against genuinely new data instead of re-condemning the same cached number.

_lock = threading.Lock()
_store: dict | None = None  # {"skins": {key: {...}}} -- same lazy in-process cache shape as
# invalid_skins.py; the sim loop and dashboard share one process, the CLI reads fresh at start.


def _key(name: str, currency: int) -> str:
    return f"{currency}:{name}"


def _ensure_loaded() -> None:
    global _store
    if _store is not None:
        return
    if WEAR_ANOMALY_FILE.exists():
        try:
            _store = json.loads(WEAR_ANOMALY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _store = {"skins": {}}
    else:
        _store = {"skins": {}}
    _store.setdefault("skins", {})


def _save_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEAR_ANOMALY_FILE.write_text(json.dumps(_store), encoding="utf-8")


def mark(skin_name: str, wear: str, stattrak: bool, currency: int, price: float,
         better_wear: str, better_price: float) -> None:
    """Record (or refresh) a wear-order inversion for one skin+wear+StatTrak. The flagged
    key is the PRICED wear (the one whose price is inflated) -- the cheaper better wear is
    evidence, not the problem."""
    name = steam_prices_mod.market_hash_name(skin_name, wear, stattrak)
    now = time.time()
    with _lock:
        _ensure_loaded()
        entry = _store["skins"].get(_key(name, currency)) or {
            "skin": skin_name, "wear": wear, "stattrak": stattrak, "currency": currency,
            "market_hash_name": name, "first_seen": now}
        entry.update({"price": price, "better_wear": better_wear, "better_price": better_price,
                      "inversion_pct": (price - better_price) / price * 100 if price else 0.0,
                      "marked_at": now})
        _store["skins"][_key(name, currency)] = entry
        _save_locked()


def clear_name(name: str, currency: int) -> bool:
    """Drop a mark the moment a re-check finds the wear ladder back in order. Cheap no-op
    (no disk write) when the name isn't registered -- the overwhelmingly common case."""
    with _lock:
        _ensure_loaded()
        if _store["skins"].pop(_key(name, currency), None) is not None:
            _save_locked()
            return True
        return False


def is_flagged(name: str, currency: int) -> bool:
    """True only if this market_hash_name is currently marked AND within the recheck window
    (an expired mark reads as clean so the skin gets a fresh chance). Plain in-memory lookup,
    safe for the per-drop pricing hot path in tradeup.py."""
    with _lock:
        _ensure_loaded()
        entry = _store["skins"].get(_key(name, currency))
        if entry is None:
            return False
        return (time.time() - entry["marked_at"]) < RECHECK_SECONDS


def check_output_price(skin: dict, priced_wear: str, price_val: float, stattrak: bool,
                       currency: int, abort_check=None) -> dict | None:
    """The actual test: price every condition of `skin` BETTER than `priced_wear` (its
    "wears" list is ordered best-to-worst) and compare against `price_val`, the number the
    trade-up's revenue is using. Any better wear cheaper by more than
    INVERSION_TOLERANCE_PCT means price_val is a bad data point: mark the registry and
    return the anomaly details (cheapest better wear cited as evidence). A clean ladder
    clears any existing mark (self-healing) and returns None.

    Same price expression as the revenue side (median falling back to lowest, see
    tradeup._price_and_volume_at_matched_wear) so this compares like with like."""
    wears = skin.get("wears") or []
    if priced_wear not in wears:
        return None
    better_wears = wears[:wears.index(priced_wear)]  # list is best-to-worst
    worst_inversion = None  # (better_wear, better_price), cheapest better wear seen
    for wear in better_wears:
        if abort_check and abort_check():
            break
        name = steam_prices_mod.market_hash_name(skin["name"], wear, stattrak)
        price = steam_prices_mod.get_price(name, currency, abort_check)
        if not price:
            continue
        better_price = price["median_price"] or price["lowest_price"]
        if better_price is None:
            continue
        if better_price < price_val * (1 - INVERSION_TOLERANCE_PCT / 100):
            if worst_inversion is None or better_price < worst_inversion[1]:
                worst_inversion = (wear, better_price)
    priced_name = steam_prices_mod.market_hash_name(skin["name"], priced_wear, stattrak)
    if worst_inversion is None:
        clear_name(priced_name, currency)
        return None
    better_wear, better_price = worst_inversion
    mark(skin["name"], priced_wear, stattrak, currency, price_val, better_wear, better_price)
    return {"priced_wear": priced_wear, "price": price_val,
            "better_wear": better_wear, "better_price": better_price,
            "inversion_pct": (price_val - better_price) / price_val * 100 if price_val else 0.0}


def list_anomalies(currency: int | None = None) -> list:
    """Every currently-registered mark (most recent first), tagged active/expired -- for the
    dashboard's Market Intelligence panel and the CLI `wear-anomalies` command."""
    now = time.time()
    with _lock:
        _ensure_loaded()
        rows = [dict(e) for e in _store["skins"].values()
                if currency is None or e.get("currency") == currency]
    for r in rows:
        r["active"] = (now - r.get("marked_at", 0)) < RECHECK_SECONDS
    rows.sort(key=lambda e: e.get("marked_at", 0), reverse=True)
    return rows


def clear_all() -> int:
    """Erase every mark. Safe: a market fact, not outcome data -- any price still inverted
    gets re-flagged by the next positive-ROI scan that touches it."""
    with _lock:
        _ensure_loaded()
        n = len(_store["skins"])
        _store["skins"] = {}
        _save_locked()
        return n


def forget_all() -> None:
    """Test/maintenance helper, same as invalid_skins.forget_all."""
    global _store
    with _lock:
        _store = None
