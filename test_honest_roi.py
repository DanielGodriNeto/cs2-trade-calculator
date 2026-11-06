"""Self-check for the honest (cash-market) output revenue rule in src.tradeup.

1. An output is worth the LOWEST after-fee proceeds across Steam (13%), Skinport (8%) and
   Buff163 (2.5%) -- a high Steam price with a low Skinport price pays out at Skinport.
2. CASH_MARKET_REVENUE = False restores the Steam-only 13% rule.
3. A candidate input below MIN_INPUT_PRICE ($0.50) is skipped: a $0.03 input contract disappears.
4. An output that can land in two wears is priced by the probability-weighted average of them.
5. A price that only exists in the 90-day Steam window is not used (input or output side).

Hermetic: Steam, Skinport, Buff163 and the has-listing oracle are replaced with in-memory fakes.

Run: python test_honest_roi.py
"""
from src import bulk_prices
from src import data as data_mod
from src import steam_prices
from src import tradeup as tu

SHORT = "csgotrader:last_24h"
LONG = "csgotrader:last_90d"
IN_NAME = "Input (Field-Tested)"
OUT_NAME = "Out (Field-Tested)"

STEAM: dict = {}  # market_hash_name -> (price, source tag)
SKINPORT: dict = {}
BUFF: dict = {}

RARITY_LOW = data_mod.RARITY_CHAIN[0][0]
RARITY_HIGH = data_mod.RARITY_CHAIN[1][0]


def fake_get_price(name, currency=1, abort_check=None):
    rec = STEAM.get(name)
    if rec is None:
        return None
    price, source = rec
    return {"lowest_price": price, "median_price": price, "volume": 40, "source": source}


def install_fakes():
    tu.get_price = fake_get_price
    steam_prices.get_price = fake_get_price
    bulk_prices.get_skinport_map = lambda currency=1: SKINPORT
    bulk_prices.get_buff163_map = lambda currency=1: BUFF
    bulk_prices.has_market_listing = lambda name: True


def reset_markets():
    STEAM.clear()
    SKINPORT.clear()
    BUFF.clear()


def skin(name, wears, lo=0.0, hi=1.0):
    return {"name": name, "wears": wears, "stattrak": False, "min_float": lo, "max_float": hi, "image": ""}


def scan(input_skin, outputs):
    return tu.scan_collection("Test Collection", {RARITY_LOW: [input_skin], RARITY_HIGH: outputs},
                              stattrak=False, currency=1)


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def check_skinport_beats_high_steam():
    reset_markets()
    STEAM[IN_NAME] = (1.0, SHORT)
    STEAM[OUT_NAME] = (100.0, SHORT)
    SKINPORT[OUT_NAME] = 10.0
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert len(res) == 1, res
    assert close(res[0].possible_drops[0].price, 10.0), res[0].possible_drops[0].price
    assert close(res[0].expected_revenue_after_fee, 10.0 * 0.92), res[0].expected_revenue_after_fee


def check_buff_lowest_wins():
    reset_markets()
    STEAM[IN_NAME] = (1.0, SHORT)
    STEAM[OUT_NAME] = (100.0, SHORT)
    SKINPORT[OUT_NAME] = 10.0  # 9.20 after its 8% fee
    BUFF[OUT_NAME] = 9.0  # 8.775 after its 2.5% fee -- the lowest proceeds
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert len(res) == 1, res
    assert close(res[0].expected_revenue_after_fee, 9.0 * 0.975), res[0].expected_revenue_after_fee


def check_steam_only_flag_restores_old_rule():
    reset_markets()
    STEAM[IN_NAME] = (1.0, SHORT)
    STEAM[OUT_NAME] = (100.0, SHORT)
    SKINPORT[OUT_NAME] = 10.0
    previous = tu.CASH_MARKET_REVENUE
    tu.CASH_MARKET_REVENUE = False
    try:
        res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    finally:
        tu.CASH_MARKET_REVENUE = previous
    assert len(res) == 1, res
    assert close(res[0].expected_revenue_after_fee, 100.0 * 0.87), res[0].expected_revenue_after_fee


def check_cheap_input_disappears():
    reset_markets()
    STEAM[IN_NAME] = (0.03, SHORT)
    STEAM[OUT_NAME] = (100.0, SHORT)
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert res == [], f"a $0.03 input must be skipped, got {len(res)} row(s)"
    STEAM[IN_NAME] = (0.60, SHORT)  # control: just above the floor, the row comes back
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert len(res) == 1, "a $0.60 input must still produce a row"


def check_cash_market_never_raises_revenue():
    reset_markets()
    STEAM[IN_NAME] = (1.0, SHORT)
    STEAM[OUT_NAME] = (100.0, SHORT)
    BUFF[OUT_NAME] = 500.0  # a lone high cash-market listing must not lift revenue above Steam
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert len(res) == 1, res
    assert close(res[0].expected_revenue_after_fee, 100.0 * 0.87), res[0].expected_revenue_after_fee


def check_cash_market_alone_is_not_revenue():
    reset_markets()
    STEAM[IN_NAME] = (1.0, SHORT)
    SKINPORT[OUT_NAME] = 10.0  # no Steam anchor for the output (e.g. 90d-only): cash alone is not priced
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert res == [], "an output with no Steam price must not be priced from Skinport alone"


def check_two_wear_weighted_average():
    reset_markets()
    STEAM[IN_NAME] = (1.0, SHORT)
    STEAM["Out (Factory New)"] = (100.0, SHORT)
    STEAM["Out (Minimal Wear)"] = (50.0, SHORT)
    # Output float range 0.0-0.3 with FT inputs lands in [0.045, 0.114]: Factory New and Minimal Wear.
    out = skin("Out", ["Factory New", "Minimal Wear"], lo=0.0, hi=0.3)
    res = scan(skin("Input", ["Field-Tested"]), [out])
    assert len(res) == 1, res
    drop = res[0].possible_drops[0]
    assert set(drop.wear_probs) == {"Factory New", "Minimal Wear"}, drop.wear_probs
    p_fn = drop.wear_probs["Factory New"]
    p_mw = drop.wear_probs["Minimal Wear"]
    expected = (p_fn * 100.0 + p_mw * 50.0) / (p_fn + p_mw)
    assert 50.0 < expected < 100.0, expected
    assert close(drop.price, expected), (drop.price, expected)
    assert close(res[0].expected_revenue_after_fee, expected * 0.87), res[0].expected_revenue_after_fee


def check_ninety_day_only_price_ignored():
    reset_markets()
    STEAM[IN_NAME] = (1.0, SHORT)
    STEAM[OUT_NAME] = (100.0, LONG)  # output priced only from the 90d window -> unpriced -> no row
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert res == [], "a 90d-only output price must not produce a row"
    STEAM[IN_NAME] = (1.0, LONG)  # input priced only from the 90d window -> skipped
    STEAM[OUT_NAME] = (100.0, SHORT)
    res = scan(skin("Input", ["Field-Tested"]), [skin("Out", ["Field-Tested"])])
    assert res == [], "a 90d-only input price must not produce a row"


def main():
    install_fakes()
    check_skinport_beats_high_steam()
    check_buff_lowest_wins()
    check_cash_market_never_raises_revenue()
    check_cash_market_alone_is_not_revenue()
    check_steam_only_flag_restores_old_rule()
    check_cheap_input_disappears()
    check_two_wear_weighted_average()
    check_ninety_day_only_price_ignored()
    print("ok test_honest_roi")


if __name__ == "__main__":
    main()
