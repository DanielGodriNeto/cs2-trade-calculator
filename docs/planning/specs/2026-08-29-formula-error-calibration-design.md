# Formula-Error Calibration Model — Design Spec (M3)

**Date:** 2026-08-29
**Status:** Design only. A follow-on `writing-plans` cycle builds it from this spec; nothing here is implemented.
**Owner module (to be created):** `src/formula_calibration.py` — a **third** model, structurally parallel to `src/learning.py` and `src/sim_learning.py`, never merged with either.
**Source finding:** M3 in the 2026-08-29 ML & performance code review (`docs/planning/plans/2026-08-29-ml-and-perf-betterments.md`).

---

## 1. Goal

`simulate.simulate()` computes `expected_roi_pct` from a fixed formula: probability-weighted output revenue (`oddsmath`) minus input cost, minus `tradeup.STEAM_SELL_FEE`, all off Steam cache prices. That formula has **systematic, learnable error** — it is consistently optimistic for thin/dead-skin collections (surviving priced outputs skew expensive), consistently wrong in a specific direction for particular output-price tiers, etc. Stage-2 verification already catches this case-by-case (`verify_queue` cross-checks against Skinport / Buff163 / CSFloat), but only for the handful of compositions that clear the rate-limited queue.

M3 generalises that signal: **train a small regressor to predict the formula's error from a candidate's structure, and subtract the predicted error to get `calibrated_roi_pct`.** It is a bias-correction layer on the formula, learned from independent third-party price evidence — not a new ROI model, not a replacement for anything.

### Non-goals

- Not a replacement for the formula. `expected_roi_pct` stays authoritative and unchanged everywhere it is computed; M3 only *adds* `calibrated_roi_pct` alongside it.
- Not merged with `learning.py` (real user trades) or `sim_learning.py` (per-son simulated EV). It is a third store, third model directory, third `MIN_TRAINING_SAMPLES` gate. See §1.1.
- Not a per-candidate live fetch. It scores from already-cached structure/prices, like `sim_learning.predict_expected_roi`.
- Not a fill-risk model. It corrects the formula toward *verified cross-market prices*, which are still listed prices, not fills.

### 1.1 Firewall — why this does not breach "never merge the two models / never train on our own numbers"

The project's firewall rule (stated in `learning.py`'s and `sim_learning.py`'s module docstrings, and `ml-and-perf-betterments` Global Constraints):

> `sim_learning` (trained on simulated EV) and `learning` (trained on real logged `outcomes.jsonl` trades) never share training data; neither is ever fed the tool's own guesses back in as if they were ground truth.

M3 does not touch either side of that firewall:

