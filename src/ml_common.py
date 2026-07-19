"""Shared model-FITTING algorithm code for learning.py and sim_learning.py.

IMPORTANT: this module carries no data of its own and never will -- it only receives whatever
X/y arrays its two callers already built from their own (deliberately separate, never-merged)
sources: learning.py's real logged outcomes.jsonl vs. sim_learning.py's per-son
simulations.jsonl. Sharing this pure algorithm code does NOT violate the project's "never mix
real outcomes with simulated trials" rule -- see learning.py's and sim_learning.py's own module
docstrings for why that rule exists. This module exists only so the two callers' near-identical
CV/fit/permutation-importance code can't independently drift out of sync with each other (a bug
already happened once in this codebase from near-duplicate sibling files, see mixed_tradeup.py/
mixed_covert.py's history).

fit_best_model() tries a small, fixed set of well-regularized candidate regressors -- a gradient
-boosted tree (HistGradientBoostingRegressor, handles missing/NaN features natively), a random
forest, and a linear baseline (Ridge, on imputed+scaled features) -- through the SAME K-fold
cross-validation split, and keeps whichever one actually generalizes best on THIS dataset,
instead of assuming one fixed algorithm is always the right choice. This is standard model-
selection practice (the same idea scikit-learn's own model-selection guide and most Kaggle-style
tabular pipelines use): a single k-fold split scores every candidate identically, so the
comparison isn't skewed by one candidate getting an easier split. Every candidate's own CV MAE is
returned alongside the winner so the dashboard can show WHY a given algorithm was chosen, not
just its final score.

Deliberately NOT a hyperparameter grid search across many settings per candidate -- with the
small sample sizes this project actually sees (tens to low hundreds of logged outcomes / a few
hundred simulated trials before a retrain), searching many configurations against the same CV
split risks overfitting the CV score itself (picking whichever setting got lucky on this
particular fold split, not whichever generalizes best) rather than the model. A small, fixed,
already-regularized candidate per algorithm avoids that trap while still comparing genuinely
different algorithm families.
"""
from dataclasses import dataclass


@dataclass
class ModelSelectionResult:
    model: object                    # the winning estimator, already fit on ALL of X/y
    model_name: str                  # which candidate won, e.g. "hist_gradient_boosting"
    cv_mae: float                    # the winner's own cross-validated MAE (honest, pre-refit)
    candidate_maes: dict             # every candidate's CV MAE, for transparency
    feature_importances: dict        # permutation importance, computed against the winner
    oof_residuals: list              # winner's out-of-fold (y - prediction) values, honest
                                      # held-out errors -- callers use the empirical quantiles of
                                      # this list to build a real prediction interval instead of
                                      # a fake n-samples-based confidence label. Ridge is the one
                                      # remaining squared-error-loss candidate (no robust-loss
                                      # linear option is bundled here); flagged as an accepted
                                      # limitation rather than fixed, since Ridge rarely wins on
                                      # this project's nonlinear trade-up math (see README).


