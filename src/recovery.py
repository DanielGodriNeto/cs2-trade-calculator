"""Exploration Recovery Mode: what happens when a son stays stuck PAST the point where the
existing soft remedies (sim_loop's manual/anti-convergence nudge()) have had their chance.

sim_loop.py already detects "stuck" (rounds_since_improvement >= STUCK_THRESHOLD_ROUNDS) and
already has one recovery lever -- nudge()'s temporary temperature kick. This module EXTENDS
that system rather than replacing it: nothing here changes when the stuck flag itself trips,
and nudge() keeps working exactly as before (manual or anti-convergence-triggered). Recovery
Mode only engages once a son has remained stuck for ENTER_AFTER_EXTRA_ROUNDS more rounds than
the plain stuck threshold -- i.e. after a nudge (5 rounds of boost) has demonstrably not been
enough -- and disengages THE MOMENT a real improvement lands (run_round resets
rounds_since_improvement under the same IMPROVEMENT_EPSILON_PP guard as always).

Two phases, per spec:

1. PROGRESSIVE ESCALATION (levels 1..MAX_LEVEL): each level applies stronger multiplicative
   pressure to the son's LIVE search params for as long as the mode is active -- mutation rate
   up, exploration up, novelty weighting up, exploitation bias (memory_weight, i.e. softmax
   sharpness) down, and search diversity (candidate-pool temperature) up. These are DERIVED
   multipliers layered on top of adaptive.py's live params at the top of each round, never
   written into the adopted params themselves -- so leaving Recovery Mode instantly restores
   the son's own self-tuned personality with nothing to undo. The level escalates every
   ESCALATE_EVERY_ROUNDS rounds while improvement still hasn't come.

2. CONTROLLED RANDOM PERTURBATION: if the son is STILL stuck after topping out at MAX_LEVEL,
   the adopted adaptive params themselves get one independent Gaussian perturbation per
   parameter per ESCALATE_EVERY_ROUNDS window -- each parameter moves on its own (some up,
   some down, sign uncorrelated across params), always clamped inside adaptive.bounds_for's
   per-agent safety limits (the same "configurable safety limits" every adaptation already
   respects). Unlike phase 1's multipliers these ARE real moves -- deliberately: by this point
   the adopted operating point itself is the suspect, and adaptive.py's normal (1+1)-ES will
   keep tuning from wherever the perturbation lands.

Every entered/escalated/perturbed/exited event is appended to data/recovery_log.jsonl (same
cheap-append, corrupt-line-tolerant JSONL convention as market_reliability.py's log) and the
current mode state rides in the son's own checkpoint (checkpoint["recovery"], wiped by
full_reset like all other search state).
"""
import json
import threading
import time
from pathlib import Path

from . import adaptive as adaptive_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_FILE = DATA_DIR / "recovery_log.jsonl"

# Rounds stuck PAST sim_loop.STUCK_THRESHOLD_ROUNDS before Recovery Mode engages -- one full
# NUDGE_DURATION_ROUNDS window plus slack, so the existing gentler remedy always gets first shot.
ENTER_AFTER_EXTRA_ROUNDS = 8
ESCALATE_EVERY_ROUNDS = 5     # rounds at one level before escalating (or perturbing, at max)
MAX_LEVEL = 4

# Per-level multiplicative pressure (applied at level L as 1 +/- STEP*L, clamped below).
MUTATION_STEP = 0.30      # mutation_strength  *= 1 + 0.30*L  (more mutation of known bests)
EXPLORATION_STEP = 0.25   # exploration_rate   *= 1 + 0.25*L  (wider candidate pool)
NOVELTY_STEP = 0.50       # novelty weighting  *= 1 + 0.50*L  (UCB bonus counts for more)
EXPLOIT_STEP = 0.15       # memory_weight      *= 1 - 0.15*L  (trust the model less)
DIVERSITY_STEP = 0.30     # temperature        *= 1 + 0.30*L  (flatter softmax selection)

PERTURB_SIGMA = 0.18      # stddev of the phase-2 independent per-param Gaussian move --
# deliberately larger than adaptive.PERTURB_SIGMA's routine 0.12 tweak: routine tuning already
# failed here, a bolder (but still bounds-clamped) move is the point.

MAX_LOG_RETURNED = 500

_log_lock = threading.Lock()


def initial_state() -> dict:
    return {"active": False, "level": 0, "rounds_at_level": 0, "perturbations": 0,
            "entered_at": None, "entered_round": None, "last_event": None}


def ensure_state(cp: dict) -> dict:
    """Initializes/backfills checkpoint["recovery"] -- same lazy-backfill convention as
    adaptive.ensure_state, cheap after the first call."""
    if not cp.get("recovery"):
        cp["recovery"] = initial_state()
    else:
        for k, v in initial_state().items():
            cp["recovery"].setdefault(k, v)
    return cp["recovery"]


