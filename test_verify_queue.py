"""Self-check for src.verify_queue: duplicate input_mix units sum (not overwrite), an output
without a price (or <99% priced probability) yields no verified ROI, the verification log is
written atomically and a corrupt file is quarantined (OSError propagates), and the writeback
to a live sim_loop checkpoint runs under that son's state.lock. Temp dirs, mocked saves, no
network, never data/.

Run: python test_verify_queue.py
"""
import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src import sim_loop as sim_loop_mod
from src import verify_queue as vq

tmp_dir = Path(tempfile.mkdtemp())
vq.LOG_FILE = tmp_dir / "sons" / "verification_queue.json"


def price_row(role, skin, wear, consensus, prob=None):
    return SimpleNamespace(role=role, skin=skin, wear=wear, consensus_price=consensus,
                           probability_pct=prob, price_confidence="ok")


def expected_roi(revenue, cost):
    return (revenue * (1 - vq.STEAM_SELL_FEE) - cost) / cost * 100


# Item 6: duplicate (skin, wear) input_mix entries must SUM their counts (2 + 3 = 5 units).
# One input row per input_mix item, same consensus price -> cost 10 * 5 = 50.
dup_entry = {
    "input_mix": [{"skin": "A", "wear": "FT", "count": 2, "collection": "c", "unit_price": 10.0},
                  {"skin": "A", "wear": "FT", "count": 3, "collection": "c", "unit_price": 10.0}],
    "output_distribution": [{"skin": "O", "wear": "FT", "prob_pct": 100.0, "price": 200.0}],
}
dup_rows = [price_row("input", "A", "FT", 10.0), price_row("input", "A", "FT", 10.0),
            price_row("output", "O", "FT", 200.0, 100.0)]
roi, conf = vq._summarize(dup_entry, dup_rows)
assert roi is not None and abs(roi - expected_roi(200.0, 50.0)) < 1e-9, roi
assert conf == "ok"

# Item 7: an output with no price must not produce a verified ROI (old code priced only the rest).
partial_entry = {
    "input_mix": [{"skin": "A", "wear": "FT", "count": 1, "collection": "c", "unit_price": 10.0}],
    "output_distribution": [{"skin": "O", "wear": "FT", "prob_pct": 80.0, "price": 200.0},
                            {"skin": "P", "wear": "FT", "prob_pct": 20.0, "price": None}],
}
partial_rows = [price_row("input", "A", "FT", 10.0), price_row("output", "O", "FT", 200.0, 80.0)]
assert vq._summarize(partial_entry, partial_rows)[0] is None

# Item 7: all outputs priced but a priced output has no row (covered probability 95% < 99%).
gap_entry = {
    "input_mix": [{"skin": "A", "wear": "FT", "count": 1, "collection": "c", "unit_price": 10.0}],
    "output_distribution": [{"skin": "O", "wear": "FT", "prob_pct": 95.0, "price": 200.0},
                            {"skin": "P", "wear": "FT", "prob_pct": 5.0, "price": 50.0}],
}
gap_rows = [price_row("input", "A", "FT", 10.0), price_row("output", "O", "FT", 200.0, 95.0)]
assert vq._summarize(gap_entry, gap_rows)[0] is None

# Item 7 control: fully priced distribution still reports a number.
full_entry = {
    "input_mix": [{"skin": "A", "wear": "FT", "count": 1, "collection": "c", "unit_price": 10.0}],
    "output_distribution": [{"skin": "O", "wear": "FT", "prob_pct": 100.0, "price": 200.0}],
}
full_rows = [price_row("input", "A", "FT", 10.0), price_row("output", "O", "FT", 200.0, 100.0)]
assert vq._summarize(full_entry, full_rows)[0] is not None

# Web "Verify prices" regression: an entry with no output_distribution key still yields a number
# when its rows are priced; the same entry WITH a distribution containing an unpriced output yields None.
web_entry = {"input_mix": [{"skin": "A", "wear": "FT", "count": 1, "collection": "c", "unit_price": 10.0}]}
assert abs(vq._summarize(web_entry, full_rows)[0] - expected_roi(200.0, 10.0)) < 1e-9
assert vq._summarize(dict(web_entry, output_distribution=partial_entry["output_distribution"]), partial_rows)[0] is None

# Writeback of a None result must keep an earlier valid verification on the same entry.
keep_path = tmp_dir / "keep_checkpoint.json"
keep_state = SimpleNamespace(lock=threading.Lock(), checkpoint_file=keep_path,
                             checkpoint={"best": [{"id": "k1", "verified_roi_pct": 7.0, "price_confidence": "ok"}]})
