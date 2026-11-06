"""Trade-up contract profitability scanner.

A CS2 trade-up contract consumes 10 skins of the same rarity (all same collection,
in the simple single-collection strategy this scans for) and returns 1 random skin
of the next rarity up, drawn uniformly from that collection's skins at that tier.

APPROXIMATION NOTE: the output's exact wear (float) depends on the average of the
10 input floats mapped into the output skin's float range. Since this scanner buys
all 10 inputs as the same cheapest skin+wear combo, those 10 floats are all in the
same wear bracket -- so the output is assumed to land in that *same* wear bracket
too (e.g. 10 Battle-Scarred inputs -> a Battle-Scarred output), rather than treating
the output as a blend across every possible wear. Real traders can still
"float-manipulate" (deliberately mix input wears to steer to a different bracket,
see float_mix.py) to do better than this default; treat results as a shortlist to
verify manually (e.g. on a float-checking site) before committing money, not as
guaranteed numbers.
"""
import functools
from dataclasses import dataclass, field

from . import bulk_prices as bulk_prices_mod
from . import data as data_mod
from . import float_mix as float_mix_mod
from . import marketplaces as marketplaces_mod
from . import oddsmath as oddsmath_mod
from . import rules_engine as rules_engine_mod
from . import tradability as tradability_mod
from . import user_overrides as user_overrides_mod
from . import wear_anomaly as wear_anomaly_mod
from .steam_prices import get_cached_price, get_price, market_hash_name

EARLY_EXIT = True  # skip a rarity tier when its ROI upper bound (cached Steam prices only) is below min_roi
ROI_BOUND_EPSILON = 1e-9  # float slack so rounding can never flip a borderline skip
STEAM_SELL_FEE = 0.13  # Steam Market cut on a sale; commonly cited as 13-15%, varies by price tier
# Cash-market sell fees for the honest revenue rule. Checked 2026-11 -- fees change, re-verify
# before trusting the numbers.
SKINPORT_SELL_FEE = 0.08
BUFF_SELL_FEE = 0.025
# Output revenue = the LOWEST after-fee proceeds across Steam (anchor, required) and the cash
# markets Skinport/Buff163 when they have a price. Steam wallet prices overstate what a cash-out
# returns. False = Steam-only revenue at STEAM_SELL_FEE (the old, optimistic rule).
CASH_MARKET_REVENUE = True
MIN_INPUT_PRICE = 0.50  # USD; candidate inputs cheaper than this are skipped (junk-price contracts)
LONG_WINDOW_SOURCE = "csgotrader:last_90d"  # bulk_prices tag when only the 90-day window had data
_N = rules_engine_mod.STANDARD_INPUT_COUNT  # single source of truth, see rules_engine.py

_WEAR_ORDER = [name for name, _, _ in float_mix_mod.WEAR_BREAKPOINTS]  # Factory New .. Battle-Scarred


@dataclass
class PossibleDrop:
    name: str
    probability_pct: float
    price: float | None  # probability-weighted gross price of the binding (lowest after-fee) market
    # across the wears this output can land in; None if no price data was found
    profit_after_fee: float | None  # this drop's probability-weighted after-fee proceeds minus the contract's cost
    max_input_price: float | None  # breakeven price *per input unit* if this is the drop you get
    thirdparty_check: dict | None = None  # thirdparty_prices.drought_flag() result, or None if not requested/available
    image: str = ""  # direct Steam CDN thumbnail URL, "" if the upstream feed doesn't have one for this skin
    wear: str = ""  # the wear this specific price was fetched at -- may differ from the contract's
    # matched_wear if this skin's float range excludes it (see _closest_available_wear)
    volume: int | None = None  # Steam's own recent-sales count for `price` -- a low number (see
    # price_report.LOW_VOLUME_THRESHOLD) means that price is resting on very few real sales and
    # may not reflect what you could actually sell at (the "someone lists it high but nobody's
    # buying" case)
    wear_probs: dict = field(default_factory=dict)  # {wear name: %} -- where this output's
    # float could actually land given the inputs' wear-bracket RANGES (not just midpoints),
    # via oddsmath.wear_probabilities_for_output. `wear` above stays the single most likely
    # priced wear; this shows the honest spread (e.g. FT 72% / MW 28%).
    wear_anomaly: dict | None = None  # wear_anomaly.check_output_price() details when this
    # drop's price failed the wear-order sanity check (a BETTER condition of the same skin
    # was cheaper -- see wear_anomaly.py). When set, price/profit_after_fee/max_input_price
    # are None: the price existed but was a proven-bad data point, excluded from revenue.


