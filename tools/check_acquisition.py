"""UCB acquisition: _acquisition_score collapses a score_fn() result to the scalar
_softmax_weighted_sample ranks on -- (mean, std) tuple -> mean + beta*std, bare float
passes through, None -> -inf. Discriminating: fails if the UCB term is dropped."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_acquisition.py`

from src.random_trade import _acquisition_score

assert _acquisition_score((10.0, 8.0), 1.0) == 18.0          # UCB adds beta*std
assert _acquisition_score((10.0, 8.0), 0.0) == 10.0          # beta 0 -> mean only (today's behaviour)
assert _acquisition_score((10.0, 1.0), 1.0) == 11.0
assert _acquisition_score((10.0, 8.0), 1.0) > _acquisition_score((10.0, 1.0), 1.0)  # variance wins under beta>0
assert _acquisition_score(5.0, 1.0) == 5.0                   # bare float passes through
assert _acquisition_score(None, 1.0) == float("-inf")        # unscored
print("OK  _acquisition_score: UCB / passthrough / None all correct")
