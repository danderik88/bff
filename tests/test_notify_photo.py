import json
import sqlite3
import tempfile
import unittest
from unittest import mock

from pygmy import notify


def memdb():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
    return db


class Resp:
    def close(self):
        pass


class TelegramPhoto(unittest.TestCase):
    def db(self):
        db = memdb()
        notify.put(db, "telegram", {"token": "1:x", "chat": "2"})
        return db

    def test_photo_goes_as_sendphoto(self):
        seen = []

        def fake(req, timeout=0):
            seen.append(req)
            return Resp()
        with mock.patch.object(notify.urllib.request, "urlopen", fake):
            self.assertTrue(notify.send_telegram(self.db(), "✅ cube finished.", b"\xff\xd8jpeg"))
        self.assertEqual([r.full_url.rsplit("/", 1)[1] for r in seen], ["sendPhoto"])
        self.assertTrue(seen[0].get_header("Content-type").startswith("multipart/form-data; boundary="))
        body = seen[0].data.decode("latin-1")
        self.assertIn('name="caption"', body)
        self.assertIn("✅ cube finished.".encode(), seen[0].data)
        self.assertIn('filename="photo.jpg"', body)

    def test_photo_error_falls_back_to_text(self):
        seen = []

        def fake(req, timeout=0):
            seen.append(req)
            if req.full_url.endswith("/sendPhoto"):
                raise OSError("photo too big")
            return Resp()
        with mock.patch.object(notify.urllib.request, "urlopen", fake):
            self.assertTrue(notify.send_telegram(self.db(), "✅ cube finished.", b"\xff\xd8jpeg"))
        self.assertEqual([r.full_url.rsplit("/", 1)[1] for r in seen], ["sendPhoto", "sendMessage"])
        self.assertEqual(json.loads(seen[1].data), {"chat_id": "2", "text": "✅ cube finished."})


class PushImage(unittest.TestCase):
    def test_payload_carries_the_image_url(self):
        db = memdb()
        notify.put(db, "push_subs", [{"id": "a", "label": "iPhone", "sub": {"endpoint": "https://push/a"}}])
        seen = []
        with mock.patch.object(notify, "webpush", lambda **kw: seen.append(kw["data"])):
            self.assertEqual(notify.send_push(db, "Delta", "done", "/app", "https://pygmy/photo/x"), 1)
        self.assertEqual(json.loads(seen[0])["image"], "https://pygmy/photo/x")


class Duration(unittest.TestCase):
    """The duration comes only from a start this Notifier saw: a restart mid-print means no ' in N min'."""

    def notifier(self, states):
        path = tempfile.mktemp(suffix=".db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
        db.commit()
        db.close()
        n = notify.Notifier(path, lambda: ["delta"], lambda a: states[a], lambda a: "Delta")
        n.background = False
        self.sent = []
        patches = [mock.patch.object(notify, "send_push",
                                     lambda db, t, b, u, image=None: self.sent.append(b) or 1),
                   mock.patch.object(notify, "send_telegram",
                                     lambda db, text, photo=None: self.sent.append(text) or True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return n

    def test_no_duration_when_the_start_was_not_seen(self):
        states = {"delta": {"connected": True, "state": "RUNNING", "subtask_name": "cube"}}
        n = self.notifier(states)
        n.step(0)  # already printing when BFF starts: its real start is unknown
        states["delta"] = {"connected": True, "state": "FINISH", "subtask_name": "cube"}
        n.step(600)
        self.assertTrue(self.sent)
        self.assertNotIn(" in ", self.sent[0])

    def test_duration_when_the_start_was_seen(self):
        states = {"delta": {"connected": True, "state": "IDLE"}}
        n = self.notifier(states)
        n.step(0)
        states["delta"] = {"connected": True, "state": "RUNNING", "subtask_name": "cube"}
        n.step(5)
        states["delta"] = {"connected": True, "state": "FINISH", "subtask_name": "cube"}
        n.step(605)
        self.assertIn(" in 10 min", self.sent[0])

    def test_under_a_minute_no_duration(self):
        states = {"delta": {"connected": True, "state": "IDLE"}}
        n = self.notifier(states)
        n.step(0)
        states["delta"] = {"connected": True, "state": "RUNNING", "subtask_name": "cube"}
        n.step(0)
        states["delta"] = {"connected": True, "state": "FINISH", "subtask_name": "cube"}
        n.step(20)
        self.assertTrue(self.sent)
        self.assertNotIn("0 min", self.sent[0])


if __name__ == "__main__":
    unittest.main()
