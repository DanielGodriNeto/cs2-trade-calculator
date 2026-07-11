"""Cross-marketplace price normalization for winning (ROI >= 0) trade-up results -- both the
cheapest input skin+wear and every possible output skin, verified against Steam, Skinport, and
(if CSFLOAT_API_KEY is configured) CSFloat. Two uses: (1) manually double-checking a result
before actually executing a trade-up, and (2) building an exportable, spreadsheet-ready record
for training/reference (see main.py's `price-report` command and web/server.py's
/api/price-report).

Deliberately only ever runs on results the caller has already filtered to roi_pct >= 0 -- the
same "don't spend rate-limited Skinport/CSFloat requests on a trade you wouldn't act on anyway"
gate tradeup.py's own buy_links/thirdparty_check already apply (see tradeup.py's scan_collection).

Steam prices are NOT re-fetched here -- they're taken straight from the TradeUpResult/PossibleDrop
the scan already computed (r.cost/10 for the input, d.price for each output), both already a
genuinely fresh or cache-hit priceoverview lookup. Re-querying Steam a second time here would
double the request count for no new information and risk a slightly different number if the
1-hour price cache happened to roll over in between -- this only adds the NEW verification work
(Skinport/CSFloat), which is the actual point of this module.

**Liquidity check**: a Steam price is really just "whatever the most recent trade(s) happened to
go for" -- for a rarely-traded skin, that can mean one or two sales set the number, i.e. it's an
outlier nobody else is actually buying/selling at, not a price you could reliably transact at.
`price_confidence` flags exactly this: LOW_VOLUME_THRESHOLD (same convention/default as
anomalies.py's --min-volume) checked against PossibleDrop.volume/TradeUpResult.cheapest_input_volume
(Steam's own recent-sales count for that specific price), combined with whether a third-party
source (Skinport/CSFloat) independently shows a similar listing at all -- a low-volume Steam price
that's ALSO not corroborated anywhere else is the "someone lists it high but nobody's actually
buying" case this exists to catch.

**Consensus price (median across sites)**: when the sources that returned a price for a skin
DISAGREE badly with each other (max source >= HIGH_SPREAD_THRESHOLD_PCT above min source), no
single site's number deserves to be trusted alone -- the cheapest one is just as likely the
outlier (a stale mirror entry, a fat-fingered listing) as the priciest. Each row therefore also
carries median_price (the median of however many of the 1-4 sources actually returned a price)
and consensus_price: cheapest_price normally, but the MEDIAN when the spread is high. The
verified-ROI math in verify_queue._summarize uses consensus_price, so the ML's positive-ROI
finds are re-valued against the cross-site consensus rather than one possibly-broken number.
"""
import statistics
from dataclasses import dataclass, asdict

from . import thirdparty_prices as thirdparty_prices_mod

LOW_VOLUME_THRESHOLD = 5  # matches anomalies.py's own --min-volume default -- same "too few real
# sales to trust this number" convention, reused here rather than inventing a second one

HIGH_SPREAD_THRESHOLD_PCT = 20.0  # cheapest-to-priciest gap across the sources that answered must
# reach this before consensus_price switches from cheapest to the cross-site median -- same 20 as
# mispriced_skins.GAP_THRESHOLD_PCT on purpose: both are "the verified market genuinely disagrees"
# bars sitting between drought_flag's looser 15 and dead_skin.LARGE_PRICE_GAP_PCT's stricter 25

CSV_COLUMNS = ["collection", "from_rarity", "to_rarity", "stattrak", "roi_pct", "ml_predicted_roi_pct",
               "role", "skin", "wear", "probability_pct", "steam_price", "steam_volume",
               "skinport_price", "buff163_price", "csfloat_price", "cheapest_price", "cheapest_source",
               "steam_vs_cheapest_pct", "median_price", "n_sources", "source_spread_pct",
               "high_spread", "consensus_price", "price_confidence"]
_MONEY_FIELDS = ("roi_pct", "ml_predicted_roi_pct", "probability_pct", "steam_price",
                  "skinport_price", "buff163_price", "csfloat_price", "cheapest_price", "steam_vs_cheapest_pct",
                  "median_price", "source_spread_pct", "consensus_price")


