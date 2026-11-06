"""Self-check for src.pattern_scanner._resolve_item cross-check: a decoded float/paint seed that
disagrees with Steam's listing properties is rejected as unverified; one source alone behaves as
before. Every network/disk path is mocked (decoder patched, cache pre-marked loaded, nothing saved).

Run: python test_pattern_decode.py
"""
from unittest import mock

from src import pattern_scanner as ps

_counter = [0]


def resolve(decoded: dict | None, prop_seed: int | None, prop_float: float | None) -> dict | None:
    """Resolve a fresh (uncached) link with the decoder mocked to return `decoded`."""
    _counter[0] += 1
    link = f"steam://run/730//+csgo_econ_action_preview test{_counter[0]}"
    ps._inspect_cache_loaded = True  # keep the disk cache out of it
    with mock.patch.object(ps.inspect_decode, "decode_inspect_link", return_value=decoded):
        return ps._resolve_item(link, market_hash_name="AK-47 | Case Hardened", sticker_names=[],
                                allow_api=False, prop_paint_seed=prop_seed, prop_float=prop_float)


def mismatches() -> int:
    return ps._stats["unverified_mismatch"]


DECODED = {"float_value": 0.1234567, "paint_seed": 661, "paint_index": None,
           "def_index": 7, "sticker_ids": []}

# match: both sources agree (float within 1e-6, seed exact) -> resolved, not counted
before = mismatches()
item = resolve(DECODED, 661, 0.1234567 + 5e-7)
assert item is not None and item["paint_seed"] == 661, item
assert mismatches() == before

# match: exact float as well
assert resolve(DECODED, 661, 0.1234567) is not None

# mismatch: paint seed differs by one
before = mismatches()
assert resolve(DECODED, 662, 0.1234567) is None
assert mismatches() == before + 1

# mismatch: float differs beyond tolerance (2e-6 > 1e-6)
before = mismatches()
assert resolve(DECODED, 661, 0.1234567 + 2e-6) is None
assert mismatches() == before + 1
assert ps._stats["inspect_last_error"] and "unverified" in ps._stats["inspect_last_error"]

# single source: decode only (Steam published no props) -> behaves as before
before = mismatches()
item = resolve(DECODED, None, None)
assert item is not None and item["float_value"] == 0.1234567, item
assert mismatches() == before

# single source: Steam props only (decoder cannot read the link) -> behaves as before
before = mismatches()
item = resolve(None, 661, 0.5)
assert item is not None and item["paint_seed"] == 661 and item["float_value"] == 0.5, item
assert mismatches() == before

# single source: decode gave no float but the seed agrees -> no verdict on the missing field
before = mismatches()
assert resolve({"float_value": None, "paint_seed": 661}, 661, 0.9) is not None
assert mismatches() == before

print("ok")
