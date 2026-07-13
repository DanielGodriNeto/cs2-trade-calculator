"""CLI entry point.

Usage examples (run from the cs2-trade-calculator directory):
    python -m src.main tradeups
    python -m src.main tradeups --collection "Dead Hand" --stattrak
    python -m src.main anomalies --collection "Dead Hand"
    python -m src.main update-data

All commands only READ public price data (Steam's priceoverview endpoint and the
open CSGO-API skin database). Nothing here places orders, logs into Steam, or
touches an inventory -- see README.md for why, and for the roadmap on adding
real execution safely.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

from . import data as data_mod
from . import tradeup as tradeup_mod
from . import anomalies as anomalies_mod
from . import covert as covert_mod
from . import marketplaces as marketplaces_mod
from . import outcomes as outcomes_mod
from . import learning as learning_mod
from . import retrain_loop as retrain_loop_mod
from . import price_history as price_history_mod
from . import price_report as price_report_mod
from . import data_archive as data_archive_mod
from . import split_archive as split_archive_mod
from . import price_scan as price_scan_mod
from . import profile_card_export as profile_card_export_mod
from . import sim_loop as sim_loop_mod
from . import sim_learning as sim_learning_mod
from . import agents as agents_mod
from . import dead_skin as dead_skin_mod
from . import invalid_skins as invalid_skins_mod
from . import mispriced_skins as mispriced_skins_mod
from . import wear_anomaly as wear_anomaly_mod
from . import market_reliability as market_reliability_mod
from . import verify_queue as verify_queue_mod
from . import pattern_db as pattern_db_mod
from . import pattern_scanner as pattern_scanner_mod
from . import user_overrides as user_overrides_mod
from . import backup_engine as backup_engine_mod
from . import watch_loop as watch_loop_mod
from .steam_prices import CURRENCIES, get_price, market_hash_name as build_market_hash_name


def _print_table(rows: list[dict], columns: list[str]):
    if not rows:
        print("(no results)")
        return
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def _write_csv(rows: list[dict], columns: list[str], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in columns})
    print(f"\nWrote {len(rows)} rows to {path}")


def cmd_update_data(args):
    skins = data_mod.load_skins(force_refresh=True)
    print(f"Refreshed skin database: {len(skins)} tradeable skins loaded.")


def cmd_currencies(args):
    rows = [{"code": code, "name": name, "symbol": symbol}
            for code, (name, symbol) in sorted(CURRENCIES.items(), key=lambda kv: kv[1][0])]
    _print_table(rows, ["code", "name", "symbol"])


def cmd_tradeups(args):
    skins = data_mod.load_skins()
    idx = data_mod.collections_index(skins)
    scope = [c for c in idx if not args.collection or args.collection.lower() in c.lower()]
    print(f"Scanning {len(scope)} collection(s)"
          f"{' matching ' + repr(args.collection) if args.collection else ''}"
          f"{' (StatTrak)' if args.stattrak else ''}... this can take a while due to"
          f" Steam's rate limit (~1.5s per priced item, cached for 1h after).\n")

    results = tradeup_mod.scan_all(idx, stattrak=args.stattrak, currency=args.currency,
                                    only_collection=args.collection, fast_mode=args.fast_mode,
                                    float_strategy=args.float_strategy, min_roi=args.min_roi,
                                    mixed_strategy=args.mixed_strategy, strategy_any_roi=args.strategy_any_roi,
                                    check_thirdparty=args.verify_prices, use_ml=args.use_ml)
    results = [r for r in results if r.roi_pct >= args.min_roi]
    top_results = results[:args.top]
    rows = [{
        "collection": r.collection,
        "from": r.from_rarity,
        "to": r.to_rarity,
        "cost": f"{r.cost:.2f}",
        "cheapest_input": r.cheapest_input,
        "exp_revenue": f"{r.expected_revenue:.2f}",
        "exp_revenue_after_fee": f"{r.expected_revenue_after_fee:.2f}",
        "profit": f"{r.profit:.2f}",
        "roi_pct": f"{r.roi_pct:.1f}",
        "max_input_price": f"{r.max_input_price:.2f}",
        "output_variants_priced": f"{r.priced_output_variants}/{r.n_output_variants}",
    } for r in top_results]
    columns = ["collection", "from", "to", "cost", "cheapest_input", "exp_revenue",
               "exp_revenue_after_fee", "profit", "roi_pct", "max_input_price", "output_variants_priced"]
    _print_table(rows, columns)

    if args.show_drops:
        for r in top_results:
            print(f"\n{r.collection} | {r.from_rarity} -> {r.to_rarity} -- possible drops:")
            drop_rows = [{
                "skin": d.name,
                "probability_pct": f"{d.probability_pct:.2f}",
                "price": f"{d.price:.2f}" if d.price is not None else "no price data",
                "profit_after_fee": f"{d.profit_after_fee:+.2f}" if d.profit_after_fee is not None else "-",
                "max_input_price": f"{d.max_input_price:.2f}" if d.max_input_price is not None else "-",
            } for d in r.possible_drops]
            _print_table(drop_rows, ["skin", "probability_pct", "price", "profit_after_fee", "max_input_price"])

    # When both --float-strategy and --mixed-strategy are passed together, scan_collection
    # computes the unified mixed_float_strategy instead of the two below (see tradeup.py) --
    # print that superset result instead of separately checking float_strategies/mixed_strategy,
    # which the combined run leaves empty.
    if args.float_strategy and args.mixed_strategy:
        for r in top_results:
            m = r.mixed_float_strategy
            if not m:
                continue
            mix_desc = " + ".join(f"{p['count']}x {p['wear']} (${p['unit_price']:.2f} ea)" for p in m.primary_mix)
            print(f"\n{r.collection} | {r.from_rarity} -> {r.to_rarity} -- mixed-float strategy "
                  f"(beats single-collection default):")
            print(f"  Primary: {mix_desc}")
            print(f"  Filler: {m.filler_count}x {m.filler_input} (from {m.filler_collection})")
            print(f"  Cost: {m.cost:.2f}  Exp. revenue: {m.expected_revenue:.2f}  "
                  f"After fee: {m.expected_revenue_after_fee:.2f}  "
                  f"Profit: {m.profit:+.2f}  ROI: {m.roi_pct:.1f}% (+{m.roi_improvement_pct:.1f}pp vs. single-collection)")
    else:
        if args.float_strategy:
            for r in top_results:
                if not r.float_strategies:
                    continue
                best = r.float_strategies[0]
                mix_desc = " + ".join(f"{m['count']}x {m['wear']} (${m['unit_price']:.2f} ea)" for m in best.input_mix)
                better = " (better than default!)" if best.roi_pct > r.roi_pct else " (default already about this good)"
                print(f"\n{r.collection} | {r.from_rarity} -> {r.to_rarity} -- best float-mix strategy{better}:")
                print(f"  Buy: {mix_desc}")
                print(f"  Targets landing output in: {best.target_wear}")
                print(f"  Cost: {best.input_cost:.2f}  Exp. revenue: {best.expected_revenue:.2f}  "
                      f"After fee: {best.expected_revenue_after_fee:.2f}  "
                      f"Profit: {best.profit:+.2f}  ROI: {best.roi_pct:.1f}%")

        if args.mixed_strategy:
            for r in top_results:
                m = r.mixed_strategy
                if not m:
                    continue
                print(f"\n{r.collection} | {r.from_rarity} -> {r.to_rarity} -- filler mix beats single-collection:")
                print(f"  Buy: {m.primary_count}x {m.primary_input} (from {m.primary_collection}) + "
                      f"{m.filler_count}x {m.filler_input} (filler, from {m.filler_collection})")
                print(f"  Cost: {m.cost:.2f}  Exp. revenue: {m.expected_revenue:.2f}  "
                      f"Profit: {m.profit:+.2f}  ROI: {m.roi_pct:.1f}% (+{m.roi_improvement_pct:.1f}pp vs. single-collection)")

    for r in top_results:
        if r.buy_links:
            print(f"\n{r.collection} | {r.from_rarity} -> {r.to_rarity} -- buy the cheapest input here:")
            for site, url in r.buy_links.items():
                print(f"  {marketplaces_mod.MARKETPLACE_LABELS.get(site, site)}: {url}")

    if args.out:
        csv_rows = [dict(row, possible_drops="; ".join(
            f"{d.name} ({d.probability_pct:.1f}%, "
            f"{'$' + format(d.price, '.2f') if d.price is not None else 'no price'}, "
            f"{'profit ' + format(d.profit_after_fee, '+.2f') if d.profit_after_fee is not None else 'no price'})"
            for d in r.possible_drops
        )) for row, r in zip(rows, top_results)]
        _write_csv(csv_rows, columns + ["possible_drops"], args.out)


def cmd_covert(args):
    skins = data_mod.load_skins()
    knives_gloves = data_mod.load_knives_gloves()
    cases_idx = data_mod.cases_index(skins, knives_gloves)
    scope = [c for c in cases_idx if not args.case or args.case.lower() in c.lower()]
    print(f"Scanning {len(scope)} case(s) for Covert -> knife/glove trade-ups"
          f"{' matching ' + repr(args.case) if args.case else ''}"
          f"{' (StatTrak)' if args.stattrak else ''}... 5 Covert inputs per contract.\n")

    results = covert_mod.scan_all(cases_idx, stattrak=args.stattrak, currency=args.currency,
                                   only_case=args.case, fast_mode=args.fast_mode,
                                   float_strategy=args.float_strategy, min_roi=args.min_roi,
                                   mixed_strategy=args.mixed_strategy, strategy_any_roi=args.strategy_any_roi)
    results = [r for r in results if r.roi_pct >= args.min_roi]
    top_results = results[:args.top]
    rows = [{
        "case": r.case,
        "cost": f"{r.cost:.2f}",
        "cheapest_input": r.cheapest_input,
        "exp_revenue": f"{r.expected_revenue:.2f}",
        "exp_revenue_after_fee": f"{r.expected_revenue_after_fee:.2f}",
        "profit": f"{r.profit:.2f}",
        "roi_pct": f"{r.roi_pct:.1f}",
        "max_input_price": f"{r.max_input_price:.2f}",
        "output_variants_priced": f"{r.priced_output_variants}/{r.n_output_variants}",
    } for r in top_results]
    columns = ["case", "cost", "cheapest_input", "exp_revenue", "exp_revenue_after_fee",
               "profit", "roi_pct", "max_input_price", "output_variants_priced"]
    _print_table(rows, columns)

    if args.show_drops:
        for r in top_results:
            print(f"\n{r.case} -- possible knife/glove drops:")
            drop_rows = [{
                "skin": d.name,
                "probability_pct": f"{d.probability_pct:.2f}",
                "price": f"{d.price:.2f}" if d.price is not None else "no price data",
                "profit_after_fee": f"{d.profit_after_fee:+.2f}" if d.profit_after_fee is not None else "-",
                "max_input_price": f"{d.max_input_price:.2f}" if d.max_input_price is not None else "-",
            } for d in r.possible_drops]
            _print_table(drop_rows, ["skin", "probability_pct", "price", "profit_after_fee", "max_input_price"])

    # Same superset-vs-separate logic as cmd_tradeups: when both flags are passed together,
    # scan_case computes the unified mixed_float_strategy instead of the two below.
    if args.float_strategy and args.mixed_strategy:
        for r in top_results:
            m = r.mixed_float_strategy
            if not m:
                continue
            mix_desc = " + ".join(f"{p['count']}x {p['wear']} (${p['unit_price']:.2f} ea)" for p in m.primary_mix)
            print(f"\n{r.case} -- mixed-float strategy (beats single-case default):")
            print(f"  Primary: {mix_desc}")
            if m.filler_count:
                print(f"  Filler: {m.filler_count}x {m.filler_input} (from {m.filler_case})")
            print(f"  Cost: {m.cost:.2f}  Exp. revenue: {m.expected_revenue:.2f}  "
                  f"After fee: {m.expected_revenue_after_fee:.2f}  "
                  f"Profit: {m.profit:+.2f}  ROI: {m.roi_pct:.1f}% (+{m.roi_improvement_pct:.1f}pp vs. single-case)")
    else:
        if args.float_strategy:
            for r in top_results:
                if not r.float_strategies:
                    continue
                best = r.float_strategies[0]
                mix_desc = " + ".join(f"{m['count']}x {m['wear']} (${m['unit_price']:.2f} ea)" for m in best.input_mix)
                better = " (better than default!)" if best.roi_pct > r.roi_pct else " (default already about this good)"
                print(f"\n{r.case} -- best float-mix strategy{better}:")
                print(f"  Buy: {mix_desc}")
                print(f"  Targets landing output in: {best.target_wear}")
                print(f"  Cost: {best.input_cost:.2f}  Exp. revenue: {best.expected_revenue:.2f}  "
                      f"After fee: {best.expected_revenue_after_fee:.2f}  "
                      f"Profit: {best.profit:+.2f}  ROI: {best.roi_pct:.1f}%")

        if args.mixed_strategy:
            for r in top_results:
                m = r.mixed_strategy
                if not m:
                    continue
                print(f"\n{r.case} -- filler mix beats single-case:")
                print(f"  Buy: {m.primary_count}x {m.primary_input} (from {m.primary_case}) + "
                      f"{m.filler_count}x {m.filler_input} (filler, from {m.filler_case})")
                print(f"  Cost: {m.cost:.2f}  Exp. revenue: {m.expected_revenue:.2f}  "
                      f"Profit: {m.profit:+.2f}  ROI: {m.roi_pct:.1f}% (+{m.roi_improvement_pct:.1f}pp vs. single-case)")

    for r in top_results:
        if r.buy_links:
            print(f"\n{r.case} -- buy the cheapest Covert input here:")
            for site, url in r.buy_links.items():
                print(f"  {marketplaces_mod.MARKETPLACE_LABELS.get(site, site)}: {url}")

    if args.out:
        _write_csv(rows, columns, args.out)


def cmd_anomalies(args):
    skins = data_mod.load_skins()
    if args.collection:
        skins = [s for s in skins if any(args.collection.lower() in c.lower() for c in s["collections"])]
    print(f"Scanning {len(skins)} skins for underpriced listings"
          f" (gap >= {args.min_gap_pct}%, volume >= {args.min_volume})...\n")
    found = anomalies_mod.scan_skins(skins, currency=args.currency,
                                      min_gap_pct=args.min_gap_pct, min_volume=args.min_volume,
                                      fast_mode=args.fast_mode, stattrak=args.stattrak)
    rows = [{
        "name": a.name,
        "lowest": f"{a.lowest_price:.2f}",
        "median": f"{a.median_price:.2f}",
        "volume": a.volume,
        "gap_pct": f"{a.gap_pct:.1f}",
        "profit_after_fee": f"{a.profit_after_fee:.2f}",
        "max_buy_price": f"{a.max_buy_price:.2f}",
    } for a in found[:args.top]]
    columns = ["name", "lowest", "median", "volume", "gap_pct", "profit_after_fee", "max_buy_price"]
    _print_table(rows, columns)
    if args.out:
        _write_csv(rows, columns, args.out)


def _parse_mix_entry(spec: str) -> dict:
    """'skin:wear:count:unit_price_paid' -> {"skin","wear","count","unit_price_paid"}. Skin
    names/wear names don't contain colons, so a plain split is safe. "wear" can be a bracket
    name ("Field-Tested") or an exact float ("0.18") -- resolved to a canonical wear name (plus
    the precise float, if given) by outcomes.log_outcome() -> outcomes.resolve_wear()."""
    parts = spec.split(":")
    if len(parts) != 4:
        raise SystemExit(f"--input/--filler must be 'skin:wear:count:unit_price_paid', got {spec!r}")
    skin, wear, count, price = parts
    try:
        return {"skin": skin, "wear": wear, "count": int(count), "unit_price_paid": float(price)}
    except ValueError:
        raise SystemExit(f"--input/--filler count/price must be numeric, got {spec!r}")


def cmd_log_outcome(args):
    input_mix = [_parse_mix_entry(s) for s in args.input]
    filler_input = _parse_mix_entry(args.filler) if args.filler else None
    try:
        outcome = outcomes_mod.log_outcome(
            collection=args.collection, kind=args.kind, stattrak=args.stattrak,
            from_rarity=args.from_rarity, to_rarity=args.to_rarity, currency=args.currency,
            input_mix=input_mix, output_skin=args.output_skin, output_wear=args.output_wear,
            filler_collection=args.filler_collection, filler_input=filler_input,
            sale_price=args.sale_price, status=args.status,
            predicted_roi_pct=args.predicted_roi, predicted_revenue=args.predicted_revenue,
            notes=args.notes or "")
    except ValueError as e:
        raise SystemExit(f"error: {e}")
    print(f"Logged outcome {outcome.id}")
    print(f"  Output wear: {outcome.output_wear}"
          f"{f' (float {outcome.output_float:.2f})' if outcome.output_float is not None else ''}")
    print(f"  Cost paid: {outcome.cost_paid:.2f}")
    if outcome.status == "sold":
        result = "WIN" if outcome.win else "LOSS"
        print(f"  Profit: {outcome.profit:+.2f}  ROI: {outcome.roi_pct:.1f}%  ({result})")
    else:
        print(f"  Status: {outcome.status} -- no sale price yet, profit/ROI pending "
              f"(use --status sold with --sale-price once it's resold)")


def cmd_list_outcomes(args):
    outcomes = sorted(outcomes_mod.load_outcomes(), key=lambda o: o.logged_at, reverse=True)
    rows = [{
        "id": o.id,
        "collection": o.collection,
        "from": o.from_rarity,
        "to": o.to_rarity,
        "status": o.status,
        "cost_paid": f"{o.cost_paid:.2f}",
        "profit": f"{o.profit:+.2f}" if o.profit is not None else "-",
        "roi_pct": f"{o.roi_pct:.1f}" if o.roi_pct is not None else "-",
        "win": "Y" if o.win else ("N" if o.win is False else "-"),
    } for o in outcomes[:args.top]]
    _print_table(rows, ["id", "collection", "from", "to", "status", "cost_paid", "profit", "roi_pct", "win"])
    s = outcomes_mod.stats()
    win_rate = f", {s['win_rate_pct']:.1f}% win rate" if s["win_rate_pct"] is not None else ""
    print(f"\n{s['n_total']} logged ({s['n_sold']} sold, {s['n_wins']} wins, {s['n_losses']} losses{win_rate})")


def cmd_train_model(args):
    result = learning_mod.train()
    rows = [{
        "trained": result.trained,
        "n_samples": result.n_samples,
        "validation_mae": f"{result.validation_mae:.2f}" if result.validation_mae is not None else "-",
        "reason": result.reason or "-",
    }]
    _print_table(rows, ["trained", "n_samples", "validation_mae", "reason"])
    if result.trained and result.feature_importances:
        print("\nFeature importances (permutation, higher = more predictive):")
        imp_rows = [{"feature": k, "importance": f"{v:.4f}"} for k, v in
                    sorted(result.feature_importances.items(), key=lambda kv: -kv[1])]
        _print_table(imp_rows, ["feature", "importance"])


def cmd_retrain_loop(args):
    retrain_loop_mod.run_foreground(poll_interval=args.poll_interval)


def cmd_watch(args):
    watch_loop_mod.run_foreground(poll_interval=args.interval, roi_threshold=args.roi_threshold,
                                   pattern_threshold=args.pattern_threshold)


def cmd_price_report(args):
    """Three scan modes, matched to the same flags the rest of the CLI already uses:
      1. fast:  --fast-mode (all collections, one wear priced per skin -- quickest, roughest)
      2. full:  neither flag (every wear priced per skin -- default, most accurate)
      3. ml:    --ml-only (needs a trained model -- see 'train-model'; only verifies trades the
                model itself also predicts a non-negative ROI for, not just the plain formula)
    All three only ever verify winning (roi_pct >= --min-roi, default 0%) trade-ups -- see
    price_report.py's module docstring for why."""
    skins = data_mod.load_skins()
    idx = data_mod.collections_index(skins)
    scope = [c for c in idx if not args.collection or args.collection.lower() in c.lower()]
    mode = "fast" if args.fast_mode else ("ml" if args.ml_only else "full")
    print(f"Scanning {len(scope)} collection(s)"
          f"{' matching ' + repr(args.collection) if args.collection else ''} for winning trade-ups "
          f"(ROI >= {args.min_roi}%, mode={mode})... this can take a while due to Steam's rate limit.\n")

    results = tradeup_mod.scan_all(idx, stattrak=args.stattrak, currency=args.currency,
                                    only_collection=args.collection, fast_mode=args.fast_mode,
                                    min_roi=args.min_roi, use_ml=args.ml_only)
    results = [r for r in results if r.roi_pct >= args.min_roi]
    if args.ml_only:
        results = [r for r in results if r.ml_predicted_roi_pct is not None and r.ml_predicted_roi_pct >= 0]
    results = results[:args.top]

    if not results:
        reason = (" -- log outcomes and run 'train-model' first (needs 20+ sold outcomes), "
                   "then the model can flag winners here" if args.ml_only else "")
        print(f"No matching winning trade-ups found{reason}.")
        return

    print(f"Verifying Skinport/CSFloat prices for {len(results)} winning trade-up(s) "
          f"(input + every possible output)...\n")
    rows = price_report_mod.build_report(results, args.currency)
    csv_rows = price_report_mod.to_csv_rows(rows)
    out_path = args.out or f"price-report-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    _write_csv(csv_rows, price_report_mod.CSV_COLUMNS, out_path)
    n_drought = sum(1 for row in rows if row.steam_vs_cheapest_pct is not None and row.steam_vs_cheapest_pct >= 15.0)
    print(f"{len(rows)} price row(s) across {len(results)} trade-up(s) -- {n_drought} priced 15%+ above "
          f"the cheapest verified source.")


