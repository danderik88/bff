"""Concurrent use of the shared sqlite3 connection via LockedDB.

Run: python3 -m unittest discover -s tests"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from bff_mcp import server  # noqa: E402


class DbThreadsTest(unittest.TestCase):
    def setUp(self):
        server.open_db(":memory:")

    def tearDown(self):
        server.DB.close()

    def test_shared_connection_under_threads(self):
        errors = []
        N_ITERS = 300

        def work(i):
            try:
                for _ in range(N_ITERS):
                    value = server.DB.execute("SELECT 1").fetchone()[0]
                    assert value == 1
                    with server.DB:
                        server.DB.execute(
                            "INSERT OR REPLACE INTO settings VALUES (?,?,?)",
                            (f"t{i}", "k", "v"),
                        )
            except Exception as err:  # noqa: BLE001
                errors.append((i, repr(err)))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        count = server.DB.execute("SELECT COUNT(*) FROM settings WHERE key='k'").fetchone()[0]
        self.assertEqual(count, 8)  # one row per thread (INSERT OR REPLACE)


if __name__ == "__main__":
    unittest.main()
