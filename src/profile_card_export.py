"""Exports a small, DELAYED/ANONYMIZED snapshot of the sim-population's aggregate stats for a
GitHub profile card -- deliberately NOT the real top-N trade-up opportunities the population has
found. Publishing live, actionable opportunities on a public GitHub profile would give away this
project's actual trading edge to anyone watching; aggregate numbers (population-wide averages,
a magnitude-only best-find figure, cumulative counts) are interesting to a profile visitor without
telling them what to buy or when (user decision, 2026-07-12).

Writes a small JSON file this project's data never otherwise needs -- consumed by a GitHub Action
in the profile repo (DanielGodriNeto/DanielGodriNeto) that renders it into an SVG card. This
script does NOT commit or push anything itself; see the module docstring in that repo's workflow
for how the JSON gets there (a separate, manual step, so every push to a public profile is
something the user actually looks at first)."""
import json
import time
from pathlib import Path

from . import sim_loop as sim_loop_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTCOMES_FILE = DATA_DIR / "outcomes.jsonl"

SEVEN_DAYS_SECONDS = 7 * 24 * 3600


def _round_history_entries(son_id: int) -> list[dict]:
    path = sim_loop_mod.son_round_history_file(son_id)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def build_snapshot() -> dict:
    """Real numbers, computed fresh from local data every time this is called -- never cached,
    never guessed. Every field here is deliberately an AGGREGATE or a MAGNITUDE, never a specific
    skin/collection/composition -- see module docstring.

    best_find_verified_roi_pct is deliberately restricted to entries with a real
    verified_roi_pct (Stage-2 Skinport/Buff163/CSFloat cross-check, see price_report.py) --
    NOT the raw simulated expected_roi_pct estimate, and NOT round_history.jsonl's
    best_roi_ever (a historical high-water mark that never gets revised down even after the
    candidate that set it is pruned/blacklisted). User feedback (2026-07-13): an unverified
    number looked wrong on a first pass ("that is wrong it wasn't checked on other sites") --
    publishing an unconfirmed estimate on a public profile is worse than publishing nothing, so
    this is None whenever no current best-list entry across the population has actually been
    verified yet, rather than falling back to a guess."""
    now = time.time()
    all_rois_7d: list[float] = []
    best_verified_roi: float | None = None
    total_simulated = 0

    for son_id in range(sim_loop_mod.N_SONS):
        cp = sim_loop_mod.load_checkpoint(sim_loop_mod.son_checkpoint_file(son_id))
        total_simulated += cp.get("n_simulated", 0)
        for entry in cp.get("best") or []:
            verified = entry.get("verified_roi_pct")
            if verified is not None and (best_verified_roi is None or verified > best_verified_roi):
                best_verified_roi = verified
        for entry in _round_history_entries(son_id):
            at = entry.get("at")
            roi = entry.get("avg_roi_pct")
            if at is None:
                continue
            if roi is not None and (now - at) <= SEVEN_DAYS_SECONDS:
                all_rois_7d.append(roi)

    n_outcomes = 0
    if OUTCOMES_FILE.exists():
        with open(OUTCOMES_FILE, encoding="utf-8") as f:
            n_outcomes = sum(1 for line in f if line.strip())

    return {
        "generated_at": now,
        "active_agents": sim_loop_mod.N_SONS,
        "population_avg_roi_pct_7d": (sum(all_rois_7d) / len(all_rois_7d)) if all_rois_7d else None,
        "best_find_verified_roi_pct": best_verified_roi,  # None until something's actually
        # been cross-checked -- see docstring above; magnitude only, no skin/collection identity
        "total_candidates_scanned": total_simulated,
        "real_outcomes_logged": n_outcomes,
    }


def write_snapshot(path: Path) -> dict:
    snapshot = build_snapshot()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return snapshot