def cmd_price_history(args):
    name = build_market_hash_name(args.skin, args.wear, args.stattrak)
    print(f"Looking up {name!r}...")
    price = get_price(name, args.currency)
    if price is None:
        print("Could not fetch a current price -- check the skin/wear spelling, or Steam may be rate-limiting.")

    history = price_history_mod.get_history(name, args.currency)
    if not history:
        print("No price history recorded for this skin/wear/currency yet.")
        return
    rows = [{
        "logged_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"])),
        "lowest_price": h["lowest_price"],
        "median_price": h["median_price"],
        "volume": h["volume"],
    } for h in history]
    _print_table(rows, ["logged_at", "lowest_price", "median_price", "volume"])

    prediction = price_history_mod.predict_next_price(history)
    if prediction is None:
        print(f"\nNot enough data to predict yet -- have {len(history)} point(s), need "
              f"{price_history_mod.MIN_HISTORY_POINTS}+. Run this again after a few more lookups/scans "
              f"(history only accumulates going forward -- Steam has no history endpoint to backfill from).")
    else:
        print(f"\nNaive linear-trend prediction ({prediction.horizon_hours:.0f}h ahead): "
              f"{prediction.predicted_price:.2f} (slope {prediction.slope_per_day:+.3f}/day, "
              f"R²={prediction.r_squared:.2f}, n={prediction.n_points}) -- a coursework-level "
              f"naive baseline, not a real forecast or financial advice.")


