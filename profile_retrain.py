"""Read-only diagnostic: where is each population son's retrain time actually going?

Run from the repo root:  python profile_retrain.py

Reads nothing but each son's already-persisted model metadata (data/sons/son_N/model/
sim_roi_model_meta.json) -- it trains nothing, writes nothing, and touches no model file, so
it is safe to run while the app is live. Exists because retrain slowness has repeatedly been
misattributed on this project: the phase that looks slow is usually not the phase doing the
work (see sim_learning.py's TRAIN_MAX_CONCURRENCY block comment for the history), and the
per-son number the dashboard shows INCLUDES time spent queued behind sibling sons.

The split that matters is wait vs work:
  wait  = await_train_slot, i.e. time queued behind other sons on the training semaphore.
          High wait means the machine is saturated, NOT that this son's own retrain is slow.
          Total wall time for a whole retrain cycle can improve while this number goes UP.
  work  = everything this son actually did itself. This is the number to optimize.
"""
import json
import os
import platform
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    src = ROOT / "src"
    print(f"machine   : {platform.processor()[:60]}")
    print(f"cores     : {os.cpu_count()} logical")
    # Which of the speed fixes this checkout actually has, so a slow run on a machine that
    # never received them isn't mistaken for a fix that didn't work.
    server = (ROOT / "web" / "server.py").read_text(encoding="utf-8")
    sim_learning = (src / "sim_learning.py").read_text(encoding="utf-8")
    # prewarm is checked in TWO places on purpose. The function living in ml_common.py proves
    # nothing -- it only helps if web/server.py actually starts the thread. Copying src/ between
    # machines without web/server.py would leave the definition present and never called, which
    # looks identical to "the fix didn't work" unless the call site is checked separately.
    checks = {
        "prewarm defined (src/ml_common.py)":
            "def prewarm" in (src / "ml_common.py").read_text(encoding="utf-8"),
        "prewarm CALLED at boot (web/server.py)":
            "ml_common_mod.prewarm" in server,
        "per-file tail-cache locks (src/simulate.py)":
            "_lock_for" in (src / "simulate.py").read_text(encoding="utf-8"),
        "semaphore around whole retrain (src/sim_learning.py)":
            "_train_locked" in sim_learning,
        "collection-lookup memo (src/sim_learning.py)":
            "cache=reliability_cache" in sim_learning,
        "shared reliability state (src/market_reliability.py)":
            "_resolved_state" in (src / "market_reliability.py").read_text(encoding="utf-8"),
    }
    for name, present in checks.items():
        print(f"  [{'x' if present else ' '}] {name}")
    if not all(checks.values()):
        print("  !! MISSING FIXES ABOVE -- this checkout is not running everything from the\n"
              "     speed pass. Sync the unchecked files before reading anything into the times.")
    try:
        concurrency = [l for l in sim_learning.splitlines()
                        if l.startswith("TRAIN_MAX_CONCURRENCY")][0]
        print(f"  {concurrency.split('#')[0].strip()}   (tuned for a 16-core box)")
    except IndexError:
        pass
    print()

    sons = sorted((ROOT / "data" / "sons").glob("son_*"))
    if not sons:
        print("no data/sons/ directories found -- is this the repo root?")
        return

    tot_wait = tot_work = 0.0
    for d in sons:
        meta = d / "model" / "sim_roi_model_meta.json"
        if not meta.exists():
            print(f"{d.name}: never trained")
            continue
        m = json.loads(meta.read_text(encoding="utf-8"))
        p = m.get("phase_seconds") or {}
        total = m.get("train_seconds") or 0.0
        wait = p.get("await_train_slot", 0.0)
        work = total - wait
        tot_wait += wait
        tot_work += work
        age_h = (time.time() - m["trained_at"]) / 3600 if m.get("trained_at") else float("nan")
        print(f"{d.name}  n={m['n_samples']:5d}  total={total:6.2f}s  "
              f"wait={wait:6.2f}s  work={work:6.2f}s   ({age_h:.1f}h ago)")
        big = {k: v for k, v in sorted(p.items(), key=lambda kv: -kv[1])
               if v >= 0.05 and k != "await_train_slot"}
        print(f"          phases: {json.dumps(big)}")
        print(f"          last 8 retrains: {(m.get('recent_train_seconds') or [])[-8:]}")

    n = len([d for d in sons if (d / 'model' / 'sim_roi_model_meta.json').exists()])
    if n:
        print(f"\nacross {n} sons: mean wait={tot_wait / n:.2f}s  mean work={tot_work / n:.2f}s")
        if tot_wait > tot_work:
            print("=> QUEUEING dominates. The retrains themselves are not the problem; the\n"
                  "   machine is saturated or TRAIN_MAX_CONCURRENCY is wrong for this core count.")
        else:
            print("=> REAL WORK dominates. Look at the biggest phase above.")


if __name__ == "__main__":
    main()
