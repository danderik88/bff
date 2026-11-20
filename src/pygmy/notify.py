"""BFF's own notifications: which alerts to send, and to which channels (Web Push, Telegram).
Home Assistant keeps its own path in ha_mqtt (the Alert event)."""
import base64
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request

try:
    from .ha_mqtt import TEXT, events, flat_state
except ImportError:  # server.py runs with src/pygmy on sys.path
    from ha_mqtt import TEXT, events, flat_state

try:
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
except ImportError:  # ponytail: Web Push off until `pip install -r requirements.txt`; Telegram and HA still work
    Vapid02 = webpush = None
    WebPushException = Exception

LOW_SPOOL = 10      # % left on an RFID spool
OFFLINE_S = 120     # unreachable this long before "offline"
PRINTING = ("RUNNING", "PAUSE")
# The event kind becomes the notification title's tail: "<printer>: print failed" / "<printer>: error".
KIND_TITLE = {"failed": "print failed", "attention": "error"}
# ponytail: one lock for all notify settings rows
SETTINGS_LOCK = threading.RLock()


def _low_slots(state):
    return {slot: info for slot, info in (state.get("ams") or {}).items()
            if isinstance(info, dict) and isinstance(info.get("remain"), int) and info["remain"] <= LOW_SPOOL}


def alerts(prev, cur, lang="en", minutes=None, warned=None):
    """(kind, message) for every channel. warned: slots already warned this print, owned by the caller."""
    warned = set() if warned is None else warned
    printing = str(cur.get("state") or "").upper() in PRINTING
    if not printing:
        warned.clear()
    if not prev:
        warned.update(_low_slots(cur) if printing else ())  # after a restart: no reminder burst
        return []
    out = [("attention" if kind == "error" else kind, msg)
           for kind, msg in events(prev, cur, lang, minutes)]  # spaghetti too (the user 2026-11-15)
    if printing:
        t, before = TEXT.get(lang, TEXT["en"]), prev.get("ams") or {}
        for slot, info in _low_slots(cur).items():
            was = (before.get(slot) or {}).get("remain")
            # Only a spool in use: active now, or its % just went down.
            if slot in warned or not (info.get("active") or (isinstance(was, int) and was > info["remain"])):
                continue
            warned.add(slot)
            name = info.get("name") or info.get("material")
            out.append(("low_spool", t["low_spool"].format(slot=slot, remain=info["remain"],
                                                           name=f" · {name}" if name else "")))
    return out


class Presence:
    """offline once after OFFLINE_S unreachable, online once when it comes back after that."""

    def __init__(self):
        self.since, self.told, self.seen = None, False, False

    def update(self, connected, now):
        if not self.seen:  # already offline when BFF starts: not news, and neither is its return
            self.seen, self.told = True, "quiet" if not connected else False
        if connected:
            self.since = None
            told, self.told = self.told, False
            return "online" if told is True else None
        if self.since is None:
            self.since = now
        if not self.told and now - self.since > OFFLINE_S:
            self.told = True
            return "offline"
        return None


