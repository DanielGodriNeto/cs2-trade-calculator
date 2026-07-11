"""Pushes the current best-known trade-up + rare-pattern opportunities to a Google Sheet, live,
via a service account (no OAuth login popup, no re-auth ever -- the standard approach for an
unattended background process like watch_loop.py). This is IN ADDITION to xlsx_export.py's
local .xlsx workbook, not a replacement -- that one stays the richer, fuller export; this one is
deliberately a single lightweight "what's live right now" tab, cheap enough to overwrite every
watch_loop cycle without worrying about API quota (Sheets' default quota is 60 write requests/
user/minute, and one cycle here is a small number of batched calls).

gspread is imported lazily inside export(), matching the same optional-dependency convention as
openpyxl in xlsx_export.py and scikit-learn in learning.py -- nothing else in this project needs
it installed, and `pip install` isn't required until Sheets export is actually configured/used.

Setup (one-time, done by the user in their own Google account -- see README):
  1. Create a Google Cloud project, enable the "Google Sheets API".
  2. Create a service account, download its JSON key.
  3. Create (or reuse) a Google Sheet, share it with the service account's email
     (client_email in the JSON key) as Editor.
  4. Set env vars GOOGLE_SERVICE_ACCOUNT_FILE (path to the JSON key) and GOOGLE_SHEET_ID (the
     long id in the sheet's URL between /d/ and /edit).

SECURITY: keep the JSON key file OUTSIDE this project's data/ directory (and ideally outside the
repo entirely, e.g. your home directory). backup_engine.py's auto-backup zips EVERYTHING under
data/ and pushes it to GitHub -- a key placed there would get committed and pushed as a
side-effect the very next watch_loop cycle.

Unset either env var and export()/configured() just report "not configured" -- never an error,
same convention as CSFLOAT_API_KEY/NTFY_TOPIC elsewhere in this project.
"""
import os
import time
from datetime import datetime, timezone

GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

TRADEUPS_TAB = "Live Trade-Ups"
PATTERNS_TAB = "Live Rare Patterns"
SUMMARY_TAB = "Summary"

TRADEUP_HEADERS = ["collection", "from_rarity", "to_rarity", "stattrak", "cost", "cheapest_input",
                    "expected_revenue_after_fee", "profit", "roi_pct"]
PATTERN_HEADERS = ["weapon", "skin", "wear", "pattern_label", "pattern_tier", "marketplace",
                    "asking_price", "estimated_collector_value", "expected_profit", "edge_pct",
                    "roi_pct", "listing_url"]


def configured() -> bool:
    return bool(GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEET_ID)


def _client():
    import gspread
    return gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_FILE)


def _get_or_create_tab(spreadsheet, title: str, n_cols: int):
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=200, cols=max(n_cols, 10))


def export(tradeup_results: list, pattern_opportunities: list[dict]) -> dict:
    """tradeup_results: list[tradeup.TradeUpResult] already filtered to whatever's worth
    showing (watch_loop passes its roi_pct>=ALERT threshold subset). pattern_opportunities:
    list of pattern_scanner opportunity dicts, same deal. Overwrites both tabs wholesale each
    call -- simplest correct behavior for a "what's live right now" view; the append-only
    history lives in data/*.jsonl and xlsx_export.py already, not here.

    Returns a JSON-safe result dict; never raises -- a Sheets outage must not kill the watch
    loop's other work (alerts, backup)."""
    if not configured():
        return {"ok": False, "reason": "not_configured"}
    try:
        client = _client()
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

        tu_ws = _get_or_create_tab(spreadsheet, TRADEUPS_TAB, len(TRADEUP_HEADERS))
        tu_rows = [TRADEUP_HEADERS] + [
            [r.collection, r.from_rarity, r.to_rarity, r.stattrak, round(r.cost, 2),
             r.cheapest_input, round(r.expected_revenue_after_fee, 2), round(r.profit, 2),
             round(r.roi_pct, 1)]
            for r in tradeup_results
        ]
        tu_ws.clear()
        tu_ws.update(values=tu_rows, range_name="A1")

        pat_ws = _get_or_create_tab(spreadsheet, PATTERNS_TAB, len(PATTERN_HEADERS))
        pat_rows = [PATTERN_HEADERS] + [
            [o.get("weapon"), o.get("skin"), o.get("wear"), o.get("pattern_label"),
             o.get("pattern_tier"), o.get("marketplace"), o.get("asking_price"),
             o.get("estimated_collector_value"), o.get("expected_profit"), o.get("edge_pct"),
             (round(o["expected_profit"] / o["asking_price"] * 100, 1)
              if o.get("asking_price") else None),
             o.get("listing_url")]
            for o in pattern_opportunities
        ]
        pat_ws.clear()
        pat_ws.update(values=pat_rows, range_name="A1")

        summary_ws = _get_or_create_tab(spreadsheet, SUMMARY_TAB, 2)
        now = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        summary_ws.update(values=[
            ["Last updated (UTC)", now],
            ["Live trade-ups shown", len(tradeup_results)],
            ["Live rare patterns shown", len(pattern_opportunities)],
        ], range_name="A1")

        return {"ok": True, "n_tradeups": len(tradeup_results), "n_patterns": len(pattern_opportunities),
                "updated_at": time.time()}
    except Exception as e:  # noqa: BLE001 -- a Sheets failure must never kill the watch loop
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
