"""Mutation operators that improve an agent's own already-found candidates, instead of
drawing everything from scratch every round.

random_trade.py's generate_batch() only ever draws fresh, uniformly random candidates
(guided only by which of that random pool gets SELECTED for simulation, via
softmax-weighted sampling -- see its own docstring). This module is the other half of
the search strategy: given a candidate an agent already knows is good (its own
checkpoint's `best` list, see sim_loop._consider_best), produce a small variation of it
-- change the collection ratio, swap out an input skin, change a wear-mix, retarget
the rarity path, or move a whole leg to a different collection -- and let
sim_loop.run_round() simulate those alongside fresh random draws.

That last operator (_mutate_replace_collection) is what stops this module being a trap.
Every other operator keeps the candidate inside the collections it started in, so with
mutation_strength high enough to make mutants most of a round's batch, an agent could not
reach a collection its best-list didn't already contain -- measured at 0 new collections
across 400 mutants for four of the six sons. See that function's docstring. A mutation only survives into an agent's best-list if it actually beats what's
already there -- sim_loop._consider_best already only keeps the top BEST_KEPT by ROI,
so no extra "did this improve things" bookkeeping is needed here.

Every mutation operator either returns a new, structurally valid random_trade.Candidate
(guaranteed to pass rules_engine.validate_contract -- checked by mutate() itself before
returning) or None if that particular mutation wasn't applicable to the given candidate
(e.g. no OTHER skin exists in that collection to swap to) -- callers should treat None
as "try a different operator/candidate", never as an error.
"""
import random as random_mod
from copy import deepcopy

from . import data as data_mod
from . import random_trade as random_trade_mod
from . import rules_engine as rules_engine_mod


_REPLACE_COLLECTION_TRIES = 8  # how many shuffled candidate collections
# _mutate_replace_collection will try before giving up on the mutation -- see its docstring.


def _rarity_id_for_name(name: str) -> str | None:
    for rid, rname in data_mod.RARITY_CHAIN:
        if rname == name:
            return rid
    return None


def _adjacent_rarity_pairs() -> list:
    ids = [r for r, _ in data_mod.RARITY_CHAIN]
    return list(zip(ids, ids[1:]))


def candidate_from_best_entry(entry: dict, idx: dict) -> "random_trade_mod.Candidate | None":
    """Rebuilds a full random_trade.Candidate from one of sim_loop.py's checkpoint `best`
    entries -- those only store display-friendly names/summaries (skin NAME strings,
    rarity NAMES), not the live skin dicts / rarity ids the mutation operators below need
    to actually inspect and change. Returns None if any referenced skin/rarity/wear can no
    longer be resolved against the current skin database (e.g. an upstream data refresh
    renamed or dropped something) -- callers should just skip mutating that entry rather
    than crash."""
    from_id = _rarity_id_for_name(entry["from_rarity"])
    to_id = _rarity_id_for_name(entry["to_rarity"])
    if from_id is None or to_id is None:
        return None
    skins_by_name = {s["name"]: s for s in data_mod.load_skins()}

    legs_by_collection: dict = {}
    for row in entry["input_mix"]:
        skin = skins_by_name.get(row["skin"])
        if skin is None or row["wear"] not in skin.get("wears", []):
            return None
        legs_by_collection.setdefault(row["collection"], []).append(
            {"skin": skin, "wear": row["wear"], "count": row["count"]})
    if not legs_by_collection:
        return None

    legs = []
    for collection, units in legs_by_collection.items():
        outputs = idx.get(collection, {}).get(to_id, [])
        if entry["stattrak"]:
            outputs = [s for s in outputs if s["stattrak"]]
        if not outputs:
            return None
        legs.append(random_trade_mod.CandidateLeg(collection=collection, units=units, outputs=outputs))
    # Keep the original primary-leg-first ordering (legs[0] convention elsewhere).
    legs.sort(key=lambda l: 0 if l.collection == entry["primary_collection"] else 1)

    from_name = dict(data_mod.RARITY_CHAIN)[from_id]
    to_name = dict(data_mod.RARITY_CHAIN)[to_id]
    return random_trade_mod.Candidate(from_rarity_id=from_id, from_rarity_name=from_name,
                                       to_rarity_id=to_id, to_rarity_name=to_name,
                                       stattrak=entry["stattrak"], legs=legs)


def _copy_legs(candidate) -> list:
    return [random_trade_mod.CandidateLeg(collection=l.collection, units=deepcopy(l.units), outputs=l.outputs)
            for l in candidate.legs]


