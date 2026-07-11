"""Unified float-mix + cross-collection filler strategy ("mixed float") for trade-up contracts.

float_mix.py steers the output wear bracket by deliberately mixing input wears within a
single collection. mixed_tradeup.py steers the output odds by diluting a primary collection
with a cheap filler collection, but only ever buys the filler (and the primary) at its single
cheapest wear. Neither alone explores the combination: mixing input wears *within* the primary
group while also diluting with a filler collection, since the wear split still determines
where the primary group's own outputs land even when a filler group is present.

By default, restricted to trade-ups whose plain (no-strategy) ROI is already >= 0 -- this is an
"can we improve an already-workable trade" search, not an attempt to rescue a losing one, so
the extra request cost (float_mix-style per-wear pricing PLUS mixed_tradeup-style filler-collection
pricing) stays bounded to results worth the scrutiny. Pass require_positive_baseline=False to
search every trade-up regardless of its starting ROI -- useful for finding a mix that turns a
losing trade-up positive, at the cost of noticeably more requests (every collection gets the
full search instead of only ones already known to work).
"""
from dataclasses import dataclass

from . import oddsmath
from . import marketplaces as marketplaces_mod
from . import user_overrides as user_overrides_mod
from .float_mix import _candidate_mixes, _price_per_wear
from .tradeup import _cheapest_wear_price, _price_at_matched_wear, STEAM_SELL_FEE

MAX_FILLER_CANDIDATES = 6  # cap how many other collections get priced -- each one costs requests
MAX_N_FILLER = 4  # today's default filler-slot ceiling (1..4 of the 10); a caller may raise
# this for deeper coverage, same idea as float_mix.py's max_distinct_wears


@dataclass
class MixedFloatResult:
    primary_collection: str
    filler_collection: str  # "" when the winning combo used no filler at all (pure wear-mix)
    primary_mix: list  # [{"wear": str, "count": int, "unit_price": float}]
    filler_count: int  # 0 means no filler -- the wear-mix alone already beat the baseline
    filler_input: str  # "" when filler_count == 0
    filler_unit_price: float  # 0.0 when filler_count == 0
    cost: float
    expected_revenue: float
    expected_revenue_after_fee: float
    profit: float
    roi_pct: float
    roi_improvement_pct: float  # vs. the plain single-collection baseline
    buy_links: dict
    ml_predicted_roi_pct: float | None = None  # learning.py's calibrated prediction, tiebreaker only
    ml_confidence: str | None = None  # "low" | "medium" | "high" by training-set size, or None if unset
    primary_skin_image: str = ""  # direct Steam CDN thumbnail URL -- same for every wear in primary_mix
    filler_skin_image: str = ""  # "" when filler_count == 0