def _candidates(random_state: int, n_samples: int, include: set | None = None) -> dict:
    """Small, fixed set of already-regularized candidates -- max_depth/min_samples_leaf/
    n_estimators are chosen to behave reasonably at the small sample sizes this project
    realistically trains on (tens to low hundreds of rows), not tuned per-dataset.

    include: optional set of candidate names to keep (see fit_best_model's own docstring) --
    None (default) returns all three, unchanged from this module's original behavior."""
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler

    # HistGradientBoostingRegressor handles NaN features natively (this project relies on that
    # for columns like predicted_roi_pct/collection_mean_roi that are sometimes unknown) -- fed
    # the raw X directly, no imputation. min_samples_leaf scales down for small n so it doesn't
    # default to a leaf size that's a large fraction of the whole training set.
    # loss="absolute_error" (not the sklearn default squared_error) so the tree's own split/leaf
    # objective matches the MAE metric this module selects and reports on -- trade-up ROI is
    # heavy-tailed (rare huge wins), so a squared-error objective would let a couple of outlier
    # rows dominate the fit while the MAE shown to the user hides that sensitivity.
    hgb = HistGradientBoostingRegressor(
        loss="absolute_error", random_state=random_state, max_depth=4,
        min_samples_leaf=max(3, min(20, n_samples // 8)))

    # RandomForest/Ridge can't handle NaN -- mean-impute first (Ridge also needs scaling, since
    # its L2 penalty is sensitive to feature scale; RandomForest doesn't strictly need scaling
    # but it's harmless to share the same imputed features for a fair comparison).
    # criterion="squared_error" (not "absolute_error"): sklearn's RF absolute_error criterion
    # sorts values at every split instead of using the cheap running-sum trick squared_error
    # gets, ~2.4x slower per fit on this project's data (benchmarked: 11.6s vs 4.8s for 15
    # CV fits) for a candidate that has never once won the CV comparison against
    # hist_gradient_boosting across any of the 6 sim agents' actual trained models -- the
    # outlier-robustness this bought wasn't paying for itself.
    # n_estimators=120 (was 300): RF is the comparison baseline, never the winner (see above)
    # -- fewer trees costs it a little stability in its own CV MAE, which doesn't matter since
    # it isn't going to win regardless, in exchange for real speed.
    rf = make_pipeline(
        SimpleImputer(strategy="mean"),
        RandomForestRegressor(random_state=random_state, n_estimators=120, max_depth=5,
                               min_samples_leaf=max(2, n_samples // 15),
                               criterion="squared_error"))
    ridge = make_pipeline(SimpleImputer(strategy="mean"), StandardScaler(), Ridge(alpha=1.0))

    all_candidates = {"hist_gradient_boosting": hgb, "random_forest": rf, "ridge": ridge}
    return {k: v for k, v in all_candidates.items() if include is None or k in include}


def _fit_kwargs(estimator, sample_weight) -> dict:
    """Routes a sample_weight array to the estimator's fit() -- pipelines need the final
    step's name prefixed (sklearn's dunder routing), bare estimators take it directly."""
    if sample_weight is None:
        return {}
    from sklearn.pipeline import Pipeline
    if isinstance(estimator, Pipeline):
        return {f"{estimator.steps[-1][0]}__sample_weight": sample_weight}
    return {"sample_weight": sample_weight}


def fit_best_model(X, y, feature_names: list, random_state: int = 42,
                    sample_weight=None, candidates: set | None = None,
                    n_folds: int | None = None,
                    importance_n_repeats: int = 5) -> ModelSelectionResult:
    """X: 2D array (n_samples, n_features), may contain NaN. y: 1D array of targets.
    feature_names: parallel to X's columns, used only to label the returned importances.

    sample_weight (optional, parallel to y): reliability-weighted training -- see
    market_reliability.training_weight. Weights flow through BOTH halves: each CV fold fits
    on the training fold's weights, and the reported MAE is itself weight-averaged (a
    fully-verified row's error counts ~3x a Steam-only row's), so model selection optimizes
    the same weighted objective the final fit uses. The CV loop is written out manually
    (rather than cross_val_predict) because that's the only version-stable way to route
    per-fold fit weights through sklearn's pipeline parameter naming across the >=1.3 range
    this project supports. None behaves exactly like the historical unweighted path.

    candidates/n_folds/importance_n_repeats: all optional, all default to this module's
    original fixed behavior (all 3 candidates, 5 folds, 5 importance repeats) -- used by
    learning.py's small real-outcomes model unchanged. sim_learning.py's much larger,
    much-more-often-retrained sim-population model passes its own faster FAST_* profile (see
    that module) instead -- see this function's callers, not this module, for why."""
    import numpy as np
    from sklearn.model_selection import RepeatedKFold
    from sklearn.metrics import mean_absolute_error
    from sklearn.inspection import permutation_importance
    from sklearn.base import clone

    n = len(y)
    n_folds = min(n_folds or 5, n)
    # RepeatedKFold (1 repeat -- was 3): repeating with different shuffles stabilizes the
    # winner choice and the honest error estimate against one unlucky split, but costs a
    # full extra fit-cycle per repeat. User accepted less CV stability in exchange for speed
    # (benchmarked: this was the single biggest remaining cost after the RF criterion fix,
    # ~3x the fold-fit count for comparatively little payoff once a dataset is a few hundred
    # rows rather than tens -- see the sim population's own history, where the winning
    # candidate has beaten the runner-up by 2-3x MAE on every son, i.e. one shuffle is
    # already decisive). Single KFold split, not literally "no repeats" (n_repeats=1 keeps
    # the exact same code path/shapes as the >1 case, just skips the redundant reshuffles).
    n_repeats = 1
    cv = RepeatedKFold(n_splits=n_folds, n_repeats=n_repeats, random_state=random_state)
    w = np.asarray(sample_weight, dtype=float) if sample_weight is not None else None
    if w is not None and (not np.any(w > 0)):
        w = None  # every row zero-weighted -> degenerate; fall back to unweighted rather than crash

    candidate_estimators = _candidates(random_state, n, include=candidates)
    candidate_maes = {}
    candidate_oof_preds = {}  # name -> (n_repeats, n) array, averaged into one oof estimate below
    for name, estimator in candidate_estimators.items():
        sums = np.zeros(n, dtype=float)
        counts = np.zeros(n, dtype=float)
        for train_idx, test_idx in cv.split(X):
            fold = clone(estimator)
            fold_w = w[train_idx] if w is not None else None
            fold.fit(X[train_idx], y[train_idx], **_fit_kwargs(fold, fold_w))
            sums[test_idx] += fold.predict(X[test_idx])
            counts[test_idx] += 1.0
        preds = sums / counts  # every row appears in exactly one test fold per repeat, so counts == n_repeats
        candidate_oof_preds[name] = preds
        candidate_maes[name] = float(mean_absolute_error(y, preds, sample_weight=w))

    best_name = min(candidate_maes, key=candidate_maes.get)
    best_model = candidate_estimators[best_name]
    oof_residuals = (y - candidate_oof_preds[best_name]).tolist()
    # Final refit on ALL data (with weights), after CV already measured honest error above.
    best_model.fit(X, y, **_fit_kwargs(best_model, w))

    # importance_n_repeats (default 5, was 10): permutation importance is a display/diagnostic
    # number, not part of the model itself -- fewer repeats costs less for a noisier but still
    # directionally-correct importance ranking.
    importance_result = permutation_importance(best_model, X, y, n_repeats=importance_n_repeats,
                                                random_state=random_state, sample_weight=w)
    importances = dict(zip(feature_names, [float(v) for v in importance_result.importances_mean]))

    return ModelSelectionResult(model=best_model, model_name=best_name, cv_mae=candidate_maes[best_name],
                                 candidate_maes=candidate_maes, feature_importances=importances,
                                 oof_residuals=oof_residuals)
