# Walk-Forward Backtest Harness — Design Spec (N2)

**Date:** 2026-08-29
**Status:** Design only. A follow-on `writing-plans` cycle builds it from this spec; nothing here is implemented.
**Owner module (to be created):** `tools/backtest_harness.py` + `tools/backtest/` package. Read-only over `data/`. No `src/` change except the config seams M1/M3/M6 already added.
**Source finding:** N2 in the 2026-08-29 ML & performance code review (`docs/superpowers/plans/2026-08-29-ml-and-perf-betterments.md`). N3 (stratified candidate generation) is folded in — see §7.

---

## 1. Goal

Answer one question offline, from committed data, in under five minutes: **does a given ranking / model / feature change actually pick better trade-ups, or does it just move the CV-MAE number?**

The existing `tools/mae_parity.py` gate proves a `sim_learning` change didn't regress cross-validated MAE on a son's own `simulations.jsonl`. It does **not** prove the *ranked output the user sees* got better — MAE is measured against `expected_roi_pct` (a formula estimate), not against what the market actually did. This harness closes that gap: it replays history, ranks the candidates that were visible at a past instant, and scores that ranking against subsequent real price movements in `data/price_history.jsonl`.

### Non-goals

- Not a live paper-trader. It never fetches; it only reads committed files.
- Not a fill-risk model. It measures *ranking quality* (did we order the candidates well?), not *execution risk* (could you actually buy the inputs / sell the output at that price?). See §6.
- Not a replacement for `mae_parity.py`. That stays the fast per-commit gate; this is the slower, run-it-before-a-merge instrument.
- Not a UI. Output is a printed table + a JSON artifact under `tools/backtest/out/`.

---

## 2. Data sources & time index

Three stores, three different notions of "time". The harness has to reconcile them.

### 2.1 `data/price_history.jsonl` — the only wall-clock price series

Written by `src/price_history.py:record_snapshot()`, one line per genuinely-fresh (non-cache-hit) Steam price fetch. Verified line shape (`price_history.py:49-56`):

```json
{"ts": 1783521262.93, "market_hash_name": "PP-Bizon | RMX (Battle-Scarred)",
 "currency": 1, "lowest_price": 0.11, "median_price": 0.14, "volume": 252}
```

