"""Automatic Stage-2 verification queue: the moment a background agent's find crosses a
"worth double-checking against live third-party prices" bar, queue it for the SAME
price_report.verify_simulation_record() call the dashboard's manual "Verify prices" button
already uses -- just triggered automatically instead of by a click.

The check covers EVERY distinct input skin+wear and every priced output of the queued
trade-up against Skinport, Buff163 and CSFloat (price_report.verify_simulation_record) --
not just the headline skin.

Deliberately rate-limited, NOT "verify everything": a past attempt to make third-party
verification MANDATORY for every simulated candidate broke the feature in practice
(round 50, 0 simulated / 2000 invalid, per project history) because Skinport doesn't list
every cheap trade-up-fodder skin -- see simulate.py's own module docstring. This queue only
ever holds candidates that ALREADY cleared a real bar (see is_eligible: positive expected
ROI, not dead-skin-flagged, a fresh cost basis) AND were admitted into an agent's best list
-- never the raw simulation firehose -- and a single consumer thread drains it ONE AT A
TIME through the exact same throttled/cooldown-respecting path (thirdparty_prices.py's
SKINPORT_FAILURE_COOLDOWN_SECONDS, Steam's own adaptive rate limiter) every other live
request in this app already goes through. Never a burst of parallel calls, and always
pausable from the dashboard (same Start/Pause pattern as every other background loop here)
if it ever proves disruptive.
"""
import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import mispriced_skins as mispriced_skins_mod
from . import price_report as price_report_mod
from . import sim_blacklist as sim_blacklist_mod
from . import verified_low_roi as verified_low_roi_mod
from .tradeup import STEAM_SELL_FEE

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_FILE = DATA_DIR / "sons" / "verification_queue.json"  # last MAX_LOG_KEPT auto-verify
# results, for restart-persistence/dashboard visibility -- NOT the live in-memory queue
# itself (that's a plain queue.Queue, rebuilt empty on every process restart; safe, since a
# candidate that would still qualify gets re-queued naturally next time its agent re-finds it).

ROI_THRESHOLD_PCT = 0.0         # any positive expected ROI qualifies (is_eligible uses a strict
# >, so exactly-break-even finds still don't burn a rate-limited verification) -- lowered from
# the original 20.0 so every winning find gets its inputs+outputs cross-checked against
# Skinport/Buff163/CSFloat automatically, not just the spectacular ones
CONFIDENCE_THRESHOLD = 60.0     # (100 - worst counted unit's dead_skin score) must clear this
MAX_LOG_KEPT = 200
POLL_INTERVAL_SECONDS = 2.0

_write_lock = threading.Lock()
_queue: "queue.Queue" = queue.Queue()
_queued_ids: set = set()  # de-dupe -- an entry can be re-considered many rounds in a row


@dataclass
class QueueItem:
    son_id: int
    entry: dict  # one of sim_loop.py's checkpoint `best` list dicts


def is_eligible(entry: dict) -> bool:
    """Mirrors the spec's Stage-2 gate: ROI > threshold AND a fresh cost basis (never
    auto-verify something priced off a stale cache entry) AND no counted input/output unit
    carrying a dead-skin score above threshold. Checked independent of which agent found it
    -- a checkpoint entry's mere presence in an agent's best list already passed THAT
    agent's own admission gate (sim_loop._consider_best), but agents differ in how strict
    that gate is (see agents.py's liquidity_importance/risk_tolerance), so the auto-verify
    bar is re-checked here the same way for everyone."""
    if entry.get("expected_roi_pct", float("-inf")) <= ROI_THRESHOLD_PCT:
        return False
    if entry.get("cost_basis_source") == "stale":
        return False
    # Already proven unprofitable by a PRIOR verification of this exact composition -- see
    # verified_low_roi.py's module docstring for why re-queuing it here would just burn another
    # round of rate-limited Skinport/Buff163/CSFloat requests re-confirming an answer this tool
    # already has. enqueue_forced/enqueue_top deliberately bypass is_eligible (an explicit,
    # user-triggered audit sweep), so this only guards the automatic path.
    if verified_low_roi_mod.is_marked(sim_blacklist_mod.signature_of(entry)):
        return False
    all_units = list(entry.get("input_mix") or []) + list(entry.get("output_distribution") or [])
    worst = max((u.get("dead_skin_score", 0.0) or 0.0 for u in all_units), default=0.0)
    return (100.0 - worst) >= CONFIDENCE_THRESHOLD


