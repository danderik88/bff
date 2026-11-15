import tempfile
import unittest
from pathlib import Path

import test_filament_newprint as base

server = base.server


class ModelSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        server.CFG.update(printers={"a1": 1}, tokens={}, control=True, orca_wake_url="")
        server.open_db(str(Path(self.tmp.name) / "state.db"))

    def row(self, rid):
        return server.DB.execute("SELECT status, source, file_name, file_size, reply FROM print_requests WHERE id=?",
                                 (rid,)).fetchone()

    def test_link_sources(self):
        for url, source in (("https://www.printables.com/model/123-box", "printables"),
                            ("https://www.thingiverse.com/thing:42", "thingiverse"),
                            ("https://makerworld.com/en/models/1", "makerworld")):
            server.tool_request_print({"printer": "a1", "url": url})
            rid = server.DB.execute("SELECT max(id) FROM print_requests").fetchone()[0]
            self.assertEqual(self.row(rid)[:2], ("pending", source))
        for bad in ("https://printables.com.evil.io/x", "http://www.thingiverse.com/thing:1", "https://cults3d.com/x"):
            with self.assertRaises(server.ToolError):
                server.tool_request_print({"printer": "a1", "url": bad})

    def test_upload_model_kept_while_open(self):
        out = server.upload_model("a1", "Box v2.STL", b"solid x")
        rid = out["request_id"]
        self.assertEqual(self.row(rid), ("pending", "upload", "Box v2.STL", 7, None))
        self.assertEqual(server.DB.execute("SELECT data FROM models WHERE request_id=?", (rid,)).fetchone()[0], b"solid x")
        server.tool_cancel_request({"printer": "a1", "request_id": rid})
        server.upload_model("a1", "b.3mf", b"pk")
        self.assertIsNone(server.DB.execute("SELECT 1 FROM models WHERE request_id=?", (rid,)).fetchone())
        for bad in ("x.exe", "../a.stl", "a.stl.sh", ""):
            with self.assertRaises(server.ToolError):
                server.upload_model("a1", bad, b"x")
        with self.assertRaises(server.ToolError):
            server.upload_model("zz", "a.stl", b"x")

    def test_reply_only_when_blocked(self):
        rid = server.upload_model("a1", "a.step", b"x")["request_id"]
        with self.assertRaises(server.ToolError):
            server.tool_reply_request({"printer": "a1", "request_id": rid, "reply": "123456"})
        server.tool_print_requests({"printer": "a1", "id": rid, "status": "blocked", "note": "Enter the MakerWorld code"})
        server.tool_reply_request({"printer": "a1", "request_id": rid, "reply": " 123456 "})
        self.assertEqual(self.row(rid)[0::4], ("pending", "123456"))
        with self.assertRaises(server.ToolError):
            server.tool_reply_request({"printer": "a1", "request_id": rid, "reply": ""})


if __name__ == "__main__":
    unittest.main()
