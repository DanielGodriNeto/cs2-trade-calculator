"""Registry of skins whose Steam price was VERIFIED (live Skinport/Buff163/CSFloat
cross-check) to sit far above or below the cheapest independent market price -- the
"overvalued/undervalued" feedback loop behind the dashboard's "Verify top 50" button.

Distinction from the neighbors:
  - invalid_skins.py: NO price on any source (can't be valued at all).
  - dead_skin.py:     price exists but Steam's own liquidity signals look thin (volume-based,
                      computed for free on every simulation).
  - THIS module:      price exists and may even look liquid, but an actual Stage-2
                      verification (verify_queue.py / the manual Verify buttons) showed
                      Steam's number disagrees with the wider market by >= GAP_THRESHOLD_PCT.
                      OVERVALUED Steam prices inflate a trade-up's expected revenue (fake
                      ROI); UNDERVALUED ones distort costs/model features the other way.
                      Either direction means the tool's numbers for that skin can't be
                      trusted right now.

How the agents "learn to avoid" a flagged skin (the user-facing promise):
  1. simulate.py consults active_gap() per priced unit and adds dead-skin points for a
     flagged skin -- crossing the dead-skin threshold sets low_volume_warning, which
     sim_learning.train() already excludes from every agent's training set and strict agents'
     best-known lists (_consider_best). So future trials containing the skin stop teaching
     the models anything and stop surfacing as opportunities.
  2. verify_queue.summarize_and_writeback() stamps the verified ROI/confidence back onto the
     originating best-list entry, so ranking.py's reliability dampening immediately demotes
     an entry whose headline ROI rested on the mispriced skin.

Like the neighbors, this is TEMPORARY and self-healing, never a permanent blacklist: entries
expire after RECHECK_SECONDS, and any later verification showing the gap back under threshold
clears the mark immediately (record_from_rows both marks and clears). One shared registry
(a market fact, not per-son data), same as invalid_skins.py.
"""
import json
import threading
import time
from pathlib import Path

from . import steam_prices as steam_prices_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MISPRICED_FILE = DATA_DIR / "mispriced_skins.json"

GAP_THRESHOLD_PCT = 20.0  # |Steam vs cheapest verified| must reach this to flag -- slightly
# looser than dead_skin.LARGE_PRICE_GAP_PCT (25) is strict, deliberately between
# thirdparty_prices.drought_flag's 15 and that 25: this mark is backed by a live verification,
# not a passive bulk-feed glance, so it can afford to be a bit more sensitive.
RECHECK_SECONDS = 24 * 3600  # same self-healing cadence as invalid_skins.INVALID_RECHECK_SECONDS
MISPRICED_DEAD_SKIN_POINTS = 40.0  # what a currently-active mark contributes to a unit's
# dead-skin score in simulate.py -- deliberately equal to dead_skin.DEAD_SKIN_THRESHOLD so a
# verified mispricing ALONE excludes the skin from training/leaderboards, no second signal
# needed (that's the whole point of having spent a live verification on it).

_lock = threading.Lock()
_store: dict | None = None  # {"skins": {key: {...}}} -- same lazy in-process cache shape as
# invalid_skins.py; the sim loop and dashboard share one process, the CLI reads fresh at start.


def _key(name: str, currency: int) -> str:
    return f"{currency}:{name}"


