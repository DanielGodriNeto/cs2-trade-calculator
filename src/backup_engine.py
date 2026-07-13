"""Automated GitHub Backup Engine (delta-aware): an asynchronous, non-blocking background
worker that wakes every 6 hours, hashes the mutable telemetry files, and -- ONLY when
something actually changed -- wraps the accumulated data/ state into a structured archive via
the existing export-data pipeline (data_archive.export_archive, the same code behind the
dashboard's "Export data archive" button and the CLI's export-data command) and commits+pushes
it to a designated git repository.

Temporal delta check: before any disk-heavy or network work, SHA-256 each TARGET_FILE and
compare against the hashes recorded at the last successful backup. Unchanged -> log the
spec's clean "No telemetry mutations detected. Skipping remote push." line and sleep another
6 hours. That makes the idle cost of this worker three file reads per 6 hours.

Archive naming (audit registry schema, per spec):
    archive_YYYYMMDD_HHMMSS_[num_skins]skins_[num_outcomes]outcomes.zip
num_skins = distinct skins currently carrying price-history telemetry; num_outcomes = real
logged outcomes (outcomes.jsonl lines) -- both readable straight off the filename when
scanning the backup repo's history.

Git integration is CONFIGURED, never assumed: data/backup_config.json names the local clone
(repo_dir) + remote + branch. Until the user points it at a real git repository the worker
stays fully functional but idles in a clearly-reported "not configured" state (get_status()
explains exactly what to set up). Push failures (no remote, auth, offline) are logged and
retried on the next cycle; the commit still lands locally, so no archive is ever lost to a
transient network failure.

IMPORTANT: repo_dir must point at a DEDICATED backup repository, never at this project's own
source repo. data/ is already tracked directly by the source repo (plain diffable JSON/JSONL --
see the .gitignore's own comment on this), so wrapping the same data/ into a zip and committing
it to the SAME repo double-stores everything and bloats history with non-deduplicatable binary
blobs on every cycle. This is exactly what happened before 2026-07-12: repo_dir was misconfigured
to point at cs2-trade-calculator itself, growing .git to 1.3GB across ~31 archive commits before
being caught and fixed (repo_dir now points at a separate local repo outside this project tree
entirely, with no relation to the source repo's own history).

Everything is logged to data/backup_log.jsonl (append-only JSONL, same convention as every
other audit log here).
"""
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

from . import data_archive as data_archive_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_FILE = DATA_DIR / "backup_config.json"
STATE_FILE = DATA_DIR / "backup_state.json"
LOG_FILE = DATA_DIR / "backup_log.jsonl"

# The mutable telemetry files whose SHA-256 decides "did anything change" (per spec).
TARGET_FILES = ("outcomes.jsonl", "price_history.jsonl", "dead_skin_scores.json")

WAKE_INTERVAL_SECONDS = 6 * 3600
GIT_TIMEOUT_SECONDS = 120

NO_CHANGE_MESSAGE = "No telemetry mutations detected. Skipping remote push."

_lock = threading.Lock()


def _default_config() -> dict:
    return {"enabled": False, "repo_dir": None, "remote": "origin", "branch": "main"}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return _default_config()
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_config()
    out = _default_config()
    out.update({k: cfg.get(k, v) for k, v in out.items()})
    return out


def save_config(cfg: dict) -> dict:
    merged = load_config()
    for k in _default_config():
        if k in cfg:
            merged[k] = cfg[k]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        CONFIG_FILE.write_text(json.dumps(merged), encoding="utf-8")
    return merged


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_hashes": {}, "last_backup_at": None, "last_archive": None,
                "last_result": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_hashes": {}, "last_backup_at": None, "last_archive": None,
                "last_result": None}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _log(event: str, **fields) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, "at": time.time(), **fields}) + "\n")


def list_log(limit: int = 200) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    out = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out.reverse()
    return out[:limit]


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def current_hashes() -> dict:
    return {name: _sha256(DATA_DIR / name) for name in TARGET_FILES}


def _count_skins_and_outcomes() -> tuple[int, int]:
    """num_skins = distinct market names in price_history telemetry; num_outcomes = logged
    real outcomes. Cheap line scans, only run when a delta was already detected."""
    skins = set()
    ph = DATA_DIR / "price_history.jsonl"
    if ph.exists():
        with open(ph, encoding="utf-8") as f:
            for line in f:
                try:
                    skins.add(json.loads(line)["market_hash_name"])
                except (json.JSONDecodeError, KeyError):
                    continue
    n_outcomes = 0
    oc = DATA_DIR / "outcomes.jsonl"
    if oc.exists():
        with open(oc, encoding="utf-8") as f:
            n_outcomes = sum(1 for line in f if line.strip())
    return len(skins), n_outcomes


