"""Standalone bulk price-scan mode: walks every skin/wear/StatTrak combo that could ever
appear as a trade-up input or output (the "tradeable subset" -- every skin in a collection,
both StatTrak and non-StatTrak where it exists, every wear it has) and warms
steam_prices.py's cache + data/price_history.jsonl, without doing any trade-up math at all.

Run this BEFORE simulate.py/sim_loop.py's guided random simulation to give it a head start --
simulate.py now falls back to a live steam_prices.get_price() call on its own when a candidate
needs a skin/wear this hasn't warmed yet, so a cold cache no longer makes most candidates
invalid, but a pre-warmed one still means far fewer live fetches (and faster rounds) once the
population starts running. Reuses
steam_prices.get_price()'s existing adaptive rate limiter/retry/cache logic completely
unchanged -- this module only decides WHAT to price, never how to be polite about it.

This is explicitly a separate mode from `tradeups`/`covert` scanning, not a replacement:
running it and simulate-loop/tradeups at the same time means both would compete for the same
Steam rate limiter, so prefer running them one at a time.
"""
import json
import threading
import time
from pathlib import Path

from . import data as data_mod
from . import invalid_skins as invalid_skins_mod
from .steam_prices import get_price, market_hash_name, flush_cache, set_attribution

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHECKPOINT_FILE = DATA_DIR / "price_scan_checkpoint.json"


def _empty_checkpoint() -> dict:
    return {"index": 0, "n_priced": 0, "updated_at": None}


def load_checkpoint() -> dict:
    if not CHECKPOINT_FILE.exists():
        return _empty_checkpoint()
    try:
        cp = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_checkpoint()
    for k, v in _empty_checkpoint().items():
        cp.setdefault(k, v)
    return cp


