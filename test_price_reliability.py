"""Self-check for two price-reliability guards in the bulk mirror path.

1. FX feed down: no price is produced (never a USD number passed off as EUR) and nothing is cached.
2. A price taken from a 90d averaging window is labelled as non-Steam-current, so the existing
   confidence demotion in pattern_scanner applies to it.

Run: python test_price_reliability.py
"""
import time

from src import bulk_prices, pattern_scanner, steam_prices, user_overrides

NAME_90D = "Test Gun | Fake Skin (Field-Tested)"
NAME_24H = "Test Gun | Fresh Skin (Field-Tested)"
EUR = 3

now = time.time()
bulk_prices._feeds["steam"] = {"_fetched_at": now, "data": {
    NAME_90D: {"last_90d": 10.0},
    NAME_24H: {"last_24h": 12.0},
}}
bulk_prices._feeds["skinport"] = {"_fetched_at": now, "data": {NAME_90D: {"starting_at": 9.0}}}

real_load_feed = bulk_prices._load_feed
bulk_prices._load_feed = lambda feed: None if feed == "rates" else real_load_feed(feed)
steam_prices.STEAM_LIVE_FALLBACK = False
steam_prices._save_cache = lambda: None

assert bulk_prices.get_steam_entry(NAME_90D, EUR) is None, "FX down must not price a EUR item"
assert bulk_prices.get_skinport_map(EUR) == {}, "FX down must not build a converted map"
assert ("skinport", EUR) not in bulk_prices._converted_maps, "FX failure must not be cached"
assert steam_prices.get_price(NAME_90D, EUR) is None, "FX down must skip the price"
assert steam_prices._cache.get(f"{EUR}:{NAME_90D}") is None, "FX failure must not be cached"

user_overrides._blacklist = {}
user_overrides._overrides = {
    NAME_90D: {"price": 10.0, "currency": EUR},
    NAME_24H: {"price": 10.0, "currency": 1},
}
assert user_overrides._convert(10.0, EUR, 1) is None, "FX down must not convert an override"
assert user_overrides.get_override_price(NAME_90D, 1) is None, "FX down must skip the EUR override"
assert user_overrides.get_override_price(NAME_24H, 1) == 10.0, "USD->USD must not need FX"

bulk_prices._load_feed = real_load_feed
bulk_prices._feeds["rates"] = {"_fetched_at": now, "data": {"EUR": 0.5}}
assert bulk_prices.get_steam_entry(NAME_90D, EUR)["median_price"] == 5.0, "rate must still convert"
assert user_overrides.get_override_price(NAME_90D, 1) == 20.0, "EUR override must convert back"

assert pattern_scanner._base_price(NAME_24H, {"consensus_price": 7.0})[1] == "steam"
label_90d = pattern_scanner._base_price(NAME_90D, {"consensus_price": 7.0})[1]
assert label_90d != "steam", f"90d price must not be labelled current Steam, got {label_90d!r}"
wording = pattern_scanner._base_basis_text(label_90d)
assert wording == "Steam median-sale price (last_90d window, confidence demoted)", wording
assert "consensus" not in wording
assert "consensus" in pattern_scanner._base_basis_text("consensus")

print("ok")
