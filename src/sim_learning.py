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
TRAIN_TIME_HISTORY_KEEP = 20  # how many past retrain durations each son's metadata carries
RETRAIN_TRIGGER_DELTA = 25  # simulated rows accumulate far faster than real logged outcomes --
# a higher delta than retrain_loop.py's real-model 5 avoids retraining on every tiny batch

MAX_ROWS_FOR_ANALYSIS = 3000  # caps train()'s CV/fit cost AND _collection_roi_lookup()'s
# market-reliability rescan cost so both stay bounded as a son's simulations.jsonl grows
# without limit -- a son that's run for weeks would otherwise pay an ever-growing per-retrain/
# per-round tax just to keep re-including trials from long ago. Keeps the MOST RECENT rows --
# recent market conditions are more representative than old ones anyway, and 3000 rows is
# already far more than an 8-feature model needs to fit well, so this is a real speed win at
# essentially no accuracy cost.
#
# Both readers get these rows via simulate.load_recent_simulations(), which reads BACKWARDS
# from the end of the file. That matters: this cap used to be applied by slicing the tail off a
# full-file parse, so the rows outside the window were still read and JSON-decoded on every
# retrain -- ~136 MB across the 6 agents per cycle, growing every round, to then throw roughly
# half of it away. The cap now bounds the I/O too, not just the fitting.

RECENCY_HALFLIFE_ROWS = 750  # each training row's sample_weight halves per this many rows of age (newest row = age 0)

# Fast candidate-selection profile for THIS (sim-population) model only -- see ml_common.py's
# fit_best_model(). random_forest is dropped entirely: across every one of the 6 sim agents'
# real trained models, it has never once won against hist_gradient_boosting (see ml_common.py's
# own comment), so cutting it loses zero real accuracy while removing the slowest-per-fit of
# the three candidates. Folds/importance-repeats are trimmed too -- with thousands of rows
# (this dataset, unlike learning.py's small real-outcomes one), 3-fold CV is still a solid
# estimate, and permutation importance is a display-only diagnostic. learning.py's real-outcomes
# model (small sample, retraining was never the reported problem there) deliberately keeps
# ml_common.py's original 3-candidate/5-fold/5-repeat defaults -- this profile is NOT shared.
FAST_CANDIDATES = {"hist_gradient_boosting", "ridge"}
FAST_N_FOLDS = 3
FAST_IMPORTANCE_REPEATS = 3

# Gradient-boosting speed knobs for THIS model only (ml_common.fit_best_model's hgb_loss/
# hgb_max_bins), both benchmarked on all 6 sons' real training sets rather than assumed:
#
# FAST_HGB_LOSS: squared_error instead of ml_common's absolute_error default -- ~2x faster per
# fit and, across 3 CV shuffles per son, a LOWER held-out MAE on 5 of the 6 sons (only son_1
# regressed). See ml_common._candidates' loss comment for the full numbers and for why the
# default there stays absolute_error for learning.py's real-outcomes model.
#
# FAST_MAX_BINS: coarser feature binning for the histogram split-finder (sklearn's default is
# 255). This is the single biggest lever on the larger sons -- son_2 (2184 rows) went 3x faster
# for +0.4% MAE at 64 bins. It stops paying off on the small sons, where the fit is already
# sub-second and the accuracy cost is real (son_0, 638 rows: +12.9% MAE for 1.3x), so it's
# applied only above FAST_MAX_BINS_MIN_ROWS. Below that threshold the model keeps sklearn's
# default binning.
#
# Stays at 64 after an explicit "prefer speed, some accuracy loss is fine" pass re-examined it.
# A sweep across all 6 sons (3 CV shuffles each, same splits per variant) first suggested 32 was
# the best-value accuracy-for-speed knob available:
#     max_bins=32      1.82x faster    +7.7% MAE
#     max_iter=50      1.77x faster   +10.2% MAE
#     max_depth=3      1.51x faster    +9.6% MAE
#     early_stopping   1.37x faster    +8.2% MAE
#     max_iter=30      2.05x faster   +27.2% MAE
# That 1.82x was misleading and is recorded here so it doesn't get "rediscovered". The sweep's
# max_bins=32 variant applied 32 bins to EVERY son, including the small ones whose baseline is
# sklearn's default 255 (they sit below FAST_MAX_BINS_MIN_ROWS) -- so most of the measured
# speedup came from 255 -> 32 on the small sons, not from the 64 -> 32 this constant actually
# controls. Measured directly at production settings, 64 -> 32 buys 0.04s (0.329s -> 0.285s on
# son_2) and costs +16.9% MAE on son_2 and +16.6% on son_5, the two most data-rich sons. Not a
# trade worth making on a retrain that is already sub-second, at any speed preference.
#
# Also measured and deliberately NOT taken: cutting MAX_ROWS_FOR_ANALYSIS 3000 -> 1500. That
# looks like an obvious speed win and is actually the worst trade available -- 1.2x faster for
# a 35% WORSE MAE (4.378 -> 5.922 averaged across the sons), because these models are still
# genuinely data-hungry at a few thousand rows. Left at 3000.
#
# The real remaining win was structural, not a quality trade-off: see
# IMPORTANCE_EVERY_N_RETRAINS below.
FAST_HGB_LOSS = "squared_error"
FAST_MAX_BINS = 64
FAST_MAX_BINS_MIN_ROWS = 1000

