"""Local, never-backed-up store for third-party connection secrets (CSFloat API key, NTFY
push-alert topic/server).

Deliberately NOT under data/: this project's data/ directory is committed to git on purpose
(see outcomes.py/price_history.py/learning.py docstrings) and backup_engine.py's worker zips
the ENTIRE data/ tree (data_archive.export_archive walks DATA_DIR.rglob("*") with no
exclusions) and pushes it to the user's configured GitHub backup repo every 6h. A secret
dropped anywhere under data/ would get auto-committed and pushed right along with it. This
file lives at the project root instead -- structurally outside that sweep -- and is listed in
.gitignore next to the project's other local-only files (SETUP_NOTES.md, run_watch.bat).

Same load/merge/write shape as backup_engine.py's own config (default-dict whitelist, merge
only whitelisted keys present in the POST body, own lock) -- deliberately not shared code with
backup_engine.py since the two configs have unrelated lifecycles and a different, disjoint
safety requirement (backup_engine's config is fine to back up; this one must never be).
"""
import json
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "connections.local.json"

_lock = threading.Lock()


def _default_config() -> dict:
    return {
        "csfloat_api_key": None,
        # Steam session token (steamLoginSecure). NOT an API key -- whoever holds it can act as
        # that Steam account on Steam's web endpoints, so it lives here for exactly the reason
        # this module exists (gitignored, outside data/, never in the backup archive) and is
        # only ever returned to the UI through masked_key().
        "steam_login_secure": None,
        "ntfy_topic": None,
        "ntfy_server": "https://ntfy.sh",
        "ntfy_enabled": False,
        "ntfy_roi_threshold_pct": 100.0,
        "ntfy_pattern_threshold_pct": 100.0,
    }


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
    with _lock:
        CONFIG_FILE.write_text(json.dumps(merged), encoding="utf-8")
    return merged


def masked_key(key: str | None) -> str | None:
    """Never echo a full secret back to the UI -- last 4 chars only, same convention as most
    API-key management UIs (GitHub, Stripe, ...)."""
    if not key:
        return None
    if len(key) <= 4:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def apply_to_runtime(cfg: dict | None = None) -> None:
    """Pushes the persisted config into every live module that reads these values -- called
    once at server startup and again after every successful save so changes take effect
    without a process restart. Import is local to avoid a hard dependency for callers (e.g.
    CLI commands) that never touch connections at all.

    `cfg` is only non-None when a caller just saved a config (the /api/connections/config
    handler passes the freshly-written dict) -- that's an explicit user action and should
    always win, including clearing a field back to empty. The implicit startup call (cfg=None)
    must NOT do that: alerts.py/thirdparty_prices.py/pattern_scanner.py are designed to pick up
    CSFLOAT_API_KEY/NTFY_TOPIC from the environment at import time, and connections.local.json
    defaults every key to None/empty when nothing has ever been saved through the dashboard.
    Blindly re-applying that empty file over an env-var-configured value at every startup wiped
    out the env var silently -- e.g. NTFY_TOPIC set per alerts.py's own setup instructions,
    then the dashboard started and instantly cleared it, so alerts looked "configured" but
    never sent."""
    explicit = cfg is not None
    cfg = cfg or load_config()
    from . import alerts as alerts_mod
    from . import pattern_scanner as pattern_scanner_mod
    from . import thirdparty_prices as thirdparty_prices_mod

    if explicit or cfg.get("csfloat_api_key"):
        thirdparty_prices_mod.configure_csfloat_key(cfg.get("csfloat_api_key"))
        pattern_scanner_mod.configure_csfloat_key(cfg.get("csfloat_api_key"))
    # Same env-var-vs-saved-config rule as the keys above: only clobber the value
    # pattern_scanner picked up from STEAM_LOGIN_SECURE_COOKIE at import time when the user
    # explicitly saved the form, never on the implicit startup pass.
    if explicit or cfg.get("steam_login_secure"):
        pattern_scanner_mod.configure_steam_cookie(cfg.get("steam_login_secure"))
    if explicit or cfg.get("ntfy_topic"):
        alerts_mod.configure(cfg.get("ntfy_topic"), cfg.get("ntfy_server"))
