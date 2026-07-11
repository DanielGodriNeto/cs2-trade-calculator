"""Evolution Manager (Evolution Engine 2.0): cross-agent coordination layered ON TOP of the
existing multi-agent population -- the specialists themselves (agents.py's profiles, their
adaptive.py self-tuning, their reward functions, their per-son storage) are never modified.

What the existing system already does per-agent: each son mutates ITS OWN best finds
(mutation.py via sim_loop.run_round), self-tunes ITS OWN search params (adaptive.py), and gets
nudged when the population over-converges (sim_loop.ConvergenceWatcher). What no existing
module does -- and what this one adds -- is COOPERATION between agents:

  - Elite Population: the cross-agent top genomes (checkpoint best-entry dicts, the project's
    genome representation), deduped by composition signature, fitness-scored by ranking.py's
    composite (never raw ROI alone -- same "beat fake high ROI" pressure as the final ranking).
  - Parent Selection: tournament selection over the elite, novelty-biased (see below), used to
    pick which elite genomes get injected into a son's mutation pool each round.
  - Species Management: elites are grouped into species by the same coarse strategy signature
    anti-convergence already uses (collection + rarity path); per-species stats drive...
  - Adaptive Mutation: a per-species mutation multiplier that rises while a species stagnates
    and relaxes when it improves, applied to the injecting son's mutation share.
  - Hall of Fame: the all-time best genomes ever seen, never displaced by mere staleness --
    only by strictly fitter genomes -- kept even after live prices invalidate them (audit/
    inspiration record, clearly separated from the live elite).
  - Novelty Search: an archive counting how often each strategy signature has ever been seen;
    rarer signatures earn a novelty bonus in parent selection, so the manager actively feeds
    under-explored strategies back into circulation instead of just amplifying the winners.
  - Genome Lineage: every elite admission and every mutation-child that traces back to an
    injected parent is appended to data/evolution/lineage.jsonl (append-only audit trail).
  - Cross-Agent Knowledge Sharing: parents_for() prefers OTHER agents' genomes, so each son's
    mutation operators (unchanged) now also improve on its siblings' discoveries.
  - Meta-Agent Creation: builds/refreshes the two second-generation agents' inherited
    personalities via behavioral crossover from the best-performing specialists (see
    agents.py's META_AGENT_IDS block; refresh preserves each meta agent's own accumulated
    experience -- only the base personality moves).
  - Hyperparameter Evolution: population-based training over adaptive.py's evolvable
    hyperparams (see adaptive.HYPERPARAM_BOUNDS) -- periodically copies a mutated version of
    the best-recent-reward son's hyperparams onto the worst's, so the population evolves its
    search BEHAVIOR, not just its trade-up genomes.

Excluded trade-ups (market_reliability.py) are barred at every door, per spec: _consider_best
already refuses them into any best list, ingest re-refuses them into the elite, and a periodic
recheck evicts elites whose skins later turn Excluded (they re-enter naturally once healthy --
nothing here is a permanent blacklist, same guarantee as everywhere else in this project).

The manager runs as one background watcher thread (watch(), started at server boot alongside
watch_convergence) and can be enabled/disabled at any time via set_enabled() -- disabled, every
hook (parents_for, mutation_boost, the tick itself) returns neutral values and the rest of the
application behaves exactly as if this module didn't exist.
"""
import json
import threading
import time
from pathlib import Path

from . import adaptive as adaptive_mod
from . import agents as agents_mod
from . import market_reliability as market_reliability_mod
from . import ranking as ranking_mod
from . import sim_blacklist as sim_blacklist_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "evolution"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
LINEAGE_FILE = DATA_DIR / "lineage.jsonl"
META_PROFILE_FILE = agents_mod.META_PROFILE_FILE  # single source of truth for the path

ELITE_SIZE = 60            # cross-agent elite genomes kept (deduped by signature)
HOF_SIZE = 25              # hall-of-fame entries (never pruned for staleness)
TOURNAMENT_K = 3           # parent-selection tournament size
NOVELTY_SHARE = 0.25       # fraction of injected parents chosen by novelty instead of fitness
INJECT_MAX = 6             # max elite parents injected into one son's mutation pool per round

