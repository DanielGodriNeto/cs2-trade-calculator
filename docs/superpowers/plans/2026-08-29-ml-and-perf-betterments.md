# ML & Performance Betterments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use **superpowers:subagent-driven-development** to execute this plan — a fresh implementer subagent per task, a task review after each, a whole-branch review at the end. Steps use checkbox (`- [ ]`) syntax.
>
> **COMMIT DISCIPLINE (explicit user requirement):** commit after **every** micro-step that changes a file — not once per task. Each `- [ ] **Step N**` that writes code or a test ends with its own `git commit`. A task with 6 code steps produces ~6 commits. The implementer prompt must repeat this.

**Goal:** Land 11 self-contained ML-quality and performance improvements to the simulation-population search and ranking, and produce design specs for 3 larger items (backtest harness, formula-error calibration model, real-outcome backfill importer).

**Architecture:** Small, independently-reviewable changes to the existing `src/` modules. Every change that can alter a model prediction or a displayed ranking number carries a before/after check on the real `data/sons/son_*/simulations.jsonl`. No new dependency. The two-model firewall (`sim_learning` trained on simulated EV vs `learning` trained on real logged outcomes — never merged) stays intact.

**Tech Stack:** Python 3.14 stdlib, scikit-learn 1.9 (`HistGradientBoostingRegressor`, `Ridge` via `ml_common.fit_best_model`), numpy 2.5, joblib. No test framework in this repo — checks are `tools/*.py` assertion scripts (`python tools/<name>.py` → prints `OK` / raises) plus `python profile_retrain.py` (existing, read-only) for timing.

**Spec:** none — this plan implements findings M1–M8, P-1–P-4, N1–N3, S1–S2 from the 2026-08-29 code review (see `MEMORY.md` → `telemetry-hud-redesign`, and the review transcript). Related but out of scope: backlog items P2/P6/P7/P8 in `ml-ux-review-2026-08` (still need their own specs); the retrain-speed items #1/#3/#4 in `docs/superpowers/plans/2026-08-29-sim-retrain-speedups.md`.

## Global Constraints

