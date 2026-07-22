"""Cross-case "mixed input" search for the Covert -> knife/glove trade-up (filler Covert
skins from a second case).

Mirrors mixed_tradeup.py's cross-collection filler search -- same group-based odds math from
oddsmath.py -- just keyed by CASE instead of collection and COVERT_INPUT_COUNT (5) total
inputs instead of 10. Diluting a primary case's Covert inputs with a cheap filler Covert skin
from a second case's own pool can steer group odds away from a primary case's worst
knife/glove outcomes, same idea as the regular trade-up version, smaller n.
"""
from dataclasses import dataclass

from . import oddsmath
from . import marketplaces as marketplaces_mod
from .tradeup import _cheapest_wear_price, _price_at_matched_wear, STEAM_SELL_FEE
from .covert import COVERT_INPUT_COUNT

MAX_FILLER_CANDIDATES = 6  # cap how many other cases get priced -- each one costs requests


@dataclass
class MixedCovertResult:
    primary_case: str
    filler_case: str
    primary_count: int
    filler_count: int
    primary_input: str
    filler_input: str
    cost: float
    expected_revenue: float
    expected_revenue_after_fee: float
    profit: float
    roi_pct: float
    roi_improvement_pct: float  # vs. the pure single-case baseline
    buy_links: dict
    primary_input_image: str = ""
    filler_input_image: str = ""


def find_best_filler_mix(primary_case: str, primary_baseline_roi: float, cases_idx: dict,
                          stattrak: bool, currency: int = 1, abort_check=None, fast_mode: bool = False,
                          max_filler_candidates: int = MAX_FILLER_CANDIDATES) -> "MixedCovertResult | None":
    primary_data = cases_idx.get(primary_case)
    if not primary_data:
        return None
    primary_inputs = primary_data["covert"]
    primary_outputs = primary_data["knives_gloves"]
    if stattrak:
        primary_inputs = [s for s in primary_inputs if s["stattrak"]]
        primary_outputs = [s for s in primary_outputs if s["stattrak"]]
        if not primary_inputs or not primary_outputs:
            return None

    primary_cheapest = None
    primary_skin = None
    for skin in primary_inputs:
        if abort_check and abort_check():
            return None
        found = _cheapest_wear_price(skin, stattrak, currency, abort_check, fast_mode)
        if found and (primary_cheapest is None or found[0] < primary_cheapest[0]):
            primary_cheapest = found
            primary_skin = skin
    if primary_cheapest is None:
        return None
    primary_price, primary_name, primary_wear = primary_cheapest[:3]  # 4th element (volume) unused here

    candidates = []
    for case, case_data in cases_idx.items():
        if case == primary_case:
            continue
        f_inputs = case_data["covert"]
        f_outputs = case_data["knives_gloves"]
        if stattrak:
            f_inputs = [s for s in f_inputs if s["stattrak"]]
            f_outputs = [s for s in f_outputs if s["stattrak"]]
        if not f_inputs or not f_outputs:
            continue
        candidates.append((case, f_inputs, f_outputs))
    candidates = candidates[:max_filler_candidates]

    best = None
    for case, f_inputs, f_outputs in candidates:
        if abort_check and abort_check():
            break
        filler_cheapest = None
        filler_skin = None
        for skin in f_inputs:
            found = _cheapest_wear_price(skin, stattrak, currency, abort_check, fast_mode)
            if found and (filler_cheapest is None or found[0] < filler_cheapest[0]):
                filler_cheapest = found
                filler_skin = skin
        if filler_cheapest is None:
            continue
        filler_price, filler_name, filler_wear = filler_cheapest[:3]  # 4th element (volume) unused here

        for n_filler in range(1, COVERT_INPUT_COUNT):
            if abort_check and abort_check():
                break
            n_primary = COVERT_INPUT_COUNT - n_filler
            cost = n_primary * primary_price + n_filler * filler_price
            primary_odds, filler_odds = oddsmath.group_odds(
                [(n_primary, len(primary_outputs)), (n_filler, len(f_outputs))])
            avg_norm = oddsmath.average_normalized_float([
                (primary_skin, primary_wear, n_primary), (filler_skin, filler_wear, n_filler)])

            revenues_weighted = []
            for skin in primary_outputs:
                landed_wear = oddsmath.remap_float_to_wear(skin, avg_norm)
                price_val = _price_at_matched_wear(skin, landed_wear, stattrak, currency, abort_check)
                if price_val is not None:
                    revenues_weighted.append((price_val, primary_odds))
            for skin in f_outputs:
                landed_wear = oddsmath.remap_float_to_wear(skin, avg_norm)
                price_val = _price_at_matched_wear(skin, landed_wear, stattrak, currency, abort_check)
                if price_val is not None:
                    revenues_weighted.append((price_val, filler_odds))
            # Full odds mass of both groups' outputs, priced or not -- see mixed_tradeup.py's
            # copy of this comment and oddsmath's unpriced-outputs section.
            total_prob = primary_odds * len(primary_outputs) + filler_odds * len(f_outputs)
            priced_prob = oddsmath.priced_odds_pct(revenues_weighted)
            if total_prob <= 0 or not oddsmath.fully_priced(priced_prob, total_prob):
                continue
            expected_revenue = oddsmath.expected_revenue_full_odds(revenues_weighted, total_prob)
            after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
            profit = after_fee - cost
            roi = (profit / cost * 100) if cost else 0.0
            if best is None or roi > best.roi_pct:
                best = MixedCovertResult(
                    primary_case=primary_case, filler_case=case,
                    primary_count=n_primary, filler_count=n_filler,
                    primary_input=primary_name, filler_input=filler_name, cost=cost,
                    expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
                    profit=profit, roi_pct=roi, roi_improvement_pct=roi - primary_baseline_roi,
                    buy_links={
                        "primary": marketplaces_mod.build_links(primary_skin["name"], primary_wear, stattrak),
                        "filler": marketplaces_mod.build_links(filler_skin["name"], filler_wear, stattrak),
                    } if roi >= 0 else {},
                    primary_input_image=primary_skin.get("image", ""),
                    filler_input_image=filler_skin.get("image", ""),
                )

    if best and best.roi_pct > primary_baseline_roi:
        return best
    return None
