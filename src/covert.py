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
from . import rules_engine as rules_engine_mod
from . import user_overrides as user_overrides_mod
from .tradeup import PossibleDrop, STEAM_SELL_FEE, _cheapest_wear_price, _price_and_volume_at_matched_wear
from . import wear_anomaly as wear_anomaly_mod

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

    cheapest = None
    cheapest_skin = None
    for skin in inputs:
        if abort_check and abort_check():
            break
        found = _cheapest_wear_price(skin, stattrak, currency, abort_check, fast_mode)
        if found and (cheapest is None or found[0] < cheapest[0]):
            cheapest = found
            cheapest_skin = skin
    if cheapest is None:
        return None
    cost = cheapest[0] * COVERT_INPUT_COUNT
    matched_wear = cheapest[2]  # same same-bracket approximation as the regular scan (see tradeup.py)

    revenues = []
    priced = 0
    drops = []
    probability_pct = 100.0 / len(outputs)
    for skin in outputs:
        if abort_check and abort_check():
            break
        found_price = _price_and_volume_at_matched_wear(skin, matched_wear, stattrak, currency, abort_check)
        price_val, actual_wear, volume = found_price if found_price else (None, "", None)
        drop_profit = price_val * (1 - STEAM_SELL_FEE) - cost if price_val is not None else None
        drop_max_input_price = (price_val * (1 - STEAM_SELL_FEE) / COVERT_INPUT_COUNT) if price_val is not None else None
        drops.append(PossibleDrop(name=skin["name"], probability_pct=probability_pct, price=price_val,
                                   profit_after_fee=drop_profit, max_input_price=drop_max_input_price,
                                   image=skin.get("image", ""), wear=actual_wear, volume=volume))
        if price_val is not None:
            revenues.append(price_val)
            priced += 1

    # Retry any unpriced output once, same reasoning as the regular scan: a miss might just
    # be a transient rate limit/connection hiccup, not "genuinely no listings".
    for i, skin in enumerate(outputs):
        if drops[i].price is not None or (abort_check and abort_check()):
            continue
        found_price = _price_and_volume_at_matched_wear(skin, matched_wear, stattrak, currency, abort_check)
        if found_price is None:
            continue
        price_val, actual_wear, volume = found_price
        drop_profit = price_val * (1 - STEAM_SELL_FEE) - cost
        drop_max_input_price = price_val * (1 - STEAM_SELL_FEE) / COVERT_INPUT_COUNT
        drops[i] = PossibleDrop(name=skin["name"], probability_pct=probability_pct, price=price_val,
                                 profit_after_fee=drop_profit, max_input_price=drop_max_input_price,
                                 image=skin.get("image", ""), wear=actual_wear, volume=volume)
        revenues.append(price_val)
        priced += 1

    # Same positive-ROI wear-order sanity check as the regular scan (see tradeup.py /
    # wear_anomaly.py): a priced output whose own better condition is cheaper is a bad data
    # point, not revenue -- drop it before this result can look profitable off it.
    if revenues and cost and (sum(revenues) / len(outputs)) * (1 - STEAM_SELL_FEE) >= cost:
        for out_skin, drop in zip(outputs, drops):
            if drop.price is None or (abort_check and abort_check()):
                continue
            anomaly = wear_anomaly_mod.check_output_price(
                out_skin, drop.wear, drop.price, stattrak, currency, abort_check)
            if anomaly:
                drop.wear_anomaly = anomaly
                drop.price = None
                drop.profit_after_fee = None
                drop.max_input_price = None
        revenues = [d.price for d in drops if d.price is not None]
        priced = len(revenues)

    # Same all-or-nothing output rule as the regular scan (see tradeup.py's own comment and
    # oddsmath's unpriced-outputs section) -- and it bites hardest here: a knife/glove pool
    # where only the headline knife prices would otherwise report that knife's price as the
    # expected outcome of every single contract.
    if not revenues or priced < len(outputs):
        return None
    drops.sort(key=lambda d: (d.price is None, -(d.price or 0)))
    expected_revenue = sum(revenues) / len(outputs)
    after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
    profit = after_fee - cost
    roi = (profit / cost * 100) if cost else 0.0
    max_input_price = after_fee / COVERT_INPUT_COUNT

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
                cheapest_skin, outputs, stattrak, currency, abort_check, fee=STEAM_SELL_FEE,
                n=COVERT_INPUT_COUNT)
        if mixed_strategy and full_cases_idx and strategy_gate and not (abort_check and abort_check()):
            from . import mixed_covert as mixed_covert_mod
            mixed_result = mixed_covert_mod.find_best_filler_mix(
                case, roi, full_cases_idx, stattrak, currency, abort_check, fast_mode)

    return CovertTradeUpResult(
        case=case, stattrak=stattrak, cost=cost, cheapest_input=cheapest[1],
        expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
        profit=profit, roi_pct=roi, n_output_variants=len(outputs),
        priced_output_variants=priced, max_input_price=max_input_price,
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
