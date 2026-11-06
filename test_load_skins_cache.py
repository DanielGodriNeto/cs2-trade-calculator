"""Self-check for src.data.load_skins caching: two consecutive calls parse the raw skin JSON
once, and a bumped SKINS_CACHE mtime forces a reload. Uses a temp file, never the real cache.

Run: python test_load_skins_cache.py
"""
import json
import os
import tempfile
from pathlib import Path

from src import data as data_mod

MINIMAL_SKINS = [{
    "name": "AK-47 | Test",
    "weapon": {"name": "AK-47"},
    "rarity": {"id": "rarity_rare_weapon", "name": "Mil-Spec Grade"},
    "collections": [{"name": "The Test Collection"}],
    "wears": [{"name": "Field-Tested"}],
}]

real_skins_cache = data_mod.SKINS_CACHE
real_load_raw = data_mod.load_raw_skins
raw_calls = []


def counting_load_raw(force_refresh: bool = False) -> list:
    raw_calls.append(force_refresh)
    return real_load_raw(force_refresh)


with tempfile.TemporaryDirectory() as tmp:
    tmp_file = Path(tmp) / "skins.json"
    tmp_file.write_text(json.dumps(MINIMAL_SKINS), encoding="utf-8")
    data_mod.SKINS_CACHE = tmp_file
    data_mod.load_raw_skins = counting_load_raw
    try:
        first = data_mod.load_skins()
        second = data_mod.load_skins()
        assert len(raw_calls) == 1, f"expected 1 raw load after 2 calls, got {len(raw_calls)}"
        assert second is first, "cached call should return the same list object"
        assert [s["name"] for s in first] == ["AK-47 | Test"]

        t = tmp_file.stat().st_mtime + 10
        os.utime(tmp_file, (t, t))
        third = data_mod.load_skins()
        assert len(raw_calls) == 2, f"expected reload after mtime bump, got {len(raw_calls)} raw loads"
        assert third is not first

        data_mod.load_skins(force_refresh=True)
        assert len(raw_calls) == 3, "force_refresh must bypass the cache"
    finally:
        data_mod.SKINS_CACHE = real_skins_cache
        data_mod.load_raw_skins = real_load_raw

print("ok")
