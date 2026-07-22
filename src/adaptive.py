"""Self-tuning agent preferences: each population son automatically adjusts its own search
personality over time, instead of running forever on the fixed values in agents.py.

agents.py's AgentProfile values are now STARTING points. Five of the eight parameters -- the
SEARCH parameters -- become live, per-son values persisted in that son's own checkpoint
(checkpoint["adaptive"], wiped by full_reset like everything else) and adjusted automatically:

    exploration_rate, memory_weight, collection_mixing_bias,
    wear_optimization_bias, mutation_strength           (ADAPTIVE_PARAMS)

The other three are DELIBERATELY fixed, for reward-hacking reasons, not laziness:
- roi_weight is ranking.py's trust weight for this agent's finds in the final cross-agent
  ranking -- letting an agent tune the weight its own results are judged by would let it game
  its evaluation rather than improve its search.
- liquidity_importance / risk_tolerance are sim_loop._consider_best's data-QUALITY admission
  gates. The adaptation reward below is built from what the agent finds; letting it loosen its
  own quality gates would let it "improve" its reward by admitting thin/stale junk it should
  be rejecting -- the exact fake-high-ROI failure mode ranking.py exists to prevent.

How adaptation works -- a windowed (1+1) evolution strategy, the simplest self-tuning loop
that is robust to this environment's noise and needs no extra simulate() budget:
1. Run ADAPT_WINDOW_ROUNDS rounds with the current adopted params and measure a reward
   ("baseline" phase).
2. Gaussian-perturb the params (PERTURB_SIGMA, clamped to per-agent bounds -- see bounds_for)
   and run the SAME number of rounds with the tweaked params ("trial" phase).
3. Adopt the tweak only if its window reward beats the baseline by more than ADOPT_EPSILON_PP
   (guards against adopting on noise); either way, go back to step 1 and re-measure a fresh
   baseline -- prices drift, so an old baseline is never trusted twice.

The reward (window_reward) is ROI-FIRST with an explicit exploration term, per the spec
"ROI and trying new skins/collections are both priorities":
    reward = median expected ROI pp of the window's VOLUME-CONFIRMED valid sims
             + NOVELTY_REWARD_PP * (fraction of valid sims landing in under-explored collections)
             - INVALID_PENALTY_PP * (fraction of attempts that came back invalid/unpriceable)
Median (not mean) so one absurd thin-market outlier can't steer the policy; low_volume_warning
rows are excluded from the ROI term entirely -- the same filter sim_learning.train() applies --
so an agent can never earn reward through prices too thin to trust.

Exploration can never collapse: bounds_for() clamps every agent's exploration_rate to at least
EXPLORATION_RATE_FLOOR, memory_weight to at most MEMORY_WEIGHT_CEIL (softmax temperature
never fully freezes onto the model's top picks), and mutation_strength to at most
MUTATION_STRENGTH_CEIL (a fixed share of every round stays fresh random draws rather than
re-mutations of what this son already found) -- and each param stays within BAND_RADIUS of the
agent's base personality so the four agents remain genuinely different searchers instead of all
drifting to one point. A tightened clamp reaches already-adopted values too, not just newly
proposed ones -- see _clamp_to_bounds, without which the mutation ceiling would have taken
effect only if some later coin-flip trial happened to be adopted.

Separately from parameter adaptation, novelty_bonus() gives candidate SELECTION a UCB-style
"optimism about the un-tried" bonus: each son tracks how many of its valid sims have used each
collection (checkpoint["coverage"], unit-share weighted), and a candidate's selection score is
its model-predicted ROI plus exploration_rate * NOVELTY_UCB_PP * sqrt(ln(total)/(n+1)) per leg.
A collection the son has never priced gets a real, structural bonus that shrinks as it gets
explored -- the "don't miss a powerful trade-up hiding in a collection the model has simply
never seen" guarantee, working even before a model is trained at all.

NOTE on nudges: nudge() (manual or anti-convergence) temporarily boosts temperature mid-window,
which adds noise to that window's A/B comparison. Accepted -- ADOPT_EPSILON_PP plus the
re-measured baseline after every trial keep a noisy window from permanently mis-steering params.
"""
import math
import time
from statistics import median

from . import agents as agents_mod

# The five search parameters that self-tune (see module docstring for why the other three
# AgentProfile parameters are deliberately fixed).
ADAPTIVE_PARAMS = ("exploration_rate", "memory_weight", "collection_mixing_bias",
                   "wear_optimization_bias", "mutation_strength")

