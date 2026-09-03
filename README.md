# CS2 Trade-Up & Anomaly Scanner

Finds CS2 trade-up contracts and Steam Market listings that look profitable, using
public price data. This is a **read-only scanner** -- it does not log into Steam,
does not place buy/sell orders, and does not touch any inventory or account.

**Project overview:** <https://danielgodrineto.github.io/cs2-trade-calculator/> --
what it does, how the math works, the architecture, and which parts were my design
vs. AI-assisted build. Source for that page lives in [`docs/`](docs/).

## Why it doesn't auto-buy (yet)

Steam's Subscriber Agreement prohibits bots that automatically buy/sell on the
Steam Community Market. Accounts caught doing this get banned, sometimes with
items or balance forfeited. So this tool deliberately stops at "here are the
opportunities" -- you review and execute manually on Steam.

If you want real automated execution later, the safe path is trading through a
third-party marketplace that **officially supports bot/API trading** (e.g.
Skinport, CSFloat, DMarket -- each publishes a trading API meant for this). That's
a separate integration requiring API keys from that marketplace, and is a good
"Phase 2" once you've validated the calculator finds real edge. See
[Roadmap](#roadmap) below.

## What it does

1. **`tradeups`** -- for every collection, simulates buying 10 of the cheapest
   skin at a given rarity and trading up to the next rarity. Reports cost,
   expected revenue, profit, and ROI%, ranked best first. Each result can be
   expanded to see every possible output skin, its drop probability, and
   whether that specific outcome would be a win or a loss (color-coded).
2. **`anomalies`** -- flags specific listings where the lowest ask is far below
   the median price (a classic "someone underpriced this" flip signal), filtered
   by minimum trade volume so illiquid noise doesn't show up.
3. **`covert`** -- scans the Oct-2025 Covert -> knife/glove trade-up: 5 Covert
   (red) skins from the same weapon case can be traded up into a knife or a
   pair of gloves from that case's own pool. See
   [Covert -> knife/glove trade-ups](#covert---knifeglove-trade-ups-oct-2025) below.
4. **Float-mix strategy** (opt-in, `--float-strategy` / dashboard checkbox) --
   for each trade-up result, finds the cheapest way to mix input wear conditions
   (e.g. some Factory New + some Battle-Scarred) to deliberately steer the
   output into its most valuable wear bracket, instead of leaving it to chance.
   See [Float manipulation](#float-manipulation-float-mix-strategy) below.
5. **Mixed-collection filler strategy** (opt-in, `--mixed-strategy` / dashboard
   checkbox) -- tries diluting a collection's inputs with a cheap "filler" skin
   from a second collection, using the Oct-2025 group-based odds rules, to see
   if that beats using one collection alone. See
   [Manipulating odds with filler skins](#manipulating-odds-with-filler-skins) below.
   Turning **both** this and Float-mix on at once runs the two unified into a
   single **mixed-float strategy** instead (wear-mixing the primary group *and*
   diluting with a filler collection together) -- see
   [Mixed-float strategy](#mixed-float-strategy-float-mix--filler-collection-combined) below.
6. **Buy links** -- every result with ROI >= 0% gets one-click search links
   (Steam Market, CSFloat, Skinport, DMarket, BitSkins) for the cheapest input
   skin, so you don't have to retype the name into five search boxes to go find
   a cheap, high-float copy. See [Buy links](#buy-links) below.
7. **Outcome logging** -- record what actually happened after you executed a trade-up
   manually (real prices paid, real sale price, win or loss), so predicted-vs-actual can be
   compared over time. See [Learning from real trades](#learning-from-real-trades) below.
8. **Third-party price verification** (opt-in, `--verify-prices` / dashboard checkbox) --
   cross-checks the cheapest input's Steam price against Skinport (and CSFloat if you've
   configured a key) to flag a "drought": Steam priced anomalously high vs. everywhere else,
   which usually means temporarily thin Steam supply rather than the skin actually being worth
   more. See [Third-party price verification](#third-party-price-verification-drought-detection)
   below.
9. **ML-assisted ROI calibration** (opt-in, `--use-ml` / dashboard checkbox) -- once you've
   logged enough real outcomes, trains a lightweight model to predict realized ROI and uses it
   as a tiebreaker alongside the formula-based numbers everywhere else in this tool -- never a
   replacement. See [ML-assisted ROI calibration](#ml-assisted-roi-calibration) below.
10. **ML retrain-watch loop** (`retrain-loop` CLI command / always-on in the dashboard) --
    automatically retrains the ROI model the moment enough new outcomes have been logged since
    the last training run. See [ML retrain-watch loop](#ml-retrain-watch-loop) below.
12. **Dashboard explanations, charts, and skin images** -- a "How this works" section explains
    every constant/formula with numbers pulled live from the actual code (not copy-pasted), a
    "Learning Machine -- top picks" section surfaces the ML model's highest-rated live results,
    and skin thumbnails/charts (ROI distribution, feature importances, cumulative profit, price
    trends) appear throughout. See [Dashboard extras](#dashboard-extras-explanations-charts-and-images) below.
13. **Skin price history & prediction** (`price-history` CLI command / dashboard section) --
    records a price history itself (Steam's API has none) starting from first use, and fits a
    naive linear-trend prediction once there's enough data. See
    [Skin price history & prediction](#skin-price-history--prediction) below.
14. **Data archive export/import** (`export-data`/`import-data` CLI commands / dashboard
    button) -- zips up everything accumulated in `data/` for backup or to carry your training
    data to another machine, merging safely (never overwriting real data) on import. See
    [Training across multiple machines](#training-across-multiple-machines) below.
15. **Cross-marketplace price report** (`price-report` CLI command / "Price report (CSV)"
    dashboard button) -- for winning (ROI >= 0) trade-ups only, cross-checks Steam vs.
    Skinport/Buff163/CSFloat prices for the cheapest input AND every possible output skin, and
    exports a spreadsheet-ready CSV. See [Cross-marketplace price report](#cross-marketplace-price-report)
    below.
16. **Real trade-up eligibility checks** -- a skin only enters a scan, a random candidate, or
    the manual builder if it can ACTUALLY be traded up: its collection must have skins at the
    next rarity tier (the in-game contract rule, now enforced in `rules_engine.validate_contract`),
    and non-souvenir copies must actually trade (souvenir items can't enter a contract). See
    [Trade-up eligibility](#trade-up-eligibility-can-this-skin-actually-be-traded-up) below.
17. **Buff163 as a second independent price source** (2026-07) -- the same csgotrader.app bulk
    mirror also publishes Buff163 (the deepest third-party CS2 marketplace) listing prices, now
    wired into drought detection, Stage-2 verification, price reports, and the no-Steam-price
    fallback alongside Skinport. See
    [Third-party price verification](#third-party-price-verification-drought-detection).
18. **Manual trade-up builder** (dashboard, Trade-Up Calculator page) -- compose a contract from
    ANY skins, mixing collections freely (group-based odds), with per-skin eligibility errors and
    live pricing through the same `simulate.py` math the agents use. See
    [Manual trade-up builder](#manual-trade-up-builder) below.
19. **Output wear-landing odds** -- every possible output (scan drops, simulated trades, the
    manual builder) now shows the honest per-wear-bracket chance of where its float could land
    (e.g. `FT 72% / MW 28%`), sampled across each input's full wear range instead of only the
    midpoint. See [Output wear-landing odds](#output-wear-landing-odds) below.
20. **"Verify top 50" + mispriced-skin feedback** (dashboard, Results page) -- one click queues
    the current top 50 opportunities for the live Stage-2 cross-check; any skin whose Steam price
    disagrees with the verified market by 20%+ is registered over/undervalued and the agents'
    future simulations penalize it, so training routes around it automatically. See
    [Verify top 50 & the mispriced-skin registry](#verify-top-50--the-mispriced-skin-registry) below.
22. **Meta Agents** -- two additional named agents (Meta Alpha, Meta Beta) alongside the four
    specialists, each with its own fixed personality and fully independent accumulated
    experience/model, storage-isolated like every other son. (A prior cross-agent evolution
    system that periodically bred their base personalities from the specialists was removed
    2026-07 -- see [Removed features](#removed-features).)
23. **Market Reliability Engine** -- unifies dead-skin scoring, the mispriced registry, and
    cross-source consensus into one auditable Healthy/Suspicious/Excluded state per skin, adds
    rolling variance/z-score outlier detection, and gates ML training, ranking, and best-list
    admission. Manual override always available; every exclusion re-verifies automatically.
    See [Market Reliability Engine](#market-reliability-engine) below.
24. **Reliability-weighted ML training** -- training samples no longer contribute equally:
    each row is weighted by how well its skins verify across marketplaces (1.0 fully verified
    -> 0 known anomaly) instead of being deleted. See
    [Reliability-weighted training](#reliability-weighted-training) below.
26. **Rare Pattern Arbitrage Engine** (dedicated "Rare Patterns" dashboard page) -- scans
    Steam Market listings directly for documented collector patterns (Blue Gems, Case
    Hardened tiers, Doppler phases, Katowice crafts, ...) priced at ordinary market value,
    matched against a centralized, versioned Pattern Knowledge Database -- never inferred by
    ML. See [Rare Pattern Arbitrage Engine](#rare-pattern-arbitrage-engine) below.
27. **Automated GitHub backup engine** -- a delta-aware background worker that SHA-256-hashes
    the telemetry files every 6h and commits+pushes a structured archive to a configured git
    repo only when something actually changed. See
    [Automated GitHub backup engine](#automated-github-backup-engine) below.
28. **Steam-sellable revenue only** (2026-07) -- an output skin's revenue must be backed by
    actual Steam sale evidence: outputs are never priced from the Skinport/Buff163 fallback
    anymore. A skin that only trades ABOVE Steam's price cap (e.g. AWP | Gungnir: zero Steam
    sales in any mirror window, $6k-12k third-party listings) used to be "sold" in the math at
    a price it can never fetch on Steam, producing absurd fake ROI. Existing best-list entries
    contaminated this way are hidden retroactively. See
    [Steam-sellable outputs](#steam-sellable-outputs-the-gungnir-fix) below.
29. **Skin blacklist & manual price overrides** (2026-07, "Market intelligence"
    panels / `skin-blacklist` & `price-override` CLI commands) -- user-managed and permanent by
    default (unlike every self-healing registry here): blacklist a skin to ban it everywhere
    (inputs AND outputs, existing trade-ups removed) -- forever, or for a number of days you
    choose -- or set your own price for a skin+wear to override every source and have existing
    trade-ups' ROI recomputed. A one-click blacklist button sits on every skin in a result
    row's input/output tables, so the skin behind an absurd ROI can be banned where you spot
    it. See
    [Skin blacklist & manual price overrides](#skin-blacklist--manual-price-overrides)
    below.
30. **Premium spreadsheet workstation** (`/api/export/xlsx`, "Export full spreadsheet" button) --
    the one-click Excel workbook now opens with three polished, chart-driven sheets (📊 Executive
    Dashboard, 🚀 Live Opportunities, 🛡 Reliability & Audit) in front of the original full-detail
    sheets. Local .xlsx by choice -- see `src/xlsx_export.py`'s docstring for why this replaces a
    Google Sheets integration.

Data sources: [ByMykel/CSGO-API](https://github.com/ByMykel/CSGO-API) (skin/rarity/
collection/case database) and, since 2026-07, the free
[csgotrader.app](https://csgotrader.app) bulk price mirror (`src/bulk_prices.py`) -- real
Steam Market median-sale prices plus Skinport and Buff163 listing prices, one bulk download,
no login, no rate limits. Steam's own `priceoverview` endpoint now answers essentially every anonymous
request with HTTP 429 (verified live 2026-07-10), so the old per-item live fetch survives
only as an opt-in fallback (`steam_prices.configure_steam_live_fallback`).

## Float manipulation (float-mix strategy)

A trade-up's output float is the average of the 10 input floats (each normalized
to its own skin's range), re-applied to the output skin's range. That means you
can deliberately steer the result: buying, say, 6 Factory New + 4 Battle-Scarred
inputs instead of 10 of whatever's cheapest can push the output into a specific,
more valuable wear bracket.

Turn it on with `--float-strategy` (CLI) or the "Float-mix strategy" checkbox
(dashboard). For each result it searches every achievable target wear bracket,
works out the cheapest input mix to land there, and shows whether that actually
beats just buying the cheapest input outright. It's opt-in because it needs
per-wear pricing for the input skin *and* every possible output skin -- several
times more requests than the default scan.

**Approximation**: Steam's public API doesn't expose exact per-listing float
values, only wear-condition buckets. This estimates each wear by the midpoint of
its standard float range (Factory New 0.00-0.07, ... Battle-Scarred 0.45-1.00)
and assumes most skins span the full 0.00-1.00 range, which holds for the
majority of weapon skins but not all. It also only tries mixes of up to two wear
conditions (not arbitrary combinations of three or more), which covers the
useful cases without a combinatorial search. Verify a recommended mix on a
float-checking site before actually buying it.

## Trade-up eligibility: can this skin actually be traded up?

Two real-game rules used to be under-enforced (`src/tradability.py`, 2026-07):

1. **Structural**: the game refuses a skin whose collection has no skins at the next rarity
   tier -- and a Covert weapon skin can't enter the standard 10-input contract at all (only
   the separate 5-input Covert -> knife/glove path consumes those).
   `rules_engine.validate_contract` now checks every leg's collection actually has outputs at
   the target rarity and every input skin is really at the contract's from-rarity -- so no
   random/mutated candidate that isn't a real contract can ever reach pricing.
2. **Market existence**: souvenir items can NEVER be used in a trade-up, so an input is only
   real if **non-souvenir copies actually trade**. The upstream skin database can't tell a
   souvenir-only skin from a droppable one (both look identical in its `crates` field --
   verified against Dragon Lore vs. P250 | Sand Dune), so the oracle is the market itself:
   a skin with **no Steam median-sale price at any wear across the mirror's 24h-90d windows**
   (`bulk_prices.has_market_listing`) either doesn't exist non-souvenir or has literally
   nothing to buy -- either way it can't honestly be a trade-up input today. This check is
   deliberately **steam-feed-only**: the mirror's skinport/buff163 dumps list souvenir-only
   items under the plain (unprefixed) name too (verified 2026-07-10: both price
   "AWP | Dragon Lore" at the souvenir item's ~$6.5k), so they'd false-positive exactly the
   skins this exists to catch.

Applied in three places, all filtering **inputs only** (an output never needs to be bought):
the tradeups scan skips ineligible input skins before spending any price lookups, the
multi-agent population excludes them from candidate generation up front (unioned into the
same exclusion set `invalid_skins.py` already feeds, but known ahead of time instead of
learned one failed pricing at a time), and the manual builder rejects them with a
per-skin reason. Degrades gracefully: if the bulk feed is unreachable, nothing is filtered --
an outage must never brand the whole catalog untradeable.

3. **Real collection, not a data-fallback bucket** (`src/data.py`, 2026-07-11): a skin's
   "collection" as reported by the upstream feed isn't always a genuine Valve collection with a
   real drop pool. Every actual CS2 collection -- weapon-case sets and no-case Community/map
   collections alike -- is named `"The <X> Collection"`; the one exception found in the feed,
   `"Limited Edition Item"`, is a fallback tag the upstream API applies to a handful of
   unrelated XP-Shop/promotional skins (e.g. Desert Eagle | Heat Treated, AK-47 | Aphrodite --
   different weapons, no shared case) that have no real collection at all. Trusting it as a
   normal collection let the scanner and the guided simulation build "trade-ups" between skins
   that were never really part of the same drop pool. `data.load_skins()` now drops any
   collection tag that doesn't match the real naming pattern (`data._is_real_collection`) --
   general, not a per-skin exclusion -- so a skin whose only tagged collection is a placeholder
   correctly has no trade-up-eligible collection at all, same as one with zero collections.
   Already-banked opportunities that reference a since-invalidated collection self-clean out of
   every best/archive list the next time they're read (`sim_loop._entry_uses_unreal_collection`).

## Steam-sellable outputs (the Gungnir fix)

Every revenue number in this tool means "sell on the Steam Market, minus the 13% fee" -- so an
output's price must be backed by actual **Steam sale evidence**. `simulate.py` used to fall
back to the cheapest Skinport/Buff163 listing when Steam had no price for an output, which
quietly broke that contract for a whole class of skins: anything that only really trades
**above Steam's price cap**. Verified live (2026-07-11): the mirror's Steam feed carries
`AWP | Gungnir` with every sale window (24h-90d) null -- zero Steam sales at any wear -- while
Skinport/Buff163 list it at $6,200-$12,600. Trade-ups producing it were "selling" it at those
prices, inflating expected ROI by thousands of percent, and nothing flagged it because the
price was real -- just not realizable on the market the math assumes.

The fix (`simulate._price(allow_thirdparty=False)`, applied to every output):

- **Outputs are priced from Steam-confirmed data only** (cache / mirror / live fetch). No
  Steam price = that output contributes no revenue; if nothing on the output side has a Steam
  price, the whole candidate is invalid -- same "refuse rather than fabricate" rule as
  everywhere else. Such outputs are tagged `not_sellable_on_steam` in their row for
  visibility when the Steam feed positively confirms the absence.
- **Inputs keep the third-party fallback** -- buying an input on another marketplace and
  trading it in is real; selling an output above Steam's cap on Steam is not.
- **Retroactive**: existing best-list/archive entries whose revenue rests on an output with
  no Steam sale evidence (`bulk_prices.has_market_listing` is False for that exact
  market_hash_name) are hidden from the opportunity lists immediately and evicted from
  checkpoints by the same self-clean `_consider_best` already runs -- no re-simulation
  needed. A user [price override](#skin-blacklist--manual-price-overrides) on the
  output is exempt (your own number is authoritative). Feed outage degrades to no filtering,
  never to "everything is unsellable".

The classic `tradeups` scan was never affected (it prices outputs via the Steam mirror only);
this closes the gap in the simulation population, the manual builder, and everything else
that goes through `simulate.py`.

## Skin blacklist & manual price overrides

Every protective registry above (dead skins, invalid skins, mispriced marks, wear anomalies,
market reliability) is deliberately temporary and self-healing -- right for transient market
weirdness, useless when you've personally confirmed a skin's data is garbage and want it gone.
`src/user_overrides.py` adds the user-managed counterpart: nothing here decays or re-checks,
and entries change only when you say so (dashboard "Market intelligence" panels,
`/api/skin-blacklist` + `/api/price-overrides` endpoints, or the `skin-blacklist` /
`price-override` CLI commands).

**Skin blacklist** (`data/user_skin_blacklist.json`, keyed by bare skin name, all wears and
StatTrak variants at once). A blacklisted skin is banned as an input **and** as a possible
output -- a contract that could land on it can't be honestly valued, so the whole trade-up
goes, not just the one price:

- agents stop proposing candidates containing it (excluded from generation up front, and
  `simulate.simulate()` hard-rejects anything that slips through mutation/evolution);
- the `tradeups`/Covert scans skip it as an input and skip any rarity step / case whose
  output pool contains it (mixed/filler searches apply the same rule to filler collections);
- the manual builder rejects it with a per-skin reason;
- every EXISTING opportunity containing it disappears from the leaderboard/archive at once
  -- and from the Collector Arbitrage list on the same page. One bad price routinely poisons
  dozens of rows (a single skin can be a possible output of 50 different contracts), so this is
  deliberately all-or-nothing rather than a per-row dismissal. Sons self-clean their
  checkpoints on their next round -- same mechanism as the
  [trade-up blacklist](#blacklisting-a-trade-up-youve-already-done), which stays separate:
  that one hides a single composition you already executed; this one bans a skin outright.

**How long** is yours to pick, per entry. The default everywhere -- panel dropdown on
*Forever*, empty answer in the row-button prompt, no `--days` on the CLI -- is **permanent**:
the entry stands until you delete it, and nothing ever re-checks the skin or revives it. Give
it a duration instead (1/7/30/90 days in the panel, any number of days from the CLI or the row
prompt) and it stops applying, and drops off the list, by itself when that runs out. This is
still your intent, not self-healing: the tool never shortens, extends, or second-guesses a
duration you set. Re-blacklisting a skin already on the list **replaces** its duration, so
adding it again is how you extend one or promote it to permanent.

**Where to click**: every skin name in a result row's Inputs and Possible-outputs tables
carries two small buttons -- **⛔** blacklists that skin (bare name: all wears and StatTrak
variants), asking the duration in one prompt, and **💲** sets a price override for that exact
skin **+ wear** instead. That's the fast path when a wrong price
has produced an absurd ROI: ban it on the row where you found it, and the leaderboard drops
that opportunity along with every other one touching the skin. The full panel (Simulation Lab
-> "Market intelligence & risk registries" -> "Skin blacklist") is where you review, remove,
and see each entry's expiry.

Reach for **💲** rather than **⛔** when the skin is fine and only its *number* is wrong:
blacklisting discards a possibly-real trade-up, overriding repairs it -- the row survives with
its ROI recomputed and a "repriced" badge. Both act on every affected list at once.

CLI: `python -m src.main skin-blacklist [--add "AWP | Gungnir" [--note why] [--days N]]
[--remove NAME]`.

**Price overrides** (`data/price_overrides.json`, keyed by full market_hash_name -- skin +
wear + StatTrak). Your price **wins over every other source**: `steam_prices.get_price()` and
`get_cached_price()` return it before consulting the cache, the bulk mirror, or a live fetch,
so every scan, simulation, strategy search, and the manual builder price with it
automatically (`"source": "user_override"`; never cached, never recorded into price history).
Entered in any supported currency and converted through the same exchange-rate feed the bulk
prices use. On top of that, existing best-list/archive entries containing the skin are
**re-priced at list time** (`repriced_entry_copy`: matching unit/output prices swapped on a
copy, cost / per-output net profit / expected ROI recomputed with the same after-fee math,
tagged with a "repriced" badge in the dashboard) -- so a Min-ROI filter catches bugged
trade-ups immediately, and removing the override reverts everything just as instantly
(the stored as-simulated numbers are never mutated).

CLI: `python -m src.main price-override [--skin NAME --wear WEAR [--stattrak] --price N
[--currency C]] [--remove "NAME (WEAR)"]`.

## Manual trade-up builder

The dashboard's Trade-Up Calculator page now has a **Manual trade-up builder**: type skin
names (autocomplete), pick a wear and count for each, and mix skins **from any number of
different collections** -- the post-Oct-2025 group-based odds are exactly what makes that
interesting (each collection's share of the 10 inputs is its total chance, split evenly
across that collection's own outputs at the target rarity).

Once 10 inputs are in, "Compute trade-up" runs the composition through the **same** code path
the agents use -- `rules_engine.validate_contract` then `simulate.simulate` (live pricing,
Steam mirror first, Skinport/Buff163 fallback) -- never a parallel re-implementation. The
result shows total cost, per-output drop odds, prices/volumes, probability-weighted expected
ROI after the Steam fee, and each output's [wear-landing odds](#output-wear-landing-odds).
Ineligible skins are rejected at add/compute time with the specific reason (wrong rarity mix,
collection has no next-tier outputs, souvenir-only/nothing to buy, wear not available,
no StatTrak variant). Endpoint: POST `/api/manual-tradeup`.

## Output wear-landing odds

`oddsmath.remap_float_to_wear` answers "which wear does the output land in" with a single
number built from wear-bracket **midpoints** -- fine as an estimate, but near a bracket edge
the true answer is genuinely split. `oddsmath.sample_avg_normalized_floats` +
`wear_probabilities_for_output` (2026-07) sample each input's float uniformly across its full
wear bracket (clamped to that skin's own min/max range, 600 deterministic draws), average
them the same way the game does, and remap per output skin -- yielding an honest
`{wear: probability}` split like `FT 72% / MW 28%`.

Shown as chips in a "Wear odds" column on every drops table: scan results
(`PossibleDrop.wear_probs`), simulated trades (`output_distribution[].wear_probs`), and the
manual builder. The single most-likely priced wear is still what the revenue math uses (same
approximation as before); the odds column is visibility into how confident that assumption
actually is. Same caveat as all float math here: Steam's API only exposes wear buckets, not
per-listing floats -- verify on a float-checking site before buying.

## Verify top 50 & the mispriced-skin registry

The Results page's **"Verify top 50 (live cross-check)"** button force-queues the current
"Trade-up opportunities" leaderboard's top 50 rows into the existing
[Stage-2 verification queue](#automatic-stage-2-verification-queue) -- same one-at-a-time,
rate-limit-respecting consumer, it just skips the automatic queue's eligibility bar (the
point is auditing the leaderboard as it stands). Verified ROI / price confidence land on each
row exactly as with the automatic queue.

What's new is the **feedback loop** (`src/mispriced_skins.py`): every verification -- automatic,
manual button, or this sweep -- feeds its per-skin gaps into a shared registry. A skin whose
Steam price sits **20%+ away** from the cheapest independently verified price
(Skinport/Buff163/CSFloat) is marked **overvalued** (Steam above the market: inflates a
trade-up's expected revenue, i.e. fake ROI) or **undervalued** (distorts costs/features the
other way). While a mark is active, `simulate.py` adds enough dead-skin points to any trial
containing that skin to cross `DEAD_SKIN_THRESHOLD` on its own -- which excludes those trials
from every agent's ML training set and from strict agents' best-known lists, the same
machinery `low_volume_warning` already drives. That's the "adjust the bots' training to avoid
the over/undervalued skin" loop: no model surgery, just an honest data-quality gate the
models already respect.

Like every protective registry here, it's **temporary and self-healing, never a permanent
blacklist**: marks expire after 24h, and any later verification showing the gap back under
threshold clears the mark immediately. `python -m src.main mispriced-skins` (or the
dashboard's Market intelligence section) lists everything currently marked. Endpoints:
POST `/api/sim-population/verify-top`, GET `/api/mispriced-skins`,
POST `/api/mispriced-skins/clear`.

## Manipulating odds with filler skins

The October 2025 CS2 update changed trade-up odds to be **group-based**: each
collection's (or, for Covert trade-ups, each case's) share of the 10 inputs
determines its total chance, split evenly across that group's own outputs at
the target rarity. Concretely: 9 of 10 inputs from Collection A (which has 1
possible output) and 1 input from Collection B (which has 3 possible outputs)
gives Collection A's output a 90% chance, and each of Collection B's three
outputs a ~3.33% chance. See `src/oddsmath.py` for the shared implementation
(also used by the Covert -> knife/glove scan below).

That means deliberately diluting a collection's inputs with a handful of cheap
"filler" skins from a second collection can improve expected value, by shaving
odds away from a collection's worst outcomes rather than eliminating them
outright. Turn this search on with `--mixed-strategy` (CLI) or the
"Mixed-collection filler strategy" checkbox (dashboard): for each result, it
tries a shortlist of other collections (capped, since each candidate costs
requests) as filler at 1-4 of the 10 slots, and reports the best split if it
actually beats using the primary collection alone.

**Scope**: this searches primary + *one* filler collection, not arbitrary
combinations of three or more -- an opt-in "does mixing help here" check, not
an exhaustive optimizer. It also doesn't yet account for order-book depth (see
[Important limitations](#important-limitations-read-before-trusting-the-numbers)
below) -- the same caveat that applies to every cost figure in this tool.

## Mixed-float strategy (float-mix + filler collection, combined)

Float-mix and the mixed-collection filler strategy each only capture half the
picture on their own: float-mix only ever buys from one collection, and the
filler search only ever buys each group at its single cheapest wear. But the
wear split still determines where the *primary* group's own outputs land even
once a filler collection is diluting the odds -- so mixing wears within the
primary group **and** diluting with a filler collection at the same time can
beat either strategy alone.

Turning on **both** `--float-strategy` and `--mixed-strategy` (or both
dashboard checkboxes) together runs this unified search instead of the two
separately -- there's no point computing the float-only and filler-only
results on top of it, since the combined search is a strict superset of both.
It's restricted to trade-ups whose plain (no-strategy) ROI is **already >= 0%**:
this is an "can we improve an already-workable trade" search, not an attempt
to rescue a losing one, so the added request cost (both strategies' pricing
combined) stays bounded to results actually worth the scrutiny. In the
dashboard, a result with a better combined-strategy ROI is flagged right on
the collapsed row (an "↑ N% available" badge) so you don't have to expand
every row to notice one.

## Covert -> knife/glove trade-ups (Oct 2025)

The same update also means Covert (red) skins are no longer the end of the
trade-up chain: 5 Covert skins from the same weapon case can be traded up into
one knife or a pair of gloves from that case's own pool (5 StatTrak Covert ->
StatTrak knife only -- gloves have no StatTrak variant). Run it with
`python -m src.main covert` or the "Scan Covert -> knife/glove trade-ups"
checkbox in the dashboard.

**Data note**: case membership and knife/glove pools come from
[ByMykel/CSGO-API's crates.json](https://github.com/ByMykel/CSGO-API), filtered
to real weapon cases (`type == "Case"`, 42 of them) so souvenir packages and
sticker capsules -- which also populate a skin's `crates` field -- don't leak
in as fake knife sources. Float/wear data is accurate for the ~94 knives/gloves
present in the main skin feed; the rest (most of them, since that feed only
carries a legacy subset) fall back to a documented approximation (full
0.00-1.00 float range, all 5 wears) rather than being silently dropped -- see
`_fallback_knife_glove()` in `src/data.py`.

## Buy links

Every trade-up, mixed-strategy, and Covert result with ROI >= 0% includes
one-click search links for the cheapest input skin+wear on Steam Market,
CSFloat, Skinport, DMarket, and BitSkins -- so once you've found a genuinely
+EV contract, you don't have to retype the skin name into five different
search boxes to go hunt for a cheap, high-float copy. These are deep-links
into each site's own search page (not an API integration), so treat a dead
link as "that site changed its URL scheme" rather than a pricing bug -- and
as always, verify the actual float/price before buying. Links are withheld
for losing (ROI < 0%) results since there's no point sending you shopping for
a trade that loses money.

## Learning from real trades

This tool never auto-buys or auto-sells (see [Why it doesn't auto-buy](#why-it-doesnt-auto-buy-yet)
above), so it can't observe your real outcomes on its own -- you log them yourself after
executing a trade-up manually. `python -m src.main log-outcome ...` (see
[CLI usage](#cli-usage) below) or the "Log a trade-up outcome" form in the dashboard records the
exact wear mix you actually bought, the real prices paid, what you actually got back, and what
it actually sold for. The tool computes cost/profit/ROI/win-or-loss from that (not from your own
arithmetic), and stores it in `data/outcomes.jsonl` -- a plain-text, append-only log that's
**not** touched by anything that clears `cache/` (that directory is disposable/regenerable
price data; your logged trade history is the opposite).

This log is the training data for [ML-assisted ROI calibration](#ml-assisted-roi-calibration)
below, and lets you compare what the tool predicted against what actually happened
(`predicted_roi_pct` vs. the logged `roi_pct`) as your own record of whether this tool's
estimates are trustworthy for the collections you actually trade.

`python -m src.main list-outcomes` prints everything you've logged so far.

**Dashboard shortcut**: every scan result row (trade-ups and Covert) has a "&#128203; Log this"
button that pre-fills the outcome form below with everything the scan already knows --
collection/case, rarity, the recommended wear-mix if a float strategy was found, and the
formula's predicted ROI -- so you only have to type in the output skin and what you actually
sold it for after executing it manually. This is deliberately just a form-fill shortcut, not
automatic logging: **only what you confirm and submit** ever gets saved, and only sold outcomes
ever train the ML model (see the correctness guardrail above) -- the scan's own predictions are
never fed into training directly, since that would just teach the model to echo its own formula
back rather than calibrate against anything real.

## Third-party price verification (drought detection)

Every cost/revenue number elsewhere in this tool comes from Steam Market prices alone. That's
usually fine, but Steam's own supply for a specific skin+wear can occasionally run thin (a
"drought"), pushing its Steam price well above what the same skin is actually selling for on
other markets -- in which case treating that inflated Steam price as normal would make a losing
trade look profitable, or make an already-good trade look better than it is.

Turn this on with `--verify-prices` (CLI) or the "Verify 3rd-party prices" checkbox (dashboard):
for each result, the cheapest input's Steam price is cross-checked against **two independent
sources enabled by default** -- Skinport and, since 2026-07, **Buff163** (the deepest
third-party CS2 marketplace, whose much larger book usually still lists the cheap trade-up
fodder Skinport doesn't carry -- exactly the gap behind most old "no 3rd-party match" rows) --
plus CSFloat if you've set a `CSFLOAT_API_KEY` environment variable. Both default sources come
from the same csgotrader.app bulk mirror (`bulk_prices.get_skinport_map`/`get_buff163_map`),
so neither costs extra live requests. A result gets a "drought" flag when Steam is priced 15%+
above the cheapest verified alternative (`src/thirdparty_prices.py`'s `drought_flag()`).

**This is an annotation only** -- a drought flag never changes `cost`/`expected_revenue`/
`profit`/`roi_pct` anywhere; it's a separate `thirdparty_check` field you should use as a reason
to double-check a result manually before buying, same spirit as this tool's existing
wear-midpoint float approximation. DMarket and BitSkins aren't wired up yet -- both require an
account-issued API key for market-data reads even read-only, unlike Skinport/CSFloat's genuinely
public endpoints, so they're documented stubs in `src/thirdparty_prices.py` for a future phase.

**Diagnostics**: a genuine "no listings found" and "couldn't reach the source at all" both just
look like a missing price from a single lookup, so `src/thirdparty_prices.py` tracks the real
outcome of every fetch attempt (`get_stats()`) -- surfaced as a "3rd-party price checks" line in
the dashboard, right under the Steam network-health panel. Since 2026-07 the Skinport prices
come from the csgotrader.app bulk mirror (`src/bulk_prices.py`) rather than Skinport's own API:
that endpoint's Cloudflare Bot Management now 403s every non-browser client (verified live
2026-07-10, residential IP included -- the old "works from a residential connection" note was
overtaken by events). When the mirror is unreachable, this tool degrades gracefully (no crash,
no drought flags, exactly as if there were no third-party data at all) -- check the dashboard's
status line if drought detection seems to never fire, rather than assuming there just aren't
any droughts.

## Cross-marketplace price report

Drought detection above only ever checks the *cheapest input*. `price-report` (CLI) / the
"Price report (CSV)" button (dashboard, next to "Trade-up opportunities") goes further: for
every currently winning (ROI >= 0) trade-up, it cross-checks Steam vs. Skinport/CSFloat for
**both** the cheapest input skin+wear **and every possible output skin**, and exports one row
per skin to a CSV -- collection, rarity path, role (input/output), skin, wear, each source's
price, Steam's own sale volume, the cheapest verified price/source, the % Steam sits above or
below it, the cross-site consensus fields (see below), and a `price_confidence` verdict (see
below). Opens directly in Excel/Google Sheets/etc.

**Consensus price (median across sites)**: when the sources that returned a price for a skin
disagree badly with *each other* (priciest >= 20% above cheapest --
`price_report.HIGH_SPREAD_THRESHOLD_PCT`, same bar as the mispriced-skin registry's), no single
site's number deserves to be trusted alone: the cheapest one is just as likely the outlier (a
stale mirror entry, a fat-fingered listing) as the priciest. Every row therefore also carries
`median_price` (the median of however many of the up-to-4 sources -- Steam, Skinport, Buff163,
CSFloat -- actually returned a price), `n_sources`, `source_spread_pct`, a `high_spread` flag,
and `consensus_price`: the plain cheapest price normally, but the **median** when the spread is
high. The [automatic Stage-2 verification queue](#automatic-stage-2-verification-queue) computes
its verified ROI from `consensus_price`, and the dashboard's verify table shows a
"median of N" badge on any row where the median kicked in.

**Liquidity check ("does this price actually make sense")**: a Steam price -- especially an
output skin's, which this tool otherwise takes at face value -- can be set by just one or two
recent sales, i.e. a real listing that's still an outlier nobody else is actually trading at, not
a price you could reliably sell at. Every priced trade-up drop now also carries Steam's own
recent-sales `volume` (surfaced directly in the dashboard's expanded drop table as a Volume
column, with a "⚠ low volume (N)" badge when it's under `LOW_VOLUME_THRESHOLD` -- 5, same
convention as [underpriced-listing scanning](#cli-usage)'s own `--min-volume` default). The price
report's `price_confidence` column combines that with whether Skinport/CSFloat independently
show anything close: `"ok"`, `"low volume"`, `"no 3rd-party match"`, or (the one actually worth
double-checking manually before buying) `"low volume + no 3rd-party match"` -- Steam's the only
source showing a price at all, and it's resting on very few real trades.

Steam prices are never re-fetched for this -- they're reused straight from the scan that already
found the winning result (see [Important limitations](#important-limitations-read-before-trusting-the-numbers)
for the same wear-bracket approximation that applies everywhere else in this tool), so the only
new network cost is the cross-marketplace verification itself. Restricted to ROI >= 0 results for
the same reason [buy links](#buy-links) and [third-party verification](#third-party-price-verification-drought-detection)
already are: no point spending rate-limited Skinport/CSFloat requests verifying a trade you
wouldn't act on anyway.

**Three modes** (CLI flags / dashboard scan settings -- these are the same knobs the regular scan
already exposes, not new ones specific to this feature):

1. **Fast** -- `--fast-mode` (CLI) / "Fast mode" checkbox (dashboard): all collections, one wear
   priced per skin. Quickest, roughest cost numbers.
2. **Full** -- no flag / checkbox unchecked (the default): every wear priced per skin. Slower,
   most accurate.
3. **ML picks only** -- `--ml-only` (CLI) / "ML picks only" checkbox (dashboard): only verifies
   trades the trained ML model *also* predicts a non-negative ROI for, not just the plain
   formula -- needs a trained model first (see [ML-assisted ROI calibration](#ml-assisted-roi-calibration)
   below; `--ml-only` implies `--use-ml`).

CLI: `python -m src.main price-report --collection "Dead Hand" --top 20` (add `--fast-mode` or
`--ml-only`, omit `--collection` to scan everything, `--out` to name the CSV). The dashboard
button instead reuses whatever the *current* scan already found (respecting its Min ROI% and
Fast mode settings) rather than starting a new scan, so it's fast even when the CLI command
covering the same scope wouldn't be -- run/rescan first, then click "Price report (CSV)".

## ML-assisted ROI calibration

Once you've logged **20+ sold outcomes** (`log-outcome`, see
[Learning from real trades](#learning-from-real-trades) above), `python -m src.main train-model`
(or the dashboard's "Train now" button) fits a lightweight scikit-learn model
(`HistGradientBoostingRegressor`) that predicts realized ROI% from a trade's features -- wear-mix
composition, whether a filler collection was used, StatTrak, cost, the tool's own formula ROI at
the time, and how that collection has historically performed for you. Turn it on with `--use-ml`
(CLI) or the "Use ML calibration" checkbox (dashboard).

**This never replaces the formula-based ROI anywhere** -- it only adds a separate
`ml_predicted_roi_pct` field (shown as a `(ML) N%` badge in the dashboard) and is consulted as a
**tiebreaker** when the float-mix/mixed-float search finds two candidates with near-identical
formula ROI, never to override a clear formula winner. Below 20 logged outcomes, `train-model`
refuses with a clear reason instead of training on too little data, and every ML field is simply
absent -- the tool behaves exactly as it does without this feature until you've logged enough
trades. Realistically, this needs 50-100+ outcomes spread across a handful of repeated
collections before its predictions carry real signal -- treat it as a long-horizon feature, not
an instant upgrade.

**Correctness guardrail**: only real, user-logged outcomes (actual money spent/received) ever
train this model -- never the tool's own formula estimates or the guided-simulation population's
exploratory findings (see [Guided random-trade simulation](#guided-random-trade-simulation-4-agent-population)
below), which would just teach the model to agree with itself instead of with reality.

## ML retrain-watch loop

`python -m src.main retrain-loop` (auto-started in the dashboard) watches your logged outcomes
in the background and automatically re-runs `train-model` the moment 5+ new sold outcomes have
accumulated since the model was last trained -- so the calibration in
[ML-assisted ROI calibration](#ml-assisted-roi-calibration) above keeps itself current as you log
more trades, without you having to remember to retrain manually. It's deliberately **not** a
busy loop that keeps refitting the same unchanged data over and over (a fixed model refit on
identical rows always produces the identical result) -- it polls cheaply every 5 minutes and only
does real work when there's actually new data to learn from.

## Bulk price scan

A separate mode from every scan above: `python -m src.main scan-prices` (or the dashboard's "Bulk
price scan" panel) walks every skin/wear/StatTrak combo that could ever appear as a trade-up
input or output -- the tradeable subset, not the whole upstream skin catalog -- and just warms
`cache/price_cache.json` + `data/price_history.jsonl`. It does **no trade-up math at all**.
Reuses `steam_prices.py`'s existing adaptive rate limiter/retry logic unchanged, and is
resumable/checkpointed (`data/price_scan_checkpoint.json`) exactly like the deep search loop, so
pausing/stopping never loses progress. Running this ahead of time gives the guided random-trade
simulation below a head start (its own cache-miss fallback still fetches live, just one at a time
as candidates need it -- see that section), so a warm cache means fewer live fetches, not fewer
valid candidates. Don't run it at the same time as another scan -- they'd compete for the same
rate-limited requests.

## Guided random-trade simulation (search heuristic, not a market oracle)

`src/random_trade.py` generates totally random, structurally valid trade-up candidates: a random
rarity pair, a random primary collection, 0-9 random filler collections from *different*
collections (up to all 10 inputs each from a different collection is possible -- mixing
collections is real, group-based odds, confirmed against current community documentation of the
post-Oct-2025 rules: each collection's share of the 10 inputs is its total chance, split evenly
across its own outputs at the target rarity, see
[Manipulating odds with filler skins](#manipulating-odds-with-filler-skins)), and a random wear
for every individual input unit. The number of filler collections is drawn from a decaying
distribution (`FILLER_COUNT_DECAY = 0.55` per extra collection) -- mostly favoring fewer
collections, since mixing more dilutes odds across more possible outputs and real traders mostly
stack one collection, but the tail is never cut off, so heavily-diluted many-collection mixes are
still occasionally sampled and the ML gets to learn from that part of the space too.

Within each collection, the 10-input rule only requires the same rarity tier -- it never requires
the same skin -- so each collection "leg" can itself mix **multiple distinct skins**, not just one
skin at different wears (`SKIN_MIX_DECAY = 0.55`, same decaying-favoring-fewer shape). A single
candidate can therefore combine, say, 4 units of one skin, 3 of a second skin from the same
collection, and 3 units of a third skin from an entirely different collection, each at its own
random wear. Every skin/collection/wear comes straight from the same real skin database
`tradeup.py` uses, so a candidate can never be implausible -- there's no way to construct a float
outside a given skin's own min/max range.

`src/simulate.py` runs each candidate through the exact same formula-based math the rest of this
tool already trusts (`oddsmath.py`'s group odds and float remap, the same 13% Steam fee) --
pricing every input/output from the bulk-scanned cache first, falling back to a real live Steam
fetch (which then caches it, so later candidates needing the same skin/wear get a cache hit) on a
genuine miss -- and computes the full probability-weighted expected ROI across every possible
output, not a single random draw. This fallback is why a candidate is no longer thrown out just
because `scan-prices` hasn't warmed that exact skin/wear/StatTrak combo yet, at the cost of a
round sometimes blocking on real request latency instead of being instant.

### A multi-agent population: named agents, each with a distinct search personality

Rather than one search lineage, `python -m src.main simulate-population` (or the dashboard's
"Multi-agent simulation" panel) runs the **4 specialist agents** below -- plus the two
[Meta Agents](#meta-agents), **6 independent agents** in total -- in parallel (`src/sim_loop.py`),
each in its own `data/sons/son_N/` directory with its own logged simulations, its own trained
surrogate model (`src/sim_learning.py`, its own `sim_roi_model.joblib` -- literally multiple
separate machine-learning models, one per agent), and its own random-number generator. The
`son_N` directory naming is internal/historical -- it's never renamed to match an agent's
display name, so existing accumulated data/models are never orphaned by a labeling change.

Each agent is a named identity with a **purpose and 8 behavioral parameters** (`src/agents.py`),
not just a single randomness knob. The values below are each agent's **starting personality** --
five of the eight self-tune automatically as the agent runs (see
[Self-tuning agent preferences](#self-tuning-agent-preferences-adaptive-search) below):

| # | Agent | Purpose | Notable parameters |
|---|---|---|---|
| 0 | **Collection Mixer** | Finds profitable collection combinations -- different ratios, mixed collections, hidden ROI | high `collection_mixing_bias` (favors more filler collections), high `mutation_strength` |
| 1 | **ROI Accountant** | Finds mathematically strongest trades -- highest expected ROI, lowest unnecessary risk, avoids fake profits | `roi_weight`/`liquidity_importance` maxed, low `risk_tolerance`/`exploration_rate` |
| 2 | **Wear Efficiency Agent** | Optimizes wear categories -- cheap inputs, better output wear value, whether upgrading input wear is worth the cost | `wear_optimization_bias` maxed (skews random wear draws toward the bracket extremes instead of uniform) |
| 3 | **Discovery Agent** | Finds opportunities other agents miss -- unusual combinations, unpopular collections, high-risk/high-reward | `exploration_rate`/`mutation_strength`/`collection_mixing_bias` all maxed, low `liquidity_importance` |

Every parameter actually changes behavior, not just cosmetically:
- **`exploration_rate`** scales how large a candidate pool this agent casts each round before
  narrowing down to what actually gets simulated (the "adaptive search budget" -- see
  [Adaptive simulation, not Monte Carlo](#adaptive-simulation-not-monte-carlo) below for why this
  replaces literal trial-count scaling).
- **`memory_weight`** replaces the old fixed per-agent "exploration temperature": it controls how
  sharply this agent trusts its own trained model's ranking when choosing which of that pool to
  simulate (`random_trade.generate_batch`'s softmax-weighted sampling, weight ∝ `exp(score /
  temperature)`) -- high `memory_weight` means low temperature (exploit its own model), low
  `memory_weight` means high temperature (ignore the model, sample broadly).
- **`collection_mixing_bias`** overrides `random_trade.FILLER_COUNT_DECAY` per agent -- a higher
  bias flattens the decay curve, favoring more filler collections per candidate.
- **`wear_optimization_bias`** skews `random_trade`'s per-unit wear draws toward the bracket
  EXTREMES (Factory New / Battle-Scarred) instead of uniform-random, operationalizing "can cheap
  Battle-Scarred inputs create valuable outputs."
- **`risk_tolerance`**/**`liquidity_importance`** control how strict THIS agent is when deciding
  whether a simulated find is good enough for its own best-known list (`sim_loop._consider_best`)
  -- e.g. the ROI Accountant rejects any dead-skin-flagged or stale-priced find outright; the
  Discovery Agent is far more permissive.
- **`mutation_strength`** controls what share of a round's batch comes from mutating this agent's
  own current best finds (see [Mutation system](#mutation-system-improving-on-known-winners)
  below) instead of fresh random draws.
- **`roi_weight`** scales this agent's weight on raw ROI vs. the other 4 factors in the
  [weighted final ranking](#weighted-final-ranking) once its finds are merged with the other 3
  agents' -- an agent not built to chase pure ROI (Discovery Agent) has its high-ROI-but-shaky
  finds judged more on liquidity/confidence instead.

Running several agents side by side (via the dashboard's radar chart, comparing all 4 across
their 8 parameters at a glance -- the chart plots the LIVE self-tuned values, so it drifts in
real time) means one that plateaus in a local optimum doesn't stall the
whole search -- the others keep going, and an
[anti-convergence watcher](#anti-convergence-dont-let-every-agent-find-the-same-thing) nudges
agents that all settle on the same strategy for too long.

### Self-tuning agent preferences (adaptive search)

The five **search** parameters -- `exploration_rate`, `memory_weight`, `collection_mixing_bias`,
`wear_optimization_bias`, `mutation_strength` -- are live, per-agent values that **adjust
themselves automatically** (`src/adaptive.py`, persisted in each son's own checkpoint and wiped
by a Full reset like everything else there). The mechanism is a windowed **(1+1) evolution
strategy** -- the simplest self-tuning loop that tolerates this environment's noise and costs no
extra `simulate()` budget:

1. Run `ADAPT_WINDOW_ROUNDS` rounds with the current adopted parameters and measure a reward
   (the "baseline" window).
2. Gaussian-perturb the parameters and run the same number of rounds with the tweak (the
   "trial" window).
3. Adopt the tweak only if its reward beat the baseline by more than `ADOPT_EPSILON_PP`
   (never adopt on noise); either way, re-measure a fresh baseline next -- prices drift, so an
   old baseline is never trusted twice.

The reward is **ROI-first with an explicit exploration term**, so chasing profit never starves
out new territory:

```
reward = median expected-ROI (pp) of the window's volume-CONFIRMED valid sims
       + NOVELTY_REWARD_PP x (fraction of valid sims landing in under-explored collections)
       - INVALID_PENALTY_PP x (fraction of attempts that came back invalid/unpriceable)
```

Median (not mean) so one absurd thin-market outlier can't steer the policy, and
`low_volume_warning` rows are excluded from the ROI term entirely (the same filter
`sim_learning.train()` applies) -- an agent can never earn reward through prices too thin to
trust.

Three parameters deliberately do **not** self-tune, to prevent reward hacking rather than out of
caution: `roi_weight` is the [weighted final ranking](#weighted-final-ranking)'s trust weight for
this agent's finds -- letting an agent tune the weight its own results are judged by would let it
game its evaluation, not improve its search; `liquidity_importance`/`risk_tolerance` are
`_consider_best`'s data-QUALITY admission gates -- letting an agent loosen its own quality bar
would let it "improve" its reward by admitting the exact thin/fake-liquidity junk the ranking
exists to catch.

Guardrails: every adaptive parameter stays within `BAND_RADIUS` of that agent's base personality
(the four agents remain genuinely different searchers), `exploration_rate` can never drop below
`EXPLORATION_RATE_FLOOR`, `memory_weight` can never exceed `MEMORY_WEIGHT_CEIL`, and
`mutation_strength` can never exceed `MUTATION_STRENGTH_CEIL` -- so no agent can ever collapse
into pure exploitation, no matter how well it's currently paying.

That last ceiling was added after a measured collapse, and it is the one that had actually been
missing. `mutation_strength` is the share of each round spent re-mutating the agent's OWN
best-known list; left unbounded it drifted to 0.94-1.00 on four of the six agents, so
essentially the whole budget was variations of a best-50 list that had not changed in ~600
rounds. Because mutated candidates are blended in *after* selection has run, they also bypass
both the model's scoring and the novelty bonus described below -- which is why, on those agents,
tuning novelty did nothing measurable: novelty only ever reached the few percent of the batch
that wasn't a mutant. A tightened clamp is applied to already-adopted values too, not just newly
proposed ones (`adaptive._clamp_to_bounds`), so it takes effect on the next round rather than
waiting for a coin-flip trial to be adopted.

Separately from parameter adaptation, candidate **selection** carries a UCB-style novelty bonus:
each agent tracks how much of its simulated history has used each collection
(`checkpoint["coverage"]`, unit-share weighted), and a candidate's selection score becomes its
model-predicted ROI **plus** `exploration_rate x NOVELTY_UCB_PP x sqrt(ln(total) / (seen + 1))`
per leg. A collection the agent has never priced gets a real, shrinking-as-it's-explored bonus --
the "don't miss a powerful trade-up hiding in a collection the model has simply never seen"
guarantee. Before a model is trained at all, the novelty bonus alone scores the batch (with the
same softmax selection), so even the very first rounds spread across collections instead of
blindly taking the first `SIMULATE_PER_ROUND` random draws.

Each agent's card on the ML Lab page shows where it is in the measure/trial cycle, how many
tweaks it has kept vs. tried, its last window reward, and how many collections it has explored;
the exact constants (`sim_adapt_*`) are pulled live from the code via `/api/constants`.

### Trade-up rules engine

`src/rules_engine.py` centralizes what used to be a bare `10` literal duplicated across
`tradeup.py`'s cost/breakeven math and separately re-declared as `random_trade.N_INPUTS` (only
the 5-input Covert path was already a shared constant, `covert.COVERT_INPUT_COUNT`). It's now the
single source of truth for both `STANDARD_INPUT_COUNT`/`COVERT_INPUT_COUNT`, plus
`is_legal_rarity_step()` (wraps `data.RARITY_CHAIN`) and `validate_contract()` -- every random or
mutated candidate is checked against it before ever touching the price cache, so a multi-agent
search can never simulate (let alone auto-verify) something that isn't a real, legal trade-up.

### Mutation system: improving on known winners

Random generation alone only ever explores fresh, uniformly random candidates. `src/mutation.py`
adds the other half: given a candidate an agent already knows is good (its own best-known list),
it produces a small variation -- change the collection ratio (spec example: 7/3 → 5/5 → 4/5/1),
replace one or several input units' skins, change a unit's wear, retarget the whole contract
to a different adjacent rarity pair, or move a whole leg to a different collection -- and
`sim_loop.run_round()` simulates those alongside fresh
random draws, blended in a `mutation_strength`-controlled ratio per agent. Every mutation is
re-validated through the rules engine before use; a mutation only survives into an agent's
best-list if it actually beats what's already there (the existing best-list logic already only
keeps the top ROI entries, so no separate bookkeeping was needed for this).

The leg-move operator (`_mutate_replace_collection`) is what keeps this from being a trap. Every
other operator preserves the collections the parent was built from, and the one path that could
introduce a new one only fires on a *single-leg* candidate -- so a multi-leg parent was
mathematically confined to its own collections. Measured across the six agents' real best-lists,
400 mutants each, the old operator set reached **0 new collections** for four of them; with the
leg-move operator those same agents reach 34-44 (all 1,500 results still passing
`validate_contract`). Combined with `MUTATION_STRENGTH_CEIL` above, replaying the agents' real
checkpoints and models moves the worst-collapsed agent from 170 distinct input skins across 4
collections to 1,736 across all 91, with its 90th-percentile predicted ROI unchanged
(-62.5 → -63.1, against a model MAE of ~4.6pp).

### Dead-skin protection (scored, not a blacklist)

The old binary `low_volume_warning` (Steam volume < 5) is now a graduated 0-100 score
(`src/dead_skin.py`) built from every reliability signal already at hand: a REPORTED zero
recent-sales volume (40 points -- a real reported number, not a missing field, which is treated
leniently, same as before), a thin-but-nonzero volume (30 points, needs a second signal to
actually flag), a large gap vs. the cheapest verified Skinport/CSFloat price (20 points, only
checked once already-thin, to avoid the mandatory-verification mistake documented below), and
unstable recorded price history (10 points). `SimulationRecord.low_volume_warning` is now a
*derived view* of "score ≥ `dead_skin.DEAD_SKIN_THRESHOLD` (40)" -- same field name for backward
compatibility, graduated computation underneath.

Crucially, **this is temporary, never a permanent blacklist**: a separate, weekly-reviewed
persisted store (`data/dead_skin_scores.json`, piggybacked on the existing ML retrain-watch
loop's cheap polling cadence -- no new background thread) re-derives every tracked skin's score
from whatever's currently in the price cache (zero new live calls) and EMA-decays it toward 0 if
the skin's market activity looks healthy again. `python -m src.main dead-skins` (or the
dashboard's "Market intelligence" section) lists everything currently flagged.

### Invalid skins (no price on any source)

A *dead* skin (above) still has a price -- it's just thin or suspicious. An **invalid** skin is
one where `simulate.py` could get **no price at all**: the Steam cache missed, a live Steam fetch
came up empty, **and** the third-party fallback (Skinport/CSFloat, tried as an actual price
source here, not just corroboration) also had nothing. There's no honest way to value a trade-up
built out of such a skin, so it's recorded in `src/invalid_skins.py`
(`data/invalid_skins.json`), and `random_trade.py` **excludes it from candidate generation
entirely** -- the agents never propose, or waste a `simulate()` call on, a trade-up that contains
it. This also cuts the `incomplete_inputs` invalid rate at the source, rather than re-discovering
the same priceless skin every round.

Like dead-skin protection, this is **temporary and self-healing, never a permanent blacklist**:
each entry carries a timestamp and becomes re-checkable after `INVALID_RECHECK_SECONDS` (24h, to
match the price-cache TTL), and any later successful price clears the mark immediately (a relisted
skin, or a recovered Skinport/CSFloat outage, heals on its own). It's a single **shared** registry
across the whole population (a market fact, not per-son simulated data, so it doesn't cross any
son's data with another's). `python -m src.main invalid-skins` (or the dashboard's "Market
intelligence" section) lists everything currently registered.

### Automatic Stage-2 verification queue

The moment an agent's find crosses a real bar -- any **positive ROI** (> 0%), a fresh
(non-stale) cost basis, and no counted unit's dead-skin score above 40 -- it's queued
automatically (`src/verify_queue.py`) for the same live Skinport/Buff163/CSFloat cross-check
the dashboard's manual "Verify prices" button already does
(`price_report.verify_simulation_record`), no click needed. The check covers **every distinct
input skin+wear and every priced output** of the trade-up, not just the headline skin.
A single background consumer drains the queue **one item at a time** through the exact same
throttled/cooldown-respecting path (`thirdparty_prices.py`'s cooldown, Steam's adaptive rate
limiter) every other live request in this app already uses -- never a burst of parallel calls.

This is narrower than it sounds deliberately: an earlier attempt (documented below, see
[Third-party price verification](#third-party-price-verification-drought-detection)) to make
3rd-party verification *mandatory for every candidate* broke the feature in practice (round 50,
0 simulated / 2000 invalid) because Skinport doesn't list every cheap trade-up-fodder skin. This
queue only ever holds finds that already cleared the bar above *and* were admitted into an
agent's best list -- never the raw simulation firehose. Verified results
(`verified_roi_pct`, `price_confidence`, `verified_at`) are written back onto the checkpoint entry
-- same fields whether triggered automatically or by the manual button. The verified ROI is
computed from each skin's `consensus_price`: normally the cheapest of
Steam/Skinport/Buff163/CSFloat, but the **median of the sources that answered** whenever they
disagree by 20%+ with each other (see
[Consensus price](#cross-marketplace-price-report)) -- a badly-split market means the cheapest
single site is as likely the broken number as the priciest. Pausable from the
dashboard's "Automatic Stage-2 verification queue" panel (or `python -m src.main verify-queue`
for the CLI/foreground equivalent) if it ever needs to be turned off.

**Closing the loop: verification results stick, and stop repeating themselves (2026-07-11).**
Two gaps meant a "bugged" composition (one whose raw simulated ROI looks great only because a
Steam price is stale/anomalous) could get re-verified over and over, burning Skinport/Buff163/
CSFloat requests to re-confirm an answer this tool already had:

- `sim_loop._consider_best`'s per-signature dedupe used to keep whichever run of a composition
  scored the higher **raw** `expected_roi_pct` -- so a fresh, unverified re-simulation of the
  same bugged composition would silently overwrite (and discard) a verification that had already
  proven it unprofitable, the moment its still-inflated raw ROI looked better again. It now
  carries `verified_roi_pct`/`price_confidence`/`verified_at` forward onto the fresh entry
  instead of dropping them, and compares/sorts by the **verified-aware** effective ROI
  (`ranking.effective_roi`) rather than the raw estimate.
- New `src/verified_low_roi.py`: once a composition's verified ROI comes back at or below 0%,
  its signature (from/to rarity, StatTrak, input skin/wear/count multiset -- same identity
  `sim_blacklist.py` uses) is marked, self-healing after 24h like `mispriced_skins.py`. A marked
  composition is (1) hidden from every best/archive list (`sim_loop._entry_hidden`) and
  self-cleans out of a son's checkpoint next round, (2) skipped by the automatic verify queue
  (`verify_queue.is_eligible`) so it's never re-queued, and (3) excluded from
  `sim_learning.train()`'s training set so the surrogate model stops being taught the inflated
  number.

CSFloat's live per-skin listing lookup (unlike Skinport/Buff163, which are one bulk-mirror fetch
reused as a dict lookup) also had no cache at all -- every verification of a skin re-fetched
live even if the exact same skin+wear had just been checked seconds earlier for a different
candidate. `thirdparty_prices._get_csfloat_price` now caches successful responses (a real price
*or* a confirmed "no listings") for `CSFLOAT_TTL_SECONDS` (1h -- shorter than Steam's 24h cache
since these are live buy-now listings, not aggregated sale stats); a transient network/HTTP
failure is never cached, so a retry isn't poisoned, matching `steam_prices.get_price`'s own rule.

### Weighted final ranking

`sim_loop.list_best_simulations()` (the CLI's `best-simulations` / the dashboard's "Trade-up
opportunities" table) is ranked by a weighted composite score (`src/ranking.py`) instead of raw
ROI alone: 60% real ROI, 15% profit amount, 10% liquidity, 10% confidence, 5% discovery value (an
unusual-collection-combination bonus) -- "a smaller but reliable profit should beat fake high
ROI." ROI/profit are min-max normalized against the other candidates in the same ranking call
(they're unbounded); liquidity/confidence/discovery are already 0-100 scores. The 60% ROI weight
is itself scaled by the discovering agent's own `roi_weight` parameter before use (see the agent
table above), and a reliability multiplier (based on liquidity+confidence) dampens the final
score for badly-flagged entries -- this is directional ranking pressure away from fake-looking
ROI, not a hard veto, matching the spec's own percentage-weighted formula rather than an absolute
guarantee. The raw `expected_roi_pct` stays visible as its own column either way.

The confidence factor also folds in a small, capped bonus (`src/ranking.py`'s
`_ml_agreement_bonus`, up to 10 of the 100 points) for how closely the discovering agent's own
`sim_learning.py` model *anticipated* this exact candidate's ROI before it was even simulated
(`SimulationRecord.ml_predicted_roi_pct`, set by `sim_loop.run_round()` right after `simulate()`
returns, whenever that agent already had a trained model at draw time). Previously a trained
sim-population model only ever influenced *which* candidates got sampled in the first place
(`random_trade.py`'s guided search) -- it had no say at all in how much the final ranking trusts
a result once found. Close agreement between the model's pre-simulation guess and the exact
formula-computed ROI is treated as a real, independent trust signal; a large residual doesn't
mean the entry itself is wrong (the exact math is always authoritative) -- just that this
particular find is the kind of thing the model doesn't understand well yet. Entries from before
this field existed, or drawn while an agent had no trained model yet, get no bonus and no
penalty either way.

### Automatic de-duplication: one row per unique trade-up

An agent re-simulates the exact same skin/wear/count combination over and over (each run gets a
fresh `id` from `simulate._new_id`), and several agents independently land on the same good
combination, so without de-duplication the "Trade-up opportunities" table fills up with the same
trade-up placed many times over. It's collapsed automatically by content *signature* (from/to
rarity + StatTrak + the multiset of input units) in two places: `sim_loop._consider_best` keeps
only one entry per signature in each son's best list (replacing the stored copy only when a later
run scores a higher ROI, so the best-priced instance wins), and `sim_loop.list_best_simulations`
collapses across sons after ranking, keeping only the single best-ranked instance of each
signature. Net effect: each unique composition appears exactly once, never repeated -- no user
action needed. (The manual blacklist below is a separate, stronger action: it *removes* a
composition entirely rather than just collapsing its copies.)

### Blacklisting a trade-up you've already done

The agents keep re-discovering the same profitable composition round after round, so one good
trade-up shows up in the "Trade-up opportunities" table (now de-duplicated to a single row per
the section above). Once you've actually executed one by hand (this
tool never auto-buys/sells), you don't want to keep seeing it. Open the opportunity's detail row
and hit **"Mark done / blacklist"**: from then on it's hidden from the table and no son will
re-admit it to its best-known list.

Matching is by the trade-up's *content signature* -- from/to rarity, StatTrak flag, and the
multiset of input units (skin, wear, count) -- not the per-simulation `id`, because
`simulate.py` mints a fresh time-based id on every run, so the "same" contract never keeps a
stable id. Because the key is the composition, blacklisting one copy also clears every duplicate
of it across all four sons at once. The list lives in a single shared `data/sim_blacklist.json`
(`src/sim_blacklist.py`) -- it's a *user* preference ("I already did this"), not simulated data
or a trained model, so unlike everything else in the population it is deliberately shared across
sons rather than kept per-son (see `sim_blacklist.py`'s docstring). Wired into
`sim_loop._consider_best` (never re-admits a blacklisted composition, and self-cleans any copy a
son banked before you blacklisted it) and `list_best_simulations` (hides it immediately). The
dashboard's **"Blacklisted trade-ups"** panel lists everything you've marked done with a
**Restore** button to un-blacklist. Endpoints: GET/POST `/api/sim-population/blacklist`, POST
`/api/sim-population/blacklist/remove`.

### Anti-convergence: don't let every agent find the same thing

A lightweight watcher (`sim_loop.ConvergenceWatcher`, checked every 60s) fingerprints each
agent's current #1 best-known strategy (primary collection + rarity pair + rounded input ratio --
not the exact skin/wear mix, which mutation.py varies constantly even within one agent's own
repeated visits to the same idea). If 3+ agents share the same fingerprint for 3 consecutive
checks, every agent in that group gets automatically `nudge()`-d -- the same soft-retry
temperature/exploration boost a user can already trigger by hand, just triggered automatically.
Surfaced in the dashboard as a banner right above the agent cards when it fires.

### Adaptive simulation, not Monte Carlo

Some multi-agent optimization designs call for scaling how many trials a candidate gets simulated
(more trials for a promising one, fewer for an unknown one) to build confidence against sampling
noise. That doesn't apply here: this tool computes the EXACT probability-weighted expected value
analytically (`oddsmath.group_odds()`'s exact per-output probabilities × each output's price,
summed) rather than sampling a stochastic outcome -- there's no noise to average out, so nothing
to gain by re-simulating the same candidate more times. What DOES scale adaptively instead is
each agent's *search budget*: `exploration_rate` scales how large a pool of fresh candidates gets
drawn and scored per round (see the agent table above) -- more effort spent searching, not more
trials re-confirming a number that's already exact.

**Progress tracking, so you can actually tell if it's working.** Each son's checkpoint keeps a
`history` of (round, best-ROI-found-so-far) snapshots, rendered as a live line-chart sparkline
per son in the dashboard, plus `rounds_since_improvement` -- a son is flagged **STUCK** once 15
rounds pass without its best ROI improving by more than 1 percentage point. The dashboard shows,
per son: status, round count, trials simulated, best ROI ever found, its own model's training
status/MAE, and the stuck badge -- everything needed to tell "still climbing" from "flatlined"
at a glance.

**Two ways to retry a stuck son**, both scoped to that one son only (via
`python -m src.main nudge-son --son N` / `reset-son --son N`, or the dashboard's per-son
buttons):
- **Nudge (soft)** -- reseeds that son's RNG and temporarily multiplies its temperature ×3 for
  the next 5 rounds before decaying back, pushing it to explore more broadly. Its logged data
  and trained model are left completely untouched.
- **Full reset (hard)** -- wipes that son's logged simulations, trained model, and checkpoint
  entirely, and starts that one lineage over from scratch. The dashboard confirms before doing
  this since it's destructive; other sons are never affected either way.

`python -m src.main best-simulations` (or the dashboard's combined table) merges the best
simulated trades found across all 4 sons, tagged by which son found each one.
`python -m src.main population-status` prints a one-line-per-son summary for scripting/quick
checks without opening the dashboard.

### Per-skin detail, and protecting the ML from anomalous prices

Each row in the dashboard's "Best simulated trades" table expands (click the row) into: every
distinct input skin actually used (image, wear, count, unit price -- summing to the full 10
inputs, not just "the cheapest one"), and every possible output (image, wear, drop probability,
price, net profit) -- the same `renderDrops()` table style used elsewhere in the dashboard.

Three mechanisms protect the ML from learning off an unreliable Steam price:
- **Automatic, free, applied to every single background trial.** Steam's own recent-sales
  `volume` for every priced input/output is captured for free (it's already returned by every
  price lookup, just previously discarded) and compared against
  `price_report.LOW_VOLUME_THRESHOLD` (5, the same convention `anomalies.py`'s `--min-volume`
  and the cross-marketplace price report already use). Any simulated trial resting on a
  thinly-traded price -- Steam's own "someone lists it high but nobody's actually buying" case --
  is flagged `low_volume_warning` and **excluded from that son's training set and its
  best-known leaderboard** (still preserved in `simulations.jsonl`, just never curated/trained
  on).
- **Automatic, but only when Steam's own price already looks thin.** Once an input/output is
  flagged `low_volume_warning`, `simulate.py` also tries Skinport (whole catalog bulk-fetched and
  cached for 5 minutes, so this is a cheap dict lookup after the first fetch) and CSFloat (only
  if `CSFLOAT_API_KEY` is configured) for corroboration, recorded as `skinport_price`/
  `csfloat_price` on that entry. **This used to be a hard requirement on every single
  input/output regardless of volume, which in practice invalidated nearly every candidate** --
  Skinport simply doesn't list every skin/wear/StatTrak combination that exists on Steam
  (especially cheap, common trade-up fodder), so requiring it unconditionally made the feature
  produce almost nothing. Missing third-party corroboration no longer disqualifies anything by
  itself -- it's now purely extra visibility on a price that was already flagged as
  low-confidence on Steam's own terms. (If Skinport itself is unreachable, a 60-second failure
  cooldown, `SKINPORT_FAILURE_COOLDOWN_SECONDS`, stops repeated checks from re-attempting a live
  request on every call.)
- **On-demand, opt-in, for a closer look at a row you actually open.** A "Verify prices vs
  Steam/Skinport/CSFloat" button in the expanded detail cross-checks every skin in that specific
  row (regardless of volume) and renders the full comparison (`price_confidence`: `ok` / `low
  volume` / `no 3rd-party match` / both, gap %, cheapest source) -- the deliberate, thorough
  check for a trade you're actually considering acting on.

**A candidate is only ever simulated if it's actually complete.** A trade-up needs ALL of its
inputs (10, or 5 for a Covert->knife/glove path) -- there's no such thing as a partial one, so
`simulate.py` requires a cached Steam price for every single input unit, and at least one usable
output price, before it will produce a real result. Anything short of that becomes an
`InvalidSimulation` instead -- logged separately (`data/sons/son_N/invalid_simulations.jsonl`,
tagged `incomplete_inputs` or `no_output_price`) and counted in the dashboard/`population-status`
as "N invalid (missing price data)," but never treated as a candidate, never trained on, never
ranked. Expect a high invalid count until `scan-prices` has warmed the cache broadly -- a
uniformly-random candidate can easily touch a skin/wear nobody's priced yet.

**Read this before trusting a number here:**
- **This is a theoretical estimate, not a real trade.** Nothing is ever bought or sold; ROI
  assumes you could transact instantly at the cached price, ignoring slippage and other buyers
  taking the listing first (`cost_basis_source` is `cache` if every input price was fresh, or
  `stale` if >=1 had expired past the 24-hour TTL but was used anyway -- either way every input
  was actually priced, see above).
- **The trade-up math itself is deterministic, not something the ML "discovers."** Output
  float/probability is pure arithmetic given the inputs -- what the model actually learns is
  which *regions of the random-candidate search space* tend to score well, a search heuristic,
  not a claim about real market behavior.
- **None of these son models are ever merged with the real ROI-calibration model above, or with
  each other.** `learning.py` trains only on `data/outcomes.jsonl` (trades you actually
  executed); each son's `sim_learning.py` model trains only on that SAME son's own
  `data/sons/son_N/simulations.jsonl` (simulated trials). Mixing any of these would let the
  tool's own guesses masquerade as ground truth, or let one son's exploration bias another's --
  the same reasoning [ML-assisted ROI calibration](#ml-assisted-roi-calibration) and the deep
  search loop already document for their own data.
- **Pure random sampling mostly finds losers**, especially before a son's first 20 simulated
  trials exist to train its own model on -- expect a lot of near-zero/negative-ROI noise early
  on, particularly from the high-temperature (explorative) sons; that's normal, not a bug.
- **`rounds_since_improvement`/STUCK is a heuristic, not a guarantee.** Cached prices are noisy
  (stale/thin listings), so a son's best-ROI can occasionally jump on a lucky noisy draw rather
  than genuine convergence -- treat the stuck badge as "worth a look," not certainty.
- **Third-party corroboration only ever runs for already-flagged low-volume prices, and only
  covers what Skinport (and optionally CSFloat) happen to have listed right now.** It's a
  visibility aid on top of the `low_volume_warning` flag, not an independent guarantee -- a
  low-volume Steam price with no Skinport listing is still just flagged `low_volume_warning`
  (and still excluded from training/the leaderboard), exactly as if the corroboration check
  didn't exist. It used to be a hard requirement on every input/output regardless of volume;
  that made the feature invalidate nearly every candidate in practice (Skinport doesn't carry
  every skin/wear/StatTrak combination, especially cheap common trade-up fodder) and was walked
  back for that reason.

## Meta Agents

`agents.py` defines **6** agents: the 4 hand-written specialists above plus two more named
agents, **Meta Alpha** and **Meta Beta**, each with its own fixed starting personality:

| # | Agent | Priorities |
|---|---|---|
| 4 | **Meta Alpha** | ROI, confidence, verification, liquidity |
| 5 | **Meta Beta** | Novelty, discovery, rare collections, mutation diversity |

Each learns independently exactly like a specialist -- own `son_N` directory, own model, own
adaptive self-tuning ([Self-tuning agent preferences](#self-tuning-agent-preferences-adaptive-search)) --
fully storage-isolated, same as sons 0-3.

## Removed features

**Cross-agent evolution layer (2026-07, removed 2026-07-12).** A prior Stage 4 build added a
cooperative layer on top of the population -- a cross-agent elite population, tournament
parent-sharing between sons' mutation pools, species-based adaptive mutation pressure, a Hall
of Fame, genome lineage logging, periodic behavioral crossover breeding Meta Alpha/Beta's base
personalities from the other specialists, and population-based training of each son's search
hyperparameters. Removed after a PM review found no evidence it was working: 219 forced
"Exploration Recovery Mode" perturbations fired across the population in 3 days of continuous
background compute (see below) while the genome lineage log accumulated only 4 entries total.
The base per-son random search, self-tuning ([adaptive.py](#self-tuning-agent-preferences-adaptive-search)),
and ranking are unaffected and were kept -- along with every son's already-accumulated
simulations/models, none of which were touched by the removal. Meta Alpha/Beta keep their
original seed personalities as fixed values instead of periodically-bred ones.

**Exploration Recovery Mode** (removed with the evolution layer above, same reasoning). When a
son stayed stuck well past a nudge's chance to fix it, this extended the stuck-detection/nudge
machinery with progressive escalation (levels 1-4: mutation/exploration/novelty pressure up,
exploitation down) and, if still stuck at max level, controlled random perturbations of the
son's adopted adaptive parameters. The 219-perturbations-in-3-days figure that motivated this
whole removal came from this mechanism specifically.

## Market Reliability Engine

`src/market_reliability.py` unifies what used to be three separate signals each caller had to
combine by hand -- dead-skin score, mispriced-skin registry, cross-source consensus/spread --
into ONE auditable state per skin+wear: **healthy / suspicious / excluded**. On top of the
existing signals it adds a **rolling variance / rolling z-score** over the recorded price
history (window of 10 snapshots): a snapshot standing 3+ standard deviations from the skin's
own recent neighborhood -- the fingerprint of temporary Steam inflation or a manipulated
listing -- forces Excluded on its own ("abnormal cross-market deviation" detection). Per-skin
cross-marketplace metrics (consensus price, market confidence, source agreement, liquidity
score) come from the same bulk-feed math the price report uses.

Excluded skins are excluded from **ML training** (weight 0 -- see below), **ranking reward**
(hard dampener in `ranking.py`), and **best-list admission** (`sim_loop._consider_best`).
Exclusion is **never permanent**:
every state is re-derived fresh from current signals on each check, a periodic recheck
(piggybacked on the retrain loop's poll, far tighter than the spec's 24h minimum) re-evaluates
everything tracked and logs every state TRANSITION to `data/market_reliability_log.jsonl`, and
pricing that normalizes restores the skin automatically. **Manual override** is always
available (dashboard Market intelligence panel, or
`python -m src.main override-reliability --skin "..." --state excluded|suspicious|healthy`)
and always wins until cleared. CLI: `python -m src.main market-reliability`.

## Reliability-weighted training

Training samples no longer contribute equally (`market_reliability.training_weight`, applied
as sklearn `sample_weight` in BOTH `learning.py`'s real-outcome model and every son's
`sim_learning.py` model, through cross-validation and the final fit alike):

| Sample's skins verify as | Weight |
|---|---|
| Fully verified across marketplaces | 1.0 |
| Minor disagreement / suspicious | 0.75 |
| Steam-only (no cross-source confirmation) | 0.35 |
| Known anomaly (any Excluded skin) | 0.0 |

Questionable rows lose influence instead of being deleted -- historical information is
preserved, the learning process just stops trusting it blindly.

## Rare Pattern Arbitrage Engine

A subsystem **independent of the trade-up optimizer** with its own dashboard page ("Rare
Patterns"): find listings where a seller priced a **documented collector pattern** at ordinary
market price.

- **Pattern Knowledge Database** (`src/pattern_db.py`, live copy in `data/pattern_db.json`):
  the centralized, versioned store -- Case Hardened Blue Gem tiers, Desert Eagle Heat Treated
  seeds, Doppler/Gamma Doppler phases (Ruby/Sapphire/Emerald/Black Pearl by paint index),
  Marble Fade Fire & Ice, Crimson Web centered webs, Karambit #387/#601 class patterns,
  Katowice 2014 / Crown Foil crafts, ... Every entry stores weapon/skin/type/seeds/tier/
  premium multiplier/confidence/float requirements/source link. Nothing is hardcoded anywhere
  else in the app. Every change (seed merges on refresh, manual edits) bumps the version and
  lands in `data/pattern_db_history.jsonl`.
- **Marketplace scanner** (`src/pattern_scanner.py`): cycles the knowledge base's targets one
  polite query at a time against **Steam Market listings -- the only listing source** (a prior
  CSFloat listings-API source was removed 2026-07-12, see [Removed features](#removed-features);
  third-party sources are cross-verification only project-wide, never primary discovery). The
  classic listings `render` endpoint (kept reachable behind the new Market Beta via its own
  opt-out cookie, `bMarketOptOut=1`) returns each live listing's price and inspect link; the
  link is resolved to float/paint seed/paint index/stickers through CSFloat's public INSPECT
  endpoint (api.csfloat.com -- a separate, optional-key endpoint from the removed listings
  API; a key only widens its rate budget, one throttled call per listing, cached forever since
  an item's float/seed never change). This is also the only way to buy a matched pattern
  directly on Steam. **Honest caveat**: Steam rate-limits this endpoint aggressively for
  anonymous callers (the same policy that killed `priceoverview`) -- a 429/HTML answer puts
  the scanner on its own exponentially-growing cooldown (5 min -> 1h), visible in the status
  panel rather than hidden, and a scan can spend real wall-clock time sitting out cooldowns
  instead of finding listings. An optional `STEAM_LOGIN_SECURE_COOKIE` env var (your own Steam
  session's cookie value, never entered through the dashboard) gets Steam's much friendlier
  authenticated rate limit instead. The Skinport/Buff163 mirrors remain price-only -- they
  feed the VALUATION side, not pattern scanning.
- **Opportunity detection**: estimated collector value = the skin's base price x the entry's
  conservative documented premium multiplier. Every listing that matches a documented pattern
  becomes an opportunity row -- **profitable or not** (2026-07-11: dropped the old hardcoded
  "30%+ below estimate" hard filter so nothing is hidden; sort/filter by expected profit, price,
  confidence, etc. instead to find the worthwhile ones). Ranking still uses a transparent
  scoring formula over profit/tier/confidence/float/liquidity -- deliberately NOT a model. The
  base price is **Steam-first** (2026-07): the skin's own
  Steam median-sale price when it exists, falling back to the cross-market consensus only
  when Steam has no sale evidence -- the mirror's skinport/buff163 dumps conflate
  souvenir-only items under plain names and can carry listings far above anything Steam pays
  out, so anchoring collector value to them would manufacture fake edge. Per the spec's
  cross-verification rule, a base price fewer than 2 sources confirm -- or one that isn't
  Steam's own sale price -- demotes the opportunity's confidence automatically.
- **ML constraint, per spec**: no model anywhere in the match path -- pattern classification
  is exclusively documented public knowledge; ML's only permitted role is prioritization.
- **Dedicated page**: filter by weapon/pattern type/marketplace/min profit/min confidence/
  float range/tier, sort by best opportunity/profit/price/premium/confidence/newest; each row shows
  float, seed, ask vs. estimated collector value, expected profit, confidence, direct listing
  link, the WHY (explanation tooltip) and the documenting source link. Detected opportunities
  also appear on the Results page, clearly tagged **Collector Arbitrage** so they're never
  mistaken for trade-ups.

CLI: `python -m src.main pattern-opportunities` (`--refresh` re-verifies knowledge).
Endpoints: GET `/api/patterns/status|opportunities|db|db/history`, POST
`/api/patterns/scan/start|scan/pause|refresh|db/update-entry`.

**Documented limitation** (surfaced in the UI too, not hidden): automatic knowledge
re-verification re-validates against the seed knowledge shipped with the code and versions
every change; it does NOT live-scrape CSBlueGem/FloatDB -- neither offers a stable public API,
and silent scraping would break without notice. Every entry links its source for one-click
manual verification instead.

## Automated GitHub backup engine

`src/backup_engine.py`: an asynchronous background worker (own daemon thread, never blocks
anything) that wakes **every 6 hours**, SHA-256-hashes the mutable telemetry files
(`data/outcomes.jsonl`, `data/price_history.jsonl`, `data/dead_skin_scores.json`) and compares
them to the last successful backup. No delta -> logs `No telemetry mutations detected.
Skipping remote push.` and sleeps -- idle cost is three file hashes per 6h. A real delta ->
wraps `data/` via the existing export-data pipeline into
`archive_YYYYMMDD_HHMMSS_[num_skins]skins_[num_outcomes]outcomes.zip` and commits+pushes it to
the configured git repository.

Configuration is explicit, never guessed (`data/backup_config.json` / the dashboard's Backup
panel): `repo_dir` must point at a local clone of your designated backup GitHub repo -- this
project directory itself is not a git repository, so there is nothing safe to auto-discover.
Until configured, the worker idles in a clearly-reported "not configured" state. A failed push
still commits locally (nothing is ever lost) and retries next cycle. Full audit log in
`data/backup_log.jsonl`. CLI: `python -m src.main backup` / `backup --run-now`. Endpoints:
GET `/api/backup/status`, POST `/api/backup/config`, POST `/api/backup/run-now`.

## Training across multiple machines

Everything the ML/learning side of this tool accumulates -- logged outcomes, recorded price
history, the search loop's checkpoint, and the trained model itself -- lives under `data/`
(never `cache/`, which is disposable and machine-specific). Two ways to keep that continuous
across more than one PC:

**Git (recommended for ongoing use).** `data/outcomes.jsonl`, `data/price_history.jsonl`, and
`data/search_checkpoint.json` are all plain JSON Lines/JSON text -- diff- and merge-friendly,
exactly what git is good at. `.gitignore` in this project deliberately excludes `cache/` but
**not** `data/`. Initialize a repo here (`git init`), commit `data/` along with the source, push
it somewhere, and `git pull` on your other machine(s) before scanning/training there. The one
binary file, `data/model/roi_model.joblib`, doesn't diff/merge meaningfully -- if two machines
each retrain independently you'll get a merge conflict on that one file; just keep whichever and
re-run `train-model` (it's a fast, fully-reproducible rebuild from `outcomes.jsonl`, never
something you need to preserve byte-for-byte).

**The oversized logs travel as split archives.** Two logs grow far past GitHub's hard
100 MB per-file limit (`data/market_reliability_log.jsonl` adds ~200 MB/day; each son's
`data/sons/son_N/simulations.jsonl` is append-only forever), so they're `.gitignore`d and
carried as small dated gzip chunks under `data/archive/` instead:

```
# Before committing (incremental -- only archives lines added since the last run,
# never modifies the live logs, safe while the sim loop is running):
python -m src.main split-data

# What's archived so far (chunks, records, date range, compressed size):
python -m src.main split-data --status

# On a fresh clone, rebuild the live logs from the chunks (only ever writes a
# missing/empty live file -- it will never overwrite accumulated data):
python -m src.main split-data --restore
```

Each `data/archive/<log>/manifest.json` records per chunk: the date, record count, unique
skin count, first/last record time, and raw/compressed sizes. Chunks are immutable once
written -- a re-run only appends new ones -- which keeps day-to-day git pushes small.

**Archive export/import (for a one-off transfer, or if you'd rather not git-track `data/`).**
```
# On the machine with the data:
python -m src.main export-data
# -> writes data-archive-<timestamp>.zip in the project root (or the dashboard's
#    "Export data archive" button in the Background learning section downloads the same thing)

# On the other machine, after copying that .zip over:
python -m src.main import-data --file data-archive-20260101-120000.zip
```
`import-data` **merges by default** -- it's designed for "I scanned/logged independently on two
machines, now I want them combined," not for clobbering one machine's real data with another's:
outcomes are unioned by their unique id (nothing lost, nothing duplicated), price-history points
are unioned by (timestamp, skin, currency), and the search checkpoint keeps whichever machine
found the *better* ROI for each collection/rarity/StatTrak combo -- never regresses. The trained
model itself is left untouched on merge (two trained models can't be meaningfully combined) --
run `train-model` afterward to fold newly-merged outcomes in. Pass `--replace` instead if you
genuinely want to wipe local data and take the archive's contents wholesale (e.g. a brand new
machine with nothing accumulated yet) -- this one **is** destructive, so it's opt-in only.

## Dashboard extras: explanations, charts, and images

The dashboard is meant to be self-explanatory without reading the source, so it now includes:

- **"How this works" section** (collapsible, collapsed by default at the top of the page) --
  explains the trade-up math, float manipulation, drought detection, the ML model, and price
  prediction in plain language, with a short glossary. Every number quoted in it (the Steam
  fee %, wear-bracket ranges, ML sample-size thresholds, price-history minimums) is pulled live
  from `GET /api/constants`, which reads the real values
  out of the modules that define them (`tradeup.STEAM_SELL_FEE`, `oddsmath.WEAR_BREAKPOINTS`,
  `learning.MIN_TRAINING_SAMPLES`, etc.) rather than being retyped into the HTML -- so the
  explanation can't silently drift out of sync with the code.
- **Skin thumbnails** -- every skin name in every table now shows a small image, hotlinked
  directly from Steam's own CDN (the URL is already present in the upstream ByMykel/CSGO-API
  feed, `cache/skins.json`'s `image` field -- no new data source, no local image hosting).
  Broken/unreachable images fail silently (hidden via `onerror`) rather than showing a broken
  icon.
- **Charts** (via [Chart.js](https://www.chartjs.org/), loaded from a CDN -- the one JS
  dependency this project uses, everywhere else is vanilla JS): an ROI% distribution histogram
  over the current scan results, a horizontal bar of the ML model's permutation feature
  importances, a cumulative profit/loss line over your logged outcomes, the wear-bracket range
  visual in the methodology section, and the price-history trend chart (below).
- **"Learning Machine -- top picks" section** -- a dedicated, visually distinct panel (near the
  top of the page) showing the current scan's top 10 results by `ml_predicted_roi_pct`, with a
  computed "ML edge" (how much higher/lower the ML model rates a trade-up vs. the formula) and
  a confidence badge. Empty until you've logged enough outcomes, trained a model, and turned on
  "Use ML calibration" for a scan -- the empty state explains exactly what's missing rather than
  just showing nothing.

## Skin price history & prediction

Steam's public API has **no history endpoint** -- `priceoverview` only ever returns the current
lowest/median price. `src/price_history.py` builds a history itself: every genuinely fresh
(non-cache-hit) Steam price lookup anywhere in this tool -- a scan, an anomaly check, a
float-mix search -- appends one snapshot to `data/price_history.jsonl`
(`{ts, market_hash_name, currency, lowest_price, median_price, volume}`). This is **not
retroactive**: a skin/wear you've never looked up has no history, and the tool says so plainly.

Once **5+ points** are recorded for a given skin/wear/currency, a plain
`sklearn.linear_model.LinearRegression` is fit on (timestamp, price) and extrapolated 24 hours
ahead -- reported as `{predicted_price, slope_per_day, r_squared, n_points}`. This is a **naive
baseline**, explicitly framed that way in both the CLI output and the dashboard: it assumes a
straight-line trend continues, which real skin prices frequently don't. Treat it as a
coursework-level demonstration of trend extrapolation, not a real forecasting model or
financial advice. Below 5 points, no prediction is shown at all -- same "refuse rather than
fabricate" gate as the ML ROI model's `MIN_TRAINING_SAMPLES`.

CLI: `python -m src.main price-history --skin "AK-47 | Redline" --wear "Field-Tested"` --
triggers one live lookup (planting the first data point if none exists yet), prints every
recorded snapshot, and prints the prediction (or the "not enough data" message).

Dashboard: the "Skin price history & prediction" section has an autocomplete search box
(backed by `GET /api/skins?q=...`, matching against the full skin/knife/glove roster) and
renders the recorded points plus the predicted point (dashed line) on a chart, exactly as the
CLI describes them in text.

## Important limitations (read before trusting the numbers)

- **Float/wear is approximated** in the default scan (not using float-mix
  strategy above): since all 10 inputs are the same cheapest skin+wear combo,
  their floats are all in the same bracket, so the output is assumed to land in
  that *same* wear bracket too (e.g. buying 10 Battle-Scarred inputs prices the
  output at its Battle-Scarred price) rather than blending across every possible
  wear. If the output skin doesn't have that exact wear available, it falls back
  to the closest condition that does. Still an approximation (not an exact
  float), but it's now tied to the wear you're actually likely to get instead of
  an abstract average -- treat results as a shortlist to verify manually, not
  exact numbers.
- **Steam Market fee** is approximated at 13% (real range is ~13-15%, it varies).
- **Prices move.** By the time you manually execute, the cheapest listing may be
  gone. This is a screening tool, not a guarantee.
- **Trade holds**: items bought on the Steam Market have a trade cooldown before
  they can be re-traded/used again -- factor that into how fast you can cycle capital.

## Setup

Requires Python 3.10+. No third-party packages needed for the core scanner.

```
cd cs2-trade-calculator
python -m src.main update-data      # one-time: downloads the skin/collection database
```

Only needed if you want [ML-assisted ROI calibration](#ml-assisted-roi-calibration)
(`--use-ml` / `train-model` / `retrain-loop`) or the price-trend part of
[skin price history & prediction](#skin-price-history--prediction) (`price-history` still works
without it -- it just shows recorded points with no prediction) -- every other feature,
including outcome logging, third-party price verification, charts, and images, still needs
nothing beyond the standard library (Chart.js loads from a CDN in the browser, not via pip):

```
pip install -r requirements.txt
```

## Web dashboard (auto-refreshing)

```
python web/server.py
```

Then open http://127.0.0.1:5000. It's the same scanner as the CLI, wrapped in a
local dashboard with live-updating tables -- no need to re-run commands by hand.
Still fully read-only: no login, no orders, nothing auto-buys.

**No automatic rescanning**: a scan runs once when you trigger it (Scan now,
Continue, or a config change) and then the dashboard just sits on the results
until you ask for another pass. Earlier versions re-ran on a timer automatically;
that's been removed since a background poll no one asked for accomplishes
nothing that a manual "Scan now" doesn't, and it made "is this stuck or just
between rounds" harder to tell. **This now also genuinely includes server startup**: a
previous version of `web/server.py` started the very first scan automatically the instant the
process booted (a real bug -- it silently kicked off a full ~90-collection scan every time you
launched the dashboard, contradicting this exact claim). Fixed: opening the dashboard now always
starts at `status: "idle"` with zero requests made until you click something.

Notes:
- **Narrow the collection filter before your first scan.** With it blank, the
  dashboard scans all ~90 collections, which can take a long time end-to-end
  because Steam's price endpoint is throttled to ~1 request/1.5s. Changing the
  filter and clicking "Apply & rescan" interrupts an in-progress scan almost
  immediately and restarts with the new scope.
- Price lookups are cached for 24 hours (same cache the CLI uses, and the same one
  simulate.py's guided-simulation loop reads from), so once a scope has been scanned once,
  repeat passes within that window are near-instant. Entries older than 24h are purged outright
  (not just treated as stale) the next time the cache is loaded or written, so a skin nobody's
  asked about in a day doesn't linger forever -- the next lookup for it is a real fresh fetch.
- The "Scan now" button starts a fresh pass.
- **Scan Covert -> knife/glove trade-ups**: opt-in checkbox, off by default since
  it's a separate scan pass over all 42 real weapon cases (or fewer, with the
  "Case filter" text box narrowing scope the same way the collection filter
  does for regular trade-ups). Runs after the regular trade-up and
  underpriced-listing passes finish.
- **Pause / Continue**: "Pause" stops the current pass but remembers exactly which
  collection it was on -- clicking "Continue" picks back up there instead of
  restarting from the first collection. Useful for a scan that's going to take a
  while and you want to step away without losing progress. Changing scope/settings
  or clicking "Scan now" always starts fresh, though, since a paused position only
  makes sense for the scope it was paused under.
- The "Stop" button halts the current pass and discards the resume point entirely
  (unlike Pause) -- it just goes idle and stays there until you click "Scan now" again.
- **Show only underpriced listings**: a display-only checkbox that hides the
  trade-up table so you can focus on just the underpriced-listing flips. Doesn't
  change what's being scanned, just what's shown.
- **Export CSV**: each table has an "Export CSV" button that downloads exactly
  what's currently on screen (respecting your current sort order), ready to open
  in Excel/Google Sheets/etc.
- **Max buy price** column: the breakeven price -- pay more than this per input
  unit (trade-ups) or per listing (underpriced listings) and the deal stops being
  profitable. Useful because the "cost" figures elsewhere assume you actually get
  the single cheapest listing, which isn't guaranteed once you're buying multiple
  units or the listing sells before you do.
- **Currency**: pick any of ~25 Steam-supported currencies from the dropdown.
  Switching currency means every price has to be re-fetched from Steam in that
  currency (nothing is shared with the USD cache), so expect a slow first pass
  after switching, same as narrowing/widening the collection filter.
- If Steam starts returning rate-limit errors (happens if you scan very large
  scopes repeatedly in a short time), the scanner backs off automatically and
  retries later rather than caching a false "no price data" result. The
  per-request delay is now **adaptive**: it starts at ~1.5s, widens (up to 8s)
  each time a 429 comes back, and only narrows back down toward 1.5s after a
  run of successes -- so a scan that's actually tripping the limit slows
  itself down harder instead of continuing to hammer at the same pace and
  getting rate-limited over and over. Each individual 429's cooldown also
  grows exponentially with consecutive 429s (capped at 5 minutes, with a
  little random jitter so many collections' requests don't resync into
  lockstep), and honors Steam's own `Retry-After` header when it sends one
  instead of guessing. The dashboard's network health panel shows the current
  adaptive spacing ("Request spacing: N.Ns") live.
- **Network health panel**: shows live counts of successful/rate-limited/errored
  requests and when the last one succeeded, so you can tell "still working,
  just slow" apart from "actually stuck" -- a running "ok" count means it's
  making progress even if results aren't appearing yet; a stuck "rate-limited"
  count with a countdown means Steam is actively throttling (temporary, not a
  ban -- see the note on IP exposure and policy further down); a climbing
  "errors" count with no "ok" ever happening on a machine with multiple Python
  installs may mean the one running this has an outdated/misconfigured SSL
  trust store (see below) rather than anything Steam-side.
- **VPN switch warning**: if Steam refuses *every* request for 5 straight
  minutes, a red banner suggests switching your VPN server/location (e.g. a
  different city/country in Avast SecureLine) to pick up a fresh IP, since
  Steam's throttling is IP-based and a persistent block on one IP usually
  clears immediately on another. This is a suggestion, not automatic --
  actually switching your VPN's connection is a real change to your system's
  network state, so it isn't something this tool does on your behalf.
- **Unpriced items are retried once at the end of each collection**: if a
  specific skin/wear came back with no price data during the main pass (a
  transient rate limit or dropped connection), the scanner tries it again once
  more after finishing the rest of that collection, before giving up on it.

### Dashboard (telemetry HUD)

`web/static/` is a no-build, native-ES-module app served by `web/server.py`.
Entry: `index.html` → `js/app.js` (hash router). Screens: `js/views/*.js`.
Charts: vendored uPlot (`vendor/`) + a canvas heatmap in `js/charts.js`.
Overview / Opportunities / Calculator / Price are the rebuilt screens; Patterns /
Lab / Connections still run the previous code via `js/views/legacy.js`.
Logic self-check: open `/charts-selftest.html` — it prints `SELFTEST OK`.
Command bar: press `/`, type a skin / collection / `GO <view>` / `scan|verify top|train`.

## Making scans faster

Steam's ~1 request/1.5s throttle is the floor on speed (it adapts upward, not
down, if Steam starts rate-limiting -- see
[Web dashboard](#web-dashboard-auto-refreshing) above) -- these just reduce
how many requests a scan needs:

- **Narrow the collection filter.** The single biggest lever -- scanning 1
  collection instead of ~90 is ~90x fewer requests.
- **Fast mode** (checkbox in the dashboard, `--fast-mode` on the CLI): prices
  only one wear per skin instead of every wear (up to 5), roughly a 5x cut.
  Cost estimates use the cheapest-looking wear; revenue estimates use
  Field-Tested (the most commonly traded condition) as a stand-in for "typical"
  value -- numbers get rougher, but the scan is much faster. Results already
  cached from a full (non-fast) scan are reused as-is.
- **Skip underpriced-listing scan** (checkbox) / just use the `tradeups`
  subcommand instead of `anomalies` on the CLI: skipping it when you only care
  about trade-ups cuts out that whole scan for the scope.
- **StatTrak checkbox now also applies to the underpriced-listing scan**: it used
  to always check both StatTrak and normal variants for every eligible skin
  (2x the requests); it now checks only whichever one the checkbox says, halving
  that scan's requests.
- **Float-mix strategy is skipped below your Min ROI threshold**: since a result
  below that threshold gets filtered out of what you see anyway, there's no point
  spending extra requests analyzing its float-mix strategy -- only fully computed
  for results that will actually be shown.
- Price-cache writes to disk are now batched (every 10 new entries, or immediately
  when pausing/stopping/finishing) instead of re-writing the whole cache file
  after every single lookup -- less disk I/O on top of the network savings above.
- Once any scope is warm (scanned once), repeat passes within the hour reuse
  the cache and are near-instant regardless of these settings.

## CLI usage

```
# Best trade-ups across all collections
python -m src.main tradeups --top 20

# Only a specific collection, StatTrak variants
python -m src.main tradeups --collection "Dead Hand" --stattrak

# Only show >=10% ROI, export to CSV
python -m src.main tradeups --min-roi 10 --out tradeups.csv

# Underpriced listings in a collection
python -m src.main anomalies --collection "Dead Hand" --min-gap-pct 20

# Covert -> knife/glove trade-ups, only a specific case
python -m src.main covert --case "Dreams & Nightmares" --min-roi 0

# Also search for a cross-collection filler mix that beats the single collection
python -m src.main tradeups --collection "Dead Hand" --mixed-strategy

# Cross-check prices against Skinport/CSFloat and consult the ML calibration model
python -m src.main tradeups --collection "Dead Hand" --verify-prices --use-ml

# Log what actually happened after you executed a trade-up manually
python -m src.main log-outcome --collection "Dead Hand" --from-rarity "Mil-Spec Grade" \
    --to-rarity Restricted --input "MP9 | Hot Rod:Field-Tested:7:0.42" \
    --input "MP9 | Hot Rod:Battle-Scarred:3:0.31" --output-skin "MP9 | Bulldozer" \
    --output-wear "Battle-Scarred" --sale-price 5.10

python -m src.main list-outcomes          # see everything you've logged
python -m src.main train-model            # fit the ML model once you have 20+ sold outcomes
python -m src.main retrain-loop           # watch outcomes and auto-retrain (Ctrl+C to stop)

# Record/refresh a skin's price history and see the naive trend prediction
python -m src.main price-history --skin "AK-47 | Redline" --wear "Field-Tested"

# Cross-check Steam vs. Skinport/CSFloat for winning trade-ups' input + every possible output,
# export a spreadsheet-ready CSV (see "Cross-marketplace price report" above for the 3 modes)
python -m src.main price-report --collection "Dead Hand" --top 20
python -m src.main price-report --fast-mode --out all-collections-fast.csv
python -m src.main price-report --ml-only

# Carry your training data to another machine (merges safely -- see "Training across
# multiple machines" below)
python -m src.main export-data
python -m src.main import-data --file data-archive-20260101-120000.zip

# Bulk-warm the price cache, then run the 4-son guided random-simulation population
python -m src.main scan-prices --collection "Dead Hand"
python -m src.main simulate-population       # Ctrl+C stops all 4 sons
python -m src.main population-status         # one row per son -- status, best ROI, stuck?
python -m src.main best-simulations --top 20 # merged best finds across all sons
python -m src.main nudge-son --son 2         # soft retry: temporarily boost exploration
python -m src.main reset-son --son 2         # hard retry: wipe that son's data/model, start over

# List currently flagged dead/illiquid skins (temporary, weekly-reviewed -- never a blacklist)
python -m src.main dead-skins

# Watch for positive-ROI finds and cross-check every input/output against Skinport/Buff163/CSFloat automatically
python -m src.main verify-queue

# Stage 4 (2026-07): reliability / rare patterns / backup
python -m src.main market-reliability            # unified Healthy/Suspicious/Excluded states
python -m src.main override-reliability --skin "AK-47 | Redline (Field-Tested)" --state excluded
python -m src.main pattern-opportunities --min-confidence medium --sort profit
python -m src.main backup                        # GitHub backup engine status + audit log
python -m src.main backup --run-now              # force one archive+commit+push cycle now
```

Since 2026-07 prices come from the csgotrader.app bulk mirror (one download covers the
whole catalog), so even a full all-collections price scan finishes in seconds -- the old
"first run is slow" caveat about Steam's per-item endpoint no longer applies unless you
explicitly re-enable the live fallback (`steam_prices.configure_steam_live_fallback`).

## Troubleshooting: every request fails, but it's not rate limiting

If the network health panel shows a climbing "errors" count and zero "ok" ever, with
`CERTIFICATE_VERIFY_FAILED: Basic Constraints of CA cert not marked critical`, an
HTTPS-scanning antivirus (Avast/AVG "Web Shield" and similar) is re-signing every
connection with a locally generated CA cert that Python 3.13+'s strict certificate
validation rejects. We hit this directly on this project's own machine (2026-07-10): with
Avast's HTTPS scanning on, 100% of stdlib requests failed while browsers worked fine --
and worse, the antivirus proxy's TLS fingerprint got the traffic bot-flagged by
Cloudflare/Steam on ANY IP, which looked exactly like being "rate limited everywhere".
`src/net.py` now clears the over-strict flag (full verification stays on) so requests at
least complete, but if you see Cloudflare 403s from services that should work, disable the
antivirus's HTTPS/SSL scanning feature -- an earlier version of this note blamed differing
bundled OpenSSL versions between Python installs, which was a misdiagnosis of the same
antivirus interception.

## Roadmap

- **Phase 2 (execution):** integrate a marketplace with an official trading API
  (Skinport/CSFloat/DMarket) once you have an account + API key there, gated
  behind a dry-run/confirm-by-default mode -- never silently auto-spend real money.
- **Order-book-depth-aware cost**: use Steam's order histogram (not just the
  single lowest listing) to model the real cost of buying 10 (or 5, for Covert)
  units -- this is also what would let the mixed-collection filler search (see
  [Manipulating odds with filler skins](#manipulating-odds-with-filler-skins))
  weigh more than one filler collection at once without the cost model becoming
  unrealistic.
- **Float-mix beyond two wear conditions**: the default `--float-strategy`/`--mixed-strategy`
  scan caps at 2 wears / 4 filler slots for speed; a caller can raise `max_distinct_wears`/
  `max_n_filler` for deeper (slower) coverage.
- **DMarket/BitSkins price verification**: both require an account-issued API key for
  market-data reads even read-only, unlike Skinport/CSFloat's genuinely public endpoints -- see
  [Third-party price verification](#third-party-price-verification-drought-detection). Stubbed
  in `src/thirdparty_prices.py`, not implemented.
- **ML/third-party verification on the Covert -> knife/glove path**: currently only wired into
  the regular collection-based `tradeups` scan (`src/tradeup.py`), not `covert.py` -- extending
  it there would mean threading the same `check_thirdparty`/`use_ml` params through
  `covert.py`/`mixed_covert.py`/`mixed_float_covert.py` the same way `tradeup.py`/
  `mixed_float.py` already do.
- **Full float/wear data for every knife/glove**: the Covert -> knife/glove scan
  currently falls back to an approximated float range for finishes missing from
  the main skin feed (see
  [Covert -> knife/glove trade-ups](#covert---knifeglove-trade-ups-oct-2025)) --
  swapping in a data source that carries accurate min/max floats for all of them
  would remove that approximation.
- **Second price source**: done as of 2026-07 via the csgotrader.app mirror
  (`src/bulk_prices.py`), which carries both Steam and Skinport catalogs. Skinport's own
  public items API is no longer an option for anything server-side: its Cloudflare Bot
  Management JS challenge now 403s every non-browser client, residential IPs included
  (verified live 2026-07-10 -- the old note here that it "works fine from a normal
  residential connection" is stale).
