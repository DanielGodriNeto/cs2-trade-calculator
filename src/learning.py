"""Optional, lightweight ML calibration layer on top of the formula-based ROI estimate.

Trains on logged outcomes (src/outcomes.py -- real trades the user actually executed) to
predict realized ROI% from a trade's features. Never replaces the existing formula-based
roi_pct anywhere in the codebase: this only adds an ml_predicted_roi_pct + confidence
alongside it, consumed as a tiebreaker by float_mix.py/mixed_float.py/tradeup.py. Entirely
dormant until MIN_TRAINING_SAMPLES logged outcomes exist -- every scan result works exactly
as it does today for a user who never logs an outcome.

IMPORTANT: this module must never be fed the tool's own formula-based estimates or the
guided-simulation population's (sim_loop.py) exploratory findings as if they were real
outcomes -- only
genuine, user-logged Outcome records (src/outcomes.py, actual money spent/received) are
valid training data. Mixing the tool's own guesses back in as "ground truth" would let the
model learn to agree with itself rather than with reality.

sklearn/joblib/numpy imports happen lazily inside train()/predict_roi(), not at module load,
so `import src.learning` itself requires no third-party dependencies until one of those is
actually called -- every other module in this codebase stays stdlib-only.
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
from . import outcomes as outcomes_mod

MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "model"
MODEL_FILE = MODEL_DIR / "roi_model.joblib"
META_FILE = MODEL_DIR / "roi_model_meta.json"

MIN_TRAINING_SAMPLES = 20  # below this, train() refuses and predict_roi() always returns None

FEATURE_NAMES = ["rarity_from_rank", "n_distinct_wears", "wear_spread", "has_filler",
                  "filler_fraction", "stattrak", "cost_paid", "predicted_roi_pct",
                  "collection_mean_historical_roi"]
# thirdparty_gap_pct deliberately excluded: the current Outcome schema never captures a
# thirdparty_check snapshot at logging time (see _featurize_outcome), so that column would be
# 100% NaN across every training row -- HistGradientBoostingRegressor's binning step raises
# ValueError on an entirely-missing column ("window shape cannot be larger than input array
# shape"), not just a low-signal warning. predict_roi() still accepts a thirdparty_gap_pct
# argument (callers already pass it) so re-adding this feature later, once outcomes.py captures
# it, doesn't require touching every call site -- it's just not fed into the model yet.

RARITY_RANK_BY_NAME = {name: idx for idx, (_, name) in enumerate(data_mod.RARITY_CHAIN)}

_lock = threading.Lock()  # guards model file load/dump within this process (train() vs. predict_roi())
_loaded_model = None
_loaded_model_mtime = None
_collection_roi_cache = {"mtime": None, "lookup": {}}


@dataclass
class TrainResult:
    trained: bool
    reason: str  # "" on success, else why training was refused/skipped
    n_samples: int
    validation_mae: float | None
    feature_importances: dict | None
    model_name: str | None = None       # which candidate algorithm won, see ml_common.py
    candidate_maes: dict | None = None  # every candidate's own CV MAE, for transparency


def _not_excluded(o: outcomes_mod.Outcome) -> bool:
    """market_reliability.py's unified Excluded-state gate, adapted to a real Outcome's shape
    (a single realized output_skin/output_wear, not a probability distribution) -- extends
    train()'s existing sold-outcome filter so a real trade resting on a currently Excluded
    skin (a verified mispriced gap, an extreme rolling price-deviation z-score) can't teach
    this model a number that isn't trustworthy right now."""
    units = list(o.input_mix or [])
    if o.filler_input:
        units = units + [o.filler_input]
    units = units + [{"skin": o.output_skin, "wear": o.output_wear}]
    return not market_reliability_mod.any_unit_excluded(units, [], currency=o.currency, stattrak=o.stattrak)


def _rarity_rank(from_rarity: str | None) -> float:
    if from_rarity is None:
        return float("nan")
    return float(RARITY_RANK_BY_NAME.get(from_rarity, float("nan")))


def _feature_vector(*, from_rarity, input_mix, filler_input, stattrak, cost_paid,
                     predicted_roi_pct, thirdparty_gap_pct, collection_mean_roi) -> list[float]:
    # thirdparty_gap_pct is accepted but NOT included below -- see the FEATURE_NAMES comment
    # for why (it's always NaN in training data today, which crashes the model's binning step).
    wears = [item["wear"] for item in input_mix]
    distinct = set(wears)
    wear_indices = [oddsmath.WEAR_ORDER.index(w) for w in distinct if w in oddsmath.WEAR_ORDER]
    wear_spread = (max(wear_indices) - min(wear_indices)) if wear_indices else 0
    total_count = sum(item["count"] for item in input_mix) + (filler_input["count"] if filler_input else 0)
    filler_fraction = (filler_input["count"] / total_count) if filler_input and total_count else 0.0
    return [
        _rarity_rank(from_rarity),
        float(len(distinct)),
        float(wear_spread),
        1.0 if filler_input else 0.0,
        filler_fraction,
        1.0 if stattrak else 0.0,
        float(cost_paid),
        float(predicted_roi_pct) if predicted_roi_pct is not None else float("nan"),
        float(collection_mean_roi) if collection_mean_roi is not None else float("nan"),
    ]


