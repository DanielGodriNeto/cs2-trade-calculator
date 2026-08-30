"""At low temperature with a wide score spread, selection must still be weighted-random,
not 'take the first k in list order' (the exp() underflow branch)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_softmax.py`

import random
from src.random_trade import _softmax_weighted_sample

rng = random.Random(0)
scored = [(0.0, "top")] + [(-200.0, f"tail{i}") for i in range(200)]   # exp(-200/0.2) = exp(-1000) underflows to exactly 0.0
picks = [tuple(_softmax_weighted_sample(random.Random(s), scored, 5, 0.2)) for s in range(60)]
tails = [p[1:] for p in picks]
assert len(set(tails)) > 1, "underflow: tail is deterministic list-order, not random"
assert all(p[0] == "top" for p in picks), "top score should still almost always be picked"
print(f"OK  {len(set(tails))} distinct tail draws over 60 seeds")