# How often the permutation-importance diagnostic is actually recomputed, in retrains. It was
# the largest single phase of a retrain (0.37s of son_2's 0.94s) and it feeds nothing but the
# dashboard's importance bars -- no prediction, no weight, no model selection depends on it.
# Recomputing it every 10th retrain instead of every one makes ~40% of the remaining cost
# disappear at zero cost to the model, which is a strictly better trade than any of the knobs
# that actually degrade accuracy. In between, the previous values are carried forward and
# flagged (see "feature_importances_stale" in the persisted metadata) rather than silently
# presented as fresh. A model that has never computed them yet always computes them, so a
# newly-trained son's dashboard is never blank.
IMPORTANCE_EVERY_N_RETRAINS = 10

# Scheduling, not arithmetic -- the two knobs below change WHEN and on how many cores a retrain
# runs, never what it computes. They exist because the dominant cost of a retrain on this
# machine turned out to be neither the row count nor the model: it was six sons fighting each
# other for the same cores.
#
# The evidence, from the sons' own persisted recent_train_seconds histories: all 6 sons show
# their fast stretch and their slow stretch at the SAME positions in their own history, which a
# per-son data-size effect cannot produce; cv:ridge -- a fit whose real cost is about a
# millisecond -- was logged anywhere from 0.089s to 1.067s depending only on what else was
# running; and re-running son_2's train() on its exact same data with the rest of the app
# stopped took 4.5s against the 31.1s its metadata had recorded live.
#
# FAST_MAX_THREADS = 1: see ml_common._thread_limit(). At this project's row counts a
# single-threaded gradient-boosting fit is genuinely faster than a 16-thread one, so this costs
# nothing even for a son training completely alone.
#
# TRAIN_MAX_CONCURRENCY = 2: sim_loop.py gives every son its own thread and lets each call
# train() independently, so up to 6 retrains can land at once. Benchmarked with 6 sons retraining
# simultaneously on 2200-row data (wall = time for all six to finish; per-son = what a son
# measures for ITSELF, i.e. the number that reaches the dashboard):
#     uncapped, free-for-all (before)  wall 4.56s   per-son 4.39s
#     1 thread, free-for-all           wall 2.06s   per-son 2.01s
#     1 thread + 2 at a time (chosen)  wall 1.95s   per-son 0.62s
# Gating to 2 wins on BOTH axes -- best wall-clock of the three, and a ~7x cut in the per-son
# figure -- because queued sons wait on a semaphore instead of thrashing the scheduler. It also
# leaves the other 14 cores to the sim loops themselves, which are the actual product work.
#
# The semaphore originally wrapped only the fit_best_model call, because that benchmark measured
# the fit. Re-profiling the whole retrain showed that was gating the minority of the work: row
# loading, the reliability filter, featurizing and the sample-weight walk are all real CPU and
# together exceed the fit on every son (son_4: 0.514 + 0.971 vs 1.155). It now wraps the entire
# retrain -- see train().
FAST_MAX_THREADS = 1
TRAIN_MAX_CONCURRENCY = 2
_train_slots = threading.Semaphore(TRAIN_MAX_CONCURRENCY)