def _featurize_outcome(o: outcomes_mod.Outcome, collection_mean_roi: float) -> list[float]:
    # thirdparty_gap_pct is always NaN here -- the current Outcome schema doesn't capture a
    # thirdparty_check snapshot at logging time, only at live-scan/predict time. Documented
    # gap, not a bug: the model simply never sees training signal for that one feature yet.
    return _feature_vector(from_rarity=o.from_rarity, input_mix=o.input_mix, filler_input=o.filler_input,
                            stattrak=o.stattrak, cost_paid=o.cost_paid, predicted_roi_pct=o.predicted_roi_pct,
                            thirdparty_gap_pct=None, collection_mean_roi=collection_mean_roi)


def _leave_one_out_collection_means(sold: list) -> list[float]:
    """Parallel list to `sold`: for each outcome, the mean roi_pct of every OTHER sold outcome
    in the same collection (NaN if it's the only sample for that collection) -- captures "this
    collection runs hot/cold" without one-hot-encoding dozens of collections into more columns
    than rows, and without letting an outcome's own label leak into its own feature."""
    by_collection_indices: dict = {}
    for i, o in enumerate(sold):
        by_collection_indices.setdefault(o.collection, []).append(i)
    out = [float("nan")] * len(sold)
    for idxs in by_collection_indices.values():
        rois = [sold[i].roi_pct for i in idxs]
        total = sum(rois)
        n = len(rois)
        for i in idxs:
            out[i] = (total - sold[i].roi_pct) / (n - 1) if n > 1 else float("nan")
    return out


def _collection_roi_lookup() -> dict:
    """{collection: mean roi_pct across all sold outcomes} for predict-time use (unlike
    training, prediction isn't leaving anything out since the candidate isn't in the log).
    Cached against outcomes.jsonl's mtime so repeated predict_roi() calls during one scan
    (float_mix.py's tiebreaker can call this many times) don't re-read/re-parse the file
    every time."""
    global _collection_roi_cache
    if not outcomes_mod.OUTCOMES_FILE.exists():
        return {}
    mtime = outcomes_mod.OUTCOMES_FILE.stat().st_mtime
    if _collection_roi_cache["mtime"] == mtime:
        return _collection_roi_cache["lookup"]
    sold = [o for o in outcomes_mod.load_outcomes() if o.status == "sold" and _not_excluded(o)]
    by_collection: dict = {}
    for o in sold:
        by_collection.setdefault(o.collection, []).append(o.roi_pct)
    lookup = {c: sum(v) / len(v) for c, v in by_collection.items()}
    _collection_roi_cache = {"mtime": mtime, "lookup": lookup}
    return lookup


def _sklearn_version() -> str:
    import sklearn
    return sklearn.__version__


def train() -> TrainResult:
    """Fits a fresh model on every currently-logged sold outcome and overwrites the persisted
    model + metadata. Refuses (not a crash) below MIN_TRAINING_SAMPLES. Cross-validated MAE is
    measured BEFORE the final fit-on-everything, so it's an honest held-out estimate, not
    training error."""
    sold = [o for o in outcomes_mod.load_outcomes() if o.status == "sold" and _not_excluded(o)]
    if len(sold) < MIN_TRAINING_SAMPLES:
        return TrainResult(trained=False,
                            reason=f"Only {len(sold)} sold, reliability-cleared outcomes logged, "
                                    f"need {MIN_TRAINING_SAMPLES}+",
                            n_samples=len(sold), validation_mae=None, feature_importances=None)

    import joblib
    import numpy as np

    loo_means = _leave_one_out_collection_means(sold)
    X = np.array([_featurize_outcome(o, loo_means[i]) for i, o in enumerate(sold)], dtype=float)
    y = np.array([o.roi_pct for o in sold], dtype=float)

    # Reliability-weighted training, same ladder as sim_learning.train() (see
    # market_reliability.training_weight): a real outcome whose skins verify cleanly across
    # marketplaces teaches at full strength; one only Steam can price teaches at 0.35 --
    # reduced influence instead of deletion, preserving the historical record either way.
    weights = []
    for o in sold:
        units = list(o.input_mix or []) + ([o.filler_input] if o.filler_input else [])
        units = units + [{"skin": o.output_skin, "wear": o.output_wear}]
        weight, _reason = market_reliability_mod.training_weight(units, [], currency=o.currency,
                                                                  stattrak=o.stattrak)
        weights.append(weight)
    sample_weight = np.array(weights, dtype=float)

    # Tries several candidate algorithms via cross-validation and keeps whichever generalizes
    # best on this dataset, rather than assuming one fixed algorithm is always right -- see
    # ml_common.py's module docstring for the reasoning.
    selection = ml_common.fit_best_model(X, y, FEATURE_NAMES, random_state=42,
                                          sample_weight=sample_weight)
    model, mae, importances = selection.model, selection.cv_mae, selection.feature_importances

    # Real prediction-interval bounds from the winner's own out-of-fold residuals (honest held
    # -out errors), NOT a per-prediction estimate -- replaces the old n-samples-only "high/medium
    # /low" confidence label, which was identical for every prediction regardless of how well
    # that specific candidate was actually supported. np.percentile on a small residual array is
    # still meaningful (it's the same array CV MAE is computed from) and degrades gracefully
    # (wide/uninformative interval) exactly when the sample is too small to trust, which is more
    # honest than a fixed n-based tier.
    residuals = np.array(selection.oof_residuals, dtype=float)
    residual_p10 = float(np.percentile(residuals, 10))
    residual_p90 = float(np.percentile(residuals, 90))

    global _loaded_model, _loaded_model_mtime
    with _lock:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, MODEL_FILE)
        meta = {"trained_at": time.time(), "n_samples": len(sold), "validation_mae": mae,
                "feature_importances": importances, "feature_names": FEATURE_NAMES,
                "sklearn_version": _sklearn_version(), "model_name": selection.model_name,
                "candidate_maes": selection.candidate_maes,
                "residual_p10": residual_p10, "residual_p90": residual_p90}
        META_FILE.write_text(json.dumps(meta), encoding="utf-8")
        _loaded_model = None  # force a reload on the next predict_roi() call
        _loaded_model_mtime = None

    return TrainResult(trained=True, reason="", n_samples=len(sold), validation_mae=mae,
                        feature_importances=importances, model_name=selection.model_name,
                        candidate_maes=selection.candidate_maes)


