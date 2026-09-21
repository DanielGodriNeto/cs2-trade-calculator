"""Split the giant append-only JSONL logs into small, dated, gzip-compressed chunks that
git (and GitHub's hard 100 MB per-file limit) can actually carry.

Why this exists: the project's cross-machine story is "commit data/ to git" (see the
README's "Training across multiple machines"), but a few logs outgrow GitHub's limit within
a day or two of running the sim loop:

  data/market_reliability_log.jsonl     every reliability transition (~200 MB/day)
  data/sons/son_N/simulations.jsonl     every simulated trade-up, never truncated
  data/evolution/lineage.jsonl          every elite/mutation/meta-agent lineage event

Those live files are APPEND-ONLY BY CONTRACT -- each son's surrogate model trains on its
own full simulations.jsonl (see sim_loop.py / sim_learning.py) -- so this module NEVER
modifies or truncates them. Instead, `split` incrementally copies not-yet-archived lines
into immutable chunks:

  data/archive/<label>/<YYYY-MM-DD>_partNNN.jsonl.gz

A new chunk starts when the record's date changes or the current one hits RAW_CHUNK_CAP of
raw JSONL, so no chunk approaches GitHub's limit even before compression (gzip shrinks
these logs drastically anyway -- the records repeat long Steam CDN image URLs). Chunks are
written once and never rewritten: each run resumes from the byte offset stored in the
manifest, so it's safe to run at any time, including while the sim loop is appending (a
partially written final line -- no trailing newline yet -- is left for the next run).
Raw bytes are copied verbatim, so concatenating a label's chunks in manifest order
reproduces the archived prefix of the live file byte-for-byte.

data/archive/<label>/manifest.json records, per chunk: date, record count, unique skin
count, first/last record time, and raw/compressed sizes -- so you can see what data a
clone carries without decompressing anything.

`restore` rebuilds a live file by concatenating its chunks (for a fresh clone). It only
writes a live file that is missing or empty -- it will never overwrite accumulated real
data (same caution as data_archive.import_archive's merge-by-default).

The live files themselves are .gitignore'd; the chunks + manifests are what get committed.
"""
import gzip
import json
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
ARCHIVE_DIR = DATA_DIR / "archive"

# Raw (uncompressed) bytes per chunk before a new part is started. 48 MB raw stays far
# under GitHub's 100 MB hard limit even if a chunk were somehow stored uncompressed, and
# compresses to a few MB -- small enough that git pushes/pulls stay quick.
RAW_CHUNK_CAP = 48 * 1024 * 1024


def sources() -> list[tuple[str, Path, tuple[str, ...]]]:
    """(label, live file, timestamp-field candidates) for every log big enough to need
    splitting. The sons are discovered by glob, not hardcoded, so a 5th son's
    simulations.jsonl would be picked up automatically."""
    out = [("market_reliability_log", DATA_DIR / "market_reliability_log.jsonl", ("at",)),
           ("evolution_lineage", DATA_DIR / "evolution" / "lineage.jsonl", ("at",))]
    for sim in sorted((DATA_DIR / "sons").glob("son_*/simulations.jsonl")):
        out.append((f"{sim.parent.name}_simulations", sim, ("simulated_at",)))
    return out


def _manifest_path(label: str) -> Path:
    return ARCHIVE_DIR / label / "manifest.json"


def _load_manifest(label: str, source_path: Path) -> dict:
    path = _manifest_path(label)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "source": str(source_path.relative_to(PROJECT_ROOT)).replace(os.sep, "/"),
        "bytes_archived": 0,   # byte offset into the live file already covered by chunks
        "lines_archived": 0,
        "part_counters": {},   # date -> highest part number written for that date
        "chunks": [],
    }


def _save_manifest(label: str, manifest: dict) -> None:
    path = _manifest_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    os.replace(tmp, path)


def _record_skins(rec: dict, acc: set) -> None:
    """Every skin a record mentions: the reliability log's market_hash_name, and a
    simulation's input mix + possible outputs."""
    name = rec.get("market_hash_name")
    if name:
        acc.add(name)
    for key in ("input_mix", "output_distribution"):
        items = rec.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("skin"):
                    acc.add(item["skin"])


def _fmt_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


class _Chunk:
    """Accumulates raw lines until the date rolls over or RAW_CHUNK_CAP is hit."""

    def __init__(self, date: str):
        self.date = date
        self.lines: list[bytes] = []
        self.raw_bytes = 0      # bytes of the live file this chunk covers (incl. blanks)
        self.records = 0
        self.skins: set = set()
        self.first_ts: float | None = None
        self.last_ts: float | None = None

    def add(self, raw: bytes, rec: dict | None, ts: float | None) -> None:
        self.lines.append(raw)
        self.raw_bytes += len(raw)
        if rec is not None:
            self.records += 1
            _record_skins(rec, self.skins)
        if ts is not None:
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts


