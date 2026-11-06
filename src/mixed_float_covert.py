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
from .float_mix import _candidate_mixes, buyable_prices_per_wear, cheapest_buyable_input
from .mixed_float import _avg_norm, _FillerSlot, _skin_key
from .tradeup import _cash_market_maps, _honest_market_quote
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

    found_primary = cheapest_buyable_input(primary_inputs, stattrak, currency, abort_check, fast_mode)
    if found_primary is None:
        return None
    primary_skin = found_primary[0]

    primary_prices = buyable_prices_per_wear(primary_skin, stattrak, currency, abort_check)
    if not primary_prices:
        return None
    primary_norm = {w: oddsmath.normalized_float(primary_skin, w) for w in primary_prices}

    best = None
    price_memo: dict = {}
    cash_maps = _cash_market_maps(currency)
    primary_keyed = [(s, _skin_key(s)) for s in primary_outputs]
    mixes_by_n: dict[int, list[dict]] = {}  # the same primary's mixes for each n_primary, built once per call

    def _mixes(n: int) -> list[dict]:
        if n not in mixes_by_n:
            mixes_by_n[n] = _candidate_mixes(primary_skin, primary_prices, n)
        return mixes_by_n[n]

    def _output_quote(skin: dict, key: tuple, avg_norm: float) -> tuple[float, float] | None:
        # Same per-call memo as mixed_float.py: stattrak, currency and cash maps are fixed for the
        # call, and the honest quote depends only on the skin's key and the landed wear.
        landed_wear = oddsmath.remap_float_to_wear(skin, avg_norm)
        memo_key = (key, landed_wear)
        if memo_key not in price_memo:
            quote = _honest_market_quote(skin, landed_wear, stattrak, currency, cash_maps, abort_check)
            price_memo[memo_key] = None if quote is None else (quote.gross, quote.proceeds)
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

        priced = []  # (gross, proceeds, odds) per output that has a quote
        for skin, key in primary_keyed:
            quote = _output_quote(skin, key, avg_norm)
            if quote is not None:
                priced.append((quote[0], quote[1], slot.primary_odds))
        for skin, key in slot.outputs:
            quote = _output_quote(skin, key, avg_norm)
            if quote is not None:
                priced.append((quote[0], quote[1], slot.filler_odds))
        # Full odds mass of every output, priced or not -- see oddsmath.honest_expectation.
        total_prob = slot.primary_odds * len(primary_outputs) + slot.filler_odds * len(slot.outputs)
        expected = oddsmath.honest_expectation(priced, total_prob)
        if expected is None:
            return
        expected_revenue, after_fee = expected
        profit = after_fee - cost
        roi = (profit / cost * 100) if cost else 0.0
        if best is not None and roi <= best.roi_pct:
            return
        buy_links = {}
        if roi >= 0:
            buy_links = {
                "primary": {w: marketplaces_mod.build_links(primary_skin["name"], w, stattrak) for w in primary_mix},
                "filler": marketplaces_mod.build_links(slot.skin["name"], slot.wear, stattrak) if n_filler else {},
            }
        best = MixedFloatCovertResult(
            primary_case=primary_case, filler_case=slot.collection if n_filler else "",
            primary_mix=[{"wear": w, "count": c, "unit_price": primary_prices[w]} for w, c in primary_mix.items()],
            filler_count=n_filler, filler_input=slot.name if n_filler else "",
            filler_unit_price=slot.price if n_filler else 0.0,
            cost=cost, expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
            profit=profit, roi_pct=roi, roi_improvement_pct=roi - primary_baseline_roi,
            buy_links=buy_links,
            primary_skin_image=primary_skin.get("image", ""),
            filler_skin_image=slot.skin.get("image", "") if n_filler else "",
        )

    # Zero-filler candidates first -- pure wear-mixing within the primary case alone. Without
    # this, a case whose best play genuinely is "just float-manipulate, don't dilute" would be
    # missed entirely (see mixed_float.py's docstring for why this matters).
    zero_slot = _FillerSlot(collection="", skin=None, name="", price=0.0, wear="", norm=0.0, n_filler=0,
                            outputs=[], primary_odds=100.0 / len(primary_outputs), filler_odds=0.0)
    for primary_mix in _mixes(COVERT_INPUT_COUNT):
        if abort_check and abort_check():
            break
        _consider(primary_mix, zero_slot)

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
        found_filler = cheapest_buyable_input(f_inputs, stattrak, currency, abort_check, fast_mode)
        if found_filler is None:
            continue
        filler_skin, filler_found = found_filler
        filler_price, filler_name, filler_wear = filler_found[:3]  # 4th element (volume) unused here
        filler_norm = oddsmath.normalized_float(filler_skin, filler_wear)
        f_keyed = [(s, _skin_key(s)) for s in f_outputs]

        for n_filler in range(1, COVERT_INPUT_COUNT):
            if abort_check and abort_check():
                break
            n_primary = COVERT_INPUT_COUNT - n_filler
            primary_odds, filler_odds = oddsmath.group_odds(
                [(n_primary, len(primary_outputs)), (n_filler, len(f_outputs))])
            slot = _FillerSlot(collection=case, skin=filler_skin, name=filler_name, price=filler_price,
                               wear=filler_wear, norm=filler_norm, n_filler=n_filler, outputs=f_keyed,
                               primary_odds=primary_odds, filler_odds=filler_odds)
            for primary_mix in _mixes(n_primary):
                if abort_check and abort_check():
                    break
                _consider(primary_mix, slot)

    if best and best.roi_pct > primary_baseline_roi:
        return best
    return None
