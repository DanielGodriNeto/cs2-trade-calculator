"""Weighted final ranking across all 4 agents' finds: 60% real ROI, 15% profit amount,
10% liquidity, 10% confidence, 5% discovery value -- "a smaller but reliable profit
should beat fake high ROI," per spec, instead of sorting by raw ROI% alone (which is
exactly what let a thin, fake-liquidity result look better than a smaller, trustworthy
one in the first place -- see dead_skin.py for why that's a real failure mode this tool
already guards against elsewhere).

Operates on sim_loop.py's checkpoint `best`-list dicts (plain JSON-shaped, not
dataclasses) so it has no dependency on sim_loop.py itself -- sim_loop.list_best_
simulations() calls rank() as its final sort step instead of a bare key=roi sort.

Two refinements on top of a plain weighted average of the 5 spec factors:
1. The 60% ROI weight is SCALED by the discovering agent's own roi_weight parameter
   (agents.py) before use, redistributing whatever's freed proportionally across the
   other 4 factors -- see _effective_weights. An agent explicitly built to chase raw
   ROI (e.g. ROI Accountant, roi_weight=1.0) keeps the full spec weight; one that isn't
   (e.g. Discovery Agent, roi_weight=0.50) has its finds judged more on liquidity/
   confidence/discovery instead, since an eye-catching ROI from an agent that doesn't
   even optimize for ROI is exactly the number this ranking should be more skeptical of.
2. A multiplicative reliability dampener (see rank()) scales the final composite by the
   entry's own liquidity/confidence average -- a maximally unreliable entry is cut to
   half its weighted score, a maximally reliable one is untouched. This makes "beats
   fake high ROI" a real, structural ranking pressure rather than something a large
   enough raw ROI/profit gap could always out-weigh in a plain weighted average.

Confidence also folds in a small, capped bonus (see _ml_agreement_bonus) for how closely the
discovering agent's OWN sim_learning.py model anticipated this exact candidate's ROI before it
was simulated -- previously that trained model only ever influenced which candidates got
sampled (random_trade.py), never how much the final ranking trusts a result once found.
"""
import math

from . import agents as agents_mod
from . import dead_skin as dead_skin_mod
from . import market_reliability as market_reliability_mod

MAX_MIXED_COLLECTIONS = 10  # primary + up to 9 filler (random_trade.MAX_FILLER_COLLECTIONS)
# -- used only to normalize discovery_value's 0-100 scale, not re-imported from
# random_trade.py to avoid a needless dependency for one constant.

BASE_WEIGHTS = {"roi": 0.60, "profit": 0.15, "liquidity": 0.10, "confidence": 0.10, "discovery": 0.05}


def _effective_weights(entry: dict) -> dict:
    """See module docstring, refinement 1. Falls back to BASE_WEIGHTS unscaled if the
    entry carries no recognizable son id (e.g. a hand-built entry in a test)."""
    son_id = entry.get("son")
    if son_id is None:
        return dict(BASE_WEIGHTS)
    try:
        roi_weight = agents_mod.profile_for(son_id).roi_weight
    except KeyError:
        return dict(BASE_WEIGHTS)
    scaled_roi = BASE_WEIGHTS["roi"] * roi_weight
    freed = BASE_WEIGHTS["roi"] - scaled_roi
    other_total = 1.0 - BASE_WEIGHTS["roi"]
    weights = {"roi": scaled_roi}
    for k in ("profit", "liquidity", "confidence", "discovery"):
        weights[k] = BASE_WEIGHTS[k] + freed * (BASE_WEIGHTS[k] / other_total)
    return weights


ROI_SQUASH_CENTER = 25.0   # ROI% mapped to 0.5 by the sigmoid -- a "solidly good" trade-up
ROI_SQUASH_SCALE = 25.0    # ROI% change for a ~0.23 score change near the centre
PROFIT_SQUASH_REF = 500.0  # absolute profit ($) mapped to ~0.76; log1p keeps small profits legible


def _squash_roi(roi_pct: float) -> float:
    """Absolute 0-1 sigmoid of ROI% -- fixed scale, independent of the other entries in
    this call (see rank()'s CAVEAT). ROI_SQUASH_CENTER -> 0.5."""
    return 1.0 / (1.0 + math.exp(-(roi_pct - ROI_SQUASH_CENTER) / ROI_SQUASH_SCALE))


