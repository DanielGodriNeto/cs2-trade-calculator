"""Centralized CS2 trade-up contract rules: contract sizes and structural validity.

Before this module existed, the standard trade-up's input count (10) was a bare
literal duplicated across tradeup.py's cost/breakeven math and separately re-declared
as random_trade.N_INPUTS -- only covert.py's COVERT_INPUT_COUNT=5 was a real shared
constant. This module is now the single source of truth for both; tradeup.py and
random_trade.py import from here instead of re-declaring, and covert.py's own
COVERT_INPUT_COUNT is kept as an alias so its existing importers (mixed_covert.py,
mixed_float_covert.py) don't need to change.

group_odds()/average_normalized_float()/remap_float_to_wear() (the actual probability
and float math) already live in oddsmath.py and are correct -- this module doesn't
re-implement them, it wraps data.py's rarity chain plus the two input-count constants
into one place agents can import, and adds validate_contract() so a multi-agent
search can check a randomly-generated or mutated candidate is structurally legal
BEFORE spending a cache lookup (let alone a live request) pricing it.
"""
from . import data as data_mod

STANDARD_INPUT_COUNT = 10   # normal collection-based trade-up (tradeup.py)
COVERT_INPUT_COUNT = 5      # Covert -> knife/glove trade-up (covert.py, Oct-2025 rules)


def input_count_for(contract_type: str = "standard") -> int:
    """contract_type: "standard" (default, collection-based) | "covert" (case-based
    Covert -> knife/glove path, see covert.py)."""
    if contract_type == "covert":
        return COVERT_INPUT_COUNT
    if contract_type == "standard":
        return STANDARD_INPUT_COUNT
    raise ValueError(f"unknown contract_type {contract_type!r}")


def is_legal_rarity_step(from_rarity_id: str, to_rarity_id: str) -> bool:
    """True if to_rarity_id is exactly one rung above from_rarity_id in
    data.RARITY_CHAIN -- a real trade-up contract only ever climbs one tier at a
    time, never skips or goes sideways/backwards."""
    if from_rarity_id not in data_mod.RARITY_INDEX or to_rarity_id not in data_mod.RARITY_INDEX:
        return False
    return data_mod.RARITY_INDEX[to_rarity_id] == data_mod.RARITY_INDEX[from_rarity_id] + 1


def _contract_type_of(candidate) -> str:
    # random_trade.py's generator only ever produces the standard 10-input contract
    # type today (a covert-path random generator doesn't exist yet) -- this reads an
    # explicit optional attribute rather than guessing from rarity names, so a future
    # covert-aware candidate generator can opt in unambiguously by setting it.
    return getattr(candidate, "contract_type", "standard")


def validate_contract(candidate) -> tuple[bool, str]:
    """Structural validity check for a random_trade.Candidate (or a mutation.py
    mutation of one) -- anything shaped like from_rarity_id/to_rarity_id/legs[].units[]
    with each unit a {"skin", "wear", "count"} dict. Returns (True, "") if legal,
    else (False, reason); never raises, so callers can just branch on the bool.

    This is checked BEFORE simulate.py ever touches the price cache, so agents never
    burn a lookup (let alone, via the Stage-2 verification queue, a live third-party
    request) on a contract that couldn't be a real trade-up in the first place.

    Only checks STRUCTURE (right total input count, legal rarity step, every input skin
    really at the contract's from-rarity, every referenced wear actually exists on that skin,
    and every leg's collection actually having outputs at the target rarity -- the real
    in-game rule: the contract UI refuses a skin whose collection has nothing at the next
    tier, so a candidate violating it isn't a long-shot trade, it's not a trade at all) --
    pricing/liquidity validity is simulate.py's job, not this module's."""
    if not is_legal_rarity_step(candidate.from_rarity_id, candidate.to_rarity_id):
        return False, "illegal_rarity_step"
    if not candidate.legs:
        return False, "no_legs"
    total_units = 0
    for leg in candidate.legs:
        if not leg.units:
            return False, "empty_leg"
        if not getattr(leg, "outputs", None):
            return False, "collection_has_no_outputs_at_target_rarity"
        for unit in leg.units:
            skin, wear, count = unit["skin"], unit["wear"], unit["count"]
            if count <= 0:
                return False, "non_positive_count"
            if skin.get("rarity_id") != candidate.from_rarity_id:
                return False, "input_rarity_mismatch"
            if wear not in skin.get("wears", []):
                return False, "wear_not_available_for_skin"
            total_units += count
    expected = input_count_for(_contract_type_of(candidate))
    if total_units != expected:
        return False, f"wrong_input_count (got {total_units}, need {expected})"
    return True, ""
