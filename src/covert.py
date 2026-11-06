"""Covert -> knife/glove trade-up scanner (Oct-2025 CS2 update).

Valve extended trade-up contracts so Covert (red) skins are no longer the end of the chain:
5 Covert-rarity skins from the same case can now be traded up into one knife or a pair of
gloves from that same case's knife/glove pool (5 StatTrak Covert -> StatTrak knife only --
gloves have no StatTrak variant). This mirrors tradeup.py's single-collection scan, just
grouped by CASE instead of collection, and with 5 inputs instead of 10 -- see oddsmath.py
for the shared group-odds/float math both rely on.
"""
from dataclasses import dataclass, field

from . import float_mix as float_mix_mod
from . import marketplaces as marketplaces_mod
from . import oddsmath
from . import rules_engine as rules_engine_mod
from . import user_overrides as user_overrides_mod
from .float_mix import cheapest_buyable_input
from .tradeup import PossibleDrop, _cash_market_maps, _quote_outputs, _reject_wear_anomalies

COVERT_INPUT_COUNT = rules_engine_mod.COVERT_INPUT_COUNT  # alias, see rules_engine.py


@dataclass
class CovertTradeUpResult:
    case: str
    stattrak: bool
    cost: float
    cheapest_input: str
    expected_revenue: float
    expected_revenue_after_fee: float
    profit: float
    roi_pct: float
    n_output_variants: int
    priced_output_variants: int
    max_input_price: float  # breakeven price *per input unit*
    possible_drops: list = field(default_factory=list)  # list[PossibleDrop]
    buy_links: dict = field(default_factory=dict)  # only populated when roi_pct >= 0
    float_strategies: list = field(default_factory=list)  # list[FloatMixResult], best-ROI first
    mixed_strategy: object = None  # MixedCovertResult | None -- best cross-case filler mix
    mixed_float_strategy: object = None  # MixedFloatCovertResult | None -- combined search,
    # computed instead of the two above when both are requested together (see scan_case)
    cheapest_input_image: str = ""  # direct Steam CDN thumbnail URL for cheapest_input's skin
    cheapest_input_volume: int | None = None  # Steam's own recent-sales count for cheapest_input


def _covert_drops(outputs: list, quotes: list, wear_probs_list: list, cost: float) -> list[PossibleDrop]:
    """Possible-drops table for a 5-input covert contract (tradeup._build_drops is tied to 10 inputs)."""
    probability_pct = 100.0 / len(outputs)  # each knife/glove equally likely within the case pool
    drops = [PossibleDrop(name=skin["name"], probability_pct=probability_pct, price=q.gross,
                          profit_after_fee=q.proceeds - cost, max_input_price=q.proceeds / COVERT_INPUT_COUNT,
                          image=skin.get("image", ""), wear=q.primary.wear, volume=q.primary.volume,
                          wear_probs=probs)
             for skin, q, probs in zip(outputs, quotes, wear_probs_list)]
    drops.sort(key=lambda d: -d.price)
    return drops


