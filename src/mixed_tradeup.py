"""Cross-collection "mixed input" trade-up search (filler skins across collections).

Post-October-2025 CS2 trade-up odds are group-based: each collection's share of the 10
inputs determines its total chance, split evenly across its own outputs at the target
rarity (see oddsmath.py). That means mixing in a few cheap "filler" inputs from a second,
cheaper-output collection can dilute a primary collection's odds away from its worst
outcomes -- see the trade-up guide's "Manipulating odds & filler skins" section. This
searches primary-collection + one-filler-collection splits (1-4 of the 10 slots being
filler) and reports the best one, if any beats using the primary collection alone.

Deliberately capped to *one* filler collection and a handful of candidates: searching every
pair/triple of collections at every split is combinatorially large for what's meant to be an
opt-in "does mixing help here" check, not an exhaustive optimizer.
"""
from dataclasses import dataclass

from . import oddsmath
from . import marketplaces as marketplaces_mod
from . import user_overrides as user_overrides_mod
from .float_mix import cheapest_buyable_input, landed_quotes
from .tradeup import _cash_market_maps

MAX_FILLER_CANDIDATES = 6  # cap how many other collections get priced -- each one costs requests


@dataclass
class MixedTradeUpResult:
    primary_collection: str
    filler_collection: str
    primary_count: int
    filler_count: int
    cost: float
    primary_input: str
    filler_input: str
    expected_revenue: float
    expected_revenue_after_fee: float
    profit: float
    roi_pct: float
    roi_improvement_pct: float  # vs. the pure single-collection baseline
    buy_links: dict
    primary_input_image: str = ""
    filler_input_image: str = ""


def find_best_filler_mix(primary_collection: str, primary_baseline_roi: float, idx: dict,
                          from_id: str, to_id: str, stattrak: bool, currency: int = 1,
                          abort_check=None, fast_mode: bool = False,
                          max_filler_candidates: int = MAX_FILLER_CANDIDATES) -> "MixedTradeUpResult | None":
    primary_inputs = idx.get(primary_collection, {}).get(from_id, [])
    primary_outputs = idx.get(primary_collection, {}).get(to_id, [])
    if not primary_inputs or not primary_outputs:
        return None
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
            # Same rule the main scan applies (see tradeup.scan_collection): a user-blacklisted
            # skin never enters as an input, and a filler collection whose output pool contains
            # one is skipped entirely -- mixing it in would put the blacklisted skin back in
            # the possible-drops table.
            f_inputs = [s for s in f_inputs if s["name"] not in blacklisted]
            if any(s["name"] in blacklisted for s in f_outputs):
                continue
        if not f_inputs or not f_outputs:
            continue
        candidates.append((coll, f_inputs, f_outputs))
    candidates = candidates[:max_filler_candidates]

    best = None
    for coll, f_inputs, f_outputs in candidates:
        if abort_check and abort_check():
            break
        found_filler = cheapest_buyable_input(f_inputs, stattrak, currency, abort_check, fast_mode)
        if found_filler is None:
            continue
        filler_skin, filler_found = found_filler
        filler_price, filler_name, filler_wear = filler_found[:3]  # 4th element (volume) unused here

        for n_filler in (1, 2, 3, 4):
            if abort_check and abort_check():
                break
            n_primary = 10 - n_filler
            cost = n_primary * primary_price + n_filler * filler_price
            primary_odds, filler_odds = oddsmath.group_odds(
                [(n_primary, len(primary_outputs)), (n_filler, len(f_outputs))])
            avg_norm = oddsmath.average_normalized_float([
                (primary_skin, primary_wear, n_primary), (filler_skin, filler_wear, n_filler)])

            # (gross, proceeds, odds) per output priced at its landed wear (honest cash-market rule).
            priced = landed_quotes(primary_outputs, avg_norm, primary_odds, stattrak, currency, cash_maps,
                                   abort_check) + landed_quotes(f_outputs, avg_norm, filler_odds, stattrak,
                                                                currency, cash_maps, abort_check)
            # Full odds mass of both groups' outputs, priced or not -- an unpriced output is
            # still an outcome this contract can land on, so it must stay in the denominator
            # rather than have its probability handed to the priced ones. Filler mixes are
            # especially exposed: a filler collection is picked for its cheap INPUTS, and its
            # outputs are correspondingly the thin, often-unpriced ones. See oddsmath's
            # unpriced-outputs section.
            total_prob = primary_odds * len(primary_outputs) + filler_odds * len(f_outputs)
            expected = oddsmath.honest_expectation(priced, total_prob)
            if expected is None:
                continue
            expected_revenue, after_fee = expected
            profit = after_fee - cost
            roi = (profit / cost * 100) if cost else 0.0
            if best is None or roi > best.roi_pct:
                best = MixedTradeUpResult(
                    primary_collection=primary_collection, filler_collection=coll,
                    primary_count=n_primary, filler_count=n_filler,
                    cost=cost, primary_input=primary_name, filler_input=filler_name,
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
