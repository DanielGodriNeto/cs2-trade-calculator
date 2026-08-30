"""Random plausible trade-up candidate generator.

Generates a structurally valid, totally random n-input trade-up contract: a random rarity
pair, a random primary collection, 0-9 random filler collections (see mixed_tradeup.py for
why mixing collections works under the post-Oct-2025 group-based odds), a random SELECTION OF
DISTINCT SKINS within each collection (not just one repeated skin), and a random wear for
every individual input unit -- so the skin mix and the wear-mix are both random, not just
which collection(s) are involved. A real trade-up only requires all 10 inputs share the same
rarity tier; it never requires them to be the same skin, even within one collection, so
restricting each collection to a single skin would have silently excluded most of the real
search space.

Plausibility (float-cap awareness) comes for free by construction: every skin, collection,
and wear is pulled straight from data.py's real skin database (the same one tradeup.py
already trusts), so there is no path to generating a skin/wear/collection
combination that couldn't really exist. See oddsmath.normalized_float, which reads a wear's
float purely against that specific skin's own min_float/max_float.

Sampling isn't uniform once sim_learning.py has a trained model: generate_batch() draws a
larger pool than it needs and keeps only the top-scored candidates (importance sampling)
-- see its docstring for why pure-uniform random search wastes most of its budget on
obviously-bad candidates.
"""
import math
import random as random_mod
from dataclasses import dataclass, field

from . import data as data_mod
from . import rules_engine as rules_engine_mod
from . import steam_prices as steam_prices_mod

MAX_FILLER_COLLECTIONS = 9  # up to 9 filler collections + 1 primary = all 10 inputs can each
# come from a DIFFERENT collection. No combinatorial-search cost to raising this (unlike
# mixed_tradeup.py's exhaustive brute-force search, which deliberately caps at 1 filler
# collection for that reason) -- every random draw here is O(1) regardless of how many
# collections are in play; simulate.py just prices one more leg per extra collection.
FILLER_COUNT_DECAY = 0.55  # weight(k filler collections) = FILLER_COUNT_DECAY ** k -- favors
# fewer collections (real traders mostly stack one collection; mixing more dilutes odds
# across more outputs, per CS2's group-based rules -- see oddsmath.group_odds), but leaves a
# non-negligible tail so heavily-diluted many-collection mixes are still occasionally sampled
# and the ML gets to see (and learn from) that part of the space too.
SKIN_MIX_DECAY = 0.55  # same shape, applied within one collection: weight(k distinct skins)
# = SKIN_MIX_DECAY ** (k-1) -- favors buying one cheap skin x10 (the common real strategy),
# but a collection's input-tier skins are never forced into a single choice.
N_INPUTS = rules_engine_mod.STANDARD_INPUT_COUNT  # single source of truth, see rules_engine.py

WARM_CACHE_BIAS = 3.0  # weight multiplier applied to a skin/wear that's ALREADY warm in
# steam_prices' cache (see random_candidate's warm_names param, sourced from
# steam_prices.iter_cached_entries() -- the same helper dead_skin.py already reuses) over a cold
# one that would need a real live Steam fetch to price. Turns "spend a live request to discover
# whether a candidate can even be priced" into "prefer candidates already known to be
# priceable" -- strictly cheaper, and doesn't touch oddsmath.py's odds math at all (only WHICH
# skins/wears get proposed changes, never how a formed candidate's probabilities are computed).
# Deliberately a bias, never a hard filter (warm_names=None/empty reproduces the exact original
# uniform-random behavior) -- a hard filter would mean the search space could never grow past
# whatever price_scan.py has already warmed, which would starve exploration of any signal about
# what's worth warming next.

STATTRAK_RATE = 0.15  # P(a random candidate is StatTrak-scoped). Module default; random_candidate/
# generate_batch take a `stattrak_rate` override so a future adaptive/per-agent value has a seam
# (sim_loop.run_round is NOT wired to it yet).