def _git(repo_dir: str, *args: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True,
                               timeout=GIT_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return False, "git executable not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"git {' '.join(args)} timed out"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def _repo_ready(cfg: dict) -> tuple[bool, str]:
    if not cfg.get("enabled"):
        return False, "backup engine disabled in data/backup_config.json"
    repo_dir = cfg.get("repo_dir")
    if not repo_dir:
        return False, ("no backup repository configured -- set repo_dir (a local clone of the "
                        "designated GitHub repo) via the dashboard's Backup panel or "
                        "data/backup_config.json")
    if not Path(repo_dir).is_dir():
        return False, f"repo_dir does not exist: {repo_dir}"
    ok, out = _git(repo_dir, "rev-parse", "--is-inside-work-tree")
    if not ok or out.strip() != "true":
        return False, f"repo_dir is not a git repository: {repo_dir} ({out})"
    return True, ""


def run_backup(force: bool = False) -> dict:
    """One full delta-check -> archive -> commit -> push cycle. force=True skips the delta
    check (the dashboard's Run now button). Returns a JSON-safe result dict; every outcome is
    also appended to the audit log."""
    cfg = load_config()
    state = _load_state()
    hashes = current_hashes()
    changed = [n for n in TARGET_FILES if hashes.get(n) != state["last_hashes"].get(n)]
    if not changed and not force:
        _log("no_change", message=NO_CHANGE_MESSAGE)
        result = {"ok": True, "action": "skipped", "message": NO_CHANGE_MESSAGE}
        state["last_result"] = result
        _save_state(state)
        return result

    ready, why = _repo_ready(cfg)
    if not ready:
        _log("not_configured", reason=why, changed_files=changed)
        result = {"ok": False, "action": "not_configured", "message": why,
                  "changed_files": changed}
        state["last_result"] = result
        _save_state(state)
        return result

    n_skins, n_outcomes = _count_skins_and_outcomes()
    ts = time.strftime("%Y%m%d_%H%M%S")
    archive_name = f"archive_{ts}_{n_skins}skins_{n_outcomes}outcomes.zip"
    repo_dir = cfg["repo_dir"]
    archive_path = Path(repo_dir) / archive_name
    try:
        # Split into <=100 MB parts (see data_archive.export_archive) -- GitHub hard-rejects
        # any single pushed file over 100 MB, and this worker pushes what it commits.
        part_paths = data_archive_mod.export_archive(archive_path)
    except (FileNotFoundError, OSError) as e:
        _log("archive_failed", error=str(e))
        result = {"ok": False, "action": "archive_failed", "message": str(e)}
        state["last_result"] = result
        _save_state(state)
        return result
    part_names = [p.name for p in part_paths]

    ok, out = _git(repo_dir, "add", *part_names)
    if ok:
        ok, out = _git(repo_dir, "commit", "-m",
                        f"Automated data backup {ts}: {n_skins} skins, {n_outcomes} outcomes "
                        f"({len(part_names)} part(s))")
    if not ok:
        _log("commit_failed", error=out, archive=part_names)
        result = {"ok": False, "action": "commit_failed", "message": out,
                  "archive": part_names}
        state["last_result"] = result
        _save_state(state)
        return result

    pushed, push_out = _git(repo_dir, "push", cfg.get("remote") or "origin",
                             cfg.get("branch") or "main")
    now = time.time()
    # The commit landed locally either way -- record the new baseline hashes so the same delta
    # isn't re-archived next cycle; a failed push is retried implicitly when the NEXT delta
    # pushes on top of it (git push sends all pending commits).
    state["last_hashes"] = hashes
    state["last_backup_at"] = now
    state["last_archive"] = part_names
    result = {"ok": pushed, "action": "backed_up" if pushed else "pushed_failed_committed_locally",
              "archive": part_names, "n_skins": n_skins, "n_outcomes": n_outcomes,
              "changed_files": changed, "push_output": push_out if not pushed else None}
    state["last_result"] = result
    _save_state(state)
    _log("backup" if pushed else "push_failed", archive=part_names, n_skins=n_skins,
         n_outcomes=n_outcomes, changed_files=changed,
         **({} if pushed else {"error": push_out}))
    return result


class BackupWorkerState:
    def __init__(self):
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.last_wake_at = None


def worker_loop(state: BackupWorkerState) -> None:
    """The 6-hour background worker -- non-blocking (its own daemon thread), delta-aware (see
    run_backup), and immune to individual cycle failures (logged + retried next wake)."""
    while not state.stop_event.is_set():
        state.last_wake_at = time.time()
        try:
            run_backup(force=False)
        except Exception as e:  # noqa: BLE001 -- a failing cycle must never kill the worker
            _log("worker_error", error=f"{type(e).__name__}: {e}")
        # Sleep 6h, but wake early on demand (Run now sets wake_event) or on shutdown.
        woke = state.wake_event.wait(timeout=WAKE_INTERVAL_SECONDS)
        if woke:
            state.wake_event.clear()


def get_status(worker: "BackupWorkerState | None" = None) -> dict:
    cfg = load_config()
    state = _load_state()
    ready, why = _repo_ready(cfg)
    return {"config": cfg, "configured": ready, "configuration_issue": None if ready else why,
            "targets": list(TARGET_FILES), "wake_interval_seconds": WAKE_INTERVAL_SECONDS,
            "last_backup_at": state.get("last_backup_at"),
            "last_archive": state.get("last_archive"),
            "last_result": state.get("last_result"),
            "last_wake_at": worker.last_wake_at if worker else None,
            "recent_log": list_log(10)}
