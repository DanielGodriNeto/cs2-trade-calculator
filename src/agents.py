"""Named agent personalities for the 4-son simulation population (see sim_loop.py).

Historically the 4 "sons" differed by nothing but a single softmax temperature
scalar (a fixed SON_TEMPERATURES list). This module formalizes them into 4 named
agents -- Collection Mixer, ROI Accountant, Wear Efficiency Agent, Discovery Agent --
each carrying 8 named 0-1 behavioral parameters. The mapping from son_id (0-3) to
agent identity is FIXED and the underlying storage (data/sons/son_N/) is UNCHANGED --
this is a behavior + display layer on top of the existing per-son isolation, not a
directory rename (renaming would orphan already-accumulated simulations/models for
existing users -- see sim_loop.py's own docstring on why each son's data must stay
isolated).

Where a parameter's value is a worked example, it's noted; every other value was
chosen to fit that agent's stated purpose, not precisely calibrated -- tunable later.

NOTE (self-tuning, see adaptive.py): the five SEARCH parameters -- exploration_rate,
memory_weight, collection_mixing_bias, wear_optimization_bias, mutation_strength -- are now
STARTING points only. Each son adjusts its own live copies automatically (persisted in its
checkpoint, bounded around these base values) toward a ROI-first + novelty reward.
risk_tolerance, liquidity_importance, and roi_weight stay fixed at the values below --
adaptive.py's module docstring explains why letting an agent tune those would be reward
hacking, not learning.

How each parameter actually affects behavior (see sim_loop.py / random_trade.py):
- exploration_rate: how large a candidate pool this agent draws per round before
  narrowing (random_trade.generate_batch's `n`) -- see sim_loop.run_round.
- memory_weight: how sharply this agent trusts its own trained model's ranking when
  choosing which of that pool to actually simulate. HIGH memory_weight -> LOW softmax
  temperature (exploit); LOW memory_weight -> HIGH temperature (ignore the model,
  sample broadly). Replaces the old fixed SON_TEMPERATURES list -- see
  base_temperature() below.
- collection_mixing_bias: how willing this agent is to dilute a trade-up across many
  collections -- a per-agent override of random_trade.FILLER_COUNT_DECAY (higher bias
  -> flatter decay -> more filler collections favored). See filler_count_decay().
- wear_optimization_bias: how strongly this agent's random wear draws favor the
  bracket EXTREMES (Factory New / Battle-Scarred) over the middle brackets, instead of
  uniform-random -- see random_trade._wear_weights.
- risk_tolerance / liquidity_importance: how strict this agent is when deciding
  whether a simulated find is good enough to enter ITS OWN best-known list -- see
  sim_loop._consider_best.
- mutation_strength: what fraction of a round's simulated batch comes from mutating
  this agent's own current best finds (mutation.py) instead of drawing fresh random
  candidates -- see sim_loop.run_round.
- roi_weight: this agent's weight on raw ROI vs. ranking.py's other factors
  (profit/liquidity/confidence/discovery) when its finds are merged into the global
  final ranking across all 4 agents -- see ranking.py.
"""
import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class AgentProfile:
    son_id: int
    name: str
    purpose: str
    exploration_rate: float
    risk_tolerance: float
    collection_mixing_bias: float
    wear_optimization_bias: float
    liquidity_importance: float
    roi_weight: float
    mutation_strength: float
    memory_weight: float


