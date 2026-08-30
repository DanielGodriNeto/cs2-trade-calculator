"""CV-MAE parity gate for changes to sim_learning / ml_common fitting.

First run per son writes a baseline; later runs compare. Delete tools/.mae_baseline.json
to re-baseline (do that on `main` BEFORE starting a model-touching task).

Exit codes: 0 = every checked son within 1.05x its baseline; 1 = a real regression
(ratio > 1.05); 2 = nothing was checked (no son had a trainable simulations.jsonl).
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent  # repo root
sys.path.insert(0, str(ROOT))  # so `from src import` works under `python tools/mae_parity.py`

from src import sim_learning as SL

BASELINE = ROOT / "tools" / ".mae_baseline.json"
sons = sys.argv[1:] or [f"son_{i}" for i in range(6)]
try:
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
except (json.JSONDecodeError, OSError):
    base = {}  # corrupt/truncated baseline -> treat as re-baseline
worst = 1.0
checked = 0
for s in sons:
    d = ROOT / "data" / "sons" / s
    if not (d / "simulations.jsonl").exists():
        print(f"{s}  (no local simulations.jsonl -- skipped)"); continue
    r = SL.train(d / "model", d / "simulations.jsonl")
    if not r.trained:
        print(f"{s}  (train refused: {r.reason})"); continue
    mae = float(r.validation_mae)
    checked += 1
    if s not in base:
        base[s] = mae
        print(f"{s}  baseline set: {mae:.4f}")
    else:
        ratio = mae / base[s]
        worst = max(worst, ratio)
        print(f"{s}  base={base[s]:.4f}  new={mae:.4f}  ratio={ratio:.3f}{'  <-- REGRESSION' if ratio > 1.05 else ''}")
if not checked:
    print("nothing checked -- no son had a trainable simulations.jsonl")
    sys.exit(2)
BASELINE.write_text(json.dumps(base, indent=2))
sys.exit(1 if worst > 1.05 else 0)
