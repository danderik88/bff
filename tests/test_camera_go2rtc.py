"""go2rtc: one camera stream per printer, RTSP for Home Assistant, WebRTC for the app."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src" / "pygmy"))
import camera  # noqa: E402
import go2rtc  # noqa: E402
import onboard  # noqa: E402
import server  # noqa: E402

TOKEN = "t" * 40
CODE = "12345678"
JPEG = b"\xff\xd8frame"


class Stub(BaseHTTPRequestHandler):
    """go2rtc's API, one request at a time: records method, path, content type and body."""
    calls = []
    answer = b"v=0\r\nanswer"

    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        Stub.calls.append({"method": self.command, "path": self.path,
                           "type": self.headers.get("Content-Type"), "body": body})
        self.send_response(200)
        self.send_header("Content-Length", str(len(Stub.answer)))
        self.end_headers()
        self.wfile.write(Stub.answer)

    do_GET = do_PUT = do_DELETE = do_POST = _handle


class StubApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        Stub.calls = []

    def test_add_and_remove_stream(self):
        src = "rtspx://bblp:CODE@1.2.3.4:322/streaming/live/1"
        with mock.patch.object(go2rtc, "API", self.url):
            go2rtc.add_stream("a1", src)
            go2rtc.remove_stream("a1")
        put, delete = [c for c in Stub.calls if c["method"] != "GET"]  # GET: does go2rtc have it already
        path, _, query = put["path"].partition("?")
        self.assertEqual((put["method"], path), ("PUT", "/api/streams"))
        self.assertEqual(urllib.parse.parse_qs(query), {"name": ["a1"], "src": [src]})
        path, _, query = delete["path"].partition("?")
        self.assertEqual((delete["method"], path), ("DELETE", "/api/streams"))
        self.assertEqual(urllib.parse.parse_qs(query), {"src": ["a1"]})

    def test_webrtc_answer(self):
        with mock.patch.object(go2rtc, "API", self.url):
            answer = go2rtc.webrtc_answer("a1", "v=0\r\noffer")
        self.assertEqual(answer, "v=0\r\nanswer")
        call = Stub.calls[0]
        self.assertEqual((call["method"], call["path"], call["type"], call["body"]),
                         ("POST", "/api/webrtc?src=a1", "application/sdp", b"v=0\r\noffer"))


class Offline(unittest.TestCase):
    def test_api_down_is_soft_and_never_logs_the_access_code(self):
        err = StringIO()
        with mock.patch.object(go2rtc, "API", "http://127.0.0.1:1"), redirect_stderr(err):
            go2rtc.add_stream("a1", f"rtspx://bblp:{CODE}@1.2.3.4:322/streaming/live/1")
            go2rtc.remove_stream("a1")
        self.assertNotIn(CODE, err.getvalue())


class Sources(unittest.TestCase):
    def test_camera_kind(self):
        self.assertEqual(onboard.camera_kind("N1"), "jpeg")
        self.assertEqual(onboard.camera_kind("C12"), "jpeg")
        self.assertEqual(onboard.camera_kind("N7"), "rtsp")
        self.assertEqual(onboard.camera_kind("ZZZ"), "jpeg")

    def test_source_for_rtsp_printer(self):
        entry = {"host": "1.2.3.4", "access_code": "CODE", "model": "BL-P001"}
        self.assertEqual(go2rtc.source_for("x1c", entry, "http://127.0.0.1:8765", "t" * 20),
                         "rtspx://bblp:CODE@1.2.3.4:322/streaming/live/1")

    def test_source_for_jpeg_printer_reads_bff_stream(self):
        self.assertEqual(go2rtc.source_for("a1", {"host": "1.2.3.4", "access_code": "CODE", "model": "N2S"},
                                          "http://127.0.0.1:8767", "t" * 20),
                         "ffmpeg:http://127.0.0.1:8767/stream/a1/tttttttttttttttttttt#video=h264")

    def test_source_for_jpeg_without_token_is_none(self):
        self.assertIsNone(go2rtc.source_for("a1", {"host": "1.2.3.4", "access_code": "CODE", "model": "N2S"},
                                            "http://127.0.0.1:8767", ""))


class Go2rtcFeedTest(unittest.TestCase):
    def test_frames_are_cached_for_a_second_and_errors_become_camera_errors(self):
        clock, frames = [0.0], []

        def fetch():
            frames.append(len(frames))
            return JPEG

        feed = camera.Go2rtcFeed("a1", clock=lambda: clock[0], fetch=fetch)
        self.assertEqual(feed.latest(), JPEG)
        clock[0] = 0.5
        self.assertEqual(feed.latest(), JPEG)
        self.assertEqual(len(frames), 1)
        clock[0] = 2.0
        self.assertEqual(feed.latest(), JPEG)
        self.assertEqual(len(frames), 2)
        with self.assertRaises(camera.CameraError):
            camera.Go2rtcFeed("a1", fetch=mock.Mock(side_effect=OSError("refused"))).latest()


class Tools(unittest.TestCase):
    def setUp(self):
        server.CFG.update(printers={"a1": 1}, tokens={"iphone": TOKEN}, control=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"STATE_DIRECTORY": self.tmp.name})
        self.env.start()
        for name in ("BFF_RTSP_USER", "BFF_RTSP_PASS", "BFF_PUBLIC_HOST"):
            os.environ.pop(name, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def urls(self):
        return json.loads(server.tool_camera_urls({"printer": "a1"})[0]["text"])

    def test_rtsp_is_null_without_env(self):
        got = self.urls()
        self.assertIsNone(got["rtsp"])
        self.assertEqual(got["kind"], "jpeg")
        self.assertNotIn("still", got)
        self.assertNotIn("mjpeg", got)

    def test_rtsp_url_from_env(self):
        with mock.patch.dict(os.environ, {"BFF_RTSP_USER": "u", "BFF_RTSP_PASS": "p",
                                          "BFF_PUBLIC_HOST": "bff.example"}):
            self.assertEqual(self.urls()["rtsp"], "rtsp://u:p@bff.example:8554/a1")

    def test_webrtc_refused_for_jpeg_printers(self):
        with self.assertRaisesRegex(server.ToolError, "no video stream"):
            server.tool_camera_webrtc({"printer": "a1", "offer": "v=0"})


class AppPage(unittest.TestCase):
    def test_webrtc_and_home_assistant_camera_are_in_the_page(self):
        page = (ROOT / "src" / "pygmy" / "app.html").read_text()
        for text in ("RTCPeerConnection", "camera_webrtc", "Home Assistant camera",
                     "Add it in Home Assistant as a Generic Camera (stream source).",
                     "RTSP not set up on this server"):
            self.assertIn(text, page)
        for italian in ("Stampante", "Errore", "Impostazioni", "Annulla", "Chiudi"):
            self.assertNotIn(italian, page)


if __name__ == "__main__":
    unittest.main()


class AddStreamOnce(unittest.TestCase):
    """A stream go2rtc already has with the same source is not PUT again (each PUT rewrites its config)."""

    def test_second_put_skipped_until_source_changes(self):
        calls, streams = [], {}

        def fake(method, endpoint, query, **_):
            calls.append(method)
            if method == "PUT":
                streams[query["name"]] = query["src"]
            return json.dumps(streams).encode() if method == "GET" else b""
        go2rtc._SENT.clear()
        with mock.patch.object(go2rtc, "_request", fake):
            go2rtc.add_stream("a1", "ffmpeg:x")
            go2rtc.add_stream("a1", "ffmpeg:x")
            go2rtc.add_stream("a1", "ffmpeg:y")
        self.assertEqual(calls.count("PUT"), 2)
