"""Third-party marketplace search-link builder.

Not an API integration -- just deep-links into each site's own search/listing page for a
given skin, so a profitable trade-up's cheapest input (or a Covert -> knife/glove input)
can be tracked down quickly on whichever site actually has a cheap, high-float copy right
now. Same read-only spirit as the rest of this tool: nothing here places an order, it just
saves you from retyping the skin name into five different search boxes.

URL patterns are each site's public search page as of the 2025 CS2 update; sites do change
these occasionally, so treat a dead link as "verify manually" rather than a bug in the price
numbers themselves.
"""
import urllib.parse

from .steam_prices import market_hash_name as _steam_market_hash_name

# CSFloat's own sort key for "cheapest listing with the highest float in its wear bracket
# first" -- the whole point of hunting cheap+high-float is that a worse (higher) float
# within the SAME wear bucket is usually cheaper for basically the same look, so default to
# sorting by price rather than float unless the caller wants otherwise.
CSFLOAT_SORT = "price"


def build_links(skin_name: str, wear: str | None = None, stattrak: bool = False) -> dict:
    """Returns {marketplace_key: url} search links for this skin (optionally at a specific
    wear). skin_name should be the bare skin name (e.g. "AK-47 | Redline" or
    "★ Karambit | Doppler"), not a pre-built market_hash_name."""
    full_name = _steam_market_hash_name(skin_name, wear, stattrak) if wear else \
        (f"StatTrak™ {skin_name}" if stattrak else skin_name)
    q_full = urllib.parse.quote(full_name)
    q_name = urllib.parse.quote(skin_name)
    return {
        "steam": f"https://steamcommunity.com/market/listings/730/{q_full}",
        "csfloat": f"https://csfloat.com/search?market_hash_name={q_full}&sort_by={CSFLOAT_SORT}",
        "skinport": f"https://skinport.com/market?search={q_name}&sort=price&order=asc",
        "dmarket": f"https://dmarket.com/ingame-items/item-list/csgo-skins?title={q_name}",
        "bitskins": f"https://bitskins.com/market/cs2?search={q_name}&sort_by=price&order=asc",
    }


MARKETPLACE_LABELS = {
    "steam": "Steam Market",
    "csfloat": "CSFloat",
    "skinport": "Skinport",
    "dmarket": "DMarket",
    "bitskins": "BitSkins",
}
