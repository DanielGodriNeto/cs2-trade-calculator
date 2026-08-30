"""Population of independent "sons" running the guided random-trade simulation in parallel.

Each son is a full instance of the same search loop -- draw random candidates
(random_trade.py), simulate them (simulate.py, cache first, live Steam fetch on a miss), log
every trial to its OWN
data/sons/son_N/simulations.jsonl, and train its OWN surrogate model
(data/sons/son_N/model/sim_roi_model.joblib) -- but each son has a different search
personality (agents.py), starting from a different `temperature` controlling how strongly it
trusts that model when choosing which random candidates to actually simulate each round (see
random_trade.generate_batch/_softmax_weighted_sample): low temperature sons mostly exploit
their own model's top picks, high temperature sons mostly keep exploring broadly. Those
personalities SELF-TUNE over time (adaptive.py): every few rounds each son A/B-tests a small
tweak of its own search parameters against a ROI-first + collection-novelty reward and keeps
the tweak only if it measurably helped, within bounds that keep the four agents distinct and
exploration alive. Candidate selection also carries a UCB novelty bonus for collections a son
has rarely simulated (checkpoint["coverage"]), so unexplored territory always competes for the
simulate() budget. Running several of these side by side means a son that plateaus in a local
optimum doesn't stall the whole search -- the others keep going, and the plateaued one can be
individually nudged or fully reset (see nudge()/full_reset() below) without touching the others.

Each son also tracks a `history` of (round, best-ROI-so-far) snapshots and a
`rounds_since_improvement` counter, so the dashboard can show whether a son is actually making
progress or has flatlined (`stuck`), rather than just a static status string.

IMPORTANT: this must never feed anything back into learning.py/data/outcomes.jsonl -- see
that module's own docstring for why. Each son's
own sim_learning.py model only ever reads that SAME son's own simulations.jsonl -- never
another son's, and never the real outcomes.jsonl.
"""
import json
import math
import os
import random
import statistics
import threading
import time
from pathlib import Path

from . import adaptive as adaptive_mod
from . import agents as agents_mod
from . import bulk_prices as bulk_prices_mod
from . import data as data_mod
from . import invalid_skins as invalid_skins_mod
from . import user_overrides as user_overrides_mod
from . import market_reliability as market_reliability_mod
from . import mutation as mutation_mod
from . import random_trade as random_trade_mod
from . import ranking as ranking_mod
from . import sim_blacklist as sim_blacklist_mod
from . import simulate as simulate_mod
from . import steam_prices as steam_prices_mod
from . import sim_learning as sim_learning_mod
from . import verified_low_roi as verified_low_roi_mod
from . import verify_queue as verify_queue_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SONS_DIR = DATA_DIR / "sons"

N_SONS = len(agents_mod.AGENT_PROFILES)
# Kept for backward compat with the one existing reader (web/server.py's /api/constants) --
# now DERIVED per-agent from memory_weight (see agents.py.base_temperature) instead of a
# hardcoded list. Order matches son_id 0..N_SONS-1.
SON_TEMPERATURES = [agents_mod.base_temperature(agents_mod.profile_for(i)) for i in range(N_SONS)]

_RARITY_ID_BY_NAME = {name: rid for rid, name in data_mod.RARITY_CHAIN}  # "Mil-Spec Grade" -> "rarity_rare_weapon"
# -- reverse of data.RARITY_CHAIN, for reconstructing a random_trade.Candidate from a stored
# best-list/archive entry (which carries rarity NAMES, not ids). See _entry_to_candidate.

BATCH_SIZE = 200          # random candidates drawn per round before scoring/filtering
SIMULATE_PER_ROUND = 40   # of that batch, how many actually get simulate.simulate() called
BEST_KEPT = 50            # best-known simulations kept per son's checkpoint
HISTORY_KEEP = 300        # progress-history snapshots kept per son (older ones trimmed)
ROUND_COOLDOWN_SECONDS = 10  # idle pause between rounds when the round finished cleanly (a
# round with live-fetch cache misses can itself take much longer than this, rate-limited by
# steam_prices.py -- this constant only governs the gap AFTER a round completes)

IMPROVEMENT_EPSILON_PP = 1.0    # a new best-ROI must beat the old one by more than this (percentage
# points) to count as a real improvement -- guards against resetting the stuck-counter on noise
STUCK_THRESHOLD_ROUNDS = 15     # rounds without a real improvement before a son is flagged "stuck"
NUDGE_DURATION_ROUNDS = 5       # how many rounds a Nudge's temperature boost lasts
NUDGE_KICK_FACTOR = 3.0         # temporary temperature multiplier applied by Nudge


def son_dir(son_id: int) -> Path:
    return SONS_DIR / f"son_{son_id}"


def son_checkpoint_file(son_id: int) -> Path:
    return son_dir(son_id) / "checkpoint.json"


def son_sims_file(son_id: int) -> Path:
    return son_dir(son_id) / "simulations.jsonl"


def son_model_dir(son_id: int) -> Path:
    return son_dir(son_id) / "model"


def son_invalid_file(son_id: int) -> Path:
    return son_dir(son_id) / "invalid_simulations.jsonl"


def son_round_history_file(son_id: int) -> Path:
    """Uncapped, append-only mirror of checkpoint.json's `history` list (which is deliberately
    trimmed to the last HISTORY_KEEP=300 rounds to keep the checkpoint small/fast to rewrite
    every round). A long-running son can pass 300 rounds -- once it does, the checkpoint quietly
    drops its earliest progress forever. This file never trims, so a full round-by-round ROI/
    profit/confidence history survives for as long as the son has run, for later analysis (price
    trend charts, medium-term ROI progress, etc.) even after the checkpoint itself has moved on --
    same "never truncate, append only" convention as simulations.jsonl/invalid_simulations.jsonl."""
    return son_dir(son_id) / "round_history.jsonl"


def _empty_checkpoint() -> dict:
    # cumulative_cost/cumulative_expected_profit: running totals across EVERY valid simulated
    # candidate this son has ever produced (not just its curated best-known list) -- summing
    # each record's own cost/expected_profit as if every single one had actually been bought,
    # since this codebase always computes exact probability-weighted expected value rather than
    # sampling a random outcome (see simulate.py's module docstring). Purely a hypothetical
    # "what if you blindly bought everything this bot proposed" counter for the dashboard --
    # never influences ranking/training/best-known selection, see run_round().
    # adaptive: this son's self-tuning search-parameter state (initialized lazily by
    # adaptive.ensure_state since it depends on the son's own agent profile -- this function is
    # profile-agnostic). coverage: unit-share-weighted count of valid sims per collection,
    # feeding adaptive.novelty_bonus's UCB selection bonus. Both are per-son search state, so
    # they belong in the checkpoint (wiped by full_reset) rather than any shared file.
    return {"round": 0, "n_simulated": 0, "n_invalid": 0, "n_low_volume": 0,
            "n_simulated_at_last_train": 0, "best": [],
            "history": [], "best_roi_ever": None, "rounds_since_improvement": 0,
            "nudge_rounds_remaining": 0, "updated_at": None,
            "cumulative_cost": 0.0, "cumulative_expected_profit": 0.0,
            "adaptive": None, "coverage": {}, "recovery": None}


def load_checkpoint(path: Path) -> dict:
    """One retry on a JSONDecodeError before giving up -- guards against reading a checkpoint
    mid-write from a concurrent son thread. save_checkpoint() below writes atomically (temp
    file + os.replace) so a torn read should no longer be possible going forward, but this
    stays as cheap defense-in-depth (a genuinely corrupt pre-existing file still falls through
    to _empty_checkpoint on the second failure, same as before)."""
    if not path.exists():
        return _empty_checkpoint()
    cp = None
    for attempt in range(2):
        try:
            cp = json.loads(path.read_text(encoding="utf-8"))
            break
        except json.JSONDecodeError:
            if attempt == 0:
                time.sleep(0.05)
    if cp is None:
        return _empty_checkpoint()
    for k, v in _empty_checkpoint().items():
        cp.setdefault(k, v)
    return cp