FEATURE_NAMES = ["rarity_rank", "n_legs", "n_distinct_wears", "wear_spread", "filler_fraction",
                  "stattrak", "collection_mean_expected_roi"]
# `cost` deliberately excluded -- the same train/serve-skew reasoning as learning.py's
# thirdparty_gap_pct comment, arrived at from the opposite direction. It USED to be in this
# list, and by permutation importance it looked like one of the strongest features (2nd of 8 on
# sons 1/2/4). But a candidate isn't priced until simulate.simulate() runs, so the only caller
# that matters -- predict_expected_roi(), scoring not-yet-simulated candidates -- could only
# ever pass cost=NaN. Training rows, in contrast, always carry a real cost, so the model never
# saw a missing value for this column during fitting and HistGradientBoosting fell back to its
# arbitrary default branch at every cost split the moment it was asked to actually select
# something. The reported CV MAE measured a condition (true cost known) that never occurs at
# selection time, so it was flattering a model that was materially worse at its only job.
#
# Measured 4-fold on all 6 sons' real rows, the three regimes:
#     train with cost, score with the true cost (what the dashboard used to report)   3.726
#     train with cost, score with cost=NaN      (what selection ACTUALLY received)    7.927
#     train without cost                        (train and serve agree -- this)       4.582
# So dropping the feature cuts real selection error 42% (son_2, the most data-rich: 10.04 ->
# 2.45). The dashboard's MAE will read HIGHER than it used to (3.7 -> 4.6) -- that is the point:
# the old number was measuring a fiction. Nothing else about the model changed.
#
# Re-adding it later is a real option, but only alongside a cost ESTIMATE cheap enough to
# compute pre-simulation (warm-cache unit prices would cover most candidates); feeding NaN
# again would just reproduce the skew.

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
    # Wall-clock cost of THIS retrain, for the dashboard's per-agent training-time display.
    # elapsed_seconds is the whole train() call; phase_seconds breaks it down (load_rows /
    # filter_rows / featurize / sample_weights / fit_best_model / persist, plus
    # ml_common's own cv:*/final_refit/permutation_importance sub-phases folded in). Both
    # default to None so a refused (untrained) result and every existing construction/unpack
    # site keep their exact current shape -- see this project's return-shape-drift history.
    elapsed_seconds: float | None = None
    phase_seconds: dict | None = None


def _wear_spread(input_mix: list) -> tuple:
    wears = {row["wear"] for row in input_mix}
    idxs = [oddsmath.WEAR_ORDER.index(w) for w in wears if w in oddsmath.WEAR_ORDER]
    spread = (max(idxs) - min(idxs)) if idxs else 0
    return len(wears), float(spread)


def _feature_vector(*, from_rarity, input_mix, primary_collection, stattrak, cost,
                     collection_mean_roi) -> list:
    # `cost` is accepted but NOT included below -- see the FEATURE_NAMES comment for why
    # (it's unknowable at candidate-selection time, so training on it taught the model a
    # split it could never take when it mattered). Kept in the signature so both call sites
    # stay unchanged and re-adding it later, once a pre-simulation cost estimate exists,
    # doesn't mean touching them again -- same convention as learning._feature_vector's
    # thirdparty_gap_pct.
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


def _not_excluded(r, cache: dict | None = None) -> bool:
    """Extends the existing low_volume_warning filter with market_reliability.py's unified
    Excluded state (see that module's docstring) -- a row can clear the plain volume-based
    dead_skin check yet still rest on a skin currently Excluded for a DIFFERENT reason (a
    verified mispriced gap, an extreme rolling price-deviation z-score), which shouldn't teach
    this son's search heuristic anything either. Also excludes a row whose exact composition was
    itself already Stage-2-verified unprofitable (verified_low_roi.py) -- that row's
    expected_roi_pct label is now known to be an inflated/bugged estimate, so training on it
    would just re-teach the model the wrong answer for this composition.

    cache: optional per-batch memo passed straight through to market_reliability -- see
    train(), which shares one dict across this filter and the sample-weight loop."""
    if verified_low_roi_mod.is_marked(
            sim_blacklist_mod.signature(r.from_rarity, r.to_rarity, r.stattrak, r.input_mix)):
        return False
    return not market_reliability_mod.any_unit_excluded(
        r.input_mix, r.output_distribution, currency=1, stattrak=r.stattrak, cache=cache)


