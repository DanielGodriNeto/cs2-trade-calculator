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
import contextlib
import time
from dataclasses import dataclass, field


def _thread_limit(max_threads: int | None):
    """Caps the native (OpenMP/BLAS) thread pools sklearn's fitting code spins up, for the
    duration of one fit_best_model() call.

    Why this exists: HistGradientBoostingRegressor parallelizes its split-finding over OpenMP
    threads and, left alone, grabs ONE THREAD PER CORE (16 here). That is a bad trade twice
    over on this project's data. First, at these sizes the parallelism doesn't even pay for
    itself -- benchmarked on a 1500x8 set, a single-threaded fit is FASTER than the 16-thread
    one (0.042s vs 0.071s per fit), because the per-iteration thread handshake costs more than
    the work it distributes. Second and much worse, sim_loop.py runs all 6 population sons as
    real concurrent threads in ONE process (see web/server.py's startup), each free to call
    train() whenever its own round trips RETRAIN_TRIGGER_DELTA -- so the uncapped version can
    put 6 x 16 = 96 native threads on 16 cores, on top of the 6 sim loops, the price scan, the
    pattern scanner and the verify queue all wanting the same cores. That oversubscription, not
    the model or the row count, is what turns a ~4s retrain into a ~25s one.

    None (default) keeps the historical uncapped behavior for learning.py's real-outcomes
    model, which is small, retrains rarely, and never dogpiles with 5 siblings.

    threadpoolctl ships as a scikit-learn dependency, but the import is guarded anyway and
    degrades to "no limit" rather than failing a retrain -- same defensive posture as this
    project's other optional-dependency paths."""
    if max_threads is None:
        return contextlib.nullcontext()
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:
        return contextlib.nullcontext()
    return threadpool_limits(limits=max_threads)


def prewarm() -> float:
    """Pays this process's one-time sklearn startup cost up front, on a background thread at
    server boot, instead of billing it to whichever son happens to retrain first. Returns the
    seconds it took (callers may log it; nothing depends on the value).

    There are TWO one-time costs, and only the first was ever visible. Importing sklearn is
    ~1.6s and fit_best_model already times it as its own "sklearn_import" phase. The second is
    hidden INSIDE the first HistGradientBoostingRegressor.fit() of a process and costs a further
    ~1.6s -- lazily-loaded compiled extensions and their first-touch page-ins -- which lands in
    the "cv:hist_gradient_boosting" phase and is indistinguishable there from real fitting work.
    Measured, same 2181-row input throughout:
        cold     import 1.609 | fit#0 1.594 | fit#1 0.073 | fit#2 0.072
        prewarm  import 1.595 | throwaway 32-row fit 1.553 | fit#0 0.074 | fit#1 0.068
    A 32-row throwaway fit absorbs essentially all of it, and every subsequent fit -- at any row
    count -- is ~21x faster than the first one was.

    This was not theoretical: son_0's persisted metadata recorded sklearn_import 3.413s and
    cv:hist_gradient_boosting 2.827s on just 637 rows, against son_1's 0.712s on 1182 rows in
    the same process. son_0 was simply the first to retrain after a restart, and its dashboard
    read 9.4s for it. The cost recurs on every server restart, so it belongs at startup.

    Deliberately fits BOTH candidate families sim_learning.py uses (the boosted tree and the
    Ridge pipeline) on throwaway random data -- 32 rows, 8 features, matching FEATURE_NAMES'
    width. Nothing is persisted, cached or returned; this touches no project data and no model
    file. Every failure mode is swallowed: a prewarm that breaks must never take the server down
    with it, since the only consequence of skipping it is the historical behavior."""
    t0 = time.perf_counter()
    try:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
        from sklearn.inspection import permutation_importance  # noqa: F401 -- import cost only
        from sklearn.model_selection import RepeatedKFold      # noqa: F401 -- import cost only

        rng = np.random.default_rng(0)
        X = rng.random((32, 8))
        y = rng.random(32)
        with _thread_limit(1):
            HistGradientBoostingRegressor(loss="squared_error", max_depth=4, max_iter=5,
                                           min_samples_leaf=3, random_state=0).fit(X, y).predict(X)
            make_pipeline(SimpleImputer(strategy="mean"), StandardScaler(),
                           Ridge(alpha=1.0)).fit(X, y).predict(X)
    except Exception:
        pass
    return round(time.perf_counter() - t0, 3)


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
    # Wall-clock seconds per fitting sub-phase: one "cv:<candidate>" key per candidate's full
    # cross-validation cycle, plus "final_refit" and "permutation_importance". Purely
    # observational (nothing reads it to make a decision) and defaulted, so it can't change this
    # dataclass's construction/unpacking shape for any existing caller -- see this project's
    # documented return-shape-drift bug history. Callers surface it for per-agent training-time
    # display; see sim_learning.TrainResult.phase_seconds.
    timings: dict = field(default_factory=dict)


