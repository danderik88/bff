"""Devices added from the app: a key per device, handed over by QR, link or 6-digit code.

Only the SHA-256 of a key is stored (state.db settings, key "devices"). Codes live in memory for 10 minutes.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlsplit

import segno

CODE_S = 600
MAX_FAILS = 10
PENDING = {}  # code -> (key, name, expires)
FAILS = []  # timestamps of wrong codes within the last CODE_S seconds
UNKNOWN = object()  # sentinel: the device cache is not loaded yet
CACHE = [None]  # in-memory {name: {"hash": ...}}: loaded from the db on first use
LOCK = threading.Lock()  # ponytail: one lock for codes and the devices row; a handful of devices


def _all(db):
    if CACHE[0] is None:
        row = db.execute("SELECT value FROM settings WHERE printer='*' AND key='devices'").fetchone()
        CACHE[0] = json.loads(row[0]) if row else {}
    return dict(CACHE[0])  # a copy: callers edit it, the cache changes only in _save


def _save(db, devs):
    db.execute("INSERT OR REPLACE INTO settings (printer, key, value) VALUES ('*', 'devices', ?)", (json.dumps(devs),))
    db.commit()
    CACHE[0] = devs


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def check(db, key):
    """Name of the stored device owning this key, or None."""
    h = _hash(key)
    found = None
    for name, dev in _all(db).items():
        if hmac.compare_digest(dev["hash"], h):
            found = name
    return found


def cached_check(key):
    """Device name for key using only the cache: None if absent, UNKNOWN if not loaded."""
    if CACHE[0] is None:
        return UNKNOWN
    h = _hash(key)
    for name, dev in list(CACHE[0].items()):
        if hmac.compare_digest(dev["hash"], h):
            return name
    return None


def add(db, name, builtin, origin, now=None):
    now = time.time() if now is None else now
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= 32 or not name.isprintable():
        raise ValueError("name: 1 to 32 printable characters")
    with LOCK:
        return _add(db, name, builtin, origin, now)


def _add(db, name, builtin, origin, now):
    devs = _all(db)
    if name.lower() in {n.lower() for n in devs}:
        raise ValueError(f"a device named {name} already exists")
    # Agent keys are invisible in the list: say where the name comes from, or the user sees "already exists" for nothing.
    if name.lower() in {n.lower() for n in builtin}:
        raise ValueError(f"{name} is already an agent key on the server: choose another name")
    url = urlsplit(origin or "")
    if url.scheme not in ("http", "https") or not url.netloc:
        raise ValueError("origin: http(s) address needed")
    key = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(10**6):06d}"
    devs[name] = {"hash": _hash(key), "created": int(now)}
    _save(db, devs)
    for old in [c for c, p in PENDING.items() if p[2] < now]:
        del PENDING[old]
    PENDING[code] = (key, name, now + CODE_S)
    link = f"{url.scheme}://{url.netloc}/app#p={code}"
    return {"name": name, "code": code, "link": link, "expires_in": CODE_S,
            "qr": segno.make(link, error="m").svg_data_uri(scale=6, border=2)}


def redeem(code, now=None):
    """Key for a valid code (same key every time within 10 minutes), else None."""
    now = time.time() if now is None else now
    with LOCK:
        hit = PENDING.get(code) if isinstance(code, str) else None
        if hit is None or hit[2] < now:
            FAILS[:] = [t for t in FAILS if t >= now - CODE_S]
            FAILS.append(now)
            if len(FAILS) >= MAX_FAILS:  # brute force guard: cancel every pending code
                PENDING.clear()
                FAILS.clear()
            return None
        FAILS.clear()
        return hit[0]


def listing(db, builtin, me=None):
    return ([{"name": n, "builtin": True, "me": n == me} for n in builtin]
            + [{"name": n, "builtin": False, "me": n == me} for n in _all(db)])


def remove(db, name, builtin):
    if name in builtin:
        raise ValueError("built-in keys are changed on the server, not here")
    with LOCK:
        devs = _all(db)
        if devs.pop(name, None) is None:
            raise ValueError(f"no device named {name}")
        _save(db, devs)
        for code in [c for c, p in PENDING.items() if p[1] == name]:
            del PENDING[code]


def cancel(name):
    """Remove PENDING codes for this device name."""
    with LOCK:
        for code in [c for c, p in PENDING.items() if p[1] == name]:
            del PENDING[code]
