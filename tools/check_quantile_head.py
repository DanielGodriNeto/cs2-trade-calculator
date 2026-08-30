"""with_std returns (mean, std>=0); std is 0 when the hi model is absent; mean matches the
plain call exactly."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_quantile_head.py`

from pathlib import Path
from src import sim_learning as SL, random_trade as RT, data
son = Path("data/sons/son_0"); md, sf = son/"model", son/"simulations.jsonl"
idx = data.collections_index(data.load_skins()); import random; c = RT.random_candidate(idx, random.Random(3))
plain = SL.predict_expected_roi(c, md, sf)
pair = SL.predict_expected_roi(c, md, sf, with_std=True)
if plain is None:
    assert pair is None; print("OK  (son_0 untrained locally — None/None)")
else:
    m, s = pair
    assert abs(m - plain) < 1e-9 and s >= 0.0, (m, plain, s)
    print(f"OK  mean={m:.2f}  std={s:.2f}")