def _rebuild(candidate, legs: list) -> "random_trade_mod.Candidate | None":
    legs = [l for l in legs if l.units and sum(u["count"] for u in l.units) > 0]
    if not legs:
        return None
    return random_trade_mod.Candidate(from_rarity_id=candidate.from_rarity_id,
                                       from_rarity_name=candidate.from_rarity_name,
                                       to_rarity_id=candidate.to_rarity_id,
                                       to_rarity_name=candidate.to_rarity_name,
                                       stattrak=candidate.stattrak, legs=legs,
                                       contract_type=candidate.contract_type)


def _reduce_leg_units(units: list, amount: int, rng: random_mod.Random) -> list:
    """Reduces a leg's total unit count by `amount` (caller guarantees amount < the leg's
    total, so this never empties the leg entirely), shrinking/removing individual unit
    entries in shuffled order so no single skin/wear is always the one that gets cut."""
    units = deepcopy(units)
    remaining = amount
    order = list(range(len(units)))
    rng.shuffle(order)
    for i in order:
        if remaining <= 0:
            break
        take = min(units[i]["count"], remaining)
        units[i]["count"] -= take
        remaining -= take
    return [u for u in units if u["count"] > 0]


def _add_to_leg(leg, amount: int, rng: random_mod.Random) -> list:
    """Adds `amount` more total input units to a leg, bumping a randomly chosen EXISTING
    (skin, wear) entry rather than introducing a new skin choice -- the simplest possible
    diff from the original candidate (introducing a new skin is _mutate_replace_input's
    job instead)."""
    units = deepcopy(leg.units)
    target = rng.choice(units)
    target["count"] += amount
    return units


# --- Operators -----------------------------------------------------------------------

def _mutate_collection_ratio(candidate, idx: dict, rng: random_mod.Random):
    """Shifts a few input units from one leg to another (spec example: 7A/3B -> 5A/5B),
    or, if the candidate is currently single-collection, introduces a new filler
    collection at a small share of the inputs (spec example: 5A/5B -> 4A/5B/1C)."""
    legs = _copy_legs(candidate)
    if len(legs) >= 2:
        donors = [l for l in legs if sum(u["count"] for u in l.units) > 1]
        if not donors:
            return None
        donor = rng.choice(donors)
        recipient = rng.choice([l for l in legs if l is not donor])
        donor_total = sum(u["count"] for u in donor.units)
        shift = rng.randint(1, min(3, donor_total - 1))
        donor.units = _reduce_leg_units(donor.units, shift, rng)
        recipient.units = _add_to_leg(recipient, shift, rng)
    else:
        leg = legs[0]
        total = sum(u["count"] for u in leg.units)
        if total < 2:
            return None
        eligible = [c for c in idx if c != leg.collection
                    and idx[c].get(candidate.from_rarity_id) and idx[c].get(candidate.to_rarity_id)]
        if not eligible:
            return None
        new_collection = rng.choice(eligible)
        by_rarity = idx[new_collection]
        inputs = by_rarity.get(candidate.from_rarity_id, [])
        outputs = by_rarity.get(candidate.to_rarity_id, [])
        if candidate.stattrak:
            inputs = [s for s in inputs if s["stattrak"]]
            outputs = [s for s in outputs if s["stattrak"]]
        if not inputs or not outputs:
            return None
        shift = rng.randint(1, min(3, total - 1))
        skin = rng.choice(inputs)
        wear = rng.choice(skin["wears"])
        leg.units = _reduce_leg_units(leg.units, shift, rng)
        legs.append(random_trade_mod.CandidateLeg(
            collection=new_collection, units=[{"skin": skin, "wear": wear, "count": shift}], outputs=outputs))
    return _rebuild(candidate, legs)


def _replaceable(leg, from_rarity_id: str, idx: dict, stattrak: bool):
    pool = idx.get(leg.collection, {}).get(from_rarity_id, [])
    if stattrak:
        pool = [s for s in pool if s["stattrak"]]
    current_names = {u["skin"]["name"] for u in leg.units}
    return [s for s in pool if s["name"] not in current_names]


def _mutate_replace_input(candidate, idx: dict, rng: random_mod.Random):
    """Swaps ONE input unit's skin for a different skin from the same collection/rarity
    pool, keeping its count."""
    legs = _copy_legs(candidate)
    leg = rng.choice(legs)
    alternatives = _replaceable(leg, candidate.from_rarity_id, idx, candidate.stattrak)
    if not alternatives or not leg.units:
        return None
    unit = rng.choice(leg.units)
    new_skin = rng.choice(alternatives)
    unit["skin"] = new_skin
    unit["wear"] = rng.choice(new_skin["wears"])
    return _rebuild(candidate, legs)


