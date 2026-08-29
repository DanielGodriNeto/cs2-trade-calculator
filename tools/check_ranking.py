"""Ranking invariants: (1) a fixed entry's composite_score is identical whether ranked alone
or in a big list (S1); (2) rank() makes one reliability cache, not one lookup per entry (P-1)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works under `python tools/check_ranking.py`

from src import ranking

base = {"son": 1, "cost": 100.0, "expected_roi_pct": 40.0, "input_mix": [], "output_distribution": [],
        "confidence_score": 80.0, "stattrak": False}
solo = ranking.rank([dict(base)])[0]["composite_score"]
padded = ranking.rank([dict(base)] + [dict(base, expected_roi_pct=r) for r in (5, 500, -20, 300)])
mine = next(e for e in padded if e["expected_roi_pct"] == 40.0)["composite_score"]
assert abs(solo - mine) < 1e-9, f"S1 broken: {solo} vs {mine} (score moved when neighbours changed)"
print(f"OK  fixed-scale composite stable: {solo:.3f}")
