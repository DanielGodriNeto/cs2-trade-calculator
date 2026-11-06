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
_USE_PRICE_MEMO = True  # per-call memo of output price lookups inside find_best_mixed_float_strategy


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
    ml_roi_range: list | None = None  # [lo, hi] 80% interval from the model's own held-out residuals, or None
    primary_skin_image: str = ""  # direct Steam CDN thumbnail URL -- same for every wear in primary_mix
    filler_skin_image: str = ""  # "" when filler_count == 0


@dataclass(frozen=True)
class _FillerSlot:
    """One filler-collection choice at one filler count: everything _consider needs that does not
    depend on the primary wear mix. n_filler=0 is the pure wear-mix slot (no filler at all)."""
    collection: str
    skin: dict | None
    name: str
    price: float
    wear: str
    norm: float  # oddsmath.normalized_float of the filler input at its wear
    n_filler: int
    outputs: list  # [(skin, _skin_key(skin)), ...]
    primary_odds: float
    filler_odds: float


def _avg_norm(terms: list[tuple[float, int]]) -> float:
    """oddsmath.average_normalized_float on precomputed (normalized_float, count) pairs. Same
    arithmetic in the same order, so the result is bit-identical; it only avoids resolving each
    input's normalized float again for every candidate mix."""
    total = sum(c for _, c in terms)
    if not total:
        return 0.5
    return sum(v * c for v, c in terms) / total