sim_loop_mod._live_states[8] = keep_state
assert vq._writeback(8, "k1", None, "no data") is False
assert keep_state.checkpoint["best"][0]["verified_roi_pct"] == 7.0
assert keep_state.checkpoint["best"][0]["price_confidence"] == "ok"

# Consumer: an OSError from the log append is reported and the loop keeps running (returns on stop).
consumer_entry = {"id": "c1", "primary_collection": "c", "from_rarity": "r", "to_rarity": "r2",
                  "stattrak": False, "expected_roi_pct": 5.0, "input_mix": [], "output_distribution": []}
vq._queue.put(vq.QueueItem(son_id=1, entry=consumer_entry))
consumer_state = vq.VerifyQueueState()
append_calls = []


def failing_append(result):
    append_calls.append(result["id"])
    consumer_state.stop_event.set()  # the loop sees this on its next poll and returns
    raise OSError("disk full")


with mock.patch.object(vq.price_report_mod, "verify_simulation_record", return_value=[]), \
     mock.patch.object(vq, "summarize_and_writeback", return_value=(None, "no data")), \
     mock.patch.object(vq, "_append_log", side_effect=failing_append):
    vq.consumer_loop(consumer_state, poll_interval=0.01)
assert append_calls == ["c1"], append_calls
assert consumer_state.recent[0]["id"] == "c1"

# Item 13: corrupt log is quarantined (not overwritten), new log holds only the new result.
def log_result(i):
    return {"son_id": 0, "id": f"e{i}", "verified_roi_pct": float(i), "price_confidence": "ok",
            "verified_at": 0.0, "error": None}


vq.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
corrupt = '[{"id": "old"'
vq.LOG_FILE.write_text(corrupt, encoding="utf-8")
vq._append_log(log_result(1))
quarantined = list(vq.LOG_FILE.parent.glob("verification_queue.json.corrupt-*"))
assert len(quarantined) == 1, quarantined
assert quarantined[0].read_text(encoding="utf-8") == corrupt
assert json.loads(vq.LOG_FILE.read_text(encoding="utf-8")) == [log_result(1)]

# Valid history: new result first, nothing dropped; no temp leftovers from the atomic write.
vq._append_log(log_result(2))
assert [r["id"] for r in json.loads(vq.LOG_FILE.read_text(encoding="utf-8"))] == ["e2", "e1"]
assert not list(vq.LOG_FILE.parent.glob("*.tmp"))

# OSError on read propagates and the existing log is left untouched.
before = vq.LOG_FILE.read_text(encoding="utf-8")
try:
    with mock.patch.object(Path, "read_text", side_effect=OSError("disk gone")):
        vq._append_log(log_result(3))
    raise AssertionError("OSError on read was swallowed")
except OSError:
    pass
assert vq.LOG_FILE.read_text(encoding="utf-8") == before

# Item 14: writeback to a live son's checkpoint runs under that son's state.lock and mutates
# the in-memory copy (the one sim_loop re-saves every round).
live_path = tmp_dir / "live_checkpoint.json"
live_state = SimpleNamespace(lock=threading.Lock(), checkpoint_file=live_path,
                             checkpoint={"best": [{"id": "e1", "expected_roi_pct": 5.0}]})
sim_loop_mod._live_states[7] = live_state
lock_held_at_save = []
real_save = sim_loop_mod.save_checkpoint


def spy_save(path, cp):
    lock_held_at_save.append(live_state.lock.locked())
    real_save(path, cp)


with mock.patch.object(sim_loop_mod, "save_checkpoint", side_effect=spy_save):
    assert vq._writeback(7, "e1", 12.5, "ok") is True
assert lock_held_at_save == [True], lock_held_at_save
assert live_state.checkpoint["best"][0]["verified_roi_pct"] == 12.5
assert json.loads(live_path.read_text(encoding="utf-8"))["best"][0]["verified_roi_pct"] == 12.5
assert vq._writeback(7, "missing", 1.0, "ok") is False

# No live state (CLI process): falls back to the checkpoint file on disk.
disk_path = tmp_dir / "disk_checkpoint.json"
disk_path.write_text(json.dumps({"best": [{"id": "d1"}]}), encoding="utf-8")
with mock.patch.object(sim_loop_mod, "son_checkpoint_file", return_value=disk_path):
    assert vq._writeback(99, "d1", 3.0, "ok") is True
assert json.loads(disk_path.read_text(encoding="utf-8"))["best"][0]["verified_roi_pct"] == 3.0

print("ok")