def reset_model() -> None:
    """Deletes the persisted model + metadata so get_model_status() reports untrained again.
    Only touches data/model/ -- never outcomes.jsonl (the real logged outcomes stay put, so a
    reset model can simply be retrained from them via train())."""
    global _loaded_model, _loaded_model_mtime
    with _lock:
        MODEL_FILE.unlink(missing_ok=True)
        META_FILE.unlink(missing_ok=True)
        _loaded_model = None
        _loaded_model_mtime = None


def get_model_status() -> dict:
    """Cheap, no-sklearn-import-needed status check -- reads only the small JSON sidecar, not
    the model itself. Used everywhere as the "is it even worth trying?" gate before predict_roi()."""
    if not META_FILE.exists():
        return {"trained": False, "n_samples": 0, "validation_mae": None, "trained_at": None,
                "min_samples_required": MIN_TRAINING_SAMPLES}
    try:
        meta = json.loads(META_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"trained": False, "n_samples": 0, "validation_mae": None, "trained_at": None,
                "min_samples_required": MIN_TRAINING_SAMPLES}
    return {"trained": True, "min_samples_required": MIN_TRAINING_SAMPLES, **meta}


def predict_roi(*, collection: str, from_rarity: str | None, input_mix: list, stattrak: bool,
                 cost_paid: float, predicted_roi_pct: float | None,
                 thirdparty_gap_pct: float | None = None, filler_input: dict | None = None) -> dict | None:
    """Returns {"ml_predicted_roi_pct": float, "confidence": "low"|"medium"|"high",
    "predicted_roi_range": [lo, hi] | None} or None if the model isn't trained yet -- callers
    must treat a None return as "no ML signal available", never fabricate a number.

    confidence is still a simple n_samples-based heuristic (kept for backward-compat display),
    but predicted_roi_range is a REAL uncertainty signal: an 80% interval built from the trained
    model's own out-of-fold residuals at training time (see ml_common.fit_best_model's
    oof_residuals). It's a single global interval width, not a per-candidate heteroscedastic
    estimate -- still an honest improvement over confidence's identical label for every
    prediction, since it reflects the model's actual held-out error spread (including outlier
    skew) rather than just how many rows it saw. None if the loaded meta predates this field."""
    global _loaded_model, _loaded_model_mtime
    status = get_model_status()
    if not status["trained"] or not MODEL_FILE.exists():
        return None

    with _lock:
        mtime = MODEL_FILE.stat().st_mtime
        if _loaded_model is None or _loaded_model_mtime != mtime:
            import joblib
            try:
                _loaded_model = joblib.load(MODEL_FILE)
                _loaded_model_mtime = mtime
            except (FileNotFoundError, EOFError):
                return None  # model file mid-write (a concurrent train() call) -- try again next call
        model = _loaded_model

    import numpy as np
    collection_mean = _collection_roi_lookup().get(collection, float("nan"))
    features = _feature_vector(from_rarity=from_rarity, input_mix=input_mix, filler_input=filler_input,
                                stattrak=stattrak, cost_paid=cost_paid, predicted_roi_pct=predicted_roi_pct,
                                thirdparty_gap_pct=thirdparty_gap_pct, collection_mean_roi=collection_mean)
    pred = float(model.predict(np.array([features], dtype=float))[0])
    n = status["n_samples"]
    confidence = "high" if n >= 200 else "medium" if n >= 50 else "low"
    roi_range = None
    if status.get("residual_p10") is not None and status.get("residual_p90") is not None:
        roi_range = [pred + status["residual_p10"], pred + status["residual_p90"]]
    return {"ml_predicted_roi_pct": pred, "confidence": confidence, "predicted_roi_range": roi_range}
