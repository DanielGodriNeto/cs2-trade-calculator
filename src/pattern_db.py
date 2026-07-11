"""Pattern Knowledge Database: the centralized, VERSIONED store of publicly documented
collector-pattern knowledge (Blue Gems, Heat Treated, Doppler phases, Marble Fade Fire & Ice,
Katowice/Crown sticker crafts, ...) that the Rare Pattern Arbitrage Engine
(pattern_scanner.py) matches marketplace listings against.

Hard rules, straight from the spec:
  - Pattern knowledge comes EXCLUSIVELY from documented public collector consensus (every
    entry links its source). No machine learning may ever invent, infer or extend a rare
    pattern -- ML's only permitted role in the whole engine is prioritizing already-matched
    opportunities, and even that is currently a plain transparent scoring formula
    (pattern_scanner._score_opportunity), not a model.
  - Nothing is hardcoded through the application: this module is the single source of truth.
    A conservative seed (src/pattern_seed_knowledge.json, shipped with the code) is copied to
    data/pattern_db.json on first use; the data/ copy is the LIVE database from then on.
  - Every modification is versioned: refresh_knowledge() bumps the version and appends a diff
    summary to data/pattern_db_history.jsonl (append-only audit trail, same JSONL convention
    as every other log in this project), so tier/premium changes are always auditable.

Entry types the scanner understands:
  - "paint_seed":    match by paint seed list (Case Hardened tiers, Heat Treated, Marble Fade
                     Fire & Ice, ...). weapon/skin are exact names ("★ Karambit" for knives).
  - "paint_index":   match by paint index (Doppler/Gamma Doppler phases -- Ruby=415,
                     Sapphire=416, Black Pearl=417, Emerald=568, ... -- the finish catalog id
                     IS the phase, no seed needed). weapon "*" matches any weapon.
  - "sticker_craft": match by sticker name substring(s) on the listing (Katowice 2014 holos,
                     Crown Foils). Premiums for crafts are screening floors only -- craft value
                     is sticker/position-dependent and flagged for manual review.

Automatic knowledge verification: refresh_knowledge() re-validates the live DB against the
bundled seed (picking up entries added in newer code versions without ever overwriting local
edits -- local wins on conflict) and re-stamps last_verified_at. TRUE remote verification
against community resources (CSBlueGem, FloatDB, Steam guides) is exposed as a pluggable hook
(REMOTE_VERIFIERS) that currently ships EMPTY: those sites offer no stable public API and
scraping them would break silently and violate their terms -- documented limitation, surfaced
in get_status() rather than hidden (the spec's own "if you can't implement it, say so" rule).
Sources are still linked on every entry for one-click manual verification.
"""
import json
import threading
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
DATA_DIR = SRC_DIR.parent / "data"
SEED_FILE = SRC_DIR / "pattern_seed_knowledge.json"
DB_FILE = DATA_DIR / "pattern_db.json"
HISTORY_FILE = DATA_DIR / "pattern_db_history.jsonl"

REFRESH_INTERVAL_SECONDS = 24 * 3600  # periodic re-verification cadence (see refresh_due)

# Pluggable remote verifiers: {source_key: callable(db) -> list[change dict]}. Ships empty --
# see module docstring. A future phase can register e.g. a CSBlueGem fetcher here without
# touching any other code.
REMOTE_VERIFIERS: dict = {}

_lock = threading.Lock()
_db_cache: dict = {"mtime": None, "db": None}