def cmd_export_data(args):
    try:
        parts = data_archive_mod.export_archive(args.out)
    except FileNotFoundError as e:
        raise SystemExit(f"error: {e}")
    print(f"Exported accumulated data (outcomes, price history, search checkpoint, ML model) "
          f"into {len(parts)} part(s), each <=100 MB so GitHub will accept them:")
    for p in parts:
        print(f"  {p}")
    print("\nCommit these files (or track the whole data/ folder directly in git -- it's plain")
    print("JSON/JSONL text, diff-friendly) to carry your training data to another machine.")


def cmd_import_data(args):
    try:
        result = data_archive_mod.import_archive(args.file, merge=not args.replace)
    except FileNotFoundError as e:
        raise SystemExit(f"error: {e}")
    if result["mode"] == "replace":
        print(f"Replaced data/ entirely with the contents of {args.file}.")
    else:
        print(f"Merged {args.file} into data/ (existing data kept, nothing overwritten):")
        print(f"  {result['outcomes_added']} new outcome(s) added")
        print(f"  {result['price_points_added']} new price-history point(s) added")
        print("\nThe trained ML model itself was left as-is (merging two trained models isn't")
        print("meaningful) -- run 'python -m src.main train-model' to fold in any new outcomes.")


def cmd_split_data(args):
    if args.status:
        split_archive_mod.status()
    elif args.restore:
        split_archive_mod.restore_all()
    else:
        split_archive_mod.split_all()


