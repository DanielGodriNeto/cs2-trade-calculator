"""Export/import the accumulated learning data (the data/ directory) as a set of portable
.zip archive parts, so it can be committed to a git repo, backed up, or moved between
machines to keep training continuous -- the whole point of this module is "scan/log on one
PC, carry the results to another."

Scope: data/ only (outcomes.jsonl, price_history.jsonl, model/, sons/), MINUS the append-only
logs that split_archive.py already carries as small .jsonl.gz chunks under data/archive/
(market_reliability_log.jsonl and every sons/son_*/simulations.jsonl -- see that module's
sources()). Zipping those live files here too would duplicate gigabytes of data that's
already archived elsewhere and single-handedly blow the per-part size cap below. *.tmp
scratch files from interrupted atomic writes are skipped too. NOT cache/ -- that's
disposable, TTL'd Steam/skin data that regenerates on its own; shipping it around machines
would be pointless and would bloat the archive for no reason.

Split into parts: GitHub hard-rejects any single pushed file over 100 MB (the automated
backup worker in backup_engine.py commits+pushes these), so files are packed whole into
successive zip parts, starting a new part whenever the next file's raw size would push the
current part over MAX_PART_RAW_BYTES. 90 MB raw stays safely under the 100 MB limit even for
a part whose contents don't compress at all -- same conservative-cap pattern split_archive.py
uses for the logs this module excludes. A single file larger than the cap on its own still
gets its own part (nothing else to do with it), which can only happen for files split_archive
doesn't already own growing unexpectedly large.

Import is a MERGE by default, not a naive overwrite, because the realistic use case is
"I've been scanning/logging on two machines independently, now I want them combined" --
overwriting one machine's local data with another's would silently destroy real logged
outcomes. Pass merge=False (--replace on the CLI) if you genuinely want to discard local data
and take the archive's contents wholesale (e.g. setting up a brand new machine with nothing
accumulated yet).
"""
import dataclasses
import json
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Raw (uncompressed) bytes per zip part before a new one is started. Stays well under
# GitHub's 100 MB hard per-file limit -- see module docstring.
MAX_PART_RAW_BYTES = 90 * 1024 * 1024

_PART_RE = re.compile(r"^(.*)_part\d+\.zip$")


def _split_archive_owned_files() -> set[Path]:
    """Resolved paths of the live logs split_archive.py already archives incrementally --
    excluded here so this export never duplicates them (see module docstring)."""
    from . import split_archive as split_archive_mod
    return {path.resolve() for _, path, _ in split_archive_mod.sources()}


def _files_to_archive() -> list[Path]:
    owned = _split_archive_owned_files()
    out = []
    for path in sorted(DATA_DIR.rglob("*")):
        if not path.is_file() or path.suffix == ".tmp" or path.resolve() in owned:
            continue
        out.append(path)
    return out


def export_archive(out_path: str | Path | None = None,
                    max_part_bytes: int = MAX_PART_RAW_BYTES) -> list[Path]:
    """Zips data/ (minus the files split_archive.py already owns -- see module docstring)
    into one or more <=max_part_bytes zip parts, named `<stem>_partNNN.zip`. Default stem:
    a timestamped file in the project root (data-archive-YYYYMMDD-HHMMSS.zip) -- deliberately
    NOT written inside data/ itself, so re-exporting later doesn't recursively pick up earlier
    archives. Returns the list of part paths written, in order."""
    files = _files_to_archive()
    if not files:
        raise FileNotFoundError(
            "data/ doesn't exist or is empty (aside from files already archived separately by "
            "split-data) -- nothing accumulated to archive yet. Log an outcome, look up a "
            "skin's price history, or run the search loop first.")
    if out_path is None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_path = PROJECT_ROOT / f"data-archive-{ts}.zip"
    out_path = Path(out_path)
    stem = out_path.with_suffix("")

    parts: list[Path] = []
    zf: zipfile.ZipFile | None = None
    part_raw_bytes = 0

    def _start_part() -> None:
        nonlocal zf, part_raw_bytes
        part_path = Path(f"{stem}_part{len(parts) + 1:03d}.zip")
        parts.append(part_path)
        zf = zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED)
        part_raw_bytes = 0

    _start_part()
    for path in files:
        size = path.stat().st_size
        if part_raw_bytes and part_raw_bytes + size > max_part_bytes:
            zf.close()
            _start_part()
        zf.write(path, path.relative_to(DATA_DIR))
        part_raw_bytes += size
    zf.close()
    return parts


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


def _resolve_parts(archive_path: str | Path | list) -> list[Path]:
    """Accepts a single part, a list of parts, or a directory of parts. Given one part named
    `<prefix>_partNNN.zip`, auto-discovers its siblings (`<prefix>_part*.zip` in the same
    directory) so passing just the first part is enough to import a whole multi-part export
    (see export_archive)."""
    if isinstance(archive_path, (list, tuple)):
        paths = [Path(p) for p in archive_path]
        for p in paths:
            if not p.exists():
                raise FileNotFoundError(f"Archive not found: {p}")
        return paths

    path = Path(archive_path)
    if path.is_dir():
        paths = sorted(path.glob("*.zip"))
        if not paths:
            raise FileNotFoundError(f"No .zip archive parts found in {path}")
        return paths
    if not path.exists():
        raise FileNotFoundError(f"Archive not found: {path}")
    m = _PART_RE.match(path.name)
    if m:
        siblings = sorted(path.parent.glob(f"{m.group(1)}_part*.zip"))
        if siblings:
            return siblings
    return [path]


def import_archive(archive_path: str | Path | list, merge: bool = True) -> dict:
    """Extracts an archive (single part, multiple parts, or a directory of parts -- see
    _resolve_parts) into data/. merge=True (default): unions outcomes/price-history, and
    leaves the existing trained model alone (a trained model is a derived artifact of
    outcomes.jsonl, not something two versions can be meaningfully merged together -- run
    train-model afterward to fold in anything new). merge=False: wipes data/ and replaces it
    wholesale with the archive's contents, including its model/ -- only do this on a machine
    with nothing worth keeping yet."""
    parts = _resolve_parts(archive_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for part in parts:
            with zipfile.ZipFile(part) as zf:
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
