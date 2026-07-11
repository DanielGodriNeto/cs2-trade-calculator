"""ML surrogate trained on one population son's own simulations.jsonl (src/simulate.py's
theoretical EV trials, see src/sim_loop.py for the data/sons/son_N/ layout), used ONLY to bias
src/random_trade.py's guided sampling toward promising regions of the random-candidate search
space.

IMPORTANT -- mirrors learning.py's own guardrail, adapted: this model is trained on
theoretical expected-value numbers from simulated trials (nothing bought or sold), never on
money actually made. It must never be presented as, compared against, or merged with
learning.py's real ROI-calibration model (trained only on data/outcomes.jsonl, genuine
user-executed trades) -- that would blur a search heuristic with a claim about real-world
execution risk this model has no way to observe. Its only consumer is random_trade.py's own
sampling bias in sim_loop.py.

Same lazy sklearn/joblib import pattern as learning.py, for the same reason: importing this
module costs nothing until train()/predict_expected_roi() is actually called.

Every population "son" (sim_loop.py) has its OWN model directory and simulations file, so
every function here takes explicit model_dir/sims_file parameters rather than a fixed
module-level path -- and the small in-process caches below (_loaded_models,
_collection_roi_caches) are keyed BY that path, so one son's loaded model/cache can never leak
into another son's prediction.
"""
from dataclasses import dataclass
import json
import threading
import time
from pathlib import Path

from . import data as data_mod
from . import market_reliability as market_reliability_mod
from . import ml_common
from . import oddsmath
from . import sim_blacklist as sim_blacklist_mod
from . import simulate as simulate_mod
from . import verified_low_roi as verified_low_roi_mod

MODEL_FILENAME = "sim_roi_model.joblib"
META_FILENAME = "sim_roi_model_meta.json"

MIN_TRAINING_SAMPLES = 20
RETRAIN_TRIGGER_DELTA = 25  # simulated rows accumulate far faster than real logged outcomes --
# a higher delta than retrain_loop.py's real-model 5 avoids retraining on every tiny batch

FEATURE_NAMES = ["rarity_rank", "n_legs", "n_distinct_wears", "wear_spread", "filler_fraction",
                  "stattrak", "cost", "collection_mean_expected_roi"]

RARITY_RANK_BY_NAME = {name: idx for idx, (_, name) in enumerate(data_mod.RARITY_CHAIN)}

_lock = threading.Lock()
_loaded_models: dict = {}          # model_dir -> (model, mtime)
_collection_roi_caches: dict = {}  # sims_file -> {"mtime":..., "lookup": {...}}


@dataclass
class TrainResult:
    trained: bool
    reason: str
    n_samples: int
    validation_mae: float | None
    feature_importances: dict | None
    model_name: str | None = None       # which candidate algorithm won, see ml_common.py
    candidate_maes: dict | None = None  # every candidate's own CV MAE, for transparency


def _wear_spread(input_mix: list) -> tuple:
    wears = {row["wear"] for row in input_mix}
    idxs = [oddsmath.WEAR_ORDER.index(w) for w in wears if w in oddsmath.WEAR_ORDER]
    spread = (max(idxs) - min(idxs)) if idxs else 0
    return len(wears), float(spread)


def _feature_vector(*, from_rarity, input_mix, primary_collection, stattrak, cost,
                     collection_mean_roi) -> list:
    n_legs = len({row["collection"] for row in input_mix}) or 1
    n_distinct_wears, wear_spread = _wear_spread(input_mix)
    primary_count = sum(row["count"] for row in input_mix if row["collection"] == primary_collection)
    total_count = sum(row["count"] for row in input_mix) or 1
    filler_fraction = 1.0 - (primary_count / total_count)
    return [
        float(RARITY_RANK_BY_NAME.get(from_rarity, float("nan"))),
        float(n_legs),
        float(n_distinct_wears),
        wear_spread,
        filler_fraction,
        1.0 if stattrak else 0.0,
        float(cost),
        float(collection_mean_roi) if collection_mean_roi is not None else float("nan"),
    ]


def _featurize_record(r, collection_mean_roi: float) -> list:
    return _feature_vector(from_rarity=r.from_rarity, input_mix=r.input_mix,
                            primary_collection=r.primary_collection, stattrak=r.stattrak,
                            cost=r.cost, collection_mean_roi=collection_mean_roi)