def _load_seed() -> dict:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def _append_history(event: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def load_db() -> dict:
    """The live pattern database (data/pattern_db.json), initialized from the bundled seed on
    first use. mtime-cached, same convention as market_reliability._history_index."""
    with _lock:
        if not DB_FILE.exists():
            seed = _load_seed()
            db = {"schema_version": seed["schema_version"],
                  "knowledge_version": seed["knowledge_version"],
                  "sources": seed["sources"], "entries": seed["entries"],
                  "created_at": time.time(), "last_verified_at": None,
                  "last_refresh_result": None}
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            DB_FILE.write_text(json.dumps(db, indent=1), encoding="utf-8")
            _append_history({"event": "initialized_from_seed",
                             "knowledge_version": db["knowledge_version"],
                             "n_entries": len(db["entries"]), "at": time.time()})
        mtime = DB_FILE.stat().st_mtime
        if _db_cache["mtime"] == mtime and _db_cache["db"] is not None:
            return _db_cache["db"]
        try:
            db = json.loads(DB_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt live DB: fall back to the seed IN MEMORY (never overwrite the corrupt
            # file automatically -- the user may want to inspect/repair it).
            seed = _load_seed()
            db = {"schema_version": seed["schema_version"],
                  "knowledge_version": seed["knowledge_version"], "sources": seed["sources"],
                  "entries": seed["entries"], "corrupt_on_disk": True}
        _db_cache["mtime"] = mtime
        _db_cache["db"] = db
        return db


def _save_db(db: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_FILE.write_text(json.dumps(db, indent=1), encoding="utf-8")
    with _lock:
        _db_cache["mtime"] = None  # force re-read


def entries(entry_type: str | None = None) -> list[dict]:
    db = load_db()
    out = db.get("entries") or []
    if entry_type:
        out = [e for e in out if e.get("type") == entry_type]
    return out


def source_url(source_key: str) -> str | None:
    for s in load_db().get("sources") or []:
        if s.get("key") == source_key:
            return s.get("url")
    return None


def refresh_due() -> bool:
    db = load_db()
    last = db.get("last_verified_at")
    return last is None or (time.time() - last) >= REFRESH_INTERVAL_SECONDS


def refresh_knowledge() -> dict:
    """Automatic knowledge verification pass: merges any NEW seed entries from the installed
    code version into the live DB (never overwriting locally-modified entries -- local wins),
    runs every registered remote verifier (none ship enabled, see module docstring), bumps the
    knowledge version if anything changed, and logs the whole result to the version-history
    audit trail."""
    db = load_db()
    db = json.loads(json.dumps(db))  # deep copy -- never mutate the shared cache in place
    seed = _load_seed()
    by_id = {e["id"]: e for e in db.get("entries") or []}
    changes = []
    for entry in seed.get("entries") or []:
        if entry["id"] not in by_id:
            db["entries"].append(entry)
            changes.append({"change": "entry_added_from_seed", "id": entry["id"]})
    remote_results = {}
    for source_key, verifier in REMOTE_VERIFIERS.items():
        try:
            remote_changes = verifier(db) or []
            changes.extend(remote_changes)
            remote_results[source_key] = {"ok": True, "n_changes": len(remote_changes)}
        except Exception as e:  # noqa: BLE001 -- a broken verifier must never kill the refresh
            remote_results[source_key] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    now = time.time()
    db["last_verified_at"] = now
    db["last_refresh_result"] = {
        "at": now, "n_changes": len(changes),
        "remote_verifiers": remote_results or None,
        "remote_verification_available": bool(REMOTE_VERIFIERS),
    }
    if changes:
        db["knowledge_version"] = int(db.get("knowledge_version") or 0) + 1
    _save_db(db)
    _append_history({"event": "knowledge_refresh", "at": now,
                     "knowledge_version": db["knowledge_version"], "changes": changes,
                     "remote_verifiers": remote_results or None})
    return db["last_refresh_result"]


def update_entry(entry_id: str, fields: dict, note: str = "") -> bool:
    """Manual override for one entry (tier/premium/confidence/seeds) -- versioned + audited
    like every other change. Returns False if the id is unknown."""
    allowed = {"paint_seeds", "paint_indexes", "tier", "premium_multiplier", "confidence",
               "min_float", "max_float", "label", "notes", "sticker_match"}
    db = json.loads(json.dumps(load_db()))
    entry = next((e for e in db.get("entries") or [] if e.get("id") == entry_id), None)
    if entry is None:
        return False
    before = {k: entry.get(k) for k in fields if k in allowed}
    applied = {k: v for k, v in fields.items() if k in allowed}
    if not applied:
        return False
    entry.update(applied)
    db["knowledge_version"] = int(db.get("knowledge_version") or 0) + 1
    _save_db(db)
    _append_history({"event": "entry_updated", "id": entry_id, "before": before,
                     "after": applied, "note": note,
                     "knowledge_version": db["knowledge_version"], "at": time.time()})
    return True


def list_history(limit: int = 200) -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    out = []
    with open(HISTORY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out.reverse()
    return out[:limit]


def get_status() -> dict:
    db = load_db()
    by_type: dict = {}
    for e in db.get("entries") or []:
        by_type[e.get("type", "?")] = by_type.get(e.get("type", "?"), 0) + 1
    return {"knowledge_version": db.get("knowledge_version"),
            "n_entries": len(db.get("entries") or []),
            "entries_by_type": by_type,
            "sources": db.get("sources"),
            "last_verified_at": db.get("last_verified_at"),
            "last_refresh_result": db.get("last_refresh_result"),
            "refresh_due": refresh_due(),
            # Honest limitation, surfaced not hidden -- see module docstring.
            "remote_verification_available": bool(REMOTE_VERIFIERS),
            "remote_verification_note": (
                None if REMOTE_VERIFIERS else
                "Automatic re-verification currently re-validates against the bundled seed and "
                "logs versioned changes; live scraping of CSBlueGem/FloatDB is not wired up "
                "(no stable public API). Every entry links its documenting source for manual "
                "verification."),
            "corrupt_on_disk": db.get("corrupt_on_disk", False)}
