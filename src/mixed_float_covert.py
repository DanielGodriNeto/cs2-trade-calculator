"""Unified float-mix + cross-case filler strategy ("mixed float") for the Covert ->
knife/glove trade-up.

Mirrors mixed_float.py -- see that module's docstring for the full rationale (wear-mixing the
primary group's inputs while diluting with a filler group still determines where the primary
group's own outputs land, so searching both together beats running either alone) -- just keyed
by CASE instead of collection and COVERT_INPUT_COUNT (5) total inputs instead of 10.

By default, restricted to trade-ups whose plain (no-strategy) ROI is already >= 0, same as
mixed_float.py, to keep the extra per-wear + cross-case pricing cost bounded to results worth
the scrutiny -- pass require_positive_baseline=False to search every case regardless of its
starting ROI (more requests: every case gets the full search, not just already-working ones).
"""
from dataclasses import dataclass

from . import oddsmath
from . import marketplaces as marketplaces_mod
from .float_mix import _candidate_mixes, _price_per_wear
from .tradeup import _cheapest_wear_price, _price_at_matched_wear, STEAM_SELL_FEE
from .covert import COVERT_INPUT_COUNT

MAX_FILLER_CANDIDATES = 6  # cap how many other cases get priced -- each one costs requests


@dataclass
class MixedFloatCovertResult:
    primary_case: str
    filler_case: str  # "" when the winning combo used no filler at all (pure wear-mix)
    primary_mix: list  # [{"wear": str, "count": int, "unit_price": float}]
    filler_count: int  # 0 means no filler -- the wear-mix alone already beat the baseline
    filler_input: str  # "" when filler_count == 0
    filler_unit_price: float  # 0.0 when filler_count == 0
    cost: float
    expected_revenue: float
    expected_revenue_after_fee: float
    profit: float
    roi_pct: float
    roi_improvement_pct: float  # vs. the plain single-case baseline
    buy_links: dict
    primary_skin_image: str = ""
    filler_skin_image: str = ""  # "" when filler_count == 0


def find_best_mixed_float_strategy(primary_case: str, primary_baseline_roi: float, cases_idx: dict,
                                    stattrak: bool, currency: int = 1, abort_check=None,
                                    fast_mode: bool = False,
                                    max_filler_candidates: int = MAX_FILLER_CANDIDATES,
                                    require_positive_baseline: bool = True) -> "MixedFloatCovertResult | None":
    if require_positive_baseline and primary_baseline_roi < 0:
        return None  # opt-in: skip the extra requests unless the trade already works

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

    primary_prices = _price_per_wear(primary_skin, stattrak, currency, "lowest_price", abort_check)
    if not primary_prices:
        return None

    best = None

    def _consider(primary_mix, filler_case, filler_skin, filler_name, filler_price, filler_wear,
                  n_filler, f_outputs):
        nonlocal best
        n_primary = COVERT_INPUT_COUNT - n_filler
        primary_cost = sum(primary_prices[w] * c for w, c in primary_mix.items())
        cost = primary_cost + filler_price * n_filler
        inputs = [(primary_skin, w, c) for w, c in primary_mix.items()]
        if n_filler:
            inputs.append((filler_skin, filler_wear, n_filler))
        avg_norm = oddsmath.average_normalized_float(inputs)

        if n_filler:
            primary_odds, filler_odds = oddsmath.group_odds(
                [(n_primary, len(primary_outputs)), (n_filler, len(f_outputs))])
        else:
            primary_odds, filler_odds = 100.0 / len(primary_outputs), 0.0

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
        # Full odds mass of every output, priced or not -- see mixed_tradeup.py's copy of this
        # comment and oddsmath's unpriced-outputs section.
        total_prob = primary_odds * len(primary_outputs) + filler_odds * len(f_outputs)
        priced_prob = oddsmath.priced_odds_pct(revenues_weighted)
        if total_prob <= 0 or not oddsmath.fully_priced(priced_prob, total_prob):
            return
        expected_revenue = oddsmath.expected_revenue_full_odds(revenues_weighted, total_prob)
        after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
        profit = after_fee - cost
        roi = (profit / cost * 100) if cost else 0.0
        if best is not None and roi <= best.roi_pct:
            return
        buy_links = {}
        if roi >= 0:
            buy_links = {
                "primary": {w: marketplaces_mod.build_links(primary_skin["name"], w, stattrak) for w in primary_mix},
                "filler": marketplaces_mod.build_links(filler_skin["name"], filler_wear, stattrak) if n_filler else {},
            }
        best = MixedFloatCovertResult(
            primary_case=primary_case, filler_case=filler_case if n_filler else "",
            primary_mix=[{"wear": w, "count": c, "unit_price": primary_prices[w]} for w, c in primary_mix.items()],
            filler_count=n_filler, filler_input=filler_name if n_filler else "",
            filler_unit_price=filler_price if n_filler else 0.0,
            cost=cost, expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
            profit=profit, roi_pct=roi, roi_improvement_pct=roi - primary_baseline_roi,
            buy_links=buy_links,
            primary_skin_image=primary_skin.get("image", ""),
            filler_skin_image=filler_skin.get("image", "") if n_filler else "",
        )

    # Zero-filler candidates first -- pure wear-mixing within the primary case alone. Without
    # this, a case whose best play genuinely is "just float-manipulate, don't dilute" would be
    # missed entirely (see mixed_float.py's docstring for why this matters).
    for primary_mix in _candidate_mixes(primary_skin, primary_prices, COVERT_INPUT_COUNT):
        if abort_check and abort_check():
            break
        _consider(primary_mix, "", None, "", 0.0, "", 0, [])

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
            for primary_mix in _candidate_mixes(primary_skin, primary_prices, n_primary):
                if abort_check and abort_check():
                    break
                _consider(primary_mix, case, filler_skin, filler_name, filler_price, filler_wear,
                          n_filler, f_outputs)

    if best and best.roi_pct > primary_baseline_roi:
        return best
    return None
