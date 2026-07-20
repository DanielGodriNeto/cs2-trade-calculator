"""Advanced Market Reliability Engine: unifies dead_skin.py's liquidity score,
mispriced_skins.py's verified Steam-vs-market gap registry, and price_report.py's
cross-source consensus/spread math into ONE auditable Healthy/Suspicious/Excluded state per
skin+wear+currency -- rather than three separate signals each caller has to combine by hand
(which is exactly what ranking.py, sim_learning.py and verify_queue.py were each already
doing slightly differently before this module existed).

New capability this module actually adds (the neighbors don't do this): a ROLLING variance
and Z-score computed from price_history.py's recorded snapshots (price_history.py itself only
ever did a one-shot linear-trend prediction, never a rolling-window outlier check) -- this is
what catches "abnormal cross-market deviations": a single snapshot that stands out sharply
from a skin's own recent price neighborhood, the kind of blip a temporary Steam inflation or a
manipulated listing produces. This is a NAIVE population z-score that includes the latest
point in its own baseline window (not held out) -- good enough to flag an outlier standing out
from its own recent history, not a statistically rigorous anomaly test (same "naive baseline,
not a real forecasting model" caveat price_history.predict_next_price already documents).

Three-state model, never a permanent blacklist (same guarantee dead_skin.py and
mispriced_skins.py already make individually):
  - healthy:    reliability_score >= HEALTHY_THRESHOLD, no hard-override signal.
  - suspicious: reliability_score between EXCLUDE_THRESHOLD and HEALTHY_THRESHOLD.
  - excluded:   reliability_score < EXCLUDE_THRESHOLD, OR a rolling z-score whose magnitude
                alone crosses Z_SCORE_EXCLUDE (an extreme single-snapshot outlier forces
                Excluded regardless of how healthy everything else about the skin looks).

Excluded skins are never final: every call to compute_state() re-derives the state fresh from
whatever's CURRENT (dead_skin's own EMA-decaying score, mispriced_skins' self-expiring
RECHECK_SECONDS mark, and a fresh rolling-window read) -- so a skin recovers the moment its
underlying signals do. run_periodic_recheck(), wired into retrail_loop.py's existing 5-minute
poll, re-evaluates every currently-tracked skin far more often than the spec's own "re-enter
verification after 24 hours" minimum and logs every state TRANSITION (not every check, to
avoid log spam) to LOG_FILE for a full audit trail.

Deliberately does NOT introduce a new background thread -- same "reuse an existing cheap poll,
real work only when something's actually due" shape as dead_skin.run_weekly_review(), attached
to retrain_loop.py's loop rather than spinning up a fourth one.

Two halves, split for cost reasons:
  - compute_state()/is_excluded()/any_unit_excluded(): pure, no disk writes -- safe to call in
    a hot loop (ranking.py's rank(), sim_learning.py's train(), sim_loop.py's best-list
    admission all call these on every candidate).
  - get_state()/list_tracked()/run_periodic_recheck(): persist the latest known state and log
    transitions -- only called from on-demand paths (the dashboard listing, the CLI, the
    5-minute recheck poll), never from a per-candidate hot loop.
"""
import json
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import dead_skin as dead_skin_mod
from . import mispriced_skins as mispriced_skins_mod
from . import price_history as price_history_mod
from . import steam_prices as steam_prices_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_FILE = DATA_DIR / "market_reliability_log.jsonl"
STATE_FILE = DATA_DIR / "market_reliability_state.json"
OVERRIDES_FILE = DATA_DIR / "market_reliability_overrides.json"

HEALTHY_THRESHOLD = 70.0
EXCLUDE_THRESHOLD = 40.0  # matches dead_skin.DEAD_SKIN_THRESHOLD's convention -- one strong
# signal alone (e.g. a full mispriced-gap penalty) can already cross it, same deliberate
# strictness dead_skin.py itself documents for its own worst-case bucket.

ROLLING_WINDOW_POINTS = 10
MIN_ROLLING_POINTS = 4       # below this, rolling_stats refuses rather than fabricating a
                              # z-score from too little data (price_history.py's own convention)
Z_SCORE_SUSPICIOUS = 2.0
Z_SCORE_EXCLUDE = 3.0        # hard override -- see module docstring
STALE_SECONDS = 7 * 24 * 3600  # no fresh snapshot in a week -> "stale_listing" reason

