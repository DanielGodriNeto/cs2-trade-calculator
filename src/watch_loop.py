"""Continuous, unattended "leave it running" loop that watches for two kinds of opportunity and
pushes a phone notification (see alerts.py) the moment either shows up:
  - a trade-up contract at or above --roi-threshold ROI (default 100% -- your money roughly
    doubles), from the same scan_all() the `tradeups` CLI command uses.
  - a rare-pattern listing priced at or below half its documented collector value, i.e. buying
    it nets >=--pattern-threshold profit relative to the asking price (pattern_scanner.py's
    own opportunity store, which this loop also has to keep populated -- see run_foreground).

Each cycle also drives backup_engine.run_backup() (auto-commit+push accumulated data/ to
GitHub, see data/backup_config.json) and sheets_export.export() (push the live view to Google
Sheets), both harmless no-ops until configured -- see those modules' docstrings.

Steam prices are cached for 1h (steam_prices.py), so only the first cycle after a cold start is
slow (a full ~90-collection scan can take a while per the `tradeups` command's own warning) --
later cycles within the same hour mostly hit the cache and finish fast.

Run with: python -m src.main watch [--interval SECONDS] [--roi-threshold PCT]
                                    [--pattern-threshold PCT]
Ctrl+C to stop. Meant to be left running for hours/days on a machine that stays on.
"""
import json
import threading
import time
from pathlib import Path

from . import alerts as alerts_mod
from . import backup_engine as backup_engine_mod
from . import data as data_mod
from . import pattern_scanner as pattern_scanner_mod
from . import sheets_export as sheets_export_mod
from . import tradeup as tradeup_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "watch_alert_state.json"

DEFAULT_POLL_INTERVAL_SECONDS = 1800  # 30 min between full cycles
DEFAULT_ROI_THRESHOLD_PCT = 100.0
DEFAULT_PATTERN_THRESHOLD_PCT = 100.0
# Don't re-alert the same still-live opportunity more than once per cooldown window -- it may
# still be sitting there next cycle (nobody bought it yet), which isn't news worth a second ping.
ALERT_COOLDOWN_SECONDS = 6 * 3600


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"alerted": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"alerted": {}}
    state.setdefault("alerted", {})
    return state


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _should_alert(state: dict, key: str) -> bool:
    last = state["alerted"].get(key)
    return last is None or (time.time() - last) >= ALERT_COOLDOWN_SECONDS


def _mark_alerted(state: dict, key: str) -> None:
    state["alerted"][key] = time.time()


def _ascii(s: str) -> str:
    """ntfy sends these as raw HTTP header values, which must be Latin-1 -- skin/sticker names
    are almost always plain ASCII, but this is a cheap guarantee against the rare one that isn't."""
    return s.encode("ascii", "replace").decode("ascii")


def scan_tradeups(roi_threshold: float) -> list:
    skins = data_mod.load_skins()
    idx = data_mod.collections_index(skins)
    results = tradeup_mod.scan_all(idx, currency=1, fast_mode=True, min_roi=roi_threshold)
    return [r for r in results if r.roi_pct >= roi_threshold]


def scan_patterns(pattern_threshold: float) -> list[dict]:
    opps = pattern_scanner_mod.list_opportunities(sort="profit")
    out = []
    for o in opps:
        ask = o.get("asking_price")
        profit = o.get("expected_profit")
        if not ask or profit is None:
            continue
        if (profit / ask) * 100.0 >= pattern_threshold:
            out.append(o)
    return out