def maybe_enqueue(son_id: int, entry: dict) -> bool:
    """Called from sim_loop.py right after an entry is admitted into an agent's best list.
    Returns True if it was queued (for tests/observability), False if not eligible or
    already queued/verified this process lifetime."""
    entry_id = entry.get("id")
    if not entry_id or entry_id in _queued_ids:
        return False
    if not is_eligible(entry):
        return False
    _queued_ids.add(entry_id)
    _queue.put(QueueItem(son_id=son_id, entry=entry))
    return True


def enqueue_forced(son_id: int, entry: dict) -> bool:
    """Queue an entry REGARDLESS of is_eligible() -- for the user-triggered "Verify top N"
    sweep, where the whole point is auditing entries as they stand (including ones whose
    headline ROI might be resting on a mispriced skin the eligibility gate would have
    excused). Still de-duped and still drained one-at-a-time by the same consumer -- forcing
    changes WHAT gets verified, never how fast."""
    entry_id = entry.get("id")
    if not entry_id or entry_id in _queued_ids:
        return False
    _queued_ids.add(entry_id)
    _queue.put(QueueItem(son_id=son_id, entry=entry))
    return True


def enqueue_top(n: int = 50) -> int:
    """The dashboard's "Verify top N trade-ups" button: force-queue the current combined
    leaderboard's top n entries (ranking.rank()-ordered, deduped, blacklist-hidden -- exactly
    what the Trade-up opportunities table shows) for Stage-2 verification. Each result flows
    through the same summarize_and_writeback() as every other verification: verified ROI /
    price confidence stamped back onto the checkpoint entry, and any skin whose Steam price
    disagrees with the verified market by >= mispriced_skins.GAP_THRESHOLD_PCT recorded in
    the mispriced registry, which future simulations penalize (see mispriced_skins.py's
    module docstring for the full feedback loop). Returns how many were newly queued (0 when
    everything's already queued or the leaderboard is empty)."""
    from . import sim_loop as sim_loop_mod  # lazy -- see _writeback's cycle note
    queued = 0
    for entry in sim_loop_mod.list_best_simulations()[:n]:
        if enqueue_forced(int(entry.get("son", 0)), entry):
            queued += 1
    return queued


_CONFIDENCE_RANK = {"ok": 0, "low volume": 1, "no 3rd-party match": 1,
                     "low volume + no 3rd-party match": 2}


def _worst_confidence(rows: list) -> str:
    if not rows:
        return "no data"
    return max((r.price_confidence for r in rows), key=lambda c: _CONFIDENCE_RANK.get(c, 0))