RECHECK_SECONDS = 24 * 3600  # documented per spec; run_periodic_recheck's actual cadence
                              # (gated by recheck_due(), see below) is far tighter than this
RECHECK_POLL_INTERVAL_SECONDS = 30 * 60  # run_periodic_recheck re-derives EVERY tracked skin's
# state (rewriting STATE_FILE + appending to LOG_FILE for each change) -- gating it to once per
# 30 minutes instead of every retrain_loop.py poll tick (5 min) keeps that write volume bounded
# while still blowing past the spec's 24h minimum re-verification cadence by a wide margin.

VALID_STATES = ("healthy", "suspicious", "excluded")
MAX_LOG_KEPT = 2000

_write_lock = threading.Lock()
_override_lock = threading.Lock()
_override_store: dict | None = None  # lazy in-process cache, same shape as mispriced_skins._store

_history_index_lock = threading.Lock()
_history_index_cache: dict = {"mtime": None, "index": {}, "checked_at": 0.0}
_HISTORY_STAT_INTERVAL_SECONDS = 5.0  # how often the cache re-stats the file for freshness --
# NOT how often it re-reads it (that still only happens when the mtime actually changed).
# Without this throttle every single is_excluded()/any_unit_excluded() call paid a real
# filesystem exists()+stat() -- microseconds each on paper, but ranking a large archive makes
# ~10 of those calls per candidate, and profiling a 39k-candidate export showed the stats alone
# costing ~40 SECONDS on Windows (445k nt.stat calls). A <=5s-stale index is well inside every
# caller's tolerance: the dashboard itself only polls every 3s.


def _history_index() -> dict:
    """In-process index of price_history.jsonl, keyed by (market_hash_name, currency) -> its
    snapshots (oldest first). price_history.get_history() does a full-file scan per call --
    fine for the single-skin lookups it was built for, but list_tracked()/run_periodic_recheck()
    need EVERY tracked skin's history, and any_unit_excluded() gets called once per unit per
    candidate in ranking.py/sim_learning.py/sim_loop.py's hot loops -- calling get_history()
    there would mean re-reading a multi-MB, tens-of-thousands-of-lines file over and over for
    every single lookup. Rebuilt only when the file's mtime changes (checked at most once per
    _HISTORY_STAT_INTERVAL_SECONDS, see above), same cache-keyed-by-mtime convention as
    sim_learning.py's _collection_roi_caches."""
    global _history_index_cache
    now = time.time()
    if (now - _history_index_cache["checked_at"] < _HISTORY_STAT_INTERVAL_SECONDS
            and _history_index_cache["mtime"] is not None):
        return _history_index_cache["index"]
    path = price_history_mod.HISTORY_FILE
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    with _history_index_lock:
        if _history_index_cache["mtime"] == mtime:
            _history_index_cache["checked_at"] = now
            return _history_index_cache["index"]
        index: dict = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (entry.get("market_hash_name"), entry.get("currency"))
                index.setdefault(key, []).append(entry)
        for snapshots in index.values():
            snapshots.sort(key=lambda e: e["ts"])
        _history_index_cache = {"mtime": mtime, "index": index, "checked_at": now}
        return index


def _history_for(market_hash_name: str, currency: int) -> list:
    return _history_index().get((market_hash_name, currency), [])


@dataclass
class ReliabilityState:
    market_hash_name: str
    currency: int
    state: str  # "healthy" | "suspicious" | "excluded"
    reliability_score: float
    reasons: list
    rolling_z: float | None
    rolling_stdev: float | None
    dead_skin_score: float
    mispriced_gap_pct: float | None
    stale: bool
    computed_at: float
    # Cross-marketplace metrics (see market_metrics()) -- populated only on the on-demand
    # paths (get_state/list_tracked), None on hot-path compute_state calls, which must stay
    # free of bulk-feed reads. Added at the end with defaults so nothing that constructs or
    # unpacks the original fields changes shape.
    consensus_price: float | None = None
    price_variance: float | None = None
    n_sources: int | None = None
    source_agreement_pct: float | None = None
    liquidity_score: float | None = None
    market_confidence: str | None = None


def _key(market_hash_name: str, currency: int) -> str:
    return f"{currency}:{market_hash_name}"


# --- Rolling variance / Z-score (the genuinely new signal) ----------------------------