def _squash_profit(profit: float) -> float:
    """Absolute 0-1 log squash of profit ($). <= 0 -> 0.0; PROFIT_SQUASH_REF -> ~0.76;
    saturates at 1.0. Fixed scale, independent of the other entries in this call."""
    if profit <= 0:
        return 0.0
    return min(1.0, math.log1p(profit) / math.log1p(PROFIT_SQUASH_REF))


def effective_roi(entry: dict) -> float:
    """Prefers a Stage-2-VERIFIED ROI (verify_queue.py / the manual "Verify prices"
    button) over the plain cache-priced estimate, when one exists -- the whole point of
    verification is that it's the more trustworthy number once it's available. Public (not
    just this module's internal use) so sim_loop.py's checkpoint admission/ordering can rank
    "best" by the same verified-aware number this module's own composite score already uses,
    instead of the raw simulated estimate alone."""
    verified = entry.get("verified_roi_pct")
    return verified if verified is not None else entry.get("expected_roi_pct", 0.0)


_effective_roi = effective_roi  # internal alias -- every call site below predates the rename


def _profit_amount(entry: dict) -> float:
    cost = entry.get("cost", 0.0) or 0.0
    return cost * (_effective_roi(entry) / 100.0)


def _liquidity_score(entry: dict) -> float:
    """0-100. Penalizes counted units whose dead-skin flag came from a VOLUME-based
    reason specifically (no_recent_sales/very_low_listings, see dead_skin.py) --
    deliberately excludes the price-gap/instability reasons, which feed confidence_score
    instead, keeping the two spec factors conceptually distinct rather than double
    counting the same underlying dead_skin_score twice."""
    units = list(entry.get("input_mix") or []) + list(entry.get("output_distribution") or [])
    if not units:
        return 50.0  # unknown -- neutral, not a penalty for missing data
    hits = sum(1 for u in units
               if any(r in ("no_recent_sales", "very_low_listings") for r in (u.get("dead_skin_reasons") or [])))
    return max(0.0, 100.0 * (1 - hits / len(units)))


ML_AGREEMENT_MAX_BONUS = 10.0     # small and deliberately capped -- a secondary trust signal
# layered on top of the primary cost-basis/dead-skin confidence, never a replacement for it
ML_AGREEMENT_RESIDUAL_CAP_PP = 25.0  # |predicted - actual| ROI, in percentage points, at or
# beyond which the bonus is zero -- roughly 3-5x the CV MAE this project's sim-population models
# typically train at (see sim_learning.TrainResult.validation_mae), so a genuinely well-
# calibrated model still earns most of the bonus while a wild miss earns none.


ML_AGREEMENT_UNVERIFIED_SCALE = 0.5  # see _ml_agreement_bonus: halved when effective_roi is still
# the Steam/third-party-derived expected_roi_pct estimate rather than a Stage-2-verified number --
# two theoretically-derived numbers (the sim model's guess and simulate.py's own formula estimate)
# agreeing is a weaker trust signal than the model agreeing with a verified, execution-checked ROI.


ENSEMBLE_MAX_BONUS = 6.0        # sibling of ML_AGREEMENT_MAX_BONUS -- capped ensemble-agreement trust
ENSEMBLE_SPREAD_CAP_PP = 30.0   # ensemble p90-p10 (ROI%pp) at/beyond which the bonus is zero


def _ml_agreement_bonus(entry: dict) -> float:
    """0-ML_AGREEMENT_MAX_BONUS. Some entries carry ml_predicted_roi_pct (simulate.
    SimulationRecord.ml_predicted_roi_pct) -- the discovering agent's OWN sim_learning.py model's
    pre-simulation guess at this exact candidate's ROI, set by sim_loop.run_round() only when
    that agent already had a trained model at draw time. Comparing it against the exact
    formula-computed effective_roi is an independent trust signal: a candidate the model
    confidently anticipated is more likely to reflect a real, learnable pattern than one that
    caught the model completely off guard -- neither cost-basis freshness nor dead-skin liquidity
    can see this. The exact math (expected_roi_pct/verified_roi_pct) always stays authoritative;
    a large residual only means this specific find is the kind of thing the model doesn't
    understand well yet, not that the entry itself is wrong. None (no model trained yet at draw
    time, or a legacy record predating this field) contributes no bonus and no penalty.

    CAVEAT (do not remove without re-reading): sim_learning.py's model and simulate.py's
    expected_roi_pct both ultimately derive from the same Steam/third-party listing-price data,
    which only weakly represents true liquidity/execution risk (a listing price is not a
    guaranteed fill). Until an entry has a real verified_roi_pct (Stage-2 price verification),
    "agreement" here means two model-adjacent numbers correlate, not that real-world execution
    risk is low -- so the bonus is scaled down by ML_AGREEMENT_UNVERIFIED_SCALE unless the entry
    is already verified, where the comparison carries more weight."""
    predicted = entry.get("ml_predicted_roi_pct")
    if predicted is None:
        return 0.0
    residual = abs(predicted - _effective_roi(entry))
    bonus = ML_AGREEMENT_MAX_BONUS * max(0.0, 1.0 - residual / ML_AGREEMENT_RESIDUAL_CAP_PP)
    if entry.get("verified_roi_pct") is None:
        bonus *= ML_AGREEMENT_UNVERIFIED_SCALE
    return bonus


