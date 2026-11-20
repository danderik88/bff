"""Tests for the files folder (src/pygmy/library.py). Run: python3 -m unittest tests.test_library"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from pygmy import library  # noqa: E402


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        library.setup(self.tmp.name)

    def test_save_read_roundtrip(self):
        library.save(1, b"hello world")
        self.assertEqual(library.read(1), b"hello world")

    def test_remove_missing_is_ok(self):
        library.remove(999)
        with self.assertRaises(FileNotFoundError):
            library.read(999)

    def test_prune_keeps_under_cap_oldest_first(self):
        library.setup(self.tmp.name, cap_bytes=100)
        for fid in range(1, 6):
            library.save(fid, b"x" * 40)
        deleted = library.prune([5, 4, 3, 2, 1], set())
        self.assertEqual(deleted, [1, 2, 3])
        for fid in (4, 5):
            self.assertTrue((Path(self.tmp.name) / f"{fid}.gcode.3mf").exists())
        for fid in (1, 2, 3):
            self.assertFalse((Path(self.tmp.name) / f"{fid}.gcode.3mf").exists())

    def test_prune_never_deletes_kept(self):
        library.setup(self.tmp.name, cap_bytes=100)
        for fid in range(1, 6):
            library.save(fid, b"x" * 40)
        deleted = library.prune([5, 4, 3, 2, 1], {1})
        self.assertEqual(deleted, [2, 3, 4])
        self.assertTrue((Path(self.tmp.name) / "1.gcode.3mf").exists())

    def test_save_failure_leaves_no_partial_file(self):
        os.chmod(self.tmp.name, 0o555)
        self.addCleanup(os.chmod, self.tmp.name, 0o755)
        with self.assertRaises(OSError):
            library.save(1, b"x" * 10)
        self.assertFalse((Path(self.tmp.name) / "1.gcode.3mf").exists())


if __name__ == "__main__":
    unittest.main()
