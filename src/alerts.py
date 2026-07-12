"""Push notifications to a phone via ntfy.sh (https://ntfy.sh) -- free, no account, no API key.
Used by watch_loop.py to flag high-ROI trade-ups and cheap rare-pattern finds the moment a scan
turns them up, without the user having to keep the dashboard open.

Env-var gated exactly like CSFLOAT_API_KEY elsewhere in this project (thirdparty_prices.py,
pattern_scanner.py): unset NTFY_TOPIC = every send() call silently no-ops, never an error, so
nothing here ever blocks a scan/watch cycle just because notifications aren't configured yet.

Setup (one-time, no account needed):
  1. Install the "ntfy" app from the iOS App Store.
  2. Pick a private, hard-to-guess topic name (anyone who knows it can read your alerts --
     ntfy.sh topics are public by default). e.g. cs2-alerts-<random string>.
  3. In the app, subscribe to that topic on server ntfy.sh.
  4. Set the NTFY_TOPIC environment variable to the same name before running watch_loop.py.

Self-hosting is also supported (set NTFY_SERVER) if the free public server's reliability isn't
good enough later -- not needed to get started.
"""
import json
import os
import urllib.error
import urllib.request

from .net import SSL_CONTEXT

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # unset = notify() silently skipped, never an error

REQUEST_TIMEOUT_SECONDS = 10


def configured() -> bool:
    return bool(NTFY_TOPIC)


def configure(topic: str | None, server: str | None = None):
    """Live setter for NTFY_TOPIC/NTFY_SERVER, same pattern as thirdparty_prices's
    configure_csfloat_key() -- lets the dashboard's Connections settings page apply a saved
    topic/server without a process restart. An empty/falsy server falls back to the public
    default rather than clearing it, since ntfy.sh is the sane default for anyone who hasn't
    self-hosted."""
    global NTFY_TOPIC, NTFY_SERVER
    NTFY_TOPIC = topic or None
    NTFY_SERVER = server or "https://ntfy.sh"


def notify(title: str, message: str, *, priority: str = "default", tags: list[str] | None = None,
           click_url: str | None = None) -> bool:
    """Sends one push notification. priority: min|low|default|high|urgent (ntfy.sh convention).
    tags are ntfy's emoji shortcodes (e.g. "moneybag", "gem"), shown next to the title. Returns
    True on a confirmed 200 from the server, False on any failure (network, unconfigured, etc)
    -- callers should treat a False as "couldn't notify this time", never as fatal to the
    calling scan/watch cycle."""
    if not NTFY_TOPIC:
        return False
    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS, context=SSL_CONTEXT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def notify_json(payload: dict) -> bool:
    """Same as notify() but posts ntfy's JSON publish format instead of header-encoded fields --
    used when a field (e.g. a message with non-Latin1 characters) can't safely go in an HTTP
    header. payload must include at least {"topic": ..., "message": ...}; NTFY_TOPIC is filled
    in automatically if the caller omits "topic"."""
    if not NTFY_TOPIC:
        return False
    payload = {"topic": NTFY_TOPIC, **payload}
    url = f"{NTFY_SERVER.rstrip('/')}/"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS, context=SSL_CONTEXT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
