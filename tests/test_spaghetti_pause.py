"""Pause on spaghetti: a failure pauses the print once, only when the option is on."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from pygmy import server  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402

PROFILE = {"command_timeout_s": 0.3, "supports": {"pause": True}}


class Watch:
    """Just the fields note_spaghetti reads and writes."""

    def __init__(self, verdict="safe", state="RUNNING"):
        self.verdict, self.state, self.score, self.error = verdict, state, 0.5, None
        self.muted, self.last_jpeg, self.paused = False, None, None

    def state_of(self):
        return self.state


class PauseOnFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp))
        server.open_db(str(tmp / "state.db"))
        self.link = server.LINKS["a1"] = direct_tests.FakeLink()
        self.watch = Watch()
        server.WATCHES["a1"] = self.watch

    def tearDown(self):
        server.DB.close()
        server.LINKS.clear()
        server.WATCHES.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def pauses(self):
        return [p for p in self.link.published if p.get("print", {}).get("command") == "pause"]

    def note(self, verdict):
        self.watch.verdict = verdict
        for _ in range(3):  # further failures are what a watch keeps reporting while the print stays bad
            server.note_spaghetti("a1", self.watch)

    def test_failure_pauses_once(self):
        self.note("failure")
        self.assertEqual(len(self.pauses()), 1)
        self.assertIs(self.watch.paused, True)

    def test_warning_does_not_pause(self):
        self.note("warning")
        self.assertEqual(self.pauses(), [])
        self.assertIsNone(self.watch.paused)

    def test_option_off_does_not_pause(self):
        server.tool_set_print_option({"printer": "a1", "option": "bff_spaghetti_pause", "enabled": False})
        self.note("failure")
        self.assertEqual(self.pauses(), [])

    def test_muted_does_not_pause(self):
        self.watch.muted = True
        self.note("failure")
        self.assertEqual(self.pauses(), [])

    def test_not_running_does_not_pause(self):
        self.watch.state = "PAUSE"
        self.note("failure")
        self.assertEqual(self.pauses(), [])

    def test_option_defaults_on(self):
        self.assertTrue(server.spaghetti_pause_on("a1"))
        server.tool_set_print_option({"printer": "a1", "option": "bff_spaghetti_pause", "enabled": False})
        self.assertFalse(server.spaghetti_pause_on("a1"))


if __name__ == "__main__":
    unittest.main()
