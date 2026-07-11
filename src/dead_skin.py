"""Dead-skin protection: a numeric, TEMPORARY score for "is this skin's price actually
tradeable, or fake ROI from a listing nobody's buying" -- replacing the old binary
low_volume_warning (steam volume < 5, see simulate.py) with a graduated score built from
every liquidity/consistency signal already available, and NEVER a permanent blacklist.

Score buckets (points added when the condition is observed):
- no_recent_sales (40): Steam reports an EXPLICIT zero recent-sales volume for this exact
  skin+wear+currency -- the strongest signal, since it's a real reported number, not a
  missing field. A MISSING volume (None) triggers nothing here -- same lenient treatment
  the old _is_low_volume(volume) already gave a missing volume (see price_report.py),
  since Steam's API just omits the field sometimes for reasons unrelated to liquidity.
- very_low_listings (30): a real, reported volume below price_report.LOW_VOLUME_THRESHOLD
  but above zero -- thin, but not necessarily fake.
- large_price_gap (20): Steam's price sits well above the cheapest verified third-party
  price (Skinport/CSFloat) -- only computable when that data has already been fetched
  (simulate.py only fetches it once a price already looks thin, to avoid the mandatory-
  verification mistake documented in project history), so this bucket is a REFINEMENT
  step, not part of the initial gate.
- unstable_pricing (10): this skin+wear's recorded price_history.py points swing more
  than UNSTABLE_CV_THRESHOLD (coefficient of variation) -- a price that's all over the
  place is less trustworthy even at healthy volume.

DEAD_SKIN_THRESHOLD=40 is set so a lone REPORTED-zero-volume signal (40) already crosses
it by itself -- preserving the old code's strictness for that worst case -- while a lone
merely-thin-but-nonzero volume (30) needs ONE more corroborating signal to cross it. This
is a deliberate loosening ONLY for the "thin but otherwise consistent" middle case (the
graduated nuance the spec asks for), never for the "reported zero" case.

Two use sites:
1. INLINE, in simulate.py, computed fresh from whatever that call already has in hand
   (no extra network calls) -- becomes SimulationRecord.low_volume_warning (see
   score_from_signals/is_dead below).
2. A PERSISTED, DECAYING store (data/dead_skin_scores.json) reviewed weekly (piggybacked
   on retrain_loop.py's existing cheap polling cadence, not a new background thread --
   see run_weekly_review) purely from what's ALREADY in the price cache (again, zero new
   live calls) -- this is what lets a skin's penalty fall back off if its market activity
   recovers, per the spec's explicit "do not permanently blacklist" requirement.
"""
import json
import threading
import time
from pathlib import Path

from . import price_history as price_history_mod
from . import price_report as price_report_mod
from . import steam_prices as steam_prices_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SCORES_FILE = DATA_DIR / "dead_skin_scores.json"

NO_RECENT_SALES_POINTS = 40
VERY_LOW_LISTINGS_POINTS = 30
LARGE_PRICE_GAP_POINTS = 20
UNSTABLE_PRICING_POINTS = 10
DEAD_SKIN_THRESHOLD = 40

LARGE_PRICE_GAP_PCT = 25.0     # Steam this much (or more) above the cheapest 3rd-party price
UNSTABLE_CV_THRESHOLD = 0.30   # stdev/mean of recorded price_history points

REVIEW_INTERVAL_SECONDS = 7 * 24 * 3600  # weekly, per spec
EMA_ALPHA = 0.5  # how much a fresh review moves the persisted score toward the newly
# computed one -- keeps the stored score a TEMPORARY, decaying signal: a skin whose
# market activity has genuinely recovered sees its stored score fall toward 0 over a
# few consecutive weekly reviews rather than being stuck wherever it first scored.

_write_lock = threading.Lock()


def _stability(history: list) -> bool:
    """True if recorded price_history points for a skin+wear look unstable (coefficient
    of variation above UNSTABLE_CV_THRESHOLD). Needs at least 3 points -- below that,
    "instability" isn't a meaningful judgment, so this returns False (no penalty) rather
    than guessing off too little data, same "refuse rather than fabricate" convention
    price_history.predict_next_price() already uses."""
    prices = [h["lowest_price"] for h in history if h.get("lowest_price") is not None]
    if len(prices) < 3:
        return False
    mean = sum(prices) / len(prices)
    if mean <= 0:
        return False
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    return (variance ** 0.5) / mean > UNSTABLE_CV_THRESHOLD