ADAPT_WINDOW_ROUNDS = 4   # rounds per measurement window (baseline or trial). At
# SIMULATE_PER_ROUND=40 that's up to ~160 sims per window -- enough for a stable median.
ADOPT_EPSILON_PP = 0.5    # a trial must beat the baseline reward by more than this (percentage
# points) to be adopted -- same guard-against-noise idea as sim_loop.IMPROVEMENT_EPSILON_PP
PERTURB_SIGMA = 0.12      # stddev of the Gaussian tweak applied to each param per trial
BAND_RADIUS = 0.35        # each param may drift at most this far from the agent's base value --
# wide enough to matter, narrow enough that the 4 personalities stay distinct
PARAM_FLOOR = 0.05        # absolute floor for any adaptive param (0 exactly would switch some
# behaviors fully off, e.g. mutation_strength=0 never mutates again)
EXPLORATION_RATE_FLOOR = 0.25  # global exploration floor -- no agent may stop casting a wide
# candidate pool entirely, no matter how well pure exploitation is paying right now
MEMORY_WEIGHT_CEIL = 0.90      # softmax temperature never fully collapses onto the model's top
# picks (see agents.temperature_for: 0.90 still leaves temperature ~0.35)
MUTATION_STRENGTH_CEIL = 0.60  # a fixed share of every round must stay FRESH random draws.
# Third of the "exploration can never collapse" clamps, and the one that was actually missing.
#
# mutation_strength is the fraction of a round's batch that comes from mutating this son's own
# best-known list (see sim_loop.run_round). Left uncapped it reached 0.94-1.00 on four of the
# six sons, i.e. essentially the WHOLE budget was re-mutations of a best-50 list that had not
# changed in ~600 rounds (checkpoint rounds_since_improvement: 547-606). Those mutants are also
# spliced in AFTER generate_batch has already run selection, so they bypass both the model's
# scoring and novelty_bonus below -- which is why every novelty tweak measured as a no-op on
# those sons: novelty only ever got to influence the 0-6% of the batch that wasn't a mutant.
#
# Replayed over the sons' real checkpoints/models (mutation.py's collection-move operator
# already in place, so this measures the cap ALONE, not the trap it used to compound):
#     share       distinct skins   collections   coverage entropy   pred ROI p90
#     uncapped         773            72.8           2.863           -34.01
#     <= 0.8           949            84.5           3.297           -34.13
#     <= 0.6          1145            86.7           3.638           -34.34
# ~48% more distinct skins and a materially flatter collection spread, for 0.3pp of p90
# predicted ROI -- which is noise at this model's MAE (~4.6pp). Mean predicted ROI does drop
# slightly, as it must: a batch that explores more has a worse AVERAGE by construction. The
# tail is what this search is actually mining, and the tail is flat.
#
# Not lower than 0.6: mutation is the "improve an existing good find" half of the search and
# still has to have real budget. Sons whose base personality sits above this (the Discovery
# Agent at 1.00, Collection Mixer at 0.80) end up clamped into a narrow band just under the
# ceiling by bounds_for's own min/max normalisation -- deliberate; their exploratory identity
# is carried by exploration_rate and collection_mixing_bias, not by refusing to draw anything
# fresh.

NOVELTY_UCB_PP = 8.0      # scale (in ROI percentage points) of the UCB selection bonus for an
# unexplored collection -- comparable to a genuinely-good ROI edge, so novelty really competes
NOVELTY_SEEN_THRESHOLD = 3.0  # a collection with less than this much unit-share-weighted
# coverage still counts as "under-explored" for the reward's novelty fraction
NOVELTY_REWARD_PP = 12.0  # max reward bonus (pp) when 100% of a window's valid sims explored
# under-explored collections
INVALID_PENALTY_PP = 8.0  # max reward penalty (pp) when 100% of a window's attempts were invalid
REWARD_HISTORY_KEEP = 40  # adaptation decisions remembered per son (checkpoint payload cap)

def bounds_for(profile: "agents_mod.AgentProfile") -> dict:
    """Per-agent [lo, hi] clamp for every adaptive param: BAND_RADIUS around that agent's own
    base personality, intersected with the global floors/ceilings above."""
    out = {}
    for param in ADAPTIVE_PARAMS:
        base = getattr(profile, param)
        lo = max(PARAM_FLOOR, base - BAND_RADIUS)
        hi = min(1.0, base + BAND_RADIUS)
        if param == "exploration_rate":
            lo = max(lo, EXPLORATION_RATE_FLOOR)
        if param == "memory_weight":
            hi = min(hi, MEMORY_WEIGHT_CEIL)
        if param == "mutation_strength":
            hi = min(hi, MUTATION_STRENGTH_CEIL)
        out[param] = (min(lo, hi), max(lo, hi))
    return out


def _clamped_base_params(profile: "agents_mod.AgentProfile") -> dict:
    bounds = bounds_for(profile)
    return {p: min(max(getattr(profile, p), bounds[p][0]), bounds[p][1]) for p in ADAPTIVE_PARAMS}


