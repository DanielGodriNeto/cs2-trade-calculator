"""Self-check for src.mispriced_skins: corrupt registry is quarantined (not overwritten),
transient read errors propagate without touching the file, saves are atomic, and
record_from_rows saves once per batch. Uses a temp dir, never data/.

Run: python test_mispriced_skins.py
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import mispriced_skins as m

tmp_dir = Path(tempfile.mkdtemp())
m.DATA_DIR = tmp_dir
m.MISPRICED_FILE = tmp_dir / "mispriced_skins.json"

# Corrupt file -> loads as empty, original moved to a quarantine copy, nothing overwritten.
corrupt = '{"skins": {"0:AK-47 | Redline (Field-Tested)": {"gap_pct": 30'
m.MISPRICED_FILE.write_text(corrupt, encoding="utf-8")
m.forget_all()
assert m.list_mispriced() == []
assert not m.MISPRICED_FILE.exists()
quarantined = list(tmp_dir.glob("mispriced_skins.json.corrupt-*"))
assert len(quarantined) == 1, quarantined
assert quarantined[0].read_text(encoding="utf-8") == corrupt

# Next save writes a valid file and leaves the quarantine copy untouched (no temp leftovers).
m.mark("AK-47 | Redline", "Field-Tested", False, 1, 30.0, "skinport", 130.0, 100.0)
saved = json.loads(m.MISPRICED_FILE.read_text(encoding="utf-8"))
assert len(saved["skins"]) == 1
assert quarantined[0].read_text(encoding="utf-8") == corrupt
assert sorted(p.name for p in tmp_dir.iterdir()) == sorted(
    [m.MISPRICED_FILE.name, quarantined[0].name])

# Transient read OSError propagates, leaves _store unloaded and the file byte-identical.
good_bytes = m.MISPRICED_FILE.read_bytes()
m.forget_all()
with mock.patch.object(type(m.MISPRICED_FILE), "read_text", side_effect=OSError("sharing violation")):
    try:
        m.list_mispriced()
    except OSError:
        raised = True
    else:
        raised = False
assert raised, "transient OSError must propagate, not load an empty store"
assert m._store is None, "_store must stay unloaded after a failed read"
assert m.MISPRICED_FILE.read_bytes() == good_bytes
assert len(list(tmp_dir.glob("mispriced_skins.json.corrupt-*"))) == 1  # no new quarantine
# Once the patch is gone, a later call loads the real marks.
loaded = m.list_mispriced()
assert len(loaded) == 1 and loaded[0]["skin"] == "AK-47 | Redline"

# record_from_rows: one save per batch, same marked/cleared counts.
m.forget_all()
m.mark("AWP | Asiimov", "Field-Tested", False, 1, 50.0, "buff163", 90.0, 60.0)  # to be cleared
m.forget_all()
rows = [
    SimpleNamespace(skin="AK-47 | Redline", wear="Field-Tested", stattrak=False,
                    steam_vs_cheapest_pct=30.0, cheapest_source="skinport",
                    steam_price=130.0, cheapest_price=100.0),
    SimpleNamespace(skin="M4A4 | Howl", wear="Minimal Wear", stattrak=False,
                    steam_vs_cheapest_pct=-25.0, cheapest_source="csfloat",
                    steam_price=3000.0, cheapest_price=4000.0),
    SimpleNamespace(skin="AWP | Asiimov", wear="Field-Tested", stattrak=False,
                    steam_vs_cheapest_pct=2.0, cheapest_source="buff163",
                    steam_price=61.0, cheapest_price=60.0),
    SimpleNamespace(skin="Glock-18 | Fade", wear="Factory New", stattrak=False,
                    steam_vs_cheapest_pct=None, cheapest_source="skinport",
                    steam_price=None, cheapest_price=None),
]
real_save = m._save_locked
save_calls = []


def counting_save() -> None:
    save_calls.append(1)
    real_save()


m.mark("AWP | Asiimov", "Field-Tested", False, 1, 50.0, "buff163", 90.0, 60.0)
with mock.patch.object(m, "_save_locked", side_effect=counting_save):
    result = m.record_from_rows(rows, 1)
assert result == {"marked": 2, "cleared": 1}, result
assert len(save_calls) == 1, save_calls
assert {e["skin"] for e in m.list_mispriced(1)} == {"AK-47 | Redline", "M4A4 | Howl"}

print("ok")