@dataclass
class PriceRow:
    collection: str
    from_rarity: str
    to_rarity: str
    stattrak: bool
    roi_pct: float
    ml_predicted_roi_pct: float | None
    role: str  # "input" | "output"
    skin: str
    wear: str
    probability_pct: float | None  # None for the input row -- only outputs have a drop chance
    steam_price: float | None
    steam_volume: int | None  # Steam's own recent-sales count backing steam_price -- see module docstring
    skinport_price: float | None
    buff163_price: float | None
    csfloat_price: float | None
    cheapest_price: float | None
    cheapest_source: str | None
    steam_vs_cheapest_pct: float | None  # positive = Steam is pricier than the cheapest verified source
    median_price: float | None  # median across every source that returned a price (Steam included)
    n_sources: int  # how many of the up-to-4 sources actually returned a price for this row
    source_spread_pct: float | None  # priciest vs. cheapest source, %; None with fewer than 2 sources
    high_spread: bool  # source_spread_pct >= HIGH_SPREAD_THRESHOLD_PCT -- sites disagree badly
    consensus_price: float | None  # what verified-ROI math uses: median when high_spread, else cheapest
    price_confidence: str  # "ok" | "low volume" | "no 3rd-party match" | "low volume + no 3rd-party match"


def _normalize(steam_price: float | None, skinport_price: float | None,
                buff163_price: float | None, csfloat_price: float | None) -> dict:
    """Cross-source price fields for one skin+wear, from whichever of the 4 sources actually had
    data: the cheapest price/source and Steam's gap to it (the same comparison
    thirdparty_prices.drought_flag() makes for a single source), plus the consensus fields the
    module docstring describes -- median across the answering sources, the cheapest-to-priciest
    spread, and consensus_price (median when the spread crosses HIGH_SPREAD_THRESHOLD_PCT, the
    plain cheapest otherwise). Keys match PriceRow's field names so the caller can **splat them."""
    candidates = [(src, p) for src, p in
                  (("steam", steam_price), ("skinport", skinport_price),
                   ("buff163", buff163_price), ("csfloat", csfloat_price))
                  if p is not None]
    out = {"cheapest_price": None, "cheapest_source": None, "steam_vs_cheapest_pct": None,
           "median_price": None, "n_sources": len(candidates), "source_spread_pct": None,
           "high_spread": False, "consensus_price": None}
    if not candidates:
        return out
    cheapest_source, cheapest_price = min(candidates, key=lambda kv: kv[1])
    out["cheapest_price"], out["cheapest_source"] = cheapest_price, cheapest_source
    if steam_price is not None and cheapest_price:
        out["steam_vs_cheapest_pct"] = (steam_price - cheapest_price) / cheapest_price * 100
    prices = [p for _, p in candidates]
    out["median_price"] = round(statistics.median(prices), 2)
    if len(prices) >= 2 and cheapest_price > 0:
        out["source_spread_pct"] = (max(prices) - cheapest_price) / cheapest_price * 100
        out["high_spread"] = out["source_spread_pct"] >= HIGH_SPREAD_THRESHOLD_PCT
    out["consensus_price"] = out["median_price"] if out["high_spread"] else cheapest_price
    return out


def _confidence(steam_volume: int | None, skinport_price: float | None,
                buff163_price: float | None, csfloat_price: float | None) -> str:
    low_volume = steam_volume is not None and steam_volume < LOW_VOLUME_THRESHOLD
    unconfirmed = skinport_price is None and buff163_price is None and csfloat_price is None
    if low_volume and unconfirmed:
        return "low volume + no 3rd-party match"
    if low_volume:
        return "low volume"
    if unconfirmed:
        return "no 3rd-party match"
    return "ok"


def _price_row(*, collection: str, from_rarity: str, to_rarity: str, stattrak: bool, roi_pct: float,
                ml_predicted_roi_pct: float | None, role: str, skin: str, wear: str,
                probability_pct: float | None, steam_price: float | None, steam_volume: int | None,
                currency: int, abort_check=None) -> PriceRow:
    tp = thirdparty_prices_mod.get_thirdparty_price(skin, wear, stattrak, currency, abort_check)
    skinport_price = tp.get("skinport")
    buff163_price = tp.get("buff163")
    csfloat_price = tp.get("csfloat")
    return PriceRow(
        collection=collection, from_rarity=from_rarity, to_rarity=to_rarity, stattrak=stattrak,
        roi_pct=roi_pct, ml_predicted_roi_pct=ml_predicted_roi_pct, role=role, skin=skin, wear=wear,
        probability_pct=probability_pct, steam_price=steam_price, steam_volume=steam_volume,
        skinport_price=skinport_price, buff163_price=buff163_price, csfloat_price=csfloat_price,
        **_normalize(steam_price, skinport_price, buff163_price, csfloat_price),
        price_confidence=_confidence(steam_volume, skinport_price, buff163_price, csfloat_price))


