"""Pause and stop are safety commands: an unknown last outcome must not refuse them."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from bff_mcp import server  # noqa: E402
import test_bff_direct as direct_tests  # noqa: E402

PROFILE = {"command_timeout_s": 0.3, "supports": {"pause": True, "resume": True, "stop": True}}


class StopLink(direct_tests.FakeLink):
    """The base FakeLink answers a pause only; this one also reports the print as stopped."""

    def publish(self, payload):
        sent = super().publish(payload)
        if sent and payload.get("print", {}).get("command") == "stop":
            self.feed({"print": {"gcode_state": "FAILED"}})
        return sent


class SafetyCommandsSkipTheLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "a1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, profiles=str(tmp))
        server.open_db(str(tmp / "state.db"))
        self.link = server.LINKS["a1"] = StopLink()

    def tearDown(self):
        server.DB.close()
        server.LINKS.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def locked(self):
        with server.DB:
            server.DB.execute("INSERT OR REPLACE INTO locks VALUES (?,?)", ("a1", "old-key-0001"))

    def commands(self):
        return [p.get("print", {}).get("command") for p in self.link.published]

    def test_pause_goes_through_a_locked_printer(self):
        self.locked()
        result = server.execute("a1", "pause", {}, "key-pause-01")
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(self.commands(), ["pause"])

    def test_stop_goes_through_a_locked_printer(self):
        self.locked()
        token = json.loads(server.tool_prepare_stop({"printer": "a1"})[0]["text"])["confirm_token"]
        result = server.execute("a1", "stop", None, "key-stop-01", token=token)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertIn("stop", self.commands())

    def test_resume_is_still_refused(self):
        self.locked()
        self.assertRaises(server.ToolError, server.execute, "a1", "resume", {}, "key-resume-01")
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    unittest.main()