def _summarize(entry: dict, rows: list) -> tuple[float | None, str]:
    """Recomputes an approximate VERIFIED ROI from the verify_simulation_record() rows --
    same probability-weighted, fee-adjusted math simulate.py itself uses, but using each
    row's `consensus_price` instead of Steam-only pricing: normally the cheapest of
    Steam/Skinport/Buff163/CSFloat, but the MEDIAN of however many sources answered when
    they disagree by >= price_report.HIGH_SPREAD_THRESHOLD_PCT -- a badly-split market means
    the cheapest single site is as likely the outlier as the priciest (see price_report.py's
    module docstring). Returns (None, worst_confidence) if the verified data doesn't
    actually cover enough of the contract to trust a number (rather than reporting a
    partial/misleading ROI)."""
    input_rows = [r for r in rows if r.role == "input"]
    output_rows = [r for r in rows if r.role == "output"]
    counts = {(u["skin"], u["wear"]): u["count"] for u in entry.get("input_mix") or []}
    cost = 0.0
    for r in input_rows:
        if r.consensus_price is None:
            return None, _worst_confidence(rows)
        cost += r.consensus_price * counts.get((r.skin, r.wear), 1)
    revenue_weighted = [(r.consensus_price, r.probability_pct or 0.0)
                         for r in output_rows if r.consensus_price is not None]
    total_prob = sum(p for _, p in revenue_weighted)
    if cost <= 0 or not revenue_weighted or total_prob <= 0:
        return None, _worst_confidence(rows)
    expected_revenue = sum(v * p for v, p in revenue_weighted) / total_prob
    after_fee = expected_revenue * (1 - STEAM_SELL_FEE)
    verified_roi_pct = (after_fee - cost) / cost * 100
    return verified_roi_pct, _worst_confidence(rows)


def summarize_and_writeback(son_id: int, entry: dict, rows: list,
                             currency: int = 1) -> tuple[float | None, str]:
    """Shared by BOTH triggers (see module docstring): the automatic consumer_loop below, and
    the dashboard's manual "Verify prices" button (web/server.py's /api/sim-population/
    verify-prices, once it's given a son id to write back to) -- "populated by either the
    manual button or the automatic queue" is the same code path either way, not two
    implementations that could drift apart.

    Also the single place every verification's per-skin gaps feed the mispriced-skin
    registry (mispriced_skins.record_from_rows) -- marking skins the verified market
    disagrees with Steam about, and clearing ones verified back in line -- and the place the
    whole COMPOSITION's verified ROI feeds verified_low_roi.py, so a composition proven
    unprofitable stops resurfacing and stops getting re-verified every time a son re-discovers
    it (see that module's docstring)."""
    verified_roi_pct, price_confidence = _summarize(entry, rows)
    _writeback(son_id, entry["id"], verified_roi_pct, price_confidence)
    mispriced_skins_mod.record_from_rows(rows, currency)
    if verified_roi_pct is not None:
        verified_low_roi_mod.record(sim_blacklist_mod.signature_of(entry), verified_roi_pct)
    return verified_roi_pct, price_confidence


def _writeback(son_id: int, entry_id: str, verified_roi_pct: float | None, price_confidence: str) -> bool:
    """Writes the verification result back onto the LIVE checkpoint's best-list entry, if
    it's still there -- an entry can fall out of the top BEST_KEPT between when it was
    queued and when it's actually verified (sim_loop.py's best-list keeps only the top N by
    ROI); that's fine, just skip the write rather than resurrecting a no-longer-relevant
    entry. Lazy-imports sim_loop.py to avoid a circular import (sim_loop imports this
    module to enqueue; this module needs sim_loop's checkpoint helpers to write back) --
    same lazy-import-to-break-a-cycle pattern steam_prices.py already uses for price_history.py."""
    from . import sim_loop as sim_loop_mod
    cp_path = sim_loop_mod.son_checkpoint_file(son_id)
    cp = sim_loop_mod.load_checkpoint(cp_path)
    for e in cp["best"]:
        if e["id"] == entry_id:
            e["verified_roi_pct"] = verified_roi_pct
            e["price_confidence"] = price_confidence
            e["verified_at"] = time.time()
            sim_loop_mod.save_checkpoint(cp_path, cp)
            return True
    return False


