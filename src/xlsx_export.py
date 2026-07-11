"""Builds a single, richly formatted local .xlsx workbook covering as much of this dashboard's
guided-simulation population data as possible -- the substitute for a "create a Google Sheet"
button (the user opted for a local file over wiring up Google Sheets API credentials/OAuth, see
requirements.txt). Read-only, like every other module in this tool: never writes back into any
of the sources it reads from.

openpyxl is imported lazily inside build_workbook(), not at module load, matching the same
optional-dependency convention as learning.py/sim_learning.py's sklearn imports -- importing
this module costs nothing until an export is actually requested.

Sheets (the first three are the Stage-4 "premium trading workstation" trio; the rest are the
original full-detail sheets, unchanged):
  1. 📊 Executive Dashboard   -- KPI scorecards (capital dispatched / net realized ROI /
     high-confidence opportunity count) + three native charts: ROI distribution histogram,
     cumulative arbitrage curve, Healthy/Suspicious/Excluded vulnerability doughnut.
  2. 🚀 Live Opportunities    -- top 50 ranked live arbitrage vectors (trade-ups AND collector
     pattern opportunities, each tagged), charcoal/slate-navy headers, zebra striping,
     tier/wear badge fills, gradient formatting on ROI + confidence.
  3. 🛡 Reliability & Audit   -- every tracked skin's reliability state, 24h re-verification
     countdown, and the variance metrics that triggered its current flag; crimson/orange
     fills on Excluded/Suspicious rows.
  4. Opportunities  -- sim_loop.list_all_opportunities(), the FULL uncapped archive of every
     unique trade-up composition ever simulated (see that function's docstring for why this
     differs from the dashboard's top-50-per-agent table).
  5. Agents         -- one row per son: round/simulated/trainable/low-volume/invalid/best ROI/
     stuck flag/temperature/cumulative bankroll, plus two bar charts.
  6. Agent history   -- round-by-round best-ROI-so-far / cumulative expected profit per agent,
     straight from each son's checkpoint `history`.
  7. Leaderboards    -- most_profitable_collections()/most_profitable_skins(), uncapped.
  8. Model quality    -- the real ROI-calibration model's (learning.py) status/feature
     importances, plus every agent's own sim-model (sim_learning.py) status.
"""
import time
from datetime import datetime, timezone

from . import learning as learning_mod
from . import market_reliability as market_reliability_mod
from . import outcomes as outcomes_mod
from . import pattern_scanner as pattern_scanner_mod
from . import sim_loop as sim_loop_mod

HEADER_FILL = "1F2937"
HEADER_FONT_COLOR = "FFFFFF"
TAB_COLORS = ["4C6EF5", "2F9E44", "E8590C", "AE3EC9", "1098AD"]

# Premium-trio palette (per the Stage-4 spec's design language).
PREMIUM_HEADER_FILL = "1A2530"   # charcoal / slate navy
ZEBRA_FILL = "F8F9FA"            # ultra-light gray zebra striping
KPI_FILL = "111827"
KPI_FONT_COLOR = "F9FAFB"
BADGE_MINT = "D1FAE5"            # Factory New / healthy accents
BADGE_GOLD = "FDE68A"            # Tier 1 patterns
BADGE_SLATE = "E2E8F0"           # other badges
ALERT_CRIMSON = "FADBD8"         # Excluded rows
ALERT_ORANGE = "FDEBD0"          # Suspicious rows


def _fmt_ts(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _style_header_row(ws, row_idx: int, n_cols: int) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=row_idx, column=col)
        cell.font = Font(bold=True, color=HEADER_FONT_COLOR)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row_idx].height = 20


def _autosize(ws, n_cols: int, sample_rows: int = 300) -> None:
    from openpyxl.utils import get_column_letter
    for col in range(1, n_cols + 1):
        letter = get_column_letter(col)
        cells = ws[letter][:sample_rows]
        max_len = max((len(str(c.value)) for c in cells if c.value is not None), default=0)
        ws.column_dimensions[letter].width = min(46, max(10, max_len + 2))