| | Trains on | Sees a user's executed trade? | Label is "our own number"? |
|---|---|---|---|
| `learning.py` | `data/outcomes.jsonl` — real money in/out | **Yes** (that's its whole point) | No — real realised ROI |
| `sim_learning.py` | per-son `simulations.jsonl` — `expected_roi_pct` of simulated trials | No | Yes, deliberately — it's a *search heuristic*, never presented as real-world ROI |
| **`formula_calibration.py` (M3)** | `(features, verified_roi_pct − expected_roi_pct)` pairs, where `verified_roi_pct` comes from `verify_queue` Stage-2 **external-market cross-checks** | **No** | No — the label is `independent_third_party_evidence − our_formula`, i.e. *how wrong our formula was vs. a source we do not control* |

The M3 label is a **residual against independent evidence**. `verified_roi_pct` is computed in `verify_queue._summarize()` (`src/verify_queue.py:141-167`) from `price_report.PriceRow.consensus_price` values — the cheapest of Steam / Skinport / Buff163 / CSFloat, or the cross-site **median** when the sources disagree by ≥ `price_report.HIGH_SPREAD_THRESHOLD_PCT` (20%). That number is not something this tool made up; it is a live reading of three third-party marketplaces. Training a model to predict `verified_roi_pct − expected_roi_pct` teaches it **the formula's systematic offset from the wider market**, and nothing else. It never sees `outcomes.jsonl`. It never sees a simulated `expected_roi_pct` used *as a target* (only as one input feature and as the subtrahend of the label). It is not the real-ROI model and is not presented as one.

**The code reviewer of the M3 implementation PR must explicitly confirm, in the review, all four of:**
1. `formula_calibration.py` imports neither `learning` nor `sim_learning` for data (only `ml_common` for the shared *algorithm*, which carries no data — see `ml_common.py:1-11`);
2. its training corpus is assembled only from `verify_queue`-written `verified_roi_pct` values (checkpoint entries + the M3 pair store, §2), never from `outcomes.jsonl`;
3. the label is `verified_roi_pct − expected_roi_pct`, not `verified_roi_pct` alone and not `expected_roi_pct` alone;
4. `calibrated_roi_pct` is additive-only: no code path replaces or overwrites `expected_roi_pct`.

Sharing `ml_common.fit_best_model` is explicitly fine — its docstring (`ml_common.py:1-11`) already states it "carries no data of its own and never will", exactly so a third caller like this one can reuse the CV/fit/importance code without the firewall implication.

---

## 2. Label source & the data on disk

### 2.1 The pair

One training row = `(feature_vector(entry), verified_roi_pct − expected_roi_pct)`.

- `expected_roi_pct` — the formula ROI stored on the `SimulationRecord` / checkpoint `best` entry at simulate time (`simulate.py:215`).
- `verified_roi_pct` — the Stage-2 cross-market ROI. Computed at `verify_queue.py:166`:
  ```
  after_fee = expected_revenue * (1 - STEAM_SELL_FEE)          # expected_revenue from consensus_price, prob-weighted
  verified_roi_pct = (after_fee - cost) / cost * 100           # cost from consensus_price of each input unit
  ```
  Returned by `verify_queue._summarize(entry, rows)` and written back onto the live checkpoint `best` entry by `verify_queue._writeback()` (`src/verify_queue.py:192-210`) as `entry["verified_roi_pct"]`, `entry["price_confidence"]`, `entry["verified_at"]`. Also handed to `verified_low_roi_mod.record(sim_blacklist_mod.signature_of(entry), verified_roi_pct)` at `verify_queue.py:188` inside `summarize_and_writeback()`.
- The label is `None`-guarded: `verify_queue._summarize` returns `verified_roi_pct = None` when the verified data doesn't cover enough of the contract (`price_report` confidence gate). Rows with `verified_roi_pct is None` are **not** training pairs.

### 2.2 What is actually on disk today — and why M3 needs its own store

| File | Size today | Content | Usable directly as an M3 corpus? |
|---|---|---|---|
| `data/verified_low_roi.json` | ~396 KB | `{"signatures": {sig: {"verified_roi_pct", "marked_at"}}}` — **only** compositions whose verified ROI came in **at or below `VERIFIED_ROI_FLOOR_PCT` (0.0)**. `verified_low_roi.record()` marks losers and *clears* winners (`src/verified_low_roi.py:73-88`). | **No** — one-sided (losers only) and carries no features. Using it alone would train M3 that the formula is always optimistic. |
| `data/mispriced_skins.json` | ~657 KB | Per-skin verified Steam-vs-market gaps (`{"skins": {key: {gap_pct, direction, steam_price, cheapest_price, cheapest_source, marked_at, ...}}}`, `src/mispriced_skins.py:80-97`). Self-healing, keyed by `market_hash_name`. | **No** — per *skin*, not per *composition*; and it also only stores currently-active gaps, not a full history of verifications. Useful as a *feature* (§3), not as the corpus. |
| `data/sons/verification_queue.json` | (rolling) | Last `MAX_LOG_KEPT = 200` auto-verify results: `{son_id, id, primary_collection, verified_roi_pct, price_confidence, verified_at, error}` (`src/verify_queue.py:213-224`, `_append_log`). | Partially — has `verified_roi_pct` and enough to locate the originating `SimulationRecord`, but only 200 rows and no features. |
| `data/sons/son_*/checkpoint.json` `best[]` entries | — | Entries that were verified carry `verified_roi_pct` + `verified_at` + the full `input_mix` / `output_distribution` / `expected_roi_pct` needed to featurize. | Yes, but only the current top-N per son survive; older verified entries fall out of `best` (`sim_loop`'s `BEST_KEPT` trim). |

**Conclusion: M3 gets its own append-only pair store**, `data/formula_calibration_pairs.jsonl`, written by a one-line hook at the single choke point every verification already flows through — `verify_queue.summarize_and_writeback()` (`src/verify_queue.py:170-189`), which its own docstring calls "the shared … single place every verification's per-skin gaps feed the mispriced-skin registry … and the place the whole COMPOSITION's verified ROI feeds verified_low_roi.py". M3 adds one more sink there:

```
formula_calibration.record_pair(entry, verified_roi_pct, price_confidence)   # appends one JSONL line
```

Each line: `{"signature", "verified_at", "expected_roi_pct", "verified_roi_pct", "price_confidence", "features": {...}, "feature_schema_version": N}`. Append-only, skip-corrupt-line reader, `data/` not `cache/` — identical conventions to `outcomes.jsonl` / `simulations.jsonl` / `price_history.jsonl`. This captures **both** winners and losers, with features frozen at verification time, and grows naturally as the Verify-queue / Verify-top-50 button run. A backfill helper (`formula_calibration.backfill_from_checkpoints()`) seeds it once from whatever verified `best[]` entries currently exist across the 6 sons.

---

## 3. Feature set

Aligned, where possible, to quantities that already exist as real `sim_learning.FEATURE_NAMES` entries after M6 (`src/sim_learning.py:166-172`) so the featurizer can borrow `sim_learning`'s helpers rather than re-deriving:

| Feature | Source / definition | Notes |
|---|---|---|
| `collection` (target-encoded) | leave-one-out mean of `(verified_roi_pct − expected_roi_pct)` per `primary_collection` across the pair store — same LOO scheme as `sim_learning._leave_one_out_collection_means` (`sim_learning.py:284-295`) and `learning._leave_one_out_collection_means` | never one-hot; NaN when a collection has one pair |
| `output_price_tier` | `log10(max(1e-3, mean priced output price))` bucketed, or the raw log value — the priced-output magnitude the formula's optimism scales with | from `entry["output_distribution"][k]["price"]` |
| `liquidity_dead_skin_score` | worst counted unit's `dead_skin.non_volume_score(u["dead_skin_score"], u["dead_skin_reasons"])` across `input_mix + output_distribution` — the exact quantity `simulate.py:497-500` and `ranking._confidence_score` already compute | 0–100; the dead-skin axis the formula is most wrong on |
| `n_target_outputs` | `entry["n_output_variants"]` (a.k.a. `sim_learning`'s `n_target_outputs`, `SimulationRecord.n_output_variants`, `simulate.py:219`) | odds-dilution denominator |
| `filler_fraction` | `1 − primary_count / total_count` over `input_mix` — identical to `sim_learning._feature_vector`'s `filler_fraction` (`sim_learning.py:256-260`) | how much of the contract is mixed-in filler collections |
| `output_input_price_ratio` | `mean(non-None output prices) / mean(non-None input unit_price)` — `sim_learning._price_ratio` (`sim_learning.py:238-245`), computed from the frozen entry prices, not a live fetch | NaN when a whole side is unpriced |
| `stattrak` | `entry["stattrak"]` | 0/1 |
| `from_rarity_rank` | `sim_learning.RARITY_RANK_BY_NAME[entry["from_rarity"]]` | ordinal |
| `cost_basis_source_stale` | `1.0 if entry["cost_basis_source"] == "stale" else 0.0` | a stale cost basis is a known formula-error amplifier |
| `has_active_mispriced_unit` | `1.0` if any `input_mix`/`output_distribution` unit's `market_hash_name` was in `mispriced_skins` at `verified_at` (frozen into the pair at record time) | ties in the per-skin gap signal that `verified_low_roi.py`'s docstring says compounds into composition-level error |

`FEATURE_NAMES` is a module constant, versioned (`FEATURE_SCHEMA_VERSION`), and `get_model_status()` invalidates a persisted model whose `feature_names` don't match — the same self-healing guard as `sim_learning.get_model_status` (`sim_learning.py:574-583`). Pairs written under an older schema are re-featurized on load when possible, dropped when not.

Fitting: `ml_common.fit_best_model(X, y, FEATURE_NAMES, random_state=42, sample_weight=...)`. `sample_weight` = `1.0` for `price_confidence == "ok"`, `0.4` for `"low volume"` / `"no 3rd-party match"`, `0.15` for `"low volume + no 3rd-party match"` (mirrors the reliability-weight ladder in `learning.train` / `sim_learning.train`, keyed off `verify_queue`'s own `_worst_confidence` labels). No monotonic constraint. HGB handles the NaN features natively.

---

## 4. Where the correction applies

### 4.1 The new field

```
predicted_formula_error = formula_calibration.predict_error(entry)   # float, or None if dormant/untrained
calibrated_roi_pct      = expected_roi_pct - predicted_formula_error
```

`predict_error` returns `None` when the model is dormant (§5) or the loaded model file is mid-write — callers treat `None` as "no calibration available" and fall back to `expected_roi_pct`, never fabricate a number (same contract as `learning.predict_roi` / `sim_learning.predict_expected_roi`).

`calibrated_roi_pct` is attached:
- onto the `SimulationRecord` as a new optional field `calibrated_roi_pct: float | None = None` (default `None` so old `simulations.jsonl` lines still load via `SimulationRecord(**json.loads(line))`, same backward-compat reasoning as `avg_input_float` / `ml_predicted_roi_pct` already documented at `simulate.py:249-266`), set by `sim_loop.run_round()` right after `simulate()` returns — the same "one place that actually knows" pattern already used there for `agent` and `ml_predicted_roi_pct`;
- onto the ranking payload (`ranking.rank()` output dicts) and the dashboard row, **beside** the existing three ROI numbers — formula (`expected_roi_pct`), ML (`ml_predicted_roi_pct`), verified (`verified_roi_pct`) — so the operator sees all four. This is the same panel the telemetry-HUD OPPS row-expand already plans to show "formula ROI + ML ROI + verified ROI shown together" (`docs/planning/specs/2026-08-29-telemetry-hud-redesign-design.md` §6.2); M3 adds a fourth cell, `calibrated`.

### 4.2 `ranking.effective_roi` preference order

Today (`src/ranking.py:82-90`):

```python
def effective_roi(entry: dict) -> float:
    verified = entry.get("verified_roi_pct")
    return verified if verified is not None else entry.get("expected_roi_pct", 0.0)
```

Becomes a three-step preference — **verified → calibrated → expected**:

```python
def effective_roi(entry: dict) -> float:
    verified = entry.get("verified_roi_pct")
    if verified is not None:
        return verified
    calibrated = entry.get("calibrated_roi_pct")
    if calibrated is not None:
        return calibrated
    return entry.get("expected_roi_pct", 0.0)
```

Rationale for the ordering: a Stage-2 verification is direct evidence for *this exact composition* and always wins; `calibrated_roi_pct` is a model's estimate of the formula's bias, better on average than the raw formula but still a model; `expected_roi_pct` is the floor. This flows automatically into everything that already routes through `effective_roi`: `ranking._squash_roi` / `_profit_amount` / `_ml_agreement_bonus` / `_ensemble_agreement_bonus` / `score_breakdown` / the composite, and `sim_loop`'s verified-aware `best` ordering (which calls `ranking.effective_roi` directly — see its docstring at `ranking.py:82-88`).

`_ml_agreement_bonus` and `_ensemble_agreement_bonus` compare a model prediction against `_effective_roi(entry)`. With M3 live, an unverified entry's `_effective_roi` becomes `calibrated_roi_pct`, so those bonuses now measure "does the surrogate agree with the *calibrated* formula" — a small, deliberate tightening, called out in the implementation plan so the reviewer expects the composite-score shift on unverified rows.

### 4.3 What does **not** change

- `expected_roi_pct` is never overwritten, anywhere.
- `simulate.simulate()`'s math is untouched.
- The M3 model has no vote on training either `learning` or `sim_learning`; `calibrated_roi_pct` is not fed back into any `simulations.jsonl` training label (it's a display/ranking field, and `sim_learning.train` trains on `r.expected_roi_pct`, unchanged).

---

## 5. Cold start

M3 is **dormant below `MIN_TRAINING_PAIRS` pairs**, mirroring `learning.MIN_TRAINING_SAMPLES`:

```python
MIN_TRAINING_PAIRS = 20   # == learning.MIN_TRAINING_SAMPLES; below this, train() refuses and predict_error() returns None
```

While dormant:
- `formula_calibration.train()` returns a `TrainResult(trained=False, reason="Only N verified pairs, need 20+", ...)` — refuses, does not crash (identical shape to `learning.TrainResult` / `sim_learning.TrainResult`).
- `predict_error()` returns `None` for every candidate.
- `calibrated_roi_pct` is left `None`; `effective_roi` falls straight through to `expected_roi_pct`; the dashboard's `calibrated` cell shows `—`.
- `get_model_status()` reports `{"trained": False, "n_pairs": N, "min_pairs_required": 20}` for the dashboard's model-status indicator (same shape as `learning.get_model_status`).

The pair store accumulates whenever the auto Verify-queue drains or the "Verify top 50" button runs (both funnel through `summarize_and_writeback`), so a user who runs verification at all will cross 20 pairs without doing anything M3-specific. `backfill_from_checkpoints()` (§2.2) can seed most or all of it in one shot from existing verified `best[]` entries.

Retrain trigger: after `RETRAIN_TRIGGER_PAIRS = 5` new pairs (matching `retrain_loop.py`'s real-model delta, not `sim_learning`'s 25 — verified pairs accrue slowly, like real outcomes), on the same background retrain loop the other models use. Persisted to `data/model_formula_calibration/` (its own directory, `compress=3` joblib, `roi_model_meta.json` sidecar) — never `data/model/` (that's `learning`) and never a son's `model/` dir (that's `sim_learning`).

---

## 6. Guardrail

**Clamp the correction magnitude at ±`MAX_CORRECTION_PP` (default 50 percentage points):**

```python
MAX_CORRECTION_PP = 50.0

raw = model.predict(...)                       # predicted_formula_error, unclamped
predicted_formula_error = max(-MAX_CORRECTION_PP, min(MAX_CORRECTION_PP, raw))
calibrated_roi_pct = expected_roi_pct - predicted_formula_error
```

Why: a bad fit (small corpus, an outlier-heavy stratum, a feature-schema mishap) could otherwise predict a −400 pp "error" and flip a genuine +30% trade-up to −370%, or turn a −60% dud into +40% and float it to the top of OPPS. A ±50 pp clamp keeps M3 able to move a marginal call across the 0% line (its whole point) while making it structurally impossible for the calibration layer to **invert the sign of a trade-up that the formula was confident about** (|`expected_roi_pct`| > 50). The clamp is applied in `predict_error()` itself, so every consumer gets the bounded value; the unclamped raw prediction is logged to the meta sidecar for diagnostics only.

Secondary guardrail: if `TrainResult.validation_mae` for the M3 model exceeds `expected_roi_pct`'s own dispersion by a wide margin (`--m3-mae-ceiling`, default: CV MAE > 60 pp), `train()` still persists the model but `get_model_status()` flags `"low_confidence": true` and `predict_error()` halves its output (`predicted_formula_error *= 0.5`) — a poorly-fit calibrator contributes a damped correction rather than a confident wrong one. This mirrors `ranking.py`'s existing "scale the bonus down when the signal is weak" posture (`ML_AGREEMENT_UNVERIFIED_SCALE`).

---

## 7. File / module plan

```
src/formula_calibration.py                     new: record_pair(), load_pairs(), backfill_from_checkpoints(),
                                                    train(), predict_error(), get_model_status(), reset_model()
                                                    FEATURE_NAMES / FEATURE_SCHEMA_VERSION / MIN_TRAINING_PAIRS=20 /
                                                    RETRAIN_TRIGGER_PAIRS=5 / MAX_CORRECTION_PP=50.0
data/formula_calibration_pairs.jsonl           new: append-only (features, verified_roi_pct, expected_roi_pct) pairs
data/model_formula_calibration/                new: roi_model.joblib (compress=3) + roi_model_meta.json

src/verify_queue.py                            +1 call in summarize_and_writeback(): formula_calibration.record_pair(...)
src/simulate.py                                SimulationRecord: + calibrated_roi_pct: float | None = None
src/sim_loop.py                                run_round(): set record.calibrated_roi_pct after simulate();
                                                    _record_to_entry() / list_best_simulations() carry it through
src/ranking.py                                 effective_roi(): verified -> calibrated -> expected (3-step)
web/server.py                                  model-status endpoint reports formula_calibration.get_model_status();
                                                    opportunity payload carries calibrated_roi_pct
tools/check_formula_calibration.py             new: assertion script (dormant<20 -> None; clamp at +-50;
                                                    additive-only; firewall imports absent)
```

Reuses: `ml_common.fit_best_model` / `_thread_limit`, `sim_learning._price_ratio` / `_leave_one_out_collection_means` / `RARITY_RANK_BY_NAME` (or copies with a shared-drift note), `dead_skin.non_volume_score`, `sim_blacklist.signature_of`, `mispriced_skins.active_gap`, `verify_queue`'s existing `_worst_confidence` labels. Lazy `sklearn`/`joblib`/`numpy` imports inside `train()` / `predict_error()`, same as every other model module.

## 8. Deliverable of the follow-on plan

`src/formula_calibration.py` + the wiring in §7, with:
- `tools/check_formula_calibration.py` in the `python tools/<name>.py -> OK/raise` style asserting: refuses below 20 pairs and `predict_error` returns `None`; a synthetic corpus where the formula is uniformly +30 pp optimistic yields `predicted_formula_error ≈ +30` and `calibrated_roi_pct ≈ verified`; the ±50 pp clamp holds on a crafted outlier; `effective_roi` returns `calibrated` for an unverified entry and `verified` when both present;
- a one-shot `backfill_from_checkpoints()` run recorded in the plan's final step showing how many pairs the current 6 sons' checkpoints yield;
- the code-review confirmation of the four firewall points (§1.1) captured in the review notes.

---

## Self-review (placeholder scan)

Scanned for `TBD`, `TODO`, `FIXME`, `add error handling`, bare "write tests", `figure out`, hand-wave `etc.`:
- None present.
- All five required brief points covered concretely: (1) label source `verified_roi_pct − expected_roi_pct` with the firewall argument + the explicit four-point reviewer checklist (§1.1), and the on-disk reality of `verified_low_roi.json` (~396 KB) / `mispriced_skins.json` (~657 KB) / `verification_queue.json` / checkpoint `best[]`, plus why a dedicated `formula_calibration_pairs.jsonl` store is needed (§2); (2) feature set with each feature tied to a real `sim_learning`/`simulate`/`dead_skin` quantity (§3); (3) `calibrated_roi_pct = expected_roi_pct − predicted_formula_error` surfaced beside formula/ML/verified, and the concrete 3-step `effective_roi` rewrite quoting the current `ranking.py:82-90` body (§4); (4) cold start dormant below `MIN_TRAINING_PAIRS = 20` == `learning.MIN_TRAINING_SAMPLES` (§5); (5) ±50 pp clamp with the sign-inversion rationale + a damped-correction secondary guard (§6).
- Every code citation is a verified file+line (`verify_queue.py:141-167/166/188/192-210/213-224`, `ranking.py:82-90`, `sim_learning.py:166-172/238-245/256-260/284-295/574-583`, `simulate.py:215/219/249-266/497-500`, `verified_low_roi.py:73-88`, `mispriced_skins.py:80-97`, `ml_common.py:1-11`).
- `learning.MIN_TRAINING_SAMPLES = 20` confirmed by reading `src/learning.py:37`.