def _leave_one_out_collection_means(rows: list) -> list:
    by_idx: dict = {}
    for i, r in enumerate(rows):
        by_idx.setdefault(r.primary_collection, []).append(i)
    out = [float("nan")] * len(rows)
    for idxs in by_idx.values():
        rois = [rows[i].expected_roi_pct for i in idxs]
        total = sum(rois)
        n = len(rois)
        for i in idxs:
            out[i] = (total - rows[i].expected_roi_pct) / (n - 1) if n > 1 else float("nan")
    return out


def _not_excluded(r) -> bool:
    """Extends the existing low_volume_warning filter with market_reliability.py's unified
    Excluded state (see that module's docstring) -- a row can clear the plain volume-based
    dead_skin check yet still rest on a skin currently Excluded for a DIFFERENT reason (a
    verified mispriced gap, an extreme rolling price-deviation z-score), which shouldn't teach
    this son's search heuristic anything either. Also excludes a row whose exact composition was
    itself already Stage-2-verified unprofitable (verified_low_roi.py) -- that row's
    expected_roi_pct label is now known to be an inflated/bugged estimate, so training on it
    would just re-teach the model the wrong answer for this composition."""
    if verified_low_roi_mod.is_marked(
            sim_blacklist_mod.signature(r.from_rarity, r.to_rarity, r.stattrak, r.input_mix)):
        return False
    return not market_reliability_mod.any_unit_excluded(
        r.input_mix, r.output_distribution, currency=1, stattrak=r.stattrak)


def forget(model_dir: Path, sims_file: Path) -> None:
    """Drops any in-process caches for this son's model/data. Call after a hard "full reset"
    (the caller is responsible for actually deleting the underlying files) so a stale cached
    model or collection-ROI lookup from before the reset can never be served again."""
    with _lock:
        _loaded_models.pop(model_dir, None)
        _collection_roi_caches.pop(sims_file, None)


def _collection_roi_lookup(sims_file: Path) -> dict:
    if not sims_file.exists():
        return {}
    mtime = sims_file.stat().st_mtime
    cached = _collection_roi_caches.get(sims_file)
    if cached and cached["mtime"] == mtime:
        return cached["lookup"]
    rows = [r for r in simulate_mod.load_simulations(sims_file)
            if not r.low_volume_warning and _not_excluded(r)]
    by_collection: dict = {}
    for r in rows:
        by_collection.setdefault(r.primary_collection, []).append(r.expected_roi_pct)
    lookup = {c: sum(v) / len(v) for c, v in by_collection.items()}
    _collection_roi_caches[sims_file] = {"mtime": mtime, "lookup": lookup}
    return lookup