def _ensure_loaded() -> None:
    global _store
    if _store is not None:
        return
    if MISPRICED_FILE.exists():
        try:
            _store = json.loads(MISPRICED_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _store = {"skins": {}}
    else:
        _store = {"skins": {}}
    _store.setdefault("skins", {})


def _save_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MISPRICED_FILE.write_text(json.dumps(_store), encoding="utf-8")


def mark(skin_name: str, wear: str, stattrak: bool, currency: int, gap_pct: float,
         cheapest_source: str, steam_price: float | None, cheapest_price: float | None) -> None:
    """Record (or refresh) a verified Steam-vs-market gap for one skin+wear+StatTrak.
    direction is derived from the sign: positive gap = Steam above the market (overvalued)."""
    name = steam_prices_mod.market_hash_name(skin_name, wear, stattrak)
    now = time.time()
    with _lock:
        _ensure_loaded()
        entry = _store["skins"].get(_key(name, currency)) or {
            "skin": skin_name, "wear": wear, "stattrak": stattrak, "currency": currency,
            "market_hash_name": name, "first_seen": now}
        entry.update({"gap_pct": gap_pct,
                      "direction": "overvalued" if gap_pct >= 0 else "undervalued",
                      "cheapest_source": cheapest_source, "steam_price": steam_price,
                      "cheapest_price": cheapest_price, "marked_at": now})
        _store["skins"][_key(name, currency)] = entry
        _save_locked()


def clear_name(name: str, currency: int) -> bool:
    with _lock:
        _ensure_loaded()
        if _store["skins"].pop(_key(name, currency), None) is not None:
            _save_locked()
            return True
        return False


def active_gap(name: str, currency: int) -> "float | None":
    """The verified gap %, or None if this market_hash_name isn't currently marked (or the
    mark has aged past RECHECK_SECONDS -- expired marks read as clean so the skin gets a
    fresh chance). Cheap in-memory lookup, safe for simulate.py's per-unit hot loop."""
    with _lock:
        _ensure_loaded()
        entry = _store["skins"].get(_key(name, currency))
        if entry is None or (time.time() - entry["marked_at"]) >= RECHECK_SECONDS:
            return None
        return entry["gap_pct"]


def record_from_rows(rows: list, currency: int) -> dict:
    """Fold one verification's PriceRow list (price_report.verify_simulation_record /
    build_rows_for_result output) into the registry: every row whose |steam_vs_cheapest_pct|
    >= GAP_THRESHOLD_PCT gets marked, every row verified back under it gets cleared. Called
    from verify_queue.summarize_and_writeback so BOTH triggers (automatic queue, any manual
    Verify button, the Verify-top-50 sweep) feed the same loop. Returns
    {"marked": n, "cleared": n} for logging/UI."""
    marked = cleared = 0
    for r in rows:
        gap = getattr(r, "steam_vs_cheapest_pct", None)
        if gap is None:
            continue
        # cheapest_source == "steam" means no third-party source undercut Steam; a NEGATIVE
        # gap can't exist then, and a zero gap says nothing -- only flag when an independent
        # source actually disagrees.
        if abs(gap) >= GAP_THRESHOLD_PCT and getattr(r, "cheapest_source", None) not in (None, "steam"):
            mark(r.skin, r.wear, r.stattrak, currency, gap, r.cheapest_source,
                 getattr(r, "steam_price", None), getattr(r, "cheapest_price", None))
            marked += 1
        else:
            name = steam_prices_mod.market_hash_name(r.skin, r.wear, r.stattrak)
            if clear_name(name, currency):
                cleared += 1
    return {"marked": marked, "cleared": cleared}


def list_mispriced(currency: int | None = None) -> list:
    """Every currently-registered mark (most recent first), tagged active/expired -- for the
    dashboard's Market Intelligence panel and the CLI."""
    now = time.time()
    with _lock:
        _ensure_loaded()
        rows = [dict(e) for e in _store["skins"].values()
                if currency is None or e.get("currency") == currency]
    for r in rows:
        r["active"] = (now - r.get("marked_at", 0)) < RECHECK_SECONDS
    rows.sort(key=lambda e: e.get("marked_at", 0), reverse=True)
    return rows


def clear_all() -> int:
    with _lock:
        _ensure_loaded()
        n = len(_store["skins"])
        _store["skins"] = {}
        _save_locked()
        return n


def forget_all() -> None:
    """Test/maintenance helper, same as invalid_skins.forget_all."""
    global _store
    with _lock:
        _store = None
