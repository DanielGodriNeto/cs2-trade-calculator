"""Flags Steam Market listings where the lowest ask sits well below the median price.

This is a real, low-effort flip signal: sellers occasionally underprice a listing
(fat-fingered price, panic sale, or just not checking the market first). Buying the
lowest listing and relisting near the median can be profitable if the gap covers
Steam's ~13-15% sell fee plus some margin. Volume is checked to avoid flagging
illiquid items where "median" is noise from 1-2 stale sales.
"""
from dataclasses import dataclass

from .steam_prices import get_price, market_hash_name
from .tradeup import STEAM_SELL_FEE


@dataclass
class Anomaly:
    name: str
    lowest_price: float
    median_price: float
    volume: int
    gap_pct: float
    profit_after_fee: float
    max_buy_price: float  # breakeven price -- pay more than this reselling at median stops being worth it
    image: str = ""  # direct Steam CDN thumbnail URL


def scan_skins(skins: list[dict], currency: int = 1, min_gap_pct: float = 15.0,
               min_volume: int = 5, abort_check=None, fast_mode: bool = False,
               stattrak: bool = False) -> list[Anomaly]:
    """fast_mode: only check one wear per skin -- several times fewer requests, at the cost
    of only spotting anomalies on that one wear.
    stattrak: check only StatTrak variants (True) or only normal variants (False) for
    eligible skins, instead of always checking both -- halves the requests for this scan
    since you're generally only interested in one or the other, not both at once."""
    found = []
    for skin in skins:
        if fast_mode:
            # Field-Tested is typically the most liquid condition, giving the most reliable
            # volume/median reading for a single-wear check (unlike the trade-up revenue
            # estimate, the gap here is relative to the same wear's own median, so which
            # wear is picked doesn't bias the result -- it's just a liquidity choice).
            all_wears = skin["wears"]
            wears = ["Field-Tested"] if "Field-Tested" in all_wears else [all_wears[len(all_wears) // 2]]
        else:
            wears = skin["wears"]
        want_stattrak = stattrak and skin["stattrak"]
        for wear in wears:
            if abort_check and abort_check():
                return sorted(found, key=lambda a: a.profit_after_fee, reverse=True)
            name = market_hash_name(skin["name"], wear, want_stattrak)
            price = get_price(name, currency, abort_check)
            if not price or not price["lowest_price"] or not price["median_price"]:
                continue
            if not price["volume"] or price["volume"] < min_volume:
                continue
            lowest, median = price["lowest_price"], price["median_price"]
            if median <= 0:
                continue
            gap_pct = (median - lowest) / median * 100
            if gap_pct >= min_gap_pct:
                max_buy_price = median * (1 - STEAM_SELL_FEE)
                profit_after_fee = max_buy_price - lowest
                if profit_after_fee > 0:
                    found.append(Anomaly(name, lowest, median, price["volume"], gap_pct, profit_after_fee,
                                          max_buy_price, image=skin.get("image", "")))
    return sorted(found, key=lambda a: a.profit_after_fee, reverse=True)
