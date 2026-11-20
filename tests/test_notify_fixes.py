import json
import sqlite3
import unittest
from unittest import mock

from pygmy import notify


def memdb():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
    return db


class Gone(Exception):
    def __init__(self, code):
        super().__init__(f"push {code}")
        self.response = mock.Mock(status_code=code)


class NotifyFixes(unittest.TestCase):
    def test_subscribe_same_endpoint_updates_keys_and_label(self):
        db = memdb()
        sub = {"endpoint": "https://push/x",
               "keys": {"p256dh": "old", "auth": "oldauth"}}
        dev = notify.app_call(db, "push_subscribe", {"sub": sub, "label": "iPhone"})["id"]
        new = {"endpoint": "https://push/x",
               "keys": {"p256dh": "new", "auth": "newauth"}}
        again = notify.app_call(db, "push_subscribe", {"sub": new, "label": "iPad"})["id"]
        self.assertEqual(dev, again)
        subs = notify.get(db, "push_subs")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["id"], dev)
        self.assertEqual(subs[0]["label"], "iPad")
        self.assertEqual(subs[0]["sub"]["keys"], {"p256dh": "new", "auth": "newauth"})

    def test_malformed_entry_does_not_raise(self):
        db = memdb()
        notify.put(db, "push_subs", [{}, {"id": "a", "sub": {"endpoint": "https://push/a"}}])
        with mock.patch.object(notify, "webpush", lambda **_kw: None), \
             mock.patch.object(notify, "WebPushException", Gone):
            self.assertEqual(notify.send_push(db, "Delta", "done", "/app"), 1)
        view = notify.public_view(db)
        self.assertEqual(view["devices"], [{"id": "a", "label": "Device"}])

    def test_prune_keeps_a_device_added_meanwhile(self):
        db = memdb()
        notify.put(db, "push_subs", [{"id": "a", "label": "Laptop", "sub": {"endpoint": "https://push/a"}}])

        def fake(subscription_info, **_kw):
            if subscription_info["endpoint"].endswith("a"):
                notify.app_call(db, "push_subscribe", {"sub": {"endpoint": "https://push/new",
                                                               "keys": {"p256dh": "k", "auth": "a"}},
                                                        "label": "iPad"})
                raise Gone(410)
        with mock.patch.object(notify, "webpush", fake), mock.patch.object(notify, "WebPushException", Gone):
            notify.send_push(db, "Delta", "done", "/app")
        self.assertEqual([s["label"] for s in notify.get(db, "push_subs")], ["iPad"])
