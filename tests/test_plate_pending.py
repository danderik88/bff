import tempfile
import unittest
from pathlib import Path

import test_filament_newprint as base

server = base.server


class PlatePending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        server.CFG.update(printers={"a1": 1})
        server.open_db(str(Path(self.tmp.name) / "state.db"))
        server.LAST_STATE.clear()

    def pending(self):
        return server.plate_clear_pending("a1")

    def walk(self, *states):
        for state in states:
            server.track_plate("a1", state)

    def test_set_when_a_print_ends_cleared_when_the_next_starts(self):
        self.walk("RUNNING", "FINISH")
        self.assertTrue(self.pending())
        self.walk("IDLE")
        self.assertTrue(self.pending())
        self.walk("PREPARE")
        self.assertFalse(self.pending())
        self.walk("RUNNING", "PAUSE", "FAILED")
        self.assertTrue(self.pending())

    def test_restart_invents_no_edge(self):
        self.walk("FINISH")
        self.assertFalse(self.pending())

    def test_cleared_by_hand(self):
        self.walk("RUNNING", "FINISH")
        server.tool_plate_cleared({"printer": "a1"})
        self.assertFalse(self.pending())
        server.ha_action("a1", "plate_cleared", None, "k" * 10)
        self.assertFalse(self.pending())


if __name__ == "__main__":
    unittest.main()