def scan_case(case: str, case_data: dict, stattrak: bool, currency: int = 1,
              abort_check=None, fast_mode: bool = False, float_strategy: bool = False,
              min_roi: float = float("-inf"), mixed_strategy: bool = False,
              full_cases_idx: dict | None = None, strategy_any_roi: bool = False) -> "CovertTradeUpResult | None":
    """float_strategy/mixed_strategy/min_roi/full_cases_idx/strategy_any_roi mirror
    tradeup.scan_collection's same-named params -- see there for the full rationale.
    full_cases_idx (the whole cases_index, not just this case) is needed for mixed_strategy to
    know what other cases are available to dilute with. When both float_strategy and
    mixed_strategy are requested together, this runs the unified mixed_float_covert search
    instead of the two separately (same reasoning as scan_collection)."""
    inputs = case_data["covert"]
    outputs = case_data["knives_gloves"]
    if stattrak:
        inputs = [s for s in inputs if s["stattrak"]]
        # gloves have no StatTrak variant (Valve doesn't track kills on them) -- only knives do
        outputs = [s for s in outputs if s["stattrak"]]
    # User's permanent skin blacklist -- same rule as tradeup.scan_collection: blacklisted
    # skins never enter as inputs, and a case whose knife/glove pool contains one is skipped
    # (the contract can't be done without a chance of landing on it).
    blacklisted = user_overrides_mod.blacklisted_skins()
    if blacklisted:
        inputs = [s for s in inputs if s["name"] not in blacklisted]
        if any(s["name"] in blacklisted for s in outputs):
            return None
    if not inputs or not outputs:
        return None

    found_cheapest = cheapest_buyable_input(inputs, stattrak, currency, abort_check, fast_mode)
    if found_cheapest is None:
        return None
    cheapest_skin, cheapest = found_cheapest
    cost = cheapest[0] * COVERT_INPUT_COUNT
    matched_wear = cheapest[2]  # same same-bracket approximation as the regular scan (see tradeup.py)

    # Honest revenue, same rule as tradeup.scan_collection: each output is worth the lowest
    # after-fee proceeds across Steam/Skinport/Buff163 at the wears it can land in. The float
    # distribution is sampled for COVERT_INPUT_COUNT inputs, not tradeup's 10.
    input_samples = oddsmath.sample_avg_normalized_floats([(cheapest_skin, matched_wear, COVERT_INPUT_COUNT)])
    wear_probs_list = [oddsmath.wear_probabilities_for_output(skin, input_samples) for skin in outputs]
    quotes = _quote_outputs(outputs, wear_probs_list, matched_wear, stattrak, currency,
                            _cash_market_maps(currency), abort_check)
    # All-or-nothing: one unpriced output means the contract's outcome is unknown (see oddsmath).
    if any(q is None for q in quotes):
        return None
    quotes = _reject_wear_anomalies(outputs, quotes, cost, stattrak, currency, abort_check)
    if any(q is None for q in quotes):
        return None
    expected_revenue = sum(q.gross for q in quotes) / len(outputs)
    after_fee = sum(q.proceeds for q in quotes) / len(outputs)
    profit = after_fee - cost
    roi = (profit / cost * 100) if cost else 0.0
    max_input_price = after_fee / COVERT_INPUT_COUNT
    drops = _covert_drops(outputs, quotes, wear_probs_list, cost)

    buy_links = marketplaces_mod.build_links(cheapest_skin["name"], matched_wear, stattrak) if roi >= 0 else {}

    float_strategies = []
    mixed_result = None
    mixed_float_result = None
    run_combined = float_strategy and mixed_strategy and full_cases_idx
    strategy_gate = strategy_any_roi or roi >= min_roi
    if run_combined and strategy_gate and not (abort_check and abort_check()):
        from . import mixed_float_covert as mixed_float_covert_mod
        mixed_float_result = mixed_float_covert_mod.find_best_mixed_float_strategy(
            case, roi, full_cases_idx, stattrak, currency, abort_check, fast_mode,
            require_positive_baseline=not strategy_any_roi)
    else:
        if float_strategy and strategy_gate and not (abort_check and abort_check()):
            float_strategies = float_mix_mod.evaluate_float_strategies(
                cheapest_skin, outputs, stattrak, currency, abort_check, n=COVERT_INPUT_COUNT)
        if mixed_strategy and full_cases_idx and strategy_gate and not (abort_check and abort_check()):
            from . import mixed_covert as mixed_covert_mod
            mixed_result = mixed_covert_mod.find_best_filler_mix(
                case, roi, full_cases_idx, stattrak, currency, abort_check, fast_mode)

    return CovertTradeUpResult(
        case=case, stattrak=stattrak, cost=cost, cheapest_input=cheapest[1],
        expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
        profit=profit, roi_pct=roi, n_output_variants=len(outputs),
        priced_output_variants=len(outputs), max_input_price=max_input_price,
        possible_drops=drops, buy_links=buy_links,
        float_strategies=float_strategies, mixed_strategy=mixed_result,
        mixed_float_strategy=mixed_float_result,
        cheapest_input_image=cheapest_skin.get("image", ""),
        cheapest_input_volume=cheapest[3],
    )


def scan_all(cases_idx: dict, stattrak: bool = False, currency: int = 1,
             only_case: str | None = None, fast_mode: bool = False,
             abort_check=None, float_strategy: bool = False, min_roi: float = float("-inf"),
             mixed_strategy: bool = False, strategy_any_roi: bool = False) -> list[CovertTradeUpResult]:
    results = []
    for case, case_data in cases_idx.items():
        if only_case and only_case.lower() not in case.lower():
            continue
        if abort_check and abort_check():
            break
        r = scan_case(case, case_data, stattrak, currency, abort_check, fast_mode,
                       float_strategy=float_strategy, min_roi=min_roi, mixed_strategy=mixed_strategy,
                       full_cases_idx=cases_idx, strategy_any_roi=strategy_any_roi)
        if r:
            results.append(r)
    return sorted(results, key=lambda r: r.roi_pct, reverse=True)
