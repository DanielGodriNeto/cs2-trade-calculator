"""Registry of trade-up COMPOSITIONS (keyed by sim_blacklist.signature -- from/to rarity,
StatTrak flag, and the multiset of input skin/wear/count units) whose Stage-2 verified ROI
(verify_queue.py's live Skinport/Buff163/CSFloat cross-check) came back at or below
VERIFIED_ROI_FLOOR_PCT: the cross-marketplace check showed this exact composition isn't actually
profitable in reality, whatever its raw simulated `expected_roi_pct` still claims (a "bugged"
skin -- one whose Steam price is stale/anomalous/thin -- is the usual cause).

Why this needs to exist as its own registry, distinct from mispriced_skins.py: that module marks
individual SKINS whose Steam price disagrees with the market by >= GAP_THRESHOLD_PCT, which is a
real but narrower signal -- a composition's real ROI can come in far below its simulated estimate
from several small per-unit gaps compounding (probability-weighted across many possible outputs)
without any single skin ever crossing that per-skin threshold on its own. This registry marks the
composition as a whole, straight from `verify_queue._summarize()`'s own probability-weighted
verified ROI, catching that compounding case too.

Why this matters operationally: sim_loop.py's per-signature dedupe (_consider_best) previously
kept whichever run of a given composition scored the higher RAW expected_roi_pct -- so a fresh,
unverified re-simulation of the exact same bugged composition would immediately overwrite (and
silently discard) a verification that had already proven it unprofitable, and the same skins
would get queued for a brand new Skinport/Buff163/CSFloat check next round. This registry, plus
sim_loop._entry_hidden consulting it, breaks that loop: once a composition is verified bad it's
hidden from every best/archive list and never re-admitted, so it stops resurfacing and stops
burning rate-limited verification requests on an answer this tool already has. sim_learning.py's
training set excludes it too, so the surrogate model stops being taught that this composition is
a good find.

Self-healing like invalid_skins.py/mispriced_skins.py, never a permanent blacklist: a mark
expires after RECHECK_SECONDS so the composition gets a fresh look if prices genuinely move
(record() also clears an existing mark the moment a later verification comes back above the
floor). One shared data/verified_low_roi.json across the whole population -- this is a market
fact about a specific composition, not per-son simulated data or a trained model, so it does not
violate feedback_sim_data_must_never_touch_real_outcomes (no per-son data crosses here, same
justification sim_blacklist.py documents for its own shared file).
"""
import json
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_FILE = DATA_DIR / "verified_low_roi.json"

VERIFIED_ROI_FLOOR_PCT = 0.0  # matches verify_queue.ROI_THRESHOLD_PCT -- a verified ROI at or
# below this means the composition isn't actually a winning trade in reality, whatever the raw
# simulated expected_roi_pct still says.
RECHECK_SECONDS = 24 * 3600  # same self-healing cadence as mispriced_skins.RECHECK_SECONDS /
# invalid_skins.INVALID_RECHECK_SECONDS

_lock = threading.Lock()
_store: "dict | None" = None  # {"signatures": {sig: {...}}} -- same lazy in-process cache shape
# as invalid_skins.py/mispriced_skins.py


def _ensure_loaded() -> None:
    global _store
    if _store is not None:
        return
    if STORE_FILE.exists():
        try:
            _store = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _store = {"signatures": {}}
    else:
        _store = {"signatures": {}}
    _store.setdefault("signatures", {})


def _save_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORE_FILE.write_text(json.dumps(_store), encoding="utf-8")


def record(signature: str, verified_roi_pct: float, *, meta: dict | None = None) -> None:
    """Called from verify_queue.summarize_and_writeback right after a verification produces a
    real (non-None) verified_roi_pct. Marks the composition bad when verified_roi_pct is at or
    below VERIFIED_ROI_FLOOR_PCT; clears any prior mark otherwise (verified clean now, e.g. after
    prices moved back in line) -- mirrors mispriced_skins.record_from_rows' mark-or-clear shape."""
    with _lock:
        _ensure_loaded()
        if verified_roi_pct <= VERIFIED_ROI_FLOOR_PCT:
            entry = {"verified_roi_pct": verified_roi_pct, "marked_at": time.time()}
            if meta:
                entry.update(meta)
            _store["signatures"][signature] = entry
            _save_locked()
        elif _store["signatures"].pop(signature, None) is not None:
            _save_locked()


def is_marked(signature: str) -> bool:
    """Cheap in-memory lookup, safe for sim_loop's/sim_learning's hot paths. A mark past
    RECHECK_SECONDS reads as clean (self-healing) so the composition gets a fresh chance rather
    than being excluded forever off one aging reading."""
    with _lock:
        _ensure_loaded()
        entry = _store["signatures"].get(signature)
        if entry is None:
            return False
        return (time.time() - entry["marked_at"]) < RECHECK_SECONDS


def list_marked() -> list:
    """Every currently-registered mark (most recent first), tagged active/expired -- for
    dashboard/CLI visibility, same shape as mispriced_skins.list_mispriced."""
    now = time.time()
    with _lock:
        _ensure_loaded()
        rows = [{"signature": sig, **e} for sig, e in _store["signatures"].items()]
    for r in rows:
        r["active"] = (now - r.get("marked_at", 0)) < RECHECK_SECONDS
    rows.sort(key=lambda e: e.get("marked_at", 0), reverse=True)
    return rows


def forget_all() -> None:
    """Test/maintenance helper, same as invalid_skins.forget_all / mispriced_skins.forget_all."""
    global _store
    with _lock:
        _store = None
