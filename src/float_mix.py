"""Float-mix ("float manipulation") strategy for trade-up contracts.

A trade-up's output float is the average of the 10 input floats, each normalized within its
own skin's min/max range, then re-applied to the output skin's own min/max range. Real
traders exploit this: buying a deliberate mix of input wear conditions (rather than
whatever's cheapest) can steer the output into a more valuable wear bracket than you'd land
in by chance.

This searches over candidate input mixes (every single wear, plus every combination of up to
`max_distinct_wears` wear brackets at every count split -- n is small, so brute force is
instant up to a couple of wears; default max_distinct_wears=2 matches the original pair-only
search), computes each
candidate's resulting average normalized float via oddsmath, remaps that into EVERY possible
output skin's own float range (skins with a narrow range land differently than ones spanning
the full 0-1), and averages revenue across whichever outputs are reachable -- then reports the
best-ROI mix per distinct wear bracket it can land the output in.

APPROXIMATION: we don't have exact per-listing float values (Steam's public API doesn't
expose them), only wear-condition buckets. This module approximates each wear condition by
the midpoint of its standard float range (Factory New 0.00-0.07, ..., Battle-Scarred
0.45-1.00). Treat results as a strategy worth verifying (e.g. with a float-checking site)
before buying.
"""
import itertools
from dataclasses import dataclass, field

from . import oddsmath
from .oddsmath import WEAR_BREAKPOINTS, WEAR_MIDPOINT  # re-exported: tradeup.py reads these off this module
from .steam_prices import get_price, market_hash_name


@dataclass
class FloatMixResult:
    target_wear: str
    input_mix: list  # [{"wear": str, "count": int, "unit_price": float}]
    input_cost: float
    expected_revenue: float
    expected_revenue_after_fee: float
    profit: float
    roi_pct: float
    priced_output_variants: int
    n_output_variants: int
    ml_predicted_roi_pct: float | None = None  # learning.py's calibrated prediction, tiebreaker only
    ml_confidence: str | None = None  # "low" | "medium" | "high" by training-set size, or None if unset
    ml_roi_range: list | None = None  # [lo, hi] 80% interval from the model's own held-out residuals, or None


def _price_per_wear(skin: dict, stattrak: bool, currency: int, price_field: str,
                     abort_check=None) -> dict:
    """{wear_name: price}, using price_field ('lowest_price' for buying, 'median_price'
    for selling), skipping wears with no listing data."""
    out = {}
    for wear in skin["wears"]:
        if abort_check and abort_check():
            break
        name = market_hash_name(skin["name"], wear, stattrak)
        price = get_price(name, currency, abort_check)
        if not price:
            continue
        val = price.get(price_field) or price.get("lowest_price")
        if val is not None:
            out[wear] = val
    return out


def _compositions(n: int, k: int):
    """Yields every tuple of k positive ints summing to n (stars-and-bars: choose k-1 cut
    points among 1..n-1). Each position in the tuple lines up with the caller's fixed wear
    ordering, so this only needs to enumerate the split sizes, not which wear gets which."""
    if k == 1:
        yield (n,)
        return
    for cuts in itertools.combinations(range(1, n), k - 1):
        parts = []
        prev = 0
        for c in cuts:
            parts.append(c - prev)
            prev = c
        parts.append(n - prev)
        yield tuple(parts)


def _candidate_mixes(input_skin: dict, wear_prices: dict, n: int = 10,
                      max_distinct_wears: int = 2) -> list[dict]:
    """Every combination of up to max_distinct_wears distinct wear brackets, at every count
    split summing to n. max_distinct_wears=2 (the default, matching this module's original
    single-wear/pair-only search) keeps this brute force instant; a caller may raise it for
    deeper coverage -- growth is combinatorial
    (C(len(wears), k) combinations x compositions of n into k positive parts per k), so higher
    values cost meaningfully more per call. n is small (10, or 5 for Covert trade-ups)."""
    wears = sorted(wear_prices.keys(), key=lambda w: oddsmath.normalized_float(input_skin, w))
    max_k = min(max_distinct_wears, len(wears))
    mixes = []
    for k in range(1, max_k + 1):
        for combo in itertools.combinations(wears, k):
            if k == 1:
                mixes.append({combo[0]: n})
                continue
            for counts in _compositions(n, k):
                mixes.append(dict(zip(combo, counts)))
    return mixes