def build_rows_for_result(r, currency: int, abort_check=None) -> list[PriceRow]:
    """r: a TradeUpResult with roi_pct >= 0 -- callers must filter before calling (this doesn't
    re-check). One input row plus one row per possible output that actually had a Steam price
    (nothing to cross-verify for a drop no price was ever found for). Each output uses ITS OWN
    actual priced wear (PossibleDrop.wear) rather than assuming it matches the input's wear --
    they can differ when an output skin's float range excludes the input's wear bracket (see
    tradeup.py's _closest_available_wear)."""
    rows = [_price_row(
        collection=r.collection, from_rarity=r.from_rarity, to_rarity=r.to_rarity, stattrak=r.stattrak,
        roi_pct=r.roi_pct, ml_predicted_roi_pct=r.ml_predicted_roi_pct, role="input",
        skin=r.cheapest_input_skin, wear=r.cheapest_input_wear, probability_pct=None,
        steam_price=r.cost / 10 if r.cost else None, steam_volume=r.cheapest_input_volume,
        currency=currency, abort_check=abort_check)]

    for d in r.possible_drops:
        if d.price is None:
            continue
        if abort_check and abort_check():
            break
        rows.append(_price_row(
            collection=r.collection, from_rarity=r.from_rarity, to_rarity=r.to_rarity, stattrak=r.stattrak,
            roi_pct=r.roi_pct, ml_predicted_roi_pct=r.ml_predicted_roi_pct, role="output",
            skin=d.name, wear=d.wear or r.cheapest_input_wear, probability_pct=d.probability_pct,
            steam_price=d.price, steam_volume=d.volume, currency=currency, abort_check=abort_check))
    return rows


def build_report(results: list, currency: int, abort_check=None) -> list[PriceRow]:
    """results must already be filtered to roi_pct >= 0 (winning trades only) -- see the module
    docstring for why. Returns every input/output PriceRow across all of them, flattened."""
    rows = []
    for r in results:
        if abort_check and abort_check():
            break
        rows.extend(build_rows_for_result(r, currency, abort_check))
    return rows


def verify_simulation_record(*, primary_collection: str, from_rarity: str, to_rarity: str,
                              stattrak: bool, expected_roi_pct: float, input_mix: list,
                              output_distribution: list, currency: int, abort_check=None) -> list[PriceRow]:
    """Same idea as build_rows_for_result, but for one src/simulate.py SimulationRecord (a
    random-generated, simulated candidate) instead of a live TradeUpResult -- the on-demand,
    live-network half of protecting the guided-simulation feature from anomalous prices (see
    sim_loop.py's module docstring for the OTHER half: an automatic, free, Steam-volume-only
    check applied to every background trial, since this live Skinport/CSFloat check is too
    rate-limited to run on more than a row the user actually opens).

    Verifies EVERY distinct input skin+wear in input_mix (not just "the cheapest input" the way
    build_rows_for_result does -- a simulation can mix several different skins/collections) plus
    every priced entry in output_distribution. Each input row uses its OWN collection (a
    multi-collection mix has more than one); output rows are labeled with primary_collection
    since output_distribution doesn't carry a per-entry collection of its own."""
    rows = []
    for item in input_mix:
        if abort_check and abort_check():
            break
        rows.append(_price_row(
            collection=item["collection"], from_rarity=from_rarity, to_rarity=to_rarity,
            stattrak=stattrak, roi_pct=expected_roi_pct, ml_predicted_roi_pct=None, role="input",
            skin=item["skin"], wear=item["wear"], probability_pct=None,
            steam_price=item["unit_price"], steam_volume=item.get("volume"),
            currency=currency, abort_check=abort_check))
    for item in output_distribution:
        if item.get("price") is None:
            continue
        if abort_check and abort_check():
            break
        rows.append(_price_row(
            collection=primary_collection, from_rarity=from_rarity, to_rarity=to_rarity,
            stattrak=stattrak, roi_pct=expected_roi_pct, ml_predicted_roi_pct=None, role="output",
            skin=item["skin"], wear=item["wear"], probability_pct=item.get("prob_pct"),
            steam_price=item["price"], steam_volume=item.get("volume"),
            currency=currency, abort_check=abort_check))
    return rows


def to_csv_rows(rows: list[PriceRow]) -> list[dict]:
    """PriceRow -> plain dicts with every field in _MONEY_FIELDS rounded to 2 decimals (clean
    spreadsheet output instead of raw floating-point noise like 0.42000000000000004) -- shared by
    both the CLI (`price-report`) and the web dashboard's /api/price-report so the two produce
    byte-identical formatting for the same data."""
    return [{k: (f"{v:.2f}" if k in _MONEY_FIELDS and v is not None else v) for k, v in asdict(row).items()}
            for row in rows]
