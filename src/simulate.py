"""Deterministic outcome + expected-value simulation for one random_trade.py candidate.

Reuses the exact same formula-based math the rest of the app already trusts -- oddsmath.py's
group odds / float remap, tradeup.py's STEAM_SELL_FEE -- rather than re-deriving any of it.
The only new thing here is feeding a RANDOMLY generated candidate through that existing math
instead of a brute-force-searched one, and reporting the full probability-weighted expected
ROI across every possible output (never a single random draw), since the whole point of this
feature is not being blind to how uncertain a "win" really is.

Prices come from steam_prices.get_cached_price() first; on a cache miss, simulate() now falls
back to a real live steam_prices.get_price() call (which fetches AND caches, so the next
candidate that needs the same skin/wear gets a cache hit) instead of giving up. This was a
deliberate reversal of the original "never call live" rule -- with price_scan.py's bulk warm
never covering the full ~10k skin/wear/StatTrak universe in practice, requiring every one of a
candidate's 10 inputs to already be cached made almost everything invalid (see README's
"invalid" stats). get_price() already shares the same adaptive rate limiter/retry/backoff as
every other live caller (price_scan.py, tradeups scans), so this doesn't need its own throttling
-- it just means a round can now block for real request latency instead of being instant.
See price_scan.py for the separate mode that pre-warms the cache in bulk ahead of time; that's
now a head start for this fallback, not a hard prerequisite.

A real trade-up contract needs ALL of its inputs (10, or 5 for the Covert->knife/glove path) --
there's no such thing as a partial trade-up. So simulate() requires every single input unit to
have a cached price before it will compute a real SimulationRecord; if even one unit's price is
missing, or if nothing ends up priced on the output side, the candidate is INVALID -- reported
as an InvalidSimulation, not silently dropped and not guessed at with a skewed subset of the
true inputs (see simulate()'s own docstring for why an earlier "partial pricing is close
enough" approximation was replaced with this hard requirement).

Third-party prices (thirdparty_prices.py: Skinport's bulk-cached catalog, or CSFloat if an API
key is configured) are used two different ways, both non-fatal:
  - As CORROBORATION when Steam DOES have a price but it already looks thin
    (dead_skin.score_from_signals(volume) > 0 -- see dead_skin.py): recorded
    (skinport_price/csfloat_price on each input/output entry) purely for extra visibility. A
    missing match here never disqualifies anything by itself -- it used to be a hard
    requirement for every input/output, but that made the feature nearly unusable in practice
    (many cheap trade-up-fodder skins simply aren't on Skinport, and outages invalidated
    everything).
  - As a PRICE FALLBACK when Steam has NO price at all (cache miss + empty live fetch): the
    cheapest third-party price is used as the actual unit/output price so a skin isn't thrown
    away just because Steam happens to be dry. Only if NEITHER Steam NOR any third-party site
    has a price is the skin genuinely unpriceable -- see the invalid-skin handling below.

A skin that comes up with no price on ANY source is recorded in invalid_skins.py (a temporary,
self-healing, shared registry -- see that module) so random_trade.py stops building candidates
out of it and the population stops wasting simulate() calls on trade-ups it already knows can't
be valued. Any later successful price for that skin clears the mark automatically.

Each population "son" (see sim_loop.py) has its OWN simulations.jsonl under its own
data/sons/son_N/ directory (mirroring outcomes.py's append-only-jsonl format) -- log_simulation
and load_simulations take an explicit sims_file path rather than a fixed module-level constant,
so a son's data can never accidentally cross-contaminate another son's, and this is DELIBERATELY
separate from data/outcomes.jsonl either way -- see learning.py's own docstring for why the
tool's own guesses must never be fed back in as if they were real,
user-executed trades. Nothing in this module ever reads or writes outcomes.jsonl.
"""
from dataclasses import dataclass, asdict, field
import json
import threading
from pathlib import Path
import time

from . import bulk_prices as bulk_prices_mod
from . import dead_skin as dead_skin_mod
from . import invalid_skins as invalid_skins_mod
from . import mispriced_skins as mispriced_skins_mod
from . import oddsmath
from . import thirdparty_prices as thirdparty_prices_mod
from . import user_overrides as user_overrides_mod
from .steam_prices import get_cached_price, get_price, market_hash_name
from .tradeup import STEAM_SELL_FEE

_write_lock = threading.Lock()