def _ensemble_agreement_bonus(entry: dict) -> float:
    """0-ENSEMBLE_MAX_BONUS. When sim_loop attached ensemble_roi_median / ensemble_roi_spread
    (>=3 of the 6 sons' models scored this exact composition), reward a consensus that is BOTH
    tight AND matches the formula ROI: full bonus when the spread is small (< ENSEMBLE_SPREAD_CAP_PP)
    AND the median is within ML_AGREEMENT_RESIDUAL_CAP_PP of effective_roi; zero when the spread is
    wide or the median disagrees. Scaled by ML_AGREEMENT_UNVERIFIED_SCALE until Stage-2 verified,
    exactly like the _ml_agreement_bonus sibling. Missing ensemble_* keys -> 0.0 (no penalty)."""
    median = entry.get("ensemble_roi_median")
    spread = entry.get("ensemble_roi_spread")
    if median is None or spread is None:
        return 0.0
    residual = abs(median - _effective_roi(entry))
    agree = max(0.0, 1.0 - residual / ML_AGREEMENT_RESIDUAL_CAP_PP)
    tight = max(0.0, 1.0 - spread / ENSEMBLE_SPREAD_CAP_PP)
    bonus = ENSEMBLE_MAX_BONUS * agree * tight
    if entry.get("verified_roi_pct") is None:
        bonus *= ML_AGREEMENT_UNVERIFIED_SCALE
    return bonus


def _confidence_score(entry: dict) -> float:
    """0-100. Prefers the STORED confidence_score (simulate.SimulationRecord.confidence_score,
    computed once at simulate()-time from the same cost-basis-freshness + worst-dead-skin-score
    formula) when present, rather than recomputing it -- falls back to computing it here for
    entries logged before that field existed. Either way, adds bonuses that can only be known
    after simulate()-time: one once this entry has actually been through Stage-2 verification
    with a clean result, one for how well this son's own model anticipated the result (see
    _ml_agreement_bonus), and one for how tightly ALL the sons' models agree on it and whether
    that consensus matches the formula ROI (see _ensemble_agreement_bonus) -- none is ever part
    of the stored base value."""
    stored = entry.get("confidence_score")
    if stored is not None:
        base = stored
    else:
        basis_score = 100.0 if entry.get("cost_basis_source") == "cache" else 60.0
        units = list(entry.get("input_mix") or []) + list(entry.get("output_distribution") or [])
        # Non-volume-only, matching simulate.py's stored computation -- see
        # dead_skin.non_volume_score's docstring for why (avoids double-counting the same
        # volume-based dead-skin problem this function's own _liquidity_score already penalizes).
        worst_dead_skin = max((dead_skin_mod.non_volume_score(u.get("dead_skin_score", 0.0) or 0.0,
                                                                u.get("dead_skin_reasons"))
                                for u in units), default=0.0)
        base = (basis_score + (100.0 - worst_dead_skin)) / 2
    verified_bonus = 0.0
    if entry.get("verified_roi_pct") is not None:
        verified_bonus = 15.0 if entry.get("price_confidence") == "ok" else 5.0
    return min(100.0, base + verified_bonus + _ml_agreement_bonus(entry) + _ensemble_agreement_bonus(entry))


def _discovery_value(entry: dict) -> float:
    """0-100. Rewards structurally UNUSUAL candidates -- more distinct collections mixed
    into the same contract (the Collection Mixer/Discovery Agent's hallmark) score
    higher, normalized against MAX_MIXED_COLLECTIONS so one extra filler collection isn't
    already "maximally novel"."""
    collections = {u.get("collection") for u in (entry.get("input_mix") or []) if u.get("collection")}
    n = len(collections)
    if n <= 1:
        return 0.0
    return min(100.0, (n - 1) / (MAX_MIXED_COLLECTIONS - 1) * 100.0)


