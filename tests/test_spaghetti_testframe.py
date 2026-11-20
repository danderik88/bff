import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bff_mcp import server


class TestFrame(unittest.TestCase):
    def test_file_stands_in_until_deleted(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"STATE_DIRECTORY": d}):
            self.assertIsNone(server.test_frame("e3k"))
            (Path(d) / "spaghetti").mkdir()
            (Path(d) / "spaghetti" / "test-e3k.jpg").write_bytes(b"\xff\xd8x")
            self.assertEqual(server.test_frame("e3k"), b"\xff\xd8x")
            self.assertIsNone(server.test_frame("a1"))


if __name__ == "__main__":
    unittest.main()


class JoinedMidPrint(unittest.TestCase):
    """Pygmy restarted during a print: no warmup, bad frames alarm within the one-minute vote."""

    def first_verdict(self, progress):
        from bff_mcp import spaghetti
        bad = [["failure", 0.9, [0, 0, 1, 1]]] * 3
        w = spaghetti.Watch(lambda: "RUNNING", lambda: b"j", lambda j: bad, progress_of=lambda: progress)
        for _ in range(spaghetti.WINDOW):
            w.tick()
        return w.verdict

    def test_midway_skips_warmup(self):
        self.assertIn(self.first_verdict(40), ("warning", "failure"))

    def test_fresh_print_keeps_warmup(self):
        self.assertEqual(self.first_verdict(0), "safe")