TICK_SECONDS = 120                 # manager wake-up cadence (cheap in-memory work per tick)
ELITE_RECHECK_SECONDS = 30 * 60    # evict elites whose skins turned Excluded, this often
META_REFRESH_SECONDS = 6 * 3600    # behavioral-crossover refresh cadence for the meta agents
PBT_INTERVAL_SECONDS = 30 * 60     # hyperparameter-evolution step cadence
PBT_MUTATE_FRAC = 0.20             # each copied hyperparam is jittered +/- this fraction
MIN_REWARD_POINTS = 5              # reward_history points needed before a son joins PBT

STAGNANT_SPECIES_SECONDS = 45 * 60   # species with no fitness improvement for this long ...
MUTATION_BOOST_MAX = 1.6             # ... ramp toward this mutation multiplier
LINEAGE_LOG_KEPT = 2000

_lock = threading.Lock()          # guards _state/_config in-process
_io_lock = threading.Lock()       # guards file writes
_state_cache: dict | None = None
_config_cache: dict | None = None


# --- Config (enable/disable) --------------------------------------------------------------

def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        if CONFIG_FILE.exists():
            try:
                _config_cache = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _config_cache = {}
        else:
            _config_cache = {}
        _config_cache.setdefault("enabled", True)
    return _config_cache


def enabled() -> bool:
    with _lock:
        return bool(_load_config()["enabled"])


def set_enabled(value: bool) -> None:
    with _lock:
        cfg = _load_config()
        cfg["enabled"] = bool(value)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _io_lock:
            CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")


# --- Persistent state ---------------------------------------------------------------------

def _empty_state() -> dict:
    return {"elite": [],                 # [{genome_id, entry, son_id, fitness, signature, species,
                                          #   generation, added_at}]
            "hall_of_fame": [],          # same shape, all-time, never staleness-pruned
            "species": {},               # key -> {best_fitness, last_improved_at, n_members,
                                          #          mutation_multiplier}
            "novelty_archive": {},       # signature -> times ever seen
            "generation": 0,
            "injections": 0,             # cross-agent parents handed out (cheap counter)
            "last_tick_at": None,
            "last_elite_recheck_at": None,
            "last_meta_refresh_at": None,
            "last_pbt_at": None,
            "pbt_events": [],            # newest first, capped
            "meta_events": []}           # newest first, capped


def _load_state() -> dict:
    global _state_cache
    if _state_cache is None:
        if STATE_FILE.exists():
            try:
                _state_cache = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _state_cache = _empty_state()
        else:
            _state_cache = _empty_state()
        for k, v in _empty_state().items():
            _state_cache.setdefault(k, v)
    return _state_cache


