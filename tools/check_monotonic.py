"""With monotonic_cst on collection_mean_expected_roi, raising only that feature must never
lower the prediction."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_monotonic.py`

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
