"""Event-driven background loop that automatically retrains learning.py's ROI model whenever
enough NEW sold outcomes have accumulated since the last training run.

Deliberately NOT a busy restart-loop on unchanged data: HistGradientBoostingRegressor with a
fixed random_state, fit on the exact same rows, always converges to the same model -- retrying
that "training" repeatedly would waste CPU and produce a byte-identical result. Instead this
polls cheaply (just reads outcomes.stats(), no sklearn import) and only actually retrains once
RETRAIN_TRIGGER_DELTA new sold outcomes exist since the model was last fit, which is the "when
finished, save the info, then train again" behavior gated on real new data actually existing.

Also piggybacks dead_skin.py's WEEKLY dead-skin score review on this same poll tick (see
dead_skin.review_due()/run_weekly_review()) -- reusing this loop's existing cheap-poll,
real-work-only-when-due shape instead of spinning up a second background thread just for a
once-a-week check. The review itself makes zero live network calls (it only re-derives scores
from whatever's already in the price cache), so running it inline here adds no real cost.

Same reuse for market_reliability.py's periodic recheck (gated by its own
recheck_due()/RECHECK_POLL_INTERVAL_SECONDS, see that module) -- a fourth background thread
was never needed for any of these "just re-derive from what's already on disk, every so often"
jobs.
"""
import threading

from . import dead_skin as dead_skin_mod
from . import learning as learning_mod
from . import market_reliability as market_reliability_mod
from . import outcomes as outcomes_mod

RETRAIN_TRIGGER_DELTA = 5  # retrain once this many new sold outcomes have appeared since the last training
POLL_INTERVAL_SECONDS = 300


class RetrainLoopState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | watching | training | paused | stopped
        self.last_result = None  # learning.TrainResult | None, from the most recent train() call
        self.last_dead_skin_review = None  # dict | None, from the most recent dead_skin.run_weekly_review()
        self.last_market_reliability_recheck = None  # dict | None, from the most recent
        # market_reliability.run_periodic_recheck()
        self.error = None  # last poll-tick exception message, if any -- see retrain_loop()'s
        # own try/except for why this exists (mirrors sim_loop.SimulationLoopState.error).
        # stop_event is a genuine thread-terminating stop (used by the CLI's Ctrl+C, where the
        # process exits anyway). The web dashboard's Start/Stop buttons use pause_event instead,
        # so the one background thread started at server boot can be paused/resumed indefinitely
        # without ever needing to spawn a replacement thread.
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

    def snapshot(self) -> dict:
        with self.lock:
            status = self.status
            last_result = self.last_result
            last_dead_skin_review = self.last_dead_skin_review
            last_market_reliability_recheck = self.last_market_reliability_recheck
            error = self.error
        model_status = learning_mod.get_model_status()
        n_sold = outcomes_mod.stats()["n_sold"]
        if not model_status["trained"]:
            needed = max(0, learning_mod.MIN_TRAINING_SAMPLES - n_sold)
        else:
            needed = max(0, RETRAIN_TRIGGER_DELTA - (n_sold - model_status.get("n_samples", 0)))
        return {
            "status": status,
            "error": error,
            "model_trained": model_status["trained"],
            "model_n_samples": model_status.get("n_samples"),
            "model_trained_at": model_status.get("trained_at"),
            "model_validation_mae": model_status.get("validation_mae"),
            "n_sold_outcomes": n_sold,
            "outcomes_needed_to_retrain": needed,
            "last_train_result": (
                {"trained": last_result.trained, "reason": last_result.reason,
                 "n_samples": last_result.n_samples, "validation_mae": last_result.validation_mae}
                if last_result else None
            ),
            "dead_skin_review_due": dead_skin_mod.review_due(),
            "last_dead_skin_review": last_dead_skin_review,
            "market_reliability_recheck_due": market_reliability_mod.recheck_due(),
            "last_market_reliability_recheck": last_market_reliability_recheck,
        }


def _should_retrain(model_status: dict, n_sold: int) -> bool:
    if n_sold < learning_mod.MIN_TRAINING_SAMPLES:
        return False
    if not model_status["trained"]:
        return True
    return (n_sold - model_status.get("n_samples", 0)) >= RETRAIN_TRIGGER_DELTA


def retrain_loop(state: RetrainLoopState, poll_interval: float = POLL_INTERVAL_SECONDS):
    """Polls outcomes.stats() every poll_interval seconds; retrains the moment enough new sold
    outcomes have appeared since the model was last trained. Runs until stop_event is set
    (CLI/process exit); pause_event (web dashboard's Start/Pause/Stop) just idles the same
    thread indefinitely without terminating it, so the dashboard never needs to respawn one."""
    while True:
        if state.stop_event.is_set():
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return
        if state.pause_event.is_set():
            with state.lock:
                state.status = "paused"
            # Wake on either a pause-event clear+poll or the stop_event -- wait on stop_event
            # with a short timeout so a Stop while paused is still noticed promptly.
            while state.pause_event.is_set() and not state.stop_event.is_set():
                state.stop_event.wait(timeout=1.0)
            continue
        try:
            with state.lock:
                state.status = "watching"
            model_status = learning_mod.get_model_status()
            n_sold = outcomes_mod.stats()["n_sold"]
            if _should_retrain(model_status, n_sold):
                with state.lock:
                    state.status = "training"
                result = learning_mod.train()
                with state.lock:
                    state.last_result = result
                    state.status = "watching"
            if dead_skin_mod.review_due():
                with state.lock:
                    state.last_dead_skin_review = dead_skin_mod.run_weekly_review()
            if market_reliability_mod.recheck_due():
                with state.lock:
                    state.last_market_reliability_recheck = market_reliability_mod.run_periodic_recheck()
            with state.lock:
                state.error = None
        except Exception as e:
            # Never let one bad poll tick (e.g. a transient OSError from a locked state file --
            # this has actually happened, see market_reliability._save_state_store) permanently
            # kill this daemon thread the way it silently did before -- same self-healing
            # error-surfacing convention as sim_loop.sim_loop().
            with state.lock:
                state.status = "error"
                state.error = f"{type(e).__name__}: {e}"
        if state.stop_event.wait(timeout=poll_interval):
            with state.lock:
                state.status = "stopped"
                state.stop_event.clear()
            return


def run_foreground(poll_interval: float = POLL_INTERVAL_SECONDS):
    """CLI entry point (`python -m src.main retrain-loop`) -- runs in the current thread,
    Ctrl+C to stop."""
    state = RetrainLoopState()
    print(f"Watching for new logged outcomes -- retrains automatically once "
          f"{RETRAIN_TRIGGER_DELTA}+ new sold outcomes accumulate since the last training "
          f"(minimum {learning_mod.MIN_TRAINING_SAMPLES} total logged before the first train). "
          f"Polling every {poll_interval:.0f}s. Ctrl+C to stop.\n")
    try:
        retrain_loop(state, poll_interval=poll_interval)
    except KeyboardInterrupt:
        state.stop_event.set()
        print("\nStopped.")
        return