def _thirdparty_prices(skin_name: str, wear: str, stattrak: bool, currency: int, abort_check=None) -> dict:
    return thirdparty_prices_mod.get_thirdparty_price(skin_name, wear, stattrak, currency, abort_check)


def _cheapest_thirdparty(tp: dict) -> float | None:
    prices = [p for p in (tp or {}).values() if p]
    return min(prices) if prices else None


def _price(skin_name: str, wear: str, stattrak: bool, currency: int, abort_check,
           live_fetch: bool, allow_thirdparty: bool = True) -> tuple[dict | None, str]:
    """Prices one skin+wear+StatTrak, trying every source in turn before giving up:
    get_cached_price() first; on a genuine miss ("missing", never "stale" -- a stale price is
    still usable), a real live get_price() call (which also writes back into the same cache, so
    it's a one-time cost per skin/wear); and finally, if Steam still has nothing, a third-party
    fallback (thirdparty_prices.get_thirdparty_price -> Skinport/CSFloat) used as an actual
    price source, not just corroboration -- returned as a synthesized data dict with freshness
    "thirdparty" (volume None, plus the raw per-source prices under "thirdparty").

    allow_thirdparty=False turns that final fallback OFF -- the OUTPUT-pricing rule. Revenue in
    this tool is always "sell on Steam, minus the Steam fee", so an output's price must be
    backed by actual Steam sale evidence. A skin with NO Steam price in any of the mirror's
    24h-90d windows but big Skinport/Buff163 listings is exactly the trap this closes: e.g.
    "AWP | Gungnir" trades ABOVE Steam's price cap, so it has no Steam sales at all, while the
    third-party feeds list it at $6k-12k -- pricing it as revenue produced absurd fake ROI
    (the 2026-07 incident that motivated this flag). Inputs keep the fallback (buying on a
    third-party market and trading the item in is real), outputs never get it. With the
    fallback off, a Steam miss is also never recorded in invalid_skins -- that registry means
    "no price on ANY source", which this path deliberately didn't check.

    If NO source has a price, the skin is recorded in invalid_skins.py (so candidate generation
    stops proposing trade-ups built out of it -- see random_trade.py) and (None, "missing") is
    returned. Any successful price clears a prior invalid mark, so a skin that gets relisted /
    an outage that recovers heals on its own. See invalid_skins.py for why that registry is
    temporary and shared, not a permanent per-son blacklist.

    IMPORTANT distinction (added after a real incident where a network outage got mis-recorded
    as "confirmed no price"): get_price() returns None for two very different reasons that its
    own return value alone can't tell apart -- a genuine "success": false response from Steam
    (which IS written to price_cache.json as a real, confirmed data=None entry) or a transient
    network/SSL/rate-limit failure (which is NEVER cached, precisely so a later retry isn't
    poisoned by a bad attempt). Treating both as equally "confirmed no price" would let a
    single bad network moment brand a perfectly priceable skin invalid for a full
    INVALID_RECHECK_SECONDS window. So after a failed live fetch, this re-checks the cache: if
    Steam's confirmed-empty entry is now present, the "no Steam price" verdict is trustworthy;
    if the cache is still empty, we simply couldn't reach Steam this attempt and must not treat
    that as evidence of anything. Only a CONFIRMED Steam miss (combined with no third-party
    price either) is ever recorded via mark_invalid() below."""
    name = market_hash_name(skin_name, wear, stattrak)
    data, freshness = get_cached_price(name, currency)
    # True once Steam has genuinely confirmed (now or on a prior fetch still in the cache) that
    # this skin/wear has no listings -- as opposed to simply never having reached Steam
    # successfully, which must NOT be treated as proof of anything (see docstring above).
    confirmed_no_steam_price = data is None and freshness in ("fresh", "stale")
    if data is None and freshness == "missing" and live_fetch:
        data = get_price(name, currency, abort_check)
        if data is not None:
            freshness = "fresh"
        else:
            # Re-check the cache rather than assuming this attempt's failure means anything --
            # but unlike the original version of this fix, actually USE a real price if one
            # showed up in the meantime (e.g. a concurrent son or price_scan.py fetched it while
            # we were waiting on our own request/backoff) instead of discarding it and looking
            # only at the freshness label, which could misread "someone else just priced this"
            # as "confirmed no price" and wrongly invalidate a perfectly priceable skin.
            recheck_data, live_freshness = get_cached_price(name, currency)
            if recheck_data is not None:
                data = recheck_data
                freshness = live_freshness
            else:
                confirmed_no_steam_price = live_freshness != "missing"
                freshness = "missing"
    # A skin whose only Steam signal is median_price (thin market: one historic sale, no active
    # listing) is still genuinely priced -- the output-pricing loop below already accepts
    # median_price as a fallback (see `price = data.get("median_price") or data.get("lowest_price")`
    # further down); requiring lowest_price specifically here made those skins perpetually fail
    # this gate and fall through to being marked invalid despite having a real price.
    if data is not None and (data.get("lowest_price") is not None or data.get("median_price") is not None):
        invalid_skins_mod.clear_name(name, currency)
        return data, freshness
    if not live_fetch:
        # We never actually exhausted the live sources -- a plain cache miss here just means this
        # skin isn't warmed yet, NOT that it's priceless. Don't brand it invalid on that basis.
        return None, "missing"
    if not allow_thirdparty:
        # Output-pricing rule (see docstring): no Steam price = no honest Steam revenue. Not
        # marked invalid either -- the third-party sources were never consulted on this path.
        return None, "missing"
    # Steam came up empty -- try third-party sites as a genuine price fallback
    tp = _thirdparty_prices(skin_name, wear, stattrak, currency, abort_check)
    tp_price = _cheapest_thirdparty(tp)
    if tp_price is not None:
        invalid_skins_mod.clear_name(name, currency)
        return ({"lowest_price": tp_price, "median_price": tp_price, "volume": None,
                 "thirdparty": tp}, "thirdparty")
    # Only mark invalid when Steam's absence was CONFIRMED, not just unreachable this attempt --
    # a transient failure should simply come up "missing" again next time and get retried like
    # any other cold skin, not get branded invalid off a network hiccup (see docstring above).
    if confirmed_no_steam_price:
        invalid_skins_mod.mark_invalid(skin_name, wear, stattrak, currency, reason="no_price_any_source")
    return None, "missing"


