"""Local web dashboard for the CS2 trade-up / anomaly scanner.

Runs a background thread that re-scans on a timer and a small stdlib-only HTTP
server (no Flask/etc.) that serves a dashboard showing the latest results. This
is a READ-ONLY scanner, same as the CLI: it never logs into Steam, places
orders, or touches an inventory -- see ../README.md for why.

Run with:  python web/server.py   (from the cs2-trade-calculator directory)
Then open: http://127.0.0.1:5000
"""
import csv
import io
import json
import sys
import threading
import time
import zipfile
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import data as data_mod
from src import tradeup as tradeup_mod
from src import anomalies as anomalies_mod
from src import covert as covert_mod
from src.steam_prices import CURRENCIES
from src import steam_prices
from src import outcomes as outcomes_mod
from src import learning as learning_mod
from src import retrain_loop as retrain_loop_mod
from src import thirdparty_prices as thirdparty_prices_mod
from src import price_report as price_report_mod
from src import price_history as price_history_mod
from src import oddsmath as oddsmath_mod
from src import data_archive as data_archive_mod
from src import price_scan as price_scan_mod
from src import sim_loop as sim_loop_mod
from src import sim_learning as sim_learning_mod
from src import ml_common as ml_common_mod
from src import agents as agents_mod
from src import adaptive as adaptive_mod
from src import dead_skin as dead_skin_mod
from src import invalid_skins as invalid_skins_mod
from src import mispriced_skins as mispriced_skins_mod
from src import wear_anomaly as wear_anomaly_mod
from src import market_reliability as market_reliability_mod
from src import tradability as tradability_mod
from src import rules_engine as rules_engine_mod
from src import random_trade as random_trade_mod
from src import simulate as simulate_mod
from src import verify_queue as verify_queue_mod
from src import sim_blacklist as sim_blacklist_mod
from src import user_overrides as user_overrides_mod
from src import xlsx_export as xlsx_export_mod
from src import pattern_db as pattern_db_mod
from src import pattern_scanner as pattern_scanner_mod
from src import backup_engine as backup_engine_mod
from src import connections_config as connections_config_mod
from src import alerts as alerts_mod
from src import watch_loop as watch_loop_mod
import tempfile

STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = 5000