def _write_table(ws, headers: list, rows: list, start_row: int = 1) -> int:
    """Writes a header row (styled) + data rows starting at start_row; returns the last row
    index written (start_row + len(rows)), so callers can stack more content/charts below it."""
    for j, h in enumerate(headers, 1):
        ws.cell(row=start_row, column=j, value=h)
    _style_header_row(ws, start_row, len(headers))
    for i, row in enumerate(rows, start_row + 1):
        for j, v in enumerate(row, 1):
            ws.cell(row=i, column=j, value=v)
    _autosize(ws, len(headers))
    if rows:
        ws.freeze_panes = ws.cell(row=start_row + 1, column=1).coordinate
    return start_row + len(rows)


def _add_roi_color_scale(ws, col_letter: str, first_row: int, last_row: int) -> None:
    from openpyxl.formatting.rule import ColorScaleRule
    if last_row < first_row:
        return
    rule = ColorScaleRule(start_type="min", start_color="F8696B",
                           mid_type="percentile", mid_value=50, mid_color="FFEB84",
                           end_type="max", end_color="63BE7B")
    ws.conditional_formatting.add(f"{col_letter}{first_row}:{col_letter}{last_row}", rule)


def _kpi_card(ws, row: int, col_start: int, col_end: int, label: str, value) -> None:
    """One merged, centered, high-contrast KPI scorecard block (spec: oversized bold type)."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    ws.merge_cells(start_row=row, start_column=col_start, end_row=row, end_column=col_end)
    ws.merge_cells(start_row=row + 1, start_column=col_start, end_row=row + 2, end_column=col_end)
    label_cell = ws.cell(row=row, column=col_start, value=label)
    value_cell = ws.cell(row=row + 1, column=col_start, value=value)
    for r in range(row, row + 3):
        for c in range(col_start, col_end + 1):
            ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=KPI_FILL)
    label_cell.font = Font(bold=True, size=10, color="9CA3AF")
    label_cell.alignment = Alignment(horizontal="center", vertical="center")
    value_cell.font = Font(bold=True, size=22, color=KPI_FONT_COLOR)
    value_cell.alignment = Alignment(horizontal="center", vertical="center")
    for c in range(col_start, col_end + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13


def _style_premium_header(ws, row_idx: int, n_cols: int) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=row_idx, column=col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=PREMIUM_HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row_idx].height = 22


def _add_executive_dashboard(wb, snaps: list, opportunities: list) -> None:
    """Sheet 1: 📊 Executive Dashboard -- see module docstring."""
    from openpyxl.chart import BarChart, DoughnutChart, LineChart, Reference
    from openpyxl.styles import Font

    ws = wb.create_sheet("📊 Executive Dashboard", 0)
    ws.sheet_properties.tabColor = PREMIUM_HEADER_FILL
    ws.sheet_view.showGridLines = False
    ws.cell(row=1, column=1, value="CS2 Trading Workstation — Executive Dashboard").font = \
        Font(bold=True, size=16)
    ws.cell(row=2, column=1,
            value=f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}").font = \
        Font(size=9, color="6B7280")

    # KPI scorecards.
    total_cost = sum(s.get("cumulative_cost") or 0 for s in snaps)
    sold = [o for o in outcomes_mod.load_outcomes() if o.status == "sold"] \
        if outcomes_mod.OUTCOMES_FILE.exists() else []
    realized_cost = sum(o.cost_paid for o in sold)
    realized_profit = sum(o.profit or 0 for o in sold)
    net_roi = (realized_profit / realized_cost * 100.0) if realized_cost else None
    high_conf = sum(1 for o in opportunities if (o.get("confidence_score") or 0) >= 70)
    _kpi_card(ws, 4, 1, 3, "TOTAL CAPITAL DISPATCHED (SIM)", f"${total_cost:,.0f}")
    _kpi_card(ws, 4, 5, 7, "NET REALIZED ROI %",
              f"{net_roi:+.1f}%" if net_roi is not None else "— (no sold outcomes)")
    _kpi_card(ws, 4, 9, 11, "HIGH-CONFIDENCE OPPORTUNITIES", high_conf)

    # ROI distribution histogram (volume of configurations per profitability bucket).
    bins = [(-100, 0), (0, 5), (5, 10), (10, 20), (20, 40), (40, 80), (80, 10_000)]
    labels = ["<0%", "0-5%", "5-10%", "10-20%", "20-40%", "40-80%", "80%+"]
    counts = [0] * len(bins)
    for o in opportunities:
        roi = o.get("expected_roi_pct")
        if roi is None:
            continue
        for i, (lo, hi) in enumerate(bins):
            if lo <= roi < hi:
                counts[i] += 1
                break
    ws.cell(row=9, column=1, value="ROI bucket").font = Font(bold=True, size=9)
    ws.cell(row=9, column=2, value="Configurations").font = Font(bold=True, size=9)
    for i, (label, count) in enumerate(zip(labels, counts), 10):
        ws.cell(row=i, column=1, value=label)
        ws.cell(row=i, column=2, value=count)
    hist = BarChart()
    hist.type = "col"
    hist.title = "ROI Distribution (live opportunity archive)"
    hist.legend = None
    hist.add_data(Reference(ws, min_col=2, min_row=9, max_row=9 + len(bins)), titles_from_data=True)
    hist.set_categories(Reference(ws, min_col=1, min_row=10, max_row=9 + len(bins)))
    hist.height, hist.width = 7.5, 11
    ws.add_chart(hist, "D9")

    # Cumulative arbitrage curve: realized profit over time when real outcomes exist,
    # otherwise the population's cumulative EXPECTED profit history (clearly labeled).
    curve_rows = []
    if sold:
        running = 0.0
        for o in sorted(sold, key=lambda o: o.logged_at):
            running += o.profit or 0.0
            curve_rows.append((datetime.fromtimestamp(o.logged_at, tz=timezone.utc)
                               .strftime("%m-%d %H:%M"), round(running, 2)))
        curve_title = "Cumulative Realized Profit"
    else:
        merged: dict = {}
        for s in snaps:
            for h in s.get("history") or []:
                merged[h["at"]] = merged.get(h["at"], 0.0) + (h.get("cumulative_expected_profit") or 0.0)
        for ts in sorted(merged)[-60:]:
            curve_rows.append((datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M"),
                               round(merged[ts], 2)))
        curve_title = "Cumulative EXPECTED Profit (simulated, nothing bought)"
    if curve_rows:
        base = 9 + len(bins) + 3
        ws.cell(row=base - 1, column=1, value="Time").font = Font(bold=True, size=9)
        ws.cell(row=base - 1, column=2, value="Cumulative").font = Font(bold=True, size=9)
        for i, (label, v) in enumerate(curve_rows, base):
            ws.cell(row=i, column=1, value=label)
            ws.cell(row=i, column=2, value=v)
        curve = LineChart()
        curve.title = curve_title
        curve.legend = None
        curve.smooth = True
        curve.add_data(Reference(ws, min_col=2, min_row=base - 1, max_row=base - 1 + len(curve_rows)),
                        titles_from_data=True)
        curve.set_categories(Reference(ws, min_col=1, min_row=base, max_row=base - 1 + len(curve_rows)))
        curve.height, curve.width = 7.5, 11
        ws.add_chart(curve, "D26")

    # Market vulnerability breakdown (Healthy / Suspicious / Excluded doughnut).
    counts_by_state = market_reliability_mod.state_counts()
    ws.cell(row=9, column=14, value="State").font = Font(bold=True, size=9)
    ws.cell(row=9, column=15, value="Skins").font = Font(bold=True, size=9)
    for i, state in enumerate(("healthy", "suspicious", "excluded"), 10):
        ws.cell(row=i, column=14, value=state.capitalize())
        ws.cell(row=i, column=15, value=counts_by_state[state])
    doughnut = DoughnutChart()
    doughnut.title = "Market Vulnerability Breakdown"
    doughnut.add_data(Reference(ws, min_col=15, min_row=9, max_row=12), titles_from_data=True)
    doughnut.set_categories(Reference(ws, min_col=14, min_row=10, max_row=12))
    doughnut.height, doughnut.width = 7.5, 9
    ws.add_chart(doughnut, "P9")


def _add_live_opportunities_matrix(wb, opportunities: list) -> None:
    """Sheet 2: 🚀 Live Opportunities -- top 50 ranked arbitrage vectors (trade-ups + collector
    pattern finds), premium design language per spec."""
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Alignment, Font, PatternFill

    ws = wb.create_sheet("🚀 Live Opportunities", 1)
    ws.sheet_properties.tabColor = "2F9E44"
    ws.sheet_view.showGridLines = False

    rows = []
    for o in opportunities[:50]:
        rows.append({"kind": "Trade-Up", "asset": f"{o.get('primary_collection')} "
                                                   f"({o.get('from_rarity')} → {o.get('to_rarity')})",
                     "pattern": "—", "wear": "mixed",
                     "ask": o.get("cost"), "premium": None,
                     "roi": o.get("expected_roi_pct"),
                     "confidence": o.get("confidence_score"),
                     "tier": None, "link": None})
    for p in pattern_scanner_mod.list_opportunities()[:50]:
        rows.append({"kind": "Collector Arbitrage", "asset": p.get("market_hash_name"),
                     "pattern": f"#{p.get('paint_seed')} ({p.get('pattern_label')})"
                                if p.get("paint_seed") is not None else p.get("pattern_label"),
                     "wear": p.get("wear") or "?",
                     "ask": p.get("asking_price"), "premium": p.get("estimated_collector_value"),
                     "roi": (p.get("expected_profit") / p["asking_price"] * 100.0)
                            if p.get("asking_price") else None,
                     "confidence": {"high": 90, "medium": 65, "low": 35}.get(p.get("confidence")),
                     "tier": p.get("pattern_tier"), "link": p.get("listing_url")})
    rows.sort(key=lambda r: r.get("roi") or 0, reverse=True)
    rows = rows[:50]

    headers = ["Type", "Market Asset", "Pattern / Seed", "Target Wear", "Ask Price",
               "Est. Premium", "Expected ROI %", "Confidence Score", "Action Link"]
    for j, h in enumerate(headers, 1):
        ws.cell(row=1, column=j, value=h)
    _style_premium_header(ws, 1, len(headers))
    zebra = PatternFill("solid", fgColor=ZEBRA_FILL)
    mint = PatternFill("solid", fgColor=BADGE_MINT)
    gold = PatternFill("solid", fgColor=BADGE_GOLD)
    slate = PatternFill("solid", fgColor=BADGE_SLATE)
    for i, r in enumerate(rows, 2):
        values = [r["kind"], r["asset"], r["pattern"], r["wear"],
                  round(r["ask"], 2) if r["ask"] is not None else None,
                  round(r["premium"], 2) if r["premium"] is not None else None,
                  round(r["roi"], 1) if r["roi"] is not None else None,
                  round(r["confidence"], 0) if r["confidence"] is not None else None,
                  "View Listing ↗" if r["link"] else None]
        for j, v in enumerate(values, 1):
            cell = ws.cell(row=i, column=j, value=v)
            if i % 2 == 0:
                cell.fill = zebra
        # Micro-visual badge fills (spec: mint for Factory New, soft gold for Tier 1).
        if r["wear"] == "Factory New":
            ws.cell(row=i, column=4).fill = mint
        elif r["wear"] not in (None, "?", "mixed"):
            ws.cell(row=i, column=4).fill = slate
        if r["tier"] == 1:
            ws.cell(row=i, column=3).fill = gold
        elif r["tier"] in (2, 3):
            ws.cell(row=i, column=3).fill = slate
        ws.cell(row=i, column=3).alignment = Alignment(horizontal="center")
        ws.cell(row=i, column=4).alignment = Alignment(horizontal="center")
        if r["link"]:
            link_cell = ws.cell(row=i, column=9)
            link_cell.hyperlink = r["link"]
            link_cell.font = Font(color="1D4ED8", underline="single")
    last = 1 + len(rows)
    if rows:
        # Real-time spread alerts: gradient emerald (>= +20%) down through amber (thin/volatile).
        ws.conditional_formatting.add(f"G2:G{last}", ColorScaleRule(
            start_type="num", start_value=0, start_color="F5B041",
            mid_type="num", mid_value=20, mid_color="ABEBC6",
            end_type="max", end_color="1E8449"))
        ws.conditional_formatting.add(f"H2:H{last}", ColorScaleRule(
            start_type="min", start_color="F5B041",
            mid_type="percentile", mid_value=50, mid_color="FCF3CF",
            end_type="max", end_color="1E8449"))
        ws.freeze_panes = "A2"
    _autosize(ws, len(headers))


def _add_reliability_audit_registry(wb) -> None:
    """Sheet 3: 🛡 Reliability & Audit -- asset health, anomaly triggers, 24h re-verification
    countdowns, crimson/orange visual guardrails per spec."""
    from openpyxl.styles import Font, PatternFill

    ws = wb.create_sheet("🛡 Reliability & Audit", 2)
    ws.sheet_properties.tabColor = "C0392B"
    ws.sheet_view.showGridLines = False
    tracked = market_reliability_mod.list_tracked()
    headers = ["Asset", "State", "Reliability Score", "Trigger Notes",
               "Rolling Z-Score", "In State Since", "Last Checked",
               "24h Re-verification Countdown", "Manual Override"]
    for j, h in enumerate(headers, 1):
        ws.cell(row=1, column=j, value=h)
    _style_premium_header(ws, 1, len(headers))
    crimson = PatternFill("solid", fgColor=ALERT_CRIMSON)
    orange = PatternFill("solid", fgColor=ALERT_ORANGE)
    zebra = PatternFill("solid", fgColor=ZEBRA_FILL)
    now = time.time()
    for i, t in enumerate(tracked, 2):
        since = t.get("since") or t.get("computed_at")
        countdown_s = max(0.0, market_reliability_mod.RECHECK_SECONDS - (now - since)) \
            if (since and t["state"] == "excluded") else None
        notes = ", ".join(t.get("reasons") or [])
        if t.get("rolling_z") is not None:
            notes += (f" | z={t['rolling_z']:.2f} vs suspicious>="
                      f"{market_reliability_mod.Z_SCORE_SUSPICIOUS}, exclude>="
                      f"{market_reliability_mod.Z_SCORE_EXCLUDE}")
        values = [t["market_hash_name"], t["state"].capitalize(),
                  round(t["reliability_score"], 1), notes,
                  round(t["rolling_z"], 2) if t.get("rolling_z") is not None else None,
                  _fmt_ts(since), _fmt_ts(t.get("computed_at")),
                  (f"{countdown_s / 3600:.1f}h until auto re-verification"
                   if countdown_s else ("re-verified continuously" if t["state"] != "excluded"
                                         else "eligible now")),
                  t.get("override_note") or ("manual" if t.get("overridden") else "—")]
        fill = crimson if t["state"] == "excluded" else orange if t["state"] == "suspicious" \
            else (zebra if i % 2 == 0 else None)
        for j, v in enumerate(values, 1):
            cell = ws.cell(row=i, column=j, value=v)
            if fill is not None:
                cell.fill = fill
        if t["state"] == "excluded":
            ws.cell(row=i, column=2).font = Font(bold=True, color="943126")
        elif t["state"] == "suspicious":
            ws.cell(row=i, column=2).font = Font(bold=True, color="9C640C")
    if tracked:
        ws.freeze_panes = "A2"
    _autosize(ws, len(headers))


def build_workbook(sim_population: list, agent_names: dict) -> bytes:
    """sim_population is the live list of sim_loop.SimulationLoopState instances the server
    already holds (so per-agent status/history come from .snapshot(), zero extra disk reads
    beyond what list_all_opportunities()/most_profitable_*() need). Returns the .xlsx file's raw
    bytes, ready to write straight to an HTTP response body."""
    import io
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    snaps = [s.snapshot() for s in sim_population]

    # ---- Sheet: every trade-up opportunity ever simulated (uncapped archive) ----
    ws = wb.active
    ws.title = "Opportunities"
    ws.sheet_properties.tabColor = TAB_COLORS[0]
    opportunities = sim_loop_mod.list_all_opportunities()
    headers = ["Agent", "Collection", "From rarity", "To rarity", "StatTrak", "Cost",
               "Expected ROI %", "Verified ROI %", "Composite score", "Confidence score",
               "Price basis", "Discovered (id)"]
    rows = [[
        agent_names.get(str(o.get("son")), agent_names.get(o.get("son"), f"Son {o.get('son')}")),
        o.get("primary_collection"), o.get("from_rarity"), o.get("to_rarity"),
        "Yes" if o.get("stattrak") else "No", round(o.get("cost") or 0, 2),
        round(o.get("expected_roi_pct") or 0, 2),
        round(o["verified_roi_pct"], 2) if o.get("verified_roi_pct") is not None else None,
        round(o.get("composite_score") or 0, 1), round(o.get("confidence_score") or 0, 1),
        o.get("cost_basis_source"), o.get("id"),
    ] for o in opportunities]
    last_row = _write_table(ws, headers, rows)
    if rows:
        ws.auto_filter.ref = f"A1:{ws.cell(row=last_row, column=len(headers)).coordinate}"
        _add_roi_color_scale(ws, "G", 2, last_row)

    # ---- Sheet 2: agent population snapshot + comparison charts ----
    ws2 = wb.create_sheet("Agents")
    ws2.sheet_properties.tabColor = TAB_COLORS[1]
    headers2 = ["Agent", "Status", "Round", "Simulated", "Trainable", "Low-volume", "Invalid",
                "Best ROI ever %", "Rounds since improvement", "Stuck", "Temperature",
                "Cumulative cost", "Cumulative expected profit"]
    rows2 = [[
        s.get("agent_name"), s.get("status"), s.get("round"), s.get("n_simulated"),
        s.get("n_trainable"), s.get("n_low_volume"), s.get("n_invalid"),
        round(s["best_roi_ever"], 2) if s.get("best_roi_ever") is not None else None,
        s.get("rounds_since_improvement"), "Yes" if s.get("stuck") else "No",
        round(s.get("effective_temperature") or 0, 3),
        round(s.get("cumulative_cost") or 0, 2), round(s.get("cumulative_expected_profit") or 0, 2),
    ] for s in snaps]
    last_row2 = _write_table(ws2, headers2, rows2)

    if rows2:
        chart = BarChart()
        chart.title = "Best ROI ever by agent (%)"
        chart.y_axis.title = "ROI %"
        chart.add_data(Reference(ws2, min_col=8, min_row=1, max_row=last_row2), titles_from_data=True)
        chart.set_categories(Reference(ws2, min_col=1, min_row=2, max_row=last_row2))
        chart.height, chart.width = 8, 16
        ws2.add_chart(chart, f"A{last_row2 + 3}")

        chart2 = BarChart()
        chart2.type = "col"
        chart2.grouping = "stacked"
        chart2.overlap = 100
        chart2.title = "Training data quality by agent"
        chart2.add_data(Reference(ws2, min_col=5, max_col=7, min_row=1, max_row=last_row2), titles_from_data=True)
        chart2.set_categories(Reference(ws2, min_col=1, min_row=2, max_row=last_row2))
        chart2.height, chart2.width = 8, 16
        ws2.add_chart(chart2, f"A{last_row2 + 20}")

    # ---- Sheet 3: round-by-round agent history ----
    ws3 = wb.create_sheet("Agent history")
    ws3.sheet_properties.tabColor = TAB_COLORS[2]
    headers3 = ["Agent", "Round", "Best ROI ever %", "Avg ROI % this round",
                "Cumulative expected profit"]
    rows3 = []
    for s in snaps:
        for h in s.get("history") or []:
            rows3.append([s.get("agent_name"), h.get("round"), h.get("best_roi_ever"),
                          h.get("avg_roi_pct"), h.get("cumulative_expected_profit")])
    _write_table(ws3, headers3, rows3)

    # ---- Sheet 4: leaderboards (uncapped) ----
    ws4 = wb.create_sheet("Leaderboards")
    ws4.sheet_properties.tabColor = TAB_COLORS[3]
    collections = sim_loop_mod.most_profitable_collections(limit=100_000)
    headers4a = ["Collection", "# finds", "Avg ROI %", "Best ROI %", "Total expected profit"]
    rows4a = [[c["collection"], c["count"], round(c["avg_roi_pct"], 2), round(c["best_roi_pct"], 2),
               round(c["total_expected_profit"], 2)] for c in collections]
    last4a = _write_table(ws4, headers4a, rows4a, start_row=1)

    skins = sim_loop_mod.most_profitable_skins(limit=100_000)
    headers4b = ["Skin", "Wear", "# appearances", "Weighted expected profit"]
    rows4b = [[s["skin"], s["wear"], s["appearances"], round(s["weighted_profit"], 2)] for s in skins]
    _write_table(ws4, headers4b, rows4b, start_row=last4a + 3)

    # ---- Sheet 5: model quality (real calibration model + every agent's sim-model) ----
    from openpyxl.styles import Font
    ws5 = wb.create_sheet("Model quality")
    ws5.sheet_properties.tabColor = TAB_COLORS[4]
    real_status = learning_mod.get_model_status()
    real_outcomes_stats = outcomes_mod.stats()
    ws5.cell(row=1, column=1, value="Real ROI-calibration model (learning.py, trained on outcomes.jsonl)").font = Font(bold=True)
    row = 2
    for k, v in {
        "Trained": "Yes" if real_status.get("trained") else "No",
        "Samples": real_status.get("n_samples"),
        "Validation MAE (pp)": round(real_status["validation_mae"], 3) if real_status.get("validation_mae") is not None else None,
        "Trained at": _fmt_ts(real_status.get("trained_at")),
        "Sold outcomes logged": real_outcomes_stats.get("n_sold"),
    }.items():
        ws5.cell(row=row, column=1, value=k)
        ws5.cell(row=row, column=2, value=v)
        row += 1
    if real_status.get("feature_importances"):
        row += 1
        ws5.cell(row=row, column=1, value="Feature")
        ws5.cell(row=row, column=2, value="Permutation importance")
        _style_header_row(ws5, row, 2)
        for name, imp in sorted(real_status["feature_importances"].items(), key=lambda kv: -kv[1]):
            row += 1
            ws5.cell(row=row, column=1, value=name)
            ws5.cell(row=row, column=2, value=round(imp, 4))

    row += 3
    ws5.cell(row=row, column=1, value="Per-agent search-heuristic models (sim_learning.py, theoretical -- never real trades)").font = Font(bold=True)
    row += 1
    headers5 = ["Agent", "Trained", "Samples", "Validation MAE (pp)", "Trained at"]
    for j, h in enumerate(headers5, 1):
        ws5.cell(row=row, column=j, value=h)
    _style_header_row(ws5, row, len(headers5))
    for s in snaps:
        ms = s.get("sim_model_status") or {}
        row += 1
        ws5.cell(row=row, column=1, value=s.get("agent_name"))
        ws5.cell(row=row, column=2, value="Yes" if ms.get("trained") else "No")
        ws5.cell(row=row, column=3, value=ms.get("n_samples"))
        ws5.cell(row=row, column=4, value=round(ms["validation_mae"], 3) if ms.get("validation_mae") is not None else None)
        ws5.cell(row=row, column=5, value=_fmt_ts(ms.get("trained_at")))
    _autosize(ws5, 5)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
