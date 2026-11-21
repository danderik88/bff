import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "slicer"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import slicerd  # noqa: E402
from pygmy import server  # noqa: E402


class FakeProc:
    def __init__(self, cmd, *a, **k):
        self.cmd, self.done = cmd, False

    def poll(self):
        return 0 if self.done else None

    def terminate(self):
        self.done = True

    def wait(self, timeout=None):
        return 0


class Gui(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.p = [mock.patch.object(slicerd, "ORCA_DATA", self.data),
                  mock.patch.object(slicerd.subprocess, "Popen", FakeProc),
                  mock.patch.dict(slicerd._GUI, {"proc": None, "started": 0.0, "login": None})]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def login_done(self):
        (self.data / "user" / "abc").mkdir(parents=True)
        (self.data / "user" / "abc" / "sync_state").write_text("{}")

    def test_no_gui_until_someone_signs_in(self):
        slicerd.gui_tick(now=100.0)
        self.assertIsNone(slicerd._GUI["proc"])

    def test_sign_in_starts_the_gui_and_sets_the_token_file(self):
        with mock.patch.object(slicerd, "_drive_login"):
            self.assertEqual(slicerd.start_login("github"), {})
        self.assertIsNotNone(slicerd._GUI["proc"])
        self.assertEqual(slicerd._LINK["state"], "starting")
        conf = json.loads((self.data / "OrcaSlicer.conf").read_text())
        self.assertEqual(conf["app"]["use_encrypted_token_file"], "true")

    def test_logout_forgets_the_account(self):
        (self.data / "orca_refresh_token.sec").write_text("x")
        self.login_done()
        self.assertTrue(slicerd.orca_logged_in())
        slicerd.logout()
        self.assertFalse(slicerd.orca_logged_in())
        self.assertFalse((self.data / "orca_refresh_token.sec").exists())

    def test_email_sign_in_needs_both_fields(self):
        for email, password in (("", "x"), ("a@b.c", ""), ("", "")):
            with self.assertRaises(ValueError):
                slicerd.start_login("email", email, password)

    def test_a_typing_failure_never_carries_the_password(self):
        import subprocess
        boom = subprocess.TimeoutExpired(["xdotool", "type", "secret-pw"], 15)
        with mock.patch.object(slicerd, "_click"), mock.patch.object(slicerd, "_x", side_effect=boom):
            with self.assertRaises(RuntimeError) as caught:
                slicerd._type_login("a@b.c", "secret-pw")
        self.assertNotIn("secret-pw", str(caught.exception))

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            slicerd.start_login("facebook")

    def test_window_closes_after_its_time(self):
        with mock.patch.object(slicerd, "_drive_login"):
            slicerd.start_login()
        procs = slicerd._GUI["login"][0]
        slicerd.gui_tick(now=slicerd._GUI["login"][2] + 1)
        self.assertIsNone(slicerd._GUI["login"])
        self.assertTrue(all(p.done for p in procs))

    def test_signed_in_gui_restarts_hourly(self):
        self.login_done()
        slicerd.gui_tick(now=10.0)
        first = slicerd._GUI["proc"]
        slicerd.gui_tick(now=10.0 + slicerd.GUI_RESTART_S + 1)
        self.assertTrue(first.done)
        self.assertIsNot(slicerd._GUI["proc"], first)

    def test_finish_login_only_forwards_orcas_own_callback(self):
        slicerd._LINK.update(state="link", port=41172)
        for bad in ("https://evil.example/callback?code=1", "http://localhost:9999/callback?code=1",
                    "http://localhost:41172/other?code=1", "nonsense", ""):
            with self.assertRaises(ValueError):
                slicerd.finish_login(bad)
        with mock.patch("urllib.request.urlopen") as opened:
            slicerd.finish_login("http://localhost:41172/callback?orca_state=a&code=b")
        self.assertEqual(opened.call_args[0][0], "http://127.0.0.1:41172/callback?orca_state=a&code=b")
        self.assertEqual(slicerd._LINK["state"], "done")


class CloudTool(unittest.TestCase):
    EMPTY = {"logged_in": False, "login_open": False, "login_state": None, "login_link": None,
             "login_error": None, "machines": [], "machine": None}

    def test_status_without_sign_in(self):
        with mock.patch.object(server.slicer, "orca_status", lambda: {"logged_in": False, "login_open": False}):
            got = json.loads(server.tool_orca_cloud({})[-1]["text"])
        self.assertEqual(got, self.EMPTY)

    def test_account_without_printer_profiles_is_not_an_error(self):
        def no_printer_profiles(_data):
            raise server.slicer.SliceError("No printer profile in this file")
        with mock.patch.object(server.slicer, "orca_status", lambda: {"logged_in": True}), \
                mock.patch.object(server.slicer, "orca_user", lambda: b"zip"), \
                mock.patch.object(server.slicer, "user_bundle", no_printer_profiles):
            got = json.loads(server.tool_orca_cloud({})[-1]["text"])
        self.assertTrue(got["logged_in"])
        self.assertEqual(got["machines"], [])

    def test_login_starts_with_the_chosen_provider_and_reports_the_link(self):
        calls = []
        status = {"logged_in": False, "login_open": True, "state": "link", "url": "https://auth.orcaslicer.com/x"}
        with mock.patch.object(server.slicer, "orca_login", lambda a, **k: calls.append((a, k)) or {}), \
                mock.patch.object(server.slicer, "orca_status", lambda: status):
            got = json.loads(server.tool_orca_cloud({"action": "login", "provider": "apple"})[-1]["text"])
        self.assertEqual(calls, [("start", {"provider": "apple", "email": "", "password": ""})])
        self.assertEqual(got["login_link"], "https://auth.orcaslicer.com/x")

    def test_email_sign_in_passes_the_typed_fields_on(self):
        calls = []
        with mock.patch.object(server.slicer, "orca_login", lambda a, **k: calls.append((a, k)) or {}), \
                mock.patch.object(server.slicer, "orca_status", lambda: {"logged_in": False}):
            server.tool_orca_cloud({"action": "login", "provider": "email", "email": "a@b.c", "password": "pw"})
        self.assertEqual(calls, [("start", {"provider": "email", "email": "a@b.c", "password": "pw"})])

    def test_logout_empties_what_was_followed(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT)")
        db.execute("INSERT INTO settings VALUES ('e3k', 'orca_cloud', 'Creality E3K 0.6 nozzle')")
        calls = []
        with mock.patch.object(server, "DB", db), \
                mock.patch.object(server.slicer, "orca_login", lambda a, **k: calls.append(a) or {}), \
                mock.patch.object(server.slicer, "orca_status", lambda: {"logged_in": False}):
            server.tool_orca_cloud({"action": "logout"})
        self.assertEqual(calls, ["logout"])
        self.assertEqual(db.execute("SELECT count(*) FROM settings WHERE key='orca_cloud'").fetchone()[0], 0)

    def test_pasted_address_goes_to_the_slicer(self):
        calls = []
        with mock.patch.object(server.slicer, "orca_login", lambda a, **k: calls.append((a, k)) or {}), \
                mock.patch.object(server.slicer, "orca_status", lambda: {"logged_in": False}):
            server.tool_orca_cloud({"action": "login_paste", "url": "http://localhost:1/callback?code=2"})
        self.assertEqual(calls, [("finish", {"url": "http://localhost:1/callback?code=2"})])

    def test_sync_skips_unchanged_presets(self):
        calls = []
        with mock.patch.object(server, "orca_cloud_follows", lambda alias=None: {"e3k": "Creality E3K 0.6 nozzle"}), \
                mock.patch.object(server.slicer, "orca_user", lambda: b"zip"), \
                mock.patch.object(server, "tool_orca_profiles", lambda args: calls.append(args["machine"])), \
                mock.patch.dict(server._ORCA_CLOUD_DIGEST, clear=True):
            server.orca_cloud_sync()
            server.orca_cloud_sync()
        self.assertEqual(calls, ["Creality E3K 0.6 nozzle"])


if __name__ == "__main__":
    unittest.main()