def score_from_signals(volume: int | None, history: list | None = None) -> tuple[float, list]:
    """The zero-network-call half of the score -- everything simulate.py already has in
    hand (or can get from a free local file read) BEFORE deciding whether third-party
    corroboration is even worth fetching. Returns (score, reasons)."""
    score = 0.0
    reasons = []
    if volume is not None and volume == 0:
        score += NO_RECENT_SALES_POINTS
        reasons.append("no_recent_sales")
    elif volume is not None and volume < price_report_mod.LOW_VOLUME_THRESHOLD:
        score += VERY_LOW_LISTINGS_POINTS
        reasons.append("very_low_listings")
    if history and _stability(history):
        score += UNSTABLE_PRICING_POINTS
        reasons.append("unstable_pricing")
    return score, reasons


def refine_with_thirdparty(score: float, reasons: list, steam_price: float | None,
                            thirdparty_prices: dict) -> tuple[float, list]:
    """Adds the large_price_gap bucket once third-party prices are ALREADY available
    (simulate.py only fetches them when score_from_signals already looks thin -- see
    module docstring) -- never triggers its own network call."""
    candidates = [p for p in (thirdparty_prices or {}).values() if p]
    if steam_price and candidates:
        cheapest = min(candidates)
        if cheapest > 0:
            gap_pct = (steam_price - cheapest) / cheapest * 100
            if gap_pct >= LARGE_PRICE_GAP_PCT and "large_price_gap" not in reasons:
                score += LARGE_PRICE_GAP_POINTS
                reasons.append("large_price_gap")
    return min(score, 100.0), reasons


def is_dead(score: float) -> bool:
    return score >= DEAD_SKIN_THRESHOLD


def non_volume_score(score: float, reasons: list | None) -> float:
    """Strips the volume-based points (no_recent_sales/very_low_listings) back out of an
    already-computed dead_skin score, leaving only what large_price_gap/unstable_pricing
    contributed. Used wherever a caller already penalizes volume-based dead-skin risk
    through its own dedicated channel (ranking.py's liquidity factor checks these same two
    reasons directly) and needs a SEPARATE signal for the rest, so the same underlying
    volume problem isn't counted twice under two different names -- see ranking.py's
    _confidence_score, which used to take the raw (unfiltered) worst dead_skin_score here,
    silently double-penalizing low-volume units through both its liquidity AND confidence
    factors at once."""
    if not reasons:
        return score
    adjusted = score
    if "no_recent_sales" in reasons:
        adjusted -= NO_RECENT_SALES_POINTS
    if "very_low_listings" in reasons:
        adjusted -= VERY_LOW_LISTINGS_POINTS
    return max(0.0, adjusted)


# --- Persisted, decaying store (weekly-reviewed) --------------------------------------

def _load_store() -> dict:
    if not SCORES_FILE.exists():
        return {"_last_review_at": None, "skins": {}}
    try:
        store = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"_last_review_at": None, "skins": {}}
    store.setdefault("_last_review_at", None)
    store.setdefault("skins", {})
    return store