def cmd_scan_prices(args):
    price_scan_mod.run_foreground(collection_filter=args.collection or "", currency=args.currency)


def cmd_simulate_population(args):
    sim_loop_mod.run_foreground(max_rounds=args.max_rounds, currency=args.currency)


def cmd_population_status(args):
    sons = sim_loop_mod.create_population()
    rows = []
    for s in sons:
        snap = s.snapshot()
        rows.append({
            "son": s.son_id,
            "agent": snap["agent_name"],
            "temperature": s.base_temperature,
            "status": snap["status"],
            "round": snap["round"],
            "n_simulated": snap["n_simulated"],
            "n_invalid": snap["n_invalid"],
            "best_roi_ever": f"{snap['best_roi_ever']:.1f}" if snap["best_roi_ever"] is not None else "-",
            "rounds_since_improvement": snap["rounds_since_improvement"],
            "stuck": snap["stuck"],
            "model_trained": snap["sim_model_status"]["trained"],
            # Self-tuning progress (src/adaptive.py): preference tweaks kept vs. A/B-tested,
            # and how many distinct collections this agent has explored so far.
            "tweaks_kept": (f"{snap['adaptive']['adaptations_adopted']}/{snap['adaptive']['adaptations_tried']}"
                            if snap.get("adaptive") else "-"),
            "collections_explored": snap.get("coverage_collections", 0),
        })
    _print_table(rows, ["son", "agent", "temperature", "status", "round", "n_simulated", "n_invalid",
                         "best_roi_ever", "rounds_since_improvement", "stuck", "model_trained",
                         "tweaks_kept", "collections_explored"])


def cmd_best_simulations(args):
    sims = sim_loop_mod.list_best_simulations(son_id=args.son, collection_filter=args.collection)
    rows = [{
        "son": s["son"],
        "agent": agents_mod.AGENT_NAMES.get(s["son"], "?"),
        "collection": s["primary_collection"],
        "from": s["from_rarity"],
        "to": s["to_rarity"],
        "stattrak": s["stattrak"],
        "cost": f"{s['cost']:.2f}",
        "expected_roi_pct": f"{s['expected_roi_pct']:.1f}",
        "basis": s["cost_basis_source"],
    } for s in sims[:args.top]]
    _print_table(rows, ["son", "agent", "collection", "from", "to", "stattrak", "cost", "expected_roi_pct", "basis"])
    print("\nExpected ROI here is a THEORETICAL, cache-priced estimate from randomly generated,")
    print("simulated trials -- never a replacement for 'tradeups'/'best-mixes' real formula")
    print("results, and never merged with logged real outcomes. Verify before buying.")


def cmd_train_sim_model(args):
    son_ids = [args.son] if args.son is not None else list(range(sim_loop_mod.N_SONS))
    rows = []
    for son_id in son_ids:
        result = sim_learning_mod.train(sim_loop_mod.son_model_dir(son_id), sim_loop_mod.son_sims_file(son_id))
        rows.append({
            "son": son_id,
            "trained": result.trained,
            "n_samples": result.n_samples,
            "validation_mae": f"{result.validation_mae:.2f}" if result.validation_mae is not None else "-",
            "reason": result.reason or "-",
        })
    _print_table(rows, ["son", "trained", "n_samples", "validation_mae", "reason"])


def cmd_nudge_son(args):
    sons = sim_loop_mod.create_population()
    sim_loop_mod.nudge(sons[args.son])
    print(f"Son {args.son} nudged -- temperature temporarily boosted for the next "
          f"{sim_loop_mod.NUDGE_DURATION_ROUNDS} rounds. Its logged data and trained model are untouched.")


def cmd_reset_son(args):
    sons = sim_loop_mod.create_population()
    sim_loop_mod.full_reset(sons[args.son])
    print(f"Son {args.son} fully reset -- its logged simulations, trained model, and checkpoint "
          f"were wiped. It starts completely fresh next run.")


def cmd_dead_skins(args):
    dead = dead_skin_mod.list_dead_skins(currency=args.currency)
    if not dead:
        print("No skins currently flagged. Scores are reviewed weekly and only reflect skins "
              "actually seen by a scan/simulation so far -- an empty list may just mean nothing's "
              "been reviewed yet, not that the whole market looks healthy.")
        return
    rows = [{
        "skin": d["market_hash_name"],
        "score": f"{d['score']:.0f}",
        "reasons": ", ".join(d["reasons"]),
    } for d in dead]
    _print_table(rows, ["skin", "score", "reasons"])
    print(f"\n{len(dead)} skin(s) currently above the dead-skin threshold "
          f"({dead_skin_mod.DEAD_SKIN_THRESHOLD}). Temporary, decaying scores -- never a permanent "
          f"blacklist -- see src/dead_skin.py.")


def cmd_invalid_skins(args):
    invalid = invalid_skins_mod.list_invalid_skins(currency=args.currency)
    if not invalid:
        print("No skins currently registered as invalid. A skin lands here only when simulate.py "
              "could get NO price for it from Steam OR any third-party site -- an empty list is the "
              "healthy case (or just means nothing's been simulated yet).")
        return
    rows = [{
        "skin": d["market_hash_name"],
        "reason": d.get("reason", ""),
        "state": "active" if d.get("active") else "due for re-check",
        "marked": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["marked_at"])) if d.get("marked_at") else "-",
    } for d in invalid]
    _print_table(rows, ["skin", "reason", "state", "marked"])
    n_active = sum(1 for d in invalid if d.get("active"))
    print(f"\n{len(invalid)} skin(s) registered ({n_active} active). Active ones are excluded from "
          f"trade-up candidate generation; each becomes re-checkable after "
          f"{invalid_skins_mod.INVALID_RECHECK_SECONDS // 3600}h. Temporary + self-healing, never a "
          f"permanent blacklist -- see src/invalid_skins.py.")


def cmd_mispriced_skins(args):
    rows_in = mispriced_skins_mod.list_mispriced(currency=args.currency)
    if not rows_in:
        print("No verified mispricings on record. Marks only appear after a live Stage-2 "
              "verification (the automatic queue, a dashboard Verify button, or Verify top 50) "
              "finds Steam's price 20%+ away from the cheapest independent market price.")
        return
    rows = [{
        "skin": d["market_hash_name"],
        "direction": d.get("direction", ""),
        "gap %": f"{d.get('gap_pct', 0):+.1f}",
        "steam": f"{d.get('steam_price'):.2f}" if d.get("steam_price") is not None else "-",
        "cheapest": (f"{d.get('cheapest_price'):.2f} ({d.get('cheapest_source', '?')})"
                      if d.get("cheapest_price") is not None else "-"),
        "state": "active (penalized)" if d.get("active") else "expired -- due for re-check",
        "marked": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["marked_at"])) if d.get("marked_at") else "-",
    } for d in rows_in]
    _print_table(rows, ["skin", "direction", "gap %", "steam", "cheapest", "state", "marked"])
    n_active = sum(1 for d in rows_in if d.get("active"))
    print(f"\n{len(rows_in)} skin(s) marked ({n_active} active). While active, new simulations "
          f"containing the skin are scored unreliable (excluded from ML training and strict "
          f"agents' best lists). Marks expire after "
          f"{mispriced_skins_mod.RECHECK_SECONDS // 3600}h and clear immediately when a later "
          f"verification shows the price back in line -- see src/mispriced_skins.py.")


