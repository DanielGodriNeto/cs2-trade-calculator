"""Self-check for CSFloat failure cooldown and USD->currency conversion in src.thirdparty_prices.

No network: urlopen, the Skinport/Buff163 bulk maps and the FX rate are all stubbed.

Run: python test_thirdparty_csfloat.py
"""
import json
import time
from urllib.error import URLError

from src import bulk_prices, thirdparty_prices as tp

tp.CSFLOAT_API_KEY = "test-key-not-real"
bulk_prices.get_skinport_map = lambda currency, abort_check=None: {}
bulk_prices.get_buff163_map = lambda currency, abort_check=None: {}

calls = {"n": 0, "fail": True}
rates = {"EUR": 0.5}


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def fake_urlopen(req, timeout=None, context=None):
    calls["n"] += 1
    if calls["fail"]:
        raise URLError("simulated outage")
    return _FakeResp(json.dumps([{"price": 1000}]).encode())


tp.urlopen = fake_urlopen
bulk_prices.rate_for = lambda currency: rates.get("EUR", 1.0) if currency == 3 else 1.0


def reset_state():
    calls["n"] = 0
    tp._csfloat_cache.clear()
    tp._stats["csfloat"].update(last_error=None, consecutive_failures=0)
    tp._stats["csfloat"].pop("last_failure_at", None)


# 1. Failure starts a cooldown: the second lookup for another skin is skipped, not re-requested,
#    not counted as a new failure, and nothing fake is cached.
reset_state()
assert tp.get_thirdparty_price("Skin A", "Factory New", False, 1)["csfloat"] is None
assert calls["n"] == 1
assert tp._stats["csfloat"]["consecutive_failures"] == 1
assert tp.get_thirdparty_price("Skin B", "Factory New", False, 1)["csfloat"] is None
assert calls["n"] == 1, "cooldown must skip the second HTTP call"
assert tp._stats["csfloat"]["consecutive_failures"] == 1, "skipped lookup must not count as failure"
assert not any("Skin B" in k[0] for k in tp._csfloat_cache), "skipped lookup must not cache"

# 2. After the cooldown passes, a live request is made again and its USD price is returned.
tp._stats["csfloat"]["last_failure_at"] = time.time() - tp.CSFLOAT_FAILURE_COOLDOWN_SECONDS - 1
calls["fail"] = False
assert tp.get_thirdparty_price("Skin B", "Factory New", False, 1)["csfloat"] == 10.0
assert calls["n"] == 2

# 3. Currency: USD value is converted by the shared rate; a missing rate yields None, not USD.
reset_state()
calls["fail"] = False
assert tp.get_thirdparty_price("Skin C", "Factory New", False, 3)["csfloat"] == 5.0
rates.pop("EUR")
assert tp.get_thirdparty_price("Skin D", "Factory New", False, 3)["csfloat"] is None

print("ok")
