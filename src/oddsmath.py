"""Shared odds & float math for the post-October-2025 CS2 trade-up rules.

Two rules changed from the old CS:GO model, per community testing after the update:

1. Odds are GROUP-based, not per-outcome. Inputs are grouped by collection (regular
   trade-ups) or by case (Covert -> knife/glove trade-ups). Each group's total chance is
   simply its share of the 10 (or 5, for Covert) inputs; that chance is then split evenly
   across every valid output in that group at the target rarity. A single-group contract
   (all 10 inputs from one collection) is just the n_groups=1 special case of this, so the
   plain scan in tradeup.py already matches it -- this module exists for when there's more
   than one group (see mixed_tradeup.py and covert.py).

2. Output float is the average of the input floats, each normalized into ITS OWN skin's
   min/max range, then re-applied to the OUTPUT skin's own min/max range -- not a shared
   global 0-1 scale. Skins with a restricted float range (e.g. 0.06-0.80) behave differently
   from ones spanning the full range, in both directions.

APPROXIMATION: Steam's public priceoverview endpoint doesn't expose exact per-listing float
values, only wear-condition buckets -- so "the input's float" here means the midpoint of its
matched wear bracket (float_mix.WEAR_MIDPOINT), not a real per-listing float. Treat results
as a shortlist to verify with a float-checking site before buying, not exact numbers.
"""
WEAR_BREAKPOINTS = [
    ("Factory New", 0.00, 0.07),
    ("Minimal Wear", 0.07, 0.15),
    ("Field-Tested", 0.15, 0.38),
    ("Well-Worn", 0.38, 0.45),
    ("Battle-Scarred", 0.45, 1.00),
]
WEAR_MIDPOINT = {name: (lo + hi) / 2 for name, lo, hi in WEAR_BREAKPOINTS}
WEAR_ORDER = [name for name, _, _ in WEAR_BREAKPOINTS]  # Factory New .. Battle-Scarred, cleanest to worst


def normalized_float(skin: dict, wear: str) -> float:
    """This skin's approximate float at `wear` (its wear-bracket midpoint), normalized into
    the skin's own [min_float, max_float] range -- 0.0 at its own cleanest possible float,
    1.0 at its own worst."""
    lo, hi = skin.get("min_float", 0.0), skin.get("max_float", 1.0)
    raw = WEAR_MIDPOINT.get(wear, 0.5)
    raw = min(max(raw, lo), hi)  # clamp: a narrow range can exclude a wear's nominal midpoint
    if hi <= lo:
        return 0.5
    return (raw - lo) / (hi - lo)


def average_normalized_float(inputs: list[tuple[dict, str, int]]) -> float:
    """inputs: [(skin_dict, wear, count), ...], possibly different skins/collections/cases.
    Returns the count-weighted average of each input's own-range-normalized float."""
    total = sum(c for _, _, c in inputs)
    if not total:
        return 0.5
    return sum(normalized_float(skin, wear) * c for skin, wear, c in inputs) / total


def wear_from_float(value: float) -> str:
    """Maps a raw float (0.00 = cleanest, 1.00 = worst -- not normalized to any particular
    skin's own min/max range) directly to its standard wear-bracket name. Clamps out-of-range
    input instead of raising, since a fat-fingered 1.02 in a log-outcome form should still
    resolve to Battle-Scarred rather than crash."""
    value = min(max(value, 0.0), 1.0)
    for name, lo, hi in WEAR_BREAKPOINTS:
        if lo <= value < hi:
            return name
    return WEAR_BREAKPOINTS[-1][0]  # >= the last breakpoint's lower bound -> Battle-Scarred


def remap_float_to_wear(output_skin: dict, avg_normalized: float) -> str:
    """Maps a count-weighted average normalized float (0=cleanest, 1=worst) back into
    output_skin's own [min_float, max_float] range, then returns whichever standard wear
    bracket that landed float actually falls into."""
    lo, hi = output_skin.get("min_float", 0.0), output_skin.get("max_float", 1.0)
    target_float = lo + avg_normalized * (hi - lo)
    for name, blo, bhi in WEAR_BREAKPOINTS:
        if blo <= target_float < bhi:
            return name
    return WEAR_BREAKPOINTS[-1][0]  # >= the last breakpoint's lower bound -> Battle-Scarred


import random as _random_mod

