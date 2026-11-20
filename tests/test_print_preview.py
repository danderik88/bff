"""print_preview: the image of the job printing now, for the Status tab's second page (2026-11-19)."""
import base64
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "bff_mcp"))
import moonraker_link as ml  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from bff_mcp import server  # noqa: E402


def plate_3mf(png=b"plate-png"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28")
        z.writestr("Metadata/plate_1.png", png)
    return buf.getvalue()


class KlipperThumbnail(unittest.TestCase):
    def link(self, metadata, data=b"png"):
        link = ml.MoonrakerLink("k", "host")
        self.calls = {}

        def request(method, path, **query):
            self.calls["meta"] = (method, path, query)
            return metadata

        def get(path):
            self.calls["get"] = path
            return data

        link._request = request
        link._get_bytes = get
        return link

    def test_largest_thumbnail_of_a_file_in_a_subfolder(self):
        meta = {"result": {"thumbnails": [
            {"width": 32, "height": 32, "relative_path": ".thumbs/cube-32x32.png"},
            {"width": 300, "height": 300, "relative_path": ".thumbs/cube-300x300.png"}]}}
        self.assertEqual(self.link(meta).thumbnail("sub/cube.gcode"), b"png")
        self.assertEqual(self.calls["meta"], ("GET", "/server/files/metadata", {"filename": "sub/cube.gcode"}))
        self.assertEqual(self.calls["get"], "/server/files/gcodes/sub/.thumbs/cube-300x300.png")

    def test_no_thumbnails_is_none(self):
        self.assertIsNone(self.link({"result": {}}).thumbnail("cube.gcode"))
        self.assertNotIn("get", self.calls)

    def test_error_is_none(self):
        link = ml.MoonrakerLink("k", "host")
        link._request = mock.Mock(side_effect=OSError("down"))
        self.assertIsNone(link.thumbnail("cube.gcode"))


class FakeBambu:
    def __init__(self, job, state="RUNNING"):
        self.job, self.state = job, state

    def snapshot(self):
        return {"state": self.state, "subtask_name": self.job}


class PrintPreviewTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.open_db(os.path.join(self.tmp.name, "state.db"))
        server.library.setup(os.path.join(self.tmp.name, "files"))
        server.CFG.update(printers={"a1": 1})
        server.LINKS.clear()
        server.PREVIEWS.clear()

    def tearDown(self):
        server.LINKS.clear()
        server.PREVIEWS.clear()
        server.DB.close()
        self.tmp.cleanup()

    def started(self, request_name, upload_name):
        server.library.save(7, plate_3mf())
        with server.DB:
            server.DB.execute("INSERT INTO uploads (file_id, name) VALUES (7, ?)", (upload_name,))
            server.DB.execute("INSERT INTO print_requests (printer, url, created, status, file_id, file_name) "
                              "VALUES ('a1', '', 0, 'started', 7, ?)", (request_name,))

    def preview(self, job, state="RUNNING"):
        server.LINKS["a1"] = FakeBambu(job, state)
        return json.loads(server.tool_print_preview({"printer": "a1"})[0]["text"])

    def test_matching_started_request_gives_the_plate_png(self):
        self.started("part", "part.gcode.3mf")
        got = self.preview("part")
        self.assertEqual(base64.b64decode(got["png"]), b"plate-png")
        self.assertEqual(got["name"], "part")

    def test_non_matching_job_is_null(self):
        self.started("part", "part.gcode.3mf")
        self.assertIsNone(self.preview("something-else")["png"])

    def test_nothing_printing_is_null(self):
        self.started("part", "part.gcode.3mf")
        self.assertEqual(self.preview("part", state="IDLE"), {"png": None, "name": None})


if __name__ == "__main__":
    unittest.main()