def cmd_skin_blacklist(args):
    if args.add:
        all_names = {s["name"] for s in data_mod.load_skins() + data_mod.load_knives_gloves()}
        name = args.add if args.add in all_names else next(
            (n for n in all_names if n.lower() == args.add.lower()), None)
        if name is None:
            print(f'Unknown skin "{args.add}" -- use the exact database name '
                  f'(e.g. "AWP | Gungnir").')
            return
        user_overrides_mod.add_blacklist(name, note=args.note or "")
        print(f"Blacklisted permanently: {name}. It is now excluded from candidate generation, "
              f"scans, and the manual builder, and every existing trade-up containing it is "
              f"hidden. Remove with: python -m src.main skin-blacklist --remove \"{name}\"")
        return
    if args.remove:
        removed = user_overrides_mod.remove_blacklist(args.remove)
        print("Removed." if removed else f'"{args.remove}" was not blacklisted.')
        return
    rows_in = user_overrides_mod.list_blacklist()
    if not rows_in:
        print("No skins blacklisted. Unlike every self-healing registry here, entries on this "
              "list NEVER expire -- they change only when you add/remove them "
              "(--add/--remove, or the dashboard's Skin blacklist panel).")
        return
    rows = [{
        "skin": d["skin"],
        "note": d.get("note", ""),
        "added": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["added_at"])) if d.get("added_at") else "-",
    } for d in rows_in]
    _print_table(rows, ["skin", "note", "added"])
    print(f"\n{len(rows_in)} skin(s) permanently blacklisted -- excluded everywhere (inputs AND "
          f"outputs) until removed. See src/user_overrides.py.")


def cmd_price_override(args):
    if args.remove:
        removed = user_overrides_mod.remove_override(args.remove)
        print("Removed." if removed else f'No override stored for "{args.remove}".')
        return
    if args.skin and args.wear and args.price is not None:
        entry = user_overrides_mod.set_override(args.skin, args.wear, args.stattrak,
                                                 args.price, args.currency)
        print(f"Override set: {entry['market_hash_name']} = {entry['price']} "
              f"(currency {entry['currency']}). This price now wins over every other source, "
              f"and existing trade-ups containing the skin are re-priced/re-ROI'd in the "
              f"opportunity lists.")
        return
    rows_in = user_overrides_mod.list_overrides()
    if not rows_in:
        print("No price overrides stored. Set one with: python -m src.main price-override "
              "--skin \"AWP | Gungnir\" --wear \"Factory New\" --price 1500")
        return
    rows = [{
        "market name": d["market_hash_name"],
        "price": f"{d['price']:.2f}",
        "currency": d.get("currency", 1),
        "added": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["added_at"])) if d.get("added_at") else "-",
    } for d in rows_in]
    _print_table(rows, ["market name", "price", "currency", "added"])
    print(f"\n{len(rows_in)} override(s) active -- each wins over the price cache, the bulk "
          f"mirror, and live fetches until removed. See src/user_overrides.py.")


def cmd_wear_anomalies(args):
    rows_in = wear_anomaly_mod.list_anomalies(currency=args.currency)
    if not rows_in:
        print("No wear-order price inversions on record. Marks appear when a positive-ROI scan "
              "finds an output skin whose BETTER condition (less wear) is priced cheaper than "
              "the condition the trade-up would produce -- that higher price is a bad data "
              "point, not real revenue.")
        return
    rows = [{
        "skin": d["market_hash_name"],
        "priced at": f"{d.get('price'):.2f}" if d.get("price") is not None else "-",
        "better wear": (f"{d.get('better_wear', '?')} @ {d.get('better_price'):.2f}"
                         if d.get("better_price") is not None else "-"),
        "inversion %": f"{d.get('inversion_pct', 0):.1f}",
        "state": "active (excluded)" if d.get("active") else "expired -- due for re-check",
        "marked": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["marked_at"])) if d.get("marked_at") else "-",
    } for d in rows_in]
    _print_table(rows, ["skin", "priced at", "better wear", "inversion %", "state", "marked"])
    n_active = sum(1 for d in rows_in if d.get("active"))
    print(f"\n{len(rows_in)} skin(s) marked ({n_active} active). While active, the flagged "
          f"skin+wear prices as unavailable in every scan and strategy search, so no trade-up "
          f"gets recommended off its inflated price. Marks expire after "
          f"{wear_anomaly_mod.RECHECK_SECONDS // 3600}h and clear immediately when a re-check "
          f"finds the wear ladder back in order -- see src/wear_anomaly.py.")


def cmd_market_reliability(args):
    rows_in = market_reliability_mod.list_tracked(currency=args.currency)
    if not rows_in:
        print("No skins currently tracked. This is the union of whatever dead_skin.py/"
              "mispriced_skins.py has already scored -- an empty list just means nothing's "
              "been reviewed yet, not that the whole market looks healthy.")
        return
    rows = [{
        "skin": d["market_hash_name"],
        "state": d["state"] + (" (override)" if d.get("overridden") else ""),
        "score": f"{d['reliability_score']:.0f}" if d.get("reliability_score") is not None else "-",
        "reasons": ", ".join(d.get("reasons") or []),
        "rolling z": f"{d['rolling_z']:.2f}" if d.get("rolling_z") is not None else "-",
    } for d in rows_in]
    _print_table(rows, ["skin", "state", "score", "reasons", "rolling z"])
    n_excluded = sum(1 for d in rows_in if d["state"] == "excluded")
    n_suspicious = sum(1 for d in rows_in if d["state"] == "suspicious")
    print(f"\n{len(rows_in)} skin(s) tracked ({n_excluded} excluded, {n_suspicious} suspicious). "
          f"Excluded skins are removed from ML training, ranking's reward score, and best-list "
          f"admission -- never permanent, re-evaluated from current signals on every recheck "
          f"(every {market_reliability_mod.RECHECK_POLL_INTERVAL_SECONDS // 60} min via the "
          f"retrain loop) -- see src/market_reliability.py.")


def cmd_override_reliability(args):
    if args.clear:
        removed = market_reliability_mod.clear_override(args.skin, args.currency)
        print(f"Override cleared for {args.skin!r}." if removed
              else f"No override was set for {args.skin!r}.")
        return
    if not args.state:
        print("--state is required unless --clear is given (healthy|suspicious|excluded).")
        return
    market_reliability_mod.set_override(args.skin, args.currency, args.state, args.note or "")
    print(f"Override set: {args.skin!r} -> {args.state} (currency={args.currency}). "
          f"Takes priority over the computed state everywhere until cleared.")


def cmd_verify_queue(args):
    verify_queue_mod.run_foreground(currency=args.currency)


def cmd_pattern_opportunities(args):
    rows_in = pattern_scanner_mod.list_opportunities(
        weapon=args.weapon, pattern_type=args.pattern_type, min_profit=args.min_profit,
        min_confidence=args.min_confidence, tier=args.tier, sort=args.sort)
    status = pattern_db_mod.get_status()
    print(f"Pattern knowledge: version {status['knowledge_version']}, "
          f"{status['n_entries']} documented patterns, last verified "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(status['last_verified_at'])) if status['last_verified_at'] else 'never'}.")
    if args.refresh:
        result = pattern_db_mod.refresh_knowledge()
        print(f"Knowledge refreshed: {result['n_changes']} change(s).")
    if not rows_in:
        print("\nNo rare-pattern opportunities currently recorded. The scanner runs from the "
              "dashboard's Rare Patterns page (needs a free CSFLOAT_API_KEY to scan listings) -- "
              "see src/pattern_scanner.py.")
        return
    rows = [{
        "item": o["market_hash_name"],
        "pattern": f"{o.get('pattern_label', '')} (T{o['pattern_tier']})" if o.get("pattern_tier") else o.get("pattern_label", ""),
        "seed/idx": o.get("paint_seed") if o.get("paint_seed") is not None else o.get("paint_index", "-"),
        "float": f"{o['float_value']:.4f}" if o.get("float_value") is not None else "-",
        "ask": f"{o['asking_price']:.2f}",
        "est. value": f"{o['estimated_collector_value']:.2f}",
        "est. profit": f"{o['expected_profit']:.2f}",
        "conf": o.get("confidence", "-"),
        "listing": o.get("listing_url") or "-",
    } for o in rows_in[:args.top]]
    _print_table(rows, ["item", "pattern", "seed/idx", "float", "ask", "est. value", "est. profit", "conf", "listing"])
    print(f"\n{len(rows_in)} opportunit{'y' if len(rows_in) == 1 else 'ies'} recorded. Estimated "
          f"values = consensus base price x documented collector premium (conservative) -- verify "
          f"the float/seed on the listing and the pattern's linked source before buying.")


