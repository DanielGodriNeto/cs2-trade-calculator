# Real-Outcome Backfill Importer — Design Spec (N1)

**Date:** 2026-08-29
**Status:** Design only. A follow-on `writing-plans` cycle builds it from this spec; nothing here is implemented.
**Owner module (to be created):** `src/outcome_import.py` + a CLI subcommand + one web endpoint. Writes `data/outcomes.jsonl` through `src/outcomes.py`'s existing helpers only.
**Source finding:** N1 in the 2026-08-29 ML & performance code review (`docs/planning/plans/2026-08-29-ml-and-perf-betterments.md`).

---

## 1. Problem

`data/outcomes.jsonl` is **empty** (0 bytes, verified). Consequences, all current:

- `learning.py` — the real-ROI calibration model, the codebase's stated "compare against real prices you actually paid/received" goal — is **fully dormant**. `learning.train()` refuses below `MIN_TRAINING_SAMPLES = 20` (`src/learning.py:37`, `:170-174`); `learning.predict_roi()` returns `None` for every candidate (`:274`); `learning.get_model_status()` reports `{"trained": False, "n_samples": 0}` (`:247-249`).
- Every consumer that would use `ml_predicted_roi_pct` as a tiebreaker (`float_mix.py` / `mixed_float.py` / `tradeup.py`, and `ranking._ml_agreement_bonus` for the *real* model path) gets nothing.
- The retrain-watch loop (`web/server.py:1480`) spins forever polling `outcomes.stats()` and never has enough rows to fire.

**This spec is about seeding history**, not about reducing future logging friction. Two-stage outcome logging (log the craft now, fill in the sale later) is **backlog item P4** and a separate piece of work; it makes *new* outcomes cheaper to record. N1 is orthogonal: it takes trades the user *already executed* — before this tool tracked them — and turns them into `Outcome` rows so `learning.py` has a corpus to train on today.

### Non-goals

- Not auto-buy / auto-sell (the tool never does that — README "Why it doesn't auto-buy").
- Not a live Steam scraper baked into the app. The importer reads a file the user produced; it does not hold Steam session cookies or paginate Steam on a schedule.
- Not a replacement for hand-logging. Hand-logged outcomes are authoritative; backfill rows are marked and never overwrite them (§4).
- Not P4. No change to `outcomes.log_outcome`'s live signature beyond one new optional field.

---

## 2. Input

### 2.1 What Steam actually exposes

There is **no bulk "export my market history" button** in the Steam Community Market UI. The realistically obtainable forms, in order of practicality:

1. **The `market/myhistory` paginated JSON endpoint.**
   `https://steamcommunity.com/market/myhistory/?norender=1&start=<N>&count=<M>` (also reachable as `/market/myhistory/render/`). Returns:
   - `results_html` — an HTML fragment, one `<div class="market_listing_row">` per event (Purchase / Sale / Listing created / Listing canceled), carrying the item's `market_hash_name` (via the listing name + game), the **price** of that event, and a **date string** ("For sale: 12 Aug" / "Sold: 14 Aug" style — year is omitted for the current year, a parsing hazard);
   - `assets` — the item metadata keyed by `appid/contextid/assetid` (name, `market_hash_name`, type line with the wear bracket, icon);
   - `hovers` — JS wiring that links each row to its asset.
   Constraints the importer must handle: it requires the user's **own logged-in session cookie** (`steamLoginSecure`), it is **rate-limited** (HTTP 429 on fast paging; practical batch ≈ `count=100`, a few seconds apart), and Steam caps how far back `start` will go (a few thousand rows in practice). Crucially, **it does not tell you which purchase rows combine into which trade-up** — it is a flat ledger of buys and sells, not contracts.

2. **In-client inventory history** (Steam client → Inventory → *trade/market history*). Human-readable only; no machine format. Not a viable importer input; mentioned so the spec is complete.

3. **Third-party trackers** (e.g. a browser extension the user already runs that scrapes `myhistory` into CSV). Out of scope to depend on, but if the user has such a CSV it lands in the same manual-CSV path as (4).