def _mutate_replace_multiple_inputs(candidate, idx: dict, rng: random_mod.Random):
    """Same as _mutate_replace_input but swaps 2-3 units at once, possibly across
    different legs -- a bigger single step through the search space."""
    legs = _copy_legs(candidate)
    all_units = [(leg, u) for leg in legs for u in leg.units]
    if len(all_units) < 2:
        return None
    n_replace = rng.randint(2, min(3, len(all_units)))
    chosen = rng.sample(all_units, n_replace)
    changed = False
    for leg, unit in chosen:
        alternatives = _replaceable(leg, candidate.from_rarity_id, idx, candidate.stattrak)
        if not alternatives:
            continue
        new_skin = rng.choice(alternatives)
        unit["skin"] = new_skin
        unit["wear"] = rng.choice(new_skin["wears"])
        changed = True
    if not changed:
        return None
    return _rebuild(candidate, legs)


def _mutate_wear_mix(candidate, idx: dict, rng: random_mod.Random):
    """Changes ONE input unit's wear to a different wear the same skin is available at --
    the wear-category equivalent of float manipulation (see oddsmath.py/float_mix.py):
    tests whether a different wear bracket on an already-good candidate does better."""
    legs = _copy_legs(candidate)
    all_units = [u for leg in legs for u in leg.units]
    if not all_units:
        return None
    unit = rng.choice(all_units)
    alternatives = [w for w in unit["skin"].get("wears", []) if w != unit["wear"]]
    if not alternatives:
        return None
    unit["wear"] = rng.choice(alternatives)
    return _rebuild(candidate, legs)


def _mutate_replace_collection(candidate, idx: dict, rng: random_mod.Random):
    """Repoints ONE leg at a different eligible collection, keeping that leg's unit count and
    drawing a fresh skin/wear mix from the new collection's input pool.

    This is the operator that lets a mutant leave the collections its parent was built from at
    all. Without it the set above can only introduce a new collection through
    _mutate_collection_ratio's else-branch, which fires only when the candidate is SINGLE-leg --
    so every multi-leg parent was permanently confined to its own collections, and the agents
    spend most of their budget on mutants (see sim_loop.run_round's mutation_strength blend).
    Measured on the six sons' real best-lists, 400 mutants each:

        son   collections in best-50   reached by old operators   with this one
         0            4                        4  (0 new)          50  (46 new)
         2            4                        4  (0 new)          54  (50 new)
         3            5                        5  (0 new)          42  (37 new)
         5            7                        7  (0 new)          46  (39 new)

    Sons 1 and 4 already escaped a little (18->30, 11->21) because more of their best entries
    are single-leg; they go to 49 and 59. All 2,400 results passed validate_contract.

    Deliberately a WHOLE-leg move rather than a per-unit one: a leg is the unit CS2's
    group-based odds actually operate on (see oddsmath.group_odds), so swapping a leg's
    collection produces a contract with a coherent, differently-shaped output distribution
    instead of a leg whose units disagree about which collection they belong to -- which the
    rest of the pipeline (simulate.py's per-leg pricing, sim_learning's n_legs/filler_fraction
    features) assumes cannot happen. Tries a handful of shuffled candidate collections rather
    than just one so a collection whose input pool is entirely invalid/unpriceable right now
    doesn't waste the whole mutation attempt."""
    legs = _copy_legs(candidate)
    leg = rng.choice(legs)
    in_use = {l.collection for l in legs}
    eligible = [c for c in idx
                if c not in in_use
                and idx[c].get(candidate.from_rarity_id) and idx[c].get(candidate.to_rarity_id)]
    if not eligible:
        return None
    total = sum(u["count"] for u in leg.units)
    if total <= 0:
        return None
    rng.shuffle(eligible)
    for new_collection in eligible[:_REPLACE_COLLECTION_TRIES]:
        by_rarity = idx[new_collection]
        inputs = by_rarity.get(candidate.from_rarity_id, [])
        outputs = by_rarity.get(candidate.to_rarity_id, [])
        if candidate.stattrak:
            inputs = [s for s in inputs if s["stattrak"]]
            outputs = [s for s in outputs if s["stattrak"]]
        if not inputs or not outputs:
            continue
        # Reuses random_trade's own skin/wear mixer, so a moved leg is built exactly the way a
        # freshly drawn one would be (several distinct skins, per-unit wears) instead of the
        # single-skin block the other operators leave behind.
        units = random_trade_mod._random_skin_mix(rng, inputs, total, stattrak=candidate.stattrak)
        if units is None:  # every input skin here is unpriceable right now -- try another
            continue
        leg.collection = new_collection
        leg.units = units
        leg.outputs = outputs
        return _rebuild(candidate, legs)
    return None