def cmd_backup(args):
    if args.run_now:
        result = backup_engine_mod.run_backup(force=True)
        print(f"Backup run: {result.get('action')}"
              + (f" -- {result['archive']}" if result.get("archive") else "")
              + (f" -- {result['message']}" if result.get("message") else ""))
        return
    status = backup_engine_mod.get_status()
    cfg = status["config"]
    print(f"Automated GitHub backup engine (delta-aware, every "
          f"{status['wake_interval_seconds'] // 3600}h): "
          f"{'configured' if status['configured'] else 'NOT configured'}")
    if not status["configured"]:
        print(f"  -> {status['configuration_issue']}")
    print(f"Config: enabled={cfg['enabled']} repo_dir={cfg['repo_dir']} "
          f"remote={cfg['remote']} branch={cfg['branch']}  (data/backup_config.json)")
    print(f"Watched files: {', '.join(status['targets'])}")
    print(f"Last backup: "
          + (time.strftime("%Y-%m-%d %H:%M", time.localtime(status["last_backup_at"]))
             if status.get("last_backup_at") else "never")
          + (f" ({status['last_archive']})" if status.get("last_archive") else ""))
    for entry in status.get("recent_log") or []:
        detail = entry.get("archive") or entry.get("message") or entry.get("reason") or entry.get("error") or ""
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(entry['at']))} {entry['event']}: {detail}")