# Settings in state.db's settings table, printer "*": channels, telegram, vapid, push_subs.
def get(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE printer='*' AND key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings (printer, key, value) VALUES ('*', ?, ?)", (key, json.dumps(value)))
    db.commit()


def channels(db):
    return {"push": True, "telegram": True, **{k: v for k, v in (get(db, "channels") or {}).items() if k in ("push", "telegram")}}


def _vapid(db):
    keys = get(db, "vapid")
    if not keys:
        with SETTINGS_LOCK:
            keys = get(db, "vapid")
            if not keys:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric import ec
                key = ec.generate_private_key(ec.SECP256R1())
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
                point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
                keys = {"private_pem": pem, "public_b64": base64.urlsafe_b64encode(point).rstrip(b"=").decode()}
                put(db, "vapid", keys)
    return keys


def vapid_public(db):
    return _vapid(db)["public_b64"]


def send_push(db, title, body, url, image=None):
    """Deliver to every registered device; drop the ones the push service says are gone. Returns how many got it.
    image: a URL the phone opens without a token (the /photo/ ids are unguessable and short-lived)."""
    subs = get(db, "push_subs") or []
    if not subs or webpush is None:
        return 0
    vapid = Vapid02.from_pem(_vapid(db)["private_pem"].encode()) if Vapid02 else _vapid(db)["private_pem"]
    payload = {"title": title, "body": body, "url": url}
    if image:
        payload["image"] = image
    data = json.dumps(payload)
    # Apple refuses a localhost subject; BFF_PUSH_CONTACT lets an install name its own.
    claims = {"sub": os.environ.get("BFF_PUSH_CONTACT") or "mailto:bff@example.com"}
    sent, gone = 0, set()
    for entry in subs:
        if not isinstance(entry, dict) or not entry.get("id") or not isinstance(entry.get("sub"), dict):
            continue
        for attempt in (1, 2):  # one retry, then give up on this alert for this device
            try:
                webpush(subscription_info=entry["sub"], data=data, vapid_private_key=vapid,
                        vapid_claims=dict(claims), ttl=3600, timeout=10)
                sent += 1
                break
            except Exception as err:  # WebPushException, and network errors from requests
                code = getattr(getattr(err, "response", None), "status_code", None)
                if code in (404, 410):
                    gone.add(entry["id"])
                    break
                if attempt == 2:
                    print(f"notify: push to {entry.get('label')} failed ({code or type(err).__name__})", file=sys.stderr)
    if gone:  # fresh read under the lock: a device added or removed meanwhile stays as it is
        with SETTINGS_LOCK:
            put(db, "push_subs", [s for s in get(db, "push_subs") or []
                                   if not (isinstance(s, dict) and s.get("id") in gone)])
    return sent


def _field(boundary, name, value, filename=None):
    """One multipart/form-data part."""
    disp = f'name="{name}"' + (f'; filename="{filename}"' if filename else "")
    return (f"--{boundary}\r\nContent-Disposition: form-data; {disp}\r\n".encode()
            + (b"Content-Type: image/jpeg\r\n" if filename else b"")
            + b"\r\n" + value + b"\r\n")


def send_telegram(db, text, photo=None):
    """The text, with the print's last frame as a photo when there is one; any photo trouble falls back to text."""
    conf = get(db, "telegram") or {}
    if not conf.get("token") or not conf.get("chat"):
        return False
    api = f"https://api.telegram.org/bot{conf['token']}"
    if photo:
        boundary = f"----bff{secrets.token_hex(8)}"
        body = (_field(boundary, "chat_id", str(conf["chat"]).encode())
                + _field(boundary, "caption", text.encode())
                + _field(boundary, "photo", photo, "photo.jpg")
                + f"--{boundary}--\r\n".encode())
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{api}/sendPhoto", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=20).close()
            return True
        except Exception as err:  # the error text can carry the URL, so the token: never print it
            print(f"notify: telegram photo failed ({type(err).__name__})", file=sys.stderr)
    req = urllib.request.Request(f"{api}/sendMessage",
                                 data=json.dumps({"chat_id": conf["chat"], "text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as err:  # the error text can carry the URL, so the token: never print it
        print(f"notify: telegram failed ({type(err).__name__})", file=sys.stderr)
        return False


def public_view(db):
    """What the app may see: no token, no endpoints, no private key."""
    tg = get(db, "telegram") or {}
    token = tg.get("token") or ""
    return {"channels": channels(db), "vapid_public": vapid_public(db),
            "telegram": {"token": "••••" + token[-4:] if token else "", "chat": tg.get("chat") or ""},
            "devices": [{"id": s["id"], "label": s.get("label") or "Device"}
                        for s in get(db, "push_subs") or []
                        if isinstance(s, dict) and s.get("id") and isinstance(s.get("sub"), dict)]}


def new_id():
    return secrets.token_hex(4)


def app_call(db, name, args):
    """The app's Options → Notifications calls. Raises ValueError on bad input."""
    if name == "notify_get":
        return public_view(db)
    if name == "notify_set":
        if isinstance(args.get("channels"), dict):
            change = {k: bool(v) for k, v in args["channels"].items() if k in ("push", "telegram")}
            with SETTINGS_LOCK:
                put(db, "channels", {**(get(db, "channels") or {}), **change})
        if isinstance(args.get("telegram"), dict):
            new, old = args["telegram"], get(db, "telegram") or {}
            with SETTINGS_LOCK:
                put(db, "telegram", {"token": str(new.get("token") or "").strip() or old.get("token", ""),
                                     "chat": str(new.get("chat", old.get("chat", ""))).strip()})
        return public_view(db)
    if name == "push_subscribe":
        sub = args.get("sub") if isinstance(args.get("sub"), dict) else {}
        keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
        endpoint = str(sub.get("endpoint") or "")
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("not a push subscription")
        new_keys = {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])}
        with SETTINGS_LOCK:
            subs = get(db, "push_subs") or []
            for entry in subs:
                if isinstance(entry, dict) and entry.get("sub", {}).get("endpoint") == endpoint:
                    entry["sub"]["keys"] = new_keys
                    entry["label"] = str(args.get("label") or "Device")[:40]
                    put(db, "push_subs", subs)
                    return {"id": entry["id"]}
            entry = {"id": new_id(), "label": str(args.get("label") or "Device")[:40],
                     "sub": {"endpoint": endpoint, "keys": new_keys}}
            put(db, "push_subs", subs + [entry])
            return {"id": entry["id"]}
    if name == "push_remove":
        with SETTINGS_LOCK:
            put(db, "push_subs", [s for s in get(db, "push_subs") or [] if s.get("id") != args.get("id")])
        return public_view(db)
    if name == "notify_test":
        if args.get("channel") == "push":
            n = send_push(db, "Pygmy", "Test notification: it works.", "/app")
            return {"ok": n > 0, "detail": f"Sent to {n} device(s)" if n else "No device got it: tap Enable on this device first"}
        if args.get("channel") == "telegram":
            ok = send_telegram(db, "Pygmy: test notification, it works.")
            return {"ok": ok, "detail": "Sent" if ok else "Telegram is not set up, or it refused the token or chat id"}
        raise ValueError("unknown channel")
    raise ValueError(f"unknown call {name}")


def dispatch(db, title, message, url, photo=None, photo_url=None):
    """Every enabled channel. Home Assistant is not one: it has every entity, the Alert event included.
    photo: the print's last frame; photo_url turns it into a link for the push image."""
    on = channels(db)
    image = None
    if photo and photo_url:
        try:
            image = photo_url(photo)
        except Exception as err:  # a photo is a bonus: the notification still goes
            print(f"notify: photo {type(err).__name__}", file=sys.stderr)
    if on["push"]:
        try:
            send_push(db, title, message, url, image)
        except Exception as err:  # never let one channel stop the other
            print(f"notify: push {type(err).__name__}", file=sys.stderr)
    if on["telegram"] and get(db, "telegram") and not send_telegram(db, f"{title}: {message}", photo):
        send_telegram(db, f"{title}: {message}", photo)  # one retry


class Notifier:
    """Polls every printer like the HA bridge does, and runs without HA."""
    POLL_S = 5

    def __init__(self, db_path, aliases, read_status, names, lang="en", base_url="", photo=None, photo_url=None):
        self.db_path, self.aliases, self.read_status, self.names = db_path, aliases, read_status, names
        self.lang, self.base_url = lang, base_url
        self.photo, self.photo_url = photo, photo_url  # alias -> JPEG, and JPEG -> URL for the push image
        self.background = True
        self.last, self.presence, self.warned, self.started = {}, {}, {}, {}

    def _send(self, title, message, url, photo=None):
        def run():
            db = sqlite3.connect(self.db_path)
            try:
                dispatch(db, title, message, url, photo, self.photo_url)
            except Exception as err:
                print(f"notify: {type(err).__name__}", file=sys.stderr)
            finally:
                db.close()
        if self.background:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()

    def step(self, now):
        out = []
        aliases = list(self.aliases())
        for gone in set(self.last) - set(aliases):
            for book in (self.last, self.presence, self.warned, self.started):
                book.pop(gone, None)
        for alias in aliases:
            try:
                cur = flat_state(self.read_status(alias))
            except Exception as err:  # An unreadable status is not a disconnection: keep the last one.
                print(f"notify: {alias}: {type(err).__name__}", file=sys.stderr)
                continue
            presence = self.presence.setdefault(alias, Presence())
            found = []
            change = presence.update(bool(cur and cur.get("connected")), now)
            if change:
                found.append((change, TEXT.get(self.lang, TEXT["en"])[change]))
            if cur is not None:
                prev = self.last.get(alias)
                running = str(cur.get("state") or "").upper() in PRINTING
                was = str((prev or {}).get("state") or "").upper()
                # Only a start seen happening: after a restart the real start is unknown, so no "in N min".
                if prev is not None and running and was not in PRINTING:
                    self.started[alias] = now
                minutes = round((now - self.started[alias]) / 60) if alias in self.started else None
                found += alerts(prev, cur, self.lang, minutes, self.warned.setdefault(alias, set()))
                if not running:
                    self.started.pop(alias, None)
                self.last[alias] = cur
            title = self.names(alias) or alias.upper()
            for kind, message in found:
                photo = None
                if kind in ("finished", "failed") and self.photo is not None:
                    try:
                        photo = self.photo(alias)
                    except Exception as err:  # the frame is a bonus, never a reason to skip the alert
                        print(f"notify: foto {alias}: {type(err).__name__}", file=sys.stderr)
                self._send(f"{title}: {KIND_TITLE[kind]}" if kind in KIND_TITLE else title, message,
                           f"{self.base_url}/app?printer={alias}", photo)
                out.append((alias, kind, message))
        return out

    def start(self):
        def loop():
            while True:
                try:
                    self.step(time.monotonic())
                except Exception as err:
                    print(f"notify: {type(err).__name__}: {err}", file=sys.stderr)
                time.sleep(self.POLL_S)
        threading.Thread(target=loop, daemon=True).start()