def _save_state_locked(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _io_lock:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _append_lineage(event: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _io_lock:
        with open(LINEAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")


def list_lineage(limit: int = LINEAGE_LOG_KEPT) -> list[dict]:
    """Most recent lineage events first -- same skip-corrupt-lines convention as every other
    JSONL log in this project."""
    if not LINEAGE_FILE.exists():
        return []
    out = []
    with open(LINEAGE_FILE, encoding="utf-8") as f:
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


# --- Elite population / species / novelty / hall of fame ----------------------------------

def _species_key(entry: dict) -> str:
    return f"{entry.get('primary_collection', '')}|{entry.get('from_rarity', '')}|{entry.get('to_rarity', '')}"


def _entry_excluded(entry: dict) -> bool:
    return market_reliability_mod.any_unit_excluded(
        entry.get("input_mix"), entry.get("output_distribution"), currency=1,
        stattrak=entry.get("stattrak", False))


def _admit(state: dict, entry: dict, son_id: int, fitness: float, now: float) -> bool:
    """Admits one best-entry genome into the elite population (dedup by composition signature,
    keep the fitter instance), updates its species and the novelty archive, and logs the
    admission's lineage. Returns True if the elite actually changed."""
    sig = sim_blacklist_mod.signature_of(entry)
    species = _species_key(entry)
    existing = next((e for e in state["elite"] if e["signature"] == sig), None)
    if existing is not None and existing["fitness"] >= fitness:
        return False
    if existing is not None:
        state["elite"].remove(existing)
    genome = {"genome_id": entry.get("id"), "entry": entry, "son_id": son_id,
              "fitness": fitness, "signature": sig, "species": species,
              "generation": state["generation"], "added_at": now}
    state["elite"].append(genome)
    state["elite"].sort(key=lambda g: g["fitness"], reverse=True)
    del state["elite"][ELITE_SIZE:]

    state["novelty_archive"][sig] = state["novelty_archive"].get(sig, 0) + 1

    sp = state["species"].setdefault(species, {"best_fitness": None, "last_improved_at": now,
                                                "n_members": 0, "mutation_multiplier": 1.0})
    if sp["best_fitness"] is None or fitness > sp["best_fitness"]:
        sp["best_fitness"] = fitness
        sp["last_improved_at"] = now

    _append_lineage({"event": "elite_admitted", "genome_id": entry.get("id"), "signature": sig,
                     "species": species, "son_id": son_id, "fitness": round(fitness, 3),
                     "roi_pct": entry.get("expected_roi_pct"),
                     "generation": state["generation"], "at": now})
    return True


def ingest_population(sons: list) -> int:
    """Folds every son's CURRENT best list into the elite population. Fitness is ranking.py's
    composite score (the same weighted 60/15/10/10/5 + reliability dampener the final
    cross-agent ranking uses), so the elite can never be captured by fake high raw ROI.
    Excluded trade-ups are refused outright, per spec. Returns how many genomes changed."""
    now = time.time()
    candidates = []
    for son in sons:
        with son.lock:
            best = [dict(e) for e in son.checkpoint.get("best") or []]
        for e in best:
            e["son"] = son.son_id
            candidates.append((son.son_id, e))
    if not candidates:
        return 0
    ranked = ranking_mod.rank([e for _, e in candidates])
    by_id = {e.get("id"): e for e in ranked}
    changed = 0
    with _lock:
        state = _load_state()
        for son_id, entry in candidates:
            scored = by_id.get(entry.get("id"))
            fitness = scored.get("composite_score") if scored else None
            if fitness is None:
                continue
            if _entry_excluded(entry):
                continue
            if _admit(state, entry, son_id, float(fitness), now):
                changed += 1
        # Species member counts + adaptive mutation multipliers (see module docstring).
        members: dict = {}
        for g in state["elite"]:
            members[g["species"]] = members.get(g["species"], 0) + 1
        for key, sp in list(state["species"].items()):
            sp["n_members"] = members.get(key, 0)
            stagnant_for = now - (sp.get("last_improved_at") or now)
            ramp = min(1.0, stagnant_for / STAGNANT_SPECIES_SECONDS)
            sp["mutation_multiplier"] = round(1.0 + (MUTATION_BOOST_MAX - 1.0) * ramp, 3)
            if sp["n_members"] == 0 and stagnant_for > 4 * STAGNANT_SPECIES_SECONDS:
                del state["species"][key]  # long-empty species record -- bookkeeping only
        # Hall of Fame: strictly-fitter-only replacement, never staleness pruning.
        hof_sigs = {g["signature"]: g for g in state["hall_of_fame"]}
        for g in state["elite"]:
            held = hof_sigs.get(g["signature"])
            if held is None or g["fitness"] > held["fitness"]:
                if held is not None:
                    state["hall_of_fame"].remove(held)
                state["hall_of_fame"].append(dict(g))
                hof_sigs[g["signature"]] = g
        state["hall_of_fame"].sort(key=lambda g: g["fitness"], reverse=True)
        del state["hall_of_fame"][HOF_SIZE:]
        if changed:
            state["generation"] += 1
        _save_state_locked(state)
    return changed


def _novelty_score(state: dict, sig: str) -> float:
    return 1.0 / ((state["novelty_archive"].get(sig, 0) + 1.0) ** 0.5)


def parents_for(son_id: int, n: int = INJECT_MAX, rng=None) -> list[dict]:
    """Cross-agent knowledge sharing: up to n elite genome ENTRIES (checkpoint best-entry
    dicts, directly compatible with mutation.mutate_batch) chosen by tournament selection on
    fitness, with a NOVELTY_SHARE slice chosen by novelty score instead -- preferring genomes
    discovered by OTHER agents so each son's mutation operators improve on its siblings' finds.
    Returns [] when the manager is disabled (the caller then behaves exactly as before)."""
    if not enabled():
        return []
    import random as random_mod
    rng = rng or random_mod
    with _lock:
        state = _load_state()
        pool = [g for g in state["elite"] if g["son_id"] != son_id] or list(state["elite"])
        if not pool:
            return []
        picked, seen_sigs = [], set()
        n = min(n, len(pool))
        n_novel = max(1, round(n * NOVELTY_SHARE)) if n > 1 else 0
        by_novelty = sorted(pool, key=lambda g: _novelty_score(state, g["signature"]), reverse=True)
        for g in by_novelty:
            if len(picked) >= n_novel:
                break
            if g["signature"] not in seen_sigs:
                picked.append(g)
                seen_sigs.add(g["signature"])
        attempts = 0
        while len(picked) < n and attempts < n * 10:
            attempts += 1
            contenders = [rng.choice(pool) for _ in range(min(TOURNAMENT_K, len(pool)))]
            winner = max(contenders, key=lambda g: g["fitness"])
            if winner["signature"] not in seen_sigs:
                picked.append(winner)
                seen_sigs.add(winner["signature"])
        state["injections"] += len(picked)
        return [dict(g["entry"]) for g in picked]


def mutation_boost(son_id: int) -> float:
    """Adaptive mutation: the mean mutation multiplier of the species this son's own elite
    genomes belong to (1.0 when disabled / no data) -- a son working stagnant species mutates
    harder, one whose species are still improving keeps its own self-tuned rate."""
    if not enabled():
        return 1.0
    with _lock:
        state = _load_state()
        keys = {g["species"] for g in state["elite"] if g["son_id"] == son_id}
        if not keys:
            return 1.0
        vals = [state["species"].get(k, {}).get("mutation_multiplier", 1.0) for k in keys]
        return sum(vals) / len(vals)


def record_child(child_id: str, parent_id: str, son_id: int, roi_pct: float | None) -> None:
    """Genome lineage for a mutation child that traces back to an injected/own best parent --
    called by sim_loop when a mutated candidate's simulation is admitted to a best list."""
    _append_lineage({"event": "mutation_child", "genome_id": child_id, "parent_id": parent_id,
                     "son_id": son_id, "roi_pct": roi_pct, "at": time.time()})


def _prune_excluded_elites() -> int:
    """Periodic recheck: evict any elite/HOF-independent genome whose skins have since turned
    Excluded. The genome re-enters naturally on a later ingest once its skins recover -- same
    'temporary state, never a blacklist' guarantee as market_reliability itself."""
    with _lock:
        state = _load_state()
        before = len(state["elite"])
        state["elite"] = [g for g in state["elite"] if not _entry_excluded(g["entry"])]
        evicted = before - len(state["elite"])
        if evicted:
            _save_state_locked(state)
        return evicted


# --- Meta-agent creation / refresh ---------------------------------------------------------

_CROSSOVER_PARAMS = ("exploration_rate", "risk_tolerance", "collection_mixing_bias",
                      "wear_optimization_bias", "liquidity_importance", "roi_weight",
                      "mutation_strength", "memory_weight")

# Post-crossover personality bias: the spec's stated priorities for each meta agent, blended
# 50/50 with the raw uniform crossover so inherited behavior dominates neither direction.
_META_BIAS = {
    4: {"roi_weight": 1.0, "liquidity_importance": 0.95, "risk_tolerance": 0.20,
        "memory_weight": 0.80},                                        # Meta Alpha
    5: {"exploration_rate": 1.0, "mutation_strength": 1.0, "collection_mixing_bias": 0.95,
        "memory_weight": 0.20, "liquidity_importance": 0.25},          # Meta Beta
}


def _specialist_fitness(state: dict) -> dict:
    """son_id -> best elite fitness, specialists (sons 0-3) only."""
    out: dict = {}
    for g in state["elite"]:
        if g["son_id"] in agents_mod.META_AGENT_IDS:
            continue
        out[g["son_id"]] = max(out.get(g["son_id"], 0.0), g["fitness"])
    return out


def _live_params_for(sons: list, son_id: int) -> dict:
    """A specialist parent's CURRENT behavior: its live self-tuned adaptive params overlaid on
    its static profile -- what the agent actually does today, not its hand-written starting
    personality."""
    profile = agents_mod.profile_for(son_id)
    params = {p: getattr(profile, p) for p in _CROSSOVER_PARAMS}
    son = next((s for s in sons if s.son_id == son_id), None)
    if son is not None:
        with son.lock:
            adaptive = son.checkpoint.get("adaptive")
            if adaptive:
                params.update(adaptive_mod.live_params(adaptive))
    return params


def refresh_meta_agents(sons: list, rng=None) -> dict | None:
    """Behavioral crossover: uniform per-param inheritance from the two best-performing
    specialists (by elite fitness), then a 50/50 blend toward each meta agent's spec-stated
    priorities (_META_BIAS). Writes agents.META_PROFILE_FILE, which profile_for() overlays --
    each meta agent's accumulated experience (checkpoint/simulations/model) is untouched, only
    the base personality its adaptive bounds are centered on moves."""
    import random as random_mod
    rng = rng or random_mod
    with _lock:
        state = _load_state()
        fitness = _specialist_fitness(state)
    if len(fitness) < 2:
        return None  # not enough specialist performance data to breed from yet
    parents = sorted(fitness, key=fitness.get, reverse=True)[:2]
    pa, pb = (_live_params_for(sons, parents[0]), _live_params_for(sons, parents[1]))
    now = time.time()
    payload = {"agents": {}, "parents": parents, "refreshed_at": now}
    for meta_id in agents_mod.META_AGENT_IDS:
        crossed = {p: (pa if rng.random() < 0.5 else pb)[p] for p in _CROSSOVER_PARAMS}
        for p, target in _META_BIAS[meta_id].items():
            crossed[p] = round(0.5 * crossed[p] + 0.5 * target, 4)
        crossed = {p: round(min(1.0, max(0.0, v)), 4) for p, v in crossed.items()}
        payload["agents"][str(meta_id)] = {"params": crossed, "parents": parents,
                                            "refreshed_at": now}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _io_lock:
        META_PROFILE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    with _lock:
        state = _load_state()
        state["last_meta_refresh_at"] = now
        state["meta_events"] = ([{"at": now, "parents": parents}] + state["meta_events"])[:20]
        _save_state_locked(state)
    _append_lineage({"event": "meta_refresh", "parents": parents, "at": now})
    return payload


# --- Hyperparameter evolution (population-based training) ----------------------------------

def _recent_reward(son) -> float | None:
    with son.lock:
        adaptive = son.checkpoint.get("adaptive") or {}
        history = adaptive.get("reward_history") or []
    rewards = [h["reward"] for h in history[-10:] if h.get("reward") is not None]
    if len(rewards) < MIN_REWARD_POINTS:
        return None
    return sum(rewards) / len(rewards)


def run_hyperparam_evolution(sons: list, rng=None) -> dict | None:
    """One PBT step: the son with the best recent adaptation reward donates a MUTATED copy of
    its hyperparams to the son with the worst (exploit + explore, the standard PBT move).
    Mutation = each value jittered +/- PBT_MUTATE_FRAC, clamped to adaptive.HYPERPARAM_BOUNDS.
    The receiving son's in-flight A/B window keeps running -- its next window simply measures
    under the new knobs (rewards are always re-baselined per window, so no stale comparison)."""
    import random as random_mod
    rng = rng or random_mod
    scored = [(s, r) for s in sons if (r := _recent_reward(s)) is not None]
    if len(scored) < 2:
        return None
    scored.sort(key=lambda sr: sr[1], reverse=True)
    donor, donor_reward = scored[0]
    receiver, receiver_reward = scored[-1]
    if donor.son_id == receiver.son_id:
        return None
    with donor.lock:
        donor_hp = adaptive_mod.hyperparams(donor.checkpoint.get("adaptive") or {})
    mutated = {}
    for k, v in donor_hp.items():
        lo, hi = adaptive_mod.HYPERPARAM_BOUNDS[k]
        jitter = 1.0 + rng.uniform(-PBT_MUTATE_FRAC, PBT_MUTATE_FRAC)
        nv = min(max(v * jitter, lo), hi)
        mutated[k] = int(round(nv)) if k == "adapt_window_rounds" else round(nv, 4)
    from . import sim_loop as sim_loop_mod  # local: sim_loop imports this module
    with receiver.lock:
        adaptive = adaptive_mod.ensure_state(receiver.checkpoint, receiver.profile)
        adaptive["hyperparams"] = mutated
        sim_loop_mod.save_checkpoint(receiver.checkpoint_file, receiver.checkpoint)
    now = time.time()
    event = {"at": now, "donor": donor.son_id, "receiver": receiver.son_id,
             "donor_reward": round(donor_reward, 2), "receiver_reward": round(receiver_reward, 2),
             "hyperparams": mutated}
    with _lock:
        state = _load_state()
        state["last_pbt_at"] = now
        state["pbt_events"] = ([event] + state["pbt_events"])[:20]
        _save_state_locked(state)
    _append_lineage({"event": "hyperparam_transfer", **event})
    return event


# --- Manager loop / snapshot ----------------------------------------------------------------

def tick(sons: list) -> dict:
    """One manager step: ingest, periodic elite recheck, meta refresh, PBT -- each on its own
    cadence. Called by watch() and by the on-demand API. No-op when disabled."""
    if not enabled():
        return {"enabled": False}
    now = time.time()
    result = {"enabled": True, "ingested": ingest_population(sons)}
    with _lock:
        state = _load_state()
        recheck_due = (state["last_elite_recheck_at"] is None
                       or now - state["last_elite_recheck_at"] >= ELITE_RECHECK_SECONDS)
        meta_due = (state["last_meta_refresh_at"] is None
                    or now - state["last_meta_refresh_at"] >= META_REFRESH_SECONDS)
        pbt_due = (state["last_pbt_at"] is None
                   or now - state["last_pbt_at"] >= PBT_INTERVAL_SECONDS)
    if recheck_due:
        result["evicted_excluded"] = _prune_excluded_elites()
        with _lock:
            state = _load_state()
            state["last_elite_recheck_at"] = now
            _save_state_locked(state)
    if meta_due:
        result["meta_refreshed"] = refresh_meta_agents(sons) is not None
    if pbt_due:
        result["pbt_event"] = run_hyperparam_evolution(sons)
    with _lock:
        state = _load_state()
        state["last_tick_at"] = now
        _save_state_locked(state)
    return result


def watch(sons: list, stop_event: threading.Event, interval: float = TICK_SECONDS) -> None:
    """Background manager loop, one thread for the whole population -- same shape as
    sim_loop.watch_convergence. A failing tick is surfaced in the state and retried next
    interval, never allowed to kill the thread (same guarantee as sim_loop.sim_loop)."""
    while not stop_event.wait(timeout=interval):
        try:
            tick(sons)
        except Exception as e:  # noqa: BLE001 -- deliberate catch-all, see docstring
            with _lock:
                state = _load_state()
                state["last_error"] = f"{type(e).__name__}: {e}"
                _save_state_locked(state)


def snapshot() -> dict:
    """JSON-safe view for the dashboard's Evolution panel + /api/evolution/status."""
    with _lock:
        state = _load_state()
        species = [{"species": k, **v} for k, v in state["species"].items()]
        species.sort(key=lambda s: (s.get("best_fitness") or 0), reverse=True)
        return {
            "enabled": _load_config()["enabled"],
            "generation": state["generation"],
            "elite_size": len(state["elite"]),
            "elite_capacity": ELITE_SIZE,
            "elite_top": [{"genome_id": g["genome_id"], "son_id": g["son_id"],
                           "fitness": round(g["fitness"], 2), "species": g["species"],
                           "roi_pct": g["entry"].get("expected_roi_pct"),
                           "added_at": g["added_at"]} for g in state["elite"][:15]],
            "hall_of_fame": [{"genome_id": g["genome_id"], "son_id": g["son_id"],
                              "fitness": round(g["fitness"], 2), "species": g["species"],
                              "roi_pct": g["entry"].get("expected_roi_pct"),
                              "added_at": g["added_at"]} for g in state["hall_of_fame"]],
            "n_species": len(state["species"]),
            "species": species[:20],
            "novelty_archive_size": len(state["novelty_archive"]),
            "injections": state["injections"],
            "last_tick_at": state["last_tick_at"],
            "last_meta_refresh_at": state["last_meta_refresh_at"],
            "last_pbt_at": state["last_pbt_at"],
            "pbt_events": state["pbt_events"][:5],
            "meta_events": state["meta_events"][:5],
            "last_error": state.get("last_error"),
        }
