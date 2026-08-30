"""P-3 measurement: how often does market_reliability rebuild the price_history.jsonl index?

Measure-first tool (no production change). Simulates a busy stretch -- 200 distinct
compute_state() calls interleaved with 20 record_snapshot() appends -- and counts how many
of the underlying _history_index() calls did a full file re-parse (rebuild) vs returned the
cached index (fast-path throttle hit or unchanged-mtime hit).

Counting method: _history_index() only ever assigns a brand-new dict to
_history_index_cache["index"] on the full-reparse branch (src/market_reliability.py:137);
the fast-path (:114) and unchanged-mtime (:122) branches return the SAME object. So a
rebuild happened this call iff `_history_index_cache["index"] is not` the object it was
before the call. We hold a real reference across the call so a freed dict's address can't
be recycled into a false negative. On each detected rebuild we add the temp file's current
byte size to a running total (that's what got parsed from byte 0).

The real data/price_history.jsonl is tracked and record_snapshot() appends to it, so we run
the whole thing against a tempfile copy: point price_history.HISTORY_FILE at the copy (the
same module attribute market_reliability reads at call time) and never touch the real file.
"""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))  # repo root, so `from src import` works

import os
import shutil
import tempfile

from src import data
from src import market_reliability as mr
from src import price_history as price_history_mod

REAL_HISTORY_FILE = price_history_mod.HISTORY_FILE
EMPTY_CACHE = {"mtime": None, "index": {}, "checked_at": 0.0}

_counters = {"rebuilds": 0, "hits": 0, "parse_bytes": 0}
_orig_history_index = mr._history_index


def _counting_history_index():
    before = mr._history_index_cache["index"]  # real ref: keeps the object alive across the call
    result = _orig_history_index()
    after = mr._history_index_cache["index"]
    if after is not before:
        _counters["rebuilds"] += 1
        _counters["parse_bytes"] += price_history_mod.HISTORY_FILE.stat().st_size
    else:
        _counters["hits"] += 1
    return result


def _reset_cache():
    mr._history_index_cache = dict(EMPTY_CACHE)


def _file_stats(path):
    lines = sum(1 for _ in open(path, encoding="utf-8"))
    mb = path.stat().st_size / 1024 / 1024
    return lines, mb


def main():
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="price_history_measure_", suffix=".jsonl")
    os.close(tmp_fd)
    tmp_path = pathlib.Path(tmp_name)
    shutil.copyfile(REAL_HISTORY_FILE, tmp_path)

    price_history_mod.HISTORY_FILE = tmp_path
    _reset_cache()
    mr._history_index = _counting_history_index

    try:
        names, seen = [], set()
        for s in data.load_skins():
            n = s["name"]
            if n not in seen:
                seen.add(n)
                names.append(n)
            if len(names) == 200:
                break
        assert len(names) == 200, f"only {len(names)} distinct skin names available"

        snap = {"lowest_price": 1.0, "median_price": 1.0, "volume": 1}
        appends = 0
        t0 = time.time()
        for i, n in enumerate(names, 1):
            mr.compute_state(f"{n} (Field-Tested)", 1)
            if i % 10 == 0:
                price_history_mod.record_snapshot(f"{n} (Field-Tested)", 1, snap)
                appends += 1
        workload_s = time.time() - t0

        total_mb = _counters["parse_bytes"] / 1024 / 1024
        print(f"rebuilds={_counters['rebuilds']}  "
              f"fastpath_or_mtime_hits={_counters['hits']}  "
              f"total_parse_MB={total_mb:.1f}  "
              f"over <200 compute_state + {appends} appends>  "
              f"(workload wall time {workload_s:.2f}s)")

        # Standalone: one cold rebuild, timed.
        _reset_cache()
        t0 = time.time()
        _orig_history_index()
        cold_s = time.time() - t0
        lines, mb = _file_stats(tmp_path)
        print(f"single_cold_rebuild={cold_s:.2f}s  ({lines} lines, {mb:.1f} MB)")
    finally:
        price_history_mod.HISTORY_FILE = REAL_HISTORY_FILE
        mr._history_index = _orig_history_index
        try:
            tmp_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