def _save_store(store: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        SCORES_FILE.write_text(json.dumps(store), encoding="utf-8")


def _key(market_hash_name: str, currency: int) -> str:
    return f"{currency}:{market_hash_name}"


def record_observation(market_hash_name: str, currency: int, score: float, reasons: list) -> None:
    """Opportunistically called from simulate.py's inline scoring (cheap -- one dict
    update) so the persisted store reflects the most recently OBSERVED signals for a skin
    in between weekly reviews, without simulate.py needing to know anything about review
    cadence. Blends via the same EMA as run_weekly_review, so a skin doesn't need to wait
    a full week to start recovering if it's suddenly seen consistently healthy signals."""
    store = _load_store()
    key = _key(market_hash_name, currency)
    entry = store["skins"].get(key, {"score": 0.0, "reasons": [], "last_seen": None})
    entry["score"] = EMA_ALPHA * score + (1 - EMA_ALPHA) * entry["score"]
    entry["reasons"] = reasons
    entry["last_seen"] = time.time()
    store["skins"][key] = entry
    _save_store(store)


def get_score(market_hash_name: str, currency: int) -> dict | None:
    store = _load_store()
    return store["skins"].get(_key(market_hash_name, currency))


def clear_all() -> int:
    """Erase every persisted dead-skin score. This is a decaying market signal recomputed
    from the price cache/history (see module docstring), not simulated/outcome data -- any
    skin still genuinely thin will simply re-accumulate a score on the next weekly review or
    inline observation. Returns how many entries were removed. Used by the dashboard's
    global 'Reset all data' button."""
    with _write_lock:
        store = _load_store()
        n = len(store["skins"])
        store = {"_last_review_at": None, "skins": {}}
        SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCORES_FILE.write_text(json.dumps(store), encoding="utf-8")
        return n


def list_dead_skins(currency: int | None = None) -> list:
    """Every currently-tracked skin whose PERSISTED (decayed) score is above threshold --
    the "dead-skin review list" the spec asks for, surfaced in the dashboard's Market
    Intelligence section (see web/static)."""
    store = _load_store()
    out = []
    for key, entry in store["skins"].items():
        cur_str, _, name = key.partition(":")
        if currency is not None and cur_str != str(currency):
            continue
        if is_dead(entry["score"]):
            out.append({"market_hash_name": name, "currency": int(cur_str),
                        "score": entry["score"], "reasons": entry["reasons"],
                        "last_seen": entry["last_seen"]})
    return sorted(out, key=lambda e: e["score"], reverse=True)


def list_all_tracked(currency: int | None = None) -> list:
    """Every skin this store has EVER scored, regardless of current score -- unlike
    list_dead_skins() (which only returns entries currently above DEAD_SKIN_THRESHOLD), this is
    the full population for a caller that wants to re-evaluate everything itself (see
    market_reliability.py's _tracked_keys, which unions this with mispriced_skins.list_mispriced
    to build the Market Reliability Engine's tracked-skin universe)."""
    store = _load_store()
    out = []
    for key, entry in store["skins"].items():
        cur_str, _, name = key.partition(":")
        if currency is not None and cur_str != str(currency):
            continue
        out.append({"market_hash_name": name, "currency": int(cur_str),
                    "score": entry["score"], "reasons": entry["reasons"],
                    "last_seen": entry["last_seen"]})
    return sorted(out, key=lambda e: e["score"], reverse=True)


def review_due() -> bool:
    store = _load_store()
    last = store["_last_review_at"]
    return last is None or (time.time() - last) >= REVIEW_INTERVAL_SECONDS


def run_weekly_review() -> dict:
    """Re-derives every tracked (plus newly-cached) skin's score from whatever's
    CURRENTLY in the Steam price cache (steam_prices.iter_cached_entries) -- zero new live
    network calls, a free re-evaluation of already-fetched data. Called from
    retrain_loop.py's existing polling loop once review_due() is True (weekly, per spec),
    not a new background thread. A skin whose current cached volume looks healthy again
    has its score EMA-decayed toward 0 (see record_observation) rather than deleted
    outright or left stuck -- "temporary score... if market activity returns, lower the
    penalty" is exactly what this loop does, one review at a time."""
    store = _load_store()
    n_reviewed = 0
    for name, currency, data, _fetched_at in steam_prices_mod.iter_cached_entries():
        history = price_history_mod.get_history(name, currency)
        score, reasons = score_from_signals(data.get("volume"), history)
        key = _key(name, currency)
        entry = store["skins"].get(key, {"score": 0.0, "reasons": [], "last_seen": None})
        entry["score"] = EMA_ALPHA * score + (1 - EMA_ALPHA) * entry["score"]
        entry["reasons"] = reasons
        entry["last_seen"] = time.time()
        store["skins"][key] = entry
        n_reviewed += 1
    store["_last_review_at"] = time.time()
    _save_store(store)
    return {"reviewed": n_reviewed, "dead_count": len(list_dead_skins())}