def forget(model_dir: Path, sims_file: Path) -> None:
    """Drops any in-process caches for this son's model/data. Call after a hard "full reset"
    (the caller is responsible for actually deleting the underlying files) so a stale cached
    model or collection-ROI lookup from before the reset can never be served again."""
    with _lock:
        _loaded_models.pop(model_dir, None)
        _collection_roi_caches.pop(sims_file, None)
    # simulate.py memoizes the parsed tail of this same sims_file (see its
    # load_recent_simulations). Its own guards would catch a reset on the next read anyway, but
    # a reset is exactly the moment to drop it outright rather than rely on the backstop.
    simulate_mod.forget_cached_rows(sims_file)


def _collection_roi_lookup(sims_file: Path) -> dict:
    if not sims_file.exists():
        return {}
    mtime = sims_file.stat().st_mtime
    # Read/written under _lock, matching how _loaded_models is handled a few functions down.
    # Whole-entry dict get/set on a per-file key is atomic under CPython anyway, so this was
    # never an observed bug -- but the two module caches sitting right next to each other under
    # different rules is the kind of inconsistency that invites a future non-atomic edit.
    # The lock covers only the dict access, never the load/filter below.
    with _lock:
        cached = _collection_roi_caches.get(sims_file)
    if cached and cached["mtime"] == mtime:
        return cached["lookup"]
    raw_rows = simulate_mod.load_recent_simulations(sims_file, MAX_ROWS_FOR_ANALYSIS)
    # Same per-batch reliability memo train() uses for the identical filter -- this walks up to
    # MAX_ROWS_FOR_ANALYSIS rows through market_reliability and hits the same handful of skins
    # over and over. train() got this memo when it was added; this sibling filter was missed and
    # kept paying the uncached price on a once-per-round hot path (measured 0.471s uncached vs
    # 0.180s cached over son_2's 3000 rows). Scoped to this call and dropped on return, exactly
    # as in train() -- see market_reliability.any_unit_excluded's `cache` parameter.
    reliability_cache: dict = {}
    rows = [r for r in raw_rows
            if not r.low_volume_warning and _not_excluded(r, cache=reliability_cache)]
    by_collection: dict = {}
    for r in rows:
        by_collection.setdefault(r.primary_collection, []).append(r.expected_roi_pct)
    lookup = {c: sum(v) / len(v) for c, v in by_collection.items()}
    with _lock:
        _collection_roi_caches[sims_file] = {"mtime": mtime, "lookup": lookup}
    return lookup


def collection_roi_lookup(sims_file: Path) -> dict:
    """Public entry point for _collection_roi_lookup -- lets a hot-loop caller (sim_loop.
    run_round) compute this ONCE per round and pass the same dict into every
    predict_expected_roi() call in that round (see its collection_roi_lookup param), instead of
    letting each call re-derive it from sims_file's mtime. That per-call mtime cache is fine for
    an occasional caller, but inside run_round's own simulate loop each candidate's
    log_simulation() append changes sims_file's mtime, invalidating the NEXT candidate's cached
    lookup -- turning what should be one O(n) pass per round into a full reload-and-refilter of
    the entire (ever-growing) simulations.jsonl on almost every single candidate. This was a real,
    measured bug: profiling one round on real data showed ~60 of ~76 seconds going here."""
    return _collection_roi_lookup(sims_file)