def _log_event(son_id: int, event: str, rec: dict, cp: dict, details: dict | None = None) -> None:
    entry = {"son_id": son_id, "event": event, "level": rec["level"], "round": cp.get("round"),
             "rounds_since_improvement": cp.get("rounds_since_improvement"),
             "perturbations": rec["perturbations"], "at": time.time()}
    if details:
        entry["details"] = details
    rec["last_event"] = {"event": event, "at": entry["at"], "level": rec["level"]}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def list_log(limit: int = MAX_LOG_RETURNED) -> list[dict]:
    """Most recent events first -- same skip-corrupt-lines convention as
    market_reliability.list_log."""
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


def apply(live: dict, rec: dict, stuck_threshold: int) -> dict:
    """Returns the search params run_round should ACTUALLY use this round: `live` (adaptive.py's
    self-tuned values) with this son's current recovery-level pressure layered on top, plus two
    extra derived keys the caller applies itself:
      - "novelty_multiplier": scales the UCB novelty bonus in candidate selection
      - "temperature_multiplier": scales the softmax temperature (search diversity)
    Multipliers only -- never mutates `live` or the checkpoint (see module docstring's
    "instantly restores itself on exit" guarantee). Inactive mode returns the params untouched
    with both multipliers at 1.0."""
    out = dict(live)
    if not rec.get("active"):
        out["novelty_multiplier"] = 1.0
        out["temperature_multiplier"] = 1.0
        return out
    level = rec["level"]
    out["mutation_strength"] = min(1.0, live["mutation_strength"] * (1 + MUTATION_STEP * level))
    out["exploration_rate"] = min(1.0, live["exploration_rate"] * (1 + EXPLORATION_STEP * level))
    out["memory_weight"] = max(adaptive_mod.PARAM_FLOOR,
                                live["memory_weight"] * (1 - EXPLOIT_STEP * level))
    out["novelty_multiplier"] = 1.0 + NOVELTY_STEP * level
    out["temperature_multiplier"] = 1.0 + DIVERSITY_STEP * level
    return out


def end_round(son_id: int, cp: dict, profile, rng) -> str | None:
    """Called once per completed round from sim_loop.run_round's end-of-round bookkeeping
    (AFTER rounds_since_improvement was updated for this round, under the son's own lock, so
    the checkpoint save that follows persists whatever this changes). Advances the Recovery
    Mode state machine; returns the event that fired ("entered"/"escalated"/"perturbed"/
    "exited") or None. The caller does NOT need to act on the return value -- it's for logging
    symmetry with adaptive.end_round."""
    rec = ensure_state(cp)
    stalled = cp["rounds_since_improvement"]

    # Exit trigger: ANY real improvement (sim_loop just reset the counter to 0). Immediate,
    # per spec -- multiplier pressure vanishes with the flag, nothing else to restore.
    if rec["active"] and stalled == 0:
        _log_event(son_id, "exited", rec, cp)
        cp["recovery"] = initial_state()
        cp["recovery"]["last_event"] = {"event": "exited", "at": time.time(), "level": 0}
        return "exited"

    if not rec["active"]:
        from . import sim_loop as sim_loop_mod  # local import: sim_loop imports this module
        if stalled >= sim_loop_mod.STUCK_THRESHOLD_ROUNDS + ENTER_AFTER_EXTRA_ROUNDS:
            rec["active"] = True
            rec["level"] = 1
            rec["rounds_at_level"] = 0
            rec["perturbations"] = 0
            rec["entered_at"] = time.time()
            rec["entered_round"] = cp["round"]
            _log_event(son_id, "entered", rec, cp)
            return "entered"
        return None

    rec["rounds_at_level"] += 1
    if rec["rounds_at_level"] < ESCALATE_EVERY_ROUNDS:
        return None
    rec["rounds_at_level"] = 0

    if rec["level"] < MAX_LEVEL:
        rec["level"] += 1
        _log_event(son_id, "escalated", rec, cp)
        return "escalated"

    # Phase 2: still stuck at max escalation -- one controlled, independent Gaussian move per
    # adopted param, clamped to this agent's own safety bounds (adaptive.bounds_for). Applied
    # to the ADOPTED params (a real move, see module docstring), and any in-flight A/B trial is
    # cancelled so adaptive.py re-baselines from the new operating point instead of comparing
    # a trial against a baseline that no longer exists.
    adaptive = cp.get("adaptive")
    if adaptive and adaptive.get("params"):
        bounds = adaptive_mod.bounds_for(profile)
        before = dict(adaptive["params"])
        moved = {}
        for param in adaptive_mod.ADAPTIVE_PARAMS:
            lo, hi = bounds[param]
            moved[param] = min(max(adaptive["params"][param] + rng.gauss(0.0, PERTURB_SIGMA), lo), hi)
        adaptive["params"] = moved
        adaptive["trial_params"] = None
        adaptive["phase"] = "baseline"
        adaptive["baseline_reward"] = None
        rec["perturbations"] += 1
        _log_event(son_id, "perturbed", rec, cp,
                    details={"before": before, "after": moved})
        return "perturbed"
    return None


def summary(rec: dict | None) -> dict:
    """JSON-safe view for sim_loop.snapshot() -> the dashboard's per-agent recovery line."""
    rec = rec or initial_state()
    return {"active": rec.get("active", False), "level": rec.get("level", 0),
            "perturbations": rec.get("perturbations", 0), "entered_at": rec.get("entered_at"),
            "last_event": rec.get("last_event")}