def save_checkpoint(path: Path, cp: dict) -> None:
    """Atomic write (temp file + os.replace) so a concurrent load_checkpoint() call never
    observes a partially-written file -- os.replace is atomic on both POSIX and Windows when
    source/dest share a filesystem, unlike the previous in-place path.write_text()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cp["updated_at"] = time.time()
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(cp), encoding="utf-8")
    os.replace(tmp_path, path)


def _entry_output_unsellable(entry: dict) -> bool:
    """True when this best-list/archive entry's REVENUE rests on an output priced with no
    Steam sale evidence behind it -- the retroactive half of the "AWP | Gungnir" fix (see
    simulate._price's allow_thirdparty note): entries banked BEFORE outputs became
    steam-only-priced can still carry a third-party-fallback output price, so they're hidden
    here rather than waiting for an agent to happen to re-simulate the composition.

    A priced output whose exact market_hash_name shows no Steam median sale in ANY of the
    mirror's 24h-90d windows (bulk_prices.has_market_listing is False -- None, feed down,
    never filters) could not have gotten that price from Steam. A user price override on that
    name is exempt: the user's own number is authoritative wherever it came from."""
    stattrak = bool(entry.get("stattrak"))
    for o in entry.get("output_distribution") or []:
        if o.get("price") is None or not (o.get("prob_pct") or 0):
            continue
        name = steam_prices_mod.market_hash_name(o.get("skin", ""), o.get("wear", ""), stattrak)
        if user_overrides_mod.get_override_price(name) is not None:
            continue
        if bulk_prices_mod.has_market_listing(name) is False:
            return True
    return False


def _entry_uses_unreal_collection(entry: dict) -> bool:
    """True when any leg of this entry references a "collection" the skin database no longer
    considers real (see data.valid_collection_names / data._is_real_collection) -- catches
    entries a son banked BEFORE that filter existed (e.g. the upstream feed's "Limited Edition
    Item" placeholder bucket, a handful of unrelated promotional skins with no genuine
    drop-pool collection), the same retroactive-cleanup shape _entry_output_unsellable already
    uses for the Gungnir fix. Checks every distinct collection actually referenced in
    input_mix (a mixed-collection candidate can have several), not just primary_collection."""
    valid = data_mod.valid_collection_names()
    collections = {u.get("collection") for u in (entry.get("input_mix") or []) if u.get("collection")}
    collections.add(entry.get("primary_collection"))
    return any(c and c not in valid for c in collections)


def _entry_hidden(entry: dict) -> bool:
    """Every reason an existing best-list/archive entry must not be shown or re-admitted:
    the user marked the composition done (sim_blacklist), the user permanently blacklisted a
    skin it touches (user_overrides), its revenue rests on a non-Steam-sellable output
    (see _entry_output_unsellable), it references a collection the skin database no longer
    treats as real (see _entry_uses_unreal_collection), or a PRIOR Stage-2 verification already
    proved this exact composition isn't actually profitable (verified_low_roi.py) -- without
    this last check a fresh, unverified re-simulation of the same bugged composition would keep
    resurfacing and keep getting queued for another live Skinport/Buff163/CSFloat check of an
    answer this tool already has. Shared by _consider_best (admission + self-clean) and both
    list functions (immediate hiding, no re-simulation needed)."""
    return (sim_blacklist_mod.is_blacklisted(entry)
            or user_overrides_mod.entry_contains_blacklisted_skin(entry)
            or _entry_output_unsellable(entry)
            or _entry_uses_unreal_collection(entry)
            or verified_low_roi_mod.is_marked(sim_blacklist_mod.signature_of(entry)))


def _consider_best(cp: dict, record, profile: "agents_mod.AgentProfile") -> None:
    """Skips a record flagged low_volume_warning entirely when THIS agent cares enough about
    liquidity to reject it outright (liquidity_importance > 0.5 -- e.g. the ROI Accountant
    always does; the Discovery Agent, which deliberately explores illiquid corners, doesn't).
    Also skips a stale-priced record for a cautious agent (risk_tolerance < 0.35) -- it isn't
    willing to bank a find as "best" when the cost basis wasn't even freshly priced. A
    thinly-traded/stale price shouldn't be presented as a "best find" any more than it should
    be trained on (see simulate.SimulationRecord's own docstring for why); the record is still
    preserved in simulations.jsonl either way (see run_round) -- only the curated best-known
    list is agent-personality-gated. Stores the full input_mix/output_distribution (not just a
    summary) so the dashboard's per-skin image/wear breakdown never needs to re-read the raw
    jsonl by id."""
    if record.low_volume_warning and profile.liquidity_importance > 0.5:
        return
    if record.cost_basis_source == "stale" and profile.risk_tolerance < 0.35:
        return
    # market_reliability.py's unified Excluded state -- applies regardless of THIS agent's own
    # personality (unlike the two checks above), since "excluded from Elite Population" per
    # spec is a market-fact gate, not a per-agent risk-tolerance judgment call.
    if market_reliability_mod.any_unit_excluded(record.input_mix, record.output_distribution,
                                                 currency=1, stattrak=record.stattrak):
        return
    entry = {"id": record.id, "primary_collection": record.primary_collection,
             "from_rarity": record.from_rarity, "to_rarity": record.to_rarity,
             "stattrak": record.stattrak, "cost": record.cost,
             "expected_roi_pct": record.expected_roi_pct, "cost_basis_source": record.cost_basis_source,
             "input_mix": record.input_mix, "output_distribution": record.output_distribution,
             "agent": record.agent, "confidence_score": record.confidence_score,
             "verified_roi_pct": record.verified_roi_pct, "verified_at": record.verified_at}
    # A trade-up the user already executed and blacklisted should never be re-admitted, no
    # matter how good its ROI keeps looking each round (see sim_blacklist.py) -- and the same
    # goes for one touching a user-blacklisted SKIN, or one whose revenue rests on an output
    # with no Steam sale evidence (see _entry_hidden). This also drops any already-stored
    # hidden entries from this son's in-memory best list, so a son that banked one before the
    # rule/blacklist applied self-cleans on its next round (the running son owns this
    # checkpoint object and persists it after the round -- no cross-thread disk write).
    if _entry_hidden(entry):
        return
    cp["best"] = [e for e in cp["best"] if not _entry_hidden(e)]
    # Auto-dedupe by composition: a son re-simulates the exact same skin/wear/count combination
    # over and over (each run gets a fresh id -- see simulate._new_id), so without this its best
    # list fills up with many copies of one trade-up -- the "it just places the same trade in
    # multiple times" problem. Keep only ONE entry per signature, replacing the stored copy only
    # when this run scores a higher EFFECTIVE ROI (verified-aware, see ranking.effective_roi --
    # so the best-priced instance of the combination wins).
    sig = sim_blacklist_mod.signature_of(entry)
    existing = next((e for e in cp["best"] if sim_blacklist_mod.signature_of(e) == sig), None)
    if existing is not None:
        # A prior Stage-2 verification (verify_queue.py) already measured this exact
        # composition's real cross-marketplace ROI -- carry it forward onto the fresh
        # re-simulation instead of silently discarding it. Without this, a fresh unverified
        # re-simulation would erase the verification the moment its (possibly still-inflated,
        # bugged) raw ROI looks better, and the same composition would get queued for another
        # live Skinport/Buff163/CSFloat check next round even though this tool already knows
        # the verified answer (see verified_low_roi.py).
        if existing.get("verified_roi_pct") is not None:
            entry["verified_roi_pct"] = existing["verified_roi_pct"]
            entry["price_confidence"] = existing.get("price_confidence")
            entry["verified_at"] = existing.get("verified_at")
        if ranking_mod.effective_roi(entry) <= ranking_mod.effective_roi(existing):
            return
        cp["best"].remove(existing)
    cp["best"].append(entry)
    cp["best"].sort(key=ranking_mod.effective_roi, reverse=True)
    del cp["best"][BEST_KEPT:]


