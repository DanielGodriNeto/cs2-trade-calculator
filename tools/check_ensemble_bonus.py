"""6-son ensemble agreement bonus: a tight consensus that also matches the formula ROI earns
near ENSEMBLE_MAX_BONUS; a wide spread or a disagreeing median earns ~0; missing ensemble_*
keys earn nothing; and _confidence_score still can't exceed 100 with every bonus maxed."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_ensemble_bonus.py`

from src import ranking

tight = {"expected_roi_pct": 40.0, "verified_roi_pct": 40.0, "price_confidence": "ok",
         "ensemble_roi_median": 41.0, "ensemble_roi_spread": 3.0}
wide  = dict(tight, ensemble_roi_spread=60.0)
disagree = dict(tight, ensemble_roi_median=200.0)
none_ = {"expected_roi_pct": 40.0}
assert ranking._ensemble_agreement_bonus(tight) > ranking.ENSEMBLE_MAX_BONUS * 0.7, ranking._ensemble_agreement_bonus(tight)
assert ranking._ensemble_agreement_bonus(wide) < 0.5
assert ranking._ensemble_agreement_bonus(disagree) < 0.5
assert ranking._ensemble_agreement_bonus(none_) == 0.0
# confidence_score never exceeds 100 even with every bonus maxed
maxed = {"cost_basis_source": "cache", "input_mix": [], "output_distribution": [],
         "expected_roi_pct": 40.0, "verified_roi_pct": 40.0, "price_confidence": "ok",
         "ml_predicted_roi_pct": 40.0, "ensemble_roi_median": 40.0, "ensemble_roi_spread": 0.0}
assert ranking._confidence_score(maxed) <= 100.0
print("OK  ensemble agreement bonus: tight~max / wide~0 / disagree~0 / cap<=100")
