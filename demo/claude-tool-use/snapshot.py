"""A small, frozen snapshot of scanner output.

This is NOT live data. It is a hand-picked slice of the kind of rows the real
pipeline produces (src/tradeup.py, src/anomalies.py, src/steam_prices.py), so the
Tool Use demo runs offline, fast, and deterministically. Prices are illustrative.
"""

COLLECTIONS = {
    "Kilowatt": {
        "skins": [
            {"name": "AK-47 | Nightwish", "rarity": "Covert", "wear": "Field-Tested",
             "prices": {"steam": 92.40, "skinport": 88.10, "csfloat": 90.00}},
            {"name": "M4A1-S | Black Lotus", "rarity": "Classified", "wear": "Minimal Wear",
             "prices": {"steam": 41.75, "skinport": 39.20, "csfloat": 40.10}},
            {"name": "USP-S | Jawbreaker", "rarity": "Restricted", "wear": "Field-Tested",
             "prices": {"steam": 6.10, "skinport": 5.55, "csfloat": 5.80}},
            {"name": "Glock-18 | Block-18", "rarity": "Mil-Spec", "wear": "Factory New",
             "prices": {"steam": 1.42, "skinport": 1.18, "csfloat": 1.25}},
            {"name": "MP9 | Featherweight", "rarity": "Mil-Spec", "wear": "Factory New",
             "prices": {"steam": 0.68, "skinport": 0.51, "csfloat": 0.55}},
        ],
        "tradeups": [
            {"input_skin": "MP9 | Featherweight", "input_rarity": "Mil-Spec",
             "output_rarity": "Restricted", "cost": 5.10, "revenue": 6.35,
             "profit": 1.25, "roi_pct": 24.5, "break_even_pct": 80.3,
             "outcomes": [
                 {"skin": "USP-S | Jawbreaker", "prob_pct": 62.5, "price": 5.80, "win": False},
                 {"skin": "Nova | Dark Sigil", "prob_pct": 37.5, "price": 7.25, "win": True},
             ]},
        ],
    },
    "Recoil": {
        "skins": [
            {"name": "AK-47 | Ice Coaled", "rarity": "Classified", "wear": "Field-Tested",
             "prices": {"steam": 18.90, "skinport": 17.40, "csfloat": 17.95}},
            {"name": "Dual Berettas | Flora Carnivora", "rarity": "Restricted", "wear": "Minimal Wear",
             "prices": {"steam": 3.05, "skinport": 2.60, "csfloat": 2.75}},
            {"name": "P250 | Visions", "rarity": "Mil-Spec", "wear": "Factory New",
             "prices": {"steam": 0.92, "skinport": 0.70, "csfloat": 0.76}},
            {"name": "SSG 08 | Rapid Transit", "rarity": "Mil-Spec", "wear": "Factory New",
             "prices": {"steam": 0.60, "skinport": 0.44, "csfloat": 0.47}},
        ],
        "tradeups": [
            {"input_skin": "SSG 08 | Rapid Transit", "input_rarity": "Mil-Spec",
             "output_rarity": "Restricted", "cost": 4.70, "revenue": 3.90,
             "profit": -0.80, "roi_pct": -17.0, "break_even_pct": 120.5,
             "outcomes": [
                 {"skin": "Dual Berettas | Flora Carnivora", "prob_pct": 50.0, "price": 2.75, "win": False},
                 {"skin": "MAC-10 | Toybox", "prob_pct": 50.0, "price": 5.05, "win": True},
             ]},
        ],
    },
    "Dreams & Nightmares": {
        "skins": [
            {"name": "AK-47 | Nightmare", "rarity": "Classified", "wear": "Field-Tested",
             "prices": {"steam": 22.10, "skinport": 20.05, "csfloat": 20.80}},
            {"name": "MP9 | Starlight Protector", "rarity": "Restricted", "wear": "Field-Tested",
             "prices": {"steam": 4.55, "skinport": 3.95, "csfloat": 4.15}},
            {"name": "MP7 | Abyssal Apparition", "rarity": "Mil-Spec", "wear": "Factory New",
             "prices": {"steam": 1.10, "skinport": 0.82, "csfloat": 0.90}},
            {"name": "PP-Bizon | Space Cat", "rarity": "Mil-Spec", "wear": "Factory New",
             "prices": {"steam": 0.75, "skinport": 0.58, "csfloat": 0.62}},
        ],
        "tradeups": [
            {"input_skin": "PP-Bizon | Space Cat", "input_rarity": "Mil-Spec",
             "output_rarity": "Restricted", "cost": 6.20, "revenue": 8.80,
             "profit": 2.60, "roi_pct": 41.9, "break_even_pct": 70.5,
             "outcomes": [
                 {"skin": "MP9 | Starlight Protector", "prob_pct": 40.0, "price": 4.15, "win": False},
                 {"skin": "MP7 | Guerrilla", "prob_pct": 35.0, "price": 9.60, "win": True},
                 {"skin": "Sawed-Off | Spirit Board", "prob_pct": 25.0, "price": 12.30, "win": True},
             ]},
        ],
    },
}

# Underpriced listings the anomaly scanner would flag (lowest ask far below the
# rolling median, filtered by 24h sales volume).
ANOMALIES = [
    {"skin": "AK-47 | Ice Coaled", "collection": "Recoil", "wear": "Field-Tested",
     "lowest_ask": 14.10, "median": 18.60, "discount_pct": 24.2, "volume_24h": 310, "market": "Steam"},
    {"skin": "M4A1-S | Black Lotus", "collection": "Kilowatt", "wear": "Minimal Wear",
     "lowest_ask": 34.00, "median": 41.20, "discount_pct": 17.5, "volume_24h": 95, "market": "Skinport"},
    {"skin": "USP-S | Jawbreaker", "collection": "Kilowatt", "wear": "Field-Tested",
     "lowest_ask": 4.65, "median": 6.05, "discount_pct": 23.1, "volume_24h": 28, "market": "Steam"},
    {"skin": "AK-47 | Nightmare", "collection": "Dreams & Nightmares", "wear": "Well-Worn",
     "lowest_ask": 15.90, "median": 17.10, "discount_pct": 7.0, "volume_24h": 640, "market": "CSFloat"},
]
