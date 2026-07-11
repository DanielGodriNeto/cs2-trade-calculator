"""User-managed, PERMANENT market corrections: a per-skin blacklist and manual price overrides.

Every other protective registry in this project (dead_skin.py, invalid_skins.py,
mispriced_skins.py, wear_anomaly.py, market_reliability.py) is deliberately temporary and
self-healing -- great for transient market weirdness, useless when the user has personally
confirmed a skin's data is garbage and wants it gone for good. This module is the opposite by
design: nothing here ever expires, decays, or gets re-checked. Entries change ONLY when the
user adds or removes them (dashboard "Skin blacklist" / "Price overrides" panels, the
/api/skin-blacklist and /api/price-overrides endpoints, or the CLI commands).

Two registries, one file each (both under data/ -- user preferences, never wiped by anything
that clears cache/):

1. SKIN BLACKLIST (data/user_skin_blacklist.json) -- keyed by BARE skin name (e.g.
   "AWP | Gungnir"), applying to every wear and StatTrak variant at once. A blacklisted skin:
     - is excluded from random-candidate generation (sim_loop.run_round unions
       blacklisted_market_names() into the same exclusion set invalid_skins already feeds);
     - hard-fails simulate.simulate() as input OR output (an InvalidSimulation, so no agent
       can ever value a contract touching it, whatever generator produced the candidate);
     - is filtered out of the classic tradeups scan (inputs skipped; a rarity step whose
       OUTPUT pool contains a blacklisted skin is skipped entirely -- you can't do the
       trade-up without a chance of landing on it, so no honest row can be built around it);
     - is rejected by the manual builder with a per-skin reason;
     - and every EXISTING best-list/archive entry containing it (either side) is hidden
       immediately at list time and evicted from checkpoints by _consider_best's self-clean,
       exactly the sim_blacklist.py pattern.

2. PRICE OVERRIDES (data/price_overrides.json) -- keyed by full market_hash_name (skin +
   wear + StatTrak prefix), storing the price the user says is the real one. An override:
     - WINS over every other source: steam_prices.get_cached_price()/get_price() return it
       before consulting the cache, the bulk mirror, or a live fetch -- so every scan,
       simulation, strategy search and the manual builder all price with it automatically;
     - retro-actively REPRICES existing best-list/archive entries at list time
       (repriced_entry_copy(): swaps the matching unit/output prices in a COPY, recomputes
       cost / expected revenue / profit / ROI with the same 13% fee math simulate.py used,
       and tags the entry `repriced_by_override`) so Min-ROI filters catch bugged trade-ups
       without waiting for an agent to re-simulate them.

Overrides are stored with the currency they were entered in and converted through the same
csgotrader exchange-rate feed bulk_prices' own price conversion uses, so a USD override still
answers a BRL-configured scan honestly.

Import-cycle note: steam_prices.py consults this module (lazily, inside its two lookup
functions), so this module must never import steam_prices at module level -- market hash names
are built locally with the same formatting rule, and tradeup.STEAM_SELL_FEE is imported lazily
inside repriced_entry_copy().
"""
import json
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BLACKLIST_FILE = DATA_DIR / "user_skin_blacklist.json"
OVERRIDES_FILE = DATA_DIR / "price_overrides.json"

_lock = threading.Lock()
_blacklist: dict | None = None   # {bare skin name: {"added_at", "note"}}
_overrides: dict | None = None   # {market_hash_name: {"price", "currency", "skin", "wear",
#                                   "stattrak", "added_at"}}
_version = 0  # bumped on every blacklist mutation -- change-detection key for the cached
# blacklisted_market_names() expansion below (same idiom as tradability's feed_fetched_at key)
_market_names_cache: dict = {"version": -1, "names": set()}


def _market_hash_name(skin_name: str, wear: str, stattrak: bool) -> str:
    # Same formatting rule as steam_prices.market_hash_name -- duplicated (one f-string)
    # instead of imported to avoid a module-level import cycle (see module docstring).
    prefix = "StatTrak™ " if stattrak else ""
    return f"{prefix}{skin_name} ({wear})"


def _load_locked() -> None:
    global _blacklist, _overrides
    if _blacklist is None:
        if BLACKLIST_FILE.exists():
            try:
                _blacklist = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8")).get("skins", {})
            except (json.JSONDecodeError, OSError):
                _blacklist = {}
        else:
            _blacklist = {}
    if _overrides is None:
        if OVERRIDES_FILE.exists():
            try:
                _overrides = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8")).get("overrides", {})
            except (json.JSONDecodeError, OSError):
                _overrides = {}
        else:
            _overrides = {}


