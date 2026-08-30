"""Recency-weight DIRECTION (I4): simulate.load_recent_simulations returns rows OLDEST-first,
so _train_locked must give the newest row full weight and decay the oldest -- an inverted
exponent would train cleanly, look plausible, and silently teach every son from the oldest rows."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_recency_weight.py`

from src.sim_learning import _recency_weights, RECENCY_HALFLIFE_ROWS
w = _recency_weights(2000)
assert w[-1] == 1.0, w[-1]                                   # newest row: full weight
assert w[0] == 0.5 ** (1999 / RECENCY_HALFLIFE_ROWS), w[0]   # oldest row: max decay
assert all(w[i] <= w[i+1] for i in range(len(w)-1)), "recency weights must be non-decreasing oldest->newest"
assert w[0] < w[-1], (w[0], w[-1])
print(f"OK  recency direction: oldest {w[0]:.4f} -> newest {w[-1]:.4f}")