WEAR_PROB_SAMPLES = 600  # Monte-Carlo draws behind output_wear_probabilities() -- plenty for
# 5 display buckets rounded to whole percents, small enough to stay negligible next to the
# price lookups every candidate already pays for.


def sample_avg_normalized_floats(inputs: list[tuple[dict, str, int]],
                                  n: int = WEAR_PROB_SAMPLES, seed: int = 1337) -> list[float]:
    """The DISTRIBUTION behind average_normalized_float(): instead of pinning every input to
    its wear-bracket midpoint, draw each input's float uniformly within its wear bracket
    (clamped to that skin's own min/max range), normalize into the skin's own range, and
    average -- n times. The result is what the output float could actually be, not just its
    single midpoint estimate; wear_probabilities_for_output() below buckets it per output
    skin. Deterministic (fixed seed) so the same contract always reports the same
    percentages instead of flickering between renders.

    Computed ONCE per contract and reused for every output skin -- the input side of the
    math doesn't depend on which output you land."""
    rng = _random_mod.Random(seed)
    bracket = {name: (lo, hi) for name, lo, hi in WEAR_BREAKPOINTS}
    total = sum(c for _, _, c in inputs)
    if not total:
        return [0.5] * n
    # Pre-resolve each unit's sampling range once (not per draw).
    units = []
    for skin, wear, count in inputs:
        blo, bhi = bracket.get(wear, (0.0, 1.0))
        slo, shi = skin.get("min_float", 0.0), skin.get("max_float", 1.0)
        lo, hi = max(blo, slo), min(bhi, shi)
        if hi < lo:
            lo = hi = min(max((blo + bhi) / 2, slo), shi)  # bracket outside skin range: clamp
        norm_span = (shi - slo) or 1.0
        units.append((lo, hi, slo, norm_span, count))
    out = []
    for _ in range(n):
        acc = 0.0
        for lo, hi, slo, norm_span, count in units:
            for _i in range(count):
                raw = rng.uniform(lo, hi)
                acc += (raw - slo) / norm_span
        out.append(acc / total)
    return out


def wear_probabilities_for_output(output_skin: dict, avg_norm_samples: list[float]) -> dict:
    """{wear name: probability %} of where this output skin's float actually lands, given the
    sampled input-average distribution from sample_avg_normalized_floats(). Buckets with a
    0% share are omitted. This is the honest version of remap_float_to_wear()'s single
    midpoint answer -- e.g. inputs straddling a bracket edge really can produce either
    Field-Tested or Minimal Wear, and this says how often each."""
    if not avg_norm_samples:
        return {}
    lo, hi = output_skin.get("min_float", 0.0), output_skin.get("max_float", 1.0)
    counts: dict = {}
    for avg in avg_norm_samples:
        target = lo + avg * (hi - lo)
        counts[wear_from_float(target)] = counts.get(wear_from_float(target), 0) + 1
    n = len(avg_norm_samples)
    return {name: round(counts[name] / n * 100, 1) for name in WEAR_ORDER if name in counts}


def output_wear_probabilities(inputs: list[tuple[dict, str, int]], output_skin: dict,
                               n: int = WEAR_PROB_SAMPLES) -> dict:
    """Convenience one-shot for a single output -- callers pricing MANY outputs of the same
    contract should call sample_avg_normalized_floats() once and
    wear_probabilities_for_output() per output instead."""
    return wear_probabilities_for_output(output_skin, sample_avg_normalized_floats(inputs, n))


def group_odds(groups: list[tuple[int, int]]) -> list[float]:
    """groups: [(input_count_in_group, n_outputs_in_group), ...] for however many
    collections/cases are mixed into one contract. Returns, for each group, the
    PER-OUTPUT probability (as a percent): the group's share of total inputs, split evenly
    across its own outputs. (Guide example: 9 of 10 inputs from a 1-output collection and 1
    from a 3-output collection -> [90.0, 3.333, 3.333, 3.333], i.e. group_odds returns
    [90.0, 3.333] here since each group's *per-output* share is what callers multiply out
    across that group's output list.)"""
    total_inputs = sum(c for c, _ in groups)
    out = []
    for c, n_out in groups:
        if not total_inputs or not n_out:
            out.append(0.0)
            continue
        group_share = c / total_inputs
        out.append(group_share / n_out * 100)
    return out
