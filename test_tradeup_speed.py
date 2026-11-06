"""Self-check for the trade-up speed path (oddsmath sampling/bucketing + lazy wear_probs in tradeup).

1. oddsmath fast paths (remap_float_to_wear, sample_avg_normalized_floats, wear_probabilities_for_output)
   equal their original loop implementations exactly, on random and boundary inputs.
2. tradeup.scan_all: wear_probs attached to rows kept by min_roi=0 equal the ones recomputed with the
   ORIGINAL reference functions from the full (min_roi=-inf) scan, and the kept row set is exactly the
   ROI-filtered subset of the full scan.

Run: python test_tradeup_speed.py
"""
import hashlib
import json
import random

from src import data as data_mod
from src import oddsmath as om
from src import tradeup as tu

BRACKET = {name: (lo, hi) for name, lo, hi in om.WEAR_BREAKPOINTS}


def ref_remap(output_skin, avg):
    lo, hi = output_skin.get("min_float", 0.0), output_skin.get("max_float", 1.0)
    target = lo + avg * (hi - lo)
    for name, blo, bhi in om.WEAR_BREAKPOINTS:
        if blo <= target < bhi:
            return name
    return om.WEAR_BREAKPOINTS[-1][0]


def ref_samples(inputs, n=om.WEAR_PROB_SAMPLES, seed=1337):
    rng = random.Random(seed)
    total = sum(c for _, _, c in inputs)
    if not total:
        return [0.5] * n
    units = []
    for skin, wear, count in inputs:
        blo, bhi = BRACKET.get(wear, (0.0, 1.0))
        slo, shi = skin.get("min_float", 0.0), skin.get("max_float", 1.0)
        lo, hi = max(blo, slo), min(bhi, shi)
        if hi < lo:
            lo = hi = min(max((blo + bhi) / 2, slo), shi)
        units.append((lo, hi, slo, (shi - slo) or 1.0, count))
    out = []
    for _ in range(n):
        acc = 0.0
        for lo, hi, slo, norm_span, count in units:
            for _i in range(count):
                acc += (rng.uniform(lo, hi) - slo) / norm_span
        out.append(acc / total)
    return out


def ref_wear_probs(output_skin, samples):
    if not samples:
        return {}
    lo, hi = output_skin.get("min_float", 0.0), output_skin.get("max_float", 1.0)
    counts = {}
    for avg in samples:
        name = om.wear_from_float(lo + avg * (hi - lo))
        counts[name] = counts.get(name, 0) + 1
    return {name: round(counts[name] / len(samples) * 100, 1) for name in om.WEAR_ORDER if name in counts}


def random_skin(rng):
    lo = rng.choice([0.0, 0.0, 0.06, -0.1])
    hi = rng.choice([1.0, 1.0, 0.8, 0.5, 1.2])
    return {"name": "s", "min_float": lo, "max_float": hi}


def check_oddsmath_equivalence():
    rng = random.Random(7)
    boundary_avgs = [0.0, 0.07, 0.15, 0.38, 0.45, 1.0, -0.2, 1.3]
    for _ in range(300):
        skin = random_skin(rng)
        avg = rng.uniform(-0.3, 1.4)
        assert om.remap_float_to_wear(skin, avg) == ref_remap(skin, avg), (skin, avg)
    for avg in boundary_avgs:
        for skin in [{"min_float": 0.0, "max_float": 1.0}, {"min_float": 0.06, "max_float": 0.8}]:
            assert om.remap_float_to_wear(skin, avg) == ref_remap(skin, avg), (skin, avg)
    for trial in range(20):
        inputs = []
        for _ in range(rng.randint(1, 4)):
            wear = rng.choice(list(BRACKET))
            inputs.append((random_skin(rng), wear, rng.randint(1, 10)))
        got = om.sample_avg_normalized_floats(inputs)
        assert got == ref_samples(inputs), f"sampler mismatch trial {trial}"
        out_skin = random_skin(rng)
        assert om.wear_probabilities_for_output(out_skin, got) == ref_wear_probs(out_skin, got), trial
    edge_samples = [-0.2, 0.0, 0.07, 0.15, 0.38, 0.45, 1.0, 1.3] * 3
    for skin in [{"min_float": 0.0, "max_float": 1.0}, {"min_float": 0.06, "max_float": 0.8}]:
        assert om.wear_probabilities_for_output(skin, edge_samples) == ref_wear_probs(skin, edge_samples)
    assert om.wear_probabilities_for_output(skin, []) == {}
    print("ok oddsmath equivalence")


def digest_rows(results):
    rows = []
    for r in results:
        drops = [(d.name, d.price, d.wear, d.wear_probs) for d in r.possible_drops]
        rows.append((r.collection, r.from_rarity, r.to_rarity, r.roi_pct, drops))
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def check_scan_wear_probs(idx):
    full = tu.scan_all(idx, fast_mode=True, min_roi=float("-inf"))
    kept = tu.scan_all(idx, fast_mode=True, min_roi=0.0)
    full_by_key = {(r.collection, r.from_rarity, r.to_rarity): r for r in full}
    kept_keys = {(r.collection, r.from_rarity, r.to_rarity) for r in kept}
    assert kept_keys == set(full_by_key), "min_roi must not change which rows scan_all returns"
    id_by_name = {name: rid for rid, name in data_mod.RARITY_CHAIN}
    for r in full:
        in_skin = next(s for s in idx[r.collection][id_by_name[r.from_rarity]] if s["name"] == r.cheapest_input_skin)
        samples = ref_samples([(in_skin, r.cheapest_input_wear, tu._N)])
        for d in r.possible_drops:
            out_skin = next(s for s in idx[r.collection][id_by_name[r.to_rarity]] if s["name"] == d.name)
            assert d.wear_probs == ref_wear_probs(out_skin, samples), (r.collection, d.name)
    n_kept = 0
    for r in kept:
        ref = full_by_key[(r.collection, r.from_rarity, r.to_rarity)]
        if r.roi_pct >= 0.0:
            n_kept += 1
            assert digest_rows([r]) == digest_rows([ref]), (r.collection, r.from_rarity)
        else:
            assert all(d.wear_probs == {} for d in r.possible_drops), "below min_roi: wear_probs skipped"
    assert n_kept, "no kept rows: the wear_probs check would be vacuous"
    print(f"ok scan wear_probs: {len(full)} rows verified against reference, {n_kept} rows at roi>=0 identical")


def main():
    check_oddsmath_equivalence()
    idx = data_mod.collections_index(data_mod.load_skins())
    check_scan_wear_probs(idx)
    print("ok test_tradeup_speed")


if __name__ == "__main__":
    main()
