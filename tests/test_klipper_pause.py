"""A Klipper pause past idle_timeout: motors and heaters are off, so RESUME must be refused."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from pygmy import server  # noqa: E402
from pygmy.moonraker_link import MoonrakerLink, pause_state  # noqa: E402

PROFILE = {"command_timeout_s": 0.3, "supports": {"pause": True, "resume": True, "stop": True}}


class PausedLink:
    """A Klipper printer whose pause outlived idle_timeout."""
    def __init__(self):
        self.posts = []

    def snapshot(self):
        return {"connected": True, "state": "PAUSE", "resume_blocked": True, "pause_left_s": None}

    def command(self, action, **params):
        self.posts.append(action)
        return True


class PauseState(unittest.TestCase):
    def test_no_homed_axes_blocks_resume(self):
        self.assertEqual(pause_state("", "Ready", 1800, 600), (True, None))

    def test_partial_axes_block_resume(self):
        self.assertEqual(pause_state("xy", "Ready", 1800, 600), (True, None))

    def test_idle_timeout_state_blocks_resume(self):
        self.assertEqual(pause_state("xyz", "Idle", 1800, 600), (True, None))

    def test_all_axes_homed_and_ready_reports_the_time_left(self):
        self.assertEqual(pause_state("xyz", "Ready", 1800, 600), (False, 1200))

    def test_seconds_left_floors_at_zero(self):
        self.assertEqual(pause_state("xyz", "Ready", 600, 900), (False, 0))


class SnapshotFields(unittest.TestCase):
    def link(self, homed="xyz", idle="Printing"):
        link = MoonrakerLink("k1", "host", clock=lambda: 100.0)
        link.idle_timeout_s = 1800
        link._paused_since = 40.0
        link.last_report = 100.0
        link.raw = {"print_stats": {"state": "paused"}, "toolhead": {"homed_axes": homed},
                    "idle_timeout": {"state": idle}}
        return link.snapshot()

    def test_paused_and_homed(self):
        snap = self.link()
        self.assertFalse(snap["resume_blocked"])
        self.assertEqual(snap["pause_left_s"], 1740)

    def test_motors_off_blocks_resume(self):
        snap = self.link(homed="")
        self.assertTrue(snap["resume_blocked"])
        self.assertIsNone(snap["pause_left_s"])


class ResumeRefused(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "k1.json").write_text(json.dumps(PROFILE))
        server.CFG.update(printers={"k1": 1}, tokens={}, control=True, profiles=str(tmp))
        server.open_db(str(tmp / "state.db"))
        self.link = server.LINKS["k1"] = PausedLink()

    def tearDown(self):
        server.DB.close()
        server.LINKS.clear()
        server.CFG.pop("control", None)
        self.tmp.cleanup()

    def test_resume_is_refused_and_nothing_is_sent(self):
        with self.assertRaises(server.ToolError) as caught:
            server.execute("k1", "resume", {}, "key-pause-expired")
        self.assertIn("Pause expired", str(caught.exception))
        self.assertEqual(self.link.posts, [])


if __name__ == "__main__":
    unittest.main()