- `ts` — epoch seconds (`time.time()`), the authoritative wall-clock axis for the whole harness.
- `market_hash_name` — already `steam_prices.market_hash_name(skin, wear, stattrak)`-formatted, so it joins directly against a `SimulationRecord`'s `input_mix[i]` / `output_distribution[i]` once those are re-encoded the same way.
- `lowest_price` / `median_price` — either may be `null` (see the `UMP-45 | Fragment` line in the real file); the harness prefers `lowest_price`, falls back to `median_price`, treats "both null" as "no observation".
- `currency` — always `1` in this codebase (`sim_loop.run_round`'s default); the harness filters to `currency == 1` and asserts the file contains nothing else.

Access helpers that already exist and must be reused, not reimplemented:
- `price_history.get_history(market_hash_name, currency) -> list[dict]` — all snapshots for one name, oldest first.
- `price_history.predict_next_price(history, field="lowest_price") -> PredictionResult | None` — the naive linear-trend extrapolator, `None` below `MIN_HISTORY_POINTS = 5`. Used only as an optional baseline "predictor under test" (§4), never as ground truth.

**Properties the harness must respect, stated plainly:**
- **Not retroactive.** A skin/wear pair has history only from the first time this tool looked it up. Before that instant there is *no* row — not a zero, not an interpolation.
- **Sparse and irregular.** Snapshots land whenever a scan happened to fetch that name fresh. Gaps of days are normal. The harness never assumes a fixed sampling cadence.
- **Append-only, never rewritten** (same convention as `outcomes.jsonl`). Safe to `mmap`/stream.

### 2.2 Per-son `data/sons/son_*/round_history.jsonl` — round-indexed, not a price series

Written by `sim_loop.run_round()` (`sim_loop.py:1221-1240`), the uncapped mirror of the checkpoint's trimmed `history` list. Verified line shape:

```json
{"round": 1, "n_simulated": 40, "best_roi_ever": -49.9, "avg_input_float": 0.44,
 "avg_roi_pct": -79.15, "avg_confidence": 98.25, "cumulative_cost": 17611.44,
 "cumulative_expected_profit": -14040.04, "at": 1783697218.73}
```

- `round` — a per-son monotone counter. **This is the primary key, not time.**
- `at` — epoch seconds for when that round *finished*. Present, so a round can be located on the wall clock, but everything else in the row is a **per-round aggregate across every valid candidate simulated that round** (`avg_roi_pct`, `avg_confidence`, `avg_input_float`) — there is no per-candidate detail here.
- `best_roi_ever` / `cumulative_*` — running totals, not point-in-time observations.

**Use in the harness:** `round_history.jsonl` is read for two things only —
1. bounding the usable backtest window (`min(at)` across sons is the earliest cut time worth trying; `max(at)` the latest);
2. as an optional context column in the report ("at cut time T, son_3 was on round ~R with avg_roi_pct ~X"), purely descriptive.

It is **never** the candidate source and **never** a label source. `load_round_history(son_id, max_points)` (`sim_loop.py:553`) is the existing reader.

### 2.3 Per-son `data/sons/son_*/simulations.jsonl` — the candidate corpus, timestamped per row

`simulate.SimulationRecord`, append-only, never truncated (`simulate.py:825` docstring). Verified fields the harness depends on (`simulate.py:200-267`):

| Field | Type | Harness use |
|---|---|---|
| `id` | str | stable candidate key |
| `simulated_at` | float epoch (`time.time()`) | **the per-candidate time index** — the only reason this file, not the checkpoint, is the corpus |
| `from_rarity`, `to_rarity`, `stattrak` | str/str/bool | rarity-pair stratum; re-rank inputs |
| `primary_collection` | str | collection stratum |
| `input_mix` | list of `{collection, skin, wear, count, unit_price, volume, dead_skin_score, dead_skin_reasons, skinport_price, buff163_price, csfloat_price, image}` | re-price the input side from `price_history` at T and T+Δ; re-encode each unit to a `market_hash_name` |
| `output_distribution` | list of `{skin, wear, prob_pct, price, net_profit, volume, dead_skin_score, dead_skin_reasons, wear_probs, ...}` | re-price the output side; `prob_pct` is the odds weight |
| `cost` | float | recorded cost basis (for reference; the harness recomputes) |
| `expected_roi_pct` | float | the formula ROI = the "prediction" for correlation metrics; the M3-correction input |
| `cost_basis_source` | `"cache"` \| `"stale"` | feeds `ranking._confidence_score` when re-ranking |
| `confidence_score` | float \| None | ditto |
| `low_volume_warning` | bool | excluded from strict re-ranking, matching `sim_loop._consider_best` |
| `n_output_variants` | int | `n_target_outputs` feature (M6) |
| `verified_roi_pct`, `verified_at` | float \| None | if a row was Stage-2-verified before T, the harness may use `verified_roi_pct` as a stronger (still not perfect) label — see §3.3 |
| `ml_predicted_roi_pct` | float \| None | the son's own surrogate guess at draw time; a candidate "predictor under test" option |

Reader: `simulate.load_simulations(sims_file)` (whole file) or `simulate.load_recent_simulations(sims_file, limit)` (tail, cached). The harness uses `load_simulations` once per son at startup and holds the parsed list in memory (≤ ~500 k rows total across 6 sons at current sizes; acceptable for a one-shot offline tool — see §5).

### 2.4 What is NOT time-reconstructable — stated for the record

`data/sons/son_*/checkpoint.json` is a **single "now" snapshot**, not a time series. Its `best` list is the current top-N by `ranking.rank()`; its `verified_roi_pct` / `verified_at` / `composite_score` fields reflect the latest state. There is **no** stored history of "what the `best` list looked like at round R" or "at time T".

Therefore **"the ranker's top-N at time T" must be reconstructed, not looked up:**

> Take every `SimulationRecord` across all 6 sons with `simulated_at < T`. That is the candidate set visible at T. Feed it through the ranker-under-test. Take the top-N. Nothing from `checkpoint.json` participates.

The checkpoint's `best` list *may* be read once, at the end, purely to sanity-check that the harness's reconstruction of "top-N as of `max(simulated_at)`" broadly matches the live checkpoint (a large divergence means the reconstruction logic is wrong). That check is advisory, not a gate.

### 2.5 Time-index reconciliation summary

| Store | Index | Role in harness |
|---|---|---|
| `price_history.jsonl` | `ts` (wall clock) | prices at T and T+Δ; the ground-truth movement |
| `simulations.jsonl` | `simulated_at` (wall clock, per row) | candidate corpus; filter `< T` |
| `round_history.jsonl` | `round` (+ `at` wall clock) | window bounds + descriptive context only |
| `checkpoint.json` | none (a "now" snapshot) | not used for reconstruction; one advisory end-check |

---

## 3. Evaluation loop

### 3.1 Cut times

`T_k` is a sequence of epoch timestamps. Default construction:

- `T_0 = min(price_history.ts)` shifted forward by a **warm-up margin** `WARMUP_DAYS = 14` (before that, too few skins have any price history to re-price anything — see §6).
- `T_last = max(price_history.ts) − Δ` (need a full look-forward window after the last cut).
- Between them, one cut every `STEP_DAYS = 7`. Config: `--step-days`, `--warmup-days`, `--first`, `--last` (ISO date or epoch).
- `Δ` (look-forward horizon) default `HORIZON_DAYS = 7`. Config: `--horizon-days`. The harness may run several Δ in one invocation (`--horizon-days 7,14,30`) and report each as its own column block.

Typical run: ~10–25 cut times over the available history.

### 3.2 Per-cut procedure

For each `T_k`:

1. **Build the visible candidate set.** All `SimulationRecord`s with `simulated_at < T_k`, across all sons. De-duplicate by `sim_blacklist.signature_of(record)` (`sim_blacklist.py:38-54` — the from/to-rarity + StatTrak + input-unit multiset content hash), keeping the row with the **latest** `simulated_at` before `T_k` (matches `sim_loop._consider_best`'s "keep the freshest simulation of a composition" intent). Drop rows where `low_volume_warning` is `True` for the strict variant (config `--keep-low-volume` to include them).

2. **Re-price at `T_k`.** For every distinct `market_hash_name` in the candidate set's `input_mix` + `output_distribution`, take the price-history observation **at or before `T_k`** (last snapshot with `ts <= T_k`), using `lowest_price` then `median_price`. If a name has **no** snapshot before `T_k`, that candidate is **unpriceable at T_k** → excluded from this cut with reason `no_price_at_T` (counted and reported; this is the sparsity tax, §6).

3. **Recompute `expected_roi_pct` at `T_k`** from those re-priced units using the exact same math already in `simulate.simulate()` / `oddsmath`:
   - `cost = Σ unit_price(T_k) · count` over input units;
   - `expected_revenue = oddsmath.expected_revenue_full_odds(revenue_weighted, total_prob)` where `revenue_weighted = [(output_price(T_k), prob_pct)]` and `total_prob = Σ prob_pct`;
   - `after_fee = expected_revenue · (1 − tradeup.STEAM_SELL_FEE)`;
   - `roi_at_T = (after_fee − cost) / cost · 100`.
   This reuses `oddsmath` directly — the harness must not re-derive fee/odds math (same rule as `simulate.py`'s own docstring).

4. **Score with the ranker-under-test.** Build `ranking.rank()`-shaped entry dicts (the plain JSON shape `ranking` already consumes: `expected_roi_pct` ← `roi_at_T`, `cost`, `input_mix`, `output_distribution`, `confidence_score`, `cost_basis_source`, `son`, `stattrak`, plus `verified_roi_pct` only if `verified_at < T_k`). Call the ranker (see §4 for which). Take the **top-N** (`N` default `20`, config `--top-n`).

5. **Look forward to `T_k + Δ`.** Re-price every top-N candidate's units at `T_k + Δ` (last snapshot with `ts <= T_k + Δ`). Recompute `roi_at_T_plus_delta` with the identical §3.3 math. A top-N candidate whose units are not all priceable at `T_k + Δ` is scored as `realised = NaN` and excluded pairwise from the metrics that need it (reported as `realised_missing`).

6. **Random-N baseline.** From the same *priceable-at-both-T-and-T+Δ* pool at this cut, draw `N` candidates uniformly at random, seeded (`--seed`, default `0`, cut index mixed in), `R_BASELINE_DRAWS = 200` times; average the realised metrics over the draws. This is the "did ranking beat luck?" control.

### 3.3 Labels

The realised outcome of a candidate at a cut is `roi_at_T_plus_delta` (§3.2 step 5) — the formula ROI recomputed from **prices that actually obtained Δ later**. This is the label for precision@N and realised-ROI.

Optional stronger label (`--label verified`): for candidates that carry `verified_roi_pct` with `verified_at` in `(T_k, T_k + Δ]`, use that Stage-2 cross-market number instead of the recomputed formula ROI. It is closer to truth (independent Skinport/Buff163/CSFloat evidence, `verify_queue._summarize`) but available for only a small fraction of rows, so it is off by default and, when on, the harness reports coverage (`n_labelled_verified / n_top_n`).

`+EV` threshold for precision@N is `roi_at_T_plus_delta > EV_THRESHOLD_PCT`, default `0.0` (config `--ev-threshold`), matching `verify_queue.ROI_THRESHOLD_PCT` and `verified_low_roi.VERIFIED_ROI_FLOOR_PCT`.

---

## 4. What it compares (config flags)

Each run selects a **ranker-under-test** and a **candidate-scorer**; the harness holds everything else fixed and diffs two runs.

### 4.1 M1 — UCB acquisition on/off

`--m1-beta FLOAT` (default `0.0`). The candidate corpus is fixed history, so M1 cannot change *which* candidates exist — but it changes the *score used to order them* when a surrogate std is available. When `--m1-beta > 0`, the harness scores each candidate as `mean + beta · std` where `(mean, std)` come from `sim_learning.predict_expected_roi(cand, son_model_dir, son_sims_file, with_std=True)` (the real M1 seam: `sim_learning.py:587`, `random_trade._acquisition_score`, `random_trade.generate_batch(acquisition_beta=)`). Reconstruction of the `random_trade.Candidate` from a stored row reuses `sim_loop._entry_to_candidate(entry, idx)` (`sim_loop.py:308`) verbatim. `beta` sweep supported: `--m1-beta 0,0.5,1.0,2.0` (matching `agents.acquisition_beta`'s `[0.0, 2.0]` range).

Caveat surfaced in the report: the models on disk are trained on *later* data than most cut times (training runs on a second machine — see `MEMORY.md` → `ml-training-runs-on-a-second-pc`), so M1's backtest is **look-ahead-biased in the model's favour**. The harness prints this warning whenever `--m1-beta > 0` and does not treat an M1 win as conclusive — it is a directional signal only.

### 4.2 M3 — formula-error correction on/off

`--m3 {off,on}` (default `off`). When `on`, after step §3.2.3 the harness computes `calibrated_roi_pct = roi_at_T − predicted_formula_error` using the M3 calibration model (design: `docs/superpowers/specs/2026-08-29-formula-error-calibration-design.md`), and the ranker orders on `calibrated_roi_pct` instead of `roi_at_T` (mirrors the `effective_roi` preference `verified → calibrated → expected` from that spec). Requires a trained M3 model artifact path (`--m3-model PATH`); absent → hard error, not a silent fallback.

### 4.3 M6 — feature sets

`--features {m6,pre_m6,custom:a,b,c}` (default `m6`). Only meaningful together with `--scorer surrogate` (§4.5). Selects which `sim_learning.FEATURE_NAMES` vector the reconstructed candidate is scored with:
- `m6` — the current 9: `rarity_rank, n_legs, n_distinct_wears, wear_spread, filler_fraction, stattrak, collection_mean_expected_roi, output_input_price_ratio, n_target_outputs` (`sim_learning.py:166-172`).
- `pre_m6` — the historical 7 (drop `output_input_price_ratio`, `n_target_outputs`), scored with a model retrained on that subset (`--pre-m6-model PATH`).
- `custom:...` — an explicit ordered subset, for ablation.

Because M6 already landed (`sim_learning` commit `4db5a88`), the practical use is the *reverse* comparison: does re-ranking history with the pre-M6 feature model do measurably worse than with M6?

### 4.4 Ranking weight variants

`--weights roi=0.60,profit=0.15,liquidity=0.10,confidence=0.10,discovery=0.05` overrides `ranking.BASE_WEIGHTS` (`ranking.py:41`) for the run. Also `--roi-squash-center` / `--roi-squash-scale` / `--profit-squash-ref` override the S1 absolute-squash constants (`ranking.py:63-65`). The harness monkey-patches these module constants for the duration of one run (documented, single-process, no concurrency) rather than threading a config object through `ranking.rank()` — keeps `src/ranking.py` untouched.

### 4.5 Scorer selection (orthogonal to ranker)

`--scorer {formula,surrogate,ensemble,naive_trend}`:
- `formula` (default) — order on `roi_at_T` (the recomputed formula ROI). No model needed.
- `surrogate` — order on `sim_learning.predict_expected_roi(...)` for the discovering son's model.
- `ensemble` — order on the median of all 6 trained sons' `predict_expected_roi` (the M7 signal, `sim_loop._attach_ensemble_predictions`).
- `naive_trend` — order on `price_history.predict_next_price()`'s extrapolated output value minus current cost; the crude baseline the review named, included so every model-based scorer has a dumb reference.

### 4.6 Run identity & diffing

Every run writes `tools/backtest/out/<utc-timestamp>-<shorthash-of-config>.json` with the full config, per-cut rows, and aggregate metrics. `tools/backtest_harness.py --diff RUN_A RUN_B` prints a side-by-side of the aggregates with a sign on each delta. CI-friendly exit code: `--diff ... --require "precision_at_n>=baseline"` exits non-zero if the assertion fails.

---

## 5. Runtime budget

**Hard target: < 5 minutes wall-clock, single process, no network, from committed data only.**

Cost breakdown and how it stays bounded:

| Phase | Cost driver | Bound |
|---|---|---|
| Parse `simulations.jsonl` ×6 | ~500 k rows total at current size | one-shot, streamed via `simulate.load_simulations`; ~20–40 s; parsed list held in RAM (~200–300 MB — acceptable for a manual tool, matches the `_TAIL_CACHE` note in `simulate.py:646`) |
| Parse `price_history.jsonl` | ~64 k lines / ~10 MB (measured, `tools/measure_history_index.py`) | one-shot; build an in-memory `{(name,currency): sorted list of (ts, low, med)}` index once; ~1–2 s |
| Per-cut re-pricing | `#candidates_visible × #distinct_names` | bisect into the pre-sorted per-name list — O(log n) per lookup; the dominant term is total distinct `market_hash_name`s (~thousands), not candidates |
| Per-cut ranking (`--scorer formula`) | `ranking.rank()` over visible set | pure Python arithmetic; ~0.5–2 s per cut × ~20 cuts |
| Per-cut ranking (`--scorer surrogate/ensemble`) | `sim_learning.predict_expected_roi` per candidate | the expensive path — cap it: only score the **top `SURROGATE_PRESCREEN = 500`** candidates by formula ROI (mirrors `_attach_ensemble_predictions`'s `ENSEMBLE_ATTACH_TOP_N` prescreen), reuse one `collection_roi_lookup(sims_file)` per son per cut, cap native threads via `ml_common._thread_limit(1)` |
| Random-N baseline | 200 draws × N × 2 re-prices | re-prices are memoised per (name, cut); draws are just index sampling |

Enforcement: the harness prints elapsed time per phase (same `phase_seconds` habit as `sim_learning.TrainResult`) and `--time-budget-seconds 300` aborts with a partial report if exceeded. `--scorer formula` (the default) must comfortably fit the budget on the full history; the model-based scorers fit it because of the prescreen.

No `joblib.load` beyond the per-son model files already on disk. No `sklearn` import unless a model-based scorer/label is selected (lazy, same convention as `learning.py` / `sim_learning.py`).

---

## 6. Honest limits

These go in the spec, in the tool's `--help`, and printed at the top of every report. They are the reason this is a *directional instrument*, not a P&L statement.

1. **`price_history.jsonl` is sparse and non-retroactive.** A candidate is only scoreable if *every* input and output unit has a snapshot before `T_k` and another before `T_k + Δ`. At early cut times most candidates fail this and are dropped (`no_price_at_T`). The report shows `n_scoreable / n_visible` per cut; a cut below `MIN_SCOREABLE = 50` is flagged `thin` and excluded from the headline aggregate (still shown in the per-cut table).

2. **Thin early cut times.** The `WARMUP_DAYS = 14` margin is a heuristic, not a guarantee. The first two or three usable cuts typically rest on a few hundred skins that happened to be scanned early — not a representative sample of the search space. Weight them lightly by eye.

3. **This measures ranking quality, not fill risk.** `price_history` records Steam's *listed* `lowest_price` / `median_price`, not a filled trade. The harness's "realised ROI" assumes you bought every input at the listed low and sold the output at the listed price minus the Steam fee. Real execution has slippage, partial fills, listing delays, and the output-side "no Steam sale evidence" trap that `simulate._price(allow_thirdparty=False)` guards against live but that this replay cannot see. A candidate that scores +40% realised here could still be untradeable.

4. **Look-ahead bias in the model-based scorers.** The `sim_roi_model*.joblib` files were trained on data that mostly postdates the cut times (training runs on a second machine). `--scorer surrogate/ensemble` and `--m1-beta > 0` therefore flatter the model. Treat a model-based win as "worth a real forward test", never as proof.

5. **Formula-ROI-as-label is circular for `--scorer formula`.** When both the ranker and the label are the recomputed formula ROI (just at different times), the harness is really measuring *price-movement predictability of the formula*, which is legitimate but narrower than "did we pick good trades". `--label verified` breaks the circularity where coverage allows; otherwise the Spearman/precision numbers should be read as "formula ROI persistence", and that framing is printed.

6. **De-dup by composition signature can hide churn.** Keeping only the freshest pre-`T_k` simulation of a composition matches production, but if a composition was re-simulated many times with swinging prices, the harness sees only one point and the realised movement may look calmer than the operator actually experienced.

7. **Currency and StatTrak coverage.** Everything is `currency == 1`. StatTrak candidates are a `~15%` minority (`random_trade.STATTRAK_RATE`) and their price history is thinner still; per-stratum metrics for StatTrak will often be `thin`.

---

## 7. N3 — stratified candidate generation (folded here per controller ruling)

**What N3 is.** Today `random_trade.random_candidate()` samples the search space with structural biases but no stratification: the rarity pair is drawn uniformly from `random_trade._ADJACENT_RARITY_PAIRS`; the primary collection uniformly from the eligible set; filler-collection count via `FILLER_COUNT_DECAY ** k`; skin mix via `SKIN_MIX_DECAY`; per-unit wear uniform (or skewed by `wear_bias`). Nothing deliberately balances coverage *across* rarity tiers, collections, or wear brackets — a rarity pair with 40 eligible collections is explored 40× as often as one with a single collection, and cheap high-volume collections dominate simply because they have more input skins.

**Stratified `random_trade` candidate generation** = draw candidates to fill a target quota per stratum instead of by raw uniform/decay weight. Concretely, a `--stratified` mode for `random_candidate` / `generate_batch` that:
- partitions the draw budget across strata defined by `(from_rarity tier, primary_collection bucket, dominant input wear band)` — tier from `data.RARITY_CHAIN`, wear band from `oddsmath.WEAR_ORDER`;
- within a stratum, keeps the existing `SKIN_MIX_DECAY` / `FILLER_COUNT_DECAY` / `warm_names` biases unchanged;
- guarantees a floor of draws for thinly-populated strata (rare rarity pairs, small collections) that pure-uniform starves.

**Why it rides in this spec and does not ship blind.** N3's *only* measurable payoff is "the guided search finds good trade-ups it currently misses in under-sampled strata". That is exactly what this harness measures — precision@N and realised-ROI-of-top-N over a *reconstructed* candidate corpus. Shipping N3 as a standalone change would alter what `simulations.jsonl` accumulates going forward with no way to check, before weeks of new data, whether the redistribution helped or just diluted the budget into unprofitable corners.

**How the harness A/Bs it.** N3 cannot be evaluated purely on frozen history (the historical corpus was generated by the *non*-stratified sampler), so the harness gains a **generation A/B mode** distinct from the replay mode of §3:

- `--stratified {off,on}` plus `--generate N` switches the harness from "replay stored `SimulationRecord`s" to "generate `N` fresh candidates now via `random_trade.generate_batch(stratified=...)`, price every unit from the *latest* `price_history` snapshot, run them through `simulate`-equivalent math, then rank".
- It runs both arms (`off` and `on`) against the **same** latest-price snapshot and the **same** ranker, then compares:
  - **stratum coverage** — Gini / entropy of the candidate count per `(rarity tier, collection, wear band)` stratum (expected: `on` is flatter, by construction — this is the sanity check, not the result);
  - **top-N realised quality** — because there is no future in "latest snapshot", this arm reuses the walk-forward loop with the newest feasible `T_k` (`max(price_history.ts) − Δ`) and a single cut: generate at `T_k`, look forward Δ, score. Small sample, wide error bars, reported as such;
  - **priceable fraction** — `n_priceable / N` per arm (stratified draws may land on thinner skins more often; if `on` tanks this, that is a finding against N3).
- Output: a two-row table (`stratified=off` / `stratified=on`) with `coverage_entropy`, `n_priceable`, `precision@N`, `realised_roi_topN`, `realised_roi_random_N`, and a one-line verdict (`stratified improves coverage AND does not hurt realised quality` / `stratified improves coverage BUT realised quality drops K pp` / `inconclusive — sample too thin`).

The N3 implementation itself (the `stratified=` kwarg on `random_trade.random_candidate` / `generate_batch`, and its wiring in `sim_loop.run_round`) is **out of scope for this spec** — it belongs in the follow-on `writing-plans` cycle, gated on this A/B mode showing a coverage win with no realised-quality regression.

---

## 8. File / module plan

```
tools/
  backtest_harness.py        CLI entry: arg parsing, run/diff/generate modes, budget guard, report
  backtest/
    __init__.py
    corpus.py                load + de-dup simulations.jsonl across sons; simulated_at filter
    prices.py                price_history index: {(name,ccy): [(ts,low,med)]}; at-or-before lookup
    reprice.py               recompute cost/expected_revenue/roi at a timestamp via oddsmath (no new math)
    rankers.py               ranker-under-test wiring: formula / surrogate / ensemble / naive_trend / weight overrides
    metrics.py               precision@N, realised-ROI-of-top-N, random-N baseline, Spearman
    generate.py              N3 A/B mode: generate_batch(stratified=) both arms, coverage + quality
    report.py                printed table + JSON artifact under out/; --diff renderer
    out/                      .gitignored run artifacts
```

- Reuses, never reimplements: `simulate.load_simulations`, `price_history.get_history` / `predict_next_price`, `sim_blacklist.signature_of`, `sim_loop._entry_to_candidate`, `ranking.rank` / `ranking.effective_roi` / `ranking.BASE_WEIGHTS`, `oddsmath.*`, `tradeup.STEAM_SELL_FEE`, `sim_learning.predict_expected_roi` / `collection_roi_lookup` / `get_model_status`, `data.load_skins` / `data.collections_index`, `ml_common._thread_limit`.
- No `src/` change. Weight/constant overrides are done by documented monkey-patch of `ranking` module constants for the run's duration (single process, no concurrency).
- No new dependency. `numpy` only if a model-based scorer/label is selected (lazy import). Spearman via a ~10-line rank-correlation over stdlib, or `numpy.corrcoef` on ranks when numpy is already loaded.

## 9. Deliverable of the follow-on plan

`tools/backtest_harness.py` + `tools/backtest/` implementing §3–§7, with:
- a `tools/check_backtest.py` assertion script (synthetic 3-candidate corpus + hand-built price series → known precision@N / Spearman) in the repo's existing `python tools/<name>.py → OK/raise` style;
- one committed sample run artifact + its printed report in the plan's final step, so a reviewer sees real numbers on the real (stale-but-shape-representative) data;
- `tools/backtest/out/` added to `.gitignore`.

---

## Self-review (placeholder scan)

Ran a scan for `TBD`, `TODO`, `FIXME`, `add error handling`, `figure out`, `somehow`, `etc.` used as a hand-wave, and bare "write tests":
- No `TBD` / `TODO` / `FIXME`.
- Every config flag has a concrete default and a named module constant.
- Every data-source claim is cited to a verified file + line range (`price_history.py:49-56`, `simulate.py:200-267`, `sim_loop.py:1221-1240`, `sim_blacklist.py:38-54`, `ranking.py:41/63-65/82-90`, `sim_learning.py:166-172/587`).
- The six required brief points (§2 data sources & time index; §3 evaluation loop over `T_k`, Δ=7d; §3.3+§4 metrics: precision@N, realised-ROI vs random-N, Spearman; §4 M1/M3/M6/weights flags; §5 <5 min offline no network; §6 honest limits) are each covered concretely, plus the §7 N3 fold-in with its `--stratified` A/B rather than a blind ship.
- "etc." appears once (§9, "price trend charts, medium-term ROI progress, etc.") quoting the existing `sim_loop.py` docstring context, not as a design gap.