def _save_blacklist_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BLACKLIST_FILE.write_text(json.dumps({"skins": _blacklist}), encoding="utf-8")


def _save_overrides_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(json.dumps({"overrides": _overrides}), encoding="utf-8")


# --- Skin blacklist ------------------------------------------------------------------------


def add_blacklist(skin_name: str, note: str = "") -> dict:
    """Permanently blacklist a bare skin name (idempotent). The name should match the skin
    database exactly (e.g. "AWP | Gungnir") -- callers that take user input should resolve it
    against data.load_skins() first for a friendly error, but an unknown name is still stored
    (it simply never matches anything) rather than rejected here."""
    global _version
    skin_name = (skin_name or "").strip()
    if not skin_name:
        raise ValueError("skin name is required")
    with _lock:
        _load_locked()
        entry = _blacklist.get(skin_name)
        if entry is None:
            entry = {"added_at": time.time(), "note": note or ""}
            _blacklist[skin_name] = entry
            _version += 1
            _save_blacklist_locked()
        return {"skin": skin_name, **entry}


def remove_blacklist(skin_name: str) -> bool:
    global _version
    with _lock:
        _load_locked()
        if skin_name in _blacklist:
            del _blacklist[skin_name]
            _version += 1
            _save_blacklist_locked()
            return True
        return False


def is_blacklisted_skin(skin_name: str) -> bool:
    with _lock:
        _load_locked()
        return skin_name in _blacklist


def blacklisted_skins() -> set:
    """The current set of blacklisted bare skin names -- grab once per batch/scan and do plain
    set lookups on the hot path instead of calling is_blacklisted_skin() per unit."""
    with _lock:
        _load_locked()
        return set(_blacklist)


def list_blacklist() -> list:
    with _lock:
        _load_locked()
        rows = [{"skin": name, **entry} for name, entry in _blacklist.items()]
    rows.sort(key=lambda e: e.get("added_at", 0), reverse=True)
    return rows


def blacklisted_market_names() -> set:
    """Every market_hash_name (all wears x StatTrak variants) of every blacklisted skin --
    shaped to union straight into sim_loop.run_round's exclusion set alongside
    invalid_skins.active_invalid_names() / tradability.untradeable_market_names(). Cached and
    rebuilt only when the blacklist actually changes (the skin database is static for the
    process lifetime). Names not found in the skin database expand to nothing -- harmless."""
    with _lock:
        _load_locked()
        version = _version
        if _market_names_cache["version"] == version:
            return _market_names_cache["names"]
        wanted = set(_blacklist)
    names: set = set()
    if wanted:
        from . import data as data_mod  # lazy: keep this module import-light (see docstring)
        for s in data_mod.load_skins() + data_mod.load_knives_gloves():
            if s["name"] not in wanted:
                continue
            for wear in s.get("wears", []):
                names.add(_market_hash_name(s["name"], wear, stattrak=False))
                if s.get("stattrak"):
                    names.add(_market_hash_name(s["name"], wear, stattrak=True))
    with _lock:
        _market_names_cache["version"] = version
        _market_names_cache["names"] = names
    return names


def entry_contains_blacklisted_skin(entry) -> bool:
    """True if ANY input unit or possible output of a best-list/archive entry (the dict shape
    sim_loop._consider_best stores) names a blacklisted skin. Used to hide existing entries the
    moment a skin is blacklisted -- no re-simulation needed."""
    bl = blacklisted_skins()
    if not bl:
        return False
    for u in entry.get("input_mix") or []:
        if u.get("skin") in bl:
            return True
    for o in entry.get("output_distribution") or []:
        if o.get("skin") in bl:
            return True
    return False


# --- Price overrides -----------------------------------------------------------------------


def set_override(skin_name: str, wear: str, stattrak: bool, price: float, currency: int = 1) -> dict:
    skin_name = (skin_name or "").strip()
    if not skin_name or not wear:
        raise ValueError("skin name and wear are required")
    price = float(price)
    if price <= 0:
        raise ValueError("price must be > 0")
    name = _market_hash_name(skin_name, wear, bool(stattrak))
    entry = {"price": round(price, 2), "currency": int(currency), "skin": skin_name,
             "wear": wear, "stattrak": bool(stattrak), "added_at": time.time()}
    with _lock:
        _load_locked()
        _overrides[name] = entry
        _save_overrides_locked()
    return {"market_hash_name": name, **entry}


