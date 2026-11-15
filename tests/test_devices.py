import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bff_mcp"))
import devices  # noqa: E402
import server  # noqa: E402


def memdb():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
    return db


BUILTIN = ["imac", "mini", "iphone"]


class Devices(unittest.TestCase):
    def setUp(self):
        devices.PENDING.clear()
        devices.FAILS.clear()
        devices.CACHE[0] = None

    def test_add_redeem_check(self):
        db = memdb()
        out = devices.add(db, " iPad ", BUILTIN, "https://bff.example.ts.net", now=100)
        self.assertRegex(out["code"], r"^\d{6}$")
        self.assertEqual(out["link"], "https://bff.example.ts.net/app#p=" + out["code"])
        self.assertTrue(out["qr"].startswith("data:image/svg+xml"))
        key = devices.redeem(out["code"], now=200)
        self.assertGreaterEqual(len(key), 32)
        self.assertEqual(devices.redeem(out["code"], now=300), key)  # Safari + Home Screen app
        self.assertEqual(devices.check(db, key), "iPad")
        self.assertIsNone(devices.check(db, key + "x"))
        self.assertNotIn(key, str(db.execute("SELECT value FROM settings").fetchall()))

    def test_code_expires(self):
        out = devices.add(memdb(), "iPad", BUILTIN, "https://x", now=100)
        self.assertIsNone(devices.redeem(out["code"], now=100 + devices.CODE_S + 1))

    def test_bruteforce_cancels_codes(self):
        out = devices.add(memdb(), "iPad", BUILTIN, "https://x", now=100)
        wrong = "000000" if out["code"] != "000000" else "111111"
        for _ in range(devices.MAX_FAILS):
            self.assertIsNone(devices.redeem(wrong, now=101))
        self.assertIsNone(devices.redeem(out["code"], now=102))

    def test_names(self):
        db = memdb()
        devices.add(db, "iPad", BUILTIN, "https://x", now=1)
        for bad in ("", "  ", "x" * 33, "ipad", "IPHONE", None):
            with self.assertRaises(ValueError):
                devices.add(db, bad, BUILTIN, "https://x", now=1)
        with self.assertRaises(ValueError):
            devices.add(db, "Pixel", BUILTIN, "javascript:alert(1)", now=1)

    def test_list_and_remove(self):
        db = memdb()
        code = devices.add(db, "iPad", BUILTIN, "https://x", now=1)["code"]
        key = devices.redeem(code, now=2)
        rows = devices.listing(db, BUILTIN, me="iPad")
        self.assertIn({"name": "iPad", "builtin": False, "me": True}, rows)
        self.assertIn({"name": "imac", "builtin": True, "me": False}, rows)
        with self.assertRaises(ValueError):
            devices.remove(db, "imac", BUILTIN)
        devices.remove(db, "iPad", BUILTIN)
        self.assertIsNone(devices.check(db, key))
        self.assertIsNone(devices.redeem(code, now=3))

    def test_parallel_adds_keep_every_device(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "s.db")
        sqlite3.connect(path).execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
        barrier = threading.Barrier(12)

        def one(i):
            db = sqlite3.connect(path, timeout=10)
            barrier.wait()
            devices.add(db, f"d{i}", BUILTIN, "https://x")
            db.close()
        threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(devices.listing(sqlite3.connect(path), [])), 12)

    def test_unprintable_name_and_fail_reset(self):
        with self.assertRaises(ValueError):
            devices.add(memdb(), "a\nb", BUILTIN, "https://x", now=1)
        out = devices.add(memdb(), "iPad", BUILTIN, "https://x", now=1)
        for _ in range(devices.MAX_FAILS - 1):
            devices.redeem("bad", now=2)
        self.assertTrue(devices.redeem(out["code"], now=2))
        devices.redeem("bad", now=2)  # a success resets the count: this typo must not cancel the code
        self.assertTrue(devices.redeem(out["code"], now=3))


class OverHttp(unittest.TestCase):
    """The new device redeems its code at /pair, then its key opens the MCP endpoint."""

    def setUp(self):
        devices.PENDING.clear()
        devices.FAILS.clear()
        devices.CACHE[0] = None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server.DB_PATH = str(Path(tmp.name) / "state.db")
        memdb_file = sqlite3.connect(server.DB_PATH)
        memdb_file.execute("CREATE TABLE settings (printer TEXT, key TEXT, value TEXT, PRIMARY KEY (printer, key))")
        memdb_file.close()
        self.addCleanup(lambda: setattr(server, "DB_PATH", None))
        saved = dict(server.CFG)
        self.addCleanup(lambda: (server.CFG.clear(), server.CFG.update(saved)))
        server.CFG.update(printers={}, tokens={"imac": "i" * 40})
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def post(self, path, body, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=5)
        conn.request("POST", path, json.dumps(body), {"Authorization": f"Bearer {token}"} if token else {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, json.loads(data) if data else None

    def call(self, name, args, token):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {"printer": "", **args}}}
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=5)
        conn.request("POST", "/", json.dumps(msg), {"Authorization": f"Bearer {token}", "X-BFF-App": "1"})
        out = json.loads(conn.getresponse().read())
        conn.close()
        return out["result"]

    def test_pair_then_use_then_remove(self):
        res = self.call("device_add", {"name": "iPad", "origin": "https://bff.x.ts.net"}, "i" * 40)
        code = json.loads(res["content"][0]["text"])["code"]
        self.assertEqual(self.post("/pair", {"code": "nope"})[0], 400)
        status, body = self.post("/pair", {"code": code})
        self.assertEqual(status, 200)
        key = body["key"]
        listed = json.loads(self.call("device_list", {}, key)["content"][0]["text"])["devices"]
        self.assertIn({"name": "iPad", "builtin": False, "me": True}, listed)
        self.call("device_remove", {"name": "iPad"}, "i" * 40)
        self.assertEqual(self.post("/", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key)[0], 401)


class AppPage(unittest.TestCase):
    def test_app_pairs_and_lists_devices(self):
        page = (Path(server.__file__).parent / "app.html").read_text()
        for needle in ('fetch("/pair"', 'location.hash.startsWith("#p=")', 'rpc("device_add"', 'rpc("device_remove"', "devOptions()", 'OPT_BOXES.some(b => $(b).contains(document.activeElement))'):
            self.assertIn(needle, page)

    def test_png_icons(self):
        here = Path(server.__file__).parent
        srcs = [i["src"] for i in json.loads((here / "manifest.json").read_text())["icons"]]
        for n in (192, 512):
            data = (here / f"icon-{n}.png").read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(data[12:16], b"IHDR")
            self.assertEqual((int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")), (n, n))
            self.assertIn(f"/icon-{n}.png", srcs)
            self.assertIn(f"/icon-{n}.png", server.STATIC)


if __name__ == "__main__":
    unittest.main()