def rolling_stats(history: list[dict], field: str = "lowest_price") -> dict | None:
    """mean/stdev/variance over the trailing ROLLING_WINDOW_POINTS snapshots, plus the most
    recent point's z-score against that same window (see module docstring for the "naive,
    self-inclusive baseline" caveat). Returns None below MIN_ROLLING_POINTS usable points --
    refuse rather than fabricate, same convention price_history.predict_next_price uses."""
    points = [(h["ts"], h[field]) for h in history if h.get(field) is not None]
    if len(points) < MIN_ROLLING_POINTS:
        return None
    window = points[-ROLLING_WINDOW_POINTS:]
    values = [v for _, v in window]
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    latest_ts, latest_value = points[-1]
    z = (latest_value - mean) / stdev if stdev > 0 else None
    return {
        "mean": mean, "stdev": stdev, "variance": stdev ** 2, "z": z,
        "latest": latest_value, "latest_ts": latest_ts, "n_points": len(window),
        "stale": (time.time() - latest_ts) >= STALE_SECONDS,
    }


# --- State computation (pure -- no disk writes) ----------------------------------------

def compute_state(market_hash_name: str, currency: int, *, dead_skin_score: float | None = None,
                   dead_skin_reasons: list | None = None, price_confidence: str | None = None,
                   high_spread: bool | None = None) -> ReliabilityState:
    """Combines dead_skin.py's liquidity/consistency score, mispriced_skins.py's active
    verified gap, and a fresh rolling-window read of price_history.py into one composite
    reliability_score + state. dead_skin_score/dead_skin_reasons may be passed in by a caller
    that already has them in hand (e.g. a checkpoint entry's per-unit dead_skin_score, via
    simulate.py's _apply_mispriced_mark -- which ALREADY folds an active mispriced-skin mark
    into that per-unit score) to avoid a redundant store read AND avoid double-counting the
    same mispriced signal twice (see the `already_folded_in` check below).
    price_confidence/high_spread likewise come from the caller's most recent Stage-2
    verification (price_report.PriceRow) when one exists -- this function never triggers new
    network calls itself, only reads what's already persisted/cached."""
    caller_provided_score = dead_skin_score is not None
    if not caller_provided_score:
        stored = dead_skin_mod.get_score(market_hash_name, currency)
        dead_skin_score = stored["score"] if stored else 0.0
        dead_skin_reasons = stored["reasons"] if stored else []
    dead_skin_reasons = list(dead_skin_reasons or [])

    mispriced_gap_pct = mispriced_skins_mod.active_gap(market_hash_name, currency)
    history = _history_for(market_hash_name, currency)
    stats = rolling_stats(history)

    score = 100.0 - min(100.0, dead_skin_score)
    reasons = dead_skin_reasons

    rolling_z = stats["z"] if stats else None
    rolling_stdev = stats["stdev"] if stats else None
    stale = bool(stats["stale"]) if stats else False

    # simulate.py's _apply_mispriced_mark already adds mispriced_skins.MISPRICED_DEAD_SKIN_POINTS
    # to a caller-provided per-unit dead_skin_score (tagged "verified_overvalued"/
    # "verified_undervalued") -- re-subtracting our own gap-scaled penalty on top of an
    # already-folded-in score would double-count the exact same signal.
    already_folded_in = caller_provided_score and any(
        r in ("verified_overvalued", "verified_undervalued") for r in dead_skin_reasons)
    if mispriced_gap_pct is not None and not already_folded_in:
        score -= min(60.0, abs(mispriced_gap_pct) * 1.5)
        reasons.append("verified_price_gap")
    elif already_folded_in:
        reasons.append("verified_price_gap")

    hard_excluded = False
    if rolling_z is not None and abs(rolling_z) >= Z_SCORE_EXCLUDE:
        hard_excluded = True
        reasons.append("extreme_price_deviation")
    elif rolling_z is not None and abs(rolling_z) >= Z_SCORE_SUSPICIOUS:
        score -= 20.0
        reasons.append("abnormal_price_swing")

    if stale:
        score -= 10.0
        reasons.append("stale_listing")

    if high_spread:
        score -= 15.0
        reasons.append("cross_market_disagreement")

    if price_confidence in ("no 3rd-party match", "low volume + no 3rd-party match"):
        score -= 10.0
        reasons.append("unconfirmed_by_third_party")

    score = max(0.0, min(100.0, score))

    if hard_excluded or score < EXCLUDE_THRESHOLD:
        state = "excluded"
    elif score < HEALTHY_THRESHOLD:
        state = "suspicious"
    else:
        state = "healthy"

    # dedupe reasons while preserving a stable order (dict.fromkeys, not set(), so the log/UI
    # doesn't show reasons in a different order on every call)
    return ReliabilityState(
        market_hash_name=market_hash_name, currency=currency, state=state,
        reliability_score=score, reasons=list(dict.fromkeys(reasons)), rolling_z=rolling_z,
        rolling_stdev=rolling_stdev, dead_skin_score=dead_skin_score,
        mispriced_gap_pct=mispriced_gap_pct, stale=stale, computed_at=time.time())


