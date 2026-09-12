# Simulation Retrain Speedups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut simulation-population retrain wall-clock and per-round scorer overhead without changing what any model predicts — by (1) moving sim-model retrains onto one coalescing worker thread, (2) skipping the CV model-selection bake-off on most retrains, and (3) batching the per-candidate ML scoring call.

**Architecture:** Three independent changes to the existing `src/sim_*` modules. Each keeps model outputs bit-identical (verified by a parity check) and only changes *when / where / how often* work runs. No new dependency. No behaviour change visible in the dashboard beyond faster numbers.

**Tech Stack:** Python 3.14 stdlib (`threading.Thread`, `queue.Queue`), scikit-learn 1.9 (`HistGradientBoostingRegressor` via `ml_common.fit_best_model`), numpy 2.5.

**Spec:** none — this plan implements items #1, #3, #4 from the project-memory note `training-speed-findings` (2026-08-29 profiling). That note is the design rationale; it travels with this plan. Items #2, #5, #6 and backlog items P2/P4/P6/P7/P8 are **out of scope** — see "Deferred" at the end.

## Global Constraints

- **No model-output change.** Every task ends with a parity check: the same rows through the new path must produce the same predictions / CV MAE / OOF interval as `main` (tolerance: exact for #1/#4, `cv_mae` within 1e-9 for #3's HGB-only path vs an HGB-only run on `main`).
- **No new dependency.** stdlib `threading` / `queue` only. LightGBM is explicitly rejected in the memory note.
- **Existing constants keep their names and values** unless a task says otherwise: `RETRAIN_TRIGGER_DELTA = 25` (`src/sim_learning.py:42`), `MIN_TRAINING_SAMPLES = 20` (`:40`), `TRAIN_MAX_CONCURRENCY = 2` (`:158`), `FAST_MAX_THREADS = 1` (`:157`).
- **No test framework in this repo.** Checks are small committed assertion scripts under `tools/` run with `python tools/<name>.py` (prints `OK` / raises), plus `python profile_retrain.py` (existing, read-only) for before/after timing, plus a one-off parity script per task.
- **The local `data/sons/son_*/simulations.jsonl` are stale** (training runs on another machine — memory `ml-training-runs-on-a-second-pc`) but shape-representative; fine for parity and timing checks, not for absolute-accuracy claims.
- **Commit after every task**, `perf(sim):` prefix.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/sim_retrain_queue.py` | **create** | One daemon worker thread; `enqueue(son_id)` coalesces duplicate pending son ids; drains by calling a supplied `train_one(son_id)`. |
| `src/sim_loop.py` | modify `run_round` (~`:1124`) and loop startup (~`:1201`) | Replace the inline `sim_learning.train()` call at the retrain trigger with `sim_retrain_queue.enqueue(state.son_id)`; start/stop the queue worker with the population. |
| `src/sim_learning.py` | modify `train` / `_train_locked` (~`:340`, `:378`); add `predict_expected_roi_batch` | Add a `full_selection: bool` path (HGB-only refit between periodic bake-offs); add a vectorised predict. |
| `src/ml_common.py` | no change | `fit_best_model(..., include=...)` already supports a single-candidate run (`:210-211`). |
| `src/random_trade.py` | modify `generate_batch` (`:332-360`) | Call `score_fn` once with the whole candidate list when it advertises batch support. |
| `tools/parity_predict_batch.py` | create | Assert `predict_expected_roi_batch` == N× `predict_expected_roi`. |
| `tools/check_retrain_queue.py` | create | Assert the queue coalesces and drains in order. |

---

## Task 1: Batched round scorer (memory item #4)

Smallest, fully isolated, biggest per-round win (~2.5 s/round across the population per the memory note). Speeds up *rounds* (⇒ more sims/hour), not retrains.

**Files:**
- Modify: `src/sim_learning.py` — add `predict_expected_roi_batch` next to `predict_expected_roi`.
- Modify: `src/random_trade.py:332-360` — `generate_batch`.
- Modify: `src/sim_loop.py:933-938` — `score_fn` construction in `run_round`.
- Create: `tools/parity_predict_batch.py`

**Interfaces:**
- Consumes: the existing `predict_expected_roi(candidate, model_dir, sims_file, model_status=…)` signature (`src/sim_loop.py:934` shows the call shape).
- Produces:
  - `predict_expected_roi_batch(candidates: list, model_dir, sims_file, model_status=None) -> list[float]` — one `model.predict(X_batch)` call; falls back to per-candidate default (same value `predict_expected_roi` returns) for an unloadable/absent model. Order-preserving, `len(out) == len(candidates)`.
  - `generate_batch(..., score_fn=…, score_fn_batched: bool = False)` — when `score_fn_batched`, calls `scores = score_fn(candidates)` once instead of the `[(score_fn(c), c) for c in candidates]` loop at `:360`.

- [ ] **Step 1: Write the parity script (fails: function missing)**

`tools/parity_predict_batch.py`:

```python
"""Assert predict_expected_roi_batch matches N single predict_expected_roi calls."""
import sys
from pathlib import Path
from src import sim_learning as SL
from src import random_trade as RT
from src import data as data_mod

son0 = Path("data/sons/son_0")
model_dir, sims = son0 / "model", son0 / "simulations.jsonl"
idx = data_mod.load_index()
rng = __import__("random").Random(1)
cands = [RT.random_candidate(idx, rng=rng) for _ in range(200)]

single = [SL.predict_expected_roi(c, model_dir, sims) for c in cands]
batch = SL.predict_expected_roi_batch(cands, model_dir, sims)
assert len(batch) == len(single), (len(batch), len(single))
worst = max(abs(a - b) for a, b in zip(single, batch))
assert worst < 1e-9, f"max abs diff {worst}"
print(f"OK  200 candidates, max abs diff {worst:.2e}")
```

Run: `python tools/parity_predict_batch.py` → **AttributeError: predict_expected_roi_batch**.

(If `random_trade.random_candidate` / `data.load_index` names differ, read the top of `src/random_trade.py` and match them — the point is 200 real candidates.)

- [ ] **Step 2: Implement `predict_expected_roi_batch`**

Read `predict_expected_roi` in `src/sim_learning.py` first. It featurises one candidate, loads the model + collection-ROI lookup, and returns `model.predict([x])[0]` (or a default). The batch version does the same load once and one `predict`:

```python
def predict_expected_roi_batch(candidates, model_dir, sims_file, model_status=None):
    if not candidates:
        return []
    bundle = _load_model_bundle(model_dir)            # whatever predict_expected_roi uses
    if bundle is None:
        return [_DEFAULT_PREDICTION] * len(candidates)  # same constant predict_expected_roi returns
    roi_lookup = _collection_roi_lookup(sims_file)
    X = [_featurize_candidate(c, roi_lookup) for c in candidates]   # same featurizer predict uses
    return [float(v) for v in bundle.model.predict(X)]
```

Match the *exact* helper names `predict_expected_roi` uses (`_load_model_bundle`, `_featurize_candidate`, default constant — names may differ; mirror them). If `predict_expected_roi` applies any post-clip/round, apply the identical transform elementwise here.

- [ ] **Step 3: Run parity script → OK**

`python tools/parity_predict_batch.py` → `OK  200 candidates, max abs diff 0.00e+00` (or `< 1e-9`).

- [ ] **Step 4: Wire `generate_batch` to accept a batched scorer**

In `src/random_trade.py`, change the scoring block (`:358-360`):

```python
    if score_fn is None or keep_top_k is None:
        ...
    if score_fn_batched:
        scores = score_fn(candidates)
        scored = list(zip(scores, candidates))
    else:
        scored = [(score_fn(c), c) for c in candidates]
```

Add `score_fn_batched: bool = False` to the signature (`:332-334`).

- [ ] **Step 5: Use it in `sim_loop.run_round`**

At `src/sim_loop.py:933`, replace the per-candidate `score_fn` with a batched one and pass the flag to `generate_batch` (`:972-973`):

```python
        def score_fn(cs):
            return sim_learning_mod.predict_expected_roi_batch(
                cs, state.model_dir, state.sims_file, model_status=model_status)
        # _novelty path (`:938`) stays per-candidate: keep score_fn_batched=False when score_fn = _novelty
```

```python
    candidates = random_trade_mod.generate_batch(
        idx, batch_size, rng=state.rng, score_fn=score_fn, score_fn_batched=(score_fn is not _novelty),
        keep_top_k=keep_top_k, temperature=temperature, ...)
```

Keep the `_novelty` branch (`:938`) exactly as-is — it is not the ML model.

- [ ] **Step 6: Smoke one round + timing**

```bash
python -c "from src import sim_loop, data; s=sim_loop.SimulationLoopState.load('son_0'); sim_loop.run_round(s)"
```

Expected: completes without error, `data/sons/son_0/simulations.jsonl` gains a batch of rows. Optionally time a 5-round loop before/after on `main` — memory predicts ~0.4 s/son/round saved.

- [ ] **Step 7: Commit**

```bash
git add src/sim_learning.py src/random_trade.py src/sim_loop.py tools/parity_predict_batch.py
git commit -m "perf(sim): batch the per-round ML scorer (one model.predict per round)"
```

---

## Task 2: Winner-only refit between periodic bake-offs (memory item #3)

Memory: **2.4–2.9× faster** retrains, CV MAE and OOF p10/p90 **bit-identical** because Ridge/RF never beat HGB on any son. Run full model selection every 10th retrain (same cadence as permutation importance), HGB-only otherwise.

**Files:**
- Modify: `src/sim_learning.py` — `train` (`:340`), `_train_locked` (`:378`), the checkpoint dict init in `src/sim_loop.py:123` (add a counter).

**Interfaces:**
- Consumes: `ml_common.fit_best_model(X, y, feature_names, random_state, include=None, …)` — `include` filters candidates (`src/ml_common.py:210-211`); `include={"hist_gradient_boosting"}` runs HGB only.
- Produces: `train(model_dir, sims_file, *, full_selection: bool = True) -> TrainResult` — when `full_selection=False`, passes `include={"hist_gradient_boosting"}` to `fit_best_model`; `TrainResult` shape unchanged (`model_name` will always be `"hist_gradient_boosting"` on the fast path).

- [ ] **Step 1: Thread a `full_selection` flag through `train` → `_train_locked` → `fit_best_model`**

`train(...)` signature gains `*, full_selection: bool = True`. Pass it to `_train_locked`. At the `fit_best_model` call (`src/sim_learning.py:445`):

```python
    selection = ml_common.fit_best_model(
        X, y, FEATURE_NAMES, random_state=42,
        include=None if full_selection else {"hist_gradient_boosting"},
        max_threads=FAST_MAX_THREADS)
```

(Confirm `fit_best_model` accepts `include=` as a kwarg — `src/ml_common.py:225`. If the current `_train_locked` call doesn't already pass `include`, this adds it.)

- [ ] **Step 2: Add the bake-off counter to the son checkpoint**

`src/sim_loop.py:123` — add `"retrains_since_bakeoff": 0` to the checkpoint dict alongside `"n_simulated_at_last_train": 0`.

- [ ] **Step 3: Decide `full_selection` at the retrain trigger**

`src/sim_loop.py` retrain trigger (`~:1124-1139`). Replace the `sim_learning.train(...)` call:

```python
        n = state.checkpoint.get("retrains_since_bakeoff", 0)
        full = (n % 10 == 0)
        result = sim_learning_mod.train(state.model_dir, state.sims_file, full_selection=full)
        state.checkpoint["retrains_since_bakeoff"] = n + 1
```

(The permutation-importance recompute in `sim_learning` already runs "every 10th retrain" per its `:117-118` comment — align the bake-off to the same tick if that counter is reachable; otherwise the independent counter here is fine.)

- [ ] **Step 4: Parity check — HGB-only path vs an HGB-only run on `main`**

One-off script `tools/parity_winner_only.py`:

```python
"""full_selection=False must equal a main-branch fit_best_model(include={'hist_gradient_boosting'})."""
from pathlib import Path
from src import sim_learning as SL
son = Path("data/sons/son_0")
r_fast = SL.train(son / "model", son / "simulations.jsonl", full_selection=False)
r_full = SL.train(son / "model", son / "simulations.jsonl", full_selection=True)
print("fast :", r_fast.model_name, round(r_fast.cv_mae, 6))
print("full :", r_full.model_name, round(r_full.cv_mae, 6))
assert r_fast.model_name == "hist_gradient_boosting"
# On every son in the memory profiling, full also picks HGB, so cv_mae should match to ~1e-9:
assert abs(r_fast.cv_mae - r_full.cv_mae) < 1e-9, abs(r_fast.cv_mae - r_full.cv_mae)
print("OK  winner-only == full selection on son_0")
```

Run on 2–3 sons if their local `simulations.jsonl` is non-empty. If `full` picks something other than HGB on any son, **stop** — the memory's premise doesn't hold for that son; report it rather than shipping.

- [ ] **Step 5: Timing**

`python profile_retrain.py` before, then run a few retrains through the fast path, compare the `work` (not `wait`) number. Memory expects the HGB-only `work` at ~0.23–0.26 s vs ~0.58–0.74 s.

- [ ] **Step 6: Commit**

```bash
git add src/sim_learning.py src/sim_loop.py tools/parity_winner_only.py
git commit -m "perf(sim): HGB-only refit between every-10th CV bake-off"
```

---

## Task 3: Serial coalescing retrain queue (memory item #1)

Memory: removes the last 2-wide HGB thrash and stops retrains stealing cores from the sim loops that generate the data. A wave of 6 coalesced retrains ≈ 2–4 s total vs 6× 8–18 s. Behaviour identical, only *when/where* it runs.

**Files:**
- Create: `src/sim_retrain_queue.py`
- Modify: `src/sim_loop.py` — retrain trigger (`~:1124`), population start (`~:1201-1211`), and any explicit "train now" path.
- Create: `tools/check_retrain_queue.py`

**Interfaces:**
- Consumes: a `train_one(son_id: str) -> None` callback (the caller wires it to `sim_learning.train` for that son, reusing Task 2's `full_selection` decision).
- Produces (`src/sim_retrain_queue.py`):
  - `start(train_one: Callable[[str], None]) -> None` — idempotent; spawns one daemon thread.
  - `enqueue(son_id: str) -> None` — no-op if `son_id` already pending or in-flight (coalesce); never blocks the caller.
  - `stop(drain: bool = True) -> None` — stop the worker; if `drain`, finish the queue first.
  - `pending() -> list[str]` — snapshot, for the dashboard / tests.

- [ ] **Step 1: Write the queue check (fails: module missing)**

`tools/check_retrain_queue.py`:

```python
"""Queue coalesces duplicate son ids and drains each once, in FIFO order."""
import time
from src import sim_retrain_queue as Q

seen = []
def train_one(son_id):
    time.sleep(0.02)          # simulate work so coalescing has a window
    seen.append(son_id)

Q.start(train_one)
for sid in ["son_0", "son_1", "son_0", "son_0", "son_2", "son_1"]:
    Q.enqueue(sid)
Q.stop(drain=True)

assert seen == ["son_0", "son_1", "son_2"], seen     # each once, first-seen order
print("OK", seen)
```

Run: `python tools/check_retrain_queue.py` → **ModuleNotFoundError**.

- [ ] **Step 2: Implement `src/sim_retrain_queue.py`**

```python
"""Single-worker, duplicate-coalescing retrain queue for the 6 sim sons.

Sim loops enqueue a son id when its retrain trigger fires and never block. One daemon
thread drains them one at a time, so retrains no longer run 2-wide (TRAIN_MAX_CONCURRENCY)
and no longer steal cores from the sim loops that generate the training rows. A son already
waiting (or currently training) is not re-queued -- the next drain uses its latest data anyway.
"""
from __future__ import annotations
import threading
from collections import deque
from typing import Callable

_lock = threading.Lock()
_cv = threading.Condition(_lock)
_queue: deque[str] = deque()
_pending: set[str] = set()
_inflight: str | None = None
_worker: threading.Thread | None = None
_running = False
_train_one: Callable[[str], None] | None = None


def start(train_one: Callable[[str], None]) -> None:
    global _worker, _running, _train_one
    with _lock:
        _train_one = train_one
        if _running:
            return
        _running = True
        _worker = threading.Thread(target=_run, name="sim-retrain-queue", daemon=True)
        _worker.start()


def enqueue(son_id: str) -> None:
    with _cv:
        if son_id in _pending or son_id == _inflight:
            return
        _pending.add(son_id)
        _queue.append(son_id)
        _cv.notify()


def pending() -> list[str]:
    with _lock:
        return list(_queue)


def stop(drain: bool = True) -> None:
    global _running
    with _cv:
        _running = False
        if not drain:
            _queue.clear()
            _pending.clear()
        _cv.notify_all()
    if _worker:
        _worker.join(timeout=30)


def _run() -> None:
    global _inflight
    while True:
        with _cv:
            while _running and not _queue:
                _cv.wait()
            if not _running and not _queue:
                return
            son_id = _queue.popleft()
            _pending.discard(son_id)
            _inflight = son_id
        try:
            if _train_one:
                _train_one(son_id)
        finally:
            with _lock:
                _inflight = None
```

- [ ] **Step 3: Run the check → OK**

`python tools/check_retrain_queue.py` → `OK ['son_0', 'son_1', 'son_2']`.

- [ ] **Step 4: Swap the inline retrain for an enqueue**

In `src/sim_loop.py` at the retrain trigger (`~:1124`), replace the direct `sim_learning_mod.train(...)` + checkpoint write with:

```python
        sim_retrain_queue_mod.enqueue(state.son_id)
```

Move the `n_simulated_at_last_train` / `retrains_since_bakeoff` checkpoint bump **into** the `train_one` callback (Step 5) so it advances when the retrain actually happens, not when it's queued — otherwise a coalesced enqueue would skip rows. If `state.son_id` isn't a field, use whatever id key the checkpoint / `model_dir` path uses (e.g. `son_0`).

- [ ] **Step 5: Start the queue with the population; define `train_one`**

In `src/sim_loop.py` population startup (`~:1201`, `run_foreground` / the equivalent used by `web/server.py`):

```python
    from src import sim_retrain_queue as sim_retrain_queue_mod

    def _train_one(son_id: str) -> None:
        state = _states_by_id[son_id]                 # however the loop tracks its sons
        n = state.checkpoint.get("retrains_since_bakeoff", 0)
        sim_learning_mod.train(state.model_dir, state.sims_file, full_selection=(n % 10 == 0))
        with state.checkpoint_lock:
            state.checkpoint["n_simulated_at_last_train"] = _count_simulated(state)
            state.checkpoint["retrains_since_bakeoff"] = n + 1
            _save_checkpoint(state)

    sim_retrain_queue_mod.start(_train_one)
```

Wire `sim_retrain_queue_mod.stop(drain=True)` into the population's existing shutdown path (next to where the son threads are joined). The single "train now" button path (`POST /api/sim-population/<id>/train`) should also go through `enqueue` for consistency — or keep it synchronous if the UI expects an immediate result; note which in the commit.

- [ ] **Step 6: Integration smoke**

```bash
python -m web.server &
sleep 3
# let the sim loops run a few rounds, then:
curl -s localhost:5000/api/sim-population/status | python -m json.tool | grep -E "retrain|training" | head
kill %1
```

Expected: no exceptions in server output; each son's model meta `modified` timestamp still advances over a few minutes; `sim_retrain_queue.pending()` never grows unbounded.

- [ ] **Step 7: Timing**

`python profile_retrain.py` after a live run: the `wait` number should collapse (no more semaphore queueing — there's one worker) and total retrain wall-clock for a 6-son wave should be far below `6 × single`.

- [ ] **Step 8: Commit**

```bash
git add src/sim_retrain_queue.py src/sim_loop.py tools/check_retrain_queue.py
git commit -m "perf(sim): serial coalescing retrain queue (one worker, no 2-wide thrash)"
```

---

## Self-Review

**1. Coverage** — memory items #1 (Task 3), #3 (Task 2), #4 (Task 1) each have a task. #2/#5/#6 and P2/P4/P6/P7/P8 are explicitly deferred below.

**2. Placeholder scan** — the "match the exact helper names" instructions in Task 1 Step 2 and Task 3 Step 4/5 are deliberate ("read this function, mirror its names") because the precise private-helper names in `sim_learning.predict_expected_roi` and the son-id/checkpoint accessors in `sim_loop` must be read from the tree at implementation time — they are named anchors with a concrete lookup instruction, not vague "figure it out". No "TBD" / "add error handling" / bare "write tests".

**3. Type consistency** — `full_selection: bool` flows `train` → `_train_locked` → `fit_best_model(include=…)` identically in Tasks 2 and 3. `train_one(son_id: str)` is the queue's callback type in Task 3 Steps 1, 2, 5. `predict_expected_roi_batch(candidates, model_dir, sims_file, model_status=None) -> list[float]` is consistent between Task 1 Steps 2 and 5. `retrains_since_bakeoff` checkpoint key is written in Task 2 Step 2/3 and read in Task 3 Step 5.

---

## Deferred — needs its own brainstorm/spec or a code-reading pass first

These were in scope of "the other code changes" but are **not** turned into step-by-step tasks here, each for a concrete reason. Recommend one `brainstorming` → `writing-plans` cycle per item, in this order:

| Item | Source | Why it isn't planned here |
|---|---|---|
| **Novelty-triggered retrain** | speed #2 | The trivial half (raise `RETRAIN_TRIGGER_DELTA` to ~100–150) is a one-line change, but the memory's own caveat — "the novelty signal must not starve a genuinely-shifted son" — means the `{new collection}` / `{OOF residual band broken}` triggers need designing and a per-son adopt-vs-revert study. Small spec. |
| **Order-book-depth-aware cost** | backlog P2 | Architectural. `cost = lowest listing × 10` is wrong everywhere at once — it feeds the formula ROI, all 6 sons' surrogate targets, and the real-price comparison. Needs Steam order-histogram integration + a migration story for every ROI number already on disk. Its own spec. |
| **Two-stage outcome logging** | backlog P4 | Backend `update_outcome_sale` exists (`web/server.py:1036`) but "mark bought → snapshot input + 3rd-party prices → later record sale" needs a buy-time snapshot store and new UI. Best done as a follow-up to the telemetry-HUD redesign (it lands on CALC + a new OVW "pending sales" card). Fold into that plan's next iteration. |
| **Re-add `cost` feature to `sim_learning.FEATURE_NAMES`** | backlog P5 | The code comment at `src/sim_learning.py:161-182` shows re-adding `cost` naively makes MAE *worse* (7.93 vs 4.58) due to train/serve skew. Needs a cheap pre-simulation cost estimate (`n_legs × mean warm-cache unit price`) designed and validated first. Small spec. |
| **`adaptive.py` (1+1)-ES redesign** | backlog P6 | Explicitly measure-first: instrument adopt-vs-revert rate before touching the estimator. If it's at the noise floor the ES isn't learning and the fix is different. Investigation, then spec. |
| **Expand the 7-feature sim model** | backlog P7 | Feature engineering (`output/input price ratio`, `# distinct outputs at target rarity`, per-collection invalid rate, `market_reliability` aggregate). Each needs an importance study. Its own spec. |
| **Population diversity metric** | backlog P8 | Needs the metric defined (pairwise best-list overlap vs collection-coverage divergence) before it can be a task. Small spec. |
| **Stash salvage + reliability-log compaction wiring** | backlog P1 / memory `reliability-log-compaction` | Project memory is stale: `src/split_archive.py` is now tracked on `main`, but `market_reliability.compact_log()` / `restore_all()` and the `web/server.py main()` startup call are still not. `src/inventory.py` is still stash-only. Needs a real `git diff stash@{0} HEAD -- src/ web/ extension/` to see exactly what remains un-merged, then a small cherry-pick plan. The React SPA in the stash is now dead (the redesign is vanilla-in-place) — do **not** salvage `frontend/`. |