def _has_unpriced_output(entry: dict) -> bool:
    """Whether any outcome this contract can land on has no price. An entry with no
    output_distribution at all (a hand-built entry in a test, a legacy shape) is NOT treated as
    unpriced -- there's nothing to judge, and rank() shouldn't silently drop it."""
    return any(o.get("price") is None for o in (entry.get("output_distribution") or []))


def score_breakdown(entry: dict) -> dict:
    """Per-factor 0-1 sub-scores for `entry`. roi/profit are ABSOLUTE squashes (fixed
    sigmoid / log against module constants) -- their value depends only on this entry, not
    on the other entries in the ranking call. liquidity/confidence/discovery are already
    0-1 after the /100."""
    return {
        "roi": _squash_roi(_effective_roi(entry)),
        "profit": _squash_profit(_profit_amount(entry)),
        "liquidity": _liquidity_score(entry) / 100.0,
        "confidence": _confidence_score(entry) / 100.0,
        "discovery": _discovery_value(entry) / 100.0,
    }


def rank(entries: list) -> list:
    """Ranks `entries` (checkpoint `best`-list dicts, from any/all agents) by the spec's
    weighted composite score instead of raw ROI alone. ROI/profit are unbounded, so
    they're squashed onto a FIXED absolute 0-1 scale (sigmoid for ROI%, log1p for profit
    $, against the ROI_SQUASH_* / PROFIT_SQUASH_REF module constants) -- liquidity/
    confidence/discovery are already 0-100 scores and just get /100.

    Non-destructive: returns NEW dicts (never mutates the input entries, which may be
    live references into a son's in-memory checkpoint) with 'composite_score',
    'score_breakdown', and 'effective_roi_pct'/'profit_amount' added for transparency --
    the dashboard can show the raw ROI% column right alongside this ranking.

    composite_score is a stable ABSOLUTE 0-100 scale: every input to it (the roi/profit
    squashes, _effective_weights, the reliability dampener) is a function of the single
    entry alone, so a given entry scores the same number whether it is ranked solo, in
    the full list, or in any filtered subset (e.g. the archive endpoint's collection/
    min_roi/agent filters). Two different filtered queries ARE comparable."""
    if not entries:
        return []
    # market_reliability.py's unified Excluded state is kept out of the reward-like composite
    # score entirely (per spec: "excluded from reward calculations"), not just dampened like an
    # ordinary low-confidence entry -- see that module's docstring.
    _rel_cache: dict = {}
    entries = [e for e in entries if not market_reliability_mod.any_unit_excluded(
        e.get("input_mix") or [], e.get("output_distribution") or [],
        currency=1, stattrak=e.get("stattrak", False), cache=_rel_cache)]
    # Same hard exclusion for a partially-priced output side. simulate() now refuses to build a
    # SimulationRecord at all in that case (reason="incomplete_outputs"), so this only ever
    # catches entries admitted to a best list BEFORE that gate existed -- but those are already
    # sitting in every son's checkpoint, and their ROI is the inflated priced-subset average
    # (see oddsmath's unpriced-outputs section), so they'd otherwise keep ranking as top
    # opportunities forever. Filtering on read retires them without rewriting live checkpoints.
    entries = [e for e in entries if not _has_unpriced_output(e)]
    if not entries:
        return []

    scored = []
    for e in entries:
        breakdown = score_breakdown(e)
        weights = _effective_weights(e)
        weighted = sum(weights[k] * v for k, v in breakdown.items()) * 100.0
        # Reliability dampener (module docstring, refinement 2): a maximally unreliable
        # entry (liquidity=confidence=0) is cut to half its weighted score; a maximally
        # reliable one (both 100) is untouched.
        reliability = (breakdown["liquidity"] + breakdown["confidence"]) / 2.0
        composite = weighted * (0.5 + 0.5 * reliability)
        out = dict(e)
        out["composite_score"] = composite
        out["score_breakdown"] = breakdown
        out["effective_roi_pct"] = _effective_roi(e)
        out["profit_amount"] = _profit_amount(e)
        scored.append(out)
    return sorted(scored, key=lambda e: e["composite_score"], reverse=True)