AGENT_PROFILES: dict[int, AgentProfile] = {
    0: AgentProfile(
        son_id=0, name="Collection Mixer",
        purpose="Finds profitable collection combinations -- different collection ratios, "
                "mixed collections, hidden ROI.",
        exploration_rate=0.85, risk_tolerance=0.70,            # worked example values
        collection_mixing_bias=0.95, mutation_strength=0.80,   # worked example values
        wear_optimization_bias=0.30, liquidity_importance=0.40,
        roi_weight=0.60, memory_weight=0.40,
    ),
    1: AgentProfile(
        son_id=1, name="ROI Accountant",
        purpose="Finds mathematically strongest trades -- highest expected ROI, lowest "
                "unnecessary risk, avoids fake profits.",
        roi_weight=1.0, liquidity_importance=0.95,              # worked example values
        exploration_rate=0.40, risk_tolerance=0.20,             # worked example values
        collection_mixing_bias=0.15, wear_optimization_bias=0.40,
        mutation_strength=0.35, memory_weight=0.85,
    ),
    2: AgentProfile(
        son_id=2, name="Wear Efficiency Agent",
        purpose="Optimizes wear categories -- cheap inputs, better output wear value, "
                "whether upgrading input wear is worth the cost.",
        wear_optimization_bias=1.0, roi_weight=1.0,             # worked example values
        exploration_rate=0.70, mutation_strength=0.60,          # worked example values
        risk_tolerance=0.45, collection_mixing_bias=0.30,
        liquidity_importance=0.55, memory_weight=0.55,
    ),
    3: AgentProfile(
        son_id=3, name="Discovery Agent",
        purpose="Finds opportunities other agents miss -- unusual combinations, unpopular "
                "collections, high-risk/high-reward strategies.",
        exploration_rate=1.0, mutation_strength=1.0,            # worked example values
        risk_tolerance=0.90, collection_mixing_bias=1.0,        # worked example values
        wear_optimization_bias=0.50, liquidity_importance=0.20,
        roi_weight=0.50, memory_weight=0.15,
    ),
}

# --- Second-generation Meta Agents (Evolution Engine 2.0) --------------------------------
#
# Sons 4-5 are META agents: unlike the four specialists above (whose personalities are fixed
# starting points chosen by hand), a meta agent's behavioral parameters are INHERITED -- the
# Evolution Manager (evolution.py) performs behavioral crossover from the best-performing
# specialists and persists the result to META_PROFILE_FILE, which profile_for() overlays on
# the seed values below. The seeds only matter until the first crossover happens (a brand-new
# install has no performance data to cross over from yet). The four specialists themselves are
# NEVER modified by any of this -- meta agents are additive, storage-isolated in their own
# data/sons/son_4 / son_5 directories like every other son, and keep learning independently
# through the exact same adaptive.py machinery once created.
#
# The Evolution Manager refreshes the inherited values below periodically (see
# evolution.refresh_meta_agents) while each meta agent's OWN accumulated experience --
# simulations, trained model, checkpoint, self-tuned adaptive params -- is preserved untouched;
# only the BASE personality (the center of adaptive.bounds_for's per-param band) moves.

META_AGENT_IDS = (4, 5)

_META_SEED_PROFILES: dict[int, AgentProfile] = {
    4: AgentProfile(
        son_id=4, name="Meta Alpha",
        purpose="Second-generation agent bred from the best specialists -- prioritizes ROI, "
                "confidence, verification and liquidity.",
        roi_weight=1.0, liquidity_importance=0.90, risk_tolerance=0.25,
        exploration_rate=0.50, collection_mixing_bias=0.30, wear_optimization_bias=0.50,
        mutation_strength=0.45, memory_weight=0.75,
    ),
    5: AgentProfile(
        son_id=5, name="Meta Beta",
        purpose="Second-generation agent bred from the best specialists -- prioritizes "
                "novelty, discovery, rare collections and mutation diversity.",
        exploration_rate=0.95, mutation_strength=0.95, collection_mixing_bias=0.90,
        risk_tolerance=0.80, liquidity_importance=0.30, roi_weight=0.55,
        wear_optimization_bias=0.45, memory_weight=0.25,
    ),
}

AGENT_PROFILES.update(_META_SEED_PROFILES)

AGENT_NAMES = {sid: p.name for sid, p in AGENT_PROFILES.items()}

