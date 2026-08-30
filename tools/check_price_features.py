"""M6 price-ratio features: _price_ratio math + _feature_vector width stays == len(FEATURE_NAMES).
RED before Task 9's changes: ImportError on _price_ratio (and width 7 != 9)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_price_features.py`

import math

from src.sim_learning import _price_ratio, _feature_vector, FEATURE_NAMES

assert _price_ratio([10.0, 20.0], [5.0]) == 3.0                       # mean(15) / mean(5)
assert math.isnan(_price_ratio([None, None], [5.0]))                  # all-cold outputs -> nan
assert math.isnan(_price_ratio([10.0], []))                          # no inputs -> nan
assert _price_ratio([10.0, None], [4.0, 6.0]) == 2.0                 # None entries dropped: 10 / 5

fv = _feature_vector(from_rarity="Mil-Spec Grade",
                     input_mix=[{"collection": "C", "wear": "Field-Tested", "count": 10}],
                     primary_collection="C", stattrak=False, cost=1.0, collection_mean_roi=0.0,
                     output_input_price_ratio=2.0, n_target_outputs=5.0)
assert len(fv) == len(FEATURE_NAMES) == 9, (len(fv), len(FEATURE_NAMES))
assert fv[-2] == 2.0 and fv[-1] == 5.0

fv_nan = _feature_vector(from_rarity="Mil-Spec Grade",
    input_mix=[{"collection": "C", "wear": "Field-Tested", "count": 10}],
    primary_collection="C", stattrak=False, cost=1.0, collection_mean_roi=0.0,
    output_input_price_ratio=float("nan"), n_target_outputs=5.0)
assert math.isnan(fv_nan[-2]) and fv_nan[-1] == 5.0   # nan ratio flows through, n_target_outputs stays real

print("OK  _price_ratio + _feature_vector width (9)")