def save_checkpoint(cp: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cp["updated_at"] = time.time()
    CHECKPOINT_FILE.write_text(json.dumps(cp), encoding="utf-8")


def _collection_priority_weights() -> dict:
    """Collection -> how many of the 4 population sons' CURRENT best-known checkpoint entries
    (data/sons/son_N/checkpoint.json) have it as their primary_collection -- a collection
    several sons have already found profitable is more useful to keep freshly priced than one
    nobody's landed on yet. Lazy import: sim_loop.py doesn't import price_scan.py, so this is a
    safe one-directional dependency, and keeping it lazy means a plain price-scan run never pays
    for importing the whole simulation-population module graph unless prioritize=True is
    actually used. Pure disk reads (checkpoint.json), no network. Empty dict (matching today's
    flat sweep order) if no son has any best-known entries yet."""
    from . import sim_loop as sim_loop_mod
    weights: dict = {}
    for son_id in range(sim_loop_mod.N_SONS):
        cp = sim_loop_mod.load_checkpoint(sim_loop_mod.son_checkpoint_file(son_id))
        for entry in cp.get("best", []):
            coll = entry.get("primary_collection", "")
            if coll:
                weights[coll] = weights.get(coll, 0) + 1
    return weights


def build_target_list(collection_filter: str | None = None, prioritize: bool = False) -> list:
    """(skin_name, wear, stattrak) triples for every skin that appears in some collection at
    a tradeable rarity -- exactly the population tradeup.py/random_trade.py can ever touch
    (never the full upstream catalog, e.g. skins with no collection are already excluded by
    data.load_skins() itself). Deduplicated: the same skin can appear in more than one
    collection's index.

    prioritize=True stable-sorts this (still-complete) list so skin/wear/StatTrak triples
    belonging to a collection the guided-simulation population has already banked a best-known
    find in (see _collection_priority_weights) come first -- the bulk scan increasingly
    front-loads what sim_loop.py's sons actually want warmed next instead of a flat sweep, per
    the guided-simulation session's own goal, without ever DROPPING a collection from the sweep
    (a full pass still eventually covers everything -- this only reorders). Ties keep the
    original deterministic order (Python's sort is stable). False (the default) reproduces the
    exact original flat-sweep order -- this is opt-in, since PriceScanState.checkpoint["index"]
    is a positional index into this list (see run_scan), and toggling prioritize mid-scan shifts
    what that index points at exactly like changing collection_filter already does today (see
    web/server.py's /api/price-scan/config, which resets index to 0 on either change)."""
    skins = data_mod.load_skins()
    if collection_filter:
        skins = [s for s in skins if any(collection_filter.lower() in c.lower() for c in s["collections"])]
    seen = set()
    targets = []
    skin_collections: dict = {}
    for skin in skins:
        skin_collections[skin["name"]] = skin.get("collections", [])
        for wear in skin["wears"]:
            for stattrak in ((False, True) if skin["stattrak"] else (False,)):
                key = (skin["name"], wear, stattrak)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(key)
    if prioritize:
        weights = _collection_priority_weights()
        if weights:
            def _priority(target):
                colls = skin_collections.get(target[0], [])
                return -max((weights.get(c, 0) for c in colls), default=0)
            targets.sort(key=_priority)
    return targets


class PriceScanState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"  # idle | running | paused | stopped
        self.progress = {"current": 0, "total": 0}
        self.checkpoint = load_checkpoint()
        self.config = {"collection": "", "prioritize": False}  # prioritize: see
        # build_target_list -- opt-in, off by default (preserves the original flat-sweep order/
        # checkpoint-index semantics for anyone not explicitly asking for it)
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

    def abort_requested(self) -> bool:
        return self.stop_event.is_set() or self.pause_event.is_set()

    def snapshot(self) -> dict:
        with self.lock:
            cp = self.checkpoint
            return {"status": self.status, "progress": dict(self.progress),
                    "n_priced": cp["n_priced"], "updated_at": cp["updated_at"]}


def run_scan(state: PriceScanState, currency: int = 1):
    set_attribution("price_scan")  # see steam_prices.set_attribution -- breaks this thread's
    # live-fetch stats out from the population sons' in get_stats()["by_label"]
    with state.lock:
        state.status = "running"
        collection_filter = state.config.get("collection", "")
        prioritize = state.config.get("prioritize", False)
        start_index = state.checkpoint["index"]

    targets = build_target_list(collection_filter, prioritize=prioritize)

    for i, (skin_name, wear, stattrak) in enumerate(targets):
        if i < start_index:
            continue
        if state.stop_event.is_set():
            with state.lock:
                state.checkpoint["index"] = 0
                save_checkpoint(state.checkpoint)
                state.status = "stopped"
            flush_cache()
            return
        if state.pause_event.is_set():
            with state.lock:
                state.checkpoint["index"] = i
                save_checkpoint(state.checkpoint)
                state.status = "paused"
            flush_cache()
            return
        with state.lock:
            state.progress = {"current": i + 1, "total": len(targets)}
        name = market_hash_name(skin_name, wear, stattrak)
        price = get_price(name, currency, state.abort_requested)
        if price is not None:
            with state.lock:
                state.checkpoint["n_priced"] += 1
            # Heal a prior invalid mark the moment this bulk warm actually finds a price --
            # simulate.py was the only caller that ever did this, so a skin invalidated during a
            # bad run could sit marked for the full INVALID_RECHECK_SECONDS window even after
            # this scan re-priced it successfully, and random_trade.py would keep excluding it
            # from candidates in the meantime for no reason.
            invalid_skins_mod.clear_name(name, currency)

    with state.lock:
        state.checkpoint["index"] = 0
        save_checkpoint(state.checkpoint)
        state.status = "idle"
    flush_cache()


def scan_loop(state: PriceScanState, currency: int = 1):
    """Web-dashboard-style loop: waits for a Start/Resume trigger, runs one full pass, then
    waits again -- mirrors web/server.py's ScanState.scan_loop, no automatic rescanning."""
    state.wake_event.wait()
    state.wake_event.clear()
    while True:
        run_scan(state, currency=currency)
        with state.lock:
            if state.stop_event.is_set():
                state.stop_event.clear()
                state.status = "stopped"
        state.wake_event.wait()
        state.wake_event.clear()


def run_foreground(collection_filter: str = "", currency: int = 1):
    """CLI entry point (`python -m src.main scan-prices`) -- Ctrl+C-safe, checkpointed."""
    state = PriceScanState()
    state.config["collection"] = collection_filter
    targets_n = len(build_target_list(collection_filter))
    print(f"Bulk price scan -- {targets_n} skin/wear/StatTrak combo(s)"
          f"{' matching ' + repr(collection_filter) if collection_filter else ' across all collections'}, "
          f"warms cache/price_cache.json + data/price_history.jsonl only (no trade-up math). "
          f"Resumable -- checkpoint: {CHECKPOINT_FILE}\nCtrl+C to stop.\n")
    try:
        run_scan(state, currency=currency)
    except KeyboardInterrupt:
        state.stop_event.set()
        with state.lock:
            current = state.progress["current"]
            state.checkpoint["index"] = max(0, current - 1)
            save_checkpoint(state.checkpoint)
        flush_cache()
        print("\nStopped -- checkpoint saved.")
        return
    print(f"\nDone -- {state.checkpoint['n_priced']} price(s) fetched/refreshed this run.")
