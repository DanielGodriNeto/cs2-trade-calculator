"""Can this skin ACTUALLY be used in a trade-up contract? Two real-game rules the skin
database alone can't fully answer:

1. STRUCTURAL: a skin can only enter a trade-up if its collection has at least one skin at
   the NEXT rarity tier (the in-game contract UI refuses the skin otherwise). Also implied:
   the skin's own rarity must not be the top of the chain for a standard contract.
   rules_engine.validate_contract enforces this for whole contracts; check_skin() below
   answers it for one skin (for the manual builder's error messages and /api/skins).

2. MARKET EXISTENCE: souvenir items can NEVER be used in a trade-up, so what matters for an
   input is whether NON-souvenir copies actually trade. The upstream ByMykel feed can't
   answer that (souvenir-heavy and droppable collections look identical in its `crates`
   field -- verified against Dragon Lore vs. P250 | Sand Dune, 2026-07-10), so the honest
   oracle is the market itself: bulk_prices.has_market_listing() checks whether the
   NON-souvenir market_hash_name has ANY Steam median-sale price in the mirror's windows
   (24h through 90d). A skin failing that at EVERY wear either doesn't exist non-souvenir
   (souvenir-only, e.g. "Souvenir AWP | Dragon Lore" is the only Dragon Lore that's traded
   in months) or is so illiquid there is literally nothing to buy -- either way there's no
   honest way to build (or price) a contract around it as an input TODAY. Note this is
   about INPUTS only: outputs are never filtered by this (you don't need to buy an output).

Degrades gracefully: if the bulk price feeds have never been fetchable, every check returns
"eligible" (no filtering) rather than branding the whole catalog untradeable -- same
outage-must-not-invalidate-everything rule invalid_skins.py follows.

The computed unobtainable-name set is cached in-process and rebuilt only when the underlying
steam feed re-fetches (same freshness key bulk_prices' own converted maps use), so the hot
paths (sim_loop's per-round exclusion set, scan-time input filtering) are plain set lookups.
"""
import threading

from . import bulk_prices as bulk_prices_mod
from . import data as data_mod
from . import steam_prices as steam_prices_mod

_lock = threading.Lock()
_cache: dict = {}  # {"feed_fetched_at": epoch|None, "unobtainable": set[str] (bare skin names)}


def _steam_feed_fetched_at() -> "float | None":
    stats = bulk_prices_mod.get_stats().get("steam_feed") or {}
    age = stats.get("age_seconds")
    return None if age is None else -age  # only used as a change-detection key, sign irrelevant


def skin_is_obtainable(skin: dict) -> bool:
    """True if ANY wear of this skin (non-StatTrak, non-souvenir name) shows market evidence
    of really existing -- see the module docstring. True (never filter) when the feeds are
    unavailable. The non-StatTrak name is deliberate: every StatTrak-capable skin also exists
    plain, and souvenir-only skins have no StatTrak variant at all."""
    unknown = True
    for wear in skin.get("wears", []):
        name = steam_prices_mod.market_hash_name(skin["name"], wear, stattrak=False)
        found = bulk_prices_mod.has_market_listing(name)
        if found:
            return True
        if found is False:
            unknown = False
    return unknown  # every wear confirmed absent -> False; feeds never loaded -> True


def _rebuild_cache() -> dict:
    """One pass over the skin database against the current feeds: both the bare-name set and
    the expanded market_hash_name set (all wears x StatTrak variants), cached together until
    the underlying steam feed re-fetches. Empty sets when the feeds are unavailable (an
    outage means no filtering, never 'everything is untradeable')."""
    if not bulk_prices_mod.ensure_feeds_loaded():
        return {"feed_fetched_at": None, "unobtainable": set(), "market_names": set()}
    key = _steam_feed_fetched_at()
    with _lock:
        if _cache.get("feed_fetched_at") == key and key is not None:
            return _cache
    unobtainable = set()
    market_names = set()
    for s in data_mod.load_skins():
        if skin_is_obtainable(s):
            continue
        unobtainable.add(s["name"])
        for wear in s.get("wears", []):
            market_names.add(steam_prices_mod.market_hash_name(s["name"], wear, stattrak=False))
            if s.get("stattrak"):
                market_names.add(steam_prices_mod.market_hash_name(s["name"], wear, stattrak=True))
    fresh = {"feed_fetched_at": key, "unobtainable": unobtainable, "market_names": market_names}
    with _lock:
        _cache.update(fresh)
    return fresh


def unobtainable_skin_names() -> set:
    """Bare skin names (not market_hash_names) with NO market evidence at any wear --
    souvenir-only skins, mostly. Cached until the underlying steam feed re-fetches."""
    return _rebuild_cache()["unobtainable"]


def untradeable_market_names(currency: int = 1) -> set:
    """Every market_hash_name (all wears x StatTrak variants) of every unobtainable skin --
    shaped to union straight into invalid_skins.active_invalid_names()'s exclusion set in
    sim_loop.run_round, so the agents never propose a trade-up built from a skin no
    non-souvenir copy of exists. Unlike invalid_skins' learned-one-miss-at-a-time registry,
    this is known up front from the bulk feeds, costing zero live requests. currency is
    accepted for call-site symmetry; market_hash_names are currency-independent."""
    return _rebuild_cache()["market_names"]


def check_skin(skin: dict, idx: dict) -> tuple[bool, str]:
    """Full per-skin eligibility verdict for the manual builder / /api/skins: (True, "") if
    this skin can legally enter a standard trade-up right now, else (False, reason). idx is
    data.collections_index(). Reasons are stable machine-readable slugs the frontend maps to
    friendly text."""
    rid = skin.get("rarity_id")
    if rid not in data_mod.RARITY_INDEX:
        return False, "not_a_tradeup_rarity"
    if data_mod.RARITY_INDEX[rid] >= len(data_mod.RARITY_CHAIN) - 1:
        # Covert is the top of the standard chain -- only the separate 5-input case-based
        # Covert -> knife/glove contract (covert.py) can consume these, not this builder's
        # standard 10-input path.
        return False, "max_rarity_no_standard_tradeup"
    next_rid = data_mod.RARITY_CHAIN[data_mod.RARITY_INDEX[rid] + 1][0]
    if not any(idx.get(c, {}).get(next_rid) for c in skin.get("collections", [])):
        return False, "collection_has_no_next_rarity_outputs"
    if not skin_is_obtainable(skin):
        return False, "no_nonsouvenir_copies_on_market"
    return True, ""


ELIGIBILITY_REASONS = {
    # slug -> human-readable explanation, served via /api/constants so the dashboard's
    # wording can't drift from the code (same live-constants convention as everything else).
    "not_a_tradeup_rarity": "Not a trade-up rarity (knives/gloves/contraband can't be inputs).",
    "max_rarity_no_standard_tradeup": "Covert is the top of the standard chain -- only the "
                                       "5-input Covert → knife/glove contract can consume it.",
    "collection_has_no_next_rarity_outputs": "Its collection has no skins at the next rarity "
                                              "tier, so the game won't accept it in a contract.",
    "no_nonsouvenir_copies_on_market": "No non-souvenir copy has traded on Steam in the last "
                                        "90 days at any wear -- souvenir-only, or nothing to "
                                        "actually buy for a contract right now.",
}
