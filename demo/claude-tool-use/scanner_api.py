"""The four functions Claude is allowed to call.

Each mirrors a real capability of the scanner; the docstring names the module it
would delegate to in the full pipeline. Here they read the frozen snapshot so the
demo needs nothing but an API key.
"""
from __future__ import annotations

from snapshot import COLLECTIONS, ANOMALIES


def list_collections() -> list[str]:
    """Real pipeline: src/data.py collection index."""
    return sorted(COLLECTIONS)


def scan_tradeups(collection: str, max_cost: float | None = None,
                  min_roi_pct: float | None = None) -> list[dict]:
    """Real pipeline: src/tradeup.py -> ranked +EV trade-up contracts.

    Returns rows for the named collection, newest-cheapest first, after the
    optional cost / ROI filters. Each row carries the full outcome distribution.
    """
    key = _match(collection, COLLECTIONS)
    if key is None:
        return [{"error": f"unknown collection {collection!r}; try one of {list_collections()}"}]
    rows = list(COLLECTIONS[key]["tradeups"])
    if max_cost is not None:
        rows = [r for r in rows if r["cost"] <= max_cost]
    if min_roi_pct is not None:
        rows = [r for r in rows if r["roi_pct"] >= min_roi_pct]
    rows.sort(key=lambda r: r["roi_pct"], reverse=True)
    return rows


def find_anomalies(min_volume_24h: int = 0, min_discount_pct: float = 0.0) -> list[dict]:
    """Real pipeline: src/anomalies.py -> underpriced Steam / marketplace listings."""
    rows = [a for a in ANOMALIES
            if a["volume_24h"] >= min_volume_24h and a["discount_pct"] >= min_discount_pct]
    rows.sort(key=lambda a: a["discount_pct"], reverse=True)
    return rows


def get_skin_price(name: str) -> dict:
    """Real pipeline: src/steam_prices.py + src/thirdparty_prices.py cross-market lookup."""
    for coll in COLLECTIONS.values():
        for skin in coll["skins"]:
            if _norm(name) in _norm(skin["name"]):
                return {"name": skin["name"], "rarity": skin["rarity"],
                        "wear": skin["wear"], "prices": skin["prices"]}
    return {"error": f"no price on file for {name!r}"}


# ---- helpers ----
def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _match(needle: str, keys) -> str | None:
    n = _norm(needle)
    for k in keys:
        if n == _norm(k) or n in _norm(k):
            return k
    return None


# name -> callable, used by the tool-use loop
DISPATCH = {
    "list_collections": list_collections,
    "scan_tradeups": scan_tradeups,
    "find_anomalies": find_anomalies,
    "get_skin_price": get_skin_price,
}

if __name__ == "__main__":  # tiny self-check, no API needed
    assert "Kilowatt" in list_collections()
    assert scan_tradeups("Dreams & Nightmares")[0]["roi_pct"] == 41.9
    assert scan_tradeups("Recoil", min_roi_pct=0) == []            # the only Recoil row is -17% ROI
    assert find_anomalies(min_volume_24h=100)[0]["skin"] == "AK-47 | Ice Coaled"
    assert get_skin_price("black lotus")["rarity"] == "Classified"
    assert "error" in get_skin_price("Butterfly Knife")
    print("scanner_api self-check OK")