@dataclass
class TradeUpResult:
    collection: str
    stattrak: bool
    from_rarity: str
    to_rarity: str
    cost: float
    cheapest_input: str
    expected_revenue: float
    expected_revenue_after_fee: float
    profit: float
    roi_pct: float
    n_output_variants: int
    priced_output_variants: int
    max_input_price: float  # breakeven price *per input unit* -- pay more than this and the expected case loses money
    possible_drops: list = field(default_factory=list)  # list[PossibleDrop], every candidate output skin
    float_strategies: list = field(default_factory=list)  # list[FloatMixResult], best-ROI first; empty unless requested
    buy_links: dict = field(default_factory=dict)  # marketplace search links for the cheapest input -- only
    # populated when roi_pct >= 0 (no point sending someone shopping for a trade that loses money)
    mixed_strategy: object = None  # MixedTradeUpResult | None -- best cross-collection filler mix, if requested and better
    mixed_float_strategy: object = None  # MixedFloatResult | None -- combined float-mix + filler search,
    # computed instead of the two separate strategies above when both are requested together (see scan_collection)
    thirdparty_check: dict | None = None  # thirdparty_prices.drought_flag() result for cheapest_input, or None
    # if --verify-prices wasn't requested or no third-party data was available -- annotation only, never
    # changes expected_revenue/profit/roi_pct above
    ml_predicted_roi_pct: float | None = None  # learning.py's calibrated prediction for the plain scan, tiebreaker-grade signal only
    ml_confidence: str | None = None  # "low" | "medium" | "high" by training-set size, or None if unset
    ml_roi_range: list | None = None  # [lo, hi] 80% interval from the model's own held-out residuals, or None
    cheapest_input_image: str = ""  # direct Steam CDN thumbnail URL for cheapest_input's skin
    cheapest_input_skin: str = ""  # bare skin name (no "StatTrak(TM) "/"(wear)" formatting) -- same
    cheapest_input_wear: str = ""  # skin+wear cheapest_input describes as one string. Kept separately
    # (not just parsed back out of cheapest_input) so callers like price_report.py don't need to
    # re-derive them from a display string.
    cheapest_input_volume: int | None = None  # Steam's own recent-sales count for cheapest_input --
    # same liquidity signal as PossibleDrop.volume, for the input side


def _cheapest_wear_check_list(skin: dict, fast_mode: bool) -> list:
    if not fast_mode:
        return skin["wears"]
    # Fast mode, buy side: only price the last listed wear (Battle-Scarred when available --
    # the data lists wears best-to-worst) instead of every wear. Worse condition is usually
    # cheaper, so this is a reasonable single-request stand-in for "the cheapest price".
    return skin["wears"][-1:]


def _closest_available_wear(wears: list, target_wear: str) -> str:
    """The exact matched wear isn't always available on a given output skin (some skins have
    a restricted float range that excludes it) -- fall back to whichever available wear is
    closest to it in condition order."""
    if target_wear in wears:
        return target_wear
    target_idx = _WEAR_ORDER.index(target_wear) if target_wear in _WEAR_ORDER else 0
    return min(wears, key=lambda w: abs(_WEAR_ORDER.index(w) - target_idx) if w in _WEAR_ORDER else 99)


def _cheapest_wear_price(skin: dict, stattrak: bool, currency: int, abort_check=None,
                          fast_mode: bool = False) -> tuple[float, str, str, int | None] | None:
    """Returns (price, market_hash_name, wear, volume) for the cheapest wear of this skin.
    volume is Steam's own recent-sales count for that exact listing -- callers that don't need
    it (most of them, still just index [0]/[1]/[2]) can ignore the 4th element; added for
    price_report.py's liquidity check (see TradeUpResult.cheapest_input_volume)."""
    best = None
    for wear in _cheapest_wear_check_list(skin, fast_mode):
        if abort_check and abort_check():
            break
        name = market_hash_name(skin["name"], wear, stattrak)
        price = get_price(name, currency, abort_check)
        if price and price["lowest_price"] and price.get("source") != LONG_WINDOW_SOURCE:
            if best is None or price["lowest_price"] < best[0]:
                best = (price["lowest_price"], name, wear, price.get("volume"))
    return best