def _flush_chunk(label: str, chunk: _Chunk, manifest: dict) -> dict:
    """Writes one immutable .jsonl.gz chunk and commits it to the manifest immediately, so
    an interruption can never lose or double-archive lines."""
    counters = manifest["part_counters"]
    part = counters.get(chunk.date, 0) + 1
    counters[chunk.date] = part
    out_dir = ARCHIVE_DIR / label
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{chunk.date}_part{part:03d}.jsonl.gz"
    tmp = out_dir / (name + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=6) as gz:
        for raw in chunk.lines:
            gz.write(raw)
    os.replace(tmp, out_dir / name)

    fmt = lambda ts: (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None)
    entry = {
        "file": name,
        "date": chunk.date,
        "records": chunk.records,
        "unique_skins": len(chunk.skins),
        "first_record_at": fmt(chunk.first_ts),
        "last_record_at": fmt(chunk.last_ts),
        "raw_bytes": chunk.raw_bytes,
        "gz_bytes": (out_dir / name).stat().st_size,
    }
    manifest["chunks"].append(entry)
    manifest["bytes_archived"] += chunk.raw_bytes
    manifest["lines_archived"] += chunk.records
    _save_manifest(label, manifest)
    return entry


def split_source(label: str, path: Path, ts_keys: tuple[str, ...]) -> list[dict]:
    """Archives every complete not-yet-archived line of one live log. Returns the manifest
    entries of the chunks written this run (possibly none)."""
    if not path.exists():
        return []
    manifest = _load_manifest(label, path)
    offset = manifest["bytes_archived"]
    size = path.stat().st_size
    if size < offset:
        print(f"  ! {label}: live file is smaller than what's already archived "
              f"({_fmt_mb(size)} < {_fmt_mb(offset)}) -- was it reset? Leaving the archive "
              f"alone; delete data/archive/{label}/ to re-archive from scratch.")
        return []
    if size == offset:
        return []

    written = []
    chunk: _Chunk | None = None
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # partial line mid-append by the sim loop -- next run gets it
            rec = ts = None
            stripped = raw.strip()
            if stripped:
                try:
                    rec = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    rec = None  # keep the line (byte fidelity), just can't date/count it
            if isinstance(rec, dict):
                for key in ts_keys:
                    if isinstance(rec.get(key), (int, float)):
                        ts = rec[key]
                        break
            else:
                rec = None
            date = (time.strftime("%Y-%m-%d", time.localtime(ts)) if ts
                    else (chunk.date if chunk else time.strftime("%Y-%m-%d")))
            if chunk and (chunk.date != date or chunk.raw_bytes >= RAW_CHUNK_CAP):
                written.append(_flush_chunk(label, chunk, manifest))
                chunk = None
            if chunk is None:
                chunk = _Chunk(date)
            chunk.add(raw, rec, ts)
    if chunk and chunk.lines:
        written.append(_flush_chunk(label, chunk, manifest))
    return written


_TAIL_CHUNK_BYTES = 1 << 20  # 1 MB reverse-read step for compact_live_to_tail's tail walk