def train(model_dir: Path, sims_file: Path) -> TrainResult:
    """Fits a fresh model on every currently-logged simulation (for ONE son -- model_dir/
    sims_file are that son's own paths) and overwrites its persisted model + metadata. Refuses
    (not a crash) below MIN_TRAINING_SAMPLES. Cross-validated MAE is measured before the final
    fit-on-everything, mirroring learning.py.train().

    Excludes any row flagged low_volume_warning (a counted input/output price rests on Steam's
    own recent-sales volume below price_report.LOW_VOLUME_THRESHOLD -- an unreliable, thinly-
    traded price that shouldn't be allowed to teach the model a pattern that isn't real) --
    those rows are still preserved in sims_file, just never trained on. Also caps how many of
    the most recent raw rows are even considered (see MAX_ROWS_FOR_ANALYSIS) so retrain cost
    stays bounded no matter how long this son has been running.

    Every phase is wall-clock timed into the returned TrainResult (and persisted into this
    son's model metadata) so a slow retrain can be attributed to a specific phase instead of
    guessed at -- see TrainResult.phase_seconds."""
    t_start = time.perf_counter()
    phase: dict = {}

    def _mark(name: str, since: float) -> float:
        now = time.perf_counter()
        phase[name] = round(now - since, 3)
        return now

    # The training slot is taken HERE, around the whole retrain, not just around the fit. Every
    # phase below -- loading rows, the reliability filter, featurizing, the sample-weight walk --
    # is real CPU work competing for the same cores, and together they outweigh the fit itself on
    # every son (son_4: filter 0.514 + weights 0.971 against fit_best_model 1.155). Gating only
    # the fit left that majority running 6-wide, which is the exact thrash TRAIN_MAX_CONCURRENCY
    # exists to prevent. Waiting is still timed as its own phase so a son that queued behind two
    # siblings can never be mistaken for a son whose own work was slow.
    with _train_slots:
        # Measured from t_start: nothing happens between entering train() and reaching here, so
        # this is the whole of the queue wait and elapsed_seconds stays exactly accounted for.
        t = _mark("await_train_slot", t_start)
        return _train_locked(model_dir, sims_file, t_start, t, phase, _mark)


