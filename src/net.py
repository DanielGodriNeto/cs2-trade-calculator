"""Shared TLS context for every live HTTPS fetch in this project.

Python 3.13+ turned on VERIFY_X509_STRICT by default in ssl.create_default_context().
HTTPS-scanning antivirus proxies re-sign every site with a locally generated CA cert, and
those certs commonly fail the new strict extension checks -- verified live on this project's
own machine (2026-07-10): Avast Web Shield's "Avast Web/Mail Shield Root" cert fails with
"Basic Constraints of CA cert not marked critical", killing 100% of stdlib urlopen() calls
while browsers (which don't apply the strict flag) work fine. steam_prices.py's old docstring
blamed an intermittent Steam CDN edge-node cert -- that was this, and it isn't intermittent.

Clearing the strict flag keeps full chain and hostname verification -- it only drops the
newer nit-picking extension checks, restoring the exact validation behavior of Python <=3.12.
"""
import ssl


def make_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


SSL_CONTEXT = make_context()