def initial_state(profile: "agents_mod.AgentProfile") -> dict:
    return {
        "params": _clamped_base_params(profile),  # currently ADOPTED params
        "trial_params": None,                     # the tweak being A/B-tested (trial phase only)
        "phase": "baseline",                      # "baseline" | "trial"
        "window_rounds": 0,
        "window_rois": [],       # expected_roi_pct of this window's volume-CONFIRMED valid sims
        "window_novel": 0,       # valid sims that landed in an under-explored collection
        "window_valid": 0,
        "window_invalid": 0,
        "baseline_reward": None,
        "last_reward": None,
        "last_action": None,     # "trial_started" | "adopted" | "reverted"
        "adaptations_tried": 0,
        "adaptations_adopted": 0,
        "reward_history": [],    # [{at, reward, action, params}], newest last, capped
    }


def ensure_state(cp: dict, profile: "agents_mod.AgentProfile") -> dict:
    """Initializes (or backfills, for checkpoints written before a field existed) this son's
    checkpoint["adaptive"] block. Called at the top of every round -- cheap after the first."""
    if not cp.get("adaptive"):
        cp["adaptive"] = initial_state(profile)
    else:
        for k, v in initial_state(profile).items():
            cp["adaptive"].setdefault(k, v)
        _clamp_to_bounds(cp["adaptive"], profile)
    return cp["adaptive"]


def _clamp_to_bounds(adaptive: dict, profile: "agents_mod.AgentProfile") -> None:
    """Re-clamps the ADOPTED (and any in-flight trial) params into the current bounds.

    perturb() clamps the values it generates, so bounds_for() governs everything a son
    proposes -- but an already-adopted value is never re-examined, so tightening a bound would
    otherwise leave every existing checkpoint running outside it indefinitely: the son keeps
    searching at the old value and only comes down if some later trial happens to be adopted,
    which is a coin flip that may never land. Doing it here means a new clamp takes effect on
    the very next round for every son, including ones mid-trial.

    This is what made MUTATION_STRENGTH_CEIL actually bite: the six live checkpoints carried
    adopted mutation_strength of 0.35-1.00, four of them above the new ceiling."""
    bounds = bounds_for(profile)
    for key in ("params", "trial_params"):
        params = adaptive.get(key)
        if not params:
            continue
        for param, (lo, hi) in bounds.items():
            if param in params:
                params[param] = min(max(params[param], lo), hi)


def live_params(adaptive: dict) -> dict:
    """The params this son should actually search with RIGHT NOW -- the trial tweak while one
    is being measured, the adopted params otherwise."""
    if adaptive["phase"] == "trial" and adaptive["trial_params"]:
        return dict(adaptive["trial_params"])
    return dict(adaptive["params"])


def perturb(params: dict, bounds: dict, rng, sigma: float = PERTURB_SIGMA) -> dict:
    """One Gaussian tweak of every adaptive param, clamped to this agent's bounds. sigma
    defaults to the historical constant; end_round passes the son's own evolved value."""
    out = {}
    for p in ADAPTIVE_PARAMS:
        lo, hi = bounds[p]
        out[p] = min(max(params[p] + rng.gauss(0.0, sigma), lo), hi)
    return out


def note_valid(adaptive: dict, expected_roi_pct: float, low_volume: bool, novel: bool) -> None:
    """Folds one successfully simulated candidate into the current measurement window.
    Low-volume rows count as valid activity but are excluded from the ROI term -- see the
    module docstring."""
    adaptive["window_valid"] += 1
    if novel:
        adaptive["window_novel"] += 1
    if not low_volume:
        adaptive["window_rois"].append(expected_roi_pct)


def note_invalid(adaptive: dict) -> None:
    adaptive["window_invalid"] += 1


def window_reward(adaptive: dict) -> float:
    """See module docstring. A window with zero valid sims scores its ROI term as 0 and still
    takes the full invalid penalty -- a tweak that prices nothing must lose to one that works."""
    rois = adaptive["window_rois"]
    roi_term = median(rois) if rois else 0.0
    valid = adaptive["window_valid"]
    novelty_frac = (adaptive["window_novel"] / valid) if valid else 0.0
    attempts = valid + adaptive["window_invalid"]
    invalid_frac = (adaptive["window_invalid"] / attempts) if attempts else 0.0
    return roi_term + NOVELTY_REWARD_PP * novelty_frac - INVALID_PENALTY_PP * invalid_frac


def _reset_window(adaptive: dict) -> None:
    adaptive["window_rounds"] = 0
    adaptive["window_rois"] = []
    adaptive["window_novel"] = 0
    adaptive["window_valid"] = 0
    adaptive["window_invalid"] = 0