def _price_at_matched_wear(skin: dict, matched_wear: str, stattrak: bool, currency: int,
                            abort_check=None) -> float | None:
    """The output's expected price, assuming it lands in the same wear bracket as the (all
    identical) inputs -- see the module APPROXIMATION NOTE for why."""
    actual_wear = _closest_available_wear(skin["wears"], matched_wear)
    name = market_hash_name(skin["name"], actual_wear, stattrak)
    # An actively flagged wear-order inversion (a better condition of this skin verified
    # cheaper -- see wear_anomaly.py) means this price is a known-bad data point: treat it
    # as unpriceable so no strategy search builds revenue on it while the mark is active.
    if wear_anomaly_mod.is_flagged(name, currency):
        return None
    price = get_price(name, currency, abort_check)
    if not price:
        return None
    return price["median_price"] or price["lowest_price"]


def _price_and_volume_at_matched_wear(skin: dict, matched_wear: str, stattrak: bool, currency: int,
                                       abort_check=None) -> tuple[float, str, int | None] | None:
    """Same lookup/approximation as _price_at_matched_wear (kept as a separate function rather
    than changing that one's return shape, since it's called in 10+ places across the mixed_*.py
    strategy searches that all treat its result as a plain scalar for arithmetic) -- also returns
    the actual wear priced (may differ from matched_wear if this skin's float range excludes it,
    see _closest_available_wear) and Steam's own recent-sales volume for it, for callers that
    need to judge whether a price is backed by real liquidity (PossibleDrop.wear/.volume,
    consumed by price_report.py's low-volume/unconfirmed flag)."""
    actual_wear = _closest_available_wear(skin["wears"], matched_wear)
    name = market_hash_name(skin["name"], actual_wear, stattrak)
    if wear_anomaly_mod.is_flagged(name, currency):  # same known-bad-price skip as above
        return None
    price = get_price(name, currency, abort_check)
    if not price:
        return None
    price_val = price["median_price"] or price["lowest_price"]
    if price_val is None:
        return None
    return price_val, actual_wear, price.get("volume")


@dataclass(frozen=True)
class _MarketQuote:
    """One output skin at one wear, priced across every market that has a price."""
    gross: float  # listed price of the binding market (the one with the lowest proceeds)
    proceeds: float  # what that binding market nets after its own sell fee
    steam_price: float | None  # Steam's price for this wear (None if absent or 90d-only)
    wear: str  # wear actually priced (see _closest_available_wear)
    volume: int | None  # Steam's recent-sales count for steam_price


@dataclass(frozen=True)
class _OutputQuote:
    """One output skin, averaged over the wears it can land in (weighted by wear_probs)."""
    gross: float  # probability-weighted gross price
    proceeds: float  # probability-weighted after-fee proceeds
    primary: _MarketQuote  # quote at the single most likely landing wear (display, anomaly check)


@functools.lru_cache(maxsize=512)
def _input_wear_samples(min_float: float, max_float: float, wear: str) -> list:
    """oddsmath sampling for the contract's input side. Depends only on the input skin's float
    range and wear, so the many contracts sharing a range reuse one sampling pass. Callers must
    not mutate the result."""
    return oddsmath_mod.sample_avg_normalized_floats([({"min_float": min_float, "max_float": max_float}, wear, _N)])


def _output_wear_probs(outputs: list, cheapest_skin: dict, matched_wear: str) -> list[dict]:
    """{wear: %} per output skin: where each output's float can actually land given the inputs."""
    samples = _input_wear_samples(cheapest_skin.get("min_float", 0.0), cheapest_skin.get("max_float", 1.0),
                                  matched_wear)
    return [oddsmath_mod.wear_probabilities_for_output(skin, samples) for skin in outputs]


