"""Self-check for the honest revenue rule and junk-input floor in the mixed/covert strategy searches.

1. A mixed-float result values each output at the lowest after-fee proceeds (Skinport below Steam
   pays out less), the same rule as tradeup.scan_collection.
2. The mixed-tradeup and covert searches use the same proceeds rule.
3. A junk-priced input (below MIN_INPUT_PRICE) never enters a mixed-float candidate, primary or filler.
4. oddsmath.remap_float_to_wear returns exactly what the original bisect version returns, on 10k random
   floats and on every exact wear-edge boundary.

Hermetic: Steam, Skinport, Buff163, the wear-anomaly oracle and the blacklist are in-memory fakes.

Run: python test_honest_mixed.py
"""
import math
import random
from bisect import bisect_right

from src import bulk_prices
from src import covert as covert_mod
from src import data as data_mod
from src import float_mix as float_mix_mod
from src import mixed_float as mixed_float_mod
from src import mixed_tradeup as mixed_tradeup_mod
from src import oddsmath
from src import tradeup as tu
from src import user_overrides as user_overrides_mod
from src import wear_anomaly as wear_anomaly_mod
from src.steam_prices import market_hash_name

SHORT = "csgotrader:last_24h"
LONG = "csgotrader:last_90d"
FT = "Field-Tested"

STEAM: dict = {}  # market_hash_name -> (price, source tag)
SKINPORT: dict = {}

FROM_ID = data_mod.RARITY_CHAIN[0][0]
TO_ID = data_mod.RARITY_CHAIN[1][0]


def fake_get_price(name, currency=1, abort_check=None):
    rec = STEAM.get(name)
    if rec is None:
        return None
    price, source = rec
    return {"lowest_price": price, "median_price": price, "volume": 40, "source": source}


def install_fakes():
    for module in (tu, float_mix_mod):
        module.get_price = fake_get_price
    bulk_prices.get_skinport_map = lambda currency=1: SKINPORT
    bulk_prices.get_buff163_map = lambda currency=1: {}
    bulk_prices.rate_for = lambda currency=1: 1.0
    wear_anomaly_mod.is_flagged = lambda name, currency=1: False
    wear_anomaly_mod.check_output_price = lambda *args, **kwargs: None
    user_overrides_mod.blacklisted_skins = lambda: set()


def reset_markets():
    STEAM.clear()
    SKINPORT.clear()


def skin(name, wears, lo=0.0, hi=1.0):
    return {"name": name, "wears": wears, "stattrak": False, "min_float": lo, "max_float": hi, "image": ""}


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def set_price(skin_name, price, source=SHORT, wear=FT):
    STEAM[market_hash_name(skin_name, wear, False)] = (price, source)


def check_mixed_float_uses_lowest_proceeds():
    reset_markets()
    set_price("Input", 1.0)
    set_price("Out", 10.0)  # Steam: 10 -> 8.70 after its 13% fee
    SKINPORT[market_hash_name("Out", FT, False)] = 9.0  # Skinport: 9 -> 8.28 after 8%, the lower proceeds
    idx = {"P": {FROM_ID: [skin("Input", [FT])], TO_ID: [skin("Out", [FT])]}}
    res = mixed_float_mod.find_best_mixed_float_strategy(
        "P", -100.0, idx, FROM_ID, TO_ID, False, require_positive_baseline=False)
    assert res is not None, "a single-output contract must still produce a mixed-float row"
    assert close(res.expected_revenue_after_fee, 8.28), res.expected_revenue_after_fee
    assert close(res.expected_revenue, 9.0), res.expected_revenue


def check_mixed_tradeup_uses_lowest_proceeds():
    reset_markets()
    set_price("Input", 1.0)
    set_price("FIn", 1.0)
    set_price("Out", 10.0)
    set_price("FOut", 10.0)
    SKINPORT[market_hash_name("Out", FT, False)] = 9.0
    SKINPORT[market_hash_name("FOut", FT, False)] = 9.0
    idx = {
        "P": {FROM_ID: [skin("Input", [FT])], TO_ID: [skin("Out", [FT])]},
        "F": {FROM_ID: [skin("FIn", [FT])], TO_ID: [skin("FOut", [FT])]},
    }
    res = mixed_tradeup_mod.find_best_filler_mix("P", -100.0, idx, FROM_ID, TO_ID, False)
    assert res is not None, "a filler contract must still produce a row"
    assert close(res.expected_revenue_after_fee, 8.28), res.expected_revenue_after_fee


def check_covert_uses_lowest_proceeds():
    reset_markets()
    set_price("In", 1.0)
    set_price("Knife", 100.0)
    SKINPORT[market_hash_name("Knife", FT, False)] = 10.0  # 9.20 after 8%, below Steam's 87.00
    case = {"covert": [skin("In", [FT])], "knives_gloves": [skin("Knife", [FT])]}
    res = covert_mod.scan_case("Case A", case, stattrak=False, currency=1)
    assert res is not None, "a covert contract must still produce a row"
    assert close(res.expected_revenue_after_fee, 9.2), res.expected_revenue_after_fee
    assert close(res.cost, 5.0), res.cost