META_PROFILE_FILE = Path(__file__).resolve().parent.parent / "data" / "evolution" / "meta_agents.json"

_meta_lock = threading.Lock()
_meta_cache: dict = {"mtime": None, "profiles": {}}


def _meta_overrides() -> dict:
    """{son_id: AgentProfile} built from evolution.py's persisted crossover file, cached by
    mtime (same convention as market_reliability._history_index). Missing/corrupt file or
    out-of-range values -> empty dict, i.e. the seed profiles stand."""
    if not META_PROFILE_FILE.exists():
        return {}
    mtime = META_PROFILE_FILE.stat().st_mtime
    with _meta_lock:
        if _meta_cache["mtime"] == mtime:
            return _meta_cache["profiles"]
        profiles: dict = {}
        try:
            stored = json.loads(META_PROFILE_FILE.read_text(encoding="utf-8"))
            for sid_str, payload in (stored.get("agents") or {}).items():
                sid = int(sid_str)
                if sid not in _META_SEED_PROFILES:
                    continue
                params = payload.get("params") or {}
                fields = {}
                for f in ("exploration_rate", "risk_tolerance", "collection_mixing_bias",
                          "wear_optimization_bias", "liquidity_importance", "roi_weight",
                          "mutation_strength", "memory_weight"):
                    v = params.get(f)
                    if isinstance(v, (int, float)) and 0.0 <= v <= 1.0:
                        fields[f] = float(v)
                if fields:
                    profiles[sid] = replace(_META_SEED_PROFILES[sid], **fields)
        except (json.JSONDecodeError, OSError, ValueError):
            profiles = {}
        _meta_cache["mtime"] = mtime
        _meta_cache["profiles"] = profiles
        return profiles


def profile_for(son_id: int) -> AgentProfile:
    if son_id in META_AGENT_IDS:
        override = _meta_overrides().get(son_id)
        if override is not None:
            return override
    return AGENT_PROFILES[son_id]


# Softmax temperature bounds -- replaces the old fixed SON_TEMPERATURES = [0.2, 0.5, 1.0, 2.0]
# list (kept, but now derived, in sim_loop.py for the one existing reader that expects that
# exact name/shape: web/server.py's /api/constants).
TEMPERATURE_MIN = 0.15
TEMPERATURE_MAX = 2.2


def temperature_for(memory_weight: float) -> float:
    """HIGH memory_weight -> LOW temperature (trusts/exploits its own trained model's
    ranking); LOW memory_weight -> HIGH temperature (ignores the model, samples broadly).
    Takes the raw value (not a profile) so it works on adaptive.py's live per-son params
    as well as the static base profiles. See this module's docstring."""
    return TEMPERATURE_MIN + (1.0 - memory_weight) * (TEMPERATURE_MAX - TEMPERATURE_MIN)


def base_temperature(profile: AgentProfile) -> float:
    """temperature_for() at this agent's STARTING memory_weight -- the live value drifts,
    see adaptive.py."""
    return temperature_for(profile.memory_weight)


# random_trade.FILLER_COUNT_DECAY's per-agent override range -- see collection_mixing_bias
# in this module's docstring. Concentrated/single-collection agents (e.g. ROI Accountant)
# sit near _MIXING_DECAY_MIN; diluted/many-collection agents (e.g. Discovery Agent) sit
# near _MIXING_DECAY_MAX.
_MIXING_DECAY_MIN = 0.15
_MIXING_DECAY_MAX = 0.90


def filler_decay_for(collection_mixing_bias: float) -> float:
    """Raw-value variant, same reason as temperature_for(): usable with adaptive.py's live
    per-son params, not just a static profile."""
    return _MIXING_DECAY_MIN + collection_mixing_bias * (_MIXING_DECAY_MAX - _MIXING_DECAY_MIN)


def filler_count_decay(profile: AgentProfile) -> float:
    return filler_decay_for(profile.collection_mixing_bias)