@dataclass
class CandidateLeg:
    collection: str
    units: list            # [{"skin": dict, "wear": str, "count": int}, ...] -- possibly
    # SEVERAL DISTINCT skins from this collection, each with their own wear-mix; summing all
    # counts gives this leg's share of N_INPUTS
    outputs: list           # this collection's skins at the target rarity (possible drops for this leg)


@dataclass
class Candidate:
    from_rarity_id: str
    from_rarity_name: str
    to_rarity_id: str
    to_rarity_name: str
    stattrak: bool
    legs: list = field(default_factory=list)  # list[CandidateLeg]; legs[0] is the primary leg by convention
    contract_type: str = "standard"  # "standard" (10-input, collection-based) | "covert" (5-input,
    # case-based) -- this generator only ever produces "standard" today; see rules_engine.py's
    # validate_contract()/_contract_type_of(), which reads this to know how many total input
    # units a candidate needs.


def _wear_weights(wears: list, wear_bias: float) -> list:
    """wear_bias=0 -> uniform (every wear equally likely, the original behavior). wear_bias>0
    skews sampling toward THIS skin's own wear-order EXTREMES (its cleanest and its worst
    available wear) over the middle brackets -- operationalizes the Wear Efficiency Agent's
    purpose ("can cheap Battle-Scarred inputs create valuable outputs / is paying more for
    better wear worth it") by actually searching the extremes more often instead of drawing
    every wear with equal probability. Not a reuse of float_mix.py's deliberate wear-mix
    search (that operates on an already-priced candidate mid-scan, a different call shape) --
    a self-contained weighting so this stays a cheap, cache-free random draw."""
    n = len(wears)
    if wear_bias <= 0 or n <= 1:
        return [1.0] * n
    mid = (n - 1) / 2
    return [1.0 + wear_bias * 3.0 * (abs(i - mid) / mid if mid else 1.0) for i in range(n)]


def _valid_wears(skin: dict, stattrak: bool, invalid_names: set | None) -> list:
    """This skin's available wears minus any whose market_hash_name is currently in the
    invalid-skin registry (no obtainable price on Steam or any third-party site -- see
    invalid_skins.py). invalid_names is pre-scoped to the run's currency, so this is a plain
    in-memory set lookup; None/empty means no filtering (the original behavior)."""
    if not invalid_names:
        return skin["wears"]
    return [w for w in skin["wears"]
            if steam_prices_mod.market_hash_name(skin["name"], w, stattrak) not in invalid_names]


def _random_wear_counts(rng: random_mod.Random, wears: list, count: int, wear_bias: float = 0.0,
                        warm_wears: set | None = None) -> dict:
    """Assigns each of `count` units a random wear from `wears` (this skin's available wears,
    already filtered to ones with an obtainable price -- see _valid_wears) -- the source of
    "totally random floats". wear_bias=0 (the default, used by every agent except the Wear
    Efficiency Agent) is uniform-random, exactly the original behavior -- see _wear_weights.

    warm_wears: the subset of `wears` that are ALREADY warm in steam_prices' cache (see
    random_candidate's warm_names) -- multiplies their draw weight by WARM_CACHE_BIAS on top of
    wear_bias's extremity weighting, so a candidate is more likely to land on units simulate.py
    can price straight from cache instead of blocking on a live fetch. None/empty reproduces the
    original behavior exactly (never a hard exclusion of cold wears)."""
    out: dict = {}
    weights = _wear_weights(wears, wear_bias)
    if warm_wears:
        weights = [w * (WARM_CACHE_BIAS if wear in warm_wears else 1.0) for w, wear in zip(weights, wears)]
    for _ in range(count):
        w = rng.choices(wears, weights=weights)[0]
        out[w] = out.get(w, 0) + 1
    return out


def _random_composition(rng: random_mod.Random, total: int, parts: int) -> list:
    """Random positive-integer composition of `total` into `parts` pieces (each >= 1)."""
    if parts == 1:
        return [total]
    cuts = sorted(rng.sample(range(1, total), parts - 1))
    sizes = []
    prev = 0
    for c in cuts:
        sizes.append(c - prev)
        prev = c
    sizes.append(total - prev)
    return sizes


