"""Self-check for the scan_collection early exit (src.tradeup.EARLY_EXIT).

1. Every output has a cached Steam price and the ROI upper bound is below min_roi => the tier is
   skipped before any output pricing (no output price lookups, no results).
2. One output wear has no cached price => no sound bound => the tier is NOT skipped and is priced.
3. EARLY_EXIT = False => never skipped, even when the bound is below min_roi.

Hermetic: Steam prices and cache lookups are in-memory fakes.

Run: python test_early_exit.py
"""
from src import bulk_prices
from src import data as data_mod
from src import steam_prices
from src import tradeup as tu

LONG = "csgotrader:last_90d"
SHORT = "csgotrader:last_24h"
IN_NAME = "Input (Field-Tested)"
OUT_NAME = "Out (Field-Tested)"

STEAM: dict = {}  # market_hash_name -> price, served by the live lookup
CACHED: dict = {}  # market_hash_name -> price, served by the cache-only lookup
output_lookups: list = []  # names looked up through get_price, to prove pricing was skipped

RARITY_LOW = data_mod.RARITY_CHAIN[0][0]
RARITY_HIGH = data_mod.RARITY_CHAIN[1][0]


def fake_get_price(name, currency=1, abort_check=None):
    if name.startswith("Out "):
        output_lookups.append(name)
    price = STEAM.get(name)
    if price is None:
        return None
    return {"lowest_price": price, "median_price": price, "volume": 40, "source": SHORT}


def fake_get_cached_price(name, currency=1):
    price = CACHED.get(name)
    if price is None:
        return None, "missing"
    return {"lowest_price": price, "median_price": price, "volume": 40, "source": SHORT}, "fresh"


def install_fakes():
    tu.get_price = fake_get_price
    tu.get_cached_price = fake_get_cached_price
    steam_prices.get_price = fake_get_price
    bulk_prices.get_skinport_map = lambda currency=1: {}
    bulk_prices.get_buff163_map = lambda currency=1: {}
    bulk_prices.has_market_listing = lambda name: True


def skin(name, wears=("Field-Tested",)):
    return {"name": name, "wears": list(wears), "stattrak": False, "min_float": 0.0, "max_float": 1.0,
            "image": ""}


def scan(min_roi, strategy_any_roi=False):
    output_lookups.clear()
    return tu.scan_collection("Test Collection", {RARITY_LOW: [skin("Input")], RARITY_HIGH: [skin("Out")]},
                              stattrak=False, currency=1, min_roi=min_roi, strategy_any_roi=strategy_any_roi)


def setup_losing_tier(output_cached: bool):
    """Input costs 10 x $1.00 = $10; the output is worth $0.50 at Steam, so the best case ROI is ~-96%."""
    STEAM.clear()
    CACHED.clear()
    STEAM[IN_NAME] = 1.0
    STEAM[OUT_NAME] = 0.5
    if output_cached:
        CACHED[OUT_NAME] = 0.5


def check_skipped_when_bound_below_min_roi():
    setup_losing_tier(output_cached=True)
    res = scan(min_roi=0.0)
    assert res == [], res
    assert output_lookups == [], output_lookups


def check_uncached_output_not_skipped():
    setup_losing_tier(output_cached=False)
    res = scan(min_roi=0.0)
    assert len(res) == 1, res
    assert res[0].roi_pct < 0, res[0].roi_pct
    assert output_lookups, "uncached output must still be priced"


def check_flag_off_never_skips():
    setup_losing_tier(output_cached=True)
    saved = tu.EARLY_EXIT
    tu.EARLY_EXIT = False
    try:
        res = scan(min_roi=0.0)
    finally:
        tu.EARLY_EXIT = saved
    assert len(res) == 1, res
    assert output_lookups, "flag off must price the output"


def main():
    install_fakes()
    check_skipped_when_bound_below_min_roi()
    check_uncached_output_not_skipped()
    check_flag_off_never_skips()
    print("ok")


if __name__ == "__main__":
    main()
