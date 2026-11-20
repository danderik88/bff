"""Per-printer queue for sliced files."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from pygmy import server  # noqa: E402
from test_filament_newprint import FilamentNewPrint, body, make_3mf  # noqa: E402
from test_bff_print_start import PrintStartTest  # noqa: E402


def jobs(reply):
    return json.loads(reply[0]["text"])["jobs"]


class QueueTests(FilamentNewPrint):
    def test_add_list_remove(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        out = body(server.tool_queue_add({"printer": "a1", "file_id": fid}))
        self.assertIn("id", out)
        j = jobs(server.tool_queue_list({"printer": "a1"}))
        self.assertEqual(len(j), 1)
        self.assertEqual((j[0]["file_id"], j[0]["name"], j[0]["filament"], j[0]["plate"]), (fid, "a.gcode.3mf", "PLA", None))
        server.tool_queue_remove({"printer": "a1", "id": j[0]["id"]})
        self.assertEqual(jobs(server.tool_queue_list({"printer": "a1"})), [])
        self.assertTrue(server.library.exists(fid))

    def test_missing_file_entry_is_dropped(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        server.tool_queue_add({"printer": "a1", "file_id": fid})
        server.library.remove(fid)  # gone outside BFF
        self.assertEqual(jobs(server.tool_queue_list({"printer": "a1"})), [])
        self.assertEqual(server.queued_file_ids(), set())

    def test_remove_own_deletes_file(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        qid = body(server.tool_queue_add({"printer": "a1", "file_id": fid, "own": True}))["id"]
        server.tool_queue_remove({"printer": "a1", "id": qid})
        self.assertFalse(server.library.exists(fid))
        with server.DB:
            self.assertIsNone(server.DB.execute("SELECT 1 FROM uploads WHERE file_id=?", (fid,)).fetchone())

    def test_remove_not_own_keeps_file(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        qid = body(server.tool_queue_add({"printer": "a1", "file_id": fid}))["id"]
        server.tool_queue_remove({"printer": "a1", "id": qid})
        self.assertTrue(server.library.exists(fid))
        with server.DB:
            self.assertIsNotNone(server.DB.execute("SELECT 1 FROM uploads WHERE file_id=?", (fid,)).fetchone())

    def test_remove_twice_says_already_removed(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        qid = body(server.tool_queue_add({"printer": "a1", "file_id": fid}))["id"]
        server.tool_queue_remove({"printer": "a1", "id": qid})
        with self.assertRaisesRegex(server.ToolError, "Already removed"):
            server.tool_queue_remove({"printer": "a1", "id": qid})

    def test_delete_file_drops_queue_entries(self):
        fid = server.upload_file("a.gcode.3mf", make_3mf())["file_id"]
        server.tool_queue_add({"printer": "a1", "file_id": fid})
        server.tool_delete_file({"printer": "a1", "file_id": fid})
        self.assertEqual(jobs(server.tool_queue_list({"printer": "a1"})), [])

    def test_prune_skips_queued(self):
        server.library.setup(str(Path(self.tmp.name) / "files"))
        server.upload_file("seed.gcode.3mf", make_3mf())
        size = (Path(self.tmp.name) / "files" / "1.gcode.3mf").stat().st_size
        server.library.setup(str(Path(self.tmp.name) / "files"), cap_bytes=size * 2 + 1)
        fids = [server.upload_file(f"f{i}.gcode.3mf", make_3mf())["file_id"] for i in range(1, 6)]
        server.tool_queue_add({"printer": "a1", "file_id": fids[-1]})
        server.upload_file("bump.gcode.3mf", make_3mf())
        self.assertTrue(server.library.exists(fids[-1]))


class QueuePrintStartTest(PrintStartTest):
    def test_print_start_pops_queue_entry(self):
        fid = self.upload()["file_id"]
        qid = json.loads(server.tool_queue_add({"printer": "a1", "file_id": fid})[0]["text"])["id"]
        token = json.loads(self.prepare(fid)[-1]["text"])["confirm_token"]
        self.confirm(token)
        self.assertEqual(jobs(server.tool_queue_list({"printer": "a1"})), [])
        self.assertTrue(server.library.exists(fid))
        with server.DB:
            self.assertIsNone(server.DB.execute("SELECT 1 FROM queue WHERE id=?", (qid,)).fetchone())


if __name__ == "__main__":
    unittest.main()
