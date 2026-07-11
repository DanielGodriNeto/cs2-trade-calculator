"""User-maintained blacklist of trade-up opportunities that have already been "done".

The guided random-trade simulation (sim_loop.py) keeps re-discovering the same profitable
trade-up composition round after round, so the same opportunity shows up over and over in the
dashboard's "Trade-up opportunities" table. Once the user has actually executed one (this tool
never auto-buys/sells -- see the project's core rule), they don't want to keep seeing it. This
module lets them mark a specific trade-up as done; from then on it is hidden from the
opportunity list and never re-admitted to any son's best-known list.

WHY THIS FILE IS SHARED ACROSS SONS (and does NOT violate the per-son isolation rule):
a trade-up composition is the same trade-up no matter which son happened to find it, and "I
already did this one" is a USER preference, not simulation data or a trained model. The strict
isolation guardrail in sim_loop.py/sim_learning.py is about never letting one son's simulated
trials or surrogate model leak into another son's training/ranking -- this blacklist is neither,
so a single shared data/sim_blacklist.json is correct here.

Identity: each SimulationRecord gets a fresh time-based `id` every time it's simulated
(simulate._new_id), so the id can't identify "the same trade-up." Instead we key on the
CONTENT signature -- the from/to rarity, StatTrak flag, and the multiset of input units
(skin, wear, count). Two records with the same signature are the same contract; the outputs
are a deterministic function of those inputs, so they don't need to be part of the key.
"""
import hashlib
import json
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BLACKLIST_FILE = DATA_DIR / "sim_blacklist.json"

_lock = threading.Lock()
_loaded = False
_entries: list = []              # full records, for the management UI (sig + human-readable summary)
_sigs: set = set()               # just the signatures, for fast is_blacklisted() lookups on the hot path


def signature(from_rarity, to_rarity, stattrak, input_mix) -> str:
    """Stable content hash identifying a trade-up composition regardless of when/which son
    produced it. `input_mix` is a list of dicts with at least skin/wear/count (the same shape
    on both a SimulationRecord and a stored checkpoint 'best' entry)."""
    units = sorted(
        f"{u.get('skin', '')}|{u.get('wear', '')}|{int(u.get('count', 0))}"
        for u in (input_mix or [])
    )
    key = f"{from_rarity}|{to_rarity}|{int(bool(stattrak))}|" + ";".join(units)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def signature_of(entry) -> str:
    """Convenience: derive a signature from a dict-like entry (checkpoint 'best' row or the
    dashboard payload) that carries from_rarity/to_rarity/stattrak/input_mix."""
    return signature(entry.get("from_rarity"), entry.get("to_rarity"),
                     entry.get("stattrak"), entry.get("input_mix"))


def _load() -> None:
    global _loaded, _entries, _sigs
    if _loaded:
        return
    if BLACKLIST_FILE.exists():
        try:
            data = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8"))
            _entries = list(data.get("blacklist", []))
        except (json.JSONDecodeError, OSError):
            _entries = []
    else:
        _entries = []
    _sigs = {e["signature"] for e in _entries if e.get("signature")}
    _loaded = True


def _persist() -> None:
    BLACKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    BLACKLIST_FILE.write_text(json.dumps({"blacklist": _entries}), encoding="utf-8")


def is_blacklisted(entry) -> bool:
    """Hot-path check used by sim_loop._consider_best and list_best_simulations."""
    with _lock:
        _load()
        return signature_of(entry) in _sigs


def add(entry) -> dict:
    """Mark a trade-up (given as a dashboard/checkpoint entry) as done. Idempotent -- adding an
    already-blacklisted composition just returns the existing record. Stores a small
    human-readable summary alongside the signature for the management UI."""
    sig = signature_of(entry)
    with _lock:
        _load()
        if sig in _sigs:
            return next((e for e in _entries if e["signature"] == sig), {"signature": sig})
        record = {
            "signature": sig,
            "primary_collection": entry.get("primary_collection"),
            "from_rarity": entry.get("from_rarity"),
            "to_rarity": entry.get("to_rarity"),
            "stattrak": bool(entry.get("stattrak")),
            "n_inputs": sum(int(u.get("count", 0)) for u in (entry.get("input_mix") or [])),
            "expected_roi_pct": entry.get("expected_roi_pct"),
            "added_at": time.time(),
        }
        _entries.append(record)
        _sigs.add(sig)
        _persist()
        return record


def remove(sig: str) -> bool:
    """Un-blacklist a signature (the user changed their mind). Returns True if it was present."""
    global _entries
    with _lock:
        _load()
        if sig not in _sigs:
            return False
        _entries = [e for e in _entries if e.get("signature") != sig]
        _sigs.discard(sig)
        _persist()
        return True


def list_entries() -> list:
    """All blacklisted trade-ups, newest first -- for the dashboard's management list."""
    with _lock:
        _load()
        return sorted(_entries, key=lambda e: e.get("added_at", 0), reverse=True)
