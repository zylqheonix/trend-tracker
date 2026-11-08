"""Tiny HTTP helper: JSON/text GET with retries and backoff. Stdlib only."""
import json
import os
import platform
import ssl
import subprocess
import time
import urllib.error
import urllib.request

UA = "trend-tracker/1.0 (+personal research script)"

_CTX = None
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAC_KEYCHAIN = "/System/Library/Keychains/SystemRootCertificates.keychain"


def ssl_context():
    """SSL context that works on a stock python.org macOS install.

    That installer ships without root certificates (CERTIFICATE_VERIFY_FAILED).
    Order: certifi if installed -> system defaults -> on macOS, export Apple's
    built-in root certs once to data/macos-roots.pem and use those.
    """
    global _CTX
    if _CTX is not None:
        return _CTX
    try:
        import certifi
        _CTX = ssl.create_default_context(cafile=certifi.where())
        return _CTX
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    if platform.system() == "Darwin" and not ctx.get_ca_certs():
        pem = os.path.join(_ROOT, "data", "macos-roots.pem")
        if not os.path.exists(pem):
            try:
                os.makedirs(os.path.dirname(pem), exist_ok=True)
                out = subprocess.run(["security", "find-certificate", "-a", "-p", _MAC_KEYCHAIN],
                                     capture_output=True, check=True).stdout
                with open(pem, "wb") as f:
                    f.write(out)
            except Exception as e:
                print(f"  ! couldn't export macOS root certs ({e}); "
                      "run: pip3 install certifi")
        if os.path.exists(pem):
            ctx.load_verify_locations(pem)
    _CTX = ctx
    return ctx


def get(url, *, retries=4, timeout=20, as_json=True, headers=None):
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if as_json else body
        except urllib.error.HTTPError as e:
            last_err = e
            # 429 / 5xx: back off and retry (honour Retry-After). 4xx otherwise: give up.
            if e.code == 429 or e.code >= 500:
                wait = 2 ** attempt * 3
                ra = e.headers.get("Retry-After") if e.headers else None
                if ra and ra.isdigit():
                    wait = max(wait, min(int(ra), 60))
                time.sleep(wait)
                continue
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET failed: {url} ({last_err})")


def try_get(url, quiet=False, **kw):
    """Like get(), but returns None instead of raising. Logs the failure unless quiet."""
    try:
        return get(url, **kw)
    except RuntimeError as e:
        if not quiet:
            print(f"  ! {e}")
        return None
