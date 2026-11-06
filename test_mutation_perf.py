"""Self-check for src.mutation perf fix: mutate_batch loads the skin JSON at most once per call,
candidate_from_best_entry still works without the new skins_by_name kwarg, and an empty batch
returns [] without touching load_skins.

Run: python test_mutation_perf.py
"""
import random

from src import data as data_mod
from src import mutation as mutation_mod

real_load_skins = data_mod.load_skins
load_calls = []


def counting_load_skins(force_refresh: bool = False) -> list:
    load_calls.append(1)
    return real_load_skins(force_refresh)


# Stub entries: rarity names are real (so the early rarity lookup passes and load_skins is
# reached), but the skin name is not in the database, so every rebuild returns None. Mutation
# therefore never runs and the test only measures how often the skin database is loaded.
entries = [
    {"id": f"e{i}", "from_rarity": "Consumer Grade", "to_rarity": "Industrial Grade",
     "stattrak": False, "primary_collection": "Stub Collection",
     "input_mix": [{"skin": "No Such Skin", "wear": "Factory New", "collection": "Stub Collection", "count": 10}]}
    for i in range(4)
]

data_mod.load_skins = counting_load_skins
try:
    out = mutation_mod.mutate_batch(entries, {}, random.Random(0), 6)
    assert out == [], out
    assert len(load_calls) <= 1, f"load_skins called {len(load_calls)} times, expected at most 1"

    # Empty batch: early return, no load at all.
    load_calls.clear()
    assert mutation_mod.mutate_batch([], {}, random.Random(0), 3) == []
    assert load_calls == [], load_calls

    # Positional call without the new kwarg still works and still loads the database itself.
    load_calls.clear()
    assert mutation_mod.candidate_from_best_entry(entries[0], {}) is None
    assert len(load_calls) == 1, load_calls

    # With a prebuilt map, no load happens.
    load_calls.clear()
    assert mutation_mod.candidate_from_best_entry(entries[0], {}, skins_by_name={}) is None
    assert load_calls == [], load_calls
finally:
    data_mod.load_skins = real_load_skins

print("ok")