def _percentile(vals: list, pct: float) -> float:
    """Linear-interpolated pct-th percentile of `vals` (0 <= pct <= 100). Small, self-contained
    -- the ensemble spread is p90-p10 over at most N_SONS predictions, not worth pulling numpy
    into a read path for."""
    s = sorted(vals)
    k = (len(s) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def _entry_to_candidate(entry: dict, idx: dict):
    """Rebuilds a random_trade.Candidate from a stored best-list/archive entry so every
    trained son's sim_learning model can score this exact composition (see
    _attach_ensemble_predictions). The entry carries rarity NAMES + a flat input_mix of
    {collection, skin(name str), wear, count} rows; a Candidate needs rarity ids, per-collection
    legs, and REAL output skin dicts (predict_expected_roi reads leg.outputs[k]["name"]/["wears"]
    and unit["skin"]["name"] for its warm-cache price features -- Task 9). Returns None whenever
    the entry can't be faithfully reconstructed (unknown rarity, a collection the current skin
    DB doesn't know, no outputs at the target rarity) -- a missing ensemble signal, never a
    broken ranking call. Any exception -> None for the same reason."""
    try:
        from_id = _RARITY_ID_BY_NAME.get(entry["from_rarity"])
        to_id = _RARITY_ID_BY_NAME.get(entry["to_rarity"])
        if from_id is None or to_id is None:
            return None
        stattrak = bool(entry.get("stattrak"))
        groups: dict = {}  # collection -> [unit rows], first-seen order preserved
        for u in entry.get("input_mix") or []:
            groups.setdefault(u["collection"], []).append(u)
        if not groups:
            return None
        primary = entry.get("primary_collection")
        ordered = ([primary] + [c for c in groups if c != primary]) if primary in groups else list(groups)
        legs = []
        for collection in ordered:
            if collection not in idx:
                return None
            outs = idx.get(collection, {}).get(to_id, [])
            if stattrak:
                outs = [s for s in outs if s.get("stattrak")]
            if not outs:
                return None
            units = [{"skin": {"name": u["skin"]}, "wear": u["wear"], "count": u["count"]}
                     for u in groups[collection]]
            legs.append(random_trade_mod.CandidateLeg(collection=collection, units=units, outputs=list(outs)))
        return random_trade_mod.Candidate(
            from_rarity_id=from_id, from_rarity_name=entry["from_rarity"],
            to_rarity_id=to_id, to_rarity_name=entry["to_rarity"],
            stattrak=stattrak, legs=legs)
    except Exception:
        return None


def _attach_ensemble_predictions(entries: list) -> None:
    """Mutates `entries` in place: for every entry that reconstructs to a Candidate (see
    _entry_to_candidate), score it against EVERY currently-trained son's ROI surrogate and
    attach ensemble_roi_median / ensemble_roi_spread (p90-p10) when >=3 models produced a
    number -- ranking._ensemble_agreement_bonus turns a tight consensus that also matches the
    formula ROI into a small confidence bonus. <3 models -> neither key attached (no signal).
    Every predict_expected_roi call is guarded: a stale/broken per-son model must never break a
    read path."""
    trained = [(son_model_dir(i), son_sims_file(i), sim_learning_mod.collection_roi_lookup(son_sims_file(i)))
               for i in range(N_SONS)
               if sim_learning_mod.get_model_status(son_model_dir(i)).get("trained")]
    if len(trained) < 3:
        return  # can never reach the >=3-prediction bar below
    idx = data_mod.collections_index(data_mod.load_skins())
    for entry in entries:
        cand = _entry_to_candidate(entry, idx)
        if cand is None:
            continue
        preds = []
        for md, sf, lookup in trained:
            try:
                p = sim_learning_mod.predict_expected_roi(cand, md, sf, collection_roi_lookup=lookup)
            except Exception:
                p = None
            if p is not None:
                preds.append(float(p))
        if len(preds) >= 3:
            entry["ensemble_roi_median"] = statistics.median(preds)
            entry["ensemble_roi_spread"] = _percentile(preds, 90) - _percentile(preds, 10)


def list_best_simulations(son_id: int | None = None, collection_filter: str | None = None) -> list:
    """Reads checkpoint(s) straight off disk -- no running loop required. son_id=None merges
    across every son (tagging each entry with its son id) for a combined leaderboard; used by
    the CLI's `best-simulations` command and the web dashboard's combined table.

    Ranked by ranking.rank()'s weighted composite score (60% ROI / 15% profit / 10% liquidity
    / 10% confidence / 5% discovery), not raw ROI alone -- "a smaller but reliable profit
    should beat fake high ROI" per spec. The raw expected_roi_pct field is still present on
    every returned entry (ranking.rank() adds fields, never removes any), so callers that only
    care about the plain number don't need to change."""
    son_ids = [son_id] if son_id is not None else list(range(N_SONS))
    out = []
    for sid in son_ids:
        cp = load_checkpoint(son_checkpoint_file(sid))
        # Hide blacklisted ("already done") trade-ups, user-blacklisted-skin trade-ups, and
        # non-Steam-sellable-output entries immediately, even ones a son banked before the rule
        # applied and hasn't cycled out of its checkpoint yet (see _entry_hidden) -- and apply
        # any active manual price overrides (cost/ROI recomputed on a copy, see
        # user_overrides.repriced_entry_copy) BEFORE ranking so Min-ROI filters see the
        # corrected numbers.
        out.extend({**user_overrides_mod.repriced_entry_copy(e), "son": sid}
                   for e in cp["best"] if not _entry_hidden(e))
    if collection_filter:
        out = [e for e in out if collection_filter.lower() in e["primary_collection"].lower()]
    # 6-son ensemble trust signal: score each entry against every trained son's ROI surrogate
    # and attach ensemble_roi_median/spread, so ranking._ensemble_agreement_bonus can reward a
    # tight cross-model consensus that also matches the formula ROI (see that function).
    _attach_ensemble_predictions(out)
    # Cross-son dedupe: the same composition can be independently banked by several sons, so
    # after ranking keep only the single best-ranked instance of each signature -- one row per
    # unique trade-up in the opportunities table, never the same one repeated across agents
    # (mirrors _consider_best's within-son dedupe above).
    ranked = ranking_mod.rank(out)
    seen: set = set()
    deduped = []
    for e in ranked:
        sig = sim_blacklist_mod.signature_of(e)
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(e)
    return deduped


_archive_cache: dict = {}  # sims_file -> {"pos": bytes already folded in, "by_sig": {sig: entry}}
# Backs list_all_opportunities() below -- see that function's docstring for why this exists
# separately from the BEST_KEPT=50-capped checkpoint "best" list.
_archive_cache_lock = threading.Lock()  # web/server.py's ThreadingHTTPServer spawns a new
# thread per request, and the archive/xlsx-export endpoints both reach _update_archive_cache
# directly (no per-son thread involved) -- without a lock, two concurrent requests could race
# on the read-check-mutate sequence below (e.g. both reading a stale cache["pos"] before either
# writes it back).


def _record_to_entry(record) -> dict:
    """Same field subset _consider_best stores on a checkpoint 'best' entry, so archive rows
    render through the exact same dashboard/export code as the curated best-known list."""
    return {"id": record.id, "primary_collection": record.primary_collection,
            "from_rarity": record.from_rarity, "to_rarity": record.to_rarity,
            "stattrak": record.stattrak, "cost": record.cost,
            "expected_roi_pct": record.expected_roi_pct, "cost_basis_source": record.cost_basis_source,
            "input_mix": record.input_mix, "output_distribution": record.output_distribution,
            "agent": record.agent, "confidence_score": record.confidence_score,
            "verified_roi_pct": record.verified_roi_pct, "verified_at": record.verified_at}


def _update_archive_cache(sims_file: Path) -> dict:
    """Incrementally folds only the bytes appended to sims_file since the last call into a
    signature-deduped {sig: entry} map (keeping whichever instance of a repeated composition
    scored the higher ROI) -- a plain byte-offset tail-read, NOT a full re-parse of the whole
    file, so this stays cheap even once simulations.jsonl has accumulated tens of thousands of
    lines over weeks of runtime. Mirrors the mtime-cache idiom used elsewhere in this codebase
    (e.g. learning._collection_roi_lookup) but keyed on a byte offset instead, since we need to
    resume mid-file rather than just detect "did it change".

    A hard full_reset() truncates/recreates the son's sims_file -- detected below (current size
    < the cached offset) and treated as a cold start, discarding this son's stale archive
    entries along with it."""
    with _archive_cache_lock:
        cache = _archive_cache.setdefault(sims_file, {"pos": 0, "by_sig": {}})
        if not sims_file.exists():
            cache["pos"], cache["by_sig"] = 0, {}
            return cache["by_sig"]
        size = sims_file.stat().st_size
        if size < cache["pos"]:
            cache["pos"], cache["by_sig"] = 0, {}
        if size == cache["pos"]:
            return cache["by_sig"]
        with open(sims_file, encoding="utf-8") as f:
            f.seek(cache["pos"])
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = simulate_mod.SimulationRecord(**json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue  # a torn trailing line from a read racing a concurrent append --
                    # same skip-corrupt-lines convention as simulate.load_simulations(); picked
                    # up whole on the next call once the writer's flush completes.
                entry = _record_to_entry(record)
                sig = sim_blacklist_mod.signature_of(entry)
                existing = cache["by_sig"].get(sig)
                if existing is None or entry["expected_roi_pct"] > existing["expected_roi_pct"]:
                    cache["by_sig"][sig] = entry
        cache["pos"] = size
        return cache["by_sig"]


def list_all_opportunities(son_id: int | None = None, collection_filter: str | None = None,
                            min_roi: float | None = None) -> list:
    """EVERY unique trade-up composition this population has ever successfully simulated,
    across every round it has ever run -- unlike list_best_simulations() (backed by the
    checkpoint's "best" list, capped at BEST_KEPT=50 per son with lower-ranked finds permanently
    dropped via `del cp["best"][BEST_KEPT:]`), this reads the full, append-only
    simulations.jsonl per son (never truncated, see simulate.log_simulation), so nothing the ML
    ever proposed is lost from view -- only saved from disk, via _update_archive_cache's
    incremental tail-read.

    Same composition-signature dedupe (best ROI instance of a repeat wins) and blacklist-hiding
    as list_best_simulations, but deliberately NOT gated by an agent's own personality
    (_consider_best's liquidity_importance/risk_tolerance admission checks) -- those control
    what a son considers ITS OWN best finds, not what belongs in a complete record of everything
    proposed. Backs the dashboard's paginated "browse all opportunities" panel and the
    spreadsheet export; callers should paginate/limit the result themselves rather than render
    this whole (potentially large) list at once."""
    son_ids = [son_id] if son_id is not None else list(range(N_SONS))
    out = []
    for sid in son_ids:
        by_sig = _update_archive_cache(son_sims_file(sid))
        # Same hiding + override-repricing as list_best_simulations -- repriced_entry_copy
        # never mutates the cached entry, so removing an override later reverts automatically.
        out.extend({**user_overrides_mod.repriced_entry_copy(e), "son": sid}
                   for e in by_sig.values() if not _entry_hidden(e))
    if collection_filter:
        out = [e for e in out if collection_filter.lower() in (e["primary_collection"] or "").lower()]
    if min_roi is not None:
        out = [e for e in out if e["expected_roi_pct"] >= min_roi]
    # 6-son ensemble trust signal -- same attach as list_best_simulations (see that call site
    # and _attach_ensemble_predictions / ranking._ensemble_agreement_bonus).
    _attach_ensemble_predictions(out)
    ranked = ranking_mod.rank(out)
    seen: set = set()
    deduped = []
    for e in ranked:
        sig = sim_blacklist_mod.signature_of(e)
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(e)
    return deduped


def load_round_history(son_id: int, max_points: int = 500) -> list:
    """Reads this son's FULL round-by-round history straight from round_history.jsonl --
    unlike checkpoint.json's `history` field (capped at HISTORY_KEEP=300, see
    son_round_history_file's docstring), this file never trims, so it's the durable source for
    a long-running son's complete ROI/profit progress. Downsampled with a simple fixed stride to
    at most max_points entries (evenly spaced across the full run, always including the very
    last point) so a son with tens of thousands of rounds still returns a small, fast payload --
    this is for a lifetime trend chart, not a row-by-row audit (list_all_opportunities already
    covers the "every single one" need for opportunities specifically)."""
    path = son_round_history_file(son_id)
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn trailing line from a read racing a concurrent append
    if len(rows) <= max_points:
        return rows
    stride = len(rows) / max_points
    sampled = [rows[int(i * stride)] for i in range(max_points - 1)]
    sampled.append(rows[-1])
    return sampled


def most_profitable_collections(limit: int = 10) -> list:
    """Aggregates every son's CURATED best-known entries (list_best_simulations(), same source
    the dashboard's main opportunity table already uses -- no extra jsonl scanning) grouped by
    primary_collection. Ranked by average expected ROI % across every entry seen for that
    collection (not a single lucky outlier), with entry count and total expected profit
    (ranking.rank()'s own profit_amount = cost * roi/100) as supporting context."""
    entries = list_best_simulations()
    groups: dict = {}
    for e in entries:
        key = e["primary_collection"] or "(unknown)"
        g = groups.setdefault(key, {"collection": key, "count": 0, "total_roi_pct": 0.0,
                                     "best_roi_pct": float("-inf"), "total_expected_profit": 0.0})
        g["count"] += 1
        g["total_roi_pct"] += e["expected_roi_pct"]
        g["best_roi_pct"] = max(g["best_roi_pct"], e["expected_roi_pct"])
        g["total_expected_profit"] += e.get("profit_amount", 0.0)
    out = [{"collection": g["collection"], "count": g["count"],
            "avg_roi_pct": g["total_roi_pct"] / g["count"], "best_roi_pct": g["best_roi_pct"],
            "total_expected_profit": g["total_expected_profit"]} for g in groups.values()]
    out.sort(key=lambda g: g["avg_roi_pct"], reverse=True)
    return out[:limit]


def most_profitable_skins(limit: int = 10) -> list:
    """Aggregates the OUTPUT skins appearing across every son's curated best-known entries,
    weighting each appearance by its own landing probability (prob_pct) -- a skin that only
    lands 3% of the time in one mix contributes proportionally less than one that's the sole
    (100%) output of another. Ranked by total probability-weighted expected profit summed
    across every best-known entry it appears in, since that's the number that actually answers
    "which skin has been the most reliably profitable to land," not just "which skin shows up
    most often" or "which skin has the single biggest net_profit on paper."""
    entries = list_best_simulations()
    groups: dict = {}
    for e in entries:
        for o in e.get("output_distribution") or []:
            if o.get("net_profit") is None or not o.get("prob_pct"):
                continue
            key = (o["skin"], o["wear"])
            g = groups.setdefault(key, {"skin": o["skin"], "wear": o["wear"], "image": o.get("image", ""),
                                         "appearances": 0, "weighted_profit": 0.0})
            g["appearances"] += 1
            g["weighted_profit"] += o["net_profit"] * o["prob_pct"] / 100.0
    out = list(groups.values())
    out.sort(key=lambda g: g["weighted_profit"], reverse=True)
    return out[:limit]


def _sims_file_size(sims_file: Path) -> int:
    """Byte size of this son's training log, 0 if it doesn't exist yet. Never raises -- an
    integrity CHECK must not be the thing that fails a round."""
    try:
        return sims_file.stat().st_size
    except OSError:
        return 0


class TrainingLogStalled(RuntimeError):
    """This son logged valid simulations but its simulations.jsonl didn't grow -- see
    _verify_sims_log_grew. Its own class (rather than a bare RuntimeError) so the dashboard
    or a future consumer can tell this apart from an ordinary round failure."""


def _verify_sims_log_grew(state: "SimulationLoopState", size_before: int, n_logged: int) -> None:
    """Confirms this round's log_simulation() calls actually reached disk, by the only measure
    that can't be faked from inside the process: the file is bigger than it was at the top of
    the round. Raises TrainingLogStalled if it isn't.

    Deliberately a size comparison rather than a write-succeeded flag. log_simulation() opening
    the file "a" and returning without raising already "succeeded" in every run where this went
    wrong -- the rows stopped being there for reasons OUTSIDE this process (see run_round's own
    comment). What that history shows is that the loop cannot trust its own bookkeeping about a
    file other things can also touch; it has to re-read the file. Anything that rolls the log
    back -- a data_archive.import_archive(merge=False) restore, a directory copied over the
    top, an editor saving an old buffer -- now shows up on the NEXT round instead of being
    discovered days later from an unexplained flat best-ROI curve. (data_archive's export
    deliberately excludes simulations.jsonl, since split_archive.py owns it, so a plain
    export/import round-trip really can drop the live file back to whatever the committed
    chunks last carried.)

    Called only AFTER the round's checkpoint has been saved, so raising costs none of the
    round's real work -- the simulations themselves were genuine and the best-known list,
    coverage and round history they produced are all still valid and already persisted. What
    raising does buy is sim_loop()'s existing failure handling: status="error" plus the message
    on the dashboard (a plain state.error assignment would be wiped by the `state.error = None`
    that follows every clean run_round return), and a retry after the normal cooldown, so the
    son self-heals the moment the log starts growing again. Retraining is skipped meanwhile,
    which is the point -- refitting a log that hasn't changed is the wasted work this whole
    check exists to stop."""
    if n_logged <= 0:
        return
    size_after = _sims_file_size(state.sims_file)
    if size_after > size_before:
        return
    raise TrainingLogStalled(
        f"training log did not grow: logged {n_logged} valid simulation(s) to "
        f"{state.sims_file.name} but it went {size_before} -> {size_after} bytes. Retraining is "
        f"paused for this son because the model would only refit identical rows. Something "
        f"outside the sim loop is rewriting or reverting this file -- check for a data "
        f"restore/copy over data/sons/, then confirm the file is growing again.")


def _effective_temperature(base_temperature: float, cp: dict) -> float:
    boosted = cp["nudge_rounds_remaining"] > 0
    return base_temperature * (NUDGE_KICK_FACTOR if boosted else 1.0)


class SimulationLoopState:
    """One son. Thread-control wrapper (pause/stop/wake events, checkpoint lock), scoped
    to this son's own directory/temperature/RNG."""

    def __init__(self, son_id: int):
        self.son_id = son_id
        self.profile = agents_mod.profile_for(son_id)
        self.base_temperature = agents_mod.base_temperature(self.profile)
        self.sims_file = son_sims_file(son_id)
        self.model_dir = son_model_dir(son_id)
        self.invalid_file = son_invalid_file(son_id)
        self.checkpoint_file = son_checkpoint_file(son_id)
        self.round_history_file = son_round_history_file(son_id)
        self.lock = threading.Lock()
        self.status = "idle"  # idle | running | paused | stopped
        self.progress = {"current": 0, "total": 0}
        self.checkpoint = load_checkpoint(self.checkpoint_file)
        self.rng = random.Random()
        self.error = None  # last run_round() exception message, if the most recent attempt
        # failed (see sim_loop()) -- cleared on the next successful round. Surfaced on the
        # dashboard via snapshot() so a failure is visible instead of silently stalling.
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

    def abort_requested(self) -> bool:
        return self.stop_event.is_set() or self.pause_event.is_set()

    def snapshot(self) -> dict:
        with self.lock:
            cp = self.checkpoint
            # "profile" reports the LIVE self-tuned values for the five adaptive search params
            # (falling back to the static base profile for a son that hasn't run a round yet)
            # and the static values for the three deliberately-fixed ones -- so the dashboard's
            # radar chart/parameter displays show what each agent is ACTUALLY doing right now,
            # drifting in real time as adaptations are adopted. See adaptive.py.
            adaptive = cp.get("adaptive")
            if adaptive:
                live = adaptive_mod.live_params(adaptive)
            else:
                live = {p: getattr(self.profile, p) for p in adaptive_mod.ADAPTIVE_PARAMS}
            live_temperature = agents_mod.temperature_for(live["memory_weight"])
            return {
                "son_id": self.son_id,
                "agent_name": self.profile.name,
                "agent_purpose": self.profile.purpose,
                "profile": {
                    "exploration_rate": live["exploration_rate"],
                    "risk_tolerance": self.profile.risk_tolerance,
                    "collection_mixing_bias": live["collection_mixing_bias"],
                    "wear_optimization_bias": live["wear_optimization_bias"],
                    "liquidity_importance": self.profile.liquidity_importance,
                    "roi_weight": self.profile.roi_weight,
                    "mutation_strength": live["mutation_strength"],
                    "memory_weight": live["memory_weight"],
                },
                "adaptive": adaptive_mod.summary(adaptive, self.profile) if adaptive else None,
                "is_meta_agent": self.son_id in agents_mod.META_AGENT_IDS,
                "coverage_collections": len(cp.get("coverage") or {}),
                "temperature": live_temperature,
                "effective_temperature": _effective_temperature(live_temperature, cp),
                "status": self.status,
                "error": self.error,
                "progress": dict(self.progress),
                "round": cp["round"],
                "n_simulated": cp["n_simulated"],
                "n_invalid": cp["n_invalid"],
                # n_low_volume is a SUBSET of n_simulated (priced fine, just thin-liquidity) --
                # n_trainable = n_simulated - n_low_volume is what sim_learning.train() actually
                # fits on. cp.get(...) defaults handle checkpoints saved before this field existed.
                "n_low_volume": cp.get("n_low_volume", 0),
                "n_trainable": cp["n_simulated"] - cp.get("n_low_volume", 0),
                "best_roi_ever": cp["best_roi_ever"],
                "rounds_since_improvement": cp["rounds_since_improvement"],
                "stuck": cp["rounds_since_improvement"] >= STUCK_THRESHOLD_ROUNDS,
                "nudge_rounds_remaining": cp["nudge_rounds_remaining"],
                "cumulative_cost": cp["cumulative_cost"],
                "cumulative_expected_profit": cp["cumulative_expected_profit"],
                "history": cp["history"][-60:],  # cap payload size for the dashboard sparkline
                "updated_at": cp["updated_at"],
                "sim_model_status": sim_learning_mod.get_model_status(self.model_dir),
            }


def create_population() -> list:
    return [SimulationLoopState(son_id) for son_id in range(N_SONS)]


# --- Anti-convergence: prevent all agents from settling on the same strategy ----------

CONVERGENCE_MIN_AGENTS = 3          # spec: "if 3+ agents converge on the same strategies"
CONVERGENCE_CHECKS_REQUIRED = 3     # consecutive watch_loop checks before actually nudging --
# avoids reacting to a one-off coincidence (agents can share a region for a check or two by
# chance even when genuinely still exploring independently)
CONVERGENCE_CHECK_INTERVAL_SECONDS = 60


def _convergence_signature(entry: dict) -> tuple:
    """A coarse fingerprint of a checkpoint best-entry's SEARCH REGION -- primary
    collection, rarity pair, and the primary collection's rounded share of total inputs
    (nearest 0.2) -- deliberately NOT the exact skin/wear mix, since mutation.py
    constantly varies that even within one agent's own repeated visits to the same
    underlying strategy. Two entries with the same signature represent "the same idea,"
    which is what the spec's anti-convergence system cares about."""
    primary = entry.get("primary_collection", "")
    from_r, to_r = entry.get("from_rarity", ""), entry.get("to_rarity", "")
    input_mix = entry.get("input_mix") or []
    total = sum(u.get("count", 0) for u in input_mix) or 1
    primary_units = sum(u.get("count", 0) for u in input_mix if u.get("collection") == primary)
    ratio_bucket = round((primary_units / total) * 5) / 5
    return primary, from_r, to_r, ratio_bucket


def detect_convergence(sons: list) -> dict:
    """Looks at each son's CURRENT #1 best-known entry (its top strategy right now) and
    groups sons that share the same _convergence_signature. Returns only groups of 2+ --
    purely descriptive; ConvergenceWatcher below is what decides whether a group is big
    enough (CONVERGENCE_MIN_AGENTS) and persistent enough (CONVERGENCE_CHECKS_REQUIRED)
    to actually act on."""
    groups: dict = {}
    for s in sons:
        with s.lock:
            best = list(s.checkpoint["best"])
        if not best:
            continue
        sig = _convergence_signature(best[0])
        groups.setdefault(sig, []).append(s.son_id)
    return {sig: ids for sig, ids in groups.items() if len(ids) >= 2}


class ConvergenceWatcher:
    """Tracks how many CONSECUTIVE checks have found the same over-converged signature
    (CONVERGENCE_MIN_AGENTS+ sons sharing it), and once that streak reaches
    CONVERGENCE_CHECKS_REQUIRED, automatically nudge()s every son sharing it -- the exact
    same soft-retry temperature/exploration boost a user can already trigger by hand (see
    nudge() above), just triggered automatically instead of by a click. Nudging resets
    that signature's streak, giving the nudge a few rounds to actually diverge things
    before this could re-trigger on the same signature."""

    def __init__(self):
        self._streaks: dict = {}
        self.last_check_at = None
        self.last_nudged: list = []  # most recent first, each {"signature", "son_ids", "at"}

    def check(self, sons: list) -> dict:
        self.last_check_at = time.time()
        converged = detect_convergence(sons)
        nudged_this_check = []
        for sig in list(self._streaks):
            if sig not in converged or len(converged[sig]) < CONVERGENCE_MIN_AGENTS:
                del self._streaks[sig]  # no longer converged -- streak broken
        for sig, son_ids in converged.items():
            if len(son_ids) < CONVERGENCE_MIN_AGENTS:
                continue
            self._streaks[sig] = self._streaks.get(sig, 0) + 1
            if self._streaks[sig] >= CONVERGENCE_CHECKS_REQUIRED:
                for s in sons:
                    if s.son_id in son_ids:
                        nudge(s)
                nudged_this_check.append({"signature": list(sig), "son_ids": list(son_ids), "at": time.time()})
                self._streaks[sig] = 0
        if nudged_this_check:
            self.last_nudged = (nudged_this_check + self.last_nudged)[:20]
        return {"converged_groups": {str(k): v for k, v in converged.items()}, "nudged": nudged_this_check}

    def snapshot(self) -> dict:
        return {"last_check_at": self.last_check_at, "last_nudged": list(self.last_nudged[:5]),
                "active_streaks": {str(k): v for k, v in self._streaks.items()}}


def watch_convergence(sons: list, watcher: "ConvergenceWatcher", stop_event: threading.Event,
                       interval: float = CONVERGENCE_CHECK_INTERVAL_SECONDS) -> None:
    """Background loop: re-checks detect_convergence() every `interval` seconds until
    stop_event is set. Cheap -- just reads each son's already-in-memory checkpoint, no
    network/disk I/O beyond nudge()'s own checkpoint save when it actually fires."""
    while not stop_event.wait(timeout=interval):
        watcher.check(sons)


# --- Round-robin scheduler: "run all sons, one round each, in turn" --------------------

ROUND_ROBIN_POLL_SECONDS = 1.0  # comfortably below ROUND_COOLDOWN_SECONDS, so a finished son
# is handed off to the next one long before it would otherwise start a second round on its own


class RoundRobinScheduler:
    """Alternative to letting every son run continuously/concurrently: takes over each son's own
    pause_event and cycles through the population in order, giving each one exactly ONE round
    before pausing it and unpausing the next (wrapping back to son 0 after the last). All four
    still make progress "at the same time" from the user's point of view -- one Start button
    covers the whole population -- but never more than one son is actually mid-round, so they
    don't all hit the (rate-limited, see thirdparty_prices.py) price lookups at once. Individual
    per-son Start/Pause/Stop/Nudge/Full-reset (see server.py) are unaffected by this and can
    still be used mid-cycle; doing so just races with the scheduler's own pause/resume calls on
    that one son.
    """

    def __init__(self, sons: list):
        self.sons = sons
        self.enabled = False
        self.active_index = 0
        self._turn_start_round = 0
        self._lock = threading.Lock()

    def _activate(self, index: int) -> None:
        self.active_index = index
        son = self.sons[index]
        with son.lock:
            self._turn_start_round = son.checkpoint["round"]
            son.pause_event.clear()
        son.wake_event.set()

    def start(self) -> None:
        with self._lock:
            for son in self.sons:
                son.pause_event.set()
            self.enabled = True
            self._activate(0)

    def stop(self) -> None:
        with self._lock:
            self.enabled = False
            self.sons[self.active_index].pause_event.set()
            self.sons[self.active_index].wake_event.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled,
                    "active_son_id": self.sons[self.active_index].son_id if self.enabled else None}

    def tick(self) -> None:
        """Called periodically by watch_round_robin. Once the currently-active son has completed
        at least one full round since its turn started, hands off to the next son in order --
        also wakes it immediately (rather than waiting out its own ROUND_COOLDOWN_SECONDS idle
        timer) so the handoff is prompt. Also hands off immediately if the active son is
        currently in "error" status (see sim_loop()) -- a son whose round keeps failing would
        otherwise never satisfy round_now > _turn_start_round, stalling every other son in the
        population behind it forever."""
        with self._lock:
            if not self.enabled:
                return
            son = self.sons[self.active_index]
            with son.lock:
                round_now = son.checkpoint["round"]
                errored = son.status == "error"
            if round_now > self._turn_start_round or errored:
                son.pause_event.set()
                son.wake_event.set()
                self._activate((self.active_index + 1) % len(self.sons))


def watch_round_robin(scheduler: "RoundRobinScheduler", stop_event: threading.Event,
                       interval: float = ROUND_ROBIN_POLL_SECONDS) -> None:
    """Background loop: ticks the scheduler every `interval` seconds until stop_event is set --
    cheap, just an in-memory checkpoint-round comparison, no network/disk I/O of its own."""
    while not stop_event.wait(timeout=interval):
        scheduler.tick()


def nudge(state: SimulationLoopState) -> None:
    """Soft retry: reseeds this son's RNG and temporarily multiplies its temperature by
    NUDGE_KICK_FACTOR for the next NUDGE_DURATION_ROUNDS rounds (pushing it to explore more
    broadly), and gives it a fresh rounds_since_improvement window. Its logged simulations,
    trained model, and best-known list are all left completely untouched."""
    with state.lock:
        state.rng = random.Random()
        state.checkpoint["nudge_rounds_remaining"] = NUDGE_DURATION_ROUNDS
        state.checkpoint["rounds_since_improvement"] = 0
        save_checkpoint(state.checkpoint_file, state.checkpoint)


def full_reset(state: SimulationLoopState) -> None:
    """Hard retry: wipes this son's logged simulations, trained model, and checkpoint entirely,
    and reseeds its RNG -- a genuine from-scratch restart for this ONE lineage. Other sons are
    completely unaffected."""
    with state.lock:
        state.rng = random.Random()
        state.sims_file.parent.mkdir(parents=True, exist_ok=True)
        state.sims_file.write_text("", encoding="utf-8")
        if state.invalid_file.exists():
            state.invalid_file.write_text("", encoding="utf-8")
        model_file = state.model_dir / sim_learning_mod.MODEL_FILENAME
        meta_file = state.model_dir / sim_learning_mod.META_FILENAME
        model_file.unlink(missing_ok=True)
        meta_file.unlink(missing_ok=True)
        (state.model_dir / sim_learning_mod.HI_MODEL_FILENAME).unlink(missing_ok=True)  # auxiliary quantile head
        sim_learning_mod.forget(state.model_dir, state.sims_file)
        _archive_cache.pop(state.sims_file, None)  # drop this son's stale opportunity-archive
        # cache too -- _update_archive_cache would self-heal on the next call anyway (truncated
        # size < cached pos), but dropping it explicitly here matches sim_learning.forget's
        # same-reset "don't serve stale cached state" contract instead of relying on that.
        state.checkpoint = _empty_checkpoint()
        save_checkpoint(state.checkpoint_file, state.checkpoint)


def reset_all(population: list) -> None:
    """Hard retry for the WHOLE population at once -- runs full_reset() on every son, wiping
    every agent's logged simulations, trained model, and checkpoint back to a clean slate.
    Each son is still reset independently through the same per-son full_reset(), so this never
    merges or crosses any son's data with another's (see
    feedback_sim_data_must_never_touch_real_outcomes) -- it's just the "Restart all training &
    data" button doing what four individual Full-reset clicks would do."""
    for son in population:
        full_reset(son)


def run_round(state: SimulationLoopState, currency: int = 1) -> None:
    """Runs one batch of candidates for this son, then advances/retrains/updates its progress
    bookkeeping. Pausing/stopping mid-batch just stops early -- every candidate is drawn
    independently, so there's nothing to resume "partway through"; the next round just draws a
    fresh batch."""
    # Tags this son's own dedicated thread so every steam_prices call it makes from here on
    # (this round AND every later one -- the tag is thread-local and this thread never runs
    # another son's code) shows up broken out by son in steam_prices.get_stats()["by_label"] --
    # see steam_prices.set_attribution's docstring. Cheap (a plain attribute set); safe to call
    # every round.
    steam_prices_mod.set_attribution(f"son_{state.son_id}")

    skins = data_mod.load_skins()
    idx = data_mod.collections_index(skins)

    with state.lock:
        # Live self-tuned search params (adaptive.py) -- NOT the static profile values, which
        # are only this agent's starting personality. Initialized lazily here because
        # _empty_checkpoint is profile-agnostic. Temperature is derived from the LIVE
        # memory_weight, so the exploit-vs-explore dial itself adapts over time.
        adaptive = adaptive_mod.ensure_state(state.checkpoint, state.profile)
        live = adaptive_mod.live_params(adaptive)
        # novelty_multiplier/temperature_multiplier used to be layered on by the now-removed
        # Exploration Recovery Mode -- always 1.0 (no-op) now that it's gone.
        live["novelty_multiplier"] = 1.0
        live["temperature_multiplier"] = 1.0
        temperature = _effective_temperature(agents_mod.temperature_for(live["memory_weight"]),
                                             state.checkpoint) * live["temperature_multiplier"]
        # Copied so candidate scoring below can read it lock-free; the authoritative dict is
        # only ever mutated by this son's own thread (in the valid-record branch, under lock).
        coverage = dict(state.checkpoint["coverage"])

    model_status = sim_learning_mod.get_model_status(state.model_dir)
    # Computed ONCE per round (same reasoning as model_status above -- stable for this whole
    # round even though this son's own log_simulation() calls below will keep changing
    # sims_file's mtime as the round proceeds). This was a real, measured bug: leaving each
    # predict_expected_roi() call to re-derive its own lookup via sims_file's mtime meant every
    # candidate's log_simulation() invalidated the NEXT candidate's cache, turning one O(n) pass
    # per round into a full reload-and-refilter of the entire simulations.jsonl per candidate --
    # profiling one live round showed ~60 of ~76 seconds going here alone. See
    # sim_learning.collection_roi_lookup's docstring.
    collection_roi_lookup = (sim_learning_mod.collection_roi_lookup(state.sims_file)
                              if model_status["trained"] else {})
    # Selection score = model-predicted ROI + a UCB novelty bonus for collections this son has
    # rarely/never simulated (see adaptive.novelty_bonus), scaled by the live exploration_rate.
    # ROI stays the dominant term once a model exists, but an unexplored collection always
    # carries real selection weight -- a powerful trade-up hiding where the model has no data
    # can't be starved out of the simulate() budget forever. Before a model is trained, the
    # novelty bonus alone scores the batch (softmax-selected), so even the very first rounds
    # spread across collections instead of blindly taking the first SIMULATE_PER_ROUND draws.
    coverage_total = sum(coverage.values())
    exploration = live["exploration_rate"]
    novelty_multiplier = live["novelty_multiplier"]  # >1.0 only in Recovery Mode
    novelty_ucb_pp = adaptive_mod.NOVELTY_UCB_PP

    def _novelty(c):
        return (exploration * novelty_multiplier
                * adaptive_mod.novelty_bonus(c, coverage, coverage_total, ucb_pp=novelty_ucb_pp))

    if model_status["trained"]:
        acq_beta = agents_mod.acquisition_beta_for(live["exploration_rate"])
        def score_fn(c):
            pred = sim_learning_mod.predict_expected_roi(c, state.model_dir, state.sims_file,
                                                          collection_roi_lookup=collection_roi_lookup,
                                                          with_std=True)
            mean, std = pred if pred is not None else (0.0, 0.0)
            return (mean + _novelty(c), std)
    else:
        acq_beta = 0.0
        score_fn = _novelty
    keep_top_k = SIMULATE_PER_ROUND
    # exploration_rate scales how large a pool this agent casts before narrowing down to
    # SIMULATE_PER_ROUND -- the "adaptive search budget" stand-in for the spec's literal
    # Monte-Carlo trial-count idea, which doesn't apply here (see rules_engine.py/module
    # docstrings: this codebase computes exact expected value, never samples an outcome).
    # keep_top_k/the actual simulate() budget below is UNCHANGED by this -- only the cheap,
    # cache-free candidate-construction pool grows for more exploratory agents.
    batch_size = max(50, round(BATCH_SIZE * (0.5 + exploration)))
    # Skins with no obtainable price on Steam or any third-party site (invalid_skins.py, marked
    # by simulate.py) are excluded from candidate construction entirely, so this son never wastes
    # a round proposing -- or a simulate() call pricing -- a trade-up it already knows can't be
    # valued. Loaded once per round (a plain in-memory set); entries past their recheck window
    # drop out on their own so a relisted skin gets retried. Currency-scoped to match.
    invalid_names = invalid_skins_mod.active_invalid_names(currency)
    # Also exclude skins that CAN'T legally be trade-up inputs at all -- souvenir-only skins
    # with no non-souvenir copy on any market (see tradability.py). Unlike invalid_skins'
    # learned-one-miss-at-a-time registry, this is known up front from the bulk price feeds
    # (zero live requests, cached until the feed refreshes), so no round is ever wasted
    # proposing a contract the game itself would refuse.
    from . import tradability as tradability_mod
    invalid_names = invalid_names | tradability_mod.untradeable_market_names(currency)
    # And the user's PERMANENT skin blacklist (user_overrides.py) -- cheap cached expansion to
    # market_hash_names. simulate.simulate() hard-gates these anyway (mutation/evolution can
    # reintroduce a skin the generator excluded), this just stops the budget being wasted.
    invalid_names = invalid_names | user_overrides_mod.blacklisted_market_names()
    # Everything currently warm in steam_prices' cache (a plain in-memory dict scan, no network
    # -- iter_cached_entries() is the same helper dead_skin.py's weekly review already reuses),
    # scoped to this currency -- biases generate_batch's candidate construction toward skin/wear
    # combos already known to be priceable instead of ones that would need a fresh live Steam
    # fetch to even find out (see random_trade.WARM_CACHE_BIAS). Loaded once per round, same
    # cadence as invalid_names above.
    warm_names = {name for name, cur, _data, _fetched_at in steam_prices_mod.iter_cached_entries()
                  if cur == currency}
    candidates = random_trade_mod.generate_batch(
        idx, batch_size, rng=state.rng, score_fn=score_fn, keep_top_k=keep_top_k, temperature=temperature,
        filler_count_decay=agents_mod.filler_decay_for(live["collection_mixing_bias"]),
        wear_bias=live["wear_optimization_bias"], invalid_names=invalid_names, warm_names=warm_names,
        acquisition_beta=acq_beta)

    # mutation_strength-controlled share of this round's batch comes from mutating this
    # agent's OWN current best finds (mutation.py) instead of fresh random draws -- the
    # "improve existing successful trade-ups" half of the search. Only kicks in once the
    # agent actually has a best-known list to mutate from; total candidate count (and so
    # the simulate() budget below) is unchanged either way, only the MIX shifts.
    with state.lock:
        mutation_pool = list(state.checkpoint["best"])
    effective_mutation = live["mutation_strength"]
    n_mutated = round(len(candidates) * effective_mutation) if mutation_pool else 0
    if n_mutated:
        mutated = mutation_mod.mutate_batch(mutation_pool, idx, state.rng, n_mutated)
        if mutated:
            candidates = mutated + candidates[len(mutated):]

    with state.lock:
        state.status = "running"
        state.progress = {"current": 0, "total": len(candidates)}

    # Per-round aggregates over every VALID simulated candidate this round (not just the ones
    # that made the curated best-known list) -- feeds the dashboard's "stats over rounds" chart
    # (avg_input_float/avg_roi_pct/avg_confidence) alongside the existing best-ROI-so-far line.
    round_floats, round_rois, round_confidences = [], [], []
    # Training-log integrity: this son's whole reason to exist is that every valid record it
    # produces lands in simulations.jsonl, which is the ONLY thing sim_learning.train() ever
    # reads. Nothing used to check that it actually did, and the failure is completely silent
    # from every angle the dashboard shows: rounds still advance, n_simulated still climbs,
    # best-known finds still update, retrains still report "trained" -- on a frozen file.
    #
    # This is not hypothetical. Across rounds 135-725 of a real ~36h run this son's n_simulated
    # went 5,386 -> 28,953 while simulations.jsonl kept its last row (and its mtime) from nine
    # days earlier, so all 33 retrains in that window refit the identical rows and the search
    # had no way to learn anything from 23,567 simulations. See _verify_sims_log_grew.
    sims_size_before = _sims_file_size(state.sims_file)
    n_logged = 0

    for i, candidate in enumerate(candidates, 1):
        if state.stop_event.is_set():
            with state.lock:
                state.status = "stopped"
            return
        if state.pause_event.is_set():
            with state.lock:
                state.status = "paused"
            return
        record = simulate_mod.simulate(candidate, currency=currency, abort_check=state.abort_requested)
        if isinstance(record, simulate_mod.SimulationRecord):
            record.agent = state.son_id  # simulate.py itself is agent-agnostic (reused by every
            # son) -- this is the one place that actually knows which agent ran it.
            if model_status["trained"]:
                # Same model_status snapshot used to build score_fn above (stable for this whole
                # round, even if a retrain happens after it -- see the retrain trigger below) --
                # records this son's own pre-simulation guess for the exact candidate that was
                # just simulated, so ranking.py can later compare it against the real
                # expected_roi_pct as an agreement-based trust signal (see ranking.
                # _ml_agreement_bonus). Cheap: reuses the already-loaded model, no extra I/O.
                record.ml_predicted_roi_pct = sim_learning_mod.predict_expected_roi(
                    candidate, state.model_dir, state.sims_file,
                    collection_roi_lookup=collection_roi_lookup)
            simulate_mod.log_simulation(record, state.sims_file)
            n_logged += 1
            round_floats.append(record.avg_input_float)
            round_rois.append(record.expected_roi_pct)
            round_confidences.append(record.confidence_score or 0.0)
            with state.lock:
                cp = state.checkpoint
                cp["n_simulated"] += 1
                # Priced successfully but thin-volume (see dead_skin.py) -- sim_learning.train()
                # excludes these from the training set entirely (its own `rows` filter), so
                # n_simulated alone overstates how much of this son's data is actually feeding
                # its model. Tracked separately so the dashboard can show trainable vs excluded
                # instead of conflating "simulated" with "trainable".
                if record.low_volume_warning:
                    cp["n_low_volume"] = cp.get("n_low_volume", 0) + 1
                # "If you'd bought literally every trade this bot ever proposed" -- see
                # _empty_checkpoint's docstring. Every valid record counts here regardless of
                # whether it was good enough for the curated best-known list.
                cp["cumulative_cost"] += record.cost
                cp["cumulative_expected_profit"] += record.expected_profit
                # Self-tuning bookkeeping: fold this sim into the collection-coverage counts
                # (drives the UCB novelty bonus above) and the current measurement window
                # (drives end_round()'s adopt/revert reward -- see adaptive.py). "novel" =
                # its primary collection was still under-explored BEFORE this sim.
                novel = adaptive_mod.update_coverage(cp["coverage"], record)
                adaptive_mod.note_valid(cp["adaptive"], record.expected_roi_pct,
                                        record.low_volume_warning, novel)
                _consider_best(cp, record, state.profile)
                # Auto-verify queue (see verify_queue.py): a cheap, non-blocking, in-memory
                # enqueue only -- the actual live third-party call happens on a SEPARATE
                # consumer thread, never inline in this loop.
                admitted = next((e for e in cp["best"] if e["id"] == record.id), None)
                if admitted is not None:
                    verify_queue_mod.maybe_enqueue(state.son_id, admitted)
        else:  # InvalidSimulation -- not every input/output priced; logged separately, never
            # trained on or ranked (see simulate.py's own docstring for why)
            simulate_mod.log_invalid(record, state.invalid_file)
            with state.lock:
                state.checkpoint["n_invalid"] += 1
                adaptive_mod.note_invalid(state.checkpoint["adaptive"])
        with state.lock:
            state.progress = {"current": i, "total": len(candidates)}

    with state.lock:
        cp = state.checkpoint
        cp["round"] += 1
        current_best = cp["best"][0]["expected_roi_pct"] if cp["best"] else None
        if current_best is not None and (cp["best_roi_ever"] is None
                                          or current_best > cp["best_roi_ever"] + IMPROVEMENT_EPSILON_PP):
            cp["best_roi_ever"] = current_best
            cp["rounds_since_improvement"] = 0
        else:
            cp["rounds_since_improvement"] += 1
        cp["nudge_rounds_remaining"] = max(0, cp["nudge_rounds_remaining"] - 1)
        # One step of the self-tuning state machine (adaptive.py): advances the measurement
        # window and, every ADAPT_WINDOW_ROUNDS rounds, either starts a trial tweak of this
        # son's search params or adopts/reverts the one just measured. Persisted with the
        # checkpoint save below, same as every other per-round counter.
        adaptive_mod.end_round(cp["adaptive"], state.profile, state.rng)
        history_entry = {
            "round": cp["round"], "n_simulated": cp["n_simulated"],
            "best_roi_ever": cp["best_roi_ever"],
            # This round's averages across every valid candidate simulated this round (None if
            # the whole round came up empty/invalid) -- see round_floats/round_rois/
            # round_confidences above.
            "avg_input_float": (sum(round_floats) / len(round_floats)) if round_floats else None,
            "avg_roi_pct": (sum(round_rois) / len(round_rois)) if round_rois else None,
            "avg_confidence": (sum(round_confidences) / len(round_confidences)) if round_confidences else None,
            "cumulative_cost": cp["cumulative_cost"],
            "cumulative_expected_profit": cp["cumulative_expected_profit"],
            "at": time.time(),
        }
        cp["history"].append(history_entry)
        del cp["history"][:-HISTORY_KEEP]
        save_checkpoint(state.checkpoint_file, cp)
        try:
            state.round_history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(state.round_history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(history_entry) + "\n")
        except OSError:
            pass  # best-effort durable log -- never let a disk hiccup here fail the round itself
        state.status = "idle"
        n_simulated = cp["n_simulated"]
        n_at_last_train = cp["n_simulated_at_last_train"]

    # Checked after the checkpoint save so a stalled log can never cost this son the round's
    # real work, and before the retrain trigger so it can skip it -- see _verify_sims_log_grew.
    _verify_sims_log_grew(state, sims_size_before, n_logged)

    if (n_simulated - n_at_last_train >= sim_learning_mod.RETRAIN_TRIGGER_DELTA
            and n_simulated >= sim_learning_mod.MIN_TRAINING_SAMPLES):
        result = sim_learning_mod.train(state.model_dir, state.sims_file)
        # Only advance the "since last train" counter on an actual successful fit. train()
        # refuses below MIN_TRAINING_SAMPLES *volume-confirmed* rows (low_volume_warning ones
        # are excluded -- see its docstring), which is a materially smaller count than
        # n_simulated for a son hitting a lot of thin-liquidity skins. Advancing this counter
        # unconditionally (the old behavior) meant a refused attempt bought itself another full
        # RETRAIN_TRIGGER_DELTA (25) simulations before trying again -- if the volume-confirmed
        # share of new rows stays low, that compounds into a long, misleading "stuck at
        # untrained" stretch even though plenty of raw candidates were being simulated. Retrying
        # every round on failure instead lets the model train the moment the cumulative
        # volume-confirmed count actually crosses the threshold.
        if result.trained:
            with state.lock:
                state.checkpoint["n_simulated_at_last_train"] = n_simulated
                save_checkpoint(state.checkpoint_file, state.checkpoint)


def sim_loop(state: SimulationLoopState, max_rounds: int | None = None, currency: int = 1) -> None:
    """Runs rounds back-to-back for one son, with the same pause/stop/resume handling every
    other background loop in this app uses."""
    rounds_done = 0
    while True:
        if state.stop_event.is_set():
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
        if state.pause_event.is_set():
            with state.lock:
                state.status = "paused"
            state.wake_event.wait()
            state.wake_event.clear()
            with state.lock:
                state.pause_event.clear()
            continue

        try:
            run_round(state, currency=currency)
            with state.lock:
                state.error = None
        except Exception as e:
            # Never let an exception from a single bad round (e.g. a transient OSError from a
            # locked file, see invalid_skins.py/dead_skin.py) silently kill this daemon thread --
            # that already happened once before (a missing sklearn install) with nothing visible
            # on the dashboard. Surface it via state.error/status="error" instead, then retry
            # after the normal cooldown -- self-heals if the failure was transient.
            with state.lock:
                state.status = "error"
                state.error = f"{type(e).__name__}: {e}"
            state.wake_event.wait(timeout=ROUND_COOLDOWN_SECONDS)
            state.wake_event.clear()
            continue
        rounds_done += 1

        with state.lock:
            status_after = state.status
        if status_after == "paused":
            state.wake_event.wait()
            state.wake_event.clear()
            with state.lock:
                state.pause_event.clear()
            continue
        if status_after == "stopped" or state.stop_event.is_set():
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
        if max_rounds is not None and rounds_done >= max_rounds:
            with state.lock:
                state.status = "stopped"
            return
        state.wake_event.wait(timeout=ROUND_COOLDOWN_SECONDS)
        state.wake_event.clear()


def run_foreground(max_rounds: int | None = None, currency: int = 1) -> None:
    """CLI entry point (`python -m src.main simulate-population`) -- runs all N_SONS sons as
    background threads in this one process, plus a lightweight anti-convergence watcher
    thread (see ConvergenceWatcher/watch_convergence above); Ctrl+C stops all of them."""
    sons = create_population()
    threads = [threading.Thread(target=sim_loop, args=(s,),
                                 kwargs={"max_rounds": max_rounds, "currency": currency}, daemon=True)
               for s in sons]
    convergence_watcher = ConvergenceWatcher()
    convergence_stop = threading.Event()
    watcher_thread = threading.Thread(target=watch_convergence, args=(sons, convergence_watcher, convergence_stop),
                                       daemon=True)
    print(f"Starting a population of {len(sons)} guided-simulation sons "
          f"(temperatures {SON_TEMPERATURES}) -- checkpoints under {SONS_DIR}\n"
          f"Ctrl+C to stop all of them (progress is saved after every round).\n")
    for t in threads:
        t.start()
    watcher_thread.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        for s in sons:
            s.stop_event.set()
            s.wake_event.set()
        for t in threads:
            t.join(timeout=5)
        convergence_stop.set()
        print("\nStopped -- checkpoints saved.")
        return
    convergence_stop.set()
    for s in sons:
        snap = s.snapshot()
        print(f"Son {s.son_id} ({snap['agent_name']}, temp {s.base_temperature}): "
              f"{snap['n_simulated']} simulated, best ROI ever {snap['best_roi_ever']}")