def find_best_mixed_float_strategy(primary_collection: str, primary_baseline_roi: float, idx: dict,
                                    from_id: str, to_id: str, stattrak: bool, currency: int = 1,
                                    abort_check=None, fast_mode: bool = False,
                                    max_filler_candidates: int = MAX_FILLER_CANDIDATES,
                                    require_positive_baseline: bool = True,
                                    max_distinct_wears: int = 2, max_n_filler: int = MAX_N_FILLER,
                                    use_ml: bool = False, from_rarity: str | None = None) -> "MixedFloatResult | None":
    """max_distinct_wears: how many wear brackets a primary-mix candidate may combine (see
    float_mix.py's _candidate_mixes) -- a caller may raise this for deeper coverage.
    max_n_filler: ceiling on filler slots tried (1..max_n_filler of the 10), also
    progressively raised there. use_ml: consult learning.py's trained ROI model as a tiebreaker
    between near-identical formula-ROI candidates -- same "never overrides a clear formula
    winner" rule as float_mix.py, no-op when no model is trained yet."""
    if require_positive_baseline and primary_baseline_roi < 0:
        return None  # opt-in: skip the extra requests unless the trade already works

    primary_inputs = idx.get(primary_collection, {}).get(from_id, [])
    primary_outputs = idx.get(primary_collection, {}).get(to_id, [])
    if not primary_inputs or not primary_outputs:
        return None
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

    use_ml_enabled = use_ml
    if use_ml_enabled:
        from . import learning as learning_mod
        use_ml_enabled = learning_mod.get_model_status()["trained"]
    ML_TIE_EPSILON_PP = 0.5  # formula-ROI gap (percentage points) within which ML breaks the tie

    def _ml_predict(primary_mix, filler_skin, filler_wear, filler_price, n_filler, cost, roi):
        filler_input = None
        if n_filler:
            filler_input = {"skin": filler_skin["name"], "wear": filler_wear, "count": n_filler,
                             "unit_price_paid": filler_price}
        return learning_mod.predict_roi(
            collection=primary_collection, from_rarity=from_rarity,
            input_mix=[{"skin": primary_skin["name"], "wear": w, "count": c,
                        "unit_price_paid": primary_prices[w]} for w, c in primary_mix.items()],
            stattrak=stattrak, cost_paid=cost, predicted_roi_pct=roi, filler_input=filler_input)

    best = None

    def _consider(primary_mix, filler_collection, filler_skin, filler_name, filler_price, filler_wear,
                  n_filler, f_outputs):
        nonlocal best
        n_primary = 10 - n_filler
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
        total_prob = sum(p for _, p in revenues_weighted)
        if total_prob <= 0:
            return
        expected_revenue = sum(v * p for v, p in revenues_weighted) / total_prob
        after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
        profit = after_fee - cost
        roi = (profit / cost * 100) if cost else 0.0

        take_new = best is None or roi > best.roi_pct
        ml_result = None
        if not take_new and use_ml_enabled and best is not None and abs(roi - best.roi_pct) <= ML_TIE_EPSILON_PP:
            ml_result = _ml_predict(primary_mix, filler_skin, filler_wear, filler_price, n_filler, cost, roi)
            if ml_result and (best.ml_predicted_roi_pct is None
                               or ml_result["ml_predicted_roi_pct"] > best.ml_predicted_roi_pct):
                take_new = True
        if not take_new:
            return
        if use_ml_enabled and ml_result is None:
            ml_result = _ml_predict(primary_mix, filler_skin, filler_wear, filler_price, n_filler, cost, roi)

        buy_links = {}
        if roi >= 0:
            buy_links = {
                "primary": {w: marketplaces_mod.build_links(primary_skin["name"], w, stattrak) for w in primary_mix},
                "filler": marketplaces_mod.build_links(filler_skin["name"], filler_wear, stattrak) if n_filler else {},
            }
        best = MixedFloatResult(
            primary_collection=primary_collection, filler_collection=filler_collection if n_filler else "",
            primary_mix=[{"wear": w, "count": c, "unit_price": primary_prices[w]} for w, c in primary_mix.items()],
            filler_count=n_filler, filler_input=filler_name if n_filler else "",
            filler_unit_price=filler_price if n_filler else 0.0,
            cost=cost, expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
            profit=profit, roi_pct=roi, roi_improvement_pct=roi - primary_baseline_roi,
            buy_links=buy_links,
            ml_predicted_roi_pct=ml_result["ml_predicted_roi_pct"] if ml_result else None,
            ml_confidence=ml_result["confidence"] if ml_result else None,
            primary_skin_image=primary_skin.get("image", ""),
            filler_skin_image=filler_skin.get("image", "") if n_filler else "",
        )

    # Zero-filler candidates first -- pure wear-mixing within the primary collection alone,
    # equivalent to what float_mix.py finds on its own. Without this, a collection whose best
    # play genuinely is "just float-manipulate, don't dilute" would be missed entirely (every
    # filler-collection candidate below requires at least 1 filler unit), meaning the unified
    # strategy could show *nothing* even where either original strategy alone would have.
    for primary_mix in _candidate_mixes(primary_skin, primary_prices, 10, max_distinct_wears):
        if abort_check and abort_check():
            break
        _consider(primary_mix, "", None, "", 0.0, "", 0, [])

    blacklisted = user_overrides_mod.blacklisted_skins()
    candidates = []
    for coll, by_rarity in idx.items():
        if coll == primary_collection:
            continue
        f_inputs = [s for s in by_rarity.get(from_id, []) if s["stattrak"] or not stattrak]
        f_outputs = [s for s in by_rarity.get(to_id, []) if s["stattrak"] or not stattrak]
        if stattrak:
            f_inputs = [s for s in f_inputs if s["stattrak"]]
            f_outputs = [s for s in f_outputs if s["stattrak"]]
        if blacklisted:
            # Same user-blacklist rule as mixed_tradeup.find_best_filler_mix: never buy a
            # blacklisted skin as filler, never mix in a collection whose output pool would put
            # a blacklisted skin back among the possible drops.
            f_inputs = [s for s in f_inputs if s["name"] not in blacklisted]
            if any(s["name"] in blacklisted for s in f_outputs):
                continue
        if not f_inputs or not f_outputs:
            continue
        candidates.append((coll, f_inputs, f_outputs))
    candidates = candidates[:max_filler_candidates]

    for coll, f_inputs, f_outputs in candidates:
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

        for n_filler in range(1, min(max_n_filler, 9) + 1):
            if abort_check and abort_check():
                break
            n_primary = 10 - n_filler
            for primary_mix in _candidate_mixes(primary_skin, primary_prices, n_primary, max_distinct_wears):
                if abort_check and abort_check():
                    break
                _consider(primary_mix, coll, filler_skin, filler_name, filler_price, filler_wear,
                          n_filler, f_outputs)

    if best and best.roi_pct > primary_baseline_roi:
        return best
    return None
