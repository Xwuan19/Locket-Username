"""Proxy pool for outgoing Locket API calls.

Round-robin picks an enabled proxy from the `proxies` table and returns it
in the dict shape `requests` expects. The pool is shared by Auth (Firebase
verifyPassword) and LocketAPI (Locket + RevenueCat) so all upstream calls
flow through the same egress IP per attempt.

Master switch lives in `site_settings` (key=`proxy_master`) → admin can
disable the whole pool with one click; the helpers fall through to direct
connection in that case.

Shorthand parser accepts both:
    http://user:pass@host:port
    user:pass:host:port
    host:port
"""

import threading
import time
from itertools import cycle

from . import db


_lock = threading.Lock()
_iter = None  # itertools.cycle, rebuilt on add/remove/enable


def _build_iter():
    global _iter
    rows = list_enabled()
    _iter = cycle(rows) if rows else None


def _normalize_url(raw):
    s = (raw or "").strip()
    if not s:
        return None
    if "://" in s:
        return s
    parts = s.split(":")
    if len(parts) == 2:
        # host:port
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) >= 4:
        # user:pass:host:port  (extras after port are appended to password)
        host, port = parts[-2], parts[-1]
        user = parts[0]
        pwd = ":".join(parts[1:-2])
        return f"http://{user}:{pwd}@{host}:{port}"
    return None


def parse_lines(text):
    """Return [normalized_url, ...] from a multi-line paste, skipping blanks
    and comments."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url = _normalize_url(line)
        if url:
            out.append(url)
    return out


def list_all():
    client = db.get_client()
    response = client.table("proxies").select("*").order("id").execute()
    return response.data if response.data else []


def list_enabled():
    client = db.get_client()
    response = client.table("proxies").select("id, url").eq("enabled", True).order("id").execute()
    return response.data if response.data else []


def add(raw_url):
    url = _normalize_url(raw_url)
    if not url:
        raise ValueError("Invalid proxy URL")
    client = db.get_client()
    from datetime import datetime
    client.table("proxies").insert({
        "url": url,
        "enabled": True,
        "added_at": datetime.utcnow().isoformat()
    }).execute()
    with _lock:
        _build_iter()


def add_many(raw_text):
    urls = parse_lines(raw_text)
    inserted = 0
    client = db.get_client()
    from datetime import datetime
    for url in urls:
        try:
            client.table("proxies").insert({
                "url": url,
                "enabled": True,
                "added_at": datetime.utcnow().isoformat()
            }).execute()
            inserted += 1
        except Exception as e:
            # Skip duplicates
            print(f"proxies: skipped duplicate {url}: {e}")
    if inserted:
        with _lock:
            _build_iter()
    return inserted


def remove(proxy_id):
    client = db.get_client()
    client.table("proxies").delete().eq("id", int(proxy_id)).execute()
    with _lock:
        _build_iter()


def set_enabled(proxy_id, enabled):
    client = db.get_client()
    client.table("proxies").update({"enabled": bool(enabled)}).eq("id", int(proxy_id)).execute()
    with _lock:
        _build_iter()


def mark_ok(proxy_id):
    client = db.get_client()
    from datetime import datetime
    client.table("proxies").update({
        "last_ok_at": datetime.utcnow().isoformat(),
        "last_err": None
    }).eq("id", int(proxy_id)).execute()


def mark_err(proxy_id, err):
    client = db.get_client()
    from datetime import datetime
    client.table("proxies").update({
        "last_err_at": datetime.utcnow().isoformat(),
        "last_err": str(err)[:240]
    }).eq("id", int(proxy_id)).execute()


# ---- master switch ----

_MASTER_KEY = "proxy_master"


def is_master_on():
    client = db.get_client()
    response = client.table("site_settings").select("value").eq("key", _MASTER_KEY).maybe_single().execute()
    if not response or not response.data:
        return False
    import json as _json
    try:
        value = response.data.get("value")
        if not value:
            return False
        if isinstance(value, str):
            value = _json.loads(value)
        return bool(value.get("enabled"))
    except (ValueError, TypeError, KeyError):
        return False


def set_master(enabled):
    import json as _json
    from datetime import datetime
    client = db.get_client()
    client.table("site_settings").upsert({
        "key": _MASTER_KEY,
        "value": {"enabled": bool(enabled)},
        "updated_at": datetime.utcnow().isoformat()
    }).execute()


# ---- runtime accessors used by HTTP layer ----

def next_proxy():
    """Return (id, requests_proxies_dict) or (None, None) when disabled/empty."""
    if not is_master_on():
        return None, None
    with _lock:
        global _iter
        if _iter is None:
            _build_iter()
        if _iter is None:
            return None, None
        try:
            entry = next(_iter)
        except StopIteration:
            return None, None
    url = entry["url"]
    return entry["id"], {"http": url, "https": url}