def _candidates(random_state: int, n_samples: int, include: set | None = None,
                 hgb_loss: str = "absolute_error", hgb_max_bins: int | None = None) -> dict:
    """Small, fixed set of already-regularized candidates -- max_depth/min_samples_leaf/
    n_estimators are chosen to behave reasonably at the small sample sizes this project
    realistically trains on (tens to low hundreds of rows), not tuned per-dataset.

    include: optional set of candidate names to keep (see fit_best_model's own docstring) --
    None (default) returns all three, unchanged from this module's original behavior.

    hgb_loss/hgb_max_bins: speed knobs for the gradient-boosted candidate, both defaulting to
    this module's original behavior so learning.py's real-outcomes model is untouched. Only
    sim_learning.py opts into different values -- see its FAST_* block for the measurements
    that justify them, and the loss= comment below for why this is a per-caller choice rather
    than a new global default."""
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler

    # HistGradientBoostingRegressor handles NaN features natively (this project relies on that
    # for columns like predicted_roi_pct/collection_mean_roi that are sometimes unknown) -- fed
    # the raw X directly, no imputation. min_samples_leaf scales down for small n so it doesn't
    # default to a leaf size that's a large fraction of the whole training set.
    # loss: defaults to "absolute_error" (not the sklearn default squared_error) so the tree's
    # own split/leaf objective matches the MAE metric this module selects and reports on --
    # trade-up ROI is heavy-tailed (rare huge wins), and the reasoning was that a squared-error
    # objective would let a couple of outlier rows dominate the fit while the MAE shown to the
    # user hides that sensitivity.
    #
    # That reasoning was tested rather than assumed, and it does NOT hold on the sim population's
    # data: benchmarked across all 6 sons' real training sets x 3 CV shuffles, squared_error was
    # ~2x faster per fit AND produced a LOWER held-out MAE on 5 of the 6 (son_0 -7.9%, son_2
    # -23.2%, son_3 -6.9%, son_4 -2.9%, son_5 -14.3%; only son_1 regressed, +17.3%). The
    # heavy-tail argument is real but is evidently outweighed here by absolute_error's much
    # noisier gradients at these sample sizes. sim_learning.py therefore passes squared_error;
    # the default stays absolute_error because learning.py's real-outcomes model is a different,
    # far smaller dataset where that benchmark says nothing -- and it was never the slow one.
    #
    # max_bins: None keeps sklearn's default (255). A lower value coarsens the feature binning
    # the histogram split-finder works on -- the single biggest speed lever for this model on
    # larger row counts. Again opt-in per caller; see sim_learning.FAST_MAX_BINS.
    hgb_kwargs = {}
    if hgb_max_bins is not None:
        hgb_kwargs["max_bins"] = hgb_max_bins
    hgb = HistGradientBoostingRegressor(
        loss=hgb_loss, random_state=random_state, max_depth=4,
        min_samples_leaf=max(3, min(20, n_samples // 8)), **hgb_kwargs)

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
                    importance_n_repeats: int = 5,
                    hgb_loss: str = "absolute_error",
                    hgb_max_bins: int | None = None,
                    max_threads: int | None = None) -> ModelSelectionResult:
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

    candidates/n_folds/importance_n_repeats/hgb_loss/hgb_max_bins/max_threads: all optional, all
    default to this module's original fixed behavior (all 3 candidates, 5 folds, 5 importance
    repeats, absolute_error loss, sklearn's default binning, uncapped native threads) -- used by
    learning.py's small real-outcomes model unchanged. sim_learning.py's much larger, much-more-often-retrained sim-population
    model passes its own faster FAST_* profile (see that module) instead -- see this function's
    callers, not this module, for why.

    The returned ModelSelectionResult.timings carries wall-clock seconds per sub-phase, so a
    caller can report where a slow retrain actually went instead of guessing."""
    # Timed as its own phase: sklearn is imported lazily (see this project's convention), so the
    # FIRST train() call in a process pays several seconds of one-time module import here while
    # every later one pays ~0. Without its own entry that cost would sit inside fit_best_model
    # yet be absent from the cv:*/final_refit/importance children, making the first agent of a
    # run look mysteriously slow with no phase to blame.
    _t_import = time.perf_counter()
    import numpy as np
    from sklearn.model_selection import RepeatedKFold
    from sklearn.metrics import mean_absolute_error
    from sklearn.inspection import permutation_importance
    from sklearn.base import clone
    _import_seconds = round(time.perf_counter() - _t_import, 3)

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

    candidate_estimators = _candidates(random_state, n, include=candidates,
                                        hgb_loss=hgb_loss, hgb_max_bins=hgb_max_bins)
    candidate_maes = {}
    candidate_oof_preds = {}  # name -> (n_repeats, n) array, averaged into one oof estimate below
    timings: dict = {"sklearn_import": _import_seconds}
    # Materialized once: cv.split() is a generator, and re-splitting per candidate would also
    # mean every candidate is scored on a freshly-derived (identical, but recomputed) set of
    # folds. Reusing one list keeps the "same split for every candidate" guarantee this module's
    # docstring promises literal rather than incidental.
    splits = list(cv.split(X))
    # Everything below is the actual number-crunching, so it's the whole of what the native
    # thread cap needs to cover -- see _thread_limit() for why capping is a speed WIN here and
    # not a sacrifice. Purely a scheduling change: identical arithmetic, identical results,
    # and random_state pins every candidate, so the chosen model and its MAE are unaffected.
    with _thread_limit(max_threads):
        for name, estimator in candidate_estimators.items():
            t0 = time.perf_counter()
            sums = np.zeros(n, dtype=float)
            counts = np.zeros(n, dtype=float)
            for train_idx, test_idx in splits:
                fold = clone(estimator)
                fold_w = w[train_idx] if w is not None else None
                fold.fit(X[train_idx], y[train_idx], **_fit_kwargs(fold, fold_w))
                sums[test_idx] += fold.predict(X[test_idx])
                counts[test_idx] += 1.0
            preds = sums / counts  # every row appears in exactly one test fold per repeat, so counts == n_repeats
            candidate_oof_preds[name] = preds
            candidate_maes[name] = float(mean_absolute_error(y, preds, sample_weight=w))
            timings[f"cv:{name}"] = round(time.perf_counter() - t0, 3)

        best_name = min(candidate_maes, key=candidate_maes.get)
        best_model = candidate_estimators[best_name]
        oof_residuals = (y - candidate_oof_preds[best_name]).tolist()
        # Final refit on ALL data (with weights), after CV already measured honest error above.
        t0 = time.perf_counter()
        best_model.fit(X, y, **_fit_kwargs(best_model, w))
        timings["final_refit"] = round(time.perf_counter() - t0, 3)

        # importance_n_repeats (default 5, was 10): permutation importance is a display/diagnostic
        # number, not part of the model itself -- fewer repeats costs less for a noisier but still
        # directionally-correct importance ranking.
        #
        # 0 skips it entirely and returns {}. Worth having as an option because this phase costs
        # more than the model does: profiled on son_2's real data it was 0.37s of a 0.94s retrain,
        # the single largest phase, for a number that never touches a prediction -- it refits
        # nothing, changes no weights, and only feeds the dashboard's importance bars. A caller
        # that skips it must decide what to display instead (sim_learning.py carries the previous
        # retrain's values forward and flags them stale); returning {} rather than a fabricated
        # ranking keeps that decision at the caller, where the context is.
        if importance_n_repeats:
            t0 = time.perf_counter()
            importance_result = permutation_importance(best_model, X, y, n_repeats=importance_n_repeats,
                                                        random_state=random_state, sample_weight=w)
            importances = dict(zip(feature_names, [float(v) for v in importance_result.importances_mean]))
            timings["permutation_importance"] = round(time.perf_counter() - t0, 3)
        else:
            importances = {}

    return ModelSelectionResult(model=best_model, model_name=best_name, cv_mae=candidate_maes[best_name],
                                 candidate_maes=candidate_maes, feature_importances=importances,
                                 oof_residuals=oof_residuals, timings=timings)
