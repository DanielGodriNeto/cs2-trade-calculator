"""Self-check for two phantom-ROI guards in src.simulate. No network, no data/ access: every
price lookup, invalid-skin write, override read and mispriced mark is mocked here.

1. A stale input price is never costed: it is refreshed live, or the unit is dropped (the
   candidate becomes incomplete_inputs). Its cost_basis_source is always "cache" now.
2. An ACTIVE overvalued mark on an OUTPUT skin replaces its inflated Steam price with the mark's
   cheapest_price, or contributes 0 revenue when that is missing (flagged). An undervalued INPUT
   is costed at its cheapest_price.

Run: python test_simulate_reliability.py
"""
from unittest import mock

from src import mispriced_skins as mispriced_mod
from src import simulate
from src.oddsmath import expected_revenue_full_odds
from src.random_trade import Candidate, CandidateLeg

IN_NAME = "Input Skin"
OUT_NAME = "Output Skin"
CURRENCY = 1

prices: dict = {}  # base skin name -> (data, freshness) served by the fake cache
live: dict = {}    # base skin name -> data returned by the fake live get_price
marks: list = []   # active mispriced-mark entries (same shape as mispriced_skins.list_mispriced)


def base(name: str) -> str:
    return name.split(" (")[0]


def fake_cached(name: str, currency: int = 1):
    return prices.get(base(name), (None, "missing"))


def fake_live(name: str, currency: int = 1, abort_check=None):
    return live.get(base(name))


def fake_list_mispriced(currency=None) -> list:
    return [dict(m) for m in marks if currency is None or m["currency"] == currency]


def fake_active_gap(name: str, currency: int):
    for m in marks:
        if base(m["market_hash_name"]) == base(name) and m["currency"] == currency:
            return m["gap_pct"]
    return None


def mark(name: str, gap_pct: float, cheapest_price: float | None) -> dict:
    return {"market_hash_name": f"{name} (Field-Tested)", "currency": CURRENCY, "active": True,
            "gap_pct": gap_pct, "cheapest_price": cheapest_price, "cheapest_source": "skinport",
            "steam_price": None, "marked_at": 0.0}


def quote(price: float, volume: int = 500) -> dict:
    return {"lowest_price": price, "median_price": price, "volume": volume}


def candidate() -> Candidate:
    in_skin = {"name": IN_NAME, "min_float": 0.15, "max_float": 0.38}
    out_skin = {"name": OUT_NAME, "min_float": 0.15, "max_float": 0.38}
    leg = CandidateLeg(collection="Test Collection",
                       units=[{"skin": in_skin, "wear": "Field-Tested", "count": 10}],
                       outputs=[out_skin])
    return Candidate(from_rarity_id="r1", from_rarity_name="Mil-Spec", to_rarity_id="r2",
                     to_rarity_name="Restricted", stattrak=False, legs=[leg])


def run():
    with mock.patch.multiple(simulate, get_cached_price=fake_cached, get_price=fake_live,
                             _thirdparty_prices=lambda *a, **k: {}), \
         mock.patch.multiple(simulate.invalid_skins_mod, clear_name=lambda *a, **k: False,
                             mark_invalid=lambda *a, **k: None), \
         mock.patch.multiple(simulate.user_overrides_mod, blacklisted_skins=lambda: set()), \
         mock.patch.multiple(simulate.bulk_prices_mod, has_market_listing=lambda name: None), \
         mock.patch.multiple(mispriced_mod, list_mispriced=fake_list_mispriced,
                             active_gap=fake_active_gap):
        return simulate.simulate(candidate(), currency=CURRENCY, live_fetch=True)


def reset(input_quote, output_quote, input_freshness="fresh"):
    prices.clear()
    live.clear()
    marks.clear()
    prices[IN_NAME] = (input_quote, input_freshness) if input_quote else (None, "missing")
    prices[OUT_NAME] = (output_quote, "fresh")


# 1a. Stale input, live refresh succeeds: costed at the FRESH price, basis "cache".
reset(quote(5.0), quote(100.0), input_freshness="stale")
live[IN_NAME] = quote(10.0)
rec = run()
assert isinstance(rec, simulate.SimulationRecord), rec
assert rec.cost == 100.0, f"stale 5.0 must not be costed; refreshed 10.0 x10 expected, got {rec.cost}"
assert rec.cost_basis_source == "cache", rec.cost_basis_source

# 1b. Stale input, live refresh fails: unit dropped -> incomplete_inputs, never a stale cost.
reset(quote(5.0), quote(100.0), input_freshness="stale")
rec = run()
assert isinstance(rec, simulate.InvalidSimulation), f"stale input must not be costed, got {rec}"
assert rec.reason == "incomplete_inputs", rec.reason

# 2a. Active overvalued OUTPUT mark: revenue uses the mark's cheapest_price, not Steam's 1000.
reset(quote(10.0), quote(1000.0))
marks.append(mark(OUT_NAME, 900.0, 100.0))
rec = run()
assert isinstance(rec, simulate.SimulationRecord), rec
assert rec.output_distribution[0]["price"] == 100.0, rec.output_distribution[0]
assert abs(rec.expected_revenue - 100.0) < 1e-6, rec.expected_revenue
assert "verified_overvalued" in rec.output_distribution[0]["dead_skin_reasons"]

# 2b. Overvalued OUTPUT mark with no cheapest_price: contributes 0 revenue, flagged.
reset(quote(10.0), quote(1000.0))
marks.append(mark(OUT_NAME, 900.0, None))
rec = run()
assert isinstance(rec, simulate.SimulationRecord), rec
assert rec.expected_revenue == 0.0, rec.expected_revenue
assert "verified_overvalued" in rec.output_distribution[0]["dead_skin_reasons"]

# 2c. Active UNDERvalued INPUT mark: cost uses the mark's cheapest_price (14), not Steam's 10.
reset(quote(10.0), quote(100.0))
marks.append(mark(IN_NAME, -50.0, 14.0))
rec = run()
assert isinstance(rec, simulate.SimulationRecord), rec
assert rec.cost == 140.0, rec.cost

# 2d. Overvalued OUTPUT mark but Steam BELOW the stored cheapest: revenue stays at Steam (50).
reset(quote(10.0), quote(50.0))
marks.append(mark(OUT_NAME, 900.0, 100.0))
rec = run()
assert isinstance(rec, simulate.SimulationRecord), rec
assert rec.output_distribution[0]["price"] == 50.0, rec.output_distribution[0]
assert abs(rec.expected_revenue - 50.0) < 1e-6, rec.expected_revenue

# 2e. Undervalued INPUT mark but Steam ABOVE the stored cheapest: cost stays at Steam (10 x10).
reset(quote(10.0), quote(100.0))
marks.append(mark(IN_NAME, -50.0, 8.0))
rec = run()
assert isinstance(rec, simulate.SimulationRecord), rec
assert rec.cost == 100.0, rec.cost

# 2f. Undervalued INPUT mark with NO cheapest_price: unit unusable -> incomplete_inputs,
# never costed at the low Steam price.
reset(quote(10.0), quote(100.0))
marks.append(mark(IN_NAME, -50.0, None))
rec = run()
assert isinstance(rec, simulate.InvalidSimulation), f"undervalued without cheapest must not be costed, got {rec}"
assert rec.reason == "incomplete_inputs", rec.reason

print("ok")
