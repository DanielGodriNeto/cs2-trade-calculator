"""Self-check for the per-call price memo in src.mixed_float.find_best_mixed_float_strategy.

Runs the real tradeup.scan_all (fast_mode, float + mixed strategies, any ROI) over the first
N_COLLECTIONS collections twice: once with mixed_float._USE_PRICE_MEMO = True, once with False.
Asserts the full result digest is identical, at least one mixed-float result exists (so the digest
actually covers that path), and the memoized run makes at least 5x fewer _price_at_matched_wear calls.

Run: python test_mixed_float_memo.py
"""
import hashlib
import json
from dataclasses import asdict, is_dataclass

from src import data as data_mod
from src import mixed_float as mixed_float_mod
from src import tradeup as tradeup_mod

N_COLLECTIONS = 8
MIN_REDUCTION = 5


def _normalize(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(value))
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def _digest(results: list) -> str:
    blob = json.dumps(_normalize(results), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _run(sub_idx: dict, memo_on: bool) -> tuple[str, int, int]:
    """Returns (digest, _price_at_matched_wear call count, non-None mixed-float result count)."""
    mixed_float_mod._USE_PRICE_MEMO = memo_on
    original = mixed_float_mod._price_at_matched_wear
    calls = [0]

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    mixed_float_mod._price_at_matched_wear = counting
    try:
        results = tradeup_mod.scan_all(sub_idx, stattrak=False, currency=1, fast_mode=True,
                                       float_strategy=True, mixed_strategy=True, strategy_any_roi=True)
    finally:
        mixed_float_mod._price_at_matched_wear = original
        mixed_float_mod._USE_PRICE_MEMO = True
    mixed_count = sum(1 for r in results if r.mixed_float_strategy is not None)
    return _digest(results), calls[0], mixed_count


def main() -> None:
    idx = data_mod.collections_index(data_mod.load_skins())
    sub_idx = dict(list(idx.items())[:N_COLLECTIONS])

    digest_on, calls_on, mixed_on = _run(sub_idx, memo_on=True)
    digest_off, calls_off, mixed_off = _run(sub_idx, memo_on=False)

    assert mixed_off > 0, "no mixed-float results were produced; the digest covers nothing"
    assert digest_on == digest_off, "memoized results differ from unmemoized results"
    assert mixed_on == mixed_off, "memoized run produced a different number of mixed-float results"
    assert calls_on * MIN_REDUCTION <= calls_off, (
        f"memo reduction too small: {calls_off} calls without memo, {calls_on} with memo "
        f"(need >= {MIN_REDUCTION}x)")
    print(f"mixed-float results: {mixed_off} (memo on: {mixed_on}); digest {digest_off[:16]}")
    print(f"_price_at_matched_wear calls: without memo={calls_off}, with memo={calls_on} "
          f"({calls_off / max(calls_on, 1):.1f}x fewer)")
    print("ok")


if __name__ == "__main__":
    main()