- **Commit after every micro-step** that changes a file (see the note above). Prefix: `perf(sim):`, `fix(rank):`, `feat(ml):`, `docs(spec):` as fits.
- **No new dependency.** stdlib + already-present sklearn/numpy/joblib only.
- **No silent model-output change.** Any task touching `sim_learning` / `ml_common` fitting ends with a step that runs `tools/mae_parity.py` (created in Task 4) on ≥3 sons with non-empty local `simulations.jsonl`: the new CV MAE must be **≤ 1.05× the pre-change MAE** on every checked son, or the task stops and reports (a real regression is a ruling for the controller, not a merge).
- **Two-model firewall intact.** `sim_learning` and `learning` never share training data. Task 13 (formula-error calibration) trains on **external-market cross-check labels** (`verify_queue` `verified_roi_pct` vs the formula's `expected_roi_pct`), not user trades — the design task must state this explicitly and the reviewer must confirm it.
- **The local `data/sons/son_*/simulations.jsonl` are stale** (training runs on another machine — `ml-training-runs-on-a-second-pc`) but shape-representative: fine for parity, timing, and API-shape checks; never quote their absolute accuracy numbers as current.
- **Preserve return-shape contracts.** `predict_expected_roi` returns `float | None` consumed positionally by `random_trade` and `ranking._ml_agreement_bonus`. New signals go through **new optional kwargs / new functions**, never by widening an existing return (this codebase has a documented tuple/return-shape-drift bug class — see `sim_learning.py:462-468`).
- **`ranking.rank()` composite-score scale may change (Task 1) — that is intended and user-visible.** Every other task must leave `composite_score` ordering unchanged unless its own text says otherwise.

---

## File Structure

| File | Tasks | What changes |
|---|---|---|
| `src/ranking.py` | 1, 10 | P-1 per-call reliability cache; S1 fixed (absolute) ROI/profit normalization; M7 ensemble-agreement trust bonus. |
| `src/random_trade.py` | 2, 6, 8 | S2 softmax underflow floor; M8 named StatTrak-rate constant + per-call override; M1 acquisition-score plumb-through. |
| `src/sim_learning.py` | 3, 4, 5, 7, 8, 9 | P-4 `compress=`; M4 recency `sample_weight`; M5 `monotonic_cst`; M2 quantile head + `predict_expected_roi(..., with_std=)`; M6 price-ratio features; M1 uses M2's std. |
| `src/learning.py` | 3 | P-4 `compress=`. |
| `src/ml_common.py` | 5 | thread `monotonic_cst` through `_candidates` / `fit_best_model`. |
| `src/sim_loop.py` | 8, 10 | M1 pass acquisition beta into `generate_batch`; M10 collect the 6 sons' predictions for `ranking`. |
| `src/agents.py` | 8 | M1 map `exploration_rate` → acquisition beta (helper only, no new profile field). |
| `tools/mae_parity.py` | 4 (create) | CV-MAE before/after harness, reused by 5, 7, 9. |
| `tools/*.py` | per task | one assertion script per non-trivial change. |
| `docs/superpowers/specs/2026-08-29-backtest-harness-design.md` | 12 (create) | N2 design. |
| `docs/superpowers/specs/2026-08-29-formula-error-calibration-design.md` | 13 (create) | M3 design. |
| `docs/superpowers/specs/2026-08-29-outcome-backfill-importer-design.md` | 14 (create) | N1 design. |

**Task order matters:** 4 before 5/7/9 (they use `tools/mae_parity.py`); 7 before 8 (M1 needs M2's std); 1 is independent; 12–14 are design-only and can run any time but are listed last.

---

## Task 1: `ranking.py` — per-call reliability cache (P-1) + fixed normalization (S1)

**Files:**
- Modify: `src/ranking.py` — `rank()` (`:200-257`), add module constants + two helpers.
- Create: `tools/check_ranking.py`

**Interfaces:**
- Consumes: `market_reliability.any_unit_excluded(input_mix, output_distribution, currency, stattrak, cache=…)` — the `cache` kwarg already exists (`market_reliability.py:329`).
- Produces: `rank(entries)` returns the same dict shape (`composite_score`, `score_breakdown`, `effective_roi_pct`, `profit_amount` added) — but `composite_score` is now on a **fixed 0-100 scale**, comparable across calls. `score_breakdown["roi"]` / `["profit"]` are now absolute squashes, not min-max-relative.

- [ ] **Step 1: `tools/check_ranking.py` — pin current behaviour, then the new invariant**

```python
"""Ranking invariants: (1) a fixed entry's composite_score is identical whether ranked alone
or in a big list (S1); (2) rank() makes one reliability cache, not one lookup per entry (P-1)."""
from src import ranking

base = {"son": 1, "cost": 100.0, "expected_roi_pct": 40.0, "input_mix": [], "output_distribution": [],
        "confidence_score": 80.0, "stattrak": False}
solo = ranking.rank([dict(base)])[0]["composite_score"]
padded = ranking.rank([dict(base)] + [dict(base, expected_roi_pct=r) for r in (5, 500, -20, 300)])
mine = next(e for e in padded if e["expected_roi_pct"] == 40.0)["composite_score"]
assert abs(solo - mine) < 1e-9, f"S1 broken: {solo} vs {mine} (score moved when neighbours changed)"
print(f"OK  fixed-scale composite stable: {solo:.3f}")
```

Run: `python tools/check_ranking.py` → **AssertionError** (today's min-max normalization makes `solo != mine`).

- [ ] **Step 2: commit the check**

```bash
git add tools/check_ranking.py && git commit -m "test(rank): pin fixed-scale + cache invariants (failing)"
```

- [ ] **Step 3: P-1 — thread one cache through `rank()`**

In `src/ranking.py:rank()`, replace the two `market_reliability_mod.any_unit_excluded(...)` calls (`:224-226` filter, and note `_has_unpriced_output` at `:233` is unrelated — leave it) with a shared memo:

```python
    _rel_cache: dict = {}
    entries = [e for e in entries if not market_reliability_mod.any_unit_excluded(
        e.get("input_mix") or [], e.get("output_distribution") or [],
        currency=1, stattrak=e.get("stattrak", False), cache=_rel_cache)]
```

(`any_unit_excluded`'s docstring at `market_reliability.py:340-347` explicitly names `ranking.py` as a hot loop that was missed when the cache was added elsewhere.)

- [ ] **Step 4: commit P-1**

```bash
git add src/ranking.py && git commit -m "perf(rank): reuse one market-reliability memo across all entries"
```

- [ ] **Step 5: S1 — fixed ROI/profit squash**

Add near the top of `src/ranking.py`:

```python
import math

ROI_SQUASH_CENTER = 25.0   # ROI% mapped to 0.5 by the sigmoid — a "solidly good" trade-up
ROI_SQUASH_SCALE = 25.0    # ROI% change for a ~0.23 score change near the centre
PROFIT_SQUASH_REF = 500.0  # absolute profit ($) mapped to ~0.76; log1p keeps small profits legible

def _squash_roi(roi_pct: float) -> float:
    return 1.0 / (1.0 + math.exp(-(roi_pct - ROI_SQUASH_CENTER) / ROI_SQUASH_SCALE))

def _squash_profit(profit: float) -> float:
    if profit <= 0:
        return 0.0
    return min(1.0, math.log1p(profit) / math.log1p(PROFIT_SQUASH_REF))
```

In `score_breakdown(entry, roi_lo, roi_hi, profit_lo, profit_hi)` (`:190`) replace the `_normalize(...)` calls for `"roi"` and `"profit"` with `_squash_roi(_effective_roi(entry))` and `_squash_profit(_profit_amount(entry))`. Leave `liquidity`/`confidence`/`discovery` unchanged (already 0-1). In `rank()`, the `roi_lo/roi_hi/profit_lo/profit_hi` args become unused — keep the parameter list of `score_breakdown` intact (other call sites / tests) but stop computing `rois`/`profits`/`min`/`max` in `rank()`; pass `0, 0, 0, 0`. Update the `score_breakdown` docstring and the `rank()` `CAVEAT` block (`:212-218`) to say the scale is now absolute.

- [ ] **Step 6: verify + eyeball**

```bash
python tools/check_ranking.py   # -> OK
python -c "from src import sim_loop; import json; \
  r=sim_loop.list_best_simulations(); \
  print('\n'.join(f\"{e['composite_score']:6.2f}  roi={e.get('effective_roi_pct',0):7.1f}  {e.get('primary_collection','?')}\" for e in r[:20]))"
```

Compare the printed top-20 against the same command on `git stash`'d old code (or `git show HEAD~2`): ordering may shift slightly; a wildly different set is a ruling for the controller.

- [ ] **Step 7: commit S1**

```bash
git add src/ranking.py tools/check_ranking.py
git commit -m "fix(rank): absolute ROI/profit squash so composite_score is comparable across queries"
```

---

## Task 2: `random_trade.py` — softmax underflow floor (S2)

**Files:**
- Modify: `src/random_trade.py` — `_softmax_weighted_sample()` (`:297-329`).
- Create: `tools/check_softmax.py`

**Interfaces:** unchanged — `_softmax_weighted_sample(rng, scored, k, temperature) -> list`.

- [ ] **Step 1: failing check**

```python
"""At low temperature with a wide score spread, selection must still be weighted-random,
not 'take the first k in list order' (the exp() underflow branch)."""
import random
from src.random_trade import _softmax_weighted_sample

rng = random.Random(0)
scored = [(0.0, "top")] + [(-80.0, f"tail{i}") for i in range(200)]   # exp(-80/0.2) underflows to 0
picks = [tuple(_softmax_weighted_sample(random.Random(s), scored, 5, 0.2)) for s in range(60)]
tails = [p[1:] for p in picks]
assert len(set(tails)) > 1, "underflow: tail is deterministic list-order, not random"
assert all(p[0] == "top" for p in picks), "top score should still almost always be picked"
print(f"OK  {len(set(tails))} distinct tail draws over 60 seeds")
```

Run → **AssertionError**.

- [ ] **Step 2: commit check** — `git add tools/check_softmax.py && git commit -m "test(sim): pin softmax anti-underflow (failing)"`

- [ ] **Step 3: fix**

In `_softmax_weighted_sample`, the per-draw weight line is `weights = [math.exp((s - max_score) / temperature) for s, _ in pool]`. Add a positive floor so no candidate is ever exactly 0:

```python
        FLOOR = 1e-12
        weights = [max(FLOOR, math.exp((s - max_score) / temperature)) for s, _ in pool]
```

Keep the existing `if total <= 0` guard as a belt-and-braces fallback.

- [ ] **Step 4: verify + commit**

```bash
python tools/check_softmax.py   # -> OK
git add src/random_trade.py && git commit -m "fix(sim): floor softmax weights so low-temp tail stays weighted-random"
```

---

## Task 3: joblib compression (P-4)

**Files:** `src/sim_learning.py` (`joblib.dump` at `:475`), `src/learning.py` (`joblib.dump` at `:217`).

- [ ] **Step 1:** in both files change `joblib.dump(model, <path>)` → `joblib.dump(model, <path>, compress=3)`. Nothing else.

- [ ] **Step 2: verify round-trips**

```bash
python -c "import joblib,numpy as np; from sklearn.ensemble import HistGradientBoostingRegressor as H; \
m=H(max_iter=10).fit(np.random.rand(50,4),np.random.rand(50)); \
joblib.dump(m,'/tmp/m.joblib',compress=3); m2=joblib.load('/tmp/m.joblib'); \
print('OK', float(m2.predict(np.random.rand(1,4))[0]))"
```

- [ ] **Step 3: commit** — `git add src/sim_learning.py src/learning.py && git commit -m "perf(ml): compress persisted models (joblib compress=3)"`

---

## Task 4: recency weighting in the sim surrogate (M4) + `tools/mae_parity.py`

**Files:**
- Create: `tools/mae_parity.py` (reused by Tasks 5, 7, 9).
- Modify: `src/sim_learning.py` — `_train_locked()` sample-weight block (`:417-426`), add a module constant.

**Interfaces:**
- Produces: `RECENCY_HALFLIFE_ROWS = 750` constant; the `sample_weight` array passed to `fit_best_model` is multiplied element-wise by `0.5 ** (age_in_rows / RECENCY_HALFLIFE_ROWS)` where `age_in_rows` is `len(rows) - 1 - i` (0 = newest). No signature change.
- `tools/mae_parity.py`: `python tools/mae_parity.py [son_0 son_2 ...]` → for each son, prints `son_N  base_mae=…  new_mae=…  ratio=…` and exits non-zero if any `ratio > 1.05`. It works by calling `sim_learning.train(model_dir, sims_file)` on the current working tree and comparing its `TrainResult.validation_mae` to a baseline JSON it writes on first run (`tools/.mae_baseline.json`, git-ignored).

- [ ] **Step 1: build `tools/mae_parity.py`**

```python
"""CV-MAE parity gate for changes to sim_learning / ml_common fitting.

First run per son writes a baseline; later runs compare. Delete tools/.mae_baseline.json
to re-baseline (do that on `main` BEFORE starting a model-touching task).
"""
import json, sys
from pathlib import Path
from src import sim_learning as SL

BASELINE = Path("tools/.mae_baseline.json")
sons = sys.argv[1:] or [f"son_{i}" for i in range(6)]
base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
worst = 1.0
for s in sons:
    d = Path("data/sons") / s
    if not (d / "simulations.jsonl").exists():
        print(f"{s}  (no local simulations.jsonl — skipped)"); continue
    r = SL.train(d / "model", d / "simulations.jsonl")
    if not r.trained:
        print(f"{s}  (train refused: {r.reason})"); continue
    mae = float(r.validation_mae)
    if s not in base:
        base[s] = mae
        print(f"{s}  baseline set: {mae:.4f}")
    else:
        ratio = mae / base[s]
        worst = max(worst, ratio)
        print(f"{s}  base={base[s]:.4f}  new={mae:.4f}  ratio={ratio:.3f}{'  <-- REGRESSION' if ratio > 1.05 else ''}")
BASELINE.write_text(json.dumps(base, indent=2))
sys.exit(1 if worst > 1.05 else 0)
```

Add `tools/.mae_baseline.json` to `.gitignore`.

- [ ] **Step 2: commit the harness** — `git add tools/mae_parity.py .gitignore && git commit -m "test(ml): CV-MAE parity gate harness"`

- [ ] **Step 3: baseline on unmodified code**

```bash
rm -f tools/.mae_baseline.json
python tools/mae_parity.py    # prints "baseline set" per son; exit 0
```

- [ ] **Step 4: implement recency weighting**

In `_train_locked`, after the `weights` list is built and before `sample_weight = np.array(weights, dtype=float)` (`:425`):

```python
    # Recency: a row from weeks ago is a weaker signal about today's market than yesterday's.
    # Multiplies the reliability weight (does not replace it). age 0 = newest row.
    n = len(rows)
    weights = [w * (0.5 ** ((n - 1 - i) / RECENCY_HALFLIFE_ROWS)) for i, w in enumerate(weights)]
```

Add `RECENCY_HALFLIFE_ROWS = 750` next to `MAX_ROWS_FOR_ANALYSIS` with a one-line comment. Extend the `sample_weight_breakdown` / `mean_sample_weight` meta note if needed (they still work — the mean just drops).

- [ ] **Step 5: parity gate**

```bash
python tools/mae_parity.py son_0 son_2 son_5    # must exit 0 (ratio <= 1.05 each)
```

If it fails, raise `RECENCY_HALFLIFE_ROWS` (weaker decay) and re-run; if it still fails at 2000, stop and report — recency decay is hurting this data and the finding doesn't hold.

- [ ] **Step 6: commit**

```bash
git add src/sim_learning.py && git commit -m "feat(ml): exponential recency weighting in sim surrogate training"
```

---

## Task 5: monotonic constraints on the boosted tree (M5)

**Files:** `src/ml_common.py` (`_candidates` `:142`, `fit_best_model` `:225`), `src/sim_learning.py` (the `fit_best_model` call `:445`).

**Interfaces:**
- `_candidates(..., hgb_monotonic_cst: list | None = None)` — when given, passed as `HistGradientBoostingRegressor(monotonic_cst=…)`. Only affects the HGB candidate; RF/Ridge ignore it.
- `fit_best_model(..., hgb_monotonic_cst: list | None = None)` — forwarded to `_candidates`. Default `None` = today's behaviour (learning.py untouched).
- `sim_learning` passes a vector aligned to `FEATURE_NAMES`: `+1` (increasing) for `collection_mean_expected_roi`, `0` for the rest.

- [ ] **Step 1: `tools/check_monotonic.py`**

```python
"""With monotonic_cst on collection_mean_expected_roi, raising only that feature must never
lower the prediction."""
import numpy as np
from src import ml_common
FN = ["rarity_rank","n_legs","n_distinct_wears","wear_spread","filler_fraction","stattrak","collection_mean_expected_roi"]
rng = np.random.default_rng(0)
X = rng.random((300, 7)); y = X[:, 6] * 40 + rng.normal(0, 5, 300)      # target roughly rises with feat 6
cst = [0,0,0,0,0,0,1]
res = ml_common.fit_best_model(X, y, FN, candidates={"hist_gradient_boosting"}, hgb_monotonic_cst=cst)
grid = np.tile(X[:1], (50, 1)); grid[:, 6] = np.linspace(0, 1, 50)
p = res.model.predict(grid)
assert np.all(np.diff(p) >= -1e-9), f"not monotonic: {p[:5]} ... {p[-5:]}"
print("OK  prediction is monotone non-decreasing in collection_mean_expected_roi")
```

- [ ] **Step 2: commit check** — `git commit -m "test(ml): pin monotonic constraint on the surrogate (failing)"`

- [ ] **Step 3: thread `hgb_monotonic_cst`** through `_candidates` (add to `hgb_kwargs` when not None) and `fit_best_model` (new kwarg, forwarded). Do not touch the RF/Ridge branches.

- [ ] **Step 4: commit ml_common** — `git commit -m "feat(ml): optional monotonic_cst passthrough in fit_best_model"`

- [ ] **Step 5:** in `sim_learning._train_locked`, add the constant `MONOTONIC_CST = [0, 0, 0, 0, 0, 0, 1]` (comment: aligned to `FEATURE_NAMES`; only the target-encoded collection mean is constrained) and pass `hgb_monotonic_cst=MONOTONIC_CST` in the `fit_best_model` call.

- [ ] **Step 6: check + parity**

```bash
python tools/check_monotonic.py                 # -> OK
python tools/mae_parity.py son_0 son_2 son_5     # exit 0
```

- [ ] **Step 7: commit** — `git add src/ml_common.py src/sim_learning.py tools/check_monotonic.py && git commit -m "feat(ml): constrain surrogate monotone-increasing in collection mean ROI"`

---

## Task 6: named StatTrak-rate constant + per-call override (M8)

**Files:** `src/random_trade.py` — `random_candidate` (`:237`, the `stattrak = rng.random() < 0.15` line `:257`), `generate_batch` (`:332`).

**Interfaces:**
- `STATTRAK_RATE = 0.15` module constant (replaces the inline literal).
- `random_candidate(..., stattrak_rate: float | None = None)` and `generate_batch(..., stattrak_rate: float | None = None)` — `None` → `STATTRAK_RATE`. `generate_batch` forwards to `random_candidate`.
- `sim_loop.run_round` is **not** changed in this task (no per-son value yet) — the plumb-through is so a future adaptive/agent value has a seam. Note that in the commit body.

- [ ] **Step 1:** replace the literal with `STATTRAK_RATE`; add the two `stattrak_rate` kwargs; use `stattrak_rate if stattrak_rate is not None else STATTRAK_RATE` at the draw.

- [ ] **Step 2: check**

```python
"""stattrak_rate=1.0 -> every candidate StatTrak; 0.0 -> none."""
from src import random_trade, data
idx = data.load_index()
import random
on = [random_trade.random_candidate(idx, random.Random(i), stattrak_rate=1.0) for i in range(30)]
off = [random_trade.random_candidate(idx, random.Random(i), stattrak_rate=0.0) for i in range(30)]
assert all(c is None or c.stattrak for c in on) and all(c is None or not c.stattrak for c in off)
print("OK  stattrak_rate override honoured")
```

(match `data.load_index` to the real helper name — check `src/data.py`.)

- [ ] **Step 3: commit** — `git add src/random_trade.py tools/check_stattrak_rate.py && git commit -m "feat(sim): STATTRAK_RATE constant + per-call override seam"`

---

## Task 7: per-candidate uncertainty — quantile head (M2)

**Files:** `src/sim_learning.py` — `_train_locked` (persist a second model), `predict_expected_roi` (`:526`), module constants.

**Interfaces:**
- New persisted file per son: `sim_roi_model_hi.joblib` — a `HistGradientBoostingRegressor(loss="quantile", quantile=STD_QUANTILE)` fit on the **same X, y, sample_weight** as the winner, single fit (no CV — it's an auxiliary band, not the selected model). `STD_QUANTILE = 0.8413` (≈ mean + 1σ for a normal).
- `predict_expected_roi(candidate, model_dir, sims_file, collection_roi_lookup=None, with_std: bool = False)`:
  - `with_std=False` (default) → unchanged: `float | None`.
  - `with_std=True` → `tuple[float, float] | None` = `(mean_pred, std_est)` where `std_est = max(0.0, hi_pred - mean_pred)`; if the hi model file is missing (older son), `std_est = 0.0`.
- `get_model_status` gains nothing (the hi model has no meta of its own).
- The in-process cache `_loaded_models` gets a parallel `_loaded_hi_models` keyed the same way.

- [ ] **Step 1: `tools/check_quantile_head.py`**

```python
"""with_std returns (mean, std>=0); std is 0 when the hi model is absent; mean matches the
plain call exactly."""
from pathlib import Path
from src import sim_learning as SL, random_trade as RT, data
son = Path("data/sons/son_0"); md, sf = son/"model", son/"simulations.jsonl"
idx = data.load_index(); import random; c = RT.random_candidate(idx, random.Random(3))
plain = SL.predict_expected_roi(c, md, sf)
pair = SL.predict_expected_roi(c, md, sf, with_std=True)
if plain is None:
    assert pair is None; print("OK  (son_0 untrained locally — None/None)")
else:
    m, s = pair
    assert abs(m - plain) < 1e-9 and s >= 0.0, (m, plain, s)
    print(f"OK  mean={m:.2f}  std={s:.2f}")
```

- [ ] **Step 2: commit check**

- [ ] **Step 3: fit + persist the hi model** in `_train_locked`, right after `joblib.dump(model, model_dir / MODEL_FILENAME, compress=3)`:

```python
        from sklearn.ensemble import HistGradientBoostingRegressor
        hi = HistGradientBoostingRegressor(loss="quantile", quantile=STD_QUANTILE,
                                            random_state=42, max_depth=4,
                                            min_samples_leaf=max(3, min(20, len(rows) // 8)),
                                            max_bins=(FAST_MAX_BINS if len(rows) >= FAST_MAX_BINS_MIN_ROWS else 255))
        with ml_common._thread_limit(FAST_MAX_THREADS):
            hi.fit(X, y, sample_weight=sample_weight)
        joblib.dump(hi, model_dir / "sim_roi_model_hi.joblib", compress=3)
```

Add `STD_QUANTILE = 0.8413` and `HI_MODEL_FILENAME = "sim_roi_model_hi.joblib"` constants. On `forget()` / reset, also `unlink` the hi file.

- [ ] **Step 4: extend `predict_expected_roi`** — add the `with_std` param; after computing `mean_pred` (the current `return float(...)` line), when `with_std`, load the hi model (parallel cache, same mtime guard), predict, and `return (mean_pred, max(0.0, hi_pred - mean_pred))`. Missing hi file → `return (mean_pred, 0.0)`. Keep the default path byte-for-byte identical.

- [ ] **Step 5: check + one retrain**

```bash
python tools/check_quantile_head.py
python -c "from pathlib import Path; from src import sim_learning as SL; \
  d=Path('data/sons/son_0'); print(SL.train(d/'model', d/'simulations.jsonl'))"
ls -la data/sons/son_0/model/sim_roi_model_hi.joblib   # exists after the retrain
python tools/mae_parity.py son_0 son_2 son_5            # winner model unaffected -> exit 0
```

- [ ] **Step 6: commit** — `git add src/sim_learning.py tools/check_quantile_head.py && git commit -m "feat(ml): quantile head for per-candidate uncertainty (predict_expected_roi with_std)"`

---

## Task 8: UCB acquisition in the guided search (M1)

**Files:** `src/agents.py` (helper), `src/sim_loop.py` (`run_round` score_fn `:933`, `generate_batch` call `:972`), `src/random_trade.py` (`generate_batch` `:332`, `_softmax_weighted_sample` caller path).

**Interfaces:**
- `agents.acquisition_beta(profile) -> float` — `profile.exploration_rate` mapped linearly to `[0.0, 2.0]` (exploration_rate 0 → pure exploit, 1 → beta 2.0). Pure helper, no new profile field.
- `random_trade.generate_batch(..., acquisition_beta: float = 0.0)` — when `> 0` and `score_fn` returns `(mean, std)` tuples, the score used for `_softmax_weighted_sample` is `mean + acquisition_beta * std`; when `score_fn` returns bare floats or `acquisition_beta == 0`, behaviour is exactly today's.
- `sim_loop.run_round` builds `score_fn` to call `predict_expected_roi(c, …, with_std=True)` and passes `acquisition_beta=agents.acquisition_beta(profile)`.

- [ ] **Step 1: `tools/check_acquisition.py`**

```python
"""With beta>0, a high-variance candidate outranks an equal-mean low-variance one."""
import random
from src.random_trade import _softmax_weighted_sample

# emulate generate_batch's scoring: score = mean + beta*std
def score(mean, std, beta): return mean + beta * std
a = score(10, 8, 1.0); b = score(10, 1, 1.0)
assert a > b, (a, b)
picks = _softmax_weighted_sample(random.Random(0), [(a, "risky"), (b, "safe")], 1, 0.3)
print("OK  UCB favours the uncertain candidate:", picks)
```

- [ ] **Step 2: commit check**

- [ ] **Step 3: `agents.acquisition_beta`** — add the helper + a one-line doc tying it to `exploration_rate`.

- [ ] **Step 4: `generate_batch`** — add `acquisition_beta: float = 0.0`. In the scoring block (`:360`), after `scored = [(score_fn(c), c) for c in candidates]` (or the batched form from the other plan), normalise each score: if it's a 2-tuple `(m, s)`, use `m + acquisition_beta * s`; if a float, use it; if `None`, `-inf` (as today). Keep the `all None` fallback.

- [ ] **Step 5: `run_round`** — change the `score_fn` (`:933-937`) to request `with_std=True` and pass `acquisition_beta=agents_mod.acquisition_beta(profile)` into `generate_batch`. The `_novelty` branch (`:938`) is untouched (`acquisition_beta` stays 0 there).

- [ ] **Step 6: integration smoke**

```bash
python tools/check_acquisition.py
python -c "from src import sim_loop; s=sim_loop.SimulationLoopState.load('son_0'); sim_loop.run_round(s)"
```

Expected: round completes, `simulations.jsonl` grows, no exception. (Match `SimulationLoopState.load` to the real constructor — read `sim_loop.py` around `:569`.)

- [ ] **Step 7: commit** — `git add src/agents.py src/random_trade.py src/sim_loop.py tools/check_acquisition.py && git commit -m "feat(ml): UCB acquisition (mean + beta*std) in guided candidate selection"`

---

## Task 9: pre-simulation price-ratio features (M6)

**Files:** `src/sim_learning.py` — `FEATURE_NAMES` (`:161`), `_feature_vector` (`:219`), `_featurize_record` (`:243`), `predict_expected_roi` (`:570`).

**Interfaces:**
- `FEATURE_NAMES` gains `output_input_price_ratio` and `n_target_outputs` (appended — order matters for persisted models; `get_model_status`'s feature-set check `:519` auto-invalidates old models, so this self-heals on next retrain).
- `_feature_vector(..., output_input_price_ratio: float, n_target_outputs: float)` — both new args. `output_input_price_ratio` = `mean(warm-cache output medians) / mean(warm-cache input medians)`, `nan` when either side has no warm price. `n_target_outputs` = count of distinct skins at the target rarity across all legs (odds dilution) — always known.
- Training rows (`_featurize_record`): compute the ratio from `r.input_mix` / `r.output_distribution` prices already on the `SimulationRecord` (read `simulate.SimulationRecord` — the priced trial carries per-unit `price`). `n_target_outputs` from the record's output distribution length.
- Prediction (`predict_expected_roi`): inputs/outputs aren't priced yet — use `steam_prices` warm-cache lookups (`steam_prices.market_hash_name` + the cache getter `random_trade` already imports); `nan` when cold. `n_target_outputs` from `sum(len(leg.outputs) for leg in candidate.legs)`.

- [ ] **Step 1: `tools/check_price_features.py`** — assert `_feature_vector` returns the right width (`len(FEATURE_NAMES)`), that a known input/output price pair yields the expected ratio, and that all-cold inputs yield `nan` for the ratio but a real `n_target_outputs`.

- [ ] **Step 2: commit check**

- [ ] **Step 3:** implement the two features across the four sites. Keep `cost` still excluded (its comment `:163-184` stands — the ratio is the cheap proxy that finding M6 proposes instead).

- [ ] **Step 4: parity + retrain**

```bash
python tools/check_price_features.py
python -c "from pathlib import Path; from src import sim_learning as SL; d=Path('data/sons/son_0'); print(SL.train(d/'model', d/'simulations.jsonl').validation_mae)"
rm -f tools/.mae_baseline.json && python tools/mae_parity.py son_0 son_2 son_5   # re-baseline: new feature set, so this establishes the new normal
```

Note in the commit body: this task **re-baselines** `mae_parity` (the feature set changed on purpose); record the pre/post MAE numbers from `git show HEAD~1` era in the commit message for the record.

- [ ] **Step 5: commit** — `git add src/sim_learning.py tools/check_price_features.py && git commit -m "feat(ml): add output/input price ratio + n_target_outputs surrogate features"`

---

## Task 10: 6-son ensemble trust signal in ranking (M7)

**Files:** `src/sim_loop.py` (`list_best_simulations` `:286` / wherever `rank()` is called), `src/ranking.py` (`_confidence_score` / a new bonus).

**Interfaces:**
- `sim_loop`: before calling `ranking.rank(out)`, for each entry compute `ensemble_preds` = `[predict_expected_roi(reconstructed_candidate, son_i_model_dir, son_i_sims_file) for i in range(N_SONS) if that model is trained]` — **only for entries that carry enough structure to reconstruct a `random_trade.Candidate`-shaped input** (they do: `input_mix` + rarities + `stattrak`). Attach `entry["ensemble_roi_median"]` and `entry["ensemble_roi_spread"]` (p90−p10 of the predictions), or omit both when <3 models available.
- `ranking.py`: a new `_ensemble_agreement_bonus(entry) -> float` (0–`ENSEMBLE_MAX_BONUS = 6.0`), folded into `_confidence_score` exactly like `_ml_agreement_bonus` (`:114-141`): full bonus when `ensemble_roi_spread` is small **and** `ensemble_roi_median` is within `ML_AGREEMENT_RESIDUAL_CAP_PP` of `effective_roi`; zero when spread is wide or the median disagrees. Scaled by `ML_AGREEMENT_UNVERIFIED_SCALE` when unverified, same as the sibling.

- [ ] **Step 1:** read `sim_loop.list_best_simulations` and `list_all_opportunities` (`:286`, `:420`) — both call `rank()`. Factor the ensemble-prediction attach into one helper used by both.

- [ ] **Step 2: `tools/check_ensemble_bonus.py`** — a hand-built entry with tight agreeing `ensemble_*` gets a bonus near `ENSEMBLE_MAX_BONUS`; one with a wide spread gets ~0; a `confidence_score` never exceeds 100.

- [ ] **Step 3: commit check**

- [ ] **Step 4:** implement the `sim_loop` attach helper. Reconstructing a `Candidate` from an entry dict: write `_entry_to_candidate(entry)` in `sim_loop` (it already has all the pieces — `from_rarity`, `to_rarity`, `stattrak`, `input_mix` with collection/wear/count). Guard every `predict_expected_roi` call in a `try/except` returning `None` (a stale per-son model must never break ranking).

- [ ] **Step 5:** implement `ranking._ensemble_agreement_bonus` + fold into `_confidence_score`. Update that function's docstring.

- [ ] **Step 6: verify**

```bash
python tools/check_ensemble_bonus.py
python -c "from src import sim_loop; r=sim_loop.list_best_simulations(); \
  print([ (round(e['composite_score'],1), e.get('ensemble_roi_spread')) for e in r[:10] ])"
```

- [ ] **Step 7: commit** — `git add src/sim_loop.py src/ranking.py tools/check_ensemble_bonus.py && git commit -m "feat(ml): 6-son ensemble agreement as a ranking trust signal"`

---

## Task 11: price-history index — measure, then decide (P-3)

**Files:** create `tools/measure_history_index.py`; **maybe** modify `src/market_reliability.py` (`_history_index_cache`, `:90-137`).

This task is **measure-first**. Its deliverable is a decision recorded in the ledger, and code only if the measurement justifies it.

- [ ] **Step 1:** write `tools/measure_history_index.py` — patches `market_reliability` to count how often `_history_for` triggers a full rebuild of the 11 MB `price_history.jsonl` index vs a cache hit, over a simulated busy stretch: call `compute_state` on 200 distinct skins interleaved with 20 `price_history.record(...)` appends, print `rebuilds=… hits=… total_parse_MB=…`.

- [ ] **Step 2: commit the tool**, run it, record the numbers in the SDD ledger as `Task 11: measured — rebuilds=X hits=Y over Z ops`.

- [ ] **Step 3 (conditional):**
  - **If rebuilds are rare** (the `_HISTORY_STAT_INTERVAL_SECONDS` throttle already blunts it — likely): ledger `Task 11: Ruling: no change — index rebuild fires ~X/min, not worth an incremental-update rewrite`. Task done, no code commit beyond the tool.
  - **If rebuilds are frequent:** implement an append-aware update — on rebuild, if the file only grew (same inode, size increased, old prefix unchanged), parse only the appended tail and merge into the existing index instead of re-reading from byte 0. Add `tools/check_history_index_incremental.py` asserting the incremental path yields the identical index to a full rebuild. Commit.

- [ ] **Step 4: commit** whatever resulted (tool only, or tool + fix), message reflecting the ruling.

---

## Task 12 (design only): Walk-forward backtest harness spec (N2)

**Deliverable:** `docs/superpowers/specs/2026-08-29-backtest-harness-design.md`, committed. **No implementation in this plan** — a follow-on `writing-plans` cycle builds it from this spec.

The spec MUST cover, concretely:
1. **Data sources & time index.** `data/price_history.jsonl` (timestamped snapshots) and per-son `data/sons/son_*/round_history.jsonl` (round-indexed, not wall-clock). State plainly what is and isn't time-reconstructable — checkpoints are not time-indexed, so "the ranker's top-N at time T" has to be rebuilt from `simulations.jsonl` rows whose timestamps precede T.
2. **The evaluation loop.** For a sequence of cut times `T_k`: build the candidate set visible at `T_k`, score it with the ranker-under-test, take top-N, then look forward to `T_k + Δ` (Δ = 7d default) and compute realised outcome from `price_history` movements of those exact skins/wears.
3. **Metrics.** precision@N (fraction of top-N that were actually +EV at T+Δ), realised-ROI-of-top-N vs realised-ROI-of-a-random-N baseline, rank correlation (Spearman) between predicted and realised.
4. **What it's used to compare.** M1 (UCB on/off), M3 (formula-error correction on/off), M6 (feature sets), ranking weight variants — each a config flag.
5. **Runtime budget.** must run offline from committed data in < 5 min; no network.
6. **Honest limits.** `price_history` is sparse and non-retroactive; early cut times have thin data; this measures *ranking quality*, not fill risk.

- [ ] **Step 1:** write the spec covering all six points. **Step 2:** self-review for placeholders. **Step 3:** `git add … && git commit -m "docs(spec): walk-forward backtest harness design (N2)"`.

---

## Task 13 (design only): Formula-error calibration model spec (M3)

**Deliverable:** `docs/superpowers/specs/2026-08-29-formula-error-calibration-design.md`, committed.

MUST cover:
1. **Label source and the firewall.** Training pairs are `(features, verified_roi_pct − expected_roi_pct)` where `verified_roi_pct` comes from `verify_queue` Stage-2 external-market cross-checks (Skinport/Buff163/CSFloat) — **not** `data/outcomes.jsonl` (user trades). State why this does not breach the "never train on our own numbers / never merge the two models" rule, and require the code reviewer to confirm it. Data on disk today: `data/verified_low_roi.json` (396 KB), `data/mispriced_skins.json` (660 KB), and `verified_roi_pct` written back onto checkpoint entries.
2. **Feature set.** collection, output-price tier, liquidity/dead-skin score, `n_target_outputs`, filler_fraction, `output_input_price_ratio` (from M6).
3. **Where the correction applies.** A new field `calibrated_roi_pct = expected_roi_pct − predicted_formula_error`, surfaced beside the other three ROI numbers (formula / ML / verified) — the ranker's `effective_roi` prefers `verified` → `calibrated` → `expected`.
4. **Cold start.** dormant below N pairs (mirror `learning.MIN_TRAINING_SAMPLES`), no correction applied until then.
5. **Guardrail.** cap the correction magnitude (e.g. ±50 pp) so a bad fit can't invert a trade-up's sign.

- [ ] **Step 1:** write it. **Step 2:** self-review. **Step 3:** commit `docs(spec): formula-error calibration model design (M3)`.

---

## Task 14 (design only): Real-outcome backfill importer spec (N1)

**Deliverable:** `docs/superpowers/specs/2026-08-29-outcome-backfill-importer-design.md`, committed.

MUST cover:
1. **Problem.** `data/outcomes.jsonl` is empty → `learning.py` (the real ROI model, the app's stated "compare with real prices" goal) is fully dormant. Two-stage logging (backlog P4) reduces friction going forward; this is about seeding history.
2. **Input.** Steam's own market-history / inventory-history export (describe the actual format the user can obtain), plus a manual CSV fallback (`collection, from_rarity, to_rarity, stattrak, input skins+wears+prices, output skin+wear, sale price, date`).
3. **Mapping to `outcomes.Outcome`.** field-by-field; which fields can't be recovered from a Steam export (predicted_roi_pct at the time — set `None`) and which can.
4. **Safeguards.** dedupe against existing rows; mark backfilled rows (`source: "backfill"`) so their weight can be tuned separately; never overwrite a hand-logged outcome.
5. **UI touchpoint.** a "Import history" control on the (redesigned) Connections or Overview screen — cross-reference `2026-08-29-telemetry-hud-redesign.md`.

- [ ] **Step 1:** write it. **Step 2:** self-review. **Step 3:** commit `docs(spec): real-outcome backfill importer design (N1)`.

---

## Self-Review

**1. Coverage** — M1→T8, M2→T7, M3→T13(spec), M4→T4, M5→T5, M6→T9, M7→T10, M8→T6, P-1→T1, P-3→T11, P-4→T3, N1→T14(spec), N2→T12(spec), N3→(folded into T12's spec as a stratified-generation option; not its own task — flag if the controller wants it separate), S1→T1, S2→T2. N3 is the one item without a dedicated task — **Ruling for the controller:** stratified candidate generation is small but its value is only measurable via N2's harness, so it rides with N2's spec rather than shipping blind.

**2. Placeholder scan** — the "match the real helper name" instructions (T6 `data.load_index`, T8 `SimulationLoopState.load`, T10 `_entry_to_candidate` fields) are concrete lookups against named files, not vague "figure it out". Tasks 12–14 are explicitly design-only with enumerated required sections. No "TBD" / "add error handling" / bare "write tests".

**3. Type consistency** — `predict_expected_roi(..., with_std: bool=False) -> float | None | tuple[float,float]` defined in T7, consumed in T8 and T10 (T10 uses the default `with_std=False` path). `acquisition_beta: float` flows `agents.acquisition_beta` → `sim_loop.run_round` → `generate_batch` in T8. `tools/mae_parity.py` created in T4, invoked in T4/T5/T7/T9. `hgb_monotonic_cst` flows `sim_learning` → `fit_best_model` → `_candidates` in T5. `FEATURE_NAMES` width change in T9 is self-healing via the existing `get_model_status` feature-set guard (`sim_learning.py:519`).

---

## Execution notes for the controller

- **Batch T1–T3 and T6** are small and single-file; T2, T3, T6 especially can be one dispatch each on the cheapest model (plan text carries the full code). T1 is two related changes in one file — one dispatch, review as a unit.
- **T4, T5, T7, T8, T9, T10** touch `sim_learning` / `ml_common` / `ranking` fitting and need the standard model + careful review (the parity gate is the objective backstop, but read the diffs).
- **T8** is the highest-risk implementation task (3 files, changes what the search selects). If its review loop stalls, the fallback ruling is `acquisition_beta` defaults to `0.0` everywhere (pure today's-behaviour) and M1 ships dormant behind the seam, like M8.
- **T11** may produce zero code — that's a valid outcome, ledger the ruling.
- **T12–T14** produce specs; after this plan, run `writing-plans` on each to get their implementation plans.
