"""User-managed market corrections: a per-skin blacklist and manual price overrides.

Every other protective registry in this project (dead_skin.py, invalid_skins.py,
mispriced_skins.py, wear_anomaly.py, market_reliability.py) is deliberately temporary and
self-healing -- great for transient market weirdness, useless when the user has personally
confirmed a skin's data is garbage and wants it gone for good. This module is the opposite by
design: nothing here decays or gets re-checked, and the DEFAULT for both registries is
permanent. Entries change ONLY when the user says so (dashboard "Skin blacklist" / "Price
overrides" panels, the /api/skin-blacklist and /api/price-overrides endpoints, or the CLI
commands) -- with exactly one exception, opted into per entry: a blacklist entry may carry a
user-chosen `expires_at`, after which it stops applying and is pruned (see _active_locked).
That is still user intent, not self-healing: the tool never re-checks the skin, never revives
an entry, and never shortens or extends a duration the user picked. Omit the duration (the
default everywhere) and the entry is permanent exactly as before.

Two registries, one file each (both under data/ -- user preferences, never wiped by anything
that clears cache/):

1. SKIN BLACKLIST (data/user_skin_blacklist.json) -- keyed by BARE skin name (e.g.
   "AWP | Gungnir"), applying to every wear and StatTrak variant at once, permanently unless
   the entry was given a duration. A blacklisted skin:
     - is excluded from random-candidate generation (sim_loop.run_round unions
       blacklisted_market_names() into the same exclusion set invalid_skins already feeds);
     - hard-fails simulate.simulate() as input OR output (an InvalidSimulation, so no agent
       can ever value a contract touching it, whatever generator produced the candidate);
     - is filtered out of the classic tradeups scan (inputs skipped; a rarity step whose
       OUTPUT pool contains a blacklisted skin is skipped entirely -- you can't do the
       trade-up without a chance of landing on it, so no honest row can be built around it);
     - is rejected by the manual builder with a per-skin reason;
     - is filtered out of the Collector Arbitrage list too
       (pattern_scanner.list_opportunities), so a banned skin can't linger there as a "buy
       this" row after being cleared from every trade-up table on the same page;
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
_blacklist: dict | None = None   # {bare skin name: {"added_at", "note", "expires_at"}}
# expires_at is None (or absent, on entries written before durations existed) = permanent.
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


def _active_locked() -> dict:
    """The live blacklist, with any user-chosen durations that have run out dropped first.

    Call this instead of _load_locked() from every blacklist READ path. Expiry is checked
    lazily here rather than by a timer because there is nothing to do between reads, and
    because the prune has to bump _version: blacklisted_market_names() caches its expansion
    against that counter, so an entry falling off on the clock has to invalidate the cache
    exactly the way an explicit add/remove does. Pruning WRITES the file (so the expiry is
    durable and the dashboard list stops showing dead rows), but only on the one call that
    actually observes something expiring -- the common path is a scan of a handful of entries
    that are all still live. Must be called with _lock held."""
    global _version
    _load_locked()
    if not _blacklist:
        return _blacklist
    now = time.time()
    expired = [name for name, e in _blacklist.items()
               if e.get("expires_at") is not None and e["expires_at"] <= now]
    if expired:
        for name in expired:
            del _blacklist[name]
        _version += 1
        _save_blacklist_locked()
    return _blacklist


def add_blacklist(skin_name: str, note: str = "", duration_days: float | None = None) -> dict:
    """Blacklist a bare skin name. Permanent by default; pass duration_days to have the entry
    stop applying (and prune itself) that many days from now instead -- the user's own choice
    of "for a while", never the tool's.

    Re-adding a skin already on the list REPLACES its duration and note rather than being a
    no-op, so "blacklist this for 7 days" after a permanent entry (or vice versa) does what it
    says; the original added_at is kept so the list still shows when you first banned it.

    The name should match the skin database exactly (e.g. "AWP | Gungnir") -- callers that take
    user input should resolve it against data.load_skins() first for a friendly error, but an
    unknown name is still stored (it simply never matches anything) rather than rejected here."""
    global _version
    skin_name = (skin_name or "").strip()
    if not skin_name:
        raise ValueError("skin name is required")
    if duration_days is not None:
        duration_days = float(duration_days)
        if duration_days <= 0:
            raise ValueError("duration_days must be > 0 (omit it entirely for a permanent ban)")
    with _lock:
        previous = _active_locked().get(skin_name)
        entry = {"added_at": (previous or {}).get("added_at", time.time()),
                 "note": note or (previous or {}).get("note", ""),
                 "expires_at": None if duration_days is None else time.time() + duration_days * 86400}
        _blacklist[skin_name] = entry
        _version += 1
        _save_blacklist_locked()
        return {"skin": skin_name, **entry}


def remove_blacklist(skin_name: str) -> bool:
    global _version
    with _lock:
        _active_locked()  # an entry that already expired on its own reports "not blacklisted"
        if skin_name in _blacklist:
            del _blacklist[skin_name]
            _version += 1
            _save_blacklist_locked()
            return True
        return False


def is_blacklisted_skin(skin_name: str) -> bool:
    with _lock:
        return skin_name in _active_locked()


def blacklisted_skins() -> set:
    """The currently-ACTIVE set of blacklisted bare skin names (entries whose user-chosen
    duration has run out are already gone) -- grab once per batch/scan and do plain set lookups
    on the hot path instead of calling is_blacklisted_skin() per unit."""
    with _lock:
        return set(_active_locked())


def list_blacklist() -> list:
    with _lock:
        rows = [{"skin": name, **entry} for name, entry in _active_locked().items()]
    rows.sort(key=lambda e: e.get("added_at", 0), reverse=True)
    return rows


def blacklisted_market_names() -> set:
    """Every market_hash_name (all wears x StatTrak variants) of every blacklisted skin --
    shaped to union straight into sim_loop.run_round's exclusion set alongside
    invalid_skins.active_invalid_names() / tradability.untradeable_market_names(). Cached and
    rebuilt only when the blacklist actually changes (the skin database is static for the
    process lifetime). Names not found in the skin database expand to nothing -- harmless.
    _active_locked()'s prune bumps _version too, so an entry reaching its user-chosen expiry
    invalidates this cache exactly the way an explicit removal does."""
    with _lock:
        wanted = set(_active_locked())
        version = _version
        if _market_names_cache["version"] == version:
            return _market_names_cache["names"]
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


WEAR_NAMES = ("Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred")


def bare_skin_name(market_hash_name: str) -> str:
    """Inverse of _market_hash_name: "StatTrak(tm) AK-47 | Redline (Field-Tested)" ->
    "AK-47 | Redline", which is what this blacklist is keyed by.

    Needed by callers that only ever hold a full market name (the pattern scanner's listings),
    never a skin/wear pair. Deliberately conservative in both directions: the leading star that
    marks a knife/glove is part of the database name and is KEPT, and a trailing parenthetical
    is only dropped when it is literally one of the five wear brackets -- so a skin whose own
    name contains brackets survives intact instead of being silently truncated. Steam writes
    the star before the StatTrak marker while _market_hash_name writes it after, so both orders
    are stripped rather than assuming either."""
    name = (market_hash_name or "").strip()
    if name.startswith("Souvenir "):
        name = name[len("Souvenir "):]
    # The star is part of the knife/glove's DATABASE name (data.load_knives_gloves returns
    # "★ Karambit | Doppler"), so it has to survive; only the StatTrak marker comes off. Steam
    # writes "★ StatTrak™ X" while _market_hash_name writes "StatTrak™ ★ X" -- both orders
    # reduce to the same bare name here.
    star = ""
    if name.startswith("★ "):
        star, name = "★ ", name[len("★ "):]
    if name.startswith("StatTrak™ "):
        name = name[len("StatTrak™ "):]
    if not star and name.startswith("★ "):
        star, name = "★ ", name[len("★ "):]
    name = star + name
    if name.endswith(")"):
        head, sep, tail = name.rpartition(" (")
        if sep and tail[:-1] in WEAR_NAMES:
            name = head
    return name.strip()


def is_blacklisted_market_name(market_hash_name: str) -> bool:
    """Blacklist check for callers holding a full market_hash_name rather than a bare skin
    name -- every wear and StatTrak variant of a blacklisted skin is banned, so this resolves
    to the bare name first."""
    return is_blacklisted_skin(bare_skin_name(market_hash_name))


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