def remove_override(market_hash_name: str) -> bool:
    with _lock:
        _load_locked()
        if market_hash_name in _overrides:
            del _overrides[market_hash_name]
            _save_overrides_locked()
            return True
        return False


def list_overrides() -> list:
    with _lock:
        _load_locked()
        rows = [{"market_hash_name": name, **entry} for name, entry in _overrides.items()]
    rows.sort(key=lambda e: e.get("added_at", 0), reverse=True)
    return rows


def has_overrides() -> bool:
    """Cheap gate so hot paths (every get_cached_price call, every list-time reprice) can skip
    all override work entirely in the overwhelmingly common no-overrides case."""
    with _lock:
        _load_locked()
        return bool(_overrides)


def _convert(price: float, from_currency: int, to_currency: int) -> float:
    if from_currency == to_currency:
        return price
    from . import bulk_prices  # lazy -- exchange rates come from the same mirror feed the
    # bulk price conversion already uses; falls back to 1.0 per unknown rate, same policy
    return price / bulk_prices.rate_for(from_currency) * bulk_prices.rate_for(to_currency)


def get_override_price(market_hash_name: str, currency: int = 1) -> float | None:
    """The user's price for this exact market_hash_name, converted to `currency` -- or None
    when no override exists (the overwhelmingly common case; one dict lookup)."""
    with _lock:
        _load_locked()
        entry = _overrides.get(market_hash_name)
    if entry is None:
        return None
    return round(_convert(entry["price"], entry.get("currency", 1), currency), 2)


def get_override_entry(market_hash_name: str, currency: int = 1) -> dict | None:
    """Shaped exactly like steam_prices.get_price()'s return so it can be handed straight back
    from the price-lookup layer: {"lowest_price", "median_price", "volume", "source"}."""
    price = get_override_price(market_hash_name, currency)
    if price is None:
        return None
    return {"lowest_price": price, "median_price": price, "volume": None,
            "source": "user_override"}


def repriced_entry_copy(entry, currency: int = 1):
    """If any input unit or priced output of a best-list/archive entry has an active price
    override, return a DEEP-ADJUSTED COPY with the overridden prices swapped in and
    cost / per-output net profit / expected ROI recomputed (same probability-weighted,
    after-13%-fee math simulate.simulate uses); otherwise return the entry object unchanged.

    Never mutates the original -- list_all_opportunities' archive cache and each son's
    in-memory checkpoint keep their stored (as-simulated) numbers, so removing an override
    later reverts the display automatically. currency=1 matches sim_loop's own hardcoded
    simulation currency."""
    if not has_overrides():
        return entry
    stattrak = bool(entry.get("stattrak"))
    applied: list = []

    input_mix = []
    for u in entry.get("input_mix") or []:
        name = _market_hash_name(u.get("skin", ""), u.get("wear", ""), stattrak)
        price = get_override_price(name, currency)
        if price is not None and price != u.get("unit_price"):
            u = {**u, "unit_price": price}
            applied.append(name)
        input_mix.append(u)

    output_distribution = []
    for o in entry.get("output_distribution") or []:
        name = _market_hash_name(o.get("skin", ""), o.get("wear", ""), stattrak)
        price = get_override_price(name, currency)
        if price is not None and price != o.get("price"):
            o = {**o, "price": price}
            applied.append(name)
        output_distribution.append(o)

    if not applied:
        return entry

    from .tradeup import STEAM_SELL_FEE  # lazy -- see module docstring's cycle note
    cost = sum((u.get("unit_price") or 0.0) * (u.get("count") or 0) for u in input_mix)
    revenue_weighted = [(o["price"], o.get("prob_pct") or 0.0)
                        for o in output_distribution if o.get("price") is not None]
    total_prob = sum(p for _, p in revenue_weighted)
    new = {**entry, "input_mix": input_mix, "output_distribution": output_distribution,
           "cost": round(cost, 2), "repriced_by_override": sorted(set(applied))}
    if revenue_weighted and total_prob > 0 and cost:
        expected_revenue = sum(v * p for v, p in revenue_weighted) / total_prob
        after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
        new["expected_roi_pct"] = (after_fee - cost) / cost * 100
        new["output_distribution"] = [
            ({**o, "net_profit": o["price"] * (1 - STEAM_SELL_FEE) - cost}
             if o.get("price") is not None else o)
            for o in output_distribution]
    return new