def _weighted_sample_without_replacement(rng: random_mod.Random, items: list, weights: list, k: int) -> list:
    """Plain weighted draw-without-replacement over already-positive linear weights (unlike
    _softmax_weighted_sample below, these aren't scores that need temperature-scaling). Pools
    here are tiny (one collection's input-tier skin count), so the O(n*k) cost is negligible --
    same shape/cost tradeoff as _softmax_weighted_sample."""
    pool = list(zip(items, weights))
    chosen = []
    for _ in range(min(k, len(pool))):
        total = sum(w for _, w in pool)
        if total <= 0:
            chosen.extend(item for item, _ in pool[:k - len(chosen)])
            break
        r = rng.random() * total
        acc = 0.0
        for i, (item, w) in enumerate(pool):
            acc += w
            if acc >= r:
                chosen.append(item)
                pool.pop(i)
                break
        else:
            chosen.append(pool[-1][0])
            pool.pop()
    return chosen


def _random_skin_mix(rng: random_mod.Random, inputs: list, count: int, wear_bias: float = 0.0,
                     stattrak: bool = False, invalid_names: set | None = None,
                     warm_names: set | None = None) -> "list | None":
    """Splits `count` input units across a random selection of 1..min(len(pool), count)
    DISTINCT skins from this collection's input pool at this rarity (weighted toward fewer
    skins via SKIN_MIX_DECAY, but never forced to just one), each getting its own random
    per-unit wear-mix (_random_wear_counts, see wear_bias there). Returns
    [{"skin": dict, "wear": str, "count": int}, ...].

    Skins whose EVERY available wear is currently invalid (no obtainable price anywhere -- see
    invalid_skins.py) are dropped from the pool first, and only each surviving skin's still-
    valid wears are drawn from; if that leaves this collection with no usable input skin at all,
    returns None so random_candidate() discards the whole candidate and draws again.

    warm_names: market_hash_names currently warm in steam_prices' cache (see random_candidate) --
    when given, biases BOTH which skins get chosen (a skin with more already-warm wears gets a
    higher draw weight, via _weighted_sample_without_replacement) and which of a chosen skin's
    wears get used (via _random_wear_counts' own warm_wears bias) -- see WARM_CACHE_BIAS. None
    (the default) reproduces the original uniform `rng.sample` behavior exactly."""
    pool = [(s, _valid_wears(s, stattrak, invalid_names)) for s in inputs]
    pool = [(s, wears) for s, wears in pool if wears]
    if not pool:
        return None
    max_distinct = min(len(pool), count)
    choices = list(range(1, max_distinct + 1))
    weights = [SKIN_MIX_DECAY ** (k - 1) for k in choices]
    n_skins = rng.choices(choices, weights=weights)[0]
    if warm_names:
        skin_weights = []
        for s, wears in pool:
            n_warm = sum(1 for w in wears
                        if steam_prices_mod.market_hash_name(s["name"], w, stattrak) in warm_names)
            skin_weights.append(1.0 + (WARM_CACHE_BIAS - 1.0) * (n_warm / len(wears)))
        chosen = _weighted_sample_without_replacement(rng, pool, skin_weights, n_skins)
    else:
        chosen = rng.sample(pool, n_skins)
    counts = _random_composition(rng, count, n_skins)
    units = []
    for (skin, valid_wears), c in zip(chosen, counts):
        warm_wears = None
        if warm_names:
            warm_wears = {w for w in valid_wears
                          if steam_prices_mod.market_hash_name(skin["name"], w, stattrak) in warm_names}
        for wear, wear_count in _random_wear_counts(rng, valid_wears, c, wear_bias, warm_wears).items():
            units.append({"skin": skin, "wear": wear, "count": wear_count})
    return units


def _eligible_collections(idx: dict, from_id: str, to_id: str, stattrak: bool) -> list:
    out = []
    for coll, by_rarity in idx.items():
        inputs = by_rarity.get(from_id, [])
        outputs = by_rarity.get(to_id, [])
        if stattrak:
            inputs = [s for s in inputs if s["stattrak"]]
            outputs = [s for s in outputs if s["stattrak"]]
        if inputs and outputs:
            out.append(coll)
    return out


