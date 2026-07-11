"""Export/import the accumulated learning data (the data/ directory) as a single portable
.zip archive, so it can be committed to a git repo, backed up, or moved between machines to
keep training continuous -- the whole point of this module is "scan/log on one PC, carry the
results to another."

Scope: data/ only (outcomes.jsonl, price_history.jsonl, model/, sons/).
NOT cache/ -- that's disposable, TTL'd Steam/skin data that regenerates on its own; shipping
it around machines would be pointless and would bloat the archive for no reason.

Import is a MERGE by default, not a naive overwrite, because the realistic use case is
"I've been scanning/logging on two machines independently, now I want them combined" --
overwriting one machine's local data with another's would silently destroy real logged
outcomes. Pass merge=False (--replace on the CLI) if you genuinely want to discard local data
and take the archive's contents wholesale (e.g. setting up a brand new machine with nothing
accumulated yet).
"""
import dataclasses
import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def export_archive(out_path: str | Path | None = None) -> Path:
    """Zips everything currently in data/ into a single .zip file. Default: a timestamped
    file in the project root (data-archive-YYYYMMDD-HHMMSS.zip) -- deliberately NOT written
    inside data/ itself, so re-exporting later doesn't recursively pick up earlier archives."""
    if not DATA_DIR.exists() or not any(DATA_DIR.iterdir()):
        raise FileNotFoundError(
            "data/ doesn't exist or is empty -- nothing accumulated to archive yet. Log an "
            "outcome, look up a skin's price history, or run the search loop first.")
    if out_path is None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_path = PROJECT_ROOT / f"data-archive-{ts}.zip"
    out_path = Path(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in DATA_DIR.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(DATA_DIR))
    return out_path


def _merge_outcomes(incoming_dir: Path) -> int:
    """Union of both outcome logs, deduped by id (already unique/sortable-timestamp-based --
    see outcomes.py) -- a real outcome logged on either machine is kept, nothing is dropped."""
    from . import outcomes as outcomes_mod
    existing = {o.id: o for o in outcomes_mod.load_outcomes()}
    incoming_file = incoming_dir / "outcomes.jsonl"
    if not incoming_file.exists():
        return 0
    added = 0
    with open(incoming_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("id") and row["id"] not in existing:
                try:
                    existing[row["id"]] = outcomes_mod.Outcome(**row)
                    added += 1
                except TypeError:
                    continue  # schema mismatch (e.g. archive from an older version) -- skip, don't crash
    if added:
        outcomes_mod.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(outcomes_mod.OUTCOMES_FILE, "w", encoding="utf-8") as f:
            for o in sorted(existing.values(), key=lambda o: o.logged_at):
                f.write(json.dumps(dataclasses.asdict(o)) + "\n")
    return added


def _merge_price_history(incoming_dir: Path) -> int:
    """Union of both price histories, deduped by (ts, market_hash_name, currency) -- the same
    snapshot recorded independently on two machines shouldn't double-count."""
    from . import price_history as price_history_mod
    incoming_file = incoming_dir / "price_history.jsonl"
    if not incoming_file.exists():
        return 0
    seen = set()
    existing_lines = []
    if price_history_mod.HISTORY_FILE.exists():
        with open(price_history_mod.HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                existing_lines.append(line)
                try:
                    row = json.loads(line)
                    seen.add((row.get("ts"), row.get("market_hash_name"), row.get("currency")))
                except json.JSONDecodeError:
                    pass
    added = 0
    new_lines = []
    with open(incoming_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (row.get("ts"), row.get("market_hash_name"), row.get("currency"))
            if key not in seen:
                seen.add(key)
                new_lines.append(line)
                added += 1
    if added:
        price_history_mod.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(price_history_mod.HISTORY_FILE, "w", encoding="utf-8") as f:
            for line in existing_lines + new_lines:
                f.write(line + "\n")
    return added


def import_archive(archive_path: str | Path, merge: bool = True) -> dict:
    """Extracts an archive into data/. merge=True (default): unions outcomes/price-history,
    and leaves the existing trained model alone (a trained model is a derived artifact of
    outcomes.jsonl, not something two versions can be meaningfully merged together -- run
    train-model afterward to fold in anything new). merge=False: wipes data/ and replaces it
    wholesale with the archive's contents, including its model/ -- only do this on a machine
    with nothing worth keeping yet."""
    archive_path = Path(archive_path)
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(tmp_path)

        if not merge:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            for item in DATA_DIR.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            for item in tmp_path.iterdir():
                dest = DATA_DIR / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            return {"mode": "replace", "outcomes_added": None, "price_points_added": None}

        outcomes_added = _merge_outcomes(tmp_path)
        price_points_added = _merge_price_history(tmp_path)
        return {"mode": "merge", "outcomes_added": outcomes_added,
                "price_points_added": price_points_added}