def evaluate_float_strategies(input_skin: dict, outputs: list, stattrak: bool, currency: int,
                               abort_check=None, fee: float = 0.13, n: int = 10,
                               max_distinct_wears: int = 2, use_ml: bool = False,
                               collection: str | None = None, from_rarity: str | None = None) -> list[FloatMixResult]:
    """For the given input skin and the pool of possible output skins, finds the cheapest
    input wear-mix that lands the output in each achievable wear bracket, and the resulting
    expected revenue there. Requires per-wear pricing for the input skin and every output
    skin (more requests than the default scan, hence opt-in). n=10 for regular trade-ups,
    n=5 for Covert -> knife/glove. max_distinct_wears controls how many wear brackets a
    candidate mix may combine (see _candidate_mixes). use_ml: also consult learning.py's
    trained ROI model as a tiebreaker when two candidates land the same wear bracket with
    near-identical formula ROI (never overrides a clear formula-ROI winner) -- no-op (and
    cheap: a single JSON status read, not a model load) when no model is trained yet."""
    input_prices = _price_per_wear(input_skin, stattrak, currency, "lowest_price", abort_check)
    if not input_prices:
        return []

    output_price_maps = [_price_per_wear(skin, stattrak, currency, "median_price", abort_check)
                          for skin in outputs]

    use_ml = use_ml and collection is not None
    if use_ml:
        from . import learning as learning_mod
        use_ml = learning_mod.get_model_status()["trained"]

    def _ml_predict(mix: dict, cost: float, roi: float) -> dict | None:
        return learning_mod.predict_roi(
            collection=collection, from_rarity=from_rarity,
            input_mix=[{"skin": input_skin["name"], "wear": w, "count": c,
                        "unit_price_paid": input_prices[w]} for w, c in mix.items()],
            stattrak=stattrak, cost_paid=cost, predicted_roi_pct=roi)

    ML_TIE_EPSILON_PP = 0.5  # formula-ROI gap (percentage points) within which ML breaks the tie

    best_by_wear: dict[str, FloatMixResult] = {}
    for mix in _candidate_mixes(input_skin, input_prices, n, max_distinct_wears):
        cost = sum(input_prices[w] * c for w, c in mix.items())
        avg_norm = oddsmath.average_normalized_float([(input_skin, w, c) for w, c in mix.items()])

        revenues = []
        landed_counts: dict[str, int] = {}
        unpriced = False
        for skin, price_map in zip(outputs, output_price_maps):
            landed_wear = oddsmath.remap_float_to_wear(skin, avg_norm)
            if landed_wear not in price_map:
                # This output has no listing at the wear this mix would land it in. Skipping it
                # and averaging the rest is what made float-mixing look like free ROI: steering
                # the landed wear into a bracket where the cheap outputs happen to have no
                # listings doesn't remove those outcomes from the contract, it only removes them
                # from the average. See oddsmath's unpriced-outputs section.
                unpriced = True
                break
            revenues.append(price_map[landed_wear])
            landed_counts[landed_wear] = landed_counts.get(landed_wear, 0) + 1
        if unpriced or not revenues:
            continue

        expected_revenue = sum(revenues) / len(outputs)
        after_fee = expected_revenue * (1 - fee)
        profit = after_fee - cost
        roi = (profit / cost * 100) if cost else 0.0
        dominant_wear = max(landed_counts, key=landed_counts.get)

        cur = best_by_wear.get(dominant_wear)
        take_new = cur is None or roi > cur.roi_pct
        ml_result = None
        if not take_new and use_ml and cur is not None and abs(roi - cur.roi_pct) <= ML_TIE_EPSILON_PP:
            ml_result = _ml_predict(mix, cost, roi)
            if ml_result and (cur.ml_predicted_roi_pct is None
                               or ml_result["ml_predicted_roi_pct"] > cur.ml_predicted_roi_pct):
                take_new = True
        if take_new:
            if use_ml and ml_result is None:
                ml_result = _ml_predict(mix, cost, roi)
            best_by_wear[dominant_wear] = FloatMixResult(
                target_wear=dominant_wear,
                input_mix=[{"wear": w, "count": c, "unit_price": input_prices[w]} for w, c in mix.items()],
                input_cost=cost,
                expected_revenue=expected_revenue,
                expected_revenue_after_fee=after_fee,
                profit=profit,
                roi_pct=roi,
                priced_output_variants=len(revenues),
                n_output_variants=len(outputs),
                ml_predicted_roi_pct=ml_result["ml_predicted_roi_pct"] if ml_result else None,
                ml_confidence=ml_result["confidence"] if ml_result else None,
                ml_roi_range=ml_result["predicted_roi_range"] if ml_result else None,
            )
    return sorted(best_by_wear.values(), key=lambda r: r.roi_pct, reverse=True)