def _cash_market_maps(currency: int) -> tuple[dict, dict]:
    """(skinport, buff163) price maps from the bulk mirror already in memory, fetched once per
    tier. Empty maps when CASH_MARKET_REVENUE is off, so only Steam prices the output."""
    if not CASH_MARKET_REVENUE:
        return {}, {}
    return bulk_prices_mod.get_skinport_map(currency), bulk_prices_mod.get_buff163_map(currency)


def _honest_market_quote(skin: dict, wear: str, stattrak: bool, currency: int,
                         cash_maps: tuple[dict, dict], abort_check=None) -> _MarketQuote | None:
    """The lowest after-fee proceeds across Steam, Skinport and Buff163 for this skin at this wear.
    Steam is the anchor: without a Steam price there is no quote (a lone cash-market listing can be
    an outlier that would RAISE revenue, e.g. one Buff163 ask 5x Steam's median). Cash markets only
    ever lower the Steam-based revenue. A cash market with no price for this exact name is ignored."""
    actual_wear = _closest_available_wear(skin["wears"], wear)
    name = market_hash_name(skin["name"], actual_wear, stattrak)
    if wear_anomaly_mod.is_flagged(name, currency):  # known-bad price, see _price_at_matched_wear
        return None
    steam = get_price(name, currency, abort_check)
    steam_price = None
    if steam and steam.get("source") != LONG_WINDOW_SOURCE:
        steam_price = steam["median_price"] or steam["lowest_price"]
    if not steam_price:
        return None
    skinport_map, buff_map = cash_maps
    markets = [(steam_price, STEAM_SELL_FEE), (skinport_map.get(name), SKINPORT_SELL_FEE),
               (buff_map.get(name), BUFF_SELL_FEE)]
    proceeds_by_market = [(price * (1 - fee), price) for price, fee in markets if price]
    proceeds, gross = min(proceeds_by_market)
    return _MarketQuote(gross=gross, proceeds=proceeds, steam_price=steam_price, wear=actual_wear,
                        volume=steam.get("volume") if steam_price is not None else None)


def _honest_output_quote(skin: dict, wear_probs: dict, matched_wear: str, stattrak: bool, currency: int,
                         cash_maps: tuple[dict, dict], abort_check=None) -> _OutputQuote | None:
    """Probability-weighted quote for one output. Every wear it can land in must be priced: a
    wear with no price makes the whole output unpriced (same all-or-nothing rule as the contract)."""
    weights = {wear: pct for wear, pct in wear_probs.items() if pct > 0} or {matched_wear: 100.0}
    total = sum(weights.values())
    gross_sum = 0.0
    proceeds_sum = 0.0
    primary = None
    primary_pct = -1.0
    for wear, pct in weights.items():
        quote = _honest_market_quote(skin, wear, stattrak, currency, cash_maps, abort_check)
        if quote is None:
            return None
        gross_sum += pct * quote.gross
        proceeds_sum += pct * quote.proceeds
        if pct > primary_pct:
            primary, primary_pct = quote, pct
    return _OutputQuote(gross=gross_sum / total, proceeds=proceeds_sum / total, primary=primary)


def _quote_outputs(outputs: list, wear_probs_list: list, matched_wear: str, stattrak: bool, currency: int,
                   cash_maps: tuple[dict, dict], abort_check=None) -> list[_OutputQuote | None]:
    quotes: list = [None] * len(outputs)
    for i, skin in enumerate(outputs):
        if abort_check and abort_check():
            break
        quotes[i] = _honest_output_quote(skin, wear_probs_list[i], matched_wear, stattrak, currency,
                                         cash_maps, abort_check)
    # Retry any output skin that came back with no price data -- a transient failure
    # (rate limit, dropped connection, bad edge node) might resolve on a second attempt
    # after some time has passed scanning the rest of the collection. This runs even if
    # nothing at all got priced the first time, so a whole tier isn't given up on early.
    for i, skin in enumerate(outputs):
        if quotes[i] is not None:
            continue
        if abort_check and abort_check():
            break
        quotes[i] = _honest_output_quote(skin, wear_probs_list[i], matched_wear, stattrak, currency,
                                         cash_maps, abort_check)
    return quotes