def end_round(adaptive: dict, profile: "agents_mod.AgentProfile", rng) -> "str | None":
    """Called once per completed round (sim_loop.run_round's end-of-round bookkeeping, under the
    son's own lock). Advances the measurement window; when a window completes, runs one step of
    the baseline->trial->adopt/revert state machine described in the module docstring. Returns
    the action taken ("trial_started"/"adopted"/"reverted") when a window closed, else None."""
    adaptive["window_rounds"] += 1
    if adaptive["window_rounds"] < ADAPT_WINDOW_ROUNDS:
        return None
    reward = window_reward(adaptive)
    adaptive["last_reward"] = reward
    if adaptive["phase"] == "baseline":
        adaptive["baseline_reward"] = reward
        adaptive["trial_params"] = perturb(adaptive["params"], bounds_for(profile), rng,
                                            sigma=PERTURB_SIGMA)
        adaptive["phase"] = "trial"
        action = "trial_started"
    else:
        adaptive["adaptations_tried"] += 1
        baseline = adaptive["baseline_reward"]
        if baseline is not None and reward > baseline + ADOPT_EPSILON_PP:
            adaptive["params"] = dict(adaptive["trial_params"])
            adaptive["adaptations_adopted"] += 1
            action = "adopted"
        else:
            action = "reverted"
        adaptive["trial_params"] = None
        adaptive["phase"] = "baseline"
        adaptive["baseline_reward"] = None  # always re-measured fresh -- see module docstring
    adaptive["last_action"] = action
    adaptive["reward_history"].append(
        {"at": time.time(), "reward": reward, "action": action, "params": dict(adaptive["params"])})
    del adaptive["reward_history"][:-REWARD_HISTORY_KEEP]
    _reset_window(adaptive)
    return action


def summary(adaptive: dict, profile: "agents_mod.AgentProfile") -> dict:
    """JSON-safe view for sim_loop.snapshot() -> the dashboard's per-agent self-tuning line.
    base_params included so the UI can show how far each param has drifted from the agent's
    original personality."""
    return {
        "phase": adaptive["phase"],
        "window_rounds": adaptive["window_rounds"],
        "window_size": ADAPT_WINDOW_ROUNDS,
        "params": live_params(adaptive),
        "adopted_params": dict(adaptive["params"]),
        "base_params": {p: getattr(profile, p) for p in ADAPTIVE_PARAMS},
        "baseline_reward": adaptive["baseline_reward"],
        "last_reward": adaptive["last_reward"],
        "last_action": adaptive["last_action"],
        "adaptations_tried": adaptive["adaptations_tried"],
        "adaptations_adopted": adaptive["adaptations_adopted"],
        "reward_history": adaptive["reward_history"][-10:],
    }


# --- Collection-coverage tracking + UCB novelty bonus (candidate selection) -------------------

def update_coverage(coverage: dict, record) -> bool:
    """Folds one valid SimulationRecord into this son's per-collection coverage counts
    (unit-share weighted: a 10-input candidate split 7/3 across two collections adds 0.7/0.3).
    Returns whether the record was NOVEL -- its primary collection's coverage BEFORE this update
    was still under NOVELTY_SEEN_THRESHOLD -- which feeds the reward's novelty fraction."""
    input_mix = record.input_mix or []
    total = sum(row.get("count", 0) for row in input_mix)
    novel = coverage.get(record.primary_collection, 0.0) < NOVELTY_SEEN_THRESHOLD
    if not total:
        return novel
    shares: dict = {}
    for row in input_mix:
        coll = row.get("collection")
        if coll:
            shares[coll] = shares.get(coll, 0.0) + row.get("count", 0) / total
    for coll, share in shares.items():
        coverage[coll] = coverage.get(coll, 0.0) + share
    return novel


def novelty_bonus(candidate, coverage: dict, coverage_total: float,
                   ucb_pp: float = NOVELTY_UCB_PP) -> float:
    """UCB-style selection bonus (in ROI percentage points) for a random_trade.Candidate whose
    collections this son has rarely (or never) simulated -- weighted by each leg's share of the
    contract's input units, so a 1-unit filler from an unexplored collection earns a tenth of
    what a full 10-unit primary would. Callers scale by the agent's live exploration_rate, so
    exploratory agents chase novelty harder than exploitative ones. ucb_pp defaults to the
    historical constant; sim_loop passes this same constant explicitly."""
    total_units = sum(u["count"] for leg in candidate.legs for u in leg.units) or 1
    log_total = math.log(coverage_total + math.e)  # +e so the bonus is well-defined from zero
    bonus = 0.0
    for leg in candidate.legs:
        leg_units = sum(u["count"] for u in leg.units)
        n_seen = coverage.get(leg.collection, 0.0)
        bonus += (leg_units / total_units) * math.sqrt(log_total / (n_seen + 1.0))
    return ucb_pp * bonus
