"""Append-only log of trade-up outcomes the user actually executed, entered manually after the
fact (this tool never auto-buys or auto-sells -- see README's "Why it doesn't auto-buy"). Used
as training data for the optional ML calibration model in learning.py.

Lives in data/ (NOT cache/): cache/ is disposable, TTL'd, regenerable data (skins.json,
price_cache.json, ...) that a "clear cache" operation could reasonably wipe. This log is the
opposite -- hand-entered, irreplaceable record of real trades -- so it deliberately lives in a
sibling directory a cache-clearing helper would never touch.

Format is JSON Lines (one JSON object per line), not a single JSON array: outcomes are appended
one at a time, so a plain append (open in "a" mode) never needs to read-modify-write the whole
history, and a corrupt trailing line (e.g. the process died mid-write) can be skipped on read
without losing every prior entry.
"""
from dataclasses import dataclass, asdict
import json
import random
import string
import threading
import time
from pathlib import Path

from . import oddsmath
from .tradeup import STEAM_SELL_FEE

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTCOMES_FILE = DATA_DIR / "outcomes.jsonl"

STATUSES = ("sold", "held", "kept")

# Serializes appends within this process, mirroring steam_prices.py's _stats_lock pattern.
# Does NOT protect against a second OS process (e.g. the CLI and the web server both logging
# an outcome at the same instant) -- same class of gap price_cache.json already has today, not
# newly introduced here. Documented, not silently assumed away.
_write_lock = threading.Lock()


@dataclass
class Outcome:
    id: str
    logged_at: float
    collection: str
    kind: str  # "collection" | "case"
    stattrak: bool
    from_rarity: str
    to_rarity: str
    currency: int
    input_mix: list             # [{"skin","wear","count","unit_price_paid"}, ...]
    filler_collection: str | None
    filler_input: dict | None   # {"skin","wear","count","unit_price_paid"} | None
    output_skin: str
    output_wear: str
    sale_price: float | None
    status: str                 # "sold" | "held" | "kept"
    cost_paid: float
    profit: float | None
    roi_pct: float | None
    win: bool | None
    predicted_roi_pct: float | None
    predicted_revenue: float | None
    ml_predicted_roi_pct: float | None
    output_float: float | None = None  # exact float (e.g. 0.95) if that's what was logged instead
    # of a wear-bracket name -- see resolve_wear(). Defaults to None so old logs (pre-dating this
    # field) still load fine via load_outcomes()'s Outcome(**json.loads(line)).
    notes: str = ""


def resolve_wear(raw) -> tuple[str, float | None]:
    """Accepts either a wear-bracket name (e.g. "Field-Tested") or a float -- as a number or a
    numeric string like "0.95" -- and returns (canonical_wear_name, float_value_or_None).
    Numeric input is rounded to 2 decimal places and mapped to its standard wear bracket via
    oddsmath.wear_from_float(), so you can log the exact float you saw on a listing (CSFloat, the
    in-game inspect link) instead of eyeballing which of the five bracket names it falls into.
    Non-numeric input (a wear name, or anything else) passes through unchanged with float=None --
    this keeps every existing caller (CLI, old logged data) working exactly as before."""
    try:
        value = round(float(raw), 2)
    except (TypeError, ValueError):
        return str(raw), None
    if not (0.0 <= value <= 1.0):
        return str(raw), None  # not a plausible float -- treat as a literal (if odd) wear name
    return oddsmath.wear_from_float(value), value


def _resolve_item_wear(item: dict) -> dict:
    wear_name, float_value = resolve_wear(item["wear"])
    return {**item, "wear": wear_name, "float": float_value}


def _new_id() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime())}-{suffix}"


def _compute_financials(input_mix: list, filler_input: dict | None, sale_price: float | None,
                         status: str) -> tuple[float, float | None, float | None, bool | None]:
    """Returns (cost_paid, profit, roi_pct, win). profit/roi_pct/win stay None until the
    outcome is actually sold -- a "held"/"kept" craft has a real cost but no realized return yet."""
    cost = sum(item["unit_price_paid"] * item["count"] for item in input_mix)
    if filler_input:
        cost += filler_input["unit_price_paid"] * filler_input["count"]
    if status != "sold" or sale_price is None:
        return cost, None, None, None
    after_fee = sale_price * (1 - STEAM_SELL_FEE)
    profit = after_fee - cost
    roi = (profit / cost * 100) if cost else 0.0
    return cost, profit, roi, profit > 0


