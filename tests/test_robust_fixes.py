"""Small robustness fixes: notify read errors, HEARD under threads, certificate refusals."""
import contextlib
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pygmy"))
import onboard  # noqa: E402
import printer_link  # noqa: E402
from pygmy import notify  # noqa: E402


class FakeClient:
    def __init__(self):
        self.password = None
        self.disconnects = 0

    def username_pw_set(self, _user, password):
        self.password = password

    def disconnect(self):
        self.disconnects += 1


class FakeSock:
    def __init__(self):
        self.der = b"cert-a"

    def getpeercert(self, binary_form):
        return self.der


class ReadErrorIsNotOffline(unittest.TestCase):
    def test_a_status_that_keeps_raising_produces_no_offline_alert(self):
        path = tempfile.mktemp(suffix=".db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
        db.commit()
        states = {"delta": {"connected": True, "state": "IDLE"}}
        n = notify.Notifier(path, lambda: list(states), lambda a: states[a], lambda a: a.capitalize())
        n.background = False
        n.step(0)
        n.read_status = lambda a: (_ for _ in ()).throw(RuntimeError("down"))
        with contextlib.redirect_stderr(io.StringIO()):
            out = [n.step(t) for t in (10, 100, 200, 300)]
        self.assertEqual([x for cycle in out for x in cycle], [])
        self.assertFalse(n.presence["delta"].told)


class HeardWhileWritten(unittest.TestCase):
    def setUp(self):
        self.saved = dict(onboard.HEARD)

    def tearDown(self):
        with onboard.HEARD_LOCK:
            onboard.HEARD.clear()
            onboard.HEARD.update(self.saved)

    def test_heard_iterates_while_another_thread_adds(self):
        clock = lambda: 0.0  # noqa: E731 (everything stays fresh)
        with onboard.HEARD_LOCK:
            onboard.HEARD["seed"] = ({"serial": "seed"}, 0.0)

        def writer():
            for i in range(1000):
                with onboard.HEARD_LOCK:
                    onboard.HEARD[f"s{i}"] = ({"serial": f"s{i}"}, 0.0)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(1000):
                heard = onboard.heard(clock=clock)
                self.assertTrue(heard)
        finally:
            thread.join()


class CertificateRefusal(unittest.TestCase):
    def test_oserror_from_check_fingerprint_does_not_propagate_and_disconnects(self):
        client = FakeClient()
        link = printer_link.PrinterLink("a1", "host", "s", "code", tempfile.mkdtemp())
        with mock.patch.object(printer_link, "check_fingerprint", side_effect=OSError("boom")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            link._on_socket_open(client, None, FakeSock())
        self.assertEqual(client.password, "")
        self.assertEqual(client.disconnects, 1)
        self.assertIn("OSError", err.getvalue())

    def test_certificate_changed_stops_reconnecting(self):
        client, sock = FakeClient(), FakeSock()
        with tempfile.TemporaryDirectory() as tmp_dir:
            link = printer_link.PrinterLink("a1", "host", "s", "code", tmp_dir)
            with contextlib.redirect_stderr(io.StringIO()):
                link._on_socket_open(client, None, sock)
                self.assertEqual(client.password, "code")
                sock.der = b"cert-b"
                link._on_socket_open(client, None, sock)
                self.assertTrue(link.cert_refused)
                self.assertEqual(client.disconnects, 1)
                client.password = None
                link._on_socket_open(client, None, sock)  # A retry: refused before any credential.
                self.assertEqual(client.disconnects, 2)
                self.assertIsNone(client.password)
                snapshot = link.snapshot()
            self.assertEqual(snapshot["connected"], False)
            self.assertEqual(snapshot["error"], "certificate changed: re-add the printer")


if __name__ == "__main__":
    unittest.main()
