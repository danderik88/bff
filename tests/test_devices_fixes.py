import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bff_mcp"))
import devices  # noqa: E402


def memdb():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
    return db


BUILTIN = ["imac", "mini", "iphone"]


class DeviceFixes(unittest.TestCase):
    def setUp(self):
        devices.PENDING.clear()
        devices.FAILS.clear()
        devices.CACHE[0] = None

    def test_cached_check_after_add_and_remove(self):
        db = memdb()
        out = devices.add(db, "iPad", BUILTIN, "https://bff.example.ts.net", now=100)
        key = devices.redeem(out["code"], now=200)
        self.assertEqual(devices.cached_check(key), "iPad")
        self.assertIsNone(devices.cached_check(key + "x"))
        devices.remove(db, "iPad", BUILTIN)
        self.assertIsNone(devices.cached_check(key))

    def test_cached_check_unknown_before_load(self):
        self.assertIs(devices.cached_check("anything"), devices.UNKNOWN)

    def test_old_wrong_codes_expire_within_window(self):
        db = memdb()
        for _ in range(9):
            self.assertIsNone(devices.redeem("000000", now=100))
        out = devices.add(db, "iPad", BUILTIN, "https://x", now=100 + devices.CODE_S + 1)
        for _ in range(5):
            self.assertIsNone(devices.redeem("111111", now=100 + devices.CODE_S + 2))
        self.assertTrue(devices.redeem(out["code"], now=100 + devices.CODE_S + 3))

    def test_ten_wrong_codes_within_window_clear_pending(self):
        db = memdb()
        out = devices.add(db, "iPad", BUILTIN, "https://x", now=100)
        for _ in range(devices.MAX_FAILS):
            self.assertIsNone(devices.redeem("000000", now=101))
        self.assertIsNone(devices.redeem(out["code"], now=102))

    def test_cancel_makes_code_unredeemable(self):
        db = memdb()
        out = devices.add(db, "iPad", BUILTIN, "https://x", now=100)
        devices.cancel("iPad")
        self.assertIsNone(devices.redeem(out["code"], now=200))


if __name__ == "__main__":
    unittest.main()