def _train_locked(model_dir: Path, sims_file: Path, t_start: float, t: float,
                   phase: dict, _mark) -> TrainResult:
    """train()'s body, running with a training slot already held -- see its call site for why
    the slot covers the whole retrain rather than only the fit. Split out purely so the gated
    region is one unindented block instead of the whole function nested a level deeper."""
    raw_rows = simulate_mod.load_recent_simulations(sims_file, MAX_ROWS_FOR_ANALYSIS)
    t = _mark("load_rows", t)

    # One memo shared by the exclusion filter below and the sample-weight loop further down:
    # both walk the same rows' same units through market_reliability, which re-derived each
    # skin's state from scratch every time (~18 repeat lookups per distinct skin per retrain
    # on real data). Scoped to this single train() call and dropped when it returns, so
    # nothing outside this function can be served a stale verdict -- see
    # market_reliability.any_unit_excluded's `cache` parameter.
    reliability_cache: dict = {}
    rows = [r for r in raw_rows
            if not r.low_volume_warning and _not_excluded(r, cache=reliability_cache)]
    t = _mark("filter_rows", t)
    if len(rows) < MIN_TRAINING_SAMPLES:
        return TrainResult(trained=False,
                            reason=f"Only {len(rows)} volume-confirmed, reliability-cleared simulated "
                                    f"trials logged, need {MIN_TRAINING_SAMPLES}+ (low-volume/thinly-traded/"
                                    f"market-reliability-excluded rows don't count)",
                            n_samples=len(rows), validation_mae=None, feature_importances=None,
                            elapsed_seconds=round(time.perf_counter() - t_start, 3),
                            phase_seconds=phase)

    import joblib
    import numpy as np

    loo_means = _leave_one_out_collection_means(rows)
    X = np.array([_featurize_record(r, loo_means[i]) for i, r in enumerate(rows)], dtype=float)
    y = np.array([r.expected_roi_pct for r in rows], dtype=float)
    t = _mark("featurize", t)

    # Reliability-weighted training (see market_reliability.training_weight): rows verified
    # across marketplaces teach at full strength, Steam-only rows at 0.35 -- reduced influence
    # instead of deletion. (Rows on currently-Excluded skins were already filtered above; their
    # weight would be 0 anyway -- the filter and the weight ladder agree by construction.)
    weights = []
    weight_reasons: dict = {}
    for r in rows:
        weight, reason = market_reliability_mod.training_weight(
            r.input_mix, r.output_distribution, currency=1, stattrak=r.stattrak,
            cache=reliability_cache)
        weights.append(weight)
        weight_reasons[reason] = weight_reasons.get(reason, 0) + 1
    # Recency: a row from weeks ago is a weaker signal about today's market than yesterday's.
    # Multiplies the reliability weight (does not replace it). age 0 = newest row.
    n = len(rows)
    weights = [w * (0.5 ** ((n - 1 - i) / RECENCY_HALFLIFE_ROWS)) for i, w in enumerate(weights)]
    sample_weight = np.array(weights, dtype=float)
    t = _mark("sample_weights", t)

    # Cross-validated model selection, same underlying algorithm/CV code as learning.py (see
    # ml_common.py's module docstring) but with this son's own FAST_* speed profile (see the
    # module-level comment above) -- this dataset is thousands of rows and retrains almost every
    # round, unlike learning.py's small real-outcomes model, so it gets a deliberately cheaper
    # (not learning.py-shared) configuration.
    # Read this son's PREVIOUS metadata once, before fitting: it supplies both the rolling
    # train-time history and the importance-refresh bookkeeping below.
    prev_meta = get_model_status(model_dir)
    prev_times = prev_meta.get("recent_train_seconds") or []
    prev_importances = prev_meta.get("feature_importances") or None
    retrain_counter = int(prev_meta.get("retrain_counter") or 0) + 1
    # Recompute the importance diagnostic on a schedule (see IMPORTANCE_EVERY_N_RETRAINS), but
    # always compute it if we have no previous values to fall back on -- otherwise a freshly
    # reset son would show empty importance bars for its first 9 retrains.
    fresh_importances = (prev_importances is None
                         or retrain_counter % IMPORTANCE_EVERY_N_RETRAINS == 0)

    selection = ml_common.fit_best_model(X, y, FEATURE_NAMES, random_state=42,
                                          sample_weight=sample_weight,
                                          candidates=FAST_CANDIDATES, n_folds=FAST_N_FOLDS,
                                          importance_n_repeats=(FAST_IMPORTANCE_REPEATS
                                                                if fresh_importances else 0),
                                          hgb_loss=FAST_HGB_LOSS,
                                          hgb_max_bins=(FAST_MAX_BINS
                                                        if len(rows) >= FAST_MAX_BINS_MIN_ROWS
                                                        else None),
                                          max_threads=FAST_MAX_THREADS)
    model, mae = selection.model, selection.cv_mae
    # On a skipped refresh fit_best_model returns {} -- carry the last real values forward
    # rather than blanking the dashboard, and mark them stale so the display can say so.
    importances = selection.feature_importances if fresh_importances else prev_importances
    t = _mark("fit_best_model", t)
    phase.update(selection.timings)  # ml_common's own cv:*/final_refit/importance sub-phases

    # Same real-residual-based uncertainty band as learning.py's train() -- kept in sync
    # deliberately (see this project's documented sibling-drift bug history). NOT threaded
    # through predict_expected_roi()'s return value: that function's contract is a bare
    # `float | None` consumed positionally by random_trade.py's sampling bias and
    # ranking._ml_agreement_bonus, and widening it risks the exact "tuple/return-shape drift"
    # failure class this codebase has hit before. The interval is exposed only via
    # get_model_status() for the dashboard's per-son model-quality display.
    residuals = np.array(selection.oof_residuals, dtype=float)
    residual_p10 = float(np.percentile(residuals, 10))
    residual_p90 = float(np.percentile(residuals, 90))

    with _lock:
        model_dir.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, model_dir / MODEL_FILENAME, compress=3)
        elapsed = round(time.perf_counter() - t_start, 3)
        phase["persist"] = round(time.perf_counter() - t, 3)
        meta = {"trained_at": time.time(), "n_samples": len(rows), "validation_mae": mae,
                "feature_importances": importances, "feature_names": FEATURE_NAMES,
                "model_name": selection.model_name, "candidate_maes": selection.candidate_maes,
                "sample_weight_breakdown": weight_reasons,
                "mean_sample_weight": round(float(sample_weight.mean()), 3),
                # Importance bookkeeping (see IMPORTANCE_EVERY_N_RETRAINS): the counter drives
                # the refresh schedule, the stale flag tells the dashboard these bars came from
                # an earlier retrain so it never presents carried-forward numbers as fresh.
                "retrain_counter": retrain_counter,
                "feature_importances_stale": not fresh_importances,
                "residual_p10": residual_p10, "residual_p90": residual_p90,
                # Training cost of THIS retrain plus a short rolling tail of previous ones, so
                # the dashboard can show a per-agent trend ("is this agent getting slower as its
                # simulations.jsonl grows?") without needing a separate history file.
                "train_seconds": elapsed, "phase_seconds": phase,
                "recent_train_seconds": (prev_times + [elapsed])[-TRAIN_TIME_HISTORY_KEEP:]}
        (model_dir / META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")
        _loaded_models.pop(model_dir, None)  # force a reload on the next predict_expected_roi() call

    return TrainResult(trained=True, reason="", n_samples=len(rows), validation_mae=mae,
                        feature_importances=importances, model_name=selection.model_name,
                        candidate_maes=selection.candidate_maes,
                        elapsed_seconds=elapsed, phase_seconds=phase)


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
    # A model persisted against a DIFFERENT feature set can't be used: predict_expected_roi
    # would hand sklearn an array with the wrong column count and raise mid-round. Reporting
    # it as untrained instead makes the next round fall back to novelty-only scoring and the
    # next retrain overwrite it -- the same self-healing path a son that never trained takes,
    # so changing FEATURE_NAMES never needs a manual model wipe. (This bit first, when `cost`
    # was dropped from FEATURE_NAMES -- every son on disk carried an 8-column model.)
    if list(meta.get("feature_names") or []) != FEATURE_NAMES:
        return {"trained": False, "n_samples": 0, "validation_mae": None, "trained_at": None,
                "min_samples_required": MIN_TRAINING_SAMPLES,
                "stale_feature_set": True, "stale_feature_names": meta.get("feature_names")}
    return {"trained": True, "min_samples_required": MIN_TRAINING_SAMPLES, **meta}


def predict_expected_roi(candidate, model_dir: Path, sims_file: Path,
                          collection_roi_lookup: dict | None = None) -> float | None:
    """Scores a not-yet-simulated random_trade.Candidate for random_trade.generate_batch's
    guided sampling -- estimated from the candidate's structure alone (no pricing needed), so
    it's cheap enough to call on every random draw before deciding which ones are even worth
    the cost of a full simulate.simulate() call (which can now block on a real live price fetch
    for any skin/wear not already cached). Returns None
    if no model is trained yet -- callers must treat that as "no signal", never fabricate a
    number (same contract as learning.predict_roi). model_dir/sims_file identify WHICH son's
    model/data to use -- see the module docstring for why these aren't fixed constants.

    collection_roi_lookup: pass a pre-fetched collection_roi_lookup(sims_file) result to skip
    this call's own internal lookup -- see sim_loop.run_round, which computes it ONCE per round
    and reuses it across every candidate instead of paying for a fresh one each time (this
    function's own per-call lookup is mtime-cached, but a hot loop calling log_simulation()
    between candidates invalidates that cache on almost every call -- see collection_roi_lookup's
    docstring). None (default) falls back to the original per-call lookup, fine for an
    occasional/non-hot-loop caller."""
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
    if collection_roi_lookup is None:
        collection_roi_lookup = _collection_roi_lookup(sims_file)
    collection_mean = collection_roi_lookup.get(candidate.legs[0].collection, float("nan"))
    # cost is unknown pre-simulation -- fed as NaN, which HistGradientBoostingRegressor
    # handles natively (it has real cost values at training time, unlike the all-NaN
    # thirdparty_gap_pct case learning.py's FEATURE_NAMES comment documents avoiding).
    features = _feature_vector(from_rarity=candidate.from_rarity_name, input_mix=input_mix,
                                primary_collection=candidate.legs[0].collection, stattrak=candidate.stattrak,
                                cost=float("nan"), collection_mean_roi=collection_mean)
    return float(model.predict(np.array([features], dtype=float))[0])
