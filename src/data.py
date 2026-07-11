"""Downloads and indexes the CS2 skin/collection/rarity database.

Source: ByMykel/CSGO-API (community-maintained, public, no auth needed).
"""
import json
import time
from pathlib import Path
from urllib.request import urlopen, Request

from . import net

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
SKINS_URL = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en/skins.json"
SKINS_CACHE = CACHE_DIR / "skins.json"
CRATES_URL = "https://cdn.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en/crates.json"
CRATES_CACHE = CACHE_DIR / "crates.json"
DATA_TTL_SECONDS = 7 * 24 * 3600  # skin/collection data changes rarely; refresh weekly

# Knives/gloves ("Extraordinary") can't be told apart from real Covert weapon skins by rarity
# id alone: gloves get their own "rarity_ancient" id, but knives are tagged "rarity_ancient_weapon"
# -- the SAME id genuine Covert weapon skins use (e.g. "AK-47 | Fire Serpent" is also
# rarity_ancient_weapon). The only reliable signal for "this is a knife" is the category field.
# (Gloves also happen to carry their own distinct category, "sfui_invpanel_filter_gloves".)
KNIFE_GLOVE_CATEGORY_IDS = {"sfui_invpanel_filter_melee", "sfui_invpanel_filter_gloves"}

# Rarity chain a trade-up contract climbs, low -> high. "Extraordinary" (knives/gloves)
# and "Contraband" are terminal/special drops, not part of the normal chain, so excluded.
RARITY_CHAIN = [
    ("rarity_common_weapon", "Consumer Grade"),
    ("rarity_uncommon_weapon", "Industrial Grade"),
    ("rarity_rare_weapon", "Mil-Spec Grade"),
    ("rarity_mythical_weapon", "Restricted"),
    ("rarity_legendary_weapon", "Classified"),
    ("rarity_ancient_weapon", "Covert"),
]
RARITY_INDEX = {rid: i for i, (rid, _) in enumerate(RARITY_CHAIN)}


def _download(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "cs2-trade-calculator/1.0"})
    with urlopen(req, timeout=30, context=net.SSL_CONTEXT) as resp:
        dest.write_bytes(resp.read())


def _fresh_enough(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < DATA_TTL_SECONDS


def load_raw_skins(force_refresh: bool = False) -> list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if force_refresh or not _fresh_enough(SKINS_CACHE):
        _download(SKINS_URL, SKINS_CACHE)
    return json.loads(SKINS_CACHE.read_text(encoding="utf-8"))


def load_raw_crates(force_refresh: bool = False) -> list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if force_refresh or not _fresh_enough(CRATES_CACHE):
        _download(CRATES_URL, CRATES_CACHE)
    return json.loads(CRATES_CACHE.read_text(encoding="utf-8"))


def load_skins(force_refresh: bool = False) -> list[dict]:
    """Returns a flat, normalized list of tradeable weapon skins eligible for trade-up."""
    raw = load_raw_skins(force_refresh)
    out = []
    for item in raw:
        rarity = item.get("rarity") or {}
        rid = rarity.get("id")
        if rid not in RARITY_INDEX:
            continue  # skip knives/gloves (Extraordinary) and Contraband (M4A4 Howl)
        collections = [c["name"] for c in (item.get("collections") or []) if _is_real_collection(c["name"])]
        if not collections:
            continue  # not from a (real) collection -> can't be farmed via trade-up strategy
        wears = [w["name"] for w in (item.get("wears") or [])]
        if not wears:
            continue
        out.append({
            "name": item["name"],
            "weapon": (item.get("weapon") or {}).get("name", ""),
            "rarity_id": rid,
            "rarity_name": rarity.get("name", ""),
            "rarity_rank": RARITY_INDEX[rid],
            "min_float": item.get("min_float", 0.0),
            "max_float": item.get("max_float", 1.0),
            "stattrak": bool(item.get("stattrak")),
            "wears": wears,
            "collections": collections,
            "crates": [c["name"] for c in (item.get("crates") or [])],
            "image": item.get("image", ""),  # direct Steam CDN URL, straight from the upstream feed
        })
    return out


def load_knives_gloves(force_refresh: bool = False) -> list[dict]:
    """Returns a flat, normalized list of knife/glove ("Extraordinary") skins. These are
    trade-up OUTPUTS ONLY for the Oct-2025 Covert -> knife/glove contract (see src/covert.py)
    -- they're never trade-up inputs, so load_skins() above deliberately excludes them and
    this is kept separate. Filtered by category (see KNIFE_GLOVE_CATEGORY_IDS), not rarity --
    knives share their rarity id with genuine Covert weapon skins, so rarity alone can't tell
    them apart."""
    raw = load_raw_skins(force_refresh)
    out = []
    for item in raw:
        category = item.get("category") or {}
        if category.get("id") not in KNIFE_GLOVE_CATEGORY_IDS:
            continue
        rarity = item.get("rarity") or {}
        wears = [w["name"] for w in (item.get("wears") or [])]
        if not wears:
            continue
        out.append({
            "name": item["name"],
            "weapon": (item.get("weapon") or {}).get("name", ""),
            "rarity_id": rarity.get("id"),
            "rarity_name": rarity.get("name", ""),
            "min_float": item.get("min_float", 0.0),
            "max_float": item.get("max_float", 1.0),
            "stattrak": bool(item.get("stattrak")),
            "wears": wears,
            "crates": [c["name"] for c in (item.get("crates") or [])],
            "image": item.get("image", ""),
        })
    return out


def collections_index(skins: list[dict]) -> dict:
    """collection_name -> rarity_id -> [skin, ...]"""
    idx: dict = {}
    for s in skins:
        for c in s["collections"]:
            idx.setdefault(c, {}).setdefault(s["rarity_id"], []).append(s)
    return idx


_collection_names_cache: dict = {"mtime": None, "names": frozenset()}


def valid_collection_names() -> frozenset:
    """Every real, trade-up-eligible collection name currently in the skin database (post
    _is_real_collection filtering) -- cached until SKINS_CACHE's mtime changes (same freshness
    key the file itself uses, refreshed at most weekly per DATA_TTL_SECONDS), so hot per-entry
    filtering (sim_loop.py's _entry_hidden, run across a son's whole opportunity archive -- can
    be thousands of entries) doesn't re-parse the whole skin database on every single check.
    Used to retroactively hide any already-banked trade-up opportunity that references a
    collection later found to be a data-fallback placeholder (see _is_real_collection) rather
    than a real one -- self-cleans stale entries a son found before this filter existed, same as
    every other _entry_hidden reason."""
    mtime = SKINS_CACHE.stat().st_mtime if SKINS_CACHE.exists() else None
    if mtime is not None and _collection_names_cache["mtime"] == mtime:
        return _collection_names_cache["names"]
    names = frozenset(c for s in load_skins() for c in s["collections"])
    _collection_names_cache["mtime"] = mtime
    _collection_names_cache["names"] = names
    return names


def _is_real_collection(name: str) -> bool:
    """Every genuine CS2 collection -- weapon-case sets and no-case Community/map collections
    alike -- is named "The <X> Collection" (verified against all 92 raw collection tags in the
    upstream feed, 2026-07-11: 91/92 match this pattern). The one outlier, "Limited Edition
    Item", is the upstream feed's fallback bucket for a handful of unrelated XP-Shop/promotional
    skins (e.g. Desert Eagle | Heat Treated, AK-47 | Aphrodite -- different weapons, no shared
    case, "collections[0].id" literally "collection-set-xpshop-wpn-01") that have no real
    Valve-defined collection at all, not a genuine drop-pool set the trade-up contract UI would
    ever let you select. Filtering these out here (general pattern, not a per-skin exclusion)
    means a skin whose ONLY tagged collection is a placeholder like this correctly has no
    tradeup-eligible collection -- see the `if not collections: continue` below, same rule
    already applied to skins with zero collections at all."""
    return name.startswith("The ") and name.endswith("Collection")


_DEFAULT_KNIFE_WEARS = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]


