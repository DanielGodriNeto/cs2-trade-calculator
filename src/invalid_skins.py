"""Registry of skins that have NO obtainable price on Steam OR any third-party site.

The distinction from dead_skin.py: a "dead" skin has a price, it's just thin/illiquid/
suspiciously high (fake ROI). An "invalid" skin here is one where simulate.py could not get a
usable price at all -- Steam's cache missed, a live Steam fetch came up empty, AND the
third-party fallback (Skinport/CSFloat, see thirdparty_prices.py) had nothing either. There's
no honest way to value a trade-up that contains such a skin, so:

1. simulate.py records it here (mark_invalid) the moment both sources come up empty for a
   given skin+wear+StatTrak.
2. random_trade.py filters these out of candidate generation (active_invalid_names) so the
   ML never bothers proposing -- or wasting a simulate() call on -- a trade-up built out of a
   skin with no price anywhere.

Like dead_skin.py (and unlike sim_blacklist.py's user "already did this" list), this is a
market fact, not a per-son preference or simulated result -- so it's ONE shared
data/invalid_skins.json across the whole population, which does NOT violate
feedback_sim_data_must_never_touch_real_outcomes (no outcomes, no per-son model/data crossing).

And, again like dead_skin.py, it is deliberately TEMPORARY and self-healing, never a permanent
blacklist: each entry carries a marked_at timestamp, and after INVALID_RECHECK_SECONDS it drops
out of active_invalid_names() so the skin gets a fresh pricing attempt on its own -- if it
prices again (relisted, an outage recovered), simulate.py clears it immediately via clear_name().

Keyed by steam_prices.market_hash_name (which already encodes wear + the "StatTrak(TM) "
prefix), so the key composes directly with what random_trade.py / simulate.py already build.
An in-memory _store cache avoids re-reading the file on every hot-loop membership check -- the
sim loop and the dashboard API live in the same process, so they share that cache and stay
consistent; the CLI `invalid-skins` command reads its own fresh copy at process start.
"""
import json
import threading
import time
from pathlib import Path

from . import steam_prices as steam_prices_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INVALID_FILE = DATA_DIR / "invalid_skins.json"

INVALID_RECHECK_SECONDS = 24 * 3600  # after this long, allow a fresh pricing attempt again --
# matches steam_prices.PRICE_TTL_SECONDS so a skin becomes re-checkable at the same cadence its
# cached price would have expired anyway. Temporary + self-healing, never a permanent blacklist.

_lock = threading.Lock()
_store: dict | None = None  # {"skins": {key: {...}}}; loaded lazily, cached in-process


def _key(name: str, currency: int) -> str:
    return f"{currency}:{name}"


def _ensure_loaded() -> None:
    global _store
    if _store is not None:
        return
    if INVALID_FILE.exists():
        try:
            _store = json.loads(INVALID_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _store = {"skins": {}}
    else:
        _store = {"skins": {}}
    _store.setdefault("skins", {})


def _save_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INVALID_FILE.write_text(json.dumps(_store), encoding="utf-8")


def mark_invalid(skin_name: str, wear: str, stattrak: bool, currency: int, reason: str) -> None:
    """Record (or refresh) a skin+wear+StatTrak as having no obtainable price on any source.
    Refreshing an existing entry just bumps marked_at (restarting its recheck window) so a skin
    that's repeatedly confirmed priceless keeps getting skipped rather than flickering back in."""
    name = steam_prices_mod.market_hash_name(skin_name, wear, stattrak)
    key = _key(name, currency)
    now = time.time()
    with _lock:
        _ensure_loaded()
        entry = _store["skins"].get(key)
        if entry is None:
            entry = {"skin": skin_name, "wear": wear, "stattrak": stattrak, "currency": currency,
                     "market_hash_name": name, "reason": reason, "first_seen": now, "marked_at": now}
        else:
            entry["marked_at"] = now
            entry["reason"] = reason
        _store["skins"][key] = entry
        _save_locked()


def clear_name(name: str, currency: int) -> bool:
    """Drop a skin from the registry the moment it prices again (simulate.py calls this on any
    successful cache/live/third-party price). A cheap no-op -- no disk write -- when the skin
    isn't currently registered, which is the overwhelmingly common case, so it's safe to call
    for every priced unit in simulate.py's hot loop."""
    key = _key(name, currency)
    with _lock:
        _ensure_loaded()
        if key in _store["skins"]:
            del _store["skins"][key]
            _save_locked()
            return True
        return False


def is_invalid(name: str, currency: int) -> bool:
    """True only if this market_hash_name is currently marked AND still within its recheck
    window (an expired entry reads as valid so it gets retried)."""
    key = _key(name, currency)
    with _lock:
        _ensure_loaded()
        entry = _store["skins"].get(key)
        if entry is None:
            return False
        return (time.time() - entry["marked_at"]) < INVALID_RECHECK_SECONDS


def active_invalid_names(currency: int) -> set:
    """The set of market_hash_names random_trade.py should currently exclude from candidate
    generation for `currency` -- only entries still inside the recheck window (see
    INVALID_RECHECK_SECONDS). Loaded once per round by sim_loop.run_round and passed down, so
    the per-unit filtering in random_trade.py is a plain in-memory set lookup, no disk I/O."""
    now = time.time()
    with _lock:
        _ensure_loaded()
        return {e["market_hash_name"] for e in _store["skins"].values()
                if e.get("currency") == currency
                and (now - e["marked_at"]) < INVALID_RECHECK_SECONDS}


def list_invalid_skins(currency: int | None = None) -> list:
    """Every currently-registered invalid skin (most-recently-marked first), each tagged with
    whether it's still active (within the recheck window) or due for a re-check -- for the CLI
    `invalid-skins` command and the dashboard's Market Intelligence panel."""
    now = time.time()
    with _lock:
        _ensure_loaded()
        rows = [dict(e) for e in _store["skins"].values()
                if currency is None or e.get("currency") == currency]
    for r in rows:
        r["active"] = (now - r.get("marked_at", 0)) < INVALID_RECHECK_SECONDS
    rows.sort(key=lambda e: e.get("marked_at", 0), reverse=True)
    return rows


def clear_all() -> int:
    """Erase every registered invalid skin from disk and the in-process cache. This is a market
    fact, not simulated/outcome data (see module docstring), so wiping it is harmless: any skin
    that still has no price will simply be re-marked the next time simulate.py comes up empty for
    it. Returns how many entries were removed. Used by the dashboard's 'Erase invalid skins' button
    and the CLI."""
    with _lock:
        _ensure_loaded()
        n = len(_store["skins"])
        _store["skins"] = {}
        _save_locked()
        return n


def forget_all() -> None:
    """Test/maintenance helper: drop the in-process cache so the next call re-reads from disk.
    Not used in normal operation (the process-lived cache is the intended fast path)."""
    global _store
    with _lock:
        _store = None