def _reject_wear_anomalies(outputs: list, quotes: list, cost: float, stattrak: bool, currency: int,
                           abort_check=None) -> list:
    """Wear-order sanity check, only when the contract would come out positive (a losing result
    never gets recommended, so it isn't worth even the cheap check): each output's Steam price at
    its primary wear is verified against its own BETTER conditions. A Field-Tested copy "worth" more
    than its own Factory New is a bad data point (souvenir conflation, a troll listing -- see
    wear_anomaly.py); that output becomes unpriced so the contract is dropped, and the registry keeps
    it excluded everywhere for the next 24h. Prices come from the bulk mirror, so no extra requests."""
    if not cost or sum(q.proceeds for q in quotes) / len(outputs) < cost:
        return quotes
    kept = list(quotes)
    for i, (skin, quote) in enumerate(zip(outputs, quotes)):
        if abort_check and abort_check():
            continue
        if quote.primary.steam_price is None:
            continue
        anomaly = wear_anomaly_mod.check_output_price(
            skin, quote.primary.wear, quote.primary.steam_price, stattrak, currency, abort_check)
        if anomaly:
            kept[i] = None
    return kept


def _build_drops(outputs: list, quotes: list, wear_probs_list: list, cost: float) -> list[PossibleDrop]:
    probability_pct = 100.0 / len(outputs)  # single-collection trade-up: each output skin equally likely
    drops = [PossibleDrop(name=skin["name"], probability_pct=probability_pct, price=q.gross,
                          profit_after_fee=q.proceeds - cost, max_input_price=q.proceeds / _N,
                          image=skin.get("image", ""), wear=q.primary.wear, volume=q.primary.volume,
                          wear_probs=probs)
             for skin, q, probs in zip(outputs, quotes, wear_probs_list)]
    drops.sort(key=lambda d: -d.price)
    return drops


def _cached_steam_price(skin: dict, wear: str, stattrak: bool, currency: int) -> float | None:
    """The Steam price _honest_market_quote would use at this wear, read from cache only (no
    network). None when it isn't a fresh cached price -- then no sound bound exists."""
    actual_wear = _closest_available_wear(skin["wears"], wear)
    name = market_hash_name(skin["name"], actual_wear, stattrak)
    if wear_anomaly_mod.is_flagged(name, currency):
        return None
    data, freshness = get_cached_price(name, currency)
    if freshness != "fresh" or not data or data.get("source") == LONG_WINDOW_SOURCE:
        return None
    return data["median_price"] or data["lowest_price"] or None


def _revenue_upper_bound(outputs: list, wear_probs_list: list, matched_wear: str, stattrak: bool,
                         currency: int) -> float | None:
    """Upper bound on scan_collection's after_fee (mean over outputs of the probability-weighted
    proceeds). Each output's honest proceeds are the MIN over Steam/Skinport/Buff163 and Steam is
    one of them, so proceeds <= Steam * (1 - STEAM_SELL_FEE) at every landing wear. None if any
    landing wear has no cached Steam price."""
    total = 0.0
    for skin, wear_probs in zip(outputs, wear_probs_list):
        weights = {wear: pct for wear, pct in wear_probs.items() if pct > 0} or {matched_wear: 100.0}
        weight_sum = sum(weights.values())
        skin_bound = 0.0
        for wear, pct in weights.items():
            steam_price = _cached_steam_price(skin, wear, stattrak, currency)
            if steam_price is None:
                return None
            skin_bound += pct * steam_price * (1 - STEAM_SELL_FEE)
        total += skin_bound / weight_sum
    return total / len(outputs)


def _below_min_roi_bound(outputs: list, wear_probs_list: list, matched_wear: str, cost: float,
                         min_roi: float, stattrak: bool, currency: int) -> bool:
    """True only when the ROI upper bound is provably below min_roi (see _revenue_upper_bound)."""
    if not EARLY_EXIT or not cost or min_roi == float("-inf"):
        return False
    bound = _revenue_upper_bound(outputs, wear_probs_list, matched_wear, stattrak, currency)
    if bound is None:
        return False
    return (bound - cost) / cost * 100 < min_roi - ROI_BOUND_EPSILON