def _skin_key(skin: dict) -> tuple:
    """Everything an output's landed wear and price lookup depend on, so memo keys built from it
    are exact (two skins with equal keys always price and remap identically)."""
    return (skin["name"], tuple(skin["wears"]), skin.get("min_float", 0.0), skin.get("max_float", 1.0))


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
    primary_norm = {w: oddsmath.normalized_float(primary_skin, w) for w in primary_prices}

    use_ml_enabled = use_ml
    if use_ml_enabled:
        from . import learning as learning_mod
        use_ml_enabled = learning_mod.get_model_status()["trained"]
    ML_TIE_EPSILON_PP = 0.5  # formula-ROI gap (percentage points) within which ML breaks the tie

    def _ml_predict(primary_mix, slot: _FillerSlot, cost, roi):
        filler_input = None
        if slot.n_filler:
            filler_input = {"skin": slot.skin["name"], "wear": slot.wear, "count": slot.n_filler,
                             "unit_price_paid": slot.price}
        return learning_mod.predict_roi(
            collection=primary_collection, from_rarity=from_rarity,
            input_mix=[{"skin": primary_skin["name"], "wear": w, "count": c,
                        "unit_price_paid": primary_prices[w]} for w, c in primary_mix.items()],
            stattrak=stattrak, cost_paid=cost, predicted_roi_pct=roi, filler_input=filler_input)

    best = None
    price_memo: dict = {}
    primary_keyed = [(s, _skin_key(s)) for s in primary_outputs]
    mixes_by_n: dict[int, list[dict]] = {}  # the same primary's mixes for each n_primary, built once per call

    def _mixes(n: int) -> list[dict]:
        if n not in mixes_by_n:
            mixes_by_n[n] = _candidate_mixes(primary_skin, primary_prices, n, max_distinct_wears)
        return mixes_by_n[n]

    def _output_price(skin: dict, key: tuple, avg_norm: float) -> float | None:
        # Every candidate mix re-prices the same (output skin, landed wear) pairs. The lookup is
        # deterministic for this call (stattrak and currency are fixed), so cache it for this call
        # only (no cross-call state). `key` is _skin_key(skin): the lookup depends only on it.
        landed_wear = oddsmath.remap_float_to_wear(skin, avg_norm)
        if not _USE_PRICE_MEMO:
            return _price_at_matched_wear(skin, landed_wear, stattrak, currency, abort_check)
        memo_key = (key, landed_wear)
        if memo_key not in price_memo:
            price_memo[memo_key] = _price_at_matched_wear(skin, landed_wear, stattrak, currency, abort_check)
        return price_memo[memo_key]

    def _consider(primary_mix: dict, slot: _FillerSlot) -> None:
        nonlocal best
        n_filler = slot.n_filler
        primary_cost = sum(primary_prices[w] * c for w, c in primary_mix.items())
        cost = primary_cost + slot.price * n_filler
        terms = [(primary_norm[w], c) for w, c in primary_mix.items()]
        if n_filler:
            terms.append((slot.norm, n_filler))
        avg_norm = _avg_norm(terms)

        revenues_weighted = []
        for skin, key in primary_keyed:
            price_val = _output_price(skin, key, avg_norm)
            if price_val is not None:
                revenues_weighted.append((price_val, slot.primary_odds))
        for skin, key in slot.outputs:
            price_val = _output_price(skin, key, avg_norm)
            if price_val is not None:
                revenues_weighted.append((price_val, slot.filler_odds))
        # Full odds mass of every output, priced or not -- see mixed_tradeup.py's copy of this
        # comment and oddsmath's unpriced-outputs section. This search combines wear-mixing with
        # filler dilution, so it stacks BOTH ways of accidentally dropping outputs out of the
        # average: a filler collection's thin outputs, and outputs with no listing at the wear
        # the mix steers them into.
        total_prob = slot.primary_odds * len(primary_outputs) + slot.filler_odds * len(slot.outputs)
        priced_prob = oddsmath.priced_odds_pct(revenues_weighted)
        if total_prob <= 0 or not oddsmath.fully_priced(priced_prob, total_prob):
            return
        expected_revenue = oddsmath.expected_revenue_full_odds(revenues_weighted, total_prob)
        after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
        profit = after_fee - cost
        roi = (profit / cost * 100) if cost else 0.0

        take_new = best is None or roi > best.roi_pct
        ml_result = None
        if not take_new and use_ml_enabled and best is not None and abs(roi - best.roi_pct) <= ML_TIE_EPSILON_PP:
            ml_result = _ml_predict(primary_mix, slot, cost, roi)
            if ml_result and (best.ml_predicted_roi_pct is None
                               or ml_result["ml_predicted_roi_pct"] > best.ml_predicted_roi_pct):
                take_new = True
        if not take_new:
            return
        if use_ml_enabled and ml_result is None:
            ml_result = _ml_predict(primary_mix, slot, cost, roi)

        buy_links = {}
        if roi >= 0:
            buy_links = {
                "primary": {w: marketplaces_mod.build_links(primary_skin["name"], w, stattrak) for w in primary_mix},
                "filler": marketplaces_mod.build_links(slot.skin["name"], slot.wear, stattrak) if n_filler else {},
            }
        best = MixedFloatResult(
            primary_collection=primary_collection, filler_collection=slot.collection if n_filler else "",
            primary_mix=[{"wear": w, "count": c, "unit_price": primary_prices[w]} for w, c in primary_mix.items()],
            filler_count=n_filler, filler_input=slot.name if n_filler else "",
            filler_unit_price=slot.price if n_filler else 0.0,
            cost=cost, expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
            profit=profit, roi_pct=roi, roi_improvement_pct=roi - primary_baseline_roi,
            buy_links=buy_links,
            ml_predicted_roi_pct=ml_result["ml_predicted_roi_pct"] if ml_result else None,
            ml_confidence=ml_result["confidence"] if ml_result else None,
            ml_roi_range=ml_result["predicted_roi_range"] if ml_result else None,
            primary_skin_image=primary_skin.get("image", ""),
            filler_skin_image=slot.skin.get("image", "") if n_filler else "",
        )

    # Zero-filler candidates first -- pure wear-mixing within the primary collection alone,
    # equivalent to what float_mix.py finds on its own. Without this, a collection whose best
    # play genuinely is "just float-manipulate, don't dilute" would be missed entirely (every
    # filler-collection candidate below requires at least 1 filler unit), meaning the unified
    # strategy could show *nothing* even where either original strategy alone would have.
    zero_slot = _FillerSlot(collection="", skin=None, name="", price=0.0, wear="", norm=0.0, n_filler=0,
                            outputs=[], primary_odds=100.0 / len(primary_outputs), filler_odds=0.0)
    for primary_mix in _mixes(10):
        if abort_check and abort_check():
            break
        _consider(primary_mix, zero_slot)

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
        filler_norm = oddsmath.normalized_float(filler_skin, filler_wear)
        f_keyed = [(s, _skin_key(s)) for s in f_outputs]

        for n_filler in range(1, min(max_n_filler, 9) + 1):
            if abort_check and abort_check():
                break
            n_primary = 10 - n_filler
            primary_odds, filler_odds = oddsmath.group_odds(
                [(n_primary, len(primary_outputs)), (n_filler, len(f_outputs))])
            slot = _FillerSlot(collection=coll, skin=filler_skin, name=filler_name, price=filler_price,
                               wear=filler_wear, norm=filler_norm, n_filler=n_filler, outputs=f_keyed,
                               primary_odds=primary_odds, filler_odds=filler_odds)
            for primary_mix in _mixes(n_primary):
                if abort_check and abort_check():
                    break
                _consider(primary_mix, slot)

    if best and best.roi_pct > primary_baseline_roi:
        return best
    return None