def is_excluded(market_hash_name: str, currency: int, *, dead_skin_score: float | None = None,
                 dead_skin_reasons: list | None = None) -> bool:
    """Cheap hot-path check -- checks a manual override first, else computes fresh. Safe to
    call once per unit per candidate (ranking.py/sim_learning.py/sim_loop.py all do).
    dead_skin_score/dead_skin_reasons let a caller that already has a unit's own per-record
    score in hand pass it through (see compute_state's docstring for why this matters)."""
    override = _get_override(market_hash_name, currency)
    if override is not None:
        return override["state"] == "excluded"
    return compute_state(market_hash_name, currency, dead_skin_score=dead_skin_score,
                          dead_skin_reasons=dead_skin_reasons).state == "excluded"


def _unit_cache_key(name: str, currency: int, u: dict) -> tuple:
    """Identity of one unit lookup for the caller-supplied memo (see any_unit_excluded's
    `cache` parameter). Includes the per-unit dead_skin_score/reasons the caller passes
    through, not just the skin name -- two rows can name the SAME skin while carrying
    different per-record scores (simulate.py stamps those per trial), and collapsing those
    onto one cache entry would silently serve one row's reliability verdict for another's."""
    reasons = u.get("dead_skin_reasons")
    return (name, currency, u.get("dead_skin_score"),
            tuple(reasons) if reasons is not None else None)


def any_unit_excluded(input_mix: list, output_distribution: list, currency: int,
                       stattrak: bool = False, cache: dict | None = None) -> bool:
    """True if any counted input or output unit is currently Excluded -- the single predicate
    ML training filters, ranking, and best-list admission all gate on (see module docstring's
    "Excluded trade-ups are removed from ML training / Evolution / Elite Population / reward
    calculations" requirement). Reuses each unit's OWN dead_skin_score/dead_skin_reasons when
    the unit dict already carries them (every simulate.py-produced input_mix/output_distribution
    entry does) instead of re-deriving from the persisted (weekly-reviewed, possibly stale)
    dead_skin store -- a real Outcome's units (learning.py) don't carry these keys, so this
    falls back to compute_state's own store-derived default for those.

    cache: an optional caller-owned dict memoizing per-unit verdicts. A batch caller that walks
    many rows at once (sim_learning.train, which evaluates up to MAX_ROWS_FOR_ANALYSIS rows and
    then weights the survivors) hits the same handful of skins over and over -- measured at ~18
    repeat lookups per distinct skin per retrain, each one re-deriving the same dead_skin /
    mispriced / rolling-z state. The caller creates one dict, passes it to every call in that
    batch, and drops it when the batch ends, so freshness is scoped to a single operation
    instead of becoming a module-level cache that could go stale under the hot loops in
    ranking.py / sim_loop.py. None (default) is the original uncached behavior."""
    for u in list(input_mix or []) + list(output_distribution or []):
        name = steam_prices_mod.market_hash_name(u["skin"], u["wear"], stattrak)
        if cache is None:
            excluded = is_excluded(name, currency, dead_skin_score=u.get("dead_skin_score"),
                                    dead_skin_reasons=u.get("dead_skin_reasons"))
        else:
            key = ("excluded",) + _unit_cache_key(name, currency, u)
            if key not in cache:
                cache[key] = is_excluded(name, currency, dead_skin_score=u.get("dead_skin_score"),
                                          dead_skin_reasons=u.get("dead_skin_reasons"))
            excluded = cache[key]
        if excluded:
            return True
    return False


# --- Persisted state + audit log (on-demand paths only, never a hot loop) ---------------