def scan_collection(collection: str, skins_by_rarity: dict, stattrak: bool, currency: int = 1,
                     abort_check=None, fast_mode: bool = False, float_strategy: bool = False,
                     min_roi: float = float("-inf"), mixed_strategy: bool = False,
                     full_idx: dict | None = None, strategy_any_roi: bool = False,
                     check_thirdparty: bool = False, use_ml: bool = False,
                     max_distinct_wears: int = 2, max_n_filler: int = 4) -> list[TradeUpResult]:
    """abort_check: optional zero-arg callable; if it returns True, stop between rarity
    tiers and return whatever was found so far (used by the web dashboard to let a
    scope change interrupt a long scan instead of waiting for the whole collection).
    fast_mode: only price one input wear per skin instead of searching every wear for the
    true cheapest -- faster, at the cost of a rougher (usually conservative) cost estimate.
    float_strategy: also compute the best input wear-mix ("float manipulation") for
    each result -- needs per-wear pricing for the cheapest input skin and every
    output skin, so it costs noticeably more requests than the default scan.
    min_roi: skip the (expensive) float-mix/mixed-collection computation for results that
    fall below this ROI%, since they'd be filtered out of the results anyway -- no point
    spending extra requests analyzing a result nobody will see. Ignored (every collection is
    analyzed) when strategy_any_roi is set.
    mixed_strategy: also search for a cross-collection "filler skin" mix that beats this
    collection used alone (see mixed_tradeup.py) -- needs full_idx (the whole
    collections_index, not just this collection) to know what other collections are
    available to mix in.
    When both float_strategy and mixed_strategy are requested together, this runs the unified
    mixed_float search (see mixed_float.py) instead of the two separately -- it's a superset of
    both (wear-mixing the primary group AND diluting with a filler collection at once), so
    running both independently on top of it would just waste requests re-deriving a subset of
    what it already covers. That combined search is itself restricted to collections whose
    plain ROI is already >= 0, unless strategy_any_roi is set -- opt-in since it means every
    collection gets the full (expensive) search instead of only ones already known to work.
    check_thirdparty: cross-check the cheapest input's price against a second marketplace
    (Skinport by default, see thirdparty_prices.py) to flag a "drought" (Steam priced
    anomalously high vs. elsewhere) -- annotation only via thirdparty_check, never changes
    expected_revenue/profit/roi_pct. use_ml: also compute learning.py's calibrated ROI
    prediction (a tiebreaker-grade signal, not a replacement) for this result and, when
    float_strategy/mixed_strategy are on, pass it through so their own candidate searches can
    use it too. max_distinct_wears/max_n_filler: forwarded to the float-mix/mixed-float search
    to widen how many wear brackets / filler slots a candidate may combine -- a caller may raise
    these for deeper coverage; the default (2 wears, 4 filler slots) matches today's behavior.
    Revenue is the honest cash-market rule (see CASH_MARKET_REVENUE): each output is worth the
    lowest after-fee proceeds across Steam/Skinport/Buff163, weighted by the wears it can land in."""
    results = []
    min_input_price = MIN_INPUT_PRICE * bulk_prices_mod.rate_for(currency)
    for from_id, to_id in zip([r for r, _ in data_mod.RARITY_CHAIN], [r for r, _ in data_mod.RARITY_CHAIN][1:]):
        if abort_check and abort_check():
            break
        inputs = [s for s in skins_by_rarity.get(from_id, []) if s["stattrak"] or not stattrak]
        outputs = [s for s in skins_by_rarity.get(to_id, []) if s["stattrak"] or not stattrak]
        if not inputs or not outputs:
            continue
        # Real-game eligibility: souvenir items can't enter a trade-up, and a souvenir-only
        # skin has no non-souvenir copy to buy at all (see tradability.py) -- pricing one as
        # an input would just burn lookups on a contract that can't exist. Whole souvenir-only
        # collections (e.g. Cobblestone) drop out here with zero requests spent.
        inputs = [s for s in inputs if tradability_mod.skin_is_obtainable(s)]
        # User's permanent skin blacklist (user_overrides.py): blacklisted skins never enter as
        # inputs, and a rarity step whose OUTPUT pool contains one is skipped outright -- the
        # contract can't be done without a chance of landing on the blacklisted skin, so no
        # honest cost/revenue row can be built around it.
        blacklisted = user_overrides_mod.blacklisted_skins()
        if blacklisted:
            inputs = [s for s in inputs if s["name"] not in blacklisted]
            if any(s["name"] in blacklisted for s in outputs):
                continue
        if not inputs:
            continue
        if stattrak:
            inputs = [s for s in inputs if s["stattrak"]]
            outputs = [s for s in outputs if s["stattrak"]]
            if not inputs or not outputs:
                continue

        cheapest = None
        cheapest_skin = None
        for skin in inputs:
            if abort_check and abort_check():
                break
            found = _cheapest_wear_price(skin, stattrak, currency, abort_check, fast_mode)
            if not found or found[0] < min_input_price:
                continue
            if cheapest is None or found[0] < cheapest[0]:
                cheapest = found
                cheapest_skin = skin
        if cheapest is None:
            continue
        cost = cheapest[0] * _N
        matched_wear = cheapest[2]  # all N inputs are this same skin+wear, so this is the output's expected wear too

        # Honest revenue (see _honest_output_quote): every output is quoted at each wear it can
        # land in, weighted by wear_probs; any unpriced landing wear leaves the output unpriced.
        wear_probs_list = _output_wear_probs(outputs, cheapest_skin, matched_wear)
        # Early exit: even the best case (cached Steam prices only) is below min_roi, so every
        # caller's min_roi filter drops this tier -- skip its pricing and strategies. strategy_any_roi
        # keeps every tier, so the early exit is off then.
        if not strategy_any_roi and _below_min_roi_bound(outputs, wear_probs_list, matched_wear, cost,
                                                         min_roi, stattrak, currency):
            continue
        cash_maps = _cash_market_maps(currency)
        quotes = _quote_outputs(outputs, wear_probs_list, matched_wear, stattrak, currency, cash_maps, abort_check)
        # All-or-nothing on the output side: this contract can land on any of `outputs`, so one
        # unpriced output means the honest expected revenue is unknown -- not "the average of
        # the ones that worked". Averaging the priced subset silently hands the unpriced
        # outcomes' probability to the priced ones, and the outputs that fail to price are
        # disproportionately the illiquid cheap ones, so what survives is the expensive tail:
        # the ROI comes out inflated in exactly the cases where it's least trustworthy.
        # See oddsmath's unpriced-outputs section.
        if any(q is None for q in quotes):
            continue
        quotes = _reject_wear_anomalies(outputs, quotes, cost, stattrak, currency, abort_check)
        if any(q is None for q in quotes):
            continue
        expected_revenue = sum(q.gross for q in quotes) / len(outputs)
        after_fee = sum(q.proceeds for q in quotes) / len(outputs)
        profit = after_fee - cost
        roi = (profit / cost * 100) if cost else 0.0
        max_input_price = after_fee / _N  # breakeven price per unit -- pay more and the expected case is a loss
        drops = _build_drops(outputs, quotes, wear_probs_list, cost)

        from_name = dict(data_mod.RARITY_CHAIN)[from_id]
        to_name = dict(data_mod.RARITY_CHAIN)[to_id]

        float_strategies = []
        mixed_result = None
        mixed_float_result = None
        run_combined = float_strategy and mixed_strategy and full_idx
        strategy_gate = strategy_any_roi or roi >= min_roi
        if run_combined and strategy_gate and not (abort_check and abort_check()):
            from . import mixed_float as mixed_float_mod
            mixed_float_result = mixed_float_mod.find_best_mixed_float_strategy(
                collection, roi, full_idx, from_id, to_id, stattrak, currency, abort_check, fast_mode,
                require_positive_baseline=not strategy_any_roi, max_distinct_wears=max_distinct_wears,
                max_n_filler=max_n_filler, use_ml=use_ml, from_rarity=from_name)
        else:
            if float_strategy and strategy_gate and not (abort_check and abort_check()):
                float_strategies = float_mix_mod.evaluate_float_strategies(
                    cheapest_skin, outputs, stattrak, currency, abort_check,
                    max_distinct_wears=max_distinct_wears, use_ml=use_ml, collection=collection,
                    from_rarity=from_name)
            if mixed_strategy and full_idx and strategy_gate and not (abort_check and abort_check()):
                from . import mixed_tradeup as mixed_tradeup_mod
                mixed_result = mixed_tradeup_mod.find_best_filler_mix(
                    collection, roi, full_idx, from_id, to_id, stattrak, currency, abort_check, fast_mode)

        # Only worth sending someone shopping for inputs to a trade that's actually +EV.
        buy_links = marketplaces_mod.build_links(cheapest_skin["name"], matched_wear, stattrak) if roi >= 0 else {}

        thirdparty_check_result = None
        # Only worth spending Skinport/CSFloat requests (rate-limited, and Skinport's bulk
        # fetch is shared/throttled across the whole scan) verifying a result that's already
        # +EV -- same reasoning as buy_links above, never worth it for a result you wouldn't
        # act on anyway.
        if check_thirdparty and roi >= 0 and not (abort_check and abort_check()):
            from . import thirdparty_prices as thirdparty_prices_mod
            tp_prices = thirdparty_prices_mod.get_thirdparty_price(
                cheapest_skin["name"], matched_wear, stattrak, currency, abort_check)
            thirdparty_check_result = thirdparty_prices_mod.drought_flag(cheapest[0], tp_prices)

        ml_predicted_roi_pct = None
        ml_confidence = None
        ml_roi_range = None
        if use_ml:
            from . import learning as learning_mod
            if learning_mod.get_model_status()["trained"]:
                ml_result = learning_mod.predict_roi(
                    collection=collection, from_rarity=from_name,
                    input_mix=[{"skin": cheapest_skin["name"], "wear": matched_wear, "count": _N,
                                "unit_price_paid": cheapest[0]}],
                    stattrak=stattrak, cost_paid=cost, predicted_roi_pct=roi,
                    thirdparty_gap_pct=thirdparty_check_result["gap_pct"] if thirdparty_check_result else None)
                if ml_result:
                    ml_predicted_roi_pct = ml_result["ml_predicted_roi_pct"]
                    ml_confidence = ml_result["confidence"]
                    ml_roi_range = ml_result["predicted_roi_range"]

        results.append(TradeUpResult(
            collection=collection,
            stattrak=stattrak,
            from_rarity=from_name,
            to_rarity=to_name,
            cost=cost,
            cheapest_input=cheapest[1],
            expected_revenue=expected_revenue,
            expected_revenue_after_fee=after_fee,
            profit=profit,
            roi_pct=roi,
            n_output_variants=len(outputs),
            priced_output_variants=len(outputs),  # the gate above guarantees every output is priced
            max_input_price=max_input_price,
            possible_drops=drops,
            float_strategies=float_strategies,
            buy_links=buy_links,
            mixed_strategy=mixed_result,
            mixed_float_strategy=mixed_float_result,
            thirdparty_check=thirdparty_check_result,
            ml_predicted_roi_pct=ml_predicted_roi_pct,
            ml_confidence=ml_confidence,
            ml_roi_range=ml_roi_range,
            cheapest_input_image=cheapest_skin.get("image", ""),
            cheapest_input_skin=cheapest_skin["name"],
            cheapest_input_wear=matched_wear,
            cheapest_input_volume=cheapest[3],
        ))
    return results


def scan_all(collections_idx: dict, stattrak: bool = False, currency: int = 1,
             only_collection: str | None = None, fast_mode: bool = False,
             float_strategy: bool = False, min_roi: float = float("-inf"),
             mixed_strategy: bool = False, strategy_any_roi: bool = False,
             check_thirdparty: bool = False, use_ml: bool = False,
             max_distinct_wears: int = 2, max_n_filler: int = 4) -> list[TradeUpResult]:
    all_results = []
    for collection, skins_by_rarity in collections_idx.items():
        if only_collection and only_collection.lower() not in collection.lower():
            continue
        all_results.extend(scan_collection(collection, skins_by_rarity, stattrak, currency,
                                            fast_mode=fast_mode, float_strategy=float_strategy, min_roi=min_roi,
                                            mixed_strategy=mixed_strategy, full_idx=collections_idx,
                                            strategy_any_roi=strategy_any_roi, check_thirdparty=check_thirdparty,
                                            use_ml=use_ml, max_distinct_wears=max_distinct_wears,
                                            max_n_filler=max_n_filler))
    return sorted(all_results, key=lambda r: r.roi_pct, reverse=True)