def _apply_mispriced_mark(skin_name: str, wear: str, stattrak: bool, currency: int,
                           score: float, reasons: list) -> tuple[float, list]:
    """Folds a currently-active verified-mispricing mark (mispriced_skins.py -- written by the
    Stage-2 verification queue / the Verify buttons) into a unit's dead-skin score: a flagged
    skin gets MISPRICED_DEAD_SKIN_POINTS, enough to cross dead_skin.DEAD_SKIN_THRESHOLD on its
    own, so trials built on it are excluded from training and strict agents' best lists --
    the "adjust the bots' training to avoid the over/undervalued skin" feedback loop. Cheap
    in-memory lookup; no network."""
    name = market_hash_name(skin_name, wear, stattrak)
    gap = mispriced_skins_mod.active_gap(name, currency)
    if gap is None:
        return score, reasons
    reason = "verified_overvalued" if gap >= 0 else "verified_undervalued"
    if reason not in reasons:
        score = min(score + mispriced_skins_mod.MISPRICED_DEAD_SKIN_POINTS, 100.0)
        reasons = reasons + [reason]
    return score, reasons


@dataclass
class SimulationRecord:
    id: str
    simulated_at: float
    from_rarity: str
    to_rarity: str
    stattrak: bool
    primary_collection: str
    input_mix: list             # [{"collection","skin","wear","count","unit_price","image","volume",
    # "skinport_price","csfloat_price"}, ...] -- skinport/csfloat prices are only looked up (and
    # otherwise stay None) when Steam's own volume already looked thin, see simulate()
    cost: float
    expected_revenue: float
    expected_revenue_after_fee: float
    expected_profit: float
    expected_roi_pct: float
    output_distribution: list   # [{"skin","wear","prob_pct","price","net_profit","image","volume",
    # "skinport_price","csfloat_price"}, ...] -- skinport/csfloat are only looked up (and
    # otherwise stay None) when this output's Steam volume already looked thin, see simulate()
    priced_output_variants: int
    n_output_variants: int
    cost_basis_source: str      # "cache" (every input price was fresh) | "stale" (>=1 input
    # price had expired past PRICE_TTL_SECONDS but was used anyway -- all 10/5 inputs were
    # still priced, just not all within the TTL)
    low_volume_warning: bool = False  # True if any COUNTED input/output's dead_skin.py score
    # (see dead_skin.score_from_signals/refine_with_thirdparty) crosses dead_skin.DEAD_SKIN_
    # THRESHOLD -- the "someone lists it high but nobody's actually buying" case, now a
    # graduated score (reported-zero volume, thin volume, a large gap vs. third-party prices)
    # instead of a flat volume<5 cutoff. Computed from data steam_prices.py already cached plus,
    # only once already-thin, a third-party lookup -- never an extra network call otherwise.
    # sim_learning.py's train() and sim_loop.py's _consider_best() both exclude these rows from
    # training/the best-known leaderboard (still logged to simulations.jsonl either way -- see
    # log_simulation). The field name is kept for backward compat with existing consumers
    # (dashboard/CLI) even though the underlying computation is now dead_skin.py's, not a
    # separate "low volume" concept -- see that module's docstring.
    agent: int | None = None  # which population "son" (see sim_loop.py/agents.py) discovered
    # this -- set by sim_loop.run_round() right after simulate() returns (this module itself is
    # agent-agnostic, reused by every son). Persisted on the record itself (not just derivable
    # from which son's simulations.jsonl it's stored in) so it self-describes if ever read out
    # of context -- the spec's Memory System "agent that discovered it" field.
    confidence_score: float | None = None  # 0-100, computed from cost_basis_source freshness +
    # the worst counted unit's dead_skin score (see simulate()) -- the NON-verified half of
    # ranking.py's confidence_score; ranking.py prefers this stored value when present instead
    # of recomputing it, and adds its own verified-Stage-2 bonus on top once verified_roi_pct
    # (below) exists.
    verified_roi_pct: float | None = None  # populated after the fact by EITHER the dashboard's
    # manual "Verify prices" button or verify_queue.py's automatic queue (see that module) --
    # same fields, two triggers. None until a live Skinport/CSFloat cross-check has actually run.
    verified_at: float | None = None
    avg_input_float: float = 0.5  # oddsmath.average_normalized_float(float_inputs) -- this
    # candidate's count-weighted input float, normalized 0 (cleanest) - 1 (worst) across each
    # input's OWN wear range, i.e. the same number that decided the landed output wear. Exposed
    # here (instead of only ever being a throwaway local in simulate()) so callers like
    # sim_loop.py can track a son's "preferred float" over time without recomputing it. Default
    # 0.5 (oddsmath's own "no inputs" neutral fallback) purely for backward compat when reading
    # back OLDER simulations.jsonl lines logged before this field existed -- never actually used
    # for a freshly simulated record, which always sets this explicitly below.
    ml_predicted_roi_pct: float | None = None  # this son's OWN sim_learning.py model's
    # pre-simulation prediction for this exact candidate (see sim_learning.predict_expected_roi),
    # set by sim_loop.run_round() right after simulate() returns -- same "one place that actually
    # knows" pattern as the `agent` field above, since this module stays agent-agnostic and has
    # no model_dir/sims_file of its own. None if no model was trained yet at draw time, or for
    # any record logged before this field existed. Lets ranking.py compare the model's guess
    # against the exact formula-computed expected_roi_pct as an independent trust signal (see
    # ranking._ml_agreement_bonus) -- close agreement is a real confidence boost that a purely
    # cost-basis/dead-skin-based score can't see, without ever overriding the authoritative
    # formula math itself.