**Design decision:** the importer supports **two input modes**, and the manual CSV is the reliable one:

- `--source steam-json PATH` — one or more raw `myhistory` JSON responses saved by the user (the app never fetches them itself; the user pastes the URL into their browser with `&norender=1`, saves the JSON, repeats for each `start` offset). The importer parses `results_html` + `assets`, pairs each **Sale** row with the matching **Purchase** rows for the same `market_hash_name` by nearest-earlier date, and emits *best-effort* partial `Outcome`s. Because Steam's ledger doesn't group a trade-up's 10 inputs, this mode **cannot reconstruct the input mix** — it can only recover `output_skin` / `output_wear` / `sale_price` / date, and (optionally) a rough `cost_paid` if the user also supplies the input asset IDs. It is a convenience for the output side; every row it produces is flagged `needs_review` and requires the user to confirm or complete the input mix before it is written.
- `--source csv PATH` — **the primary path.** A flat CSV the user fills in from their own records (screenshots, notes, the `myhistory` page read by eye). This is where a real, trainable `Outcome` comes from.

### 2.2 Manual CSV format

One row per executed trade-up. Columns (header row required; order-independent; the brief's list, made concrete):

| Column | Required | Example | Maps to |
|---|---|---|---|
| `date` | yes | `2026-07-14` or `2026-07-14T18:22:00Z` | `logged_at` (see §3) |
| `collection` | yes | `The St. Marc Collection` | `Outcome.collection`; validated against `data.collections_index(data.load_skins())` |
| `kind` | no (default `collection`) | `collection` \| `case` | `Outcome.kind` |
| `from_rarity` | yes | `Industrial Grade` | `Outcome.from_rarity`; validated against `data.RARITY_CHAIN` names |
| `to_rarity` | yes | `Mil-Spec Grade` | `Outcome.to_rarity`; must be the adjacent tier above `from_rarity` |
| `stattrak` | no (default `false`) | `true` \| `false` | `Outcome.stattrak` |
| `inputs` | yes | `XM1014 \| Banana Leaf@Factory New x7 @0.27; Nova \| Turquoise Pour@Minimal Wear x1 @0.10; ...` | `Outcome.input_mix` — see grammar below |
| `filler_collection` | no | `The Ascent Collection` | `Outcome.filler_collection` |
| `filler_inputs` | no | same grammar as `inputs`, single skin | `Outcome.filler_input` (dict, not list) |
| `output_skin` | yes | `MP9 \| Hot Rod` | `Outcome.output_skin` |
| `output_wear` | yes | `Factory New` or `0.03` | `Outcome.output_wear` (+ `output_float`) via `outcomes.resolve_wear` |
| `sale_price` | no (blank ⇒ `status=held`) | `4.10` | `Outcome.sale_price` |
| `status` | no (default derived) | `sold` \| `held` \| `kept` | `Outcome.status` |
| `currency` | no (default `1`) | `1` | `Outcome.currency` |
| `notes` | no | `first FT roll` | `Outcome.notes` |

**`inputs` cell grammar** (semicolon-separated units; each unit = `SKIN@WEAR xCOUNT @UNIT_PRICE`):
```
XM1014 | Banana Leaf @ Factory New x7 @0.27 ; Nova | Turquoise Pour @ Minimal Wear x1 @0.10
```
- `SKIN` — exact market name (`AK-47 | Redline`); whitespace around `|` tolerated.
- `WEAR` — a bracket name (`Field-Tested`) **or** a float (`0.21`) — resolved via `outcomes.resolve_wear`, exactly as `outcomes.log_outcome` already does for hand-logged rows (`src/outcomes.py:129-135`).
- `xCOUNT` — integer; the sum of all input counts (+ filler count) must equal `rules_engine.STANDARD_INPUT_COUNT` (10) or the Covert path count (5); a mismatch is a per-row validation error (row rejected, reported, import continues).
- `@UNIT_PRICE` — price per unit the user paid, in `currency`.

A blank `sale_price` with no explicit `status` ⇒ `status = "held"` (real cost, no realised return — `learning.train()` will skip it until it's sold, exactly like a hand-logged held craft). `sale_price` present with no `status` ⇒ `status = "sold"`.

---

## 3. Field-by-field mapping to `outcomes.Outcome`

`Outcome` fields (`src/outcomes.py:39-65`), and where each value comes from on import:

| `Outcome` field | Manual CSV | Steam `myhistory` JSON | Notes |
|---|---|---|---|
| `id` | generated | generated | `outcomes._new_id()`-shaped: `<utc-iso>-<6 rand>`. Deterministic variant for dedupe safety: `bf-<sha1(signature)[:12]>` so re-importing the same CSV can't double-insert (§4). |
| `logged_at` | `date` → epoch (`time.strptime`, assume 00:00Z if no time) | Sale row's parsed date → epoch | This is the **trade date**, not the import moment — so `learning`'s recency/ordering sees the real timeline. Import moment is recorded in `notes` (`"backfilled 2026-08-30"`). |
| `collection` | `collection` | from `assets[].market_hash_name` → collection lookup (often ambiguous) | validated |
| `kind` | `kind` (default `collection`) | `collection` (Steam can't tell) | one of `outcomes` `("collection","case")` |
| `stattrak` | `stattrak` | detected from the asset name prefix `StatTrak™` | |
| `from_rarity` / `to_rarity` | columns | **not recoverable** — user must supply or the row is `needs_review` | validated adjacent in `data.RARITY_CHAIN` |
| `currency` | `currency` (default 1) | assumed 1 (wallet currency of the session) | |
| `input_mix` | parsed `inputs` grammar → `[{"skin","wear","count","unit_price_paid"}]` | **not recoverable** from the flat ledger | `outcomes.log_outcome` re-runs `resolve_wear` per unit, so passing bracket names or floats both work |
| `filler_collection` | `filler_collection` | not recoverable | |
| `filler_input` | parsed `filler_inputs` → single dict or `None` | not recoverable | |
| `output_skin` | `output_skin` | Sale row's `market_hash_name` (skin part) | |
| `output_wear` | `output_wear` (name or float) | Sale row's type line → bracket | `resolve_wear` → `(name, float)` |
| `output_float` | derived by `resolve_wear` if a float was given | `None` (Steam market history has no float) | default `None`, backward-compatible (`outcomes.py:62-64`) |
| `sale_price` | `sale_price` | Sale row price (Steam **subtracts its fee before display** on some views — the importer records the **gross list price**; see the fee note below) | |
| `status` | `status` or derived | `"sold"` (it's a Sale row) | |
| `cost_paid` | **computed** by `outcomes._compute_financials` from `input_mix` + `filler_input` unit prices | only if the user also mapped the input asset IDs (rare) → else row is `needs_review` | never taken from the CSV directly — always recomputed, same as hand-logging |
| `profit` | **computed** — `after_fee − cost_paid`, `after_fee = sale_price·(1 − STEAM_SELL_FEE)` | same | `None` while `status != "sold"` |
| `roi_pct` | **computed** — `profit / cost_paid · 100` | same | `None` while unsold |
| `win` | **computed** — `profit > 0` | same | `None` while unsold |
| `predicted_roi_pct` | **`None`** | **`None`** | the tool didn't score this trade at the time — there is no honest value. `learning._featurize_outcome` already maps `None` → `NaN` for this feature (`learning.py:106`), so a backfill row trains fine with it absent. |
| `predicted_revenue` | **`None`** | **`None`** | same reasoning; not a model feature, display-only |
| `ml_predicted_roi_pct` | **`None`** | **`None`** | the ML model wasn't trained then; `None` is the truthful value |
| `output_float` | see above | `None` | |
| `notes` | `notes` + `"[backfill]"` tag | `"[backfill from steam myhistory]"` | |
| `source` | **`"backfill"`** | **`"backfill"`** | NEW field — see §4 |

**Computed vs. recovered, summarised:** everything financial (`cost_paid`, `profit`, `roi_pct`, `win`) is **derived on write** by `outcomes._compute_financials(input_mix, filler_input, sale_price, status)` (`src/outcomes.py:95-107`) — the importer never trusts a CSV's own profit column, exactly as `outcomes.log_outcome` never does. The three "what did the tool predict at the time" fields (`predicted_roi_pct`, `predicted_revenue`, `ml_predicted_roi_pct`) are **unrecoverable by definition** and set to `None`. Everything else is copied from the CSV (manual mode) or best-effort-parsed and flagged for review (Steam mode).

**Steam fee note:** `outcomes._compute_financials` applies `after_fee = sale_price · (1 − tradeup.STEAM_SELL_FEE)`. So `sale_price` on an `Outcome` is always the **gross buyer-paid price** (what the item "sold for" on the listing), and the fee is taken by the tool's math. The importer must record the gross figure; when parsing Steam `myhistory` it must use the "sold for" price, not any "you received" net figure, or profit will be double-fee'd. This is stated in the CSV column help too.

---

## 4. Safeguards

### 4.1 The `source` field (new, optional, backward-compatible)

`outcomes.load_outcomes()` builds each row as `Outcome(**json.loads(line))` and **skips any line that raises `TypeError`** (`src/outcomes.py:165-168`). An unknown JSON key ⇒ `TypeError` ⇒ the row is silently dropped. Therefore `source` **must be a real dataclass field with a default**, not an ad-hoc dict key:

```python
@dataclass
class Outcome:
    ...
    output_float: float | None = None
    source: str | None = None   # None / "manual" => hand-logged via log_outcome; "backfill" => imported by outcome_import.
    notes: str = ""
```

- Default `None` (equivalently treated as `"manual"`) so **every existing and future hand-logged line still loads** — old lines have no `source` key, `Outcome(**{...})` fills the default, no `TypeError`.
- `outcomes.log_outcome()` gains `source: str | None = None` (keyword, default `None`) and sets it on the constructed `Outcome` — so the field round-trips through `asdict()` on write.
- Backfill rows are written with `source="backfill"`.
- `learning.train()` can then optionally down-weight backfill rows: extend its existing reliability `sample_weight` walk (`learning.py:187-194`) with `× BACKFILL_WEIGHT` (a new `learning` constant, default `1.0` = no change; tunable to e.g. `0.7` later if backfill rows prove noisier) when `o.source == "backfill"`. Default `1.0` means N1 lands with zero behaviour change to model selection until someone deliberately tunes it — the field just makes the distinction *available*.

### 4.2 Dedupe

Before writing, `outcome_import` loads the current `outcomes.load_outcomes()` and builds a signature per existing row:

```
sig(o) = sha1(f"{o.collection}|{int(bool(o.stattrak))}|{o.from_rarity}|{o.to_rarity}|"
              f"{o.output_skin}|{o.output_wear}|{date_only(o.logged_at)}|"
              f"{sorted_input_units(o.input_mix)}").hexdigest()[:16]
```

where `sorted_input_units` is the `skin|wear|count` multiset joined sorted — the same *composition + date* identity shape `sim_blacklist.signature` uses for trade-up compositions (`src/sim_blacklist.py:38-47`), plus the trade date and the realised output. A candidate import row whose signature matches **any** existing row is **skipped** (counted, reported), regardless of that existing row's `source`.

The deterministic `id` (`bf-<sha1(sig)[:12]>`) is a second guard: two runs of the same CSV produce identical ids, and a pre-write `id in {existing ids}` check catches a re-import even if the signature scheme is later changed.

### 4.3 Never overwrite a hand-logged outcome

- The importer is **append-only**: it calls `outcomes.log_outcome(..., source="backfill")` per accepted row (which does a plain `open(..., "a")` append — `src/outcomes.py:148-150`). It never calls `update_outcome_sale` and never rewrites the file.
- On a signature collision with an existing row where `existing.source in (None, "manual")` (a hand-logged row), the import row is **dropped unconditionally** and logged as `skipped: collides with hand-logged <id>`. A hand-logged row is always the source of truth; the backfill defers to it.
- On a signature collision with an existing `source == "backfill"` row, also skip (idempotent re-import).
- Dry-run first: `--dry-run` (default **on**; `--commit` to actually write) prints the full accept / skip / needs-review breakdown and the exact `Outcome` JSON that *would* be appended, so the user reviews before anything touches `outcomes.jsonl`.
- A pre-commit backup: on `--commit`, copy `data/outcomes.jsonl` to `data/outcomes.jsonl.pre-backfill-<utc>` first (cheap; the file is small; lets the user revert by hand). Documented, not automatic-restore.

### 4.4 Validation (per row, non-fatal)

Each row is validated before acceptance; a failure rejects **that row only**, import continues, and the reason is in the report:
- `collection` / `filler_collection` exist in `data.collections_index(data.load_skins())`;
- `from_rarity` / `to_rarity` are names in `data.RARITY_CHAIN` and adjacent (to is one tier above from);
- every `inputs` skin exists at `from_rarity` in that collection; every `output_skin` exists at `to_rarity`;
- input counts (+ filler) sum to 10 (or 5 for the Covert→knife/glove path);
- `sale_price` (if present) and every `@UNIT_PRICE` parse as positive floats;
- `date` parses;
- `outcomes.log_outcome`'s own `ValueError`/`TypeError` (raised for `status`/`input_mix`/`sale_price` inconsistencies, `src/outcomes.py:117-127`) is caught and turned into a row-level rejection rather than aborting the run.

---

## 5. UI touchpoint

An **"Import history"** control, wired to a new endpoint `POST /api/outcomes/import` (multipart file upload: the CSV or the saved Steam JSON, plus `mode`, `dry_run`, `currency`). The endpoint calls `outcome_import.run(...)` and returns the accept / skip / needs-review report as JSON; with `dry_run=false` it also returns the appended `Outcome`s. Sits beside the existing `/api/log-outcome` / `/api/update-outcome-sale` handlers (`web/server.py:1023-1047`) and, like them, takes no `state.lock` (`outcomes.py` has its own write lock).

Placement in the redesigned dashboard — cross-referencing `docs/planning/plans/2026-08-29-telemetry-hud-redesign.md` and `docs/planning/specs/2026-08-29-telemetry-hud-redesign-design.md`:

- **Primary home: the Connections screen (`CONN`).** The redesign runs Connections through `js/views/legacy.js` (telemetry-HUD plan Task 6 / spec §7.1) — it mounts the current markup + bind logic, CSS-reskinned only. The importer control is a small panel there ("Backfill / Import" section): a file picker, a `mode` toggle (Manual CSV / Steam JSON), a "Dry run" checkbox (default checked), a "Download CSV template" link (serves a static header-only CSV), and a results `<pre>` for the report. Connections is the natural fit because it is already the "hook the tool up to your data" screen (CSFloat key, GitHub backup, NTFY).
- **Secondary surfacing: the Overview screen (`OVW`).** Per spec §6.1, OVW shows a "Learning progress" KPI cell and three inline connection status chips. When `learning.get_model_status()` reports `trained: false` **and** `outcomes.stats()["n_total"] == 0`, the "Learning progress" cell renders a one-line call to action — `0 outcomes · Import history →` — linking to the `CONN` importer panel. This is the discoverability path: the user who never opens Connections still sees, on the landing screen, that the real-ROI model is starved and that there's a way to feed it. It disappears once any outcome exists.
- No new instrument, no new chart. The importer is a form + a text report, consistent with the redesign's "quiet everywhere except the command bar" rule (spec §2). The command bar (`command.js`) gains one `ACTION`: `import history` → focuses/opens the `CONN` importer panel (no destructive confirm — the panel's own dry-run default is the guard).

---

## 6. File / module plan

```
src/outcome_import.py            new: parse_csv(), parse_steam_myhistory(), validate_row(), dedupe(),
                                      run(source, path, currency, dry_run) -> ImportReport
                                      constants: BACKFILL_SOURCE="backfill"
src/outcomes.py                  Outcome: + source: str | None = None ; log_outcome(..., source=None)
src/learning.py                  train(): optional BACKFILL_WEIGHT (default 1.0) in the sample_weight walk
src/main.py (CLI)                new subcommand: `import-outcomes --source {csv,steam-json} PATH [--commit] [--currency 1]`
web/server.py                    POST /api/outcomes/import (multipart) -> outcome_import.run(...)
                                 GET  /api/outcomes/import/template -> static header-only CSV
web/static/js/views/legacy.js    CONN: "Backfill / Import" panel (file picker, mode, dry-run, report <pre>)
web/static/js/views/overview.js  OVW: "Learning progress" cell -> "Import history" CTA when 0 outcomes
web/static/js/command.js         ACTIONS: + { id: "import history", ... }
tools/check_outcome_import.py    new: assertion script
data/outcomes.jsonl.pre-backfill-<utc>   written on --commit only
```

Reuses: `outcomes.log_outcome` / `load_outcomes` / `resolve_wear` / `_compute_financials` / `_new_id`, `data.load_skins` / `collections_index` / `RARITY_CHAIN`, `oddsmath.wear_from_float` (via `resolve_wear`), `rules_engine.STANDARD_INPUT_COUNT`, `tradeup.STEAM_SELL_FEE` (via `_compute_financials`). Stdlib `csv`, `hashlib`, `html.parser` (for the Steam `results_html` fragment) — no new dependency.

## 7. Deliverable of the follow-on plan

`src/outcome_import.py` + the wiring above, with:
- `tools/check_outcome_import.py` (`python tools/<name>.py → OK/raise` style) asserting: a 3-row sample CSV parses to 3 `Outcome`s with `source="backfill"`, correct `cost_paid`/`profit`/`roi_pct`/`win` from `_compute_financials`, and `predicted_roi_pct`/`predicted_revenue`/`ml_predicted_roi_pct` all `None`; a re-import of the same CSV writes 0 new rows (dedupe); a row colliding with a hand-logged `Outcome` is skipped; an old `outcomes.jsonl` line without a `source` key still loads (backward compat); an input-count-≠-10 row is rejected without aborting the run;
- a committed sample template CSV under `docs/` or `web/static/`;
- the plan's final step notes that once ≥ 20 valid **sold** backfill rows exist, `learning.train()` becomes eligible — but does not require the user to have data to merge the code.

---

## Self-review (placeholder scan)

Scanned for `TBD`, `TODO`, `FIXME`, `add error handling`, bare "write tests", `figure out`, hand-wave `etc.`:
- None present.
- All five required brief points covered concretely: (1) problem — empty `outcomes.jsonl` ⇒ `learning.py` dormant at `MIN_TRAINING_SAMPLES = 20`, cited to `learning.py:37/170-174/247-249/274`; P4 two-stage logging named as separate future work, this is seeding (§1); (2) input — the real `market/myhistory?norender=1` endpoint (no bulk export UI, session-cookie + rate-limit + no contract grouping) and the primary manual CSV with a full column table + `inputs` cell grammar (§2); (3) field-by-field `Outcome` mapping table with computed-vs-recovered split and the three `predicted_*` fields → `None` with the reason, plus the gross-price/fee hazard (§3); (4) safeguards — new `source: str | None = None` dataclass field (why a bare dict key breaks `load_outcomes`'s `TypeError` skip, cited `outcomes.py:165-168`), composition+date dedupe signature (shape borrowed from `sim_blacklist.signature`), append-only + never-overwrite-hand-logged + dry-run-default + pre-commit backup, per-row non-fatal validation (§4); (5) UI — "Import history" on `CONN` (runs via `legacy.js`) with an `OVW` "Learning progress" CTA fallback and a `command.js` action, cross-referencing the telemetry-HUD plan + design spec by path and section (§5).
- Every code citation is a verified file+line (`outcomes.py:39-65/95-107/117-127/129-135/148-150/165-168`, `learning.py:37/106/170-174/187-194/247-249/274`, `sim_blacklist.py:38-47`, `web/server.py:1023-1047/1480`).
