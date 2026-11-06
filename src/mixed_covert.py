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
from .float_mix import cheapest_buyable_input, landed_quotes
from .tradeup import _cash_market_maps
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

    found_primary = cheapest_buyable_input(primary_inputs, stattrak, currency, abort_check, fast_mode)
    if found_primary is None:
        return None
    primary_skin, primary_found = found_primary
    primary_price, primary_name, primary_wear = primary_found[:3]  # 4th element (volume) unused here
    cash_maps = _cash_market_maps(currency)

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
        found_filler = cheapest_buyable_input(f_inputs, stattrak, currency, abort_check, fast_mode)
        if found_filler is None:
            continue
        filler_skin, filler_found = found_filler
        filler_price, filler_name, filler_wear = filler_found[:3]  # 4th element (volume) unused here

        for n_filler in range(1, COVERT_INPUT_COUNT):
            if abort_check and abort_check():
                break
            n_primary = COVERT_INPUT_COUNT - n_filler
            cost = n_primary * primary_price + n_filler * filler_price
            primary_odds, filler_odds = oddsmath.group_odds(
                [(n_primary, len(primary_outputs)), (n_filler, len(f_outputs))])
            avg_norm = oddsmath.average_normalized_float([
                (primary_skin, primary_wear, n_primary), (filler_skin, filler_wear, n_filler)])

            # (gross, proceeds, odds) per output priced at its landed wear (honest cash-market rule).
            priced = landed_quotes(primary_outputs, avg_norm, primary_odds, stattrak, currency, cash_maps,
                                   abort_check) + landed_quotes(f_outputs, avg_norm, filler_odds, stattrak,
                                                                currency, cash_maps, abort_check)
            # Full odds mass of both groups' outputs, priced or not -- see mixed_tradeup.py's
            # copy of this comment and oddsmath's unpriced-outputs section.
            total_prob = primary_odds * len(primary_outputs) + filler_odds * len(f_outputs)
            expected = oddsmath.honest_expectation(priced, total_prob)
            if expected is None:
                continue
            expected_revenue, after_fee = expected
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