def build_parser():
    p = argparse.ArgumentParser(prog="cs2-trade-calculator")
    sub = p.add_subparsers(dest="command", required=True)

    p_update = sub.add_parser("update-data", help="Refresh the cached skin/collection database")
    p_update.set_defaults(func=cmd_update_data)

    p_currencies = sub.add_parser("currencies", help="List supported Steam currency codes")
    p_currencies.set_defaults(func=cmd_currencies)

    p_tradeups = sub.add_parser("tradeups", help="Scan trade-up contracts for +EV opportunities")
    p_tradeups.add_argument("--collection", default=None, help="Substring filter, e.g. 'Dead Hand'")
    p_tradeups.add_argument("--stattrak", action="store_true", help="Scan StatTrak trade-ups instead")
    p_tradeups.add_argument("--currency", type=int, default=1,
                             help="Steam currency code (1 = USD, run 'currencies' subcommand for the full list)")
    p_tradeups.add_argument("--min-roi", type=float, default=0.0, help="Hide results below this ROI%%")
    p_tradeups.add_argument("--top", type=int, default=30, help="Max rows to show")
    p_tradeups.add_argument("--out", default=None, help="Optional CSV output path")
    p_tradeups.add_argument("--show-drops", action="store_true",
                             help="Print every possible output skin (with drop probability and price) for each result")
    p_tradeups.add_argument("--fast-mode", action="store_true",
                             help="Only price one wear per skin instead of every wear -- several times faster, rougher numbers")
    p_tradeups.add_argument("--float-strategy", action="store_true",
                             help="Also find the best input wear-mix (float manipulation) per result -- more requests")
    p_tradeups.add_argument("--mixed-strategy", action="store_true",
                             help="Also search for a cross-collection filler-skin mix that beats using one "
                                  "collection alone (Oct-2025 group-based odds) -- more requests")
    p_tradeups.add_argument("--strategy-any-roi", action="store_true",
                             help="Run --float-strategy/--mixed-strategy (and the combined mixed-float search) on "
                                  "every collection, not just ones whose plain ROI is already >= 0 -- many more requests")
    p_tradeups.add_argument("--verify-prices", action="store_true", dest="verify_prices",
                             help="Cross-check the cheapest input's Steam price against Skinport/CSFloat to flag "
                                  "a 'drought' (Steam priced anomalously high vs. elsewhere) -- extra requests, "
                                  "annotation only, never changes the cost/ROI numbers")
    p_tradeups.add_argument("--use-ml", action="store_true", dest="use_ml",
                             help="Consult the trained ML ROI-calibration model (see 'train-model') as a "
                                  "tiebreaker signal -- no-op until you've logged 20+ outcomes and trained a model")
    p_tradeups.set_defaults(func=cmd_tradeups)

    p_covert = sub.add_parser("covert", help="Scan Covert -> knife/glove trade-ups (Oct-2025 update)")
    p_covert.add_argument("--case", default=None, help="Substring filter, e.g. 'Dreams'")
    p_covert.add_argument("--stattrak", action="store_true",
                           help="Scan StatTrak Covert -> StatTrak knife trade-ups instead (no gloves)")
    p_covert.add_argument("--currency", type=int, default=1, help="Steam currency code (1 = USD)")
    p_covert.add_argument("--min-roi", type=float, default=0.0, help="Hide results below this ROI%%")
    p_covert.add_argument("--top", type=int, default=30, help="Max rows to show")
    p_covert.add_argument("--out", default=None, help="Optional CSV output path")
    p_covert.add_argument("--show-drops", action="store_true",
                           help="Print every possible knife/glove drop, with odds and price")
    p_covert.add_argument("--fast-mode", action="store_true",
                           help="Only price one wear per skin instead of every wear -- faster, rougher numbers")
    p_covert.add_argument("--float-strategy", action="store_true",
                           help="Also find the best input wear-mix (float manipulation) per result -- more requests")
    p_covert.add_argument("--mixed-strategy", action="store_true",
                           help="Also search for a cross-case filler-skin mix that beats using one "
                                "case alone (Oct-2025 group-based odds) -- more requests")
    p_covert.add_argument("--strategy-any-roi", action="store_true",
                           help="Run --float-strategy/--mixed-strategy (and the combined mixed-float search) on "
                                "every case, not just ones whose plain ROI is already >= 0 -- many more requests")
    p_covert.set_defaults(func=cmd_covert)

    p_anom = sub.add_parser("anomalies", help="Scan for underpriced Steam Market listings")
    p_anom.add_argument("--collection", default=None, help="Substring filter, e.g. 'Dead Hand'")
    p_anom.add_argument("--currency", type=int, default=1, help="Steam currency code (1 = USD)")
    p_anom.add_argument("--min-gap-pct", type=float, default=15.0)
    p_anom.add_argument("--min-volume", type=int, default=5)
    p_anom.add_argument("--top", type=int, default=30)
    p_anom.add_argument("--out", default=None)
    p_anom.add_argument("--fast-mode", action="store_true",
                         help="Only price one wear per skin -- several times faster")
    p_anom.add_argument("--stattrak", action="store_true",
                         help="Check StatTrak variants instead of normal -- checking both would double the requests")
    p_anom.set_defaults(func=cmd_anomalies)

    p_log = sub.add_parser("log-outcome", help="Log the actual outcome of a trade-up you executed")
    p_log.add_argument("--collection", required=True, help="Collection name (or case name if --kind case)")
    p_log.add_argument("--kind", choices=["collection", "case"], default="collection")
    p_log.add_argument("--stattrak", action="store_true")
    p_log.add_argument("--from-rarity", required=True)
    p_log.add_argument("--to-rarity", required=True)
    p_log.add_argument("--currency", type=int, default=1)
    p_log.add_argument("--input", action="append", required=True,
                        help="'skin:wear:count:unit_price_paid', repeatable for a multi-wear mix -- "
                             "'wear' can be a bracket name (Field-Tested) or an exact float (0.18), "
                             "rounded to 2 decimals and auto-mapped to its bracket")
    p_log.add_argument("--filler", default=None, help="Optional filler leg, same 'skin:wear:count:price' format")
    p_log.add_argument("--filler-collection", default=None)
    p_log.add_argument("--output-skin", required=True)
    p_log.add_argument("--output-wear", required=True,
                        help="A bracket name (Field-Tested) or an exact float (0.18) -- same rules as --input's wear")
    p_log.add_argument("--sale-price", type=float, default=None, help="Required when --status sold (the default)")
    p_log.add_argument("--status", choices=["sold", "held", "kept"], default="sold")
    p_log.add_argument("--predicted-roi", type=float, default=None,
                        help="The tool's own roi_pct at scan time, if you want it recorded for later comparison")
    p_log.add_argument("--predicted-revenue", type=float, default=None)
    p_log.add_argument("--notes", default="")
    p_log.set_defaults(func=cmd_log_outcome)

    p_listout = sub.add_parser("list-outcomes", help="List logged trade-up outcomes")
    p_listout.add_argument("--top", type=int, default=50)
    p_listout.set_defaults(func=cmd_list_outcomes)

    p_train = sub.add_parser("train-model", help="Train the ML ROI-calibration model on logged outcomes")
    p_train.set_defaults(func=cmd_train_model)

    p_retrain = sub.add_parser("retrain-loop",
                                help="Watch logged outcomes and automatically retrain the ML model as new ones arrive")
    p_retrain.add_argument("--poll-interval", type=float, default=300.0)
    p_retrain.set_defaults(func=cmd_retrain_loop)

    p_price_hist = sub.add_parser("price-history",
                                   help="Show recorded price history + a naive trend prediction for a skin")
    p_price_hist.add_argument("--skin", required=True, help="Bare skin name, e.g. 'AK-47 | Redline'")
    p_price_hist.add_argument("--wear", required=True,
                               help="Factory New | Minimal Wear | Field-Tested | Well-Worn | Battle-Scarred")
    p_price_hist.add_argument("--stattrak", action="store_true")
    p_price_hist.add_argument("--currency", type=int, default=1)
    p_price_hist.set_defaults(func=cmd_price_history)

    p_price_report = sub.add_parser(
        "price-report",
        help="Cross-check Steam vs. Skinport/CSFloat prices for winning trade-ups' input and every "
             "possible output, export a spreadsheet-ready CSV")
    p_price_report.add_argument("--collection", default=None,
                                 help="Substring filter, e.g. 'Dead Hand' -- omit for all collections")
    p_price_report.add_argument("--stattrak", action="store_true")
    p_price_report.add_argument("--currency", type=int, default=1)
    p_price_report.add_argument("--min-roi", type=float, default=0.0,
                                 help="Only verify winning trades -- default 0%% (any non-negative ROI)")
    p_price_report.add_argument("--top", type=int, default=30,
                                 help="Max trade-ups to verify (each adds 1 input row + up to N output rows)")
    p_price_report.add_argument("--fast-mode", action="store_true",
                                 help="Mode 1: all collections, one wear priced per skin -- fastest, roughest numbers")
    p_price_report.add_argument("--ml-only", action="store_true", dest="ml_only",
                                 help="Mode 3: only verify trades the trained ML model also predicts a "
                                      "non-negative ROI for (needs 'train-model' run first) -- implies --use-ml")
    p_price_report.add_argument("--out", default=None,
                                 help="Output CSV path (default: price-report-<timestamp>.csv)")
    p_price_report.set_defaults(func=cmd_price_report)

    p_export = sub.add_parser("export-data",
                               help="Zip data/ (outcomes, price history, search checkpoint, ML model) into portable, <=100 MB archive parts")
    p_export.add_argument("--out", default=None,
                           help="Output .zip path/stem (default: data-archive-<timestamp>.zip in the project root); "
                                "actual files are written as <stem>_partNNN.zip")
    p_export.set_defaults(func=cmd_export_data)

    p_import = sub.add_parser("import-data",
                               help="Import a data archive -- merges with existing data by default (safe across machines)")
    p_import.add_argument("--file", required=True,
                           help="Path to one .zip part created by export-data (sibling parts with the same "
                                "prefix are found automatically) or a directory of parts")
    p_import.add_argument("--replace", action="store_true",
                           help="Replace data/ entirely instead of merging (DESTRUCTIVE -- discards anything not in the archive)")
    p_import.set_defaults(func=cmd_import_data)

    p_split = sub.add_parser(
        "split-data",
        help="Split the giant append-only logs (market reliability, per-son simulations) into "
             "small dated .jsonl.gz chunks under data/archive/ so the repo fits GitHub's "
             "100 MB per-file limit -- live files are never modified")
    p_split.add_argument("--restore", action="store_true",
                          help="Rebuild missing/empty live log files from the archived chunks "
                               "(for a fresh clone); never overwrites a live file that has data")
    p_split.add_argument("--status", action="store_true",
                          help="Show what's archived (chunks, records, date range) without writing anything")
    p_split.set_defaults(func=cmd_split_data)

    p_scan_prices = sub.add_parser(
        "scan-prices",
        help="Bulk-fetch and cache base prices for every tradeable skin/wear (no trade-up math) -- "
             "run this before simulate-population so it isn't working off an empty cache")
    p_scan_prices.add_argument("--collection", default=None, help="Substring filter, e.g. 'Dead Hand'")
    p_scan_prices.add_argument("--currency", type=int, default=1, help="Steam currency code (1 = USD)")
    p_scan_prices.set_defaults(func=cmd_scan_prices)

    p_sim_pop = sub.add_parser(
        "simulate-population",
        help="Run a population of guided random-trade simulation 'sons' in the background (checkpointed, "
             "Ctrl+C to stop all) -- each son generates random plausible trade-ups with a different "
             "exploration temperature, simulates their EV from cached prices, logs to its own "
             "data/sons/son_N/simulations.jsonl, and trains its own search-heuristic ML model on that log")
    p_sim_pop.add_argument("--max-rounds", type=int, default=None,
                            help="Stop after this many rounds per son instead of running forever")
    p_sim_pop.add_argument("--currency", type=int, default=1, help="Steam currency code (1 = USD)")
    p_sim_pop.set_defaults(func=cmd_simulate_population)

    p_pop_status = sub.add_parser("population-status",
                                   help="Print one row per son: status, round, best ROI ever, whether it's stuck")
    p_pop_status.set_defaults(func=cmd_population_status)

    p_best_sims = sub.add_parser("best-simulations",
                                  help="Print the best-known simulated trades found so far (merged across all "
                                       "sons by default)")
    p_best_sims.add_argument("--son", type=int, default=None, help="Only show this son's results (0-3)")
    p_best_sims.add_argument("--collection", default=None)
    p_best_sims.add_argument("--top", type=int, default=30)
    p_best_sims.set_defaults(func=cmd_best_simulations)

    p_train_sim = sub.add_parser("train-sim-model",
                                  help="Train a son's search-heuristic ML model on its own logged simulations")
    p_train_sim.add_argument("--son", type=int, default=None,
                              help="Which son (0-3) to train -- omit to train all of them")
    p_train_sim.set_defaults(func=cmd_train_sim_model)

    p_nudge = sub.add_parser(
        "nudge-son",
        help="Soft retry: temporarily boost a son's exploration temperature to kick it out of a plateau -- "
             "keeps its logged data and trained model")
    p_nudge.add_argument("--son", type=int, required=True, help="Which son (0-3) to nudge")
    p_nudge.set_defaults(func=cmd_nudge_son)

    p_reset = sub.add_parser(
        "reset-son",
        help="Hard retry: wipe one son's logged simulations, trained model, and checkpoint, and start "
             "it over from scratch -- other sons are unaffected")
    p_reset.add_argument("--son", type=int, required=True, help="Which son (0-3) to fully reset")
    p_reset.set_defaults(func=cmd_reset_son)

    p_dead = sub.add_parser(
        "dead-skins",
        help="List currently flagged dead/illiquid skins (temporary scores, reviewed weekly -- never a "
             "permanent blacklist, see src/dead_skin.py)")
    p_dead.add_argument("--currency", type=int, default=1)
    p_dead.set_defaults(func=cmd_dead_skins)

    p_invalid = sub.add_parser(
        "invalid-skins",
        help="List skins with no obtainable price on Steam OR any third-party site -- excluded from "
             "trade-up candidate generation (temporary, re-checkable, never a permanent blacklist -- "
             "see src/invalid_skins.py)")
    p_invalid.add_argument("--currency", type=int, default=1)
    p_invalid.set_defaults(func=cmd_invalid_skins)

    p_mispriced = sub.add_parser(
        "mispriced-skins",
        help="List skins a live Stage-2 verification showed to be over/undervalued on Steam vs. "
             "the wider market (Skinport/Buff163/CSFloat) -- while active, the agents' training "
             "avoids them (temporary, self-healing -- see src/mispriced_skins.py)")
    p_mispriced.add_argument("--currency", type=int, default=1)
    p_mispriced.set_defaults(func=cmd_mispriced_skins)

    p_wear_anom = sub.add_parser(
        "wear-anomalies",
        help="List skins whose price failed the wear-order sanity check (a BETTER condition "
             "priced cheaper than a worse one -- the worse wear's price is a bad data point, "
             "excluded from recommendations while active; temporary, self-healing -- "
             "see src/wear_anomaly.py)")
    p_wear_anom.add_argument("--currency", type=int, default=1)
    p_wear_anom.set_defaults(func=cmd_wear_anomalies)

    p_skin_bl = sub.add_parser(
        "skin-blacklist",
        help="List/manage the PERMANENT per-skin blacklist (src/user_overrides.py) -- a "
             "blacklisted skin is excluded everywhere (inputs AND outputs) and every existing "
             "trade-up containing it is hidden, until you remove it yourself")
    p_skin_bl.add_argument("--add", help='Exact skin name to blacklist, e.g. "AWP | Gungnir"')
    p_skin_bl.add_argument("--remove", help="Skin name to un-blacklist")
    p_skin_bl.add_argument("--note", help="Optional note stored with --add")
    p_skin_bl.set_defaults(func=cmd_skin_blacklist)

    p_override = sub.add_parser(
        "price-override",
        help="List/manage manual price overrides (src/user_overrides.py) -- an override wins "
             "over every price source for that skin+wear, and existing trade-ups containing "
             "the skin get their cost/ROI recomputed in the opportunity lists")
    p_override.add_argument("--skin", help='Skin name, e.g. "AWP | Gungnir"')
    p_override.add_argument("--wear", help='Wear, e.g. "Factory New"')
    p_override.add_argument("--stattrak", action="store_true")
    p_override.add_argument("--price", type=float, help="Your price for this skin+wear")
    p_override.add_argument("--currency", type=int, default=1, help="Steam currency code (default 1 = USD)")
    p_override.add_argument("--remove", metavar="MARKET_HASH_NAME",
                            help='Remove the override for this full market name, e.g. "AWP | Gungnir (Factory New)"')
    p_override.set_defaults(func=cmd_price_override)

    p_verify_queue = sub.add_parser(
        "verify-queue",
        help="Watch for simulate-population finds crossing the auto-verify bar (any positive ROI + "
             "confidence) and cross-check every input skin and output against Skinport/Buff163/CSFloat "
             "automatically, one at a time (see src/verify_queue.py)")
    p_verify_queue.add_argument("--currency", type=int, default=1)
    p_verify_queue.set_defaults(func=cmd_verify_queue)

    p_mkt_rel = sub.add_parser(
        "market-reliability",
        help="List every skin's unified Healthy/Suspicious/Excluded reliability state -- combines "
             "dead_skin.py's score, mispriced_skins.py's verified gap, and a rolling price-history "
             "Z-score (see src/market_reliability.py)")
    p_mkt_rel.add_argument("--currency", type=int, default=1)
    p_mkt_rel.set_defaults(func=cmd_market_reliability)

    p_override_rel = sub.add_parser(
        "override-reliability",
        help="Manually force (or clear) a skin's Market Reliability Engine state -- always wins "
             "over the computed state until cleared (see src/market_reliability.py)")
    p_override_rel.add_argument("--skin", required=True,
                                 help="Full market_hash_name, e.g. 'AK-47 | Redline (Field-Tested)'")
    p_override_rel.add_argument("--currency", type=int, default=1)
    p_override_rel.add_argument("--state", choices=list(market_reliability_mod.VALID_STATES))
    p_override_rel.add_argument("--note", default="")
    p_override_rel.add_argument("--clear", action="store_true", help="Remove any existing override instead")
    p_override_rel.set_defaults(func=cmd_override_reliability)


    p_patterns = sub.add_parser(
        "pattern-opportunities",
        help="List recorded Rare Pattern (Collector Arbitrage) opportunities -- documented "
             "collector patterns listed at ordinary market prices (see src/pattern_scanner.py)")
    p_patterns.add_argument("--weapon", default=None, help="Filter by weapon name substring")
    p_patterns.add_argument("--pattern-type", dest="pattern_type", default=None,
                             choices=["paint_seed", "paint_index", "sticker_craft"])
    p_patterns.add_argument("--min-profit", dest="min_profit", type=float, default=None)
    p_patterns.add_argument("--min-confidence", dest="min_confidence", default=None,
                             choices=["low", "medium", "high"])
    p_patterns.add_argument("--tier", type=int, default=None, choices=[1, 2, 3])
    p_patterns.add_argument("--sort", default="score",
                             choices=["score", "profit", "premium", "confidence", "newest"])
    p_patterns.add_argument("--top", type=int, default=30)
    p_patterns.add_argument("--refresh", action="store_true",
                             help="Run a pattern-knowledge verification/refresh pass first")
    p_patterns.set_defaults(func=cmd_pattern_opportunities)

    p_backup = sub.add_parser(
        "backup",
        help="Show the automated GitHub backup engine's status/audit log, or --run-now to force "
             "one archive+commit+push cycle immediately (see src/backup_engine.py)")
    p_backup.add_argument("--run-now", dest="run_now", action="store_true",
                           help="Run a full backup cycle now, skipping the 6h delta check")
    p_backup.set_defaults(func=cmd_backup)

    p_watch = sub.add_parser(
        "watch",
        help="Leave running: repeatedly scans for high-ROI trade-ups and cheap rare patterns, "
             "sends a phone push notification (ntfy.sh, see src/alerts.py) on new hits, and "
             "drives the GitHub auto-backup + Google Sheets export each cycle (see "
             "src/watch_loop.py)")
    p_watch.add_argument("--interval", type=float, default=watch_loop_mod.DEFAULT_POLL_INTERVAL_SECONDS,
                          help=f"Seconds between scan cycles (default "
                               f"{watch_loop_mod.DEFAULT_POLL_INTERVAL_SECONDS:.0f})")
    p_watch.add_argument("--roi-threshold", dest="roi_threshold", type=float,
                          default=watch_loop_mod.DEFAULT_ROI_THRESHOLD_PCT,
                          help=f"Alert on trade-ups at or above this ROI%% (default "
                               f"{watch_loop_mod.DEFAULT_ROI_THRESHOLD_PCT:.0f})")
    p_watch.add_argument("--pattern-threshold", dest="pattern_threshold", type=float,
                          default=watch_loop_mod.DEFAULT_PATTERN_THRESHOLD_PCT,
                          help=f"Alert on rare patterns priced at or below this %% under their "
                               f"documented collector value (default "
                               f"{watch_loop_mod.DEFAULT_PATTERN_THRESHOLD_PCT:.0f})")
    p_watch.set_defaults(func=cmd_watch)

    p_profile_card = sub.add_parser(
        "profile-card",
        help="Write a small DELAYED/ANONYMIZED aggregate-stats JSON snapshot (population avg "
             "ROI, a magnitude-only best-find figure, total candidates scanned, real outcomes "
             "logged) for the GitHub profile-card workflow -- never real actionable trade-up "
             "opportunities (see src/profile_card_export.py). Does NOT commit or push anything.")
    p_profile_card.add_argument("--out", default=str(Path("data") / "profile_card_snapshot.json"),
                                 help="Output path (default: data/profile_card_snapshot.json)")
    p_profile_card.set_defaults(func=cmd_profile_card)

    return p


def cmd_profile_card(args):
    snapshot = profile_card_export_mod.write_snapshot(Path(args.out))
    print(f"Wrote {args.out}")
    for k, v in snapshot.items():
        print(f"  {k}: {v}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass  # older Python without reconfigure(); StatTrak(TM) symbol may render oddly
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