def check_junk_input_never_enters_mixed_float():
    reset_markets()
    set_price("Junk", 0.20)  # cheapest, but below MIN_INPUT_PRICE ($0.50): must not be the primary
    set_price("Good", 1.00)
    set_price("Out", 10.0)
    idx = {"P": {FROM_ID: [skin("Junk", [FT]), skin("Good", [FT])], TO_ID: [skin("Out", [FT])]}}
    res = mixed_float_mod.find_best_mixed_float_strategy(
        "P", -100.0, idx, FROM_ID, TO_ID, False, require_positive_baseline=False)
    assert res is not None, "the floor must leave a usable primary input"
    prices = [leg["unit_price"] for leg in res.primary_mix]
    assert prices and all(close(p, 1.00) for p in prices), prices


def check_junk_filler_never_enters_mixed_float():
    reset_markets()
    set_price("Input", 1.0)
    set_price("Out", 10.0)
    set_price("FJunk", 0.20)  # the cheapest filler by far, but junk
    set_price("FGood", 2.00)
    set_price("FOut", 10.0)
    idx = {
        "P": {FROM_ID: [skin("Input", [FT])], TO_ID: [skin("Out", [FT])]},
        "F": {FROM_ID: [skin("FJunk", [FT]), skin("FGood", [FT])], TO_ID: [skin("FOut", [FT])]},
    }
    res = mixed_float_mod.find_best_mixed_float_strategy(
        "P", -100.0, idx, FROM_ID, TO_ID, False, require_positive_baseline=False)
    assert res is not None, "a filler strategy must still exist"
    assert not close(res.filler_unit_price, 0.20), f"junk filler used: {res.filler_unit_price}"
    assert res.filler_count == 0 or close(res.filler_unit_price, 2.00), res.filler_unit_price


# --- oddsmath.remap_float_to_wear: bit-for-bit against the original bisect implementation ---------

_UPPER_EDGES = [hi for _, _, hi in oddsmath.WEAR_BREAKPOINTS]


def ref_remap(output_skin: dict, avg_normalized: float) -> str:
    lo, hi = output_skin.get("min_float", 0.0), output_skin.get("max_float", 1.0)
    target_float = lo + avg_normalized * (hi - lo)
    if target_float < 0:
        return oddsmath.WEAR_BREAKPOINTS[-1][0]
    idx = bisect_right(_UPPER_EDGES, target_float)
    return oddsmath.WEAR_BREAKPOINTS[min(idx, len(oddsmath.WEAR_BREAKPOINTS) - 1)][0]


def check_remap_equivalence():
    rng = random.Random(20261105)
    los = [0.0, 0.06, 0.15, -0.1, 0.0]
    his = [1.0, 0.8, 0.5, 1.2, 0.3, 0.0]
    skins = [{"min_float": rng.choice(los), "max_float": rng.choice(his)} for _ in range(200)]
    skins += [{"min_float": 0.0, "max_float": 1.0}, {"min_float": 0.06, "max_float": 0.8}, {"min_float": 0.5, "max_float": 0.5}]
    checked = 0
    for _ in range(10000):
        skin_ = rng.choice(skins)
        avg = rng.uniform(-0.3, 1.4)
        assert oddsmath.remap_float_to_wear(skin_, avg) == ref_remap(skin_, avg), (skin_, avg)
        checked += 1
    for skin_ in skins:
        lo, hi = skin_["min_float"], skin_["max_float"]
        span = hi - lo
        if not span:
            continue
        for edge in (0.0, 0.07, 0.15, 0.38, 0.45, 1.0):
            centre = (edge - lo) / span
            for step in range(-3, 4):
                avg = centre
                for _ in range(abs(step)):
                    avg = math.nextafter(avg, math.inf if step > 0 else -math.inf)
                assert oddsmath.remap_float_to_wear(skin_, avg) == ref_remap(skin_, avg), (skin_, edge, avg)
                checked += 1
    for avg in (-math.inf, math.inf, 0.0, 1.0, -0.2, 1.3):
        for skin_ in skins:
            assert oddsmath.remap_float_to_wear(skin_, avg) == ref_remap(skin_, avg), (skin_, avg)
            checked += 1
    assert checked > 10000, checked


def main():
    install_fakes()
    check_mixed_float_uses_lowest_proceeds()
    check_mixed_tradeup_uses_lowest_proceeds()
    check_covert_uses_lowest_proceeds()
    check_junk_input_never_enters_mixed_float()
    check_junk_filler_never_enters_mixed_float()
    check_remap_equivalence()
    print("ok test_honest_mixed")


if __name__ == "__main__":
    main()