def _load_state_store() -> dict:
    if not STATE_FILE.exists():
        return {"_last_full_recheck_at": None, "skins": {}}
    try:
        store = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"_last_full_recheck_at": None, "skins": {}}
    store.setdefault("_last_full_recheck_at", None)
    store.setdefault("skins", {})
    return store


def _save_state_store(store: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        STATE_FILE.write_text(json.dumps(store), encoding="utf-8")


def _append_log(transition: dict) -> None:
    """JSONL append, same cheap-append/corrupt-line-tolerant convention as price_history.py
    and outcomes.py -- a full audit trail without a read-modify-write on every transition."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(transition) + "\n")


def list_log(limit: int = MAX_LOG_KEPT) -> list[dict]:
    """Most recent transitions first. Skips corrupt trailing lines rather than raising, same
    convention as price_history.get_history."""
    if not LOG_FILE.exists():
        return []
    out = []
    with open(LOG_FILE, encoding="utf-8") as f:
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


def _record_state(rs: ReliabilityState) -> bool:
    """Persists rs as this skin's latest known state; appends an audit-log entry ONLY when the
    state actually changed since the last recorded check (avoids log spam on every unchanged
    recheck while keeping a complete transition history). Returns True if it changed."""
    key = _key(rs.market_hash_name, rs.currency)
    store = _load_state_store()
    prev = store["skins"].get(key)
    changed = prev is None or prev.get("state") != rs.state
    store["skins"][key] = {
        "market_hash_name": rs.market_hash_name, "currency": rs.currency, "state": rs.state,
        "reliability_score": rs.reliability_score, "reasons": rs.reasons, "rolling_z": rs.rolling_z,
        "since": rs.computed_at if changed else (prev or {}).get("since", rs.computed_at),
        "last_checked": rs.computed_at,
    }
    _save_state_store(store)
    if changed:
        _append_log({
            "market_hash_name": rs.market_hash_name, "currency": rs.currency,
            "from_state": (prev or {}).get("state"), "to_state": rs.state,
            "reliability_score": rs.reliability_score, "reasons": rs.reasons,
            "rolling_z": rs.rolling_z, "at": rs.computed_at,
        })
    return changed


def get_state(market_hash_name: str, currency: int) -> ReliabilityState:
    """compute_state() + cross-marketplace metrics + persist/log the result -- the on-demand
    (dashboard/CLI) entry point. Never call this from a per-candidate hot loop; use
    compute_state()/is_excluded() there (they skip the bulk-feed metric reads entirely)."""
    rs = compute_state(market_hash_name, currency)
    metrics = market_metrics(market_hash_name, currency)
    rs.consensus_price = metrics["consensus_price"]
    rs.price_variance = metrics["price_variance"]
    rs.n_sources = metrics["n_sources"]
    rs.source_agreement_pct = metrics["source_agreement_pct"]
    rs.liquidity_score = metrics["liquidity_score"]
    rs.market_confidence = metrics["market_confidence"]
    _record_state(rs)
    return rs


# --- Cross-marketplace metrics + reliability-weighted training ---------------------------
#
# Per the Stage-4 spec, every trade should carry consensus price / market confidence / price
# variance / liquidity / source agreement across the supported marketplaces. The per-trade,
# live-fetching version of this already exists (price_report.PriceRow's consensus_price/
# source_spread_pct/n_sources, used by Stage-2 verification); what's added here is the
# CACHED-FEED equivalent per skin -- Steam mirror + Skinport + Buff163 bulk maps
# (bulk_prices.py), zero per-item network calls -- so the reliability registry, the training
# weighter and the pattern engine can all read the same cross-market picture without burning
# rate-limited live requests. CSFloat has no bulk feed (per-item API only), so it participates
# in Stage-2 verification but not in these cached metrics.
#
# CAVEAT (see project memory / tradability.py): the Skinport/Buff163 dumps price souvenir
# items under the plain market name for some skins, which can skew a single skin's agreement
# numbers. These metrics are ADVISORY annotations and training weights -- tradability decisions
# stay Steam-feed-only in tradability.py, exactly as before.

WEIGHT_FULLY_VERIFIED = 1.0     # confirmed across marketplaces, no disagreement
WEIGHT_MINOR_DISAGREEMENT = 0.75
WEIGHT_STEAM_ONLY = 0.35        # priced on Steam alone -- usable, but far less trusted
WEIGHT_ANOMALY = 0.0            # currently Excluded -- keep the row, contribute nothing

SOURCE_DISAGREEMENT_PCT = 25.0  # cheapest-vs-priciest THIRD-PARTY source gap above this =
# "disagreement" (matches price_report.HIGH_SPREAD_THRESHOLD_PCT's intent for the cached-feed
# variant). Deliberately compares Skinport vs Buff163 only, never Steam -- see
# source_agreement_pct's comment in market_metrics() for why Steam can't be part of this
# spread.


def market_metrics(market_hash_name: str, currency: int) -> dict:
    """Cached-feed cross-marketplace metrics for one skin -- see block comment above. All
    fields None/0 when no source currently prices the item."""
    from . import bulk_prices as bulk_prices_mod  # lazy: keeps hot-path imports unchanged
    prices = {}
    steam_entry = bulk_prices_mod.get_steam_entry(market_hash_name, currency)
    if steam_entry and steam_entry.get("lowest_price") is not None:
        prices["steam"] = float(steam_entry["lowest_price"])
    for source, getter in (("skinport", bulk_prices_mod.get_skinport_map),
                            ("buff163", bulk_prices_mod.get_buff163_map)):
        p = getter(currency).get(market_hash_name)
        if p is not None:
            prices[source] = float(p)

    values = sorted(prices.values())
    n = len(values)
    consensus = statistics.median(values) if values else None
    variance = statistics.pvariance(values) if n >= 2 else None

    # source_agreement_pct measures whether the INDEPENDENT third-party feeds (Skinport,
    # Buff163) corroborate each other -- deliberately EXCLUDING Steam. Steam Community Market
    # runs a resale fee that leaves its price structurally ~25-50% above Skinport/Buff163 for
    # nearly every skin (confirmed empirically 2026-07 -- e.g. AK-47 Nightwish: steam 89.72 vs
    # skinport 59.37 / buff163 60.11), so a spread that includes Steam reads as "disagreement"
    # almost universally regardless of actual data quality, making the signal useless for both
    # market_confidence here and training_weight's any_disagreement check below. Steam's own
    # gap vs the market is already a first-class, separately-calibrated signal elsewhere --
    # dead_skin.py's large_price_gap bucket (cached feed) and mispriced_skins.py's active_gap
    # (live-verified) -- both of which already feed compute_state()/dead_skin_score, which
    # training_weight also checks (state == "excluded"/"suspicious"). So dropping Steam here
    # doesn't lose that signal, it just stops re-measuring it a second time, badly.
    thirdparty_values = sorted(v for k, v in prices.items() if k != "steam")
    if len(thirdparty_values) >= 2:
        tp_consensus = statistics.median(thirdparty_values)
        if tp_consensus:
            spread_frac = (thirdparty_values[-1] - thirdparty_values[0]) / tp_consensus
            agreement = round(max(0.0, 100.0 * (1.0 - min(1.0, spread_frac))), 1)
        else:
            agreement = None
    else:
        agreement = None

    stored = dead_skin_mod.get_score(market_hash_name, currency)
    liquidity = round(max(0.0, 100.0 - (stored["score"] if stored else 0.0)), 1)

    # Confidence: how much cross-checking actually backs this skin's price right now -- "high"
    # now means all 3 feeds present AND the two independent ones actually agree with each
    # other, a real corroboration rather than "Steam agrees with its own fee markup".
    if n >= 3 and (agreement or 0) >= 75:
        confidence = "high"
    elif n >= 2:
        confidence = "medium"
    elif n == 1:
        confidence = "low"
    else:
        confidence = "none"

    return {"sources": prices, "n_sources": n, "consensus_price": consensus,
            "price_variance": variance, "source_agreement_pct": agreement,
            "liquidity_score": liquidity, "market_confidence": confidence}


def training_weight(input_mix: list, output_distribution: list, currency: int,
                     stattrak: bool = False, cache: dict | None = None) -> tuple[float, str]:
    """Reliability weight for ONE training sample (a simulated trial or a real outcome),
    per the Stage-4 spec's ladder: fully verified across marketplaces 1.0, minor
    disagreement 0.75, Steam-only 0.35, known anomaly 0. Returns (weight, reason).

    Walks every counted unit exactly like any_unit_excluded (reusing the same name
    construction), but instead of a binary gate it classifies the WHOLE sample by its
    weakest signal: any Excluded unit -> anomaly (0); no unit priced by any third-party
    source -> steam-only; a THIRD-PARTY MINORITY (n_with_thirdparty*2 <= len(units)), any
    Suspicious unit, or any cheapest-vs-priciest source gap over SOURCE_DISAGREEMENT_PCT ->
    minor disagreement; otherwise fully verified. Deliberately a MAJORITY gate rather than
    requiring every unit covered: a strict all-units requirement left fully_verified at
    ~0 rows across every population (a 10-unit trade-up needs all 10 priced on Skinport/
    Buff163 simultaneously, which almost never happens), so the weighting ladder was
    paying its full computational cost for no real differentiation. Requiring a majority
    still means the sample's price picture is mostly cross-checked, just not unanimously.
    Questionable samples keep their row -- only their influence shrinks (the caller passes
    these as sklearn sample_weights, see ml_common.fit_best_model).

    cache: optional caller-owned memo, exactly the same contract and rationale as
    any_unit_excluded's -- and deliberately the SAME dict in sim_learning.train(), since that
    function walks every row's units through both functions back to back. Keys are namespaced
    per function so the two never collide."""
    units = list(input_mix or []) + list(output_distribution or [])
    if not units:
        return WEIGHT_STEAM_ONLY, "no_units"
    n_with_thirdparty = 0
    any_suspicious = False
    any_disagreement = False
    for u in units:
        name = steam_prices_mod.market_hash_name(u["skin"], u["wear"], stattrak)
        key = (("weight_unit",) + _unit_cache_key(name, currency, u)) if cache is not None else None
        cached = cache.get(key) if cache is not None else None
        if cached is None:
            rs = compute_state(name, currency, dead_skin_score=u.get("dead_skin_score"),
                                dead_skin_reasons=u.get("dead_skin_reasons"))
            override = _get_override(name, currency)
            state = override["state"] if override is not None else rs.state
            metrics = market_metrics(name, currency)
            has_thirdparty = any(s != "steam" for s in metrics["sources"])
            disagrees = (has_thirdparty
                          and metrics["source_agreement_pct"] is not None
                          and metrics["source_agreement_pct"] < 100.0 - SOURCE_DISAGREEMENT_PCT)
            cached = (state, has_thirdparty, disagrees)
            if cache is not None:
                cache[key] = cached
        state, has_thirdparty, disagrees = cached
        if state == "excluded":
            return WEIGHT_ANOMALY, "known_anomaly"
        if state == "suspicious":
            any_suspicious = True
        if has_thirdparty:
            n_with_thirdparty += 1
            if disagrees:
                any_disagreement = True
    if n_with_thirdparty == 0:
        return WEIGHT_STEAM_ONLY, "steam_only"
    if any_suspicious or any_disagreement or n_with_thirdparty * 2 <= len(units):
        return WEIGHT_MINOR_DISAGREEMENT, "minor_disagreement"
    return WEIGHT_FULLY_VERIFIED, "fully_verified"


def _tracked_keys(currency: int | None = None) -> list:
    """Every skin+currency any existing registry already considered worth persistently
    tracking -- dead_skin.py's full store (not just currently-dead entries) union
    mispriced_skins.py's marks (active or expired) union any skin with a manual override (a
    user can override a skin the CLI reaches by name even if neither registry has scored it
    yet -- without this, that override would silently never show up in list_tracked()).
    Bounded by those stores' own size (150 + 490 entries today), not the unbounded simulation
    firehose."""
    seen = set()
    keys = []
    for entry in dead_skin_mod.list_all_tracked(currency):
        k = (entry["market_hash_name"], entry["currency"])
        if k not in seen:
            seen.add(k)
            keys.append(k)
    for entry in mispriced_skins_mod.list_mispriced(currency):
        k = (entry["market_hash_name"], entry["currency"])
        if k not in seen:
            seen.add(k)
            keys.append(k)
    with _override_lock:
        _ensure_overrides_loaded()
        override_entries = list(_override_store["skins"].values())
    for entry in override_entries:
        if currency is not None and entry["currency"] != currency:
            continue
        k = (entry["market_hash_name"], entry["currency"])
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def list_tracked(currency: int | None = None) -> list[dict]:
    """Every currently-tracked skin's state (Excluded/Suspicious first), for the dashboard's
    Market Reliability panel and the CLI. Calling this also persists/logs each entry's latest
    state (see get_state) -- fine for an on-demand listing, not called in a hot loop."""
    out = []
    for name, cur in _tracked_keys(currency):
        rs = get_state(name, cur)
        override = _get_override(name, cur)
        d = asdict(rs)
        d["overridden"] = override is not None
        d["override_note"] = override.get("note", "") if override else ""
        if override is not None:
            d["state"] = override["state"]
        out.append(d)
    order = {"excluded": 0, "suspicious": 1, "healthy": 2}
    out.sort(key=lambda d: (order.get(d["state"], 3), -d["reliability_score"]))
    return out


def state_counts() -> dict:
    """{healthy, suspicious, excluded} counts from the PERSISTED state store -- a cheap read
    for KPI displays (xlsx export's vulnerability breakdown, dashboards) that must not trigger
    a full list_tracked() recompute of every tracked skin."""
    store = _load_state_store()
    counts = {"healthy": 0, "suspicious": 0, "excluded": 0}
    for entry in store["skins"].values():
        state = entry.get("state")
        if state in counts:
            counts[state] += 1
    return counts


def recheck_due() -> bool:
    """Gate for run_periodic_recheck() -- see RECHECK_POLL_INTERVAL_SECONDS. Same
    due-check/run split as dead_skin.review_due()/run_weekly_review()."""
    store = _load_state_store()
    last = store["_last_full_recheck_at"]
    return last is None or (time.time() - last) >= RECHECK_POLL_INTERVAL_SECONDS


def run_periodic_recheck() -> dict:
    """Called from retrain_loop.py's existing poll once recheck_due() is True -- re-evaluates
    every tracked skin from CURRENT signals and logs any transition. This is what actually
    gives Excluded skins their re-verification: nothing here holds a skin excluded past
    whatever its live signals currently justify."""
    n_checked = n_transitioned = 0
    for name, cur in _tracked_keys():
        rs = compute_state(name, cur)
        if _record_state(rs):
            n_transitioned += 1
        n_checked += 1
    store = _load_state_store()
    store["_last_full_recheck_at"] = time.time()
    _save_state_store(store)
    return {"checked": n_checked, "transitioned": n_transitioned, "checked_at": time.time()}


# --- Manual override --------------------------------------------------------------------

def _ensure_overrides_loaded() -> None:
    global _override_store
    if _override_store is not None:
        return
    if OVERRIDES_FILE.exists():
        try:
            _override_store = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _override_store = {"skins": {}}
    else:
        _override_store = {"skins": {}}
    _override_store.setdefault("skins", {})


def _save_overrides_locked() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(json.dumps(_override_store), encoding="utf-8")


def _get_override(market_hash_name: str, currency: int) -> dict | None:
    with _override_lock:
        _ensure_overrides_loaded()
        return _override_store["skins"].get(_key(market_hash_name, currency))


def set_override(market_hash_name: str, currency: int, state: str, note: str = "") -> None:
    """Manual override, always available per spec -- takes priority over the computed state
    everywhere (is_excluded, any_unit_excluded, list_tracked) until cleared."""
    if state not in VALID_STATES:
        raise ValueError(f"state must be one of {VALID_STATES}, got {state!r}")
    now = time.time()
    with _override_lock:
        _ensure_overrides_loaded()
        _override_store["skins"][_key(market_hash_name, currency)] = {
            "market_hash_name": market_hash_name, "currency": currency, "state": state,
            "note": note, "set_by": "manual", "set_at": now,
        }
        _save_overrides_locked()
    _append_log({"market_hash_name": market_hash_name, "currency": currency, "from_state": None,
                 "to_state": state, "reliability_score": None, "reasons": ["manual_override"],
                 "rolling_z": None, "at": now})


def clear_override(market_hash_name: str, currency: int) -> bool:
    with _override_lock:
        _ensure_overrides_loaded()
        removed = _override_store["skins"].pop(_key(market_hash_name, currency), None) is not None
        if removed:
            _save_overrides_locked()
    return removed


def forget_all() -> None:
    """Test/maintenance helper, same as invalid_skins.forget_all / mispriced_skins.forget_all."""
    global _override_store
    with _override_lock:
        _override_store = None