_ADJACENT_RARITY_PAIRS = list(zip([r for r, _ in data_mod.RARITY_CHAIN],
                                   [r for r, _ in data_mod.RARITY_CHAIN][1:]))


def random_candidate(idx: dict, rng: random_mod.Random | None = None, n_inputs: int = N_INPUTS,
                      max_filler_collections: int = MAX_FILLER_COLLECTIONS,
                      filler_count_decay: float | None = None, wear_bias: float = 0.0,
                      invalid_names: set | None = None, warm_names: set | None = None, stattrak_rate: float | None = None) -> "Candidate | None":
    """One random candidate. Returns None on the rare case (mostly StatTrak-scoped rarity
    pairs) where no valid collection exists for the randomly drawn rarity pair -- OR where a
    chosen collection has no input skin with an obtainable price left after invalid_names
    filtering -- callers should just draw again (see generate_batch).

    filler_count_decay: per-agent override of FILLER_COUNT_DECAY (None = module default) --
    see agents.py's collection_mixing_bias. wear_bias: per-agent wear-extremity skew (0 =
    original uniform-random behavior) -- see agents.py's wear_optimization_bias and
    _wear_weights above. invalid_names: market_hash_names to exclude (skins with no price on
    Steam or any third-party site -- see invalid_skins.py / simulate.py); None = no filtering.
    warm_names: market_hash_names ALREADY warm in steam_prices' cache (see
    steam_prices.iter_cached_entries()) -- biases (never hard-filters) skin/wear selection
    toward these within each leg, see _random_skin_mix/WARM_CACHE_BIAS; None = no bias, the
    original uniform-random behavior. stattrak_rate: override the module STATTRAK_RATE for
    this draw (None = default)."""
    rng = rng or random_mod.Random()
    from_id, to_id = rng.choice(_ADJACENT_RARITY_PAIRS)
    stattrak = rng.random() < (stattrak_rate if stattrak_rate is not None else STATTRAK_RATE)  # StatTrak trade-ups are a real but minority use case

    eligible = _eligible_collections(idx, from_id, to_id, stattrak)
    if not eligible:
        return None
    primary_collection = rng.choice(eligible)

    # How many filler collections to mix in: capped by both max_filler_collections and how
    # many OTHER eligible collections actually exist for this rarity pair (e.g. a rare
    # StatTrak rarity pair might only have a couple of eligible collections at all).
    max_possible = min(max_filler_collections, len(eligible) - 1)
    choices = list(range(0, max_possible + 1))
    effective_decay = filler_count_decay if filler_count_decay is not None else FILLER_COUNT_DECAY
    weights = [effective_decay ** k for k in choices]
    n_filler = rng.choices(choices, weights=weights)[0]
    filler_pool = [c for c in eligible if c != primary_collection]
    rng.shuffle(filler_pool)
    filler_collections = filler_pool[:n_filler]

    counts = _random_composition(rng, n_inputs, len(filler_collections) + 1)

    legs = []
    for collection, count in zip([primary_collection, *filler_collections], counts):
        by_rarity = idx[collection]
        inputs = by_rarity.get(from_id, [])
        outputs = by_rarity.get(to_id, [])
        if stattrak:
            inputs = [s for s in inputs if s["stattrak"]]
            outputs = [s for s in outputs if s["stattrak"]]
        units = _random_skin_mix(rng, inputs, count, wear_bias, stattrak, invalid_names, warm_names)
        if units is None:  # every input skin in this collection is priceless/invalid right now
            return None
        legs.append(CandidateLeg(collection=collection, units=units, outputs=outputs))

    from_name = dict(data_mod.RARITY_CHAIN)[from_id]
    to_name = dict(data_mod.RARITY_CHAIN)[to_id]
    return Candidate(from_rarity_id=from_id, from_rarity_name=from_name, to_rarity_id=to_id,
                      to_rarity_name=to_name, stattrak=stattrak, legs=legs)


