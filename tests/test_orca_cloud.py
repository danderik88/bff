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
        got = slicerd.start_login()
        self.assertEqual(got["port"], slicerd.NOVNC_PORT)
        self.assertEqual(len(got["password"]), 8)
        self.assertIsNotNone(slicerd._GUI["proc"])
        conf = json.loads((self.data / "OrcaSlicer.conf").read_text())
        self.assertEqual(conf["app"]["use_encrypted_token_file"], "true")

    def test_window_closes_after_its_time(self):
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


class CloudTool(unittest.TestCase):
    def test_status_without_sign_in(self):
        with mock.patch.object(server.slicer, "orca_status", lambda: {"logged_in": False, "login_open": False}):
            got = json.loads(server.tool_orca_cloud({})[-1]["text"])
        self.assertEqual(got, {"logged_in": False, "login_open": False, "machines": [], "machine": None})

    def test_login_returns_the_window(self):
        with mock.patch.object(server.slicer, "orca_login", lambda a: {"port": 6080, "password": "abcd1234"}):
            got = json.loads(server.tool_orca_cloud({"action": "login"})[-1]["text"])
        self.assertEqual(got, {"login_port": 6080, "password": "abcd1234"})

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
