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


def _normalize(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.5  # no spread across this set -- neutral rather than div-by-zero
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


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
    time, or a legacy record predating this field) contributes no bonus and no penalty."""
    predicted = entry.get("ml_predicted_roi_pct")
    if predicted is None:
        return 0.0
    residual = abs(predicted - _effective_roi(entry))
    return ML_AGREEMENT_MAX_BONUS * max(0.0, 1.0 - residual / ML_AGREEMENT_RESIDUAL_CAP_PP)


def _confidence_score(entry: dict) -> float:
    """0-100. Prefers the STORED confidence_score (simulate.SimulationRecord.confidence_score,
    computed once at simulate()-time from the same cost-basis-freshness + worst-dead-skin-score
    formula) when present, rather than recomputing it -- falls back to computing it here for
    entries logged before that field existed. Either way, adds two bonuses that can only be known
    after simulate()-time: one once this entry has actually been through Stage-2 verification
    with a clean result, and one for how well this son's own model anticipated the result (see
    _ml_agreement_bonus) -- neither is ever part of the stored base value."""
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
    return min(100.0, base + verified_bonus + _ml_agreement_bonus(entry))


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


def score_breakdown(entry: dict, roi_lo: float, roi_hi: float, profit_lo: float, profit_hi: float) -> dict:
    return {
        "roi": _normalize(_effective_roi(entry), roi_lo, roi_hi),
        "profit": _normalize(_profit_amount(entry), profit_lo, profit_hi),
        "liquidity": _liquidity_score(entry) / 100.0,
        "confidence": _confidence_score(entry) / 100.0,
        "discovery": _discovery_value(entry) / 100.0,
    }


def rank(entries: list) -> list:
    """Ranks `entries` (checkpoint `best`-list dicts, from any/all agents) by the spec's
    weighted composite score instead of raw ROI alone. ROI/profit are unbounded, so
    they're min-max normalized against the OTHER entries in THIS SAME call (a relative
    comparison across the current candidate set, not an absolute scale) -- liquidity/
    confidence/discovery are already 0-100 scores and don't need it.

    Non-destructive: returns NEW dicts (never mutates the input entries, which may be
    live references into a son's in-memory checkpoint) with 'composite_score',
    'score_breakdown', and 'effective_roi_pct'/'profit_amount' added for transparency --
    the dashboard can show the raw ROI% column right alongside this ranking.

    CAVEAT: because roi/profit are normalized against the min/max of THIS SAME `entries`
    list, composite_score is only comparable WITHIN one call -- it is not a stable,
    absolute 0-100 scale. Calling this on a smaller/filtered subset (e.g. the archive
    endpoint's collection/min_roi/agent filters) re-scales roi/profit against that
    narrower set, so the same underlying entry can get a different composite_score
    depending on what else was in the list it was ranked alongside. Don't compare
    composite_score across two different filtered queries as if it were absolute."""
    if not entries:
        return []
    # market_reliability.py's unified Excluded state is kept out of the reward-like composite
    # score entirely (per spec: "excluded from reward calculations"), not just dampened like an
    # ordinary low-confidence entry -- see that module's docstring.
    entries = [e for e in entries if not market_reliability_mod.any_unit_excluded(
        e.get("input_mix") or [], e.get("output_distribution") or [],
        currency=1, stattrak=e.get("stattrak", False))]
    if not entries:
        return []
    rois = [_effective_roi(e) for e in entries]
    profits = [_profit_amount(e) for e in entries]
    roi_lo, roi_hi = min(rois), max(rois)
    profit_lo, profit_hi = min(profits), max(profits)

    scored = []
    for e in entries:
        breakdown = score_breakdown(e, roi_lo, roi_hi, profit_lo, profit_hi)
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
