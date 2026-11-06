"""Self-check for the mixed-float speedups in src.mixed_float / src.mixed_float_covert.

1. Average normalization on precomputed terms is bit-identical to oddsmath.average_normalized_float.
2. _candidate_mixes runs at most once per n_primary per primary collection/case (not once per
   filler candidate).
3. The covert search never prices the same (output skin, landed wear) twice within one call.

Run: python test_mixed_speed.py
"""
import random
from collections import Counter

from src import data as data_mod
from src import mixed_float as mixed_float_mod
from src import mixed_float_covert as covert_mod
from src import oddsmath

N_COLLECTIONS = 8
_RARITY_ID_BY_NAME = {name: rid for rid, name in data_mod.RARITY_CHAIN}
FROM_RARITY = _RARITY_ID_BY_NAME["Mil-Spec Grade"]  # collection index keys are rarity ids
TO_RARITY = _RARITY_ID_BY_NAME["Restricted"]


def check_avg_norm_bit_identical() -> None:
    rng = random.Random(7)
    for _ in range(300):
        inputs = []
        for _ in range(rng.randint(1, 5)):
            skin = {"name": "x", "wears": oddsmath.WEAR_ORDER,
                    "min_float": rng.choice([0.0, 0.06, 0.15]), "max_float": rng.choice([0.5, 0.8, 1.0])}
            inputs.append((skin, rng.choice(oddsmath.WEAR_ORDER), rng.randint(1, 6)))
        terms = [(oddsmath.normalized_float(s, w), c) for s, w, c in inputs]
        assert mixed_float_mod._avg_norm(terms) == oddsmath.average_normalized_float(inputs), inputs
    assert mixed_float_mod._avg_norm([]) == 0.5


def max_mix_builds_per_call(module, names, run_one) -> tuple[int, int]:
    """Returns (largest _candidate_mixes call count for one (n, primary) in one call, total calls)."""
    original = module._candidate_mixes
    counts: Counter = Counter()

    def counting(*args, **kwargs):
        counts[args[2]] += 1  # n_primary is the third positional argument in every call site
        return original(*args, **kwargs)

    module._candidate_mixes = counting
    worst = 0
    total = 0
    try:
        for name in names:
            counts.clear()
            run_one(name)
            worst = max([worst, *counts.values()])
            total += sum(counts.values())
    finally:
        module._candidate_mixes = original
    return worst, total


def check_mixes_built_once_per_n(sub_idx: dict) -> None:
    def run(collection: str) -> None:
        mixed_float_mod.find_best_mixed_float_strategy(
            collection, 0.0, sub_idx, FROM_RARITY, TO_RARITY, False, fast_mode=True,
            require_positive_baseline=False)

    worst, total = max_mix_builds_per_call(mixed_float_mod, list(sub_idx), run)
    assert total > 0, "no candidate mixes were built; the check covers nothing"
    assert worst <= 1, f"_candidate_mixes ran {worst} times for one (n_primary) in one call"


def check_covert_mixes_built_once_per_n(cases_idx: dict) -> None:
    def run(case: str) -> None:
        covert_mod.find_best_mixed_float_strategy(case, 0.0, cases_idx, False, fast_mode=True,
                                                  require_positive_baseline=False)

    worst, total = max_mix_builds_per_call(covert_mod, list(cases_idx), run)
    assert total > 0, "no covert candidate mixes were built; the check covers nothing"
    assert worst <= 1, f"covert _candidate_mixes ran {worst} times for one (n_primary) in one call"


def check_covert_prices_memoized(cases_idx: dict) -> None:
    original = covert_mod._honest_market_quote
    seen: list = []

    def recording(*args, **kwargs):
        seen.append((args[0]["name"], args[1]))
        return original(*args, **kwargs)

    covert_mod._honest_market_quote = recording
    try:
        for case in list(cases_idx)[:N_COLLECTIONS]:
            seen.clear()
            covert_mod.find_best_mixed_float_strategy(case, 0.0, cases_idx, False, fast_mode=True,
                                                      require_positive_baseline=False)
            assert len(seen) == len(set(seen)), f"{case}: {len(seen) - len(set(seen))} repeated price lookups"
    finally:
        covert_mod._honest_market_quote = original


def main() -> None:
    check_avg_norm_bit_identical()
    skins = data_mod.load_skins()
    idx = data_mod.collections_index(skins)
    sub_idx = {c: v for c, v in idx.items() if v.get(FROM_RARITY) and v.get(TO_RARITY)}
    sub_idx = dict(list(sub_idx.items())[:N_COLLECTIONS])
    check_mixes_built_once_per_n(sub_idx)
    cases_idx = data_mod.cases_index(skins, data_mod.load_knives_gloves())
    sub_cases = dict(list(cases_idx.items())[:N_COLLECTIONS])  # every case has covert inputs and outputs
    check_covert_mixes_built_once_per_n(sub_cases)
    check_covert_prices_memoized(sub_cases)
    print("ok")


if __name__ == "__main__":
    main()