def _fallback_knife_glove(name: str, case_name: str) -> dict:
    """Used when a case's knife/glove finish (from crates.json's contains_rare) isn't in the
    smaller, float-accurate load_knives_gloves() set -- see the APPROXIMATION note on
    cases_index() below for why this gap exists. Assumes the common case: full 0.00-1.00
    float range, all 5 wears, StatTrak-eligible unless it's gloves (Valve doesn't track kills
    on gloves). Narrow-range finishes (e.g. some Doppler-style patterns) that hit this
    fallback will have their wear/price approximated less accurately than ones with real data."""
    is_glove = "Gloves" in name or "Hand Wraps" in name or "Wraps" in name
    return {
        "name": name,
        "weapon": name.split("|")[0].replace("★", "").strip(),
        "rarity_id": "rarity_ancient" if is_glove else "rarity_ancient_weapon",  # matches real data's own ids
        "rarity_name": "Extraordinary",
        "min_float": 0.0,
        "max_float": 1.0,
        "stattrak": not is_glove,
        "wears": list(_DEFAULT_KNIFE_WEARS),
        "crates": [case_name],
        "image": "",  # no accurate image for this documented fallback -- see the APPROXIMATION note above
    }


def cases_index(skins: list[dict], knives_gloves: list[dict], force_refresh: bool = False) -> dict:
    """case_name -> {"covert": [skin, ...], "knives_gloves": [skin, ...]} for the Oct-2025
    Covert -> knife/glove trade-up: 5 Covert skins from the same case can be traded up into
    a knife or gloves from that same case's pool.

    Restricted to real weapon cases (crates.json type == "Case") -- a skin's "crates" field
    also includes souvenir packages, sticker capsules, etc. which aren't reachable by this
    trade-up and would otherwise leak in as fake knife/glove sources.

    APPROXIMATION: the upstream skins.json feed only carries accurate min/max float and wear
    data for a legacy subset of knives/gloves (~94 of them), not the full roster every real
    case actually offers -- so the case's own contains_rare list (which does cover all cases)
    is the source of truth for WHICH finishes exist, cross-referenced against that accurate
    subset where possible and falling back to a documented default (see
    _fallback_knife_glove) for finishes missing accurate float data."""
    raw_crates = load_raw_crates(force_refresh)
    accurate_kg_by_name = {kg["name"]: kg for kg in knives_gloves}

    idx: dict = {}
    for case in raw_crates:
        if case.get("type") != "Case":
            continue
        case_name = case["name"]
        covert_names = {item["name"] for item in (case.get("contains") or [])
                        if (item.get("rarity") or {}).get("name") == "Covert"}
        rare_names = {item["name"] for item in (case.get("contains_rare") or [])}
        if not covert_names or not rare_names:
            continue

        covert_skins = [s for s in skins if s["rarity_id"] == "rarity_ancient_weapon" and s["name"] in covert_names]
        if not covert_skins:
            continue
        kg_skins = [accurate_kg_by_name.get(name) or _fallback_knife_glove(name, case_name) for name in rare_names]

        idx[case_name] = {"covert": covert_skins, "knives_gloves": kg_skins}

    return idx