def _mutate_rarity_path(candidate, idx: dict, rng: random_mod.Random):
    """Retargets the whole contract to a DIFFERENT adjacent rarity pair, keeping the same
    collections where they're still eligible there -- e.g. was climbing Restricted->
    Classified, try Mil-Spec->Restricted instead with the same collection mix."""
    pairs = [p for p in _adjacent_rarity_pairs() if p != (candidate.from_rarity_id, candidate.to_rarity_id)]
    rng.shuffle(pairs)
    for from_id, to_id in pairs:
        legs = []
        ok = True
        for old_leg in candidate.legs:
            by_rarity = idx.get(old_leg.collection, {})
            inputs = by_rarity.get(from_id, [])
            outputs = by_rarity.get(to_id, [])
            if candidate.stattrak:
                inputs = [s for s in inputs if s["stattrak"]]
                outputs = [s for s in outputs if s["stattrak"]]
            if not inputs or not outputs:
                ok = False
                break
            total = sum(u["count"] for u in old_leg.units)
            skin = rng.choice(inputs)
            wear = rng.choice(skin["wears"])
            legs.append(random_trade_mod.CandidateLeg(collection=old_leg.collection,
                                                        units=[{"skin": skin, "wear": wear, "count": total}],
                                                        outputs=outputs))
        if not ok:
            continue
        from_name = dict(data_mod.RARITY_CHAIN)[from_id]
        to_name = dict(data_mod.RARITY_CHAIN)[to_id]
        return random_trade_mod.Candidate(from_rarity_id=from_id, from_rarity_name=from_name,
                                           to_rarity_id=to_id, to_rarity_name=to_name,
                                           stattrak=candidate.stattrak, legs=legs)
    return None


_OPERATORS = [
    _mutate_collection_ratio,
    _mutate_replace_input,
    _mutate_replace_multiple_inputs,
    _mutate_wear_mix,
    _mutate_rarity_path,
    _mutate_replace_collection,
]


def mutate(candidate, idx: dict, rng: random_mod.Random) -> "random_trade_mod.Candidate | None":
    """Applies ONE randomly chosen mutation operator to `candidate`, trying the remaining
    operators (in shuffled order) if the first doesn't apply (e.g. no second skin exists
    to swap to). Always validates the result via rules_engine.validate_contract before
    returning -- callers can trust every non-None return is a real, legal trade-up
    contract, never a half-built one."""
    ops = list(_OPERATORS)
    rng.shuffle(ops)
    for op in ops:
        mutated = op(candidate, idx, rng)
        if mutated is None:
            continue
        valid, _ = rules_engine_mod.validate_contract(mutated)
        if valid:
            return mutated
    return None


def mutate_batch(best_entries: list, idx: dict, rng: random_mod.Random, n: int,
                  track_parents: dict | None = None) -> list:
    """Produces up to n mutated candidates by reconstructing + mutating random entries
    from an agent's own best-known list (sim_loop.py checkpoint's `best`) -- the "improve
    existing successful trade-ups" half of the search, complementing
    random_trade.generate_batch's fresh-random half (see sim_loop.run_round, which blends
    the two per-round in a mutation_strength-controlled ratio). Skips, never raises on, an
    entry that can no longer be reconstructed (see candidate_from_best_entry) or that
    fails every mutation attempt -- returns fewer than n in that case rather than error.

    track_parents (optional): a caller-owned dict this function fills with
    id(candidate) -> the source entry's "id" -- genome-lineage bookkeeping for
    evolution.py's audit trail (sim_loop reads it back at best-list admission time).
    Keyed by object identity because a Candidate has no id of its own until simulate.py
    assigns the resulting record one; the caller consumes the mapping within the same
    round, so identity keys never outlive their objects."""
    if not best_entries:
        return []
    out = []
    attempts = 0
    while len(out) < n and attempts < n * 5:
        attempts += 1
        entry = rng.choice(best_entries)
        base = candidate_from_best_entry(entry, idx)
        if base is None:
            continue
        mutated = mutate(base, idx, rng)
        if mutated is not None:
            if track_parents is not None and entry.get("id"):
                track_parents[id(mutated)] = entry["id"]
            out.append(mutated)
    return out