def log_outcome(*, collection: str, kind: str, stattrak: bool, from_rarity: str, to_rarity: str,
                 currency: int, input_mix: list, output_skin: str, output_wear: str,
                 filler_collection: str | None = None, filler_input: dict | None = None,
                 sale_price: float | None = None, status: str = "sold",
                 predicted_roi_pct: float | None = None, predicted_revenue: float | None = None,
                 ml_predicted_roi_pct: float | None = None, notes: str = "") -> Outcome:
    """Validates, computes cost/profit/roi/win, appends one JSON line, returns the Outcome."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    if not input_mix:
        raise ValueError("input_mix must have at least one entry")
    if status == "sold" and sale_price is None:
        raise ValueError("sale_price is required when status is 'sold'")
    for item in input_mix:
        if not all(k in item for k in ("skin", "wear", "count", "unit_price_paid")):
            raise ValueError("each input_mix entry needs skin/wear/count/unit_price_paid")
    if filler_input and not all(k in filler_input for k in ("skin", "wear", "count", "unit_price_paid")):
        raise ValueError("filler_input needs skin/wear/count/unit_price_paid")

    # Each "wear" can be a bracket name ("Field-Tested") or an exact float ("0.95") -- resolve
    # to a canonical wear name (what the rest of the codebase, e.g. learning.py's feature
    # extraction, expects) plus the precise float if one was given. See resolve_wear().
    input_mix = [_resolve_item_wear(item) for item in input_mix]
    if filler_input:
        filler_input = _resolve_item_wear(filler_input)
    output_wear, output_float = resolve_wear(output_wear)

    cost, profit, roi, win = _compute_financials(input_mix, filler_input, sale_price, status)
    outcome = Outcome(
        id=_new_id(), logged_at=time.time(), collection=collection, kind=kind, stattrak=stattrak,
        from_rarity=from_rarity, to_rarity=to_rarity, currency=currency, input_mix=input_mix,
        filler_collection=filler_collection, filler_input=filler_input, output_skin=output_skin,
        output_wear=output_wear, sale_price=sale_price, status=status, cost_paid=cost, profit=profit,
        roi_pct=roi, win=win, predicted_roi_pct=predicted_roi_pct, predicted_revenue=predicted_revenue,
        ml_predicted_roi_pct=ml_predicted_roi_pct, output_float=output_float, notes=notes,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(outcome))
    with _write_lock:
        with open(OUTCOMES_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return outcome


def load_outcomes() -> list[Outcome]:
    """Reads every logged outcome. Skips (rather than raises on) any corrupt trailing line --
    e.g. a process that died mid-write -- so one bad line doesn't lose the whole history."""
    if not OUTCOMES_FILE.exists():
        return []
    out = []
    with open(OUTCOMES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Outcome(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # corrupt/partial line -- skip, don't lose the rest of the log
    return out


def update_outcome_sale(outcome_id: str, sale_price: float) -> Outcome | None:
    """For a 'held'/'kept' outcome logged before resale -- rewrites the whole file with that
    one entry's sale_price/status/profit/roi/win filled in. A full read-modify-write is
    deliberate and rare here (user-triggered edit, not a hot path) -- the only place this
    module does one, unlike log_outcome's plain append."""
    outcomes = load_outcomes()
    updated = None
    with _write_lock:
        for o in outcomes:
            if o.id == outcome_id:
                o.status = "sold"
                o.sale_price = sale_price
                o.cost_paid, o.profit, o.roi_pct, o.win = _compute_financials(
                    o.input_mix, o.filler_input, sale_price, "sold")
                updated = o
        if updated:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(OUTCOMES_FILE, "w", encoding="utf-8") as f:
                for o in outcomes:
                    f.write(json.dumps(asdict(o)) + "\n")
    return updated


def stats() -> dict:
    """Summary for the web dashboard's model-status indicator. Recomputed from load_outcomes()
    on every call -- no caching needed at this data scale (hundreds, not millions, of rows)."""
    outcomes = load_outcomes()
    sold = [o for o in outcomes if o.status == "sold"]
    wins = [o for o in sold if o.win]
    return {
        "n_total": len(outcomes),
        "n_sold": len(sold),
        "n_wins": len(wins),
        "n_losses": len(sold) - len(wins),
        "win_rate_pct": (len(wins) / len(sold) * 100) if sold else None,
    }