def _append_log(result: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        existing = []
        if LOG_FILE.exists():
            try:
                existing = json.loads(LOG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = []
        existing.insert(0, result)
        del existing[MAX_LOG_KEPT:]
        LOG_FILE.write_text(json.dumps(existing), encoding="utf-8")


class VerifyQueueState:
    """Thread-control wrapper mirroring retrain_loop.RetrainLoopState's pause/stop shape --
    same Start/Pause/Stop dashboard pattern as every other background loop in this app."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | watching | verifying | paused | stopped
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.n_verified_total = 0
        self.n_verified_today = 0
        self._today = time.strftime("%Y-%m-%d")
        self.recent: list = []  # most recent results first, capped at MAX_LOG_KEPT

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "queue_depth": _queue.qsize(),
                "n_verified_total": self.n_verified_total,
                "n_verified_today": self.n_verified_today,
                "recent": list(self.recent[:20]),
                "roi_threshold_pct": ROI_THRESHOLD_PCT,
                "confidence_threshold": CONFIDENCE_THRESHOLD,
            }


def consumer_loop(state: VerifyQueueState, currency: int = 1,
                   poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    """Drains _queue ONE item at a time -- the actual verify_simulation_record() call is
    itself already throttled/rate-limited internally (Steam + Skinport/CSFloat), so this
    loop's own job is just "never run two of those concurrently" and "never let one bad
    lookup (a network exception) kill the whole consumer". Runs until stop_event is set
    (CLI/process exit); pause_event (dashboard Start/Pause) idles it indefinitely without
    terminating the thread, same as retrain_loop.py."""
    while True:
        if state.stop_event.is_set():
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
        if state.pause_event.is_set():
            with state.lock:
                state.status = "paused"
            if state.stop_event.wait(timeout=poll_interval):
                with state.lock:
                    state.status = "stopped"
                    state.stop_event.clear()
                return
            continue
        try:
            item = _queue.get(timeout=poll_interval)
        except queue.Empty:
            with state.lock:
                state.status = "watching"
            continue
        with state.lock:
            state.status = "verifying"
        try:
            rows = price_report_mod.verify_simulation_record(
                primary_collection=item.entry["primary_collection"], from_rarity=item.entry["from_rarity"],
                to_rarity=item.entry["to_rarity"], stattrak=item.entry["stattrak"],
                expected_roi_pct=item.entry["expected_roi_pct"], input_mix=item.entry["input_mix"],
                output_distribution=item.entry["output_distribution"], currency=currency,
                abort_check=state.stop_event.is_set)
            verified_roi_pct, price_confidence = summarize_and_writeback(item.son_id, item.entry, rows,
                                                                          currency=currency)
            result = {"son_id": item.son_id, "id": item.entry["id"],
                      "primary_collection": item.entry.get("primary_collection", ""),
                      "verified_roi_pct": verified_roi_pct, "price_confidence": price_confidence,
                      "verified_at": time.time(), "error": None}
        except Exception as e:  # a live-network call -- never let one bad lookup take down
            # the whole consumer thread; log the failure and keep draining the queue.
            result = {"son_id": item.son_id, "id": item.entry.get("id", ""),
                      "primary_collection": item.entry.get("primary_collection", ""),
                      "verified_roi_pct": None, "price_confidence": None,
                      "verified_at": time.time(), "error": str(e)}
        finally:
            _queued_ids.discard(item.entry.get("id"))
        with state.lock:
            state.n_verified_total += 1
            today = time.strftime("%Y-%m-%d")
            if today != state._today:
                state._today = today
                state.n_verified_today = 0
            state.n_verified_today += 1
            state.recent.insert(0, result)
            del state.recent[MAX_LOG_KEPT:]
        _append_log(result)


def run_foreground(currency: int = 1) -> None:
    """CLI entry point (`python -m src.main verify-queue`) -- runs in the current thread,
    Ctrl+C to stop."""
    state = VerifyQueueState()
    print("Watching for auto-verify-eligible finds (ROI > "
          f"{ROI_THRESHOLD_PCT}%, confidence >= {CONFIDENCE_THRESHOLD}) from any running "
          "simulate-population son. Ctrl+C to stop.\n")
    try:
        consumer_loop(state, currency=currency)
    except KeyboardInterrupt:
        state.stop_event.set()
        print("\nStopped.")
        return