def _tail_cut_offset(path: Path, keep_lines: int) -> int:
    """Byte offset of the start of the last `keep_lines` lines of `path`, found by seeking
    backwards from EOF in chunks (never reads the whole file -- the point of this whole
    exercise). Returns a line boundary: 0 (keep everything, fewer lines than asked), or the
    offset just after a newline.

    Trailing-newline handling: to keep K lines we must find the newline that TERMINATES the line
    just BEFORE the block, and cut right after it. The file's own final newline terminates the
    last kept line, not a boundary between kept lines, so when the file ends in '\\n' we look for
    K+1 newlines rather than K. A file with no final newline (a partial last record still being
    appended) needs exactly K, and the partial line is correctly kept as the newest record."""
    size = path.stat().st_size
    if size == 0:
        return 0
    with open(path, "rb") as f:
        f.seek(size - 1)
        target = keep_lines + (1 if f.read(1) == b"\n" else 0)
        pos = size
        newlines = 0
        while pos > 0:
            step = min(_TAIL_CHUNK_BYTES, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            idx = len(chunk)
            while True:
                nl = chunk.rfind(b"\n", 0, idx)
                if nl == -1:
                    break
                newlines += 1
                idx = nl
                if newlines == target:
                    return pos + nl + 1  # first byte after this newline = start of kept block
    return 0  # walked to the start without hitting `target` newlines -> keep everything


def compact_live_to_tail(label: str, path: Path, ts_keys: tuple[str, ...],
                         keep_lines: int) -> dict | None:
    """Shrinks a giant live JSONL log to just its last `keep_lines` records, WITHOUT losing any
    history: every complete not-yet-archived line is flushed into immutable .jsonl.gz chunks
    first (via split_source), so the full audit trail survives in data/archive/<label>/ (the
    git-carried record). Only then is the live file rewritten to its tail.

    The manifest's bytes_archived offset is adjusted so a later split_all() neither re-archives
    the retained tail nor skips the un-archived remainder inside it:

        old file bytes [0 .. S), archived up to offset A (A = last complete-line boundary)
        keep the tail [C .. S)  -> new file is those (S - C) bytes
        of the new file, bytes [0 .. A-C) were already archived; [A-C .. end) were not
        so new bytes_archived = max(0, A - C)

    Because split_source archives every complete line, A is always at/after the complete-line
    boundary C, so A - C >= 0 and no double-archiving can occur. Returns a small summary dict
    (or None if nothing to do)."""
    if not path.exists():
        return None
    size_before = path.stat().st_size
    split_source(label, path, ts_keys)  # flush full history to chunks first -- never lose data
    manifest = _load_manifest(label, path)
    archived = manifest["bytes_archived"]
    cut = _tail_cut_offset(path, keep_lines)
    if cut <= 0:
        return None  # file already <= keep_lines lines; nothing to trim
    size = path.stat().st_size
    tmp = path.with_suffix(path.suffix + ".compact-tmp")
    kept = 0
    with open(path, "rb") as src, open(tmp, "wb") as out:
        src.seek(cut)
        while True:
            block = src.read(_TAIL_CHUNK_BYTES)
            if not block:
                break
            out.write(block)
            kept += len(block)
    os.replace(tmp, path)
    manifest["bytes_archived"] = max(0, archived - cut)
    _save_manifest(label, manifest)
    return {"label": label, "bytes_before": size_before, "bytes_after": kept,
            "lines_kept": keep_lines, "reclaimed": size - kept}


def split_all() -> None:
    total = 0
    for label, path, ts_keys in sources():
        for entry in split_source(label, path, ts_keys):
            total += 1
            print(f"  {label}/{entry['file']}: {entry['records']} records, "
                  f"{entry['unique_skins']} unique skins, "
                  f"{_fmt_mb(entry['raw_bytes'])} raw -> {_fmt_mb(entry['gz_bytes'])}")
    if total == 0:
        print("Nothing new to archive -- every complete line is already chunked.")
    else:
        print(f"\n{total} chunk(s) written under data/archive/. The live files were not "
              f"touched; commit data/archive/ to carry this data in git.")


def restore_all() -> None:
    """Rebuilds live log files from their chunks, e.g. on a fresh clone. Refuses to touch
    any live file that already has content -- restoring over real accumulated data is
    exactly the kind of silent loss this project goes out of its way to avoid."""
    for label, path, _ in sources():
        manifest_file = _manifest_path(label)
        if not manifest_file.exists():
            continue
        manifest = _load_manifest(label, path)
        if path.exists() and path.stat().st_size > 0:
            print(f"  {label}: live file already has data ({_fmt_mb(path.stat().st_size)})"
                  f" -- skipped, never overwritten.")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".restore-tmp")
        n = 0
        with open(tmp, "wb") as out:
            for entry in manifest["chunks"]:
                with gzip.open(ARCHIVE_DIR / label / entry["file"], "rb") as gz:
                    out.write(gz.read())
                n += entry["records"]
        os.replace(tmp, path)
        print(f"  {label}: restored {n} records from {len(manifest['chunks'])} chunk(s) "
              f"to {manifest['source']}")


def status() -> None:
    for label, path, _ in sources():
        manifest_file = _manifest_path(label)
        if not manifest_file.exists():
            print(f"  {label}: not archived yet")
            continue
        manifest = _load_manifest(label, path)
        chunks = manifest["chunks"]
        gz_total = sum(c["gz_bytes"] for c in chunks)
        dates = sorted({c["date"] for c in chunks})
        live = path.stat().st_size if path.exists() else 0
        pending = max(live - manifest["bytes_archived"], 0)
        print(f"  {label}: {len(chunks)} chunk(s), {manifest['lines_archived']} records, "
              f"{dates[0]} -> {dates[-1]}, {_fmt_mb(gz_total)} compressed"
              + (f", {_fmt_mb(pending)} of live file not yet archived" if pending else ""))


if __name__ == "__main__":
    split_all()