@dataclass
class InvalidSimulation:
    """A candidate that could NOT be turned into a real SimulationRecord -- either not every
    input unit had a cached price (a real trade-up needs ALL of them, never a subset), or
    nothing on the output side had a price at all. Logged separately (see log_invalid /
    data/sons/son_N/invalid_simulations.jsonl) purely for visibility into how often/why the
    cache is coming up short -- never trained on, never ranked, never treated as a candidate."""
    id: str
    simulated_at: float
    from_rarity: str
    to_rarity: str
    stattrak: bool
    reason: str  # "incomplete_inputs" (fewer than n_required_input_units got priced) |
    # "no_output_price" (every input priced, but no output has a usable price)
    n_priced_input_units: int
    n_required_input_units: int
    primary_collection: str  # best-effort label (the leg with the most priced units); "" if
    # literally nothing was priced at all


def _new_id(prefix: str = "sim") -> str:
    return f"{prefix}-{time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime())}-{time.time_ns() % 1_000_000:06d}"


def simulate(candidate, currency: int = 1, abort_check=None, live_fetch: bool = True) -> "SimulationRecord | InvalidSimulation":
    """Prices every leg's cached wears, computes group odds across legs
    (oddsmath.group_odds), the count-weighted output float
    (oddsmath.average_normalized_float + remap_float_to_wear per possible output skin), and
    the probability-weighted expected ROI across every possible output.

    Returns an InvalidSimulation (never None, never a guess) if either: (a) fewer than every
    single input unit got a real price (cached or, on a miss, freshly live-fetched -- see
    _price()) -- a real trade-up needs all 10 (or 5) inputs, so a partially-priced candidate
    isn't a smaller version of the same trade, it's just not a valid trade; or (b) every input
    priced fine but nothing on the output side has a usable price, so there's no revenue to
    compute at all."""
    # User skin blacklist (user_overrides.py) -- a HARD gate, checked before any pricing is
    # spent: a contract touching a blacklisted skin on EITHER side can never be honestly
    # valued (the user has personally branded its data garbage), so whatever generator
    # produced this candidate (random draw, mutation, evolution parent, manual builder), it
    # dies here as an InvalidSimulation rather than reaching any best list.
    blacklisted = user_overrides_mod.blacklisted_skins()
    if blacklisted:
        touched = {u["skin"]["name"] for leg in candidate.legs for u in leg.units} | \
                  {s["name"] for leg in candidate.legs for s in leg.outputs}
        if touched & blacklisted:
            return InvalidSimulation(
                id=_new_id("invalid"), simulated_at=time.time(),
                from_rarity=candidate.from_rarity_name, to_rarity=candidate.to_rarity_name,
                stattrak=candidate.stattrak, reason="blacklisted_skin",
                n_priced_input_units=0,
                n_required_input_units=sum(u["count"] for leg in candidate.legs for u in leg.units),
                primary_collection=candidate.legs[0].collection if candidate.legs else "")

    groups = []        # (priced_count, n_outputs) per leg, for oddsmath.group_odds
    float_inputs = []  # (skin, wear, count) per (leg, unit), for average_normalized_float
    input_mix_rows = []
    cost = 0.0
    any_stale = False
    any_low_volume = False
    n_required_input_units = 0
    n_priced_input_units = 0

    leg_priced_counts = []  # parallel to candidate.legs -- how many of each leg's units got priced
    for leg in candidate.legs:
        leg_priced_units = 0
        for unit in leg.units:  # possibly several DISTINCT skins within this one collection leg
            n_required_input_units += unit["count"]
            skin, wear, count = unit["skin"], unit["wear"], unit["count"]
            data, freshness = _price(skin["name"], wear, candidate.stattrak, currency, abort_check, live_fetch)
            # median_price fallback matches _price()'s own "is this priced at all" gate and the
            # output-pricing loop below -- a thin-market skin with only a median_price (no active
            # lowest_price listing) is still a real, usable price.
            price = (data.get("lowest_price") or data.get("median_price")) if data else None
            if price is None:
                continue  # no price on Steam OR any 3rd-party site -- _price already recorded
                # this skin in invalid_skins.py; this candidate falls into incomplete_inputs below
            if freshness == "stale":
                any_stale = True
            volume = data.get("volume")
            tp = data.get("thirdparty") or {}  # non-empty when the price itself came from a
            # third-party fallback (Steam had nothing) -- surfaced on the input row for free
            # score_from_signals(volume) with no history is the zero-network-call gate --
            # its volume-based buckets exactly mirror the old _is_low_volume(volume) trigger
            # set (see dead_skin.py's module docstring), so this fetches third-party
            # corroboration in exactly the same cases as before. The unstable_pricing
            # bucket (needs a price_history.py file scan) is deliberately skipped here --
            # too expensive to do per input unit in this hot loop -- and left to the
            # weekly-reviewed persisted store instead (dead_skin.run_weekly_review).
            score, reasons = dead_skin_mod.score_from_signals(volume)
            if score > 0:
                # Steam's own liquidity already looks thin -- try third-party corroboration
                # for extra visibility, but a miss here no longer disqualifies the whole trade
                # (that made the feature nearly unusable in practice -- see README's Risks note).
                tp = _thirdparty_prices(skin["name"], wear, candidate.stattrak, currency, abort_check)
                score, reasons = dead_skin_mod.refine_with_thirdparty(score, reasons, price, tp)
            score, reasons = _apply_mispriced_mark(skin["name"], wear, candidate.stattrak,
                                                    currency, score, reasons)
            if dead_skin_mod.is_dead(score):
                any_low_volume = True
            cost += price * count
            leg_priced_units += count
            n_priced_input_units += count
            float_inputs.append((skin, wear, count))
            input_mix_rows.append({"collection": leg.collection, "skin": skin["name"],
                                    "wear": wear, "count": count, "unit_price": price,
                                    "image": skin.get("image", ""), "volume": volume,
                                    "skinport_price": tp.get("skinport"), "buff163_price": tp.get("buff163"),
                                    "csfloat_price": tp.get("csfloat"),
                                    "dead_skin_score": score, "dead_skin_reasons": reasons})
        groups.append((leg_priced_units, len(leg.outputs)))
        leg_priced_counts.append(leg_priced_units)

    # The candidate's nominal "primary" leg (legs[0]) can end up entirely unpriced while a
    # filler leg is fully priced -- report whichever leg actually dominates the COSTED input
    # mix as primary_collection (best-effort, even for an invalid result), not blindly legs[0].
    dominant_idx = max(range(len(candidate.legs)), key=lambda i: leg_priced_counts[i])
    primary_collection = candidate.legs[dominant_idx].collection if any(leg_priced_counts) else ""

    if n_priced_input_units < n_required_input_units:
        return InvalidSimulation(
            id=_new_id("invalid"), simulated_at=time.time(), from_rarity=candidate.from_rarity_name,
            to_rarity=candidate.to_rarity_name, stattrak=candidate.stattrak, reason="incomplete_inputs",
            n_priced_input_units=n_priced_input_units, n_required_input_units=n_required_input_units,
            primary_collection=primary_collection)

    avg_norm = oddsmath.average_normalized_float(float_inputs)
    group_probs = oddsmath.group_odds(groups)
    # Sampled distribution of the input-average float (each input drawn uniformly within its
    # wear bracket instead of pinned to the midpoint) -- computed ONCE per contract, then
    # bucketed per output skin into {wear: %} so each output row can show "could land FT 72% /
    # MW 28%" instead of only the single midpoint wear. See oddsmath.sample_avg_normalized_floats.
    avg_norm_samples = oddsmath.sample_avg_normalized_floats(float_inputs)

    output_dist = []
    revenue_weighted = []
    priced_variants = 0
    n_variants = 0
    for leg, group_prob in zip(candidate.legs, group_probs):
        for skin in leg.outputs:
            n_variants += 1
            landed_wear = oddsmath.remap_float_to_wear(skin, avg_norm)
            # allow_thirdparty=False: revenue must be backed by real Steam sale evidence --
            # an output with no Steam price but four-figure Buff163/Skinport listings (e.g.
            # AWP | Gungnir, which trades above Steam's price cap) is unsellable at that
            # price on the market this tool's fee/revenue math assumes. See _price().
            data, _ = _price(skin["name"], landed_wear, candidate.stattrak, currency, abort_check,
                             live_fetch, allow_thirdparty=False)
            price = (data.get("median_price") or data.get("lowest_price")) if data else None
            volume = data.get("volume") if data else None
            tp = (data.get("thirdparty") or {}) if data else {}
            score, reasons = dead_skin_mod.score_from_signals(volume) if price is not None else (0.0, [])
            if price is None and bulk_prices_mod.has_market_listing(
                    market_hash_name(skin["name"], landed_wear, candidate.stattrak)) is False:
                # Visibility only (the row is already unpriced/excluded from revenue): the
                # Steam feed POSITIVELY confirms no non-souvenir copy has sold in 90 days --
                # this output can't be sold on Steam at any believable price right now.
                reasons = ["not_sellable_on_steam"]
            if score > 0:
                # Steam's own liquidity already looks thin -- try third-party corroboration for
                # extra visibility, but a miss no longer disqualifies this output (that made the
                # feature nearly unusable in practice -- see README's Risks note).
                tp = _thirdparty_prices(skin["name"], landed_wear, candidate.stattrak, currency, abort_check)
                score, reasons = dead_skin_mod.refine_with_thirdparty(score, reasons, price, tp)
            if price is not None:
                score, reasons = _apply_mispriced_mark(skin["name"], landed_wear, candidate.stattrak,
                                                        currency, score, reasons)
            is_dead_output = price is not None and dead_skin_mod.is_dead(score)
            net_profit = (price * (1 - STEAM_SELL_FEE) - cost) if price is not None else None
            output_dist.append({"skin": skin["name"], "wear": landed_wear, "prob_pct": group_prob,
                                 "price": price, "net_profit": net_profit,
                                 "image": skin.get("image", ""), "volume": volume,
                                 "skinport_price": tp.get("skinport"), "buff163_price": tp.get("buff163"),
                                 "csfloat_price": tp.get("csfloat"),
                                 "dead_skin_score": score, "dead_skin_reasons": reasons,
                                 "wear_probs": oddsmath.wear_probabilities_for_output(skin, avg_norm_samples)})
            if price is not None:
                priced_variants += 1
                revenue_weighted.append((price, group_prob))
                if group_prob > 0 and is_dead_output:
                    any_low_volume = True

    total_prob = sum(p for _, p in revenue_weighted)
    if not revenue_weighted or total_prob <= 0:
        return InvalidSimulation(
            id=_new_id("invalid"), simulated_at=time.time(), from_rarity=candidate.from_rarity_name,
            to_rarity=candidate.to_rarity_name, stattrak=candidate.stattrak, reason="no_output_price",
            n_priced_input_units=n_priced_input_units, n_required_input_units=n_required_input_units,
            primary_collection=primary_collection)
    expected_revenue = sum(v * p for v, p in revenue_weighted) / total_prob
    after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
    profit = after_fee - cost
    roi = (profit / cost * 100) if cost else 0.0

    basis = "stale" if any_stale else "cache"

    # confidence_score: blends cost-basis freshness with the worst counted unit's dead-skin
    # score -- the NON-verified half of ranking.py's own confidence formula (kept in sync
    # deliberately; see that module's _confidence_score, which prefers this stored value once
    # present and adds its own verified-Stage-2 bonus on top).
    basis_score = 100.0 if basis == "cache" else 60.0
    # Non-volume-only: liquidity.py's own factor already penalizes volume-based dead-skin
    # reasons (no_recent_sales/very_low_listings) directly, so stripping them back out here
    # avoids counting the same underlying volume problem twice under two different names
    # (see dead_skin.non_volume_score's docstring).
    worst_dead_skin = max((dead_skin_mod.non_volume_score(u.get("dead_skin_score", 0.0) or 0.0,
                                                            u.get("dead_skin_reasons"))
                            for u in input_mix_rows + output_dist), default=0.0)
    confidence_score = min(100.0, (basis_score + (100.0 - worst_dead_skin)) / 2)

    return SimulationRecord(
        id=_new_id(), simulated_at=time.time(), from_rarity=candidate.from_rarity_name,
        to_rarity=candidate.to_rarity_name, stattrak=candidate.stattrak,
        primary_collection=primary_collection, input_mix=input_mix_rows, cost=cost,
        expected_revenue=expected_revenue, expected_revenue_after_fee=after_fee,
        expected_profit=profit, expected_roi_pct=roi, avg_input_float=avg_norm,
        output_distribution=output_dist,
        priced_output_variants=priced_variants, n_output_variants=n_variants,
        cost_basis_source=basis, low_volume_warning=any_low_volume,
        confidence_score=confidence_score,
    )


def log_simulation(record: SimulationRecord, sims_file: Path) -> None:
    sims_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(record))
    with _write_lock:
        with open(sims_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def log_invalid(record: InvalidSimulation, invalid_file: Path) -> None:
    invalid_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(record))
    with _write_lock:
        with open(invalid_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_invalid_simulations(invalid_file: Path) -> list:
    """Same skip-corrupt-lines convention as load_simulations. Purely for visibility/debugging
    (e.g. "why is my cache-warm son still finding so few valid trades?") -- nothing in
    sim_learning.py or sim_loop.py's best-known list ever reads this back in."""
    if not invalid_file.exists():
        return []
    out = []
    with open(invalid_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(InvalidSimulation(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
    return out


def load_simulations(sims_file: Path) -> list:
    """Skips (rather than raises on) any corrupt trailing line, same convention as
    outcomes.load_outcomes()."""
    if not sims_file.exists():
        return []
    out = []
    with open(sims_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(SimulationRecord(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
    return out
