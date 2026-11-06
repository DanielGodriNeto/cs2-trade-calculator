"""Self-check for sim speed changes:
  - sim_loop: a candidate scored in the round's pool reuses that score for its record (no second
    predict_expected_roi call); an unscored candidate (mutant) still predicts.
  - sim_learning.predict_expected_roi: a shared price_memo gives identical output with fewer
    get_cached_price calls.
  - sim_learning.train: two retrains of the same model_dir never overlap.

Run: python test_sim_speed.py
"""
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from src import sim_learning as sim_learning_mod
from src import sim_loop as sim_loop_mod
from src import steam_prices as steam_prices_mod


def check_scored_candidate_reuses_first_pass() -> None:
    scored = SimpleNamespace(name="scored")
    mutant = SimpleNamespace(name="mutant")
    first_pass = sim_loop_mod._FirstPassScores()
    first_pass.store(scored, 1.5)
    with mock.patch.object(sim_learning_mod, "predict_expected_roi", return_value=2.5) as predict:
        assert first_pass.has(scored)
        assert first_pass.get(scored) == 1.5
        assert not first_pass.has(mutant)
        assert sim_loop_mod._FirstPassScores().has(scored) is False
        # Same id() but a different object must not match the stored entry.
        impostor = SimpleNamespace(name="impostor")
        first_pass._by_id[id(impostor)] = (scored, 9.9)
        assert not first_pass.has(impostor)
        # Unscored candidate -> exactly one predict call, which is the fallback path.
        assert predict.call_count == 0
        fallback = predict(mutant, Path("m"), Path("s"), collection_roi_lookup={})
        assert fallback == 2.5 and predict.call_count == 1


def _fake_candidate() -> SimpleNamespace:
    units = [{"skin": {"name": "Skin A"}, "wear": "Field-Tested", "count": 5},
             {"skin": {"name": "Skin A"}, "wear": "Field-Tested", "count": 5}]
    outputs = [{"name": "Skin B", "wears": ["Field-Tested", "Minimal Wear"]}]
    leg = SimpleNamespace(collection="Test Collection", units=units, outputs=outputs)
    return SimpleNamespace(legs=[leg], from_rarity_name="Restricted", stattrak=False)


class _FakeModel:
    def predict(self, x):
        return np.full(len(x), 0.25)


def check_price_memo_same_output_fewer_lookups() -> None:
    candidate = _fake_candidate()
    tmp = Path(tempfile.mkdtemp())
    model_file = tmp / sim_learning_mod.MODEL_FILENAME
    model_file.write_bytes(b"x")
    mtime = model_file.stat().st_mtime
    sim_learning_mod._loaded_models[tmp] = (_FakeModel(), mtime)
    fake_status = {"trained": True}
    price = {"lowest_price": 1.0, "median_price": 2.0}

    def fake_cached(_mhn, _cur=1):
        fake_cached.calls += 1
        return price, "fresh"
    fake_cached.calls = 0

    common = dict(collection_roi_lookup={}, with_std=False)
    with mock.patch.object(sim_learning_mod, "get_model_status", return_value=fake_status), \
            mock.patch.object(steam_prices_mod, "get_cached_price", side_effect=fake_cached):
        no_memo = sim_learning_mod.predict_expected_roi(candidate, tmp, tmp / "s.jsonl", **common)
        calls_no_memo = fake_cached.calls
        fake_cached.calls = 0
        memo: dict = {}
        with_memo_1 = sim_learning_mod.predict_expected_roi(candidate, tmp, tmp / "s.jsonl",
                                                            price_memo=memo, **common)
        with_memo_2 = sim_learning_mod.predict_expected_roi(candidate, tmp, tmp / "s.jsonl",
                                                            price_memo=memo, **common)
        calls_memo = fake_cached.calls
    assert no_memo == with_memo_1 == with_memo_2 == 0.25, (no_memo, with_memo_1, with_memo_2)
    # 2 identical input units + 2 output wears -> 3 distinct keys even without a shared memo
    # (the per-call memo dedupes within one predict); a shared memo makes the 2nd predict free.
    assert calls_no_memo == 3, calls_no_memo
    assert calls_memo == 3, calls_memo


def check_train_serial_per_model_dir() -> None:
    def run_pair(dir_a: Path, dir_b: Path, hold: float) -> int:
        active = {"n": 0, "max": 0}
        guard = threading.Lock()
        sentinel = object()

        def fake_locked(model_dir, sims_file, t_start, t, phase, _mark):
            with guard:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(hold)
            with guard:
                active["n"] -= 1
            return sentinel

        with mock.patch.object(sim_learning_mod, "_train_locked", side_effect=fake_locked):
            threads = [threading.Thread(target=sim_learning_mod.train, args=(d, Path("s.jsonl")))
                       for d in (dir_a, dir_b)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        return active["max"]

    assert run_pair(Path("model_same"), Path("model_same"), 0.2) == 1, "same model_dir overlapped"
    assert run_pair(Path("model_x"), Path("model_y"), 0.3) == 2, "different models should still run in parallel"


if __name__ == "__main__":
    check_scored_candidate_reuses_first_pass()
    check_price_memo_same_output_fewer_lookups()
    check_train_serial_per_model_dir()
    print("ok")
