"""stattrak_rate=1.0 -> every candidate StatTrak; 0.0 -> none."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_stattrak_rate.py`

from src import random_trade, data
import random

idx = data.collections_index(data.load_skins())
on = [random_trade.random_candidate(idx, random.Random(i), stattrak_rate=1.0) for i in range(30)]
off = [random_trade.random_candidate(idx, random.Random(i), stattrak_rate=0.0) for i in range(30)]
assert all(c is None or c.stattrak for c in on) and all(c is None or not c.stattrak for c in off)
print("OK  stattrak_rate override honoured")