def _compute_app_version() -> dict:
    """Git commit short-hash + date this running server was actually started from, computed
    ONCE at import time -- a server restart is already required to pick up any code change at
    all (this project's Python-doesn't-hot-reload convention), so there's no reason to re-shell
    out to git on every request. Falls back to "unknown"/None if git isn't available (e.g. a
    deployment without a .git directory) rather than failing server startup over a display-only
    feature."""
    import subprocess
    repo_dir = Path(__file__).resolve().parent.parent

    def _git(*args) -> str:
        result = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True,
                                 text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""

    try:
        commit = _git("rev-parse", "--short", "HEAD")
        date = _git("log", "-1", "--format=%cd", "--date=short")
        dirty = bool(_git("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "date": None, "dirty": False}
    return {"commit": commit or "unknown", "date": date or None, "dirty": dirty}


APP_VERSION = _compute_app_version()

_universe_size_cache = None  # lazily computed, then cached for the process lifetime -- the
# skin database doesn't change while the server runs, so there's no reason to rebuild
# price_scan.build_target_list()'s full (skin, wear, stattrak) universe on every top-bar poll


def _universe_size() -> int:
    global _universe_size_cache
    if _universe_size_cache is None:
        _universe_size_cache = len(price_scan_mod.build_target_list())
    return _universe_size_cache

CONFIG_KEYS = ("collection", "stattrak", "min_roi", "anomaly_min_gap_pct",
               "anomaly_min_volume", "currency", "fast_mode", "skip_anomalies",
               "float_strategy", "mixed_strategy", "strategy_any_roi", "scan_covert", "case",
               "check_thirdparty", "use_ml")


class ScanState:
    def __init__(self):
        self.lock = threading.Lock()
        self.tradeups: list = []
        self.anomalies: list = []
        self.covert: list = []
        self.status = "idle"  # starting | running | idle | paused | stopped | error
        self.progress = {"stage": "", "current": 0, "total": 0, "label": ""}
        self.last_run = None
        self.last_duration = None
        self.error = None
        self.config = {
            "collection": "",
            "stattrak": False,
            "min_roi": 0.0,
            "anomaly_min_gap_pct": 15.0,
            "anomaly_min_volume": 5,
            "currency": 1,
            "fast_mode": False,
            "skip_anomalies": False,
            "float_strategy": False,
            "mixed_strategy": False,
            "strategy_any_roi": False,  # opt-in: run float/mixed/mixed-float strategies on every
            # result, not just ones whose plain ROI is already >= 0 -- many more requests
            "scan_covert": False,  # opt-in: Covert -> knife/glove needs its own case data + extra requests
            "case": "",  # substring filter for the Covert -> knife/glove scan (case names, not collections)
            "check_thirdparty": False,  # opt-in: cross-check Steam prices against Skinport/CSFloat
            # to flag a "drought" (Steam priced anomalously high) -- see src/thirdparty_prices.py
            "use_ml": False,  # opt-in: consult the trained ROI model (src/learning.py) as a tiebreaker
            # signal -- no-op until enough outcomes have been logged and train-model has run
        }
        self.wake_event = threading.Event()  # "restart now with current config" (scan-now / config change)
        self.stop_event = threading.Event()  # "stop and discard progress" -- doesn't auto-restart
        self.pause_event = threading.Event()  # "pause but remember where we were" -- resumable
        # Resume bookkeeping: which collection to pick back up at, and whatever results were
        # already found before pausing (only meaningful while paused; reset on any fresh start).
        self.resume_collection_index = 0
        self.partial_tradeup_results: list = []

    def abort_requested(self) -> bool:
        """A restart, a deliberate stop, or a pause should all interrupt an in-progress
        price lookup -- which one it was is sorted out by the caller in run_scan."""
        return self.wake_event.is_set() or self.stop_event.is_set() or self.pause_event.is_set()

    def reset_resume_state(self):
        """Call whenever a scan should start completely fresh (new scope/settings, explicit
        Scan now, or Stop) rather than continuing a previously paused pass."""
        self.resume_collection_index = 0
        self.partial_tradeup_results = []

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "progress": dict(self.progress),
                "last_run": self.last_run,
                "last_duration": self.last_duration,
                "error": self.error,
                "config": dict(self.config),
                "tradeup_count": len(self.tradeups),
                "anomaly_count": len(self.anomalies),
                "covert_count": len(self.covert),
                "can_resume": self.status == "paused",
            }


def run_scan(state: ScanState):
    with state.lock:
        state.status = "running"
        state.error = None
        cfg = dict(state.config)
        start_index = state.resume_collection_index
        tradeup_results = list(state.partial_tradeup_results)
    start = time.time()
    try:
        skins = data_mod.load_skins()
        idx = data_mod.collections_index(skins)
        scope = [c for c in idx if not cfg["collection"] or cfg["collection"].lower() in c.lower()]

        for i, collection in enumerate(scope, 1):
            if i <= start_index:
                continue  # already scanned in a previous pass before this one was paused
            if state.wake_event.is_set() or state.stop_event.is_set():
                with state.lock:
                    state.reset_resume_state()
                return  # scope/settings changed, or stopped -- abandon this pass, discard resume point
            if state.pause_event.is_set():
                with state.lock:
                    state.resume_collection_index = i - 1
                    state.partial_tradeup_results = tradeup_results
                    state.status = "paused"
                steam_prices.flush_cache()
                return
            with state.lock:
                state.progress = {"stage": "tradeups", "current": i, "total": len(scope), "label": collection}
            collection_results = tradeup_mod.scan_collection(
                collection, idx[collection], cfg["stattrak"], cfg["currency"],
                abort_check=state.abort_requested, fast_mode=cfg["fast_mode"],
                float_strategy=cfg["float_strategy"], min_roi=cfg["min_roi"],
                mixed_strategy=cfg["mixed_strategy"], full_idx=idx,
                strategy_any_roi=cfg["strategy_any_roi"], check_thirdparty=cfg["check_thirdparty"],
                use_ml=cfg["use_ml"])
            if state.pause_event.is_set() and not (state.wake_event.is_set() or state.stop_event.is_set()):
                # scan_collection bailed out mid-way when the pause hit -- that collection's
                # results are incomplete, so discard them and redo the whole collection on resume
                with state.lock:
                    state.resume_collection_index = i - 1
                    state.partial_tradeup_results = tradeup_results
                    state.status = "paused"
                steam_prices.flush_cache()
                return
            if state.wake_event.is_set() or state.stop_event.is_set():
                with state.lock:
                    state.reset_resume_state()
                return
            tradeup_results.extend(collection_results)
            # publish partial results after every collection so a full (all-collections) scan is
            # useful to watch live instead of showing nothing until it finishes hours later
            visible = sorted([r for r in tradeup_results if r.roi_pct >= cfg["min_roi"]],
                              key=lambda r: r.roi_pct, reverse=True)
            with state.lock:
                state.tradeups = visible

        with state.lock:
            state.reset_resume_state()  # tradeups phase is fully done -- nothing left to resume there
        steam_prices.flush_cache()

        if cfg["skip_anomalies"]:
            with state.lock:
                state.anomalies = []
        else:
            flat_skins = [s for s in skins
                          if not cfg["collection"] or any(cfg["collection"].lower() in c.lower() for c in s["collections"])]
            anomaly_results = []
            chunk = 25
            for i in range(0, len(flat_skins), chunk):
                if state.abort_requested():
                    if state.pause_event.is_set() and not (state.wake_event.is_set() or state.stop_event.is_set()):
                        with state.lock:
                            state.status = "paused"
                        steam_prices.flush_cache()
                    return
                part = flat_skins[i:i + chunk]
                with state.lock:
                    state.progress = {"stage": "anomalies", "current": min(i + chunk, len(flat_skins)),
                                       "total": len(flat_skins), "label": ""}
                anomaly_results.extend(
                    anomalies_mod.scan_skins(part, cfg["currency"], cfg["anomaly_min_gap_pct"], cfg["anomaly_min_volume"],
                                              abort_check=state.abort_requested, fast_mode=cfg["fast_mode"],
                                              stattrak=cfg["stattrak"])
                )
                with state.lock:
                    state.anomalies = sorted(anomaly_results, key=lambda a: a.profit_after_fee, reverse=True)
        steam_prices.flush_cache()

        if not cfg["scan_covert"]:
            with state.lock:
                state.covert = []
        else:
            if state.abort_requested():
                if state.pause_event.is_set() and not (state.wake_event.is_set() or state.stop_event.is_set()):
                    with state.lock:
                        state.status = "paused"
                    steam_prices.flush_cache()
                return
            with state.lock:
                state.progress = {"stage": "covert", "current": 0, "total": 1, "label": "Covert -> knife/glove"}
            knives_gloves = data_mod.load_knives_gloves()
            cases_idx = data_mod.cases_index(skins, knives_gloves)
            covert_results = covert_mod.scan_all(
                cases_idx, stattrak=cfg["stattrak"], currency=cfg["currency"], only_case=cfg["case"],
                fast_mode=cfg["fast_mode"], abort_check=state.abort_requested,
                float_strategy=cfg["float_strategy"], min_roi=cfg["min_roi"], mixed_strategy=cfg["mixed_strategy"],
                strategy_any_roi=cfg["strategy_any_roi"])
            if state.pause_event.is_set() and not (state.wake_event.is_set() or state.stop_event.is_set()):
                with state.lock:
                    state.status = "paused"
                steam_prices.flush_cache()
                return
            with state.lock:
                state.covert = [r for r in covert_results if r.roi_pct >= cfg["min_roi"]]

        with state.lock:
            state.status = "idle"
            state.last_run = time.time()
            state.last_duration = time.time() - start
            state.progress = {"stage": "", "current": 0, "total": 0, "label": ""}
        steam_prices.flush_cache()
    except Exception as e:  # keep the background loop alive even if a scan fails
        with state.lock:
            state.status = "error"
            state.error = str(e)
        steam_prices.flush_cache()


def scan_loop(state: ScanState):
    """No automatic rescanning -- a scan runs once (when triggered by Scan now, Continue, or
    a config change) and then this thread just waits indefinitely for the next trigger. This
    includes the very FIRST run: the thread waits on wake_event before ever calling run_scan,
    so nothing scans just because the server process started (previously it did, silently
    kicking off a full ~90-collection scan on every boot -- contradicted this module's own
    docstring above)."""
    state.wake_event.wait()
    state.wake_event.clear()
    while True:
        run_scan(state)
        with state.lock:
            stopped = state.stop_event.is_set()
            if stopped:
                state.stop_event.clear()
                state.status = "stopped"
                state.progress = {"stage": "", "current": 0, "total": 0, "label": ""}
        state.wake_event.wait()  # stay put until scan-now, resume, or a config change wakes us
        state.wake_event.clear()


RATE_LIMIT_AUTO_RETRY_CHECK_SECONDS = 30
RATE_LIMIT_AUTO_RETRY_AFTER_SECONDS = 5 * 60  # matches the dashboard's VPN-warning threshold


def rate_limit_watchdog(state: ScanState):
    """This is the one exception to 'no automatic rescanning': if Steam has refused every
    request for 5+ minutes straight, there's otherwise no way to get unstuck without a human
    clicking something. So this retries automatically every 5 minutes for as long as that
    streak continues, and stops the moment a single request succeeds (the streak clears in
    steam_prices, so this naturally goes quiet again -- it isn't a general recurring rescan).

    IMPORTANT: a scan that's actively "running" is NOT stuck -- it's just slow, and
    steam_prices.get_price() already retries/backs off per-request on its own. This must never
    interrupt or discard progress from an in-progress pass (a real bug this project hit: a long
    scan across many collections would get silently killed and restarted from scratch partway
    through, so it could run "forever" without ever finishing). It only nudges a scan that's
    genuinely idle (nothing running to interrupt) -- and even then, resumes in place (like
    clicking Continue) rather than wiping any paused/partial progress (like clicking Scan now
    would) if there is any to preserve."""
    last_auto_retry = 0.0
    while True:
        time.sleep(RATE_LIMIT_AUTO_RETRY_CHECK_SECONDS)
        streak_start = steam_prices.get_stats().get("rate_limit_streak_start")
        if not streak_start:
            continue
        now = time.time()
        if now - streak_start >= RATE_LIMIT_AUTO_RETRY_AFTER_SECONDS and \
                now - last_auto_retry >= RATE_LIMIT_AUTO_RETRY_AFTER_SECONDS:
            with state.lock:
                if state.status == "running":
                    continue  # let the in-progress pass keep going -- do NOT touch it
            last_auto_retry = now
            state.pause_event.clear()
            state.wake_event.set()


DASHBOARD_ALERTS_INTERVAL_SECONDS = 300  # 5 min -- independent of the 3s UI poll


def dashboard_alerts_loop(state: ScanState, stop_event: threading.Event):
    """Lightweight sibling to watch_loop.py's standalone CLI watcher (see that module's own
    docstring/run_cycle) -- reuses its exact cooldown-state file, alert keys, and notify format
    (imported directly, not duplicated) so a user who ALSO runs `python -m src.main watch`
    never gets double-alerted for the same opportunity. Deliberately does NOT re-scan from
    scratch like watch_loop.py's CLI does: it only reads state.tradeups (the dashboard's own
    already-computed scan results, refreshed by whatever scan the user already started) and
    pattern_scanner's already-populated opportunity store -- zero extra Steam/CSFloat calls
    beyond whatever the dashboard's own scans were already making.

    No-ops entirely unless connections_config's ntfy_enabled + ntfy_topic are both set --
    re-checked fresh every tick, so toggling it off in the Connections page takes effect within
    one cycle without a server restart."""
    while not stop_event.wait(DASHBOARD_ALERTS_INTERVAL_SECONDS):
        cfg = connections_config_mod.load_config()
        if not cfg.get("ntfy_enabled") or not cfg.get("ntfy_topic"):
            continue
        roi_threshold = cfg.get("ntfy_roi_threshold_pct", 100.0)
        pattern_threshold = cfg.get("ntfy_pattern_threshold_pct", 100.0)
        alert_state = watch_loop_mod._load_state()
        changed = False

        with state.lock:
            tradeups_snapshot = list(state.tradeups)
        for r in tradeups_snapshot:
            if r.roi_pct < roi_threshold:
                continue
            key = f"tradeup:{r.collection}:{r.from_rarity}:{r.to_rarity}:{r.stattrak}"
            if watch_loop_mod._should_alert(alert_state, key):
                title = f"Trade-up +{r.roi_pct:.0f}% ROI"
                body = (f"{r.collection}: {r.from_rarity} -> {r.to_rarity}"
                        f"{' (StatTrak)' if r.stattrak else ''}\n"
                        f"Cost ${r.cost:.2f} -> ${r.expected_revenue_after_fee:.2f} after fees "
                        f"(profit ${r.profit:+.2f})")
                alerts_mod.notify(watch_loop_mod._ascii(title), watch_loop_mod._ascii(body),
                                   priority="high", tags=["moneybag"])
                watch_loop_mod._mark_alerted(alert_state, key)
                changed = True

        try:
            pattern_opps = pattern_scanner_mod.list_opportunities(sort="profit")
        except Exception:
            pattern_opps = []
        for o in pattern_opps:
            ask = o.get("asking_price")
            profit = o.get("expected_profit")
            if not ask or profit is None:
                continue
            roi = (profit / ask) * 100.0
            if roi < pattern_threshold:
                continue
            key = f"pattern:{o.get('listing_id')}"
            if watch_loop_mod._should_alert(alert_state, key):
                title = f"Rare pattern +{roi:.0f}% under value"
                body = (f"{o.get('weapon')} | {o.get('skin')} ({o.get('wear')}) -- {o.get('pattern_label')}\n"
                        f"Ask ${ask:.2f}, worth ~${o.get('estimated_collector_value'):.2f} "
                        f"on {o.get('marketplace')}")
                alerts_mod.notify(watch_loop_mod._ascii(title), watch_loop_mod._ascii(body),
                                   priority="high", tags=["gem"], click_url=o.get("listing_url"))
                watch_loop_mod._mark_alerted(alert_state, key)
                changed = True

        if changed:
            watch_loop_mod._save_state(alert_state)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Like ThreadingHTTPServer, but doesn't dump a traceback to the console
    every time a browser tab cancels an in-flight poll (ConnectionAbortedError /
    ConnectionResetError) -- that's normal client behavior, not a server bug."""

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_handler(state: ScanState, retrain_state: retrain_loop_mod.RetrainLoopState,
                  price_scan_state: price_scan_mod.PriceScanState,
                  sim_population: list,
                  verify_queue_state: verify_queue_mod.VerifyQueueState,
                  convergence_watcher: "sim_loop_mod.ConvergenceWatcher",
                  round_robin: "sim_loop_mod.RoundRobinScheduler",
                  pattern_scan_state: "pattern_scanner_mod.PatternScanState",
                  backup_worker: "backup_engine_mod.BackupWorkerState"):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str):
            if not path.exists():
                self.send_response(404)
                self.end_headers()
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            if path == "/style.css":
                return self._send_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
            if path == "/api/status":
                return self._send_json(state.snapshot())
            if path == "/api/tradeups":
                with state.lock:
                    rows = [asdict(r) for r in state.tradeups]
                return self._send_json(rows)
            if path == "/api/anomalies":
                with state.lock:
                    rows = [asdict(a) for a in state.anomalies]
                return self._send_json(rows)
            if path == "/api/covert":
                with state.lock:
                    rows = [asdict(r) for r in state.covert]
                return self._send_json(rows)
            if path == "/api/currencies":
                return self._send_json([{"code": code, "name": name, "symbol": symbol}
                                         for code, (name, symbol) in sorted(CURRENCIES.items(), key=lambda kv: kv[1][0])])
            if path == "/api/network-stats":
                stats = steam_prices.get_stats()
                stats["universe_size"] = _universe_size()
                return self._send_json(stats)
            if path == "/api/thirdparty-stats":
                return self._send_json(thirdparty_prices_mod.get_stats())
            if path == "/api/outcomes":
                return self._send_json([asdict(o) for o in outcomes_mod.load_outcomes()])
            if path == "/api/outcomes/stats":
                return self._send_json(outcomes_mod.stats())
            if path == "/api/model-status":
                return self._send_json(learning_mod.get_model_status())
            if path == "/api/retrain-loop/status":
                return self._send_json(retrain_state.snapshot())
            if path == "/api/price-scan/status":
                return self._send_json(price_scan_state.snapshot())
            if path == "/api/sim-population/status":
                return self._send_json([s.snapshot() for s in sim_population])
            if path == "/api/sim-population/best-simulations":
                params = parse_qs(urlparse(self.path).query)
                son_param = params.get("son", [""])[0]
                son_id = int(son_param) if son_param.isdigit() else None
                return self._send_json(sim_loop_mod.list_best_simulations(son_id=son_id))
            if path == "/api/sim-population/round-history":
                # Full lifetime ROI/profit progress per son, read straight from the uncapped
                # round_history.jsonl (see sim_loop.son_round_history_file) -- unlike the
                # checkpoint-backed history the agent-stats chart already uses (capped at the
                # last 300 rounds), this survives as long as the son has run. Loaded on demand by
                # the Results page, not the 3s poll.
                return self._send_json({sid: sim_loop_mod.load_round_history(sid) for sid in range(sim_loop_mod.N_SONS)})
            if path == "/api/sim-population/opportunities/archive":
                # Unlike best-simulations above (checkpoint-backed, capped at BEST_KEPT=50 per
                # son), this reads sim_loop.list_all_opportunities() -- EVERY unique trade-up
                # the population has ever simulated, paginated server-side so the response stays
                # small regardless of how large the archive has grown. Loaded on demand by the
                # dashboard's "Browse all opportunities" panel, not on the 3s poll.
                params = parse_qs(urlparse(self.path).query)
                son_param = params.get("son", [""])[0]
                son_id = int(son_param) if son_param.isdigit() else None
                collection = params.get("collection", [""])[0].strip() or None
                min_roi_param = params.get("min_roi", [""])[0]
                min_roi = float(min_roi_param) if min_roi_param else None
                page = max(1, int(params.get("page", ["1"])[0]) if params.get("page", ["1"])[0].isdigit() else 1)
                page_size_raw = params.get("page_size", ["50"])[0]
                page_size = min(500, max(1, int(page_size_raw) if page_size_raw.isdigit() else 50))
                all_items = sim_loop_mod.list_all_opportunities(son_id=son_id, collection_filter=collection,
                                                                  min_roi=min_roi)
                start = (page - 1) * page_size
                return self._send_json({
                    "total": len(all_items), "page": page, "page_size": page_size,
                    "n_pages": max(1, -(-len(all_items) // page_size)),
                    "items": all_items[start:start + page_size],
                })
            if path == "/api/export/xlsx":
                # Local-file substitute for a "create a Google Sheet" button (see
                # requirements.txt / xlsx_export.py's module docstring for why) -- a single
                # formatted workbook covering the full opportunity archive, every agent's
                # stats/history, leaderboards, and model quality, with a couple of embedded
                # charts. openpyxl is imported lazily inside build_workbook(), so a server
                # without it installed only fails HERE, on an actual export click, not at
                # startup or anywhere else in the dashboard.
                try:
                    data = xlsx_export_mod.build_workbook(sim_population, agents_mod.AGENT_NAMES)
                except ImportError as e:
                    return self._send_json(
                        {"ok": False, "error": f"openpyxl not installed in this Python "
                                                 f"environment ({e}) -- run: pip install openpyxl"}, 500)
                ts = time.strftime("%Y%m%d-%H%M%S")
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f'attachment; filename="cs2-trade-data-{ts}.xlsx"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/sim-population/blacklist":
                return self._send_json(sim_blacklist_mod.list_entries())
            if path == "/api/skin-blacklist":
                return self._send_json(user_overrides_mod.list_blacklist())
            if path == "/api/price-overrides":
                return self._send_json(user_overrides_mod.list_overrides())
            if path == "/api/sim-population/leaderboards":
                return self._send_json({
                    "collections": sim_loop_mod.most_profitable_collections(),
                    "skins": sim_loop_mod.most_profitable_skins(),
                })
            if path == "/api/verify-queue/status":
                return self._send_json(verify_queue_state.snapshot())
            if path == "/api/sim-population/convergence":
                return self._send_json(convergence_watcher.snapshot())
            if path == "/api/sim-population/round-robin/status":
                return self._send_json(round_robin.snapshot())
            if path == "/api/dead-skins":
                params = parse_qs(urlparse(self.path).query)
                cur_param = params.get("currency", ["1"])[0]
                currency = int(cur_param) if cur_param.isdigit() else 1
                return self._send_json(dead_skin_mod.list_dead_skins(currency=currency))
            if path == "/api/invalid-skins":
                params = parse_qs(urlparse(self.path).query)
                cur_param = params.get("currency", ["1"])[0]
                currency = int(cur_param) if cur_param.isdigit() else 1
                return self._send_json(invalid_skins_mod.list_invalid_skins(currency=currency))
            if path == "/api/skins":
                params = parse_qs(urlparse(self.path).query)
                q = params.get("q", [""])[0].strip().lower()
                tradeable_only = params.get("tradeable", ["false"])[0].lower() == "true"
                skins = data_mod.load_skins()
                idx = data_mod.collections_index(skins)
                all_skins = skins + data_mod.load_knives_gloves()
                matches = [s for s in all_skins if not q or q in s["name"].lower()]
                out = []
                for s in matches:
                    ok, reason = tradability_mod.check_skin(s, idx) if "collections" in s else (False, "not_a_tradeup_rarity")
                    if tradeable_only and not ok:
                        continue
                    out.append({"name": s["name"], "image": s.get("image", ""),
                                "wears": s["wears"], "collections": s.get("collections", []),
                                "rarity_name": s.get("rarity_name", ""),
                                "stattrak": bool(s.get("stattrak")),
                                "tradeable": ok, "tradeable_reason": reason})
                    if len(out) >= 30:
                        break
                return self._send_json(out)
            if path == "/api/mispriced-skins":
                params = parse_qs(urlparse(self.path).query)
                cur_param = params.get("currency", ["1"])[0]
                currency = int(cur_param) if cur_param.isdigit() else 1
                return self._send_json(mispriced_skins_mod.list_mispriced(currency=currency))
            if path == "/api/wear-anomalies":
                params = parse_qs(urlparse(self.path).query)
                cur_param = params.get("currency", ["1"])[0]
                currency = int(cur_param) if cur_param.isdigit() else 1
                return self._send_json(wear_anomaly_mod.list_anomalies(currency=currency))
            if path == "/api/market-reliability":
                params = parse_qs(urlparse(self.path).query)
                cur_param = params.get("currency", ["1"])[0]
                currency = int(cur_param) if cur_param.isdigit() else 1
                return self._send_json(market_reliability_mod.list_tracked(currency=currency))
            if path == "/api/market-reliability/log":
                params = parse_qs(urlparse(self.path).query)
                limit_param = params.get("limit", ["200"])[0]
                limit = int(limit_param) if limit_param.isdigit() else 200
                return self._send_json(market_reliability_mod.list_log(limit=limit))
            if path == "/api/patterns/status":
                return self._send_json({"scanner": pattern_scan_state.snapshot(),
                                         "knowledge": pattern_db_mod.get_status()})
            if path == "/api/patterns/opportunities":
                params = parse_qs(urlparse(self.path).query)
                def _s(name):
                    v = params.get(name, [""])[0].strip()
                    return v or None
                def _f(name):
                    v = params.get(name, [""])[0].strip()
                    try:
                        return float(v) if v else None
                    except ValueError:
                        return None
                tier_param = params.get("tier", [""])[0]
                return self._send_json(pattern_scanner_mod.list_opportunities(
                    weapon=_s("weapon"), pattern_type=_s("pattern_type"),
                    marketplace=_s("marketplace"), min_profit=_f("min_profit"),
                    min_confidence=_s("min_confidence"), min_float=_f("min_float"),
                    max_float=_f("max_float"),
                    tier=int(tier_param) if tier_param.isdigit() else None,
                    sort=params.get("sort", ["score"])[0] or "score"))
            if path == "/api/patterns/db":
                db = pattern_db_mod.load_db()
                return self._send_json({"schema_version": db.get("schema_version"),
                                         "knowledge_version": db.get("knowledge_version"),
                                         "sources": db.get("sources"),
                                         "entries": db.get("entries"),
                                         "last_verified_at": db.get("last_verified_at")})
            if path == "/api/patterns/db/history":
                params = parse_qs(urlparse(self.path).query)
                limit_param = params.get("limit", ["100"])[0]
                limit = int(limit_param) if limit_param.isdigit() else 100
                return self._send_json(pattern_db_mod.list_history(limit=limit))
            if path == "/api/backup/status":
                return self._send_json(backup_engine_mod.get_status(backup_worker))
            if path == "/api/connections/status":
                cfg = connections_config_mod.load_config()
                thirdparty_stats = thirdparty_prices_mod.get_stats()
                pattern_status = pattern_scan_state.snapshot()
                return self._send_json({
                    "csfloat": {
                        "configured": thirdparty_stats["csfloat"]["configured"],
                        "masked_key": connections_config_mod.masked_key(cfg.get("csfloat_api_key")),
                        "note": pattern_status.get("csfloat_key_note"),
                    },
                    # Steam session token -- masked_key() only, the raw value never leaves the
                    # server (see connections_config.configure/pattern_scanner.configure_steam_cookie).
                    "steam_session": {
                        "configured": bool(pattern_status.get("steam_login_cookie_configured")),
                        "masked_key": connections_config_mod.masked_key(cfg.get("steam_login_secure")),
                        "note": pattern_status.get("steam_login_cookie_note"),
                    },
                    "github_backup": backup_engine_mod.get_status(backup_worker),
                    "ntfy": {
                        "configured": alerts_mod.configured(),
                        "enabled": bool(cfg.get("ntfy_enabled")),
                        "topic": cfg.get("ntfy_topic"),
                        "server": cfg.get("ntfy_server"),
                        "roi_threshold_pct": cfg.get("ntfy_roi_threshold_pct"),
                        "pattern_threshold_pct": cfg.get("ntfy_pattern_threshold_pct"),
                    },
                })
            if path == "/api/constants":
                return self._send_json({
                    "app_version": APP_VERSION,
                    "steam_sell_fee_pct": tradeup_mod.STEAM_SELL_FEE * 100,
                    "wear_breakpoints": oddsmath_mod.WEAR_BREAKPOINTS,
                    "drought_threshold_pct": 15.0,  # thirdparty_prices.drought_flag()'s default threshold_pct
                    "min_training_samples": learning_mod.MIN_TRAINING_SAMPLES,
                    "retrain_trigger_delta": retrain_loop_mod.RETRAIN_TRIGGER_DELTA,
                    "price_ttl_seconds": steam_prices.PRICE_TTL_SECONDS,
                    "min_price_history_points": price_history_mod.MIN_HISTORY_POINTS,
                    "low_volume_threshold": price_report_mod.LOW_VOLUME_THRESHOLD,
                    "sim_min_training_samples": sim_learning_mod.MIN_TRAINING_SAMPLES,
                    "sim_retrain_trigger_delta": sim_learning_mod.RETRAIN_TRIGGER_DELTA,
                    "sim_son_temperatures": sim_loop_mod.SON_TEMPERATURES,
                    "sim_stuck_threshold_rounds": sim_loop_mod.STUCK_THRESHOLD_ROUNDS,
                    "sim_nudge_duration_rounds": sim_loop_mod.NUDGE_DURATION_ROUNDS,
                    # Self-tuning agent preferences (src/adaptive.py). agent_profiles below are
                    # each agent's STARTING personality -- the live, self-tuned values are on
                    # each son's own snapshot in /api/sim-population/status.
                    "sim_adaptive_params": list(adaptive_mod.ADAPTIVE_PARAMS),
                    "sim_adapt_window_rounds": adaptive_mod.ADAPT_WINDOW_ROUNDS,
                    "sim_adapt_adopt_epsilon_pp": adaptive_mod.ADOPT_EPSILON_PP,
                    "sim_adapt_band_radius": adaptive_mod.BAND_RADIUS,
                    "sim_adapt_exploration_floor": adaptive_mod.EXPLORATION_RATE_FLOOR,
                    "sim_adapt_novelty_ucb_pp": adaptive_mod.NOVELTY_UCB_PP,
                    "sim_adapt_novelty_reward_pp": adaptive_mod.NOVELTY_REWARD_PP,
                    "sim_adapt_invalid_penalty_pp": adaptive_mod.INVALID_PENALTY_PP,
                    "agent_names": agents_mod.AGENT_NAMES,
                    "agent_profiles": {sid: {"name": p.name, "purpose": p.purpose,
                                              "exploration_rate": p.exploration_rate,
                                              "risk_tolerance": p.risk_tolerance,
                                              "collection_mixing_bias": p.collection_mixing_bias,
                                              "wear_optimization_bias": p.wear_optimization_bias,
                                              "liquidity_importance": p.liquidity_importance,
                                              "roi_weight": p.roi_weight,
                                              "mutation_strength": p.mutation_strength,
                                              "memory_weight": p.memory_weight}
                                        for sid, p in agents_mod.AGENT_PROFILES.items()},
                    "dead_skin_threshold": dead_skin_mod.DEAD_SKIN_THRESHOLD,
                    "invalid_skin_recheck_seconds": invalid_skins_mod.INVALID_RECHECK_SECONDS,
                    "verify_queue_roi_threshold_pct": verify_queue_mod.ROI_THRESHOLD_PCT,
                    "verify_queue_confidence_threshold": verify_queue_mod.CONFIDENCE_THRESHOLD,
                    "mispriced_gap_threshold_pct": mispriced_skins_mod.GAP_THRESHOLD_PCT,
                    "mispriced_recheck_seconds": mispriced_skins_mod.RECHECK_SECONDS,
                    "wear_anomaly_tolerance_pct": wear_anomaly_mod.INVERSION_TOLERANCE_PCT,
                    "wear_anomaly_recheck_seconds": wear_anomaly_mod.RECHECK_SECONDS,
                    "tradability_reasons": tradability_mod.ELIGIBILITY_REASONS,
                    "standard_input_count": rules_engine_mod.STANDARD_INPUT_COUNT,
                })
            if path == "/api/price-report":
                # Reuses whatever the current scan already found (state.tradeups is already
                # filtered to roi_pct >= the configured min-roi -- see run_scan) instead of
                # kicking off a brand new scan: Steam prices are taken straight from those
                # results (see price_report.py's module docstring for why), so this only adds
                # ONE new network call (Skinport's shared bulk fetch, already cached 5 min) plus
                # optional per-row CSFloat calls -- fast enough to run inline, unlike a scan.
                params = parse_qs(urlparse(self.path).query)
                ml_only = params.get("ml_only", ["false"])[0].lower() == "true"
                with state.lock:
                    candidates = list(state.tradeups)
                    currency = state.config["currency"]
                if ml_only:
                    candidates = [r for r in candidates
                                  if r.ml_predicted_roi_pct is not None and r.ml_predicted_roi_pct >= 0]
                if not candidates:
                    reason = ("no ML-flagged winning trade-ups in the current results -- train a model, "
                               "enable \"Use ML calibration\", and rescan first" if ml_only else
                               "no winning trade-ups in the current results -- run a scan first")
                    return self._send_json({"ok": False, "error": reason}, 400)
                rows = price_report_mod.build_report(candidates, currency)
                csv_rows = price_report_mod.to_csv_rows(rows)
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=price_report_mod.CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(csv_rows)
                data = buf.getvalue().encode("utf-8")
                ts = time.strftime("%Y%m%d-%H%M%S")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="cs2-price-report-{ts}.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/export-data":
                # Generated to a temp file (not the project root -- the browser's own download
                # already saves a copy wherever the user wants; leaving one in the project
                # root too on every click would just accumulate clutter, unlike the CLI's
                # export-data, which deliberately leaves its archive in place).
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        tmp_path = Path(tmp) / "data-archive.zip"
                        # export_archive splits into <=100 MB parts (GitHub's per-file limit
                        # -- see data_archive.py); bundle them into one outer zip so the
                        # browser still gets a single one-click download.
                        parts = data_archive_mod.export_archive(tmp_path)
                        if len(parts) == 1:
                            data = parts[0].read_bytes()
                        else:
                            bundle_path = Path(tmp) / "data-archive-bundle.zip"
                            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_STORED) as bz:
                                for part in parts:
                                    bz.write(part, part.name)
                            data = bundle_path.read_bytes()
                except FileNotFoundError as e:
                    return self._send_json({"ok": False, "error": str(e)}, 400)
                ts = time.strftime("%Y%m%d-%H%M%S")
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="cs2-data-archive-{ts}.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/price-history":
                params = parse_qs(urlparse(self.path).query)
                skin = params.get("skin", [""])[0]
                wear = params.get("wear", [""])[0]
                stattrak = params.get("stattrak", ["false"])[0].lower() == "true"
                try:
                    currency = int(params.get("currency", ["1"])[0])
                except ValueError:
                    currency = 1
                if not skin or not wear:
                    return self._send_json({"ok": False, "error": "skin and wear are required"}, 400)
                name = steam_prices.market_hash_name(skin, wear, stattrak)
                history = price_history_mod.get_history(name, currency)
                if not history:
                    # Plant the first data point immediately (one live Steam lookup) rather
                    # than making the user run a full collection scan before this feature
                    # shows anything at all.
                    steam_prices.get_price(name, currency)
                    history = price_history_mod.get_history(name, currency)
                prediction = price_history_mod.predict_next_price(history)
                return self._send_json({
                    "market_hash_name": name,
                    "history": history,
                    "prediction": (prediction.__dict__ if prediction else None),
                    "min_points_required": price_history_mod.MIN_HISTORY_POINTS,
                })
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/config":
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                with state.lock:
                    for k in CONFIG_KEYS:
                        if k in body:
                            state.config[k] = body[k]
                    state.reset_resume_state()  # new scope/settings -- any paused progress no longer applies
                    cfg = dict(state.config)
                return self._send_json({"ok": True, "config": cfg})
            if path == "/api/scan-now":
                with state.lock:
                    state.reset_resume_state()  # explicit fresh start, not a resume
                state.pause_event.clear()
                state.wake_event.set()
                return self._send_json({"ok": True})
            if path == "/api/stop":
                state.stop_event.set()
                state.pause_event.clear()
                # if we're currently paused, scan_loop is blocked in an untimed wake_event.wait() --
                # stop_event alone won't unblock that, so wake it too or Stop would silently do nothing
                state.wake_event.set()
                return self._send_json({"ok": True})
            if path == "/api/pause":
                state.pause_event.set()
                return self._send_json({"ok": True})
            if path == "/api/resume":
                state.pause_event.clear()
                state.wake_event.set()  # resume_collection_index/partial_tradeup_results are left as-is
                return self._send_json({"ok": True})

            if path == "/api/patterns/scan/start":
                pattern_scan_state.pause_event.clear()
                pattern_scan_state.wake_event.set()
                return self._send_json({"ok": True})
            if path == "/api/patterns/scan/pause":
                pattern_scan_state.pause_event.set()
                return self._send_json({"ok": True})
            if path == "/api/patterns/refresh":
                try:
                    result = pattern_db_mod.refresh_knowledge()
                except (OSError, json.JSONDecodeError) as e:
                    return self._send_json({"ok": False, "error": str(e)}, 500)
                return self._send_json({"ok": True, "result": result})
            if path == "/api/patterns/db/update-entry":
                # Manual override for one knowledge entry -- versioned + audited by pattern_db.
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                entry_id = body.get("id")
                fields = body.get("fields") or {}
                if not entry_id or not isinstance(fields, dict):
                    return self._send_json({"ok": False, "error": "need id + fields"}, 400)
                ok = pattern_db_mod.update_entry(entry_id, fields, note=body.get("note", ""))
                return self._send_json({"ok": ok} if ok else
                                        {"ok": False, "error": "unknown entry id or no updatable fields"}, 200 if ok else 400)

            if path == "/api/backup/config":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                cfg = backup_engine_mod.save_config(body)
                return self._send_json({"ok": True, "config": cfg,
                                         "status": backup_engine_mod.get_status(backup_worker)})
            if path == "/api/backup/run-now":
                # Force one full cycle right now (skips the delta check), synchronously, so the
                # dashboard can show the result of THIS run rather than "check back later".
                result = backup_engine_mod.run_backup(force=True)
                return self._send_json({"ok": result.get("ok", False), "result": result})

            if path == "/api/connections/steam-session":
                # Written to ONLY by the bundled helper extension (extension/), which is the one
                # thing that can read the steamLoginSecure cookie -- it is HttpOnly, so no page
                # script or bookmarklet can, by design.
                #
                # Threat model, since this accepts a session token: the server already binds
                # 127.0.0.1 only, so nothing off-machine can reach it. The remaining exposure is
                # a malicious PAGE in the user's own browser POSTing here cross-origin. Two
                # things close that: (1) a browser extension sends Origin: chrome-extension://...
                # (or moz-extension://), and a normal web page cannot forge that header; (2) a
                # page attempting this with Content-Type: application/json triggers a CORS
                # preflight we never answer. Worst case for a forged post is a bogus cookie that
                # makes scanning fail loudly -- it can never read the stored value back, since
                # every read path masks it.
                origin = self.headers.get("Origin") or ""
                if origin and not origin.startswith(("chrome-extension://", "moz-extension://",
                                                      "safari-web-extension://")):
                    return self._send_json(
                        {"ok": False, "error": "this endpoint only accepts posts from the "
                                                "bundled browser extension"}, 403)
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                value = (body.get("steam_login_secure") or "").strip()
                if not value:
                    return self._send_json(
                        {"ok": False, "error": "no steamLoginSecure value sent -- are you "
                                                "logged into Steam in this browser?"}, 400)
                cfg = connections_config_mod.save_config({"steam_login_secure": value})
                connections_config_mod.apply_to_runtime(cfg)
                # Never echo the value, not even to the extension that just sent it.
                return self._send_json({
                    "ok": True,
                    "masked_key": connections_config_mod.masked_key(cfg.get("steam_login_secure")),
                    "message": "Steam session saved -- the Rare Patterns scanner now uses "
                               "Steam's authenticated rate limit.",
                })

            if path == "/api/connections/config":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                cfg = connections_config_mod.save_config(body)
                connections_config_mod.apply_to_runtime(cfg)
                return self._send_json({"ok": True, "config": {
                    "csfloat_configured": bool(cfg.get("csfloat_api_key")),
                    "csfloat_masked_key": connections_config_mod.masked_key(cfg.get("csfloat_api_key")),
                    "steam_session_configured": bool(cfg.get("steam_login_secure")),
                    "steam_session_masked_key": connections_config_mod.masked_key(cfg.get("steam_login_secure")),
                    "ntfy_enabled": cfg.get("ntfy_enabled"),
                    "ntfy_topic": cfg.get("ntfy_topic"),
                    "ntfy_server": cfg.get("ntfy_server"),
                    "ntfy_roi_threshold_pct": cfg.get("ntfy_roi_threshold_pct"),
                    "ntfy_pattern_threshold_pct": cfg.get("ntfy_pattern_threshold_pct"),
                }})
            if path == "/api/connections/ntfy/test":
                if not alerts_mod.configured():
                    return self._send_json({"ok": False, "error": "no NTFY topic configured -- save one first"}, 400)
                ok = alerts_mod.notify("CS2 dashboard test notification",
                                        "If you can see this, NTFY alerts are wired up correctly.",
                                        priority="default", tags=["white_check_mark"])
                return self._send_json({"ok": ok} if ok else
                                        {"ok": False, "error": "send failed -- check the topic/server and your network"})

            if path == "/api/market-reliability/override":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                try:
                    market_reliability_mod.set_override(
                        body["market_hash_name"], int(body.get("currency", 1)),
                        body["state"], body.get("note", ""))
                except (KeyError, TypeError, ValueError) as e:
                    return self._send_json({"ok": False, "error": str(e)}, 400)
                return self._send_json({"ok": True})

            if path == "/api/market-reliability/override/clear":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                try:
                    removed = market_reliability_mod.clear_override(
                        body["market_hash_name"], int(body.get("currency", 1)))
                except (KeyError, TypeError) as e:
                    return self._send_json({"ok": False, "error": str(e)}, 400)
                return self._send_json({"ok": True, "removed": removed})

            if path == "/api/log-outcome":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                # No state.lock here -- outcomes.py has its own internal write lock, and this
                # endpoint never touches ScanState, so it can't contend with the scan loop.
                try:
                    outcome = outcomes_mod.log_outcome(**body)
                except (ValueError, TypeError) as e:
                    return self._send_json({"ok": False, "error": str(e)}, 400)
                return self._send_json({"ok": True, "outcome": asdict(outcome)})

            if path == "/api/update-outcome-sale":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                try:
                    updated = outcomes_mod.update_outcome_sale(body["id"], float(body["sale_price"]))
                except (KeyError, TypeError, ValueError) as e:
                    return self._send_json({"ok": False, "error": str(e)}, 400)
                if updated is None:
                    return self._send_json({"ok": False, "error": "outcome id not found"}, 404)
                return self._send_json({"ok": True, "outcome": asdict(updated)})

            if path == "/api/train-model":
                # Sub-second on the row counts this feature realistically sees -- runs inline
                # in the request handler rather than needing its own background-thread state
                # machine, unlike scan-now.
                result = learning_mod.train()
                return self._send_json({"ok": True, "trained": result.trained, "reason": result.reason,
                                         "n_samples": result.n_samples, "validation_mae": result.validation_mae,
                                         "feature_importances": result.feature_importances,
                                         "model_name": result.model_name, "candidate_maes": result.candidate_maes})

            if path == "/api/invalid-skins/clear":
                removed = invalid_skins_mod.clear_all()
                return self._send_json({"ok": True, "removed": removed})

            if path == "/api/mispriced-skins/clear":
                removed = mispriced_skins_mod.clear_all()
                return self._send_json({"ok": True, "removed": removed})

            if path == "/api/wear-anomalies/clear":
                removed = wear_anomaly_mod.clear_all()
                return self._send_json({"ok": True, "removed": removed})

            if path == "/api/sim-population/verify-top":
                # "Verify top N trade-ups" button (Results page): force-queue the current
                # leaderboard's top N into the SAME one-at-a-time Stage-2 consumer the
                # automatic queue uses -- see verify_queue.enqueue_top. Each verification's
                # per-skin gaps feed the mispriced-skin registry, which future simulations
                # penalize (mispriced_skins.py's module docstring has the full loop).
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                n = body.get("n", 50)
                n = max(1, min(200, int(n) if isinstance(n, (int, float)) else 50))
                queued = verify_queue_mod.enqueue_top(n)
                return self._send_json({"ok": True, "queued": queued})

            if path == "/api/manual-tradeup":
                # Manual trade-up builder: the user composes a contract from ANY skins --
                # mixing collections is the point (post-Oct-2025 group-based odds) -- and this
                # prices/validates it through the exact same path the agents use
                # (rules_engine.validate_contract -> simulate.simulate), never a parallel
                # re-implementation. Runs live price fetches, so it can take a few seconds.
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                units_in = body.get("units") or []
                stattrak = bool(body.get("stattrak"))
                try:
                    currency = int(body.get("currency", 1))
                except (TypeError, ValueError):
                    currency = 1
                if not units_in:
                    return self._send_json({"ok": False, "error": "no input skins given"}, 400)
                skins = data_mod.load_skins()
                by_name = {s["name"]: s for s in skins}
                idx = data_mod.collections_index(skins)
                problems = []
                resolved = []
                for u in units_in:
                    name = (u.get("skin") or "").strip()
                    wear = u.get("wear") or ""
                    try:
                        count = int(u.get("count", 1))
                    except (TypeError, ValueError):
                        count = 0
                    skin = by_name.get(name)
                    if skin is None:
                        problems.append({"skin": name, "issue": "unknown_skin",
                                         "detail": "Not found in the skin database (knives/gloves can't be inputs)."})
                        continue
                    if user_overrides_mod.is_blacklisted_skin(name):
                        problems.append({"skin": name, "issue": "user_blacklisted",
                                         "detail": "You permanently blacklisted this skin "
                                                   "(Market intelligence → Skin blacklist). "
                                                   "Remove it there to use it again."})
                        continue
                    ok, reason = tradability_mod.check_skin(skin, idx)
                    if not ok:
                        problems.append({"skin": name, "issue": reason,
                                         "detail": tradability_mod.ELIGIBILITY_REASONS.get(reason, reason)})
                        continue
                    if wear not in skin["wears"]:
                        problems.append({"skin": name, "issue": "wear_not_available_for_skin",
                                         "detail": f"This skin's float range excludes {wear or '(none)'}."})
                        continue
                    if stattrak and not skin.get("stattrak"):
                        problems.append({"skin": name, "issue": "no_stattrak_variant",
                                         "detail": "No StatTrak variant exists for this skin."})
                        continue
                    if count <= 0:
                        problems.append({"skin": name, "issue": "non_positive_count", "detail": "Count must be >= 1."})
                        continue
                    resolved.append((skin, wear, count))
                if problems:
                    return self._send_json({"ok": False, "error": "some inputs can't be traded up",
                                             "problems": problems}, 400)
                rarity_ids = {s["rarity_id"] for s, _, _ in resolved}
                if len(rarity_ids) > 1:
                    return self._send_json({"ok": False, "error": "all 10 inputs must share ONE rarity tier "
                                             f"(got {', '.join(sorted(rarity_ids))})"}, 400)
                from_id = next(iter(rarity_ids))
                to_id = data_mod.RARITY_CHAIN[data_mod.RARITY_INDEX[from_id] + 1][0]
                legs_by_collection = {}
                for skin, wear, count in resolved:
                    # A skin realistically belongs to one collection; if the feed lists
                    # several, prefer one that actually has outputs at the target tier.
                    colls = skin.get("collections") or [""]
                    coll = next((c for c in colls if idx.get(c, {}).get(to_id)), colls[0])
                    legs_by_collection.setdefault(coll, []).append(
                        {"skin": skin, "wear": wear, "count": count})
                legs = []
                for coll, units in legs_by_collection.items():
                    outputs = idx.get(coll, {}).get(to_id, [])
                    if stattrak:
                        outputs = [s for s in outputs if s["stattrak"]]
                    legs.append(random_trade_mod.CandidateLeg(collection=coll, units=units, outputs=outputs))
                from_name = dict(data_mod.RARITY_CHAIN)[from_id]
                to_name = dict(data_mod.RARITY_CHAIN)[to_id]
                candidate = random_trade_mod.Candidate(
                    from_rarity_id=from_id, from_rarity_name=from_name,
                    to_rarity_id=to_id, to_rarity_name=to_name, stattrak=stattrak, legs=legs)
                valid, reason = rules_engine_mod.validate_contract(candidate)
                if not valid:
                    friendly = {"collection_has_no_outputs_at_target_rarity":
                                    f"a collection in this mix has no {to_name} skins -- the game "
                                    "won't accept its skins in this contract"}
                    return self._send_json({"ok": False,
                                             "error": friendly.get(reason, f"not a legal contract: {reason}")}, 400)
                record = simulate_mod.simulate(candidate, currency=currency, live_fetch=True)
                if isinstance(record, simulate_mod.InvalidSimulation):
                    errors = {
                        "incomplete_inputs": ("couldn't price every input "
                                               f"({record.n_priced_input_units}/"
                                               f"{record.n_required_input_units} priced)"),
                        # Distinct from no_output_price: SOME outputs priced, not all. Worth
                        # saying plainly -- the ROI isn't merely unavailable, it would have been
                        # inflated (see oddsmath's unpriced-outputs section), and the user is
                        # staring at a contract that looks perfectly fine.
                        "incomplete_outputs": ("couldn't price every possible output -- this "
                                                "contract can land on a skin with no price, so any "
                                                "ROI would only count the outputs that did price "
                                                "and come out too high"),
                    }
                    return self._send_json({
                        "ok": False,
                        "error": errors.get(record.reason, "no output skin has a usable price right now"),
                        "reason": record.reason}, 422)
                return self._send_json({"ok": True, "result": asdict(record)})

            if path == "/api/reset-model":
                learning_mod.reset_model()
                return self._send_json({"ok": True})

            if path == "/api/retrain-loop/start":
                with retrain_state.lock:
                    retrain_state.pause_event.clear()
                return self._send_json({"ok": True})
            if path == "/api/retrain-loop/stop":
                retrain_state.pause_event.set()
                return self._send_json({"ok": True})

            if path == "/api/verify-queue/start":
                # Resumes the automatic Stage-2 verification consumer -- see verify_queue.py's
                # module docstring for why this is safe to auto-start (narrow gate, one live
                # call at a time) but still pausable if it ever proves disruptive.
                with verify_queue_state.lock:
                    verify_queue_state.pause_event.clear()
                return self._send_json({"ok": True})
            if path == "/api/verify-queue/stop":
                verify_queue_state.pause_event.set()
                return self._send_json({"ok": True})

            if path == "/api/price-scan/config":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                with price_scan_state.lock:
                    if "collection" in body:
                        price_scan_state.config["collection"] = body["collection"]
                        price_scan_state.checkpoint["index"] = 0  # new scope -- any paused progress no longer applies
                    if "prioritize" in body:
                        price_scan_state.config["prioritize"] = bool(body["prioritize"])
                        price_scan_state.checkpoint["index"] = 0  # reordering the sweep -- see
                        # price_scan.build_target_list's prioritize docstring, same reasoning as collection above
                return self._send_json({"ok": True})
            if path == "/api/price-scan/start" or path == "/api/price-scan/resume":
                price_scan_state.pause_event.clear()
                price_scan_state.stop_event.clear()
                price_scan_state.wake_event.set()
                return self._send_json({"ok": True})
            if path == "/api/price-scan/pause":
                price_scan_state.pause_event.set()
                return self._send_json({"ok": True})
            if path == "/api/price-scan/stop":
                price_scan_state.stop_event.set()
                price_scan_state.pause_event.clear()
                price_scan_state.wake_event.set()
                return self._send_json({"ok": True})

            if path == "/api/sim-population/verify-prices":
                # On-demand, live Skinport/CSFloat cross-check for ONE simulation record the
                # user actually opened -- never run automatically across the whole leaderboard
                # (rate-limited). See price_report.verify_simulation_record's own docstring.
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                try:
                    rows = price_report_mod.verify_simulation_record(
                        primary_collection=body["primary_collection"], from_rarity=body["from_rarity"],
                        to_rarity=body["to_rarity"], stattrak=body["stattrak"],
                        expected_roi_pct=body["expected_roi_pct"], input_mix=body["input_mix"],
                        output_distribution=body["output_distribution"], currency=body.get("currency", 1))
                except (KeyError, TypeError) as e:
                    return self._send_json({"ok": False, "error": f"missing/invalid field: {e}"}, 400)
                verified_roi_pct = price_confidence = None
                # Persist back to the checkpoint entry too, same as the automatic queue does
                # (verify_queue.summarize_and_writeback) -- "populated by either the manual
                # button or the automatic queue" is one code path, not two. Only when the
                # caller actually identifies which entry this is (id + son) -- optional so an
                # older/simpler caller that just wants the display-only rows still works.
                if "id" in body and "son" in body:
                    entry = {"id": body["id"], "input_mix": body["input_mix"]}
                    verified_roi_pct, price_confidence = verify_queue_mod.summarize_and_writeback(
                        int(body["son"]), entry, rows, currency=body.get("currency", 1))
                return self._send_json({"ok": True, "rows": [asdict(r) for r in rows],
                                         "verified_roi_pct": verified_roi_pct, "price_confidence": price_confidence})

            if path == "/api/sim-population/blacklist":
                # Mark a trade-up the user has already executed as "done" -- it's hidden from the
                # opportunity list from now on and never re-admitted to any son's best list
                # (see sim_blacklist.py). Keyed by the composition's content signature, not the
                # per-simulation id, so the same contract can't sneak back under a fresh id.
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                try:
                    record = sim_blacklist_mod.add(body)
                except (KeyError, TypeError) as e:
                    return self._send_json({"ok": False, "error": f"missing/invalid field: {e}"}, 400)
                return self._send_json({"ok": True, "entry": record})
            if path == "/api/sim-population/blacklist/remove":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                removed = sim_blacklist_mod.remove(body.get("signature", ""))
                return self._send_json({"ok": True, "removed": removed})

            if path == "/api/skin-blacklist":
                # PERMANENT per-skin blacklist (user_overrides.py -- unlike every self-healing
                # registry, this only ever changes when the user says so). Existing best-list/
                # archive entries touching the skin disappear at once (list-time filtering) and
                # sons self-clean their checkpoints on their next round; scans/agents/manual
                # builder all refuse the skin from now on.
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                skin = (body.get("skin") or "").strip()
                if not skin:
                    return self._send_json({"ok": False, "error": "skin name is required"}, 400)
                all_names = {s["name"] for s in data_mod.load_skins() + data_mod.load_knives_gloves()}
                if skin not in all_names:
                    # Case-insensitive resolution as a convenience -- an exact-name mismatch
                    # would otherwise silently blacklist nothing.
                    resolved = next((n for n in all_names if n.lower() == skin.lower()), None)
                    if resolved is None:
                        return self._send_json({"ok": False,
                                                 "error": f'unknown skin "{skin}" -- use the exact '
                                                          f'database name (try the autocomplete)'}, 400)
                    skin = resolved
                entry = user_overrides_mod.add_blacklist(skin, note=body.get("note", ""))
                return self._send_json({"ok": True, "entry": entry})
            if path == "/api/skin-blacklist/remove":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                removed = user_overrides_mod.remove_blacklist(body.get("skin", ""))
                return self._send_json({"ok": True, "removed": removed})

            if path == "/api/price-overrides":
                # Manual price override for one skin+wear(+StatTrak) -- wins over every other
                # price source (see user_overrides.py) and retro-reprices existing best-list/
                # archive entries at list time, so Min-ROI filters catch bugged trade-ups
                # immediately.
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                try:
                    entry = user_overrides_mod.set_override(
                        body.get("skin", ""), body.get("wear", ""), bool(body.get("stattrak")),
                        float(body.get("price", 0)), int(body.get("currency", 1)))
                except (TypeError, ValueError) as e:
                    return self._send_json({"ok": False, "error": str(e)}, 400)
                return self._send_json({"ok": True, "entry": entry})
            if path == "/api/price-overrides/remove":
                try:
                    body = self._read_json_body()
                except json.JSONDecodeError:
                    return self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                removed = user_overrides_mod.remove_override(body.get("market_hash_name", ""))
                return self._send_json({"ok": True, "removed": removed})

            if path == "/api/sim-population/reset-all":
                # Global "Restart all training & data": stop the round-robin scheduler first so it
                # isn't mid-handoff while we wipe checkpoints, pause every son so no running thread
                # writes a fresh round on top of the reset, then hard-reset each agent from scratch.
                round_robin.stop()
                for son in sim_population:
                    son.pause_event.set()
                sim_loop_mod.reset_all(sim_population)
                return self._send_json({"ok": True})

            if path == "/api/reset-all-data":
                # App-wide "Reset all data" (topbar): wipes every REGENERABLE cache/registry --
                # the ML sim population, rare-pattern scan results, the live-Steam price cache,
                # and the invalid-skin/mispriced-skin/wear-anomaly/dead-skin registries -- so the
                # whole app can restart cold. Deliberately does NOT touch data/outcomes.jsonl
                # (real hand-logged trade history), the real ROI model, price_history.jsonl,
                # sim_blacklist.json (a user preference, "already done"), or user_overrides.py's
                # permanent blacklist/price overrides -- none of that is regenerable.
                round_robin.stop()
                for son in sim_population:
                    son.pause_event.set()
                sim_loop_mod.reset_all(sim_population)
                pattern_scan_state.pause_event.set()
                removed_patterns = pattern_scanner_mod.clear_opportunities()
                removed_prices = steam_prices.clear_cache()
                removed_invalid = invalid_skins_mod.clear_all()
                removed_dead = dead_skin_mod.clear_all()
                removed_mispriced = mispriced_skins_mod.clear_all()
                removed_wear = wear_anomaly_mod.clear_all()
                return self._send_json({"ok": True, "removed": {
                    "pattern_opportunities": removed_patterns, "price_cache_entries": removed_prices,
                    "invalid_skins": removed_invalid, "dead_skin_scores": removed_dead,
                    "mispriced_skins": removed_mispriced, "wear_anomalies": removed_wear}})

            if path == "/api/sim-population/round-robin/start":
                round_robin.start()
                return self._send_json({"ok": True})
            if path == "/api/sim-population/round-robin/stop":
                round_robin.stop()
                return self._send_json({"ok": True})

            # Per-son actions: /api/sim-population/<son_id>/start|resume|pause|stop|nudge|reset|train
            parts = path.split("/")
            if (len(parts) == 5 and parts[1] == "api" and parts[2] == "sim-population" and parts[3].isdigit()):
                son_id, action = int(parts[3]), parts[4]
                if 0 <= son_id < len(sim_population):
                    son = sim_population[son_id]
                    if action in ("start", "resume"):
                        with son.lock:
                            son.pause_event.clear()
                        son.wake_event.set()
                        return self._send_json({"ok": True})
                    if action in ("pause", "stop"):
                        # "Stop" idles the same always-running background thread rather than
                        # terminating it -- the checkpoint already saves after every round, so a
                        # later Start resumes instantly instead of spawning a replacement thread.
                        son.pause_event.set()
                        return self._send_json({"ok": True})
                    if action == "nudge":
                        sim_loop_mod.nudge(son)
                        return self._send_json({"ok": True})
                    if action == "reset":
                        sim_loop_mod.full_reset(son)
                        return self._send_json({"ok": True})
                    if action == "train":
                        result = sim_learning_mod.train(son.model_dir, son.sims_file)
                        return self._send_json({"ok": True, "trained": result.trained, "reason": result.reason,
                                                 "n_samples": result.n_samples, "validation_mae": result.validation_mae,
                                                 "feature_importances": result.feature_importances,
                                                 "model_name": result.model_name, "candidate_maes": result.candidate_maes,
                                                 "elapsed_seconds": result.elapsed_seconds,
                                                 "phase_seconds": result.phase_seconds})

            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            pass  # keep console output clean; use /api/status for progress instead

    return Handler


def main():
    # Apply any previously-saved Connections settings (CSFloat key, NTFY topic/server) before
    # anything else starts -- see src/connections_config.py for why this lives outside data/.
    connections_config_mod.apply_to_runtime()

    # Absorb sklearn's one-time per-process startup cost (~3.2s: the import itself plus a
    # further ~1.6s hidden inside the first gradient-boosting fit) on a throwaway 32-row fit
    # here, rather than billing it to whichever population son happens to retrain first after
    # this restart -- see ml_common.prewarm() for the measurements. Started first and left
    # unjoined: nothing below waits on it, the sons all boot paused anyway, and the only cost
    # of it not finishing in time is the behavior we had before.
    threading.Thread(target=ml_common_mod.prewarm, daemon=True).start()

    state = ScanState()
    threading.Thread(target=scan_loop, args=(state,), daemon=True).start()
    threading.Thread(target=rate_limit_watchdog, args=(state,), daemon=True).start()

    # Dashboard NTFY alerts (see dashboard_alerts_loop above): safe to auto-start, it no-ops
    # entirely until a topic is saved AND enabled in the Connections page, and only reads
    # data the dashboard's own scans have already computed -- no extra network calls.
    dashboard_alerts_stop = threading.Event()
    threading.Thread(target=dashboard_alerts_loop, args=(state, dashboard_alerts_stop), daemon=True).start()

    # Retrain-watch loop: safe to auto-start -- it only polls outcomes.stats() (cheap, no
    # network) until enough new logged outcomes exist to actually retrain.
    retrain_state = retrain_loop_mod.RetrainLoopState()
    threading.Thread(target=retrain_loop_mod.retrain_loop, args=(retrain_state,), daemon=True).start()

    # Bulk price scan: starts idle, waiting for the dashboard's Start button -- same
    # no-scan-on-boot reasoning as ScanState.scan_loop above (this one also hits live Steam
    # prices, just without any trade-up math on top).
    price_scan_state = price_scan_mod.PriceScanState()
    threading.Thread(target=price_scan_mod.scan_loop, args=(price_scan_state,), daemon=True).start()

    # Guided random-simulation population: 4 independent "sons" (see sim_loop.py), each with
    # its own temperature/data/model, all starting paused -- same reasoning as the deep search
    # loop, a background batch job shouldn't kick off just because the server started.
    sim_population = sim_loop_mod.create_population()
    for son in sim_population:
        son.pause_event.set()
        threading.Thread(target=sim_loop_mod.sim_loop, args=(son,), daemon=True).start()

    # Anti-convergence watcher (see sim_loop.ConvergenceWatcher): a cheap, always-on
    # background check across the whole population -- safe to auto-start since it only
    # reads each son's already-in-memory checkpoint and does nothing until 3+ sons have
    # genuinely converged on the same strategy for several consecutive checks.
    convergence_watcher = sim_loop_mod.ConvergenceWatcher()
    convergence_stop = threading.Event()
    threading.Thread(target=sim_loop_mod.watch_convergence,
                      args=(sim_population, convergence_watcher, convergence_stop), daemon=True).start()

    # Round-robin scheduler (see sim_loop.RoundRobinScheduler): "Run all" button on the
    # dashboard -- disabled until a user starts it, same as every other opt-in loop above.
    round_robin = sim_loop_mod.RoundRobinScheduler(sim_population)
    round_robin_stop = threading.Event()
    threading.Thread(target=sim_loop_mod.watch_round_robin,
                      args=(round_robin, round_robin_stop), daemon=True).start()

    # Automatic Stage-2 verification queue (see verify_queue.py): safe to auto-start, same
    # reasoning as retrain-loop above -- it only does real (live-network) work once a
    # background son's find actually clears the narrow ROI/confidence bar, and even then
    # only one at a time.
    verify_queue_state = verify_queue_mod.VerifyQueueState()
    threading.Thread(target=verify_queue_mod.consumer_loop, args=(verify_queue_state,), daemon=True).start()

    # Rare Pattern Arbitrage scanner (see pattern_scanner.py): starts PAUSED -- it talks to
    # an external listings API, so scanning is opt-in via the Rare Patterns page's Start
    # button, same convention as the bulk price scan above.
    pattern_scan_state = pattern_scanner_mod.PatternScanState()
    threading.Thread(target=pattern_scanner_mod.scan_loop, args=(pattern_scan_state,),
                      daemon=True).start()

    # Automated GitHub Backup Engine (see backup_engine.py): the 6-hour delta-aware worker.
    # Safe to auto-start -- until data/backup_config.json points at a real git clone it only
    # hashes 3 local files per wake and reports "not configured" in its status panel.
    backup_worker = backup_engine_mod.BackupWorkerState()
    threading.Thread(target=backup_engine_mod.worker_loop, args=(backup_worker,),
                      daemon=True).start()

    httpd = QuietThreadingHTTPServer(("127.0.0.1", PORT),
                                 make_handler(state, retrain_state, price_scan_state,
                                              sim_population, verify_queue_state, convergence_watcher,
                                              round_robin, pattern_scan_state, backup_worker))
    print(f"CS2 scanner dashboard running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