def run_cycle(state: dict, roi_threshold: float, pattern_threshold: float) -> dict:
    tradeup_hits = scan_tradeups(roi_threshold)
    for r in tradeup_hits:
        key = f"tradeup:{r.collection}:{r.from_rarity}:{r.to_rarity}:{r.stattrak}"
        if _should_alert(state, key):
            title = f"Trade-up +{r.roi_pct:.0f}% ROI"
            body = (f"{r.collection}: {r.from_rarity} -> {r.to_rarity}"
                    f"{' (StatTrak)' if r.stattrak else ''}\n"
                    f"Cost ${r.cost:.2f} -> ${r.expected_revenue_after_fee:.2f} after fees "
                    f"(profit ${r.profit:+.2f})")
            alerts_mod.notify(_ascii(title), _ascii(body), priority="high", tags=["moneybag"])
            _mark_alerted(state, key)

    pattern_hits = scan_patterns(pattern_threshold)
    for o in pattern_hits:
        key = f"pattern:{o.get('listing_id')}"
        if _should_alert(state, key):
            roi = (o["expected_profit"] / o["asking_price"]) * 100.0
            title = f"Rare pattern +{roi:.0f}% under value"
            body = (f"{o.get('weapon')} | {o.get('skin')} ({o.get('wear')}) -- {o.get('pattern_label')}\n"
                    f"Ask ${o['asking_price']:.2f}, worth ~${o.get('estimated_collector_value'):.2f} "
                    f"on {o.get('marketplace')}")
            alerts_mod.notify(_ascii(title), _ascii(body), priority="high", tags=["gem"],
                               click_url=o.get("listing_url"))
            _mark_alerted(state, key)

    backup_result = backup_engine_mod.run_backup(force=False)

    sheets_result = {"ok": False, "reason": "not_configured"}
    if sheets_export_mod.configured():
        sheets_result = sheets_export_mod.export(tradeup_hits, pattern_hits)

    return {"n_tradeup_hits": len(tradeup_hits), "n_pattern_hits": len(pattern_hits),
            "backup": backup_result, "sheets": sheets_result}


def run_foreground(poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
                    roi_threshold: float = DEFAULT_ROI_THRESHOLD_PCT,
                    pattern_threshold: float = DEFAULT_PATTERN_THRESHOLD_PCT):
    """CLI entry point (`python -m src.main watch`) -- runs in the current thread, Ctrl+C to
    stop. Starts pattern_scanner's own scan loop in a background thread first (it starts PAUSED
    by default -- opt-in since it talks to external APIs -- so this explicitly resumes it),
    otherwise list_opportunities() would only ever see whatever a dashboard elsewhere already
    happened to have scanned."""
    pattern_state = pattern_scanner_mod.PatternScanState()
    pattern_state.pause_event.clear()
    threading.Thread(target=pattern_scanner_mod.scan_loop, args=(pattern_state,), daemon=True).start()

    print(f"Watch mode: trade-ups >= {roi_threshold:.0f}% ROI and rare patterns >= "
          f"{pattern_threshold:.0f}% under value trigger a push notification "
          f"({'ntfy configured' if alerts_mod.configured() else 'NTFY_TOPIC not set -- alerts disabled'}).\n"
          f"GitHub auto-backup: {'on' if backup_engine_mod.load_config().get('enabled') else 'off'}. "
          f"Google Sheets export: {'on' if sheets_export_mod.configured() else 'off'}.\n"
          f"Polling every {poll_interval:.0f}s. Ctrl+C to stop.\n")

    state = _load_state()
    try:
        while True:
            cycle_start = time.time()
            try:
                result = run_cycle(state, roi_threshold, pattern_threshold)
                _save_state(state)
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                      f"{result['n_tradeup_hits']} trade-up hit(s), {result['n_pattern_hits']} "
                      f"pattern hit(s). backup={result['backup']['action']} "
                      f"sheets={'ok' if result['sheets'].get('ok') else result['sheets'].get('reason')}")
            except Exception as e:  # noqa: BLE001 -- one bad cycle must never kill the watch loop
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] cycle error: {type(e).__name__}: {e}")
            elapsed = time.time() - cycle_start
            time.sleep(max(0.0, poll_interval - elapsed))
    except KeyboardInterrupt:
        print("\nStopped.")
        return