def _softmax_weighted_sample(rng: random_mod.Random, scored: list, k: int, temperature: float) -> list:
    """Weighted sampling WITHOUT replacement of k candidates from `scored` ([(score, item), ...]),
    weight ∝ exp(score / temperature) -- the "randomness variability" dial each son uses.

    Low temperature (e.g. 0.2) makes the softmax sharply peaked around the highest scores, so
    this closely approximates deterministic top-K (an exploitative son that mostly trusts its
    model). High temperature (e.g. 2.0) flattens the distribution toward uniform, so selection
    barely depends on score at all (an explorative son that mostly ignores its own model and
    keeps sampling broadly). Implemented as k sequential weighted draws-without-replacement
    (remove the drawn item, renormalize, repeat) -- batch sizes here are small (~200), so the
    O(n*k) cost is negligible; no numpy needed."""
    scores = [s for s, _ in scored]
    max_score = max(scores)
    pool = list(scored)
    chosen = []
    for _ in range(min(k, len(pool))):
        FLOOR = 1e-12
        weights = [max(FLOOR, math.exp((s - max_score) / temperature)) for s, _ in pool]
        total = sum(weights)
        if total <= 0:
            chosen.extend(item for _, item in pool[:k - len(chosen)])
            break
        r = rng.random() * total
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if acc >= r:
                chosen.append(pool[i][1])
                pool.pop(i)
                break
        else:
            chosen.append(pool[-1][1])
            pool.pop()
    return chosen


def _acquisition_score(raw, acquisition_beta: float) -> float:
    """Collapse one score_fn() result to the scalar _softmax_weighted_sample ranks on.
    (mean, std) 2-tuple -> mean + acquisition_beta*std (UCB); bare float -> itself;
    None -> -inf (unscored, as before)."""
    if raw is None:
        return float("-inf")
    if isinstance(raw, tuple):
        mean, std = raw
        return mean + acquisition_beta * std
    return raw


def generate_batch(idx: dict, n: int, rng: random_mod.Random | None = None,
                    score_fn=None, keep_top_k: int | None = None, temperature: float = 1.0,
                    filler_count_decay: float | None = None, wear_bias: float = 0.0,
                    invalid_names: set | None = None, warm_names: set | None = None, stattrak_rate: float | None = None,
                    acquisition_beta: float = 0.0) -> list:
    """Draws up to n random candidates (retrying past the occasional None from
    random_candidate). If score_fn is given (sim_learning.predict_expected_roi), keeps only
    keep_top_k of them via temperature-controlled softmax-weighted sampling (see
    _softmax_weighted_sample) -- the "guided" importance-sampling step: once the surrogate
    model exists, spend simulate.py's (finite, and not free -- a cache miss falls through to a
    rate-limited live Steam fetch) budget on candidates it thinks are promising instead of n
    uniformly random ones, with `temperature`
    controlling how strictly "promising" is interpreted. Falls back to returning all n
    candidates, unscored, before a model exists.

    filler_count_decay/wear_bias: forwarded straight to random_candidate -- the per-agent
    personality knobs from agents.py (collection_mixing_bias/wear_optimization_bias).
    warm_names: forwarded straight to random_candidate -- see its own docstring/WARM_CACHE_BIAS.
    stattrak_rate: forwarded to random_candidate.
    acquisition_beta: when score_fn returns (mean, std) tuples, the ranking score is
    mean + acquisition_beta*std (UCB); 0.0 (default) or bare-float scores reproduce today's."""
    rng = rng or random_mod.Random()
    candidates = []
    attempts = 0
    while len(candidates) < n and attempts < n * 5:
        attempts += 1
        c = random_candidate(idx, rng, filler_count_decay=filler_count_decay, wear_bias=wear_bias,
                             invalid_names=invalid_names, warm_names=warm_names, stattrak_rate=stattrak_rate)
        if c is not None:
            candidates.append(c)
    if score_fn is None or keep_top_k is None:
        return candidates
    scored = [(score_fn(c), c) for c in candidates]
    if all(s is None for s, _ in scored):
        return candidates[:keep_top_k]
    scored = [(_acquisition_score(s, acquisition_beta), c) for s, c in scored]
    return _softmax_weighted_sample(rng, scored, keep_top_k, temperature)