def train(model_dir: Path, sims_file: Path) -> TrainResult:
    """Fits a fresh model on every currently-logged simulation (for ONE son -- model_dir/
    sims_file are that son's own paths) and overwrites its persisted model + metadata. Refuses
    (not a crash) below MIN_TRAINING_SAMPLES. Cross-validated MAE is measured before the final
    fit-on-everything, mirroring learning.py.train().

    Excludes any row flagged low_volume_warning (a counted input/output price rests on Steam's
    own recent-sales volume below price_report.LOW_VOLUME_THRESHOLD -- an unreliable, thinly-
    traded price that shouldn't be allowed to teach the model a pattern that isn't real) --
    those rows are still preserved in sims_file, just never trained on."""
    rows = [r for r in simulate_mod.load_simulations(sims_file)
            if not r.low_volume_warning and _not_excluded(r)]
    if len(rows) < MIN_TRAINING_SAMPLES:
        return TrainResult(trained=False,
                            reason=f"Only {len(rows)} volume-confirmed, reliability-cleared simulated "
                                    f"trials logged, need {MIN_TRAINING_SAMPLES}+ (low-volume/thinly-traded/"
                                    f"market-reliability-excluded rows don't count)",
                            n_samples=len(rows), validation_mae=None, feature_importances=None)

    import joblib
    import numpy as np

    loo_means = _leave_one_out_collection_means(rows)
    X = np.array([_featurize_record(r, loo_means[i]) for i, r in enumerate(rows)], dtype=float)
    y = np.array([r.expected_roi_pct for r in rows], dtype=float)

    # Reliability-weighted training (see market_reliability.training_weight): rows verified
    # across marketplaces teach at full strength, Steam-only rows at 0.35 -- reduced influence
    # instead of deletion. (Rows on currently-Excluded skins were already filtered above; their
    # weight would be 0 anyway -- the filter and the weight ladder agree by construction.)
    weights = []
    weight_reasons: dict = {}
    for r in rows:
        weight, reason = market_reliability_mod.training_weight(
            r.input_mix, r.output_distribution, currency=1, stattrak=r.stattrak)
        weights.append(weight)
        weight_reasons[reason] = weight_reasons.get(reason, 0) + 1
    sample_weight = np.array(weights, dtype=float)

    # Same cross-validated model selection as learning.py -- see ml_common.py's module docstring.
    selection = ml_common.fit_best_model(X, y, FEATURE_NAMES, random_state=42,
                                          sample_weight=sample_weight)
    model, mae, importances = selection.model, selection.cv_mae, selection.feature_importances

    with _lock:
        model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, model_dir / MODEL_FILENAME)
        meta = {"trained_at": time.time(), "n_samples": len(rows), "validation_mae": mae,
                "feature_importances": importances, "feature_names": FEATURE_NAMES,
                "model_name": selection.model_name, "candidate_maes": selection.candidate_maes,
                "sample_weight_breakdown": weight_reasons,
                "mean_sample_weight": round(float(sample_weight.mean()), 3)}
        (model_dir / META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")
        _loaded_models.pop(model_dir, None)  # force a reload on the next predict_expected_roi() call

    return TrainResult(trained=True, reason="", n_samples=len(rows), validation_mae=mae,
                        feature_importances=importances, model_name=selection.model_name,
                        candidate_maes=selection.candidate_maes)


def get_model_status(model_dir: Path) -> dict:
    meta_file = model_dir / META_FILENAME
    if not meta_file.exists():
        return {"trained": False, "n_samples": 0, "validation_mae": None, "trained_at": None,
                "min_samples_required": MIN_TRAINING_SAMPLES}
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"trained": False, "n_samples": 0, "validation_mae": None, "trained_at": None,
                "min_samples_required": MIN_TRAINING_SAMPLES}
    return {"trained": True, "min_samples_required": MIN_TRAINING_SAMPLES, **meta}


def predict_expected_roi(candidate, model_dir: Path, sims_file: Path) -> float | None:
    """Scores a not-yet-simulated random_trade.Candidate for random_trade.generate_batch's
    guided sampling -- estimated from the candidate's structure alone (no pricing needed), so
    it's cheap enough to call on every random draw before deciding which ones are even worth
    the cost of a full simulate.simulate() call (which can now block on a real live price fetch
    for any skin/wear not already cached). Returns None
    if no model is trained yet -- callers must treat that as "no signal", never fabricate a
    number (same contract as learning.predict_roi). model_dir/sims_file identify WHICH son's
    model/data to use -- see the module docstring for why these aren't fixed constants."""
    status = get_model_status(model_dir)
    model_file = model_dir / MODEL_FILENAME
    if not status["trained"] or not model_file.exists():
        return None
    with _lock:
        mtime = model_file.stat().st_mtime
        cached = _loaded_models.get(model_dir)
        if cached is None or cached[1] != mtime:
            import joblib
            try:
                model = joblib.load(model_file)
                _loaded_models[model_dir] = (model, mtime)
            except (FileNotFoundError, EOFError):
                return None  # model file mid-write (a concurrent train() call) -- try again next call
        else:
            model = cached[0]

    import numpy as np
    input_mix = [{"collection": leg.collection, "wear": u["wear"], "count": u["count"]}
                 for leg in candidate.legs for u in leg.units]
    collection_mean = _collection_roi_lookup(sims_file).get(candidate.legs[0].collection, float("nan"))
    # cost is unknown pre-simulation -- fed as NaN, which HistGradientBoostingRegressor
    # handles natively (it has real cost values at training time, unlike the all-NaN
    # thirdparty_gap_pct case learning.py's FEATURE_NAMES comment documents avoiding).
    features = _feature_vector(from_rarity=candidate.from_rarity_name, input_mix=input_mix,
                                primary_collection=candidate.legs[0].collection, stattrak=candidate.stattrak,
                                cost=float("nan"), collection_mean_roi=collection_mean)
    return float(model.predict(np.array([features], dtype=float))[0])
